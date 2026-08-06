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

from nh_parsing.gemma_client import STATS, reset_stats, stats_table
from nh_parsing.llm_view import build_doc_view
from nh_parsing.pipeline import process_file

# 이 저장소 안의 샘플이 기본값이다. 예전 기본값은 저장소 밖 `../nh-data` 를 가리켰는데
# 그 경로는 존재하지 않아서 --input 없이 돌리면 죽었다 (2026-08-06 확인, walkthrough §10 #9).
DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "nh-data" / "sample-data"


def _print_timing(rows: list[dict], wall_s: float) -> None:
    """파일별 소요시간 표. 합계만으로는 "어디에 시간이 드나"에 답할 수 없다.

    VLM 대기와 그 외(렌더·PaddleX OCR·우리 코드)를 갈라 찍는다 — 둘은 늘리는 방법이
    완전히 다르다(전자는 호출 수, 후자는 타일 수·해상도). vlm_cached 가 0 이 아니면
    캐시가 켜진 실행이므로 시간 수치를 대외 자료에 쓰면 안 된다.
    """
    print("\n" + "=" * 100)
    print("파일별 소요시간")
    print("=" * 100)
    hdr = f'{"파일":<34}{"형식":>6}{"쪽":>3}{"경로":>10}{"영역":>5}{"라인":>6}' \
          f'{"총초":>8}{"VLM초":>8}{"그외초":>8}{"VLM호출":>8}{"캐시":>5}{"실패":>5}'
    print(hdr)
    print("-" * 106)
    for r in rows:
        print(f'{r["file"][:33]:<34}{r["type"]:>6}{r["pages"]:>3}{r["routes"]:>10}'
              f'{r["regions"]:>5}{r["lines"]:>6}{r["parse_s"]:>8.1f}{r["vlm_s"]:>8.1f}'
              f'{r["non_vlm_s"]:>8.1f}{r["vlm_calls"]:>8}{r["vlm_cached"]:>5}'
              f'{r["stage_fails"]:>5}')
    print("-" * 106)
    tot_p = sum(r["parse_s"] for r in rows)
    tot_v = sum(r["vlm_s"] for r in rows)
    tot_c = sum(r["vlm_calls"] for r in rows)
    tot_cached = sum(r["vlm_cached"] for r in rows)
    print(f'{"합계 " + str(len(rows)) + "건":<34}{"":>6}{sum(r["pages"] for r in rows):>3}'
          f'{"":>10}{sum(r["regions"] for r in rows):>5}{sum(r["lines"] for r in rows):>6}'
          f'{tot_p:>8.1f}{tot_v:>8.1f}{tot_p - tot_v:>8.1f}{tot_c:>8}{tot_cached:>5}'
          f'{sum(r["stage_fails"] for r in rows):>5}')
    if tot_c:
        # 추론 서버가 공유 자원이라 실행마다 크게 흔들린다 — 정상 실행에서 7.2~10.0s 를
        # 관측했다(2026-08-06). 그래서 이 값만으로는 실패를 못 가린다. 판정은 아래
        # _report_stage_failures 의 dead_pages(산출물 검사)가 한다. 이 줄은 참고용이다.
        print(f'  호출당 평균 {tot_v / tot_c:.1f}s (서버 부하로 흔들림 — 실패 판정 근거 아님)')
    print(f'  전체 벽시계 {wall_s:.1f}s (파일 합 {tot_p:.1f}s + 러너 오버헤드 '
          f'{wall_s - tot_p:.1f}s), 문서당 평균 {tot_p / len(rows):.1f}s')
    if tot_cached:
        print(f'  ⚠️ VLM 캐시 재생 {tot_cached}/{tot_c}회 — 이 시간은 실제 처리 시간이 아니다. '
              f'VLM_CACHE 를 끄고 다시 재야 한다')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent.parent / "out")
    parser.add_argument("--only", type=str, default=None, help="파일명 부분 일치 필터")
    parser.add_argument("--exclude", type=str, default=None, help="파일명 부분 일치 제외 (예: 은행연합회 — rag 트랙)")
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

    failed: list[str] = []
    timing: list[dict] = []      # 파일별 소요시간 — 마지막에 표로 찍고 out/_timing.json 에 저장
    run_started = time.time()
    for path in files:
        print(f"\n{'=' * 72}\n▶ {path.name}")
        reset_stats()   # 문서별 단계 비용을 따로 본다 (안 하면 누적돼 차분을 손으로 빼야 한다)
        start = time.time()
        try:
            doc = process_file(path, preview_dir=out_prev)
        except Exception as exc:
            # 실패하면 이 문서의 out/json·out/llm_view 는 **이전 실행 결과가 그대로 남는다**.
            # 그걸 모르고 비교하면 "완전히 결정론적"이라는 가짜 결론이 나온다 —
            # 2026-08-03 실측으로 당함(코드가 죽었는데 5문서 중 4문서가 옛 파일이라
            # 100% 일치로 보였다). 그래서 실패를 종료코드로 올려 자동화가 알아채게 한다.
            print(f"  ✗ 처리 실패: {exc}")
            failed.append(path.name)
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

        vlm_calls = int(sum(s["calls"] for s in STATS.values()))
        vlm_cached = int(sum(s["cached"] for s in STATS.values()))
        vlm_secs = sum(s["seconds"] for s in STATS.values())
        # 단계 실패 집계 — notes 에만 남고 아무도 안 세던 것 (2026-08-06 사고 참조)
        stage_fails = (sum(1 for n in doc.notes if "실패" in n)
                       + sum(1 for pg in doc.pages for n in pg.notes if "실패" in n))
        # 캔버스가 있는데 통독 후보가 하나도 안 붙은 페이지 = 밴드 통독이 통째로 죽었다.
        # '실패' 문자열이 아니라 **산출물 자체**로 재는 검사라 에러 메시지 형식이 바뀌어도 산다.
        dead_pages = [
            pg.page_no for pg in doc.pages
            if pg.canvas_w and any(r.lines for r in pg.regions)
            and not any(r.vlm_reading for r in pg.regions)
        ]
        timing.append({
            "stage_fails": stage_fails,
            "dead_pages": dead_pages,
            "file": path.name,
            "size_kb": round(path.stat().st_size / 1024, 1),
            "type": doc.file_type,
            "pages": len(doc.pages),
            "routes": "+".join(sorted({pg.parse_route for pg in doc.pages})),
            "regions": sum(len(pg.regions) for pg in doc.pages),
            "lines": total_lines,
            "parse_s": round(elapsed, 1),
            "vlm_calls": vlm_calls,
            "vlm_cached": vlm_cached,
            "vlm_s": round(vlm_secs, 1),
            # 파싱 시간에서 VLM 대기를 뺀 나머지 — 렌더·OCR(PaddleX)·우리 코드
            "non_vlm_s": round(elapsed - vlm_secs, 1),
        })

    if timing:
        _print_timing(timing, time.time() - run_started)
        (args.out / "_timing.json").write_text(
            json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if failed:
        print(f"\n✗ {len(failed)}건 실패 — 해당 문서의 out/json·out/llm_view 는 이전 실행본이 남아 있다: "
              + ", ".join(failed))
        sys.exit(1)
    if _report_stage_failures(timing):
        sys.exit(1)
    print("\n완료.")


def _report_stage_failures(rows: list[dict]) -> bool:
    """단계 실패를 소리 내어 보고한다. 산출물이 무효인 실행이면 True(종료코드 1).

    **왜 필요한가 — 2026-08-06 실측 사고.** 사내 게이트웨이가 모델명을 바꿔
    (`gemma-4-26b-...` → `spark-gemma-4-26b-...`) VLM 호출 **56회가 전부 400 으로
    죽었는데** 이 러너는 exit 0 에 "완료." 를 찍었다. 단계별 예외를 `page.notes` 에만
    적고 아무도 그걸 세지 않았기 때문이다. 겉으로 보이던 것들이 전부 정상처럼 보였다:

      · `VLM호출=56`      — 실패 호출도 카운터에 잡힌다
      · `452초`           — 추론이 아니라 3회 재시도 + 2s/4s 백오프 대기였다
      · `영역 225개`      — OCR 은 멀쩡히 돌았다. 죽은 건 VLM 단계뿐
      · 유일한 단서가 라인 509 → 485 (스윕 회수분 24줄 소실)뿐이었다

    그래서 두 가지로 잰다. `stage_fails` 는 에러 메시지를 세는 약한 신호이고,
    `dead_pages` 는 **산출물 자체**를 보는 강한 신호다 — 캔버스가 있고 글자도 붙었는데
    통독 후보가 한 건도 없으면 그 페이지의 VLM 경로는 통째로 죽은 것이다. 종료코드는
    후자로만 올린다(밴드 하나가 실패한 부분 손상까지 CI 를 깨뜨리지는 않는다).
    """
    fails = [r for r in rows if r["stage_fails"]]
    dead = [r for r in rows if r["dead_pages"]]
    if not fails and not dead:
        return False
    print("\n" + "!" * 100)
    print("VLM 단계 실패 — 이 산출물을 지표나 대외 자료에 쓰기 전에 원인을 확인할 것")
    print("!" * 100)
    for r in fails:
        print(f'  {r["file"]}: 단계 실패 {r["stage_fails"]}건')
    for r in dead:
        print(f'  ✗ {r["file"]}: 페이지 {r["dead_pages"]} 에 통독 후보가 0건 '
              f'— 이 페이지의 VLM 경로는 통째로 죽었다 (산출물 무효)')
    print("  확인: grep 실패 out/_run_parse.log  /  모델명은 GET <gemma_url>/../models 로 대조")
    return bool(dead)


if __name__ == "__main__":
    main()
