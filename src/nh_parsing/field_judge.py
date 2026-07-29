from __future__ import annotations

"""필드 정합 단계 — 이전 프로젝트의 '관측 → 병합 → judge' 3단계 추출 이식.

원본 패턴 (paddle-gemma-orchestrator):
- core/extraction.py::check_field_consistency — 값 토큰 중 OCR 텍스트에서
  발견된 비율(match)을 연속 신뢰 신호로 쓰고, BACKED_TOKEN_RATIO(0.8) 이상이면
  ocr_backed, 미만이면 needs_reconcile.
- clients/gemma.py::request_gemma_page_judge — 병합된 관측 bundle 을 텍스트
  전용 LLM 호출로 판정해 최종 추출 JSON 산출.

NH 적용 방식 (전면 judge 가 아니라 충돌-트리거 judge):
1. 병합: 같은 (key, 정규화 값) 중복은 코드가 합침 (LLM 불필요)
2. 스코어링: 값 토큰 비율 ocr_score 를 페이지 전체 텍스트 기준으로 계산
   (라인 앵커 검증을 통과 못한 여러 줄 걸친 값을 구제)
3. judge: 신호가 충돌하는 항목만 텍스트 전용 1회 호출로 keep/drop/rekey 판정
   — 충돌이 없으면 호출 자체가 없다 (평상시 비용 0).

충돌 정의 (범용 신호만):
- ocr_backed=False 이고 crop_verified 도 아님 (환각 의심)
- '금리' 키인데 값에 %p 가산 표기 (금리 vs 우대금리 혼동 의심)
- 같은 수치 토큰이 금리/우대금리 양쪽 키로 중복 추출됨
"""

import re

from .gemma_client import chat_json
from .ir import ExtractedField, Line
from .vlm_judge import FIELD_KEYS

BACKED_TOKEN_RATIO = 0.8   # 이전 프로젝트 검증값
_MAX_JUDGE_ITEMS = 20
_RATE_KEYS = {"금리", "우대금리"}


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣.%]", "", str(s).casefold())


def check_field_consistency(value: str, page_text: str) -> float:
    """값 토큰 중 페이지 텍스트에서 발견된 비율 (0.0~1.0) — 연속 신뢰 신호."""
    norm_page = _norm(page_text)
    tokens = [t for t in (_norm(tok) for tok in re.split(r"[\s,;/·()~]+", value)) if t]
    if not tokens:
        return 1.0 if _norm(value) and _norm(value) in norm_page else 0.0
    return sum(1 for t in tokens if t in norm_page) / len(tokens)


def merge_observations(
    observations: list[list[ExtractedField]],
) -> list[ExtractedField]:
    """복수 관측(전체 1회 + 섹션 단위 N회) 병합 — 이전 프로젝트 _add_observation 이식.

    - 같은 key 에서 정규화 값이 같거나 포함 관계면 한 항목으로 합치고
      obs_count(득표)를 누적한다. 더 긴(완전한) 값이 대표값이 된다 —
      단일 호출의 절단('1년 이내' vs '1년 이내 (1년씩 기한연장 가능)')을 흡수.
    - 어느 관측에서도 안 나온 값은 존재할 수 없으므로(합집합),
      단일 호출의 누락 변동도 흡수된다. 환각 방어는 관측별 ocr_backed +
      후속 reconcile/judge 가 담당.
    """
    merged: list[ExtractedField] = []
    for obs in observations:
        for f in obs:
            f.obs_count = 1
            placed = False
            for m in merged:
                if m.key != f.key:
                    continue
                a, b = _norm(m.value), _norm(f.value)
                if not (a == b or a in b or b in a):
                    continue
                if len(f.value) > len(m.value):  # 더 완전한 표기로 교체
                    m.value = f.value
                    m.bbox = f.bbox or m.bbox
                m.obs_count = (m.obs_count or 1) + 1
                m.ocr_backed = bool(m.ocr_backed or f.ocr_backed)
                m.regex_backed = m.regex_backed or f.regex_backed
                if (f.confidence or 0) > (m.confidence or 0):
                    m.confidence = f.confidence
                placed = True
                break
            if not placed:
                merged.append(f)
    return merged


def dedupe_fields(fields: list[ExtractedField]) -> tuple[list[ExtractedField], int]:
    """같은 (key, 정규화 값) 관측 병합 — 검증 플래그는 OR, 신뢰도는 최대값."""
    seen: dict[tuple[str, str], ExtractedField] = {}
    out: list[ExtractedField] = []
    for f in fields:
        k = (f.key, _norm(f.value))
        if k in seen:
            prev = seen[k]
            prev.ocr_backed = bool(prev.ocr_backed or f.ocr_backed)
            prev.crop_verified = bool(prev.crop_verified or f.crop_verified) or None
            if (f.confidence or 0) > (prev.confidence or 0):
                prev.confidence = f.confidence
            continue
        seen[k] = f
        out.append(f)
    return out, len(fields) - len(out)


def find_conflicts(fields: list[ExtractedField]) -> list[int]:
    idx: set[int] = set()
    for i, f in enumerate(fields):
        if f.ocr_backed is False and not f.crop_verified:
            idx.add(i)
        if f.key == "금리" and "%p" in f.value.replace(" ", ""):
            idx.add(i)  # %p = 포인트 가산 표기 — 우대금리 혼동 의심
    nums: dict[str, set[str]] = {}
    for f in fields:
        if f.key in _RATE_KEYS:
            for n in re.findall(r"\d+(?:\.\d+)?%p?", f.value.replace(" ", "")):
                nums.setdefault(n, set()).add(f.key)
    dup_nums = {n for n, keys in nums.items() if len(keys & _RATE_KEYS) == 2}
    if dup_nums:
        for i, f in enumerate(fields):
            if f.key in _RATE_KEYS and any(
                n in f.value.replace(" ", "") for n in dup_nums
            ):
                idx.add(i)
    return sorted(idx)


_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},  # analysis-first: strict 빈 배열 퇴행 방지
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_index": {"type": "integer"},
                    "action": {"type": "string", "enum": ["keep", "drop", "rekey"]},
                    "new_key": {"type": "string", "enum": FIELD_KEYS},
                    "reason": {"type": "string"},
                },
                "required": ["field_index", "action", "new_key", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "resolutions"],
    "additionalProperties": False,
}

_JUDGE_PROMPT = """당신은 금융상품 광고 필드 추출의 최종 심판(judge)입니다.
아래 필드 후보들은 검증 신호가 충돌합니다. 각 후보의 근거 라인을 보고 판정하세요.

판정 (action):
- keep: 값과 키가 근거 라인으로 뒷받침됨 (new_key = 기존 키 그대로)
- rekey: 값은 맞지만 키가 잘못됨 → new_key 에 올바른 키.
  규칙: 조건 충족 시 가산·추가 제공되는 이율(%p 가산, 우대·특별 조건부)은 '우대금리',
  기본·최고·최저·약정 이율 표기는 '금리'.
- drop: 근거 라인에 값이 존재하지 않거나(환각) 다른 항목과 완전 중복

충돌 후보:
{items}

페이지 텍스트 (근거 확인용):
{context}

먼저 analysis 에 각 후보의 문제를 정리한 뒤, 모든 후보에 대해 판정을 반환하세요."""


def judge_conflicts(
    fields: list[ExtractedField],
    conflict_idx: list[int],
    lines: list[Line],
) -> list[str]:
    """충돌 항목만 텍스트 전용 judge 호출로 정리. 반환: 조치 노트(감사 추적)."""
    items = []
    for i in conflict_idx[:_MAX_JUDGE_ITEMS]:
        f = fields[i]
        flags = f"ocr_backed={f.ocr_backed} ocr_score={f.ocr_score} crop_verified={f.crop_verified}"
        items.append(f"[{i}] key={f.key} value={f.value!r} ({flags})")
    context = "\n".join(l.text for l in lines[:150])

    data = chat_json(
        [{"type": "text", "text": _JUDGE_PROMPT.format(
            items="\n".join(items), context=context)}],
        schema_name="field_judge",
        schema=_JUDGE_SCHEMA,
        max_tokens=1200,
    )
    notes: list[str] = []
    to_drop: set[int] = set()
    valid = set(conflict_idx)
    for res in data.get("resolutions", []):
        i = res.get("field_index", -1)
        if i not in valid:
            continue
        action = res.get("action")
        if action == "drop":
            to_drop.add(i)
            notes.append(f"judge 제거: [{fields[i].key}] {fields[i].value!r} — {res.get('reason', '')}")
        elif action == "rekey" and res.get("new_key") in FIELD_KEYS:
            old = fields[i].key
            if res["new_key"] != old:
                fields[i].key = res["new_key"]
                notes.append(f"judge 키 교정: {old}→{res['new_key']} {fields[i].value!r}")
    if to_drop:
        kept = [f for i, f in enumerate(fields) if i not in to_drop]
        fields.clear()
        fields.extend(kept)
    return notes


def reconcile_fields(
    fields: list[ExtractedField], lines: list[Line]
) -> tuple[list[ExtractedField], list[str]]:
    """병합 → 스코어링 → (충돌 시에만) judge. 반환: (정리된 필드, 조치 노트)."""
    notes: list[str] = []
    fields, merged = dedupe_fields(fields)
    if merged:
        notes.append(f"필드 중복 관측 {merged}건 병합")

    page_text = "\n".join(l.text for l in lines)
    for f in fields:
        f.ocr_score = round(check_field_consistency(f.value, page_text), 3)
        if not f.ocr_backed and f.ocr_score >= BACKED_TOKEN_RATIO:
            f.ocr_backed = True  # 여러 줄에 걸친 값 구제 (토큰 비율 기준)

    conflicts = find_conflicts(fields)
    if conflicts:
        try:
            notes.extend(judge_conflicts(fields, conflicts, lines))
        except Exception as exc:
            notes.append(f"필드 judge 실패(원 목록 유지): {exc}")
    return fields, notes
