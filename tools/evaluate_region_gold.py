# -*- coding: utf-8 -*-
"""사람이 원본에서 만든 영역 골드를 P1/P3 산출물과 비교한다.

골드의 ``gold_block_id``는 파서 ``region_id``와 독립적이다. 영역 로직을 바꿔도 골드가
같이 무효화되지 않도록, 공백을 제외한 원문과 의미 비교용 ``truncation.norm``을 이용해
페이지의 어느 파싱 영역이 각 gold block을 담는지 찾는다.

이 채점은 두 질문을 분리한다.

* P1 parser: OCR/PDF 정본과 물리 영역 경계가 맞는가.
* P3 review: 심의에 실제 전달되는 선택 문구와 라벨이 맞는가.

사용 예::

    uv run python tools/evaluate_region_gold.py --gold ...yaml --p1 ...json --p3 ...json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nh_parsing.truncation import norm  # noqa: E402


_WS = re.compile(r"\s+")


def fold_ws(text: object) -> str:
    """글자는 건드리지 않고 공백·줄바꿈만 접는다."""
    return _WS.sub("", str(text or ""))


def _p1_pages(data: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    return {
        int(page["page_no"]): [
            {
                "region_id": region.get("region_id"),
                "text": ((region.get("text_evidence") or {}).get("parser_primary_text") or {}).get("text") or "",
                "line_refs": [line.get("line_ref") for line in region.get("lines") or []],
                "lines": region.get("lines") or [],
            }
            for region in page.get("regions") or []
            if region.get("lines")
        ]
        for page in data.get("pages") or []
    }


def _p3_pages(data: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    return {
        int(page["page_no"]): [
            {
                "region_id": region.get("region_id"),
                "text": region.get("review_text") or "",
                "line_refs": region.get("line_refs") or [],
                "labels": region.get("labels") or [],
            }
            for region in page.get("regions") or []
        ]
        for page in data.get("pages") or []
    }


def _expected_labels(block: dict[str, Any]) -> set[str]:
    return {
        str(label)
        for label in (
            list(block.get("labels") or [])
            + [span.get("label") for span in block.get("label_spans") or []]
        )
        if label
    }


def _candidate_indices(text: str, regions: list[dict[str, Any]], *, exact: bool) -> list[int]:
    needle = fold_ws(text) if exact else norm(text)
    if not needle:
        return []
    return [
        index
        for index, region in enumerate(regions)
        if needle in (fold_ws(region["text"]) if exact else norm(region["text"]))
    ]


def _select_candidate(
    block: dict[str, Any], candidates: list[int], previous_index: int,
) -> int | None:
    """반복 문구는 occurrence를, 나머지는 골드 순서와 맞는 첫 영역을 고른다."""
    if not candidates:
        return None
    occurrence = block.get("occurrence")
    if occurrence is not None:
        pos = int(occurrence) - 1
        return candidates[pos] if 0 <= pos < len(candidates) else None
    return next((index for index in candidates if index >= previous_index), candidates[0])


def _match_blocks(
    blocks: list[dict[str, Any]], regions: list[dict[str, Any]], *, exact: bool,
) -> dict[str, int | None]:
    matched: dict[str, int | None] = {}
    previous = 0
    for block in blocks:
        candidates = _candidate_indices(str(block.get("text") or ""), regions, exact=exact)
        selected = _select_candidate(block, candidates, previous)
        matched[str(block["gold_block_id"])] = selected
        if selected is not None:
            previous = selected
    return matched


def _label_result(
    block: dict[str, Any], region: dict[str, Any] | None,
) -> dict[str, Any]:
    expected = sorted(_expected_labels(block))
    if region is None:
        return {"status": "unscored_no_region", "expected": expected, "actual": []}
    actual = sorted({str(label.get("label")) for label in region.get("labels") or [] if label.get("label")})
    return {
        "status": "pass" if actual == expected else "fail",
        "expected": expected,
        "actual": actual,
    }


def _score_page(
    gold_page: dict[str, Any], p1_regions: list[dict[str, Any]], p3_regions: list[dict[str, Any]],
) -> dict[str, Any]:
    blocks = list(gold_page.get("text_blocks") or [])
    p1_semantic = _match_blocks(blocks, p1_regions, exact=False)
    p1_exact = _match_blocks(blocks, p1_regions, exact=True)
    p3_semantic = _match_blocks(blocks, p3_regions, exact=False)
    p3_exact = _match_blocks(blocks, p3_regions, exact=True)

    block_results: list[dict[str, Any]] = []
    for block in blocks:
        block_id = str(block["gold_block_id"])
        p1_index = p1_semantic[block_id]
        p3_index = p3_semantic[block_id]
        p1_region = p1_regions[p1_index] if p1_index is not None else None
        p3_region = p3_regions[p3_index] if p3_index is not None else None
        block_results.append({
            "gold_block_id": block_id,
            "strict": bool(block.get("strict")),
            "p1": {
                "same_region_semantic": p1_index is not None,
                "same_region_exact_ws": p1_exact[block_id] is not None,
                "region_id": p1_region.get("region_id") if p1_region else None,
            },
            "p3": {
                "same_region_semantic": p3_index is not None,
                "same_region_exact_ws": p3_exact[block_id] is not None,
                "region_id": p3_region.get("region_id") if p3_region else None,
                "labels": _label_result(block, p3_region),
            },
        })

    block_by_id = {str(block["gold_block_id"]): block for block in blocks}
    must_not_merge: list[dict[str, Any]] = []
    for rule in gold_page.get("must_not_merge") or []:
        left, right = (str(value) for value in rule["blocks"])
        li, ri = p1_semantic.get(left), p1_semantic.get(right)
        if li is None or ri is None:
            status = "unscored_no_region"
        else:
            status = "pass" if li != ri else "fail"
        must_not_merge.append({"blocks": [left, right], "status": status})

    strict_tokens: list[dict[str, Any]] = []
    for rule in gold_page.get("strict_tokens") or []:
        block_id = str(rule["block"])
        token = str(rule["token"])
        expected_block = block_by_id[block_id]
        assert token in str(expected_block.get("text") or ""), f"{block_id} 본문에 strict token {token!r} 없음"
        p1_index = p1_semantic.get(block_id)
        p3_index = p3_semantic.get(block_id)
        strict_tokens.append({
            "block": block_id,
            "token": token,
            "p1_same_region": bool(
                p1_index is not None and token in p1_regions[p1_index]["text"]
            ),
            "p3_same_region": bool(
                p3_index is not None and token in p3_regions[p3_index]["text"]
            ),
            "p1_anywhere": any(token in region["text"] for region in p1_regions),
            "p3_anywhere": any(token in region["text"] for region in p3_regions),
        })

    gold_norms = [norm(block.get("text") or "") for block in blocks]
    parser_lines = [line for region in p1_regions for line in region.get("lines") or [] if norm(line.get("parser_text") or "")]
    covered_lines = [
        line for line in parser_lines
        if any(norm(line.get("parser_text") or "") in gold_text for gold_text in gold_norms)
    ]

    def count(path: tuple[str, ...], value: object = True) -> int:
        out = 0
        for item in block_results:
            current: Any = item
            for key in path:
                current = current[key]
            out += current == value
        return out

    return {
        "page_no": gold_page.get("page_no"),
        "summary": {
            "gold_block_count": len(blocks),
            "p1_same_region_semantic": count(("p1", "same_region_semantic")),
            "p1_same_region_exact_ws": count(("p1", "same_region_exact_ws")),
            "p3_same_region_semantic": count(("p3", "same_region_semantic")),
            "p3_same_region_exact_ws": count(("p3", "same_region_exact_ws")),
            "p3_label_exact": count(("p3", "labels", "status"), "pass"),
            "p3_label_scored": sum(item["p3"]["labels"]["status"] != "unscored_no_region" for item in block_results),
            "must_not_merge_pass": sum(item["status"] == "pass" for item in must_not_merge),
            "must_not_merge_scored": sum(item["status"] != "unscored_no_region" for item in must_not_merge),
            "strict_token_p1_same_region": sum(item["p1_same_region"] for item in strict_tokens),
            "strict_token_p3_same_region": sum(item["p3_same_region"] for item in strict_tokens),
            "strict_token_total": len(strict_tokens),
            "p1_parser_line_gold_coverage": len(covered_lines),
            "p1_parser_line_total": len(parser_lines),
        },
        "blocks": block_results,
        "must_not_merge": must_not_merge,
        "strict_tokens": strict_tokens,
        "uncovered_parser_lines": [
            {"line_ref": line.get("line_ref"), "parser_text": line.get("parser_text")}
            for line in parser_lines if line not in covered_lines
        ],
    }


def evaluate(gold: dict[str, Any], p1: dict[str, Any], p3: dict[str, Any]) -> dict[str, Any]:
    if gold.get("document", {}).get("source_file") != p1.get("source_file"):
        raise ValueError("gold와 P1 source_file이 다르다")
    expected_template = gold.get("document", {}).get("template_id")
    actual_template = (p1.get("template") or {}).get("template_id")
    if expected_template != actual_template:
        raise ValueError(f"template_id 불일치: gold={expected_template!r}, p1={actual_template!r}")

    p1_pages = _p1_pages(p1)
    p3_pages = _p3_pages(p3)
    pages = [
        _score_page(page, p1_pages.get(int(page["page_no"]), []), p3_pages.get(int(page["page_no"]), []))
        for page in gold.get("pages") or []
    ]
    return {
        "contract": "nh-ad-region-gold-evaluation-v1",
        "document": gold.get("document"),
        "pages": pages,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--p1", type=Path, required=True)
    parser.add_argument("--p3", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    gold = yaml.safe_load(args.gold.read_text(encoding="utf-8"))
    p1 = json.loads(args.p1.read_text(encoding="utf-8"))
    p3 = json.loads(args.p3.read_text(encoding="utf-8"))
    report = evaluate(gold, p1, p3)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(json.dumps({"pages": [page["summary"] for page in report["pages"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
