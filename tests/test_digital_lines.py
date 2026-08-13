# -*- coding: utf-8 -*-
"""표 칸이 한 줄로 뭉개지던 결함(§A) — extract_digital_lines._split_by_column_gap.

세로 겹침만으로 줄을 묶으면 표의 서로 다른 칸("가입금액" | "100만원이상")이 한
줄로 합쳐진다(올원 p1_r006 실측: 옆 칸 내용이 엉뚱한 영역에 붙었다). 실측
(2026-08-13, `1. 예금성상품(거치식).pdf`) 정상 낱말 사이 간격은 평균 글자 폭의
최대 2.2배, 표 칸 경계 간격은 5.6~19배였다 — 그 사이(2.2~5.6배)엔 실제 문장이
없어 4배를 경계로 쓴다.
"""

from pathlib import Path

import pypdfium2 as pdfium
import pytest

from nh_parsing.triage import _split_by_column_gap, extract_digital_lines

SAMPLES = Path(__file__).resolve().parents[1] / "nh-data" / "new-sample-data"
PDF_DEPOSIT = SAMPLES / "1. 예금성상품(거치식).pdf"

CH_W = 10.0  # 이 테스트에서 쓰는 글자 하나의 폭(px) — 배수 계산 기준


def _chars(words: list[str], gaps: list[float]) -> list[tuple[str, tuple[float, float, float, float]]]:
    """words 를 가로로 이어붙인 글자 목록. gaps[i] 는 words[i]-words[i+1] 사이 간격(px).

    실제 extract_digital_lines 는 공백 문자 자체를 건너뛰므로(원본 참조), 여기서도
    공백을 별도 글자로 넣지 않고 두 낱말 사이 간격 값 하나로만 표현한다.
    """
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    x = 0.0
    for wi, word in enumerate(words):
        for ch in word:
            out.append((ch, (x, 0.0, x + CH_W, 20.0)))
            x += CH_W
        if wi < len(gaps):
            x += gaps[wi]
    return out


def test_정상_낱말_간격은_안_쪼갠다():
    """실측 최대 2.2배(1.55x~2.19x 관측) — 한 줄 안의 정상 공백은 그대로 둔다."""
    group = _chars(["AB", "CD"], [CH_W * 2.2])
    assert len(_split_by_column_gap(group)) == 1


def test_표_칸_경계_간격은_쪼갠다():
    """실측 최소 5.6배(가입금액100만원이상 사례) — 표 칸 경계는 확실히 갈라야 한다."""
    group = _chars(["AB", "CD"], [CH_W * 5.6])
    segments = _split_by_column_gap(group)
    assert len(segments) == 2
    assert "".join(c for c, _ in segments[0]) == "AB"
    assert "".join(c for c, _ in segments[1]) == "CD"


def test_여러_칸이면_여러_조각으로_쪼갠다():
    """가입금액 | 100만원이상 | 조건 | 금리(%p) — 4칸 표 헤더 재현."""
    group = _chars(
        ["가입금액", "100만원이상", "조건", "금리"],
        [CH_W * 19, CH_W * 12, CH_W * 6],
    )
    segments = _split_by_column_gap(group)
    texts = ["".join(c for c, _ in seg) for seg in segments]
    assert texts == ["가입금액", "100만원이상", "조건", "금리"]


def test_글자_하나면_그대로_돌려준다():
    group = [("A", (0.0, 0.0, 10.0, 20.0))]
    assert _split_by_column_gap(group) == [group]


@pytest.mark.skipif(not PDF_DEPOSIT.exists(), reason="샘플 PDF 없음(비공개)")
def test_실제_파일에서_옆칸_내용이_더는_안_붙는다():
    """회귀 고정 — 수정 전엔 '가입금액100만원이상조건금리(%p)' 로 3칸이 통짜였다.

    이 통짜 줄이 나중에 build_regions 에서 중심점 기준 영역 하나(p1_r006, 오른쪽
    칸)에 전부 붙어 왼쪽 칸 내용('가입금액100만원이상')이 오른쪽 영역으로 침범했다
    (사용자 실측 지적). 여기서는 extract_digital_lines 단계만 보되, 옆 칸 라벨과
    합쳐지지 않고 실제로 갈라졌는지를 고정한다.
    """
    pdf = pdfium.PdfDocument(str(PDF_DEPOSIT))
    lines = extract_digital_lines(pdf[0], 200 / 72.0)
    texts = [l.text for l in lines]
    assert "가입금액100만원이상조건금리(%p)" not in texts, "3칸이 다시 통짜로 합쳐졌다"
    # 칸은 갈라지되 내용은 그대로 남아 있어야 한다(유실 금지)
    assert any("가입금액100만원이상" == t for t in texts)
    assert any(t == "조건" for t in texts)
    assert any("금리" in t and "%p" in t for t in texts)
