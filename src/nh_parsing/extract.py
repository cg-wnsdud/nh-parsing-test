"""STAGE_3 — 스키마 기반 필드 추출.

입력: 파싱 결과의 lean 투영(llm_view.build_doc_view) + 상품군 스키마(schemas/*.json)
출력: 스키마 필드가 채워진 JSON + 근거 region_id + 미배정(unmapped) + 커버리지 지표

설계 원칙
- 이미지는 넣지 않는다. 텍스트 정본(text) + VLM 통독 후보(vlm_reading)만 준다
  (파싱 단계에서 이미 VLM 이 이미지를 봤고, 여기서 또 보면 판정이 흔들린다).
- bbox 는 넣지 않는다. region_id 만 주고 근거로 돌려받아, 코드가 나중에 id→bbox 를 되붙인다.
- 호출을 그룹으로 쪼갠다(A안). 한 번에 50여 필드를 채우게 하면 뒤쪽 필드가 뭉개진다.
- 없는 값을 만들지 않게 하고(status=not_found), 어느 필드에도 안 맞은 텍스트는
  반드시 unmapped 로 남긴다 — 조용한 유실 금지.
"""

from __future__ import annotations

import json
import re

from .applicability import classify_absences, input_gap
from .extract_models import ExtractResult, FieldCell, ObservationItem, UnmappedItem
from .field_judge import BACKED_TOKEN_RATIO, check_field_consistency
from .gemma_client import chat_json
from .schema_pack import load_pack, response_schema

_COMMON_RULES = """당신은 금융상품 광고 심의를 지원하는 정보 추출기입니다.
아래 [광고 파싱 결과]를 읽고, [뽑을 항목]을 찾아 지정된 JSON 형식으로 채우세요.

반드시 지킬 규칙:
1. 광고 내용을 임의로 고치거나 계산하지 마세요. 단위(%와 %p)·날짜·금액을 다른 값으로 바꾸거나,
   여러 값을 더해 새 값을 만들지 않습니다.
   ※ 단, 아래 규칙 5에 따라 "정본의 인식 오류를 후보로 바로잡는 것"은 값을 바꾸는 것이 아니라
     같은 원문을 더 정확히 옮기는 것이므로 반드시 해야 합니다. 규칙 1과 5가 부딪히면 5를 따르세요.
2. 광고에 없는 내용을 만들지 마세요. status 는 value 와 반드시 짝이 맞아야 합니다.
   - value 에 내용을 채웠다  → status="found"
   - value 를 빈 문자열로 뒀다 → status="not_found"
   - 값을 채웠지만 이게 맞는지 확신이 낮다 → status="uncertain" + note 에 이유
   value 에 내용을 넣고 status 를 "not_found" 로 두는 일은 절대 없어야 합니다(서로 모순입니다).
3. evidence 에는 근거가 된 region_id 를 모두 적습니다(예: ["p1_r008"]). 근거 없이 값을 쓰지 마세요.
4. 항목 설명에 "~인 경우", "~라면" 같은 조건이 붙어 있으면, 그 조건에 해당하지 않는 광고에서는
   반드시 value="" status="not_found" 로 둡니다. 비슷해 보이는 다른 문구를 끌어와 칸을 채우지 마세요.
   빈 칸으로 두는 것이 잘못된 값을 넣는 것보다 항상 낫습니다.
5. 한 영역에 정본(첫 줄)과 [후보…](VLM 이 다시 읽은 것)가 함께 있으면 둘 중 더 정확한 쪽을 값으로 씁니다.
   - 정본은 글자·숫자가 잘못 붙거나 깨질 수 있습니다(예: 낱글자가 숫자와 붙음, 원문자가 일반 숫자로 바뀜).
   → 한국어 문장으로 자연스럽고 숫자·단위가 일관된 쪽을 고르되, 한쪽에만 있는 내용을 버리지는 마세요.
     어느 쪽을 왜 골랐는지 note 에 한 줄로 남깁니다.
   정본에 인식 오류가 보이는데 후보가 그 부분을 바르게 담고 있으면, 후보 쪽을 값으로 씁니다.
   오류를 알아보고도 그대로 남기지 마세요 — 잘못 읽힌 글자를 그대로 두면 심의 판단이 틀어집니다.
   ★ 후보 딱지가 곧 지시입니다. 대괄호 안 이름을 보고 다음과 같이 다루세요:
   - [후보-뒷부분잘림] : 후보가 문장 뒤를 못 읽고 끊긴 것입니다. **정본을 값으로 쓰세요.**
     후보의 표기가 더 깔끔해 보여도 뒤에 있던 내용(금액·명수·조건)이 통째로 사라진 상태입니다.
     표기만 후보에서 빌려오고 싶다면, 잘린 뒷부분은 반드시 정본에서 채워 합치세요.
   - [후보-항목명생략] : 앞의 항목명("상품명", "가입대상" 등)만 뺀 것으로 값 자체는 온전합니다.
     값만 필요한 칸이면 후보를 그대로 써도 됩니다.
   - [후보-정본보다많이읽음] : 정본이 놓친 글자를 후보가 더 담았습니다. 후보 쪽이 유리합니다.
   - [후보-정본과불일치] : 둘의 내용이 서로 다릅니다. 어느 쪽도 확신할 수 없으므로,
     그 값이 필수 항목이면 status="uncertain" 으로 두고 note 에 두 판독을 모두 적으세요.
6. 이 그룹의 어느 항목에도 해당하지 않지만 심의와 관련될 수 있는 문구는 unmapped 에 남깁니다.
   - kind="심의관련_필드없음": 심의에 필요해 보이는데 이 스키마에 넣을 항목이 아예 없는 경우
   - kind="심의무관": 버튼·장식·안내 링크처럼 심의와 무관한 경우
   심의 관련 문구를 unmapped 에도 남기지 않고 버리면 안 됩니다.
   단, 다른 호출그룹이 담당하는 내용(예: 이 그룹이 상품기본이면 금리·이자 문구)은 이미 다른 그룹에서
   처리하므로 unmapped 에 올리지 마세요. 같은 문구를 여러 번 반복해 올리지도 마세요.
7. analysis 에는 이 광고에서 무엇을 어떻게 찾았는지 2~3문장으로 먼저 정리합니다.
8. 앱 화면 캡처·지폐 그림 같은 **견본(목업)** 안에 적힌 금액·계좌번호·날짜·이름은 실제 상품
   조건이 아니므로 **값으로 쓰지 마세요**(예: 앱 송금 화면 견본의 '100,000원', 예시 통장의
   'NH0000통장'을 금액·상품명 필드에 넣으면 안 됩니다). 견본인지는 **주변 문구로 판단**하세요 —
   메뉴 이름·버튼 글자·사람 이름·'홈/이체/출금' 같은 화면 요소가 함께 나오면 견본입니다.
   다만 견본 안에도 실제 심의 대상 문구가 섞일 수 있으니(헤드라인·이벤트 기간 등) 읽기는
   하고, 값으로 쓸 때 그것이 광고의 실제 조건인지 화면 견본인지 판단해서 쓰세요.
   견본에서 온 문구는 unmapped 에 kind="심의무관" 으로 남기세요."""


def _render_fields(group: dict) -> str:
    lines = []
    for f in group.get("fields", []) or group.get("observation_fields", []):
        # 조건부 항목을 명확히 구분한다 — 해당 없는데 억지로 채우는 오탐이 실측됨
        req = "규정상 필수표시항목" if f.get("required_by_rule") else "조건부(해당 없으면 not_found)"
        head = f"- {f['field_key']} ({f['label']}, {req})"
        if f.get("type") == "list":
            head += " [여러 개면 배열로]"
        if f.get("enum"):
            head += f" [택1: {', '.join(f['enum'])}]"
        lines.append(head)
        lines.append(f'  """{f["prompt"]}"""')
    return "\n".join(lines)


# VLM 통독 후보의 관계 → 프롬프트에 찍을 딱지. truncation.classify_reading 의 kind 값.
# 딱지 자체가 지시문이다 — 프롬프트 규칙에서 이 문구를 그대로 참조한다.
_RELATION_TAG = {
    "tail_cut": "후보-뒷부분잘림",
    "head_drop": "후보-항목명생략",
    "expanded": "후보-정본보다많이읽음",
    "diverged": "후보-정본과불일치",
    "same": "후보",
}


def _render_doc(view: dict) -> str:
    """파싱 결과를 LLM 이 읽을 텍스트로 렌더. region_id 를 근거 지목용으로 노출."""
    out: list[str] = []
    for page in view.get("pages", []):
        out.append(f"### 페이지 {page.get('page_number')}")
        # 섹션(의미 묶음) 계층은 2026-08-03 제거 — 영역이 읽기순서(위→아래)로 오므로
        # 화면 흐름 자체가 문맥이다. 자세한 근거는 vlm_judge 모듈 상단 주석.
        for r in page.get("regions", []):
            text = (r.get("text") or "").replace("\n", " / ")
            out.append(f"  {r['region_id']} ({r.get('role','')}): {text}")
            cand = r.get("vlm_reading")
            if cand:
                # 후보에 관계 딱지를 붙인다. 안 붙이면 LLM 은 정본과 후보를 동등한
                # 두 선택지로 보는데, 후보가 뒤에서 잘려 있어도 표기(원문자·낫표)는
                # 더 예뻐서 그쪽을 고를 수 있다 — 실측 002 p1_r018 후보는 경품 금액
                # '네이버페이 20,000원' 이 통째로 빠진 판독이었다.
                tag = _RELATION_TAG.get(r.get("vlm_reading_relation") or "", "후보")
                out.append(f"      [{tag}] {cand.replace(chr(10), ' / ')}")
        if page.get("unassigned"):
            # 미배정 텍스트에도 근거 ID를 준다. 없으면 전수수집 배열('표기 그대로 (region_id)')에
            # 담을 수가 없어 조용히 빠진다 — 003 실측: 배너의 'NH Benefit 2025.10.01-2025.10.31'
            # 이 STAGE_3 입력에는 있었는데 댈 ID 가 없어 period_mentions 에서 누락됐다.
            out.append("[미배정 텍스트]")
            out.append(f"  {_unassigned_id(page)} (미배정): "
                       + page["unassigned"].replace("\n", " / "))
    return "\n".join(out)


def _unassigned_id(page: dict) -> str:
    """미배정 텍스트 덩어리의 가상 region_id. 실제 영역이 아니므로 커버리지 분모에는 안 넣는다."""
    return f"p{page.get('page_number')}_unassigned"


def _prompt(group: dict, doc_text: str) -> str:
    parts = [_COMMON_RULES, ""]
    if group.get("instruction"):
        parts += [f"이 그룹의 지침: {group['instruction']}", ""]
    parts += ["[뽑을 항목]", _render_fields(group), "", "[광고 파싱 결과]", doc_text]
    return "\n".join(parts)


def _max_tokens(group: dict) -> int:
    n = len(group.get("fields", []) or group.get("observation_fields", []))
    return min(8000, 400 * n + 1500)


# value 칸에 값 대신 '없음'을 뜻하는 말을 적어 보내는 경우 — 문자열이라 bool() 로는
# 값이 있는 것처럼 보인다. 실측(002): installment_type/deposit_kind/tax_benefit 세 필드가
# value="not_found", evidence=[] 인 채 status="found" 로 통과해 회수 집계를 부풀렸다.
_EMPTY_SENTINELS = {
    "not_found", "notfound", "none", "null", "n/a", "na", "-", "—",
    "없음", "해당없음", "해당 없음", "미표시", "미기재", "확인불가",
}


def _is_empty_value(v) -> bool:
    """빈 값 판정 — 빈 문자열·빈 배열뿐 아니라 '없음'류 표현도 빈 값으로 본다."""
    if isinstance(v, list):
        return not [x for x in v if not _is_empty_value(x)]
    if v is None:
        return True
    s = str(v).strip().strip("\"'").lower()
    return s == "" or s in _EMPTY_SENTINELS


def _normalize_status(val: dict, key: str, result: dict) -> None:
    """value 와 status 가 모순이면 value 를 신뢰해 status 를 맞추고 그 사실을 기록한다.

    실측: 값은 정확히 뽑았는데 status 만 not_found 로 붙여 보내는 경우가 있다(G2 금리 그룹 전체).
    이 지표로 누락 여부를 판단하므로 모순을 그대로 두면 안 되고, 조용히 고쳐서도 안 된다.
    """
    v = val.get("value")
    has_value = not _is_empty_value(v)
    reasons: list[str] = []

    if not has_value and v not in ("", [], None):
        # '없음'류 표현이 값 칸에 들어온 경우 — 값 자체도 비워야 뒤에서 근거검증·집계가 꼬이지 않는다
        reasons.append(f"value 에 '{v}' 가 들어와 빈 값으로 정정")
        val["value"] = [] if isinstance(v, list) else ""

    st = val.get("status")
    if has_value and st == "not_found":
        val["status"] = "found"
        reasons.append("값이 있는데 not_found 로 와서 found 로 보정")
    elif not has_value and st != "not_found":
        val["status"] = "not_found"
        reasons.append(f"값이 없는데 {st} 로 와서 not_found 로 보정")

    if reasons:
        val["status_corrected"] = " / ".join(reasons)
        result.setdefault("status_corrections", []).append(key)


def _dedupe_unmapped(items: list[dict]) -> list[dict]:
    """같은 문구를 여러 호출그룹이 각각 미배정으로 올리므로 합친다.

    문구가 같으면 한 건으로 보고, 어느 그룹들이 올렸는지만 groups 에 모아둔다.
    kind 가 엇갈리면 '심의관련_필드없음'을 남긴다(놓치는 쪽보다 보수적).
    """
    merged: dict[str, dict] = {}
    for it in items:
        key = (it.get("text") or "").strip()
        if not key:
            continue
        # 이미 병합된 항목(groups 보유)과 새 항목(group 보유)을 함께 받아도 되게 정규화
        incoming = [g for g in (it.pop("groups", None) or [it.pop("group", None)]) if g]
        cur = merged.get(key)
        if cur is None:
            it["groups"] = incoming
            merged[key] = it
            continue
        for g in incoming:
            if g not in cur["groups"]:
                cur["groups"].append(g)
        if it.get("kind") == "심의관련_필드없음":
            cur["kind"] = "심의관련_필드없음"
            cur["reason"] = it.get("reason") or cur.get("reason", "")
        for rid in it.get("evidence") or []:
            if rid not in (cur.get("evidence") or []):
                cur.setdefault("evidence", []).append(rid)
    return list(merged.values())


def extract_document(view: dict, *, product_group: str | None = None,
                     ad_type: str | None = None, parse_doc: dict | None = None) -> dict:
    """문서 하나에 대해 스키마 호출그룹을 차례로 돌려 필드를 채운다.

    한 그룹이 실패해도 나머지는 진행하고, 실패 사실을 errors 에 남긴다(조용한 실패 금지).

    parse_doc 은 같은 문서의 파싱 원본(out/json)이다. 주면 llm_view 에 안 실린 영역을
    (C)'입력 유실' 근거로 남긴다 — 없어도 추출은 동일하게 동작한다.
    """
    pg = product_group or view.get("product_group")
    at = ad_type or view.get("ad_type")
    if not pg:
        return {"error": "product_group 미확정 — 스키마를 고를 수 없음", "document": view.get("document")}

    pack = load_pack(pg, at)
    doc_text = _render_doc(view)

    result: dict = {
        "document": view.get("document"),
        "doc_id": view.get("doc_id"),
        "product_group": pg,
        "ad_type": at,
        "schema_id": pack["schema_id"],
        "schema_version": pack["version"],
        "overlays_applied": pack["overlays_applied"],
        "fields": {},
        "observations": {},
        "events": [],
        "unmapped": [],
        "group_analysis": {},
        "errors": [],
    }

    for group in pack["call_groups"]:
        gid = group["group_id"]
        schema = response_schema(group)
        try:
            data = chat_json(
                [{"type": "text", "text": _prompt(group, doc_text)}],
                schema_name=f"extract_{gid}",
                schema=schema,
                max_tokens=_max_tokens(group),
            )
            _validate_group_response(data)
        except Exception as exc:
            result["errors"].append({"group": gid, "error": str(exc)})
            continue

        result["group_analysis"][gid] = data.get("analysis", "")
        for item in data.get("unmapped", []) or []:
            item["group"] = gid
            result["unmapped"].append(item)
        result["unmapped"] = _dedupe_unmapped(result["unmapped"])

        if "fields" in data:
            for key, val in (data["fields"] or {}).items():
                val["group"] = gid
                _normalize_status(val, key, result)
                result["fields"][key] = val
        if "observations" in data:
            for key, val in (data["observations"] or {}).items():
                if val:
                    result["observations"][key] = val
        if "events" in data:
            result["events"] = data.get("events") or []
            result["event_count_reported"] = data.get("event_count")

    _reclassify_already_handled(result)
    prune_empty_events(result)
    # 미발견 필드의 3분류(해당없음 / 미표시 / 확인필요) — 코드 전용, LLM 호출 없음.
    # not_found 한 통에 뭉쳐 두면 심의 지적사항(미표시)을 골라낼 수 없다.
    result["review_gaps"] = classify_absences(result, pack)
    # (C) 근거: 파싱은 됐는데 STAGE_3 입력에 안 실린 영역 — 있으면 '미표시'가 사실은
    # 우리 입력 누락일 수 있다는 뜻이라 같이 남긴다.
    result["input_gap"] = input_gap(view, parse_doc)
    # 근거 대조(코드 전용, LLM 호출 없음) — LLM 이 스스로 적어낸 evidence 를 검산한다.
    result["evidence_unbacked"] = _verify_evidence(view, result, _unverifiable_field_keys(pack))
    # 어느 값에도 안 실린 수치 — 전수수집 배열이 놓친 표기 불일치의 흔적
    result["unused_figures"] = _unused_figures(view, result)
    result["coverage"] = compute_coverage(view, result)

    # 최종 출력 계약 검증(경계 ②) — 내부 코드가 계약을 어겼는지 확인한다. 여기서
    # 실패하면 우리 쪽 버그라는 뜻이라 조용히 넘기지 않고 그대로 올린다(catch 안 함) —
    # 잘못된 모양을 파일로 써서 다음 단계(RAG/DB)에 넘기는 것보다 여기서 멈추는 게 낫다.
    validated = ExtractResult.model_validate(result)
    return validated.model_dump(mode="json", exclude_none=True)


def _validate_group_response(data: dict) -> None:
    """VLM 응답이 strict 계약(schema_pack.response_schema)을 실제로 지켰는지 재확인(경계 ①).

    guided decoding 이 모양을 강제하지만 100%는 아니다 — gemma_client 의
    _repair_trailing_escape 가 이미 서버측 결함(문자열 종료부 손상)을 잡아낸 적이
    있다. 여기서 걸리면 chat_json 호출 실패와 동일하게 취급돼(호출측 try/except가
    잡음) 그룹 전체를 스킵한다 — 절반만 검증된 필드를 섞어 쓰는 것보다 그룹 전체를
    버리는 편이 안전하고, 어차피 그룹이 실패 처리의 최소 단위다.
    """
    for val in (data.get("fields") or {}).values():
        FieldCell.model_validate(val)
    for vals in (data.get("observations") or {}).values():
        for v in vals or []:
            ObservationItem.model_validate(v)
    for ev in data.get("events") or []:
        for val in (ev or {}).values():
            FieldCell.model_validate(val)
    for item in data.get("unmapped") or []:
        UnmappedItem.model_validate(item)


# 부분문자열 대조로는 못 잡는 의역(어순이 달라진 재진술)을 잡기 위한 완화된 임계.
# BACKED_TOKEN_RATIO(0.8)는 "이 값이 이 근거에 있다"는 근거검증용이라 엄격하고,
# 여기는 반대로 "같은 사실의 다른 표현인가"라 완전 일치를 기대하면 안 된다.
# 그래도 못 잡는 표기 차이가 남을 수 있음(예: '선착순 3만좌만' vs '판매한도:3만좌') —
# 어순이 크게 다른 의역까지 완벽히 잡으려면 결국 LLM 판단이 필요하고, 항목이 문서당
# 0~5건뿐인 지금 표본에서는 그 상시 호출 비용이 안 맞아 보류(2026-07-28).
_RECLASSIFY_TOKEN_RATIO = 0.6


def _reclassify_already_handled(result: dict) -> None:
    """다른 그룹이 이미 값으로 뽑은 문구가 '필드 없음'으로 올라온 것을 걸러낸다.

    호출을 그룹으로 쪼갠 부작용 — 예컨대 심의필 문구는 G3 가 review_stamp 로 제대로 뽑는데
    G1·G2·G4 는 "우리 그룹에 넣을 항목이 없다"며 미배정으로 올린다. 그대로 두면
    unmapped_schema_gap(다음 스키마 버전 작업목록)이 부풀어 판단을 흐린다.
    문구를 지우지는 않고 kind 만 바꿔 남긴다(무엇을 걸렀는지 보이게).
    """
    def squash(s: str) -> str:
        return "".join(ch for ch in str(s) if not ch.isspace())

    extracted: list[str] = []
    for v in result["fields"].values():
        val = v.get("value")
        if isinstance(val, list):
            extracted += [squash(x) for x in val if x]
        elif val:
            extracted.append(squash(val))
    for ev in result["events"]:
        for v in (ev.values() if isinstance(ev, dict) else []):
            val = v.get("value") if isinstance(v, dict) else v
            if isinstance(val, list):
                extracted += [squash(x) for x in val if x]
            elif val:
                extracted.append(squash(val))
    extracted = [e for e in extracted if len(e) >= 6]
    extracted_blob = " ".join(extracted)

    for u in result["unmapped"]:
        if u.get("kind") != "심의관련_필드없음":
            continue
        t = squash(u.get("text", ""))
        if not t:
            continue
        # 1차: 부분문자열 대조(값을 그대로 옮겨적은 경우) — 값 하나하나와 직접 비교.
        if any(t in e or e in t for e in extracted):
            u["kind"] = "다른항목에서_처리됨"
            continue
        # 2차: 토큰 겹침 비율(어순이 달라진 의역) — 뽑힌 값 전체를 배경 텍스트로 본다.
        if check_field_consistency(u.get("text", ""), extracted_blob) >= _RECLASSIFY_TOKEN_RATIO:
            u["kind"] = "다른항목에서_처리됨"


def prune_empty_events(result: dict) -> None:
    """값이 하나도 없는 이벤트 항목을 걷어낸다. 조용히 지우지 않고 사실을 남긴다.

    실측(002, 2026-07-28): 모델이 `event_count=1` 이라고 답해놓고 events 배열에는 2개를
    보냈고 두 번째는 전 필드가 빈 값이었다. 그대로 두면 부재 판정이 그 유령 이벤트의
    필수 항목 전부를 '미표시 = 심의 지적사항' 6건으로 올린다 — 없는 지적을 만들어내는
    셈이라 제품 신뢰도에 직결된다. 이벤트 개수는 원래 실행마다 1↔2 로 흔들리는 항목이라
    (스키마 문서 기록) 프롬프트로 막기보다 코드가 검산하는 편이 확실하다.

    스키마가 이미 `event_count` 를 받아 두고 있었는데 아무도 안 쓰고 있었다 —
    그 값과 배열 길이의 불일치도 함께 기록한다.
    """
    events = result.get("events") or []
    if not events:
        return
    kept, dropped = [], []
    for i, ev in enumerate(events, 1):
        has_value = isinstance(ev, dict) and any(
            isinstance(v, dict) and v.get("status") in ("found", "uncertain")
            for v in ev.values()
        )
        (kept if has_value else dropped).append(i)
    if not dropped:
        return
    result["events"] = [events[i - 1] for i in kept]
    reported = result.get("event_count_reported")
    result["events_pruned"] = {
        "dropped_indexes": dropped,
        "reason": "값이 하나도 없는 이벤트 — 부재 판정에서 허위 지적사항을 만든다",
        "event_count_reported": reported,
        "array_length": len(events),
    }


def _region_texts(view: dict) -> dict[str, str]:
    """region_id → STAGE_3 가 그 영역에서 볼 수 있었던 텍스트 전부(정본 + 통독 후보).

    후보까지 합치는 이유: 프롬프트가 "정본과 후보 중 정확한 쪽을 골라라"라고 지시하므로,
    후보에서 고른 값을 환각으로 오판하면 안 된다.
    """
    out: dict[str, str] = {}
    for page in view.get("pages", []):
        for r in page.get("regions", []):
            parts = [r.get("text") or "", r.get("vlm_reading") or ""]
            out[r["region_id"]] = " ".join(p for p in parts if p)
        # 미배정 덩어리도 근거로 지목할 수 있게 됐으므로(_render_doc), 대조 대상에 넣는다.
        # 안 넣으면 정상 인용이 '존재하지 않는 region_id'= 환각 신호로 잘못 잡힌다.
        if page.get("unassigned"):
            out[_unassigned_id(page)] = page["unassigned"]
    return out


# 값 안에 주석으로 박힌 근거 표기 — '(p1_r008)', '(p1_r014, p1_r017)'
_EVIDENCE_ANNOT_RE = re.compile(r"\(\s*p\d+_r\d+(?:\s*,\s*p\d+_r\d+)*\s*\)")


def _score_evidence(item: dict, value_key: str, region_texts: dict[str, str]) -> None:
    """값이 자기가 지목한 근거 영역에 실재하는지 대조해 점수를 붙인다 (LLM 호출 없음).

    evidence 는 파이프라인이 계산해 준 사실이 아니라 STAGE_3 LLM 이 스스로 적어낸
    주장이다. 검산이 없으면 지어낸 값도 근거가 달린 것처럼 보인다 — 003 r016 에서
    통독이 없는 상품명을 만들어낸 실측이 있다(원본 'NH올원모임서비스' → 'NH클럽온뱅크').
    값·status 는 건드리지 않고 신호만 남긴다(조용한 수정 금지).
    """
    raw = item.get(value_key)
    value = " ".join(str(x) for x in raw if x) if isinstance(raw, list) else str(raw or "")
    # 전수수집 배열(rate_mentions·prize_mentions)은 '표기 그대로 (region_id)' 형식이라
    # 그 괄호 주석이 값에 섞여 있다. 원문에는 없는 주석이므로 빼고 대조한다
    # (안 빼면 정상 항목도 점수가 깎여 임계 근처에서 오탐이 난다 — 002 rate_mentions 실측).
    value = _EVIDENCE_ANNOT_RE.sub(" ", value)
    if not value.strip():
        return
    ids = item.get("evidence") or []
    known = [i for i in ids if i in region_texts]
    missing = [i for i in ids if i not in region_texts]
    if missing:
        # 존재하지 않는 region_id 를 근거로 댄 경우 — 그 자체가 강한 환각 신호
        item["evidence_missing"] = missing
    if not known:
        item["evidence_backed"] = None  # 대조 불가 (근거 미제시)
        return
    score = check_field_consistency(value, " ".join(region_texts[i] for i in known))
    item["evidence_score"] = round(score, 3)
    item["evidence_backed"] = score >= BACKED_TOKEN_RATIO


def _unverifiable_field_keys(pack: dict) -> set[str]:
    """값이 광고 원문 그대로가 아니어서 토큰 대조가 의미 없는 필드들.

    두 부류다.
    - enum: 값이 스키마가 정한 분류다(product_subtype='입출금(통장·MMDA)',
      event_prize_kind='일반경품'). 그 낱말이 광고에 없는 게 정상이다.
    - derived: 프롬프트가 '형태 예: 연 X.XX% (YY.MM.DD 기준, 세전)' 처럼 정형을 지시해,
      원문을 규정 표기로 재구성하는 필드다. 실측(2026-07-28): preferential_rate_total 이
      원문 '최고 4.8%p' 에 지시대로 '연' 을 붙여 세 문서 모두 0.667 로 걸렸다.
    둘 다 대조하면 정상 동작이 환각으로 오탐되므로 점수를 매기지 않는다.
    """
    keys: set[str] = set()
    for group in pack.get("call_groups", []):
        for f in (group.get("fields") or []) + (group.get("observation_fields") or []):
            if f.get("enum") or f.get("derived"):
                keys.add(f["field_key"])
    return keys


def _verify_evidence(view: dict, result: dict, skip_keys: set[str]) -> list[str]:
    """모든 값 항목(필드·이벤트·관측·미배정)에 근거 대조 점수를 부착. 반환: 미검증 키 목록."""
    region_texts = _region_texts(view)
    unbacked: list[str] = []

    def run(item: dict, value_key: str, label: str) -> None:
        _score_evidence(item, value_key, region_texts)
        if item.get("evidence_backed") is False or item.get("evidence_missing"):
            unbacked.append(label)

    for key, val in result["fields"].items():
        if key not in skip_keys:
            run(val, "value", key)
    for i, ev in enumerate(result["events"], 1):
        if isinstance(ev, dict):
            for key, val in ev.items():
                if isinstance(val, dict) and key not in skip_keys:
                    run(val, "value", f"event{i}.{key}")
    for key, obs_list in result["observations"].items():
        for j, obs in enumerate(obs_list or []):
            if isinstance(obs, dict):
                run(obs, "quote", f"{key}[{j}]")
    for j, u in enumerate(result["unmapped"]):
        run(u, "text", f"unmapped[{j}]")
    return unbacked


# 수치+단위 한 덩어리 (금액·인원·기간·비율). 심의에서 어긋나면 문제가 되는 표기들.
_FIGURE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%p|%|억원|만원|천원|원|명|팀|좌|개월|년|일|회|건)")


def _unused_figures(view: dict, result: dict) -> list[dict]:
    """광고 본문에는 있는데 어떤 값에도 실리지 않은 수치를 찾는다 (코드 전용).

    '한 개념 = 한 필드' 구조는 광고의 중복·불일치 표기를 지운다. 실측: 003 은
    헤드라인에 '추첨을 통해 총 1,000명', 경품박스에 '(선착순 1,000팀)' 으로 서로
    다르게 적혀 있는데 STAGE_3 가 '1,000팀' 하나로 합쳐버려 불일치가 사라졌다 —
    그 불일치야말로 심의가 잡아야 할 대상이다. 스키마의 전수수집 배열이 1차 방어이고,
    이건 그것마저 놓쳤을 때 남는 흔적이다(판정 아님, 검토 신호).

    정본(text)만 훑는다 — 통독 후보의 환각 수치를 '유실'로 잘못 보고하지 않기 위해서.
    """
    def squash(s: str) -> str:
        return "".join(str(s).split())

    used: list[str] = []
    for v in result["fields"].values():
        raw = v.get("value")
        used += [squash(x) for x in raw] if isinstance(raw, list) else [squash(raw or "")]
    for ev in result["events"]:
        for v in (ev.values() if isinstance(ev, dict) else []):
            raw = v.get("value") if isinstance(v, dict) else v
            used += [squash(x) for x in raw] if isinstance(raw, list) else [squash(raw or "")]
    for obs_list in result["observations"].values():
        used += [squash(o.get("quote", "")) for o in (obs_list or []) if isinstance(o, dict)]
    used += [squash(u.get("text", "")) for u in result["unmapped"]]
    blob = " ".join(u for u in used if u)

    seen: set[str] = set()
    out: list[dict] = []
    for page in view.get("pages", []):
        for r in page.get("regions", []):
            for m in _FIGURE_RE.findall(r.get("text") or ""):
                fig = squash(m)
                if fig in seen or fig in blob:
                    continue
                seen.add(fig)
                out.append({"figure": m.strip(), "region_id": r["region_id"]})
    return out


def compute_coverage(view: dict, result: dict) -> dict:
    """근거로 지목된 region 비율 — 스키마가 광고 내용을 얼마나 덮었는지 측정.

    낮으면 (a) 스키마 필드가 부족하거나 (b) LLM 이 근거를 안 달았다는 신호다.
    unmapped 의 '심의관련_필드없음' 건수는 다음 스키마 버전의 작업 목록이 된다.
    """
    all_regions: set[str] = set()
    for page in view.get("pages", []):
        for r in page.get("regions", []):
            all_regions.add(r["region_id"])

    cited: set[str] = set()

    def _cite(container):
        for v in container:
            for rid in (v.get("evidence") or []):
                if rid in all_regions:
                    cited.add(rid)

    _cite(result["fields"].values())
    for obs_list in result["observations"].values():
        _cite(obs_list)
    for ev in result["events"]:
        _cite(ev.values() if isinstance(ev, dict) else [])
    _cite(result["unmapped"])

    found = sum(1 for v in result["fields"].values() if v.get("status") == "found")
    not_found = sum(1 for v in result["fields"].values() if v.get("status") == "not_found")
    uncertain = sum(1 for v in result["fields"].values() if v.get("status") == "uncertain")
    # 부재 내역은 필드 기준으로 센다(events 는 배열이라 분모가 다르다 — 따로 보고).
    kinds: dict[str, int] = {}
    for v in result["fields"].values():
        kind = (v.get("absence") or {}).get("kind")
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
    event_missing = sum(
        1 for item in (result.get("review_gaps") or {}).get("미표시", [])
        if item["field_key"].startswith("event")
    )

    return {
        "regions_total": len(all_regions),
        "regions_cited": len(cited),
        "regions_uncited": sorted(all_regions - cited),
        "region_coverage": round(len(cited) / len(all_regions), 3) if all_regions else None,
        "fields_found": found,
        "fields_not_found": not_found,
        "fields_uncertain": uncertain,
        # fields_not_found 의 내역 — 네 값의 합이 fields_not_found 와 같다.
        # 성능 지표로 볼 것은 fields_not_found 가 아니라 '미표시'(심의 지적사항)와
        # '확인필요'(판정 불가) 쪽이다.
        "absence_missing": kinds.get("미표시", 0),
        "absence_not_applicable": kinds.get("해당없음", 0),
        "absence_out_of_scope": kinds.get("판정제외", 0),
        "absence_needs_check": kinds.get("확인필요", 0),
        # 이벤트 배열 안에서 난 미표시 (분모가 이벤트 개수라 위 숫자와 섞지 않는다)
        "absence_missing_in_events": event_missing,
        # STAGE_3 입력에서 빠진 영역 수 — 0 이 아니면 '미표시' 판정에 입력 누락이 섞일 수 있다
        "input_gap_regions": len(result.get("input_gap", [])),
        "unmapped_total": len(result["unmapped"]),
        "unmapped_schema_gap": sum(
            1 for u in result["unmapped"] if u.get("kind") == "심의관련_필드없음"
        ),
        # 근거 대조에서 걸린 항목 수 — 값이 지목한 영역에 실재하지 않음(환각 의심)
        "evidence_unbacked_count": len(result.get("evidence_unbacked", [])),
        # 광고에 있으나 어느 값에도 안 실린 수치 — 표기 불일치 유실 의심
        "unused_figures_count": len(result.get("unused_figures", [])),
    }
