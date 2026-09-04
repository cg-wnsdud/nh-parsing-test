# NH 광고심의 PoC

금융상품 광고물(PDF/이미지/HWP) → 구조화 텍스트 → 농협 제공 템플릿 항목 라벨링
파이프라인. 문서마다 다음을 수행한다:

```
입력 PDF/이미지/HWP
  -> triage: structured / scan_like / hybrid 판정         (디지털 텍스트 신뢰도 기반)
  -> 캔버스 정규화 (렌더 · 필요 시 밀도 기반 분할·타일링)
  -> PaddleX PP-StructureV3 레이아웃 + OCR
  -> 카드/공통영역 공간 경계 + 읽기순서 정리
  -> VLM 문서 분류 (상품군 · 광고유형)
  -> 모든 StructureV3 텍스트·표 영역을 마스킹 Reader로 독립 판독
  -> OCR 정본과 Reader가 다를 때만 Judge 비교 (정본 불변)
  -> 페이지 sweep으로 StructureV3 미검출 문구 후보만 별도 탐색
  -> 템플릿 항목 라벨링 — 정형 문구 완전일치(1층) + VLM 줄범위 판정(2층)
  -> evidence-v6(P1 감사 원본) + ad-review-input-v6(P2 최소 심의 전달 JSON)
```

**광고물 트랙은 스키마 기반 필드 추출(STAGE_3)을 거치지 않는다** — 종점은
농협이 준 광고 템플릿(회사명·상품명·대출한도 등 12종)을 기준으로 영역마다
항목명을 붙인 통합 JSON이다. STAGE_3(`extract.py`/`run_extract.py`,
`schemas/*.json`)는 예금성·대출성 심의 규정 필드를 스키마로 뽑아내는 별도
트랙으로 코드는 남아 있지만, 광고물 파싱 결과가 이 단계를 거쳐 나가지는
않는다 — 자세한 배경은 [docs/etc/L-템플릿-제작과-모델-사용-지점.md](docs/etc/L-템플릿-제작과-모델-사용-지점.md),
스키마 트랙 자체 설명은 [docs/schema-explained.md](docs/schema-explained.md) 참조.

결과물은 모두 **`out_ad_full/`(또는 지정한 `--out`) 아래 JSON 파일**로 쌓인다 —
서버형 DB가 없다. 심의 판정(위반 여부 산정)은 아직 없다 — 현재 하는 일은
"광고물에서 무엇이 어디에 있는지 빠짐없이 뽑아내고, 어느 템플릿 항목인지
표시하는" 단계까지다. 경계는 [docs/handoff.md](docs/handoff.md) §6 참조.

> **처음 읽는 분께:** 아래 [아키텍처 & 코드 맵](#아키텍처--코드-맵)부터 보는 게 빠르다.
> 권장 순서: **이 README → `src/nh_parsing/pipeline.py`(파싱 진입점) →
> `tools/run_ad_label.py`(템플릿 라벨링 진입점, 광고물 트랙의 종점)**.

## 직접 준비해야 하는 외부 서비스

무거운 모델 서비스 2개는 **레포에 포함돼 있지 않다** — 사내망 서버를 가리키도록 설정한다.
둘 다 **회사 VPN(WireGuard)으로 접속된 상태**에서만 응답한다 — VPN 권한이 없으면 코드를
받아도 파싱 자체가 안 된다.

- **PaddleX PP-StructureV3** HTTP 엔드포인트 (레이아웃 + OCR) → `PADDLEX_URL`
- **OpenAI 호환 VLM** 엔드포인트 (Gemma 4 등) — 분류·통합판독·필드추출용 → `GEMMA_URL`, `GEMMA_MODEL`

호스트에 추가로 필요한 것:

- Python **3.13+**, [uv](https://docs.astral.sh/uv/)
- **JDK(아무 배포판)** — `document-processor`(사내 HWP 파서, public repo)가
  HWP 변환에 `jpype1`(Java 상호운용)을 쓴다. `document-processor` 자체 문서에도 이 설치법이
  안 나와 있어서 놓치기 쉽다 — JDK 설치 후 `JAVA_HOME`이 잡혀 있는지(또는 `java`가 `PATH`에
  있는지) 확인할 것. **HWP가 아닌 PDF/이미지만 다룰 거라면 필요 없다.**

> ⚠️ **Java 없이 HWP를 돌리면 에러 없이 조용히 실패한다.** `hwp_ingest.py`가
> `document_processor` 호출 실패를 통째로 잡아 그 문서만 "unreadable"로 표시하고 넘어간다
> (`run_nhdata.py`는 이걸 실패로 안 잡아 **exit code 0**)
> 기본 입력(`nh-data/sample-data`)에 `.hwp` 파일이 하나 섞여 있으므로 첫 실행부터
> 해당될 수 있다. 실행 후 `out/json/*.json`에서 `"사내 파서 HWP 추출 실패"` 노트가 있는지
> 확인하거나, 처음엔 `--input`으로 HWP가 없는 폴더만 넣어 확인하는 편이 안전하다.

## 설치

```bash
git clone <repo> && cd nh-ad-review-poc
uv sync                                  # Python 3.13 + 의존성 (document-processor 포함, 둘 다 public repo)

cp .env.example .env
# .env 편집 — PADDLEX_URL, GEMMA_URL, GEMMA_MODEL (VPN 연결 상태에서만 접근 가능,
# 실제 주소는 레포에 없으므로 별도로 전달받을 것)

# 광고물 트랙 (현재 개발 중심, kl_parser 가 실제로 호출하는 경로)
uv run python tools/run_ad_label.py --input <파일 또는 폴더> --out out_ad_full
uv run python tools/make_parse_explorer.py                 # 결과 열람 → out_ad_full/explorer.html
```

`--input`에 폴더를 주면 그 안의 `.pdf/.png/.jpg/.jpeg` 전부를 처리한다. 이미
저장된 파싱 결과를 재사용하고 라벨링만 다시 하려면 `--reuse-parse`를 추가한다
(문서당 파싱이 ~9분 걸려 라벨링만 고칠 때 유용하다). 자세한 옵션은
[tools/run_ad_label.py](tools/run_ad_label.py) 참조.

`VLM_CACHE=r`(기록)/`=p`(재생) 환경변수로 모델 호출 없이 결정론적 재실행이 가능하다
(개발 전용, 응답을 그대로 재생하므로 실제 처리 시간이 아니다).

### 영역 VLM 판독/비교 shadow 검증 (기본 파이프라인 안에서 선택 실행)

`kl_parser`가 호출하는 `ad_export.process_ad_file()`와 위 `run_ad_label.py`는 모두
같은 `pipeline.process_file()`을 사용한다. 아래 환경변수를 켜면 별도 후처리 도구 없이
그 기본 경로의 마지막에 VLM 판독/비교 교차검증이 들어간다.

```powershell
$env:REGION_READING_MODE = "shadow"
$env:REGION_READING_SCOPE = "all"   # 기본 all | 비용 비교용 targeted
uv run python tools/run_ad_label.py --input <파일> --out out_reader_judge
```

- `all`은 StructureV3가 반환한 텍스트·표 영역을 빠짐없이 VLM 판독에 보낸다. `targeted`는
  표·저신뢰 OCR만 대상으로 하는 비용 비교용 모드다.
- VLM 판독 입력은 목표 bbox 밖을 마스킹하고 파란 테두리를 표시한다. 목표 bbox가 다른
  StructureV3 텍스트 영역을 품으면 그 하위 영역도 마스킹하고 제외한 `region_id`를 기록한다.
- VLM 판독/비교 결과는 P1의 `region.text_evidence.vlm_region_reading`,
  `region.text_evidence.parser_vlm_comparison`, `region.text_evidence.p2_text_selection`에 들어간다.
  `lines[].parser_text` 파서 기본값과 템플릿 라벨 입력은 절대 변경하지 않는다. P2는 Judge가
  VLM을 선택했더라도 한 review view가 StructureV3 영역의 모든 줄을 포함할 때만 그 문구를
  시험적으로 사용한다. 넓은 밴드의 영역별 VLM 후보는 이 경로에서 쓰지 않는다.
- 표는 StructureV3가 찾은 영역 bbox만 사용한다. PaddleX HTML·행/열·셀 좌표는 최종
  계약에서 제외하며, 표 전용 VLM이 낸 `rows`는 파서 기본값을 바꾸지 않는 관측값으로 남긴다.
- 페이지 sweep은 StructureV3가 놓친 문구만 `page.recovery_candidates`에 별도 기록하며,
  OCR/디지털 파서 기본값이나 미배정 줄에 자동 편입하지 않는다.
- `tools/make_parse_explorer.py --in <out>`로 만든 `explorer.html`에서 파서 기본 텍스트·VLM 비교·
  카드 경계·VLM 표 관측·review_targets를 함께 확인할 수 있다.

**스키마 기반 필드 추출(STAGE_3, 별도 트랙)**을 시험하려면:
```bash
uv run python tools/run_nhdata.py        # 파싱 → out/json, out/llm_view
uv run python tools/run_extract.py       # STAGE_3 필드 추출 → out/extracted
```
이 트랙은 광고물 파싱 결과의 최종 산출물이 아니다 — 위 "광고물 트랙" 설명 참조.

## 산출물 — 파일 기반, DB 서버 없음

### 광고물 트랙 (`run_ad_label.py`, 현재 최종 경로)

```
<out>/parse/<파일명>.json   파싱 결과 (레이아웃·OCR·VLM 판독, --reuse-parse 재사용 대상)
<out>/json/<doc_id>.json    evidence-v6(P1) — 좌표·파서 기본 텍스트·VLM/Judge·시인성·카드·카드별 템플릿 근거 원본
<out>/review_input/<doc_id>.json  ad-review-input-v6(P2) — 라벨별 심의 문구 + 미배정 광고문구 + 표 격자 + P1 줄 참조  ★다음 단계 인계
<out>/pages/<doc_id>_p<n>.jpg   쪽 이미지(검수용, 박스 없음)
```

두 JSON은 같은 문서의 서로 다른 목적을 갖는다. `json/`은 감사·재검수·화면 하이라이트를
위한 원본이며, `review_input/`은 `labelled_ad_copy[]`와 `unmapped_ad_copy[]`가 파서 기본
줄을 빠짐없이 나눠 다음 심의/RAG/RDB 단계가 평면적으로 소비할 수 있게 만든 투영본이다.
페이지 sweep 후보는 어느 쪽에도 파서 기본 문구로 섞이지 않고 `unverified_recovery_candidates[]`에 남는다.
표는 `review_text` 평면 문구 **옆에** `text_views[].tables[]`로 행/열 격자를 함께 넘긴다 —
평면 텍스트로 접으면 격자를 되살릴 수 없다(실측 `16. 대출성상품` p1_r019: 요율 56줄이
한 줄씩 나열되면 헤더가 중복되고 교차 요율을 복원 못 한다). 격자는 VLM 관측이고 파서
정본이 아니므로 `status`·`confidence`를 함께 싣고, 라벨 연결은 **2층 VLM 의미 판정이
있을 때만** 한다(정형문구 완전일치만으로는 표를 다른 라벨에 붙이지 않는다).
기본 `--out`은 `out_ad`; 93건 전수조사는 `--out out_ad_full`로 만들었다.
`.gitignore` 대상이라 새로 클론하면 비어 있다 — 위 명령을 돌려야 생긴다.

### 스키마 트랙 (`run_nhdata.py`/`run_extract.py`, 별도)

```
out/json/<doc_id>.json        전체 IR (bbox·신뢰도 포함) — 원본 위 하이라이트용
out/llm_view/<doc_id>.json    LLM 전달용 lean 투영 (좌표 제거, region_id 유지)
out/extracted/<doc_id>.json   STAGE_3 필드 추출 결과 (schemas/*.json 기준)
out/_timing.json              파일별 소요시간
```

운영:
- **초기화** = 해당 `out*/` 폴더 삭제(다음 실행 때 재생성).
- 템플릿 사전(`src/nh_parsing/templates/ad_templates.json`)과 스키마 데이터
  (`src/nh_parsing/schemas/*.json`)는 손으로 쓴 입력이라 산출물과 분리돼 패키지
  안에 있다 — 설치본(휠)에서도 찾히게 하기 위함.

## 아키텍처 & 코드 맵

### 파싱 (모든 트랙 공통, `pipeline.py`)

| 모듈 | 역할 |
|---|---|
| `pipeline.py` | 전체 라우팅·조립 — 입력 전처리, 카드 경계, 페이지 누락문구 후보 탐색, 전 영역 VLM 판독 호출 |
| `triage.py` | PDF 페이지 단위 structured/scan_like/hybrid 판정 + 디지털 라인 추출 |
| `canvas.py` | 입력 정규화 (이미지/PDF → 캔버스, scan_like 는 네이티브 DPI 렌더) |
| `bands.py` | 글자밀도 기반 분할 — 타일링·스윕·카드 개수 판정의 공통 primitive |
| `tiling.py` | 밀도 분할 기반 타일 생성 + 좌표 복원 + 중복 제거 |
| `paddlex_client.py` | PP-StructureV3 호출 (레이아웃 + OCR) |
| `regions.py` | 레이아웃 블록 → 영역(Region) 조립 |
| `gemma_client.py` | VLM 공용 호출(chat_json) + 분류 + 호출 비용 계측 |
| `cards.py` | 카드-분할 — 개수는 밀도(코드), 배정은 VLM |
| `reading_adjudication.py` | StructureV3 영역 마스킹 VLM 판독 + 불일치 Judge (파서 기본값 불변) |
| `vlm_direct.py` | 페이지 전체 누락문구 탐색(sweep, 정본 미편입) |
| `layout_gap.py` | 레이아웃이 통째로 놓친 블록 진단 (감지만, 자동 승격 없음) |
| `field_judge.py` | `check_field_consistency` — 값이 원문에 실재하는지 검산 |
| `hwp_ingest.py` | 사내 파서(document-processor)로 HWP 디지털 추출 |
| `ir.py` | `AdPageIR` 스키마 (pydantic) — 1단계 산출물의 최종 형태 |
| `config.py` | 실행 설정 (환경변수 기반 `Settings`) |
| `vlm_cache.py` | VLM 응답 기록/재생 — 결정론적 재실행 장치 (개발 전용) |

### 광고물 트랙 — 템플릿 라벨링 (`tools/run_ad_label.py`, 현재 최종 경로)

| 모듈 | 역할 |
|---|---|
| `ad_template.py` | 템플릿 판정(12종 중 1개) + 영역마다 항목명(gubun) VLM 판정(줄 범위 단위) |
| `ad_export.py` | 원문 근거·VLM 판독/비교·표·템플릿 라벨을 `review_targets` 중심 통합 JSON으로 결합 |
| `templates/ad_templates.json` | 농협 제공 템플릿 md에서 생성한 라벨 사전 (`tools/build_ad_templates.py`) |

`kl_parser`(농협 KL 연동)가 실제로 호출하는 것도 이 경로다 —
`kl_parser/service/parsing_service.py` → `ad_export.process_ad_file()`.

### 스키마 트랙 — STAGE_3 필드 추출 (`tools/run_extract.py` → `extract.py`, 별도)

> ⚠️ 광고물 파싱 결과는 이 단계를 거치지 않는다. 예금성·대출성 심의 규정이
> 요구하는 필드를 스키마 기준으로 뽑아내는 별도 트랙으로, 코드는 유지되지만
> 현재 개발 우선순위는 위 템플릿 라벨링 쪽에 있다.

| 모듈 | 역할 |
|---|---|
| `extract.py` | `out/llm_view` → 스키마 기반 필드 추출. 호출그룹 분할, 부재 판정, 근거 검증 |
| `extract_models.py` | STAGE_3 응답의 pydantic 계약 — 계약 밖 값을 보내면 그 그룹만 스킵 |
| `applicability.py` | 부재 4분류(해당없음/미표시/확인필요/판정제외) — 스키마 메타데이터를 코드로 평가 |
| `llm_view.py` | `out/json` → STAGE_3 입력 정제본 (좌표 제거, 판독 관계 딱지 부착) |
| `schema_pack.py` | `schemas/*.json` 로드 + 오버레이 합성 + strict json_schema 생성 |

스키마는 코드에 하드코딩하지 않는다 — `src/nh_parsing/schemas/`:

| 파일 | 내용 |
|---|---|
| `_common.json` | 예금성·대출성 공통 필드 (은행명·심의필·수수료 등) |
| `예금성.json` / `대출성.json` | 상품군별 스키마 — `_common` 을 include, 호출그룹·필드·의무등급 |
| `_overlay_이벤트.json` | `ad_type=이벤트페이지`일 때만 덧붙는 오버레이 |
| `_product_group_fields.json` | 근거 대장 — 규정 조문 ↔ 필드 매핑. `check_coverage()` 가 이걸로 누락을 검사 |

### 별도 트랙 (파싱 파이프라인과 무관)

| 모듈 | 역할 |
|---|---|
| `rag_ingest.py` | `tools/run_ragdata.py` 전용 — 규정 원문 → RAG 청크 + 이미지 캡션 (스키마 근거 도출용) |

## 새 상품군 스키마 추가 (스키마 트랙 전용)

> 아래는 STAGE_3 스키마 트랙에만 해당한다. 광고물 트랙의 템플릿(12종)을
> 갱신하려면 `nh-data/pilot-data-set/광고 템플릿(근거규정x, 필수 여부).md`를
> 바꾸고 `uv run python tools/build_ad_templates.py`를 다시 돌리면 된다
> (§2 [docs/etc/L-템플릿-제작과-모델-사용-지점.md](docs/etc/L-템플릿-제작과-모델-사용-지점.md) 참조).

`schemas/<이름>.json` 을 새로 쓰면 STAGE_3 필드 추출 로직 자체는 건드릴 필요 없다
(`schema_pack.py` 가 파일을 읽어 동적으로 호출그룹을 구성한다). 다만 상품군 **이름**은
아래 두 곳에도 알려야 분류·라우팅이 그 이름을 인식한다 — 완전한 플러그인 구조는 아니다:

```
1. schemas/<이름>.json 작성          (schemas/예금성.json 복사해서 시작)
2. gemma_client.py 의 product_group enum 에 <이름> 추가   (VLM 분류가 고를 값)
3. config.py 의 product_group_keywords 에 <이름>: [파일명 키워드] 추가
```

## 검수

```bash
# 광고물 트랙 — 원본 그림 위에 라벨 상자를 얹어 눈으로 대조
uv run python tools/make_ad_review.py           # out_ad/*.json,*.jpg 대상 → out_ad/review.html

# 광고물 트랙 — 영역 단위로 접었다 펼치는 상세 열람(OCR/VLM 판독·시인성·표·라벨 한자리)
uv run python tools/make_parse_explorer.py      # → out_ad_full/explorer.html (기본 out_ad_full 대상)
uv run python tools/make_parse_explorer.py --only "13. 대출성상품"   # 일부 문서만

# 스키마 트랙
uv run python tools/make_review.py              # → out/review.html
```

`make_ad_review.py`는 원본·판독 결과를 **base64 로 파일 안에 그대로 내장**한다(문서
수에 따라 수 MB~수십 MB) — 폴더 없이 파일 하나만 보내도 그대로 열린다. `--for-print`
로 인쇄·PDF용(토글 전부 펼침, A4 1단) 생성도 가능하다.
`make_parse_explorer.py`는 그림을 `pages/` 상대경로로 걸어 가벼운 대신, 결과 폴더
(`out_ad_full/`) 안에서 열어야 한다.

## 도구 (tools/)

| 도구 | 하는 일 | 모델 호출 |
|---|---|---|
| `run_ad_label.py` | **광고물 트랙(현재 최종)** — 파싱 + 템플릿 라벨링 → evidence `<out>/json` + 인계 `<out>/review_input` | **필요** |
| `build_ad_review_input.py` | 기존 evidence JSON → ad-review-input 재생성 (OCR/VLM 호출 없음) | 없음 |
| `build_ad_templates.py` | 농협 광고 템플릿 md → 라벨 사전(`templates/ad_templates.json`) | 없음 |
| `make_ad_review.py` | 광고물 트랙 육안 검수 화면 (`out_ad/review.html`) | 없음 |
| `make_parse_explorer.py` | 광고물 트랙 영역 단위 상세 열람 화면 (`out_ad_full/explorer.html`) | 없음 |
| `measure_parse_defects.py` | 광고물 트랙 결함 유형 전수 계측 (`out_ad_full/json` 대상) | 없음 |
| `run_nhdata.py` | 스키마 트랙 파싱 — 광고물 → `out/json` · `out/llm_view` · `out/_timing.json` | **필요** |
| `run_extract.py` | 스키마 트랙 STAGE_3 — `out/llm_view` → `out/extracted` | **필요** |
| `run_ragdata.py` | 규정 원문 → RAG 청크 (별도 트랙) | **필요** |
| `run_schema_source.py` | 규정 문서 → 원본 파싱 결과 (스키마 도출 근거용) | 없음 |
| `verify_numbers.py` | 문서에 쓰는 모든 숫자를 `out/` 에서 재계산 | 없음 |
| `evaluate.py` | 골드셋 채점 — 분류·영역검출·문장 회수 | 없음 |
| `verify_extract.py` | 골드셋 채점 — 필드 회수 | 없음 |
| `make_review.py` | 스키마 트랙 육안 검수 화면 `out/review.html` 생성 | 없음 |
| `export_previews.py` | 검수 화면과 같은 그림을 이미지 파일로 (영역 라벨 포함) | 없음 |
| `rebuild_views.py` | `out/json` → `out/llm_view` 재생성 (파싱은 안 다시 함) | 없음 |
| `reclassify_absences.py` | 부재 4분류를 스키마 메타로 재계산 (스키마 수정 후 검증용) | 없음 |

## 테스트

```bash
uv run python -m pytest tests/ -q
```

## 문서

| 알고 싶은 것 | 문서 |
|---|---|
| **현재 파서의 단계·두 JSON·HyundaiHS 이후 단계 비교** (처음 보는 사람용, 2026-08-31) | ⭐ [docs/광고물_파싱파이프라인_현재구조_및_다음단계_2026-08-31.md](docs/광고물_파싱파이프라인_현재구조_및_다음단계_2026-08-31.md) |
| **가장 최신 — 전체 이해 문서 묶음(A~N), 광고물 트랙 중심** | ⭐ [docs/etc/00-읽는-순서.md](docs/etc/00-읽는-순서.md) |
| **이 저장소가 무엇이고 어떻게 흐르나** (PR·인수인계 설명용, 2026-08-06 기준) | [docs/흐름과_인수인계_2026-08-06.md](docs/흐름과_인수인계_2026-08-06.md) |
| **실제로 무엇이 나왔나** (숫자·시간·사례) | [docs/parsing-output-report.md](docs/parsing-output-report.md) |
| **코드가 실제로 무엇을 하나** (좌표·실값으로 끝까지) | [docs/architecture/pipeline-walkthrough.md](docs/architecture/pipeline-walkthrough.md) |
| **인계 시 정할 것** (스키마·DB·RAG 경계) | [docs/handoff.md](docs/handoff.md) |
| 스키마 내부 구조 상세 | [docs/schema-explained.md](docs/schema-explained.md) |
| 검수 화면(review.html) 읽는 법 | [docs/screen-guide-review-html.md](docs/screen-guide-review-html.md) |
| 다이어그램 캔버스 동반 설명 | [docs/architecture/pipeline-diagram-guide.md](docs/architecture/pipeline-diagram-guide.md) |
| **붙일 때 무엇이 막히나** (필드 대응표·해법) | ⭐ [docs/pr-plan-2026-08-07.md](docs/pr-plan-2026-08-07.md) |
| 팀장님 레포(nh-ad-compliance)와의 비교 | [docs/compare-nh-ad-compliance.md](docs/compare-nh-ad-compliance.md) |

**갱신하지 않는 옛 문서** — 어긋나는 지점은
[walkthrough §10](docs/architecture/pipeline-walkthrough.md) 에 모아 두었다:
`docs/architecture/pipeline-map.md` · `docs/발표대본_2026-08-04.md` ·
`docs/notion_파싱파이프라인-output_2026-08-03.md` · [docs/previous/](docs/previous/)

## 참고 저장소

- `cginside/repo-analysis/paddle-gemma-orchestrator` — 이전 프로젝트(OCR+VLM 오케스트레이터), 재사용 패턴은 docs/previous 참고
- [CGINSIDE-ROOKIES/document-processor](https://github.com/CGINSIDE-ROOKIES/document-processor) — 사내 문서 파서(DocIR)

## 알려진 한계 / 다음 단계

`docs/handoff.md` §6(아직 없는 것 / 측정된 결함) 및
`docs/architecture/pipeline-map.md` 하단 "알려진 한계" 참조.
