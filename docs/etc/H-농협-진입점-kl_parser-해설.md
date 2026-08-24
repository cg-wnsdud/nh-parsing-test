# H. 농협 진입점(`kl_parser`) — 농협이 준 파일부터 우리 구현까지

> **무엇인가**: `kl_parser/` 폴더가 뭐고, 왜 그렇게 생겼는지를 **처음 보는 사람 기준**으로
> 풀어 쓴 문서. 농협이 준 예제 파일 18개가 각각 뭔지부터 시작해서, 그걸 보고 우리가
> 무엇을 어떻게 짰는지, 모르는 용어(`gunicorn`·`ASGI`·`worker`…)가 뭔지까지 담았다.
>
> **작성 기준일** 2026-08-24 · 왕복 시험 7건 실측 직후
> **원칙**: 농협 원본 파일과 우리 코드를 **직접 열어 대조한 것만** 적었다.
> 농협 자료끼리 어긋나는 대목은 어느 쪽을 따랐는지 함께 적었다.

**읽는 순서**: §1(전체 그림) → §2(용어) → §3(농협 파일 18개) → §4(계약) →
§5(우리 구현) → §6(고친 것) → §7(안 된 것). 급하면 §1 + §7만 봐도 된다.

---

## 1. 30초 요약 — 이 폴더가 뭔가

농협에는 **GenAI Knowledge Lake**(줄여서 KL)라는 사내 시스템이 있다. 문서를 넣으면
쪼개서 벡터DB에 색인해 두고, 나중에 "이런 규정이 어디 있었지?"를 검색할 수 있게 해 준다.

그런데 KL의 기본 문서 파서는 한글 금융 문서(HWP, 광고 PDF)를 잘 못 읽는다. 그래서 KL은
**"파서를 직접 만들어 꽂을 수 있는 구멍"**을 열어 뒀다. 그게 **Custom 파서 슬롯**이다.

```
   ┌── 농협 KL ────────────────────────────┐
   │  문서를 받으면 파서에게 보낸다        │
   │       ↓ (HTTP 호출)                   │
   │  [ Custom 파서 슬롯 ]  ← 여기가 빈칸  │
   │       ↑                               │
   │  결과(jsonl)를 받아 벡터DB에 색인     │
   └───────────────────────────────────────┘
                  ↑
        여기에 꽂는 게 `kl_parser/` 다
```

**`kl_parser/`는 파싱을 하는 코드가 아니다.** 파싱 본체는 `src/nh_parsing/`(A 문서 참조)에
이미 있고, `kl_parser/`는 **"KL이 부르는 방식대로 전화를 받아서, 파싱 본체에 넘기고,
KL이 원하는 모양으로 포장해 돌려주는" 껍데기**다. 그래서 코드가 900줄뿐이다.

비유하면 — 파싱 본체가 주방이고, `kl_parser/`는 **주문 받는 창구**다. 창구는 요리를 하지
않는다. 다만 주문서 양식(`src_file`, `option`), 번호표 발급(uuid), "아직 조리 중입니다"
응대(PARSING), 포장 규격(zip)을 정확히 지켜야 한다. 창구가 규격을 어기면 주방이 아무리
잘해도 손님(KL)이 음식을 못 받는다.

---

## 2. 모르는 용어 — 먼저 이것만

문서를 읽다 막히면 대부분 여기 있다. **아래로 갈수록 세부적이다.**

### 2-1. HTTP · API · POST/GET

**HTTP**는 프로그램끼리 대화하는 규칙이다. 웹브라우저가 서버에 페이지를 요청할 때 쓰는
그 규칙이고, 프로그램끼리도 똑같이 쓴다. **API**는 "우리 프로그램에 이런 식으로 말을
걸면 이런 식으로 답해 준다"는 약속이다.

| | 뜻 | 이 프로젝트에서 |
|---|---|---|
| **POST** | 무언가를 **보내면서** 요청 | KL이 파일을 보내며 "파싱해 줘" |
| **GET** | 무언가를 **달라고** 요청 | KL이 "그 결과 됐어?" |
| **상태 코드** | 답장 맨 앞에 붙는 3자리 숫자 | `200` 성공 · `202` 접수함 · `500` 서버 오류 |

`202`가 중요하다. `200`(다 됐음)과 달리 **`202`는 "요청은 받았고 지금 처리 중"**이라는
뜻이다. 파싱이 10분 걸리는데 KL을 10분 동안 붙잡아 둘 수 없으니, 일단 202로 번호표를
주고 KL이 나중에 다시 물어보게 한다. (§4)

### 2-2. FastAPI · uvicorn · gunicorn · ASGI · worker

이 넷이 제일 헷갈리는데, **역할이 층으로 나뉜다.**

```
   KL 이 HTTP 로 전화를 건다
        ↓
 ┌──────────────────────────────────────────────┐
 │ gunicorn   "관리자"                          │
 │   프로세스를 몇 개 띄울지, 죽으면 다시       │
 │   살릴지, 몇 초 응답이 없으면 죽일지 관리    │
 │  ┌────────────────────────────────────────┐  │
 │  │ uvicorn worker  "전화 받는 직원" ×N    │  │
 │  │   실제로 HTTP 를 해석해 파이썬 함수를  │  │
 │  │   불러 주는 일꾼                       │  │
 │  │  ┌──────────────────────────────────┐  │  │
 │  │  │ FastAPI (우리 main.py)           │  │  │
 │  │  │   "/parsing 으로 POST 가 오면    │  │  │
 │  │  │    이 함수를 실행해라" 는 정의   │  │  │
 │  │  └──────────────────────────────────┘  │  │
 │  └────────────────────────────────────────┘  │
 └──────────────────────────────────────────────┘
```

- **FastAPI** — 파이썬으로 API를 짤 때 쓰는 도구(라이브러리). `@app.post("/parsing")`
  처럼 한 줄 적으면 "이 주소로 POST가 오면 아래 함수를 실행"이 된다. **우리가 코드를
  쓰는 층**이다.
- **ASGI** — FastAPI가 만든 앱과, HTTP를 실제로 처리하는 프로그램 사이의 **표준 규격**.
  콘센트 규격 같은 것이다. 규격이 같으니 uvicorn을 다른 걸로 바꿔도 코드는 그대로다.
- **uvicorn** — 그 ASGI 규격에 맞춰 HTTP를 실제로 주고받는 프로그램. 이게 없으면
  FastAPI 코드는 그냥 함수 덩어리일 뿐 아무도 부를 수 없다.
- **gunicorn** — uvicorn을 **여러 개 띄워 관리하는 상위 관리자**. 왜 필요한가:
  ① 요청을 동시에 여러 개 처리하려면 일꾼이 여럿 필요하다 ② 하나가 죽어도 서비스가
  안 멈춘다 ③ "180초 동안 아무 응답 없는 일꾼은 죽이고 새로 띄운다" 같은 정책을 걸 수 있다.
- **worker** — gunicorn이 띄운 일꾼 하나하나. 농협 설정은 개발 2개 / 운영 8개다.

**로컬 개발에서는 gunicorn을 생략하고 uvicorn만 띄워도 돌아간다.** 우리가 지금까지 그렇게
시험했다 — 그리고 그게 §6-①의 사고 원인이었다.

### 2-3. 그 외

| 용어 | 뜻 |
|---|---|
| **멀티파트 폼 데이터** | 파일을 HTTP로 보낼 때 쓰는 포장 방식. 웹페이지에서 "파일 선택" 후 업로드할 때 쓰는 그것 |
| **base64** | 아무 데이터나 영문·숫자 글자열로 바꿔 놓는 인코딩. 파일이나 JSON을 HTTP 한 칸에 안전하게 실어 보낼 때 쓴다 |
| **jsonl** | 한 줄에 JSON 하나씩 적은 파일(JSON **L**ines). 줄 단위로 읽을 수 있어 대용량에 유리하다 |
| **uuid** | 중복되지 않는 긴 식별자(`1987f9cdf49c465e…`). 여기서는 **번호표** 역할 |
| **폐쇄망** | 인터넷이 끊긴 내부 전용 네트워크. `pip install` 이 안 되므로 필요한 패키지를 파일로 들고 들어가야 한다("반입") |
| **whl** | 파이썬 패키지를 미리 빌드해 둔 설치 파일(`.whl`). 폐쇄망 반입용 |
| **VectorDB** | 문장을 숫자 벡터로 바꿔 저장해 "뜻이 비슷한 문장"을 찾을 수 있게 한 DB. KL이 이걸로 검색한다 |

---

## 3. 농협이 준 파일 18개 — 각각 뭔가

경로: `C:\Users\cccjj\cginside\농협프로젝트\1.awx_custom_parser_example_api\`

```
1.awx_custom_parser_example_api/
├── readme.md                    ★★★ 규격 본문 — 출력 파일 형식의 정본
├── 2.Package.txt                    농협 베이스 이미지에 깔린 패키지 287개 목록
├── in_sample.hwp                    입력 예시 (인사관리 규정 HWP)
├── out_sample_hrc.jsonl         ★★  출력 예시 = "정답 샘플"
├── out_sample_hrc.json              출력 예시 (문서 정보 쪽)
├── image/BIN0001.jpg                위 샘플에서 추출된 이미지
└── server/flow/
    ├── readme/
    │   ├── parser_howto.txt     ★★★ 통신 규격 — 202·폴링·ERROR 응답의 정본
    │   └── parser_readme.txt        "이 순서로 실행하세요" 4줄
    ├── setup-application.sh         whl 설치 1줄
    ├── run-application.sh       ★★  기동 스크립트 — **폴더 구조를 여기서 강제한다**
    ├── test-application.sh          왕복 시험 예시 (curl + jq)
    ├── requirements.txt             python-multipart 한 줄
    ├── whl/python_multipart-…whl    그 whl 실물
    ├── dummy.txt                    시험용 빈 입력 파일
    └── app_custom_parser/       ★★★ **예제 코드 본체 — 이걸 베꼬 고친다**
        ├── main.py                    진입점 (POST/GET 두 개)
        ├── gunicorn_config.py         기동 설정
        └── service/parsing_service.py 파싱 자리 + 상태 파일 함수
```

### 3-1. `readme.md` — **출력 형식의 정본**

"결과 jsonl을 어떻게 생겼게 만들어라"가 여기 있다. 핵심만:

```
· 각 행은 item 하나.  item 유형은 text · table · image · h1~h4
· value 는 필수.  text/h1~h4는 글자, table은 HTML <table>, image는 파일명
· h1~h4 는 제목 계층. h1 이 가장 큰 제목
· table 은 type_property.title 필수 (없으면 "")
· image 는 type_property 의 title·width·height·ratio 필수
· page 를 모르면 0
· 사용자 메타: cust_attr1~5(조회용) / cust_sattr1~5(검색+조회용)
```

**왜 h1~h4가 중요한가.** `readme.md` 맨 끝 "VectorDB 활용 예"를 보면 KL은 `h1`~`h4`를
**아래 본문들의 meta로 자동 부착**한다. 즉 `h1: "1. 쿠버네티스"` 아래의 모든 text에
"이건 1. 쿠버네티스 절 내용"이라는 꼬리표가 붙어 검색 품질이 올라간다. h1이 하나도
없으면 그 꼬리표가 비게 된다 — 그래서 우리 검사기가 h1 부재를 **경고**로 낸다.

**파일명 규칙**도 여기 있다. 원본이 `광고규정.hwp`면 결과는 `광고규정.hwp_hrc.jsonl`
(확장자를 떼지 않고 뒤에 붙인다)이다.

### 3-2. `parser_howto.txt` — **통신 규격의 정본**

42줄짜리 텍스트인데 여기가 통신 방식의 정본이다. 그대로 인용하면:

```
POST /parsing
  · form data : src_file (원본 문서), option (json string)
  · 정상 response status : 202
  · body 예) {"result":"OK","body":{"uuid":"1237…","timeout":600}}
  · timeout 은 생략 가능하며 기본값은 3시간입니다.
  · 오류인 경우 status code 500

GET /parsing/result/{uuid}
  · 파싱 진행중  → status 200, {"status":"PARSING"}
  · 파싱 오류    → status 200, {"status":"ERROR", "message": "..."}   ← ★
  · 그 외 오류   → status 500, {"message": "..."}
  · 파싱 완료    → status 200, zip 파일
```

★ 표시한 줄이 §6-②의 근거다. **파싱 오류는 500이 아니라 200이다.**

### 3-3. `run-application.sh` — **폴더 구조를 강제하는 파일**

농협이 컨테이너에서 서버를 띄우는 스크립트다. 앞부분은 `DO NOT EDIT` 주석이 붙어 있고,
마지막 줄이 핵심이다:

```bash
export FLOW_APP_NAME="app_custom_parser"     # 5행
...
cd $FLOW_APP_DIR/$FLOW_APP_NAME              # 44행 — 그 폴더로 들어간다
...
gunicorn main:app --config gunicorn_config.py  # 64행
```

**이 세 줄이 우리 폴더 이름과 파일 배치를 결정한다.**

- 폴더 이름이 **반드시 `app_custom_parser`** 여야 한다 (5행이 그 이름을 고정)
- 그 폴더 **안으로 들어가서**(44행) 거기서 `main:app`을 띄운다(64행)
- 따라서 `main.py`가 그 폴더 바로 안에 있어야 하고, gunicorn은 그걸
  **최상위 모듈 `main`**으로 읽는다 (`app_custom_parser.main`이 아니다)

마지막 줄의 파급이 커서 따로 설명한다 → §6-①

### 3-4. `app_custom_parser/main.py` — 예제 진입점 (251줄)

우리가 베낀 원본이다. 구조는 단순하다:

| 부분 | 하는 일 |
|---|---|
| `POST /parsing` | ① 파일 저장 ② uuid 생성 ③ 상태를 PARSING으로 기록 ④ **백그라운드로 파싱 시작** ⑤ 즉시 202 응답 |
| `GET /parsing/result/{uuid}` | 상태 파일을 읽어 DONE이면 zip을, PARSING이면 그렇다고, 아니면 500 |
| `run_method_in_subprocess()` | 파싱을 **별도 프로세스**로 띄우고 timeout 감시 |
| `GET /health` | 살아 있냐는 물음에 `{"status":"OK"}` |

**주의할 대목이 예제 코드 안에 있다.** 194~196행:

```python
else:
    status_code = 500          # ← 파싱 오류인데 500 을 준다
    result_form = {"message": _parse_msg}
```

`parser_howto.txt`는 이 상황을 **200 + `{"status":"ERROR"}`**라고 적었다.
**규격서와 예제 코드가 서로 다르다.** 우리는 규격서를 따랐고 확인 항목으로 올렸다.

### 3-5. `app_custom_parser/service/parsing_service.py` — 파싱 자리 (95줄)

세 부분이다:

1. `write_parse_status()` / `get_parse_status()` — **`DO NOT EDIT`** 이라고 명시된
   두 함수. 작업 폴더에 `genaikl.status` 파일을 만들어 `PARSING`/`DONE`/`ERROR`를
   기록한다. KL이 이 파일 형식에 의존하므로 형식을 바꾸면 연동이 깨진다.
   → 우리는 **글자 그대로 옮겼고**, 우리 로직과 섞이지 않게 `status.py`로 따로 뺐다.
2. `parse()` — **`TODO: M A I N`** 이라고 적힌 자리. 예제는 여기에 하드코딩 문자열
   (`sample_jsonl`)을 쓴다. **이 자리를 우리 파이프라인 호출로 바꾸는 게 구현의 전부다.**
3. `sample_jsonl` — 쿠버네티스 문서 예시 하드코딩. "구현 완료 후 주석처리"라고 적혀 있다.

**여기 문법 오류가 하나 있다.** 72~73행:

```python
except Exception as e:(
    write_parse_status(work_dir, "ERROR", str(e)))
```

괄호 위치가 이상하다. 실제로 `py_compile`로 확인한 결과 **문법 오류는 아니고**
(`:` 뒤에 괄호로 감싼 식이 와도 파이썬은 받아들인다) 동작도 한다. 우리 저장소의 옛
README가 "문법 오류"라고 적었던 것은 **틀린 서술**이었고, 그래도 읽기 어려운 코드라
우리는 평범한 형태로 다시 썼다.

### 3-6. `gunicorn_config.py` — 기동 설정

```python
import dlp                                        # ← 농협 사내 모듈
worker_class = "uvicorn.workers.UvicornWorker"    # uvicorn 을 일꾼으로 쓴다
timeout      = 180      # 180초 동안 응답 없는 일꾼은 죽이고 새로 띄운다
graceful_timeout = 30   # 죽일 때 30초는 마무리할 시간을 준다
workers      = 2        # 일꾼 2개 (운영 환경이면 8)
bind         = "0.0.0.0:9101"
if dlp.is_infer_env():  # 운영 환경 판별
    workers = 8
```

`import dlp`가 **농협 DAP 환경에만 있는 사내 모듈**이라, 이 파일은 우리 PC에서 실행할
수 없다. 그래서 **우리 저장소에는 이 파일이 아직 없다** → §7-①

### 3-7. `2.Package.txt` — 반입 목록을 정하는 근거

농협 베이스 이미지에 이미 깔린 패키지 287개 목록이다. **여기 있으면 반입할 필요가 없다.**
직접 대조한 결과:

| 패키지 | 농협 이미지 | 우리 시험 환경 | 반입 필요? |
|---|---|---|---|
| fastapi | 0.139.2 | 0.115.6 | **불필요** |
| uvicorn | 0.51.0 | 0.34.0 | **불필요** |
| python-multipart | 0.0.32 | 0.0.17 | **불필요** |
| gunicorn | 26.0.0 | (안 씀) | 불필요 |
| pillow / numpy / requests / pydantic / httpx | 있음 | — | 불필요 |

**즉 반입은 0건이다.** 2026-08-24 이전 우리 `requirements.txt`는 위 3개를 "반입이
필요한 것"이라 적고 있었는데 **사실과 달랐다** — 고쳤다. (농협 예제의
`setup-application.sh`가 multipart whl을 동봉하는 것도, 지금 이미지 기준으로는
불필요한 잔재로 보인다.)

**남는 문제는 버전 차이다.** 우리가 시험한 fastapi 0.115와 농협 이미지의 0.139는
마이너 3칸 차이다. 그 조합에서 도는지는 농협 환경에서만 확인할 수 있다 → §7

### 3-8. `out_sample_hrc.jsonl` — 정답 샘플, 그런데 규격서와 어긋난다

농협이 준 "이런 모양으로 만들어라"의 실물이다. 그런데 규격서와 다른 대목이 있다:

```jsonl
{"item": "break", "value": "", "page": 0}      ← 규격서에 없는 item 유형
{"item": "image", "value": "/HYB008/datasets/…/BIN0001.jpg", …}
                          ↑ 규격서는 "이미지 파일명"이라 했는데 절대경로가 들어 있다
```

이런 어긋남이 여러 건이라 **우리 검사기(`spec_check.py`)는 규격서를 기준으로 검사하고,
정답 샘플에만 있는 것은 오류가 아니라 경고로 낸다.** 어느 쪽이 맞는지는 농협 확인
사항이다(README §7에 8건 정리).

---

## 4. 통신 계약 — 왜 두 번 부르나

가장 중요한 개념이다. **파싱이 오래 걸리기 때문에 한 번에 못 끝낸다.**

```
KL                                    우리 진입점(kl_parser)
 │                                            │
 ├─ POST /parsing ──────────────────────────▶ │  파일 + option 받음
 │   (src_file=광고.pdf, option=base64)       │  ① 작업폴더 temp/<uuid>/ 만들고 파일 저장
 │                                            │  ② genaikl.status 에 PARSING 기록
 │                                            │  ③ 백그라운드로 파싱 시작 (10분 걸릴 수도)
 │ ◀──────── 202 {uuid, timeout} ─────────────┤  ④ 기다리지 않고 즉시 번호표 발급
 │                                            │
 │   … 파싱은 계속 돌아간다 …                 │
 │                                            │
 ├─ GET /parsing/result/<uuid> ─────────────▶ │  상태 파일을 읽는다
 │ ◀──────── 200 {"status":"PARSING"} ────────┤  아직 중
 │   (KL 이 잠시 후 다시 물어본다 = 폴링)     │
 │                                            │
 ├─ GET /parsing/result/<uuid> ─────────────▶ │
 │ ◀──────── 200 + zip 파일 ──────────────────┤  DONE → 결과를 zip 으로 포장
 │                                            │  (성공했으면 작업폴더 삭제)
```

**폴링(polling)** = "됐어? 됐어?" 하고 되묻는 방식. `timeout` 값은 **KL이 얼마나
되물어 볼지**를 우리가 알려주는 값이다. 짧게 신고하면 우리가 아직 파싱 중인데 KL이
먼저 포기한다 → §6-④가 정확히 이 사고였다.

**작업 폴더의 생김새:**

```
temp/<uuid>/
├── 광고.pdf                    KL 이 보낸 원본
├── genaikl.status              {"status":"PARSING"} → DONE 또는 ERROR
├── 광고.pdf_hrc.jsonl          결과 (규정문서 트랙)
├── 광고.pdf_hrc.json           문서 정보
├── image/                      추출 이미지들 → _img.zip 으로 묶인다
└── kl_parser_notes.json        우리 검수용 (**zip 에 안 넣는다**)
```

---

## 5. 우리 구현 — 파일 하나씩

### 5-1. 농협 파일 ↔ 우리 파일 대응

| 농협 원본 | 우리 것 | 관계 |
|---|---|---|
| `app_custom_parser/main.py` | `kl_parser/app_custom_parser/main.py` | **구조 유지, 3곳 다르게** (§5-3) |
| `service/parsing_service.py` 의 상태 함수 2개 | `service/status.py` | **글자 그대로** (DO NOT EDIT) |
| `service/parsing_service.py` 의 `parse()` | `service/parsing_service.py` | **여기가 우리 구현** |
| `gunicorn_config.py` | (없음) | §7-① |
| `run-application.sh` | `scripts/run-local.sh` | 로컬용으로 다시 씀 |
| `test-application.sh` (curl+jq) | `tests/roundtrip.py` 등 3개 | 파이썬으로 다시 씀 (§5-4) |
| `readme.md` 출력 규격 | `tests/spec_check.py` | **규격을 검사 코드로 옮김** |

### 5-2. 폴더 구성

```
kl_parser/
├── README.md                    조항별 대조표 · 실측 결과
├── requirements.txt             시험 환경 재현용 (반입 목록 아님 — §3-7)
├── app_custom_parser/           ← 이름을 바꿀 수 없다 (§3-3)
│   ├── main.py                    진입점: POST /parsing · POST /ad/parsing · GET 결과
│   └── service/                   (__init__.py 없음 — 예제와 동일)
│       ├── status.py                농협 DO NOT EDIT 블록
│       └── parsing_service.py       우리 파이프라인 호출
├── scripts/                     기동·시험 스크립트
├── tests/
│   ├── boot_check.py              기동·실패경로 (서버 불필요, 1초)
│   ├── roundtrip.py               규정문서 왕복
│   ├── roundtrip_ad.py            광고물 왕복
│   └── spec_check.py              결과 jsonl 규격 검사
└── samples/{in,out,out_ad}/     시험 입출력
```

### 5-3. 예제와 일부러 다르게 한 3가지

**① 파싱을 별도 프로세스가 아니라 같은 프로세스의 백그라운드 작업으로 돌린다.**

농협 예제는 이렇게 한다:

```python
command = [python, "-c", f"from service.parsing_service import parse; parse({','.join(map(repr, args))})"]
subprocess.Popen(command, …)
```

파싱 인자를 `repr()`로 **문자열에 박아 명령줄을 만든다.** 경로에 따옴표·역슬래시가
있으면 깨진다. 우리 입력 파일명은 한글·공백·괄호가 흔하다
(`(2023년)예금성상품 광고시 준수사항_은행연합회.hwp`). 그래서 FastAPI의
`BackgroundTasks`로 같은 프로세스에서 부른다.

규격이 요구하는 것은 **"응답을 즉시 202로 돌려주는 것"**이고 그건 지켜진다.
(다만 gunicorn `timeout=180`과의 상호작용은 §7-② 확인 필요.)

**② 실패한 작업 폴더를 지우지 않는다.** 예제는 ERROR일 때도 지운다. 그러면 무엇이 왜
실패했는지 남지 않는다. 성공(DONE) 시에는 예제와 같이 지운다.

**③ 우리 검수용 파일(`kl_parser_notes.json`)은 zip에 넣지 않는다.** 규격에 없는
파일이라서다. 왜 만드나: `_hrc.jsonl`만 보면 "왜 이 문단을 h2로 봤는지"를 나중에
설명할 수 없다.

### 5-4. 시험 3종 — 무엇을 보는가

| 시험 | 서버 필요? | 무엇을 보나 | 소요 |
|---|---|---|---|
| `boot_check.py` | 아니오 | ① 상대 import 없나 ② 농협 방식으로 기동되나 ③ 실패=200+ERROR인가 | 1초 |
| `roundtrip.py` | 예 | 규정문서: 202 → 폴링 → zip → `spec_check` | 4~44초 |
| `roundtrip_ad.py` | 예 | 광고물: 위와 같고 + **진입점 결과 = 직접 실행 결과인가** | 70~630초 |
| `spec_check.py` | 아니오 | `_hrc.jsonl`이 `readme.md` 규격을 지키나 | 즉시 |

`roundtrip_ad.py`의 마지막 항목이 중요하다. 진입점은 껍데기여야 하므로 **같은 입력이면
직접 실행과 같은 결과**가 나와야 한다. 다르면 껍데기가 뭔가를 바꾸고 있다는 뜻이다.
단 비교할 때 **층을 갈라야** 한다:

```
정본층 (source = ocr / digital)   결정론이다 → 다르면 진입점 결함, 실패로 올린다
VLM 층 (source = vlm_sweep 등)    실행마다 흔들린다 → 수치만 찍는다
```

이걸 안 갈랐다가 "줄 수 53 vs 55"로 **없는 결함**을 만들어 낸 적이 있다(§6 아래).

### 5-5. 두 트랙 — 산출물이 다르다

이 프로젝트에는 성격이 완전히 다른 입력 두 종류가 있다(00 문서 참조). 진입점도 갈라진다.

| | 규정문서 `POST /parsing` | 광고물 `POST /ad/parsing` |
|---|---|---|
| 예 | `(2023년)예금성상품 광고시 준수사항.hwp` | `13. 대출성상품.pdf` |
| 성격 | 검색당하는 쪽 = **지식** | 검색하는 쪽 = **질문** |
| 받는 곳 | KL 벡터DB에 색인 | 심의 엔진 (색인 안 함) |
| 산출물 | `_hrc.jsonl` · `_hrc.json` · `_img.zip` | `_parsed.json`(통합) · `ad_summary.json` · `_img.zip` |
| 좌표 | 없음 | **있다** (쪽·영역·줄 bbox) |
| 이미지 | 추출 이미지 + **VLM 캡션**을 text item으로 함께 색인 | 쪽 원본 이미지 |

**광고물이 `_hrc.jsonl`이 아닌 이유:** 심의는 "이 지적의 근거가 원본 어디인가"를 화면에
표시해야 한다. `_hrc.jsonl` 규격에는 좌표를 담을 칸이 없어 그 정보가 전부 버려진다.
그래서 광고물은 좌표·템플릿 라벨이 살아 있는 통합 JSON을 낸다. `/ad/parsing`은 **KL이
부르는 자리가 아니고** 심의 엔진이 부르는 자리다 — 통신 방식만 같게 맞췄다.

---

## 6. 2026-08-24에 고친 것 — 왜 그동안 안 잡혔나

여섯 건 전부 **"코드가 틀렸다"보다 "우리 시험이 그 경로를 안 태웠다"**가 원인이었다.

### ① 농협 방식으로 기동하면 뜨지도 않았다

농협은 `cd app_custom_parser && gunicorn main:app`으로 띄운다(§3-3). 그러면
`main.py`가 **최상위 모듈**로 읽힌다. 이때 파이썬은 `from .service…` 같은
**상대 import를 거부한다** — "나는 어느 패키지에 속한 모듈인지 모른다"는 뜻의
`attempted relative import with no known parent package` 오류가 난다.

```
고치기 전                              고친 후
kl_parser/app/main.py                  kl_parser/app_custom_parser/main.py
  from .service.status import …          from service.status import …
띄우는 법: uvicorn app.main:app        띄우는 법: cd app_custom_parser; uvicorn main:app
→ 로컬은 통과, 농협 배포에서만 깨짐   → 양쪽 다 같은 경로로 뜬다
```

농협 예제도 절대 import(`from service.parsing_service import …`)를 쓰고 `service/`에
`__init__.py`를 두지 않는다. **예제를 그대로 따르는 게 정답이었다.**

**한 번 더 걸렸다.** 최상위 import를 다 고친 뒤에도 함수 **안에** 숨은
`from .service.status import write_parse_status` 하나가 남아 있었다. 그건 POST가
실제로 들어와야 실행되므로 `import main`만으로는 안 잡혔고, 첫 왕복에서
`500 ImportError`로 드러났다. → 그래서 `boot_check.py`가 **소스를 AST로 훑어**
상대 import를 정적으로 잡도록 했다.

### ② 파싱 실패 응답이 500이었다 (규격은 200)

`parser_howto.txt`는 파싱 오류를 **200 + `{"status":"ERROR"}`**, 그 외 오류(uuid를
못 찾는 등)를 500으로 나눈다. 우리는 둘을 한 분기로 묶어 전부 500을 줬다.
정상 파일만 넣어 시험하면 이 분기를 지나가지 않아서 안 잡혔다.

고친 뒤 **일부러 되돌려 시험이 정말 잡는지도 확인했다** — 되돌리면 NG, 고치면 OK.

### ③ 광고 트랙 산출물을 통합 JSON으로 교체

전에는 파싱 결과(`AdDocument`)를 그대로 냈다. 이제 좌표 + 템플릿 라벨을 합친 통합
JSON을 낸다. 로컬 실행(`tools/run_ad_label.py`)과 **같은 함수**
(`ad_export.process_ad_file`)를 쓴다 — 판정 순서를 두 곳에 따로 적으면 한쪽만 고치는
사고가 난다.

### ④ 신고한 timeout(600초)을 실측이 넘었다

```
13. 대출성상품.pdf   627초   ← 600 이었으면 시연 중에 KL 이 먼저 포기했다
2. 예금성상품.pdf    153초
올원e적금.png        198초
규정문서 HWP/PDF     4~44초
```

**대부분이 VLM 대기다** — 대출성 312초 중 309초. `TIMEOUT` 기본값을 1800으로 올렸다.
규격상 이 값을 생략하면 기본 3시간이므로 1800은 그보다 보수적이다.

### ⑤ 환경이 다르면 파싱 결과가 갈렸다 — 가장 위험했던 것

진입점 결과와 직접 실행 결과가 계속 달랐다(69줄 중 글자 3건, 좌표 13건). 처음엔
"OCR 재현성 문제"로 결론냈는데 **틀렸다.** 같은 진입점을 연달아 두 번 돌리니
0건 차이였다 — 즉 시간 문제가 아니고 **두 환경의 차이**였다.

```
개발 venv   pypdfium2 5.12.0 → 같은 PDF·같은 dpi·같은 크기 렌더 → 픽셀 해시 eda2fc94…
진입점 venv pypdfium2 5.13.0 →                                   픽셀 해시 0d36adc1…
                                                                  ↑ 다르다
그래서 OCR 이 다르게 읽었다:  '7적금' ↔ '기적금',  'I 제공' ↔ 'ㅣ 제공'
```

`pyproject.toml`이 `pypdfium2>=5.9.0,<6.0.0` **범위**여서 환경마다 다른 버전이 깔린
것이다. `==5.12.0`으로 못박아 해소했고, 재실행해서 차이가 사라진 것을 확인했다.

**농협 폐쇄망에도 우리가 whl을 반입하므로 여기서 고정하지 않으면 시연 결과와 납품
결과가 달라진다.**

### ⑥ 대조 방식이 없는 결함을 만들어 냈다

위 ⑤를 조사하다 발견. 진입점과 직접 실행을 통째로 비교해 "줄 수 53 vs 55"로 실패를
냈는데, 층을 갈라 보니 OCR 49줄은 완전 일치이고 `vlm_sweep`만 4↔6이었다. **VLM 층은
실행마다 흔들리는 것이 이미 문서화된 사실**이라 비교 대상이 아니었다. → §5-4

---

## 7. 아직 안 된 것 — 시연 전에 알아야 할 것

지금까지 확인한 것은 **"농협 규격 문서와 예제 코드 기준으로 우리 코드가 프로토콜을
맞게 지키는가"**이다. **"농협 실제 인프라에 꽂아서 도는가"는 확인할 방법이 없다** —
폐쇄망 접속이 안 되는 상태다. 이 둘을 구분해야 한다.

| # | 항목 | 상태 | 막힌 이유 |
|---|---|---|---|
| ① | `gunicorn_config.py` | **파일 없음** | 농협 원본이 `import dlp`(사내 모듈)를 쓴다. 우리 PC에서 실행 불가 |
| ② | 실제 `gunicorn` 기동 | **한 번도 안 해봄** | 위와 같음. 지금 검증은 `uvicorn`으로 하고 "import 구조가 맞는가"만 확인 |
| ③ | gunicorn `timeout=180` × 우리 파싱 627초 | **미검증** | 백그라운드 작업이 일꾼을 붙잡으면 180초에 죽을 수 있다. 실제 gunicorn으로 재야 안다 |
| ④ | 라이브러리 버전 차이 | **미검증** | fastapi 0.115(우리) ↔ 0.139(농협) |
| ⑤ | KL 검색(조회) API | **미구현** | 규격 자료 자체를 받지 못했다 |
| ⑥ | ~~규격 미확정 8건~~ → **우리 규격 v1 확정** | **해소 (2026-08-24)** | 정답 샘플을 정본으로 보고 회신을 기다리던 항목들이다. 방침이 바뀌어 **우리가 정하고 농협에 요구**한다 → [J문서](J-우리-규격-정본-v1.md). 남은 것은 "요구 4건 + 미수령 4건" |
| ⑦ | 실제 KL 슬롯 등록 | **미확인** | 폐쇄망 미접속 |
| ⑧ | 에이전트 분리 배포 | 규정문서만 코드 존재, **실행 이력 없음** | 광고 트랙은 분리 모드 미구현 |

**③이 특히 중요하다.** 파싱이 10분 걸리는데 gunicorn이 180초에 일꾼을 죽이면 아무리
202를 잘 돌려줘도 결과가 안 나온다. 이론적으로는 백그라운드 작업이 별도 스레드에서
돌아 일꾼의 심장박동은 계속되므로 괜찮지만, **실측하지 않았으므로 단정할 수 없다.**

### 그래서 지금 시연할 수 있나

| 시연 내용 | 가능? |
|---|---|
| 우리 PC에서 농협 규격대로 왕복(202→폴링→zip) 보여주기 | **가능** — 7건 실측 완료 |
| 규정문서가 `_hrc.jsonl`로 바뀌고 이미지에 캡션이 붙는 것 | **가능** |
| 광고물이 좌표+템플릿 라벨 통합 JSON으로 나오는 것 | **가능** |
| 규격 준수를 검사 코드로 증명 (`spec_check`·`boot_check`) | **가능** |
| **농협 실제 KL에 꽂아서 도는 것** | **불가** — 폐쇄망 미접속, ①~⑦ 미확인 |

---

## 8. 실행 방법

```bash
cd kl_parser

# 1) 시험 환경 만들기 (농협 폐쇄망에서는 불필요 — 전부 이미 있다, §3-7)
python -m venv .venv-entry
.venv-entry/Scripts/python.exe -m pip install -r requirements.txt
.venv-entry/Scripts/python.exe -m pip install -e ..        # 파싱 본체 (로컬 시험용)

# 2) 서버 없이 되는 검사부터 (1초)
.venv-entry/Scripts/python.exe tests/boot_check.py

# 3) 서버 기동 — 농협과 같은 cwd·import 경로로 띄운다
./scripts/run-local.sh                              # http://127.0.0.1:9101

# 4) 왕복 시험 (다른 터미널에서)
./scripts/test-roundtrip.sh                                     # 규정문서
.venv-entry/Scripts/python.exe tests/roundtrip_ad.py <광고파일>   # 광고물
```

`pip install -e ..`가 하는 일: 파싱 본체(`src/nh_parsing/`)를 **복사하지 않고 경로만
연결**한다. 그래서 파이프라인 코드를 고치면 진입점에도 즉시 반영된다(재설치 불필요).
확인하려면 `.venv-entry/Lib/site-packages/_editable_impl_nh_ad_review_poc.pth`를 열어
보면 된다 — `src` 경로 한 줄이 적혀 있다.

> ⚠️ 단 이건 **로컬 in-process 모드** 이야기다. 진짜 배포(에이전트 분리, `AGENT_URL`)로
> 가면 에이전트 서비스를 별도로 재배포해야 한다 → §7-⑧

---

## 9. 이 문서를 쓰면서 새로 찾아낸 것

| 어긋남 | 어디 |
|---|---|
| `requirements.txt`가 "반입 필요 3개"라 적었으나 **전부 농협 이미지에 이미 있다 → 반입 0건** | §3-7 |
| 농협 예제의 `except Exception as e:(` 는 **문법 오류가 아니다** (우리 옛 README의 서술이 틀렸다) | §3-5 |
| 규격서와 예제 코드가 파싱 오류 응답에서 **서로 다르다** (200 vs 500) | §3-2, §6-② |
| 정답 샘플에 규격서에 없는 `break` item과 절대경로 image value가 있다 | §3-8 |
| 농협 `setup-application.sh`의 multipart whl 설치는 현 이미지 기준 불필요한 잔재로 보인다 | §3-7 |
