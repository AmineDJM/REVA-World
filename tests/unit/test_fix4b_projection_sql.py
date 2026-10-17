"""FIX4B (audit AUDIT3 M6): cells of a series published without a history boundary (``unknown_boundary``) are
never observations in atlas reads (``PROJECTION_SQL``) nor in the decision engine's baseline."""

from __future__ import annotations

import json

import duckdb

from reva.atlas.query import PROJECTION_SQL
from reva.predict.db import _is_projection


def _flag(flags: dict[str, object]) -> bool:
    con = duckdb.connect()
    con.execute("CREATE TABLE x (flags JSON)")
    con.execute("INSERT INTO x VALUES (?)", [json.dumps(flags)])
    return bool(con.execute(f"SELECT {PROJECTION_SQL} FROM x").fetchone()[0])  # type: ignore[index]


def test_unknown_boundary_is_not_an_observation() -> None:
    steo = {"estimate_kind": "unknown_boundary", "series_name": "Venezuela liquids", "frequency": "M"}
    assert _flag(steo)
    assert _flag({"estimate_kind": "published_history_boundary_unknown"})
    assert _flag({"estimate_kind": "projection"})
    assert not _flag({"series_name": "observed", "last_historical_period": "202609"})
    assert not _flag({})
    assert _is_projection(steo) and not _is_projection({"last_historical_period": "202609"})
