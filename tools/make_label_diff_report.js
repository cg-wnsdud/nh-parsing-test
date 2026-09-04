// 라벨링 A/B 비교 화면 — 두 실행의 P1/P2 를 맞대어 무엇이 달라졌는지 보인다.
//
// 왜 필요한가. 라벨 VLM 은 비결정적이라 한 실행만 보면 "고친 것의 효과"와 "실행 간
// 변동"을 가릴 수 없다(src/nh_parsing/vlm_cache.py 주석의 그 문제다). 프롬프트를 바꾸면
// VLM 캐시 키가 달라져 재생도 못 쓴다. 그래서 **영역 단위로 라벨이 어떻게 바뀌었는지**를
// 나열해, 노린 자리가 바뀌었는지 사람이 직접 확인하게 한다.
//
//   node tools/make_label_diff_report.js --before <출력폴더> --after <출력폴더> [--out <html>]

const fs = require("fs");
const path = require("path");

function argumentValue(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const beforeRoot = path.resolve(argumentValue("--before", "out_0902_p1p2_v5_two_docs"));
const afterRoot = path.resolve(argumentValue("--after", "out_0903_step12_rerun"));
const outputPath = path.resolve(argumentValue("--out", path.join(afterRoot, "label-diff-report.html")));

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function docNames(root) {
  const dir = path.join(root, "json");
  if (!fs.existsSync(dir)) throw new Error(`json 폴더가 없습니다: ${root}`);
  return fs.readdirSync(dir).filter((name) => name.endsWith(".json")).sort();
}

// 영역 하나의 라벨 상태를 사람이 비교할 수 있는 문자열로 접는다.
function regionState(region) {
  const semantic = (region.template_labels?.semantic || [])
    .map((entry) => `${entry.gubun}[${entry.line_from}-${entry.line_to}]`)
    .join(" ");
  const unlabelled = (region.lines || [])
    .map((line, index) => ((line.vlm_gubun || []).length ? null : index))
    .filter((index) => index !== null);
  return {
    id: region.region_id,
    layout: region.layout?.label ?? "?",
    score: region.layout?.score ?? null,
    lineCount: (region.lines || []).length,
    semantic,
    unlabelled,
    text: (region.lines || []).map((line) => line.parser_text).filter(Boolean).slice(0, 3).join(" / ").slice(0, 110),
  };
}

function indexRegions(evidence) {
  const out = new Map();
  for (const page of evidence.pages || []) {
    for (const region of page.regions || []) out.set(region.region_id, regionState(region));
  }
  return out;
}

function tableRows(p2) {
  const out = [];
  for (const group of p2.labelled_ad_copy || []) {
    for (const view of group.text_views || []) {
      for (const table of view.tables || []) out.push({ label: group.label, ...table });
    }
  }
  for (const view of p2.unmapped_ad_copy || []) {
    for (const table of view.tables || []) out.push({ label: "(미분류)", ...table });
  }
  return out;
}

const docs = docNames(afterRoot).map((name) => {
  const after = readJson(path.join(afterRoot, "json", name));
  const afterP2 = readJson(path.join(afterRoot, "review_input", name));
  const beforePath = path.join(beforeRoot, "json", name);
  const hasBefore = fs.existsSync(beforePath);
  const before = hasBefore ? readJson(beforePath) : { pages: [] };
  const beforeP2 = hasBefore ? readJson(path.join(beforeRoot, "review_input", name)) : { summary: {} };

  const beforeRegions = indexRegions(before);
  const afterRegions = indexRegions(after);
  const changes = [];
  for (const [id, now] of afterRegions) {
    const was = beforeRegions.get(id);
    if (!was) { changes.push({ kind: "added", id, was: null, now }); continue; }
    if (was.semantic !== now.semantic || was.unlabelled.length !== now.unlabelled.length) {
      changes.push({ kind: "changed", id, was, now });
    }
  }
  for (const [id, was] of beforeRegions) {
    if (!afterRegions.has(id)) changes.push({ kind: "removed", id, was, now: null });
  }
  return {
    name: name.replace(/\.json$/, ""),
    template: after.template?.template_id || after.template?.status || "?",
    metrics: {
      lines: after.completeness?.lines_total ?? 0,
      coveredBefore: before.completeness?.lines_covered ?? null,
      coveredAfter: after.completeness?.lines_covered ?? 0,
      unmappedBefore: beforeP2.summary?.unmapped_ad_copy_parser_primary_line_count ?? null,
      unmappedAfter: afterP2.summary?.unmapped_ad_copy_parser_primary_line_count ?? 0,
      contract: afterP2.contract?.version || "?",
    },
    labelNotes: (after.notes || []).filter((note) => note.includes("템플릿 항목 라벨링")),
    tables: tableRows(afterP2),
    changes,
  };
});

const esc = (value) => String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const span = (list) => (list.length ? list.reduce((acc, index) => {
  const last = acc[acc.length - 1];
  if (last && index === last[1] + 1) { last[1] = index; return acc; }
  acc.push([index, index]); return acc;
}, []).map(([a, b]) => (a === b ? `${a}` : `${a}-${b}`)).join(",") : "없음");

const html = `<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>라벨링 A/B 비교</title>
<style>
  :root { --bg:#f6f8fa; --card:#fff; --line:#e3e8ee; --muted:#6b7280; --add:#0f766e; --del:#b91c1c; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:#111827; font:14px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif; }
  header { background:#111827; color:#fff; padding:16px 22px; }
  header h1 { margin:0 0 4px; font-size:17px; }
  header p { margin:0; color:#9ca3af; font-size:12px; }
  main { padding:20px 22px 60px; max-width:1180px; margin:0 auto; }
  .doc { background:var(--card); border:1px solid var(--line); border-radius:10px; margin-bottom:18px; overflow:hidden; }
  .doc > h2 { margin:0; padding:12px 16px; font-size:15px; border-bottom:1px solid var(--line); background:#fbfcfd; }
  .doc > h2 small { color:var(--muted); font-weight:400; margin-left:8px; font-size:12px; }
  .body { padding:14px 16px; }
  .metrics { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
  .kpi { border:1px solid var(--line); border-radius:6px; padding:6px 10px; font-size:12px; background:#fbfcfd; }
  .kpi b { font-size:14px; }
  .better { color:var(--add); } .worse { color:var(--del); }
  h3 { font-size:13px; margin:16px 0 8px; text-transform:none; color:#374151; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th,td { text-align:left; vertical-align:top; padding:6px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; background:#fbfcfd; }
  code { font-family:Consolas,monospace; font-size:11px; }
  .was { color:var(--del); } .now { color:var(--add); }
  .empty { color:var(--muted); padding:8px 0; font-size:12px; }
  ul.notes { margin:0; padding-left:18px; font-size:12px; color:#374151; }
  .grid td { font-family:Consolas,monospace; font-size:11px; }
  .tag { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:1px 7px; font-size:11px; color:var(--muted); margin-left:6px; }
</style></head><body>
<header>
  <h1>라벨링 A/B 비교 — 긴 영역 조각 분할 + 구조 신호</h1>
  <p>before <code>${esc(path.basename(beforeRoot))}</code> · after <code>${esc(path.basename(afterRoot))}</code> ·
     라벨 VLM 은 비결정적이라 변동이 섞인다 — 노린 자리가 바뀌었는지로 판정한다.</p>
</header>
<main>
${docs.map((doc) => {
  const m = doc.metrics;
  const delta = (before, after, lowerIsBetter) => {
    if (before === null) return "";
    const diff = after - before;
    if (!diff) return ' <span class="tag">변화 없음</span>';
    const good = lowerIsBetter ? diff < 0 : diff > 0;
    return ` <span class="${good ? "better" : "worse"}">(${diff > 0 ? "+" : ""}${diff})</span>`;
  };
  return `<section class="doc">
  <h2>${esc(doc.name)}<small>${esc(doc.template)} · ${esc(m.contract)}</small></h2>
  <div class="body">
    <div class="metrics">
      <div class="kpi">정본 줄 <b>${m.lines}</b></div>
      <div class="kpi">라벨 닿은 줄 <b>${m.coveredBefore ?? "?"} → ${m.coveredAfter}</b>${delta(m.coveredBefore, m.coveredAfter, false)}</div>
      <div class="kpi">P2 미배정 줄 <b>${m.unmappedBefore ?? "?"} → ${m.unmappedAfter}</b>${delta(m.unmappedBefore, m.unmappedAfter, true)}</div>
      <div class="kpi">영역 라벨 변화 <b>${doc.changes.length}</b>건</div>
    </div>

    <h3>영역 라벨이 달라진 곳</h3>
    ${doc.changes.length ? `<table><thead><tr><th>영역</th><th>레이아웃</th><th>줄</th><th>before</th><th>after</th><th>원문(앞부분)</th></tr></thead><tbody>
      ${doc.changes.map((change) => {
        const state = change.now || change.was;
        return `<tr>
          <td><code>${esc(change.id)}</code></td>
          <td>${esc(state.layout)}${state.score == null ? "" : ` <span class="tag">${state.score.toFixed(2)}</span>`}</td>
          <td>${state.lineCount}</td>
          <td class="was">${change.was ? `${esc(change.was.semantic || "라벨 없음")}<br><span class="tag">미라벨 ${esc(span(change.was.unlabelled))}</span>` : "<i>영역 없음</i>"}</td>
          <td class="now">${change.now ? `${esc(change.now.semantic || "라벨 없음")}<br><span class="tag">미라벨 ${esc(span(change.now.unlabelled))}</span>` : "<i>영역 없음</i>"}</td>
          <td>${esc(state.text)}</td>
        </tr>`;
      }).join("")}
    </tbody></table>` : '<div class="empty">라벨이 달라진 영역이 없습니다.</div>'}

    <h3>P2 로 넘어간 표 격자</h3>
    ${doc.tables.length ? doc.tables.map((table) => `<p style="margin:6px 0 4px"><b>${esc(table.label)}</b> ← <code>${esc(table.region_id)}</code>
        <span class="tag">${esc(table.status)}</span><span class="tag">conf ${esc(table.confidence)}</span><span class="tag">struct ${esc(table.structure_confidence)}</span></p>
      <table class="grid"><tbody>${(table.rows || []).map((row) => `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table>`).join("")
      : '<div class="empty">이 문서에는 표 영역이 없습니다.</div>'}

    <h3>라벨링 진단 노트</h3>
    ${doc.labelNotes.length ? `<ul class="notes">${doc.labelNotes.map((note) => `<li>${esc(note)}</li>`).join("")}</ul>`
      : '<div class="empty">무응답·범위이탈·겹침 없음.</div>'}
  </div>
</section>`;
}).join("")}
</main></body></html>`;

fs.writeFileSync(outputPath, html, "utf8");
console.log(`→ ${outputPath}`);
console.log(`  documents=${docs.length}, changes=${docs.reduce((sum, doc) => sum + doc.changes.length, 0)}, bytes=${fs.statSync(outputPath).size}`);
