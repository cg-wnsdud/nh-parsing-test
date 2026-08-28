"""PDF 자체 구조(TEXT 런 + 세로 구분선)로 줄을 정하는 경로의 회귀 시험.

여기서 못박는 것은 두 결함이다. 둘 다 2026-08-21 에 원본 대조로 발견했다.

  ① 글자 교차 스크램블 — 24pt 제목의 글꼴칸(높이 67px) 안에 10.4pt 글자가 들어가
     겹침이 100% 가 되면서, 가로로 640px 떨어진 오른쪽 칸 **두 줄**이 제목에 끌려와
     한 줄이 됐다. x 정렬 때 글자가 번갈아 섞여 판독이 불가능해졌다.
  ② 칼럼 넘김 — 좌우 2단이 같은 높이라는 이유로 한 줄로 붙었고, 그 줄이 어느 한쪽
     영역에도 50% 이상 겹치지 않아 엉뚱한 영역(하단 유의사항)에 배정됐다.

기호 재부착(`_attach_marks`)도 같이 못박는다 — 줄 소속을 베이스라인으로 정하면
마침표·`■` 처럼 광학적으로 가운데 맞춘 기호가 떨어져 나가기 때문이다.
"""

from pathlib import Path

import pypdfium2 as pdfium
import pytest

from nh_parsing.triage import (
    _text_runs,
    _vertical_dividers,
    extract_digital_lines,
)

PDF = Path("nh-data/new-sample-data/1. 예금성상품(적립식).pdf")
ARC_PDF = Path("nh-data/new-sample-data/1. 예금성상품(입출식·적립식 통합).pdf")
PX_PER_PT = 200 / 72

pytestmark = pytest.mark.skipif(not PDF.exists(), reason="샘플 PDF 없음")


def _lines(path: Path) -> list:
    page = pdfium.PdfDocument(str(path))[0]
    return extract_digital_lines(page, PX_PER_PT)


def _texts(path: Path) -> list[str]:
    # 이 파일의 기존 회귀 검증은 줄 경계·기호 보존을 대상으로 한다. PDF 공백을
    # 정본으로 보존하기 시작해도 그 검증의 의미가 바뀌지 않도록 비교에서는 공백만 뺀다.
    return ["".join(ln.text.split()) for ln in _lines(path)]


def test_세로_구분선을_벡터_객체로_찾는다():
    """좌우 2단 점선은 임계값이 아니라 PDF 에 그려진 객체다."""
    page = pdfium.PdfDocument(str(PDF))[0]
    tp = page.get_textpage()
    chars = [
        (tp.get_text_range(i, 1), tp.get_charbox(i), None, None, None)
        for i in range(tp.count_chars())
        if tp.get_text_range(i, 1).strip()
    ]
    dividers = _vertical_dividers(page, chars)
    # 좌우 2단 구분선(x≈256.7pt) 과 표 내부 구분선(x≈484.7pt) 둘
    assert len(dividers) == 2
    xs = sorted(round(x, 1) for x, _, _ in dividers)
    assert xs == [256.7, 484.7]


def test_두_줄이_글자_단위로_교차되지_않는다():
    """결함 ①. 예금자보호 문구가 두 줄로 온전히 나와야 한다."""
    texts = _texts(PDF)
    assert "이예금은예금자보호법에따라원금과소정의이자를합하여" in texts
    assert "1인당“1억원까지”(본은행의여타보호상품과합산)보호됩니다." in texts
    # 교차되면 이런 모양이 된다 — 어떤 줄에도 이 조각이 있어서는 안 된다
    assert not any("1이인예당금" in t for t in texts)


def test_좌우_칸이_한_줄로_붙지_않는다():
    """결함 ②. 구분선 왼쪽(만기후 금리)과 오른쪽(가입혜택)은 다른 줄이다."""
    texts = _texts(PDF)
    assert "만기후금리중도해지및만기후" in texts
    assert any(t.startswith("가입혜택") for t in texts)
    assert not any("만기후금리" in t and "가입혜택" in t for t in texts)


def test_구분선을_가로지르는_줄이_없다():
    """구분선의 y 범위 안에서 그것을 넘는 줄은 하나도 없어야 한다."""
    page = pdfium.PdfDocument(str(PDF))[0]
    _, page_h = page.get_size()
    tp = page.get_textpage()
    chars = [
        (tp.get_text_range(i, 1), tp.get_charbox(i), None, None, None)
        for i in range(tp.count_chars())
        if tp.get_text_range(i, 1).strip()
    ]
    dividers = _vertical_dividers(page, chars)
    crossing = []
    for line in extract_digital_lines(page, PX_PER_PT):
        x0, y0, x1, y1 = line.bbox
        for x, bottom, top in dividers:
            xp = x * PX_PER_PT
            dy0, dy1 = (page_h - top) * PX_PER_PT, (page_h - bottom) * PX_PER_PT
            if x0 < xp < x1 and dy0 < y1 and dy1 > y0:
                crossing.append(line.text)
                break
    assert crossing == []


@pytest.mark.parametrize(
    "expected",
    [
        "1.0%p",                    # 마침표가 별도 런이라 떨어져 `10%p` 가 됐다
        "1.5%p",
        "banking.nonghyup.com",     # 도메인의 점 둘
        "준법감시인심의필2026-4256",   # 하이픈
    ],
)
def test_기호가_제자리에_붙는다(expected):
    assert expected in _texts(PDF)


def test_글머리_기호가_본문에_붙는다():
    """`■` 는 본문보다 1.25pt 높게 그려져 베이스라인으로는 안 붙는다."""
    bullets = [t for t in _texts(PDF) if t.startswith("■")]
    assert len(bullets) == 7
    assert "■" not in _texts(PDF)  # 기호만 남은 줄이 있어서는 안 된다


def test_글자를_하나도_잃지_않는다():
    """줄 묶기를 바꿔도 텍스트층 글자 수는 그대로여야 한다."""
    page = pdfium.PdfDocument(str(PDF))[0]
    tp = page.get_textpage()
    src = "".join(
        c for i in range(tp.count_chars())
        if (c := tp.get_text_range(i, 1)) and not c.isspace()
    )
    out = "".join("".join(ln.text.split()) for ln in extract_digital_lines(page, PX_PER_PT))
    assert sorted(out) == sorted(src)


@pytest.mark.skipif(not ARC_PDF.exists(), reason="샘플 PDF 없음")
def test_아치형_제목이_한_줄로_이어진다():
    """곡선 제목은 글자마다 베이스라인이 다르다(폭 7pt). 이웃끼리 연쇄로 이어야 한다."""
    assert "NH비대면전용입출식및적립식" in _texts(ARC_PDF)


def test_런이_없는_페이지는_종전_방식으로_되돌아간다(monkeypatch):
    """조용한 실패 금지 — 구조를 못 읽어도 텍스트는 다 나와야 한다."""
    monkeypatch.setattr("nh_parsing.triage._text_runs", lambda page: [])
    page = pdfium.PdfDocument(str(PDF))[0]
    tp = page.get_textpage()
    src = "".join(
        c for i in range(tp.count_chars())
        if (c := tp.get_text_range(i, 1)) and not c.isspace()
    )
    lines = extract_digital_lines(page, PX_PER_PT)
    assert lines, "폴백이 아무것도 못 냈다"
    assert sorted("".join("".join(ln.text.split()) for ln in lines)) == sorted(src)


def test_런과_구분선을_실제로_읽는다():
    page = pdfium.PdfDocument(str(PDF))[0]
    runs = _text_runs(page)
    assert len(runs) == 126          # 폭·높이 0 인 빈 런을 뺀 수
    assert all(r["size"] > 0 for r in runs)
    assert all("baseline" in r for r in runs)
