# 저장소 비교 분석 — nh-ad-review-poc ↔ nh-ad-compliance

> 작성 2026-07-29. 구현 없음, 구조 비교만.
> 대조 대상: `nh-ad-compliance` (docs 14,487행 + adr 10,652행 + apps/packages 실제 코드),
> `nh-ad-review-poc` (docs + schemas + **out/ 실제 산출물 5문서**).
> 문서 설명이 아니라 **실제 코드·실제 JSON**을 우선 근거로 삼았다. 근거 위치를 다 적었다.

---

## 0. 결론 요약

| # | 판정 | 한 줄 |
|---|---|---|
| **1** | **겹치지 않는다** | 저장소 2의 파서 서비스는 **PaddleOCR 라인 OCR 1회 + layoutBlocks 빈 배열**이다. 우리 17단계 파이프라인과 기능이 겹치는 게 아니라, **우리 것이 들어갈 빈 자리**다 |
| **2** | **가장 큰 격차** | 저장소 2에는 **필드 단위 추출 결과를 담을 테이블이 없다.** `review_items`는 존재하는 텍스트 블록 1개 = 1행 구조라서, **"없는 필드"는 행이 아예 생기지 않는다** (코드로 확인) |
| **3** | **직접 충돌** | `product_group` enum이 `DEPOSIT/SAVINGS/DEMAND_DEPOSIT`로 갈라져 있다. 우리는 이 셋을 **예금성 하나 + product_subtype**으로 합쳤다. 어느 쪽이 맞는지 정해야 기준자료 적재가 시작된다 |
| **4** | **의외의 수렴** | `result_status` enum 5종이 우리 `absence.kind` 4분류와 **의미상 1:1로 맞는다**. 같은 문제를 각자 풀었고 답도 같았다 — 다만 **다른 레이어에 놓여 있다** |
| **5** | **이미 결정돼 있음** | 회의 안건 5개 중 **5번(심의판정 위치)은 ADR-0013·0018로 확정**돼 있다. 4번(재처리)은 절반, 2·3번은 미결정 |
| **6** | **우리가 놓친 것** | 검토유형 7종 중 우리가 다루는 건 3종. `VISIBILITY`(시인성)·`PRODUCT_CONSISTENCY`(약관 정합성)는 **입력 자체가 없다** (스타일 정보·2차 파일) |

---

## 1. 두 저장소의 성격 — 무엇을 "정답"으로 볼 수 있나

| | 저장소 1 (PoC) | 저장소 2 (프로토타입) |
|---|---|---|
| 넓이 | 좁다 — 파싱 + 필드추출 | 넓다 — 화면/API/워커/파서/DB/평가/배포 |
| 깊이 | 깊다 — 5문서 실측, 결함 정량화 | 얕다 — provider-free thin slice + fixture |
| 검증된 것 | 필드회수 44/44, 문장 237/242, 실행간 변동 235~237 실측 | M0~M8 결정론 fixture E2E, dev 자동배포 |
| 검증 **안** 된 것 | 판정·위험도·리포트·화면 전부 | **실제 OCR/RAG/LLM 품질** (README가 명시적으로 인정) |
| 결정 밀도 | 문서 3개, 미합의 5건 | **ADR 83개 중 82개 Accepted**, 미결 1건(Q77 폐쇄망 반입) |

**따라서 역할이 갈린다.**
- 저장소 2가 정답에 가까운 영역: **저장·API·상태·권한·평가·배포 계약** (우리가 아예 안 만든 것)
- 우리가 정답에 가까운 영역: **광고물이 실제로 어떻게 안 읽히는지, 값이 없을 때 무슨 일이 벌어지는지** (저장소 2가 아직 실측 못 한 것)

저장소 2 README도 이걸 인정한다:
> *"provider-free 성공은 실제 OCR/RAG/LLM 품질, 고객 시연·검증, 결함 0건을 대신 증명하지 않으며, 해당 항목은 증거가 생길 때까지 `Backlog`/`Blocked`로 유지"*

우리 `out/`이 바로 그 "증거"다.

---

## 2. 중점 ① — 상품군과 스키마 설계, 같은 방향인가

### 2-1. 상품군 축: **구조가 다르다** (직접 충돌)

| 저장소 2 `product_group` enum | 우리 모델 | 판정 |
|---|---|---|
| `DEPOSIT` (예금) | 예금성 + `product_subtype=예금` | ⚠️ **레벨 불일치** |
| `SAVINGS` (적금) | 예금성 + `product_subtype=적금` | ⚠️ **레벨 불일치** |
| `DEMAND_DEPOSIT` (입출금) | 예금성 + `product_subtype=입출금(통장·MMDA)` | ⚠️ **레벨 불일치** |
| — | 예금성 + `product_subtype=ELD/주가·환율연동` | ❌ **저장소 2에 없음** |
| `EVENT_FINANCIAL_PRODUCT` (이벤트성 금융상품) | **상품군이 아님** → `ad_type=이벤트페이지` | ❌ **정면 충돌** |
| `LOAN` / `CARD` / `INVESTMENT` | 대출성 / 카드 / 투자성 | ✅ 일치 |
| — | 보장성, 시설대여·할부, 업무광고, 상품이미지 | ❌ 저장소 2에 없음 |

근거: `docs/database-specification.md`(저장소2) / [out/schema_source/_product_group_fields.json](../out/schema_source/_product_group_fields.json) `classification_axes`

**왜 이게 실무 문제인가.** `standards.product_group`·`evidences.product_group`이 필수문구 목록을 필터링하는 키다(F-006 처리규칙 1: *"상품군과 광고유형을 기준으로 필수 문구 목록을 조회한다"*). 우리 스키마의 **G3 의무고지 15필드는 예금/적금/입출금에 공통**이다. 저장소 2 enum을 그대로 쓰면:

- 같은 근거조문 행을 `DEPOSIT`/`SAVINGS`/`DEMAND_DEPOSIT` **3배로 적재**해야 하거나
- `product_group=null`(전 상품군 공통)로 두어 **대출성·투자성에까지 새는** 수밖에 없다

우리가 조건부 판정에 쓰는 `product_subtype` 분기(*"수시입출식엔 만기 개념이 없음"*)도 저장소 2 모델에서는 **product_group 간 비교**가 되어버려 규칙을 표현할 자리가 없다.

**이벤트 건은 저장소 2 안에서도 이미 모순이다.** `advertisement_type`에 `EVENT_PAGE`가 이미 있는데 `product_group`에도 `EVENT_FINANCIAL_PRODUCT`가 있다. 002·003 광고를 넣으면 `product_group`을 `SAVINGS`로 쓸지 `EVENT_FINANCIAL_PRODUCT`로 쓸지 **결정 불가**다(둘 다 NN 단일값). 우리 실측 결론(이벤트는 상품군 위에 얹히는 광고유형)이 이 모순을 해소한다.

> **제안**: `product_group`에 `DEPOSIT_LIKE`(예금성)를 두고 `product_subtype` 컬럼을 신설. `EVENT_FINANCIAL_PRODUCT`는 제거하고 `advertisement_type=EVENT_PAGE`로 통일.

### 2-2. 광고유형 축: **부분 겹침, 축이 다르게 섞여 있다**

| 저장소 2 `advertisement_type` | 우리 `ad_type` | |
|---|---|---|
| `EVENT_PAGE` | 이벤트페이지 | ✅ |
| `LEAFLET` | 안내장 | ✅ |
| `MOBILE_BANNER`, `INTERNET_BANKING_BANNER` | 배너 | △ 우리가 덜 세분 |
| `APP_PUSH` | 앱푸시 | ✅ |
| `SMS`, `ALIMTALK` | SMS·알림톡 | △ |
| `HTML_CAPTURE` | 상세페이지 | △ 이름만 다름 |
| `BRANCH_MEMO`, `CUSTOMER_NOTICE` | — | ❌ 우리에 없음 |
| — | **상품광고 / 업무광고** | ❌ **저장소 2에 없음** |

**중요한 차이**: 우리 축에는 `상품광고/업무광고`가 들어 있다. 이건 매체가 아니라 **광고 성립성·심의대상성 게이트**(v12 시트5 2단계)다. 저장소 2는 `advertisement_type`을 순수 매체·형식으로만 두고 `channel_type`을 따로 뒀다 — **더 깔끔하다.** 우리 `ad_type`이 두 축을 섞고 있으니 우리가 분리하는 쪽이 맞다.

### 2-3. 필드/체크항목이 사는 곳: **저장 형태가 완전히 다르다**

| | 우리 | 저장소 2 |
|---|---|---|
| 필수문구 목록 | `schemas/예금성.json` (Git JSON, 46필드) | `standards` / `evidences` **테이블** (DB 행) |
| 근거조문 | `out/schema_source/_product_group_fields.json` (근거 대장) | `evidences.article_no` + `content` + `rule_type` |
| 조건부 적용 | `applicability.conditions` (4가지 rule 타입) | ❌ **없음** — `applies_when`류 컬럼 부재 |
| 의무 강도 | `obligation`: 필수/권장/관측/분류 | `rule_type`: `REQUIRED/PROHIBITED/RECOMMENDED/REFERENCE` |
| LLM 지시문 | `prompt` (필드마다) | `configs/prompts/*.yaml` (ADR-0016, agent 단위) |
| 버전 | `schema_version: v1` | `standard_versions` + `effective_date`/`expired_date` |

**그런데 근거 대장은 저장소 2의 `evidences` 테이블과 놀랄 만큼 잘 맞는다:**

```jsonc
// 우리 근거 대장 (out/schema_source/_product_group_fields.json)
{ "field_key": "advertiser_bank_name", "priority": "필수", "category": "표시의무",
  "review_items": [{ "check": "광고 주체인 은행의 명칭을 표시하였는가?",
    "basis": { "law": "은행 광고심의 기준", "article": "§16①1",
               "source_doc": "(2023)은행연합회 광고심의 매뉴얼 1부.hwp p105",
               "source_type": "자율규제" }, "applies_when": "전 광고" }] }
```
↕
```
evidences.article_no    = "§16①1"
evidences.content       = check 문장
evidences.rule_type     = REQUIRED      (priority=필수)
evidences.evidence_type = REGULATION    (source_type=자율규제)
evidences.product_group = DEPOSIT_LIKE
standards.title         = "(2023)은행연합회 광고심의 매뉴얼 1부"
```

**즉 우리 근거 대장은 저장소 2의 기준자료 seed로 거의 그대로 변환된다.** 44개 필드 × 근거조문이 이미 정규화된 상태다. 이게 두 저장소 사이에서 **가장 값싸고 확실한 접점**이다.

**단, 옮겨지지 않는 것 3개:**
1. `applicability.conditions` — `subtype_not_in`/`trigger_any`/`conditional`/`subtype_in`. `evidences`에 조건 컬럼이 없다. `metadata_json`에 넣을 순 있지만 그러면 **부재 판정 로직이 jsonb 파싱에 의존**한다
2. `effective_date` — 우리 스키마에 없다. 근거 대장 `open_questions` Q2(*"예금자보호 로고 병기 2026.9 시행 예정"*)가 정확히 이 문제고, 저장소 2는 `standard_effective_date` + `applied_standard_version_ids`로 **이미 풀어놨다**
3. `field_key` — **저장소 2 DB·API 어디에도 필드 식별자 컬럼이 없다** (grep 결과 0건). §3-2에서 다시 다룬다

### 2-4. 판정

| 축 | 방향 |
|---|---|
| 상품군이 필수문구 목록을 가른다 | ✅ **같은 방향** (F-006 처리규칙 1 = 우리 축 1) |
| 광고유형이 상품군과 직교한다 | ✅ **같은 방향** (그쪽도 `advertisement_type` 별도 컬럼) — 단 `EVENT_FINANCIAL_PRODUCT`가 이 원칙을 스스로 깬다 |
| 상품군 세분 레벨 | ❌ **다름** (그쪽 3개 vs 우리 1+subtype) |
| 필드 정의를 어디 두나 | ❌ **다름** (Git JSON vs DB 행) |
| 호출그룹 분할(G1~G5) | ❌ **저장소 2에 개념 없음** — `review_type` 7종이 유사하나 목적이 다름(판정 분류 vs LLM 호출 단위) |

---

## 3. 중점 ② — DB 스키마가 우리 산출물을 담을 수 있나

우리 산출물은 3층이다. 층마다 결과가 다르다.

| 산출물 | 저장소 2 목적지 | 담기는가 |
|---|---|---|
| `out/json` (IR 60~85KB) | `ocr_text_blocks` + `layout_blocks` + `parser_artifacts` | **△ 60% — 좌표는 완벽, 의미 계층은 유실** |
| `out/llm_view` (17~27KB) | ❌ 목적지 없음 | **행선지 미정** |
| `out/extracted` (18~32KB) | `review_items` (?) | **❌ 담기지 않음 — 구조가 다름** |

### 3-1. `out/json` → `ocr_text_blocks` / `layout_blocks`

**좌표는 실측으로 검증했다.** 우리 5문서 225영역 445라인을 저장소 2의 `Coordinate` pydantic 계약(`packages/parser-contracts/src/nh_ad_parser_contracts/models.py`(저장소2))에 대조:

```
regions 225 / lines 445
  width<=0 또는 height<=0 :  0건
  좌표 음수                :  0건
  source 경계 초과          :  0건   ← x+width <= source_width 검증 통과
  bbox 없는 region          :  1건   (HWP — 예상된 것)
```

→ **좌표 계약은 변환만 하면 그대로 통과한다** (`[x0,y0,x1,y1]` → `x/y/width/height` + `normalized*`). canvas_w/h가 `sourceWidth/Height`가 된다.

**매핑 표:**

| 우리 IR | 저장소 2 | 상태 |
|---|---|---|
| `lines[].text` / `bbox` / `confidence` | `ocr_text_blocks.block_text` / 좌표 / `confidence_score` | ✅ |
| `regions[].bbox` | `layout_blocks` 좌표 | ✅ |
| `regions[].region_id` | `layout_blocks.layout_block_id` | ✅ |
| `regions[].lines` | `layout_blocks.related_ocr_block_ids` (jsonb) | ✅ |
| `regions[].label` (header/footer/text/…) | `layout_blocks.block_type` | ✅ |
| `regions[].role` (제목/본문/유의사항/각주/버튼/이미지/고지문구) | `blockType`: `TITLE/BODY/NOTICE/FOOTNOTE/BUTTON/IMAGE/BANNER` | ✅ **거의 1:1** (`고지문구`만 신규 필요, 그쪽 `BANNER`는 우리에 없음) |
| `regions[].role_confidence` | `layout_blocks.confidence_score` | ✅ |
| `page.canvas_w/h`, `parse_route` | `pages[]`, `parser_name` | ✅ |
| — | | |
| **`sections[]` (13종 section_type)** | ❌ **없음** | **계층 유실** |
| `sections[].group_no` / `section_no` | ❌ 없음 | 유실 |
| `regions[].is_illustrative` (**24건 실측**) | ❌ 없음 | 유실 |
| `regions[].card_no` | ❌ 없음 | 유실 |
| `regions[].vlm_reading` + `_score`/`_coverage`/`_relation` | ❌ 없음 | **감사 추적 유실** |
| `lines[].source` (`digital`/`ocr`/`vlm`) | ❌ 없음 | 유실 |
| `page.unassigned_lines` | ❌ 없음 | 유실 |
| `page.notes` / `doc.notes` (진단) | `warnings[]` (code/message/requiresReview) | △ 형식 변환 필요 |

**유실 항목의 무게 차이:**

- **`sections`(13종)** — 저장소 2의 `NormalizedDocument`는 **document → pages → textBlocks / layoutBlocks** 3층이다. 우리는 **document → page → section → region → line** 5층이다. `헤드라인/이벤트안내/경품안내/참여방법/이벤트유의사항/상품유의사항/고지문구/우대혜택/당첨자안내/행동유도/장식예시/상품안내/기타` — 이건 layoutBlock 하나로는 표현 안 된다(여러 region을 묶는 상위 단위). **F-004(광고 화면 구조 분석) 수용기준이 *"주요 문구 영역과 유의사항 영역을 구분해야 한다"*인데, 그 결과를 저장할 자리가 없다.**
- **`is_illustrative`(장식예시)** — 24영역이 실측으로 태깅됐다. 이걸 못 담으면 앱 목업 화면의 가짜 문구가 심의 대상으로 올라간다. handoff §6에 적힌 대로 *"5문서 33영역 소실"* 사고가 났던 자리다.
- **`vlm_reading`** — handoff §3의 핵심 설계(*"OCR은 `10.1%p`, VLM은 `① 0.1%p`로 읽었다"를 둘 다 보관해야 추적됨*). `relation` 5종(`same`/`diverged`/`tail_cut`/`head_drop`/`expanded`)이 실측 값이다. 담을 자리가 없다.

**⚠️ 계약 구현 자체의 문제 3건** (저장소 2 내부 드리프트 — 우리가 흡수하려면 걸린다)

1. **`TextBlock`에 `metadata` 필드가 없다.** ADR-0065는 *"`metadata` | N | parser별 확장 정보"*를 명시하는데, 실제 pydantic 모델은 `extra="forbid"`이고 `metadata` 필드가 아예 없다(`packages/parser-contracts/src/nh_ad_parser_contracts/models.py`(저장소2)). → **`vlm_reading`·`source`·`is_illustrative`를 넣을 확장 슬롯이 실제로는 막혀 있다.**
2. **`LayoutBlock` 필드명이 3곳에서 다르다.** ADR-0065 = `blockType`, DB 명세 = `block_type`, 구현 = `layout_type`(alias `layoutType`). 구현에는 `confidence_status`도 없다(DB엔 있음).
3. **`block_metadata_matches_document` 검증자가 하이브리드 문서를 거부한다.** 모든 TextBlock의 `parser_name`이 문서 `parser_name`과 같아야 한다(`packages/parser-contracts/src/nh_ad_parser_contracts/models.py`(저장소2)). 우리 IR은 **라인 단위로 `source`가 digital/ocr/vlm로 섞인다**(PDF hybrid 라우팅). ADR-0079가 `parser_name=hwp-hybrid`로 병합해 푼 것과 같은 방식(예: `parser_name=paddle-gemma-orch`)이 필요하고, 그러면 **라인별 출처 정보는 계약상 사라진다.**

**⚠️ 그리고 실제 버그 위험 1건:** `normalized_document()`가 문서 confidence를 `min(모든 블록 confidence)`로 계산한다(`apps/parser-services/service.py`(저장소2)). 우리 IR에는 **confidence 0.0 라인이 9건 있다**(빈 텍스트 검출). 그대로 emit하면 문서 confidence = 0.0 → `UNREADABLE` → ADR-0073의 `OCR_UNREADABLE` → **ADR-0024 평가 제외 후보로 자동 강하**된다. 저장소 2의 PaddleOCR 서비스는 `if text:`로 빈 블록을 버려서 이 문제를 우회하지만, min() 자체는 여전히 위험하다(라인 1개가 문서 전체 판정을 끌어내림).

### 3-2. `out/extracted` → `review_items`: **담기지 않는다**

이게 가장 큰 격차다. 우리 실제 산출물(002, 37필드):

```jsonc
"deposit_kind": { "value": "", "evidence": [], "status": "not_found",
  "absence": { "kind": "미표시", "obligation": "필수", "rule": "applicable" } }

"product_name": { "value": "NH행운의7적금", "status": "found",
  "evidence": ["p1_r004","p1_r012","p1_r023","p1_r025","p1_r029","p1_r037"],
  "evidence_score": 1.0, "evidence_backed": true }
```

`review_items` 컬럼과 대조:

| 우리 필드 | `review_items` 컬럼 | 상태 |
|---|---|---|
| **`field_key`** (`deposit_kind`) | ❌ **없음** — grep 0건. `review_type` 7종만 있고 필드 식별자 컬럼이 DB·API 어디에도 없다 | **❌ 치명** |
| `value` | `target_text`? (의미가 다름 — 그쪽은 *검토 대상 문구*) | ❌ 부적합 |
| `status` (`found`/`not_found`/`uncertain`) | `result_status` 5종에 흡수 가능 | △ |
| **`absence.kind`** (4분류) | ❌ 없음 (§3-3 참조) | **❌** |
| **`absence.obligation`** (필수/권장) | ❌ 없음 — `evidences.rule_type`에는 있으나 결과 행에 없음 | ❌ |
| **`absence.rule`** (4가지 판정규칙) | ❌ 없음 | ❌ |
| `evidence` (**region_id N개**, 최대 6) | `ocr_block_id` / `layout_block_id` **단일 컬럼** | **❌ 카디널리티 불일치** |
| `evidence_score`, `evidence_backed` | ❌ 없음 (`confidence_score`는 판정 신뢰도로 의미가 다름) | ❌ |
| `note` (LLM 서술) | `reason` | ✅ |
| `group` (G1~G5) | ❌ 없음 | ❌ |
| `events[]` (이벤트별 11필드 배열) | ❌ 없음 | ❌ |
| `observations` (9종 × 후보 배열) | `review_type=MISLEADING_EXPRESSION` 행들로 분해 가능 | △ |
| `unmapped[]` | ❌ 없음 | ❌ |
| `unused_figures`, `status_corrections`, `input_gap`, `review_gaps`, `coverage`(17지표), `group_analysis`, `errors` | ❌ 전부 없음 (`result_json` jsonb에 던져넣는 것 외에) | ❌ |

**그리고 구조적으로 더 심각한 문제**: `review_items`가 어떻게 만들어지는지 실제 코드를 보면

```python
# apps/worker/src/nh_ad_worker/results.py:145
items = [self._rule_item(document, block, index)
         for index, block in enumerate(document.text_blocks, 1)]
```

**텍스트 블록 1개 = review_item 1행.** 즉 **광고에 존재하는 문구만 행이 된다.**
→ *"예금자보호 문구가 없다"* 같은 **부재 지적은 행이 생길 수가 없다.**

그런데 저장소 2 자신의 요구사항은 이걸 요구한다:
- F-006 처리규칙 4: *"필수 문구가 없거나 의미상 부족한 경우 '누락 의심'으로 표시한다"*
- KPI `REQUIRED_PHRASE_ACCURACY` 오답 정의: *"**누락을 통과로 보거나**, 존재하는 필수 문구를 누락으로 판정"* — 목표 80%
- ADR-0051: *"좌표 또는 텍스트 위치가 없는 검토 항목도 `review_items`에는 반드시 남긴다"* + `DOCUMENT_LEVEL_ISSUE`/`LIST_ONLY`

**→ 명세는 부재를 요구하고, 화면 표시 정책(ADR-0051)까지 준비돼 있는데, 데이터 모델과 구현이 부재를 만들 수 없다.** 우리 PoC가 실제로 채운 게 정확히 이 구멍이다(002 실측: `미표시` 4건, `해당없음` 9건).

**`evidence`라는 단어가 두 저장소에서 정반대를 뜻한다** — 회의에서 먼저 정리해야 할 용어 충돌:

| | 뜻 | 저장 |
|---|---|---|
| 저장소 2 `evidence` | **규정 근거** (법령 조문·매뉴얼 청크) | `evidences`, `review_item_evidences` |
| 우리 `evidence` | **광고 안의 위치** (어느 region에서 봤나) | `fields[].evidence: ["p1_r004", …]` |

저장소 2에서 우리 개념에 해당하는 건 `ocr_block_id`/`layout_block_id`/`annotations`다. 이름을 안 맞추면 API 설계에서 반드시 사고가 난다.

### 3-3. 의외의 수렴 — `result_status` ↔ `absence.kind`

| 저장소 2 `result_status` | 우리 `absence.kind` | 뜻 |
|---|---|---|
| `NEEDS_REVISION` (수정 필요) | **미표시** | 표시 의무 있는데 없음 — **진짜 지적** |
| `NOT_APPLICABLE` (해당 없음) | **해당없음** | 이 상품 유형엔 개념이 없음 |
| `NEEDS_CONFIRMATION` (확인 필요) | **확인필요** | 유형 미정으로 판단 보류 |
| `REVIEW_EXCLUDED` (평가 제외) | **판정제외** | 애초에 심의 대상 아님 |
| `APPROPRIATE` (적정) | (`status=found`) | 값이 있고 문제 없음 |

**독립적으로 도출됐는데 4:4로 맞는다.** 이건 두 설계가 같은 업무 현실을 봤다는 증거고, 통합 시 최대 자산이다.

**차이는 레이어다:**
- 저장소 2: `result_status`는 **검토 항목의 판정 결과** (심의판정 단계 산출)
- 우리: `absence.kind`는 **필드 부재의 원인 분류** (추출 단계 산출, 모델 호출 없이 순수 코드)

우리 4분류는 *"판정의 입력"*이고 그쪽 5분류는 *"판정의 출력"*이다. **`미표시`가 자동으로 `NEEDS_REVISION`이 되는 건 아니다** — 규정 해석이 한 단계 남아 있다. 이 구분을 흐리면 handoff §4에서 경고한 *"규정 해석이 파싱 코드에 박히는"* 사고가 난다.

### 3-4. `out/llm_view`는 갈 곳이 없다

저장소 2의 RAG(Qdrant/OpenSearch)는 **기준자료 인덱스**다(`evidence_chunks`, `review_cases`, `product_documents`). **광고 본문을 벡터 인덱싱하는 컬렉션이 없다** — 광고는 검색 대상이 아니라 검색 질의(query)다.

우리 handoff §3은 `llm_view`의 용도를 *"RAG 인덱싱"*으로 적었는데, 저장소 2 구조에서는 그게 성립하지 않는다. 실제 용도는 **STAGE_3 추출의 LLM 입력**이고, 그 입장에서는 `parser_artifacts`의 중간 산출물(`artifact_type=NORMALIZED_DOCUMENT` 또는 신규 코드)로 Object Storage에 두는 게 맞다. **회의 안건 1번(뭘 넘길지)의 답이 여기서 한 칸 바뀐다.**

---

## 4. 중점 ③ — 파서 서비스, 겹치는가 흡수 자리인가

### 4-1. 저장소 2 파서 서비스의 실체

`apps/parser-services/`는 4개 컨테이너다(opendataloader-pdf / paddleocr / rhwp / document-processor). 이미지·스캔PDF를 담당하는 `paddleocr_app.py` **전체 210행**을 읽은 결과:

```python
self._ocr = PaddleOCR(use_angle_cls=True, lang="korean", use_gpu=False, show_log=False)
...
page_rows = [self._ocr.ocr(str(image), cls=True)[0] or [] for image in sources]
...
return normalized_document(request, engine=self.name, version="v1",
                           pages=pages, blocks=blocks)   # layout_blocks 인자 없음 → []
```

| 항목 | 저장소 2 | 우리 |
|---|---|---|
| 엔진 | `PaddleOCR` (라인 검출·인식) | `PP-StructureV3` (검출·인식·**레이아웃 블록**) |
| 레이아웃 | **`layoutBlocks: []`** (빈 배열) | 레이아웃 블록 + 폴백 군집 → 영역 조립 |
| 타일 분할 | **없음** — 전체 이미지 1회 | **밀도 기반 타일** (에지 대비, 높이상한 1600px) |
| 세로 긴 광고 (6111px) | 1회 처리 (det 다운스케일에 그대로 노출) | 밴드 분할 후 타일당 1회 |
| 방향 분류기 | **`use_angle_cls=True`** | ⚠️ 우리 실측으로 **광고물 파싱을 망가뜨리는 기본값** |
| PDF 렌더 | `pdftoppm -r 200` **고정 DPI** | scan_like는 **내장 래스터 원본 해상도**(업스케일 금지) |
| PDF 디지털 텍스트 | ❌ (PaddleOCR 경로는 항상 래스터) | `structured`면 **OCR 안 돌림**, `hybrid`면 디지털 우선 병합 |
| GPU | `use_gpu=False` | GPU 서버 |
| 역할·섹션 판정 | ❌ 없음 (모든 블록 `textBlockType="BODY"`) | VLM 페이지당 1회, 13종 섹션 |
| 교정·누락회수 | ❌ 없음 | 밴드 통독 + 스윕 + 중복정정 + 저신뢰 재판독 |
| 분류 | ❌ 없음 (사용자 입력) | VLM 문서당 1회 (9/9 실측) |
| 단계 수 | **1** | **17** |

**결론: 기능이 겹치지 않는다.** 저장소 2의 것은 계약을 성립시키기 위한 최소 구현(thin slice)이고, 우리 것은 그 자리에 들어갈 실물이다. `use_angle_cls=True`와 무타일·고정DPI는 우리가 실측으로 확인한 실패 조합이므로, **그대로 두면 실제 광고물에서 품질이 안 나온다.**

### 4-2. 흡수 자리는 설계돼 있다

```
DocumentIngestionService → ParserRouter → {Adapter} → NormalizedDocument v1
                                                     → ReviewPipeline / RAG / Annotation
```

- 계약 지점: `POST /v1/parse` (`sourceFileId`/`reviewId`/`fileName`/`mimeType`/`contentBase64`) → `NormalizedDocument v1`
- 우리는 **5번째 엔진 컨테이너**로 들어간다: `Dockerfile.paddle-gemma-orch` + `paddle_gemma_app.py` + `service.py::app_for(engine)` 재사용
- ADR-0072가 요구하는 교체 원칙(*"업무 로직은 엔진 SDK output을 직접 읽지 않는다"*)에 우리가 부합한다 — 우리 IR은 어차피 자체 포맷이니 어댑터에서 변환하면 됨
- ParserRouter 설정 파일화가 이미 후속 조치로 잡혀 있어(ADR-0072 후속조치) 라우팅 추가 비용이 낮다

**그러나 마찰 4가지:**

| # | 마찰 | 근거 | 무게 |
|---|---|---|---|
| 1 | **VLM의 지위가 정반대** | ADR-0073: *"이미지/스캔 PDF에서 OCR 누락 → **VLM OCR 보조 재처리 후보**. 단, 외부 AI 입력 가능 자료에 한정"* / ADR-0072 대안 D *"모든 문서를 VLM/OCR 중심으로 처리 → 비용·속도·재현성·온프렘 제약이 커 **기각**"* | **🔴 최대** — 우리는 VLM 9단계가 **필수 경로**다. 그쪽 설계에서는 예외 경로다 |
| 2 | **계약 표현력** | `NormalizedDocument`에 section/role/card/is_illustrative/vlm_reading 자리 없음 + `TextBlock.metadata` 부재 | 🔴 |
| 3 | **시간 예산** | ADR-0059 OCR/Parser 단계 timeout **10분**, 전체 Job **30분** / 우리 실측 파싱 3~7분 + STAGE_3 5분 = **최대 12분**, 추론서버 공유로 **2.5~4배 흔들림** | 🟠 초과 가능 |
| 4 | **활성화 스위치** | ADR-0081: 파서는 `NH_PARSER_SERVICES_ENABLED=true`(기본 on), 외부 AI는 `NH_EXTERNAL_AI_ENABLED=false`(기본 off) | 🟠 **우리 파서가 VLM을 쓰므로 "파서인데 AI가 필요"한 경우가 생긴다 — 스위치 경계를 깨뜨림** |

4번이 미묘하지만 실질적이다. 저장소 2의 provider-free 릴리스 게이트(`release-readiness.yml`)는 **AI 없이 파서·규칙 경로가 완주하는 것**을 배포 조건으로 쓴다. 우리 파이프라인이 파서 자리에 들어오면 그 게이트가 성립하지 않는다. → **VLM 없는 축소 모드(OCR만, 역할판정·통독 생략)를 우리가 제공할지**가 정해져야 한다.

---

## 5. 중점 ④ — ADR이 회의 안건 5개를 이미 정했나

| # | handoff §0 안건 | 내 제안 | 저장소 2 결정 | 판정 |
|---|---|---|---|---|
| **1** | 세 산출물 중 뭘 넘길지 | 셋 다 (RAG=llm_view, DB=extracted, 화면=json) | **부분 결정.** ADR-0065: 업무 로직은 `NormalizedDocument` v1만 사용, raw는 ADR-0067로 Object Storage 보존 + DB엔 참조 ID만. `parser_artifacts.artifact_type`에 `NORMALIZED_DOCUMENT`/`OCR_RAW`/`LAYOUT_RAW`/`WARNING_DETAIL` | **🟡 절반 결정** — IR과 llm_view는 artifact로 갈 자리 있음. **extracted는 자리 없음.** llm_view의 "RAG용"이라는 우리 전제는 성립 안 함(§3-4) |
| **2** | 산출물 연결 키 (`region_id` + 처리버전) | region_id 단독 금지 | **미결정.** ADR-0028은 ID **형식**만 정함(UUID PK + Prefix 외부ID). **재처리 간 안정성은 다루지 않음.** 단 `ocr_block_id`/`layout_block_id`가 `review_id`·`file_id` FK를 갖고, `parser_artifacts.attempt_no`/`is_selected_output`이 시도를 구분 | **🟢 우리 제안과 정합** — 사실상 `review_id`가 우리가 말한 "처리 버전"이다. **명문화만 안 돼 있음** |
| **3** | 부재 분류 DB 컬럼 (`status` + `absence.kind`) | 두 컬럼 | **미결정.** `result_status` 5종이 의미상 대응하지만 **레이어가 다름**(항목 판정 결과 vs 필드 부재 원인). `absence` 컬럼·`field_key` 컬럼 **둘 다 없음** | **🔴 미결정 + 우리가 더 앞서 있음** |
| **4** | 재처리 정책 (버전 누적, 덮어쓰기 금지) | 버전 누적 | **부분 결정.** ADR-0059 기술 retry(3회, 1/3/10분 backoff, `FAILED_FINAL`) / ADR-0073 품질 재처리(`attempt_no`, `is_selected_output`, `rerun_reason_code`) / ADR-0074 평가 snapshot **불변** + `snapshotHash` / `reviews.review_round` + `advertisement_revisions` | **🟡 절반** — **누적 구조는 이미 있다**(review_round, attempt_no, evaluation snapshot). 그런데 **"같은 입력에 같은 출력이 안 나온다"는 사실 자체는 어디에도 안 적혀 있다.** ADR-0070은 오히려 deterministic ID로 idempotent upsert를 전제(검색 인덱스 한정) |
| **5** | 심의 판정 단계 위치 | 미정 — 논의 필요 | **결정됨.** ADR-0013: Rule / RAG / LLM / **Review Orchestrator** 4역할 분리, 최종 판단은 준법감시 담당자. ADR-0018: 위험도 산정(Rule 우선, 불확실은 `CHECK_REQUIRED`). 위치는 **worker**(`apps/worker/results.py::ReviewResultEngine`), 단계 코드 `RULE_REVIEW`→`RAG_REVIEW`→`RESULT_GENERATION` | **🟢 결정 완료** — 우리 산출물의 끝은 **추출까지**. 판정은 worker의 Orchestrator |

**요약: 5개 중 1개는 이미 결정(#5), 2개는 절반(#1·#4), 1개는 우리 제안과 정합하나 미문서화(#2), 1개는 완전 미결정이며 우리가 앞서 있다(#3).**

**#5가 결정돼 있다는 게 실무적으로 가장 큰 소득이다.** 우리 산출물의 경계가 확정된다:
- `observations`(G4) 9종을 판정하지 않고 후보로만 모으는 우리 설계 = **ADR-0013과 정확히 일치** (*"LLM 출력은 최종 판단이 아니라 추천/설명/초안"*)
- `미표시` 분류까지가 우리 끝, `NEEDS_REVISION` 판정은 Orchestrator 몫

---

## 6. 저장소 2엔 있는데 우리가 놓친 것

| 항목 | 저장소 2 근거 | 우리 상태 | 무게 |
|---|---|---|---|
| **검토유형 7종 중 4종 미커버** | `review_type`: `PRODUCT_CONSISTENCY`, `VISIBILITY`, `EVIDENCE_MATCHING`, `OCR_QUALITY` | 우리는 `REQUIRED_PHRASE`(G3) / `INTEREST_RATE`(G2) / `MISLEADING_EXPRESSION`(G4) 3종만 | 🔴 |
| ↳ **시인성 검토** (F-010) | `layout_blocks.style_json` (글자 크기·색상·굵기) | **우리 IR에 스타일 정보가 전혀 없다.** 글자 크기·색·굵기를 안 뽑는다 | 🔴 입력부터 없음 |
| ↳ **상품설명서·약관 정합성** (F-009) | `advertisement_files.file_type`: `PRODUCT_DESCRIPTION`, `TERMS`, `ADDITIONAL` | **광고 1개만 입력받는다.** 2차 대조 파일 개념이 없음 | 🔴 |
| **기준일·버전 고정** | `reviews.standard_effective_date` + `applied_standard_version_ids` (jsonb, NN), ADR-0040 | 스키마에 `effective_date` 없음. 근거대장 `open_questions` **Q2가 정확히 이 문제**(2026.9 시행 예정 항목) | 🟠 |
| **위험도 산정** | `risk_level` 4종 + `risk_policy_version` + `risk_reason_codes` + `risk_score_detail`(Rule/RAG/LLM/confidence 입력 전체), ADR-0018·0068, `risk-assessment-criteria.md` | 없음 (판정 단계이므로 정상) | 🟢 경계 밖 |
| **Annotation 표시 정책** | ADR-0051: `BOX`/`TEXT_HIGHLIGHT`/`LIST_ONLY`/`UNAVAILABLE` × `LOCATED`/`PARTIALLY_LOCATED`/`NOT_LOCATED`/`LOW_CONFIDENCE`/`DOCUMENT_LEVEL_ISSUE` | **우리 §5-④ "HWP는 좌표가 없어 화면 하이라이트를 못 한다"의 답이 여기 있다** — `textPath` + `normalized offset`으로 하이라이트 (ADR-0052) | 🟢 **가져올 것** |
| **신뢰도 임계값 정책** | ADR-0053: OCR 0.80/0.50, Parser structure 0.75/0.50, Annotation 0.80/0.50 + `confidence_policy_version` | 우리 저신뢰 재판독 임계 `conf<0.80` — **우연히 일치**. 단 버전 관리 없음 | 🟢 정합 |
| **파서 버전 4종 기록** | `parser_name`/`parser_version`/`parser_rule_version`/`ir_version` (NN, 블록마다) | 우리 IR에 버전 필드 없음 | 🟠 쉬움 |
| **평가 체계** | KPI 4종(`REQUIRED_PHRASE_ACCURACY` 80% / `MISLEADING_EXPRESSION_ACCURACY` 75% / `EVIDENCE_PRECISION` 85% / `HUMAN_AGREEMENT_RATE` 75%), 부분정답 0.5, 평가제외 7코드, `validation_datasets`/`validation_judgments`/`evaluations` + snapshot hash (ADR-0074) | 우리는 `evaluate.py`(분류/섹션/문장) + `verify_extract.py`(필드회수 44/44). **KPI 4종 중 어느 것도 직접 산출하지 않는다** | 🔴 §7 참조 |
| 문구추천 / 의견초안 / 리포트 / 수정전후비교 / Q&A | `suggestions`, `opinion_drafts`, `reports`, `comparisons`, `qa_*` + ADR-0038·0039·0054 | 없음 (범위 밖) | 🟢 |
| 권한·감사·부서 scope | ADR-0055·0022·0036, `audit_logs` | 없음 | 🟢 |
| 폐쇄망 반입 | Q77 **미결정** (저장소 2도 미정) | 우리도 미정 | 🟡 양쪽 미정 |

### ⚠️ 평가 기준 정렬이 안 돼 있다 (가장 놓치기 쉬운 항목)

우리 지표와 그쪽 KPI가 **한 개도 같은 걸 재지 않는다:**

| 우리 지표 | 그쪽 KPI |
|---|---|
| 분류 9/9, 섹션 37/44, 문장 237/242 (`evaluate.py`) | (대응 없음 — 파싱 품질은 KPI가 아님) |
| **필드 회수 44/44** (`verify_extract.py`) | (대응 없음) |
| — | `REQUIRED_PHRASE_ACCURACY` 목표 **80%** |
| — | `MISLEADING_EXPRESSION_ACCURACY` 목표 **75%** |
| — | `EVIDENCE_PRECISION` 목표 **85%** |
| — | `HUMAN_AGREEMENT_RATE` 목표 **75%** |

**그런데 `REQUIRED_PHRASE_ACCURACY`의 정답 기준이 우리 산출물과 정확히 겹친다:**

> 정답(1.0) = *"필수 문구의 **존재/누락/불충분** 상태를 정답지와 동일하게 판정"*
> 오답(0) = *"**누락을 통과로 보거나**, 존재하는 필수 문구를 **누락으로 판정**"*

이건 우리 `status`(found/not_found) + `absence.kind`(미표시/해당없음)가 정확히 재는 것이다. **우리 44/44는 "회수"만 잰 값이고, 이 KPI는 "부재 판정의 정확도"를 잰다** — 우리가 002에서 `미표시` 4건 / `해당없음` 9건을 낸 그 판정이 맞았는지를 채점하는 지표다. 우리 `verify_extract.py`는 아직 그걸 채점하지 않는다.

또한 gold set 형태가 다르다. 우리 `gold/*.yaml`은 (sections + must_contain) 즉 **파싱 정답지**다. 그쪽 `validation_judgments`는 (target_text, review_type, expected_status) 즉 **판정 정답지**다. ADR-0074가 *"공식 원천은 DB, Git fixture는 회귀 테스트용"*으로 못박아 뒀으므로, **우리 gold는 DB 정답지로 승격되지 않고 회귀 테스트 자산으로 남는다.**

---

## 7. 우리가 앞서 있는 것 — 저장소 2가 아직 못 가진 실측 자산

| 자산 | 실측값 | 저장소 2 상태 |
|---|---|---|
| **부재 4분류 + 판정규칙 4종** | 002: 미표시 4 / 해당없음 9. 모델 호출 0회(순수 코드) | 데이터 모델 없음. KPI는 요구, 구현은 불가 |
| **근거 대장 44필드 × 조문** | `field_key`·`check`·`basis{law,article,source_doc,source_type}`·`applies_when`·`status` | `evidences` 테이블은 비어 있음. **우리 대장이 seed** |
| **이벤트=오버레이 판정** | 002·003 실측 (둘 다 예금성+이벤트페이지) | `EVENT_FINANCIAL_PRODUCT` enum이 모순 상태 |
| **VLM 비결정성 정량화** | 문장 회수 **235~237**, 섹션 37~38. `temperature=0`으로 안 잡힘 | ADR 어디에도 없음. ADR-0070은 오히려 결정론 전제 |
| **region_id 불안정 실측** | 76영역 재처리 시 **같은 id·다른 내용 4건** (r018/019/022/026) | ADR-0028은 형식만 다룸 |
| **OCR 정본 / VLM 후보 분리** | `vlm_reading_relation` 5종. VLM이 정본을 덮지 않음 | 개념 없음 |
| **밀도 기반 타일 분할** | 상한 제거 시 레이아웃 블록 84→62 (002) | 없음 (전체 1회 OCR) |
| **환각 검산** | `evidence_backed`. 003 실측: `NH올원모임서비스`→`NH클럽온뱅크` 지어냄 | 없음 |
| **표기 불일치 흡수 탐지** | `_unused_figures`. 003: "총 1,000명" vs "(선착순 1,000팀)" | 없음 |
| **장식예시 격리** | 24영역 태깅. 제외했을 때 33영역 소실 사고 | 개념 없음 |
| **파싱 결함 정량화** | 겹친 영역 4페이지 37쌍(32쌍 섹션갈림), 회전텍스트, 스윕 환각 | 없음 |
| **VLM 호출 비용 실측** | 문서당 3~7분, **96~98%가 모델 대기**. 단계별 호출수·초 표 | ADR-0042는 조회 P95만(1.5~3초) |
| **강제 스키마 결함 2종** | 배열 선두 퇴행 → analysis 선행 필드로 해소 / 종료부 역슬래시 오발행 → 복구 | 없음 (`INVALID_SCHEMA`로만 처리) |
| **좌표 계약 적합성** | 225영역 0위반 (실측 확인, 위 §3-1) | — |

---

## 8. 먼저 맞춰야 할 것 — 우선순위

### P0 — 이걸 정하지 않으면 어느 쪽도 다음 줄을 못 쓴다

| # | 정할 것 | 선택지 | 영향 |
|---|---|---|---|
| **P0-1** | **`product_group` 레벨** | (a) `DEPOSIT_LIKE` + `product_subtype` 컬럼 신설 (**우리 실측 근거**) / (b) `DEPOSIT`/`SAVINGS`/`DEMAND_DEPOSIT` 유지 + 근거자료 3배 적재 | 기준자료 적재·스키마 파일 구조·조건부 판정 전부 |
| **P0-2** | **`EVENT_FINANCIAL_PRODUCT` 제거 여부** | 제거하고 `advertisement_type=EVENT_PAGE`로 통일 (우리 002·003 실측) | `advertisements` NN 컬럼 결정 불가 문제 해소 |
| **P0-3** | **필드 단위 추출 결과를 어디에 담나** | (a) `review_items`에 `field_key` + `absence_kind` + `obligation` **컬럼 추가** / (b) 신규 테이블 `extracted_fields` / (c) `result_json` jsonb에 밀어넣기(**계약 밖 → 비추천**) | **DB 명세 개정 필요.** F-006·KPI가 이거 없이는 성립 안 함 |
| **P0-4** | **`evidence` 용어 분리** | 우리 것 → `source_regions` / `ad_locations` 등으로 개명. 그쪽 `evidence`는 규정근거 전용 | API·DB·화면 전체 |

### P1 — 파서 흡수를 시작하려면

| # | 정할 것 | 메모 |
|---|---|---|
| **P1-1** | **VLM이 필수 경로냐 보조냐** | ADR-0073은 "보조·승인자료 한정". 우리는 필수. **ADR 개정 or 새 ADR 필요.** 동시에 provider-free 릴리스 게이트(ADR-0081) 성립 여부 결정 |
| **P1-2** | `NormalizedDocument`에 section 계층 추가 여부 | `sections[]`(13종 section_type) + `is_illustrative` + `card_no`. F-004 수용기준 충족에 필요. **v2 스키마 or `TextBlock.metadata` 부활** |
| **P1-3** | `vlm_reading` 후보를 계약에 남길지 | 남기면 감사 추적 유지. 안 남기면 우리 `parser_artifacts` raw JSON에만 존재 → 화면 대조 기능 소실 |
| **P1-4** | 시간 예산 | OCR/Parser timeout 10분 vs 우리 최대 12분 + 2.5~4배 변동. **timeout 상향 or STAGE_3를 별도 단계로 분리** |
| **P1-5** | 계약 구현 드리프트 3건 수정 | `TextBlock.metadata` 부재 / `LayoutBlock` 필드명 3중 불일치 / 문서 confidence = `min()` |

### P2 — 범위·평가

| # | 정할 것 | 메모 |
|---|---|---|
| **P2-1** | `VISIBILITY`·`PRODUCT_CONSISTENCY`를 우리가 맡나 | 맡으면 **스타일 정보 추출**(크기·색·굵기)과 **2차 파일 입력**이 파이프라인에 새로 필요 |
| **P2-2** | 평가 지표 정렬 | 우리 44/44 → `REQUIRED_PHRASE_ACCURACY`로 환산하는 매핑. gold yaml → `validation_judgments` 변환 여부(ADR-0074상 회귀용으로만 남음) |
| **P2-3** | `effective_date` 도입 | 근거대장 Q1(예금자보호 5천만/1억) Q2(2026.9 로고 시행)가 이걸 요구 |
| **P2-4** | 비결정성 명문화 | 실측 235~237을 ADR로 남길지. ADR-0074 snapshot 정책과 정합(같은 evaluationId 불변)이므로 **충돌은 없고 문서화만 필요** |

---

## 9. 회의 안건 재작성 (handoff §0 → 저장소 2 기준)

| 기존 # | 새 상태 | 회의에서 실제로 할 일 |
|---|---|---|
| 1 산출물 인계 범위 | 🟡 절반 결정 | `parser_artifacts.artifact_type`에 우리 IR·llm_view를 어떤 코드로 넣을지. **`extracted`는 P0-3에 흡수** |
| 2 region_id 안정성 | 🟢 정합, 미문서화 | `review_id` 스코프가 "처리 버전"임을 확인하고 명문화. 논쟁 아님 |
| 3 부재 분류 DB 컬럼 | 🔴 미결정 | **→ P0-3.** 우리 4분류를 컬럼으로 승격하는 DB 명세 개정 제안 |
| 4 재처리 정책 | 🟡 절반 | 누적 구조는 이미 있음(review_round/attempt_no/snapshot). **비결정성 실측만 공유** |
| 5 심의판정 위치 | 🟢 결정 완료 | **논의 불필요.** ADR-0013 Review Orchestrator(worker). 우리 끝은 추출까지 |
| **신규 A** | 🔴 | **P0-1 상품군 레벨** (가장 파급이 큼) |
| **신규 B** | 🔴 | **P0-4 `evidence` 용어 충돌** |
| **신규 C** | 🔴 | **P1-1 VLM 필수/보조 지위** (ADR 개정 사안) |

**→ 5개 안건 중 실제로 논쟁이 필요한 건 3번뿐이고, 새로 올려야 할 게 3개다.**

---

## 10. 미확인 / 추가 검증 필요

정직하게 남긴다.

| 항목 | 왜 확인 못 했나 |
|---|---|
| `apps/frontend` 실제 화면 | 읽지 않았다. `screen-specification.md`(1,371행)·`screen-api-mapping.md`(860행)도 미독 |
| `apps/backend` 구현 | 디렉터리 구조만 봤다. migrations/versions의 **실제 DDL이 DB 명세와 일치하는지 미확인** |
| `opendataloader_app.py` / `rhwp_app.py` / `document_processor_app.py` | PaddleOCR만 읽었다. **HWP 경로(ADR-0079 hwp-hybrid)와 우리 사내 파서 트랙의 관계 미확인** |
| `docs/규정 및 가이드라인/` 원본 | 우리 근거대장의 출처와 **같은 문서인지 대조 안 함** |
| `docs/광고예시/NH농협은행-2026_001-대출성.hwp` | 우리 nh-data와 **같은 샘플인지 확인 안 함** (파일명은 우리 004와 동일 계열) |
| `test-cases.md`(1,069행) | 우리 산출물이 통과해야 할 케이스 목록. 미독 |
| 우리 코드 실제 실행 경로 | 문서(pipeline-map)와 `src/` 실제 코드를 이번엔 재대조하지 않았다 (out/ JSON으로 간접 확인) |

**특히 마지막 두 개는 다음 단계에서 먼저 볼 가치가 있다** — `test-cases.md`가 우리 통합의 합격 조건을 이미 적어놨을 가능성이 높고, `광고예시`가 같은 샘플이면 두 저장소가 같은 데이터로 비교 가능해진다.

---

## 11. 2026-08-07 재확인 — 무엇이 아직 유효한가

> 위 §0~10 은 **2026-07-29 작성분이며 지우지 않았다**(시점 기록). 이 절만 8/7 에 덧붙였다.
> 방법: 대상 저장소를 **읽기만** 했다(`dev` 브랜치, 커밋 `3cfcbb5` 시점).

### 11-1. 결론 — parser 계층은 3주째 안 움직였다

```
git -C <대상> log --oneline --since=2026-07-29 -- apps/parser-services packages/parser-contracts
→ 결과 없음 (커밋 0건)
```

그 사이 `dev` 가 움직인 곳은 프론트엔드 검토화면·Q&A·문서·Notion 동기화였다.
**따라서 §1~§9 의 parser 관련 분석은 재조사 없이 그대로 쓸 수 있다.**

### 11-2. 재확인한 계약 (실제 코드 대조)

| 항목 | 07-29 기록 | 08-07 실제 | 위치 |
|---|---|---|---|
| `ParserAdapter` Protocol | 멤버 2개 | `name: str` + `parse(document) -> NormalizedDocument` — 동일 | `routing.py:51` |
| `paddleocr` 의 `layoutBlocks` | 빈 배열 | 동일 — `normalized_document()` 에 `layout_blocks` 인자를 **아예 안 넘긴다** | `paddleocr_app.py:79` |
| 예약 슬롯 | (미기록) | `"vlm-ocr"`(이미지·외부AI허용 시) · `"mineru"` 가 라우팅에만 있고 구현 없음 | `routing.py:73-77` |
| 문서 confidence | (미기록) | `min(모든 블록 confidenceScore)`, `default=0.0` | `service.py:65` |
| confidence 임계 | (미기록) | `≥0.80` READABLE · `≥0.50` LOW_CONFIDENCE · 그 미만 **UNREADABLE** | `models.py:20-25` |

### 11-3. 새로 확인한 **하드 제약** — 계약이 생각보다 엄격하다

`Coordinate` 는 단순한 좌표 상자가 아니라 검증기가 붙어 있다(`models.py:40-72`):

```
source_width / source_height : gt=0        ← 0 이면 실패
width / height               : gt=0        ← 빈 상자 실패
x + width <= source_width                  ← 경계 이탈 실패
normalized_* 가 source 와 일치 (abs_tol=0.0001)
```

`TextBlock` 은 `coordinate` 와 `text_path` 가 **개별로는 선택**이지만 검증기가 하나를 강제한다:

```python
if self.coordinate is None and not self.text_path:
    raise ValueError("a text block requires a coordinate or textPath")
```

또 `confidence_status` 가 `confidence_score` 와 **일치해야** 한다(`models.py:105`) — 점수는 낮게
주면서 상태만 좋게 적을 수 없다.

### 11-4. 우리 산출물을 이 검증에 넣어 보면 (2026-08-06 무캐시 실행분 전수)

| 대상 | 통과 | 실패 | 실패 내용 |
|---|---|---|---|
| 라인(→`TextBlock`) | **488** | 12 | 전부 004 HWP — `bbox` 없음, `canvas 0×0` |
| 영역(→`LayoutBlock`) | **224** | 1 | 위와 같은 HWP 페이지 |

**폭 0 · 경계 이탈 · 음수 좌표는 0건이다.** 즉 이미지·PDF 트랙 좌표는 손대지 않고 그대로
넘어가고, **막히는 것은 HWP 하나로 국소화된다.**

### 11-5. §10 미확인 항목 중 해소된 것

- `document_processor_app.py` 가 `layout_blocks=layouts` 를 넘기는 유일한 서비스임을 확인
  (`document_processor_app.py:206`) — 즉 레이아웃을 채우는 경로가 대상 저장소에 이미 있다.
  우리 어댑터가 만들 `layoutBlocks` 도 같은 자리에 들어간다.
- 나머지(`test-cases.md`·`광고예시` 동일성·프론트엔드)는 **여전히 미확인**이다.
