# 스키마 내부 구조 설명 — 왜 이렇게 짰고, 검증 없이 안전한가

> `handoff.md`가 팀 회의용(무엇을 정할지)이라면, 이 문서는 **본인 학습용**이다.
> 왜 그룹 이름이 영어인지, 이벤트 스키마가 왜 배열인지, pydantic 모델 없이
> LLM 출력을 그대로 믿어도 되는지 — 코드 근거를 들어 답한다.
> 기준일 2026-07-29.

---

## 1. `field_key`는 왜 영어이고 `label`은 왜 한글인가

`schemas/예금성.json`을 보면 필드마다 이렇게 되어 있다:

```json
{
  "field_key": "eligibility",
  "label": "가입대상·가입조건",
  ...
}
```

**`field_key`는 사람이 읽는 이름이 아니라 JSON의 키(key) 그 자체다.** [schema_pack.py:110](../src/nh_parsing/schema_pack.py#L110)을 보면:

```python
props[f["field_key"]] = { "type": "object", "properties": {...} }
```

`field_key`가 그대로 `response_schema()`가 만드는 JSON Schema의 **property 이름**이 되고, 그 스키마가 vLLM에 `strict: true`로 강제된다. 즉 LLM이 실제로 뱉는 JSON이 `{"fields": {"eligibility": {...}}}` 모양이 된다. 이걸 한글로 하면:

- JSON 키에 한글이 들어가면 인코딩·이스케이프 문제가 생기기 쉽고
- Python 코드(`extract.py`, `applicability.py`)에서 `val["eligibility"]`처럼 문자열 리터럴로 계속 참조하는데, 영어가 오타·자동완성에 유리하고
- 무엇보다 **`field_key`는 코드와 스키마 사이의 "계약"**이라 프로그래밍 언어 식별자 규칙(공백·특수문자 없음)을 따라야 조작하기 쉽다

반면 `label`은 **사람이 읽을 때만** 쓰인다 — `make_review.py`(검수 화면), `applicability.py`의 사람용 메시지 등. LLM에게 보내는 프롬프트에는 `label`도 같이 들어가서 "이 필드는 `가입대상·가입조건`을 말한다"고 알려주지만, LLM이 **응답할 때 쓰는 키는 `field_key`(영어)**다.

정리하면: **`field_key` = 기계 계약, `label` = 사람 설명.** 하나의 필드가 두 이름을 갖는 건 중복이 아니라 역할 분리다.

---

## 2. 그룹을 왜 이렇게 나눴나 — G1~G4 + 오버레이

`schemas/예금성.json`의 4개 그룹, `_overlay_이벤트.json`의 1개 그룹, 이렇게 5개가 있다. 각각 "왜 이 필드들이 여기 같이 있는가"를 다르게 답해야 한다.

### G1_상품기본 (8필드) — "이 상품이 뭔지"

`product_subtype, product_name, eligibility, contract_period, deposit_amount, installment_type, deposit_kind, tax_benefit`

**공통점: 상품 그 자체를 식별하는 정보.** 특히 `product_subtype`(예금/적금/입출금/ELD)이 이 그룹의 첫 필드인 이유가 있다 — 이 값이 **다른 모든 그룹의 조건부 판정 근거**로 쓰인다(§4 참조). G1이 먼저 채워져야 G2의 "만기 개념이 없는 수시입출식엔 성립 안 함" 같은 판정이 가능해진다. 순서가 우연이 아니다.

### G2_금리 (14필드) — "돈이 얼마나 붙는지"

가장 필드가 많다. 이유: 금융광고 심의에서 **금리 표시가 가장 자주, 가장 정교하게 규제되는 항목**이기 때문이다(최고금리 표시 시 기본금리 병기 의무, 우대조건 명시 의무 등). `rate_mentions`(수집형)이 마지막에 있는데, 이건 "판정용 값"이 아니라 **전수 텍스트 수집**이다 — 광고에 금리 표기가 여러 번 나오면(본문에 한 번, 각주에 한 번) 그 모든 표기를 다 담아서, 서로 다른 표기가 있었다는 사실 자체를 남긴다. 이게 왜 필요한지는 §5(`_unused_figures`)에서 다시 나온다.

### G3_의무고지 (15필드) — "법이 반드시 있으라고 한 문구"

`review_stamp`(심의필번호), `depositor_protection`(예금자보호), `right_to_explanation`(설명받을권리) 등 — **표시 여부 자체가 심의 대상인 정형 문구들.** 이 그룹의 필드 대부분이 `obligation: 필수`이고, 값이 없으면 대부분 `미표시`(진짜 지적)로 귀결된다. G4(관측)와 성격이 정반대다 — G3는 "있어야 하는데 없나"를 보고, G4는 "있는데 문제인가"를 본다.

### G4_위험표현 (9필드) — "판정하지 않는 유일한 그룹"

`obs_definitive_expression`(단정적 표현), `obs_superlative`(근거 없는 최상급) 등 9종. **이름이 전부 `obs_`로 시작**하는 게 의도적이다 — observation(관측)이지 field(값)가 아니라는 표시다. `schema_pack.py`도 이 그룹만 다른 JSON 스키마를 만든다:

```python
def response_schema(group: dict) -> dict:
    if group.get("observation_fields"):   # G4만 이 분기
        return _observation_schema(group)
```

`_observation_schema()`는 값 하나(`value`)가 아니라 **배열**(`quote`, `evidence`, `why`가 담긴 여러 개)을 반환한다 — "단정적 표현이 몇 번 나왔는지 다 모아라"는 뜻이지 "단정적 표현이 있다/없다"를 판정하는 게 아니다. 판정은 다음 단계(심의) 몫이라는 걸 스키마 구조 자체가 강제한다.

### G5_이벤트 (오버레이, 11필드) — "조건부로만 등장하는 그룹"

`_overlay_이벤트.json`은 별도 파일이고, `applies_to.ad_type: ["이벤트페이지"]`일 때만 [`load_pack()`](../src/nh_parsing/schema_pack.py#L37)이 G1~G4에 이 그룹을 더한다:

```python
for path in sorted(SCHEMA_DIR.glob("_overlay_*.json")):
    ov = _load(path)
    if want_ad and (ad_type not in want_ad):
        continue          # 이벤트페이지 아니면 이 그룹 통째로 스킵
    groups.extend(ov.get("call_groups", []))
```

**왜 파일을 따로 뺐나.** 예금성 스키마와 이벤트 스키마는 독립적으로 바뀔 수 있다 — 이벤트 규정만 바뀌었는데 예금성 파일 전체를 건드리면 diff가 지저분해지고, 나중에 "대출성+이벤트" 조합이 생겨도 이 파일 하나만 재사용하면 된다. 오버레이 메커니즘 자체가 **"광고유형은 상품군과 직교(orthogonal)한다"**는 설계 판단(§1, `handoff.md`)을 코드로 구현한 것이다.

### 왜 이벤트는 배열(`events: [...]`)인가

G5는 `cardinality: list_of_events`라는 속성이 있고, `schema_pack.py`가 이걸 보고 세 번째 형태(`_event_schema`)를 만든다:

```python
def _event_schema(group: dict) -> dict:
    return { "properties": {
        "event_count": {"type": "integer"},
        "events": { "type": "array", "items": {...} },
    }}
```

이유는 실측 때문이다 — 003 광고 하나에 EVENT 1과 EVENT 2가 **같은 화면에 동시에** 있었다("추첨을 통해 총 1,000명" vs "게시물 공유하고 3만원권"). 이걸 고정 필드(`event_name`, `event_period`...) 하나씩으로 만들면 이벤트가 2개일 때 담을 자리가 없다. 배열로 만들어야 "이벤트가 몇 개든" 각각 독립된 `event_period`·`event_prize`를 가질 수 있다. `event_count`를 같이 요구하는 이유는 `extract.py`의 `prune_empty_events`가 "LLM이 2개라고 선언했는데 실제로 2번째가 텅 비어 있다"를 잡아내기 위한 자기 검증용 숫자다(§5에서 실측 사례로 다시 나온다).

---

## 3. 값 하나의 실제 형태 — 왜 `value` 말고 `status`·`evidence`·`note`가 붙나

```json
"eligibility": {
  "value": "개인(1인 1계좌)",
  "evidence": ["p1_r001", "p1_r004"],
  "status": "found",
  "note": ""
}
```

네 칸이 전부 [`_fields_schema()`](../src/nh_parsing/schema_pack.py#L107)에서 `required`로 강제된다 — 넷 다 없으면 그 응답 자체가 거부된다(§4). 각각의 역할:

- **`value`** — 실제 값
- **`status`** — `found`/`not_found`/`uncertain`. **값이 있어도 없어도 반드시 셋 중 하나를 골라야 한다.** `null`을 허용하지 않는 이유는 아래 §4에.
- **`evidence`** — 이 값을 어느 영역(`region_id`)에서 봤는지. 근거 없이 값만 있으면 검증이 불가능하다.
- **`note`** — LLM이 자유 텍스트로 남기는 설명(왜 이 값을 골랐는지, 애매한 점 등). 심의 담당자가 읽는 용도.

---

## 4. Pydantic — 원래 계획에 있었고, 지금은 구현돼 있다

**2026-07-30 갱신.** 이 절은 원래 "pydantic이 없는데 왜 괜찮은가"였는데, 확인해보니
질문 자체가 틀렸다. `docs/previous/schema-layer-plan_2026-07-23.md` §1(A)를 보면:

> *"출력은 response_format: {type: json_schema, strict: true}로 디코딩 강제 +
> **pydantic 재검증**. (HyundaiHS와 동일 골격)"*

**pydantic 재검증은 처음부터 계획에 있었다.** 그런데 실제 `extract.py`는 strict
디코딩 강제만 구현되고 pydantic 재검증은 빠진 채 완성됐다 — 의도적으로 뺀 게
아니라, 계획에 있던 절반이 구현 중에 누락된 것이다. 이번에 그 누락분을
[extract_models.py](../src/nh_parsing/extract_models.py)로 채웠다.

**이제 뭐가 방어하는지, 4겹으로 나눠서 본다** — ①②는 여전히 pydantic이 아닌
다른 방식(더 강한 방식)이고, ③④가 이번에 새로 pydantic으로 채워진 자리다.

### ① "모양(shape)이 틀릴 수 있는가" — vLLM이 생성 단계에서 막는다

pydantic은 보통 "응답을 받은 뒤에" 검증한다. 여기는 다르다 — [gemma_client.py:92](../src/nh_parsing/gemma_client.py#L92)에서:

```python
"response_format": {
    "type": "json_schema",
    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
}
```

`strict: True`는 **guided decoding**이라, LLM이 토큰을 한 개씩 생성하는 그 순간에 서버가 "이 다음 토큰이 스키마를 어길 수 있으면 아예 후보에서 제외"한다. 그래서 `additionalProperties: false`·`required: [...]`가 걸린 필드는 **애초에 어길 수 있는 응답 자체가 생성되지 않는다.** 이건 pydantic의 "사후 검증"보다 강한 보장이다 — 사후 검증은 "틀린 걸 발견"하지만 guided decoding은 "틀리게 만들 수가 없다."

**단, 여기엔 한계가 있다** — 스키마가 강제하는 건 "모양"(타입·필수키)이지 "내용의 진실성"(값이 원문에 실재하는지, 논리적으로 말이 되는지)이 아니다. pydantic도 이건 못 잡는다. 그래서 ②~④가 필요하다.

### ② JSON 자체가 깨지면? — 파싱 실패 + 재시도 + 알려진 결함 보정

guided decoding이 "스키마를 어기는 토큰"은 막아도, **서버가 응답을 중간에 끊거나 이상한 문자를 섞어 보내는 건 못 막는다.** 실제로 겪은 결함이 있다 — [gemma_client.py:124](../src/nh_parsing/gemma_client.py#L124) `_repair_trailing_escape`:

> *"문자열을 닫기 직전 불필요한 역슬래시를 내보내고 그대로 생성을 멈추는 경우가 실측됨...
> 끝에서 고정 길이를 자르는 방식은 통하지 않아 텍스트 전체에서 마지막 역슬래시
> 하나만 제거해본다."*

이건 pydantic이 해줄 수 없는 종류의 문제다(pydantic은 애초에 유효한 JSON을 받았다는 전제에서 시작). 여기서는:
1. JSON 파싱 시도
2. 실패하면 알려진 결함 패턴으로 복구 시도
3. 그래도 실패하면 예외
4. **예외면 2회까지 재시도**([gemma_client.py:60](../src/nh_parsing/gemma_client.py#L60) `retries: int = 2`)
5. 그래도 실패하면 `RuntimeError`를 던지고, 이걸 `extract.py`가 잡아서 **그 호출그룹의 errors에 기록하고 다음 그룹으로 진행**한다([extract.py:276](../src/nh_parsing/extract.py#L276)):

```python
except Exception as exc:
    result["errors"].append({"group": gid, "error": str(exc)})
    continue
```

**여기는 pydantic으로도 못 막는 빈틈이다.** 한 그룹이 완전히 실패하면, 그 그룹의 필드 N개는 `result["fields"]`에 **키 자체가 안 생긴다.** `absence` 4분류(§5)는 `result["fields"]`에 있는 것만 훑으므로, 통째로 실패한 그룹의 필드는 `absence` 판정도 못 받고 그냥 **최종 JSON에 안 나타난다.** 유일한 흔적은 `result["errors"]`뿐이다 — 이건 데이터 모양 문제가 아니라 제어 흐름(재시도·에러 처리) 문제라 pydantic의 관할이 아니다.

`extract_models.py`의 `ExtractResult.errors`는 `list[ExtractError]`로 **필수 필드**다 — `errors` 키 자체가 없는 결과는 이제 통과할 수 없다(빈 리스트는 되지만, 키 부재는 안 됨). 최소한 "이 파일을 읽는 도구가 `errors`를 확인 안 하고 넘어갈 수 없게" 만든 것이다. 다만 `tools/verify_extract.py`가 실제로 `errors`를 들여다보는 로직은 아직 없다 — **구조적으로 존재가 강제됐을 뿐, 아직 활용되진 않는다.** 다음으로 손볼 항목이다.

### ③-b pydantic이 새로 막는 것 — 서버가 계약을 어겼을 때

경계 두 곳에서 검증한다([extract_models.py](../src/nh_parsing/extract_models.py)):

1. **`chat_json()` 원값 직후** — `_validate_group_response()`가 `FieldCell`/`ObservationItem`/`UnmappedItem`으로 즉시 검증한다. guided decoding이 강력하지만 100%는 아니라는 전제(위 ②의 `_repair_trailing_escape` 결함이 그 증거)를 실제로 감시하는 자리다. 걸리면 `chat_json` 실패와 똑같이 취급돼 **그 그룹 전체가 스킵**되고(절반만 신뢰하는 것보다 안전) `errors`에 기록된다.
2. **`extract_document()` 최종 반환 직전** — `ExtractResult.model_validate(result)`. 이건 서버가 아니라 **우리 코드**를 감시한다. 15개 함수가 같은 `result` 딕셔너리를 순서대로 돌아가며 채우는 구조라, 그중 하나가 계약과 다른 모양을 만들면(예: 새 기능 추가하며 필드에 키를 잘못 붙임) 여기서 걸린다. **잡히면 예외를 그대로 올린다** — catch하지 않는다. 틀린 모양을 파일로 써서 RAG/DB 팀에 넘기는 것보다, 여기서 멈추는 게 낫다.

**실제로 모델을 두 번 고쳤다.** 처음 짤 때 `evidence_missing`을 bool로, `unmapped`의 소속 그룹을 `group`(단수)으로 모델링했는데, 실제 4문서 실행에서 즉시 `ValidationError`가 났다 — `evidence_missing`은 실제로 region_id **리스트**였고, `_dedupe_unmapped`가 `group`을 `groups`(복수)로 바꿔버리는 걸 놓쳤었다. **이게 이 경계가 하는 일을 그대로 보여주는 사례다** — 코드와 스키마가 실제로 다르면, 조용히 넘어가지 않고 그 자리에서 멈춘다.

### ③ 모양은 맞는데 내용이 모순되면? — 코드가 자체 교정

`status`가 "값 있음"인데 `not_found`라고 오는 경우가 실측됐다([extract.py:175](../src/nh_parsing/extract.py#L175) `_normalize_status`):

> *"값은 정확히 뽑았는데 status 만 not_found 로 붙여 보내는 경우가 있다(G2 금리 그룹 전체)."*

```python
if has_value and st == "not_found":
    val["status"] = "found"
    reasons.append("값이 있는데 not_found 로 와서 found 로 보정")
```

**조용히 고치지 않는다** — `status_corrected` 필드에 왜 고쳤는지 남기고, 문서 전체의 `status_corrections` 목록에도 그 필드 키를 추가한다. `verify_extract.py`를 돌리면 "status보정 N건"으로 이 숫자가 실제로 보인다(직전 세션 로그에도 몇 건 나왔었다). pydantic이라면 타입이 안 맞으면 그냥 예외를 던지고 끝인데, 여기는 **"둘 다 스키마상 유효한 값인데 서로 모순"이라는 애매한 경우**라 예외가 아니라 규칙 기반 보정 + 기록이 맞는 처리다.

### ④ 값이 지어낸(환각) 것이면? — 사후 대조로 신호만 남김

`evidence`에 적힌 `region_id`가 진짜로 그 값을 담고 있는지, LLM은 스스로 검증하지 않는다(할 수도 없다 — 지어낸 값이면 지어낸 근거도 그럴듯하게 댈 수 있다). 그래서 [extract.py:424](../src/nh_parsing/extract.py#L424) `_score_evidence`가 **코드로 다시 대조**한다:

> *"003 r016 에서 통독이 없는 상품명을 만들어낸 실측이 있다
> (원본 'NH올원모임서비스' → 'NH클럽온뱅크')."*

값의 토큰이 지목된 근거 영역의 실제 텍스트에 있는지 대조해 `evidence_backed: true/false`를 붙인다. **값을 바꾸거나 지우지 않는다** — 판정은 사람(또는 다음 심의 단계) 몫이고, 여기는 "이 값은 의심스럽다"는 신호만 남긴다. `enum` 필드(예: `product_subtype`)와 `derived` 필드(프롬프트가 재구성을 지시한 필드)는 이 대조에서 빠진다 — 정상 동작인데 원문에 그 글자가 없다는 이유로 오탐되기 때문이다(`preferential_rate_total`이 원문 '최고 4.8%p'에 지시대로 '연'을 붙였다가 세 문서 다 걸린 실측이 있었다).

### 덤 — 값이 흡수되어 사라졌는지 확인 (`_unused_figures`)

pydantic이 절대 해줄 수 없는 종류의 검증이 하나 더 있다: **"광고 본문의 숫자가 어느 필드에도 안 실렸는지."** 003 실측: 헤드라인엔 "총 1,000명", 경품박스엔 "(선착순 1,000팀)"이라고 서로 다르게 적혀 있었는데, STAGE_3가 이걸 하나의 값으로 합쳐버려 **표기 불일치 자체가 사라진** 적이 있다. 그 불일치가 심의가 잡아야 할 대상인데도. `_unused_figures`는 원문의 숫자를 전부 훑어 어떤 필드에도 안 쓰인 게 있으면 검토 신호로 남긴다 — 판정은 아니고 "이거 확인해봐"다.

---

## 5. 결론 — 무엇이 무엇을 막는가

| 방어 대상 | 방식 | 비고 |
|---|---|---|
| 모양(shape)·필수필드 | vLLM guided decoding (`strict: true`) | pydantic보다 강함 — 생성 자체를 막음 |
| JSON 파싱 실패 | 알려진 결함 복구 + 재시도 2회 | pydantic 관할 밖(전제가 유효한 JSON) |
| **서버가 그래도 계약을 어김** | **pydantic** (`_validate_group_response`, 경계①) | 2026-07-30 신설. 원래 계획(§4)에 있었는데 빠져 있던 부분 |
| **우리 코드가 계약을 어김** | **pydantic** (`ExtractResult.model_validate`, 경계②) | 최종 반환 직전. 실제로 모델링 실수 2건을 실행 중 잡아냄 |
| 값·상태 모순 | `_normalize_status`(자동교정 + 기록) | 모양은 맞고 내용만 애매한 경우라 예외보다 교정이 맞음 |
| 근거 환각 의심 | `_score_evidence`(신호만, 판정 안 함) | pydantic이 원래 할 수 없는 일(내용의 진실성) |
| 값 유실(표기 불일치 흡수) | `_unused_figures`(신호만) | 위와 같은 이유로 pydantic 밖의 일 |
| **그룹 전체 실패** | **여전히 미해결** | 제어흐름 문제라 pydantic 관할 아님. `errors`는 이제 필수 필드지만 `verify_extract.py`가 아직 안 봄 |
| 필드명 리팩터링 오타 | 없음 | `field_key`가 스키마 JSON의 동적 키라 pydantic의 "칸 모양" 검증으로는 못 잡음. `applicability.py`는 `.get()`이라 최소한 크래시는 안 남 |

**요약**: 모양 차원은 guided decoding이 생성 단계에서 막고, 그 아래에 pydantic이 두 겹(서버·우리 코드) 더 감시한다. 내용의 진실성(환각·유실)은 pydantic이 원래 할 수 없는 일이라 별도 코드가 맡는다. 아직 안 남은 빈틈은 **"그룹 전체 실패가 다음 단계에서 조용히 통과할 수 있다"**와 **"필드명 동적 키라 리팩터링 정적검사가 없다"** 둘 — 회의 때 언급할 가치가 있는 실제 리스크이되, 지금까지 실제로 문제를 일으킨 적은 없다(44/44는 무캐시 실행으로도 재확인됨).
