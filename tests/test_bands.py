# -*- coding: utf-8 -*-
"""글자 밀도 기반 분할 테스트.

핵심 성질 두 가지를 고정한다.
  1) 색 배경에 속지 않는다 — '어두움'이 아니라 '국소 대비'로 재기 때문.
  2) 조각마다 담기는 글자량이 균등해진다 — VLM 이 조각 크기와 무관하게 고정 예산을
     쓰므로, 글자량이 몰린 조각이 손해를 본다.
"""

import collections

from PIL import Image, ImageDraw

from nh_parsing.bands import (
    CLEAN_MAX, band_count_for, content_bands, content_spans, vlm_band_span,
    count_cards_by_density, edge_profile, plan_cuts,
)


def _page(width=400, height=1200, bg=(255, 255, 255), text_rows=()):
    """text_rows = [(y0, y1), ...] 구간에 가로 줄무늬 글자를 그린 가짜 페이지."""
    img = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(img)
    for y0, y1 in text_rows:
        for y in range(y0, y1, 6):       # 6px 간격 줄무늬 = 글자 획 흉내
            d.line([(20, y), (width - 20, y)], fill=(0, 0, 0), width=2)
    return img


# ───────────────────────── 배경색에 속지 않는가 ─────────────────────────


def test_컬러_배경은_글자로_세지_않는다():
    """001 실측 문제 — 전폭 컬러 패널을 '어두움'으로 재면 전부 글자로 잡힌다."""
    dark_bg = _page(bg=(40, 60, 120))            # 어둡지만 글자는 없는 페이지
    prof = edge_profile(dark_bg, "y")
    assert max(prof) <= CLEAN_MAX, "균일한 색 배경은 글자가 없는 것으로 나와야 한다"


def test_컬러_배경_위의_글자는_잡는다():
    img = _page(bg=(40, 60, 120), text_rows=[(500, 700)])
    prof = edge_profile(img, "y")
    assert max(prof[500:700]) > CLEAN_MAX
    assert max(prof[:400]) <= CLEAN_MAX


# ───────────────────────── 등량 분할 ─────────────────────────


def test_글자가_몰린_페이지를_글자량으로_나눈다():
    """아래쪽에 글자가 몰린 페이지 — 높이로 반 자르면 한쪽이 텅 빈다."""
    img = _page(height=1200, text_rows=[(100, 200), (700, 1150)])
    bands = content_bands(img, n_bands=2, overlap=0)

    assert len(bands) == 2
    masses = [b.mass for b in bands]
    assert abs(masses[0] - masses[1]) < 0.15, f"글자량이 고르지 않다: {masses}"
    # 높이 기준 반절(600)보다 훨씬 아래에서 잘려야 한다
    assert bands[1].offset > 600


def test_컷은_글자_없는_자리로_옮겨진다():
    img = _page(height=1200, text_rows=[(0, 560), (640, 1200)])
    bands = content_bands(img, n_bands=2, overlap=0)
    prof = edge_profile(img, "y")

    # 정확한 픽셀은 선 렌더링에 달렸으므로 '빈 구간 안이고 글자가 없는 자리'로 본다
    cut = bands[1].offset
    assert 550 <= cut <= 645, f"빈 구간(약 560~640) 안에서 잘려야 한다 (실제 {cut})"
    assert prof[cut] <= CLEAN_MAX, "자른 자리에 글자가 있으면 안 된다"
    assert bands[1].snapped is True


def test_빈틈이_없으면_실패를_숨기지_않는다():
    """글자가 빈틈없이 꽉 찬 페이지 — 어디를 잘라도 글자를 가로지른다.

    모든 행에 획이 걸치도록 세로 줄무늬로 채운다(가로 줄무늬는 줄 사이가 비어 있어
    자를 자리가 생긴다).
    """
    img = Image.new("RGB", (400, 1200), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for x in range(20, 380, 6):
        d.line([(x, 0), (x, 1200)], fill=(0, 0, 0), width=2)

    _, flags = plan_cuts(edge_profile(img, "y"), 3, snap_window=20)
    assert any(f is False for f in flags), "스냅 실패를 True 로 보고하면 안 된다"


def test_오버랩은_경계를_되짚는다():
    img = _page(height=1200, text_rows=[(100, 1100)])
    bands = content_bands(img, n_bands=2, overlap=50)
    assert bands[1].offset == bands[0].image.height - 50


def test_조각_개수는_기존_규칙과_같다():
    """개수까지 바꾸면 회수율 변화가 위치 때문인지 개수 때문인지 못 가른다."""
    tall = _page(width=720, height=6000, text_rows=[(0, 6000)])
    assert len(content_bands(tall)) == band_count_for(tall, vlm_band_span(tall))
    # 소비처가 다른 span 을 주면 개수도 그 규칙을 따른다 (OCR 타일 1600px 등)
    assert len(content_bands(tall, span=1600)) == band_count_for(tall, 1600)


def test_짧은_페이지는_자르지_않는다():
    short = _page(width=720, height=800, text_rows=[(0, 800)])
    bands = content_bands(short)
    assert len(bands) == 1 and bands[0].mass == 1.0


def test_글자가_아예_없으면_균등_간격으로_되돌아간다():
    blank = _page(height=900)
    cuts, _ = plan_cuts(edge_profile(blank, "y"), 3)
    assert cuts == [300, 600]


# ───────────────────────── 카드(가로) 판정 ─────────────────────────


def _cards(width, height, columns):
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for x0, x1 in columns:
        for x in range(x0, x1, 6):
            d.line([(x, 20), (x, height - 20)], fill=(0, 0, 0), width=2)
    return img


def test_카드_개수를_거터로_센다():
    img = _cards(900, 600, [(20, 260), (330, 570), (640, 880)])
    count, spans = count_cards_by_density(img)
    assert count == 3, f"3장이어야 한다 (실제 {count}, {spans})"


def test_글자량이_미미한_덩어리는_카드가_아니다():
    """003 p2 실측 — 가장 넓은 빈틈이 카드 경계가 아니고, 끝에 붙은 얇은 요소가 있다."""
    img = _cards(900, 600, [(20, 400), (450, 700), (860, 870)])
    count, spans = count_cards_by_density(img)
    assert count == 2, f"얇은 장식은 빼고 2장이어야 한다 (실제 {count}, {spans})"


def test_단일_패널은_카드가_아니다():
    img = _cards(900, 600, [(20, 880)])
    assert count_cards_by_density(img)[0] == 1


def test_content_spans_는_비중을_함께_돌려준다():
    img = _cards(900, 600, [(20, 400), (500, 880)])
    spans = content_spans(edge_profile(img, "x"))
    assert len(spans) == 2
    assert abs(sum(m for _, _, m in spans) - 1.0) < 0.05


def test_높이_상한을_넘으면_조각을_더_쪼갠다():
    """글자량만 맞추면 성긴 구간이 아주 긴 조각이 되고, 레이아웃 검출이 거칠어진다.

    실측(002): 상한 없이 2157px 짜리 조각이 생겨 레이아웃 블록이 84→62 로 줄었고,
    그 탓에 작은 영역이 큰 영역에 흡수됐다.
    """
    img = _page(width=720, height=6000, text_rows=[(200, 2600), (3400, 5800)])

    loose = content_bands(img, n_bands=3)
    assert max(b.image.height for b in loose) > 1600, "이 상황을 재현해야 의미가 있다"

    capped = content_bands(img, n_bands=3, max_span=1600)
    assert max(b.image.height for b in capped) <= 1600
    assert len(capped) > len(loose), "상한을 지키려면 조각이 늘어야 한다"


def test_글자가_극단적으로_몰리면_최선을_쓰되_더_나쁘게_하지_않는다():
    """등량 분할은 글자 없는 긴 구간을 못 쪼갠다 — 개수를 늘려도 상한을 못 맞출 수 있다.

    이때 길이 기준으로 억지로 자르면 컷이 글자량과 무관한 자리에 박혀 오히려 회수가
    떨어진다(실측 3문서 골드 139 → 136). 못 맞추더라도 등량 분할을 유지한다.
    """
    img = _page(width=720, height=6000, text_rows=[(0, 400), (5600, 6000)])
    capped = content_bands(img, n_bands=3, max_span=1600)

    # 상한을 못 맞출 수는 있어도, 상한 없는 경우보다 나빠지지는 않아야 한다
    loose_max = max(b.image.height for b in content_bands(img, n_bands=3))
    assert max(b.image.height for b in capped) <= loose_max


def test_상한이_이미_지켜지면_조각을_안_늘린다():
    img = _page(width=720, height=6000, text_rows=[(0, 6000)])
    assert len(content_bands(img, n_bands=5, max_span=1600)) == len(
        content_bands(img, n_bands=5)
    )


# ───────────────────── 밴드 통합판독의 영역 절단 방지 ─────────────────────


def test_밴드_크롭은_맡은_영역을_통째로_담는다():
    """경계가 영역을 반토막 내면 그 영역은 반쪽만 보인 채로 판독된다.

    실측(2026-07-28): 영역의 5~14%가 밴드 경계를 가로지르고, 하필 그중에 값을 하던
    것들이 있었다 — 001 p1_r069(우대금리 조건 교정), 올원e p1_r014(원문자 '① 0.1%p').
    """
    from PIL import Image as _Image

    from nh_parsing import pipeline
    from nh_parsing.ir import AdPage, Line, Region

    canvas = _Image.new("RGB", (720, 3000), "white")
    # 밴드 경계 근처(y=1400~1600)에 걸치는 영역 하나를 심는다
    page = AdPage(
        page_no=1, canvas_w=720, canvas_h=3000, parse_route="ocr",
        regions=[Region(region_id="p1_r000", bbox=[50, 1400, 700, 1600], role="본문",
                        lines=[Line(text="경계에 걸친 영역", bbox=[50, 1400, 700, 1600],
                                    source="ocr")])],
    )

    seen: list[tuple[int, int]] = []

    def fake_read(crop, entries):
        seen.append((crop.width, crop.height))
        # 반환은 (판독, 누락문구, 버린이유집계) 3-tuple. 2-tuple 로 두면 언팩 ValueError 가
        # pipeline 의 try 에 잡혀 "밴드 통합판독 실패" 노트로 둔갑하고, 이 테스트는 다른
        # 노트를 보고 그대로 통과한다 — 2026-08-06 실제로 그렇게 통과하고 있었다.
        return {rid: (txt, 0.9) for rid, txt in entries}, [], collections.Counter()

    import nh_parsing.vlm_direct as vd
    orig = vd.read_band_regions
    vd.read_band_regions = fake_read
    try:
        pipeline._merged_band_read(page, canvas, [])
    finally:
        vd.read_band_regions = orig

    assert seen, "영역을 맡은 밴드가 최소 하나는 호출돼야 한다"
    # 200px 짜리 영역이 통째로 들어가려면 크롭 높이가 그 이상이어야 한다
    assert max(h for _, h in seen) >= 200
    assert any("크롭 확장" in n for n in page.notes), "확장 사실을 노트로 남겨야 한다"
    # 성공 경로를 재는 테스트다 — 실패 노트가 있으면 무엇을 쟀는지 알 수 없다
    assert not any("통합판독 실패" in n for n in page.notes), page.notes
    assert page.regions[0].vlm_reading == "경계에 걸친 영역"
