# -*- coding: utf-8 -*-
"""스윕-기존줄 중복판정이 방향성 없이 버리던 결함(§E) — _resolve_sweep_duplicates.

포함관계만으로 "같은 물리적 줄"을 판정하면, 스윕이 기존 줄을 포함하면서 훨씬 더
긴 경우(로고·장식 문구처럼 디지털/OCR 이 놓친 부분을 스윕만 더 읽어낸 경우)까지
"중복"으로 버려진다. 실측(2026-08-13, `1. 카드상품.pdf`): 스윕 '올바른 NEW HAVE
주요 서비스' 가 기존 줄 '주요서비스' 를 뒤쪽에 포함한다는 이유로 통째로 삭제돼
카드 브랜드명 'NEW HAVE' 가 전 출력에서 사라졌다. 반대 방향(스윕이 기존 줄의
부분집합 — 원문자를 숫자로 뭉갠 진짜 중복)은 기존대로 재판독해 흡수해야 한다.
"""

from nh_parsing import pipeline, vlm_direct
from nh_parsing.ir import AdPage, Line, Region


def _page_with_line(text: str) -> AdPage:
    bbox = [100, 100, 300, 140]
    region = Region(region_id="p1_r0", bbox=bbox, role="본문",
                     lines=[Line(text=text, bbox=bbox, source="ocr")])
    return AdPage(page_no=1, canvas_w=1000, canvas_h=1000, parse_route="hybrid", regions=[region])


def test_스윕이_기존줄을_포함하며_훨씬_길면_버리지_않는다(monkeypatch):
    """실측 재현 — 'NEW HAVE' 처럼 기존 줄 밖의 내용은 잃으면 안 된다."""
    page = _page_with_line("주요서비스")
    sweep = Line(text="올바른 NEW HAVE 주요 서비스", bbox=[0, 90, 1000, 150], source="vlm_sweep")

    called = {"n": 0}
    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("잔여가 있으면 재판독을 호출하지 않아야 한다(비용 낭비 방지)")
    monkeypatch.setattr(vlm_direct, "transcribe_line_crop", boom)

    remaining = pipeline._resolve_sweep_duplicates(page, [sweep], canvas_img=None)

    assert remaining == [sweep], "잔여 내용이 있는 스윕은 버리지 않고 흘려보내야 한다"
    assert called["n"] == 0
    assert page.regions[0].lines[0].vlm_reading is None, "엉뚱한 후보가 붙으면 안 된다"


def test_진짜_중복은_기존처럼_재판독해_후보로_붙인다(monkeypatch):
    """반대쪽(스윕 core == 기존줄 core, 원문자→숫자 오독) — 진짜 중복은 그대로 흡수돼야 한다."""
    page = _page_with_line("10.1%p:NH올원e통장")
    sweep = Line(text="① 0.1%p : 「NH올원e통장」", bbox=[0, 90, 1000, 150], source="vlm_sweep")

    monkeypatch.setattr(vlm_direct, "transcribe_line_crop",
                         lambda bbox, canvas_img: "10.1%p : NH올원e통장")

    remaining = pipeline._resolve_sweep_duplicates(page, [sweep], canvas_img=None)

    assert remaining == [], "진짜 중복은 흘려보내지 않고 흡수해야 한다"
    assert page.regions[0].lines[0].vlm_reading == "10.1%p : NH올원e통장"


def test_잔여가_짧으면_기존처럼_재판독_경로를_탄다(monkeypatch):
    """경계값 — 잔여 4자 미만은 여전히 재판독으로 확인 후 흡수한다."""
    page = _page_with_line("가입기간")
    sweep = Line(text="가입기간요", bbox=[0, 90, 1000, 150], source="vlm_sweep")  # 잔여 1자("요")

    monkeypatch.setattr(vlm_direct, "transcribe_line_crop", lambda bbox, canvas_img: "가입기간요")

    remaining = pipeline._resolve_sweep_duplicates(page, [sweep], canvas_img=None)

    assert remaining == [], "짧은 잔여는 기존처럼 중복 처리돼야 한다(경계값 회귀 방지)"


def test_스윕이_기존줄의_부분집합이면_반대방향도_그대로_동작한다(monkeypatch):
    """스윕이 기존 줄보다 짧게 읽은 경우(진짜 중복) — 이 방향은 건드리지 않았다."""
    page = _page_with_line("가입기간 12개월 단일")
    sweep = Line(text="가입기간", bbox=[0, 90, 1000, 150], source="vlm_sweep")

    monkeypatch.setattr(vlm_direct, "transcribe_line_crop",
                         lambda bbox, canvas_img: "가입기간 12개월 단일")

    remaining = pipeline._resolve_sweep_duplicates(page, [sweep], canvas_img=None)

    assert remaining == [], "스윕이 기존 줄의 부분집합인 반대방향은 항상 중복 처리돼야 한다"
