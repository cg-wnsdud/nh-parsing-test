# -*- coding: utf-8 -*-
"""파싱 상태 파일 — 농협 예제의 `DO NOT EDIT` 블록을 **그대로** 옮긴 것.

원본: `1.awx_custom_parser_example_api/server/flow/app_custom_parser/service/parsing_service.py`
농협이 주석으로 `DO NOT EDIT` 이라 명시한 두 함수다. KL 이 이 파일(`genaikl.status`)을
읽어 폴링 응답을 만들기 때문에 형식(키 이름·값)을 바꾸면 연동이 깨진다.

우리가 손댄 것은 **파일을 별 모듈로 뺀 것뿐**이다 — 우리 파싱 로직(parsing_service.py)과
섞여 있으면 나중에 규격이 개정될 때 어디까지가 농협 것인지 구분이 안 된다.
"""

import json
import os
import shutil

STATUS_FILENAME = "genaikl.status"

# 농협 규격의 상태 값 3종. 이 문자열이 곧 계약이다.
PARSING = "PARSING"
DONE = "DONE"
ERROR = "ERROR"


def write_parse_status(work_dir: str, work_status: str, msg: str = "") -> None:
    """상태를 기록한다. tmp 에 쓰고 move — 폴링이 반쯤 쓰인 파일을 읽는 것을 막는다."""
    status_file = os.path.join(work_dir, STATUS_FILENAME)
    temp_file = status_file + ".tmp"
    with open(temp_file, "w+", encoding="utf-8") as status_fd:
        _status = {"status": work_status, "message": msg}
        json.dump(_status, status_fd, ensure_ascii=False)
    shutil.move(temp_file, status_file, copy_function=shutil.copy)


def get_parse_status(work_dir: str) -> tuple[str, str]:
    """현 상태를 (status, message) 로 반환. 디렉터리·파일이 없으면 UNKNOWN."""
    status_file = os.path.join(work_dir, STATUS_FILENAME)
    if os.path.exists(status_file):
        with open(status_file, "r", encoding="utf-8") as status_fd:
            json_data = json.load(status_fd)
            return (json_data["status"], json_data["message"])
    if os.path.exists(work_dir) and os.path.exists(status_file + ".tmp"):
        return (PARSING, "")
    return ("UNKNOWN", "Requested url does not exist.")
