# -*- coding: utf-8 -*-
"""P1/evidence-v6 → P2/ad-review-input-v5 최소 심의 전달 계약."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nh_parsing.ad_review_input import build_ad_review_input
from nh_parsing.ad_export import build_unified
from nh_parsing import ad_template as AT

from test_ad_export import _doc


PACK = Path(__file__).resolve().parent.parent / "src/nh_parsing/templates/ad_templates.json"


@pytest.fixture(scope="module")
def pack() -> dict:
    return json.loads(PACK.read_text(encoding="utf-8"))


def _evidence(pack: dict) -> dict:
    doc = _doc(["NH농협은행"], ["템플릿 밖 홍보 문구"], unassigned=["미배정 낱줄"])
    label = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={"p1_r000": [{"gubun": "회사명", "confidence": 0.9, "line_from": 0, "line_to": 0}]},
    )
    return build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)


def test_템플릿_필드와_미배정_광고문구가_정본줄을_빠짐없이_분할한다(pack: dict) -> None:
    review = build_ad_review_input(_evidence(pack))

    company = next(item for item in review["labelled_ad_copy"] if item["label"] == "회사명")
    assert company["label_id"] == "template:회사명"
    assert set(company) == {"label_id", "label", "template_scope", "text_views"}
    view = company["text_views"][0]
    assert view["review_text"] == "NH농협은행"
    assert view["text_source"] == "parser_primary_text"
    assert view["line_refs"] == ["p1/p1_r000/L00"]
    assert "writing_rules" not in company
    assert "requirement" not in company
    assert "fixed_phrase_checks" not in company

    copies = [item["review_text"] for item in review["unmapped_ad_copy"]]
    assert copies == ["템플릿 밖 홍보 문구\n미배정 낱줄"]
    assert review["contract"]["version"] == "nh-ad-review-input-v5"
    assert review["summary"]["parser_primary_line_total"] == 3
    assert review["summary"]["labelled_parser_primary_line_count"] == 1
    assert review["summary"]["unmapped_ad_copy_parser_primary_line_count"] == 2


def test_표는_paddlex_셀대신_vlm_관측만_인계한다(pack: dict) -> None:
    doc = _doc(["표 OCR 줄"])
    region = doc["pages"][0]["regions"][0]
    region.update({
        "label": "table",
        "table": {"n_rows": 99, "cells": [{"row": 0, "col": 0, "text": "PaddleX 값"}]},
        "element_vlm_reading": "상품\t금리\nA\t3%",
        "element_vlm_confidence": 0.88,
        "table_vlm_reading": {
            "text": "상품\t금리\nA\t3%",
            "rows": [["상품", "금리"], ["A", "3%"]],
            "confidence": 0.88,
            "structure_confidence": 0.84,
            "status": "observed",
        },
    })
    label = AT.label_document(doc, "예금성상품-적립식", pack)
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    view = review["unmapped_ad_copy"][0]
    assert view["review_text"] == "표 OCR 줄"
    assert view["line_refs"] == ["p1/p1_r000/L00"]
    assert "table" not in view


def test_judge가_vlm을_선택해도_영역전체_라벨일때만_p2에_쓴다(pack: dict) -> None:
    doc = _doc(["OCR 디자인 문구"])
    region = doc["pages"][0]["regions"][0]
    region.update({
        "element_vlm_reading": "VLM 디자인 문구",
        "reading_adjudication": {
            "mode": "shadow", "crop_bbox": [0, 0, 100, 20], "target_bbox": [0, 0, 100, 20],
            "crop_policy": "masked_outside_region", "excluded_overlap_region_ids": [],
            "selection_reasons": ["all_structure_text_regions"], "canonical_source": "ocr",
            "reader_text": "VLM 디자인 문구", "reader_confidence": 0.95, "relation": "diverged",
            "status": "uncertain", "judge_decision": "candidate_a",
            "proposed_text": "VLM 디자인 문구", "proposed_source": "element_vlm",
            "confidence": 0.95, "reason": "픽셀상 VLM 후보", "error": None,
        },
    })
    label = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={"p1_r000": [{"gubun": "회사명", "confidence": 0.9, "line_from": 0, "line_to": 0}]},
    )
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    company = next(item for item in review["labelled_ad_copy"] if item["label"] == "회사명")
    view = company["text_views"][0]
    assert view["review_text"] == "VLM 디자인 문구"
    assert view["text_source"] == "vlm_judge_selected_region_test"
    assert evidence["pages"][0]["regions"][0]["lines"][0]["parser_text"] == "OCR 디자인 문구"


def test_judge_vlm_영역문구는_라벨이_영역일부면_p2에_쓰지않는다(pack: dict) -> None:
    doc = _doc(["금리 OCR", "유의사항 OCR"])
    region = doc["pages"][0]["regions"][0]
    region.update({
        "element_vlm_reading": "금리 VLM\n유의사항 VLM",
        "reading_adjudication": {
            "mode": "shadow", "crop_bbox": [0, 0, 100, 20], "target_bbox": [0, 0, 100, 20],
            "crop_policy": "masked_outside_region", "excluded_overlap_region_ids": [],
            "selection_reasons": ["all_structure_text_regions"], "canonical_source": "ocr",
            "reader_text": "금리 VLM\n유의사항 VLM", "reader_confidence": 0.95, "relation": "diverged",
            "status": "uncertain", "judge_decision": "candidate_a",
            "proposed_text": "금리 VLM\n유의사항 VLM", "proposed_source": "element_vlm",
            "confidence": 0.95, "reason": "픽셀상 VLM 후보", "error": None,
        },
    })
    label = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={"p1_r000": [{"gubun": "회사명", "confidence": 0.9, "line_from": 0, "line_to": 0}]},
    )
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    company = next(item for item in review["labelled_ad_copy"] if item["label"] == "회사명")
    view = company["text_views"][0]
    assert view["review_text"] == "금리 OCR"
    assert view["text_source"] == "parser_primary_text"


def test_같은_페이지카드의_여러_라벨근거는_하나의_review_text_view로_합친다(pack: dict) -> None:
    doc = _doc(["첫 번째 회사명"], ["두 번째 회사명"])
    label = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={
            "p1_r000": [{"gubun": "회사명", "confidence": 0.9, "line_from": 0, "line_to": 0}],
            "p1_r001": [{"gubun": "회사명", "confidence": 0.9, "line_from": 0, "line_to": 0}],
        },
    )
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    company = next(item for item in review["labelled_ad_copy"] if item["label"] == "회사명")
    assert len(company["text_views"]) == 1
    view = company["text_views"][0]
    assert view["review_text"] == "첫 번째 회사명\n두 번째 회사명"
    assert view["line_refs"] == ["p1/p1_r000/L00", "p1/p1_r001/L00"]
