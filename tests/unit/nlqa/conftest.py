"""NLQA unit tests: make the in-memory fakes (tests/fixtures/nlqa/nlqa_fakes.py) importable."""

from __future__ import annotations

import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "nlqa"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))
