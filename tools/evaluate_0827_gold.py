# -*- coding: utf-8 -*-
"""Evaluate a namespaced 0827 batch against the repository gold YAML files.

The original ``tools/evaluate.py`` expects unprefixed files in ``out/json``.
The resumable batch intentionally prefixes document ids and separates input
namespaces, so this adapter matches documents by the recorded ``source_file``.
It evaluates the intermediate parse JSON because that is the parser output the
gold evaluator was designed for; template/export fields are downstream of it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from evaluate import CAND_ONLY, VIEW_DROP, eval_file  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input_dir", type=Path, default=ROOT / "0827-parsing")
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    args = parser.parse_args()

    input_dir = args.input_dir if args.input_dir.is_absolute() else ROOT / args.input_dir
    out_json = args.out_json or input_dir / "gold-evaluation.json"
    out_md = args.out_md or input_dir / "gold-evaluation.md"

    parsed_by_source: dict[str, tuple[Path, dict]] = {}
    duplicates: dict[str, list[str]] = defaultdict(list)
    for path in sorted(input_dir.glob("*/parse/*.json")):
        parsed = json.loads(path.read_text(encoding="utf-8"))
        source = parsed.get("source_file")
        if not source:
            continue
        if source in parsed_by_source:
            duplicates[source].append(str(path.relative_to(ROOT)))
        else:
            parsed_by_source[source] = (path, parsed)

    aggregate: dict[str, dict[str, int]] = defaultdict(
        lambda: {"passed": 0, "total": 0, "candidate_only": 0, "view_drop": 0}
    )
    documents: list[dict] = []
    unmatched_gold: list[str] = []

    for gold_path in sorted((ROOT / "gold").glob("*.yaml")):
        gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
        source = gold.get("file")
        hit = parsed_by_source.get(source)
        if hit is None:
            unmatched_gold.append(gold_path.name)
            continue
        parsed_path, parsed = hit
        rows = eval_file(gold, parsed, None)
        metrics: dict[str, dict[str, int]] = defaultdict(
            lambda: {"passed": 0, "total": 0, "candidate_only": 0, "view_drop": 0}
        )
        failures: list[dict] = []
        special: list[dict] = []
        for metric, target, ok, note in rows:
            item = metrics[metric]
            item["total"] += 1
            item["passed"] += int(ok)
            item["candidate_only"] += int(ok and note == CAND_ONLY)
            item["view_drop"] += int(ok and note == VIEW_DROP)
            agg = aggregate[metric]
            for key in item:
                agg[key] += int(
                    (key == "total")
                    or (key == "passed" and ok)
                    or (key == "candidate_only" and ok and note == CAND_ONLY)
                    or (key == "view_drop" and ok and note == VIEW_DROP)
                )
            record = {"metric": metric, "target": target, "note": note}
            if not ok:
                failures.append(record)
            elif note in (CAND_ONLY, VIEW_DROP):
                special.append(record)
        documents.append(
            {
                "source_file": source,
                "gold_file": gold_path.name,
                "parse_json": str(parsed_path.relative_to(ROOT)),
                "metrics": dict(metrics),
                "failures": failures,
                "special": special,
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": str(input_dir.relative_to(ROOT) if input_dir.is_relative_to(ROOT) else input_dir),
        "evaluated_documents": len(documents),
        "aggregate": dict(aggregate),
        "unmatched_gold": unmatched_gold,
        "duplicate_source_names": dict(duplicates),
        "documents": documents,
        "limitations": [
            "골드가 있는 입력만 평가하므로 94건 전체 정확도가 아니다.",
            "문장 지표는 정규화 부분일치이며 CER/WER 또는 숫자 필드 정확도가 아니다.",
            "VLM 후보로만 찾은 문장도 회수 성공으로 세되 candidate_only로 별도 집계한다.",
            "이번 배치에는 별도 llm_view가 없어 STAGE_3 입력 누락은 평가하지 않는다.",
        ],
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 0827 배치 소규모 골드 평가",
        "",
        "> 94건 전체 정확도가 아니라, 저장소에 정답 YAML이 있는 입력만 본 제한적 회귀 지표입니다.",
        "",
        f"- 평가 문서: {len(documents)}건",
        f"- 입력에 없는 골드: {len(unmatched_gold)}건",
        "",
        "## 종합",
        "",
        "| 지표 | 통과 | 후보로만 회수 |",
        "| --- | ---: | ---: |",
    ]
    for metric, item in aggregate.items():
        total = item["total"]
        pct = 100 * item["passed"] / total if total else 0
        lines.append(
            f"| {metric} | {item['passed']}/{total} ({pct:.1f}%) | {item['candidate_only']} |"
        )
    lines.extend(["", "## 문서별 실패", ""])
    for doc in documents:
        lines.append(f"### `{doc['source_file']}`")
        lines.append("")
        if not doc["failures"]:
            lines.append("- 실패 없음")
        else:
            for failure in doc["failures"]:
                lines.append(
                    f"- {failure['metric']}: {failure['target']} — {failure['note']}"
                )
        for special in doc["special"]:
            lines.append(
                f"- 후보층 회수: {special['metric']} {special['target']} — {special['note']}"
            )
        lines.append("")
    lines.extend(["## 해석 제한", ""])
    lines.extend(f"- {item}" for item in payload["limitations"])
    if unmatched_gold:
        lines.extend(["", "## 입력에 없던 골드", ""])
        lines.extend(f"- `{name}`" for name in unmatched_gold)
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(payload["aggregate"], ensure_ascii=False, indent=2))
    print(f"evaluated_documents={len(documents)}")
    print(f"report={out_md}")


if __name__ == "__main__":
    main()
