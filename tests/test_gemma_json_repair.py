# -*- coding: utf-8 -*-
"""guided-decoding 오발행 역슬래시 복구 (gemma_client._repair_trailing_escape).

서버가 문자열 안에 불필요한 역슬래시를 내보내고 생성을 멈추는 결함이 실측됐다
(gemma-4-26b-NVFP4-MTP, finish_reason=stop, temperature=0 에서도 재현).
호출마다 위치·길이가 달라서 끝에서 고정 길이를 자르는 방식은 통하지 않는다.

2026-08-12 new-sample-data 실행에서 **문자열 중간**에 깨진 이스케이프가 나고
그 뒤에 정상 역슬래시가 더 있는 형태가 나왔다:
`Invalid \\uXXXX escape: line 1 column 3244` → 3회 재시도 뒤 밴드 통독 실패.
마지막 역슬래시만 지우던 옛 방식은 이걸 못 고쳤다.

모델 호출 없음 — 순수 문자열 처리라 폐쇄망 불필요.
"""

from __future__ import annotations

import json

from nh_parsing.gemma_client import _repair_trailing_escape

BS = chr(92)  # 리터럴에 역슬래시를 직접 쓰면 파이썬 이스케이프와 섞여 읽기 어렵다


def test_문제지점_뒤에_정상_역슬래시가_있어도_복구한다():
    """옛 방식(마지막 역슬래시 제거)이 실패하던 형태 — 이게 이번 수정의 핵심이다."""
    broken = '{"a": "금리 ' + BS + 'u00 이자", "b": "줄바꿈' + BS + 'n끝"}'
    assert json_is_broken(broken)

    repaired = _repair_trailing_escape(broken)

    assert repaired == {"a": "금리 u00 이자", "b": "줄바꿈\n끝"}
    # 뒤쪽의 정상 개행 이스케이프는 보존돼야 한다 (엉뚱한 걸 지우지 않았다는 확인)
    assert "\n" in repaired["b"]


def test_뒤에_역슬래시가_없는_기존_형태도_그대로_복구한다():
    broken = '{"a": "금리 ' + BS + 'u00 이자"}'
    assert json_is_broken(broken)
    assert _repair_trailing_escape(broken) == {"a": "금리 u00 이자"}


def test_문자열_종료부_오발행도_복구한다():
    """최초 실측 형태(2026-07-20) — 닫는 따옴표 직전 역슬래시."""
    broken = '{"a": "정상 문구' + BS + '"}'
    assert json_is_broken(broken)
    assert _repair_trailing_escape(broken) == {"a": "정상 문구"}


def test_복구_불가능하면_None_을_돌려준다():
    """조용히 빈 값을 만들지 않는다 — 호출측이 원래 예외를 올려 재시도/기록한다."""
    assert _repair_trailing_escape('{"a": "닫히지 않은 문자열') is None
    assert _repair_trailing_escape("완전히 JSON 이 아님") is None


def test_역슬래시가_없으면_None():
    assert _repair_trailing_escape('{"a": 잘못된값}') is None


def json_is_broken(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return True
    return False
