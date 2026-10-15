"""Invariant 4: no source report labelled as certain reality.

Every observation the connectors parse from captured provider responses keeps
an explicit *unquantified* uncertainty (the providers publish none) and is never
labelled ``exact_by_definition``; published belief estimates built from them are
``selected_report`` rows that keep that status, and the kernel refuses to label
a report as exact unless a definition kind is declared.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.evidence.runner import FixtureHttp
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.ports.connectors import FetchBudget

STATISTICAL_CONNECTORS = ("worldbank.wdi.indicators", "un.comtrade.public_exact")


def _parsed(name: str) -> list[Any]:
    conn = builtin_connectors()[name]
    http = FixtureHttp(name)

    async def go() -> list[Any]:
        out: list[Any] = []
        for item in conn.plan(None, FetchBudget(max_requests=1000)).items:
            try:
                raw = await conn.fetch(item, http)
            except Exception as e:
                if getattr(e, "details", {}).get("kind") == "fixture_missing":
                    continue
                raise
            for batch in conn.parse(raw):
                out.extend(batch.observations)
        return out

    return asyncio.run(go())


@pytest.mark.parametrize("name", STATISTICAL_CONNECTORS)
def test_parsed_source_reports_are_never_certain(name: str) -> None:
    obs = _parsed(name)
    assert obs, f"no captured observations replayed for {name}"
    for o in obs:
        u = o.uncertainty
        assert u.status == UncertaintyStatus.unquantified, (name, o.logical_key, u)
        assert u.status != UncertaintyStatus.exact_by_definition
        # an unquantified report is not a zero-width interval around the value
        assert u.lower is None and u.upper is None and u.level is None


def test_kernel_refuses_exact_label_without_a_definition() -> None:
    for kind in (UncertaintyKind.unknown, UncertaintyKind.bounds):
        with pytest.raises(ValueError, match="exact_by_definition must have kind=definition"):
            UncertaintySpec(status=UncertaintyStatus.exact_by_definition, kind=kind, description="a source said so")


@pytest.mark.db
def test_published_source_estimates_keep_unquantified_status(db, fixture_release) -> None:
    with db.owner.connect() as c:
        rows = (
            c.execute(
                text(
                    """SELECT e.modality, e.uncertainty->>'status' AS status, count(*) AS n
               FROM belief.estimate e JOIN belief.input i ON i.estimate_id = e.id
               WHERE e.release_id = :r AND i.observation_id IS NOT NULL
               GROUP BY 1, 2"""
                ),
                {"r": fixture_release["release_id"]},
            )
            .mappings()
            .all()
        )
    assert rows, "the fixture release published no source-backed estimates"
    for r in rows:
        assert r["modality"] == "selected_report", dict(r)
        assert r["status"] == "unquantified", dict(r)
