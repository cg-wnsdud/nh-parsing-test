# -*- coding: utf-8 -*-
"""농협 GenAI Knowledge Lake Custom 파서 진입점 (비동기 방식).

계약은 농협 규격서(`readme.md`)와 예제(`app_custom_parser/main.py`)를 그대로 따른다.
바꾼 곳은 파싱 본체(`service.parsing_service.parse`)뿐이다.

  POST /parsing                  → **202** + {result, body:{uuid, timeout}}
  GET  /parsing/result/{uuid}    → 200 + zip | 200 + {"status":"PARSING"} | 500 + {message}
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

from .service.parsing_service import parse
from .service.status import DONE, PARSING, get_parse_status

TIMEOUT = int(os.getenv("TIMEOUT", "600"))
PATH_WORK = os.getenv("PATH_TEMP", "./temp")
KEEP_WORK_DIR = os.getenv("KEEP_WORK_DIR", "") not in ("", "0", "false", "False")

# zip 에 넣을 결과 파일. 규격서 "Output jsonl" 단락이 정한 3종 + doc_data(선택).
# `kl_parser_notes.json` 은 우리 검수용이라 **일부러 제외**한다.
_RESULT_SUFFIXES = ("_hrc.jsonl", "_hrc.json", "doc_data.json")

app = FastAPI(title="NH KL Custom Parser (CGInside)", version="0.1.0")


@app.post("/parsing", name="Request parsing", description="문서 파싱을 요청한다(비동기).")
async def request_parsing(
    src_file: UploadFile,
    background_tasks: BackgroundTasks,
    option: str = Form(default=None),
):
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
        from .service.status import write_parse_status

        write_parse_status(work_dir, PARSING)

        background_tasks.add_task(parse, work_dir, img_dir, file_fullpath, _decode_option(option))

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
        if status != DONE:
            # 실패 디렉터리는 남긴다(위 ② 참조).
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

    # 이미지가 있으면 `<원본파일명>_img.zip` 을 만들어 함께 넣는다.
    jsonl = next((p for p in targets if p.name.endswith("_hrc.jsonl")), None)
    if jsonl and img_dir.is_dir():
        images = [p for p in sorted(img_dir.iterdir()) if p.is_file()]
        if images:
            img_zip = work / jsonl.name.replace("_hrc.jsonl", "_img.zip")
            with zipfile.ZipFile(img_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in images:
                    zf.write(p, p.name)
            targets.append(img_zip)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in targets:
            zf.write(p, p.name)
    return buf.getvalue()
