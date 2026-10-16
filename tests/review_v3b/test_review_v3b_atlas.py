"""Review V3b — licence restrictions in atlas answers (LIC-1).

IPU Parline (CC BY-NC-SA 4.0, non-commercial, share-alike), V-Dem (CC BY-SA), TI CPI (CC BY-ND), Constitute /
CCP (commercial use prohibited) are in the live dev snapshot and served by /v1/atlas/*, the dossier and
/v1/ask. A cell (``AtlasQuery._cell``: value, unit, flags, provenance) names no licence unless the connector
happened to copy it into its own flags (Constitute, CCP, SSA, Gapminder do; IPU, V-Dem, TI, WPP... do not);
the snapshot manifest rows carry no rights, so the query layer cannot add them either.
"""

from __future__ import annotations

import json
from typing import Any

from reva.atlas.catalog import cached_catalog

IPU_WOMEN = "politics.parliament.women_lower_chamber_pct"


def _names_licence(cell: dict[str, Any]) -> bool:
    if cell.get("licence") or cell.get("rights"):
        return True
    blob = json.dumps([cell.get("flags"), cell.get("provenance")]).lower()
    return "licence" in blob or "license" in blob


# Fixed (agent REVFIX2): every cell carries ``rights`` (table artifact rights, else the atlas source spec).
def test_dev_non_commercial_ipu_cells_name_their_licence(dev_atlas: Any) -> None:
    m = dev_atlas.map_values(cached_catalog(), IPU_WOMEN, 2023)
    ipu = [c for c in m["values"] if c["source"] == "ipu"]
    assert len(ipu) > 100  # IPU answers this concept for most countries
    assert all(_names_licence(c) for c in ipu)
    r = ipu[0]["rights"]
    assert r["non_commercial"] and r["share_alike"] and r["restricted"] and r["commercial_use"] == "not_permitted"
    assert any(x["source"] == "ipu" and x["restricted"] for x in m["licences"])
