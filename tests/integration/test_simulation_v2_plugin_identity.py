"""V2 platform: the crude model as plugin #1 reproduces the V1 engine bit for bit.

The golden file ``tests/fixtures/simulation/crude_v1_db_golden.json`` was captured
by running this module with ``REVA_CAPTURE_SIM_GOLDEN=<path>`` against the V1
(pre-plugin) engine on the fixture-built baseline release. Every run below goes
through the real job path (``handle_simulation`` with the reva_sim role) and its
published rows, outcome applicability, problems, seed plan and cost counters must
equal the golden after masking per-database identifiers (release/estimate/run
UUIDs, the release-dependent baseline digest and wall-clock compute seconds).
Output digests are release independent and compared unmasked.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.belief.publication import publish_release
from reva.cli.seed import entity_id
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.ids import logical_hash
from reva.kernel.time import utc_now
from reva.operations.context import JobContext
from reva.simulation.jobs import handle_simulation
from reva.simulation.spec import CONTRACTS, validate_scenario_spec

pytestmark = pytest.mark.db

GOLDEN = Path(__file__).parents[1] / "fixtures" / "simulation" / "crude_v1_db_golden.json"
CAPTURE = os.environ.get("REVA_CAPTURE_SIM_GOLDEN")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SUBJECTS = {str(entity_id(i)): i for i in ("FRA", "CHN", "USA", "DZA")}
MASK_KEYS = {"baseline_digest", "compute_seconds", "cache_hit_of"}
GRID = [
    {"import_elasticity": -0.5, "export_elasticity": 0.0, "basis": "scenario_assumption"},
    {"import_elasticity": 0.0, "export_elasticity": 0.0, "basis": "scenario_assumption"},
    {"import_elasticity": -0.2, "export_elasticity": 0.3, "basis": "scenario_assumption"},
    {"import_elasticity": -0.2, "export_elasticity": 0.3, "basis": "scenario_assumption"},
]


def mask(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<masked>" if k in MASK_KEYS else mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    if isinstance(obj, str) and _UUID.match(obj):
        return f"<subject:{SUBJECTS[obj]}>" if obj in SUBJECTS else "<uuid>"
    return obj


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
    spec["title"] = "Test: plugin identity"
    spec.update(over)
    return spec


def create_scenario(db, spec_dict: dict[str, Any]) -> UUID:
    spec = validate_scenario_spec(spec_dict)
    with db.api.begin() as conn:
        sid = conn.execute(
            text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :bytes, :digest, 'human:test')"),
            {"spec": json.dumps(spec.as_json()), "bytes": spec.canonical_bytes(), "digest": spec.digest()},
        ).scalar_one()
    return UUID(str(sid))


def run(db, scenario_id: UUID, fidelity: str) -> dict[str, Any]:
    payload = {"scenario_id": str(scenario_id), "fidelity": fidelity}
    with db.owner.begin() as conn:
        jid = conn.execute(
            text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                    VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
            {"p": json.dumps(payload), "k": f"test-sim-identity-{utc_now().timestamp()}-{scenario_id}-{fidelity}"},
        ).scalar_one()
        token = conn.execute(
            text("""UPDATE ops.job SET status='running', worker_id='identity', lease_until=now()+interval '10 minutes',
                        fencing_token=nextval('ops.fencing_seq'), attempts=attempts+1
                    WHERE id=:j RETURNING fencing_token"""),
            {"j": jid},
        ).scalar_one()

    def heartbeat(_p: dict[str, Any] | None = None) -> str:
        with db.owner.connect() as c:
            return str(c.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": jid}).scalar_one())

    ctx = JobContext(
        job_id=jid,
        job_type="simulation",
        worker_id="identity",
        fencing_token=int(token),
        attempt=1,
        settings=db.settings,
        worker_engine=db.worker,
        sim_engine=db.sim,
        store=db.store,
        heartbeat=heartbeat,
    )
    return handle_simulation(payload, ctx)


def snapshot(db, ref: dict[str, Any]) -> dict[str, Any]:
    with db.owner.connect() as c:
        run_row = (
            c.execute(text("SELECT seed_plan, execution_profile FROM sim.run WHERE id=:r"), {"r": ref["run_id"]})
            .mappings()
            .one()
        )
        outcome = (
            c.execute(
                text(
                    "SELECT status, output_digest, applicability, problems, cost FROM sim.run_outcome WHERE run_id=:r"
                ),
                {"r": ref["run_id"]},
            )
            .mappings()
            .one()
        )
        rows = (
            c.execute(
                text("""SELECT output_key, subject_id::text AS subject_id, variant, value, uncertainty,
                                        applicability FROM sim.result WHERE run_id=:r
                                 ORDER BY output_key, subject_id, variant"""),
                {"r": ref["run_id"]},
            )
            .mappings()
            .all()
        )
    ep = dict(run_row["execution_profile"])
    for k in ("job_id", "attempt", "worker_id", "model_manifest_logical_sha256", "cache_key"):
        ep.pop(k, None)
    return mask(
        {
            "status": ref["status"],
            "output_digest": ref["output_digest"],
            "problems": ref["problems"],
            "outcome": {k: outcome[k] for k in ("status", "output_digest", "applicability", "problems")},
            "cost": {k: v for k, v in outcome["cost"].items() if k != "compute_seconds"},
            "seed_plan": run_row["seed_plan"],
            "execution_profile": ep,
            "rows": [dict(r) for r in rows],
        }
    )


def scenarios(baseline: dict[str, Any]) -> dict[str, tuple[dict[str, Any], str]]:
    return {
        "l1_three": (make_spec(baseline), "L1"),
        "l2_grid": (
            make_spec(
                baseline,
                fidelity="L2",
                assumptions={
                    "quantity_response": "elasticity_sensitivity",
                    "elasticity_grid": GRID,
                    "no_general_equilibrium": True,
                },
            ),
            "L2",
        ),
        "all_four_missing_dza": (make_spec(baseline, countries=["FRA", "DZA", "CHN", "USA"]), "L1"),
        "l3_reserved": (make_spec(baseline), "L3"),
        "l0_miss": (make_spec(baseline, seed=99, title="Test: identity cache"), "L0"),
        "l1_for_cache": (make_spec(baseline, seed=99, title="Test: identity cache"), "L1"),
        "l0_hit": (make_spec(baseline, seed=99, title="Test: identity cache"), "L0"),
    }


def test_crude_plugin_reproduces_v1_engine_on_fixture_release(db, baseline):
    got: dict[str, Any] = {}
    created: dict[str, UUID] = {}
    for name, (spec, fid) in scenarios(baseline).items():
        key = json.dumps(spec, sort_keys=True)
        if key not in created:
            created[key] = create_scenario(db, spec)
        ref = run(db, created[key], fid)
        got[name] = snapshot(db, ref)
        # the L0 cache key is the exact-spec identity, excluding only the fidelity (V1 formula)
        with db.owner.connect() as c:
            ck = c.execute(
                text("SELECT execution_profile->>'cache_key' FROM sim.run WHERE id=:r"), {"r": ref["run_id"]}
            ).scalar_one()
        stored = validate_scenario_spec({**spec, "fidelity": "L1" if fid in ("L0", "L3") else fid}).as_json()
        assert ck == logical_hash({k: v for k, v in stored.items() if k != "fidelity"}), name
    assert got["l0_hit"]["output_digest"] == got["l1_for_cache"]["output_digest"]
    if CAPTURE:
        Path(CAPTURE).write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False, default=str))
        pytest.skip(f"golden captured to {CAPTURE}")
    golden = json.loads(GOLDEN.read_text())
    assert set(got) == set(golden)
    for name in golden:
        assert json.loads(json.dumps(got[name], default=str)) == golden[name], name
