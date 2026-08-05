// -*- coding: utf-8 -*-
// NH 광고심의 파싱 파이프라인 — Excalidraw 다이어그램 생성기
// 좌표를 손으로 안 맞추려고 스크립트로 만든다. 실행: node gen_excalidraw.js
"use strict";
const fs = require("fs");

let idCounter = 1;
function nid(prefix) { return `${prefix}_${idCounter++}`; }
const SEED = 1234567;
const NOW = 1754352000000; // 고정 타임스탬프 (파일 재생성해도 diff 가 안정적)

// ── 색 팔레트 ──────────────────────────────────────────────
const COLOR = {
  CODE:   { stroke: "#495057", bg: "#f1f3f5" },
  PADDLE: { stroke: "#1971c2", bg: "#d0ebff" },
  VLM:    { stroke: "#9c36b5", bg: "#f3d9fa" },
  OUT:    { stroke: "#2f9e44", bg: "#d3f9d8" },
  IO:     { stroke: "#212529", bg: "#ffffff" },
  RED:    { stroke: "#e03131", bg: "#ffe3e3" },
};
const TEXT_DARK = "#1e1e1e";
const TEXT_GRAY = "#5c5f66";
const TEXT_MONO = "#2b8a3e";
const ARROW_MAIN = "#495057";
const ARROW_BRANCH = "#868e96";
const ARROW_SPECIAL = "#7048e8";

// ── 텍스트 폭 추정 (한글은 넓게, 영문/숫자는 좁게) ─────────
function charW(ch, fontSize) {
  const c = ch.codePointAt(0);
  const isWide = (c >= 0x1100 && c <= 0x11ff) || (c >= 0x3000 && c <= 0x9fff) ||
                 (c >= 0xac00 && c <= 0xd7a3) || (c >= 0xff00 && c <= 0xffef);
  return isWide ? fontSize * 1.0 : fontSize * 0.56;
}
function lineW(line, fontSize) {
  let w = 0;
  for (const ch of line) w += charW(ch, fontSize);
  return w;
}
function measure(lines, fontSize) {
  const width = Math.max(...lines.map((l) => lineW(l, fontSize)));
  const height = lines.length * fontSize * 1.3;
  return { width: Math.ceil(width) + 6, height: Math.ceil(height) };
}

// ── 엘리먼트 팩토리 ────────────────────────────────────────
const elements = [];

function baseProps(id, x, y, w, h, extra) {
  return Object.assign(
    {
      id, x, y, width: w, height: h, angle: 0,
      strokeColor: "#1e1e1e", backgroundColor: "transparent",
      fillStyle: "solid", strokeWidth: 1, strokeStyle: "solid",
      roughness: 1, opacity: 100, groupIds: [], frameId: null,
      roundness: null, seed: SEED, version: 1, versionNonce: SEED + idCounter,
      isDeleted: false, boundElements: null, updated: NOW, link: null, locked: false,
    },
    extra
  );
}

function rect(x, y, w, h, stroke, bg, opts = {}) {
  const id = nid("rect");
  elements.push(
    baseProps(id, x, y, w, h, {
      type: "rectangle",
      strokeColor: stroke,
      backgroundColor: bg,
      roundness: { type: 3 },
      strokeWidth: opts.strokeWidth || 2,
      strokeStyle: opts.strokeStyle || "solid",
    })
  );
  return id;
}

function textEl(x, y, lines, fontSize, color, opts = {}) {
  const id = nid("text");
  const { width, height } = measure(lines, fontSize);
  elements.push(
    baseProps(id, x, y, width, height, {
      type: "text",
      strokeColor: color,
      text: lines.join("\n"),
      fontSize,
      fontFamily: opts.fontFamily || 2,
      textAlign: opts.textAlign || "left",
      verticalAlign: "top",
      baseline: Math.round(fontSize * 0.9),
      containerId: null,
      originalText: lines.join("\n"),
      lineHeight: 1.3,
    })
  );
  return { id, width, height };
}

function arrow(x, y, points, color, opts = {}) {
  const id = nid("arrow");
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const w = Math.max(...xs) - Math.min(...xs) || 1;
  const h = Math.max(...ys) - Math.min(...ys) || 1;
  elements.push(
    baseProps(id, x, y, w, h, {
      type: "arrow",
      strokeColor: color,
      strokeWidth: opts.strokeWidth || 2,
      strokeStyle: opts.strokeStyle || "solid",
      roundness: opts.round === false ? null : { type: 2 },
      points,
      lastCommittedPoint: null,
      startBinding: null,
      endBinding: null,
      startArrowhead: opts.startArrowhead || null,
      endArrowhead: opts.endArrowhead || "triangle",
    })
  );
  return id;
}

// 상자 + 안의 3슬롯(제목/본문/데이터띠)을 한 번에 만든다
function box(x, y, w, h, title, body, dataLines, colorKey, opts = {}) {
  rect(x, y, w, h, COLOR[colorKey].stroke, COLOR[colorKey].bg, opts);
  textEl(x + 10, y + 8, [title], 17, TEXT_DARK, { fontFamily: 2 });
  if (body && body.length) {
    textEl(x + 10, y + 34, body, 12.5, TEXT_GRAY, { fontFamily: 2 });
  }
  if (dataLines && dataLines.length) {
    textEl(x, y + h + 8, dataLines, 12, TEXT_MONO, { fontFamily: 3 });
  }
  return { x, y, w, h, cx: x + w / 2, cy: y + h / 2 };
}

// 부모 상자 → 여러 자식(분기 목록)으로 팬아웃하는 화살표 (ㄱ자 꺾임)
function fanArrow(hub, target, color, bend = 40) {
  const targetLeftMid = [target.x, target.y + target.h / 2];
  const pts = [
    [0, 0],
    [bend, 0],
    [bend, targetLeftMid[1] - hub[1]],
    [targetLeftMid[0] - hub[0], targetLeftMid[1] - hub[1]],
  ];
  arrow(hub[0], hub[1], pts, color, { strokeWidth: 1.5, round: false });
}

// ══════════════════════════════════════════════════════════
// 0. 제목 + 범례
// ══════════════════════════════════════════════════════════
textEl(40, -170, ["NH 광고심의 파싱 파이프라인 — 자료구조가 무엇으로 변해가는가"], 26, TEXT_DARK, { fontFamily: 2 });
textEl(40, -130, [
  "예시 값은 올원e적금 p1_r004(상품명|NH올원e적금) 1건을 끝까지 따라간다 · 2026-08-05 기준",
], 14, TEXT_GRAY, { fontFamily: 2 });

const legendY = -80;
const legendItems = [
  ["CODE", "순수 코드 (모델 호출 없음)"],
  ["PADDLE", "PaddleX OCR"],
  ["VLM", "Gemma VLM"],
  ["OUT", "산출물 파일"],
  ["IO", "입력·중간 데이터"],
];
let lx = 40;
for (const [key, label] of legendItems) {
  rect(lx, legendY, 22, 22, COLOR[key].stroke, COLOR[key].bg, { strokeWidth: 2 });
  const { width } = textEl(lx + 30, legendY + 2, [label], 13, TEXT_DARK, { fontFamily: 2 });
  lx += 30 + width + 34;
}
textEl(lx + 10, legendY + 2, ["점선=예외경로 · 빨강=알려진 결함/강조 · 초록 monospace=실제 데이터"], 13, "#e03131", { fontFamily: 2 });

// ══════════════════════════════════════════════════════════
// 1. 메인 플로우 — 9개 상자
// ══════════════════════════════════════════════════════════
const BOX_W = 230, BOX_H = 130, STEP = 320;
const Y_MAIN = 260;

const specs = [
  { title: "① 광고 파일", body: ["PDF · PNG · HWP", "샘플 5건 (PDF2 PNG2 HWP1)"], data: ["올원e적금.png", "1122×6429"], color: "IO" },
  { title: "② 라우팅", body: ["이 페이지를 어떻게", "읽을지 먼저 정한다", "※렌더링 전 PDF 내부를 읽음"], data: ["→ route='ocr'"], color: "CODE" },
  { title: "③ 조각 분할", body: ["글자 밀도로 자른다", "밝기 아니라 대비로 잼", "높이상한 1600px"], data: ["9조각 (y=0,1400,2800…)"], color: "CODE" },
  { title: "④ StructureV3", body: ["조각당 1회 호출", "글자+좌표+레이아웃 블록을", "한 응답에서 같이 받음"], data: ["Line'상품명'[66,863,200,922] c0.99", "Block'text'[68,865,677,912]"], color: "PADDLE" },
  { title: "⑤ 영역 조립", body: ["라인 중심점이 블록", "안이면 그 블록에 넣는다", "못 들어간 건 미배정"], data: ["Region p1_r004 role=본문", "lines=[상품명,NH올원e적금]"], color: "CODE" },
  { title: "⑥ 구조 판정", body: ["역할 9종 + 카드 묶음", "페이지당 1회", "개수=코드 · 배정=VLM"], data: ["role: 본문 (VLM 판정)"], color: "VLM" },
  { title: "⑦ 통합 재판독", body: ["VLM 이 같은 조각을", "다시 읽는다 (4종)", "결과는 후보로만 부착"], data: ["+vlm_reading 'NH올원e적금'", "+relation 'head_drop'"], color: "VLM" },
  { title: "⑧ 읽기순서", body: ["카드 → 위아래 → 좌우", "좌표·신뢰도 버리고", "region_id 만 남김"], data: ["→ llm_view 투영"], color: "CODE" },
  { title: "⑨ 산출물", body: ["목적이 서로 충돌해서", "3개로 나눈다"], data: [], color: "OUT" },
];

const mainBoxes = specs.map((s, i) => {
  const x = 40 + i * STEP;
  return box(x, Y_MAIN, BOX_W, BOX_H, s.title, s.body, s.data, s.color);
});

for (let i = 0; i < mainBoxes.length - 1; i++) {
  const a = mainBoxes[i], b = mainBoxes[i + 1];
  arrow(a.x + a.w, a.cy, [[0, 0], [b.x - (a.x + a.w), 0]], ARROW_MAIN, { strokeWidth: 2.5 });
}

// ══════════════════════════════════════════════════════════
// 2. 라우팅 분기 (라우팅 박스 아래, 4갈래 — 상호배타)
// ══════════════════════════════════════════════════════════
{
  const parent = mainBoxes[1];
  const hub = [parent.cx, parent.y + parent.h + 40];
  arrow(parent.cx, parent.y + parent.h, [[0, 0], [0, 40]], ARROW_BRANCH, { strokeWidth: 1.5, endArrowhead: null });

  const itemW = 250, itemH = 62, gap = 12;
  const items = [
    { title: "scan_like", body: ["20자↓ · U+FFFD · PUA폰트"], data: ["🏷001 p1(0자) · 003 p1·p2"] },
    { title: "hybrid ⚠", body: ["이미지면적 50%↑"], data: ["🏷샘플 0건 — 미검증"] },
    { title: "structured", body: ["전부 통과 → OCR 안돌림"], data: ["🏷003 p3 (503자,한글317자)"] },
    { title: "HWP · 판정없음", body: ["사내 파서가 정본"], data: ["🏷004 (VLM0회·1.4초)"] },
  ];
  let iy = hub[1] - itemH / 2;
  const ix = hub[0] + 90;
  for (const it of items) {
    const b = box(ix, iy, itemW, itemH, it.title, it.body, it.data, "CODE", { strokeWidth: 1.5 });
    fanArrow(hub, b, ARROW_BRANCH, 35);
    iy += itemH + gap;
  }
}

// ══════════════════════════════════════════════════════════
// 3. 통합 재판독 분기 (⑦ 박스 아래, 4종 — 전부 실행)
// ══════════════════════════════════════════════════════════
let retranscribeBottom = 0;
{
  const parent = mainBoxes[6];
  const hub = [parent.cx, parent.y + parent.h + 40];
  arrow(parent.cx, parent.y + parent.h, [[0, 0], [0, 40]], ARROW_BRANCH, { strokeWidth: 1.5, endArrowhead: null });

  const itemW = 280, itemH = 62, gap = 12;
  const items = [
    { title: "⑩ 밴드 통독", body: ["OCR 과 같은 조각을 VLM 에"], data: ["27회 · 후보 177건"] },
    { title: "⑪ 통짜 스윕", body: ["페이지 전체 1장, 대형 타이포"], data: ["6회 · 24줄 회수"] },
    { title: "⑫ 중복 심판", body: ["두 판독 갈렸을 때 그 줄만"], data: ["14회 · 5건 부착"] },
    { title: "⑬ 저신뢰 재판독", body: ["conf<0.8 인 줄만 확대"], data: ["10회 · 5건 부착 ⚠3건 유실"] },
  ];
  let iy = hub[1] - itemH / 2;
  const ix = hub[0] + 90;
  for (const it of items) {
    const b = box(ix, iy, itemW, itemH, it.title, it.body, it.data, "VLM", { strokeWidth: 1.5 });
    fanArrow(hub, b, ARROW_BRANCH, 35);
    iy += itemH + gap;
  }
  retranscribeBottom = iy;
}

// ══════════════════════════════════════════════════════════
// 4. 산출물 분기 (⑨ 박스 아래, 3개)
// ══════════════════════════════════════════════════════════
{
  const parent = mainBoxes[8];
  const hub = [parent.cx, parent.y + parent.h + 40];
  arrow(parent.cx, parent.y + parent.h, [[0, 0], [0, 40]], ARROW_BRANCH, { strokeWidth: 1.5, endArrowhead: null });

  const itemW = 300, itemH = 62, gap = 12;
  const items = [
    { title: "out/json", body: ["전체 기록 — 좌표·신뢰도·출처·판단로그"], data: ["→ 감사 · 미리보기 · 화면 왼쪽"] },
    { title: "out/llm_view", body: ["정제 텍스트 — 좌표 버림, id 만 남김"], data: ["→ RAG · STAGE_3 입력"] },
    { title: "out/extracted", body: ["구조화 필드 — 값·상태·근거"], data: ["→ DB"] },
  ];
  let iy = hub[1] - itemH / 2;
  const ix = hub[0] + 90;
  for (const it of items) {
    const b = box(ix, iy, itemW, itemH, it.title, it.body, it.data, "OUT", { strokeWidth: 1.5 });
    fanArrow(hub, b, ARROW_BRANCH, 35);
    iy += itemH + gap;
  }
}

// ══════════════════════════════════════════════════════════
// 5. 점선 특수 경로 2개
// ══════════════════════════════════════════════════════════
// (a) structured 우회: 라우팅 → 영역조립, 메인 라인 위로 아치
{
  const from = mainBoxes[1], to = mainBoxes[4];
  const sx = from.cx, sy = from.y;
  const ex = to.cx, ey = to.y;
  const apex = -120;
  arrow(sx, sy, [[0, 0], [0, apex], [ex - sx, apex], [ex - sx, 0]], ARROW_SPECIAL, {
    strokeWidth: 2, strokeStyle: "dashed", round: false,
  });
  textEl(sx + (ex - sx) / 2 - 160, sy + apex - 24, [
    "structured 면 StructureV3 를 건너뛴다 (④⑤ 우회)",
    "PDF 내부 텍스트가 곧바로 ⑤ 로 · 🏷003 p3: 영역2·라인25",
  ], 13, ARROW_SPECIAL, { fontFamily: 2 });
}
// (b) 미배정 되돌림: 영역조립 박스 자기 자신에게로 (아래로 루프)
{
  const b4 = mainBoxes[4];
  const sx = b4.x + 45, sy = b4.y + b4.h;
  const ex = b4.x + b4.w - 45, ey = b4.y + b4.h;
  const dip = 90;
  arrow(sx, sy, [[0, 0], [0, dip], [ex - sx, dip], [ex - sx, 0]], ARROW_SPECIAL, {
    strokeWidth: 2, strokeStyle: "dashed", round: false,
  });
  textEl(sx - 10, sy + dip + 8, [
    "미배정 낱줄 — 좌표만 보고 다시 붙인다 (VLM 없음)",
    "포함→같은칼럼근접 · 🏷001: 58줄 붙음 (포함29/근접29)",
  ], 13, ARROW_SPECIAL, { fontFamily: 2 });
}

// ══════════════════════════════════════════════════════════
// 6. 빨간 강조 상자 — "정본은 안 덮는다"
// ══════════════════════════════════════════════════════════
{
  const x = mainBoxes[6].x + 320, y = retranscribeBottom + 30, w = 560, h = 210;
  rect(x, y, w, h, COLOR.RED.stroke, COLOR.RED.bg, { strokeWidth: 2.5 });
  textEl(x + 20, y + 16, ["정본은 안 덮는다"], 20, "#c92a2a", { fontFamily: 2 });
  textEl(x + 20, y + 50, [
    "OCR 이 읽은 값은 그대로 두고 VLM 판독은",
    "옆칸에 후보로만 붙인다.",
    "",
    "왜 — VLM 은 실행마다 미세하게 달라져 덮으면",
    "같은 광고를 두 번 돌릴 때 심의 대상 문구가",
    "바뀐다. 최종 선택은 STAGE_3 가 한다.",
  ], 13.5, "#1e1e1e", { fontFamily: 2 });
  textEl(x + 20, y + 160, [
    "실제 사례(002)  정본'최고연71%'→후보'최고연7.1%'→필드'연7.1%'",
    "정본은 rate_mentions 에 그대로 남아 감사 가능",
  ], 12, TEXT_MONO, { fontFamily: 3 });
}

// ══════════════════════════════════════════════════════════
// 저장
// ══════════════════════════════════════════════════════════
const drawing = {
  type: "excalidraw",
  version: 2,
  source: "https://github.com/zsviczian/obsidian-excalidraw-plugin",
  elements,
  appState: { gridSize: null, viewBackgroundColor: "#ffffff" },
  files: {},
};

const md = `---

excalidraw-plugin: parsed
tags: [excalidraw]

---
==⚠  Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this document. ⚠== You can decompress Drawing data with the command palette: 'Decompress current Excalidraw file'. For more info check in plugin settings under 'Saving'.


# Excalidraw Data

## Text Elements

%%
## Drawing
\`\`\`json
${JSON.stringify(drawing, null, 2)}
\`\`\`
%%
`;

const OUT_PATH = process.argv[2] || "pipeline-diagram.excalidraw.md";
fs.writeFileSync(OUT_PATH, md, "utf-8");
console.log(`엘리먼트 ${elements.length}개 생성 완료 → ${OUT_PATH}`);
