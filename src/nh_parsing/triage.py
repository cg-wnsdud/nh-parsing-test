from __future__ import annotations

"""PDF 페이지 단위 triage [B1] — 설계서 4.2절.

document-processor 의 probe 기준(정상문자 비율, U+FFFD, 이미지 면적비)을
pypdfium2 로 재현한다. 프로토타입에서는 이중 렌더를 피하려고 인라인 구현했고,
프로덕션에서는 사내 파서 probe 를 그대로 감싸도 된다.

핵심 원칙(조용한 실패 금지): 모든 페이지는 반드시 structured / scan_like /
hybrid 중 하나로 판정되고 판정 근거가 AdPage.triage 에 남는다.
"""

import unicodedata
from dataclasses import dataclass, field

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .config import SETTINGS
from .ir import Line


@dataclass
class PageTriage:
    verdict: str                      # structured | scan_like | hybrid
    char_count: int
    korean_count: int
    fffd_count: int
    image_area_ratio: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "char_count": self.char_count,
            "korean_count": self.korean_count,
            "fffd_count": self.fffd_count,
            "image_area_ratio": round(self.image_area_ratio, 3),
            "reasons": self.reasons,
        }


def _image_area_ratio(page: pdfium.PdfPage) -> float:
    """페이지 내 이미지 오브젝트 면적 합 / 페이지 면적 (겹침 미보정 근사)."""
    w, h = page.get_size()
    page_area = w * h
    if page_area <= 0:
        return 0.0
    total = 0.0
    try:
        for obj in page.get_objects(max_depth=2):
            if obj.type != pdfium_c.FPDF_PAGEOBJ_IMAGE:
                continue
            left, bottom, right, top = obj.get_bounds()  # pypdfium2 v5 (v4: get_pos)
            total += max(0.0, right - left) * max(0.0, top - bottom)
    except Exception:
        return 0.0
    return min(1.0, total / page_area)


def triage_page(page: pdfium.PdfPage) -> PageTriage:
    textpage = page.get_textpage()
    text = textpage.get_text_bounded() or ""
    stripped = "".join(ch for ch in text if not ch.isspace())
    char_count = len(stripped)
    korean = sum(1 for c in stripped if "가" <= c <= "힣")
    fffd = stripped.count("�")
    img_ratio = _image_area_ratio(page)

    reasons: list[str] = []
    verdict = "structured"
    if char_count <= SETTINGS.min_readable_chars:
        verdict = "scan_like"
        reasons.append(f"텍스트 {char_count}자 이하 — 텍스트 레이어 불신")
    elif fffd >= SETTINGS.min_fffd_count and fffd / max(char_count, 1) >= SETTINGS.max_fffd_ratio:
        verdict = "scan_like"
        reasons.append(f"U+FFFD {fffd}개({fffd / char_count:.0%}) — 인코딩 깨짐")
    elif char_count >= 50 and _normal_ratio(stripped) <= 0.1:
        verdict = "scan_like"
        reasons.append("정상 문자 비율 10% 이하 — PUA/난독 의심")
    elif img_ratio >= SETTINGS.hybrid_image_area_ratio:
        verdict = "hybrid"
        reasons.append(
            f"텍스트 레이어 정상이지만 이미지 면적비 {img_ratio:.0%} — 이미지 영역 OCR 병행"
        )
    return PageTriage(verdict, char_count, korean, fffd, img_ratio, reasons)


def _normal_ratio(stripped: str) -> float:
    if not stripped:
        return 0.0
    normal = 0
    for c in stripped:
        if "가" <= c <= "힣" or "ㄱ" <= c <= "ㆎ" or c.isascii():
            normal += 1
            continue
        cat = unicodedata.category(c)
        if cat.startswith(("L", "N", "P", "S")) and cat != "Co":  # Co = 사설영역
            normal += 1
    return normal / len(stripped)


def extract_digital_lines(page: pdfium.PdfPage, px_per_pt: float) -> list[Line]:
    """텍스트 레이어 → 라인(문자 bbox y-겹침 군집). 좌표는 렌더 픽셀로 환산."""
    textpage = page.get_textpage()
    _, page_h = page.get_size()
    n = textpage.count_chars()
    chars: list[tuple[str, tuple[float, float, float, float]]] = []
    for i in range(n):
        ch = textpage.get_text_range(i, 1)
        if not ch or ch.isspace():
            continue
        try:
            left, bottom, right, top = textpage.get_charbox(i)
        except Exception:
            continue
        chars.append((ch, (left, bottom, right, top)))
    if not chars:
        return []

    # top 기준 정렬 후 수직 겹침 50% 이상이면 같은 라인으로 군집
    chars.sort(key=lambda c: (-c[1][3], c[1][0]))
    lines_raw: list[list[tuple[str, tuple[float, float, float, float]]]] = []
    for ch, box in chars:
        placed = False
        for group in lines_raw:
            _, gbox = group[0]
            overlap = min(box[3], gbox[3]) - max(box[1], gbox[1])
            height = min(box[3] - box[1], gbox[3] - gbox[1])
            if height > 0 and overlap / height >= 0.5:
                group.append((ch, box))
                placed = True
                break
        if not placed:
            lines_raw.append([(ch, box)])

    lines: list[Line] = []
    for group in lines_raw:
        group.sort(key=lambda c: c[1][0])
        text = "".join(ch for ch, _ in group)
        left = min(b[0] for _, b in group)
        bottom = min(b[1] for _, b in group)
        right = max(b[2] for _, b in group)
        top = max(b[3] for _, b in group)
        # PDF pt(원점 좌하단) → 렌더 픽셀(원점 좌상단)
        bbox = [
            int(left * px_per_pt),
            int((page_h - top) * px_per_pt),
            int(right * px_per_pt),
            int((page_h - bottom) * px_per_pt),
        ]
        lines.append(Line(text=text, bbox=bbox, confidence=None, source="digital"))
    from .tiling import sort_reading_order

    return sort_reading_order(lines)
