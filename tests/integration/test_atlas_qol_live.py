"""Live checks of the A-QOL atlas connectors (deselected by default: ``-m live``).

Small requests only: UNDP (3 files, ~2 MB), WHR (1 file), WID (archive tail + 1 country). They
confirm the provider files still have the layout the parsers expect; no database is written.
"""

from __future__ import annotations

from typing import Any

import pytest

from reva.adapters.connectors import atlas_undp, atlas_whr, atlas_wid
from reva.adapters.connectors.atlas_qol_common import fetch_all
from reva.adapters.http.safe_client import SafeHttpClient
from reva.atlas.ingest import AtlasRun
from reva.atlas.tables import validate_atlas_frame

pytestmark = pytest.mark.live


async def _validated(connector: Any) -> dict[str, Any]:
    spec = connector.describe()
    async with SafeHttpClient(
        allowed_hosts=spec.allowed_hosts, timeout_seconds=120, max_bytes=spec.max_response_bytes, proxy_mode="env"
    ) as client:
        raws = await fetch_all(connector, AtlasRun.offline(spec, client, origin="live"))  # fetch only, no archive
    out = {}
    for b in connector.build(raws):
        f, unmapped = validate_atlas_frame(
            b.frame, source=b.source, dataset=b.dataset, raw_sha256s=b.raw_sha256s, geo_scheme=b.geo_scheme
        )
        out[b.dataset] = (b, f, unmapped)
    return out


async def test_undp_live_layout() -> None:
    out = await _validated(atlas_undp.UndpHdrConnector())
    assert out["hdr25"][1]["iso3"].n_unique() >= 190
    assert out["gmpi2026"][2] == []  # every MPI country name maps


async def test_whr_live_layout() -> None:
    out = await _validated(atlas_whr.WhrConnector())
    assert out["whr26_fig21"][1]["iso3"].n_unique() >= 140


async def test_wid_live_one_country() -> None:
    out = await _validated(atlas_wid.WidConnector(countries=["FR"]))
    _b, f, _ = out["wid_key"]
    assert set(f["iso3"].unique().to_list()) == {"FRA"} and f.height > 500
