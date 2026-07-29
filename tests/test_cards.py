# -*- coding: utf-8 -*-
"""카드-분할(§D) 단위테스트 — VLM 은 monkeypatch (폐쇄망 불필요).

판단 분담: 개수·경계는 픽셀 글자밀도(코드), 배정은 VLM. 개수가 맞으면 투표를 더 안 돌린다.
"""

from PIL import Image, ImageDraw

from nh_parsing import cards, vlm_judge
from nh_parsing.ir import AdPage, Line, Region


def _page():
    return AdPage(
        page_no=1, canvas_w=2000, canvas_h=1120, parse_route="ocr",
        regions=[
            Region(region_id="p1_r0", bbox=[100, 60, 900, 120], role="본문",
                   lines=[Line(text="공통 배너", bbox=[100, 60, 900, 120], source="ocr")]),
            Region(region_id="p1_r1", bbox=[100, 200, 600, 900], role="본문",
                   lines=[Line(text="EVENT 1 참여방법", bbox=[100, 200, 600, 900], source="ocr")]),
            Region(region_id="p1_r2", bbox=[1300, 200, 1800, 900], role="본문",
                   lines=[Line(text="EVENT 2 참여방법", bbox=[1300, 200, 1800, 900], source="ocr")]),
        ],
    )


def test_assign_cards_collage(monkeypatch):
    def fake(parts, schema_name, schema, max_tokens):
        return {"analysis": "좌우 2개 카드 + 상단 공통 배너",
                "page_kind": "card_collage", "card_count": 2,
                "assignments": [
                    {"region_id": "p1_r0", "card_no": 0},   # 공통 배너
                    {"region_id": "p1_r1", "card_no": 1},
                    {"region_id": "p1_r2", "card_no": 2},
                ]}
    monkeypatch.setattr(cards, "chat_json", fake)
    out = cards.assign_cards_vlm(_page(), Image.new("RGB", (2000, 1120), "white"))
    assert out == {"p1_r0": 0, "p1_r1": 1, "p1_r2": 2}


def test_tall_scroll_skips_card_split_without_calling_vlm(monkeypatch):
    """세로 스크롤(종횡비 큼)은 카드-분할 대상 제외 — VLM 호출조차 안 한다(올원e 회귀 방지)."""
    called = {"n": 0}
    def spy(*a, **k):
        called["n"] += 1
        return {"analysis": "", "page_kind": "card_collage", "card_count": 4, "assignments": []}
    monkeypatch.setattr(cards, "chat_json", spy)
    scroll = AdPage(
        page_no=1, canvas_w=1122, canvas_h=6429, parse_route="ocr",  # h/w≈5.7 스크롤
        regions=[
            Region(region_id="p1_r0", bbox=[60, 100, 900, 160], role="본문",
                   lines=[Line(text="헤드라인", bbox=[60, 100, 900, 160], source="ocr")]),
            Region(region_id="p1_r1", bbox=[60, 2000, 900, 2100], role="본문",
                   lines=[Line(text="유의사항", bbox=[60, 2000, 900, 2100], source="ocr")]),
        ],
    )
    assert cards.assign_cards_vlm(scroll, Image.new("RGB", (1122, 6429), "white")) == {}
    assert called["n"] == 0  # 스크롤은 VLM 호출 자체를 안 함


def test_wide_single_panel_skips_card_split_without_vlm(monkeypatch):
    """가로지만 분리 가능한 덩어리가 없는 단일 패널(003 p3류)은 VLM 호출 없이 제외 — 리소스 절약."""
    called = {"n": 0}
    def spy(*a, **k):
        called["n"] += 1
        return {"analysis": "", "page_kind": "card_collage", "card_count": 2, "assignments": []}
    monkeypatch.setattr(cards, "chat_json", spy)
    panel = AdPage(
        page_no=1, canvas_w=2000, canvas_h=1125, parse_route="ocr",  # 가로, 하지만 한 덩어리
        regions=[
            Region(region_id="p1_r0", bbox=[200, 150, 1800, 300], role="본문",
                   lines=[Line(text="한 덩어리 텍스트", bbox=[200, 150, 1800, 300], source="ocr")]),
            Region(region_id="p1_r1", bbox=[210, 320, 1790, 480], role="본문",
                   lines=[Line(text="이어지는 내용", bbox=[210, 320, 1790, 480], source="ocr")]),
            Region(region_id="p1_r2", bbox=[205, 500, 1795, 660], role="본문",
                   lines=[Line(text="더 이어짐", bbox=[205, 500, 1795, 660], source="ocr")]),
        ],
    )
    assert cards.assign_cards_vlm(panel, Image.new("RGB", (2000, 1125), "white")) == {}
    assert called["n"] == 0  # 단일 패널은 VLM 호출 안 함


def test_density_counts_cards_without_calling_vlm():
    """003 실측 재현 — 컬럼 사이 거터로 카드 개수를 모델 없이 센다."""
    img = Image.new("RGB", (2000, 1120), "white")
    d = ImageDraw.Draw(img)
    for x0, x1 in ((100, 600), (750, 1250), (1400, 1900)):
        for x in range(x0, x1, 8):
            d.line([(x, 100), (x, 1000)], fill=(0, 0, 0), width=2)

    assert cards.density_card_count(img) == 3


def test_density_count_stops_extra_votes(monkeypatch):
    """개수가 밀도와 맞으면 관측 1회로 끝낸다 — 투표는 개수를 안정화하려던 것이었다."""
    img = Image.new("RGB", (2000, 1120), "white")
    d = ImageDraw.Draw(img)
    for x0, x1 in ((100, 600), (1400, 1900)):
        for x in range(x0, x1, 8):
            d.line([(x, 100), (x, 1000)], fill=(0, 0, 0), width=2)

    calls = {"n": 0}

    def fake(parts, schema_name, schema, max_tokens):
        calls["n"] += 1
        return {"analysis": "", "page_kind": "card_collage", "card_count": 2,
                "assignments": [{"region_id": "p1_r0", "card_no": 0},
                                {"region_id": "p1_r1", "card_no": 1},
                                {"region_id": "p1_r2", "card_no": 2}]}

    monkeypatch.setattr(cards, "chat_json", fake)
    out = cards.assign_cards_vlm(_page(), img, votes=3)

    assert out == {"p1_r0": 0, "p1_r1": 1, "p1_r2": 2}
    assert calls["n"] == 1, f"밀도와 개수가 같으면 1회면 충분하다 (실제 {calls['n']}회)"


def _three_card_canvas():
    img = Image.new("RGB", (2000, 1120), "white")
    d = ImageDraw.Draw(img)
    for x0, x1 in ((100, 600), (750, 1250), (1400, 1900)):   # 밀도 = 3장
        for x in range(x0, x1, 8):
            d.line([(x, 100), (x, 1000)], fill=(0, 0, 0), width=2)
    return img


def _fake_reply(card_count, calls, seen):
    def fake(parts, schema_name, schema, max_tokens):
        calls["n"] += 1
        seen.append(parts[0]["text"])
        return {"analysis": "", "page_kind": "card_collage", "card_count": card_count,
                "assignments": [{"region_id": "p1_r1", "card_no": 1},
                                {"region_id": "p1_r2", "card_no": 2}]}
    return fake


def test_밀도_근거가_있으면_한_번만_묻는다(monkeypatch):
    """같은 질문을 반복해도 새 정보가 없다 — 실측(003 p1): 3회를 더 써서 같은 답이 나왔다."""
    calls, seen = {"n": 0}, []
    monkeypatch.setattr(cards, "chat_json", _fake_reply(3, calls, seen))
    cards.assign_cards_vlm(_page(), _three_card_canvas(), votes=3)
    assert calls["n"] == 1


def test_프롬프트에_밀도_관측을_증거로_싣는다(monkeypatch):
    calls, seen = {"n": 0}, []
    monkeypatch.setattr(cards, "chat_json", _fake_reply(3, calls, seen))
    cards.assign_cards_vlm(_page(), _three_card_canvas(), votes=3)
    assert "3개로 보이며" in seen[0], "개수를 근거로 줘야 한다"
    assert "x_ratio" in seen[0] and "1번" in seen[0], "카드별 가로 범위도 줘야 한다"
    assert "다른 개수로 답하세요" in seen[0], "VLM 이 뒤집을 수 있어야 한다(정답 아닌 증거)"


def test_증거를_보고도_다르면_VLM_을_따르되_기록한다(monkeypatch):
    """밀도가 VLM 보다 정확하다는 근거가 아직 문서 하나뿐이라 권한을 뺏지 않는다."""
    calls, seen = {"n": 0}, []
    monkeypatch.setattr(cards, "chat_json", _fake_reply(2, calls, seen))
    page = _page()
    cards.assign_cards_vlm(page, _three_card_canvas(), votes=3)
    assert calls["n"] == 1, "증거를 보고도 다르면 다시 물어도 같은 답이다"
    assert any("카드 개수 불일치" in n for n in page.notes), "조용히 넘기면 안 된다"


def test_밀도_근거가_없으면_예전처럼_투표한다(monkeypatch):
    """캔버스가 없으면(HWP 디지털 등) 흔들림을 흡수할 다른 수단이 없다."""
    calls, seen = {"n": 0}, []
    monkeypatch.setattr(cards, "chat_json", _fake_reply(2, calls, seen))
    monkeypatch.setattr(cards, "should_detect_cards", lambda page, canvas=None: True)
    cards.assign_cards_vlm(_page(), None, votes=3)
    assert calls["n"] == 3


def test_assign_cards_single_scroll_returns_empty(monkeypatch):
    def fake(parts, schema_name, schema, max_tokens):
        return {"analysis": "세로 스크롤", "page_kind": "single_scroll",
                "card_count": 1, "assignments": []}
    monkeypatch.setattr(cards, "chat_json", fake)
    assert cards.assign_cards_vlm(_page(), Image.new("RGB", (2000, 1120), "white")) == {}


def test_assign_cards_vlm_failure_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("서버 오류")
    monkeypatch.setattr(cards, "chat_json", boom)
    assert cards.assign_cards_vlm(_page(), Image.new("RGB", (2000, 1120), "white")) == {}


def test_judge_uses_card_assignment_as_group(monkeypatch):
    """card_by_region 을 주면 group_no 를 VLM 눈대중 대신 카드 배정으로 확정한다."""
    def fake_roles(parts, schema_name, schema, max_tokens):
        # VLM 역할판정은 group_no 를 엉뚱하게(전부 1) 줘도, card_by_region 이 이긴다
        return {"analysis": "", "regions": [
            {"region_id": "p1_r1", "role": "본문", "section_type": "참여방법",
             "section_no": 1, "group_no": 1, "confidence": 1.0},
            {"region_id": "p1_r2", "role": "본문", "section_type": "참여방법",
             "section_no": 1, "group_no": 1, "confidence": 1.0},
        ]}
    monkeypatch.setattr(vlm_judge, "chat_json", fake_roles)
    page = _page()
    secs = vlm_judge.judge_region_roles(
        page.regions, Image.new("RGB", (2000, 1120), "white"), page.canvas_h,
        card_by_region={"p1_r1": 1, "p1_r2": 2},
    )
    # 카드가 다르므로(1 vs 2) 같은 타입이어도 별도 섹션(그룹)으로 갈라져야 함
    groups = {s.group_no for s in secs}
    assert groups == {1, 2}
