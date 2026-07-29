# -*- coding: utf-8 -*-
"""VLM 응답 캐시 회귀 테스트 — A/B 를 결정론적으로 만드는 장치라 동작이 정확해야 한다."""

import importlib

import pytest


def _fresh(monkeypatch, mode: str, tmp_path):
    """환경변수를 바꾼 상태로 모듈을 다시 읽어들인다 (모드는 import 시점에 결정)."""
    monkeypatch.setenv("VLM_CACHE", mode)
    monkeypatch.setenv("VLM_CACHE_DIR", str(tmp_path / "cache"))
    import nh_parsing.vlm_cache as vc
    importlib.reload(vc)
    import nh_parsing.gemma_client as gc
    importlib.reload(gc)
    return vc, gc


def test_disabled_by_default(monkeypatch, tmp_path):
    """운영 기본은 캐시 off — 켜져 있으면 옛 판독을 실서비스에 재생하게 된다."""
    monkeypatch.delenv("VLM_CACHE", raising=False)
    monkeypatch.setenv("VLM_CACHE_DIR", str(tmp_path / "cache"))
    import nh_parsing.vlm_cache as vc
    importlib.reload(vc)
    assert vc.enabled() is False


def test_record_then_replay_returns_same_without_calling(monkeypatch, tmp_path):
    """기록 모드로 한 번 부르면, 이후 같은 호출은 서버에 안 가고 같은 값을 준다."""
    vc, gc = _fresh(monkeypatch, "r", tmp_path)
    calls = []

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '{"analysis":"a","v":1}'}}]}

    def fake_post(url, **kw):
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr(gc.requests, "post", fake_post)
    parts = [{"type": "text", "text": "hello"}]
    schema = {"type": "object"}

    first = gc.chat_json(parts, "t", schema, max_tokens=10)
    assert first == {"analysis": "a", "v": 1}
    assert len(calls) == 1

    second = gc.chat_json(parts, "t", schema, max_tokens=10)
    assert second == first
    assert len(calls) == 1, "캐시 적중인데 서버를 다시 호출했다"


def test_replay_mode_fails_loudly_on_miss(monkeypatch, tmp_path):
    """재생 모드에서 캐시에 없으면 조용히 호출하지 않고 실패한다 (결정론 보장)."""
    vc, gc = _fresh(monkeypatch, "p", tmp_path)
    monkeypatch.setattr(gc.requests, "post",
                        lambda *a, **k: pytest.fail("replay 모드인데 서버를 호출했다"))
    with pytest.raises(RuntimeError, match="캐시 미스"):
        gc.chat_json([{"type": "text", "text": "x"}], "t", {"type": "object"}, max_tokens=10)


def test_key_distinguishes_image_content(monkeypatch, tmp_path):
    """이미지가 1px 이라도 다르면 다른 호출로 본다 — 밴드 크롭이 섞이면 안 된다."""
    vc, _ = _fresh(monkeypatch, "r", tmp_path)
    schema = {"type": "object"}
    def k(url):
        return vc.key_for([{"type": "image_url", "image_url": {"url": url}}],
                          "t", schema, 10, "m")
    assert k("data:image/jpeg;base64,AAAA") != k("data:image/jpeg;base64,AAAB")
    assert k("data:image/jpeg;base64,AAAA") == k("data:image/jpeg;base64,AAAA")
