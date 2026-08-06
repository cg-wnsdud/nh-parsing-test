"""NH 광고심의 PoC — 파싱 파이프라인.

광고물 파일(PDF/PNG/HWP) → 구조화 IR(`out/json`) → LLM 투영(`out/llm_view`) →
스키마 필드(`out/extracted`).

읽는 순서:
  docs/변경정리_2026-08-06.md               최근에 무엇을 왜 바꿨나
  docs/parsing-output-report.md            실제로 무엇이 나왔나 (숫자·시간)
  docs/architecture/pipeline-walkthrough.md 코드가 실제로 하는 일 (좌표·실값)

초기 설계서는 docs/previous/parsing-pipeline-design_v0.1_2026-07-16.md 로 옮겨졌다
(현행 동작과 어긋나는 부분이 있으므로 walkthrough 를 정본으로 볼 것).
"""
