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

from __future__ import annotations

from .ir import AdDocument, AdPage, Line, Region
from .tiling import sort_reading_order
from .truncation import OVERFLOWED


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


def region_text_box(region: Region) -> list[int] | None:
    """영역의 **정본 글자가 실제로 차지한** 사각형. 글자가 없으면 검출 bbox 로 폴백.

    검출 bbox 를 쓰지 않는 이유(2026-08-04 실측). 레이아웃 박스는 서로 겹치거나 한쪽이
    다른 쪽을 품는 일이 있다 — 001 p1 의 `r069`(bbox 상단 5963, 실제 글자 6047~)가
    `r065`(bbox 상단 5963, 글자 5965~)를 감싸는 형태여서, bbox 로 순서를 정하면
    우대조건 ③이 ①② 보다 먼저 나왔다. 5문서 중 4페이지에서 어긋났다.
    """
    boxes = [l.bbox for l in region.lines if l.bbox]
    if not boxes:
        return region.bbox
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _swappable(a: Region, b: Region) -> bool:
    """`b` 가 `a` 보다 먼저 읽혀야 하는 게 **확실한가**. 세 조건을 다 만족해야 한다.

    ① 같은 카드          — 카드 경계를 넘지 않는다. 좌우로 나란한 카드가 있는 문서
                            (003 EVENT1/EVENT2)는 y 가 비슷해 섞으면 두 카드 문장이
                            한 줄씩 번갈아 나온다.
    ② 좌우 구간이 겹친다 — 안 겹치면 **다른 칸**이라 순서를 건드리지 않는다.
    ③ `b` 가 `a` 보다 세로로 **완전히** 위 — 세로로 겹치면 같은 행이므로 좌우 순서를 지킨다.

    ③이 임계값을 없앤다. '몇 px 이상 위' 같은 숫자가 필요 없다.
    """
    if (a.card_no or 0) != (b.card_no or 0):
        return False
    ba, bb = region_text_box(a), region_text_box(b)
    if not ba or not bb:
        return False
    if min(ba[2], bb[2]) - max(ba[0], bb[0]) <= 0:   # ② 좌우 안 겹침 = 다른 칸
        return False
    return bb[3] <= ba[1]                            # ③ b 가 완전히 위


def repair_reading_order(regions: list[Region]) -> list[Region]:
    """레이아웃 엔진이 준 순서를 **최소한으로 고친다.** 다시 정렬하지 않는다.

    **왜 다시 정렬하지 않나** (2026-08-24, 이걸 한 번 해보고 되돌렸다). y 좌표로 전부
    다시 정렬하면 **2단 문서가 망가진다.** 실측 `2. 예금성상품(적립식).pdf` p1 —
    원본은 가운데 세로 점선으로 좌우가 갈린 2단이다:

        왼쪽  가입대상 가입금액 가입기간 예금종류 이자지급방식 기본금리 판매한도
        오른쪽 우대금리 + 우대이자율 표 + 각주 + 중도해지

        엔진 순서   왼쪽 6개 → 오른쪽 5개                    ← 사람이 읽는 순서다
        y 정렬      우대금리(y1480) → 가입대상(y1494) →
                    표(y1582) → 가입기간(y1684) → …          ← 칸을 번갈아 읽는다
                    '우대금리 4.8%p' 가 '가입대상' 설명처럼 붙는다

    y 가 14px 밖에 안 다르니 y 만 보면 어느 칸 소속인지 알 수가 없다. 그래서 **엔진
    순서를 정본으로 두고**, 확실히 거꾸로인 이웃끼리만 교환한다(`_swappable`).

    이게 잡는 것(실측 5문서): 한 문장이 두 줄로 이어지는데 순서가 뒤집힌 경우 —
    올원e p1 꼬리의 `'수 있는 권리가 있습니다.'` 가 `'금융소비자 보호에 관한 법률
    제19조제1항에 따른 설명을 받을'` 보다 먼저 실려 있었다. 5문서 합계 14건 → 8건,
    2단 문서(`2. 예금성상품`)는 교환 0건으로 손대지 않는다.

    이게 못 잡는 것: 로고 조각이 문서 중간에 있는 것(다른 칸이라 안 움직인다),
    2열 항목표의 항목명↔내용 짝(`대출대상 | 내용` — 좌우가 안 겹친다). 후자는 별개
    문제로 미뤘다.
    """
    out = list(regions)
    # 안정 정렬이라 같은 카드 안에서는 엔진 순서가 그대로 유지된다.
    out.sort(key=lambda r: r.card_no or 0)
    for _ in range(len(out)):
        moved = False
        for i in range(len(out) - 1):
            if _swappable(out[i], out[i + 1]):
                out[i], out[i + 1] = out[i + 1], out[i]
                moved = True
        if not moved:
            break
    return out


def build_page_view(page: AdPage) -> dict:
    """한 페이지의 lean 투영 — 영역 clean text 를 읽기순서로 평면 나열.

    bbox·신뢰도·출처는 빼고 region_id 는 남긴다(추출 후 bbox 재부착용).
    순서 규칙은 `repair_reading_order` 참조 — 파이프라인(`_finalize_reading_order`)과
    검수 화면(make_review)이 **같은 함수**를 쓴다. 여기서 한 번 더 부르는 건 무해하다
    (같은 입력에 같은 결과) — 파이프라인을 안 거친 페이지도 안전하게 하려는 것이다.
    """
    ordered = repair_reading_order(page.regions)
    regions: list[dict] = []
    for r in ordered:
        text = _region_text(r)
        if not text and not r.vlm_reading:
            continue
        item = {"region_id": r.region_id, "role": r.role, "text": text}
        # VLM 통독 후보(§6, B안): OCR 정본과 다를 때만 후보로 병존 노출.
        # STAGE_3 가 정밀도/커버리지를 보고 raw(text)와 후보 중 선택 (§1A).
        # 범람 후보(다른 영역 내용을 통째로 삼킨 것)는 아예 안 싣는다 — 실으면 STAGE_3 가
        # 그 값을 이 영역의 근거로 채택해 **엉뚱한 좌표를 가리키는 근거**가 만들어진다.
        # 판정 근거는 out/json 의 notes 와 vlm_reading_relation 에 남으므로 조용한 삭제가
        # 아니다 (pipeline._swallowed_regions 주석 참조).
        if (
            r.vlm_reading
            and r.vlm_reading.strip()
            and r.vlm_reading.strip() != text.strip()
            and r.vlm_reading_relation != OVERFLOWED
        ):
            item["vlm_reading"] = r.vlm_reading
            item["vlm_reading_score"] = r.vlm_reading_score
            item["vlm_reading_coverage"] = r.vlm_reading_coverage
            # 점수는 숫자라 LLM 이 해석해야 하고, 토큰 겹침이라 잘린 판독이 만점을
            # 받는다. 관계 라벨을 같이 실어 '이 후보는 뒤가 잘렸다'를 말로 알려준다.
            if r.vlm_reading_relation:
                item["vlm_reading_relation"] = r.vlm_reading_relation

        # 결함 수정(2026-08-12): 라인 단위 재판독 후보(sweep_dedupe/lowconf_reread)가
        # 지금까지 여기서 안 실려 STAGE_3 에 도달하지 못했다. 영역 후보(위)와 트리거가
        # 다르고(밴드 통합판독 vs 개별 라인 재판독) **밴드가 그 영역을 못 채택한 경우
        # (실측: "밴드 통합판독 일부 미채택" 003 p2, 22개 중 0개 채택) 영역 후보가 아예
        # 없을 수 있다 — 그럴 때 라인 후보가 유일한 교정 신호다. 영역 후보가 이미 있어도
        # 서로 다른 관측이므로 조용히 버리지 않고 같이 싣는다.
        line_cands = [
            {"line_text": ln.text, "vlm_reading": ln.vlm_reading}
            for ln in r.lines
            if ln.vlm_reading and ln.vlm_reading.strip() and ln.vlm_reading.strip() != ln.text.strip()
        ]
        if line_cands:
            item["line_candidates"] = line_cands
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
