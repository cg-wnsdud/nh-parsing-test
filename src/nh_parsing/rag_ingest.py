"""rag-data 트랙 [Track A] — 기준 문서(법령·규제·심의사례) → RAG 청크. 설계서 5절.

광고물 트랙(AdPageIR)과 달리 출력은 검색용 청크 목록이다.
- 텍스트·표: document-processor 디지털 추출이 정본
- 내장 이미지: VLM 캡션(전사+설명)으로 텍스트화해 청크에 포함
  (은행연합회 준수사항 HWP 실측: 금리 표기 사례 스크린샷 10장 —
   실질 심의 기준 예시가 이미지 안에 있음, 2026-07-18)

위치 앵커: DocIR 문단 스트림에 이미지 참조 '노드'는 없지만, RunIR 텍스트에
"[image:<asset키>]" 마커가 남는다(2026-07-18 실측) → 캡션 청크를 마커가 있는
청크 바로 뒤에 삽입한다. 마커가 없는 이미지만 문서 말미에 부착.

- 스캔형 PDF 페이지: document-processor 는 OCR 이 없어 **조용히 건너뛴다**
  (`page.parse_status == "skipped"`). 그 페이지만 렌더 → OCR 해서 되살린다.
  실측(2026-08-12): `(2025년)대출성 상품 광고시 준수사항_은행연합회.pdf` 6쪽 중
  3쪽이 `pdf_scan_like` 로 스킵돼 **규정 원문 한 쪽이 RAG 인덱스에 없었다.**

청크 분할은 구조적 경계(조항 번호 등 개요 기호)와 길이 상한만 쓴다.
내용 판단(무엇이 중요한 기준인지)은 이후 RAG/VLM 단계의 몫이다.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from .assets import decode_asset_image, is_decorative, iter_assets
from .gemma_client import chat_json, image_part
from .hwp_ingest import _paragraph_text, _tables_to_lines, table_to_html


class RagChunk(BaseModel):
    chunk_id: str
    kind: str  # text | table | image_caption | ocr_page
    heading: str | None = None   # 청크가 속한 조항/제목 (출처 표시용)
    text: str
    asset_id: str | None = None  # image_caption 청크의 원본 asset 키
    # 표 청크의 HTML 표현. `text` 는 사람이 읽는 평문(미리보기·검색용)이고 이쪽은
    # 농협 KL 의 `table` item 이 요구하는 `<table>` 문자열이다. 구조 API 가 없거나
    # 셀이 비면 None — 그때 하류는 text item 으로 낸다(표가 아닌 것을 표로 신고하지 않는다).
    table_html: str | None = None
    notes: list[str] = Field(default_factory=list)


class RagDocument(BaseModel):
    doc_id: str
    source_file: str
    file_type: str
    chunks: list[RagChunk] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ──────────────────────────── 청크 분할 ────────────────────────────

_MAX_CHUNK_CHARS = 700

# 조항/개요 기호로 시작하는 짧은 문단 = 구조 경계 (내용 판단 아님)
_HEADING_RE = re.compile(
    r"^\s*(?:제?\s?\d+\s?[.조항편장절관)]|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\s?\.?|[가-힣]\.|\(\d+\)|\d+\)"
    r"|[□■◎○●◇◆▷▶☞]|【[^】]*】)"
)


def _is_heading(text: str) -> bool:
    return len(text) <= 80 and bool(_HEADING_RE.match(text))


def _chunk_blocks(blocks: list[tuple[str, str, str | None]], doc_id: str) -> list[RagChunk]:
    """(kind, text, table_html) 문서 순서 블록 → 제목 경계·길이 상한 기준 청크."""
    chunks: list[RagChunk] = []
    buf: list[str] = []
    heading: str | None = None
    buf_heading: str | None = None

    def flush() -> None:
        nonlocal buf
        if buf:
            chunks.append(RagChunk(
                chunk_id=f"{doc_id}_c{len(chunks):03d}",
                kind="text", heading=buf_heading, text="\n".join(buf),
            ))
            buf = []

    for kind, text, table_html in blocks:
        if kind == "table":
            flush()
            chunks.append(RagChunk(
                chunk_id=f"{doc_id}_c{len(chunks):03d}",
                kind="table", heading=heading, text=text, table_html=table_html,
            ))
            continue
        if _is_heading(text):
            flush()
            heading = text
        if not buf:
            buf_heading = heading
        buf.append(text)
        if sum(len(t) for t in buf) >= _MAX_CHUNK_CHARS:
            flush()
    flush()
    return chunks


# ──────────────────────────── 이미지 캡션 (VLM) ────────────────────────────

_CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        # analysis 선행 필드 — strict json_schema 빈 출력 퇴행 방지 (설계서 부록 C-5)
        "analysis": {"type": "string"},
        "transcription": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["analysis", "transcription", "description"],
    "additionalProperties": False,
}

_CAPTION_PROMPT = """금융상품 광고 심의 기준 문서에 삽입된 예시 이미지입니다.

1. analysis: 이미지에 무엇이 보이는지 먼저 관찰을 서술하세요.
2. transcription: 이미지 안의 모든 텍스트를 보이는 그대로 전사하세요.
   숫자·소수점·%·%p 단위를 정확히, 줄바꿈은 " / " 로 구분합니다.
3. description: 이 이미지가 어떤 사례를 보여주는지 1~2문장으로 요약하세요.
   빨간 박스 등 강조 표시가 있으면 어느 부분이 강조됐는지 반드시 명시하세요."""


# ──────────────────────── 스캔형 PDF 페이지 OCR 복원 ────────────────────────

def _page_lead_text(pdf, page_index: int, take: int = 24) -> str:
    """페이지의 첫 글자들(공백 제거) — 스킵 페이지를 끼워 넣을 위치의 앵커.

    DocIR 문단에는 쪽 번호가 없다(실측: `native_anchor.debug_path` 가 전부 `s1.pN`).
    그래서 "스킵된 쪽 **다음** 쪽의 첫 글자"를 텍스트 레이어에서 직접 읽어,
    그 글자로 시작하는 청크 **앞**에 복원 청크를 끼운다.
    """
    try:
        text = pdf[page_index].get_textpage().get_text_bounded() or ""
    except Exception:
        return ""
    return re.sub(r"\s+", "", text)[:take]


def _insert_at_anchor(doc: RagDocument, pdf, page_no: int, chunk: RagChunk) -> None:
    """`chunk` 를 스킵된 `page_no` 다음 쪽이 시작하는 자리 앞에 끼운다.

    DocIR 문단에는 쪽 번호가 없다(실측: `native_anchor.debug_path` 가 전부 `s1.pN`).
    그래서 다음 쪽의 첫 글자를 텍스트 레이어에서 읽어, 그 글자로 시작하는 청크 **앞**에
    복원 청크를 끼운다. 못 찾으면 문서 말미에 붙인다.
    """
    anchor = _page_lead_text(pdf, page_no)  # 다음 쪽(0-based 인덱스 = page_no)
    idx = None
    if anchor:
        idx = next(
            (i for i, c in enumerate(doc.chunks)
             if anchor[:12] and anchor[:12] in re.sub(r"\s+", "", c.text)),
            None,
        )
    if idx is None:
        chunk.notes.append("다음 쪽 앵커를 못 찾음 — 문서 말미 부착")
        doc.chunks.append(chunk)
    else:
        doc.chunks.insert(idx, chunk)


def _ocr_skipped_pdf_pages(path: Path, docir, doc: RagDocument) -> None:
    """사내 파서가 건너뛴 스캔형 페이지를 복원한다 — 디지털 텍스트 우선, 이미지만 OCR.

    광고 트랙에서 이미 쓰는 부품(canvas 렌더 · tiling · PaddleX)만 재사용하고,
    역할 판정·카드 분할·필드 추출 같은 광고 전용 단계는 **타지 않는다** —
    법령 조문에 '제목/유의사항' 역할은 의미가 없고 비용만 든다.

    **왜 디지털 텍스트를 먼저 읽나** (2026-08-19 실측, 대출성 준수사항 3쪽). 사내
    파서가 이 페이지를 `skipped` 로 표시한 건 "문단 추출에 실패했다"는 뜻이지
    "텍스트가 없다"는 뜻이 아니다. `pypdfium2` 로 직접 읽으면 캡션 4줄(69자,
    `[1] 최저·최고금리 병기` 등)이 정확한 띄어쓰기로 나온다. 예전 구현은 이 페이지를
    통째로 렌더해 OCR 했는데, 그 결과 같은 4줄이 `[1]최저·최고금리 병기`처럼
    띄어쓰기가 깨졌다 — 렌더 이미지를 다시 판독하면서 생긴 손실이지 원본 결함이
    아니다. 반면 스크린샷 예시 안의 수치(`연4.4%`, `연7.50%~13.30%` 등)는 디지털
    텍스트 레이어에 없다 — 그건 진짜 이미지라 OCR 이 필요한 부분이다.

    그래서 순서를 둘로 나눈다: ① 디지털 텍스트를 그대로 청크로 낸다(정확) ②
    렌더+OCR 은 계속하되, 디지털 텍스트와 정규화 후 일치하는 줄은 **버리고** 남는
    줄만 청크로 낸다 — 그 남는 줄이 이미지 안에만 있던 내용이다.
    """
    pages = getattr(docir, "pages", None) or []
    skipped = [
        p.page_number for p in pages
        if getattr(p, "parse_status", None) == "skipped" and p.page_number
    ]
    if not skipped:
        return

    import pypdfium2 as pdfium

    from .canvas import native_image_dpi, render_pdf_page
    from .paddlex_client import request_layout_parsing
    from .tiling import dedupe_lines, make_tiles, restore_coords, sort_reading_order

    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:
        doc.notes.append(f"스킵 페이지 {skipped} OCR 복원 실패 — PDF 열기: {exc}")
        return

    for page_no in skipped:
        digital_text = ""
        try:
            digital_text = pdf[page_no - 1].get_textpage().get_text_bounded() or ""
        except Exception:
            pass
        digital_lines = [ln.strip() for ln in digital_text.splitlines() if ln.strip()]
        seen = {re.sub(r"\s+", "", d) for d in digital_lines}

        if digital_lines:
            _insert_at_anchor(doc, pdf, page_no, RagChunk(
                chunk_id=f"{doc.doc_id}_p{page_no:03d}_digital",
                kind="text",
                heading=f"[{page_no}쪽]",
                text="\n".join(digital_lines),
            ))
            doc.notes.append(f"{page_no}쪽 디지털 텍스트 복원 ({len(digital_lines)}줄, {len(digital_text)}자)")

        try:
            pdf_page = pdf[page_no - 1]
            # 스캔형은 내장 래스터의 네이티브 해상도로 렌더한다 — 업스케일하면
            # OCR 검출이 단어 단위로 조각난다(001 실측, canvas.native_image_dpi 주석).
            canvas = render_pdf_page(pdf_page, page_no, dpi=native_image_dpi(pdf_page))
            lines = []
            for tile in make_tiles(canvas.image):
                result = request_layout_parsing(tile.image)
                lines.extend(restore_coords(result.ocr_lines, tile.y_offset))
            lines = sort_reading_order(dedupe_lines(lines))
        except Exception as exc:
            doc.notes.append(f"{page_no}쪽 OCR 복원 실패: {exc}")
            continue

        # 디지털 텍스트로 이미 확보한 줄은 뺀다 — 남는 건 이미지(스크린샷) 안에만
        # 있던 내용이다. 그게 이 페이지를 OCR 하는 진짜 이유다.
        new_lines = [
            ln for ln in lines
            if ln.text.strip() and re.sub(r"\s+", "", ln.text.strip()) not in seen
        ]
        text = "\n".join(ln.text.strip() for ln in new_lines)
        if not text:
            if not digital_lines:
                doc.notes.append(f"{page_no}쪽 OCR 복원 — 글자 0건 (빈 쪽이거나 판독 실패)")
            continue

        chunk = RagChunk(
            chunk_id=f"{doc.doc_id}_p{page_no:03d}_ocr",
            kind="ocr_page",
            heading=f"[{page_no}쪽 — 이미지 내 텍스트]",
            text=text,
            notes=[
                f"사내 파서가 스캔형으로 건너뛴 쪽에서, 디지털 텍스트로 못 얻은 "
                f"이미지 내용만 OCR 로 복원(전체 {len(lines)}줄 중 {len(new_lines)}줄). "
                "판독이므로 정본 신뢰도가 낮다."
            ],
        )
        _insert_at_anchor(doc, pdf, page_no, chunk)
        doc.notes.append(f"{page_no}쪽 OCR 복원 완료 ({len(new_lines)}줄, {len(text)}자)")


def caption_image(img: Image.Image) -> dict:
    return chat_json(
        [{"type": "text", "text": _CAPTION_PROMPT}, image_part(img)],
        schema_name="image_caption",
        schema=_CAPTION_SCHEMA,
        max_tokens=800,
    )


# ──────────────────────────── 인제스천 ────────────────────────────

def ingest_rag_file(path: Path, asset_dir: Path | None = None) -> RagDocument:
    """기준 문서 1개 → RagDocument. 실패는 notes 에 기록 (조용한 실패 금지)."""
    doc = RagDocument(
        doc_id=path.stem,
        source_file=path.name,
        file_type=path.suffix.lstrip(".").lower(),
    )
    try:
        from document_processor import DocIR

        docir = DocIR.from_file(str(path))
    except Exception as exc:
        doc.notes.append(f"사내 파서 추출 실패: {exc}")
        return doc

    # 1) 텍스트·표 → 문서 순서 블록 → 청크
    blocks: list[tuple[str, str, str | None]] = []
    for para in docir.paragraphs:
        text = _paragraph_text(para).strip()
        if text:
            blocks.append(("text", text, None))
        for node in getattr(para, "content", []) or []:
            if type(node).__name__ == "TableIR":
                rows = _tables_to_lines(node)
                if rows:
                    # 평문은 미리보기·검색용, HTML 은 농협 KL `table` item 용. 둘 다
                    # 남긴다 — 사람이 읽는 화면에 태그를 뿌리지 않으면서 규격도
                    # 만족하려면 필요하다. table_to_html 이 구조를 못 읽으면 None.
                    blocks.append(("table", "\n".join(rows), table_to_html(node)))
    doc.chunks = _chunk_blocks(blocks, doc.doc_id)

    # 1-b) 사내 파서가 조용히 건너뛴 스캔형 PDF 페이지 → 렌더 + OCR 복원
    try:
        _ocr_skipped_pdf_pages(path, docir, doc)
    except Exception as exc:  # 복원 실패가 문서 전체를 죽이지 않게
        doc.notes.append(f"스캔형 페이지 OCR 복원 단계 실패: {exc}")

    # 2) 내장 이미지 → VLM 캡션 청크
    decorative: list[str] = []
    caption_chunks: list[RagChunk] = []
    for name, asset in iter_assets(docir):
        img = decode_asset_image(asset)
        if img is None:
            doc.notes.append(f"이미지 {name}: 바이트 추출/디코딩 실패")
            continue
        if is_decorative(*img.size):
            decorative.append(str(name))
            continue
        if asset_dir:
            asset_dir.mkdir(parents=True, exist_ok=True)
            img.save(asset_dir / f"{name}.png")
        chunk_id = f"{doc.doc_id}_img_{name}"
        try:
            cap = caption_image(img)
            text = (
                f"[이미지 사례 {name}] {cap.get('description', '').strip()}\n"
                f"이미지 내 텍스트: {cap.get('transcription', '').strip()}"
            )
            caption_chunks.append(RagChunk(
                chunk_id=chunk_id, kind="image_caption", text=text, asset_id=str(name),
            ))
        except Exception as exc:
            caption_chunks.append(RagChunk(
                chunk_id=chunk_id, kind="image_caption", asset_id=str(name),
                text=f"[이미지 사례 {name}] (VLM 캡션 실패 — 원본 {img.size[0]}x{img.size[1]}px 보존)",
                notes=[f"VLM 캡션 실패: {exc}"],
            ))

    # 3) 위치 앵커 삽입 — 본문 마커 "[image:<asset키>]" 가 있는 청크 바로 뒤.
    #    역순 삽입으로 같은 청크에 앵커된 이미지들의 원래 순서를 보존한다.
    for chunk in reversed(caption_chunks):
        marker = f"[image:{chunk.asset_id}]"
        idx = next((i for i, c in enumerate(doc.chunks) if marker in c.text), None)
        if idx is None:
            chunk.notes.append("본문에 위치 마커 없음 — 문서 말미 부착")
            doc.chunks.append(chunk)
        else:
            chunk.heading = doc.chunks[idx].heading
            doc.chunks.insert(idx + 1, chunk)

    # 장식 이미지 마커는 본문에서 제거 (구분선 등 — 검색 노이즈 방지)
    for name in decorative:
        marker = f"[image:{name}]"
        for c in doc.chunks:
            if marker in c.text:
                c.text = "\n".join(
                    ln for ln in (l.replace(marker, "").rstrip() for l in c.text.splitlines()) if ln
                )
    if decorative:
        doc.notes.append(f"장식 이미지 {len(decorative)}개 스킵 (크기 필터): {', '.join(decorative)}")
    return doc
