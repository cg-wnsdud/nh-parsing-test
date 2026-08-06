"""VLM 기반 영역 역할 판정 + 필드 추출 — 설계서 6.5절 (판단 주체 = VLM).

원칙:
- 역할(유의사항/제목 등)과 필드(심의필/금리 등)의 판단은 VLM 이 한다.
  키워드 매칭·위치 규칙은 판단하지 않는다 (VLM 실패 시 폴백으로만 존재).
- VLM 은 새 값을 지어낼 수 없다: 필드 value 는 반드시 참조한 라인(line_index)의
  실제 텍스트에 존재해야 하며, 코드가 이를 검증해 ocr_backed 로 기록한다
  (이전 프로젝트 check_field_consistency 패턴).
- regex 는 보조 신호(regex_backed)로만 부착한다. 채택/폐기를 결정하지 않는다.
"""

from __future__ import annotations

import re

from PIL import Image

from .gemma_client import chat_json, image_part
from .ir import Region

ROLES = ["제목", "본문", "유의사항", "각주", "버튼", "고지문구", "표", "이미지", "기타"]

# 섹션(의미 묶음) 판정은 2026-08-03 파싱 파이프라인에서 제거했다.
#
# 이유 셋 — 전부 실측 근거가 있다:
#  1. 흔들린다. 같은 코드·같은 입력 2회 실행에서 섹션 구조가 4문서 중 3문서에서
#     달랐다(001 11↔10, 002 13↔12, 올원e 10↔9). 역할(role)도 79% 일치에 그쳤다.
#     그리고 이 단계에는 검산이 없었다 — 카드배정은 밀도 관측, 낱줄귀속은 좌표 게이트,
#     밴드통독은 관계 라벨로 교차검증하는데 여기만 VLM 답을 그대로 썼다.
#  2. 받는 쪽에 자리가 없다. 후속 시스템의 NormalizedDocument v1 은
#     문서→페이지→블록 3층이라 섹션 계층을 담을 곳이 없다. 흔들리는 것과
#     경계에서 버려지는 것이 같은 집합이었다.
#  3. 파싱의 일이 아니다. "이 화면이 헤드라인·경품안내·참여방법으로 구성된다"는
#     광고의 의미 해석이지 글자를 어떻게 읽었나가 아니다.
#
# role(위 ROLES 9종)은 남긴다 — 후속 계약의 LayoutBlock.blockType
# (TITLE/BODY/NOTICE/FOOTNOTE/BUTTON/IMAGE)과 거의 1:1이고, 시인성 검토가
# "고지문구가 어디에 있나"를 필요로 한다.
#
# ir.Section 모델과 AdPage.sections 필드는 남겨 두되 항상 빈 리스트다
# (검수 도구들이 조용히 비는 쪽이 크래시보다 낫다).

# 주의: strict json_schema(제약 디코딩)에서 배열이 스키마 선두에 있으면 모델이
# 최소 유효 출력(빈 배열)으로 조기 종료하는 퇴행이 실측됨 (2026-07-16, gemma-4-26b).
# 배열 앞에 analysis(선행 분석) 문자열 필드를 두면 해소된다.
_ROLE_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "region_id": {"type": "string"},
                    "role": {"type": "string", "enum": ROLES},
                    "confidence": {"type": "number"},
                },
                "required": ["region_id", "role", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "regions"],
    "additionalProperties": False,
}

_ROLE_PROMPT = """당신은 금융상품 광고심의 보조 시스템의 화면 구조 분석기입니다.
아래는 광고 화면에서 검출된 영역 목록입니다. 각 영역의 **역할**을 판정하세요.

[역할 정의]
- 제목: 상품명, 핵심 캐치프레이즈, 섹션 헤드라인
- 본문: 혜택·조건·참여방법 등 일반 설명 문구
- 유의사항: 소비자 보호 고지, 제한조건, 세금·해지·보호한도 안내 등 fine-print.
  "유의사항"이라는 단어가 없어도 내용이 고지·제한·경고 성격이면 유의사항입니다.
- 각주: 본문에 딸린 보충 설명 (*, ※ 등)
- 버튼: 가입하기/둘러보기 등 행동 유도(CTA)
- 고지문구: 준법감시인 심의필 번호, 광고 유효기간 등 규정상 필수 표기
- 표: 표 형태 정보 / 이미지: 텍스트가 거의 없는 그림·사진 영역 / 기타

판정 기준: 텍스트의 **의미**와 화면 내 위치(y_ratio: 0=최상단, 1=최하단)를 종합하세요.
특정 키워드 유무가 아니라 내용의 성격으로 판단하세요.
'기타'는 최후의 수단입니다 — 내용이 조금이라도 특정 역할에 맞으면 그 역할을 쓰세요.
첨부 이미지는 전체 화면 축소본입니다(레이아웃·강조 참고용).

영역 목록:
{regions}

먼저 analysis 에 화면이 대략 어떤 내용으로 구성되는지 한두 문장으로 정리한 뒤,
모든 region_id 에 대해 하나씩 역할을 반환하세요."""


def judge_region_roles(
    regions: list[Region], canvas: Image.Image | None, canvas_h: int,
) -> int:
    """페이지의 모든 영역 **역할**을 VLM 한 번의 호출로 판정 (in-place).

    반환: 역할이 반영된 영역 수. 실패 시 예외를 올린다 —
    폴백(regions._refine_role 규칙) 유지는 호출측(pipeline) 책임.

    섹션(의미 묶음) 판정은 2026-08-03 제거했다 (모듈 상단 주석 참조).
    그에 따라 이 함수가 하던 세 가지도 같이 사라졌다:
      - section_type/section_no/group_no 수집과 섹션 조립
      - 비연속 섹션 분할 (수직 갭 임계)
      - 시각 순서 정렬 후 타입별 section_no 재부여
    """
    judgeable = [r for r in regions if r.lines]  # 텍스트 없는 영역은 이미지/기타 유지
    if not judgeable:
        return 0

    listing_lines = []
    for r in judgeable:
        y_ratio = round(r.bbox[1] / canvas_h, 2) if (r.bbox and canvas_h) else "?"
        excerpt = " / ".join(l.text for l in r.lines[:4])[:160]
        listing_lines.append(f'- region_id={r.region_id} y_ratio={y_ratio} 텍스트: "{excerpt}"')

    parts: list[dict] = [
        {"type": "text", "text": _ROLE_PROMPT.format(regions="\n".join(listing_lines))}
    ]
    if canvas is not None:
        parts.append(image_part(canvas))

    data = chat_json(
        parts,
        schema_name="region_roles",
        schema=_ROLE_SCHEMA,
        max_tokens=max(1200, 60 * len(judgeable) + 300),
    )
    verdicts = {v["region_id"]: v for v in data.get("regions", [])}
    applied = 0
    for r in judgeable:
        v = verdicts.get(r.region_id)
        if not v:
            continue  # 판정이 안 온 영역은 규칙 폴백(_refine_role) 값을 유지한다
        r.role = v["role"]
        r.role_confidence = v.get("confidence")
        r.role_source = "vlm"
        applied += 1
    return applied
