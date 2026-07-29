# -*- coding: utf-8 -*-
"""STAGE_3 실행 — out/llm_view/*.json 을 스키마로 추출해 out/extracted/ 에 저장.

사용: uv run python tools/run_extract.py [--only 파일명일부]
"""
import argparse
import json
import sys
import time
from pathlib import Path

# 출력을 파일/파이프로 넘기면 stdout 이 cp949 로 잡혀 '—' 같은 문자에서 죽는다
# (run_nhdata.py 와 동일 처리). 마지막 파일 스킵 메시지에서 전체 실행이 중단된 실측.
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nh_parsing.applicability import check_schema_metadata  # noqa: E402
from nh_parsing.extract import extract_document  # noqa: E402
from nh_parsing.schema_pack import check_coverage, load_pack  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="파일명에 이 문자열이 포함된 문서만")
    ap.add_argument("--view-dir", default="out/llm_view")
    ap.add_argument("--out-dir", default="out/extracted")
    ap.add_argument("--parse-dir", default="out/json",
                    help="파싱 원본 — STAGE_3 입력에서 빠진 영역(입력 유실) 대조용")
    args = ap.parse_args()

    view_dir = ROOT / args.view_dir
    out_dir = ROOT / args.out_dir
    parse_dir = ROOT / args.parse_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    cov = check_coverage("예금성")
    print(f"[스키마] 근거대장 텍스트항목 {cov['catalog_text_items']}개 중 {cov['covered']}개 반영, "
          f"미반영 {len(cov['missing'])}건 / 스키마 필드 {cov['schema_field_count']}개")
    print(f"[스키마] 호출그룹: {', '.join(cov['call_groups'])}")
    meta_problems = check_schema_metadata(load_pack("예금성", "이벤트페이지"))
    if meta_problems:
        # 의무등급·적용조건이 빠진 필드는 '필수·전 광고'로 평가돼 없던 지적사항을 만든다
        print(f"[스키마] !! 부재 판정 메타 누락 {len(meta_problems)}건: {meta_problems[:5]}")
    print()

    summary = []
    for path in sorted(view_dir.glob("*.json")):
        if args.only and args.only not in path.name:
            continue
        view = json.loads(path.read_text(encoding="utf-8"))
        pg = view.get("product_group")
        if pg != "예금성":
            print(f"— 건너뜀: {path.name} (product_group={pg}, PoC 대상 아님)")
            continue

        print(f"▶ {path.name}  ({view.get('ad_type')})")
        parse_path = parse_dir / path.name
        parse_doc = (
            json.loads(parse_path.read_text(encoding="utf-8"))
            if parse_path.exists() else None
        )
        t0 = time.time()
        result = extract_document(view, parse_doc=parse_doc)
        elapsed = round(time.time() - t0, 1)
        result["elapsed_s"] = elapsed

        (out_dir / path.name).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        c = result.get("coverage", {})
        gaps = result.get("review_gaps", {})
        print(f"   {elapsed}s | 필드 found {c.get('fields_found')} / "
              f"not_found {c.get('fields_not_found')} / uncertain {c.get('fields_uncertain')}")
        # not_found 를 셋으로 갈라 보여준다 — 숫자 하나로는 '광고 결함'과 '해당 없음'이 안 갈린다
        print(f"   부재 내역: 미표시(지적) {c.get('absence_missing')}"
              f"+이벤트 {c.get('absence_missing_in_events')} / "
              f"해당없음 {c.get('absence_not_applicable')} / "
              f"판정제외 {c.get('absence_out_of_scope')} / "
              f"확인필요 {c.get('absence_needs_check')}")
        for m in gaps.get("미표시", []):
            print(f"      [지적] {m['field_key']} ({m['obligation']})")
        if result.get("input_gap"):
            print(f"   !! STAGE_3 입력 유실 {len(result['input_gap'])}개 영역 — "
                  f"위 '미표시'에 입력 누락이 섞였을 수 있음 "
                  f"(예: {result['input_gap'][0]['text'][:40]!r})")
        print(f"   근거 커버리지 {c.get('region_coverage')} "
              f"({c.get('regions_cited')}/{c.get('regions_total')} 영역) | "
              f"미배정 {c.get('unmapped_total')}건 (스키마공백 {c.get('unmapped_schema_gap')})")
        if result.get("events"):
            print(f"   이벤트 {len(result['events'])}건")
        for e in result.get("errors", []):
            print(f"   !! {e['group']} 실패: {e['error'][:120]}")
        summary.append({"file": path.name, **c, "elapsed_s": elapsed,
                        "errors": len(result.get("errors", []))})

    (out_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n완료 — {len(summary)}건, 결과: {out_dir}")


if __name__ == "__main__":
    main()
