"""같은 OCR 타일을 현재 JPEG 90과 무손실 PNG로 보내 결과를 비교한다.

파이프라인을 바꾸지 않는 진단 도구다. 원본 파일을 현재와 같은 방식으로 캔버스화하고
같은 타일을 두 번 호출하여 전송 바이트, 응답 시간, OCR 텍스트/숫자/신뢰도를 기록한다.
엔드포인트와 비밀값은 결과 파일에 기록하지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from nh_parsing.canvas import load_image_canvas, native_image_dpi, render_pdf_page  # noqa: E402
from nh_parsing.config import SETTINGS  # noqa: E402
from nh_parsing.tiling import make_tiles  # noqa: E402
from nh_parsing.triage import triage_page  # noqa: E402


@dataclass(frozen=True)
class Case:
    case_id: str
    source: Path
    page_no: int
    target_y: int | None
    expected: tuple[str, ...]
    note: str


CASES = (
    Case(
        "low_res_png",
        ROOT / "nh-data/new-sample-data/4-1. 예금성상품(거치식).png",
        1,
        223,
        (),
        "원본 자체가 219x720인 저화질 PNG. 압축 차이만 비교하고 자동 정답 판정은 하지 않는다.",
    ),
    Case(
        "long_event_decimal",
        ROOT / "nh-data/sample-data/NH농협은행-2026_002-예금성.png",
        1,
        2125,
        ("최고연7.1%",),
        "장문 PNG의 큰 금리 숫자. 원본 육안 정답은 '최고 연 7.1%'.",
    ),
    Case(
        "long_card_amount",
        ROOT / "nh-data/new-sample-data/5. 카드상품.png",
        1,
        801,
        ("17만원",),
        "장문 PNG의 금액 문구. 현재 결과의 '17만원' 보존 여부를 본다.",
    ),
    Case(
        "scan_pdf_amount",
        ROOT / "nh-data/sample-data/NH농협은행-2026_001-예금성.pdf",
        1,
        3680,
        ("20000원",),
        "scan_like PDF를 PDFium으로 렌더한 실제 OCR 타일. 육안 정답은 '20,000원'.",
    ),
)


def _encode(image: Image.Image, variant: str) -> bytes:
    buf = io.BytesIO()
    if variant == "jpeg90":
        image.save(buf, format="JPEG", quality=90)
    elif variant == "png":
        image.save(buf, format="PNG")
    else:
        raise ValueError(variant)
    return buf.getvalue()


def _payload(encoded: bytes) -> dict[str, Any]:
    return {
        "file": base64.b64encode(encoded).decode("ascii"),
        "fileType": 1,
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "textDetLimitSideLen": SETTINGS.paddlex_text_det_limit_side_len,
        "textDetLimitType": SETTINGS.paddlex_text_det_limit_type,
        "layoutMergeBboxesMode": SETTINGS.paddlex_layout_merge_bboxes_mode,
        "useFormulaRecognition": SETTINGS.paddlex_use_formula_recognition,
        "useTextlineOrientation": SETTINGS.paddlex_use_textline_orientation,
    }


def _call(encoded: bytes) -> tuple[dict[str, Any], float, int]:
    started = time.perf_counter()
    response = requests.post(
        SETTINGS.paddlex_url,
        json=_payload(encoded),
        timeout=SETTINGS.paddlex_timeout_s,
    )
    elapsed = time.perf_counter() - started
    response_size = len(response.content)
    response.raise_for_status()
    body = response.json()
    if body.get("errorCode") != 0:
        raise RuntimeError(f"PaddleX error {body.get('errorCode')}: {body.get('errorMsg')}")
    pages = (body.get("result") or {}).get("layoutParsingResults") or []
    if not pages:
        raise RuntimeError("PaddleX returned no layoutParsingResults")
    return pages[0].get("prunedResult") or {}, elapsed, response_size


def _norm(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣.%]", "", text).lower()


def _numbers(texts: list[str]) -> list[str]:
    found: list[str] = []
    for text in texts:
        found.extend(re.findall(r"\d[\d,.]*(?:%|원|만원|억원|년|월|일|p)?", text))
    return found


def _line_records(pruned: dict[str, Any]) -> list[dict[str, Any]]:
    ocr = pruned.get("overall_ocr_res") or {}
    texts = ocr.get("rec_texts") or []
    scores = ocr.get("rec_scores") or []
    boxes = ocr.get("rec_boxes") or []
    records: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        records.append(
            {
                "text": str(text),
                "score": float(scores[index]) if index < len(scores) else None,
                "bbox": boxes[index] if index < len(boxes) else None,
            }
        )
    return records


def _nearby(lines: list[dict[str, Any]], target_rel_y: int | None) -> list[dict[str, Any]]:
    if target_rel_y is None:
        return lines
    result = []
    for line in lines:
        box = line.get("bbox")
        if not isinstance(box, list) or len(box) != 4:
            continue
        center = (float(box[1]) + float(box[3])) / 2
        if abs(center - target_rel_y) <= 280:
            result.append(line)
    return result


def _summarize(
    case: Case,
    variant: str,
    encoded: bytes,
    pruned: dict[str, Any],
    elapsed: float,
    response_size: int,
    target_rel_y: int | None,
) -> dict[str, Any]:
    lines = _line_records(pruned)
    near = _nearby(lines, target_rel_y)
    scores = [line["score"] for line in lines if line["score"] is not None]
    nearby_text = "\n".join(line["text"] for line in near)
    normalized_nearby = _norm(nearby_text)
    expected = {
        phrase: _norm(phrase) in normalized_nearby
        for phrase in case.expected
    }
    return {
        "variant": variant,
        "encoded_bytes": len(encoded),
        "base64_chars": 4 * math.ceil(len(encoded) / 3),
        "elapsed_s": round(elapsed, 3),
        "response_bytes": response_size,
        "server_width": pruned.get("width"),
        "server_height": pruned.get("height"),
        "line_count": len(lines),
        "mean_confidence": round(statistics.mean(scores), 6) if scores else None,
        "min_confidence": round(min(scores), 6) if scores else None,
        "nearby_lines": near,
        "nearby_numbers": _numbers([line["text"] for line in near]),
        "expected_matches": expected,
        "all_text_normalized": _norm("\n".join(line["text"] for line in lines)),
    }


def _pixel_metrics(image: Image.Image, jpeg_bytes: bytes) -> dict[str, Any]:
    decoded = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    diff = ImageChops.difference(image.convert("RGB"), decoded)
    stat = ImageStat.Stat(diff)
    mean_abs = statistics.mean(stat.mean)
    sq = ImageChops.difference(image.convert("RGB"), decoded)
    channel_rms = ImageStat.Stat(sq).rms
    rms = math.sqrt(sum(value * value for value in channel_rms) / len(channel_rms))
    psnr = float("inf") if rms == 0 else 20 * math.log10(255 / rms)
    return {
        "mean_abs_error": round(mean_abs, 4),
        "rms_error": round(rms, 4),
        "psnr_db": None if math.isinf(psnr) else round(psnr, 3),
    }


def _select_tile(image: Image.Image, target_y: int | None):
    tiles = make_tiles(image)
    if target_y is None:
        return tiles[0], 0, len(tiles)
    containing = [
        tile for tile in tiles
        if tile.y_offset <= target_y < tile.y_offset + tile.image.height
    ]
    if not containing:
        raise RuntimeError(f"target_y={target_y}를 포함하는 타일이 없다")
    tile = max(
        containing,
        key=lambda item: min(target_y - item.y_offset, item.y_offset + item.image.height - target_y),
    )
    return tile, target_y - tile.y_offset, len(tiles)


def _pipeline_canvas(case: Case) -> tuple[Image.Image, dict[str, Any]]:
    """실제 pipeline._process_pdf와 같은 PDF 렌더 정책으로 캔버스를 만든다."""
    if case.source.suffix.lower() != ".pdf":
        canvas = load_image_canvas(case.source)
        return canvas.image.convert("RGB"), {"kind": "image_native_pixels", "dpi": None}

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(case.source))
    page = pdf[case.page_no - 1]
    verdict = triage_page(page)
    dpi = native_image_dpi(page) if verdict.verdict in {"scan_like", "hybrid"} else None
    canvas = render_pdf_page(page, case.page_no, dpi=dpi)
    return canvas.image.convert("RGB"), {
        "kind": "pdfium_pipeline_render",
        "triage": verdict.verdict,
        "requested_dpi": dpi,
        "actual_dpi": canvas.dpi,
        "px_per_pt": canvas.px_per_pt,
    }


def _safe_pruned(pruned: dict[str, Any]) -> dict[str, Any]:
    """응답 시각화 Base64는 버리고 실제 판독·레이아웃 데이터만 보존한다."""
    return {key: value for key, value in pruned.items() if key not in {"input_img", "outputImages"}}


def _pair_comparison(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    jpg = results["jpeg90"]
    png = results["png"]
    jpg_text = jpg.pop("all_text_normalized")
    png_text = png.pop("all_text_normalized")
    return {
        "normalized_text_similarity": round(SequenceMatcher(None, jpg_text, png_text).ratio(), 6),
        "line_count_delta_png_minus_jpeg": png["line_count"] - jpg["line_count"],
        "mean_confidence_delta_png_minus_jpeg": (
            round(png["mean_confidence"] - jpg["mean_confidence"], 6)
            if png["mean_confidence"] is not None and jpg["mean_confidence"] is not None
            else None
        ),
        "encoded_size_ratio_png_over_jpeg": round(png["encoded_bytes"] / jpg["encoded_bytes"], 4),
        "expected_match_change": {
            phrase: {"jpeg90": jpg["expected_matches"][phrase], "png": png["expected_matches"][phrase]}
            for phrase in jpg["expected_matches"]
        },
    }


def _report(data: dict[str, Any]) -> str:
    rows = []
    for case in data["cases"]:
        jpg = case["variants"]["jpeg90"]
        png = case["variants"]["png"]
        pair = case["comparison"]
        expected = ", ".join(
            f"{key}: J={value['jpeg90']}/P={value['png']}"
            for key, value in pair["expected_match_change"].items()
        ) or "정답표 없음"
        rows.append(
            "| {id} | {size} | {lines} | {conf} | {sim} | {expected} |".format(
                id=case["case_id"],
                size=f"{jpg['encoded_bytes']:,} / {png['encoded_bytes']:,}",
                lines=f"{jpg['line_count']} / {png['line_count']}",
                conf=f"{jpg['mean_confidence']} / {png['mean_confidence']}",
                sim=pair["normalized_text_similarity"],
                expected=expected,
            )
        )
    return "\n".join(
        [
            "# OCR 전송 인코딩 A/B",
            "",
            "같은 픽셀 타일을 현재 방식인 JPEG quality 90과 무손실 PNG로 각각 PaddleX에 보낸 결과다.",
            "정답 문구는 육안 또는 PDF 원문으로 확인 가능한 사례만 사용했다.",
            "",
            "| 사례 | 전송 bytes JPEG/PNG | 줄 수 JPEG/PNG | 평균 confidence JPEG/PNG | 텍스트 유사도 | 정답 문구 포함 |",
            "|---|---:|---:|---:|---:|---|",
            *rows,
            "",
            "## 해석 주의",
            "",
            "- confidence는 모델 내부 점수이므로 포맷 간 작은 차이를 실제 정확도 차이로 단정하지 않는다.",
            "- PNG가 더 좋은지는 정답 문구 회수, 숫자 보존, 반복 표본 평가로 판단한다.",
            "- PDF 사례는 실제 파이프라인과 같이 scan_like/hybrid이면 내장 이미지 원본 DPI로 PDFium 렌더한 픽셀을 비교한다.",
            "  PDF 파일 바이트를 서버에 직접 보낸 비교가 아니라, 파이프라인이 실제 OCR에 보내는 렌더 타일 비교다.",
        ]
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "0827-parsing/ocr-transport-ab")
    args = parser.parse_args()
    out = args.out.resolve()
    raw_dir = out / "raw"
    tile_dir = out / "tiles"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tile_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "method": "same decoded tile pixels; current JPEG quality=90 vs lossless PNG",
        "settings": {
            "textDetLimitSideLen": SETTINGS.paddlex_text_det_limit_side_len,
            "textDetLimitType": SETTINGS.paddlex_text_det_limit_type,
            "layoutMergeBboxesMode": SETTINGS.paddlex_layout_merge_bboxes_mode,
            "useTextlineOrientation": SETTINGS.paddlex_use_textline_orientation,
        },
        "cases": [],
    }

    for case_index, case in enumerate(CASES):
        print(f"[{case_index + 1}/{len(CASES)}] {case.case_id}: canvas/tile 준비", flush=True)
        image, render_info = _pipeline_canvas(case)
        tile, target_rel_y, tile_count = _select_tile(image, case.target_y)
        encodings = {name: _encode(tile.image, name) for name in ("jpeg90", "png")}
        (tile_dir / f"{case.case_id}.jpg").write_bytes(encodings["jpeg90"])
        (tile_dir / f"{case.case_id}.png").write_bytes(encodings["png"])

        case_result: dict[str, Any] = {
            "case_id": case.case_id,
            "source": str(case.source.relative_to(ROOT)),
            "page_no": case.page_no,
            "note": case.note,
            "canvas_size": list(image.size),
            "render_info": render_info,
            "tile_count": tile_count,
            "tile_y_offset": tile.y_offset,
            "tile_size": list(tile.image.size),
            "target_y_canvas": case.target_y,
            "target_y_tile": target_rel_y,
            "jpeg_pixel_distortion": _pixel_metrics(tile.image, encodings["jpeg90"]),
            "variants": {},
        }

        order = ("jpeg90", "png") if case_index % 2 == 0 else ("png", "jpeg90")
        for variant in order:
            print(f"  - {variant} 전송", flush=True)
            pruned, elapsed, response_size = _call(encodings[variant])
            summary = _summarize(
                case, variant, encodings[variant], pruned, elapsed, response_size, target_rel_y
            )
            case_result["variants"][variant] = summary
            (raw_dir / f"{case.case_id}__{variant}.json").write_text(
                json.dumps(_safe_pruned(pruned), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        case_result["comparison"] = _pair_comparison(case_result["variants"])
        report["cases"].append(case_result)
        (out / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    (out / "report.md").write_text(_report(report), encoding="utf-8")
    print(f"완료: {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
