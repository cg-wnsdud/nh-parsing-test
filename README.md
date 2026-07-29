# NH 광고심의 PoC

AI 활용 금융상품 광고심의 적정성 검토 에이전트 (NH농협은행 PoC, 씨지인사이드).

## 현재 상태

파싱(광고물 → 구조화 IR) + STAGE_3 스키마 추출(구조화 필드) 프로토타입.
심의 판정(위반 여부 산정)은 아직 없음 — 자세한 경계는
[docs/handoff.md](docs/handoff.md) §6 참조.

## 실행

```bash
uv sync                                # Python 3.13 + 의존성 (사내 파서 포함, Java 필요)

# 1단계: 파싱 (광고물 → out/json, out/llm_view)
uv run python tools/run_nhdata.py --input nh-data/sample-data

# 2단계: STAGE_3 스키마 추출 (out/llm_view → out/extracted)
uv run python tools/run_extract.py

# 채점 — 파싱 품질(분류/섹션/문장)과 필드 회수는 서로 다른 도구가 잰다
uv run python tools/evaluate.py         # gold/*.yaml 대비 → out/eval_report.md
uv run python tools/verify_extract.py   # out/extracted 대비 필드 회수

# 육안 검수 — 원본 위 bbox 하이라이트 + OCR/VLM 판독 대조
uv run python tools/make_review.py      # → out/review.html
```

외부 서비스(PaddleX/Gemma)는 사내 엔드포인트 — [.env.example](.env.example) 참고.
`VLM_CACHE=r`(기록)/`=p`(재생) 환경변수로 결정론적 A/B가 가능하다(개발 전용).

## 코드 맵 (src/nh_parsing/)

### 파싱 파이프라인 (`tools/run_nhdata.py` → `pipeline.py`)

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
| `vlm_judge.py` | 영역 역할·섹션 판정, 미배정 라인 내용 귀속 |
| `cards.py` | 카드-분할 — 개수는 밀도(코드), 배정은 VLM |
| `vlm_direct.py` | 밴드 통합판독, 스윕, 저신뢰 재판독 |
| `truncation.py` | OCR 정본 vs VLM 후보의 관계 판정 (잘림/생략/회수/불일치) |
| `layout_gap.py` | 레이아웃이 통째로 놓친 블록 진단 (감지만, 자동 승격 없음) |
| `field_judge.py` | `check_field_consistency` — 값이 원문에 실재하는지 검산 |
| `hwp_ingest.py` | 사내 파서(document-processor)로 HWP 디지털 추출 (표는 구조 API 우선) |
| `assets.py` | 내장 이미지(ImageAsset) 공용 헬퍼 |
| `ir.py` | AdPageIR 스키마 (pydantic) |
| `config.py` | 실행 설정 (환경변수 기반) |
| `vlm_cache.py` | VLM 응답 기록/재생 — A/B 결정론 장치 (개발 전용) |

### STAGE_3 스키마 추출 (`tools/run_extract.py` → `extract.py`)

| 모듈 | 역할 |
|---|---|
| `extract.py` | `out/llm_view` → 스키마 기반 필드 추출. 호출그룹 분할, 부재 판정, 근거 검증 |
| `applicability.py` | 부재 4분류(해당없음/미표시/확인필요/판정제외) — 스키마 메타데이터를 코드로 평가 |
| `llm_view.py` | `out/json` → STAGE_3 입력 정제본 (좌표 제거, 판독 관계 딱지 부착) |
| `schema_pack.py` | `schemas/*.json` 로드 + 오버레이 합성 + strict json_schema 생성 |

### 별도 트랙 (파싱 파이프라인과 무관)

| 모듈 | 역할 |
|---|---|
| `rag_ingest.py` | `tools/run_ragdata.py` 전용 — 규정 원문 → RAG 청크 + 이미지 캡션 (스키마 근거 도출용) |

## 문서

- **[docs/handoff.md](docs/handoff.md)** — ⭐ **여기부터.** 스키마를 어떻게 짰고 무엇이
  나오는지, 다음 단계(RAG/DB) 인계 시 정해야 할 것 5가지. 팀 논의용
- **[docs/architecture/pipeline-map.md](docs/architecture/pipeline-map.md)** — 파이프라인
  내부. 단계별 실행 순서·판단 주체(OCR/VLM/코드)·비용 실측
- **[docs/previous/](docs/previous/)** — 대체된 과거 설계 문서 (역사적 기록)

## 참고 저장소

- `cginside/repo-analysis/paddle-gemma-orchestrator` — 이전 프로젝트(OCR+VLM 오케스트레이터), 재사용 패턴은 docs/previous 참고
- [CGINSIDE-ROOKIES/document-processor](https://github.com/CGINSIDE-ROOKIES/document-processor) — 사내 문서 파서(DocIR)

## 알려진 한계 / 다음 단계

`docs/handoff.md` §6(아직 없는 것 / 측정된 결함) 및
`docs/architecture/pipeline-map.md` 하단 "알려진 한계" 참조.
