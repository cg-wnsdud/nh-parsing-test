# -*- coding: utf-8 -*-
"""통독 후보 점수·관계 딱지를 **정본 확정 뒤에** 매기는지 고정한다.

이 테스트가 지키는 것은 값이 아니라 **순서**다. 관계 판정을 밴드 판독 시점으로
되돌리면(_merged_band_read 안에서 즉시 판정) 아래 첫 테스트가 깨진다.

bbox 는 올원e p1_r006 실측값이다 — '가입기간'(top 1218)이 '12 개월'(top 1216)보다
2px 아래라, 전역 정렬 키 (top, left) 로는 값이 라벨보다 앞에 온다. 표 한 행의 두
셀은 이렇게 몇 px 어긋나는 게 정상이므로 이건 특수 케이스가 아니다.
"""

from nh_parsing.ir import AdPage, Line, Region
from nh_parsing.pipeline import _score_reading_candidates
from nh_parsing.tiling import sort_reading_order
from nh_parsing.truncation import HEAD_DROPPED, TAIL_TRUNCATED, classify_reading


def _table_row_region() -> Region:
    """올원e p1_r006 — 표 한 행(라벨 | 값). 라인은 전역 정렬 순서로 담는다."""
    return Region(
        region_id="p1_r006",
        bbox=[66, 1217, 557, 1264],
        lines=[
            # (top, left) 정렬 결과 그대로 — 값이 라벨보다 앞
            Line(text="12 개월", bbox=[408, 1216, 562, 1275], source="ocr"),
            Line(text="가입기간", bbox=[69, 1218, 241, 1272], source="ocr"),
        ],
        vlm_reading="12 개월",
    )


def test_전역정렬_순서로는_딱지가_뒤집힌다():
    """수정 전 동작을 못박아 둔다 — 이게 실패하면 defect 재현이 안 되는 것이다."""
    region = _table_row_region()
    ocr = " ".join(l.text for l in region.lines)
    assert ocr == "12 개월 가입기간"
    assert classify_reading(ocr, region.vlm_reading).kind == TAIL_TRUNCATED


def test_읽기순서_정렬_뒤에_매기면_앞항목_생략으로_바뀐다():
    region = _table_row_region()
    region.lines = sort_reading_order(region.lines)
    assert [l.text for l in region.lines] == ["가입기간", "12 개월"]

    page = AdPage(page_no=1, parse_route="ocr", canvas_w=1122, canvas_h=6429,
                  regions=[region])
    _score_reading_candidates(page)

    assert region.vlm_reading_relation == HEAD_DROPPED, (
        "정렬 뒤 정본은 '가입기간 12 개월' 이므로 '앞 항목명 생략'이 맞는 답이다"
    )
    # 무해 판정이므로 잘림 경보가 뜨면 안 된다 (검수 화면 '확인 필요' 오탐 방지)
    assert not any("잘림 경보" in n for n in page.notes), page.notes


def test_점수도_같은_시점에_매겨진다():
    """score/coverage 는 토큰 집합이라 순서엔 무관하지만, 정본이 확정된 뒤여야 한다.

    밴드 판독 이후에도 유령 중복 라인 삭제(vlm_direct.py:413)가 라인을 빼므로
    그 시점의 정본은 최종본이 아니다.
    """
    region = _table_row_region()
    assert region.vlm_reading_score is None and region.vlm_reading_coverage is None
    _score_reading_candidates(AdPage(page_no=1, parse_route="ocr", canvas_w=1122, canvas_h=6429,
                                     regions=[region]))
    # 후보 '12 개월' 은 정본에 그대로 있으므로 정밀도 만점, 커버리지는 절반
    assert region.vlm_reading_score == 1.0
    assert 0.0 < region.vlm_reading_coverage < 1.0


def test_후보가_없는_영역은_건드리지_않는다():
    region = Region(region_id="p1_r001", bbox=[0, 0, 10, 10],
                    lines=[Line(text="본문", bbox=[0, 0, 10, 10], source="ocr")])
    page = AdPage(page_no=1, parse_route="ocr", canvas_w=100, canvas_h=100, regions=[region])
    _score_reading_candidates(page)
    assert region.vlm_reading_relation is None
    assert region.vlm_reading_score is None
    assert page.notes == []


def test_잘린_후보는_경보가_뜬다():
    """002 p1_r018 실측 — 경품 금액이 통째로 빠진 후보. 정렬과 무관하게 잘림이다."""
    region = Region(
        region_id="p1_r018",
        bbox=[0, 0, 800, 100],
        lines=[Line(text="경품안내 총777명추첨 N pay 포인트 쿠폰 네이버페이 20,000원",
                    bbox=[0, 0, 800, 100], source="ocr")],
        vlm_reading="총 777명 추첨",
    )
    page = AdPage(page_no=1, parse_route="ocr", canvas_w=800, canvas_h=200, regions=[region])
    _score_reading_candidates(page)
    assert region.vlm_reading_relation == TAIL_TRUNCATED
    assert any("잘림 경보" in n and "p1_r018" in n for n in page.notes), page.notes
