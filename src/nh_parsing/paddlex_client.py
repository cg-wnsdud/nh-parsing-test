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
