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
    path_count: int = 0               # 벡터 도형(PATH) 개수 — 판정에는 안 쓰고 기록만
    path_area_ratio: float = 0.0      # 겹침 미보정이라 1.0 을 넘을 수 있다(배경+패널 중첩)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "char_count": self.char_count,
            "korean_count": self.korean_count,
            "fffd_count": self.fffd_count,
            "image_area_ratio": round(self.image_area_ratio, 3),
            "path_count": self.path_count,
            "path_area_ratio": round(self.path_area_ratio, 3),
            "reasons": self.reasons,
        }


def _object_census(page: pdfium.PdfPage) -> tuple[float, int, float]:
    """이미지/벡터 오브젝트를 한 번에 센다 → (이미지 면적비, PATH 개수, PATH 면적비).

    **왜 PATH 도 세나.** `_image_area_ratio` 는 FPDF_PAGEOBJ_IMAGE 만 센다. 그런데
    글자를 **벡터 윤곽선(PATH)으로 그려 넣은** 페이지가 있고, 그런 글자는 두 신호
    어디에도 안 잡힌다 — 텍스트 레이어에도 없고(문자가 아니라 도형이므로)
    이미지 면적비도 0 이다. 그래서 `structured` 로 판정돼 OCR 을 통째로 건너뛴다.

    실측 (003 p3, 2026-08-06 좌표 대조): 오브젝트 TEXT 173 · PATH 11 · **IMAGE 0**.
    PATH 11개 중 8개가 낱말 크기였고 원본 크롭과 좌표가 일치했다 —

        [100,91,147,107] '10/20'      [155,90,240,108] '인스타그램'
        [98,119,216,150] '올원모임'    [227,120,345,151] '소문내기'
        [359,120,448,151] '이벤트'     [457,120,513,150] '멘션'
        [1776,79,1910,101] 'Last Updated'  [1825,114,1911,128] '2025.10.15'

    나머지 3개만 배경(2000x1125)·패널(1854x808)·주황띠(1854x44)였다. 이 헤더 문구는
    디지털 레이어에 한 글자도 안 들어왔고(첫 디지털 줄이 y=190 의 '멘션'), ⑦-2
    통짜 스윕이 우연히 3건을 건졌다. 스윕이 안 걸렸으면 조용히 사라졌을 문구다.

    **그래도 판정은 안 바꾼다.** 벡터 글자와 벡터 장식(테두리·구분선·배경 도형)을
    개수나 면적으로 가를 방법이 이 표본에는 없다 — 대부분의 PDF 는 장식 PATH 를
    가지고 있고, 여기서 임계를 만들면 그건 샘플 1건에 맞춘 규칙이다. 그래서 세어서
    남기기만 한다. 판정을 바꾸려면 벡터 글자가 있는 문서를 더 모아 정답을 만드는
    것이 선행이다.
    """
    w, h = page.get_size()
    page_area = w * h
    if page_area <= 0:
        return 0.0, 0, 0.0
    img_total = path_total = 0.0
    path_n = 0
    try:
        for obj in page.get_objects(max_depth=2):
            if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
                left, bottom, right, top = obj.get_bounds()  # pypdfium2 v5 (v4: get_pos)
                img_total += max(0.0, right - left) * max(0.0, top - bottom)
            elif obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
                path_n += 1
                left, bottom, right, top = obj.get_bounds()
                path_total += max(0.0, right - left) * max(0.0, top - bottom)
    except Exception:
        return 0.0, 0, 0.0
    # 이미지 면적비는 hybrid 판정에 쓰이므로 기존대로 1.0 에서 자른다. PATH 면적비는
    # 판정에 안 쓰고 진단용이라 자르지 않는다 — 배경과 패널이 겹쳐 1.0 을 넘는 것
    # 자체가 "이건 장식 도형이 깔린 페이지"라는 정보다 (003 p3 = 1.71).
    return min(1.0, img_total / page_area), path_n, path_total / page_area


def triage_page(page: pdfium.PdfPage) -> PageTriage:
    textpage = page.get_textpage()
    text = textpage.get_text_bounded() or ""
    stripped = "".join(ch for ch in text if not ch.isspace())
    char_count = len(stripped)
    korean = sum(1 for c in stripped if "가" <= c <= "힣")
    fffd = stripped.count("�")
    img_ratio, path_n, path_ratio = _object_census(page)

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

    # 경고만 — 판정은 위에서 이미 끝났고 아래 줄은 verdict 를 건드리지 않는다.
    # structured 일 때만 남긴다: OCR 을 건너뛰는 경로에서만 벡터 글자가 유실되기 때문이다
    # (scan_like/hybrid 는 어차피 OCR 이 도니 위험이 없다). 조용한 실패 금지 원칙에서,
    # reasons 가 빈 배열이면 "확인할 게 없다"로 읽히던 자리다 — 003 p3 이 그랬다.
    if verdict == "structured" and path_n:
        reasons.append(
            f"[주의] 벡터 도형 {path_n}개(면적비 {path_ratio:.2f}) — 글자를 벡터로 그렸으면 "
            f"텍스트 레이어에도 이미지 면적비에도 안 잡혀 OCR 생략 시 유실된다 "
            f"(실측 003 p3: 헤더 8낱말). 판정은 유지 — 스윕 회수 결과를 확인할 것"
        )
    return PageTriage(verdict, char_count, korean, fffd, img_ratio, path_n, path_ratio, reasons)


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
