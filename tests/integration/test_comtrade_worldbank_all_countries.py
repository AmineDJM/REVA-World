"""V2 step 1: crosswalk entities are seeded; the all-country Comtrade/World Bank connectors
ingest captured fixtures (small reporter subsets, origin=test_fixture) into a release where
gaps stay typed and unreviewed HS editions stay out.

Fixture subset: FRA, DZA, CHN, USA, CHE, IRQ, NLD, ATG (2023). Facts in the captured bytes
(tests/fixtures/source/un.comtrade.public_batch): DZA returned no rows; IRQ exports are
reported under H2 and ATG imports under H3 (outside the reviewed H4-H6 mapping).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors.comtrade_batch import ComtradeBatchConnector
from reva.adapters.connectors.worldbank_all import WorldBankAllConnector
from reva.cli.seed import entity_id
from reva.evidence.runner import FixtureHttp, run_connector

pytestmark = pytest.mark.db

CT_SUBSET = {"FRA": 251, "DZA": 12, "CHN": 156, "USA": 842, "CHE": 757, "IRQ": 368, "NLD": 528, "ATG": 28}
WB_SUBSET = ["FRA", "DZA", "CHN", "USA", "CHE", "IRQ", "NLD", "ATG", "XKX", "PRI"]


def fixture_comtrade() -> ComtradeBatchConnector:
    return ComtradeBatchConnector(CT_SUBSET, total_years=(2023,), bilateral_years=(2023,), bilateral_batch=4)


def fixture_worldbank() -> WorldBankAllConnector:
    return WorldBankAllConnector(WB_SUBSET)


def _alias(conn: Any, provider: str, key: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        text("SELECT entity_id, confidence_kind, mapping_version FROM world.alias WHERE provider=:p "
             "AND external_key=:k ORDER BY recorded_at DESC"), {"p": provider, "k": key}).mappings()]


def test_seed_creates_crosswalk_entities_with_provider_declared_aliases(db) -> None:
    with db.owner.connect() as conn:
        che = _alias(conn, "comtrade_reporter", "757")
        assert che and che[0]["entity_id"] == entity_id("CHE")
        assert che[0]["confidence_kind"] == "provider_declared"
        assert _alias(conn, "iso3166_alpha3", "CHE")[0]["entity_id"] == entity_id("CHE")
        assert _alias(conn, "worldbank", "XKX")[0]["entity_id"] == entity_id("XKX")
        assert _alias(conn, "iso3166_alpha3", "XKX") == []  # Kosovo: World Bank code, not ISO 3166
        # Composite / group / historical reporter codes are never aliased to a country.
        for code in ("490", "97", "975", "810", "736"):
            assert _alias(conn, "comtrade_reporter", code) == [], code
        # Reviewed entities keep their reviewed aliases only (nothing added from the crosswalk).
        fra = _alias(conn, "comtrade_reporter", "251")
        assert [a["confidence_kind"] for a in fra] == ["reviewed_exact"]
        n = conn.execute(text("SELECT count(DISTINCT entity_id) FROM world.alias WHERE provider='iso3166_alpha3'"
                              )).scalar_one()
    assert n == 248


async def test_all_country_fixture_ingest_keeps_gaps_typed(db) -> None:
    from reva.belief.publication import publish_release
    from reva.belief.snapshots import estimates
    from reva.kernel.context import Origin
    from reva.kernel.time import utc_now

    for c in (fixture_worldbank(), fixture_comtrade()):
        rep = await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)
        assert all(o.status in ("published", "unchanged") for o in rep.outcomes), rep.as_dict()
    # Lineage heads of the shared source, whichever connector wrote them: the batch connector
    # shares lineage (source, origin, logical key) with `un.comtrade.public_exact`, so an
    # identical row that an earlier module ingested through the exact connector stays the head
    # ("unchanged") and is not re-inserted under the batch connector version.
    with db.owner.connect() as conn:
        rows = {r["lk"]: dict(r) for r in conn.execute(text(
            "SELECT DISTINCT ON (o.logical_key) o.logical_key AS lk, o.value, o.source_flags, "
            "evidence.observation_is_eligible(o.id, clock_timestamp()) AS eligible "
            "FROM evidence.observation o JOIN evidence.source s ON s.id = o.source_id "
            "WHERE s.source_key = 'un.comtrade.public_preview' AND o.origin = 'test_fixture' "
            "ORDER BY o.logical_key, o.revision_no DESC")).mappings()}
    dza = rows["comtrade:HS2709:M:12:0:2023:primaryValue"]
    assert dza["value"]["kind"] == "missing" and dza["source_flags"]["provider_returned_no_rows"] is True
    assert dza["eligible"] is True  # typed Missing is admitted (admit_unclassified_missing)
    irq = rows["comtrade:HS2709:X:368:0:2023:primaryValue"]
    assert irq["source_flags"]["classificationCode"] == "H2" and irq["value"]["kind"] == "decimal"
    assert irq["eligible"] is False  # stored, but outside the reviewed H4-H6 scope
    assert rows["comtrade:HS2709:M:757:0:2023:primaryValue"]["eligible"] is True
    # Bilateral partner codes are kept, composite partners included as provider codes.
    bilateral = [k for k in rows if ":0:2023:" not in k and k.endswith(":2023:primaryValue")]
    assert bilateral and all(k.split(":")[4] != "0" for k in bilateral)

    rel = publish_release(db.worker, db.store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test")
    with db.owner.connect() as conn:
        def est(key: str, concept: str) -> list[dict[str, Any]]:
            return [e for e in estimates(conn, rel.release_id, subject_id=entity_id(key), concept_id=concept)
                    if e["temporal_extent"].get("start", "").startswith("2023")]

        che_m = est("CHE", "trade.hs2709.import_value.world_total")
        assert len(che_m) == 1 and che_m[0]["value"]["kind"] == "decimal"
        assert est("IRQ", "trade.hs2709.export_value.world_total") == []  # H2: never selected
        dza_m = est("DZA", "trade.hs2709.import_value.world_total")
        assert len(dza_m) == 1 and dza_m[0]["value"]["kind"] == "missing"
        che_g = est("CHE", "economics.gdp.nominal_current_usd")
        assert len(che_g) == 1 and che_g[0]["value"]["kind"] == "decimal"
        assert est("XKX", "economics.gdp.nominal_current_usd")  # WB-only economy keyed by its WB code
