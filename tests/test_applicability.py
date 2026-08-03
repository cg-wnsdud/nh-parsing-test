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


def _result(subtype: str, not_found: list[str], found: list[str] | None = None) -> dict:
    fields: dict = {
        "product_subtype": {"value": subtype, "status": "found", "evidence": []},
    }
    for k in found or []:
        fields[k] = {"value": "값", "status": "found", "evidence": []}
    for k in not_found:
        fields[k] = {"value": "", "status": "not_found", "evidence": []}
    return {"fields": fields, "events": [], "ad_type": "이벤트페이지"}


# ───────────────────────── 상품유형 조건 ─────────────────────────


def test_적금_중도해지이율_미표시는_지적사항(pack):
    """002 실측 — 적금에 중도해지이율이 없으면 파싱 실패가 아니라 광고의 결함이다."""
    result = _result("적금", ["early_termination_rate", "post_maturity_rate"])
    gaps = classify_absences(result, pack)

    flagged = {m["field_key"] for m in gaps["미표시"]}
    assert "early_termination_rate" in flagged
    assert "post_maturity_rate" in flagged
    assert result["fields"]["early_termination_rate"]["absence"]["obligation"] == "필수"


def test_수시입출식은_만기항목이_해당없음(pack):
    """001·003 실측 — 만기가 없는 통장에 만기후이율을 요구하면 오탐이다."""
    keys = ["early_termination_rate", "post_maturity_rate",
            "maturity_interest_example", "contract_period"]
    result = _result("입출금(통장·MMDA)", keys)
    gaps = classify_absences(result, pack)

    assert gaps["미표시"] == []
    assert set(gaps["해당없음"]) == set(keys)
    assert result["fields"]["contract_period"]["absence"]["rule"] == "subtype_not_in"


def test_적금전용_항목은_다른_유형에서_해당없음(pack):
    result = _result("입출금(통장·MMDA)", ["installment_type", "deposit_kind"])
    gaps = classify_absences(result, pack)
    assert gaps["미표시"] == []

    result = _result("적금", ["installment_type", "deposit_kind"])
    gaps = classify_absences(result, pack)
    assert len(gaps["미표시"]) == 2, "적금이면 적립방법·예금종류는 표시 의무다"


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
