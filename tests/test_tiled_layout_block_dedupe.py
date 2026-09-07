# -*- coding: utf-8 -*-
"""오버랩 타일의 레이아웃 블록 중복 제거 회귀 테스트."""

from nh_parsing.paddlex_client import LayoutBlock
from nh_parsing.pipeline import _dedupe_tiled_layout_blocks


def _block(
    bbox: list[int], *, tile: int, span: tuple[int, int], label: str = "text",
) -> LayoutBlock:
    return LayoutBlock(
        label=label,
        bbox=bbox,
        score=0.9,
        tile_indices=(tile,),
        tile_spans=(span,),
    )


def test_서로_다른_오버랩_타일의_같은_블록은_합집합으로_병합한다():
    blocks = [
        _block([41, 4838, 619, 4915], tile=2, span=(3200, 5000)),
        _block([43, 4863, 634, 5064], tile=3, span=(4800, 6600)),
    ]

    kept, count = _dedupe_tiled_layout_blocks(blocks)

    assert count == 1
    assert len(kept) == 1
    assert kept[0].bbox == [41, 4838, 634, 5064]
    assert kept[0].tile_indices == (2, 3)


def test_같은_타일의_중첩_영역은_의도된_계층일_수_있어_합치지_않는다():
    blocks = [
        _block([10, 100, 600, 300], tile=1, span=(0, 1600)),
        _block([12, 120, 598, 280], tile=1, span=(0, 1600)),
    ]

    kept, count = _dedupe_tiled_layout_blocks(blocks)

    assert count == 0
    assert len(kept) == 2


def test_다른_label은_타일과_bbox가_겹쳐도_합치지_않는다():
    blocks = [
        _block([10, 1450, 600, 1580], tile=0, span=(0, 1600), label="text"),
        _block([10, 1450, 600, 1580], tile=1, span=(1400, 3000), label="vision_footnote"),
    ]

    kept, count = _dedupe_tiled_layout_blocks(blocks)

    assert count == 0
    assert len(kept) == 2


def test_가로_위치가_다른_형제_블록은_합치지_않는다():
    blocks = [
        _block([10, 1450, 280, 1580], tile=0, span=(0, 1600)),
        _block([300, 1450, 600, 1580], tile=1, span=(1400, 3000)),
    ]

    kept, count = _dedupe_tiled_layout_blocks(blocks)

    assert count == 0
    assert len(kept) == 2


def test_실제_타일_오버랩_띠_밖의_박스는_합치지_않는다():
    blocks = [
        _block([10, 1000, 600, 1200], tile=0, span=(0, 1600)),
        _block([10, 1000, 600, 1200], tile=1, span=(1400, 3000)),
    ]

    kept, count = _dedupe_tiled_layout_blocks(blocks)

    assert count == 0
    assert len(kept) == 2
