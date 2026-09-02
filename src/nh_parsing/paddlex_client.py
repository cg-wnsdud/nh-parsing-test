"""PaddleX PP-StructureV3 클라이언트 [B2-3] — 설계서 6.4절.

이전 프로젝트 orchestrator/adapters/paddlex.py 의 요청/응답 계약을 따른다.
타일 이미지를 보내고 레이아웃 영역 + OCR 라인을 받아온다.
좌표는 보낸 이미지(=타일) 픽셀 기준이므로 호출측에서 y_offset 만 더하면 된다.
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import requests
from PIL import Image

from .config import SETTINGS
from .ir import Line


@dataclass
class LayoutBlock:
    label: str
    bbox: list[int]
    score: float | None = None
    content: str | None = None
    source: str = "parsing_res_list"
    # 표 블록일 때만 채워지는 행·열 격자 (`_build_table_grid`). ir.RegionTable 로 그대로
    # 넘어가므로 dict 로 나른다 — 같은 모양을 두 곳에 정의하지 않으려고.
    table: dict | None = None


@dataclass
class PaddleXPageResult:
    ocr_lines: list[Line] = field(default_factory=list)
    blocks: list[LayoutBlock] = field(default_factory=list)
    source_width: int = 0
    source_height: int = 0


def _encode_jpeg(image: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _norm_bbox(raw, width: int, height: int) -> list[int] | None:
    """block_bbox [x0,y0,x1,y1] 또는 폴리곤 [[x,y],...] → [x0,y0,x1,y1]."""
    if raw is None:
        return None
    try:
        pts = list(raw)
        if pts and isinstance(pts[0], (list, tuple)):
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            box = [min(xs), min(ys), max(xs), max(ys)]
        else:
            vals = [float(v) for v in pts]
            if len(vals) == 8:  # 평탄화된 폴리곤
                xs, ys = vals[0::2], vals[1::2]
                box = [min(xs), min(ys), max(xs), max(ys)]
            elif len(vals) == 4:
                box = vals
            else:
                return None
    except (TypeError, ValueError):
        return None
    x0 = max(0, min(int(box[0]), width))
    y0 = max(0, min(int(box[1]), height))
    x1 = max(0, min(int(box[2]), width))
    y1 = max(0, min(int(box[3]), height))
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


class _TableHTMLParser(HTMLParser):
    """`pred_html` 을 훑어 셀마다 (행, 열, rowspan, colspan, 텍스트)를 **문서 순서로** 낸다.

    왜 점유 격자를 들고 가나. `rowspan` 이 있으면 다음 행의 `<td>` 개수가 줄어들어
    "몇 번째 td 인가"가 열 번호와 어긋난다. 실측(2026-08-24, `16. 대출성상품.pdf` p1):

        <tr><td>구분</td><td>임차보증금…</td><td>5천만원 이하</td>… </tr>   6칸
        <tr><td rowspan="4">일반</td><td>2천만원 이하</td><td>2.5%</td>… </tr>
        <tr><td>4천만원이하</td><td>2.7%</td>… </tr>                       5칸 ← '일반'이 계속 점유

    문서 순서를 지키는 이유는 `cell_box_list` 가 그 순서로 오기 때문이다(실측: 4개 표
    전부 `<td>` 개수 == `cell_box_list` 개수, 행우선).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[dict] = []
        self._row = -1
        self._col = 0
        self._occupied: set[tuple[int, int]] = set()
        self._open: dict | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row += 1
            self._col = 0
            return
        if tag not in ("td", "th"):
            return
        a = {k: (v or "") for k, v in attrs}
        row_span = _positive_int(a.get("rowspan"), 1)
        col_span = _positive_int(a.get("colspan"), 1)
        while (self._row, self._col) in self._occupied:
            self._col += 1
        cell = {
            "row": max(0, self._row), "col": self._col,
            "row_span": row_span, "col_span": col_span, "text": "",
        }
        for dr in range(row_span):
            for dc in range(col_span):
                self._occupied.add((self._row + dr, self._col + dc))
        self._col += col_span
        self._open = cell
        self._buf = []

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._open is not None:
            self._open["text"] = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self.cells.append(self._open)
            self._open, self._buf = None, []

    def close(self) -> None:  # 닫는 </td> 가 없는 응답도 버리지 않는다
        if self._open is not None:
            self.handle_endtag("td")
        super().close()


def _positive_int(raw, default: int) -> int:
    try:
        v = int(str(raw))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _build_table_grid(pred_html: str, cell_boxes, width: int, height: int) -> dict:
    """`pred_html` + `cell_box_list` → 행·열 격자.

    **왜 텍스트를 HTML 에서 가져오나.** 표 안 글자에도 OCR 오류가 있어(실측 `'BD'`,
    `'2 295,000원'`) HTML 텍스트가 정본은 아니다. 그래서 `html` 원본과 셀 텍스트를
    같이 남기고, 정본 줄과의 연결은 상류(`ad_export`)가 좌표로 짝지운다. 여기서
    하는 일은 **행·열 관계와 셀 좌표를 잃지 않는 것**뿐이다.

    개수가 안 맞으면 좌표를 **비운다**(틀린 좌표보다 없는 좌표). 격자 자체는 남긴다.
    """
    parser = _TableHTMLParser()
    parser.feed(pred_html or "")
    parser.close()
    cells = parser.cells

    boxes = list(cell_boxes or [])
    note = None
    if len(boxes) == len(cells) and cells:
        for cell, raw in zip(cells, boxes):
            cell["bbox"] = _norm_bbox(raw, width, height)
    else:
        note = (
            f"셀 좌표를 붙이지 못했다 — HTML 셀 {len(cells)}개 vs cell_box_list "
            f"{len(boxes)}개. 행·열 관계만 남긴다"
        )
        for cell in cells:
            cell["bbox"] = None

    n_rows = max((c["row"] + c["row_span"] for c in cells), default=0)
    n_cols = max((c["col"] + c["col_span"] for c in cells), default=0)
    return {
        "n_rows": n_rows, "n_cols": n_cols,
        # 우리 격자 해석이 틀렸을 때 대조할 원본. 버리면 재현할 방법이 없다.
        "html": pred_html or "",
        "cells": cells, "note": note,
    }


def _attach_tables(blocks: list[LayoutBlock], table_res_list, width: int, height: int) -> None:
    """`table_res_list` 의 격자를 같은 표의 `parsing_res_list` 블록에 붙인다.

    짝짓는 기준은 **`block_content` == `pred_html`** 이다 (실측 2026-08-24: 정확히 일치).
    같은 HTML 이 두 번 나오는 문서(실측: `1. 예금성상품(거치식·적립식 통합).pdf` 는
    거치식/적립식 우대금리표가 열 이름만 다르다)가 있으므로 **한 번 쓴 블록은 소비**한다.
    내용이 안 맞으면 셀 합집합이 블록 안에 들어가는지로 폴백한다.
    """
    candidates = [b for b in blocks if b.source == "parsing_res_list" and b.table is None]
    for entry in table_res_list or []:
        if not isinstance(entry, dict):
            continue
        grid = _build_table_grid(entry.get("pred_html") or "", entry.get("cell_box_list"), width, height)
        if not grid["cells"]:
            continue
        html = (entry.get("pred_html") or "").strip()
        target = next((b for b in candidates if (b.content or "").strip() == html and html), None)
        if target is None:
            union = _cells_union(grid["cells"])
            target = next(
                (b for b in candidates
                 if union and _contains(b.bbox, union)
                 and str(b.label).lower() == "table"),
                None,
            )
        if target is None:
            continue
        target.table = grid
        candidates.remove(target)


def _cells_union(cells: list[dict]) -> list[int] | None:
    boxes = [c["bbox"] for c in cells if c.get("bbox")]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _contains(outer: list[int] | None, inner: list[int]) -> bool:
    if not outer:
        return False
    pad = 4  # 셀 테두리 반올림 여유
    return (outer[0] - pad <= inner[0] and outer[1] - pad <= inner[1]
            and outer[2] + pad >= inner[2] and outer[3] + pad >= inner[3])


def request_layout_parsing(image: Image.Image) -> PaddleXPageResult:
    payload = {
        "file": base64.b64encode(_encode_jpeg(image)).decode("ascii"),
        "fileType": 1,
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        # 공식 predict 파라미터의 camelCase 전달 (실서버 동작 검증: 2026-07-17)
        "textDetLimitSideLen": SETTINGS.paddlex_text_det_limit_side_len,
        "textDetLimitType": SETTINGS.paddlex_text_det_limit_type,
        "layoutMergeBboxesMode": SETTINGS.paddlex_layout_merge_bboxes_mode,
        "useFormulaRecognition": SETTINGS.paddlex_use_formula_recognition,
        "useTextlineOrientation": SETTINGS.paddlex_use_textline_orientation,
    }
    resp = requests.post(SETTINGS.paddlex_url, json=payload, timeout=SETTINGS.paddlex_timeout_s)
    resp.raise_for_status()
    body = resp.json()
    if body.get("errorCode") != 0:
        raise RuntimeError(f"PaddleX error {body.get('errorCode')}: {body.get('errorMsg')}")

    pages = (body.get("result") or {}).get("layoutParsingResults") or []
    if not pages:
        raise RuntimeError("PaddleX returned no layoutParsingResults")
    pruned = pages[0].get("prunedResult") or {}
    width = int(pruned.get("width") or image.width)
    height = int(pruned.get("height") or image.height)

    result = PaddleXPageResult(source_width=width, source_height=height)

    ocr = pruned.get("overall_ocr_res") or {}
    texts = ocr.get("rec_texts") or []
    scores = ocr.get("rec_scores") or []
    boxes = ocr.get("rec_boxes") or []
    polys = ocr.get("rec_polys") or []
    for i, text in enumerate(texts):
        bbox = _norm_bbox(boxes[i] if i < len(boxes) else None, width, height)
        if not bbox and i < len(polys):
            bbox = _norm_bbox(polys[i], width, height)
        if not bbox:
            continue
        score = float(scores[i]) if i < len(scores) else None
        result.ocr_lines.append(Line(text=str(text), bbox=bbox, confidence=score, source="ocr"))

    for block in pruned.get("parsing_res_list") or []:
        bbox = _norm_bbox(block.get("block_bbox"), width, height)
        if not bbox:
            continue
        result.blocks.append(
            LayoutBlock(
                label=str(block.get("block_label") or "unknown"),
                bbox=bbox,
                content=block.get("block_content"),
            )
        )
    det_blocks: list[LayoutBlock] = []
    for box in (pruned.get("layout_det_res") or {}).get("boxes") or []:
        bbox = _norm_bbox(box.get("coordinate"), width, height)
        if not bbox:
            continue
        det_blocks.append(
            LayoutBlock(
                label=str(box.get("label") or "unknown"),
                bbox=bbox,
                score=box.get("score"),
                source="layout_det_res",
            )
        )
    _attach_det_scores(result.blocks, det_blocks)
    # `table_res_list`의 HTML/셀 좌표는 광고 파이프라인에서 사용하지 않는다. 표인지에
    # 대한 영역 bbox는 StructureV3 block label로 충분하며, 행/셀 관계는 같은 bbox를
    # 전달받은 table_region_reader VLM이 관측한다. 불안정한 PaddleX 격자를 Region에
    # 붙이면 이후 출력에 다시 정본처럼 섞일 위험이 있어 여기서 차단한다.
    result.blocks.extend(det_blocks)
    return result


# 검출 확신도를 옮겨 붙일 때 요구하는 최소 겹침. 두 목록은 같은 검출에서 나온 것이라
# 대응 박스는 거의 같은 자리에 있다 — 0.8 은 "같은 블록으로 봐도 되는가"의 문턱이지
# 조율한 값이 아니다. 애매하면 안 붙이고 None 으로 둔다(없는 것과 틀린 것 중 없는 쪽).
_SCORE_MATCH_IOU = 0.8


def _attach_det_scores(primary: list[LayoutBlock], det: list[LayoutBlock]) -> None:
    """`parsing_res_list` 블록에 `layout_det_res` 의 검출 확신도를 이어 붙인다.

    **왜 필요한가** (2026-08-06 서버 응답 실측). StructureV3 는 레이아웃을 두 형태로
    돌려주는데 **확신도가 한쪽에만 있다**:

        parsing_res_list  키: block_bbox · block_content · block_id · block_label ·
                              block_order          ← score 없음
        layout_det_res    키: coordinate · label · cls_id · **score**

    영역 조립(regions.build_regions)은 읽기순서와 본문이 담긴 `parsing_res_list` 를
    우선 쓰고 `layout_det_res` 쪽은 중복으로 버린다. 그래서 확신도가 통째로 사라졌다.
    walkthrough §9-⑤ 는 이걸 "Region 에 받는 칸이 없어서"라고 적었는데, 칸을 만들어도
    (2026-08-06 `Region.layout_score` 추가) 값이 0건이었다 — 원인은 우리가 쓰는 목록에
    애초에 안 온다는 것이었다.

    판정에는 안 쓴다. 역할 판정이 규칙과 VLM 사이에서 갈릴 때 "레이아웃 엔진도 확신이
    없던 자리인가"를 보려는 진단값이다.
    """
    for block in primary:
        best, best_iou = None, 0.0
        for cand in det:
            iou = _iou(block.bbox, cand.bbox)
            if iou > best_iou:
                best, best_iou = cand, iou
        if best is not None and best_iou >= _SCORE_MATCH_IOU:
            block.score = best.score


def _iou(a: list[int], b: list[int]) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0
