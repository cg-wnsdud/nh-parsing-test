# -*- coding: utf-8 -*-
"""rag-data 인제스천 러너 — 기준 문서(HWP/PDF 등) → RAG 청크 JSON + 검토용 MD.

사용:
  uv run python tools/run_ragdata.py <파일|폴더> [...]
  (인자 없으면 nh-data 의 은행연합회 준수사항 HWP 1개)

출력:
  out/rag/<doc_id>.json   — RagDocument (청크 목록)
  out/rag/<doc_id>.md     — 사람 검토용 미리보기
  out/rag/assets/<doc_id>/*.png — 캡션 근거 원본 이미지
"""

import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nh_parsing.rag_ingest import RagDocument, ingest_rag_file

DEFAULT_INPUT = Path(
    r"c:\Users\cccjj\cginside\repo-analysis\paddle-gemma-orchestrator\nh-data"
    r"\(2023년)예금성상품 광고시 준수사항_은행연합회.hwp"
)
RAG_EXTS = {".hwp", ".hwpx", ".pdf"}


def _to_markdown(doc: RagDocument) -> str:
    lines = [f"# {doc.source_file} — RAG 청크 미리보기\n"]
    for note in doc.notes:
        lines.append(f"> note: {note}")
    for chunk in doc.chunks:
        head = f" · {chunk.heading}" if chunk.heading else ""
        lines.append(f"\n## {chunk.chunk_id} [{chunk.kind}]{head}\n")
        lines.append(chunk.text)
        for note in chunk.notes:
            lines.append(f"> note: {note}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="*", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent.parent / "out" / "rag")
    args = parser.parse_args()

    targets: list[Path] = []
    for item in args.inputs or [DEFAULT_INPUT]:
        if item.is_dir():
            targets.extend(sorted(p for p in item.iterdir() if p.suffix.lower() in RAG_EXTS))
        else:
            targets.append(item)

    args.out.mkdir(parents=True, exist_ok=True)
    for path in targets:
        print(f"\n{'=' * 72}\n▶ {path.name}")
        start = time.time()
        doc = ingest_rag_file(path, asset_dir=args.out / "assets" / path.stem)
        elapsed = time.time() - start

        (args.out / f"{doc.doc_id}.json").write_text(
            doc.model_dump_json(indent=2), encoding="utf-8"
        )
        (args.out / f"{doc.doc_id}.md").write_text(_to_markdown(doc), encoding="utf-8")

        kinds: dict[str, int] = {}
        for chunk in doc.chunks:
            kinds[chunk.kind] = kinds.get(chunk.kind, 0) + 1
        print(f"  청크 {len(doc.chunks)}개: " + ", ".join(f"{k}={v}" for k, v in kinds.items()))
        for chunk in doc.chunks:
            if chunk.kind == "image_caption":
                first = chunk.text.splitlines()[0][:80]
                print(f"    [{chunk.chunk_id}] {first}")
                for note in chunk.notes:
                    print(f"      note: {note}")
        for note in doc.notes:
            print(f"  note: {note}")
        print(f"  {elapsed:.1f}s → {doc.doc_id}.json / .md")

    print("\n완료.")


if __name__ == "__main__":
    main()
