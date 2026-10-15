"""Live checks for V2 step 1 (deselected by default; ``uv run pytest -m live tests/live``).

Structural only: the Comtrade preview accepts multi-reporter, multi-flow requests and refuses
more than one period (the documented preview limit the batch connector is built on); the
provider reference lists still match the archived crosswalk inputs or, if they changed, the
crosswalk rules still hold on the new bytes. Nothing is written to fixtures or registry files.
"""

from __future__ import annotations

import json

import pytest

from reva.adapters.connectors import country_crosswalk as cw
from reva.adapters.connectors.comtrade_batch import BASE_URL, ComtradeBatchConnector
from reva.adapters.connectors.worldbank_all import WorldBankAllConnector
from reva.evidence.runner import make_live_client
from reva.kernel.errors import RevaError
from reva.ports.connectors import FetchBudget
from reva.settings import get_settings

pytestmark = pytest.mark.live


async def test_comtrade_preview_accepts_multi_reporter_and_refuses_multi_period() -> None:
    c = ComtradeBatchConnector({"FRA": 251, "CHN": 156, "USA": 842, "DZA": 12}, total_years=(2023,),
                               bilateral_years=())
    http = make_live_client(c.describe(), get_settings())
    try:
        item = c.units()[0]
        raw = await c.fetch(item, http)
        (batch,) = list(c.parse(raw))
        report = c.validate(batch)
        assert report.outcome == "pass", report
        reporters = {d["reporterCode"] for d in batch.metadata["rows"]}
        assert {"156", "251", "842"} <= {str(r) for r in reporters}
        with pytest.raises(RevaError):
            await http.get(f"{BASE_URL}?period=2022,2023&reporterCode=251&cmdCode=2709&flowCode=M&partnerCode=0"
                           "&partner2Code=0&customsCode=C00&motCode=0&maxRecords=500")
    finally:
        await http.aclose()


async def test_worldbank_accepts_all_crosswalk_economies_in_one_page() -> None:
    wb = WorldBankAllConnector()
    http = make_live_client(wb.describe(), get_settings())
    try:
        item = next(i for i in wb.plan(None, FetchBudget()).items if i.key.startswith("series:NY.GDP"))
        raw = await wb.fetch(item, http)
        (batch,) = list(wb.parse(raw))
        assert not batch.follow_up and len(batch.expected_keys) == len(wb.countries) * 6
        assert wb.validate(batch).outcome == "pass"
    finally:
        await http.aclose()


async def test_live_reference_lists_still_satisfy_the_crosswalk_rules() -> None:
    http = make_live_client(ComtradeBatchConnector().describe(), get_settings())
    try:
        reporters = (await http.get(cw.SOURCES[1].url)).body
    finally:
        await http.aclose()
    doc = json.loads(reporters)
    by_code = {str(r["reporterCode"]): r for r in doc["results"]}
    assert by_code["490"]["isGroup"] is True or "nes" in by_code["490"]["text"]
    mapped = cw.comtrade_reporters()
    for key, code in mapped.items():
        assert str(code) in by_code, f"{key}: reporter {code} disappeared from the provider list"
