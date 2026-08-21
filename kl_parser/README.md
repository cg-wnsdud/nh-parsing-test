# NH GenAI Knowledge Lake — Custom 파서 (씨지인사이드)

농협 GenAI Knowledge Lake 의 **Custom 파서 슬롯**에 꽂아 쓰는 문서 파싱 API 입니다.
원본 문서(HWP·PDF·이미지)를 받아 KL 이 요구하는 구조화 파일 3종을 만들어 돌려줍니다.

- 규격 근거: `1.awx_custom_parser_example_api/readme.md` **"Output jsonl"** 단락
- 통신 방식: **비동기**(POST → uuid → 폴링 → zip). 규격서가 권장한 방식입니다
- 이 폴더만으로 **로컬 왕복 시험까지** 돌아갑니다 (§5)

---

## 1. 무엇을 하는가

```
   ┌─ KL ─────────────────────────────────────────────────────────┐
   │  POST /parsing (src_file, option)                            │
   │        └──▶ 202 {result:"OK", body:{uuid, timeout}}          │
   │  GET  /parsing/result/{uuid}                                 │
   │        └──▶ 200 zip  |  200 {"status":"PARSING"}  |  500     │
   └──────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │  진입점 (이 폴더)  │  얇다. fastapi·uvicorn·multipart 3개
                    │  app/main.py       │
                    └─────────┬──────────┘
                              │ HTTP (배포)  또는  직접 호출 (로컬 시험)
                    ┌─────────▼──────────┐
                    │  에이전트 서비스   │  파싱 본체
                    │  nh_parsing        │  PP-StructureV3 · VLM · 사내 HWP 파서
                    └────────────────────┘
                              │
              <원본파일명>_hrc.jsonl   구조화 본문 (item 단위)
              <원본파일명>_hrc.json    문서 정보
              <원본파일명>_img.zip     추출 이미지 (있을 때만)
```

### 진입점과 에이전트를 나눈 이유

| | 한 컨테이너에서 직접 | **별도 에이전트 서비스 (채택)** |
|---|---|---|
| KL 컨테이너에 필요한 것 | pypdfium2 + 사내 HWP 파서 + **JVM** + Pillow… | **fastapi·uvicorn·multipart 3개** |
| Python 버전 제약 | 우리 코드 전부가 3.11 에서 돌아야 함 | **진입점만.** 에이전트는 자유 |
| 실패 격리 | 파싱이 죽으면 슬롯이 죽는다 | 진입점은 살아서 상태를 돌려준다 |

사내 HWP 파서가 `jpype1` + JVM 을 요구하는데 DAP 베이스 이미지는 그 조합을 담지 않습니다.
이 분리로 그 충돌이 자동으로 풀립니다. (2026-08 회의 결정사항)

---

## 2. 규격을 어떻게 맞췄나 — 조항별 대조

`readme.md` "Output jsonl" 단락의 요구사항을 하나씩 확인했습니다.
검사는 코드로 자동화돼 있습니다 → `tests/spec_check.py`

| # | 규격 요구사항 | 우리 구현 | 검사 |
|---|---|---|---|
| 1 | POST 방식 | `POST /parsing` | O |
| 2 | request parameter 이름은 `src_file`, 멀티파트 | 동일 | O |
| 3 | `option` 은 base64 인코딩 → 디코딩 후 `parser_info`·`doc_data` 활용 | 디코딩해 `doc_data.origin_url` 을 `_hrc.json` 의 `source` 에 반영 | O |
| 4 | TEXT 파일명은 `원본파일명 + "_hrc.jsonl"` | 확장자 포함 원본명 그대로 (예제 코드와 동일) | O |
| 5 | INFO 파일명은 `원본파일명 + "_hrc.json"` | 동일 | O |
| 6 | 이미지가 있으면 `원본파일명 + "_img.zip"` 으로 압축 | 동일. **경로 없이 파일만** 압축 | O |
| 7 | 결과 3종을 zip 으로 압축해 리턴 | 동일 | O |
| 8 | 각 행은 `item` 항목으로 구성 | jsonl 한 행 = 객체 하나 | O |
| 9 | `item` 유형은 `text`·`table`·`image`·`h1`~`h4` | 이 6종만 사용 | O |
| 10 | 헤딩으로 도출된 항목을 `h1`~`h4` 로 변환 | 조항 번호 패턴으로 판별 (§4) | O |
| 11 | `value` 는 필수. `table` 은 HTML table, `image` 는 파일명 | 동일 | O |
| 12 | `table` 은 `type_property.title` 필수 (없으면 `""`) | 동일 | O |
| 13 | `image` 는 `title`·`width`·`height`·`ratio` 필수 | 동일 | O |
| 14 | `page` 를 알 수 없으면 `0` | HWP 는 페이지 개념이 없어 `0` | O |
| 15 | 사용자 메타는 `cust_attr1~5`(조회) / `cust_sattr1~5`(검색·조회) | §3 참조 | O |
| 16 | 파싱 요청 응답은 **반드시 202**, body 에 `uuid`·`timeout` | 동일 | O (왕복 시험이 검사) |
| 17 | 상태 파일 `genaikl.status` (`PARSING`/`DONE`/`ERROR`) | 예제의 DO NOT EDIT 함수를 그대로 사용 (`app/service/status.py`) | O |

### 예제 코드와 일부러 다르게 한 것 3가지

**1. 파싱 실행을 `subprocess` 대신 `BackgroundTasks` 로 했습니다.**
예제는 `python -c "…parse({repr 로 박은 인자})"` 로 명령줄을 만듭니다. 우리 입력 파일명에는
한글·공백·괄호가 흔해서 그 방식이 깨집니다 (예: `(2023년)예금성상품 광고시 준수사항_은행연합회.hwp`).
규격이 요구하는 것은 **응답을 즉시 202 로 돌려주는 것**이고 그건 지켜집니다.

**2. 실패한 작업 디렉터리를 지우지 않습니다.**
예제는 `ERROR` 일 때도 디렉터리를 삭제합니다. 그러면 무엇이 왜 실패했는지 남지 않습니다.
성공(`DONE`) 시에는 예제와 같이 삭제합니다.

**3. 예제의 문법 오류를 고쳤습니다.**
`parsing_service.py` 의 `except Exception as e:(` — 괄호 위치가 잘못돼 그대로는 실행되지
않습니다. 같은 동작(상태 파일에 ERROR 기록)으로 다시 썼습니다.

---

## 3. `cust_meta` 를 어떻게 채웠나

규격은 이름만 정하고 용도는 우리에게 맡깁니다. 검색 필터로 쓸 것은 `sattr`(검색·조회),
참고용은 `attr`(조회만)에 넣었습니다.

| 칸 | 넣은 값 | 왜 |
|---|---|---|
| `cust_attr1` | 청크 종류 (`text`/`table`/`heading`) | 조회 시 구분용 |
| `cust_attr2` | 청크 ID (`<문서명>_c000`) | 원본 청크로 되짚기용 |
| `cust_attr4` | 추출 출처 (`digital`/`ocr`) | 품질 판단 재료 |
| `cust_sattr1` | 문서명 | **검색 필터** — "이 규정 문서 안에서만" |
| `cust_sattr2` | 상품군 (`예금성`/`대출성`/`공통`) | **검색 필터** — 파일명에서 결정론적으로 판별 |
| `cust_sattr3` | 소속 조항 제목 | **검색 필터** — 조문 단위 좁히기 |

> `cust_attr6~10` 은 쓰지 않았습니다. 규격서 **본문은 5개**라 하고 **표는 10개**로
> 적혀 있어 어느 쪽이 맞는지 확인이 필요합니다 (§7 미확정 항목 2번).

---

## 4. `h1`~`h4` 판별 규칙

규정 문서의 조항 번호 패턴으로 결정론적으로 정합니다. **AI 판단을 쓰지 않습니다** —
같은 문서를 두 번 넣으면 같은 결과가 나와야 하기 때문입니다.

```
h1  ←  I. II. III …            (로마 숫자)
h1  ←  제1조 · 제2장 · 제3절 …
h2  ←  1. 2. 3. …
h3  ←  가. 나. 다. …  /  (1) (2) …
h4  ←  그 외 헤딩으로 판별된 것
```

> **현재 알려진 한계**: HWP 문서의 대제목이 1×1 표 안에 들어 있으면 `table` 로 분류되어
> `h1` 이 만들어지지 않습니다. 실측(`(2024년)대출모집인 광고 유의사항`): `h4` 1개, `h1` 0개.
> 이 경우 KL 에서 본문에 최상위 `meta` 가 비게 됩니다.
> `tests/spec_check.py` 가 이 상황을 **경고로 알려줍니다.**

---

## 5. 실행 방법

### 로컬 왕복 시험 (이 폴더만으로 가능)

```bash
# 1) 진입점 의존성
python -m venv .venv-entry
.venv-entry/Scripts/python.exe -m pip install -r requirements.txt

# 2) 파싱 본체 (로컬 in-process 시험용. 배포 시에는 에이전트 서비스에 있습니다)
.venv-entry/Scripts/python.exe -m pip install -e ..

# 3) 서버 기동
./scripts/run-local.sh                      # http://127.0.0.1:9101

# 4) 왕복 시험 (다른 터미널에서)
./scripts/test-roundtrip.sh
```

실제 실행 결과 (2026-08-21):

```
입력: (2024년)대출모집인 광고 유의사항_은행연합회.hwp  (80,384 bytes)
POST /parsing → 202  {"result": "OK", "body": {"uuid": "0b4a…", "timeout": 600}}
GET  /parsing/result → 200 zip 1,724 bytes (4s 대기)
zip 내용 2개: ['…_hrc.json', '…_hrc.jsonl']

[OK ] (2024년)대출모집인 광고 유의사항_은행연합회.hwp_hrc.jsonl  (6행)
        경고: h1 이 없다 (h2 이하만 있음) — 최상위 meta 가 비게 된다
검사 1건 · 실패 0건
```

### 배포 형태 (에이전트 분리)

```bash
export AGENT_URL=http://agent-service:8080
./scripts/run-local.sh
```

이때 진입점에는 `requirements.txt` 의 3개만 있으면 됩니다 — `nh_parsing` 불필요.

### 환경 변수

| 이름 | 기본값 | 뜻 |
|---|---|---|
| `PATH_TEMP` | `./temp` | 작업 디렉터리 상위 |
| `TIMEOUT` | `600` | 202 응답의 `timeout` 값(초) |
| `AGENT_URL` | (없음) | 있으면 remote 모드 |
| `AGENT_TIMEOUT` | `600` | 에이전트 호출 타임아웃(초) |
| `KEEP_WORK_DIR` | (없음) | `1` 이면 성공해도 작업 디렉터리 보존(개발용) |

---

## 6. 규격 검사기

우리 출력이 규격을 지키는지 코드로 검사합니다. **농협 정답 샘플도 같은 검사기를 통과합니다.**

```bash
.venv-entry/Scripts/python.exe tests/spec_check.py samples/out
.venv-entry/Scripts/python.exe tests/spec_check.py <농협 out_sample_hrc.jsonl 경로>
```

검사 항목은 §2 표의 8~15번입니다. 규격서와 정답 샘플이 어긋나는 항목은 **오류가 아니라
경고**로 냅니다 — 어느 쪽이 맞는지는 확인이 필요한 사항이기 때문입니다.

---

## 7. 확인이 필요한 사항

우리가 판단할 수 없어 농협 확인이 필요한 항목입니다. 지금은 **규격서 본문을 따르고
있습니다.**

| # | 항목 | 규격서 | 정답 샘플 | 우리 처리 |
|---|---|---|---|---|
| 1 | `item` 유형 `break` | 없음 | **1행에 있음** | 쓰지 않음. 검사기는 경고만 |
| 2 | `cust_attr` 개수 | 본문 **5개** | 표 **10개** | 5개까지만 사용 |
| 3 | `image` 의 `width`·`height` 단위 | mm | PDF 는 pt, HWP 는 cm 로 보임 | mm 기준으로 계산 |
| 4 | `image` 의 `ratio` 기준 | "페이지 크기 대비" | `"11%"` (문자열) | 문자열 `%` |
| 5 | `_hrc.jsonl` 의 `item` 이 곧 청크인가 | 명시 없음 | — | 청크 = item 1:1 로 가정 |
| 6 | `image` 의 `value` | "이미지 파일명" | **절대경로**가 들어 있음 | 파일명만 |
| 7 | `parsed_status` 의 값 목록 | 명시 없음 | `"S"` | `"S"` 고정 |
| 8 | `page_info` 단위 | 명시 없음 | PDF `595.3x841.9`(pt), HWP `21.0x29.7`(cm) | PDF 는 pt 실측, HWP 는 **빈 배열**(모르는 값을 지어내지 않음) |
| 9 | KL 검색(조회) API 규격 | **자료에 없음** | — | 미구현 — 규격 요청 필요 |

---

## 8. 폴더 구성

```
kl_parser/
├── README.md                        이 문서
├── requirements.txt                 진입점 의존성 (반입 대상 3개)
├── app/
│   ├── main.py                      진입점 — POST /parsing, GET /parsing/result/{uuid}
│   └── service/
│       ├── status.py                농협 예제의 DO NOT EDIT 블록 (그대로)
│       └── parsing_service.py       파싱 본체 — 우리 파이프라인 호출
├── scripts/
│   ├── run-local.sh                 서버 기동
│   └── test-roundtrip.sh            왕복 시험
├── tests/
│   ├── roundtrip.py                 POST → 폴링 → zip → 규격 검사
│   └── spec_check.py                규격 검사기 (단독 실행 가능)
└── samples/
    ├── in/                          입력 예
    └── out/                         출력 예 (왕복 시험이 채운다)
```
