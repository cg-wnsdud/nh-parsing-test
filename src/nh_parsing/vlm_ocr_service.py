# -*- coding: utf-8 -*-
"""파일 바이트 하나 → 대상 계약 NormalizedDocument v1 dict. 서비스의 알맹이.

**여기에 FastAPI 가 없는 게 핵심이다.** 대상 저장소에 들어갈 `vlm_ocr_app.py` 는
HTTP 껍데기 20여 줄뿐이고, 실제로 하는 일은 전부 이 함수 하나다. 그래서:

  · 이 저장소에서 HTTP 없이 그대로 테스트할 수 있다 (아래 함수를 직접 부르면 된다)
  · 대상 저장소가 리뷰할 코드가 껍데기로 줄어든다
  · 우리 파이프라인이 바뀌어도 대상 껍데기는 안 바뀐다

대상 껍데기가 할 일은 `parser-service/vlm_ocr_app.py` 에 그대로 적혀 있다.

**이 서비스가 맡는 것은 이미지·PDF 뿐이다.** HWP/HWPX 는 대상 라우터가 `hwp-hybrid`
로 보내므로 여기 올 일이 없고, 와도 거절한다 — 캔버스가 없어 계약을 통과할 수 없다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

from .normalized_export import PARSER_NAME, PARSER_VERSION, ExportResult, export_normalized_document
from .pipeline import process_file

__all__ = ["PARSER_NAME", "ParseFailed", "parse_to_normalized"]


class ParseFailed(ValueError):
    """대상 껍데기가 422 로 바꿔 내보낼 실패.

    대상 `service.py:45` 의 `app_for()` 가 어떤 예외든 잡아 422
    `{engine}_PARSE_FAILED` 로 바꾼다 — 엔진 내부 사정이 워커로 새지 않게 하려는
    설계다. 그래서 여기서는 예외 종류를 늘리지 않고 뜻만 분명히 한다.
    """


def parse_to_normalized(
    *,
    source_file_id: str,
    review_id: str,
    file_name: str,
    body: bytes,
    parser_version: str = PARSER_VERSION,
    raw_artifact_ref: Optional[str] = None,
    preview_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """광고물 파일 하나를 파싱해 계약 dict 로 돌려준다.

    `source_file_id` · `review_id` 는 요청에서 온다 — 워커가 응답의 신원 3개
    (reviewId · sourceFileId · parserName)를 요청과 대조해 다르면 거부한다
    (`parser_services.py:63`). 우리가 지어내면 안 된다.
    """
    result = _run(
        source_file_id=source_file_id,
        review_id=review_id,
        file_name=file_name,
        body=body,
        parser_version=parser_version,
        raw_artifact_ref=raw_artifact_ref,
        preview_dir=preview_dir,
    )
    if result.skipped:
        raise ParseFailed(f"이 서비스가 맡지 않는 입력이다: {result.skipped}")
    document = result.document
    assert document is not None
    if not document["textBlocks"]:
        # 대상 형제 서비스들과 같은 판정(`paddleocr_app.py:78` 의 "no readable text").
        # 글자가 0개인 응답은 계약은 통과하지만 심의에 쓸 수 없다. 조용히 빈 문서를
        # 돌려주면 워커가 "파싱 성공"으로 적재한다.
        raise ParseFailed("읽어 낸 글자가 없다")
    return document


def _run(
    *,
    source_file_id: str,
    review_id: str,
    file_name: str,
    body: bytes,
    parser_version: str,
    raw_artifact_ref: Optional[str],
    preview_dir: Optional[Path],
) -> ExportResult:
    """파이프라인은 경로를 받는다 — 바이트를 임시 파일로 떨궈 부른다.

    대상 `paddleocr_app.py:26` 도 같은 방식이다(`source.write_bytes(body)`).
    **파일 이름을 그대로 써야 한다** — 파이프라인이 확장자로 경로를 가르고
    (`pipeline.py:33`), 분류 단계가 파일명을 사전 근거로 쓴다(`category_source`
    의 `filename_and_vlm`).
    """
    if not body:
        raise ParseFailed("빈 파일")
    # 요청의 fileName 이 그대로 경로가 되므로 폴더 부분을 떼어 낸다.
    # `.strip()` 까지 봐야 한다 — 공백만 있는 이름은 위 두 검사를 통과한 뒤 파일을
    # 열 때 PermissionError 로 터진다(2026-08-07 실측, 윈도우).
    safe_name = Path(file_name).name
    if not safe_name.strip() or safe_name in {".", ".."}:
        raise ParseFailed(f"쓸 수 없는 파일 이름: {file_name!r}")

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / safe_name
        source.write_bytes(body)
        document = process_file(source, preview_dir)

    return export_normalized_document(
        document.model_dump(),
        source_file_id=source_file_id,
        review_id=review_id,
        # 감사 추적. 좌표·판독 후보·notes 처럼 계약에 자리가 없는 것들이 어디에
        # 남아 있는지를 가리킨다. 대상 기본값 규약은 f"{engine}-service-v1".
        raw_artifact_ref=raw_artifact_ref or f"{PARSER_NAME}-service-v1",
        parser_version=parser_version,
    )
