# I. 광고 파싱 결과를 어디에 어떻게 저장하나 — 설계

> **작성** 2026-08-24 · **상태: 설계만.** 실물 미구현이다.
> **막힌 이유**: PostgreSQL 접속 정보를 못 받았다("내부 투입 시 제공", D문서 §확인항목 ③).
> **원칙**: 아래 숫자·키 목록은 전부 실제 산출물(`out_ad/json/*.json`)에서 뽑았다.
> 지어낸 필드는 없다. 미결은 미결로 적었다(§7).

---

## 1. 왜 DB 가 필요한가 — 30초

```
규정문서 → _hrc.jsonl → 농협 KL → 농협 벡터DB      ← 농협이 저장한다. 우리 일 아님
광고물   → 통합 JSON  → ???                        ← 받는 곳이 없다
```

**농협 KL 은 광고물을 받지 않는다.** 규격(`readme.md`)의 `item` 유형은
`text · table · image · h1~h4` 뿐이고 부가정보는 `cust_meta`(문자열 key-value)만
허용한다 — **좌표를 담을 칸이 없다.** 광고 심의는 "이 지적의 근거가 원본 어디인가"를
화면에 찍어야 하므로 좌표가 본질이다. 그래서 광고 결과는 KL 밖에 있어야 한다.

지금 있는 건 **파일 json 뿐**이다(`out_ad/json/`). 그래서 이 문서가 필요하다.

---

## 2. 저장할 것이 두 가지다 — 이번엔 하나만

| | 무엇 | 왜 DB 인가 | 이번 범위 |
|---|---|---|---|
| **(가) 파싱 원장** | 통합 JSON — 좌표 + 템플릿 라벨 | 문서당 **162초**(89건 실측 평균)라 재파싱이 비싸다. 한 번 파싱하고 재사용 | **설계함** |
| (나) 심의 이력 | 지적사항 · 판정 · 검수자 확인 | 조회 · 이력 · 재심의 | **안 함** — 심의 엔진 규격이 먼저다 |

(가)만으로도 값이 있다. 89건 = **4시간 1분**(실측 14,452초)인데, 원장이 있으면
같은 파일을 다시 안 돌린다.

> **(나)를 미루는 이유.** 심의 엔진이 아직 없다. 스키마를 먼저 만들면 엔진이 생길 때
> 안 맞아서 버리게 된다. 이 프로젝트는 그런 식으로 죽은 모델을 이미 두 개 지웠다
> (`Section`, `ExtractedField` — `ir.py` 주석 참조).

---

## 3. 정본을 하나로 둔다 — 이게 설계의 핵

같은 정보를 두 곳에 두면 어느 쪽이 맞는지 알 수 없게 된다. 그래서:

```
정본  ad_document.unified   JSONB 한 칸에 통합 JSON 통째로
                            ↓ 여기서 파생 (derived)
색인  ad_page · ad_region · ad_line · ad_line_label · ad_table_cell
```

**색인 테이블은 질의용 파생물이다.** 어긋나면 JSONB 를 다시 펼쳐 만든다. 반대는 없다.
`template_items`(항목→문구→줄) 는 **테이블을 만들지 않는다** — `ad_line_label` 의
역방향 보기라 파생의 파생이다. 필요하면 질의로 뒤집는다.

크기는 문제가 아니다. 실측 파일 크기 45~103 KB, 89건이면 **약 7 MB**.

---

## 4. 테이블

### 4-1. `ad_document` — 문서 한 번의 파싱 = 한 행

```sql
CREATE TABLE ad_document (
  doc_uid            bigserial PRIMARY KEY,   -- 같은 파일을 여러 번 파싱하므로 대리키
  source_file        text    NOT NULL,        -- '[예금성상품-적금] 올원e적금.png'
  source_sha256      char(64) NOT NULL,       -- 원본 파일 해시 (재파싱 판별용)
  doc_id             text    NOT NULL,        -- 통합 JSON 의 doc_id (확장자 뗀 이름)
  file_type          text    NOT NULL,        -- pdf | image | hwp | hwpx
  product_group      text,                    -- 예금성 | 대출성 | 카드 …
  ad_type            text,                    -- 상세페이지 | 상품광고 …
  product_name_shown text,                    -- 노출 | 미노출 | NULL(판단불가)
  class_source       text,                    -- filename_and_vlm | vlm | …
  class_confidence   real,
  template_id        text,                    -- '예금성상품-적립식'
  template_status    text,                    -- 확정 | 판단불가
  pipeline_version   text    NOT NULL,        -- 코드 버전 (커밋 해시)
  parsed_at          timestamptz NOT NULL DEFAULT now(),
  unified            jsonb   NOT NULL,        -- ★ 정본
  UNIQUE (source_sha256, pipeline_version, parsed_at)
);
CREATE INDEX ON ad_document (source_sha256, pipeline_version);
CREATE INDEX ON ad_document (product_group, template_id);
```

**왜 대리키(`doc_uid`)인가.** 파일명은 유일하지 않고(`13. 대출성상품.pdf` 가 두 폴더에
있다), 같은 파일을 여러 번 파싱한다. 그리고 **다시 파싱하면 결과가 조금 달라진다** —
정본층(OCR/digital)은 100% 재현되지만 VLM 층은 실행마다 흔들린다(실측: 같은 입력 2회에
정본 484줄 중 2줄이 갈렸고 그 2줄이 전부 VLM 경로였다). 두 실행을 **구분해서** 보관해야
"왜 어제와 다른가"에 답할 수 있다.

**재사용 판정.** `source_sha256` + `pipeline_version` 이 같으면 재파싱하지 않고
가장 최근 행을 쓴다. 파이프라인 코드가 바뀌면 해시가 같아도 다시 돌린다 —
`pypdfium2` 마이너 버전 하나가 렌더 픽셀을 바꿔 OCR 결과가 갈린 적이 있다
(H문서 §6-⑤). 그래서 버전을 키에 넣는다.

### 4-2. `ad_page`

```sql
CREATE TABLE ad_page (
  doc_uid      bigint  NOT NULL REFERENCES ad_document ON DELETE CASCADE,
  page_no      int     NOT NULL,
  canvas_w     int     NOT NULL,      -- 좌표의 기준. 실측 720~1920 x 1080~6429
  canvas_h     int     NOT NULL,
  dpi          int,                   -- PDF 렌더 시만. 이미지 원본은 NULL
  parse_route  text    NOT NULL,      -- ocr | hybrid | digital
  parse_status text    NOT NULL,      -- ok | partial | unreadable
  PRIMARY KEY (doc_uid, page_no)
);
```

`canvas_w/h` 를 반드시 같이 둔다 — **bbox 는 이 캔버스 픽셀 좌표**라서 캔버스 크기를
모르면 좌표를 해석할 수 없다. 검수 이미지가 축소 저장되는 일이 있어(실측: 캔버스
1122×6429 인데 jpg 1000×5730) 환산이 필요하다.

### 4-3. `ad_region`

```sql
CREATE TABLE ad_region (
  doc_uid          bigint NOT NULL,
  page_no          int    NOT NULL,
  region_id        text   NOT NULL,   -- 'p1_r000'
  ord              int    NOT NULL,   -- 읽는 순서 (0부터). 배열 순서를 열로 고정
  bbox             int[4] NOT NULL,
  role             text,              -- 제목|본문|유의사항|각주|버튼|고지문구|이미지|표|기타
  gubun            text,              -- 템플릿 항목명 ('우대금리')
  gubun_source     text,              -- vlm | phrase
  gubun_confidence real,
  vlm_reading      text,              -- 영역 통독 후보. 정본이 아니다
  vlm_reading_relation text,          -- same|tail_cut|head_drop|expanded|diverged
  table_n_rows     int,               -- 표 영역만. 아니면 NULL
  table_n_cols     int,
  table_html       text,              -- PaddleX pred_html 원본
  table_note       text,              -- 격자를 온전히 못 만든 이유
  PRIMARY KEY (doc_uid, region_id),
  FOREIGN KEY (doc_uid, page_no) REFERENCES ad_page ON DELETE CASCADE
);
CREATE INDEX ON ad_region (doc_uid, gubun);
```

**`ord` 를 왜 열로 두나.** 관계형 테이블은 행 순서를 보장하지 않는다. 읽는 순서는
2026-08-24 에 파이프라인에서 확정하게 만든 값이라(`_finalize_reading_order`) 잃으면
문장이 거꾸로 붙는다 — 실측: `'수 있는 권리가 있습니다. 금융소비자 보호에 관한 법률…'`.

`role` 과 `gubun` 을 **나란히** 둔다. 서로 다른 어휘고(파서 범용 역할 ↔ 템플릿 항목명)
서로 검산에 쓴다. 한쪽이 다른 쪽을 덮으면 그 검산을 못 한다.

### 4-4. `ad_line` — 가장 큰 테이블

```sql
CREATE TABLE ad_line (
  doc_uid    bigint NOT NULL,
  line_ref   text   NOT NULL,      -- 'p1/p1_r000/L00' — 통합 JSON 과 같은 문자열
  page_no    int    NOT NULL,
  region_id  text,                 -- NULL = 영역에 못 붙은 낱줄
  ord        int    NOT NULL,      -- 영역 안 순서
  text       text   NOT NULL,
  bbox       int[4],
  source     text   NOT NULL,      -- ocr | digital | vlm_sweep | vlm_direct
  confidence real,
  style      jsonb,                -- pt·색·굵기 (HWP/PDF 만. OCR 은 NULL)
  vlm_reading      text,           -- 줄 단위 재판독 후보
  vlm_reading_stage text,          -- sweep_dedupe | lowconf_reread
  PRIMARY KEY (doc_uid, line_ref)
);
CREATE INDEX ON ad_line (doc_uid, region_id);
CREATE INDEX ON ad_line (doc_uid, source);
```

**`source` 로 층을 가를 수 있어야 한다.** 이게 이 테이블에서 제일 중요한 열이다:

```
ocr / digital   결정론 — 같은 입력이면 같은 결과. 여기 차이는 결함이다
vlm_sweep 등    실행마다 흔들림 — 차이를 결함으로 세면 없는 버그를 만든다
```

이 구분을 안 해서 "줄 수 53 vs 55" 로 없는 결함을 만든 적이 있다(H문서 §6-⑥).
질의에서도 같은 함정이 있으므로 층을 섞어 세는 집계는 만들지 않는다.

**`region_id IS NULL` 을 허용하는 이유.** 영역에 못 붙은 줄도 텍스트다. 실측 22개 중
20개가 `vlm_sweep` 후보였고 대부분 정본과 중복이었지만, 버리면 '모든 텍스트 보존'이
깨진다. 낮은 라벨 커버리지는 결함이 아니다 — 광고 헤드라인처럼 **템플릿에 해당 항목이
없어서** 라벨이 안 붙는 게 정상인 경우가 많다.

### 4-5. `ad_line_label`

```sql
CREATE TABLE ad_line_label (
  doc_uid   bigint NOT NULL,
  line_ref  text   NOT NULL,
  gubun     text   NOT NULL,       -- 템플릿 항목명
  phrase_id text   NOT NULL,       -- 'P001'
  how       text,                  -- 한줄내 | 여러줄 …
  PRIMARY KEY (doc_uid, line_ref, gubun, phrase_id),
  FOREIGN KEY (doc_uid, line_ref) REFERENCES ad_line ON DELETE CASCADE
);
CREATE INDEX ON ad_line_label (doc_uid, gubun);
```

한 줄에 문구가 여러 개 걸릴 수 있어 별도 테이블이다(실측: 유의사항이 `■` 로 붙어
한 줄에 두 문구가 든 경우).

### 4-6. `ad_table_cell` — 2026-08-24 신설

```sql
CREATE TABLE ad_table_cell (
  doc_uid   bigint NOT NULL,
  region_id text   NOT NULL,
  row       int    NOT NULL,
  col       int    NOT NULL,
  row_span  int    NOT NULL DEFAULT 1,
  col_span  int    NOT NULL DEFAULT 1,
  bbox      int[4],                 -- 셀 개수가 안 맞으면 NULL (틀린 좌표보다 없는 쪽)
  cell_text text,                   -- PaddleX 표 인식 값. 정본이 아니다
  PRIMARY KEY (doc_uid, region_id, row, col),
  FOREIGN KEY (doc_uid, region_id) REFERENCES ad_region ON DELETE CASCADE
);

CREATE TABLE ad_table_cell_line (   -- 셀 ↔ 정본 줄 (다대다)
  doc_uid   bigint NOT NULL,
  region_id text   NOT NULL,
  row       int    NOT NULL,
  col       int    NOT NULL,
  line_ref  text   NOT NULL,
  PRIMARY KEY (doc_uid, region_id, row, col, line_ref)
);
```

**`cell_text` 가 정본이 아닌 이유.** 표 안 글자에도 OCR 오류가 있다(실측 `'BD'`,
`'2 295,000원'`). 디지털 텍스트가 대개 더 정확하다. 그래서 표 인식 값은 근거로 남기고
정본은 `ad_line` 쪽을 가리킨다(`ad_table_cell_line`). **어느 쪽을 쓸지는 하류가 고른다** —
여기서 대신 고르지 않는다.

이 테이블이 있으면 우대금리 질의가 한 줄로 된다:

```sql
-- '우대조건 ↔ 우대금리' 짝
SELECT a.cell_text AS 조건, b.cell_text AS 금리
FROM ad_table_cell a
JOIN ad_table_cell b USING (doc_uid, region_id, row)
WHERE a.col = 0 AND b.col = 1 AND a.doc_uid = $1 AND a.row > 0;
```

지금은 이게 불가능하다 — 표가 줄로 흩어져 `'-(가입월부터…)입출식05%p'` 처럼 소수점이
문장에 붙는다.

---

## 5. 규모 — 실측 기반

| 단위 | 5문서 실측 | 89문서 실측 | 비고 |
|---|---|---|---|
| 문서 | 5 | 89 | |
| 쪽 | 5 | 97 | |
| 영역 | 33·12·20·53·50 = 168 | **2,463** | |
| 줄 | 70·55·73·97·78 = 373 | **7,360** | 가장 큰 테이블 |
| 표 영역 | 1 (13.대출성) | 48 | 그중 `role=표` 는 29 |
| JSONB | 45~103 KB | 약 7 MB | 무시할 크기 |

**89문서를 전부 넣어도 만 행이 안 된다.** 인덱스 설계에 고민할 규모가 아니다.
파티셔닝·샤딩은 필요 없다.

---

## 6. 적재 방식

```
process_ad_file(path) → 통합 JSON
    ↓ 트랜잭션 하나로
INSERT ad_document (unified = 통합 JSON 통째)   ← 정본 먼저
    ↓ 같은 트랜잭션에서 JSONB 를 펼쳐
INSERT ad_page / ad_region / ad_line / ad_line_label / ad_table_cell(+_line)
COMMIT
```

**한 트랜잭션인 이유.** 정본만 들어가고 색인이 안 들어가면 질의 결과가 조용히
비어 보인다 — "광고에 그 말이 없다"로 둔갑하는 그 실패다. 통합 JSON 생성 단계가
이미 텍스트가 새면 예외를 던지므로(`ad_export._verify`), DB 도 같은 태도를 잇는다.

색인을 다시 만드는 함수를 하나 둔다(`reindex(doc_uid)`) — 색인 구조를 바꿀 때
재파싱(문서당 162초) 없이 JSONB 에서 다시 펼치면 된다.

---

## 7. 정하지 않은 것 — 농협 확인 필요

| # | 항목 | 왜 못 정했나 |
|---|---|---|
| ① | 접속 정보 (host/db/user) | "내부 투입 시 제공" — 미제공 |
| ② | PostgreSQL 버전 | `int[]`·`jsonb`·`gen_random_uuid` 가용 여부가 갈린다 |
| ③ | 스키마 이름 · 테이블 접두어 규칙 | 사내 명명 규칙을 모른다 |
| ④ | 원본 파일 자체를 DB 에 넣나 | 지금 설계는 해시만 넣는다. 원본 보관 위치는 농협 결정 |
| ⑤ | 보존 기간 · 삭제 정책 | 광고물은 심의 근거 자료라 보존 의무가 있을 수 있다 |
| ⑥ | 심의 이력 스키마 (나) | 심의 엔진 규격 미확정 |
| ⑦ | DAP 와의 관계 | 농협 DAP 쪽에도 저장소가 있다면 어느 쪽이 정본인지 |

**①~③ 이 없어도 설계는 굳는다** — 위 DDL 은 표준 SQL + PostgreSQL 기본 타입만 쓴다.
접속 정보를 받으면 그대로 올린다.

---

## 8. 대안을 안 고른 이유

| 안 | 왜 안 골랐나 |
|---|---|
| **JSONB 한 칸만** (색인 없음) | 좌표·항목 질의를 매번 JSON 안에서 풀어야 한다. 7,360 줄 규모에서 `line_ref → bbox` 조회가 전 문서 스캔이 된다 |
| **정규화만** (JSONB 없음) | 정본이 사라진다. 색인 구조를 바꿀 때마다 재파싱(문서당 162초 × 89 = 4시간)해야 한다 |
| **파일 그대로 두기** | 지금 상태다. 문서당 162초를 매번 다시 쓴다. 그리고 `out_ad/` 는 실행마다 비워진다 |
| **KL 벡터DB 에 넣기** | 규격에 좌표 칸이 없다(§1). 좌표가 본질인 트랙이라 애초에 안 맞는다 |
| **SQLite** | 지금 당장 돌게 만들 수는 있다. 다만 목표가 농협 PostgreSQL 이라 두 번 만드는 셈이고, 사용자 결정은 "설계만" 이다 |

---

## 9. 이 문서를 쓰면서 눈에 띈 것

| 관찰 | 어디 |
|---|---|
| `line_ref` 가 `'p1/p1_r000/L00'` — 쪽 접두어가 두 번 들어간다(`rid` 에 이미 `p1_` 이 있다). 결함은 아니다(`ad_template`·`ad_export` 가 같은 규칙을 쓴다) — 다만 사람이 읽기 나쁘다. 바꾸면 기존 참조가 전부 깨지므로 여기 기록만 한다 | §4-4 |
| `template_items` 를 테이블로 만들 유혹이 크다. `ad_line_label` 의 역방향 보기라 파생의 파생이다 — 두면 어긋난다 | §3 |
| 89문서 실측 런에 **VLM 실패 5건**이 있었다(2건은 산출물 무효). 원장에 넣기 전에 그 판정을 열로 남길 필요가 있다 — `ad_page.parse_status` 가 그 자리다 | §4-2 |
