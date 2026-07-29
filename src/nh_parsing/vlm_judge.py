from __future__ import annotations

"""VLM 기반 영역 역할 판정 + 필드 추출 — 설계서 6.5절 (판단 주체 = VLM).

원칙:
- 역할(유의사항/제목 등)과 필드(심의필/금리 등)의 판단은 VLM 이 한다.
  키워드 매칭·위치 규칙은 판단하지 않는다 (VLM 실패 시 폴백으로만 존재).
- VLM 은 새 값을 지어낼 수 없다: 필드 value 는 반드시 참조한 라인(line_index)의
  실제 텍스트에 존재해야 하며, 코드가 이를 검증해 ocr_backed 로 기록한다
  (이전 프로젝트 check_field_consistency 패턴).
- regex 는 보조 신호(regex_backed)로만 부착한다. 채택/폐기를 결정하지 않는다.
"""

import re

from PIL import Image

from .gemma_client import chat_json, image_part
from .ir import Line, Region, Section

ROLES = ["제목", "본문", "유의사항", "각주", "버튼", "고지문구", "표", "이미지", "기타"]

# 섹션 = 같은 목적을 가진 영역 묶음 (골드셋 평가의 기본 단위)
SECTION_TYPES = [
    "헤드라인",        # 상품명·캐치프레이즈·기간 등 상단 핵심 안내
    "상품안내",        # 금리/가입기간/대상 등 상품 스펙 설명
    "우대혜택",        # 우대금리·우대조건 안내
    "이벤트안내",      # 이벤트 소개·대상·경품 개요
    "참여방법",        # 참여 절차, 방법1/방법2, 앱 경로 안내
    "경품안내",        # 경품 내용·수량·지급일
    "당첨자안내",      # 당첨자 발표·통지 방법
    "이벤트유의사항",  # 이벤트 관련 fine-print
    "상품유의사항",    # 상품 관련 fine-print (예금자보호 등)
    "고지문구",        # 준법감시인 심의필 등 규정상 필수 표기
    "행동유도",        # 가입하기/둘러보기 버튼 등 CTA
    "장식예시",        # 앱 화면 예시, 지폐 그림 등 심의 대상 아닌 장식·예시 요소
    "기타",
]

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
                    "section_type": {"type": "string", "enum": SECTION_TYPES},
                    "section_no": {"type": "integer"},
                    "group_no": {"type": "integer"},
                    "confidence": {"type": "number"},
                },
                "required": ["region_id", "role", "section_type", "section_no",
                             "group_no", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "regions"],
    "additionalProperties": False,
}

_ROLE_PROMPT = """당신은 금융상품 광고심의 보조 시스템의 화면 구조 분석기입니다.
아래는 광고 화면에서 검출된 영역 목록입니다. 각 영역의 (1) 역할과 (2) 소속 섹션을 판정하세요.

[역할 정의]
- 제목: 상품명, 핵심 캐치프레이즈, 섹션 헤드라인
- 본문: 혜택·조건·참여방법 등 일반 설명 문구
- 유의사항: 소비자 보호 고지, 제한조건, 세금·해지·보호한도 안내 등 fine-print.
  "유의사항"이라는 단어가 없어도 내용이 고지·제한·경고 성격이면 유의사항입니다.
- 각주: 본문에 딸린 보충 설명 (*, ※ 등)
- 버튼: 가입하기/둘러보기 등 행동 유도(CTA)
- 고지문구: 준법감시인 심의필 번호, 광고 유효기간 등 규정상 필수 표기
- 표: 표 형태 정보 / 이미지: 텍스트가 거의 없는 그림·사진 영역 / 기타

[섹션 정의] — 섹션은 같은 목적을 가진 영역들의 묶음입니다:
헤드라인, 상품안내(금리·기간 등 스펙), 우대혜택, 이벤트안내, 참여방법,
경품안내, 당첨자안내, 이벤트유의사항, 상품유의사항, 고지문구(심의필 등),
행동유도(CTA), 장식예시(앱 화면 예시·지폐 그림 등 심의 대상이 아닌 장식), 기타

[섹션 부여 규칙]
1. 같은 섹션에 속한 영역들(섹션 제목 + 그 아래 본문들)은 **같은 section_type 과
   같은 section_no** 를 갖습니다. 섹션 제목 영역도 그 섹션에 포함시키세요.
2. 같은 타입의 섹션이 여러 개면 화면 위→아래 순서로 section_no 를 1, 2, ... 로
   구분하세요. (예: '이벤트1 유의사항' 블록들 = 이벤트유의사항 1,
   '이벤트2 유의사항' 블록들 = 이벤트유의사항 2)
3. 서로 멀리 떨어져 있고 내용도 이어지지 않는 별개 블록은 타입이 같아도
   section_no 를 나누세요. 시각적·의미적으로 연속된 묶음만 한 섹션입니다.
4. 앱 화면 예시 내부 텍스트, 지폐·경품 그림 속 숫자 등은 장식예시 섹션입니다.
5. '기타'는 최후의 수단입니다 — 내용이 조금이라도 특정 타입에 맞으면 그 타입을 쓰세요.

[group_no — 시각적 묶음]
페이지가 카드/패널/컬럼 같은 시각적 묶음(예: SNS 카드 여러 장, 좌우 배치 패널)으로
구성되어 있으면, 각 영역이 속한 묶음 번호를 좌→우, 위→아래 순서로 1부터 부여하세요.
서로 다른 묶음의 영역은 section_no 도 달라야 합니다.
묶음 구조가 없는 일반 페이지(단일 세로 흐름 등)는 모두 0 을 주세요.

판정 기준: 텍스트의 **의미**와 화면 내 위치(y_ratio: 0=최상단, 1=최하단)를 종합하세요.
특정 키워드 유무가 아니라 내용의 성격으로 판단하세요.
첨부 이미지는 전체 화면 축소본입니다(레이아웃·강조 참고용).

영역 목록:
{regions}

먼저 analysis 에 화면이 어떤 섹션들로 구성되는지 한두 문장으로 정리한 뒤,
모든 region_id 에 대해 하나씩 판정을 반환하세요."""


def judge_region_roles(
    regions: list[Region], canvas: Image.Image | None, canvas_h: int,
    card_by_region: dict[str, int] | None = None,
) -> list[Section]:
    """페이지의 모든 영역 역할 + 소속 섹션을 VLM 한 번의 호출로 판정.

    영역 role 은 in-place 반영하고, 섹션 목록을 반환한다.
    실패 시 예외를 올린다 — 폴백(규칙) 적용은 호출측(pipeline) 책임.

    card_by_region 이 주어지면(카드-분할 §D) 시각 묶음(group_no)을 VLM 눈대중 대신 그
    카드 배정으로 확정한다 — 개수부터 세고 배정한 전용 판정이라 더 신뢰(003 내용 섞임 방지).

    **미배정 라인 귀속을 여기 합치려다 되돌렸다(2026-07-28).** 호출은 4회 줄었지만
    (65→61) 프롬프트에 미배정 낱줄이 통째로 들어가 max_tokens 가 4320→6640 으로 뛰었고,
    가장 큰 문서(001, 영역 67 + 낱줄 58)에서 역할판정이 120초 타임아웃에 3회 연속 걸려
    **섹션이 0개**가 됐다(골드 섹션 37→29). 좌표 흡수가 먼저 대부분을 걷어낸 뒤 남은
    것만 물어야 하는데, 병합하면 섹션이 아직 없어서 그 필터를 못 쓴다 — 필터를 근사해도
    58→19 까지만 줄어 여전히 기준선보다 18% 크다. 이득 6.5% 대비 최대 문서 타임아웃
    위험이 커서 기각. judge_orphan_sections 를 그대로 쓴다.
    """
    judgeable = [r for r in regions if r.lines]  # 텍스트 없는 영역은 이미지/기타 유지
    if not judgeable:
        return []

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
    raw_groups: dict[tuple[str, int, int], list[Region]] = {}
    for r in judgeable:
        v = verdicts.get(r.region_id)
        if not v:
            continue
        r.role = v["role"]
        r.role_confidence = v.get("confidence")
        r.role_source = "vlm"
        if card_by_region is not None and r.region_id in card_by_region:
            gno = card_by_region[r.region_id]  # 카드-분할 결과를 시각 묶음으로 확정
        else:
            gno = max(0, int(v.get("group_no") or 0))
        key = (v["section_type"], max(1, int(v.get("section_no") or 1)), gno)
        raw_groups.setdefault(key, []).append(r)

    # 비연속 섹션 분할 — VLM 이 멀리 떨어진 동종 블록을 한 섹션으로 묶으면
    # bbox 가 무관한 영역까지 덮는다(올원 고지문구 4,000px 스팬 실측).
    # 수직 갭이 캔버스 비례 임계를 넘으면 나눈다 (범용 기하 규칙 — 특정 양식 가정 없음).
    gap_limit = max(300, int(canvas_h * 0.15)) if canvas_h else None
    split: list[tuple[str, int, list[Region]]] = []
    for (stype, _sno, gno), regs in raw_groups.items():
        boxed = sorted((r for r in regs if r.bbox), key=lambda r: r.bbox[1])
        unboxed = [r for r in regs if not r.bbox]
        if gap_limit is None or len(boxed) < 2:
            split.append((stype, gno, regs))
            continue
        parts: list[list[Region]] = [[boxed[0]]]
        for r in boxed[1:]:
            if r.bbox[1] - max(p.bbox[3] for p in parts[-1]) > gap_limit:
                parts.append([r])
            else:
                parts[-1].append(r)
        parts[0] = unboxed + parts[0]
        split.extend((stype, gno, part) for part in parts)

    # 시각 순서(위→아래, 좌→우)로 정렬 후 타입별 section_no 재부여
    def visual_key(regs: list[Region]) -> tuple[int, int]:
        boxes = [r.bbox for r in regs if r.bbox]
        if not boxes:
            return (0, 0)
        return (min(b[1] for b in boxes), min(b[0] for b in boxes))

    split.sort(key=lambda item: visual_key(item[2]))
    type_counter: dict[str, int] = {}
    sections: list[Section] = []
    for stype, gno, regs in split:
        type_counter[stype] = type_counter.get(stype, 0) + 1
        boxed = [r.bbox for r in regs if r.bbox]
        bbox = (
            [min(b[0] for b in boxed), min(b[1] for b in boxed),
             max(b[2] for b in boxed), max(b[3] for b in boxed)]
            if boxed else None
        )
        confs = [r.role_confidence for r in regs if r.role_confidence is not None]
        section = Section(
            section_id=f"s{len(sections):02d}",
            section_type=stype,
            section_no=type_counter[stype],
            group_no=gno or None,
            bbox=bbox,
            region_ids=[r.region_id for r in regs],
            confidence=round(sum(confs) / len(confs), 3) if confs else None,
        )
        for r in regs:
            r.section_id = section.section_id
        sections.append(section)

    return sections


# ──────────────────── 미배정 라인 섹션 귀속 (VLM 판정) ────────────────────
#
# judge_region_roles 는 StructureV3 가 만든 영역(region)만 본다. 그 영역 밖에서
# 검출된 낱줄(레이아웃 블록 미검출·섹션 경계 밖 fine-print 등)은 좌표 포함 검사
# (_absorb_unassigned_into_sections)로 1차 구제되지만, 어느 섹션 bbox 안에도
# 안 들어가는 라인(섹션 사이 갭·경계 근접)은 미배정으로 남는다. 이때 좌표가
# 아니라 **내용**으로 어느 섹션의 연장인지 VLM 에게 물어 판정한다.
#
# 판정 주체는 VLM(내용), 좌표 후보(cand)는 참고 힌트로만 제공한다. 최종 수용은
# 호출측(pipeline)이 좌표 정합 게이트로 한 번 더 교차검증한다 — "내용 판단 ×
# 좌표 정합" 두 신호가 함께 맞을 때만 귀속(사용자 요청: 두 신호를 비교해 결정).

_ORPHAN_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},  # 빈 배열 퇴행 방지 (부록 C-5)
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "orphan_index": {"type": "integer"},
                    "section_id": {"type": "string"},  # 기존 섹션 id 또는 "none"
                    "confidence": {"type": "number"},
                },
                "required": ["orphan_index", "section_id", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "assignments"],
    "additionalProperties": False,
}

_ORPHAN_PROMPT = """당신은 금융상품 광고 화면 구조 분석기입니다.
[기존 섹션]은 이미 확정된 의미 단위 묶음이고, [미배정 문구]는 아직 어느 섹션에도
들어가지 못한 낱줄들입니다. 각 미배정 문구가 어느 기존 섹션에 **내용상 이어지는지**
판정하세요.

규칙:
- 문구가 특정 섹션의 연장(같은 유의사항/안내가 이어지는 다음 줄 등)이면 그 section_id.
- 어느 섹션에도 이어지지 않는 완전히 독립적인 내용이면 section_id 를 "none" 으로.
- cand 는 좌표상 가장 가까운 섹션(참고용 힌트)일 뿐입니다. 위치가 가깝다는 이유만으로
  귀속하지 말고, 내용이 실제로 그 섹션의 연장인지로 판단하세요.
- 화면에서 서로 멀리 떨어진 섹션에는 억지로 붙이지 마세요. 이어지는 흐름이 아니면 "none".

[기존 섹션] (형식: [id] 타입#번호 (세로 y 0=상단~1=하단) 예시텍스트)
{sections}

[미배정 문구] (형식: #번호 (y위치) cand=좌표후보 "텍스트")
{orphans}

먼저 analysis 에 판단 근거를 한두 문장 정리한 뒤, 모든 orphan_index 에 대해 판정을 반환하세요."""


def _orphan_candidate(line_box: list[int], sections: list[Section]) -> str:
    """미배정 라인의 좌표상 최근접 섹션 id (수평 겹침 우선, 없으면 수직 최근접)."""
    lx0, ly0, lx1, ly1 = line_box
    lcx = (lx0 + lx1) / 2

    def vgap(sb: list[int]) -> float:
        if ly0 > sb[3]:
            return ly0 - sb[3]
        if ly1 < sb[1]:
            return sb[1] - ly1
        return 0.0

    boxed = [s for s in sections if s.bbox]
    if not boxed:
        return "none"
    overlap = [s for s in boxed if s.bbox[0] <= lcx <= s.bbox[2]]
    pool = overlap or boxed
    return min(pool, key=lambda s: vgap(s.bbox)).section_id


def judge_orphan_sections(
    orphans: list[Line], sections: list[Section], canvas: Image.Image | None, canvas_h: int
) -> dict[int, tuple[str, float | None]]:
    """미배정 라인 각각이 어느 기존 섹션의 연장인지 VLM 으로 판정한다.

    반환: {orphan 순번(0-base): (section_id 또는 "none", confidence)}.
    실패 시 예외를 올린다 — 폴백(미배정 유지)은 호출측 책임.
    """
    if not orphans or not sections:
        return {}

    def yr(v: int) -> float:
        return round(v / canvas_h, 2) if canvas_h else 0.0

    sec_lines = []
    for s in sections:
        excerpt = ""
        # 섹션 대표 텍스트는 호출측이 region 을 붙여 넘기지 않으므로 타입/번호로 식별.
        yspan = f"y {yr(s.bbox[1])}~{yr(s.bbox[3])}" if s.bbox else "y ?"
        sec_lines.append(f"- [{s.section_id}] {s.section_type}#{s.section_no} ({yspan})")

    orph_lines = []
    for i, l in enumerate(orphans):
        cand = _orphan_candidate(l.bbox, sections) if l.bbox else "none"
        y = yr(l.bbox[1]) if l.bbox else 0.0
        orph_lines.append(f'#{i} (y {y}) cand={cand} "{l.text[:80]}"')

    parts: list[dict] = [
        {"type": "text", "text": _ORPHAN_PROMPT.format(
            sections="\n".join(sec_lines), orphans="\n".join(orph_lines)
        )}
    ]
    if canvas is not None:
        parts.append(image_part(canvas))

    data = chat_json(
        parts,
        schema_name="orphan_sections",
        schema=_ORPHAN_SCHEMA,
        max_tokens=max(600, 40 * len(orphans) + 300),
    )
    out: dict[int, tuple[str, float | None]] = {}
    for a in data.get("assignments", []):
        try:
            idx = int(a["orphan_index"])
        except (KeyError, TypeError, ValueError):
            continue
        out[idx] = (str(a.get("section_id") or "none"), a.get("confidence"))
    return out
