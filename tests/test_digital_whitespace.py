# -*- coding: utf-8 -*-
"""PDF 텍스트 레이어의 일반 공백을 정본에 보존하는 회귀 시험."""

from pathlib import Path

import pypdfium2 as pdfium
import pytest

from nh_parsing.triage import extract_digital_lines


PDF = Path("nh-data/new-sample-data/12. 대출성상품.pdf")
PX_PER_PT = 200 / 72.0


@pytest.mark.skipif(not PDF.exists(), reason="샘플 PDF 없음")
def test_pdfium_텍스트_레이어의_띄어쓰기를_보존한다():
    lines = extract_digital_lines(pdfium.PdfDocument(str(PDF))[0], PX_PER_PT)
    texts = [line.text for line in lines]
    assert "● 공무원연금공단에서 확인된 재직기간이 3개월 이상인 공무원" in texts
    assert "●공무원연금공단에서확인된재직기간이3개월이상인공무원" not in texts
