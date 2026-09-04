# -*- coding: utf-8 -*-
"""기존 광고 파싱 결과를 현행 농협 전달 ZIP 계약으로 로컬 재구성한다.

이 도구는 OCR/VLM 서비스를 호출하지 않는다. 과거 ``*_parsed.json``에 보존된
템플릿 라벨과 같은 실행의 내부 parse JSON을 이용해 현행 evidence-v6 근거 원장과
ad-review-input-v6 인계본을 만든다. 따라서 새 VLM 표 판독이 없던 실행은 결과에도
``not_run``으로 남고, 존재하지 않는 관측값을 꾸며 넣지 않는다.

사용 예::

    python tools/build_nh_ad_delivery.py \
      --legacy-evidence old/12.pdf_parsed.json \
      --parse-json out_ad_full/parse/12.json \
      --image-zip old/12.pdf_img.zip \
      --out-dir deliverables/current
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nh_parsing.ad_export import build_unified  # noqa: E402
from nh_parsing.ad_review_input import build_ad_review_input  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _line_total(document: dict[str, Any]) -> int:
    return sum(
        len(region.get("lines") or [])
        for page in document.get("pages") or []
        for region in page.get("regions") or []
    ) + sum(
        len(page.get("unassigned_lines") or [])
        for page in document.get("pages") or []
    )


def _legacy_label_result(legacy: dict[str, Any]) -> dict[str, Any]:
    """과거 통합 JSON의 라벨 정보를 ``build_unified`` 입력으로 복원한다."""
    items = copy.deepcopy(legacy.get("template_items") or [])
    items_by_gubun = {str(item.get("gubun") or ""): item for item in items}
    region_labels: list[dict[str, Any]] = []
    for page in legacy.get("pages") or []:
        for region in page.get("regions") or []:
            gubun = region.get("gubun")
            lines = region.get("lines") or []
            semantic = []
            if gubun:
                semantic.append(
                    {
                        "gubun": gubun,
                        "confidence": region.get("gubun_confidence"),
                        "line_from": 0,
                        "line_to": max(0, len(lines) - 1),
                    }
                )
                # 과거 계약은 영역에 gubun을 남겼지만 template_items[].line_refs에는
                # 정형 문구 일치만 싣던 시기가 있었다. 현행 review-input의 완전 분할을
                # 복원하려면 그 영역의 줄을 같은 템플릿 항목 근거로 되돌려야 한다.
                item = items_by_gubun.get(str(gubun))
                if item is not None:
                    refs = list(item.get("line_refs") or [])
                    for line in lines:
                        ref = line.get("line_ref")
                        if ref and ref not in refs:
                            refs.append(ref)
                    item["line_refs"] = refs
            region_labels.append(
                {
                    "region_id": region.get("region_id"),
                    "gubun_breakdown": semantic,
                    "phrase_hits": region.get("phrase_hits") or [],
                    "phrase_share": region.get("phrase_share", 0.0),
                }
            )
    return {
        "items": items,
        "region_labels": region_labels,
        "completeness": {"lines_total": _line_total(legacy)},
    }


def build_delivery(
    legacy_evidence: Path,
    parse_json: Path,
    out_dir: Path,
    image_zip: Path | None = None,
) -> list[Path]:
    legacy = _read_json(legacy_evidence)
    parsed = _read_json(parse_json)
    evidence = build_unified(
        parsed,
        legacy.get("template") or {},
        _legacy_label_result(legacy),
    )
    review_input = build_ad_review_input(evidence)

    out_dir.mkdir(parents=True, exist_ok=True)
    source_file = str(evidence.get("source_file") or legacy.get("source_file") or "advertisement")
    evidence_path = out_dir / f"{source_file}_parsed.json"
    review_path = out_dir / f"{source_file}_review_input.json"
    summary_path = out_dir / "ad_summary.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    review_path.write_text(
        json.dumps(review_input, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(
            {
                "doc_id": evidence.get("doc_id"),
                "file_type": evidence.get("file_type"),
                "classification": evidence.get("classification") or {},
                "template": evidence.get("template") or {},
                "pages": len(evidence.get("pages") or []),
                "completeness": evidence.get("completeness") or {},
                "review_input_summary": review_input.get("summary") or {},
                "notes": evidence.get("notes") or [],
                "generation_note": (
                    "기존 로컬 parse/label 결과를 현행 계약으로 재구성했으며 "
                    "OCR/VLM 외부 서비스는 다시 호출하지 않음"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    members = [evidence_path, review_path, summary_path]
    if image_zip is not None:
        if not image_zip.is_file():
            raise FileNotFoundError(image_zip)
        copied_image_zip = out_dir / f"{source_file}_img.zip"
        if image_zip.resolve() != copied_image_zip.resolve():
            shutil.copy2(image_zip, copied_image_zip)
        members.append(copied_image_zip)

    output_zip = out_dir / "kl-core-s2-output.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, path.name)
    output_zip.write_bytes(buffer.getvalue())
    return [output_zip, *members]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-evidence", type=Path, required=True)
    parser.add_argument("--parse-json", type=Path, required=True)
    parser.add_argument("--image-zip", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    for output in build_delivery(
        args.legacy_evidence,
        args.parse_json,
        args.out_dir,
        args.image_zip,
    ):
        print(output)


if __name__ == "__main__":
    main()
