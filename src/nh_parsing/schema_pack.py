from __future__ import annotations

"""상품군 스키마 팩 — 데이터 파일(schemas/*.json)을 읽어 STAGE_3 호출용으로 변환한다.

스키마는 코드에 하드코딩하지 않는다. 규정이 바뀌면 근거 대장
(out/schema_source/_product_group_fields.json)을 고치고 schemas/*.json 을 갱신하면
코드 변경 없이 반영된다.

- load_pack(product_group, ad_type): 상품군 스키마 + 조건에 맞는 오버레이를 합쳐 호출그룹 목록으로
- response_schema(group): 그 호출그룹의 strict json_schema (vLLM guided decoding 용)
- check_coverage(): 근거 대장의 '표시의무' 항목이 스키마에 빠짐없이 반영됐는지 검사 (조용한 누락 방지)

응답 형태 설계 메모
- 값 타입은 string/array 로만 쓴다. nullable union 은 guided decoding 에서 불안정해
  "없으면 빈 문자열 + status=not_found" 규약으로 대체한다(status 가 권위 신호).
- 배열이 스키마 선두에 오면 퇴행하는 사례가 있어 analysis(문자열)를 항상 첫 필드로 둔다.
- unmapped 는 모든 호출그룹에 필수다. 어느 필드에도 안 맞은 텍스트를 여기 남기지 않으면
  조용히 유실된다 — 금융광고 심의에서 가장 위험한 실패다.
"""

import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "out" / "schema_source" / "_product_group_fields.json"
)

STATUS_ENUM = ["found", "not_found", "uncertain"]
UNMAPPED_KIND = ["심의무관", "심의관련_필드없음"]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_pack(product_group: str, ad_type: str | None = None) -> dict:
    """상품군 스키마에 조건이 맞는 오버레이를 합쳐 반환한다.

    반환: {schema_id, version, call_groups:[...], overlays_applied:[...]}
    """
    base_path = SCHEMA_DIR / f"{product_group}.json"
    if not base_path.exists():
        raise FileNotFoundError(
            f"상품군 스키마 없음: {base_path.name} (PoC 대상은 '예금성')"
        )
    base = _load(base_path)
    groups = list(base.get("call_groups", []))
    applied: list[str] = []

    for path in sorted(SCHEMA_DIR.glob("_overlay_*.json")):
        ov = _load(path)
        cond = ov.get("applies_to", {})
        want_ad = cond.get("ad_type")
        if want_ad and (ad_type not in want_ad):
            continue
        want_group = cond.get("product_group")
        if want_group and product_group not in want_group:
            continue
        groups.extend(ov.get("call_groups", []))
        applied.append(ov.get("schema_id", path.stem))

    return {
        "schema_id": base.get("schema_id", product_group),
        "version": base.get("version", "v1"),
        "call_groups": groups,
        "overlays_applied": applied,
    }


def _value_schema(field: dict) -> dict:
    """필드 하나의 value 부분 스키마."""
    if field.get("type") == "list":
        return {"type": "array", "items": {"type": "string"}}
    if field.get("enum"):
        # enum 에 빈 문자열을 더해 '표기 없음'을 표현 (nullable union 회피)
        return {"type": "string", "enum": list(field["enum"]) + [""]}
    return {"type": "string"}


def _unmapped_schema() -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "kind": {"type": "string", "enum": UNMAPPED_KIND},
                "reason": {"type": "string"},
            },
            "required": ["text", "evidence", "kind", "reason"],
            "additionalProperties": False,
        },
    }


def response_schema(group: dict) -> dict:
    """호출그룹 하나의 strict json_schema. group_id 로 세 가지 형태를 만든다."""
    if group.get("observation_fields"):
        return _observation_schema(group)
    if group.get("cardinality") == "list_of_events":
        return _event_schema(group)
    return _fields_schema(group)


def _fields_schema(group: dict) -> dict:
    props: dict = {}
    for f in group["fields"]:
        props[f["field_key"]] = {
            "type": "object",
            "properties": {
                "value": _value_schema(f),
                "evidence": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": STATUS_ENUM},
                "note": {"type": "string"},
            },
            "required": ["value", "evidence", "status", "note"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "fields": {
                "type": "object",
                "properties": props,
                "required": list(props.keys()),
                "additionalProperties": False,
            },
            "unmapped": _unmapped_schema(),
        },
        "required": ["analysis", "fields", "unmapped"],
        "additionalProperties": False,
    }


def _observation_schema(group: dict) -> dict:
    props: dict = {}
    for f in group["observation_fields"]:
        props[f["field_key"]] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                },
                "required": ["quote", "evidence", "why"],
                "additionalProperties": False,
            },
        }
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "observations": {
                "type": "object",
                "properties": props,
                "required": list(props.keys()),
                "additionalProperties": False,
            },
            "unmapped": _unmapped_schema(),
        },
        "required": ["analysis", "observations", "unmapped"],
        "additionalProperties": False,
    }


def _event_schema(group: dict) -> dict:
    props: dict = {}
    for f in group["fields"]:
        props[f["field_key"]] = {
            "type": "object",
            "properties": {
                "value": _value_schema(f),
                "evidence": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": STATUS_ENUM},
                "note": {"type": "string"},
            },
            "required": ["value", "evidence", "status", "note"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "event_count": {"type": "integer"},
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": list(props.keys()),
                    "additionalProperties": False,
                },
            },
            "unmapped": _unmapped_schema(),
        },
        "required": ["analysis", "event_count", "events", "unmapped"],
        "additionalProperties": False,
    }


# ──────────────────────────── 커버리지 검사 ────────────────────────────


def check_coverage(product_group: str = "예금성") -> dict:
    """근거 대장의 '표시의무' 항목이 스키마 필드에 반영됐는지 검사.

    대장에 있는데 스키마가 안 가리키는 표시의무 항목 = 조용한 누락 후보 → missing 으로 보고.
    양식(layout)·절차(metadata) 항목은 텍스트 추출 대상이 아니므로 제외하고 그 사실을 남긴다.
    """
    catalog = _load(CATALOG_PATH)["field_catalog"]
    pack = load_pack(product_group, ad_type="이벤트페이지")  # 오버레이까지 포함해 최대 커버리지

    refs: set[str] = set()
    for g in pack["call_groups"]:
        for f in g.get("fields", []) + g.get("observation_fields", []):
            # 한 스키마 필드가 대장의 여러 항목을 함께 커버할 수 있다(catalog_refs).
            for ref in f.get("catalog_refs", []) or []:
                refs.add(ref)
            ref = f.get("catalog_ref")
            if ref:
                refs.add(ref)

    sections = {"예금성": ["common", "deposit", "event_overlay"]}.get(
        product_group, ["common", "event_overlay"]
    )
    missing, excluded = [], []
    total = 0
    for sec in sections:
        for item in catalog.get(sec, []):
            src = (item.get("extraction") or {}).get("source", "text")
            cat = item.get("category")
            ref = f"{sec}/{item['field_key']}"
            if src in ("layout", "metadata"):
                excluded.append({"ref": ref, "label": item["label"], "reason": f"source={src} — 텍스트 추출 대상 아님"})
                continue
            if cat == "절차":
                excluded.append({"ref": ref, "label": item["label"], "reason": "category=절차 — 광고물 판독 대상 아님"})
                continue
            total += 1
            if ref not in refs:
                missing.append({"ref": ref, "label": item["label"], "category": cat})

    return {
        "product_group": product_group,
        "catalog_text_items": total,
        "covered": total - len(missing),
        "missing": missing,
        "excluded_from_extraction": excluded,
        "schema_field_count": sum(
            len(g.get("fields", [])) + len(g.get("observation_fields", []))
            for g in pack["call_groups"]
        ),
        "call_groups": [g["group_id"] for g in pack["call_groups"]],
    }
