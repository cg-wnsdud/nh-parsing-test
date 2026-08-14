# -*- coding: utf-8 -*-
"""밴드 통독 후보가 다른 영역 내용을 통째로 삼킨 경우 (pipeline._swallowed_regions).

실측 근거(2026-08-13, 89문서): 밴드 통합판독은 밴드 이미지 1장 + 영역 목록 N개를
한 번에 보내는데, VLM 이 분배를 무시하고 **밴드 전체 판독문을 목록 첫 항목에 통째로**
넣는 일이 있다. 범람 13건 중 **12건이 자기 밴드 요청목록의 첫 항목**이었다.

`12. 카드상품` p1_r000 은 121x113px 짜리 SNS 게시자 표시(자기 3줄 20자)인데 후보에
페이지 전체 344자가 들어와 다른 영역 6개를 삼켰다. 그런데 겉보기가 `expanded`
(정본보다 많이 읽음)와 같아 검수 화면에 **초록색 '회수 가능'**으로 떴다.

모델 호출 없음.
"""

from __future__ import annotations

from nh_parsing.ir import AdPage, Line, Region
from nh_parsing.llm_view import build_page_view
from nh_parsing.pipeline import _score_reading_candidates, _swallowed_regions
from nh_parsing.truncation import EXPANDED, OVERFLOWED


def _region(rid: str, box: list[int], text: str, cand: str = "") -> Region:
    return Region(
        region_id=rid, bbox=box, role="본문",
        lines=[Line(text=text, bbox=box, source="ocr")],
        vlm_reading=cand or None,
    )


def _page(regions: list[Region]) -> AdPage:
    return AdPage(page_no=1, parse_route="ocr", canvas_w=520, canvas_h=1850, regions=regions)


def _overflow_page() -> AdPage:
    """실측 축소판 — 맨 위 작은 영역 하나가 페이지 전체를 판독문으로 받았다."""
    head = _region("p1_r000", [40, 1, 161, 114], "test_nhcard 58초전")
    body1 = _region("p1_r001", [74, 577, 426, 653], "호텔스닷컴 예약하고 20% 할인")
    body2 = _region("p1_r002", [11, 1267, 397, 1288], "NH농협카드 이벤트 해외여행")
    head.vlm_reading = (
        "test_nhcard 58초전 호텔스닷컴 예약하고 20% 할인 "
        "NH농협카드 이벤트 해외여행 프로모션 기간 2026.08.01"
    )
    return _page([head, body1, body2])


def test_삼킨_영역들을_찾아낸다():
    page = _overflow_page()
    assert set(_swallowed_regions(page.regions[0], page)) == {"p1_r001", "p1_r002"}


def test_범람은_expanded_가_아니라_overflow_로_찍힌다():
    """★ 이 테스트가 결함의 핵심 — 예전엔 초록색 '회수 가능'으로 떴다."""
    page = _overflow_page()
    _score_reading_candidates(page)
    assert page.regions[0].vlm_reading_relation == OVERFLOWED
    assert any("범람 경보" in n for n in page.notes), "조용히 넘기면 안 된다"


def test_범람_후보는_STAGE_3_입력에서_빠진다():
    """실으면 페이지 전체 내용이 이 작은 영역의 근거로 붙는다 — 좌표가 거짓이 된다."""
    page = _overflow_page()
    _score_reading_candidates(page)
    view = build_page_view(page)
    head = next(r for r in view["regions"] if r["region_id"] == "p1_r000")
    assert "vlm_reading" not in head
    # 정본은 그대로 남아야 한다 (조용한 삭제가 아니다)
    assert "test_nhcard" in head["text"]


def test_정상_회수는_범람으로_안_찍힌다():
    """OCR 이 못 읽은 것을 VLM 이 제대로 읽은 경우 — 길이는 늘지만 남의 영역을 안 삼킨다.

    실측에서 이 형태(자기 6자 → 후보 32자)가 길이 조건만으로는 걸렸다.
    """
    r = _region("p1_r000", [40, 1, 400, 114], "스마임카", "스마이카드 전월실적 30만원 이상")
    far = _region("p1_r009", [40, 1600, 400, 1700], "전혀 다른 유의사항 문구입니다")
    page = _page([r, far])
    _score_reading_candidates(page)
    assert _swallowed_regions(r, page) == []
    assert r.vlm_reading_relation != OVERFLOWED
    # 후보가 살아 있어야 한다 — 범람이 아니면 STAGE_3 가 볼 수 있어야 한다
    assert "vlm_reading" in next(
        x for x in build_page_view(page)["regions"] if x["region_id"] == "p1_r000"
    )


def test_정본보다_많이_읽은_것은_여전히_expanded():
    """딱지 체계를 안 망가뜨렸는지 — 정본이 후보에 그대로 들어있는 순수 회수 케이스."""
    r = _region("p1_r000", [40, 1, 400, 114], "우대금리",
                "우대금리 최고 연 0.3%p 제공 조건은 아래와 같습니다")
    page = _page([r])
    _score_reading_candidates(page)
    assert r.vlm_reading_relation == EXPANDED


def test_바로_옆_한_줄이_섞인_정도는_범람이_아니다():
    """기하 검산 — 물리적으로 가능한 범위면 판독이 경계를 조금 넘은 것뿐이다.

    길이만 보면 걸리지만(3배 이상) 삼킨 영역이 자기 높이 근처에 있다 — 실측에서
    이 조건이 없으면 자기 21자→후보 21자 같은 우연 포함이 45건 걸렸다.
    """
    a = _region("p1_r000", [40, 100, 400, 300], "가입금액")
    b = _region("p1_r001", [40, 310, 400, 380], "100만원 이상 원단위")
    a.vlm_reading = "가입금액 100만원 이상 원단위 (2026 기준)"
    page = _page([a, b])
    assert _swallowed_regions(a, page) == []


def test_후보가_짧으면_삼킴으로_안_센다():
    """8자 미만 문구는 우연히 포함될 수 있다."""
    a = _region("p1_r000", [40, 1, 400, 60], "제목")
    b = _region("p1_r009", [40, 1600, 400, 1700], "연 3%")
    a.vlm_reading = "제목 연 3% 상세 내용이 이어집니다 여기까지"
    page = _page([a, b])
    assert _swallowed_regions(a, page) == []
