# -*- coding: utf-8 -*-
"""레이아웃 실패 감지 — '놓친 자리'만 짚고 정상 인식은 건드리지 않는지 본다.

이 모듈이 존재하는 이유는 경보를 울리는 것이므로, **안 울려야 할 때 안 우는 것**이
울려야 할 때 우는 것만큼 중요하다. 오탐이 잦으면 아무도 안 본다.
"""

from nh_parsing.layout_gap import (
    Cluster, cluster_line_boxes, detect, median_line_height, missed_blocks,
    uncovered_clusters,
)


def _line(y, x0=100, x1=400, h=30):
    return [x0, y, x1, y + h]


# ───────────────────── 묶기 ─────────────────────


def test_같은_문단의_줄은_한_덩어리다():
    boxes = [_line(y) for y in (100, 140, 180, 220)]   # 40px 간격, 글자높이 30
    assert len(cluster_line_boxes(boxes)) == 1


def test_멀리_떨어진_문단은_따로다():
    boxes = [_line(100), _line(140), _line(900), _line(940)]
    assert len(cluster_line_boxes(boxes)) == 2


def test_같은_줄의_단어들은_한_덩어리다():
    """가로는 넉넉히 팽창 — 한 줄이 여러 박스로 잘려 나와도 붙어야 한다."""
    boxes = [[100, 100, 200, 130], [230, 100, 330, 130], [360, 100, 460, 130]]
    assert len(cluster_line_boxes(boxes)) == 1


def test_팽창폭은_글자크기에_비례한다():
    """고정 픽셀을 쓰면 큰 헤드라인과 작은 각주 중 한쪽은 반드시 틀린다."""
    big = [[0, 0, 400, 200], [0, 320, 400, 520]]        # 글자높이 200, 간격 120
    small = [[0, 0, 400, 20], [0, 140, 400, 160]]       # 글자높이 20,  간격 120
    assert len(cluster_line_boxes(big)) == 1, "큰 글자는 같은 간격도 가깝다"
    assert len(cluster_line_boxes(small)) == 2, "작은 글자는 같은 간격이 멀다"


def test_글자가_없으면_덩어리도_없다():
    assert cluster_line_boxes([]) == []
    assert median_line_height([]) == 0.0


# ───────────────────── 놓친 자리 판정 ─────────────────────


def test_영역이_덮은_덩어리는_놓친_게_아니다():
    clusters = [Cluster([100, 100, 400, 300], 5)]
    assert uncovered_clusters(clusters, [[90, 90, 410, 310]]) == []


def test_영역이_없는_자리는_놓친_것이다():
    clusters = [Cluster([100, 100, 400, 300], 5)]
    missed = uncovered_clusters(clusters, [[1000, 1000, 1200, 1200]])
    assert len(missed) == 1 and missed[0].covered == 0.0


def test_여러_영역이_나눠_덮어도_인정한다():
    """큰 덩어리가 작은 영역 여러 개로 인식된 것은 실패가 아니다."""
    clusters = [Cluster([100, 100, 400, 300], 6)]
    regions = [[100, 100, 400, 200], [100, 200, 400, 300]]
    assert uncovered_clusters(clusters, regions) == []


def test_절반만_덮이면_놓친_것이다():
    clusters = [Cluster([100, 100, 400, 300], 6)]
    missed = uncovered_clusters(clusters, [[100, 100, 400, 200]])   # 위 절반만
    assert len(missed) == 1 and 0.4 < missed[0].covered < 0.6


def test_낱줄_하나짜리는_무시한다():
    """장식 글자 한 조각까지 실패로 세면 경보가 울기만 하고 아무도 안 본다."""
    assert uncovered_clusters([Cluster([0, 0, 50, 20], 1)], []) == []


def test_영역이_아예_없으면_전부_놓친_것이다():
    boxes = [_line(y) for y in (100, 140, 900, 940)]
    assert len(detect(boxes, [])) == 2


# ───────────────────── 통합 ─────────────────────


def test_정상_페이지는_경보를_울리지_않는다():
    """영역이 글자를 제대로 덮고 있으면 놓친 자리가 0 이어야 한다."""
    boxes = [_line(y) for y in (100, 140, 180, 600, 640, 680)]
    regions = [[90, 90, 410, 220], [90, 590, 410, 720]]
    assert detect(boxes, regions) == []


def test_흩어진_글자를_영역이_못_담으면_짚어낸다():
    """001 실측 상황 — 앱 목업처럼 글자가 흩어지면 StructureV3 가 영역을 못 만든다."""
    boxes = [_line(100), _line(140), _line(900, x0=600, x1=700), _line(940, x0=600, x1=700)]
    regions = [[90, 90, 410, 180]]            # 위 덩어리만 인식됨
    missed = detect(boxes, regions)
    assert len(missed) == 1
    assert missed[0].bbox[1] >= 900, "못 담긴 아래쪽 덩어리를 짚어야 한다"


# ───────────────── 줄 배정 기준 (면적 기준의 착시 교정) ─────────────────


def test_통째로_빠진_문단을_짚는다():
    assigned = [_line(100), _line(140)]
    orphan = [_line(900), _line(940), _line(980)]
    blocks = missed_blocks(assigned, orphan)
    assert len(blocks) == 1 and blocks[0].line_count == 3


def test_이미_배정된_덩어리는_안_짚는다():
    """면적으로 재면 걸리던 착시 — 덩어리 bbox 는 넓은데 안의 영역이 작은 경우."""
    assigned = [_line(y) for y in (100, 140, 180, 220, 260)]
    assert missed_blocks(assigned, []) == []


def test_흩어진_낱줄은_덩어리로_안_센다():
    """001 실측 — 미배정 27줄 중 응집한 것은 8줄뿐이었다. 나머지는 흩어진 UI 글자."""
    scattered = [[0, 0, 40, 20], [700, 1500, 740, 1520], [300, 3000, 340, 3020]]
    assert missed_blocks([], scattered) == []


def test_배정줄이_섞여_있으면_과반일_때만_짚는다():
    cluster_rows = [_line(y) for y in (100, 140, 180, 220)]
    assert missed_blocks(cluster_rows[:3], cluster_rows[3:]) == []      # 1/4 미배정
    assert len(missed_blocks(cluster_rows[:1], cluster_rows[1:])) == 1  # 3/4 미배정
