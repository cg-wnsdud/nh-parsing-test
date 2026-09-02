# -*- coding: utf-8 -*-
"""농협 Custom Parser 요청을 ``nh_parsing`` 파이프라인에 연결한다.

이 모듈은 OCR/VLM 모델을 직접 실행하지 않는다. 실제 파싱 로직은 ``nh_parsing``에 있고,
그 패키지가 PADDLEX_URL/GEMMA_URL의 GPU 서비스에 HTTP로 요청한다. 즉 이 모듈과
``nh_parsing``은 같은 프로세스 안의 Python 호출 관계이고, 원격인 것은 모델 추론뿐이다.
"""

import json
import shutil
import traceback
from pathlib import Path

from service.status import DONE, ERROR, PARSING, write_parse_status


def parse(work_dir: str, img_dir: str, file_path: str, option=None) -> None:
    """규정문서 하나를 KL 색인용 파일로 변환하고 상태 파일에 결과를 기록한다."""
    work = Path(work_dir)
    try:
        write_parse_status(work_dir, PARSING)
        _parse_document(work, Path(img_dir), Path(file_path), option)
        write_parse_status(work_dir, DONE)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        write_parse_status(work_dir, ERROR, f"{exc.__class__.__name__}: {exc}")


def _parse_document(work: Path, img_dir: Path, src: Path, option) -> None:
    """nh_parsing으로 규정문서 → KL 산출물을 만든다."""
    from nh_parsing.kl_export import export_kl_files
    from nh_parsing.rag_ingest import ingest_rag_file

    asset_dir = work / "assets"
    doc = ingest_rag_file(src, asset_dir=asset_dir)
    result = export_kl_files(
        json.loads(doc.model_dump_json()), work, source_path=src,
        asset_dir=asset_dir if asset_dir.is_dir() else None,
    )
    # main.py가 image/의 파일을 모아 KL 이름 규칙의 *_img.zip을 만든다.
    if asset_dir.is_dir():
        img_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted(asset_dir.iterdir()):
            if p.is_file():
                shutil.copy2(p, img_dir / p.name)
    if result.img_zip_path and result.img_zip_path.exists():
        result.img_zip_path.unlink()

    _fix_info_source(result.info_path, src, option)
    _write_notes(work, result.notes, result.item_kinds, result.item_count)


def parse_ad(work_dir: str, img_dir: str, file_path: str, option=None) -> None:
    """광고를 evidence JSON과 다음 단계용 review-input JSON으로 변환한다."""
    work = Path(work_dir)
    try:
        write_parse_status(work_dir, PARSING)
        from nh_parsing.ad_export import process_ad_file_outputs

        src = Path(file_path)
        unified, review_input, canvases = process_ad_file_outputs(src)
        out = work / f"{src.name}_parsed.json"
        out.write_text(json.dumps(unified, ensure_ascii=False, indent=2), encoding="utf-8")
        (work / f"{src.name}_review_input.json").write_text(
            json.dumps(review_input, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        # 박스는 이미지에 그리지 않고 JSON 좌표로 보존한다. main.py가 페이지 이미지를 ZIP으로 묶는다.
        img_path = Path(img_dir)
        img_path.mkdir(parents=True, exist_ok=True)
        for pno, img in canvases.items():
            img.convert("RGB").save(img_path / f"{src.stem}_p{pno}.jpg", quality=85)

        (work / "ad_summary.json").write_text(
            json.dumps({
                "doc_id": unified["doc_id"], "file_type": unified["file_type"],
                "classification": unified["classification"], "template": unified["template"],
                "pages": len(unified["pages"]), "completeness": unified["completeness"],
                "review_input_summary": review_input["summary"],
                "notes": unified["notes"],
            }, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        write_parse_status(work_dir, DONE)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        write_parse_status(work_dir, ERROR, f"{exc.__class__.__name__}: {exc}")


def _fix_info_source(info_path: Path, src: Path, option) -> None:
    """임시 작업 경로 대신 KL이 이해할 원본 출처를 ``_hrc.json``에 기록한다."""
    if not info_path.exists():
        return
    origin = ""
    if isinstance(option, dict):
        doc_data = option.get("doc_data") or {}
        if isinstance(doc_data, dict):
            origin = str(doc_data.get("origin_url") or doc_data.get("origin_doc_id") or "")
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    info["source"] = origin or src.name
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _write_notes(work: Path, notes: list[str], kinds: dict, count: int) -> None:
    """검수용 진단을 남긴다. 이 파일은 농협 결과 ZIP에는 포함하지 않는다."""
    (work / "kl_parser_notes.json").write_text(
        json.dumps({"item_count": count, "item_kinds": kinds, "notes": notes},
                   ensure_ascii=False, indent=2), encoding="utf-8",
    )
