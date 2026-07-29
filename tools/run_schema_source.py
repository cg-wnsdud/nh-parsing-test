# -*- coding: utf-8 -*-
"""rag-data 심의규정 → 원본 파싱 결과(raw DocIR) JSON. 상품군별 스키마 도출 근거용.

사내 파서 document-processor(`DocIR.from_file`)로 HWP/PDF 를 파싱한다.
 - xlsx 는 파서가 미지원 → 제외 (체크리스트 xlsx 는 openpyxl 별도 트랙).
 - OCR 이 없어서 스캔형 PDF 페이지는 parse_status="skipped"(pdf_scan_like)로 조용히
   스킵된다 → 페이지별 status 를 확인해 _skipped.md 로 반드시 보고한다.
 - HWP 파싱이 실패하면(예: Scripts/DefaultJScript 스트림 부재) kordoc(npx)로 폴백.
   --no-kordoc 로 폴백 비활성화. (kordoc 출력은 마크다운, bbox 없음)

출력 (out/schema_source/):
  <doc_id>.json      — 문서별 원본 파싱 결과(텍스트/표/bbox + 페이지 status).
                       assets 는 메타만 보존(base64 바이트 제외 → 파일 비대화 방지).
                       kordoc 폴백분은 parser="kordoc" + markdown 필드.
  <doc_id>.kordoc.md — kordoc 폴백으로 복구한 문서의 원본 마크다운(읽기용).
  _index.json        — 전체 요약(문서·문단·페이지·스킵·복구·에러 카운트).
  _skipped.md        — 스킵(스캔형)·실패·kordoc복구 문서/페이지 목록.

사용:
  uv run python tools/run_schema_source.py            # nh-data/rag-data 전체
  uv run python tools/run_schema_source.py <파일|폴더> ...
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from document_processor import DocIR

PROJECT = Path(__file__).resolve().parents[1]  # ...\nh-ad-review-poc
DEFAULT_INPUT = PROJECT / "nh-data" / "rag-data"
DEFAULT_OUT = PROJECT / "out" / "schema_source"

PARSE_EXTS = {".hwp", ".hwpx", ".pdf"}      # document-processor 지원 입력
SKIP_EXTS = {".xlsx", ".xls"}               # 파서 미지원 → 명시적 제외


def _asset_meta(asset) -> dict:
    """이미지 asset → 메타만(base64 바이트 제외). 근사 디코드 크기 포함."""
    b64 = getattr(asset, "data_base64", "") or ""
    return {
        "filename": getattr(asset, "filename", None),
        "mime_type": getattr(asset, "mime_type", None),
        "intrinsic_width_px": getattr(asset, "intrinsic_width_px", None),
        "intrinsic_height_px": getattr(asset, "intrinsic_height_px", None),
        "approx_size_bytes": len(b64) * 3 // 4,
    }


def _docir_to_json(docir: DocIR) -> dict:
    """DocIR → JSON-safe dict. assets 바이트만 메타로 치환, 나머지는 그대로."""
    data = docir.model_dump(mode="json", exclude={"assets"})
    data["assets"] = {name: _asset_meta(a) for name, a in docir.assets.items()}
    return data


# ──────────────────────────── kordoc 폴백 ────────────────────────────
# document-processor(hwplib) 가 HWP 를 못 여는 경우 — 예: Scripts/DefaultJScript
# 스트림 부재로 FileNotFoundException — Node.js CLI kordoc(다른 HWP 리더)로 폴백.
# 실측(2508_투자광고...공지.hwp): document-processor 실패 → kordoc 정상 파싱.
# 단, kordoc 출력은 마크다운(텍스트+표)이고 bbox 는 없다 → 스키마 근거 텍스트/표 용도.

def _kordoc_markdown(path: Path, timeout: int = 600) -> dict | None:
    """kordoc(npx) 로 파싱 → {markdown, summary}. 미설치/실패면 None."""
    npx = shutil.which("npx")
    if not npx:
        return None
    try:
        proc = subprocess.run(
            [npx, "-y", "kordoc", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except Exception:
        return None
    body = "\n".join(
        ln for ln in (proc.stdout or "").splitlines()
        if not ln.startswith(("[kordoc]", "npm notice"))
    ).strip()
    if not body:
        return None
    lines = body.splitlines()
    return {
        "markdown": body,
        "summary": {
            "lines": sum(1 for l in lines if l.strip()),
            "chars": len(body),
            "headings": sum(1 for l in lines if l.lstrip().startswith("#")),
            "markdown_table_rows": sum(
                1 for l in lines if l.strip().startswith("|") and l.strip().endswith("|")
            ),
            "html_tables": body.count("<table>"),
            "images": len(re.findall(r"!\[[^\]]*\]\(", body)),
        },
    }


def _page_summary(docir: DocIR) -> dict:
    pages = docir.pages or []
    skipped = [
        {"page_number": p.page_number, "reason": p.parse_skip_reason}
        for p in pages
        if p.parse_status == "skipped"
    ]
    return {
        "total_pages": len(pages),
        "parsed_pages": sum(1 for p in pages if p.parse_status == "parsed"),
        "skipped_pages": len(skipped),
        "skipped_page_detail": skipped,
        "has_page_metadata": bool(pages),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="*", type=Path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-kordoc", action="store_true",
                    help="HWP 파싱 실패 시 kordoc(npx) 폴백을 비활성화")
    args = ap.parse_args()

    # 대상 수집
    targets: list[Path] = []
    excluded: list[Path] = []
    for item in args.inputs or [DEFAULT_INPUT]:
        entries = sorted(item.iterdir()) if item.is_dir() else [item]
        for p in entries:
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext in PARSE_EXTS:
                targets.append(p)
            elif ext in SKIP_EXTS:
                excluded.append(p)

    args.out.mkdir(parents=True, exist_ok=True)

    index: list[dict] = []
    skipped_docs: list[dict] = []      # scan_like 페이지 있는 문서
    failed_docs: list[dict] = []       # 파싱 자체 실패(폴백도 실패)
    recovered_docs: list[dict] = []    # document-processor 실패 → kordoc 로 복구
    empty_docs: list[dict] = []        # 파싱은 됐으나 텍스트 0

    for path in targets:
        print(f"\n{'=' * 72}\n▶ {path.name}")
        start = time.time()
        record = {
            "source_file": path.name,
            "file_type": path.suffix.lstrip(".").lower(),
        }
        try:
            docir = DocIR.from_file(str(path))
        except Exception as exc:  # noqa: BLE001 — 조용한 실패 금지, 전부 기록
            elapsed = time.time() - start
            print(f"  ✗ document-processor 파싱 실패 ({elapsed:.1f}s): {exc}")
            # HWP/HWPX 실패는 kordoc(다른 HWP 리더)로 폴백 시도
            kd = None
            if not args.no_kordoc and record["file_type"] in {"hwp", "hwpx"}:
                print("    ↳ kordoc 폴백 시도 (npx)…")
                kd = _kordoc_markdown(path)
            if kd:
                out_obj = {
                    "source_file": path.name,
                    "file_type": record["file_type"],
                    "parser": "kordoc",
                    "note": ("document-processor(hwplib) 실패 → kordoc 폴백. "
                             "출력은 마크다운(텍스트+표), bbox 없음."),
                    "docir_error": repr(exc),
                    "parse_summary": kd["summary"],
                    "markdown": kd["markdown"],
                }
                out_path = args.out / f"{path.stem}.json"
                out_path.write_text(json.dumps(out_obj, ensure_ascii=False, indent=2), encoding="utf-8")
                (args.out / f"{path.stem}.kordoc.md").write_text(kd["markdown"], encoding="utf-8")
                total = time.time() - start
                s = kd["summary"]
                record.update({"status": "ok_kordoc_fallback", "parser": "kordoc",
                               "out_file": out_path.name, "elapsed_s": round(total, 1), **s})
                index.append(record)
                recovered_docs.append({"file": path.name, "docir_error": repr(exc), "summary": s})
                print(f"    ✓ kordoc 복구 {total:.1f}s 라인={s['lines']} "
                      f"표행={s['markdown_table_rows']} html표={s['html_tables']} "
                      f"헤딩={s['headings']} → {out_path.name} (+.kordoc.md)")
            else:
                record.update({"status": "error", "error": repr(exc), "elapsed_s": round(elapsed, 1)})
                index.append(record)
                failed_docs.append({"file": path.name, "error": repr(exc)})
            continue

        elapsed = time.time() - start
        data = _docir_to_json(docir)
        pg = _page_summary(docir)
        n_para = len(docir.paragraphs)
        n_text = sum(1 for para in docir.paragraphs
                     for node in (getattr(para, "content", []) or [])
                     if getattr(node, "text", "").strip())

        # 페이로드에 요약 헤더 부착
        out_obj = {
            "source_file": path.name,
            "file_type": record["file_type"],
            "source_doc_type": data.get("source_doc_type"),
            "parse_summary": {
                "paragraphs": n_para,
                "assets": len(docir.assets),
                **pg,
            },
            "docir": data,
        }
        out_path = args.out / f"{path.stem}.json"
        out_path.write_text(json.dumps(out_obj, ensure_ascii=False, indent=2), encoding="utf-8")

        record.update({
            "status": "ok",
            "out_file": out_path.name,
            "paragraphs": n_para,
            "assets": len(docir.assets),
            "elapsed_s": round(elapsed, 1),
            **pg,
        })
        index.append(record)

        msg = (f"  ✓ {elapsed:.1f}s 문단={n_para} asset={len(docir.assets)} "
               f"페이지={pg['total_pages']}(parsed={pg['parsed_pages']}/skip={pg['skipped_pages']})")
        print(msg + f" → {out_path.name}")

        if pg["skipped_pages"]:
            skipped_docs.append({"file": path.name, "skipped": pg["skipped_page_detail"],
                                 "total_pages": pg["total_pages"]})
            print(f"    ⚠ 스캔형 스킵 페이지 {pg['skipped_pages']}개 → _skipped.md")
        # 주의: HWP 는 페이지별 scan triage 를 안 해 parse_status=None(=parsed 0).
        # "빈 문서" 판정은 parsed_pages 가 아니라 실제 스킵/내용으로만 한다.
        if pg["total_pages"] and pg["skipped_pages"] == pg["total_pages"]:
            empty_docs.append({"file": path.name, "reason": "all pages skipped (scan_like)"})
        elif n_para == 0 and len(docir.assets) == 0:
            empty_docs.append({"file": path.name, "reason": "no paragraphs / no assets"})

    # _index.json
    (args.out / "_index.json").write_text(
        json.dumps({
            "parsed_ok": sum(1 for r in index if r["status"] == "ok"),
            "recovered_kordoc": len(recovered_docs),
            "failed": len(failed_docs),
            "excluded_unsupported": [p.name for p in excluded],
            "documents": index,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # _skipped.md
    md = ["# 파싱 스킵·실패 보고 (별도 OCR/파서 트랙 필요분)\n",
          "> document-processor 는 OCR 이 없어 스캔형 PDF 페이지를 조용히 스킵합니다.",
          "> xlsx 는 파서 미지원이라 제외했습니다.\n"]
    md.append("## 제외 — 파서 미지원 형식 (xlsx)\n")
    md += ([f"- {p.name}" for p in excluded] or ["- (없음)"])
    md.append("\n## 파싱 실패 (문서 단위 — 폴백도 실패)\n")
    md += ([f"- {d['file']} — {d['error']}" for d in failed_docs] or ["- (없음)"])
    md.append("\n## document-processor 실패 → kordoc 폴백으로 복구됨\n")
    md.append("> kordoc 출력은 마크다운(텍스트+표), bbox 없음. `<stem>.kordoc.md` 동봉.")
    if recovered_docs:
        for d in recovered_docs:
            s = d["summary"]
            md.append(f"- {d['file']} — kordoc 복구 (라인 {s['lines']}, 표행 "
                      f"{s['markdown_table_rows']}+html {s['html_tables']}, 헤딩 {s['headings']})")
            md.append(f"  · document-processor 원인: {d['docir_error']}")
    else:
        md.append("- (없음)")
    md.append("\n## 스캔형 스킵 페이지가 있는 문서\n")
    if skipped_docs:
        for d in skipped_docs:
            pages = ", ".join(str(s["page_number"]) for s in d["skipped"])
            md.append(f"- {d['file']} — {len(d['skipped'])}/{d['total_pages']}p 스킵 (p{pages})")
    else:
        md.append("- (없음 — 스캔형으로 스킵된 페이지 없음)")
    md.append("\n## 내용이 비어있는 문서 (확인 필요)\n")
    md += ([f"- {d['file']} — {d['reason']}" for d in empty_docs] or ["- (없음)"])
    (args.out / "_skipped.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"\n{'=' * 72}\n완료: ok={sum(1 for r in index if r['status']=='ok')} "
          f"kordoc복구={len(recovered_docs)} 실패={len(failed_docs)} "
          f"제외(xlsx)={len(excluded)} 스캔스킵문서={len(skipped_docs)}")
    print(f"출력: {args.out}")


if __name__ == "__main__":
    main()
