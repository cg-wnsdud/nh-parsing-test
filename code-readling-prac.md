# paddle-gemma-orchestrator 코드 리딩 가이드 — NH 광고심의 파이프라인 설계자용

> 대상: OCR/VLM 파이프라인 경험이 적은 개발자가, 이 레포(현대홈쇼핑 식품 규제문서용)를
> 레퍼런스 아키텍처로 삼아 **NH은행 광고심의 파싱 파이프라인**을 새로 설계하는 상황.
> 목표: (1) 2~3세션 안에 엔진을 이해하는 효율적 읽기 순서, (2) "광고심의 도메인팩"을
> 만들거나 엔진 패턴을 차용하기 위한 구체적 적용 맵.
>
> 총 코드량 약 9,200줄(Python). 핵심만 읽으면 ~3,500줄이면 엔진이 잡힌다.
> 참고: 이 레포는 이미 **"새 도메인 = 농협은행"을 명시적 예제로 문서화**해 놓았다
> (`orchestrator/domain/base.py:12` "새 도메인(예: 농협은행)", `domain_packs/README.md`의
> `nonghyup_bank` 워크스루). 저자가 이 코드를 우리 용도로 확장할 것을 예상하고 설계했다는 뜻.

---

## 1. 읽기 순서

### 세션 1 — 뼈대와 계약 (약 900줄)

| 순서 | 파일 | 줄수 | 왜 / 무엇을 볼 것인가 |
|---|---|---|---|
| 1 | `README.md` | 209 | 3계층 구조(엔진↔도메인팩↔저장) 그림과 파일 지도. "새 도메인 추가 = 폴더 하나 + ORCH_DOMAIN 변경, 엔진 0줄 수정"이 이 레포의 핵심 주장. 읽기 순서 권장까지 적혀 있음. |
| 2 | `.env.example` | 132 | **설정이 곧 파이프라인 지도다.** [1]필수(PADDLEX_URL, GEMMA_BASE_URL), [2]선택(ORCH_DOMAIN, VLM_FALLBACK_ENABLED, VLM_EXTRACTION_ENABLED 두 개의 큰 스위치), [3]고급(타일링/폴백 임계치). `ROI_FALLBACK_MIN_REGIONS_<카테고리>` 카테고리별 오버라이드 규칙을 눈여겨볼 것. |
| 3 | `main.py` | 43 | 진입점 전체. .env 로드 → `Settings.from_env()` → `OrchestratorApp` → `ThreadingHTTPServer`. 5분이면 끝. |
| 4 | `orchestrator/config.py` | 382 | `Settings` frozen dataclass 하나에 모든 환경변수·임계치가 모임. `fallback_min_regions_for()`(L220)와 `_roi_fallback_overrides_from_env()`(L336, 카테고리명 하드코딩 없이 env 스캔)가 도메인 중립 설계의 예시. `as_dict()`는 run_config.json 재현성용. |
| 5 | `orchestrator/domain/base.py` | 90 | **엔진↔도메인 분리선. 이 레포에서 가장 중요한 90줄.** `DomainSpec` Protocol의 12개 메서드가 곧 "도메인팩이 제공해야 할 전부". `DocumentType` dataclass(code/korean/description/expected_fields/filename_keywords/aliases) 구조 암기. |
| 6 | `orchestrator/domain/loader.py` | 50 | `ORCH_DOMAIN` → `domain_packs.<name>.domain.get_domain()` 동적 import. 규칙만 지키면 자동 인식. |
| 7 | `orchestrator/llm/tasks.py` | 18 | 5개 태스크 이름 상수: `PAGE_ANALYSIS`, `ROI_EXTRACTION`, `PAGE_JUDGE`, `VLM_FALLBACK`, `VLM_TILE_FALLBACK`. 도메인팩은 이 5개에 대한 프롬프트+스키마만 구현하면 된다. |
| 8 | `orchestrator/models.py` | 231 | 단계 간 데이터 컨테이너: `PageWork`(페이지 1장), `DocumentWork`(문서), `JobState`(잡 진행상태), `RegionCandidate`(ROI 후보). 이후 app.py를 읽을 때 사전 역할. |

### 세션 2 — 파이프라인 심장부 (약 1,900줄)

| 순서 | 파일 | 줄수 | 왜 / 무엇을 볼 것인가 |
|---|---|---|---|
| 9 | `orchestrator/runtime/app.py` | **1054** | 파이프라인 전체. 아래 **섹션 지도**대로 나눠 읽을 것. |
| 10 | `orchestrator/adapters/paddlex.py` | 44 | PaddleX 호출 전부. base64 JPEG POST → `layoutParsingResults` 응답. 외부 OCR 서버와의 유일한 접점이 44줄이라는 점이 교훈(우리가 OCR 엔진을 바꿔도 이 파일만 바꾸면 됨). |
| 11 | `orchestrator/adapters/openai_compatible.py` | 105 | strict json_schema 강제(`add_response_format` L70)와 JSON 복구 사다리(`extract_json_object` L17). 핵심 패턴 (a) 참조. |
| 12 | `orchestrator/clients/gemma.py` | 573 | VLM 호출 4종: 분류(L75), 폴백 ROI(L186, 타일링 분기 포함), 크롭 추출(L412), 페이지 judge(L497). 4개 함수가 모두 같은 "mode×attempt 재시도 루프" 골격을 공유함을 확인. |
| 13 | `orchestrator/clients/http.py` | 28 | urllib 기반 `post_json` 하나. 의존성 최소화 철학. |
| 14 | `orchestrator/adapters/image_io.py` | 139 | `render_pdf_pages`(pdftoppm), `resize_for_vlm`(thumbnail=종횡비 보존, L95 — 좌표 패턴의 전제조건), `rgb_on_white`(투명 PNG 처리). |

**app.py(1054줄) 섹션 지도** — 순서대로가 아니라 ⑤→⑥→①→④ 순으로 읽는 게 빠르다:

| 섹션 | 대략 라인 | 내용 |
|---|---|---|
| ① 잡 생명주기 | L52~176 | `__init__`(도메인 로드/store/잡 레지스트리), `start_batch`/`start_single`/`_start_job`(데몬 스레드 기동), stop 협조 중지, `resolve_input_paths`(경로 탈출 방어) |
| ② 조회/내보내기 | L178~368 | run/문서 조회, `_write_run_exports`(summary/documents/pages_extraction JSON), Label Studio export/sync |
| ③ 페이지 펼치기 | L370~395 | `_iter_pages`: 이미지=1장, PDF=DPI 220 렌더 → `PageWork` 리스트 |
| ④ 단계 함수 | L397~426 | `_run_gemma_step`(분류+회전가드), `_run_paddlex_step`(회전 적용→OCR→아티팩트) |
| ⑤ VLM 폴백 | L428~599 | `_run_vlm_fallback_step`(폴백 결과로 **regions 교체**, L455~462가 핵심), `_run_vlm_fallback_pass`(스레드풀 배치) |
| ⑥ 필드 추출 | L601~724 | `_run_extraction_pass`: 크롭 4장씩 청크 → merge → judge → 교차검증 |
| ⑦ 메인 루프 | L726~1054 | `_run_job`: L762~895 `drain_gemma`/`drain_paddlex` 클로저(2단계 파이프라이닝), L897~944 제출 루프, L947~956 폴백·추출 패스, L958~1017 SQLite 영속화+요약, L1025~1054 중지/치명오류 처리 |

### 세션 3 — 코어 로직·도메인팩·저장 (약 2,600줄, 선별)

| 순서 | 파일 | 줄수 | 왜 / 무엇을 볼 것인가 |
|---|---|---|---|
| 15 | `orchestrator/core/layout_artifacts.py` | 182 | PaddleX 응답 1건 → 영역/크롭/OCR/폴백플래그로 변환하는 `save_structure_artifacts`. ROI 전략 분기(text-density 대체, L110~145)도 여기. |
| 16 | `orchestrator/core/region_artifacts.py` | 230 | 후보 수집(`parsing_res_list`+`layout_det_res` 이중 소스, L28), IoU 0.85 병합(L68), **OCR 라인→영역 매핑**(`ocr_lines_for_bbox` L143: 중심점 포함 or 30% 겹침), 크롭 생성(L169). |
| 17 | `orchestrator/core/fallback_policy.py` | 86 | 폴백 결정 정책 전체. 핵심 패턴 (c) 참조. 짧고 순수함수라 테스트하기 좋은 형태의 모범. |
| 18 | `orchestrator/core/vlm_fallback.py` | 396 | quad_1000 정규화(L65~135), 카테고리 별칭 정규화(L137, ⚠식품 도메인 누수), `fallback_segments_to_regions`(L316: 좌표만 받고 텍스트는 안 받음 — L340 `detected_text = ""`). |
| 19 | `orchestrator/core/vlm_tiling.py` | 586 | 대형 이미지 타일링: 발동 조건(L46), 타일 생성(L66), 타일좌표→전체좌표(L114), partial 조각 스티칭(L192, union-find), 중복 병합(L240). 모바일 상세페이지 캡처에 그대로 필요. |
| 20 | `orchestrator/core/extraction.py` | 352 | OCR 교차검증(`check_field_consistency` L27, `BACKED_TOKEN_RATIO=0.8` L17), 후보 병합(`merge_extractions` L237), 평탄화(L312). 핵심 패턴 (d). |
| 21 | `orchestrator/core/page_analysis.py` | 211 | 분류 결과 정규화, `apply_orientation_fix`(L76), `apply_orientation_guard`(L87 — 회전 억제는 도메인이 결정). ⚠L14 `DEFAULT_DOMAIN`이 import 시점에 고정됨. |
| 22 | `orchestrator/core/text_density.py` | 104 | OCR 라인 박스를 팽창→연결요소로 묶는 RLSA식 군집. "StructureV3는 실패해도 OCR 라인은 정확하다"는 관찰의 산물. VLM 없이 ROI를 만드는 제3의 전략. |
| 23 | `domain_packs/README.md` | 146 | **신규 도메인 추가 실전 가이드.** 예제가 문자 그대로 `nonghyup_bank`(대출신청서). 고칠 곳 4군데: document_types.py / schemas.py의 두 enum / prompts / domain.py 이름. |
| 24 | `domain_packs/hyundai_home/document_types.py` | 156 | `DocumentType` 5종 실물. expected_fields 문장이 그대로 추출 프롬프트에 주입되는 구조를 확인. |
| 25 | `domain_packs/hyundai_home/schemas.py` | 297 | **가장 중요.** `Category`/`CanonicalKey` enum이 strict json_schema로 VLM 출력을 강제. `PageClassification`, `FallbackSegment`(quad_1000), `ChunkEvidenceExtraction`, `PageJudgeExtraction` 구조는 도메인 무관하게 재사용. |
| 26 | `domain_packs/hyundai_home/domain.py` | 196 | DomainSpec 구현체. `prompt()`의 5개 태스크 분기와 플레이스홀더 주입(L93~172), `_prompt_template`(L191: prompts/*.md 로드). |
| 27 | `domain_packs/hyundai_home/prompts/*.md` (5개) | 각 1~9줄(밀도 높음) | page_analysis / roi_extraction / page_judge / vlm_fallback / vlm_tile_fallback. §4에서 광고심의 버전과 대응시켜 정독. |
| 28 | `domain_packs/hyundai_home/roi.py` + `extraction_contract.py` | 34+126 | 문서유형별 ROI 전략/회전가드 선언, 유형별 권장 키 채점(비차단 audit). |
| 29 | `orchestrator/storage/store.py` | 703 | SQLite 8테이블 스키마(L60~164), 콘텐츠 주소(sha256) 아티팩트 BLOB(L166~209), 경로→artifact_id 치환(`canonicalize_artifacts` L329), `persist_document_result`(L374). 전부 파일 하나·서버 없음. |
| 30 | `docs/PLAN.md` + `docs/VLM_FALLBACK_ANALYSIS_20260612.md` | 153+205 | 설계 의도와 실전 장애 분석. §5에서 상세히. ⚠PLAN.md는 일부 낡음(코드가 진실). |
| 31 | `tests/test_domain_seam.py` 외 | 61+ | 도메인 특화가 엔진으로 새는지 잡는 회귀 테스트. 새 도메인팩 만들면 이 테스트부터 돌릴 것. |

건너뛰어도 되는 것: `api/server.py`(530줄, 라우팅+디버그 UI — 상단 docstring의 라우트 맵만),
`integrations/label_studio.py`(373줄 — 검수 도입 시점에), `tools/`, `uv.lock`.

---

## 2. 파이프라인 흐름 추적 — 문서 1건이 `_run_job`을 통과하는 과정

시나리오: `test_docs/단호박식혜/표시사항.png` 1장을 single 잡으로 처리.

**0. 잡 시작** — `OrchestratorApp.start_single()` → `_start_job()`(app.py:90)이 `JobState` 등록 후
`_run_job`을 데몬 스레드로 실행. `_run_job`(app.py:726)이 `run-YYYYMMDDTHHMMSS-<job_id>` 디렉터리를 만들고
`store.create_run`(app.py:755), `run_config.json` 덤프(app.py:756, 재현성).

**1. 페이지 펼치기** — `_iter_pages`(app.py:370): 이미지라 `rgb_on_white`로 1장,
PDF였다면 `render_pdf_pages(path, vlm_pdf_dpi=220)`(image_io.py:114). 페이지마다 `PageWork` 생성.

**2. VLM 분류 + 회전 판단** — 메인 루프(app.py:919~930)가 `gemma_executor.submit(self._run_gemma_step, page_work)`.
`_run_gemma_step`(app.py:397) → `request_gemma_classification`(clients/gemma.py:75):
- `domain.category_hint_from_filename()`으로 파일명 힌트("표시사항" → `label_or_packaging`) 추출(gemma.py:87)
- `domain.prompt(PAGE_ANALYSIS, ...)`로 프롬프트 렌더, `resize_for_vlm(1800)` 후 base64 이미지와 함께
  `/chat/completions` POST. `add_response_format`으로 strict json_schema 부착(gemma.py:127)
- 응답을 `normalize_classification_result`(page_analysis.py:123)로 정규화 → `{category, orientation_fix, preserve_layout_orientation, confidence, ...}`
- 실패 시 `default_classification_result`(page_analysis.py:178)로 **기본값 폴백**(파이프라인 계속 진행, `classification_defaulted=true` 기록)

이어서 `apply_orientation_guard`(page_analysis.py:87): `domain.uses_orientation_guard(category)`가 참이고
분류기가 `preserve_layout_orientation=true`를 줬으면 90도 회전을 억제(`orientation_fix→none`).

**3. 서버측 회전 + PaddleX 레이아웃/OCR** — `drain_gemma`(app.py:791)가 분류가 끝난 페이지를 즉시
`submit_paddlex`(app.py:783)로 넘김(두 스레드풀이 겹쳐 돔). `_run_paddlex_step`(app.py:412):
- `apply_orientation_fix`(page_analysis.py:76)로 회전 적용
- `request_paddlex_image`(adapters/paddlex.py:19): base64 JPEG POST, `useDocOrientationClassify=false`(회전은 이미 VLM이 결정)
- `save_structure_artifacts`(core/layout_artifacts.py:40):
  - `collect_region_candidates`(region_artifacts.py:28): `prunedResult.parsing_res_list` + `layout_det_res.boxes` 이중 수집
  - `merge_region_candidates`(region_artifacts.py:68): 같은 라벨 & IoU≥0.85 병합
  - `collect_ocr_lines`(region_artifacts.py:116) → `crop_regions`(region_artifacts.py:169): 영역별 크롭 PNG + `ocr_lines_for_bbox`로 매칭된 OCR 텍스트 사이드카(`.ocr.txt`) 저장

**4. 폴백 플래그 결정** — 같은 함수 안에서 `fallback_flags_for_regions`(fallback_policy.py:35):
- 영역 수 < `settings.fallback_min_regions_for(category)`(기본 3) → `region_count_below_threshold` 플래그
- `label_or_packaging`처럼 `domain.uses_large_region_fallback_guard()`가 참인 유형은 최대 영역 면적비 ≥ 0.55 → `label_region_area_above_threshold` 플래그
- 단, ROI 전략이 `TEXT_DENSITY_CLUSTER`인 유형(hyundai에선 label_or_packaging)은 OCR 라인 군집(`cluster_line_boxes`, text_density.py:22)이 성공하면 그걸로 영역을 대체하고 **플래그를 비움**(layout_artifacts.py:145)

`drain_paddlex`(app.py:852)가 플래그된 페이지를 `fallback_items`에 적립(app.py:882).

**5. VLM 폴백 ROI 재검출** — 큐가 다 빠진 뒤 `_run_vlm_fallback_pass`(app.py:947→498) →
페이지별 `_run_vlm_fallback_step`(app.py:428):
- `request_gemma_fallback_rois`(gemma.py:186): `should_use_tiled_fallback`(vlm_tiling.py:46)이
  "타일링 켜짐 & 카테고리 일치 & (≥30MP 또는 긴 변 ≥4000px)"이면 `build_tile_specs`(3000px 타일/300px 오버랩)로
  타일별 병렬 호출 → `stitch_tile_segments`(vlm_tiling.py:192)로 partial 조각 봉합. 아니면 전체 이미지 1회 호출(gemma.py:316)
- `normalize_fallback_segments`(vlm_fallback.py:187): `quad_1000` → `box_pixels` 변환(`normalized_box_to_pixels`, vlm_fallback.py:120)
- `fallback_segments_to_regions`(vlm_fallback.py:316): 크롭 저장. **VLM은 좌표만 주고 텍스트는 전사하지 않음**(vlm_fallback.py:340)
- **app.py:455~462: StructureV3 영역을 `structure_regions`로 보관하고 `regions`를 폴백 결과로 통째 교체**,
  `active_region_source="vlm_fallback"`. (과거엔 append여서 검수 화면이 겹쳤음 — §5의 분석 문서 참조)

**6. (옵션) 필드 추출** — `_run_extraction_pass`(app.py:955→601), `VLM_EXTRACTION_ENABLED=true`일 때:
- 페이지의 크롭들을 `VLM_EXTRACTION_CROPS_PER_CALL`(4장)씩 청크로 나눠 `request_gemma_extraction`(gemma.py:412) —
  "보이는 것만 수집" 단계(`ChunkEvidenceExtraction` 스키마)
- `merge_extractions`(extraction.py:237): 청크 관찰들을 label/canonical_key로 그룹핑, 후보마다
  `check_field_consistency`로 **OCR 매칭 점수** 부착 → 페이지 번들
- `request_gemma_page_judge`(gemma.py:497): 번들+OCR 전문을 주고 최종 페이지 JSON 판정(`PageJudgeExtraction`)
- `annotate_table_consistency`(extraction.py:297) + `flatten_extraction_fields`(extraction.py:312)로
  `ocr_backed`/`ocr_match` 부착, `domain.audit_extraction_contract`(extraction_contract.py:83)로 권장 키 누락 감사
- 결과를 `page["extraction"]`에 부착(app.py:714)

**7. SQLite 영속화** — 문서별 payload 조립(app.py:961~988) → `store.persist_document_result`(app.py:989→store.py:374):
- `canonicalize_artifacts`(store.py:329): `*_path`를 sha256 콘텐츠 주소 아티팩트로 치환(`*_artifact_id`+`*_url`), 이미지가 SQLite BLOB로 들어감
- `runs/documents/run_documents/pages/page_results/regions` 테이블 upsert
- run 종료: `finish_run`(app.py:1017), 스크래치 디렉터리 삭제(app.py:1024). 이후 조회·Label Studio 내보내기는 전부 DB에서.

---

## 3. 훔쳐올 핵심 패턴 5가지

### (a) OpenAI 호환 VLM HTTP 추상화 — strict json_schema + 재시도 루프

- **위치**: `orchestrator/adapters/openai_compatible.py:70~105`(`add_response_format`, `structured_output_modes`),
  재시도 골격은 `clients/gemma.py:111~183`(분류), 같은 패턴이 :354~408, :444~488, :525~567에 반복.
  JSON 복구 사다리는 `openai_compatible.py:17~67`.
- **동작**: ① 모든 호출에 `response_format={type:"json_schema", strict:true, schema:...}`를 강제 부착.
  주석(L76~79)에 실측 근거가 있다 — LiteLLM→vLLM 경로에서 **실제로 강제되는 모드는 json_schema뿐**이고
  `guided_json` 등은 조용히 무시되어 깨진 JSON을 낳으므로, 다운그레이드 대신 json_schema로 재시도만 한다
  (`structured_output_modes`는 이제 `["json_schema"]`만 반환, L101~105).
  ② 파싱 실패 시 "Retry because the previous answer was not parseable JSON..." 문구를 덧붙여 최대 N회 재시도(gemma.py:120~125),
  HTTP 400이면 해당 모드 포기(L131~133). ③ 그래도 실패하면 `extract_json_object`가 코드펜스 제거→json.loads→
  ast.literal_eval→정규식 수리(꼬리 콤마, 따옴표 없는 키, None/True/False 치환) 순으로 복구.
  ④ 분류만은 최종 실패 시 기본값 반환(`GEMMA_CLASSIFICATION_DEFAULT_ON_ERROR`, gemma.py:166)해서 배치가 안 죽음.
- **광고심의 적용**: 그대로 가져간다. 사내 vLLM/LiteLLM에 Qwen-VL 등 다른 모델을 붙여도 이 계층은 무수정.
  교훈: **구조화 출력은 "모델에게 부탁"이 아니라 서버 강제 + Pydantic 이중 검증**(`schemas.validate`, gemma.py:479)으로.
  금리 필드처럼 형식이 중요한 값은 스키마 레벨에서 패턴을 걸 수도 있다.

### (b) quad_1000 정규화 좌표 → 픽셀 변환

- **위치**: 프롬프트 계약은 `domain_packs/hyundai_home/prompts/vlm_fallback.md:1,6`
  ("normalized 1000x1000 coordinates", "quad_1000 as four points clockwise"),
  변환은 `core/vlm_fallback.py:120~134`(`normalized_box_to_pixels`: `round(x*width/1000)`),
  전제조건은 `adapters/image_io.py:95~100`(`resize_for_vlm`이 `thumbnail()` 사용 = **종횡비 보존**),
  타일 로컬좌표→전체좌표는 `core/vlm_tiling.py:147~159`.
- **동작**: VLM에는 축소본(긴 변 1800px)을 보내고 좌표는 0~1000 정규화로 받는다. 종횡비가 보존되는 한
  정규화 좌표는 스케일 무관이므로, 원본 해상도 이미지에 그대로 사영해 고해상도 크롭을 만들 수 있다.
  `normalize_fallback_quad`(vlm_fallback.py:90)가 dict/list 등 별난 응답 형태를 흡수하고 0~1000으로 클램프.
  `docs/VLM_FALLBACK_ANALYSIS_20260612.md` §2가 이 매핑의 정합성을 실측 검증했다(종횡비 오차 <0.14%).
- **광고심의 적용**: 상세페이지에서 **필수문구/유의사항 블록의 위치 증거**를 남길 때 그대로 쓴다.
  심의 결과에 "이 문구가 페이지 어디에 있었는지" 하이라이트 오버레이(`draw_vlm_fallback_overlay`, vlm_fallback.py:282)를
  첨부하면 심의자 검수 UX가 크게 좋아진다. 주의: 축소·크롭 어느 단계에서든 종횡비를 깨면 이 패턴 전체가 무너진다.

### (c) 폴백 정책 — 싸게 먼저, 비싼 VLM은 플래그된 페이지만

- **위치**: `core/fallback_policy.py:17~32`(영역 수 임계치), :35~86(대형 영역 가드),
  임계치 해석은 `config.py:220~225`(`fallback_min_regions_for`), 카테고리별 오버라이드 env 스캔은 `config.py:336~352`,
  플래그 적립은 `app.py:868~883`, 소비는 `_run_vlm_fallback_pass`(app.py:498).
- **동작**: 모든 페이지는 일단 PaddleX(싸다)를 통과. `region_count < ROI_FALLBACK_MIN_REGIONS`(기본 3,
  `ROI_FALLBACK_MIN_REGIONS_<CATEGORY>`로 유형별 조정) 또는 한 영역이 페이지의 55%+를 덮으면
  "StructureV3가 못 잘랐다"고 보고 그 페이지만 VLM ROI 재검출로 보낸다. 성공 시 영역을 **교체**하고
  원본은 `structure_regions`로 보관(감사 가능). 제3의 전략으로 text-density 군집(VLM조차 안 씀)도 있다.
- **광고심의 적용**: 개념은 유지하되 **비율이 뒤집힐 것**을 예상하라. 식품 문서는 대부분 표 중심 스캔이라
  StructureV3가 잘 먹히고 폴백이 예외지만, 배너·이벤트페이지는 디자인 중심이라 StructureV3가 상시 실패
  → 폴백이 사실상 기본 경로가 된다. 이 레포의 답은 `RoiStrategy` 심(`domain.roi_strategy_for()`,
  roi.py:20)이다: 유형별로 "처음부터 StructureV3를 믿지 않는" 전략을 도메인팩에서 선언할 수 있다.
  배너류는 `ROI_FALLBACK_MIN_REGIONS_BANNER`를 높게 잡아 강제 폴백시키거나, 아예 VLM-first 전략을 추가하는 게 자연스럽다.

### (d) OCR-VLM 교차검증 — ocr_backed (임계 0.8)

- **위치**: `core/extraction.py:17`(`BACKED_TOKEN_RATIO = 0.8`), :27~48(`check_field_consistency`),
  후보 병합 시 점수 부착 :65~67·:143~153, 최종 평탄화 :312~352, 테이블 행 검증 :297~309.
  판정 근거 전달은 `_run_extraction_pass`(app.py:621~623, 영역 OCR 텍스트 연결)와 page_judge 프롬프트
  ("Use ONLY values present in page_ocr_text or chunk_results", `prompts/page_judge.md:2`).
- **동작**: VLM이 추출한 값을 공백·문장부호 제거+casefold로 정규화한 뒤 토큰 단위로 페이지 OCR 전문에서
  찾는다. 토큰의 80% 이상이 OCR에 존재하면 `ocr_backed=true`, `match`는 0~1 연속 점수.
  즉 **VLM(이해력 좋음, 환각 있음) × OCR(글자 정확, 이해 없음)의 상호 검증**. judge에게는 후보들+점수를 주고
  "OCR에 있는 값만 골라라"고 제약하며, 코드 쪽에서 한 번 더 점수를 박아 다운스트림이 신뢰도를 볼 수 있게 한다.
- **광고심의 적용**: **가장 중요한 이식 대상.** 광고심의의 최악 실패는 금리 환각("연 3.5%"를 "3.8%"로 읽음)이다.
  이 패턴을 숫자 특화로 확장하라: 금리·한도·기간은 토큰 매칭이 아니라 **숫자 정확 일치**(정규식으로 OCR에서
  `\d+\.\d+%` 추출 후 집합 비교)로 강화하고, `기본금리+우대금리=최고금리` 같은 산술 검산을
  `annotate_table_consistency` 자리에 추가한다. `ocr_backed=false`인 금리 필드는 자동으로 사람 검수 큐로 보낸다.
  ⚠주의: VLM 폴백 영역은 OCR 텍스트가 비어 있어(§5-4) 이 검증의 닻이 사라진다 — 광고에서는 폴백 빈도가 높으므로 반드시 보완할 것.

### (e) DomainSpec Protocol 심 + 도메인팩 격리

- **위치**: 계약은 `orchestrator/domain/base.py:43~90`, 로더는 `domain/loader.py:31~44`,
  태스크 이름은 `llm/tasks.py`, 구현 예는 `domain_packs/hyundai_home/domain.py:53~196`,
  누수 방지 회귀 테스트는 `tests/test_domain_seam.py`.
- **동작**: 엔진은 문서유형·필드·프롬프트를 하나도 모른다. 필요할 때마다 `domain.prompt(task, **ctx)` /
  `domain.schema(task)` / `domain.model(task)` / `domain.normalize_document_type(...)` /
  `domain.roi_strategy_for(...)` / `domain.uses_orientation_guard(...)`로 물어본다.
  도메인팩은 폴더 하나(문서유형 정의 + Pydantic 스키마 + 프롬프트 5개 + ROI/회전 선언 + 계약 감사)이고
  `ORCH_DOMAIN` 환경변수로 갈아끼운다. 프롬프트는 코드가 아니라 `.md` 파일 + `$플레이스홀더` 치환이라
  비개발자(심의 담당자)도 리뷰할 수 있다.
- **광고심의 적용**: 이것이 우리 선택지를 결정한다 — **엔진을 포크하지 말고 `domain_packs/nh_ad_review/`를
  추가하라.** 작업량의 80%는 `document_types.py`+`schemas.py`(CanonicalKey enum)이고 나머지는 거의 복붙이라고
  `domain_packs/README.md:143~145`가 명시한다. 새 파이프라인을 밑바닥부터 짜더라도 "엔진은 태스크 이름으로
  도메인에 질문한다"는 이 분리선 자체를 설계 원칙으로 가져가야 한다.

---

## 4. 광고심의 도메인팩 적용 맵 — `domain_packs/nh_ad_review/` 가상 설계

### 4-1. 파일별 대응표

| 파일 | hyundai_home (현재) | nh_ad_review (제안) |
|---|---|---|
| `document_types.py` | 성적서 / 품목제조보고서 / 표시사항 / HACCP / 등록증 (5종) | `detail_page`(상세페이지) / `event_page`(이벤트페이지) / `notice`(안내장) / `lms_text`(LMS문구) / `banner`(배너). `filename_keywords`=("상세", "이벤트", "안내장", "LMS", "배너", "banner"...), `expected_fields`에 유형별 심의 체크 필드 서술("상품명, 기본금리, 최고금리, 우대조건, 가입기간, 예금자보호 문구, 유의사항, 준법감시인 심의필 번호, 광고 유효기간...") |
| `schemas.py` — `Category` enum | quality_report 등 5개 | 위 5개 광고 유형 코드 |
| `schemas.py` — `CanonicalKey` enum | product_name, nutrition, haccp_valid_to 등 60여 개 | `product_name, product_type(예금/적금/대출/카드), base_rate, max_rate, preferential_rate, rate_condition, interest_payment_method, subscription_period, deposit_limit, deposit_protection_phrase(예금자보호), caution_text(유의사항), compliance_review_no(심의필), ad_valid_period, eligibility, event_period, event_benefit, disclaimer, contact...` ★ 이 enum이 VLM 출력에서 strict 강제되므로 심의 체크리스트와 1:1로 설계하는 것이 곧 요건 정의다 |
| `schemas.py` — 구조 모델 | PageClassification / FallbackSegment / ChunkEvidenceExtraction / PageJudgeExtraction 등 | **무수정 재사용**(README 지침). 단 금리 검산 결과를 담으려면 `PageJudgeExtraction`에 `rate_consistency` 섹션을 추가하는 확장 고려 |
| `roi.py` | label_or_packaging만 TEXT_DENSITY_CLUSTER + 회전가드 + 대형영역가드 | 회전가드·대형영역가드 대상 사실상 없음(디지털 광고는 항상 정방향). `banner`/`event_page`를 강제 폴백 또는 text-density로 실험. 유의사항 깨알글씨 블록엔 text-density 군집이 의외로 잘 맞을 수 있음 |
| `extraction_contract.py` | 유형별 권장 키(성적서→issuer, test_items...) | 유형별 필수 키: 예·적금 광고 → `base_rate, max_rate, deposit_protection_phrase, compliance_review_no`; 대출 광고 → `base_rate, rate_condition, caution_text, compliance_review_no`; LMS → `compliance_review_no, contact, disclaimer`. `missing_recommended_keys`가 그대로 "필수문구 누락" 1차 스크리닝이 된다 |
| `domain.py` | HyundaiHomeDomain | 이름·_models 매핑만 교체, 메서드 본문 복붙 |

### 4-2. 프롬프트 5개 재작성 방향

| 프롬프트 | 현재 (식품 규제문서) | 광고심의 버전이 물어야 할 것 |
|---|---|---|
| `page_analysis.md` | 문서유형 5종 분류 + 회전 판단(orientation_fix, preserve_layout_orientation). 파일명 힌트를 STRONG PRIOR로 | **광고 유형 분류**(상세/이벤트/안내장/LMS/배너) + 부가로 상품 카테고리(예금/적금/대출/카드/펀드) 힌트. 회전 지시문은 대폭 축소 — 디지털 광고는 사실상 항상 `none`(스캔 안내장만 예외). 회전 문단이 프롬프트의 70%를 차지하는 현재 구조를 광고 유형 판별 근거(레이아웃·CTA 버튼·채널 특징) 서술로 바꾼다 |
| `roi_extraction.md` | 크롭에서 보이는 한글 라벨/값 수집. "판단하지 말고, 지어내지 말고, 보이는 것만". canonical_key 힌트 목록 나열 | 동일 골격 유지(수집기≠판정기 분리는 그대로 가치 있음). canonical_key 목록을 금리/필수문구 키로 교체. 추가 지시: "금리는 숫자·단위·기준(연/월, 세전/세후)을 원문 그대로", "각주 번호(*, 1))와 그 본문을 짝지어 기록", "작은 글씨 고지문도 생략 말 것" |
| `page_judge.md` | 번들+OCR로 최종 페이지 JSON. "OCR/청크에 있는 값만" | 동일 골격 + **금리 수치 교차검증 강화**: "모든 금리·수치는 page_ocr_text에 문자 그대로 존재해야 한다. 기본금리+우대금리 합이 최고금리와 일치하는지 검산하고 불일치는 conflicts에 기록", "예금자보호 문구·심의필 번호가 후보에 없으면 notices에 누락 표시" |
| `vlm_fallback.md` | 좌표만 반환, quad_1000, "읽을 수 있는 인쇄 텍스트/데이터 표만", 제외 목록(일러스트·로고·치수선·마케팅 슬로건) | 좌표 전용·quad_1000 계약은 그대로. 포착 대상을 광고 구성요소로: "헤드라인/오퍼 영역, 금리·수치 표시 블록, 상품 조건 표, **하단 필수문구·유의사항 블록(작은 글씨일수록 중요)**, 심의필 번호 라인, 기간·대상 고지". 제외 목록 재작성: 모델·연예인 사진, 배경 일러스트, 앱 UI 크롬. ⚠현재의 "marketing slogans 제외"는 광고에서는 **포함**으로 뒤집어야 함(슬로건이 심의 대상) |
| `vlm_tile_fallback.md` | 타일 로컬 좌표 + partial/continuation_edges/edge_spans_1000 스티칭 계약 | 계약 그대로(플레이스홀더 유지). 세로로 긴 모바일 상세페이지 캡처(높이 4000px+)가 주 고객. "타일 경계에서 잘린 유의사항 문단은 partial=true + 연속 방향 표기" 등 예시만 광고 문맥으로 교체 |

### 4-3. 엔진 기능 유지/조정/제거 판단

| 기능 | 판단 | 근거 |
|---|---|---|
| 분류→OCR 스레드풀 파이프라이닝 (app.py:762~944) | **유지** | 배치 광고 심의(수백 건)에서 그대로 유효 |
| 회전 보정 + 회전 가드 | **축소** | 디지털 광고는 정방향. `page_analysis` 프롬프트에서 회전 비중을 줄이고 기본 `none`. 스캔 안내장 유형에만 잔존. VLM_FALLBACK_ANALYSIS가 보고한 회전 판단 불안정성(§5-3) 리스크도 함께 소멸 |
| VLM 폴백 + 교체(replace) 시맨틱 | **유지·확대** | 디자인 중심 광고에서 StructureV3 실패율이 높아 폴백이 준-기본 경로가 됨. 카테고리별 임계치로 제어 |
| 타일링 (vlm_tiling.py) | **필수 유지** | 모바일 상세페이지 캡처는 높이 4000~20000px가 흔함 → `vlm_fallback_tile_min_side_px=4000` 트리거에 정확히 걸림. `.env`에 `VLM_FALLBACK_TILING_CATEGORIES=detail_page,event_page` |
| text-density 군집 (text_density.py) | **실험 유지** | 유의사항 깨알글씨 벽이 OCR 라인으로는 잘 잡히므로 포장 도면과 동형 문제 |
| OCR 교차검증 (extraction.py) | **유지+숫자 특화 확장** | §3-(d). 금리 정확 일치 + 산술 검산 추가 |
| PDF 렌더(pdftoppm) | **유지** | 안내장·인쇄물 심의용. 웹/앱 캡처는 PNG로 바로 들어옴 |
| SQLite 단일 파일 저장 + 콘텐츠 주소 아티팩트 | **유지** | 파일럿에 최적(서버 0개). 규모 커지면 store.py만 교체하면 됨(경계가 깨끗함) |
| Label Studio 검수 | **유지하되 수정 필요** | 라벨 목록이 엔진에 식품 도메인으로 하드코딩(§5-1) — 광고 라벨(headline/rate_block/mandatory_notice/caution/review_no)로 교체 필요 |
| PaddleX 전처리 옵션(grayscale 등) | 기본 off 유지 | 광고는 컬러 대비가 정보임 |
| `classification_default_on_error` | **유지** | 광고 유형 분류가 실패해도 파이프라인이 계속 가는 안전장치 |

### 4-4. `.env` 광고심의 프로파일 초안

```
ORCH_DOMAIN=nh_ad_review
ROI_FALLBACK_MIN_REGIONS=3
ROI_FALLBACK_MIN_REGIONS_BANNER=5          # 배너는 사실상 상시 VLM 폴백 유도
ROI_FALLBACK_MIN_REGIONS_LMS_TEXT=1        # LMS는 텍스트 위주라 StructureV3로 충분
VLM_FALLBACK_TILING_ENABLED=true
VLM_FALLBACK_TILING_CATEGORIES=detail_page,event_page
VLM_FALLBACK_TILE_MIN_SIDE_PX=4000         # 긴 모바일 캡처 트리거
PADDLEX_USE_DOC_ORIENTATION_CLASSIFY=false # 회전은 애초에 거의 없음
```

---

## 5. 주의점 / 한계

### 5-1. 엔진에 남아 있는 현대(식품) 도메인 누수 — 광고팩 만들 때 걸리는 지점

"엔진 0줄 수정" 주장은 대체로 참이지만, 완전하지는 않다. grep으로 확인된 누수:

1. **`orchestrator/core/vlm_fallback.py:137~184` `normalize_fallback_category`** — 식품 별칭 사전이 엔진 코어에 하드코딩
   (`"원재료": "text"`, `"영양정보": "table"`, `"제조원": "text"`, nutrition/barcode/package_surface...).
   광고팩에서는 미지정 값이 기본 `"text"`로 떨어지므로 동작은 하지만, 광고 특화 별칭(예: "금리표"→table)을
   추가하려면 이 엔진 파일을 건드리거나 도메인 위임으로 리팩터링해야 한다.
2. **`orchestrator/integrations/label_studio.py:25~46, 49~75, 224~226`** — Label Studio 라벨 목록(`ingredients`,
   `nutrition_facts`, `manufacturer_info`, `package_surface`...)과 키워드 매핑(`"영양" in raw → nutrition_facts`)이
   엔진에 하드코딩. 검수를 쓸 계획이면 이 파일 수정이 필수(도메인팩만으로 해결 불가).
3. **`orchestrator/config.py:22~26`** — 기본 URL이 내부망 IP(`http://192.168.111.1:...`)로 박혀 있음. `.env` 없이 돌리면
   엉뚱한 데 접속 시도. 반드시 `.env`를 채울 것.
4. **`orchestrator/core/page_analysis.py:14~22`** — `DEFAULT_DOMAIN = load_domain_from_env()`가 **모듈 import 시점**에
   실행되고 `CLASSIFICATION_JSON_SCHEMA`도 그때 고정된다. env 로드보다 import가 먼저면 잘못된 도메인이 잡히고,
   프로세스 하나에서 도메인을 갈아끼우는 것도 안전하지 않다. 테스트 작성 시 특히 주의(ORCH_DOMAIN을 먼저 세팅).
5. `orchestrator/core/text_density.py` docstring 등 주석 곳곳에 식품/포장 문맥 서술 — 동작 무관, 혼동만 주의.

### 5-2. `docs/VLM_FALLBACK_ANALYSIS_20260612.md`에서 알아야 할 것 (실전 장애 분석)

- **§1 영역 append 버그(당시)**: 폴백 영역이 기존 StructureV3 영역에 **추가**되어 검수 화면에 실패한 큰 박스가
  겹쳐 나왔음(예: before 1 + VLM 7 = 8). 권고대로 현재 코드는 **교체+보관**으로 수정됨(app.py:455~462,
  `replaced_structure_regions: true`). → 교훈: 폴백 설계 시 "대체냐 추가냐"를 처음부터 명시하고 원본을 감사용으로 남길 것.
- **§2 좌표 매핑은 무죄**: 1000-정규화 매핑 자체는 검증 결과 정합(종횡비 델타 <0.14%). 박스가 이상해 보이면
  좌표 변환이 아니라 (i) VLM이 준 좌표 품질, (ii) 회전이 먼저 틀림, (iii) append 중복이 원인이었다.
  권고된 QA(종횡비 델타 1% 초과 플래그, 폴백 오버레이 상시 저장)는 우리 파이프라인에도 넣을 가치가 있다.
- **§3 회전 판단 불안정**: 같은 이미지가 run에 따라 `rotate_left_90`/`rotate_right_90`로 갈렸다(온도 0인데도).
  VLM 회전 판단은 프롬프트 민감·비결정적이라는 실측 증거. 이후 프롬프트 강화 + `preserve_layout_orientation`
  가드가 들어갔다. → 광고심의는 회전 자체를 거의 없애는 게 정답(§4-3).

### 5-3. `docs/PLAN.md`는 낡았다 — 코드가 진실

PLAN.md는 초기 설계 문서로, 현재 코드와 3가지가 어긋난다: ① "폴백 영역을 같은 regions 리스트에 append"(L77)
→ 지금은 교체, ② "json_schema 실패 시 guided_json→json_object→plain으로 다운그레이드"(L84~86)
→ 지금은 json_schema 고정 재시도(openai_compatible.py:101~105, vLLM 실측 근거), ③ "추출은 다음 단계"(L44~46)
→ 이미 구현됨(`_run_extraction_pass`). 아키텍처 의도 파악용으로만 읽고 세부는 믿지 말 것.

### 5-4. 설계상 한계 (광고심의에서 특히 체감할 것들)

- **VLM 폴백 페이지는 OCR 닻을 잃는다**: 폴백 영역은 좌표 전용이라 `ocr_text`가 빈 문자열
  (vlm_fallback.py:340~341). `_run_extraction_pass`는 활성 regions의 ocr_text만 모으므로(app.py:621~623)
  폴백 페이지의 `page_ocr_text`가 비고, `check_field_consistency`가 전부 0점 → **ocr_backed 교차검증이 무력화**된다.
  (StructureV3의 OCR 라인은 `structure_regions`에 보관돼 있지만 사용되지 않음.) 광고는 폴백 빈도가 높을 것이므로
  새 파이프라인에서는 "폴백 크롭에 OCR 재실행" 또는 "보관된 OCR 라인을 폴백 영역에 재매핑"을 반드시 넣어야 한다.
- 잡 상태는 인메모리(`self.jobs`) — 서버 재시작 시 진행 중 run은 `stale_stopped` 처리(app.py:120, store.py:305).
  체크포인트/재개 없음.
- `delete_run`은 아티팩트 BLOB을 지우지 않는다(콘텐츠 주소 공유 저장소, app.py:229) — DB 파일 단조 증가.
  이미지가 전부 SQLite BLOB이므로 대량 배치 시 수 GB 단위로 커지는 것을 감안.
- 페이지 단위 독립 처리 — **다페이지 문서의 맥락 연결 없음**(1페이지의 상품명과 3페이지의 유의사항을 잇는
  document-level 판정은 미구현). 광고심의에서 상세페이지가 여러 캡처로 쪼개져 오면 문서 레벨 병합 단계를 새로 설계해야 함.
- `pdftoppm`(Poppler) 필수 — Windows 개발 환경이면 PATH 세팅 필요(image_io.py:115~117에서 즉시 에러).
- 재시도·폴백이 페이지당 VLM 호출을 증폭시킨다(분류 1 + 폴백 1~타일 N + 추출 청크 M + judge 1).
  타일링 걸린 페이지는 20+ 호출도 가능 — GPU 용량 산정 시 반영.

### 5-5. 신규 도메인팩 검증 루틴

```bash
# domain_packs/nh_ad_review/ 작성 후
python -m unittest discover -s tests    # test_domain_seam.py가 엔진 누수 회귀 감시
ORCH_DOMAIN=nh_ad_review python main.py # 부팅 + /api/config 확인
```

---

## 부록 — 한눈 요약

```
입력(PDF/이미지)
 → _iter_pages                    페이지 펼치기 (app.py:370)
 → request_gemma_classification   [VLM] 광고유형 분류(+회전)   (gemma.py:75, domain: page_analysis.md)
 → request_paddlex_image          [OCR] 레이아웃+텍스트        (paddlex.py:19)
 → save_structure_artifacts       영역/크롭/OCR/플래그        (layout_artifacts.py:40)
 → fallback_flags_for_regions     영역부족? 판단              (fallback_policy.py:35)
 → request_gemma_fallback_rois    [VLM] ROI 재검출(±타일링)   (gemma.py:186, domain: vlm_fallback.md)
 → request_gemma_extraction ×N    [VLM] 크롭 증거 수집        (gemma.py:412, domain: roi_extraction.md)
 → merge_extractions              OCR 점수 부착 병합          (extraction.py:237)
 → request_gemma_page_judge       [VLM] 최종 판정             (gemma.py:497, domain: page_judge.md)
 → persist_document_result        SQLite 1파일 영속화         (store.py:374)
```

엔진은 그대로, `domain_packs/nh_ad_review/` 폴더 하나(문서유형 5종 + CanonicalKey enum + 프롬프트 5개)가
광고심의 파이프라인의 도메인 지식 전부를 담는다 — 이것이 이 레포에서 가져갈 가장 큰 설계 자산이다.
