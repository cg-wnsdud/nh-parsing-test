"""영역 구성 + 규칙 기반 초기 역할(폴백 전용) — 설계서 6.4절.

⚠ 역할 판정의 판단 주체는 VLM(vlm_judge.py)이다.
이 모듈의 키워드/위치 규칙은:
  1) VLM 호출 전의 초기값, 2) VLM 실패 시 폴백
으로만 쓰인다. nh-data 샘플에 맞춘 패턴이므로 일반화를 기대하지 말 것 —
regex 를 판단 로직으로 승격하려면 대량 데이터에서 공통 패턴 도출이 선행돼야 한다.

(regex 기반 필드 추출 extract_fields 는 필드가 STAGE_3 로 일원화되며 죽은 코드가
되어 2026-07-29 제거했다. `_REVIEW_NO` 정규식만 `_apply_role_rules` 의 역할 판정
폴백으로 남아 있다 — 금리 정규식 `_RATE` 는 부르는 곳이 없어 2026-08-06 제거했다.)
"""

from __future__ import annotations

import re

from .ir import Line, Region, RegionTable
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
# 2026-08-06 제거: `_RATE`(금리 표기 정규식). 모듈 상단 주석은 "_REVIEW_NO/_RATE 는
# _refine_role 의 폴백으로 여전히 쓰여 남긴다"고 적고 있었지만 실제로는 `_REVIEW_NO`
# 만 쓰였다(_apply_role_rules). 금리 정규식은 필드 추출이 STAGE_3 로 일원화된 뒤
# 부르는 곳이 없어졌다.


def _overlap_ratio(line_bbox: list[int], region_bbox: list[int]) -> float:
    """라인 bbox 가 영역 bbox 와 겹치는 면적 — 라인 자신의 면적 대비 비율(0~1).

    예전엔 라인 중심점이 영역 안에 있는가만 봤다. 표 칸이 한 줄로 합쳐진 경우
    (extract_digital_lines 참조) 중심점이 어느 칸에 찍히느냐에 따라 통짜 줄 전체가
    엉뚱한 영역에 붙었다(올원 p1_r006 실측: 오른쪽 칸 '가입금액100만원이상'이 왼쪽
    영역에 붙음). 겹침 비율로 재면 "이 줄의 대부분을 담은 영역"이 이기므로 같은
    상황에서도 더 안전하고, 가로 분리가 덜 된 잔여 케이스의 이중 방어가 된다.
    """
    ix0 = max(line_bbox[0], region_bbox[0])
    iy0 = max(line_bbox[1], region_bbox[1])
    ix1 = min(line_bbox[2], region_bbox[2])
    iy1 = min(line_bbox[3], region_bbox[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    line_area = max(1, (line_bbox[2] - line_bbox[0]) * (line_bbox[3] - line_bbox[1]))
    return inter / line_area


_MIN_REGION_OVERLAP = 0.5  # 라인 면적의 절반 이상이 겹쳐야 그 영역 소속으로 본다


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
                # 표 격자(PaddleX table_res_list). 없으면 None — 표가 아닌 영역이다.
                table=RegionTable(**block.table) if block.table else None,
            )
        )

    unassigned: list[Line] = []
    for line in lines:
        target = None
        if line.bbox:
            best_ratio, best_key = 0.0, (-1.0, 0)
            for region in regions:
                if not region.bbox:
                    continue
                ratio = _overlap_ratio(line.bbox, region.bbox)
                area = (region.bbox[2] - region.bbox[0]) * (region.bbox[3] - region.bbox[1])
                # 겹침비율이 같으면(중첩 영역이 라인을 둘 다 완전히 품는 경우) 더 작은
                # (구체적인) 영역을 우선한다 — _absorb_unassigned_into_regions 의 기존
                # 규칙("가장 작은 영역")과 같은 원칙이라 여기도 목록 순서에 안 흔들린다.
                key = (ratio, -area)
                if key > best_key:
                    best_key, target = key, region
            best_ratio = best_key[0]
            if best_ratio < _MIN_REGION_OVERLAP:
                target = None
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
