# -*- coding: utf-8 -*-
"""광고 산출물(`_parsed.json`) 전수에서 **구조 결함 6종**을 센다.

왜 필요한가. 눈으로 본 문서는 24/93 건이고, 거기서 나온 결함이 예외인지 전형인지는
숫자로만 갈린다. 그리고 고도화의 완료 기준이 전부 이 지표의 **전/후 비교**다
(기준값은 `docs/etc/N-파싱-고도화-계획.md` 2절에 박혀 있다).

세는 것 — 전부 산출물만 읽는다(파이프라인 재실행 없음):
  빈 영역          글자도 VLM 후보도 없는 영역 (하류 페이로드에서 빠진다)
  gubun 심부       gubun 이 붙었는데 5줄을 넘는 영역 (VLM 은 첫 4줄만 봤다)
  되돌아감          뒤 영역이 앞 영역보다 세로로 완전히 위인 인접쌍 (읽기순서 파손)
  근접 중복         IoU>=0.5 쌍 (타일 오버랩 쌍둥이 → 문장 쪼개짐)
  포함             한쪽이 다른쪽 면적의 90% 이상을 덮는 쌍 (근거 위치 과대)
  표·항목 층        table 채움/note/미포함줄, 변수형 항목 착수 여부

사용: uv run python tools/measure_parse_defects.py out_ad_full/json
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
from pathlib import Path


def _pair(a: list[int], b: list[int]) -> tuple[float, float]:
    """(IoU, 작은 쪽 대비 겹침비)."""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0, 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    aa = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    bb = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (aa + bb - inter), inter / min(aa, bb)


def measure_doc(doc: dict) -> dict:
    d = collections.Counter()
    for page in doc.get("pages") or []:
        regions = page.get("regions") or []
        d["regions"] += len(regions)
        for r in regions:
            lines = r.get("lines") or []
            d["lines"] += len(lines)
            d["style"] += sum(1 for l in lines if l.get("style"))
            d["labeled_lines"] += sum(1 for l in lines if l.get("labels"))
            if not lines and not r.get("vlm_reading"):
                d["empty"] += 1
            if r.get("gubun"):
                d["gubun"] += 1
                if len(lines) > 4:
                    d["gubun_deep"] += 1
                    d["gubun_deep_16plus"] += 1 if len(lines) > 15 else 0
            table = r.get("table")
            if table:
                d["tables"] += 1
                d["tbl_note"] += 1 if table.get("note") else 0
                d["tbl_outside"] += 1 if table.get("lines_outside_cells") else 0
            if r.get("vlm_reading"):
                d["rel_" + str(r.get("vlm_reading_relation"))] += 1
        # 읽기순서: 뒤 영역이 앞 영역보다 완전히 위 = 되돌아감
        for a, b in zip(regions, regions[1:]):
            if b["bbox"][3] <= a["bbox"][1]:
                d["back"] += 1
        for a, b in itertools.combinations(regions, 2):
            iou, cover = _pair(a["bbox"], b["bbox"])
            d["near"] += 1 if iou >= 0.5 else 0
            d["contain"] += 1 if cover >= 0.9 else 0
    for item in doc.get("template_items") or []:
        d["items"] += 1
        d["items_no_refs"] += 0 if item.get("line_refs") else 1
        for entry in item.get("variable_entries") or []:
            d["var_entries"] += 1
            d["var_pending"] += 1 if entry.get("status") == "미착수" else 0
    return d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default="out_ad_full/json", type=Path)
    ap.add_argument("--top", type=int, default=10, help="지표별 상위 문서 수")
    args = ap.parse_args()

    files = sorted(args.src.glob("*.json"))
    if not files:
        raise SystemExit(f"산출물이 없다: {args.src}")

    total: collections.Counter = collections.Counter()
    per_doc: list[tuple[str, collections.Counter]] = []
    for fp in files:
        d = measure_doc(json.loads(fp.read_text(encoding="utf-8")))
        per_doc.append((fp.stem, d))
        total.update(d)

    def pct(a: int, b: int) -> str:
        return f"{a}/{b} = {100 * a / b:.1f}%" if b else f"{a}/0"

    print(f"문서 {len(files)}건 · 영역 {total['regions']} · 줄 {total['lines']}\n")
    print(f"빈 영역           {pct(total['empty'], total['regions'])}")
    print(f"gubun 5줄 초과     {pct(total['gubun_deep'], total['gubun'])}"
          f"  (16줄+ {total['gubun_deep_16plus']})")
    print(f"되돌아가는 인접쌍   {total['back']}")
    print(f"근접 중복쌍(IoU≥.5) {total['near']}")
    print(f"포함쌍(90%+)       {total['contain']}")
    print(f"table 채워진 영역   {total['tables']}"
          f"  (note {total['tbl_note']} / 미포함줄 {total['tbl_outside']})")
    print(f"변수형 항목 미착수   {pct(total['var_pending'], total['var_entries'])}")
    print(f"라벨 붙은 줄        {pct(total['labeled_lines'], total['lines'])}")
    print(f"시인성 있는 줄      {pct(total['style'], total['lines'])}")
    rels = {k[4:]: v for k, v in total.items() if k.startswith("rel_")}
    print("relation           " + " / ".join(f"{k} {v}" for k, v in
                                             sorted(rels.items(), key=lambda x: -x[1])))

    for key in ("gubun_deep", "back", "empty", "near", "contain"):
        print(f"\n=== {key} 상위 {args.top} ===")
        for name, d in sorted(per_doc, key=lambda x: -x[1][key])[:args.top]:
            print(f"  {name[:44]:<46} {d[key]:>4}  (영역 {d['regions']})")

    kinds = ("empty", "gubun_deep", "back", "near", "contain")
    spread: collections.Counter = collections.Counter(
        sum(1 for k in kinds if d[k]) for _, d in per_doc
    )
    print("\n=== 문서당 결함 종류 수 ===")
    for k in sorted(spread):
        print(f"  {k}종: {spread[k]}문서")


if __name__ == "__main__":
    main()
