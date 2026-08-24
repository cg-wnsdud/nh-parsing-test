# -*- coding: utf-8 -*-
"""기동·실패경로 검사 — **서버를 띄우지 않고** 규격의 두 구멍을 본다.

왜 따로 있나. `roundtrip.py` 는 행복 경로(정상 파싱 → zip)만 본다. 그래서
2026-08-24 까지 규격 위반 2건이 잡히지 않았다:

  ① `gunicorn main:app` 로 뜨는가        — 로컬은 `uvicorn app.main:app` 이라 통과했다
  ② 파싱 실패가 200+ERROR 인가          — 정상 파일만 넣어 보니 이 분기를 안 지났다

둘 다 "실행해 보기 전엔 안 드러나는" 종류라, 실제로 그 모양으로 import 하고
그 분기를 태워서 확인한다. 외부 의존(파싱 본체·VLM·네트워크)은 타지 않는다.

    python tests/boot_check.py
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent / "app_custom_parser"


def check_no_relative_imports() -> list[str]:
    """상대 import 가 **한 줄도** 없는지 — 함수 안에 숨은 것까지.

    `import main` 만으로는 부족하다. 2026-08-24 실측: 최상위 import 를 전부 고친 뒤에도
    `_accept()` 안에 `from .service.status import write_parse_status` 가 남아 있었고,
    그건 **POST 가 실제로 들어왔을 때만** 실행돼서 기동 검사를 통과해 버렸다. 왕복
    시험에서 `500 ImportError` 로 처음 드러났다. 그래서 소스를 훑어 정적으로 잡는다.
    """
    bad: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
                dots = "." * node.level
                bad.append(
                    f"{py.relative_to(APP_DIR)}:{node.lineno} "
                    f"`from {dots}{node.module or ''} import …` — 상대 import 는 "
                    f"`gunicorn main:app` 에서 깨진다"
                )
    return bad


def check_gunicorn_import() -> list[str]:
    """농협 배포와 **같은 방식**으로 import 되는지.

    `run-application.sh`: `cd $FLOW_APP_NAME && gunicorn main:app`
    → cwd=app_custom_parser, 모듈 이름은 `main` (최상위). 이 상태에서 상대 import
    (`from .service import …`)를 쓰면 ImportError 로 뜨지도 못한다.
    """
    code = (
        "import main; "
        "paths=sorted(r.path for r in main.app.routes if hasattr(r,'path')); "
        "print(__import__('json').dumps(paths))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=APP_DIR, capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return [f"`cd app_custom_parser && gunicorn main:app` 형태로 import 실패:\n"
                + "\n".join(f"          {t}" for t in tail)]

    paths = json.loads(proc.stdout.strip().splitlines()[-1])
    missing = [p for p in ("/parsing", "/parsing/result/{uuid}", "/health") if p not in paths]
    return [f"경로 없음: {m}" for m in missing]


def check_error_is_200() -> list[str]:
    """파싱 실패 → **200 + {"status":"ERROR"}** 인가.

    규격 `parser_howto.txt` §1-2:
      "파싱 오류가 발생하면 status 200, {"status":"ERROR", "message":...}를 반환합니다.
       그 외 오류가 발생하면 status 500, {"message":...}를 반환합니다."

    농협 **예제 코드**(`app_custom_parser/main.py`)는 이 경우에도 500 을 준다 —
    규격서와 예제가 서로 다르다. 규격서 본문을 따르고, 이 차이는 확인 항목으로 올렸다.
    """
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, PATH_TEMP=tmp, PYTHONUTF8="1")
        # 라우트 함수를 직접 부른다 — `TestClient` 가 httpx 를 요구하는데 우리 시험
        # 환경(`requirements.txt` 로 만든 venv)에 없어서다. (농협 베이스 이미지에는
        # httpx 0.28.1 이 있으므로 거기서는 TestClient 도 쓸 수 있다. 다만 시험이
        # 환경 하나에서만 도는 것보다 아무 데서나 도는 편이 낫다.)
        # 파싱은 태우지 않고 상태 파일만 만들어 결과 조회 분기가 무엇을 돌려주는지 본다.
        code = r'''
import asyncio, json, os
from pathlib import Path
import main
from service.status import write_parse_status, ERROR, PARSING

work = Path(os.environ["PATH_TEMP"])
out = {}

def call(uid):
    resp = asyncio.run(main.get_parsing_result(uid))
    return {"code": resp.status_code, "body": json.loads(bytes(resp.body).decode("utf-8"))}

for uid, status, msg in (("err1", ERROR, "일부러 낸 실패"), ("busy1", PARSING, "")):
    d = work / uid
    d.mkdir(parents=True, exist_ok=True)
    write_parse_status(str(d), status, msg)
    out[uid] = call(uid)

out["missing"] = call("nonexistent-uuid")
print("@@" + json.dumps(out, ensure_ascii=False))
'''
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=APP_DIR, capture_output=True, text=True, encoding="utf-8", env=env,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-4:]
            return ["실패 경로 검사를 실행하지 못했다:\n"
                    + "\n".join(f"          {t}" for t in tail)]

        line = next(l for l in proc.stdout.splitlines() if l.startswith("@@"))
        got = json.loads(line[2:])

    err = got["err1"]
    if err["code"] != 200:
        errors.append(f'파싱 실패 응답이 {err["code"]} — 규격은 200 (parser_howto §1-2)')
    if err["body"].get("status") != "ERROR":
        errors.append(f'파싱 실패 body 에 `"status":"ERROR"` 가 없다: {err["body"]}')
    if "message" not in err["body"]:
        errors.append(f'파싱 실패 body 에 `message` 가 없다: {err["body"]}')

    busy = got["busy1"]
    if busy["code"] != 200 or busy["body"].get("status") != "PARSING":
        errors.append(f'진행중 응답이 규격과 다르다: {busy}')

    # "그 외 오류"(uuid 를 못 찾음)는 500 이 맞다 — 위 ERROR 와 갈라져야 한다.
    missing = got["missing"]
    if missing["code"] != 500:
        errors.append(f'없는 uuid 응답이 {missing["code"]} — 규격은 500 ("그 외 오류")')
    return errors


def main() -> int:
    checks = [
        ("상대 import 없음 (함수 안에 숨은 것까지)", check_no_relative_imports),
        ("농협 배포 형태로 기동 (cd app_custom_parser && gunicorn main:app)", check_gunicorn_import),
        ("파싱 실패 = 200 + status:ERROR / 그 외 오류 = 500", check_error_is_200),
    ]
    bad = 0
    for title, fn in checks:
        errs = fn()
        print(f"[{'OK ' if not errs else 'NG '}] {title}")
        for e in errs:
            print(f"        ✗ {e}")
        bad += bool(errs)
    print(f"\n검사 {len(checks)}건 · 실패 {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
