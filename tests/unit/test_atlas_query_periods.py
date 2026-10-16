"""Atlas year view (ATLASOPS, review V3 ATLAS-2): which cells a calendar year shows, and BCE period text.

No database: a synthetic table is written to a filesystem store and pinned by an in-memory snapshot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.atlas.catalog import Catalog, Concept, Series
from reva.atlas.query import AtlasQuery
from reva.atlas.snapshot import AtlasSnapshot
from reva.atlas.tables import ATLAS_SCHEMA, to_arrow
from reva.simulation.datasets import parquet_bytes

RAW = "b" * 64


def _rows() -> list[dict]:
    def r(geo: str, start: tuple[int, int, int], end: tuple[int, int, int], value: str, **flags: object) -> dict:
        return {"geo": geo, "s": start, "e": end, "value": value, "flags": json.dumps(flags, sort_keys=True)}

    return [
        r("FRA", (2024, 6, 1), (2024, 6, 2), "1"),  # point in time ("as of" date)
        r("FRA", (2023, 1, 1), (2024, 1, 1), "2"),  # calendar year
        r("DEU", (2024, 3, 1), (2024, 4, 1), "3"),  # a month: per-source only, never "absent"
        r("IND", (2024, 4, 1), (2025, 4, 1), "4", label_year=2024),  # fiscal year labelled 2024 by the provider
        r("ITA", (-10000, 1, 1), (-9999, 1, 1), "5"),  # 10001 BCE (astronomical -10000)
        r("ESP", (2025, 1, 1), (2025, 7, 1), "6"),  # semester S1
        r("ESP", (2025, 7, 1), (2026, 1, 1), "7"),  # semester S2: the year view shows the latest
    ]


def _query(tmp_path: Path) -> tuple[AtlasQuery, Catalog]:
    base = pl.DataFrame(_rows())
    df = base.select(
        pl.lit("src").alias("source"),
        pl.lit("ds").alias("dataset"),
        pl.lit("IND").alias("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.col("geo").alias("iso3"),
        pl.col("geo").alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(pl.col("s").list.get(0), pl.col("s").list.get(1), pl.col("s").list.get(2)).alias("period_start"),
        pl.date(pl.col("e").list.get(0), pl.col("e").list.get(1), pl.col("e").list.get(2)).alias("period_end"),
        pl.col("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        pl.lit("u").alias("unit"),
        pl.col("flags"),
        pl.lit(RAW).alias("raw_sha256"),
        pl.format('{{"row":"{}"}}', pl.col("value")).alias("selector"),
    ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
    df = df.sort(["indicator", "provider_geo", "period_start"])
    store = FilesystemArtifactStore(tmp_path / "objects")
    obj = store.put_bytes(parquet_bytes(to_arrow(df)), "normalised", "application/vnd.apache.parquet")
    table = {"dataset_key": "atlas.src.ds", "dataset_id": "1", "artifact_sha256": obj.sha256, "row_count": df.height}
    snap = AtlasSnapshot(obj.sha256[::-1], "test_fixture", datetime(2026, 10, 16, tzinfo=UTC), (table,))
    cat = Catalog(
        concepts={"t.c": Concept("t.c", {"fr": "c", "en": "c"}, "d", "u", "t", None, (Series("src", "ds", "IND"),))}
    )
    return AtlasQuery(snap, store, tmp_path / "cache"), cat


def test_year_view_attaches_points_fiscal_years_and_semesters(tmp_path: Path) -> None:
    q, cat = _query(tmp_path)
    m24 = q.map_values(cat, "t.c", 2024)
    by = {c["iso3"]: c for c in m24["values"]}
    assert by["FRA"]["value"] == "1" and by["FRA"]["period_basis"] == "point_in_time"
    assert by["IND"]["value"] == "4" and by["IND"]["period_start"] == "2024-04-01"  # provider label_year
    assert by["IND"]["period_basis"] == "twelve_month_window"
    assert "DEU" not in by and m24["countries_with_other_period_cells"] == ["DEU"]  # monthly: not absent
    assert "DEU" not in m24["countries_without_value"]
    m25 = q.map_values(cat, "t.c", 2025)
    esp = {c["iso3"]: c for c in m25["values"]}["ESP"]
    assert (esp["value"], esp["period_start"], esp["period_basis"]) == ("7", "2025-07-01", "sub_annual")


def test_bce_periods_keep_five_digit_years(tmp_path: Path) -> None:
    q, cat = _query(tmp_path)
    (c,) = q.map_values(cat, "t.c", -10000)["values"]
    assert (c["year"], c["period_start"], c["period_end"]) == (-10000, "-10000-01-01", "-9999-01-01")
