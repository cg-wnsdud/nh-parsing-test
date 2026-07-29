# -*- coding: utf-8 -*-
"""골드셋 자동 채점기 — gold/*.yaml (정답) vs out/json (현재 파싱) 비교.

지표:
  1) 섹션 검출: 골드 섹션(타입+위치)이 예측 섹션과 매칭되는가 (타입 일치 + 겹침≥0.3)
  2) 텍스트 커버리지: must_contain 문장이 파싱 라인에 존재하는가 (정규화 부분일치)
  3) 분류: product_group / ad_type 일치

필드 정확도는 이 파일이 재지 않는다 — 필드는 STAGE_3(out/extracted) 단일 출처이고
채점은 tools/verify_extract.py 가 한다. 여기는 '파싱이 화면의 글자를 다 건졌는가'만 본다.

사용: uv run python tools/evaluate.py [--only 파일명부분]
출력: 콘솔 스코어카드 + out/eval_report.md (실패 항목 목록 포함)
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).parent.parent


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
_CIRCLED_TO_DIGIT = str.maketrans(_CIRCLED, "1234567890"[: len(_CIRCLED)])


def norm(text: str) -> str:
    """소수점·하이픈 보존 정규화 (7.1% vs 71% 구분 유지).

    - 괄호·인용부호·중점(·)류는 OCR 이 다르게 읽는 일이 잦아 전부 제거
      (커브드 인용부호 “”‘’와 세로 구분선 | 도 같은 이유로 포함 — 003 'NH Benefit | 날짜',
      002 '"1억원까지"' 실측: 원본이 이 문자들을 쓰는데 골드가 직선 인용부호로 타이핑)
    - 마침표는 숫자 사이(소수점·날짜)만 보존, 문장부호/중점 대용은 제거
      (예: '예·적금' 을 OCR 이 '예.적금' 으로 읽어도 동일 취급)
    - 원문자(①②③)는 일반 숫자로 치환해 비교 (001 TEENZ 목록·올원 '⑦번동의서' 실측:
      VLM 이 원문자를 정확히 읽어도 골드가 일반 숫자로 타이핑하면 표기차만으로 불일치)
    """
    text = re.sub(
        r"[\s,~:：%()\[\]{}'\"`´『』「」〈〉<>【】·※*ㆍ■□|“”‘’]", "", str(text)
    )
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", "", text)  # 숫자 사이가 아닌 마침표 제거
    # 이모지·깨진 문자(U+FFFD)는 장식 — 골드 타이핑/추출 경로에 따라 달라지므로 제거
    text = re.sub(r"[\U00010000-\U0010FFFF☀-➿️‍�]", "", text)
    text = text.translate(_CIRCLED_TO_DIGIT)
    text = text.lower().replace("–", "-").replace("‧", "")
    # 하이픈은 숫자 사이만 보존 (심의필 2026-0000 구분 유지).
    # 그 외 위치의 하이픈은 물결(~) OCR 오인식과 섞이므로 제거
    return re.sub(r"(?<!\d)-|-(?!\d)", "", text)


# 통과했지만 정본(lines)이 아니라 VLM 통독 후보에서만 나온 경우의 전용 표식.
# (다른 지표도 통과 행에 비고를 달기 때문에 '비고 있음'으로 세면 오집계된다.)
CAND_ONLY = "VLM 통독 후보로만 회수 (정본 lines 에는 없음)"
# 파싱은 됐는데(out/json) STAGE_3 입력(out/llm_view)까지 못 간 경우.
# 장식예시(2a) 격리가 대표 원인 — 격리는 '보관'이지만 LLM 관점에선 삭제와 같다.
# 채점 대상이 out/json 이라 이 유실은 지표에 안 잡히므로 별도로 표시한다.
VIEW_DROP = "파싱됨 but STAGE_3 입력(llm_view)에서 제외됨"


def view_norm_by_page(view: dict | None) -> dict[int, str]:
    """llm_view(STAGE_3 페이로드)의 페이지별 정규화 텍스트 — 정본+후보+미배정 전부."""
    if not view:
        return {}
    out: dict[int, str] = {}
    for page in view.get("pages", []):
        parts: list[str] = []
        for sec in page.get("sections", []):
            for r in sec.get("regions", []):
                parts.append(r.get("text") or "")
                parts.append(r.get("vlm_reading") or "")
        parts.append(page.get("unassigned") or "")
        out[page.get("page_number")] = norm("".join(parts))
    return out


def section_match(gold_box: list, pred_box: list) -> bool:
    """섹션 박스 매칭 — 겹침 / 작은 쪽 면적 ≥ 0.5.

    (골드는 섹션 전체 범위, 예측은 텍스트 라인 합집합이라 더 작은 경우가
    일반적이므로 골드 면적 기준만 쓰면 부당하게 탈락한다.)
    """
    ix = max(0, min(gold_box[2], pred_box[2]) - max(gold_box[0], pred_box[0]))
    iy = max(0, min(gold_box[3], pred_box[3]) - max(gold_box[1], pred_box[1]))
    inter = ix * iy
    area_g = max(1, (gold_box[2] - gold_box[0]) * (gold_box[3] - gold_box[1]))
    area_p = max(1, (pred_box[2] - pred_box[0]) * (pred_box[3] - pred_box[1]))
    return inter / min(area_g, area_p) >= 0.5


def eval_file(gold: dict, parsed: dict, view: dict | None = None) -> dict:
    rows = []  # (지표, 대상, 성공여부, 비고)
    # 분류
    for key in ("product_group", "ad_type"):
        g = gold.get(key)
        if g:
            ok = parsed.get(key) == g
            rows.append(("분류", f"{key}={g}", ok, f"예측={parsed.get(key)}"))

    parsed_pages = {p["page_no"]: p for p in parsed["pages"]}
    view_norms = view_norm_by_page(view)
    for gpage in gold.get("pages", []):
        pno = gpage["page_no"]
        ppage = parsed_pages.get(pno)
        if not ppage:
            rows.append(("페이지", f"p{pno}", False, "파싱 결과에 페이지 없음"))
            continue

        # 골드 박스 좌표계 → 파싱 캔버스 좌표계 스케일 (렌더 DPI 가 달라질 수 있음)
        gc = gpage.get("canvas")
        sx = sy = 1.0
        if gc and ppage.get("canvas_w") and ppage.get("canvas_h"):
            sx = ppage["canvas_w"] / gc[0]
            sy = ppage["canvas_h"] / gc[1]

        # 1) 섹션 매칭
        pred_secs = ppage.get("sections", [])
        for gs in gpage.get("sections", []) or []:
            cands = [s for s in pred_secs if s["section_type"] == gs["type"]]
            matched = False
            gbox = gs.get("box")
            if gbox:
                gbox = [gbox[0] * sx, gbox[1] * sy, gbox[2] * sx, gbox[3] * sy]
            if gbox and any(c.get("bbox") for c in cands):
                matched = any(
                    c.get("bbox") and section_match(gbox, c["bbox"])
                    for c in cands
                )
            else:
                matched = bool(cands)  # bbox 없는 골드(HWP 등)는 타입 존재만
            rows.append(
                ("섹션", f"p{pno} {gs['type']}#{gs.get('no', 1)}", matched,
                 "" if matched else "동일 타입·위치의 예측 섹션 없음")
            )

        # 2) must_contain — 정본(lines)과 VLM 통독 후보(vlm_reading)를 두 층으로 본다.
        #    B안(§6)에서 통독은 region.lines 를 덮지 않고 후보로만 붙으므로, 정본만
        #    훑으면 실제로 회수된 문구가 미회수로 잡힌다(002 '최고 연 7.1%' 실측:
        #    p1_r008 vlm_reading 에 있고 STAGE_3 도 그 값을 채택했는데 미회수로 집계됨).
        #    회수 인정은 두 층의 합집합, 다만 어느 층에서 나왔는지는 구분해 기록한다.
        all_lines = [
            l["text"] for r in ppage["regions"] for l in r["lines"]
        ] + [l["text"] for l in ppage.get("unassigned_lines", [])]
        line_norms = [norm(t) for t in all_lines]
        page_norm = "".join(line_norms)
        region_norms = [
            norm(" ".join(l["text"] for l in r["lines"])) for r in ppage["regions"]
        ]
        cand_norms = [
            norm(r["vlm_reading"]) for r in ppage["regions"] if r.get("vlm_reading")
        ]

        def found_in(n: str, line_norms=line_norms, region_norms=region_norms,
                     page_norm=page_norm, cand_norms=cand_norms) -> str | None:
            """회수 출처: 'lines'(정본) | 'vlm_reading'(통독 후보) | None(미회수)."""
            if (any(n in ln for ln in line_norms)
                    or any(n in rn for rn in region_norms)
                    or n in page_norm):
                return "lines"
            if any(n in cn for cn in cand_norms):
                return "vlm_reading"
            return None

        for sent in gpage.get("must_contain", []) or []:
            if not isinstance(sent, str):  # YAML 콜론 오파싱 방어
                sent = json.dumps(sent, ensure_ascii=False)
            n = norm(sent)
            if not n:  # 이모지·깨진 문자만 남은 항목은 채점 불가 — 스킵
                continue
            src = found_in(n)
            note = ""
            if src is None:
                note = "파싱 결과에 없음"
            elif src == "vlm_reading":
                note = CAND_ONLY
            # 파싱은 됐는데 STAGE_3 페이로드까지 못 갔으면(격리 등) 별도 표시 —
            # 회수 자체는 성공이므로 실패로 세지 않고, 유실 경로만 드러낸다.
            if src is not None and pno in view_norms and n not in view_norms[pno]:
                note = VIEW_DROP
            rows.append(("문장", f"p{pno} {sent[:38]!r}", src is not None, note))

        # 3) 필드 지표는 여기서 재지 않는다 (2026-07-28).
        #    ⑥-4(파싱 단계 필드추출)를 없애고 필드를 STAGE_3 하나로 일원화했으므로,
        #    필드 품질은 out/extracted 를 보는 tools/verify_extract.py 가 단일 창구다.
        #    두 채점기가 서로 다른 산출물을 '필드'라는 같은 이름으로 재던 것이 혼선의
        #    원인이었다 — STAGE_3 를 고쳐도 이 지표는 안 움직였다(실측).
        #    이 파일은 파싱 품질(분류·섹션·문장)만 책임진다.
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument(
        "--src", type=Path, default=ROOT / "out",
        help="채점할 산출물 폴더 (json/·llm_view/ 를 가진 out 디렉터리). 실행본 비교용",
    )
    args = parser.parse_args()
    src = args.src if args.src.is_absolute() else ROOT / args.src

    report = ["# 골드셋 채점 리포트\n"]
    grand = {}
    for gf in sorted((ROOT / "gold").glob("*.yaml")):
        if args.only and args.only not in gf.stem:
            continue
        jf = src / "json" / (gf.stem + ".json")
        if not jf.exists():
            print(f"⚠ {gf.stem}: 파싱 결과 없음 — 먼저 run_nhdata.py 실행")
            continue
        gold = yaml.safe_load(gf.read_text(encoding="utf-8"))
        parsed = json.loads(jf.read_text(encoding="utf-8"))
        vf = src / "llm_view" / (gf.stem + ".json")
        view = json.loads(vf.read_text(encoding="utf-8")) if vf.exists() else None
        rows = eval_file(gold, parsed, view)

        print(f"\n===== {gf.stem}")
        report.append(f"\n## {gf.stem}\n")
        by_metric: dict[str, list] = {}
        for metric, target, ok, note in rows:
            by_metric.setdefault(metric, []).append((target, ok, note))
        for metric, items in by_metric.items():
            passed = sum(1 for _, ok, _ in items if ok)
            # 정본에는 없고 통독 후보로만 회수된 건 — 통과지만 정본 품질 문제로 따로 센다
            cand_only = sum(1 for _, ok, note in items if ok and note == CAND_ONLY)
            view_drop = sum(1 for _, ok, note in items if ok and note == VIEW_DROP)
            grand.setdefault(metric, [0, 0, 0, 0])
            grand[metric][0] += passed
            grand[metric][1] += len(items)
            grand[metric][2] += cand_only
            grand[metric][3] += view_drop
            marks = []
            if cand_only:
                marks.append(f"후보로만 {cand_only}")
            if view_drop:
                marks.append(f"LLM입력 누락 {view_drop}")
            tail = f" (그중 {', '.join(marks)})" if marks else ""
            print(f"  {metric}: {passed}/{len(items)}{tail}")
            report.append(f"- **{metric}**: {passed}/{len(items)}{tail}")
            for target, ok, note in items:
                if not ok:
                    print(f"    ✗ {target}  ({note})")
                    report.append(f"    - ✗ {target} — {note}")
                elif note in (CAND_ONLY, VIEW_DROP):
                    mark = "△" if note == CAND_ONLY else "▲"
                    print(f"    {mark} {target}  ({note})")
                    report.append(f"    - {mark} {target} — {note}")

    print("\n===== 종합")
    report.append("\n## 종합\n")
    for metric, (p, t, c, v) in grand.items():
        pct = 100 * p / t if t else 0
        marks = []
        if c:
            marks.append(f"{c}건은 VLM 통독 후보로만 회수(정본 미포함)")
        if v:
            marks.append(f"{v}건은 파싱됐으나 STAGE_3 입력에서 제외")
        tail = f" — 그중 {', '.join(marks)}" if marks else ""
        print(f"  {metric}: {p}/{t} ({pct:.0f}%){tail}")
        report.append(f"- {metric}: {p}/{t} ({pct:.0f}%){tail}")
    out = ROOT / "out" / "eval_report.md"
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"\n리포트: {out}")


if __name__ == "__main__":
    main()
