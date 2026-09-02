# -*- coding: utf-8 -*-
"""evidence JSON을 다음 심의/RAG 단계용 필드 중심 JSON으로 투영한다.

``nh-ad-review-evidence-v4``는 좌표, 파서 기본 텍스트, VLM 판독 관측을 빠짐없이
보존하는 감사 원본이다. 그 구조를 하류가 매번 pages→regions→lines로 풀지 않도록 이 모듈은
두 개의 서로 배타적인 문구 목록을 만든다.

* ``template_fields``: 선택된 광고 템플릿의 라벨에 배정된 파서 기본 문구
* ``unmapped_ad_copy``: 어떤 템플릿 라벨에도 배정되지 않은 파서 기본 문구

각 필드 값과 일반 문구는 원본 evidence의 line_ref/bbox/card/VLM 비교 상태를 가리킨다.
따라서 이 출력은 편의용 요약일 뿐, VLM 결과나 새 문장을 파서 기본값으로 승격하지
않는다. 페이지 sweep 회수 후보는 파서 기본 문구와 섞지 않고 별도 목록으로 남긴다.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


CONTRACT_VERSION = "nh-ad-review-input-v2"


def build_ad_review_input(evidence: dict[str, Any]) -> dict[str, Any]:
    """근거 JSON 하나를 다음 단계 소비용 두-목록 계약으로 바꾼다.

    ``review_targets[].evidence.line_refs``가 현재 템플릿 라벨 배정의 유일한 원천이다.
    라벨을 다시 추론하거나 VLM 텍스트를 새 값으로 쓰지 않아, 이 투영 자체가 VLM
    결과를 흔들지 않는다.
    """
    _require_evidence_contract(evidence)
    line_index, ordered_refs, recovery_candidates = _line_index(evidence)

    refs_by_target: dict[str, list[str]] = {}
    targets_by_ref: dict[str, list[str]] = defaultdict(list)
    for target in evidence.get("review_targets") or []:
        target_id = str(target.get("target_id") or "")
        refs = _unique_in_document_order(
            target.get("evidence", {}).get("line_refs") or [], ordered_refs,
        )
        unknown = set(target.get("evidence", {}).get("line_refs") or []) - set(line_index)
        if unknown:
            raise ValueError(f"review_target {target_id!r}가 없는 line_ref를 가리킨다: {sorted(unknown)!r}")
        refs_by_target[target_id] = refs
        for ref in refs:
            targets_by_ref[ref].append(target_id)

    template_fields = [
        _template_field(target, refs_by_target.get(str(target.get("target_id") or ""), []), line_index)
        for target in evidence.get("review_targets") or []
    ]
    assigned_refs = {ref for refs in refs_by_target.values() for ref in refs}
    unmapped_ad_copy = _unmapped_copy(
        [ref for ref in ordered_refs if ref not in assigned_refs], line_index,
    )
    _verify_partition(ordered_refs, template_fields, unmapped_ad_copy)

    return {
        "contract": {
            "version": CONTRACT_VERSION,
            "source_evidence_contract": (evidence.get("reading_evidence_contract") or {}).get("version"),
            "parser_primary_text_policy": (
                "template_fields + unmapped_ad_copy partition parser primary lines"
            ),
            "vlm_observation_policy": "observation_only; never replaces parser primary text",
            "table_policy": "structurev3 region bbox + VLM table observation; PaddleX grid excluded",
        },
        "document": {
            "doc_id": evidence.get("doc_id"),
            "source_file": evidence.get("source_file"),
            "file_type": evidence.get("file_type"),
            "classification": evidence.get("classification") or {},
            "template": evidence.get("template") or {},
        },
        "template_fields": template_fields,
        "unmapped_ad_copy": unmapped_ad_copy,
        "unverified_recovery_candidates": recovery_candidates,
        "summary": {
            "parser_primary_line_total": len(ordered_refs),
            "template_assigned_parser_primary_line_count": len(assigned_refs),
            "unmapped_ad_copy_parser_primary_line_count": len(ordered_refs) - len(assigned_refs),
            "template_field_count": len(template_fields),
            "located_template_field_count": sum(
                1 for field in template_fields if field["evidence_status"] == "located"
            ),
            "unmapped_ad_copy_count": len(unmapped_ad_copy),
            "table_region_count": len({
                (entry["page_no"], entry["region_id"])
                for entry in line_index.values()
                if entry.get("table") is not None and entry.get("region_id") is not None
            }),
            "recovery_candidate_count": len(recovery_candidates),
        },
    }


def _require_evidence_contract(evidence: dict[str, Any]) -> None:
    if not isinstance(evidence.get("pages"), list):
        raise ValueError("evidence JSON에 pages[]가 없다")
    version = str((evidence.get("reading_evidence_contract") or {}).get("version") or "")
    # 기존 evidence-v2/v3도 새 투영을 시험할 수 있게 허용한다. 이전 표 격자는 읽지 않는다.
    if version and not version.startswith("nh-ad-review-evidence-v"):
        raise ValueError(f"지원하지 않는 evidence 계약: {version!r}")


def _line_index(evidence: dict[str, Any]) -> tuple[dict[str, dict], list[str], list[dict]]:
    index: dict[str, dict] = {}
    ordered: list[str] = []
    recovery: list[dict] = []
    for page in evidence.get("pages") or []:
        page_no = page.get("page_no")
        for region in page.get("regions") or []:
            context = _region_context(page_no, region)
            for line in region.get("lines") or []:
                ref = line.get("line_ref")
                if not ref:
                    raise ValueError(f"line_ref가 없는 영역 줄: {region.get('region_id')!r}")
                if ref in index:
                    raise ValueError(f"중복 line_ref: {ref!r}")
                index[ref] = {"line": line, **context}
                ordered.append(ref)
        for line in page.get("unassigned_lines") or []:
            ref = line.get("line_ref")
            if not ref:
                raise ValueError(f"line_ref가 없는 미배정 줄: page={page_no!r}")
            if ref in index:
                raise ValueError(f"중복 line_ref: {ref!r}")
            index[ref] = {
                "line": line,
                "page_no": page_no,
                "region_id": None,
                "region_bbox": None,
                "card_no": None,
                "layout": None,
                "visibility": None,
                "vlm_region_reading": None,
                "table": None,
                "is_unassigned": True,
            }
            ordered.append(ref)
        for candidate in page.get("recovery_candidates") or []:
            recovery.append({"page_no": page_no, **candidate})
    return index, ordered, recovery


def _region_context(page_no: int | None, region: dict) -> dict:
    text_evidence = region.get("text_evidence") or {}
    # v4 공개 명명과 v2/v3 호환 입력을 둘 다 받는다. 새 출력은 v4 명명만 쓴다.
    vlm_reading = text_evidence.get("vlm_region_reading") or text_evidence.get("reader") or {}
    comparison = (
        text_evidence.get("parser_vlm_comparison")
        or text_evidence.get("adjudication")
        or {}
    )
    return {
        "page_no": page_no,
        "region_id": region.get("region_id"),
        "region_bbox": region.get("bbox"),
        "card_no": region.get("card_no"),
        "layout": region.get("layout"),
        "visibility": region.get("visibility"),
        "vlm_region_reading": {
            "text": vlm_reading.get("text") or comparison.get("vlm_region_text"),
            "confidence": (
                vlm_reading.get("confidence")
                if vlm_reading.get("confidence") is not None
                else comparison.get("vlm_region_confidence", comparison.get("reader_confidence"))
            ),
            "comparison_status": (
                comparison.get("comparison_status")
                or _legacy_comparison_status(comparison.get("status"))
                if comparison else "not_run"
            ),
            "comparison_relation": (
                comparison.get("comparison_relation") or comparison.get("relation")
            ) if comparison else None,
        } if vlm_reading or comparison else None,
        # v2의 cells/html은 여기서 절대 복사하지 않는다.
        "table": _table_observation(region),
        "is_unassigned": False,
    }


def _table_observation(region: dict) -> dict | None:
    table = region.get("table")
    if not table:
        return None
    vlm_reading = table.get("vlm_table_reading") or table.get("reader")
    if isinstance(vlm_reading, dict):
        return {
            "detected_by": table.get("detected_by") or "structurev3",
            "paddlex_grid_used": False,
            "parser_primary_line_refs": (
                table.get("parser_primary_line_refs")
                or table.get("canonical_line_refs")
                or []
            ),
            "vlm_table_reading": {
                "source": vlm_reading.get("source"),
                "status": vlm_reading.get("status"),
                "text": vlm_reading.get("text"),
                "rows": vlm_reading.get("rows") or [],
                "confidence": vlm_reading.get("confidence"),
                "structure_confidence": vlm_reading.get("structure_confidence"),
            },
        }

    # 기존 v2 테스트 결과는 일반 VLM 영역 판독 문자열만 가지고 있다. 그 문자열을 셀/행
    # 구조라고 주장하지 않고 text-only 관측으로만 옮긴다.
    legacy_reader = (region.get("text_evidence") or {}).get("reader") or {}
    return {
        "detected_by": "structurev3",
        "paddlex_grid_used": False,
        "parser_primary_line_refs": [line.get("line_ref") for line in region.get("lines") or []],
        "vlm_table_reading": {
            "source": "legacy_region_reading",
            "status": "legacy_text_only" if legacy_reader.get("text") else "not_run",
            "text": legacy_reader.get("text"),
            "rows": [],
            "confidence": legacy_reader.get("confidence"),
            "structure_confidence": None,
        },
    }


def _template_field(target: dict, refs: list[str], index: dict[str, dict]) -> dict:
    values = _values_for_refs(refs, index, value_prefix=str(target.get("target_id") or "template:unknown"))
    return {
        "target_id": target.get("target_id"),
        "label": target.get("label"),
        # 템플릿에는 별도 영문 field_key가 없으므로 target_id가 안정적인 기계 식별자다.
        "field_key": target.get("target_id"),
        "requirement": target.get("requirement"),
        "writing_rules": target.get("writing_rules") or [],
        "evidence_status": target.get("evidence_status"),
        "fixed_phrase_checks": (target.get("evidence") or {}).get("fixed_phrase_checks") or [],
        "values": values,
    }


def _values_for_refs(refs: list[str], index: dict[str, dict], *, value_prefix: str) -> list[dict]:
    grouped: dict[tuple, list[str]] = defaultdict(list)
    for ref in refs:
        entry = index[ref]
        # 하나의 라벨이 여러 상품 카드나 여러 독립 영역에 반복될 수 있으므로 합치지 않는다.
        key = (entry["page_no"], entry["region_id"], entry["card_no"])
        grouped[key].append(ref)

    values: list[dict] = []
    for ordinal, refs_in_group in enumerate(grouped.values(), start=1):
        first = index[refs_in_group[0]]
        lines = [_line_evidence(index[ref]["line"]) for ref in refs_in_group]
        values.append({
            "value_id": f"{value_prefix}:value:{ordinal}",
            # 이 값은 문자열을 새로 추출/정규화하지 않고, 라벨에 배정된 파서 기본 줄을
            # 그대로 이어 붙인 것이다.
            "value": "\n".join(line["parser_text"] for line in lines).strip(),
            "value_source": "parser_primary_text",
            "page_no": first["page_no"],
            "region_id": first["region_id"],
            "region_bbox": first["region_bbox"],
            "card_no": first["card_no"],
            "lines": lines,
            "vlm_region_reading": first["vlm_region_reading"],
            "table": first["table"],
        })
    return values


def _unmapped_copy(refs: list[str], index: dict[str, dict]) -> list[dict]:
    grouped: dict[tuple, list[str]] = defaultdict(list)
    for ref in refs:
        entry = index[ref]
        key = (entry["page_no"], entry["region_id"], entry["card_no"])
        grouped[key].append(ref)

    output: list[dict] = []
    for ordinal, refs_in_group in enumerate(grouped.values(), start=1):
        first = index[refs_in_group[0]]
        lines = [_line_evidence(index[ref]["line"]) for ref in refs_in_group]
        suffix = first["region_id"] or f"p{first['page_no']}_unassigned"
        output.append({
            "copy_id": f"copy:{suffix}:{ordinal}",
            "assignment_status": "unmapped_to_template",
            "ad_copy_text": "\n".join(line["parser_text"] for line in lines).strip(),
            "page_no": first["page_no"],
            "region_id": first["region_id"],
            "region_bbox": first["region_bbox"],
            "card_no": first["card_no"],
            "layout": first["layout"],
            "visibility": first["visibility"],
            "lines": lines,
            "vlm_region_reading": first["vlm_region_reading"],
            "table": first["table"],
        })
    return output


def _line_evidence(line: dict) -> dict:
    return {
        "line_ref": line.get("line_ref"),
        "parser_text": line.get("parser_text", line.get("text") or ""),
        "bbox": line.get("bbox"),
        "text_source": line.get("text_source", line.get("source")),
        "ocr_confidence": line.get("ocr_confidence", line.get("confidence")),
    }


def _unique_in_document_order(refs: list[str], ordered_refs: list[str]) -> list[str]:
    wanted = set(refs)
    return [ref for ref in ordered_refs if ref in wanted]


def _verify_partition(ordered_refs: list[str], fields: list[dict], unmapped: list[dict]) -> None:
    assigned = {
        line["line_ref"]
        for field in fields for value in field.get("values") or []
        for line in value.get("lines") or []
    }
    remaining = {
        line["line_ref"]
        for copy in unmapped for line in copy.get("lines") or []
    }
    all_refs = set(ordered_refs)
    if assigned & remaining:
        raise ValueError(f"template/unmapped 문구가 겹친다: {sorted(assigned & remaining)[:3]!r}")
    if assigned | remaining != all_refs:
        missing = all_refs - (assigned | remaining)
        extra = (assigned | remaining) - all_refs
        raise ValueError(f"ad-review-input 문구 분할 오류: 누락={sorted(missing)[:3]!r}, 초과={sorted(extra)[:3]!r}")


def _legacy_comparison_status(status: object) -> str | None:
    """v2/v3 evidence의 내부 상태를 v2 review-input 공개 상태로 읽는다."""
    return {
        "reader_failed": "vlm_reading_failed",
        "agreed": "parser_and_vlm_agree",
        "judge_selected": "judge_selected_parser_primary_text",
        "uncertain": "needs_human_review",
        "judge_failed": "vlm_judge_failed",
    }.get(str(status or ""))
