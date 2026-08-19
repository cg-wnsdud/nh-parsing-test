# -*- coding: utf-8 -*-
"""미발견 필드 3분류(해당없음/미표시/확인필요) 회귀 테스트.

이 판정이 제품의 최종 산출물이다 — 잘못 가르면 (a) 없는 지적사항을 만들어내거나
(b) 진짜 지적사항을 '해당없음'으로 조용히 삼킨다. 둘 다 심의 업무에서 치명적이라
실측 케이스를 그대로 고정해 둔다.

기준 케이스(2026-07-28 out/extracted 실측):
  002 는 적금인데 중도해지이율이 없다 → 미표시(지적사항)
  001·003 은 수시입출식이라 중도해지·만기 개념 자체가 없다 → 해당없음
"""

import pytest

from nh_parsing.applicability import (
    check_schema_metadata, classify_absences, derived_rules, input_gap,
)
from nh_parsing.schema_pack import load_pack


@pytest.fixture(scope="module")
def pack():
    return load_pack("예금성", "이벤트페이지")


def _result(subtype, not_found: list[str], found: list[str] | None = None) -> dict:
    """subtype 은 문자열 하나 또는 목록. 스키마 v2 부터 세부유형은 배열이다."""
    fields: dict = {
        "deposit_subtypes": {
            "value": [subtype] if isinstance(subtype, str) else list(subtype),
            "status": "found", "evidence": [],
        },
    }
    for k in found or []:
        fields[k] = {"value": "값", "status": "found", "evidence": []}
    for k in not_found:
        fields[k] = {"value": "", "status": "not_found", "evidence": []}
    return {"fields": fields, "events": [], "ad_type": "이벤트페이지"}


# ───────────────────────── 상품유형 조건 ─────────────────────────


def test_적립식_중도해지이율_미표시는_지적사항(pack):
    """002 실측 — 적립식에 중도해지이율이 없으면 파싱 실패가 아니라 광고의 결함이다."""
    result = _result("적립식", ["early_termination_rate", "post_maturity_rate"])
    gaps = classify_absences(result, pack)

    flagged = {m["field_key"] for m in gaps["미표시"]}
    assert "early_termination_rate" in flagged
    assert "post_maturity_rate" in flagged
    assert result["fields"]["early_termination_rate"]["absence"]["obligation"] == "필수"


def test_수시입출식은_만기항목이_해당없음(pack):
    """001·003 실측 — 만기가 없는 통장에 만기후이율을 요구하면 오탐이다."""
    keys = ["early_termination_rate", "post_maturity_rate",
            "maturity_interest_example", "contract_period"]
    result = _result("입출식", keys)
    gaps = classify_absences(result, pack)

    assert gaps["미표시"] == []
    assert set(gaps["해당없음"]) == set(keys)
    assert result["fields"]["contract_period"]["absence"]["rule"] == "subtype_not_in"


def test_적립식전용_항목은_다른_유형에서_해당없음(pack):
    result = _result("입출식", ["installment_type"])
    gaps = classify_absences(result, pack)
    assert gaps["미표시"] == []

    result = _result("적립식", ["installment_type"])
    gaps = classify_absences(result, pack)
    assert len(gaps["미표시"]) == 1, "적립식이면 적립방법은 표시 의무다"


# ─────────────── 세부유형이 배열이 된 뒤 생긴 두 가지 함정 ───────────────


def test_복수유형_광고는_한쪽만_맞아도_대상이다(pack):
    """'거치식·적립식 통합' 광고가 실재한다(실데이터 3건).

    적립식이 섞여 있으면 만기 개념이 성립하므로, 만기 관련 항목을 '해당없음'으로
    빼면 진짜 지적사항이 조용히 사라진다.
    """
    keys = ["early_termination_rate", "post_maturity_rate", "contract_period"]
    result = _result(["거치식", "적립식"], keys)
    gaps = classify_absences(result, pack)

    assert gaps["해당없음"] == [], "한쪽 유형에 성립하면 해당없음이 아니다"
    assert {m["field_key"] for m in gaps["미표시"]} == set(keys)


def test_스키마가_모르는_유형값은_확인필요로_간다(pack):
    """세부유형 용어를 바꿨을 때 실제로 열렸던 조용한 실패 경로의 회귀 테스트.

    구 용어 '입출금(통장·MMDA)' 는 신 스키마 enum 에 없다. 검증이 없으면
    `subtype_not_in: ["입출식"]` 이 "목록에 없음 = 참" 으로 그냥 통과해서
    필드가 '필수·전 광고' 로 평가되고 **없던 미표시 지적이 생긴다.**
    """
    keys = ["early_termination_rate", "contract_period"]
    result = _result("입출금(통장·MMDA)", keys)
    gaps = classify_absences(result, pack)

    assert gaps["미표시"] == [], "모르는 유형값으로 허위 지적을 만들면 안 된다"
    assert set(gaps["확인필요"]) == set(keys)
    assert gaps["subtype_unknown"] is True


def test_유형을_못정하면_해당없음이_아니라_확인필요(pack):
    """'판단불가'를 해당없음으로 밀면 지적사항이 조용히 사라진다."""
    result = _result("판단불가", ["early_termination_rate"])
    gaps = classify_absences(result, pack)

    assert gaps["미표시"] == []
    assert gaps["확인필요"] == ["early_termination_rate"]
    assert result["fields"]["early_termination_rate"]["absence"]["rule"] == "subtype_unknown"


# ───────────────────────── 방아쇠·자기참조 조건 ─────────────────────────


def test_최고금리를_표시했으면_기본금리는_의무(pack):
    result = _result("적금", ["base_rate"], found=["max_rate"])
    gaps = classify_absences(result, pack)
    assert [m["field_key"] for m in gaps["미표시"]] == ["base_rate"]


def test_최고금리가_없으면_기본금리는_해당없음(pack):
    result = _result("적금", ["base_rate", "max_rate"])
    gaps = classify_absences(result, pack)
    assert gaps["미표시"] == []
    assert set(gaps["해당없음"]) == {"base_rate", "max_rate"}


def test_자기참조_조건부_항목은_없으면_해당없음(pack):
    """수상 표기가 없는 광고에 수상 시기를 요구하면 안 된다."""
    keys = ["award_cert_info", "stats_source", "endorsement_disclosure",
            "ai_generated_notice", "tax_benefit"]
    result = _result("적금", keys)
    gaps = classify_absences(result, pack)
    assert gaps["미표시"] == []
    assert set(gaps["해당없음"]) == set(keys)


# ───────────────────────── 의무등급 ─────────────────────────


def test_판정대상이_아닌_항목은_판정제외(pack):
    """분류축·전수수집 배열·광고물 밖의 절차는 '없다'가 지적사항이 아니다."""
    result = _result("적금", ["rate_mentions", "compliance_procedure_declaration",
                              "other_notices"])
    gaps = classify_absences(result, pack)
    assert gaps["미표시"] == []
    assert set(gaps["판정제외"]) == {"rate_mentions", "compliance_procedure_declaration",
                                    "other_notices"}


def test_권장항목은_의무등급이_구분된다(pack):
    result = _result("적금", ["deposit_amount"])
    gaps = classify_absences(result, pack)
    assert gaps["미표시"] == [{"field_key": "deposit_amount", "obligation": "권장"}]


def test_스키마에_없는_키는_조용히_넘기지_않는다(pack):
    result = _result("적금", ["존재하지_않는_필드"])
    gaps = classify_absences(result, pack)
    assert gaps["확인필요"] == ["존재하지_않는_필드"]


# ───────────────────────── 이벤트 배열 ─────────────────────────


def test_이벤트는_이벤트별로_판정한다(pack):
    """한 이벤트에 경품이 없는 것과 다른 이벤트에 없는 것은 별개 지적이다."""
    result = _result("적금", [])
    result["events"] = [
        {"event_name": {"value": "A", "status": "found"},
         "event_prize": {"value": "", "status": "not_found"}},
        {"event_name": {"value": "B", "status": "found"},
         "event_prize": {"value": "포인트", "status": "found"}},
    ]
    gaps = classify_absences(result, pack)
    assert [m["field_key"] for m in gaps["미표시"]] == ["event1.event_prize"]


# ───────────────────────── (C) 입력 유실 ─────────────────────────


def test_입력에서_빠진_영역만_유실로_보고한다():
    # 투영은 2026-08-03 부터 pages → regions 평면 구조 (섹션 계층 제거)
    view = {"pages": [{"regions": [{"region_id": "p1_r000", "text": "실린 영역"}]}]}
    parse_doc = {"pages": [{"page_no": 1, "regions": [
        {"region_id": "p1_r000", "lines": [{"text": "실린 영역"}]},
        {"region_id": "p1_r001", "lines": [{"text": "빠진 문구"}], "is_illustrative": True},
        {"region_id": "p1_r002", "lines": []},  # 텍스트 없음 — 유실이 아니다
    ]}]}
    gaps = input_gap(view, parse_doc)

    assert [g["region_id"] for g in gaps] == ["p1_r001"]
    assert gaps[0]["reason"] == "장식예시 격리"
    assert gaps[0]["text"] == "빠진 문구"


def test_파싱원본이_없으면_유실_판단을_안_한다():
    assert input_gap({"pages": []}, None) == []


# ───────────────────────── 스키마 자체 점검 ─────────────────────────


def test_모든_필드가_의무등급과_적용조건을_갖췄다(pack):
    """빠진 필드는 기본값('필수'·조건없음)으로 평가돼 없던 지적사항을 만든다."""
    assert check_schema_metadata(pack) == []


def test_해석으로_넣은_조건은_사유가_붙어_있다(pack):
    """조문에 없는 판단은 반드시 why 를 달아 사람이 승인할 수 있게 한다."""
    derived = derived_rules(pack)
    assert derived, "derived 조건이 하나도 없으면 리포트가 무의미하다"
    assert all(d["why"] for d in derived)


# ───────────────────────── 미배정 텍스트의 근거 ID ─────────────────────────


def test_미배정_텍스트도_근거로_지목할_수_있다():
    """근거 ID 가 없으면 전수수집 배열('표기 그대로 (region_id)')에 담을 수가 없다.

    003 실측: 배너의 'NH Benefit 2025.10.01-2025.10.31' 이 STAGE_3 입력에는 있었는데
    댈 ID 가 없어 period_mentions 에서 조용히 빠졌다.
    """
    from nh_parsing.extract import _region_texts, _render_doc

    view = {"pages": [{
        "page_number": 1,
        "sections": [{"regions": [{"region_id": "p1_r000", "role": "본문", "text": "본문"}]}],
        "unassigned": "NH Benefit 2025.10.01- 2025.10.31",
    }]}

    rendered = _render_doc(view)
    assert "p1_unassigned" in rendered, "미배정 덩어리에 지목할 ID 가 있어야 한다"
    assert "2025.10.01" in rendered

    texts = _region_texts(view)
    assert "p1_unassigned" in texts, "근거 대조 대상에도 있어야 환각으로 오판되지 않는다"
    assert "2025.10.01" in texts["p1_unassigned"]


def test_미배정이_없으면_가상_ID를_만들지_않는다():
    from nh_parsing.extract import _region_texts

    view = {"pages": [{"page_number": 1, "sections": [], "unassigned": ""}]}
    assert "p1_unassigned" not in _region_texts(view)


# ───────────────────────── 라인 단위 후보 (결함 수정 2026-08-12) ─────────────────────────


def test_라인후보가_프롬프트에_찍히고_근거대조_대상에도_들어간다():
    """llm_view 가 line_candidates 를 실어도 extract.py 가 안 읽으면 도로 유실이다.

    렌더(_render_doc)와 근거대조(_region_texts) 양쪽 다 확인한다 — 전자만 되면
    LLM 은 후보를 보지만 그 값을 쓴 필드가 '환각 의심(evidence_backed=False)'으로
    잘못 걸린다(후보 텍스트가 region_texts 에 없어서).
    """
    from nh_parsing.extract import _region_texts, _render_doc

    view = {"pages": [{
        "page_number": 1,
        "regions": [{
            "region_id": "p1_r016", "role": "본문",
            "text": "1O.1%p:[NH올원e통장]에서 출금",
            "line_candidates": [
                {"line_text": "1O.1%p:[NH올원e통장]에서 출금",
                 "vlm_reading": "① 0.1%p : 「NH올원e통장」에서 출금"},
            ],
        }],
    }]}

    rendered = _render_doc(view)
    assert "[라인후보]" in rendered
    assert "① 0.1%p" in rendered

    texts = _region_texts(view)
    assert "① 0.1%p" in texts["p1_r016"], "근거대조 대상에 없으면 이 값을 쓴 필드가 환각으로 오판된다"


# ───────────────────────── 유령 이벤트 방어 ─────────────────────────


def test_값이_하나도_없는_이벤트는_지적사항을_만들지_않는다(pack):
    """002 실측 — event_count=1 인데 배열은 2개, 두 번째가 전부 빈 값이었다.

    그대로 두면 유령 이벤트의 필수 항목이 전부 '미표시=심의 지적사항'으로 올라간다
    (실측 6건). 이벤트 개수는 실행마다 흔들리는 항목이라 코드가 검산해야 한다.
    """
    from nh_parsing.extract import prune_empty_events

    result = _result("적금", [])
    result["event_count_reported"] = 1
    result["events"] = [
        {"event_name": {"value": "행운의 이벤트", "status": "found"},
         "event_prize": {"value": "포인트", "status": "found"}},
        {"event_name": {"value": "", "status": "not_found"},
         "event_prize": {"value": "", "status": "not_found"}},
    ]

    prune_empty_events(result)
    assert len(result["events"]) == 1
    assert result["events_pruned"]["dropped_indexes"] == [2]
    assert result["events_pruned"]["event_count_reported"] == 1

    assert classify_absences(result, pack)["미표시"] == []


def test_값이_있는_이벤트는_남긴다():
    """일부만 비어도 실제 이벤트일 수 있다 — 한 칸이라도 값이 있으면 판정 대상이다."""
    from nh_parsing.extract import prune_empty_events

    result = {"events": [
        {"event_name": {"value": "A", "status": "found"},
         "event_prize": {"value": "", "status": "not_found"}},
    ]}
    prune_empty_events(result)
    assert len(result["events"]) == 1
    assert "events_pruned" not in result


def test_인용문이_빈_관측은_하류로_넘기지_않는다():
    """2026-08-19 v2 실측 — 관측항목 49건 중 41건이 빈 quote 였다.

    관측할 게 없으면 빈 배열을 내야 하는데, strict 스키마가 항목의 '모양'만 강제하고
    '만들지 마라'는 강제하지 못해 {quote:"", evidence:[], why:""} 한 칸이 채워져 온다.
    관측은 하류가 그대로 신뢰하는 '의심 후보'라 유령을 넘기면 없는 위반이 생긴다.
    """
    from nh_parsing.extract import prune_empty_observations

    result = {"observations": {
        "obs_superlative": [
            {"quote": "업계 유일", "evidence": ["p1_r003"], "why": "최상급"},
            {"quote": "", "evidence": [], "why": ""},
        ],
        "obs_definitive_expression": [{"quote": "   ", "evidence": [], "why": ""}],
        "obs_comparison": [],
    }}
    prune_empty_observations(result)

    assert len(result["observations"]["obs_superlative"]) == 1
    assert result["observations"]["obs_definitive_expression"] == []
    assert result["observations_pruned"]["total"] == 2
    assert result["observations_pruned"]["dropped_per_field"] == {
        "obs_superlative": 1, "obs_definitive_expression": 1,
    }


def test_인용문이_있는_관측만_있으면_기록을_남기지_않는다():
    from nh_parsing.extract import prune_empty_observations

    result = {"observations": {
        "obs_superlative": [{"quote": "최초", "evidence": ["p1_r001"], "why": "최상급"}],
    }}
    prune_empty_observations(result)
    assert len(result["observations"]["obs_superlative"]) == 1
    assert "observations_pruned" not in result
