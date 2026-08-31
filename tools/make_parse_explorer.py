# -*- coding: utf-8 -*-
"""영역별 파싱 상세 열람 화면 — 통합 JSON 전건을 영역 단위 토글로 펼쳐 본다.

`tools/make_ad_review.py` 와 목적이 다르다. 그쪽은 **라벨이 잘 붙었나**를 그림 위
상자로 보는 검수용이고, 이건 **한 영역에서 무슨 일이 있었나**를 보는 열람용이다:
영역마다 OCR/디지털 정본 · 밴드 VLM 후보 · Reader/Judge shadow 근거 · 시인성
(글자 크기·굵기·색) · 라벨을 한자리에 모아 접었다 펼친다.

영역 순서는 **통합 JSON 에 실린 순서 그대로**다(레이아웃 검출 순서). 다음 단계
(벡터DB·RDB 엔진)가 이 파일을 읽으면 이 순서로 보게 되므로, 화면도 같은 순서여야
"넘어가는 것"과 "보는 것"이 어긋나지 않는다. 읽기순서(위→아래)로 다시 정렬하지
않는 이유가 그것이다.

그림은 base64 로 박지 않고 `pages/` 상대경로로 건다 — 93건을 통째로 담으면 파일이
수십 MB 가 되고, 이 화면은 `out_ad_full/` 안에서 여는 것을 전제로 한다.

    uv run python tools/make_parse_explorer.py                      # out_ad_full 전건
    uv run python tools/make_parse_explorer.py --only "13. 대출성상품"
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 출처 → (배지 글자, 색, 설명). 통합 JSON 에 실제로 나오는 세 값만 둔다
# (전수 93건: ocr 5119 · digital 2408 · vlm_sweep 362).
SOURCE_META = {
    "ocr":       ("O", "#2563eb", "OCR 이 픽셀에서 읽음"),
    "digital":   ("D", "#059669", "PDF 텍스트 레이어에서 그대로 추출"),
    "vlm_sweep": ("S", "#db2777", "OCR 이 못 읽어 VLM 이 건져옴"),
}

# 판독 관계 → (딱지, 등급). truncation.classify_reading 이 붙인 값.
RELATION = {
    "same":      ("표기 차이", "ok"),
    "head_drop": ("앞 항목명 생략", "ok"),
    "expanded":  ("정본보다 많이 읽음", "good"),
    "tail_cut":  ("뒷부분 잘림", "bad"),
    "diverged":  ("정본과 불일치", "bad"),
    "overflow":  ("다른 영역을 삼킴", "bad"),
}

CSS = """
:root {
  --bg:#f7f7f9; --card:#fff; --ink:#1c1a20; --dim:#77727f; --line:#e5e3e9;
  --soft:#f1f0f4; --accent:#c0186b; --ok:#059669; --bad:#c2410c; --warn:#b45309;
}
* { box-sizing:border-box; }
body { margin:0; padding:22px 26px 60px; background:var(--bg); color:var(--ink);
       font:14px/1.65 -apple-system,"Malgun Gothic","맑은 고딕",sans-serif; }
h1 { font-size:21px; margin:0 0 6px; }
.sub { color:var(--dim); font-size:13px; margin:0 0 4px; }
code,.mono { font:12px/1.5 "Consolas",monospace; }

/* 색인 */
.index { background:var(--card); border:1px solid var(--line); border-radius:8px;
         padding:14px 16px; margin:16px 0 22px; }
.index h2 { font-size:14px; margin:0 0 10px; }
.idxgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:2px 14px; }
.idxgrid a { font-size:12.5px; color:#2b4c7e; text-decoration:none; padding:1px 0;
             white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.idxgrid a:hover { text-decoration:underline; color:var(--accent); }

/* 문서 */
.doc { background:var(--card); border:1px solid var(--line); border-radius:8px;
       margin:14px 0; }
.doc > summary { padding:13px 16px; cursor:pointer; font-weight:600; font-size:15px;
                 display:flex; flex-wrap:wrap; gap:4px 14px; align-items:baseline; }
.doc > summary::marker { color:var(--accent); }
.doc[open] > summary { border-bottom:1px solid var(--line); }
.docbody { padding:16px; }
.kv { font-size:12.5px; font-weight:400; color:var(--dim); }
.kv b { color:var(--ink); font-weight:600; }

.cols { display:grid; grid-template-columns:minmax(280px,38%) 1fr; gap:20px; }
@media (max-width:1080px) { .cols { grid-template-columns:1fr; } }
.pagewrap { position:sticky; top:10px; align-self:start; }
.canvas { position:relative; line-height:0; margin-bottom:8px; }
.canvas img { width:100%; border:1px solid var(--line); }
.box { position:absolute; border:1.5px solid rgba(120,120,130,.55); }
.box.lab { border-color:var(--ok); background:rgba(5,150,105,.09); }
.box.on { border-color:var(--accent); border-width:3px; background:rgba(192,24,107,.14); z-index:4; }
.noimg { color:var(--dim); font-size:12.5px; padding:10px; background:var(--soft);
         border:1px dashed var(--line); border-radius:4px; }

/* 영역 토글 */
.rgn { border:1px solid var(--line); border-radius:5px; margin-bottom:6px; background:var(--card); }
.rgn > summary { padding:7px 10px; cursor:pointer; display:flex; flex-wrap:wrap;
                 gap:3px 8px; align-items:center; font-size:13px; }
.rgn > summary:hover { background:var(--soft); }
.rgn[open] > summary { border-bottom:1px solid var(--line); background:var(--soft); }
.seq { font:11px/1.4 "Consolas",monospace; color:var(--dim); min-width:22px; }
.rid { font:11.5px/1.4 "Consolas",monospace; color:#2b4c7e; }
.role { font-size:12px; color:var(--dim); }
.rgnbody { padding:9px 11px; display:flex; flex-direction:column; gap:11px; }

/* 배지 */
.chip { display:inline-block; font-size:11px; padding:0 5px; border-radius:3px;
        background:var(--soft); color:var(--dim); white-space:nowrap; }
.chip.gubun { background:#e6f4ec; color:#1f6f45; font-weight:600; }
.chip.none  { background:#f4f0f0; color:#9a8f8f; }
.chip.tbl   { background:#eef2fb; color:#2b4c7e; }
.chip.ok   { background:#e6f4ec; color:#1f6f45; }
.chip.good { background:#e6f4ec; color:#1f6f45; }
.chip.bad  { background:#fdeee6; color:var(--bad); font-weight:600; }
.chip.shadow { background:#e8eefc; color:#1d4ed8; font-weight:600; }
.tag { display:inline-block; width:15px; text-align:center; color:#fff; font-size:10px;
       font-weight:700; border-radius:2px; }

/* 줄 표 */
.blk { }
.blkhd { font-size:11.5px; color:var(--dim); letter-spacing:.04em; margin-bottom:4px;
         text-transform:uppercase; }
.lines { overflow-x:auto; }
table.ln { border-collapse:collapse; width:100%; font-size:12.5px; }
table.ln th, table.ln td { border-bottom:1px solid var(--soft); padding:3px 6px;
                           text-align:left; vertical-align:top; }
table.ln th { color:var(--dim); font-weight:500; font-size:11px; white-space:nowrap; }
table.ln td.c { text-align:center; white-space:nowrap; }
table.ln td.n { font:11.5px/1.5 "Consolas",monospace; white-space:nowrap; color:var(--dim); }
.sw { display:inline-block; width:9px; height:9px; border:1px solid #bbb; vertical-align:-1px;
      margin-right:3px; border-radius:1px; }
.big { font-weight:600; }

pre.txt { margin:0; padding:7px 9px; background:var(--soft); border-radius:4px;
          font:12px/1.6 "Consolas",monospace; white-space:pre-wrap; word-break:break-all; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
@media (max-width:760px) { .two { grid-template-columns:1fr; } }
.two > div > .blkhd { margin-bottom:3px; }

table.grid { border-collapse:collapse; font-size:12px; }
table.grid td { border:1px solid #c9c6d0; padding:3px 6px; }

.unassigned { border:1px dashed var(--warn); border-radius:5px; padding:8px 10px;
              background:#fffaf2; }
.legend { font-size:12px; color:var(--dim); margin-top:8px; line-height:1.9; }
"""

JS = """
// 영역 토글을 펼치면 왼쪽 그림의 같은 상자를 짚어 준다.
document.addEventListener('toggle', function (e) {
  var d = e.target;
  if (!d.classList || !d.classList.contains('rgn')) return;
  var doc = d.closest('.docbody');
  if (!doc) return;
  var box = doc.querySelector('.box[data-rid="' + d.dataset.rid + '"]');
  if (!box) return;
  if (d.open) { box.classList.add('on'); box.scrollIntoView({block:'nearest'}); }
  else { box.classList.remove('on'); }
}, true);
"""


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _boxes(page: dict) -> str:
    w = page.get("canvas_w") or 1
    h = page.get("canvas_h") or 1
    out = []
    for r in page.get("regions") or []:
        b = r.get("bbox")
        if not b:
            continue
        cls = " lab" if (r.get("template_labels") or {}).get("semantic") else ""
        style = (f"left:{b[0]/w:.3%};top:{b[1]/h:.3%};"
                 f"width:{(b[2]-b[0])/w:.3%};height:{(b[3]-b[1])/h:.3%}")
        out.append(f'<div class="box{cls}" data-rid="{_esc(r["region_id"])}" '
                   f'style="{style}" title="{_esc(r["region_id"])}"></div>')
    return "".join(out)


def _style_cell(style: dict | None) -> str:
    """시인성 칸. **디지털 텍스트(드래그로 잡히는 글자)에만** 값이 있다.

    PDF 텍스트 레이어를 읽을 때 폰트 크기·굵기·색이 같이 오기 때문이다. OCR·VLM 으로
    읽은 글자에서 시인성을 재는 로직은 아직 없다 — 값이 비어 있는 것은 결함이 아니라
    미구현이다(전수 93건: digital 2408줄 전부 있음 / ocr 5119·vlm_sweep 362 전부 없음).
    """
    if not style:
        return '<span class="chip none">—</span>'
    bits = []
    pt = style.get("size_pt")
    if pt is not None:
        lo, hi = style.get("size_pt_min"), style.get("size_pt_max")
        txt = f"{pt}pt" if lo == hi or lo is None else f"{lo}~{hi}pt"
        bits.append(f'<span class="{"big" if pt and pt >= 14 else ""}">{_esc(txt)}</span>')
    if style.get("bold"):
        bits.append('<b>굵음</b>')
    color = style.get("color")
    if color:
        bits.append(f'<span class="sw" style="background:{_esc(color)}"></span>'
                    f'<span class="mono">{_esc(color)}</span>')
    return " ".join(bits) or '<span class="chip none">—</span>'


def _lines_table(lines: list[dict]) -> str:
    rows = []
    for ln in lines:
        abbr, color, _ = SOURCE_META.get(ln.get("source"), ("?", "#888", ""))
        conf = ln.get("confidence")
        rows.append(
            f'<tr><td class="c"><span class="tag" style="background:{color}">{abbr}</span></td>'
            f'<td>{_esc(ln.get("text")) or "&nbsp;"}</td>'
            f'<td class="n">{f"{conf:.3f}" if isinstance(conf, (int, float)) else "—"}</td>'
            f'<td>{_style_cell(ln.get("style"))}</td>'
            f'<td class="n">{_esc(ln.get("bbox"))}</td></tr>'
        )
    return (
        '<div class="lines"><table class="ln"><thead><tr>'
        '<th title="O=OCR · D=디지털 텍스트 · S=VLM 회수">출처</th><th>텍스트</th>'
        '<th title="OCR 확신도. 디지털은 추정이 아니라 정본이라 값이 없다">확신</th>'
        '<th title="글자 크기·굵기·색. 드래그로 잡히는 디지털 텍스트에서만 나온다 — '
        'OCR·VLM 글자의 시인성 수집은 아직 미구현">시인성</th>'
        '<th>bbox</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
    )


def _line_rereads(lines: list[dict]) -> str:
    """라인 단위 VLM 재판독 후보. 영역 통독과 트리거가 달라 따로 보여야 한다."""
    got = [l for l in lines if (l.get("vlm_reading") or "").strip()]
    if not got:
        return ""
    rows = "".join(
        f'<tr><td>{_esc(l.get("text"))}</td><td>{_esc(l.get("vlm_reading"))}</td>'
        f'<td class="n">{_esc(l.get("vlm_reading_stage"))}</td></tr>'
        for l in got
    )
    return (
        '<div class="blk"><div class="blkhd">라인 단위 VLM 재판독 후보 '
        f'({len(got)}줄)</div><div class="lines"><table class="ln"><thead><tr>'
        '<th>정본</th><th>VLM 후보</th><th title="lowconf_reread=저신뢰 재판독 · '
        'sweep_dedupe=스윕 중복 정정">계기</th></tr></thead><tbody>'
        + rows + '</tbody></table></div></div>'
    )


def _region_vlm(r: dict) -> str:
    """영역 통독 후보 — 정본을 덮지 않고 나란히 둔다(B안)."""
    cand = (r.get("vlm_reading") or "").strip()
    if not cand:
        return ""
    ocr_txt = "\n".join(l.get("text") or "" for l in r.get("lines") or [])
    label, grade = RELATION.get(r.get("vlm_reading_relation") or "same", ("판정 없음", "ok"))
    return (
        '<div class="blk"><div class="blkhd">영역 VLM 통독 후보 '
        f'<span class="chip {grade}">{_esc(label)}</span> '
        f'<span class="chip">정밀도 {_esc(r.get("vlm_reading_score"))} · '
        f'커버리지 {_esc(r.get("vlm_reading_coverage"))}</span></div>'
        '<div class="two">'
        '<div><div class="blkhd">정본 (기록·재현의 기준)</div>'
        f'<pre class="txt">{_esc(ocr_txt) or "(없음)"}</pre></div>'
        '<div><div class="blkhd">VLM 후보 (다음 단계가 고를 수 있음)</div>'
        f'<pre class="txt">{_esc(cand)}</pre></div>'
        '</div></div>'
    )


def _reader_judge(r: dict) -> str:
    """목표 영역만 마스킹한 Reader/Judge shadow 결과를 정본과 나란히 보여 준다."""
    evidence = r.get("text_evidence") or {}
    adj = evidence.get("adjudication") or {}
    reader = ((evidence.get("reader") or {}).get("text") or "").strip()
    if not adj and not reader:
        return ""
    canonical = ((evidence.get("canonical") or {}).get("text") or "").strip()
    status = adj.get("status") or "Reader 결과만 있음"
    reasons = ", ".join(str(value) for value in adj.get("selection_reasons") or []) or "—"
    crop = adj.get("crop_bbox")
    target = adj.get("target_bbox")
    excluded = ", ".join(adj.get("excluded_overlap_region_ids") or []) or "없음"
    meta = (
        f'<span class="chip shadow">{_esc(status)}</span> '
        f'<span class="chip">선별: {_esc(reasons)}</span> '
        f'<span class="chip">Reader 확신 {_esc(adj.get("reader_confidence") or (evidence.get("reader") or {}).get("confidence"))}</span>'
    )
    judge = ""
    if adj:
        judge = (
            '<div class="blkhd">Judge 판정</div>'
            f'<pre class="txt">decision={_esc(adj.get("judge_decision"))}\n'
            f'confidence={_esc(adj.get("confidence"))}\n'
            f'reason={_esc(adj.get("reason") or adj.get("error"))}\n'
            f'crop={_esc(crop)}\n'
            f'target_in_crop={_esc(target)}\n'
            f'excluded_overlap_regions={_esc(excluded)}\n'
            f'policy={_esc(adj.get("crop_policy"))}</pre>'
        )
    return (
        '<div class="blk"><div class="blkhd">영역 Reader/Judge shadow ' + meta + '</div>'
        '<p class="kv">파란 테두리 안만 남기고 바깥 픽셀을 마스킹한 crop으로 비교합니다. '
        '아래 후보는 정본을 자동 변경하지 않습니다.</p>'
        '<div class="two">'
        '<div><div class="blkhd">정본</div>'
        f'<pre class="txt">{_esc(canonical) or "(없음)"}</pre></div>'
        '<div><div class="blkhd">독립 Reader</div>'
        f'<pre class="txt">{_esc(reader) or "(빈 응답)"}</pre></div>'
        '</div>' + judge + '</div>'
    )


def _table_block(t: dict | None) -> str:
    if not t:
        return ""
    note = t.get("note")
    outside = t.get("lines_outside_cells")
    meta = []
    if note:
        meta.append(_esc(note))
    if outside:
        meta.append(f"셀 밖 줄 {len(outside)}개")
    visual_rows = t.get("reader_visual_rows") or []
    reader = ("<div class=\"blkhd\">Reader 시각적 행 순서 (구조 미확정)</div>"
              f"<pre class=\"txt\">{_esc(chr(10).join(visual_rows))}</pre>" if visual_rows else "")
    return (
        f'<div class="blk"><div class="blkhd">표 격자 — {t.get("n_rows")}행 '
        f'{t.get("n_cols")}열{" · " + " · ".join(meta) if meta else ""}</div>'
        f'<div class="lines">{t.get("html") or ""}</div>{reader}</div>'
    )


def _phrases(r: dict) -> str:
    labels = r.get("template_labels") or {}
    hits = labels.get("fixed_phrase_hits") or []
    if not hits:
        return ""
    chips = " ".join(
        f'<span class="chip">{_esc(h.get("gubun"))} · {_esc(h.get("phrase_id"))}</span>'
        for h in hits
    )
    return ('<div class="blk"><div class="blkhd">정형 문구 완전일치 (규칙 · VLM 무관)</div>'
            f'{chips}</div>')


def _region_html(seq: int, r: dict) -> str:
    lines = r.get("lines") or []
    srcs = {l.get("source") for l in lines}
    src_chips = " ".join(
        f'<span class="tag" style="background:{SOURCE_META[s][1]}" title="{SOURCE_META[s][2]}">'
        f'{SOURCE_META[s][0]}</span>'
        for s in ("digital", "ocr", "vlm_sweep") if s in srcs
    )
    semantic = (r.get("template_labels") or {}).get("semantic") or []
    gubun = ", ".join(str(item.get("gubun")) for item in semantic if item.get("gubun"))
    gchip = (f'<span class="chip gubun">{_esc(gubun)}</span>' if gubun
              else '<span class="chip none">라벨 없음</span>')
    extra = ""
    if r.get("table"):
        extra += f'<span class="chip tbl">표 {r["table"].get("n_rows")}×{r["table"].get("n_cols")}</span>'
    evidence = r.get("text_evidence") or {}
    if evidence.get("adjudication") or evidence.get("reader"):
        status = (evidence.get("adjudication") or {}).get("status") or "Reader"
        extra += f'<span class="chip shadow">shadow { _esc(status) }</span>'

    body = (
        _lines_table(lines) if lines else '<p class="kv">글자가 안 붙은 검출 상자</p>'
    ) + _reader_judge(r) + _table_block(r.get("table")) + _phrases(r)

    return (
        f'<details class="rgn" data-rid="{_esc(r["region_id"])}">'
        f'<summary><span class="seq">{seq:02d}</span>'
        f'<span class="rid">{_esc(r["region_id"])}</span>'
        f'<span class="role">{_esc((r.get("layout") or {}).get("label"))}</span>{gchip}{extra}'
        f'<span class="chip">{len(lines)}줄</span>{src_chips}</summary>'
        f'<div class="rgnbody">{body}</div></details>'
    )


def _review_targets_block(targets: list[dict]) -> str:
    """다음 심의 단계로 넘길 템플릿 작업 목록을, 원본 영역과 분리해 보여 준다."""
    if not targets:
        return '<div class="blk"><div class="blkhd">Review targets</div><p class="kv">템플릿 판단불가 또는 대상 없음</p></div>'
    rows = "".join(
        '<tr>'
        f'<td>{_esc(target.get("label"))}</td>'
        f'<td>{_esc(target.get("requirement"))}</td>'
        f'<td>{_esc(target.get("evidence_status"))}</td>'
        f'<td class="mono">{_esc(", ".join(target.get("evidence", {}).get("region_ids") or []))}</td>'
        f'<td class="mono">{_esc(target.get("evidence", {}).get("card_nos"))}</td>'
        '</tr>'
        for target in targets
    )
    return (
        '<div class="blk"><div class="blkhd">Review targets — 심의용 항목·근거 연결</div>'
        '<p class="kv">위반 여부가 아니라 파서가 위치시킨 근거 상태입니다.</p>'
        '<div class="lines"><table class="ln"><thead><tr><th>항목</th><th>필수</th>'
        '<th>근거 상태</th><th>영역</th><th>카드</th></tr></thead><tbody>'
        + rows + '</tbody></table></div></div>'
    )


def _doc_html(doc: dict, pages_rel: str, anchor: str) -> str:
    cls, tpl, comp = doc["classification"], doc["template"], doc["completeness"]
    regions = [r for p in doc["pages"] for r in p["regions"]]
    n_lab = sum(1 for r in regions if (r.get("template_labels") or {}).get("semantic"))
    n_shadow = sum(1 for r in regions if (r.get("text_evidence") or {}).get("adjudication"))

    canvases = []
    for page in doc["pages"]:
        canvases.append(
            f'<div class="canvas">'
            f'<img src="{_esc(pages_rel)}/{_esc(doc["doc_id"])}_p{page["page_no"]}.jpg" '
            f'alt="p{page["page_no"]}" loading="lazy">{_boxes(page)}</div>'
        )
    if not canvases:
        canvases.append('<div class="noimg">쪽 이미지 없음 (좌표 없는 입력)</div>')

    seq = 0
    blocks = []
    for page in doc["pages"]:
        for r in page["regions"]:
            seq += 1
            blocks.append(_region_html(seq, r))
        un = page.get("unassigned_lines") or []
        if un:
            blocks.append(
                '<details class="rgn"><summary><span class="seq">—</span>'
                '<span class="rid">미배정</span>'
                '<span class="role">어느 영역 상자에도 안 붙은 낱줄</span>'
                f'<span class="chip">{len(un)}줄</span></summary>'
                f'<div class="rgnbody"><div class="unassigned">{_lines_table(un)}</div>'
                '</div></details>'
            )
        recovered = page.get("recovery_candidates") or []
        if recovered:
            blocks.append(
                '<details class="rgn"><summary><span class="seq">?</span>'
                '<span class="rid">페이지 누락문구 후보</span>'
                '<span class="role">정본 미편입</span>'
                f'<span class="chip">{len(recovered)}개</span></summary>'
                f'<div class="rgnbody"><pre class="txt">{_esc(chr(10).join(str(item.get("text") or "") for item in recovered))}</pre>'
                '</div></details>'
            )

    return (
        f'<details class="doc" id="{_esc(anchor)}">'
        f'<summary>{_esc(doc["source_file"])}'
        f'<span class="kv">분류 <b>{_esc(cls["product_group"])}</b> / {_esc(cls["ad_type"])}</span>'
        f'<span class="kv">템플릿 <b>{_esc(tpl["template_id"] or "판단불가")}</b></span>'
        f'<span class="kv">영역 <b>{len(regions)}</b> · 줄 <b>{comp["lines_total"]}</b> · '
        f'라벨영역 <b>{n_lab}</b> · Reader/Judge <b>{n_shadow}</b></span></summary>'
        f'<div class="docbody"><div class="cols">'
        f'<div class="pagewrap">{"".join(canvases)}'
        '<div class="legend">초록 상자 = 템플릿 항목이 붙은 영역 · 회색 = 안 붙음<br>'
        '오른쪽 영역을 펼치면 그 상자를 짚어 줍니다</div></div>'
        f'<div>{_review_targets_block(doc.get("review_targets") or [])}{"".join(blocks)}</div>'
        f'</div></div></details>'
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", type=Path, default=ROOT / "out_ad_full")
    ap.add_argument("--out", type=Path, default=None,
                    help="기본값: <--in>/explorer.html (그림을 상대경로로 걸므로 같은 폴더여야 한다)")
    ap.add_argument("--only", default=None, help="파일명(확장자 제외) 쉼표로 여러 개")
    ap.add_argument("--recursive", action="store_true",
                    help="<--in> 아래의 */json/*.json 을 모두 읽는다. 입력 묶음별 하위 폴더를 합쳐 볼 때 쓴다")
    args = ap.parse_args()
    out = args.out or (args.src / "explorer.html")

    files = sorted(args.src.rglob("json/*.json") if args.recursive
                   else (args.src / "json").glob("*.json"))
    if args.only:
        keys = {k.strip() for k in args.only.split(",") if k.strip()}
        files = [p for p in files if p.stem in keys]
        missing = keys - {p.stem for p in files}
        if missing:
            raise SystemExit(f"--only 로 지정했지만 없는 파일: {missing}")
    if not files:
        raise SystemExit(f"통합 JSON 이 없다: {args.src / 'json'}")

    docs = [json.loads(p.read_text(encoding="utf-8")) for p in files]

    # 전수 실행은 입력 묶음별로 `new-sample-data/json`, `sample-data/json` 아래에
    # 결과를 둔다. 검수 HTML 은 그 상위에서 열기 때문에 문서마다 그림 상대경로가 다르다.
    # 기존 단일 폴더 실행의 `pages` 경로는 그대로 유지한다.
    def pages_rel_for(json_path: Path) -> str:
        if not args.recursive:
            return "pages"
        return (json_path.parent.parent.relative_to(args.src) / "pages").as_posix()

    idx = "".join(
        f'<a href="#d{i}">{_esc(d["source_file"])}</a>' for i, d in enumerate(docs)
    )
    body = "".join(
        _doc_html(d, pages_rel_for(p), f"d{i}")
        for i, (p, d) in enumerate(zip(files, docs, strict=True))
    )

    n_r = sum(len(r) for r in ([p["regions"] for d in docs for p in d["pages"]]))
    n_l = sum(d["completeness"]["lines_total"] for d in docs)
    n_shadow = sum(1 for d in docs for p in d["pages"] for r in p["regions"]
                   if (r.get("text_evidence") or {}).get("adjudication"))
    n_tbl = sum(1 for d in docs for p in d["pages"] for r in p["regions"] if r.get("table"))

    out.write_text(
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>영역별 파싱 상세</title>"
        f"<style>{CSS}</style>"
        f"<h1>영역별 파싱 상세 — {len(docs)}건</h1>"
        f'<p class="sub">영역 {n_r} · 줄 {n_l} · Reader/Judge shadow {n_shadow} · 표 격자 {n_tbl}</p>'
        '<p class="sub">영역 순서와 배열은 <b>통합 JSON 에 실린 것 그대로</b>입니다 — 다음 단계'
        '(벡터DB·RDB 엔진)가 읽는 순서와 같습니다. 문서를 펼치고, 영역을 다시 펼치면 '
        '그 영역의 정본·밴드 VLM·Reader/Judge 후보·시인성·라벨이 한자리에 나옵니다.</p>'
        '<p class="sub">출처 배지 <span class="tag" style="background:#059669">D</span> = '
        '드래그로 잡히는 디지털 텍스트 · <span class="tag" style="background:#2563eb">O</span> = '
        'OCR 이 픽셀에서 읽음 · <span class="tag" style="background:#db2777">S</span> = '
        'OCR 이 못 읽어 VLM 이 건져옴. <b>시인성(글자 크기·굵기·색)은 D 에만 있습니다</b> — '
        'OCR·VLM 글자의 시인성 수집 로직은 아직 없습니다.</p>'
        f'<div class="index"><h2>문서 {len(docs)}건</h2><div class="idxgrid">{idx}</div></div>'
        f"{body}<script>{JS}</script>",
        encoding="utf-8",
    )
    print(f"→ {out}  ({out.stat().st_size / 1e6:.1f}MB, {len(docs)}건 · 영역 {n_r} · 줄 {n_l})")


if __name__ == "__main__":
    main()
