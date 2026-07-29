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


_ILLUSTRATIVE_SECTION_TYPES = {"장식예시"}


def _mark_illustrative(page: AdPage) -> None:
    """VLM 이 '장식예시'(앱 화면 예시·지폐 그림 등 심의 무관)로 판정한 섹션과 소속
    영역을 is_illustrative 로 태깅한다 (2a).

    삭제가 아니라 격리 — 심의 텍스트/필드 추출에서만 제외하고 IR·검수 화면엔
    보관해 감사·복원이 가능하게 한다(이전 orch 가 폴백 시 원본을 structure_regions
    로 남긴 것과 같은 철학). 판단 주체는 VLM(section_type)이며 규칙 하드코딩이 아니다.
    """
    if not page.sections:
        return
    region_by_id = {r.region_id: r for r in page.regions}
    secs = regs = 0
    for section in page.sections:
        if section.section_type not in _ILLUSTRATIVE_SECTION_TYPES:
            continue
        section.is_illustrative = True
        secs += 1
        for rid in section.region_ids:
            r = region_by_id.get(rid)
            if r is not None and not r.is_illustrative:
                r.is_illustrative = True
                regs += 1
    if secs:
        page.notes.append(
            f"예시/장식 격리(2a): {secs}개 섹션·{regs}개 영역을 심의 대상에서 제외(보관)"
        )


def _apply_vlm_judgments(page: AdPage, canvas_img: Image.Image | None) -> None:
    """VLM 이 판단 주체 (설계서 6.5): 영역 역할 판정 + 밴드 통합판독(교정+누락 회수).

    실패 시에만 규칙/regex 폴백을 유지하고, 그 사실을 notes 에 기록한다
    (조용한 실패 금지 원칙).
    """
    all_lines = [l for r in page.regions for l in r.lines] + page.unassigned_lines

    # 카드-분할(§D): 카드/예시 뭉치가 한 화면에 있으면 VLM 이 개수를 세고 각 영역을 카드에
    # 배정 → group_no 를 눈대중 대신 이 카드 배정으로 확정(003 카드 내용 섞임 방지).
    card_by_region: dict[str, int] | None = None
    if canvas_img is not None:
        from .cards import assign_cards_vlm

        try:
            cards = assign_cards_vlm(page, canvas_img, votes=SETTINGS.card_split_votes)
            if cards:
                card_by_region = cards
                for r in page.regions:
                    r.card_no = cards.get(r.region_id)
                ncards = len({c for c in cards.values() if c > 0})
                page.notes.append(f"카드-분할(§D): {ncards}개 카드로 그룹핑 (VLM 판정, 공통요소 card_no=0)")
        except Exception as exc:
            page.notes.append(f"카드-분할 실패(기존 group_no 유지): {exc}")

    try:
        page.sections = judge_region_roles(
            page.regions, canvas_img, page.canvas_h, card_by_region=card_by_region
        )
    except Exception as exc:
        page.notes.append(f"VLM 역할/섹션 판정 실패 → 규칙 폴백 유지(섹션 없음): {exc}")

    # 예시/장식(앱 화면 예시 등) 격리 태깅 (2a) — 판단 주체는 VLM(section_type)
    _mark_illustrative(page)

    # 미배정 라인의 섹션 귀속 (2단):
    # 1단(좌표) — 라인 중심이 섹션 bbox '내부'면 그 섹션에 붙인다. 근사 bbox 인
    #   vlm_sweep 라인은 제외 (y 추정이 이웃 섹션에 오귀속되는 실측, 올원).
    # 2단(VLM 내용) — 1단에서 남은, 어느 섹션 bbox 안에도 없는 라인은 VLM 에게
    #   내용상 어느 섹션의 연장인지 물어 판정하고, 좌표 정합 게이트로 교차검증해
    #   수용한다 (002 하단 이벤트 고지 3줄, 003 p2 상품유의 하단 4줄 실측).
    _absorb_unassigned_into_sections(page)
    if canvas_img is not None:
        _assign_orphans_via_vlm(page, canvas_img)

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


def _attach_line_to_section(line: Line, section: Section, region_by_id: dict[str, Region]) -> bool:
    """미배정 라인을 섹션의 최근접 영역에 붙이고, 영역·섹션 bbox 를 확장한다.

    좌표 흡수(_absorb)와 VLM 판정 귀속이 공유하는 실제 부착 로직.
    반환: 부착 성공 여부 (섹션에 bbox 있는 영역이 없으면 실패).
    """
    from .tiling import sort_reading_order

    if not line.bbox:
        return False
    regions = [
        region_by_id[rid] for rid in section.region_ids
        if rid in region_by_id and region_by_id[rid].bbox
    ]
    if not regions:
        return False
    cx = (line.bbox[0] + line.bbox[2]) / 2
    cy = (line.bbox[1] + line.bbox[3]) / 2
    target = min(
        regions,
        key=lambda r: abs((r.bbox[1] + r.bbox[3]) / 2 - cy)
        + abs((r.bbox[0] + r.bbox[2]) / 2 - cx),
    )
    target.lines.append(line)
    target.lines = sort_reading_order(target.lines)
    target.bbox = [
        min(target.bbox[0], line.bbox[0]), min(target.bbox[1], line.bbox[1]),
        max(target.bbox[2], line.bbox[2]), max(target.bbox[3], line.bbox[3]),
    ]
    if section.bbox:  # 섹션 bbox 도 확장 (미배정 라인이 섹션 범위를 넓힘)
        section.bbox = [
            min(section.bbox[0], line.bbox[0]), min(section.bbox[1], line.bbox[1]),
            max(section.bbox[2], line.bbox[2]), max(section.bbox[3], line.bbox[3]),
        ]
    return True


def _vertical_gap(box: list[int], sec: list[int]) -> int:
    """라인 box 와 섹션 sec 사이 수직 갭(px). 겹치면 0."""
    if box[1] > sec[3]:
        return box[1] - sec[3]
    if box[3] < sec[1]:
        return sec[1] - box[3]
    return 0


def _absorb_unassigned_into_sections(page: AdPage) -> None:
    """정밀 bbox 미배정 라인을 좌표상 감싸는 섹션의 최근접 영역에 귀속시킨다.

    - 대상: source 가 ocr/digital 인 라인만 (vlm_sweep 은 근사 밴드 bbox 라 제외)
    - 귀속 조건: 라인 중심점이 섹션 bbox 내부 — 겹치는 섹션이 여럿이면 가장
      작은(구체적인) 섹션. 어느 섹션에도 안 들어가면(섹션 사이 갭 등) 미배정 유지.
    """
    if not page.sections:
        return
    region_by_id = {r.region_id: r for r in page.regions}
    remaining: list[Line] = []
    absorbed = 0
    for line in page.unassigned_lines:
        if not line.bbox or line.source not in ("ocr", "digital"):
            remaining.append(line)
            continue
        cx = (line.bbox[0] + line.bbox[2]) / 2
        cy = (line.bbox[1] + line.bbox[3]) / 2
        candidates = [
            s for s in page.sections
            if s.bbox and s.bbox[0] <= cx <= s.bbox[2] and s.bbox[1] <= cy <= s.bbox[3]
        ]
        if not candidates:
            remaining.append(line)
            continue
        section = min(
            candidates,
            key=lambda s: (s.bbox[2] - s.bbox[0]) * (s.bbox[3] - s.bbox[1]),
        )
        if _attach_line_to_section(line, section, region_by_id):
            absorbed += 1
        else:
            remaining.append(line)
    if absorbed:
        page.unassigned_lines = remaining
        page.notes.append(f"미배정 라인 {absorbed}개를 좌표 기준 소속 섹션에 귀속")


def _assign_orphans_via_vlm(page: AdPage, canvas_img: Image.Image | None) -> None:
    """좌표 흡수 후에도 남은 미배정 라인의 섹션 소속을 VLM(내용)으로 판정.

    _absorb 는 라인 중심이 섹션 bbox '내부'일 때만 귀속한다. 어느 섹션 bbox
    안에도 안 들어가는 라인(섹션 사이 갭·경계 근접의 fine-print — 002 하단
    이벤트 고지 3줄, 003 p2 상품유의 하단 4줄 실측)은 여기서 처리한다.

    **역할판정 호출에 합치려다 되돌렸다(2026-07-28)** — 합치면 좌표 흡수를 아직 안 한
    시점이라 낱줄 전부(001 기준 58개)를 프롬프트에 실어야 하고, 그 탓에 역할판정이
    타임아웃 나 섹션이 통째로 날아갔다. 여기서 묻는 것은 흡수가 걷어내고 남은 소수다.

    판정 주체는 VLM(내용), 수용은 좌표 정합 게이트와 함께 결정한다:
    VLM 이 고른 섹션과 라인 사이 수직 갭이 캔버스 비례 임계(비연속 분할과
    동일 기준)를 넘으면 반려하고 미배정으로 남긴다 — 내용은 맞지만 화면상
    멀리 떨어진 별개 인스턴스를 억지로 붙여 섹션 bbox 가 비정상 팽창하는 것을
    막는다. 근사 bbox 인 vlm_sweep 라인은 제외(좌표 게이트 신뢰 불가).
    """
    if not page.sections:
        return
    idxs = [
        i for i, l in enumerate(page.unassigned_lines)
        if l.text.strip() and l.bbox and l.source in ("ocr", "digital")
    ]
    if not idxs:
        return
    orphans = [page.unassigned_lines[i] for i in idxs]

    from .vlm_judge import judge_orphan_sections

    try:
        assignments = judge_orphan_sections(orphans, page.sections, canvas_img, page.canvas_h)
    except Exception as exc:
        page.notes.append(f"VLM 미배정 섹션 판정 실패(미배정 유지): {exc}")
        return

    section_by_id = {s.section_id: s for s in page.sections}
    region_by_id = {r.region_id: r for r in page.regions}
    gap_limit = max(300, int(page.canvas_h * 0.15)) if page.canvas_h else None
    consumed: set[int] = set()
    applied: list[str] = []
    rejected = 0
    for k, line in enumerate(orphans):
        sid, _conf = assignments.get(k, ("none", None))
        section = section_by_id.get(sid) if sid and sid != "none" else None
        if section is None or not section.bbox:
            continue
        gap = _vertical_gap(line.bbox, section.bbox)
        if gap_limit is not None and gap > gap_limit:
            rejected += 1
            continue
        if _attach_line_to_section(line, section, region_by_id):
            consumed.add(idxs[k])
            applied.append(f"'{line.text[:24]}'→{sid}")
    if consumed:
        page.unassigned_lines = [
            l for i, l in enumerate(page.unassigned_lines) if i not in consumed
        ]
        page.notes.append(
            f"VLM 미배정 섹션 판정: {len(consumed)}개 내용 기준 귀속 ("
            + ", ".join(applied) + ")"
        )
    if rejected:
        page.notes.append(f"VLM 미배정 판정 {rejected}개 반려(좌표 정합 게이트 초과 → 미배정 유지)")


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
    """스윕 라인이 기존 OCR/디지털 라인의 다른 판독(중복)이면 크롭 재판독으로 정정.

    스윕 자체 dedup 은 정규화 완전일치만 잡으므로, OCR 이 원문자 번호를 숫자에
    합쳐 읽거나 소수점을 잃은 경우(올원 '10.1%p' vs 스윕 '① 0.1%p')는 못 걸러
    두 판본이 공존한다. 여기서 숫자·기호를 뺀 안정 핵심부로 같은 줄임을 탐지하고,
    그 위치를 고해상 재판독해(심판 — 규칙으로 우열을 정하지 않음) OCR 라인 텍스트를
    정정한 뒤 중복 스윕 라인을 제거한다. 좌표는 정밀한 OCR 쪽 유지(best-of-both).

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
                twin.text = reading
                page.notes.append(
                    f"스윕-OCR 중복 크롭 정정: {old!r} → {reading!r} (스윕판 {s.text[:30]!r} 확인)"
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


_ROLE_COLORS = {
    "유의사항": (220, 30, 30),
    "고지문구": (240, 140, 0),
    "제목": (0, 160, 60),
    "표": (150, 60, 200),
    "이미지": (60, 120, 220),
    "본문": (130, 130, 130),
    "각주": (0, 170, 170),
}


def _load_font(size: int):
    try:
        from PIL import ImageFont

        return ImageFont.truetype("malgun.ttf", size)  # Windows 한글 폰트
    except Exception:
        return None


def _save_preview(canvas: CanvasPage, page: AdPage, preview_dir: Path, doc_id: str) -> None:
    preview_dir.mkdir(parents=True, exist_ok=True)
    img = canvas.image.copy()
    draw = ImageDraw.Draw(img)
    for region in page.regions:
        if not region.bbox:
            continue
        color = _ROLE_COLORS.get(region.role, (130, 130, 130))
        draw.rectangle(region.bbox, outline=color, width=4)
        for line in region.lines:
            if line.bbox:
                draw.rectangle(line.bbox, outline=color, width=1)
    # 섹션 = 굵은 남색 박스 + 좌상단 라벨 (골드셋 비교의 기본 단위)
    font = _load_font(max(22, img.width // 45))
    for section in page.sections:
        if not section.bbox:
            continue
        x0, y0, x1, y1 = section.bbox
        pad = 8
        box = [max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)]
        draw.rectangle(box, outline=(20, 20, 120), width=6)
        label = f"{section.section_type}{section.section_no if section.section_no > 1 else ''}"
        if section.group_no:
            label += f" G{section.group_no}"
        if font:
            tb = draw.textbbox((box[0], max(0, box[1] - 36)), label, font=font)
            draw.rectangle(tb, fill=(20, 20, 120))
            draw.text((box[0], max(0, box[1] - 36)), label, fill=(255, 255, 255), font=font)
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
