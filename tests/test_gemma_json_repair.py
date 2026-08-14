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


def test_한_응답에_오발행이_여러_개여도_복구한다():
    """2026-08-14 실측 — 한 개만 지우던 옛 방식이 못 살리던 형태.

    파싱 개선으로 밴드 통독에 들어가는 텍스트가 늘자 응답이 길어졌고, 한 응답에
    오발행이 겹쳐 나오기 시작했다. new-sample-data 89건 재실행에서 밴드 통독 실패가
    6 → 17건으로 늘었고 그중 16건이 `Invalid \\uXXXX escape` 였다.
    """
    broken = (
        '{"a": "우대금리 ' + BS + 'u00 0.5%p", '
        '"b": "기본금리 ' + BS + 'u00 연 3.1%", '
        '"c": "최고 ' + BS + 'u00 연 7.3%"}'
    )
    assert json_is_broken(broken)

    repaired = _repair_trailing_escape(broken)

    assert repaired == {
        "a": "우대금리 u00 0.5%p",
        "b": "기본금리 u00 연 3.1%",
        "c": "최고 u00 연 7.3%",
    }


def test_정상_이스케이프는_여러_개여도_안_지운다():
    """반복 제거가 멀쩡한 이스케이프까지 갉아먹지 않는지 — 과잉 수정 방지."""
    broken = (
        '{"a": "깨짐 ' + BS + 'u00", '
        '"b": "줄1' + BS + 'n줄2' + BS + 'n줄3", '
        '"c": "따옴표 ' + BS + '" 안"}'
    )
    assert json_is_broken(broken)

    repaired = _repair_trailing_escape(broken)

    assert repaired["b"] == "줄1\n줄2\n줄3"      # 개행 3개 그대로
    assert repaired["c"] == '따옴표 " 안'         # 이스케이프된 따옴표 그대로


def test_복구_불가능하면_None_을_돌려준다():
    """조용히 빈 값을 만들지 않는다 — 호출측이 원래 예외를 올려 재시도/기록한다.

    응답이 escape 문제가 아니라 통째로 잘린 경우가 여기 해당한다. 반복 제거로도
    안 살아나므로 상한(max_fixes)에서 멈추고 None 을 돌려 호출측 재시도에 맡긴다.
    """
    assert _repair_trailing_escape('{"a": "닫히지 않은 문자열') is None
    assert _repair_trailing_escape("완전히 JSON 이 아님") is None
    # 잘린 응답 안에 정상 역슬래시가 여러 개 있어도 조용히 뭔가 만들어내면 안 된다
    assert _repair_trailing_escape('{"a": "줄1' + BS + 'n줄2' + BS + 'n줄3') is None


def test_역슬래시가_없으면_None():
    assert _repair_trailing_escape('{"a": 잘못된값}') is None


def json_is_broken(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return True
    return False
