# -*- coding: utf-8 -*-
"""골드셋 검수용 고해상도 크롭 생성 — 사람/모델이 원본을 직접 판독하기 위한 이미지."""
import sys
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")
Image.MAX_IMAGE_PIXELS = None
DATA = Path(r"c:\Users\cccjj\cginside\repo-analysis\paddle-gemma-orchestrator\nh-data")
OUT = Path(__file__).parent.parent / "out" / "gold_review"
OUT.mkdir(parents=True, exist_ok=True)


def save(img, name, scale=1.0):
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    img.save(OUT / f"{name}.png")
    print(name, img.size)


# 올원e적금 (1122x6429)
ol = Image.open(DATA / "[예금성상품-적금] 올원e적금.png").convert("RGB")
save(ol.crop((0, 0, 1122, 700)), "ol_1_head")
save(ol.crop((0, 700, 1122, 1700)), "ol_2_product")
save(ol.crop((0, 1700, 1122, 2850)), "ol_3_udae", 1.3)
save(ol.crop((0, 2850, 1122, 4100)), "ol_4_udae2", 1.3)
save(ol.crop((0, 4100, 1122, 4650)), "ol_5_prodinfo", 1.3)
save(ol.crop((0, 4550, 1122, 5900)), "ol_6_notice", 1.3)
save(ol.crop((0, 5850, 1122, 6429)), "ol_7_footer", 1.3)

# 002 (720x6111)
p2 = Image.open(DATA / "NH농협은행-2026_002-예금성.png").convert("RGB")
save(p2.crop((0, 0, 720, 1450)), "002_1_head", 1.5)
save(p2.crop((0, 1450, 720, 2350)), "002_2_event_product", 1.6)
save(p2.crop((0, 2350, 720, 3500)), "002_3_prize", 1.5)
save(p2.crop((0, 3500, 720, 4450)), "002_4_method", 1.5)
save(p2.crop((0, 4450, 720, 5150)), "002_5_evnotice", 1.9)
save(p2.crop((0, 5100, 720, 6111)), "002_6_prodnotice", 1.9)

# 001 — PDF 내장 원본 이미지(720x6554)를 다시 추출
from pypdf import PdfReader

r = PdfReader(str(DATA / "NH농협은행-2026_001-예금성.pdf"))
im001 = None
for pimg in r.pages[0].images:
    from io import BytesIO

    im001 = Image.open(BytesIO(pimg.data)).convert("RGB")
    break
save(im001.crop((0, 0, 720, 900)), "001_1_head", 1.8)
save(im001.crop((0, 1450, 720, 2600)), "001_2_method", 1.6)
save(im001.crop((0, 3550, 720, 4300)), "001_3_prize_renewal", 1.8)
save(im001.crop((0, 5150, 720, 5800)), "001_4_evnotice", 2.2)
save(im001.crop((0, 5750, 720, 6554)), "001_5_prodnotice", 2.2)

# 003 — 300dpi 렌더 후 카드별 크롭
pdf = pdfium.PdfDocument(DATA / "NH농협은행-2026_003-예금성.pdf")
pg1 = pdf[0].render(scale=300 / 72).to_pil()  # 3000x1688
pg2 = pdf[1].render(scale=300 / 72).to_pil()
save(pg1.crop((0, 0, 900, 1500)), "003_p1_left")
save(pg1.crop((1100, 380, 2100, 1300)), "003_p1_card1")
save(pg1.crop((2050, 380, 3000, 1300)), "003_p1_card2")
save(pg2.crop((150, 450, 1000, 1350)), "003_p2_evnotice")
save(pg2.crop((1080, 580, 1900, 1250)), "003_p2_prodnotice")
