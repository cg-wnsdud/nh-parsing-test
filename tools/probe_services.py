"""PaddleX / Gemma 서버 설정을 SSH 없이 HTTP 로만 조회한다.

서버에 로그인하지 않아도 여기까지는 보인다:
  - PaddleX  : 살아있는지, /layout-parsing 이 받는 파라미터 39종, 서버가 실제로
               적용한 기본값(model_settings·text_det_params) — 마지막 것은 아무
               이미지나 1장 추론해 응답 본문에서 읽는 수밖에 없다.
  - Gemma    : 게이트웨이(LiteLLM)에 등록된 모델명·백엔드 주소, 컨텍스트 길이.
               컨텍스트 길이는 노출 API 가 없어서 max_tokens 를 일부러 넘겨
               400 에러 메시지에서 뽑는다.

보이지 않는 것(= SSH 필요): 하위 모델 이름·가중치 버전(PP-OCRv5 server/mobile 등),
GPU·배치·정밀도, vLLM 기동 인자. README 의 "서버에서 확인" 절 참고.

실행:  uv run python tools/probe_services.py
"""
from __future__ import annotations

import base64
import io
import json
import sys

import requests
from PIL import Image, ImageDraw

sys.path.insert(0, "src")
from nh_parsing.config import SETTINGS  # noqa: E402

TIMEOUT = 30


def _get(url: str) -> tuple[int, object]:
    try:
        r = requests.get(url, timeout=TIMEOUT, verify=False)
    except Exception as exc:  # 연결 자체가 안 되면 VPN 미연결이 대부분이다
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:200]


def _base(url: str, suffix: str) -> str:
    """'.../paddlex/layout-parsing' → '.../paddlex' 처럼 마지막 경로를 떼어낸다."""
    return url.rstrip("/").rsplit(suffix, 1)[0].rstrip("/")


def probe_paddlex() -> None:
    root = _base(SETTINGS.paddlex_url, "/layout-parsing")
    print(f"\n=== PaddleX  {root} ===")
    code, body = _get(f"{root}/health")
    print(f"health           HTTP {code}  {body}")
    if code != 200:
        return

    code, spec = _get(f"{root}/openapi.json")
    if code == 200 and isinstance(spec, dict):
        params = spec["components"]["schemas"]["InferRequest"]["properties"]
        print(f"accepted params  {len(params)}종")
        # 우리가 실제로 보내는 것과 서버가 받는 것의 차이가 곧 '서버 기본값에 맡긴 것'
        sent = {
            "fileType", "useDocOrientationClassify", "useDocUnwarping",
            "textDetLimitSideLen", "textDetLimitType", "layoutMergeBboxesMode",
            "useFormulaRecognition", "useTextlineOrientation", "file",
        }
        default = [k for k in params if k not in sent]
        print(f"우리가 안 보내는 것({len(default)}종, 서버 기본값 적용):")
        print("  " + ", ".join(default))

    # 서버가 실제로 적용한 값은 추론 응답에만 실려 온다. 흰 바탕에 글자 한 줄이면 충분.
    img = Image.new("RGB", (640, 120), "white")
    ImageDraw.Draw(img).text((20, 45), "probe 12345", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    payload = {
        "file": base64.b64encode(buf.getvalue()).decode("ascii"),
        "fileType": 1,
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
    }
    try:
        r = requests.post(SETTINGS.paddlex_url, json=payload, timeout=SETTINGS.paddlex_timeout_s, verify=False)
        pruned = (r.json().get("result") or {}).get("layoutParsingResults", [{}])[0].get("prunedResult") or {}
    except Exception as exc:
        print(f"추론 프로브 실패: {exc}")
        return
    print("서버가 적용한 파이프라인 스위치 (model_settings):")
    print("  " + json.dumps(pruned.get("model_settings"), ensure_ascii=False))
    print("서버가 적용한 검출 파라미터 (text_det_params):")
    print("  " + json.dumps((pruned.get("overall_ocr_res") or {}).get("text_det_params"), ensure_ascii=False))


def probe_gemma() -> None:
    root = _base(SETTINGS.gemma_url, "/chat/completions")  # '.../llm/v1'
    print(f"\n=== Gemma  {root} ===")
    code, body = _get(f"{root}/models")
    print(f"models           HTTP {code}")
    if isinstance(body, dict):
        for m in body.get("data", []):
            mark = "  ← .env 의 GEMMA_MODEL" if m.get("id") == SETTINGS.gemma_model else ""
            print(f"  - {m.get('id')}{mark}")

    # LiteLLM 프록시면 백엔드 주소까지 나온다 (vLLM 실주소·태그)
    code, info = _get(f"{root.rsplit('/v1', 1)[0]}/model/info")
    if code == 200 and isinstance(info, dict):
        print("게이트웨이 라우팅 (LiteLLM /model/info):")
        for entry in info.get("data", []):
            lp = entry.get("litellm_params", {})
            print(f"  {entry.get('model_name'):32} → {lp.get('api_base')}  ({lp.get('model')})")

    # 컨텍스트 길이 전용 API 가 없다. 일부러 넘겨서 400 메시지로 읽는다.
    try:
        r = requests.post(
            SETTINGS.gemma_url,
            json={"model": SETTINGS.gemma_model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 99_999_999},
            timeout=TIMEOUT, verify=False,
        )
        msg = r.json().get("error", {}).get("message", "")
        print(f"컨텍스트 길이     {msg.split('Please request')[0].strip() or msg[:200]}")
    except Exception as exc:
        print(f"컨텍스트 길이 프로브 실패: {exc}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 cp949 깨짐 방지
    requests.packages.urllib3.disable_warnings()  # 사내 인증서
    probe_paddlex()
    probe_gemma()
