from __future__ import annotations

"""파일 → AdDocument 오케스트레이션 — 설계서 3절 Track B.

라우팅(설계서 4.1):
  PNG/JPG  → OCR 트랙 직행
  PDF      → 페이지 단위 triage → digital / ocr / hybrid
  HWP/HWPX → 사내 파서 디지털 추출
"""

import re
import statistics
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from .canvas import CanvasPage, load_image_canvas, render_pdf_page, rgb_on_white
from .config import SETTINGS
from .gemma_client import classify
from .hwp_ingest import ingest_hwp
from .ir import AdDocument, AdPage, Line, Region
from .paddlex_client import LayoutBlock, request_layout_parsing
from .regions import build_regions
from .tiling import dedupe_lines, make_tiles, restore_coords
from .vlm_judge import judge_region_roles

IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
HWP_EXTS = {".hwp", ".hwpx"}


def process_file(path: Path, preview_dir: Path | None = None) -> AdDocument:
    ext = path.suffix.lower()
    if ext in HWP_EXTS:
        return _process_hwp(path, preview_dir)
    if ext in IMAGE_EXTS:
        return _process_image(path, preview_dir)
    if ext == ".pdf":
        return _process_pdf(path, preview_dir)
    doc = AdDocument(doc_id=path.stem, source_file=path.name, file_type=ext.lstrip("."))
    doc.notes.append(f"미지원 확장자 {ext} — 판독 불가 처리 (F-001 예외)")
    doc.pages.append(AdPage(page_no=1, parse_route="ocr", parse_status="unreadable"))
    return doc


def _process_image(path: Path, preview_dir: Path | None) -> AdDocument:
    doc = AdDocument(doc_id=path.stem, source_file=path.name, file_type="image")
    canvas = load_image_canvas(path)
    page = _ocr_canvas_to_page(canvas, page_no=1)
    _apply_vlm_judgments(page, canvas.image)
    doc.pages.append(page)
    _classify_into(doc, canvas.image)
    if preview_dir:
        _save_preview(canvas, page, preview_dir, doc.doc_id)
    return doc


def _process_hwp(path: Path, preview_dir: Path | None) -> AdDocument:
    """HWP: 텍스트·표는 디지털 정본, 내장 이미지는 PNG 와 동일한 OCR/VLM 트랙."""

    def _image_page(img: Image.Image, page_no: int) -> AdPage:
        canvas = CanvasPage(image=rgb_on_white(img), page_no=page_no)
        page = _ocr_canvas_to_page(canvas, page_no)
        _apply_vlm_judgments(page, canvas.image)
        if preview_dir:
            _save_preview(canvas, page, preview_dir, path.stem)
        return page

    return ingest_hwp(path, image_page_processor=_image_page)


def _process_pdf(path: Path, preview_dir: Path | None) -> AdDocument:
    from .triage import extract_digital_lines, triage_page

    doc = AdDocument(doc_id=path.stem, source_file=path.name, file_type="pdf")
    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as exc:
        # 폴백 체인(설계서 4.3): pdfium 조차 실패 → 판독 불가
        doc.notes.append(f"pdfium 파싱 실패: {exc}")
        doc.pages.append(AdPage(page_no=1, parse_route="ocr", parse_status="unreadable"))
        return doc

    from .canvas import native_image_dpi

    first_canvas: Image.Image | None = None
    for i, pdf_page in enumerate(pdf):
        page_no = i + 1
        verdict = triage_page(pdf_page)
        # 이미지 기반 페이지는 내장 래스터의 원본 해상도로 렌더 (업스케일 금지)
        dpi = None
        if verdict.verdict in ("scan_like", "hybrid"):
            dpi = native_image_dpi(pdf_page)
        canvas = render_pdf_page(pdf_page, page_no, dpi=dpi)
        if first_canvas is None:
            first_canvas = canvas.image

        if verdict.verdict == "structured":
            lines = extract_digital_lines(pdf_page, canvas.px_per_pt)
            page = _assemble_page(canvas, lines, blocks=[], route="digital", page_no=page_no)
        elif verdict.verdict == "hybrid":
            digital = extract_digital_lines(pdf_page, canvas.px_per_pt)
            page = _ocr_canvas_to_page(canvas, page_no, extra_digital=digital, route="hybrid")
        else:  # scan_like
            page = _ocr_canvas_to_page(canvas, page_no)
        page.triage = verdict.as_dict()
        _apply_vlm_judgments(page, canvas.image)
        doc.pages.append(page)
        if preview_dir:
            _save_preview(canvas, page, preview_dir, doc.doc_id)

    if first_canvas is not None:
        _classify_into(doc, first_canvas)
    return doc


def _ocr_canvas_to_page(
    canvas: CanvasPage,
    page_no: int,
    extra_digital: list[Line] | None = None,
    route: str = "ocr",
) -> AdPage:
    """캔버스 → 타일링 → PaddleX → 좌표 복원 → 영역/필드 (설계서 6.3~6.6)."""
    tiles = make_tiles(canvas.image)
    all_lines: list[Line] = []
    all_blocks: list[LayoutBlock] = []
    errors: list[str] = []
    for tile in tiles:
        try:
            result = request_layout_parsing(tile.image)
        except Exception as exc:
            errors.append(f"y={tile.y_offset}: {exc}")
            continue
        all_lines.extend(restore_coords(result.ocr_lines, tile.y_offset))
        for block in result.blocks:
            shifted = LayoutBlock(
                label=block.label,
                bbox=[block.bbox[0], block.bbox[1] + tile.y_offset,
                      block.bbox[2], block.bbox[3] + tile.y_offset],
                score=block.score,
                content=block.content,
                source=block.source,
            )
            all_blocks.append(shifted)

    lines = dedupe_lines(all_lines)
    if extra_digital:
        # 디지털 우선(설계서 원칙 4): OCR 라인 중 디지털과 겹치는 것 제거
        lines = _merge_digital_ocr(extra_digital, lines)

    page = _assemble_page(canvas, lines, all_blocks, route=route, page_no=page_no)
    if errors:
        page.parse_status = "partial" if lines else "unreadable"
        page.notes.extend(f"OCR 타일 실패 {msg}" for msg in errors)
    page.notes.append(f"타일 {len(tiles)}개 처리 (오버랩 {SETTINGS.tile_overlap_px}px)")
    return page



def _apply_vlm_judgments(page: AdPage, canvas_img: Image.Image | None) -> None:
    """VLM 이 판단 주체 (설계서 6.5): 영역 역할 판정 + 밴드 통합판독(교정+누락 회수).

    실패 시에만 규칙/regex 폴백을 유지하고, 그 사실을 notes 에 기록한다
    (조용한 실패 금지 원칙).
    """
    all_lines = [l for r in page.regions for l in r.lines] + page.unassigned_lines

    # 카드-분할(§D): 좌우로 나란히 배치된 카드(003 EVENT1/EVENT2 등)가 있으면 카드
    # 단위로 먼저 묶는다. 2026-08-03 섹션 제거 때 "group_no 는 섹션 전용"이라고 잘못
    # 판단해 이 호출을 통째로 껐었는데, 실제로는 **읽기순서**(llm_view 정렬)가 카드
    # 경계를 넘어 섞이지 않게 하는 데도 쓰이고 있었다 — 실측(003): 카드 배정 없이
    # 좌표만으로 정렬하면 EVENT1/EVENT2 문장이 y좌표가 비슷해 한 줄씩 번갈아 나온다.
    # 스크롤·단일패널은 게이트에서 호출 자체를 안 한다(assign_cards_vlm 내부).
    if canvas_img is not None:
        from .cards import assign_cards_vlm

        try:
            cards = assign_cards_vlm(page, canvas_img, votes=SETTINGS.card_split_votes)
            for r in page.regions:
                r.card_no = cards.get(r.region_id)
            ncards = len({c for c in cards.values() if c > 0})
            if ncards:
                page.notes.append(f"카드-분할(§D): {ncards}개 카드로 그룹핑 (VLM 판정, 읽기순서용)")
        except Exception as exc:
            page.notes.append(f"카드-분할 실패(좌표 순서 유지): {exc}")

    # 영역 역할 판정 — 섹션(의미 묶음)은 2026-08-03 제거했다(vlm_judge 상단 주석).
    # 같이 빠진 것: 장식예시 격리(section_type 의존), 미배정 낱줄의 VLM 내용 귀속.
    # 둘 다 흔들리는 의미 판정이었고 후속 계약에 담을 자리도 없었다.
    try:
        judge_region_roles(page.regions, canvas_img, page.canvas_h)
    except Exception as exc:
        page.notes.append(f"VLM 역할 판정 실패 → 규칙 폴백(_refine_role) 유지: {exc}")

    # 미배정 라인 귀속 — 좌표만 보는 결정론 1단계로 축소(예전 2단계 중 VLM 단 제거).
    _absorb_unassigned_into_regions(page)

    # VLM 직독 폴백 1: OCR/디지털이 놓친 시각 전용 텍스트 회수 (설계서 6.4).
    # 장식 타이포·벡터(패스) 텍스트는 어느 라우트에서도 빠질 수 있어 전 페이지 공통.
    # 복수 관측(개선 A): 스윕은 서버측 비결정성으로 실행마다 회수 문구가 흔들린다
    # (temperature=0 이어도 재현 — guided-decoding/배치 비결정성). 필드에 쓴 것과
    # 같은 관측→합집합 패턴으로 여러 번 돌려 회수율을 안정화한다. 2회차에는 1회차
    # 결과를 이미 있는 것으로 넘겨 새 문구만 받으므로(자연 합집합) 중복이 없다.
    if canvas_img is not None:
        # 밴드 1장 = ④+ 교정 + 밴드 스윕을 한 호출로(_merged_band_read). 통짜 스윕만
        # 따로 남긴다 — 밴드가 원래 못 잡는 대형 장식 타이포용(002 '행운의 777 이벤트').
        from .vlm_direct import reread_low_confidence_lines, sweep_missing_lines

        swept = _merged_band_read(page, canvas_img, all_lines)
        try:
            new_lines, notes = sweep_missing_lines(
                all_lines + swept, canvas_img, page.canvas_w, page.canvas_h, banded=False,
            )
            page.notes.extend(f"스윕(통짜): {n}" for n in notes)
            swept.extend(new_lines)
        except Exception as exc:
            page.notes.append(f"통짜 스윕 실패(원 결과 유지): {exc}")
        if swept:
            swept = _resolve_sweep_duplicates(page, swept, canvas_img)
        if swept:
            page.unassigned_lines.extend(swept)
            page.notes.append(
                "VLM 스윕 회수 문구 " + ", ".join(f"'{l.text[:30]}'" for l in swept)
            )
        all_lines = [l for r in page.regions for l in r.lines] + page.unassigned_lines

        # 저신뢰 OCR 라인 크롭 재판독 (2b): 심의 관련 영역의 낮은 신뢰도 라인을
        # 고해상 재판독으로 교정한다. 예시/장식·이미지 영역은 제외(무관·비용).
        try:
            page.notes.extend(reread_low_confidence_lines(page.regions, canvas_img))
        except Exception as exc:
            page.notes.append(f"저신뢰 라인 재판독 실패(원값 유지): {exc}")

    _note_layout_gaps(page)

    # 필드는 STAGE_3(스키마 기반) 단일 창구다 — 파싱 단계에서는 뽑지 않는다.
    # 읽기 순서 국소 재정렬: 전역 정렬이 긴 페이지에서 시각적 줄을 조각내는 문제
    # (001 단어 섞임 실측)를 영역 범위 재정렬로 교정한다.
    _finalize_reading_order(page)


def _note_layout_gaps(page: AdPage) -> None:
    """StructureV3 가 통째로 놓친 블록이 있으면 노트로 남긴다 (진단 전용).

    **왜 감지만 하나.** 놓친 블록을 유사 영역으로 승격하면 섹션 판정과 STAGE_3 입력이
    모두 흔들린다 — 지금 필드 44/44 인 상태를 검증 없이 위험에 빠뜨릴 이유가 없다.
    먼저 재고, 재는 게 쓸모 있다는 근거가 쌓이면 그때 붙인다.

    **지금 이 신호로 무엇을 아나.** 미배정 낱줄이 27개라는 숫자만으로는 '레이아웃이
    깨진 것'인지 '원래 흩어진 화면'인지 모른다. 이 노트는 그중 **응집한 덩어리**만
    골라내 구분해 준다. 실측(2026-07-29, 5문서): 001 p1 이 8줄짜리 덩어리 하나,
    003 p3 이 2줄 하나. 001 의 덩어리는 앱 목업의 UI 바('로그아웃', '김농협님>',
    '큰글')였고 미배정으로 남는 게 맞았다 — 즉 **이번 샘플에서는 지표를 못 올렸다.**
    다른 입력(대출성·HWP 렌더·신규 양식)에서 진짜 유실을 조기에 잡자는 목적이다.
    """
    from .layout_gap import missed_blocks

    assigned = [l.bbox for r in page.regions for l in r.lines if l.bbox]
    orphan = [l.bbox for l in page.unassigned_lines if l.bbox]
    if not orphan:
        return
    blocks = missed_blocks(assigned, orphan)
    if blocks:
        page.notes.append(
            "레이아웃 미검출 블록(진단): "
            + ", ".join(f"{b.line_count}줄@y={b.bbox[1]}" for b in blocks)
            + f" / 미배정 낱줄 {len(orphan)}개 중"
        )


def _finalize_reading_order(page: AdPage) -> None:
    """각 영역의 라인을 국소적으로 읽기 순서 재정렬한다.

    build_regions 는 전역 정렬된 라인 목록을 영역에 분배하는데, 전역
    sort_reading_order 는 각 라인을 '직전 행'하고만 비교하므로 세로로 긴
    페이지에서는 같은 시각적 줄의 단어들 사이에 다른 위치의 줄이 끼어들어
    행 묶기가 깨진다(단어가 top 좌표순으로만 나열 — 001 실측). 영역 하나로
    범위를 좁혀 다시 정렬하면 끼어드는 무관한 줄이 없어 올바른 순서가 된다.
    """
    from .tiling import sort_reading_order

    for region in page.regions:
        if len(region.lines) > 1:
            region.lines = sort_reading_order(region.lines)


def _attach_line_to_region(line: Line, target: Region) -> bool:
    """미배정 라인을 영역에 붙이고 영역 bbox 를 확장한다.

    예전에는 섹션을 게이트로 두고 그 안의 최근접 영역에 붙였는데(_attach_line_to_section),
    섹션 자체가 실행마다 흔들려 같은 낱줄이 실행마다 다른 영역에 붙었다 — 001 실측에서
    11줄이 영역↔미배정 사이를 오갔다. 이제 영역만 보고 결정한다(순수 좌표, VLM 무관).
    """
    from .tiling import sort_reading_order

    if not line.bbox or not target.bbox:
        return False
    target.lines.append(line)
    target.lines = sort_reading_order(target.lines)
    target.bbox = [
        min(target.bbox[0], line.bbox[0]), min(target.bbox[1], line.bbox[1]),
        max(target.bbox[2], line.bbox[2]), max(target.bbox[3], line.bbox[3]),
    ]
    return True


def _vertical_gap(box: list[int], other: list[int]) -> int:
    """두 박스 사이 수직 갭(px). 겹치면 0."""
    if box[1] > other[3]:
        return box[1] - other[3]
    if box[3] < other[1]:
        return other[1] - box[3]
    return 0


def _column_overlap(line_box: list[int], region_box: list[int]) -> float:
    """라인의 가로 구간이 영역의 가로 구간에 얼마나 들어가는가 (라인 폭 기준 0~1).

    낱줄 귀속에서 **세로 갭만 보면 안 되는** 이유를 막는 게이트다. 가로로 나란한
    패널(003: 표지·내지1·내지2 가 좌우 3분할)에서는 캔버스 반대쪽 영역도 세로 갭이
    0이라, 가로를 안 보면 '내지2' 라벨(x≈1587)이 표지 영역(x≈104~491)에 붙어
    영역 bbox 가 패널 3개를 관통한다(2026-08-03 실측: 영역 17개 폭 확대, 그중 12개가
    캔버스 절반 이상 관통). 같은 칼럼에 있는 영역만 후보로 남긴다.
    """
    overlap = min(line_box[2], region_box[2]) - max(line_box[0], region_box[0])
    width = max(1, line_box[2] - line_box[0])
    return max(0, overlap) / width


_MIN_COLUMN_OVERLAP = 0.5  # 라인 폭의 절반 이상이 영역 가로 구간에 들어와야 후보


def _absorb_unassigned_into_regions(page: AdPage) -> None:
    """정밀 bbox 미배정 라인을 좌표만 보고 영역에 귀속시킨다 (VLM 무관, 결정론).

    - 대상: source 가 ocr/digital 인 라인만 (vlm_sweep 은 근사 밴드 bbox 라 제외)
    - 1순위: 라인 중심점을 품는 영역 — 여럿이면 가장 작은(구체적인) 영역
    - 2순위: 품는 영역이 없으면 **같은 칼럼에 있는**(가로 구간이 라인 폭의 절반 이상
      겹치는) 영역 중 수직 갭이 임계 이내인 최근접 영역. 임계는 이 저장소가 이미
      쓰던 캔버스 비례 값(max(300, canvas_h*0.15))을 그대로 쓴다 — 새 튜닝 상수를
      만들지 않기 위해서다. 가로 게이트는 _column_overlap 주석 참조(없으면 좌우 분할
      패널에서 영역이 패널 경계를 관통한다).
    - 둘 다 실패하면 미배정 유지. 미배정도 llm_view 의 `unassigned` 로 STAGE_3 에
      전달되므로 텍스트가 사라지지는 않는다(근거 지목이 영역 단위로 안 될 뿐).

    2026-08-03 재배선: 예전에는 섹션 bbox 를 게이트로 썼는데, 섹션이 VLM 산물이라
    실행마다 흔들렸고 그 탓에 같은 낱줄이 실행마다 다른 영역에 붙었다.
    """
    boxed = [r for r in page.regions if r.bbox]
    if not boxed:
        return
    gap_limit = max(300, int(page.canvas_h * 0.15)) if page.canvas_h else None
    remaining: list[Line] = []
    absorbed = near = 0
    for line in page.unassigned_lines:
        if not line.bbox or line.source not in ("ocr", "digital"):
            remaining.append(line)
            continue
        cx = (line.bbox[0] + line.bbox[2]) / 2
        cy = (line.bbox[1] + line.bbox[3]) / 2
        inside = [
            r for r in boxed
            if r.bbox[0] <= cx <= r.bbox[2] and r.bbox[1] <= cy <= r.bbox[3]
        ]
        if inside:
            target = min(
                inside, key=lambda r: (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1])
            )
            is_near = False
        elif gap_limit is not None:
            # 같은 칼럼(가로 구간 겹침)에 있는 영역만 후보. 동률(세로 갭 0이 여럿)은
            # 가로 겹침이 큰 쪽 → region_id 순으로 갈라 실행마다 같은 답이 나오게 한다.
            cands = [
                r for r in boxed
                if _column_overlap(line.bbox, r.bbox) >= _MIN_COLUMN_OVERLAP
            ]
            if not cands:
                remaining.append(line)
                continue
            target = min(
                cands,
                key=lambda r: (
                    _vertical_gap(line.bbox, r.bbox),
                    -_column_overlap(line.bbox, r.bbox),
                    r.region_id,
                ),
            )
            if _vertical_gap(line.bbox, target.bbox) > gap_limit:
                remaining.append(line)
                continue
            is_near = True
        else:
            remaining.append(line)
            continue
        if _attach_line_to_region(line, target):
            absorbed += 1
            near += 1 if is_near else 0
        else:
            remaining.append(line)
    if absorbed:
        page.unassigned_lines = remaining
        page.notes.append(
            f"미배정 라인 {absorbed}개를 좌표 기준 영역에 귀속 (포함 {absorbed - near} / 근접 {near})"
        )



_MAX_SWEEP_RESOLVE = 8  # 페이지당 스윕-OCR 중복 크롭 재판독 상한 (비용 가드)


def _stable_core(text: str) -> str:
    """숫자·기호·공백·원문자 번호를 제거한 안정 핵심부 (같은 줄 판정용).

    OCR 이 ①→'1' 로 뭉개 읽거나 소수점을 잃어도, 숫자·기호를 빼면 두 판독의
    본문이 같아진다('10.1%p:NH올원e통장...' 와 '① 0.1%p : NH올원e통장...'
    → 둘 다 'nh올원e통장...'). 이 핵심부로 중복을 탐지한다.
    """
    return re.sub(
        r"[0-9①②③④⑤⑥⑦⑧⑨⑩\s%p.,:;·「」『』\"'`~()\[\]{}\-—]+", "", text
    ).lower()


def _find_twin(sweep: Line, known: list[Line]) -> Line | None:
    """스윕 라인과 '같은 물리적 줄'인 기존 라인을 찾는다 — 정규화 후 부분문자열 포함.

    **유사도 임계값은 일부러 안 쓴다.** 포함 관계로는 못 잡는 중복이 있어서(가운데
    단어만 다르게 읽은 경우) 유사도나 최장공통부분열로 넓혀 보려고 실측했는데,
    4문서 전체 스윕 라인에서 **진짜 중복과 오탐이 겹쳐** 임계값을 그을 자리가 없었다
    (2026-07-29):

        유사도 0.727  '정년드림정약통상)은 제외'  ← 진짜 중복(OCR 오독)
        유사도 0.700  '[NH저축TEENZ]'            ← 오탐(올원TEENZ통장과 다른 상품)
        최장공통 0.27 '정년드림정약통상)은 제외'  ← 같은 진짜 중복이 최하위

    OCR 오독은 글자가 여기저기 틀려 공통 구간이 끊기므로 두 지표 모두 뒤집힌다.
    좌표로 가르는 것도 안 된다 — 스윕 bbox 는 y_ratio 추정치라 진짜 중복이 1863px
    떨어져 나온 사례가 있었다. 여기서 임계값을 정하면 이 4문서에 맞추는 것이므로,
    표본이 늘거나 더 나은 신호를 찾을 때까지 포함 관계만 유지한다.
    """
    s_core = _stable_core(sweep.text)
    for line in known:
        k_core = _stable_core(line.text)
        if len(k_core) >= 4 and (s_core in k_core or k_core in s_core):
            return line
    return None


def _resolve_sweep_duplicates(
    page: AdPage, swept: list[Line], canvas_img: Image.Image
) -> list[Line]:
    """스윕 라인이 기존 OCR/디지털 라인의 다른 판독(중복)이면 크롭 재판독해 **후보를 붙인다**.

    스윕 자체 dedup 은 정규화 완전일치만 잡으므로, OCR 이 원문자 번호를 숫자에
    합쳐 읽거나 소수점을 잃은 경우(올원 '10.1%p' vs 스윕 '① 0.1%p')는 못 걸러
    두 판본이 공존한다. 여기서 숫자·기호를 뺀 안정 핵심부로 같은 줄임을 탐지하고,
    그 위치를 고해상 재판독해(심판 — 규칙으로 우열을 정하지 않음) `Line.vlm_reading`
    후보로 붙인 뒤 중복 스윕 라인을 제거한다. 좌표는 정밀한 OCR 쪽 유지(best-of-both).

    **정본은 덮지 않는다**(2026-08-03 변경). 예전에는 OCR 라인 텍스트에 대입했는데,
    이 단계의 트리거가 스윕 회수 문구이고 스윕은 실행마다 다른 것을 회수하므로
    정본이 실행마다 흔들렸다 — 같은 코드·같은 입력 2회 실측에서 갈린 정본 2줄이
    전부 이 경로였다(ir.Line.vlm_reading 주석 참조).

    반환: 병합되지 않고 남은 스윕 라인들 (unassigned 로 추가될 것).
    """
    from .vlm_direct import transcribe_line_crop

    # 미배정 낱줄도 비교 대상에 넣는다 — 버그(2026-07-29 발견): 영역 라인만 보느라,
    # 소속 없는 OCR 줄을 스윕이 다시 읽어 만든 **오독 판본이 그대로 남았다**. 실측
    # (003 p2): 원본 3줄이 6줄이 됐고 늘어난 3줄은 원본에 없는 문구였다 —
    # '금융상품을 가입하시기 전에' → '금융상품을 개방하거나 판매할 시에는'.
    # 심의는 '무엇이 쓰여 있나'를 판정하므로 없는 문장이 들어가면 허위 지적이 된다.
    known_lines = [
        l for l in ([l for r in page.regions for l in r.lines] + page.unassigned_lines)
        if l.source in ("ocr", "digital") and l.bbox
    ]
    remaining: list[Line] = []
    resolved = 0
    for s in swept:
        s_core = _stable_core(s.text)
        if len(s_core) < 4:
            remaining.append(s)
            continue
        twin = _find_twin(s, known_lines)
        if twin is None or resolved >= _MAX_SWEEP_RESOLVE:
            remaining.append(s)
            continue
        reading = transcribe_line_crop(twin.bbox, canvas_img)
        resolved += 1
        # 재판독이 같은 줄을 읽었는지 확인(엉뚱한 크롭 방어) — 아니면 둘 다 유지
        r_core = _stable_core(reading)
        if reading and r_core and (r_core in s_core or s_core in r_core):
            old = twin.text
            if old != reading:
                # 정본을 덮지 않는다(ir.Line.vlm_reading 주석 참조) — 이 단계의 트리거가
                # 스윕 회수 문구이고 스윕은 실행마다 다른 것을 회수하므로, 여기서 대입하면
                # '이번엔 고쳐지고 다음엔 안 고쳐지는' 정본이 된다. 후보로만 남기고 선택은
                # 하류에 맡긴다. 스윕 라인 자체는 여전히 중복이므로 remaining 에 안 넣는다.
                twin.vlm_reading = reading
                twin.vlm_reading_stage = "sweep_dedupe"
                page.notes.append(
                    f"스윕-OCR 중복 재판독 후보 부착(정본 유지): 정본 {old!r} ← 후보 {reading!r} "
                    f"(스윕판 {s.text[:30]!r} 확인)"
                )
        else:
            remaining.append(s)
    return remaining


def _merge_digital_ocr(digital: list[Line], ocr: list[Line]) -> list[Line]:
    from .tiling import _iou, sort_reading_order

    kept = list(digital)
    for line in ocr:
        overlapped = any(
            d.bbox and line.bbox and _iou(d.bbox, line.bbox) >= 0.5 for d in digital
        )
        if not overlapped:
            kept.append(line)
    return sort_reading_order(kept)


def _assemble_page(
    canvas: CanvasPage, lines: list[Line], blocks: list[LayoutBlock], route: str, page_no: int
) -> AdPage:
    w, h = canvas.image.size
    regions, unassigned = build_regions(blocks, lines, canvas_h=h, page_no=page_no)
    if not regions and lines:
        regions = _pseudo_regions(lines, h, page_no)
        unassigned = []
    all_lines = [l for r in regions for l in r.lines] + unassigned
    return AdPage(
        page_no=page_no,
        canvas_w=w,
        canvas_h=h,
        dpi=canvas.dpi,
        parse_route=route,  # type: ignore[arg-type]
        parse_status="ok" if all_lines else "partial",
        regions=regions,
        unassigned_lines=unassigned,
        # 필드는 STAGE_3(스키마 기반) 단일 출처다. 파싱 단계에서 regex 로 뽑아 두면
        # 같은 이름의 값이 두 곳에 생겨 어느 쪽이 정본인지 헷갈린다(실측: eval 의 '필드'
        # 지표가 ⑥-4 를 보고 있어서 STAGE_3 개선이 지표에 안 잡혔다).
        extracted_fields=[],
    )


def _pseudo_regions(lines: list[Line], canvas_h: int, page_no: int) -> list[Region]:
    """레이아웃 블록이 없을 때(디지털 전용 페이지) 수직 간격 군집으로 문단 영역 생성."""
    from .regions import _refine_role

    boxed = [l for l in lines if l.bbox]
    if not boxed:
        return [Region(region_id=f"p{page_no}_r000", label="page", role="본문", lines=lines)]
    heights = [l.bbox[3] - l.bbox[1] for l in boxed]
    gap_limit = 1.5 * statistics.median(heights)
    groups: list[list[Line]] = [[boxed[0]]]
    for line in boxed[1:]:
        if line.bbox[1] - groups[-1][-1].bbox[3] <= gap_limit:
            groups[-1].append(line)
        else:
            groups.append([line])
    regions = []
    for i, group in enumerate(groups):
        bbox = [
            min(l.bbox[0] for l in group),
            min(l.bbox[1] for l in group),
            max(l.bbox[2] for l in group),
            max(l.bbox[3] for l in group),
        ]
        region = Region(
            region_id=f"p{page_no}_r{i:03d}", bbox=bbox, label="text_cluster",
            role="본문", lines=group,
        )
        _refine_role(region, canvas_h)
        regions.append(region)
    return regions


def _classify_into(doc: AdDocument, canvas: Image.Image) -> None:
    result = classify(canvas, doc.source_file)
    doc.product_group = result.product_group
    doc.ad_type = result.ad_type
    doc.category_source = result.category_source
    doc.classification_confidence = result.confidence
    if result.reason:
        doc.notes.append(f"분류: {result.reason}")


# 미리보기 박스 색 — **의미가 아니라 계층만** 나타낸다.
#
# 2026-08-03: 역할별 색상(제목=초록/유의사항=빨강/…)을 없앴다. 미리보기의 용도는
# "이미지에서 글자를 어디서 어떻게 잡았는가"를 보이는 것이고, 그건 라인·영역 좌표로
# 끝난다(실측 재현율 100%). 역할은 VLM 의미 판단이라 같은 입력에서도 실행마다 달라질 수
# 있어(실측 225개 중 6개 = 97.3% 일치) 그림에 색으로 박으면 "파싱이 이렇게 잡았다"는
# 그림이 실행마다 달라 보인다. 역할 자체는 llm_view·하류 계약(LayoutBlock.blockType)에
# 그대로 남는다 — 그림에서만 뺀다.
#
# 같은 색조의 농도 차만 쓴다(다른 색조를 쓰면 그게 다시 '종류'로 읽힌다).
_LINE_BOX_COLOR = (255, 105, 215)   # 라인(OCR 이 검출한 한 줄) — 연한 마젠타, 얇은 선
_REGION_BOX_COLOR = (190, 0, 130)   # 영역(라인들의 묶음) — 진한 마젠타, 굵은 선


def _save_preview(canvas: CanvasPage, page: AdPage, preview_dir: Path, doc_id: str) -> None:
    """원본 위에 라인·영역 bbox 만 올린 검수용 이미지.

    가는 선 = 라인, 굵은 선 = 영역. 의미(역할·섹션) 표시는 하지 않는다 — 위 주석 참조.
    """
    preview_dir.mkdir(parents=True, exist_ok=True)
    img = canvas.image.copy()
    draw = ImageDraw.Draw(img)
    for region in page.regions:
        if not region.bbox:
            continue
        for line in region.lines:
            if line.bbox:
                draw.rectangle(line.bbox, outline=_LINE_BOX_COLOR, width=1)
        # 영역을 라인 뒤에 그려 겹칠 때 경계가 가려지지 않게 한다
        draw.rectangle(region.bbox, outline=_REGION_BOX_COLOR, width=3)
    # 어느 영역에도 안 묶인 낱줄도 '잡은 글자'다 — 안 그리면 그림이 실제보다 덜 잡은
    # 것처럼 보인다. 굵은 영역 박스가 없는 얇은 박스로 보이므로 '미배정'이 그림에서
    # 그대로 읽힌다. vlm_sweep 은 밴드 근사 bbox(폭이 캔버스 전체)라 좌표 그림에
    # 올리면 오해를 만들어 제외한다 — 텍스트 자체는 llm_view 의 unassigned 로 전달된다.
    for line in page.unassigned_lines:
        if line.bbox and line.source in ("ocr", "digital"):
            draw.rectangle(line.bbox, outline=_LINE_BOX_COLOR, width=1)
    if img.width > 1400:
        ratio = 1400 / img.width
        img = img.resize((1400, int(img.height * ratio)))
    img.save(preview_dir / f"{doc_id}_p{page.page_no}.jpg", quality=80)


def _merged_band_read(page: AdPage, canvas_img: Image.Image, all_lines: list[Line]) -> list[Line]:
    """밴드 하나당 1회 호출로 ④+ 통독 교정과 ⑧ 누락 회수를 함께 처리한다.

    OCR 이 본 것과 **같은 크롭**(make_tiles)을 VLM 에도 준다. 그러면 영역이 어느 밴드에
    속하는지가 계산 없이 맞아떨어지고(영역이 그 타일의 레이아웃 블록에서 나왔으므로),
    "이 영역들을 고쳐라 + 목록에 없는 문구를 찾아라"를 한 번에 물을 수 있다.

    남기는 것: 전체 캔버스 통짜 스윕 1회. 밴드가 원래 못 잡는 대형 장식 타이포를 잡는
    패스이고(실측: 002 '행운의 777 이벤트'), 페이지당 1회라 싸다.

    반환: 회수된 누락 라인들(호출측이 unassigned 로 편입).
    """
    from .bands import content_bands
    from .field_judge import check_field_consistency
    from .truncation import Relation, classify_reading
    from .vlm_direct import read_band_regions

    bands = content_bands(canvas_img, span=SETTINGS.tile_max_height_px,
                          max_span=SETTINGS.tile_max_height_px)
    known = _n_join(all_lines)
    recovered: list[Line] = []
    truncated: list[tuple[str, Relation]] = []
    corrected = attached = expanded = 0

    # 영역을 밴드에 **정확히 하나씩** 배정한다. 밴드는 서로 200px 겹치므로(오버랩은
    # 경계 글자를 놓치지 않으려고 일부러 둔 것) '중심이 이 밴드 범위에 드는가'로 고르면
    # 겹침 구간의 영역이 양쪽에 다 들어간다 — 버그(2026-07-29 발견): 올원e 50개 중
    # 12개(24%)가 두 밴드에서 각각 판독되고 뒤 밴드 결과가 앞 것을 조용히 덮어썼다.
    # 같은 영역을 두 번 읽으니 프롬프트도 그만큼 커지고, 어느 판독이 남을지도 밴드
    # 순서에 달린 우연이었다. 겹친 높이가 가장 큰 밴드 하나에만 준다(동률이면 위쪽).
    owner: dict[int, list[Region]] = {}
    for region in page.regions:
        if not (region.bbox and region.lines) or region.is_illustrative:
            continue
        y0, y1 = region.bbox[1], region.bbox[3]
        best_i, best_overlap = None, 0
        for i, band in enumerate(bands):
            top, bottom = band.offset, band.offset + band.image.height
            covered = min(y1, bottom) - max(y0, top)
            if covered > best_overlap:
                best_i, best_overlap = i, covered
        if best_i is None:  # 어느 밴드와도 안 겹치는 영역은 중심으로 떨어뜨린다
            mid = (y0 + y1) // 2
            best_i = min(
                range(len(bands)),
                key=lambda i: abs(mid - (bands[i].offset + bands[i].image.height // 2)),
            )
        owner.setdefault(best_i, []).append(region)

    for band_i, band in enumerate(bands):
        top, bottom = band.offset, band.offset + band.image.height
        mine = owner.get(band_i) or []
        if not mine:
            continue

        # 밴드 경계가 영역을 반토막 내면 그 영역은 반쪽만 보인 채로 판독된다. 실측(2026-07-28):
        # 영역의 5~14%가 경계를 가로지르고, 하필 그중에 값을 하던 것들이 있다 — 001 p1_r069
        # (우대금리 조건, OCR 유사도 0.56→VLM 1.0 으로 교정된 영역)와 올원e p1_r014
        # ('① 0.1%p : 「NH올원e통장」…', ④+ 가 존재하는 이유인 원문자 교정 케이스)가 그렇다.
        # 그래서 컷을 옮기는 대신 **크롭을 넓혀** 내가 맡은 영역이 통째로 보이게 한다.
        # 컷을 옮기면 밀도 등량 분할이 깨지고 옆 밴드까지 연쇄로 흔들리지만, 크롭을 넓히는
        # 것은 그 밴드 안에서만 끝난다.
        need_top = min(top, min(r.bbox[1] for r in mine))
        need_bottom = max(bottom, max(r.bbox[3] for r in mine))
        if (need_top, need_bottom) != (top, bottom):
            expanded += 1
        crop = canvas_img.crop((0, max(0, need_top), canvas_img.width,
                                min(canvas_img.height, need_bottom)))
        crop_top = max(0, need_top)
        entries = [(r.region_id, " ".join(l.text for l in r.lines)) for r in mine]
        try:
            readings, missing = read_band_regions(crop, entries)
        except Exception as exc:
            page.notes.append(f"밴드 통합판독 실패(y={band.offset}, 이 구간 원값 유지): {exc}")
            continue

        by_id = {r.region_id: r for r in page.regions}
        for rid, (text, conf) in readings.items():
            region = by_id.get(rid)
            if region is None:
                continue
            ocr = " ".join(l.text for l in region.lines)
            # B안 유지 — OCR 정본은 안 건드리고 후보로만 붙인다. 판단은 STAGE_3 몫.
            region.vlm_reading = text
            region.vlm_reading_score = round(check_field_consistency(text, ocr), 3)
            region.vlm_reading_coverage = round(check_field_consistency(ocr, text), 3)
            # 위 두 점수는 토큰 겹침이라 순서를 못 본다 — 뒤가 잘린 판독이 정밀도 만점을
            # 받는다(실측 5건). 경계 기준으로 관계를 따로 판정해 후보에 딱지를 붙인다.
            rel = classify_reading(ocr, text)
            region.vlm_reading_relation = rel.kind
            if rel.is_truncated:
                truncated.append((region.region_id, rel))
            attached += 1
            if _n_squash(text) != _n_squash(ocr):
                corrected += 1

        for item in missing[:20]:
            text = str(item.get("text", "")).strip()
            if not text or len(_n_squash(text)) < 2 or _n_squash(text) in known:
                continue
            y = min(max(float(item.get("y_ratio", 0.0)), 0.0), 1.0)
            half = max(12, int(crop.height * 0.012))
            cy = crop_top + int(y * crop.height)   # 넓힌 크롭 기준으로 환산
            recovered.append(Line(
                text=text,
                bbox=[0, max(0, cy - half), page.canvas_w, min(page.canvas_h, cy + half)],
                confidence=item.get("confidence"),
                source="vlm_sweep",
            ))

    page.notes.append(
        f"밴드 통합판독: {len(bands)}회 호출로 영역 {attached}개 후보 부착"
        f"(OCR 과 다른 것 {corrected}개) + 누락 후보 {len(recovered)}건"
        f" / 영역 절단 방지로 크롭 확장 {expanded}개 밴드"
    )
    # 잘린 후보 경보 — 통합판독으로 옮길 때 빠뜨렸던 것을 복구(2026-07-29). 옛 경로
    # (_transcribe_regions_vlm)에는 '절단 의심' 표시가 있었는데 이 함수엔 없었다.
    # 정본을 안 덮으므로 파싱 결과가 틀어지지는 않지만, STAGE_3 가 잘린 쪽을 고르면
    # 내용이 사라진다 — 실측 002 p1_r018 은 경품 금액이 통째로 빠진 후보였다.
    if truncated:
        page.notes.append(
            "통독 후보 잘림 경보(정본 우선): "
            + ", ".join(f"{rid}({rel.lost_tail:.0%} 소실)" for rid, rel in truncated)
        )
    return recovered


def _n_squash(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _n_join(lines: list[Line]) -> str:
    return _n_squash("".join(l.text for l in lines))
