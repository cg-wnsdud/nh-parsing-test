# -*- coding: utf-8 -*-
"""농협 Custom Parser의 ``genaikl.status`` 호환 계층.

농협 KL은 UUID별 작업 폴더의 이 파일을 읽어 상태를 판단한다. 상태 문자열과 JSON 키는
계약이므로 변경하지 않는다. 임시 파일을 쓴 뒤 이동해 폴링 중 반쯤 작성된 JSON을 읽지
않게 한다.
"""

import json
import os
import shutil

STATUS_FILENAME = "genaikl.status"
PARSING = "PARSING"
DONE = "DONE"
ERROR = "ERROR"


def write_parse_status(work_dir: str, work_status: str, msg: str = "") -> None:
    """작업 상태와 오류 메시지를 원자적으로 기록한다."""
    status_file = os.path.join(work_dir, STATUS_FILENAME)
    temp_file = status_file + ".tmp"
    with open(temp_file, "w+", encoding="utf-8") as status_fd:
        json.dump({"status": work_status, "message": msg}, status_fd, ensure_ascii=False)
    shutil.move(temp_file, status_file, copy_function=shutil.copy)


def get_parse_status(work_dir: str) -> tuple[str, str]:
    """현재 상태를 반환한다. 상태 파일 생성 중이면 PARSING, 없으면 UNKNOWN이다."""
    status_file = os.path.join(work_dir, STATUS_FILENAME)
    if os.path.exists(status_file):
        with open(status_file, "r", encoding="utf-8") as status_fd:
            json_data = json.load(status_fd)
            return (json_data["status"], json_data["message"])
    if os.path.exists(work_dir) and os.path.exists(status_file + ".tmp"):
        return (PARSING, "")
    return ("UNKNOWN", "Requested url does not exist.")
