# -*- coding: utf-8 -*-
"""STAGE_3 입력 투영(llm_view) 단위테스트 — 폐쇄망(VLM 서버) 불필요.

옛 이름은 test_region_vlm.py 였다. 영역별 VLM 통독(§6 A안/B안 프로토타입) 테스트는
그 기능(_transcribe_regions_vlm/transcribe_region_crops/verify_numeric_fields)이
밴드 통합판독(merged_band_read)으로 완전히 흡수되며 죽은 코드가 되어 걷어냈다
(2026-07-29, 죽은 코드 감사).

2026-08-03: 섹션 계층이 파싱에서 제거되면서 투영이 `pages → regions` 평면 구조가
됐다. 장식예시 표시/제외 테스트도 같이 사라졌다 — section_type 이 없으면 무엇이
장식인지 판정할 주체가 없다. 대신 **읽기순서 보존**과 **미배정 낱줄 유지**를
검증한다: 전자는 섹션 라벨이 하던 문맥 전달을 대신하고, 후자는 "STAGE_3 가 본 적도
없는데 필드 미발견으로 집계"되는 사고를 막는 안전장치다.
"""

from nh_parsing import llm_view
from nh_parsing.ir import AdDocument, AdPage, Line, Region


def _doc(page: AdPage) -> AdDocument:
    return AdDocument(
        doc_id="d1", source_file="x.png", file_type="image",
        product_group="예금성", ad_type="이벤트페이지", pages=[page],
    )


def test_llm_view_strips_machine_signals_and_keeps_region_id():
    doc = _doc(AdPage(
        page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
        regions=[
            Region(region_id="p1_r000", bbox=[0, 0, 200, 50], role="제목",
                   lines=[Line(text="최고 연 7.1%", bbox=[0, 0, 200, 40], source="ocr")]),
        ],
    ))
    built = llm_view.build_doc_view(doc)
    assert built["product_group"] == "예금성"
    page = built["pages"][0]

    # 섹션 계층 없음 — 영역이 페이지 바로 아래 평면으로 온다
    assert "sections" not in page
    assert len(page["regions"]) == 1

    region = page["regions"][0]
    assert region == {"region_id": "p1_r000", "role": "제목", "text": "최고 연 7.1%"}
    # bbox/신뢰도/출처 같은 기계 신호는 투영에 없음
    assert "bbox" not in region and "confidence" not in region and "source" not in region


def test_llm_view_orders_regions_top_to_bottom_left_to_right():
    """섹션 라벨이 사라진 만큼 순서가 문맥을 담는다 — 화면 흐름대로 나와야 한다."""
    doc = _doc(AdPage(
        page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
        regions=[
            Region(region_id="아래", bbox=[0, 300, 200, 350], role="유의사항",
                   lines=[Line(text="유의사항", bbox=[0, 300, 200, 340], source="ocr")]),
            Region(region_id="위-오른쪽", bbox=[100, 0, 200, 50], role="본문",
                   lines=[Line(text="오른쪽", bbox=[100, 0, 200, 40], source="ocr")]),
            Region(region_id="위-왼쪽", bbox=[0, 0, 90, 50], role="제목",
                   lines=[Line(text="왼쪽", bbox=[0, 0, 90, 40], source="ocr")]),
        ],
    ))
    ids = [r["region_id"] for r in llm_view.build_doc_view(doc)["pages"][0]["regions"]]
    assert ids == ["위-왼쪽", "위-오른쪽", "아래"]


def test_llm_view_keeps_unassigned_lines():
    """어느 영역에도 못 붙은 낱줄도 텍스트는 전달돼야 한다 (근거 지목만 불가)."""
    doc = _doc(AdPage(
        page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
        regions=[Region(region_id="p1_r000", bbox=[0, 0, 200, 50], role="제목",
                        lines=[Line(text="헤드라인", bbox=[0, 0, 200, 40], source="ocr")])],
        unassigned_lines=[Line(text="준법감시인 심의필 2026-0000",
                               bbox=[0, 380, 200, 400], source="ocr")],
    ))
    page = llm_view.build_doc_view(doc)["pages"][0]
    assert page["unassigned"] == "준법감시인 심의필 2026-0000"


def test_llm_view_exposes_vlm_candidate_only_when_it_differs():
    """정본과 다른 통독 후보만 병존 노출 — 같으면 노이즈라 안 싣는다."""
    same = Region(region_id="p1_r000", bbox=[0, 0, 200, 50], role="제목",
                  vlm_reading="헤드라인", vlm_reading_score=1.0,
                  lines=[Line(text="헤드라인", bbox=[0, 0, 200, 40], source="ocr")])
    diff = Region(region_id="p1_r001", bbox=[0, 60, 200, 110], role="본문",
                  vlm_reading="① 0.1%p", vlm_reading_score=0.5,
                  vlm_reading_relation="diverged",
                  lines=[Line(text="1O.1%p", bbox=[0, 60, 200, 100], source="ocr")])
    doc = _doc(AdPage(page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
                      regions=[same, diff]))
    regions = llm_view.build_doc_view(doc)["pages"][0]["regions"]
    assert "vlm_reading" not in regions[0]
    assert regions[1]["vlm_reading"] == "① 0.1%p"
    assert regions[1]["vlm_reading_relation"] == "diverged"
