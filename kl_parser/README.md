# NH Knowledge Lake Custom Parser

`kl_parser`는 농협 Knowledge Lake(KL)가 호출하는 **Custom Parser API**다.

농협은 파일 업로드·상태 조회·결과 ZIP의 통신 규격을 정하고, 이 폴더는
`nh_parsing`으로 문서를 읽고 구조화하는 방식을 구현한다. 즉 이 폴더는 파싱 엔진
자체가 아니라 **농협 KL과 우리 파싱 엔진을 연결하는 어댑터**다.

## 먼저 알아둘 구조

```text
농협 KL
  │  POST /parsing 또는 /ad/parsing
  ▼
kl_parser (이 폴더)
  │  Python 직접 호출
  ▼
nh_parsing (프로젝트 루트의 src/nh_parsing)
  ├─ HTTP → PaddleX/OCR 서비스
  └─ HTTP → VLM 서비스
```

 `nh_parsing`은 `PADDLEX_URL`, `GEMMA_URL` 환경변수에
설정한 GPU 서비스로 HTTP 요청을 보낸다. 로컬에서는 DGX-Spark 주소를, 농협 내부
배포에서는 농협 내부 GPU 서비스 주소를 설정하는 그림을 전제로 한다.

## 이 프로젝트의 두 입력 트랙

| 트랙 | 요청 | 입력 | 결과 | 주 용도 |
|---|---|---|---|---|
| 규정문서 | `POST /parsing` | HWP/HWPX/PDF 등 기준 문서 | `_hrc.jsonl`, `_hrc.json`, 선택적 `_img.zip` | KL 색인·검색용 지식 생성 |
| 광고물 | `POST /ad/parsing` | 광고 PDF/PNG/JPG | `_parsed.json`, `_review_input.json`, `ad_summary.json`, 선택적 `_img.zip` | 근거 보존 + 다음 심의 단계 인계 |

규정문서와 광고물은 역할이 다르다.

```text
규정문서 → “무엇을 기준으로 판단할 것인가”를 KL에 넣는다.
광고물   → “광고에 실제 무엇이 보였는가”를 구조화한다.
```

이 파서는 최종 심의 위반을 확정하거나 KL 검색 API를 구현하지 않는다. 그 판단과 검색
연동은 이 API 밖의 다음 단계다.

## 농협이 보는 비동기 API 흐름

문서 파싱은 오래 걸릴 수 있으므로, 업로드 요청은 즉시 접수만 하고 결과는 나중에 받는다.

```text
1. KL → POST /parsing (src_file, option)
2. kl_parser → 202 {result: "OK", body: {uuid, timeout}}
3. kl_parser → 백그라운드에서 파싱 수행
4. KL → GET /parsing/result/{uuid} 반복 호출
5. 결과
   - 200 {"status":"PARSING"}              아직 처리 중
   - 200 {"status":"ERROR", "message":…} 파싱 실패
   - 200 ZIP                                  처리 완료
```

`uuid`마다 `PATH_TEMP/<uuid>/` 작업 폴더가 만들어진다. 그 안의 `genaikl.status` 파일이
`PARSING`, `DONE`, `ERROR` 상태를 보관한다.

## API 명세

엔드포인트는 4개다. **결과 조회는 두 트랙이 같은 경로를 쓴다** — `/ad/parsing`으로 넣은
광고도 `/parsing/result/{uuid}`로 받는다. 광고 전용 조회 경로는 없다.

### `POST /parsing` · `POST /ad/parsing`

요청은 `multipart/form-data`다.

| 항목 | 필수 | 설명 |
|---|---|---|
| `src_file` | 필수 | 파싱할 원본 파일 |
| `option` | 선택 | base64로 인코딩한 JSON. 해석에 실패하면 경고만 찍고 무시한 채 계속한다 |

응답:

```text
202  {"result": "OK", "body": {"uuid": "<32자 hex>", "timeout": 1800}}
500  {"message": "Exception : <예외이름> <메시지>"}
```

`202`는 “파싱을 끝냈다”가 아니라 **“접수했다”**는 뜻이다. `timeout`은 KL에게 “이만큼은
기다려 달라”고 알려 주는 값이며 `TIMEOUT` 환경변수에서 온다.

`500`은 접수 자체가 실패한 경우다(파일 저장 불가 등). 이때 만들던 작업 폴더는 지운다.

### `GET /parsing/result/{uuid}`

작업 상태에 따라 응답이 네 가지로 갈린다.

| 작업 폴더 / 상태 | 응답 | 본문 |
|---|---|---|
| 폴더 없음 | `500` | `{"message": "Requested url does not exist."}` |
| `PARSING` | `200` | `{"status": "PARSING"}` |
| `ERROR` | `200` | `{"status": "ERROR", "message": "<예외이름>: <메시지>"}` |
| `DONE` | `200` | ZIP 바이너리 |

**파싱 실패가 `500`이 아니라 `200` + `ERROR`인 것은 농협 규격이다**(`parser_howto.txt`).
`500`은 “API 호출 자체가 잘못됐다”는 뜻으로만 쓴다. 농협 예제 코드는 파싱 실패에도
`500`을 주는데, 규격서와 예제가 어긋나는 부분이라 규격서를 따랐다.

성공 ZIP은 `Content-Disposition: attachment; filename=kl-core-s2-output.zip`으로 나간다.
ZIP을 만든 뒤 작업 폴더는 지운다(`KEEP_WORK_DIR=1`이면 남긴다).

### `GET /health`

```text
200  {"status": "OK"}
```

파싱도 GPU 호출도 하지 않고 API 프로세스가 살아 있는지만 본다.

## 엔드포인트별 내부 흐름

### 접수 (`POST` 규정문서, 심의 광고물 경로 공통)

```text
① uuid 발급 → PATH_TEMP/<uuid>/ 와 그 안의 image/ 생성
② 업로드 파일을 PATH_TEMP/<uuid>/<원본파일명> 으로 저장
③ genaikl.status 에 PARSING 기록          ← 응답보다 먼저 쓴다
④ 백그라운드 작업 등록 (parse 또는 parse_ad)
⑤ 202 응답 반환
```

③을 응답보다 먼저 하는 이유가 있다. KL이 `202`를 받은 직후 폴링하면 상태 파일이 아직
없어 `UNKNOWN`이 나가는데, 그러면 “접수는 됐다는데 작업이 없다”처럼 보인다.

### 규정문서 파싱 (`parse`)

```text
nh_parsing.rag_ingest.ingest_rag_file()     문서 → 텍스트 청크 + 추출 이미지
        ↓
nh_parsing.kl_export.export_kl_files()      KL 규격 파일로 변환
        ↓
작업 폴더에 남는 것
├─ <원본파일명>_hrc.jsonl     KL이 읽는 구조화 본문
├─ <원본파일명>_hrc.json      문서 정보
├─ image/…                    추출 이미지
├─ kl_parser_notes.json       우리 검수용 (ZIP에 넣지 않는다)
└─ genaikl.status             DONE
```

`_hrc.json`의 `source`는 임시 작업 경로가 아니라 원본 출처로 바꿔 준다. `option`에
`doc_data.origin_url` 또는 `doc_data.origin_doc_id`가 있으면 그 값을, 없으면 원본
파일명을 쓴다.

### 광고물 파싱 (`parse_ad`)

```text
nh_parsing.ad_export.process_ad_file_outputs()  파싱 + 템플릿 판정 + 라벨링을 한 번에
        ↓
작업 폴더에 남는 것
├─ <원본파일명>_parsed.json   P1/evidence-v6: 좌표·파서 기본 텍스트·VLM/Judge·카드·카드별 템플릿·라벨 근거 원본
├─ <원본파일명>_review_input.json  P2/ad-review-input-v5: 라벨별 심의 문구 + 미배정 광고문구 + P1 줄 참조
├─ ad_summary.json            분류·템플릿·완결성만 뽑은 짧은 요약
├─ image/<이름>_p1.jpg …      쪽 이미지 (박스를 그려 넣지 않은 원본)
└─ genaikl.status             DONE
```

박스를 이미지에 그리지 않는다. 좌표는 이미 `_parsed.json`에 있으므로, 어떻게 그릴지는
받는 쪽이 정하게 둔다. `_review_input.json`의 `labelled_ad_copy[]`와
`unmapped_ad_copy[]`는 파서 기본 줄을 빠짐없이 나누며, 각 review view는 `_parsed.json`의
`line_refs`로 좌표 근거를 가리킨다. P2는 Judge가 고른 VLM 문구를 영역 전체가 하나의 view일
때만 시험적으로 사용한다. 표는 PaddleX 셀 격자 대신 StructureV3 영역 + 표 전용 VLM 관측으로
P1에 남긴다.

### 결과 ZIP 구성

작업 폴더의 모든 파일을 담지 않는다. 호출자가 받아야 할 것만 골라 담는다.

```text
규정문서 ZIP
├─ <원본파일명>_hrc.jsonl
├─ <원본파일명>_hrc.json
└─ <원본파일명>_img.zip     image/ 에 파일이 있을 때만

광고물 ZIP
├─ <원본파일명>_parsed.json
├─ <원본파일명>_review_input.json
├─ ad_summary.json
└─ <원본파일명>_img.zip     image/ 에 파일이 있을 때만
```

`genaikl.status`와 `kl_parser_notes.json`은 내부 파일이라 넣지 않는다.

## 파일별 역할

```text
app_custom_parser/
├─ main.py
│  농협 API 접수, UUID 생성, 상태 조회, ZIP 반환을 담당한다.
│
└─ service/
   ├─ status.py
   │  농협 예제와 호환되는 genaikl.status 읽기/쓰기를 담당한다.
   │
   └─ parsing_service.py
      요청을 nh_parsing 파이프라인 호출로 연결하고 결과 파일을 작업 폴더에 쓴다.

tests/
├─ boot_check.py      농협 방식의 import·오류 응답 규격 검사
├─ spec_check.py      규정문서 JSONL 형식 검사
├─ roundtrip.py       규정문서 HTTP 왕복 검사
└─ roundtrip_ad.py    광고물 HTTP 왕복 검사

scripts/
├─ run-local.sh       농협 배포와 같은 import 경로로 로컬 서버 기동
└─ test-roundtrip.sh  규정문서 왕복 시험 실행
```

농협 배포 스크립트는 `cd app_custom_parser && gunicorn main:app` 형태로 기동한다.
그래서 `main.py`와 `service/`의 평평한 폴더 구조, 그리고 `from service...` 절대 import를
유지해야 한다. `boot_check.py`가 이 두 가지를 검사한다.

## 로컬에서 실행하고 결과 확인하기

로컬 실행에는 두 환경이 모두 필요하다.

1. 이 폴더의 API 의존성: FastAPI, Uvicorn, multipart
2. 프로젝트 루트의 `nh_parsing` 패키지와 그 의존성: HWP/PDF 처리 라이브러리, 스키마 등

```bash
# kl_parser/ 에서 — 시험 환경 만들기
python -m venv .venv-entry
.venv-entry/Scripts/python.exe -m pip install -r requirements.txt
.venv-entry/Scripts/python.exe -m pip install -e ..

# ① 서버·GPU 없이 되는 검사 (1초)
.venv-entry/Scripts/python.exe tests/boot_check.py
#   → 검사 3건 · 실패 0건

# ② 서버 기동
./scripts/run-local.sh
#   → 진입점 기동: http://127.0.0.1:9101
```

서버가 떴으면 실제로 불러 본다.

```bash
# 접수 — uuid 를 받는다
curl -X POST http://127.0.0.1:9101/parsing -F "src_file=@문서.pdf"
#   → {"result":"OK","body":{"uuid":"3f2a…","timeout":1800}}

# 폴링 — 끝날 때까지 PARSING 이 나온다
curl http://127.0.0.1:9101/parsing/result/3f2a…
#   → {"status":"PARSING"}

# 완료되면 같은 요청이 ZIP 을 준다
curl -o out.zip http://127.0.0.1:9101/parsing/result/3f2a…
```

돌고 있는 동안 `temp/<uuid>/`를 열어 보면 진행 상황이 그대로 보인다 — 업로드된 원본,
`genaikl.status`, 그리고 완성되는 대로 결과 파일이 쌓인다.

왕복을 스크립트로 확인할 수도 있다. 다만 이 둘은 **실제 파싱을 수행**하므로
PaddleX·VLM 서비스가 준비된 환경에서만 돌아간다.

```bash
./scripts/test-roundtrip.sh                                     # 규정문서
.venv-entry/Scripts/python.exe tests/roundtrip_ad.py <광고파일>   # 광고물
```

## 환경변수

| 변수 | 기본값 | 역할 |
|---|---:|---|
| `PATH_TEMP` | `./temp` | UUID별 작업 폴더의 상위 경로 |
| `TIMEOUT` | `1800`초 | KL에게 알려 주는 최대 대기 시간 |
| `KEEP_WORK_DIR` | 꺼짐 | `1`이면 성공한 작업 폴더도 남김 |
| `PADDLEX_URL` | 별도 설정 | OCR/레이아웃 GPU 서비스 주소 (`nh_parsing`이 사용) |
| `GEMMA_URL` | 별도 설정 | VLM GPU 서비스 주소 (`nh_parsing`이 사용) |

## 현재 확인된 것과 남은 확인

확인됨:

- 농협과 같은 `cd app_custom_parser && gunicorn main:app` import 방식 검사 통과
- 오류 상태와 없는 UUID 응답 규칙 검사 통과
- 농협 제공 JSONL 샘플이 `spec_check.py`를 통과
- 규정문서·광고물 두 트랙 모두 로컬에서 접수 → 폴링 → ZIP 왕복 확인

남은 확인:

- **Python 3.11 대응**: 현재 `nh_parsing`은 `requires-python >=3.13`인데 농협 환경은
  3.11 기준이다. 3.11에서 돌게 맞추는 작업을 추후 진행한다
- 실제 농협 Custom Parser 이미지에서 `nh_parsing`과 HWP/JVM 의존성을 함께 반입할 수 있는지
- 농협 내부 PaddleX/VLM 주소·인증·timeout 설정
- 실제 농협 환경에서 장시간 문서 파싱과 결과 ZIP 수신이 안정적으로 되는지
- 광고 트랙에 HWP/HWPX를 넣는 경우. 파싱 자체는 라우팅되지만 좌표가 없어 광고 트랙의
  목적(근거 위치 표시)을 채우지 못한다. 이 조합은 이 API로 검증하지 않았다
- KL 검색 API를 사용한 최종 규정 검색·심의 판단 연결

## 정리

> `kl_parser`는 농협 KL이 호출하는 Custom Parser API입니다. 파일을 접수하고 UUID로
> 상태를 관리한 뒤, 실제 파싱은 같은 프로세스의 `nh_parsing`에 맡깁니다. `nh_parsing`은
> 농협 내부의 PaddleX OCR과 VLM GPU 서비스를 HTTP로 호출해 문서를 구조화합니다.
> 규정문서는 KL 색인용 JSONL로, 광고물은 좌표와 템플릿 라벨을 포함한 JSON으로
> 반환합니다. 현재 API 계약과 로컬 왕복 검증은 되어 있고, Python 3.11 대응과 농협 운영
> 환경 의존성 반입, 실제 GPU 연동은 배포 전 확인 항목입니다.
