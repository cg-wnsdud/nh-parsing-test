"""Gemma(OpenAI 호환 VLM) 클라이언트 — 설계서 6.2/6.5절.

- chat_json(): 모든 VLM 태스크가 공유하는 strict json_schema 호출 헬퍼 (재시도 포함)
- classify(): 문서 분류 — 파일명 prior + VLM 관측 결합, category_source 로 합의/충돌 기록
  (이전 프로젝트 검증 패턴)
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
from dataclasses import dataclass

import requests
from PIL import Image

from . import vlm_cache
from .config import SETTINGS

# 단계별 VLM 호출 수·시간 집계 (schema_name 기준). 어느 단계가 비용을 쓰는지 재려면
# 호출 지점마다 세는 수밖에 없었는데, 그때그때 로그를 눈으로 세다 보니 매번 값이 달랐다.
# 운영에 영향 없는 순수 카운터라 항상 켜 둔다.
STATS: dict[str, dict[str, float]] = {}


def reset_stats() -> None:
    STATS.clear()


def _record(schema_name: str, seconds: float, cached: bool) -> None:
    s = STATS.setdefault(schema_name, {"calls": 0, "cached": 0, "seconds": 0.0})
    s["calls"] += 1
    s["seconds"] += seconds
    if cached:
        s["cached"] += 1


def stats_table() -> str:
    """단계별 호출 수·누적 시간 표 (호출 많은 순)."""
    if not STATS:
        return "(VLM 호출 없음)"
    rows = sorted(STATS.items(), key=lambda kv: -kv[1]["calls"])
    out = [f"{'단계(schema_name)':34s} {'호출':>5s} {'캐시':>5s} {'초':>8s}"]
    for name, s in rows:
        out.append(f"{name:34s} {int(s['calls']):5d} {int(s['cached']):5d} {s['seconds']:8.1f}")
    tot_c = sum(s["calls"] for _, s in rows)
    tot_s = sum(s["seconds"] for _, s in rows)
    out.append(f"{'합계':34s} {int(tot_c):5d} {'':5s} {tot_s:8.1f}")
    return "\n".join(out)


def chat_json(
    content_parts: list[dict],
    schema_name: str,
    schema: dict,
    max_tokens: int = 1500,
    retries: int = 2,
) -> dict:
    """VLM 에 멀티모달 메시지를 보내고 strict json_schema 로 강제된 JSON 을 받는다.

    strict json_schema 만 사용하는 이유: LiteLLM→vLLM 경로에서 실제로 강제되는
    유일한 구조화 출력 모드 (이전 프로젝트 실전 검증). 파싱은 방어적으로 수행.
    """
    # 개발용 결정론 캐시(기본 off). 같은 호출이면 저장된 응답을 재생해 A/B 에서
    # '내가 바꾼 단계'만 달라지게 한다 — vlm_cache 모듈 설명 참조.
    import time as _time
    _t0 = _time.time()
    cache_key = None
    if vlm_cache.enabled():
        cache_key = vlm_cache.key_for(
            content_parts, schema_name, schema, max_tokens, SETTINGS.gemma_model
        )
        cached = vlm_cache.load(cache_key)
        if cached is not None:
            vlm_cache.note("hit")
            _record(schema_name, _time.time() - _t0, cached=True)
            return cached
        vlm_cache.note("miss")
        if vlm_cache.replay_only():
            raise RuntimeError(
                f"VLM 캐시 미스(replay 모드): {schema_name} — 먼저 VLM_CACHE=r 로 기록하세요"
            )

    payload = {
        "model": SETTINGS.gemma_model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content_parts}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                SETTINGS.gemma_url, json=payload, timeout=SETTINGS.gemma_timeout_s
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            text = _strip_fences(content)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                repaired = _repair_trailing_escape(text)
                if repaired is None:
                    raise
                parsed = repaired
            if cache_key is not None:
                vlm_cache.store(cache_key, schema_name, parsed)
                vlm_cache.note("stored")
            _record(schema_name, _time.time() - _t0, cached=False)
            return parsed
        except Exception as exc:  # 연결 오류·JSON 파싱 실패 모두 재시도
            last_exc = exc
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    _record(schema_name, _time.time() - _t0, cached=False)  # 실패도 시간·호출은 썼다
    raise RuntimeError(f"VLM 호출 실패({retries + 1}회): {last_exc}")


def _repair_trailing_escape(text: str) -> dict | None:
    """guided-decoding 서빙 결함 보정 — 문자열을 닫기 직전 불필요한 역슬래시를
    내보내고 그대로 생성을 멈추는 경우가 실측됨(gemma-4-26b-NVFP4-MTP,
    finish_reason=stop, temperature=0 에서도 호출마다 미묘하게 다른 위치/길이로
    재현 — 끝에서 고정 길이를 자르는 방식은 통하지 않아 텍스트 전체에서
    마지막 역슬래시 하나만 제거해본다. 그래도 파싱되지 않으면 None(원래
    예외 유지) — 여기서 만든 값은 호출측의 형식 가드로 다시 검증된다."""
    idx = text.rfind("\\")
    if idx == -1:
        return None
    candidate = text[:idx] + text[idx + 1:]
    try:
        return json.loads(candidate)
    except Exception:
        return None


def image_part(image: Image.Image, box: tuple[int, int] = (896, 2400), quality: int = 85) -> dict:
    """캔버스를 VLM 입력용 축소 이미지 파트로 변환 (비율 유지)."""
    img = image.copy()
    img.thumbnail(box)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": url}}


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return match.group(1) if match else text


# ──────────────────────────── 문서 분류 ────────────────────────────

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "product_group": {"type": "string", "enum": ["예금성", "대출성", "기타", "판단불가"]},
        "ad_type": {
            "type": "string",
            "enum": ["상세페이지", "안내장", "배너", "이벤트페이지", "기타"],
        },
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["product_group", "ad_type", "confidence", "reason"],
    "additionalProperties": False,
}

_PROMPT = """당신은 금융상품 광고물 분류기입니다. 이미지를 보고 다음을 판정하세요.

1. product_group: 광고하는 상품의 성격 — 예금성(예금/적금/입출금), 대출성, 기타, 판단불가
2. ad_type: 광고물 형태 — 상세페이지(세로 스크롤 웹/모바일), 안내장(인쇄물 형태), 배너, 이벤트페이지, 기타

파일명 힌트: "{filename}"
파일명 힌트는 강한 사전확률입니다. 이미지에서 명확히 반대되는 증거가 보일 때만 뒤집으세요.
reason 은 한 문장으로."""


def filename_prior(filename: str) -> str | None:
    for group, keywords in SETTINGS.product_group_keywords.items():
        if any(kw in filename for kw in keywords):
            return group
    return None


@dataclass
class Classification:
    product_group: str | None
    ad_type: str | None
    confidence: float | None
    category_source: str
    reason: str = ""


def classify(canvas: Image.Image, filename: str) -> Classification:
    """파일명 prior 와 VLM 관측을 결합한다. **어느 경로로 정해졌는지를 값으로 남긴다.**

    2026-08-06: `category_source="filename"` 이 서로 다른 세 상황을 뭉치고 있었다.
    셋은 신뢰도가 완전히 다른데 값이 같아서 산출물만 보고 가를 수가 없었다:

      1. VLM 호출이 실패했다 (서버 죽음·타임아웃) → `filename_vlm_failed`
         파일명 말고는 근거가 없다. 재실행하면 달라질 수 있다.
      2. VLM 이 봤는데 "기타/판단불가"라고 답했다 → `filename_vlm_abstained`
         **VLM 이 반대 의견을 낸 것**이지 정보가 없는 게 아니다. 실측(003): VLM 이
         확신도 0.9 로 "특정 금융상품 가입 유도가 아니라 '올원모임' 서비스 이벤트"라고
         답했는데 파일명의 '예금성' 이 그 판단을 덮었다. 스키마 선택이 걸린 자리라
         (예금성 팩으로 심의) 검수자가 이 케이스를 알아야 한다.
      3. VLM 을 애초에 안 불렀다 (HWP — 캔버스가 없다) → `filename_no_vlm`
         hwp_ingest.py 가 붙인다. 여기 코드가 도는 경로가 아니다.

    §9-③ 의 '규칙폴백 2건 중 1건은 안 부른 것' 과 같은 종류의 혼동이다 —
    "폴백했다"와 "물어보지도 않았다"를 한 값에 담으면 실측을 세는 순간 틀린다.
    """
    prior = filename_prior(filename)
    try:
        data = chat_json(
            [
                {"type": "text", "text": _PROMPT.format(filename=filename)},
                image_part(canvas),
            ],
            schema_name="ad_classification",
            schema=_CLASSIFY_SCHEMA,
            max_tokens=300,
        )
    except Exception as exc:
        # VLM 실패 시 파일명 prior 로 안전 기본값 (이전 프로젝트 default_classification 패턴)
        return Classification(
            product_group=prior,
            ad_type=None,
            confidence=None,
            category_source="filename_vlm_failed" if prior else "none",
            reason=f"VLM 분류 실패, 파일명 prior 사용: {exc}",
        )

    raw_group = data.get("product_group")          # 기타/판단불가 를 지우기 전 원값
    vlm_group = None if raw_group in ("기타", "판단불가") else raw_group
    abstained = raw_group in ("기타", "판단불가")
    reason = str(data.get("reason", ""))

    if prior and vlm_group:
        source = "filename_and_vlm" if prior == vlm_group else "vlm_overrode_filename"
        group = vlm_group
    elif vlm_group:
        source, group = "vlm", vlm_group
    elif prior and abstained:
        source, group = "filename_vlm_abstained", prior
        # VLM 이 무엇을 근거로 반대했는지를 노트에 그대로 남긴다. 이 문장이 없으면
        # 산출물에서 "VLM 도 동의했다"와 구분이 안 된다 (003 이 그렇게 보였다).
        reason = (
            f"VLM 은 '{raw_group}' 로 판단(확신도 {data.get('confidence')})했고 "
            f"파일명 prior '{prior}' 를 적용했다. VLM 사유: {reason}"
        )
    elif prior:
        source, group = "filename_vlm_failed", prior
    elif abstained:
        source, group = "vlm_abstained", None
    else:
        source, group = "none", None
    return Classification(
        product_group=group,
        ad_type=data.get("ad_type"),
        confidence=data.get("confidence"),
        category_source=source,
        reason=reason,
    )
