# -*- coding: utf-8 -*-
"""기존 evidence JSON에서 다음 단계용 ad-review-input JSON을 만든다.

원본 OCR/PaddleX/VLM을 다시 실행하지 않는다. 따라서 이미 생성한 ``out_0831`` 같은
결과를 입력으로 새 인계 계약만 검증할 수 있다.

    python tools/build_ad_review_input.py out_0831_region_reader_judge_v2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nh_parsing.ad_review_input import build_ad_review_input  # noqa: E402


def _evidence_files(src: Path) -> list[Path]:
    if src.is_file():
        return [src]
    # run_ad_label/run_parsing_batch 산출물의 evidence 원본만 찾는다. parse/summary 등은
    # JSON 모양이 달라 여기서 손대지 않는다.
    return sorted(src.rglob("json/*.json"))


def _is_evidence(path: Path) -> bool:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(raw.get("pages"), list) and "reading_evidence_contract" in raw


def _destination(src: Path, input_root: Path, out: Path | None) -> Path:
    if out is None:
        return src.parent.parent / "review_input" / src.name
    if input_root.is_file():
        return out / src.name
    # <root>/<sample>/json/file.json → <root>/<sample>/review_input/file.json
    rel = src.parent.parent.relative_to(input_root)
    return out / rel / "review_input" / src.name


def main() -> None:
    ap = argparse.ArgumentParser(description="evidence JSON → ad-review-input JSON (모델 호출 없음)")
    ap.add_argument("input", type=Path, help="evidence JSON 하나 또는 산출물 루트 폴더")
    ap.add_argument("--out", type=Path, default=None, help="출력 루트 (기본: 각 json/ 옆 review_input/)")
    args = ap.parse_args()

    files = [path for path in _evidence_files(args.input) if _is_evidence(path)]
    if not files:
        raise SystemExit(f"evidence JSON을 찾지 못했습니다: {args.input}")

    for src in files:
        evidence = json.loads(src.read_text(encoding="utf-8"))
        review_input = build_ad_review_input(evidence)
        dst = _destination(src, args.input, args.out)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(review_input, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = review_input["summary"]
        print(
            f"✓ {src.name} → {dst}\n"
            f"  parser-primary {summary['parser_primary_line_total']} / "
            f"template {summary['template_assigned_parser_primary_line_count']} / "
            f"unmapped {summary['unmapped_ad_copy_parser_primary_line_count']} / "
            f"fields {summary['template_field_count']}"
        )


if __name__ == "__main__":
    main()
