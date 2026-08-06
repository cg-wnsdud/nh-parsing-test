# -*- coding: utf-8 -*-
"""VLM 통독 후보와 OCR 정본의 '관계'를 경계 기준으로 판정한다.

**왜 필요한가.** 후보 점수를 지금까지 check_field_consistency(토큰 겹침)로만 냈는데,
토큰 겹침은 순서를 버리기 때문에 **잘린 판독이 만점을 받는다** — 잘린 부분은 애초에
비교 대상이 아니므로 남은 부분은 100% 정확하다. 실측(올원e p1_r041):

    OCR : 예금잔액증명서 발급 당일 및 계좌에 (가)압류 등 법적 지급제한조치, 질권설정 등이…
    VLM : • 예금잔액증명서 발급 당일 및 계좌에 (가)압류 등 법적          ← 문장 중간에서 끊김
    정밀도 1.0 (만점)   커버리지 0.59

커버리지로 거르면 될 것 같지만 안 된다. 같은 커버리지 대역에 **무해한 경우**가 섞인다:

    OCR : 상품명 NH올원e적금        VLM : NH올원e적금      커버리지 0.50
    → 잘린 게 아니라 항목명을 뺀 것. 값 자체는 온전하다.

두 경우의 차이는 **어느 쪽이 없어졌는가**다(앞 vs 뒤). 그건 순서 정보이고, 토큰 겹침
점수에는 남아 있지 않다. 그래서 여기서 부분문자열 위치로 다시 잰다.

참고: HyundaiHS orchestrator/core/reconcile.py 가 같은 함정을 지적한다 —
"token overlap lets a truncated name score 1.0 when it happens to be a substring".
접근만 참고했고 코드는 가져오지 않았다(도메인·자료구조가 다름).

순수 함수만 둔다 — 모델 호출·I/O 없음.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# 판독 관계 라벨. STAGE_3 프롬프트와 notes 가 이 값을 그대로 쓴다.
SAME = "same"                    # 표기 차이뿐 — 내용 동일
TAIL_TRUNCATED = "tail_cut"      # 뒤가 잘림 → 정본이 더 완전 (위험)
HEAD_DROPPED = "head_drop"       # 앞(항목명 등)을 뺌 → 값은 온전 (보통 무해)
EXPANDED = "expanded"            # VLM 이 더 많이 읽음 → OCR 누락 회수 (이득)
DIVERGED = "diverged"            # 서로 다른 내용 — 어느 쪽이 맞는지 여기서 못 정함

# 잘림/누락으로 부를 최소 비율. 이 미만이면 표기 흔들림으로 보고 SAME 취급.
# 0.15 = 정본의 15% 이상이 사라져야 '없어졌다'고 말한다 (구두점 한둘로 경보가 뜨면
# 라벨이 의미를 잃는다).
MIN_LOSS_RATIO = 0.15
# (NEAR_SAME_RATIO 0.92 는 2026-08-05 제거 — _relation_by_shape 마지막 블록 주석 참조.
#  norm() 이 구두점을 이미 지우므로 유사도 폴백이 덮던 7건은 전부 진짜 오독이었다.)

_KEEP = re.compile(r"[0-9A-Za-z가-힣]+")
# 원문자는 '지워야 할 장식'이 아니라 '숫자의 다른 표기'다. 안 바꾸면 VLM 이 더 정확히
# 읽은 게 손실로 잡힌다 — 실측(001 p1_r006): OCR '참여방법 1' → VLM '참여 방법 ①' 에서
# ① 이 정규화에 지워져 '1 이 사라졌다'로 읽혀 잘림 오탐이 났다. evaluate.py 도 같은
# 이유로 같은 치환을 한다.
_CIRCLED = {
    **{chr(0x2460 + i): str(i + 1) for i in range(20)},   # ①~⑳
    **{chr(0x2474 + i): str(i + 1) for i in range(20)},   # ⑴~⒇
    **{chr(0x24F5 + i): str(i + 1) for i in range(10)},   # ⓵~⓾
}
_CIRCLED_TABLE = str.maketrans(_CIRCLED)


def norm(text: str) -> str:
    """비교용 정규화 — 글자·숫자만 남긴다 (원문자는 숫자로 환산).

    괄호·낫표·중점·공백은 OCR 과 VLM 이 서로 다르게 쓰는 대표적인 자리라(실측:
    OCR '[NH대박7적금]' vs VLM '「NH대박7적금」') 관계 판정에서는 걷어낸다.

    **주의: 이 함수는 마침표도 지운다.** 그래서 '7.1%' 와 '71%' 가 같아진다.
    숫자가 같은지는 이 함수로 판단하면 안 된다 — numeric_signature 를 쓴다.
    """
    return "".join(_KEEP.findall((text or "").casefold().translate(_CIRCLED_TABLE)))


_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def numeric_signature(text: str) -> list[str]:
    """텍스트에 **적힌 그대로의** 숫자열만 순서대로 뽑는다 (천단위 콤마만 제거).

    **왜 따로 재나.** norm() 은 비교를 위해 마침표를 지우므로 '7.1%' 와 '71%' 가
    둘 다 '71' 이 된다. 금리 광고에서 이 둘은 10배 차이다. 실측(2026-08-05,
    5문서 177개 후보 전수) — 아래 둘이 relation=same 으로 찍혀 교차검증 경고가
    뜨지 않았다:
침
      002 p1_r014  정본 '최고연71%'
                   후보 '최고연 7.1%'                        → 금리 10배
      003 p2_r008  정본 '…고객행복센터(1661-3000,1522-3000)…'
                   후보 '…고객행복센터(1661-3000, 1522-3000)…' → OCR 이 '3000,1522' 를
                        한 덩어리로 붙여 읽어 '30001522' 가 됐다

    둘 다 **VLM 이 정확히 읽고 OCR 이 틀린** 경우다. 후보가 정답을 들고 있는데도
    "차이 없음"으로 덮여 하류가 볼 기회를 잃었다.

    **원문자(①②③)는 숫자로 환산하지 않는다.** norm() 은 ①→1 로 바꾸는데
    그쪽은 그게 맞다(항목번호가 사라진 것처럼 보이는 오탐을 막는다 — 위 _CIRCLED
    주석). 숫자 비교에서 같이 하면 반대 방향 오탐이 난다: VLM 이 원문자를 정확히
    읽어낸 것이 '숫자가 늘었다'로 잡힌다. 실측에서 환산할 때 11건이 걸렸는데
    그중 6건이 이 경우였다. \\d 만 보면 원문자는 자연히 빠진다 — 원문자는 값이
    아니라 순서 표시다.
    """
    return [m.replace(",", "") for m in _NUM.findall(text or "")]


def numbers_differ(ocr_text: str, vlm_text: str) -> bool:
    """적힌 숫자가 **실질적으로** 다른가 — 원문자 오독은 차이로 세지 않는다.

    한 자리 정수를 따로 취급한다. 이유는 실측이다(2026-08-05, 5문서 177개 후보):
    숫자를 전부 그대로 비교하면 무해한 원문자 회수가 걸린다 — VLM 이 '1' 을 '①' 로
    **더 정확히** 읽어낸 것이 '숫자가 늘었다'로 잡힌다. 이 저장소의 기존 테스트도
    같은 패턴('참여방법 1' vs '참여 방법 ①')을 무해한 것으로 이미 고정해 두고 있었다.

    그래서 규칙을 이렇게 둔다:

      - 두 자리 이상 또는 소수점이 있는 숫자는 **항상** 비교한다. 금리·금액·기간·
        연락처가 여기 들어온다. 실측 2건이 이 규칙으로 잡힌다(numeric_signature 주석).
      - 한 자리 정수가 **한쪽에만 있으면** 원문자 회수로 보고 무시한다.
      - 단 **양쪽에 한 자리가 다 있는데 다르면** 비교한다. '연 3%' 대비 '연 5%'
        같은 실제 값 차이를 놓치지 않기 위해서다.

    이 규칙으로 5문서에서 5건이 same 에서 빠졌고 **5건 모두 OCR 이 실제로 틀린**
    것이었다(오탐 0). 중대도는 갈린다 — 금리 10배·전화번호 붕괴 2건이 값 오류이고,
    나머지 3건은 원문자 오독이다:

        올원e p1_r017  OCR 이 '③' 을 별도 라인 '3' 으로, 상단첨자 '²' 를 '2' 로 읽음
                       (금리 '0.2%p' 자체는 양쪽 동일 — 값 오류가 아니다)
        003 p1_r008    OCR 이 '②' 를 '1' 로 읽음
        003 p1_r009    OCR 이 '③' 을 '3' 으로 읽음

    **알려진 한계.** 한 자리 값이 한쪽에서만 사라진 경우('연 3%' → '연 %')는 이
    게이트를 통과한다. 그건 모양 판정(잘림/불일치)이 잡을 자리다.
    """
    a, b = numeric_signature(ocr_text), numeric_signature(vlm_text)
    a_multi = [n for n in a if len(n) > 1]
    b_multi = [n for n in b if len(n) > 1]
    if a_multi != b_multi:
        return True
    a_one = [n for n in a if len(n) == 1]
    b_one = [n for n in b if len(n) == 1]
    return bool(a_one and b_one and a_one != b_one)


@dataclass(frozen=True)
class Relation:
    """OCR 정본 대비 VLM 후보의 관계."""

    kind: str
    lost_head: float = 0.0   # 정본 앞쪽에서 사라진 비율 (0~1)
    lost_tail: float = 0.0   # 정본 뒤쪽에서 사라진 비율 (0~1)
    gained: float = 0.0      # 후보가 더 들고 있는 비율 (0~1, EXPANDED 용)

    @property
    def is_truncated(self) -> bool:
        """정본이 더 완전하다 — 후보를 그대로 쓰면 내용이 없어진다."""
        return self.kind == TAIL_TRUNCATED

    @property
    def label(self) -> str:
        """사람/LLM 이 읽을 한글 설명. 프롬프트와 notes 가 같은 문구를 쓴다."""
        if self.kind == TAIL_TRUNCATED:
            return f"뒷부분 잘림({self.lost_tail:.0%} 소실) — 정본이 더 완전"
        if self.kind == HEAD_DROPPED:
            return "앞 항목명 생략 — 값 자체는 온전"
        if self.kind == EXPANDED:
            return "정본보다 많이 읽음 — 누락 회수 가능"
        if self.kind == DIVERGED:
            return "정본과 내용이 다름 — 원본 확인 필요"
        return "표기 차이"


def classify_reading(ocr_text: str, vlm_text: str) -> Relation:
    """정본(ocr_text) 대비 후보(vlm_text)의 관계를 판정한다.

    두 단계다 — 먼저 문자열 모양으로 관계를 정하고(_relation_by_shape), 그 답이
    SAME 이면 **적힌 숫자가 같은지 다시 본다**(numeric_signature).

    숫자를 따로 보는 이유는 numeric_signature 주석에 실측과 함께 적었다. 요지는
    모양 비교가 쓰는 norm() 이 마침표를 지워서 '7.1%' 와 '71%' 를 같다고 말한다는
    것이다. SAME 일 때만 검사한다 — 나머지 딱지는 이미 "다르다"를 말하고 있고,
    잘림(TAIL_TRUNCATED)·회수(EXPANDED)는 숫자가 달라지는 게 정상이다(뒤가
    잘리면 그 안의 숫자도 같이 없어진다).
    """
    rel = _relation_by_shape(ocr_text, vlm_text)
    if rel.kind == SAME and numbers_differ(ocr_text, vlm_text):
        return Relation(DIVERGED)
    return rel


def _relation_by_shape(ocr_text: str, vlm_text: str) -> Relation:
    """문자열 모양만으로 관계를 정한다 (숫자 게이트 전 단계).

    판정 순서 — 부분문자열 관계를 먼저 보고, 아니면 유사도로 떨어진다.
      1) 정규화 후 같다            → SAME
      2) 후보 ⊂ 정본               → 사라진 쪽이 앞이냐 뒤냐로 HEAD_DROPPED / TAIL_TRUNCATED
      3) 정본 ⊂ 후보               → EXPANDED
      4) 그 외                     → 유사도로 SAME(표기차) / DIVERGED
    """
    a, b = norm(ocr_text), norm(vlm_text)
    if not a or not b:
        # 한쪽이 비면 관계를 말할 수 없다. 후보만 있으면 회수(EXPANDED), 정본만 있으면 잘림.
        if b and not a:
            return Relation(EXPANDED, gained=1.0)
        if a and not b:
            return Relation(TAIL_TRUNCATED, lost_tail=1.0)
        return Relation(SAME)

    if a == b:
        return Relation(SAME)

    if b in a:  # 후보가 정본의 일부 — 무언가 사라졌다
        head = a.index(b)
        tail = len(a) - head - len(b)
        head_ratio, tail_ratio = head / len(a), tail / len(a)
        if max(head_ratio, tail_ratio) < MIN_LOSS_RATIO:
            return Relation(SAME, lost_head=head_ratio, lost_tail=tail_ratio)
        # 뒤가 더 많이 없어졌으면 잘림. 앞만 없어졌으면 항목명 생략.
        kind = TAIL_TRUNCATED if tail_ratio >= head_ratio else HEAD_DROPPED
        return Relation(kind, lost_head=head_ratio, lost_tail=tail_ratio)

    if a in b:  # 정본이 후보의 일부 — 후보가 더 읽었다
        gained = (len(b) - len(a)) / len(b)
        return Relation(SAME if gained < MIN_LOSS_RATIO else EXPANDED, gained=gained)

    # 부분문자열이 아니다 — 정규화를 거치고도 글자·숫자가 실제로 다르다.
    #
    # 예전에는 여기서 유사도 0.92 이상이면 SAME(표기차)으로 떨궜다. 2026-08-05 에
    # 걷어냈다. 근거 — norm() 이 공백·괄호·낫표·중점·마침표를 **이미 다 지운다.**
    # 그러고도 남은 차이는 구두점이 아니라 글자다. 즉 폴백이 막으려던 상황("구두점
    # 한둘로 경보가 뜨는 것")은 norm() 선에서 끝나 있었고, 폴백은 그 아래에서
    # 진짜 오독만 덮고 있었다. 실측(5문서 177개 후보 전수) — 이 폴백으로 SAME 이
    # 된 것이 7건이었고 **7건 모두 OCR 오독**이었다:
    #
    #   0.923 '777명에게 쓴다'   → '쏜다'          0.938 '첫결음'   → '첫걸음'
    #   0.971 'NH놓협은행'       → 'NH농협은행'     0.972 'iNH농협'  → '① NH농협'
    #   0.974 '모여봐요을올원모임' → '모여봐요 올원모임'
    #   0.994 '기본이자을'       → '기본이자율'
    #   0.944 '1모임주'          → '② 모임주'      (숫자 게이트가 이미 잡는 것)
    #
    # 마지막 줄이 이 방식의 한계를 보여준다 — 긴 문장의 한 글자 오독은 유사도가
    # 0.994 다. 임계를 올려서 잡을 수 있는 문제가 아니었다.
    match = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    tail_ratio = (len(a) - match.a - match.size) / len(a)
    # 정본 쪽에만 있는 꼬리가 크면(후보가 뒤를 못 읽음) 잘림으로 본다. 조건 셋:
    #
    #  1. 후보 내용의 대부분이 정본 안에 그대로 있다 (아예 다른 글이 아니다)
    #  2. **후보가 정본보다 눈에 띄게 짧다** — 잘림은 길이로 재야 한다
    #  3. 없어진 쪽이 앞이 아니다 (앞이면 head_drop 성격)
    #
    # 2번이 2026-08-05 에 추가됐다. 예전엔 '최장일치 뒤에 남은 정본 비율'만 봤는데,
    # 그러면 **길이가 같은 한 글자 오독이 잘림으로 둔갑한다** — 실측(002 p1_r006):
    # '…777명에게 쓴다' vs '…777명에게 쏜다' 는 정규화 길이가 13 대 13 으로 같은데
    # 앞 11자가 일치하고 정본 꼬리가 15.4% 라 옛 조건을 통과했다. 잘린 게 아니라
    # 뒤에 서로 다른 글자가 각각 남은 것이다.
    #
    # 처음엔 '후보가 최장일치에서 끝나야 한다'로 막으려 했는데 **진짜 잘림 2건을
    # 같이 죽였다** — 003 p2_r004(202자→88자)와 p2_r017(134자→109자)은 문장 중간의
    # 한 글자 오독('미용모'→'미모임', '기기 제외'→'기재') 때문에 최장일치가 일찍
    # 끊겨 후보 뒤에 글자가 남아 있었다. 잘림 여부는 일치 위치가 아니라 길이다.
    shortfall = (len(a) - len(b)) / len(a)
    head_ratio = match.a / len(a)
    if (match.size >= len(b) * 0.8
            and shortfall >= MIN_LOSS_RATIO
            and head_ratio < MIN_LOSS_RATIO):
        return Relation(TAIL_TRUNCATED, lost_tail=tail_ratio)
    return Relation(DIVERGED)
