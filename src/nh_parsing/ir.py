"""AdPageIR — 광고물 파싱 산출물 스키마 (설계서 7절).

모든 bbox 는 [x0, y0, x1, y1] 원본 캔버스 픽셀 좌표.
HWP 디지털 추출처럼 캔버스가 없는 경우 bbox 는 None.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# 실제로 대입되는 값 셋뿐이다 (2026-08-06 out/json 전수: ocr 448 · digital 37 · vlm_sweep 15).
# 예전에는 "vlm"·"vlm_region" 도 있었는데 B안 전환(VLM 판독이 정본을 덮지 않고 후보로만
# 붙는다) 이후 라인 출처로 대입되는 코드가 없어졌다 — 지우기 전 전수 확인함.
Source = Literal["digital", "ocr", "vlm_sweep"]
ParseRoute = Literal["digital", "ocr", "hybrid"]
ParseStatus = Literal["ok", "partial", "unreadable"]


class Line(BaseModel):
    text: str
    bbox: Optional[list[int]] = None
    confidence: Optional[float] = None
    source: Source
    # 라인 단위 VLM 재판독 후보 — Region.vlm_reading 과 같은 원칙(B안)을 라인에도 적용한다.
    # 원칙은 원래 Region 에만 지켜지고 라인에서는 두 단계가 정본을 갈아치우고 있었다:
    # 스윕-OCR 중복 심판(pipeline)과 저신뢰 재판독(vlm_direct). 두 단계 다 비결정
    # (스윕이 회수하는 문구가 실행마다 다름)이라 정본이 실행마다 흔들렸다 — 같은 코드·같은
    # 입력 2회 실측(2026-08-03): 정본 484줄 중 2줄이 갈렸고 그 2줄이 전부 이 경로였다
    # (002 '이벤트 기간' ↔ '이벤트 기간內', 올원e '20.2%p:...' ↔ '② 0.2%p : ...').
    # 이제 정본은 그대로 두고 후보만 붙인다. 최종 텍스트 선택은 하류(STAGE_3/심의)가 한다.
    vlm_reading: Optional[str] = None
    vlm_reading_conf: Optional[float] = None
    vlm_reading_stage: Optional[str] = None  # sweep_dedupe | lowconf_reread


class Region(BaseModel):
    region_id: str
    bbox: Optional[list[int]] = None
    label: str = "unknown"          # 엔진 원 라벨 (PP-Structure label 등)
    # StructureV3 가 이 블록 판정에 붙인 확신도(LayoutBlock.score). 판정에는 안 쓴다 —
    # 역할 판정이 갈릴 때 "레이아웃 엔진도 확신이 없던 자리인가"를 보려고 남긴다.
    # 2026-08-06 이전에는 받을 칸이 없어 파싱 직후 사라졌다 (walkthrough §9-⑤).
    layout_score: Optional[float] = None
    role: str = "본문"               # 제목|본문|유의사항|각주|버튼|고지문구|이미지|표|기타
    role_confidence: Optional[float] = None
    role_source: Optional[str] = None  # vlm | rules (VLM 실패 시 폴백)
    # 규칙(_refine_role)이 내린 판정 — VLM 이 role 을 통째로 덮기 **전** 값이다.
    # 판정에는 안 쓴다. 지금까지는 VLM 이 성공하면 규칙 판정이 흔적 없이 사라져
    # "규칙과 VLM 이 어디서 몇 건 갈리는가"를 잴 수가 없었다(총계 97.3% 일치만 알았다).
    # 이 두 필드가 그 측정의 재료다 — walkthrough §9-⑤ 고도화 1단계.
    role_rule: Optional[str] = None
    role_rule_confidence: Optional[float] = None
    card_no: Optional[int] = None      # 카드-분할(§D): 1..N=카드(위→아래·좌→우), 0=페이지 공통(배너/헤더). 스크롤/미적용은 None
    # 예시/장식(앱화면 예시·지폐 그림 등) 격리용. ⚠️ **어디서도 True 로 대입되지 않는다** —
    # 2026-08-03 섹션 제거 때 설정 코드가 같이 빠졌다. 읽는 곳 3군데(vlm_direct·pipeline·
    # applicability)가 전부 항상 False 로 돈다. 되살릴 조건은 walkthrough §9-⑥ 참조.
    is_illustrative: bool = False
    lines: list[Line] = Field(default_factory=list)
    # 영역별 VLM 통독(§6) — B안: OCR 정본을 덮어쓰지 않고 '후보'로만 보존한다. VLM 이
    # 이 영역 크롭을 통독한 clean text(회전·장식 교정 포함)이며, ocr_score(정밀도)로
    # OCR 과 교차검증된다. 최종 텍스트 선택은 judge/STAGE_3(스키마)가 수행 — 파싱 단계는
    # 정본(OCR)과 후보(vlm_reading)를 둘 다 남길 뿐 조용히 대체하지 않는다.
    vlm_reading: Optional[str] = None
    vlm_reading_score: Optional[float] = None   # 통독 후보의 OCR 대조 정밀도 (0~1)
    vlm_reading_coverage: Optional[float] = None # 통독 후보의 OCR 내용 커버리지 (재현율, 0~1)
    # 정본 대비 후보의 관계 (truncation.classify_reading). 위 두 점수는 토큰 겹침이라
    # 순서를 못 봐서 '뒤가 잘린 판독'이 정밀도 만점을 받는다 — 그 사각을 메우는 라벨.
    # same | tail_cut | head_drop | expanded | diverged
    vlm_reading_relation: Optional[str] = None

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


# 2026-08-06 제거: `ExtractedField` 와 `Section` 모델, `AdPage.extracted_fields` ·
# `AdPage.sections` 필드.
#
#   · Section       — 섹션(의미 묶음) 생성을 2026-08-03 에 파이프라인에서 없앴다.
#                     그 뒤로 항상 빈 리스트였고 채우는 코드가 없다.
#   · ExtractedField — 필드 추출은 STAGE_3(out/extracted)가 단일 창구다. 파싱 단계는
#                     필드를 안 뽑으므로 이 모델도 항상 빈 리스트였다.
#
# 산출물(out/json)에서 두 키가 사라진다. 읽던 곳은 러너의 진단 출력(항상 아무것도 안
# 찍혔다)과 삭제된 도구 둘(dump_text·make_gold_draft)뿐이었다. 되살릴 일이 생기면
# git 이력에 남아 있다.


class AdPage(BaseModel):
    page_no: int
    canvas_w: int = 0
    canvas_h: int = 0
    dpi: Optional[int] = None       # PDF 렌더 시 기록. PNG 원본은 None
    parse_route: ParseRoute
    parse_status: ParseStatus = "ok"
    triage: Optional[dict] = None   # PDF 페이지 triage 근거 (감사 추적용)
    regions: list[Region] = Field(default_factory=list)
    unassigned_lines: list[Line] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AdDocument(BaseModel):
    doc_id: str
    source_file: str
    file_type: str                  # pdf|image|hwp|hwpx
    product_group: Optional[str] = None
    ad_type: Optional[str] = None
    # 분류가 어느 경로로 정해졌나 (gemma_client.classify 주석 참조). prior 와 VLM 이
    # 둘 다 있을 때: filename_and_vlm(합의) | vlm_overrode_filename(충돌, VLM 채택).
    # VLM 만: vlm | vlm_abstained. 파일명만: filename_vlm_abstained(VLM 이 반대) |
    # filename_vlm_failed(호출 실패) | filename_no_vlm(HWP — 안 부름). 없으면 none.
    category_source: Optional[str] = None
    classification_confidence: Optional[float] = None
    pages: list[AdPage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
