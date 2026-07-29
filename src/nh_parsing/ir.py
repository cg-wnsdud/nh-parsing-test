from __future__ import annotations

"""AdPageIR — 광고물 파싱 산출물 스키마 (설계서 7절).

모든 bbox 는 [x0, y0, x1, y1] 원본 캔버스 픽셀 좌표.
HWP 디지털 추출처럼 캔버스가 없는 경우 bbox 는 None.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

Source = Literal["digital", "ocr", "vlm", "vlm_sweep", "vlm_region"]
ParseRoute = Literal["digital", "ocr", "hybrid"]
ParseStatus = Literal["ok", "partial", "unreadable"]


class Line(BaseModel):
    text: str
    bbox: Optional[list[int]] = None
    confidence: Optional[float] = None
    source: Source


class Region(BaseModel):
    region_id: str
    bbox: Optional[list[int]] = None
    label: str = "unknown"          # 엔진 원 라벨 (PP-Structure label 등)
    role: str = "본문"               # 제목|본문|유의사항|각주|버튼|고지문구|이미지|표|기타
    role_confidence: Optional[float] = None
    role_source: Optional[str] = None  # vlm | rules (VLM 실패 시 폴백)
    section_id: Optional[str] = None   # 소속 섹션 (AdPage.sections 참조)
    card_no: Optional[int] = None      # 카드-분할(§D): 1..N=카드(위→아래·좌→우), 0=페이지 공통(배너/헤더). 스크롤/미적용은 None
    is_illustrative: bool = False      # 예시/장식(앱화면 예시·지폐 그림 등) — 심의 대상 제외·보관 (2a)
    lines: list[Line] = Field(default_factory=list)
    # (레거시) A안 시절: 통독 clean text 가 lines 를 대체하고 OCR 은 여기로 강등됐음.
    # B안(현행)에서는 OCR/디지털이 lines 정본으로 남으므로 이 필드는 보통 비어 있다.
    ocr_lines: list[Line] = Field(default_factory=list)
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


class ExtractedField(BaseModel):
    key: str                        # 심의필번호|금리|우대금리|가입기간 ...
    value: str
    bbox: Optional[list[int]] = None
    confidence: Optional[float] = None
    source: Source = "ocr"
    extractor: Optional[str] = None      # vlm | vlm+crop(수치 재확인 교정) | regex(폴백)
    ocr_backed: Optional[bool] = None    # 값이 참조 라인 텍스트에 실재하는가 (환각 방지)
    ocr_score: Optional[float] = None    # 값 토큰 중 페이지 텍스트 발견 비율 (연속 신호)
    regex_backed: Optional[bool] = None  # 알려진 표기 패턴과 형태 일치 (보조 신호)
    crop_verified: Optional[bool] = None # 수치 필드 고해상 크롭 재확인 통과 여부 (6.4)
    obs_count: Optional[int] = None      # 복수 관측 중 이 값이 관측된 횟수 (득표수)


class Section(BaseModel):
    """의미 단위 섹션 — 같은 목적의 영역 묶음 (예: '이벤트1 유의사항' 전체).

    골드셋 평가의 기본 단위. bbox 는 소속 영역들의 합집합.
    """

    section_id: str
    section_type: str               # vlm_judge.SECTION_TYPES 참조
    section_no: int = 1             # 같은 타입 섹션이 여럿일 때 위→아래 순번
    group_no: Optional[int] = None  # 시각적 묶음(카드/패널/컬럼) 번호 — SNS 카드형 등.
                                    # 묶음 구조가 없는 페이지는 None (범용 계층, 특정 양식 가정 없음)
    bbox: Optional[list[int]] = None
    region_ids: list[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    source: str = "vlm"
    is_illustrative: bool = False   # 장식예시 섹션 — 심의 대상에서 격리(보관) (2a)


class AdPage(BaseModel):
    page_no: int
    canvas_w: int = 0
    canvas_h: int = 0
    dpi: Optional[int] = None       # PDF 렌더 시 기록. PNG 원본은 None
    parse_route: ParseRoute
    parse_status: ParseStatus = "ok"
    triage: Optional[dict] = None   # PDF 페이지 triage 근거 (감사 추적용)
    sections: list[Section] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)
    unassigned_lines: list[Line] = Field(default_factory=list)
    extracted_fields: list[ExtractedField] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AdDocument(BaseModel):
    doc_id: str
    source_file: str
    file_type: str                  # pdf|image|hwp|hwpx
    product_group: Optional[str] = None
    ad_type: Optional[str] = None
    category_source: Optional[str] = None  # filename|vlm|filename_and_vlm|vlm_overrode_filename
    classification_confidence: Optional[float] = None
    pages: list[AdPage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
