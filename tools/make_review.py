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
import hashlib
import html
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nh_parsing.ir import Line, Region      # noqa: E402
from nh_parsing.llm_view import region_order_key  # noqa: E402
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


def _src(path: str, fields: str = "") -> str:
    """이 블록의 값이 **어느 산출물 파일의 어느 필드**에서 온 것인지 딱지.

    2026-08-04: 산출물 보고서(docs/notion_파싱파이프라인-output)가 json 3개를 기준으로
    쓰였는데 화면엔 파일 출처가 없어서, 보고서와 화면을 대조할 때 "이 표가 어느
    파일이냐"를 매번 되물어야 했다. 블록마다 파일명·필드명을 같이 적는다.
    """
    f = f'<span class="srcfields">{html.escape(fields)}</span>' if fields else ""
    return f'<span class="srcfile">{html.escape(path)}</span>{f}'


def _intro(text: str) -> str:
    """토글을 펼쳤을 때 맨 위에 붙는 한두 문장 설명 — 발표·인수인계용(2026-08-04)."""
    return f'<p class="blkintro">{text}</p>'


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
    ordered = sorted(page.get("regions", []), key=lambda r: region_order_key(Region(**r)))
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
        sweep = sum(1 for l in unassigned if l.get("source") == "vlm_sweep")
        blocks.append(
            '<div class="sec unassigned">'
            f'<div class="sechead bad">미배정 — 어느 영역에도 안 붙은 낱줄 ({len(unassigned)}줄)'
            f'<span class="meta">S 태그 {sweep}줄 = OCR 이 못 읽어 VLM 이 건진 문구'
            f'(밴드 근사 좌표라 일부러 안 붙임) · O/D 태그 {len(unassigned) - sweep}줄 = '
            '붙일 영역을 못 찾은 것</span></div>'
            f'{u_html}</div>'
        )
    return ("".join(blocks) or '<p class="evidence-label">(영역 없음)</p>'), len(ordered), len(unassigned)


def _page_stats(page: dict) -> dict:
    """페이지 커버리지 숫자.

    미배정을 **출처별로 가른다**(2026-08-04). 그냥 "미배정 13줄"로 보이면 "13줄을 못
    붙였다 = 결함"으로 읽히는데, 실측은 정반대였다 — 전체 미배정 21줄 중 20줄이
    `vlm_sweep`(⑪ 통짜 스윕이 **OCR 이 아예 못 읽어서 새로 건져온** 문구)이고, OCR
    출처는 1줄(002 `0ㅇ` 노이즈)뿐이다. 스윕 라인은 밴드 근사 좌표라 ⑨ 귀속에서 일부러
    제외한다(좌표를 신뢰할 수 없어서). 즉 **미배정 대부분은 실패가 아니라 추가 회수분**
    이고, 진짜 결함 신호는 `unassigned_ocr` 쪽이다.
    """
    regions = page.get("regions", [])
    total_lines = sum(len(r.get("lines", [])) for r in regions)
    un = page.get("unassigned_lines", [])
    un_sweep = sum(1 for l in un if l.get("source") == "vlm_sweep")
    low_conf = 0
    for r in regions:
        for l in r.get("lines", []):
            c = l.get("confidence")
            if c is not None and c < 0.8:
                low_conf += 1
    return {
        "regions": len(regions), "total": total_lines + len(un),
        "unassigned": len(un), "unassigned_sweep": un_sweep,
        "unassigned_ocr": len(un) - un_sweep, "low_conf": low_conf,
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


def _llm_view_html(page: dict) -> tuple[str, int, int]:
    """최종 파싱 결과를 'LLM 전달 형태'로 렌더 — 실제 산출물과 같은 lean 투영을 쓴다.

    라이브러리 llm_view.build_page_view 를 단일 출처로 재사용(out/llm_view/*.json 과
    글자 그대로 동일). 2026-08-03 부터 섹션 계층이 없어 영역이 읽기순서(카드→위아래→
    좌우)로 평면 나열된다 — 장식예시 필터도 같은 시점에 없앴다(더는 걸러내지 않는다).

    2026-08-04: **통독 후보가 있는 영역은 그 자리에서 나란히 보여준다.** 예전에는 정본만
    나열하고 후보 대조는 아래 별도 블록(③)에만 있었는데, ③은 위험한 것부터 재정렬해
    보여주므로 "이 영역에 후보가 붙어 있었나"를 읽기순서대로 확인할 수 없었다. 여기서는
    llm_view 에 실린 그대로(순서·필드 전부) 보여주는 것이 목적이므로 재정렬하지 않는다.

    후보는 **영역 단위**다(라인 단위가 아니다) — llm_view 의 한 항목이 영역 하나이고
    `text` 안에 줄바꿈으로 여러 줄이 들어간다. 라인 단위 후보(⑫⑬ 재판독)는 out/json 에만
    있고 llm_view 로는 안 넘어간다.

    반환: (내용 html, 영역 개수, 후보 있는 영역 수) — 뒤 둘은 접힌 summary 표시용.
    """
    from nh_parsing.ir import AdPage
    from nh_parsing.llm_view import build_page_view

    view = build_page_view(AdPage(**page))
    blocks, n_cand = [], 0
    for r in view["regions"]:
        head = (
            f'<div class="lvid"><b>{html.escape(r["region_id"])}</b>'
            f'<span class="lvrole">{html.escape(str(r.get("role") or ""))}</span>'
        )
        text_html = html.escape(r["text"])
        cand = (r.get("vlm_reading") or "").strip()
        if not cand:
            blocks.append(
                f'<div class="lvrow">{head}</div>'
                f'<pre class="lvtext">{text_html}</pre></div>'
            )
            continue
        n_cand += 1
        rel = r.get("vlm_reading_relation") or "same"
        label, cls = _RELATION_VIEW.get(rel, ("판정 없음", "rel-ok"))
        head += (
            f'<span class="rel {cls}">{html.escape(label)}</span>'
            f'<span class="meta">정밀도 {r.get("vlm_reading_score")} · '
            f'커버리지 {r.get("vlm_reading_coverage")}</span></div>'
        )
        blocks.append(
            f'<div class="lvrow cand">{head}'
            f'<div class="lvcols">'
            f'<div class="lvcol ocr"><div class="lvhd">text — 파싱 정본 (기록·재현의 기준)</div>'
            f'<pre>{text_html}</pre></div>'
            f'<div class="lvcol vlm"><div class="lvhd">vlm_reading — VLM 통독 후보</div>'
            f'<pre>{html.escape(cand)}</pre></div>'
            f'</div></div>'
        )

    if view.get("unassigned"):
        blocks.append(
            '<div class="lvrow"><div class="lvid"><b>unassigned</b>'
            '<span class="lvrole">영역 미배정 낱줄</span></div>'
            f'<pre class="lvtext">{html.escape(view["unassigned"])}</pre></div>'
        )
    body = "".join(blocks) or '<p class="evidence-label">(파싱 결과 없음)</p>'
    return f'<div class="llmview">{body}</div>', len(view["regions"]), n_cand


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
            '<div class="cmpcol ocr"><div class="cmphd">OCR 정본 — 파싱이 확정한 값</div>'
            f'<pre>{html.escape(ocr_txt) or "(없음)"}</pre></div>'
            '<div class="cmpcol vlm"><div class="cmphd">VLM 통독 후보 — 필드 값으로는 STAGE_3 가 고를 수 있음</div>'
            f'<pre>{html.escape(cand)}</pre></div>'
            '</div></div>'))
    if not rows:
        return ""
    rows.sort(key=lambda x: x[0])
    bad = sum(1 for o, _ in rows if o == 0)
    summary = (
        f'③ OCR 정본 ↔ VLM 통독 후보 대조'
        f'<span class="meta">갈린 영역 {len(rows)}개 (그중 확인 필요 {bad}개)</span>'
        + _src("out/json/*.json",
               "regions[] : lines[].text(정본) ↔ vlm_reading(후보) · 다를 때만 llm_view 에도 실림")
    )
    body = _intro(
        '같은 자리를 <b>OCR 이 읽은 것과 VLM 이 다시 읽은 것이 갈린 영역</b>만 모았습니다. '
        'VLM 판독이 더 정확해 보여도 <b>정본(text)을 덮어쓰지 않습니다</b> — 실행마다 값이 '
        '바뀌면 재현이 안 되기 때문입니다. 그래서 <b>파싱이 확정해 기록·감사·재현의 기준으로 '
        '쓰는 값은 항상 왼쪽(정본)</b>이고, 후보는 별도 필드로 나란히 전달만 됩니다. '
        '단 <b>스키마 필드에 넣을 값</b>은 STAGE_3 가 둘을 보고 고르므로 오른쪽이 채택될 수 '
        '있습니다(그때도 정본은 안 바뀝니다). 딱지 뜻: <b>뒷부분 잘림·불일치</b>=정본을 믿을 것'
        '(먼저 확인), <b>정본보다 많이 읽음</b>=OCR 이 놓친 글자를 후보가 건진 경우, '
        '<b>표기 차이</b>=무해.'
    ) + '<div class="regcmp">' + "".join(h for _, h in rows) + '</div>'
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


def _region_label_overlay(page: dict) -> str:
    """영역 박스 왼쪽 위에 `r002 이미지`(=region_id + 역할) 라벨을 얹는다.

    2026-08-04 요청. **JPG 에 굽지 않고 HTML 절대좌표 div 로 올린다** — 이유 둘:
      ① 역할은 VLM 판단이라 실행 간 97.3% 일치(225개 중 6개 변동)다. 좌표 그림 자체에
         구워 넣으면 "같은 광고인데 실행마다 다른 그림"이 남는다(pipeline._save_preview
         주석과 같은 이유). div 는 켜고 끌 수 있어 좌표 원본을 오염시키지 않는다.
      ② 라벨을 굽자고 전체 재파싱(OCR+VLM 15분)을 돌릴 필요가 없다.

    페이지마다 체크박스로 표시/숨김. 영역이 70개 넘는 페이지도 있어(001 p1=76개)
    항상 켜두면 글자가 겹쳐 원본이 안 보이므로 끌 수 있어야 한다.

    **글자가 안 붙은 영역은 흐리게, 그리고 먼저 그린다**(2026-08-04). PP-StructureV3 가
    거의 같은 자리에 레이아웃 박스를 두 번 내는 일이 있고(올원e `r017`[113,2949,1055,3051]
    ↔ `r019`[113,2950,1056,3050]) 우리 중복 제거는 bbox 완전일치만 걸러서 1px 다른
    쌍은 둘 다 남는다. 라인은 한쪽에만 붙고 다른 쪽은 빈 껍데기가 되는데(224개 중 24개,
    11%), 라벨이 같은 좌표에 겹쳐 그려져 **빈 껍데기 이름이 실제 영역 이름을 덮었다** —
    그림에는 `r019`, 오른쪽 파싱 결과에는 `r017` 로 보여 번호가 틀린 것처럼 읽혔다.
    """
    cw, ch = page.get("canvas_w"), page.get("canvas_h")
    if not (cw and ch):
        return ""
    empty_html, real_html = [], []
    for r in page.get("regions", []):
        if not r.get("bbox"):
            continue
        l, t, _w, _h = _norm_box(r["bbox"], cw, ch)
        # 화면 폭을 아끼려고 `p1_` 접두는 뺀다(페이지는 블록 머리줄에 이미 있다).
        short = r["region_id"].split("_", 1)[-1]
        role = html.escape(str(r.get("role") or "?"))
        has_lines = bool(r.get("lines"))
        title = f'{r["region_id"]} · 역할 {role} · bbox {r["bbox"]}'
        if not has_lines:
            title += " · 글자가 안 붙은 빈 검출 박스 (거의 같은 자리 중복 검출)"
        body = f'{html.escape(short)} <i>{role}</i>' if has_lines else f'{html.escape(short)} <i>빈 박스</i>'
        span = (
            f'<span class="rlbl{"" if has_lines else " empty"}" '
            f'style="left:{l:.2f}%;top:{t:.2f}%" title="{html.escape(title)}">{body}</span>'
        )
        (real_html if has_lines else empty_html).append(span)
    return "".join(empty_html) + "".join(real_html)


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
        f'② STAGE_3 스키마 추출 결과'
        f'<span class="meta">있음 {counts["ok"]} · 없음 {counts["na"]} · '
        f'확신낮음 {counts["warn"]} · <b class="miss-inline">미표시 {counts["miss"]}</b> · '
        f'근거커버리지={extracted.get("coverage", {}).get("region_coverage")}</span>'
        + _src("out/extracted/*.json", "fields{} / events[] / unmapped[] : value · status · evidence · absence")
    )
    body = _intro(
        '①의 글자를 <b>심의 스키마 필드</b>(상품명·금리·의무고지 등)에 채운 결과입니다. '
        '<b>값이 있는지 / 없으면 왜 없는지</b>까지만 판단하고, 규정 위반 여부는 판정하지 '
        '않습니다. <b>행에 마우스를 올리면</b> 왼쪽 이미지에서 그 값을 뽑아온 위치가 '
        '하이라이트됩니다(근거 추적).'
    ) + f'<div class="stage3">{"".join(parts)}</div>'
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

    # 미리보기 이미지 (있으면 내장) — STAGE_3 근거 하이라이트·영역 라벨은 절대좌표 오버레이
    uri = _img_data_uri(preview_dir / f"{doc_id}_p{pno}.jpg")
    if uri:
        # id 는 ASCII 만 — doc_id 에 한글·괄호·공백이 섞여 있어 해시로 만든다(실행 간 고정).
        chk = f"lbl_{hashlib.md5(doc_id.encode('utf-8')).hexdigest()[:8]}_{pno}"
        img_html = (
            f'<input type="checkbox" class="lblchk" id="{chk}" checked>'
            f'<label class="lblctl" for="{chk}">영역 이름·역할 라벨</label>'
            f'<div class="imgstack"><img src="{uri}" alt="p{pno} preview">'
            f'{stage3_overlay}{_region_label_overlay(page)}</div>'
        )
    else:
        img_html = '<div class="noimg">이미지 없음<br>(HWP 디지털 추출 — 좌표 없음)</div>'

    triage = page.get("triage") or {}
    triage_txt = ""
    if triage:
        triage_txt = f' · triage=<b>{html.escape(str(triage.get("verdict","")))}</b>'
        if triage.get("reasons"):
            triage_txt += f' ({html.escape("; ".join(triage["reasons"]))})'

    # 커버리지 요약 막대 — 항상 보이는 한 줄. 상세는 아래 토글에서.
    # 미배정은 출처를 갈라 보여준다 — VLM 스윕 회수분은 '못 붙인 실패'가 아니라
    # 'OCR 이 못 읽어 새로 건진 글자'다(_page_stats 주석의 실측 참조).
    un_detail = ""
    if st["unassigned"]:
        un_detail = (
            f'<span class="sub">VLM 회수 {st["unassigned_sweep"]} · '
            f'OCR {st["unassigned_ocr"]}</span>'
        )
    cov = (
        f'<div class="cov">'
        f'<span class="pill">영역 {st["regions"]}개</span>'
        f'<span class="pill">라인 {st["total"]}줄</span>'
        f'<span class="pill{" bad" if st["unassigned_ocr"] else ""}" '
        f'title="어느 영역에도 안 붙은 낱줄. VLM 회수분은 밴드 근사 좌표라 일부러 안 붙인 것이고, '
        f'텍스트는 다음 단계로 전달된다. 결함 신호는 OCR 쪽 숫자다.">'
        f'미배정 {st["unassigned"]}줄{un_detail}</span>'
        f'<span class="pill{" warn" if st["low_conf"] else ""}">저신뢰(&lt;0.8) {st["low_conf"]}줄</span>'
        f'</div>'
    )

    llmview_body, region_n, cand_n = _llm_view_html(page)
    llmview = _details(
        f'① 최종 파싱 결과 (LLM 에 넘기는 형태)'
        f'<span class="meta">영역 {region_n}개 · 읽기순서 정렬 · 통독 후보 {cand_n}개</span>'
        + _src("out/llm_view/*.json",
               "pages[].regions[] : region_id · role · text · vlm_reading"),
        _intro(
            '파싱의 <b>최종 산출물</b>입니다. 좌표·신뢰도 같은 기계 신호를 다 빼고 '
            '<b>읽는 순서</b>(카드 → 위→아래 → 좌→우)로 텍스트만 남긴 것이며, 다음 단계'
            '(STAGE_3 스키마 추출)에 이 글자가 그대로 들어갑니다. '
            '<code>p1_r002</code> = 영역 ID, 옆의 <code>이미지</code> = 그 영역의 역할이고, '
            '왼쪽 그림의 같은 이름 박스가 그 위치입니다. '
            '<b>한 항목 = 영역 하나</b>이며(라인 단위가 아닙니다) 한 항목의 <code>text</code> 안에 '
            '여러 줄이 줄바꿈으로 들어갑니다. VLM 통독 후보(<code>vlm_reading</code>)가 붙은 '
            '영역은 <b>정본과 나란히</b> 보여줍니다 — 후보도 영역 단위이고, 라인 단위 재판독 '
            '후보(⑫⑬)는 <code>out/json</code> 에만 있고 여기로는 넘어가지 않습니다.'
        ) + llmview_body, cls="llmviewwrap",
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
        f'④ 감사용 원본 증거층 — 한 줄씩 쪼갠 그대로'
        f'<span class="meta">영역 {ev_region_n}개{unassigned_note}</span>'
        + _src("out/json/*.json", "regions[].lines[] : text · source · confidence · bbox"),
        _intro(
            '①이 <b>사람이 읽기 좋게 다듬은 결과</b>라면, 이건 <b>다듬기 전 원자료</b>입니다. '
            '①에서 한 문장으로 이어 붙인 줄이 실제로는 OCR 박스 몇 개였는지, 각 조각을 '
            '<b>무엇이 잡았고</b>(O=OCR / D=디지털텍스트 / V·S·R=VLM) '
            '<b>얼마나 확신했는지</b>(0~1, 0.8 미만은 노란 배경) 가 그대로 보입니다. '
            '"이 값 어디서 나왔냐"는 추궁에 답하는 층이라 <b>감사용</b>이고, 평소 검토에는 '
            '①만 봐도 됩니다. 맨 아래 <b>미배정</b>은 어느 영역 박스에도 못 붙은 낱줄로, '
            '글자는 다음 단계로 전달되지만 근거 좌표를 영역 단위로 지목할 수 없습니다.'
        ) + evidence_body, cls="evidencewrap",
    ))

    # 노트 — 파이프라인이 스스로 남긴 결정 기록
    notes = page.get("notes", [])
    if notes:
        items = "".join(f"<li>{html.escape(n)}</li>" for n in notes)
        parts.append(_details(
            f'⑤ 파이프라인 판단 로그<span class="meta">{len(notes)}건</span>'
            + _src("out/json/*.json", "pages[].notes[]"),
            _intro(
                '파이프라인이 <b>스스로 남긴 처리 기록</b>입니다. 몇 개 타일로 잘랐는지, '
                '낱줄 몇 개를 어떤 근거로(포함/근접) 영역에 붙였는지, VLM 통독이 실패했거나 '
                '뒤가 잘렸는지, 어떤 저신뢰 글자를 재판독했는지가 시간순으로 적힙니다. '
                '결과가 이상할 때 <b>어느 단계에서 그렇게 됐는지</b>를 여기서 찾습니다 — '
                '조용히 실패하지 않게 하려고 만든 층이라, 실패도 성공도 다 남깁니다.'
            ) + f'<div class="notes"><ul>{items}</ul></div>', cls="noteswrap",
        ))

    # 머리줄 항목마다 title 로 뜻을 달아 둔다 — 발표 중 마우스만 올려도 설명이 나오게(2026-08-04).
    return (
        f'<div class="page">'
        f'<div class="phead"><b>p{pno}</b> · '
        f'<span title="이 페이지를 읽은 경로: ocr=전부 OCR / digital=디지털 텍스트 정본 / hybrid=병행">'
        f'route=<b>{html.escape(str(page.get("parse_route")))}</b></span> · '
        f'<span title="이 페이지 파싱 성패: ok / unreadable(판독 불가)">'
        f'status={html.escape(str(page.get("parse_status")))}</span> · '
        f'<span title="처리 기준 이미지 크기(px, 가로×세로). 모든 bbox 좌표가 이 좌표계다">'
        f'canvas {page.get("canvas_w")}×{page.get("canvas_h")}</span>{triage_txt} · '
        f'<span title="이 페이지에서 잡은 라인 수 = 영역에 붙은 줄 + 미배정 줄">'
        f'총 {st["total"]}줄</span></div>'
        f'<div class="cols"><div class="imgcol">{img_html}</div>'
        f'<div class="txtcol">{"".join(parts)}</div></div>'
        f'</div>'
    )


def _doc_html(parsed: dict, preview_dir: Path, extracted_dir: Path,
              parsing_only: bool = False) -> tuple[str, dict]:
    doc_id = parsed["doc_id"]
    # parsing_only: STAGE_3 결과를 아예 안 읽는다 → ② 표·근거 하이라이트가 사라진다.
    # 스키마가 확정 전인데 필드 표를 띄우면 논의가 "미완인 스키마" 쪽으로 끌려간다.
    extracted = None if parsing_only else _load_extracted(extracted_dir, doc_id)
    pages_html = "".join(
        _page_html(parsed, p, preview_dir, extracted) for p in parsed.get("pages", [])
    )
    agg = {"pages": len(parsed.get("pages", [])), "regions": 0, "lines": 0,
           "unassigned": 0, "unassigned_ocr": 0}
    for p in parsed.get("pages", []):
        st = _page_stats(p)
        agg["regions"] += st["regions"]; agg["lines"] += st["total"]
        agg["unassigned"] += st["unassigned"]; agg["unassigned_ocr"] += st["unassigned_ocr"]
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
.ovhead { font-size:13px; font-weight:700; color:#1f2328; margin:18px 0 8px; padding-bottom:5px; border-bottom:1px solid #eaecef; }
.actlegend { float:right; font-weight:normal; font-size:11px; color:#8b949e; }
.actlegend .act { margin-left:12px; }
.act { display:inline-block; font-size:9.5px; font-weight:700; padding:1px 5px; border-radius:3px; vertical-align:middle; }
.act-code { background:#eaecef; color:#424a53; }
.act-paddle { background:#ddf4ff; color:#0550ae; }
.act-vlm { background:#fbefff; color:#8250df; }
/* 도식 — 인라인 SVG(폐쇄망: 외부 도식 라이브러리 금지). 좁은 화면에서는 가로 스크롤. */
.svgwrap { overflow-x:auto; margin-bottom:10px; }
svg.pipesvg { width:100%; min-width:1000px; height:auto; display:block; }
svg.pipesvg text { font-family:'Malgun Gothic','Segoe UI',sans-serif; }
svg.pipesvg .lanelbl { font-size:11.5px; font-weight:700; fill:#57606a; }
svg.pipesvg .lanerule { stroke:#eaecef; stroke-width:1; }
svg.pipesvg .bt { font-size:12px; font-weight:700; fill:#1f2328; }
svg.pipesvg .bs { font-size:10px; fill:#57606a; }
svg.pipesvg .arw { stroke:#57606a; stroke-width:1.4; fill:none; }
svg.pipesvg .arw.dash { stroke:#8250df; stroke-dasharray:5 3; }
svg.pipesvg .edgelbl { font-size:10px; fill:#8250df; }
svg.pipesvg .edgebg { fill:#fff; }
svg.pipesvg .dimmed { opacity:0.35; }
svg.pipesvg .todaycut { font-size:10.5px; font-weight:700; fill:#9a6700; }
svg.pipesvg .bx rect { stroke-width:1.2; }
svg.pipesvg .code rect { fill:#f6f8fa; stroke:#afb8c1; }
svg.pipesvg .paddle rect { fill:#ddf4ff; stroke:#54aeff; }
svg.pipesvg .vlm rect { fill:#faf0ff; stroke:#c297ff; }
svg.pipesvg .out rect { fill:#f0fdf1; stroke:#4ac26b; }
svg.pipesvg .io rect { fill:#fff; stroke:#8b949e; stroke-dasharray:4 3; }
svg.pipesvg .note rect { fill:#fff8c5; stroke:#d4a72c; }
svg.pipesvg .note .bt { fill:#7d4e00; }
details.stepswrap { margin-bottom:14px; }
details.stepswrap > summary { background:#f6f8fa; padding:6px 10px; font-size:12.5px; }
details.stepswrap .phases { padding:10px; margin-bottom:0; }
.phases { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px; align-items:flex-start; }
.phase { flex:1 1 300px; min-width:280px; border:1px solid #d0d7de; border-radius:6px; overflow:hidden; background:#fff; }
.phhead { background:#f6f8fa; border-bottom:1px solid #d0d7de; padding:7px 10px; font-size:13px; font-weight:700; }
.phno { display:inline-block; background:#0550ae; color:#fff; font-size:11px; width:18px; height:18px; line-height:18px; text-align:center; border-radius:9px; margin-right:6px; }
.phdesc { display:block; font-weight:normal; font-size:11px; color:#8b949e; margin-top:2px; }
ul.steps { margin:0; padding:6px 10px 8px; list-style:none; }
ul.steps li { padding:4px 0; border-top:1px dotted #eaecef; font-size:11.5px; line-height:1.55; }
ul.steps li:first-child { border-top:none; }
.stno { color:#8b949e; font-weight:700; margin-right:4px; }
.stname { font-weight:700; color:#0550ae; margin-right:5px; }
.stdesc { display:block; color:#57606a; margin-top:1px; }
.outputs { display:flex; gap:10px; margin-bottom:14px; flex-wrap:wrap; align-items:stretch; }
.outbox { flex:1 1 240px; min-width:220px; border:1px solid #eaecef; border-radius:6px; padding:9px 12px; font-size:11.5px; color:#57606a; background:#fafbfc; line-height:1.55; }
.outbox.highlight { border-color:#1a7f37; background:#f0fdf1; }
/* 파일명만 블록 — `.outbox > b` 로 잡으면 설명문 안의 강조 <b> 까지 줄바꿈된다(실측) */
.outbox b.fname { display:block; color:#1f2328; font-size:13px; font-family:Consolas,monospace; }
.outbox .of { display:block; font-size:11px; font-weight:700; color:#0550ae; margin:1px 0 3px; }
.outbox code { display:block; margin-top:5px; font-size:10.5px; color:#8b949e; word-break:break-all; }
.howto { font-size:12px; color:#57606a; line-height:1.75; background:#f6f8fa; border-radius:6px; padding:10px 14px; margin:0 0 4px; }
details.termswrap { margin-top:14px; }
details.termswrap > summary { background:#f6f8fa; padding:6px 10px; font-size:13px; }
table.terms { border-collapse:collapse; width:100%; font-size:11.5px; }
table.terms th, table.terms td { border-top:1px solid #eaecef; padding:5px 9px; text-align:left; vertical-align:top; line-height:1.5; }
table.terms th { background:#fafbfc; color:#57606a; font-size:11px; }
table.terms .tm { white-space:nowrap; font-weight:700; color:#0550ae; }
table.terms .tw { color:#8b949e; }
.summary .sumnote { font-size:11.5px; color:#57606a; line-height:1.7; margin:8px 0 0; }
header.top .scopetag { font-size:11px; font-weight:normal; background:#fff8c5; color:#7d4e00; padding:2px 8px; border-radius:10px; vertical-align:middle; margin-left:6px; }
.pipeline-overview .scopebar { background:#fff8c5; border-left:3px solid #d4a72c; border-radius:4px; padding:9px 13px; font-size:12.5px; color:#57606a; line-height:1.7; margin:0 0 12px; }
.blkintro { font-size:11.5px; color:#57606a; line-height:1.7; margin:0; padding:8px 12px; background:#fafbfc; border-bottom:1px solid #eaecef; }
/* 토글 제목줄: [▸ 제목] [요약숫자] ...밀어냄... [파일명] / 다음 줄 [필드경로]
   기본 .sechead 는 space-between 2단 배치라 항목이 4개가 되면 흩어진다 — 여기만 재정의. */
details.toggle > summary.sechead { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 8px; justify-content:flex-start; }
.srcfile { margin-left:auto; font-size:10px; font-family:Consolas,monospace; color:#57606a; background:#eaecef; padding:1px 6px; border-radius:3px; }
.srcfields { margin-left:auto; font-size:9.5px; color:#8b949e; font-weight:normal; }
.lblchk { display:none; }
.lblctl { display:inline-block; font-size:11px; color:#57606a; cursor:pointer; margin-bottom:6px; user-select:none; }
.lblctl::before { content:'☑ '; }
.lblchk:not(:checked) ~ .lblctl::before { content:'☐ '; }
.lblchk:not(:checked) ~ .imgstack .rlbl { display:none; }
.rlbl { position:absolute; transform:translateY(-1px); font-size:8px; line-height:1.25; padding:0 2px; background:rgba(190,0,130,0.88); color:#fff; white-space:nowrap; border-radius:2px; pointer-events:auto; cursor:help; }
.rlbl i { font-style:normal; opacity:0.82; }
.rlbl.empty { background:rgba(120,120,130,0.6); }
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
.pill .sub { font-size:10px; opacity:0.75; margin-left:5px; }
.summary .subcell { display:block; font-size:10px; color:#8b949e; font-weight:normal; }
/* ① LLM 전달 형태 — 영역 하나가 한 행. 통독 후보가 있으면 그 자리에서 2단 대조. */
.llmview { background:#f7fbff; padding:6px 8px; }
.lvrow { border-top:1px solid #e3eefc; padding:5px 4px; }
.lvrow:first-child { border-top:none; }
.lvrow.cand { background:#fff; border:1px solid #c8e1ff; border-radius:5px; margin:5px 0; padding:6px 8px; }
.lvid { font-size:11.5px; color:#57606a; display:flex; flex-wrap:wrap; align-items:baseline; gap:6px; }
.lvid b { color:#0550ae; font-family:Consolas,monospace; }
.lvrole { background:#eaecef; color:#424a53; font-size:10px; padding:1px 6px; border-radius:8px; }
.lvtext, .lvcol pre { margin:2px 0 0; font-family:'Malgun Gothic','Segoe UI',sans-serif; font-size:13px; line-height:1.6; white-space:pre-wrap; word-break:break-word; color:#1f2328; }
.lvcols { display:flex; gap:8px; margin-top:4px; }
.lvcol { flex:1; min-width:0; border:1px solid #eaecef; border-radius:4px; overflow:hidden; }
.lvcol.ocr { border-color:#c8d7ff; }
.lvcol.vlm { border-color:#d8b9ff; }
.lvhd { font-size:9.5px; padding:2px 7px; background:#f6f8fa; color:#57606a; border-bottom:1px solid #eaecef; font-family:Consolas,monospace; }
.lvcol pre { padding:4px 7px; font-size:12.5px; }
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


# 처리 단계 — docs/architecture/pipeline-map.md 와 같은 번호(①~⑮). 문서와 화면의
# 단계 번호가 어긋나면 인수인계에서 서로 다른 것을 가리키게 되므로 반드시 같이 고친다.
# 주체: code=순수 코드(모델 호출 없음) / paddle=PaddleX OCR / vlm=Gemma VLM.
_ACTOR_META = {
    "code": ("코드", "act-code", "순수 코드 — 좌표 계산·규칙·픽셀 통계. 모델 호출 없음"),
    "paddle": ("OCR", "act-paddle", "PaddleX PP-StructureV3 — 글자 검출·인식·레이아웃 블록"),
    "vlm": ("VLM", "act-vlm", "Gemma VLM — 의미 판단(역할·카드·교정·분류)"),
}

_PHASES: list[tuple[str, str, str, list[tuple[str, str, str, str]]]] = [
    ("0", "라우팅", "이 페이지를 어떻게 읽을지 먼저 정한다", [
        ("0", "트리아지", "code",
         "PDF는 페이지마다 structured / hybrid / scan_like 판정. 디지털 텍스트를 믿을 수 있으면 "
         "OCR 을 아예 안 돌린다(정확·무료). HWP는 텍스트·표를 사내 파서 정본으로 쓰고 내장 이미지만 OCR."),
    ]),
    ("1", "글자 획득", "여기가 OCR — 글자와 좌표를 뽑는다", [
        ("①", "밀도 기반 타일 분할", "code",
         "세로로 긴 광고(6000px+)는 한 번에 못 보내 잘라야 한다. 글자량이 고르게 나뉘는 자리를 찾아 "
         "글자 없는 행에 스냅해 자른다 — 기계적으로 1600px마다 자르면 글자가 반토막 났다."),
        ("②", "레이아웃 + OCR", "paddle",
         "타일당 1회. 글자 박스·인식 텍스트·레이아웃 블록(제목/본문/표/그림)을 한 번에 받는다."),
        ("③", "좌표 복원 · 중복 제거", "code",
         "타일 좌표를 원본 이미지 좌표로 되돌리고, 타일이 200px 겹치는 구간의 중복 검출을 지운다."),
        ("④", "디지털 우선 병합", "code",
         "PDF·HWP의 디지털 텍스트와 OCR 결과가 같은 자리에서 겹치면 디지털을 정본으로 쓴다."),
        ("⑤", "영역 조립", "code",
         "레이아웃 블록에 라인을 좌표로 분배해 <b>영역</b>을 만든다. 어느 블록에도 안 들어간 라인이 "
         "<b>미배정 낱줄</b>이 되고, 이것이 ⑨가 존재하는 이유다."),
    ]),
    ("2", "구조 정리", "글자를 화면 구조에 맞게 묶는다", [
        ("⑥", "카드 게이트", "code",
         "세로 스크롤형(높이/폭 ≥ 2)이거나 단일 패널이면 카드 판정을 <b>호출조차 안 한다</b> — 헛호출 차단."),
        ("⑦", "카드 배정", "vlm",
         "한 이미지에 광고 패널이 좌우로 여러 장일 때 묶음을 나눈다. <b>개수는 픽셀 밀도가 결정론적으로 세고</b> "
         "배정은 VLM — 전폭 헤드라인처럼 어느 컬럼에도 안 속하는 요소는 좌표로 못 정하기 때문. 밀도 관측을 증거로 실어 1회만 묻는다."),
        ("⑧", "역할 판정", "vlm",
         "영역마다 <b>역할</b>(제목·본문·유의사항·고지문구·각주·표·이미지·버튼·기타) 하나를 정한다. "
         "왼쪽 그림 박스 옆 라벨의 괄호 값이 이것. OCR 정본만 보고 판정한다(VLM 후보는 안 씀 — 비결정성 격리)."),
        ("⑨", "미배정 낱줄 귀속", "code",
         "⑤에서 남은 낱줄을 <b>좌표만 보고</b> 붙인다(VLM 호출 없음). ㉠ 포함: 중심점을 품는 가장 작은 영역 → "
         "㉡ 근접: <b>같은 칼럼</b>(가로 50% 이상 겹침) 중 수직 갭 최근접. 둘 다 실패하면 미배정 유지."),
    ]),
    ("3", "통합 판독", "OCR 이 놓치거나 잘못 읽은 글자를 VLM 이 재확인 — 정본은 안 덮는다", [
        ("⑩", "밴드 통합판독", "vlm",
         "OCR 이 본 것과 <b>같은 크롭</b>을 VLM 에도 주고 \"이 영역들을 고쳐라 + 목록에 없는 문구를 찾아라\"를 "
         "한 번에 묻는다(두 단계를 합쳐 호출 122→65회). 결과는 <b>후보</b>(vlm_reading)로만 붙는다."),
        ("⑪", "통짜 스윕", "vlm",
         "타일이 원래 못 잡는 대형 장식 타이포를 페이지 전체 1회 통짜로 회수(실측: 002 '행운의 777 이벤트')."),
        ("⑫", "스윕-OCR 중복 정정", "vlm",
         "같은 자리를 다르게 읽은 경우(<code>1O.1%p</code> vs <code>① 0.1%p</code>) 그 자리만 고해상 재판독해 "
         "<b>VLM 이 심판</b>한다. 코드가 규칙으로 우열을 정하지 않는다."),
        ("⑬", "저신뢰 재판독", "vlm",
         "OCR 신뢰도 0.80 미만 라인을 다시 읽는다(실측: '생학해대' → '생활형태'). 역시 후보로만 부착."),
        ("⑭", "읽기순서 정렬 · 진단", "code",
         "카드 → 위→아래 → 좌→우 로 정렬한다. 레이아웃이 <b>통째로 놓친 덩어리</b>가 있으면 판단 로그에 남긴다."),
        ("⑮", "광고 분류", "vlm", "상품군(예금성·대출성)과 광고 유형을 문서당 1회 판정 — 어느 스키마를 쓸지 결정."),
    ]),
    ("4", "스키마 추출 (STAGE_3)", "심의 스키마 필드에 값을 채운다 — 별도 프로세스", [
        ("", "5그룹 분할 호출", "vlm",
         "상품기본 / 금리 / 의무고지 / 위험표현 / 이벤트 로 나눠 문서당 5회. 한 번에 57필드를 다 물으면 "
         "응답이 길어져 배열이 비는 퇴행이 난다."),
        ("", "부재 4분류", "code",
         "값이 없을 때 <b>미표시 / 해당없음 / 확인필요 / 판정제외</b>를 스키마 메타데이터로 코드가 가른다 — "
         "모델 호출 0회. '없음'을 다 똑같이 두면 진짜 누락이 묻힌다."),
        ("", "근거 bbox 재부착", "code",
         "필드가 지목한 region_id 로 좌표를 되붙여 이 화면의 hover 하이라이트에 연결한다."),
    ]),
]

# 발표·인수인계용 용어 사전 — 화면에 실제로 찍히는 표기만 넣는다.
_TERMS: list[tuple[str, str, str]] = [
    ("라인 (line)", "OCR·디지털 텍스트가 잡은 <b>글자 한 조각</b>. 한 시각적 줄이 여러 조각으로 쪼개지기도 한다",
     "그림의 <b>가는</b> 마젠타 박스 / 요약표 '라인'"),
    ("영역 (region)", "붙어 있는 라인들을 묶은 <b>레이아웃 단위</b>. <code>p1_r002</code> = 1페이지의 3번째(000부터) 영역",
     "그림의 <b>굵은</b> 마젠타 박스 / 요약표 '영역'"),
    ("역할 (role)", "그 영역이 무엇인지 — 제목·본문·유의사항·고지문구·각주·표·이미지·버튼·기타 9종 (⑧ VLM 판정)",
     "박스 라벨의 괄호 값 / <code>p1_r002 (이미지)</code>"),
    ("카드 (card_no)", "한 이미지 안에 광고 패널이 좌우로 여러 장일 때의 묶음 번호 (⑦)", "증거층 영역 머리줄 '카드1'"),
    ("미배정", "어느 영역 박스에도 못 붙은 낱줄. <b>글자는 다음 단계로 전달되지만</b> 근거를 영역 단위로 지목할 수 없다",
     "요약표 '미배정' / 증거층 맨 아래"),
    ("정본", "최종적으로 <b>실제 쓰는 값</b> = OCR·디지털 텍스트. VLM 판독은 후보일 뿐 이 값을 덮지 않는다", "③ 대조 패널 왼쪽 칸"),
    ("후보 (vlm_reading)", "VLM 이 다시 읽어 본 결과. 정본과 나란히 전달되고 <b>선택은 STAGE_3</b>가 한다", "③ 대조 패널 오른쪽 칸"),
    ("route", "이 페이지를 읽은 경로 — <code>ocr</code>(전부 OCR) / <code>digital</code>(디지털 텍스트) / <code>hybrid</code>(병행)",
     "페이지 머리줄"),
    ("status", "그 페이지 파싱 성패 — <code>ok</code> / <code>unreadable</code>(판독 불가)", "페이지 머리줄"),
    ("canvas", "처리 기준 이미지 크기(px, 가로×세로). 모든 bbox 좌표가 이 좌표계다", "페이지 머리줄"),
    ("총 n줄", "그 페이지에서 잡은 라인 수 = 영역에 붙은 줄 + 미배정 줄", "페이지 머리줄 / 요약표 '라인'"),
    ("conf", "OCR 인식 신뢰도 0~1. <b>0.8 미만이면 노란 배경</b>으로 표시하고 ⑬ 재판독 대상이 된다", "증거층 각 줄 앞 숫자"),
    ("found / not_found", "스키마 필드에 값을 채웠나 / 광고에서 그 값을 못 찾았나. <b>not_found 는 결함이 아니다</b> — 원래 없을 수도 있다",
     "요약표 'found/not_found' / ② 표 '상태'"),
    ("미표시", "<b>표시 의무가 있는데 값이 없는</b> 경우. 위반 판정이 아니라 <b>사실 관측</b>이며, 최종 심의는 다음 단계 몫",
     "요약표 '미표시'(빨강) / ② 표"),
    ("해당없음", "이 상품·광고 유형에는 그 개념이 <b>원래 성립하지 않는</b> 경우(예: 적금에 대출한도). 확인할 필요 없다", "② 표 '상태'"),
]


# 도식 — 텍스트 단계표(_PHASES)와 같은 내용을 그림으로. 순수 인라인 SVG 로 그린다:
# 폐쇄망이라 외부 도식 라이브러리(mermaid 등)를 못 쓰고, PNG 로 굽는 것보다 SVG 가
# 확대해도 안 깨지고 diff 도 된다. (id, 주체, 제목, 부제줄들, 폭) — id 는 특수 화살표
# (structured 우회 / 미배정 되돌림)가 좌표를 찾을 때 쓴다.
_LANES: list[tuple[str, list[tuple[str, str, str, list[str], int]]]] = [
    ("0 라우팅", [
        ("in", "io", "입력", ["PDF · PNG · HWP"], 130),
        ("triage", "code", "⓿ 트리아지", ["디지털 텍스트를 믿을 수 있나", "structured / hybrid / scan_like"], 210),
    ]),
    ("1 글자 획득", [
        ("tile", "code", "① 타일 분할", ["글자 없는 행에서 자른다"], 165),
        ("ocr", "paddle", "② OCR + 레이아웃", ["글자·좌표·레이아웃 블록", "타일당 1회"], 185),
        ("merge", "code", "③④ 좌표복원·병합", ["중복 제거 · 디지털 우선"], 185),
        ("region", "code", "⑤ 영역 조립", ["블록에 라인 배정", "= 굵은 박스"], 165),
    ]),
    ("2 구조 정리", [
        ("card", "vlm", "⑥⑦ 카드 배정", ["개수=밀도(코드)", "배정=VLM"], 165),
        ("role", "vlm", "⑧ 역할 판정", ["제목·유의사항 등 9종", "= 박스 라벨 괄호"], 185),
        ("absorb", "code", "⑨ 낱줄 귀속", ["포함 → 같은 칼럼 근접", "좌표만, VLM 없음"], 185),
    ]),
    ("3 통합 판독", [
        ("band", "vlm", "⑩ 밴드 통독", ["OCR 과 같은 크롭으로", "교정 + 누락 회수"], 175),
        ("sweep", "vlm", "⑪ 통짜 스윕", ["대형 장식 타이포"], 150),
        ("reread", "vlm", "⑫⑬ 재판독", ["중복·저신뢰 라인"], 150),
        ("keep", "note", "정본은 안 덮는다", ["후보(vlm_reading)로만 부착", "선택은 STAGE_3 몫"], 200),
    ]),
    ("4 정렬 · 산출물", [
        ("sort", "code", "⑭ 읽기순서 정렬", ["카드 → 위아래 → 좌우", "라인 좌표 기준"], 175),
        ("json", "out", "out/json", ["전체 기록 — 좌표·신뢰도", "출처·판단 로그 (감사용)"], 190),
        ("view", "out", "out/llm_view", ["정제 텍스트 + 통독 후보", "= STAGE_3 입력"], 190),
    ]),
    ("5 스키마 추출", [
        ("s3", "vlm", "STAGE_3 5그룹 호출", ["상품기본·금리·의무고지", "위험표현·이벤트"], 190),
        ("absence", "code", "부재 4분류 · bbox 재부착", ["미표시/해당없음/확인필요", "/판정제외 — 모델 호출 0"], 210),
        ("ext", "out", "out/extracted", ["값·상태·근거 필드"], 175),
    ]),
]

_SVG_LANE_H = 108      # 레인 하나의 높이
_SVG_BOX_H = 66
_SVG_X0 = 118          # 레인 이름 칸 폭
_SVG_GAP = 34          # 박스 사이 화살표 자리


def _pipeline_svg(dim_from_lane: int | None = None) -> str:
    """레인(단계) x 박스(세부단계) 도식. 특수 화살표 2개를 곡선으로 얹는다.

    dim_from_lane: 그 인덱스부터의 레인을 흐리게 — 파싱만 보여줄 때 "여기까지가 오늘
    범위"를 그림에서 바로 읽히게 한다(스키마 단계를 지우면 전체 그림이 안 보이므로
    지우지 않고 흐린다).
    """
    pos: dict[str, tuple[float, float, float, float]] = {}   # id → (x, y, w, h)
    body: list[str] = []
    for li, (lane, boxes) in enumerate(_LANES):
        dim = " dimmed" if dim_from_lane is not None and li >= dim_from_lane else ""
        cy = 30 + li * _SVG_LANE_H + _SVG_BOX_H / 2
        top = cy - _SVG_BOX_H / 2
        if dim and li == dim_from_lane:
            body.append(
                f'<text class="todaycut" x="8" y="{top - 8:.0f}">'
                '↓ 여기부터는 오늘 범위 밖 (스키마 미완)</text>'
            )
        body.append(
            f'<text class="lanelbl{dim}" x="8" y="{cy + 4:.0f}">{html.escape(lane)}</text>'
            f'<line class="lanerule" x1="0" y1="{cy + _SVG_LANE_H / 2 - 4:.0f}" '
            f'x2="1180" y2="{cy + _SVG_LANE_H / 2 - 4:.0f}"/>'
        )
        x = _SVG_X0
        for bi, (bid, actor, title, subs, w) in enumerate(boxes):
            if bi:
                body.append(
                    f'<path class="arw" d="M{x - _SVG_GAP + 4} {cy} H{x - 5}" marker-end="url(#ah)"/>'
                )
            pos[bid] = (x, top, w, _SVG_BOX_H)
            sub_y = top + 36
            subs_svg = "".join(
                f'<text class="bs" x="{x + w / 2:.0f}" y="{sub_y + i * 13:.0f}" '
                f'text-anchor="middle">{html.escape(s)}</text>'
                for i, s in enumerate(subs)
            )
            body.append(
                f'<g class="bx {actor}{dim}"><rect x="{x}" y="{top:.0f}" width="{w}" '
                f'height="{_SVG_BOX_H}" rx="7"/>'
                f'<text class="bt" x="{x + w / 2:.0f}" y="{top + 21:.0f}" '
                f'text-anchor="middle">{html.escape(title)}</text>{subs_svg}</g>'
            )
            x += w + _SVG_GAP
        # 레인 사이 연결: 마지막 박스 아래 → 다음 레인 첫 박스 위
        if li + 1 < len(_LANES):
            lx, _, lw, _h = pos[boxes[-1][0]]
            from_x, from_y = lx + lw / 2, top + _SVG_BOX_H
            to_x = _SVG_X0 + _LANES[li + 1][1][0][4] / 2
            mid = from_y + (_SVG_LANE_H - _SVG_BOX_H) / 2
            body.append(
                f'<path class="arw" d="M{from_x:.0f} {from_y:.0f} V{mid:.0f} '
                f'H{to_x:.0f} V{from_y + _SVG_LANE_H - _SVG_BOX_H - 5:.0f}" marker-end="url(#ah)"/>'
            )

    def edge_label(x: float, y: float, text: str) -> str:
        """점선 위에 얹는 설명. 선이 글자를 관통하지 않게 흰 받침을 깐다(실측 겹침)."""
        w = len(text) * 6.2 + 8
        return (
            f'<rect class="edgebg" x="{x - 4:.0f}" y="{y - 10:.0f}" width="{w:.0f}" height="13"/>'
            f'<text class="edgelbl" x="{x:.0f}" y="{y:.0f}">{html.escape(text)}</text>'
        )

    # ㉠ structured 우회 — 디지털 텍스트가 정본이면 ①② 를 건너뛴다
    tx, ty, tw, th = pos["triage"]
    rx, ry, rw, _rh = pos["region"]
    body.append(
        f'<path class="arw dash" d="M{tx + tw} {ty + th / 2:.0f} H{rx + rw - 24:.0f} '
        f'V{ry - 5:.0f}" marker-end="url(#ah2)"/>'
        + edge_label(tx + tw + 10, ty + th / 2 - 6, "structured → OCR 생략 (디지털 텍스트가 정본)")
    )
    # ㉡ ⑤ 에서 남은 미배정 낱줄이 ⑨ 로 되돌아온다
    ax, ay, aw, _ah = pos["absorb"]
    loop_y = ry + _SVG_BOX_H + 17
    body.append(
        f'<path class="arw dash" d="M{rx + 24} {ry + _SVG_BOX_H} V{loop_y:.0f} '
        f'H{ax + aw / 2:.0f} V{ay - 5:.0f}" marker-end="url(#ah2)"/>'
        + edge_label(rx + 34, loop_y + 4, "⑤ 에서 어느 영역에도 못 붙은 미배정 낱줄")
    )

    legend = "".join(
        f'<g class="bx {a}"><rect x="{118 + i * 150}" y="668" width="18" height="12" rx="3"/></g>'
        f'<text class="edgelbl" x="{140 + i * 150}" y="678">{lab}</text>'
        for i, (a, lab) in enumerate(
            [("code", "순수 코드"), ("paddle", "PaddleX OCR"), ("vlm", "Gemma VLM"),
             ("out", "산출물 파일"), ("io", "입·출력")]
        )
    )
    return (
        '<svg class="pipesvg" viewBox="0 0 1190 692" role="img" '
        'aria-label="파싱 파이프라인 처리 단계 도식">'
        '<defs>'
        '<marker id="ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" '
        'orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="#57606a"/></marker>'
        '<marker id="ah2" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" '
        'orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="#8250df"/></marker>'
        '</defs>'
        f'{"".join(body)}{legend}</svg>'
    )


def _pipeline_overview_html(parsing_only: bool = False) -> str:
    """처음 보는 사람용 오리엔테이션 — 페이지 맨 위 고정. 접지 않는다.

    2026-08-04: 팀 공유 때 이 화면 하나로 "뭘 하는 시스템이고 어떻게 읽으면 되는지"가
    잡히게 해달라는 요청으로 추가. 외부 이미지·라이브러리 없이 순수 HTML/CSS 로만
    흐름도를 그린다(폐쇄망 제약과 동일한 이유).

    같은 날 2차: 4칸 요약으로는 "중간에 무슨 일이 일어나는지"가 안 보인다는 지적을 받아
    ①~⑮ 전 단계를 주체(코드/OCR/VLM) 딱지와 함께 펼쳤다. 이 화면으로 발표하므로
    용어 사전도 같이 넣는다 — 화면에 찍히는 표기와 1:1로만 적는다.
    """
    actor_legend = " ".join(
        f'<span class="act {cls}">{lab}</span> {html.escape(desc)}'
        for lab, cls, desc in _ACTOR_META.values()
    )
    phases = []
    for pno, ptitle, pdesc, steps in _PHASES:
        rows = "".join(
            f'<li><span class="stno">{sno}</span>'
            f'<span class="stname">{html.escape(sname)}</span>'
            f'<span class="act {_ACTOR_META[actor][1]}">{_ACTOR_META[actor][0]}</span>'
            f'<span class="stdesc">{sdesc}</span></li>'
            for sno, sname, actor, sdesc in steps
        )
        phases.append(
            f'<div class="phase"><div class="phhead"><span class="phno">{pno}</span>'
            f'{html.escape(ptitle)}<span class="phdesc">{html.escape(pdesc)}</span></div>'
            f'<ul class="steps">{rows}</ul></div>'
        )
    _OUTPUTS = [
        ("out/json", "전체 기록",
         "파싱이 본 모든 것 — 라인마다 좌표·신뢰도·출처, 영역마다 역할·카드·후보판독, "
         "페이지마다 판단 로그. <b>감사·재현용</b>이라 아무것도 버리지 않는다.",
         "pages[].regions[].lines[] · unassigned_lines[] · notes[]", False),
        ("out/llm_view", "정제 텍스트",
         "좌표·신뢰도를 다 빼고 읽기순서 텍스트만 남긴 것. "
         "<b>다음 단계(STAGE_3)에 실제로 들어가는 입력</b>이다.",
         "pages[].regions[] : region_id · role · text · vlm_reading", False),
        ("out/extracted", "구조화 필드",
         ("스키마 필드에 값·상태·근거를 채운 결과. <b>스키마 확정 전이라 오늘 화면에서는 뺐습니다.</b>"
          if parsing_only else
          "스키마 필드에 값·상태·근거를 채운 결과. 이 화면의 <b>② STAGE_3 표가 이 파일</b>이다."),
         "fields{} · events[] · unmapped[] · coverage{}", not parsing_only),
    ]
    outputs = "".join(
        f'<div class="outbox{" highlight" if hi else ""}"><b class="fname">{name}</b>'
        f'<span class="of">{label}</span>{desc}<code>{html.escape(keys)}</code></div>'
        for name, label, desc, keys, hi in _OUTPUTS
    )
    terms = "".join(
        f'<tr><td class="tm">{t}</td><td class="td1">{d}</td><td class="tw">{w}</td></tr>'
        for t, d, w in _TERMS
    )
    return (
        '<section class="pipeline-overview">'
        '<h2>이 화면은 무엇인가</h2>'
        + ('<p class="scopebar"><b>이 화면은 파싱(OCR) 결과까지만 봅니다.</b> 광고 이미지에서 '
           '<b>글자를 정확히·빠짐없이 뽑았는가</b>가 오늘의 주제입니다. 뽑은 글자를 심의 스키마 '
           '필드에 채우는 단계(STAGE_3)는 <b>스키마가 아직 확정 전</b>이라 이 화면에서 뺐습니다 — '
           '기능이 없는 게 아니라 <b>보여줄 만큼 굳지 않았습니다</b>.</p>'
           if parsing_only else "")
        + '<p>광고 이미지(PDF·PNG·HWP)에서 글자를 뽑고, 심의 스키마에 맞춰 값을 채운 결과를 '
        '원본과 나란히 놓고 검토하는 화면입니다. <b>"이 광고가 규정 위반인가"는 판정하지 '
        '않습니다</b> — 값이 있는지, 없으면 왜 없는지(표시 의무 누락인지 / 이 상품 유형엔 '
        '원래 없는 개념인지)까지만 파악하고, 최종 판단은 규정과 대조하는 다음 단계 '
        '(RAG/DB 엔진 + 심의 담당자) 몫입니다.</p>'
        '<div class="ovhead">처리 단계 — 입력 1건이 아래 순서를 그대로 지나갑니다'
        f'<span class="actlegend">{actor_legend}</span></div>'
        f'<div class="svgwrap">{_pipeline_svg(dim_from_lane=5 if parsing_only else None)}</div>'
        '<details class="toggle stepswrap"><summary class="sechead">단계별 설명 (글로 보기)'
        '<span class="meta">위 그림의 각 상자가 왜 필요한지 · 실측 근거</span></summary>'
        f'<div class="phases">{"".join(phases)}</div></details>'
        '<p class="howto"><b>이 배치가 원칙입니다</b> — <b>의미 판단은 모델이, 검산은 코드가</b> 합니다. '
        '카드 개수는 픽셀 밀도가 세고(⑦), 낱줄 귀속은 좌표 게이트가 막고(⑨), 통독 후보는 관계 딱지로 '
        '교차검증합니다(⑩). 반대로 코드가 규칙으로 값의 우열을 정하지는 않습니다(⑫). '
        '그리고 <b>VLM 판독은 정본을 절대 덮지 않습니다</b> — 덮으면 같은 광고를 두 번 돌렸을 때 값이 달라져 '
        '재현이 안 되기 때문입니다(실측: 역할 판정은 실행 간 97.3% 일치, 정본 텍스트·좌표는 100% 일치).</p>'
        '<div class="ovhead">산출물 3개 — 목적이 서로 달라 합치지 않습니다</div>'
        f'<div class="outputs">{outputs}</div>'
        '<p class="howto"><b>이 화면 보는 법</b> — 문서마다 페이지별로 왼쪽엔 원본 이미지에 '
        '파싱이 잡은 위치를 <b>마젠타 박스</b>로 표시합니다(가는 선=한 줄, 굵은 선=여러 줄을 '
        '묶은 한 영역). 박스 왼쪽 위 <b><code>r002 이미지</code></b> 라벨은 영역 ID와 역할이고, '
        '위쪽 체크박스로 껐다 켤 수 있습니다 — 역할별 <b>색</b>은 쓰지 않습니다(모델 판단이라 '
        '실행마다 미세하게 바뀔 수 있어 좌표 그림에 굽지 않습니다). '
        + ('오른쪽엔 ①③④⑤ 결과가 <b>토글로 접혀</b> 있고 제목을 누르면 펼쳐집니다(② STAGE_3 는 '
           '오늘 범위 밖이라 없습니다). 접힌 상태에서도 '
           if parsing_only else
           '오른쪽엔 ①~⑤ 결과가 <b>토글로 접혀</b> 있고 제목을 누르면 펼쳐집니다. 접힌 상태에서도 ')
        + '핵심 숫자와 <b>어느 산출물 파일에서 온 값인지</b>가 제목 줄에 적혀 있습니다.</p>'
        f'<details class="toggle termswrap"><summary class="sechead">용어 사전 — 화면에 찍히는 표기'
        '<span class="meta">발표·인수인계용. 이 표에 있는 말만 화면에 씁니다</span></summary>'
        '<table class="terms"><thead><tr><th>표기</th><th>뜻</th><th>어디에 나오나</th></tr></thead>'
        f'<tbody>{terms}</tbody></table></details>'
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
    parser.add_argument(
        "--parsing-only", action="store_true",
        help="STAGE_3(스키마 추출) 결과를 뺀 파싱 전용 화면. 스키마 확정 전 공유용",
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
        doc_html, agg = _doc_html(parsed, preview_dir, extracted_dir, args.parsing_only)
        docs_html.append(doc_html)
        did = parsed["doc_id"]
        cells = [
            f'<td><a href="#{html.escape(did)}">{html.escape(did)}</a></td>',
            f'<td>{agg["pages"]}</td><td>{agg["regions"]}</td><td>{agg["lines"]}</td>',
            f'<td{" class=\"warncell\"" if agg["unassigned_ocr"] else ""}>{agg["unassigned"]}'
            f'<span class="subcell">그중 OCR {agg["unassigned_ocr"]}</span></td>',
        ]
        if not args.parsing_only:
            found_txt = f'{agg["found"]} / {agg["not_found"]}' if agg["found"] is not None else "—"
            cells.append(f'<td>{found_txt}</td>')
            cells.append(
                f'<td{" class=\"warncell\"" if agg["miss"] else ""}>'
                f'{agg["miss"] if agg["found"] is not None else "—"}</td>'
            )
        summary_rows.append(f'<tr>{"".join(cells)}</tr>')

    legend = " ".join(
        f'<span class="tag" style="background:{c}">{a}</span> {d}'
        for a, c, d in SOURCE_META.values()
    )
    # 컬럼마다 뜻을 title 로 달고, 표 아래에 한 줄 해설을 붙인다 — 이 화면으로 발표한다(2026-08-04).
    cols = [
        ("파일", "문서 하나. 눌러서 아래 상세로 이동"),
        ("페이지", "그 문서의 페이지 수"),
        ("영역", "레이아웃 단위 개수(굵은 박스). 많다고 좋은 게 아니라 화면 구성에 따라 달라진다"),
        ("라인", "잡은 글자 줄 수 = 영역에 붙은 줄 + 미배정 줄. 이 숫자가 '얼마나 읽었나'다"),
        ("미배정", "어느 영역에도 안 붙은 낱줄. 대부분은 OCR 이 못 읽어 VLM 이 새로 건진 문구이고 "
                   "밴드 근사 좌표라 일부러 안 붙인 것 — 텍스트는 다음 단계로 전달된다. "
                   "결함 신호는 '그중 OCR' 숫자다"),
    ]
    if not args.parsing_only:
        cols += [
            ("found/not_found", "스키마 필드 중 값을 채운 개수 / 광고에서 못 찾은 개수. not_found 는 결함이 아니다"),
            ("미표시", "표시 의무가 있는데 값이 없는 개수. 위반 판정이 아니라 확인이 필요하다는 관측"),
        ]
    head = "".join(f'<th title="{html.escape(d)}">{h}</th>' for h, d in cols)
    # 미배정 해설 — "낮을수록 좋다"고만 쓰면 오해다(실측: 21줄 중 20줄이 VLM 추가 회수분).
    un_note = (
        '<b>미배정</b>은 총계가 아니라 <b>그중 OCR</b> 숫자를 봅니다 — 전체 21줄 중 20줄은 '
        'OCR 이 아예 못 읽어 VLM 이 새로 건진 문구이고(밴드 근사 좌표라 일부러 영역에 안 붙임, '
        '텍스트는 다음 단계로 전달) OCR 출처는 1줄뿐입니다. '
        '<b>영역 개수는 많고 적음이 품질이 아닙니다</b> — 화면을 몇 덩어리로 검출했나일 뿐입니다. '
    )
    if args.parsing_only:
        sumnote = (
            '<p class="sumnote"><b>보는 방향</b> — <b>라인</b>은 많이 읽었을수록 좋습니다. '
            + un_note + '컬럼 제목에 마우스를 올리면 뜻이 나옵니다.</p>'
        )
    else:
        sumnote = (
            '<p class="sumnote"><b>보는 방향</b> — <b>라인</b>은 많이 읽었을수록, '
            '<b>found</b>는 높을수록 좋습니다. ' + un_note
            + '<b>not_found 는 결함이 아닙니다</b>(광고에 원래 없는 값일 수 있음). '
            '<b>미표시</b>(빨강)만 실제로 사람이 확인할 값이며, 이것도 <b>위반 판정이 아니라 사실 관측</b>입니다. '
            '컬럼 제목에 마우스를 올리면 뜻이 나옵니다.</p>'
        )
    summary = (
        '<div class="summary"><b>파일별 요약</b> — 문서 이름을 누르면 아래 상세로 이동합니다.'
        f'<table><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(summary_rows)}</tbody></table>{sumnote}</div>'
    )

    doc = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>NH 광고심의 파싱 리뷰{" — 파싱 결과" if args.parsing_only else ""}</title>'
        f'<style>{CSS}</style></head><body>'
        f'<header class="top"><h1>NH 광고심의 파싱 결과 리뷰'
        f'{" <span class=\"scopetag\">파싱(OCR) 결과까지</span>" if args.parsing_only else ""}</h1>'
        f'<div class="legend">라인 출처 태그: {legend}</div>'
        '</header>'
        f'<div class="wrap">{_pipeline_overview_html(args.parsing_only)}'
        f'{summary}{"".join(docs_html)}</div>'
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
