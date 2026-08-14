# -*- coding: utf-8 -*-
"""PDF 라우팅 분기 — structured 판정도 레이아웃 검출을 부른다 (pipeline._process_pdf).

예전에는 structured(순수 디지털 텍스트) 판정이면 StructureV3 를 **아예 안 부르고**
세로 간격만 보는 _pseudo_regions 로 영역을 만들었다. 그래서 가로 인식이 없어 좌우
2단 페이지가 한 덩어리로 뭉쳤다 — 실측(2026-08-14, `6. 대출성상품.pdf` p1):
원금및이자상환방법·대출금리·우대금리·채권보전·부대비용·신청채널이 한 영역 27줄로
뭉쳤다.

그 분기가 아낀 것은 StructureV3 호출 하나(단일 타일 3.67초, GPU)뿐이다. VLM(카드
분할·역할 판정·밴드 통독·분류)은 _apply_vlm_judgments 가 route 를 안 가리고 항상
부르므로 라우팅과 무관하게 이미 전부 지불 중이었다.

여기서는 모델을 부르지 않는다 — 레이아웃 검출을 가짜로 바꿔 **부르는지 여부와 분기
결과**만 본다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nh_parsing import pipeline
from nh_parsing.ir import Line
from nh_parsing.paddlex_client import LayoutBlock, PaddleXPageResult

SAMPLES = Path(__file__).resolve().parents[1] / "nh-data" / "new-sample-data"
PDF_STRUCTURED = SAMPLES / "7. 대출성상품.pdf"   # triage 가 structured 로 보는 쪽

pytestmark = pytest.mark.skipif(not PDF_STRUCTURED.exists(), reason="샘플 PDF 없음(비공개)")


@pytest.fixture
def spy(monkeypatch):
    """레이아웃 검출을 가짜로 바꾸고 호출 횟수를 센다. VLM·분류는 통째로 끈다.

    pipeline 이 `from .paddlex_client import request_layout_parsing` 로 이름을 직접
    들고 있으므로 paddlex_client 쪽을 바꿔봐야 소용없다 — pipeline 의 이름을 바꾼다.
    """
    calls: list[object] = []

    def fake(image):
        calls.append(image)
        # 왼쪽 라벨칸 / 오른쪽 내용칸 — 세로 간격만으로는 절대 못 만드는 가로 분할
        return PaddleXPageResult(
            ocr_lines=[],
            blocks=[
                LayoutBlock(label="text", bbox=[300, 500, 460, 2000], score=0.9),
                LayoutBlock(label="text", bbox=[470, 500, 1600, 2000], score=0.9),
            ],
        )

    monkeypatch.setattr(pipeline, "request_layout_parsing", fake)
    monkeypatch.setattr(pipeline, "_apply_vlm_judgments", lambda page, img: None)
    monkeypatch.setattr(pipeline, "_classify_into", lambda doc, img: None)
    return calls


def test_structured_페이지도_레이아웃_검출을_부른다(spy):
    doc = pipeline._process_pdf(PDF_STRUCTURED, preview_dir=None)
    assert doc.pages[0].triage["verdict"] == "structured", "전제가 깨졌다 — 이 쪽은 structured 여야 한다"
    assert spy, "structured 판정에서 레이아웃 검출을 건너뛰었다"


def test_route_는_hybrid_로_통일한다(spy):
    """실제로 두 출처를 병합하므로 '디지털 텍스트만 썼다'는 표시는 이제 사실이 아니다."""
    page = pipeline._process_pdf(PDF_STRUCTURED, preview_dir=None).pages[0]
    assert page.parse_route == "hybrid"


def test_structured_였다는_사실은_기록에_남는다(spy):
    """분기는 없앴지만 3갈래 판정 자체는 진단용으로 살아 있어야 한다."""
    page = pipeline._process_pdf(PDF_STRUCTURED, preview_dir=None).pages[0]
    assert page.triage["verdict"] == "structured"


def test_잉크_커버리지가_근거에_남는다(spy):
    """H(4efb092)가 찾아낸 진단 신호 — 라우팅은 안 가르지만 기록은 유지한다."""
    page = pipeline._process_pdf(PDF_STRUCTURED, preview_dir=None).pages[0]
    assert any("덮은 비율" in r for r in page.triage["reasons"])


def test_영역이_레이아웃_블록에서_만들어진다(spy):
    """_pseudo_regions 폴백이 아니라 블록 기반 배정을 타야 한다.

    가짜 블록은 좌우 2칸이다. 세로 간격만 보는 폴백은 이 경계를 만들 수 없으므로,
    라벨칸 폭(x<470) 안에만 있는 영역이 생겼다면 블록을 실제로 썼다는 뜻이다.
    """
    page = pipeline._process_pdf(PDF_STRUCTURED, preview_dir=None).pages[0]
    label_column = [r for r in page.regions if r.bbox and r.bbox[2] <= 470]
    assert label_column, "가로 분할이 반영되지 않았다 — 폴백을 탄 것으로 보인다"


def test_디지털_텍스트가_정본으로_남는다(spy):
    """구조만 OCR 에서 받고 값은 디지털이 정본 — 라벨이 그대로 있어야 한다."""
    page = pipeline._process_pdf(PDF_STRUCTURED, preview_dir=None).pages[0]
    texts = [l.text for r in page.regions for l in r.lines] + [
        l.text for l in page.unassigned_lines
    ]
    assert "필요서류" in texts
    assert all(l.source == "digital" for r in page.regions for l in r.lines if l.text == "필요서류")


def test_레이아웃_검출이_실패해도_페이지를_잃지_않는다(monkeypatch):
    """모델이 죽어도 오늘 동작(_pseudo_regions)까지만 내려가고 텍스트는 남아야 한다."""
    def boom(_image):
        raise RuntimeError("PaddleX 죽음")

    monkeypatch.setattr(pipeline, "request_layout_parsing", boom)
    monkeypatch.setattr(pipeline, "_apply_vlm_judgments", lambda page, img: None)
    monkeypatch.setattr(pipeline, "_classify_into", lambda doc, img: None)

    page = pipeline._process_pdf(PDF_STRUCTURED, preview_dir=None).pages[0]
    texts = [l.text for r in page.regions for l in r.lines]
    assert "필요서류" in texts, "레이아웃 실패가 디지털 텍스트까지 날렸다"
    assert page.parse_status == "partial"
    assert any("OCR 타일 실패" in n for n in page.notes), "실패가 조용히 묻혔다"
