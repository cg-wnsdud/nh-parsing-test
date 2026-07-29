from __future__ import annotations

"""PaddleX PP-StructureV3 클라이언트 [B2-3] — 설계서 6.4절.

이전 프로젝트 orchestrator/adapters/paddlex.py 의 요청/응답 계약을 따른다.
타일 이미지를 보내고 레이아웃 영역 + OCR 라인을 받아온다.
좌표는 보낸 이미지(=타일) 픽셀 기준이므로 호출측에서 y_offset 만 더하면 된다.
"""

import base64
import io
from dataclasses import dataclass, field

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
    for box in (pruned.get("layout_det_res") or {}).get("boxes") or []:
        bbox = _norm_bbox(box.get("coordinate"), width, height)
        if not bbox:
            continue
        result.blocks.append(
            LayoutBlock(
                label=str(box.get("label") or "unknown"),
                bbox=bbox,
                score=box.get("score"),
                source="layout_det_res",
            )
        )
    return result
