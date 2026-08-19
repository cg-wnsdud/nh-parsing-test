# -*- coding: utf-8 -*-
"""규정문서 RAG 청크 → 농협 KL 커스텀 파서 출력 규격(`_hrc.jsonl` 3종).

**이 변환기가 맡는 것은 규정문서 트랙뿐이다.** 광고물은 KL 에 적재하지 않는다 —
KL 규격의 item 에는 좌표를 담을 칸이 없고(§규격 대조 아래), 광고심의는 "이 지적의
근거가 원본 어디인지"를 화면에 표시해야 해서 좌표가 본질이다. 광고물 산출물은
`out/extracted/*.json` 그대로 심의 앱 DB 로 간다.

**규격 출처**: `농협프로젝트/1.awx_custom_parser_example_api/readme.md` 의 "Output jsonl"
단락과 실제 정답 샘플 `out_sample_hrc.jsonl`(141줄). 규격서와 샘플이 어긋나는 대목이
4건 있어(문서화 안 된 `item: "break"`, image `value` 가 절대경로, 크기 단위 mm↔픽셀,
cust_meta 키 개수 5↔10) 그 항목은 아래 주석에 근거와 우리 선택을 적었다. 현장에서
확인할 목록은 `docs/농협KL_미확정대장_2026-08-19.md`.

**출력 3종** (파일명 규칙은 확장자를 떼지 않고 그대로 붙인다 — 규격 명시):
    <원본파일명>_hrc.jsonl   한 줄에 item 하나. 필수
    <원본파일명>_hrc.json    문서 정보(페이지 수·크기·파싱상태)
    <원본파일명>_img.zip     추출된 이미지 전체. 경로 없이 파일만 압축

최종 응답 zip 은 이 3개를 묶은 것이고 그건 KL 진입점 껍데기(`main.py`)가 만든다.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ─────────────────────────── 규격 상수 ───────────────────────────
# cust_meta 키 개수. readme 본문(:174)은 `cust_arrt1~5`, 표(:215~229)는 `cust_attr1~10`
# 으로 **서로 다르게** 적혀 있다. 이름도 본문은 `cust_arrt`(오타), 정의 예시는
# `cust_mata`(오타), 실제 jsonl 예시(:257)만 `cust_meta`+`cust_attr1` 로 정상이다.
# 예시를 정본으로 보되 **개수는 적은 쪽(5)에 맞춘다** — 6~10 이 실제로 저장되는지
# 확인되지 않았고, 규정문서에 필요한 칸은 5개 안에서 충분하다.
MAX_ATTR = 5
MAX_SATTR = 5

# `cust_attr*` 은 "(조회only)", `cust_sattr*` 은 "(검색,조회) … 임베딩시 포함"이다
# (readme 표). 그래서 기계값(chunk_id·kind)은 attr 로, 검색어로 써야 하는 것
# (출처 문서명·조항 제목)은 sattr 로 나눈다. 좌표·ID 를 sattr 에 넣으면 임베딩이
# 오염돼 검색 품질이 떨어진다.

# 개요 기호 → 제목 깊이. rag_ingest 가 청크를 나눌 때 쓰는 것과 **같은 근거**다
# (모듈 주석: "청크 분할은 구조적 경계(조항 번호 등 개요 기호)와 길이 상한만 쓴다").
# 판별이 안 되면 h4 로 떨어뜨린다 — 억지로 계층을 만들지 않는다.
_HEADING_DEPTH = (
    (re.compile(r"^\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\s*[.．)]?"), "h1"),   # Ⅰ. 목적
    (re.compile(r"^\s*제\s*\d+\s*[조장절관]"), "h1"),           # 제3조(직원의 구분)
    (re.compile(r"^\s*\d+\s*[.．)]"), "h2"),                    # 1. 준수사항
    (re.compile(r"^\s*[가나다라마바사아자차카타파하]\s*[.．)]"), "h3"),  # 가. 최고금리를…
    (re.compile(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]"), "h3"),
    (re.compile(r"^\s*\(\s*\d+\s*\)"), "h4"),                   # (1)
)


@dataclass
class KlExportResult:
    jsonl_path: Path
    info_path: Path
    img_zip_path: Path | None
    item_count: int
    item_kinds: dict[str, int] = field(default_factory=dict)
    images: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def heading_level(heading: str) -> str:
    """조항 제목 → `h1`~`h4`. 판별 실패는 `h4`."""
    for pattern, level in _HEADING_DEPTH:
        if pattern.match(heading):
            return level
    return "h4"


def _product_group(source_file: str) -> str:
    """파일명에서 상품군을 결정론적으로 읽는다 — 검색 필터로 쓰라고 sattr 에 넣는다.

    파일명이 근거인 이유: 은행연합회 규정 문서는 제목에 대상 상품군이 들어 있다
    (`(2023년)예금성상품 광고시 준수사항`, `(2025년)대출성 상품 광고시 준수사항`).
    본문을 읽어 판단하지 않는다 — 그건 VLM 이 할 일이고, 여기서는 규칙으로 확정
    가능한 것만 붙인다. 판별 안 되면 빈 문자열로 두고 억지로 정하지 않는다.
    """
    if "예금성" in source_file:
        return "예금성"
    if "대출성" in source_file:
        return "대출성"
    return ""


def _cust_meta(attrs: list[str], sattrs: list[str]) -> list[dict[str, str]]:
    """`[{"name": "cust_attr1", "value": "..."}]` 형식. 빈 값은 칸을 쓰지 않는다."""
    out: list[dict[str, str]] = []
    for i, v in enumerate(attrs[:MAX_ATTR], start=1):
        if v:
            out.append({"name": f"cust_attr{i}", "value": v})
    for i, v in enumerate(sattrs[:MAX_SATTR], start=1):
        if v:
            out.append({"name": f"cust_sattr{i}", "value": v})
    return out


def build_items(rag_doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """RagChunk 목록 → KL item 목록. 두 번째 반환값은 우리가 내린 판단의 기록.

    **kind 매핑과 그 근거**

    | RagChunk.kind   | KL item        | 왜 |
    |---|---|---|
    | `text`          | `text`         | 그대로 |
    | `table`         | **`text`**     | 우리 table 청크의 `text` 는 HTML 이 아니라 평문이다(실측: "Ⅰ. 목적", "Ⅲ. 일반원칙"). KL 의 `table` item 은 `value` 가 HTML `<table>` 이어야 하므로, 평문을 `<table>` 로 감싸면 표가 아닌 것을 표로 신고하는 것이 된다. 원래 kind 는 `cust_attr1` 에 남긴다 |
    | `image_caption` | **`text`**     | KL 의 `image` item 은 `value` 가 파일명이고 캡션을 담을 곳이 `type_property.title` 뿐인데, 그 title 이 임베딩 대상인지 규격에 없다. readme "VectorDB 활용 예"에서 본문으로 임베딩되는 것은 `TEXT` 의 `value` 다 → 캡션은 text item 으로 넣어 검색에 걸리게 한다. 이미지 파일 자체는 별도 `image` item + `_img.zip` |
    | `ocr_page`      | `text`         | 사내 파서가 건너뛴 스캔 페이지를 OCR 로 복원한 것. **정본 신뢰도가 낮다**는 사실을 `cust_attr4="ocr"` 로 남긴다 |

    `heading` 이 있으면 그 청크 **앞에** `h1`~`h4` item 을 넣는다. KL 은 h 아이템을
    "이후 본문의 meta" 로 적용하므로(readme:300) 순서가 뜻을 만든다. 같은 제목이
    연달아 나오면 한 번만 넣는다.
    """
    source_file = rag_doc.get("source_file") or rag_doc.get("doc_id") or ""
    doc_title = Path(source_file).stem
    pgroup = _product_group(source_file)

    items: list[dict[str, Any]] = []
    notes: list[str] = []
    last_heading: str | None = None
    table_as_text = 0
    caption_as_text = 0

    for chunk in rag_doc.get("chunks", []):
        kind = chunk.get("kind") or "text"
        text = (chunk.get("text") or "").strip()
        heading = (chunk.get("heading") or "").strip()
        chunk_id = chunk.get("chunk_id") or ""
        asset_id = chunk.get("asset_id") or ""

        if heading and heading != last_heading:
            items.append({"item": heading_level(heading), "value": heading})
            last_heading = heading

        if not text:
            continue

        if kind == "table":
            table_as_text += 1
        elif kind == "image_caption":
            caption_as_text += 1

        # `page` 는 0 으로 낸다. RagChunk 에 페이지 정보가 없고, 규격이 "페이지 번호를
        # 알 수 없는 경우에는 0" 을 허용한다. 농협 정답 샘플도 141줄 전부 page=0 이었다.
        items.append(
            {
                "item": "text",
                "value": text,
                "page": 0,
                "cust_meta": _cust_meta(
                    attrs=[
                        kind,                                   # attr1 원래 청크 종류
                        chunk_id,                               # attr2 우리 산출물과 대조
                        asset_id,                               # attr3 캡션의 원본 이미지
                        "ocr" if kind == "ocr_page" else "digital",  # attr4 정본 신뢰도
                    ],
                    sattrs=[
                        doc_title,                              # sattr1 출처 문서명 (검색어)
                        pgroup,                                 # sattr2 상품군 (검색 필터)
                        heading or (last_heading or ""),        # sattr3 조항 제목
                    ],
                ),
            }
        )

    # 이미지 파일 자체. `value` 는 **파일명**으로 낸다 — 규격서(readme)가 "이미지
    # 파일명" 이라고 적었다. 정답 샘플은 절대경로였지만(`/HYB008/datasets/...`) 그건
    # 농협 내부 경로라 우리가 흉내낼 수 없고, 흉내내면 존재하지 않는 경로를 신고하는
    # 것이 된다. 현장 확인 항목.
    for chunk in rag_doc.get("chunks", []):
        if chunk.get("kind") == "image_caption" and chunk.get("asset_id"):
            items.append(
                {
                    "item": "image",
                    "value": f"{chunk['asset_id']}.png",
                    # width·height 는 실제 픽셀이다. 규격서는 "mm 기준"이라고 적었지만
                    # 정답 샘플이 1102×557(A4 폭 210mm 를 넘는 값)이어서 픽셀로 보인다.
                    # ratio 는 "페이지 크기 대비"인데 규정문서는 페이지 크기를 모른다
                    # (RagChunk 에 페이지 정보 없음) → 빈 문자열로 두고 현장 확인한다.
                    "type_property": {"title": "", "width": 0, "height": 0, "ratio": ""},
                    "page": 0,
                    "cust_meta": _cust_meta(
                        attrs=["image_asset", chunk.get("chunk_id") or "", chunk["asset_id"], "digital"],
                        sattrs=[doc_title, pgroup, ""],
                    ),
                }
            )

    if table_as_text:
        notes.append(
            f"table 청크 {table_as_text}개를 text item 으로 내보냈다 — 청크의 text 가 "
            f"HTML 이 아니라 평문이어서 KL 의 table item 규격(value=HTML <table>)을 "
            f"만족할 수 없다. 원래 kind 는 cust_attr1 에 남겼다."
        )
    if caption_as_text:
        notes.append(
            f"image_caption 청크 {caption_as_text}개를 text item 으로 내보냈다 — "
            f"임베딩 대상이 TEXT 의 value 라서 캡션이 검색에 걸리게 하려는 선택이다. "
            f"이미지 파일은 별도 image item + _img.zip 으로 함께 보낸다."
        )
    if not any(i["item"].startswith("h") for i in items):
        notes.append("h1~h4 item 이 0개다 — 이 문서의 청크에 heading 이 붙지 않았다.")
    return items, notes


def build_info(rag_doc: dict[str, Any], source_path: Path | None) -> dict[str, Any]:
    """`_hrc.json` — 문서 정보.

    `page_info` 는 페이지 크기 목록인데 RagChunk 에 페이지 정보가 없다. PDF 는
    pypdfium2 로 실측할 수 있고 HWP 는 알 수 없다 — **모르는 것을 지어내지 않고
    빈 배열로 둔다.** 규격서 샘플은 PDF 를 pt(595.3×841.9), HWP 를 cm(21.0×29.7)로
    적었는데 단위 규정이 없어 이것도 현장 확인 항목이다.
    """
    source_file = rag_doc.get("source_file") or ""
    page_info: list[dict[str, Any]] = []
    size = 0
    if source_path and source_path.is_file():
        size = source_path.stat().st_size
        if source_path.suffix.lower() == ".pdf":
            try:
                import pypdfium2 as pdfium

                pdf = pdfium.PdfDocument(str(source_path))
                for n in range(len(pdf)):
                    page = pdf[n]
                    page_info.append({"page": n + 1, "width": page.get_width(), "height": page.get_height()})
                pdf.close()
            except Exception:  # 페이지 크기는 부가정보다 — 못 읽으면 빈 배열로 둔다
                page_info = []
    return {
        "name": Path(source_file).name,
        "source": str(source_path) if source_path else source_file,
        "file_type": (rag_doc.get("file_type") or Path(source_file).suffix.lstrip(".")).lower(),
        "size": size,
        "title": Path(source_file).stem,
        "parsed_status": "S",  # 정답 샘플의 값. 값 목록이 규격서에 없어 현장 확인 항목
        "page_info": page_info,
    }


def export_kl_files(
    rag_doc: dict[str, Any],
    out_dir: Path,
    *,
    source_path: Path | None = None,
    asset_dir: Path | None = None,
) -> KlExportResult:
    """규정문서 청크 하나 → `_hrc.jsonl` + `_hrc.json` + `_img.zip` 을 `out_dir` 에 쓴다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(rag_doc.get("source_file") or rag_doc.get("doc_id") or "unknown").name

    items, notes = build_items(rag_doc)
    jsonl_path = out_dir / f"{base}_hrc.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    info_path = out_dir / f"{base}_hrc.json"
    info_path.write_text(
        json.dumps(build_info(rag_doc, source_path), ensure_ascii=False), encoding="utf-8"
    )

    # 이미지는 **경로 없이 파일만** 압축한다(규격 명시).
    img_zip_path: Path | None = None
    images: list[str] = []
    if asset_dir and asset_dir.is_dir():
        files = sorted(p for p in asset_dir.iterdir() if p.is_file())
        if files:
            img_zip_path = out_dir / f"{base}_img.zip"
            with zipfile.ZipFile(img_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in files:
                    zf.write(p, p.name)
                    images.append(p.name)

    kinds: dict[str, int] = {}
    for item in items:
        kinds[item["item"]] = kinds.get(item["item"], 0) + 1
    return KlExportResult(
        jsonl_path=jsonl_path,
        info_path=info_path,
        img_zip_path=img_zip_path,
        item_count=len(items),
        item_kinds=kinds,
        images=images,
        notes=notes,
    )
