# -*- coding: utf-8 -*-
"""광고 파싱 결과 + 템플릿 라벨을 근거 JSON으로 합친다.

왜 합치나. 지금까지 둘은 따로 났다 — 좌표는 파싱 결과(`out/json`)에, 라벨은 라벨링
결과(`out_label`)에. 하류(심의)는 "이 문구가 화면 어디에 있나"를 물으므로 두 파일을
line_ref 로 손수 이어 붙여야 했다. 그 이음질을 여기서 한 번만 한다.

**출력의 뼈대는 파싱 결과 쪽이다** — 쪽 → 영역 → 줄. 라벨은 그 위에 얹는 곁가지다.
반대로(항목 → 문구 → 줄) 짜면 라벨이 안 붙은 줄을 실을 자리가 없어져, 이 프로젝트의
기둥인 '모든 텍스트 보존'이 구조부터 깨진다. 항목별 검토가 필요하면 원문을 복제한
`template_items` 대신, 필요한 항목·기재요령·근거 줄만 담은 `review_targets`를 만든다.
원본은 항상 줄/영역 쪽이다.

`build_unified()` 는 텍스트가 하나라도 새면 **예외를 던진다.** 조용히 빠뜨리느니
멈추는 게 낫다 — 빠진 줄은 다음 단계에서 '광고에 그 말이 없다'로 둔갑한다.

이 파일의 결과는 evidence-v6(감사 원본)다. 다음 단계가 바로 소비하는 필드 중심
`ad-review-input-v5`는 ``ad_review_input.build_ad_review_input``이 이 결과에서 별도로
만든다. 두 출력을 섞으면 좌표 근거와 단순 필드 값 중 하나가 반드시 흐려진다.
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

    # 카드가 둘 이상인 한 페이지는 상품 광고 두 장을 한 파일로 모은 경우가 많다. 이때
    # 파일명(예: "입출식·적립식 통합")이나 문서 전체 문구로 템플릿 하나를 고르면 다른
    # 카드의 필수 항목이 조용히 사라진다. 카드별 템플릿 경로를 먼저 선택한다.
    card_scopes = _card_template_scopes(doc)
    if card_scopes:
        scoped_results = _label_card_scopes(doc, canvases, card_scopes, pack, AT)
        resolution = _card_scoped_resolution(scoped_results)
        label = _merge_scoped_labels(doc, scoped_results)
        return build_unified(doc, resolution, label)

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

    label = AT.label_document(
        doc, resolution.template_id, pack, vlm_labels=vlm_labels,
        apply_explicit_line_guard=True,
    )
    return build_unified(doc, resolution.as_dict(), label)


def _card_template_scopes(doc: dict) -> list[dict]:
    """한 페이지 안에서 분리된 카드가 둘 이상일 때만 상품별 scope를 만든다.

    ``card_no`` 는 읽기 순서 보조 정보로 이미 존재하지만, 여기서는 처음으로 템플릿
    선택의 경계로도 쓴다. ``0``은 공통 헤더/고지이고, ``None``은 카드 판정이 없었던
    영역이므로 상품 scope로 억지 배정하지 않는다.
    """
    scopes: list[dict] = []
    for page in doc.get("pages") or []:
        page_no = page.get("page_no")
        cards = sorted({
            region.get("card_no")
            for region in page.get("regions") or []
            if isinstance(region.get("card_no"), int) and region.get("card_no") > 0
        })
        if len(cards) < 2:
            continue
        scopes.extend({
            "scope_id": f"p{page_no}:card{card_no}",
            "scope": {"page_no": page_no, "card_no": card_no},
        } for card_no in cards)
    return scopes


def _document_for_card(doc: dict, scope: dict) -> dict:
    """원문 구조는 바꾸지 않고, 한 카드의 영역만 보이는 얕은 문서 view를 만든다."""
    page_no = (scope.get("scope") or {}).get("page_no")
    card_no = (scope.get("scope") or {}).get("card_no")
    pages = []
    for page in doc.get("pages") or []:
        if page.get("page_no") != page_no:
            continue
        regions = [
            region for region in page.get("regions") or []
            if region.get("card_no") == card_no
        ]
        pages.append({
            **page,
            "regions": regions,
            # 카드에 귀속되지 않은 낱줄은 상품 템플릿에 임의로 넣지 않는다.
            "unassigned_lines": [],
        })
    return {**doc, "pages": pages}


def _masked_card_canvas(canvas, page: dict, card_no: int):
    """다른 카드의 텍스트만 흰색 처리한 같은 좌표계의 캔버스.

    crop 좌표를 다시 계산하면 VLM 프롬프트의 bbox/화면 위치가 어긋난다. 그래서 원본과
    같은 크기를 유지하고 다른 카드의 StructureV3 영역만 가린다. 공통(card 0) 요소는
    그대로 둔다. 템플릿 선택은 자신의 카드 줄을 기준으로 하고, 공통 고지는 P2에서
    ``unmapped_ad_copy``로 별도 보존한다.
    """
    if canvas is None:
        return None
    from PIL import ImageDraw

    masked = canvas.convert("RGB").copy()
    draw = ImageDraw.Draw(masked)
    for region in page.get("regions") or []:
        other = region.get("card_no")
        bbox = region.get("bbox") or []
        if other not in {None, 0, card_no} and len(bbox) == 4:
            draw.rectangle(tuple(bbox), fill="white")
    return masked


def _label_card_scopes(doc: dict, canvases: dict, scopes: list[dict], pack: dict, AT) -> list[dict]:
    """카드별 템플릿 확정과 영역 라벨링을 독립 수행한다."""
    results: list[dict] = []
    pages_by_no = {page.get("page_no"): page for page in doc.get("pages") or []}
    for scope in scopes:
        scope_doc = _document_for_card(doc, scope)
        page_no = (scope.get("scope") or {}).get("page_no")
        card_no = (scope.get("scope") or {}).get("card_no")
        page = pages_by_no.get(page_no) or {}
        scope_canvas = _masked_card_canvas(canvases.get(page_no), page, card_no)

        # 통합 파일명에는 상충하는 세부유형 힌트가 함께 들어갈 수 있다. 카드별 판정에는
        # 파일명 힌트를 쓰지 않고 해당 카드의 문구·이미지로 VLM 결선투표를 한다.
        resolution = _resolve_card_template(
            scope_doc, scope_canvas, pack, AT,
        )

        vlm_labels = {}
        if resolution.template_id:
            vlm_labels = AT.label_regions_vlm(
                scope_doc, resolution.template_id, pack=pack,
                canvas_for_page=lambda _page, image=scope_canvas: image,
            )
        label = AT.label_document(
            scope_doc, resolution.template_id, pack, vlm_labels=vlm_labels,
            apply_explicit_line_guard=True,
        )
        results.append({
            **scope,
            "template": resolution.as_dict(),
            "label": label,
        })
    return results


def _resolve_card_template(scope_doc: dict, scope_canvas, pack: dict, AT):
    """카드별 문구·이미지로 한 번만 세부 템플릿을 선택한다.

    통합 파일명에는 입출식·적립식처럼 상충하는 힌트가 함께 들어갈 수 있다. 따라서 파일명
    힌트 없이 해당 카드의 문구와, 다른 카드 텍스트를 가린 페이지 이미지를 사용한다.
    """
    base = AT.resolve_template(
        scope_doc.get("product_group"), scope_doc.get("product_name_shown"), "", pack=pack,
    )
    if base.status == "확정":
        return base
    return AT.resolve_template_vlm(scope_doc, base, pack=pack, canvas=scope_canvas)


def _card_scoped_resolution(scoped_results: list[dict]) -> dict:
    """기존 단일 ``template`` 자리에 카드별 선택 계약을 명시한다."""
    return {
        "template_id": None,
        "status": "card_scoped",
        "basis": ["card_no: per-card template selection"],
        "note": "한 페이지에 둘 이상 카드가 있어 카드별 템플릿을 독립 선택했다",
        "selection_mode": "per_card",
        "shared_content_policy": (
            "card_no=0/None content is preserved as unmapped_ad_copy; it is not copied into a product card"
        ),
        "scopes": [
            {
                "scope_id": result["scope_id"],
                "scope": result["scope"],
                "template": result["template"],
            }
            for result in scoped_results
        ],
    }


def _merge_scoped_labels(doc: dict, scoped_results: list[dict]) -> dict:
    """카드별 라벨 결과를 P1 공통 좌표 구조에 얹을 수 있게 합친다."""
    items: list[dict] = []
    region_labels: list[dict] = []
    for result in scoped_results:
        descriptor = {
            "scope_id": result["scope_id"],
            "scope": result["scope"],
            "template_id": (result.get("template") or {}).get("template_id"),
        }
        label = result["label"]
        items.extend({**item, "template_scope": descriptor} for item in label.get("items") or [])
        region_labels.extend({**region, "template_scope": descriptor}
                             for region in label.get("region_labels") or [])

    line_total = sum(
        len(region.get("lines") or [])
        for page in doc.get("pages") or [] for region in page.get("regions") or []
    ) + sum(len(page.get("unassigned_lines") or []) for page in doc.get("pages") or [])
    return {
        "template_id": None,
        "items": items,
        "region_labels": region_labels,
        "other_content": [],
        "completeness": {"lines_total": line_total},
    }


def label_parsed_ad_outputs(
    doc: dict,
    filename: str,
    canvases: dict,
    pack: dict | None = None,
) -> tuple[dict, dict]:
    """파싱 후 공통 단계의 두 최종 산출물을 함께 만든다.

    첫 값은 evidence-v6 감사 원본, 두 번째 값은 그것만을 입력으로 만든
    ad-review-input-v5이다. P2는 Judge가 고른 VLM 영역 문구를 범위가 정확히 일치하는
    view에만 시험적으로 사용하며, 파서 기본 텍스트 자체는 바꾸지 않는다.
    """
    from .ad_review_input import build_ad_review_input

    evidence = label_parsed_ad(doc, filename, canvases, pack)
    return evidence, build_ad_review_input(evidence)


def process_ad_file(path: Path, pack: dict | None = None) -> tuple[dict, dict]:
    """파일 하나를 파싱부터 통합까지 전부 처리한다 (기본 진입점 — 매번 새로 파싱).

    반환: (통합 JSON, {쪽번호: PIL.Image}) — 이미지는 호출자가 미리보기 등에 쓴다.
    """
    from .pipeline import process_file

    doc = json.loads(process_file(path).model_dump_json())
    canvases = page_canvases(path)
    return label_parsed_ad(doc, path.name, canvases, pack), canvases


def process_ad_file_outputs(path: Path, pack: dict | None = None) -> tuple[dict, dict, dict]:
    """광고 파일 하나를 두 최종 JSON과 페이지 캔버스로 처리한다."""
    from .pipeline import process_file

    doc = json.loads(process_file(path).model_dump_json())
    canvases = page_canvases(path)
    evidence, review_input = label_parsed_ad_outputs(doc, path.name, canvases, pack)
    return evidence, review_input, canvases


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
            parser_primary_text = "\n".join(line["parser_text"] for line in lines_out).strip()
            vlm_region_text = region.get("element_vlm_reading")
            table_out = _table_evidence(region, lines_out)
            regions_out.append({
                "region_id": rid,
                "bbox": region.get("bbox"),
                "layout": {
                    "label": region.get("label"),
                    "score": region.get("layout_score"),
                },
                # 공간 경계만 보존한다. 파싱 단계에서 상품/이벤트 의미를 확정하지 않는다.
                "card_no": region.get("card_no"),
                "visibility": _visibility(region.get("bbox"), lines_out, page),
                # lines[]가 파서 기본 텍스트의 원자 단위다. 영역 문자열은 검색/심의
                # 편의용이며 VLM 판독·비교는 이 값을 바꾸지 않는 독립 관측이다.
                "text_evidence": {
                    "parser_primary_text": {
                        "text": parser_primary_text,
                        "text_sources": sorted({
                            line["text_source"] for line in lines_out if line.get("text_source")
                        }),
                        "line_refs": [line["line_ref"] for line in lines_out],
                    },
                    "vlm_region_reading": {
                        "text": vlm_region_text,
                        "confidence": region.get("element_vlm_confidence"),
                    } if vlm_region_text is not None else None,
                    "parser_vlm_comparison": _vlm_comparison_evidence(
                        region.get("reading_adjudication"),
                    ),
                    # P1은 파서/VLM/Judge 근거를 모두 보존한다. P2는 이 선택값을
                    # 영역 전체가 하나의 심의 문구일 때만 쓸 수 있으며, Line.text 자체는
                    # 어떤 경우에도 바꾸지 않는다.
                    "p2_text_selection": _p2_text_selection(
                        parser_primary_text, region.get("reading_adjudication"),
                    ),
                },
                # 템플릿 라벨은 범용 role과 다르다. 대표값 하나로 접지 않고 줄 범위와
                # 정형 문구 일치 근거를 모두 남긴다.
                "template_labels": {
                    "template_scope": meta.get("template_scope"),
                    "semantic": meta.get("gubun_breakdown") or [],
                    "fixed_phrase_hits": meta.get("phrase_hits") or [],
                    "phrase_share": meta.get("phrase_share", 0.0),
                },
                "lines": lines_out,
                "table": table_out,
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
            # StructureV3 영역을 만들지 못한 페이지 문구의 후보. 정본에는 편입하지 않는다.
            "recovery_candidates": page.get("recovery_candidates") or [],
        })

    review_targets = _review_targets(_items_index(label, labels_by_ref), pages_out)
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
        "reading_evidence_contract": {
            "version": "nh-ad-review-evidence-v6",
            "parser_primary_text": (
                "pages[].regions[].text_evidence.parser_primary_text + lines[].parser_text"
            ),
            "parser_primary_text_sources": ["digital", "ocr", "hybrid"],
            "vlm_region_reading_mode": "shadow_observation",
            "parser_mutates_primary_text_from_vlm": False,
            "p2_text_selection": (
                "judge_selected_region_test; only when a P2 view covers all parser primary lines "
                "in the StructureV3 region"
            ),
            "card_template_selection": (
                "per_card when two or more positive card_no values exist on a page; "
                "card_no=0/None is preserved as shared/unmapped content"
            ),
            "table_structure": "structurev3_bbox + vlm_table_reading_observation",
            "paddlex_table_grid_used": False,
        },
        "pages": pages_out,
        # 다음 심의 단계가 쓰는 작업 목록. 이전 template_items의 내부 매칭 상세를
        # 그대로 넘기지 않고, 정적 기준과 실제 좌표 근거만 정리한다.
        "review_targets": review_targets,
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
        # parser_text는 한 줄의 기본값이다. OCR만을 뜻하지 않으며 text_source를
        # 함께 봐야 디지털 PDF 텍스트인지 OCR인지 알 수 있다.
        "parser_text": ln.get("text") or "",
        "bbox": ln.get("bbox"),
        "text_source": ln.get("source"),
        "ocr_confidence": ln.get("confidence"),
        "style": ln.get("style"),
        # 1층: 이 줄에 걸린 템플릿 **정형** 문구(완전일치, 결정론). 한 줄에 여러
        # 문구가 걸릴 수 있어 목록이다(실측: 유의사항이 `■` 로 붙어 한 줄에 두
        # 문구가 들어 있는 경우).
        "labels": labels_by_ref.get(ref) or [],
        # 2층: VLM 이 이 줄을 어느 항목으로 판정했는지(참고용, 1층과 우열 없음).
        # 영역이 항목 여러 개로 쪼개졌을 때(25번 문서 등) 어느 항목인지 줄 단위로
        # 남기는 자리 — region.gubun 하나로 접히며 사라지던 정보다.
        "vlm_gubun": (vlm_gubun_by_ref or {}).get(ref) or [],
    }


_COMPARISON_STATUS = {
    "reader_failed": "vlm_reading_failed",
    "agreed": "parser_and_vlm_agree",
    "judge_selected": "judge_selected_parser_primary_text",
    "uncertain": "needs_human_review",
    "judge_failed": "vlm_judge_failed",
}
_PROPOSED_TEXT_SOURCE = {
    "canonical": "parser_primary_text",
    "element_vlm": "vlm_region_reading",
    "merged": "merged_candidate",
}


def _vlm_comparison_evidence(reading: dict | None) -> dict | None:
    """내부 Reader/Judge 용어를 소비자용 VLM 비교 계약으로 바꾼다.

    내부 구현의 ``canonical``/``reader`` 명명은 공개 JSON에 새지 않는다. 파서가
    보존한 기본 텍스트와 이미지 기반 VLM 판독을 명확히 구분해, 하류가 OCR 결과와
    VLM 후보를 한 값으로 오인하지 못하게 한다.
    """
    if not reading:
        return None
    proposed_source = reading.get("proposed_source")
    status = str(reading.get("status") or "")
    # VLM을 택한 Judge 결과는 shadow 때문에 Region.lines를 바꾸지 않는다. 그래도 P1에는
    # 그 선택 사실을 남겨, P2가 '영역 전체가 하나의 심의 문구'인 제한된 경우에만 시험할
    # 수 있게 한다.
    if proposed_source == "element_vlm" and reading.get("proposed_text"):
        public_status = "judge_selected_vlm_candidate_requires_review"
    else:
        public_status = _COMPARISON_STATUS.get(status, "needs_human_review")
    return {
        "mode": reading.get("mode") or "shadow",
        "comparison_status": public_status,
        "crop_bbox": reading.get("crop_bbox"),
        "target_bbox": reading.get("target_bbox"),
        "crop_policy": reading.get("crop_policy"),
        "excluded_overlap_region_ids": reading.get("excluded_overlap_region_ids") or [],
        "selection_reasons": reading.get("selection_reasons") or [],
        "parser_primary_text_source": reading.get("canonical_source"),
        "vlm_region_text": reading.get("reader_text"),
        "vlm_region_confidence": reading.get("reader_confidence"),
        "comparison_relation": reading.get("relation"),
        "judge": {
            "decision": reading.get("judge_decision"),
            "selected_text": reading.get("proposed_text"),
            "selected_text_source": _PROPOSED_TEXT_SOURCE.get(proposed_source, proposed_source),
            "confidence": reading.get("confidence"),
            "reason": reading.get("reason"),
            "error": reading.get("error"),
        },
    }


def _p2_text_selection(parser_primary_text: str, reading: dict | None) -> dict:
    """P2가 사용할 수 있는 영역 단위 단일 문구 후보를 명시한다.

    Reader가 StructureV3 영역 전체를 읽으므로, VLM 선택은 P2에서 그 영역의 모든 파서
    줄을 같은 view가 포함할 때만 사용한다. 영역 일부(예: 유의사항 7줄 중 금리 1줄)에
    VLM 전체 판독을 억지로 잘라 넣는 일은 이 함수와 P2 투영 모두에서 금지한다.
    """
    fallback = {
        "selected_text": parser_primary_text,
        "selected_text_source": "parser_primary_text",
        "selection_status": "parser_primary_fallback",
        "selection_policy": "judge_selected_region_test",
        "scope_requirement": "all_region_parser_primary_lines",
    }
    if not reading:
        return {**fallback, "selection_status": "parser_primary_no_vlm_comparison"}

    proposed = str(reading.get("proposed_text") or "").strip()
    proposed_source = reading.get("proposed_source")
    decision = reading.get("judge_decision")
    if proposed and proposed_source == "element_vlm" and decision in {"candidate_a", "candidate_b"}:
        return {
            "selected_text": proposed,
            "selected_text_source": "vlm_judge_selected_region_test",
            "selection_status": "judge_selected_vlm_region_test",
            "selection_policy": "judge_selected_region_test",
            "scope_requirement": "all_region_parser_primary_lines",
        }
    if proposed_source == "canonical":
        return {**fallback, "selection_status": "judge_selected_parser_primary_text"}
    if proposed_source == "merged":
        return {**fallback, "selection_status": "merged_candidate_not_used"}
    if reading.get("status") in {"uncertain", "judge_failed", "reader_failed"}:
        return {**fallback, "selection_status": "judge_unresolved_parser_primary_fallback"}
    return fallback


def _table_evidence(region: dict, lines: list[dict]) -> dict | None:
    """표 영역의 위치는 StructureV3, 행/셀 관측은 VLM으로만 남긴다.

    PaddleX의 HTML, 셀 텍스트, 셀 bbox는 결과에 복사하지 않는다. 이 값들은 실행마다
    맞기도 틀리기도 하는 격자 후보라 심의 근거 계약에 넣으면 안 된다. 표에 속한 OCR
    파서 기본 줄은 다른 영역과 똑같이 ``lines``/``text_evidence.parser_primary_text``에
    보존한다.
    """
    is_table = str(region.get("label") or "").casefold() == "table" or region.get("table") is not None
    if not is_table:
        return None

    observation = region.get("table_vlm_reading") or {}
    reader_text = region.get("element_vlm_reading")
    rows = observation.get("rows") or []
    text = observation.get("text") or reader_text or ""
    if observation:
        status = observation.get("status") or ("observed" if text else "unreadable")
        source = "table_region_reader"
    elif reader_text:
        # v2 결과를 변환할 때의 호환 표기다. 새 파이프라인에서는 table_vlm_reading이
        # 채워진다. 기존 일반 Reader의 줄바꿈을 셀 구조로 오인하지 않는다.
        status = "legacy_text_only"
        source = "legacy_region_reader"
    else:
        status = "not_run"
        source = "table_region_reader"
    return {
        "detected_by": "structurev3",
        "paddlex_grid_used": False,
        "parser_primary_line_refs": [line["line_ref"] for line in lines],
        "vlm_table_reading": {
            "source": source.replace("reader", "reading"),
            "status": status,
            "text": text or None,
            "rows": rows,
            "confidence": observation.get("confidence", region.get("element_vlm_confidence")),
            "structure_confidence": observation.get("structure_confidence"),
        },
    }


def _table_out(
    table: dict | None,
    lines: list[dict],
    layout_score: float | None = None,
) -> dict | None:
    """[호환용/비활성] 이전 PaddleX 표 격자에 정본 줄을 좌표로 이어 붙인다.

    왜 셀 텍스트로 안 끝내나. 표 안 글자에도 OCR 오류가 있다 — PaddleX 표 인식이 읽은
    값과 정본(디지털 텍스트/OCR 라인)이 다를 수 있고, 디지털 텍스트가 대개 더 정확하다.
    그래서 `text`(표 인식 값)는 근거로 남기고, `line_refs` 로 정본 줄을 가리킨다.
    하류는 둘 중 무엇을 쓸지 고를 수 있다 — 여기서 대신 고르지 않는다.

    셀 좌표가 없으면(격자 note 참조) `line_refs` 는 빈 목록이 된다. 행·열 관계는 남는다.

    현재 evidence-v6와 ad-review-input-v5는 이 함수를 호출하지 않는다. 표 구조의
    신뢰 원천은 StructureV3 영역 bbox + table_region_reader VLM 관측이다.
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
                    if item.get("template_scope"):
                        entry["template_scope"] = item["template_scope"]
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
    """항목 → (문구 상태 + 그 문구가 걸린 줄)의 내부 점검 색인.

    `line_refs` 는 두 층의 합집합이다 — ① `labels_by_ref`(1층, 정형 문구 완전일치)
    ② `item["line_refs"]`(2층, `ad_template.label_document` 이 이미 VLM 판정으로
    채워 준 것). 여기서 ②를 안 합치면 변수형 항목(`상품명`·`대출한도` 등)이
    VLM 이 실제로 위치를 짚었는데도 `line_refs` 가 항상 빈 채로 나간다 —
    `label_document` 이 애써 채운 값이 이 단계에서 조용히 지워지는 결함이었다
    (2026-08-26 발견).
    """
    items: list[dict] = []
    for item in label.get("items") or []:
        scope_id = ((item.get("template_scope") or {}).get("scope_id"))
        fixed_refs = {
            r for r in labels_by_ref
            if any(
                e["gubun"] == item["gubun"]
                and (
                    not scope_id
                    or ((e.get("template_scope") or {}).get("scope_id")) == scope_id
                )
                for e in labels_by_ref[r]
            )
        }
        refs = fixed_refs | set(item.get("line_refs") or [])
        items.append({**item, "line_refs": sorted(refs)})
    return items


def _visibility(bbox: list[int] | None, lines: list[dict], page: dict) -> dict:
    """심의가 재계산할 수 있는 시인성 사실만 남긴다.

    글자 크기·색은 PDF/HWP 디지털 텍스트에서만 얻을 수 있어 ``style_available``을 함께
    둔다. OCR 이미지에 값을 꾸며 넣지 않고, bbox 비율과 OCR 줄 높이를 공통 근거로 쓴다.
    """
    canvas_w = int(page.get("canvas_w") or 0)
    canvas_h = int(page.get("canvas_h") or 0)
    position = None
    if bbox and len(bbox) == 4 and canvas_w > 0 and canvas_h > 0:
        x0, y0, x1, y1 = bbox
        position = {
            "x_ratio": [round(x0 / canvas_w, 4), round(x1 / canvas_w, 4)],
            "y_ratio": [round(y0 / canvas_h, 4), round(y1 / canvas_h, 4)],
            "area_ratio": round(max(0, x1 - x0) * max(0, y1 - y0) / (canvas_w * canvas_h), 6),
        }
    heights = [
        line["bbox"][3] - line["bbox"][1]
        for line in lines if line.get("bbox") and len(line["bbox"]) == 4
    ]
    styles = [line.get("style") for line in lines if line.get("style")]
    return {
        "position": position,
        "line_height_px": {
            "min": min(heights) if heights else None,
            "max": max(heights) if heights else None,
            "mean": round(sum(heights) / len(heights), 2) if heights else None,
        },
        "style_available": bool(styles),
        "style_sources": sorted({style.get("style_source") for style in styles if style.get("style_source")}),
    }


def _review_targets(items: list[dict], pages: list[dict]) -> list[dict]:
    """심의로 넘길 최소 템플릿 작업 목록을 만든다.

    ``template_items``는 정형 문구 매칭의 내부 상세라 그대로 계약으로 쓰지 않는다. 여기서는
    항목명·필수여부·기재요령·실제 line/region/card 근거만 남긴다. ``evidence_status``는
    위반 여부가 아니라 파서가 근거를 위치시켰는지에 대한 상태다.
    """
    owner: dict[str, dict] = {}
    for page in pages:
        for region in page.get("regions") or []:
            for line in region.get("lines") or []:
                owner[line["line_ref"]] = {
                    "region_id": region["region_id"],
                    "card_no": region.get("card_no"),
                }

    targets: list[dict] = []
    for item in items:
        refs = sorted(set(item.get("line_refs") or []))
        owners = [owner[ref] for ref in refs if ref in owner]
        template_scope = item.get("template_scope") or {}
        scope_id = template_scope.get("scope_id")
        fixed_checks = []
        for phrase in item.get("fixed_phrases") or []:
            phrase_refs = sorted({ref for match in phrase.get("matches") or [] for ref in match.get("refs") or []})
            fixed_checks.append({
                "phrase_id": phrase.get("phrase_id"),
                "requirement": phrase.get("requirement"),
                "exact_match_found": bool(phrase.get("found")),
                "line_refs": phrase_refs,
                "prefix_coverage": phrase.get("prefix_coverage"),
            })
        if refs:
            evidence_status = "located"
        elif item.get("variable_entries"):
            evidence_status = "not_located"
        elif fixed_checks:
            evidence_status = "no_exact_match"
        else:
            evidence_status = "not_applicable"
        targets.append({
            "target_id": (
                f"{scope_id}:template:{item.get('gubun')}"
                if scope_id else f"template:{item.get('gubun')}"
            ),
            "label": item.get("gubun"),
            "template_id": template_scope.get("template_id"),
            "template_scope": template_scope or None,
            "requirement": item.get("requirement"),
            "writing_rules": item.get("writing_rules") or [],
            "evidence_status": evidence_status,
            "evidence": {
                "line_refs": refs,
                "region_ids": sorted({value["region_id"] for value in owners}),
                "card_nos": sorted({value["card_no"] for value in owners if value["card_no"] is not None}),
                "fixed_phrase_checks": fixed_checks,
            },
        })
    return targets


def _completeness(pages: list[dict], label: dict) -> dict:
    """통합 결과 **자체를 다시 세어** 낸다 — 라벨링이 준 숫자를 베끼지 않는다."""
    lines = unified_lines(pages)
    labels_of_region = {
        r["region_id"]: (r.get("template_labels") or {}).get("semantic") or []
        for p in pages for r in p["regions"]
    }
    labeled = sum(1 for l in lines if l["labels"])
    covered = 0
    for page in pages:
        for region in page["regions"]:
            has_region_label = bool(labels_of_region.get(region["region_id"]))
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
    after = Counter(l["parser_text"] for l in unified_lines(unified["pages"]))
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
