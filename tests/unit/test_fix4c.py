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
    assert r["basis"] == "statement_reference" and r["artifact_recorded_for_source_key"] == "noaa.ghcnm"


def test_artifact_rights_kept_for_the_source_that_recorded_them() -> None:
    row = {"source_key": "noaa.ghcnm", "raw_sha256": NE_SHA, "refs": [{"licence": "anything else"}]}
    r = fq._rights(row, _by_sha(NOAA_RIGHTS))
    assert r is not None and r["licence"].startswith("US Government work")
    # no statement licence: the artifact's (possibly more restrictive) rights stay
    r2 = fq._rights({"source_key": "naturalearth.admin1_10m", "raw_sha256": NE_SHA, "refs": []}, _by_sha(NOAA_RIGHTS))
    assert r2 is not None and r2["licence"].startswith("US Government work")
