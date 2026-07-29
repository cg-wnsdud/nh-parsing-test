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
# DIVERGED 로 부를 최대 유사도. 이 이상 닮았으면 표기 차이로 본다.
NEAR_SAME_RATIO = 0.92

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
    """
    return "".join(_KEEP.findall((text or "").casefold().translate(_CIRCLED_TABLE)))


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

    # 부분문자열이 아니다 — 중간이 다르거나 양쪽이 조금씩 다르다.
    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio >= NEAR_SAME_RATIO:
        return Relation(SAME)
    # 정본 쪽에만 있는 꼬리가 크면(후보가 뒤를 못 읽음) 잘림으로 본다.
    match = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    tail_ratio = (len(a) - match.a - match.size) / len(a)
    if match.size >= len(b) * 0.8 and tail_ratio >= MIN_LOSS_RATIO:
        return Relation(TAIL_TRUNCATED, lost_tail=tail_ratio)
    return Relation(DIVERGED)
