# -*- coding: utf-8 -*-
"""농협 GenAI Knowledge Lake Custom 파서 진입점 (비동기 방식).

계약은 농협 규격서(`readme.md`)와 예제(`app_custom_parser/main.py`)를 그대로 따른다.
바꾼 곳은 파싱 본체(`service.parsing_service.parse`)뿐이다.

**폴더가 평평한 이유.** 농협 배포 스크립트(`run-application.sh`)는
`export FLOW_APP_NAME="app_custom_parser"` 뒤 `cd $FLOW_APP_NAME && gunicorn main:app`
로 뜬다 — gunicorn 이 `main` 을 **최상위 모듈**로 import 한다(`app_custom_parser.main` 이
아니다). 그 상태에서 `from .service import ...` 같은 상대 import 를 쓰면
`attempted relative import with no known parent package` 로 뜨지도 못한다.
그래서 농협 예제도 절대 import(`from service.parsing_service import ...`)를 쓰고
`service/` 에 `__init__.py` 를 두지 않는다(네임스페이스 패키지) — 이 파일도 그대로 따른다.
2026-08-24 이전에는 `app/` 로 한 겹 더 감싸고 상대 import 를 썼는데, 로컬 왕복 시험
(`uvicorn app.main:app`, cwd=kl_parser/)에서는 통과하고 **농협 배포 형태에서만 깨지는**
결함이었다 — 실행해 보기 전까진 안 드러난다.

  POST /parsing                  → **202** + {result, body:{uuid, timeout}}
  GET  /parsing/result/{uuid}    → 200 + zip
                                  | 200 + {"status":"PARSING"}
                                  | 200 + {"status":"ERROR", message}   (파싱 오류)
                                  | 500 + {message}                    (그 외 오류 — uuid 못 찾음 등)
  GET  /health                   → 200 + {"status":"OK"}

**예제와 다르게 한 것 2가지와 그 이유**

① 예제는 `subprocess` + `python -c "from service.parsing_service import parse; parse(...)"`
   로 파싱을 띄운다. 인자를 `repr()` 로 문자열에 박아 명령줄을 만드는 방식인데, 경로에
   따옴표·역슬래시가 있으면 깨진다. 우리 입력 파일명은 한글·공백·괄호가 흔하다
   (`(2023년)예금성상품 광고시 준수사항_은행연합회.hwp`). 그래서 `BackgroundTasks` 로
   같은 프로세스에서 부르고, 타임아웃은 파싱 쪽에서 관리한다.
   → 규격이 요구하는 것은 **응답을 즉시 202 로 돌려주는 것**이고 그건 지켜진다.

② 작업 디렉터리 삭제 시점. 예제는 결과 zip 을 만든 뒤 지운다. 같게 두되, 실패
   (`ERROR`)일 때는 **지우지 않는다** — 지우면 무엇이 왜 실패했는지 남지 않는다.
   `KEEP_WORK_DIR=1` 이면 성공해도 남긴다(개발용).
"""

import io
import os
import shutil
import traceback
import uuid
import zipfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from service.parsing_service import parse, parse_ad
from service.status import DONE, ERROR, PARSING, get_parse_status, write_parse_status

# 202 응답에 실어 보내는 값 — **KL 이 결과를 기다려 주는 시간**이다. 짧게 신고하면
# 우리가 아직 파싱 중인데 KL 이 먼저 포기한다.
#
# 실측(2026-08-24, 캐시 없는 콜드 실행)이 예전 기본값 600초를 이미 넘겼다:
#     13. 대출성상품.pdf   627초   ← 600 이었으면 시연 중에 잘렸다
#     2. 예금성상품.pdf    153초
#     올원e적금.png        198초
#     규정문서 HWP/PDF     4~44초
# 대부분이 VLM 대기다(대출성 312초 중 309초). 그래서 3배 여유를 두고 1800 으로 올린다.
# 규격서(`parser_howto.txt:14`)상 이 값을 **생략하면 기본 3시간**이므로 1800 은 그보다
# 보수적인 값이다. 파싱을 빠르게 만들면 그때 낮추면 된다.
TIMEOUT = int(os.getenv("TIMEOUT", "1800"))
PATH_WORK = os.getenv("PATH_TEMP", "./temp")
KEEP_WORK_DIR = os.getenv("KEEP_WORK_DIR", "") not in ("", "0", "false", "False")

# zip 에 넣을 결과 파일. 규격서 "Output jsonl" 단락이 정한 3종 + doc_data(선택).
# `kl_parser_notes.json` 은 우리 검수용이라 **일부러 제외**한다.
_RESULT_SUFFIXES = (
    "_hrc.jsonl", "_hrc.json", "doc_data.json",   # 규정문서 트랙 (KL 규격)
    "_parsed.json", "ad_summary.json",            # 광고물 트랙 (우리 규격)
)

app = FastAPI(title="NH KL Custom Parser (CGInside)", version="0.1.0")


@app.post("/parsing", name="Request parsing", description="규정문서 파싱을 요청한다(KL 규격).")
async def request_parsing(
    src_file: UploadFile,
    background_tasks: BackgroundTasks,
    option: str = Form(default=None),
):
    """KL 이 호출하는 자리. 산출물은 `_hrc.jsonl`/`_hrc.json`/`_img.zip`."""
    return await _accept(src_file, background_tasks, option, parse)


@app.post("/ad/parsing", name="Request ad parsing", description="광고물 파싱을 요청한다.")
async def request_ad_parsing(
    src_file: UploadFile,
    background_tasks: BackgroundTasks,
    option: str = Form(default=None),
):
    """광고 심의용. **통신 방식(202·uuid·폴링·zip)은 위와 완전히 같고 산출물만 다르다.**

    KL 이 부르는 자리가 아니다 — 광고물은 벡터DB 색인 대상이 아니라 매번 새로 들어오는
    질문이다. 산출물은 좌표·시인성이 살아 있는 우리 파싱 결과(`_parsed.json`)다.
    """
    return await _accept(src_file, background_tasks, option, parse_ad)


async def _accept(src_file, background_tasks, option, worker):
    work_dir = ""
    try:
        base_filename = os.path.basename(src_file.filename or "unknown")
        _uuid = uuid.uuid4().hex

        work_dir = os.path.join(PATH_WORK, _uuid)
        img_dir = os.path.join(work_dir, "image")
        os.makedirs(img_dir, exist_ok=True)

        file_fullpath = os.path.join(work_dir, base_filename)
        with open(file_fullpath, "wb") as fo:
            fo.write(await src_file.read())

        # 상태를 **응답 전에** 쓴다. 폴링이 202 직후에 들어와도 UNKNOWN 이 아니라 PARSING 이다.
        write_parse_status(work_dir, PARSING)

        background_tasks.add_task(worker, work_dir, img_dir, file_fullpath, _decode_option(option))

        # 규격: 파싱 요청 정상 응답은 **반드시 202**, body 에 uuid·timeout.
        return JSONResponse(
            status_code=202,
            content=jsonable_encoder({"result": "OK", "body": {"uuid": _uuid, "timeout": TIMEOUT}}),
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse(
            status_code=500,
            content=jsonable_encoder({"message": f"Exception : {exc.__class__.__name__} {exc}"}),
        )


@app.get("/parsing/result/{uuid}", name="Return parsing results")
async def get_parsing_result(uuid: str):
    work_dir = os.path.join(PATH_WORK, uuid)
    try:
        if not os.path.exists(work_dir):
            return JSONResponse(status_code=500,
                                content={"message": "Requested url does not exist."})

        status, msg = get_parse_status(work_dir)
        if status == PARSING:
            return JSONResponse(status_code=200, content={"status": PARSING})
        if status == ERROR:
            # 규격(parser_howto.txt §1-2): "파싱 오류가 발생하면 status 200,
            # {"status":"ERROR", "message": "..."}" — **500 이 아니다.** 500 은 그
            # 아래 문장 "그 외 오류"(uuid 를 못 찾는 등) 몫이다. 이전에는 이 둘을
            # 같은 분기로 묶어 파싱 실패도 500 으로 냈다 — 실행해서 왕복해 보지 않고는
            # 안 드러나는 차이라 왕복 시험(§spec_check)에 이 경로 검사를 새로 추가했다.
            return JSONResponse(status_code=200, content={"status": ERROR, "message": msg})
        if status != DONE:
            # 여기 남는 건 UNKNOWN(상태 파일이 아예 없음) 같은 "그 외 오류" 뿐이다.
            return JSONResponse(status_code=500, content={"message": msg or status})

        zip_bytes = _build_result_zip(Path(work_dir))
        if not KEEP_WORK_DIR:
            shutil.rmtree(work_dir, ignore_errors=True)
        return StreamingResponse(
            iter([zip_bytes]),
            media_type="application/x-zip-compressed",
            headers={"Content-Disposition": "attachment; filename=kl-core-s2-output.zip"},
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": f"Exception : {exc}"})


@app.get("/health")
async def health():
    return {"status": "OK"}


def _decode_option(option: str | None):
    """`option` 은 base64(JSON). 못 읽어도 파싱은 계속한다 — 부가정보일 뿐이다."""
    if not option:
        return None
    import base64
    import json

    try:
        return json.loads(base64.b64decode(option).decode("utf-8"))
    except Exception:  # noqa: BLE001
        print(f"[warn] option 을 해석하지 못했다 (무시하고 계속): {option[:40]}")
        return None


def _build_result_zip(work: Path) -> bytes:
    """결과 3종 + `image/` 를 zip 으로. **경로 없이 파일만** 넣는다(규격 명시)."""
    img_dir = work / "image"
    targets: list[Path] = [
        p for p in sorted(work.iterdir())
        if p.is_file() and p.name.endswith(_RESULT_SUFFIXES)
    ]

    # 이미지가 있으면 `<원본파일명>_img.zip` 을 만들어 함께 넣는다. 이름의 기준이 되는
    # 결과 파일은 트랙마다 다르다 — 규정문서는 `_hrc.jsonl`, 광고물은 `_parsed.json`
    # (§parse_ad, 2026-08-24: 광고 쪽지 이미지를 img_dir 에 놓기 시작하면서 추가했다.
    # 이 기준을 안 넓히면 이미지를 저장은 해 놓고 zip 에 담기지 않아 응답에서 빠진다).
    primary = next(
        (p for p in targets if p.name.endswith(("_hrc.jsonl", "_parsed.json"))), None,
    )
    if primary and img_dir.is_dir():
        images = [p for p in sorted(img_dir.iterdir()) if p.is_file()]
        if images:
            base = primary.name.replace("_hrc.jsonl", "").replace("_parsed.json", "")
            img_zip = work / f"{base}_img.zip"
            with zipfile.ZipFile(img_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in images:
                    zf.write(p, p.name)
            targets.append(img_zip)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in targets:
            zf.write(p, p.name)
    return buf.getvalue()
