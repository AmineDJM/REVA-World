"""/v1/predict/* on a throwaway database (PRED-ENGINE): reference decisions, a composed decision propagated
through an effect estimate stored in ``kg.effect_estimate`` (SYNTHETIC REVA-run row, origin test_fixture, source
key ``test.predict``: not a real-world effect), typed gaps of the simulators on an empty release, problem details.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from reva.settings import Settings

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def api(db) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    from reva.api.app import create_app
    from reva.api.auth import new_api_token
    from reva.effects import EffectEstimateIn, Horizon, RevaRun, Uncertainty, load_effect_estimates

    rep = load_effect_estimates(
        db.worker,
        db.store,
        source_kind="reva_run",
        source_key="test.predict",
        dataset="synthetic_oil_cpi",
        raw_sha256s=[],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
        complete=True,
        estimates=[
            EffectEstimateIn(
                estimate_id="oil_cpi_synthetic",
                cause_kind="variable",
                cause_id="oil_price.global",
                cause_definition="synthetic: global oil price, percent change",
                cause_transform="percent_change",
                effect_kind="atlas_concept",
                effect_id="imf.inflation_cpi_avg",
                effect_definition="synthetic: CPI inflation, percentage points",
                effect_transform="pp_change",
                measure="response",
                unit="percentage_points",
                shock_size=10,
                shock_unit="percent",
                sign="positive",
                point=0.5,
                point_kind="point",
                uncertainty=Uncertainty(se=0.1, status="reported"),
                horizon=Horizon(kind="impact", value=0, unit="quarter"),
                scope={"countries": ["FRA"], "period": {"from": "2000", "to": "2020"}},
                method="local_projection",
                as_of="2026-01-01",
                run=RevaRun(run_id="test-run-1", code_version="test", data_snapshot="none", as_of="2026-01-01"),
                applicability="REVA note: synthetic test row",
            )
        ],
    )
    assert rep.inserted == 1, rep
    tok, sha = new_api_token()
    with db.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'predict-read')"),
            {"h": sha, "s": ["read"]},
        )
    settings = Settings.model_validate(
        {
            **db.settings.model_dump(),
            "reva_session_secret": SecretStr("s" * 48),
            "reva_allowed_hosts": "testserver,localhost",
            "reva_cors_origins": "",
        }
    )
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            yield client, tok
    finally:
        app.state.engine.dispose()


def post(api: tuple[TestClient, str], body: dict[str, Any]) -> Any:
    client, tok = api
    return client.post("/v1/predict/decision", json=body, headers={"Authorization": f"Bearer {tok}"})


def test_reference_decisions_and_instruments(api: tuple[TestClient, str]) -> None:
    client, tok = api
    r = client.get("/v1/predict/decisions", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert len(d["decisions"]) == 5 and d["label"] == "scénario non validé — pas une probabilité"
    assert {i["id"] for i in d["instruments"]} >= {"policy_rate_change", "oil_production_quota_change"}
    assert client.get("/v1/predict/decisions").status_code == 401


def test_composed_decision_propagates_through_a_stored_estimate(api: tuple[TestClient, str]) -> None:
    decision = {
        "id": "test_oil_10",
        "label_fr": "Hypothèse : pétrole +10 %",
        "actor": {"label_fr": "Utilisateur"},
        "instrument": "oil_price_assumption",
        "target": {"label_fr": "Prix mondial du brut"},
        "magnitude": {"value": "10", "unit": "percent"},
        "start": "2027Q1",
        "horizon_quarters": 4,
        "countries": ["FRA", "DEU"],
    }
    r = post(api, {"decision": decision, "draws": 4000, "seed": 3, "origin": "test_fixture"})
    assert r.status_code == 200, r.text
    body = r.json()
    data = body["data"]
    assert data["label"] == "scénario non validé — pas une probabilité"
    assert data["effect_layer"]["layer"] == "present"
    assert body["modality"] == "assumption_conditioned_scenario"
    assert {w["code"] for w in body["warnings"]} >= {"unvalidated_scenario", "not_financial_advice"}
    fra = next(v for v in data["variables"] if v["id"] == "atlas_concept:imf.inflation_cpi_avg@FRA")
    assert fra["periods"][0]["p50"] == pytest.approx(0.5, abs=0.01)  # 0.5 pp per 10 %, SE 0.1
    assert fra["periods"][1]["missing_reason"].startswith("lag_not_published")
    hop = next(h for h in data["hops"] if h["kind"] == "estimate")
    assert hop["estimate_key"] == "run:test-run-1:oil_cpi_synthetic" and hop["source"]["source_kind"] == "reva_run"
    # DEU is outside the estimate's scope: excluded with a reason, never given a number
    assert not [v for v in data["variables"] if v["id"].endswith("@DEU") and "inflation" in v["id"]]
    assert any(x["code"] == "out_of_scope" and x["geo"] == "DEU" for x in data["excluded_links"])
    # simulators: no published release on this database -> typed gaps
    assert {g["code"] for g in data["gaps"]} >= {"plugin_data_missing"}


def test_reference_decision_reports_gaps_not_numbers(api: tuple[TestClient, str]) -> None:
    r = post(api, {"reference_id": "france_retirement_65_2027", "draws": 200})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["variables"] == []
    assert any(g["code"] == "plugin_data_missing" for g in data["gaps"])
    r = post(api, {"reference_id": "ecb_plus_50bp_2027q1", "draws": 200})
    assert any(g["code"] == "no_effect_estimate_for_cause" for g in r.json()["data"]["gaps"])


def test_problems(api: tuple[TestClient, str]) -> None:
    assert post(api, {"reference_id": "nope"}).status_code == 404
    assert post(api, {}).status_code == 422
    assert post(api, {"reference_id": "ecb_plus_50bp_2027q1", "draws": 10}).status_code == 422
