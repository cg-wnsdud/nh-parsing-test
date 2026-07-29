from __future__ import annotations

"""글자 밀도 기반 분할 — 페이지를 자를 때 '어디서' 와 '얼마씩' 을 내용으로 정한다.

지금까지 페이지 분할이 세 군데에 따로 있었고 기준도 달랐다(OCR 타일 1600px 고정,
스윕 밴드 폭x2.0, 카드 판정은 VLM 질의). 여기서 하나로 모은다.

**왜 밝기가 아니라 에지인가.** '어두운 픽셀 = 글자'로 재면 색 배경이 깔린 광고에서
전부 잉크로 잡혀 무의미해진다(001 실측: 글자 없는 행이 6554행 중 121행=2%). 글자의
특징은 어둡다는 게 아니라 **국소 대비가 크다**는 것이라, 가로 방향 밝기 변화량으로
재면 배경색과 무관해진다(같은 001 에서 31%). 이 차이가 이 모듈의 존재 이유다.

**왜 등량(等量) 분할인가.** VLM 서버는 보낸 이미지 크기와 무관하게 고정 토큰 예산
(~1,050)으로 리샘플한다. 그런데 높이만 균등하게 자르면 조각마다 담기는 글자량이
2.0~2.7배까지 차이 난다(001: 15/20/18/19/40%). 글자 40% 를 담은 조각과 15% 를 담은
조각이 같은 예산을 받는 셈이다. 글자량으로 나누면 이 불균형이 사라지고, 문서마다
최적 밴드비가 엇갈려 '최악이 가장 덜 나쁜' 2.0 을 고를 수밖에 없던 문제
(config.vlm_band_ratio 주석 참조)도 같이 없어진다.

**자르는 위치**는 등량 목표점 근처에서 글자가 없는 행/열로 옮긴다(스냅). 실측상
등량 목표점 10개 전부 ±160px 안에서 깨끗한 행을 찾았다. 그래도 못 찾으면 목표점을
그대로 쓰고 오버랩이 보완한다 — 조용히 실패하지 않도록 사유를 함께 돌려준다.
"""

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw

from .config import SETTINGS

# 밝기 변화량이 이 이상인 픽셀을 '글자 획의 경계'로 본다. 낮추면 사진 노이즈까지
# 글자로 세고, 높이면 얇은 회색 fine-print 를 놓친다.
EDGE_MIN = 25
# 행/열의 에지 픽셀 비율이 이 이하면 '글자 없음'. 자를 수 있는 자리.
CLEAN_MAX = 0.004
# max_span 을 맞추려 개수를 늘리는 횟수 상한 (무한 루프 방지)
_MAX_SPAN_RETRIES = 8


@dataclass
class Band:
    """분할된 조각 하나. offset 은 원본 캔버스에서의 시작 좌표(축에 따라 x 또는 y)."""
    image: Image.Image
    offset: int
    axis: str = "y"
    mass: float = 0.0        # 이 조각이 담은 글자량 (전체 대비 비율)
    snapped: bool = True     # 깨끗한 자리로 옮겨 잘랐는가 (False = 빈틈 못 찾음)


def edge_mask(img: Image.Image) -> Image.Image:
    """글자 획의 경계만 남긴 흑백 마스크. 배경색·밝기와 무관하다.

    가로·세로 두 방향 변화량을 모두 보고 큰 쪽을 쓴다. 한 방향만 보면 그 방향으로
    균일한 획을 통째로 놓친다(가로줄만 있는 표 괘선을 행 프로파일이 못 보는 식).
    numpy 없이 PIL 만으로 — 1px 옮긴 이미지와의 차분이 곧 그 방향의 변화량이다.
    """
    gray = img.convert("L")
    dx = ImageChops.difference(gray, gray.transform(gray.size, Image.AFFINE, (1, 0, 1, 0, 1, 0)))
    dy = ImageChops.difference(gray, gray.transform(gray.size, Image.AFFINE, (1, 0, 0, 0, 1, 1)))
    mask = ImageChops.lighter(dx, dy).point(lambda v: 255 if v > EDGE_MIN else 0)
    # 1px 옮길 때 바깥에서 검은 픽셀이 들어와 마지막 행·열이 통째로 에지로 잡힌다.
    # 그대로 두면 흰 페이지조차 글자가 있는 것으로 나오고, 맨 아래 행은 늘 '자를 수
    # 없는 자리'가 된다. 실제 내용이 아니므로 지운다.
    if mask.width > 1 and mask.height > 1:
        ImageDraw.Draw(mask).rectangle(
            [mask.width - 1, 0, mask.width - 1, mask.height - 1], fill=0
        )
        ImageDraw.Draw(mask).rectangle(
            [0, mask.height - 1, mask.width - 1, mask.height - 1], fill=0
        )
    return mask


def edge_profile(img: Image.Image, axis: str = "y") -> list[float]:
    """축을 따라 훑은 '글자 있음' 프로파일. axis='y' 면 행별, 'x' 면 열별 비율.

    마스크를 한 축으로 1px 폭까지 축소하면, 각 값이 그 행/열의 에지 픽셀 비율이 된다.
    """
    mask = edge_mask(img)
    size = (1, img.height) if axis == "y" else (img.width, 1)
    return [v / 255.0 for v in mask.resize(size, Image.BOX).getdata()]


def _equal_mass_targets(profile: list[float], n: int) -> list[int]:
    """글자량을 n 등분하는 목표 컷 위치. 글자가 없으면 균등 간격으로 되돌아간다."""
    total = sum(profile)
    if n <= 1:
        return []
    if total <= 0:
        step = len(profile) / n
        return [int(step * k) for k in range(1, n)]
    step, acc, targets, k = total / n, 0.0, [], 1
    for pos, v in enumerate(profile):
        acc += v
        while k < n and acc >= step * k:
            targets.append(pos)
            k += 1
        if k >= n:
            break
    return targets


def _snap_to_clean(profile: list[float], pos: int, window: int) -> tuple[int, bool]:
    """목표 컷 위치를 가장 가까운 '글자 없는' 행/열로 옮긴다. 반환 (위치, 옮겼는가).

    이미 깨끗하면 그대로 둔다. window 안에 깨끗한 자리가 없으면 원래 위치를 쓰고
    False 를 돌려준다 — 호출측이 '여기는 글자를 가로지를 수 있다'를 알 수 있게.
    """
    if pos < len(profile) and profile[pos] <= CLEAN_MAX:
        return pos, True
    for delta in range(1, window + 1):
        for cand in (pos - delta, pos + delta):
            if 0 < cand < len(profile) and profile[cand] <= CLEAN_MAX:
                return cand, True
    return pos, False


def plan_cuts(
    profile: list[float], n_bands: int, *, snap_window: int | None = None,
    max_shift: float | None = None,
) -> tuple[list[int], list[bool]]:
    """프로파일 → 컷 위치 목록과 각 컷의 스냅 성공 여부. (이미지 없이 테스트 가능)

    max_shift 는 등량 컷이 균등 간격에서 벗어날 수 있는 한계(균등 간격 대비 비율)다.
    조각 길이가 너무 들쭉날쭉하면 레이아웃 검출이 다른 스케일로 동작해 블록 묶음이
    거칠어진다(실측: 001 영역 78→60, 002 42→31, 그 결과 002 에서 '최고연 7.1%' 를
    담던 작은 영역이 큰 영역에 흡수돼 통독 후보가 부실해졌다). None 이면 제한 없음.
    """
    window = SETTINGS.vlm_band_snap_px if snap_window is None else snap_window
    uniform = len(profile) / n_bands if n_bands else 0
    cuts: list[int] = []
    flags: list[bool] = []
    for k, target in enumerate(_equal_mass_targets(profile, n_bands), start=1):
        if max_shift is not None:
            limit = uniform * max_shift
            target = int(min(max(target, uniform * k - limit), uniform * k + limit))
        pos, ok = _snap_to_clean(profile, target, window)
        # 스냅 때문에 앞 컷을 추월하면 원래 자리를 쓴다 (순서 역전 방지)
        if cuts and pos <= cuts[-1]:
            pos, ok = target, False
        if cuts and pos <= cuts[-1]:
            continue
        cuts.append(pos)
        flags.append(ok)
    return cuts, flags


def band_count_for(img: Image.Image, span: int, axis: str = "y") -> int:
    """기존 규칙이 만들던 것과 같은 조각 개수를 낸다 — 위치만 바꾸고 개수는 유지한다.

    개수까지 함께 바꾸면 회수율이 달라졌을 때 '위치 때문인지 개수 때문인지' 못 가른다.
    span 은 소비처마다 다르다: OCR 타일은 검출기 상한(긴 변 2500px) 때문에 1600px,
    VLM 밴드는 서버의 고정 토큰 예산 때문에 폭x비율. 그 규칙 자체는 호출측이 정한다.
    """
    length = img.height if axis == "y" else img.width
    if length <= span:
        return 1
    n, y = 0, 0
    while y < length:
        y1 = min(y + span, length)
        if length - y1 <= SETTINGS.tile_overlap_px:
            y1 = length
        n += 1
        if y1 >= length:
            break
        y = y1 - SETTINGS.tile_overlap_px
    return n


def vlm_band_span(img: Image.Image) -> int:
    """VLM 한 장이 감당할 세로 길이 — 서버가 크기와 무관하게 고정 토큰을 쓰기 때문."""
    return max(SETTINGS.vlm_band_min_height_px, int(img.width * SETTINGS.vlm_band_ratio))


def content_bands(
    img: Image.Image,
    *,
    axis: str = "y",
    n_bands: int | None = None,
    span: int | None = None,
    overlap: int | None = None,
    profile: list[float] | None = None,
    max_shift: float | None = None,
    max_span: int | None = None,
) -> list[Band]:
    """글자량 등분 + 깨끗한 자리 스냅 + 오버랩으로 페이지를 자른다.

    개수는 n_bands 로 직접 주거나 span(조각 하나가 감당할 길이)으로 정한다. 둘 다
    없으면 VLM 기준을 쓴다. overlap 은 경계에 걸친 내용이 양쪽 어디에서도 안 읽히는
    일을 막는 안전장치라 기본값을 유지한다.

    max_span 을 주면 **어떤 조각도 그 길이를 넘지 않을 때까지 개수를 늘린다.** 글자량만
    맞추면 글자가 성긴 구간이 아주 긴 조각이 되는데, 레이아웃 검출은 조각이 길수록
    거칠게 묶는다(실측: 상한 없이 001 블록 156→121, 002 84→62 로 줄어 002 에서 '최고연
    7.1%' 를 담던 작은 영역이 흡수됐다). 상한을 걸면 조각 수는 늘지만(15→24) 블록은
    332→360 으로 오히려 세밀해지고 골드 회수도 138→139 로 손실 없이 올라간다.
    """
    prof = profile if profile is not None else edge_profile(img, axis)
    length = img.height if axis == "y" else img.width
    if n_bands is None:
        n_bands = band_count_for(img, span or vlm_band_span(img), axis)
    if max_span:
        # 균등 간격으로도 상한을 넘으면 애초에 개수가 모자란다 — 먼저 올려놓고 시작한다
        n_bands = max(n_bands, -(-length // max_span))
    n = n_bands
    ov = SETTINGS.tile_overlap_px if overlap is None else overlap
    total_mass = sum(prof) or 1.0

    if n <= 1:
        return [Band(img, 0, axis, 1.0, True)]

    def build(count: int) -> list[Band]:
        cuts, flags = plan_cuts(prof, count, max_shift=max_shift)
        return bands_from_cuts(img, cuts, axis=axis, overlap=ov, profile=prof, snapped=flags)

    bands = build(n)
    if not max_span:
        return bands

    # 상한을 넘으면 조각 수를 하나씩 늘려 다시 나눈다. 길이 기준으로 억지로 쪼개지
    # 않는 이유: 그러면 컷이 글자량과 무관한 자리에 박혀 오히려 회수가 떨어진다
    # (실측 3문서 골드 139 → 136). 등량 분할을 유지한 채 개수만 늘리는 쪽이 낫다.
    def longest(bs: list[Band]) -> int:
        return max((b.image.height if axis == "y" else b.image.width) for b in bs)

    best = bands
    for _ in range(_MAX_SPAN_RETRIES):
        if longest(bands) <= max_span:
            return bands
        n += 1
        bands = build(n)
        if longest(bands) < longest(best):
            best = bands
    # 글자가 한쪽에 극단적으로 몰려 개수를 늘려도 못 맞추는 경우 — 가장 나은 시도를 쓴다
    return bands if longest(bands) <= max_span else best


def bands_from_cuts(
    img: Image.Image,
    cuts: list[int],
    *,
    axis: str = "y",
    overlap: int | None = None,
    profile: list[float] | None = None,
    snapped: list[bool] | None = None,
) -> list[Band]:
    """확정된 컷 위치로 조각을 만든다. 컷 선정과 자르기를 분리해 두는 이유는,
    호출측이 컷을 한 번 더 손볼 수 있어야 하기 때문이다(스윕은 OCR 라인 좌표로 재검사).
    """
    prof = profile if profile is not None else edge_profile(img, axis)
    length = img.height if axis == "y" else img.width
    ov = SETTINGS.tile_overlap_px if overlap is None else overlap
    total_mass = sum(prof) or 1.0

    edges = [0] + list(cuts) + [length]
    bands: list[Band] = []
    for i in range(len(edges) - 1):
        start, end = edges[i], edges[i + 1]
        # 오버랩은 앞쪽으로만 준다 — 뒤 조각이 앞 경계를 되짚어 보게 해서
        # 경계에 걸친 글자가 최소 한 조각에는 온전히 담기게 한다.
        crop_start = max(0, start - ov) if i > 0 else 0
        if end - crop_start < 1:
            continue
        box = ((0, crop_start, img.width, end) if axis == "y"
               else (crop_start, 0, end, img.height))
        bands.append(Band(
            image=img.crop(box),
            offset=crop_start,
            axis=axis,
            mass=sum(prof[start:end]) / total_mass,
            snapped=(snapped[i - 1] if snapped and i - 1 < len(snapped) else True) if i else True,
        ))
    return bands


# ────────────────────────── 카드(가로 분할) 판정 ──────────────────────────

# 콘텐츠 덩어리가 전체 글자의 이 비율 미만이면 카드가 아니라 여백·장식으로 본다.
# 실측 마진(003 3페이지): 진짜 카드 28~59% vs 카드 아닌 덩어리 0~4.4%.
CARD_MIN_MASS = 0.10
# 이 폭(px) 이상 글자가 없어야 카드 사이 거터로 인정한다.
CARD_MIN_GUTTER = 20


def content_spans(
    profile: list[float], *, min_gap: int = CARD_MIN_GUTTER, min_mass: float = CARD_MIN_MASS
) -> list[tuple[int, int, float]]:
    """프로파일에서 '글자가 있는 덩어리' 목록 → [(시작, 끝, 글자량비중)].

    거터(글자 없는 구간)로 잘라 덩어리를 만들고, 글자량이 미미한 덩어리는 버린다.
    '가장 넓은 빈틈이 경계'라는 규칙은 틀렸다(003 p2 실측: 가장 넓은 566px 빈틈이
    카드 경계가 아니다). 넓이가 아니라 **양쪽 덩어리에 글자가 있는가**로 판단한다.
    """
    total = sum(profile) or 1.0
    gaps: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(profile):
        if v <= CLEAN_MAX:
            start = i if start is None else start
        elif start is not None:
            if i - start >= min_gap:
                gaps.append((start, i))
            start = None
    if start is not None and len(profile) - start >= min_gap:
        gaps.append((start, len(profile)))

    edges = [0] + [(a + b) // 2 for a, b in gaps] + [len(profile)]
    edges = sorted(set(edges))
    spans: list[tuple[int, int, float]] = []
    for a, b in zip(edges, edges[1:]):
        mass = sum(profile[a:b]) / total
        if mass >= min_mass:
            spans.append((a, b, mass))
    return spans


def count_cards_by_density(img: Image.Image) -> tuple[int, list[tuple[int, int, float]]]:
    """가로로 나란한 카드 개수를 좌표 계산만으로 센다 (모델 호출 0회).

    반환 (개수, 덩어리 목록). 1 이면 카드 콜라주가 아니라는 뜻이다.
    개수·경계만 판단한다 — '이 영역이 몇 번 카드냐'는 의미 판단이라 VLM 몫으로 남긴다.
    """
    spans = content_spans(edge_profile(img, "x"))
    return len(spans), spans
