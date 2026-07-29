# -*- coding: utf-8 -*-
"""누락 스윕 밴드 분할 회귀 테스트.

VLM 서버가 pan-and-scan 없이 고정 예산으로 리샘플링하므로(2026-07-27 실측), 세로로 긴
캔버스를 통짜로 보내면 작은 글씨가 소실된다. 밴드로 잘라 보내고 좌표를 원본으로
되돌리는 부분을 고정한다. VLM 호출은 monkeypatch 로 대체해 폐쇄망 없이 돈다.
"""

import nh_parsing.vlm_direct as vd
import pytest
from nh_parsing.config import SETTINGS
from nh_parsing.ir import Line
from PIL import Image


def test_landscape_canvas_is_not_split():
    """가로 슬라이드(003류)는 이미 읽을 수 있는 비율이라 분할하지 않는다."""
    bands = vd._sweep_bands(Image.new("RGB", (1990, 1120)))
    assert len(bands) == 1
    assert bands[0][1] == 0


def test_tall_canvas_is_split_with_overlap_and_full_cover():
    """세로 초장신은 겹치며 잘리고 빈틈 없이 전체를 덮는다.

    밴드 '개수'는 폭 기준 비율이 정하고(고정 토큰 예산), '위치'는 글자 밀도가 정한다.
    글자가 없는 캔버스에서는 밀도가 판단 근거가 없으므로 균등 간격으로 되돌아간다.
    """
    from nh_parsing.bands import band_count_for, vlm_band_span

    w, h = 720, 6554
    canvas = Image.new("RGB", (w, h))
    bands = vd._sweep_bands(canvas)

    assert len(bands) == band_count_for(canvas, vlm_band_span(canvas))
    # 이웃 밴드는 overlap 만큼 겹친다 (경계에서 글자가 반토막 나는 것 방지)
    for (img_a, y_a), (_, y_b) in zip(bands, bands[1:]):
        assert y_b == y_a + img_a.height - SETTINGS.tile_overlap_px
    # 마지막 밴드가 캔버스 끝까지 닿아 빠지는 구간이 없다
    last_img, last_y = bands[-1]
    assert last_y + last_img.height == h


def _first_cut(canvas, obstacles=None) -> int:
    bands = vd._sweep_bands(canvas, obstacles)
    return bands[0][1] + bands[0][0].height


def test_cut_snaps_away_from_text():
    """절단선이 글자 한가운데를 지나면 빈 구간으로 당겨진다 (밴드 경계 글자 절단 방지).

    OCR 라인 좌표는 픽셀 밀도와 보는 것이 다르다 — 밀도가 '깨끗하다'고 본 자리라도
    검출된 라인 상자를 가로지르면 옮겨야 한다. 두 검사가 모두 걸려 있는지 확인한다.
    """
    w, h = 720, 6554
    canvas = Image.new("RGB", (w, h))
    plain = _first_cut(canvas)                       # 장애물 없을 때의 컷
    victim = [100, plain - 20, 600, plain + 20]      # 그 컷을 가로지르는 글자를 심는다

    cut = _first_cut(canvas, [victim])
    assert not (victim[1] < cut < victim[3]), "절단선이 여전히 글자를 가로지름"


def test_cut_keeps_original_when_no_gap_exists():
    """빈틈이 없으면 원래 위치를 쓴다 — 무한정 밀려나 밴드가 뭉개지지 않게."""
    w, h = 720, 6554
    canvas = Image.new("RGB", (w, h))
    plain = _first_cut(canvas)
    # 탐색 범위 전체를 덮는 장애물 → 피할 곳이 없음
    wall = [0, plain - SETTINGS.vlm_band_snap_px - 50,
            w, plain + SETTINGS.vlm_band_snap_px + 50]

    assert _first_cut(canvas, [wall]) == plain, "피할 곳이 없으면 원래 자리를 지켜야 한다"


def test_sweep_maps_band_ratio_back_to_canvas_coords(monkeypatch):
    """밴드 안의 y_ratio 가 원본 캔버스 절대좌표로 복원된다 (밴드 오프셋 가산)."""
    w, h = 720, 6554
    calls: list[int] = []

    def fake_chat_json(parts, **kwargs):
        # 밴드 순서대로 서로 다른 문구를 하나씩 돌려준다
        idx = len(calls)
        calls.append(idx)
        return {"analysis": "", "missing": [
            {"text": f"밴드{idx}에만 보이는 문구", "y_ratio": 0.5, "confidence": 0.9},
        ]}

    monkeypatch.setattr(vd, "chat_json", fake_chat_json)
    lines, notes = vd.sweep_missing_lines([], Image.new("RGB", (w, h)), w, h)

    bands = vd._sweep_bands(Image.new("RGB", (w, h)))
    assert len(lines) == len(bands)
    for line, (img, y_off) in zip(lines, bands):
        cy = (line.bbox[1] + line.bbox[3]) / 2
        assert cy == pytest.approx(y_off + img.height * 0.5, abs=2)
        assert line.source == "vlm_sweep"
    # 밴드 기준이라 bbox 가 통짜(캔버스 1.2%=157px)보다 좁아진다
    assert (lines[0].bbox[3] - lines[0].bbox[1]) < int(h * 0.012) * 2


def test_one_failed_band_keeps_other_bands(monkeypatch):
    """밴드 하나가 죽어도 나머지 회수분은 살리고, 실패 사실을 노트로 남긴다."""
    w, h = 720, 6554
    seen: list[int] = []

    def flaky(parts, **kwargs):
        idx = len(seen)
        seen.append(idx)
        if idx == 1:
            raise RuntimeError("Unterminated string")
        return {"analysis": "", "missing": [
            {"text": f"문구{idx}", "y_ratio": 0.2, "confidence": 0.9},
        ]}

    monkeypatch.setattr(vd, "chat_json", flaky)
    lines, notes = vd.sweep_missing_lines([], Image.new("RGB", (w, h)), w, h)

    assert len(lines) == len(seen) - 1          # 실패한 밴드 하나만 빠짐
    assert any("스윕 밴드 실패" in n for n in notes)  # 조용한 실패 금지
