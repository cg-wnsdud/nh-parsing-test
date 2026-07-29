from __future__ import annotations

"""LLM 전달용 lean 텍스트 투영 (§1A / §6).

최종 파싱 결과(AdDocument)에서 bbox·신뢰도·출처 같은 기계 신호를 빼고, 읽기순서·
의미묶음(섹션)으로 정렬한 텍스트만 남긴다. 스키마-LLM 단계(§2)에 실제로 투입할
페이로드의 본문이자, review.html 'LLM 전달 형태' 표시의 단일 출처.

- region_id 는 유지한다 → 추출 후 이 ID 로 bbox 를 되붙여 F-011/012 하이라이트에 연결(§1A).
  (HyundaiHS 는 좌표를 아예 버려 재부착도 안 하지만, 우리는 시인성 요구 때문에 ID 를 남긴다.)
- 장식예시(2a) 섹션은 기본 제외 — 심의 대상이 아니므로 LLM 입력에서도 뺀다.
- B안: region.text 는 항상 결정론적 OCR/디지털 정본(읽기순서·같은 행 조각 이어붙임).
  영역별 VLM 통독(§6)이 켜진 영역엔 vlm_reading(후보 텍스트)+점수를 함께 실어,
  STAGE_3(스키마 LLM)가 raw 정본과 후보 중 더 정확한 쪽을 고르게 한다(§1A raw+candidates).
"""

from .ir import AdDocument, AdPage, Line, Region
from .tiling import sort_reading_order


def _rows_from_lines(lines: list[Line]) -> list[str]:
    """같은 시각적 행으로 검출된 박스 조각들을 공백으로 이어붙여 실제 줄 모양을 복원한다.

    OCR 은 한 줄을 여러 박스로 쪼갠다(같은 y, x 만 다름). 세로 50% 이상 겹치는 이웃은
    한 줄로 합친다. 통독 라인(줄바꿈 포함 단일 Line)은 조각이 아니므로 그대로 한 행.
    """
    rows: list[list[Line]] = []
    for line in lines:
        b = line.bbox
        if b and rows and rows[-1][-1].bbox:
            prev = rows[-1]
            r_top = min(x.bbox[1] for x in prev)
            r_bot = max(x.bbox[3] for x in prev)
            overlap = min(r_bot, b[3]) - max(r_top, b[1])
            height = min(b[3] - b[1], r_bot - r_top)
            if height > 0 and overlap / height >= 0.5:
                prev.append(line)
                continue
        rows.append([line])
    return [" ".join(x.text for x in row).strip() for row in rows]


def _region_text(region: Region) -> str:
    """영역의 라인들을 읽기순서로 정렬 후 같은 행 조각을 이어붙인 clean text."""
    ordered = sort_reading_order(region.lines) if len(region.lines) > 1 else region.lines
    rows = [r for r in _rows_from_lines(ordered) if r.strip()]
    return "\n".join(rows)


def _section_blocks(page: AdPage) -> list[tuple[object, list[Region]]]:
    """페이지를 (섹션, 영역목록) 순서로 — 묶음(카드)→섹션→영역(위→아래, 좌→우).

    섹션에 안 든 영역은 (None, leftovers) 로 맨 뒤에 붙인다. make_review 표시 규칙과 동일.
    """
    region_by_id = {r.region_id: r for r in page.regions}
    used: set[str] = set()
    blocks: list[tuple[object, list[Region]]] = []

    def sec_y(s) -> int:
        return s.bbox[1] if s.bbox else (1 << 30)

    for s in sorted(page.sections, key=lambda s: (s.group_no or 0, sec_y(s))):
        regs = [region_by_id[rid] for rid in s.region_ids if rid in region_by_id]
        regs.sort(key=lambda r: (r.bbox[1], r.bbox[0]) if r.bbox else (0, 0))
        used.update(r.region_id for r in regs)
        blocks.append((s, regs))

    leftovers = [r for r in page.regions if r.region_id not in used]
    if leftovers:
        leftovers.sort(key=lambda r: (r.bbox[1], r.bbox[0]) if r.bbox else (0, 0))
        blocks.append((None, leftovers))
    return blocks


def build_page_view(page: AdPage, *, include_illustrative: bool = True) -> dict:
    """한 페이지의 lean 투영. 섹션→영역 clean text (bbox/신뢰도/출처 제외, region_id 유지).

    장식예시(2a) 섹션은 **빼지 않고 표시만 한다**(`illustrative: true`). 예전에는 아예
    제외했는데, 격리는 '보관'이라는 취지와 달리 LLM 관점에서는 삭제와 같았다 — 실측
    (2026-07-28): 003 의 헤드라인+이벤트기간이 장식예시로 판정돼 통째로 빠졌고, 그 탓에
    STAGE_3 가 그 내용을 본 적조차 없는데 '필드 미발견'으로 집계됐다. 문서 5개에서
    33개 영역이 이렇게 사라졌다. 무엇이 예시인지는 표시해 주고 판단은 STAGE_3 에 맡긴다
    (판단 주체는 LLM 이라는 원칙과도 맞다).
    """
    sections: list[dict] = []
    for sec, regs in _section_blocks(page):
        if sec is not None and sec.is_illustrative and not include_illustrative:
            continue
        region_items = []
        for r in regs:
            text = _region_text(r)
            if not text and not r.vlm_reading:
                continue
            item = {"region_id": r.region_id, "role": r.role, "text": text}
            # VLM 통독 후보(§6, B안): OCR 정본과 다를 때만 후보로 병존 노출.
            # STAGE_3 가 정밀도/커버리지를 보고 raw(text)와 후보 중 선택 (§1A).
            if r.vlm_reading and r.vlm_reading.strip() and r.vlm_reading.strip() != text.strip():
                item["vlm_reading"] = r.vlm_reading
                item["vlm_reading_score"] = r.vlm_reading_score
                item["vlm_reading_coverage"] = r.vlm_reading_coverage
                # 점수는 숫자라 LLM 이 해석해야 하고, 토큰 겹침이라 잘린 판독이 만점을
                # 받는다. 관계 라벨을 같이 실어 '이 후보는 뒤가 잘렸다'를 말로 알려준다.
                if r.vlm_reading_relation:
                    item["vlm_reading_relation"] = r.vlm_reading_relation
            region_items.append(item)
        if not region_items:
            continue
        if sec is None:
            sections.append({
                "section_id": None, "section_type": None, "section_no": None,
                "group_no": None, "regions": region_items,
            })
        else:
            item = {
                "section_id": sec.section_id, "section_type": sec.section_type,
                "section_no": sec.section_no, "group_no": sec.group_no,
                "regions": region_items,
            }
            if sec.is_illustrative:
                item["illustrative"] = True  # 심의 대상이 아닐 수 있음 — 삭제가 아니라 표시
            sections.append(item)

    view: dict = {"page_number": page.page_no, "sections": sections}
    if page.unassigned_lines:
        rows = [
            r for r in _rows_from_lines(sort_reading_order(page.unassigned_lines))
            if r.strip()
        ]
        if rows:
            view["unassigned"] = "\n".join(rows)
    return view


def build_doc_view(doc: AdDocument, *, include_illustrative: bool = True) -> dict:
    """문서 전체의 lean 투영 — 스키마-LLM 단계에 넣을 본문 페이로드."""
    return {
        "document": doc.source_file,
        "doc_id": doc.doc_id,
        "product_group": doc.product_group,
        "ad_type": doc.ad_type,
        "pages": [
            build_page_view(p, include_illustrative=include_illustrative)
            for p in doc.pages
        ],
    }
