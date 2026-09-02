# -*- coding: utf-8 -*-
"""evidence-v4 → ad-review-input-v2 투영 계약."""

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

    company = next(field for field in review["template_fields"] if field["label"] == "회사명")
    assert company["field_key"] == "template:회사명"
    assert company["values"][0]["value"] == "NH농협은행"
    assert company["values"][0]["lines"][0]["line_ref"] == "p1/p1_r000/L00"
    assert company["values"][0]["value_source"] == "parser_primary_text"
    assert company["values"][0]["lines"][0]["parser_text"] == "NH농협은행"

    copies = [item["ad_copy_text"] for item in review["unmapped_ad_copy"]]
    assert copies == ["템플릿 밖 홍보 문구", "미배정 낱줄"]
    assert review["summary"]["parser_primary_line_total"] == 3
    assert review["summary"]["template_assigned_parser_primary_line_count"] == 1
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

    table = review["unmapped_ad_copy"][0]["table"]
    assert table["paddlex_grid_used"] is False
    assert table["vlm_table_reading"]["rows"] == [["상품", "금리"], ["A", "3%"]]
    assert "cells" not in table
    assert "html" not in table
    assert review["summary"]["table_region_count"] == 1
