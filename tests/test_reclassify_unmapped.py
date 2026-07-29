# -*- coding: utf-8 -*-
"""unmapped 재분류 회귀 테스트 — '스키마 공백'이 실제보다 부풀지 않게 막는 장치.

호출을 5그룹으로 쪼개다 보니, 한 그룹이 이미 값으로 뽑은 문구를 다른 그룹이
"우리 그룹엔 넣을 항목이 없다"며 또 unmapped 에 올린다. 부분문자열 대조로 못 잡는
의역(어순이 달라진 재진술)은 토큰 겹침 비율로 한 번 더 구제한다(실측: 002/001).
"""

from nh_parsing.extract import _reclassify_already_handled


def _result(fields: dict, unmapped_text: str) -> dict:
    return {
        "fields": {k: {"value": v} for k, v in fields.items()},
        "events": [],
        "unmapped": [{"text": unmapped_text, "kind": "심의관련_필드없음"}],
    }


def test_exact_substring_still_reclassified():
    """기존 동작(값을 그대로 옮겨적음) 은 그대로 유지돼야 한다."""
    r = _result({"review_stamp": "준법감시인 심의필:2025-0000(2025.12.01.~2025.12.14.)"},
                "준법감시인 심의필:2025-0000(2025.12.01.~2025.12.14.)")
    _reclassify_already_handled(r)
    assert r["unmapped"][0]["kind"] == "다른항목에서_처리됨"


def test_reworded_duplicate_is_caught_by_token_ratio():
    """실측(001) — 이벤트명이 다른 표현으로 반복된 문구는 부분문자열로 안 잡히지만
    토큰 겹침으로는 잡혀야 한다."""
    r = _result(
        {"event_name": "우리아이 금융생활 서비스 확대! 첫 계좌만들고 지원금 받자"},
        "우리아이 금융생활 서비스 확대기념 이벤트",
    )
    _reclassify_already_handled(r)
    assert r["unmapped"][0]["kind"] == "다른항목에서_처리됨"


def test_genuinely_new_fact_is_not_reclassified():
    """실측(002/003) — 이미 뽑힌 값과 관련 없는 새 사실은 그대로 진짜 공백으로 남아야
    한다(과잉 재분류 방지 — 뭐든 다 '처리됨'으로 지워버리면 조용한 유실과 같다)."""
    r = _result(
        {"review_stamp": "NH농협은행 준법감시인 심의필 2026-0000(2026.06.00.~2026.12.31)"},
        "이 광고는 법령 및 내부통제기준에 따른 광고 관련 절차를 준수하였습니다.",
    )
    _reclassify_already_handled(r)
    assert r["unmapped"][0]["kind"] == "심의관련_필드없음"


def test_short_extracted_values_dont_cause_false_matches():
    """6자 미만 값(예: '적금')은 배경 텍스트에 안 섞여야 한다 — 안 그러면 거의
    모든 unmapped 문구가 우연히 걸려 재분류된다."""
    r = _result({"product_subtype": "적금"}, "이 문구는 적금과 아무 관련이 없는 별개의 사실이다")
    _reclassify_already_handled(r)
    assert r["unmapped"][0]["kind"] == "심의관련_필드없음"
