# -*- coding: utf-8 -*-
"""우리 파싱 산출물(out/json) → 대상 `nh-ad-compliance` 의 NormalizedDocument v1 **dict**.

**왜 dict 인가.** 대상 계약 패키지(`nh-ad-parser-contracts`)는 `requires-python`
`>=3.12,<3.13` 으로 3.13 을 **명시적으로 배제**한다. 이 저장소는 `>=3.13`(실행
3.13.14)이라 계약 모델을 import 할 수가 없다. 그래서 여기서는 pydantic 모델을 쓰지 않고
**계약의 와이어 표현(dict)만** 만든다. 실제 검증은 프로젝트 밖 3.12 환경에서
`tools/verify_contract.py` 가 `NormalizedDocument.model_validate()` 로 돌린다.
어차피 어댑터 코드는 최종적으로 대상 저장소(3.12) 안에 살게 되므로 이 구조가 자연스럽다.

**범위.** 이미지·PDF 만. HWP/HWPX 는 대상 저장소가 이미 자기 서비스로 처리한다
(`document_processor_app.py` — 우리 `hwp_ingest.py` 와 **같은 사내 파서**를 쓰고,
좌표 없는 문제를 `textPath` 로 이미 풀었다). 캔버스가 없는 문서는 변환하지 않고
`ExportResult.skipped` 에 사유를 남긴다.

**이 변환기가 가져다주는 것은 `layoutBlocks` 다.** 대상 `paddleocr_app.py:79` 는 이 배열을
빈 채로 내보낸다(`normalized_document(...)` 호출에 `layout_blocks` 인자가 없다).
우리는 영역 224개에 역할(한글 9종)까지 붙여 넘긴다.

**계약 밖이라 실리지 못하는 것** (버리는 게 아니라 `out/json` 에 그대로 남는다):
  · `Region.vlm_reading*` — 영역 통독 후보. 계약에 후보 판독을 담을 칸이 없다
  · `Line.vlm_reading*`   — 라인 재판독 후보. 같은 이유
  · `layout_score` · `role_rule` · `role_source` · `card_no` · `triage` — 진단 정보
  · `product_group` · `ad_type` · `category_source` — 분류 결과. 계약에 자리가 없다
`ExportResult.stats` 가 몇 건이 못 실렸는지 센다 — 조용히 사라지지 않게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Optional

# ─────────────────────────── 계약 고정값 ───────────────────────────
# 대상 `routing.py:77` 이 이미지 2차 어댑터로 이름만 예약해 둔 슬롯. 이 이름을 쓰면
# 라우팅 코드를 건드리지 않아도 된다.
PARSER_NAME = "vlm-ocr"
# 대상 `service.py:73` 규약 f"{engine}-normalized-v1" 을 그대로 따른다.
PARSER_RULE_VERSION = f"{PARSER_NAME}-normalized-v1"
# NormalizedDocument.ir_version 은 pattern=r"^normalized-document-v1$" 로 고정돼 있다.
IR_VERSION = "normalized-document-v1"
# Confidence.policy_version · TextBlock.confidence_policy_version (대상 `service.py:83`).
CONFIDENCE_POLICY_VERSION = "confidence-thresholds-v1"
# 좌표 단위. 우리 bbox 는 전부 원본 캔버스 픽셀이다(ir.py 모듈 주석).
SOURCE_UNIT = "pixel"
# `textBlockType` 은 자유 문자열이고 대상에서 이 값으로 분기하는 코드는 hybrid.py 의
# HWP 병합뿐이다. 대상 서비스들이 쓰는 값("BODY")을 그대로 따른다 — 역할 정보는
# `layoutType` 으로 손실 없이 넘어가므로 여기에 우리 어휘를 새로 만들 이유가 없다.
TEXT_BLOCK_TYPE = "BODY"

# ─────────────────────────── 채움값 (전부 실측 근거) ───────────────────────────
# `source == "digital"` 라인은 confidence 가 None 이다. 2026-08-06 무캐시 산출물 전수:
# confidence 가 None 인 라인 25건이 **전부** digital 이고, digital 이면서 값이 있는
# 라인은 0건이다. PDF 텍스트 레이어를 파서가 그대로 읽은 정본이라 OCR 추정이 아니다
# (pr-plan §3-3 이 HWP 에 대해 세운 논리와 같다). 계약은 confidenceScore 를 필수로
# 요구하므로 None 을 그대로 둘 수 없다.
DIGITAL_CONFIDENCE = 1.0
# `role_confidence` 가 None 인 영역 20건은 **전부** role_source == "rules"(VLM 실패
# 폴백)다. 같은 폴백 경로인데 점수가 붙은 5건은 전부 0.5 였다 — 지어낸 값이 아니라
# 같은 경로의 실측값을 그대로 쓴다.
RULES_ROLE_CONFIDENCE = 0.5
# `vlm_sweep` 라인 15건은 **15건 전부** bbox 가 [0, y0, canvas_w, y1] 이다. 글자는 그
# 가로줄 어딘가에 있는데 좌표는 줄 전체를 가리킨다. 계약 검증은 통과하지만(폭>0,
# 경계 내) 뜻이 틀렸다. 대상은 coordinateConfidence < 0.5 를 `LIST_ONLY` 표시로
# 떨어뜨린다(`results.py:345`, 테스트 `(0.49, "LIST_ONLY", "NOT_LOCATED")`) — 즉
# "글자는 맞는데 위치는 못 믿는다"를 계약이 이미 표현할 수 있다.
SWEEP_COORDINATE_CONFIDENCE = 0.3

# Coordinate 는 width·height 에 gt=0 을 건다. 대상 `paddleocr_app.py:72` 도 같은 이유로
# max(..., 1.0) 을 쓴다. 2026-08-06 산출물에는 폭·높이 0 인 bbox 가 0건이지만, 서비스가
# 문서 하나 때문에 통째로 죽는 것보다 1px 로 올리고 세는 편이 낫다.
MIN_EXTENT = 1.0

_CANVASLESS_TYPES = {"hwp", "hwpx"}


def _parser_version() -> str:
    try:
        return version("nh-ad-review-poc")
    except PackageNotFoundError:  # 설치 없이 소스에서 돌릴 때
        return "0.0.0+unknown"


PARSER_VERSION = _parser_version()


# ─────────────────────────── 반환값 ───────────────────────────


@dataclass(frozen=True)
class ExportResult:
    """변환 결과 하나.

    `document` 가 None 이면 변환하지 않은 것이고 `skipped` 에 사유가 있다.
    `stats` 는 무엇이 몇 건 들어가고 몇 건 빠졌는지의 집계 — 문서에 쓸 숫자는 손으로
    세지 않고 여기서 뽑는다(`tools/verify_numbers.py --section export`).
    """

    document: Optional[dict[str, Any]] = None
    skipped: Optional[str] = None
    stats: dict[str, int] = field(default_factory=dict)


# ─────────────────────────── 계약 헬퍼 ───────────────────────────


def confidence_status(score: float) -> str:
    """대상 `models.py:20 confidence_status()` 와 같은 임계값.

    계약에 `status_matches_score` 검증기가 있어 **임의로 적을 수 없다.** 여기서
    어긋나면 pydantic 이 거부한다.
    """
    if score >= 0.80:
        return "READABLE"
    if score >= 0.50:
        return "LOW_CONFIDENCE"
    return "UNREADABLE"


def _clamp01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _coordinate(
    bbox: Optional[list[float]],
    canvas_w: float,
    canvas_h: float,
    coordinate_confidence: float,
    stats: dict[str, int],
) -> Optional[dict[str, Any]]:
    """bbox [x0, y0, x1, y1] → 계약 Coordinate.

    계약 검증기(`models.py:56`)가 두 가지를 본다:
      · x + width <= sourceWidth (y 도 동일) — 넘으면 "coordinate exceeds the source bounds"
      · normalized* 4개가 원본/캔버스와 abs_tol=1e-4 안에서 일치
    2026-08-06 산출물에는 경계를 넘는 bbox 가 0건이지만, 넘는 값이 오면 캔버스로 잘라
    넣고 `clamped_coordinates` 로 센다 — 문서 하나가 통째로 거부되는 것보다 낫다.
    """
    if not bbox or canvas_w <= 0 or canvas_h <= 0:
        return None

    x0, y0, x1, y1 = (float(v) for v in bbox)
    x, y = max(0.0, min(x0, canvas_w)), max(0.0, min(y0, canvas_h))
    width = max(min(x1, canvas_w) - x, MIN_EXTENT)
    height = max(min(y1, canvas_h) - y, MIN_EXTENT)
    # MIN_EXTENT 로 올린 뒤 경계를 넘을 수 있다(캔버스 맨 끝에 붙은 1px 상자).
    x = min(x, canvas_w - width)
    y = min(y, canvas_h - height)
    if (x, y, width, height) != (x0, y0, x1 - x0, y1 - y0):
        stats["clamped_coordinates"] = stats.get("clamped_coordinates", 0) + 1

    return {
        "sourceWidth": canvas_w,
        "sourceHeight": canvas_h,
        "sourceUnit": SOURCE_UNIT,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "normalizedX": x / canvas_w,
        "normalizedY": y / canvas_h,
        "normalizedWidth": width / canvas_w,
        "normalizedHeight": height / canvas_h,
        "coordinateConfidence": _clamp01(coordinate_confidence),
    }


def line_confidence(line: dict[str, Any]) -> float:
    """라인 하나의 계약 confidenceScore."""
    raw = line.get("confidence")
    if raw is None:
        # digital 라인만 여기 온다 (모듈 상단 DIGITAL_CONFIDENCE 주석의 전수 확인).
        return DIGITAL_CONFIDENCE if line.get("source") == "digital" else 0.0
    return _clamp01(raw)


def _text_block(
    line: dict[str, Any],
    *,
    text_block_id: str,
    source_file_id: str,
    page_no: int,
    canvas_w: float,
    canvas_h: float,
    parser_version: str,
    stats: dict[str, int],
) -> dict[str, Any]:
    score = line_confidence(line)
    text = line["text"]
    # 스윕 라인만 좌표 신뢰도를 텍스트 신뢰도에서 떼어 낸다. 대상 헬퍼
    # `service.py:134` 는 둘을 같이 묶어 두는데, 우리는 "글자는 맞고 위치만 못 믿는"
    # 경우가 있어 그 헬퍼를 그대로 못 쓴다.
    coordinate_confidence = (
        SWEEP_COORDINATE_CONFIDENCE if line.get("source") == "vlm_sweep" else score
    )
    return {
        "textBlockId": text_block_id,
        "fileId": source_file_id,
        "pageNo": page_no,
        "textBlockType": TEXT_BLOCK_TYPE,
        "rawText": text,
        # 정규화 규칙은 아직 합의 전이다(pr-plan §2-2). 규칙 없이 손대면 대상의
        # normalizedText 와 뜻이 갈리므로 지금은 정본을 그대로 둔다.
        "normalizedText": text,
        "parserName": PARSER_NAME,
        "parserVersion": parser_version,
        "parserRuleVersion": PARSER_RULE_VERSION,
        "irVersion": IR_VERSION,
        "confidenceScore": score,
        "confidenceStatus": confidence_status(score),
        "confidencePolicyVersion": CONFIDENCE_POLICY_VERSION,
        "coordinate": _coordinate(
            line.get("bbox"), canvas_w, canvas_h, coordinate_confidence, stats
        ),
    }


def region_confidence(region: dict[str, Any]) -> float:
    """영역 하나의 계약 confidenceScore (= 역할 판정 확신도)."""
    raw = region.get("role_confidence")
    if raw is None:
        # rules 폴백만 여기 온다 (모듈 상단 RULES_ROLE_CONFIDENCE 주석의 전수 확인).
        return RULES_ROLE_CONFIDENCE
    return _clamp01(raw)


def _layout_block(
    region: dict[str, Any],
    *,
    source_file_id: str,
    page_no: int,
    canvas_w: float,
    canvas_h: float,
    related_text_block_ids: list[str],
    stats: dict[str, int],
) -> dict[str, Any]:
    score = region_confidence(region)
    return {
        "layoutBlockId": region["region_id"],
        "fileId": source_file_id,
        "pageNo": page_no,
        # `layoutType` 은 자유 문자열이라 우리 역할 이름(한글 9종)을 손실 없이 넘긴다.
        "layoutType": region.get("role") or "기타",
        # 영역 상자는 레이아웃 엔진이 직접 잡은 것이라 위치 자체는 믿을 수 있다.
        # 여기 들어가는 role_confidence 는 "역할 판정"의 확신도이지 상자 위치의
        # 확신도가 아니므로, 좌표 신뢰도로 재사용하지 않고 layout_score(엔진이 이
        # 블록 판정에 붙인 점수)를 쓴다. 없으면 역할 확신도로 떨어진다.
        "coordinate": _coordinate(
            region.get("bbox"),
            canvas_w,
            canvas_h,
            region.get("layout_score") if region.get("layout_score") is not None else score,
            stats,
        ),
        "relatedTextBlockIds": related_text_block_ids,
        "confidenceScore": score,
    }


# ─────────────────────────── 본체 ───────────────────────────


def export_normalized_document(
    ad_document: dict[str, Any],
    *,
    source_file_id: str,
    review_id: str,
    raw_artifact_ref: str,
    parser_version: str = PARSER_VERSION,
    created_at: Optional[datetime] = None,
) -> ExportResult:
    """AdDocument dict(out/json) → NormalizedDocument v1 dict.

    `source_file_id` · `review_id` 는 **우리가 만들지 않는다** — 대상 워커가 요청에
    실어 보내고(`parser_services.py:39`), 응답의 신원 3개(reviewId · sourceFileId ·
    parserName)가 요청과 다르면 거부한다. 그래서 인자로 받는다.
    """
    stats: dict[str, int] = {
        "pages": 0,
        "lines_in": 0,
        "text_blocks": 0,
        "dropped_empty_text": 0,
        "sweep_blocks": 0,
        "digital_filled_confidence": 0,
        "layout_blocks": 0,
        "layout_blocks_without_coordinate": 0,
        "rules_filled_role_confidence": 0,
        "clamped_coordinates": 0,
        "region_readings_not_carried": 0,
        "line_readings_not_carried": 0,
    }

    skip = _canvasless_reason(ad_document)
    if skip:
        return ExportResult(document=None, skipped=skip, stats=stats)

    pages: list[dict[str, Any]] = []
    text_blocks: list[dict[str, Any]] = []
    layout_blocks: list[dict[str, Any]] = []
    non_ok_pages: list[str] = []

    for page in ad_document.get("pages", []):
        page_no = int(page["page_no"])
        canvas_w, canvas_h = float(page["canvas_w"]), float(page["canvas_h"])
        stats["pages"] += 1
        pages.append(
            {"pageNo": page_no, "width": canvas_w, "height": canvas_h, "unit": SOURCE_UNIT}
        )
        if page.get("parse_status", "ok") != "ok":
            non_ok_pages.append(f"p{page_no}={page['parse_status']}")

        for region in page.get("regions", []):
            related: list[str] = []
            for index, line in enumerate(region.get("lines", [])):
                block_id = f"{region['region_id']}_t{index:03d}"
                block = _emit_line(
                    line,
                    block_id=block_id,
                    source_file_id=source_file_id,
                    page_no=page_no,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    parser_version=parser_version,
                    stats=stats,
                )
                if block is not None:
                    text_blocks.append(block)
                    related.append(block_id)
            if region.get("vlm_reading"):
                stats["region_readings_not_carried"] += 1
            if region.get("role_confidence") is None:
                stats["rules_filled_role_confidence"] += 1
            layout = _layout_block(
                region,
                source_file_id=source_file_id,
                page_no=page_no,
                canvas_w=canvas_w,
                canvas_h=canvas_h,
                related_text_block_ids=related,
                stats=stats,
            )
            if layout["coordinate"] is None:
                stats["layout_blocks_without_coordinate"] += 1
            layout_blocks.append(layout)
            stats["layout_blocks"] += 1

        # 미배정 라인도 글자다. 어느 영역에도 안 실렸을 뿐이므로 textBlock 으로는
        # 내보내고 relatedTextBlockIds 어디에도 넣지 않는다.
        for index, line in enumerate(page.get("unassigned_lines", [])):
            block = _emit_line(
                line,
                block_id=f"p{page_no}_u_t{index:03d}",
                source_file_id=source_file_id,
                page_no=page_no,
                canvas_w=canvas_w,
                canvas_h=canvas_h,
                parser_version=parser_version,
                stats=stats,
            )
            if block is not None:
                text_blocks.append(block)

    warnings = _warnings(stats, non_ok_pages)
    # 대상 `service.py:65` 와 같은 식. 우리가 응답 전체를 만들므로 여기서 계산한다.
    # ⚠️ 빈 텍스트 11건을 걸러도 confidence < 0.5 인 라인이 남는 문서가 있어 문서
    # 신뢰도가 UNREADABLE 로 떨어질 수 있다 — 계약 위반은 아니고 사실 그대로다.
    score = min((block["confidenceScore"] for block in text_blocks), default=0.0)
    stamp = created_at or datetime.now(timezone.utc)

    document = {
        "documentId": f"doc-{source_file_id}",
        "sourceFileId": source_file_id,
        "reviewId": review_id,
        "sourceFileType": _source_file_type(ad_document),
        "parserName": PARSER_NAME,
        "parserVersion": parser_version,
        "parserRuleVersion": PARSER_RULE_VERSION,
        "irVersion": IR_VERSION,
        "pages": pages,
        "textBlocks": text_blocks,
        "layoutBlocks": layout_blocks,
        # 우리 IR 에 표 모델이 없다 — 표도 라인으로 평탄화된다(pr-plan §2-1).
        "tables": [],
        "warnings": warnings,
        "confidence": {
            "score": score,
            "status": confidence_status(score),
            "policyVersion": CONFIDENCE_POLICY_VERSION,
        },
        "rawArtifactRef": raw_artifact_ref,
        "createdAt": stamp.isoformat(),
    }
    return ExportResult(document=document, skipped=None, stats=stats)


def _emit_line(
    line: dict[str, Any],
    *,
    block_id: str,
    source_file_id: str,
    page_no: int,
    canvas_w: float,
    canvas_h: float,
    parser_version: str,
    stats: dict[str, int],
) -> Optional[dict[str, Any]]:
    """라인 하나 → textBlock. 글자가 없으면 None (세기는 한다)."""
    stats["lines_in"] += 1
    if not str(line.get("text") or "").strip():
        # 빈 문자열은 애초에 글자 블록이 아니다 — OCR 이 상자는 잡았는데 글자를 못
        # 읽은 것이다(전수 11건, 전부 confidence 0.0). 대상은 문서 신뢰도를
        # min(모든 블록)으로 계산하므로(`service.py:65`) 0.0 하나가 섞이면 나머지
        # 477줄을 아무리 잘 읽었어도 문서가 통째로 UNREADABLE 로 떨어진다.
        # 대상 `document_processor_app.py` 도 `if not text.strip(): return None` 으로
        # 같은 처리를 한다. 버리지 않고 warnings 에 건수로 남긴다.
        stats["dropped_empty_text"] += 1
        return None
    if line.get("confidence") is None:
        stats["digital_filled_confidence"] += 1
    if line.get("source") == "vlm_sweep":
        stats["sweep_blocks"] += 1
    if line.get("vlm_reading"):
        stats["line_readings_not_carried"] += 1
    stats["text_blocks"] += 1
    return _text_block(
        line,
        text_block_id=block_id,
        source_file_id=source_file_id,
        page_no=page_no,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        parser_version=parser_version,
        stats=stats,
    )


def _canvasless_reason(ad_document: dict[str, Any]) -> Optional[str]:
    """변환 제외 사유. 없으면 None.

    파일 확장자가 아니라 **캔버스가 있느냐**로 판정한다 — 계약을 막는 것은 확장자가
    아니라 `Coordinate.sourceWidth: gt=0` 이기 때문이다(HWP 는 canvas 0×0).
    """
    file_type = str(ad_document.get("file_type") or "").casefold()
    if file_type in _CANVASLESS_TYPES:
        return (
            f"file_type={file_type}: 대상 저장소가 자기 HWP 서비스로 처리한다"
            " (document_processor_app — 같은 사내 파서, textPath 로 좌표 문제 해결됨)"
        )
    bad = [
        f"p{page['page_no']}={page['canvas_w']}x{page['canvas_h']}"
        for page in ad_document.get("pages", [])
        if not (int(page.get("canvas_w") or 0) > 0 and int(page.get("canvas_h") or 0) > 0)
    ]
    if bad:
        return f"캔버스 없음({', '.join(bad)}): Coordinate.sourceWidth 는 gt=0 을 요구한다"
    if not ad_document.get("pages"):
        return "페이지 0개"
    return None


def _source_file_type(ad_document: dict[str, Any]) -> str:
    """대상 `service.py:70` 과 같은 규칙 — 파일명의 확장자만 소문자로.

    우리 `file_type` 은 png·jpg 를 "image" 로 뭉친다. 계약에는 원래 확장자가 더
    쓸모 있고, 대상 서비스들이 전부 확장자를 넣으므로 그쪽에 맞춘다.
    """
    name = str(ad_document.get("source_file") or "")
    if "." in name:
        return name.rsplit(".", 1)[-1].casefold()
    return str(ad_document.get("file_type") or "unknown").casefold()


def _warnings(stats: dict[str, int], non_ok_pages: list[str]) -> list[dict[str, Any]]:
    """계약 warnings.

    **우리 `notes` 를 통째로 옮기지 않는다.** 두 가지 이유다:
      · notes 는 진단 기록(타일 개수, 후보 판정 근거 등)이지 결함이 아니다. 그대로
        옮기면 리뷰어가 봐야 할 진짜 경고가 30여 줄 산문에 묻힌다.
      · 대상 라우터가 후보를 고를 때 `-len(document.warnings)` 로 **경고가 많을수록
        순위를 깎는다**(`routing.py` `_candidate_rank`). 진단을 경고로 부풀리면
        우리 결과가 다른 어댑터에 밀린다.
    그래서 **기계 판독 가능한 필드에서 유도되는 사실만** 경고로 낸다. 산문 진단은
    `out/json` 에 그대로 남아 있고 `rawArtifactRef` 가 그 위치를 가리킨다.
    """
    warnings: list[dict[str, Any]] = []
    if stats["dropped_empty_text"]:
        warnings.append(
            {
                "code": "TEXT_DETECTED_BUT_UNREADABLE",
                "message": (
                    f"검출됐으나 판독 실패 {stats['dropped_empty_text']}건 — 상자는 잡았고"
                    " 글자를 못 읽어 textBlock 으로 내보내지 않았다"
                ),
                "requiresReview": True,
            }
        )
    if stats["sweep_blocks"]:
        warnings.append(
            {
                "code": "COORDINATE_BAND_ONLY",
                "message": (
                    f"좌표가 가로 전체인 라인 {stats['sweep_blocks']}건 — 글자는 맞고 위치는"
                    f" 못 믿는다. coordinateConfidence={SWEEP_COORDINATE_CONFIDENCE} 로 표시"
                ),
                # 위치 불확실은 블록마다 coordinateConfidence 로 이미 표현되고 대상이
                # 그 값으로 LIST_ONLY 까지 떨어뜨린다. 문서 단위로 한 번 더 사람을
                # 부르면 같은 사실을 두 번 신고하는 셈이다.
                "requiresReview": False,
            }
        )
    if stats["clamped_coordinates"]:
        warnings.append(
            {
                "code": "COORDINATE_CLAMPED",
                "message": (
                    f"캔버스 밖이거나 크기 0 인 상자 {stats['clamped_coordinates']}건을"
                    " 캔버스 안으로 잘랐다"
                ),
                "requiresReview": True,
            }
        )
    if non_ok_pages:
        warnings.append(
            {
                "code": "PAGE_PARSE_NOT_OK",
                "message": f"파싱이 온전하지 않은 페이지: {', '.join(non_ok_pages)}",
                "requiresReview": True,
            }
        )
    return warnings
