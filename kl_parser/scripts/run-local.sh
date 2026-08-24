#!/bin/bash
# 진입점 서버를 로컬에서 띄운다.
#
#   AGENT_URL 없음  → in-process 모드. nh_parsing 을 같은 프로세스에서 부른다(로컬 e2e).
#   AGENT_URL 있음  → remote 모드. 에이전트 서비스에 HTTP 로 넘긴다(배포 형태).
#
# cwd 를 `app_custom_parser/` 로 옮기고 그 안에서 `main:app` 을 부른다 — 농협 배포
# (`cd $FLOW_APP_NAME && gunicorn main:app`)와 똑같은 import 경로로 띄워야, 로컬에서만
# 통과하고 배포에서 깨지는 결함을 로컬 시험이 못 잡는 일이 없다(2026-08-24 이전 결함).
set -euo pipefail
cd "$(dirname "$0")/../app_custom_parser"

PORT="${PORT:-9101}"
PY="${PY:-../.venv-entry/Scripts/python.exe}"     # Linux: ../.venv-entry/bin/python
[ -x "$PY" ] || PY="../.venv-entry/bin/python"

export PYTHONUTF8=1
export PATH_TEMP="${PATH_TEMP:-../temp}"

echo "진입점 기동: http://127.0.0.1:${PORT}  (모드: ${AGENT_URL:+remote}${AGENT_URL:-in-process})"
exec "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
