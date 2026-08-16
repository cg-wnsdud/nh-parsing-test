# -*- coding: utf-8 -*-
"""응답이 중간에 끊긴 밴드 통독을 반으로 쪼개 되살린다 (vlm_direct.read_band_regions).

실측 근거(2026-08-14, 89문서 재실행): 서버가 정상 속도(호출당 8.5~18.6초)로 돌아온
뒤에도 밴드 통독 실패 11건이 남았고 **전부 `Unterminated string`**(응답이 도중에
끊김) 한 유형이었다. 원인은 서버측 guided-decoding 결함이라 우리가 못 고치지만,
한 번에 요구하는 출력량을 줄이면 잘리기 전에 끝날 확률이 올라간다.

max_tokens 를 올리는 방식은 안 쓴다 — 지금도 상한 8000 이고 올리면 정상 호출까지
전부 느려진다. 실패한 것만 쪼개는 쪽이 싸고 정확하다.

모델 호출은 monkeypatch — 폐쇄망 불필요.
"""

from __future__ import annotations

import pytest
from PIL import Image

import nh_parsing.vlm_direct as vd


def _entries(n: int) -> list[tuple[str, str]]:
    return [(f"p1_r{i:03d}", f"영역{i} 현재판독") for i in range(n)]


def _reply(rids: list[str]) -> dict:
    return {
        "analysis": "",
        "regions": [{"region_id": r, "text": f"{r} 판독본", "confidence": 0.9} for r in rids],
        "missing": [],
    }


def test_잘린_응답은_반으로_쪼개_다시_묻는다(monkeypatch):
    """★ 핵심 — 통짜 1회는 실패하지만 절반씩 2회는 성공하는 상황."""
    calls: list[int] = []

    def fake(parts, schema_name, schema, max_tokens):
        listing = parts[0]["text"]
        rids = [ln.split("region_id=")[1].split(" ")[0]
                for ln in listing.splitlines() if "region_id=" in ln]
        calls.append(len(rids))
        if len(rids) > 3:
            raise RuntimeError("Unterminated string starting at: line 1 column 192 (char 191)")
        return _reply(rids)

    monkeypatch.setattr(vd, "chat_json", fake)
    readings, missing, dropped = vd.read_band_regions(Image.new("RGB", (100, 100)), _entries(6))

    assert len(readings) == 6, "쪼갠 뒤 6개 영역이 모두 살아야 한다"
    assert calls == [6, 3, 3], f"통짜 1회 실패 후 절반씩 2회여야 한다 (실제 {calls})"
    assert dropped["잘림_분할재시도"] == 1, "재시도 사실이 기록돼야 한다(조용한 복구 금지)"


def test_쪼개도_안_되면_더_쪼갠다(monkeypatch):
    """재귀 — 절반도 잘리면 그 절반을 또 나눈다."""
    def fake(parts, schema_name, schema, max_tokens):
        rids = [ln.split("region_id=")[1].split(" ")[0]
                for ln in parts[0]["text"].splitlines() if "region_id=" in ln]
        if len(rids) > 1:
            raise RuntimeError("Unterminated string")
        return _reply(rids)

    monkeypatch.setattr(vd, "chat_json", fake)
    readings, _, dropped = vd.read_band_regions(Image.new("RGB", (100, 100)), _entries(4))
    assert len(readings) == 4
    assert dropped["잘림_분할재시도"] >= 3


def test_영역이_하나면_더_못_쪼개므로_그대로_올린다(monkeypatch):
    """무한 재귀 방지 — 호출측이 기록할 수 있게 예외를 그대로 전달한다."""
    def fake(*a, **k):
        raise RuntimeError("Unterminated string")

    monkeypatch.setattr(vd, "chat_json", fake)
    with pytest.raises(RuntimeError, match="Unterminated"):
        vd.read_band_regions(Image.new("RGB", (100, 100)), _entries(1))


def test_잘림이_아닌_실패는_재시도하지_않는다(monkeypatch):
    """모델명 오타·서버 다운까지 쪼개면 실패 1건이 2건이 될 뿐이다."""
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        raise RuntimeError("404 model not found: spark-gemma-oops")

    monkeypatch.setattr(vd, "chat_json", fake)
    with pytest.raises(RuntimeError, match="404"):
        vd.read_band_regions(Image.new("RGB", (100, 100)), _entries(8))
    assert calls["n"] == 1, "잘림이 아니면 한 번만 시도해야 한다"


def test_이스케이프_도중에_잘린_것도_잘림으로_본다(monkeypatch):
    """2026-08-14 실측 — 남은 실패 10건이 전부 이 유형이었다.

    잘린 자리가 하필 `\\uXXXX` 한가운데면 파서가 'Unterminated string' 이 아니라
    'Invalid \\uXXXX escape' 를 낸다. 같은 잘림인데 유형 목록에 없어서 분할 재시도를
    못 타고 그대로 실패했다. 응답 원문으로 확인한 꼬리는 장식 기호의 반복 퇴행이었다
    (★ 7건 · ✨ 1건 · ⋅ 1건 · 이모지 변형자 1건, 오류 위치는 전부 응답 끝 1~5자).
    """
    calls: list[int] = []

    def fake(parts, schema_name, schema, max_tokens):
        rids = [ln.split("region_id=")[1].split(" ")[0]
                for ln in parts[0]["text"].splitlines() if "region_id=" in ln]
        calls.append(len(rids))
        if len(rids) > 3:
            raise RuntimeError(
                "VLM 호출 실패(3회): Invalid \\uXXXX escape: line 1 column 2963 (char 2962)"
            )
        return _reply(rids)

    monkeypatch.setattr(vd, "chat_json", fake)
    readings, _, dropped = vd.read_band_regions(Image.new("RGB", (100, 100)), _entries(6))

    assert len(readings) == 6, "이스케이프 잘림도 쪼개서 살려야 한다"
    assert calls == [6, 3, 3], f"통짜 1회 실패 후 절반씩 2회여야 한다 (실제 {calls})"


def test_한_조각이_실패해도_나머지는_건진다(monkeypatch):
    """장식 기호 반복 퇴행은 보통 **특정 영역 하나**에서 터진다.

    예전에는 재귀 호출을 그대로 둬서 그 하나 때문에 같은 밴드의 멀쩡한 영역들까지
    통독 후보를 통째로 잃었다.
    """
    def fake(parts, schema_name, schema, max_tokens):
        rids = [ln.split("region_id=")[1].split(" ")[0]
                for ln in parts[0]["text"].splitlines() if "region_id=" in ln]
        # p1_r000 이 낀 요청은 몇 개로 쪼개든 항상 잘린다 (별 반복 퇴행 재현)
        if "p1_r000" in rids or len(rids) > 2:
            raise RuntimeError("Invalid \\uXXXX escape: line 1 column 900 (char 899)")
        return _reply(rids)

    monkeypatch.setattr(vd, "chat_json", fake)
    readings, _, dropped = vd.read_band_regions(Image.new("RGB", (100, 100)), _entries(4))

    assert "p1_r000" not in readings, "못 읽은 영역은 안 담긴다(호출측이 원값 유지)"
    assert len(readings) >= 2, f"멀쩡한 영역은 살아야 한다 (실제 {sorted(readings)})"
    assert dropped["잘림_조각미채택"] >= 1, "버린 조각이 기록돼야 한다(조용한 실패 금지)"


def test_양쪽_다_실패하면_예외를_올린다(monkeypatch):
    """건진 게 하나도 없으면 성공한 척하지 않는다."""
    def fake(*a, **k):
        raise RuntimeError("Invalid \\uXXXX escape: line 1 column 10 (char 9)")

    monkeypatch.setattr(vd, "chat_json", fake)
    with pytest.raises(RuntimeError, match="Invalid"):
        vd.read_band_regions(Image.new("RGB", (100, 100)), _entries(4))


def test_정상_응답은_경로가_안_바뀐다(monkeypatch):
    """회귀 확인 — 성공하는 호출은 재시도 로직을 안 탄다."""
    calls = {"n": 0}

    def fake(parts, schema_name, schema, max_tokens):
        calls["n"] += 1
        rids = [ln.split("region_id=")[1].split(" ")[0]
                for ln in parts[0]["text"].splitlines() if "region_id=" in ln]
        return _reply(rids)

    monkeypatch.setattr(vd, "chat_json", fake)
    readings, _, dropped = vd.read_band_regions(Image.new("RGB", (100, 100)), _entries(5))
    assert len(readings) == 5 and calls["n"] == 1
    assert "잘림_분할재시도" not in dropped
