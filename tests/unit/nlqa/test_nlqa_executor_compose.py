"""Executor (fakes) + composer: cited sentences, no blending, missing said as missing, paths are not causes."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import nlqa_fakes as F
import pytest

from reva.nlqa.compose import compose, format_number
from reva.nlqa.executor import Executor, rank_cells
from reva.nlqa.plan import validate_plan
from reva.nlqa.ports import Backends, KgPath, PathHop

CAT = F.catalog()


def op(id_: str, name: str, **kw: Any) -> dict[str, Any]:
    return {"id": id_, "op": name, **kw}


def run(question: str, *ops: dict[str, Any], lang: str = "fr", backends: Backends | None = None, unsupported=None):
    p = validate_plan(
        {"input_language": lang, "restatement": "", "operations": list(ops), "unsupported": unsupported or []},
        CAT,
        question,
    )
    x = Executor(backends or F.backends(), CAT).run(p)
    return x, compose(x, question, CAT)


def all_cited(x, c) -> None:
    ids = {e["id"] for e in x.evidence.items}
    for s in c.sentences:
        assert s.citations, s.text
        assert set(s.citations) <= ids, s.text


COLUMBUS_Q = "Comment Christophe Colomb est-il lié au fait que les Brésiliens parlent portugais ?"
COLUMBUS_OPS = (
    op("s1", "entity_lookup", text="Christophe Colomb", kind="person"),
    op("s2", "entity_lookup", text="portugais", kind="language"),
    op("s3", "kg_path", from_entity="s1", to_entity="s2", max_hops=4),
)


def test_path_is_connected_via_not_a_cause_and_cites_every_hop() -> None:
    x, c = run(COLUMBUS_Q, *COLUMBUS_OPS)
    all_cited(x, c)
    res = x.results[0]
    assert res.status == "ok"
    path, hops = res.data["paths"][0]
    assert path.engine == "nlqa.bfs" and len(hops) == 4
    head = next(s for s in c.sentences if s.kind == "path")
    assert "relié(e) à portugais via 4 étape(s)" in head.text and "pas une cause" in head.text
    assert len(head.citations) == 4
    # the only causal wording is the source-asserted P828 hop, attributed to Wikidata
    causal = [s for s in c.sentences if "causale" in s.text]
    assert all("affirmée par Wikidata" in s.text or "affirme lui-même" in s.text for s in causal)
    assert not re.search(r"à cause de|parce que|a provoqué|because", c.text)
    ev = [x.evidence.get(e) for e in head.citations]
    assert all(e["kind"] == "kg_statement" and e["citation"]["status"] == "community_edited" for e in ev)
    assert ev[0]["citation"]["url"].startswith("https://www.wikidata.org/wiki/Q7322")
    assert "oldid=" in ev[0]["citation"]["revision_url"]


def test_path_engine_port_is_preferred_when_present() -> None:
    class Paths:
        name = "links.paths (fake)"

        def paths(self, a, b, *, max_hops, as_of, limit):
            return [KgPath((PathHop(F.STATEMENTS[4]),), engine=self.name)]

    x, c = run(COLUMBUS_Q, *COLUMBUS_OPS, backends=F.backends(paths=Paths()))
    assert x.results[0].data["paths"][0][0].engine == "links.paths (fake)"
    assert "links.paths (fake)" in c.text


def test_no_path_is_said_as_missing_with_sources_consulted() -> None:
    x, c = run(
        COLUMBUS_Q,
        op("s1", "entity_lookup", text="Christophe Colomb"),
        op("s2", "entity_lookup", text="Brésil"),
        op("s3", "kg_path", from_entity="s1", to_entity="s2", max_hops=2),
    )
    all_cited(x, c)
    assert "Aucun chemin d'au plus 2 étapes" in c.text and "nlqa.bfs" in c.text
    assert x.evidence.get(c.sentences[-2].citations[0])["kind"] == "absence"


def test_rank_is_within_one_source_and_one_year_never_blended() -> None:
    x, c = run("Quels pays ont la plus forte densité ?", op("s1", "atlas_rank", concept="population.density"))
    all_cited(x, c)
    d = x.results[0].data
    # 2023 has a single early reporter: the latest well-covered year (2022) is ranked
    assert d["source"] == "wb.wdi.EN.POP.DNST" and d["year"] == 2022
    assert [cell.iso3 for cell, _ in d["rows"]][:3] == ["MCO", "SGP", "BHR"]
    assert {cell.source for cell, _ in d["rows"]} == {"wb"} and {cell.year for cell, _ in d["rows"]} == {2022}
    assert "MAC" not in {cell.iso3 for cell, _ in d["rows"]}  # only in the other source: not mixed in
    assert "autres sources non mélangées : un.wpp.PopDensity" in c.text
    assert "≈ 26 337,46" in c.text  # rounded for display, marked; exact value in the evidence
    assert x.evidence.get(d["rows"][0][1])["value"] == "26337.4615384615"


def test_rank_excludes_missing_values() -> None:
    cells = [*F.CELLS, F.cell("wb", "wdi", "EN.POP.DNST", "ATA", 2022, None, "x", F.RAW_WDI, "population.density")]
    _src, _year, rows, n, non_numeric = rank_cells(cells, ("wb.wdi.EN.POP.DNST",), 2022, "asc", 3)
    assert "ATA" not in {r.iso3 for r in rows} and non_numeric == 1 and n == 9
    assert [r.iso3 for r in rows] == ["BRA", "USA", "SEN"]


def test_value_names_its_source_and_flags_and_sources_stay_separate() -> None:
    x, c = run(
        "densité de Monaco en 2022", op("s1", "atlas_value", concept="population.density", country="MCO", year=2022)
    )
    all_cited(x, c)
    texts = [s.text for s in c.sentences if s.kind == "fact"]
    assert texts[0].startswith("Densité de population — Monaco, 2022 : ≈ 26 337,46")
    assert "source : wb/wdi, indicateur EN.POP.DNST" in texts[0]
    assert texts[1].startswith("Autre source : ") and "un/wpp" in texts[1]
    assert "sans moyenne ni fusion" in c.text


def test_missing_value_is_said_missing_naming_sources_then_latest_labelled() -> None:
    x, c = run(
        Q := "salaire minimum au Sénégal en 2023",
        op("s1", "atlas_value", concept="labour.minimum_wage_monthly", country="SEN", year=2023),
    )
    all_cited(x, c)
    assert Q
    miss = next(s for s in c.sentences if s.kind == "missing")
    assert miss.text.startswith("Aucune source consultée ne fournit Salaire minimum mensuel")
    assert "ilo.ilostat_mw.EAR_INEE_NOC_NB|cur=LCU" in miss.text
    latest = next(s for s in c.sentences if s.kind == "fact")
    assert latest.text.startswith("Dernière valeur disponible (autre année)") and "2019" in latest.text


def test_concept_without_any_data_is_missing_not_zero() -> None:
    x, c = run(
        "salaire moyen au Sénégal", op("s1", "atlas_value", concept="labour.average_monthly_earnings", country="SEN")
    )
    all_cited(x, c)
    assert "Aucune source consultée ne fournit Salaire mensuel moyen pour Sénégal" in c.text
    assert not re.search(r"\b0\b", c.text)
    assert c.sentences[-1].text.startswith("Aucune réponse sourcée")


def test_unavailable_backend_is_typed_capability_unavailable() -> None:
    x, c = run(
        "densité", op("s1", "atlas_rank", concept="population.density"), backends=F.backends(atlas=F.BrokenAtlas())
    )
    assert x.results[0].status == "unavailable"
    assert "capability_unavailable" in c.text and "l'atlas" in c.text
    x, c = run(COLUMBUS_Q, *COLUMBUS_OPS, backends=Backends())
    assert [r.status for r in x.results] == ["unresolved"]
    assert x.lookups["s1"].status == "unavailable"


def test_timeline_and_neighbours_label_wikidata_and_dates() -> None:
    x, c = run(
        "Position des États-Unis dans les années 1950-60",
        op("s1", "entity_lookup", text="États-Unis"),
        op("s2", "kg_timeline", entity="s1", from_year=1950, to_year=1969),
        op("s3", "kg_neighbours", entity="s1", properties=["P35"], as_of="1955"),
    )
    all_cited(x, c)
    facts = [s.text for s in c.sentences if s.kind == "fact"]
    assert facts[0].startswith("1945-10-24 : États-Unis — « membre de » : Organisation des Nations unies")
    assert all("(Wikidata, donnée communautaire)" in t for t in facts)
    neigh = next(r for r in x.results if r.op.op == "kg_neighbours")
    assert [s.object_qid for s, _ in neigh.data["statements"]] == ["Q9916"]  # as_of 1955: Eisenhower only


def test_llm_strings_not_in_question_are_never_quoted() -> None:
    _x, c = run(
        "Combien de morts ?",
        op("s1", "entity_lookup", text="1000 morts selon moi"),
        op("s2", "kg_neighbours", entity="s1", properties=[]),
        unsupported=[{"text": "le PIB est de 999 milliards", "reason": "x"}],
    )
    assert "1000" not in c.text and "999" not in c.text
    assert "un terme de la question" in c.text and "une partie de la question" in c.text


def test_ambiguous_entity_is_warned_and_alternatives_named() -> None:
    class TwoColumbus(F.FakeKg):
        def search_entities(self, text, *, kind, lang, limit):
            from reva.nlqa.ports import EntityMatch

            return [EntityMatch(F.E["Q7322"], "label_exact"), EntityMatch(F.E["Q90000009"], "label_exact")]

    x, c = run(COLUMBUS_Q, op("s1", "entity_lookup", text="Colomb"), backends=F.backends(kg=TwoColumbus()))
    assert x.lookups["s1"].entity.qid == "Q7322"  # higher sitelinks
    assert any(w.startswith("ambiguous_entity") for w in x.warnings)
    assert "Autres candidats possibles : Colombo (Q90000009)" in c.text


def test_deprecated_statements_are_excluded() -> None:
    import dataclasses

    dep = dataclasses.replace(
        F.STATEMENTS[5],
        statement_id="Q155$DEPRECATED",
        rank="deprecated",
        object_qid="Q29",
        object_labels={"fr": "Espagne"},
    )
    kg = F.FakeKg([*F.STATEMENTS, dep])
    x, c = run(
        "langue du Brésil",
        op("s1", "entity_lookup", text="Brésil"),
        op("s2", "kg_neighbours", entity="s1", properties=["P37"]),
        backends=F.backends(kg=kg),
    )
    assert "Espagne" not in c.text and "DEPRECATED" not in str(x.evidence.items)


def test_links_cite_their_statements() -> None:
    x, c = run("liens du Brésil", op("s1", "links", country="BRA", link_kinds=["shared_language"]))
    all_cited(x, c)
    s = next(s for s in c.sentences if s.kind == "fact")
    kinds = [x.evidence.get(e)["kind"] for e in s.citations]
    assert kinds[0] == "link" and kinds.count("kg_statement") == 2


def test_english_answer() -> None:
    _x, c = run("highest density", op("s1", "atlas_rank", concept="population.density", year=2022, limit=2), lang="en")
    assert c.text.startswith("Population density (people per sq. km of land area), 2022, highest values")
    assert "1. Monaco: ≈ 26,337.46; 2. Singapore: 7,595.2." in c.text


def test_template_numbers_come_from_cited_evidence() -> None:
    """Every number in a fact sentence is a cited value (display rounding only), a year/date of a cited
    record, a rank position, or part of a cited indicator code."""
    x, c = run(
        "Quels pays ont la plus forte densité ?",
        op("s1", "atlas_rank", concept="population.density"),
        op("s2", "atlas_value", concept="labour.minimum_wage_monthly", country="SEN"),
    )
    for s in c.sentences:
        if s.kind != "fact":
            continue
        ev = [x.evidence.get(e) for e in s.citations]
        values = [Decimal(e["value"]) for e in ev if e.get("value")]
        allowed_text = " ".join(str(e) for e in ev)
        for m in re.finditer(r"\d[\d ]*(?:,\d+)?", s.text):
            tok = m.group(0).replace(" ", "")
            if tok in allowed_text or (len(tok) <= 2 and tok.isdigit()):
                continue
            d = Decimal(tok.replace(",", "."))
            assert any(abs(v - d) <= Decimal("0.01") * max(1, abs(v)) for v in values), (tok, s.text)


@pytest.mark.parametrize(
    ("value", "lang", "shown"),
    [
        ("26337.4615384615", "fr", "≈ 26 337,46"),
        ("26337.4615384615", "en", "≈ 26,337.46"),
        ("58900", "fr", "58 900"),
        ("1678.95", "en", "1,678.95"),
        ("0.012345", "en", "≈ 0.0123"),
        ("-3.5", "fr", "-3,5"),
        ("12.50", "en", "12.5"),
    ],
)
def test_format_number(value: str, lang: str, shown: str) -> None:
    assert format_number(value, lang) == shown


def test_derived_link_hop_cites_every_supporting_record() -> None:
    from reva.nlqa.ports import DerivedLinkHop

    class Paths:
        name = "reva.links.paths (fake)"

        def paths(self, a, b, *, max_hops, as_of, limit):  # type: ignore[no-untyped-def]
            hop = DerivedLinkHop(
                "former_colony_of",
                "Q155",
                "Q45",
                {"fr": "Brésil"},
                {"fr": "Portugal"},
                statements=(F.STATEMENTS[6], F.STATEMENTS[7]),
                period="1500–1822",
            )
            return [KgPath((PathHop(F.STATEMENTS[5], forward=False), PathHop(link=hop)), engine=self.name)]

    x, c = run(
        "lien entre le portugais et le Portugal",
        op("s1", "entity_lookup", text="portugais"),
        op("s2", "entity_lookup", text="Portugal"),
        op("s3", "kg_path", from_entity="s1", to_entity="s2", max_hops=3),
        backends=F.backends(paths=Paths()),
    )
    all_cited(x, c)
    hop2 = next(s for s in c.sentences if s.text.startswith("Étape 2"))
    assert "« lien dérivé : former_colony_of » : Portugal (1500–1822)" in hop2.text
    kinds = [x.evidence.get(e)["kind"] for e in hop2.citations]
    assert kinds == ["link", "kg_statement", "kg_statement"]
