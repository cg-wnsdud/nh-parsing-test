# -*- coding: utf-8 -*-
"""역할 판정에서 **버려지던 정보를 살려만 두는지** 확인한다 (walkthrough §9-⑤ 1단계).

두 가지를 지킨다:
  ① layout_score(StructureV3 확신도)와 role_rule(규칙 판정)이 실제로 남는가
  ② ★ 판정은 하나도 안 바뀌는가 — 이 값들은 재료일 뿐 아무것도 결정하지 않는다

②가 더 중요하다. 프롬프트를 바꾸거나 이 값으로 분기를 만들면, 좋아졌는지 나빠졌는지
잴 기준이 없는 상태에서 판정을 흔드는 것이다.
"""

from nh_parsing.ir import Line, Region
from nh_parsing.paddlex_client import LayoutBlock
from nh_parsing.regions import _LABEL_TO_ROLE, _refine_role, build_regions


def _line(text: str, box: list[int]) -> Line:
    return Line(text=text, bbox=box, source="ocr")


def test_StructureV3_확신도가_Region에_담긴다():
    """전에는 LayoutBlock.score 를 받을 칸이 없어 파싱 직후 사라졌다."""
    blocks = [
        LayoutBlock(label="text", bbox=[0, 0, 100, 50], score=0.92),
        LayoutBlock(label="paragraph_title", bbox=[0, 60, 100, 90], score=0.41),
    ]
    regions, _ = build_regions(blocks, [], canvas_h=1000, page_no=1)
    assert [r.layout_score for r in regions] == [0.92, 0.41]


def test_확신도가_없는_블록은_None_이다():
    """엔진이 score 를 안 주는 경로(layout_det_res 폴백 등)를 0.0 으로 채우지 않는다."""
    regions, _ = build_regions([LayoutBlock(label="text", bbox=[0, 0, 10, 10])],
                               [], canvas_h=100, page_no=1)
    assert regions[0].layout_score is None


def test_규칙_판정이_role_rule_에_남는다():
    """VLM 이 role 을 통째로 덮어도 규칙이 뭐라 했는지 남아 있어야 대조가 된다."""
    region = Region(region_id="p1_r001", bbox=[0, 900, 100, 990], role="본문",
                    lines=[_line("※ 유의사항 : 세부 조건은 상품설명서 참고", [0, 900, 100, 990])])
    _refine_role(region, canvas_h=1000)
    assert region.role == "유의사항"
    assert region.role_rule == "유의사항"
    assert region.role_rule_confidence == 0.9

    # VLM 이 덮어쓰는 상황을 흉내 낸다 (vlm_judge.judge_region_roles 와 같은 대입)
    region.role, region.role_confidence, region.role_source = "본문", 0.7, "vlm"
    assert region.role_rule == "유의사항", "규칙 판정이 흔적 없이 사라지면 대조를 못 한다"
    assert region.role_rule_confidence == 0.9


def test_규칙이_아무것도_안_고쳐도_스냅샷은_남는다():
    """'규칙이 판단하지 않았다'와 '기록이 없다'는 다르다 — 전자는 본문 유지가 판정이다."""
    region = Region(region_id="p1_r002", bbox=[0, 0, 100, 50], role="본문",
                    lines=[_line("평범한 본문입니다", [0, 0, 100, 50])])
    _refine_role(region, canvas_h=1000)
    assert region.role_rule == "본문"
    assert region.role_rule_confidence is None   # 규칙이 확신도를 안 붙인 경우


def test_하단20퍼_휴리스틱도_그대로_기록된다():
    region = Region(region_id="p1_r003", bbox=[0, 850, 100, 990], role="본문",
                    lines=[_line("가입 전 상품설명서를 확인하세요", [0, 850, 100, 990])])
    _refine_role(region, canvas_h=1000)
    assert (region.role, region.role_rule, region.role_rule_confidence) == \
           ("유의사항", "유의사항", 0.5)


# ───────────────── ★ 판정 무변경 — 여기가 이 항목의 핵심 ─────────────────


def test_판정_결과는_하나도_안_바뀐다():
    """label→role 초기 매핑과 규칙 판정 결과가 B6 이전과 같은지 못박는다."""
    cases = [
        # (label, 텍스트, 캔버스 상단 배치, 기대 role)
        ("doc_title", "NH올원e적금", [0, 0, 100, 50], "제목"),
        ("text", "평범한 본문", [0, 0, 100, 50], "본문"),
        ("text", "※ 유의사항 : 조건 확인", [0, 0, 100, 50], "유의사항"),
        ("text", "준법감시인 심의필 2025-0000", [0, 0, 100, 50], "고지문구"),
    ]
    for label, text, box, expect in cases:
        blocks = [LayoutBlock(label=label, bbox=box, score=0.5)]
        regions, _ = build_regions(blocks, [_line(text, box)], canvas_h=1000, page_no=1)
        assert regions[0].role == expect, f"{label}/{text!r} 의 판정이 바뀌었다"


def test_label_초기매핑이_그대로다():
    """1층(label→role)은 규칙보다 앞이라 여기가 흔들리면 전부 흔들린다."""
    assert _LABEL_TO_ROLE["doc_title"] == "제목"
    assert _LABEL_TO_ROLE["text"] == "본문"


def test_새_필드는_판정에_쓰이지_않는다():
    """layout_score 를 극단값으로 줘도 role 이 달라지면 안 된다."""
    box, text = [0, 0, 100, 50], "평범한 본문"
    roles = []
    for score in (0.0, 0.5, 1.0, None):
        blocks = [LayoutBlock(label="text", bbox=box, score=score)]
        regions, _ = build_regions(blocks, [_line(text, box)], canvas_h=1000, page_no=1)
        roles.append(regions[0].role)
    assert len(set(roles)) == 1, f"layout_score 가 판정을 흔든다: {roles}"
