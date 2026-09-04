/*
 * P1/P2 영역-심의 연결 열람 화면을 만든다.
 *
 * 사용:
 *   node tools/make_p1p2_region_explorer.js
 *   node tools/make_p1p2_region_explorer.js --in out_0902_p1p2_v5_two_docs
 *
 * P1의 StructureV3 영역 bbox를 원본 페이지 위에 표시하고, P2 라벨 묶음의
 * line_refs를 클릭 가능한 연결로 바꾼다. 라벨을 재판단하거나 P1/P2를 수정하지
 * 않는 읽기 전용 검사 도구다.
 */

"use strict";

const fs = require("fs");
const path = require("path");

function argumentValue(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const inputRoot = path.resolve(argumentValue("--in", "out_0902_p1p2_v5_two_docs"));
const outputPath = path.resolve(argumentValue("--out", path.join(inputRoot, "p1p2-region-explorer.html")));
const p1Dir = path.join(inputRoot, "json");
const p2Dir = path.join(inputRoot, "review_input");

if (!fs.existsSync(p1Dir) || !fs.existsSync(p2Dir)) {
  throw new Error(`P1 또는 P2 폴더가 없습니다: ${inputRoot}`);
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function textOf(line) {
  return String(line.parser_text ?? line.text ?? "").trim();
}

function p2RefIndex(p2) {
  const labelsByRef = new Map();
  const unmappedRefs = new Set();
  const add = (ref, label) => {
    if (!labelsByRef.has(ref)) labelsByRef.set(ref, []);
    labelsByRef.get(ref).push(label);
  };
  for (const group of p2.labelled_ad_copy || []) {
    for (const view of group.text_views || []) {
      for (const ref of view.line_refs || []) add(ref, group.label || group.label_id);
    }
  }
  for (const view of p2.unmapped_ad_copy || []) {
    for (const ref of view.line_refs || []) unmappedRefs.add(ref);
  }
  return { labelsByRef, unmappedRefs };
}

function simplifyDocument(p1, p2) {
  const { labelsByRef, unmappedRefs } = p2RefIndex(p2);
  const pages = (p1.pages || []).map((page) => ({
    pageNo: page.page_no,
    canvasW: page.canvas_w,
    canvasH: page.canvas_h,
    regions: (page.regions || []).map((region) => {
      const semantic = (region.template_labels?.semantic || [])
        .map((item) => item.gubun)
        .filter(Boolean);
      return {
        id: region.region_id,
        bbox: region.bbox,
        cardNo: region.card_no,
        layout: region.layout?.label || "unknown",
        semantic,
        selection: region.text_evidence?.p2_text_selection?.selected_text_source || "parser_primary_text",
        lines: (region.lines || []).map((line) => ({
          ref: line.line_ref,
          text: textOf(line),
          bbox: line.bbox,
          source: line.text_source || line.source || "unknown",
          ocrConfidence: line.ocr_confidence ?? line.confidence ?? null,
          p2Labels: labelsByRef.get(line.line_ref) || [],
          unmapped: unmappedRefs.has(line.line_ref),
        })),
      };
    }),
    unassignedLines: (page.unassigned_lines || []).map((line) => ({
      ref: line.line_ref,
      text: textOf(line),
      bbox: line.bbox,
      source: line.text_source || line.source || "unknown",
      ocrConfidence: line.ocr_confidence ?? line.confidence ?? null,
      p2Labels: labelsByRef.get(line.line_ref) || [],
      unmapped: unmappedRefs.has(line.line_ref),
    })),
  }));
  const labelled = (p2.labelled_ad_copy || []).map((group) => ({
    id: group.label_id,
    label: group.label,
    templateScope: group.template_scope || null,
    views: (group.text_views || []).map((view) => ({
      scope: view.scope,
      reviewText: view.review_text,
      textSource: view.text_source,
      lineRefs: view.line_refs || [],
      tables: view.tables || [],
    })),
  }));
  const unmapped = (p2.unmapped_ad_copy || []).map((view) => ({
    scope: view.scope,
    reviewText: view.review_text,
    textSource: view.text_source,
    lineRefs: view.line_refs || [],
    tables: view.tables || [],
  }));
  return {
    id: p1.doc_id,
    sourceFile: p1.source_file,
    fileType: p1.file_type,
    template: p1.template || {},
    completeness: p1.completeness || {},
    p2Summary: p2.summary || {},
    pages,
    labelled,
    unmapped,
    recoveryCount: (p2.unverified_recovery_candidates || []).length,
  };
}

const docs = fs.readdirSync(p1Dir)
  .filter((name) => name.endsWith(".json"))
  .sort((a, b) => a.localeCompare(b, "ko"))
  .map((name) => simplifyDocument(readJson(path.join(p1Dir, name)), readJson(path.join(p2Dir, name))));

if (!docs.length) throw new Error(`P1 JSON이 없습니다: ${p1Dir}`);

const data = JSON.stringify({ docs }).replace(/</g, "\\u003c");
const html = `<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>P1/P2 영역-심의 연결 검사</title>
  <style>
    :root { --bg:#f5f6f8; --panel:#fff; --ink:#1c2430; --muted:#697386; --line:#d8dee8; --blue:#1565c0; --green:#20744a; --amber:#9a6500; --purple:#6b42b8; --gray:#6b7280; --red:#b42318; }
    * { box-sizing:border-box; }
    body { margin:0; padding:20px; color:var(--ink); background:var(--bg); font:14px/1.55 -apple-system,BlinkMacSystemFont,"Malgun Gothic","맑은 고딕",sans-serif; }
    h1 { margin:0; font-size:22px; }
    h2 { margin:0; font-size:16px; }
    h3 { margin:0 0 8px; font-size:14px; }
    p { margin:6px 0; }
    button, select { font:inherit; }
    button { cursor:pointer; }
    .hint { color:var(--muted); max-width:1000px; }
    .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:16px 0; }
    .toolbar label { font-weight:600; }
    select { min-width:290px; padding:7px 9px; border:1px solid var(--line); border-radius:5px; background:var(--panel); color:var(--ink); }
    .metric { padding:4px 8px; border-radius:12px; background:#e8eef7; color:#34455f; font-size:12px; }
    .layout { display:grid; grid-template-columns:minmax(330px, 54%) minmax(300px, 46%); gap:16px; align-items:start; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .image-stack { display:grid; gap:16px; }
    .page { position:relative; line-height:0; overflow:hidden; border:1px solid var(--line); background:#fff; }
    .page img { display:block; width:100%; height:auto; }
    .region-box, .line-box { position:absolute; padding:0; background:transparent; border:1.5px solid var(--gray); }
    .region-box.semantic { border-color:var(--green); background:rgba(32,116,74,.09); }
    .region-box.focus { border:3px solid var(--purple); background:rgba(107,66,184,.12); z-index:4; }
    .region-box.selected { border:3px solid var(--blue); background:rgba(21,101,192,.13); z-index:5; }
    .region-box.unmapped { border-color:var(--amber); border-style:dashed; }
    .region-id { position:absolute; top:1px; left:1px; padding:1px 4px; background:var(--panel); color:var(--ink); border:1px solid var(--line); font:10px/1.2 Consolas,monospace; white-space:nowrap; }
    .line-box { border:2px solid var(--amber); background:rgba(154,101,0,.14); pointer-events:none; z-index:7; }
    .legend { display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; color:var(--muted); font-size:12px; }
    .legend i { display:inline-block; width:12px; height:12px; margin-right:4px; vertical-align:-2px; border:2px solid; }
    .legend .green { border-color:var(--green); }.legend .gray { border-color:var(--gray); }.legend .purple { border-color:var(--purple); }.legend .amber { border-color:var(--amber); }
    .tabs { display:flex; gap:5px; margin:0 0 12px; flex-wrap:wrap; }
    .tab, .group { text-align:left; border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:5px; padding:6px 8px; }
    .tab.active, .group.active { color:#fff; background:var(--blue); border-color:var(--blue); }
    .groups { display:grid; gap:5px; max-height:340px; overflow:auto; padding-right:2px; }
    .group { display:flex; justify-content:space-between; gap:8px; align-items:baseline; }
    .group small { color:var(--muted); white-space:nowrap; }.group.active small { color:#e7f0ff; }
    .detail { margin-top:14px; border-top:1px solid var(--line); padding-top:14px; }
    .metadata { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 10px; }
    .tag { background:#eef1f5; color:#344054; border-radius:10px; padding:2px 7px; font-size:12px; }.tag.green { background:#e8f4ec; color:#17633b; }.tag.amber { background:#fff4df; color:#8a5700; }.tag.purple { background:#f0ebfb; color:#5d38a0; }
    pre { margin:8px 0; padding:9px; overflow:auto; white-space:pre-wrap; word-break:break-word; background:#f6f8fb; border:1px solid #e7ebf0; border-radius:5px; font:12px/1.5 Consolas,"Malgun Gothic",monospace; }
    table { width:100%; border-collapse:collapse; font-size:12px; } th,td { text-align:left; vertical-align:top; padding:6px; border-bottom:1px solid #e7ebf0; } th { color:var(--muted); font-weight:600; } td.ref { font-family:Consolas,monospace; white-space:nowrap; color:#4b5563; } .line-row { cursor:pointer; }.line-row:hover { background:#f4f8ff; }
    .empty { color:var(--muted); padding:12px 0; }
    @media (max-width:920px) { body { padding:12px; }.layout { grid-template-columns:1fr; }.panel { padding:12px; }.image-stack { max-height:none; } select { min-width:0; max-width:100%; } }
  </style>
</head>
<body>
  <h1>P1/P2 영역-심의 연결 검사</h1>
  <p class="hint">왼쪽은 새 P1의 StructureV3 영역입니다. 오른쪽의 P2 라벨을 누르면 그 라벨이 실제로 참조하는 P1 줄과 영역을 표시합니다. 이 화면은 결과를 수정하거나 라벨을 재판단하지 않습니다.</p>
  <div class="toolbar">
    <label for="doc-select">문서</label><select id="doc-select"></select>
    <span class="metric" id="metric-regions"></span><span class="metric" id="metric-lines"></span><span class="metric" id="metric-p2"></span>
  </div>
  <main class="layout">
    <section class="panel"><h2>원본 위 P1 영역</h2><div id="pages" class="image-stack"></div>
      <div class="legend"><span><i class="green"></i>P1 의미 라벨 영역</span><span><i class="gray"></i>라벨 없는 영역</span><span><i class="purple"></i>선택한 P2 라벨 범위</span><span><i class="amber"></i>선택한 줄 또는 미분류 범위</span></div>
    </section>
    <section class="panel">
      <h2>P2 심의 전달 묶음</h2>
      <div class="tabs"><button class="tab active" data-mode="labels" type="button">라벨별 문구</button><button class="tab" data-mode="unmapped" type="button">미분류 문구</button></div>
      <div id="groups" class="groups"></div>
      <div id="detail" class="detail" aria-live="polite"></div>
    </section>
  </main>
  <script>
    const DATA = ${data};
    const p = new URLSearchParams(location.search);
    let docIndex = Math.max(0, DATA.docs.findIndex(d => d.id.includes(p.get('doc') || '002')));
    if (docIndex < 0) docIndex = 0;
    let mode = 'labels';
    let selected = { type: 'group', index: 0 };
    const $ = (id) => document.getElementById(id);
    const refMap = (doc) => new Map(doc.pages.flatMap(page => page.regions.flatMap(region => region.lines.map(line => [line.ref, {page, region, line}]))).concat(doc.pages.flatMap(page => page.unassignedLines.map(line => [line.ref, {page, region:null, line}]))));
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    function selectedRefs(doc) {
      if (selected.type === 'region') return new Set(selected.region.lines.map(line => line.ref));
      const item = mode === 'labels' ? doc.labelled[selected.index] : doc.unmapped[selected.index];
      return new Set((item?.views || [item]).flatMap(view => view?.lineRefs || []));
    }
    function p2Item(doc) { return mode === 'labels' ? doc.labelled[selected.index] : doc.unmapped[selected.index]; }
    function p2Title(item) { return mode === 'labels' ? item.label : '미분류 문구'; }
    function fillSelect() {
      $('doc-select').innerHTML = DATA.docs.map((doc, index) => '<option value="' + index + '">' + esc(doc.sourceFile) + ' · ' + esc(doc.template.template_id || '템플릿 미확정') + '</option>').join('');
      $('doc-select').value = String(docIndex);
    }
    function renderMetrics(doc) {
      const regionCount = doc.pages.reduce((sum, page) => sum + page.regions.length, 0);
      $('metric-regions').textContent = 'P1 영역 ' + regionCount;
      $('metric-lines').textContent = '기준 원문 줄 ' + (doc.p2Summary.parser_primary_line_total ?? doc.completeness.lines_total ?? 0);
      $('metric-p2').textContent = 'P2 라벨 ' + doc.labelled.length + ' · 미분류 ' + (doc.p2Summary.unmapped_ad_copy_parser_primary_line_count ?? 0);
    }
    function boxStyle(bbox, page) {
      if (!bbox || !page.canvasW || !page.canvasH) return '';
      return 'left:' + (bbox[0] / page.canvasW * 100) + '%;top:' + (bbox[1] / page.canvasH * 100) + '%;width:' + ((bbox[2]-bbox[0]) / page.canvasW * 100) + '%;height:' + ((bbox[3]-bbox[1]) / page.canvasH * 100) + '%;';
    }
    function renderPages(doc) {
      const refs = selectedRefs(doc);
      const pages = $('pages'); pages.innerHTML = '';
      for (const page of doc.pages) {
        const wrap = document.createElement('div'); wrap.className = 'page';
        const image = document.createElement('img'); image.alt = 'P' + page.pageNo + ' 원본'; image.src = 'pages/' + encodeURIComponent(doc.id) + '_p' + page.pageNo + '.jpg'; wrap.appendChild(image);
        for (const region of page.regions) {
          const regionRefs = region.lines.map(line => line.ref);
          const focused = regionRefs.some(ref => refs.has(ref));
          const isSelected = selected.type === 'region' && selected.region.id === region.id;
          const isOnlyUnmapped = regionRefs.length && regionRefs.every(ref => {
            const line = region.lines.find(item => item.ref === ref); return line && line.unmapped;
          });
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'region-box' + (region.semantic.length ? ' semantic' : '') + (focused ? ' focus' : '') + (isSelected ? ' selected' : '') + (isOnlyUnmapped ? ' unmapped' : '');
          button.style.cssText = boxStyle(region.bbox, page); button.setAttribute('aria-label', region.id + ' 영역 선택');
          button.title = region.id + ' — ' + (region.semantic.join(', ') || 'P1 의미 라벨 없음');
          button.addEventListener('click', () => { selected = {type:'region', region}; render(); });
          const name = document.createElement('span'); name.className = 'region-id'; name.textContent = region.id.replace(/^p[0-9]+_/, ''); button.appendChild(name); wrap.appendChild(button);
        }
        for (const region of page.regions) for (const line of region.lines) if (refs.has(line.ref) && line.bbox) {
          const lineBox = document.createElement('div'); lineBox.className = 'line-box'; lineBox.style.cssText = boxStyle(line.bbox, page); wrap.appendChild(lineBox);
        }
        pages.appendChild(wrap);
      }
    }
    function renderGroups(doc) {
      const list = mode === 'labels' ? doc.labelled : doc.unmapped;
      const groups = $('groups'); groups.innerHTML = '';
      if (!list.length) { groups.innerHTML = '<div class="empty">해당 문구가 없습니다.</div>'; return; }
      list.forEach((item, index) => {
        const refs = (item.views || [item]).flatMap(view => view.lineRefs || []);
        const button = document.createElement('button'); button.type = 'button'; button.className = 'group' + (selected.type === 'group' && index === selected.index ? ' active' : '');
        button.innerHTML = '<span>' + esc(mode === 'labels' ? item.label : '미분류 문구') + '</span><small>' + refs.length + '줄 · ' + (item.views || [item]).length + '뷰</small>';
        button.addEventListener('click', () => { selected = {type:'group', index}; render(); }); groups.appendChild(button);
      });
    }
    function renderDetail(doc) {
      const detail = $('detail');
      if (selected.type === 'region') {
        const region = selected.region;
        const tags = [region.id, '구조: ' + region.layout, region.cardNo == null ? '공통 영역' : '카드 ' + region.cardNo]
          .concat(region.semantic.length ? region.semantic.map(v => 'P1: ' + v) : ['P1 의미 라벨 없음'])
          .map((tag, i) => '<span class="tag ' + (i >= 3 && region.semantic.length ? 'green' : '') + '">' + esc(tag) + '</span>').join('');
        const rows = region.lines.map(line => '<tr class="line-row" data-ref="' + esc(line.ref) + '"><td class="ref">' + esc(line.ref.split('/').pop()) + '</td><td>' + esc(line.text) + '</td><td>' + esc(line.source) + '</td><td>' + (line.p2Labels.length ? line.p2Labels.map(label => '<span class="tag purple">' + esc(label) + '</span>').join(' ') : line.unmapped ? '<span class="tag amber">미분류</span>' : '—') + '</td></tr>').join('');
        detail.innerHTML = '<h3>P1 선택 영역</h3><div class="metadata">' + tags + '</div><table><thead><tr><th>줄</th><th>기준 원문</th><th>출처</th><th>P2 연결</th></tr></thead><tbody>' + rows + '</tbody></table>';
        return;
      }
      const item = p2Item(doc);
      if (!item) { detail.innerHTML = '<div class="empty">선택할 P2 항목이 없습니다.</div>'; return; }
      const views = item.views || [item];
      const refs = selectedRefs(doc); const lookup = refMap(doc);
      const p1Regions = [...new Set([...refs].map(ref => lookup.get(ref)?.region?.id).filter(Boolean))];
      const scopeText = views.map(view => 'p' + (view.scope?.page_no ?? '?') + ' / ' + (view.scope?.card_no == null ? '공통' : '카드 ' + view.scope.card_no)).join(', ');
      detail.innerHTML = '<h3>' + esc(p2Title(item)) + '</h3><div class="metadata"><span class="tag purple">P2 ' + (mode === 'labels' ? '라벨 묶음' : '미분류 묶음') + '</span><span class="tag">' + refs.size + ' P1 줄</span><span class="tag">' + esc(scopeText) + '</span><span class="tag">영역 ' + esc(p1Regions.join(', ') || '미배정') + '</span></div>' + views.map(view => '<div><p><b>심의 텍스트</b> <span class="tag">' + esc(view.textSource) + '</span></p><pre>' + esc(view.reviewText) + '</pre><p class="hint">근거: ' + (view.lineRefs || []).map(ref => '<code>' + esc(ref) + '</code>').join(' ') + '</p>' + tablesHtml(view) + '</div>').join('');
    }
    function tablesHtml(view) {
      // 격자는 VLM 관측이라 정본이 아니다 — status/신뢰도를 값과 같이 보여 준다.
      return (view.tables || []).map(table => {
        const rows = (table.rows || []).map(row => '<tr>' + row.map(cell => '<td>' + esc(cell) + '</td>').join('') + '</tr>').join('');
        const meta = [table.status, table.confidence == null ? null : 'conf ' + table.confidence,
                      table.structure_confidence == null ? null : 'struct ' + table.structure_confidence]
                     .filter(Boolean).map(text => '<span class="tag amber">' + esc(text) + '</span>').join('');
        return '<p><b>표 격자 관측</b> <span class="tag">' + esc(table.region_id) + '</span>' + meta + '</p>'
             + (rows ? '<table><tbody>' + rows + '</tbody></table>'
                     : '<div class="empty">격자를 읽지 못했습니다 (표 영역만 검출).</div>');
      }).join('');
    }
    function render() {
      const doc = DATA.docs[docIndex];
      const list = mode === 'labels' ? doc.labelled : doc.unmapped;
      if (selected.type === 'group' && (!list.length || selected.index >= list.length)) selected = {type:'group', index:0};
      renderMetrics(doc); renderGroups(doc); renderPages(doc); renderDetail(doc);
    }
    $('doc-select').addEventListener('change', event => { docIndex = Number(event.target.value); mode = 'labels'; selected = {type:'group', index:0}; render(); });
    document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => { mode = tab.dataset.mode; selected = {type:'group', index:0}; document.querySelectorAll('.tab').forEach(item => item.classList.toggle('active', item === tab)); render(); }));
    fillSelect(); render();
  </script>
</body>
</html>`;

fs.writeFileSync(outputPath, html, "utf8");
console.log(`→ ${outputPath}`);
console.log(`  documents=${docs.length}, bytes=${fs.statSync(outputPath).size}`);
