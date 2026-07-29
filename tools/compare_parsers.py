# -*- coding: utf-8 -*-
"""파서 비교 하네스 — 설계서 8.2/8.3 의 '디지털 추출 트랙' 파서 선택 근거 실측.

비교 대상 (어댑터):
  docproc     사내 document-processor (DocIR) — 현재 채택 파서
  kordoc      chrisryugj/kordoc (Node.js CLI, npx) — HWP 폴백 후보
  pdfium_text pypdfium2 텍스트 레이어 (PDF 전용 베이스라인)
  (참고) nh_pipeline — 현재 OCR/VLM 파이프라인 결과(out/json)의 커버리지.
         디지털 파서와의 비교 기준선으로만 표기.

지표 (골드셋 gold/*.yaml 재사용):
  - must_contain 문장 커버리지: 정규화 부분일치 (evaluate.norm 재사용)
  - 골드 필드값 커버리지: 값이 추출 텍스트 안에 존재하는가
  - 구조 신호: 라인/문자 수, U+FFFD, 표 수, 이미지 수, bbox 제공 여부, 처리 시간

주의: 이 하네스는 '디지털 추출' 품질만 잰다. PNG/스캔형 PDF 는 어떤 디지털
파서로도 커버가 안 되는 것이 정상이며, 그 갭이 곧 OCR/VLM 트랙의 존재 이유다.

사용: uv run python tools/compare_parsers.py [--input nh-data경로] [--only 부분명]
출력: 콘솔 + out/parser_comparison.md
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from evaluate import norm  # 채점기와 동일한 정규화 규칙 공유

DEFAULT_INPUT = Path(
    r"c:\Users\cccjj\cginside\repo-analysis\paddle-gemma-orchestrator\nh-data"
)


@dataclass
class ParseOutcome:
    parser: str
    ok: bool
    elapsed: float = 0.0
    text: str = ""
    n_lines: int = 0
    n_tables: int = 0
    n_images: int = 0
    has_bbox: bool = False
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def n_chars(self) -> int:
        return len(re.sub(r"\s", "", self.text))

    @property
    def n_fffd(self) -> int:
        return self.text.count("�")


# ──────────────────────────── 어댑터 ────────────────────────────

def parse_docproc(path: Path) -> ParseOutcome:
    out = ParseOutcome(parser="docproc", ok=False)
    start = time.time()
    try:
        from document_processor import DocIR

        from nh_parsing.hwp_ingest import _paragraph_text, _tables_to_lines

        docir = DocIR.from_file(str(path))
        lines: list[str] = []
        n_tables = 0
        has_bbox = False
        for para in docir.paragraphs:
            text = _paragraph_text(para).strip()
            if text:
                lines.append(text)
            if getattr(para, "bbox", None):
                has_bbox = True
            for node in getattr(para, "content", []) or []:
                if type(node).__name__ == "TableIR":
                    n_tables += 1
                    lines.extend(_tables_to_lines(node))
        assets = getattr(docir, "assets", {}) or {}
        out.ok = True
        out.text = "\n".join(lines)
        out.n_lines = len(lines)
        out.n_tables = n_tables
        out.n_images = len(assets)
        out.has_bbox = has_bbox
        if not lines:
            out.notes.append("추출 결과 비어있음 (SCAN_LIKE 조용한 스킵 가능성)")
    except Exception as exc:
        out.error = str(exc)[:200]
    out.elapsed = time.time() - start
    return out


def parse_kordoc(path: Path, extra_args: list[str] | None = None, name: str = "kordoc") -> ParseOutcome:
    out = ParseOutcome(parser=name, ok=False)
    npx = shutil.which("npx")
    if not npx:
        out.error = "npx 미설치 (Node.js 필요)"
        return out
    start = time.time()
    try:
        proc = subprocess.run(
            [npx, "-y", "kordoc", *(extra_args or []), str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600,
        )
        raw = proc.stdout or ""
        body = "\n".join(
            ln for ln in raw.splitlines()
            if not ln.startswith(("[kordoc]", "npm notice"))
        ).strip()
        if proc.returncode != 0 and not body:
            out.error = (proc.stderr or "kordoc 비정상 종료")[:200]
        else:
            out.ok = True
            out.n_images = len(re.findall(r"!\[[^\]]*\]\(", body))
            out.n_tables = body.count("<table>") + len(
                re.findall(r"^\|.+\|$", body, re.MULTILINE)
            ) // 3  # 마크다운 표는 대략 행수/3 로 근사
            # 텍스트만 남기기: HTML 태그·이미지 마커 제거 후 라인화
            text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
            text = re.sub(r"<br\s*/?>", "\n", text)
            text = re.sub(r"<[^>]+>", "\n", text)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            out.text = "\n".join(lines)
            out.n_lines = len(lines)
    except subprocess.TimeoutExpired:
        out.error = "타임아웃"
    except Exception as exc:
        out.error = str(exc)[:200]
    out.elapsed = time.time() - start
    return out


def parse_kordoc_ocr(path: Path) -> ParseOutcome:
    """kordoc --ocr — 내장 PP-OCRv5 korean (스캔/이미지 PDF 페이지만 OCR).

    첫 실행 시 모델 ~18MB 자동 다운로드 → 폐쇄망에서는 사전 반입 필요.
    """
    return parse_kordoc(path, extra_args=["--ocr"], name="kordoc_ocr")


def parse_pdfium_text(path: Path) -> ParseOutcome:
    out = ParseOutcome(parser="pdfium_text", ok=False)
    if path.suffix.lower() != ".pdf":
        out.error = "PDF 전용 베이스라인"
        return out
    start = time.time()
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(path)
        pages_text = []
        for page in pdf:
            tp = page.get_textpage()
            pages_text.append(tp.get_text_bounded() or "")
        text = "\n".join(pages_text)
        pdf.close()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        out.ok = True
        out.text = "\n".join(lines)
        out.n_lines = len(lines)
        out.has_bbox = True  # charbox 로 라인 bbox 산출 가능 (triage.extract_digital_lines)
    except Exception as exc:
        out.error = str(exc)[:200]
    out.elapsed = time.time() - start
    return out


def coverage_pipeline_reference(stem: str) -> str | None:
    """현재 OCR/VLM 파이프라인 결과(out/json)의 텍스트 — 참고 기준선."""
    jf = ROOT / "out" / "json" / f"{stem}.json"
    if not jf.exists():
        return None
    parsed = json.loads(jf.read_text(encoding="utf-8"))
    lines: list[str] = []
    for pg in parsed.get("pages", []):
        for r in pg.get("regions", []):
            lines.extend(l["text"] for l in r.get("lines", []))
        lines.extend(l["text"] for l in pg.get("unassigned_lines", []))
    return "\n".join(lines)


ADAPTERS = {
    "docproc": (parse_docproc, {".hwp", ".hwpx", ".pdf"}),
    "kordoc": (parse_kordoc, {".hwp", ".hwpx", ".pdf", ".docx", ".xlsx"}),
    "kordoc_ocr": (parse_kordoc_ocr, {".pdf"}),
    "pdfium_text": (parse_pdfium_text, {".pdf"}),
}


# ──────────────────────────── 골드 대조 ────────────────────────────

def load_gold(stem: str) -> tuple[list[str], list[str]]:
    """(must_contain 문장들, 필드값들) — 전 페이지 합산."""
    gf = ROOT / "gold" / f"{stem}.yaml"
    if not gf.exists():
        return [], []
    gold = yaml.safe_load(gf.read_text(encoding="utf-8"))
    sents: list[str] = []
    fields: list[str] = []
    for pg in gold.get("pages", []):
        for s in pg.get("must_contain", []) or []:
            if isinstance(s, str):
                sents.append(s)
        for f in pg.get("fields", []) or []:
            fields.append(str(f["value"]))
    return sents, fields


def covered(items: list[str], text: str) -> tuple[int, list[str]]:
    joined = norm(text)
    misses = [it for it in items if norm(it) not in joined]
    return len(items) - len(misses), misses


# ──────────────────────────── 실행/리포트 ────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--only", default=None)
    args = parser.parse_args()

    files = sorted(
        p for p in args.input.iterdir()
        if p.suffix.lower() in {".pdf", ".png", ".jpg", ".hwp", ".hwpx"}
    )
    if args.only:
        files = [p for p in files if args.only in p.name]

    report = [
        "# 파서 비교 리포트 (디지털 추출 트랙)",
        "",
        "설계서 8.2/8.3 — document-processor vs kordoc 실측. "
        "골드셋 must_contain/필드값 커버리지 기준.",
        "",
        "| 파일 | 파서 | 상태 | 시간(s) | 라인 | 문자 | U+FFFD | 표 | 이미지 | 문장 커버 | 필드값 커버 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    detail: list[str] = []

    for path in files:
        sents, fvals = load_gold(path.stem)
        print(f"\n===== {path.name}  (골드 문장 {len(sents)}, 필드값 {len(fvals)})")
        ext = path.suffix.lower()

        rows: list[tuple[str, ParseOutcome, int, list[str], int]] = []
        for name, (fn, exts) in ADAPTERS.items():
            if ext not in exts:
                report.append(f"| {path.stem[:32]} | {name} | 미지원({ext}) | | | | | | | | |")
                print(f"  {name:12s} 미지원 확장자 {ext}")
                continue
            outcome = fn(path)
            s_cov, s_miss = covered(sents, outcome.text) if outcome.ok else (0, sents)
            f_cov, _ = covered(fvals, outcome.text) if outcome.ok else (0, fvals)
            rows.append((name, outcome, s_cov, s_miss, f_cov))
            status = "OK" if outcome.ok else f"실패: {outcome.error}"
            print(
                f"  {name:12s} {status:20s} {outcome.elapsed:5.1f}s "
                f"라인 {outcome.n_lines:4d} 표 {outcome.n_tables:2d} 이미지 {outcome.n_images:2d} "
                f"문장 {s_cov}/{len(sents)} 필드값 {f_cov}/{len(fvals)}"
            )
            report.append(
                f"| {path.stem[:32]} | {name} | {'OK' if outcome.ok else '실패'} "
                f"| {outcome.elapsed:.1f} | {outcome.n_lines} | {outcome.n_chars} "
                f"| {outcome.n_fffd} | {outcome.n_tables} | {outcome.n_images} "
                f"| {s_cov}/{len(sents)} | {f_cov}/{len(fvals)} |"
            )
            for note in outcome.notes:
                detail.append(f"- {path.stem} · {name}: {note}")

        # 참고: 현재 OCR/VLM 파이프라인 커버리지
        ref_text = coverage_pipeline_reference(path.stem)
        if ref_text is not None and sents:
            s_cov, _ = covered(sents, ref_text)
            f_cov, _ = covered(fvals, ref_text)
            print(f"  {'(참고)OCR/VLM':12s} {'out/json 기준':20s}        "
                  f"문장 {s_cov}/{len(sents)} 필드값 {f_cov}/{len(fvals)}")
            report.append(
                f"| {path.stem[:32]} | (참고) 현행 OCR/VLM | — | — | — | — | — | — | — "
                f"| {s_cov}/{len(sents)} | {f_cov}/{len(fvals)} |"
            )

        # 미커버 문장 상세 (디지털 파서 간 차이 확인용)
        for name, outcome, s_cov, s_miss, _ in rows:
            if outcome.ok and s_miss and len(s_miss) < len(sents):
                detail.append(f"\n### {path.stem} · {name} 미커버 문장 ({len(s_miss)})")
                detail.extend(f"- {m[:70]}" for m in s_miss)

    report += [
        "",
        "## 정적 제약 매트릭스 (실측+공식 문서 기준)",
        "",
        "| 항목 | document-processor | kordoc |",
        "| --- | --- | --- |",
        "| 런타임 | Python 3.13 + Java(ODL/hwplib) (+.NET8 DOC) | Node.js (npx 단일 CLI) |",
        "| 폐쇄망 반입 | 사내 repo — 통제 용이 | npm 오프라인 패키징 필요, 외부 OSS 보안 검토 필요 |",
        "| 출력 구조 | DocIR(pydantic): 문단/표/asset 객체 + PDF bbox | Markdown/HTML 문자열 (bbox 없음) |",
        "| PDF bbox | 있음 (F-013 하이라이트에 필수) | 없음 |",
        "| HWP 표 | TableIR (병합셀 반복 → 후처리 필요) | HTML colspan/rowspan 보존, 중첩표 지원 |",
        "| HWP 이미지 | assets dict (BinData) + 본문 [image:키] 마커 | ![image](...) 마커 (파일 덤프) |",
        "| OCR | 없음 (SCAN_LIKE 조용한 스킵) | 없음 |",
        "| 이미지 파일 입력 | 불가 | 불가 |",
        "| 유지보수 | 사내 — 수정 기여 가능 | 외부 1인 OSS |",
        "",
        "## 상세 노트",
    ] + detail

    out = ROOT / "out" / "parser_comparison.md"
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"\n리포트: {out}")


if __name__ == "__main__":
    main()
