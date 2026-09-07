# -*- coding: utf-8 -*-
"""라벨 설명 A/B/C 비교 — 조건별 산출물이 실제로 무엇이 달라졌는지 센다.

조건(`NH_LABEL_DEFS`):
  A  항목명 + 예시 1줄            (종전)
  B  A + 구분 설명
  C  B + 포함범위·구별기준

이 도구는 **정확도를 재지 않는다.** 라벨 골드가 있는 문서는 1건뿐이라
(`evaluate_region_gold.py` 가 그쪽을 맡는다), 여기서는 나머지 4건을 포함해
"무엇이 달라졌는가"와 "같은 조건을 다시 돌리면 얼마나 흔들리는가"를 센다.

흔들림을 같이 재는 이유: 서버가 fp8 KV·prefix caching·chunked prefill·투기디코딩을
켠 채로 떠 있어 temperature=0 이어도 배치 상황에 따라 답이 갈린다. 조건 간 차이가
반복 간 차이보다 작으면 그 차이는 설명 덕이 아니다.

    uv run python tools/compare_label_defs.py --root out_0907_label_defs_ab
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

MODES = ("A", "B", "C")
_RUN_DIR = re.compile(r"^([ABC])_r(\d+)$")


def spans_of(doc: dict) -> dict[str, set[tuple[int, int, str]]]:
    """region_id -> {(line_from, line_to, gubun)}. 판정 자체를 비교 단위로 둔다."""
    out: dict[str, set[tuple[int, int, str]]] = {}
    for page in doc.get("pages") or []:
        for region in page.get("regions") or []:
            semantic = ((region.get("template_labels") or {}).get("semantic")) or []
            out[region["region_id"]] = {
                (int(s.get("line_from", -1)), int(s.get("line_to", -1)), str(s.get("gubun") or ""))
                for s in semantic
            }
    return out


def labels_of(doc: dict) -> dict[str, frozenset[str]]:
    """region_id -> 붙은 라벨 이름 집합. 줄 범위 차이는 무시하고 라벨만 본다."""
    return {
        rid: frozenset(g for _, _, g in spans)
        for rid, spans in spans_of(doc).items()
    }


def load_runs(root: Path) -> dict[tuple[str, int], dict[str, dict]]:
    """(mode, rep) -> {doc_stem: 통합 JSON}"""
    runs: dict[tuple[str, int], dict[str, dict]] = {}
    for child in sorted(root.iterdir()):
        m = _RUN_DIR.match(child.name) if child.is_dir() else None
        if not m:
            continue
        docs = {}
        for f in sorted((child / "json").glob("*.json")):
            docs[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        if docs:
            runs[(m.group(1), int(m.group(2)))] = docs
    return runs


def disagreement(a: dict[str, frozenset[str]], b: dict[str, frozenset[str]]) -> tuple[int, int]:
    """(라벨 집합이 다른 영역 수, 양쪽에 다 있는 영역 수)"""
    shared = set(a) & set(b)
    return sum(1 for rid in shared if a[rid] != b[rid]), len(shared)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    args = ap.parse_args()

    runs = load_runs(args.root)
    if not runs:
        sys.exit(f"{args.root} 아래에 <조건>_r<반복> 폴더가 없다")
    reps = sorted({rep for _, rep in runs})
    stems = sorted({s for docs in runs.values() for s in docs})

    print(f"조건 {MODES} · 반복 {reps} · 문서 {len(stems)}건\n")

    # ── 0. 교란 요인 점검 — 템플릿 판정도 VLM 이 한다 ─────────────────
    # 템플릿이 실행마다 뒤집히면 후보 라벨 집합 자체가 달라진다. 그 문서의 라벨
    # 차이는 설명 덕이 아니므로 먼저 걸러 놔야 한다.
    print("── 템플릿 판정 안정성 (뒤집히면 그 문서 비교는 무효) " + "─" * 16)
    flipped: set[str] = set()
    for stem in stems:
        seen = {
            f"{mode}r{rep}": (docs[stem].get("template") or {}).get("template_id")
            for (mode, rep), docs in sorted(runs.items())
            if stem in docs
        }
        uniq = set(seen.values())
        mark = "  ← 뒤집힘" if len(uniq) > 1 else ""
        if len(uniq) > 1:
            flipped.add(stem)
        print(f"  {stem[:34]:36} {'/'.join(sorted(uniq)) if uniq else '?'}{mark}")
    if flipped:
        print(f"  ※ 무효 처리 대상: {', '.join(sorted(flipped))}")
    print()

    # ── 1. 조건별 총량 ────────────────────────────────────────────────
    print("── 조건별 총량 (문서 5건 합계, 반복별) " + "─" * 30)
    print(f"{'조건':4} {'반복':4} {'영역':>6} {'라벨스팬':>8} {'라벨붙은영역':>12} {'빈영역':>8} {'해당없음':>8}")
    totals: dict[str, list[tuple[int, int, int, int, int]]] = defaultdict(list)
    for mode in MODES:
        for rep in reps:
            docs = runs.get((mode, rep))
            if not docs:
                continue
            n_region = n_span = n_labeled = n_empty = n_abstain = 0
            for doc in docs.values():
                for _rid, spans in spans_of(doc).items():
                    n_region += 1
                    real = {s for s in spans if s[2] and s[2] != "해당없음"}
                    n_span += len(real)
                    n_abstain += sum(1 for s in spans if s[2] == "해당없음")
                    if real:
                        n_labeled += 1
                    else:
                        n_empty += 1
            totals[mode].append((n_region, n_span, n_labeled, n_empty, n_abstain))
            print(f"{mode:4} {rep:<4} {n_region:>6} {n_span:>8} {n_labeled:>12} {n_empty:>8} {n_abstain:>8}")
    print()

    # ── 2. 반복 간 흔들림 (같은 조건, 다른 실행) ──────────────────────
    print("── 반복 간 흔들림 — 같은 조건을 다시 돌렸을 때 라벨이 다른 영역 " + "─" * 8)
    noise: dict[str, list[float]] = defaultdict(list)
    for mode in MODES:
        for r1, r2 in itertools.combinations(reps, 2):
            d1, d2 = runs.get((mode, r1)), runs.get((mode, r2))
            if not (d1 and d2):
                continue
            diff = shared = 0
            for stem in stems:
                if stem in d1 and stem in d2:
                    a, b = disagreement(labels_of(d1[stem]), labels_of(d2[stem]))
                    diff += a
                    shared += b
            pct = 100 * diff / shared if shared else 0
            noise[mode].append(pct)
            print(f"  {mode}  r{r1}↔r{r2}   {diff:>4}/{shared:<4} = {pct:5.1f}%")
    print()

    # ── 3. 조건 간 차이 (같은 반복끼리 짝지어 비교) ───────────────────
    print("── 조건 간 차이 — 라벨이 다른 영역 " + "─" * 34)
    for m1, m2 in itertools.combinations(MODES, 2):
        pcts = []
        for rep in reps:
            d1, d2 = runs.get((m1, rep)), runs.get((m2, rep))
            if not (d1 and d2):
                continue
            diff = shared = 0
            for stem in stems:
                if stem in d1 and stem in d2:
                    a, b = disagreement(labels_of(d1[stem]), labels_of(d2[stem]))
                    diff += a
                    shared += b
            if shared:
                pcts.append(100 * diff / shared)
                print(f"  {m1}↔{m2}  r{rep}   {diff:>4}/{shared:<4} = {pcts[-1]:5.1f}%")
        if pcts:
            print(f"  {m1}↔{m2}  평균 {sum(pcts)/len(pcts):5.1f}%")
    print()

    # ── 4. 판정 근거 — 조건 간 차이가 반복 잡음보다 큰가 ──────────────
    print("── 판정 " + "─" * 56)
    noise_all = [p for v in noise.values() for p in v]
    if noise_all:
        avg_noise = sum(noise_all) / len(noise_all)
        print(f"  같은 조건 반복 간 평균 불일치(잡음)   {avg_noise:5.1f}%")
        for m1, m2 in itertools.combinations(MODES, 2):
            pcts = []
            for rep in reps:
                d1, d2 = runs.get((m1, rep)), runs.get((m2, rep))
                if not (d1 and d2):
                    continue
                diff = shared = 0
                for stem in stems:
                    if stem in d1 and stem in d2:
                        a, b = disagreement(labels_of(d1[stem]), labels_of(d2[stem]))
                        diff += a
                        shared += b
                if shared:
                    pcts.append(100 * diff / shared)
            if pcts:
                sig = sum(pcts) / len(pcts)
                verdict = "잡음보다 큼 — 실제 차이" if sig > avg_noise else "잡음 수준 — 차이라 볼 수 없음"
                print(f"  {m1}↔{m2} 평균 {sig:5.1f}%   → {verdict}")
    print()

    # ── 5. 어떤 라벨에서 갈렸나 (A→C 기준, 반복 1) ────────────────────
    print("── A→C 에서 바뀐 라벨 (반복 1) " + "─" * 34)
    dA, dC = runs.get(("A", reps[0])), runs.get(("C", reps[0]))
    if dA and dC:
        gained: Counter = Counter()
        lost: Counter = Counter()
        for stem in stems:
            if stem not in dA or stem not in dC:
                continue
            la, lc = labels_of(dA[stem]), labels_of(dC[stem])
            for rid in set(la) & set(lc):
                for g in lc[rid] - la[rid]:
                    gained[g] += 1
                for g in la[rid] - lc[rid]:
                    lost[g] += 1
        print(f"  {'라벨':16} {'C에서 새로 붙음':>14} {'C에서 떨어짐':>12}")
        for g in sorted(set(gained) | set(lost), key=lambda x: -(gained[x] + lost[x])):
            print(f"  {g:16} {gained[g]:>14} {lost[g]:>12}")


if __name__ == "__main__":
    main()
