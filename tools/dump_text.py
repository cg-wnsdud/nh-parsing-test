# -*- coding: utf-8 -*-
"""추출 텍스트 확인용 임시 덤프 — out/json (AdPageIR) → out/text/*.txt.

AdPageIR 은 좌표·신뢰도·출처까지 담느라 수천 줄이라 "뭐가 뽑혔는지" 눈으로
확인하기 어렵다. 이 도구는 텍스트만 읽기 순서(위→아래)로 나열한다.

사용:
  uv run python tools/dump_text.py                # 전체 → out/text/*.txt
  uv run python tools/dump_text.py --only 001     # 파일명 부분 일치
  uv run python tools/dump_text.py --annotate     # [출처 신뢰도] 주석 포함
"""

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nh_parsing.ir import Line              # noqa: E402
from nh_parsing.tiling import sort_reading_order  # noqa: E402


def _sorted_lines(regs: list[dict]) -> list[dict]:
    """섹션 영역들의 라인을 모아 국소 읽기순서 재정렬 (영역 간 순서 교정)."""
    lines = [l for r in regs for l in r.get("lines", [])]
    ordered = sort_reading_order([Line(**l) for l in lines])
    return [o.model_dump() for o in ordered]


def _section_blocks(page: dict) -> list[tuple[str, list[dict]]]:
    """페이지를 (블록 제목, 영역 목록) 시퀀스로 — 묶음(카드) → 섹션 → 영역 순서.

    JSON 의 sections 는 group_no(시각적 묶음: SNS 카드/패널)를 가진다.
    단순 y 정렬은 좌우 배치 카드의 내용을 가로질러 섞어 읽으므로,
    묶음 우선(같은 카드 내용을 연속으로) → 묶음 내 y 순으로 나열한다.
    """
    regions = {r["region_id"]: r for r in page.get("regions", [])}
    sections = page.get("sections", [])

    def sec_y(s: dict) -> int:
        return s["bbox"][1] if s.get("bbox") else (1 << 30)

    blocks: list[tuple[str, list[dict]]] = []
    used: set[str] = set()
    for s in sorted(sections, key=lambda s: (s.get("group_no") or 0, sec_y(s))):
        grp = f"묶음{s['group_no']} · " if s.get("group_no") else ""
        title = f"{grp}{s['section_type']}#{s['section_no']}"
        regs = [regions[rid] for rid in s.get("region_ids", []) if rid in regions]
        regs.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]) if r.get("bbox") else (0, 0))
        used.update(r["region_id"] for r in regs)
        blocks.append((title, regs))

    leftovers = [r for r in page.get("regions", []) if r["region_id"] not in used]
    if leftovers:
        leftovers.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]) if r.get("bbox") else (0, 0))
        blocks.append(("섹션 미지정 영역", leftovers))
    return blocks


def dump_doc(parsed: dict, annotate: bool) -> tuple[str, dict]:
    out: list[str] = []
    stats = {"lines": 0, "chars": 0, "low_conf": 0, "fields": 0}
    out.append(
        f"# {parsed['doc_id']} ({parsed['file_type']}) — "
        f"분류: {parsed.get('product_group')}/{parsed.get('ad_type')} "
        f"(source={parsed.get('category_source')})"
    )

    def emit(line: dict) -> None:
        text = line.get("text", "")
        stats["lines"] += 1
        stats["chars"] += len(text)
        conf = line.get("confidence")
        if conf is not None and conf < 0.8:
            stats["low_conf"] += 1
        if annotate:
            tag = f"{line.get('source', '?')[:1]}"
            if conf is not None:
                tag += f" {conf:.2f}"
            out.append(f"  ({tag}) {text}")
        else:
            out.append(text)

    for page in parsed.get("pages", []):
        n_lines = sum(len(r.get("lines", [])) for r in page.get("regions", [])) + len(
            page.get("unassigned_lines", [])
        )
        out.append(
            f"\n## p{page['page_no']} — route={page.get('parse_route')} "
            f"status={page.get('parse_status')} "
            f"canvas={page.get('canvas_w')}x{page.get('canvas_h')} "
            f"라인 {n_lines}개"
        )
        for title, regs in _section_blocks(page):
            out.append(f"\n--- [{title}] ---")
            for line in _sorted_lines(regs):
                emit(line)
        unassigned = page.get("unassigned_lines", [])
        if unassigned:
            out.append("\n--- [미배정 — 어느 섹션에도 좌표 귀속 안 됨] ---")
            for line in unassigned:
                emit(line)

        fields = page.get("extracted_fields", [])
        stats["fields"] += len(fields)
        if fields:
            out.append(f"\n### 추출 필드 ({len(fields)}개)")
            for f in fields:
                extra = ""
                if annotate:
                    extra = (
                        f"  [extractor={f.get('extractor')} "
                        f"ocr_backed={f.get('ocr_backed')}]"
                    )
                out.append(f"- {f['key']}: {f['value']}{extra}")
    return "\n".join(out), stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument("--annotate", action="store_true")
    args = parser.parse_args()

    out_dir = ROOT / "out" / "text"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_files = sorted((ROOT / "out" / "json").glob("*.json"))
    if args.only:
        json_files = [p for p in json_files if args.only in p.stem]
    if not json_files:
        print("out/json 에 결과 없음 — 먼저 run_nhdata.py 실행")
        return

    print(f"{'파일':<44} {'라인':>5} {'문자':>7} {'저신뢰':>5} {'필드':>4}")
    for jf in json_files:
        parsed = json.loads(jf.read_text(encoding="utf-8"))
        text, stats = dump_doc(parsed, args.annotate)
        suffix = "_annotated" if args.annotate else ""
        out_path = out_dir / f"{jf.stem}{suffix}.txt"
        out_path.write_text(text, encoding="utf-8")
        name = jf.stem if len(jf.stem) <= 42 else jf.stem[:40] + "…"
        print(
            f"{name:<44} {stats['lines']:>5} {stats['chars']:>7} "
            f"{stats['low_conf']:>5} {stats['fields']:>4}"
        )
    print(f"\n→ {out_dir}")


if __name__ == "__main__":
    main()
