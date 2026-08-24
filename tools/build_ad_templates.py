# -*- coding: utf-8 -*-
"""농협 광고 템플릿 md → 라벨 사전(JSON) 생성.

  입력  nh-data/pilot-data-set/광고 템플릿(근거규정x, 필수 여부).md   (농협 제공, gitignore)
  출력  src/nh_parsing/templates/ad_templates.json

**이 팩은 판정 도구가 아니라 라벨 사전이다.**
파싱 결과의 각 줄에 "이건 `금리` 항목이다" 같은 이름표를 붙이는 데 쓴다.
`필수여부`(O/△)와 `기재요령`은 **다음 단계(심의)가 조회할 정적 근거**로 함께 담되,
실행 결과에는 `template_id` + `gubun` 만 남긴다 — 조문·규칙 텍스트를 산출물마다
복사해 넣지 않는다.

**범위**: 예금성·대출성·카드 12종만. 투자성 7종은 제외한다(2026-08-24 결정,
실데이터 샘플 0건이라 검증할 방법이 없다).

손으로 쓰지 않고 생성하는 이유: 농협이 템플릿을 갱신하면 다시 돌리면 된다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_MD = ROOT / "nh-data/pilot-data-set/광고 템플릿(근거규정x, 필수 여부).md"
OUT_JSON = ROOT / "src/nh_parsing/templates/ad_templates.json"

# 이번 범위. 접두어로 고른다.
IN_SCOPE = ("대출성상품-", "예금성상품-", "카드상품-")

# 예시문구에 이 패턴이 있으면 "값을 뽑아야 하는" 변수형이다.
# 농협 템플릿은 숫자 자리를 0 으로 채워 둔다 (0000, 0.00%, 0만원 …).
PLACEHOLDER = re.compile(r"0000|0\.00|0만원|0년|0개월|0등급|00-0000|0%|000")


# ─────────────────────────────── HTML 표 읽기 ───────────────────────────────
# 템플릿 md 는 HTML 표다. 중첩 표(상환방법 안의 표)와 rowspan 이 있어
# 정규식 한 방으로는 못 읽는다. 깊이를 세면서 최상위 행/칸만 집는다.

def _text(html: str) -> str:
    html = re.sub(r"<br\s*/?>", "\n", html)
    html = re.sub(r"<img[^>]*>", "[이미지]", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = unicodedata.normalize("NFC", html)
    return re.sub(r"[ \t]+", " ", html).strip()


def _outer_rows(block: str) -> list[str]:
    """최상위 <tr> 만. 중첩 표 안의 <tr> 은 바깥 행의 내용으로 남긴다."""
    rows: list[str] = []
    depth = 0
    cur: list[str] | None = None
    for tok in re.split(r"(<table[^>]*>|</table>|<tr[^>]*>|</tr>)", block):
        if tok.startswith("<table"):
            depth += 1
            if depth > 1 and cur is not None:
                cur.append(tok)
            continue
        if tok == "</table>":
            depth -= 1
            if depth >= 1 and cur is not None:
                cur.append(tok)
            continue
        if depth == 1 and tok.startswith("<tr"):
            cur = []
            continue
        if depth == 1 and tok == "</tr>":
            if cur is not None:
                rows.append("".join(cur))
                cur = None
            continue
        if cur is not None:
            cur.append(tok)
    return rows


def _cells(row: str) -> list[tuple[int, str]]:
    """최상위 td/th 만. (rowspan, 내용) 목록."""
    out: list[tuple[int, str]] = []
    depth = 0
    cur: list[str] | None = None
    attrs = ""
    for tok in re.split(r"(<table[^>]*>|</table>|<t[dh][^>]*>|</t[dh]>)", row):
        if tok.startswith("<table"):
            depth += 1
        elif tok == "</table>":
            depth -= 1
        if depth == 0 and re.match(r"<t[dh][^>]*>", tok):
            cur, attrs = [], tok
            continue
        if depth == 0 and re.match(r"</t[dh]>", tok):
            if cur is not None:
                m = re.search(r'rowspan="(\d+)"', attrs)
                out.append((int(m.group(1)) if m else 1, "".join(cur)))
                cur = None
            continue
        if cur is not None:
            cur.append(tok)
    return out


def _grid(block: str) -> list[list[str]]:
    """rowspan 을 아래 행으로 펼쳐 직사각형 격자로 만든다."""
    rows = _outer_rows(block)
    if not rows:
        return []
    grid: list[list[str]] = []
    carry: dict[int, tuple[int, str]] = {}     # 열 → (남은 행수, 내용)
    for raw in rows[1:]:                       # 0행은 헤더
        cs = _cells(raw)
        line: list[str] = []
        col = k = 0
        while col < 5:
            if col in carry and carry[col][0] > 0:
                remain, txt = carry[col]
                line.append(txt)
                carry[col] = (remain - 1, txt)
                col += 1
                continue
            if k >= len(cs):
                break
            span, html = cs[k]
            k += 1
            txt = _text(html)
            if span > 1:
                carry[col] = (span - 1, txt)
            line.append(txt)
            col += 1
        grid.append(line)
    return grid


# ─────────────────────────────── 행 해석 ───────────────────────────────

def _split_row(cells_: list[str]) -> tuple[str, str, str, str]:
    """한 행에서 (구분, 예시문구, 필수여부, 기재요령)을 골라낸다.

    열 위치가 템플릿마다 다르다 — 예금성은 예시문구가 colspan=2 이고,
    `예금자보호` 행은 로고 이미지 칸이 하나 더 붙는다. 그래서 위치가 아니라
    **값의 생김새**로 고른다: 'O'/'△' 인 칸이 필수여부, 그 뒤가 기재요령.
    """
    gubun = _norm_gubun(cells_[0]) if cells_ else ""
    req_at = next((i for i, c in enumerate(cells_) if c.strip() in ("O", "△")), None)
    if req_at is None:
        body = [c for c in cells_[1:] if c.strip()]
        return gubun, max(body, key=len) if body else "", "", ""
    req = cells_[req_at].strip()
    mid = [c for c in cells_[1:req_at] if c.strip() and c.strip() != "[이미지]"]
    example = max(mid, key=len) if mid else ""
    rule = cells_[req_at + 1].strip() if req_at + 1 < len(cells_) else ""
    return gubun, example, req, rule


def _norm_gubun(s: str) -> str:
    """`모집인\n유의사항` 처럼 <br> 로 접힌 항목명을 한 줄로 편다."""
    return re.sub(r"\s+", " ", s).strip()


def _rules(text: str) -> list[str]:
    """기재요령 칸을 규칙 목록으로. 줄마다 하나씩, 앞머리 '-' 제거."""
    out = []
    for part in text.split("\n"):
        p = part.strip().lstrip("-").strip()
        if len(p) > 2:
            out.append(p)
    return out


# ─────────────────────────────── 생성 ───────────────────────────────

def build(md_path: Path) -> dict:
    raw = md_path.read_text(encoding="utf-8")
    chunks = re.split(r"\*\*\[(.+?)\]\*\*", raw)
    pairs = [(chunks[i].strip(), chunks[i + 1]) for i in range(1, len(chunks), 2)]

    pool: dict[str, dict] = {}      # 정형 문구 공통 풀 (본문 → PXXX)
    pool_by_text: dict[str, str] = {}
    templates: dict[str, dict] = {}
    skipped: list[str] = []

    for name, block in pairs:
        if not name.startswith(IN_SCOPE):
            skipped.append(name)
            continue
        group = name.split("상품-")[0] + "성" if "성상품-" in name else "카드"
        group = {"대출성": "대출성", "예금성": "예금성", "카드": "카드"}[group.replace("성성", "성")]
        subtype = name.split("-", 1)[1]

        items: dict[str, dict] = {}   # 구분 → 항목 (같은 구분의 여러 행을 묶는다)
        for cells_ in _grid(block):
            if not cells_:
                continue
            gubun, example, req, rule = _split_row(cells_)
            if not gubun or not example:
                continue
            it = items.setdefault(gubun, {
                "gubun": gubun,
                "requirement": "",       # 항목 단위 집계 (아래에서 채운다)
                "match_mode": "",
                "entries": [],           # 문구 단위. 필수여부를 여기 보존한다
                "writing_rules": [],
            })
            # 한 항목 안에서도 문구마다 필수여부가 다르다 — 예: `유의사항` 5줄 중
            # 3줄은 O, '만기 전 해지…'와 '생성형 AI…'는 △. 항목 단위로 뭉치면
            # 그 차이가 사라져서 다음 단계가 판정할 수 없다. 문구마다 남긴다.
            if PLACEHOLDER.search(example):
                entry = {"kind": "변수", "example": example, "requirement": req}
            else:
                pid = pool_by_text.get(example)
                if pid is None:
                    pid = f"P{len(pool) + 1:03d}"
                    pool_by_text[example] = pid
                    pool[pid] = {"text": example, "used_by": []}
                if name not in pool[pid]["used_by"]:
                    pool[pid]["used_by"].append(name)
                entry = {"kind": "정형", "phrase_id": pid, "requirement": req}
            if entry not in it["entries"]:
                it["entries"].append(entry)
            for r in _rules(rule):
                if r not in it["writing_rules"]:
                    it["writing_rules"].append(r)

        for it in items.values():
            kinds = {e["kind"] for e in it["entries"]}
            reqs = {e["requirement"] for e in it["entries"] if e["requirement"]}
            # 항목 단위 집계: 하나라도 필수면 그 항목은 필수로 본다.
            # 원본 값은 entries 에 그대로 있으니 이 집계는 편의용이다.
            it["requirement"] = "O" if "O" in reqs else ("△" if reqs else "")
            it["match_mode"] = (
                "혼합" if kinds == {"정형", "변수"}
                else "정형" if kinds == {"정형"}
                else "변수" if kinds == {"변수"}
                else "미상"
            )

        templates[name] = {
            "product_group": group,
            "subtype": subtype,
            "item_count": len(items),
            "items": list(items.values()),
        }

    catalog: dict[str, dict] = {}
    for name, t in templates.items():
        for it in t["items"]:
            c = catalog.setdefault(it["gubun"], {"gubun": it["gubun"], "templates": []})
            c["templates"].append(name)

    return {
        "version": "1.0",
        "generated_from": md_path.name,
        "purpose": "라벨 사전 — 파싱된 줄에 항목명을 붙이는 용도. 판정은 다음 단계 몫이다.",
        "scope": {
            "included": sorted({t["product_group"] for t in templates.values()}),
            "excluded": skipped,
            "excluded_reason": "2026-08-24 범위 결정. 투자성은 실데이터 샘플 0건이라 검증 불가.",
        },
        "gubun_catalog": catalog,
        "phrase_pool": pool,
        "templates": templates,
    }


def report(pack: dict) -> None:
    t = pack["templates"]
    print(f"템플릿 {len(t)}종  ·  고유 구분 {len(pack['gubun_catalog'])}종  "
          f"·  정형 문구 풀 {len(pack['phrase_pool'])}개")
    print(f"제외 {len(pack['scope']['excluded'])}종: {pack['scope']['excluded_reason']}\n")

    modes, ereq = Counter(), Counter()
    print(f"{'템플릿':<32} {'항목':>4} {'문구':>4} {'필수':>4} {'조건':>4}  match")
    for name, tpl in t.items():
        entries = [e for i in tpl["items"] for e in i["entries"]]
        o = sum(1 for e in entries if e["requirement"] == "O")
        a = sum(1 for e in entries if e["requirement"] == "△")
        ereq["O"] += o
        ereq["△"] += a
        m = Counter(i["match_mode"] for i in tpl["items"])
        for k, v in m.items():
            modes[k] += v
        desc = " ".join(f"{k}{v}" for k, v in sorted(m.items()))
        print(f"{name:<32} {tpl['item_count']:>4} {len(entries):>4} {o:>4} {a:>4}  {desc}")

    print(f"\n항목 매칭 방식 합계: " + " · ".join(f"{k} {v}" for k, v in modes.most_common()))
    print(f"문구 단위 필수여부: O {ereq['O']} · △ {ereq['△']} · 합 {sum(ereq.values())}")

    shared = [(len(v["used_by"]), k, v["text"]) for k, v in pack["phrase_pool"].items()]
    shared.sort(reverse=True)
    print(f"\n여러 템플릿이 공유하는 정형 문구 상위 8개 (중복 정의를 막은 지점):")
    for n, pid, txt in shared[:8]:
        if n > 1:
            print(f"  {n}종  {pid}  {txt[:66]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=SRC_MD)
    ap.add_argument("--out", type=Path, default=OUT_JSON)
    args = ap.parse_args()

    if not args.src.is_file():
        print(f"입력이 없다: {args.src}")
        return 2
    pack = build(args.src)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    report(pack)
    print(f"\n→ {args.out.relative_to(ROOT)}  ({args.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
