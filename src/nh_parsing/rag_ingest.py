"""rag-data 트랙 [Track A] — 기준 문서(법령·규제·심의사례) → RAG 청크. 설계서 5절.

광고물 트랙(AdPageIR)과 달리 출력은 검색용 청크 목록이다.
- 텍스트·표: document-processor 디지털 추출이 정본
- 내장 이미지: VLM 캡션(전사+설명)으로 텍스트화해 청크에 포함
  (은행연합회 준수사항 HWP 실측: 금리 표기 사례 스크린샷 10장 —
   실질 심의 기준 예시가 이미지 안에 있음, 2026-07-18)

위치 앵커: DocIR 문단 스트림에 이미지 참조 '노드'는 없지만, RunIR 텍스트에
"[image:<asset키>]" 마커가 남는다(2026-07-18 실측) → 캡션 청크를 마커가 있는
청크 바로 뒤에 삽입한다. 마커가 없는 이미지만 문서 말미에 부착.

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
from .hwp_ingest import _paragraph_text, _tables_to_lines


class RagChunk(BaseModel):
    chunk_id: str
    kind: str  # text | table | image_caption
    heading: str | None = None   # 청크가 속한 조항/제목 (출처 표시용)
    text: str
    asset_id: str | None = None  # image_caption 청크의 원본 asset 키
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


def _chunk_blocks(blocks: list[tuple[str, str]], doc_id: str) -> list[RagChunk]:
    """(kind, text) 문서 순서 블록 → 제목 경계·길이 상한 기준 청크."""
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

    for kind, text in blocks:
        if kind == "table":
            flush()
            chunks.append(RagChunk(
                chunk_id=f"{doc_id}_c{len(chunks):03d}",
                kind="table", heading=heading, text=text,
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
    blocks: list[tuple[str, str]] = []
    for para in docir.paragraphs:
        text = _paragraph_text(para).strip()
        if text:
            blocks.append(("text", text))
        for node in getattr(para, "content", []) or []:
            if type(node).__name__ == "TableIR":
                rows = _tables_to_lines(node)
                if rows:
                    blocks.append(("table", "\n".join(rows)))
    doc.chunks = _chunk_blocks(blocks, doc.doc_id)

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
