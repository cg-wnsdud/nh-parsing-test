# -*- coding: utf-8 -*-
"""스캔형 PDF 페이지 OCR 복원 (rag_ingest._ocr_skipped_pdf_pages).

실측 근거: `(2025년)대출성 상품 광고시 준수사항_은행연합회.pdf` 6쪽 중 3쪽이
document-processor 에서 `pdf_scan_like` 로 조용히 스킵돼 규정 원문 한 쪽이
RAG 인덱스에 없었다(2026-08-12). 여기서는 폐쇄망 없이 돌도록 렌더·OCR 을
monkeypatch 하고, **끼워 넣는 위치**와 **실패가 조용히 묻히지 않는지**만 본다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nh_parsing import rag_ingest  # noqa: E402
from nh_parsing.ir import Line  # noqa: E402


def _docir(skipped: list[int], total: int = 4):
    pages = [
        SimpleNamespace(
            page_number=n,
            parse_status="skipped" if n in skipped else "parsed",
            parse_skip_reason="pdf_scan_like" if n in skipped else None,
        )
        for n in range(1, total + 1)
    ]
    return SimpleNamespace(pages=pages)


def _doc(texts: list[str]) -> rag_ingest.RagDocument:
    return rag_ingest.RagDocument(
        doc_id="D", source_file="D.pdf", file_type="pdf",
        chunks=[
            rag_ingest.RagChunk(chunk_id=f"D_c{i:03d}", kind="text", text=t)
            for i, t in enumerate(texts)
        ],
    )


@pytest.fixture
def patched(monkeypatch):
    """렌더·타일·OCR 을 전부 가짜로 — 네트워크·PDF 파일 없이 로직만 본다."""
    monkeypatch.setattr(
        "pypdfium2.PdfDocument", lambda _p: ["page0", "page1", "page2", "page3"]
    )
    monkeypatch.setattr("nh_parsing.canvas.native_image_dpi", lambda _p: 150)
    monkeypatch.setattr(
        "nh_parsing.canvas.render_pdf_page",
        lambda _p, n, dpi=None: SimpleNamespace(image=object(), page_no=n),
    )
    monkeypatch.setattr(
        "nh_parsing.tiling.make_tiles",
        lambda _img: [SimpleNamespace(image=object(), y_offset=0)],
    )
    monkeypatch.setattr(
        "nh_parsing.paddlex_client.request_layout_parsing",
        lambda _img: SimpleNamespace(
            ocr_lines=[Line(text="2. 예시", bbox=[0, 0, 10, 10], source="ocr")]
        ),
    )


def test_스킵페이지가_다음쪽_앵커_앞에_끼워진다(monkeypatch, patched):
    # 3쪽이 스킵 → 4쪽의 첫 글자('제2장')로 시작하는 청크 **앞**에 들어가야 한다
    monkeypatch.setattr(rag_ingest, "_page_lead_text", lambda _pdf, _i: "제2장대출비교")
    doc = _doc(["1. 준수사항 가.", "나. 최고금리는", "제2장 대출비교플랫폼"])

    rag_ingest._ocr_skipped_pdf_pages(Path("x.pdf"), _docir([3]), doc)

    kinds = [c.kind for c in doc.chunks]
    assert kinds == ["text", "text", "ocr_page", "text"], kinds
    restored = doc.chunks[2]
    assert restored.chunk_id == "D_p003_ocr"
    assert restored.heading == "[3쪽 — OCR 복원]"
    assert "2. 예시" in restored.text
    # 판독본임을 하류가 알 수 있어야 한다 (정본 아님)
    assert any("OCR" in n for n in restored.notes)
    assert any("3쪽 OCR 복원 완료" in n for n in doc.notes)


def test_앵커를_못찾으면_말미에_붙이고_이유를_남긴다(monkeypatch, patched):
    monkeypatch.setattr(rag_ingest, "_page_lead_text", lambda _pdf, _i: "어디에도없는글자")
    doc = _doc(["가", "나"])

    rag_ingest._ocr_skipped_pdf_pages(Path("x.pdf"), _docir([3]), doc)

    assert doc.chunks[-1].kind == "ocr_page"
    assert any("앵커를 못 찾음" in n for n in doc.chunks[-1].notes)


def test_스킵페이지가_없으면_아무것도_안한다(patched):
    doc = _doc(["가", "나"])
    rag_ingest._ocr_skipped_pdf_pages(Path("x.pdf"), _docir([]), doc)
    assert [c.kind for c in doc.chunks] == ["text", "text"]
    assert doc.notes == []


def test_OCR_실패는_조용히_묻히지_않는다(monkeypatch, patched):
    def boom(_img):
        raise RuntimeError("PaddleX 503")

    monkeypatch.setattr("nh_parsing.paddlex_client.request_layout_parsing", boom)
    doc = _doc(["가"])

    rag_ingest._ocr_skipped_pdf_pages(Path("x.pdf"), _docir([3]), doc)

    assert [c.kind for c in doc.chunks] == ["text"]          # 청크는 안 늘고
    assert any("3쪽 OCR 복원 실패" in n for n in doc.notes)   # 이유는 남는다
    assert any("PaddleX 503" in n for n in doc.notes)


def test_글자를_한_건도_못_읽으면_빈_청크를_안_만든다(monkeypatch, patched):
    monkeypatch.setattr(
        "nh_parsing.paddlex_client.request_layout_parsing",
        lambda _img: SimpleNamespace(ocr_lines=[]),
    )
    doc = _doc(["가"])

    rag_ingest._ocr_skipped_pdf_pages(Path("x.pdf"), _docir([3]), doc)

    assert [c.kind for c in doc.chunks] == ["text"]
    assert any("글자 0건" in n for n in doc.notes)
