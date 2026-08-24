# -*- coding: utf-8 -*-
"""통합 JSON 검수 화면 — 광고 그림 위에 라벨을 얹어 눈으로 대조한다.

`tools/run_ad_label.py` 가 낸 `out_ad/json/*.json` 과 `out_ad/pages/*.jpg` 를 읽어
파일 하나짜리 HTML 을 만든다(그림은 base64 로 박아 넣어 어디서 열어도 깨지지 않는다).

무엇을 보이나
  · 왼쪽  원본 위에 영역 상자 — 초록=템플릿 항목 붙음, 파랑=문구만 맞음, 회색=라벨 없음
  · 오른쪽 항목표(문구별 찾음/못찾음 + 접두어 커버리지) + **모든 줄** 목록

회색 상자와 흐린 줄을 일부러 남긴다. 이 단계의 목표는 '라벨을 많이 붙이는 것'이 아니라
'라벨이 안 붙은 텍스트도 빠짐없이 실려 있는 것'이라, 안 붙은 쪽이 보여야 확인이 된다.

    uv run python tools/make_ad_review.py
"""

from __future__ import annotations

import argparse
import base64
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CSS = """
:root { --ink:#1a1a1a; --dim:#8a8a8a; --line:#e3e3e3; }
* { box-sizing: border-box; }
body { font: 14px/1.6 -apple-system, "Malgun Gothic", sans-serif; color: var(--ink);
       margin: 0; padding: 24px 28px; background: #fafafa; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 0; }
.sub { color: var(--dim); font-size: 13px; }
.doc { background: #fff; border: 1px solid var(--line); border-radius: 8px;
       margin: 20px 0; padding: 18px 20px; }
.head { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: baseline;
        border-bottom: 1px solid var(--line); padding-bottom: 12px; margin-bottom: 14px; }
.kv { font-size: 13px; } .kv b { font-weight: 600; }
.cols { display: grid; grid-template-columns: minmax(320px, 44%) 1fr; gap: 22px; }
@media (max-width: 1100px) { .cols { grid-template-columns: 1fr; } }
.pagewrap { position: sticky; top: 12px; }
.canvas { position: relative; line-height: 0; margin-bottom: 10px; }
.canvas img { width: 100%; border: 1px solid var(--line); }
.box { position: absolute; border: 2px solid #b9b9b9; border-style: dashed;
       background: rgba(0,0,0,0.02); cursor: pointer; }
.box.g { border-color: #1f9d55; border-style: solid; background: rgba(31,157,85,0.10); }
.box.p { border-color: #2b6cb0; border-style: solid; background: rgba(43,108,176,0.08); }
.box.on { outline: 3px solid #e53e3e; z-index: 5; }
.box .tag { position: absolute; top: -17px; left: -2px; font: 10px/1.4 monospace;
            background: #1f9d55; color: #fff; padding: 0 4px; white-space: nowrap; }
.box.p .tag { background: #2b6cb0; } .box .tag.none { display: none; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 16px; }
th, td { border: 1px solid var(--line); padding: 4px 7px; text-align: left;
         vertical-align: top; }
th { background: #f4f4f4; font-weight: 600; }
td.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.ok { color: #1f9d55; } .no { color: #c53030; } .na { color: var(--dim); }
.lines { border: 1px solid var(--line); max-height: 640px; overflow-y: auto; }
.ln { display: grid; grid-template-columns: 96px 1fr; gap: 8px; padding: 3px 8px;
      border-bottom: 1px solid #f2f2f2; font-size: 13px; cursor: pointer; }
.ln:hover { background: #fffbe6; }
.ln.on { background: #fff0f0; }
.ln.plain { color: #9a9a9a; }
.ln .rid { font: 11px/1.7 monospace; color: var(--dim); }
.chip { display: inline-block; font-size: 11px; padding: 0 5px; border-radius: 3px;
        margin-left: 5px; background: #e6f4ec; color: #1f6f45; white-space: nowrap; }
.chip.r { background: #e8effa; color: #24557f; }
.legend { font-size: 12px; color: var(--dim); margin: 6px 0 12px; }
.legend i { display:inline-block; width:11px; height:11px; margin:0 3px 0 12px;
            vertical-align:-1px; border:2px solid #b9b9b9; border-style:dashed; }
.legend i.g { border-color:#1f9d55; border-style:solid; background:rgba(31,157,85,.15); }
.legend i.p { border-color:#2b6cb0; border-style:solid; background:rgba(43,108,176,.12); }
"""

JS = """
document.addEventListener('click', function (e) {
  var box = e.target.closest('.box');
  var ln = e.target.closest('.ln');
  var rid = box ? box.dataset.rid : (ln ? ln.dataset.rid : null);
  if (!rid) return;
  var doc = (box || ln).closest('.doc');
  doc.querySelectorAll('.on').forEach(function (n) { n.classList.remove('on'); });
  doc.querySelectorAll('[data-rid="' + rid + '"]').forEach(function (n) {
    n.classList.add('on');
  });
  if (box) {
    var first = doc.querySelector('.ln[data-rid="' + rid + '"]');
    if (first) first.scrollIntoView({block: 'nearest'});
  }
});
"""


def _img(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _boxes(page: dict) -> str:
    w, h = page.get("canvas_w") or 1, page.get("canvas_h") or 1
    out = []
    for region in page["regions"]:
        bbox = region.get("bbox")
        if not bbox:
            continue
        cls = "g" if region.get("gubun") else ("p" if region.get("phrase_hits") else "")
        tag = region.get("gubun") or (
            region["phrase_hits"][0]["gubun"] if region.get("phrase_hits") else ""
        )
        style = (f"left:{bbox[0] / w:.4%};top:{bbox[1] / h:.4%};"
                 f"width:{(bbox[2] - bbox[0]) / w:.4%};height:{(bbox[3] - bbox[1]) / h:.4%}")
        title = f'{region["region_id"]} · {tag or "라벨 없음"}'
        out.append(
            f'<div class="box {cls}" data-rid="{region["region_id"]}" style="{style}" '
            f'title="{html.escape(title)}">'
            f'<span class="tag{" none" if not tag else ""}">{html.escape(tag)}</span></div>'
        )
    return "".join(out)


def _items_table(doc: dict) -> str:
    rows = []
    for item in doc["template_items"]:
        # 한 항목이 문구 여러 개를 갖는다(유의사항은 5개). 구분 칸을 rowspan 으로 묶어
        # 어디까지가 같은 항목인지 보이게 한다 — 빈 칸으로 두면 결함처럼 보인다.
        span = len(item["fixed_phrases"]) + len(item["variable_entries"]) or 1
        head = f'<td rowspan="{span}">{html.escape(item["gubun"])}</td>'
        for phrase in item["fixed_phrases"]:
            found = phrase["found"]
            mark = '<span class="ok">찾음</span>' if found else '<span class="no">못찾음</span>'
            extra = "" if found else f'{phrase.get("prefix_coverage") or 0:.2f}'
            rows.append(
                f'<tr>{head}<td>{phrase.get("requirement") or ""}</td>'
                f'<td>{phrase["phrase_id"]}</td><td>{mark}</td>'
                f'<td class="n">{extra}</td></tr>'
            )
            head = ""
        for v in item["variable_entries"]:
            rows.append(
                f'<tr>{head}<td>{v.get("requirement") or ""}</td><td class="na">변수형</td>'
                f'<td class="na">미착수</td><td class="n"></td></tr>'
            )
            head = ""
    if not rows:
        return '<p class="na">템플릿이 판단불가라 항목 대조를 하지 않았다.</p>'
    return (
        '<table><thead><tr><th>구분</th><th>필수</th><th>문구</th><th>정형문구 대조</th>'
        '<th title="못 찾았을 때, 문구 앞에서부터 몇 %가 광고에 연속으로 있었나">접두어</th>'
        '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
    )


def _lines_html(doc: dict) -> str:
    gubun_of = {
        r["region_id"]: r.get("gubun")
        for p in doc["pages"] for r in p["regions"]
    }
    out = []
    for page in doc["pages"]:
        for region in page["regions"]:
            for line in region["lines"]:
                out.append(_line_row(line, region["region_id"], gubun_of))
        for line in page["unassigned_lines"]:
            out.append(_line_row(line, None, gubun_of))
    return '<div class="lines">' + "".join(out) + "</div>"


def _line_row(line: dict, rid: str | None, gubun_of: dict) -> str:
    chips = "".join(
        f'<span class="chip">{html.escape(l["gubun"])} · {l["phrase_id"]}</span>'
        for l in line["labels"]
    )
    if not chips and rid and gubun_of.get(rid):
        chips = f'<span class="chip r">영역: {html.escape(gubun_of[rid])}</span>'
    plain = "" if chips else " plain"
    return (
        f'<div class="ln{plain}" data-rid="{rid or ""}">'
        f'<span class="rid">{rid or "미배정"}</span>'
        f'<span>{html.escape(line["text"]) or "&nbsp;"}{chips}</span></div>'
    )


def _doc_html(doc: dict, pages_dir: Path) -> str:
    cls, tpl, comp = doc["classification"], doc["template"], doc["completeness"]
    status = tpl["status"]
    tpl_txt = tpl["template_id"] or "판단불가"
    head = (
        f'<div class="head"><h2>{html.escape(doc["source_file"])}</h2>'
        f'<span class="kv">분류 <b>{cls["product_group"]}</b> / {cls["ad_type"]} '
        f'/ 상품명 {cls["product_name_shown"] or "판단불가"}</span>'
        f'<span class="kv">템플릿 <b>{html.escape(tpl_txt)}</b> ({status})</span>'
        f'<span class="kv">줄 {comp["lines_total"]} · 라벨닿음 {comp["lines_covered"]} '
        f'({comp["lines_covered"] / max(1, comp["lines_total"]):.0%})</span>'
        f'<span class="sub">{html.escape(tpl.get("note") or "")}</span></div>'
    )
    canvases = []
    for page in doc["pages"]:
        img = pages_dir / f'{doc["doc_id"]}_p{page["page_no"]}.jpg'
        if not img.is_file():
            continue
        canvases.append(
            f'<div class="canvas"><img src="data:image/jpeg;base64,{_img(img)}">'
            f'{_boxes(page)}</div>'
        )
    legend = ('<div class="legend">상자 <i class="g"></i>템플릿 항목(VLM) '
              '<i class="p"></i>정형문구만 일치 <i></i>라벨 없음 '
              '— 상자나 줄을 누르면 서로 짚어 준다</div>')
    return (
        f'<div class="doc">{head}<div class="cols">'
        f'<div class="pagewrap">{"".join(canvases)}{legend}</div>'
        f'<div>{_items_table(doc)}{_lines_html(doc)}</div>'
        f'</div></div>'
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", type=Path, default=ROOT / "out_ad")
    ap.add_argument("--out", type=Path, default=ROOT / "out_ad" / "review.html")
    args = ap.parse_args()

    files = sorted((args.src / "json").glob("*.json"))
    if not files:
        raise SystemExit(f"통합 JSON 이 없다 — run_ad_label.py 를 먼저 돌려라: {args.src}")

    docs = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    total = sum(d["completeness"]["lines_total"] for d in docs)
    covered = sum(d["completeness"]["lines_covered"] for d in docs)
    body = "".join(_doc_html(d, args.src / "pages") for d in docs)
    args.out.write_text(
        "<!doctype html><meta charset='utf-8'>"
        "<title>광고 템플릿 라벨 검수</title>"
        f"<style>{CSS}</style>"
        f"<h1>광고 템플릿 라벨 검수 — {len(docs)}건</h1>"
        f'<p class="sub">줄 {total}개 중 라벨이 닿은 줄 {covered}개 '
        f"({covered / max(1, total):.0%}). 나머지도 전부 아래에 실려 있다.</p>"
        f"{body}<script>{JS}</script>",
        encoding="utf-8",
    )
    print(f"→ {args.out}  ({args.out.stat().st_size / 1e6:.1f}MB, {len(docs)}건)")


if __name__ == "__main__":
    main()
