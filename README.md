# NH 광고심의 PoC

AI 활용 금융상품 광고심의 적정성 검토 에이전트 (NH농협은행 PoC, 씨지인사이드).

## 현재 상태

파싱 파이프라인 프로토타입 구현 단계.

### 프로토타입 실행

```bash
uv sync                              # Python 3.13 + 의존성 (사내 파서 포함, Java 필요)
uv run python tools/run_nhdata.py    # 광고물 트랙: nh-data 6개 파일 전체 처리
uv run python tools/run_ragdata.py   # rag-data 트랙: 기준 문서 → RAG 청크(+이미지 캡션)
uv run python tools/evaluate.py      # 골드셋(gold/*.yaml) 대비 자동 채점
uv run python tools/compare_parsers.py  # 파서 비교(docproc/kordoc/pdfium) → out/parser_comparison.md
# 결과: out/json/ (AdPageIR), out/previews/ (bbox 오버레이), out/rag/ (RAG 청크),
#       out/eval_report.md (채점 리포트)
```

외부 서비스(PaddleX/Gemma)는 WireGuard VPN 활성화 필요 — [.env.example](.env.example) 참고.

### 코드 맵 (src/nh_parsing/)

| 모듈 | 역할 | 설계서 절 |
| --- | --- | --- |
| `pipeline.py` | 파일 → AdDocument 라우팅/조립 (HWP 내장 이미지 포함) | 3, 4.1 |
| `triage.py` | PDF 페이지 단위 structured/scan_like/hybrid 판정 + 디지털 라인 추출 | 4.2 |
| `canvas.py` | 입력 정규화 (이미지/PDF → 캔버스, scan_like 는 네이티브 DPI 렌더) | 6.1 |
| `tiling.py` | 세로 스크롤 오버랩 밴드 분할 + 좌표 복원 + 중복 제거 | 6.3 |
| `paddlex_client.py` | PP-StructureV3 호출 (레이아웃 + OCR) | 6.4 |
| `gemma_client.py` | VLM 공용 호출(chat_json) + 분류 (파일명 prior 결합) | 6.2 |
| `vlm_judge.py` | VLM 판단 주체: 영역 역할·섹션 판정 + 필드 추출 | 6.5, 6.6 |
| `regions.py` | 레이아웃 라벨 기반 영역 조립 (역할 규칙은 VLM 실패 시 폴백 전용) | 6.4 |
| `hwp_ingest.py` | 사내 파서(document-processor)로 HWP 디지털 추출 + 내장 이미지 라우팅 | 5, 6.8 |
| `rag_ingest.py` | rag-data 트랙: 기준 문서 → RAG 청크 + 내장 이미지 VLM 캡션 | 5 |
| `assets.py` | 내장 이미지(ImageAsset) 공용 헬퍼 (디코딩/장식 필터) | 5 |
| `ir.py` | AdPageIR 스키마 | 7 |

골드셋 평가 루프: `gold/*.yaml` (정답) ↔ `tools/evaluate.py` (채점) ↔ `out/gold_review/` (판독 근거 크롭).

- **[docs/architecture/pipeline-overview.md](docs/architecture/pipeline-overview.md)** — 전체 파이프라인 단계별 시각 가이드 (흐름도 + 단계별 상세 + 신뢰성 장치)
- **[docs/architecture/parsing-pipeline-design.md](docs/architecture/parsing-pipeline-design.md)** — 초기 파싱 파이프라인 설계서 v0.1 (팀 리뷰 대기)
  - nh-data 샘플 6개 실측 진단 결과 포함
  - 이중 트랙(기준자료 인제스천 / 광고물 분석) + 페이지 단위 하이브리드 분기 설계
  - 사내 파서(document-processor) 활용 방식과 제약, OCR/VLM 엔진 비교 계획

## 참고 저장소

- `cginside/repo-analysis/paddle-gemma-orchestrator` — 이전 프로젝트(OCR+VLM 오케스트레이터), 재사용 패턴은 설계서 부록 A 참고
- [CGINSIDE-ROOKIES/document-processor](https://github.com/CGINSIDE-ROOKIES/document-processor) — 사내 문서 파서(DocIR), 제약은 설계서 부록 B 참고

## 다음 단계

설계서 10절 참고: 파서 브리지 프로토타입 → HWP 렌더 실측 → 벤치마크 정답지 작성.
