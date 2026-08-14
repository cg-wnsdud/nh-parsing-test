# -*- coding: utf-8 -*-
"""디지털+OCR 병합 시 라벨·값이 나란히 중복되던 결함 (pipeline._merge_digital_ocr).

실측 근거(2026-08-13, `1. 예금성상품(거치식).pdf` p1_r001): 표 한 셀의 라벨+값을
디지털 텍스트는 "가입대상개인" 하나로 뭉쳐 읽고 OCR 은 "가입대상"/"개인" 둘로 나눠
읽는다. 대칭 IoU 로는 디지털 박스가 둘 다 포함하는데도 각각 0.48·0.24 로 임계(0.5)를
못 넘어 셋 다(디지털 1 + OCR 2) 살아남아 llm_view 에 "가입대상 | 가입대상개인 | 개인"
으로 중복 노출됐다.

이 결함은 Fix A/B 의 부작용이 아니라 **수정 전에는 이 셀 내용이 애초에 이 영역으로
안 왔다**(가입대상/개인 텍스트가 다른 영역 소속이었다) — A/B 가 처음으로 올바른
영역에 배정하면서 기존에 있던 병합 로직의 약점이 드러났다.

hybrid 89문서 전수 재계산 결과 165건이 이 조건에 걸렸고, 크기비 1.0~69.1배 전부를
확인해 오탐이 없었다(가장 큰 사례도 OCR 이 "※" 기호 하나만, 디지털이 그 기호로
시작하는 문장 전체를 읽은 진짜 중복).

모델 호출 없음.
"""

from __future__ import annotations

from nh_parsing.ir import Line
from nh_parsing.pipeline import _merge_digital_ocr


def test_라벨_값이_합쳐진_디지털과_나뉜_OCR은_중복제거된다():
    """실측 그대로 — 임계(0.5) 를 어느 쪽도 못 넘던 케이스."""
    digital = [Line(text="가입대상개인", bbox=[263, 1365, 482, 1399], source="digital")]
    ocr = [
        Line(text="가입대상", bbox=[256, 1358, 397, 1405], source="ocr"),
        Line(text="개인", bbox=[418, 1358, 493, 1408], source="ocr"),
    ]
    kept = _merge_digital_ocr(digital, ocr)
    assert [l.text for l in kept] == ["가입대상개인"]


def test_기호_하나만_읽은_OCR도_큰_디지털_문장에_중복제거된다():
    """실측 극단값(크기비 69배) — 크기비 상한을 안 두는 이유."""
    digital = [Line(text="※연체이자율 안내 문구가 아주 길게 이어집니다 등등",
                    bbox=[100, 100, 900, 130], source="digital")]
    ocr = [Line(text="※", bbox=[100, 100, 115, 130], source="ocr")]
    kept = _merge_digital_ocr(digital, ocr)
    assert kept == digital


def test_기존_대칭_IoU_로_이미_잡히던_경우는_그대로다():
    """크기가 비슷해 대칭 IoU 만으로도 충분한 경우 — 회귀 확인."""
    digital = [Line(text="동일문구", bbox=[100, 100, 200, 130], source="digital")]
    ocr = [Line(text="동일문구", bbox=[100, 100, 200, 130], source="ocr")]
    kept = _merge_digital_ocr(digital, ocr)
    assert kept == digital


def test_겹치지_않는_OCR_라인은_그대로_남는다():
    digital = [Line(text="가입대상개인", bbox=[263, 1365, 482, 1399], source="digital")]
    far = Line(text="완전히 다른 유의사항", bbox=[100, 3000, 500, 3100], source="ocr")
    kept = _merge_digital_ocr(digital, [far])
    assert far in kept


def test_bbox_없는_라인은_안전하게_처리된다():
    digital = [Line(text="가입대상개인", bbox=[263, 1365, 482, 1399], source="digital")]
    no_box = Line(text="좌표없음", bbox=None, source="ocr")
    kept = _merge_digital_ocr(digital, [no_box])
    assert no_box in kept
