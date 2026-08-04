# -*- coding: utf-8 -*-
"""out/json → out/llm_view 재생성 (순수 코드, 모델 호출 0회).

`llm_view` 는 `out/json`(파싱 전체 기록)에서 **코드로만** 유도되는 투영이다
(좌표·신뢰도를 빼고 읽기순서 텍스트만 남긴 것 — llm_view.build_doc_view).
그런데 이 투영 규칙을 고칠 때마다 run_nhdata.py 전체(OCR+VLM 15분)를 다시 돌려야
llm_view 가 갱신됐다. 파싱 결과가 그대로인데 재파싱하는 것은 낭비이고, 재파싱은
VLM 판단이 섞여 **의도한 변경 외의 차이까지 만든다**(비교가 안 된다).

이 도구는 out/json 을 입력으로 llm_view 만 다시 쓴다. 투영 규칙 변경의 A/B 에 쓴다.
STAGE_3 결과(out/extracted)는 별개이므로 필요하면 이어서 run_extract.py 를 돌린다.

사용:
  uv run python tools/rebuild_views.py            # out/json → out/llm_view
  uv run python tools/rebuild_views.py --only 003
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nh_parsing.ir import AdDocument  # noqa: E402
from nh_parsing.llm_view import build_doc_view  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="파일명에 이 문자열이 포함된 문서만")
    ap.add_argument("--src", type=Path, default=ROOT / "out" / "json")
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "llm_view")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    files = sorted(args.src.glob("*.json"))
    if args.only:
        files = [p for p in files if args.only in p.stem]
    if not files:
        print(f"{args.src} 에 파싱 결과 없음 — 먼저 run_nhdata.py 실행")
        return

    for path in files:
        doc = AdDocument(**json.loads(path.read_text(encoding="utf-8")))
        view = build_doc_view(doc)
        dest = args.out / path.name
        dest.write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
        n_region = sum(len(p["regions"]) for p in view["pages"])
        print(f"→ {dest.name}  (페이지 {len(view['pages'])} · 영역 {n_region})")
    print(f"\n{len(files)}개 재생성. STAGE_3 를 다시 맞추려면: uv run python tools/run_extract.py")


if __name__ == "__main__":
    main()
