# C. 최종 산출물 — 어떤 모양이고 다음 단계로 어떻게 넘어가나

> 스키마 추출 결과 JSON 의 최상위 키가 각각 뭔지, 필드 한 칸의 구조,
> **`absence.kind` 분류가 왜 중요한지**, `observations`·`unmapped`·`coverage` 의 역할,
> 그리고 이 결과가 RAG·DB 담당자에게 어떻게 넘어가는지.
>
> **STAGE_3 란**: 이 파이프라인의 세 번째 단계 — 파싱(글자·좌표 추출) 다음에 오는 "스키마
> 기반 필드 추출" 단계를 가리키는 이 프로젝트의 내부 호칭이다. A 문서가 파싱(1~2단계)을,
> 이 문서(C)가 그 다음(STAGE_3의 결과물)을 다룬다.

---

## 0. ⚠️ 먼저 사실 확인 — "최상위 21개 키"는 문서마다 다르다

기존 기록에 "최상위 21개 키"라고 적혀 있는데, 11개 산출물 전수를 세어 보니 **고정이 아니다.**

```
extracted/1. 예금성상품(적립식).json          21키  events=0
extracted/5. 예금성상품(입출식).json          21키  events=0
extracted/[예금성상품-적금] 올원e적금.json      21키  events=0
extracted/NH농협은행-2026_001-예금성.json    23키  events=1  추가=[event_count_reported, events_pruned]
extracted/NH농협은행-2026_003-예금성.json    23키  events=2  추가=[event_count_reported, events_pruned]
extracted/NH농협은행-2026_004-대출성.json    20키  events=0  없음=[ad_type]         ← HWP
```

| 키 수 | 언제 | 이유 |
|---|---|---|
| **20** | HWP 문서 | `ad_type` 이 없다 — HWP 는 캔버스가 없어 **분류 VLM 을 애초에 안 부른다**([A 문서](A-파이프라인-입력부터-결과까지.md) 1-a) |
| **21** | 일반 | 기본 |
| **23** | 이벤트페이지 | `event_count_reported` + `events_pruned` 가 조건부로 추가 |

**"21키"는 기본형이고, 조건부 키가 있다.** 문서(`농협연동_전체정리 §5-3`)가 이걸 고정으로
적고 있고, 게다가 그 4층 목록에 `overlays_applied` 가 빠져 있고 `events_pruned` 는 항상 있는
것처럼 적혀 있다 → **정리 대상.**

---

## 1. 21키를 4층으로 읽는다

```
① 식별 (7)    document · doc_id · product_group · ad_type · schema_id
              schema_version · overlays_applied
              → "무엇을 어떤 스키마로 처리했나"

② 추출값 (5)  fields · observations · events · unmapped · group_analysis
              → AI 가 낸 것

③ 코드검산 (7) review_gaps · input_gap · evidence_unbacked · unused_figures
              status_corrections · observations_pruned · errors
              → 코드가 AI 응답을 검사해 붙인 것 (VLM 호출 0회)

④ 집계 (2)    coverage(17지표) · elapsed_s
```

| 키 | 뜻 | 실물 (`1. 예금성상품(적립식)`) |
|---|---|---|
| `schema_id`/`schema_version` | 어느 스키마로 뽑았나 | `"예금성"` / `"v2"` |
| `overlays_applied` | 붙은 오버레이 | `[]` (이벤트 아님) |
| `fields` | 필드 48칸 | `found 32 · not_found 15 · uncertain 1` |
| `observations` | 금지표현 **의심 후보** | 4항목 전부 `[]` |
| `events` | 이벤트 배열 | `[]` |
| `unmapped` | 어느 필드에도 안 맞은 문구 | 19건 |
| `group_analysis` | 그룹별 AI 서술 | 9그룹 |
| `review_gaps` | 부재 분류 결과 | 아래 3절 |
| `input_gap` | 파싱은 됐는데 입력에 안 실린 영역 | `[]` |
| `evidence_unbacked` | AI 가 댄 근거가 실제 텍스트에 없는 것 | `[]` |
| `unused_figures` | 안 쓰인 도형/이미지 | `[]` |
| `status_corrections` | 코드가 status 를 고친 기록 | `[]` |
| `observations_pruned` | 버린 관측 + 이유 | 4건 버림 |
| `coverage` | 17지표 자기진단 | 아래 5절 |
| `elapsed_s` | 소요 | `37.2` |

---

## 2. 필드 한 칸의 구조 — **누가 썼는지가 갈린다**

### 값이 있을 때

```json
"advertiser_bank_name": {
  "value": "NH농협은행",
  "evidence": ["p1_r024"],        ← ★ A단계의 region_id 로 근거를 가리킨다
  "status": "found",
  "note": "",
  ─────────────────────────────── 위: VLM 이 낸 것
  "group": "C1_주체와상품",
  "evidence_score": 1.0,
  "evidence_backed": true         ← 아래: 코드가 사후에 검산해 붙인 것
}
```

### `evidence_backed` 가 이 프로젝트에서 중요한 장치다

AI 가 *"근거는 p1_r024"* 라고 답했을 때, **코드가 실제로 p1_r024 의 텍스트를 가져와 `value`
가 거기 있는지 확인**한다. 없으면 `evidence_backed: false` + `evidence_unbacked` 목록에
올라간다.

```
AI가 값을 지어냈다  →  근거 영역 텍스트에 그 값이 없다  →  코드가 잡는다
```

즉 **AI 응답을 그대로 믿지 않고 검산하는 층**이고, `evidence_unbacked_count: 0` 은
"이번 실행에서 지어낸 값이 없다"는 뜻이다.

`evidence` 가 `region_id` 인 것도 A 단계와 이어진다 —
[llm_view.py:7](../../src/nh_parsing/llm_view.py#L7):

> *"`region_id` 는 유지한다 → 추출 후 이 ID 로 **bbox 를 되붙여** 하이라이트에 연결.
> (HyundaiHS 는 좌표를 아예 버려 재부착도 안 하지만, 우리는 시인성 요구 때문에 ID 를 남긴다.)"*

### 값이 없을 때 — `absence` 가 붙는다

```json
"fee_and_cost": {
  "value": "", "evidence": [], "status": "not_found", "note": "",
  "group": "C2_의무고지문구",
  "absence": {
    "kind": "해당없음",
    "obligation": "필수",
    "rule": "conditional"
  }
}
```

---

## 3. ⭐ `absence.kind` — 왜 중요한가 (그리고 3분류가 아니라 4종이다)

### 문제

STAGE_3(AI)은 값이 없으면 **전부 `status="not_found"`** 로 돌려준다. 그런데 심의 관점에서
"값이 없다"는 **완전히 다른 사실 여러 개가 뭉개진 것**이다.

[applicability.py 모듈 docstring](../../src/nh_parsing/applicability.py#L1-L28) 원문:

> **(A) 해당없음** — 이 상품유형·광고유형에 그 항목이 성립하지 않는다.
> (수시입출식 통장에 만기후이율)
>
> **(B) 미표시** — 표시 의무가 있는 항목이 광고 텍스트에서 **발견되지 않았다는 사실**이다.
> **위반인지, 위험도가 얼마인지는 여기서 판정하지 않는다** — 그건 하류 몫이다. 우리가
> 넘기는 건 "미표시"라는 관측이지 **"위반"이라는 결론이 아니다** (2026-08-03 정정 — 이전
> 버전은 이 필드를 "심의 지적사항"으로 단정해 하류 판정을 침범했다).
>
> **(C) 확인필요** — 광고엔 있는데 우리가 못 올렸을 수 있다(파싱·입력 결함).

**측정 근거:**

> *2026-07-28: 4개 문서 미발견 51건 중 **41건이 (A)** 였다. 셋을 한 통에 담아두면
> `fields_not_found` 숫자가 성능 지표처럼 보이지만 **분모가 틀린 값**이고, 정작 팔아야 할
> (B)를 못 집어낸다.*

### 왜 중요한가 — 숫자로

`1. 예금성상품(적립식)` 실측:

```
status 분포:     found 32 · not_found 15 · uncertain 1
absence 분포:    해당없음 14 · 판정제외 1 · 미표시 0
```

**`not_found` 15건을 그대로 보면 "15개를 못 찾았다"** 로 읽힌다. 실제로는:

```
14건  →  이 상품에 애초에 해당 안 됨 (적립식 적금에 수수료·수상표기·통계출처 등)
 1건  →  판정 제외 (deposit_kind = 분류축)
 0건  →  진짜 지적 후보
```

**"15개 실패"가 아니라 "지적 후보 0건"** 이다. 정반대 결론이다.

### ⚠️ 실제로는 4종이다 — 문서·주석과 어긋난다

[applicability.py:47-50](../../src/nh_parsing/applicability.py#L47-L50) 상수:

```python
ABSENT_NOT_APPLICABLE  = "해당없음"
ABSENT_MISSING         = "미표시"
ABSENT_UNKNOWN_SUBTYPE = "확인필요"
ABSENT_OUT_OF_SCOPE    = "판정제외"     ← 4번째
```

산출물의 `review_gaps` 키도 4개다:

```json
"review_gaps": {
  "미표시": [],
  "해당없음": ["ai_generated_notice","award_cert_info","endorsement_disclosure",
              "etm_ad_marker","etm_optout","etm_sender_info","fee_and_cost",
              "future_return_disclaimer","interest_calc_method","joint_ad_insured_status",
              "joint_ad_no_guarantee","lottery_rate_probability","stats_source","tax_benefit"],
  "판정제외": ["deposit_kind"],
  "확인필요": [],
  "product_subtypes": ["적립식"],
  "subtype_unknown": false
}
```

`coverage` 지표도 4개(`absence_missing`·`absence_not_applicable`·`absence_out_of_scope`·
`absence_needs_check`).

**그런데 "3분류"라는 이름이 세 곳에 서로 다르게 남아 있다:**

| 어디 | 뭐라고 적혀 있나 |
|---|---|
| `applicability.py` docstring | "부재의 3분류" — (A)해당없음 (B)미표시 (C)**확인필요** (판정제외 빠짐) |
| `농협연동_전체정리 §5-3` | "3분류" — 해당없음 / 미표시 / **판정제외** (확인필요 빠짐) |
| 실제 코드·산출물 | **4종** |

→ **정리 대상.** 이름을 "4분류"로 고치거나, 왜 하나를 빼고 세는지 명시해야 한다.

### 4종의 뜻과 심의에서의 의미

| kind | 뜻 | 심의에서 |
|---|---|---|
| **해당없음** | 이 상품 유형에는 애초에 필요 없는 항목 (입출금 통장에 "계약기간") | 문제 없음 |
| **미표시** | 표시해야 하는데 광고에 없다 | **지적 후보** (판정은 하류) |
| **판정제외** | 판정 대상 등급이 아니다 (분류·수집·관측·절차) | 사람이 봐야 함 |
| **확인필요** | 광고엔 있는데 우리가 못 올렸을 수 있다 | **우리 결함 의심** |

### (A)/(B)를 코드가 결정론으로 가른다 — VLM 안 부른다

> *"(A)/(B) 는 규정 조건이라 **코드가 결정론적으로** 가른다 — 스키마 각 필드의
> `applicability` + `obligation` 을 평가할 뿐 **VLM 을 부르지 않는다**.
> **문제마다 VLM 단계를 얹어온 패턴을 여기서 반복하지 않기 위해서다.**"*

### (C)는 정직하게 좁혀 놓았다

> *"(C)는 정의상 '우리 산출물에 없는 것'이라 **자기 출력만 보고는 알 수 없다**. 대신
> **확실히 아는 한 가지 경로만** 근거로 남긴다 — 파싱은 됐는데 STAGE_3 입력(llm_view)에서
> 빠진 영역. … **그 밖의 (C)는 여기서 추정하지 않는다** — 키워드로 짐작하면 오탐이 섞이고
> (실측), 그걸 지적사항 옆에 섞어 놓으면 (B)의 신뢰도까지 같이 떨어진다."*

그래서 `input_gap` 키가 따로 있다 (실측 `[]` = 입력 유실 0).

---

## 4. `observations` · `unmapped` — 두 안전망

### `observations` — **판정이 아니라 의심 후보**

```json
"observations": {
  "obs_rate_overstated": [],
  "obs_max_rate_only_emphasis": [],
  "obs_compound_ambiguous": [],
  "obs_special_rate_as_cash": []
}
```

`field_key` 를 키로 하는 dict, 각 값이 배열이다. **하류가 이걸 위반으로 굳히면 허위 지적이
된다** — 합의 항목으로 올라가 있다(6절 §8-4).

**그리고 빈 관측은 버린다** (`observations_pruned`):

```json
{"dropped_per_field": {"obs_rate_overstated": 1, "obs_max_rate_only_emphasis": 1,
                       "obs_compound_ambiguous": 1, "obs_special_rate_as_cash": 1},
 "total": 4,
 "reason": "인용문이 빈 관측 — 하류가 없는 위반 후보를 보게 된다"}
```

AI 가 `quote` 없이 관측 항목만 만든 4건이다. **조용히 버리지 않고 몇 건을 왜 버렸는지
남긴다.**

> 첫 실행에서는 관측 49건 중 **41건(84%)이 유령**이었고 제거해서 **8건**만 남긴 이력이 있다.
> (⚠️ 2026-08-23 정정: 이전 판은 "18건"이라 적었으나 49−41=8이고, 근거인
> [extract.py:410](../../src/nh_parsing/extract.py#L410) 주석도 41건 제거만 확인될 뿐
> 18이라는 숫자의 출처가 없다 — 오기로 보고 8로 고친다.)

### `unmapped` — 유실 방지 안전망 (가장 위험한 실패를 막는 자리)

```json
{"text": "banking.nonghyup.com", "evidence": ["p1_r023"],
 "kind": "심의무관", "reason": "웹사이트 주소",
 "groups": ["C1_주체와상품","C2_의무고지문구","D1_상품기본","D2_금리수치","D3_예금성고지"],
 "evidence_score": 1.0, "evidence_backed": true}
```

**`groups` 가 여러 개인 이유**: 그룹마다 따로 호출하므로 **같은 문구가 여러 그룹에서
"내 필드엔 안 맞음"으로 올라온다.** 코드가 합쳐서 "어느 그룹들이 못 담았나"로 기록한다.

**`kind` 가 실제로는 3종이다** — 스키마 정의는 2종인데:

```python
# schema_pack.py:37
UNMAPPED_KIND = ["심의무관", "심의관련_필드없음"]
```

실물에는 **`다른항목에서_처리됨`** 이 있다:

```json
{"text": "NH농협금융 / 준법감시인심의필2026-4256 / (2026.07.31.~2027.07.30.)",
 "kind": "다른항목에서_처리됨", "reason": "준법감시인 심의번호 및 유효기간"}
```

[extract.py:400,404](../../src/nh_parsing/extract.py#L400) 에서 코드가 사후에 재분류한다 —
"C2 그룹은 못 담았다고 했지만 C1 그룹이 이미 `review_stamp` 로 담았다"를 코드가 대조해
딱지를 바꾼다. 안 하면 **이미 담긴 것이 "필드가 없어서 못 담았다"로 보고돼 스키마 공백처럼
읽힌다.**

19건의 분류 결과: `unmapped_schema_gap: 3` — 진짜 스키마 공백은 3건이다.

---

## 5. `coverage` — 골드(정답지) 없이 재는 17지표

> 📌 **B 문서의 "체크리스트 커버리지"와 다른 것이다.** 그건 *규정을 빠뜨렸나*
> (설계 시점 1회, 분모 = 정본 97항목, 값 97.3%), 이건 *광고를 빠뜨렸나*(문서마다,
> 분모 = 그 문서의 영역 수, 값 `region_coverage: 0.776`). 이름만 같다.
> → [00-읽는-순서 §이름이 같아서 헷갈리는 것](00-읽는-순서.md#-이름이-같아서-헷갈리는-것-5쌍)

```
regions_total 22 · regions_cited 22 · regions_uncited [] · region_coverage 1.0
fields_found 32 · fields_not_found 15 · fields_uncertain 1
absence_missing 0 · absence_not_applicable 14 · absence_out_of_scope 1 · absence_needs_check 0
absence_missing_in_events 0 · input_gap_regions 0
unmapped_total 19 · unmapped_schema_gap 3
evidence_unbacked_count 0 · unused_figures_count 0
```

**`region_coverage` 가 가장 유용한 지표다** — *"파싱이 뽑은 영역 22개 중 몇 개가 어딘가에
인용됐나"*. `1.0` 은 **읽은 것을 하나도 안 버렸다**는 뜻이다.

**그리고 코드에 성능 지표를 잘못 보지 말라는 경고가 있다**
([extract.py:666-667](../../src/nh_parsing/extract.py#L666)):

> *"성능 지표로 볼 것은 `fields_not_found` 가 아니라 **'미표시'(심의 지적사항)와
> '확인필요'(판정 불가)** 쪽이다."*

### 이벤트가 있으면 — `events_pruned` 실물

003 문서:

```json
"event_count_reported": 2,
"events": [ {이벤트1...}, {이벤트2...} ],
"events_pruned": {
  "dropped_indexes": [3],
  "reason": "값이 하나도 없는 이벤트 — 부재 판정에서 허위 지적사항을 만든다",
  "event_count_reported": 2,
  "array_length": 3
}
```

**AI 가 "이벤트 2개"라고 말하면서 배열은 3개를 냈고, 3번째가 완전히 빈 껍데기였다.**
그대로 두면 3번째 이벤트의 `event_name`·`event_period`·`event_prize` 전부가 "미표시"로
올라가 **허위 지적 여러 건**이 된다. 코드가 자기 보고(`event_count_reported`)와 배열 길이를
대조해 잡는다.

---

## 6. 다음 단계로 어떻게 넘어가나

```
[우리가 넘기는 것]                        [RAG·RDB 담당자가 만들 것]
regulation _hrc.jsonl → 농협 KL 적재       KL 검색 API 로 조문 조회
out/extracted/*.json  (21±키)             RDB 적재 + 질의어로 사용
판정: 하지 않는다                          판정: 여기서 한다
```

```
[광고물 결과]
  product_name        = "NH메디칼론"
  fee_and_cost        = "●수입인지대금: 대출금액 5천만원 이하 비과세…"
  interest_rate_range = (값 없음, absence.kind="미표시")
        │
        ├──▶ 벡터DB 조회: "이 광고에 걸리는 규정 조문이 무엇인가"
        └──▶ RDB 조회:   "이 상품 유형에 필수인 표시의무가 무엇인가"
                        "과거 유사 광고에서 어떤 지적이 있었나"
                             ↓
                    심의 결과 생성  ← 담당자 영역
```

### 연결 키 제안 — `checklist_ref`

**문제**: 광고물 필드로 벡터DB 를 검색해 규정 청크를 받아도, **그 청크가 어느 체크리스트
항목인지 모르면 판정이 안 된다.**

**우리가 이미 가진 것**: 필드마다 `checklist_ref`, 정본 97항목 대조 **유령 참조 0건**.

**제안**: 규정문서를 KL 에 넣을 때 `cust_sattr4` 에 `checklist_ref` 를 심는다.

```
[규정문서 청크]  cust_sattr4 = "DEP-INC-06"      ← 예약해 둔 자리 (kl_export.py 에 빈 칸)
[광고물 필드]    interest_rate_range.checklist_ref = ["DEP-INC-06"]
                        └─▶ 같은 값으로 KL 조회 → 해당 조문 청크가 나온다
```

> ⚠️ **아직 안 되는 것**: 규정문서 청크 → 체크리스트 항목 **매핑이 자동이 아니다.**
> `RagChunk` 에 `checklist_ref` 필드가 없다. 채우는 방법(사람이 붙이나 / 규칙 / VLM)이
> **합의 대상**이다. → [D](D-농협-자료와-연동-규격.md) · [E](E-남은-일과-정리-후보.md)

### 합의해야 할 것 6건 (그중 🔴 4건)

| | 항목 | 왜 |
|---|---|---|
| 🔴 8-1 | **연결 키 — `region_id` 는 재처리하면 깨진다** | `p1_r018` 은 레이아웃 검출 순서로 붙은 번호. 2026-08-16 파싱 방식을 바꾸자 영역이 **1,905 → 2,463개**로 변했다 → 예전 `p1_r062` 가 다른 영역을 가리킨다. **`parser_version` + `doc_id` + `region_id` 복합 키 필요** |
| 🔴 8-2 | **값 없음의 DB 표현 — `null` 하나로 뭉치면 안 된다** | RDB 설계에서는 "값 없음"을 그냥 하나의 nullable 컬럼으로 두고 싶은 유혹이 자연스럽다 — 어차피 "없다"는 사실은 같아 보이기 때문이다. 그런데 여기서 그러면 안 된다: absence 4종을 `null` 로 합치면 **"해당없음"과 "미표시"가 같아진다** = 3절 전체가 무의미해짐. `1. 대출성상품` 실측: 판정제외 1 · 해당없음 12 · 미표시 3 → 16개를 다 `null` 로 저장하면 **"미표시 3건" 지적이 사라진다.** `status` 와 `absence.kind` 를 **별도 컬럼으로** |
| 🔴 8-3 | **재처리 정책 — VLM 층은 실행마다 흔들린다** | 5문서 2회 실행 실측: 문장회수 **234 vs 233**, 후보로만 회수된 건수 13→12→9. **멱등**(같은 입력을 다시 넣어도 같은 결과가 나와야 한다는 전제) 전제가 깨진다 → (가) 덮을지 버전으로 쌓을지 (나) 심의 결과가 이미 나온 문서를 재처리해도 되는지 |
| 🔴 8-6 | **규정문서 청크의 `checklist_ref` 를 누가 채우나** | 3안: (가)사람 (나)규칙 — 정본에 `근거규정` 열이 실재(`기준 §16① 1`)해 조항 번호 매칭 가능 (다)VLM — 비결정 층이 규정 쪽까지 번진다. **추천은 (나) → (가) 보정** |
| 🟡 8-4 | `observations` 는 판정이 아니다 | 굳히면 허위 지적. 화면에 위반으로 표시 전 사람 확인 단계 필요 |
| 🟡 8-5 | `verbatim` 필드는 다듬지 말 것 | DB 저장 시 공백 정규화·트림·유니코드 정규화를 하면 **심의 대상이 사라진다**. `1,000명`/`1,000팀` 소멸 전례 → **원문 바이트 그대로** |

---

## C 요약

```
① 식별 7키    무엇을 어떤 스키마로 처리했나 (overlays_applied 포함)
② 추출값 5키   AI 가 낸 것 (fields · observations · events · unmapped · group_analysis)
③ 검산 7키    코드가 AI 응답을 검사한 것 (VLM 0회)
④ 집계 2키    coverage 17지표 · elapsed_s
   → 총 21키 (HWP 20 / 이벤트 23)

필드 한 칸:  {value, evidence, status, note}            ← VLM
            {group, evidence_score, evidence_backed}   ← 코드 검산
            {absence: {kind, obligation, rule}}        ← 값 없을 때만

absence 4종:  해당없음 (문제 없음) / 미표시 (지적 후보) /
              판정제외 (판정 대상 아님) / 확인필요 (우리 결함 의심)
   → 이걸 안 가르면 not_found 15건이 "15개 실패"로 읽힌다. 실제 지적 후보는 0건.

넘기는 곳:  extracted/*.json → RDB,  _hrc.jsonl → KL
연결 키:    checklist_ref (우리 쪽 준비 완료, 규정 청크 쪽이 비어 있음)
```

**C 에서 꼭 잡고 갈 세 가지**

1. **`absence.kind` 가 이 산출물의 존재 이유다** — "값이 없다"를 4가지로 갈라야 지적 후보를
   골라낼 수 있다. `null` 하나로 뭉치면 전부 무의미해진다(합의 §8-2 가 🔴 인 이유).
2. **코드 검산층(③)이 AI 를 믿지 않는 장치다** — `evidence_backed`(값을 지어냈나),
   `events_pruned`(유령 이벤트), `observations_pruned`(빈 관측), `status_corrections`.
   전부 VLM 호출 0회.
3. **우리는 판정하지 않는다** — "미표시"는 관측이고 "위반"은 하류의 결론이다. 2026-08-03 에
   이 경계를 침범했다가 정정한 기록이 코드에 남아 있다.
