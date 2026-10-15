"""Live ingest of the A-HEALTHEDU atlas connectors (WHO GHO, UNESCO UIS) into the throwaway test database.

Deselected by default (``-m live``). Runs the full curated indicator lists for every country through
the budget path of the ingest job (``reserve_ingest`` -> ``metered`` -> ``settle_metered``, pinned
``registry/policies/budgets.yaml``: refused while the ``who.gho`` / ``unesco.uis`` keys are not merged),
archives the raw responses, registers ``atlas.who.gho`` and ``atlas.uis.api`` and checks coverage
invariants. The ``db`` fixture's database and object root are dropped/temporary.

``REVA_HEALTHEDU_LIVE_OUT=<path.json>`` writes the coverage summary used by docs/dev/v3_a_healthedu.md.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_uis as uis
from reva.adapters.connectors import atlas_who as who
from reva.adapters.connectors.atlas_healthedu_common import register_connector, run_and_register
from reva.atlas.catalog import load_catalog
from reva.atlas.tables import known_country_keys, read_atlas_table
from reva.evidence.runner import make_live_client
from reva.operations.budgets import metered, reserve_ingest, settle_metered

pytestmark = [pytest.mark.live, pytest.mark.db]


async def _live(db: Any, conn: Any) -> dict[str, Any]:
    spec = conn.describe()
    register_connector(db.owner, db.store, conn)
    planned = len(conn.expected_item_keys())
    reservation = reserve_ingest(
        db.worker, job_id=uuid.uuid4(), attempt=1, source_key=spec.source_key, planned_requests=planned
    )
    http = metered(make_live_client(spec, db.settings), db.worker, reservation)
    try:
        out = await run_and_register(conn, http, db.store, db.worker)
    finally:
        budget = settle_metered(db.worker, http)
        await http.aclose()
    out["budget"] = budget
    return out


def _coverage(frame: pl.DataFrame, concepts: list[Any], source: str, dataset: str) -> dict[str, Any]:
    keys = sorted(known_country_keys())
    num = frame.filter(
        pl.col("iso3").is_not_null() & (pl.col("value").is_not_null() | pl.col("value_text").is_not_null())
    )
    num = num.with_columns(pl.col("period_start").dt.year().alias("year"))
    out: dict[str, Any] = {}
    for c in concepts:
        series = [s for s in c.source_order if (s.source, s.dataset) == (source, dataset)]
        if not series:
            continue
        sub = num.filter(pl.col("indicator") == series[0].indicator)
        per = sub.group_by("iso3").agg(pl.col("year").min().alias("y0"), pl.col("year").max().alias("y1"))
        latest = int(sub["year"].max()) if sub.height else None
        recent = set(sub.filter(pl.col("year") >= (latest or 0) - 4)["iso3"].unique().to_list()) if latest else set()
        have = set(per["iso3"].to_list())
        out[c.id] = {
            "series": series[0].key,
            "cells": sub.height,
            "countries": len(have),
            "countries_last5y": len(recent),
            "years": [int(sub["year"].min()), latest] if sub.height else None,
            "missing_countries": sorted(set(keys) - have),
        }
    return out


async def test_live_ingest_who_and_uis(db) -> None:
    results: dict[str, Any] = {}
    cat = load_catalog()
    concepts = (
        [c for c in cat.concepts.values()] if isinstance(getattr(cat, "concepts", None), dict) else list(cat.concepts)
    )
    for conn, source, dataset in ((uis.UisConnector(), "uis", "api"), (who.WhoGhoConnector(), "who", "gho")):
        out = await _live(db, conn)
        assert out["status"] == "registered", json.dumps({k: v for k, v in out.items() if k != "run"}, default=str)[
            :3000
        ]
        atlas = out["atlas"]
        frame = read_atlas_table(db.store, atlas["artifact_sha256"])
        cov = atlas["coverage"]
        assert cov["countries"] >= 180, cov
        assert frame["indicator"].n_unique() >= len(conn.indicators)
        per_ind = (
            frame.with_columns(pl.col("indicator").str.split("|").list.first().alias("code"))
            .group_by("code")
            .agg(pl.len().alias("rows"), pl.col("iso3").n_unique().alias("countries"))
        )
        assert set(per_ind["code"].to_list()) == set(conn.indicators)
        flags = frame["flags"].unique().to_list()
        classes: dict[str, int] = defaultdict(int)
        for f in flags:
            classes[json.loads(f).get("estimate_class", "?")] += 1
        results[f"{source}.{dataset}"] = {
            "atlas": {k: v for k, v in atlas.items() if k != "coverage"},
            "coverage": cov,
            "budget": out["budget"],
            "per_indicator": sorted(per_ind.to_dicts(), key=lambda r: r["code"]),
            "estimate_classes_distinct_flag_objects": dict(classes),
            "concepts": _coverage(frame, concepts, source, dataset),
            "extra": {k: out.get(k) for k in ("uis_version", "indicator_names", "last_data_update")},
            "raw_bytes": sum(len(db.store.get("raw", s)) for s in cov["raw_sha256s"]),
            "parquet_bytes": len(db.store.get("normalised", atlas["artifact_sha256"])),
        }
    path = os.environ.get("REVA_HEALTHEDU_LIVE_OUT")
    if path:
        with open(path, "w") as fh:
            json.dump(results, fh, indent=1, default=str)
