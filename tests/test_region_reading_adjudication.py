# -*- coding: utf-8 -*-
"""기본 파이프라인의 영역 Reader/Judge shadow 계약을 고정한다."""

from __future__ import annotations

from dataclasses import replace

from PIL import Image, ImageDraw

from nh_parsing import reading_adjudication as ra
from nh_parsing.ir import Line, Region, RegionTable


def _region(
    text: str = "정본 문구",
    *,
    confidence: float = 0.95,
    role: str = "본문",
    table: bool = False,
    band: str | None = None,
) -> Region:
    return Region(
        region_id="p1_r001",
        bbox=[20, 20, 80, 50],
        role=role,
        table=RegionTable(n_rows=1, n_cols=1) if table else None,
        lines=[Line(text=text, bbox=[20, 20, 80, 50], source="ocr", confidence=confidence)],
        vlm_reading=band,
    )


def _settings(monkeypatch, *, scope: str = "targeted") -> None:
    monkeypatch.setattr(
        ra,
        "SETTINGS",
        replace(
            ra.SETTINGS,
            region_reading_scope=scope,
            region_reader_max_per_page=0,
            region_crop_padding_px=8,
            region_mask_bleed_px=1,
        ),
    )


def test_crop은_목표_영역_밖의_이웃_글자를_마스킹한다(monkeypatch):
    _settings(monkeypatch)
    canvas = Image.new("RGB", (120, 80), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((25, 25, 70, 45), fill="black")       # 목표 영역 텍스트
    draw.rectangle((82, 25, 86, 45), fill="black")       # 이웃 영역 텍스트

    crop = ra.masked_region_crop(_region(), canvas)

    assert crop is not None
    # 목표 bbox는 crop 내부에서 보존되고, 이웃 검정 글자는 crop에 들어와도 흰색 처리된다.
    assert crop.image.getpixel((20, 20)) == (0, 0, 0)
    assert crop.image.getpixel((72, 20)) == (255, 255, 255)
    assert crop.crop_bbox == [12, 12, 88, 58]
    assert crop.target_bbox == [8, 8, 68, 38]


def test_목표_bbox_안에_중첩된_독립영역도_마스킹한다(monkeypatch):
    _settings(monkeypatch)
    canvas = Image.new("RGB", (120, 100), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((24, 24, 75, 70), fill="black")
    target = Region(
        region_id="p1_r010", bbox=[20, 20, 90, 80],
        lines=[Line(text="부모 영역", bbox=[20, 20, 90, 80], source="ocr")],
    )
    nested = Region(
        region_id="p1_r011", bbox=[40, 30, 60, 50],
        lines=[Line(text="별도 영역", bbox=[40, 30, 60, 50], source="ocr")],
    )

    crop = ra.masked_region_crop(target, canvas, [target, nested])

    assert crop is not None
    assert crop.excluded_overlap_region_ids == ["p1_r011"]
    # nested bbox 중심(원본 50, 40)은 crop 내부 좌표 (38, 28)에서 흰색이어야 한다.
    assert crop.image.getpixel((38, 28)) == (255, 255, 255)


def test_targeted_선별은_위험_근거가_있는_영역만_고른다(monkeypatch):
    _settings(monkeypatch)
    assert ra.selection_reasons(_region(confidence=0.99)) == []
    assert "low_ocr_confidence" in ra.selection_reasons(_region(confidence=0.5))
    assert "table_structure" in ra.selection_reasons(_region(table=True))
    # targeted는 비용 비교용 보조 모드이며, 이전 밴드 후보/role에 의존하지 않는다.
    assert ra.selection_reasons(_region(text="금리 7.1%", band="금리 71%")) == []


def test_reader_합의는_judge를_생략하고_정본을_보존한다(monkeypatch):
    _settings(monkeypatch, scope="all")
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return {"analysis": "한 줄", "text": "정본 문구", "confidence": 0.96}

    monkeypatch.setattr(ra, "chat_json", fake_chat)
    region = _region()
    original = region.text

    stats = ra.adjudicate_regions_shadow([region], Image.new("RGB", (120, 80), "white"))

    assert stats["requested"] == stats["agreed"] == 1
    assert len(calls) == 1
    assert region.text == original
    assert region.reading_adjudication is not None
    assert region.reading_adjudication.status == "agreed"
    assert region.reading_adjudication.crop_policy == "masked_outside_region"
    assert region.reading_adjudication.selection_reasons == ["all_structure_text_regions"]


def test_표영역은_paddlex_격자없이_표전용_reader로_관측한다(monkeypatch):
    _settings(monkeypatch, scope="all")
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(kwargs)
        return {
            "analysis": "2행 2열 표", "text": "상품\t금리\nA\t3%",
            "rows": [["상품", "금리"], ["A", "3%"]],
            "confidence": 0.93, "structure_confidence": 0.89,
        }

    monkeypatch.setattr(ra, "chat_json", fake_chat)
    region = _region(text="상품 금리 A 3%", table=True)

    stats = ra.adjudicate_regions_shadow([region], Image.new("RGB", (120, 80), "white"))

    assert stats["requested"] == 1
    assert calls[0]["schema_name"] == "table_region_reader"
    assert region.table_vlm_reading is not None
    assert region.table_vlm_reading.rows == [["상품", "금리"], ["A", "3%"]]
    assert region.element_vlm_reading == "상품\t금리\nA\t3%"


def test_vlm_선호_judge도_shadow에서는_정본을_바꾸지_않는다(monkeypatch):
    _settings(monkeypatch, scope="all")
    replies = iter([
        {"analysis": "한 줄", "text": "완전히 다른 문구", "confidence": 0.99},
        {
            "analysis": "B보다 A가 선명", "decision": "candidate_a", "text": "완전히 다른 문구",
            "confidence": 0.99, "reason": "픽셀상 후보 A",
        },
    ])
    monkeypatch.setattr(ra, "chat_json", lambda *args, **kwargs: next(replies))
    region = _region()
    original = region.text

    stats = ra.adjudicate_regions_shadow([region], Image.new("RGB", (120, 80), "white"))

    assert stats["uncertain"] == 1
    assert region.text == original
    assert region.element_vlm_reading == "완전히 다른 문구"
    assert region.reading_adjudication is not None
    assert region.reading_adjudication.status == "uncertain"
    assert region.reading_adjudication.proposed_text == "완전히 다른 문구"
    assert region.reading_adjudication.proposed_source == "element_vlm"
