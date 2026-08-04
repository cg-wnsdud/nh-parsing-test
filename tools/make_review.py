# -*- coding: utf-8 -*-
"""파싱 결과 사람-검토용 리뷰 리포트 — out/json → out/review.html.

AdPageIR JSON 은 수천 줄이라 "어디까지 잡혔나"를 눈으로 확인하기 어렵다.
이 도구는 파일별·페이지별로
  ① 미리보기 이미지(라인/영역 bbox 오버레이) 를 왼쪽에,
  ② 파싱 결과(영역 → 라인, 출처·신뢰도 태그) 를 오른쪽에
나란히 놓아, 캡처 원본과 파싱 결과를 대조하며 검토하게 한다.

미리보기 박스는 2026-08-03 부터 **역할별 색상이 아니라 단색(마젠타) 2단**이다 —
가는 선=라인, 굵은 선=영역. 역할은 VLM 의미 판단이라 실행마다 달라질 수 있어
좌표 그림에 색으로 박지 않는다(pipeline._save_preview 주석 참조). 역할 값 자체는
오른쪽 컬럼의 영역 태그로 그대로 보인다.

- 이미지는 base64 로 내장 → 단일 HTML 파일(자체완결). 브라우저로 바로 열면 됨.
- 폐쇄망 고려: 외부 리소스·업로드 없음(로컬 파일만 생성).

2026-08-04: 처음 보는 사람도 훑어볼 수 있게 두 가지를 더했다.
- 페이지 맨 위 **파이프라인 개요**(무엇을 하는지 + 4단계 흐름 + 3산출물 + 화면 읽는 법).
  회의·공유 때 이 화면 하나로 전체 그림이 잡히게 하는 것이 목적.
- 문서별 상세 블록(파싱 결과·STAGE_3 표·OCR/VLM 대조·감사용 원본 증거층·판단 로그)을
  `<details>`로 접었다. 라이브러리 없이 브라우저 내장 토글만 쓴다(폐쇄망 제약과 같은
  이유로 외부 JS 라이브러리 금지). 문서 5개를 한 화면에 다 펴두면 스크롤이 수천 px가
  되어 "전체 그림"이 오히려 안 보였다.

사용:
  uv run python tools/make_review.py                # 전체 → out/review.html
  uv run python tools/make_review.py --only 003     # 파일명 부분 일치
"""

import argparse
import base64
import html
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nh_parsing.ir import Line              # noqa: E402
from nh_parsing.tiling import sort_reading_order  # noqa: E402


def _details(summary_html: str, body_html: str, cls: str = "", open_: bool = False) -> str:
    """`<details>` 토글 래퍼 — 라이브러리 없이 브라우저 내장 기능만 쓴다.

    문서 5개를 전부 펴서 그리면 스크롤이 수천 px 가 되어 "전체 그림"이 안 보인다
    (2026-08-04). summary 에는 접힌 채로도 판단할 수 있게 핵심 숫자를 넣는다.
    """
    return (
        f'<details class="toggle {cls}"{" open" if open_ else ""}>'
        f'<summary class="sechead">{summary_html}</summary>{body_html}</details>'
    )


# 출처 태그 → (약어, 색, 설명)
SOURCE_META = {
    "ocr": ("O", "#2563eb", "OCR 인식"),
    "digital": ("D", "#059669", "디지털 텍스트 추출"),
    "vlm": ("V", "#7c3aed", "VLM"),
    "vlm_sweep": ("S", "#db2777", "VLM 스윕 회수"),
    "vlm_region": ("R", "#0891b2", "VLM 영역통독"),
}


def _region_evidence_html(page: dict) -> tuple[str, int, int]:
    """감사용 원본 증거층 — 영역별 라인(출처·신뢰도 태그)을 읽기순서로 평면 나열.

    2026-08-04: 섹션(의미 묶음) 기반 트리를 없앴다. `page["sections"]` 가 항상 빈
    리스트라(섹션 생성 자체를 파싱에서 제거, vlm_judge 모듈 주석 참조) 예전 렌더러는
    모든 영역이 "섹션 미지정" 단일 버킷으로 몰려 QA 지표로 쓸모가 없었다. 정렬 규칙은
    llm_view.build_page_view 와 동일(카드 → 위→아래 → 좌→우) — 화면에 보이는 순서가
    실제 STAGE_3 입력 순서와 일치해야 눈으로 대조할 수 있다.

    미배정 낱줄도 같은 블록 끝에 붙인다(2026-08-04, 예전엔 별도 블록) — 토글 하나로
    "이 페이지에서 진짜 파싱이 잡은 전부"를 한 번에 펼쳐보게 한다.

    반환: (html, 영역 개수, 미배정 줄 수) — 뒤 둘은 접힌 summary 표시용.
    """
    ordered = sorted(
        page.get("regions", []),
        key=lambda r: (
            r.get("card_no") or 0,
            r["bbox"][1] if r.get("bbox") else 0,
            r["bbox"][0] if r.get("bbox") else 0,
        ),
    )
    blocks = []
    for r in ordered:
        raw = r.get("lines", [])
        lines = sort_reading_order([Line(**l) for l in raw]) if len(raw) > 1 else [Line(**l) for l in raw]
        line_html = "".join(_line_html(l.model_dump()) for l in lines) or '<div class="line empty">(텍스트 없음)</div>'
        meta_bits = [f"역할 {html.escape(str(r.get('role', '')))}"]
        if r.get("role_confidence") is not None:
            meta_bits.append(f"conf {r['role_confidence']}")
        if r.get("card_no"):
            meta_bits.append(f"카드{r['card_no']}")
        if r.get("bbox"):
            meta_bits.append(f"bbox {r['bbox']}")
        head = (
            f'<div class="sechead"><b>{html.escape(r["region_id"])}</b>'
            f'<span class="meta">{" · ".join(meta_bits)}</span></div>'
        )
        blocks.append(f'<div class="sec">{head}{line_html}</div>')

    unassigned = page.get("unassigned_lines", [])
    if unassigned:
        u_html = "".join(_line_html(l) for l in unassigned)
        blocks.append(
            '<div class="sec unassigned">'
            f'<div class="sechead bad">⚠ 미배정 — 어느 영역에도 귀속 안 됨 ({len(unassigned)}줄)</div>'
            f'{u_html}</div>'
        )
    return ("".join(blocks) or '<p class="evidence-label">(영역 없음)</p>'), len(ordered), len(unassigned)


def _page_stats(page: dict) -> dict:
    regions = page.get("regions", [])
    total_lines = sum(len(r.get("lines", [])) for r in regions)
    unassigned = len(page.get("unassigned_lines", []))
    low_conf = 0
    for r in regions:
        for l in r.get("lines", []):
            c = l.get("confidence")
            if c is not None and c < 0.8:
                low_conf += 1
    return {
        "regions": len(regions), "total": total_lines + unassigned,
        "unassigned": unassigned, "low_conf": low_conf,
    }


def _line_html(line: dict) -> str:
    text = html.escape(line.get("text", ""))
    src = line.get("source", "?")
    abbr, color, _ = SOURCE_META.get(src, ("?", "#6b7280", src))
    conf = line.get("confidence")
    conf_txt = f"{conf:.2f}" if conf is not None else "—"
    low = " low" if (conf is not None and conf < 0.8) else ""
    return (
        f'<div class="line{low}">'
        f'<span class="tag" style="background:{color}">{abbr}</span>'
        f'<span class="conf">{conf_txt}</span>'
        f'<span class="txt">{text}</span>'
        f'</div>'
    )


def _rows_from_lines(lines: list[dict]) -> list[str]:
    """읽기순서 정렬된 라인들을 같은 '행'끼리 묶어 한 줄 텍스트로 복원한다.

    OCR 은 한 시각적 줄을 여러 박스로 쪼개 검출한다(예: '1천원 이상' | '30만원' |
    '이하(원단위)' 가 같은 y 에 x 만 다르게). 박스마다 줄바꿈하면 한 줄이 3줄로
    보이므로, 세로 50% 이상 겹치는 이웃 박스는 공백으로 합쳐 실제 줄 모양을 만든다.
    """
    rows: list[list[dict]] = []
    for l in lines:
        b = l.get("bbox")
        if b and rows and rows[-1][-1].get("bbox"):
            prev = rows[-1]
            r_top = min(x["bbox"][1] for x in prev)
            r_bot = max(x["bbox"][3] for x in prev)
            overlap = min(r_bot, b[3]) - max(r_top, b[1])
            height = min(b[3] - b[1], r_bot - r_top)
            if height > 0 and overlap / height >= 0.5:
                prev.append(l)
                continue
        rows.append([l])
    return [" ".join(x.get("text", "") for x in row).strip() for row in rows]


def _llm_view_html(page: dict) -> tuple[str, int]:
    """최종 파싱 결과를 'LLM 전달 형태'로 렌더 — 실제 산출물과 같은 lean 투영을 쓴다.

    라이브러리 llm_view.build_page_view 를 단일 출처로 재사용(out/llm_view/*.json 과
    글자 그대로 동일). 2026-08-03 부터 섹션 계층이 없어 영역이 읽기순서(카드→위아래→
    좌우)로 평면 나열된다 — 장식예시 필터도 같은 시점에 없앴다(더는 걸러내지 않는다).

    반환: (내용 html, 영역 개수) — 개수는 접힌 summary 에 표시한다.
    """
    from nh_parsing.ir import AdPage
    from nh_parsing.llm_view import build_page_view

    view = build_page_view(AdPage(**page))
    parts = []
    body_lines = [
        f"  {r['region_id']} ({r.get('role', '')}): {html.escape(t)}"
        for r in view["regions"] for t in r["text"].split("\n")
    ]
    if body_lines:
        parts.append("<b>【영역 (읽기순서)】</b>\n" + "\n".join(body_lines))
    if view.get("unassigned"):
        u = "\n".join("  " + html.escape(t) for t in view["unassigned"].split("\n"))
        parts.append(f'<b>【영역 미배정 낱줄】</b>\n{u}')
    text = "\n\n".join(parts) if parts else "(파싱 결과 없음)"
    return f'<pre class="llmtext">{text}</pre>', len(view["regions"])


# 판독 관계(truncation.classify_reading) → 검수화면 표시. 딱지와 색이 곧 우선순위다.
_RELATION_VIEW = {
    "tail_cut":  ("뒷부분 잘림 — 정본이 더 완전", "rel-bad"),
    "diverged":  ("정본과 불일치 — 원본 확인 필요", "rel-bad"),
    "expanded":  ("정본보다 많이 읽음 — 회수 가능", "rel-good"),
    "head_drop": ("앞 항목명 생략 — 값은 온전", "rel-ok"),
    "same":      ("표기 차이", "rel-ok"),
}


def _region_vlm_compare_html(page: dict) -> str:
    """OCR 정본 ↔ VLM 통독 후보 대조 — 검수자가 어느 쪽이 맞는지 눈으로 판정하는 패널.

    **B안 전환 때 죽어 있던 것을 되살렸다(2026-07-29).** 예전 A안에서는 통독이 정본을
    덮어쓰고 원본이 ocr_lines 로 강등됐기 때문에 여기서 ocr_lines 있는 영역만 그렸다.
    지금은 정본을 안 덮으므로(B안) ocr_lines 가 항상 비어 **후보 162개가 있는데 화면엔
    0개가 나오고 있었다.** 가장 쓸모 있는 진단(OCR 과 VLM 이 갈린 자리)이 통째로
    안 보였다.

    내용이 같은 후보는 빼고 **갈린 것만** 보여준다 — 162개를 다 그리면 정작 봐야 할
    몇 건이 묻힌다. 위험한 관계(잘림·불일치)를 위로 올린다.
    """
    rows: list[tuple[int, str]] = []
    for r in page.get("regions", []):
        cand = (r.get("vlm_reading") or "").strip()
        if not cand:
            continue
        ocr_txt = "\n".join(l.get("text", "") for l in r.get("lines", []))
        rel = r.get("vlm_reading_relation") or "same"
        if rel == "same" and cand == ocr_txt.strip():
            continue  # 완전히 같으면 볼 게 없다
        label, cls = _RELATION_VIEW.get(rel, ("판정 없음", "rel-ok"))
        prec, cov = r.get("vlm_reading_score"), r.get("vlm_reading_coverage")
        order = 0 if cls == "rel-bad" else (1 if cls == "rel-good" else 2)
        rows.append((order,
            '<div class="cmprow">'
            f'<div class="cmpid">{html.escape(r.get("region_id", "?"))} '
            f'<span class="meta">역할 {html.escape(str(r.get("role", "")))}</span> '
            f'<span class="rel {cls}">{html.escape(label)}</span> '
            f'<span class="meta">정밀도 {prec} · 커버리지 {cov}</span></div>'
            '<div class="cmpcols">'
            '<div class="cmpcol ocr"><div class="cmphd">OCR 정본 (이 값이 쓰인다)</div>'
            f'<pre>{html.escape(ocr_txt) or "(없음)"}</pre></div>'
            '<div class="cmpcol vlm"><div class="cmphd">VLM 통독 후보 (STAGE_3 가 선택)</div>'
            f'<pre>{html.escape(cand)}</pre></div>'
            '</div></div>'))
    if not rows:
        return ""
    rows.sort(key=lambda x: x[0])
    bad = sum(1 for o, _ in rows if o == 0)
    summary = (
        f'OCR 정본 ↔ VLM 통독 후보 대조'
        f'<span class="meta">갈린 영역 {len(rows)}개 (그중 확인 필요 {bad}개) · '
        '정본은 안 덮어쓴다 — 최종 선택은 STAGE_3 몫</span>'
    )
    body = '<div class="regcmp">' + "".join(h for _, h in rows) + '</div>'
    return _details(summary, body, cls="regcmpwrap")


def _load_extracted(extracted_dir: Path, doc_id: str) -> dict | None:
    """STAGE_3 스키마 추출 결과(out/extracted/{doc_id}.json)를 있으면 읽는다.

    예금성이 아닌 문서(대출성 등)나 아직 추출을 안 돌린 문서는 파일이 없을 수 있다 —
    그 경우 이 섹션 자체를 조용히 생략한다(파싱 리뷰는 STAGE_3 유무와 무관하게 항상 봐야 한다).
    """
    p = extracted_dir / f"{doc_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _page_region_bboxes(page: dict) -> dict[str, list[int]]:
    return {r["region_id"]: r["bbox"] for r in page.get("regions", []) if r.get("bbox")}


def _norm_box(bbox: list[int], cw: int, ch: int) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    return (100 * x0 / cw, 100 * y0 / ch, 100 * (x1 - x0) / cw, 100 * (y1 - y0) / ch)


def _status_label(v: dict, status_meta: dict, absence_meta: dict) -> tuple[str, str]:
    """필드 한 줄의 (CSS class, 표시문구). 미발견이면 3분류 라벨이 우선한다."""
    cls, label = status_meta.get(v.get("status"), ("", v.get("status", "")))
    absence = v.get("absence") or {}
    kind = absence.get("kind")
    if kind and kind in absence_meta:
        cls, label = absence_meta[kind]
        if kind == "미표시":
            label = f"{label}({absence.get('obligation', '')})"
    return cls, label


def _stage3_html(page: dict, extracted: dict | None, is_first_page: bool) -> tuple[str, str]:
    """STAGE_3 필드 결과 — 값/상태 표 + 원본 위 근거영역 하이라이트(hover 연동).

    found: 값 있음(초록) / not_found: 광고에 원래 없음(회색, 결함 아님 — 오히려
    심의에서 가장 중요한 "누락 의심" 신호) / uncertain: 값은 있으나 확신 낮음(주황).
    표 행에 마우스를 올리면 그 필드의 근거 region 이 왼쪽 이미지 위에서 하이라이트된다
    (F-012 요구사항 대응 프로토타입).

    not_found 필드는 정의상 evidence(근거 region)가 없어 페이지 필터링 대상이 아니므로,
    문서의 첫 페이지에서만 한 번 표시한다(모든 페이지에 반복 노출하지 않되, 최소 한 번은
    반드시 보여야 한다 — 조용한 실패 금지 원칙, 이전에 실측된 결함: not_found 가 페이지별
    evidence 필터에 걸려 어느 표에도 안 나타났었다).

    반환: (표 html, 오버레이 div html) — 오버레이는 이미지 위에, 표는 텍스트 컬럼에 배치한다.
    """
    if extracted is None:
        return "", ""
    pno = page["page_no"]
    prefix = f"p{pno}_"
    boxes = _page_region_bboxes(page)
    cw, ch = page.get("canvas_w"), page.get("canvas_h")

    overlay_divs = []
    rows = []

    def add_overlay(key: str, region_ids: list[str], css_class: str) -> bool:
        placed = False
        for rid in region_ids or []:
            if not rid.startswith(prefix) or rid not in boxes or not (cw and ch):
                continue
            l, t, w, h = _norm_box(boxes[rid], cw, ch)
            overlay_divs.append(
                f'<div class="hlbox {css_class}" data-key="{html.escape(key)}" '
                f'style="left:{l:.2f}%;top:{t:.2f}%;width:{w:.2f}%;height:{h:.2f}%"></div>'
            )
            placed = True
        return placed

    status_meta = {
        "found": ("ok", "있음"),
        "not_found": ("na", "없음"),
        "uncertain": ("warn", "확신낮음"),
    }
    # 미발견의 3분류를 그대로 보여준다. 예전엔 not_found 를 전부 "원본에 미존재 — 결함 아님"
    # 으로 적었는데, 그 안에 '표시했어야 하는데 없음'이 섞여 있었다. "미표시"는 위반
    # 여부를 판정한 게 아니라 사실 관측이다 — 최종 심의는 하류(RAG/DB 엔진 + 담당자) 몫.
    absence_meta = {
        "미표시": ("miss", "미표시 — 표시의무 있음, 확인 필요"),
        "해당없음": ("na", "해당없음(이 유형엔 성립 안 함)"),
        "판정제외": ("na", "판정제외"),
        "확인필요": ("warn", "확인필요"),
    }
    any_row = False
    counts = {"ok": 0, "na": 0, "warn": 0, "miss": 0}
    for key, v in (extracted.get("fields") or {}).items():
        ev = v.get("evidence") or []
        on_this_page = any(rid.startswith(prefix) for rid in ev)
        if not on_this_page:
            # 근거가 이 페이지에 없는 필드는, not_found(근거 자체가 없는 게 정상)일 때만
            # 첫 페이지에서 1회 노출한다. found/uncertain인데 이 페이지에 근거가 없으면
            # 다른 페이지 표에서 이미 보여줄 것이므로 여기선 건너뛴다.
            if not (v.get("status") == "not_found" and is_first_page):
                continue
        cls, label = _status_label(v, status_meta, absence_meta)
        counts[cls] = counts.get(cls, 0) + 1
        has_box = add_overlay(key, ev, f"st-{'bad' if cls == 'miss' else cls}")
        val = v.get("value")
        val_txt = " | ".join(val) if isinstance(val, list) else str(val or "")
        rows.append(
            f'<tr class="fieldrow{" hoverable" if has_box else ""}'
            f'{" missrow" if cls == "miss" else ""}" data-key="{html.escape(key)}">'
            f'<td class="k">{html.escape(key)}</td>'
            f'<td class="v">{html.escape(val_txt) or "—"}</td>'
            f'<td class="st {cls}">{label}</td>'
            f'<td class="note">{html.escape(v.get("note",""))}</td></tr>'
        )
        any_row = True

    event_rows = []
    for i, ev_item in enumerate(extracted.get("events") or [], 1):
        for key, v in ev_item.items():
            ev = v.get("evidence") or []
            on_this_page = any(rid.startswith(prefix) for rid in ev)
            if not on_this_page and not (v.get("status") == "not_found" and is_first_page):
                continue
            gkey = f"event{i}.{key}"
            cls, label = _status_label(v, status_meta, absence_meta)
            counts[cls] = counts.get(cls, 0) + 1
            has_box = add_overlay(gkey, ev, f"st-{'bad' if cls == 'miss' else cls}")
            val = v.get("value")
            val_txt = " | ".join(val) if isinstance(val, list) else str(val or "")
            event_rows.append(
                f'<tr class="fieldrow{" hoverable" if has_box else ""}'
                f'{" missrow" if cls == "miss" else ""}" data-key="{html.escape(gkey)}">'
                f'<td class="k">이벤트{i}.{html.escape(key)}</td>'
                f'<td class="v">{html.escape(val_txt) or "—"}</td>'
                f'<td class="st {cls}">{label}</td>'
                f'<td class="note">{html.escape(v.get("note",""))}</td></tr>'
            )

    unmapped_rows = []
    for j, u in enumerate(extracted.get("unmapped") or []):
        ev = u.get("evidence") or []
        if not any(rid.startswith(prefix) for rid in ev):
            continue
        ukey = f"unmapped{j}"
        kind = u.get("kind", "")
        cls = "warn" if kind == "심의관련_필드없음" else ""
        has_box = add_overlay(ukey, ev, f"st-{cls or 'muted'}")
        unmapped_rows.append(
            f'<tr class="fieldrow{" hoverable" if has_box else ""}" data-key="{html.escape(ukey)}">'
            f'<td class="k">미배정</td>'
            f'<td class="v">{html.escape(u.get("text",""))}</td>'
            f'<td class="st {cls}">{html.escape(kind)}</td>'
            f'<td class="note">{html.escape(u.get("reason",""))}</td></tr>'
        )

    if not (rows or event_rows or unmapped_rows):
        return "", ""

    parts = [
        '<table class="stage3tbl"><thead><tr><th>필드</th><th>값</th><th>상태</th><th>비고</th></tr></thead><tbody>',
        "".join(rows),
    ]
    if event_rows:
        parts.append('<tr class="grouphead"><td colspan="4">이벤트 오버레이</td></tr>')
        parts.append("".join(event_rows))
    if unmapped_rows:
        parts.append('<tr class="grouphead"><td colspan="4">미배정(스키마 공백 후보 포함)</td></tr>')
        parts.append("".join(unmapped_rows))
    parts.append("</tbody></table>")

    summary = (
        f'STAGE_3 스키마 추출 결과'
        f'<span class="meta">있음 {counts["ok"]} · 없음 {counts["na"]} · '
        f'확신낮음 {counts["warn"]} · <b class="miss-inline">미표시 {counts["miss"]}</b> · '
        f'근거커버리지={extracted.get("coverage", {}).get("region_coverage")} · '
        f'행에 마우스를 올리면 왼쪽 이미지에 근거 영역이 표시됩니다</span>'
    )
    body = f'<div class="stage3">{"".join(parts)}</div>'
    return _details(summary, body, cls="stage3wrap", open_=True), "".join(overlay_divs)


def _img_data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def _page_html(parsed: dict, page: dict, preview_dir: Path, extracted: dict | None) -> str:
    st = _page_stats(page)
    pno = page["page_no"]
    doc_id = parsed["doc_id"]

    stage3_table, stage3_overlay = _stage3_html(page, extracted, is_first_page=(pno == 1))

    # 미리보기 이미지 (있으면 내장) — STAGE_3 근거 하이라이트는 이미지 위에 절대좌표 오버레이
    uri = _img_data_uri(preview_dir / f"{doc_id}_p{pno}.jpg")
    if uri:
        img_html = f'<div class="imgstack"><img src="{uri}" alt="p{pno} preview">{stage3_overlay}</div>'
    else:
        img_html = '<div class="noimg">이미지 없음<br>(HWP 디지털 추출 — 좌표 없음)</div>'

    triage = page.get("triage") or {}
    triage_txt = ""
    if triage:
        triage_txt = f' · triage=<b>{html.escape(str(triage.get("verdict","")))}</b>'
        if triage.get("reasons"):
            triage_txt += f' ({html.escape("; ".join(triage["reasons"]))})'

    # 커버리지 요약 막대 — 항상 보이는 한 줄. 상세는 아래 토글에서.
    cov = (
        f'<div class="cov">'
        f'<span class="pill">영역 {st["regions"]}개</span>'
        f'<span class="pill">라인 {st["total"]}줄</span>'
        f'<span class="pill{" bad" if st["unassigned"] else ""}">미배정 {st["unassigned"]}줄</span>'
        f'<span class="pill{" warn" if st["low_conf"] else ""}">저신뢰(&lt;0.8) {st["low_conf"]}줄</span>'
        f'</div>'
    )

    llmview_body, region_n = _llm_view_html(page)
    llmview = _details(
        f'최종 파싱 결과 · LLM 전달 형태'
        f'<span class="meta">영역 {region_n}개 · 읽기순서 정렬 · out/llm_view/*.json 과 동일</span>',
        llmview_body, cls="llmviewwrap",
    )

    parts = [cov, llmview]
    if stage3_table:
        parts.append(stage3_table)

    # 영역별 VLM 통독 대조 (§6 적용 페이지에만 나타남 — OCR vs VLM 나란히)
    regcmp = _region_vlm_compare_html(page)
    if regcmp:
        parts.append(regcmp)

    # 감사용 원본 증거층 (영역별 raw 라인, 출처·신뢰도 태그 포함 + 미배정)
    evidence_body, ev_region_n, ev_unassigned_n = _region_evidence_html(page)
    unassigned_note = f" · 미배정 {ev_unassigned_n}줄" if ev_unassigned_n else ""
    parts.append(_details(
        f'감사용 원본 증거층'
        f'<span class="meta">영역 {ev_region_n}개{unassigned_note} · 출처·신뢰도 태그 포함, 교정 전 raw 라인</span>',
        evidence_body, cls="evidencewrap",
    ))

    # 노트
    notes = page.get("notes", [])
    if notes:
        items = "".join(f"<li>{html.escape(n)}</li>" for n in notes)
        parts.append(_details(
            f'파이프라인 판단 로그<span class="meta">{len(notes)}건</span>',
            f'<div class="notes"><ul>{items}</ul></div>', cls="noteswrap",
        ))

    return (
        f'<div class="page">'
        f'<div class="phead">p{pno} · route=<b>{html.escape(str(page.get("parse_route")))}</b> '
        f'· status={html.escape(str(page.get("parse_status")))} '
        f'· canvas {page.get("canvas_w")}×{page.get("canvas_h")}{triage_txt} '
        f'· 총 {st["total"]}줄</div>'
        f'<div class="cols"><div class="imgcol">{img_html}</div>'
        f'<div class="txtcol">{"".join(parts)}</div></div>'
        f'</div>'
    )


def _doc_html(parsed: dict, preview_dir: Path, extracted_dir: Path) -> tuple[str, dict]:
    doc_id = parsed["doc_id"]
    extracted = _load_extracted(extracted_dir, doc_id)
    pages_html = "".join(
        _page_html(parsed, p, preview_dir, extracted) for p in parsed.get("pages", [])
    )
    agg = {"pages": len(parsed.get("pages", [])), "regions": 0, "lines": 0, "unassigned": 0}
    for p in parsed.get("pages", []):
        st = _page_stats(p)
        agg["regions"] += st["regions"]; agg["lines"] += st["total"]
        agg["unassigned"] += st["unassigned"]
    cov = (extracted or {}).get("coverage") or {}
    agg["found"] = cov.get("fields_found")
    agg["not_found"] = cov.get("fields_not_found")
    agg["miss"] = (cov.get("absence_missing") or 0) + (cov.get("absence_missing_in_events") or 0)
    badge = (
        f'{html.escape(str(parsed.get("product_group")))}/'
        f'{html.escape(str(parsed.get("ad_type")))}'
    )
    header = (
        f'<h2 id="{html.escape(doc_id)}">{html.escape(doc_id)} '
        f'<span class="badge">{badge}</span> '
        f'<span class="src">source={html.escape(str(parsed.get("category_source")))}</span></h2>'
    )
    return f'<div class="doc">{header}{pages_html}</div>', agg


CSS = """
* { box-sizing: border-box; }
body { font-family: 'Malgun Gothic','Segoe UI',sans-serif; margin: 0; color: #1f2328; background: #f6f8fa; }
header.top { background: #0d1117; color: #fff; padding: 16px 24px; position: sticky; top: 0; z-index: 10; }
header.top h1 { margin: 0 0 4px; font-size: 18px; }
header.top .legend { font-size: 12px; color: #9da7b3; }
header.top .legend .tag { margin-left: 10px; }
.wrap { padding: 20px 24px 80px; max-width: 1600px; margin: 0 auto; }
.pipeline-overview { background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:18px 22px; margin-bottom:20px; }
.pipeline-overview h2 { margin:0 0 8px; font-size:16px; }
.pipeline-overview > p { margin:0 0 16px; font-size:13px; color:#57606a; line-height:1.65; }
.flow { display:flex; align-items:stretch; gap:0; margin-bottom:16px; flex-wrap:wrap; }
.flowstep { flex:1; min-width:150px; background:#f6f8fa; border:1px solid #d0d7de; border-radius:6px; padding:10px 12px; }
.flowstep.highlight { background:#e6f4e6; border-color:#1a7f37; }
.fsno { font-size:10px; font-weight:700; color:#8b949e; margin-bottom:2px; }
.fstitle { font-weight:700; font-size:13px; color:#0550ae; margin-bottom:4px; }
.flowstep.highlight .fstitle { color:#116329; }
.fsdesc { font-size:11.5px; color:#57606a; line-height:1.5; }
.flowarrow { display:flex; align-items:center; justify-content:center; padding:0 8px; color:#8b949e; font-size:16px; flex:0 0 auto; }
.outputs-label { font-size:12px; color:#57606a; margin-bottom:8px; }
.outputs { display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap; }
.outbox { flex:1; min-width:180px; border:1px solid #eaecef; border-radius:6px; padding:8px 12px; font-size:12px; color:#57606a; background:#fafbfc; }
.outbox.highlight { border-color:#1a7f37; background:#f0fdf1; }
.outbox b { display:block; color:#1f2328; font-size:13px; margin-bottom:2px; }
.howto { font-size:12px; color:#57606a; line-height:1.75; background:#f6f8fa; border-radius:6px; padding:10px 14px; margin:0; }
.summary { background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:12px 16px; margin-bottom:24px; }
.summary .warncell { color:#a40e26; font-weight:600; }
.summary table { border-collapse: collapse; width: 100%; font-size: 13px; }
.summary th, .summary td { border-bottom:1px solid #eaecef; padding:6px 10px; text-align:left; }
.summary th { color:#57606a; font-weight:600; }
.summary a { color:#0969da; text-decoration:none; }
.doc { margin-bottom: 40px; }
h2 { font-size: 17px; border-bottom: 2px solid #d0d7de; padding-bottom: 8px; }
.badge { background:#dafbe1; color:#116329; font-size:12px; padding:2px 8px; border-radius:12px; vertical-align:middle; }
.src { font-size:12px; color:#8b949e; font-weight:normal; }
.page { background:#fff; border:1px solid #d0d7de; border-radius:8px; margin-bottom:20px; overflow:hidden; }
.phead { background:#f6f8fa; border-bottom:1px solid #d0d7de; padding:8px 14px; font-size:13px; color:#57606a; }
.cols { display:flex; gap:0; align-items:flex-start; }
.imgcol { flex:0 0 40%; max-width:40%; padding:14px; border-right:1px solid #eaecef; position:sticky; top:70px; align-self:flex-start; }
.imgcol img { width:100%; height:auto; border:1px solid #d0d7de; border-radius:4px; display:block; }
.imgstack { position:relative; }
.noimg { padding:40px; text-align:center; color:#8b949e; background:#f6f8fa; border-radius:4px; }
.hlbox { position:absolute; border:2px solid transparent; border-radius:2px; opacity:0; pointer-events:none; transition:opacity .12s; }
.hlbox.active { opacity:1; }
.hlbox.st-ok { border-color:#1a7f37; background:rgba(26,127,55,0.18); }
.hlbox.st-warn { border-color:#9a6700; background:rgba(154,103,0,0.18); }
.hlbox.st-bad { border-color:#cf222e; background:rgba(207,34,46,0.18); }
.hlbox.st-muted { border-color:#57606a; background:rgba(87,96,106,0.16); }
details.toggle { margin-bottom:14px; border:1px solid #eaecef; border-radius:6px; overflow:hidden; }
details.toggle > summary { cursor:pointer; list-style:none; }
details.toggle > summary::-webkit-details-marker { display:none; }
details.toggle > summary::before { content:'▸ '; color:#8b949e; font-size:11px; }
details.toggle[open] > summary::before { content:'▾ '; }
details.toggle > summary:hover { filter:brightness(0.97); }
details.stage3wrap { border-color:#c6e6c6; }
details.stage3wrap > summary { background:#e6f4e6; color:#116329; }
details.stage3wrap .miss-inline { color:#a40e26; }
details.llmviewwrap { border-color:#c8e1ff; }
details.llmviewwrap > summary { background:#ddf4ff; color:#0550ae; }
details.regcmpwrap { border-color:#7fd4e0; }
details.regcmpwrap > summary { background:#e0f7fb; color:#075e6b; }
details.evidencewrap > summary, details.noteswrap > summary { background:#f6f8fa; color:#1f2328; }
.stage3tbl { border-collapse:collapse; width:100%; font-size:12.5px; }
.stage3tbl th, .stage3tbl td { border:1px solid #eaecef; padding:5px 8px; text-align:left; vertical-align:top; }
.stage3tbl th { background:#f6f8fa; color:#57606a; }
.stage3tbl .k { white-space:nowrap; color:#0550ae; font-weight:600; }
.stage3tbl .st { white-space:nowrap; font-weight:600; }
.stage3tbl .st.ok { color:#116329; }
.stage3tbl .st.warn { color:#9a6700; }
.stage3tbl .st.bad { color:#57606a; font-weight:normal; }
/* 미표시 = 표시의무 있는데 없음, 확인 필요(빨강, 강조) vs 해당없음 = 이 유형엔 성립 안 함(회색, 결함 아님) */
.stage3tbl .st.miss { color:#a40e26; }
.stage3tbl .st.na { color:#57606a; font-weight:normal; }
.stage3tbl tr.fieldrow.missrow { background:#fff5f5; }
.stage3tbl .note { color:#8b949e; font-size:11.5px; }
.stage3tbl tr.grouphead td { background:#eef1f4; color:#57606a; font-weight:600; font-size:11.5px; padding:4px 8px; }
.stage3tbl tr.hoverable { cursor:pointer; }
.stage3tbl tr.hoverable:hover, .stage3tbl tr.row-active { background:#fff8c5; }
.txtcol { flex:1; padding:14px; min-width:0; }
.cov { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }
.pill { font-size:12px; padding:3px 9px; border-radius:12px; background:#eaecef; color:#57606a; }
.pill.ok { background:#dafbe1; color:#116329; }
.pill.warn { background:#fff8c5; color:#7d4e00; }
.pill.bad { background:#ffebe9; color:#a40e26; }
.llmtext { margin:0; padding:10px 12px; font-family:'Malgun Gothic','Segoe UI',sans-serif; font-size:13px; line-height:1.65; white-space:pre-wrap; word-break:break-word; background:#f7fbff; color:#1f2328; }
.llmtext b { color:#0550ae; display:inline-block; margin-top:6px; }
.evidence-label { font-size:11px; color:#8b949e; margin:0 0 6px; }
.cmprow { border-top:1px solid #d5eef2; padding:6px 10px; }
.cmpid { font-size:12px; font-weight:600; color:#075e6b; margin-bottom:4px; }
.rel { font-size:10px; font-weight:700; padding:1px 6px; border-radius:9px; }
.rel-bad  { background:#ffe3e3; color:#b42318; }   /* 잘림·불일치 — 먼저 볼 것 */
.rel-good { background:#e3f6e5; color:#1a7f37; }   /* 회수 — 후보가 유리 */
.rel-ok   { background:#eef1f4; color:#57606a; }   /* 표기 차이 — 대개 무해 */
.cmpcols { display:flex; gap:10px; }
.cmpcol { flex:1; min-width:0; border:1px solid #eaecef; border-radius:4px; overflow:hidden; }
.cmpcol.ocr { border-color:#c8d7ff; }
.cmpcol.vlm { border-color:#7fd4e0; }
.cmphd { font-size:10px; padding:3px 8px; background:#f6f8fa; color:#57606a; border-bottom:1px solid #eaecef; }
.cmpcol pre { margin:0; padding:6px 8px; font-family:'Malgun Gothic','Segoe UI',sans-serif; font-size:12px; line-height:1.5; white-space:pre-wrap; word-break:break-word; color:#1f2328; }
.sec { margin-bottom:14px; border:1px solid #eaecef; border-radius:6px; overflow:hidden; }
.sec.unassigned { border-color:#ffb3ab; }
.sechead { background:#f6f8fa; padding:6px 10px; font-size:13px; border-bottom:1px solid #eaecef; display:flex; justify-content:space-between; }
.sechead.free { color:#7d4e00; background:#fff8c5; }
.sechead.bad { color:#a40e26; background:#ffebe9; }
.sechead.illus { color:#8b949e; background:#eef1f4; }
.sec.illus { opacity:0.6; }
.ibadge { font-size:10px; background:#8b949e; color:#fff; padding:1px 6px; border-radius:8px; margin-left:8px; }
.pill.illus { background:#eef1f4; color:#57606a; }
.sechead .meta { font-weight:normal; color:#8b949e; font-size:11px; }
.line { display:flex; align-items:baseline; gap:8px; padding:2px 10px; font-size:13px; line-height:1.5; }
.line:hover { background:#f6f8fa; }
.line.low { background:#fff8c5; }
.line.low:hover { background:#fdf0a8; }
.line.empty { color:#8b949e; font-style:italic; }
.tag { color:#fff; font-size:10px; font-weight:700; padding:1px 5px; border-radius:3px; flex:0 0 auto; }
.conf { color:#8b949e; font-size:11px; flex:0 0 34px; font-variant-numeric:tabular-nums; }
.txt { flex:1; word-break:break-word; }
.fields { margin:14px 0; }
.fields table { border-collapse:collapse; width:100%; font-size:13px; margin-top:6px; }
.fields th, .fields td { border:1px solid #eaecef; padding:5px 9px; text-align:left; vertical-align:top; }
.fields th { background:#f6f8fa; color:#57606a; }
.fields .k { white-space:nowrap; color:#0550ae; font-weight:600; }
.fields .fl { font-size:11px; color:#8b949e; }
.flag.bad { color:#a40e26; }
.flag.ok { color:#116329; }
.notes { padding:8px 12px 2px; }
.notes ul { margin:0; padding-left:20px; font-size:12px; color:#57606a; }
.notes li { margin:2px 0; }
"""

# 표 행에 hover 하면 같은 data-key 를 가진 이미지 위 오버레이 box 만 밝힌다.
# 라이브러리 없이 순수 JS — 폐쇄망 제약(외부 리소스 금지)과 동일한 이유로 CDN 미사용.
HOVER_JS = """
document.querySelectorAll('.stage3tbl tr.hoverable').forEach(function(row) {
  var key = row.getAttribute('data-key');
  var page = row.closest('.page');
  if (!page) return;
  var boxes = page.querySelectorAll('.hlbox[data-key="' + CSS.escape(key) + '"]');
  row.addEventListener('mouseenter', function() {
    boxes.forEach(function(b) { b.classList.add('active'); });
  });
  row.addEventListener('mouseleave', function() {
    boxes.forEach(function(b) { b.classList.remove('active'); });
  });
});
"""


def _pipeline_overview_html() -> str:
    """처음 보는 사람용 오리엔테이션 — 페이지 맨 위 고정. 접지 않는다.

    2026-08-04: 팀 공유 때 이 화면 하나로 "뭘 하는 시스템이고 어떻게 읽으면 되는지"가
    잡히게 해달라는 요청으로 추가. 외부 이미지·라이브러리 없이 순수 HTML/CSS 로만
    흐름도를 그린다(폐쇄망 제약과 동일한 이유).
    """
    steps = [
        ("0~1", "글자 획득", "PDF/PNG/HWP 에서 OCR·디지털 텍스트로 글자와 좌표를 뽑는다", False),
        ("2", "구조 정리", "좌우 카드 구분, 영역별 역할(제목/유의사항 등) 판정", False),
        ("3", "통합 판독", "OCR 이 놓친 글자를 VLM 이 재확인 — 정본은 안 덮어씀", False),
        ("4", "스키마 추출", "상품명·금리 등 값 채우기 + 근거 위치 지목 (STAGE_3)", True),
    ]
    flow = "".join(
        (f'<div class="flowarrow">→</div>' if i else "")
        + f'<div class="flowstep{" highlight" if hi else ""}">'
        f'<div class="fsno">{no}</div><div class="fstitle">{html.escape(title)}</div>'
        f'<div class="fsdesc">{html.escape(desc)}</div></div>'
        for i, (no, title, desc, hi) in enumerate(steps)
    )
    outputs = "".join(
        f'<div class="outbox{" highlight" if hi else ""}"><b>{name}</b>{html.escape(desc)}</div>'
        for name, desc, hi in [
            ("out/json", "전체 기록 — 좌표·신뢰도·출처, 감사용", False),
            ("out/llm_view", "정제 텍스트만 — 다음 단계(스키마 추출) 입력", False),
            ("out/extracted", "구조화 필드 — 이 화면 STAGE_3 표가 이 값", True),
        ]
    )
    return (
        '<section class="pipeline-overview">'
        '<h2>이 화면은 무엇인가</h2>'
        '<p>광고 이미지(PDF·PNG·HWP)에서 글자를 뽑고, 심의 스키마에 맞춰 값을 채운 결과를 '
        '원본과 나란히 놓고 검토하는 화면입니다. <b>"이 광고가 규정 위반인가"는 판정하지 '
        '않습니다</b> — 값이 있는지, 없으면 왜 없는지(표시 의무 누락인지 / 이 상품 유형엔 '
        '원래 없는 개념인지)까지만 파악하고, 최종 판단은 규정과 대조하는 다음 단계 '
        '(RAG/DB 엔진 + 심의 담당자) 몫입니다.</p>'
        f'<div class="flow">{flow}</div>'
        '<div class="outputs-label">한 문서를 처리하면 산출물이 3개 나옵니다 — 목적이 서로 달라 합치지 않습니다</div>'
        f'<div class="outputs">{outputs}</div>'
        '<p class="howto"><b>이 화면 보는 법</b> — 문서마다 페이지별로 왼쪽엔 원본 이미지에 '
        '파싱이 잡은 위치를 <b>마젠타 박스</b>로 표시합니다(가는 선=한 줄, 굵은 선=여러 줄을 '
        '묶은 한 영역 — 역할별 색은 안 씁니다, 모델 판단이라 실행마다 미세하게 바뀔 수 있어서). '
        '오른쪽엔 그 페이지의 처리 결과가 <b>토글로 접혀서</b> 나열됩니다 — 제목을 누르면 펼쳐집니다. '
        'STAGE_3 표에서 <b>found</b>=값 찾음, <b>not_found</b>=값 없음(정상일 수도 있음), '
        '<b>미표시</b>=표시 의무가 있는데 없어서 확인이 필요한 값입니다. 표 행에 마우스를 올리면 '
        '왼쪽 이미지에 그 값의 근거 위치가 하이라이트됩니다.</p>'
        '</section>'
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "out" / "review.html")
    parser.add_argument(
        "--src", type=Path, default=ROOT / "out",
        help="run_nhdata --out 과 대칭. 이 폴더의 json/·previews/ 를 읽는다 (기본 out/)",
    )
    args = parser.parse_args()

    preview_dir = args.src / "previews"
    extracted_dir = args.src / "extracted"
    json_files = sorted((args.src / "json").glob("*.json"))
    if args.only:
        json_files = [p for p in json_files if args.only in p.stem]
    if not json_files:
        print(f"{args.src / 'json'} 에 결과 없음 — 먼저 run_nhdata.py 실행")
        return

    docs_html = []
    summary_rows = []
    for jf in json_files:
        parsed = json.loads(jf.read_text(encoding="utf-8"))
        doc_html, agg = _doc_html(parsed, preview_dir, extracted_dir)
        docs_html.append(doc_html)
        did = parsed["doc_id"]
        found_txt = f'{agg["found"]} / {agg["not_found"]}' if agg["found"] is not None else "—"
        summary_rows.append(
            f'<tr><td><a href="#{html.escape(did)}">{html.escape(did)}</a></td>'
            f'<td>{agg["pages"]}</td><td>{agg["regions"]}</td><td>{agg["lines"]}</td>'
            f'<td{" class=\"warncell\"" if agg["unassigned"] else ""}>{agg["unassigned"]}</td>'
            f'<td>{found_txt}</td>'
            f'<td{" class=\"warncell\"" if agg["miss"] else ""}>{agg["miss"] if agg["found"] is not None else "—"}</td></tr>'
        )

    legend = " ".join(
        f'<span class="tag" style="background:{c}">{a}</span> {d}'
        for a, c, d in SOURCE_META.values()
    )
    summary = (
        '<div class="summary"><b>파일별 요약</b> — 각 항목을 눌러 아래 상세로 이동. '
        '미배정↓ · found↑ · 미표시(표시의무 누락)는 실제로 확인할 가치가 있는 것만.'
        '<table><thead><tr><th>파일</th><th>페이지</th><th>영역</th><th>라인</th>'
        '<th>미배정</th><th>found/not_found</th><th>미표시</th></tr></thead>'
        f'<tbody>{"".join(summary_rows)}</tbody></table></div>'
    )

    doc = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>NH 광고심의 파싱 리뷰</title>'
        f'<style>{CSS}</style></head><body>'
        '<header class="top"><h1>NH 광고심의 파싱 결과 리뷰</h1>'
        f'<div class="legend">라인 출처 태그: {legend}</div>'
        '</header>'
        f'<div class="wrap">{_pipeline_overview_html()}{summary}{"".join(docs_html)}</div>'
        f'<script>{HOVER_JS}</script>'
        '</body></html>'
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(doc, encoding="utf-8")
    size_kb = args.out.stat().st_size / 1024
    print(f"→ {args.out}  ({size_kb:.0f} KB, {len(json_files)}개 파일)")
    print("브라우저로 열어서 확인하세요.")


if __name__ == "__main__":
    main()
