"""V6 INTEG (for V4LINK): families ``same_as`` (identity evidence, v4link:wikidata_item) and ``corresponds_to``
(concordance rows, v4link:corresponds_to) are traversable; an identity step is labelled as identity, a concordance
step carries the table's own flags. Shapes follow the dev DB (nuts:DE2 -> Q980 <- iso3166_2:DE-BY; hs2022 ->
cpc21 rows of table CPC21-HS2017). Synthetic statement ids; the codes are only path shapes, not loaded facts."""

from __future__ import annotations

from reva.kg.families import concordance_flags, load_registry
from reva.links.model import Stmt
from reva.links.paths import PathQuery, build_graph, find_paths


def _g():  # type: ignore[no-untyped-def]
    flags = {"table": ("CPC21-HS2017",), "hs_partial": ("0",), "cpc_partial": ("1",), "redistribution": ("x",)}
    sts = [
        Stmt("w1", "nuts:DE2", "v4link:wikidata_item", object="Q980", source_key="wikidata.wdqs"),
        Stmt("w2", "iso3166_2:DE-BY", "v4link:wikidata_item", object="Q980", source_key="wikidata.wdqs"),
        Stmt("c1", "hs2017:270900", "v4link:corresponds_to", object="cpc21:12010", qualifiers=flags,
             source_key="unsd.classifications"),
    ]  # fmt: skip
    ids = ("nuts:DE2", "Q980", "iso3166_2:DE-BY", "hs2017:270900", "cpc21:12010")
    return build_graph(sts, labels={q: {"fr": q, "en": q} for q in ids})


def test_registry_families() -> None:
    reg = load_registry()
    assert reg.families["same_as"].symmetric and reg.families["corresponds_to"].symmetric
    assert reg.mappings["v4link:wikidata_item"].family == "same_as"
    assert reg.mappings["v4link:corresponds_to"].family == "corresponds_to"
    assert not any(f.causal for f in reg.families.values())


def test_identity_steps_are_labelled_identity() -> None:
    res = find_paths(_g(), PathQuery("nuts:DE2", "iso3166_2:DE-BY", max_hops=2))
    assert res.found
    hops = res.paths[0].to_json()["hops"]
    assert [h["family"] for h in hops] == ["same_as", "same_as"]
    assert all(h["identity"] is True for h in hops)
    assert "même entité" in res.paths[0].text["fr"][0]


def test_concordance_step_carries_table_flags() -> None:
    res = find_paths(_g(), PathQuery("cpc21:12010", "hs2017:270900", max_hops=1))
    (hop,) = res.paths[0].to_json()["hops"]
    assert hop["family"] == "corresponds_to" and hop["identity"] is False
    assert hop["flags"] == {"cpc_partial": "1", "hs_partial": "0", "table": "CPC21-HS2017"}
    # family filter works in both directions (symmetric family)
    assert find_paths(_g(), PathQuery("hs2017:270900", "cpc21:12010", families=frozenset({"corresponds_to"}))).found


def test_concordance_flags_helper() -> None:
    assert concordance_flags({"table": "HS2022-HS1992", "relationship": "1:n", "other": "x"}) == {
        "relationship": "1:n",
        "table": "HS2022-HS1992",
    }
    assert concordance_flags(None) == {}
