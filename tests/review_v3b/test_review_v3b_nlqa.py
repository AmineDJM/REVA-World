"""Review V3b — NLQA concept-tie resolution through the headline layer (reva.nlqa.fallback.headline_tiebreak).

ASK-7: "Population de la France en 2025" ties five catalog concepts; the tie is "resolved by the headline layer"
(``resolution: headline``, ``headline: reva.population.total``) in favour of ``imf.population`` (IMF WEO ``LP``),
the one series that ``reva.population.total`` explicitly lists under ``not_equivalent`` ("national definitions
... WEO years after the latest actual are projections"). ``headline_tiebreak`` treats membership of
``related_catalog_concepts`` as coverage and never looks at ``not_equivalent``.
"""

from __future__ import annotations

from reva.atlas.source_order import load_headlines
from reva.nlqa.catalog import build_catalog
from reva.nlqa.fallback import keyword_plan


# Fixed (agent REVFIX2): headline_tiebreak skips candidates whose series the headline lists as not_equivalent.
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


def test_headline_tiebreak_drops_not_equivalent_candidates_and_still_decides_among_the_rest() -> None:
    cat = build_catalog()
    reg = load_headlines()
    plan = keyword_plan("Population de la France en 2025", cat, reg)
    (amb,) = plan["ambiguities"]
    assert amb["resolution"] != "headline" or amb["chosen"] != "imf.population"
    plan = keyword_plan("Quelle est la population totale du Brésil ?", cat, reg)
    (amb,) = plan["ambiguities"]
    assert (amb["resolution"], amb["chosen"]) == ("headline", "un.wpp.population")


def test_keyword_planner_knows_french_country_names() -> None:
    """ASK-8: iso-codes French translation + short forms + curated aliases; nested names are not mentions."""
    cat = build_catalog()
    reg = load_headlines()
    ops = keyword_plan("Quelle est la population totale du Brésil ?", cat, reg)["operations"]
    assert [(o["op"], o.get("country")) for o in ops] == [("atlas_value", "BRA")]
    ops = keyword_plan("liens entre les États-Unis et le Royaume-Uni 1965 1975", cat, reg)["operations"]
    assert [(o["op"], o["country"], o["other_country"]) for o in ops] == [("links_pair", "USA", "GBR")]
    for q, iso in (
        ("Population totale en Allemagne", "DEU"),
        ("Population totale en Russie", "RUS"),
        ("Population totale en Corée du Sud", "KOR"),
        ("Population totale en Guinée-Bissau", "GNB"),
        ("Population totale en Côte d'Ivoire", "CIV"),
    ):
        countries = {o.get("country") for o in keyword_plan(q, cat, reg)["operations"]}
        assert countries == {iso}, (q, countries)
