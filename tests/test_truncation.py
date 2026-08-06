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


# ───────────────────── 숫자 게이트 ─────────────────────


def test_소수점이_사라진_금리는_불일치다():
    """002 p1_r014 실측 — 금리 10배 차이가 relation=same 으로 묻혀 있었다.
    VLM 이 정확히 읽었는데도 교차검증 경고가 뜨지 않았다.

    이 딱지는 2026-08-05 에 SAME → DIVERGED 로 **뒤집혔다.** 예전 값은 norm() 이
    마침표를 지워 '7.1' 과 '71' 이 둘 다 '71' 로 뭉개진 결과였다.
    """
    assert classify_reading("최고연71%", "최고연 7.1%").kind == DIVERGED
    # 소수점 위치만 다른 경우도 같다 (합성 케이스 — 위와 같은 규칙을 반대 방향으로 검사)
    assert classify_reading("10.1%p 조건", "① 0.1%p 조건").kind == DIVERGED


def test_붙여_읽은_전화번호는_불일치다():
    """003 p2_r008 실측 — OCR 이 '3000,1522' 를 '30001522' 로 붙여 읽었다."""
    ocr = "-기타 자세한 내용은 고객행복센터(1661-3000,1522-3000)로 문의하시기 바랍니다."
    vlm = "-기타 자세한 내용은 고객행복센터(1661-3000, 1522-3000)로 문의하시기 바랍니다."
    assert classify_reading(ocr, vlm).kind == DIVERGED


def test_한자리_숫자가_한쪽에만_있으면_원문자_회수로_본다():
    """VLM 이 원문자를 정확히 읽어 정본에 없던 번호가 생긴 경우. 표기가 나아진
    것이므로 경고를 올리지 않는다 — 위 test_원문자는_숫자와_같은_것으로_본다 와
    같은 규칙이고, 게이트가 그것을 깨지 않는지 여기서 다시 못박는다."""
    assert classify_reading("우리아이 계좌개설", "① 우리아이 계좌개설").kind == SAME
    assert classify_reading("우리아이 예·적금 가입 (12월 3일 오픈)",
                            "④ 우리아이 예·적금 가입 (12월 3일 오픈)").kind == SAME


def test_유사도가_높아도_글자가_다르면_불일치다():
    """유사도 폴백(0.92)을 2026-08-05 에 걷어냈다. norm() 이 구두점을 이미 지우므로
    그 뒤에 남은 차이는 표기차가 아니라 글자다. 실측 7건이 전부 OCR 오독이었다.

    아래 마지막 줄이 임계 조정으로는 못 잡는 이유다 — 긴 문장의 한 글자 오독은
    유사도가 0.994 다.
    """
    assert classify_reading("소비습관의 첫결음 체크카드 발급하기",
                            "소비습관의 첫걸음 체크카드 발급하기").kind == DIVERGED
    assert classify_reading("-본 이벤트는 NH놓협은행의 사정에 따라 변경될 수 있습니다.",
                            "-본 이벤트는 NH농협은행의 사정에 따라 변경될 수 있습니다.").kind == DIVERGED
    ocr = "-우대이자율:최고2.4%(기본이자을포함시최고2.5%/일별 잔액3백만원까지)"
    vlm = "-우대이자율: 최고 2.4% (기본이자율을 포함 시 최고 2.5% / 일별 잔액 3백만원까지)"
    assert classify_reading(ocr, vlm).kind == DIVERGED


def test_길이가_같은_한글자_오독은_잘림이_아니다():
    """002 p1_r006 실측 — 정규화 길이가 13 대 13 으로 같은데, 앞 11자가 일치하고
    정본 꼬리가 15.4% 라 옛 조건(최장일치 뒤 비율만 봄)이 tail_cut 을 붙였다.
    잘린 게 아니라 뒤에 서로 다른 글자가 각각 남은 것이다."""
    rel = classify_reading("농협은행이 777명에게 쓴다!", "농협은행이 777명에게 쏜다!")
    assert rel.kind == DIVERGED
    assert not rel.is_truncated


def test_중간에_오독이_있어도_짧아졌으면_잘림이다():
    """003 p2_r004 실측(축약) — 문장 중간의 '미용모'→'미모임' 오독 때문에 최장일치가
    일찍 끊기고 후보 뒤에 글자가 남는다. 그래도 후보가 정본보다 훨씬 짧으므로
    잘림이다. 잘림 여부는 일치 위치가 아니라 길이로 판단한다."""
    ocr = ("-본이벤트 시작일이전 모임 개설 이력이 있을 경우 해당 이벤트 당첨이 불가합니다. "
           "-이벤트 기간 내 응모하기는 필수이며 조건을 충족하여도 미용모 시 제외됩니다. "
           "-모임주가 여러개의 모임통장을 보유한 경우 최초 개설한 모임으로 지급됩니다.")
    vlm = ("-본 이벤트 시작일 이전 모임 개설 이력이 있을 경우 해당 이벤트 당첨이 불가합니다. "
           "-이벤트 기간 내 응모하기는 필수이며 조건을 충족하여도 미모임 시 제외됩니다. -모임주가")
    rel = classify_reading(ocr, vlm)
    assert rel.kind == TAIL_TRUNCATED and rel.is_truncated


def test_한자리_숫자가_양쪽에_다_있는데_다르면_불일치다():
    """003 p1_r008 실측 — OCR 이 '②' 를 '1' 로 읽었다. 양쪽에 한 자리가 다 있어
    표기차로 넘길 수 없다. '연 3%' vs '연 5%' 같은 실제 값 차이를 지키는 규칙이다."""
    assert classify_reading("1모임주포함2인이상의모임원초대하기",
                            "② 모임주 포함 2인 이상의 모임원 초대하기").kind == DIVERGED
    assert classify_reading("연 3% 적용", "연 5% 적용").kind == DIVERGED


def test_숫자_게이트는_같다로_떨어진_것만_본다():
    """잘림·회수는 숫자가 달라지는 게 정상이다 — 뒤가 잘리면 그 안의 숫자도 없어진다.
    게이트가 그 딱지를 DIVERGED 로 덮어쓰면 '무해한 항목명 생략'과 '유해한 문장
    잘림'을 가르는 이 모듈의 존재 이유가 무너진다."""
    ocr = "가입기간 12개월 최고 연 3.5% 세전 이자율이 적용됩니다 우대조건 확인"
    assert classify_reading(ocr, "가입기간 12개월 최고 연 3.5%").kind == TAIL_TRUNCATED
    assert classify_reading("상품명 NH올원e적금 3호", "NH올원e적금 3호").kind == HEAD_DROPPED
