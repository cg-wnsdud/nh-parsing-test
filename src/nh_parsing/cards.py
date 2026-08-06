"""카드-우선 분할 (§D): SNS 카드·예시 뭉치가 한 이미지에 있을 때, region 들을 카드
단위로 묶는다.

**역할 분담(2026-07-28 실측으로 조정)** — 개수는 코드, 배정은 VLM.

- 개수·경계: 픽셀 글자밀도(bands.count_cards_by_density)가 003 3페이지의 3/2/1 을
  모델 호출 0회로 정확히 맞혔다. 예전에 기하 분할을 배제한 근거였던 "어느 빈틈이 경계인지
  못 맞힌다"(003 p2 최대 빈틈 566px 이 경계가 아님)는 사실이지만, 빈틈의 '넓이'가 아니라
  **양쪽 덩어리의 글자량**으로 판단하면 갈린다(진짜 카드 28~59% vs 아닌 것 0~4.4%).
- 배정("이 영역이 몇 번 카드냐"): 여전히 VLM 몫이다. 전폭 헤드라인처럼 어느 컬럼에도
  안 속하는 요소는 좌표로 못 정한다(같은 실측에서 p1 헤드라인이 어긋남).
- VLM 좌표는 환각이 잦아 안 쓰고, y_ratio/x_ratio/텍스트만 근거로 준다.

개수가 결정론적으로 정해지므로 투표를 매번 3회 돌리지 않는다 — 1회 관측 후 개수가
밀도와 어긋날 때만 나머지를 돌린다(관측 1회 + 로직 검산 + 조건부 재호출).
"""

from __future__ import annotations

from PIL import Image

from .config import SETTINGS
from .gemma_client import chat_json, image_part
from .ir import AdPage

_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "page_kind": {"type": "string", "enum": ["single_scroll", "card_collage"]},
        "card_count": {"type": "integer"},
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "region_id": {"type": "string"},
                    "card_no": {"type": "integer"},
                },
                "required": ["region_id", "card_no"],
            },
        },
    },
    "required": ["analysis", "page_kind", "card_count", "assignments"],
}

_CARD_PROMPT = """이 광고 화면이 여러 개의 '카드/패널'(SNS 이벤트 카드·사례 등 별도 배경·\
테두리·묶음으로 분리된 독립 콘텐츠 영역)로 이루어져 있는지 보고, 아래 영역들을 카드별로 묶어라.

규칙:
1. 먼저 analysis 에 화면 구성을 서술하고, 카드가 몇 개인지(card_count) 정하라.
2. 세로로 길게 이어지는 단일 스크롤형 페이지면 page_kind=single_scroll, card_count=1,
   모든 영역 card_no=1.
3. 카드형이면 page_kind=card_collage. 카드에 위→아래, 좌→우 순서로 1,2,3… 번호를 매기고,
   각 region 을 그 영역이 속한 카드 번호(card_no)로 배정하라.
4. 어느 한 카드에 속하지 않고 카드들 위/전체에 걸친 공통 요소(상단 배너·브랜드 헤더·
   'SNS 카드 설명' 같은 안내문)는 card_no=0 으로 두라.
5. 좌표를 지어내지 말고, 아래 y_ratio(세로위치)·x_ratio(가로위치)·텍스트로 판단하라.
{hint}
영역 목록:
{regions}"""

# 밀도 관측을 근거로 붙이는 자리. '정답'이 아니라 '증거'로 준다 — 최종 판단은 VLM 이 한다.
_DENSITY_HINT = """
참고(픽셀 글자밀도 분석 결과):
  이 화면은 세로 여백으로 갈라진 덩어리가 {count}개로 보이며, 각 덩어리의 가로 범위는
  {ranges} 입니다 (x_ratio 기준).
  각 영역의 x_ratio 를 이 범위와 맞춰 보면 어느 카드에 속하는지 정할 수 있습니다.
  다만 이건 글자가 몰린 자리를 센 것이라 틀릴 수 있습니다 — **화면을 보고 다르다고
  판단되면 다른 개수로 답하세요.** 근거는 analysis 에 적으세요.
"""


def _assign_once(
    page: AdPage,
    judgeable: list,
    canvas: Image.Image | None,
    hint: str = "",
) -> dict[str, int] | None:
    """카드 배정 1회 관측. 스크롤/실패면 None, 카드형이면 {region_id: card_no}(0=공통)."""
    listing = []
    for r in judgeable:
        yr = round(r.bbox[1] / page.canvas_h, 2) if page.canvas_h else "?"
        xr = round(((r.bbox[0] + r.bbox[2]) / 2) / page.canvas_w, 2) if page.canvas_w else "?"
        excerpt = " / ".join(l.text for l in r.lines[:3])[:120]
        listing.append(f'- region_id={r.region_id} y_ratio={yr} x_ratio={xr} 텍스트: "{excerpt}"')
    parts: list[dict] = [
        {"type": "text", "text": _CARD_PROMPT.format(regions="\n".join(listing), hint=hint)}
    ]
    if canvas is not None:
        parts.append(image_part(canvas, box=(1024, 1024)))
    try:
        data = chat_json(parts, schema_name="card_assign", schema=_CARD_SCHEMA,
                         max_tokens=max(1200, 30 * len(judgeable) + 400))
    except Exception:
        return None
    if data.get("page_kind") == "single_scroll" or int(data.get("card_count") or 1) <= 1:
        return None  # 스크롤 — 카드 없음
    out: dict[str, int] = {}
    for a in data.get("assignments", []):
        rid = a.get("region_id")
        if rid and isinstance(a.get("card_no"), int) and a["card_no"] >= 0:
            out[rid] = a["card_no"]
    return out


def _has_separable_clusters(page: AdPage, min_gap_frac: float = 0.05, wide_frac: float = 0.6) -> bool:
    """캔버스 이미지가 없을 때 쓰는 폴백 게이트 — region bbox 만으로 덩어리 분리를 본다.

    캔버스가 있으면 픽셀 밀도(count_cards_by_density)가 더 정확하다. 이 함수는 HWP
    디지털 페이지처럼 이미지가 없는 경우를 위해 남긴다.
    """
    boxes = [r.bbox for r in page.regions if r.bbox]
    if len(boxes) < 3:
        return False
    for axis, span in ((0, page.canvas_w), (1, page.canvas_h)):
        if not span:
            continue
        narrow = [b for b in boxes if (b[axis + 2] - b[axis]) < wide_frac * span]
        merged: list[list[int]] = []
        for a, c in sorted((b[axis], b[axis + 2]) for b in narrow):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], c)
            else:
                merged.append([a, c])
        for i in range(1, len(merged)):
            if merged[i][0] - merged[i - 1][1] >= min_gap_frac * span:
                return True
    return False


def density_card_count(canvas: Image.Image | None) -> int | None:
    """픽셀 글자밀도로 센 카드 개수. 판단 근거가 없으면 None.

    실측(003 3페이지, 2026-07-28): 3/2/1 을 모델 호출 0회로 정확히 재현했다 — VLM 3회
    투표가 내던 답과 같다. 개수만 쓴다. **배정(어느 영역이 몇 번 카드냐)은 여전히 VLM
    몫이다** — 전폭 헤드라인처럼 어느 컬럼에도 안 속하는 요소는 좌표로 못 정하기 때문
    (같은 실측: 컬럼 내부 요소는 p2 에서 100% 일치했지만 p1 의 전폭 헤드라인이 어긋났다).

    0 을 세면 '카드가 없다'가 아니라 **픽셀에서 글자를 못 찾았다**는 뜻이다(캔버스가
    비었거나 에지로 안 잡히는 표현). 라인은 있는데 픽셀이 비었다면 이 신호를 믿으면
    안 되므로 None 을 돌려 호출측이 기존 판정으로 되돌아가게 한다.
    """
    if canvas is None:
        return None
    from .bands import count_cards_by_density

    return count_cards_by_density(canvas)[0] or None


def _density_hint(canvas: Image.Image | None) -> tuple[str, int | None]:
    """밀도 관측을 프롬프트에 실을 문장으로. (힌트문, 개수) — 근거가 없으면 ('', None).

    **왜 힌트로 주나.** 예전에는 밀도 개수를 '재투표 트리거'로만 썼다. 밀도와 VLM 이
    어긋나면 **같은 질문을 두 번 더 던졌는데**, 새 정보를 안 주니 같은 답이 돌아왔다 —
    실측(003 p1, 2026-07-29): 밀도 3장 vs VLM 2장에서 3회를 더 써서 2장으로 확정됐고,
    그 결과 서로 다른 카드의 'EVENT 1.' 과 'EVENT 2.' 가 한 섹션에 묶였다.

    밀도를 절대 권한으로 올리지는 않는다 — 밀도가 VLM 보다 정확하다는 근거가 아직
    문서 하나뿐이다. 대신 **관측 결과를 증거로 넘겨** 판단은 VLM 에 남긴다. 누가 맞든
    '같은 질문 반복'보다는 항상 낫고, 재투표가 없어져 호출도 줄어든다.
    """
    if canvas is None or not canvas.width:
        return "", None
    from .bands import count_cards_by_density

    count, spans = count_cards_by_density(canvas)
    if not count or count < 2 or not spans:
        return "", (count or None)
    ranges = ", ".join(
        f"{i}번 x_ratio {x0 / canvas.width:.2f}~{x1 / canvas.width:.2f}"
        for i, (x0, x1, _mass) in enumerate(spans, 1)
    )
    return _DENSITY_HINT.format(count=count, ranges=ranges), count


def should_detect_cards(page: AdPage, canvas: Image.Image | None = None) -> bool:
    """카드-분할(VLM) 대상 페이지인지 결정론적으로 라우팅 — 낭비 없는 유형 분류.

    제외 1) 세로 스크롤(높이/폭 ≥ card_split_max_aspect): 모바일 상품페이지 등, 박스형
            섹션이 있어도 카드가 아니다.
    제외 2) 단일 패널: 003 p3 같은 한 덩어리 텍스트 슬라이드 — 글자밀도로 판정한다.
    → 둘 다 아닌 '가로 슬라이드 + 다중 덩어리'만 카드 후보 → 여기서만 VLM 호출.
    """
    if not (page.canvas_w and page.canvas_h):
        return False
    if page.canvas_h / page.canvas_w >= SETTINGS.card_split_max_aspect:
        return False  # 스크롤
    n = density_card_count(canvas)
    return _has_separable_clusters(page) if n is None else n >= 2


def assign_cards_vlm(page: AdPage, canvas: Image.Image | None, votes: int = 3) -> dict[str, int]:
    """VLM 으로 카드 개수·배정을 판정 → {region_id: card_no} (1..N=카드, 0=페이지 공통).

    판단 주체는 VLM(개수·배정은 의미 판단이라 VLM 이 정확). 단, 카드 판정도 서버측 비결정성으로
    실행마다 흔들리므로(003 p1 개수 2~3, 중간카드 쪼개짐 실측) **복수 관측 다수결**로 안정화한다:

    - votes 회 관측. 과반이 스크롤(None)이면 카드 없음({} 반환 → 기존 group_no 유지).
    - 카드 번호는 관측마다 순열이 다를 수 있으므로(run A 의 1 = run B 의 2), 번호를 직접 투표하지
      않고 **공동귀속(co-membership)**을 투표한다: 두 영역이 '같은 카드'로 묶인 횟수가 과반이면
      한 카드로 union(union-find). 공통(0)은 과반이 0으로 준 영역. → 라벨 순열에 강인.
    - 최종 카드 번호는 묶음의 시각 순서(위→아래, 좌→우)로 1..N 재부여.
    """
    judgeable = [r for r in page.regions if r.lines and r.bbox]
    if len(judgeable) < 2:
        return {}
    # 결정론 라우팅 게이트: 스크롤·단일패널은 카드 후보가 아니므로 VLM 호출조차 안 한다
    # (올원e 스크롤 4카드 오분할 방지 + 003 p3 단일패널 헛호출 방지 = 리소스 절약).
    if not should_detect_cards(page, canvas):
        return {}

    # 밀도 관측을 **증거로 실어** 1회만 묻는다. 투표를 여러 번 돌린 목적이 '개수 흔들림'
    # 안정화였는데(003 p1 개수 2~3), 같은 질문을 반복하는 것은 새 정보를 주지 않는다.
    # 결정론적인 밀도 관측을 프롬프트에 넣어 답을 고정하는 쪽이 낫다(_density_hint 주석).
    # 밀도 근거가 없을 때만(캔버스 없음 등) 예전처럼 복수 관측으로 흔들림을 흡수한다.
    hint, expected = _density_hint(canvas)
    obs = [_assign_once(page, judgeable, canvas, hint=hint)]
    if not hint:
        obs += [_assign_once(page, judgeable, canvas) for _ in range(max(0, votes - 1))]
    elif obs[0]:
        got = len({c for c in obs[0].values() if c > 0})
        if expected is not None and got != expected:
            # 증거를 보고도 다르게 답했다 — 다시 물어도 같은 답이 나온다. 받아들이되
            # 사람이 볼 수 있게 남긴다(조용한 불일치 금지).
            page.notes.append(
                f"카드 개수 불일치(VLM 판단 채택): 밀도 {expected}장 vs VLM {got}장"
            )

    card_obs = [o for o in obs if o]  # 카드형 관측만
    if len(card_obs) <= len(obs) // 2:
        return {}  # 과반이 스크롤/실패 → 카드 없음

    ids = [r.region_id for r in judgeable]
    k = len(card_obs)
    # 공통(0) 투표
    common = {rid for rid in ids
              if sum(1 for o in card_obs if o.get(rid) == 0) * 2 > k}
    members = [rid for rid in ids if rid not in common]

    # 공동귀속 union-find (둘 다 같은 card_no>0 인 관측이 과반)
    parent = {rid: rid for rid in members}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            a, b = members[i], members[j]
            same = sum(1 for o in card_obs
                       if o.get(a, -1) == o.get(b, -2) and o.get(a, -1) > 0)
            if same * 2 > k:
                parent[find(a)] = find(b)

    # 묶음 → 시각 순서로 카드 번호
    bbox_of = {r.region_id: r.bbox for r in judgeable}
    comps: dict[str, list[str]] = {}
    for rid in members:
        comps.setdefault(find(rid), []).append(rid)
    def comp_key(rids):
        bs = [bbox_of[r] for r in rids if bbox_of.get(r)]
        return (min(b[1] for b in bs), min(b[0] for b in bs)) if bs else (0, 0)
    ordered = sorted(comps.values(), key=comp_key)
    out = {rid: 0 for rid in common}
    for cno, rids in enumerate(ordered, start=1):
        for rid in rids:
            out[rid] = cno
    return out if len(ordered) >= 2 else {}  # 카드가 1개로 수렴하면 그룹핑 불필요
