"""``links_pair`` (agent LINKWIRE): every link between two countries, planned, executed and worded with citations.

Vote agreement is a share of identical votes (with its counts), never worded as an alliance; a link with a
predecessor state comes with its succession chain; link types without links are "no link", never zero."""

from __future__ import annotations

from typing import Any

import nlqa_fakes as F
import pytest

from reva.nlqa.adapters import LINK_GROUPS, _pair_link, link_kind
from reva.nlqa.catalog import LINK_KINDS
from reva.nlqa.compose import compose
from reva.nlqa.executor import Executor, pair_links_shown
from reva.nlqa.fallback import keyword_plan
from reva.nlqa.plan import restate, validate_plan
from reva.nlqa.ports import AtlasCell, CountryLink, PairLinkGroup, PairLinks, PredecessorLink
from reva.nlqa.prompt import PROMPT_VERSION, output_schema

CAT = F.catalog()
RAW = "e" * 64


def cell(src: str, sel: str, subject: str) -> AtlasCell:
    return AtlasCell(src, "", "x", subject, "", "", raw_sha256=RAW, selector=sel)


def trade(a: str, b: str, year: int, v: str) -> CountryLink:
    return CountryLink(
        "trade",
        a,
        b,
        period=str(year),
        weight=v,
        unit="US dollars",
        cells=(cell("imf", f'{{"series":"{a}.XG_FOB_USD.{b}.A","time_period":"{year}"}}', a),),
        source="imf",
        link_type="bilateral_trade",
    )


def vote(decade: int, share: str, n: int, k: int, issue: str = "all", grain: str = "decade") -> CountryLink:
    hi = decade + 9 if grain == "decade" else decade
    return CountryLink(
        "diplomatic",
        "BRA",
        "PRT",
        period=f"{decade}-01-01–{hi}-12-31",
        weight=share,
        unit="share of identical votes (0-1)",
        cells=(cell("unga", f'{{"pair":["BRA","PRT"],"years":[{decade},{hi}],"issue":"{issue}"}}', "BRA"),),
        source="unga",
        link_type="unga_vote_agreement",
        basis={"n_common": n, "n_identical": k, "issue": issue, "grain": grain, "formula": "n_identical / n_common"},
        facet=f"issue={issue}",
    )


LANG = CountryLink(
    "shared_language",
    "BRA",
    "PRT",
    statements=(F.STATEMENTS[5], F.STATEMENTS[4]),
    source="wikidata",
    link_type="shared_official_language",
    via=("Q5146",),
)
COLONY = CountryLink(
    "colonial",
    "BRA",
    "Q200464",
    period="1500–1822",
    statements=(F.STATEMENTS[6],),
    source="wikidata",
    link_type="former_colony_of",
    b_node="Q200464",
)
SUCC = CountryLink(
    "succession",
    "PRT",
    "Q200464",
    statements=(F.STATEMENTS[7],),
    source="wikidata",
    link_type="successor_of",
    b_node="Q200464",
)
TYPE_LABELS = {
    "bilateral_trade": {"fr": "exportations de biens déclarées par l'exportateur", "en": "goods exports"},
    "unga_vote_agreement": {"fr": "part de votes identiques à l'AG de l'ONU", "en": "share of identical UNGA votes"},
    "shared_official_language": {"fr": "langue officielle commune", "en": "shared official language"},
    "former_colony_of": {"fr": "ancienne colonie de", "en": "former colony of"},
    "successor_of": {"fr": "successeur de", "en": "successor of"},
    "migrant_stock": {"fr": "stock de migrants (origine → destination)", "en": "migrant stock"},
    "land_border": {"fr": "frontière terrestre", "en": "land border"},
}


def group(t: str, links: list[CountryLink], directed: bool = False) -> PairLinkGroup:
    years = [int(lk.period[:4]) for lk in links if lk.period and lk.period[:4].isdigit()]
    return PairLinkGroup(
        t,
        link_kind(t),
        len(links),
        min(years, default=None),
        max(years, default=None),
        tuple(links),
        TYPE_LABELS[t],
        directed,
    )


PAIR = PairLinks(
    "BRA",
    "PRT",
    (
        group(
            "bilateral_trade",
            [trade("BRA", "PRT", y, str(1000 * y)) for y in (2000, 2001, 2025)]
            + [trade("PRT", "BRA", y, str(10 * y)) for y in (2000, 2025)],
            directed=True,
        ),
        group(
            "unga_vote_agreement",
            [
                vote(1950, "0.5989", 187, 112),
                vote(1960, "0.3902", 205, 80),
                vote(1960, "0.38", 50, 19, issue="co"),
                vote(1961, "0.4", 20, 8, grain="year"),
            ],
        ),
        group("shared_official_language", [LANG]),
    ),
    kinds_without_links=("migrant_stock", "land_border"),
    predecessor_links=(PredecessorLink("BRA", "PRT", COLONY, (SUCC,)),),
    node_labels={
        "Q5146": {"fr": "portugais", "en": "Portuguese"},
        "Q200464": {"fr": "Empire colonial portugais", "en": "Portuguese Empire"},
    },
    store="materialisation 7",
    type_labels=TYPE_LABELS,
)


class PairFake(F.FakeLinks):
    def __init__(self, pair: PairLinks = PAIR) -> None:
        self.result = pair
        self.calls: list[tuple[Any, ...]] = []

    def pair(self, a, b, kinds, from_year, to_year):  # type: ignore[no-untyped-def]
        self.calls.append((a, b, kinds, from_year, to_year))
        return self.result if (a, b) == ("BRA", "PRT") else PairLinks(a, b, (), kinds_without_links=("land_border",))


def run(question: str, *ops: dict[str, Any], lang: str = "fr", links: Any = None):  # type: ignore[no-untyped-def]
    p = validate_plan(
        {"input_language": lang, "restatement": "", "operations": list(ops), "unsupported": []}, CAT, question
    )
    x = Executor(F.backends(links=links if links is not None else PairFake()), CAT).run(p)
    return p, x, compose(x, question, CAT)


def op(id_: str, name: str, **kw: Any) -> dict[str, Any]:
    return {"id": id_, "op": name, **kw}


PAIR_OP = op("s1", "links_pair", country="BRA", other_country="PRT", link_kinds=[])


def test_plan_schema_and_validation() -> None:
    assert PROMPT_VERSION == "nlqa-plan-1.2.0"  # 1.1.0 added links_pair; 1.2.0 added why / causal_chain
    props = output_schema()["properties"]["operations"]["items"]["properties"]
    assert "links_pair" in props["op"]["enum"] and "other_country" in props
    q = "Quels liens entre le Brésil et le Portugal ?"
    p = validate_plan({"operations": [PAIR_OP]}, CAT, q)
    assert p.operations[0].args == {
        "country": "BRA",
        "other_country": "PRT",
        "kinds": [],
        "from_year": None,
        "to_year": None,
    }
    assert "les liens (tous types) entre" in restate(p, CAT)
    for bad, reason in (
        ({**PAIR_OP, "other_country": "BRA"}, "pair_with_itself"),
        ({**PAIR_OP, "other_country": "Q45"}, "country_not_in_list"),
        ({**PAIR_OP, "link_kinds": ["friendship"]}, "link_kind_not_in_list"),
        ({**PAIR_OP, "from_year": 2000, "to_year": 1990}, "invalid_argument"),
    ):
        p = validate_plan({"operations": [bad]}, CAT, q)
        assert not p.operations and p.rejected[0]["reason"] == reason, bad
    # lookup refs are accepted (resolved to ISO3 by the server)
    p = validate_plan(
        {
            "operations": [
                op("s1", "entity_lookup", text="Brésil", kind="country"),
                op("s2", "entity_lookup", text="Portugal", kind="country"),
                op("s3", "links_pair", country="s1", other_country="s2", link_kinds=["trade", "diplomatic"]),
            ]
        },
        CAT,
        q,
    )
    assert [o.op for o in p.operations] == ["entity_lookup", "entity_lookup", "links_pair"] and not p.rejected


def test_every_link_type_has_a_user_level_group() -> None:
    from reva.links.taxonomy import load_taxonomy

    grouped = {t for ts in LINK_GROUPS.values() for t in ts}
    assert set(load_taxonomy().link_types) <= grouped
    assert set(LINK_GROUPS) == set(LINK_KINDS)
    for t, g in (
        ("bilateral_trade_imports", "trade"),
        ("bilateral_trade_baci", "trade"),
        ("land_border_cshapes", "border"),
        ("colonial_rule_cshapes", "colonial"),
        ("peacekeeping_contributor_to_host", "peacekeeping"),
        ("shared_polity_territory", "historical_polity"),
        ("shared_language_glottolog", "shared_language"),
        ("co_party", "treaty"),
        ("submarine_cable", "infrastructure"),
        ("air_routes_2014", "infrastructure"),
        ("launch_service", "infrastructure"),
    ):
        assert link_kind(t) == g


def test_pair_answer_is_cited_and_worded_honestly_fr() -> None:
    _, x, c = run("Quels sont les liens entre le Brésil et le Portugal ?", PAIR_OP)
    ids = {e["id"] for e in x.evidence.items}
    for s in c.sentences:
        assert s.citations and set(s.citations) <= ids, s.text
    text = c.text
    # trade: first and last value per direction, the count of yearly values, the source
    t = next(s.text for s in c.sentences if s.text.startswith("exportations de biens"))
    assert (
        "Brésil → Portugal" in t
        and "2\u202f000\u202f000 US dollars (2000)" in t
        and "2\u202f025\u202f000 US dollars (2025)" in t
    )
    assert "3 valeur(s) annuelle(s)" in t and "(2001)" not in t and "source : imf" in t
    assert any("Portugal → Brésil" in s.text for s in c.sentences)
    # vote agreement: share of identical votes with its counts, per decade; never "alliance"
    v = [s.text for s in c.sentences if s.text.startswith("Part de votes identiques")]
    assert len(v) == 2  # issue=all and issue=co, decade grain only
    assert "1950–1959 : 0,5989 (112 identiques sur 187 votes communs)" in v[0]
    assert "1961" not in " ".join(v)
    assert "alliance" not in text.lower()
    # shared language with its via node, undated said so
    assert "langue officielle commune (via portugais) — Portugal (non daté dans la source)" in text
    # predecessor state with its succession chain
    pred = next(s for s in c.sentences if s.text.startswith("Lien avec un État prédécesseur"))
    assert "Brésil — ancienne colonie de → Empire colonial portugais (1500–1822)" in pred.text
    assert "succession : Portugal — successeur de → Empire colonial portugais" in pred.text
    assert len(pred.citations) >= 4  # both links and their statements
    # types without links: "no link", never zero
    miss = next(s for s in c.sentences if s.kind == "missing")
    assert "stock de migrants" in miss.text and "frontière terrestre" in miss.text
    assert "pas une valeur nulle" in miss.text
    assert x.evidence.get(miss.citations[0])["kind"] == "absence"
    # the vote evidence carries its counts
    ev = [e for e in x.evidence.items if e.get("link_type") == "unga_vote_agreement"]
    assert ev and ev[0]["basis"]["n_common"] == 187 and ev[0]["facet"] == "issue=all"


def test_pair_answer_in_english() -> None:
    _, _, c = run("What are the links between Brazil and Portugal?", PAIR_OP, lang="en")
    assert any(
        "Share of identical UN General Assembly votes" in s.text and "112 identical of 187 common votes" in s.text
        for s in c.sentences
    )
    assert "alliance" not in c.text.lower()
    assert any("no link, not a zero value" in s.text for s in c.sentences)


def test_pair_shown_links_are_bounded() -> None:
    g = PAIR.groups[0]
    shown = pair_links_shown(g)
    assert [(lk.a_iso3, lk.period) for lk in shown] == [
        ("BRA", "2000"),
        ("BRA", "2025"),
        ("PRT", "2000"),
        ("PRT", "2025"),
    ]
    assert len(pair_links_shown(PAIR.groups[1])) == 3


def test_empty_pair_and_missing_backend() -> None:
    _, x, c = run("liens entre la France et l'Algérie", op("s1", "links_pair", country="FRA", other_country="DZA"))
    assert x.results[0].status == "empty"
    assert c.sentences[0].kind == "missing" and "Aucun lien entre" in c.sentences[0].text
    _, x, c = run("liens entre le Brésil et le Portugal", PAIR_OP, links=F.FakeLinks())  # no pair method
    assert x.results[0].status == "unavailable" and c.sentences[0].kind == "unavailable"


def test_lookup_refs_resolve_to_countries() -> None:
    fake = PairFake()
    _, x, _ = run(
        "Quels sont les liens entre le Brésil et le Portugal ?",
        op("s1", "entity_lookup", text="Brésil", kind="country"),
        op("s2", "entity_lookup", text="Portugal", kind="country"),
        op("s3", "links_pair", country="s1", other_country="s2", link_kinds=["trade"], from_year=1990, to_year=2025),
        links=fake,
    )
    assert fake.calls == [("BRA", "PRT", ["trade"], 1990, 2025)]
    assert x.results[0].status == "ok"


@pytest.mark.parametrize(
    "question,expect",
    [
        # review V3b ASK-8: French country names are in the planner's country table (iso-codes fr translation)
        ("Quels sont les liens entre le Brésil et le Portugal ?", ("BRA", "PRT")),
        ("Quelle relation entre le Brésil et le Portugal depuis 1990 ?", ("BRA", "PRT")),
        ("Quels sont les liens entre la Bretagne et le Portugal ?", "lookups"),
        ("What are the links between Brazil and Portugal?", ("BRA", "PRT")),
        ("relations between France and Algeria 1950 2000", ("FRA", "DZA")),
    ],
)
def test_keyword_planner_recognises_pair_questions(question: str, expect: Any) -> None:
    raw = keyword_plan(question, CAT)
    p = validate_plan(raw, CAT, question, planner="keyword_fallback")
    pair = [o for o in p.operations if o.op == "links_pair"]
    assert len(pair) == 1 and not p.rejected, (raw, p.rejected)
    a = pair[0].args
    if expect == "lookups":
        lookups = {o.id: o.args["text"] for o in p.operations if o.op == "entity_lookup"}
        assert [lookups[a["country"]], lookups[a["other_country"]]] == ["Bretagne", "Portugal"]
    else:
        assert (a["country"], a["other_country"]) == expect
    if "1990" in question:
        assert a["from_year"] == 1990 and a["to_year"] == 1990
    if "1950" in question:
        assert (a["from_year"], a["to_year"]) == (1950, 2000)


def test_pair_view_link_json_becomes_a_port_record() -> None:
    lk = {
        "subject": "Q155",
        "object": "Q200464",
        "link_type": "former_colony_of",
        "temporal": {"kind": "interval", "start": "1500", "end": "1822"},
        "via": ["Q2088324"],
        "evidence": [
            {
                "kind": "kg_statement",
                "source": "wikidata",
                "statement_id": "Q155$1",
                "subject": "Q155",
                "pid": "P1365",
                "object": "Q2088324",
                "source_revision": "1",
                "origin": "test_fixture",
            },
            {
                "kind": "atlas_row",
                "source": "unga",
                "subject": "BRA",
                "pid": "vote_agreement|issue=all|grain=decade",
                "raw_sha256": RAW,
                "selector": "{}",
            },
        ],
        "sources": ["wikidata"],
        "basis": {"n_common": 12},
        "iso3": ["BRA", None],
        "facet": "",
    }
    r = _pair_link(lk, {"Q155": {"fr": "Brésil"}}, {})
    assert (r.a_iso3, r.b_iso3, r.b_node, r.period, r.via) == ("BRA", "Q200464", "Q200464", "1500–1822", ("Q2088324",))
    assert r.statements[0].statement_id == "Q155$1" and r.cells[0].selector == "{}" and r.facet is None
    assert r.kind == "colonial" and r.basis == {"n_common": 12}
