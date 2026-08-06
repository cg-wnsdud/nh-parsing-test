"""값 검증 신호 — 값이 근거 텍스트에 실재하는지 재는 연속 신뢰 지표.

원본 패턴 (paddle-gemma-orchestrator): core/extraction.py::check_field_consistency —
값 토큰 중 페이지 텍스트에서 발견된 비율(match)을 연속 신뢰 신호로 쓴다.
BACKED_TOKEN_RATIO(0.8) 이상이면 evidence_backed=True (STAGE_3, extract.py 가 사용).

이 파일은 예전엔 이전 프로젝트의 '관측 → 병합 → judge' 3단계 추출(파싱 단계 ⑥-4)을
전면 이식했었다. ⑥-4 는 2026-07-28 제거됐다 — 필드는 STAGE_3(스키마 기반) 하나로
일원화했고, 그 3단계(dedupe_fields/find_conflicts/judge_conflicts/reconcile_fields/
merge_observations)는 전부 그 제거된 경로에서만 불리고 있어 같이 걷어냈다
(2026-07-29, 죽은 코드 감사 후). 아래 두 개만 지금도 쓰인다.
"""

from __future__ import annotations

import re

BACKED_TOKEN_RATIO = 0.8   # 이전 프로젝트 검증값 — extract.py 의 evidence_backed 판정 기준


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣.%]", "", str(s).casefold())


def check_field_consistency(value: str, page_text: str) -> float:
    """값 토큰 중 페이지 텍스트에서 발견된 비율 (0.0~1.0) — 연속 신뢰 신호."""
    norm_page = _norm(page_text)
    tokens = [t for t in (_norm(tok) for tok in re.split(r"[\s,;/·()~]+", value)) if t]
    if not tokens:
        return 1.0 if _norm(value) and _norm(value) in norm_page else 0.0
    return sum(1 for t in tokens if t in norm_page) / len(tokens)
