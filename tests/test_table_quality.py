from nh_parsing.ad_export import _table_quality


def _cell(row, col, bbox, text="", refs=()):
    return {
        "row": row, "col": col, "bbox": bbox, "text": text,
        "line_refs": list(refs),
    }


def _table(cells, outside=(), note=None):
    return {
        "cells": cells,
        "lines_outside_cells": list(outside),
        "note": note,
    }


def test_table_quality_trusts_normal_grid_and_matching_line_refs():
    lines = [
        {"line_ref": "L1", "text": "대출 한도"},
        {"line_ref": "L2", "text": "최대 1억원"},
    ]
    table = _table([
        _cell(0, 0, [0, 0, 100, 30], "대출 한도", ["L1"]),
        _cell(0, 1, [100, 0, 200, 30], "최대 1억원", ["L2"]),
        _cell(1, 0, [0, 30, 100, 60]),
        _cell(1, 1, [100, 30, 200, 60]),
    ])

    quality = _table_quality(table, lines, layout_score=0.9)

    assert quality["status"] == "trusted"
    assert quality["issues"] == []


def test_table_quality_marks_reversed_row_and_column_coordinates_invalid():
    table = _table([
        _cell(0, 0, [100, 50, 200, 100]),
        _cell(0, 1, [0, 50, 100, 100]),
        _cell(1, 0, [100, 0, 200, 50]),
        _cell(1, 1, [0, 0, 100, 50]),
    ])

    quality = _table_quality(table, [], layout_score=0.9)

    assert quality["status"] == "invalid"
    assert "cell_coordinate_order_reversed" in quality["issues"]


def test_table_quality_marks_many_cell_text_reference_mismatches_invalid():
    lines = [
        {"line_ref": "L1", "text": "상환 방법"},
        {"line_ref": "L2", "text": "원리금 균등 분할상환"},
    ]
    table = _table([
        _cell(0, 0, [0, 0, 100, 30], "대출 기간", ["L1"]),
        _cell(0, 1, [100, 0, 200, 30], "만기 일시상환", ["L2"]),
    ])

    quality = _table_quality(table, lines, layout_score=0.9)

    assert quality["status"] == "invalid"
    assert "cell_text_reference_mismatch:2/2" in quality["issues"]


def test_table_quality_keeps_caption_outside_grid_as_degraded_not_invalid():
    table = _table([
        _cell(0, 0, [0, 0, 100, 30]),
        _cell(0, 1, [100, 0, 200, 30]),
        _cell(1, 0, [0, 30, 100, 60]),
        _cell(1, 1, [100, 30, 200, 60]),
    ], outside=["L-caption"])

    quality = _table_quality(table, [{"line_ref": "L-caption", "text": "표 제목"}], layout_score=0.9)

    assert quality["status"] == "degraded"
    assert quality["issues"] == ["lines_outside_cells"]
