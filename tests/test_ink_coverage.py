# -*- coding: utf-8 -*-
"""글자를 도형으로 그린 페이지를 라우팅이 못 잡던 사각 (bands.ink_coverage).

실측 근거(2026-08-14): `2. 예금성상품(입출식).pdf` p1 은 본문 전체가 벡터 도형으로
그려져 텍스트 레이어에는 심의필 문구와 숫자 파편 124자만 들어왔다. 기존 판정은
글자수 하한(20자)만 보므로 structured 로 떨어졌고 OCR 을 건너뛰어 예금자보호법·
금융소비자보호법 등 **의무고지 14개 문구가 통째로 유실**됐다.

외부 파서(kordoc 4.7.3)도 같은 파일에서 `totalTextChars: 124`, `needsOcr: false` 로
똑같이 오판했다 — 뽑힌 글자만 세는 지표로는 구조적으로 못 잡는다.

모델 호출 없음 — 합성 이미지로 순수 픽셀 계산만 본다.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from nh_parsing.bands import ink_coverage
from nh_parsing.config import SETTINGS


def _canvas(strokes: list[tuple[int, int, int, int]]) -> Image.Image:
    """흰 바탕에 검은 획을 그린 캔버스 — 획이 곧 '글자처럼 보이는 것'이다."""
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in strokes:
        for x in range(x0, x1, 4):        # 세로획을 촘촘히 = 글자 덩어리
            d.line([(x, y0), (x, y1)], fill=(0, 0, 0), width=1)
    return img


def test_텍스트가_잉크를_다_설명하면_높게_나온다():
    img = _canvas([(50, 50, 200, 80)])
    assert ink_coverage(img, [[50, 50, 200, 80]]) > 0.9


def test_잉크는_있는데_텍스트_상자가_없으면_0에_가깝다():
    """벡터로 그린 글자 — 화면엔 보이는데 텍스트 레이어엔 없다."""
    img = _canvas([(50, 50, 350, 250)])
    assert ink_coverage(img, []) == 0.0


def test_일부만_설명하면_그_비율만큼_나온다():
    """실측 케이스의 축소판 — 심의필 한 줄만 텍스트고 본문은 전부 도형."""
    img = _canvas([(20, 20, 120, 35), (20, 80, 380, 260)])   # 작은 머리글 + 큰 본문
    cov = ink_coverage(img, [[20, 20, 120, 35]])             # 머리글만 텍스트 레이어에 있음
    assert 0.0 < cov < SETTINGS.min_ink_coverage, (
        f"본문이 전부 도형이면 임계({SETTINGS.min_ink_coverage:.0%}) 아래여야 한다 (실제 {cov:.1%})"
    )


def test_잉크가_없는_빈_페이지는_판정을_바꾸지_않는다():
    """0 을 돌려주면 빈 페이지가 전부 hybrid 로 내려간다 — 안전측으로 1.0."""
    assert ink_coverage(Image.new("RGB", (400, 300), "white"), []) == 1.0


def test_크기가_없는_이미지도_판정을_안_바꾼다():
    assert ink_coverage(Image.new("RGB", (0, 0)), []) == 1.0


def test_임계값이_실측_공백_안에_있다():
    """29쪽 실측에서 2.5% 다음이 56.3% 였다 — 그 사이여야 둘 다 올바르게 갈린다."""
    assert 0.025 < SETTINGS.min_ink_coverage < 0.563
