# -*- coding: utf-8 -*-
"""추출 결과를 gold 의 fields(사람이 원본 보고 적은 값)와 대조.

수치 지표만 내지 않고, 어떤 값이 어디서 잡혔는지/안 잡혔는지 목록으로 보여준다.
매칭은 정규화 후 부분일치 — gold 값이 추출된 값들 중 어딘가에 담겼으면 회수된 것으로 본다.
"""
import json
import re
import sys
from pathlib import Path

import yaml

# 출력을 파일/파이프로 넘기면 stdout 이 cp949 로 잡혀 '—' 같은 문자에서 죽는다
# (run_nhdata.py·run_extract.py 와 동일 처리). 자동화에서 채점이 통째로 날아간 실측.
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]


def norm(s: str) -> str:
    """공백·인용부호·괄호류 차이를 흡수. 숫자·문자는 유지."""
    s = str(s)
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    s = s.replace("`", "").replace("'", "").replace('"', "")
    s = re.sub(r"\s+", "", s)
    return s


def _numeric_core(s: str) -> str:
    """수치·단위만 남긴다. '최대 연 3.6%' → '3.6%' 처럼 수식어·구두점 차이를 걷어내
    '값은 잡혔는데 표기만 다른' 경우를 진짜 누락과 구분하기 위한 보조 키."""
    s = norm(s)
    nums = re.findall(r"\d[\d,\.]*\s*(?:%p|%|원|명|팀|개월|좌)?", s)
    return "|".join(x.replace(",", "").rstrip(".") for x in nums if x)


def flatten_values(res: dict) -> list[tuple[str, str]]:
    """추출 결과에서 (출처필드, 값문자열) 목록."""
    out: list[tuple[str, str]] = []
    for key, v in (res.get("fields") or {}).items():
        val = v.get("value")
        if isinstance(val, list):
            for item in val:
                out.append((key, str(item)))
        elif val:
            out.append((key, str(val)))
    for i, ev in enumerate(res.get("events") or [], 1):
        for key, v in ev.items():
            val = v.get("value") if isinstance(v, dict) else v
            if isinstance(val, list):
                for item in val:
                    out.append((f"event{i}.{key}", str(item)))
            elif val:
                out.append((f"event{i}.{key}", str(val)))
    for key, lst in (res.get("observations") or {}).items():
        for o in lst:
            out.append((f"obs.{key}", o.get("quote", "")))
    for u in res.get("unmapped") or []:
        out.append(("unmapped", u.get("text", "")))
    return out


def main() -> None:
    gold_dir = ROOT / "gold"
    ext_dir = ROOT / "out" / "extracted"
    total_hit = total = 0

    for gpath in sorted(gold_dir.glob("*.yaml")):
        gold = yaml.safe_load(gpath.read_text(encoding="utf-8"))
        if gold.get("product_group") != "예금성":
            continue
        epath = ext_dir / (gpath.stem + ".json")
        if not epath.exists():
            print(f"— 추출 결과 없음: {gpath.stem}")
            continue
        res = json.loads(epath.read_text(encoding="utf-8"))
        pairs = flatten_values(res)
        haystack = [(k, norm(v)) for k, v in pairs]

        gold_fields = []
        for page in gold.get("pages", []):
            for f in page.get("fields", []) or []:
                gold_fields.append((f.get("key"), str(f.get("value"))))

        print(f"\n{'='*78}\n■ {gpath.stem}  (ad_type={gold.get('ad_type')} / 추출={res.get('ad_type')})")
        print(f"  gold 기재 값 {len(gold_fields)}개 대조")
        hit = partial = 0
        for gkey, gval in gold_fields:
            n = norm(gval)
            found_in = [k for k, h in haystack if n and n in h]
            if found_in:
                hit += 1
                where = found_in[0] + (f" 외{len(found_in)-1}" if len(found_in) > 1 else "")
                print(f"   O {gkey}: {gval[:52]}   ← {where}")
                continue
            # 표기 차이(수식어·구두점)와 진짜 누락을 구분한다 — 숫자 지표만 믿으면 오판한다.
            core = _numeric_core(gval)
            near = [k for k, h in haystack if core and core in _numeric_core(h)] if core else []
            if near:
                partial += 1
                print(f"   △ {gkey}: {gval[:52]}   ← {near[0]} (수치 일치, 표기 다름)")
            else:
                print(f"   X {gkey}: {gval[:52]}   ← 어디에도 없음")
        total_hit += hit + partial
        total += len(gold_fields)
        c = res.get("coverage", {})
        print(f"  → 회수 {hit + partial}/{len(gold_fields)} (완전일치 {hit}, 표기차이 {partial}) | "
              f"근거커버리지 {c.get('region_coverage')} | "
              f"미배정 {c.get('unmapped_total')} | status보정 {len(res.get('status_corrections', []))}건")
        gap = [u for u in (res.get("unmapped") or []) if u.get("kind") == "심의관련_필드없음"]
        if gap:
            print("  스키마 공백 후보:")
            for u in gap:
                print(f"     · {u['text'][:60]} :: {u.get('reason','')[:60]}")

    print(f"\n{'='*78}\n총 회수: {total_hit}/{total}")


if __name__ == "__main__":
    main()
