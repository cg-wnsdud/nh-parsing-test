# -*- coding: utf-8 -*-
"""밴드 통합판독 크롭이 옆 컬럼까지 보여주던 결함(§D) — _merged_band_read.

밴드 크롭은 원래 밴드가 소유한 모든 영역을 한 번에 담으려 항상 캔버스 전체 폭을
썼다. 좌우로 뚜렷이 떨어진 컬럼(카드 비교, 2단 표)이 같은 밴드에 걸리면, 왼쪽
영역을 다시 읽으라고 시켜도 사진에 오른쪽 내용까지 다 보였다 — 실측(올원
p1_r006): '기존 판독' 힌트에 이미 옆 칸 내용이 섞여 있어 재판독이 오염을 고치기는
커녕 재확인/강화했다. 컬럼 그룹별로 크롭을 나누되, 단일 컬럼 페이지의 기존 동작
(밴드당 1회, 전폭 크롭)은 그대로 유지되는지도 함께 고정한다.

캔버스를 좌(빨강)/우(파랑) 반반으로 칠해 두고, 크롭에서 반대쪽 색이 보이면 옆
컬럼이 침범한 것으로 잡는다 — bbox 숫자만 비교하는 것보다 실제로 "무엇이 보이는가"
를 재는 쪽이 이 결함의 성격(재판독 시야 오염)에 더 가깝다.
"""

import collections

from PIL import Image

from nh_parsing import pipeline, vlm_direct
from nh_parsing.ir import AdPage, Line, Region

RED, BLUE = (255, 0, 0), (0, 0, 255)


def _half_painted_canvas(w=2000, h=1000) -> Image.Image:
    img = Image.new("RGB", (w, h), RED)
    img.paste(Image.new("RGB", (w // 2, h), BLUE), (w // 2, 0))
    return img


def _region(rid: str, bbox: list[int], text: str) -> Region:
    return Region(region_id=rid, bbox=bbox, role="본문", lines=[Line(text=text, bbox=bbox, source="ocr")])


def _page(regions: list[Region], w=2000, h=1000) -> AdPage:
    return AdPage(page_no=1, canvas_w=w, canvas_h=h, parse_route="hybrid", regions=regions)


def _crop_colors(crop: Image.Image) -> set:
    """크롭 네 귀퉁이 색을 본다 — 반대쪽 색이 하나라도 섞였으면 침범이다."""
    w, h = crop.size
    pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    return {crop.getpixel(p) for p in pts}


def _fake_reader(calls: list[dict]):
    def fake(crop, entries):
        calls.append({"width": crop.width, "colors": _crop_colors(crop), "entries": entries})
        readings = {rid: (f"판독-{rid}", 0.9) for rid, _ in entries}
        return readings, [], collections.Counter()
    return fake


def test_단일_컬럼은_기존처럼_전폭_크롭_한_번이다(monkeypatch):
    """회귀 방지 — 컬럼이 하나뿐이면 밴드당 1회 호출·전폭 크롭이 그대로여야 한다."""
    page = _page([
        _region("p1_r0", [100, 100, 500, 200], "본문 A"),
        _region("p1_r1", [120, 220, 520, 320], "본문 B"),
    ])
    calls: list[dict] = []
    monkeypatch.setattr(vlm_direct, "read_band_regions", _fake_reader(calls))

    pipeline._merged_band_read(page, _half_painted_canvas(), [])

    assert len(calls) == 1, f"컬럼이 하나면 밴드당 1회여야 한다 (실제 {len(calls)}회)"
    assert calls[0]["width"] == 2000, "단일 컬럼은 전폭 크롭이어야 한다(회귀 방지)"
    assert {"p1_r0", "p1_r1"} == {rid for rid, _ in calls[0]["entries"]}


def test_좌우로_떨어진_두_컬럼은_따로_크롭되고_서로_안_보인다(monkeypatch):
    """실측 재현 — 왼쪽(빨강 영역) 크롭엔 파랑이, 오른쪽(파랑 영역) 크롭엔 빨강이 없어야 한다."""
    left = _region("p1_r0", [100, 100, 900, 900], "왼쪽 상품")     # 빨강 쪽(x<1000)
    right = _region("p1_r1", [1100, 100, 1900, 900], "오른쪽 상품")  # 파랑 쪽(x>=1000)
    page = _page([left, right])
    calls: list[dict] = []
    monkeypatch.setattr(vlm_direct, "read_band_regions", _fake_reader(calls))

    pipeline._merged_band_read(page, _half_painted_canvas(), [])

    assert len(calls) == 2, f"떨어진 두 컬럼은 따로 호출돼야 한다 (실제 {len(calls)}회)"
    by_entries = {tuple(rid for rid, _ in c["entries"]): c for c in calls}
    left_call = by_entries[("p1_r0",)]
    right_call = by_entries[("p1_r1",)]
    assert left_call["width"] < 2000, "왼쪽 크롭이 여전히 전폭이면 옆 컬럼이 안 걸러진 것이다"
    assert left_call["colors"] == {RED}, f"왼쪽 크롭에 파랑(오른쪽 컬럼)이 보였다: {left_call['colors']}"
    assert right_call["colors"] == {BLUE}, f"오른쪽 크롭에 빨강(왼쪽 컬럼)이 보였다: {right_call['colors']}"


def test_전폭_요소가_있으면_컬럼을_이어붙여_한_번에_읽는다(monkeypatch):
    """자기 자신이 잘리면 안 되는 요소(배너 등)는 그룹을 이어 붙여 온전히 보여준다."""
    left = _region("p1_r0", [100, 100, 900, 300], "왼쪽 상품")
    right = _region("p1_r1", [1100, 100, 1900, 300], "오른쪽 상품")
    banner = _region("p1_r2", [50, 350, 1950, 450], "전폭 배너")
    page = _page([left, right, banner])
    calls: list[dict] = []
    monkeypatch.setattr(vlm_direct, "read_band_regions", _fake_reader(calls))

    pipeline._merged_band_read(page, _half_painted_canvas(), [])

    assert len(calls) == 1, "전폭 요소가 두 컬럼을 이어 붙여 한 그룹이 돼야 한다"
    assert {"p1_r0", "p1_r1", "p1_r2"} == {rid for rid, _ in calls[0]["entries"]}
    assert calls[0]["colors"] == {RED, BLUE}, "이어붙인 그룹은 자기 자신(배너)이 잘리지 않아야 한다"


def test_컬럼_분리시_회수라인_bbox도_그_구간으로_좁혀진다(monkeypatch):
    """누락 회수 라인도 전폭으로 돌려주면 결함②(옆 컬럼 침범)를 재현한다."""
    left = _region("p1_r0", [100, 100, 900, 900], "왼쪽 상품")
    right = _region("p1_r1", [1100, 100, 1900, 900], "오른쪽 상품")
    page = _page([left, right])

    def fake(crop, entries):
        readings = {rid: (f"판독-{rid}", 0.9) for rid, _ in entries}
        missing = [{"text": "새로 찾은 문구", "y_ratio": 0.5, "confidence": 0.8}]
        return readings, missing, collections.Counter()

    monkeypatch.setattr(vlm_direct, "read_band_regions", fake)
    recovered = pipeline._merged_band_read(page, _half_painted_canvas(), [])

    assert len(recovered) == 2, "두 컬럼 각각에서 회수돼야 한다"
    for line in recovered:
        width = line.bbox[2] - line.bbox[0]
        assert width < 2000, f"회수 라인이 여전히 전폭이면 옆 컬럼 침범을 재현한다: {line.bbox}"
