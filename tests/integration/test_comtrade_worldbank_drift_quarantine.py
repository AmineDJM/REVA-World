"""Drifted provider payloads are quarantined by the real ingest path, never published.

Synthetic drifted bodies from tests/fixtures/repair are served for one request through
FixtureHttp; every other request gets its captured fixture. The drifted acquisition must
be stored in the quarantine namespace with the validation blockers recorded and no
observation, while the captured requests of the same run still publish.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors.comtrade import ComtradeConnector
from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.adapters.http.safe_client import request_fingerprint
from reva.evidence.runner import FixtureHttp, run_connector

pytestmark = pytest.mark.db

REPAIR = Path(__file__).parents[1] / "fixtures" / "repair"
CASES = {c["case"]: c for c in json.loads((REPAIR / "manifest.json").read_text())}


def _quarantined(db: Any, raw_sha256: str) -> tuple[set[str], int]:
    with db.owner.connect() as c:
        rows = (
            c.execute(
                text("SELECT a.id, a.response_metadata FROM evidence.acquisition a WHERE a.raw_sha256 = :s"),
                {"s": raw_sha256},
            )
            .mappings()
            .all()
        )
        assert rows
        codes = {
            f["code"]
            for r in rows
            for q in r["response_metadata"]["quality"]
            for f in q["findings"]
            if f["severity"] == "blocker"
        }
        n_obs = c.execute(
            text("SELECT count(*) FROM evidence.observation WHERE acquisition_id = ANY(:ids)"),
            {"ids": [r["id"] for r in rows]},
        ).scalar_one()
    return codes, n_obs


@pytest.mark.parametrize(
    ("case", "connector", "item_key", "blocker"),
    [
        (
            "ct_renamed_field",
            lambda: ComtradeConnector(reporters=("FRA",), total_years=(2023,), bilateral_years=()),
            "total:FRA:M:2023",
            "field_absent",
        ),
        (
            "ct_value_type_change",
            lambda: ComtradeConnector(reporters=("FRA",), total_years=(2023,), bilateral_years=()),
            "total:FRA:M:2023",
            "value_type_drift",
        ),
        (
            "ct_classification_change",
            lambda: ComtradeConnector(reporters=("FRA",), total_years=(2023,), bilateral_years=()),
            "total:FRA:M:2023",
            "classification_unreviewed",
        ),
        ("wb_unit_text_and_scale", WorldBankConnector, "series:SP.POP.TOTL:p1", "unit_drift"),
    ],
)
async def test_drifted_payload_is_quarantined_not_published(db, case, connector, item_key, blocker) -> None:
    c = CASES[case]
    conn = connector()
    body = (REPAIR / c["file"]).read_bytes()
    http = FixtureHttp(c["connector"], overrides={request_fingerprint("GET", c["url"]): body})
    rep = await run_connector(conn, http, db.store, db.worker, persist_cursor=False)
    by_item = {o.item_key: o for o in rep.outcomes}
    assert by_item[item_key].status == "quarantined", rep.as_dict()
    others = [o for k, o in by_item.items() if k != item_key]
    assert others and all(o.status in ("published", "unchanged") for o in others), rep.as_dict()
    codes, n_obs = _quarantined(db, str(by_item[item_key].raw_sha256))
    assert blocker in codes and n_obs == 0
