# -*- coding: utf-8 -*-
"""시인성(글자 크기·굵기·색) 부착 — 심의 항목 DEP/LOAN-FMT-01 의 유일한 재료.

여기서 지키는 것은 **값을 담는다**까지다. "몇 pt 부터 위반인가"는 규정이 숫자를 주지
않으므로 판정하지 않는다(text_style 모듈 주석).
"""

from pathlib import Path

import pypdfium2 as pdfium
import pytest

from nh_parsing.text_style import fold, rgb_hex, strip_subset_prefix
from nh_parsing.triage import extract_digital_lines

PDF = Path("nh-data/new-sample-data/1. 예금성상품(적립식).pdf")
PX_PER_PT = 200 / 72.0


def test_대표값은_평균이_아니라_글자수_최다다():
    """평균을 쓰면 실제로 존재하지 않는 크기가 나온다 — 35pt 한 글자 + 8pt 서른 글자."""
    atoms = [(35.0, True, "#FF0000", "Big")] + [(8.0, False, "#000000", "Small")] * 30
    style = fold(atoms, basis="declared", source="hwp_run")
    assert style.size_pt == 8.0        # 평균(8.87)이 아니다
    assert style.size_pt_max == 35.0   # 격차는 min/max 로 읽는다
    assert style.size_pt_min == 8.0


def test_굵기는_다수결이_아니라_하나라도_있으면_참이다():
    """금리 숫자만 굵은 흔한 배치에서 다수결은 False 가 되어 강조를 놓친다."""
    atoms = [(24.0, True, None, None)] + [(8.0, False, None, None)] * 20
    assert fold(atoms, basis="declared", source="hwp_run").bold is True


def test_스타일을_하나도_못_얻으면_None():
    assert fold([], basis="declared", source="hwp_run") is None
    assert fold([(None, None, None, None)], basis="declared", source="hwp_run") is None


def test_서브셋_글꼴_접두어를_뗀다():
    """접두어는 문서마다 무작위라 붙여 두면 문서 간 집계가 안 된다."""
    assert strip_subset_prefix("TRDBGG+OTMGothicL") == "OTMGothicL"
    assert strip_subset_prefix("OTMGothicL") == "OTMGothicL"       # 접두어 없는 경우
    assert strip_subset_prefix("ABC+X") == "ABC+X"                 # 6글자가 아니면 안 뗀다


def test_색은_대문자_16진수다():
    assert rgb_hex(1, 102, 179) == "#0166B3"


@pytest.mark.skipif(not PDF.exists(), reason="샘플 PDF 없음")
def test_PDF_라인마다_시인성이_붙는다():
    lines = extract_digital_lines(pdfium.PdfDocument(str(PDF))[0], PX_PER_PT)
    assert lines
    styled = [ln for ln in lines if ln.style and ln.style.size_pt]
    # 디지털 텍스트 라인은 전부 크기를 얻을 수 있어야 한다 (글꼴칸 높이는 항상 있다)
    assert len(styled) == len(lines)
    assert all(ln.style.style_source == "pdf_char" for ln in styled)
    assert all(ln.style.size_basis == "fontbox" for ln in styled)


@pytest.mark.skipif(not PDF.exists(), reason="샘플 PDF 없음")
def test_PDF_금리는_크고_심의필은_작다():
    """FMT-01 판정에 쓸 격차가 실제로 재어지는지 — 실측(2026-08-21): 24.1pt 대 7.8pt."""
    lines = extract_digital_lines(pdfium.PdfDocument(str(PDF))[0], PX_PER_PT)
    rate = [ln for ln in lines if "2.25%" in ln.text]
    stamp = [ln for ln in lines if "심의필" in ln.text]
    assert rate and stamp, "기준 문구를 못 찾았다 — 샘플이 바뀌었는지 확인"
    assert rate[0].style.size_pt > stamp[0].style.size_pt * 2
    assert rate[0].style.bold is True
    assert stamp[0].style.bold is False


@pytest.mark.skipif(not PDF.exists(), reason="샘플 PDF 없음")
def test_명목_글자크기는_쓰지_않는다():
    """`FPDFText_GetFontSize` 가 이 문서군에서 전부 1.0pt 를 돌려준다(크기를 행렬로 줌).

    그 값을 쓰면 모든 라인이 같은 크기가 되어 FMT-01 판정이 불가능해진다. 글꼴칸 높이를
    쓰고 있는지 확인한다 — 라인 간 크기가 실제로 갈려야 한다.
    """
    lines = extract_digital_lines(pdfium.PdfDocument(str(PDF))[0], PX_PER_PT)
    sizes = {ln.style.size_pt for ln in lines if ln.style and ln.style.size_pt}
    assert len(sizes) > 3, f"크기가 갈리지 않는다 — 명목값을 쓰고 있는지 확인: {sizes}"
    assert 1.0 not in sizes


HWP = Path("nh-data/sample-data-hwp/NH농협은행-2026_004-대출성.hwp")


@pytest.mark.skipif(not HWP.exists(), reason="샘플 HWP 없음")
def test_HWP_표_행에도_시인성이_붙는다():
    """광고 HWP 는 표가 본체다 — 실측: run 141개 중 138개가 표 안이다.

    표 행에 못 붙이면 사실상 아무것도 못 붙는다. Java(JVM) 가 없으면 사내 파서가
    실패하는데 그때는 unreadable 로 떨어지므로 스킵한다(조용한 통과 금지).
    """
    from nh_parsing.hwp_ingest import ingest_hwp

    doc = ingest_hwp(HWP)
    page = doc.pages[0]
    if page.parse_status == "unreadable":
        pytest.skip("사내 파서 실패(Java 미가용) — 시인성 검증 불가")
    lines = page.regions[0].lines
    assert lines
    styled = [ln for ln in lines if ln.style and ln.style.size_pt]
    assert len(styled) == len(lines), "일부 라인에 시인성이 안 붙었다"
    assert all(ln.style.size_basis == "declared" for ln in styled)
    assert all(ln.style.style_source == "hwp_run" for ln in styled)
    # 헤드라인과 심의필 문구의 격차가 재어져야 한다 (실측 35pt 대 8.5pt)
    head = [ln for ln in lines if "공무원" in ln.text]
    stamp = [ln for ln in lines if "심의필" in ln.text]
    assert head and stamp
    assert head[0].style.size_pt_max > stamp[0].style.size_pt_max * 2
