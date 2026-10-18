"""V8ASK — « Demander »: golden-set runner in CI form (fixture backends), the resolution bridges, the registered
table fallback, the answer bundle (headline, evidence view, how, sources, targets, pin), the brainstorm and the
API routes (auth stubbed). The live measure on the dev DB is tests/integration/v8/test_v8_ask_live_db.py."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "nlqa"
if str(FIX) not in sys.path:
    sys.path.insert(0, str(FIX))

import domain_fakes as D  # noqa: E402

from reva.nlqa import bridges as BR  # noqa: E402
from reva.nlqa.ports import AtlasCell, EntityMatch  # noqa: E402
from reva.nlqa.service import AskInput, answer_question  # noqa: E402
from reva.v8.ask import bundle as B  # noqa: E402
from reva.v8.ask import golden as G  # noqa: E402
from reva.v8.ask.brainstorm import Brainstormer, parse_target  # noqa: E402
from reva.v8.ask.service import AskService  # noqa: E402

CAT = D.catalog()


def ask(q: str, backends: Any = None) -> dict[str, Any]:
    r = asyncio.run(
        answer_question(
            AskInput(q, "a" * 64),
            catalog=CAT,
            backends=backends or D.backends(),
            drafters=None,
            ledger=None,
            budget=None,
        )
    )
    return r.as_json()


# --------------------------------------------------------------------------- golden set (CI form)
def test_golden_set_is_the_nlqa6_set() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nlqa"))
    from test_nlqa_domains import GOLDEN

    assert tuple(q for q, *_ in GOLDEN) == G.GOLDEN
    assert len(G.GOLDEN) == 47


def test_golden_coverage_fixture_mode_and_every_answer_is_sourced() -> None:
    rows = asyncio.run(G.run(G.GOLDEN, catalog=CAT, backends=D.backends()))
    s = G.summarise(rows)
    assert s["n"] == 47
    # fixture regression floor (the fakes answer what the golden test expects); live numbers in the report
    assert s["answered"] >= 33, [r["question"] for r in rows if r["status"] != "answered"]
    for r in rows:
        if r["status"] == "unknown":
            assert r["missing"], r  # « REVA ne sait pas » always says what is missing
    assert s["latency_ms"]["max"] is not None


def test_classifier() -> None:
    ev = [{"id": "E1", "kind": "atlas_cell"}, {"id": "E2", "kind": "absence", "what": "effect_estimates"}]
    yes = {"sentences": [{"kind": "fact", "citations": ["E1"]}], "evidence": ev}
    assert G.classify(yes)["status"] == "answered"
    part = {
        "sentences": [{"kind": "missing", "citations": ["E2"]}, {"kind": "related", "citations": ["E1"]}],
        "evidence": ev,
    }
    c = G.classify(part)
    assert c["status"] == "partial" and c["causes"] == ["effect_estimates"]
    no = {"sentences": [{"kind": "missing", "citations": ["E2"]}], "evidence": ev[1:]}
    assert G.classify(no)["status"] == "unknown"
    assert G.missing_text(["effect_estimates"]) == ["aucune estimation publiée chargée pour ce couple cause → effet"]


# --------------------------------------------------------------------------- bridges
def test_label_rules() -> None:
    assert BR.legal_form_rest("GLENCORE PLC", "Glencore") == "plc"
    assert BR.legal_form_rest("Glencore International Limited", "Glencore") is None
    assert BR.legal_form_rest("Wolf Hollow I LP", "Wolf") is None
    assert BR.qualified_rest("chromosome 17 (Human)", "chromosome 17") == "Human"
    assert BR.qualified_rest("chromosome 17p deletion", "chromosome 17") is None
    assert BR.fold_accents("Méthane") == "methane"
    e = D.E["fixture:water"]
    assert BR.is_code_alias_false_match(EntityMatch(e, "alias_exact", "EAU"), "eau")
    assert not BR.is_code_alias_false_match(EntityMatch(e, "alias_exact", "EAU"), "EAU")
    assert not BR.is_code_alias_false_match(EntityMatch(e, "label_exact", "eau"), "eau")


class BridgingKg(D.FakeKgDomains):
    """The fake KG plus one label bridge: wolf's record → another record carrying its English label."""

    def label_bridges(self, qid: str, exclude: Any = ()) -> list[tuple[Any, str, str]]:
        if qid == "fixture:wolf":
            return [(D.E["fixture:lion_b"], "en", "wolf")]
        return []


def test_label_bridge_answers_and_says_so() -> None:
    j = ask("Habitat du loup ?", D.backends(kg=BridgingKg()))
    t = " ".join(s["text"] for s in j["sentences"])
    assert "fiche distincte portant le libellé anglais « wolf »" in t
    assert "rapprochement par le libellé publié, pas une identité affirmée par une source" in t
    assert "Afrique" in t
    assert G.classify(j)["status"] == "answered"


def test_no_bridge_without_strict_match() -> None:
    j = ask("Habitat du loup ?")  # plain fakes: no label_bridges method → unchanged V6 absence
    assert G.classify(j)["status"] == "unknown"


class FakeTables:
    name = "fake registered tables"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def period_cells(self, concept_id, series, from_year, to_year):  # type: ignore[no-untyped-def]
        self.calls.append((concept_id, list(series), from_year, to_year))
        prov = {"rights": D.CC0, "outside_snapshot": True, "dataset_key": "atlas.fixture_eia.spot_d",
                "table_artifact_sha256": "a" * 64, "dataset_registered_at": "2026-10-17T15:42:35+00:00"}  # fmt: skip
        cells = [
            AtlasCell(
                "fixture_eia",
                "spot_d",
                "RBRTE",
                "",
                d,
                d,
                v,
                None,
                "Dollars per Barrel",
                concept_id,
                None,
                {},
                D.SHA,
                None,
                None,
                prov,
            )
            for d, v in (("2019-01-02", "54.06"), ("2019-06-03", "61.00"), ("2019-12-31", "67.77"))
        ]
        pin = {
            "dataset_key": "atlas.fixture_eia.spot_d",
            "artifact_sha256": "a" * 64,
            "recorded_at": "2026-10-17T15:42:35+00:00",
        }
        return cells, [pin]


def test_registered_table_fallback_is_labelled() -> None:
    tables = FakeTables()
    j = ask("Prix du Brent en 2019", D.backends(tables=tables))
    t = " ".join(s["text"] for s in j["sentences"])
    assert tables.calls and tables.calls[0][1] == [("fixture_eia", "spot_d", "RBRTE")]
    assert "Lu hors de l'instantané atlas courant" in t and "atlas.fixture_eia.spot_d" in t
    assert "première (2019-01-02) : 54,06" in t and "aucune moyenne calculée" in t
    cell = next(e for e in j["evidence"] if e["kind"] == "atlas_cell")
    assert cell["citation"]["outside_snapshot"] is True
    b = B.build(j, "Prix du Brent en 2019")
    assert b["how"]["as_of"]["registered_tables"][0]["dataset_key"] == "atlas.fixture_eia.spot_d"
    assert b["headline"]["kind"] == "number" and b["headline"]["value"] == "67.77"


# --------------------------------------------------------------------------- bundle
def test_bundle_number_bars_sources_targets_pin() -> None:
    q = "Causes de décès en France"
    b = B.build(ask(q), q)
    assert b["status"] == "answered"
    h = b["headline"]
    assert h["kind"] == "number" and h["value"] == "23.26" and h["period"] == "2021" and h["iso3"] == "FRA"
    assert h["honesty"] == "observed" and h["more"] == 1
    assert b["viz"]["type"] == "bars" and [r["value"] for r in b["viz"]["rows"]] == ["23.26", "6.42"]
    src = b["sources"][0]
    assert src["source"] == "fixture_who/gho" and src["licence"] == "CC-BY-4.0" and src["non_commercial"] is False
    kinds = {t["kind"] for t in b["targets"]}
    assert {"monde", "comparer", "fiche"} <= kinds
    assert b["pin"]["kind"] == "note" and "/v8/demander?q=" in b["pin"]["markdown"]
    assert b["how"]["operations"][0]["op"] == "atlas_value" and b["how"]["periods"]["first"] == "2021-01-01"


def test_bundle_unknown_says_what_is_missing() -> None:
    q = "Que mange le tigre ?"
    b = B.build(ask(q), q)
    assert b["status"] == "unknown"
    assert b["headline"]["kind"] == "unknown" and b["headline"]["text"] == "REVA ne sait pas"
    assert b["missing"] and b["headline"]["detail"]
    assert any(f.get("brainstorm") is not None for f in b["followups"])


def test_bundle_forecast_estimates_scenario_path_graph() -> None:
    q = "Quelle est la prévision d'inflation pour la France ?"
    b = B.build(ask(q), q)
    assert b["viz"]["type"] == "forecast"
    m = b["viz"]["questions"][0]["models"][0]
    assert m["point"] == "1.9" and m["lo"] == "-0.4" and m["hi"] == "4.2"
    assert b["headline"]["honesty"] == "forecast" and b["headline"]["band"] == ["-0.4", "4.2"]
    assert any(t["kind"] == "scores" for t in b["targets"])
    q = "Quel est l'effet d'une hausse du pétrole sur l'inflation ?"
    b = B.build(ask(q), q)
    assert b["viz"]["type"] == "estimates" and len(b["viz"]["rows"]) == 2
    assert "jamais moyennées" in b["viz"]["note"]
    q = "Que se passe-t-il si la BCE monte ses taux de 50 pb ?"
    b = B.build(ask(q), q)
    assert b["viz"]["type"] == "scenario" and b["headline"]["honesty"] == "scenario"
    assert any(t["kind"] == "decision" and t["ref"] == "ecb_plus_50bp_2027q1" for t in b["targets"])
    q = "Quelles maladies causées par Mycobacterium tuberculosis ?"
    b = B.build(ask(q), q)
    assert b["viz"]["type"] == "path" and b["viz"]["hops"][0]["tier"] == "T1"
    q = "Que mange le loup ?"
    b = B.build(ask(q), q)
    assert b["viz"]["type"] == "graph" and b["viz"]["center"]["id"] == "fixture:wolf"


def test_service_series_reader_and_cache() -> None:
    svc = AskService(D.backends(), CAT)
    a = asyncio.run(svc.answer("Prix du Brent en 2020"))
    assert a["viz"]["type"] == "timeseries"
    pts = a["viz"]["series"][0]["points"]
    assert [p["v"] for p in pts] == ["67.05", "70.25", "9.12", "51.22"] and all(p["cited"] for p in pts)
    assert a["cached"] is False and asyncio.run(svc.answer("Prix du Brent en 2020"))["cached"] is True


# --------------------------------------------------------------------------- brainstorm
def test_parse_target() -> None:
    p = parse_target("prévoir l'inflation française à 4 trimestres", CAT)
    assert p["iso3"] == "FRA" and p["horizon"] == {"n": 4, "unit": "trimestres"} and p["text"] == "inflation"
    p = parse_target("prévoir le prix du Brent", CAT)
    assert p["iso3"] is None and p["text"] == "prix du Brent"


def test_brainstorm_inflation_fixture() -> None:
    out = Brainstormer(D.backends(), CAT).run("prévoir l'inflation française à 4 trimestres")
    assert out["known"] is True
    assert [f["key"] for f in out["anchors"]["families"]][:1] == ["weo.cpi_inflation"]
    assert out["drivers"], out
    d = out["drivers"][0]
    assert d["n"] >= 1 and d["references"] and "data" in d
    fc = out["forecasts"][0]
    assert fc["covers"] is True and fc["n_questions"] >= 1 and fc["models"]
    assert out["next_steps"] and all(s["text"] for s in out["next_steps"])
    assert "aucun modèle de langage" in out["method"]


def test_brainstorm_unknown_target() -> None:
    out = Brainstormer(D.backends(), CAT).run("prévoir le vol des licornes")
    assert out["known"] is False and out["drivers"] == [] and out["forecasts"] == []
    assert out["next_steps"][0]["kind"] == "registre"


# --------------------------------------------------------------------------- routes
@pytest.fixture()
def client() -> Iterator[Any]:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from reva.api.auth import Principal
    from reva.api.errors import ApiProblem
    from reva.api.routes import v8_ask as V

    class _Auth:
        def authenticate(self, request: Request) -> Principal:
            return Principal(username="t", actor="token:t", method="token", scopes=frozenset({"read"}))

    app = FastAPI()
    app.state.auth = _Auth()
    app.state.nlqa_backends = D.backends()
    app.state.nlqa_catalog = CAT

    @app.exception_handler(ApiProblem)
    async def _problem(request: Request, exc: ApiProblem) -> Any:
        return JSONResponse({"code": exc.code}, status_code=exc.http_status)

    app.include_router(V.router)
    with TestClient(app) as c:
        yield c


def test_routes(client: Any) -> None:
    r = client.post("/v1/v8/ask/answer", json={"question": "Causes de décès en France"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "answered" and j["headline"]["kind"] == "number" and j["sentences"] and j["evidence"]
    r = client.post("/v1/v8/ask/answer", json={"question": "x", "extra": 1})
    assert r.status_code == 422
    r = client.get("/v1/v8/ask/brainstorm", params={"target": "prévoir l'inflation française"})
    assert r.status_code == 200 and r.json()["anchors"]["families"]
    r = client.get("/v1/v8/ask/examples")
    assert r.status_code == 200 and len(r.json()["questions"]) >= 8


# --------------------------------------------------------------------------- fixes found on the live data
def test_statement_read_against_its_direction_names_the_right_subject() -> None:
    """water ← « part of » — hydrogen, read as water « comprend » hydrogen: the subject is water, not hydrogen."""
    has_part = {"fr": "comprend", "en": "has part"}
    edge = (950, "fixture:hydrogen", "fixture:part_of", "fixture:water", "part_of", has_part, "has_part", has_part,
            False, "fixture.wikidata", D.CC0, None)  # fmt: skip
    fams = D.FakeFamilies(edges=[*D.EDGES, edge])
    j = ask("Composition chimique de l'eau ?", D.backends(families=fams))
    t = " ".join(s["text"] for s in j["sentences"])
    assert "eau — « comprend »" in t and "hydrogène — « comprend »" not in t


class CoalKg(D.FakeKgDomains):
    """« charbon » resolves to a record without plants; its English label "coal" is carried by the GPPD fuel."""

    WD = D.ent("fixture:coal_wd", "charbon", "coal", "wikidata_item")

    def search_entities(self, text: str, *, kind, lang, limit):  # type: ignore[no-untyped-def]
        if text.strip().lower() == "charbon":
            return [EntityMatch(self.WD, "label_exact", "charbon")]
        return super().search_entities(text, kind=kind, lang=lang, limit=limit)

    def label_bridges(self, qid: str, exclude: Any = ()) -> list[tuple[Any, str, str]]:
        return [(D.E["fixture:coal"], "en", "coal")] if qid == "fixture:coal_wd" else []


def test_cross_list_reads_the_bridged_record_and_says_so() -> None:
    j = ask("Quelles centrales au charbon en Inde ?", D.backends(kg=CoalKg()))
    t = " ".join(s["text"] for s in j["sentences"])
    assert "CENTRALE UN" in t and "CENTRALE GAZ" not in t
    assert "fiche distincte portant le libellé anglais « coal »" in t
