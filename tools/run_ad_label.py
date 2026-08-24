# -*- coding: utf-8 -*-
"""광고물 트랙 러너 — 파싱 → 템플릿 판정 → 라벨링 → **통합 JSON 하나**.

    파싱(process_file)      쪽·영역·줄·좌표
      → resolve_template    12종 중 하나 (못 정하면 VLM 결선투표 1회)
      → label_regions_vlm   영역마다 템플릿 항목명 (쪽당 1회)
      → label_document      정형 문구 대조(1층) + 영역 라벨(2층) 합치기
      → build_unified       좌표와 라벨을 한 파일로

산출: `<out>/json/<doc_id>.json` (통합) · `<out>/pages/<doc_id>_p{n}.jpg` (검수 화면용
원본 축소본 — 파이프라인 미리보기와 달리 **박스를 그리지 않는다.** 박스는 검수 화면이
좌표로 직접 그려야 어떤 영역인지 색으로 구분할 수 있다).

    uv run python tools/run_ad_label.py --input <파일 또는 폴더>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image                                          # noqa: E402

from nh_parsing import ad_template as AT                       # noqa: E402
from nh_parsing.ad_export import label_parsed_ad, page_canvases  # noqa: E402
from nh_parsing.gemma_client import STATS, reset_stats          # noqa: E402
from nh_parsing.pipeline import process_file                   # noqa: E402

EXTS = {".pdf", ".png", ".jpg", ".jpeg"}
PAGE_MAX_W = 1000        # 검수 화면용 축소 폭. 좌표는 canvas_w 기준 비율로 다시 그린다.


def run_one(path: Path, out_dir: Path, pack: dict, reuse_parse: bool = False) -> dict:
    started = time.time()
    reset_stats()          # 문서별로 따로 본다 — 누적되면 차분을 손으로 빼야 한다

    # 파싱 결과를 따로 저장해 두고 재사용할 수 있게 한다. 실측(2026-08-24) 파싱은
    # 문서당 9분이 걸리는데 라벨링만 고쳐 다시 볼 일이 잦다 — 매번 다시 파싱하면
    # 라벨링 한 줄 고치는 데 45분이 든다. VLM 캐시는 VLM 만 덮고 OCR 은 못 덮는다.
    parse_path = out_dir / "parse" / f"{path.stem}.json"
    if reuse_parse and parse_path.is_file():
        doc = json.loads(parse_path.read_text(encoding="utf-8"))
        print("  (저장된 파싱 결과 재사용)")
    else:
        doc = json.loads(process_file(path).model_dump_json())
        parse_path.parent.mkdir(parents=True, exist_ok=True)
        parse_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    canvases = page_canvases(path)
    unified = label_parsed_ad(doc, path.name, canvases, pack)

    (out_dir / "json").mkdir(parents=True, exist_ok=True)
    (out_dir / "pages").mkdir(parents=True, exist_ok=True)
    (out_dir / "json" / f"{doc['doc_id']}.json").write_text(
        json.dumps(unified, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    for pno, img in canvases.items():
        if img.width > PAGE_MAX_W:
            h = round(img.height * PAGE_MAX_W / img.width)
            img = img.resize((PAGE_MAX_W, h), Image.LANCZOS)
        img.convert("RGB").save(out_dir / "pages" / f"{doc['doc_id']}_p{pno}.jpg", quality=72)

    c = unified["completeness"]
    regions = sum(len(p["regions"]) for p in unified["pages"])
    # VLM 대기와 그 외(렌더·PaddleX OCR·우리 코드)를 갈라 둔다 — 늘리는 방법이 다르고,
    # 농협 규격의 timeout 값을 얼마로 신고할지도 이 숫자로 정해야 한다.
    vlm_s = sum(s["seconds"] for s in STATS.values())
    vlm_calls = int(sum(s["calls"] for s in STATS.values()))
    vlm_cached = int(sum(s["cached"] for s in STATS.values()))
    vlm_labeled = sum(1 for p in unified["pages"] for r in p["regions"] if r["gubun"])
    return {
        "file": path.name,
        "vlm_s": vlm_s, "vlm_calls": vlm_calls, "vlm_cached": vlm_cached,
        "template": unified["template"]["template_id"] or "판단불가",
        "note": unified["template"]["note"],
        "regions": regions,
        "vlm_labeled": vlm_labeled,
        "lines": c["lines_total"],
        "covered": c["lines_covered"],
        "elapsed": time.time() - started,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True, help="파일 또는 폴더")
    ap.add_argument("--out", type=Path, default=ROOT / "out_ad")
    ap.add_argument("--only", default=None, help="폴더일 때 파일명 부분 일치 필터")
    ap.add_argument("--reuse-parse", action="store_true",
                    help="<out>/parse 에 저장된 파싱 결과가 있으면 다시 파싱하지 않는다")
    args = ap.parse_args()

    pack = AT.load_pack()
    missing = AT.check_ids(pack)
    if missing:
        raise SystemExit(f"판정표가 사전에 없는 템플릿을 가리킨다: {missing}")

    files = (
        [args.input] if args.input.is_file()
        else sorted(p for p in args.input.iterdir() if p.suffix.lower() in EXTS)
    )
    if args.only:
        files = [p for p in files if args.only in p.name]
    if not files:
        raise SystemExit(f"처리할 파일이 없다: {args.input}")

    rows, failed = [], []
    for path in files:
        print(f"\n{'=' * 72}\n▶ {path.name}")
        try:
            row = run_one(path, args.out, pack, reuse_parse=args.reuse_parse)
        except Exception as exc:                               # noqa: BLE001
            print(f"  ✗ 실패: {type(exc).__name__}: {exc}")
            failed.append(path.name)
            continue
        rows.append(row)
        print(f"  템플릿: {row['template']}  ({row['note']})")
        print(f"  영역 {row['regions']} / VLM라벨 {row['vlm_labeled']} / "
              f"줄 {row['lines']} / 라벨닿음 {row['covered']} / {row['elapsed']:.0f}초 "
              f"(VLM {row['vlm_s']:.0f}초 {row['vlm_calls']}회, 캐시 {row['vlm_cached']})")

    print(f"\n{'=' * 112}")
    print(f'{"파일":<32}{"템플릿":<26}{"영역":>5}{"VLM라벨":>8}{"줄":>5}{"라벨닿음":>9}'
          f'{"총초":>7}{"VLM초":>7}{"그외초":>7}{"호출":>5}{"캐시":>5}')
    print("-" * 112)
    for r in rows:
        print(f'{r["file"][:31]:<32}{r["template"][:25]:<26}{r["regions"]:>5}'
              f'{r["vlm_labeled"]:>8}{r["lines"]:>5}{r["covered"]:>9}{r["elapsed"]:>7.0f}'
              f'{r["vlm_s"]:>7.0f}{r["elapsed"] - r["vlm_s"]:>7.0f}'
              f'{r["vlm_calls"]:>5}{r["vlm_cached"]:>5}')
    tot_l = sum(r["lines"] for r in rows)
    tot_c = sum(r["covered"] for r in rows)
    if tot_l:
        print(f'\n라벨 닿은 줄 {tot_c}/{tot_l} = {tot_c / tot_l:.0%}')
    if failed:
        raise SystemExit(f"실패 {len(failed)}건: {failed}")


if __name__ == "__main__":
    main()
