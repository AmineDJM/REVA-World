"""Regression tests for the review V3b fixes beyond the reproductions (agent REVFIX2): SEC-3 (no build of an
old snapshot's query file in a request, live file pinned against eviction), LIC-1 (rights from the snapshot
manifest), LAB-1 (ILO vintage-year nowcast), HL-2 (extrapolated WDI footnote, derived density inputs)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from v3b_helpers import SynthAtlas

from reva.atlas import query as Q
from reva.atlas.snapshot import AtlasSnapshot, normalise_rights
from reva.atlas.source_order import load_headlines

WDI = "wb.wdi_social.SP.POP.TOTL"
ILO = "ilostat.une_2eap_sex_age_rt_a.UNE_2EAP_SEX_AGE_RT|sex=SEX_T|classif1=AGE_YTHADULT_YGE15"
ILO_FLAGS = {"estimate_type": "ilo_modelled", "ilo_source": "XA:2048"}


# --------------------------------------------------------------------------- SEC-3
def test_a_missing_query_file_is_not_built_when_building_is_not_allowed(tmp_path: Path) -> None:
    snap = AtlasSnapshot("e" * 64, "live", datetime(2026, 10, 16, tzinfo=UTC), ())
    with pytest.raises(Q.SnapshotNotMaterialised) as e:
        Q.AtlasQuery(snap, None, tmp_path / "c", build=False)  # type: ignore[arg-type]
    assert e.value.http_status == 409 and e.value.code == "snapshot_not_materialised"
    assert not list((tmp_path / "c").glob("atlas-*.duckdb"))


def test_an_existing_query_file_is_served_without_building(synth_atlas: SynthAtlas) -> None:
    synth_atlas.cell(WDI, "IND", 2020, "1", unit="persons")
    synth_atlas.query().close()
    snap = AtlasSnapshot(synth_atlas.snapshot_sha, "test_fixture", datetime(2026, 10, 16, tzinfo=UTC), ())
    q = Q.AtlasQuery(snap, None, synth_atlas.directory, build=False)  # type: ignore[arg-type]
    q.close()


def test_eviction_never_removes_the_pinned_live_file(tmp_path: Path) -> None:
    d = tmp_path / "c"
    d.mkdir(mode=0o700)
    names = [c * 64 for c in "0123"]
    for i, n in enumerate(names):  # oldest first
        p = d / f"atlas-{n}{Q.DB_SUFFIX}"
        p.write_bytes(b"x")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
    Q.pin_snapshot(names[0], d)
    Q._evict(d, keep=d / f"atlas-{names[3]}{Q.DB_SUFFIX}")
    left = sorted(p.name for p in d.glob("atlas-*.duckdb"))
    assert left == sorted(f"atlas-{n}{Q.DB_SUFFIX}" for n in (names[0], names[2], names[3]))


# --------------------------------------------------------------------------- LIC-1
def test_cell_rights_come_from_the_snapshot_manifest_table(synth_atlas: SynthAtlas) -> None:
    from v3b_helpers import TSHA

    synth_atlas.cell("ipu.parline.women_pct", "AGO", 2023, "38.2", unit="%")
    synth_atlas.query().close()
    rights = {"licence": "CC-BY-NC-SA-4.0", "non_commercial": True, "attribution": "IPU Parline"}
    table = {"artifact_sha256": TSHA, "dataset_key": "atlas.ipu.parline", "row_count": 1, "rights": rights}
    snap = AtlasSnapshot(synth_atlas.snapshot_sha, "test_fixture", datetime(2026, 10, 16, tzinfo=UTC), (table,))
    q = Q.AtlasQuery(snap, None, synth_atlas.directory)  # type: ignore[arg-type]
    try:
        (r,) = q._rows(f"SELECT {Q.CELL_SELECT} FROM cells x")
        cell = q._cell(r)
    finally:
        q.close()
    assert cell["rights"]["basis"] == "table_artifact" and cell["rights"]["attribution"] == "IPU Parline"
    assert cell["rights"]["commercial_use"] == "not_permitted" and cell["rights"]["share_alike"] is True
    assert Q.licence_summary([cell])[0]["source"] == "ipu"


def test_rights_normalisation_reads_explicit_terms_and_cc_ids() -> None:
    cow = normalise_rights(
        {"licence": "COW terms of use (not an open licence): users cannot charge for access", "open_licence": False},
        basis="table_artifact",
    )
    assert cow is not None and cow["restricted"] and cow["commercial_use"] == "restricted"
    wb = normalise_rights("CC-BY-4.0", basis="atlas_source_spec")
    assert wb is not None and not wb["restricted"] and wb["open_licence"] is True
    nd = normalise_rights({"licence": "CC-BY-ND-4.0"}, basis="table_artifact")
    assert nd is not None and nd["no_derivatives"] and nd["restricted"]
    assert normalise_rights(None, basis="x") is None


# --------------------------------------------------------------------------- LAB-1 / HL-2 rules
def test_ilo_vintage_year_is_a_nowcast_not_an_actual(synth_atlas: SynthAtlas) -> None:
    synth_atlas.cell(ILO, "FRA", 2024, "7.4", unit="%", flags={**ILO_FLAGS, "obs_status": "R"})
    synth_atlas.cell(ILO, "FRA", 2025, "7.542", unit="%", flags=ILO_FLAGS)
    synth_atlas.cell(ILO, "FRA", 2026, "7.654", unit="%", flags={**ILO_FLAGS, "projection": "after the vintage"})
    q = synth_atlas.query()
    try:
        reg = load_headlines()
        s = q.headline_series(reg, "reva.labour.unemployment_rate", "FRA")
        p = q.headline_series(reg, "reva.labour.unemployment_rate", "FRA", projections="include")
        m = q.headline_map(reg, "reva.labour.unemployment_rate", 2025)
    finally:
        q.close()
    assert [c["year"] for c in s["best"]] == [2024]
    by_year = {c["year"]: c for c in p["best"]}
    assert by_year[2025]["flags"]["estimate_kind"] == "nowcast" and by_year[2025]["projection"] is True
    assert by_year[2026]["projection"] is True and by_year[2024]["projection"] is False
    (miss,) = [c for c in m["countries_without_value"] if c["iso3"] == "FRA"]
    assert miss["consulted"][0]["status"] == "nowcast"


def test_wdi_extrapolated_footnote_and_derived_density_inputs_are_not_actuals(synth_atlas: SynthAtlas) -> None:
    foot = {"footnote": "Extrapolated assuming the same growth rate as previous 6 months."}
    synth_atlas.cell(WDI, "XKX", 2022, "1761985", unit="persons", flags=foot)
    synth_atlas.cell(WDI, "XKX", 2021, "1786038", unit="persons")
    # density derived from WDI population / WDI land area: the population input of 2024 is a WPP projection
    synth_atlas.cell(WDI, "IND", 2024, "1450935791", unit="persons")
    synth_atlas.cell("wb.wdi_social.AG.LND.TOTL.K2", "IND", 2024, "2973190", unit="sq. km")
    q = synth_atlas.query()
    try:
        reg = load_headlines()
        s = q.headline_series(reg, "reva.population.total", "XKX")
        d = q.headline_map(reg, "reva.population.density", 2024)
        dp = q.headline_map(reg, "reva.population.density", 2024, projections="include")
    finally:
        q.close()
    assert [c["year"] for c in s["best"]] == [2021]
    assert not [c for c in d["values"] if c["iso3"] == "IND"]
    (cell,) = [c for c in dp["values"] if c["iso3"] == "IND"]
    assert cell["method"] == "derived" and cell["projection"] is True
    assert cell["flags"]["estimate_kind"] == "projection_upstream"


def test_headline_cells_carry_rights_and_answers_list_licences(synth_atlas: SynthAtlas) -> None:
    synth_atlas.cell(WDI, "IND", 2020, "1396387127", unit="persons")
    q = synth_atlas.query()
    try:
        m: dict[str, Any] = q.headline_map(load_headlines(), "reva.population.total", 2020)
    finally:
        q.close()
    (cell,) = m["values"]
    assert cell["rights"]["basis"] == "atlas_source_spec" and cell["rights"]["open_licence"] is True
    assert m["licences"][0]["source"] == "wb"
