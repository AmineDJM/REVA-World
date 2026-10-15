"""Review V3 (agent V3REVIEW): reproductions of defects found in the V3 review (docs/dev/review_v3.md).

Every test that demonstrates a defect is ``xfail(strict=True)``: the suite stays green while the defect
exists and turns red (XPASS) the day it is fixed, so the marker must then be removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

NLQA_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nlqa"
if str(NLQA_FIXTURES) not in sys.path:
    sys.path.insert(0, str(NLQA_FIXTURES))
