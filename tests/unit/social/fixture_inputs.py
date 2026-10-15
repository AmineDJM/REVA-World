"""Offline inputs for the retirement-age model from captured provider fixtures (no DB, no network).

* ``tests/fixtures/source/eurostat.labour_pensions`` - this module's connector (live capture 2026-10-15);
* ``tests/fixtures/social/eurostat.demography`` - agent POP's connector, FR/DE/ES/NL observed
  demography (live capture 2026-10-15, recorded with POP's reviewed connector).

Tables are derived with the connectors' own dataset derivations, exactly as the platform would.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
from typing import Any

from reva.adapters.connectors import eurostat_demography as demo
from reva.adapters.connectors import eurostat_labour as lab
from reva.evidence.runner import FixtureHttp
from reva.models.social.retirement_age import CONCEPTS
from reva.ports.connectors import FetchBudget
from reva.simulation.plugins import InputRequirement, ResolvedInputs

ROOT = Path(__file__).resolve().parents[2] / "fixtures"
TABLE_OF = {"employment_rate": "lfsa_ergan", "activity_rate": "lfsa_argan", "pension_beneficiaries": "spr_pns_ben",
            "pension_expenditure": "spr_exp_pens"}
DEMO_OF = {"population": "demo_pjan", "deaths": "demo_magec", "births": "demo_fasec"}


async def _raws(conn: Any, http: Any) -> list[Any]:
    return [await conn.fetch(i, http) for i in conn.plan(None, FetchBudget()).items]


@lru_cache(maxsize=1)
def labour_tables() -> dict[str, dict[str, Any]]:
    """{table code: {ISO3: arrow table}} from the labour/pensions fixtures."""
    c = lab.EurostatLabourPensionsConnector()
    raws = asyncio.run(_raws(c, FixtureHttp(lab.CONNECTOR_NAME)))
    return {r.item.params["table"]: lab.derive_dataset_tables(r.body, r.item.params["table"]) for r in raws}


@lru_cache(maxsize=1)
def demography_tables() -> dict[str, dict[str, Any]]:
    c = demo.EurostatDemographyConnector(["FR", "DE", "ES", "NL"], include_europop=False)
    raws = asyncio.run(_raws(c, FixtureHttp(demo.NAME, root=ROOT / "social")))
    out: dict[str, dict[str, Any]] = {}
    for r in raws:
        out.setdefault(r.item.params["dataset"], {}).update(demo.dataset_tables(r))
    return out


def tables_for(iso3: str) -> dict[str, Any]:
    lt, dt = labour_tables(), demography_tables()
    out = {name: lt[code].get(iso3) for name, code in TABLE_OF.items()}
    out.update({name: dt.get(code, {}).get(iso3) for name, code in DEMO_OF.items()})
    return out


def resolved_inputs(requirements: list[InputRequirement], drop: tuple[tuple[str, str], ...] = ()) -> ResolvedInputs:
    datasets = {}
    for r in requirements:
        t = tables_for(r.subject).get(r.name)
        if t is not None and (r.name, r.subject) not in drop:
            datasets[(r.name, r.subject)] = (t, {"dataset_id": f"fixture:{r.name}:{r.subject}",
                                                 "concept": CONCEPTS[r.name]})
    return ResolvedInputs.from_values(requirements, datasets=datasets, release={"id": "fixture-release"})
