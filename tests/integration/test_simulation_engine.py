"""Simulation engine end to end against a fixture-built, published baseline release.

The baseline is built by replaying captured WDI and Comtrade responses through the
real ingest path (origin=test_fixture) and publishing a belief release. Scenario
creation uses the API role's narrow function; jobs are claimed manually and run
through ``handle_simulation`` with the reva_sim role.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from reva.adapters.connectors.registry import builtin_connectors
from reva.belief.publication import publish_release
from reva.belief.snapshots import estimates
from reva.cli.seed import entity_id
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import CapabilityGap, StaleLease
from reva.kernel.ids import logical_hash
from reva.kernel.time import utc_now
from reva.operations.context import JobContext
from reva.simulation.jobs import handle_simulation
from reva.simulation.spec import CONTRACTS, validate_scenario_spec

pytestmark = pytest.mark.db

CONCEPTS = {
    "M": "trade.hs2709.import_value.world_total",
    "X": "trade.hs2709.export_value.world_total",
    "G": "economics.gdp.nominal_current_usd",
}
OUTPUTS = ("import_value_change_usd", "export_value_change_usd", "net_external_cost_change_usd",
           "exposure_percent_baseline_gdp")


@pytest.fixture(scope="module")
def baseline(db) -> dict[str, Any]:
    async def ingest() -> None:
        for name in ("worldbank.wdi.indicators", "un.comtrade.public_exact"):
            c = builtin_connectors()[name]
            await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)

    asyncio.run(ingest())
    rel = publish_release(db.worker, db.store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test")
    return {"release_id": rel.release_id, "cutoff": rel.knowledge_cutoff}


def make_spec(baseline: dict[str, Any], **over: Any) -> dict[str, Any]:
    spec = json.loads((CONTRACTS / "scenario.example.json").read_text())
    spec["baseline_release_id"] = str(baseline["release_id"])
    spec["knowledge_cutoff"] = baseline["cutoff"].isoformat()
    spec["title"] = "Test: 25% crude border-price stress"
    spec.update(over)
    return spec


def create_scenario(db, spec_dict: dict[str, Any], *, validate: bool = True) -> UUID:
    if validate:
        spec = validate_scenario_spec(spec_dict)
        payload, raw, digest = spec.as_json(), spec.canonical_bytes(), spec.digest()
    else:  # bypass API validation to prove the engine re-validates stored specs
        payload = spec_dict
        raw = json.dumps(spec_dict, sort_keys=True, separators=(",", ":")).encode()
        digest = logical_hash(spec_dict)
    with db.api.begin() as conn:
        sid = conn.execute(
            text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :bytes, :digest, 'human:test')"),
            {"spec": json.dumps(payload), "bytes": raw, "digest": digest},
        ).scalar_one()
    return UUID(str(sid))


def claim_job(db, payload: dict[str, Any], worker: str = "test-worker") -> JobContext:
    with db.owner.begin() as conn:
        jid = conn.execute(
            text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                    VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
            {"p": json.dumps(payload), "k": f"test-sim-{UUID(int=0).hex}-{utc_now().timestamp()}-{id(payload)}"},
        ).scalar_one()
        token = conn.execute(
            text("""UPDATE ops.job SET status='running', worker_id=:w, lease_until=now() + interval '10 minutes',
                        fencing_token=nextval('ops.fencing_seq'), attempts=attempts+1
                    WHERE id=:j RETURNING fencing_token"""),
            {"w": worker, "j": jid},
        ).scalar_one()

    def heartbeat(_progress: dict[str, Any] | None = None) -> str:
        with db.owner.connect() as c:
            return str(c.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": jid}).scalar_one())

    return JobContext(job_id=jid, job_type="simulation", worker_id=worker, fencing_token=int(token), attempt=1,
                      settings=db.settings, worker_engine=db.worker, sim_engine=db.sim, store=db.store,
                      heartbeat=heartbeat)


def run(db, scenario_id: UUID, fidelity: str | None = None) -> tuple[dict[str, Any], JobContext]:
    payload: dict[str, Any] = {"scenario_id": str(scenario_id)}
    if fidelity:
        payload["fidelity"] = fidelity
    ctx = claim_job(db, payload)
    return handle_simulation(payload, ctx), ctx


def job_row(db, job_id: UUID) -> dict[str, Any]:
    with db.owner.connect() as c:
        return dict(c.execute(text("SELECT * FROM ops.job WHERE id=:j"), {"j": job_id}).mappings().one())


def results(db, run_id: str) -> list[dict[str, Any]]:
    with db.owner.connect() as c:
        return [dict(r) for r in c.execute(
            text("""SELECT r.*, e.label FROM sim.result r JOIN world.entity e ON e.id = r.subject_id
                    WHERE run_id = :r ORDER BY output_key, label, variant"""), {"r": run_id}).mappings()]


def stored_inputs(db, baseline: dict[str, Any], iso3: str, year: int = 2023) -> dict[str, Decimal]:
    out = {}
    with db.owner.connect() as c:
        for n, concept in CONCEPTS.items():
            [row] = [r for r in estimates(c, baseline["release_id"], subject_id=entity_id(iso3), concept_id=concept)
                     if r["temporal_extent"].get("label") == str(year)]
            out[n] = Decimal(row["value"]["value"])
    return out


def count(db, sql: str, **params: Any) -> int:
    with db.owner.connect() as c:
        return int(c.execute(text(sql), params).scalar_one())


# --------------------------------------------------------------------------- tests
def test_l1_exposure_matches_hand_computation(db, baseline):
    beliefs_before = count(db, "SELECT count(*) FROM belief.estimate")
    sid = create_scenario(db, make_spec(baseline))
    ref, ctx = run(db, sid)
    assert ref["status"] == "succeeded", ref
    job = job_row(db, ctx.job_id)
    assert job["status"] == "succeeded" and job["result_ref"]["run_id"] == ref["run_id"]

    rows = results(db, ref["run_id"])
    assert len(rows) == 4 * 3
    by = {(r["label"], r["output_key"]): r for r in rows}
    for iso3, label in (("FRA", "France"), ("CHN", "China"), ("USA", "United States")):
        v = stored_inputs(db, baseline, iso3)
        with localcontext() as c:
            c.prec = 50
            d_m = v["M"] * (Decimal("1.25") - 1)
            d_x = v["X"] * (Decimal("1.25") - 1)
            d_net = d_m - d_x
            exposure = 100 * d_net / v["G"]
        expected = {"import_value_change_usd": d_m, "export_value_change_usd": d_x,
                    "net_external_cost_change_usd": d_net, "exposure_percent_baseline_gdp": exposure}
        for k, want in expected.items():
            r = by[(label, k)]
            assert Decimal(r["value"]["value"]) == want, (iso3, k)
            assert r["variant"] == "L1"
            assert r["uncertainty"]["status"] == "unquantified"
            assert r["applicability"]["modality"] == "assumption_conditioned_scenario"
        assert by[(label, "import_value_change_usd")]["value"]["unit_ref"] == "unit.usd"
        flags = by[(label, "import_value_change_usd")]["applicability"]["source_flags"]
        assert flags["M"]["valuation"] == "CIF" and flags["X"]["valuation"] == "FOB"

    # pinned provenance
    with db.owner.connect() as c:
        run_row = c.execute(text("SELECT * FROM sim.run WHERE id=:r"), {"r": ref["run_id"]}).mappings().one()
        outcome = c.execute(text("SELECT * FROM sim.run_outcome WHERE run_id=:r"),
                            {"r": ref["run_id"]}).mappings().one()
        rel = c.execute(text("SELECT * FROM belief.release WHERE id=:r"),
                        {"r": baseline["release_id"]}).mappings().one()
        ns = c.execute(text("SELECT namespace FROM meta.artifact_location WHERE artifact_sha256=:s"),
                       {"s": outcome["output_manifest_sha256"]}).scalars().all()
        model_ns = c.execute(text("SELECT namespace FROM meta.artifact_location WHERE artifact_sha256=:s"),
                             {"s": run_row["model_manifest_sha256"]}).scalars().all()
        act = c.execute(text("SELECT origin, kind FROM meta.activity WHERE id=:a"),
                        {"a": outcome["provenance_activity_id"]}).mappings().one()
        act_inputs = c.execute(text("SELECT artifact_sha256 FROM meta.activity_input WHERE activity_id=:a"),
                               {"a": outcome["provenance_activity_id"]}).scalars().all()
    assert run_row["ontology_manifest_sha256"] == rel["ontology_manifest_sha256"]
    assert run_row["seed_plan"]["run_seed"] == 1729 and run_row["job_id"] == ctx.job_id
    assert run_row["execution_profile"]["fidelity"] == "L1"
    assert outcome["status"] == "succeeded" and outcome["output_digest"] == ref["output_digest"]
    assert ns == ["scenarios"] and "scenarios" in model_ns
    assert act["origin"] == "simulation"
    assert rel["manifest_sha256"] in act_inputs
    manifest = json.loads(db.store.get("scenarios", outcome["output_manifest_sha256"]))
    assert manifest["status"] == "succeeded"
    assert manifest["baseline"]["release_id"] == str(baseline["release_id"])
    assert "not a GDP loss" in manifest["applicability"]["explanation"]["not_claimed"]
    # recompute the output digest from the stored rows
    digest = logical_hash(sorted(({"output_key": r["output_key"], "subject_id": str(r["subject_id"]),
                                   "variant": r["variant"], "value": r["value"]} for r in rows),
                                 key=lambda d: (d["output_key"], d["subject_id"], d["variant"])))
    assert digest == outcome["output_digest"]

    # idempotent: a second job for the same request reuses the immutable run
    ref2, ctx2 = run(db, sid)
    assert ref2["run_id"] == ref["run_id"] and ref2["idempotent_replay"] is True
    assert job_row(db, ctx2.job_id)["status"] == "succeeded"
    assert len(results(db, ref["run_id"])) == 12

    # scenario writes cannot change beliefs
    assert count(db, "SELECT count(*) FROM belief.estimate") == beliefs_before


def test_all_four_countries_returns_missing_input_for_dza(db, baseline):
    sid = create_scenario(db, make_spec(baseline, countries=["FRA", "DZA", "CHN", "USA"]))
    ref, ctx = run(db, sid)
    assert ref["status"] == "missing_input"
    missing = [p for p in ref["problems"] if p["code"] == "missing_input"]
    assert {p["country"] for p in missing} == {"DZA"}
    assert {p["input"] for p in missing} == {"M", "X"}
    assert all(p["reference_year"] == 2023 for p in missing)
    assert results(db, ref["run_id"]) == []  # nothing silently computed for the other three
    job = job_row(db, ctx.job_id)
    assert job["status"] == "failed" and job["result_ref"]["status"] == "missing_input"
    assert job["last_error"]["code"] == "missing_input"
    with db.owner.connect() as c:
        o = c.execute(text("SELECT * FROM sim.run_outcome WHERE run_id=:r"), {"r": ref["run_id"]}).mappings().one()
    assert o["status"] == "missing_input" and o["output_digest"] is None
    assert o["applicability"]["countries"]["DZA"]["status"] == "missing_input"
    assert o["applicability"]["countries"]["FRA"]["status"] == "ready"


def test_five_year_horizon_rejected(db, baseline):
    bad = make_spec(baseline, horizon={"periods": 5, "unit": "annual_accounting_period"},
                    title="Test: five-year horizon")
    with pytest.raises(CapabilityGap):
        validate_scenario_spec(bad)
    # even if a malformed spec were stored, the engine re-validates and records unsupported
    sid = create_scenario(db, bad, validate=False)
    ref, ctx = run(db, sid)
    assert ref["status"] == "unsupported"
    assert ref["problems"][0]["code"] == "capability_gap"
    assert results(db, ref["run_id"]) == []
    assert job_row(db, ctx.job_id)["status"] == "failed"


def test_l3_and_l4_rejected(db, baseline):
    with pytest.raises(CapabilityGap):
        validate_scenario_spec(make_spec(baseline, fidelity="L3"))
    sid = create_scenario(db, make_spec(baseline, title="Test: L3 request"))
    for fid in ("L3", "L4"):
        ref, _ = run(db, sid, fid)
        assert ref["status"] == "unsupported" and ref["problems"][0]["code"] == "capability_gap"
        assert results(db, ref["run_id"]) == []
        with db.owner.connect() as c:
            assert c.execute(text("SELECT fidelity FROM sim.run WHERE id=:r"),
                             {"r": ref["run_id"]}).scalar_one() == fid


def test_l0_cache_miss_then_hit_after_l1(db, baseline):
    sid = create_scenario(db, make_spec(baseline, seed=7, title="Test: cache"))
    miss, ctx_miss = run(db, sid, "L0")
    assert miss["status"] == "cache_miss" and results(db, miss["run_id"]) == []
    assert miss["problems"][0]["code"] == "cache_miss"
    assert job_row(db, ctx_miss.job_id)["status"] == "failed"

    l1, _ = run(db, sid, "L1")
    assert l1["status"] == "succeeded"
    hit, ctx_hit = run(db, sid, "L0")
    assert hit["status"] == "succeeded" and hit["cache_hit_of"] == l1["run_id"]
    assert hit["output_digest"] == l1["output_digest"]
    assert job_row(db, ctx_hit.job_id)["status"] == "succeeded"
    a = {(r["output_key"], r["label"]): r["value"] for r in results(db, l1["run_id"])}
    b = {(r["output_key"], r["label"]): r["value"] for r in results(db, hit["run_id"])}
    assert a == b and len(a) == 12

    # an L0 *scenario* with the same full specification also hits
    sid_l0 = create_scenario(db, make_spec(baseline, seed=7, title="Test: cache", fidelity="L0"))
    hit2, _ = run(db, sid_l0)
    assert hit2["status"] == "succeeded" and hit2["cache_hit_of"] == l1["run_id"]
    # any change to the specification is a miss
    sid_other = create_scenario(db, make_spec(baseline, seed=8, title="Test: cache", fidelity="L0"))
    other, _ = run(db, sid_other)
    assert other["status"] == "cache_miss"


def test_l2_sensitivity_range(db, baseline):
    grid = [{"import_elasticity": -0.5, "export_elasticity": 0.0, "basis": "scenario_assumption"},
            {"import_elasticity": 0.0, "export_elasticity": 0.0, "basis": "scenario_assumption"},
            {"import_elasticity": -0.2, "export_elasticity": 0.3, "basis": "scenario_assumption"}]
    sid = create_scenario(db, make_spec(baseline, fidelity="L2", title="Test: L2",
                                        assumptions={"quantity_response": "elasticity_sensitivity",
                                                     "elasticity_grid": grid, "no_general_equilibrium": True}))
    ref, _ = run(db, sid)
    assert ref["status"] == "succeeded", ref
    rows = results(db, ref["run_id"])
    # per country/output: L1 + 3 grid variants + 1 separate sensitivity-range row
    assert len(rows) == 4 * 3 * (1 + 3 + 1)
    for r in rows:
        u = r["uncertainty"]
        if r["variant"] == "L2:sensitivity_range":
            v = r["value"]
            assert v["kind"] == "sensitivity_range"
            assert v["statement"] == "assumption sensitivity, not a confidence interval"
            assert u["status"] == "quantified" and u["kind"] == "sensitivity_range" and u["level"] is None
            assert u["kind"] != "confidence_interval"
            assert (u["lower"], u["upper"]) == (v["lower"], v["upper"])
            assert Decimal(u["lower"]) <= Decimal(u["upper"])
            same = [x for x in rows if x["label"] == r["label"] and x["output_key"] == r["output_key"]]
            points = [Decimal(x["value"]["value"]) for x in same
                      if x["variant"].startswith("L2:eM=") or x["variant"] == "L1"]
            assert Decimal(u["lower"]) == min(points) and Decimal(u["upper"]) == max(points)
            l1 = next(Decimal(x["value"]["value"]) for x in same if x["variant"] == "L1")
            assert Decimal(u["lower"]) <= l1 <= Decimal(u["upper"])
            assert len(u["conditioning"]) == 3 + 1  # grid points + the always-included (0,0) point
        else:
            # the L1 fixed-quantity row and every grid variant keep the unquantified status (R3-03)
            assert u["status"] == "unquantified" and u["kind"] == "unknown", r
            assert r["value"]["kind"] == "decimal"
    by = {(r["label"], r["output_key"], r["variant"]): r["value"]["value"] for r in rows}
    for label in ("France", "China", "United States"):
        for k in OUTPUTS:
            assert by[(label, k, "L2:eM=0;eX=0")] == by[(label, k, "L1")]  # zero elasticity == L1 exactly
        # negative import elasticity with a price rise lowers the import value change
        assert Decimal(by[(label, "import_value_change_usd", "L2:eM=-0.5;eX=0")]) < Decimal(
            by[(label, "import_value_change_usd", "L1")])


def test_stale_fencing_token_cannot_publish(db, baseline):
    sid = create_scenario(db, make_spec(baseline, title="Test: stale lease"))
    payload = {"scenario_id": str(sid), "fidelity": "L1"}
    ctx = claim_job(db, payload)
    with db.owner.begin() as c:  # another worker re-claimed the job: the token moves on
        c.execute(text("UPDATE ops.job SET fencing_token = nextval('ops.fencing_seq') WHERE id=:j"),
                  {"j": ctx.job_id})
    runs_before = count(db, "SELECT count(*) FROM sim.run")
    results_before = count(db, "SELECT count(*) FROM sim.result")
    with pytest.raises(StaleLease):
        handle_simulation(payload, ctx)
    assert count(db, "SELECT count(*) FROM sim.run") == runs_before
    assert count(db, "SELECT count(*) FROM sim.result") == results_before
    assert count(db, "SELECT count(*) FROM sim.run WHERE job_id=:j", j=ctx.job_id) == 0
    assert job_row(db, ctx.job_id)["status"] == "running"


def test_cancellation_before_publication(db, baseline):
    sid = create_scenario(db, make_spec(baseline, title="Test: cancel"))
    payload = {"scenario_id": str(sid)}
    ctx = claim_job(db, payload)
    with db.api.begin() as c:
        assert c.execute(text("SELECT ops.request_cancel(:j, 'human:test')"),
                         {"j": ctx.job_id}).scalar_one() == "cancel_requested"
    out = handle_simulation(payload, ctx)
    assert out["status"] == "cancelled"
    assert job_row(db, ctx.job_id)["status"] == "cancelled"
    assert count(db, "SELECT count(*) FROM sim.run WHERE job_id=:j", j=ctx.job_id) == 0


@pytest.mark.parametrize("table", ["evidence.observation", "belief.estimate", "belief.release"])
def test_sim_role_cannot_write_evidence_or_beliefs(db, baseline, table):
    before = count(db, f"SELECT count(*) FROM {table}")
    with pytest.raises(DBAPIError) as ei, db.sim.begin() as c:
        c.execute(text(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1"))
    assert "permission denied" in str(ei.value).lower()
    with pytest.raises(DBAPIError), db.sim.begin() as c:
        c.execute(text(f"DELETE FROM {table}"))
    assert count(db, f"SELECT count(*) FROM {table}") == before
