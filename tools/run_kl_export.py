# -*- coding: utf-8 -*-
"""규정문서 RAG 청크 → 농협 KL 출력 규격 3종 (모델 호출 0회).

사용:
  uv run python tools/run_kl_export.py <rag 산출물 폴더> --out <출력 폴더>
  예) python tools/run_kl_export.py prototype/rag --out prototype/kl_out

`<rag 폴더>/*.json` 을 읽어 문서마다 다음을 만든다:
  <원본파일명>_hrc.jsonl / <원본파일명>_hrc.json / <원본파일명>_img.zip

이미지는 `<rag 폴더>/assets/<원본파일 stem>/` 에서 찾는다(run_ragdata.py 의 저장 위치).
"""

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from nh_parsing.kl_export import export_kl_files

RAG_SOURCE_DIRS = (
    Path("nh-data/rag-data"),
    Path("nh-data"),
)


def _find_source(source_file: str) -> Path | None:
    name = Path(source_file).name
    for d in RAG_SOURCE_DIRS:
        p = d / name
        if p.is_file():
            return p
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path, help="run_ragdata.py 산출물 폴더")
    ap.add_argument("--out", type=Path, required=True, help="KL 규격 산출물 폴더")
    args = ap.parse_args()

    docs = sorted(p for p in args.src.glob("*.json"))
    if not docs:
        print(f"— {args.src} 에 *.json 이 없다. 먼저 tools/run_ragdata.py 로 청크를 만들 것.")
        return

    total_items = 0
    for path in docs:
        rag_doc = json.loads(path.read_text(encoding="utf-8"))
        source_file = rag_doc.get("source_file") or rag_doc.get("doc_id") or path.stem
        asset_dir = args.src / "assets" / Path(source_file).stem
        result = export_kl_files(
            rag_doc,
            args.out,
            source_path=_find_source(source_file),
            asset_dir=asset_dir,
        )
        total_items += result.item_count

        print(f"\n{'=' * 76}\n▶ {Path(source_file).name}")
        kinds = ", ".join(f"{k}={v}" for k, v in sorted(result.item_kinds.items()))
        print(f"  item {result.item_count}개: {kinds}")
        print(f"  → {result.jsonl_path.name}")
        print(f"  → {result.info_path.name}")
        if result.img_zip_path:
            print(f"  → {result.img_zip_path.name} (이미지 {len(result.images)}개)")
        else:
            print("  → 이미지 없음 (_img.zip 미생성)")
        for note in result.notes:
            print(f"  note: {note}")

    print(f"\n완료. 문서 {len(docs)}건 · item 합계 {total_items}개 → {args.out}")


if __name__ == "__main__":
    main()
