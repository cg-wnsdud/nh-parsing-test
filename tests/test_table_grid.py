# -*- coding: utf-8 -*-
"""표 행·열 격자(`RegionTable`)의 계약.

**왜 이 시험들이 있나.** PaddleX 는 `table_res_list.pred_html` + `cell_box_list` 로
표를 정확히 돌려주는데(실측 2026-08-24: 우대조건↔우대금리 짝이 맞다) 우리 코드가
`table_res_list` 를 읽지 않아 통째로 버리고 있었다. 표가 줄로 흩어지면서
`'-(가입월부터…)입출식05%p'` 처럼 소수점이 문장에 붙는 일까지 생겼다.

아래 HTML·좌표는 전부 **실제 서버 응답에서 가져온 것**이다 — 지어낸 표로 시험하면
`rowspan` 처럼 실제로만 나오는 모양을 놓친다.
"""

from __future__ import annotations

from nh_parsing.ad_export import _table_out
from nh_parsing.paddlex_client import (
    LayoutBlock,
    _attach_tables,
    _build_table_grid,
)

# 실측: 13. 대출성상품.pdf p1 (2행 3열, 병합 없음)
HTML_요율 = (
    "<html><body><table><tbody>"
    "<tr><td>구분</td><td>고정금리</td><td>변동금리</td></tr>"
    "<tr><td>적용요율</td><td>0.01%</td><td>0.01%</td></tr>"
    "</tbody></table></body></html>"
)
BOXES_요율 = [
    [726, 1813, 973, 1852], [973, 1813, 1220, 1852], [1220, 1813, 1397, 1852],
    [726, 1852, 973, 1886], [974, 1852, 1220, 1886], [1220, 1852, 1397, 1886],
]

# 실측: 16. 대출성상품.pdf p1 — `<td rowspan="4">` 가 있는 요율표의 앞 세 행
HTML_병합 = (
    "<html><body><table><tbody>"
    "<tr><td>구분</td><td>임차보증금 부부합산 연소득</td><td>5천만원 이하</td></tr>"
    '<tr><td rowspan="2">일반</td><td>2천만원 이하</td><td>2.5%</td></tr>'
    "<tr><td>4천만원이하</td><td>2.7%</td></tr>"
    "</tbody></table></body></html>"
)


def test_행_열이_HTML_순서대로_셀_좌표와_짝지어진다() -> None:
    """`cell_box_list` 는 행우선(row-major) 순서로 온다 — 실측 4개 표 전부."""
    grid = _build_table_grid(HTML_요율, BOXES_요율, 1654, 2339)
    assert (grid["n_rows"], grid["n_cols"]) == (2, 3)
    assert grid["note"] is None
    by_rc = {(c["row"], c["col"]): c for c in grid["cells"]}
    assert by_rc[(0, 0)]["text"] == "구분"
    assert by_rc[(0, 2)]["text"] == "변동금리"
    assert by_rc[(1, 0)]["text"] == "적용요율"
    # 좌표가 제자리에 붙었나 — '적용요율' 은 왼쪽 열, 둘째 행이다
    assert by_rc[(1, 0)]["bbox"] == [726, 1852, 973, 1886]
    assert by_rc[(0, 2)]["bbox"] == [1220, 1813, 1397, 1852]


def test_rowspan_이_있으면_다음_행의_열_번호가_밀린다() -> None:
    """병합셀을 안 세면 '4천만원이하' 가 0열로 들어가 '일반' 자리를 덮는다.

    실측 표의 모양:
        row0  구분        | 임차보증금…    | 5천만원 이하
        row1  일반(rowspan) | 2천만원 이하 | 2.5%
        row2       ↑ 계속  | 4천만원이하   | 2.7%
    """
    grid = _build_table_grid(HTML_병합, None, 1654, 2339)
    cells = {(c["row"], c["col"]): c["text"] for c in grid["cells"]}
    assert cells[(1, 0)] == "일반"
    # row2 의 첫 `<td>` 는 0열이 아니라 **1열**이어야 한다
    assert cells[(2, 1)] == "4천만원이하"
    assert cells[(2, 2)] == "2.7%"
    assert (2, 0) not in cells          # 그 자리는 '일반' 이 점유 중이다
    span = next(c for c in grid["cells"] if c["text"] == "일반")
    assert span["row_span"] == 2


def test_셀_개수가_안_맞으면_좌표를_비우고_이유를_남긴다() -> None:
    """틀린 좌표보다 없는 좌표가 낫다. 다만 **조용히** 비우지는 않는다."""
    grid = _build_table_grid(HTML_요율, BOXES_요율[:4], 1654, 2339)   # 6칸인데 좌표 4개
    assert (grid["n_rows"], grid["n_cols"]) == (2, 3)                 # 격자는 살아 있다
    assert all(c["bbox"] is None for c in grid["cells"])
    assert grid["note"] and "6" in grid["note"] and "4" in grid["note"]


def test_원본_HTML_을_버리지_않는다() -> None:
    """우리 격자 해석이 틀렸을 때 대조할 유일한 근거다."""
    grid = _build_table_grid(HTML_요율, BOXES_요율, 1654, 2339)
    assert grid["html"] == HTML_요율


def test_표_격자가_같은_표의_블록에_붙는다() -> None:
    """짝짓기 기준은 `block_content` == `pred_html` (실측: 정확히 일치)."""
    blocks = [
        LayoutBlock(label="text", bbox=[0, 0, 100, 100], content="본문입니다"),
        LayoutBlock(label="table", bbox=[725, 1811, 1398, 1887], content=HTML_요율),
    ]
    _attach_tables(blocks, [{"pred_html": HTML_요율, "cell_box_list": BOXES_요율}], 1654, 2339)
    assert blocks[0].table is None
    assert blocks[1].table is not None
    assert blocks[1].table["n_cols"] == 3


def test_같은_HTML_표가_둘이면_각자_다른_블록에_붙는다() -> None:
    """실측: `1. 예금성상품(거치식·적립식 통합).pdf` 는 우대금리표가 두 벌이고
    열 이름만 다르다. 한 블록에 둘 다 붙으면 한쪽 표가 사라진다."""
    b1 = LayoutBlock(label="table", bbox=[0, 100, 500, 200], content=HTML_요율)
    b2 = LayoutBlock(label="table", bbox=[0, 900, 500, 1000], content=HTML_요율)
    _attach_tables(
        [b1, b2],
        [{"pred_html": HTML_요율, "cell_box_list": BOXES_요율},
         {"pred_html": HTML_요율, "cell_box_list": BOXES_요율}],
        1654, 2339,
    )
    assert b1.table is not None and b2.table is not None


def test_정본_줄이_좌표로_셀에_배정된다() -> None:
    """셀 텍스트는 표 인식 값이고 **정본은 줄 쪽**이다 — 둘을 잇는 게 `line_refs`.

    실측 근거: 표 안 글자에도 OCR 오류가 있다('BD', '2 295,000원'). 그래서 셀 텍스트로
    갈아치우지 않고 정본 줄을 가리킨다.
    """
    grid = _build_table_grid(HTML_요율, BOXES_요율, 1654, 2339)
    lines = [
        {"line_ref": "p1/r025/L00", "bbox": [824, 1816, 878, 1848], "text": "구분"},
        {"line_ref": "p1/r025/L01", "bbox": [1296, 1815, 1391, 1848], "text": "변동금리"},
        {"line_ref": "p1/r025/L02", "bbox": [740, 1858, 900, 1880], "text": "적용요율"},
        {"line_ref": "p1/r025/L03", "bbox": [0, 5000, 40, 5020], "text": "표 밖 캡션"},
    ]
    out = _table_out(grid, lines)
    by_rc = {(c["row"], c["col"]): c for c in out["cells"]}
    assert by_rc[(0, 0)]["line_refs"] == ["p1/r025/L00"]
    assert by_rc[(0, 2)]["line_refs"] == ["p1/r025/L01"]
    assert by_rc[(1, 0)]["line_refs"] == ["p1/r025/L02"]
    assert by_rc[(0, 1)]["line_refs"] == []          # 그 셀엔 정본 줄이 없다
    # 셀에 못 닿은 줄은 숨기지 않는다
    assert out["lines_outside_cells"] == ["p1/r025/L03"]


def test_표가_아닌_영역은_table_이_None() -> None:
    assert _table_out(None, [{"line_ref": "p1/r000/L00", "bbox": [0, 0, 1, 1]}]) is None
