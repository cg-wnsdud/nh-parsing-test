# -*- coding: utf-8 -*-
"""P1 evidence를 영역 중심 심의 입력(P3 후보)으로 투영한다.

P2(``nh-ad-review-input-v6``)는 같은 라벨의 문구를 페이지 단위로 합친다. 이 모듈은
반대로 StructureV3 영역을 한 번만 싣고, 그 영역에 0개·1개·여러 개의 라벨 범위를
붙인다. 그러므로 영역 전체 문맥과 표 전체 문맥을 유지하면서도 라벨 간 중복을 허용한다.

이 계약에서 텍스트 소유권은 ``pages[].regions[].line_refs``와
``pages[].unassigned_text[].line_ref``에만 있다. ``labels[].spans[].line_refs``와 표의
``parser_primary_line_refs``는 P1로 돌아가기 위한 참조이므로 서로 겹쳐도 된다.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


CONTRACT_VERSION = "nh-ad-review-region-input-v1"


def build_ad_review_region_input(evidence: dict[str, Any]) -> dict[str, Any]:
    """P1 근거 JSON 하나를 영역 중심 P3 후보 계약으로 바꾼다."""
    _require_evidence(evidence)
    target_index = _target_index(evidence.get("review_targets") or [])

    pages_out: list[dict] = []
    input_refs: list[str] = []
    empty_regions: list[dict] = []
    recovery_candidates: list[dict] = []
    labelled_regions = 0

    for page in evidence.get("pages") or []:
        page_no = page.get("page_no")
        regions_out: list[dict] = []
        for region in page.get("regions") or []:
            lines = region.get("lines") or []
            if not lines:
                empty_regions.append({
                    "page_no": page_no,
                    "region_id": region.get("region_id"),
                    "bbox": region.get("bbox"),
                    "layout": region.get("layout"),
                    "card_no": region.get("card_no"),
                })
                continue
            refs = _region_line_refs(region)
            input_refs.extend(refs)
            region_out = _region_view(page_no, region, refs, target_index)
            if region_out["labels"]:
                labelled_regions += 1
            regions_out.append(region_out)

        unassigned_out: list[dict] = []
        for line in page.get("unassigned_lines") or []:
            ref = line.get("line_ref")
            if not ref:
                raise ValueError(f"line_ref가 없는 미배정 줄: page={page_no!r}")
            input_refs.append(ref)
            unassigned_out.append({
                "line_ref": ref,
                "review_text": str(line.get("parser_text", line.get("text") or "")),
                "text_source": "parser_primary_text",
            })

        for candidate in page.get("recovery_candidates") or []:
            recovery_candidates.append({"page_no": page_no, **candidate})

        pages_out.append({
            "page_no": page_no,
            "regions": regions_out,
            "unassigned_text": unassigned_out,
        })

    # 산출물을 다시 순회해 소유권을 독립 검산한다. 입력을 만들 때와 같은 목록을 그대로
    # 넘기면 빌더가 나중에 줄을 빼거나 중복해도 검사가 항상 통과하는 자기검증이 된다.
    output_refs = [
        ref
        for page in pages_out
        for region in page["regions"]
        for ref in region["line_refs"]
    ] + [
        line["line_ref"]
        for page in pages_out
        for line in page["unassigned_text"]
    ]
    _verify_text_partition(input_refs, output_refs)
    label_index = _label_index(pages_out)
    region_count = sum(len(page["regions"]) for page in pages_out)
    line_total = len(input_refs)
    unassigned_count = sum(len(page["unassigned_text"]) for page in pages_out)
    return {
        "contract": {
            "version": CONTRACT_VERSION,
            "source_evidence_contract": (
                evidence.get("reading_evidence_contract") or {}
            ).get("version"),
            "review_unit": "region",
            "region_cardinality": "one region carries zero, one, or multiple label spans",
            "text_partition": (
                "pages[].regions[].line_refs + pages[].unassigned_text[].line_ref "
                "partition P1 parser primary lines"
            ),
            "label_overlap": "allowed; label span references do not create text ownership",
            "p1_reference": (
                "bbox, OCR/VLM candidates, Judge evidence, visibility, and detailed provenance "
                "remain in P1 and are resolved by region_id/line_refs"
            ),
            "tables": (
                "region.table is copied from P1 as a sibling observation; rows do not replace "
                "review_text and create no text ownership"
            ),
        },
        "document": {
            "doc_id": evidence.get("doc_id"),
            "source_file": evidence.get("source_file"),
            "file_type": evidence.get("file_type"),
            "classification": evidence.get("classification") or {},
            "template": evidence.get("template") or {},
        },
        "pages": pages_out,
        # 빠른 라벨별 탐색용 역색인이다. 본문을 다시 복사하지 않고 영역·범위만 가리킨다.
        "label_index": label_index,
        # 페이지 sweep은 P1 정본이 아니므로 영역 본문과 섞지 않는다.
        "unverified_recovery_candidates": recovery_candidates,
        "diagnostics": {"empty_regions": empty_regions},
        "summary": {
            "region_count": region_count,
            "empty_region_count": len(empty_regions),
            "parser_primary_line_total": line_total,
            "unassigned_parser_primary_line_count": unassigned_count,
            "labelled_region_count": labelled_regions,
            "unlabelled_region_count": region_count - labelled_regions,
            "label_count": len(label_index),
            "recovery_candidate_count": len(recovery_candidates),
        },
    }


def _require_evidence(evidence: dict[str, Any]) -> None:
    if not isinstance(evidence.get("pages"), list):
        raise ValueError("evidence JSON에 pages[]가 없다")
    version = str((evidence.get("reading_evidence_contract") or {}).get("version") or "")
    if version and not version.startswith("nh-ad-review-evidence-v"):
        raise ValueError(f"지원하지 않는 evidence 계약: {version!r}")


def _target_index(targets: list[dict]) -> dict[str, Any]:
    """region_id+label을 실제 target_id로 연결하고, 유일한 전역 label도 보조로 둔다."""
    by_region: dict[tuple[str, str], str] = {}
    global_ids: dict[str, set[str]] = defaultdict(set)
    for target in targets:
        label = str(target.get("label") or "")
        target_id = str(target.get("target_id") or "")
        if not (label and target_id):
            continue
        global_ids[label].add(target_id)
        for region_id in (target.get("evidence") or {}).get("region_ids") or []:
            by_region[(str(region_id), label)] = target_id
    return {
        "by_region": by_region,
        "unique_global": {
            label: next(iter(ids)) for label, ids in global_ids.items() if len(ids) == 1
        },
    }


def _region_line_refs(region: dict) -> list[str]:
    refs: list[str] = []
    for line in region.get("lines") or []:
        ref = line.get("line_ref")
        if not ref:
            raise ValueError(f"line_ref가 없는 영역 줄: {region.get('region_id')!r}")
        refs.append(ref)
    if len(refs) != len(set(refs)):
        raise ValueError(f"영역 안 중복 line_ref: {region.get('region_id')!r}")
    return refs


def _region_view(
    page_no: int | None,
    region: dict,
    refs: list[str],
    target_index: dict[str, Any],
) -> dict:
    parser_text = "\n".join(
        str(line.get("parser_text", line.get("text") or ""))
        for line in region.get("lines") or []
    ).strip()
    selection = ((region.get("text_evidence") or {}).get("p2_text_selection") or {})
    selected_source = selection.get("selected_text_source")
    selected_text = str(selection.get("selected_text") or "").strip()
    if selected_source == "vlm_judge_selected_region_test" and selected_text:
        review_text = selected_text
        text_source = "vlm_judge_selected_region_test"
    else:
        review_text = parser_text
        text_source = "parser_primary_text"

    region_id = str(region.get("region_id") or "")
    labels = _region_labels(region, refs, target_index)
    output = {
        "region_id": region_id,
        "scope": {"page_no": page_no, "card_no": region.get("card_no")},
        "review_text": review_text,
        "text_source": text_source,
        "line_refs": refs,
        "labels": labels,
    }
    # 표는 평면 본문을 대체하지 않는 형제 표현이다. P1 형태를 그대로 보존하면 아직
    # 표본이 적은 단계에서 행/열 정규화 계약을 성급히 고정하지 않아도 된다.
    if region.get("table") is not None:
        output["table"] = region["table"]
    return output


def _region_labels(region: dict, refs: list[str], target_index: dict[str, Any]) -> list[dict]:
    """VLM 의미 범위와 정형문구 히트를 줄별로 합쳐 복수 라벨 span을 만든다."""
    lines = region.get("lines") or []
    by_line: list[dict[str, dict[str, Any]]] = [defaultdict(_line_label_state) for _ in lines]

    for verdict in (region.get("template_labels") or {}).get("semantic") or []:
        label = str(verdict.get("gubun") or "")
        if not label or not lines:
            continue
        start = int(verdict.get("line_from", 0))
        end = int(verdict.get("line_to", start))
        if end < start:
            start, end = end, start
        if end < 0 or start >= len(lines):
            continue
        start, end = max(0, start), min(len(lines) - 1, end)
        for index in range(start, end + 1):
            state = by_line[index][label]
            state["sources"].add("semantic_vlm")
            confidence = verdict.get("confidence")
            if confidence is not None:
                state["confidences"].add(float(confidence))

    for index, line in enumerate(lines):
        for hit in line.get("labels") or []:
            label = str(hit.get("gubun") or "")
            if not label:
                continue
            state = by_line[index][label]
            state["sources"].add("fixed_phrase")
            phrase_id = hit.get("phrase_id")
            if phrase_id:
                state["phrase_ids"].add(str(phrase_id))

    labels_out: list[dict] = []
    all_labels = sorted({label for states in by_line for label in states})
    region_id = str(region.get("region_id") or "")
    for label in all_labels:
        signature_indices: list[tuple[int, tuple]] = []
        for index, states in enumerate(by_line):
            if label not in states:
                continue
            state = states[label]
            signature = (
                tuple(sorted(state["sources"])),
                tuple(sorted(state["confidences"])),
                tuple(sorted(state["phrase_ids"])),
            )
            signature_indices.append((index, signature))
        spans = _spans(signature_indices, refs)
        target_id = (
            target_index["by_region"].get((region_id, label))
            or target_index["unique_global"].get(label)
        )
        labels_out.append({"label_id": target_id, "label": label, "spans": spans})
    return labels_out


def _line_label_state() -> dict[str, set]:
    return {"sources": set(), "confidences": set(), "phrase_ids": set()}


def _spans(signature_indices: list[tuple[int, tuple]], refs: list[str]) -> list[dict]:
    if not signature_indices:
        return []
    groups: list[tuple[int, int, tuple]] = []
    start = previous = signature_indices[0][0]
    signature = signature_indices[0][1]
    for index, current_signature in signature_indices[1:]:
        if index == previous + 1 and current_signature == signature:
            previous = index
            continue
        groups.append((start, previous, signature))
        start = previous = index
        signature = current_signature
    groups.append((start, previous, signature))

    output: list[dict] = []
    for start, end, (sources, confidences, phrase_ids) in groups:
        span = {
            "line_from": start,
            "line_to": end,
            "line_refs": refs[start:end + 1],
            "sources": list(sources),
        }
        if confidences:
            span["confidence"] = max(confidences)
        if phrase_ids:
            span["phrase_ids"] = list(phrase_ids)
        output.append(span)
    return output


def _label_index(pages: list[dict]) -> list[dict]:
    by_label: dict[tuple[str | None, str], list[dict]] = defaultdict(list)
    for page in pages:
        for region in page["regions"]:
            for label in region["labels"]:
                by_label[(label.get("label_id"), label["label"])].append({
                    "region_id": region["region_id"],
                    "scope": region["scope"],
                    "spans": label["spans"],
                })
    return [
        {"label_id": label_id, "label": label, "references": references}
        for (label_id, label), references in by_label.items()
    ]


def _verify_text_partition(input_refs: list[str], output_refs: list[str]) -> None:
    input_set, output_set = set(input_refs), set(output_refs)
    if len(input_refs) != len(input_set):
        raise ValueError("P1 parser primary line_ref가 중복된다")
    if len(output_refs) != len(output_set):
        raise ValueError("P3 영역/미배정 텍스트 소유권이 중복된다")
    if input_set != output_set:
        raise ValueError(
            "region-review-input 문구 분할 오류: "
            f"누락={sorted(input_set - output_set)[:3]!r}, "
            f"초과={sorted(output_set - input_set)[:3]!r}"
        )
