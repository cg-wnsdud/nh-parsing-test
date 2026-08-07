# -*- coding: utf-8 -*-
"""서비스 알맹이 — 모델 호출 없이 도는 것만 여기서 본다.

진짜 파싱까지 도는 e2e 는 모델 서버(PaddleX·Gemma)가 있어야 하고 문서당 100~300초라
테스트에 넣지 않는다. 2026-08-07 에 실파일로 한 번 돌려 확인했다
(`NH농협은행-2026_002-예금성.png` 3.7MB → 138초 → textBlocks 92 · layoutBlocks 53,
계약 검증 통과 · 워커 신원 검사 통과).
"""

import pytest

from nh_parsing.vlm_ocr_service import PARSER_NAME, ParseFailed, parse_to_normalized


def _call(**overrides):
    kwargs = {
        "source_file_id": "SRCFILE-1", "review_id": "REV-1",
        "file_name": "광고.png", "body": b"\x89PNG",
    }
    kwargs.update(overrides)
    return parse_to_normalized(**kwargs)


def test_어댑터_이름은_예약된_슬롯을_쓴다():
    """대상 routing.py:77 이 이름만 올려 둔 슬롯. 워커가 응답의 parserName 을 이
    이름과 대조하므로 여기가 어긋나면 IDENTITY_MISMATCH 로 거부당한다."""
    assert PARSER_NAME == "vlm-ocr"


def test_빈_파일은_모델을_부르기_전에_거절한다():
    """비싼 단계(문서당 100~300초)에 들어가기 전에 걸러야 한다."""
    with pytest.raises(ParseFailed, match="빈 파일"):
        _call(body=b"")


@pytest.mark.parametrize("name", ["", ".", "..", "   "])
def test_쓸_수_없는_파일_이름은_거절한다(name):
    """바이트를 임시 파일로 떨궈 파이프라인에 넘기므로 이름이 경로가 된다."""
    with pytest.raises(ParseFailed):
        _call(file_name=name)


def test_경로가_섞인_파일_이름은_마지막_조각만_쓴다(tmp_path, monkeypatch):
    """요청의 fileName 을 그대로 경로에 붙이면 임시 폴더 밖으로 나갈 수 있다.

    파이프라인이 확장자로 경로를 가르고(pipeline.py:33) 분류가 파일명을 근거로 쓰기
    때문에 이름을 버릴 수는 없다 — 폴더 부분만 떼어 낸다.
    """
    seen = {}

    def fake(path, preview_dir=None):
        seen["name"] = path.name
        seen["parent"] = path.parent
        raise RuntimeError("여기까지만 본다")

    monkeypatch.setattr("nh_parsing.vlm_ocr_service.process_file", fake)
    with pytest.raises(RuntimeError):
        _call(file_name="../../etc/광고.png")
    assert seen["name"] == "광고.png"


def test_hwp는_이_서비스가_맡지_않는다(monkeypatch):
    """대상 라우터가 .hwp 를 hwp-hybrid 로 보내므로 올 일이 없지만, 와도 거절한다 —
    캔버스가 없어 계약(Coordinate.sourceWidth gt=0)을 통과할 수 없다."""
    from nh_parsing.ir import AdDocument, AdPage

    document = AdDocument(doc_id="x", source_file="x.hwp", file_type="hwp")
    document.pages.append(AdPage(page_no=1, parse_route="digital"))
    monkeypatch.setattr(
        "nh_parsing.vlm_ocr_service.process_file", lambda path, preview_dir=None: document
    )
    with pytest.raises(ParseFailed, match="맡지 않는"):
        _call(file_name="광고.hwp")


def test_글자가_하나도_없으면_실패로_올린다(monkeypatch):
    """대상 형제 서비스와 같은 판정(paddleocr_app.py:78 "no readable text").

    빈 문서도 계약은 통과한다. 조용히 돌려주면 워커가 '파싱 성공'으로 적재하고,
    "글자가 없는 광고"와 "파싱이 실패한 광고"가 DB 에서 같아진다.
    """
    from nh_parsing.ir import AdDocument, AdPage

    document = AdDocument(doc_id="x", source_file="x.png", file_type="image")
    document.pages.append(AdPage(page_no=1, canvas_w=720, canvas_h=1000, parse_route="ocr"))
    monkeypatch.setattr(
        "nh_parsing.vlm_ocr_service.process_file", lambda path, preview_dir=None: document
    )
    with pytest.raises(ParseFailed, match="글자가 없다"):
        _call()


def test_요청의_신원을_그대로_돌려준다(monkeypatch):
    """워커가 응답의 reviewId·sourceFileId·parserName 셋을 요청과 대조해 다르면
    거부한다(parser_services.py:63). 우리가 지어내면 안 된다."""
    from nh_parsing.ir import AdDocument, AdPage, Line, Region

    page = AdPage(page_no=1, canvas_w=720, canvas_h=1000, parse_route="ocr")
    page.regions.append(Region(
        region_id="p1_r000", bbox=[0, 0, 720, 200], role="본문", role_confidence=0.9,
        lines=[Line(text="글자", bbox=[10, 20, 110, 60], confidence=0.9, source="ocr")],
    ))
    document = AdDocument(doc_id="x", source_file="x.png", file_type="image")
    document.pages.append(page)
    monkeypatch.setattr(
        "nh_parsing.vlm_ocr_service.process_file", lambda path, preview_dir=None: document
    )
    result = _call(source_file_id="SRCFILE-9", review_id="REV-9")
    assert result["sourceFileId"] == "SRCFILE-9"
    assert result["reviewId"] == "REV-9"
    assert result["parserName"] == PARSER_NAME
    assert result["rawArtifactRef"]          # 필수·빈 문자열 불가
