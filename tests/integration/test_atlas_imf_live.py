"""Live IMF SDMX checks (agent A-IMF; deselected by default: ``pytest -m live``).

Three small requests to api.imf.org: WEO (FRA, current vintage), BOP goods balance (FRA) and the IMTS
country codelist. Checks that the current WEO vintage is labelled, that every year after the declared
latest actual year is flagged as a projection, and that the IMF code list still uses KOS/WBG.
"""

from __future__ import annotations

import json

import pytest

from reva.adapters.connectors import atlas_imf as m
from reva.adapters.http.safe_client import SafeHttpClient
from reva.ports.connectors import FetchItem

pytestmark = pytest.mark.live


def _item(dataset: str, agency: str, flow: str, key: str) -> FetchItem:
    return m.FlowQuery(dataset, agency, flow, key, 2020).fetch_item()


async def test_live_weo_vintage_projections_bop_and_codelist() -> None:
    async with SafeHttpClient(allowed_hosts=[m.HOST], timeout_seconds=120, proxy_mode="env") as http:
        weo = await m.fetch(_item("weo", "IMF.RES", "WEO", "FRA.NGDP_RPCH+BCA_NGDPD.A"), http)
        bop = await m.fetch(_item("bop", "IMF.STA", "BOP", "FRA.NETCD_T.G.USD.A"), http)
        cl = await m.fetch(m.imts_codelist_item(), http)
    frame, st = m.parse_weo(m.parse_sdmx(weo.body), "0" * 64)
    v = st.notes["vintage"]
    assert v["vintage_label"].startswith("WEO ") and v["flow"].startswith("IMF.RES:WEO(")
    seen_projection = False
    for r in frame.to_dicts():
        f = json.loads(r["flags"])
        la = f.get("latest_actual")
        assert la and la.isdigit()
        assert (f.get("estimate_kind") == "projection") == (r["period_start"].year > int(la))
        seen_projection |= f.get("estimate_kind") == "projection"
    assert seen_projection
    goods, _ = m.parse_bop(m.parse_sdmx(bop.body), "0" * 64)
    assert goods.height >= 3 and set(goods["indicator"].to_list()) == {"G|entry=NETCD_T"}
    names = m.parse_codelist(cl.body)
    assert names["KOS"].startswith("Kosovo") and names["WBG"] == "West Bank and Gaza" and names["G001"] == "World"
