"""Review V3b — NLQA concept-tie resolution through the headline layer (reva.nlqa.fallback.headline_tiebreak).

ASK-7: "Population de la France en 2025" ties five catalog concepts; the tie is "resolved by the headline layer"
(``resolution: headline``, ``headline: reva.population.total``) in favour of ``imf.population`` (IMF WEO ``LP``),
the one series that ``reva.population.total`` explicitly lists under ``not_equivalent`` ("national definitions
... WEO years after the latest actual are projections"). ``headline_tiebreak`` treats membership of
``related_catalog_concepts`` as coverage and never looks at ``not_equivalent``.
"""

from __future__ import annotations

import pytest

from reva.atlas.source_order import load_headlines
from reva.nlqa.catalog import build_catalog
from reva.nlqa.fallback import keyword_plan


@pytest.mark.xfail(
    strict=True,
    reason="ASK-7: fallback.headline_tiebreak picks imf.population (WEO LP), declared not_equivalent by "
    "reva.population.total, and labels the choice resolution=headline",
)
def test_headline_tiebreak_never_chooses_a_series_the_headline_declares_not_equivalent() -> None:
    cat = build_catalog()
    reg = load_headlines()
    plan = keyword_plan("Population de la France en 2025", cat, reg)
    (amb,) = plan["ambiguities"]
    if amb.get("resolution") != "headline":
        return  # not resolved by the headline layer: nothing to check
    chosen, hid = amb["chosen"], amb["headline"]
    excluded = {x["series"] for x in reg.concepts[hid].not_equivalent}
    assert not set(cat.concepts[chosen].source_order) & excluded, (chosen, hid)
