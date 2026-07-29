# -*- coding: utf-8 -*-
"""영역별 VLM 통독(§6 ④+) 병존 방식 + lean 투영 단위테스트 — 폐쇄망(VLM 서버) 불필요.

핵심 설계결정 ②를 고정한다: 통독 clean text 는 region.lines(본문·다운스트림 소비),
원본 OCR 라인은 region.ocr_lines(증거층)로 분리한다. VLM 배치 호출은 monkeypatch 로
대체하므로 WireGuard/Gemma 없이 로직만 검증한다.
"""

from PIL import Image

from nh_parsing import llm_view, pipeline, vlm_direct
from nh_parsing.ir import AdDocument, AdPage, ExtractedField, Line, Region, Section


def _page_with_regions() -> AdPage:
    return AdPage(
        page_no=1,
        canvas_w=200,
        canvas_h=400,
        parse_route="ocr",
        regions=[
            Region(
                region_id="p1_r000",
                bbox=[0, 0, 200, 50],
                role="제목",
                lines=[
                    Line(text="최고연", bbox=[0, 0, 100, 40], confidence=0.99, source="ocr"),
                    Line(text="777", bbox=[100, 0, 200, 40], confidence=0.98, source="ocr"),
                ],
            ),
            Region(  # 순수 이미지 영역(라인 없음) — 통독 대상 아님
                region_id="p1_r001",
                bbox=[0, 60, 200, 120],
                role="이미지",
                lines=[],
            ),
            Region(  # 통독이 빈 값 반환 → OCR 유지되어야 함
                region_id="p1_r002",
                bbox=[0, 130, 200, 180],
                role="본문",
                lines=[Line(text="원본유지", bbox=[0, 130, 200, 170], confidence=0.9, source="ocr")],
            ),
            Region(  # 디지털 정본 라인만 — 통독 대상 아님(재판독 무의미)
                region_id="p1_r003",
                bbox=[0, 190, 200, 240],
                role="본문",
                lines=[Line(text="디지털본문", bbox=[0, 190, 200, 230], source="digital")],
            ),
        ],
    )


def test_region_vlm_attaches_candidate_and_keeps_ocr_authoritative(monkeypatch):
    """B안: 통독은 OCR 정본(region.lines)을 대체하지 않고 region.vlm_reading 후보로만 붙는다."""
    page = _page_with_regions()

    def fake_batch(bboxes, canvas, batch_size=4):
        # 대상은 OCR 라인 있는 r000, r002 뿐(r001 라인없음·r003 디지털은 제외)
        assert bboxes == [[0, 0, 200, 50], [0, 130, 200, 180]]
        return [("최고 연 7.1%", 0.95), ("", None)]

    monkeypatch.setattr(vlm_direct, "transcribe_region_crops", fake_batch)
    pipeline._transcribe_regions_vlm(page, Image.new("RGB", (200, 400), "white"))

    r0, r1, r2, r3 = page.regions

    # 정본(OCR)은 그대로, 통독은 후보로만 부착
    assert [l.text for l in r0.lines] == ["최고연", "777"]
    assert r0.lines[0].source == "ocr"
    assert r0.ocr_lines == []                       # 강등 없음(B안)
    assert r0.vlm_reading == "최고 연 7.1%"          # 후보만 부착
    assert r0.vlm_reading_score is not None

    # 라인 없는 이미지 영역: 손대지 않음
    assert r1.lines == [] and r1.vlm_reading is None
    # 통독 빈 결과 영역: OCR 유지, 후보 없음
    assert [l.text for l in r2.lines] == ["원본유지"] and r2.vlm_reading is None
    # 디지털 정본 영역: 통독 대상 자체가 아님(그대로)
    assert [l.text for l in r3.lines] == ["디지털본문"] and r3.vlm_reading is None


def test_region_vlm_batches_and_records_note(monkeypatch):
    page = _page_with_regions()
    seen_batch = {}

    def fake_batch(bboxes, canvas, batch_size=4):
        seen_batch["size"] = batch_size
        return [("최고 연 7.1%", 0.95), ("원본유지", 0.9)]

    monkeypatch.setattr(vlm_direct, "transcribe_region_crops", fake_batch)
    pipeline._transcribe_regions_vlm(page, Image.new("RGB", (200, 400), "white"))
    assert seen_batch["size"] == pipeline.SETTINGS.region_vlm_crops_per_call
    # 조용한 수정 금지: 후보 영역마다 정밀도/커버리지 note + 요약 note
    assert any("영역 통독 후보 p1_r000" in n and "정밀도=" in n for n in page.notes)
    assert any("영역별 VLM 통독(§6, B안 후보)" in n for n in page.notes)


def test_region_vlm_rereads_leaked_batch_as_single_crop(monkeypatch):
    """배치 leakage 로 통독이 옆 셀 내용과 뒤섞이면(ocr_score 낮음) 그 영역만 단일
    크롭으로 재통독해 더 정합한 쪽을 채택한다. 올원e 상품안내표 r005~r007 오정렬 회귀 방지.
    """
    page = _page_with_regions()
    calls = {"batch": [], "single": []}

    def fake_batch(bboxes, canvas, batch_size=4):
        if batch_size == 1:  # 단일 재통독 경로 — leakage 없는 정확한 판독
            calls["single"].append(bboxes)
            # r000(=OCR '최고연 777')의 정확한 통독. 정합도 높아 채택되어야 함.
            return [("최고연 777", 0.97)]
        calls["batch"].append(bboxes)
        # 배치본: r000 에 엉뚱한 옆 셀 내용이 새어들어옴(ocr_score≈0), r002 는 정상.
        return [("전혀 다른 셀 텍스트", 0.9), ("원본유지", 0.9)]

    monkeypatch.setattr(vlm_direct, "transcribe_region_crops", fake_batch)
    pipeline._transcribe_regions_vlm(page, Image.new("RGB", (200, 400), "white"))

    r0, _, r2, _ = page.regions
    # 저신뢰 r000 후보는 단일 재통독으로 정정, r002(정합)는 배치본 유지(재통독 안 함)
    assert r0.vlm_reading == "최고연 777"           # 후보가 단일본으로 교체됨
    assert [l.text for l in r0.lines] == ["최고연", "777"]  # 정본 OCR 은 불변
    assert calls["single"] == [[[0, 0, 200, 50]]]  # r000 만 단일 재통독
    assert r2.vlm_reading == "원본유지"
    assert any("단일 재통독 채택" in n for n in page.notes)


def test_region_vlm_keeps_batch_when_single_not_more_grounded(monkeypatch):
    """정당한 교정(회전 헤드라인 등)은 단일본도 똑같이 ocr_score 가 낮으므로, 단일본이
    배치본보다 더 정합하지 않으면 배치본(=교정)을 유지해 역회귀를 막는다.
    """
    page = _page_with_regions()

    def fake_batch(bboxes, canvas, batch_size=4):
        if batch_size == 1:
            return [("최고 연 7.1%", 0.95)]  # 단일본도 동일한 교정 → 점수 동률
        return [("최고 연 7.1%", 0.95), ("원본유지", 0.9)]  # r000 회전 교정

    monkeypatch.setattr(vlm_direct, "transcribe_region_crops", fake_batch)
    pipeline._transcribe_regions_vlm(page, Image.new("RGB", (200, 400), "white"))

    r0 = page.regions[0]
    # 회전 교정 후보가 살아남음(단일본이 더 정합하지 않으면 배치 후보 유지)
    assert r0.vlm_reading == "최고 연 7.1%"
    assert [l.text for l in r0.lines] == ["최고연", "777"]  # 정본 OCR 불변


def test_region_vlm_keeps_ocr_when_transcription_is_truncated_subset(monkeypatch):
    """통독이 '정밀도 높음 + 커버리지 낮음'(OCR 순부분집합=값 절단)이면 승격하지 않고
    OCR 라인을 유지한다. 올원e 상품안내표에서 통독이 라벨만 읽고 값을 빠뜨린 실측 회귀 방지.
    """
    page = AdPage(
        page_no=1, canvas_w=200, canvas_h=200, parse_route="ocr",
        regions=[
            Region(
                region_id="p1_r000", bbox=[0, 0, 200, 50], role="본문",
                lines=[
                    Line(text="가입금액", bbox=[0, 0, 60, 40], confidence=0.99, source="ocr"),
                    Line(text="1천원 이상 30만원 이하", bbox=[70, 0, 200, 40], confidence=0.99, source="ocr"),
                ],
            ),
        ],
    )

    def fake_batch(bboxes, canvas, batch_size=4):
        # 배치·단일 모두 라벨만(값 절단) — 통독의 이 크롭 판독 변동성 재현
        return [("가입금액", 0.9)]

    monkeypatch.setattr(vlm_direct, "transcribe_region_crops", fake_batch)
    pipeline._transcribe_regions_vlm(page, Image.new("RGB", (200, 200), "white"))

    r0 = page.regions[0]
    # B안: OCR 정본은 항상 유지. 통독은 후보로 붙되 '절단 의심' 표시로 성격이 드러남.
    assert [l.text for l in r0.lines] == ["가입금액", "1천원 이상 30만원 이하"]
    assert r0.lines[0].source == "ocr"
    assert r0.vlm_reading == "가입금액"      # 절단된 후보(값 셀 누락)
    assert any("절단 의심" in n for n in page.notes)


def test_transcribe_region_crops_maps_index_and_survives_batch_failure(monkeypatch):
    """배치 호출 결과를 index 로 전역 위치에 되매핑하고, 배치 실패는 그 배치만 원값 유지."""
    calls = {"n": 0}

    def fake_chat_json(parts, schema_name, schema, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:  # 첫 배치: 순서 뒤섞인 응답도 index 로 정렬
            return {"analysis": "", "regions": [
                {"index": 1, "text": "B", "confidence": 0.9},
                {"index": 0, "text": "A", "confidence": 0.8},
            ]}
        raise RuntimeError("두 번째 배치 서버 오류")

    monkeypatch.setattr(vlm_direct, "chat_json", fake_chat_json)
    canvas = Image.new("RGB", (300, 300), "white")
    bboxes = [[0, 0, 100, 40], [0, 50, 100, 90], [0, 100, 100, 140]]
    out = vlm_direct.transcribe_region_crops(bboxes, canvas, batch_size=2)
    assert out[0] == ("A", 0.8)      # index 되매핑 정상
    assert out[1] == ("B", 0.9)
    assert out[2] == ("", None)      # 실패한 두 번째 배치만 원값 유지


def test_verify_numeric_skips_transcribed_region_fields(monkeypatch):
    """통독된 영역(영역 bbox 앵커)에서 뽑힌 수치 필드는 크롭 재판독을 건너뛴다.

    회귀 방지: 우대금리 ①②③④ 가 같은 영역 bbox 를 공유할 때, 크롭 재판독이 영역
    전체를 다시 읽어 형제 필드값을 덮어쓰던 문제(002 배치 실측).
    """
    region_bbox = [0, 100, 200, 300]
    fields = [
        ExtractedField(key="우대금리", value="① 당행 첫 거래 : 3.8%p", bbox=region_bbox, source="vlm"),
        ExtractedField(key="우대금리", value="② ... : 1.0%p", bbox=region_bbox, source="vlm"),
    ]

    def boom(*a, **k):
        raise AssertionError("통독 영역 필드는 크롭 재판독을 호출하면 안 된다")

    monkeypatch.setattr(vlm_direct, "chat_json", boom)
    notes = vlm_direct.verify_numeric_fields(
        fields, Image.new("RGB", (300, 400), "white"),
        skip_bboxes={tuple(region_bbox)},
    )
    # 값이 그대로 보존됨(덮어쓰기 없음)
    assert [f.value for f in fields] == ["① 당행 첫 거래 : 3.8%p", "② ... : 1.0%p"]
    assert any("건너뜀 2건" in n for n in notes)


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
