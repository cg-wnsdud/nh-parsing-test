# -*- coding: utf-8 -*-
"""P1 evidence를 최소 심의 전달용 ``review_text``로 투영한다.

P1(``nh-ad-review-evidence-v6``)은 파서 기본 문구, 영역 전체 VLM 판독, Judge 근거와
bbox를 모두 보존하는 감사 원본이다. P2는 심의가 읽을 라벨별 문구와 미배정 문구, 그리고
P1로 돌아갈 ``line_refs``만 전달한다. 템플릿의 requirement/writing rules 및 정형문구
매칭 진단은 템플릿 팩/P1의 책임이므로 P2에 반복하지 않는다.

VLM Judge가 고른 문구는 **그 view가 StructureV3 영역의 모든 파서 줄을 포함할 때만**
시험적으로 쓴다. 영역 일부에 해당하는 라벨에는 VLM 전체 문구를 잘라 넣지 않고 파서
기본 줄만 쓴다. 페이지 sweep 회수 후보는 확정 문구와 섞지 않고 별도 목록으로 남긴다.

표는 ``text_views[].tables[]``로 **행/열 구조를 유지한 보조 표현**을 함께 넘긴다.
``review_text``는 그대로 두고 그 옆에 붙이는 이유는, 평면 텍스트로 접으면 격자가 복원
불가능해지기 때문이다 — 실측(`16. 대출성상품` p1_r019): 요율 56줄이 한 줄씩 나열되면
`이하`·`초과` 헤더가 중복되고 (부부합산 연소득 × 임차보증금) 교차 요율을 되살릴 수 없다.
격자는 VLM 관측이고 파서 정본이 아니므로 ``status``/``confidence``를 반드시 같이 보낸다.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


CONTRACT_VERSION = "nh-ad-review-input-v6"


def build_ad_review_input(evidence: dict[str, Any]) -> dict[str, Any]:
    """근거 JSON 하나를 라벨/미배정 심의 문구 계약으로 바꾼다.

    ``review_targets[].evidence.line_refs``가 템플릿 라벨 배정의 유일한 원천이다. 이
    함수는 라벨을 다시 추론하지 않는다. 다만 P1의 Judge 선택이 영역 전체 범위와 정확히
    일치하면 해당 view의 단일 ``review_text``로 사용한다.
    """
    _require_evidence_contract(evidence)
    line_index, ordered_refs, recovery_candidates = _line_index(evidence)

    refs_by_target: dict[str, list[str]] = {}
    for target in evidence.get("review_targets") or []:
        target_id = str(target.get("target_id") or "")
        refs = _unique_in_document_order(
            target.get("evidence", {}).get("line_refs") or [], ordered_refs,
        )
        unknown = set(target.get("evidence", {}).get("line_refs") or []) - set(line_index)
        if unknown:
            raise ValueError(f"review_target {target_id!r}가 없는 line_ref를 가리킨다: {sorted(unknown)!r}")
        refs_by_target[target_id] = refs

    labelled_ad_copy = [
        _labelled_ad_copy(target, refs_by_target.get(str(target.get("target_id") or ""), []), line_index)
        for target in evidence.get("review_targets") or []
    ]
    assigned_refs = {ref for refs in refs_by_target.values() for ref in refs}
    unmapped_ad_copy = _unmapped_review_text_views(
        [ref for ref in ordered_refs if ref not in assigned_refs], line_index,
    )
    _verify_partition(ordered_refs, labelled_ad_copy, unmapped_ad_copy)

    return {
        "contract": {
            "version": CONTRACT_VERSION,
            "source_evidence_contract": (evidence.get("reading_evidence_contract") or {}).get("version"),
            "text_partition": "labelled_ad_copy + unmapped_ad_copy partition P1 parser primary lines",
            "p1_reference": "every transmitted text keeps P1 line_refs; bbox/VLM/Judge detail remains in P1",
            "template_rules": "resolve requirement and writing rules from the configured template pack using document.template and label",
            # 표 격자는 review_text 를 대체하지 않는 보조 표현이다. line_refs 분할 불변식과
            # 무관하며(_verify_partition 은 tables 를 보지 않는다) 소유권도 만들지 않는다.
            "tables": (
                "text_views[].tables[] projects P1 region.table.vlm_table_reading; "
                "VLM grid observation, not parser primary text, and creates no text ownership"
            ),
            "table_label_link": (
                "a table attaches to a labelled view only when the label is one of the region's "
                "template_labels.semantic verdicts; fixed-phrase hits alone never attach a table"
            ),
        },
        "document": {
            "doc_id": evidence.get("doc_id"),
            "source_file": evidence.get("source_file"),
            "file_type": evidence.get("file_type"),
            "classification": evidence.get("classification") or {},
            "template": evidence.get("template") or {},
        },
        "labelled_ad_copy": labelled_ad_copy,
        "unmapped_ad_copy": unmapped_ad_copy,
        "unverified_recovery_candidates": recovery_candidates,
        "summary": {
            "parser_primary_line_total": len(ordered_refs),
            "labelled_parser_primary_line_count": len(assigned_refs),
            "unmapped_ad_copy_parser_primary_line_count": len(ordered_refs) - len(assigned_refs),
            "labelled_group_count": len(labelled_ad_copy),
            "unmapped_ad_copy_view_count": len(unmapped_ad_copy),
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
                "card_no": None,
                "region_line_refs": [],
                "p2_text_selection": None,
                "table": None,
                "semantic_labels": [],
            }
            ordered.append(ref)
        for candidate in page.get("recovery_candidates") or []:
            recovery.append({"page_no": page_no, **candidate})
    return index, ordered, recovery


def _region_context(page_no: int | None, region: dict) -> dict:
    text_evidence = region.get("text_evidence") or {}
    template_labels = region.get("template_labels") or {}
    return {
        "page_no": page_no,
        "region_id": region.get("region_id"),
        "card_no": region.get("card_no"),
        "region_line_refs": [line.get("line_ref") for line in region.get("lines") or []],
        "p2_text_selection": text_evidence.get("p2_text_selection"),
        "table": region.get("table"),
        # 2층 VLM 의미 판정만 담는다. 1층 정형문구 히트(fixed_phrase_hits)는 넣지 않는다 —
        # `NH농협은행` 6글자가 표 안 문장에도 들어 있어서, 그걸로 표를 연결하면 대출금리
        # 표 전체가 회사명 라벨에 붙는다(_view_tables 주석 참조).
        "semantic_labels": [
            verdict.get("gubun")
            for verdict in template_labels.get("semantic") or []
            if verdict.get("gubun")
        ],
    }


def _labelled_ad_copy(target: dict, refs: list[str], index: dict[str, dict]) -> dict:
    """하나의 템플릿 라벨과 그 심의 대상 문구만 전달한다.

    ``target_id``/``field_key``는 P1에서 같은 값이므로 P2에는 하나의 안정 식별자
    ``label_id``만 둔다. requirement, writing_rules, evidence_status, fixed_phrase_checks는
    심의가 템플릿 팩이나 P1에서 조회할 정적/진단 정보라 여기서 복사하지 않는다.
    """
    return {
        "label_id": target.get("target_id"),
        "label": target.get("label"),
        "template_scope": target.get("template_scope"),
        "text_views": _review_text_views(refs, index, label=target.get("label")),
    }


def _unmapped_review_text_views(refs: list[str], index: dict[str, dict]) -> list[dict]:
    views = _review_text_views(refs, index, label=None)
    return views


def _review_text_views(refs: list[str], index: dict[str, dict], label: str | None) -> list[dict]:
    """같은 페이지·카드의 문구를 심의용 단일 view로 합친다.

    view 안에서는 Region 단위 segment를 유지한다. 해당 segment가 Region의 모든 파서 줄을
    포함해야만 P1 Judge가 고른 VLM 영역 문구를 사용한다. 일부 줄만 라벨에 속하면 원래
    파서 줄만 이어 붙인다. 이 규칙이 영역 전체 VLM 문구의 잘못된 라벨 귀속을 막는다.
    """
    by_view: dict[tuple, list[str]] = defaultdict(list)
    for ref in refs:
        entry = index[ref]
        by_view[(entry["page_no"], entry["card_no"])].append(ref)

    output: list[dict] = []
    for (page_no, card_no), view_refs in by_view.items():
        by_region: dict[str | None, list[str]] = defaultdict(list)
        for ref in view_refs:
            by_region[index[ref]["region_id"]].append(ref)

        segments = [_review_segment(region_refs, index) for region_refs in by_region.values()]
        sources = {segment["review_text_source"] for segment in segments}
        view = {
            "scope": {"page_no": page_no, "card_no": card_no},
            "review_text": "\n".join(segment["review_text"] for segment in segments).strip(),
            "text_source": next(iter(sources)) if len(sources) == 1 else "mixed",
            # P2에는 OCR/VLM 후보와 bbox를 반복 복사하지 않는다. 필요한 상세 근거는
            # 이 참조를 따라 P1에서 다시 읽는다.
            "line_refs": view_refs,
        }
        tables = _view_tables(view_refs, index, label)
        if tables:
            view["tables"] = tables
        output.append(view)
    return output


def _view_tables(refs: list[str], index: dict[str, dict], label: str | None) -> list[dict]:
    """이 view가 안고 있는 표 영역의 VLM 격자 관측을 투영한다.

    **연결 규칙.** 라벨 view 는 그 라벨이 표 영역의 2층 VLM 의미 판정(``semantic``)에
    들어 있을 때만 연결한다. 1층 정형문구 완전일치는 보지 않는다 — 실측(`16. 대출성상품`):
    표 영역 두 개 모두 1층 히트가 0개이고 의미 판정만 있다. 1층까지 보면 `NH농협은행`
    6글자가 표 안 문장에 들어 있는 경우 대출금리 표 전체가 회사명 라벨에 붙는다.
    미배정(``label is None``) view 는 의미 판정을 주장하지 않으므로 잘못된 귀속 위험이
    없어 보유한 표 영역을 그대로 싣는다 — 표가 라벨을 아예 못 받았거나(영역 전체 미배정)
    일부 줄만 미배정으로 남은 경우(실측 p1_r019: L00~L39 대출금리 / L40~L55 미배정)를
    같은 규칙으로 덮는다.

    ``rows`` 가 비어도 싣는다. StructureV3 가 표로 검출한 사실 자체가 심의에 필요한
    정보이고, 읽지 못했다는 것은 ``status`` 로 정직하게 전달한다.
    """
    output: list[dict] = []
    seen: set[str] = set()
    for ref in refs:
        entry = index[ref]
        region_id = entry.get("region_id")
        table = entry.get("table")
        if not (region_id and table) or region_id in seen:
            continue
        if label is not None and label not in (entry.get("semantic_labels") or []):
            continue
        seen.add(region_id)
        observation = table.get("vlm_table_reading") or {}
        output.append({
            "region_id": region_id,
            "rows": observation.get("rows") or [],
            "source": observation.get("source"),
            "status": observation.get("status"),
            "confidence": observation.get("confidence"),
            "structure_confidence": observation.get("structure_confidence"),
            # 표 영역 전체의 파서 줄이다. 이 view 가 그 줄을 다 갖는다는 뜻이 아니며
            # (p1_r019 는 두 view 에 걸친다) 텍스트 소유권도 만들지 않는다.
            "parser_primary_line_refs": table.get("parser_primary_line_refs") or [],
        })
    return output


def _review_segment(refs: list[str], index: dict[str, dict]) -> dict:
    first = index[refs[0]]
    parser_text = "\n".join(
        str(index[ref]["line"].get("parser_text", index[ref]["line"].get("text") or ""))
        for ref in refs
    ).strip()
    selection = first.get("p2_text_selection") or {}
    all_region_refs = {ref for ref in first.get("region_line_refs") or [] if ref}
    full_region_scope = bool(
        first.get("region_id") and all_region_refs and set(refs) == all_region_refs
    )
    selected_source = selection.get("selected_text_source")
    selected_text = str(selection.get("selected_text") or "").strip()
    if full_region_scope and selected_source == "vlm_judge_selected_region_test" and selected_text:
        text = selected_text
        source = "vlm_judge_selected_region_test"
    else:
        text = parser_text
        source = "parser_primary_text"
    return {
        "review_text": text,
        "review_text_source": source,
    }


def _unique_in_document_order(refs: list[str], ordered_refs: list[str]) -> list[str]:
    wanted = set(refs)
    return [ref for ref in ordered_refs if ref in wanted]


def _verify_partition(ordered_refs: list[str], labelled: list[dict], unmapped: list[dict]) -> None:
    assigned = {
        ref
        for item in labelled
        for view in item.get("text_views") or []
        for ref in view.get("line_refs") or []
    }
    remaining = {
        ref
        for view in unmapped
        for ref in view.get("line_refs") or []
    }
    all_refs = set(ordered_refs)
    if assigned & remaining:
        raise ValueError(f"template/unmapped 문구가 겹친다: {sorted(assigned & remaining)[:3]!r}")
    if assigned | remaining != all_refs:
        missing = all_refs - (assigned | remaining)
        extra = (assigned | remaining) - all_refs
        raise ValueError(f"ad-review-input 문구 분할 오류: 누락={sorted(missing)[:3]!r}, 초과={sorted(extra)[:3]!r}")
