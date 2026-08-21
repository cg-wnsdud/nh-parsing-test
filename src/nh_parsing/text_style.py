"""라인 시인성(글자 크기·굵기·색) 집계 — HWP `RunIR` 와 PDF 글자를 같은 모양으로 접는다.

**왜 별 모듈인가.** 재료는 두 곳에서 온다(HWP 는 파서가 선언값을, PDF 는 pdfium 이
글자별 값을 준다). 접는 규칙(대표값은 글자수 최다, 굵기는 하나라도 있으면 True)은
같아야 하는데 두 파일에 따로 쓰면 갈린다. 규칙만 여기 모으고, 재료 수집은 각 파일이 한다.

**판정은 하지 않는다.** "몇 pt 부터 작은가"는 규정이 숫자를 안 주므로(은행 광고심의 기준
§16①8 은 '균형'만 요구) 여기서 정하지 않는다 — 값만 담아 하류로 넘긴다.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

from .ir import SizeBasis, TextStyle

# 글자 하나 = (크기pt, 굵음, 색, 글꼴). 크기·색·글꼴은 없을 수 있다.
StyleAtom = tuple[Optional[float], Optional[bool], Optional[str], Optional[str]]


def fold(atoms: Iterable[StyleAtom], *, basis: SizeBasis, source: str) -> TextStyle | None:
    """글자별 스타일 목록 → 라인 대표 스타일. 하나도 못 얻으면 None.

    대표값은 **글자수 최다**로 뽑는다. 평균이 아니다 — 평균은 35pt 한 글자와 8pt 서른
    글자를 섞어 실제로 존재하지 않는 크기를 만든다. 최소·최대를 따로 담으므로 격차는
    그쪽에서 읽는다.
    """
    atoms = [a for a in atoms if a is not None]
    if not atoms:
        return None

    sizes = Counter(a[0] for a in atoms if a[0] is not None)
    colors = Counter(a[2] for a in atoms if a[2])
    fonts = Counter(a[3] for a in atoms if a[3])
    bolds = [a[1] for a in atoms if a[1] is not None]

    if not sizes and not colors and not fonts and not bolds:
        return None

    return TextStyle(
        size_pt=sizes.most_common(1)[0][0] if sizes else None,
        size_pt_min=min(sizes) if sizes else None,
        size_pt_max=max(sizes) if sizes else None,
        size_basis=basis if sizes else None,
        # 라인 안에 굵은 글자가 하나라도 있으면 True. "강조가 있었나"를 묻는 값이라
        # 다수결이 아니다 — 금리 숫자만 굵은 흔한 배치에서 다수결은 False 가 된다.
        bold=any(bolds) if bolds else None,
        color=colors.most_common(1)[0][0] if colors else None,
        colors=[c for c, _ in colors.most_common()],
        font=fonts.most_common(1)[0][0] if fonts else None,
        style_source=source,
    )


def rgb_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def strip_subset_prefix(name: str) -> str:
    """PDF 서브셋 글꼴명의 `ABCDEF+` 접두어를 뗀다 (`TRDBGG+OTMGothicL` → `OTMGothicL`).

    접두어는 같은 글꼴이라도 문서마다 달라서(무작위 6글자) 붙여 두면 문서 간 집계가 안 된다.
    """
    if len(name) > 7 and name[6] == "+" and name[:6].isalpha() and name[:6].isupper():
        return name[7:]
    return name
