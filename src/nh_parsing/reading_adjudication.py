# -*- coding: utf-8 -*-
"""기본 광고 파이프라인의 영역별 Reader → Judge shadow 단계.

기존 밴드 통독은 한 이미지 안의 여러 영역을 한 번에 읽어 비용은 낮지만, 결과가 이웃
영역까지 삼키는지 분리하기 어렵다. 이 모듈은 위험 신호가 있는 영역만 독립 crop으로
재판독한다. crop의 목표 bbox 밖 픽셀은 마스킹하고 목표를 파란 테두리로 표시한다.

중요: 이 모듈은 ``Line.text``를 절대 수정하지 않는다. OCR/PDF 정본, Reader, Judge
관측을 Region에 나란히 보존해 kl_parser의 통합 JSON을 받는 후속 단계가 판단하게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Literal

from PIL import Image, ImageDraw

from .config import SETTINGS
from .gemma_client import chat_json, image_part
from .ir import ReadingAdjudication, Region, TableVlmReading
from .truncation import DIVERGED, SAME, classify_reading, norm, numbers_differ


_READER_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "text": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["analysis", "text", "confidence"],
    "additionalProperties": False,
}

_READER_PROMPT = """첨부 이미지는 광고 문서의 레이아웃 영역 하나입니다.

파란 테두리 안의 글자만 원문 그대로 전사하세요. 테두리 바깥의 흰색 영역은 다른
레이아웃 영역이므로 읽거나 추측하지 마세요. 다른 OCR 결과나 정답 후보는 제공되지
않았습니다. 오직 픽셀만 보고 독립적으로 읽으세요.

- 숫자, 소수점, %, %p, 통화기호, 괄호, 줄바꿈을 보이는 그대로 보존하세요.
- 자연스럽게 고쳐 쓰거나 보이지 않는 내용을 보완하지 마세요.
- confidence는 0~1 사이 숫자입니다. 글자를 확인할 수 없으면 text를 빈 문자열로 두세요.
- analysis에는 영역의 구성만 한 문장으로 짧게 적으세요."""

_TABLE_READER_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "text": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "structure_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["analysis", "text", "rows", "confidence", "structure_confidence"],
    "additionalProperties": False,
}

_TABLE_READER_PROMPT = """첨부 이미지는 광고 문서의 표 영역 하나입니다.

파란 테두리 안의 표만 픽셀을 보고 전사하세요. 테두리 바깥의 흰색 영역은 다른
레이아웃 영역이므로 읽거나 추측하지 마세요. 다른 OCR 결과나 정답 후보는 제공되지
않았습니다.

- text에는 보이는 모든 표 문구를 위에서 아래, 왼쪽에서 오른쪽 순으로 전사하세요.
- rows에는 보이는 행을 위에서 아래 순서로 넣고, 각 행의 셀은 왼쪽에서 오른쪽 순서로
  문자열 배열에 넣으세요.
- 셀 경계가 확실하지 않으면 추측해 열을 만들지 말고 그 행 전체를 셀 하나로 넣으세요.
- 숫자, 소수점, %, %p, 통화기호, 괄호를 보이는 그대로 보존하세요.
- confidence는 글자 판독, structure_confidence는 행/셀 관계를 실제 픽셀로 구분한
  자신감입니다. 읽을 수 없으면 text와 rows를 비우세요.
- analysis에는 표의 구성만 한 문장으로 짧게 적으세요."""

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "decision": {
            "type": "string",
            "enum": ["candidate_a", "candidate_b", "merge", "uncertain"],
        },
        "text": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["analysis", "decision", "text", "confidence", "reason"],
    "additionalProperties": False,
}

_JUDGE_PROMPT = """첨부 이미지는 광고 문서의 동일한 한 영역입니다.
파란 테두리 안의 픽셀만 기준으로 두 독립 판독 후보를 비교하세요. 후보 출처는
의도적으로 숨겼습니다. 테두리 바깥의 흰색 영역은 무시하세요.

후보 A:
{candidate_a}

후보 B:
{candidate_b}

판정 규칙:
- 이미지와 정확히 일치하는 후보가 하나면 candidate_a 또는 candidate_b를 고르세요.
- 두 후보의 서로 다른 부분이 각각 이미지에서 확인될 때만 merge를 고르고 text에 완성본을 쓰세요.
- 흐린 숫자·잘린 경계처럼 픽셀만으로 우열을 확정할 수 없으면 uncertain을 고르세요.
- 숫자·소수점·%·%p·기간·금액은 문맥상 자연스러움으로 추측하지 마세요.
- candidate_a/b 선택 때 text를 새로 고쳐 쓰지 말고 선택한 후보를 그대로 반환하세요.
- uncertain이면 text는 빈 문자열이어야 합니다.
- analysis와 reason은 짧게 쓰세요."""

@dataclass(frozen=True)
class MaskedRegionCrop:
    """VLM에 전달한 이미지와 원본 좌표 계약.

    ``crop_bbox``는 원본 캔버스상 crop 위치, ``target_bbox``는 그 crop 내부에서 실제
    전사 대상인 영역 위치다. 후속 UI는 이 두 값을 사용해 같은 근거를 재현할 수 있다.
    """

    image: Image.Image
    crop_bbox: list[int]
    target_bbox: list[int]
    excluded_overlap_region_ids: list[str]


def _clip_box(box: list[int], width: int, height: int) -> list[int] | None:
    x0, y0, x1, y1 = (int(value) for value in box)
    x0, x1 = max(0, x0), min(width, x1)
    y0, y1 = max(0, y0), min(height, y1)
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None


def _mostly_inside(inner: list[int], outer: list[int], min_share: float = 0.9) -> bool:
    """``inner`` 면적 대부분이 ``outer`` 안에 있나 — 중첩 영역 제외 기준."""
    ix = max(0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = max(1, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return (ix * iy) / area >= min_share


def masked_region_crop(
    region: Region,
    canvas: Image.Image,
    regions: list[Region] | None = None,
) -> MaskedRegionCrop | None:
    """목표 밖과 목표 안의 독립 하위 영역을 마스킹한 Reader 입력을 만든다.

    StructureV3가 큰 제목/문단 bbox 안에 별도 라벨·버튼 영역을 중첩해 낼 수 있다.
    그 하위 영역은 목표 Region.lines의 정본에 없으므로, bbox 내부여도 Reader가 읽으면
    이웃 문구 혼입이다. 면적 90% 이상이 목표 안에 든 다른 텍스트 영역만 제외한다.
    """
    if not region.bbox or len(region.bbox) != 4:
        return None
    target = _clip_box(region.bbox, canvas.width, canvas.height)
    if target is None:
        return None

    padding = max(0, SETTINGS.region_crop_padding_px)
    crop_box = _clip_box(
        [target[0] - padding, target[1] - padding, target[2] + padding, target[3] + padding],
        canvas.width,
        canvas.height,
    )
    if crop_box is None:
        return None

    original = canvas.convert("RGB").crop(tuple(crop_box))
    target_local = [
        target[0] - crop_box[0], target[1] - crop_box[1],
        target[2] - crop_box[0], target[3] - crop_box[1],
    ]
    bleed = max(0, SETTINGS.region_mask_bleed_px)
    retained = _clip_box(
        [target_local[0] - bleed, target_local[1] - bleed,
         target_local[2] + bleed, target_local[3] + bleed],
        original.width,
        original.height,
    )
    if retained is None:
        return None

    # bbox 바깥 이웃 영역은 텍스트 자체를 볼 수 없게 하되, 글자 경계 clipping을 막는
    # 2px 안전 고리만 남긴다. 테두리는 VLM이 실제 판독 대상도 명시하게 한다.
    masked = Image.new("RGB", original.size, "white")
    masked.paste(original.crop(tuple(retained)), tuple(retained))

    excluded: list[str] = []
    for other in regions or []:
        if other.region_id == region.region_id or not other.bbox or not other.lines:
            continue
        other_box = _clip_box(other.bbox, canvas.width, canvas.height)
        if other_box is None or not _mostly_inside(other_box, target):
            continue
        # 하위 영역은 이웃 텍스트를 Reader가 읽는 것을 막기 위해 안전 고리까지 모두
        # 가린다. 목표 바깥 safety ring과 달리 여기서는 보존할 목표 글자가 아니다.
        other_local = _clip_box(
            [
                other_box[0] - crop_box[0] - bleed,
                other_box[1] - crop_box[1] - bleed,
                other_box[2] - crop_box[0] + bleed,
                other_box[3] - crop_box[1] + bleed,
            ],
            masked.width,
            masked.height,
        )
        if other_local is not None:
            ImageDraw.Draw(masked).rectangle(tuple(other_local), fill="white")
            excluded.append(other.region_id)

    draw = ImageDraw.Draw(masked)
    width = max(2, min(6, max(2, min(masked.size) // 80)))
    draw.rectangle(tuple(target_local), outline="#1565c0", width=width)
    return MaskedRegionCrop(masked, crop_box, target_local, excluded)


def _confidence_01(value: object) -> float | None:
    if value is None:
        return None
    confidence = float(value)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence가 0~1 범위를 벗어남: {confidence}")
    return confidence


def _canonical_source(region: Region) -> Literal["digital", "ocr", "hybrid"]:
    sources = {line.source for line in region.lines}
    if sources == {"digital"}:
        return "digital"
    if sources == {"ocr"}:
        return "ocr"
    return "hybrid"


def _is_table_region(region: Region) -> bool:
    """PaddleX 격자가 없어도 StructureV3 table 영역이면 표 Reader를 쓴다."""
    return region.label.casefold() == "table" or region.table is not None


def selection_reasons(region: Region, scope: str | None = None) -> list[str]:
    """StructureV3 텍스트 영역을 Reader에 보낼지와 그 근거를 판정한다.

    기본 ``all``은 역할·기존 밴드 후보와 무관하게 모든 텍스트/표 영역을 보낸다. 이것이
    이 브랜치의 비교 기준이다. ``targeted``는 비용 측정용 보조 모드일 뿐, role 9종이나
    넓은 밴드 후보를 다시 의존하지 않는다.
    """
    if not region.lines or region.is_illustrative or not region.bbox:
        return []
    active_scope = (scope or SETTINGS.region_reading_scope).strip().lower()
    if active_scope == "all":
        return ["all_structure_text_regions"]
    if active_scope != "targeted":
        raise ValueError(
            f"REGION_READING_SCOPE는 'targeted' 또는 'all'이어야 함: {active_scope!r}"
        )

    reasons: list[str] = []
    if _is_table_region(region):
        reasons.append("table_structure")
    if any(
        line.source == "ocr" and line.confidence is not None
        and line.confidence < SETTINGS.lowconf_reread_threshold
        for line in region.lines
    ):
        reasons.append("low_ocr_confidence")
    return reasons


def _read(
    crop: Image.Image,
    *,
    table: bool = False,
) -> tuple[str, float | None, TableVlmReading | None]:
    """일반 영역 또는 표 영역을 독립 판독한다.

    표에서는 PaddleX HTML/셀 결과를 전혀 참고하지 않는다. 같은 StructureV3 bbox의
    이미지와 표 전용 JSON 계약만 VLM에 전달한다.
    """
    if table:
        data = chat_json(
            [
                {"type": "text", "text": _TABLE_READER_PROMPT},
                image_part(crop, box=(1600, 2400), image_format="PNG"),
            ],
            schema_name="table_region_reader",
            schema=_TABLE_READER_SCHEMA,
            max_tokens=3000,
        )
        rows = [
            [str(cell).strip() for cell in row]
            for row in (data.get("rows") or [])
            if isinstance(row, list)
        ]
        text = str(data.get("text") or "").strip()
        if not text and rows:
            text = "\n".join("\t".join(row) for row in rows).strip()
        confidence = _confidence_01(data.get("confidence"))
        structure_confidence = _confidence_01(data.get("structure_confidence"))
        return text, confidence, TableVlmReading(
            text=text,
            rows=rows,
            confidence=confidence,
            structure_confidence=structure_confidence,
            status="observed" if text else "unreadable",
        )

    data = chat_json(
        [
            {"type": "text", "text": _READER_PROMPT},
            image_part(crop, box=(1600, 2400), image_format="PNG"),
        ],
        schema_name="region_reader",
        schema=_READER_SCHEMA,
        max_tokens=2400,
    )
    return str(data.get("text") or "").strip(), _confidence_01(data.get("confidence")), None


def _anonymous_candidates(region_id: str, canonical: str, reader: str) -> tuple[str, str, dict[str, str]]:
    """A/B 위치 편향을 줄이도록 안정적으로 후보 순서를 뒤집는다."""
    if sum(region_id.encode("utf-8")) % 2:
        return reader, canonical, {"candidate_a": "element_vlm", "candidate_b": "canonical"}
    return canonical, reader, {"candidate_a": "canonical", "candidate_b": "element_vlm"}


def _judge(crop: Image.Image, region_id: str, canonical: str, reader: str) -> tuple[
    str, str, float | None, str, str | None,
]:
    candidate_a, candidate_b, source_by_decision = _anonymous_candidates(region_id, canonical, reader)
    data = chat_json(
        [
            {
                "type": "text",
                "text": _JUDGE_PROMPT.format(candidate_a=candidate_a, candidate_b=candidate_b),
            },
            image_part(crop, box=(1600, 2400), image_format="PNG"),
        ],
        schema_name="region_reader_judge",
        schema=_JUDGE_SCHEMA,
        max_tokens=1200,
    )
    decision = str(data.get("decision") or "uncertain")
    confidence = _confidence_01(data.get("confidence"))
    reason = str(data.get("reason") or "").strip()
    if decision in source_by_decision:
        source = source_by_decision[decision]
        return decision, canonical if source == "canonical" else reader, confidence, reason, source
    if decision == "merge":
        return decision, str(data.get("text") or "").strip(), confidence, reason, "merged"
    return "uncertain", "", confidence, reason, None


def _safe_proposal(
    canonical: str,
    reader: str,
    relation: str,
    decision: str,
    proposed_source: str | None,
    confidence: float | None,
) -> tuple[bool, str]:
    """shadow 모드에서 확정 상태로 기록할 수 있는 유일한 경우를 제한한다."""
    if proposed_source == "canonical":
        return True, "Judge가 기존 정본 유지를 선택"
    if proposed_source is None or decision == "uncertain":
        return False, "Judge가 원본 픽셀로 우열을 확정하지 못함"
    # Reader/Judge는 같은 계열 모델이라 오독이 상관될 수 있다. 현 단계에서는 VLM 선택을
    # 정본으로 승격하지 않고 모두 uncertain으로 남긴다.
    if proposed_source == "merged" or decision == "merge":
        return False, "새 문자열 merge는 자동 채택하지 않음"
    if confidence is None or confidence < 0.90:
        return False, "Judge confidence 0.90 미만"
    if numbers_differ(canonical, reader):
        return False, "숫자·소수점·금액·기간 차이는 자동 채택하지 않음"
    if relation != DIVERGED:
        return False, f"후보 관계가 {relation}라 텍스트 증감 가능성이 있음"
    edits = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, norm(canonical), norm(reader)).get_opcodes()
        if tag != "equal"
    )
    if edits > 2:
        return False, f"국소 수정량 {edits}자 > 2자"
    return False, "VLM 선택은 shadow에서 정본으로 승격하지 않음"


def adjudicate_regions_shadow(
    regions: list[Region],
    canvas: Image.Image,
    *,
    max_regions: int | None = None,
    on_region_complete: Callable[[Region], None] | None = None,
) -> dict[str, int]:
    """선별한 영역만 Reader/Judge로 교차검증하고, 모든 결과를 Region에 보존한다."""
    stats = {
        "eligible": 0,
        "requested": 0,
        "agreed": 0,
        "judge_selected": 0,
        "uncertain": 0,
        "reader_failed": 0,
        "judge_failed": 0,
    }
    candidates = [
        (region, reasons)
        for region in regions
        if (reasons := selection_reasons(region)) and region.reading_adjudication is None
    ]
    stats["eligible"] = len(candidates)
    limit = SETTINGS.region_reader_max_per_page if max_regions is None else max_regions
    if limit > 0:
        candidates = candidates[:limit]

    def checkpoint(region: Region) -> None:
        if on_region_complete:
            on_region_complete(region)

    for region, reasons in candidates:
        masked = masked_region_crop(region, canvas, regions)
        if masked is None:
            continue
        stats["requested"] += 1
        canonical = region.text.strip()
        source = _canonical_source(region)
        common = {
            "crop_bbox": masked.crop_bbox,
            "target_bbox": masked.target_bbox,
            "excluded_overlap_region_ids": masked.excluded_overlap_region_ids,
            "selection_reasons": reasons,
            "canonical_source": source,
        }
        try:
            reader_text, reader_confidence, table_reading = _read(
                masked.image, table=_is_table_region(region),
            )
        except Exception as exc:  # noqa: BLE001 - one region must not stop a document
            stats["reader_failed"] += 1
            region.reading_adjudication = ReadingAdjudication(
                **common, status="reader_failed", error=str(exc),
            )
            checkpoint(region)
            continue

        region.element_vlm_reading = reader_text or None
        region.element_vlm_confidence = reader_confidence
        if table_reading is not None:
            region.table_vlm_reading = table_reading
        if not reader_text:
            stats["reader_failed"] += 1
            region.reading_adjudication = ReadingAdjudication(
                **common,
                reader_confidence=reader_confidence,
                status="reader_failed",
                error="Reader가 빈 텍스트를 반환",
            )
            checkpoint(region)
            continue

        relation = classify_reading(canonical, reader_text).kind
        if relation == SAME:
            stats["agreed"] += 1
            region.reading_adjudication = ReadingAdjudication(
                **common,
                reader_text=reader_text,
                reader_confidence=reader_confidence,
                relation=relation,
                status="agreed",
                proposed_text=canonical,
                proposed_source="canonical",
                confidence=reader_confidence,
                reason="정규화 문자와 중요 숫자가 일치해 Judge 호출 생략",
            )
            checkpoint(region)
            continue

        try:
            decision, proposed, confidence, reason, proposed_source = _judge(
                masked.image, region.region_id, canonical, reader_text,
            )
        except Exception as exc:  # noqa: BLE001 - one region must not stop a document
            stats["judge_failed"] += 1
            region.reading_adjudication = ReadingAdjudication(
                **common,
                reader_text=reader_text,
                reader_confidence=reader_confidence,
                relation=relation,
                status="judge_failed",
                error=str(exc),
            )
            checkpoint(region)
            continue

        safe, policy_reason = _safe_proposal(
            canonical, reader_text, relation, decision, proposed_source, confidence,
        )
        if not safe:
            stats["uncertain"] += 1
            region.reading_adjudication = ReadingAdjudication(
                **common,
                reader_text=reader_text,
                reader_confidence=reader_confidence,
                relation=relation,
                status="uncertain",
                judge_decision=decision if decision in {"candidate_a", "candidate_b", "merge", "uncertain"} else "uncertain",
                # shadow 정책 때문에 Region.lines에는 반영하지 않는다. 그래도 Judge가
                # 실제로 A/B 중 무엇을 픽셀 기준으로 골랐는지는 P1 근거에 남겨야 P2가
                # 범위가 정확히 일치하는 경우에만 제한적으로 시험할 수 있다.
                proposed_text=proposed or None,
                proposed_source=proposed_source,
                confidence=confidence,
                reason=(reason + " / " if reason else "") + "보수정책: " + policy_reason,
            )
            checkpoint(region)
            continue

        stats["judge_selected"] += 1
        region.reading_adjudication = ReadingAdjudication(
            **common,
            reader_text=reader_text,
            reader_confidence=reader_confidence,
            relation=relation,
            status="judge_selected",
            judge_decision=decision,  # type: ignore[arg-type]
            proposed_text=proposed,
            proposed_source=proposed_source,  # type: ignore[arg-type]
            confidence=confidence,
            reason=reason,
        )
        checkpoint(region)
    return stats
