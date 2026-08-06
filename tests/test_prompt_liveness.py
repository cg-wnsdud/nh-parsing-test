# -*- coding: utf-8 -*-
"""STAGE_3 프롬프트가 **실제로 렌더되는 것만** 참조하는지 확인한다.

죽은 참조는 조용하다. 규칙이 "…라고 표시된 곳은" 이라고 말하는데 그 표시가 절대
안 나오면, LLM 은 규칙 전체를 건너뛰고 우리는 규칙이 도는 줄 안다.

실제로 그랬다 (2026-08-06 발견): 규칙 8이 '섹션 제목에 "예시·목업 화면"이라고 붙은
곳' 을 참조했는데 섹션은 2026-08-03 에 제거됐다. 게다가 `Region.is_illustrative` 는
**코드 어디에서도 True 로 대입되지 않는다** — 읽는 곳 3군데(vlm_direct.py:381 ·
pipeline.py:665 · applicability.py:244)가 전부 항상 False 다. 목업 표시가 나올
경로 자체가 없었다.
"""

import inspect

from nh_parsing import extract as extract_mod
from nh_parsing.extract import _COMMON_RULES, _RELATION_TAG, _render_doc


def test_프롬프트가_섹션을_참조하지_않는다():
    """섹션 계층은 2026-08-03 제거됐다 — _render_doc 이 섹션 제목을 안 찍는다."""
    assert "섹션" not in _COMMON_RULES, (
        "섹션은 파싱에서 제거됐다. 규칙이 섹션 제목을 참조하면 절대 발동하지 않는다"
    )
    assert "예시·목업 화면" not in _COMMON_RULES


def test_렌더러가_찍는_딱지는_전부_규칙이_설명한다():
    """관계 딱지는 살아 있는 참조다 — 렌더러가 찍고 규칙이 뜻을 설명한다.

    죽은 참조의 반대 사례. 이쪽이 깨지면 LLM 이 뜻 모르는 딱지를 받는다.
    """
    for tag in _RELATION_TAG.values():
        assert tag in _COMMON_RULES, f"[{tag}] 를 찍는데 규칙이 설명하지 않는다"


def test_목업_지시는_남아_있다():
    """★ 규칙을 지운 게 아니라 불가능한 전제절만 뺐다.

    목업 격리 로직(is_illustrative 복구)은 넣지 않기로 했고(walkthrough §9-⑥),
    지금은 STAGE_3 VLM 이 내용으로 판단해 잘 걸러낸다(001 '100,000원'·'NH0000통장'이
    unmapped kind="심의무관" 으로 감 — 전수 확인). 그 지시가 사라지면 안 된다.
    """
    assert "견본" in _COMMON_RULES
    assert "100,000원" in _COMMON_RULES     # 실측에서 뽑은 구체 예시
    assert "심의무관" in _COMMON_RULES


def test_is_illustrative_는_아직_아무도_설정하지_않는다():
    """이 테스트가 깨지면 목업 격리가 되살아난 것이다 — 그때 규칙 8을 다시 쓸 수 있다.

    되살릴 때는 5문서 목업 영역을 눈으로 세어 정답부터 만들 것 (walkthrough §9-⑥).
    """
    from nh_parsing import applicability, pipeline, vlm_direct

    for mod in (pipeline, vlm_direct, applicability, extract_mod):
        src = inspect.getsource(mod)
        assert "is_illustrative = True" not in src
        assert "is_illustrative=True" not in src


def test_렌더러는_역할과_근거ID만_준다():
    """규칙이 참조할 수 있는 표시가 무엇인지 못박는다 — region_id · role · 관계 딱지."""
    view = {"pages": [{"page_number": 1, "regions": [
        {"region_id": "p1_r008", "role": "유의사항", "text": "가입기간 12개월",
         "vlm_reading": "12개월", "vlm_reading_relation": "head_drop"},
    ]}]}
    rendered = _render_doc(view)
    assert "p1_r008 (유의사항): 가입기간 12개월" in rendered
    assert "[후보-항목명생략] 12개월" in rendered
    # 목업/예시 표시는 어디에도 없다 — 규칙이 이걸 참조하면 죽은 참조가 된다
    assert "목업" not in rendered and "예시" not in rendered
