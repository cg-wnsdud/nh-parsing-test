# D. 농협 자료와 연동 규격 — 뭐고 뭘 맞추라는 건가

> 농협 자료 17개가 각각 뭔지, 농협 시스템(DAP / Knowledge Lake)에서 우리가 꽂히는 자리,
> 요구되는 출력 규격(`_hrc.jsonl`)과 현재 충족도, **규정문서 트랙과 광고물 트랙이 갈리는
> 이유**, 규격서와 실제 예제가 어긋나는 항목과 우리 판단.
>
> **자료 위치**: `C:\Users\cccjj\cginside\농협프로젝트\1.awx_custom_parser_example_api`
> **전부 열어서 확인했다** — 규격서 원문, 정답 샘플, 예제 코드, 패키지 목록.

---

## 0. ⭐ 두 트랙 — 계속 헷갈리는 부분

**한 문장**: 규정문서는 **도서관에 꽂는 책**이고, 광고물은 **그 도서관에 들고 가는 질문지**다.

```
[트랙 A — 규정문서]                     [트랙 B — 광고물]
법령·은행연합회 준수사항·심의사례          심의 대상 광고물
        │                                      │
        │ 우리가 KL 규격으로 변환                │ 우리가 파싱 + 스키마 추출
        ▼                                      ▼
   _hrc.jsonl  →  농협 KL 이 임베딩       extracted/*.json (21키)
        ▼                                      │
   농협 벡터DB (지식 창고)  ◀───── 조회 ──────────┘
                                    "이 광고에 걸리는 조문이 뭐야?"
```

### 왜 방향이 반대인가

| | 트랙 A 규정문서 | 트랙 B 광고물 |
|---|---|---|
| 역할 | **검색당하는 쪽** = 지식 | **검색하는 쪽** = 질문 |
| 우리 스키마(111필드)를 쓰나 | **안 쓴다** | **쓴다** |
| 산출물 | `RagChunk` — 제목 + 텍스트 덩어리 | 필드가 채워진 JSON |
| **좌표가 필요한가** | **아니오** | **예 — 본질** |
| KL 에 넣나 | **넣는다** | **안 넣는다** |
| 최종 저장 | 농협 벡터DB | PostgreSQL (우리 제안) |

### 왜 광고물은 KL 에 안 넣는가 — 규격을 직접 읽어 확인했다

농협 규격([readme.md:166]) 원문:

> *"`item` 유형은 `text`, `table`, `image`, `h1` ~ `h4` 입니다."*

부가정보를 담을 곳은 `cust_meta`(문자열 key-value)뿐이고 **좌표·역할·신뢰도를 담을 전용
필드가 없다.** 우리 광고물 산출물의 영역 한 칸은 이렇게 생겼는데:

```json
{ "region_id": "p1_r000", "bbox": [66, 35, 345, 118],        ← 좌표
  "role": "제목", "role_confidence": 0.9, "role_source": "vlm",
  "lines": [{"text": "금융의모든순간", "bbox": [69,44,308,96],
             "confidence": 0.99996, "source": "ocr"}],
  "vlm_reading": "금융의 모든 순간*", "vlm_reading_score": 1.0 }
```

**KL 규격으로 바꾸면 `value` 에 `"금융의모든순간"` 하나만 남고 나머지를 다 버린다.** 그런데
심의는 *"이 지적의 근거가 원본 어디인가"* 를 화면에 표시해야 하니 좌표가 본질이다.

**그리고 애초에 광고물을 검색 인덱스에 쌓을 이유가 없다.** 검색해야 하는 건 규정이다.
광고물은 **매번 새로 들어오는 질문**이다.

> ⚠️ **아직 확정이 아닌 부분**: 부가정보 칸(`cust_attr1~10`)에 좌표를 문자열로 넣는 방법은
> 물리적으로 가능하다. 지금 "규정문서만 보낸다"로 정리한 건 **좌표 전용 칸이 없어서**이지
> 불가능해서가 아니다. → [E 문서](E-남은-일과-정리-후보.md) 미확정 항목.

### ⚠️ "비교·심의한다"는 목적이 맞다 — 그런데 실행 시점에 KL을 조회하는 구조는 아니다

트랙 A(규정문서)가 KL에 쌓이는 건 **"우리가 나중에 그걸 조회해서 광고와 비교하기 위해서"**가
맞는 목적이다. 다만 **지금 구현은 그 비교를 실행 시점 조회로 하지 않는다** — 코드로 확인:

```
extract.py 가 import 하는 것: applicability / extract_models / field_judge / gemma_client / schema_pack
                             ↑ KL, Qdrant, 벡터DB, HTTP 조회가 전혀 없다
```

**대신 규정을 사람이 먼저 읽고 스키마(`schemas/*.json`)에 미리 다 적어 넣었다.**
`checklist_ref`·`prompt`·`applicability`·`obligation` 이 곧 "규정을 미리 컴파일해 둔 결과"다
([B 문서 §9](B-스키마-구조와-근거.md#9-마무리) 참조). 심의 시점엔 이 스키마만 갖고 VLM을
부르고, KL 이나 다른 벡터DB를 조회하는 코드가 없다.

```
지금 구조:   규정 원문 → (사람이 1회 읽고 씀) → 스키마 JSON → (매 광고마다) VLM 추출 → 결과
             └──────────── 이 화살표가 "조회"가 아니라 "저술"이다 ────────────┘

트랙 A(KL)는:  규정 원문 → _hrc.jsonl → KL 벡터DB   ← 광고 심의 코드가 이걸 안 읽는다
```

**둘 중 뭐가 맞는 아키텍처인지는 아직 결정된 게 아니다.** 지금 방식(정적 스키마)은 vLLM
strict schema 로 결정론적 출력을 뽑기엔 유리하지만, **규정이 바뀌면 스키마를 사람이 다시
써야 한다** — KL 실시간 조회 방식은 그 반대(규정 갱신에 자동으로 따라가지만 검색 변동성이
생긴다). 지금은 전자로 돌아가고 있다는 사실만 코드로 확정한다.

---

## 1. 농협 자료 17개 — 성격이 4갈래다

```
① 규격서 (2)
   readme.md                    17.5KB  ★ 출력 형식의 정본. "Output jsonl" 단락이 핵심
   server/flow/readme/parser_howto.txt   2.8KB  ★ 통신 규약(비동기 API)

② 정답 샘플 (4)
   in_sample.hwp                266KB   입력 예 (인사관리 규정)
   out_sample_hrc.jsonl         21KB    ★ 141줄. 우리가 맞춰야 할 실물
   out_sample_hrc.json          1KB     문서 정보 예 (17페이지 21.0×29.7)
   image/BIN0001.jpg            157KB   추출 이미지 예

③ 동작하는 예제 서버 (8)
   server/flow/app_custom_parser/main.py               11KB  ★ 진입점 (POST/GET)
   server/flow/app_custom_parser/service/parsing_service.py  6.4KB  ★ TODO: 여기에 우리 로직
   server/flow/app_custom_parser/gunicorn_config.py    1.6KB
   server/flow/requirements.txt          25B   python-multipart==0.0.17 한 줄
   server/flow/whl/python_multipart-...whl  24KB  ★ 폐쇄망 반입 방식의 실례
   server/flow/setup-application.sh      142B  pip install --target=... 로 로컬 설치
   server/flow/run-application.sh        2KB
   server/flow/test-application.sh       476B  ★ 왕복 시험 스크립트

④ 부수 (3)
   2.Package.txt                15KB    ★ 농협 베이스 이미지 패키지 285개 목록
   server/flow/readme/parser_readme.txt  252B  서버 구동법 3줄
   server/flow/dummy.txt                 143B  시험용 입력
```

### 가장 중요한 3개

| 파일 | 왜 |
|---|---|
| `readme.md` | 출력 형식의 유일한 정본. 농협 말: *"readme.md 에 적어준 규격만 만족하면 된다"* |
| `out_sample_hrc.jsonl` | **정답 실물.** 규격서와 어긋날 때 이쪽이 진실에 가깝다 |
| `2.Package.txt` | 폐쇄망에 무엇을 반입해야 하는지 결정하는 근거 |

### `out_sample_hrc.jsonl` 실물 (141줄 중 앞 6줄)

```jsonl
{"item": "break", "value": "", "page": 0}                    ← ★ 규격서에 없는 유형!
{"item": "text", "value": "인사관리 규정", "page": 0}
{"item": "image", "value": "/HYB008/datasets/projects/PJT.../BIN0001.jpg",   ← ★ 절대경로
 "type_property": {"title": "", "width": 1102, "height": 557, "ratio": "69%"}, "page": 0}
{"item": "h1", "value": "제1장 총칙"}
{"item": "h2", "value": "제1조(목적)"}
{"item": "text", "value": "이 규정은 000000(이하 \"회사\"라 한다)직원에 대한…", "page": 0}
```

**정답 샘플의 item 분포**: `text 97 · h2 34 · h1 7 · image 1 · table 1 · break 1` = 141줄,
**전부 `page: 0`**

---

## 2. 농협 시스템에서 우리가 꽂히는 자리

```
┌─────────────────── 농협 폐쇄망 ───────────────────────────┐
│                                                          │
│  DAP / GenAI Knowledge Lake (KL)                         │
│    └─ "Parser 관리" 메뉴 → Custom 파서 슬롯   ← ★ 우리 자리 │
│                                                          │
│         POST /parsing  (src_file + option)               │
│            ↓ 202 + {"uuid":"...", "timeout":600}         │
│         GET /parsing/result/{uuid}                       │
│            ↓ zip  (_hrc.jsonl + _hrc.json + _img.zip)    │
│                                                          │
│  KL 이 그 zip 을 받아 → 청킹 → 임베딩 → 벡터DB 적재         │
│                                    (여기부터 우리 소관 아님)│
└──────────────────────────────────────────────────────────┘
```

**등록 경로가 문서에 적혀 있다** (`parser_howto.txt` 마지막):

> portal > 좌측 하단 GenAIs > **GenAI Knowledge Lake** > 관리자서비스 > **KL Admin** >
> **Parser 관리** > 등록 > Parser 유형: **Custom 파서** / 프로그램 유형: **소스코드제공** /
> Custom 유형: **Parsing + 구조화**

즉 **"KL 이 우리 API 를 호출하고, 우리가 zip 을 돌려주면 KL 이 알아서 벡터DB 에 넣는다"** 는
구조다. 우리는 임베딩·벡터DB 를 직접 만지지 않는다.

### 계획한 배치 — 진입점과 본체를 나눈다

```
[진입점 껍데기]  ← KL 컨테이너 안. 얇다. requests 하나만 씀
   POST /parsing → 202 + 번호표      GET /parsing/result/{번호표} → zip
        │ HTTP
        ▼
[에이전트 서비스]  ← 별도 컨테이너. 우리 파이프라인 본체
   파싱 · 스키마 추출 · KL 형식 변환  →  PP-StructureV3, Gemma
```

**왜 나누나 — `2.Package.txt` 285개를 직접 조회해 확인한 결과:**

| 우리가 쓰는 것 | 농협 이미지에 있나 |
|---|---|
| `requests` | ✅ **2.34.2** |
| `pillow` | ✅ **12.3.0** |
| `pydantic` | ✅ **2.13.4** |
| `numpy` | ✅ **2.4.6** |
| `fastapi` | ✅ **0.139.2** |
| `uvicorn` | ✅ **0.51.0** |
| `python-multipart` | ✅ **0.0.32** |
| `gunicorn` / `starlette` | ✅ 있음 |
| **`pypdfium2`** (PDF 렌더) | ❌ **없음** |
| **`jpype1`** (사내 HWP 파서용 JVM 브리지) | ❌ **없음** |

**진입점은 `requests` 하나로 끝나지 않는다.** KL이 거는 HTTP 요청을 "받는" 서버가 되려면
`fastapi`+`uvicorn` 이 반드시 있어야 한다(`requests`는 요청을 "거는" 클라이언트 라이브러리라
서버 역할을 못 한다). 다행히 이것도 베이스 이미지에 **이미 있어 반입은 필요 없다** — 그래서
"반입 0건"이라는 결론 자체는 맞다. (`kl_parser/requirements.txt` 가 이 3개를 "반입 필요"로
적어 둔 건 이 표와 어긋난다 — §7 참조)

같은 컨테이너에서 다 돌리면 pypdfium2 + JVM + 사내 파서를 전부 반입해야 하고, **사내 HWP
파서는 Python 3.13 + 자바를 요구하는데 농협 환경은 3.11 기준**이다. 나누면 진입점만 3.11 을
지키면 되고 본체는 자유다.

> **문서 정정**: `농협연동_전체정리 §9-1` 이 반입 필요 목록에 *"pypdfium2 + 사내파서 + JVM
> + Pillow… 전부"* 라고 적었는데 **Pillow 는 이미 있다**(12.3.0). 반입이 필요한 건
> pypdfium2·jpype1·사내파서다.

---

## 3. 요구 출력 규격과 현재 충족도

### 규격서가 요구하는 것 (readme.md:14~24)

```
POST 방식 · 파라미터명 src_file (멀티파트) · option 은 base64
출력 3종:  <원본파일명>_hrc.jsonl   ← TEXT (필수)
          <원본파일명>_hrc.json    ← INFO
          <원본파일명>_img.zip     ← 이미지 전체
이 3개를 zip 으로 묶어 리턴
```

**파일명은 확장자를 떼지 않는다** — `sample.hwp` → `sample.hwp_hrc.jsonl`.
우리 출력이 실제로 그렇다:

```
prototype/kl_out/
  (2023년)예금성상품 광고시 준수사항_은행연합회.hwp_hrc.jsonl   47KB
  (2023년)예금성상품 광고시 준수사항_은행연합회.hwp_hrc.json   330B
  (2023년)예금성상품 광고시 준수사항_은행연합회.hwp_img.zip    789KB
  (2025년)대출성 상품 광고시 준수사항_은행연합회.pdf_hrc.*     3종
```

### 우리 출력 실물 대조 (직접 세었다)

```
농협 정답 샘플 (sample.hwp)     141줄  {break 1, text 97, image 1, h1 7, h2 34, table 1}
우리 (예금성 hwp)                82줄  {table 17, text 36, h2 8, h3 11, h4 1, image 9}
우리 (대출성 pdf)                65줄  {text 31, h1 4, table 4, h4 7, h2 7, h3 9, image 3}
```

### 항목별 충족도

| 농협 요구 (근거) | 우리 구현 | 상태 |
|---|---|---|
| 파일명 `_hrc.jsonl`/`_hrc.json`/`_img.zip`, 확장자 유지 | `kl_export.export_kl_files()` | ✅ 테스트로 못박음 |
| item 7종만 (`text`/`table`/`image`/`h1~h4`) | 그 7종만, 규격 밖 0건 | ✅ |
| `value` 필수 | 빈 값 0건 | ✅ |
| `h1~h4` 는 이후 본문의 meta | 청크 앞에 삽입 + 중복 제거 | ✅ |
| `page` 모르면 0 | 전부 0 (정답 샘플도 141줄 전부 0) | ✅ |
| `cust_meta` key-value | attr/sattr 성질대로 분리 | ✅ |
| `table` 은 `value` 가 HTML `<table>` | 표 21개 전부 HTML | ✅ (2026-08-19 수정) |
| `image` `type_property` 4키 필수 | 4키 전부 생성 | 🟡 **값이 `0`/`""`** — 4절 ③ |
| **비동기 API** (`parser_howto.txt`) | **미구현** | ⬜ 다음 단계 |
| 반입 패키지 목록 | 미작성 | ⬜ |
| Python 3.11 호환 검증 | 미검증 | ⬜ |

**"출력 형식"은 다 맞췄고, "통신 껍데기"와 "실행 환경"이 남았다.**

### 왜 농협 기본 파서가 아니라 우리 파서인가 — 실측 근거 2건

**이게 우리가 이 자리에 있는 이유다.**

#### ① 규정의 실질 기준이 이미지 안에 있다

`(2023년)예금성상품 광고시 준수사항` HWP 에 **금리 표기 사례 스크린샷 10장**이 있다.
*"최고금리를 이렇게 쓰면 안 된다"* 는 판단 기준이 **그림 안에** 있다. 일반 파서는 그림 속
글자를 못 읽으니 이 지식이 인덱스에 안 들어간다. 우리는 VLM 캡션을 만들어 텍스트로 넣는다
(실측 9개, 1개는 장식 이미지로 크기 필터에 걸려 제외):

```
[이미지 사례 img10] 특정 금리(연 4.00%)를 강조하여 보여주는 사례로, 연보라색
하이라이트를 통해 금리 정보를 시각적으로 부각하고 있습니다.
이미지 내 텍스트: # 님의 만기예상금액 그대로 가입하기 / 1,000,000원 을 /
1년 동안 보관하면 / 1,040,109원을 받아요 / 연 4.00% 예상이자 40,109원(세전)
```

#### ② 사내 파서가 스캔 페이지를 조용히 건너뛴다

`(2025년)대출성 상품 광고시 준수사항` PDF 는 **6쪽 중 3쪽이 스캔형**이다. 사내 파서는 OCR 이
없어 그 페이지를 **아무 경고 없이 건너뛴다**(`parse_status == "skipped"`). 2026-08-12 측정
당시 **규정 원문 한 쪽이 RAG 인덱스에 통째로 없었다.**

**여기서 한 번 잘못 고쳤다가 다시 고친 기록이 남아 있다** — 규율 6의 실례다:

```
최초 구현(08-18):  그 페이지를 통째로 렌더해서 OCR
   → 문제: 'skipped' 는 "문단 추출 실패"이지 "텍스트가 없다"가 아니었다
   → pypdfium2 로 직접 읽으면 캡션 4줄(69자)이 정확한 띄어쓰기로 나온다
   → 렌더+OCR 하면 '[1] 최저·최고금리 병기' → '[1]최저·최고금리 병기' 로 띄어쓰기가 깨진다

수정(08-19):  ① 디지털 텍스트를 그대로 text 청크로 먼저 낸다 (정확)
             ② 렌더+OCR 은 계속하되, 디지털과 일치하는 줄은 버리고 남는 줄만 ocr_page 로
   실행 로그:  3쪽 디지털 텍스트 복원 (4줄, 69자)
              3쪽 OCR 복원 완료 (31줄, 246자)   ← 수정 전 34줄에서 중복 4줄 제거
```

> **`kordoc`(Node.js)는 쓰지 않았다.** 예전 인수인계가 *"이미 HWP 폴백으로 저장소에 있다"* 고
> 적었지만 **구현된 적이 없다**(코드 0줄, 문서 언급뿐). 도입하면 폐쇄망에 Node 런타임을 추가
> 반입해야 한다. `pypdfium2` 는 이미 우리 의존성이라 **새 반입 없이** 해결됐다.

---

## 4. 규격서와 실제 예제가 어긋나는 항목 — 6건 + 우리 판단

억지로 맞추지 않고 근거를 남겼다.
[kl_export.py 상단 주석](../../src/nh_parsing/kl_export.py#L9-L13)에 같은 내용이 있다.

### ① `cust_meta` — 규격서 안에서 3번 다르게 적혀 있다

원문을 직접 대조했다:

```
readme.md:174  "조회를 위한 정보(cust_arrt1 ~ cust_arrt5)와 검색/조회를 위한
                정보(cust_sattr1 ~ cust_sattr5)"          ← 이름 오타 arrt, 개수 5
readme.md:175  "정의 방법은 key-value 형식 "cust_mata": [...]"  ← 이름 오타 mata
readme.md:215~229  표: cust_attr1 ~ cust_attr10 (10개) + cust_sattr1~5   ← 개수 10
readme.md:257  실제 jsonl 예시: "cust_meta": [{"name": "cust_attr1", ...}]  ← 정상
readme.md:300  "meta 정보와 cust_met 정보는..."                ← 또 다른 오타
```

**우리 판단**: 실제 예시(`cust_meta` + `cust_attr1`)를 정본으로 쓰되 **개수는 적은 쪽(5)에
맞춘다.** 6~10 이 실제로 저장되는지 확인되지 않았고, 규정문서에 필요한 칸은 5개로 충분하다.
→ 현장 확인 항목.

### ② `attr` vs `sattr` — 성질이 다르다 (어긋남이 아니라 설계 근거)

규격서 표 원문:

| 키 | 규격서 설명 |
|---|---|
| `cust_attr1~10` | **(조회only)** |
| `cust_sattr1~5` | **(검색,조회)** … **임베딩시 포함** |

**→ `sattr` 에 넣은 값은 벡터에 섞인다.** 좌표나 ID 를 여기 넣으면 검색 품질이 떨어진다.
그래서:

| 칸 | 값 | 왜 |
|---|---|---|
| `cust_attr1` | 청크 종류 (`text`/`table`/`image_caption`/`ocr_page`/`image_asset`) | "이게 이미지에서 뽑은 것인지 원문인지" |
| `cust_attr2` | `chunk_id` | 우리 산출물과 대조 (감사 추적) |
| `cust_attr3` | `asset_id` | 캡션의 원본 이미지 추적 |
| `cust_attr4` | `digital` \| **`ocr`** | **정본 신뢰도.** `ocr` 은 판독이라 틀릴 수 있다 |
| `cust_sattr1` | 출처 문서명 | 검색어 |
| `cust_sattr2` | 상품군 | 검색 필터 (**파일명으로만** 결정, 본문 판단 안 함) |
| `cust_sattr3` | 조항 제목 | 검색어이자 출처 표시 |
| `cust_sattr4~5` | **비워 둠** | **연결 키(`checklist_ref`) 자리로 예약** |

### ③ `image` 크기 단위 — 규격서 "mm", 샘플은 정체불명의 단위

```
readme.md:171  "이미지 크기를 width와 height 항목에 mm 기준으로 설정합니다.
                페이지 크기 대비 이미지 크기의 비율을 ratio에 설정합니다."
out_sample     "width": 1102, "height": 557, "ratio": "69%"
                        ↑ A4 폭이 210mm 인데 1102 → mm 는 아니다
```

**단순히 "픽셀이다"도 아니다.** 실제 원본 이미지(`image/BIN0001.jpg`)를 열어 직접 재 보면
**1200×607px** 다. 샘플 값(1102×557)은 실제 픽셀의 **91.8%로 균일 축소**된 값이라 어느
쪽으로도 딱 떨어지지 않는다 — mm도, 원본 픽셀도 아닌 제3의 값이다. 원인 불명.

**우리 판단**: `width=0, height=0, ratio=""` 로 두고 **현장 확인 항목으로 올린다.** 이유는
`ratio` — *"페이지 크기 대비"* 인데 우리 RagChunk 에는 페이지 정보가 없다.
**모르는 값을 지어내지 않는다.**

### ④ `item: "break"` — 규격서에 없는 유형이 정답 샘플에 있다

```
readme.md:166   "item 유형은 text, table, image, h1 ~ h4 입니다"
out_sample 1줄  {"item": "break", "value": "", "page": 0}     ← 문서화 안 됨
```

**우리 판단**: 안 쓴다. 규격서에 없는 유형을 흉내내면 KL 이 어떻게 처리할지 모른다.

### ⑤ `image` `value` — 규격서는 파일명, 샘플은 절대경로

```
readme.md:168   "image는 이미지 파일명입니다"
out_sample      "value": "/HYB008/datasets/projects/PJT20240054/DTS.../BIN0001.jpg"
```

**우리 판단**: 파일명으로 낸다(`img10.png`). 농협 내부 경로는 우리가 흉내낼 수 없고,
흉내내면 **존재하지 않는 경로를 신고하는 것**이 된다.

### ⑥ 통신 규약이 두 문서에서 다르다

| | `readme.md` | `parser_howto.txt` |
|---|---|---|
| 방식 | **동기** — POST 하면 바로 zip | **비동기** — 202 + uuid + 폴링 |
| 경로 | `POST /parse/` | `POST /parsing` |
| 정상 코드 | (명시 없음, 200 암시) | **202** |

`readme.md:5-8` 이 그 이유를 설명한다: *"API연계는 동기 방식과 비동기 방식이 가능하며 **파싱
시간이 오래 걸리는 경우는 비동기 방식 사용을 권장**"*. 그리고 **동작하는 예제 서버(`main.py`)는
비동기 쪽**을 구현했다.

**우리 선택은 비동기다** — 우리 문서 최대 처리 시간이 590초라 동기로는 타임아웃된다.

### `page` 는 전부 0

규격서가 *"페이지 번호를 알 수 없는 경우에는 0"* 을 허용하고, 농협 정답 샘플도 **141줄 전부
page=0** 이었다.

**규격 준수는 테스트로 못박아 뒀다** — `tests/test_kl_export.py` **15개** +
스캔 페이지 복원 `tests/test_rag_scan_page.py` 8개.
(⚠️ 문서는 14와 16으로 엇갈리게 적고 있다 — 실제 15개)

### ⑦ 우리 코드가 지금 규격을 위반하는 것 2건 (새로 발견, 2026-08-23)

`server/flow/readme/parser_howto.txt` — 통신 계약의 정본인데 이 문서(D)가 지금까지
`readme.md`만 근거로 삼고 이 파일을 충분히 안 봤다. 다시 대조한 결과:

**A. 파싱 실패 응답 코드가 규격 위반이다.** `parser_howto.txt` 는 오류를 두 종류로 나눈다:

> · 파싱 오류가 발생하면 status **200**, `{"status":"ERROR", "message": "..."}`
> · 그 외 오류가 발생하면 status **500**, `{"message": "..."}`

우리 [kl_parser/app/main.py:127](../../kl_parser/app/main.py#L127)은 파싱 실패(`ERROR` 상태)를
**500** 으로 보낸다 — "그 외 오류" 취급이다. KL이 이걸 서버 장애로 오해할 수 있다.
(농협 예제도 같은 실수를 한다 — `main.py:194-196`. 우리는 예제를 따라가다 이 실수까지
같이 옮겼다.)

**B. `run-application.sh` 가 요구하는 폴더 구조와 우리 것이 다르다.** 실제 기동 스크립트:

```bash
export FLOW_APP_NAME="app_custom_parser"        # ← DO NOT EDIT
cd $PATH_SOURCE/$FLOW_APP_NAME
gunicorn main:app --config gunicorn_config.py
```

`gunicorn main:app` 으로 뜨려면 `main.py` 가 있는 폴더 안에서 실행되고, 그 안의 import 는
예제처럼 평평해야 한다(`from service.parsing_service import ...`). 그런데 우리
`kl_parser/app/main.py` 는 **상대 import**(`from .service.parsing_service import ...`)를
쓴다 — `gunicorn main:app` 으로 그대로 띄우면 `attempted relative import with no known
parent package` 로 죽는다. **지금 구조로는 DAP 기동 자체가 안 될 가능성이 높다.**
`FLOW_APP_NAME` 이 DO NOT EDIT 이라 폴더명(`app_custom_parser`)도 고정일 가능성이 있다 —
현장 확인 항목.

---

## 5. 농협 예제 코드의 결함 — 6건 (코드로 검증)

인수인계 문서가 2건을 주장했는데 둘 다 사실이고 **4건을 더 찾았다.**

### ① `TIMEOUT` 타입 버그 ✅ 확인

```python
# main.py:19
TIMEOUT = os.getenv("TIMEOUT", 600)          # 환경변수 설정하면 str 이 된다
...
# main.py:228
timeout2 = timeout + 10                      # str + int → TypeError
```

**우리 문서 최대가 590초라 타임아웃을 늘리려고 환경변수를 설정하는 순간 죽는다.**
기본값 600초에서는 마진이 10초뿐이다.

### ② `except` 블록이 미할당 변수를 참조 ✅ 확인

```python
# main.py:32
    try:
        ...
# main.py:75
        work_dir = os.path.join(PATH_WORK, _uuid)     ← 여기서 처음 할당
        ...
# main.py:109
    except Exception as e:
        ...
# main.py:114
        if os.path.exists(work_dir):                  ← 75줄 전에 예외 나면 UnboundLocalError
```

예외가 69~72줄에서 나면 **except 블록 안에서 또 예외**가 나서, 구조화된 500 응답 대신
프레임워크 기본 오류가 나간다.

### ③ `img_zip_file` 이 특정 분기 안에서만 할당된다 (새로 찾음)

```python
# main.py:145-147
if str(item).endswith("_hrc.jsonl"):
    s2_text_file = ...
    img_zip_file = s2_text_file.replace("_hrc.jsonl", "_img.zip")   ← 여기서만 할당
...
# main.py:160
with zipfile.ZipFile(img_zip_file, ...) as img_zip:                 ← jsonl 없으면 NameError
```

### ④ `os.listdir(img_dir)` 무조건 호출 (새로 찾음)

```python
# main.py:158
items = os.listdir(img_dir)     ← 이미지 디렉터리가 없으면 예외
```

### ⑤ `option` 을 아예 안 쓴다 (새로 찾음)

```python
# main.py:31
async def parsing_text_docx_to_json(..., option: str = None):    ← 선택 파라미터
# main.py:37~39  (전부 주석)
        # option_dec = base64.decodebytes(option)
        # options = json.loads(option_dec.decode("utf-8"))
# main.py:64
        options = {}                                             ← 빈 dict 를 넘긴다
```

규격서(readme.md:18)는 *"option 파라미터는 base64 인코딩된 형태로 넘어오며 디코딩 후
`parser_info` 와 `doc_data` 값을 활용할 수 있습니다"* 라고 하는데 **예제는 디코딩을 안 한다.**
즉 `doc_data`(문서 제목·카테고리)와 `parser_info`(OCR 제공자·API 키)가 배선되지 않은 상태다.

**우리가 `option` 을 써야 하나?** 지금 설계에서는 아니다 — OCR/VLM 을 우리 서비스가 직접
부르므로 `parser_info.prop` 의 OCR 정보가 필요 없다. 다만 `doc_data.title`·`category*` 를
`cust_sattr` 에 실으면 검색 품질이 올라갈 수 있어 **현장 확인 항목**이다.

### ⑥ `parsing_service.py` 는 `_hrc.json` 을 만들지 않는다 (새로 찾음)

```python
# parsing_service.py:46~51
result_file_path = os.path.join(work_dir, base_filename + "_hrc.jsonl")
with open(result_file_path, "w", encoding="utf-8") as f:
    f.write(file_name_str)
    f.write(sample_jsonl)
# → _hrc.json 을 쓰는 코드가 없다
```

readme.md:22 는 INFO 파일을 요구한다. 예제를 그대로 돌리면 zip 에 jsonl 만 들어간다.
**우리 `kl_export.py` 는 3종을 다 만든다** — 이건 이미 맞춘 부분이다.

> **성격 정리**: 이 예제는 **완성품이 아니라 골격**이다. `parsing_service.py:39` 에
> `# TODO: M A I N: 파싱` 이라고 적혀 있고, 하드코딩된 쿠버네티스 샘플 텍스트
> (`"(이것은 하드코딩된 응답입니다)"`)가 들어 있다. 농협 말대로 *"일단 만들어서 부딪혀 보고
> 조정하자"* 인 단계다.

---

## 6. 시간·환경 규격 — 여유가 있는가

| 제약 | 값 | 우리 최대 590초와 |
|---|---|---|
| KL 폴링 기본 timeout | **3시간** (10,800초) | **18배 여유** |
| 농협 예제 subprocess `TIMEOUT` 기본 | 600초 | 마진 10초 → **조정 필요** |
| gunicorn `timeout` | 180초 | **무관** (비동기라 진입점은 202 를 즉시 반환) |

**반입 방식의 실례가 자료에 들어 있다** (`setup-application.sh`):

```bash
pip install --target=/project/work/flow/custom_libs \
            /project/work/flow/whl/python_multipart-0.0.17-py3-none-any.whl
```

**whl 파일을 같이 넣고 `--target` 으로 로컬 설치**하는 방식이다. 인터넷이 없으니 이게 유일한
경로다. 우리 진입점이 `requests` 하나만 쓰면 **반입할 게 없다**(이미 2.34.2 있음).

**반입 메커니즘 2경로**: ① whl `--target` + PYTHONPATH (위 실측) ② 농협 베이스 이미지 재빌드
(우리 메일 항목).

---

## 7. 이번 확인에서 새로 찾은 것 2건

### ① 죽은 문서 참조 — 정리 대상

[kl_export.py:13](../../src/nh_parsing/kl_export.py#L13) 주석:

> *"현장에서 확인할 목록은 `docs/농협KL_미확정대장_2026-08-19.md`."*

**그 파일이 존재하지 않는다.** 현장 확인 항목이 코드 주석 곳곳에 흩어져 있는데 모아 놓은
대장이 없다. → [E 문서](E-남은-일과-정리-후보.md) 백로그(농협 방문 준비물).

### ② `h1` 생성이 파일 형식에 따라 비대칭 — 실측 결함

같은 조항 제목이 HWP 냐 PDF 냐에 따라 다르게 나간다. RAG 청크와 KL 출력을 대조했다:

```
[PDF — (2025년)대출성]
  chunk c001: kind=text, heading='Ⅰ. 목적'   → h1 item 생성 ✅
  → h1 4개 · h2 7 · h3 9 · h4 7

[HWP — (2023년)예금성]
  chunk c002: kind=table, heading=None,
              table_html='<table cols=1 rows=1><tr><td>Ⅰ. 목적</td></tr></table>'
  → table item 으로 나감. h1 0개 ❌
  → h2 8 · h3 11 · h4 1  (h1 없음)
```

**원인**: HWP 규정문서는 `Ⅰ. 목적`·`Ⅱ. 적용범위` 같은 대제목이 **1×1 표 안에** 들어 있다.
`rag_ingest` 가 이걸 `kind=table` 로 잡고 `heading` 을 비워 두니,
`kl_export` 의 제목 판별 정규식([kl_export.py:50](../../src/nh_parsing/kl_export.py#L50)
`[ⅠⅡⅢ…]` → `h1`)에 도달하지 못한다.

**영향**: KL 은 `h1~h4` 를 *"이후 본문의 meta"* 로 쓴다(readme.md:300). h1 이 없으면 그 문서의
본문 청크들이 **최상위 문맥을 잃는다** — 검색 결과에 *"어느 장(章)의 내용인지"* 가 안 붙는다.

**코드가 이미 감지하고는 있다** ([kl_export.py:211](../../src/nh_parsing/kl_export.py#L211)):

```python
if not any(i["item"].startswith("h") for i in items):
    notes.append("h1~h4 item 이 0개다 — 이 문서의 청크에 heading 이 붙지 않았다.")
```

하지만 이 검사는 **h 가 하나도 없을 때만** 발동한다. 예금성은 h2~h4 가 있으니 통과한다 —
**"h1 만 없는" 경우를 못 잡는다.**

---

## D 요약

```
트랙 A 규정문서 → _hrc.jsonl 3종 → 농협 KL → 벡터DB     (검색당하는 쪽)
트랙 B 광고물   → extracted/*.json (21키) → PostgreSQL  (검색하는 쪽)
   갈리는 기준: 좌표가 필요한가. KL 규격에 좌표 칸이 없다.

농협 자료 17개 = 규격서 2 + 정답샘플 4 + 예제서버 8 + 부수 3
   정본: readme.md "Output jsonl" 단락 + out_sample_hrc.jsonl(141줄)

우리가 꽂히는 자리: KL Admin > Parser 관리 > Custom 파서 (소스코드제공 / Parsing+구조화)
   POST /parsing → 202 + uuid → GET /parsing/result/{uuid} → zip

충족도:  출력 형식 ✅ (7종 item · 파일명 · cust_meta · table HTML)
        통신 껍데기 ⬜ · 실행환경 검증 ⬜ · 반입 목록 ⬜

규격 어긋남 6건 — 전부 "모르는 것을 지어내지 않고 근거를 남긴다"로 처리
   cust_meta 개수 5↔10 / attr·sattr 성질 / 크기단위 정체불명 /
   item "break" 미문서화 / image value 파일명↔절대경로 / 동기↔비동기

우리 코드가 규격을 위반하는 것 2건 (2026-08-23 발견, 시연 전 수정 필요)
   파싱 실패 응답을 500으로 보냄 (규격은 200+ERROR) /
   gunicorn main:app 기동과 우리 상대 import 구조가 안 맞음

농협 예제 결함 6건 (우리가 고쳐 쓸 것)
   TIMEOUT 타입 · except 미할당 변수 · img_zip_file 미할당 ·
   os.listdir 무조건 호출 · option 미디코딩 · _hrc.json 미생성

새로 찾은 것 2건
   죽은 문서 참조 (농협KL_미확정대장 없음)
   h1 생성 비대칭 (HWP 대제목이 1×1 표에 갇혀 h1 0개)
```

**D 에서 꼭 잡고 갈 세 가지**

1. **두 트랙은 "좌표가 필요한가"로 갈린다** — 규정문서는 검색당할 지식(좌표 불필요),
   광고물은 검색할 질문(좌표가 본질). KL 규격에 좌표 칸이 없어서 광고물은 KL 에 안 넣는다.
2. **규격서보다 정답 샘플이 진실에 가깝다** — 하지만 샘플도 농협 내부 경로처럼 흉내낼 수
   없는 게 있다. 어긋나면 **모르는 값을 지어내지 않고 현장 확인 항목으로 올린다.**
3. **"출력 형식"과 "통신 껍데기"는 별개 작업이다** — 형식은 다 맞췄고, 남은 건 진입점 API +
   실행환경(Python 3.11 · 반입 목록).
