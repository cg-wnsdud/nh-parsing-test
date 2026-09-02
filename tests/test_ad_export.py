# -*- coding: utf-8 -*-
"""통합 출력(`ad_export.build_unified`)의 계약을 못박는다.

이 단계의 목표는 '라벨을 많이 붙이는 것'이 아니라 **좌표와 라벨을 한 파일로 합치면서
텍스트를 한 줄도 잃지 않는 것**이다. 그래서 여기 시험은 대부분 보존에 관한 것이다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nh_parsing import ad_template as AT
from nh_parsing.ad_export import build_unified, unified_lines

PACK = Path(__file__).resolve().parent.parent / "src/nh_parsing/templates/ad_templates.json"


@pytest.fixture(scope="module")
def pack() -> dict:
    if not PACK.is_file():
        pytest.skip(f"라벨 사전이 없다: {PACK}")
    return json.loads(PACK.read_text(encoding="utf-8"))


def _doc(*regions: list[str], unassigned: list[str] = ()) -> dict:
    return {
        "doc_id": "샘플", "source_file": "샘플.png", "file_type": "image",
        "product_group": "예금성", "ad_type": "상품광고", "product_name_shown": "노출",
        "category_source": "vlm", "classification_confidence": 0.9, "notes": [],
        "pages": [{
            "page_no": 1, "canvas_w": 800, "canvas_h": 1000, "dpi": 150,
            "parse_route": "ocr", "parse_status": "ok",
            "regions": [
                {"region_id": f"p1_r{i:03d}", "bbox": [0, i * 10, 100, i * 10 + 9],
                 "role": "본문",
                 "lines": [{"text": t, "bbox": [0, 0, 1, 1], "source": "ocr",
                            "confidence": 0.9, "style": None} for t in texts]}
                for i, texts in enumerate(regions)
            ],
            "unassigned_lines": [
                {"text": t, "bbox": [0, 0, 1, 1], "source": "ocr",
                 "confidence": 0.8, "style": None} for t in unassigned
            ],
        }],
    }


def _build(doc: dict, template_id: str | None, pack: dict, vlm: dict | None = None) -> dict:
    label = AT.label_document(doc, template_id, pack, vlm_labels=vlm)
    resolution = AT.Resolution(template_id, "확정" if template_id else "판단불가")
    return build_unified(doc, resolution.as_dict(), label)


def test_모든_줄이_제자리에_그대로_실린다(pack: dict) -> None:
    """뼈대가 파싱 결과(쪽→영역→줄)라 라벨이 없어도 실릴 자리가 있다."""
    doc = _doc(["NH농협은행"], ["홍보 문구"], unassigned=["영역에 못 붙은 낱줄"])
    u = _build(doc, "예금성상품-적립식", pack)
    texts = [l["parser_text"] for l in unified_lines(u["pages"])]
    assert texts == ["NH농협은행", "홍보 문구", "영역에 못 붙은 낱줄"]
    assert u["completeness"]["lines_total"] == 3
    assert u["pages"][0]["unassigned_lines"][0]["bbox"] == [0, 0, 1, 1]


def test_줄_수가_라벨링과_어긋나면_멈춘다(pack: dict) -> None:
    """조용히 빠뜨리면 다음 단계에서 '광고에 그 말이 없다'로 둔갑한다.

    라벨링(`collect_lines`)과 통합(`build_unified`)은 같은 파싱 결과를 **따로** 훑는다.
    한쪽만 고치면 소리 없이 갈라지므로 두 숫자를 맞대 본다.
    """
    doc = _doc(["NH농협은행"], ["홍보 문구"])
    label = AT.label_document(doc, "예금성상품-적립식", pack)
    doc["pages"][0]["regions"].pop()                       # 합치기 직전에 한 영역이 사라진 상황
    with pytest.raises(ValueError, match="줄 수가 어긋났다"):
        build_unified(doc, {"template_id": None, "status": "판단불가"}, label)


def test_통합이_줄을_흘리면_멈춘다(pack: dict) -> None:
    """`build_unified` 자체의 traversal 결함을 잡는 가드 — 직접 불러 확인한다."""
    from nh_parsing.ad_export import _verify

    doc = _doc(["NH농협은행"], ["홍보 문구"])
    label = AT.label_document(doc, "예금성상품-적립식", pack)
    u = _build(doc, "예금성상품-적립식", pack)
    u["pages"][0]["regions"].pop()                         # 통합 결과에서만 한 영역이 빠졌다
    with pytest.raises(ValueError, match="유실"):
        _verify(doc, u, label)


def test_좌표와_라벨이_한_줄에_같이_있다(pack: dict) -> None:
    """이 통합의 존재 이유 — 하류가 두 파일을 line_ref 로 이어 붙일 필요가 없어야 한다."""
    doc = _doc(["NH농협은행"])
    u = _build(doc, "예금성상품-적립식", pack,
               vlm={"p1_r000": [{"gubun": "회사명", "confidence": 0.9,
                                 "line_from": 0, "line_to": 0}]})
    line = u["pages"][0]["regions"][0]["lines"][0]
    assert line["bbox"] == [0, 0, 1, 1]
    assert [l["gubun"] for l in line["labels"]] == ["회사명"]
    region = u["pages"][0]["regions"][0]
    assert region["template_labels"]["semantic"] == [{
        "gubun": "회사명", "confidence": 0.9, "line_from": 0, "line_to": 0,
    }]
    assert region["text_evidence"]["parser_primary_text"]["text"] == "NH농협은행"
    assert region["lines"][0]["text_source"] == "ocr"
    assert region["lines"][0]["ocr_confidence"] == 0.9
    assert region["visibility"]["position"]["area_ratio"] > 0


def test_판단불가여도_통합_결과는_나온다(pack: dict) -> None:
    doc = _doc(["아무 문구"], unassigned=["낱줄"])
    u = _build(doc, None, pack)
    assert u["template"]["status"] == "판단불가"
    assert u["review_targets"] == []
    assert u["completeness"]["lines_total"] == 2
    assert all(not l["labels"] for l in unified_lines(u["pages"]))


def test_완전성은_통합_결과를_다시_세어_낸다(pack: dict) -> None:
    """라벨링이 준 숫자를 베끼면 이음질이 틀어져도 안 드러난다."""
    doc = _doc(["NH농협은행"], ["라벨 없는 문구"])
    u = _build(doc, "예금성상품-적립식", pack)
    c = u["completeness"]
    assert c["lines_total"] == c["labeling_lines_total"] == 2
    assert c["labeled"] == 1                               # 회사명만 잡힌다
    assert c["lines_covered"] == 1


def test_기존_밴드_통독_후보는_최종계약에_싣지_않는다(pack: dict) -> None:
    """전 영역 Reader가 기본이므로 넓은 밴드 후보는 정본/심의 계약에서 제외한다."""
    doc = _doc(["아무 문구"])
    doc["pages"][0]["regions"][0].update({
        "vlm_reading": "영역 통독 후보", "vlm_reading_score": 0.8,
        "vlm_reading_coverage": 0.9, "vlm_reading_relation": "tail_cut",
    })
    doc["pages"][0]["regions"][0]["lines"][0].update({
        "vlm_reading": "줄 재판독 후보", "vlm_reading_conf": 0.7,
        "vlm_reading_stage": "lowconf_reread",
    })
    u = _build(doc, "예금성상품-적립식", pack)
    region = u["pages"][0]["regions"][0]
    assert "vlm_reading" not in region
    line = region["lines"][0]
    assert "vlm_reading" not in line


def test_영역_reader_judge_근거와_카드경계도_통합출력에_보존한다(pack: dict) -> None:
    doc = _doc(["정본 문구"])
    doc["pages"][0]["regions"][0].update({
        "card_no": 1,
        "element_vlm_reading": "Reader 후보",
        "element_vlm_confidence": 0.91,
        "reading_adjudication": {
            "mode": "shadow",
            "crop_bbox": [0, 0, 100, 100],
            "target_bbox": [10, 10, 90, 90],
            "crop_policy": "masked_outside_region",
            "excluded_overlap_region_ids": ["p1_r001"],
            "selection_reasons": ["numeric_difference"],
            "canonical_source": "ocr",
            "reader_text": "Reader 후보",
            "reader_confidence": 0.91,
            "relation": "diverged",
            "status": "uncertain",
            "judge_decision": "candidate_a",
            "proposed_text": None,
            "proposed_source": None,
            "confidence": 0.8,
            "reason": "보수정책",
            "error": None,
        },
    })

    u = _build(doc, "예금성상품-적립식", pack)
    region = u["pages"][0]["regions"][0]
    assert region["card_no"] == 1
    assert region["text_evidence"]["vlm_region_reading"]["text"] == "Reader 후보"
    comparison = region["text_evidence"]["parser_vlm_comparison"]
    assert comparison["comparison_status"] == "needs_human_review"
    assert comparison["crop_policy"] == "masked_outside_region"
    assert comparison["excluded_overlap_region_ids"] == ["p1_r001"]
    assert u["reading_evidence_contract"]["parser_mutates_primary_text_from_vlm"] is False


def test_영역_라벨이_있으면_그_안의_줄도_닿은_것으로_센다(pack: dict) -> None:
    """문구가 안 맞아도 '이 영역은 유의사항' 이면 그 줄들의 소속은 아는 것이다."""
    doc = _doc(["표기가 다른 유의사항 문장", "두 번째 줄"])
    u = _build(doc, "예금성상품-적립식", pack,
               vlm={"p1_r000": [{"gubun": "유의사항", "confidence": 0.8,
                                 "line_from": 0, "line_to": 1}]})
    assert u["completeness"]["labeled"] == 0
    assert u["completeness"]["lines_covered"] == 2


def test_review_targets는_심의용_정적기준과_좌표근거만_전달한다(pack: dict) -> None:
    doc = _doc(["NH농협은행"])
    u = _build(doc, "예금성상품-적립식", pack,
               vlm={"p1_r000": [{"gubun": "회사명", "confidence": 0.9,
                                  "line_from": 0, "line_to": 0}]})

    target = next(item for item in u["review_targets"] if item["label"] == "회사명")
    assert target["evidence_status"] == "located"
    assert target["evidence"]["region_ids"] == ["p1_r000"]
    assert target["evidence"]["line_refs"] == ["p1/p1_r000/L00"]
    assert "template_items" not in u


def test_페이지_sweep_후보는_정본_줄과_분리된다(pack: dict) -> None:
    doc = _doc(["OCR 정본"])
    doc["pages"][0]["recovery_candidates"] = [{
        "text": "장식 문구", "bbox": [10, 10, 50, 30], "confidence": 0.7,
        "source": "page_sweep", "status": "unverified",
    }]
    u = _build(doc, "예금성상품-적립식", pack)
    page = u["pages"][0]
    assert page["recovery_candidates"][0]["text"] == "장식 문구"
    assert [line["parser_text"] for line in unified_lines(u["pages"])] == ["OCR 정본"]
