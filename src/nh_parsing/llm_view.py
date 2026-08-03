from __future__ import annotations

"""LLM 전달용 lean 텍스트 투영 (§1A / §6).

최종 파싱 결과(AdDocument)에서 bbox·신뢰도·출처 같은 기계 신호를 빼고, 읽기순서로
정렬한 텍스트만 남긴다. 스키마-LLM 단계(§2)에 실제로 투입할 페이로드의 본문이자,
review.html 'LLM 전달 형태' 표시의 단일 출처.

- region_id 는 유지한다 → 추출 후 이 ID 로 bbox 를 되붙여 F-011/012 하이라이트에 연결(§1A).
  (HyundaiHS 는 좌표를 아예 버려 재부착도 안 하지만, 우리는 시인성 요구 때문에 ID 를 남긴다.)
- B안: region.text 는 항상 결정론적 OCR/디지털 정본(읽기순서·같은 행 조각 이어붙임).
  영역별 VLM 통독(§6)이 켜진 영역엔 vlm_reading(후보 텍스트)+점수를 함께 실어,
  STAGE_3(스키마 LLM)가 raw 정본과 후보 중 더 정확한 쪽을 고르게 한다(§1A raw+candidates).

**2026-08-03: 섹션(의미 묶음) 계층 제거.** 예전에는 `pages → sections → regions` 였다.
섹션은 VLM 산물이라 실행마다 흔들렸고(같은 입력 2회에 4문서 중 3문서 불일치) 후속
계약(NormalizedDocument v1: 문서→페이지→블록)에 담을 자리도 없었다. 이제 영역을
읽기순서(위→아래, 좌→우)로 평면 나열한다 — 순서 자체가 화면 흐름을 담으므로
STAGE_3 가 문맥을 잃지 않는다. 자세한 근거는 vlm_judge 모듈 상단 주석.
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


def build_page_view(page: AdPage) -> dict:
    """한 페이지의 lean 투영 — 영역 clean text 를 읽기순서로 평면 나열.

    bbox·신뢰도·출처는 빼고 region_id 는 남긴다(추출 후 bbox 재부착용).
    정렬은 위→아래, 좌→오른쪽 — 화면 흐름 자체가 문맥이므로 섹션 라벨 없이도
    STAGE_3 가 '이 유의사항이 어느 이벤트에 딸린 것인지'를 순서로 읽을 수 있다.
    """
    ordered = sorted(page.regions, key=lambda r: (r.bbox[1], r.bbox[0]) if r.bbox else (0, 0))
    regions: list[dict] = []
    for r in ordered:
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
        regions.append(item)

    view: dict = {"page_number": page.page_no, "regions": regions}
    if page.unassigned_lines:
        # 어느 영역에도 못 붙은 낱줄 — 근거를 영역 단위로 지목할 수 없을 뿐,
        # 텍스트는 STAGE_3 에 전달돼야 한다(안 그러면 '본 적 없는데 미발견' 사고).
        rows = [
            r for r in _rows_from_lines(sort_reading_order(page.unassigned_lines))
            if r.strip()
        ]
        if rows:
            view["unassigned"] = "\n".join(rows)
    return view


def build_doc_view(doc: AdDocument) -> dict:
    """문서 전체의 lean 투영 — 스키마-LLM 단계에 넣을 본문 페이로드."""
    return {
        "document": doc.source_file,
        "doc_id": doc.doc_id,
        "product_group": doc.product_group,
        "ad_type": doc.ad_type,
        "pages": [build_page_view(p) for p in doc.pages],
    }
