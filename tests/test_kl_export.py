# -*- coding: utf-8 -*-
"""KL 출력 규격을 코드로 못박는다 — 규격서와 정답 샘플에서 읽어낸 것만 검사한다.

근거: `농협프로젝트/1.awx_custom_parser_example_api/readme.md` "Output jsonl" 단락 +
정답 샘플 `out_sample_hrc.jsonl`. 규격이 나중에 현장에서 바뀌면 이 파일부터 고친다.
"""

from __future__ import annotations

import json
import re

from nh_parsing.kl_export import build_info, build_items, export_kl_files, heading_level

# 규격이 허용하는 item 유형. 정답 샘플에는 문서화되지 않은 `break` 도 있었지만
# 우리가 만들지는 않는다 — 규격서에 없는 것을 먼저 쓰지 않는다.
ALLOWED_ITEMS = {"text", "table", "image", "h1", "h2", "h3", "h4"}
CUST_KEY = re.compile(r"^cust_s?attr[1-5]$")


def _doc(chunks: list[dict], source_file: str = "(2023년)예금성상품 광고시 준수사항_은행연합회.hwp") -> dict:
    return {"doc_id": "d", "source_file": source_file, "file_type": "hwp", "chunks": chunks, "notes": []}


def test_item_types_stay_within_spec() -> None:
    items, _ = _items_for_all_kinds()
    assert {i["item"] for i in items} <= ALLOWED_ITEMS


def test_value_is_always_present() -> None:
    """규격: "`item` 의 `value` 는 필수값입니다"."""
    items, _ = _items_for_all_kinds()
    assert all(i.get("value") not in (None, "") for i in items)


def test_cust_meta_keys_and_count_stay_within_five() -> None:
    """readme 본문은 attr 5개, 표는 10개로 어긋난다 → 적은 쪽(5)에 맞춘다."""
    items, _ = _items_for_all_kinds()
    for item in items:
        for meta in item.get("cust_meta", []):
            assert CUST_KEY.match(meta["name"]), meta["name"]


def test_coordinates_never_leak_into_embedding_slots() -> None:
    """`cust_sattr*` 은 임베딩에 포함된다 — 기계값을 넣으면 검색이 오염된다.

    sattr 에 들어가는 것은 출처 문서명·상품군·조항 제목뿐이어야 하고, chunk_id 처럼
    기계가 쓰는 식별자는 attr(조회only)에만 있어야 한다.
    """
    items, _ = _items_for_all_kinds()
    for item in items:
        sattr = {m["name"]: m["value"] for m in item.get("cust_meta", []) if m["name"].startswith("cust_sattr")}
        for value in sattr.values():
            assert "_c0" not in value and "_img_" not in value, value


def test_table_chunk_becomes_text_not_table() -> None:
    """우리 table 청크의 text 는 HTML 이 아니다 → table item 으로 신고하면 거짓이 된다."""
    items, notes = build_items(_doc([{"chunk_id": "c1", "kind": "table", "text": "Ⅰ. 목적", "heading": None}]))
    body = [i for i in items if i["item"] != "image"]
    assert [i["item"] for i in body] == ["text"]
    assert body[0]["cust_meta"][0] == {"name": "cust_attr1", "value": "table"}
    assert any("table 청크" in n for n in notes)


def test_image_caption_goes_out_as_text_and_image() -> None:
    """캡션은 임베딩되게 text 로, 이미지 파일은 별도 image item 으로."""
    items, _ = build_items(
        _doc([{"chunk_id": "i1", "kind": "image_caption", "text": "[이미지 사례 img3] 최고 금리 7.00% 강조", "asset_id": "img3", "heading": None}])
    )
    assert [i["item"] for i in items] == ["text", "image"]
    assert items[1]["value"] == "img3.png"
    assert set(items[1]["type_property"]) == {"title", "width", "height", "ratio"}


def test_ocr_page_is_marked_as_low_trust() -> None:
    """스캔 복원 텍스트는 판독이라 정본 신뢰도가 낮다 — 그 사실이 넘어가야 한다."""
    items, _ = build_items(_doc([{"chunk_id": "p3", "kind": "ocr_page", "text": "예시", "heading": "[3쪽 — OCR 복원]"}]))
    text_item = next(i for i in items if i["item"] == "text")
    attrs = {m["name"]: m["value"] for m in text_item["cust_meta"]}
    assert attrs["cust_attr4"] == "ocr"


def test_heading_becomes_h_item_before_its_body() -> None:
    """KL 은 h 아이템을 '이후 본문의 meta' 로 적용한다 → 순서가 뜻을 만든다."""
    items, _ = build_items(
        _doc([{"chunk_id": "c1", "kind": "text", "text": "본문", "heading": "1. 준수사항"}])
    )
    assert [i["item"] for i in items] == ["h2", "text"]


def test_repeated_heading_is_not_duplicated() -> None:
    chunks = [
        {"chunk_id": "c1", "kind": "text", "text": "가", "heading": "1. 준수사항"},
        {"chunk_id": "c2", "kind": "text", "text": "나", "heading": "1. 준수사항"},
    ]
    items, _ = build_items(_doc(chunks))
    assert [i["item"] for i in items] == ["h2", "text", "text"]


def test_heading_depth_from_outline_symbol() -> None:
    assert heading_level("Ⅰ. 목적") == "h1"
    assert heading_level("제3조(직원의 구분)") == "h1"
    assert heading_level("1. 준수사항") == "h2"
    assert heading_level("가. 최고금리를 표시하는 경우") == "h3"
    assert heading_level("① 첫째") == "h3"
    assert heading_level("(1) 세부") == "h4"
    # 판별 실패는 억지로 계층을 만들지 않고 h4 로 떨어뜨린다
    assert heading_level("최고금리 표시 관련") == "h4"


def test_product_group_read_from_filename_only() -> None:
    """상품군은 파일명으로만 정한다 — 본문 판단은 VLM 몫이고 여기서는 하지 않는다."""
    items, _ = build_items(_doc([{"chunk_id": "c1", "kind": "text", "text": "본문", "heading": None}],
                                source_file="(2025년)대출성 상품 광고시 준수사항_은행연합회.pdf"))
    attrs = {m["name"]: m["value"] for m in items[0]["cust_meta"]}
    assert attrs["cust_sattr2"] == "대출성"
    items2, _ = build_items(_doc([{"chunk_id": "c1", "kind": "text", "text": "본문", "heading": None}],
                                 source_file="별첨자료_금융광고규제가이드라인.pdf"))
    attrs2 = {m["name"]: m["value"] for m in items2[0]["cust_meta"]}
    assert "cust_sattr2" not in attrs2  # 판별 안 되면 칸을 쓰지 않는다


def test_filename_rule_keeps_original_extension(tmp_path) -> None:
    """규격: 원본파일명 + "_hrc.jsonl" — 확장자를 떼지 않는다."""
    result = export_kl_files(
        _doc([{"chunk_id": "c1", "kind": "text", "text": "본문", "heading": None}], source_file="sample.hwp"),
        tmp_path,
    )
    assert result.jsonl_path.name == "sample.hwp_hrc.jsonl"
    assert result.info_path.name == "sample.hwp_hrc.json"
    assert result.img_zip_path is None  # 이미지가 없으면 만들지 않는다


def test_jsonl_is_one_json_per_line(tmp_path) -> None:
    result = export_kl_files(_doc([{"chunk_id": "c1", "kind": "text", "text": "본문", "heading": "1. 가"}]), tmp_path)
    for line in result.jsonl_path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_info_does_not_invent_page_sizes() -> None:
    """페이지 크기를 모르면 지어내지 않고 빈 배열로 둔다."""
    info = build_info(_doc([]), None)
    assert info["page_info"] == []
    assert info["parsed_status"] == "S"
    assert info["file_type"] == "hwp"


def _items_for_all_kinds():
    chunks = [
        {"chunk_id": "c1", "kind": "text", "text": "본문 하나", "heading": "Ⅰ. 목적"},
        {"chunk_id": "c2", "kind": "table", "text": "표에서 평탄화된 줄", "heading": "1. 준수사항"},
        {"chunk_id": "c3", "kind": "image_caption", "text": "[이미지 사례 img3] 금리 강조", "asset_id": "img3", "heading": None},
        {"chunk_id": "c4", "kind": "ocr_page", "text": "스캔 복원 텍스트", "heading": "[3쪽 — OCR 복원]"},
    ]
    return build_items(_doc(chunks))
