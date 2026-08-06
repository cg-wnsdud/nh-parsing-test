# -*- coding: utf-8 -*-
"""문서에 쓰는 모든 숫자를 out/ 에서 다시 뽑는다 (모델 호출 0회).

**왜 필요한가.** 이 저장소에서 문서 숫자와 실제 산출물이 어긋난 사고가 반복됐다 —
주석에 '미배정 39줄'(layout_gap.py), 검수화면에 '전체 21줄 중 20줄', 발표대본에
'합계 영역 224개'(행 합은 225)가 남아 있었다. 전부 옛 실행본 수치다.

그래서 문서를 쓸 때 손으로 세지 않는다. 이 스크립트가 내는 값만 쓰고, 문서에는
이 스크립트를 돌리라고 적어 둔다. 숫자가 의심되면 여기서 다시 뽑으면 된다.

사용:
  uv run python tools/verify_numbers.py            # 전체
  uv run python tools/verify_numbers.py --section parse
  uv run python tools/verify_numbers.py --json     # 기계 판독용

섹션: parse(파싱 집계) / relation(관계 딱지 재계산) / linecand(라인 후보 전달 여부)
      role(역할 3층) / card(카드 게이트 재현) / stage3(STAGE_3 집계)
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "out"
SAMPLES = ROOT / "nh-data" / "sample-data"


def _sq(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(s or "")).lower()


def _docs() -> list[tuple[str, dict]]:
    return [(p.stem, json.loads(p.read_text(encoding="utf-8")))
            for p in sorted((OUT / "json").glob("*.json"))]


# ────────────────────────────── parse ──────────────────────────────

def section_parse(res: dict) -> None:
    """페이지별 영역·라인·미배정·저신뢰 집계. 검수화면 pill 과 같은 정의를 쓴다."""
    rows, tot = [], collections.Counter()
    for stem, d in _docs():
        for pg in d["pages"]:
            regions = pg["regions"]
            in_region = sum(len(r["lines"]) for r in regions)
            un = pg["unassigned_lines"]
            un_src = collections.Counter(l["source"] for l in un)
            low = sum(1 for r in regions for l in r["lines"]
                      if l.get("confidence") is not None and l["confidence"] < 0.8)
            empty = sum(1 for r in regions if not r["lines"])
            # 라인은 있는데 텍스트가 전부 공백 → llm_view 에서 빠진다
            blank = sum(1 for r in regions
                        if r["lines"] and not "".join(l["text"] for l in r["lines"]).strip())
            cand = sum(1 for r in regions if r.get("vlm_reading"))
            line_cand = sum(1 for r in regions for l in r["lines"] if l.get("vlm_reading"))
            rows.append({
                "doc": stem, "page": pg["page_no"], "route": pg["parse_route"],
                "status": pg["parse_status"],
                "canvas": f'{pg["canvas_w"]}x{pg["canvas_h"]}',
                "triage": (pg.get("triage") or {}).get("verdict"),
                "regions": len(regions), "empty_boxes": empty, "blank_text": blank,
                "lines_in_regions": in_region, "unassigned": len(un),
                "un_sweep": un_src.get("vlm_sweep", 0),
                "un_ocr": len(un) - un_src.get("vlm_sweep", 0),
                "total_lines": in_region + len(un),
                "lowconf": low, "region_cand": cand, "line_cand": line_cand,
            })
            for k in ("regions", "empty_boxes", "blank_text", "lines_in_regions",
                      "unassigned", "un_sweep", "un_ocr", "total_lines",
                      "lowconf", "region_cand", "line_cand"):
                tot[k] += rows[-1][k]
    res["parse_pages"] = rows
    res["parse_total"] = dict(tot)
    hdr = ("문서", "p", "route", "canvas", "영역", "빈", "영역내라인", "미배정", "sweep",
           "ocr", "총라인", "저신뢰", "영역후보", "라인후보")
    print(f"{hdr[0]:<24}{hdr[1]:>3}{hdr[2]:>9}{hdr[3]:>12}{hdr[4]:>5}{hdr[5]:>4}"
          f"{hdr[6]:>11}{hdr[7]:>7}{hdr[8]:>7}{hdr[9]:>5}{hdr[10]:>7}{hdr[11]:>7}"
          f"{hdr[12]:>9}{hdr[13]:>9}")
    for r in rows:
        print(f'{r["doc"][:23]:<24}{r["page"]:>3}{r["route"]:>9}{r["canvas"]:>12}'
              f'{r["regions"]:>5}{r["empty_boxes"]:>4}{r["lines_in_regions"]:>11}'
              f'{r["unassigned"]:>7}{r["un_sweep"]:>7}{r["un_ocr"]:>5}'
              f'{r["total_lines"]:>7}{r["lowconf"]:>7}{r["region_cand"]:>9}{r["line_cand"]:>9}')
    print(f'{"합계":<24}{"":>3}{"":>9}{"":>12}{tot["regions"]:>5}{tot["empty_boxes"]:>4}'
          f'{tot["lines_in_regions"]:>11}{tot["unassigned"]:>7}{tot["un_sweep"]:>7}'
          f'{tot["un_ocr"]:>5}{tot["total_lines"]:>7}{tot["lowconf"]:>7}'
          f'{tot["region_cand"]:>9}{tot["line_cand"]:>9}')
    print(f'  · 텍스트가 전부 공백인 영역 {tot["blank_text"]}개 (llm_view 에서 빠짐)')


# ───────────────────────────── relation ─────────────────────────────

def section_relation(res: dict) -> None:
    """관계 딱지를 **최종 라인 순서로** 다시 계산해 저장값과 비교.

    ⚠️ **이 불일치 숫자는 원인이 섞여 있다.** 저장값은 out/json 을 만든 실행 시점의
    코드가 낸 것이고 재계산은 현행 코드가 낸다. 그 사이에 코드가 바뀌면 순서 문제와
    로직 변경분이 한 숫자에 합쳐진다 — 2026-08-06 실측: 18건 중 순서 7건 ·
    숫자게이트(1c86add) 4건 · 유사도폴백 제거(6d860ec) 나머지.

    그래서 원인별로 따로 센다:
      · 이 함수      = 저장값 vs 현행·최종순서   (재파싱 후 0 이어야 함 — 회귀 감시)
      · _relation_order_effect = 순서 효과만    (현행 코드로 두 순서를 비교)
      · _relation_numeric_gate = 숫자 게이트만
    """
    from nh_parsing.truncation import classify_reading

    stored, fixed = collections.Counter(), collections.Counter()
    diffs = []
    for stem, d in _docs():
        for pg in d["pages"]:
            for r in pg["regions"]:
                cand = r.get("vlm_reading")
                if not cand:
                    continue
                s = r.get("vlm_reading_relation")
                f = classify_reading(" ".join(l["text"] for l in r["lines"]), cand).kind
                stored[s] += 1
                fixed[f] += 1
                if s != f:
                    diffs.append({"doc": stem, "page": pg["page_no"],
                                  "region_id": r["region_id"], "stored": s, "recomputed": f,
                                  "ocr": " ".join(l["text"] for l in r["lines"])[:70],
                                  "cand": cand.replace("\n", " / ")[:70]})
    res["relation_stored"] = dict(stored)
    res["relation_recomputed"] = dict(fixed)
    res["relation_diffs"] = diffs
    print("저장값     :", dict(stored))
    print("재계산     :", dict(fixed))
    print(f"불일치 {len(diffs)}건 / 전체 {sum(stored.values())}건")
    for x in diffs:
        print(f'  {x["doc"][:12]:14s} p{x["page"]} {x["region_id"]:9s} '
              f'{x["stored"]} → {x["recomputed"]}')
        print(f'      정본 {x["ocr"]!r}')
        print(f'      후보 {x["cand"]!r}')
    _relation_order_effect(res)
    _relation_numeric_gate(res)


def _relation_order_effect(res: dict) -> None:
    """라인 순서만의 효과를 가른다 — **현행 코드로 두 순서를 비교한다.**

    옛 경로(_merged_band_read 안에서 즉시 판정)는 전역 정렬 직후 순서, 즉
    `(top, left)` 로 나열된 라인을 정본으로 봤다(regions.py:108). 현행 경로
    (_score_reading_candidates)는 sort_reading_order 로 시각적 행을 묶은 뒤 본다.
    표의 `라벨|값` 은 두 셀의 top 이 몇 px 어긋나기만 해도 (top,left) 정렬에서
    값이 라벨보다 앞에 오고, 그러면 '앞 생략'이 '뒤 잘림'으로 뒤집힌다.

    저장값과 비교하지 않으므로 out/json 이 낡아도 이 숫자는 오염되지 않는다.
    """
    from nh_parsing.tiling import sort_reading_order
    from nh_parsing.truncation import classify_reading
    from nh_parsing.ir import Line

    flips = []
    total = 0
    for stem, d in _docs():
        for pg in d["pages"]:
            for r in pg["regions"]:
                cand = r.get("vlm_reading")
                if not cand or len(r["lines"]) < 2:
                    continue
                total += 1
                lines = [Line(**{k: v for k, v in l.items() if k in Line.model_fields})
                         for l in r["lines"]]
                # 저장된 순서 = 이미 sort_reading_order 를 거친 최종 순서
                final_txt = " ".join(l.text for l in lines)
                # 옛 순서 재현: 전역 정렬 키 (top, left)
                old = sorted(lines, key=lambda l: (l.bbox[1] if l.bbox else 0,
                                                   l.bbox[0] if l.bbox else 0))
                old_txt = " ".join(l.text for l in old)
                if _sq(old_txt) == _sq(final_txt):
                    continue
                a = classify_reading(old_txt, cand).kind
                b = classify_reading(final_txt, cand).kind
                if a != b:
                    flips.append({"doc": stem, "page": pg["page_no"],
                                  "region_id": r["region_id"], "old_order": a,
                                  "final_order": b, "ocr_old": old_txt[:70],
                                  "ocr_final": final_txt[:70]})
    res["relation_order_flips"] = flips
    print(f"\n라인 순서만의 효과 (현행 코드, 옛순서 vs 최종순서): {len(flips)}건 "
          f"/ 라인 2개 이상인 후보 {total}건")
    for x in flips:
        print(f'  {x["doc"][:12]:14s} p{x["page"]} {x["region_id"]:9s} '
              f'{x["old_order"]} → {x["final_order"]}')
        print(f'      옛순서 정본 {x["ocr_old"]!r}')
        print(f'      최종  정본 {x["ocr_final"]!r}')


def _relation_numeric_gate(res: dict) -> None:
    """숫자 게이트(2026-08-05 추가)가 same 에서 빼낸 건수를 따로 센다.

    위 불일치 숫자와 합치면 안 된다 — 원인이 다르다. 위는 라인 순서 시점 문제고
    이건 '모양은 같은데 적힌 숫자가 다르다'다. 합쳐 놓으면 어느 쪽이 움직였는지
    못 본다.
    """
    from nh_parsing.truncation import _relation_by_shape, classify_reading

    moved = []
    for stem, d in _docs():
        for pg in d["pages"]:
            for r in pg["regions"]:
                cand = r.get("vlm_reading")
                if not cand:
                    continue
                ocr = " ".join(l["text"] for l in r["lines"])
                if _relation_by_shape(ocr, cand).kind == classify_reading(ocr, cand).kind:
                    continue
                moved.append({"doc": stem, "page": pg["page_no"], "region_id": r["region_id"],
                              "ocr": ocr[:70], "cand": cand.replace("\n", " / ")[:70]})
    res["relation_numeric_gate"] = moved
    print(f"\n숫자 게이트가 same 에서 빼낸 것: {len(moved)}건")
    for x in moved:
        print(f'  {x["doc"][:12]:14s} p{x["page"]} {x["region_id"]:9s} same → diverged')
        print(f'      정본 {x["ocr"]!r}')
        print(f'      후보 {x["cand"]!r}')


# ───────────────────────────── linecand ─────────────────────────────

def section_linecand(res: dict) -> None:
    """라인 단위 후보(⑫⑬)가 llm_view 로 전달되는가."""
    rows = []
    for stem, d in _docs():
        vp = OUT / "llm_view" / f"{stem}.json"
        if not vp.exists():
            continue
        view = json.loads(vp.read_text(encoding="utf-8"))
        seen = {r["region_id"]: _sq((r.get("text") or "") + (r.get("vlm_reading") or ""))
                for p in view["pages"] for r in p["regions"]}
        for pg in d["pages"]:
            for r in pg["regions"]:
                for l in r["lines"]:
                    if not l.get("vlm_reading"):
                        continue
                    rows.append({
                        "doc": stem, "region_id": r["region_id"],
                        "stage": l.get("vlm_reading_stage"),
                        "ocr": l["text"], "cand": l["vlm_reading"],
                        "conf": l.get("confidence"), "vlm_conf": l.get("vlm_reading_conf"),
                        "reaches_llm_view": _sq(l["vlm_reading"]) in seen.get(r["region_id"], ""),
                    })
    lost = [r for r in rows if not r["reaches_llm_view"]]
    res["line_candidates"] = rows
    res["line_candidates_lost"] = lost
    print(f"라인 후보 부착 {len(rows)}건 / 그중 llm_view 미도달 {len(lost)}건")
    for r in rows:
        mark = "✗ 유실" if not r["reaches_llm_view"] else "○ 전달"
        print(f'  {mark} {r["doc"][:12]:14s} {r["region_id"]:9s} [{r["stage"]}] '
              f'{r["ocr"]!r} → {r["cand"]!r}')


# ─────────────────────────────── role ───────────────────────────────

def section_role(res: dict) -> None:
    """역할 3층을 재계산 — 1층(label 매핑)·2층(코드 규칙)은 순수 코드라 재현된다."""
    from nh_parsing.ir import Line, Region
    from nh_parsing.regions import _LABEL_TO_ROLE, _refine_role

    cnt = collections.Counter()
    changed12 = changed23 = 0
    rules_with_lines = []
    for stem, d in _docs():
        for pg in d["pages"]:
            for r in pg["regions"]:
                src = r.get("role_source")
                if src is None:
                    cnt["source없음(HWP)"] += 1
                    continue
                cnt["VLM" if src == "vlm" else "규칙폴백"] += 1
                l1 = _LABEL_TO_ROLE.get(str(r["label"]).lower(), "본문")
                reg = Region(region_id=r["region_id"], bbox=r.get("bbox"), label=r["label"],
                             role=l1, lines=[Line(**x) for x in r["lines"]])
                _refine_role(reg, pg["canvas_h"])
                if l1 != reg.role:
                    changed12 += 1
                if src == "vlm" and reg.role != r["role"]:
                    changed23 += 1
                if src == "rules" and r["lines"]:
                    rules_with_lines.append(f'{stem[:12]}/{r["region_id"]}')
    res["role_source"] = dict(cnt)
    res["role_changed_1to2"] = changed12
    res["role_changed_2to3"] = changed23
    res["role_rules_with_lines"] = rules_with_lines
    print("role_source:", dict(cnt), "합", sum(cnt.values()))
    print(f"1층(label)→2층(규칙) 변경 {changed12}개 / 2층→3층(VLM) 변경 {changed23}개")
    print(f"규칙폴백인데 라인이 있는 영역 {len(rules_with_lines)}개: {rules_with_lines}")
    print("  (나머지 폴백은 라인이 없어 VLM 에게 묻지 않은 빈 검출 박스)")


# ─────────────────────────────── card ───────────────────────────────

def section_card(res: dict) -> None:
    """카드 게이트를 실제 캔버스로 재현한다 (모델 호출 0회)."""
    import pypdfium2 as pdfium

    from nh_parsing.bands import count_cards_by_density
    from nh_parsing.canvas import load_image_canvas, native_image_dpi, render_pdf_page
    from nh_parsing.config import SETTINGS

    rows = []

    def check(name: str, img) -> None:
        ar = img.height / img.width
        scroll = ar >= SETTINGS.card_split_max_aspect
        n, spans = count_cards_by_density(img)
        verdict = ("스크롤 → VLM 호출 안 함" if scroll else
                   ("슬라이드·다중덩어리 → VLM 호출" if n >= 2 else "단일패널 → VLM 호출 안 함"))
        rows.append({"page": name, "size": f"{img.width}x{img.height}",
                     "aspect": round(ar, 2), "density_clusters": n, "verdict": verdict,
                     "spans": [(a, b, round(m, 3)) for a, b, m in spans]})
        print(f"  {name:12s} {img.width}x{img.height:<6} 종횡비 {ar:5.2f}  덩어리 {n}  {verdict}")
        if not scroll and n >= 2:
            for i, (a, b, m) in enumerate(spans, 1):
                print(f"        {i}번 x_ratio {a/img.width:.2f}~{b/img.width:.2f} 글자량 {m:.0%}")

    for f in sorted(SAMPLES.glob("*.png")):
        check(f.stem[:12], load_image_canvas(f).image)
    for f in sorted(SAMPLES.glob("*.pdf")):
        for i, page in enumerate(pdfium.PdfDocument(f)):
            check(f"{f.stem[12:15]} p{i+1}", render_pdf_page(page, i + 1,
                                                             dpi=native_image_dpi(page)).image)
    res["card_gate"] = rows


# ────────────────────────────── stage3 ──────────────────────────────

def section_stage3(res: dict) -> None:
    """STAGE_3 집계 — 필드 상태, 부재 4분류, 코드 검산 장치 발동 횟수."""
    rows, tot = [], collections.Counter()
    for p in sorted((OUT / "extracted").glob("*.json")):
        if p.stem.startswith("_"):
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        c, g = d.get("coverage", {}), d.get("review_gaps", {})
        kinds = collections.Counter(u.get("kind") for u in d.get("unmapped", []))
        row = {
            "doc": p.stem, "ad_type": d.get("ad_type"),
            "call_groups": len(d.get("group_analysis", {})),
            "found": c.get("fields_found"), "not_found": c.get("fields_not_found"),
            "uncertain": c.get("fields_uncertain"),
            "missing": c.get("absence_missing"),
            "missing_in_events": c.get("absence_missing_in_events"),
            "not_applicable": c.get("absence_not_applicable"),
            "out_of_scope": c.get("absence_out_of_scope"),
            "needs_check": c.get("absence_needs_check"),
            "region_coverage": c.get("region_coverage"),
            "status_corrections": len(d.get("status_corrections", [])),
            "events_pruned": bool(d.get("events_pruned")),
            "evidence_unbacked": len(d.get("evidence_unbacked", [])),
            "unused_figures": len(d.get("unused_figures", [])),
            "input_gap": len(d.get("input_gap", [])),
            "unmapped": dict(kinds),
            "missing_keys": [m["field_key"] for m in g.get("미표시", [])],
        }
        rows.append(row)
        for k in ("found", "not_found", "uncertain", "missing", "missing_in_events",
                  "not_applicable", "out_of_scope", "needs_check", "call_groups",
                  "status_corrections", "evidence_unbacked", "unused_figures", "input_gap"):
            tot[k] += row[k] or 0
        tot["events_pruned"] += int(row["events_pruned"])
    res["stage3"] = rows
    res["stage3_total"] = dict(tot)
    for r in rows:
        print(f'{r["doc"][:24]:26s} {r["ad_type"]:6s} 그룹{r["call_groups"]} | '
              f'found {r["found"]:2d} not_found {r["not_found"]:2d} unc {r["uncertain"]} | '
              f'미표시 {r["missing"]}(+ev {r["missing_in_events"]}) 해당없음 {r["not_applicable"]:2d} '
              f'판정제외 {r["out_of_scope"]} 확인필요 {r["needs_check"]} | 커버리지 {r["region_coverage"]}')
        print(f'{"":26s} 검산: status보정 {r["status_corrections"]:2d} · 유령이벤트 '
              f'{"제거" if r["events_pruned"] else "없음"} · 근거미검증 {r["evidence_unbacked"]} · '
              f'미실린수치 {r["unused_figures"]} · 입력유실 {r["input_gap"]} | unmapped {r["unmapped"]}')
        print(f'{"":26s} 미표시 항목: {r["missing_keys"]}')
    print("\n합계:", dict(tot))


SECTIONS = {
    "parse": ("파싱 집계 (검수화면 pill 과 같은 정의)", section_parse),
    "relation": ("관계 딱지 저장값 vs 최종순서 재계산", section_relation),
    "linecand": ("라인 단위 후보의 llm_view 전달 여부", section_linecand),
    "role": ("역할 3층 재계산", section_role),
    "card": ("카드 게이트 재현 (모델 호출 0회)", section_card),
    "stage3": ("STAGE_3 집계와 코드 검산 장치 발동", section_stage3),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", choices=list(SECTIONS), default=None)
    ap.add_argument("--json", action="store_true", help="결과를 JSON 으로 stdout 에")
    args = ap.parse_args()

    res: dict = {}
    names = [args.section] if args.section else list(SECTIONS)
    for name in names:
        title, fn = SECTIONS[name]
        if not args.json:
            print(f"\n{'=' * 100}\n[{name}] {title}\n{'=' * 100}")
        fn(res)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
