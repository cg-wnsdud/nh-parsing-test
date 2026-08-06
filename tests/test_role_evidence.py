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


# ───────────────── B4b: 부분 응답이 조용히 묻히지 않는가 ─────────────────


def test_VLM이_일부만_판정하면_notes에_남는다(monkeypatch):
    """judge_region_roles 의 반환값(applied)을 버려서 부분 응답을 못 잡던 자리.

    except 는 완전 실패만 잡는다 — VLM 이 정상 응답하면서 영역을 빠뜨리면 그 영역은
    조용히 규칙 폴백 값을 유지했고 notes 에 아무것도 안 남았다.
    """
    from nh_parsing import pipeline
    from nh_parsing.ir import AdPage

    regions = [
        Region(region_id=f"p1_r{i:03d}", bbox=[0, i * 10, 100, i * 10 + 9],
               lines=[_line(f"줄{i}", [0, i * 10, 100, i * 10 + 9])])
        for i in range(3)
    ]
    page = AdPage(page_no=1, parse_route="ocr", canvas_w=100, canvas_h=100, regions=regions)

    monkeypatch.setattr(pipeline, "judge_region_roles", lambda *a, **k: 2)  # 3개 중 2개만
    pipeline._apply_vlm_judgments(page, canvas_img=None)

    note = [n for n in page.notes if "부분 응답" in n]
    assert note, f"부분 응답이 조용히 묻혔다: {page.notes}"
    assert "3개 중 2개" in note[0] and "나머지 1개" in note[0]


def test_전부_판정되면_노이즈를_안_남긴다(monkeypatch):
    from nh_parsing import pipeline
    from nh_parsing.ir import AdPage

    regions = [Region(region_id="p1_r000", bbox=[0, 0, 100, 10],
                      lines=[_line("줄", [0, 0, 100, 10])])]
    page = AdPage(page_no=1, parse_route="ocr", canvas_w=100, canvas_h=100, regions=regions)
    monkeypatch.setattr(pipeline, "judge_region_roles", lambda *a, **k: 1)
    pipeline._apply_vlm_judgments(page, canvas_img=None)
    assert not any("부분 응답" in n for n in page.notes)


def test_라인_없는_빈_박스는_분모에서_빠진다(monkeypatch):
    """빈 검출 박스는 애초에 VLM 에게 안 묻는다 — 세면 가짜 부분응답 경보가 된다.

    실측 225개 영역 중 24개(10.7%)가 글자 없는 껍데기다.
    """
    from nh_parsing import pipeline
    from nh_parsing.ir import AdPage

    regions = [
        Region(region_id="p1_r000", bbox=[0, 0, 100, 10],
               lines=[_line("줄", [0, 0, 100, 10])]),
        Region(region_id="p1_r001", bbox=[0, 20, 100, 30]),   # 빈 껍데기
    ]
    page = AdPage(page_no=1, parse_route="ocr", canvas_w=100, canvas_h=100, regions=regions)
    monkeypatch.setattr(pipeline, "judge_region_roles", lambda *a, **k: 1)
    pipeline._apply_vlm_judgments(page, canvas_img=None)
    assert not any("부분 응답" in n for n in page.notes), page.notes


# ─────── layout_score 는 두 응답 목록을 이어 붙여야 나온다 (2026-08-06) ───────


def test_검출_확신도가_parsing_res_list_블록으로_옮겨진다():
    """StructureV3 는 score 를 layout_det_res 에만 준다 — 우리가 쓰는 목록엔 없다."""
    from nh_parsing.paddlex_client import _attach_det_scores

    primary = [LayoutBlock(label="doc_title", bbox=[70, 84, 399, 150])]
    det = [LayoutBlock(label="doc_title", bbox=[71, 85, 398, 149], score=0.72,
                       source="layout_det_res")]
    assert primary[0].score is None
    _attach_det_scores(primary, det)
    assert primary[0].score == 0.72


def test_겹치지_않는_박스의_확신도는_안_붙인다():
    """애매하면 None 으로 둔다 — 없는 것과 틀린 것 중 없는 쪽이 낫다."""
    from nh_parsing.paddlex_client import _attach_det_scores

    primary = [LayoutBlock(label="text", bbox=[0, 0, 100, 100])]
    _attach_det_scores(primary, [LayoutBlock(label="text", bbox=[500, 500, 600, 600],
                                             score=0.9, source="layout_det_res")])
    assert primary[0].score is None


def test_부분만_겹치면_안_붙인다():
    """절반만 겹치는 박스는 다른 블록이다 (IoU 0.8 문턱)."""
    from nh_parsing.paddlex_client import _attach_det_scores

    primary = [LayoutBlock(label="text", bbox=[0, 0, 100, 100])]
    _attach_det_scores(primary, [LayoutBlock(label="text", bbox=[0, 0, 100, 55],
                                             score=0.9, source="layout_det_res")])
    assert primary[0].score is None


def test_가장_많이_겹치는_박스를_고른다():
    from nh_parsing.paddlex_client import _attach_det_scores

    primary = [LayoutBlock(label="text", bbox=[0, 0, 100, 100])]
    det = [
        LayoutBlock(label="text", bbox=[0, 0, 100, 88], score=0.5, source="layout_det_res"),
        LayoutBlock(label="text", bbox=[1, 1, 99, 99], score=0.9, source="layout_det_res"),
    ]
    _attach_det_scores(primary, det)
    assert primary[0].score == 0.9
