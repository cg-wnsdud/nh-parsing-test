# -*- coding: utf-8 -*-
"""레이아웃 인식이 실패한 자리를 OCR 라인 좌표만으로 찾아낸다.

**왜 필요한가.** PP-StructureV3 는 글자가 흩어진 화면(앱 목업, 카드 콜라주)에서 영역을
못 만들거나 뭉뚱그린다. 그러면 OCR 은 글자를 다 읽었는데 담을 영역이 없어 낱줄로 남는다
— 실측 001 p1 에서 미배정 39줄. 지금 파이프라인은 이걸 **실패로 인지하지 못한다.**
낱줄이 많아도 "원래 그런 페이지"인지 "레이아웃이 깨진 페이지"인지 구분이 없고, 그래서
경보도 없고 VLM 에게 낱줄 귀속을 물어보는 비용만 계속 낸다.

**어떻게 재나.** 글자 위치를 거꾸로 묶어 "여기에 덩어리가 있었어야 한다"를 만들고,
StructureV3 영역이 그 덩어리를 덮었는지 본다. 덮지 못한 덩어리 = 놓친 영역.

    OCR 라인박스 → 각 박스를 글자높이 배수만큼 팽창 → 겹치는 것끼리 union
    → 덩어리(cluster) → StructureV3 영역이 60% 미만 덮은 덩어리만 남김

이건 픽셀을 다시 보는 게 아니라 **이미 있는 OCR 좌표를 재사용**하는 것이라 공짜다.
bands.py 의 밀도 분할과는 다른 일을 한다 — 저쪽은 '이미지를 어디서 자를까'(픽셀 에지),
이쪽은 '영역이 어디에 있었어야 하나'(OCR 박스).

참고: HyundaiHS orchestrator/core/text_density.py 가 같은 문제(StructureV3 가 복잡한
지면을 거대한 영역 한둘로 뭉갠다)를 같은 방식으로 푼다. 접근만 참고했고 코드는
가져오지 않았다 — 저쪽은 이걸 크롭 ROI 로 승격해서 쓰고, 여기서는 우선 **감지만** 한다.

순수 함수만 둔다 — 모델 호출·I/O 없음.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

# 팽창 배수 (글자높이 기준). 가로는 넉넉히 — 한 줄 안의 단어들은 같은 덩어리다.
# 세로는 짜게 — 문단 사이를 넘어 붙으면 페이지 전체가 덩어리 하나가 된다.
GX_MULT = 1.5
GY_MULT = 0.6
# 덩어리가 영역에 이만큼 덮이면 '인식됐다'로 본다.
MIN_COVER = 0.6
# 낱줄 하나짜리 덩어리는 무시 — 장식 글자 한 조각까지 실패로 세면 경보가 울기만 한다.
MIN_LINES = 2


@dataclass(frozen=True)
class Cluster:
    bbox: list[int]
    line_count: int
    covered: float = 0.0   # StructureV3 영역이 덮은 비율 (0~1)


def _area(b: list[int]) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _overlap(a: list[int], b: list[int]) -> int:
    return _area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def median_line_height(boxes: list[list[int]]) -> float:
    heights = [b[3] - b[1] for b in boxes if len(b) == 4 and b[3] > b[1]]
    return float(median(heights)) if heights else 0.0


def cluster_line_boxes(
    boxes: list[list[int]],
    line_height: float | None = None,
    *,
    gx_mult: float = GX_MULT,
    gy_mult: float = GY_MULT,
) -> list[Cluster]:
    """라인박스를 팽창시켜 겹치는 것끼리 묶는다 (RLSA 계열).

    팽창 폭을 글자높이에 비례시키는 게 핵심 — 고정 픽셀을 쓰면 큰 헤드라인과 작은
    유의사항에 같은 기준이 적용돼 한쪽이 반드시 틀린다.
    """
    boxes = [b for b in boxes if b and len(b) == 4 and b[2] > b[0] and b[3] > b[1]]
    if not boxes:
        return []
    lh = line_height if line_height is not None else median_line_height(boxes)
    gx, gy = max(1.0, lh * gx_mult), max(1.0, lh * gy_mult)

    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grown = [[b[0] - gx, b[1] - gy, b[2] + gx, b[3] + gy] for b in boxes]
    for i, gi in enumerate(grown):
        for j in range(i + 1, len(grown)):
            gj = grown[j]
            if gi[0] < gj[2] and gj[0] < gi[2] and gi[1] < gj[3] and gj[1] < gi[3]:
                parent[find(i)] = find(j)

    groups: dict[int, list[list[int]]] = {}
    for i, box in enumerate(boxes):
        groups.setdefault(find(i), []).append(box)

    out = [
        Cluster(
            bbox=[min(b[0] for b in g), min(b[1] for b in g),
                  max(b[2] for b in g), max(b[3] for b in g)],
            line_count=len(g),
        )
        for g in groups.values()
    ]
    out.sort(key=lambda c: (c.bbox[1], c.bbox[0]))
    return out


def uncovered_clusters(
    clusters: list[Cluster],
    region_boxes: list[list[int]],
    *,
    min_cover: float = MIN_COVER,
    min_lines: int = MIN_LINES,
) -> list[Cluster]:
    """영역이 충분히 덮지 못한 덩어리 = StructureV3 가 놓친 자리.

    한 영역이 아니라 **영역 전체의 합집합**으로 덮였는지 본다. 큰 덩어리가 작은 영역
    여러 개로 나뉘어 인식된 경우를 실패로 세면 안 되기 때문이다.
    """
    missed: list[Cluster] = []
    for c in clusters:
        if c.line_count < min_lines:
            continue
        area = _area(c.bbox)
        if area <= 0:
            continue
        covered = sum(_overlap(c.bbox, r) for r in region_boxes if r) / area
        if covered < min_cover:
            missed.append(Cluster(c.bbox, c.line_count, round(min(covered, 1.0), 3)))
    return missed


def detect(line_boxes: list[list[int]], region_boxes: list[list[int]]) -> list[Cluster]:
    """한 페이지의 라인·영역 좌표로 놓친 덩어리를 찾는다 (면적 기준)."""
    return uncovered_clusters(cluster_line_boxes(line_boxes), region_boxes)


# 덩어리를 '놓쳤다'고 부를 미배정 비율.
MIN_UNASSIGNED_RATIO = 0.5


def missed_blocks(
    assigned: list[list[int]],
    unassigned: list[list[int]],
    *,
    min_ratio: float = MIN_UNASSIGNED_RATIO,
    min_lines: int = MIN_LINES,
) -> list[Cluster]:
    """미배정 줄이 과반인 덩어리 = StructureV3 가 통째로 놓친 블록.

    **면적 기준(detect) 대신 이걸 쓴다.** 면적으로 재면 성긴 덩어리가 전부 걸린다 —
    실측(001 p1): 덩어리 8개·110줄이 '미커버'로 잡혔는데 그 줄 대부분은 이미 영역에
    잘 배정돼 있었다. 덩어리 bbox 는 넓은데 그 안의 영역들이 작아서 생긴 착시였다.
    줄 단위 배정 여부로 재면 같은 페이지에서 **덩어리 1개(8줄)**만 남는다.

    이 함수가 답하는 질문은 "낱줄이 몇 개냐"가 아니라 **"낱줄들이 한 덩어리를
    이루느냐"** 다. 흩어진 UI 글자 27개와, 통째로 빠진 문단 8줄은 성격이 다르다.
    """
    all_boxes = [b for b in list(assigned) + list(unassigned) if b and len(b) == 4]
    if not all_boxes:
        return []
    orphan = {tuple(b) for b in unassigned if b and len(b) == 4}
    out: list[Cluster] = []
    for cluster in cluster_line_boxes(all_boxes):
        cb = cluster.bbox
        inside = [
            b for b in all_boxes
            if b[0] >= cb[0] and b[1] >= cb[1] and b[2] <= cb[2] and b[3] <= cb[3]
        ]
        if len(inside) < min_lines:
            continue
        n_orphan = sum(1 for b in inside if tuple(b) in orphan)
        if n_orphan / len(inside) >= min_ratio:
            out.append(Cluster(cb, n_orphan, round(1 - n_orphan / len(inside), 3)))
    return out
