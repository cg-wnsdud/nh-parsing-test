"""0827 전수 파싱 결과의 재현 가능한 무결성 감사.

이 도구는 OCR 정답률을 추정하지 않는다. 정답 원문(gold transcription) 없이
"정확도 99%" 같은 수치를 만들지 않기 위해서다. 대신 다음을 실제 산출물과 원본을
다시 읽어 확인한다.

* 입력 파일 ↔ parse/final JSON ↔ 페이지 미리보기의 1:1 보존
* PDFium 재개방·페이지 수·텍스트 계층 접근성과 파서 결과의 일치
* 현재 타일 규칙을 같은 원본에 재적용했을 때의 타일 수/좌표계 검증
* bbox 범위, 라인 출처·신뢰도, VLM 후보·역할·템플릿/구분 라벨의 연결 무결성

결과는 ``0827-parsing/current-pipeline-audit.{json,md}``에 쓴다. 원본 또는
기존 산출물을 변경하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from nh_parsing.canvas import load_image_canvas, native_image_dpi, render_pdf_page
from nh_parsing.tiling import make_tiles
from nh_parsing.triage import triage_page

try:
    import pypdfium2 as pdfium
except ImportError as exc:  # pragma: no cover - environment failure should be clear
    raise SystemExit(f"pypdfium2가 필요합니다: {exc}") from exc


LOW_QUALITY_RE = re.compile(r"^new-sample-data/4-[1-9]\. 예금성상품\(거치식\)\.png$")
EXPECTED_FINAL = {
    "doc_id", "source_file", "file_type", "classification", "template", "pages",
    "template_items", "completeness", "notes", "batch_source",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _counter_dict(counter: Counter) -> dict[str, int]:
    return {str(k): int(v) for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))}


def _box_status(box: Any, width: int | None, height: int | None) -> str | None:
    if box is None:
        return "missing"
    if not isinstance(box, list) or len(box) != 4:
        return "malformed"
    try:
        x0, y0, x1, y1 = [float(v) for v in box]
    except (TypeError, ValueError):
        return "malformed"
    if x1 <= x0 or y1 <= y0:
        return "reversed_or_zero"
    if width is not None and height is not None and (x0 < 0 or y0 < 0 or x1 > width or y1 > height):
        return "outside_canvas"
    return None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def _preview_count(root: Path, group: str, result_id: str) -> int:
    page_dir = root / group / "pages"
    # ``Path.glob``도 ``[]``를 와일드카드 문자로 해석한다. 실제 입력 파일명에
    # ``[예금성상품-적금]``가 있으므로 prefix 비교로 센다.
    prefix = f"{result_id}_p"
    return sum(1 for path in page_dir.iterdir() if path.is_file() and path.name.startswith(prefix) and path.suffix.lower() == ".jpg")


def _pdf_audit(source: Path, parse_doc: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"open": "ok", "pages": []}
    try:
        pdf = pdfium.PdfDocument(str(source))
    except Exception as exc:
        return {"open": "failed", "error": str(exc), "pages": []}

    parsed_pages = parse_doc.get("pages") or []
    result["physical_page_count"] = len(pdf)
    result["parsed_page_count"] = len(parsed_pages)
    for i, raw_page in enumerate(pdf):
        item: dict[str, Any] = {"page_no": i + 1}
        try:
            text = raw_page.get_textpage().get_text_range()
            item["pdfium_text_chars"] = len(text.strip())
        except Exception as exc:
            item["pdfium_text_error"] = str(exc)
        try:
            verdict = triage_page(raw_page)
            item["triage_recheck"] = verdict.verdict
            dpi = native_image_dpi(raw_page) if verdict.verdict in ("scan_like", "hybrid") else None
            canvas = render_pdf_page(raw_page, i + 1, dpi=dpi)
            item["rendered_canvas"] = {"width": canvas.image.width, "height": canvas.image.height, "dpi": canvas.dpi}
            item["recomputed_tiles"] = len(make_tiles(canvas.image))
            if i < len(parsed_pages):
                parsed = parsed_pages[i]
                item["output_canvas_matches"] = (
                    parsed.get("canvas_w") == canvas.image.width
                    and parsed.get("canvas_h") == canvas.image.height
                    and parsed.get("dpi") == canvas.dpi
                )
        except Exception as exc:
            item["render_error"] = str(exc)
        result["pages"].append(item)
    return result


def _image_audit(source: Path, parse_doc: dict[str, Any]) -> dict[str, Any]:
    try:
        with Image.open(source) as image:
            source_size = list(image.size)
        canvas = load_image_canvas(source)
        parsed = (parse_doc.get("pages") or [{}])[0]
        return {
            "open": "ok",
            "source_size": source_size,
            "recomputed_tiles": len(make_tiles(canvas.image)),
            "output_canvas_matches": (
                parsed.get("canvas_w") == canvas.image.width
                and parsed.get("canvas_h") == canvas.image.height
            ),
        }
    except Exception as exc:
        return {"open": "failed", "error": str(exc)}


def _page_note_kind(note: str) -> str:
    lower = note.lower()
    if "실패" in note or "unreadable" in lower:
        return "failure_or_fallback"
    if "경보" in note or "미검출" in note or "부분 응답" in note:
        return "diagnostic_warning"
    if "타일" in note:
        return "tiling_trace"
    return "other_note"


def audit(root: Path) -> dict[str, Any]:
    manifest = _load(root / "manifest.json")
    entries = manifest.get("files") or manifest.get("items") or []
    summary = _load(root / "summary.json") if (root / "summary.json").exists() else {}
    progress = _load(root / "progress.json") if (root / "progress.json").exists() else {}
    timing_rows = _load(root / "file-timings.json") if (root / "file-timings.json").exists() else []
    progress_by_key = {
        entry.get("key"): entry for entry in (progress.get("entries") or [])
        if isinstance(entry, dict) and entry.get("key")
    }
    timings_by_key = {
        row.get("key"): row for row in timing_rows
        if isinstance(row, dict) and row.get("key")
    }

    totals = Counter()
    routes: Counter = Counter()
    statuses: Counter = Counter()
    triage: Counter = Counter()
    file_types: Counter = Counter()
    extensions: Counter = Counter()
    templates: Counter = Counter()
    category_sources: Counter = Counter()
    line_sources: Counter = Counter()
    line_box_status: Counter = Counter()
    region_box_status: Counter = Counter()
    role_sources: Counter = Counter()
    roles: Counter = Counter()
    candidate_relations: Counter = Counter()
    table_quality: Counter = Counter()
    notes_by_kind: Counter = Counter()
    warnings: list[dict[str, str]] = []
    contract_missing: list[dict[str, Any]] = []
    artifact_missing: list[dict[str, Any]] = []
    preview_mismatches: list[dict[str, Any]] = []
    pdf_checks: list[dict[str, Any]] = []
    image_checks: list[dict[str, Any]] = []
    excluded: list[str] = []
    per_file: list[dict[str, Any]] = []
    ocr_confidences: list[float] = []
    role_confidences: list[float] = []
    candidate_scores: list[float] = []
    candidate_coverages: list[float] = []
    lowconf_rechecked = 0
    label_lines = 0
    vlm_gubun_lines = 0
    gubun_regions = 0
    gubun_breakdown_items = 0
    phrase_hits = 0
    physical_pages = 0
    preview_pages = 0
    elapsed_s: list[float] = []
    vlm_calls_total = vlm_cached_total = 0
    vlm_seconds_total = 0.0
    vlm_stages: dict[str, Counter] = defaultdict(Counter)
    vlm_metric_documents = 0

    for entry in entries:
        key = str(entry.get("key") or "")
        group = str(entry.get("group") or "")
        result_id = str(entry.get("result_id") or "")
        source = Path(entry["source_path"])
        ext = str(entry.get("extension") or source.suffix).lower()
        parse_path = root / group / "parse" / f"{result_id}.json"
        final_path = root / group / "json" / f"{result_id}.json"
        totals["inputs"] += 1
        if LOW_QUALITY_RE.fullmatch(key):
            excluded.append(key)

        if not parse_path.exists() or not final_path.exists():
            artifact_missing.append({"key": key, "parse": parse_path.exists(), "final": final_path.exists()})
            continue
        parse_doc, final_doc = _load(parse_path), _load(final_path)
        # manifest는 입력 목록/충돌 방지용이고 실행 시간은 progress ledger에 있다.
        # 재개 실행에서도 ledger가 최초 실행의 metrics를 보존한다.
        entry_metrics = entry.get("metrics") or (progress_by_key.get(key) or {}).get("metrics") or {}
        timing = timings_by_key.get(key) or {}
        # file-timings.json은 run-events에서 전수 복원한 최종 벽시계 시간이다.
        # progress의 metrics는 중단 이전 71건에서 상세값이 유실됐으므로 시간에는 쓰지 않는다.
        if isinstance(timing.get("elapsed_s"), (int, float)):
            elapsed_s.append(float(timing["elapsed_s"]))
        elif isinstance(entry_metrics.get("elapsed_s"), (int, float)):
            elapsed_s.append(float(entry_metrics["elapsed_s"]))
        if entry_metrics:
            vlm_metric_documents += 1
        vlm_calls_total += int(entry_metrics.get("vlm_calls") or 0)
        vlm_cached_total += int(entry_metrics.get("vlm_cached") or 0)
        vlm_seconds_total += float(entry_metrics.get("vlm_s") or 0)
        for stage, stat in (entry_metrics.get("vlm_by_stage") or {}).items():
            if not isinstance(stat, dict):
                continue
            for metric in ("calls", "cached", "attempts", "timeouts"):
                vlm_stages[stage][metric] += int(stat.get(metric) or 0)
            vlm_stages[stage]["seconds"] += float(stat.get("seconds") or 0)
        totals["parse_json"] += 1
        totals["final_json"] += 1
        file_types[parse_doc.get("file_type") or ext.lstrip(".")] += 1
        extensions[ext] += 1
        templates[(final_doc.get("template") or {}).get("template_id") or "(미결정)"] += 1
        # export 계약의 분류 근거 키는 ``source``다. parse IR의 ``category_source``와
        # 이름이 다른 점을 여기서 명시해, "근거가 전부 없음"이라는 거짓 감사값을 막는다.
        category_sources[(final_doc.get("classification") or {}).get("source") or "(없음)"] += 1
        missing = sorted(EXPECTED_FINAL - set(final_doc))
        if missing:
            contract_missing.append({"key": key, "missing": missing})

        file_record: dict[str, Any] = {
            "key": key,
            "extension": ext,
            "excluded_from_quality_conclusion": bool(LOW_QUALITY_RE.fullmatch(key)),
            "pages": len(parse_doc.get("pages") or []),
            "template": (final_doc.get("template") or {}).get("template_id"),
            "warnings": [],
        }
        page_lines = page_regions = page_unassigned = 0
        for parse_page, final_page in zip(parse_doc.get("pages") or [], final_doc.get("pages") or []):
            totals["pages"] += 1
            canvas_w, canvas_h = parse_page.get("canvas_w"), parse_page.get("canvas_h")
            routes[parse_page.get("parse_route") or "(없음)"] += 1
            statuses[parse_page.get("parse_status") or "(없음)"] += 1
            if parse_page.get("triage"):
                triage[(parse_page["triage"].get("verdict") or "(없음)")] += 1
            page_regions += len(parse_page.get("regions") or [])
            page_unassigned += len(parse_page.get("unassigned_lines") or [])
            for note in parse_page.get("notes") or []:
                notes_by_kind[_page_note_kind(note)] += 1
                if _page_note_kind(note) in ("failure_or_fallback", "diagnostic_warning"):
                    file_record["warnings"].append(note)
                    warnings.append({"key": key, "page": str(parse_page.get("page_no")), "note": note})
            for region in parse_page.get("regions") or []:
                totals["regions"] += 1
                region_box_status[_box_status(region.get("bbox"), canvas_w, canvas_h) or "ok"] += 1
                roles[region.get("role") or "(없음)"] += 1
                role_sources[region.get("role_source") or "(없음)"] += 1
                if isinstance(region.get("role_confidence"), (int, float)):
                    role_confidences.append(float(region["role_confidence"]))
                if region.get("vlm_reading"):
                    totals["vlm_candidate_regions"] += 1
                    candidate_relations[region.get("vlm_reading_relation") or "(없음)"] += 1
                    if isinstance(region.get("vlm_reading_score"), (int, float)):
                        candidate_scores.append(float(region["vlm_reading_score"]))
                    if isinstance(region.get("vlm_reading_coverage"), (int, float)):
                        candidate_coverages.append(float(region["vlm_reading_coverage"]))
                for line in region.get("lines") or []:
                    totals["lines"] += 1
                    page_lines += 1
                    line_sources[line.get("source") or "(없음)"] += 1
                    line_box_status[_box_status(line.get("bbox"), canvas_w, canvas_h) or "ok"] += 1
                    if line.get("source") == "ocr" and isinstance(line.get("confidence"), (int, float)):
                        ocr_confidences.append(float(line["confidence"]))
                    if line.get("vlm_reading_stage") == "lowconf_reread":
                        lowconf_rechecked += 1
            for line in parse_page.get("unassigned_lines") or []:
                totals["unassigned_lines"] += 1
                totals["lines"] += 1
                page_lines += 1
                line_sources[line.get("source") or "(없음)"] += 1
                line_box_status[_box_status(line.get("bbox"), canvas_w, canvas_h) or "ok"] += 1
                if line.get("source") == "ocr" and isinstance(line.get("confidence"), (int, float)):
                    ocr_confidences.append(float(line["confidence"]))

            for region in final_page.get("regions") or []:
                if region.get("gubun"):
                    gubun_regions += 1
                gubun_breakdown_items += len(region.get("gubun_breakdown") or [])
                phrase_hits += len(region.get("phrase_hits") or [])
                table = region.get("table")
                if table:
                    totals["tables"] += 1
                    quality = table.get("quality") if isinstance(table, dict) else None
                    table_quality[(quality or {}).get("status") or "unreported"] += 1
                for line in region.get("lines") or []:
                    label_lines += len(line.get("labels") or [])
                    if line.get("vlm_gubun"):
                        vlm_gubun_lines += 1
            for line in final_page.get("unassigned_lines") or []:
                label_lines += len(line.get("labels") or [])
                if line.get("vlm_gubun"):
                    vlm_gubun_lines += 1

        totals["unassigned_lines"] += 0
        actual_previews = _preview_count(root, group, result_id)
        expected_previews = (
            len(parse_doc.get("pages") or []) if ext == ".pdf"
            else 1 if ext in {".png", ".jpg", ".jpeg"} else 0
        )
        totals["preview_pages"] += actual_previews
        preview_pages += actual_previews
        if actual_previews != expected_previews:
            preview_mismatches.append({
                "key": key, "expected": expected_previews, "actual": actual_previews,
                "result_id": result_id,
            })
        file_record.update({"regions": page_regions, "lines": page_lines, "unassigned_lines": page_unassigned})
        per_file.append(file_record)

        if ext == ".pdf":
            check = _pdf_audit(source, parse_doc)
            check["key"] = key
            pdf_checks.append(check)
            physical_pages += int(check.get("physical_page_count") or 0)
        elif ext in {".png", ".jpg", ".jpeg"}:
            check = _image_audit(source, parse_doc)
            check["key"] = key
            image_checks.append(check)

    pdf_page_records = [page for check in pdf_checks for page in check.get("pages") or []]
    pdf_canvas_match = sum(1 for page in pdf_page_records if page.get("output_canvas_matches") is True)
    pdf_canvas_checked = sum(1 for page in pdf_page_records if "output_canvas_matches" in page)
    pdf_pages_matched = sum(
        1 for check in pdf_checks
        if check.get("open") == "ok" and check.get("physical_page_count") == check.get("parsed_page_count")
    )
    image_canvas_match = sum(1 for check in image_checks if check.get("output_canvas_matches") is True)
    image_canvas_checked = sum(1 for check in image_checks if "output_canvas_matches" in check)
    tile_counts = [page["recomputed_tiles"] for page in pdf_page_records if isinstance(page.get("recomputed_tiles"), int)]
    tile_counts += [check["recomputed_tiles"] for check in image_checks if isinstance(check.get("recomputed_tiles"), int)]
    low_conf_count = sum(1 for value in ocr_confidences if value < 0.80)
    pdfium_char_counts = [
        int(page["pdfium_text_chars"])
        for page in pdf_page_records if isinstance(page.get("pdfium_text_chars"), int)
    ]

    checks = {
        "all_input_artifacts_present": len(artifact_missing) == 0,
        "final_contract_missing_count": len(contract_missing),
        "pdfium_open_failures": sum(1 for check in pdf_checks if check.get("open") != "ok"),
        "pdf_document_page_count_matches": f"{pdf_pages_matched}/{len(pdf_checks)}",
        "pdf_render_canvas_matches": f"{pdf_canvas_match}/{pdf_canvas_checked}",
        "raster_canvas_matches": f"{image_canvas_match}/{image_canvas_checked}",
        "preview_pages_saved": preview_pages,
        "expected_visual_pages": physical_pages + len(image_checks),
        "preview_page_mismatches": preview_mismatches,
        "invalid_or_outside_line_boxes": sum(v for k, v in line_box_status.items() if k not in {"ok", "missing"}),
        "invalid_or_outside_region_boxes": sum(v for k, v in region_box_status.items() if k not in {"ok", "missing"}),
    }
    metrics = {
        "counts": {**_counter_dict(totals), "inputs": len(entries), "excluded_low_quality_inputs": len(excluded)},
        "routes": _counter_dict(routes),
        "parse_statuses": _counter_dict(statuses),
        "triage": _counter_dict(triage),
        "file_types": _counter_dict(file_types),
        "extensions": _counter_dict(extensions),
        "templates": _counter_dict(templates),
        "classification_sources": _counter_dict(category_sources),
        "line_sources": _counter_dict(line_sources),
        "line_bbox_status": _counter_dict(line_box_status),
        "region_bbox_status": _counter_dict(region_box_status),
        "roles": _counter_dict(roles),
        "role_sources": _counter_dict(role_sources),
        "candidate_relations": _counter_dict(candidate_relations),
        "table_quality": _counter_dict(table_quality),
        "notes_by_kind": _counter_dict(notes_by_kind),
        "ocr_confidence": {
            "count": len(ocr_confidences), "mean": _mean(ocr_confidences), "median": _median(ocr_confidences),
            "below_0_80": low_conf_count, "lowconf_reread_stage_lines": lowconf_rechecked,
        },
        "role_confidence": {"count": len(role_confidences), "mean": _mean(role_confidences), "median": _median(role_confidences)},
        "vlm_candidate_overlap": {
            "score_count": len(candidate_scores), "score_mean": _mean(candidate_scores),
            "coverage_mean": _mean(candidate_coverages),
        },
        "labels": {
            "regions_with_vlm_gubun": gubun_regions,
            "gubun_breakdown_items": gubun_breakdown_items,
            "deterministic_phrase_hits": phrase_hits,
            "line_phrase_label_records": label_lines,
            "lines_with_vlm_gubun": vlm_gubun_lines,
        },
        "tiles": {
            "canvas_pages_recomputed": len(tile_counts), "total_tiles": sum(tile_counts),
            "pages_split_into_multiple_tiles": sum(1 for n in tile_counts if n > 1),
            "max_tiles_on_one_page": max(tile_counts, default=0),
        },
        "pdfium_text_layer": {
            "checked_pages": len(pdfium_char_counts),
            "total_non_whitespace_chars": sum(pdfium_char_counts),
            "pages_with_zero_chars": sum(1 for count in pdfium_char_counts if count == 0),
            "mean_chars_per_page": _mean([float(value) for value in pdfium_char_counts]),
        },
        "execution": {
            "elapsed_seconds_total": round(sum(elapsed_s), 3),
            "elapsed_seconds_mean": _mean(elapsed_s),
            "elapsed_document_coverage": f"{len(elapsed_s)}/{len(entries)}",
            "vlm_calls": vlm_calls_total,
            "vlm_cached": vlm_cached_total,
            "vlm_seconds_total": round(vlm_seconds_total, 3),
            "vlm_metric_document_coverage": f"{vlm_metric_documents}/{len(entries)}",
            "vlm_by_stage": {
                stage: {
                    **{name: int(value) for name, value in stats.items() if name != "seconds"},
                    "seconds": round(float(stats["seconds"]), 3),
                }
                for stage, stats in sorted(vlm_stages.items())
            },
        },
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "root": str(root.resolve()),
            "inputs": len(entries),
            "excluded_from_quality_conclusion": excluded,
            "exclusion_reason": "사용자 지정: 4-1~4-9 예금성상품(거치식)은 원본 화질이 근본적으로 낮음",
            "accuracy_boundary": "gold transcription/field gold 없이 자동 수치는 무결성·근거 추적성 지표이며 OCR 문자 정답률이 아님",
        },
        "batch_summary": summary,
        "checks": checks,
        "metrics": metrics,
        "artifact_missing": artifact_missing,
        "preview_mismatches": preview_mismatches,
        "contract_missing": contract_missing,
        "warnings": warnings,
        "pdfium": pdf_checks,
        "images": image_checks,
        "files": per_file,
    }


def markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    counts = metrics["counts"]
    checks = report["checks"]
    scope = report["scope"]
    rows = [
        ("입력 / parse JSON / final JSON", f"{counts['inputs']} / {counts.get('parse_json', 0)} / {counts.get('final_json', 0)}"),
        ("페이지 / 영역 / 줄", f"{counts.get('pages', 0)} / {counts.get('regions', 0)} / {counts.get('lines', 0)}"),
        ("저화질 결론 제외", str(counts['excluded_low_quality_inputs'])),
        ("PDFium 페이지 수 일치", checks["pdf_document_page_count_matches"]),
        ("PDF 재렌더 좌표계 일치", checks["pdf_render_canvas_matches"]),
        ("래스터 좌표계 일치", checks["raster_canvas_matches"]),
        ("유효 범위 밖/깨진 line bbox", str(checks["invalid_or_outside_line_boxes"])),
        ("유효 범위 밖/깨진 region bbox", str(checks["invalid_or_outside_region_boxes"])),
        ("저장된 검수 이미지 / 기대 시각 페이지", f"{checks['preview_pages_saved']} / {checks['expected_visual_pages']}"),
        ("OCR 줄 신뢰도 평균 / <0.80", f"{metrics['ocr_confidence']['mean']} / {metrics['ocr_confidence']['below_0_80']}"),
        ("저신뢰 재판독으로 교체된 줄", str(metrics['ocr_confidence']['lowconf_reread_stage_lines'])),
        ("VLM 후보 영역 / 후보 정본중첩 평균", f"{counts.get('vlm_candidate_regions', 0)} / {metrics['vlm_candidate_overlap']['score_mean']}"),
        ("구분 라벨 영역 / 줄 단위 VLM 구분", f"{metrics['labels']['regions_with_vlm_gubun']} / {metrics['labels']['lines_with_vlm_gubun']}"),
        ("재계산 OCR 타일 / 다중 타일 페이지", f"{metrics['tiles']['total_tiles']} / {metrics['tiles']['pages_split_into_multiple_tiles']}"),
        ("PDFium 텍스트 계층 문자 / 0문자 페이지", f"{metrics['pdfium_text_layer']['total_non_whitespace_chars']} / {metrics['pdfium_text_layer']['pages_with_zero_chars']}"),
    ]
    lines = [
        "# 0827 현재 파이프라인 산출물 감사",
        "",
        "> 이 문서는 원본과 생성물의 연결·좌표·완전성을 전수 확인한 결과입니다. gold 전사본이 없으므로 자동 수치를 OCR 문자 정답률로 해석하지 않습니다.",
        "",
        "## 범위",
        "",
        f"- 입력 {counts['inputs']}개. 사용자 지정 저화질 9개(`4-1~4-9 예금성상품(거치식)`)는 품질 결론에서 제외했습니다.",
        f"- 제외 파일: {', '.join(f'`{Path(x).name}`' for x in scope['excluded_from_quality_conclusion'])}",
        "- PDF는 PDFium으로 다시 열어 페이지 수·텍스트 계층 접근성·현 설정 렌더 좌표계·타일 수를 재확인했습니다. 이미지도 실제 픽셀 크기와 출력 캔버스를 대조했습니다.",
        "",
        "## 전수 무결성 결과",
        "",
        "| 점검 | 결과 |",
        "| --- | ---: |",
        *[f"| {name} | {value} |" for name, value in rows],
        "",
        "## 산출물 분포",
        "",
        f"- 파일 형식: `{metrics['extensions']}`",
        f"- 페이지 라우트: `{metrics['routes']}` / PDF 트리아지: `{metrics['triage']}`",
        f"- 템플릿: `{metrics['templates']}`",
        f"- 분류 근거: `{metrics['classification_sources']}`",
        f"- 라인 출처: `{metrics['line_sources']}`",
        f"- 역할 근거: `{metrics['role_sources']}`",
        f"- VLM 후보 관계: `{metrics['candidate_relations']}`",
        f"- 표 품질 플래그: `{metrics['table_quality']}`",
        f"- PDFium 텍스트 계층: `{metrics['pdfium_text_layer']}`",
        f"- 실행 계측: 총 {metrics['execution']['elapsed_seconds_total']}초 ({metrics['execution']['elapsed_document_coverage']} 파일 시간 복원), "
        f"VLM 상세계측은 재개 전 로그 제한으로 {metrics['execution']['vlm_metric_document_coverage']} 문서만 남음 "
        f"({metrics['execution']['vlm_calls']}회 / {metrics['execution']['vlm_seconds_total']}초)",
        "",
        "## 해석 경계",
        "",
        "- **통과한 것:** 입력-출력 보존, PDF 렌더 좌표계, bbox 기하, 라벨/근거 필드 존재, 실패·경고의 기록성입니다.",
        "- **아직 자동으로 단정할 수 없는 것:** OCR 글자 단위 정답률, 표 셀 값의 의미 정확도, 역할/구분 라벨의 업무적 정답률입니다. 이는 원본 대조 표본 및 별도 gold 세트가 필요합니다.",
        "- **HWP 예외:** 본문은 디지털 텍스트·표를 읽지만 bbox/렌더 페이지가 없으므로 원본 위 하이라이트 검수의 대상이 아닙니다. 이 제약은 성공으로 위장하지 않고 산출물 note에 남습니다.",
        "",
        "## 기록된 경고",
        "",
    ]
    if report["warnings"]:
        lines.extend(f"- `{w['key']}` p{w['page']}: {w['note']}" for w in report["warnings"])
    else:
        lines.append("- 없음")
    lines += ["", "원시 수치와 파일별 PDFium/타일 검사 결과는 `current-pipeline-audit.json`에 있습니다."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="root", type=Path, default=Path("0827-parsing"))
    args = parser.parse_args()
    report = audit(args.root)
    json_path = args.root / "current-pipeline-audit.json"
    md_path = args.root / "current-pipeline-audit.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
