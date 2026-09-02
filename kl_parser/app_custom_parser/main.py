# -*- coding: utf-8 -*-
"""농협 Knowledge Lake가 호출하는 Custom Parser API 진입점.

이 파일은 문서를 직접 OCR하거나 VLM에 묻지 않는다. 맡은 일은 농협의 비동기 API 계약을
지키는 것이다.

    업로드(POST) → UUID 발급 → 백그라운드 파싱 → 상태 폴링(GET) → 결과 ZIP

실제 파싱은 ``service.parsing_service``가 프로젝트의 ``nh_parsing``에 연결한다.
``nh_parsing``은 필요할 때 PADDLEX_URL/GEMMA_URL의 OCR·VLM GPU 서비스에 HTTP로 요청한다.
따라서 in-process는 ``kl_parser``와 ``nh_parsing``의 Python 호출 관계이지, 모델 추론이 이
프로세스에서 돈다는 뜻은 아니다.

농협 배포는 ``cd app_custom_parser && gunicorn main:app`` 형태다. 이때 ``main``은 최상위
모듈이므로 상대 import를 쓰면 실패한다. 그래서 예제와 같이 ``from service...`` 절대 import를
사용한다.
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

# KL이 결과를 기다리는 시간(초). 응답 시간보다 길게 잡아야 KL이 작업 중에 포기하지 않는다.
TIMEOUT = int(os.getenv("TIMEOUT", "1800"))
PATH_WORK = os.getenv("PATH_TEMP", "./temp")
KEEP_WORK_DIR = os.getenv("KEEP_WORK_DIR", "") not in ("", "0", "false", "False")

# ZIP에 넣을 호출자용 산출물. kl_parser_notes.json은 검수용 내부 파일이라 제외한다.
_RESULT_SUFFIXES = (
    "_hrc.jsonl", "_hrc.json", "doc_data.json",  # 규정문서
    "_parsed.json", "_review_input.json", "ad_summary.json",  # 광고물
)

app = FastAPI(title="NH KL Custom Parser (CGInside)", version="0.1.0")


@app.post("/parsing", name="Request parsing", description="규정문서 파싱을 요청한다(KL 규격).")
async def request_parsing(
    src_file: UploadFile,
    background_tasks: BackgroundTasks,
    option: str = Form(default=None),
):
    """규정문서를 접수한다. 결과는 KL 색인용 ``*_hrc.*`` 파일이다."""
    return await _accept(src_file, background_tasks, option, parse)


@app.post("/ad/parsing", name="Request ad parsing", description="광고물 파싱을 요청한다.")
async def request_ad_parsing(
    src_file: UploadFile,
    background_tasks: BackgroundTasks,
    option: str = Form(default=None),
):
    """광고물을 접수한다. 통신은 같고 결과는 ``*_parsed.json``이다.

    이 경로는 농협 예제의 필수 KL 색인 API가 아니라 프로젝트가 추가한 광고 파싱 API다.
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

        # 응답 전에 상태를 기록한다. 202 직후 폴링해도 UNKNOWN 대신 PARSING을 반환한다.
        write_parse_status(work_dir, PARSING)
        background_tasks.add_task(worker, work_dir, img_dir, file_fullpath, _decode_option(option))
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
    """UUID 작업의 현재 상태 또는 완료 ZIP을 반환한다."""
    work_dir = os.path.join(PATH_WORK, uuid)
    try:
        if not os.path.exists(work_dir):
            return JSONResponse(status_code=500, content={"message": "Requested url does not exist."})

        status, msg = get_parse_status(work_dir)
        if status == PARSING:
            return JSONResponse(status_code=200, content={"status": PARSING})
        if status == ERROR:
            # 농협 규격: 파싱 실패는 200 + ERROR, UUID가 없는 등의 API 오류는 500.
            return JSONResponse(status_code=200, content={"status": ERROR, "message": msg})
        if status != DONE:
            return JSONResponse(status_code=500, content={"message": msg or status})

        zip_bytes = _build_result_zip(Path(work_dir))
        if not KEEP_WORK_DIR:
            shutil.rmtree(work_dir, ignore_errors=True)
        return StreamingResponse(
            iter([zip_bytes]), media_type="application/x-zip-compressed",
            headers={"Content-Disposition": "attachment; filename=kl-core-s2-output.zip"},
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": f"Exception : {exc}"})


@app.get("/health")
async def health():
    """파싱이나 GPU 호출 없이 API 프로세스가 살아 있는지만 확인한다."""
    return {"status": "OK"}


def _decode_option(option: str | None):
    """농협이 보낸 base64(JSON) option을 해석한다. 읽지 못하면 로그만 남기고 계속한다."""
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
    """호출자용 산출물과, 필요할 때 이미지 ZIP을 하나의 응답 ZIP으로 묶는다."""
    img_dir = work / "image"
    targets: list[Path] = [
        p for p in sorted(work.iterdir()) if p.is_file() and p.name.endswith(_RESULT_SUFFIXES)
    ]

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
