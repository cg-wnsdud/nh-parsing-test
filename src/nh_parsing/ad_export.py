# -*- coding: utf-8 -*-
"""광고 파싱 결과 + 템플릿 라벨을 **하나의 JSON** 으로 합친다.

왜 합치나. 지금까지 둘은 따로 났다 — 좌표는 파싱 결과(`out/json`)에, 라벨은 라벨링
결과(`out_label`)에. 하류(심의)는 "이 문구가 화면 어디에 있나"를 물으므로 두 파일을
line_ref 로 손수 이어 붙여야 했다. 그 이음질을 여기서 한 번만 한다.

**출력의 뼈대는 파싱 결과 쪽이다** — 쪽 → 영역 → 줄. 라벨은 그 위에 얹는 곁가지다.
반대로(항목 → 문구 → 줄) 짜면 라벨이 안 붙은 줄을 실을 자리가 없어져, 이 프로젝트의
기둥인 '모든 텍스트 보존'이 구조부터 깨진다. 항목별로 보고 싶을 때를 위해
`template_items` 색인을 따로 싣는다 — 같은 사실의 역방향 보기일 뿐 원본은 줄 쪽이다.

`build_unified()` 는 텍스트가 하나라도 새면 **예외를 던진다.** 조용히 빠뜨리느니
멈추는 게 낫다 — 빠진 줄은 다음 단계에서 '광고에 그 말이 없다'로 둔갑한다.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


# 파일 확장자별 캔버스 렌더 — VLM 라벨링(템플릿 항목 판정)과 검수 화면이 같은 그림을
# 쓰게 한다. PDF 는 쪽마다, 이미지는 1쪽으로 취급한다.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


def page_canvases(path: Path) -> dict:
    """쪽번호(1-based) → PIL.Image. 지원 밖 확장자는 빈 dict(HWP 는 여기서 안 다룬다 —
    광고물 트랙은 pdf/이미지만 받는다, `pipeline.process_file` 과 같은 전제)."""
    from .canvas import load_image_canvas, render_pdf_page

    ext = path.suffix.lower()
    if ext == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        return {i + 1: render_pdf_page(pdf[i], i + 1).image for i in range(len(pdf))}
    if ext in _IMAGE_EXTS:
        return {1: load_image_canvas(path).image}
    return {}


def label_parsed_ad(doc: dict, filename: str, canvases: dict, pack: dict | None = None) -> dict:
    """이미 파싱된 문서(`doc`) 하나를 템플릿 판정→라벨링→통합까지 처리한다.

    `tools/run_ad_label.py`(로컬 반복 실행 — `--reuse-parse` 로 파싱을 건너뛸 수 있다)와
    `kl_parser`(농협 진입점, 매번 새로 파싱) 가 **파싱 이후 단계를 여기서 공유한다** —
    판정 순서(표 → VLM 결선투표 → 영역 라벨링)를 두 곳에 따로 적으면 한쪽만 고치는
    사고가 난다.
    """
    from . import ad_template as AT

    pack = pack or AT.load_pack()
    resolution = AT.resolve_template(
        doc.get("product_group"), doc.get("product_name_shown"), filename, pack=pack,
    )
    if resolution.status != "확정":
        resolution = AT.resolve_template_vlm(doc, resolution, pack=pack, canvas=canvases.get(1))

    vlm_labels = {}
    if resolution.template_id:
        vlm_labels = AT.label_regions_vlm(
            doc, resolution.template_id, pack=pack,
            canvas_for_page=lambda pg: canvases.get(pg.get("page_no")),
        )

    label = AT.label_document(doc, resolution.template_id, pack, vlm_labels=vlm_labels)
    return build_unified(doc, resolution.as_dict(), label)


def process_ad_file(path: Path, pack: dict | None = None) -> tuple[dict, dict]:
    """파일 하나를 파싱부터 통합까지 전부 처리한다 (기본 진입점 — 매번 새로 파싱).

    반환: (통합 JSON, {쪽번호: PIL.Image}) — 이미지는 호출자가 미리보기 등에 쓴다.
    """
    from .pipeline import process_file

    doc = json.loads(process_file(path).model_dump_json())
    canvases = page_canvases(path)
    return label_parsed_ad(doc, path.name, canvases, pack), canvases


def build_unified(
    doc: dict,
    resolution: dict,
    label: dict,
) -> dict:
    """파싱 결과(`doc`) · 템플릿 판정(`resolution`) · 라벨링(`label`) → 통합 JSON.

    `doc` 는 `AdDocument.model_dump()`, `resolution` 은 `ad_template.Resolution.as_dict()`,
    `label` 은 `ad_template.label_document()` 의 반환값이다.
    """
    labels_by_ref = _labels_by_ref(label)
    regions_meta = {r["region_id"]: r for r in label.get("region_labels") or []}

    pages_out: list[dict] = []
    for page in doc.get("pages") or []:
        pno = page.get("page_no")
        regions_out = []
        for region in page.get("regions") or []:
            rid = region.get("region_id")
            meta = regions_meta.get(rid) or {}
            lines_out = [
                _line(f"p{pno}/{rid}/L{i:02d}", ln, labels_by_ref)
                for i, ln in enumerate(region.get("lines") or [])
            ]
            regions_out.append({
                "region_id": rid,
                "bbox": region.get("bbox"),
                # 파서가 붙인 범용 역할(제목·유의사항·본문…). 템플릿 항목과 다른 어휘라
                # 덮어쓰지 않고 나란히 둔다 — 서로 검산에 쓴다.
                "role": region.get("role"),
                # 2층: VLM 이 정한 템플릿 항목.
                "gubun": meta.get("gubun"),
                "gubun_source": meta.get("source"),
                "gubun_confidence": meta.get("confidence"),
                # 1층: 이 영역 안에서 완전일치한 템플릿 문구들.
                "phrase_hits": meta.get("phrase_hits") or [],
                "phrase_share": meta.get("phrase_share", 0.0),
                # 영역 통독 후보(§6/B안) — OCR 정본을 덮지 않고 나란히 둔다. 실측
                # (2026-08-24, 시연 5건)으로는 33/33·19/19·31/33·42/44·10/12 영역에 있다 —
                # 드문 값이 아니라 대부분의 영역에 붙는 값이라, 빼면 "조용한 삭제"가 된다
                # (ir.py 의 설계 원칙 위반). 최종 채택은 여기서 하지 않는다 — 하류(심의) 몫이다.
                "vlm_reading": region.get("vlm_reading"),
                "vlm_reading_score": region.get("vlm_reading_score"),
                "vlm_reading_coverage": region.get("vlm_reading_coverage"),
                "vlm_reading_relation": region.get("vlm_reading_relation"),
                "lines": lines_out,
                # 표로 인식된 영역만. 줄을 대체하지 않고 **행·열 관계만** 얹는다.
                "table": _table_out(region.get("table"), lines_out),
            })
        pages_out.append({
            "page_no": pno,
            "canvas_w": page.get("canvas_w"),
            "canvas_h": page.get("canvas_h"),
            "dpi": page.get("dpi"),
            "parse_route": page.get("parse_route"),
            "parse_status": page.get("parse_status"),
            "regions": regions_out,
            # 영역에 못 붙은 낱줄. 라벨은 못 받아도 좌표와 텍스트는 그대로 실린다.
            "unassigned_lines": [
                _line(f"p{pno}/unassigned/L{i:02d}", ln, labels_by_ref)
                for i, ln in enumerate(page.get("unassigned_lines") or [])
            ],
        })

    unified = {
        "doc_id": doc.get("doc_id"),
        "source_file": doc.get("source_file"),
        "file_type": doc.get("file_type"),
        "classification": {
            "product_group": doc.get("product_group"),
            "ad_type": doc.get("ad_type"),
            "product_name_shown": doc.get("product_name_shown"),
            "source": doc.get("category_source"),
            "confidence": doc.get("classification_confidence"),
        },
        "template": resolution,
        "pages": pages_out,
        "template_items": _items_index(label, labels_by_ref),
        "completeness": _completeness(pages_out, label),
        "notes": doc.get("notes") or [],
    }
    _verify(doc, unified, label)
    return unified


def unified_lines(pages: list[dict]) -> list[dict]:
    """통합 결과의 모든 줄을 순서대로 — 영역 안이든 미배정이든."""
    out: list[dict] = []
    for page in pages:
        for region in page["regions"]:
            out.extend(region["lines"])
        out.extend(page["unassigned_lines"])
    return out


def _line(ref: str, ln: dict, labels_by_ref: dict[str, list[dict]]) -> dict:
    return {
        "line_ref": ref,
        "text": ln.get("text") or "",
        "bbox": ln.get("bbox"),
        "source": ln.get("source"),
        "confidence": ln.get("confidence"),
        "style": ln.get("style"),
        # 라인 단위 재판독 후보 — 위 region.vlm_reading 과 같은 원칙, 같은 이유로 남긴다.
        "vlm_reading": ln.get("vlm_reading"),
        "vlm_reading_conf": ln.get("vlm_reading_conf"),
        "vlm_reading_stage": ln.get("vlm_reading_stage"),
        # 이 줄에 걸린 템플릿 문구. 한 줄에 여러 문구가 걸릴 수 있어 목록이다
        # (실측: 유의사항이 `■` 로 붙어 한 줄에 두 문구가 들어 있는 경우).
        "labels": labels_by_ref.get(ref) or [],
    }


def _table_out(table: dict | None, lines: list[dict]) -> dict | None:
    """표 격자에 **정본 줄을 좌표로 이어 붙인다.**

    왜 셀 텍스트로 안 끝내나. 표 안 글자에도 OCR 오류가 있다 — PaddleX 표 인식이 읽은
    값과 정본(디지털 텍스트/OCR 라인)이 다를 수 있고, 디지털 텍스트가 대개 더 정확하다.
    그래서 `text`(표 인식 값)는 근거로 남기고, `line_refs` 로 정본 줄을 가리킨다.
    하류는 둘 중 무엇을 쓸지 고를 수 있다 — 여기서 대신 고르지 않는다.

    셀 좌표가 없으면(격자 note 참조) `line_refs` 는 빈 목록이 된다. 행·열 관계는 남는다.
    """
    if not table:
        return None
    boxed = [l for l in lines if l.get("bbox")]
    cells_out = []
    for cell in table.get("cells") or []:
        box = cell.get("bbox")
        refs = [l["line_ref"] for l in boxed if box and _mostly_inside(l["bbox"], box)] if box else []
        cells_out.append({
            "row": cell.get("row"), "col": cell.get("col"),
            "row_span": cell.get("row_span", 1), "col_span": cell.get("col_span", 1),
            "bbox": box,
            "text": cell.get("text") or "",
            "line_refs": refs,
        })
    return {
        "n_rows": table.get("n_rows", 0),
        "n_cols": table.get("n_cols", 0),
        "html": table.get("html") or "",
        "cells": cells_out,
        "note": table.get("note"),
        # 셀에 못 닿은 정본 줄 — 있으면 격자가 표 전체를 덮지 못했다는 뜻이다.
        # 숨기지 않는다. 표 밖 캡션이 영역에 같이 들어온 경우도 여기 잡힌다.
        "lines_outside_cells": [
            l["line_ref"] for l in lines
            if l["line_ref"] not in {r for c in cells_out for r in c["line_refs"]}
        ],
    }


def _mostly_inside(inner: list[int], outer: list[int], min_share: float = 0.5) -> bool:
    """`inner` 면적의 절반 이상이 `outer` 안에 있나 — 셀 배정 기준.

    0.5 는 조율한 값이 아니다. 줄 상자는 셀 테두리를 조금 넘기 마련이므로 완전 포함
    (1.0)은 거의 안 맞고, 절반 미만이면 옆 셀 것이라고 보는 게 자연스럽다.
    """
    ix = max(0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = max(1, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return (ix * iy) / area >= min_share


def _labels_by_ref(label: dict) -> dict[str, list[dict]]:
    """`items` 의 매칭 결과를 줄 기준으로 뒤집는다."""
    out: dict[str, list[dict]] = {}
    for item in label.get("items") or []:
        for phrase in item.get("fixed_phrases") or []:
            if not phrase.get("found"):
                continue
            for match in phrase.get("matches") or []:
                for ref in match.get("refs") or []:
                    entry = {
                        "gubun": item["gubun"],
                        "phrase_id": phrase["phrase_id"],
                        "requirement": phrase.get("requirement"),
                        "how": match.get("how"),
                    }
                    out.setdefault(ref, []).append(entry)
    return out


def _items_index(label: dict, labels_by_ref: dict) -> list[dict]:
    """항목 → (문구 상태 + 그 문구가 걸린 줄). 줄 쪽 사실의 역방향 보기다."""
    items: list[dict] = []
    for item in label.get("items") or []:
        refs = [
            r for r in labels_by_ref
            if any(e["gubun"] == item["gubun"] for e in labels_by_ref[r])
        ]
        items.append({**item, "line_refs": sorted(refs)})
    return items


def _completeness(pages: list[dict], label: dict) -> dict:
    """통합 결과 **자체를 다시 세어** 낸다 — 라벨링이 준 숫자를 베끼지 않는다."""
    lines = unified_lines(pages)
    gubun_of_region = {
        r["region_id"]: r["gubun"] for p in pages for r in p["regions"]
    }
    labeled = sum(1 for l in lines if l["labels"])
    covered = 0
    for page in pages:
        for region in page["regions"]:
            has_region_label = bool(gubun_of_region.get(region["region_id"]))
            for line in region["lines"]:
                if line["labels"] or has_region_label:
                    covered += 1
        # 미배정 줄은 영역 라벨을 받을 수 없지만 문구가 직접 걸릴 수는 있다.
        # 이 줄들을 빼먹으면 라벨이 붙었는데 안 닿은 것으로 세어진다(실측 1줄 차이).
        covered += sum(1 for line in page["unassigned_lines"] if line["labels"])
    return {
        "lines_total": len(lines),
        "labeled": labeled,
        "lines_covered": covered,
        # 라벨링 단계가 센 값과 어긋나면 이음질이 틀어진 것이다. 값으로 남겨 둔다.
        "labeling_lines_total": (label.get("completeness") or {}).get("lines_total"),
    }


def _verify(doc: dict, unified: dict, label: dict) -> None:
    """텍스트가 한 줄도 새지 않았는지. 새면 **여기서 멈춘다.**

    두 가지를 본다:
      1. `doc` 의 줄 텍스트 다발 == 통합 결과의 줄 텍스트 다발 (여기 traversal 결함)
      2. 라벨링이 센 줄 수 == 통합이 센 줄 수 (두 모듈이 서로 다른 것을 보고 있음)
    2번은 서로 독립으로 만든 구조를 맞대는 검산이라, 한쪽만 고치고 다른 쪽을 안 고친
    변경을 잡는다 — 예를 들어 `collect_lines` 가 세는 줄과 여기가 싣는 줄이 갈라지는 경우.
    """
    before = Counter(
        (ln.get("text") or "")
        for page in doc.get("pages") or []
        for ln in (
            [l for r in (page.get("regions") or []) for l in (r.get("lines") or [])]
            + list(page.get("unassigned_lines") or [])
        )
    )
    after = Counter(l["text"] for l in unified_lines(unified["pages"]))
    if before != after:
        lost = before - after
        extra = after - before
        raise ValueError(
            f"통합 중 텍스트가 어긋났다 — 유실 {sum(lost.values())}줄, "
            f"증식 {sum(extra.values())}줄. 예: 유실={list(lost)[:2]} 증식={list(extra)[:2]}"
        )

    counted = unified["completeness"]["lines_total"]
    labeled_total = (label.get("completeness") or {}).get("lines_total")
    if labeled_total is not None and labeled_total != counted:
        raise ValueError(
            f"라벨링과 통합이 센 줄 수가 어긋났다 — 라벨링 {labeled_total}줄, 통합 {counted}줄"
        )
