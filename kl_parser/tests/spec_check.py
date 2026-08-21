# -*- coding: utf-8 -*-
"""규격 검사기 — 우리가 만든 `_hrc.jsonl`/`_hrc.json` 이 농협 규격을 지키는지 본다.

규격 근거는 `readme.md` "Output jsonl" 단락(줄 163~176)이다. 각 검사에 그 조항을 붙였다.
**농협 정답 샘플(`out_sample_hrc.jsonl`)이 규격서와 어긋나는 항목은 경고로만 낸다** —
어느 쪽이 진실인지는 방문 확인 사항이다.
"""

import json
import sys
from pathlib import Path

# 규격서: "item 유형은 text, table, image, h1 ~ h4 입니다."
SPEC_ITEMS = {"text", "table", "image", "h1", "h2", "h3", "h4"}
# 정답 샘플에는 있으나 규격서에 없는 유형. 미확정 대장 항목.
SAMPLE_ONLY_ITEMS = {"break"}


def check_jsonl(path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warns: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return ["빈 파일"], warns

    for n, raw in enumerate(lines, 1):
        if not raw.strip():
            errors.append(f"{n}행: 빈 줄 (jsonl 은 행마다 객체여야 한다)")
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            errors.append(f"{n}행: JSON 아님 — {e}")
            continue

        kind = obj.get("item")
        if kind is None:
            errors.append(f"{n}행: `item` 없음")
            continue
        if kind not in SPEC_ITEMS:
            if kind in SAMPLE_ONLY_ITEMS:
                warns.append(f"{n}행: `{kind}` 는 정답 샘플에만 있고 규격서엔 없다")
            else:
                errors.append(f"{n}행: `item`={kind} 은 규격 밖 (허용: {sorted(SPEC_ITEMS)})")

        # 규격서: "item 의 value 는 필수값입니다."
        if "value" not in obj:
            errors.append(f"{n}행: `value` 없음 (필수)")
        elif kind != "break" and not str(obj["value"]).strip():
            errors.append(f"{n}행: `value` 가 비었다 (item={kind})")

        # 규격서: "table item 인 경우 type_property.title 값이 필수"
        if kind == "table":
            tp = obj.get("type_property") or {}
            if "title" not in tp:
                errors.append(f"{n}행: table 인데 `type_property.title` 없음 (필수, 없으면 \"\")")
            if not str(obj.get("value", "")).lstrip().startswith("<table"):
                errors.append(f"{n}행: table `value` 가 HTML `<table>` 이 아니다")

        # 규격서: "image item 의 경우 type_property 의 title, width, height, ratio 값이 필수"
        if kind == "image":
            tp = obj.get("type_property") or {}
            for key in ("title", "width", "height", "ratio"):
                if key not in tp:
                    errors.append(f"{n}행: image 인데 `type_property.{key}` 없음 (필수)")

        # 규격서: "page 번호를 알 수 없는 경우에는 0 으로 입력합니다."
        if kind in ("text", "table", "image") and "page" in obj:
            if not isinstance(obj["page"], int):
                errors.append(f"{n}행: `page` 가 정수가 아니다 ({obj['page']!r})")

        # 규격서: cust_meta 는 key-value 목록, 이름은 cust_attr1~5 / cust_sattr1~5
        for m in obj.get("cust_meta") or []:
            if not isinstance(m, dict) or "name" not in m or "value" not in m:
                errors.append(f"{n}행: `cust_meta` 항목이 {{name, value}} 형식이 아니다")
                continue
            name = m["name"]
            if not (name.startswith("cust_attr") or name.startswith("cust_sattr")):
                errors.append(f"{n}행: `cust_meta.name`={name} 이 규격 이름이 아니다")

    # h1 이 하나도 없으면 KL 에서 본문에 상위 meta 가 안 붙는다 — 경고
    kinds = {json.loads(r).get("item") for r in lines if r.strip()}
    if not (kinds & {"h1", "h2", "h3", "h4"}):
        warns.append("헤딩(h1~h4)이 하나도 없다 — KL 이 본문에 meta 를 못 붙인다")
    elif "h1" not in kinds:
        warns.append("h1 이 없다 (h2 이하만 있음) — 최상위 meta 가 비게 된다")
    return errors, warns


def check_info(path: Path) -> list[str]:
    """`_hrc.json` — 정답 샘플의 키를 기준으로 본다(규격서에 필수 목록이 없다)."""
    errors: list[str] = []
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"JSON 아님 — {e}"]
    for key in ("name", "file_type", "size", "parsed_status"):
        if key not in obj:
            errors.append(f"`{key}` 없음 (정답 샘플에 있는 키)")
    if obj.get("parsed_status") not in ("S", "F", None):
        errors.append(f"`parsed_status`={obj.get('parsed_status')!r} — 샘플은 'S'")
    return errors


def main(target: Path) -> int:
    files = sorted(target.glob("*_hrc.jsonl")) if target.is_dir() else [target]
    if not files:
        print(f"검사할 `*_hrc.jsonl` 이 없다: {target}")
        return 2
    bad = 0
    for f in files:
        errs, warns = check_jsonl(f)
        info = f.with_name(f.name.replace("_hrc.jsonl", "_hrc.json"))
        ierrs = check_info(info) if info.exists() else ["_hrc.json 이 없다"]
        n = len(f.read_text(encoding="utf-8").splitlines())
        mark = "OK " if not (errs or ierrs) else "NG "
        print(f"[{mark}] {f.name}  ({n}행)")
        for e in errs + [f"_hrc.json: {x}" for x in ierrs]:
            print(f"        ✗ {e}")
        for w in warns:
            print(f"        ⚠ {w}")
        if errs or ierrs:
            bad += 1
    print(f"\n검사 {len(files)}건 · 실패 {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
