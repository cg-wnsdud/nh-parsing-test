# -*- coding: utf-8 -*-
"""라인→영역 배정 결함(§B) — build_regions 의 중심점 판정을 겹침비율로 바꾼 이유.

중심점 판정은 "이 줄의 중심이 어느 박스 안에 있는가"만 본다. 서로 겹치는 두
영역(예: 배경 전체를 덮는 큰 '표' 블록 + 그 안의 정밀한 작은 캡션 블록)이 같은
점을 동시에 품으면, 옛 방식은 `regions` 목록에서 **먼저 나온 쪽**을 집는다 —
정답이 있는데도 레이아웃 엔진이 블록을 어느 순서로 돌려줬는지에 답이 좌우된다.
겹침비율은 "이 줄의 몇 %를 담았는가"로 재므로 순서에 안 흔들리고, 동률(중첩
영역이 둘 다 완전히 품는 경우)이면 더 작은(구체적인) 영역을 우선한다 —
`_absorb_unassigned_into_regions`가 미배정 라인에 이미 쓰던 것과 같은 원칙이다.
"""

from nh_parsing.ir import Line
from nh_parsing.paddlex_client import LayoutBlock
from nh_parsing.regions import build_regions


def _block(label: str, bbox: list[int]) -> LayoutBlock:
    return LayoutBlock(label=label, bbox=bbox, score=0.9)


def test_중첩된_두_영역_중_더_작은_쪽이_이긴다_목록_순서와_무관():
    """큰 배경 블록이 목록에 먼저 와도, 완전히 품는 두 영역 중 작은 쪽이 이긴다."""
    big = _block("표", [0, 0, 1000, 1000])
    small = _block("text", [400, 400, 500, 450])
    line = Line(text="캡션", bbox=[405, 405, 495, 445], source="ocr")

    regions_big_first, unassigned = build_regions([big, small], [line], canvas_h=1000, page_no=1)
    regions_small_first, _ = build_regions([small, big], [line], canvas_h=1000, page_no=1)

    assert unassigned == []
    small_region_big_first = next(r for r in regions_big_first if r.bbox == small.bbox)
    small_region_small_first = next(r for r in regions_small_first if r.bbox == small.bbox)
    assert small_region_big_first.lines == [line], "블록 순서가 배경(큰 영역)-먼저였을 때도 작은 쪽이 이겨야 한다"
    assert small_region_small_first.lines == [line]
    big_region = next(r for r in regions_big_first if r.bbox == big.bbox)
    assert big_region.lines == [], "큰 배경 영역이 캡션을 가로채면 안 된다"


def test_두_영역에_걸친_줄은_더_많이_겹친_쪽으로_간다():
    """표 칸이 덜 갈라져 두 영역에 걸쳐도, 절반 이상 담근 쪽이 이긴다."""
    left = _block("text", [0, 0, 300, 50])
    right = _block("text", [350, 0, 650, 50])
    # 폭 360짜리 줄: left 와 250, right 와 10 만큼 겹친다 → left 압도적 우세
    line = Line(text="가입금액100만원이상", bbox=[0, 0, 360, 50], source="digital")

    regions, unassigned = build_regions([left, right], [line], canvas_h=50, page_no=1)

    left_region = next(r for r in regions if r.bbox == left.bbox)
    right_region = next(r for r in regions if r.bbox == right.bbox)
    assert left_region.lines == [line]
    assert right_region.lines == []
    assert unassigned == []


def test_어느_영역과도_절반_못_채우면_미배정으로_남는다():
    """겹침이 애매하면 억지로 붙이지 않는다 — 흡수 단계가 나중에 판단한다."""
    a = _block("text", [0, 0, 100, 50])
    b = _block("text", [300, 0, 400, 50])
    # 폭 300짜리 줄: a 와 50, b 와 50만 겹친다 (둘 다 1/6 미만)
    line = Line(text="애매한 줄", bbox=[50, 0, 350, 50], source="ocr")

    regions, unassigned = build_regions([a, b], [line], canvas_h=50, page_no=1)

    assert unassigned == [line]
    assert all(r.lines == [] for r in regions)


def test_온전히_한_영역_안에_있으면_그대로_배정된다():
    """가장 흔한 경우(회귀 방지) — 줄이 영역 하나에만 온전히 들어가면 그대로 간다."""
    region_block = _block("text", [0, 0, 500, 100])
    other = _block("text", [600, 0, 900, 100])
    line = Line(text="본문 한 줄", bbox=[50, 10, 450, 90], source="ocr")

    regions, unassigned = build_regions([region_block, other], [line], canvas_h=100, page_no=1)

    target = next(r for r in regions if r.bbox == region_block.bbox)
    assert target.lines == [line]
    assert unassigned == []
