"""Invariant 25: no default country/geography permanently excluded from possible refinement.

Every default country profile is a target of every crude-exposure requirement in
the versioned catalogue, and the gap detector keeps reporting a country whose
data is missing (DZA's typed-missing crude imports) on every run instead of
dropping it after a failed acquisition: a gap is a standing work item, not an
exclusion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from reva.autonomy.gaps import classify
from reva.ontology.registry import load_registry

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def _default_countries() -> set[str]:
    doc = yaml.safe_load((REPO / "registry" / "entities" / "countries.yaml").read_text())
    return {e["key"] for e in doc["entities"] if e.get("kind") == "entity.country"}


def test_every_default_country_is_a_target_of_every_crude_exposure_requirement() -> None:
    countries = _default_countries()
    assert {"FRA", "DZA", "CHN", "USA"} <= countries
    reqs = load_registry().requirements
    crude = [r for r in reqs["requirements"] if r["task"] == "crude_exposure"]
    assert crude
    for r in crude:
        assert countries <= set(r["geography"]), (r["id"], sorted(countries - set(r["geography"])))


def _snapshot(dza_status: str) -> dict:
    concept = "trade.hs2709.import_value.world_total"
    cells = [{"concept_id": concept, "geo": geo, "year": y, "status": "value", "missing_reasons": [],
              "latest_recorded_at": (NOW - timedelta(days=5)).isoformat()}
             for geo in ("FRA", "CHN", "USA") for y in range(2020, 2025)]
    cells += [{"concept_id": concept, "geo": "DZA", "year": y, "status": dza_status,
               "missing_reasons": ["unavailable"] if dza_status == "missing" else [],
               "latest_recorded_at": None if dza_status == "missing" else (NOW - timedelta(days=5)).isoformat()}
              for y in range(2020, 2025)]
    return {"schema": "reva-coverage-snapshot/1", "eligible": {concept: ["FRA", "DZA", "CHN", "USA"]},
            "cells": cells}


def _dza_crude_gaps(snapshot: dict, cutoff: datetime) -> list[dict]:
    reqs = load_registry().requirements
    return [g for g in classify(reqs, snapshot, cutoff)
            if g["geo"] == "DZA" and g["requirement_id"] == "R-CRUDE-M" and g["status"] != "covered"]


def test_a_missing_country_stays_a_gap_on_every_detection_run() -> None:
    snap = _snapshot("missing")
    # never silently dropped after repeated failed acquisitions, however long the gap persists
    runs = [_dza_crude_gaps(snap, NOW + timedelta(days=d)) for d in (0, 30, 365, 3650)]
    assert all({g["year"] for g in r} == set(range(2020, 2025)) for r in runs), runs
    assert all(g["status"] == "missing" and "typed Missing" in g["detail"] for r in runs for g in r)


def test_the_gap_closes_only_when_data_arrives() -> None:
    assert _dza_crude_gaps(_snapshot("value"), NOW) == []
