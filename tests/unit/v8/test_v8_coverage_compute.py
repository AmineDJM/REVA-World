"""V8 COUVERTURE: aggregation helpers (no database): per-country counts, licence classes, catalog gaps, observation of
queued loads."""

from __future__ import annotations

from datetime import date

import polars as pl

from reva.v8.coverage.compute import catalog_by_domain, licence_class, per_country
from reva.v8.coverage.pending import Check, Item, observe


def test_per_country_counts_first_and_latest_past_year_and_future_rows() -> None:
    f = pl.DataFrame(
        {
            "iso3": ["FRA", "FRA", "FRA", "DEU", None, None],
            "period_start": [
                date(1850, 1, 1),
                date(2020, 1, 1),
                date(2030, 1, 1),
                date(2024, 7, 1),
                date(2024, 1, 1),
                date(2025, 1, 1),
            ],
        }
    )
    countries, hist, before, unmapped, latest = per_country(f, date(2026, 10, 17))
    assert countries["FRA"] == [3, 1850, 2020, 1]  # 2030 is after "today": counted as future, not as latest
    assert countries["DEU"] == [1, 2024, 2024, 0]
    assert "None" not in countries and unmapped == 2
    assert before == 1 and hist == {2020: 1, 2030: 1, 2024: 2, 2025: 1}
    assert latest == 2025


def test_per_country_only_future_rows_has_no_latest_past_year() -> None:
    f = pl.DataFrame({"iso3": ["AGO"], "period_start": [date(2050, 1, 1)]})
    countries, _, _, _, latest = per_country(f, date(2026, 10, 17))
    assert countries["AGO"] == [1, 2050, None, 1]
    assert latest is None


def test_licence_class() -> None:
    assert licence_class(None) == "unknown"
    assert licence_class({"licence": "CC-BY-4.0", "open_licence": True, "restricted": False}) == "open"
    assert licence_class({"licence": "CC-BY-NC", "non_commercial": True, "restricted": True}) == "non_commercial"
    assert licence_class({"licence": "terms", "open_licence": False, "restricted": True}) == "restricted"
    assert licence_class({"licence": "?", "open_licence": None, "restricted": False}) == "unknown"


def test_catalog_by_domain_counts_concepts_without_loaded_tables() -> None:
    concepts = [
        ("a", "macro", "A", ["atlas.imf.weo", "atlas.wb.wdi"]),
        ("b", "macro", "B", ["atlas.oecd.qna"]),
        ("c", "health", "C", ["atlas.who.gho"]),
    ]
    out = catalog_by_domain(concepts, {"atlas.wb.wdi", "atlas.who.gho"})
    assert out["macro"] == {
        "concepts": 2,
        "with_loaded_table": 1,
        "without_loaded_table": 1,
        "examples": [{"id": "b", "label": "B", "tables": ["atlas.oecd.qna"]}],
    }
    assert out["health"]["without_loaded_table"] == 0


def _load(*checks: Check) -> Item:
    return Item.model_validate(
        {
            "id": "load.t",
            "type": "load",
            "kind": "disk",
            "title": "Un chargement",
            "unlocks": {"text": "des tables"},
            "action": "Lancer",
            "commands": ["flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.x"],
            "who": "lead",
            "status": "queued",
            "source_docs": ["docs/dev/HANDOFF_V3.md"],
            "check": [c.model_dump() for c in checks],
        }
    )


def test_observe_needs_version_and_tables() -> None:
    it = _load(Check(connector="atlas.x", min_version="1.0.1", tables=("atlas.x.t",)), Check(connector="atlas.y"))
    assert observe(it, {}, [])["state"] == "not_seen"  # type: ignore[index]
    acq = {"atlas.x": {"versions": ["1.0.0"], "last_retrieved_at": "2026-10-17T20:00:00+00:00"}}
    o = observe(it, acq, ["atlas.x.t"])
    assert o is not None and o["state"] == "not_seen" and o["checks"][0]["met"] is False  # 1.0.0 < 1.0.1
    acq["atlas.x"]["versions"] = ["1.0.0", "1.0.10"]
    o = observe(it, acq, [])
    assert o is not None and o["checks"][0]["tables_missing"] == ["atlas.x.t"] and o["state"] == "not_seen"
    o = observe(it, acq, ["atlas.x.t"])
    assert o is not None and o["state"] == "partial"
    acq["atlas.y"] = {"versions": ["2.0.0"], "last_retrieved_at": None}
    assert observe(it, acq, ["atlas.x.t"])["state"] == "done"  # type: ignore[index]


def test_observe_ignores_decisions_and_loads_without_checks() -> None:
    it = _load()
    assert observe(it, {}, []) is None
