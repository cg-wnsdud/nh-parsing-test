from __future__ import annotations

"""영역 구성 + 규칙 기반 초기 역할(폴백 전용) — 설계서 6.4절.

⚠ 역할 판정의 판단 주체는 VLM(vlm_judge.py)이다.
이 모듈의 키워드/위치 규칙은:
  1) VLM 호출 전의 초기값, 2) VLM 실패 시 폴백
으로만 쓰인다. nh-data 샘플에 맞춘 패턴이므로 일반화를 기대하지 말 것 —
regex 를 판단 로직으로 승격하려면 대량 데이터에서 공통 패턴 도출이 선행돼야 한다.

(regex 기반 필드 추출 extract_fields 는 필드가 STAGE_3 로 일원화되며 죽은 코드가
되어 2026-07-29 제거했다 — 죽은 코드 감사 참조. _REVIEW_NO/_RATE 정규식은
_refine_role 의 역할 판정 폴백으로 여전히 쓰여 남긴다.)
"""

import re

from .ir import Line, Region
from .paddlex_client import LayoutBlock

# PP-StructureV3 공식 block_label 전체 매핑 (공식 문서 + 실측 vision_footnote)
_LABEL_TO_ROLE = {
    "doc_title": "제목",
    "paragraph_title": "제목",
    "title": "제목",
    "text": "본문",
    "abstract": "본문",
    "table": "표",
    "table_title": "각주",
    "figure": "이미지",
    "image": "이미지",
    "chart": "이미지",
    "chart_title": "각주",
    "seal": "이미지",
    "header_image": "이미지",
    "footer_image": "이미지",
    "figure_title": "각주",
    "footnote": "각주",
    "vision_footnote": "각주",  # 실측에서 발견 (2026-07-16)
    "aside_text": "각주",
    "number": "기타",
    "page_number": "기타",
    "formula": "기타",
    "formula_number": "기타",
    "algorithm": "기타",
    "footer": "고지문구",
    "header": "고지문구",
}

_NOTICE_HEADER = re.compile(r"유의\s*사항|알아\s*두|꼭\s*확인|주의\s*사항")
_REVIEW_NO = re.compile(
    r"(준법\s*감시인?\s*)?심의필\s*[:：]?\s*제?\s*([0-9]{4}\s*[-–~]\s*[0-9O]+)"
)
_RATE = re.compile(
    r"(?:최고|최대|최저|기본|우대)\s*연?\s*[0-9]+(?:\.[0-9]+)?\s*%p?"
    r"|연\s*[0-9]+(?:\.[0-9]+)?\s*%p?"
)


def _center_inside(line_bbox: list[int], region_bbox: list[int]) -> bool:
    cx = (line_bbox[0] + line_bbox[2]) / 2
    cy = (line_bbox[1] + line_bbox[3]) / 2
    return region_bbox[0] <= cx <= region_bbox[2] and region_bbox[1] <= cy <= region_bbox[3]


def build_regions(
    blocks: list[LayoutBlock],
    lines: list[Line],
    canvas_h: int,
    page_no: int,
) -> tuple[list[Region], list[Line]]:
    """레이아웃 블록에 OCR/디지털 라인을 배정하고 역할을 부여한다.

    반환: (영역 목록, 어느 영역에도 못 들어간 라인)
    """
    # layout_det_res 중복 박스 제거: parsing_res_list 우선
    primary = [b for b in blocks if b.source == "parsing_res_list"]
    regions: list[Region] = []
    seen: set[tuple[int, ...]] = set()
    for i, block in enumerate(primary or blocks):
        key = tuple(block.bbox)
        if key in seen:
            continue
        seen.add(key)
        regions.append(
            Region(
                region_id=f"p{page_no}_r{i:03d}",
                bbox=block.bbox,
                label=block.label,
                layout_score=block.score,   # 엔진 확신도 — 판정엔 안 쓰고 진단용으로만 보관
                role=_LABEL_TO_ROLE.get(block.label.lower(), "본문"),
            )
        )

    unassigned: list[Line] = []
    for line in lines:
        target = None
        if line.bbox:
            for region in regions:
                if region.bbox and _center_inside(line.bbox, region.bbox):
                    target = region
                    break
        if target is not None:
            target.lines.append(line)
        else:
            unassigned.append(line)

    for region in regions:
        region.lines.sort(key=lambda l: (l.bbox[1] if l.bbox else 0, l.bbox[0] if l.bbox else 0))
        _refine_role(region, canvas_h)
    return regions, unassigned


def _refine_role(region: Region, canvas_h: int) -> None:
    """규칙 기반 초기/폴백 역할 — VLM 판정(vlm_judge)이 성공하면 덮어써진다.

    덮이기 전 값을 `role_rule` 에 스냅샷한다. 판정에는 안 쓴다 — VLM 이 성공하면
    규칙 판정이 흔적 없이 사라져 두 판정을 대조할 방법이 없었기 때문이다
    (walkthrough §9-⑤ 고도화 1단계: "근거를 먼저 만든다").
    """
    region.role_source = "rules"
    _apply_role_rules(region, canvas_h)
    region.role_rule = region.role
    region.role_rule_confidence = region.role_confidence


def _apply_role_rules(region: Region, canvas_h: int) -> None:
    text = region.text
    if _NOTICE_HEADER.search(text):
        region.role = "유의사항"
        region.role_confidence = 0.9
        return
    if _REVIEW_NO.search(text):
        region.role = "고지문구"
        region.role_confidence = 0.9
        return
    # 하단 20% + 본문 라벨 → 유의사항 후보 (깨알 fine-print 휴리스틱)
    if region.bbox and canvas_h and region.role == "본문":
        if region.bbox[1] > canvas_h * 0.8:
            region.role = "유의사항"
            region.role_confidence = 0.5
