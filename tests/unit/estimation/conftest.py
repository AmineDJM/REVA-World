"""A tiny atlas query file (same tables/columns as reva.atlas.query's DuckDB file, format atlas-duckdb/6) with
synthetic cells, for SnapshotPanels / replication / run tests without the multi-GB live file."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path

import duckdb
import pytest

FAKE_SNAPSHOT = "ab" * 32


def build_atlas_file(
    directory: Path,
    series: dict[tuple[str, str, str], Iterable[tuple[str, int, str | None]]],
    *,
    snapshot: str = FAKE_SNAPSHOT,
    quarterly: dict[tuple[str, str, str], Iterable[tuple[str, int, int, str]]] | None = None,
) -> Path:
    """``series``: (source, dataset, indicator) -> [(iso3, year, value text)]; annual calendar-year cells."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"atlas-{snapshot}-v6.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE meta AS SELECT 'atlas-duckdb/6' AS format, ? AS snapshot_sha256, 'test_fixture' AS origin, "
        "'2026-10-17T00:00:00+00:00' AS cutoff",
        [snapshot],
    )
    con.execute("CREATE TABLE series(sid INTEGER, source VARCHAR, dataset VARCHAR, indicator VARCHAR)")
    con.execute(
        """CREATE TABLE cells_s(sid INTEGER, iso3 VARCHAR, year INTEGER, has_value BOOLEAN, value VARCHAR,
           value_text VARCHAR, unit VARCHAR, raw_sha256 VARCHAR, table_sha VARCHAR, period_start DATE,
           period_end DATE)"""
    )
    keys = sorted(set(series) | set(quarterly or {}))
    for sid, k in enumerate(keys, start=1):
        con.execute("INSERT INTO series VALUES (?, ?, ?, ?)", [sid, *k])
        raw = hashlib.sha256(("raw" + ".".join(k)).encode()).hexdigest()
        tsha = hashlib.sha256(("tbl" + ".".join(k)).encode()).hexdigest()
        for iso3, year, value in series.get(k, []):
            con.execute(
                "INSERT INTO cells_s VALUES (?, ?, ?, ?, ?, NULL, 'u', ?, ?, make_date(?, 1, 1), make_date(?, 1, 1))",
                [sid, iso3, year, value is not None, value, raw, tsha, year, year + 1],
            )
        for iso3, year, q, value in (quarterly or {}).get(k, []):
            m = 3 * (q - 1) + 1
            con.execute(
                """INSERT INTO cells_s VALUES (?, ?, NULL, true, ?, NULL, 'u', ?, ?, make_date(?, ?, 1),
                   make_date(?, ?, 1) + INTERVAL 3 MONTH)""",
                [sid, iso3, value, raw, tsha, year, m, year, m],
            )
    con.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + ".sha256").write_text(
        json.dumps({"format": "atlas-duckdb/6", "snapshot_sha256": snapshot, "sha256": digest})
    )
    return path


@pytest.fixture()
def atlas_file(tmp_path: Path) -> Callable[..., Path]:
    def make(series: dict[tuple[str, str, str], Iterable[tuple[str, int, str | None]]], **kw: object) -> Path:
        d = tmp_path / "atlas-cache"
        build_atlas_file(d, series, **kw)  # type: ignore[arg-type]
        return d

    return make
