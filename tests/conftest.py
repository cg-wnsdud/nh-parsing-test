# -*- coding: utf-8 -*-
"""pytest 부트스트랩 — package=false 운용이라 러너처럼 src/ 를 sys.path 에 넣는다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
