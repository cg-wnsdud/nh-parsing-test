# -*- coding: utf-8 -*-
"""벡터로 그린 글자를 라우팅이 못 세던 사각 — **경고만** 남기는지 고정한다.

이 테스트의 절반은 "바뀌지 않았음"을 지킨다. B4c 는 진단 정보만 늘리는 항목이고
판정(structured/scan_like/hybrid)은 그대로여야 한다. 임계를 만들면 샘플 1건 과적합이다.

실물 PDF 를 쓴다 — pypdfium2 오브젝트 열거를 목으로 흉내 내면 정작 재려던 것
(FPDF_PAGEOBJ_PATH 를 실제로 세는가)을 안 재게 된다.
"""

from pathlib import Path

import pypdfium2 as pdfium
import pytest

from nh_parsing.triage import triage_page

SAMPLES = Path(__file__).resolve().parents[1] / "nh-data" / "sample-data"
PDF_003 = SAMPLES / "NH농협은행-2026_003-예금성.pdf"
PDF_001 = SAMPLES / "NH농협은행-2026_001-예금성.pdf"

pytestmark = pytest.mark.skipif(not PDF_003.exists(), reason="샘플 PDF 없음(비공개)")


def _triage(pdf_path: Path, index: int):
    pdf = pdfium.PdfDocument(str(pdf_path))
    return triage_page(pdf[index])


def test_벡터_글자_페이지에_경고가_남는다():
    """003 p3 — 헤더 8낱말이 PATH 로 그려져 두 신호 어디에도 안 잡히던 페이지."""
    t = _triage(PDF_003, 2)
    assert t.image_area_ratio == 0.0     # 이미지 오브젝트가 하나도 없다
    assert t.path_count == 11
    assert t.path_area_ratio > 1.0       # 배경+패널 중첩 — 자르지 않는다
    assert t.reasons, "reasons 가 빈 배열이면 '확인할 게 없다'로 읽힌다"
    assert any("벡터 도형" in r for r in t.reasons)


def test_판정은_그대로_structured_다():
    """★ 여기가 핵심 — 경고를 넣었다고 라우팅이 바뀌면 안 된다."""
    assert _triage(PDF_003, 2).verdict == "structured"


def test_OCR이_이미_도는_페이지엔_경고를_안_남긴다():
    """scan_like 는 어차피 OCR 이 돌아 벡터 글자가 유실되지 않는다 — 노이즈만 된다.

    003 p1·p2 도 PATH 를 14개씩 가지고 있지만 텍스트 레이어가 비어 scan_like 다.
    """
    for idx in (0, 1):
        t = _triage(PDF_003, idx)
        assert t.verdict == "scan_like"
        assert not any("벡터 도형" in r for r in t.reasons), f"p{idx + 1} 에 불필요한 경고"


def test_이미지_한_장짜리_페이지는_영향_없다():
    """001 p1 — IMAGE=1(100%), PATH 없음. 기존 동작 그대로."""
    t = _triage(PDF_001, 0)
    assert t.path_count == 0
    assert t.image_area_ratio == 1.0
    assert not any("벡터 도형" in r for r in t.reasons)


def test_as_dict_에_새_필드가_실린다():
    """out/json 감사 추적에 남아야 나중에 표본을 모을 수 있다."""
    d = _triage(PDF_003, 2).as_dict()
    assert d["path_count"] == 11
    assert isinstance(d["path_area_ratio"], float)
