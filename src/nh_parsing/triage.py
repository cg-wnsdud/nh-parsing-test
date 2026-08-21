"""PDF 페이지 단위 triage [B1] — 설계서 4.2절.

document-processor 의 probe 기준(정상문자 비율, U+FFFD, 이미지 면적비)을
pypdfium2 로 재현한다. 프로토타입에서는 이중 렌더를 피하려고 인라인 구현했고,
프로덕션에서는 사내 파서 probe 를 그대로 감싸도 된다.

핵심 원칙(조용한 실패 금지): 모든 페이지는 반드시 structured / scan_like /
hybrid 중 하나로 판정되고 판정 근거가 AdPage.triage 에 남는다.
"""

from __future__ import annotations

import ctypes
import unicodedata
from dataclasses import dataclass, field

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .config import SETTINGS
from .ir import Line
from .text_style import fold, rgb_hex, strip_subset_prefix

# FPDFText_GetFontInfo 버퍼. 실측 글꼴명이 `TRDBGG+SDGothicNeoa-fSm`(23자) 수준이라 넉넉하다.
_FONT_NAME_BUF = 128


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


_LINE_GAP_MULTIPLIER = 4.0  # 평균 글자 폭의 이 배수 이상 벌어지면 다른 칸(줄)으로 본다
_LINE_GAP_MIN_PX = 6.0      # 위 배수가 너무 작아지는 것을 막는 하한


def _split_by_column_gap(
    group: list[tuple[str, tuple[float, float, float, float]]],
) -> list[list[tuple[str, tuple[float, float, float, float]]]]:
    """세로 겹침만으로 묶인 한 그룹을, 가로로 비정상 벌어진 지점에서 다시 쪼갠다.

    표 칸("가입금액" | "100만원이상" | "조건" | "금리(%p)")은 세로 위치가 같아 위 군집
    에서 한 줄로 합쳐진다. 실측(2026-08-13, `1. 예금성상품(거치식).pdf`): 정상 낱말
    사이 간격은 평균 글자 폭의 최대 2.2배인데, 표 칸 경계 간격은 5.6~19배였다 — 그
    사이 어디에도 정상 문장이 없어 4배를 경계로 쓴다. 이 경계를 못 넘으면 한 줄이
    옆 칸 내용까지 끌고 들어가 영역 배정 때 엉뚱한 영역에 붙는다(p1_r006 실측).
    """
    if len(group) < 2:
        return [group]
    widths = [item[1][2] - item[1][0] for item in group]
    avg_w = sum(widths) / len(widths)
    gap_limit = max(avg_w * _LINE_GAP_MULTIPLIER, _LINE_GAP_MIN_PX)
    segments: list[list[tuple[str, tuple[float, float, float, float]]]] = [[group[0]]]
    # 항목을 재조립하지 않고 **그대로** 넣는다. 2-튜플만 다루던 예전 코드는 `(ch, box)` 로
    # 다시 만들어 3번째 원소(시인성)를 잘라 버렸다. 여기서 필요한 건 [0]=글자·[1]=칸뿐이므로
    # 길이에 관여하지 않는 편이 호출자를 안 묶는다.
    for item in group[1:]:
        box = item[1]
        prev_right = segments[-1][-1][1][2]
        if box[0] - prev_right > gap_limit:
            segments.append([])
        segments[-1].append(item)
    return segments


# PDF 글꼴 굵기 → bold 판정 경계. 실측(2026-08-21, `1. 대출성상품.pdf` p1 전수):
# 260(OTMGothicL) 1060자 · 440(M) 491 · 360(R) 128 · **600(OTMGothicB) 99** · 540 34 · 520 16.
# 글꼴명이 Bold 인 것이 정확히 600 이라 그 값을 경계로 쓴다(CSS 관례 700 이 아니다 —
# 이 문서군의 글꼴이 600 을 Bold 로 쓴다).
_PDF_BOLD_WEIGHT = 600


def _char_style(textpage, index: int) -> tuple[float | None, bool | None, str | None, str | None]:
    """글자 하나의 (크기pt, 굵음, 색, 글꼴명). 못 얻는 항목은 None.

    크기는 여기서 안 채운다 — 호출자가 글꼴칸 높이로 넣는다(`ir.SizeBasis` 주석 참조).
    """
    weight = pdfium_c.FPDFText_GetFontWeight(textpage.raw, index)
    bold = (weight >= _PDF_BOLD_WEIGHT) if weight and weight > 0 else None

    color: str | None = None
    r, g, b, a = (ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint())
    if pdfium_c.FPDFText_GetFillColor(
        textpage.raw, index, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)
    ):
        color = rgb_hex(r.value, g.value, b.value)

    font: str | None = None
    buf = ctypes.create_string_buffer(_FONT_NAME_BUF)
    flags = ctypes.c_int()
    used = pdfium_c.FPDFText_GetFontInfo(
        textpage.raw, index, buf, _FONT_NAME_BUF, ctypes.byref(flags)
    )
    if used:
        name = buf.raw[: max(0, used - 1)].decode("utf-8", "replace").strip()
        if name:
            font = strip_subset_prefix(name)

    return None, bold, color, font


def extract_digital_lines(page: pdfium.PdfPage, px_per_pt: float) -> list[Line]:
    """텍스트 레이어 → 라인(문자 글꼴칸 y-겹침 군집 + 가로 공백 분리). 좌표는 렌더 픽셀로 환산.

    **줄 묶기는 글꼴칸(loose charbox), 좌표는 잉크칸(tight charbox)을 쓴다.** 잉크칸은
    글자마다 높이가 제각각이라(같은 줄에서 `ㆍ` 5.3px · `임` 25.8px 실측) 겹침 비율이
    글자 모양에 좌우된다. 그래서 키 큰 글자가 윗줄 글자와 임계값을 아슬하게 넘겨
    **윗줄에 흡수되고 형제 글자만 남아 라벨이 쪼개졌다** — 실측(2026-08-14,
    `7. 대출성상품.pdf` p1): `필`(높이 28.7)이 윗줄 `임`(25.8)과 51.6% 겹쳐 흡수되고
    `류`·`요`는 46~49%로 탈락해 `필요서류` → `필서` + `요류` 로 갈렸다. 같은 원인으로
    `대출한도` → `대출한` + `도`, `대출용도` → `대출용` + `도`(6. 대출성상품).

    글꼴칸은 글꼴 메트릭이 정하므로 **같은 줄 글자가 전부 같은 상자**가 된다(위 실측에서
    `필요서류` 넷 다 1916.3~1948.9, `임대차계약서` 전부 1903.6~1933.0) — 글자 모양에
    흔들리지 않는다. 다만 글꼴칸은 실제 획보다 크므로 화면 하이라이트·영역 배정이 쓰는
    bbox 는 종전대로 잉크칸으로 낸다.
    """
    textpage = page.get_textpage()
    _, page_h = page.get_size()
    n = textpage.count_chars()
    # (글자, 잉크칸, 글꼴칸, 시인성)
    chars: list[tuple[str, tuple[float, ...], tuple[float, ...], tuple]] = []
    for i in range(n):
        ch = textpage.get_text_range(i, 1)
        if not ch or ch.isspace():
            continue
        try:
            tight = textpage.get_charbox(i)
        except Exception:
            continue
        try:
            loose = textpage.get_charbox(i, loose=True)
        except Exception:
            loose = tight  # 글꼴칸을 못 얻는 글자는 종전(잉크칸) 동작으로 되돌아간다
        if loose[3] - loose[1] <= 0:
            loose = tight
        try:
            _, bold, color, font = _char_style(textpage, i)
        except Exception:
            bold = color = font = None  # 스타일을 못 얻어도 텍스트 추출은 멈추지 않는다
        # 크기는 글꼴칸 높이(pt). 선언값(FPDFText_GetFontSize)은 못 쓴다 — ir.SizeBasis 주석.
        size = round(loose[3] - loose[1], 1)
        chars.append((ch, tight, loose, (size, bold, color, font)))
    if not chars:
        return []

    # 글꼴칸 top 기준 정렬 후 수직 겹침 50% 이상이면 같은 라인으로 군집
    chars.sort(key=lambda c: (-c[2][3], c[1][0]))
    lines_raw: list[list[tuple[str, tuple[float, ...], tuple[float, ...]]]] = []
    for entry in chars:
        box = entry[2]
        placed = False
        for group in lines_raw:
            gbox = group[0][2]
            overlap = min(box[3], gbox[3]) - max(box[1], gbox[1])
            height = min(box[3] - box[1], gbox[3] - gbox[1])
            if height > 0 and overlap / height >= 0.5:
                group.append(entry)
                placed = True
                break
        if not placed:
            lines_raw.append([entry])

    lines: list[Line] = []
    for group in lines_raw:
        group.sort(key=lambda c: c[1][0])
        # 가로 공백 분리와 최종 좌표는 잉크칸 기준 (글꼴칸은 좌우로도 넉넉해 칸 경계가 뭉갠다)
        # 3번째 원소로 시인성을 함께 실어 보낸다 — _split_by_column_gap 은 [0]·[1]만 본다.
        tight_group = [(ch, tight, atom) for ch, tight, _, atom in group]
        for segment in _split_by_column_gap(tight_group):
            text = "".join(item[0] for item in segment)
            left = min(item[1][0] for item in segment)
            bottom = min(item[1][1] for item in segment)
            right = max(item[1][2] for item in segment)
            top = max(item[1][3] for item in segment)
            # PDF pt(원점 좌하단) → 렌더 픽셀(원점 좌상단)
            bbox = [
                int(left * px_per_pt),
                int((page_h - top) * px_per_pt),
                int(right * px_per_pt),
                int((page_h - bottom) * px_per_pt),
            ]
            style = fold(
                (item[2] for item in segment if len(item) > 2),
                basis="fontbox",
                source="pdf_char",
            )
            lines.append(
                Line(text=text, bbox=bbox, confidence=None, source="digital", style=style)
            )
    from .tiling import sort_reading_order

    return sort_reading_order(lines)
