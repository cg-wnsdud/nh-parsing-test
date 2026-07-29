from __future__ import annotations

"""필드 부재의 3분류 (LLM 호출 없음).

STAGE_3 는 값이 없으면 전부 `status=not_found` 로 돌려준다. 그런데 심의 관점에서
'값이 없다'는 서로 다른 세 가지 사실이 뭉개진 것이다:

  (A) 해당없음  — 이 상품유형·광고유형에 그 항목이 성립하지 않는다.
                  (수시입출식 통장에 만기후이율, 수상 표기가 없는 광고에 수상 시기)
  (B) 미표시    — 표시해야 하는데 광고에 없다. **이게 심의 지적사항이고 제품의 산출물이다.**
  (C) 확인필요  — 광고엔 있는데 우리가 못 올렸을 수 있다(파싱·입력 결함).

측정(2026-07-28): 4개 문서 미발견 51건 중 41건이 (A)였다. 셋을 한 통에 담아두면
`fields_not_found` 숫자가 성능 지표처럼 보이지만 분모가 틀린 값이고, 정작 팔아야 할
(B)를 못 집어낸다.

(A)/(B) 는 규정 조건이라 **코드가 결정론적으로** 가른다 — 스키마 각 필드의
`applicability`(적용조건) + `obligation`(의무등급)을 평가할 뿐 VLM 을 부르지 않는다.
문제마다 VLM 단계를 얹어온 패턴을 여기서 반복하지 않기 위해서다.

(C) 는 정의상 '우리 산출물에 없는 것'이라 자기 출력만 보고는 알 수 없다(원본 대조나
재판독이 필요하다). 대신 **확실히 아는 한 가지 경로만** 근거로 남긴다 — 파싱은 됐는데
STAGE_3 입력(llm_view)에서 빠진 영역. 장식예시 격리(2a) 등으로 빠지며, 원본을 다시
보지 않고도 100% 우리 쪽 손실이라고 말할 수 있는 유일한 부류다. 그 밖의 (C) 는
여기서 추정하지 않는다 — 키워드로 짐작하면 오탐이 섞이고(실측), 그걸 지적사항 옆에
섞어 놓으면 (B) 의 신뢰도까지 같이 떨어진다.

적용조건 문법 (schemas/*.json 의 필드마다 `applicability.conditions`, AND 결합):
    {"rule": "subtype_in",     "values": [...]}   상품 세부유형이 이 중 하나일 때만
    {"rule": "subtype_not_in", "values": [...]}   이 유형들에서는 성립하지 않음
    {"rule": "ad_type_in",     "values": [...]}   광고유형이 이 중 하나일 때만
    {"rule": "trigger_any",    "fields": [...]}   이 필드 중 하나라도 값이 있을 때만 의무 발생
    {"rule": "conditional"}                       광고가 그 소재를 다룰 때만 (자기참조)
conditions 가 비어 있으면 '전 광고 적용'이다. 조문에 없는 해석은 `basis="derived"` +
`why` 를 달아 두고, derived_rules() 로 뽑아 사람이 승인한다.

의무등급(`obligation`): 필수 / 권장 은 판정 대상, 분류·수집·관측·절차 는 판정 제외.
"""

from typing import Any

# 부재 3분류의 라벨. 값(문자열)이 산출물 키가 되므로 여기서만 정의한다.
ABSENT_NOT_APPLICABLE = "해당없음"
ABSENT_MISSING = "미표시"
ABSENT_UNKNOWN_SUBTYPE = "확인필요"
ABSENT_OUT_OF_SCOPE = "판정제외"

# 판정 대상이 아닌 의무등급 — 분류축(product_subtype), 전수수집 배열(rate_mentions),
# 금지표현 관측(G4), 광고물 밖의 절차(준법감시인 사전심의).
_NON_JUDGED_OBLIGATIONS = {"분류", "수집", "관측", "절차"}

_UNKNOWN_SUBTYPE = "판단불가"


def _is_filled(val: Any) -> bool:
    """STAGE_3 필드 하나가 값을 가졌는가. status 는 _normalize_status 가 이미 정렬해 뒀다."""
    if not isinstance(val, dict):
        return False
    return val.get("status") in ("found", "uncertain")


def _field_specs(pack: dict) -> dict[str, dict]:
    """스키마 팩 전체를 field_key → 필드정의 로 평탄화."""
    specs: dict[str, dict] = {}
    for group in pack.get("call_groups", []):
        for f in (group.get("fields") or []) + (group.get("observation_fields") or []):
            specs[f["field_key"]] = f
    return specs


def _eval_condition(cond: dict, ctx: dict) -> tuple[bool, str]:
    """조건 하나 평가 → (적용되는가, 사유코드).

    사유코드는 산출물에 그대로 실린다. 조문 원문은 싣지 않는다 — 근거는 스키마·대장에
    한 번만 두고 실행결과에는 field_key 와 코드만 남기는 규약.
    """
    rule = cond.get("rule")
    subtype = ctx.get("product_subtype")

    if rule in ("subtype_in", "subtype_not_in"):
        values = cond.get("values") or []
        if not subtype or subtype == _UNKNOWN_SUBTYPE:
            return False, "subtype_unknown"
        inside = subtype in values
        ok = inside if rule == "subtype_in" else not inside
        return ok, rule

    if rule == "ad_type_in":
        return (ctx.get("ad_type") in (cond.get("values") or [])), rule

    if rule == "trigger_any":
        fields = cond.get("fields") or []
        return any(f in ctx["filled_keys"] for f in fields), rule

    if rule == "conditional":
        # 자기참조 조건 — 값이 없다는 것 자체가 '광고가 그 소재를 안 다뤘다'는 뜻이다.
        # 없는데 있어야 한다고 말하려면 광고 안에 방아쇠가 있어야 하는데, 그 방아쇠가
        # 바로 이 필드다. 그래서 미발견이면 항상 해당없음으로 떨어진다.
        return False, rule

    # 모르는 규칙을 조용히 '적용됨'으로 넘기면 없는 지적사항이 생긴다 — 확인 대상으로 올린다.
    return False, f"unknown_rule:{rule}"


def classify_absence(field_key: str, spec: dict, ctx: dict) -> dict:
    """미발견 필드 하나를 3분류한다. 반환값이 그대로 산출물의 `absence` 가 된다."""
    obligation = spec.get("obligation") or "필수"
    if obligation in _NON_JUDGED_OBLIGATIONS:
        return {"kind": ABSENT_OUT_OF_SCOPE, "obligation": obligation, "rule": "not_judged"}

    conds = (spec.get("applicability") or {}).get("conditions") or []
    for cond in conds:
        ok, code = _eval_condition(cond, ctx)
        if ok:
            continue
        if code == "subtype_unknown":
            # 유형을 못 정했으면 해당없음이라고 단정하면 안 된다(지적사항을 조용히 삼킴).
            return {"kind": ABSENT_UNKNOWN_SUBTYPE, "obligation": obligation,
                    "rule": code}
        out = {"kind": ABSENT_NOT_APPLICABLE, "obligation": obligation, "rule": code}
        if cond.get("values"):
            out["detail"] = cond["values"]
        if cond.get("fields"):
            out["detail"] = cond["fields"]
        return out

    return {"kind": ABSENT_MISSING, "obligation": obligation, "rule": "applicable"}


def _context(result: dict, extra_filled: set[str] | None = None) -> dict:
    subtype = result["fields"].get("product_subtype", {}).get("value") or None
    filled = {k for k, v in result["fields"].items() if _is_filled(v)}
    if extra_filled:
        filled |= extra_filled
    return {
        "product_subtype": subtype if isinstance(subtype, str) else None,
        "ad_type": result.get("ad_type"),
        "filled_keys": filled,
    }


def classify_absences(result: dict, pack: dict) -> dict:
    """result 의 미발견 필드에 `absence` 를 붙이고 문서 단위 요약을 반환한다.

    events(이벤트 배열)는 이벤트마다 따로 평가한다 — 한 이벤트에 경품이 없는 것과
    다른 이벤트에 없는 것은 별개 지적이기 때문이다.
    """
    specs = _field_specs(pack)
    ctx = _context(result)

    missing: list[dict] = []
    not_applicable: list[str] = []
    out_of_scope: list[str] = []
    needs_check: list[str] = []

    def _bucket(key: str, absence: dict, label: str | None = None) -> None:
        name = label or key
        kind = absence["kind"]
        if kind == ABSENT_MISSING:
            missing.append({"field_key": name, "obligation": absence["obligation"]})
        elif kind == ABSENT_NOT_APPLICABLE:
            not_applicable.append(name)
        elif kind == ABSENT_OUT_OF_SCOPE:
            out_of_scope.append(name)
        else:
            needs_check.append(name)

    for key, val in result["fields"].items():
        if not isinstance(val, dict) or val.get("status") != "not_found":
            continue
        spec = specs.get(key)
        if spec is None:
            # 스키마에 없는 키가 결과에 있으면 판정 근거가 없다 — 조용히 넘기지 않는다.
            val["absence"] = {"kind": ABSENT_UNKNOWN_SUBTYPE, "rule": "spec_missing"}
            needs_check.append(key)
            continue
        val["absence"] = classify_absence(key, spec, ctx)
        _bucket(key, val["absence"])

    for i, event in enumerate(result.get("events") or [], 1):
        if not isinstance(event, dict):
            continue
        ev_filled = {k for k, v in event.items() if _is_filled(v)}
        ev_ctx = _context(result, extra_filled=ev_filled)
        for key, val in event.items():
            if not isinstance(val, dict) or val.get("status") != "not_found":
                continue
            spec = specs.get(key)
            if spec is None:
                val["absence"] = {"kind": ABSENT_UNKNOWN_SUBTYPE, "rule": "spec_missing"}
                needs_check.append(f"event{i}.{key}")
                continue
            val["absence"] = classify_absence(key, spec, ev_ctx)
            _bucket(key, val["absence"], label=f"event{i}.{key}")

    return {
        "미표시": missing,
        "해당없음": sorted(not_applicable),
        "판정제외": sorted(out_of_scope),
        "확인필요": sorted(needs_check),
        "product_subtype": ctx["product_subtype"],
    }


# ─────────────────────────── (C) 입력 유실 근거 ───────────────────────────


def input_gap(view: dict, parse_doc: dict | None) -> list[dict]:
    """파싱은 했는데 STAGE_3 입력(llm_view)에는 안 실린 영역을 찾는다 (코드 전용).

    (C)'우리 결함'중 원본을 다시 보지 않고도 확정할 수 있는 유일한 부류다. 장식예시
    격리(2a)가 주 경로 — 앱 화면 목업으로 판정된 섹션이 통째로 빠지는데, 그 안에 심의
    대상 문구가 섞여 있으면 STAGE_3 는 그것을 애초에 볼 수 없었다. 이 목록이 비어 있지
    않은 문서에서는 '미표시' 판정이 사실은 '입력 누락'일 수 있다.

    판정하지 않고 근거만 돌려준다 — 어떤 필드의 누락인지는 원본 대조의 몫이다.
    """
    if not parse_doc:
        return []
    seen: set[str] = set()
    for page in view.get("pages", []):
        for sec in page.get("sections", []):
            for r in sec.get("regions", []):
                seen.add(r["region_id"])

    gaps: list[dict] = []
    for page in parse_doc.get("pages", []):
        for region in page.get("regions", []):
            rid = region.get("region_id")
            if not rid or rid in seen:
                continue
            text = " ".join(
                (l.get("text") or "").strip() for l in region.get("lines", [])
            ).strip()
            if not text:
                continue  # 텍스트가 없는 영역은 애초에 실릴 게 없다 (유실 아님)
            gaps.append({
                "region_id": rid,
                "page": page.get("page_no"),
                "text": text[:200],
                "reason": "장식예시 격리" if region.get("is_illustrative") else "입력 투영에서 제외",
            })
    return gaps


# ─────────────────────────── 설계 점검용 리포트 ───────────────────────────


def derived_rules(pack: dict) -> list[dict]:
    """조문에 그대로 없는 '해석'으로 넣은 적용조건 목록 — 사람 승인 대상.

    수시입출식에 만기후이율이 성립하지 않는다는 것 같은 판단은 규정 원문에 문장으로
    적혀 있지 않다. 도메인 사실이라 과적합은 아니지만, 검토 없이 굳으면 지적사항을
    조용히 삼키는 규칙이 된다 — 그래서 뽑아 볼 수 있게 남긴다.
    """
    out: list[dict] = []
    for key, spec in _field_specs(pack).items():
        for cond in (spec.get("applicability") or {}).get("conditions") or []:
            if cond.get("basis") == "derived":
                out.append({
                    "field_key": key,
                    "rule": cond.get("rule"),
                    "values": cond.get("values") or cond.get("fields"),
                    "why": cond.get("why", ""),
                })
    return out


def check_schema_metadata(pack: dict) -> list[str]:
    """모든 필드가 obligation·applicability 를 갖췄는지 (조용한 기본값 방지).

    빠진 필드는 obligation 기본값 '필수' + 조건 없음으로 평가돼 없던 지적사항이 생긴다.
    스키마를 늘릴 때 이 검사가 먼저 깨지게 해서 알아차리게 한다.
    """
    problems: list[str] = []
    for key, spec in _field_specs(pack).items():
        if not spec.get("obligation"):
            problems.append(f"{key}: obligation 없음")
        if spec.get("applicability") is None:
            problems.append(f"{key}: applicability 없음")
    return problems
