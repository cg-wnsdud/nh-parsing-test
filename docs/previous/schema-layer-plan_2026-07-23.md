> 📦 **지난 문서** — 2026-07-23 세션의 착수 전 계획서. §5(다음 할 일)의 항목은 대부분
> 완료되거나 다른 형태로 대체됐다(영역별 VLM 통독 §6 → 밴드 통합판독으로 흡수).
> 현재 스키마 구성은 [../handoff.md](../handoff.md),
> 파이프라인은 [../architecture/pipeline-map.md](../architecture/pipeline-map.md) 참조.

---

# 스키마 추출 계층 — 계획 및 인수인계 (2026-07-23 기준)

> 새 채팅에서 이어서 작업하기 위한 문서. 여기부터 읽고 시작하면 됩니다.
> 관련 배경: [pipeline-overview.md](pipeline-overview_2026-07-20.md), 상위 요구사항은 repo 루트 `project-description.txt`.

## 0. 지금 어디까지 왔나 (파싱 현황)

- `nh-ad-review-poc` 파싱 파이프라인 가동 중. 골드셋 최근 점수: 분류 100% / 섹션 84~86% / 문장 87~88% / 필드 92~98%.
- 파이프라인 순서(요약): PaddleX(PP-StructureV3) 레이아웃 → 영역/라인 조립 → VLM 역할·섹션·묶음(group_no) 판정 → 외톨이 라인 흡수 → VLM 스윕(누락 회수) → 저신뢰 재판독(2b, 유령중복 제거 포함) → 필드 복수관측 병합 → 수치 크롭 재확인 → reconcile → 읽기순서 정렬.
- 산출물 `out/json/*.json` = **최종 파싱 결과(전체 IR)**. 그 안에 **두 층**이 공존:
  - `regions[].lines` = raw 증거층 (OCR/VLM 원문, 감사용)
  - `extracted_fields` = 교정된 정답값층 (수치 재확인·복수관측 병합 반영)
- 검수 도구 `tools/make_review.py` → `out/review.html`. 이번 세션에 **오른쪽 컬럼을 3단으로 개편**:
  1. **최종 파싱 결과 · LLM 전달 형태** (읽기순서·의미묶음 정렬, bbox/신뢰도 제외, 같은 행 박스는 공백으로 이어붙임)
  2. **최종 추출 필드 · 구조화 결과** (교정된 정답값)
  3. **감사용 원본 증거층** (raw 라인 + 출처/신뢰도)

## 1. 이번 세션에서 내린 설계 결정 (근거 포함)

### (A) 스키마-LLM 페이로드 구성 — 텍스트를 고치지 않는다
- **raw 읽기순서 텍스트 + 우리 교정 필드(candidates)를 같이** 넣고, 프롬프트로 "candidates 우선 사용"을 지시. LLM이 raw의 오독(`10.1%p`)보다 후보(`0.1%p`)를 고르게 함.
- 직렬화: `json.dumps`로 프롬프트 텍스트에 삽입. 출력은 `response_format: {type: json_schema, strict: true}`로 디코딩 강제 + pydantic 재검증. (HyundaiHS와 동일 골격 — 아래 §3 참조)
- **bbox는 페이로드에 안 넣되, 라인/영역 ID는 유지** → 추출 후 ID로 bbox 재부착 (F-011/012 시인성·하이라이트 요구 때문). ※ HyundaiHS는 bbox를 아예 버리고 재부착도 안 함 — 우리는 한 걸음 더 감.

### (B) ②(필드 교정 → 라인 텍스트 되쓰기) — 하지 않음
- (A) 때문에 LLM 입력엔 불필요. 라인 부분 치환은 위험 대비 이득도 작음(`10.1%p` 안의 `10.1`만 `0.1`로 바꾸는 식). 필요하면 사람 검수용 "이 라인은 필드 X로 교정됨" 표시 정도로만.

### (C) 스키마 축 = 상품군 (광고유형 아님)
- PoC 범위: 예금성 / 적금성 / 입출금 + 이벤트성 (project-description.txt AC/scope).
- 스키마 = **필드 카탈로그(무엇을 뽑을지, key + 설명 문자열) + 출력 계약(pydantic)**. HyundaiHS 패턴: 필드 설명은 프롬프트에 산문+키리스트로, 반환 모양은 json_schema strict로.
- 스키마 실물은 rag-data 심의규정/체크리스트에서 도출 (아래 §2 — 사용자가 별도 세션에서 파싱).

### (D) 카드-우선 분할 = 별도 실험 트랙
- 현재 카드 묶음(group_no)은 **전체 페이지를 VLM이 눈대중으로 번호 매김** (`vlm_judge.py:judge_region_roles`, 프롬프트 line 98-102). 취약 → 003에서 카드 3개를 묶음 2개로 잘못 셈, 내용 섞임.
- 제안: 카드형이면 **카드 bbox 먼저 확정 → 카드별 크롭 → 각 크롭에 기존 파이프라인** → 원본 좌표로 재조립. 이점: 카드 정체성이 추측→구조, 크롭 해상도↑(회전/장식 헤드라인 판독 유리), 값이 카드 경계 안 넘음. 긴 이미지는 현행 경로 유지(라우팅).
- 003 등 SNS 카드 샘플로 "카드 개수·경계 안정적으로 잡나" 프로토타입 후 판단.

## 2. 스키마 도출용 문서 파싱 (사용자가 별도 Claude Code 세션에서 진행)

- 입력: `nh-data/rag-data/` (심의규정 HWP·PDF + `260713_2_NH_광고심의_체크리스트.xlsx`).
- 목표: 상품군별 **필수문구·금리표시규칙·과장표현금지·정합성기준** 등을 구조화 JSON으로 → 추출 스키마 필드(key+설명)의 근거.
- 주의(사내 파서 document-processor 제약): OCR 없음 → **스캔형 PDF 페이지는 조용히 스킵**(`parse_status="skipped"`, `pdf_scan_like`)되니 반드시 status 확인. **PNG/JPG·xlsx는 최상위 입력 미지원** → xlsx 체크리스트는 openpyxl/pandas로 별도 파싱.
- 그 세션에 넣을 프롬프트는 §5에 있음.
- 산출물이 준비되면(사용자): 상품군별 스키마 JSON을 `nh-ad-review-poc/schemas/` (신설) 아래 둘 예정.

## 3. HyundaiHS 참고 조사 결과 (참고 전용 — 절대 그대로 이식 금지)

입력 데이터 형식이 근본적으로 달라(포장지 mm 단위 등) 코드 직수입 불가. 골격만 참고:

1. **VLM 값 교정은 없음.** 교정=순수 파이썬 OCR 대조(토큰 겹침 `BACKED_TOKEN_RATIO=0.8`, 엔티티 경계 5단계 exact/truncated/near/absent/unverifiable) + WF2 Levenshtein-1 카탈로그 교차확인. page-judge VLM은 후보에서 값을 "생성"할 뿐 재판독 교정 아님. → **우리 `verify_numeric_fields`(크롭 재판독 수치 교정)가 오히려 앞섬.**
2. **교정 저장: 기본은 주석(shadow), 적용 시 덮어쓰되 원본 보존.** `IDENTITY_GATE_SHADOW=true` 기본 → 판정만 `field_quality_shadow`에 기록·미적용. 적용 시 `value` 덮어쓰고 원본은 `corrected_from`에 보존. 누락 복원은 "덮어쓰기 금지".
3. **LLM 페이로드(핵심):** page_bundle(`page_ocr_text`+`chunk_results`+`merged_candidates(ocr_score 포함)`)을 `json.dumps`로 프롬프트에 삽입. 필드 카탈로그는 프롬프트에 산문+키리스트, 출력은 `response_format json_schema strict:true`(+pydantic 재검증). 툴콜링 없음. **좌표는 페이로드에 전혀 없음(텍스트-only), ID 재부착도 없음.**

## 4. 백로그 / 알려진 한계

- 회전·장식 헤드라인 OCR 실패: 002 "최고 연 7.1%" → OCR "최고연"+"777"(conf 0.998로 자신 있게 오독). 신뢰도 높아 2b 미적용, 스윕도 "검출됨"이라 스킵. → 카드-우선 크롭(D)이 완화 후보.
- 원문자(①②) 병합: 올원 "①0.1%p"→OCR "10.1%p", 필드층만 "0.1%p"로 교정(라인 raw엔 남음 — (B)대로 방치).
- 카드 묶음 오류(003) → (D).
- 필드 점수 실행 간 변동(서버측 비결정성, [[vlm-guided-decoding-trailing-escape]] 참조).
- 역할/섹션 판정 변동성(복수관측 미적용, 002 헤드라인↔이벤트안내 뒤바뀜).

## 5. 다음 할 일 (우선순위)

0. **[프로토타입 코드 반영 완료, 실측 대기 — §6 "구현 상태" 참조] 영역별 VLM 통독**: StructureV3로 나눈 각 영역 크롭을 VLM이 읽어 clean text를 만드는 스텝(④+)을 파이프라인에 삽입 완료(`--region-vlm`, 1페이지 게이트). 남은 것: 002 폐쇄망 실행 → OCR vs VLM 대조 육안 판정 → 전체 적용·⑧⑨ 축소.
1. **[사용자, 완료됨]** rag-data 파싱 → `out/schema_source/*.json` (§2, run_schema_source.py). 남은 것: 상품군별 필드 후보 합성(`_product_group_fields.json`).
2. **스키마-LLM 페이로드 구성기(STAGE_3 상당)**: `{영역별 clean text(line/region ID 포함) + candidates + 상품군 스키마}` → `json.dumps` + `response_format json_schema strict`. §6의 영역 통독 결과가 이 입력을 채움.
3. 예금성 스키마 초안(field key + 설명) → 위 구성기에 끼워 001/002/올원e로 시험, 골드셋 대비 측정.
4. 추출 후 line/region ID로 bbox 재부착 → F-011/012 하이라이트 연결.
5. (별도 트랙) 카드-우선 분할 프로토타입 on 003 — §6의 영역 통독을 카드 단위로 올린 상위 버전.

---

## 6. 영역별 VLM 통독 — 파이프라인 변경 설계 (구현 착수용)

**결론:** HyundaiHS의 STAGE_1(`crop_vlm`)이 바로 이것 — StructureV3가 자른 영역을 **VLM이 크롭으로 통독**해 텍스트 1차 출처가 됨. OCR은 좌표+검증(ocr_score)으로 역할 강등. 우리 파이프라인은 이 스텝이 없어서 ⑧(스윕)·⑨(2b 재판독) 같은 OCR 뒷수습 패치로 때워왔음. 이 스텝을 넣으면 그 패치들이 줄고, "자신 있게 틀린 OCR"(002 `최고연/777`, 올원 `10.1%p`)도 근본적으로 개선되며, 모든 영역에 후보가 생겨 스키마 계층(STAGE_2/3)이 성립함.

### 현재 ①~⑩ 대비 변경점

| 단계 | 현재 | 영역별 VLM 통독 도입 후 |
|---|---|---|
| ①② 라우팅·triage | — | **그대로** |
| ③ 타일링·OCR·dedupe | OCR = 텍스트 1차 출처 | OCR 여전히 실행하되 **좌표+검증(ocr_score)으로 강등** |
| ④ build_regions | OCR 줄→레이아웃 박스, 임시 역할 | **그대로**(bbox 앵커 목적) |
| **④+ [신규] 영역별 VLM 통독** | — | **각 Region bbox로 원본 크롭 → VLM이 그 영역 텍스트 통독 → clean text.** OCR 줄은 ocr_score 근거+bbox 앵커로 보존 |
| ⑤ VLM 역할·섹션 | 전체페이지 1회 호출 | 그대로, **또는 ④+와 통합**(영역별 호출이 text+role 동시 반환 → 총 호출 절감) |
| ⑥ 2a 장식격리 | — | **그대로** |
| ⑦ 외톨이 줄 흡수 | 좌표→VLM 내용 2단 | **축소**(영역 밖 텍스트 위주로만) |
| ⑧ VLM 스윕 | 놓친 문구 전체 회수 | **대폭 축소** — 영역 통독이 영역 안 장식/벡터를 이미 잡음. 스윕은 "어떤 StructureV3 영역에도 안 잡힌" 화면 텍스트 안전망만 |
| ⑨ 2b 저신뢰 재판독 | 저신뢰 OCR 크롭 재판독 | **거의 불필요** — VLM이 영역을 이미 읽음. 71·10.1%p류도 직접 해결. (유령중복 제거 로직도 자연 소멸) |
| ⑩ 필드·정합 | 몇 키만 후보 | candidates가 **전 영역 통독**에서 나옴 → 전 텍스트 커버, STAGE_2/3로 연결 |

### 구현 포인트

- **신규 함수**: `vlm_direct.py`의 `transcribe_line_crop`(2b용, 한 줄 크롭 재판독)을 일반화 → `transcribe_region_crop(region_bbox 크롭 → 읽기순서 clean text [+ 임시 role])`. 프롬프트는 "이 크롭 안 텍스트를 보이는 순서대로 그대로 옮겨 적어라(장식·회전 포함)".
- **삽입 위치**: `pipeline.py`의 `_apply_vlm_judgments` 흐름에서 ④(build_regions) 다음. region의 텍스트를 VLM 통독 결과로 대체하거나 병행 — **원본 OCR lines는 ocr_backed 근거로 보존**(source 구분: `ocr` 유지 + 새 `vlm_region` 추가).
- **ocr_score**: 기존 `field_judge`의 토큰비율(`BACKED_TOKEN_RATIO=0.8`) 재사용해 VLM 텍스트 vs OCR 대조 → 환각 감시.
- **출력 형식**: region에 VLM 통독 clean text + OCR lines(증거) 병존. bbox는 StructureV3/OCR 것 유지(F-011/012). = §1(A)의 "raw+candidates" 구조가 여기서 자연 충족됨.
- **스키마 유무 무관**: 지금은 스키마가 없으니 영역 통독 = **텍스트 전사(transcription)**로 시작. 스키마 생기면 HyundaiHS처럼 영역 통독을 필드지향(observations)으로 승격 가능.

### 트레이드오프 / 리스크 (정직하게)

- **비용**: 페이지당 VLM 호출이 영역 수만큼 증가. (완화: 작은 영역 병합, 배치 호출.)
- **StructureV3 커버리지 의존**: 영역 자체를 못 잡으면 VLM도 못 봄 → 그래서 ⑧ 스윕을 "안전망"으로 남겨둠.
- **VLM도 회전·장식 완벽 X**: 하지만 OCR 박스 조각화보다 우위. ocr_score로 교차 감시.

### 첫 프로토타입 (새 탭에서 여기부터)

1. `transcribe_region_crop` 추가(기존 `transcribe_line_crop` 복제·일반화).
2. 파이프라인 ④ 다음에, **한 파일(002 또는 003) 1페이지에만** 영역 통독 적용(플래그로 on/off).
3. review.html에서 OCR 라인 vs VLM 통독 텍스트를 나란히 비교 → 회전 헤드라인/카드 텍스트가 실제로 깨끗해지는지, ocr_score로 환각 없는지 확인.
4. 좋으면 전체 적용 + ⑧⑨ 축소.

#### 구현 상태 (2026-07-23, 착수 세션)

**1·2 코드 반영 완료** (아직 폐쇄망 실행 전 — 3번 실측은 사용자 몫). 커밋 전 상태:
- **결정 ①**: `ir.py` `Source` 리터럴에 `"vlm_region"` 추가.
- **결정 ②(핵심, 병존 방식 확정)**: 통독 clean text 를 `region.lines`(본문·다운스트림 소비)로 승격, 원본 OCR 라인은 신설 `Region.ocr_lines`(증거층, 필드추출·역할판정 **미순회**)로 강등. 같은 리스트 공존 금지 → 필드 이중 오염 원천 차단(2b는 OCR 라인이 사라져 자연 무력화 = ⑨ 축소).
- 신규 `vlm_direct.transcribe_region_crop(bbox, canvas) -> (text, conf)`: 영역 크롭 전체를 읽기순서대로 통독(회전·장식 포함), 실패/형식파손/빈 크롭이면 `("", None)`.
- 신규 `pipeline._transcribe_regions_vlm(page, canvas)`: `_apply_vlm_judgments` 맨 앞(역할판정 ⑤ 직전)에서 실행. bbox+라인 있는 영역만 대상(순수 이미지 영역은 스윕에 위임). `field_judge.check_field_consistency(reading, ocr_joined)`로 ocr_score(=통독 토큰의 OCR 근거 비율) 산출해 **note 기록**(조용한 수정 금지). 통독 bbox 는 StructureV3 영역 bbox 앵커 유지(F-011/012).
- **플래그 배선**: `process_file(..., region_vlm=False)` → 3개 `_process_*` → `_apply_vlm_judgments(..., region_vlm=)`. `page_no==1` 에서만 발동. `run_nhdata.py --region-vlm`.
- 비용 가드: `SETTINGS.region_vlm_max_per_page=16`.
- 표시: `make_review.py` 에 `vlm_region` 출처 태그(R) + **"영역별 VLM 통독 대조"** 블록(좌 OCR 원문↔우 VLM 통독) 추가 — 3번 판정용, 표시 전용.
- 테스트: `tests/test_region_vlm.py`(+`conftest.py`) — 폐쇄망 불필요(VLM monkeypatch). 병존 분리·빈결과 OCR유지·ocr_score note 검증. **2 passed.**

**실행법**: `uv run python tools/run_nhdata.py --only 002 --region-vlm` (WireGuard VPN + PaddleX/Gemma 필요) → `uv run python tools/make_review.py --only 002` 로 대조 확인.

**남은 판단(3·4)**: 002 회전 헤드라인이 통독으로 실제 깨끗해지는지 + ocr_score 로 환각 없는지 육안 확인 후, 좋으면 `page_no==1` 게이트 해제·전체 적용하며 ⑧⑨ 축소.

#### 실측 결과 (2026-07-23, 002 폐쇄망 1회 실행 — 3번 판정 완료)

`run_nhdata.py --input ../nh-data/sample-data --only 002 --region-vlm --out out_regionvlm` (샘플이 nh-data/sample-data/ 로 이동됨 — 러너 기본 input=nh-data 는 비재귀라 하위폴더 못 봄, `--input` 필수). review: `make_review.py --only 002 --src out_regionvlm` (신규 `--src` 로 out 폴더 분리 지정).

- **핵심 성공(회전 헤드라인)**: r008 OCR `최고연71%`(소수점 소실 — 메모리에 기록된 그 실패) → 통독 `최고연7.1%` 로 **복원**, 틀린 OCR 은 `ocr_lines` 증거로 보존. 결정 ② 병존 구조가 out/json 에 설계대로 저장됨(`lines`=본문 vlm_region, `ocr_lines`=OCR). 덤: r001 `777명에게 쓴다!`→`쏜다!` 교정. 최종 필드 `[금리] 최고 7.1%,세전` + judge 가 헤드라인 중복 `최고연 7.1%` 자동 제거, '777' 은 경품수(총 777명)로 정확 분리(금리 오염 아님).
- **한계 1 — 커버리지 상한**: 42영역 중 **16영역만 통독**(region_vlm_max_per_page=16 히트). page.regions 순서(대략 상→하)라 상단 헤드라인/이벤트부는 다 잡혔지만 하단 유의사항 26영역은 OCR 유지 → 2b(⑨)가 거기서 계속 동작(무력화는 부분적). **전체 적용 전 cap 상향 또는 "저신뢰·회전 의심 영역 우선" 정렬 필요.**
- **한계 2 — ocr_score 이분화**: 공백 없는 짧은 문자열(`최고연7.1%`)은 토큰 1개라 부분일치가 안 잡혀 0/1(=0.00 or 1.00)로만 나온다. r008 이 0.00 인 건 환각이 아니라 "VLM 이 OCR 밖 내용을 추가(=교정)"라는 뜻 — 감시 신호로는 작동하나 등급이 거칠다. 향후 문자단위 비율 or 숫자/한글 경계 분할로 개선 여지.
- **비용**: 단일 페이지 **~13분**(783s) — 통독 16회 + 기존 스윕2·역할·필드·2b. 예상된 트레이드오프(§트레이드오프), 전면 적용 시 배치/병렬 필요.
- 산출물: `out_regionvlm/json/…002….json`, `out_regionvlm/review_002.html`(대조 블록 포함).

#### 3파일 추가 실측 (2026-07-23, 같은 세션) — 002 재측정(region-vlm 끔) + 올원e적금 + 003 동시 실행

**핵심 정정**: 위 002 단독실행의 783초(13분)는 **대표값이 아니라 이상치**였다. 같은 조건으로 재측정한 결과:

| 실행 | region-vlm | 규모 | 소요시간 |
|---|---|---|---|
| 002 기준선 | 꺼짐 | 1p, 42영역 | 291s |
| 올원e적금 | 켜짐(16회 통독) | 1p, 46영역 | 284s |
| 003 PDF | 켜짐(16회 통독, 1p만 적용) | 3p, 총45영역 | 343s |
| (참고) 002 이전 단독실행 | 켜짐 | 1p, 42영역 | 783s(이상치) |

region-vlm 켜짐/꺼짐 차이가 이번엔 **거의 없었다**(284~343s vs 291s) — 기존 파이프라인 자체가 이미 다회 VLM 호출 구조(역할판정·스윕2회·2b·섹션별필드추출6~7회·수치재확인)라, 통독 16회를 더해도 전체 시간에 크게 반영 안 됨. 783s는 그 1회 측정 시점의 서버측 변동(부하/생성 길이 등)으로 추정 — **cap=16 자체의 정상 비용은 기준선과 비슷한 수준**으로 재해석해야 함(단정 금지, 표본 1개씩이라 추가 반복측정 필요).

**새 정성적 발견(긍정)**:
- 올원e적금 r016: OCR이 예전에 `10.1%p`로 뭉쳐 읽던 것(메모리 기록 버그)을 통독이 처음부터 `① 0.1%p`로 정확히 분리 — 기존 스윕-중복 크롭 재판독 없이 바로 해결.
- 003 p1 r007-009: 메모리 백로그 "참여방법 스텝번호 조각화(1·3만 잡히고 2 누락)"가 이번엔 1/2/3 전부 정상 캡처.

**한계 재확인**: cap=16 세 파일 모두 히트(42/46/21개 중 16). 003은 1페이지만 적용되는 설계라 2·3페이지 유의사항은 이번에도 미적용. ocr_score 이분화 재현(003 심의필 영역 0.25 등, 원인 동일).

**신규 관찰**: 003 p1 r000 ocr_score=0.62, VLM이 "7/23 돈쓰기 이벤트 / 올원모임 소문내기 이벤트 시안" 캡처 — "시안"이라는 단어로 보아 디자인 파일에 남은 워터마크성 문구를 실제 광고문구와 같이 통독했을 가능성. 급한 문제는 아니나 실제 배포본에서도 재현되는지 확인 필요.

산출물: `out_baseline/json/002…json`(비교용), `out_regionvlm/json/{올원e적금,003}…json`.

#### 배치화 + cap 제거 + lean 투영 (2026-07-23, 같은 세션) — HyundaiHS x4 방식 이식

사용자 지시로 (a) 한 호출에 여러 크롭 배치, (b) 16개 상한 제거, (c) LLM 전달용 lean 투영 정식 산출물화. `--region-vlm` 게이트는 전 페이지로 확대.

- **배치 통독**: `vlm_direct.transcribe_region_crop`(1영역=1콜) → `transcribe_region_crops`(N장/1콜, 응답 `index`로 되매핑, 배치 실패 격리). config `region_vlm_max_per_page=16` → `region_vlm_crops_per_call=4`(HyundaiHS `VLM_EXTRACTION_CROPS_PER_CALL` 기본값과 동일 — json의 `crop_vlm_x4`가 이것). `pipeline._transcribe_regions_vlm` 상한 삭제, 대상=「bbox+OCR라인 있는 영역」(디지털 정본·순수이미지 제외).
- **lean 투영(§1A 정식화)**: 신규 `src/nh_parsing/llm_view.py` `build_doc_view` — bbox·신뢰도·출처 제거, 섹션→영역 clean text, `region_id` 유지(추출 후 bbox 재부착용), 장식예시 제외. `run_nhdata`가 `out/llm_view/*.json` 저장. `make_review` LLM뷰도 이 함수 재사용(화면=산출물 동일, `AdPage(**page_dict)`로 모델 복원).
- **⑤ 결정(사용자 질의 답)**: 역할·섹션 판정 **유지**. 장식예시 필터(2a)·필드 복수관측 안정화가 섹션에 의존하고 스키마 단계 미확정이라, 지금 ⑤ 제거 시 lean 투영에 장식 텍스트 유입 + 필드 변동 재발. **스키마 콜 착수 시** "의미 그루핑→스키마 콜, ⑤→역할+장식필터만"으로 축소 예정(§6 표의 '④+와 통합' 옵션).
- **002 배치 실측(241.9s, baseline 291s보다 빠름 — 배치가 비용 안 늘림)**: 33/36 영역 통독(약 9콜), 하단 유의사항까지 전부 커버, 인덱스 정렬 실서버 정상. 회전 헤드라인 r008 `최고연 7.1%` 유지.
- **회귀 발견·수정(중요)**: 전체 통독으로 큰 영역(상품유의사항 우대이자율)이 1줄이 되면서 그 영역 필드들이 **모두 영역 bbox 앵커**를 공유 → `verify_numeric_fields`가 필드마다 영역 전체를 크롭 재판독해 우대금리 ①②③④를 헤더값(4.8%p)으로 **덮어씀**(cap=16 때는 그 영역이 통독 안 돼 안 터졌음). **수정**: `verify_numeric_fields(skip_bboxes=)` 추가 — 통독 영역(ocr_lines 있는 region) bbox 필드는 재판독 건너뜀(이미 VLM 판독). 단위테스트 `test_verify_numeric_skips_transcribed_region_fields` 추가. **교훈: 통독 라인의 필드 bbox 는 영역 단위라, "필드 bbox = 라인 bbox"를 가정하던 다운스트림(크롭 재판독류)은 통독본에서 재검토 필요.**
- 테스트: `tests/test_region_vlm.py` 5개(배치 되매핑·부분실패 격리·병존 분리·lean 투영·수치재확인 skip) 전부 통과.

---

### 부록 A — 별도 Claude Code 세션용 프롬프트 (rag-data 파싱 → 스키마 근거)

```
NH농협은행 금융상품 광고심의 PoC의 "상품군별 추출 스키마"를 만들기 위해,
nh-data/rag-data/ 안의 심의규정·체크리스트를 구조화 JSON으로 파싱해줘.

[목표]
상품군(예금성/적금성/입출금/이벤트성)별로 광고물에서 반드시 확인해야 하는 항목을
스키마 필드 후보로 뽑는 것. 각 필드는 { key, description(무엇을·왜 확인), 근거문서, 근거조항/문장 } 형태.
특히 다음을 분류해서 추출: (1) 필수 표시문구(FR-004), (2) 금리·수익률·조건 표시규칙(FR-005),
(3) 과장·오인 금지표현(FR-006), (4) 상품설명서/약관 정합성 기준(FR-007), (5) 상품군별 표시기준(DR-002).

[입력과 파싱 방법]
- 체크리스트 260713_2_NH_광고심의_체크리스트.xlsx → openpyxl/pandas로 직접 파싱
  (document-processor는 xlsx 미지원). 이게 가장 중요한 1차 근거.
- 나머지 HWP/PDF 심의규정 → 사내 document-processor로 파싱.
  주의: OCR이 없어서 스캔형 PDF 페이지는 parse_status="skipped"(pdf_scan_like)로 조용히 스킵됨.
  파싱 후 반드시 parse_status를 확인하고, 스킵된 문서·페이지는 목록으로 따로 보고할 것
  (그 문서는 별도 OCR 트랙이 필요하니 지금은 스킵 사실만 기록).
  nh-ad-review-poc/src/nh_parsing/rag_ingest.py 가 이미 이 문서들을 DocIR로 파싱하는 예시이니 참고.

[산출물]
- out/schema_source/<문서명>.json : 문서별 원본 파싱 결과(텍스트/표, 있으면 bbox)
- out/schema_source/_product_group_fields.json : 위 목표대로 상품군별 필드 후보 통합본
- out/schema_source/_skipped.md : 스캔형 등으로 파싱 못 한 문서·페이지 목록

과적합 금지: 특정 광고 샘플에 맞추지 말고, 규정 문서에 실제로 적힌 기준만 근거와 함께 옮길 것.
```

### 부록 B — 이번 세션 코드 변경
- `tools/make_review.py`: LLM-view 블록 추가(`_llm_view_html`) + 같은-행 박스 이어붙이기(`_rows_from_lines`) + 필드/증거층 라벨 개편. (파이프라인 로직 변경 없음, 표시 전용)
