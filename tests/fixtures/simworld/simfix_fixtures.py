"""Offline fixture of agent SIMFIX: ILOSTAT SDG 1.3.1 (SDG_0131_SEX_SOC_RT_A) excerpt of the real bytes.

``tests/fixtures/source/ilostat.social_protection`` holds verbatim lines (every function) of SEN, JPN,
BRA, IND, MEX, IDN, JOR (an unreliable row without value), KOS (Kosovo -> XKX) and X01 (an ILO regional
aggregate, unmapped), retrieved 2026-10-16; the manifest names the full file's sha256.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from reva.adapters.connectors import ilostat_social_protection as SP
from reva.evidence.runner import FIXTURE_ROOT


def sp_body() -> bytes:
    return (FIXTURE_ROOT / SP.NAME / f"{SP.INDICATOR_ID}.csv.gz").read_bytes()


@cache
def sp_tables() -> dict[str, Any]:
    return SP.build_tables(sp_body())[0]


def sp_table(iso3: str) -> Any:
    return sp_tables().get(iso3)
