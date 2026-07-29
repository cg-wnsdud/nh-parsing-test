# -*- coding: utf-8 -*-
"""HWP 표 렌더 — 병합 셀과 '값이 같은 이웃 칸'을 구분하는지 본다.

옛 구현은 markdown 문자열에서 '연속으로 같은 값이면 병합셀'로 접었는데, 004 부대비용
중첩표의 `적용요율 | 0.01% | 0.01%` 에서 하나를 잃었다. 금리·수수료 표에는 같은 값이
나란히 오는 일이 흔해 이 규칙은 계속 값을 잃는다. 구조 API 로 바꿔 규칙을 없앴다.
"""

from types import SimpleNamespace

from nh_parsing.hwp_ingest import _tables_to_lines


def _cell(text="", *, tables=(), rowspan=1, colspan=1):
    paras = []
    if text:
        paras.append(SimpleNamespace(text=text, content=[]))
    for t in tables:
        # 중첩표를 품은 문단은 실제 파서에서 .text 가 그 표의 평문판이다 (004 실측).
        flat = "\n".join(c for row in t._rows for c in row)
        paras.append(SimpleNamespace(text=flat, content=[t]))
    return SimpleNamespace(
        text=text, paragraphs=paras,
        cell_style=SimpleNamespace(rowspan=rowspan, colspan=colspan),
    )


class TableIR:  # 클래스 '이름'으로 표를 판별하므로 이름을 맞춰야 한다
    """iter_cell_positions() 만 흉내 낸 최소 TableIR. 병합 셀은 한 번만 나온다."""

    def __init__(self, rows):
        self._rows = rows
        self.row_count, self.col_count = len(rows), max(len(r) for r in rows)

    def iter_cell_positions(self):
        for r, row in enumerate(self._rows, 1):
            for c, text in enumerate(row, 1):
                yield r, c, _cell(text)

    def markdown(self):  # 폴백 경로가 실수로 타지 않는지 보기 위한 표식
        return "|MARKDOWN-FALLBACK|"


def test_값이_같은_이웃_칸을_둘_다_남긴다():
    """004 실측 — 고정금리·변동금리 요율이 같아도 둘 다 있어야 어느 열인지 안다."""
    table = TableIR([["구분", "고정금리", "변동금리"], ["적용요율", "0.01%", "0.01%"]])
    rows = _tables_to_lines(table)
    assert rows == ["구분 | 고정금리 | 변동금리", "적용요율 | 0.01% | 0.01%"]


def test_구조_API_가_있으면_markdown_을_안_쓴다():
    rows = _tables_to_lines(TableIR([["가", "나"]]))
    assert not any("MARKDOWN-FALLBACK" in r for r in rows)


def test_빈_칸은_건너뛴다():
    assert _tables_to_lines(TableIR([["", "값", ""]])) == ["값"]


def test_중첩표는_그_자리에_인라인된다():
    inner = TableIR([["적용요율", "0.01%", "0.01%"]])

    class Outer(TableIR):
        def iter_cell_positions(self):
            yield 1, 1, _cell("부대비용")
            yield 1, 2, _cell("중도상환해약금", tables=[inner])

    rows = _tables_to_lines(Outer([["x"]]))
    assert rows == ["부대비용 | 중도상환해약금 적용요율 | 0.01% | 0.01%"]


def test_중첩표를_두_번_넣지_않는다():
    """중첩표를 품은 문단의 .text 도 표의 평문판이라, 둘 다 쓰면 표가 두 번 들어간다."""
    inner = TableIR([["가", "나"]])

    class Outer(TableIR):
        def iter_cell_positions(self):
            yield 1, 1, _cell(tables=[inner])

    assert _tables_to_lines(Outer([["x"]])) == ["가 | 나"]


def test_구조_API_가_없으면_markdown_으로_되돌아간다():
    class NoStruct:
        def markdown(self):
            return "| 가 | 나 |\n|---|---|\n| 1 | 2 |"

    assert _tables_to_lines(NoStruct()) == ["가 | 나", "1 | 2"]
