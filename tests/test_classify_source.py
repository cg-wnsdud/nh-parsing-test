# -*- coding: utf-8 -*-
"""category_source 가 세 가지 '파일명으로 정했다'를 가르는지 고정한다.

셋은 신뢰도가 완전히 다르다 — VLM 이 반대했다 / VLM 이 죽었다 / VLM 을 안 불렀다.
한 값("filename")으로 적으면 산출물만 보고 가를 수 없고, §9-③ 의 '규칙폴백 2건 중
1건은 안 부른 것' 과 같은 착오가 반복된다.
"""

import pytest
from PIL import Image

from nh_parsing import gemma_client
from nh_parsing.gemma_client import classify


@pytest.fixture
def canvas() -> Image.Image:
    return Image.new("RGB", (64, 64), "white")


def _stub(monkeypatch, payload: dict | None = None, exc: Exception | None = None):
    def fake(*a, **kw):
        if exc is not None:
            raise exc
        return payload
    monkeypatch.setattr(gemma_client, "chat_json", fake)


def test_VLM이_판단불가로_답하면_abstained(monkeypatch, canvas):
    """003 실측 재현 — VLM 이 확신도 0.9 로 '서비스 이벤트'라 답했고 파일명이 덮었다."""
    _stub(monkeypatch, {
        "product_group": "기타",
        "ad_type": "이벤트페이지",
        "confidence": 0.9,
        "reason": "'올원모임' 서비스 이용 활성화를 위한 경품 증정 이벤트다",
    })
    r = classify(canvas, "NH농협은행-2026_003-예금성.pdf")
    assert r.category_source == "filename_vlm_abstained"
    assert r.product_group == "예금성"          # 판정 자체는 그대로 — prior 적용
    assert r.confidence == 0.9
    # VLM 이 무엇을 근거로 반대했는지가 사유에 남아야 한다
    assert "기타" in r.reason and "올원모임" in r.reason


def test_VLM_호출이_실패하면_failed(monkeypatch, canvas):
    _stub(monkeypatch, exc=RuntimeError("connection refused"))
    r = classify(canvas, "NH농협은행-2026_003-예금성.pdf")
    assert r.category_source == "filename_vlm_failed"
    assert r.product_group == "예금성"
    assert r.confidence is None                 # 관측이 없으므로 확신도도 없다


def test_HWP는_VLM을_안_부르므로_no_vlm():
    """캔버스가 없어 분류 VLM 을 애초에 호출하지 않는 경로 (hwp_ingest)."""
    import inspect

    from nh_parsing import hwp_ingest

    src = inspect.getsource(hwp_ingest.ingest_hwp)
    assert '"filename_no_vlm"' in src, "HWP 경로가 다시 'filename' 으로 뭉쳤다"


def test_합의와_충돌은_그대로(monkeypatch, canvas):
    """B4a 는 값을 세분화만 한다 — 기존 두 값의 동작은 안 바뀐다."""
    _stub(monkeypatch, {"product_group": "예금성", "ad_type": "상세페이지",
                        "confidence": 1.0, "reason": "적금 상세페이지"})
    assert classify(canvas, "..._001-예금성.pdf").category_source == "filename_and_vlm"

    _stub(monkeypatch, {"product_group": "대출성", "ad_type": "안내장",
                        "confidence": 0.8, "reason": "신용대출 안내장"})
    r = classify(canvas, "..._001-예금성.pdf")
    assert r.category_source == "vlm_overrode_filename"
    assert r.product_group == "대출성"           # 충돌 시 VLM 채택 — 기존 동작 유지


def test_파일명_힌트가_없고_VLM도_판단불가면_prior가_안_생긴다(monkeypatch, canvas):
    _stub(monkeypatch, {"product_group": "판단불가", "ad_type": "기타",
                        "confidence": 0.3, "reason": "무엇을 광고하는지 불명"})
    r = classify(canvas, "무제.png")
    assert r.category_source == "vlm_abstained"
    assert r.product_group is None
