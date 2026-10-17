"""V8DECISION: decision-studio bundles, sensitivity, comparison, brief and API on SYNTHETIC estimates (source key
``test.synthetic``; invented for the test, never loaded anywhere) through the V5 engine's static ports. The live
path against the dev DB is in tests/integration/v8/test_v8_decision_live_db.py."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from reva.api.auth import Principal
from reva.kernel.errors import RevaError
from reva.predict.context import NoBaseline, NoClaims, PluginData, StaticEffectSource
from reva.predict.decision import Decision, reference_decisions
from reva.predict.engine import LABEL_UNVALIDATED
from reva.v8.decision import bundle as B
from reva.v8.decision import runner as rn
from reva.v8.decision import service as sv
from reva.v8.decision.brief import brief_markdown
from reva.v8.decision.templates import templates, variant

Z = rn.OAT_Z
RATE = ("atlas_concept", "imf.policy_rate", "pp_change")
INFL = ("atlas_concept", "imf.inflation_cpi_avg", "pp_change")
UNEMP = ("atlas_concept", "imf.unemployment_rate", "pp_change")
QUOTE = "a 1 pp rise in the policy rate lowers inflation by 0.5 pp (synthetic, test only)"


def decision(**over: Any) -> Decision:
    doc: dict[str, Any] = {
        "id": "test_rate",
        "label_fr": "Décision synthétique",
        "actor": {"label_fr": "Banque centrale fictive"},
        "instrument": "policy_rate_change",
        "target": {"label_fr": "Taux directeur"},
        "magnitude": {"value": "50", "unit": "bp"},
        "start": "2027Q1",
        "duration_quarters": None,
        "horizon_quarters": 4,
        "countries": ["FRA"],
    }
    doc.update(over)
    return Decision.model_validate(doc)


def est(key: str, cause: tuple[str, str, str], effect: tuple[str, str, str], **over: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "estimate_key": f"pub:test.synthetic:{key}",
        "cause": {"kind": cause[0], "id": cause[1], "transform": cause[2], "definition": "synthetic cause"},
        "effect": {
            "kind": effect[0],
            "id": effect[1],
            "transform": effect[2],
            "definition": "synthetic effect",
            "label_fr": f"effet {effect[1]}",
        },
        "measure": "slope",
        "unit": "pp_per_pp",
        "shock": None,
        "sign": "negative",
        "point": -0.5,
        "point_kind": "point",
        "uncertainty": {"se": 0.1, "status": "reported"},
        "horizon": {"kind": "long_run", "value": None, "unit": None},
        "irf": [],
        "scope": {"countries": ["FRA"], "period": {"from": "2009", "to": "2024"}},
        "method": "local_projection",
        "source": {
            "kind": "publication",
            "source_key": "test.synthetic",
            "citation": {"text": "Synthetic, A. (2024). A test paper.", "short": "Synthetic 2024", "doi": "10.0/test"},
            "locator": "Table 1, p. 3",
            "quote": QUOTE,
            "rights": {"licence": "test only", "restricted": True},
        },
        "as_of": "2024-01-01",
        "wording_fr": "Selon une source synthétique, … ; REVA n'établit pas cet effet.",
    }
    e.update(over)
    return e


def ctx(ests: list[dict[str, Any]], data: PluginData | None = None) -> rn.Contexts:
    return rn.static_contexts(StaticEffectSource(ests), NoClaims(), NoBaseline(), data)


def run_bundle(d: Decision, ests: list[dict[str, Any]], **kw: Any) -> tuple[dict[str, Any], rn.Computed, bool]:
    rn.RUNS.clear()
    c = ctx(ests)
    return sv.run(sv.Scenario(d, draws=kw.pop("draws", 2000), seed=kw.pop("seed", 0)), c.fingerprint, lambda _d: c)


# ------------------------------------------------------------------------------------------- templates
def test_templates_expose_the_five_references_instruments_and_schema_limits() -> None:
    t = templates()
    assert sorted(r["id"] for r in t["references"]) == sorted(reference_decisions())
    assert t["label"] == LABEL_UNVALIDATED
    ins = {i["id"]: i for i in t["instruments"]}
    assert ins["policy_rate_change"]["needs"]["countries"] is True
    assert ins["export_ban"]["needs"] == {
        "actor_geo": True,
        "target_geo": False,
        "countries": False,
        "occurrence": True,
        "level": False,
    }
    assert ins["statutory_retirement_age"]["needs"]["level"] is True
    assert t["schema"]["horizon_max"] == 12 and t["schema"]["countries_max"] == 12
    # presets are variants of references (one field changed), valid decisions, labelled as variants
    for p in t["compare_presets"]:
        a = Decision.model_validate(p["a"])
        b = reference_decisions()[p["b_reference_id"]]
        assert a.id.endswith(".variant") and "variante" in a.label_fr
        assert a.magnitude.value != b.magnitude.value and a.instrument == b.instrument
    assert any(p["id"] == "ecb_25_vs_50" for p in t["compare_presets"])


# ------------------------------------------------------------------------------------------- bundle
def test_bundle_graph_edges_carry_the_estimate_as_published_and_its_evidence() -> None:
    e1 = est("rate_infl", RATE, INFL)
    e2 = est("infl_unemp", INFL, UNEMP, point=0.3, sign="positive", uncertainty={"status": "not_reported"})
    b, _comp, hit = run_bundle(decision(), [e1, e2])
    assert not hit and b["honesty"]["label"] == LABEL_UNVALIDATED and not b["honesty"]["validated"]
    g = b["graph"]
    kinds = {n["id"]: n["kind"] for n in g["nodes"]}
    assert kinds["decision"] == "decision" and kinds["atlas_concept:imf.policy_rate@FRA"] == "direct"
    est_edges = [e for e in g["edges"] if e["kind"] == "estimate"]
    assert [e["estimate_key"] for e in est_edges] == ["pub:test.synthetic:rate_infl", "pub:test.synthetic:infl_unemp"]
    first = est_edges[0]
    assert first["effect"]["beta"] == -0.5 and first["effect"]["beta_sd"] == 0.1
    lo, hi = first["effect"]["beta_ci90"]
    assert lo == pytest.approx(-0.5 - Z * 0.1) and hi == pytest.approx(-0.5 + Z * 0.1)
    assert first["source_short"] == "Synthetic 2024" and first["method"] == "local_projection"
    assert est_edges[1]["effect"]["beta_ci90"] is None and not est_edges[1]["effect"]["has_uncertainty"]
    ev = b["evidence"]["pub:test.synthetic:rate_infl"]
    assert ev["quote"] == QUOTE and ev["locator"] == "Table 1, p. 3" and ev["doi"] == "10.0/test" and ev["restricted"]
    assert ev["published_uncertainty_fr"].startswith("écart-type publié")
    assert b["evidence"]["pub:test.synthetic:infl_unemp"]["published_uncertainty_fr"].startswith(
        "incertitude non publiée"
    )
    # headline: undated chain values from the engine, the second flagged incomplete
    rows = {r["id"]: r for r in b["headline"]}
    infl = rows["atlas_concept:imf.inflation_cpi_avg@FRA"]
    assert infl["value"]["p50"] == pytest.approx(-0.25, abs=0.01) and infl["flags"] == []
    un = rows["atlas_concept:imf.unemployment_rate@FRA"]
    assert {f["code"] for f in un["flags"]} == {"incomplete"}
    assert [s["short"] for s in un["sources"]] == ["Synthetic 2024", "Synthetic 2024"]
    # the end of the chain is a chain end, not a blocking stop
    d = b["diagnosis"]
    assert d["has_result"] and not d["blocking"]
    assert [c["node"] for c in d["chain_ends"]] == ["atlas_concept:imf.unemployment_rate@FRA"]


def test_dated_profile_missing_lags_stay_missing_never_zero() -> None:
    irf = [{"h": 0, "unit": "quarter", "point": -0.4}, {"h": 2, "unit": "quarter", "point": -0.8}]
    e = est("irf", RATE, INFL, irf=irf, uncertainty={"status": "not_reported"}, horizon={"kind": "at_horizon"})
    b, _, _ = run_bundle(decision(), [e])
    row = next(r for r in b["headline"] if r["id"].startswith("atlas_concept:imf.inflation"))
    assert row["n_missing"] >= 1 and row["value"] is not None and row["value"]["period"] != "2027Q2"
    miss = b["diagnosis"]["missing_periods"][0]
    assert "2027Q2" in miss["periods"] and "jamais interpolé" in miss["reason_fr"]


def test_competing_estimates_side_by_side_never_averaged() -> None:
    exact = est("exact", RATE, INFL, point=-0.5)
    panel = est("panel", RATE, INFL, point=-2.0, scope={"countries": ["WLD"], "period": {"from": "1990", "to": "2020"}})
    b, _, _ = run_bundle(decision(), [exact, panel])
    infl = next(r for r in b["headline"] if r["id"].startswith("atlas_concept:imf.inflation"))
    assert infl["value"]["p50"] == pytest.approx(-0.25, abs=0.01)  # the exact-scope estimate only, not a mean
    [c] = b["conflicts"]
    assert c["kind"] == "alternatives"
    rows = {x["estimate_key"]: x for x in c["estimates"]}
    assert rows["pub:test.synthetic:exact"]["chosen"] and not rows["pub:test.synthetic:panel"]["chosen"]
    assert rows["pub:test.synthetic:panel"]["point"] == -2.0 and rows["pub:test.synthetic:panel"]["loaded"]
    edge = next(e for e in b["graph"]["edges"] if e["kind"] == "estimate")
    assert edge["n_alternatives"] == 1


def test_unresolved_tie_is_a_graph_stop_and_a_conflict_without_number() -> None:
    a = est("a_variant", RATE, INFL, point=-0.5)
    c = est("b_variant", RATE, INFL, point=-0.1)
    b, _, _ = run_bundle(decision(), [a, c])
    assert b["headline"] == []
    tie_nodes = [n for n in b["graph"]["nodes"] if n["kind"] == "tie"]
    assert len(tie_nodes) == 1
    tie_edge = next(e for e in b["graph"]["edges"] if e["kind"] == "tie")
    assert tie_edge["from"] == "atlas_concept:imf.policy_rate@FRA"
    [conf] = b["conflicts"]
    assert conf["kind"] == "tie" and not any(x["chosen"] for x in conf["estimates"])
    d = b["diagnosis"]
    assert not d["has_result"] and d["ties"] and "égalité" in d["headline_fr"]


def test_why_no_result_names_the_missing_link_and_the_near_estimates() -> None:
    # an export ban: no estimate has the ban itself as cause; a related cause exists (share of trade restricted)
    near = est(
        "share",
        ("variable", "trade.export_restriction_share", "pp_change"),
        ("variable", "prices.world_food", "percent_change"),
        scope={"countries": ["WLD"]},
    )
    d = decision(
        id="ban",
        instrument="export_ban",
        actor={"label_fr": "Pays fictif", "geo": "IND"},
        magnitude={"value": "1", "unit": "share_of_exports_banned"},
        countries=["IND"],
    )
    b, _, _ = run_bundle(d, [near])
    diag = b["diagnosis"]
    assert not diag["has_result"] and diag["headline_fr"].startswith("Aucun résultat chiffré")
    [blk] = diag["blocking"]
    assert blk["node"] == "variable:trade.export_restriction@IND"
    assert "trade.export_restriction" in blk["missing_fr"]
    assert [n["estimate_key"] for n in blk["near"]] == ["pub:test.synthetic:share"]
    stop = next(n for n in b["graph"]["nodes"] if n["kind"] == "stop")
    assert stop["id"] == "stop:variable:trade.export_restriction@IND"


def test_level_target_and_blocked_model_are_explained() -> None:
    d = reference_decisions()["france_retirement_65_2027"]
    pd = PluginData(retirement_gap="no pinned retirement inputs (test)")
    c = ctx([est("age_emp", ("variable", "pension.minimum_pension_age", "level_change"), UNEMP)], pd)
    rn.RUNS.clear()
    b, _, _ = sv.run(sv.Scenario(d, draws=200), c.fingerprint, lambda _d: c)
    diag = b["diagnosis"]
    assert not diag["has_result"]
    assert diag["plugins"] and diag["plugins"][0]["plugin"].startswith("social.retirement_age")
    lvl = next(o for o in diag["other"] if o["code"] == "level_target")
    assert [n["estimate_key"] for n in lvl["near"]] == ["pub:test.synthetic:age_emp"]
    assert any(n["kind"] == "plugin" and n.get("blocked") for n in b["graph"]["nodes"])


# ------------------------------------------------------------------------------------------- cache
def test_runs_are_cached_by_input_hash_and_data_fingerprint() -> None:
    rn.RUNS.clear()
    c = ctx([est("rate_infl", RATE, INFL)])
    calls = []

    def make(d: Decision) -> rn.Contexts:
        calls.append(d.id)
        return c

    sc = sv.Scenario(decision())
    _, _, h1 = sv.run(sc, "fp1", make)
    _, _, h2 = sv.run(sc, "fp1", make)
    _, _, h3 = sv.run(sv.Scenario(decision(), seed=1), "fp1", make)
    _, _, h4 = sv.run(sc, "fp2", make)
    assert (h1, h2, h3, h4) == (False, True, False, False) and len(calls) == 3
    assert sc.hash == sv.Scenario(decision()).hash != sv.Scenario(decision(), draws=500).hash


# ------------------------------------------------------------------------------------------- sensitivity
def test_one_at_a_time_tornado_is_exact_for_a_linear_chain_and_skips_unpublished_uncertainty() -> None:
    e1 = est("rate_infl", RATE, INFL)  # -0.5 ± 0.1 per pp; decided +0.5 pp
    e2 = est("infl_unemp", INFL, UNEMP, point=0.3, sign="positive", uncertainty={"status": "not_reported"})
    rn.RUNS.clear()
    rn.SENSITIVITY.clear()
    c = ctx([e1, e2])
    sc = sv.Scenario(decision(), draws=4000)
    out, hit = sv.sensitivity(sc, c.fingerprint, lambda _d: c)
    assert not hit and out["method"] == "one_at_a_time" and out["runs"] == 1 + 2 * 2
    v = {x["id"]: x for x in out["variables"]}
    infl = v["atlas_concept:imf.inflation_cpi_avg@FRA"]
    [bar] = infl["bars"]
    assert infl["center"]["undated"] == pytest.approx(-0.25)
    assert bar["low"]["undated"] == pytest.approx((-0.5 - Z * 0.1) * 0.5, rel=1e-5)
    assert bar["high"]["undated"] == pytest.approx((-0.5 + Z * 0.1) * 0.5, rel=1e-5)
    assert bar["mc_r2"]["undated"] == pytest.approx(1.0, abs=1e-6)  # one estimate explains all the variance
    un = v["atlas_concept:imf.unemployment_rate@FRA"]
    assert [b["estimate_key"] for b in un["bars"]] == ["pub:test.synthetic:rate_infl"]
    assert [f["estimate_key"] for f in un["no_uncertainty"]] == ["pub:test.synthetic:infl_unemp"]
    assert sv.sensitivity(sc, c.fingerprint, lambda _d: c)[1] is True


# ------------------------------------------------------------------------------------------- compare
def test_compare_pairs_draws_with_common_random_numbers() -> None:
    rn.RUNS.clear()
    c = ctx([est("rate_infl", RATE, INFL)])
    a = sv.Scenario(decision(magnitude={"value": "25", "unit": "bp"}))
    b = sv.Scenario(decision())
    out = sv.compare(a, b, c.fingerprint, lambda _d: c)
    assert out["paired"] and out["changed_fields"] == ["magnitude"]
    row = next(r for r in out["variables"] if r["id"] == "atlas_concept:imf.inflation_cpi_avg@FRA")
    [p] = row["periods"]
    # B = 2 A draw by draw (same estimate draw in both runs), so B - A = A exactly
    assert p["diff"]["p50"] == pytest.approx(p["a"]["p50"]) and p["diff"]["p05"] == pytest.approx(p["a"]["p05"])
    assert p["b"]["p50"] == pytest.approx(2 * p["a"]["p50"])
    other = sv.compare(a, sv.Scenario(decision(), seed=3), c.fingerprint, lambda _d: c)
    assert not other["paired"] and other["variables"][0]["periods"][0]["diff"] is None


# ------------------------------------------------------------------------------------------- brief
def test_brief_carries_label_citations_quotes_and_never_a_probability() -> None:
    e1 = est("rate_infl", RATE, INFL)
    alt = est("panel", RATE, INFL, point=-2.0, scope={"countries": ["WLD"]})
    rn.RUNS.clear()
    rn.SENSITIVITY.clear()
    c = ctx([e1, alt])
    sc = sv.Scenario(decision())
    out = sv.brief(sc, c.fingerprint, lambda _d: c, url="/v8/decision?ref=test", with_sensitivity=True)
    md = out["markdown"]
    assert md.startswith("# Décision synthétique")
    assert LABEL_UNVALIDATED in md and "pas un conseil financier" in md
    assert f"« {QUOTE} »" in md and "Table 1, p. 3" in md and "10.0/test" in md
    assert "pub:test.synthetic:panel" in md and "jamais moyennées" in md
    assert "/v8/decision?ref=test" in md and "## Sensibilité" in md
    assert out["filename"].endswith(".md") and out["n_citations"] == 2


def test_brief_markdown_escapes_table_cells() -> None:
    bundle = {
        "result": {"decision": {"label_fr": "x"}, "label": LABEL_UNVALIDATED},
        "honesty": {},
        "headline": [
            {
                "label_fr": "a|b",
                "geo_fr": "FRA",
                "value": None,
                "unit_fr": "%",
                "sources": [],
                "models": [],
                "flags": [],
            }
        ],
    }
    md = brief_markdown(bundle)
    assert "a\\|b" in md and "manquant" in md


def test_note_translation_and_short_sources() -> None:
    assert B.note_fr(
        "estimated on data up to 2001; the decision starts in 2027 (more than 20 years before)"
    ).startswith("estimée sur des données jusqu'en 2001")
    assert B.short_source(None, "pub:effects.literature:caldara_cavallo_iacoviello_2016:x") == "Caldara et al. 2016"
    assert B.short_source(None, "pub:effects.literature:baumeister_hamilton_2018:x") == "Baumeister & Hamilton 2018"
    assert B.short_source(None, "run:es-1b3eff04653ec3971c9c:h5").startswith("REVA · calcul es-1b3eff")
    assert B.fmt_num(-0.25) == "−0,25" and B.fmt_num(12.5) == "+12,5" and B.fmt_num(None) == "?"


# ------------------------------------------------------------------------------------------- API
class _Auth:
    def authenticate(self, request: Request) -> Principal:
        return Principal(username="t", actor="token:t", method="token", scopes=frozenset({"read"}))


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from reva.api.routes import v8_decision as V

    ests = [est("rate_infl", RATE, INFL), est("panel", RATE, INFL, point=-2.0, scope={"countries": ["WLD"]})]
    c = ctx(ests)

    class _Conn:
        def execute(self, *a: Any, **k: Any) -> None:
            return None

    @contextmanager
    def fake_conn(request: Request) -> Iterator[_Conn]:
        yield _Conn()

    monkeypatch.setattr(V, "read_conn", fake_conn)
    monkeypatch.setattr(V.rn, "data_fingerprint", lambda conn: "fp-test")
    monkeypatch.setattr(V, "_factory", lambda conn, request, sc: lambda d: c)
    monkeypatch.setattr(V, "_store", lambda request: None)
    monkeypatch.setattr(V.rn, "start_warmup", lambda engine, store: False)
    rn.RUNS.clear()
    rn.SENSITIVITY.clear()
    app = FastAPI()
    app.state.auth = _Auth()
    app.state.engine = lambda: None

    @app.exception_handler(RevaError)
    async def _problem(request: Request, exc: RevaError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse({"code": exc.code}, status_code=exc.http_status)

    app.include_router(V.router)
    with TestClient(app) as cl:
        yield cl


def test_api_templates_run_sensitivity_compare_brief(client: TestClient) -> None:
    t = client.get("/v1/v8/decision/templates")
    assert t.status_code == 200, t.text
    assert len(t.json()["data"]["references"]) == 5 and t.json()["data"]["schema"]["horizon_max"] == 12

    body = {"decision": decision().model_dump(mode="json"), "draws": 500}
    r = client.post("/v1/v8/decision/run", json=body)
    assert r.status_code == 200, r.text
    env = r.json()
    assert env["modality"] == "assumption_conditioned_scenario"
    assert {w["code"] for w in env["warnings"]} >= {"unvalidated_scenario", "not_financial_advice"}
    d = env["data"]
    assert d["result"]["label"] == LABEL_UNVALIDATED and not d["cached"]
    assert d["graph"]["edges"] and d["conflicts"] and d["evidence"]
    assert client.post("/v1/v8/decision/run", json=body).json()["data"]["cached"] is True

    s = client.post("/v1/v8/decision/sensitivity", json=body)
    assert s.status_code == 200, s.text and s.json()["data"]["variables"]
    cmp = client.post(
        "/v1/v8/decision/compare",
        json={
            "a": {**body, "decision": decision(magnitude={"value": "25", "unit": "bp"}).model_dump(mode="json")},
            "b": body,
        },
    )
    assert cmp.status_code == 200, cmp.text and cmp.json()["data"]["paired"]
    br = client.post("/v1/v8/decision/brief", json={**body, "url": "/v8/decision?i=policy_rate_change"})
    assert br.status_code == 200, br.text and "« " in br.json()["data"]["markdown"]


def test_api_refuses_bad_input(client: TestClient) -> None:
    assert client.post("/v1/v8/decision/run", json={"reference_id": "nope"}).status_code == 404
    assert client.post("/v1/v8/decision/run", json={}).status_code == 422
    bad = decision().model_dump(mode="json")
    bad["start"] = "2027-01"
    assert client.post("/v1/v8/decision/run", json={"decision": bad}).status_code == 422
    assert client.post("/v1/v8/decision/brief", json={"reference_id": "x", "url": "https://evil"}).status_code == 422


def test_variant_keeps_reference_fields() -> None:
    ecb = reference_decisions()["ecb_plus_50bp_2027q1"]
    v = variant(ecb, id="x.variant", label_fr="v", magnitude_value="25")
    assert v.magnitude.value == "25" and v.countries == ecb.countries and v.instrument == ecb.instrument
