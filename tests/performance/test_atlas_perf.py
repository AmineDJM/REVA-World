"""Atlas query performance on 10 M synthetic cells (DuckDB over one snapshot).

10 synthetic atlas tables x 100 indicators x 250 countries x 45 years, ~10 % absent = ~10.1 M rows;
1,000 catalog concepts, each with a two-source order (a primary and a secondary table), with
~10 % of primary cells absent so the secondary source answers. Targets (warm, per query):
country dossier (all concepts, latest + series) < 1 s, world map (concept, year) < 500 ms.
Run: ``uv run pytest -m perf tests/performance/test_atlas_perf.py -s``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.atlas.catalog import Catalog, Concept, Series
from reva.atlas.query import AtlasQuery
from reva.atlas.snapshot import AtlasSnapshot
from reva.atlas.tables import ATLAS_SCHEMA, known_country_keys, to_arrow
from reva.simulation.datasets import parquet_bytes

pytestmark = pytest.mark.perf

N_TABLES, N_IND, N_YEARS = 10, 100, 45
RAW = "a" * 64


def synthetic_table(t: int, countries: list[str]) -> pl.DataFrame:
    years = pl.DataFrame({"year": list(range(1985, 1985 + N_YEARS))})
    geo = pl.DataFrame({"iso3": countries})
    ind = pl.DataFrame({"i": list(range(N_IND))})
    df = ind.join(geo, how="cross").join(years, how="cross").with_row_index("n")
    # ~10 % of the cells are not reported by this table (absent rows, never zero)
    df = df.filter(((pl.col("n") * 2654435761 + t * 977) % 100) >= 10)
    return df.select(
        pl.lit(f"src{t}").alias("source"),
        pl.lit("ds").alias("dataset"),
        pl.format("IND{}", pl.col("i")).alias("indicator"),
        pl.lit(None, pl.String).alias("concept_id"),
        pl.col("iso3"),
        pl.col("iso3").alias("provider_geo"),
        pl.lit(None, pl.String).alias("entity_qid"),
        pl.date(pl.col("year"), 1, 1).alias("period_start"),
        pl.date(pl.col("year") + 1, 1, 1).alias("period_end"),
        ((pl.col("n") * 7919 % 100000).cast(pl.String) + "." + (pl.col("n") % 10).cast(pl.String)).alias("value"),
        pl.lit(None, pl.String).alias("value_text"),
        pl.lit("%").alias("unit"),
        pl.lit("{}").alias("flags"),
        pl.lit(RAW).alias("raw_sha256"),
        pl.format('{{"row":{}}}', pl.col("n")).alias("selector"),
    ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]


def test_atlas_queries_on_ten_million_rows(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "objects")
    countries = sorted(known_country_keys())
    tables, rows = [], 0
    for t in range(N_TABLES):
        df = synthetic_table(t, countries)
        data = parquet_bytes(to_arrow(df.sort(["indicator", "provider_geo", "period_start"])))
        obj = store.put_bytes(data, "normalised", "application/vnd.apache.parquet")
        tables.append({"dataset_key": f"atlas.src{t}.ds", "dataset_id": str(t), "artifact_sha256": obj.sha256,
                       "row_count": df.height})  # fmt: skip
        rows += df.height
        del df
    snap = AtlasSnapshot("e" * 64, "test_fixture", datetime(2026, 10, 15, tzinfo=UTC), tuple(tables))
    concepts = {}
    for k in range(N_TABLES * N_IND):
        t, i = k % N_TABLES, k // N_TABLES
        concepts[f"perf.c{k}"] = Concept(
            f"perf.c{k}", {"fr": f"c{k}", "en": f"c{k}"}, "synthetic", "%", "perf", None,
            (Series(f"src{t}", "ds", f"IND{i}"), Series(f"src{(t + 5) % N_TABLES}", "ds", f"IND{i}")),
        )  # fmt: skip
    cat = Catalog(concepts=concepts)

    t0 = time.perf_counter()
    q = AtlasQuery(snap, store, tmp_path / "cache")
    build = time.perf_counter() - t0

    def timed(fn, n: int = 3) -> tuple[float, object]:  # type: ignore[no-untyped-def]
        out, best = None, float("inf")
        for _ in range(n):
            s = time.perf_counter()
            out = fn()
            best = min(best, time.perf_counter() - s)
        return best, out

    dossier_t, d = timed(lambda: q.dossier(cat, "FRA"))
    map_t, m = timed(lambda: q.map_values(cat, "perf.c123", 2010))
    series_t, _ = timed(lambda: q.series(cat, "perf.c123", "DZA"))
    cov_t, _ = timed(lambda: q.coverage_by_country(cat, "perf.c123"), 1)
    size_mb = q.path.stat().st_size / 1e6
    print(
        f"\natlas perf: rows={rows:,} build={build:.1f}s duckdb={size_mb:.0f}MB dossier={dossier_t * 1000:.0f}ms "
        f"map={map_t * 1000:.0f}ms series={series_t * 1000:.0f}ms coverage_by_country={cov_t * 1000:.0f}ms"
    )
    assert rows >= 10_000_000
    assert d["concepts_with_data"] == N_TABLES * N_IND  # type: ignore[index]
    # the secondary source fills most primary gaps; a cell neither reports stays absent
    assert 0.97 * len(countries) <= len(m["values"]) <= len(countries)  # type: ignore[index]
    assert len(m["by_source"]) == 2  # type: ignore[index]
    assert dossier_t < 1.0, dossier_t
    assert map_t < 0.5, map_t
