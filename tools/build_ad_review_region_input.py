# -*- coding: utf-8 -*-
"""기존 P1 evidence JSON에서 영역 중심 P3 후보 JSON을 만든다.

OCR·VLM을 다시 호출하지 않는다. 기본 출력은 각 ``json/`` 폴더 옆의
``review_region_input/``이다.

    uv run python tools/build_ad_review_region_input.py out_0903_step12_rerun
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nh_parsing.ad_review_region_input import build_ad_review_region_input  # noqa: E402


def _evidence_files(src: Path) -> list[Path]:
    if src.is_file():
        return [src]
    return sorted(src.rglob("json/*.json"))


def _is_evidence(path: Path) -> bool:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(raw.get("pages"), list) and "reading_evidence_contract" in raw


def _destination(src: Path, input_root: Path, out: Path | None) -> Path:
    if out is None:
        return src.parent.parent / "review_region_input" / src.name
    if input_root.is_file():
        return out / src.name
    rel = src.parent.parent.relative_to(input_root)
    return out / rel / "review_region_input" / src.name


def main() -> None:
    ap = argparse.ArgumentParser(description="P1 evidence JSON → 영역 중심 P3 후보 JSON")
    ap.add_argument("input", type=Path, help="evidence JSON 하나 또는 산출물 루트 폴더")
    ap.add_argument("--out", type=Path, default=None, help="출력 루트")
    args = ap.parse_args()

    files = [path for path in _evidence_files(args.input) if _is_evidence(path)]
    if not files:
        raise SystemExit(f"evidence JSON을 찾지 못했습니다: {args.input}")

    for src in files:
        evidence = json.loads(src.read_text(encoding="utf-8"))
        output = build_ad_review_region_input(evidence)
        dst = _destination(src, args.input, args.out)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = output["summary"]
        print(
            f"✓ {src.name} → {dst}\n"
            f"  regions {summary['region_count']} / labelled {summary['labelled_region_count']} / "
            f"unlabelled {summary['unlabelled_region_count']} / "
            f"lines {summary['parser_primary_line_total']} / "
            f"unassigned {summary['unassigned_parser_primary_line_count']} / "
            f"tables {sum('table' in region for page in output['pages'] for region in page['regions'])}"
        )


if __name__ == "__main__":
    main()
