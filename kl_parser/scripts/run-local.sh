#!/bin/bash
# 진입점 서버를 로컬에서 띄운다.
#
#   AGENT_URL 없음  → in-process 모드. nh_parsing 을 같은 프로세스에서 부른다(로컬 e2e).
#   AGENT_URL 있음  → remote 모드. 에이전트 서비스에 HTTP 로 넘긴다(배포 형태).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-9101}"
PY="${PY:-.venv-entry/Scripts/python.exe}"     # Linux: .venv-entry/bin/python
[ -x "$PY" ] || PY=".venv-entry/bin/python"

export PYTHONUTF8=1
export PATH_TEMP="${PATH_TEMP:-./temp}"

echo "진입점 기동: http://127.0.0.1:${PORT}  (모드: ${AGENT_URL:+remote}${AGENT_URL:-in-process})"
exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
