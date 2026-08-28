"""원본을 직접 보며 고른 대표 표본의 결과 근거를 추출한다.

gold 정답률 검사가 아니다. 눈으로 확인 가능한 원본 문구가 final JSON의 line_ref,
bbox, source, label/VLM label에 실제로 연결돼 있는지를 남기는 표본 기록이다.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SAMPLES: list[dict[str, Any]] = [
    {
        "key": "new-sample-data/6. 대출성상품.pdf",
        "purpose": "인쇄형 PDF: 큰 제목·표·금리·fine-print가 함께 있는 구조화 페이지",
        "visual_observation": "원본 렌더에서 ‘SOHO·가맹점주 우대대출’, 금리 두 줄, 표 행과 하단 고지문이 서로 다른 시각 영역으로 보인다.",
        "phrases": ["SOHO", "최저 연 4.90%", "최대 연 1.50%p"],
    },
    {
        "key": "new-sample-data/5. 카드상품.png",
        "purpose": "초장신 모바일 이미지: 이벤트 카드 반복·날짜·여러 수준의 고지문",
        "visual_observation": "원본은 위에서 아래로 이어지는 모바일 이벤트 목록과 긴 유의사항으로 구성돼 있어 타일 경계·읽기 순서 검수에 적합하다.",
        "phrases": ["NH농협카드", "1.7만원", "유의사항"],
        "manual_findings": [
            "상단 카드 숫자는 축소 원본에서 `1.7만원`으로 읽히지만 정본 OCR은 여러 곳에서 `17만원`으로만 보존한다. 작은 숫자 표기는 확대 원본으로 한 번 더 확인해야 하므로 확정 오류로 세지 않고 **숫자 검수 큐**에 둔다.",
        ],
    },
    {
        "key": "sample-data/NH농협은행-2026_002-예금성.png",
        "purpose": "초장신 예금 이벤트: 대형 장식 타이포와 작은 약관이 공존",
        "visual_observation": "원본 상단에 ‘행운의 777 이벤트’, 중간에 ‘최고연 7.1%’, 하단에 이벤트 유의사항·상품 유의사항이 명확히 보인다.",
        "phrases": ["행운의 777 이벤트", "최고연 7.1%", "당첨자 발표"],
        "manual_findings": [
            "`최고연 7.1%`는 정본 OCR에 `최고연71%`로 저장됐지만 해당 영역의 VLM 후보는 `최고연 7.1%`다. 즉 실제 원본과 맞는 후보가 있어도 현재 정책상 정본을 자동 교체하지 않는 **확인된 숫자 표기 위험** 사례다.",
        ],
    },
    {
        "key": "sample-data/NH농협은행-2026_003-예금성.pdf",
        "purpose": "3페이지 카드 콜라주: 표지·EVENT 1·EVENT 2가 한 페이지에 병렬 배치",
        "visual_observation": "1페이지 원본은 좌우 세 카드가 병렬이며 EVENT 1과 EVENT 2의 문장이 번갈아 읽히면 안 되는 형태다.",
        "phrases": ["올원모임", "EVENT 1", "EVENT 2"],
    },
]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def _find_lines(doc: dict[str, Any], phrase: str) -> list[dict[str, Any]]:
    target = _norm(phrase)
    found: list[dict[str, Any]] = []
    for page in doc.get("pages") or []:
        for region in page.get("regions") or []:
            for line in region.get("lines") or []:
                if target in _norm(line.get("text") or ""):
                    found.append({
                        "page": page.get("page_no"), "region_id": region.get("region_id"),
                        "line_ref": line.get("line_ref"), "text": line.get("text"),
                        "source": line.get("source"), "bbox": line.get("bbox"),
                        "role": region.get("role"), "gubun": region.get("gubun"),
                        "labels": line.get("labels") or [], "vlm_gubun": line.get("vlm_gubun") or [],
                    })
        for line in page.get("unassigned_lines") or []:
            if target in _norm(line.get("text") or ""):
                found.append({
                    "page": page.get("page_no"), "region_id": None,
                    "line_ref": line.get("line_ref"), "text": line.get("text"),
                    "source": line.get("source"), "bbox": line.get("bbox"),
                    "role": None, "gubun": None,
                    "labels": line.get("labels") or [], "vlm_gubun": line.get("vlm_gubun") or [],
                })
    return found


def _cards(parse_doc: dict[str, Any]) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for page in parse_doc.get("pages") or []:
        card_ids: dict[str, list[str]] = {}
        for region in page.get("regions") or []:
            number = region.get("card_no") or 0
            card_ids.setdefault(str(number), []).append(region.get("region_id"))
        pages.append({"page": page.get("page_no"), "card_region_counts": {k: len(v) for k, v in card_ids.items()}})
    return {"pages": pages}


def inspect(root: Path) -> list[dict[str, Any]]:
    manifest = _load(root / "manifest.json")
    by_key = {entry["key"]: entry for entry in manifest["files"]}
    results: list[dict[str, Any]] = []
    for sample in SAMPLES:
        entry = by_key[sample["key"]]
        base = root / entry["group"]
        result_id = entry["result_id"]
        parse_doc = _load(base / "parse" / f"{result_id}.json")
        final_doc = _load(base / "json" / f"{result_id}.json")
        results.append({
            **sample,
            "source_file": entry["source_path"],
            "parse_json": str(base / "parse" / f"{result_id}.json"),
            "final_json": str(base / "json" / f"{result_id}.json"),
            "preview_images": [str(p) for p in sorted((base / "pages").glob(f"{result_id}_p*.jpg"))],
            "page_routes": [
                {"page": page.get("page_no"), "route": page.get("parse_route"), "regions": len(page.get("regions") or []),
                 "unassigned": len(page.get("unassigned_lines") or [])}
                for page in parse_doc.get("pages") or []
            ],
            "cards": _cards(parse_doc),
            "phrase_evidence": {phrase: _find_lines(final_doc, phrase) for phrase in sample["phrases"]},
            "template": final_doc.get("template"),
            "classification": final_doc.get("classification"),
            "relevant_notes": [
                note for page in parse_doc.get("pages") or [] for note in page.get("notes") or []
                if any(token in note for token in ("타일", "카드", "스윕", "잘림", "범람", "미검출"))
            ],
        })
    return results


def markdown(items: list[dict[str, Any]]) -> str:
    out = [
        "# 원본 기반 대표 표본 검수 기록",
        "",
        "> 원본을 눈으로 본 뒤, 그 핵심 문구가 최종 JSON의 어느 line_ref/bbox/source로 보존됐는지 적은 추적 기록입니다. 표본 확인은 전수 문자 정답률을 대신하지 않습니다.",
        "",
    ]
    for item in items:
        out += [f"## `{item['key']}`", "", f"- 목적: {item['purpose']}", f"- 원본 관찰: {item['visual_observation']}", f"- 분류: `{item['classification']}`", f"- 템플릿: `{item['template']}`", f"- 페이지: `{item['page_routes']}`", f"- 카드 분포: `{item['cards']}`", ""]
        out += ["| 원본에서 확인한 문구 | 연결 결과 |", "| --- | --- |"]
        for phrase, matches in item["phrase_evidence"].items():
            if matches:
                view = "; ".join(
                    f"p{m['page']} {m['line_ref']} ({m['source']}): {m['text']}"
                    for m in matches[:3]
                )
            else:
                view = "**미발견 — 재검수 후보**"
            out.append(f"| {phrase} | {view} |")
        if item["relevant_notes"]:
            out += ["", "- 관련 진단: " + " / ".join(item["relevant_notes"])]
        if item.get("manual_findings"):
            out += ["", "- 원본 대조 판단: " + " / ".join(item["manual_findings"])]
        out += ["", f"- 원본: `{item['source_file']}`", f"- parse: `{item['parse_json']}`", f"- final: `{item['final_json']}`", ""]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="root", type=Path, default=Path("0827-parsing"))
    args = parser.parse_args()
    items = inspect(args.root)
    (args.root / "original-sample-inspection.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.root / "original-sample-inspection.md").write_text(markdown(items), encoding="utf-8")
    print(f"wrote {args.root / 'original-sample-inspection.json'}")
    print(f"wrote {args.root / 'original-sample-inspection.md'}")


if __name__ == "__main__":
    main()
