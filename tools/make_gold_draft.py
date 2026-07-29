# -*- coding: utf-8 -*-
"""골드셋 초안 생성기 — 현재 파싱 결과(out/json)로 gold/*.yaml 초안을 만든다.

⚠ 초안은 '타이핑 절약용 비계'일 뿐, 정답의 기준은 항상 원본 파일이다.
   사람이 원본(nh-data)과 프리뷰(out/previews)를 보면서 초안을 교정해야 골드셋이 된다.
   이미 존재하는 gold/*.yaml 은 절대 덮어쓰지 않는다(사람 수정 보호).
"""

import json
import sys
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).parent.parent
GOLD = ROOT / "gold"
GOLD.mkdir(exist_ok=True)

HEADER = """\
# ─────────────────────────────────────────────────────────────────────
# 골드셋(정답지) — 이 파일은 현재 파싱 결과로 채운 '초안'입니다.
# 원본 파일과 프리뷰(out/previews)를 옆에 놓고 아래 3가지를 교정하세요:
#
# 1) sections : 이 파일에 "있어야 하는" 의미 섹션 목록
#    - type 이 틀리면 고치고, 파서가 잘못 만든 섹션은 줄을 지우고,
#      파서가 놓친 섹션은 새로 추가하세요 (box 는 대략적인 [x0,y0,x1,y1],
#      프리뷰 이미지 좌표 기준. 정밀하지 않아도 됨 — 겹침으로 채점).
#    - 사용 가능한 type: 헤드라인/상품안내/우대혜택/이벤트안내/참여방법/경품안내/
#      당첨자안내/이벤트유의사항/상품유의사항/고지문구/행동유도/장식예시/기타
#
# 2) must_contain : "심의 관점에서 반드시 추출돼야 하는 문장"들
#    - ★ 원본 기준으로 교정하는 것이 핵심입니다. 초안에는 OCR 오류가 그대로
#      들어있을 수 있습니다 (예: '최고연71%' → 원본이 '최고 연 7.1%'면 고치세요).
#      그렇게 고쳐두면 채점기가 그 OCR 오류를 자동으로 잡아냅니다.
#    - 문장 전체가 아니라 핵심 구절만 있어도 됩니다 (부분일치 채점).
#    - 파일당 10~20개면 충분합니다. 불필요한 항목은 지우세요.
#
# 3) fields : 정확히 추출돼야 하는 핵심 값 (원본과 대조해 값 교정)
#    - key: 심의필번호/금리/우대금리/가입기간/가입금액/가입대상/대출한도/
#           이벤트기간/기타중요수치
#
# 채점 실행:  uv run python tools/evaluate.py
# ─────────────────────────────────────────────────────────────────────
"""


def main() -> None:
    for jf in sorted((ROOT / "out" / "json").glob("*.json")):
        gold_path = GOLD / (jf.stem + ".yaml")
        if gold_path.exists():
            print(f"skip (이미 존재, 보호): {gold_path.name}")
            continue
        d = json.loads(jf.read_text(encoding="utf-8"))
        pages = []
        for pg in d["pages"]:
            sections = [
                {"type": s["section_type"], "no": s.get("section_no", 1), "box": s["bbox"]}
                for s in pg.get("sections", [])
            ]
            must = []
            for r in pg["regions"]:
                if r["role"] in ("유의사항", "고지문구"):
                    for line in r["lines"]:
                        t = line["text"].strip()
                        if len(t) >= 8:
                            must.append(t)
            fields = []
            seen = set()
            for f in pg.get("extracted_fields", []):
                key = (f["key"], f["value"].strip())
                if key not in seen:
                    seen.add(key)
                    fields.append({"key": f["key"], "value": f["value"].strip()})
            pages.append(
                {
                    "page_no": pg["page_no"],
                    "sections": sections,
                    "must_contain": must[:20],
                    "fields": fields,
                }
            )
        gold = {
            "file": d["source_file"],
            "product_group": d.get("product_group"),
            "ad_type": d.get("ad_type"),
            "pages": pages,
        }
        text = HEADER + yaml.safe_dump(
            gold, allow_unicode=True, sort_keys=False, width=110
        )
        gold_path.write_text(text, encoding="utf-8")
        print(f"생성: {gold_path.name}")


if __name__ == "__main__":
    main()
