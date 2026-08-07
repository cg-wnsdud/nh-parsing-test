# vlm-ocr 서비스를 nh-ad-compliance 에 붙이는 법

> 이 폴더의 파일들은 **대상 저장소로 갈 것**이고, 여기서는 검증만 한다.
> 대상 저장소(`CGINSIDE-ROOKIES/nh-ad-compliance`, `dev` `3cfcbb5`)는 읽기만 했다.
> 작성 2026-08-07. 모든 줄 번호는 그 커밋 기준.

## 0. 이 폴더에 있는 것

| 파일 | 대상 경로 | 상태 |
|---|---|---|
| `nh-ad-compliance.patch` | (전부) | ✅ 복사본에 적용해 대상 테스트 101개 통과 — **§7** |
| `vlm_ocr_app.py` | `apps/parser-services/vlm_ocr_app.py` | ✅ 컨테이너로 띄워 실파일 검증 |
| `requirements-vlm-ocr.txt` | `apps/parser-services/requirements-vlm-ocr.txt` | ✅ 이미지 빌드 성공 (290MB) |
| `Dockerfile.vlm-ocr` | `apps/parser-services/Dockerfile.vlm-ocr` | ⚠️ 패키지 **받아오는 줄만** 미검증 (§5·§7-3) |
| 아래 §2 (compose) | `compose*.yml` | ⚠️ **미검증** — 스택 전체를 못 띄웠다 |

**결론부터**: 이 패치를 그대로 병합하면 서비스는 뜨고 호출도 되지만 **우리 결과가
승자 선정에서 탈락한다**(§7-4). 그 이유는 구조적이고(§7-5), 함께 정해야 할 것이
남아 있다. 즉 **이대로는 목적을 달성하지 못한다.**

파싱 알맹이는 `nh_parsing.vlm_ocr_service.parse_to_normalized()` 하나이고, 계약 dict
생성은 `nh_parsing.normalized_export` 가 한다. 둘 다 이 저장소에 테스트와 함께 있다.

## 1. 무엇이 실제로 검증됐나 (2026-08-07)

깨끗한 3.13 가상환경에 **사내 HWP 파서·Java 없이** 우리 휠 + 런타임 의존성만
(총 24개 패키지) 설치하고, FastAPI 로 실제 요청을 넣었다.

```
GET  /health      → {'status': 'ok', 'engine': 'vlm-ocr'}
POST /v1/parse    → HTTP 200  (146.0s)   ← NH농협은행-2026_002-예금성.png 3.7MB
                    textBlocks 93 · layoutBlocks 53
                    warnings ['TEXT_DETECTED_BUT_UNREADABLE', 'COORDINATE_BAND_ONLY']
깨진 파일         → HTTP 422  {'detail': 'vlm-ocr_PARSE_FAILED'}   ← 내부 사정이 안 샌다
```

그 응답을 **대상의 진짜 계약 모델**로 검증(파이썬 3.12 임시 환경):

```
NormalizedDocument.model_validate()   통과
  신원        REV-0002 · SRCFILE-0002 · vlm-ocr    ← 워커 대조 항목 3개 일치
  블록        93 (좌표 있는 블록 93)
  레이아웃     53   ← 대상 paddleocr 는 이 배열이 비어 있다
  역할 분포    제목 15 · 유의사항 18 · 본문 14 · 각주 3 · 고지문구 2 · 버튼 1
```

> 이 파일(`002-예금성.png`)은 대상 `docs/광고예시/` 의 것과 **바이트 단위로 같다**.
> 같은 입력으로 양쪽 결과를 직접 비교할 수 있다.

## 2. compose 에 추가할 것

기존 파서 서비스 형식을 그대로 따른다. 포트는 8091~8094 가 쓰이므로 **8095**.

```yaml
  vlm-ocr:
    image: ${IMAGE_PREFIX:-nh-ad-compliance}/vlm-ocr:${RELEASE_TAG:-local}
    build:
      context: .
      dockerfile: apps/parser-services/Dockerfile.vlm-ocr
    environment:
      # ⚠️ 형제 서비스와 결정적으로 다른 점 — 이 컨테이너는 자족하지 않는다.
      #    바깥 모델 서버 두 대에 닿아야 하고, 폐쇄망이면 경로를 열어야 한다.
      PADDLEX_URL: ${PADDLEX_URL:?vlm-ocr 는 PaddleX 서버가 필요하다}
      GEMMA_URL: ${GEMMA_URL:?vlm-ocr 는 Gemma 서버가 필요하다}
      # ⚠️ 게이트웨이가 모델 이름을 바꾸면 VLM 호출이 전부 400 으로 죽는데 OCR 은
      #    멀쩡히 돌아 산출물이 그럴듯하게 나온다. 배포 전에 GET {GEMMA_URL}/../models
      #    로 대조할 것. (우리 저장소에서 실제로 겪은 사고다)
      GEMMA_MODEL: ${GEMMA_MODEL:?모델 이름을 게이트웨이 목록과 대조할 것}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/health', timeout=2)"]
      interval: 15s
      timeout: 5s
      retries: 20
      start_period: 60s
    networks: [application]
```

워커 쪽 `environment` 와 `depends_on` 에도 각각 한 줄씩:

```yaml
      VLM_OCR_ENDPOINT: http://vlm-ocr:8095
      # ⚠️ 기본 120초로는 반드시 타임아웃 난다 — 아래 §4 참조
      PARSER_SERVICE_TIMEOUT_SECONDS: ${PARSER_SERVICE_TIMEOUT_SECONDS:-420}
```
```yaml
      vlm-ocr:
        condition: service_healthy
```

## 3. 워커에 배선할 것 (3곳)

**`apps/worker/src/nh_ad_worker/settings.py:47` 다음 줄**
```python
    vlm_ocr_endpoint: str = "http://vlm-ocr:8095"
```

**`apps/worker/src/nh_ad_worker/parser_services.py`** — 인자 하나와 등록 한 줄
```python
def parser_service_adapters(
    ...
    paddleocr_endpoint: str,
    vlm_ocr_endpoint: str,          # 추가
    ...
) -> dict[str, ParserAdapter]:
    return {
        ...
        "vlm-ocr": ParserServiceAdapter(     # 추가
            "vlm-ocr", vlm_ocr_endpoint, timeout_seconds=timeout_seconds
        ),
    }
```

**`apps/worker/src/nh_ad_worker/main.py:174` 근처**
```python
            paddleocr_endpoint=settings.paddleocr_endpoint,
            vlm_ocr_endpoint=settings.vlm_ocr_endpoint,      # 추가
```

`ParserServiceAdapter` 를 그대로 쓴다 — 우리 응답이 그 클래스의 검증
(`model_validate` + 신원 3개 대조)을 이미 통과하는 걸 위 §1 에서 확인했다.

## 4. 🔴 이대로 넣으면 안 되는 것 두 가지

### 4-1. 타임아웃 — 기본값으로는 **반드시** 실패한다

`parser_service_timeout_seconds` 기본값이 **120초**인데(`settings.py:43`), 우리는
실측 **146초**(002)이고 문서에 따라 100~300초다. 게다가 그 필드에 `le=600` 상한이
걸려 있어 아무 값이나 못 넣는다.

- 최소 **420초**를 권한다(가장 느린 문서 300초 + 서버 부하 여유).
- 시간의 **97%가 모델 대기**다. 우리 CPU 를 늘려도 안 빨라진다.

### 4-2. 🔴 라우팅 — **고치지 않으면 이 서비스는 한 번도 안 불린다**

`vlm-ocr` 는 `routing.py:77` 에 2차 어댑터로 이름만 올라가 있는데, 2차 어댑터는
`quality_rerun_reason` 이 있을 때만 실행된다(`parse_with_attempts`). 이미지 경로는
그 값을 **절대 만들지 않는다**. 실제로 라우터를 돌려 확인했다:

```
PNG, 외부AI 허용    secondary=('vlm-ocr',)  rerun_reason=None
                   → 실제로 호출된 어댑터: ['paddleocr']      ← 우리는 안 불린다
```

대상 테스트(`test_contracts.py:84`)는 `route()` 반환값만 보고 실제 호출은 안 봐서
이 공백이 드러나지 않았다.

**어떻게 열지는 팀장님 결정 사항이다.** 세 갈래:

| 안 | 내용 | 대가 |
|---|---|---|
| **가** | 이미지에도 `quality_rerun_reason` 을 줘 2차로 태우고 점수로 고른다 | 기존 동작 유지. 대신 문서당 2배 시간 |
| 나 | 이미지 1차를 `vlm-ocr` 로 바꾼다 | 빠르지만 우리가 실패하면 대안이 없다 |
| 다 | `external_ai_allowed` 일 때만 1차를 바꾼다 | 정책(ADR-0081)과 맞물린다 |

라우팅은 ADR-0079 소관이라 **어느 안이든 ADR 이 따라야 한다.**

### 4-3. ⚠️ 「가」안을 고를 때 미리 알아야 할 것

승자를 고르는 `_candidate_rank` 의 **첫 정렬키가 `document.confidence.score`** 이고,
그 값은 `min(모든 블록)` 이다(`service.py:65`). 우리 002 문서의 실제 값:

```
(0.165, 10/10, 위치완성도 1.000, 경고 2건, 읽힌문구 88, -1)
 └ 첫 키                └ 세 번째 키
```

`0.165` 는 **잘 안 읽힌 라인 8건**이 min 을 끌어내린 값이다. 우리가 `layoutBlocks`
53개를 주고 상대가 0개를 줘도, 첫 키에서 지면 **위치완성도까지 순서가 안 온다.**
빈 텍스트 11건은 이미 우리가 걸러 냈지만(그래서 0.0 은 없다) 그것만으로는
부족하다. 「가」안을 고른다면 이 정렬 규칙을 함께 논의해야 한다.

## 5. 전제 조건 — 저장소 위치

Dockerfile 이 우리 패키지를 archive tarball 로 받는데(대상 `document-processor` 와
같은 방식), 지금 이 저장소는 **개인 저장소**(`cg-wnsdud/nh-parsing-test`)다.
대상 빌드가 닿으려면 옮기거나 권한을 열어야 한다. 커밋도 못박아야 한다 —
우리 `pyproject.toml` 의 git URL 은 커밋 고정이 없어 빌드가 재현되지 않는다.

## 6. 이 저장소에서 다시 확인하는 법

```bash
# 계약 변환 (모델 호출 0회)
uv run python tools/export_normalized.py
uv run --no-project --python 3.12 \
  --with "<대상>/packages/parser-contracts" python tools/verify_contract.py

# 테스트 197개
uv run python -m pytest tests/ -q

# 실행마다 흔들리는 숫자 확인 (다른 실행본으로도 돌아가야 한다)
NH_OUT=out_run3 uv run python -m pytest tests/test_normalized_export.py -q
```

---

# 7. 대상 저장소 복사본에서 실제로 돌린 결과 (2026-08-07)

§1 은 "대상 부품을 빌려다 우리 쪽에서 조립"한 것이었다. 이 절은 다르다 —
**대상 저장소를 통째로 복사해 이 패치를 전부 적용하고 그 안에서 돌렸다.**
원본(`3cfcbb5`)은 그대로 두었다(변경 0건 확인).

`nh-ad-compliance.patch` 가 그때 적용한 것과 같은 패치다.

## 7-1. 대상 테스트 — 기준선과 같다

```
패치 전  101 passed, 1 skipped
패치 후  101 passed, 1 skipped      ← 아무것도 안 깼다
```
(`apps/worker/tests` + `packages` + `apps/parser-services/tests`. 윈도우에서는
`PYTHONUTF8=1` 이 필요하다 — 없으면 대상 코드가 UTF-8 파일을 cp949 로 읽어
47개가 실패한다. 우리 변경과 무관한 환경 문제다.)

대상 테스트 3곳을 고쳐야 했다. `set(adapters) == {...}` 로 어댑터 집합을 못박아
두어서 어댑터가 하나 늘면 반드시 갈린다 — 기본값을 주든 안 주든 마찬가지다.

## 7-2. 라우팅 — 열렸다

```
PNG · 외부AI 허용    호출된 어댑터 ['paddleocr', 'vlm-ocr']    채택 vlm-ocr
PNG · 외부AI 불허    호출된 어댑터 ['paddleocr']              채택 paddleocr
```

## 7-3. 컨테이너 — 빌드하고 띄웠다

`docker build` 성공. **290MB**(Java 없음). `/health` → `{"status":"ok","engine":"vlm-ocr"}`.

> ⚠️ 빌드는 휠을 빌드 컨텍스트에 넣는 변형으로 했다. 배포용 `Dockerfile.vlm-ocr` 는
> 대상 관례대로 GitHub archive 에서 받는데 **그 주소가 아직 없다**(§5). 베이스
> 이미지·의존성·앱 import·uvicorn 기동은 두 변형이 동일하다.

## 7-4. 🔴 실제 경로로 넣은 결과 — **우리 결과가 버려졌다**

대상 `ParserRouter` + 대상 `ParserServiceAdapter` 로 실제 컨테이너를 호출했다
(`NH농협은행-2026_002-예금성.png`, 155.2초):

```
라우팅   1차=paddleocr 2차=('vlm-ocr',) 사유=LAYOUT_BLOCKS_MISSING
  시도1 paddleocr  블록   0  레이아웃   0  신뢰도 0.900   ★채택
  시도2 vlm-ocr    블록  94  레이아웃  53  신뢰도 0.165
채택   paddleocr
```

**아무것도 안 읽은 쪽이 이겼다.** 우리 응답은 계약 검증도 신원 검사도 통과했는데
승자 선정에서 탈락했다. (상대는 대역이다 — 대상 paddleocr 컨테이너를 못 띄웠다.
하지만 아래 이유로 결론은 대역 여부와 무관하다.)

## 7-5. 원인은 우연이 아니라 구조다

`_candidate_rank` 의 첫 정렬키가 `document.confidence.score` 이고 그 값은
`min(모든 블록)` 이다(`service.py:65`). **min 은 블록을 더할수록 낮아지기만 한다** —
즉 **적게 읽을수록 점수가 높다.** 우리 문서 실측이 이걸 그대로 보여 준다:

```
블록 93개 · 최저 0.165 · 중앙값 0.984 · 최고 1.000
가장 낮은 5개  0.165 · 0.375 · 0.454 · 0.686 · 0.887
→ 이 4개만 안 읽었으면 문서 신뢰도가 0.165 에서 0.889 로 올랐다
```

상대 신뢰도를 바꿔 가며 승자를 확인한 결과, **0.165 를 넘기면 상대가 블록 0개여도
무조건 이긴다.** 위치완성도(우리 1.000)는 세 번째 키라 순서가 오지 않는다.

```
상대 0.200 vs 우리 0.165  →  paddleocr
상대 0.170 vs 우리 0.165  →  paddleocr
상대 0.165 vs 우리 0.165  →  vlm-ocr    ← 동점이 돼서야 뒤집힌다
```

**그래서 라우팅만 열어서는 이 PR 이 목적을 달성하지 못한다.** 함께 정해야 할 것:

| 안 | 내용 |
|---|---|
| A | 승자 선정에서 `min` 대신 다른 집계(중앙값·평균·하위 10% 절사)를 쓴다 |
| B | `layoutBlocks` 유무를 정렬키 앞쪽에 넣는다 (이 PR 이 주는 값이 그것이므로) |
| C | 저신뢰 블록을 문서 신뢰도 계산에서 빼고 대신 warning 으로 센다 |
| D | 이미지에서는 겨루지 않고 `vlm-ocr` 를 1차로 둔다 (「나」안) |

어느 것도 우리가 정할 수 없다 — 심의 판정 품질에 직결되고 ADR-0073(승자 선정
기준) 소관이다.
