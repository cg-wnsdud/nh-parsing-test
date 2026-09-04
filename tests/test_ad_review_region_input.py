# -*- coding: utf-8 -*-
"""영역 중심 P3 후보 계약 회귀 테스트."""

from __future__ import annotations

import pytest

from nh_parsing.ad_review_region_input import build_ad_review_region_input


def _line(index: int, text: str, labels: list[dict] | None = None) -> dict:
    return {
        "line_ref": f"p1/p1_r000/L{index:02d}",
        "parser_text": text,
        "labels": labels or [],
        "vlm_gubun": [],
    }


def _evidence() -> dict:
    lines = [
        _line(0, "기본금리"),
        _line(1, "연 2.0%", [{"gubun": "금리", "phrase_id": "P100"}]),
        _line(2, "우대조건"),
        _line(3, "급여이체 연 0.2%p"),
    ]
    return {
        "doc_id": "sample",
        "source_file": "sample.pdf",
        "file_type": "pdf",
        "classification": {"product_group": "예금"},
        "template": {"template_id": "예금성상품-적립식", "status": "확정"},
        "reading_evidence_contract": {"version": "nh-ad-review-evidence-v6"},
        "pages": [{
            "page_no": 1,
            "regions": [{
                "region_id": "p1_r000",
                "card_no": None,
                "lines": lines,
                "text_evidence": {
                    "p2_text_selection": {
                        "selected_text": "기본금리 연 2.0% / 우대조건 급여이체 연 0.2%p",
                        "selected_text_source": "vlm_judge_selected_region_test",
                    }
                },
                "template_labels": {"semantic": [
                    {"gubun": "금리", "confidence": 0.93, "line_from": 0, "line_to": 1},
                    {"gubun": "우대금리", "confidence": 0.88, "line_from": 2, "line_to": 3},
                ]},
                "table": {
                    "detected_by": "structurev3",
                    "parser_primary_line_refs": [line["line_ref"] for line in lines],
                    "vlm_table_reading": {
                        "status": "read",
                        "rows": [["구분", "금리"], ["기본", "2.0%"]],
                        "confidence": 0.9,
                    },
                },
            }, {
                "region_id": "p1_r001",
                "bbox": [0, 0, 10, 10],
                "layout": {"label": "figure", "score": 0.8},
                "card_no": None,
                "lines": [],
            }],
            "unassigned_lines": [{
                "line_ref": "p1/unassigned/L00", "parser_text": "미배정 문구",
            }],
            "recovery_candidates": [{
                "text": "VLM 후보", "status": "unverified", "bbox": [1, 2, 3, 4],
            }],
        }],
        "review_targets": [{
            "target_id": "template:금리",
            "label": "금리",
            "evidence": {"region_ids": ["p1_r000"], "line_refs": [
                "p1/p1_r000/L00", "p1/p1_r000/L01",
            ]},
        }, {
            "target_id": "template:우대금리",
            "label": "우대금리",
            "evidence": {"region_ids": ["p1_r000"], "line_refs": [
                "p1/p1_r000/L02", "p1/p1_r000/L03",
            ]},
        }],
    }


def test_영역은_한번만_싣고_복수_라벨_범위를_붙인다() -> None:
    p3 = build_ad_review_region_input(_evidence())
    region = p3["pages"][0]["regions"][0]

    assert region["region_id"] == "p1_r000"
    assert region["review_text"] == "기본금리 연 2.0% / 우대조건 급여이체 연 0.2%p"
    assert region["text_source"] == "vlm_judge_selected_region_test"
    assert region["line_refs"] == [f"p1/p1_r000/L{i:02d}" for i in range(4)]
    assert [label["label"] for label in region["labels"]] == ["금리", "우대금리"]
    assert region["labels"][0]["spans"][0]["line_from"] == 0
    assert region["labels"][1]["spans"][0]["line_to"] == 3
    assert p3["summary"]["parser_primary_line_total"] == 5


def test_같은_줄의_라벨_중복을_허용하고_소유권은_중복하지_않는다() -> None:
    evidence = _evidence()
    region = evidence["pages"][0]["regions"][0]
    region["template_labels"]["semantic"].append({
        "gubun": "상품내용", "confidence": 0.7, "line_from": 1, "line_to": 2,
    })
    evidence["review_targets"].append({
        "target_id": "template:상품내용", "label": "상품내용",
        "evidence": {"region_ids": ["p1_r000"], "line_refs": [
            "p1/p1_r000/L01", "p1/p1_r000/L02",
        ]},
    })

    p3 = build_ad_review_region_input(evidence)
    output_region = p3["pages"][0]["regions"][0]

    assert {label["label"] for label in output_region["labels"]} == {"금리", "우대금리", "상품내용"}
    assert len(output_region["line_refs"]) == len(set(output_region["line_refs"])) == 4
    assert all("review_text" not in entry for entry in p3["label_index"])


def test_표는_영역_본문을_대체하지_않는_형제값으로_그대로_보존한다() -> None:
    evidence = _evidence()
    p3 = build_ad_review_region_input(evidence)
    table = p3["pages"][0]["regions"][0]["table"]

    assert table == evidence["pages"][0]["regions"][0]["table"]
    assert table["vlm_table_reading"]["rows"][1] == ["기본", "2.0%"]
    assert p3["contract"]["tables"].endswith("create no text ownership")


def test_미배정_회수후보_빈영역은_서로_섞지_않는다() -> None:
    p3 = build_ad_review_region_input(_evidence())

    assert p3["pages"][0]["unassigned_text"] == [{
        "line_ref": "p1/unassigned/L00",
        "review_text": "미배정 문구",
        "text_source": "parser_primary_text",
    }]
    assert p3["unverified_recovery_candidates"][0]["text"] == "VLM 후보"
    assert p3["diagnostics"]["empty_regions"][0]["region_id"] == "p1_r001"
    assert p3["summary"]["empty_region_count"] == 1


def test_vlm이_선택되지_않으면_파서_전문을_쓴다() -> None:
    evidence = _evidence()
    selection = evidence["pages"][0]["regions"][0]["text_evidence"]["p2_text_selection"]
    selection["selected_text_source"] = "parser_primary_text"

    p3 = build_ad_review_region_input(evidence)
    assert p3["pages"][0]["regions"][0]["review_text"] == (
        "기본금리\n연 2.0%\n우대조건\n급여이체 연 0.2%p"
    )


def test_P1_line_ref가_중복되면_거부한다() -> None:
    evidence = _evidence()
    evidence["pages"][0]["unassigned_lines"][0]["line_ref"] = "p1/p1_r000/L00"
    with pytest.raises(ValueError, match="중복"):
        build_ad_review_region_input(evidence)
