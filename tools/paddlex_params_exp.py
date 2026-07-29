# -*- coding: utf-8 -*-
"""PaddleX 서빙 파라미터 실험 — 공식 predict 파라미터의 camelCase 전달 검증.

실험 1: textDetLimitSideLen (기본 960/max 다운스케일 의심)
  - PNG-002 헤드라인 밴드: '최고 연 7.1%' 가 '71%' 로 읽히는 문제
  - 올원 하단 fine-print 밴드: 깨진 라인('링이어니을용은' 등)
실험 2: layoutMergeBboxesMode (기본 large 가 003 p1 통짜 원인 의심)
"""
import base64
import io
import json
import sys
from pathlib import Path

import requests
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from nh_parsing.config import SETTINGS

Image.MAX_IMAGE_PIXELS = None
DATA = Path(r"c:\Users\cccjj\cginside\repo-analysis\paddle-gemma-orchestrator\nh-data")


def call(img: Image.Image, extra: dict) -> dict:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    payload = {
        "file": base64.b64encode(buf.getvalue()).decode("ascii"),
        "fileType": 1,
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        **extra,
    }
    r = requests.post(SETTINGS.paddlex_url, json=payload, timeout=SETTINGS.paddlex_timeout_s)
    r.raise_for_status()
    body = r.json()
    if body.get("errorCode") != 0:
        raise RuntimeError(body.get("errorMsg"))
    return body["result"]["layoutParsingResults"][0]["prunedResult"]


def show_ocr(pruned, needle_list):
    ocr = pruned.get("overall_ocr_res") or {}
    texts, scores = ocr.get("rec_texts") or [], ocr.get("rec_scores") or []
    print(f"    ocr_lines={len(texts)}")
    for t, s in zip(texts, scores):
        if any(n in t for n in needle_list):
            print(f"    HIT: {t!r} (conf {s:.2f})")


# ── 실험 1a: PNG-002 헤드라인 밴드 (7.1% 문제) ──────────────────────
img2 = Image.open(DATA / "NH농협은행-2026_002-예금성.png")
band = img2.crop((0, 1900, 720, 2300))  # '최고 연 7.1%' 특판 금리 박스 구간
print("=== 실험 1a: PNG-002 금리 헤드라인 밴드 (원본 720px 폭) ===")
for tag, extra in [
    ("기본(=서버 default)", {}),
    ("textDetLimitSideLen=2500/max", {"textDetLimitSideLen": 2500, "textDetLimitType": "max"}),
    ("textDetLimitSideLen=1200/min", {"textDetLimitSideLen": 1200, "textDetLimitType": "min"}),
]:
    try:
        pruned = call(band, extra)
        print(f"  [{tag}]")
        show_ocr(pruned, ["7", "%", "최고"])
    except Exception as e:
        print(f"  [{tag}] 실패: {e}")

# ── 실험 1b: 올원 fine-print 밴드 (깨진 라인 문제) ───────────────────
img_o = Image.open(DATA / "[예금성상품-적금] 올원e적금.png")
band_o = img_o.crop((0, 5300, 1122, 5700))
print("\n=== 실험 1b: 올원 fine-print 밴드 (원본 1122px 폭) ===")
for tag, extra in [
    ("기본", {}),
    ("textDetLimitSideLen=2500/max", {"textDetLimitSideLen": 2500, "textDetLimitType": "max"}),
]:
    try:
        pruned = call(band_o, extra)
        ocr = pruned.get("overall_ocr_res") or {}
        print(f"  [{tag}] 전체 라인:")
        for t, s in zip(ocr.get("rec_texts") or [], ocr.get("rec_scores") or []):
            print(f"    {s:.2f} {t!r}")
    except Exception as e:
        print(f"  [{tag}] 실패: {e}")

# ── 실험 2: 003 p1 layoutMergeBboxesMode ───────────────────────────
import pypdfium2 as pdfium

pdf = pdfium.PdfDocument(DATA / "NH농협은행-2026_003-예금성.pdf")
p1 = pdf[0].render(scale=200 / 72).to_pil()
print("\n=== 실험 2: 003 p1 카드 콜라주 — layoutMergeBboxesMode ===")
for tag, extra in [
    ("기본(large)", {}),
    ("small", {"layoutMergeBboxesMode": "small"}),
    ("union", {"layoutMergeBboxesMode": "union"}),
]:
    try:
        pruned = call(p1, extra)
        blocks = pruned.get("parsing_res_list") or []
        from collections import Counter

        labels = Counter(b.get("block_label") for b in blocks)
        print(f"  [{tag}] parsing_res_list={len(blocks)} labels={dict(labels)}")
    except Exception as e:
        print(f"  [{tag}] 실패: {e}")
