# -*- coding: utf-8 -*-
"""out/json → out/normalized (대상 NormalizedDocument v1 dict). 모델 호출 0회.

여기서 만든 파일을 `tools/verify_contract.py` 가 **프로젝트 밖 3.12 환경**에서 진짜
계약 모델로 검증한다. 두 단계로 갈라 놓은 이유는 파이썬 버전이 겹치지 않기 때문이다 —
우리는 3.13(`requires-python >=3.13`), 대상 계약 패키지는 `>=3.12,<3.13`.

사용:
  uv run python tools/export_normalized.py
  uv run python tools/export_normalized.py --json     # 기계 판독용 집계
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from nh_parsing.normalized_export import export_normalized_document

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
SRC = OUT / "json"
DST = OUT / "normalized"

# 신원 2개는 원래 대상 워커의 요청에서 온다(pr-plan §6-3). 단독 실행에서는 문서마다
# 재현 가능한 값을 만들어 넣는다 — 검증만 하는 자리라 값 자체에 뜻은 없다.
REVIEW_ID = "review-local-verify"


def _identity(stem: str) -> tuple[str, str]:
    return f"file-{stem}", REVIEW_ID


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="집계를 JSON 으로 stdout 에")
    args = ap.parse_args()

    if not SRC.is_dir():
        print(f"입력 없음: {SRC} — 먼저 tools/run_nhdata.py 를 돌려야 한다", file=sys.stderr)
        return 2

    DST.mkdir(parents=True, exist_ok=True)
    for stale in DST.glob("*.json"):
        stale.unlink()

    # 같은 입력이면 같은 출력이 나와야 한다(재처리 정책, 흐름문서 §7-4). createdAt 만
    # 실행 시각이라 여기서 한 번 고정해 문서 전체가 같은 값을 쓰게 한다.
    stamp = datetime.now(timezone.utc)

    rows, totals, skipped = [], {}, []
    for path in sorted(SRC.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        source_file_id, review_id = _identity(path.stem)
        result = export_normalized_document(
            document,
            source_file_id=source_file_id,
            review_id=review_id,
            # 진단 산출물(좌표·후보 판독·notes)이 그대로 남아 있는 자리를 가리킨다.
            raw_artifact_ref=f"nh-ad-review-poc:out/json/{path.name}",
            created_at=stamp,
        )
        if result.skipped:
            skipped.append({"doc": path.stem, "reason": result.skipped})
            continue
        assert result.document is not None
        (DST / path.name).write_text(
            json.dumps(result.document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        rows.append({"doc": path.stem, **result.stats})
        for key, value in result.stats.items():
            totals[key] = totals.get(key, 0) + value

    summary = {"documents": len(rows), "rows": rows, "totals": totals, "skipped": skipped}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"{'문서':30s} {'라인입력':>6s} {'블록':>5s} {'빈텍스트':>7s} "
          f"{'스윕':>4s} {'영역':>5s} {'digital채움':>10s} {'rules채움':>9s}")
    for row in rows:
        print(f"{row['doc'][:28]:30s} {row['lines_in']:6d} {row['text_blocks']:5d} "
              f"{row['dropped_empty_text']:7d} {row['sweep_blocks']:4d} "
              f"{row['layout_blocks']:5d} {row['digital_filled_confidence']:10d} "
              f"{row['rules_filled_role_confidence']:9d}")
    print(f"\n합계: {totals}")
    for row in skipped:
        print(f"제외  {row['doc'][:28]:30s} {row['reason']}")
    print(f"\n{len(rows)}건을 {DST} 에 썼다. 계약 검증은:")
    print('  uv run --no-project --python 3.12 \\\n'
          '    --with "C:/Users/cccjj/cginside/농협프로젝트/nh-ad-compliance/packages/parser-contracts" \\\n'
          "    python tools/verify_contract.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
