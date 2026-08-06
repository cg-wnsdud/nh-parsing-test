from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_env_file(path: Path) -> None:
    """.env 파일을 읽어 **아직 없는** 환경변수만 채운다(이미 export 된 값이 우선).

    HyundaiHS(orchestrator/config.py::load_env_file)와 같은 방식 — python-dotenv
    의존성 없이 직접 파싱한다. 실제 사내 엔드포인트(PADDLEX_URL/GEMMA_URL 등)를
    코드에 하드코딩하지 않기 위한 장치다(2026-08-01, 저장소가 잠깐 공개돼 있던
    사고 이후 조치). `.env`는 `.gitignore` 대상이라 이 파일을 만들어도 커밋되지 않는다.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _find_env_file() -> Path | None:
    """`.env` 를 찾는다 — 현재 작업 디렉터리에서 위로, 그다음 소스 트리 기준으로.

    예전에는 `parents[2]/.env` 하나만 봤다(= 소스 레이아웃의 저장소 루트). 패키지를
    설치해 쓰면 `__file__` 이 site-packages 안이라 그 경로가 존재하지 않는다.
    작업 디렉터리에서 위로 올라가며 찾는 쪽을 먼저 두면 두 경우가 다 된다.
    """
    cwd = Path.cwd().resolve()
    here = Path(__file__).resolve()
    candidates = [cwd, *cwd.parents, *here.parents]
    seen: set[Path] = set()
    for base in candidates:
        if base in seen:
            continue
        seen.add(base)
        env = base / ".env"
        if env.is_file():
            return env
    return None


_ENV_FILE = _find_env_file()
if _ENV_FILE is not None:
    load_env_file(_ENV_FILE)


@dataclass(frozen=True)
class Settings:
    # ── 외부 서비스 (Spark, WireGuard 필요) ─────────────────────────
    # 실제 사내 엔드포인트는 .env(gitignore 대상)에만 둔다 — 여기 기본값은 저장소가
    # 공개돼도 내부망 주소가 드러나지 않는 자리표시자다. .env.example 참고해 로컬
    # .env 를 만들 것 (2026-08-01, 저장소가 실제로 공개돼 있던 사고 이후 조치).
    paddlex_url: str = os.environ.get(
        "PADDLEX_URL", "http://YOUR_PADDLEX_HOST:8081/layout-parsing"
    )
    gemma_url: str = os.environ.get(
        "GEMMA_URL", "http://YOUR_GEMMA_HOST:4000/v1/chat/completions"
    )
    gemma_model: str = os.environ.get("GEMMA_MODEL", "gemma-4-26b-NVFP4-MTP")
    paddlex_timeout_s: int = int(os.environ.get("PADDLEX_TIMEOUT_S", "180"))
    gemma_timeout_s: int = int(os.environ.get("GEMMA_TIMEOUT_S", "120"))

    # ── PDF triage 임계치 (document-processor probe 기준에서 출발) ──
    min_readable_chars: int = 20          # 이하면 텍스트 레이어 불신
    max_fffd_ratio: float = 0.3           # U+FFFD 비율 초과 시 SCAN_LIKE
    min_fffd_count: int = 8               # 절대 개수 하한 (오탐 방지)
    hybrid_image_area_ratio: float = 0.5  # STRUCTURED여도 이미지 면적비 초과 시 OCR 병행

    # ── 렌더/타일링 ─────────────────────────────────────────────
    pdf_render_dpi: int = 200
    tile_max_height_px: int = 1600        # 조각 하나의 높이 상한 (자르는 위치는 글자 밀도가 결정)
    tile_overlap_px: int = 200
    # 타일링 트리거. 이 값은 이전 프로젝트에서 물려받은 것이고 아래 2500 과 맞춰 계산한
    # 값이 아니다 — 그래서 2500~4000 구간(통짜로 들어가는데 서버는 2500 으로 줄이는 구간)이
    # 검증된 적이 없었다. 2026-07-28 실측으로 확인함: 폭 720px 고정, 높이 2200~6111px 로
    # 늘려가며 002 의 fine-print 를 읽혔을 때 **모든 높이에서 동일하게 검출**됐다(0.41배
    # 축소까지). PP-OCR 구조상 검출은 축소본에서 하지만 인식은 원본 좌표를 다시 크롭해
    # 읽기 때문이다. 즉 이 구간의 위험은 이 데이터에서는 발현하지 않는다.
    # 다만 문서 1건·폰트 1종 실측이라 일반화는 안 되고, 샘플 5개 중 이 구간에 드는 문서가
    # 하나도 없다(1120px 또는 6100px+). 해당 크기 입력이 실제로 들어오면 재확인할 것.
    tile_trigger_height_px: int = 4000    # 장변 기준 타일링 트리거

    # ── PaddleX PP-StructureV3 서빙 파라미터 (공식 predict 파라미터의 camelCase) ──
    # text_det_limit_side_len 서버 기본 960/max 는 타일(장변 2000px)을 절반 이하로
    # 축소해 fine-print 를 깨뜨린다 (2026-07-17 실측: 올원 유의사항 완전 복구).
    paddlex_text_det_limit_side_len: int = 2500
    paddlex_text_det_limit_type: str = "max"
    # 기본 "large" 는 겹침 박스를 큰 쪽으로 흡수 — 사진형 카드 콜라주(003 p1)가
    # 통짜 1블록이 된 원인. "small" 로 카드별 하위 블록 유지 (3→20블록 실측).
    paddlex_layout_merge_bboxes_mode: str = "small"
    paddlex_use_formula_recognition: bool = False  # 광고물에 수식 없음 — 속도 절약
    # 방향 분류기(기본 True)가 얇은 회색 fine-print 를 180도 회전으로 오판해
    # 거꾸로 인식('링이어니을용은' 사건, 2026-07-17 실측). 디지털 캡처/정방향
    # 렌더 입력에는 회전이 없으므로 비활성화.
    paddlex_use_textline_orientation: bool = False

    # ── OCR 라인 병합/중복 제거 ──────────────────────────────────
    dedupe_iou: float = 0.5
    dedupe_containment: float = 0.7   # 겹침/작은쪽 면적 — 타일 경계 부분 조각 제거

    # ── VLM 전체화면 호출의 밴드 분할 ────────────────────────────
    # VLM 서버는 pan-and-scan 없이 어떤 크기든 고정 예산(이미지 토큰 ~1,050~1,100)으로
    # 리샘플링한다(2026-07-27 실측: 896x896 과 1122x6429 의 토큰이 같음). 세로로 긴
    # 캔버스를 통짜로 보내면 축소율만 커져 작은 글씨가 소실된다 — 같은 문단을 통짜로
    # 주면 3/13, 구간만 잘라 주면 13/13 회수. 그래서 폭 기준 비율로 잘라 보낸다.
    # 비율 2.0 근거: 001/002/올원 A/B 에서 통짜가 일관되게 최악(0.23~0.27)이나 최적
    # 비율은 문서마다 엇갈려(001 은 큰 밴드, 002 는 작은 밴드 유리), 최악값이 가장
    # 높은 2.0 을 택했다. 표본이 늘면 재측정 대상.
    vlm_band_ratio: float = 2.0            # 밴드 높이 = 캔버스 폭 x 이 값
    vlm_band_min_height_px: int = 900      # 좁은 캔버스에서 과분할 방지 하한
    # 산술 절단은 글자 한가운데를 지날 수 있다(001 실측: 헤드라인·112px '20,000원',
    # 002: '농협은행이'). 잘린 글자는 양쪽 밴드 어디서도 안 읽혀 회수에서 빠지므로,
    # 이미 아는 텍스트/블록 bbox 를 피해 이 범위 안에서 절단선을 당긴다.
    vlm_band_snap_px: int = 160
    # 응답이 max_tokens 에서 잘리면 JSON 이 깨져 그 밴드 회수분이 통째로 사라진다
    # (실측: 'Unterminated string' → 회수율 0.95 에서 0.27 로 폭락). 넉넉히 준다.
    sweep_max_tokens: int = 3000

    # ── 저신뢰 라인 VLM 재판독 (2b) ──────────────────────────────
    # 유지 결정(2026-07-27): 한때 장식 기호('¥'→'★')만 고쳐 제거 후보로 봤으나, 다음
    # 실행에서 'M모닝이[신'→'부모님이 대신', '소비승관의 첫결음'→'소비습관의 첫걸음'
    # 처럼 실제 문구를 복원했다. 1회 관측으로 판단하면 안 된다는 실측 사례.
    # 심의 관련 영역의 신뢰도 낮은 OCR 라인을 고해상 크롭으로 다시 읽혀 교정한다.
    # 예시/장식·이미지 영역은 제외(무관·비용). 판단 주체는 VLM(크롭 재판독).
    lowconf_reread_threshold: float = 0.80     # OCR 신뢰도 이 미만 라인이 재판독 후보
    lowconf_reread_min_vlm_conf: float = 0.60  # VLM 재판독 확신도 이 이상일 때만 텍스트 교체
    lowconf_reread_max_per_page: int = 12      # 페이지당 재판독 호출 상한 (비용 가드)

    # ── 밴드 단위 통합 판독 (④+ 통독 + ⑧ 스윕을 한 호출로) ─────────────
    # OCR 이 본 것과 **같은 밴드**를 VLM 에도 주고, 그 안의 영역 목록을 함께 실어
    # "이 영역들을 고쳐라 + 목록에 없는 문구를 찾아라"를 한 호출로 묻는다. 예전에는
    # 영역별 통독이 영역을 하나씩(4장 배치) 보고 스윕이 밴드를 따로 봐서, 같은 페이지를
    # 서로 다른 크롭으로 두 번 훑었다 — 그 개별 경로는 이 방식이 완전히 흡수해
    # 죽은 코드가 됐고 2026-07-29 제거했다.
    #
    # 우려했던 것은 해상도였다 — 영역 크롭은 영역 하나가 896x896 을 다 쓰지만 밴드는
    # 영역 5~10개가 나눠 쓴다. A/B 실측(2026-07-28, 5문서)에서 그 손해보다 이득이 컸다:
    #   VLM 호출  122 → 65회 (-47%)      파싱 시간 37.2 → 12.2분 (-67%)
    #   골드 문장 234~235 → 236/242      STAGE_3 골드 필드 44/44 유지
    #   OCR 과 다른 교정 후보 49 → 36개 (73% 유지 — 기각선 50% 통과)
    # 원문자 교정도 살아남았고 오히려 정확해졌다(올원e p1_r017: 기존은 ② 영역에 ③ 내용을
    # 넣는 오정렬이었는데 병합 쪽이 ② 를 제대로 읽음) — 밴드가 주변 맥락을 함께 보기 때문.
    #
    # 밴드 경계가 영역을 반토막 내는 문제는 크롭을 넓혀 해결한다(실측 14개 → 0개).

    # ── 카드-분할 (§D) ────────────────────────────────────────
    # 세로 스크롤(모바일 상품페이지 등)은 박스형 섹션이 있어도 카드 collage 가 아니다.
    # VLM 이 그런 섹션을 "카드"로 오판하면(올원e 스크롤을 4카드로 쪼갠 실측) 섹션이
    # 파편화된다. 캔버스 종횡비(높이/폭)가 이 값 이상인 '긴 스크롤'은 카드-분할 대상에서
    # 제외한다 — 스크롤 vs 슬라이드는 구조적 구분(과적합 아님). 슬라이드(003 ≈0.56)만 대상.
    card_split_max_aspect: float = 2.0         # 높이/폭 이 이상이면 스크롤 → 카드-분할 안 함
    card_split_votes: int = 3                  # 카드 판정 복수관측 횟수 (다수결 안정화)

    # ── 분류 (파일명 prior) ─────────────────────────────────────
    product_group_keywords: dict = field(
        default_factory=lambda: {
            "예금성": ["예금성", "예금", "적금", "입출금"],
            "대출성": ["대출성", "대출", "신용대출", "담보"],
        }
    )


SETTINGS = Settings()
