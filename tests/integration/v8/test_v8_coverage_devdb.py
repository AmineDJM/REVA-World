"""V8 COUVERTURE against the dev database `reva` (read-only; skipped when it is not reachable).

Recomputes the aggregates for two real atlas tables (no KG pass) inside a READ ONLY transaction and checks them
against direct SQL / the table manifests; the live checks of queued loads run on the real acquisitions.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

TABLES_SQL = """
SELECT DISTINCT ON (dataset_key) dataset_key, row_count, coverage FROM evidence.dataset
WHERE concept_id = 'atlas.table' AND origin = 'live' ORDER BY dataset_key, recorded_at DESC
"""


@pytest.fixture(scope="module")
def ctx() -> dict[str, Any]:
    from reva.adapters.objects.factory import build_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    s = get_settings()
    try:
        eng = make_engine("api", s)
        with eng.connect() as c:
            rows = {r["dataset_key"]: dict(r) for r in c.execute(text(TABLES_SQL)).mappings()}
    except Exception:
        pytest.skip("dev database not reachable")
    if not rows:
        pytest.skip("no live atlas tables in the dev database")
    return {"engine": eng, "store": build_store(s), "tables": rows}


def test_compute_two_tables_matches_manifests(ctx: dict[str, Any]) -> None:
    from reva.v8.coverage.compute import compute

    small = sorted((k for k, v in ctx["tables"].items() if 1_000 < int(v["row_count"]) < 200_000))[:2]
    assert small
    doc = compute(ctx["engine"], ctx["store"], kg=False, tables_filter=lambda k: k in small)
    assert [t["key"] for t in doc["tables"]] == small
    for t in doc["tables"]:
        cov = ctx["tables"][t["key"]]["coverage"]
        assert t["rows"] == int(ctx["tables"][t["key"]]["row_count"])
        mapped = sum(v[0] for v in doc["table_countries"][t["key"]].values())
        assert mapped + t["rows_without_country"] == t["rows"]  # every row counted once: a country or none
        assert t["countries"] == cov.get("countries")  # same distinct ISO3 count as the table manifest
        assert t["licence_class"] in ("open", "non_commercial", "restricted", "unknown")
    assert len(doc["connectors"]) > 100  # every declared atlas source, loaded or not


def test_pending_observations_on_live_acquisitions(ctx: dict[str, Any]) -> None:
    from reva.v8.coverage.pending import load_pending
    from reva.v8.coverage.service import observations

    reg = load_pending()
    with ctx["engine"].connect() as c:
        obs = observations(c, reg)
    loads = [i for i in reg.items if i.type == "load" and i.check]
    assert loads and all(obs[i.id]["state"] in ("done", "partial", "not_seen") for i in loads)
    assert all(obs[i.id] is None for i in reg.items if i.type == "decision")
