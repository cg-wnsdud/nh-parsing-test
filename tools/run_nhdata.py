# -*- coding: utf-8 -*-
"""nh-data 전체 처리 러너 — 프로토타입 검증 기준(설계서 10절):
6개 파일 모두 AdPageIR 생성 + 유의사항 텍스트가 좌표와 함께 나오는가.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nh_parsing.gemma_client import reset_stats, stats_table
from nh_parsing.llm_view import build_doc_view
from nh_parsing.pipeline import process_file

DEFAULT_INPUT = Path(
    r"c:\Users\cccjj\cginside\repo-analysis\paddle-gemma-orchestrator\nh-data"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent.parent / "out")
    parser.add_argument("--only", type=str, default=None, help="파일명 부분 일치 필터")
    parser.add_argument("--exclude", type=str, default=None, help="파일명 부분 일치 제외 (예: 은행연합회 — rag 트랙)")
    parser.add_argument(
        "--region-vlm", action="store_true",
        help="영역별 VLM 통독(§6 ④+) 활성화 — 전 페이지 적용, 배치 호출·상한 없음",
    )
    args = parser.parse_args()

    out_json = args.out / "json"
    out_prev = args.out / "previews"
    out_llm = args.out / "llm_view"
    out_json.mkdir(parents=True, exist_ok=True)
    out_llm.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p for p in args.input.iterdir()
        if p.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".hwp", ".hwpx"}
    )
    if args.only:
        files = [p for p in files if args.only in p.name]
    if args.exclude:
        files = [p for p in files if args.exclude not in p.name]

    for path in files:
        print(f"\n{'=' * 72}\n▶ {path.name}")
        reset_stats()   # 문서별 단계 비용을 따로 본다 (안 하면 누적돼 차분을 손으로 빼야 한다)
        start = time.time()
        try:
            doc = process_file(path, preview_dir=out_prev, region_vlm=args.region_vlm)
        except Exception as exc:
            print(f"  ✗ 처리 실패: {exc}")
            continue
        elapsed = time.time() - start

        json_path = out_json / f"{doc.doc_id}.json"
        json_path.write_text(
            doc.model_dump_json(indent=2), encoding="utf-8"
        )
        # LLM 전달용 lean 투영(§1A) — bbox/신뢰도/출처 제외, region_id 유지
        (out_llm / f"{doc.doc_id}.json").write_text(
            json.dumps(build_doc_view(doc), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        total_lines = sum(
            len(r.lines) for pg in doc.pages for r in pg.regions
        ) + sum(len(pg.unassigned_lines) for pg in doc.pages)
        print(
            f"  분류: {doc.product_group}/{doc.ad_type} "
            f"(source={doc.category_source}, conf={doc.classification_confidence})"
        )
        for pg in doc.pages:
            print(
                f"  p{pg.page_no}: route={pg.parse_route} status={pg.parse_status} "
                f"canvas={pg.canvas_w}x{pg.canvas_h} regions={len(pg.regions)} "
                + (f" triage={pg.triage['verdict']}({';'.join(pg.triage['reasons'])})" if pg.triage else "")
            )
            for s in pg.sections:
                grp = f" 묶음{s.group_no}" if s.group_no else ""
                print(
                    f"      §{s.section_id} [{s.section_type}#{s.section_no}]{grp} "
                    f"regions={len(s.region_ids)} bbox={s.bbox} conf={s.confidence}"
                )
            for region in pg.regions:
                if region.role == "유의사항" and region.lines:
                    sample = region.lines[0].text[:60]
                    print(
                        f"      [유의사항/{region.role_source}] bbox={region.bbox} "
                        f"lines={len(region.lines)} conf={region.role_confidence} 첫줄={sample!r}"
                    )
        for pg in doc.pages:
            for note in pg.notes:
                print(f"  p{pg.page_no} note: {note}")
        print("  ── VLM 단계별 비용 ──")
        for line in stats_table().splitlines():
            print("  " + line)
        for note in doc.notes:
            print(f"  note: {note}")
        print(f"  라인 {total_lines}개, {elapsed:.1f}s → {json_path.name}")

    print("\n완료.")


if __name__ == "__main__":
    main()
