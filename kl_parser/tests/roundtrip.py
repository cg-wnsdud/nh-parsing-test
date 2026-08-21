# -*- coding: utf-8 -*-
"""왕복 시험 — 서버를 띄우고 POST → 폴링 → zip 수신 → 규격 검사까지 한 번에.

농협 예제 `test-application.sh` 가 하는 일과 같지만 3가지를 더 본다:
  ① 응답 코드가 정확히 **202** 인가 (규격이 못박은 값)
  ② body 에 `uuid`·`timeout` 이 있는가
  ③ 받은 zip 안의 파일 구성이 규격대로인가 + 내용이 규격을 지키는가(spec_check)

`curl`·`jq` 없이 파이썬만으로 돌아간다 — 폐쇄망 검증 환경을 가정한다.
"""

import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9101
BASE = f"http://127.0.0.1:{PORT}"


def _post_file(path: Path) -> tuple[int, dict]:
    """멀티파트 POST 를 표준 라이브러리로 직접 만든다 (requests 없이)."""
    boundary = "----kl-parser-roundtrip"
    body = io.BytesIO()

    def part(headers: str, payload: bytes):
        body.write(f"--{boundary}\r\n{headers}\r\n\r\n".encode())
        body.write(payload)
        body.write(b"\r\n")

    part(
        f'Content-Disposition: form-data; name="src_file"; filename="{path.name}"\r\n'
        "Content-Type: application/octet-stream",
        path.read_bytes(),
    )
    # 규격: option 은 base64(JSON). 빈 객체 `{}` = "e30=" (농협 예제와 같은 값)
    part('Content-Disposition: form-data; name="option"', b"e30=")
    body.write(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{BASE}/parsing",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else next(iter(sorted((ROOT / "samples/in").iterdir())))
    print(f"입력: {src.name}  ({src.stat().st_size:,} bytes)")

    # ① 파싱 요청
    code, payload = _post_file(src)
    print(f"POST /parsing → {code}  {json.dumps(payload, ensure_ascii=False)[:110]}")
    if code != 202:
        print(f"  ✗ 규격 위반: 파싱 요청 응답은 반드시 202 여야 한다 (받은 값 {code})")
        return 1
    for key in ("uuid", "timeout"):
        if key not in (payload.get("body") or {}):
            print(f"  ✗ 규격 위반: body 에 `{key}` 가 없다")
            return 1
    uid = payload["body"]["uuid"]

    # ② 폴링
    waited = 0.0
    while True:
        try:
            with urllib.request.urlopen(f"{BASE}/parsing/result/{uid}", timeout=120) as r:
                ctype = r.headers.get("Content-Type", "")
                raw = r.read()
                if "zip" in ctype:
                    print(f"GET  /parsing/result → 200 zip {len(raw):,} bytes ({waited:.0f}s 대기)")
                    break
                state = json.loads(raw)
                if state.get("status") != "PARSING":
                    print(f"  ✗ 예상 못한 응답: {state}")
                    return 1
        except urllib.error.HTTPError as e:
            print(f"  ✗ 파싱 실패 → {e.code} {e.read()[:200].decode('utf-8', 'replace')}")
            return 1
        if waited > 600:
            print("  ✗ 10분 초과")
            return 1
        time.sleep(2)
        waited += 2

    # ③ zip 구성 검사
    out = ROOT / "samples/out"
    out.mkdir(parents=True, exist_ok=True)
    for p in out.glob("*"):
        p.unlink()
    names = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            names.append(info.filename)
            (out / info.filename).write_bytes(zf.read(info))
    print(f"zip 내용 {len(names)}개: {names}")

    problems = []
    if not any(n.endswith("_hrc.jsonl") for n in names):
        problems.append("`_hrc.jsonl` 이 없다 (규격 필수)")
    if not any(n.endswith("_hrc.json") for n in names):
        problems.append("`_hrc.json` 이 없다 (규격 필수)")
    if any("/" in n or "\\" in n for n in names):
        problems.append("zip 안에 경로가 있다 — 규격은 '경로 없이 파일만'")
    if any(n == "kl_parser_notes.json" for n in names):
        problems.append("우리 검수용 파일이 zip 에 섞였다")
    for p in problems:
        print(f"  ✗ {p}")

    # ④ 내용 규격 검사
    print()
    rc = subprocess.call([sys.executable, str(HERE / "spec_check.py"), str(out)])
    return 1 if (problems or rc) else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
