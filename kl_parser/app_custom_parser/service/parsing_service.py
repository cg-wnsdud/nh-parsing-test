# -*- coding: utf-8 -*-
"""파싱 본체 — 농협 예제의 `TODO: MAIN` 자리에 우리 파이프라인을 넣은 것.

농협 예제는 이 자리에 하드코딩 문자열을 쓴다(`sample_jsonl`). 그 자리를 우리 것으로
바꾸는 게 이 파일의 전부다. 규격 쪽(상태 파일·파일명·zip 구성)은 예제를 그대로 따른다.

**두 가지 실행 방식을 지원한다.**

  in-process (`AGENT_URL` 없음)  — 같은 프로세스에서 `nh_parsing` 을 직접 부른다.
                                   로컬 e2e 시험용. 이 폴더만으로 왕복이 돌아간다.
  remote    (`AGENT_URL` 있음)   — 에이전트 서비스에 HTTP 로 넘긴다. **배포 형태.**

왜 두 개인가. 합의된 연동 구조가 "진입점은 얇게, 처리는 별도 에이전트 서비스"인데,
그러면 진입점 컨테이너에는 `requests` 하나만 들어가고 무거운 것(pypdfium2·JVM·사내
파서)은 전부 에이전트에 남는다. 하지만 개발 중에는 서비스 두 개를 띄우지 않고
한 프로세스로 왕복을 확인하는 편이 빠르다 — 규격 검증에는 차이가 없다.
"""

import json
import os
import shutil
import traceback
from pathlib import Path

# 절대 import — 농협 배포는 `cd app_custom_parser && gunicorn main:app` 이라
# `service` 가 최상위 패키지가 된다(main.py 모듈 주석 참조).
from service.status import DONE, ERROR, PARSING, write_parse_status

AGENT_URL = os.getenv("AGENT_URL", "").strip()
AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "600"))


def parse(work_dir: str, img_dir: str, file_path: str, option=None) -> None:
    """농협이 정한 진입 계약. 예외를 밖으로 내지 않고 상태 파일로만 알린다.

    ⚠️ 조용한 실패 금지: 실패해도 `DONE` 을 쓰지 않는다. 농협 예제 코드는 예외 처리에
    괄호 오타(`except Exception as e:(`)가 있어 그대로 쓰면 문법 오류가 나므로 다시 썼다.
    """
    work = Path(work_dir)
    try:
        write_parse_status(work_dir, PARSING)
        if AGENT_URL:
            _parse_remote(work, Path(img_dir), Path(file_path), option)
        else:
            _parse_in_process(work, Path(img_dir), Path(file_path), option)
        write_parse_status(work_dir, DONE)
    except Exception as exc:  # noqa: BLE001 — 상태 파일이 유일한 통보 경로다
        traceback.print_exc()
        write_parse_status(work_dir, ERROR, f"{exc.__class__.__name__}: {exc}")


def _parse_in_process(work: Path, img_dir: Path, src: Path, option) -> None:
    """`nh_parsing` 을 직접 호출해 규정문서 → KL 3종을 만든다.

    import 를 함수 안에서 하는 이유: 배포(remote) 형태의 진입점에는 `nh_parsing` 이
    아예 없다. 모듈 최상단에서 import 하면 그 컨테이너가 뜨지도 못한다.
    """
    from nh_parsing.kl_export import export_kl_files
    from nh_parsing.rag_ingest import ingest_rag_file

    asset_dir = work / "assets"
    doc = ingest_rag_file(src, asset_dir=asset_dir)
    result = export_kl_files(
        json.loads(doc.model_dump_json()),
        work,
        source_path=src,
        asset_dir=asset_dir if asset_dir.is_dir() else None,
    )
    # KL 은 `image/` 안의 파일을 모아 `_img.zip` 을 만든다(진입점 main.py). 우리가 만든
    # zip 을 그대로 쓰지 않고 원본 이미지를 그 자리에 두는 편이 규격에 정확히 맞는다.
    if asset_dir.is_dir():
        img_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted(asset_dir.iterdir()):
            if p.is_file():
                shutil.copy2(p, img_dir / p.name)
    if result.img_zip_path and result.img_zip_path.exists():
        result.img_zip_path.unlink()  # main.py 가 다시 만든다 — 중복 방지

    _fix_info_source(result.info_path, src, option)
    _write_notes(work, result.notes, result.item_kinds, result.item_count)


def _parse_remote(work: Path, img_dir: Path, src: Path, option) -> None:
    """에이전트 서비스에 파일을 넘기고 결과 파일들을 받아 `work` 에 쓴다."""
    import requests

    with src.open("rb") as fh:
        resp = requests.post(
            f"{AGENT_URL.rstrip('/')}/v1/kl-export",
            files={"src_file": (src.name, fh)},
            timeout=AGENT_TIMEOUT,
        )
    resp.raise_for_status()
    payload = resp.json()
    # 에이전트는 파일 내용을 문자열로 돌려준다(zip 이중 압축을 피한다).
    (work / f"{src.name}_hrc.jsonl").write_text(payload["jsonl"], encoding="utf-8")
    (work / f"{src.name}_hrc.json").write_text(payload["info"], encoding="utf-8")
    if payload.get("images"):
        import base64

        img_dir.mkdir(parents=True, exist_ok=True)
        for name, b64 in payload["images"].items():
            (img_dir / name).write_bytes(base64.b64decode(b64))
    _write_notes(work, payload.get("notes") or [], payload.get("item_kinds") or {},
                 payload.get("item_count") or 0)


def parse_ad(work_dir: str, img_dir: str, file_path: str, option=None) -> None:
    """광고물 트랙. 규정문서와 **통신 방식은 같고 산출물이 다르다.**

    왜 다른가. 규정문서는 KL 벡터DB 에 색인될 대상이라 `_hrc.jsonl`(텍스트 청크)로
    변환한다. 광고물은 색인 대상이 아니라 **매번 새로 들어오는 질문**이고, 심의는
    "이 지적의 근거가 원본 어디인가"를 화면에 표시해야 한다 — `_hrc.jsonl` 규격에는
    좌표·역할·판독대조를 담을 칸이 없어 그 정보가 전부 버려진다.

    그래서 광고물은 **파싱(좌표) + 템플릿 판정 + 라벨링을 합친 통합 JSON**을 낸다
    (`nh_parsing.ad_export.process_ad_file` — `tools/run_ad_label.py` 로컬 실행과
    같은 함수를 쓴다). 좌표·시인성·OCR/VLM 후보에 더해 템플릿 항목명까지 붙어 있다.
    """
    work = Path(work_dir)
    try:
        write_parse_status(work_dir, PARSING)
        from nh_parsing.ad_export import process_ad_file

        src = Path(file_path)
        unified, canvases = process_ad_file(src)
        out = work / f"{src.name}_parsed.json"
        out.write_text(json.dumps(unified, ensure_ascii=False, indent=2), encoding="utf-8")

        # 페이지 원본 이미지를 KL 규격의 `image/` 자리에 둔다 — main.py 가 `_img.zip` 으로
        # 묶는다(2026-08-24: 이 묶기 조건이 `_hrc.jsonl` 존재로만 걸려 있어 광고 트랙
        # 이미지가 생성만 되고 응답에 안 담기는 결함을 같이 고쳤다 — main.py 참조).
        # **박스를 그려 넣지 않는다** — 좌표·라벨은 이미 통합 JSON 에 있으니 하류가
        # 원하는 대로(항목별 색 등) 그리면 된다. 그림에 박아 넣으면 그 선택을 우리가
        # 대신 하는 셈이라 `tools/run_ad_label.py` 의 검수 화면과 같은 원칙을 따른다.
        img_path = Path(img_dir)
        img_path.mkdir(parents=True, exist_ok=True)
        for pno, img in canvases.items():
            img.convert("RGB").save(img_path / f"{src.stem}_p{pno}.jpg", quality=85)

        # 요약을 따로 낸다 — 호출한 쪽이 통합 JSON 전체를 읽지 않고도 상태를 알 수 있게.
        (work / "ad_summary.json").write_text(
            json.dumps(
                {
                    "doc_id": unified["doc_id"],
                    "file_type": unified["file_type"],
                    "classification": unified["classification"],
                    "template": unified["template"],
                    "pages": len(unified["pages"]),
                    "completeness": unified["completeness"],
                    "notes": unified["notes"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        write_parse_status(work_dir, DONE)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        write_parse_status(work_dir, ERROR, f"{exc.__class__.__name__}: {exc}")


def _fix_info_source(info_path: Path, src: Path, option) -> None:
    """`_hrc.json` 의 `source` 를 KL 에게 의미 있는 값으로 바꾼다.

    **왜 필요한가.** `nh_parsing.kl_export.build_info` 는 넘겨받은 경로를 그대로 적는다.
    그 경로가 우리 임시 작업 디렉터리(`temp/<uuid>/파일명`)라서 그대로 두면 **KL 쪽에
    아무 뜻도 없는 일회성 경로가 저장된다** — 실측(왕복 시험 2026-08-21):
    `"source": "temp\\19758596869146b5bc4e65f2c2b7c5b3\\(2024년)대출모집인…"`.

    KL 이 `option.doc_data.origin_url` 을 주면 그것이 원본 위치의 정본이므로 우선한다.
    없으면 파일명만 남긴다 — 모르는 경로를 지어내지 않는다.
    """
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
    """우리가 내린 판단을 파일로 남긴다 — KL 로는 안 가고 우리 검수용이다.

    규격에 없는 파일이라 `main.py` 의 zip 대상에도 넣지 않는다. 왜 남기나: `_hrc.jsonl`
    만 보면 "왜 이 문단을 h2 로 봤는지" 를 나중에 설명할 수 없다.
    """
    (work / "kl_parser_notes.json").write_text(
        json.dumps({"item_count": count, "item_kinds": kinds, "notes": notes},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
