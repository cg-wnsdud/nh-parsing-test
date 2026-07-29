# -*- coding: utf-8 -*-
"""판독 관계 판정 — 실측값을 그대로 고정한다.

여기 있는 문자열은 전부 out/json 에서 뽑은 실제 값이다. 임계값을 만지면 이 테스트가
먼저 깨져야 한다 — '무해한 항목명 생략'과 '유해한 문장 잘림'을 가르는 게 이 모듈의
존재 이유이고, 둘은 커버리지 숫자로는 같은 대역에 있다.
"""

from nh_parsing.truncation import (
    DIVERGED, EXPANDED, HEAD_DROPPED, SAME, TAIL_TRUNCATED, classify_reading, norm,
)


# ───────────────────── 유해: 문장이 잘린 경우 ─────────────────────


def test_문장_중간에서_끊긴_판독은_잘림이다():
    """올원e p1_r041 실측 — 정밀도 1.0(만점)이 나오던 바로 그 케이스."""
    ocr = ("예금잔액증명서 발급 당일 및 계좌에 (가)압류 등 법적 지급제한조치,"
           "질권설정 등이 등록될 경우 원금 및 이자")
    vlm = "• 예금잔액증명서 발급 당일 및 계좌에 (가)압류 등 법적"
    rel = classify_reading(ocr, vlm)
    assert rel.kind == TAIL_TRUNCATED
    assert rel.is_truncated
    assert rel.lost_tail > 0.4


def test_값을_통째로_빠뜨린_판독은_잘림이다():
    """002 p1_r018 실측 — 경품 금액('네이버페이 20,000원')이 통째로 사라졌다."""
    ocr = "경품안내 총777명추첨 N pay 포인트 쿠폰 네이버페이 20,000원"
    vlm = "총 777명 추첨"
    rel = classify_reading(ocr, vlm)
    assert rel.kind == TAIL_TRUNCATED, f"경품 금액 소실을 못 잡았다: {rel}"


# ───────────────────── 무해: 항목명만 뺀 경우 ─────────────────────


def test_항목명_생략은_잘림이_아니다():
    """올원e p1_r004/r005/r006 실측 — 커버리지 0.50~0.75 로 위 잘림과 같은 대역이다.

    커버리지만 보면 못 가른다. 값 자체는 온전하므로 경보를 내면 안 된다.
    """
    for ocr, vlm in [
        ("상품명 NH올원e적금", "NH올원e적금"),
        ("가입대상 개인(1인 1계좌)", "개인(1인 1계좌)"),
        ("가입기간 12 개월", "12 개월"),
    ]:
        rel = classify_reading(ocr, vlm)
        assert rel.kind == HEAD_DROPPED, f"{ocr!r} → {vlm!r} 를 {rel.kind} 로 봤다"
        assert not rel.is_truncated


# ───────────────────── 표기 차이 ─────────────────────


def test_괄호_모양_차이는_같은_것으로_본다():
    """002 p1_r010 실측 — OCR 대괄호 vs VLM 낫표. 내용은 동일하다."""
    assert classify_reading("[NH대박7적금]가입고객", "「NH대박7적금」 가입고객").kind == SAME


def test_공백_구두점만_다르면_같다():
    assert classify_reading("최대 연3.6%(기본3.1%)", "최대 연 3.6% (기본 3.1%)").kind == SAME


def test_아주_짧은_소실은_표기차로_본다():
    """구두점 한둘 차이로 경보가 뜨면 라벨이 의미를 잃는다."""
    long = "가" * 100
    assert classify_reading(long + "나", long).kind == SAME


# ───────────────────── 회수(이득) ─────────────────────


def test_더_많이_읽으면_회수다():
    ocr = "최고연"
    vlm = "최고 연 7.1% 인생 대박적금이 온다"
    assert classify_reading(ocr, vlm).kind == EXPANDED


def test_정본이_비면_회수다():
    assert classify_reading("", "새로 읽은 문구").kind == EXPANDED


# ───────────────────── 불일치 ─────────────────────


def test_내용이_다르면_불일치다():
    assert classify_reading("가입기간 12개월", "연이자율 3.6퍼센트").kind == DIVERGED


def test_판독이_비면_잘림이다():
    assert classify_reading("무언가 읽힌 정본", "").kind == TAIL_TRUNCATED


# ───────────────────── 라벨 ─────────────────────


def test_라벨은_사람이_읽을_수_있어야_한다():
    rel = classify_reading("가나다라마바사아자차카타파하", "가나다라마")
    assert "잘림" in rel.label and "%" in rel.label


def test_정규화는_글자와_숫자만_남긴다():
    assert norm("0.1%p : 「NH올원e통장」") == "01pnh올원e통장"


def test_원문자는_숫자와_같은_것으로_본다():
    """001 p1_r006 실측 — VLM 이 '1' 을 '①' 로 더 정확히 읽었는데, 원문자를 지워
    버리면 '1 이 사라졌다'가 되어 잘림 오탐이 난다."""
    assert norm("참여방법 1") == norm("참여 방법 ①")
    assert classify_reading("참여방법 1", "참여 방법 ①").kind == SAME
    assert classify_reading("10.1%p 조건", "① 0.1%p 조건").kind == SAME
