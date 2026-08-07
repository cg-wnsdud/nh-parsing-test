# -*- coding: utf-8 -*-
"""out/normalized/*.json 을 대상 저장소의 **진짜 계약 모델**로 검증한다.

⚠️ 이 스크립트는 **이 프로젝트 환경에서 돌지 않는다.** 대상 계약 패키지
(`nh-ad-parser-contracts`)가 `requires-python >=3.12,<3.13` 으로 3.13 을 명시적으로
배제하는데 이 저장소는 `>=3.13` 이다. 그래서 프로젝트 밖 임시 3.12 환경에서 돌린다:

  uv run --no-project --python 3.12 \\
    --with "C:/Users/cccjj/cginside/농협프로젝트/nh-ad-compliance/packages/parser-contracts" \\
    python tools/verify_contract.py

그래서 여기서는 **`nh_parsing` 을 import 하지 않는다** — 표준 라이브러리와 계약
패키지만 쓴다. 입력은 `tools/export_normalized.py` 가 미리 써 둔 파일이다.

합격 기준(2026-08-06 무캐시 실행 기준, HWP 12라인·1영역은 범위 밖이라 제외):
  · 문서 4건이 전부 model_validate 통과, 실패 0
  · 입력 라인 488 = 검증 통과한 textBlock 477 + 빈 텍스트로 걸러 낸 11
  · layoutBlock 224
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from nh_ad_parser_contracts.models import NormalizedDocument

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "out" / "normalized"

EXPECTED = {
    "documents": 4,
    "text_blocks": 477,
    "layout_blocks": 224,
    "dropped_empty_text": 11,
    "band_only": 15,
}


def main() -> int:
    paths = sorted(SRC.glob("*.json"))
    if not paths:
        print(f"입력 없음: {SRC} — 먼저 tools/export_normalized.py 를 돌려야 한다")
        return 2

    failures, text_blocks, layout_blocks = [], 0, 0
    # 좌표 신뢰도가 0.5 미만이면 대상이 표시를 LIST_ONLY 로 떨어뜨린다(results.py:345).
    # 그렇게 되는 원인이 둘이라 반드시 갈라서 세야 한다 — 뭉치면 "스윕이 23건"으로 잘못 읽힌다.
    #   band  : vlm_sweep — 상자가 가로 전체다. 글자는 맞고 위치를 못 믿는다
    #   lowtext: OCR 인식 점수 자체가 낮다. 상자는 촘촘한데 글자를 못 믿는다
    band_only, low_text = 0, 0
    dangling = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            document = NormalizedDocument.model_validate(payload)
        except Exception as exc:  # 어느 문서가 왜 막혔는지 전부 보여 준다
            failures.append((path.stem, exc))
            print(f"✗ {path.stem}\n{exc}\n")
            continue
        text_blocks += len(document.text_blocks)
        layout_blocks += len(document.layout_blocks)
        for block in document.text_blocks:
            if block.coordinate is None or block.coordinate.coordinate_confidence >= 0.5:
                continue
            if block.confidence_score >= 0.5:
                band_only += 1
            else:
                low_text += 1
        # 계약은 relatedTextBlockIds 가 실재하는 textBlockId 를 가리키는지 **안 본다**.
        # 빈 텍스트 라인을 걸러 내면서 참조만 남기면 화면이 없는 블록을 찾게 된다.
        known = {block.text_block_id for block in document.text_blocks}
        dangling.extend(
            f"{path.stem}:{layout.layout_block_id}->{ref}"
            for layout in document.layout_blocks
            for ref in layout.related_text_block_ids
            if ref not in known
        )
        located = sum(1 for block in document.text_blocks if block.coordinate is not None)
        dropped = sum(
            int(warning.message.split("판독 실패 ")[1].split("건")[0])
            for warning in document.warnings
            if warning.code == "TEXT_DETECTED_BUT_UNREADABLE"
        )
        print(
            f"✓ {path.stem[:30]:32s} textBlocks {len(document.text_blocks):4d} "
            f"(좌표 {located:4d}) layoutBlocks {len(document.layout_blocks):4d} "
            f"걸러냄 {dropped:2d} 문서신뢰도 {document.confidence.score:.3f} "
            f"{document.confidence.status.value}"
        )

    print(
        f"\n합계: 문서 {len(paths) - len(failures)}/{len(paths)} 통과 · "
        f"textBlocks {text_blocks} · layoutBlocks {layout_blocks}"
    )
    print(
        f"LIST_ONLY 로 떨어지는 블록 {band_only + low_text} = "
        f"가로전체상자 {band_only} + OCR인식저조 {low_text}"
    )
    if dangling:
        print(f"✗ 없는 textBlock 을 가리키는 참조 {len(dangling)}건: {dangling[:5]}")
    ok = not failures and not dangling \
        and text_blocks == EXPECTED["text_blocks"] \
        and layout_blocks == EXPECTED["layout_blocks"] \
        and band_only == EXPECTED["band_only"] \
        and len(paths) == EXPECTED["documents"]
    if not ok:
        print(f"기대값과 다르다: {EXPECTED}")
    print("합격" if ok else "불합격")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
