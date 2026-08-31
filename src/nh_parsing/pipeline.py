"""파일 → AdDocument 오케스트레이션 — 설계서 3절 Track B.

라우팅(설계서 4.1):
  PNG/JPG  → OCR 트랙 직행
  PDF      → 페이지 단위 triage → digital / ocr / hybrid
  HWP/HWPX → 사내 파서 디지털 추출
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from .canvas import CanvasPage, load_image_canvas, render_pdf_page, rgb_on_white
from .config import SETTINGS
from .gemma_client import classify
from .hwp_ingest import ingest_hwp
from .ir import AdDocument, AdPage, Line, RecoveryCandidate, Region
from .paddlex_client import LayoutBlock, request_layout_parsing
from .regions import build_regions
from .tiling import dedupe_lines, make_tiles, restore_coords

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

    from .bands import ink_coverage
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

        if verdict.verdict in ("structured", "hybrid"):
            # structured 도 레이아웃 검출(StructureV3)을 부른다. 예전에는 "디지털 텍스트가
            # 이미 있으니 무거운 모델을 또 부를 것 없다"고 건너뛰었는데, 실측해 보니 그
            # 절약은 **StructureV3 호출 하나(단일 타일 3.67초, GPU)**뿐이었다 — VLM(카드
            # 분할·역할 판정·밴드 통독·분류)은 _apply_vlm_judgments 가 route 를 안 가리고
            # 항상 부르므로 이미 전부 지불 중이다. 그 3.67초를 아끼는 대가로 가로 인식을
            # 통째로 잃고 있었다: 블록이 없으면 _assemble_page 가 _pseudo_regions 폴백을
            # 타는데, 그건 세로 간격만 보므로 좌우 2단 페이지가 한 덩어리로 뭉친다
            # (실측 2026-08-14, `6. 대출성상품.pdf` p1: 원금및이자상환방법부터 신청채널까지
            # 라벨 5줄이 한 영역 27줄로 뭉쳤다).
            #
            # 그래서 hybrid 가 이미 하던 조합(구조는 StructureV3, 값은 디지털 텍스트가 정본)
            # 을 structured 에도 그대로 쓴다. 3갈래 판정은 진단·기록용으로 page.triage 에
            # 남고, 여기서는 "OCR 을 부르느냐 마느냐"의 분기만 없앤다. route 를 "hybrid" 로
            # 통일하는 이유: 실제로 두 출처를 병합하므로 "digital"(디지털 텍스트만 썼다)은
            # 이제 사실이 아니다. structured 였다는 사실은 page.triage 가 그대로 들고 있다.
            digital = extract_digital_lines(pdf_page, canvas.px_per_pt)
            if verdict.verdict == "structured":
                # 커버리지는 더 이상 라우팅을 가르지 않는다(어차피 OCR 을 부른다). 다만
                # "텍스트 레이어가 화면의 글자를 얼마나 설명하는가"는 산출물 신뢰도를 읽는
                # 재료라 계속 재서 근거에 남긴다 — H(4efb092)가 찾아낸 진단 가치다.
                cov = ink_coverage(canvas.image, [l.bbox for l in digital if l.bbox])
                note = f"글자 획 픽셀 중 텍스트 레이어가 덮은 비율 {cov:.1%}"
                if cov < SETTINGS.min_ink_coverage:
                    note += (
                        f" (< {SETTINGS.min_ink_coverage:.0%}) — 글자를 도형·그림으로 그린 "
                        f"페이지로, 디지털 텍스트만으로는 화면을 설명하지 못한다"
                    )
                verdict.reasons.append(note)
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
                # 표 셀 좌표도 블록 bbox 와 같은 만큼 내려야 한다 — 안 하면 셀이 타일
                # 좌표에 남아 페이지 위쪽 엉뚱한 자리를 가리킨다.
                table=_shift_table(block.table, tile.y_offset),
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



def _shift_table(table: dict | None, y_offset: int) -> dict | None:
    """표 격자의 셀 좌표를 타일 오프셋만큼 내린다. 좌표가 없는 셀은 그대로 둔다."""
    if not table or not y_offset:
        return table
    out = dict(table)
    out["cells"] = [
        {**c, "bbox": ([c["bbox"][0], c["bbox"][1] + y_offset,
                        c["bbox"][2], c["bbox"][3] + y_offset] if c.get("bbox") else None)}
        for c in table.get("cells") or []
    ]
    return out


def _apply_vlm_judgments(page: AdPage, canvas_img: Image.Image | None) -> None:
    """좌표 경계를 유지한 VLM 보강 단계.

    이 브랜치의 기본 경로는 모든 StructureV3 텍스트 영역을 독립 Reader로 읽는다.
    따라서 이전의 ``밴드 이미지 → 여러 영역별 후보``와 역할 9종 VLM 판정은 정본
    판독 흐름에서 제외한다. 페이지 전체 sweep만 StructureV3가 만들지 못한 문구를 찾는
    별도 ``recovery_candidates`` 관측으로 남긴다.
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

    # 미배정 라인 귀속 — 좌표만 보는 결정론 1단계로 축소(예전 2단계 중 VLM 단 제거).
    _absorb_unassigned_into_regions(page)

    # 페이지 sweep은 Region Reader의 대체물이 아니다. Reader는 StructureV3가 이미 만든
    # 영역만 읽을 수 있으므로, StructureV3/OCR이 아예 놓친 장식·벡터 텍스트를 찾을 때만
    # 한 번 호출한다. 결과는 정본 Line이나 미배정 Line에 섞지 않고 별도 후보로 남긴다.
    if canvas_img is not None:
        from .vlm_direct import sweep_missing_lines

        try:
            discovered, notes = sweep_missing_lines(
                all_lines, canvas_img, page.canvas_w, page.canvas_h, banded=False,
            )
            page.notes.extend(f"페이지 누락문구 탐색: {n}" for n in notes)
            page.recovery_candidates.extend(
                RecoveryCandidate(text=line.text, bbox=line.bbox, confidence=line.confidence)
                for line in discovered
            )
            if discovered:
                page.notes.append(f"페이지 누락문구 후보 {len(discovered)}개 (정본 미편입)")
        except Exception as exc:
            page.notes.append(f"페이지 누락문구 탐색 실패(정본 유지): {exc}")

    _note_layout_gaps(page)

    # 필드는 STAGE_3(스키마 기반) 단일 창구다 — 파싱 단계에서는 뽑지 않는다.
    # 읽기 순서 국소 재정렬: 전역 정렬이 긴 페이지에서 시각적 줄을 조각내는 문제
    # (001 단어 섞임 실측)를 영역 범위 재정렬로 교정한다.
    _finalize_reading_order(page)
    # Reader → Judge 교차검증은 정본 줄 순서가 확정된 뒤 수행한다. 기본 scope=all에서는
    # StructureV3가 만든 모든 텍스트/표 영역이 한 번씩 독립 crop으로 읽힌다.
    # shadow에서는 Region.lines/템플릿 입력을 절대 바꾸지 않는다.
    if SETTINGS.region_reading_mode == "shadow":
        if canvas_img is None:
            page.notes.append("영역별 Reader/Judge shadow: 캔버스 없음 — 건너뜀")
        else:
            try:
                from .reading_adjudication import adjudicate_regions_shadow

                stats = adjudicate_regions_shadow(page.regions, canvas_img)
                page.notes.append(
                    "영역별 Reader/Judge shadow: "
                    f"선별 {stats['eligible']} / 요청 {stats['requested']} / "
                    f"일치 {stats['agreed']} / 정본선택 {stats['judge_selected']} / "
                    f"불확실 {stats['uncertain']} / Reader실패 {stats['reader_failed']} / "
                    f"Judge실패 {stats['judge_failed']}"
                )
            except Exception as exc:
                page.notes.append(f"영역별 Reader/Judge shadow 단계 실패(정본 유지): {exc}")
    elif SETTINGS.region_reading_mode not in {"", "off"}:
        page.notes.append(
            f"알 수 없는 REGION_READING_MODE={SETTINGS.region_reading_mode!r} — "
            "영역별 Reader/Judge를 실행하지 않음"
        )


_OVERFLOW_MIN_LEN_RATIO = 3.0   # 후보가 자기 정본보다 이 배수 이상 길 때만 의심
_OVERFLOW_MIN_SPAN_RATIO = 3.0  # 삼킨 영역들이 자기 bbox 높이의 이 배수 이상에 걸칠 때만
_OVERFLOW_MIN_CHARS = 8         # 이보다 짧은 문구는 우연히 포함될 수 있어 삼킴으로 안 센다


def _swallowed_regions(region: Region, page: AdPage) -> list[str]:
    """이 영역의 통독 후보가 **다른 영역의 정본을 통째로 삼켰는지** 본다.

    **왜 생기나.** 밴드 통합판독은 밴드 이미지 1장 + 영역 목록 N개를 한 번에 보내
    "영역마다 하나씩 나눠 채워라"고 시킨다. 그런데 VLM 이 이 분배를 무시하고 **밴드
    전체 판독문을 목록의 첫 항목에 통째로** 넣는 일이 있다. 실측(2026-08-13, 89문서):
    범람 13건 중 **12건이 자기 밴드 요청목록의 첫 항목**이었다. `12. 카드상품` p1_r000
    은 121x113px 짜리 SNS 게시자 표시(자기 3줄 20자)인데 후보에 페이지 전체 344자가
    들어와 다른 영역 6개를 삼켰다.

    **왜 버리지 않고 딱지만 붙이나.** 이 저장소의 원칙은 정본을 안 덮고 후보로만 붙이는
    것이고(ir.Line.vlm_reading 주석), 조용히 버리지 않는 것이다. 진짜 문제는 후보의
    존재가 아니라 **`expanded`(정본보다 많이 읽음 — 회수 가능)로 분류돼 검수 화면에
    초록색으로 뜨는 것**이다. 오염인데 이득으로 보인다. 딱지만 바꾸면 오탐이 나도
    정본은 그대로고 사람이 눈으로 되돌릴 수 있다.

    **왜 길이와 기하를 함께 보나.** 둘 중 하나만으로는 못 가른다(실측 1,547개 후보):
      · 길이만(3배 이상): 10건 — 자기 6자를 32자로 제대로 회수한 정상 건이 섞였다
      · 기하만(3배 이상): 45건 — 자기 21자 → 후보 21자로 **길이가 그대로인데** 멀리
        있는 짧은 문구를 우연히 포함한 건이 대량으로 걸렸다
      · 둘 다: **7건(0.45%)** — 위 오탐이 모두 빠지고 진짜 범람만 남았다
    """
    cand = _n_squash(region.vlm_reading or "")
    if not (cand and region.bbox):
        return []
    mine = _n_squash(" ".join(l.text for l in region.lines))
    if len(cand) < len(mine) * _OVERFLOW_MIN_LEN_RATIO:
        return []
    swallowed, ys = [], [region.bbox[1], region.bbox[3]]
    for other in page.regions:
        if other is region or not other.bbox:
            continue
        text = _n_squash(" ".join(l.text for l in other.lines))
        if len(text) >= _OVERFLOW_MIN_CHARS and text in cand:
            swallowed.append(other.region_id)
            ys += [other.bbox[1], other.bbox[3]]
    if not swallowed:
        return []
    height = max(1, region.bbox[3] - region.bbox[1])
    if (max(ys) - min(ys)) / height < _OVERFLOW_MIN_SPAN_RATIO:
        return []   # 바로 옆 영역 한 줄이 섞인 정도 — 물리적으로 가능한 범위다
    return swallowed


def _score_reading_candidates(page: AdPage) -> None:
    """확정된 정본과 통독 후보를 대조해 점수·관계 딱지를 매긴다.

    **왜 밴드 판독 시점이 아닌가.** 예전에는 _merged_band_read 안에서 후보를 붙이는
    즉시 매겼는데, 그 시점의 `region.text` 는 아직 정본이 아니다. 밴드 판독 이후에도
    정본을 바꾸는 단계가 둘 더 있다:

      1. 유령 중복 라인 삭제 — vlm_direct.py:413 (reread_low_confidence_lines 안).
         같은 행을 겹쳐 검출한 얇은 조각을 지운다. 라인이 빠지면 정본 문자열이 바뀐다.
      2. 읽기순서 국소 재정렬 — _finalize_reading_order. 표 형태 영역의 `라벨|값` 이
         전역 정렬에서 뒤집혀 있던 것을 바로잡는다.

    2번이 딱지를 실제로 뒤집었다 (실측 2026-08-06, 5문서 177건 중 7건):

        [밴드 판독 시점 — 라인 순서 뒤집힘]
          정본 '12 개월 가입기간' → 후보 '12 개월' 이 앞(위치 0)에 걸린다
          → 뒤에서 50% 사라짐 → tail_cut  "뒷부분 잘림(50% 소실)"    ← 틀린 딱지
        [최종 정렬 후 — 화면에 보이는 순서]
          정본 '가입기간 12 개월' → 후보 '12 개월' 이 위치 4
          → 앞에서 50% 사라짐 → head_drop "앞 항목명 생략 — 값은 온전"  ← 맞는 답

    tail_cut→head_drop 2 · diverged→head_drop 1 · diverged→same 4. 거짓 '잘림 경보'가
    "확인 필요" 목록 맨 위로 올라와(위험순 정렬) 검수자가 먼저 보게 되던 자리다.

    점수(score/coverage)는 토큰 집합이라 순서에 무관하지만, 1번(라인 삭제)에는 영향을
    받으므로 같이 여기서 계산한다 — 정본 판정 근거를 한 곳에 모은다.
    """
    from .field_judge import check_field_consistency
    from .truncation import OVERFLOWED, Relation, classify_reading

    truncated: list[tuple[str, Relation]] = []
    overflowed: list[str] = []
    for region in page.regions:
        cand = region.vlm_reading
        if not cand:
            continue
        ocr = " ".join(l.text for l in region.lines)
        region.vlm_reading_score = round(check_field_consistency(cand, ocr), 3)
        region.vlm_reading_coverage = round(check_field_consistency(ocr, cand), 3)
        # 위 두 점수는 토큰 겹침이라 순서를 못 본다 — 뒤가 잘린 판독이 정밀도 만점을
        # 받는다(실측 5건). 경계 기준으로 관계를 따로 판정해 후보에 딱지를 붙인다.
        swallowed = _swallowed_regions(region, page)
        if swallowed:
            region.vlm_reading_relation = OVERFLOWED
            overflowed.append(f"{region.region_id}←{','.join(swallowed)}")
            continue
        rel = classify_reading(ocr, cand)
        region.vlm_reading_relation = rel.kind
        if rel.is_truncated:
            truncated.append((region.region_id, rel))

    if overflowed:
        page.notes.append(
            "통독 후보 범람 경보(정본 우선, 근거 채택 금지): " + " / ".join(overflowed)
        )

    # 잘린 후보 경보 — 통합판독으로 옮길 때 빠뜨렸던 것을 복구(2026-07-29). 옛 경로
    # (_transcribe_regions_vlm)에는 '절단 의심' 표시가 있었는데 그 함수엔 없었다.
    # 정본을 안 덮으므로 파싱 결과가 틀어지지는 않지만, STAGE_3 가 잘린 쪽을 고르면
    # 내용이 사라진다 — 실측 002 p1_r018 은 경품 금액이 통째로 빠진 후보였다.
    if truncated:
        page.notes.append(
            "통독 후보 잘림 경보(정본 우선): "
            + ", ".join(f"{rid}({rel.lost_tail:.0%} 소실)" for rid, rel in truncated)
        )


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
    """읽기 순서를 확정한다 — **영역끼리의 순서**와 각 영역 **안 라인의 순서** 둘 다.

    영역 안 라인. build_regions 는 전역 정렬된 라인 목록을 영역에 분배하는데, 전역
    sort_reading_order 는 각 라인을 '직전 행'하고만 비교하므로 세로로 긴
    페이지에서는 같은 시각적 줄의 단어들 사이에 다른 위치의 줄이 끼어들어
    행 묶기가 깨진다(단어가 top 좌표순으로만 나열 — 001 실측). 영역 하나로
    범위를 좁혀 다시 정렬하면 끼어드는 무관한 줄이 없어 올바른 순서가 된다.

    영역끼리의 순서 (2026-08-24 추가). `page.regions` 는 그동안 PaddleX 의
    `parsing_res_list` 순서 그대로였다. 그 순서는 **문장을 거꾸로 놓는 데가 있다** —
    실측(`[예금성상품-적금] 올원e적금.png` p1, 원본 이미지 대조 완료):

        원본:  '금융소비자 보호에 관한 법률 제19조제1항에 따른 설명을 받을'  y=5546
               '수 있는 권리가 있습니다.'                                    y=5611
        기존:  r044 '수 있는 권리가 있습니다.'      ← 뒷줄이 먼저 실렸다
               r045 '금융소비자 보호에 관한 법률…'
        → 이어 읽으면 '수 있는 권리가 있습니다. 금융소비자 보호에…' 가 된다

    **다시 정렬하지 않고 최소 교정만 한다** — y 로 전부 다시 정렬하면 2단 문서가
    망가진다(이유와 실측은 `llm_view.repair_reading_order` 주석에 있다).

    **왜 여기서 하나.** 순서를 산출물 쪽(`ad_export`)에서 바꾸면 `line_ref` 가
    `ad_template.collect_lines` 의 것과 어긋난다 — 둘 다 `enumerate` 로 번호를 매기므로
    라벨이 **다른 줄에 조용히 붙는다**. 파싱 단계에서 한 번 고쳐 두면 하류 전부가
    같은 순서를 본다(검수 화면·추출층도 같은 함수를 쓴다).

    카드 경계를 지키므로 **카드 배정 뒤에** 불려야 한다(`_apply_vlm_judgments` 안,
    `assign_cards_vlm` 다음). 지금 호출 위치가 그 조건을 만족한다.
    """
    from .llm_view import repair_reading_order
    from .tiling import sort_reading_order

    for region in page.regions:
        if len(region.lines) > 1:
            region.lines = sort_reading_order(region.lines)
    page.regions = repair_reading_order(page.regions)


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


def _cluster_by_column(regions: list[Region], gutter_px: int) -> list[list[Region]]:
    """가로 구간이 맞닿거나 겹치는 영역끼리 묶는다(전이적) — gutter_px 넘게 떨어지면 분리.

    밴드 통합판독(_merged_band_read)이 한 밴드에 좌우로 분리된 여러 컬럼(카드 비교,
    2단 표)의 영역을 함께 소유할 때, 통독 크롭을 "이 그룹의 가로 구간"으로만 좁히기
    위한 그룹 나누기다. bbox 를 x0 오름차순으로 처리하면서 그때까지의 모든 그룹과
    겹치는지 검사하므로, 전폭에 걸친 요소(배너 등)가 두 그룹을 이어 붙이는 경우도
    한 그룹으로 자연스럽게 남는다(자기 자신이 잘리지 않는다) — 별도의 재병합 패스가
    필요 없다(x0 정렬 순서상 '이어붙이는 요소'는 항상 자신이 잇는 대상보다 먼저 처리됨).
    """
    ranges: list[list[int]] = []
    groups: list[list[Region]] = []
    for r in sorted(regions, key=lambda r: r.bbox[0]):
        x0, x1 = r.bbox[0], r.bbox[2]
        for g, rng in zip(groups, ranges):
            if x0 <= rng[1] + gutter_px and x1 >= rng[0] - gutter_px:
                g.append(r)
                rng[0] = min(rng[0], x0)
                rng[1] = max(rng[1], x1)
                break
        else:
            groups.append([r])
            ranges.append([x0, x1])
    return groups


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
        # 스윕이 기존 줄을 뒤에 포함하면서 훨씬 더 길면(잔여 4자 이상), 앞쪽 잔여는
        # 기존 줄 밖의 내용(디지털/OCR 이 못 본 로고·장식 문구 등)일 수 있다. 실측
        # (2026-08-13, `1. 카드상품.pdf`): 스윕 '올바른 NEW HAVE 주요 서비스' 가 기존
        # 줄 '주요서비스' 를 뒤쪽에 포함한다는 이유로 통째로 버려져 'NEW HAVE'(카드
        # 브랜드명 자체)가 전 출력에서 사라졌다. 쌍둥이 탐지(포함관계)는 그대로 두되,
        # 스윕이 진짜 더 긴 경우엔 버리지 않고 unassigned 로 흘려보낸다 — '주요서비스'
        # 중복 표기가 잠깐 남는 게 'NEW HAVE' 영구 유실보다 낫다. 반대 방향(스윕이
        # 기존 줄의 부분집합 — 스윕이 덜 읽은 진짜 중복)은 기존대로 재판독해 흡수한다.
        twin_core = _stable_core(twin.text)
        if twin_core in s_core and len(s_core) - len(twin_core) >= 4:
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


def _ocr_contained_in_digital(ocr_bbox: list[int], digital_bbox: list[int]) -> float:
    """OCR 박스 면적 중 디지털 박스와 겹치는 비율 (OCR 자신의 면적 기준).

    대칭 IoU 는 두 박스 크기가 비슷할 때만 통한다. 실측(2026-08-13, hybrid 89문서):
    표 한 셀의 라벨+값을 디지털은 "가입대상개인" 하나로 뭉쳐 읽고 OCR 은 "가입대상"
    "개인" 둘로 나눠 읽으면, 디지털 박스가 OCR 박스 둘 다를 포함하는데도 IoU 는
    각각 0.48·0.24 로 어느 쪽도 0.5(_iou 기준)를 못 넘어 **둘 다 살아남아 중복**된다
    (Fix A/B 가 이 셀 내용을 처음으로 이 영역에 제대로 배정하면서 드러났다 — 예전엔
    아예 다른 영역으로 잘못 갔었다). 포함 비율로 재면 0.69·0.58 로 분명하다.

    실측 165건(크기비 1.0~69.1배) 전부 확인 결과 오탐 없음 — 가장 극단적인 사례도
    OCR 이 "※" 기호 하나만 읽고 디지털이 그 기호로 시작하는 문장 전체를 읽은
    진짜 중복이었다(크기비 69배). 그래서 크기비 상한을 따로 두지 않는다.
    """
    ix = max(0, min(ocr_bbox[2], digital_bbox[2]) - max(ocr_bbox[0], digital_bbox[0]))
    iy = max(0, min(ocr_bbox[3], digital_bbox[3]) - max(ocr_bbox[1], digital_bbox[1]))
    area = max(1, (ocr_bbox[2] - ocr_bbox[0]) * (ocr_bbox[3] - ocr_bbox[1]))
    return (ix * iy) / area


def _merge_digital_ocr(digital: list[Line], ocr: list[Line]) -> list[Line]:
    from .tiling import _iou, sort_reading_order

    kept = list(digital)
    for line in ocr:
        overlapped = any(
            d.bbox and line.bbox and (
                _iou(d.bbox, line.bbox) >= 0.5
                or _ocr_contained_in_digital(line.bbox, d.bbox) >= 0.5
            )
            for d in digital
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
        # 지표가 ⑥-4 를 보고 있어서 STAGE_3 개선이 지표에 안 잡혔다). 그래서
        # AdPage.extracted_fields 필드 자체를 2026-08-06 제거했다 — ir.py 주석 참조.
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
    doc.product_name_shown = result.product_name_shown
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
    from .vlm_direct import read_band_regions

    bands = content_bands(canvas_img, span=SETTINGS.tile_max_height_px,
                          max_span=SETTINGS.tile_max_height_px)
    known = _n_join(all_lines)
    recovered: list[Line] = []
    corrected = attached = expanded = calls = split_bands = 0
    # 컬럼 그룹을 가를 최소 가로 간격 — 이보다 좁으면 같은 그룹(같은 크롭)으로 묶는다.
    gutter_px = max(40, int(canvas_img.width * 0.02))

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

        # 좌우로 뚜렷이 떨어진 컬럼(카드 비교, 2단 표)이 한 밴드에 같이 걸리면, 전폭
        # 크롭에 옆 컬럼 내용까지 보여서 재판독이 그 내용을 다시 끌어들일 수 있다
        # (실측 2026-08-13, 올원 p1_r006: 옆 칸 내용이 '기존 판독' 힌트에도 이미 섞여
        # 있어 재판독이 오염을 고치기는커녕 재확인/강화했다). 컬럼 그룹별로 나눠 그
        # 그룹의 가로 구간으로만 크롭을 좁힌다 — 전폭에 걸친 요소가 있으면 그 요소가
        # 자연히 그룹들을 이어 붙이므로(cluster_by_column 주석 참조) 단일 컬럼 페이지의
        # 기존 동작(밴드당 1회 호출, 전폭 크롭)은 그대로 유지된다.
        groups = _cluster_by_column(mine, gutter_px)
        if len(groups) > 1:
            split_bands += 1

        for group in groups:
            need_top = min(top, min(r.bbox[1] for r in group))
            need_bottom = max(bottom, max(r.bbox[3] for r in group))
            if (need_top, need_bottom) != (top, bottom):
                expanded += 1
            gx0 = min(r.bbox[0] for r in group)
            gx1 = max(r.bbox[2] for r in group)
            pad = max(20, int((gx1 - gx0) * 0.03))
            crop_x0 = max(0, gx0 - pad) if len(groups) > 1 else 0
            crop_x1 = min(canvas_img.width, gx1 + pad) if len(groups) > 1 else canvas_img.width
            crop = canvas_img.crop((crop_x0, max(0, need_top), crop_x1,
                                    min(canvas_img.height, need_bottom)))
            crop_top = max(0, need_top)
            entries = [(r.region_id, " ".join(l.text for l in r.lines)) for r in group]
            calls += 1
            try:
                readings, missing, dropped = read_band_regions(crop, entries)
            except Exception as exc:
                page.notes.append(f"밴드 통합판독 실패(y={band.offset}, x=[{crop_x0},{crop_x1}], 이 구간 원값 유지): {exc}")
                continue
            # 예외 없이 성공하면서 아무것도 안 돌려주는 경우가 있다 — 실측(2026-08-06)
            # 003 p2 가 영역 22개 중 0개 부착으로 나왔고(직전 실행은 22/22) 원인이
            # 기록되지 않아 못 가렸다. 버리는 규칙은 그대로 두고 이유만 남긴다.
            if dropped:
                page.notes.append(
                    f"밴드 통합판독 일부 미채택(y={band.offset}, 요청 {len(entries)}개 중 "
                    f"채택 {len(readings)}개): "
                    + ", ".join(f"{k} {v}" for k, v in sorted(dropped.items()))
                )

            by_id = {r.region_id: r for r in page.regions}
            for rid, (text, conf) in readings.items():
                region = by_id.get(rid)
                if region is None:
                    continue
                ocr = " ".join(l.text for l in region.lines)
                # B안 유지 — OCR 정본은 안 건드리고 후보로만 붙인다. 판단은 STAGE_3 몫.
                region.vlm_reading = text
                # 점수·관계 딱지는 여기서 매기지 않는다 → _score_reading_candidates 참조.
                # 이 시점의 정본은 아직 확정이 아니다(라인 순서 미정렬 + 유령 라인 미제거).
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
                    # 컬럼 그룹으로 좁힌 크롭이면 그 가로 구간만 준다 — 페이지 전폭으로
                    # 돌려주면 이 회수 라인도 옆 컬럼까지 걸치는 결함②를 재현하게 된다.
                    bbox=[crop_x0, max(0, cy - half), crop_x1, min(page.canvas_h, cy + half)],
                    confidence=item.get("confidence"),
                    source="vlm_sweep",
                ))

    page.notes.append(
        f"밴드 통합판독: {calls}회 호출({len(bands)}개 밴드, 컬럼분리 {split_bands}개)로 "
        f"영역 {attached}개 후보 부착(OCR 과 다른 것 {corrected}개) + 누락 후보 {len(recovered)}건"
        f" / 영역 절단 방지로 크롭 확장 {expanded}회"
    )
    return recovered


def _n_squash(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _n_join(lines: list[Line]) -> str:
    return _n_squash("".join(l.text for l in lines))
