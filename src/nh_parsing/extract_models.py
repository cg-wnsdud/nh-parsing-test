from __future__ import annotations

"""STAGE_3 출력 계약 — pydantic 검증 경계.

**원래 계획이었다.** `docs/previous/schema-layer-plan_2026-07-23.md` §1(A):

    "출력은 response_format: {type: json_schema, strict: true}로 디코딩 강제 +
     pydantic 재검증. (HyundaiHS와 동일 골격)"

실제로는 strict json_schema 강제만 구현되고 pydantic 재검증은 빠진 채로 굳어졌다
(2026-07-30 확인 — extract.py 는 처음부터 끝까지 plain dict 다). 의도적으로 뺀 게
아니라 계획에 있던 걸 놓친 것이라, 여기서 완성한다.

**왜 파싱 계층(ir.py)과 다르게 두나.** ir.py 의 AdPage/Region 은 스키마가 코드에
고정돼 있어(Region 클래스 자체가 필드 목록) pydantic 모델이 자연스럽다. 여기(STAGE_3)는
필드 '집합'이 상품군·광고유형에 따라 런타임에 schemas/*.json 에서 조립된다 — 즉
**어떤 키가 존재하는지는 동적**이다. 그래서 필드마다 pydantic 클래스를 만들지 않고,
**칸 하나의 모양(shape)**만 고정한다: field_key 가 뭐든 그 값 옆에는 항상
{value, evidence, status, note}가 있어야 한다. 이 '칸의 모양은 고정, 칸의 개수만
동적'이라는 성질이 여기서 pydantic 이 여전히 맞는 이유다.

**두 지점에서 검증한다** (시스템 경계에서만 검증하라는 원칙):
1. chat_json() 이 돌려준 원값 — 외부(VLM 서버)가 strict 계약을 실제로 지켰는지
   재확인. guided decoding 은 강력하지만 100% 는 아니다(_repair_trailing_escape 가
   이미 잡아낸 서버측 결함 참조).
2. extract_document() 최종 반환 직전 — 내부 코드(여러 함수가 같은 result 딕셔너리를
   순서대로 돌아가며 채우는 구조)가 계약을 어겼는지 확인. 내부 계산 로직 자체는
   그대로 plain dict 로 두고(이미 44/44 로 검증된 로직을 다시 짜는 위험을 피함),
   경계에서만 pydantic 이 감시한다.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["found", "not_found", "uncertain"]
AbsenceKind = Literal["해당없음", "미표시", "확인필요", "판정제외"]
# "다른항목에서_처리됨"은 LLM 이 내는 값이 아니라 _reclassify_already_handled 가
# 사후에 붙이는 값이다(schema_pack.UNMAPPED_KIND 에는 없음) — 최종 결과엔 나타나므로
# 여기 포함해야 최종 검증(2번 경계)에서 오탐이 안 난다.
UnmappedKind = Literal["심의무관", "심의관련_필드없음", "다른항목에서_처리됨"]


class AbsenceInfo(BaseModel):
    """부재 4분류 판정 — applicability.classify_absence 가 코드로만 붙인다(LLM 무관)."""

    model_config = ConfigDict(extra="forbid")

    kind: AbsenceKind
    obligation: str
    rule: str
    detail: list[str] | None = None


class FieldCell(BaseModel):
    """필드 값 칸 하나 — schema_pack._fields_schema() 가 강제하는 모양과 정확히 대응.

    value/evidence/status/note 넷은 VLM 응답에 항상 있어야 한다(guided decoding 필수).
    나머지는 이후 코드 단계(_normalize_status, _score_evidence, classify_absences)가
    붙이는 사후 필드라 전부 Optional — 그래서 이 모델 하나로 ①원값 검증과 ②최종
    검증을 둘 다 커버한다(원값엔 사후 필드가 아직 없을 뿐, 있으면 안 되는 게 아님).
    """

    model_config = ConfigDict(extra="forbid")

    value: str | list[str]
    evidence: list[str]
    status: Status
    note: str = ""
    group: str = ""
    status_corrected: str | None = None
    absence: AbsenceInfo | None = None
    # 아래 셋은 _score_evidence 가 붙인다. evidence_missing 은 "있다/없다"가 아니라
    # **근거로 지목했는데 실제로 없는 region_id 목록**이다(실측: 리스트로 온다 —
    # bool 로 잘못 모델링했다가 실 데이터로 검증하며 잡음, 2026-07-30).
    evidence_score: float | None = None
    evidence_backed: bool | None = None
    evidence_missing: list[str] | None = None


class ObservationItem(BaseModel):
    """G4(위험표현) 관측 후보 하나 — 판정이 아니라 수집이므로 status/absence 가 없다.

    quote/evidence/why 는 VLM 원값. 나머지 셋은 FieldCell 과 마찬가지로 _score_evidence
    가 사후에 붙인다(관측도 근거 대조 대상이다 — extract.py::_verify_evidence 참조).
    """

    model_config = ConfigDict(extra="forbid")

    quote: str
    evidence: list[str]
    why: str
    evidence_score: float | None = None
    evidence_backed: bool | None = None
    evidence_missing: list[str] | None = None


class UnmappedItem(BaseModel):
    """스키마 어느 필드에도 안 맞은 심의 관련 문구 — 조용한 유실 방지 장치.

    text/evidence/kind/reason 은 VLM 원값(schema_pack._unmapped_schema). `group`
    (단수)이 아니라 `groups`(복수)인 이유 — _dedupe_unmapped 가 같은 문구를 여러
    호출그룹이 각각 올린 걸 하나로 합치면서 `group` 을 곧바로 `groups` 리스트로
    바꿔버린다. 그래서 최종 결과엔 `group` 이 남아 있을 수가 없다(원값에도 아직
    없고, dedup 을 거치면 groups 뿐이다) — 이 모델에 `group` 을 안 넣은 게 실수가
    아니라 실제 데이터 흐름과 맞춘 것이다.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    evidence: list[str]
    kind: UnmappedKind
    reason: str
    groups: list[str] = Field(default_factory=list)
    evidence_score: float | None = None
    evidence_backed: bool | None = None
    evidence_missing: list[str] | None = None


class ExtractError(BaseModel):
    """호출그룹 하나가 완전히 실패했을 때의 기록.

    **이게 남는다는 건 그 그룹의 필드 N개가 fields 딕셔너리에 키 자체가 없다는 뜻이다**
    (schema-explained.md §4 에서 지적한 빈틈). pydantic 이 이 실패 자체를 막지는
    못한다 — 재시도·에러 흐름은 제어 문제지 데이터 모양 문제가 아니다. 대신 여기서
    ExtractResult.errors 를 필수 필드로 못박아, 이 파일을 읽는 어떤 도구든 errors 를
    빼먹고 못 넘어가게 한다.
    """

    model_config = ConfigDict(extra="forbid")

    group: str
    error: str


class EventsPruned(BaseModel):
    """유령 이벤트(값이 하나도 없는 이벤트 배열 항목) 제거 기록 — prune_empty_events.

    실측(002): 모델이 event_count=1 이라 답해놓고 배열엔 2개를 보냈고 둘째는 텅
    비어 있었다. 그대로 두면 그 유령 이벤트의 필수 항목 전부가 '미표시(지적사항)'로
    잡혀 없는 지적을 만들어낸다. 이 기록은 무엇을 왜 버렸는지 남기는 용도다.
    """

    model_config = ConfigDict(extra="forbid")

    dropped_indexes: list[int]
    reason: str
    event_count_reported: int | None = None
    array_length: int


class ExtractResult(BaseModel):
    """extract_document() 의 최종 반환 계약 — out/extracted/*.json 이 이 모양이어야 한다.

    review_gaps/coverage/input_gap/unused_figures 는 전부 우리 코드가 계산한
    진단값이라(LLM 이 안 건드림) 내부 키까지 엄격히 고정하지 않는다 — 이 넷은
    "이 파이프라인이 스스로를 어떻게 채점하는가"의 사적인 형태라 계속 바뀔 수 있고,
    바깥에서 계약으로 의존할 대상이 아니다. 바깥에서 의존하는 핵심 계약은
    fields/events/observations/unmapped 네 개이고, 그건 위 모델들로 엄격히 고정한다.
    """

    model_config = ConfigDict(extra="forbid")

    document: str | None = None
    doc_id: str | None = None
    product_group: str | None = None
    ad_type: str | None = None
    schema_id: str
    schema_version: str
    overlays_applied: list[str] = Field(default_factory=list)

    fields: dict[str, FieldCell] = Field(default_factory=dict)
    observations: dict[str, list[ObservationItem]] = Field(default_factory=dict)
    events: list[dict[str, FieldCell]] = Field(default_factory=list)
    unmapped: list[UnmappedItem] = Field(default_factory=list)
    group_analysis: dict[str, str] = Field(default_factory=dict)
    errors: list[ExtractError] = Field(default_factory=list)

    event_count_reported: int | None = None
    events_pruned: EventsPruned | None = None
    review_gaps: dict[str, Any] = Field(default_factory=dict)
    input_gap: list[dict[str, Any]] = Field(default_factory=list)
    evidence_unbacked: list[str] = Field(default_factory=list)
    unused_figures: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    status_corrections: list[str] = Field(default_factory=list)
    elapsed_s: float | None = None
