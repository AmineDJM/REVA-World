"""HTTP API acceptance against a real database through the reva_api role.

The module runs in its own freshly bootstrapped database (created from the
session ``db`` fixture's settings) so its fixture ingestion and releases never
collide with other suites sharing the session database.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.api.app import create_app
from reva.api.auth import hash_password
from reva.belief.publication import ReleaseResult, publish_release
from reva.evidence.runner import FIXTURE_ROOT, FixtureHttp, post_publish_for, run_connector
from reva.kernel.context import Origin
from reva.settings import Settings

pytestmark = pytest.mark.db

ADMIN_PASSWORD = "correct horse battery staple"
EXAMPLE = Path(__file__).resolve().parents[2] / "contracts" / "scenario.example.json"
WB = "worldbank.wdi.indicators"
POP = "demography.population.wdi_midyear_total"
CRUDE_M = "trade.hs2709.import_value.world_total"


@dataclass
class Env:
    settings: Settings
    owner: Engine
    worker: Engine
    store: FilesystemArtifactStore
    r1: ReleaseResult
    r2: ReleaseResult
    t_between_revisions: datetime
    t_between_releases: datetime
    old_pop: str
    new_pop: str
    client: TestClient


def api_settings(base: Settings, **over: Any) -> Settings:
    data = base.model_dump()
    data.update(
        reva_admin_password_hash=SecretStr(hash_password(ADMIN_PASSWORD)),
        reva_session_secret=SecretStr("s" * 48),
        reva_allowed_hosts="testserver,localhost",
        reva_cors_origins="",
        reva_env="test",
    )
    data.update(over)
    return Settings.model_validate(data)


def login(client: TestClient) -> None:
    r = client.post("/v1/session", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("reva_csrf")


def _pop_override(new_value: int) -> tuple[dict[str, bytes], str]:
    d = FIXTURE_ROOT / WB
    entry = next(e for e in json.loads((d / "manifest.json").read_text()) if "SP.POP.TOTL?" in e["url"])
    body = json.loads((d / entry["file"]).read_bytes())
    old = None
    for row in body[1]:
        if row["countryiso3code"] == "DZA" and row["date"] == "2023":
            old = row["value"]
            row["value"] = new_value
    assert old is not None
    return {entry["request_fingerprint"]: json.dumps(body).encode()}, str(old)


async def _ingest(name: str, worker: Engine, store: FilesystemArtifactStore, http: Any = None) -> None:
    c = builtin_connectors()[name]
    spec = c.describe()
    rep = await run_connector(c, http or FixtureHttp(spec.name), store, worker, persist_cursor=False,
                              post_publish=post_publish_for(spec.template))
    assert not rep.errors, rep.errors


@pytest.fixture(scope="module")
def env(db: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    import asyncio

    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    name = f"reva_test_api_{uuid.uuid4().hex[:8]}"
    admin = create_engine(db.settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    root = tmp_path_factory.mktemp("api_objects")
    url = db.settings.migration_url().set(database=name).render_as_string(hide_password=False)
    base = Settings.model_validate({**db.settings.model_dump(), "reva_migration_database_url": SecretStr(url),
                                    "reva_db_name": name, "reva_object_root": root})
    bootstrap(base)
    store = FilesystemArtifactStore(root, base.reva_s3_prefix)
    owner, worker = make_engine("migration", base), make_engine("worker", base)
    seed(worker, store)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ingest(WB, worker, store))
        loop.run_until_complete(_ingest("un.comtrade.public_exact", worker, store))
        loop.run_until_complete(_ingest("naturalearth.admin0_110m", worker, store))
        r1 = publish_release(worker, store, cutoff=datetime.now(UTC), origin=Origin.test_fixture, environment="test")
        t_between_revisions = datetime.now(UTC)
        overrides, old = _pop_override(46_999_999)
        loop.run_until_complete(_ingest(WB, worker, store, FixtureHttp(WB, overrides=overrides)))
        t_between_releases = datetime.now(UTC)
        r2 = publish_release(worker, store, cutoff=datetime.now(UTC), origin=Origin.test_fixture, environment="test")
    finally:
        loop.close()
    app = create_app(api_settings(base))
    with TestClient(app) as client:
        login(client)
        yield Env(base, owner, worker, store, r1, r2, t_between_revisions, t_between_releases, old, "46999999",
                  client)
    app.state.engine.dispose()
    for e in (owner, worker):
        e.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def problem(r: Any, status: int, code: str | None = None) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    if code:
        assert body["code"] == code, body
    return body


# ---------------------------------------------------------------------------


def test_health_ready(env: Env) -> None:
    r = env.client.get("/health/ready")
    assert r.status_code == 200, r.text
    assert {c["name"] for c in r.json()["checks"]} >= {"database", "migrations", "runtime_role"}


def test_entities_list_and_detail(env: Env) -> None:
    r = env.client.get("/v1/entities", params={"kind": "entity.country", "limit": 1000})
    assert r.status_code == 200, r.text
    labels = {e["label"] for e in r.json()["data"]}
    assert {"France", "Algeria", "China", "United States"} <= labels
    first = env.client.get("/v1/entities", params={"limit": 2})
    assert len(first.json()["data"]) == 2 and first.json()["next_cursor"]
    second = env.client.get("/v1/entities", params={"limit": 2, "cursor": first.json()["next_cursor"]})
    assert {e["id"] for e in second.json()["data"]}.isdisjoint({e["id"] for e in first.json()["data"]})
    assert env.client.get("/v1/entities", params={"limit": 1001}).status_code == 422

    d = env.client.get("/v1/entities/FRA")
    assert d.status_code == 200, d.text
    body = d.json()["data"]
    assert body["label"] == "France"
    assert {"provider": "comtrade_reporter", "external_key": "251"}.items() <= next(
        a for a in body["aliases"] if a["provider"] == "comtrade_reporter").items()
    comps = {(c["component"], c["concept_id"]) for c in body["components"]}
    assert ("observations", POP) in comps and ("estimates", POP) in comps
    assert {c["mode"] for c in body["supported_contexts"]} >= {"evidence", "belief"}
    problem(env.client.get("/v1/entities/XXX"), 404, "not_found")


def test_dza_crude_import_belief_is_typed_missing(env: Env) -> None:
    r = env.client.get("/v1/beliefs", params={"release_id": str(env.r1.release_id), "subject_id": "DZA",
                                              "concept": CRUDE_M, "period": 2023})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["data"]) == 1
    v = body["data"][0]["value"]
    assert v["kind"] == "missing" and v["reason"] == "unavailable" and "value" not in v
    assert body["origin"] == "test_fixture"
    assert any(w["code"] == "fixture_data" for w in body["warnings"])
    assert any(w["code"] == "missing_values" for w in body["warnings"])
    assert body["baseline_release"]["id"] == str(env.r1.release_id)
    assert body["modality"] == "selected_estimate" and body["context_mode"] == "belief"
    assert body["uncertainty"]["status"] == "unquantified"


def test_beliefs_are_release_pinned_and_as_of(env: Env) -> None:
    def pop(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        r = env.client.get("/v1/beliefs", params={"subject_id": "DZA", "concept": POP, "period": 2023, **params})
        assert r.status_code == 200, r.text
        return r.json()["data"][0]["value"]["value"], r.json()

    v1, _b1 = pop({"release_id": str(env.r1.release_id)})
    assert v1 == env.old_pop  # pinned: the later revision never changes release 1
    v2, b2 = pop({})
    assert v2 == env.new_pop and b2["baseline_release"]["id"] == str(env.r2.release_id)
    v_mid, b_mid = pop({"as_of": iso(env.t_between_releases)})
    assert v_mid == env.old_pop and b_mid["baseline_release"]["id"] == str(env.r1.release_id)
    assert b_mid["baseline_release"]["sequence"] < b2["baseline_release"]["sequence"]
    problem(env.client.get("/v1/beliefs", params={"as_of": "2000-01-01T00:00:00Z"}), 404, "no_release")
    problem(env.client.get("/v1/belief-releases/" + str(uuid.uuid4())), 404)
    rel = env.client.get(f"/v1/belief-releases/{env.r1.release_id}")
    assert rel.status_code == 200
    assert rel.json()["data"]["estimate_count"] == env.r1.estimates
    crude = next(c for c in rel.json()["data"]["concepts"] if c["concept_id"] == CRUDE_M)
    assert crude["missing"] >= 4  # DZA 2020-2023 (the captured 2024 response has a value)
    lst = env.client.get("/v1/belief-releases", params={"as_of": iso(env.t_between_releases)})
    assert [d["id"] for d in lst.json()["data"]] == [str(env.r1.release_id)]
    # pagination over a pinned release
    p1 = env.client.get("/v1/beliefs", params={"release_id": str(env.r1.release_id), "limit": 50})
    p2 = env.client.get("/v1/beliefs", params={"release_id": str(env.r1.release_id), "limit": 50,
                                               "cursor": p1.json()["next_cursor"]})
    assert {d["id"] for d in p1.json()["data"]}.isdisjoint({d["id"] for d in p2.json()["data"]})


def test_timeseries_revision_filtering(env: Env) -> None:
    base = {"subject_id": "DZA", "concept": POP}

    def point(params: dict[str, Any]) -> dict[str, Any]:
        r = env.client.get("/v1/timeseries", params={**base, **params})
        assert r.status_code == 200, r.text
        pts = [p for p in r.json()["data"]["points"] if p["period_label"].startswith("2023")]
        assert len(pts) == 1
        return pts[0]

    latest = point({})
    assert latest["revision_no"] == 2 and latest["value"]["value"] == env.new_pop
    before = point({"as_of": iso(env.t_between_revisions)})
    assert before["revision_no"] == 1 and before["value"]["value"] == env.old_pop
    all_rev = point({"revisions": "all"})
    assert [r["revision_no"] for r in all_rev["revisions"]] == [1, 2]
    all_before = point({"revisions": "all", "as_of": iso(env.t_between_revisions)})
    assert [r["revision_no"] for r in all_before["revisions"]] == [1]
    full = env.client.get("/v1/timeseries", params=base).json()
    assert len(full["data"]["points"]) == 25 and full["units"] == ["unit.persons"]
    assert full["origin"] == "test_fixture" and full["modality"] == "reported_observation"
    problem(env.client.get("/v1/timeseries", params={**base, "as_of": "2024-01-01T00:00:00"}), 422)

    obs = env.client.get("/v1/observations", params={"logical_key": "wb:SP.POP.TOTL:DZA:2023",
                                                     "revisions": "all"}).json()["data"]
    assert [o["revision_no"] for o in obs] == [1, 2]
    assert obs[1]["supersedes_id"] == obs[0]["id"] and obs[0]["superseded_by_id"] == obs[1]["id"]
    old = env.client.get("/v1/observations", params={"logical_key": "wb:SP.POP.TOTL:DZA:2023",
                                                     "as_of": iso(env.t_between_revisions)}).json()["data"]
    assert len(old) == 1 and old[0]["revision_no"] == 1 and old[0]["superseded_by_id"] is None


def test_provenance_reaches_raw_artifact_and_selector(env: Env) -> None:
    est = env.client.get("/v1/beliefs", params={"release_id": str(env.r1.release_id), "subject_id": "FRA",
                                                "concept": CRUDE_M, "period": 2023}).json()["data"][0]
    r = env.client.get(f"/v1/provenance/estimate/{est['id']}")
    assert r.status_code == 200, r.text
    g = r.json()["data"]
    kinds = {n["kind"] for n in g["nodes"]}
    assert {"estimate", "release", "observation", "activity", "acquisition", "artifact", "storage_location",
            "connector_version", "source"} <= kinds
    with env.owner.connect() as c:
        raw_sha = c.execute(text(
            "SELECT a.raw_sha256 FROM evidence.observation o JOIN evidence.acquisition a ON a.id = o.acquisition_id "
            "WHERE o.id = :i"), {"i": est["observation_id"]}).scalar_one()
    sel = next(e for e in g["edges"] if e["relation"] == "selected_from")
    assert sel["target"] == f"artifact:{raw_sha}"
    assert sel["attributes"]["selector"]["artifact_sha256"] == raw_sha
    assert "match" in sel["attributes"]["selector"]
    art = next(n for n in g["nodes"] if n["id"] == f"artifact:{raw_sha}")
    assert art["attributes"]["sha256"] == raw_sha
    assert any(e["source"] == art["id"] and e["relation"] == "stored_at" for e in g["edges"])
    assert any(e["relation"] == "normalised_by" for e in g["edges"])
    assert not g["truncated"]
    small = env.client.get(f"/v1/provenance/estimate/{est['id']}", params={"max_nodes": 10}).json()
    assert small["data"]["truncated"] and any(w["code"] == "truncated" for w in small["warnings"])
    assert env.client.get(f"/v1/provenance/artifact/{raw_sha}").status_code == 200
    assert env.client.get(f"/v1/provenance/release/{env.r1.release_id}").status_code == 200
    problem(env.client.get("/v1/provenance/observation/not-a-uuid"), 422)
    problem(env.client.get(f"/v1/provenance/observation/{uuid.uuid4()}"), 404)
    problem(env.client.get(f"/v1/provenance/unknown/{uuid.uuid4()}"), 422)


def _scenario(env: Env, **over: Any) -> dict[str, Any]:
    spec = json.loads(EXAMPLE.read_text())
    spec.update(baseline_release_id=str(env.r1.release_id), knowledge_cutoff=iso(datetime.now(UTC)))
    spec.update(over)
    return spec


def test_scenarios_validate(env: Env) -> None:
    ex = json.loads(EXAMPLE.read_text())
    body = problem(env.client.post("/v1/scenarios", json=ex), 422, "validation_error")
    assert "fixture" in body["detail"]
    problem(env.client.post("/v1/scenarios", json={**_scenario(env), "fidelity": "L3"}), 422, "capability_gap")
    problem(env.client.post("/v1/scenarios", json={**_scenario(env), "extra": 1}), 422, "validation_error")
    problem(env.client.post("/v1/scenarios", json=_scenario(env, baseline_release_id=str(uuid.uuid4()))), 422)
    early = _scenario(env, knowledge_cutoff="2020-01-01T00:00:00Z")
    problem(env.client.post("/v1/scenarios", json=early), 422, "validation_error")
    spec = _scenario(env, title="API test scenario")
    r = env.client.post("/v1/scenarios", json=spec)
    assert r.status_code == 201, r.text
    again = env.client.post("/v1/scenarios", json=spec)
    assert again.status_code == 200 and again.json()["scenario_id"] == r.json()["scenario_id"]
    got = env.client.get(f"/v1/scenarios/{r.json()['scenario_id']}").json()
    assert got["context_mode"] == "simulation" and any(w["code"] == "scenario_context" for w in got["warnings"])
    assert got["baseline_release"]["id"] == str(env.r1.release_id)


def test_simulations_jobs(env: Env) -> None:
    sid = env.client.post("/v1/scenarios", json=_scenario(env, title="sim test")).json()["scenario_id"]
    body = {"scenario_id": sid, "fidelity": "L1"}
    problem(env.client.post("/v1/simulations", json=body), 400, "idempotency_key_required")
    problem(env.client.post("/v1/simulations", json=body, headers={"Idempotency-Key": "short"}), 400)
    problem(env.client.post("/v1/simulations", json={**body, "fidelity": "L3"},
                            headers={"Idempotency-Key": "sim-test-L3-0001"}), 422, "capability_gap")
    problem(env.client.post("/v1/simulations", json={**body, "fidelity": "L9"},
                            headers={"Idempotency-Key": "sim-test-L9-0001"}), 422, "validation_error")
    problem(env.client.post("/v1/simulations", json={**body, "fidelity": "L2"},
                            headers={"Idempotency-Key": "sim-test-L2-0001"}), 422, "validation_error")
    problem(env.client.post("/v1/simulations", json={**body, "scenario_id": str(uuid.uuid4())},
                            headers={"Idempotency-Key": "sim-test-404-0001"}), 404)
    key = {"Idempotency-Key": f"sim-test-{uuid.uuid4()}"}
    r = env.client.post("/v1/simulations", json=body, headers=key)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert r.json()["status_url"] == f"/v1/simulations/{job_id}" and r.headers["location"] == r.json()["status_url"]
    again = env.client.post("/v1/simulations", json=body, headers=key)
    assert again.status_code == 202 and again.json()["job_id"] == job_id
    problem(env.client.post("/v1/simulations", json={**body, "fidelity": "L0"}, headers=key), 409,
            "idempotency_conflict")
    job = env.client.get(f"/v1/jobs/{job_id}").json()
    assert job["job_type"] == "simulation" and job["status"] == "queued" and job["cancellable"]
    assert job["payload"] == {"scenario_id": sid, "fidelity": "L1"}
    st = env.client.get(f"/v1/simulations/{job_id}").json()
    assert st["data"]["job"]["id"] == job_id and st["data"]["results"] == [] and st["data"]["outcome"] is None
    assert st["context_mode"] == "simulation" and st["baseline_release"]["id"] == str(env.r1.release_id)
    assert {w["code"] for w in st["warnings"]} >= {"scenario_context", "not_finished"}
    c = env.client.post(f"/v1/jobs/{job_id}/cancel")
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert env.client.get("/v1/jobs", params={"job_type": "simulation"}).json()["data"][0]["id"] == job_id
    # Algeria lacks eligible crude inputs: the all-four-country run is rejected, not imputed.
    four = env.client.post("/v1/scenarios", json=_scenario(env, title="four", countries=["FRA", "DZA", "CHN", "USA"]))
    assert four.status_code == 201, four.text
    miss = problem(env.client.post("/v1/simulations", json={"scenario_id": four.json()["scenario_id"],
                                                            "fidelity": "L1"},
                                   headers={"Idempotency-Key": f"sim-four-{uuid.uuid4()}"}), 422, "missing_input")
    assert {m["country"] for m in miss["details"]["missing"]} == {"DZA"}


def test_replay_and_discovery(env: Env) -> None:
    body = {"mode": "operational_as_known", "cutoff": iso(env.t_between_releases), "concepts": [POP]}
    problem(env.client.post("/v1/replay", json={**body, "mode": "time_travel"},
                            headers={"Idempotency-Key": "replay-bad-mode-1"}), 422, "validation_error")
    problem(env.client.post("/v1/replay", json=body), 400, "idempotency_key_required")
    r = env.client.post("/v1/replay", json=body, headers={"Idempotency-Key": f"replay-{uuid.uuid4()}"})
    assert r.status_code == 202, r.text
    assert r.json()["job_type"] == "replay" and r.json()["status_url"].startswith("/v1/jobs/")
    d = env.client.post("/v1/discovery", json={"catalogue": "eurostat", "requirement_id": "R-POP-JAN-AGESEX",
                                               "mode": "fixture"},
                        headers={"Idempotency-Key": f"disc-{uuid.uuid4()}"})
    assert d.status_code == 202, d.text
    problem(env.client.post("/v1/discovery", json={"catalogue": "worldbank"},
                            headers={"Idempotency-Key": f"disc-{uuid.uuid4()}"}), 422, "capability_gap")
    env.client.post(f"/v1/jobs/{d.json()['job_id']}/cancel")


def test_coverage_and_gaps(env: Env) -> None:
    cov = env.client.get("/v1/coverage", params={"release_id": str(env.r1.release_id)})
    assert cov.status_code == 200, cov.text
    data = cov.json()["data"]
    assert data["catalogue_version"] == "mvp-1.0.0" and "not a measure of knowledge" in data["statement"]
    crude = next(r for r in data["requirements"] if r["requirement_id"] == "R-CRUDE-M")
    assert crude["denominator"] == 20 and crude["typed_missing"] >= 4
    assert crude["available"] + crude["typed_missing"] + crude["absent"] == 20
    assert crude["availability_ratio"] == f"{crude['available']}/20"
    agesex = next(r for r in data["requirements"] if r["requirement_id"] == "R-POP-JAN-AGESEX")
    assert agesex["absent"] == 1 and agesex["available"] == 0
    gaps = env.client.get("/v1/gaps", params={"release_id": str(env.r1.release_id)}).json()["data"]
    dza = [g for g in gaps if g["entity_key"] == "DZA" and g["concept"] == CRUDE_M]
    assert {2020, 2021, 2022, 2023} <= {g["period"] for g in dza}
    assert all(g["gap_kind"] == "typed_missing" and "zero" in g["action"] for g in dza)
    assert any(g["requirement_id"] == "R-POP-JAN-AGESEX" and g["gap_kind"] == "absent" for g in gaps)


def test_relations_bounded_with_truncation_flag(env: Env) -> None:
    r = env.client.get("/v1/relations", params={"entity_id": "FRA", "max_edges": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["truncated"] is True and len(body["data"]["relations"]) == 5
    assert any(w["code"] == "truncated" for w in body["warnings"])
    rel = body["data"]["relations"][0]
    assert {p["role"] for p in rel["participants"]} == {"role.reporter", "role.partner", "role.product"}
    assert rel["value"]["kind"] in ("decimal", "missing")
    full = env.client.get("/v1/relations", params={"entity_id": "FRA", "period": 2023}).json()
    assert full["data"]["truncated"] is False
    assert all(r["support"]["period_label"] == "2023" for r in full["data"]["relations"])
    assert any(n["label"] == "France" for n in full["data"]["nodes"])
    assert env.client.get("/v1/relations", params={"max_edges": 501}).status_code == 422
    two = env.client.get("/v1/relations", params={"entity_id": "DZA", "depth": 2, "max_edges": 50}).json()
    assert len(two["data"]["relations"]) <= 50
    problem(env.client.get("/v1/relations", params={"depth": 3}), 422)


def test_query_spec(env: Env) -> None:
    with env.owner.connect() as c:
        dza = c.execute(text("SELECT entity_id FROM world.alias WHERE provider='iso3166_alpha3' "
                             "AND external_key='DZA'")).scalar_one()
    q = env.client.post("/v1/query", json={"kind": "beliefs", "release_id": str(env.r1.release_id),
                                           "subject_ids": [str(dza)], "concepts": [CRUDE_M]})
    assert q.status_code == 200, q.text
    by_year = {d["support"]["period_label"]: d["value"]["kind"] for d in q.json()["data"]}
    assert by_year["2023"] == "missing" and len(by_year) == 5
    ts = env.client.post("/v1/query", json={"kind": "timeseries", "subject_id": str(dza), "concept": POP})
    assert ts.status_code == 200 and len(ts.json()["data"]["points"]) == 25
    rq = env.client.post("/v1/query", json={"kind": "relations", "entity_id": str(dza), "max_edges": 3})
    assert rq.status_code == 200 and len(rq.json()["data"]["relations"]) <= 3
    problem(env.client.post("/v1/query", json={"kind": "sql", "sql": "SELECT 1"}), 422, "validation_error")
    problem(env.client.post("/v1/query", json={"kind": "beliefs", "limit": 5000}), 422)


def test_catalog_endpoints(env: Env) -> None:
    src = env.client.get("/v1/sources").json()["data"]
    assert {s["source_key"] for s in src} >= {"worldbank.wdi", "un.comtrade.public_preview"}
    conns = env.client.get("/v1/connectors").json()["data"]
    wb = next(c for c in conns if c["name"] == WB)
    assert wb["latest_health"]["status"] in ("healthy", "degraded") and wb["licence"] and wb["acquisitions"] >= 2
    onto = env.client.get("/v1/ontology").json()["data"]
    assert any(c["concept_id"] == CRUDE_M for c in onto["concepts"]) and onto["mappings"]
    models = env.client.get("/v1/models").json()["data"]
    crude = next(m for m in models if m["model_key"] == "economic.crude_exposure")
    ev = env.client.get(f"/v1/models/{crude['id']}/evaluations")
    assert ev.status_code == 200 and ev.json()["data"] == []
    problem(env.client.get(f"/v1/models/{uuid.uuid4()}/evaluations"), 404)
    assert env.client.get("/v1/events").json()["data"] == []
    evid = env.client.get("/v1/evidence").json()
    assert evid["context_mode"] == "evidence"
    loc = env.client.get("/v1/locations").json()
    feats = loc["data"]["features"]
    assert loc["data"]["type"] == "FeatureCollection" and feats
    assert all(f["properties"]["display_only"] for f in feats)
    assert any(w["code"] == "display_only" for w in loc["warnings"])
    fra = env.client.get("/v1/locations", params={"entity_id": "FRA"}).json()["data"]["features"]
    assert fra and fra[0]["properties"]["match_status"] in ("reviewed_match", "overseas_part", "disputed")
    assert env.client.get("/v1/proposals").json()["data"] == []
    problem(env.client.post(f"/v1/proposals/{uuid.uuid4()}/review", json={"decision": "approve"}), 404)


def test_proposal_review_records_human_reviewer(env: Env) -> None:
    with env.owner.begin() as c:
        sha = c.execute(text("SELECT manifest_sha256 FROM belief.release LIMIT 1")).scalar_one()
        pid = c.execute(text(
            "INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, summary, author) "
            "VALUES ('mapping', :s, 1, '{}'::jsonb, 'agent:discovery') RETURNING id"), {"s": sha}).scalar_one()
    r = env.client.post(f"/v1/proposals/{pid}/review", json={"decision": "reject", "comment": "needs evidence"})
    assert r.status_code == 201, r.text
    assert r.json()["reviewer"] == "human:admin"
    lst = env.client.get("/v1/proposals").json()["data"]
    assert lst[0]["reviews"][0]["decision"] == "reject"
    problem(env.client.post(f"/v1/proposals/{pid}/review", json={"decision": "maybe"}), 422)


def test_root_without_ui_and_unknown_paths(env: Env) -> None:
    r = env.client.get("/")
    assert r.status_code == 200
    if r.headers["content-type"].startswith("application/json"):
        assert r.json()["api"] == "/v1"
    problem(env.client.get("/v1/does-not-exist"), 404)
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_metrics_after_traffic(env: Env) -> None:
    r = env.client.get("/metrics")
    assert r.status_code == 200
    assert "reva_http_requests_total" in r.text and "reva_jobs" in r.text


def test_as_of_must_be_timezone_aware(env: Env) -> None:
    problem(env.client.get("/v1/beliefs", params={"as_of": (datetime.now() - timedelta(days=1)).isoformat()}), 422)


# ---------------------------------------------------------------------------
# Follow-ups from the UI integration (agent E2)
# ---------------------------------------------------------------------------


def _run_claimed(env: Env, job_id: str, handler: Any) -> dict[str, Any]:
    """Claim a queued job by hand (lease + fencing token) and run its handler in-process."""
    from reva.operations.context import JobContext

    with env.owner.begin() as c:
        row = c.execute(text(
            """UPDATE ops.job SET status = 'running', worker_id = 'test-worker-E2',
                 lease_until = clock_timestamp() + interval '15 minutes',
                 fencing_token = nextval('ops.fencing_seq'), attempts = attempts + 1
               WHERE id = :j RETURNING job_type, payload, fencing_token"""), {"j": job_id}).mappings().one()
    sim = make_engine("sim", env.settings)
    try:
        ctx = JobContext(job_id=uuid.UUID(job_id), job_type=row["job_type"], worker_id="test-worker-E2",
                         fencing_token=int(row["fencing_token"]), attempt=1, settings=env.settings,
                         worker_engine=env.worker, sim_engine=sim, store=env.store)
        result = handler(dict(row["payload"]), ctx)
        if not ctx.completed_in_handler:
            with env.worker.begin() as c:
                ctx.assert_lease(c)
                ctx.complete_in(c, "succeeded", result)
    finally:
        sim.dispose()
    return result  # type: ignore[no-any-return]


def test_provenance_of_run_and_scenario(env: Env) -> None:
    from reva.simulation.jobs import handle_simulation

    sid = env.client.post("/v1/scenarios", json=_scenario(env, title="provenance run")).json()["scenario_id"]
    acc = env.client.post("/v1/simulations", json={"scenario_id": sid, "fidelity": "L1"},
                          headers={"Idempotency-Key": f"prov-run-{uuid.uuid4()}"})
    assert acc.status_code == 202, acc.text
    _run_claimed(env, acc.json()["job_id"], handle_simulation)
    st = env.client.get(f"/v1/simulations/{acc.json()['job_id']}").json()
    links = {lk["kind"]: lk for lk in st["provenance_links"]}
    assert {"scenario", "run"} <= set(links)
    run_id = st["data"]["run"]["id"]

    # Every emitted link resolves (no dangling run/scenario links).
    for lk in st["provenance_links"][:5]:
        assert env.client.get(lk["href"]).status_code == 200, lk

    r = env.client.get(f"/v1/provenance/run/{run_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    g = body["data"]
    assert g["root"] == f"run:{run_id}" and not g["truncated"]
    assert body["context_mode"] == "simulation" and body["origin"] == "test_fixture"
    nodes = {n["id"]: n for n in g["nodes"]}
    kinds = {n["kind"] for n in g["nodes"]}
    assert {"run", "scenario", "release", "artifact", "context", "model_version"} <= kinds
    roles = {e["attributes"].get("role") for e in g["edges"] if e["source"] == g["root"]}
    assert {"model_manifest", "ontology_manifest", "result_manifest"} <= roles
    assert any(e["relation"] == "of_scenario" and e["target"] == f"scenario:{sid}" for e in g["edges"])
    assert any(e["relation"] == "baseline_release" and e["source"] == f"scenario:{sid}"
               and e["target"] == f"release:{env.r1.release_id}" for e in g["edges"])
    assert all(e["source"] in nodes and e["target"] in nodes for e in g["edges"])

    sc = env.client.get(f"/v1/provenance/scenario/{sid}").json()
    sg = sc["data"]
    spec_edge = next(e for e in sg["edges"] if e["relation"] == "defined_by")
    spec_node = next(n for n in sg["nodes"] if n["id"] == spec_edge["target"])
    assert spec_node["attributes"]["stored_inline"] is True
    assert any(n["kind"] == "release" for n in sg["nodes"]) and sc["context_mode"] == "simulation"

    small = env.client.get(f"/v1/provenance/run/{run_id}", params={"max_nodes": 10}).json()
    assert small["data"]["truncated"] and len(small["data"]["nodes"]) <= 10
    problem(env.client.get(f"/v1/provenance/run/{uuid.uuid4()}"), 404)
    problem(env.client.get(f"/v1/provenance/scenario/{uuid.uuid4()}"), 404)
    problem(env.client.get("/v1/provenance/run/not-a-uuid"), 422)


def test_connector_and_source_origin_is_derived(env: Env) -> None:
    from reva.api.routes.catalog import _acq_origin

    r = env.client.get("/v1/connectors").json()
    wb = next(c for c in r["data"] if c["name"] == WB)
    assert wb["origin"] == "test_fixture" and wb["latest_acquisition_origin"] == "test_fixture"
    assert wb["acquisitions_by_origin"] == {"live": 0, "test_fixture": wb["acquisitions"]}
    assert r["origin"] == "test_fixture" and any(w["code"] == "fixture_data" for w in r["warnings"])
    for c in r["data"]:
        if c["acquisitions"] == 0:
            assert c["origin"] == "none" and c["latest_acquisition_origin"] is None
    src = env.client.get("/v1/sources").json()
    wdi = next(s for s in src["data"] if s["source_key"] == "worldbank.wdi")
    assert wdi["origin"] == "test_fixture" and wdi["acquisitions_by_origin"]["test_fixture"] >= 2
    assert src["origin"] == "test_fixture"
    assert [_acq_origin(a, b) for a, b in ((0, 0), (3, 0), (0, 2), (1, 1))] == \
        ["none", "live", "test_fixture", "mixed"]


def test_evaluation_job_and_scorecard_labels(env: Env) -> None:
    from reva.operations.jobs import HANDLERS, resolve_handler

    assert HANDLERS["evaluation"] == "reva.evaluation.tournament_service:handle_evaluation"
    handler = resolve_handler("evaluation")
    body = {"concepts": [POP], "horizons": [1], "seed": 7}
    problem(env.client.post("/v1/evaluations", json=body), 400, "idempotency_key_required")
    problem(env.client.post("/v1/evaluations", json={"concepts": [CRUDE_M]},
                            headers={"Idempotency-Key": f"eval-bad-{uuid.uuid4()}"}), 422, "capability_gap")
    problem(env.client.post("/v1/evaluations", json={"horizons": [9]},
                            headers={"Idempotency-Key": f"eval-bad-{uuid.uuid4()}"}), 422, "validation_error")
    problem(env.client.post("/v1/evaluations", json={"seed": -1},
                            headers={"Idempotency-Key": f"eval-bad-{uuid.uuid4()}"}), 422, "validation_error")
    problem(env.client.post("/v1/evaluations", json={"extra": 1},
                            headers={"Idempotency-Key": f"eval-bad-{uuid.uuid4()}"}), 422, "validation_error")
    key = {"Idempotency-Key": f"eval-{uuid.uuid4()}"}
    acc = env.client.post("/v1/evaluations", json=body, headers=key)
    assert acc.status_code == 202, acc.text
    jid = acc.json()["job_id"]
    assert acc.json()["job_type"] == "evaluation" and acc.headers["location"] == f"/v1/jobs/{jid}"
    assert env.client.post("/v1/evaluations", json=body, headers=key).json()["job_id"] == jid
    problem(env.client.post("/v1/evaluations", json={**body, "seed": 8}, headers=key), 409, "idempotency_conflict")
    job = env.client.get(f"/v1/jobs/{jid}").json()
    assert job["payload"] == {"concepts": [POP], "horizons": [1], "seed": 7} and job["status"] == "queued"
    # A default request (no fields) is accepted with an empty payload: service defaults apply.
    empty = env.client.post("/v1/evaluations", json={}, headers={"Idempotency-Key": f"eval-{uuid.uuid4()}"})
    assert empty.status_code == 202 and env.client.get(f"/v1/jobs/{empty.json()['job_id']}").json()["payload"] == {}
    env.client.post(f"/v1/jobs/{empty.json()['job_id']}/cancel")

    result = _run_claimed(env, jid, handler)
    assert result["replay_mode"] == "revised_data_retrospective"
    assert env.client.get(f"/v1/jobs/{jid}").json()["status"] == "succeeded"

    ev = env.client.get("/v1/models/forecast.log_drift/evaluations")
    assert ev.status_code == 200, ev.text
    out = ev.json()
    rows = out["data"]
    mine = [r for r in rows if r["included_as"] == "requested_model"]
    naive = [r for r in rows if r["included_as"] == "naive_baseline_comparator"]
    assert mine and all(r["model_key"] == "forecast.log_drift" and not r["is_naive_baseline"] for r in mine)
    assert naive and all(r["model_key"] == "forecast.last_value" and r["is_naive_baseline"] for r in naive)
    dev_manifests = {r["evaluation_manifest_sha256"] for r in mine if r["task_scope"]["period"] == "development"}
    assert dev_manifests <= {r["evaluation_manifest_sha256"] for r in naive
                             if r["task_scope"]["period"] == "development"}
    label = "revised-data retrospective — not a real-time backtest"
    assert all(r["replay_mode"] == "revised_data_retrospective" and r["replay_mode_label"] == label for r in rows)
    assert any(w["code"] == "revised_data_retrospective" and label in w["message"] for w in out["warnings"])
    assert out["origin"] == "test_fixture" and out["context_mode"] == "evaluation"
    assert out["historical_validation"]["status"] == "reported"
    assert out["historical_validation"]["detail"]["labels"] == [label]
    # The naive model's own listing has no comparator rows (its rows are the baseline).
    own = env.client.get("/v1/models/forecast.last_value/evaluations").json()["data"]
    assert own and all(r["included_as"] == "requested_model" and r["is_naive_baseline"] for r in own)
    # Scope: a read-only token cannot start evaluations.
    from reva.api.auth import new_api_token

    tok, sha = new_api_token()
    with env.owner.begin() as c:
        c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'e2-read')"),
                  {"h": sha, "s": ["read"]})
    r = env.client.post("/v1/evaluations", json={}, headers={"Authorization": f"Bearer {tok}",
                                                             "Idempotency-Key": f"eval-{uuid.uuid4()}"})
    assert r.status_code == 403


def test_discovery_candidates_from_jobs_and_proposals(env: Env) -> None:
    empty = env.client.get("/v1/discovery/candidates")
    assert empty.status_code == 200, empty.text
    assert any(w["code"] == "unreviewed_candidates" for w in empty.json()["warnings"])
    with env.owner.begin() as c:
        sha = c.execute(text("SELECT manifest_sha256 FROM belief.release LIMIT 1")).scalar_one()
        pid = c.execute(text(
            "INSERT INTO ops.proposal(proposal_type, manifest_sha256, permission_tier, summary, author) "
            "VALUES ('connector_promotion', :s, 1, CAST(:sm AS jsonb), 'agent:discovery') RETURNING id"),
            {"s": sha, "sm": json.dumps({"scope": {"dataset": "demo_pjan", "concept": "x@1"}})}).scalar_one()
        result = {
            "status": "gate_hold", "requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture",
            "search": {"ranked": [{"code": "demo_pjan", "score": 3.5, "jaccard": 0.6},
                                  {"code": "demo_pjanaggr3", "score": 2.0, "jaccard": 0.4},
                                  {"code": "not-inspected", "score": 1.0, "jaccard": 0.1}],
                       "inspections": [{"code": "demo_pjan", "ok": True, "error": None},
                                       {"code": "demo_pjanaggr3", "ok": False, "error": "timeout"}]},
            "connector": {"name": "eurostat.demo_pjan", "version": "1.0.0"},
            "gate": {"proposal_id": str(pid), "outcome": "hold"},
        }
        jid = c.execute(text(
            "INSERT INTO ops.job(job_type, payload, idempotency_key, status, result_ref, finished_at) "
            "VALUES ('discovery', CAST(:p AS jsonb), :k, 'succeeded', CAST(:r AS jsonb), clock_timestamp()) "
            "RETURNING id"),
            {"p": json.dumps({"mode": "fixture"}), "k": f"disc-cand-{uuid.uuid4()}", "r": json.dumps(result)}
        ).scalar_one()
    r = env.client.get("/v1/discovery/candidates")
    assert r.status_code == 200, r.text
    body = r.json()
    found = [c for c in body["data"] if c["job_id"] == str(jid)]
    assert [c["dataset_code"] for c in found] == ["demo_pjan", "demo_pjanaggr3", "not-inspected"]
    assert [c["rank"] for c in found] == [1, 2, 3]
    top = found[0]
    assert top["selected"] and top["proposal_id"] == str(pid) and top["gate_outcome"] == "hold"
    assert top["inspected"] is True and found[1]["inspected"] is False and found[1]["inspection_error"] == "timeout"
    assert found[2]["inspected"] is None and not found[1]["selected"] and not found[2]["selected"]
    assert all(c["origin"] == "test_fixture" and c["requirement_id"] == "R-POP-JAN-AGESEX" for c in found)
    prop = next(c for c in body["data"] if c["candidate_kind"] == "connector_proposal" and c["proposal_id"] == str(pid))
    assert prop["dataset_code"] == "demo_pjan" and prop["origin"] == "test_fixture" and prop["reviews"] == []
    assert body["origin"] == "test_fixture" and any(w["code"] == "fixture_data" for w in body["warnings"])
    lim = env.client.get("/v1/discovery/candidates", params={"limit": 1}).json()
    assert len(lim["data"]) == 1 and any(w["code"] == "truncated" for w in lim["warnings"])
    assert env.client.get("/v1/discovery/candidates", params={"limit": 0}).status_code == 422


def test_relations_exclude_retracted_observations(env: Env) -> None:
    """Runs last in this module: it retracts one observation behind a FRA 2023 trade relation."""
    params = {"entity_id": "FRA", "period": 2023}
    before = env.client.get("/v1/relations", params=params).json()
    rels = [r for r in before["data"]["relations"] if r["evidence_observation_id"]]
    assert rels and before["data"]["truncated"] is False
    target = rels[0]
    t_before = datetime.now(UTC)
    with env.owner.begin() as c:
        act = c.execute(text("SELECT provenance_activity_id FROM evidence.observation WHERE id = :o"),
                        {"o": target["evidence_observation_id"]}).scalar_one()
        c.execute(text("INSERT INTO evidence.observation_retraction(observation_id, reason, activity_id) "
                       "VALUES (:o, 'test: provider withdrew the record', :a)"),
                  {"o": target["evidence_observation_id"], "a": act})
    after = env.client.get("/v1/relations", params=params).json()
    ids_after = {r["id"] for r in after["data"]["relations"]}
    assert target["id"] not in ids_after and len(ids_after) == len(before["data"]["relations"]) - 1
    # As known before the retraction, the relation is still visible (bitemporal as_of).
    then = env.client.get("/v1/relations", params={**params, "as_of": iso(t_before)}).json()
    assert target["id"] in {r["id"] for r in then["data"]["relations"]}
    # Same through the typed QuerySpec and at depth 2.
    with env.owner.connect() as c:
        fra = c.execute(text("SELECT entity_id FROM world.alias WHERE provider='iso3166_alpha3' "
                             "AND external_key='FRA'")).scalar_one()
    q = env.client.post("/v1/query", json={"kind": "relations", "entity_id": str(fra), "period": 2023})
    assert q.status_code == 200 and target["id"] not in {r["id"] for r in q.json()["data"]["relations"]}
    d2 = env.client.get("/v1/relations", params={**params, "depth": 2, "max_edges": 500}).json()
    assert target["id"] not in {r["id"] for r in d2["data"]["relations"]}
    # Roles stay ordered and role-bound; the release context rejects evidence-only filters.
    assert [p["ordinal"] for p in after["data"]["relations"][0]["participants"]] == [0, 1, 2] or \
        after["data"]["relations"][0]["participants"] == sorted(after["data"]["relations"][0]["participants"],
                                                                key=lambda p: p["ordinal"])
    problem(env.client.get("/v1/relations", params={"release_id": str(env.r1.release_id),
                                                    "as_of": iso(t_before)}), 422, "validation_error")
    pinned = env.client.get("/v1/relations", params={"release_id": str(env.r1.release_id), "max_edges": 5})
    assert pinned.status_code == 200 and pinned.json()["context_mode"] == "belief"
    assert pinned.json()["baseline_release"]["id"] == str(env.r1.release_id)


# ---------------------------------------------------------------------------
# Review findings docs/dev/review_api_sim_ops.md (R2-xx)
# ---------------------------------------------------------------------------


def _l2_spec(env: Env, title: str) -> dict[str, Any]:
    return _scenario(env, title=title, countries=["FRA"], fidelity="L2", assumptions={
        "quantity_response": "elasticity_sensitivity", "no_general_equilibrium": True,
        "elasticity_grid": [{"import_elasticity": -0.5, "export_elasticity": -0.5, "basis": "scenario_assumption"},
                            {"import_elasticity": -1.0, "export_elasticity": -1.0, "basis": "scenario_assumption"}]})


def test_r2_20_l2_sensitivity_range_and_r2_03_replayed_run(env: Env) -> None:
    from reva.simulation.jobs import handle_simulation

    created = env.client.post("/v1/scenarios", json=_l2_spec(env, "L2 range"))
    assert created.status_code == 201, created.text
    sid = created.json()["scenario_id"]
    first = env.client.post("/v1/simulations", json={"scenario_id": sid, "fidelity": "L2"},
                            headers={"Idempotency-Key": f"l2-a-{uuid.uuid4()}"}).json()["job_id"]
    _run_claimed(env, first, handle_simulation)
    st = env.client.get(f"/v1/simulations/{first}")
    assert st.status_code == 200, st.text  # R2-20: was a 500 (TypedValue rejected sensitivity_range)
    body = st.json()
    assert body["data"]["outcome"]["status"] == "succeeded", body["data"]["outcome"]
    ranges = [r for r in body["data"]["results"] if r["value"]["kind"] == "sensitivity_range"]
    assert ranges and all(r["variant"] == "L2:sensitivity_range" for r in ranges)
    rv = ranges[0]["value"]
    assert {"lower", "upper", "unit_ref", "statement"} <= set(rv) and "value" not in rv
    assert "not a confidence interval" in rv["statement"]
    run_id = body["data"]["run"]["id"]

    # R2-03: a second job for the same scenario/fidelity reuses the run; its status shows that run.
    second = env.client.post("/v1/simulations", json={"scenario_id": sid, "fidelity": "L2"},
                             headers={"Idempotency-Key": f"l2-b-{uuid.uuid4()}"}).json()["job_id"]
    res = _run_claimed(env, second, handle_simulation)
    assert res.get("run_id") == run_id
    again = env.client.get(f"/v1/simulations/{second}").json()
    assert again["data"]["job"]["id"] == second and again["data"]["run"]["id"] == run_id
    assert len(again["data"]["results"]) == len(body["data"]["results"])
    codes = {w["code"] for w in again["warnings"]}
    assert "idempotent_replay" in codes and "not_finished" not in codes

    # R2-19: simulation lineage carries the simulation envelope and banner.
    rid = ranges[0]["id"]
    prov = env.client.get(f"/v1/provenance/sim_result/{rid}").json()
    assert prov["modality"] == "assumption_conditioned_scenario" and prov["context_mode"] == "simulation"
    assert any(w["code"] == "scenario_context" for w in prov["warnings"])
    assert prov["context_id"] == body["data"]["scenario"]["context_id"]
    for kind, ident in (("run", run_id), ("scenario", sid)):
        p = env.client.get(f"/v1/provenance/{kind}/{ident}").json()
        assert p["modality"] == "assumption_conditioned_scenario" and p["context_id"] == prov["context_id"]
    est = env.client.get("/v1/provenance/release/" + str(env.r1.release_id)).json()
    assert est["modality"] == "metadata" and not any(w["code"] == "scenario_context" for w in est["warnings"])


def _token_for(env: Env, scopes: list[str], label: str) -> dict[str, str]:
    from reva.api.auth import new_api_token

    tok, sha = new_api_token()
    with env.owner.begin() as c:
        c.execute(text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, :l)"),
                  {"h": sha, "s": scopes, "l": label})
    return {"Authorization": f"Bearer {tok}"}


def _insert_job(env: Env, job_type: str, **cols: Any) -> str:
    extra = {"last_error": None, **cols}
    with env.owner.begin() as c:
        return str(c.execute(text(
            "INSERT INTO ops.job(job_type, payload, idempotency_key, last_error) "
            "VALUES (:t, '{}'::jsonb, :k, CAST(:e AS jsonb)) RETURNING id"),
            {"t": job_type, "k": f"test-{job_type}-{uuid.uuid4()}",
             "e": json.dumps(extra["last_error"]) if extra["last_error"] is not None else None}).scalar_one())


def test_r2_07_cancel_is_scoped_by_job_type(env: Env) -> None:
    sim_tok = _token_for(env, ["read", "simulate"], "svc-sim")
    ing_tok = _token_for(env, ["read", "ingest"], "svc-ingest")
    ingest_job = _insert_job(env, "ingest")
    publish_job = _insert_job(env, "publish_beliefs")
    sim_job = _insert_job(env, "simulation")
    r = env.client.post(f"/v1/jobs/{ingest_job}/cancel", headers=sim_tok)
    assert r.status_code == 403 and r.json()["details"]["required_scope"] == "ingest"
    assert env.client.post(f"/v1/jobs/{publish_job}/cancel", headers=sim_tok).status_code == 403
    r = env.client.post(f"/v1/jobs/{sim_job}/cancel", headers=ing_tok)
    assert r.status_code == 403 and r.json()["details"]["required_scope"] == "simulate"
    assert env.client.post(f"/v1/jobs/{sim_job}/cancel", headers=sim_tok).json()["status"] == "cancelled"
    assert env.client.post(f"/v1/jobs/{ingest_job}/cancel", headers=ing_tok).json()["status"] == "cancelled"
    assert env.client.get(f"/v1/jobs/{publish_job}").json()["status"] == "queued"
    problem(env.client.post(f"/v1/jobs/{uuid.uuid4()}/cancel", headers=sim_tok), 404)


def test_r2_11_malformed_cursor_is_400(env: Env) -> None:
    from reva.api.deps import encode_cursor

    for path, cur in (("/v1/jobs", ["not-a-date", "x"]), ("/v1/jobs", ["2026-01-01T00:00:00+00:00", "x"]),
                      ("/v1/jobs", ["2026-01-01T00:00:00", str(uuid.uuid4())]),
                      ("/v1/beliefs", ["a", "b", "c", "not-a-uuid"]), ("/v1/belief-releases", ["seven"]),
                      ("/v1/observations", ["k", "1", str(uuid.uuid4())]), ("/v1/scenarios", [1, 2]),
                      ("/v1/entities", ["x", 5]), ("/v1/proposals", ["2026", str(uuid.uuid4())])):
        problem(env.client.get(path, params={"cursor": encode_cursor(cur)}), 400, "invalid_cursor")
    problem(env.client.get("/v1/jobs", params={"cursor": "%%%"}), 400, "invalid_cursor")
    first = env.client.get("/v1/jobs", params={"limit": 1}).json()
    assert env.client.get("/v1/jobs", params={"limit": 1, "cursor": first["next_cursor"]}).status_code == 200


def test_r2_13_job_errors_never_expose_sql(env: Env) -> None:
    leaked = ("(psycopg.errors.UndefinedColumn) column \"secretcol\" does not exist\nLINE 1: SELECT secretcol\n"
              "[SQL: SELECT secretcol FROM ops.job WHERE id = %(id)s]\n[parameters: {'id': 'payload-value'}]\n"
              "(Background on this error at: https://sqlalche.me/e/21/f405)")
    jid = _insert_job(env, "health", last_error={"code": "internal_error", "message": leaked, "retryable": False,
                                                 "attempt": 1, "exception": "ProgrammingError"})
    read_tok = _token_for(env, ["read"], "reader")
    for r in (env.client.get(f"/v1/jobs/{jid}", headers=read_tok).json(),
              next(j for j in env.client.get("/v1/jobs", params={"job_type": "health"}).json()["data"]
                   if j["id"] == jid)):
        err = r["last_error"]
        assert err["code"] == "internal_error" and err["exception"] == "ProgrammingError"
        text_ = json.dumps(err)
        assert "[SQL" not in text_ and "parameters" not in text_ and "payload-value" not in text_
        assert "SELECT" not in text_ and "secretcol" in err["message"]


def test_r2_15_concurrent_identical_scenario_returns_existing(env: Env) -> None:
    import threading

    from reva.simulation.spec import validate_scenario_spec

    spec = _scenario(env, title=f"race {uuid.uuid4()}")
    parsed = validate_scenario_spec(spec)
    out: dict[str, Any] = {}
    with env.owner.connect() as c:
        tx = c.begin()
        winner = c.execute(text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :b, :d, 'human:other')"),
                           {"spec": json.dumps(parsed.as_json()), "b": parsed.canonical_bytes(),
                            "d": parsed.digest()}).scalar_one()

        def post() -> None:
            out["r"] = env.client.post("/v1/scenarios", json=spec)

        th = threading.Thread(target=post)
        th.start()
        th.join(1.5)  # the API's insert now waits on the uncommitted competing row
        tx.commit()
    th.join(30)
    r = out["r"]
    assert r.status_code == 200, r.text
    assert r.json()["scenario_id"] == str(winner)


def test_r2_18_query_requires_aware_as_of(env: Env) -> None:
    naive = (datetime.now() - timedelta(days=1)).isoformat()
    problem(env.client.post("/v1/query", json={"kind": "beliefs", "as_of": naive}), 422)
    problem(env.client.post("/v1/query", json={"kind": "relations", "entity_id": str(uuid.uuid4()),
                                               "as_of": naive}), 422)
    problem(env.client.post("/v1/query", json={"kind": "timeseries", "subject_id": str(uuid.uuid4()),
                                               "concept": POP, "as_of": naive}), 422)


def test_r2_04_timeseries_skips_and_flags_retracted(env: Env) -> None:
    base = {"subject_id": "DZA", "concept": POP}
    before = env.client.get("/v1/timeseries", params=base).json()
    p23 = next(p for p in before["data"]["points"] if p["period_label"].startswith("2023"))
    assert p23["revision_no"] == 2 and p23["retracted_revisions_skipped"] == 0
    p22 = next(p for p in before["data"]["points"] if p["period_label"].startswith("2022"))
    t0 = datetime.now(UTC)
    with env.owner.begin() as c:
        for oid in (p23["observation_id"], p22["observation_id"]):
            act = c.execute(text("SELECT provenance_activity_id FROM evidence.observation WHERE id = :o"),
                            {"o": oid}).scalar_one()
            c.execute(text("INSERT INTO evidence.observation_retraction(observation_id, reason, activity_id) "
                           "VALUES (:o, 'test retraction', :a)"), {"o": oid, "a": act})
    after = env.client.get("/v1/timeseries", params=base).json()
    pts = after["data"]["points"]
    q23 = next(p for p in pts if p["period_label"].startswith("2023"))
    # The retracted revision 2 is skipped: revision 1 (still valid) is the point, flagged.
    assert q23["revision_no"] == 1 and q23["value"]["value"] == env.old_pop
    assert q23["retracted_revisions_skipped"] == 1
    # 2022 had a single, now retracted, revision: it is omitted rather than shown as current.
    assert not any(p["period_label"].startswith("2022") for p in pts) and len(pts) == len(before["data"]["points"]) - 1
    w = next(w for w in after["warnings"] if w["code"] == "retracted_records")
    assert "1 retracted revision" in w["message"] and "wb:SP.POP.TOTL:DZA:2022" in w["message"]
    full = env.client.get("/v1/timeseries", params={**base, "revisions": "all"}).json()
    r23 = next(p for p in full["data"]["points"] if p["period_label"].startswith("2023"))
    assert [(r["revision_no"], r["retracted"]) for r in r23["revisions"]] == [(1, False), (2, True)]
    # As known before the retractions nothing is skipped.
    then = env.client.get("/v1/timeseries", params={**base, "as_of": iso(t0)}).json()
    assert next(p for p in then["data"]["points"] if p["period_label"].startswith("2023"))["revision_no"] == 2
    assert not any(w["code"] == "retracted_records" for w in then["warnings"])


def test_r4_06_discovery_request_is_validated_and_passed_through(env: Env) -> None:
    def post(body: dict[str, Any]) -> Any:
        return env.client.post("/v1/discovery", json=body, headers={"Idempotency-Key": f"disc-{uuid.uuid4()}"})

    for body in ({"catalogue": "worldbank"}, {"catalogue": "comtrade"}):
        assert problem(post(body), 422, "capability_gap")["details"]["supported"] == ["eurostat"]
    for body in ({"query": "nominal GDP"}, {"concept_id": "economics.gdp"}, {"max_candidates": 3}):
        problem(post(body), 422, "capability_gap")
    assert problem(post({"requirement_id": "R-CRUDE-M"}), 422, "capability_gap")["details"]["supported"] == \
        ["R-POP-JAN-AGESEX"]
    problem(post({"mode": "replay"}), 422, "validation_error")
    problem(post({"unknown": 1}), 422, "validation_error")
    ok = post({"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"})
    assert ok.status_code == 202, ok.text
    job = env.client.get(f"/v1/jobs/{ok.json()['job_id']}").json()
    assert job["payload"] == {"requirement_id": "R-POP-JAN-AGESEX", "mode": "fixture"}
    default = post({})
    assert env.client.get(f"/v1/jobs/{default.json()['job_id']}").json()["payload"] == \
        {"requirement_id": "R-POP-JAN-AGESEX", "mode": "live"}
    for jid in (ok.json()["job_id"], default.json()["job_id"]):
        env.client.post(f"/v1/jobs/{jid}/cancel")
    # Only reva_env matters here; the other production-only requirements (S3, TLS to the DB) are skipped.
    prod = create_app(api_settings(env.settings).model_copy(update={"reva_env": "production"}))
    with TestClient(prod, base_url="https://testserver") as c:
        login(c)
        r = c.post("/v1/discovery", json={"mode": "fixture"}, headers={"Idempotency-Key": f"disc-{uuid.uuid4()}"})
        assert r.status_code == 422 and "production" in r.json()["detail"]
    prod.state.engine.dispose()
    # R4-17: no endpoint text claims that nothing is promoted without review.
    w = env.client.get("/v1/discovery/candidates").json()["warnings"]
    note = next(x for x in w if x["code"] == "unreviewed_candidates")["message"]
    assert "tier-1" in note and "automatically" in note


def test_every_envelope_states_its_knowledge_cutoff(env: Env) -> None:
    rel_cut = env.client.get(f"/v1/belief-releases/{env.r2.release_id}").json()["data"]["knowledge_cutoff"][:10]
    before = datetime.now(UTC) - timedelta(minutes=5)
    for path, params in (("/v1/observations", {"limit": 5}), ("/v1/evidence", {}), ("/v1/connectors", {}),
                         ("/v1/sources", {}), ("/v1/models", {}), ("/v1/ontology", {}), ("/v1/entities", {}),
                         ("/v1/relations", {"entity_id": "FRA", "max_edges": 5}), ("/v1/jobs", {}),
                         ("/v1/proposals", {}), ("/v1/discovery/candidates", {}), ("/v1/events", {}),
                         ("/v1/locations", {"entity_id": "FRA"}), ("/v1/belief-releases", {})):
        body = env.client.get(path, params=params).json()
        assert body["knowledge_cutoff"], path
        assert body["knowledge_cutoff_basis"] == "read_time", (path, body["knowledge_cutoff_basis"])
        assert datetime.fromisoformat(body["knowledge_cutoff"]) >= before
        assert body["knowledge_cutoff"][:10] >= rel_cut
    asof = iso(env.t_between_releases)
    o = env.client.get("/v1/observations", params={"as_of": asof, "limit": 1}).json()
    assert o["knowledge_cutoff_basis"] == "as_of" and o["knowledge_cutoff"].startswith(asof[:19])
    b = env.client.get("/v1/beliefs", params={"subject_id": "FRA", "concept": POP}).json()
    assert b["knowledge_cutoff_basis"] == "release_cutoff"
    assert b["valid_time"]["start"].startswith("2000") and b["valid_time"]["end"] >= "2024"
    obs = env.client.get("/v1/observations", params={"subject_id": "FRA", "concept": POP}).json()
    assert obs["valid_time"]["start"].startswith("2000")
    rels = env.client.get("/v1/relations", params={"entity_id": "FRA", "period": 2023}).json()
    assert rels["valid_time"]["start"].startswith("2023")
    m = env.client.get("/v1/models").json()
    assert m["origin"] == "test_fixture" and any(w["code"] == "fixture_data" for w in m["warnings"])
    ev = env.client.get("/v1/models/forecast.log_drift/evaluations").json()
    assert ev["knowledge_cutoff"]
    assert ev["knowledge_cutoff_basis"] == ("evaluation_context" if ev["data"] else "read_time")
    sc = env.client.get("/v1/scenarios").json()
    if sc["data"]:
        s1 = env.client.get(f"/v1/scenarios/{sc['data'][0]['id']}").json()
        assert s1["knowledge_cutoff"] and s1["knowledge_cutoff_basis"] == "scenario_cutoff"


# ---------------------------------------------------------------------------
# R7 (ui_check.md backend requests): provider flags inline, scenario valid time, derived entity origin
# ---------------------------------------------------------------------------

CRUDE_M_WEIGHT = "trade.hs2709.import_net_weight.world_total"


def _stored_flags(env: Env, observation_id: str) -> dict[str, Any]:
    with env.owner.connect() as c:
        return c.execute(text("SELECT source_flags FROM evidence.observation WHERE id = :i"),
                         {"i": observation_id}).scalar_one()  # type: ignore[no-any-return]


def test_r7_beliefs_carry_selected_observation_source_flags(env: Env) -> None:
    r = env.client.get("/v1/beliefs", params={"release_id": str(env.r1.release_id), "subject_id": "CHN",
                                              "concept": CRUDE_M, "period": 2023})
    assert r.status_code == 200, r.text
    est = r.json()["data"][0]
    flags = est["source_flags"]
    assert est["observation_id"] and est["source_flags_truncated"] is False
    # Exactly the flags stored on the selected observation (no extra /v1/observations call needed) ...
    assert flags == _stored_flags(env, est["observation_id"])
    # ... including the Comtrade 1.2.0 interpretation (value estimation undeterminable = null) and raw flags.
    assert "provider_value_estimated" in flags and flags["provider_value_estimated"] is None
    assert {"isReported", "legacyEstimationFlag", "isQtyEstimated", "isNetWgtEstimated"} <= set(flags)
    # Same as the observation endpoint shows for that record.
    obs = env.client.get("/v1/observations", params={"subject_id": "CHN", "concept": CRUDE_M,
                                                     "period_start": 2023, "period_end": 2023}).json()["data"]
    assert next(o for o in obs if o["id"] == est["observation_id"])["source_flags"] == flags
    pop = env.client.get("/v1/beliefs", params={"subject_id": "FRA", "concept": POP, "period": 2023}).json()
    assert {"indicator", "obs_status"} <= set(pop["data"][0]["source_flags"])
    # The QuerySpec path shares the serializer.
    q = env.client.post("/v1/query", json={"kind": "beliefs", "release_id": str(env.r1.release_id),
                                            "subject_ids": [est["subject"]["id"]], "concepts": [CRUDE_M]})
    assert q.status_code == 200, q.text
    assert all("source_flags" in d for d in q.json()["data"])


def test_r7_timeseries_points_carry_source_flags(env: Env) -> None:
    r = env.client.get("/v1/timeseries", params={"subject_id": "CHN", "concept": CRUDE_M_WEIGHT})
    assert r.status_code == 200, r.text
    pts = r.json()["data"]["points"]
    assert pts
    for p in pts:
        assert p["source_flags"] == _stored_flags(env, p["observation_id"])
        assert "provider_quantity_estimated" in p["source_flags"] and p["source_flags_truncated"] is False
    # A revised lineage reports the flags of the point's own (latest non-retracted) revision.
    dza = env.client.get("/v1/timeseries", params={"subject_id": "DZA", "concept": POP}).json()["data"]["points"]
    p23 = next(p for p in dza if p["period_label"].startswith("2023"))
    assert p23["revision_no"] == 2 and p23["source_flags"] == _stored_flags(env, p23["observation_id"])


def test_r7_source_flags_are_bounded() -> None:
    from reva.api.deps import MAX_FLAG_BYTES, MAX_FLAG_KEYS, MAX_FLAG_STR_CHARS, bounded_source_flags

    assert bounded_source_flags(None) == (None, False)
    assert bounded_source_flags(["not", "a", "dict"]) == (None, False)
    small = {"isReported": False, "legacyEstimationFlag": 6, "provider_value_estimated": None,
             "categories": {"geo": "FR", "unit": "THS_T"}}
    assert bounded_source_flags(small) == (small, False)
    big: dict[str, Any] = {f"k{i:03d}": "x" * 50 for i in range(200)}
    big.update(provider_quantity_estimated=True, provider_value_estimated=None, long="y" * 5000,
               deep={"a": {"b": {"c": 1}}, "ok": 1}, lst=list(range(100)))
    out, cut = bounded_source_flags(big)
    assert cut and out is not None
    assert len(out) <= MAX_FLAG_KEYS and len(json.dumps(out)) <= MAX_FLAG_BYTES
    # Estimation flags survive the bound.
    assert out["provider_quantity_estimated"] is True and out["provider_value_estimated"] is None
    deep, _ = bounded_source_flags({"deep": {"a": {"b": 1}, "ok": 1}, "long": "y" * 5000, "lst": list(range(100))})
    assert deep == {"deep": {"ok": 1}, "long": "y" * MAX_FLAG_STR_CHARS, "lst": list(range(20))}


def test_r7_simulation_and_scenario_valid_time_is_reference_year(env: Env) -> None:
    from reva.api.routes.sim import scenario_valid_time

    year = json.loads(EXAMPLE.read_text())["reference_year"]
    want = {"start": f"{year}-01-01T00:00:00Z", "end": f"{year + 1}-01-01T00:00:00Z", "bounds": "[)",
            "basis": "scenario_reference_year", "reference_year": year}
    sid = env.client.post("/v1/scenarios", json=_scenario(env, title="r7 valid time")).json()["scenario_id"]
    assert env.client.get(f"/v1/scenarios/{sid}").json()["valid_time"] == want
    acc = env.client.post("/v1/simulations", json={"scenario_id": sid, "fidelity": "L0"},
                          headers={"Idempotency-Key": f"r7-vt-{uuid.uuid4()}"})
    assert acc.status_code == 202, acc.text
    st = env.client.get(f"/v1/simulations/{acc.json()['job_id']}").json()
    assert st["valid_time"] == want
    assert scenario_valid_time({}) is None and scenario_valid_time({"reference_year": "2023"}) is None
    assert scenario_valid_time({"reference_year": True}) is None


def test_r7_entity_origin_is_derived_from_its_data(env: Env) -> None:
    d = env.client.get("/v1/entities/FRA").json()
    # Fixture-only instance: the entity's observations and estimates are all test_fixture.
    assert d["origin"] == "test_fixture"
    assert any(w["code"] == "fixture_data" for w in d["warnings"])
    with env.owner.connect() as c:
        bare = c.execute(text(
            """SELECT e.id FROM world.entity e WHERE NOT EXISTS (
                 SELECT 1 FROM evidence.observation o WHERE o.subject_id = e.id)
               AND NOT EXISTS (SELECT 1 FROM belief.estimate b WHERE b.subject_id = e.id) LIMIT 1""")
        ).scalar_one_or_none()
    if bare is not None:
        b = env.client.get(f"/v1/entities/{bare}").json()
        assert b["origin"] == "none" and not any(w["code"] == "fixture_data" for w in b["warnings"])
    # Never claims live: no live acquisition exists in this database.
    with env.owner.connect() as c:
        assert not c.execute(text("SELECT count(*) FROM evidence.acquisition WHERE origin = 'live'")).scalar_one()
    for iso3 in ("DZA", "CHN", "USA"):
        assert env.client.get(f"/v1/entities/{iso3}").json()["origin"] == "test_fixture"
