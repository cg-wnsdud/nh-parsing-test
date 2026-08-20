# NH 광고심의 PoC

금융상품 광고물(PDF/이미지/HWP) → 구조화 텍스트 → 스키마 필드 추출 파이프라인.
문서마다 다음을 수행한다:

```
입력 PDF/이미지/HWP
  -> triage: structured / scan_like / hybrid 판정         (디지털 텍스트 신뢰도 기반)
  -> 캔버스 정규화 (렌더 · 필요 시 밀도 기반 분할·타일링)
  -> PaddleX PP-StructureV3 레이아웃 + OCR
  -> VLM 문서 분류 (상품군 · 광고유형)
  -> 밴드 단위 VLM 통합판독 (OCR 교정 + 미검출 문구 스윕)
  -> (선택) 저신뢰 라인 재판독 · 카드-분할 · 미배정 블록 진단
  -> OCR 정본 vs VLM 후보 판정 (잘림/생략/불일치 라벨)
  -> AdPageIR(JSON) + LLM 전달용 lean 투영(llm_view)
  -> (2단계) 스키마 기반 STAGE_3 필드 추출
```

결과물은 모두 **`out/` 아래 JSON 파일**로 쌓인다 — 서버형 DB가 없다.
심의 판정(위반 여부 산정)은 아직 없다 — 현재 하는 일은 "광고물에서 무엇이 어디에
있는지 빠짐없이 뽑아내는" 단계까지다. 경계는 [docs/handoff.md](docs/handoff.md) §6 참조.

> **처음 읽는 분께:** 아래 [아키텍처 & 코드 맵](#아키텍처--코드-맵)부터 보는 게 빠르다.
> 권장 순서: **이 README → `src/nh_parsing/pipeline.py`(1단계 진입점) →
> `src/nh_parsing/extract.py`(2단계 진입점)**.

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

uv run python tools/run_nhdata.py        # 1단계: 파싱 → out/json, out/llm_view
uv run python tools/run_extract.py       # 2단계: STAGE_3 필드 추출 → out/extracted
```

`VLM_CACHE=r`(기록)/`=p`(재생) 환경변수로 모델 호출 없이 결정론적 재실행이 가능하다
(개발 전용, 응답을 그대로 재생하므로 실제 처리 시간이 아니다).

## 산출물 — 파일 기반, DB 서버 없음

모든 결과는 `out/`(기본값) 아래 파일로 쌓인다. `.gitignore` 대상이라 새로 클론하면
비어 있다 — 위 명령을 돌려야 생긴다.

```
out/json/<doc_id>.json        전체 IR (bbox·신뢰도 포함) — 원본 위 하이라이트용
out/llm_view/<doc_id>.json    LLM 전달용 lean 투영 (좌표 제거, region_id 유지)
out/extracted/<doc_id>.json   STAGE_3 필드 추출 결과 (schemas/*.json 기준)
out/_timing.json              파일별 소요시간
```

운영:
- **초기화** = `out/` 삭제(다음 실행 때 재생성).
- 스키마 데이터(`src/nh_parsing/schemas/*.json`)는 손으로 쓴 입력이라 산출물과 분리돼
  패키지 안에 있다 — 설치본(휠)에서도 찾히게 하기 위함.

## 아키텍처 & 코드 맵

### 1단계 — 파싱 (`tools/run_nhdata.py` → `pipeline.py`)

| 모듈 | 역할 |
|---|---|
| `pipeline.py` | 전체 라우팅·조립 — 파일별 트랙 진입점, 밴드 통합판독, 미배정 귀속 |
| `triage.py` | PDF 페이지 단위 structured/scan_like/hybrid 판정 + 디지털 라인 추출 |
| `canvas.py` | 입력 정규화 (이미지/PDF → 캔버스, scan_like 는 네이티브 DPI 렌더) |
| `bands.py` | 글자밀도 기반 분할 — 타일링·스윕·카드 개수 판정의 공통 primitive |
| `tiling.py` | 밀도 분할 기반 타일 생성 + 좌표 복원 + 중복 제거 |
| `paddlex_client.py` | PP-StructureV3 호출 (레이아웃 + OCR) |
| `regions.py` | 레이아웃 블록 → 영역(Region) 조립 |
| `gemma_client.py` | VLM 공용 호출(chat_json) + 분류 + 호출 비용 계측 |
| `vlm_judge.py` | 영역 역할 판정 |
| `cards.py` | 카드-분할 — 개수는 밀도(코드), 배정은 VLM |
| `vlm_direct.py` | 밴드 통합판독, 스윕, 저신뢰 재판독 |
| `truncation.py` | OCR 정본 vs VLM 후보의 관계 판정 (잘림/생략/회수/불일치) |
| `layout_gap.py` | 레이아웃이 통째로 놓친 블록 진단 (감지만, 자동 승격 없음) |
| `field_judge.py` | `check_field_consistency` — 값이 원문에 실재하는지 검산 |
| `hwp_ingest.py` | 사내 파서(document-processor)로 HWP 디지털 추출 |
| `ir.py` | `AdPageIR` 스키마 (pydantic) — 1단계 산출물의 최종 형태 |
| `config.py` | 실행 설정 (환경변수 기반 `Settings`) |
| `vlm_cache.py` | VLM 응답 기록/재생 — 결정론적 재실행 장치 (개발 전용) |

### 2단계 — STAGE_3 스키마 추출 (`tools/run_extract.py` → `extract.py`)

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

## 새 상품군 스키마 추가

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
uv run python tools/make_review.py      # → out/review.html
```

원본 이미지·OCR/VLM 판독 결과를 **base64 로 파일 안에 그대로 내장**한다(문서 수에 따라
수 MB~수십 MB) — 폴더 없이 파일 하나만 보내도 그대로 열린다. `--for-print` 로 인쇄·PDF용
(토글 전부 펼침, A4 1단) 생성도 가능하다.

## 도구 (tools/)

| 도구 | 하는 일 | 모델 호출 |
|---|---|---|
| `run_nhdata.py` | 파싱 — 광고물 → `out/json` · `out/llm_view` · `out/_timing.json` | **필요** |
| `run_extract.py` | STAGE_3 — `out/llm_view` → `out/extracted` | **필요** |
| `run_ragdata.py` | 규정 원문 → RAG 청크 (별도 트랙) | **필요** |
| `run_schema_source.py` | 규정 문서 → 원본 파싱 결과 (스키마 도출 근거용) | 없음 |
| `verify_numbers.py` | 문서에 쓰는 모든 숫자를 `out/` 에서 재계산 | 없음 |
| `evaluate.py` | 골드셋 채점 — 분류·영역검출·문장 회수 | 없음 |
| `verify_extract.py` | 골드셋 채점 — 필드 회수 | 없음 |
| `make_review.py` | 육안 검수 화면 `out/review.html` 생성 | 없음 |
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
| **이 저장소가 무엇이고 어떻게 흐르나** (PR·인수인계 설명용) | ⭐ [docs/흐름과_인수인계_2026-08-06.md](docs/흐름과_인수인계_2026-08-06.md) |
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
