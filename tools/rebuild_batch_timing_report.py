# -*- coding: utf-8 -*-
"""중단·재개 후에도 이벤트 로그에서 파일별 실제 벽시계 시간을 복원한다.

`run_parsing_batch.py`는 결과가 확정될 때마다 file_succeeded 이벤트에
"<초>초, 경고 <건>건"을 남긴다. 이 도구는 그 불변 이벤트 로그를 읽어 전수 시간표를
만든다. 따라서 재개할 때 progress.json의 상세 metrics가 축약된 과거 실행도 빠지지
않는다.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


_MESSAGE = re.compile(r"(?P<elapsed>[0-9.]+)초,\s*경고\s*(?P<warnings>\d+)건")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("0827-parsing"))
    args = ap.parse_args()
    out = args.out
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    progress = json.loads((out / "progress.json").read_text(encoding="utf-8"))
    by_key = {entry["key"]: entry for entry in progress.get("entries") or []}
    completed: dict[str, dict] = {}
    for raw in (out / "run-events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(raw)
        if event.get("event") != "file_succeeded" or not event.get("key"):
            continue
        match = _MESSAGE.search(event.get("message") or "")
        if not match:
            continue
        # 같은 파일을 다시 확정한 경우(향후 수동 재실행)는 최신 성공 시간을 쓴다.
        completed[event["key"]] = {
            "finished_at": event.get("at"),
            "elapsed_s": float(match["elapsed"]),
            "warning_count": int(match["warnings"]),
        }

    rows = []
    for index, item in enumerate(manifest.get("files") or [], start=1):
        info = completed.get(item["key"], {})
        entry = by_key.get(item["key"], {})
        rows.append({
            "index": index,
            "key": item["key"],
            "source_file": item["source_file"],
            "result_id": item["result_id"],
            "status": entry.get("status", "unknown"),
            "elapsed_s": info.get("elapsed_s"),
            "warning_count": info.get("warning_count", entry.get("warning_count", 0)),
            "finished_at": info.get("finished_at"),
        })

    (out / "file-timings.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    table = [
        "# 파일별 실행 시간",
        "",
        "이 값은 파일이 최종 성공으로 확정될 때 기록한 실제 벽시계 시간입니다.",
        "",
        "| 순번 | 입력 | 상태 | 시간(초) | 경고 | 완료 시각(UTC) |",
        "| ---: | --- | --- | ---: | ---: | --- |",
    ]
    for row in rows:
        seconds = f"{row['elapsed_s']:.1f}" if row["elapsed_s"] is not None else "—"
        table.append(
            f"| {row['index']} | {row['key']} | {row['status']} | {seconds} | "
            f"{row['warning_count']} | {row['finished_at'] or '—'} |"
        )
    (out / "file-timings.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    timed = [row["elapsed_s"] for row in rows if row["elapsed_s"] is not None]
    print(f"→ {out / 'file-timings.md'} ({len(rows)}개, 시간 복원 {len(timed)}개, 합계 {sum(timed):.1f}초)")


if __name__ == "__main__":
    main()
