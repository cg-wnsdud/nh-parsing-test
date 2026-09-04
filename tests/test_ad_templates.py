# -*- coding: utf-8 -*-
"""광고 템플릿 라벨 사전을 못박는다.

근거: 농협 제공 `광고 템플릿(근거규정x, 필수 여부).md`.
`tools/build_ad_templates.py` 가 생성하고 이 파일이 결과를 고정한다.
원본 md 는 `nh-data/` 라 저장소에 없으므로, **여기 적힌 수치가 원본 대조의 유일한 기록**이다.

이 사전은 **판정 도구가 아니라 라벨 사전**이다. 필수여부·기재요령을 담고는 있지만
그건 다음 단계(심의)가 조회할 정적 근거이고, 우리 실행 결과에는 항목명만 나간다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PACK = Path(__file__).resolve().parent.parent / "src/nh_parsing/templates/ad_templates.json"


@pytest.fixture(scope="module")
def pack() -> dict:
    if not PACK.is_file():
        pytest.skip(f"라벨 사전이 없다 — tools/build_ad_templates.py 를 먼저 돌려라: {PACK}")
    return json.loads(PACK.read_text(encoding="utf-8"))


def test_이번_범위는_예금성_대출성_카드_12종이다(pack: dict) -> None:
    """2026-08-24 범위 결정. 투자성 7종은 실데이터 샘플이 0건이라 뺐다."""
    assert len(pack["templates"]) == 12
    assert set(pack["scope"]["included"]) == {"예금성", "대출성", "카드"}
    assert len(pack["scope"]["excluded"]) == 7
    assert all(e.startswith("투자성") for e in pack["scope"]["excluded"])


def test_상품군별_템플릿_수(pack: dict) -> None:
    counts: dict[str, int] = {}
    for tpl in pack["templates"].values():
        counts[tpl["product_group"]] = counts.get(tpl["product_group"], 0) + 1
    assert counts == {"대출성": 3, "예금성": 5, "카드": 4}


def test_모든_템플릿에_공통인_항목은_셋뿐이다(pack: dict) -> None:
    """회사명·유의사항·심의번호. 나머지 26종은 템플릿마다 다르다."""
    sets = [{i["gubun"] for i in t["items"]} for t in pack["templates"].values()]
    common = set.intersection(*sets)
    assert common == {"회사명", "유의사항", "심의번호"}


def test_문구별_필수여부가_항목_집계에_뭉개지지_않는다(pack: dict) -> None:
    """`유의사항` 은 한 항목 안에 O 문구와 △ 문구가 섞인다.

    적립식 유의사항 5줄의 원본 순서는 △ · O · O · O · △ 다. 항목 단위로 뭉치면
    이 차이가 사라져 다음 단계가 "생성형 AI 문구가 없다"를 지적으로 올려 버린다.
    """
    item = _item(pack, "예금성상품-적립식", "유의사항")
    assert item["requirement"] == "O"                     # 항목 집계는 O
    assert [e["requirement"] for e in item["entries"]] == ["△", "O", "O", "O", "△"]


def test_정형과_변수형을_가른다(pack: dict) -> None:
    """정형은 문자열 대조로 끝나고(VLM 불필요), 변수형만 값을 뽑아야 한다."""
    금리 = _item(pack, "예금성상품-적립식", "금리")
    assert 금리["match_mode"] == "변수"
    assert len(금리["entries"]) == 3                       # 방식 ①②③
    assert all(e["kind"] == "변수" for e in 금리["entries"])

    보호 = _item(pack, "예금성상품-적립식", "예금자보호")
    assert 보호["match_mode"] == "정형"


def test_공통_문구는_한_번만_정의된다(pack: dict) -> None:
    """19종에 흩어져 반복되는 문구를 풀로 모았다 — 한 곳만 고쳐지는 사고를 막는다."""
    pool = pack["phrase_pool"]
    ai = next(v for v in pool.values() if "생성형 AI로 제작" in v["text"])
    assert len(ai["used_by"]) == 12                        # 전 템플릿 공통
    소비자권리 = next(v for v in pool.values()
                  if "금융소비자 보호에 관한 법률 제19조" in v["text"])
    assert len(소비자권리["used_by"]) == 8


def test_풀_참조가_전부_실재한다(pack: dict) -> None:
    pool = pack["phrase_pool"]
    for name, tpl in pack["templates"].items():
        for item in tpl["items"]:
            for e in item["entries"]:
                if e["kind"] == "정형":
                    assert e["phrase_id"] in pool, f"{name}/{item['gubun']}: {e['phrase_id']}"


def test_상품명_미노출_템플릿은_항목이_확_줄어든다(pack: dict) -> None:
    """판정 오류의 파급이 큰 지점이라 수치로 박아 둔다.

    같은 대출성인데 노출형 12항목 · 미노출형 3항목이다. 템플릿을 잘못 고르면
    지적이 통째로 사라지거나 없는 지적이 쏟아진다.
    """
    assert pack["templates"]["대출성상품-상품명 노출"]["item_count"] == 12
    assert pack["templates"]["대출성상품-상품명 미노출"]["item_count"] == 3


def test_기재요령이_항목에_붙어_있다(pack: dict) -> None:
    """다음 단계가 조회할 정적 근거. 우리 실행 결과에 복사해 넣지는 않는다."""
    금리 = _item(pack, "예금성상품-적립식", "금리")
    rules = " / ".join(금리["writing_rules"])
    assert "세전" in rules
    assert "30일 이내" in rules


def _item(pack: dict, template: str, gubun: str) -> dict:
    return next(i for i in pack["templates"][template]["items"] if i["gubun"] == gubun)


# ─────────────────────── 판정·라벨링 (ad_template.py) ───────────────────────

from nh_parsing import ad_template as AT  # noqa: E402


def _doc(*regions: list[str]) -> dict:
    """영역마다 줄을 담은 최소 파싱 결과."""
    return {"pages": [{
        "page_no": 1, "canvas_h": 1000,
        "regions": [
            {"region_id": f"p1_r{i:03d}", "bbox": [0, i * 10, 100, i * 10 + 9],
             "lines": [{"text": t, "bbox": [0, 0, 1, 1], "source": "digital"} for t in texts]}
            for i, texts in enumerate(regions)
        ],
        "unassigned_lines": [],
    }]}


def test_판정표가_가리키는_템플릿이_전부_실재한다(pack: dict) -> None:
    """사전에서 이름이 바뀌었는데 판정표를 안 고치면 라벨이 통째로 안 붙는다."""
    assert AT.check_ids(pack) == []


def test_근거가_모자라면_판단불가로_둔다(pack: dict) -> None:
    """억지로 고르면 지적이 통째로 사라지거나 없던 지적이 쏟아진다."""
    assert AT.resolve_template(None, "노출", pack=pack).status == "판단불가"
    assert AT.resolve_template("예금성", None, pack=pack).status == "판단불가"
    # 예금성 노출은 세부유형까지 가야 정해진다 — 파일명에 단서가 없으면 못 정한다.
    assert AT.resolve_template("예금성", "노출", "무제.png", pack=pack).status == "판단불가"
    # 범위 밖 상품군(투자성)도 마찬가지
    assert AT.resolve_template("투자성", "노출", pack=pack).status == "판단불가"


def test_상품군과_상품명노출로_정해지는_것들(pack: dict) -> None:
    r = AT.resolve_template("대출성", "미노출", pack=pack)
    assert (r.status, r.template_id) == ("확정", "대출성상품-상품명 미노출")
    r = AT.resolve_template("예금성", "노출", "2. 예금성상품(적립식).pdf", pack=pack)
    assert (r.status, r.template_id) == ("확정", "예금성상품-적립식")


def test_모든_줄은_어딘가에_남는다(pack: dict) -> None:
    """이 설계의 기둥. 라벨이 붙든 안 붙든 텍스트는 사라지지 않는다."""
    doc = _doc(["NH농협은행"], ["아무 상관 없는 홍보 문구"], ["또 다른 문구"])
    doc["pages"][0]["unassigned_lines"] = [
        {"text": "영역에 못 붙은 낱줄", "bbox": [0, 0, 1, 1], "source": "ocr"}
    ]
    r = AT.label_document(doc, "예금성상품-적립식", pack)
    c = r["completeness"]
    assert c["lines_total"] == 4                      # 미배정 줄도 센다
    assert c["unaccounted"] == 0
    assert c["labeled"] + c["other"] == c["lines_total"]


def test_판단불가여도_텍스트는_버리지_않는다(pack: dict) -> None:
    doc = _doc(["NH농협은행"], ["문구"])
    r = AT.label_document(doc, None, pack)
    assert r["template_id"] is None
    assert r["completeness"]["unaccounted"] == 0
    assert len(r["other_content"]) == 2


def test_문구가_줄_경계를_가로질러도_잡는다(pack: dict) -> None:
    """실측(`2. 예금성상품(적립식)`): 유의사항이 ■ 로 붙어 줄 한가운데서 끊긴다."""
    doc = _doc([
        "■이 예금은 예금자보호법에 따라 원금과 소정의 이자를 합하여 1인당 “1억원까지”",
        "(본 은행의 여타 보호상품과 합산) 보호됩니다.■계좌에 압류",
    ])
    r = AT.label_document(doc, "예금성상품-적립식", pack)
    보호 = _found(r, "예금자보호")
    assert 보호 and 보호[0]["matches"][0]["how"] == "줄경계_가로지름"


def test_끝_마침표_차이로_놓치지_않는다(pack: dict) -> None:
    """정규화 구멍이었다 — 미매칭 상위가 전부 마침표 하나 차이였다(2026-08-24)."""
    doc = _doc(["금융상품을 가입하시기 전에 상품설명서 및 약관을 반드시 읽어보시기 바랍니다"])
    assert _found(r := AT.label_document(doc, "예금성상품-적립식", pack), "유의사항")
    assert r["completeness"]["unaccounted"] == 0


def test_뒤가_잘린_것과_아예_없는_것을_가른다(pack: dict) -> None:
    """`prefix_coverage` 가 그 재료다. 둘을 뭉치면 우리 파싱 결함이 미표시로 둔갑한다."""
    doc = _doc(["계좌에 압류, 가압류, 질권설정 등이 등록될 경우 원금 및 이자지급을 제한"])
    r = AT.label_document(doc, "예금성상품-적립식", pack)
    제한 = _entry(r, "이자지급제한")
    assert 제한["found"] is False
    assert 제한["prefix_coverage"] > 0.8            # 앞부분은 다 있다 = 잘린 것
    assert 제한["prefix_refs"]                       # 어느 줄인지도 짚어 준다

    빈문서 = AT.label_document(_doc(["전혀 다른 내용"]), "예금성상품-적립식", pack)
    assert _entry(빈문서, "이자지급제한")["prefix_coverage"] < 0.3   # 흔적 없음


def test_두_층은_우열을_가리지_않고_나란히_실린다(pack: dict) -> None:
    """`회사명`(=`NH농협은행` 6글자)은 다른 문장 안에도 들어 있다.

    "이 영역에 그 문자열이 있다"(1층)와 "이 영역은 심의번호다"(2층)는 **둘 다 참**이라
    충돌이 아니다. 한동안 이걸 불일치로 셌는데 없는 충돌을 만들어 낸 것이었다.
    """
    doc = _doc(["NH농협은행 준법감시인 심의필 2026-0000(2026.06.01.~2026.12.31.)"])
    r = AT.label_document(doc, "예금성상품-적립식", pack,
                          vlm_labels={"p1_r000": [{"gubun": "심의번호", "confidence": 0.95,
                                                    "line_from": 0, "line_to": 0}]})
    reg = r["region_labels"][0]
    assert reg["gubun"] == "심의번호"                                  # 2층
    assert [h["gubun"] for h in reg["phrase_hits"]] == ["회사명"]       # 1층
    assert reg["phrase_share"] < 0.5     # 영역의 일부일 뿐이라는 것도 값으로 남는다


def test_명시표제는_vlm_한줄밀림을_교정한다(pack: dict) -> None:
    """VLM 재투표 없이 `가입기간:` 등 자기표시 줄만 안전하게 고정한다."""
    doc = _doc([
        "상품 유의사항",
        "·가입대상:개인(1인1계좌)",
        "·판매한도:3만좌(판매한도 소진 시 판매종료)",
        "·가입방법:영업점,비대면(NH올원뱅크)",
        "·가입기간:12개월",
        "·가입금액: 1만원 이상~매월 최대30만원 이하",
        "·기본이자율:2.3%(기준,세전)",
    ])
    # 002 예금성에서 실제로 관측된 한 줄씩 앞당겨진 응답이다.
    bad = {"p1_r000": [
        {"gubun": "유의사항", "confidence": 1.0, "line_from": 0, "line_to": 0},
        {"gubun": "가입대상", "confidence": 1.0, "line_from": 1, "line_to": 1},
        {"gubun": "가입기간", "confidence": 1.0, "line_from": 3, "line_to": 3},
        {"gubun": "가입금액", "confidence": 1.0, "line_from": 4, "line_to": 4},
        {"gubun": "금리", "confidence": 1.0, "line_from": 5, "line_to": 5},
    ]}
    result = AT.label_document(
        doc, "예금성상품-적립식", pack, vlm_labels=bad,
        apply_explicit_line_guard=True,
    )
    refs = {item["gubun"]: item["line_refs"] for item in result["items"]}
    assert refs["가입기간"] == ["p1/p1_r000/L04"]
    assert refs["가입금액"] == ["p1/p1_r000/L05"]
    assert refs["금리"] == ["p1/p1_r000/L06"]
    assert "p1/p1_r000/L03" not in {ref for values in refs.values() for ref in values}
    assert any("명시표제 안전장치" in note for note in doc["notes"])


def test_심의필_앵커가_있으면_전화번호는_심의번호에서_제외한다(pack: dict) -> None:
    doc = _doc(["문의: (1661-3000)", "준법감시인 심의필 2026-0000", "(2026.01.01.~2026.12.31.)"])
    result = AT.label_document(
        doc, "예금성상품-적립식", pack,
        vlm_labels={"p1_r000": [
            {"gubun": "심의번호", "confidence": 0.9, "line_from": 0, "line_to": 2},
            {"gubun": "심의번호", "confidence": 0.9, "line_from": 1, "line_to": 2},
        ]},
        apply_explicit_line_guard=True,
    )
    review = next(item for item in result["items"] if item["gubun"] == "심의번호")
    assert review["line_refs"] == ["p1/p1_r000/L01", "p1/p1_r000/L02"]
    semantic = result["region_labels"][0]["gubun_breakdown"]
    assert semantic == [{"gubun": "심의번호", "confidence": 0.9, "line_from": 1, "line_to": 2}]


# ───────────────────── 영역 라벨링 호출 (2층) ─────────────────────

def _fake_vlm(monkeypatch, responses: list[dict]) -> list[list[str]]:
    """VLM 을 가짜로 갈아 끼우고, 호출마다 물어본 region_id 목록을 기록해 돌려준다."""
    from nh_parsing import gemma_client

    asked: list[list[str]] = []
    seq = list(responses)

    def fake(parts, **kw):
        ids = re.findall(r"region_id=(\S+)", parts[0]["text"])
        asked.append(ids)
        return seq.pop(0) if seq else {"analysis": "", "regions": []}

    monkeypatch.setattr(gemma_client, "chat_json", fake)
    return asked


def test_영역이_많으면_나눠_묻는다(pack: dict, monkeypatch) -> None:
    """쪽당 1회로 묶어 물으면 목록이 길 때 `regions` 가 통째로 빈 배열로 돌아온다
    (실측 2026-08-24: 영역 50개에서 0개 응답, 같은 문서 44개일 때는 36개 정상)."""
    doc = _doc(*[[f"문구 {i}"] for i in range(25)])
    asked = _fake_vlm(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)
    assert [len(a) for a in asked] == [12, 12, 1]


def test_무응답_영역은_노트로_남는다(pack: dict, monkeypatch) -> None:
    """조용히 넘기면 '판단 보류'와 '호출이 깨짐'이 결과에서 똑같이 라벨 0으로 보인다."""
    doc = _doc(["문구 A"], ["문구 B"])
    _fake_vlm(monkeypatch, [{"analysis": "설명", "regions": []}])
    assert AT.label_regions_vlm(doc, "예금성상품-적립식", pack) == {}
    assert any("무응답" in n for n in doc["notes"])


def test_안_물어본_영역을_지어내면_버린다(pack: dict, monkeypatch) -> None:
    doc = _doc(["문구 A"])
    _fake_vlm(monkeypatch, [{"analysis": "", "regions": [
        {"region_id": "p1_r000", "gubun": "회사명", "confidence": 0.9},
        {"region_id": "p9_r999", "gubun": "금리", "confidence": 0.9},
    ]}])
    v = AT.label_regions_vlm(doc, "예금성상품-적립식", pack)
    assert list(v) == ["p1_r000"]


# ─────────── 긴 영역 절단 (실측 `16. 대출성상품` p1_r019, 요율표 56줄) ───────────

def _prompts(monkeypatch, responses: list[dict]) -> list[str]:
    """프롬프트 원문을 그대로 모은다 — 어떤 줄 번호가 실렸는지 보려면 텍스트를 봐야 한다."""
    from nh_parsing import gemma_client

    seen: list[str] = []
    seq = list(responses)

    def fake(parts, **kw):
        seen.append(parts[0]["text"])
        return seq.pop(0) if seq else {"analysis": "", "regions": []}

    monkeypatch.setattr(gemma_client, "chat_json", fake)
    return seen


def test_56줄_영역의_모든_줄이_프롬프트에_실린다(pack: dict, monkeypatch) -> None:
    """예전에는 `lines[:40]` 으로 잘려 L40~L55 가 번호 목록에 아예 없었다 —
    VLM 이 가리킬 방법이 없어 16줄이 조용히 미배정으로 남았다."""
    doc = _doc([f"요율 {i}" for i in range(56)])
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    joined = "\n".join(prompts)
    for index in range(56):
        assert f"[{index:02d}]요율 {index}" in joined, f"L{index:02d} 가 프롬프트에 없다"


def test_긴_영역은_자르지_않고_한번에_묻는다(pack: dict, monkeypatch) -> None:
    doc = _doc([f"요율 {i}" for i in range(56)])
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert len(prompts) == 1
    assert "판정할 줄 0~55 (영역 전체 56줄)" in prompts[0]
    assert "[00]요율 0" in prompts[0]
    assert "[40]요율 40" in prompts[0]
    assert "[55]요율 55" in prompts[0]


def test_한_영역_응답에_복수_라벨을_함께_받는다(pack: dict, monkeypatch) -> None:
    doc = _doc([f"요율 {i}" for i in range(56)])
    _prompts(monkeypatch, [{"analysis": "", "regions": [
        {"region_id": "p1_r000", "gubun": "금리", "confidence": 0.9,
         "line_from": 0, "line_to": 39},
        {"region_id": "p1_r000", "gubun": "우대금리", "confidence": 0.85,
         "line_from": 40, "line_to": 55},
    ]}])
    verdicts = AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    covered = {
        index
        for verdict in verdicts["p1_r000"]
        for index in range(verdict["line_from"], verdict["line_to"] + 1)
    }
    assert covered == set(range(56))
    assert [v["gubun"] for v in verdicts["p1_r000"]] == ["금리", "우대금리"]
    assert not any("판정 기회를 못 얻은 줄" in note for note in doc["notes"])


def test_영역_내부의_부분_무응답은_그_줄만_노트로_남는다(pack: dict, monkeypatch) -> None:
    """한 응답이 영역 일부만 덮어도 내부 구멍을 조용히 삼키지 않는다."""
    doc = _doc([f"요율 {i}" for i in range(56)])
    _prompts(monkeypatch, [{"analysis": "", "regions": [
        {"region_id": "p1_r000", "gubun": "금리", "confidence": 0.9,
         "line_from": 0, "line_to": 19},
        {"region_id": "p1_r000", "gubun": "금리", "confidence": 0.9,
         "line_from": 40, "line_to": 55},
    ]}])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    gap = next(note for note in doc["notes"] if "판정 기회를 못 얻은 줄" in note)
    assert "p1_r000[20-39]" in gap
    assert not any("영역 1개 무응답" in note for note in doc["notes"])


def test_줄범위_밖_응답은_영역_범위로_자르고_노트로_남긴다(pack: dict, monkeypatch) -> None:
    """`line_to` 가 영역 마지막을 넘는 응답을 실제 영역으로 제한한다."""
    doc = _doc([f"요율 {i}" for i in range(25)])
    _prompts(monkeypatch, [{"analysis": "", "regions": [
        {"region_id": "p1_r000", "gubun": "금리", "confidence": 0.9,
         "line_from": -10, "line_to": 999},
    ]}])
    verdicts = AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    spans = sorted((v["line_from"], v["line_to"]) for v in verdicts["p1_r000"])
    assert spans == [(0, 24)]
    assert any("줄범위 밖 응답" in note for note in doc["notes"])


def test_영역_밖만_가리키는_응답은_버리고_노트로_남긴다(pack: dict, monkeypatch) -> None:
    doc = _doc([f"요율 {i}" for i in range(25)])
    _prompts(monkeypatch, [{"analysis": "", "regions": [
        {"region_id": "p1_r000", "gubun": "금리", "confidence": 0.9,
         "line_from": 30, "line_to": 40},
    ]}])
    verdicts = AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert "p1_r000" not in verdicts
    assert any("→버림" in note for note in doc["notes"])


def test_겹친_줄은_오류가_아니고_값으로만_남는다(pack: dict, monkeypatch) -> None:
    """라벨 간 줄 중복은 이 계약이 허용한다 — 다만 몇 줄이 그런지는 적어 둔다."""
    doc = _doc([f"문구 {i}" for i in range(5)])
    _prompts(monkeypatch, [{"analysis": "", "regions": [
        {"region_id": "p1_r000", "gubun": "금리", "confidence": 0.9, "line_from": 0, "line_to": 3},
        {"region_id": "p1_r000", "gubun": "우대금리", "confidence": 0.8, "line_from": 2, "line_to": 4},
    ]}])
    verdicts = AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert len(verdicts["p1_r000"]) == 2
    assert any("겹친 줄" in note and "p1_r000[2-3]" in note for note in doc["notes"])


def test_짧은_영역은_한_요청에_묶여_그대로_간다(pack: dict, monkeypatch) -> None:
    """문맥 보존 변경 뒤에도 짧은 영역은 12개씩 묶어 효율적으로 보낸다."""
    doc = _doc(*[[f"문구 {i}"] for i in range(25)])
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert [len(re.findall(r"region_id=(\S+)", text)) for text in prompts] == [12, 12, 1]
    assert "판정할 줄 0~0 (영역 전체 1줄)" in prompts[0]


def test_120줄을_넘는_단일_영역도_자르지_않는다(pack: dict, monkeypatch) -> None:
    """요청 예산은 영역 사이 배치 기준이지 영역 내부 절단 기준이 아니다."""
    doc = _doc([f"긴 표 {i}" for i in range(121)])
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert len(prompts) == 1
    assert "판정할 줄 0~120 (영역 전체 121줄)" in prompts[0]
    assert "[120]긴 표 120" in prompts[0]


# ─────────────────── 구조 신호 (실측 p1_r002 / p1_r012 오배정) ───────────────────

def test_구조_신호가_프롬프트에_실린다(pack: dict, monkeypatch) -> None:
    doc = _doc(["헤드라인"], ["본문 한 줄"])
    page = doc["pages"][0]
    page["canvas_w"] = 800          # 공용 _doc 은 canvas_h 만 둔다 (실제 파싱 결과는 둘 다 있다)
    page["regions"][0].update({
        "bbox": [80, 100, 720, 200], "label": "doc_title", "layout_score": 0.92,
        "lines": [{"text": "헤드라인", "bbox": [80, 100, 720, 200],
                   "source": "ocr", "confidence": 0.9, "style": None}],
    })
    page["regions"][1].update({
        "bbox": [80, 300, 400, 325], "label": "vision_footnote", "layout_score": 0.88,
        "lines": [{"text": "본문 한 줄", "bbox": [80, 300, 400, 325],
                   "source": "ocr", "confidence": 0.9, "style": None}],
    })
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    text = prompts[0]
    assert "layout_label='doc_title' layout_score=0.92" in text
    assert "layout_label='vision_footnote' layout_score=0.88" in text
    assert "x_ratio=0.10~0.90" in text          # 80/800 ~ 720/800
    assert "y_ratio=0.10" in text               # 100/1000
    assert "w_ratio=0.80" in text and "h_ratio=0.10" in text
    assert "table=없음" in text
    # 줄높이 100px vs 25px → 페이지 중앙값 62.5 대비 1.6x / 0.4x
    assert "line_h=100px line_h_rel=1.6x" in text
    assert "line_h=25px line_h_rel=0.4x" in text
    # 프롬프트가 신호 해석법을 함께 설명해야 모델이 참고 신호로 쓴다.
    assert "layout_label:" in text and "line_h, line_h_rel:" in text


def test_ocr문서처럼_글자크기가_없어도_상대_줄높이를_만든다(pack: dict, monkeypatch) -> None:
    """`style.size_pt` 는 OCR 문서에서 전부 None 이다(실측 4문서 중 3건 0개).
    그래서 크기 신호는 bbox 줄높이로 만든다."""
    doc = _doc(["큰 글씨"], ["작은 글씨"])
    page = doc["pages"][0]
    page["regions"][0]["lines"] = [{"text": "큰 글씨", "bbox": [0, 0, 100, 80],
                                    "source": "ocr", "confidence": 0.9, "style": None}]
    page["regions"][1]["lines"] = [{"text": "작은 글씨", "bbox": [0, 100, 100, 120],
                                    "source": "ocr", "confidence": 0.9, "style": None}]
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert all(line.get("style") is None for r in page["regions"] for line in r["lines"])
    assert "line_h_rel=1.6x" in prompts[0]      # 80 / median 50
    assert "line_h_rel=0.4x" in prompts[0]      # 20 / median 50


def test_bbox가_없으면_구조_신호를_지어내지_않는다(pack: dict, monkeypatch) -> None:
    doc = _doc(["좌표 없는 줄"])
    region = doc["pages"][0]["regions"][0]
    region["bbox"] = None
    region["layout_score"] = None
    region["lines"] = [{"text": "좌표 없는 줄", "bbox": None,
                        "source": "digital", "confidence": None, "style": None}]
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert "x_ratio=? y_ratio=? w_ratio=? h_ratio=?" in prompts[0]
    assert "layout_score=?" in prompts[0]
    assert "line_h=? line_h_rel=?" in prompts[0]


def test_표_영역은_table_있음으로_알린다(pack: dict, monkeypatch) -> None:
    doc = _doc(["표 줄"])
    doc["pages"][0]["regions"][0].update({"label": "table", "table": {"n_rows": 3, "cells": []}})
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert "table=있음" in prompts[0]


def test_paddlex_격자가_비어도_레이아웃이_표면_표로_알린다(pack: dict, monkeypatch) -> None:
    """실측 p1_r019: 요율표인데 `table` 이 None 이라 격자만 보면 '표 아님'이 된다.
    격자의 원천은 VLM 관측이므로 판정 기준을 `ad_export._table_evidence` 와 맞춘다."""
    doc = _doc(["요율 줄"])
    doc["pages"][0]["regions"][0].update({"label": "table", "table": None})
    prompts = _prompts(monkeypatch, [])
    AT.label_regions_vlm(doc, "예금성상품-적립식", pack)

    assert "layout_label='table'" in prompts[0]
    assert "table=있음" in prompts[0]


def _found(r: dict, gubun: str) -> list[dict]:
    item = next(i for i in r["items"] if i["gubun"] == gubun)
    return [f for f in item["fixed_phrases"] if f["found"]]


def _entry(r: dict, gubun: str) -> dict:
    item = next(i for i in r["items"] if i["gubun"] == gubun)
    return item["fixed_phrases"][0]
