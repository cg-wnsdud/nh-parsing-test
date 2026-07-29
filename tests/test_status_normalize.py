# -*- coding: utf-8 -*-
"""필드 status/value 정합 보정 회귀 테스트.

이 지표(fields_found / fields_not_found)로 "무엇을 못 뽑았나"를 판단하므로,
값과 상태가 어긋난 채 통과하면 집계 자체가 거짓이 된다. 실측(002): LLM 이 값 칸에
'not_found' 라는 글자를 적어 보내 status="found" 로 통과, 회수 3건이 부풀려졌다.
"""

from nh_parsing.extract import _is_empty_value, _normalize_status


def _run(value, status):
    val = {"value": value, "status": status, "evidence": []}
    result: dict = {}
    _normalize_status(val, "some_field", result)
    return val, result


def test_sentinel_string_in_value_is_treated_as_empty():
    """002 실측 케이스 — value 에 'not_found' 라는 글자가 들어온 채 found 로 온다."""
    val, result = _run("not_found", "found")
    assert val["status"] == "not_found"
    assert val["value"] == ""
    assert result["status_corrections"] == ["some_field"], "보정은 한 번만 기록돼야 한다"
    assert "not_found" in val["status_corrected"]


def test_korean_absence_words_are_empty():
    """'없음', '해당없음' 같은 우리말 표현도 값이 아니라 부재 표시다."""
    for word in ("없음", "해당 없음", "미표시", "-"):
        val, _ = _run(word, "found")
        assert val["status"] == "not_found", f"{word} 를 값으로 취급했다"
        assert val["value"] == ""


def test_real_value_is_untouched():
    """정상 값은 손대지 않는다 — 과잉 보정으로 실제 값을 지우면 안 된다."""
    val, result = _run("연 2.3%", "found")
    assert val == {"value": "연 2.3%", "status": "found", "evidence": []}
    assert result == {}


def test_value_present_but_status_not_found_is_corrected():
    """기존 보정(G2 금리 그룹 실측)이 그대로 동작해야 한다."""
    val, result = _run("연 최고 7.1%", "not_found")
    assert val["status"] == "found"
    assert result["status_corrections"] == ["some_field"]


def test_empty_list_with_uncertain_becomes_not_found():
    """값 없이 uncertain 인 항목은 판단 근거가 없으므로 미발견이다."""
    val, _ = _run([], "uncertain")
    assert val["status"] == "not_found"


def test_list_of_sentinels_is_empty():
    """배열 항목도 '없음'만 들어 있으면 빈 것으로 본다."""
    assert _is_empty_value(["없음"]) is True
    assert _is_empty_value(["1.0%p : 첫거래"]) is False
