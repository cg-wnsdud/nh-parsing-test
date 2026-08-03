# -*- coding: utf-8 -*-
"""이미 뽑아 둔 out/extracted/*.json 에 부재 3분류를 다시 입힌다 (VLM 호출 0회).

부재 판정(해당없음/미표시/확인필요)은 스키마의 적용조건을 코드로 평가할 뿐이라
추출을 다시 돌릴 이유가 없다. 스키마의 적용조건을 고쳤을 때 이 도구로 재적용하면
같은 추출 결과 위에서 판정만 바뀌므로, 바뀐 것이 판정인지 추출 변동인지 헷갈리지 않는다
(추출은 실행마다 흔들린다 — VLM 서버측 비결정성).

사용: uv run python tools/reclassify_absences.py [--only 파일명일부] [--dry-run]
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nh_parsing.applicability import (  # noqa: E402
    check_schema_metadata, classify_absences, derived_rules, input_gap,
)
from nh_parsing.extract import compute_coverage, prune_empty_events  # noqa: E402
from nh_parsing.schema_pack import load_pack  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과만 출력")
    ap.add_argument("--extracted-dir", default="out/extracted")
    ap.add_argument("--view-dir", default="out/llm_view")
    ap.add_argument("--parse-dir", default="out/json")
    args = ap.parse_args()

    ext_dir, view_dir, parse_dir = (ROOT / args.extracted_dir, ROOT / args.view_dir,
                                    ROOT / args.parse_dir)

    problems = check_schema_metadata(load_pack("예금성", "이벤트페이지"))
    print("[스키마] 부재 판정 메타:", "이상 없음" if not problems else problems)
    print("[스키마] 조문에 없는 해석(derived) — 사람 승인 대상:")
    for d in derived_rules(load_pack("예금성", "이벤트페이지")):
        print(f"   - {d['field_key']} :: {d['rule']} {d['values'] or ''} — {d['why']}")
    print()

    summary = []
    for path in sorted(ext_dir.glob("*.json")):
        if path.name == "_summary.json" or (args.only and args.only not in path.name):
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        pack = load_pack(result["product_group"], result.get("ad_type"))

        prune_empty_events(result)   # 유령 이벤트가 허위 미표시를 만들지 않게 먼저 걷어낸다
        result["review_gaps"] = classify_absences(result, pack)
        view_path, parse_path = view_dir / path.name, parse_dir / path.name
        result["input_gap"] = (
            input_gap(json.loads(view_path.read_text(encoding="utf-8")),
                      json.loads(parse_path.read_text(encoding="utf-8")))
            if view_path.exists() and parse_path.exists() else []
        )
        view = json.loads(view_path.read_text(encoding="utf-8")) if view_path.exists() else {"pages": []}
        result["coverage"] = compute_coverage(view, result)

        c = result["coverage"]
        print(f"▶ {path.stem}  (유형 {result['review_gaps']['product_subtype']})")
        print(f"   not_found {c['fields_not_found']} → 미표시 {c['absence_missing']}"
              f"+이벤트 {c['absence_missing_in_events']} / 해당없음 {c['absence_not_applicable']}"
              f" / 판정제외 {c['absence_out_of_scope']} / 확인필요 {c['absence_needs_check']}")
        for m in result["review_gaps"]["미표시"]:
            print(f"      [미표시] {m['field_key']} ({m['obligation']})")
        if result["input_gap"]:
            print(f"   !! 입력 유실 {len(result['input_gap'])}개 영역 (위 미표시에 섞였을 수 있음)")

        if not args.dry_run:
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        summary.append({"file": path.name, **c, "elapsed_s": result.get("elapsed_s"),
                        "errors": len(result.get("errors", []))})

    if not args.dry_run and summary:
        (ext_dir / "_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"\n{'(dry-run) ' if args.dry_run else ''}완료 — {len(summary)}건")


if __name__ == "__main__":
    main()
