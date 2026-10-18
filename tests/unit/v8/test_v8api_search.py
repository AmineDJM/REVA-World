"""V8API search: normalisation, index build from synthetic documents, ranking tiers, lookup-not-identity, hrefs."""

from __future__ import annotations

from pathlib import Path

import pytest

from reva.v8.search import hrefs
from reva.v8.search.build import Doc, IndexWriter, index_path
from reva.v8.search.index import SearchIndex, contained_tier
from reva.v8.search.normalize import norm_code, norm_text, trigrams


def test_normalisation_is_accent_and_case_insensitive() -> None:
    assert norm_text("Pétrole BRUT") == "petrole brut"
    assert norm_text("Côte d'Ivoire") == "cote d ivoire"
    assert norm_text("Œuvre") == "oeuvre"
    assert norm_code("HGNC: 1100") == "hgnc:1100"
    assert "  p" in trigrams("Pétrole")


def test_hrefs_mirror_v8links() -> None:
    assert hrefs.entity("country", "FRA") == "/v8/fiche/country/FRA"
    assert hrefs.entity("gene", "hgnc:1100") == "/v8/fiche/gene/hgnc%3A1100"
    assert hrefs.series("imf.weo", {"concept": "x y"}) == "/v8/monde?table=imf.weo&concept=x+y"
    assert hrefs.decision("opec") == "/v8/decision?ref=opec"
    assert hrefs.scoreboard("weo.cpi") == "/v8/scores?track=weo.cpi"


def test_contained_tier() -> None:
    assert contained_tier("brut", ["brut"], "brut") == "exact"
    assert contained_tier("petrole", ["petrole"], "petrole brut") == "prefix"
    assert contained_tier("brut", ["brut"], "prix du petrole brut") == "word"
    assert contained_tier("brut petrole", ["brut", "petrole"], "petrole brut") == "word"
    assert contained_tier("rut", ["rut"], "petrole brut") == "trigram"
    assert contained_tier("xyz", ["xyz"], "petrole brut") is None


def _doc(kind: str, id_: str, fr: str | None, en: str | None, prior: float, **kw: object) -> Doc:
    return Doc(kind=kind, id=id_, label_fr=fr, label_en=en, sublabel=f"{kind} {id_}", href=f"/x/{id_}",
               prior=prior, **kw)  # type: ignore[arg-type]  # fmt: skip


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> SearchIndex:
    d: Path = tmp_path_factory.mktemp("v8s")
    w = IndexWriter(index_path(d))
    w.add(_doc("country", "FRA", "France", "France", 1.0, codes=["FRA", "FR", "250", "Q142"], texts=["La France"]))
    w.add(_doc("entity", "gppd:WRI1", None, "France", 0.15, entity_kind="facility", compact=True))
    w.add(_doc("indicator", "prices.brent", "Prix spot du pétrole brut Brent", "Brent crude spot price", 0.5,
               codes=["prices.brent"]))  # fmt: skip
    w.add(_doc("entity", "hgnc:1100", "BRCA1", "BRCA1", 0.3, entity_kind="gene", codes=["hgnc:1100"],
               texts=["RNF53"], compact=True))  # fmt: skip
    w.add(_doc("decision", "opec_cut", "OPEP+ réduit sa production", "OPEC+ cuts production", 0.65))
    w.add(_doc("entity", "Q1", None, "Fragaria", 0.1, entity_kind="taxon", compact=True))
    w.finish({"version": "test", "built_at": "2026-10-17T00:00:00Z", "errors": []})
    return SearchIndex(index_path(d))


def test_exact_id_first_then_labels(index: SearchIndex) -> None:
    r = index.search("FRA")
    assert r["hits"][0]["id"] == "FRA" and r["hits"][0]["match"] == "id"
    r = index.search("HGNC:1100")
    assert r["hits"][0]["id"] == "hgnc:1100" and r["hits"][0]["match"] == "id"
    assert r["hits"][0]["href"] == "/v8/fiche/gene/hgnc%3A1100"  # compact KG record: href rebuilt


def test_same_name_two_records_never_merged(index: SearchIndex) -> None:
    r = index.search("France")
    ids = [h["id"] for h in r["hits"]]
    assert ids[:2] == ["FRA", "gppd:WRI1"]  # both kept; the country ranks first by prior
    assert r["hits"][0]["group"] == "Pays & régions" and r["hits"][1]["group"] == "Entités"


def test_accent_insensitive_word_match_and_alias(index: SearchIndex) -> None:
    r = index.search("petrole brut")
    assert r["hits"][0]["id"] == "prices.brent" and r["hits"][0]["match"] in ("prefix", "word")
    r = index.search("brut")
    assert r["hits"][0]["id"] == "prices.brent"
    r = index.search("rnf53")
    assert r["hits"][0]["id"] == "hgnc:1100" and r["hits"][0]["matched"].lower() == "rnf53"
    r = index.search("opep")
    assert r["hits"][0]["kind"] == "decision"


def test_kinds_filter_and_language(index: SearchIndex) -> None:
    r = index.search("fra", kinds=["entity"])
    assert all(h["kind"] == "entity" for h in r["hits"])
    assert "FRA" not in [h["id"] for h in r["hits"]]
    r = index.search("brent", lang="en")
    assert r["hits"][0]["label"] == "Brent crude spot price"


def test_empty_and_unknown(index: SearchIndex) -> None:
    assert index.search("   ")["hits"] == []
    assert index.search("zzzzqqq")["hits"] == []
