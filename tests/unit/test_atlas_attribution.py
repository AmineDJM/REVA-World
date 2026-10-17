"""V4CLIMATE: Carbon Brief attribution-study map -> KG extremes, T2 claims (DOI only), atlas counts (offline).

Fixtures: tests/fixtures/source/atlas.attribution_cb — derived excerpts (cut_excerpt.py) of the real page and CSV
captured 2026-10-17 (CC BY-NC-ND 4.0, Carbon Brief; unadapted rows, test use).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from reva.adapters.connectors import atlas_attribution as at
from reva.adapters.connectors.atlas_geo_common import load_country_qids
from reva.atlas.tables import validate_atlas_frame
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError

T = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def bodies() -> dict[str, bytes]:
    http = FixtureHttp(at.NAME)
    return {u: asyncio.run(http.get(u)).body for u in (at.PAGE_URL, at.CSV_URL)}


@pytest.fixture(scope="module")
def rows(bodies: dict[str, bytes]) -> list[at.Row]:
    return at.parse_csv(bodies[at.CSV_URL], min_rows=10)


def test_page_meta_and_licence_guard(bodies: dict[str, bytes]) -> None:
    assert at.page_meta(bodies[at.PAGE_URL]) == {"last_updated": "19 March 2026"}
    with pytest.raises(SourceResponseError, match="licence"):
        at.page_meta(bodies[at.PAGE_URL].replace(b"by-nc-nd", b"by"))


def test_periods_and_dois(rows: list[at.Row]) -> None:
    assert at.period("2010") == (2010, 2010)
    assert at.period("2013-14") == (2013, 2014) and at.period("1999-2000") == (1999, 2000)
    assert at.period("2008–16") == (2008, 2016) and at.period("Trend") is None and at.period("2010s") is None
    r = next(x for x in rows if x["Name"] == "Russia summer heatwave, 2010" and "Dole" in x["Citation"])
    assert r["Classification"] == "No discernible human influence"
    _doi, basis = at.doi_of(r)
    assert basis in ("citation", "link", None)


def test_claims_tiers_polarity_and_exclusions(rows: list[at.Row]) -> None:
    cl, rep = at.claims(rows, "c" * 64)
    assert cl and all(c.tier == "T2" and c.citation and c.citation["doi"].startswith("10.") for c in cl)
    assert all(c.cause_id == at.CAUSE_ID and c.relation == "contributing_factor" for c in cl)
    by_class = {c.scope["classification"]: c for c in cl}
    if "More severe or more likely to occur" in by_class:
        assert by_class["More severe or more likely to occur"].polarity == "promotes"
    if "No discernible human influence" in by_class:
        c = by_class["No discernible human influence"]
        assert c.stance == "denies" and c.polarity is None
    assert "Insufficient data/inconclusive" not in by_class
    n_inconclusive = sum(1 for r in rows if r["Classification"] == "Insufficient data/inconclusive")
    assert rep.get("no_claim_inconclusive", 0) == n_inconclusive
    no_doi = [r for r in rows if r["Classification"] != "Insufficient data/inconclusive" and at.doi_of(r)[0] is None]
    assert rep.get("no_claim_without_doi", 0) == len(no_doi)
    assert len(cl) + len(no_doi) + n_inconclusive == len(rows)
    # the study's summary statement is the mechanism, verbatim
    assert all(c.mechanism is None or c.mechanism in {r["Summary statement"] for r in rows} for c in cl)


def test_kg_items_and_atlas_counts(rows: list[at.Row]) -> None:
    f = SimpleNamespace(sha256="c" * 64, retrieved_at=T)
    p = SimpleNamespace(sha256="d" * 64, retrieved_at=T)
    ents, sts, _rep = at.kg_items(rows, f, p, {"last_updated": "19 March 2026"}, load_country_qids())
    kinds = {e.kind for e in ents}
    assert kinds <= {"climate_driver", "extreme_event", "climate_trend"} and "extreme_event" in kinds
    eth = at.event_id("Ethiopia drought, 2015")
    findings = [s for s in sts if s.subject_qid == eth and s.property_pid == "cbattr:finding"]
    assert len(findings) == sum(1 for r in rows if r["Name"] == "Ethiopia drought, 2015") >= 2
    assert any(s.property_pid == "cbattr:country" and s.object_qid and s.object_qid.startswith("Q") for s in sts)
    assert len({s.statement_id for s in sts}) == len(sts)
    rb, _arep = at.atlas_counts(rows, "c" * 64)
    fr, _ = validate_atlas_frame(rb.frame(), source=at.ATLAS_SOURCE, dataset=at.DATASET, raw_sha256s=["c" * 64],
                                 geo_scheme="iso3")  # fmt: skip
    assert fr.height == len(rb.rows) > 0
    fl = json.loads(rb.rows[0]["flags"])
    assert fl["derived"] == "count" and fl["non_commercial"] is True and fl["no_derivatives"] is True


def test_link_doi_tail_is_stripped() -> None:
    r = at.Row(
        1, {"Citation": "no doi here", "Link": "http://onlinelibrary.wiley.com/doi/10.1002/grl.50657/abstract"}, "k"
    )
    assert at.doi_of(r) == ("10.1002/grl.50657", "link")
    r2 = at.Row(2, {"Citation": "X et al., 2020, doi:10.1029/2025GL115880", "Link": ""}, "k2")
    assert at.doi_of(r2) == ("10.1029/2025GL115880", "citation")
