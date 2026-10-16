"""Atlas query layer (reva.atlas.query): defects found by the V3 review (docs/dev/review_v3.md, ATLAS-*)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl
import pytest

from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.atlas.catalog import Catalog, Concept, Series
from reva.atlas.query import AtlasQuery
from reva.atlas.snapshot import AtlasSnapshot
from reva.atlas.tables import ATLAS_SCHEMA, to_arrow
from reva.kernel.errors import IntegrityFailure
from reva.simulation.datasets import parquet_bytes

RAW = "a" * 64


def _row(source: str, dataset: str, ind: str, iso3: str, start: date, end: date, value: str) -> dict:
    return {
        "source": source,
        "dataset": dataset,
        "indicator": ind,
        "concept_id": None,
        "iso3": iso3,
        "provider_geo": iso3,
        "entity_qid": None,
        "period_start": start,
        "period_end": end,
        "value": value,
        "value_text": None,
        "unit": "u",
        "flags": "{}",
        "raw_sha256": RAW,
        "selector": json.dumps({"row": f"{iso3}{start}"}),
    }


def _snapshot(tmp_path: Path, rows: list[dict], key: str) -> tuple[AtlasSnapshot, FilesystemArtifactStore]:
    store = FilesystemArtifactStore(tmp_path / "objects")
    df = pl.DataFrame(rows, schema=ATLAS_SCHEMA).sort(["indicator", "provider_geo", "period_start"])
    obj = store.put_bytes(parquet_bytes(to_arrow(df)), "normalised", "application/vnd.apache.parquet")
    table = {"dataset_key": key, "dataset_id": "1", "artifact_sha256": obj.sha256, "row_count": df.height}
    snap = AtlasSnapshot(obj.sha256[::-1], "test_fixture", datetime(2026, 10, 15, tzinfo=UTC), (table,))
    return snap, store


def _catalog(cid: str, series: Series) -> Catalog:
    return Catalog(concepts={cid: Concept(cid, {"fr": cid, "en": cid}, "d", "u", "t", None, (series,))})


def test_tampered_duckdb_cache_is_refused(tmp_path: Path) -> None:
    rows = [_row("src", "ds", "IND", "FRA", date(2020, 1, 1), date(2021, 1, 1), "10")]
    snap, store = _snapshot(tmp_path, rows, "atlas.src.ds")
    cat = _catalog("t.c", Series("src", "ds", "IND"))
    cache = tmp_path / "cache"
    q = AtlasQuery(snap, store, cache)
    assert q.map_values(cat, "t.c", 2020)["values"][0]["value"] == "10"
    q.close()
    con = duckdb.connect(str(q.path))  # anyone who can write the cache directory (default: the shared /tmp)
    con.execute("UPDATE cells SET value = '999999'")
    con.execute("UPDATE cells_s SET value = '999999'")
    con.close()
    with pytest.raises(IntegrityFailure):
        AtlasQuery(snap, store, cache).map_values(cat, "t.c", 2020)


@pytest.mark.parametrize(
    ("label", "start", "end"),
    [
        # WHR 2026 Figure 2.1: three-year survey window ending in 2025 (atlas_whr period [Y-2, Y+1))
        ("whr_three_year_window", date(2023, 1, 1), date(2026, 1, 1)),
        # Eurostat earn_mw_cur second semester (atlas_wages_gapfill period [Y-07-01, Y+1-01-01))
        ("eurostat_semester", date(2025, 7, 1), date(2026, 1, 1)),
    ],
)
def test_non_calendar_cells_are_not_reported_as_absent(tmp_path: Path, label: str, start: date, end: date) -> None:
    rows = [_row("src", "ds", "IND", "FIN", start, end, "7.7")]
    snap, store = _snapshot(tmp_path, rows, "atlas.src.ds")
    cat = _catalog("t.c", Series("src", "ds", "IND"))
    q = AtlasQuery(snap, store, tmp_path / "cache")
    m = q.map_values(cat, "t.c", 2025)
    cov = q.coverage(cat)[0]
    # The source reports Finland for a period ending in 2025: the map must not call Finland "absent" and the
    # concept-level coverage must not be zero while its only source covers one country.
    assert "FIN" not in m["countries_without_value"], label
    assert cov["countries"] == cov["by_source"][0]["countries"] == 1, label


def test_dossier_series_points_carry_their_unit(tmp_path: Path) -> None:
    rows = [
        {**_row("src", "ds", "IND", "AUT", date(1970, 1, 1), date(1971, 1, 1), "4590"), "unit": "ATS per month"},
        {**_row("src", "ds", "IND", "AUT", date(2001, 1, 1), date(2002, 1, 1), "2254"), "unit": "EUR per month"},
    ]
    snap, store = _snapshot(tmp_path, rows, "atlas.src.ds")
    cat = _catalog("t.c", Series("src", "ds", "IND"))
    d = AtlasQuery(snap, store, tmp_path / "cache").dossier(cat, "AUT")
    points = d["domains"][0]["concepts"][0]["series"]
    assert [p.get("unit") for p in points] == ["ATS per month", "EUR per month"]
