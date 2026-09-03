# -*- coding: utf-8 -*-
"""광고 템플릿 판정 + 라벨링 — **관측만 한다. 심의는 다음 단계 몫이다.**

무엇을 하나:
  1. `resolve_template()`  파싱 결과의 분류값으로 12종 중 하나를 고른다. 못 고르면 판단불가.
  2. `label_document()`    파싱된 줄마다 템플릿 항목명(`유의사항`·`금리` …)을 붙인다.

무엇을 **안 하나**:
  · 위반 판정. "이 문구가 없다"까지가 우리 몫이고 "그래서 위반이다"는 하류가 낸다.
  · 문구 다듬기. 붙인 라벨은 곁가지이고 원문은 손대지 않는다.

**설계 기둥 — 모든 텍스트는 어딘가에 남는다.**
파싱된 줄은 라벨이 붙든 안 붙든 전부 출력에 실린다. `completeness.unaccounted == 0`
이 성립하지 않으면 라벨링이 텍스트를 삼킨 것이므로 그 자체가 결함이다.
`unassigned_lines`(영역에 못 붙은 낱줄)도 텍스트라서 함께 싣는다.

**공백에 기대지 않는다.** 2026-08-27부터 파서는 PDF 텍스트 레이어의 일반 공백을
보존하지만, OCR 입력이나 문서 제작 방식에 따라 띄어쓰기는 여전히 달라질 수 있다.
그래서 대조는 양쪽에서 공백을 모두 지우고 한다 — 원문의 공백 보존 여부와 무관하게
템플릿 문구 대조가 같은 결과를 내도록 하기 위함이다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

PACK_PATH = Path(__file__).resolve().parent / "templates" / "ad_templates.json"

_PACK_CACHE: dict | None = None


# ───────────────────────────── 사전 읽기 ─────────────────────────────

def load_pack(path: Path | None = None) -> dict:
    """라벨 사전을 읽는다(한 번만 읽고 캐시)."""
    global _PACK_CACHE
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))
    if _PACK_CACHE is None:
        _PACK_CACHE = json.loads(PACK_PATH.read_text(encoding="utf-8"))
    return _PACK_CACHE


def template_ids(pack: dict | None = None) -> list[str]:
    return sorted((pack or load_pack())["templates"])


# ───────────────────────────── 문자열 정규화 ─────────────────────────────

_LEAD = re.compile(r"^[\-–—※•·▪◦■□*\s]+")
_TRAIL = re.compile(r"[.。·\s]+$")
_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "＂": '"'})


def normalize(text: str) -> str:
    """광고 줄 대조용 정규화. 공백을 **전부** 지운다(위 모듈 주석의 파서 결함 대응).

    앞머리 글머리표(`- `, `※ `, `■ `)를 지운다 — 템플릿 예시문구는 `- ` 로 시작하지만
    광고는 `■` 를 쓰거나 아무것도 안 쓴다. 표기 차이지 내용 차이가 아니다.

    **문장 끝 마침표는 지우지 않는다.** 이 값은 줄을 이어 붙여 영역 텍스트를 만드는 데도
    쓰이는데(`_streams`), 줄마다 마침표를 떼면 `…입니다.` + `계좌에…` 가 붙을 때
    문장 경계가 사라져 두 문장짜리 문구가 도리어 안 맞게 된다.
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = s.translate(_QUOTES)
    s = _LEAD.sub("", s)
    return _WS.sub("", s)


def normalize_phrase(text: str) -> str:
    """템플릿 문구용 정규화 — `normalize()` + **끝 마침표 제거**.

    임계값을 넣기 전에 왜 안 맞는지부터 쟀더니(2026-08-24, 시연 5건) 미매칭 44건 중
    상위가 전부 마침표 하나 차이였다:

        템플릿  …개인신용평점이 하락할 수 있습니다.      (최장공통 0.97)
        광고    …개인신용평점이하락할수있습니다

    유사도로 풀 문제가 아니라 정규화 구멍이었다. 템플릿 쪽만 떼면 광고에 마침표가
    남아 있어도 부분문자열 검색은 그대로 성립한다 — 한쪽만 손대면 되는 이유다.
    **유사도 임계값은 여전히 안 쓴다** — 그 값이 이 표본에 맞춰진 과적합이 된다.
    """
    return _TRAIL.sub("", normalize(text))


# ───────────────────────────── 템플릿 판정 ─────────────────────────────

# 상품군 + 상품명노출 → 템플릿. 이 표가 곧 판정 규칙이고, 값은 사전의 키와
# 글자까지 같아야 한다(`_check_ids()` 가 기동 시 검증한다).
# 여기 없는 조합(예금성 세부유형, 카드 개인/법인, 대출모집인)은 이 표로 못 정한다 —
# **모르면 판단불가로 두고 추측하지 않는다.**
_BY_SHOWN: dict[tuple[str, str], str] = {
    ("대출성", "노출"): "대출성상품-상품명 노출",
    ("대출성", "미노출"): "대출성상품-상품명 미노출",
    ("예금성", "미노출"): "예금성상품-상품명 미노출",
    ("카드", "미노출"): "카드상품-상품명 미노출",
}

# 예금성 '노출' 은 세부유형까지 가야 정해진다. 파일명에 유형이 박힌 경우가 있어
# 그것만 신호로 쓴다 — **파일명이 없으면 판단불가다. 본문으로 추측하지 않는다.**
_DEPOSIT_SUBTYPE_HINTS: dict[str, str] = {
    "지수연동": "예금성상품-지수연동예금",
    "적립식": "예금성상품-적립식",
    "거치식": "예금성상품-거치식",
    "입출식": "예금성상품-입출식",
    "입출금": "예금성상품-입출식",
}


@dataclass
class Resolution:
    template_id: str | None
    status: str                     # 확정 | 판단불가
    basis: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "status": self.status,
            "basis": self.basis,
            "note": self.note,
        }


def resolve_template(
    product_group: str | None,
    product_name_shown: str | None,
    filename: str = "",
    pack: dict | None = None,
) -> Resolution:
    """12종 중 하나를 고른다. 근거가 모자라면 **판단불가**로 둔다.

    왜 판단불가를 남기나: 대출성만 봐도 '상품명 노출' 12항목 ↔ '미노출' 3항목이다.
    틀리게 고르면 지적 후보 9개가 통째로 사라지거나 없던 지적이 쏟아진다.
    억지로 하나 고르는 것보다 "못 정했다"고 말하는 편이 하류에 정직하다.
    """
    pack = pack or load_pack()
    known = set(pack["templates"])
    basis: list[str] = []

    if not product_group:
        return Resolution(None, "판단불가", basis, "상품군이 정해지지 않았다")
    if product_group not in {t["product_group"] for t in pack["templates"].values()}:
        return Resolution(
            None, "판단불가", basis,
            f"'{product_group}' 은 이번 범위(예금성·대출성·카드) 밖이다",
        )
    basis.append(f"product_group={product_group}")

    if not product_name_shown:
        return Resolution(None, "판단불가", basis, "상품명 노출 여부를 못 정했다")
    basis.append(f"product_name_shown={product_name_shown}")

    tid = _BY_SHOWN.get((product_group, product_name_shown))
    if tid:
        return _confirm(tid, known, basis, "상품군 + 상품명노출로 확정")

    if product_group == "예금성" and product_name_shown == "노출":
        for hint, cand in _DEPOSIT_SUBTYPE_HINTS.items():
            if hint in filename:
                basis.append(f"파일명 세부유형='{hint}'")
                return _confirm(cand, known, basis, "파일명의 세부유형으로 확정")
        return Resolution(
            None, "판단불가", basis,
            "예금성 세부유형(입출식·거치식·적립식·지수연동예금)을 못 정했다",
        )

    if product_group == "카드" and product_name_shown == "노출":
        # 개인용/법인용은 유의사항 문구가 '개인신용평점' ↔ '법인신용등급' 으로만 갈린다.
        # 본문을 봐야 아는 것이라 여기서 정하지 않는다 — 라벨링이 문구를 보고 정한다.
        return Resolution(
            None, "판단불가", basis,
            "카드 개인/법인 구분이 필요하다 (유의사항 문구로 갈린다)",
        )

    return Resolution(None, "판단불가", basis, "규칙에 없는 조합이다")


def _confirm(tid: str, known: set[str], basis: list[str], note: str) -> Resolution:
    if tid not in known:
        # 사전에서 템플릿 이름이 바뀌었는데 이 표를 안 고친 것 — 조용히 넘기면
        # 라벨이 통째로 안 붙는다. 판단불가로 떨어뜨리고 사유를 남긴다.
        return Resolution(None, "판단불가", basis, f"사전에 없는 템플릿 이름: {tid}")
    return Resolution(tid, "확정", basis, note)


def check_ids(pack: dict | None = None) -> list[str]:
    """판정 표가 가리키는 템플릿 이름이 사전에 전부 실재하는지 (기동 점검용)."""
    known = set((pack or load_pack())["templates"])
    refs = set(_BY_SHOWN.values()) | set(_DEPOSIT_SUBTYPE_HINTS.values())
    refs |= {tid for cands in _TIEBREAK.values() for tid, _ in cands}
    return sorted(refs - known)


# ─────────────────── 판단불가 뒤집기 — VLM 결선투표 ───────────────────
#
# 위 표로 못 정하는 조합은 **둘뿐**이고 둘 다 "상품군은 아는데 세부유형을 모른다" 다.
# 실측(2026-08-24, 시연 5건)에서 5건 중 2건이 여기 걸렸다:
#
#   NH농협은행-2026_002-예금성.png   파일명에 세부유형이 없다 (본문은 `NH대박7적금`)
#   13. 카드상품.jpg                 개인/법인은 유의사항 문구로만 갈린다
#
# 본문에서 '적금'·'개인신용평점' 같은 낱말을 주워 규칙을 늘릴 수도 있지만, 그건 이
# 5건에 맞춘 과적합이고 이 저장소의 규율("판단 주체는 VLM")에도 어긋난다. 세부유형
# 판별은 광고를 읽고 뜻을 아는 일이라 VLM 몫이다 — 문서당 **1회**, 그것도 위 표가
# 못 정했을 때만 부른다. VLM 도 못 정하면 그대로 판단불가로 둔다.
_TIEBREAK: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("예금성", "노출"): [
        ("예금성상품-입출식", "수시로 넣고 찾는 통장 (보통예금·자유저축예금 등)"),
        ("예금성상품-거치식", "목돈을 한 번에 맡기고 만기에 찾는 예금 (정기예금)"),
        ("예금성상품-적립식", "매월 또는 수시로 나눠 넣는 예금 (적금)"),
        ("예금성상품-지수연동예금", "주가지수 등에 연동돼 수익률이 달라지는 예금 (ELD)"),
    ],
    ("카드", "노출"): [
        ("카드상품-상품명(개인) 노출", "개인 고객용 카드 — 유의사항이 '개인신용평점' 기준"),
        ("카드상품-상품명(법인) 노출", "법인·사업자 고객용 카드 — '법인신용등급' 기준"),
        ("카드상품-장·단기카드대출", "카드론·현금서비스 등 카드대출 자체를 알리는 광고"),
    ],
}

_TIEBREAK_PROMPT = """이 광고가 아래 중 어느 유형인지 고르세요.

상품군은 **{product_group}** 으로 이미 정해져 있습니다. 세부유형만 고르면 됩니다.

[후보]
{candidates}
- {abstain}: 위 어느 것인지 판단이 서지 않는다

판단 기준
- 광고에 적힌 **상품명과 거래 방식**을 보세요 (예: 매월 납입 → 적립식).
- 낱말 하나에 걸지 말고 광고 전체가 무엇을 파는지로 판단하세요.
- **확신이 없으면 {abstain} 를 고르세요.** 틀린 유형을 고르면 항목 목록이 통째로
  달라져 없던 지적이 쏟아지거나 있어야 할 지적이 사라집니다.

광고 본문(발췌):
{excerpt}

먼저 analysis 에 근거를 한두 문장으로 적은 뒤 template_id 를 고르세요."""


def resolve_template_vlm(
    doc: dict,
    resolution: Resolution,
    pack: dict | None = None,
    canvas=None,
) -> Resolution:
    """`resolve_template()` 이 판단불가로 둔 세부유형을 VLM 에게 물어 정한다.

    표로 정해진 건(`확정`) 건드리지 않는다. 후보가 없는 판단불가(상품군 자체를 모름 등)도
    그대로 돌려준다 — **물어볼 거리가 있을 때만 부른다.**
    """
    from .gemma_client import chat_json, image_part          # 지연 import (배포 진입점 대비)

    if resolution.status == "확정":
        return resolution
    group = _basis_value(resolution, "product_group")
    shown = _basis_value(resolution, "product_name_shown")
    candidates = _TIEBREAK.get((group, shown)) if group and shown else None
    if not candidates:
        return resolution

    excerpt = _excerpt(doc)
    schema = {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "template_id": {
                "type": "string",
                "enum": [tid for tid, _ in candidates] + [_ABSTAIN],
            },
            "confidence": {"type": "number"},
        },
        "required": ["analysis", "template_id", "confidence"],
        "additionalProperties": False,
    }
    parts: list[dict] = [{"type": "text", "text": _TIEBREAK_PROMPT.format(
        product_group=group, abstain=_ABSTAIN, excerpt=excerpt,
        candidates="\n".join(f"- {tid}: {hint}" for tid, hint in candidates),
    )}]
    if canvas is not None:
        parts.append(image_part(canvas))

    try:
        data = chat_json(parts, schema_name="template_tiebreak", schema=schema, max_tokens=600)
    except Exception as exc:                                  # noqa: BLE001
        doc.setdefault("notes", []).append(f"템플릿 세부유형 판정 실패: {exc}")
        return resolution

    tid = data.get("template_id")
    if not tid or tid == _ABSTAIN:
        return Resolution(
            None, "판단불가", [*resolution.basis, "vlm=판단불가"],
            f"{resolution.note} — VLM 도 세부유형을 못 정했다",
        )
    return Resolution(
        tid, "확정",
        [*resolution.basis, f"vlm_tiebreak={tid}(conf={data.get('confidence')})"],
        "표로는 못 정해 VLM 이 세부유형을 골랐다",
    )


def _basis_value(resolution: Resolution, key: str) -> str | None:
    for b in resolution.basis:
        if b.startswith(f"{key}="):
            return b.split("=", 1)[1]
    return None


def _excerpt(doc: dict, limit: int = 1200) -> str:
    """앞쪽 줄을 순서대로 이어 붙인다 — 상품명은 대개 위쪽에 있다."""
    out: list[str] = []
    size = 0
    for page in doc.get("pages") or []:
        for region in page.get("regions") or []:
            for ln in region.get("lines") or []:
                t = (ln.get("text") or "").strip()
                if not t:
                    continue
                out.append(t)
                size += len(t)
                if size >= limit:
                    return " / ".join(out)
    return " / ".join(out)


# ───────────────────────────── 줄 모으기 ─────────────────────────────

@dataclass
class LineRef:
    """파싱 결과의 줄 하나 + 그 위치. 원문은 손대지 않고 그대로 들고 다닌다."""
    ref: str
    text: str
    norm: str
    page: int
    region_id: str | None
    bbox: list | None
    source: str | None
    confidence: float | None
    style: dict | None
    assigned: bool = False           # 라벨이 붙었나 (완전성 검사용)


def collect_lines(doc: dict) -> list[LineRef]:
    """문서의 **모든** 줄을 순서대로 모은다 — 영역 안이든 미배정이든.

    미배정 줄(`unassigned_lines`)을 빠뜨리면 '모든 텍스트 보존'이 첫 줄부터 깨진다.
    실측(`1. 예금성상품(적립식)`)에서 히어로 헤드라인 3줄이 여기 들어 있었다.
    """
    out: list[LineRef] = []
    for page in doc.get("pages") or []:
        pno = page.get("page_no")
        for region in page.get("regions") or []:
            rid = region.get("region_id")
            for i, ln in enumerate(region.get("lines") or []):
                out.append(_line_ref(f"p{pno}/{rid}/L{i:02d}", ln, pno, rid))
        for i, ln in enumerate(page.get("unassigned_lines") or []):
            out.append(_line_ref(f"p{pno}/unassigned/L{i:02d}", ln, pno, None))
    return out


def _line_ref(ref: str, ln: dict, page: int, rid: str | None) -> LineRef:
    text = ln.get("text") or ""
    return LineRef(
        ref=ref,
        text=text,
        norm=normalize(text),
        page=page,
        region_id=rid,
        bbox=ln.get("bbox"),
        source=ln.get("source"),
        confidence=ln.get("confidence"),
        style=ln.get("style"),
    )


# ───────────────────────────── 라벨링 ─────────────────────────────

def _phrase_targets(pack: dict, template_id: str) -> list[dict]:
    """템플릿의 정형 문구를 (항목명, 문구, 정규화형) 목록으로 편다."""
    pool = pack["phrase_pool"]
    out: list[dict] = []
    for item in pack["templates"][template_id]["items"]:
        for entry in item["entries"]:
            if entry["kind"] != "정형":
                continue
            text = pool[entry["phrase_id"]]["text"]
            out.append({
                "gubun": item["gubun"],
                "phrase_id": entry["phrase_id"],
                "requirement": entry["requirement"],
                "text": text,
                "norm": normalize_phrase(text),
            })
    # 긴 문구부터 맞춘다 — 짧은 문구가 긴 문구 안에 들어 있는 경우
    # (`- NH농협은행` 이 다른 문장에 포함) 짧은 쪽이 먼저 가져가면 안 된다.
    out.sort(key=lambda p: -len(p["norm"]))
    return out


@dataclass
class _Stream:
    """한 영역의 줄들을 이어 붙인 텍스트 + 글자 위치 → 줄 되짚기용 지도.

    왜 이어 붙이나. 실측(`2. 예금성상품(적립식)`)에서 유의사항이 `■` 로 구분돼
    **여러 개가 한 줄에 몰려 있고, 문구가 줄 한가운데서 다음 줄로 넘어간다**:

        ■이예금은…보호됩니다.■계좌에압류,가압류,질권설정등이등록될경
        우원금및이자지급을제한합니다.■예금잔액증명서발급당일에는…

    줄 단위로 보면 어느 문구도 온전하지 않다. 영역 단위로 이어 붙이면 한 번에 잡힌다.
    영역을 넘어서까지 잇지는 않는다 — 서로 무관한 문구가 우연히 붙어 버린다.
    """
    text: str
    spans: list[tuple[int, int, str]]        # (시작, 끝, 줄 ref)

    def refs_for(self, start: int, end: int) -> list[str]:
        return [ref for s, e, ref in self.spans if s < end and e > start]


def _streams(lines: list[LineRef]) -> list[_Stream]:
    out: list[_Stream] = []
    cur_key: object = object()
    buf, spans = "", []
    for ln in lines:
        key = (ln.page, ln.region_id)
        if key != cur_key:
            if buf:
                out.append(_Stream(buf, spans))
            cur_key, buf, spans = key, "", []
        if not ln.norm:
            continue
        spans.append((len(buf), len(buf) + len(ln.norm), ln.ref))
        buf += ln.norm
    if buf:
        out.append(_Stream(buf, spans))
    return out


def _longest_prefix(tnorm: str, streams: list[_Stream]) -> tuple[float, list[str]]:
    """문구의 앞에서부터 몇 글자까지가 광고에 **연속으로** 있는지 잰다.

    왜 필요한가. 실측(2026-08-24, 시연 5건)에서 미매칭 41건 중 상위가 전부 이 모양이었다:

        템플릿  계좌에 압류, … 원금 및 이자지급을 제한합니다     (접두어 0.91)
        광고    계좌에압류,…원금및이자지급을제한                 ← '합니다' 가 없다

    광고에 문구가 **없는** 게 아니라 **우리 파싱이 끝까지 못 읽은** 것이다. 둘을 뭉치면
    우리 결함이 '미표시' 지적으로 둔갑한다 — 이 저장소가 이미 (B)미표시 ↔ (C)확인필요 를
    가르는 이유와 같은 문제다.

    **커버리지 값을 그대로 내보내고 여기서 자르지 않는다.** 0.9 면 잘림, 0.2 면 남남 …
    이라고 우리가 선을 그으면 그 선이 이 표본에 맞춰진 임계값이 된다. 하류가 정한다.
    """
    if not tnorm:
        return 0.0, []
    lo, hi, best, refs = 1, len(tnorm), 0, []
    while lo <= hi:                                   # 이분 탐색 — 접두어는 단조적이다
        mid = (lo + hi) // 2
        found: list[str] | None = None
        for st in streams:
            pos = st.text.find(tnorm[:mid])
            if pos != -1:
                found = st.refs_for(pos, pos + mid)
                break
        if found is not None:
            best, refs, lo = mid, found, mid + 1
        else:
            hi = mid - 1
    return round(best / len(tnorm), 3), refs


def _match_phrase(target: dict, streams: list[_Stream]) -> list[dict]:
    """문구를 영역 텍스트 안에서 찾는다. **임계값 없음 — 완전포함만 인정한다.**

    한 영역에 같은 문구가 두 번 나올 수도 있어 전부 찾는다(중복 표기 관측).
    걸친 줄 수로 어떻게 잡혔는지 구분한다:
      1줄 → 그 줄 안에 통째로 있다   ·   2줄 이상 → 줄 경계를 가로질렀다
    """
    tnorm = target["norm"]
    hits: list[dict] = []
    if not tnorm:
        return hits
    for st in streams:
        start = st.text.find(tnorm)
        while start != -1:
            refs = st.refs_for(start, start + len(tnorm))
            hits.append({
                "how": "한줄내" if len(refs) == 1 else "줄경계_가로지름",
                "refs": refs,
            })
            start = st.text.find(tnorm, start + 1)
    return hits


# ───────────────────────── VLM 영역 라벨링 (2층) ─────────────────────────
#
# 왜 문자열 대조만으로 안 되나 — 실측(2026-08-24, 시연 5건)이 답이다.
# 정형 문구 61개 중 **완전일치는 20개(33%)뿐**이었고, 나머지 상당수가 "뜻은 같은데
# 표기가 다른" 경우였다:
#
#     템플릿  …제19조 제1항에 **따라 충분한 설명을** 받을 수 있는 권리가 있습니다
#     광고    …제19조제1항에 **따른 설명을** 받을 수 있는 권리가 있습니다
#     템플릿  만기 전 해지할 경우 **약정한 이율**보다 낮은 **중도해지이율**이 …
#     광고    만기 전 해지할 경우 **계약한 이자율**보다 낮은 **중도해지이자율**이 …
#
# 동의어·어미 사전을 늘려 잡으려 하면 그 사전이 이 표본에 맞춰진 과적합이 된다.
# 이 저장소의 규율("판단 주체는 VLM, 정규식은 폴백·보조 신호로만")대로 의미 판정은
# VLM 에 맡기고, 문자열 완전일치는 **흔들리지 않는 앵커**로 남겨 교차검증에 쓴다.
#
# 구조는 `vlm_judge.judge_region_roles()` 와 같다 — 페이지당 1회 호출, 영역마다 라벨
# 하나. 다른 것은 어휘뿐이다: 범용 역할 9종이 아니라 **그 문서 템플릿의 구분 목록**.

_ABSTAIN = "해당없음"


def _gubun_schema(gubuns: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            # analysis 를 선두에 — 배열이 앞에 오면 빈 배열로 조기 종료하는 퇴행이 있다.
            "analysis": {"type": "string"},
            "regions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string"},
                        # 그 영역 안에서 이 항목이 차지하는 줄 범위(0-based, 포함).
                        # 한 영역이 통째로 한 항목이면 line_from=0, line_to=마지막줄.
                        "line_from": {"type": "integer"},
                        "line_to": {"type": "integer"},
                        "gubun": {"type": "string", "enum": [*gubuns, _ABSTAIN]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["region_id", "line_from", "line_to", "gubun", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["analysis", "regions"],
        "additionalProperties": False,
    }


_GUBUN_PROMPT = """당신은 금융상품 광고심의 보조 시스템의 항목 분류기입니다.
이 광고는 **{template_id}** 유형입니다. 아래 영역들이 각각 어느 항목에 해당하는지
판정하세요.

[항목 정의]
{gubun_defs}
- {abstain}: 위 어느 항목에도 해당하지 않는 문구(홍보 문구, 장식, 이벤트 안내 등)

판정 기준
- **뜻으로 판단하세요.** 광고는 표준 문구를 글자 그대로 쓰지 않습니다.
  예: "약정한 이율" 과 "계약한 이자율" 은 같은 것이고, "…에 따라 충분한 설명을"과
  "…에 따른 설명을" 도 같은 것입니다. 표기가 달라도 내용이 그 항목이면 그 항목입니다.
- 특정 낱말이 있는지가 아니라 **그 문구가 무슨 역할을 하는지**를 보세요.
- 판단이 서지 않으면 {abstain} 을 쓰세요. **억지로 끼워 맞추지 마세요** —
  틀린 라벨은 없는 라벨보다 나쁩니다.
- y_ratio 는 화면에서의 세로 위치입니다(0=최상단, 1=최하단). 참고만 하세요.
- 첨부 이미지는 전체 화면 축소본입니다.

**한 영역 안에 항목이 여러 개 섞여 있을 수 있습니다.** 각 영역의 줄은
`[00] 텍스트` `[01] 텍스트` 처럼 번호가 붙어 있습니다. 그 영역 전체가 한 항목이면
`line_from=0, line_to=마지막 줄번호` 로 한 번만 답하세요. 항목이 줄 경계에서
바뀌면(예: 0~2번 줄은 대출금리, 3~4번 줄은 대출기간) **같은 region_id 를 항목
수만큼 여러 번** 반환하고 각각 다른 줄 범위를 쓰세요. 범위는 겹치지 않아야 하고
빠지는 줄이 없어야 합니다(0번줄부터 마지막 줄까지 전부 어느 판정엔가 속해야 함).

영역 목록:
{regions}

먼저 analysis 에 이 광고가 어떤 내용인지 한두 문장으로 적은 뒤,
**모든 region_id 의 모든 줄이 하나 이상의 판정에 포함되도록** 반환하세요."""


def _gubun_definitions(pack: dict, template_id: str) -> str:
    """항목 정의문을 사전에서 만든다 — 예시문구 한 줄을 붙여 뜻을 잡아 준다.

    정의를 손으로 쓰지 않는 이유: 템플릿이 갱신되면 정의도 같이 따라가야 한다.
    """
    pool = pack["phrase_pool"]
    out: list[str] = []
    for item in pack["templates"][template_id]["items"]:
        sample = ""
        for e in item["entries"]:
            text = pool[e["phrase_id"]]["text"] if e["kind"] == "정형" else e["example"]
            text = _WS.sub(" ", text.lstrip("- ").strip())
            if text:
                sample = text[:60]
                break
        out.append(f"- {item['gubun']}: 예) {sample}" if sample else f"- {item['gubun']}")
    return "\n".join(out)


# 한 번에 물어보는 영역 수. 쪽당 1회로 하다가 실측(2026-08-24)에서 깨졌다:
# 영역 50개짜리 광고에서 `analysis` 는 멀쩡히 나오는데 `regions` 가 **빈 배열**로
# 돌아왔다(같은 문서가 44개일 때는 36개를 정상 판정했다). 이 저장소가 이미 겪은
# '배열 선두 스키마 퇴행'의 변종이고, 스키마 순서로는 안 막힌다 — 목록이 길면 진다.
# 그래서 나눠 묻는다. 나누면 한 번에 지는 대신 그 조각만 진다.
_GUBUN_CHUNK = 12


def label_regions_vlm(
    doc: dict, template_id: str, pack: dict | None = None, canvas_for_page=None,
) -> dict[str, dict]:
    """영역마다 템플릿 항목명을 붙인다. 영역 12개마다 VLM 1회.

    반환: `region_id` → `{"gubun", "confidence"}`. `해당없음` 은 담지 않는다.
    실패한 조각은 건너뛰되 **반드시 노트를 남긴다** — 조용히 넘기면 "VLM 이 판단을
    보류했다"와 "호출이 깨졌다"가 결과에서 똑같이 라벨 0으로 보인다(실측으로 당했다).
    텍스트 자체는 라벨과 무관하게 보존되므로 파이프라인을 멈추지는 않는다.
    """
    pack = pack or load_pack()
    gubuns = [i["gubun"] for i in pack["templates"][template_id]["items"]]
    schema = _gubun_schema(gubuns)
    defs = _gubun_definitions(pack, template_id)

    verdicts: dict[str, dict] = {}
    for page in doc.get("pages") or []:
        regions = [r for r in (page.get("regions") or []) if r.get("lines")]
        if not regions:
            continue
        canvas = canvas_for_page(page) if canvas_for_page else None
        missing: list[str] = []
        for i in range(0, len(regions), _GUBUN_CHUNK):
            chunk = regions[i:i + _GUBUN_CHUNK]
            got = _label_chunk(chunk, page, template_id, defs, schema, canvas, doc)
            verdicts.update(got["verdicts"])
            missing.extend(got["missing"])
        if missing:
            # 답이 안 온 영역을 값으로 남긴다. 이게 없으면 '해당없음' 과 구분이 안 된다.
            doc.setdefault("notes", []).append(
                f"템플릿 항목 라벨링: p{page.get('page_no')} 영역 {len(missing)}개 무응답 "
                f"({', '.join(missing[:8])}{'…' if len(missing) > 8 else ''})"
            )
    return verdicts


def _label_chunk(chunk, page, template_id, defs, schema, canvas, doc) -> dict:
    """영역 한 묶음을 판정한다. 돌려주지 않은 region_id 는 `missing` 으로 올린다."""
    from .gemma_client import chat_json, image_part          # 지연 import (배포 진입점 대비)

    canvas_h = page.get("canvas_h") or 0
    listing = []
    for r in chunk:
        bbox = r.get("bbox") or []
        y = round(bbox[1] / canvas_h, 2) if (bbox and canvas_h) else "?"
        # 이전에는 앞 4줄만 발췌했다 — 영역이 여러 항목을 담고 있으면(표 행이 뭉친 경우
        # 등) 뒤쪽 줄이 안 보여 전부 첫 항목으로 오판됐다(2026-08-26 실측, 25번 문서).
        # 전체 줄을 번호와 함께 준다. 길이는 상한을 두어 프롬프트 폭주를 막는다.
        numbered = " ".join(
            f"[{i:02d}]{(l.get('text') or '')[:40]}" for i, l in enumerate(r["lines"][:40])
        )[:600]
        listing.append(f'- region_id={r["region_id"]} y_ratio={y} 줄들: {numbered}')

    parts: list[dict] = [{"type": "text", "text": _GUBUN_PROMPT.format(
        template_id=template_id, gubun_defs=defs, abstain=_ABSTAIN,
        regions="\n".join(listing),
    )}]
    if canvas is not None:
        parts.append(image_part(canvas))

    asked = [r["region_id"] for r in chunk]
    try:
        data = chat_json(
            parts, schema_name="template_gubun", schema=schema,
            max_tokens=max(1200, 60 * len(chunk) + 300),
        )
    except Exception as exc:                                 # noqa: BLE001
        doc.setdefault("notes", []).append(
            f"템플릿 항목 라벨링 실패(p{page.get('page_no')}): {exc}"
        )
        return {"verdicts": {}, "missing": asked}

    # region_id → 판정 목록. 한 영역에 항목이 여러 개 섞였으면 여기 여러 개가 쌓인다
    # (예전엔 dict[str, dict] 라 영역당 값이 하나뿐이었다 — 뒤에 온 판정이 앞을 덮었다).
    verdicts: dict[str, list[dict]] = {}
    answered: set[str] = set()
    for v in data.get("regions") or []:
        rid = v.get("region_id")
        if rid not in asked:
            continue        # 안 물어본 영역을 지어낸 경우 — 버린다
        answered.add(rid)
        if v.get("gubun") and v["gubun"] != _ABSTAIN:
            verdicts.setdefault(rid, []).append({
                "gubun": v["gubun"],
                "confidence": v.get("confidence"),
                "line_from": v.get("line_from", 0),
                "line_to": v.get("line_to", 0),
            })
    return {"verdicts": verdicts, "missing": [r for r in asked if r not in answered]}


# VLM은 문장의 뜻을 읽는 주체지만, ``가입기간:``처럼 스스로 항목명을 밝힌 줄까지
# 한 줄 밀려 다른 항목으로 가리키는 경우가 있었다. 이는 재투표할 문제가 아니다. 아래
# 표는 그러한 *명시 표제*만 다루는 안전장치이며, 일반적인 의미 판정에는 관여하지 않는다.
_EXPLICIT_LINE_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "가입대상": ("가입대상", "가입자격"),
    "가입금액": ("가입금액", "납입금액", "월납입금액"),
    "가입기간": ("가입기간", "예치기간"),
    "금리": ("금리", "기본금리", "기본이자율", "적용금리", "이자율"),
    "우대금리": ("우대금리", "우대이자율"),
    "예상수취이자": ("예상수취이자", "예상이자", "만기시예상수취이자금액"),
    "중도해지이율": ("중도해지이율", "중도해지이자율"),
    "만기후이율": ("만기후이율", "만기후이자율"),
    "이자지급시기": ("이자지급시기", "이자지급방법"),
    "이자지급제한": ("이자지급제한",),
    "예금자보호": ("예금자보호",),
    "유의사항": ("유의사항", "상품유의사항", "이벤트유의사항"),
    "심의번호": ("심의번호", "준법감시인심의필", "심의필"),
    "대출대상": ("대출대상", "대출자격"),
    "대출한도": ("대출한도",),
    "대출기간": ("대출기간",),
    "상환방법": ("상환방법",),
    "대출금리": ("대출금리", "기준금리"),
    "부대비용": ("부대비용",),
    "채권보전": ("채권보전",),
}

# 현재 제공된 템플릿에는 없지만, 위 항목들과 같은 줄에 섞여 VLM 범위가 한 칸씩 밀리는
# 대표 표제다. 이 줄은 임의의 템플릿 항목으로 승격하지 않고 미배정으로 남긴다.
_NON_TEMPLATE_LINE_PREFIXES = ("가입방법", "판매한도")


def _explicit_line_directive(text: str, available_gubuns: set[str]) -> str | None:
    """명시 표제가 지시하는 템플릿 항목(또는 미배정)을 돌려준다.

    ``None``은 표제가 없다는 뜻으로 VLM 판정을 그대로 존중한다. 따라서 이 함수는
    '뜻이 비슷한 문장'이나 표의 숫자 행을 규칙으로 재분류하지 않는다.
    """
    value = normalize(text)
    matches = [
        gubun
        for gubun, aliases in _EXPLICIT_LINE_LABEL_ALIASES.items()
        if gubun in available_gubuns and any(value.startswith(alias) for alias in aliases)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches and any(value.startswith(prefix) for prefix in _NON_TEMPLATE_LINE_PREFIXES):
        return _ABSTAIN
    return None


def _remove_verdict_line(verdicts: list[dict], line_no: int, *, except_gubun: str | None = None) -> list[dict]:
    """한 줄만 범위에서 빼되, 나머지 VLM 판정 범위는 보존한다."""
    trimmed: list[dict] = []
    for verdict in verdicts:
        if verdict.get("gubun") == except_gubun:
            trimmed.append(dict(verdict))
            continue
        lo, hi = int(verdict.get("line_from", 0)), int(verdict.get("line_to", 0))
        if hi < lo:
            lo, hi = hi, lo
        if line_no < lo or line_no > hi:
            trimmed.append(dict(verdict))
        elif lo < line_no:
            trimmed.append({**verdict, "line_from": lo, "line_to": line_no - 1})
        if line_no < hi:
            trimmed.append({**verdict, "line_from": line_no + 1, "line_to": hi})
    return trimmed


def _has_verdict_for_line(verdicts: list[dict], gubun: str, line_no: int) -> bool:
    return any(
        verdict.get("gubun") == gubun
        and min(int(verdict.get("line_from", 0)), int(verdict.get("line_to", 0))) <= line_no
        <= max(int(verdict.get("line_from", 0)), int(verdict.get("line_to", 0)))
        for verdict in verdicts
    )


def _dedupe_verdicts(verdicts: list[dict]) -> list[dict]:
    """모델이 같은 범위를 반복 반환해도 P1 의미 범위는 한 번만 남긴다."""
    unique: list[dict] = []
    seen: set[tuple] = set()
    for verdict in verdicts:
        key = (
            verdict.get("gubun"),
            int(verdict.get("line_from", 0)),
            int(verdict.get("line_to", 0)),
        )
        if key not in seen:
            seen.add(key)
            unique.append(verdict)
    return unique


def _is_review_number_date_continuation(text: str) -> bool:
    """심의필 바로 다음 줄에 단독 표기된 유효기간은 심의번호 근거로 함께 남긴다."""
    return bool(re.search(r"\d{4}.*~", normalize(text)))


def _remove_gubun_line(verdicts: list[dict], gubun: str, line_no: int) -> list[dict]:
    """다른 항목의 겹침은 건드리지 않고 지정 라벨에서만 한 줄을 뺀다."""
    retained: list[dict] = []
    for verdict in verdicts:
        if verdict.get("gubun") != gubun:
            retained.append(dict(verdict))
            continue
        lo, hi = int(verdict.get("line_from", 0)), int(verdict.get("line_to", 0))
        if hi < lo:
            lo, hi = hi, lo
        if line_no < lo or line_no > hi:
            retained.append(dict(verdict))
        elif lo < line_no:
            retained.append({**verdict, "line_from": lo, "line_to": line_no - 1})
        if line_no < hi:
            retained.append({**verdict, "line_from": line_no + 1, "line_to": hi})
    return retained


def _guard_explicit_line_labels(doc: dict, template: dict, vlm_labels: dict[str, list[dict]] | None) -> dict[str, list[dict]]:
    """명시 표제와 충돌하는 VLM 줄 범위만 국소적으로 바로잡는다.

    한 번의 VLM 응답을 다시 투표하거나 재시도하지 않는다. 예를 들어 ``가입방법``을
    ``가입기간``으로 준 경우에는 전자를 미배정으로, ``가입기간:12개월``은 가입기간으로
    고정한다. 수정 내역은 P1 ``notes``에 남긴다.
    """
    guarded = {rid: [dict(verdict) for verdict in verdicts] for rid, verdicts in (vlm_labels or {}).items()}
    available = {item["gubun"] for item in template["items"]}
    corrections: list[str] = []

    for page in doc.get("pages") or []:
        for region in page.get("regions") or []:
            rid = region.get("region_id")
            if not rid:
                continue
            verdicts = guarded.get(rid, [])
            directives: dict[int, str] = {}
            for line_no, line in enumerate(region.get("lines") or []):
                directive = _explicit_line_directive(str(line.get("text") or ""), available)
                if directive is None:
                    continue
                directives[line_no] = directive
                before = [(v.get("gubun"), v.get("line_from"), v.get("line_to")) for v in verdicts]
                if directive == _ABSTAIN:
                    verdicts = _remove_verdict_line(verdicts, line_no)
                else:
                    verdicts = _remove_verdict_line(verdicts, line_no, except_gubun=directive)
                    if not _has_verdict_for_line(verdicts, directive, line_no):
                        verdicts.append({
                            "gubun": directive,
                            "confidence": 1.0,
                            "line_from": line_no,
                            "line_to": line_no,
                        })
                after = [(v.get("gubun"), v.get("line_from"), v.get("line_to")) for v in verdicts]
                if after != before:
                    corrections.append(f"{rid}/L{line_no:02d}→{directive}")

            # `준법감시인 심의필`처럼 라벨을 직접 밝힌 줄이 같은 영역에 있으면, 전화번호
            # 같은 이웃 줄을 심의번호로 함께 잡을 근거가 없다. 단, 바로 뒤 유효기간 줄은
            # 심의필의 일부이므로 남긴다.
            review_number_lines = {
                line_no for line_no, directive in directives.items() if directive == "심의번호"
            }
            if review_number_lines:
                for line_no, line in enumerate(region.get("lines") or []):
                    if line_no in review_number_lines or _is_review_number_date_continuation(str(line.get("text") or "")):
                        continue
                    before = [(v.get("gubun"), v.get("line_from"), v.get("line_to")) for v in verdicts]
                    verdicts = _remove_gubun_line(verdicts, "심의번호", line_no)
                    after = [(v.get("gubun"), v.get("line_from"), v.get("line_to")) for v in verdicts]
                    if after != before:
                        corrections.append(f"{rid}/L{line_no:02d}→심의번호 제외")
            verdicts = _dedupe_verdicts(verdicts)
            if verdicts:
                guarded[rid] = verdicts
            else:
                guarded.pop(rid, None)

    if corrections:
        doc.setdefault("notes", []).append(
            "템플릿 라벨 명시표제 안전장치 적용: " + ", ".join(corrections[:12])
            + ("…" if len(corrections) > 12 else "")
        )
    return guarded


def label_document(
    doc: dict,
    template_id: str | None,
    pack: dict | None = None,
    vlm_labels: dict[str, list[dict]] | None = None,
    *,
    apply_explicit_line_guard: bool = False,
) -> dict:
    """파싱 결과에 템플릿 라벨을 붙이고 **모든 줄을 실어** 돌려준다.

    `template_id` 가 None(판단불가)이면 라벨 없이 전 줄을 `other_content` 로 낸다 —
    판정을 못 했다고 텍스트를 버리지는 않는다.

    `vlm_labels` 는 `label_regions_vlm()` 결과(2층) — `region_id` → 판정 목록
    (`{gubun, confidence, line_from, line_to}`). 한 영역에 항목이 여러 개 섞였으면
    목록에 여러 개가 온다(2026-08-26 이전에는 영역당 값 1개였다 — VLM 이 앞 4줄만
    보고 영역 전체를 그 항목으로 오태깅했다. 25번·3.예금성 문서 실측). 없으면
    1층만으로 돈다 — VLM 없이도 결정론적으로 재현되는 부분이 그대로 나온다.
    실파이프라인은 ``apply_explicit_line_guard=True``로 명시 표제 안전장치를 켠다.
    직접 주입하는 테스트/이관 호출은 원래 판정을 보존하도록 기본값을 끈다.
    """
    pack = pack or load_pack()
    lines = collect_lines(doc)
    by_ref = {ln.ref: ln for ln in lines}

    items_out: list[dict] = []
    if template_id:
        tpl = pack["templates"][template_id]
        if apply_explicit_line_guard:
            vlm_labels = _guard_explicit_line_labels(doc, tpl, vlm_labels)
        targets = _phrase_targets(pack, template_id)
        streams = _streams(lines)
        found_by_gubun: dict[str, list[dict]] = {}

        for t in targets:
            hits = _match_phrase(t, streams)
            for h in hits:
                for r in h["refs"]:
                    by_ref[r].assigned = True
            rec = {
                "phrase_id": t["phrase_id"],
                "requirement": t["requirement"],
                "found": bool(hits),
                "matches": hits,
            }
            if not hits:
                # 완전일치가 없을 때만 잰다. "없다"와 "우리가 못 읽었다"를 가를 재료로
                # 커버리지와 걸친 줄을 남긴다 — 판정은 하지 않는다.
                cov, refs = _longest_prefix(t["norm"], streams)
                rec["prefix_coverage"] = cov
                rec["prefix_refs"] = refs
            found_by_gubun.setdefault(t["gubun"], []).append(rec)

        # VLM 이 붙인 줄 단위 판정을 항목명 → line_ref 목록으로 뒤집는다. line_from/
        # line_to 는 그 영역의 lines 배열 안 인덱스이므로 L{i:02d} 로 그대로 바뀐다
        # (label_regions_vlm 이 준 인덱스와 collect_lines 의 인덱스는 같은 순회 순서).
        vlm_refs_by_gubun: dict[str, list[str]] = {}
        for rid, verdicts in (vlm_labels or {}).items():
            pno = rid.split("_", 1)[0]                       # "p1_r011" → "p1"
            for v in verdicts:
                lo, hi = v.get("line_from", 0), v.get("line_to", 0)
                if hi < lo:
                    lo, hi = hi, lo
                for i in range(lo, hi + 1):
                    ref = f"{pno}/{rid}/L{i:02d}"
                    if ref in by_ref:
                        vlm_refs_by_gubun.setdefault(v["gubun"], []).append(ref)

        for item in tpl["items"]:
            g = item["gubun"]
            fixed = found_by_gubun.get(g, [])
            variables = [e for e in item["entries"] if e["kind"] == "변수"]
            var_refs = sorted(set(vlm_refs_by_gubun.get(g, [])))
            items_out.append({
                "gubun": g,
                "requirement": item["requirement"],
                "match_mode": item["match_mode"],
                # 농협 원본 템플릿의 기재요령. 파서는 위반 여부를 내리지 않지만, 이후
                # 심의 단계가 어떤 항목을 어떤 기준으로 확인해야 하는지 알 수 있게 보존한다.
                "writing_rules": item.get("writing_rules") or [],
                "fixed_phrases": fixed,
                # 변수형은 값을 아직 안 뽑는다(그 결정은 그대로다) — 다만 VLM 이 "이
                # 줄들이 그 항목이다"라고 판정한 위치는 이제 여기 line_refs 로 남는다.
                # status 는 "찾았다"가 아니라 "값 추출은 미착수"라는 뜻 그대로다.
                "variable_entries": [
                    {"requirement": e["requirement"], "status": "미착수"} for e in variables
                ],
                "line_refs": var_refs,
            })

    # ── 2층: VLM 이 붙인 영역 라벨을 얹는다 ────────────────────────────────
    #
    # ⚠️ 두 층은 **서로 다른 질문에 답한다.** 한동안 이걸 "일치/불일치"로 비교했는데
    # 잘못된 틀이었다(2026-08-24 실측에서 드러남):
    #
    #   1층 "이 영역 안에 `NH농협은행` 이라는 문자열이 있나"        → 있다 (참)
    #   2층 "이 영역은 무슨 항목인가"                              → 심의번호 (참)
    #
    # `회사명` 문구는 `NH농협은행` 6글자라 다른 문장 안에도 그냥 들어 있다
    # ("…NH농협은행 준법감시인 심의필…"). 둘 다 참인데 비교하면 없는 충돌이 생긴다.
    # 그래서 **사실을 나란히 싣고 우열을 가리지 않는다.**
    #
    # 대신 판단 재료를 하나 준다 — `phrase_share`: 그 영역 글자 중 몇 %가 템플릿 문구로
    # 설명되는가. 0.9 면 영역이 곧 그 문구이고, 0.05 면 스쳐 지나간 것이다. 값만 준다.
    region_text: dict[str, int] = {}
    for ln in lines:
        if ln.region_id:
            region_text[ln.region_id] = region_text.get(ln.region_id, 0) + len(ln.norm)

    hits_by_region: dict[str, dict[str, dict]] = {}
    if template_id:
        # 문구 자체의 길이로 재야 한다. 매칭된 **줄** 길이로 재면 `회사명`(6글자)이
        # 40글자 줄에 걸렸을 때 share 가 1.0 이 나와, 영역 전체가 회사명인 것처럼 보인다.
        phrase_len = {t["phrase_id"]: len(t["norm"]) for t in targets}
        for item in items_out:
            for f in item["fixed_phrases"]:
                for m in f["matches"]:
                    for r in m["refs"]:
                        rid = by_ref[r].region_id
                        if not rid:
                            continue
                        slot = hits_by_region.setdefault(rid, {})
                        slot[f["phrase_id"]] = {
                            "phrase_id": f["phrase_id"],
                            "gubun": item["gubun"],
                            "chars": phrase_len.get(f["phrase_id"], 0),
                        }

    regions_out: list[dict] = []
    for rid in sorted(set(hits_by_region) | set(vlm_labels or {})):
        hits = sorted(hits_by_region.get(rid, {}).values(), key=lambda h: -h["chars"])
        total = region_text.get(rid) or 0
        covered = max((h["chars"] for h in hits), default=0)
        verdicts = (vlm_labels or {}).get(rid) or []
        # region_labels[].gubun 은 한 값만 담을 수 있는 기존 필드다(_parsed.json 의
        # regions[].gubun 이 이 값을 그대로 받는다) — 대표값으로 "가장 넓은 줄 범위를
        # 차지한 판정"을 쓴다. 영역이 실제로는 항목 여러 개로 쪼개졌으면 그 사실을
        # gubun_breakdown 에 전부 남긴다 — 대표값 하나로 접으면서 나머지 항목이
        # 조용히 사라지는 걸 막는다(25번 문서: 영역 하나에 항목 3개가 있었다).
        best = max(verdicts, key=lambda v: v.get("line_to", 0) - v.get("line_from", 0),
                   default=None)
        regions_out.append({
            "region_id": rid,
            # 2층 판정(대표값). 없으면 None — 1층 문구만 있는 영역이다.
            "gubun": best["gubun"] if best else None,
            "confidence": best.get("confidence") if best else None,
            "source": "vlm" if best else None,
            # 이 영역 안에 실제로 섞여 있던 판정 전부(줄 범위 포함). 대표값과 같은
            # 항목 하나뿐이면 원소 1개 — 정보가 늘지도 줄지도 않는다.
            "gubun_breakdown": [
                {"gubun": v["gubun"], "confidence": v.get("confidence"),
                 "line_from": v.get("line_from"), "line_to": v.get("line_to")}
                for v in sorted(verdicts, key=lambda v: v.get("line_from", 0))
            ],
            # 1층 사실. "이 문구가 여기 있다" 이상을 주장하지 않는다.
            "phrase_hits": hits,
            "phrase_share": round(covered / total, 3) if total else 0.0,
        })

    gubun_by_region = {r["region_id"]: r["gubun"] for r in regions_out}
    other = [
        {
            "line_ref": ln.ref, "text": ln.text, "page": ln.page,
            "region_id": ln.region_id, "bbox": ln.bbox,
            "source": ln.source, "confidence": ln.confidence, "style": ln.style,
            # 줄 자체는 문구 매칭이 안 됐어도 영역 라벨이 있으면 소속을 알 수 있다.
            "region_gubun": gubun_by_region.get(ln.region_id),
        }
        for ln in lines if not ln.assigned
    ]

    labeled = sum(1 for ln in lines if ln.assigned)
    return {
        "template_id": template_id,
        "items": items_out,
        "region_labels": regions_out,
        "other_content": other,
        "completeness": {
            "lines_total": len(lines),
            "labeled": labeled,
            "other": len(other),
            # 라벨링이 텍스트를 삼켰는지 보는 불변식. 0 이 아니면 그 자체가 결함이다.
            "unaccounted": len(lines) - labeled - len(other),
            # 라벨이 닿은 줄 — 문구로 직접 잡혔거나(1층) 소속 영역에 라벨이 있거나(2층).
            "lines_covered": labeled + sum(1 for o in other if o["region_gubun"]),
        },
    }
