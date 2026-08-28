# -*- coding: utf-8 -*-
"""복수 입력 묶음의 광고물 파싱을 중단 복구 가능하게 실행한다.

각 파일을 독립 단위로 처리해 확정 산출물(JSON·쪽 이미지·중간 parse JSON)을 즉시
저장한다. 프로세스·네트워크가 끊겨도 이미 성공한 파일은 다음 실행에서 건너뛴다.
실패·재시도·단계 경고는 모두 결과 폴더의 progress.json / run-events.jsonl 에 남긴다.

    python tools/run_parsing_batch.py --out 0827-parsing
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from PIL import Image  # noqa: E402

from measure_parse_defects import measure_doc  # noqa: E402
from nh_parsing import ad_template as AT  # noqa: E402
from nh_parsing.ad_export import label_parsed_ad, page_canvases  # noqa: E402
from nh_parsing.assets import decode_asset_image, is_decorative, iter_assets  # noqa: E402
from nh_parsing.gemma_client import STATS, reset_stats  # noqa: E402
from nh_parsing.pipeline import process_file  # noqa: E402


SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".hwp", ".hwpx"}
PAGE_MAX_W = 1000
DEFAULT_INPUTS = [
    ("new-sample-data", ROOT / "nh-data" / "new-sample-data"),
    ("sample-data", ROOT / "nh-data" / "sample-data"),
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_stem(value: str, limit: int = 100) -> str:
    """Windows 파일명으로 안전한, 사람이 읽을 수 있는 결과 ID 조각."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value).strip(" .")
    return (cleaned or "unnamed")[:limit]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _parse_input_spec(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--input-root 은 이름=경로 형식이어야 합니다")
    name, raw_path = value.split("=", 1)
    if not name.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("--input-root 은 이름=경로 형식이어야 합니다")
    return name.strip(), Path(raw_path.strip())


def _result_id(index: int, relative_path: Path, previous: dict[str, Any] | None) -> str:
    if previous and isinstance(previous.get("result_id"), str):
        return previous["result_id"]
    # 인덱스와 원래 stem을 같이 넣는다. 확장자만 다른 동명 파일도 절대 충돌하지 않는다.
    digest = hashlib.sha1(relative_path.as_posix().encode("utf-8")).hexdigest()[:8]
    return f"{index:03d}__{_safe_stem(relative_path.stem)}__{digest}"


def _collect_entries(
    input_roots: list[tuple[str, Path]], previous: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    entries: list[dict[str, Any]] = []
    filenames: dict[str, list[str]] = {}
    stems: dict[str, list[str]] = {}
    for group, source_root in input_roots:
        source_root = source_root.resolve()
        if not source_root.is_dir():
            raise SystemExit(f"입력 폴더가 없습니다: {source_root}")
        paths = sorted(
            p for p in source_root.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        )
        for path in paths:
            rel = path.relative_to(source_root)
            key = f"{group}/{rel.as_posix()}"
            old = previous.get(key)
            stat = path.stat()
            entry = {
                "key": key,
                "group": group,
                "source_root": str(source_root),
                "source_path": str(path.resolve()),
                "relative_path": rel.as_posix(),
                "source_file": path.name,
                "extension": path.suffix.lower(),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
                "result_id": _result_id(len(entries) + 1, rel, old),
                "status": (old or {}).get("status", "pending"),
                "attempts": (old or {}).get("attempts", []),
                "warning_count": (old or {}).get("warning_count", 0),
                "quality_warnings": (old or {}).get("quality_warnings", []),
                # 재개 실행이 완료 문서를 건너뛸 때도, 최초 실행에서 얻은 시간·VLM
                # 계측과 저장된 쪽 번호를 잃지 않아야 전수 실행 보고서를 만들 수 있다.
                "metrics": (old or {}).get("metrics"),
                "saved_page_numbers": (old or {}).get("saved_page_numbers", []),
            }
            # 실행 도중 강제 종료됐을 때의 running 은 다음 실행에서 다시 시도한다.
            if entry["status"] == "running":
                entry["status"] = "pending"
            entries.append(entry)
            filenames.setdefault(path.name.casefold(), []).append(key)
            stems.setdefault(path.stem.casefold(), []).append(key)
    duplicates = {
        "same_filename": [values for values in filenames.values() if len(values) > 1],
        "same_stem": [values for values in stems.values() if len(values) > 1],
    }
    return entries, duplicates


class Ledger:
    def __init__(self, out_dir: Path, entries: list[dict[str, Any]], duplicates: dict[str, list[str]],
                 max_attempts: int) -> None:
        self.out_dir = out_dir
        self.entries = entries
        self.duplicates = duplicates
        self.max_attempts = max_attempts
        self.events_path = out_dir / "run-events.jsonl"
        self.log_path = out_dir / "run.log"
        self.started_at = _now()

    def _counts(self) -> dict[str, int]:
        counts = Counter(entry["status"] for entry in self.entries)
        return {
            "total": len(self.entries),
            "succeeded": counts["succeeded"],
            "failed": counts["failed"],
            "pending": counts["pending"],
            "running": counts["running"],
            "paused": counts["paused"],
            "succeeded_with_warnings": sum(
                1 for entry in self.entries
                if entry["status"] == "succeeded" and entry.get("quality_warnings")
            ),
        }

    def save(self) -> None:
        _atomic_json(self.out_dir / "progress.json", {
            "updated_at": _now(),
            "max_attempts_per_invocation": self.max_attempts,
            "counts": self._counts(),
            "entries": self.entries,
        })

    def event(self, kind: str, entry: dict[str, Any] | None = None, **detail: Any) -> None:
        payload: dict[str, Any] = {"at": _now(), "event": kind, **detail}
        if entry is not None:
            payload.update({
                "key": entry["key"], "group": entry["group"],
                "source_file": entry["source_file"], "result_id": entry["result_id"],
            })
        text = json.dumps(payload, ensure_ascii=False)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with self.log_path.open("a", encoding="utf-8") as handle:
            file_label = f" [{entry['key']}]" if entry else ""
            message = detail.get("message") or detail.get("error") or ""
            handle.write(f"{payload['at']} {kind}{file_label} {message}\n")


def _save_pages(canvases: dict[int, Image.Image], pages_dir: Path, result_id: str) -> list[int]:
    saved: list[int] = []
    for pno, canvas in canvases.items():
        image = canvas.copy()
        if image.width > PAGE_MAX_W:
            height = round(image.height * PAGE_MAX_W / image.width)
            image = image.resize((PAGE_MAX_W, height), Image.LANCZOS)
        encoded = io.BytesIO()
        image.convert("RGB").save(encoded, format="JPEG", quality=72)
        _atomic_bytes(pages_dir / f"{result_id}_p{pno}.jpg", encoded.getvalue())
        saved.append(pno)
    return saved


def _hwp_embedded_images(path: Path, doc: dict[str, Any]) -> dict[int, Image.Image]:
    """HWP의 OCR 대상 내장 이미지를 검수용 원본 페이지 이미지로도 남긴다.

    HWP 본문은 현재 파이프라인이 좌표 없는 디지털 텍스트로만 보존한다. 내장 이미지의
    페이지 번호는 ingest_hwp와 같은 규칙(p1 본문 다음 p2부터)을 쓴다.
    """
    visual_pages = {
        page.get("page_no") for page in doc.get("pages") or []
        if page.get("canvas_w") and page.get("canvas_h")
    }
    if not visual_pages:
        return {}
    try:
        from document_processor import DocIR

        docir = DocIR.from_file(str(path))
    except Exception:
        return {}
    canvases: dict[int, Image.Image] = {}
    page_no = 2
    for _name, asset in iter_assets(docir):
        image = decode_asset_image(asset)
        if image is None or is_decorative(*image.size):
            continue
        if page_no in visual_pages:
            canvases[page_no] = image
        page_no += 1
    return canvases


def _pipeline_warnings(doc: dict[str, Any], canvases: dict[int, Image.Image]) -> list[str]:
    warnings: list[str] = []
    for note in doc.get("notes") or []:
        if "실패" in note or "unreadable" in note:
            warnings.append(f"문서: {note}")
    for page in doc.get("pages") or []:
        pno = page.get("page_no")
        if page.get("parse_status") != "ok":
            warnings.append(f"p{pno}: parse_status={page.get('parse_status')}")
        for note in page.get("notes") or []:
            if "실패" in note or "unreadable" in note:
                warnings.append(f"p{pno}: {note}")
        regions = page.get("regions") or []
        has_text = any(region.get("lines") for region in regions)
        has_vlm = any((region.get("vlm_reading") or "").strip() for region in regions)
        if pno in canvases and has_text and not has_vlm:
            warnings.append(f"p{pno}: 텍스트 영역은 있으나 VLM 통독 후보가 0건")
    if doc.get("file_type") in {"hwp", "hwpx"} and 1 not in canvases:
        warnings.append("p1: HWP 본문은 현재 좌표 없는 디지털 추출이라 원본 페이지 렌더링이 없음")
    return warnings


def _result_metrics(unified: dict[str, Any], elapsed_s: float) -> dict[str, Any]:
    pages = unified.get("pages") or []
    regions = [region for page in pages for region in page.get("regions") or []]
    comp = unified.get("completeness") or {}
    vlm_stats = {key: value.copy() for key, value in STATS.items()}
    return {
        "elapsed_s": round(elapsed_s, 3),
        "pages": len(pages),
        "regions": len(regions),
        "lines": comp.get("lines_total", 0),
        "covered_lines": comp.get("lines_covered", 0),
        "template": (unified.get("template") or {}).get("template_id"),
        "labeled_regions": sum(1 for region in regions if region.get("gubun")),
        "vlm_candidate_regions": sum(1 for region in regions if (region.get("vlm_reading") or "").strip()),
        "vlm_calls": int(sum(stat["calls"] for stat in STATS.values())),
        "vlm_cached": int(sum(stat["cached"] for stat in STATS.values())),
        "vlm_s": round(sum(stat["seconds"] for stat in STATS.values()), 3),
        "vlm_by_stage": vlm_stats,
    }


def _run_entry(entry: dict[str, Any], out_dir: Path, pack: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[int]]:
    """파일 하나를 처리한다. parse JSON이 있으면 OCR/VLM 재파싱 없이 이어서 라벨링한다."""
    source = Path(entry["source_path"])
    group_dir = out_dir / entry["group"]
    parse_path = group_dir / "parse" / f"{entry['result_id']}.json"
    unified_path = group_dir / "json" / f"{entry['result_id']}.json"
    pages_dir = group_dir / "pages"
    started = time.monotonic()
    reset_stats()

    if parse_path.is_file():
        doc = json.loads(parse_path.read_text(encoding="utf-8"))
        parse_reused = True
    else:
        doc = json.loads(process_file(source).model_dump_json())
        _atomic_json(parse_path, doc)
        parse_reused = False

    # parse JSON에는 파이프라인 본래 doc_id를 그대로 남긴다. 통합 JSON·쪽 이미지만
    # 배치 고유 ID로 맞춰 여러 입력 묶음/동명 확장자가 절대 충돌하지 않게 한다.
    export_doc = copy.deepcopy(doc)
    export_doc["doc_id"] = entry["result_id"]
    canvases = page_canvases(source)
    if source.suffix.lower() in {".hwp", ".hwpx"}:
        canvases = _hwp_embedded_images(source, doc)
    unified = label_parsed_ad(export_doc, source.name, canvases, pack)
    unified["batch_source"] = {
        "input_group": entry["group"],
        "relative_path": entry["relative_path"],
        "result_id": entry["result_id"],
        "parse_reused": parse_reused,
    }
    _atomic_json(unified_path, unified)
    saved_pages = _save_pages(canvases, pages_dir, entry["result_id"])
    warnings = _pipeline_warnings(doc, canvases)
    metrics = _result_metrics(unified, time.monotonic() - started)
    metrics["parse_reused"] = parse_reused
    return metrics, warnings, saved_pages


def _aggregate_docs(paths: list[Path]) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    defects: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    template_decided = 0
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        pages = doc.get("pages") or []
        regions = [region for page in pages for region in page.get("regions") or []]
        comp = doc.get("completeness") or {}
        totals["documents"] += 1
        totals["pages"] += len(pages)
        totals["regions"] += len(regions)
        totals["lines"] += comp.get("lines_total", 0)
        totals["covered_lines"] += comp.get("lines_covered", 0)
        totals["labeled_regions"] += sum(1 for region in regions if region.get("gubun"))
        totals["vlm_candidate_regions"] += sum(
            1 for region in regions if (region.get("vlm_reading") or "").strip()
        )
        totals["tables"] += sum(1 for region in regions if region.get("table"))
        template_decided += int(bool((doc.get("template") or {}).get("template_id")))
        for page in pages:
            for region in page.get("regions") or []:
                for line in region.get("lines") or []:
                    source_counts[str(line.get("source") or "unknown")] += 1
            for line in page.get("unassigned_lines") or []:
                source_counts[str(line.get("source") or "unknown")] += 1
        try:
            defects.update(measure_doc(doc))
        except Exception as exc:  # 계측 실패가 전체 결과 기록을 막으면 안 된다.
            totals["defect_measure_errors"] += 1
            totals["defect_measure_error_last"] = str(exc)
    totals["template_decided_documents"] = template_decided
    return {"totals": dict(totals), "defects": dict(defects), "line_sources": dict(source_counts)}


def _write_final_reports(out_dir: Path, ledger: Ledger) -> dict[str, Any]:
    success_entries = [entry for entry in ledger.entries if entry["status"] == "succeeded"]
    result_paths = [
        out_dir / entry["group"] / "json" / f"{entry['result_id']}.json"
        for entry in success_entries
    ]
    current = _aggregate_docs([path for path in result_paths if path.is_file()])

    # 기존 전수 결과와는 source_file이 같은 문서만 비교한다. 이번 범위의 HWP 1건은
    # 이전 out_ad_full에 없으므로 공통 표본 수에 넣지 않는다.
    previous_paths = sorted((ROOT / "out_ad_full" / "json").glob("*.json"))
    previous_by_source = {
        json.loads(path.read_text(encoding="utf-8")).get("source_file"): path
        for path in previous_paths
    }
    current_by_source: dict[str, Path] = {}
    for path in result_paths:
        if path.is_file():
            current_by_source[json.loads(path.read_text(encoding="utf-8")).get("source_file")] = path
    common_sources = sorted(set(previous_by_source) & set(current_by_source))
    before = _aggregate_docs([previous_by_source[source] for source in common_sources])
    after = _aggregate_docs([current_by_source[source] for source in common_sources])
    compare_fields = ["documents", "pages", "regions", "lines", "covered_lines", "labeled_regions",
                      "vlm_candidate_regions", "tables", "template_decided_documents"]
    delta = {
        field: after["totals"].get(field, 0) - before["totals"].get(field, 0)
        for field in compare_fields
    }

    failures = [
        {
            "key": entry["key"], "source_file": entry["source_file"],
            "attempts": entry.get("attempts", []),
        }
        for entry in ledger.entries if entry["status"] == "failed"
    ]
    summary = {
        "generated_at": _now(),
        "input": {"counts": ledger._counts(), "duplicates": ledger.duplicates},
        "current_metrics": current,
        "previous_comparison": {
            "baseline": "out_ad_full/json",
            "common_documents": len(common_sources),
            "excluded_from_comparison": sorted(set(current_by_source) - set(common_sources)),
            "before": before,
            "after": after,
            "delta": delta,
            "criteria": [
                "텍스트 보존: lines 및 covered_lines(템플릿 라벨/문구 대조가 닿은 줄)",
                "VLM 통독 적용: vlm_candidate_regions",
                "템플릿 적용: template_decided_documents 및 labeled_regions",
                "구조 결함: empty, back, near, contain, gubun_deep (measure_parse_defects와 같은 정의)",
            ],
        },
        "failures": failures,
    }
    _atomic_json(out_dir / "summary.json", summary)
    _atomic_json(out_dir / "failures.json", failures)

    totals = current["totals"]
    before_totals = before["totals"]
    after_totals = after["totals"]
    lines = [
        "# 0827 파싱 실행 요약",
        "",
        f"- 입력: {ledger._counts()['total']}개 / 성공: {ledger._counts()['succeeded']}개 / 실패: {ledger._counts()['failed']}개",
        f"- 결과: 페이지 {totals.get('pages', 0)} / 영역 {totals.get('regions', 0)} / 줄 {totals.get('lines', 0)}",
        f"- 템플릿 결정 문서: {totals.get('template_decided_documents', 0)} / VLM 통독 후보 영역: {totals.get('vlm_candidate_regions', 0)}",
        f"- 이전 out_ad_full과 공통 문서: {len(common_sources)}개 (HWP 등 새 결과만 있는 문서는 비교에서 제외)",
        "",
        "## 공통 문서 전후 핵심 지표",
        "",
        "| 지표 | 이전 | 현재 | 변화 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for field in compare_fields:
        lines.append(f"| {field} | {before_totals.get(field, 0)} | {after_totals.get(field, 0)} | {delta[field]:+d} |")
    lines.extend(["", "## 계측 기준", ""])
    lines.extend(f"- {criterion}" for criterion in summary["previous_comparison"]["criteria"])
    if failures:
        lines.extend(["", "## 최종 실패", ""])
        lines.extend(f"- {item['key']}" for item in failures)
    else:
        lines.extend(["", "## 최종 실패", "", "- 없음"])
    (out_dir / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "0827-parsing")
    ap.add_argument("--input-root", action="append", type=_parse_input_spec, default=[],
                    help="이름=입력폴더. 생략하면 new-sample-data와 sample-data를 사용")
    ap.add_argument("--max-attempts", type=int, default=2, help="파일 하나의 이번 실행 내 최대 시도 횟수")
    ap.add_argument("--retry-delay", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true", help="대상 목록·충돌 검사·기록 파일만 만든다")
    args = ap.parse_args()
    if args.max_attempts < 1:
        raise SystemExit("--max-attempts 는 1 이상이어야 합니다")

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    old_progress_path = out_dir / "progress.json"
    old_entries: dict[str, dict[str, Any]] = {}
    if old_progress_path.is_file():
        old_entries = {
            item["key"]: item for item in json.loads(old_progress_path.read_text(encoding="utf-8")).get("entries", [])
            if isinstance(item, dict) and item.get("key")
        }
    inputs = args.input_root or DEFAULT_INPUTS
    entries, duplicates = _collect_entries(inputs, old_entries)
    ledger = Ledger(out_dir, entries, duplicates, args.max_attempts)
    _atomic_json(out_dir / "manifest.json", {
        "generated_at": _now(),
        "input_roots": [{"name": name, "path": str(path.resolve())} for name, path in inputs],
        "supported_extensions": sorted(SUPPORTED_EXTS),
        "target_count": len(entries),
        "duplicates": duplicates,
        "files": [{key: entry[key] for key in (
            "key", "group", "source_path", "relative_path", "source_file", "extension", "size_bytes",
            "modified_at", "result_id",
        )} for entry in entries],
    })
    ledger.event("preflight_complete", message=(
        f"대상 {len(entries)}개, 같은 파일명 {len(duplicates['same_filename'])}건, "
        f"동일 stem {len(duplicates['same_stem'])}건"
    ))
    ledger.save()
    print(f"대상 {len(entries)}개 / 같은 파일명 충돌 {len(duplicates['same_filename'])}건 / "
          f"동일 stem 충돌 {len(duplicates['same_stem'])}건")
    if args.dry_run:
        print(f"사전 점검 기록 완료: {out_dir}")
        return

    pack = AT.load_pack()
    missing = AT.check_ids(pack)
    if missing:
        raise SystemExit(f"판정표가 사전에 없는 템플릿을 가리킨다: {missing}")

    for ordinal, entry in enumerate(entries, start=1):
        unified_path = out_dir / entry["group"] / "json" / f"{entry['result_id']}.json"
        if entry["status"] == "succeeded" and unified_path.is_file():
            ledger.event("skipped_completed", entry, message="확정 통합 JSON이 있어 재실행하지 않음")
            continue
        if entry["status"] == "succeeded":
            entry["status"] = "pending"
        print(f"\n[{ordinal}/{len(entries)}] {entry['key']}", flush=True)
        for attempt_no in range(1, args.max_attempts + 1):
            entry["status"] = "running"
            started_at = _now()
            ledger.event("attempt_started", entry, attempt=attempt_no)
            ledger.save()
            try:
                metrics, warnings, saved_pages = _run_entry(entry, out_dir, pack)
            except Exception as exc:  # noqa: BLE001
                attempt = {
                    "attempt": attempt_no, "started_at": started_at, "finished_at": _now(),
                    "outcome": "failed", "error_type": type(exc).__name__, "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
                entry["attempts"].append(attempt)
                ledger.event("attempt_failed", entry, attempt=attempt_no,
                             error=f"{type(exc).__name__}: {exc}")
                ledger.save()
                print(f"  실패 {type(exc).__name__}: {exc}", flush=True)
                if attempt_no < args.max_attempts:
                    ledger.event("retry_scheduled", entry, attempt=attempt_no + 1,
                                 message=f"{args.retry_delay:.0f}초 후 재시도")
                    time.sleep(args.retry_delay)
                    continue
                entry["status"] = "failed"
                ledger.event("file_failed", entry, message="이번 실행의 재시도 횟수를 모두 사용")
                ledger.save()
                break
            else:
                entry["status"] = "succeeded"
                entry["quality_warnings"] = warnings
                entry["warning_count"] = len(warnings)
                entry["metrics"] = metrics
                entry["saved_page_numbers"] = saved_pages
                entry["attempts"].append({
                    "attempt": attempt_no, "started_at": started_at, "finished_at": _now(),
                    "outcome": "succeeded", "metrics": metrics, "warnings": warnings,
                })
                ledger.event("file_succeeded", entry, attempt=attempt_no,
                             message=f"{metrics['elapsed_s']:.1f}초, 경고 {len(warnings)}건")
                ledger.save()
                print(f"  성공 {metrics['elapsed_s']:.1f}초 / 줄 {metrics['lines']} / "
                      f"영역 {metrics['regions']} / 경고 {len(warnings)}건", flush=True)
                break

    summary = _write_final_reports(out_dir, ledger)
    ledger.event("batch_finished", message=(
        f"성공 {ledger._counts()['succeeded']} / 실패 {ledger._counts()['failed']} / "
        f"줄 {summary['current_metrics']['totals'].get('lines', 0)}"
    ))
    ledger.save()
    print(f"\n완료: 성공 {ledger._counts()['succeeded']} / 실패 {ledger._counts()['failed']}")


if __name__ == "__main__":
    main()
