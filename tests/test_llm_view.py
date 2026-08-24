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


def test_llm_view_moves_clearly_out_of_order_regions_up():
    """섹션 라벨이 사라진 만큼 순서가 문맥을 담는다 — 확실히 거꾸로면 올려야 한다.

    **계약이 바뀐 자리다 (2026-08-24).** 예전에는 좌표로 전부 다시 정렬해
    `[위-왼쪽, 위-오른쪽, 아래]` 를 기대했다. 이제는 **레이아웃 엔진이 준 순서를 정본으로
    두고 확실히 거꾸로인 이웃만 교환**한다(`llm_view.repair_reading_order`).

    왜 바꿨나. 전부 다시 정렬하면 2단 문서가 좌우 칸을 번갈아 읽힌다 — 실측
    `2. 예금성상품(적립식).pdf` p1 에서 `우대금리`(오른쪽 y1480) 와 `가입대상`(왼쪽 y1494)
    이 14px 차이라, y 만으로는 어느 칸 소속인지 알 수 없었다.

    **포기한 것:** 같은 행에서 좌우 순서가 뒤집힌 경우는 이제 안 고친다 — 아래에서
    `위-오른쪽` 이 `위-왼쪽` 보다 먼저 나오는 것이 그 예다. 좌우 순서는 엔진을 믿는다
    (실측 5문서에서 엔진이 같은 행 좌우를 뒤집은 사례는 없었다).
    """
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
    # '아래'(y300)가 위 두 개보다 먼저 실려 있었다 → 확실히 거꾸로이므로 뒤로 밀린다.
    # 위 두 개의 좌우 순서는 엔진이 준 그대로 유지된다.
    assert ids == ["위-오른쪽", "위-왼쪽", "아래"]
    assert ids[-1] == "아래"


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


def test_llm_view_exposes_line_level_candidates_even_without_region_candidate():
    """결함 수정(2026-08-12): 라인 재판독 후보(sweep_dedupe/lowconf_reread)가

    영역 후보(vlm_reading)와 트리거가 다르다 — 밴드 통합판독이 그 영역을
    채택하지 못하면(실측: 003 p2 '밴드 통합판독 일부 미채택') region.vlm_reading
    은 비어 있고 라인 후보가 유일한 교정 신호다. 이게 STAGE_3 에 안 실리면
    이미 얻은 재판독이 통째로 버려진다.
    """
    region = Region(
        region_id="p1_r016", bbox=[0, 0, 200, 100], role="본문",
        lines=[
            Line(text="1O.1%p:[NH올원e통장]에서 출금", bbox=[0, 0, 200, 40], source="ocr",
                 vlm_reading="① 0.1%p : 「NH올원e통장」에서 출금"),
            Line(text="20.2%p:상품서비스 안내동의서", bbox=[0, 40, 200, 80], source="ocr",
                 vlm_reading="② 0.2%p : 상품서비스 안내동의서"),
        ],
    )
    doc = _doc(AdPage(page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
                      regions=[region]))
    item = llm_view.build_doc_view(doc)["pages"][0]["regions"][0]

    assert "vlm_reading" not in item  # 영역 후보는 원래 없던 상태 그대로
    assert item["line_candidates"] == [
        {"line_text": "1O.1%p:[NH올원e통장]에서 출금", "vlm_reading": "① 0.1%p : 「NH올원e통장」에서 출금"},
        {"line_text": "20.2%p:상품서비스 안내동의서", "vlm_reading": "② 0.2%p : 상품서비스 안내동의서"},
    ]


def test_llm_view_line_candidate_coexists_with_region_candidate():
    """서로 다른 관측이므로 영역 후보가 있어도 라인 후보를 조용히 버리지 않는다."""
    region = Region(
        region_id="p1_r016", bbox=[0, 0, 200, 100], role="본문",
        vlm_reading="① 0.1%p : 「NH올원e통장」에서 출금 ② 0.2%p : 상품서비스 안내동의서",
        lines=[
            Line(text="1O.1%p:[NH올원e통장]에서 출금", bbox=[0, 0, 200, 40], source="ocr",
                 vlm_reading="① 0.1%p : 「NH올원e통장」에서 출금"),
        ],
    )
    doc = _doc(AdPage(page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
                      regions=[region]))
    item = llm_view.build_doc_view(doc)["pages"][0]["regions"][0]

    assert item["vlm_reading"]  # 영역 후보 유지
    assert item["line_candidates"] == [
        {"line_text": "1O.1%p:[NH올원e통장]에서 출금", "vlm_reading": "① 0.1%p : 「NH올원e통장」에서 출금"},
    ]


def test_llm_view_skips_line_candidate_identical_to_ocr():
    """정본과 같은 라인 후보는 노이즈라 안 싣는다 (영역 후보와 같은 원칙)."""
    region = Region(
        region_id="p1_r000", bbox=[0, 0, 200, 50], role="본문",
        lines=[Line(text="가입기간 12개월", bbox=[0, 0, 200, 40], source="ocr",
                    vlm_reading="가입기간 12개월")],
    )
    doc = _doc(AdPage(page_no=1, canvas_w=200, canvas_h=400, parse_route="ocr",
                      regions=[region]))
    item = llm_view.build_doc_view(doc)["pages"][0]["regions"][0]
    assert "line_candidates" not in item
