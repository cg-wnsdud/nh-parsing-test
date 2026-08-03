from __future__ import annotations

"""VLM 직독 폴백 — 설계서 6.4 (OCR/디지털 추출이 놓친 텍스트 회수).

sweep_missing_lines — 페이지 축소본과 이미 추출된 텍스트 목록을 주고, 화면에 보이는데
목록에 없는 문구를 전사시킨다. 대응 대상:
- 장식 타이포·복잡 배경 헤드라인 (OCR 검출 실패: '행운의 777 이벤트')
- 벡터(패스) 텍스트 (텍스트 레이어에도 이미지 오브젝트에도 없음 —
  003 p3 '올원모임 소문내기 이벤트 멘션' 실측)
환각 방지: 정규화 기준으로 기존 텍스트에 이미 있으면 버린다.
bbox 는 VLM 이 준 y_ratio 로 만든 전폭 근사 밴드 (source='vlm_sweep' 표시).

(수치 필드 크롭 재확인 verify_numeric_fields 와 영역별 통독 transcribe_region_crops
는 밴드 통합판독 read_band_regions 이 흡수하며 죽은 코드가 되어 2026-07-29 제거했다
— 죽은 코드 감사 참조.)
"""

import re

from PIL import Image

from .config import SETTINGS
from .gemma_client import chat_json, image_part
from .ir import Line, Region

_MAX_SWEEP_ITEMS = 20


def _n(text: str) -> str:
    """중복 판정용 거친 정규화 (공백·문장부호 제거)."""
    return re.sub(r"[\s,.~:：%()\[\]{}'\"`「」『』<>·※*!?|#-]", "", str(text)).lower()


# ──────────────────────── 1) 누락 문구 스윕 ────────────────────────

_SWEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},  # strict 스키마 빈 배열 퇴행 방지 (부록 C-5)
        "missing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "y_ratio": {"type": "number"},
                    "confidence": {"type": "number"},
                },
                "required": ["text", "y_ratio", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "missing"],
    "additionalProperties": False,
}

_SWEEP_PROMPT = """당신은 광고 화면 텍스트 검수기입니다.
아래는 이 화면에서 기계 추출된 텍스트 목록입니다. 첨부 이미지를 보고,
화면에 실제로 보이는데 목록에 **없는** 문구를 찾아 그대로 전사하세요.

- 대상: 큰 제목, 장식·꾸밈 글씨, 배경 위 문구, 상단/하단 메타 표기 등
- text 는 보이는 그대로(숫자·단위 정확히), y_ratio 는 세로 위치(0=최상단, 1=최하단)
- 이미지에 실제로 보이는 문구만 적으세요. 목록에 이미 있는 내용은 제외하세요.
- 없으면 missing 을 빈 배열로 반환하세요.

기존 추출 텍스트 목록:
{existing}

먼저 analysis 에 어떤 문구가 빠져 보이는지 정리한 뒤 missing 을 채우세요."""


def _snap_cut(y: int, obstacles: list[list[int]], limit: int, canvas_h: int) -> int:
    """절단 예정 y 를 '글자를 가로지르지 않는' 가장 가까운 위치로 당긴다.

    산술로만 자르면 밴드 경계가 글자 한가운데를 지난다(001 실측: y=1240 이 헤드라인
    '우리아이 / 금융생활 서비스 확대!' 를, y=3720 이 112px 짜리 '20,000원' 을 반토막.
    002 는 y=1240 이 '농협은행이' 를 자름). 잘린 글자는 양쪽 밴드 어디서도 온전히
    안 읽혀 회수에서 빠진다 — 002 밴드 패스가 대형 타이포를 0/4 로 놓친 원인.

    obstacles 는 이미 알고 있는 텍스트/블록 bbox 다(스윕 시점엔 OCR·레이아웃 결과가
    나와 있으므로 VLM 호출 없이 좌표 계산만으로 판단한다). y 를 위아래로 최대 limit
    까지 훑어 아무것도 가로지르지 않는 지점을 찾고, 없으면 원래 y 를 쓴다.
    """
    def crosses(pos: int) -> bool:
        return any(b[1] < pos < b[3] for b in obstacles)

    if not crosses(y):
        return y
    for delta in range(1, limit + 1):
        for cand in (y - delta, y + delta):
            if 0 < cand < canvas_h and not crosses(cand):
                return cand
    return y  # 빈틈을 못 찾으면 원래 자리 (오버랩이 일부 보완)


def _sweep_bands(
    canvas: Image.Image, obstacles: list[list[int]] | None = None
) -> list[tuple[Image.Image, int]]:
    """세로로 긴 캔버스를 VLM 이 실제로 읽을 수 있는 밴드로 자른다. 반환 (밴드, y오프셋).

    실측 근거(2026-07-27): VLM 서버는 pan-and-scan 없이 어떤 크기든 고정 예산(이미지
    토큰 ~1,050~1,100)으로 리샘플링한다 — 720x6554 를 보내나 896x896 을 보내나 토큰이
    같다. 즉 길게 보낼수록 축소율만 커져 작은 글씨가 소실된다. 같은 문단을 통짜로
    주면 3/13, 구간만 잘라 주면 13/13 회수됐다.

    스윕 회수율 실측(existing=[] 로 읽기능력만 측정):
        001  통짜 0.27 / 밴드x2.0 0.59 / 밴드x4.0 0.95
        002  통짜 0.25 / 밴드x2.0 0.86 / 밴드x4.0 0.70 / 밴드x6.0 0.30
        올원  통짜 0.75 / 밴드x4.0 1.00
    통짜가 최악인 것은 일관되나 최적 밴드비는 문서마다 엇갈린다(001 은 크게, 002 는
    작게가 유리). 그래서 '가장 나쁜 경우가 가장 덜 나쁜' 2.0 을 택했다 — 002 최고치이자
    001 에서도 통짜의 2배. 표본이 늘면 재측정해 조정할 것.
    """
    from .bands import band_count_for, bands_from_cuts, edge_profile, plan_cuts, vlm_band_span

    h = canvas.height
    span = vlm_band_span(canvas)
    if h <= span:
        return [(canvas, 0)]

    # ① 밴드 개수는 종전 규칙 그대로 — 고정 토큰 예산이 정하는 값이라 바꾸지 않는다.
    #    개수까지 바꾸면 회수율 변화가 위치 때문인지 개수 때문인지 못 가른다.
    # ② 자르는 위치는 글자 밀도가 정한다. 높이로 균등하게 자르면 조각마다 담기는
    #    글자량이 2.0~2.7배 차이 나는데(001 실측 15/20/18/19/40%), VLM 은 조각 크기와
    #    무관하게 같은 예산을 쓰므로 글자가 몰린 조각이 손해를 본다. 등량 분할 후 1.1~1.2배.
    profile = edge_profile(canvas, "y")
    cuts, _ = plan_cuts(profile, band_count_for(canvas, span))

    # ③ 픽셀 밀도가 놓친 절단을 OCR 라인 좌표로 한 번 더 검사한다. 두 신호는 보는 것이
    #    달라서(픽셀 vs 검출된 라인 상자) 한쪽만으로는 샌다 — 옅은 회색 글자는 에지가
    #    약해 '빈 줄'로 보일 수 있고, 반대로 OCR 이 아예 못 잡은 장식 글자는 상자가 없다.
    #    추가 호출은 없다(둘 다 이미 가진 정보).
    if obstacles:
        cuts = [_snap_cut(c, obstacles, SETTINGS.vlm_band_snap_px, h) for c in cuts]
        cuts = sorted({c for c in cuts if 0 < c < h})

    return [(b.image, b.offset)
            for b in bands_from_cuts(canvas, cuts, overlap=SETTINGS.tile_overlap_px,
                                     profile=profile)]


def sweep_missing_lines(
    existing: list[Line], canvas: Image.Image, canvas_w: int, canvas_h: int,
    *, banded: bool = True, obstacles: list[list[int]] | None = None,
) -> tuple[list[Line], list[str]]:
    """화면에 보이지만 추출 목록에 없는 문구를 VLM 으로 회수한다 (밴드 단위).

    밴드로 나누는 이유는 _sweep_bands 참조. 부수 효과로 bbox 정밀도도 올라간다 —
    y_ratio 가 밴드 기준이라 캔버스 전체의 1.2% 가 아니라 밴드의 1.2% 가 되어,
    001 기준 세로 157px 띠가 약 35px 로 좁아진다(F-011/012 하이라이트 정확도).

    banded=False 면 캔버스 전체를 한 장으로 본다. 밴드가 항상 유리하지는 않기 때문이다 —
    운영 조건 A/B 실측: 001 은 밴드가 4/4 회수(통짜 0/4)로 압승했지만, 002 는 통짜가
    2/4(밴드 0/4)로 이겼다. 002 가 놓친 것은 화면을 꽉 채우는 대형 장식 타이포
    ('인생 대박적금이 온다!', '행운의 777 이벤트')로, 밴드 경계에서 글자가 잘려 못 읽는다.
    작은 글씨는 밴드가, 큰 글씨는 통짜가 유리하므로 호출측이 두 패스로 나눠 합집합을 쓴다.

    반환: (회수 라인, 밴드 실패 노트). 밴드 하나가 죽어도 나머지 회수분은 살린다 —
    예외로 던지면 호출측이 그 패스 전체를 버리므로(조용한 유실) 노트로 올려 보낸다.
    """
    existing_texts = [l.text for l in existing if l.text.strip()]
    existing_norm = _n("".join(existing_texts))
    listing = "\n".join(f"- {t}" for t in existing_texts[:200])
    prompt = _SWEEP_PROMPT.format(existing=listing)

    recovered: list[Line] = []
    notes: list[str] = []
    if banded:
        # obstacles 를 안 주면 existing 라인 bbox 라도 장애물로 써서 글자 절단을 줄인다
        obs = obstacles if obstacles is not None else [l.bbox for l in existing if l.bbox]
        bands = _sweep_bands(canvas, obs)
    else:
        bands = [(canvas, 0)]
    for img, y_off in bands:
        band_h_px = img.height
        try:
            data = chat_json(
                [{"type": "text", "text": prompt}, image_part(img)],
                schema_name="missing_text_sweep",
                schema=_SWEEP_SCHEMA,
                # 응답이 잘리면 JSON 파싱이 깨져 그 밴드 결과가 통째로 사라진다
                # (실측: 'Unterminated string' 발생 시 회수율 0.95 → 0.27 폭락).
                max_tokens=SETTINGS.sweep_max_tokens,
            )
        except Exception as exc:
            notes.append(f"스윕 밴드 실패(y={y_off}, 이 구간 회수 누락): {exc}")
            continue
        for item in data.get("missing", [])[:_MAX_SWEEP_ITEMS]:
            text = str(item.get("text", "")).strip()
            norm = _n(text)
            if not text or len(norm) < 2:
                continue
            if norm in existing_norm:  # 이미 있는 내용 — 환각/중복 방지
                continue
            y = min(max(float(item.get("y_ratio", 0.0)), 0.0), 1.0)
            band = max(12, int(band_h_px * 0.012))
            cy = y_off + int(y * band_h_px)
            recovered.append(Line(
                text=text,
                bbox=[0, max(0, cy - band), canvas_w, min(canvas_h, cy + band)],
                confidence=item.get("confidence"),
                source="vlm_sweep",
            ))
            existing_norm += norm  # 스윕 내 중복 방지
    if len(bands) > 1:
        notes.append(f"스윕 밴드 분할: {len(bands)}개 밴드 (통짜 입력 시 회수율 급락 실측)")
    return recovered, notes


# ──────────────────────── 크롭 재판독 공용 스키마 ────────────────────────

_CROP_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "value": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["analysis", "value", "confidence"],
    "additionalProperties": False,
}


def _looks_malformed(text: str) -> bool:
    """자연어 표기값에 나올 수 없는 구조적 파손 신호 (JSON 중괄호, 짝 안 맞는 따옴표)."""
    if any(ch in text for ch in "{}"):
        return True
    if text.count('"') % 2 != 0 or text.count("'") % 2 != 0:
        return True
    return False


def _crop_bbox(bbox: list[int] | None, canvas: Image.Image) -> Image.Image | None:
    """bbox 주변을 여백 포함해 크롭하고, 작으면 확대해 판독성을 확보한다."""
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    pad_x, pad_y = 24, 12
    box = (
        max(0, x0 - pad_x), max(0, y0 - pad_y),
        min(canvas.width, x1 + pad_x), min(canvas.height, y1 + pad_y),
    )
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    crop = canvas.crop(box)
    if crop.width < 600:  # 작은 크롭은 확대해 판독성 확보
        scale = 600 / crop.width
        crop = crop.resize((600, int(crop.height * scale)), Image.LANCZOS)
    return crop


# ──────────────────── 라인 크롭 재판독 (스윕-OCR 중복 심판) ────────────────────

_LINE_CROP_PROMPT = """크롭 이미지에 있는 한 줄의 텍스트를 보이는 그대로 정확히 전사하세요.
- 숫자·소수점·%·%p 와 ①②③ 같은 원문자 번호 기호를 정확히 옮기세요.
- 크롭에 실제로 보이는 내용만 전사하고, 없는 내용을 지어내지 마세요.
먼저 analysis 에 보이는 내용을 서술한 뒤 value 에 한 줄 전사 결과를 넣으세요."""


def transcribe_line_crop(bbox: list[int] | None, canvas: Image.Image) -> str:
    """라인 bbox 를 고해상 크롭으로 다시 읽어 전사 문자열을 반환한다(실패 시 '').

    스윕판·OCR판이 같은 줄의 서로 다른 판독일 때, 그 위치를 독립적으로 재판독해
    어느 쪽이 맞는지 가리는 심판용(규칙으로 우열을 정하지 않음).
    """
    crop = _crop_bbox(bbox, canvas)
    if crop is None:
        return ""
    try:
        data = chat_json(
            [
                {"type": "text", "text": _LINE_CROP_PROMPT},
                image_part(crop, box=(896, 896)),
            ],
            schema_name="line_crop_reread",
            schema=_CROP_SCHEMA,
            max_tokens=400,
        )
    except Exception:
        return ""
    reading = str(data.get("value", "")).strip()
    if not reading or _looks_malformed(reading):
        return ""
    return reading


def _reread_crop_with_conf(
    bbox: list[int] | None, canvas: Image.Image
) -> tuple[str, float | None]:
    """라인 bbox 를 고해상 크롭으로 재판독하고 (전사문자열, VLM 확신도) 를 반환.

    transcribe_line_crop 과 같은 크롭·프롬프트를 쓰되, 저신뢰 라인 교체 판단에
    필요한 confidence 까지 함께 돌려준다. 실패/형식파손이면 ('', None).
    """
    crop = _crop_bbox(bbox, canvas)
    if crop is None:
        return "", None
    try:
        data = chat_json(
            [
                {"type": "text", "text": _LINE_CROP_PROMPT},
                image_part(crop, box=(896, 896)),
            ],
            schema_name="lowconf_line_reread",
            schema=_CROP_SCHEMA,
            max_tokens=400,
        )
    except Exception:
        return "", None
    reading = str(data.get("value", "")).strip()
    if not reading or _looks_malformed(reading):
        return "", None
    conf = data.get("confidence")
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = None
    return reading, conf


# 유령 중복 라인 판정 파라미터 (2b 전처리) — 이웃 겹침 기반
_PHANTOM_ROW_VOVERLAP = 0.5  # 같은 '행'으로 볼 세로 겹침 하한 (짧은 쪽 높이 기준)
_PHANTOM_X_COVERAGE = 0.6    # 이 라인 가로폭이 고신뢰 이웃 합집합에 덮이는 비율 하한


def _row_overlap_ratio(a: list[int], b: list[int]) -> float:
    """두 bbox 의 세로 겹침 비율 (짧은 높이 기준). 같은 행 판정용."""
    inter = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    short = min(a[3] - a[1], b[3] - b[1])
    return inter / short if short > 0 else 0.0


def _x_union_coverage(bbox: list[int], sibs: list[list[int]]) -> float:
    """bbox 의 가로 구간이 sibs 가로 구간들의 '합집합'에 덮이는 비율 (0~1)."""
    x0, x1 = bbox[0], bbox[2]
    width = x1 - x0
    if width <= 0:
        return 0.0
    spans = sorted(
        (max(x0, b[0]), min(x1, b[2])) for b in sibs if min(x1, b[2]) > max(x0, b[0])
    )
    if not spans:
        return 0.0
    covered = 0
    cur0, cur1 = spans[0]
    for s0, s1 in spans[1:]:
        if s0 <= cur1:
            cur1 = max(cur1, s1)
        else:
            covered += cur1 - cur0
            cur0, cur1 = s0, s1
    covered += cur1 - cur0
    return covered / width


def reread_low_confidence_lines(
    regions: list[Region], canvas: Image.Image
) -> list[str]:
    """심의 관련 영역의 저신뢰 OCR 라인을 고해상 크롭으로 재판독해 **후보를 붙인다** (2b).

    - 대상: role='이미지' 도 is_illustrative(예시/장식)도 아닌 영역의, source='ocr'
      이고 confidence < 임계인 라인. 디지털·스윕 라인은 제외(디지털은 신뢰, 스윕은 근사).
    - **정본을 덮지 않는다**(2026-08-03 변경). 재판독 결과는 `Line.vlm_reading` 후보로만
      남기고 text/confidence/source 는 OCR 원값을 유지한다. 예전에는 여기서 대입해
      정본을 갈아치웠는데, 재판독이 VLM 이라 실행마다 달라질 수 있고 그러면 하류의
      지적 목록까지 흔들린다(ir.Line.vlm_reading 주석의 실측 참조). 최종 텍스트 선택은
      하류(STAGE_3/심의)가 한다 — Region.vlm_reading 과 같은 원칙(B안).
    - 부착 조건은 보수적으로 유지: 재판독이 비어있지 않고, 확신도가 임계 이상이고,
      기존 텍스트와 다를 때만 붙인다(같으면 후보도 안 붙임).
    - 조용한 변경 금지: 모든 부착을 notes 에 기록해 감사 가능하게 한다.
    반환: 부착/스킵 내역 노트 목록.
    """
    thr = SETTINGS.lowconf_reread_threshold
    min_conf = SETTINGS.lowconf_reread_min_vlm_conf
    cap = SETTINGS.lowconf_reread_max_per_page

    notes: list[str] = []
    candidates: list[tuple[Line, float]] = []
    for region in regions:
        if region.is_illustrative or region.role == "이미지":
            continue
        # 이 영역의 저신뢰 OCR 라인 후보
        lowconf = [
            (line, line.confidence)
            for line in region.lines
            if line.source == "ocr" and line.bbox
            and line.confidence is not None and line.confidence < thr
            and len(_n(line.text)) >= 1  # 빈 텍스트(이미지 잔여) 제외
        ]
        if not lowconf:
            continue
        # 유령 중복 제거: 같은 영역·같은 행의 더 높은 신뢰도 라인들이 이 라인의 가로
        # 구간을 대부분 덮으면, 이미 정확히 읽힌 텍스트의 중복 검출이다 → 재판독이 아니라
        # 삭제(001 '재테크님이' 사건: 얇은 유령 조각이 '올원뱅크에서/자녀/고객님의' 위에 겹침).
        # 저신뢰 라인을 고신뢰로 '승격'시켜 노이즈를 세탁하는 것을 막는다.
        drop_ids: set[int] = set()
        for line, c in lowconf:
            sib_bboxes = [
                o.bbox for o in region.lines
                if o is not line and o.bbox
                and (o.confidence is None or o.confidence > c)
                and _row_overlap_ratio(line.bbox, o.bbox) >= _PHANTOM_ROW_VOVERLAP
            ]
            cov = _x_union_coverage(line.bbox, sib_bboxes)
            if cov >= _PHANTOM_X_COVERAGE:
                drop_ids.add(id(line))
                notes.append(
                    f"유령 중복 라인 제거: {line.text!r}({c:.2f}) — "
                    f"같은 행 고신뢰 라인이 가로 {cov:.0%} 덮음(재판독 대상 아님)"
                )
        if drop_ids:
            region.lines = [l for l in region.lines if id(l) not in drop_ids]
        candidates.extend((line, c) for line, c in lowconf if id(line) not in drop_ids)

    if not candidates:
        return notes
    candidates.sort(key=lambda t: t[1])  # 가장 의심스러운(낮은) 것부터

    attached = 0
    for idx, (line, c) in enumerate(candidates):
        if idx >= cap:
            notes.append(f"저신뢰 재판독 상한({cap}) 도달 — 나머지 {len(candidates) - cap}줄 원값 유지")
            break
        reading, vconf = _reread_crop_with_conf(line.bbox, canvas)
        if not reading:
            continue
        if vconf is not None and vconf < min_conf:
            continue
        if _n(reading) == _n(line.text):
            continue  # 재판독이 같은 판독 — 후보 부착 불필요
        # 정본(text/confidence/source)을 덮지 않는다 — ir.Line.vlm_reading 주석 참조.
        # 재판독 결과는 VLM 이라 실행마다 달라질 수 있고, 정본이 흔들리면 하류의 지적
        # 목록까지 흔들린다. 후보로만 남기고 최종 선택은 하류에 맡긴다.
        old = line.text
        line.vlm_reading = reading
        line.vlm_reading_conf = vconf
        line.vlm_reading_stage = "lowconf_reread"
        attached += 1
        notes.append(
            f"저신뢰 라인 재판독 후보 부착(정본 유지): 정본 {old!r}({c:.2f}) ← 후보 {reading!r}"
            + (f" (VLM conf {vconf:.2f})" if vconf is not None else "")
        )
    if attached:
        notes.append(f"저신뢰 라인 재판독: 후보 {len(candidates)}줄 중 {attached}줄에 후보 부착")
    return notes


# ─────────────── 밴드 단위 통합 판독 (④+ 통독 + ⑧ 스윕을 한 호출로) ───────────────
#
# 예전엔 두 단계가 같은 페이지를 다른 크롭으로 각각 봤다(영역별 통독 4장 배치 + 밴드
# 스윕). 실측(5문서): 통독 60회 + 밴드스윕 15회. 밴드 하나에 그 안의 영역 목록을 함께
# 주면 "이 영역들을 고쳐라 + 목록에 없는 문구를 찾아라"를 한 호출로 물을 수 있어
# 합쳤다(65회로 절감, 2026-07-28). 옛 개별 경로(영역별 통독·수치 크롭 재확인)는
# 이 함수가 완전히 흡수해 죽은 코드가 됐고 2026-07-29 걷어냈다.
#
# **해상도 상충이 이 방식의 위험이었다.** 영역 크롭은 그 영역 하나가 896x896 을 다
# 쓰지만, 밴드는 영역 5~10개가 한 장을 나눠 쓴다. 통독이 잡던 미세 오류(원문자가
# 숫자에 붙음, 소수점 소실)가 낮은 배율에서도 살아남는 것을 A/B 로 확인했다.

_BAND_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},  # 배열 선두 퇴행 방지 (부록 C-5)
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "region_id": {"type": "string"},
                    "text": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["region_id", "text", "confidence"],
                "additionalProperties": False,
            },
        },
        "missing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "y_ratio": {"type": "number"},
                    "confidence": {"type": "number"},
                },
                "required": ["text", "y_ratio", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "regions", "missing"],
    "additionalProperties": False,
}

_BAND_READ_PROMPT = """첨부 이미지는 광고 화면의 한 구간입니다. 이 구간에서 기계가 뽑아낸
영역별 텍스트가 아래에 있습니다. 두 가지를 해주세요.

(1) regions — 아래 각 영역을 이미지에서 다시 읽어 정확한 텍스트로 채우세요.
    - 기계 판독이 틀린 곳을 바로잡는 것이 목적입니다. 특히 ①②③ 같은 원문자가 숫자에
      붙어버린 경우('10.1%p' → '① 0.1%p'), 소수점이 사라진 경우('71%' → '7.1%'),
      글자가 깨진 경우를 정확히 옮기세요.
    - 숫자·소수점·%·%p·하이픈·원문자를 있는 그대로 옮기고, 없는 내용을 지어내지 마세요.
    - 아래 목록의 region_id 를 그대로 써서 영역마다 하나씩, 빠짐없이 반환하세요.
    - 이미지에서 그 영역을 못 찾겠으면 text 를 빈 문자열로 두세요(추측 금지).

(2) missing — 이미지에는 보이는데 아래 목록 어디에도 없는 문구를 찾아 담으세요.
    - 대상: 큰 제목, 장식·꾸밈 글씨, 배경 위 문구 등 기계가 놓치기 쉬운 것.
    - y_ratio 는 이 구간 안에서의 세로 위치(0=맨 위, 1=맨 아래).
    - 없으면 빈 배열로 두세요.
    - **위 영역 목록에 있는 내용을 다르게(더 정확하게) 읽었다면 missing 이 아니라
      (1)의 regions 에서 그 영역을 고치세요.** missing 은 목록에 아예 없던 새 문구만
      담는 자리입니다 — 같은 내용을 두 자리에 중복으로 적지 마세요.

영역 목록:
{regions}

먼저 analysis 에 이 구간의 구성을 한두 문장으로 정리한 뒤 regions 와 missing 을 채우세요."""


def read_band_regions(
    band: Image.Image, entries: list[tuple[str, str]]
) -> tuple[dict[str, tuple[str, float | None]], list[dict]]:
    """밴드 하나를 읽어 (영역별 교정, 누락 문구)를 한 번에 돌려준다.

    entries 는 [(region_id, 현재 OCR 텍스트)]. 반환의 첫 원소는 region_id → (전사, 확신도)
    이며 형식이 깨졌거나 빈 응답인 영역은 아예 담기지 않는다(호출측이 원값 유지).
    """
    listing = "\n".join(f'- region_id={rid} 현재판독: "{txt[:120]}"' for rid, txt in entries)
    parts = [
        {"type": "text", "text": _BAND_READ_PROMPT.format(regions=listing or "(없음)")},
        image_part(band),
    ]
    data = chat_json(
        parts,
        schema_name="band_read",
        schema=_BAND_READ_SCHEMA,
        # 영역 수에 비례해 넉넉히 — 잘리면 그 밴드 결과가 통째로 사라진다(스윕 실측)
        max_tokens=min(8000, 800 + 260 * max(1, len(entries))),
    )

    known = {rid for rid, _ in entries}
    readings: dict[str, tuple[str, float | None]] = {}
    for item in data.get("regions", []) or []:
        rid = str(item.get("region_id", "")).strip()
        text = str(item.get("text", "")).strip()
        if rid not in known or not text or _looks_malformed(text):
            continue
        try:
            conf = float(item.get("confidence"))
        except (TypeError, ValueError):
            conf = None
        readings[rid] = (text, conf)
    return readings, list(data.get("missing", []) or [])
