"""VLM+OCR private service for image and scanned-PDF advertisements.

이 파일이 갈 자리:  nh-ad-compliance/apps/parser-services/vlm_ocr_app.py

형제 서비스들과 같은 모양이다 — `service.py` 의 `app_for(engine)` 에 엔진 하나를 물리면
`/health` 와 `/v1/parse` 가 생긴다. 실제 파싱은 전부 `nh_parsing` 안에 있고 여기는
전송 계층뿐이라, 대상 저장소가 리뷰할 코드가 이 파일로 끝난다.

형제들과 다른 점 하나: **`service.py` 의 `normalized_document()`·`text_block()` 헬퍼를
쓰지 않는다.** 그 헬퍼는 `coordinateConfidence = confidenceScore` 로 둘을 묶어 두는데
(`service.py:134`), 우리는 "글자는 맞고 위치는 못 믿는" 통짜 스윕 라인 때문에 둘을
갈라야 한다. 계약 dict 는 `nh_parsing.normalized_export` 가 만든다 — 그쪽에 근거가
주석으로 붙어 있고 테스트도 거기 있다.

컨테이너 메모:
  · 파이썬 3.13 으로 지어도 된다. 이 서비스는 계약 패키지(`>=3.12,<3.13`)를 import
    하지 않는다 — 형제 서비스 4개도 전부 그렇고, Dockerfile.document-processor 는
    이미 python:3.13-slim 이다. 계약 검증은 워커가 한다.
  · Java·사내 HWP 파서가 필요 없다. `nh_parsing.hwp_ingest` 가 `document_processor`
    를 함수 안에서 지연 import 하고(hwp_ingest.py:221), 이 서비스는 HWP 를 받지
    않는다(대상 라우터가 .hwp 를 hwp-hybrid 로 보낸다).
  · PaddleX·Gemma 서버에 닿아야 한다 (PADDLEX_URL · GEMMA_URL · GEMMA_MODEL).
    형제 서비스들이 자기 컨테이너 안에서 자족하는 것과 다른 점이라 배포 시 확인이
    필요하다. ⚠️ GEMMA_MODEL 이 게이트웨이 목록과 다르면 VLM 호출이 전부 400 으로
    죽는데 OCR 은 멀쩡히 돌아 산출물이 그럴듯하게 나온다.
  · 문서당 100~300초가 걸린다(그중 97%가 모델 대기). 형제 서비스보다 훨씬 느리므로
    워커의 PARSER_SERVICE_TIMEOUT_SECONDS(기본 120)를 늘려야 한다.
"""

from __future__ import annotations

from nh_parsing.vlm_ocr_service import PARSER_NAME, parse_to_normalized

from service import ParseRequest, app_for


class VlmOcrEngine:
    name = PARSER_NAME

    def parse(self, request: ParseRequest, body: bytes) -> dict[str, object]:
        return parse_to_normalized(
            source_file_id=request.source_file_id,
            review_id=request.review_id,
            file_name=request.file_name,
            body=body,
        )


engine = VlmOcrEngine()
app = app_for(engine)
