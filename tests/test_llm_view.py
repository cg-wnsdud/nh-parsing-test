# -*- coding: utf-8 -*-
"""STAGE_3 입력 투영(llm_view) 단위테스트 — 폐쇄망(VLM 서버) 불필요.

옛 이름은 test_region_vlm.py 였다. 영역별 VLM 통독(§6 A안/B안 프로토타입) 테스트는
그 기능(_transcribe_regions_vlm/transcribe_region_crops/verify_numeric_fields)이
밴드 통합판독(merged_band_read)으로 완전히 흡수되며 죽은 코드가 되어 걷어냈다
(2026-07-29, 죽은 코드 감사). 여기 남은 두 테스트는 그 기능과 무관한
llm_view.build_doc_view 검증이라 이름을 바꿔 유지한다.
"""

from nh_parsing import llm_view
from nh_parsing.ir import AdDocument, AdPage, Line, Region, Section


def test_llm_view_strips_bbox_and_tags_illustrative():
    doc = AdDocument(
        doc_id="d1", source_file="x.png", file_type="image",
        product_group="예금성", ad_type="이벤트페이지",
        pages=[AdPage(
            page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
            sections=[
                Section(section_id="s00", section_type="헤드라인", section_no=1,
                        bbox=[0, 0, 200, 50], region_ids=["p1_r000"]),
                Section(section_id="s01", section_type="장식예시", section_no=1,
                        bbox=[0, 60, 200, 120], region_ids=["p1_r001"],
                        is_illustrative=True),
            ],
            regions=[
                Region(region_id="p1_r000", bbox=[0, 0, 200, 50], role="제목",
                       section_id="s00",
                       lines=[Line(text="최고 연 7.1%", bbox=[0, 0, 200, 40], source="vlm_region")]),
                Region(region_id="p1_r001", bbox=[0, 60, 200, 120], role="이미지",
                       section_id="s01", is_illustrative=True,
                       lines=[Line(text="앱화면 예시 5,000원", bbox=[0, 60, 200, 110], source="ocr")]),
            ],
        )],
    )
    built = llm_view.build_doc_view(doc)
    assert built["product_group"] == "예금성"
    page = built["pages"][0]

    # 장식예시 섹션은 '빼지 않고 표시만' 한다. 빼면 그 안에 섞인 심의 대상 문구까지
    # 사라진다 — 실측(003): 헤드라인+이벤트기간이 장식예시로 판정돼 통째로 유실됐고,
    # STAGE_3 가 본 적도 없는 내용이 '필드 미발견'으로 집계됐다.
    types = [s["section_type"] for s in page["sections"]]
    assert types == ["헤드라인", "장식예시"]
    assert page["sections"][0].get("illustrative") is None
    assert page["sections"][1]["illustrative"] is True

    region = page["sections"][0]["regions"][0]
    assert region == {"region_id": "p1_r000", "role": "제목", "text": "최고 연 7.1%"}
    # bbox/신뢰도/출처 같은 기계 신호는 투영에 없음
    assert "bbox" not in region and "confidence" not in region and "source" not in region


def test_llm_view_can_still_exclude_illustrative_on_request():
    """검수 화면 등 '심의 대상만' 보고 싶을 때를 위해 제외 옵션은 남긴다."""
    doc = AdDocument(
        doc_id="d1", source_file="x.png", file_type="image", product_group="예금성",
        pages=[AdPage(
            page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
            sections=[Section(section_id="s01", section_type="장식예시", section_no=1,
                              bbox=[0, 0, 200, 50], region_ids=["p1_r000"],
                              is_illustrative=True)],
            regions=[Region(region_id="p1_r000", bbox=[0, 0, 200, 50], role="이미지",
                            section_id="s01", is_illustrative=True,
                            lines=[Line(text="앱화면 예시", bbox=[0, 0, 200, 40], source="ocr")])],
        )],
    )
    assert llm_view.build_doc_view(doc, include_illustrative=False)["pages"][0]["sections"] == []
