# -*- coding: utf-8 -*-
"""STAGE_3 출력 계약(pydantic) 검증 — extract_models.py + extract.py 의 두 경계.

원래 계획(docs/previous/schema-layer-plan_2026-07-23.md §1-A)엔 "strict json_schema
디코딩 강제 + pydantic 재검증"이 같이 있었는데, 실제로는 강제만 구현되고 재검증은
빠진 채 굳어졌다. 여기서 그 빠진 절반을 검증한다 — ①VLM 원값이 진짜로 계약을
지켰는지, ②우리 코드가 최종 결과를 만들 때 계약을 어기지 않는지.
"""

import pytest
from pydantic import ValidationError

from nh_parsing.extract_models import (
    AbsenceInfo, ExtractResult, FieldCell, ObservationItem, UnmappedItem,
)


def _cell(**over):
    base = {"value": "", "evidence": [], "status": "not_found", "note": ""}
    base.update(over)
    return base


# ───────────────────────── FieldCell ─────────────────────────


def test_원값_네_필드만_있어도_통과한다():
    """VLM 이 막 돌려준 원값 — 사후 필드(group/absence 등)는 아직 없다."""
    FieldCell.model_validate(_cell(value="NH올원e적금", status="found"))


def test_사후_필드가_붙은_최종형도_통과한다():
    FieldCell.model_validate(_cell(
        value="NH올원e적금", status="found", group="G1_상품기본",
        evidence_score=1.0, evidence_backed=True,
    ))


def test_배열형_값도_통과한다():
    """rate_mentions 같은 전수수집 필드는 value 가 배열이다."""
    FieldCell.model_validate(_cell(value=["최고 연 3.6%", "기본 3.1%"], status="found"))


def test_모르는_status_는_거부한다():
    """서버가 strict 계약을 실제로 어긴 경우 — guided decoding 이 100%는 아니다."""
    with pytest.raises(ValidationError):
        FieldCell.model_validate(_cell(status="완료"))  # STATUS_ENUM 에 없는 값


def test_정의에_없는_키가_섞이면_거부한다():
    """서버가 스키마에 없는 여분 키를 얹어 보내는 경우까지 잡는다."""
    with pytest.raises(ValidationError):
        FieldCell.model_validate(_cell(unexpected_key="x"))


def test_absence_는_4분류_밖의_값을_거부한다():
    with pytest.raises(ValidationError):
        AbsenceInfo.model_validate({"kind": "모름", "obligation": "필수", "rule": "x"})


# ───────────────────────── ObservationItem / UnmappedItem ─────────────────────────


def test_observation_은_판정_필드가_없다():
    """G4 는 관측이지 판정이 아니다 — status/absence 를 넣으면 거부돼야 한다."""
    ObservationItem.model_validate({"quote": "무조건", "evidence": ["p1_r1"], "why": "단정적 표현"})
    with pytest.raises(ValidationError):
        ObservationItem.model_validate({"quote": "x", "evidence": [], "why": "y", "status": "found"})


def test_unmapped_은_사후_재분류값도_받는다():
    """'다른항목에서_처리됨'은 LLM 이 안 내고 _reclassify_already_handled 가 사후에 붙인다."""
    UnmappedItem.model_validate({
        "text": "x", "evidence": [], "kind": "다른항목에서_처리됨", "reason": "중복",
        "groups": ["G1_상품기본", "G2_금리"],  # _dedupe_unmapped 가 group→groups 로 합침
    })


def test_unmapped_은_스키마_밖_kind_를_거부한다():
    with pytest.raises(ValidationError):
        UnmappedItem.model_validate({"text": "x", "evidence": [], "kind": "아무거나", "reason": "y"})


# ───────────────────────── ExtractResult (경계 ②: extract_document 최종형) ─────────────────────────


def _minimal_result(**over):
    base = {
        "schema_id": "예금성", "schema_version": "v1",
        "fields": {"product_name": _cell(value="NH올원e적금", status="found")},
    }
    base.update(over)
    return base


def test_최소_결과도_기본값으로_채워져_통과한다():
    r = ExtractResult.model_validate(_minimal_result())
    assert r.overlays_applied == [] and r.events == []


def test_events_는_FieldCell_의_배열이다():
    r = ExtractResult.model_validate(_minimal_result(
        events=[{"event_name": _cell(value="EVENT 1", status="found")}]
    ))
    assert r.events[0]["event_name"].value == "EVENT 1"


def test_그룹_실패_기록도_스키마의_일부다():
    r = ExtractResult.model_validate(_minimal_result(
        errors=[{"group": "G2_금리", "error": "VLM 호출 실패(3회): timeout"}]
    ))
    assert r.errors[0].group == "G2_금리"


def test_정의에_없는_최상위_키는_거부한다():
    with pytest.raises(ValidationError):
        ExtractResult.model_validate(_minimal_result(오타필드="x"))


def test_model_dump_은_None_값_키를_뺀다():
    """사후 필드가 안 붙은 칸은 원래 dict 에 그 키가 아예 없었다 — model_dump 도 같아야
    on-disk JSON 모양이 달라지지 않는다(기존 소비자의 .get() 호출과 호환)."""
    r = ExtractResult.model_validate(_minimal_result())
    dumped = r.model_dump(mode="json", exclude_none=True)
    cell = dumped["fields"]["product_name"]
    assert "status_corrected" not in cell and "absence" not in cell
    assert "event_count_reported" not in dumped
