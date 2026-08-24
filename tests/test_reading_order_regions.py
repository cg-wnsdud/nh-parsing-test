# -*- coding: utf-8 -*-
"""영역끼리의 읽기 순서(`llm_view.repair_reading_order`).

**왜 이 시험들이 있나.** `page.regions` 는 2026-08-24 까지 PaddleX `parsing_res_list`
순서 그대로였고, 그 순서에는 문장을 거꾸로 놓는 데가 있다. 실측(원본 이미지 대조 완료,
`[예금성상품-적금] 올원e적금.png` p1):

    원본:  '금융소비자 보호에 관한 법률 제19조제1항에 따른 설명을 받을'   y=5546
           '수 있는 권리가 있습니다.'                                     y=5611
    기존:  r044 = 뒷줄, r045 = 앞줄  ← 이어 읽으면 거꾸로 나온다

**그런데 y 로 전부 다시 정렬하면 안 된다.** 한 번 그렇게 만들어 보고 되돌렸다 —
2단 문서(`2. 예금성상품(적립식).pdf`)가 좌우 칸을 번갈아 읽혔다. 그래서 지금 규칙은
"엔진 순서를 정본으로 두고 확실히 거꾸로인 이웃만 교환"이다. 아래 시험이 **양쪽을 다**
지킨다 — 문장 역순은 고쳐지고 2단 문서는 손대지 않는다.
"""

from __future__ import annotations

from nh_parsing.ir import AdPage, Line, Region
from nh_parsing.llm_view import region_text_box, repair_reading_order
from nh_parsing.pipeline import _finalize_reading_order, _shift_table


def _region(rid: str, lines: list[tuple[str, int, int, int]], card: int | None = None) -> Region:
    """lines = [(글자, y0, x0, 너비)]. 높이는 40 고정."""
    return Region(
        region_id=rid,
        bbox=[min(x for _, _, x, _ in lines), min(y for _, y, _, _ in lines),
              max(x + w for _, _, x, w in lines), max(y for _, y, _, _ in lines) + 40],
        card_no=card,
        lines=[Line(text=t, bbox=[x, y, x + w, y + 40], source="ocr", confidence=0.9)
               for t, y, x, w in lines],
    )


def _page(*regions: Region) -> AdPage:
    return AdPage(page_no=1, canvas_w=1654, canvas_h=6429, parse_route="ocr", regions=list(regions))


def _ids(page: AdPage) -> list[str]:
    return [r.region_id for r in page.regions]


# ─────────────────────────────────────────────────────────────
# 고쳐야 하는 것
# ─────────────────────────────────────────────────────────────

def test_같은_칸에서_거꾸로_실린_문장이_제자리로_돌아온다() -> None:
    """실측 사례 그대로 — 올원e p1 꼬리. 좌우가 겹치고 세로로 완전히 갈린다."""
    page = _page(
        _region("p1_r044", [("수 있는 권리가 있습니다.", 5611, 120, 660)]),
        _region("p1_r045", [("금융소비자 보호에 관한 법률 제19조제1항에 따른 설명을 받을", 5546, 116, 874)]),
    )
    _finalize_reading_order(page)
    assert _ids(page) == ["p1_r045", "p1_r044"]
    text = " ".join(l.text for r in page.regions for l in r.lines)
    assert text.startswith("금융소비자 보호에 관한 법률")


def test_세_영역이_거꾸로여도_바로잡힌다() -> None:
    """교환은 안정될 때까지 반복한다 — 한 번만 돌면 두 칸 밀린 것을 못 잡는다."""
    page = _page(
        _region("c", [("셋째", 300, 100, 500)]),
        _region("b", [("둘째", 200, 100, 500)]),
        _region("a", [("첫째", 100, 100, 500)]),
    )
    _finalize_reading_order(page)
    assert _ids(page) == ["a", "b", "c"]


# ─────────────────────────────────────────────────────────────
# 건드려서는 안 되는 것
# ─────────────────────────────────────────────────────────────

def test_2단_문서는_칸을_지킨다() -> None:
    """실측 `2. 예금성상품(적립식).pdf` p1 의 좌표 그대로.

    y 로 정렬하면 `우대금리`(y1480) → `가입대상`(y1494) → `표`(y1582) → `가입기간`(y1684)
    처럼 칸을 번갈아 읽는다. 14px 차이라 y 만으로는 소속을 알 수 없다.
    좌우가 안 겹치므로(왼쪽 271~642 / 오른쪽 776~1518) 교환 대상이 아니다.
    """
    page = _page(
        _region("p1_r005", [("가입대상개인(1인1계좌)", 1494, 271, 371)]),
        _region("p1_r006", [("가입기간12개월(단일)", 1684, 271, 307)]),
        _region("p1_r009", [("기본금리연2.30%", 1899, 272, 402)]),
        _region("p1_r010", [("판매한도3만좌", 2068, 271, 224)]),
        _region("p1_r011", [("우대금리최대연4.8%p", 1480, 776, 432)]),
        _region("p1_r013", [("내용", 1582, 800, 695)]),
        _region("p1_r016", [("중도해지및만기후금리", 2017, 776, 737)]),
    )
    _finalize_reading_order(page)
    assert _ids(page) == ["p1_r005", "p1_r006", "p1_r009", "p1_r010",
                          "p1_r011", "p1_r013", "p1_r016"]


def test_같은_행의_좌우_짝은_순서를_지킨다() -> None:
    """세로로 겹치면 같은 행이다 — `대출대상 | 내용` 처럼 항목명↔내용 짝이 뒤집히면 안 된다."""
    page = _page(
        _region("label", [("대출대상", 664, 334, 109)]),
        _region("value", [("금융기관에 3개월 이상 재직 중", 549, 558, 797)]),
    )
    _finalize_reading_order(page)
    # value 가 위(549)지만 label(664~704)과 세로로 겹치므로 교환하지 않는다
    assert _ids(page) == ["label", "value"]


def test_카드_경계를_넘어_섞이지_않는다() -> None:
    """좌우로 나란한 카드(003 EVENT1/EVENT2)는 y 가 비슷해 섞으면 문장이 번갈아 나온다."""
    page = _page(
        _region("c2a", [("카드2 첫줄", 500, 700, 300)], card=2),
        _region("c1a", [("카드1 첫줄", 510, 100, 300)], card=1),
        _region("c2b", [("카드2 둘째줄", 620, 700, 300)], card=2),
        _region("c1b", [("카드1 둘째줄", 630, 100, 300)], card=1),
    )
    _finalize_reading_order(page)
    assert _ids(page) == ["c1a", "c1b", "c2a", "c2b"]


def test_다른_칸의_로고는_움직이지_않는다() -> None:
    """못 고치는 것을 시험으로 못박는다 — 이게 남는 걸 알고 고른 규칙이다.

    실측: 올원e 'NH'(y=125, x=906)가 45번째에 실린다. 왼쪽 본문(x=69~1009)과 좌우가
    겹치기는 하지만 세로로도 겹치는지가 갈린다. 여기서는 안 겹치는 배치를 쓴다.
    """
    page = _page(
        _region("body", [("1천원 이상 30만원 이하(원단위)", 1395, 69, 300)]),
        _region("logo", [("NH", 125, 906, 121)]),
    )
    _finalize_reading_order(page)
    # 좌우가 안 겹친다(69~369 vs 906~1027) → 교환 대상이 아니다
    assert _ids(page) == ["body", "logo"]


# ─────────────────────────────────────────────────────────────
# 세 곳의 순서가 하나인가
# ─────────────────────────────────────────────────────────────

def test_한_번_더_불러도_결과가_같다() -> None:
    """검수 화면·추출층이 같은 함수를 한 번 더 부른다 — 그게 순서를 흔들면 안 된다."""
    page = _page(
        _region("p1_r044", [("뒷줄", 5611, 120, 660)]),
        _region("p1_r045", [("앞줄", 5546, 116, 874)]),
        _region("p1_r011", [("다른 칸", 1480, 1200, 300)]),
    )
    _finalize_reading_order(page)
    once = _ids(page)
    assert [r.region_id for r in repair_reading_order(page.regions)] == once


def test_영역_안_줄도_읽기_순서로_정렬된다() -> None:
    """VLM 단계가 나중에 붙인 줄이 y 순서를 깨뜨린 채 남지 않게."""
    page = _page(_region("p1_r000", [("아래", 900, 10, 300), ("위", 100, 10, 300),
                                     ("가운데", 500, 10, 300)]))
    _finalize_reading_order(page)
    assert [l.text for l in page.regions[0].lines] == ["위", "가운데", "아래"]


def test_region_id_는_바뀌지_않는다() -> None:
    """`line_ref` 가 `region_id` 로 만들어지므로 재정렬이 참조를 깨서는 안 된다."""
    page = _page(
        _region("p1_r044", [("뒷줄", 5611, 120, 660)]),
        _region("p1_r045", [("앞줄", 5546, 116, 874)]),
    )
    _finalize_reading_order(page)
    assert sorted(_ids(page)) == ["p1_r044", "p1_r045"]


def test_글자_없는_영역은_검출_bbox_로_폴백한다() -> None:
    """검출만 되고 글자가 안 붙은 박스도 순서 계산에 들어가야 한다(검수 화면에 나온다)."""
    empty = Region(region_id="empty", bbox=[10, 20, 110, 60])
    assert region_text_box(empty) == [10, 20, 110, 60]


# ─────────────────────────────────────────────────────────────
# 타일 오프셋
# ─────────────────────────────────────────────────────────────

def test_타일_오프셋이_표_셀_좌표에도_적용된다() -> None:
    """안 하면 셀이 타일 좌표에 남아 페이지 위쪽 엉뚱한 자리를 가리킨다."""
    table = {"n_rows": 1, "n_cols": 2, "html": "<table/>", "note": None,
             "cells": [{"row": 0, "col": 0, "row_span": 1, "col_span": 1,
                        "bbox": [10, 20, 110, 60], "text": "구분"},
                       {"row": 0, "col": 1, "row_span": 1, "col_span": 1,
                        "bbox": None, "text": "좌표 없음"}]}
    out = _shift_table(table, 1400)
    assert out["cells"][0]["bbox"] == [10, 1420, 110, 1460]
    assert out["cells"][1]["bbox"] is None      # 없던 좌표를 지어내지 않는다
    assert table["cells"][0]["bbox"] == [10, 20, 110, 60]   # 원본은 그대로


def test_오프셋이_0이면_표를_그대로_둔다() -> None:
    table = {"cells": [{"row": 0, "col": 0, "bbox": [1, 2, 3, 4], "text": "x"}]}
    assert _shift_table(table, 0) is table
    assert _shift_table(None, 500) is None
