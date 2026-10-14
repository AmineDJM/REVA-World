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

    v1, b1 = pop({"release_id": str(env.r1.release_id)})
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
    d = env.client.post("/v1/discovery", json={"catalogue": "eurostat", "query": "population age sex"},
                        headers={"Idempotency-Key": f"disc-{uuid.uuid4()}"})
    assert d.status_code == 202, d.text


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
