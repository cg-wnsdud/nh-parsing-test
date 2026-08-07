# -*- coding: utf-8 -*-
"""IR → NormalizedDocument v1 변환 — 규칙마다 왜 그 값인지 실측 근거를 함께 고정한다.

여기 있는 숫자는 **2026-08-06 무캐시 실행본**(`out/json`)에서 뽑은 값이다. 파싱이
바뀌면 아래 `test_실산출물_*` 이 먼저 깨져야 한다.

이 테스트는 계약 자체는 검증하지 못한다 — 대상 계약 패키지가 파이썬 3.13 을 배제하기
때문이다. 진짜 `NormalizedDocument.model_validate()` 는 3.12 임시 환경에서 돌린다:

  uv run python tools/export_normalized.py
  uv run --no-project --python 3.12 \\
    --with "C:/Users/cccjj/cginside/농협프로젝트/nh-ad-compliance/packages/parser-contracts" \\
    python tools/verify_contract.py
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nh_parsing.normalized_export import (
    DIGITAL_CONFIDENCE,
    IR_VERSION,
    PARSER_NAME,
    PARSER_RULE_VERSION,
    RULES_ROLE_CONFIDENCE,
    SWEEP_COORDINATE_CONFIDENCE,
    confidence_status,
    export_normalized_document,
)

# 기본은 out/. 다른 실행본으로도 돌릴 수 있어야 한다 — 아래 실산출물 테스트가 한 번의
# 실행에 맞춰 깎인 게 아님을 증명하는 방법이 이것뿐이다. 2026-08-07 에 이렇게 확인했다:
#   NH_OUT=out_run3 uv run python -m pytest tests/test_normalized_export.py -q
OUT_JSON = Path(os.environ.get("NH_OUT") or Path(__file__).resolve().parents[1] / "out") / "json"
STAMP = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)


def _line(text="글자", bbox=(10, 20, 110, 60), confidence=0.9, source="ocr", **extra):
    return {"text": text, "bbox": list(bbox) if bbox else None,
            "confidence": confidence, "source": source, **extra}


def _document(*regions, unassigned=(), canvas=(720, 1000), file_type="image",
              source_file="샘플.png", parse_status="ok"):
    return {
        "doc_id": "샘플", "source_file": source_file, "file_type": file_type,
        "pages": [{
            "page_no": 1, "canvas_w": canvas[0], "canvas_h": canvas[1],
            "parse_route": "ocr", "parse_status": parse_status,
            "regions": list(regions), "unassigned_lines": list(unassigned), "notes": [],
        }],
        "notes": [],
    }


def _region(*lines, region_id="p1_r000", role="본문", role_confidence=0.9, **extra):
    return {"region_id": region_id, "bbox": [0, 0, 720, 200], "role": role,
            "role_confidence": role_confidence, "lines": list(lines), **extra}


def _export(document):
    return export_normalized_document(
        document, source_file_id="file-1", review_id="review-1",
        raw_artifact_ref="ref", parser_version="0.1.0", created_at=STAMP,
    )


# ───────────────────── 빈 텍스트: 문서 하나를 통째로 죽이는 자리 ─────────────────────


def test_빈_텍스트_라인은_내보내지_않는다():
    """대상은 문서 신뢰도를 min(모든 블록)으로 계산한다(service.py:65).

    0.0 하나가 섞이면 나머지를 아무리 잘 읽었어도 문서 전체가 UNREADABLE 로 떨어진다.
    실산출물 11건은 눈으로 확인한 결과 **전부 그림**이었다(눈송이 아이콘 6·별 3개 줄·
    잎 장식·화살표·사진 조각) — 글자가 아니므로 잃는 문구가 없다.
    """
    result = _export(_document(_region(
        _line("있다", confidence=0.9), _line("", confidence=0.0),
    )))
    document = result.document
    assert [b["rawText"] for b in document["textBlocks"]] == ["있다"]
    assert result.stats["dropped_empty_text"] == 1
    assert document["confidence"]["score"] == 0.9   # 0.0 이 min 을 끌어내리지 않았다


def test_걸러_낸_라인은_참조에도_남지_않는다():
    """계약은 relatedTextBlockIds 가 실재하는지 안 본다 — 여기서 막지 않으면 화면이
    없는 블록을 찾는다."""
    result = _export(_document(_region(
        _line("", confidence=0.0), _line("있다"), _line("", confidence=0.0),
    )))
    known = {b["textBlockId"] for b in result.document["textBlocks"]}
    related = result.document["layoutBlocks"][0]["relatedTextBlockIds"]
    assert len(related) == 1 and set(related) <= known


def test_공백만_있는_라인도_같이_걸러진다():
    """대상 document_processor_app 도 `if not text.strip(): return None` 로 같은
    처리를 한다. 현재 산출물에 공백만 있는 라인은 0건이지만 규칙은 같아야 한다."""
    assert _export(_document(_region(_line("   ")))).stats["dropped_empty_text"] == 1


def test_걸러_낸_건수는_경고로_남는다():
    """조용히 사라지면 'OCR 이 상자는 잡았는데 못 읽은 자리'라는 정보까지 없어진다."""
    warnings = _export(_document(_region(_line(""), _line("있다")))).document["warnings"]
    unreadable = [w for w in warnings if w["code"] == "TEXT_DETECTED_BUT_UNREADABLE"]
    assert len(unreadable) == 1
    assert "1건" in unreadable[0]["message"] and unreadable[0]["requiresReview"] is True


# ───────────────────── confidence 가 None 인 자리 두 곳 ─────────────────────


def test_digital_라인의_빈_confidence는_1점이다():
    """PDF 텍스트 레이어를 파서가 그대로 읽은 정본이라 OCR 추정이 아니다.

    실산출물 전수: confidence 가 None 인 라인 25건이 **전부** digital 이고, digital
    이면서 값이 있는 라인은 0건이다(pr-plan §3-3 이 HWP 에 세운 논리와 같다).
    """
    block = _export(_document(_region(
        _line("본문", confidence=None, source="digital")
    ))).document["textBlocks"][0]
    assert block["confidenceScore"] == DIGITAL_CONFIDENCE
    assert block["confidenceStatus"] == "READABLE"


def test_규칙_폴백_영역의_빈_role_confidence는_0점5다():
    """실산출물에서 role_confidence 가 None 인 영역 20건은 전부 role_source='rules'
    (VLM 실패 폴백)다. 같은 폴백인데 점수가 붙은 5건은 전부 0.5 였다 — 지어낸 값이
    아니라 같은 경로의 실측값을 쓴다."""
    layout = _export(_document(_region(
        _line("본문"), role_confidence=None, role_source="rules"
    ))).document["layoutBlocks"][0]
    assert layout["confidenceScore"] == RULES_ROLE_CONFIDENCE


# ───────────────────── 좌표 ─────────────────────


def test_bbox는_x_y_폭_높이와_정규화값으로_바뀐다():
    coordinate = _export(_document(_region(
        _line(bbox=(10, 20, 110, 60))
    ))).document["textBlocks"][0]["coordinate"]
    assert (coordinate["x"], coordinate["y"]) == (10, 20)
    assert (coordinate["width"], coordinate["height"]) == (100, 40)
    assert coordinate["sourceWidth"] == 720 and coordinate["sourceHeight"] == 1000
    assert coordinate["normalizedX"] == pytest.approx(10 / 720)
    assert coordinate["normalizedWidth"] == pytest.approx(100 / 720)
    assert coordinate["sourceUnit"] == "pixel"


def test_스윕_라인은_좌표_신뢰도를_텍스트_신뢰도에서_떼어_낸다():
    """스윕 라인 15건은 15건 전부 bbox 가 [0, y0, canvas_w, y1] — 상자가 가로 전체다.

    눈으로 확인: 720px 폭 상자 하나 안에 서로 다른 문구 여럿이 같은 좌표를 공유한다.
    글자는 맞고 위치를 못 믿는 경우이고, 대상은 coordinateConfidence < 0.5 를
    LIST_ONLY 표시로 떨어뜨린다(results.py:345).
    """
    block = _export(_document(_region(
        _line("스윕", confidence=0.95, source="vlm_sweep", bbox=(0, 100, 720, 264))
    ))).document["textBlocks"][0]
    assert block["confidenceScore"] == 0.95              # 글자는 그대로 믿는다
    assert block["coordinate"]["coordinateConfidence"] == SWEEP_COORDINATE_CONFIDENCE
    assert block["coordinate"]["coordinateConfidence"] < 0.5


def test_보통_라인은_좌표_신뢰도가_텍스트_신뢰도와_같다():
    """대상 헬퍼 service.py:134 가 둘을 같이 묶어 둔다. 상자 품질을 따로 잴 수단이
    없으므로 스윕이 아닌 라인에서는 대상 관례를 그대로 따른다."""
    block = _export(_document(_region(_line(confidence=0.73)))).document["textBlocks"][0]
    assert block["coordinate"]["coordinateConfidence"] == 0.73


def test_캔버스를_넘는_상자는_잘리고_경고로_남는다():
    """현재 산출물에는 0건이다. 서비스가 문서 하나 때문에 통째로 죽는 것보다 낫다 —
    계약 검증기는 x+width > sourceWidth 를 거부한다(models.py:69)."""
    result = _export(_document(_region(_line(bbox=(700, 20, 900, 60)))))
    coordinate = result.document["textBlocks"][0]["coordinate"]
    assert coordinate["x"] + coordinate["width"] <= 720
    assert result.stats["clamped_coordinates"] == 1
    assert any(w["code"] == "COORDINATE_CLAMPED" for w in result.document["warnings"])


def test_폭이_0인_상자는_1px로_올린다():
    """계약 Coordinate 는 width·height 에 gt=0 을 건다. 대상 paddleocr_app.py:72 도
    같은 이유로 max(..., 1.0) 을 쓴다."""
    coordinate = _export(_document(_region(
        _line(bbox=(10, 20, 10, 20))
    ))).document["textBlocks"][0]["coordinate"]
    assert coordinate["width"] > 0 and coordinate["height"] > 0


# ───────────────────── 영역 → layoutBlocks (이 PR 의 핵심) ─────────────────────


def test_역할_이름을_한글_그대로_넘긴다():
    """layoutType 은 자유 문자열이라 우리 역할 9종을 손실 없이 넘길 수 있다.
    대상 paddleocr 는 이 배열을 빈 채로 내보낸다(paddleocr_app.py:79)."""
    document = _export(_document(
        _region(_line(), region_id="p1_r000", role="유의사항"),
        _region(_line(), region_id="p1_r001", role="고지문구"),
    )).document
    assert [b["layoutType"] for b in document["layoutBlocks"]] == ["유의사항", "고지문구"]
    assert [b["layoutBlockId"] for b in document["layoutBlocks"]] == ["p1_r000", "p1_r001"]


def test_영역_좌표_신뢰도는_레이아웃_엔진_점수를_쓴다():
    """confidenceScore(역할 판정 확신도)와 좌표 확신도는 다른 것이다. 상자 자체는
    레이아웃 엔진이 잡았으므로 그 점수(layout_score)를 좌표 쪽에 쓴다."""
    layout = _export(_document(_region(
        _line(), role_confidence=0.9, layout_score=0.31
    ))).document["layoutBlocks"][0]
    assert layout["confidenceScore"] == 0.9
    assert layout["coordinate"]["coordinateConfidence"] == pytest.approx(0.31)


def test_미배정_라인도_글자로_내보내되_어느_영역에도_안_붙인다():
    document = _export(_document(
        _region(_line("영역안")), unassigned=[_line("미배정")]
    )).document
    assert {b["rawText"] for b in document["textBlocks"]} == {"영역안", "미배정"}
    related = [r for b in document["layoutBlocks"] for r in b["relatedTextBlockIds"]]
    ids = {b["textBlockId"] for b in document["textBlocks"] if b["rawText"] == "미배정"}
    assert not (set(related) & ids)


# ───────────────────── 문서 단위 ─────────────────────


def test_파서_메타_4종이_문서와_모든_블록에서_같다():
    """NormalizedDocument 검증기가 이걸 본다(models.py:174) — 하나라도 다르면 거부."""
    document = _export(_document(_region(_line(), _line("둘")))).document
    keys = ("parserName", "parserVersion", "parserRuleVersion", "irVersion")
    expected = tuple(document[k] for k in keys)
    assert expected == (PARSER_NAME, "0.1.0", PARSER_RULE_VERSION, IR_VERSION)
    for block in document["textBlocks"]:
        assert tuple(block[k] for k in keys) == expected
        assert block["fileId"] == document["sourceFileId"]


def test_신원_2개는_인자로_받은_값을_그대로_쓴다():
    """대상 워커가 응답의 reviewId·sourceFileId·parserName 이 요청과 다르면 거부한다
    (parser_services.py:39). 우리가 만들어 내면 안 된다."""
    document = _export(_document(_region(_line()))).document
    assert document["sourceFileId"] == "file-1" and document["reviewId"] == "review-1"
    assert document["documentId"] == "doc-file-1"


def test_sourceFileType은_확장자를_쓴다():
    """우리 file_type 은 png·jpg 를 'image' 로 뭉친다. 대상 서비스들은 전부 확장자를
    넣는다(service.py:70)."""
    assert _export(_document(_region(_line()), source_file="샘플.PNG")
                   ).document["sourceFileType"] == "png"


def test_문서_신뢰도는_블록_최솟값이고_상태가_점수와_맞는다():
    """상태를 임의로 못 적는다 — 계약에 status_matches_score 검증기가 있다."""
    document = _export(_document(_region(
        _line("높다", confidence=0.95), _line("낮다", confidence=0.42),
    ))).document
    assert document["confidence"]["score"] == pytest.approx(0.42)
    assert document["confidence"]["status"] == "UNREADABLE"
    for block in document["textBlocks"]:
        assert block["confidenceStatus"] == confidence_status(block["confidenceScore"])


def test_표는_빈_배열이다():
    """우리 IR 에 표 모델이 없다 — 표도 라인으로 평탄화된다."""
    assert _export(_document(_region(_line()))).document["tables"] == []


def test_진단용_notes를_경고로_옮기지_않는다():
    """notes 는 결함이 아니라 진단 기록이다. 게다가 대상 라우터는 경고가 많을수록
    후보 순위를 깎는다(routing.py `_candidate_rank` 의 -len(warnings))."""
    document = _document(_region(_line()))
    document["notes"] = ["분류: 이 광고는 ..."]
    document["pages"][0]["notes"] = ["타일 7개 처리", "이미지 영역 58개 ..."]
    assert _export(document).document["warnings"] == []


def test_파싱이_온전하지_않은_페이지는_경고로_올라온다():
    warnings = _export(_document(_region(_line()), parse_status="partial")).document["warnings"]
    assert [w["code"] for w in warnings] == ["PAGE_PARSE_NOT_OK"]
    assert warnings[0]["requiresReview"] is True


# ───────────────────── 캔버스가 없는 문서 ─────────────────────


def test_hwp는_변환하지_않고_사유를_남긴다():
    """대상 저장소가 자기 HWP 서비스로 처리한다 — 우리와 **같은 사내 파서**를 쓰고
    좌표 없는 문제를 textPath 로 이미 풀었다(pr-plan §6-1)."""
    result = _export(_document(_region(_line(bbox=None, confidence=None)),
                               canvas=(0, 0), file_type="hwp", source_file="x.hwp"))
    assert result.document is None
    assert "hwp" in result.skipped


def test_캔버스가_0인_문서는_확장자와_무관하게_제외된다():
    """계약을 막는 것은 확장자가 아니라 Coordinate.sourceWidth 의 gt=0 이다."""
    result = _export(_document(_region(_line()), canvas=(0, 0), file_type="image"))
    assert result.document is None and "캔버스 없음" in result.skipped


# ───────────────────── 실산출물 집계 고정 ─────────────────────


def _totals(source: Path):
    totals, skipped = {}, []
    for path in sorted(source.glob("*.json")):
        result = _export(json.loads(path.read_text(encoding="utf-8")))
        if result.skipped:
            skipped.append(path.stem)
            continue
        for key, value in result.stats.items():
            totals[key] = totals.get(key, 0) + value
    return totals, skipped


@pytest.mark.skipif(not OUT_JSON.is_dir(), reason="out/json 없음 — 먼저 파싱을 돌려야 한다")
def test_실산출물_집계_중_안정된_값을_고정한다():
    """⚠️ **라인 총계는 못박지 않는다.** 실행마다 움직이기 때문이다.

    같은 코드·같은 입력으로 2회 돌린 실측(2026-08-06 vs 08-07):

        영역 224          224      같음
        걸러낸 빈 텍스트 11   11      같음
        digital 채움 25    25      같음
        rules 채움 20      20      같음
        스윕 라인 15  →    17      ★ 갈림
        라인 총계 488 →   490      ★ 갈림 (스윕 +2 가 그대로 설명한다)

    스윕 회수가 비결정이라는 건 `ir.py:29` 에 이미 적혀 있다. 개수를 합격 조건으로
    박으면 코드가 멀쩡해도 다음 실행에서 빨간불이 뜬다. 그래서 흔들리는 값은 개수
    대신 **성질**로 검사한다(아래 test_스윕_라인은_전부_...).
    """
    totals, skipped = _totals(OUT_JSON)

    assert len(skipped) == 1                                # HWP 004 만 제외
    assert totals["layout_blocks"] == 224
    assert totals["dropped_empty_text"] == 11
    assert totals["digital_filled_confidence"] == 25
    assert totals["rules_filled_role_confidence"] == 20
    assert totals["clamped_coordinates"] == 0               # 좌표는 손 안 대고 통과한다
    assert totals["layout_blocks_without_coordinate"] == 0


@pytest.mark.skipif(not OUT_JSON.is_dir(), reason="out/json 없음 — 먼저 파싱을 돌려야 한다")
def test_입력_라인이_한_줄도_새지_않는다():
    """실행마다 총계가 달라져도 **이 항등식은 항상 참이어야 한다.**

    라인은 내보내지거나 걸러지거나 둘 중 하나다. 셋째 길(조용히 사라짐)이 생기면
    여기서 잡힌다 — 개수를 못박는 것보다 이쪽이 진짜 지키려던 것이다.
    """
    totals, _ = _totals(OUT_JSON)
    assert totals["lines_in"] == totals["text_blocks"] + totals["dropped_empty_text"]


@pytest.mark.skipif(not OUT_JSON.is_dir(), reason="out/json 없음 — 먼저 파싱을 돌려야 한다")
def _sweep_block_ids(source):
    """스윕 라인의 textBlockId — 변환기와 같은 규칙으로 짚는다.

    ⚠️ 텍스트로 짝지으면 안 된다. 003 재실행본에서 'Last Updated' 가 스윕으로도,
    일반 OCR 라인으로도 잡혀 엉뚱한 블록이 걸렸다(2026-08-07 실측).
    """
    ids = set()
    for page in source["pages"]:
        for region in page["regions"]:
            for index, line in enumerate(region["lines"]):
                if line["source"] == "vlm_sweep":
                    ids.add(f"{region['region_id']}_t{index:03d}")
        for index, line in enumerate(page["unassigned_lines"]):
            if line["source"] == "vlm_sweep":
                ids.add(f"p{page['page_no']}_u_t{index:03d}")
    return ids


@pytest.mark.skipif(not OUT_JSON.is_dir(), reason="out/json 없음 — 먼저 파싱을 돌려야 한다")
def test_스윕_라인은_전부_좌표_신뢰도가_낮다():
    """개수(실행마다 15~17)가 아니라 성질을 본다 — 몇 개가 나오든 전부 낮아야 한다."""
    checked = 0
    for path in sorted(OUT_JSON.glob("*.json")):
        source = json.loads(path.read_text(encoding="utf-8"))
        result = _export(source)
        if result.skipped:
            continue
        sweep_ids = _sweep_block_ids(source)
        for block in result.document["textBlocks"]:
            if block["textBlockId"] not in sweep_ids:
                continue
            checked += 1
            assert block["coordinate"]["coordinateConfidence"] < 0.5, (
                f"{path.stem} {block['textBlockId']}: 스윕인데 좌표 신뢰도가 높다"
            )
    assert checked > 0, "스윕 라인이 하나도 없다 — 표본이 바뀌었는지 확인할 것"


@pytest.mark.skipif(not OUT_JSON.is_dir(), reason="out/json 없음 — 먼저 파싱을 돌려야 한다")
def test_실산출물_참조가_전부_실재하고_아이디가_고유하다():
    for path in sorted(OUT_JSON.glob("*.json")):
        result = _export(json.loads(path.read_text(encoding="utf-8")))
        if result.skipped:
            continue
        document = result.document
        ids = [b["textBlockId"] for b in document["textBlocks"]]
        assert len(ids) == len(set(ids)), f"{path.stem}: textBlockId 중복"
        layout_ids = [b["layoutBlockId"] for b in document["layoutBlocks"]]
        assert len(layout_ids) == len(set(layout_ids)), f"{path.stem}: layoutBlockId 중복"
        known = set(ids)
        for layout in document["layoutBlocks"]:
            missing = [r for r in layout["relatedTextBlockIds"] if r not in known]
            assert not missing, f"{path.stem} {layout['layoutBlockId']}: 없는 블록 {missing}"


@pytest.mark.skipif(not OUT_JSON.is_dir(), reason="out/json 없음 — 먼저 파싱을 돌려야 한다")
def test_실산출물_변환은_두_번_돌려도_같다():
    """재처리 정책(흐름문서 §7-4)의 전제. createdAt 만 인자로 고정하면 나머지는
    같은 입력에서 같은 결과가 나와야 한다."""
    for path in sorted(OUT_JSON.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        first, second = _export(document), _export(document)
        assert first.document == second.document and first.stats == second.stats
