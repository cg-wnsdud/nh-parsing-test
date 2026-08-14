# -*- coding: utf-8 -*-
"""글자를 줄로 묶을 때 잉크칸을 써서 라벨·숫자가 갈라지던 결함 — extract_digital_lines.

잉크칸(tight charbox)은 글자 모양대로라 같은 줄에서도 높이가 제각각이다(실측
2026-08-14, `7. 대출성상품.pdf` p1: `ㆍ` 5.3px · `임` 25.8px · `필` 28.7px). 겹침
비율을 이 상자로 재면 **글자 모양이 줄 소속을 정한다** — 키 큰 `필`(28.7)이 윗줄
`임`(25.8)과 51.6% 겹쳐 윗줄에 흡수되고, 형제 `류`·`요`는 46~49%로 탈락해 따로
묶였다. 결과가 `필요서류` → `필서` + `요류`.

같은 원인이 소수점·쉼표를 통째로 떼어냈다. 마침표는 잉크칸이 아주 낮아 본문 줄과의
겹침이 임계값에 못 미쳐 **자기들끼리 별도 줄**이 됐다 — 실측 산출물(out_new)에
`기본금리연31%(20250605기준세전)` 이 그대로 실려 있었다. 원본은
`기본금리 연 3.1% (2025.06.05. 기준, 세전)` — **연 3.1% 가 연 31% 로 기록됐다.**

글꼴칸(loose charbox)은 글꼴 메트릭이 정하므로 같은 줄 글자가 전부 같은 상자가 된다
(`필요서류` 넷 다 1916.3~1948.9). 그래서 줄 묶기만 글꼴칸으로 바꿨다. 전수 대조
(53쪽): 사라진 줄 330개 중 141개가 순수 구두점, 새로 생긴 줄 125개 중 37개가
소수점 금리·날짜를 담은 줄이었다.
"""

import re
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from nh_parsing.triage import extract_digital_lines

SAMPLES = Path(__file__).resolve().parents[1] / "nh-data" / "new-sample-data"
PDF_LOAN7 = SAMPLES / "7. 대출성상품.pdf"
PDF_LOAN6 = SAMPLES / "6. 대출성상품.pdf"
PDF_DEPOSIT = SAMPLES / "1. 예금성상품(입출식·적립식 통합).pdf"

PX_PER_PT = 200 / 72.0  # 텍스트 결과는 이 값과 무관(좌표만 환산) — 기존 테스트와 맞춤
_PUNCT_ONLY = re.compile(r"[.,·:%-]+")


def _texts(path: Path) -> list[str]:
    return [l.text for l in extract_digital_lines(pdfium.PdfDocument(str(path))[0], PX_PER_PT)]


@pytest.mark.skipif(not PDF_LOAN7.exists(), reason="샘플 PDF 없음(비공개)")
def test_라벨이_옆줄에_뜯기지_않는다():
    """`필요서류` → `필서`+`요류`, `대출한도` → `대출한`+`도` 로 갈리던 회귀 고정."""
    texts = _texts(PDF_LOAN7)
    assert "필요서류" in texts
    assert "대출한도" in texts
    for broken in ("필서", "요류", "대출한"):
        assert broken not in texts, f"라벨이 다시 갈렸다: {broken}"


@pytest.mark.skipif(not PDF_LOAN6.exists(), reason="샘플 PDF 없음(비공개)")
def test_두_글자_라벨도_붙어_나온다():
    texts = _texts(PDF_LOAN6)
    assert "대출용도" in texts
    assert "대출용" not in texts


@pytest.mark.skipif(not PDF_DEPOSIT.exists(), reason="샘플 PDF 없음(비공개)")
def test_소수점이_금리에서_떨어져_나가지_않는다():
    """연 3.1% 가 연 31% 로 기록되던 회귀 고정 — 심의에서 금리는 핵심 판정 대상이다."""
    texts = _texts(PDF_DEPOSIT)
    assert "기본금리연3.1%(2025.06.05.기준,세전)" in texts
    assert "기본금리연31%(20250605기준세전)" not in texts


@pytest.mark.skipif(not PDF_DEPOSIT.exists(), reason="샘플 PDF 없음(비공개)")
@pytest.mark.parametrize("pdf_path", [PDF_LOAN7, PDF_LOAN6, PDF_DEPOSIT])
def test_구두점만_있는_줄이_생기지_않는다(pdf_path: Path):
    """구두점만인 줄은 곧 그 구두점을 잃은 다른 줄이 있다는 뜻이다(원인 아닌 증상 고정)."""
    if not pdf_path.exists():
        pytest.skip("샘플 PDF 없음(비공개)")
    orphans = [t for t in _texts(pdf_path) if _PUNCT_ONLY.fullmatch(t)]
    assert orphans == [], f"구두점이 본문에서 떨어져 나왔다: {orphans[:5]}"
