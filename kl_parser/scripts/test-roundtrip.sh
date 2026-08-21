#!/bin/bash
# 왕복 시험 — 서버가 떠 있는 상태에서 실행한다 (run-local.sh 를 먼저 띄울 것).
#   ./scripts/test-roundtrip.sh                      # samples/in 의 첫 파일
#   ./scripts/test-roundtrip.sh path/to/문서.pdf     # 지정 파일
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv-entry/Scripts/python.exe}"
[ -x "$PY" ] || PY=".venv-entry/bin/python"
export PYTHONUTF8=1
exec "$PY" tests/roundtrip.py "$@"
