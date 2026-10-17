"""FIX4C (AUDIT3 M8, L1, L2, L4, L5, L7, L9, section 5): offline checks of the fixes."""

from __future__ import annotations

from typing import Any

from reva.kg import family_query as fq

NE_SHA = "e" * 64
NOAA_RIGHTS = {
    "licence": "US Government work (NOAA NCEI), public domain in the US; cite GHCN-M v4",
    "source_key": "noaa.ghcnm",
}


def _by_sha(rights: dict[str, Any]) -> dict[str, Any]:
    from reva.atlas.snapshot import normalise_rights

    n = normalise_rights(rights, basis="raw_artifact_rights")
    assert n is not None
    return {NE_SHA: {**n, "recorded_for_source_key": rights["source_key"]}}


def test_statement_licence_wins_when_artifact_was_recorded_for_another_source() -> None:
    row = {
        "source_key": "naturalearth.admin1_10m",
        "raw_sha256": NE_SHA,
        "refs": [{"licence": "Public domain (Natural Earth)", "raw_sha256": NE_SHA}],
    }
    r = fq._rights(row, _by_sha(NOAA_RIGHTS))
    assert r is not None and r["licence"] == "Public domain (Natural Earth)"
    assert r["basis"] == "statement_reference (raw artifact rights recorded for noaa.ghcnm)"


def test_artifact_rights_kept_for_the_source_that_recorded_them() -> None:
    row = {"source_key": "noaa.ghcnm", "raw_sha256": NE_SHA, "refs": [{"licence": "anything else"}]}
    r = fq._rights(row, _by_sha(NOAA_RIGHTS))
    assert r is not None and r["licence"].startswith("US Government work") and "recorded_for_source_key" not in r
    # no statement licence: the artifact's (possibly more restrictive) rights stay
    r2 = fq._rights({"source_key": "naturalearth.admin1_10m", "raw_sha256": NE_SHA, "refs": []}, _by_sha(NOAA_RIGHTS))
    assert r2 is not None and r2["licence"].startswith("US Government work")


def test_fishstat_not_significant_cells_are_flagged() -> None:
    """AUDIT3 L4: FAO 'N' cells are published as 0 but are 'not a real zero' (< 0.5 unit each)."""
    import json
    from collections import Counter
    from decimal import Decimal

    from reva.adapters.connectors import atlas_fishstat as f

    mixed = f.Cell(total=Decimal("12"), n_values=3, symbols=Counter({"N": 2}))
    all_n = f.Cell(total=Decimal("0"), n_values=2, symbols=Counter({"N": 2}))
    plain = f.Cell(total=Decimal("5"), n_values=1)
    rows = f.atlas_rows(
        {("FRA", "capture.mg_pisces.tonnes", 2020): mixed, ("ISL", "capture.mg_pisces.tonnes", 2020): all_n,
         ("NOR", "capture.mg_pisces.tonnes", 2020): plain},
        raw_sha256="f" * 64,
    )  # fmt: skip
    by = {r["provider_geo"]: (r["value"], json.loads(r["flags"])) for r in rows}
    v, fl = by["FRA"]
    assert v == "12" and fl["includes_not_significant"] and fl["not_significant_cells"] == 2
    assert fl["understated_by_less_than"] == "1" and "all_cells_not_significant" not in fl
    assert fl["not_significant_definition"].startswith("Not significant (<0.5)")
    v, fl = by["ISL"]
    assert v == "0" and fl["all_cells_not_significant"] is True
    assert "includes_not_significant" not in by["NOR"][1]
