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
import re
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
    vlm_gubun_by_ref = _vlm_gubun_by_ref(label)
    regions_meta = {r["region_id"]: r for r in label.get("region_labels") or []}

    pages_out: list[dict] = []
    for page in doc.get("pages") or []:
        pno = page.get("page_no")
        regions_out = []
        for region in page.get("regions") or []:
            rid = region.get("region_id")
            meta = regions_meta.get(rid) or {}
            lines_out = [
                _line(f"p{pno}/{rid}/L{i:02d}", ln, labels_by_ref, vlm_gubun_by_ref)
                for i, ln in enumerate(region.get("lines") or [])
            ]
            regions_out.append({
                "region_id": rid,
                "bbox": region.get("bbox"),
                # StructureV3가 최초로 낸 레이아웃 판단. role은 VLM이 다시 판단한
                # 범용 역할이므로, 둘을 덮어쓰지 않고 대조 가능한 신호로 함께 남긴다.
                "layout_label": region.get("label"),
                "layout_score": region.get("layout_score"),
                # 파서가 붙인 범용 역할(제목·유의사항·본문…). 템플릿 항목과 다른 어휘라
                # 덮어쓰지 않고 나란히 둔다 — 서로 검산에 쓴다.
                "role": region.get("role"),
                "role_confidence": region.get("role_confidence"),
                "role_source": region.get("role_source"),
                # 2층: VLM 이 정한 템플릿 항목(대표값 — 영역 안 최대 줄범위 판정).
                "gubun": meta.get("gubun"),
                "gubun_source": meta.get("source"),
                "gubun_confidence": meta.get("confidence"),
                # 영역 하나에 항목이 여러 개 섞였을 때 그 내역 전부. gubun 하나로
                # 접히면서 나머지가 사라지지 않도록 항상 싣는다(하나뿐이면 원소 1개).
                "gubun_breakdown": meta.get("gubun_breakdown") or [],
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
                "table": _table_out(
                    region.get("table"), lines_out, region.get("layout_score"),
                ),
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
                _line(f"p{pno}/unassigned/L{i:02d}", ln, labels_by_ref, vlm_gubun_by_ref)
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


def _line(
    ref: str, ln: dict, labels_by_ref: dict[str, list[dict]],
    vlm_gubun_by_ref: dict[str, list[str]] | None = None,
) -> dict:
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
        # 1층: 이 줄에 걸린 템플릿 **정형** 문구(완전일치, 결정론). 한 줄에 여러
        # 문구가 걸릴 수 있어 목록이다(실측: 유의사항이 `■` 로 붙어 한 줄에 두
        # 문구가 들어 있는 경우).
        "labels": labels_by_ref.get(ref) or [],
        # 2층: VLM 이 이 줄을 어느 항목으로 판정했는지(참고용, 1층과 우열 없음).
        # 영역이 항목 여러 개로 쪼개졌을 때(25번 문서 등) 어느 항목인지 줄 단위로
        # 남기는 자리 — region.gubun 하나로 접히며 사라지던 정보다.
        "vlm_gubun": (vlm_gubun_by_ref or {}).get(ref) or [],
    }


def _table_out(
    table: dict | None,
    lines: list[dict],
    layout_score: float | None = None,
) -> dict | None:
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
    outside_refs = [
        l["line_ref"] for l in lines
        if l["line_ref"] not in {r for c in cells_out for r in c["line_refs"]}
    ]
    out = {
        "n_rows": table.get("n_rows", 0),
        "n_cols": table.get("n_cols", 0),
        "html": table.get("html") or "",
        "cells": cells_out,
        "note": table.get("note"),
        # 셀에 못 닿은 정본 줄 — 있으면 격자가 표 전체를 덮지 못했다는 뜻이다.
        # 숨기지 않는다. 표 밖 캡션이 영역에 같이 들어온 경우도 여기 잡힌다.
        "lines_outside_cells": outside_refs,
    }
    # 표 HTML/셀 텍스트를 정본으로 승격하지는 않는다. 대신 셀의 좌표·참조 관계가
    # 명백히 어긋난 경우를 표시해, 하류가 표 구조를 무비판적으로 쓰지 않게 한다.
    out["quality"] = _table_quality(out, lines, layout_score)
    return out


def _table_quality(table: dict, lines: list[dict], layout_score: float | None) -> dict:
    """표 구조의 *경고등*을 만든다. 어떤 텍스트·셀도 고치거나 삭제하지 않는다.

    `trusted` 는 셀 방향과 셀-줄 연결에 뚜렷한 모순이 없다는 뜻이다. `degraded` 는
    표 캡션/주석이 격자 밖에 있거나 일부 연결이 어긋났다는 뜻이며, `invalid` 는 행·열
    좌표가 뒤집혔거나 셀 텍스트와 연결된 줄이 다수 충돌한 경우다. 이는 표의 진위를
    판정하는 값이 아니라, 다음 단계가 `html`만 믿어도 되는지 판단하는 안전 신호다.
    """
    issues: list[str] = []
    cells = [c for c in table.get("cells") or [] if c.get("bbox")]

    if table.get("note"):
        issues.append("grid_geometry_unavailable")

    # 행 번호가 늘면 아래로, 열 번호가 늘면 오른쪽으로 가야 한다. 두 축 모두 반대로
    # 흐르면 180도 뒤집힌 표처럼, 단순 OCR 오차보다 훨씬 강한 구조 오류다.
    row_pairs = _cell_axis_pairs(cells, axis="row")
    col_pairs = _cell_axis_pairs(cells, axis="col")
    reversed_rows = _reversed_pair_count(row_pairs)
    reversed_cols = _reversed_pair_count(col_pairs)
    if _mostly_reversed(row_pairs, reversed_rows) and _mostly_reversed(col_pairs, reversed_cols):
        issues.append("cell_coordinate_order_reversed")

    by_ref = {line["line_ref"]: line.get("text") or "" for line in lines}
    compared = 0
    mismatched = 0
    for cell in cells:
        expected = _normalise_table_text(cell.get("text") or "")
        actual = _normalise_table_text(" ".join(by_ref.get(ref, "") for ref in cell.get("line_refs") or []))
        # 빈 셀/짧은 기호 셀은 비교 대상이 아니다. 표 엔진과 OCR의 줄바꿈 차이도
        # 허용하기 위해 한쪽이 다른 쪽을 포함하면 일치로 본다.
        if len(expected) < 4 or len(actual) < 4:
            continue
        compared += 1
        if expected not in actual and actual not in expected:
            mismatched += 1
    if compared and mismatched:
        issues.append(f"cell_text_reference_mismatch:{mismatched}/{compared}")

    if table.get("lines_outside_cells"):
        issues.append("lines_outside_cells")
    if layout_score is not None and layout_score < 0.65:
        issues.append("low_layout_score")

    invalid = (
        "grid_geometry_unavailable" in issues
        or "cell_coordinate_order_reversed" in issues
        or (compared >= 2 and mismatched / compared >= 0.5)
    )
    status = "invalid" if invalid else ("degraded" if issues else "trusted")
    return {
        "status": status,
        "issues": issues,
        "cell_text_comparison": {"compared": compared, "mismatched": mismatched},
        "geometry": {
            "row_pairs": len(row_pairs), "reversed_rows": reversed_rows,
            "col_pairs": len(col_pairs), "reversed_cols": reversed_cols,
        },
    }


def _cell_axis_pairs(cells: list[dict], axis: str) -> list[tuple[float, float]]:
    """같은 열의 행쌍/같은 행의 열쌍을 (인덱스 차, 위치 차)로 만든다."""
    other = "col" if axis == "row" else "row"
    coordinate = 1 if axis == "row" else 0  # y 중심 / x 중심
    pairs: list[tuple[float, float]] = []
    for first_i, first in enumerate(cells):
        for second in cells[first_i + 1:]:
            if first.get(other) != second.get(other):
                continue
            one, two = first.get(axis), second.get(axis)
            if one is None or two is None or one == two:
                continue
            first_center = (first["bbox"][coordinate] + first["bbox"][coordinate + 2]) / 2
            second_center = (second["bbox"][coordinate] + second["bbox"][coordinate + 2]) / 2
            pairs.append((float(two - one), second_center - first_center))
    return pairs


def _reversed_pair_count(pairs: list[tuple[float, float]]) -> int:
    return sum(1 for index_delta, position_delta in pairs if index_delta * position_delta < 0)


def _mostly_reversed(pairs: list[tuple[float, float]], count: int) -> bool:
    # 한 쌍만으로 표 전체가 뒤집혔다고 말하지 않는다. 2쌍 이상에서 80% 이상일 때만.
    return len(pairs) >= 2 and count / len(pairs) >= 0.8


def _normalise_table_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()


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
    """정형 문구의 **완전일치** 결과만 줄 기준으로 뒤집는다.

    VLM 이 판정한 변수형 항목(`item["line_refs"]`)은 여기 안 섞는다 — 섞으면 두
    가지가 동시에 깨진다. ① 완전일치·VLM 판정이 같은 줄에 겹치면 `labels` 에
    같은 gubun 이 두 번 실린다. ② `completeness.labeled`(결정론적 문구 매칭만
    재는 지표)가 VLM 판정까지 세어 의미가 달라진다(2026-08-26 시험에서 둘 다
    실측 회귀로 잡혔다). VLM 쪽은 `_vlm_gubun_by_ref` 로 따로 뒤집어 별도
    필드(`line.vlm_gubun`)에 싣는다 — 두 층은 나란히 두고 우열을 안 가린다는
    원칙(§2층 주석)을 줄 단위에도 그대로 지킨다.
    """
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


def _vlm_gubun_by_ref(label: dict) -> dict[str, list[str]]:
    """VLM 이 줄 범위로 판정한 항목명을 줄 기준으로 뒤집는다(2층, 참고용).

    `_labels_by_ref`(1층, 완전일치)와 나란히 두는 목적으로 분리했다 — 자세한 이유는
    그 함수 docstring 참조.
    """
    out: dict[str, list[str]] = {}
    for item in label.get("items") or []:
        for ref in item.get("line_refs") or []:
            bucket = out.setdefault(ref, [])
            if item["gubun"] not in bucket:
                bucket.append(item["gubun"])
    return out


def _items_index(label: dict, labels_by_ref: dict) -> list[dict]:
    """항목 → (문구 상태 + 그 문구가 걸린 줄). 줄 쪽 사실의 역방향 보기다.

    `line_refs` 는 두 층의 합집합이다 — ① `labels_by_ref`(1층, 정형 문구 완전일치)
    ② `item["line_refs"]`(2층, `ad_template.label_document` 이 이미 VLM 판정으로
    채워 준 것). 여기서 ②를 안 합치면 변수형 항목(`상품명`·`대출한도` 등)이
    VLM 이 실제로 위치를 짚었는데도 `line_refs` 가 항상 빈 채로 나간다 —
    `label_document` 이 애써 채운 값이 이 단계에서 조용히 지워지는 결함이었다
    (2026-08-26 발견).
    """
    items: list[dict] = []
    for item in label.get("items") or []:
        fixed_refs = {
            r for r in labels_by_ref
            if any(e["gubun"] == item["gubun"] for e in labels_by_ref[r])
        }
        refs = fixed_refs | set(item.get("line_refs") or [])
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
