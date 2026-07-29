# -*- coding: utf-8 -*-
"""extract_document() 의 pydantic 검증 경계 — 회귀 테스트.

경계 ①(chat_json 원값)과 ②(최종 반환값)이 실제로 걸러야 할 것만 걸러내고,
정상 흐름(44/44 를 내는 그 흐름)은 그대로 통과시키는지 확인한다.
"""

import nh_parsing.extract as extract_mod
from nh_parsing.extract import extract_document

_VIEW = {"product_group": "예금성", "ad_type": None, "doc_id": "d1", "document": "d1.png", "pages": []}


def _ok_fields_response(keys):
    return {
        "analysis": "",
        "fields": {k: {"value": "", "evidence": [], "status": "not_found", "note": ""} for k in keys},
        "unmapped": [],
    }


def _fake_reply_for(schema):
    """스키마 형태(fields/observations/events)에 맞는 최소 정상 응답을 만든다."""
    props = schema["properties"]
    if "fields" in props:
        keys = list(props["fields"]["properties"].keys())
        return _ok_fields_response(keys)
    if "observations" in props:
        return {"analysis": "", "observations": {}, "unmapped": []}
    return {"analysis": "", "event_count": 0, "events": [], "unmapped": []}


def test_정상_응답이면_경계를_그냥_통과한다(monkeypatch):
    """가장 중요한 회귀: 검증 경계가 정상 흐름까지 막으면 44/44 가 무너진다."""
    def fake(parts, schema_name, schema, max_tokens):
        return _fake_reply_for(schema)

    monkeypatch.setattr(extract_mod, "chat_json", fake)
    result = extract_document(_VIEW)
    assert not result["errors"], f"정상 응답인데 걸림: {result['errors']}"
    assert result["schema_id"] == "예금성"


def test_서버가_계약_밖의_status를_보내면_그_그룹만_스킵한다(monkeypatch):
    """guided decoding 이 100%가 아니라는 전제 — 서버가 STATUS_ENUM 밖의 값을 보낸 경우."""
    calls = {"n": 0}

    def fake(parts, schema_name, schema, max_tokens):
        calls["n"] += 1
        data = _fake_reply_for(schema)
        if "fields" in data and calls["n"] == 1:  # 첫 필드 그룹만 오염시킨다
            first = next(iter(data["fields"]))
            data["fields"][first]["status"] = "완료"  # 계약 밖의 값
        return data

    monkeypatch.setattr(extract_mod, "chat_json", fake)
    result = extract_document(_VIEW)
    assert result["errors"], "계약 위반 응답인데 아무 기록도 안 남았다"
    # 오염된 그룹의 필드는 최종 결과에 안 실려야 한다(절반만 신뢰하지 않음)
    bad_group = result["errors"][0]["group"]
    assert not any(v.get("group") == bad_group for v in result["fields"].values())


def test_최종_반환값은_ExtractResult_계약을_만족한다(monkeypatch):
    """반환값 자체가 pydantic 모델을 한 번 통과했으므로, 다시 검증해도 항상 통과해야 한다."""
    from nh_parsing.extract_models import ExtractResult

    def fake(parts, schema_name, schema, max_tokens):
        return _fake_reply_for(schema)

    monkeypatch.setattr(extract_mod, "chat_json", fake)
    result = extract_document(_VIEW)
    ExtractResult.model_validate(result)  # 다시 검증해도 예외가 나면 안 됨
