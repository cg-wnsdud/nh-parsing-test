# -*- coding: utf-8 -*-
"""P1/evidence-v6 → P2/ad-review-input-v6 최소 심의 전달 계약."""

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
    assert review["contract"]["version"] == "nh-ad-review-input-v6"
    assert review["summary"]["parser_primary_line_total"] == 3
    assert review["summary"]["labelled_parser_primary_line_count"] == 1
    assert review["summary"]["unmapped_ad_copy_parser_primary_line_count"] == 2


def _table_doc(*regions: list[str], unassigned: list[str] = ()) -> dict:
    """첫 영역만 표로 만든 문서. PaddleX 격자와 VLM 관측을 함께 둔다."""
    doc = _doc(*regions, unassigned=unassigned)
    doc["pages"][0]["regions"][0].update({
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
    return doc


def test_표는_paddlex_셀대신_vlm_관측만_인계한다(pack: dict) -> None:
    """미배정 표도 격자를 넘긴다 — 라벨을 못 받았다고 구조를 버리지 않는다."""
    doc = _table_doc(["표 OCR 줄"])
    label = AT.label_document(doc, "예금성상품-적립식", pack)
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    view = review["unmapped_ad_copy"][0]
    assert view["review_text"] == "표 OCR 줄"
    assert view["line_refs"] == ["p1/p1_r000/L00"]
    assert view["tables"] == [{
        "region_id": "p1_r000",
        "rows": [["상품", "금리"], ["A", "3%"]],
        "source": "table_region_reading",
        "status": "observed",
        "confidence": 0.88,
        "structure_confidence": 0.84,
        "parser_primary_line_refs": ["p1/p1_r000/L00"],
    }]
    # PaddleX 셀 텍스트·행수는 P2 에 새지 않는다 (격자 원천은 VLM 관측뿐).
    assert "PaddleX 값" not in json.dumps(review, ensure_ascii=False)
    assert 99 not in [table.get("n_rows") for table in view["tables"]]


def test_표는_의미라벨을_받은_라벨_view에_연결된다(pack: dict) -> None:
    doc = _table_doc(["표 OCR 줄"])
    label = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={"p1_r000": [{"gubun": "금리", "confidence": 0.9, "line_from": 0, "line_to": 0}]},
    )
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    rate = next(item for item in review["labelled_ad_copy"] if item["label"] == "금리")
    view = rate["text_views"][0]
    assert [table["region_id"] for table in view["tables"]] == ["p1_r000"]
    assert view["tables"][0]["rows"] == [["상품", "금리"], ["A", "3%"]]
    # 표 줄이 라벨을 받았으니 미배정 view 는 아예 생기지 않는다.
    assert review["unmapped_ad_copy"] == []


def test_정형문구_일치만으로는_표를_다른_라벨에_붙이지_않는다(pack: dict) -> None:
    """`NH농협은행` 이 표 안에 있어도 회사명 라벨이 표를 가져가지 않는다.

    실측(`16. 대출성상품`) 표 영역 두 개는 1층 히트가 0개였다. 이 시험은 그 방어가
    데이터 우연이 아니라 규칙임을 못박는다 — 연결 근거는 2층 의미 판정뿐이다.
    """
    doc = _table_doc(["NH농협은행 기준 금리표"])
    label = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={"p1_r000": [{"gubun": "금리", "confidence": 0.9, "line_from": 0, "line_to": 0}]},
    )
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    company = next(item for item in review["labelled_ad_copy"] if item["label"] == "회사명")
    # 회사명은 정형문구 완전일치로 같은 줄을 근거로 갖지만(중복 허용) 표는 못 가져간다.
    assert company["text_views"][0]["line_refs"] == ["p1/p1_r000/L00"]
    assert "tables" not in company["text_views"][0]

    rate = next(item for item in review["labelled_ad_copy"] if item["label"] == "금리")
    assert [table["region_id"] for table in rate["text_views"][0]["tables"]] == ["p1_r000"]


def test_표_영역이_라벨과_미배정에_걸치면_양쪽에_격자를_넘긴다(pack: dict) -> None:
    """실측 p1_r019(56줄) 재현 — L00 은 금리, L01 은 미배정이다."""
    doc = _table_doc(["금리 행", "미배정 행"])
    label = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={"p1_r000": [{"gubun": "금리", "confidence": 0.9, "line_from": 0, "line_to": 0}]},
    )
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    rate = next(item for item in review["labelled_ad_copy"] if item["label"] == "금리")
    assert [table["region_id"] for table in rate["text_views"][0]["tables"]] == ["p1_r000"]
    assert [table["region_id"] for table in review["unmapped_ad_copy"][0]["tables"]] == ["p1_r000"]
    # 격자를 두 곳에 실어도 줄 분할은 그대로다 — tables 는 소유권을 만들지 않는다.
    assert rate["text_views"][0]["line_refs"] == ["p1/p1_r000/L00"]
    assert review["unmapped_ad_copy"][0]["line_refs"] == ["p1/p1_r000/L01"]


def test_표가_없는_view에는_tables_키를_만들지_않는다(pack: dict) -> None:
    review = build_ad_review_input(_evidence(pack))
    for item in review["labelled_ad_copy"]:
        for view in item["text_views"]:
            assert "tables" not in view
    for view in review["unmapped_ad_copy"]:
        assert "tables" not in view


def test_p2_line_refs로_p1_줄과_region_bbox를_찾을_수_있다(pack: dict) -> None:
    """심의 결과를 원본 화면에 표시하는 왕복 경로.

    ``line_ref`` 문자열을 파싱하지 않고 P1 을 순회해 색인을 만든다 — 문자열 형식은
    계약이 아니므로 소비자가 의존해선 안 된다.
    """
    evidence = _evidence(pack)
    review = build_ad_review_input(evidence)

    owner: dict[str, dict] = {}
    for page in evidence["pages"]:
        for region in page["regions"]:
            for line in region["lines"]:
                owner[line["line_ref"]] = {"region": region, "line": line}
        for line in page["unassigned_lines"]:
            owner[line["line_ref"]] = {"region": None, "line": line}

    company = next(item for item in review["labelled_ad_copy"] if item["label"] == "회사명")
    ref = company["text_views"][0]["line_refs"][0]
    assert owner[ref]["region"]["region_id"] == "p1_r000"
    assert owner[ref]["region"]["bbox"] == [0, 0, 100, 9]
    assert owner[ref]["line"]["parser_text"] == "NH농협은행"

    # 미배정 문구도 같은 색인으로 닿는다 (영역이 없으면 region 은 None).
    unmapped_refs = review["unmapped_ad_copy"][0]["line_refs"]
    assert all(ref in owner for ref in unmapped_refs)
    assert any(owner[ref]["region"] is None for ref in unmapped_refs)


def test_라벨_사이_동일_line_ref_중복은_허용한다(pack: dict) -> None:
    """1층 완전일치와 2층 의미 판정이 같은 줄을 가리켜도 오류가 아니다."""
    doc = _doc(["NH농협은행 영업점 및 홈페이지"])
    label = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={"p1_r000": [{"gubun": "금리", "confidence": 0.9, "line_from": 0, "line_to": 0}]},
    )
    evidence = build_unified(doc, AT.Resolution("예금성상품-적립식", "확정").as_dict(), label)
    review = build_ad_review_input(evidence)

    holders = {
        item["label"]
        for item in review["labelled_ad_copy"]
        for view in item["text_views"]
        if "p1/p1_r000/L00" in view["line_refs"]
    }
    assert {"회사명", "금리"} <= holders
    # 겹쳐도 labelled ⊎ unmapped 분할 불변식은 유지된다.
    assert review["summary"]["parser_primary_line_total"] == 1
    assert review["summary"]["unmapped_ad_copy_parser_primary_line_count"] == 0


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
