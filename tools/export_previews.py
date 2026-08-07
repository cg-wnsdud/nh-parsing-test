"""검수 화면과 같은 그림을 **이미지 파일로** 뽑는다 — 영역 라벨(`r002 유의사항`)까지 찍어서.

**왜 따로 만드나.** `out/previews/*.jpg`(파싱이 만드는 정본 미리보기)에는 박스만 있고
라벨이 없다. 라벨은 `review.html` 이 HTML div 로 덧씌우는 것이라 그림 파일에는 안 남는다.
파싱 쪽에 라벨을 구워 넣지 않는 이유는 `pipeline._save_preview` 주석에 있다 — 역할은 VLM
판단이라 실행 간 97.3% 일치(225개 중 6개 변동)여서, 좌표 그림에 박으면 "같은 광고인데
실행마다 다른 그림"이 남는다.

그래서 **정본은 그대로 두고 파생본을 따로 낸다.** 이 도구는 `out/json`(좌표·역할)과
`out/previews`(박스 그림)만 읽어 합치므로 **모델 호출이 0회**이고 재파싱도 필요 없다.
파일을 남한테 보내야 할 때(브라우저 없이 그림만 필요할 때) 쓴다.

    uv run python tools/export_previews.py                 # → out/previews_labeled/
    uv run python tools/export_previews.py --only 올원e
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.parent

# review.html 의 `.rlbl` 과 같은 색 — 두 화면을 나란히 놓고 봐도 같은 것으로 읽히게.
_LBL_BG = (190, 0, 130)
_LBL_BG_EMPTY = (190, 0, 130, 90)   # 글자 안 붙은 빈 검출 박스는 흐리게
_LBL_FG = (255, 255, 255)
_FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/malgun.ttf"),      # 맑은 고딕 (Windows 기본)
    Path("C:/Windows/Fonts/gulim.ttc"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """한글이 깨지면 그림의 뜻이 사라지므로, 폰트를 못 찾으면 **조용히 넘어가지 않는다**."""
    for path in _FONT_CANDIDATES:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    raise SystemExit(
        "한글 폰트를 찾지 못했습니다. 아래 중 하나가 필요합니다:\n  "
        + "\n  ".join(str(p) for p in _FONT_CANDIDATES)
    )


def _place_labels(page: dict) -> list[tuple[int, int, str, bool]]:
    """(x, y, 표시문구, 빈박스여부) 목록. 겹치면 아래로 밀어내는 규칙은 review.html 과 동일.

    make_review._region_label_overlay 와 같은 규칙을 쓴다(같은 그림으로 읽혀야 하므로):
      · 빈 검출 박스는 먼저 그리고 흐리게 — 실제 영역 라벨을 가리지 않게
      · 실제 영역끼리 원점이 가까우면(x 45px·y 24px 이내) 아래로 24px 밀어낸다
    """
    empties: list[tuple[int, int, str, bool]] = []
    reals: list[tuple[int, int, str, bool]] = []
    placed: list[tuple[int, int]] = []
    for r in page.get("regions", []):
        bbox = r.get("bbox")
        if not bbox:
            continue
        x0, y0 = int(bbox[0]), int(bbox[1])
        short = r["region_id"].split("_", 1)[-1]
        if not r.get("lines"):
            empties.append((x0, y0, f"{short} 빈 박스", True))
            continue
        y = y0
        while any(abs(x0 - px) < 45 and abs(y - py) < 24 for px, py in placed):
            y += 24
        placed.append((x0, y))
        reals.append((x0, y, f"{short} {r.get('role') or '?'}", False))
    return empties + reals


def _draw_page(preview: Path, page: dict, out_path: Path) -> str:
    img = Image.open(preview).convert("RGB")
    cw, ch = page.get("canvas_w") or 0, page.get("canvas_h") or 0
    if not (cw and ch):
        return "캔버스 좌표 없음 — 건너뜀"
    # 미리보기는 원본 캔버스를 폭 1400 으로 줄인 것이다(_save_preview). 좌표도 같이 줄인다.
    scale = img.width / cw
    font = _load_font(max(9, round(11 * scale) if scale < 1 else 11))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    labels = _place_labels(page)
    for x0, y0, text, is_empty in labels:
        x, y = round(x0 * scale), round(y0 * scale)
        l, t, r, b = draw.textbbox((x, y), text, font=font)
        pad = 2
        box = (l - pad, t - pad, r + pad, b + pad)
        draw.rectangle(box, fill=(*_LBL_BG, 224) if not is_empty else _LBL_BG_EMPTY)
        draw.text((x, y), text, font=font, fill=(*_LBL_FG, 255 if not is_empty else 170))
    out = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, quality=88)
    return f"라벨 {len(labels)}개"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=ROOT / "out",
                    help="run_nhdata --out 과 대칭. json/ 과 previews/ 를 읽는다")
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "previews_labeled")
    ap.add_argument("--only", default=None, help="파일명 일부로 문서 하나만")
    args = ap.parse_args()

    json_files = sorted((args.src / "json").glob("*.json"))
    if args.only:
        json_files = [p for p in json_files if args.only in p.stem]
    if not json_files:
        print(f"{args.src / 'json'} 에 결과 없음 — 먼저 run_nhdata.py 실행", file=sys.stderr)
        raise SystemExit(1)

    made = skipped = 0
    for jf in json_files:
        doc = json.loads(jf.read_text(encoding="utf-8"))
        doc_id = doc["doc_id"]
        for page in doc.get("pages", []):
            name = f"{doc_id}_p{page['page_no']}.jpg"
            preview = args.src / "previews" / name
            if not preview.exists():
                # HWP 처럼 캔버스가 없는 문서는 미리보기 자체가 없다 — 결함이 아니다.
                print(f"  · {name}  미리보기 없음(좌표 없는 입력) — 건너뜀")
                skipped += 1
                continue
            note = _draw_page(preview, page, args.out / name)
            print(f"  · {name}  {note}")
            made += 1
    print(f"\n→ {args.out}  ({made}장 생성, {skipped}장 건너뜀)")


if __name__ == "__main__":
    main()
