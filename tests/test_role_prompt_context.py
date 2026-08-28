# -*- coding: utf-8 -*-
"""역할 VLM에 StructureV3의 기존 구조 신호를 실제로 전달하는지 검증한다."""

from nh_parsing import vlm_judge
from nh_parsing.ir import Line, Region, RegionTable, TextStyle


def test_역할_프롬프트에_레이아웃_신호를_담는다(monkeypatch):
    region = Region(
        region_id="p1_r007",
        bbox=[100, 200, 700, 300],
        label="table",
        layout_score=0.73,
        table=RegionTable(n_rows=2, n_cols=3),
        lines=[
            Line(
                text="대출한도 내용",
                bbox=[100, 200, 700, 230],
                source="digital",
                style=TextStyle(size_pt=10.0, style_source="pdf_char"),
            )
        ],
    )
    captured = {}

    def fake_chat(parts, **_kwargs):
        captured["prompt"] = parts[0]["text"]
        return {"analysis": "표", "regions": [
            {"region_id": "p1_r007", "role": "표", "confidence": 0.9}
        ]}

    monkeypatch.setattr(vlm_judge, "chat_json", fake_chat)
    assert vlm_judge.judge_region_roles([region], None, canvas_h=1000) == 1
    prompt = captured["prompt"]
    assert "layout_label='table'" in prompt
    assert "layout_score=0.73" in prompt
    assert "x_ratio=0.10~0.70" in prompt
    assert "table=2x3" in prompt
    assert "font_pt=10.0" in prompt
