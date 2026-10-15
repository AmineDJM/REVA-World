"""V2 platform end to end: dataset inputs pinned by a release, a multi-period plugin,
the experimental-model label, migration 0008 constraints and grants.

Runs in its own throwaway database (bootstrapped exactly like a deployment) so the
datasets registered here never enter releases published by other test modules.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pyarrow as pa
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres import repositories as repo
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import ValidationProblem
from reva.kernel.time import utc_now
from reva.operations.context import JobContext
from reva.settings import Settings
from reva.simulation import plugins
from reva.simulation.datasets import register_dataset
from reva.simulation.jobs import handle_simulation
from reva.simulation.plugins import InputRequirement, ResolvedInputs, ResultRow
from reva.simulation.spec import CONTRACTS, validate_scenario_spec

pytestmark = pytest.mark.db

CONCEPT = "test.v2.dataset.age_table"
GDP = "economics.gdp.nominal_current_usd"
MODEL_KEY = "test.v2_dataset_projection"


class DatasetProjection:
    """Toy multi-period plugin: sums a pinned dataset column, scales by GDP share, one row per year.

    Not a scientific model — it exercises the platform: dataset + scalar inputs,
    half-open periods, platform-owned missing-input refusal, experimental label."""

    model_key = MODEL_KEY
    model_version = "0.1.0"
    fidelities = frozenset({"L1"})
    scheduling = "deterministic: subjects sorted; one pass per year"

    def spec_schema(self) -> dict[str, Any]:
        return {
            "schema_version": {"const": "2.0.0"},
            "horizon": {"type": "object", "additionalProperties": False,
                        "properties": {"periods": {"type": "integer", "minimum": 1, "maximum": 5},
                                       "unit": {"const": "calendar_year"}}, "required": ["periods", "unit"]},
            "intervention": {"type": "object", "additionalProperties": False,
                             "properties": {"kind": {"const": "scale"}, "factor": {"type": "number"}},
                             "required": ["kind", "factor"]},
            "assumptions": {"type": "object", "additionalProperties": False, "properties": {}},
            "outputs": {"type": "array", "minItems": 1, "items": {"enum": ["scaled_total"]}},
        }

    def validate(self, spec: dict[str, Any]) -> None:
        if spec["intervention"]["factor"] <= 0:
            raise ValidationProblem("factor must be > 0")

    def requirements(self, spec: dict[str, Any]) -> list[InputRequirement]:
        out = []
        for iso in spec["countries"]:
            out.append(InputRequirement("table", CONCEPT, iso, (spec["reference_year"],), "dataset"))
            out.append(InputRequirement("G", GDP, iso, (spec["reference_year"],), "scalar"))
        return out

    def compile(self, inputs: ResolvedInputs, spec: dict[str, Any]) -> Any:
        return {"spec": spec, "inputs": inputs}

    def compute(self, compiled: Any, fidelity: str) -> list[ResultRow]:
        spec, inputs = compiled["spec"], compiled["inputs"]
        factor = Decimal(repr(spec["intervention"]["factor"]))
        y0 = spec["reference_year"]
        rows = []
        for iso in sorted(spec["countries"]):
            total = sum(Decimal(str(v)) for v in inputs.dataset("table", iso).frame()["persons"].to_list())
            for k in range(spec["horizon"]["periods"]):
                rows.append(ResultRow(
                    "scaled_total", iso, inputs.subject(iso).entity_id, "L1",
                    {"kind": "decimal", "value": str(total * factor ** (k + 1)), "unit_ref": "unit.count"},
                    {"status": "unquantified", "kind": "unknown"}, {"country": iso, "toy": True},
                    (date(y0 + k + 1, 1, 1), date(y0 + k + 2, 1, 1))))
        return rows

    def manifest(self) -> dict[str, Any]:
        return {"implementation": __name__, "review_status": "maintainer_review_pending"}


@dataclass
class Env:
    settings: Settings
    owner: Engine
    api: Engine
    worker: Engine
    sim: Engine
    store: FilesystemArtifactStore
    release_id: UUID
    cutoff: Any
    dataset_id: UUID


def _register_model_version(owner: Engine, store: FilesystemArtifactStore) -> None:
    with owner.begin() as conn:
        act = repo.simple_activity("registry.seed", "review", "maintainer:registry-seed", {"test": MODEL_KEY})
        repo.record_activity(conn, act)
        spec = {"model_key": MODEL_KEY, "version": "0.1.0", "kind": "scenario", "review_status":
                "maintainer_review_pending"}
        sha = repo.store_json_artifact(conn, store, spec, "models", schema_version="model-spec/1")
        conn.execute(text("""INSERT INTO model.version(model_key, version, kind, spec_sha256, code_digest, input_ports,
                               output_ports, applicability, assumptions, approval_status, provenance_activity_id)
                             VALUES (:k, '0.1.0', 'scenario', :sha, 'test', '[]', '[]', '{}', '[]', 'candidate', :a)"""),
                     {"k": MODEL_KEY, "sha": sha, "a": act.id})


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import seed

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    root = tmp_path_factory.mktemp("v2objects")
    with throwaway_database(base, prefix="reva_test_v2ds") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        seed(engines["migration"], store)

        async def ingest() -> None:
            for name in ("worldbank.wdi.indicators", "un.comtrade.public_exact"):
                c = builtin_connectors()[name]
                await run_connector(c, FixtureHttp(c.describe().name), store, engines["worker"],
                                    persist_cursor=False)

        asyncio.run(ingest())
        with engines["worker"].begin() as conn:
            acq = conn.execute(text("SELECT id FROM evidence.acquisition WHERE origin='test_fixture' "
                                    "ORDER BY recorded_at LIMIT 2")).scalars().all()
            table = pa.table({"age": [0, 1, 2], "sex": ["F", "M", "F"], "persons": [100, 200, 300]})
            rec = register_dataset(conn, store, dataset_key="test.v2.age_table", concept_id=CONCEPT,
                                   subject_key="FRA", table=table, source_acquisition_ids=acq,
                                   parser_version="test-parser@1", origin=Origin.test_fixture,
                                   coverage={"years": [2023]})
            again = register_dataset(conn, store, dataset_key="test.v2.age_table", concept_id=CONCEPT,
                                     subject_key="FRA", table=table, source_acquisition_ids=acq,
                                     parser_version="test-parser@1", origin=Origin.test_fixture)
            assert rec.created and not again.created and again.id == rec.id
        rel = publish_release(engines["worker"], store, cutoff=utc_now(), origin=Origin.test_fixture,
                              environment="test")
        _register_model_version(engines["migration"], store)
        plugins.register(DatasetProjection(), replace=True)
        try:
            yield Env(settings, engines["migration"], engines["api"], engines["worker"], engines["sim"], store,
                      rel.release_id, rel.knowledge_cutoff, rec.id)
        finally:
            plugins.unregister(MODEL_KEY)
            for e in engines.values():
                e.dispose()


def spec(env: Env, **over: Any) -> dict[str, Any]:
    doc = json.loads((CONTRACTS / "scenario.example.json").read_text())
    doc.update(schema_version="2.0.0", title="Test: V2 dataset projection", model_key=MODEL_KEY,
               model_version="0.1.0", baseline_release_id=str(env.release_id),
               knowledge_cutoff=env.cutoff.isoformat(), countries=["FRA"],
               horizon={"periods": 3, "unit": "calendar_year"}, intervention={"kind": "scale", "factor": 1.5},
               assumptions={}, outputs=["scaled_total"])
    doc.update(over)
    return doc


def create(env: Env, doc: dict[str, Any]) -> UUID:
    s = validate_scenario_spec(doc)
    with env.api.begin() as conn:
        return UUID(str(conn.execute(
            text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :b, :d, 'human:test')"),
            {"spec": json.dumps(s.as_json()), "b": s.canonical_bytes(), "d": s.digest()}).scalar_one()))


def run(env: Env, sid: UUID, fidelity: str) -> dict[str, Any]:
    payload = {"scenario_id": str(sid), "fidelity": fidelity}
    with env.owner.begin() as conn:
        jid = conn.execute(text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                                   VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
                           {"p": json.dumps(payload), "k": f"v2ds-{sid}-{fidelity}-{utc_now().timestamp()}"}
                           ).scalar_one()
        token = conn.execute(text("""UPDATE ops.job SET status='running', worker_id='t', attempts=attempts+1,
                                     lease_until=now()+interval '10 minutes', fencing_token=nextval('ops.fencing_seq')
                                     WHERE id=:j RETURNING fencing_token"""), {"j": jid}).scalar_one()

    def hb(_p: Any = None) -> str:
        with env.owner.connect() as c:
            return str(c.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": jid}).scalar_one())

    ctx = JobContext(job_id=jid, job_type="simulation", worker_id="t", fencing_token=int(token), attempt=1,
                     settings=env.settings, worker_engine=env.worker, sim_engine=env.sim, store=env.store,
                     heartbeat=hb)
    return handle_simulation(payload, ctx)


def q(engine: Engine, sql: str, **p: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings()]


def test_release_pins_dataset_by_digest_and_seals_it(env: Env) -> None:
    pins = q(env.owner, "SELECT * FROM belief.release_dataset WHERE release_id=:r", r=env.release_id)
    assert [(p["concept_id"], p["subject_key"], p["dataset_id"]) for p in pins] == [(CONCEPT, "FRA", env.dataset_id)]
    [rel] = q(env.owner, "SELECT manifest_sha256 FROM belief.release WHERE id=:r", r=env.release_id)
    manifest = json.loads(env.store.get("belief", rel["manifest_sha256"]))
    [entry] = manifest["dataset_inputs"]
    assert entry["artifact_sha256"] == pins[0]["artifact_sha256"] and entry["row_count"] == 3
    roles = {r["role"] for r in q(env.owner, "SELECT role FROM belief.release_artifact WHERE release_id=:r",
                                  r=env.release_id)}
    assert roles == {"estimates", "dataset"}
    # sealed: nothing can be pinned into a published release, and pins are immutable
    with pytest.raises(DBAPIError, match="sealed"), env.worker.begin() as c:
        c.execute(text("""INSERT INTO belief.release_dataset(release_id, dataset_id, concept_id, subject_key,
                          artifact_sha256) SELECT :r, id, concept_id, 'X' || subject_key, artifact_sha256
                          FROM evidence.dataset LIMIT 1"""), {"r": env.release_id})
    with pytest.raises(DBAPIError, match="Immutable"), env.owner.begin() as c:
        c.execute(text("UPDATE evidence.dataset SET row_count = 0"))
    with pytest.raises(DBAPIError, match="Immutable"), env.owner.begin() as c:
        c.execute(text("DELETE FROM belief.release_dataset"))


def test_dataset_registration_requires_archived_sources(env: Env) -> None:
    table = pa.table({"persons": [1]})
    with env.worker.begin() as conn:
        with pytest.raises(ValidationProblem):
            register_dataset(conn, env.store, dataset_key="test.v2.bad", concept_id=CONCEPT, subject_key="DEU",
                             table=table, source_acquisition_ids=[], parser_version="p@1",
                             origin=Origin.test_fixture)
        with pytest.raises(ValidationProblem):
            register_dataset(conn, env.store, dataset_key="test.v2.bad", concept_id=CONCEPT, subject_key="DEU",
                             table=table, source_acquisition_ids=[UUID(int=7)], parser_version="p@1",
                             origin=Origin.test_fixture)
        acq = conn.execute(text("SELECT id FROM evidence.acquisition LIMIT 1")).scalar_one()
        with pytest.raises(ValidationProblem):  # fixture bytes can never back a live dataset
            register_dataset(conn, env.store, dataset_key="test.v2.bad", concept_id=CONCEPT, subject_key="DEU",
                             table=table, source_acquisition_ids=[acq], parser_version="p@1", origin=Origin.live)
        conn.rollback()


def test_grants_worker_writes_sim_and_api_only_read(env: Env) -> None:
    for eng in (env.sim, env.api):
        assert q(eng, "SELECT count(*) AS n FROM evidence.dataset")[0]["n"] >= 1
        assert q(eng, "SELECT count(*) AS n FROM belief.release_dataset")[0]["n"] >= 1
        for table in ("evidence.dataset", "belief.release_dataset"):
            with pytest.raises(DBAPIError, match="permission denied"), eng.begin() as c:
                c.execute(text(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1"))


def test_multi_period_dataset_model_runs_through_the_job_path(env: Env) -> None:
    sid = create(env, spec(env))
    ref = run(env, sid, "L1")
    assert ref["status"] == "succeeded", ref
    rows = q(env.owner, """SELECT output_key, variant, value, period_start, period_end FROM sim.result
                           WHERE run_id=:r ORDER BY period_start""", r=ref["run_id"])
    assert [(r["period_start"], r["period_end"]) for r in rows] == [
        (date(2024, 1, 1), date(2025, 1, 1)), (date(2025, 1, 1), date(2026, 1, 1)),
        (date(2026, 1, 1), date(2027, 1, 1))]
    assert [Decimal(r["value"]["value"]) for r in rows] == [Decimal("900.0"), Decimal("1350.00"),
                                                          Decimal("2025.000")]
    [run_row] = q(env.owner, "SELECT model_manifest_sha256, execution_profile FROM sim.run WHERE id=:r",
                  r=ref["run_id"])
    mm = json.loads(env.store.get("scenarios", run_row["model_manifest_sha256"]))
    assert mm["models"][0]["experimental"] is True
    assert mm["models"][0]["label"] == "experimental model, not yet reviewed"
    assert run_row["execution_profile"]["scheduling"] == DatasetProjection.scheduling
    [outcome] = q(env.owner, "SELECT applicability, provenance_activity_id FROM sim.run_outcome WHERE run_id=:r",
                  r=ref["run_id"])
    assert outcome["applicability"]["model_label"] == "experimental model, not yet reviewed"
    ds = outcome["applicability"]["input_provenance"]["datasets"][0]
    assert ds["dataset_id"] == str(env.dataset_id)
    inputs = {r["artifact_sha256"] for r in q(env.owner, "SELECT artifact_sha256 FROM meta.activity_input "
                                                          "WHERE activity_id=:a", a=outcome["provenance_activity_id"])}
    assert ds["artifact_sha256"] in inputs
    manifest = json.loads(env.store.get("scenarios", ref["result_manifest_sha256"]))
    assert manifest["results"][0]["period"] == {"start": "2024-01-01", "end": "2025-01-01", "bounds": "[)"}

    # L0 reuses the multi-period run bit for bit
    hit = run(env, sid, "L0")
    assert hit["status"] == "succeeded" and hit["cache_hit_of"] == ref["run_id"]
    assert hit["output_digest"] == ref["output_digest"]
    hit_rows = q(env.owner, "SELECT period_start FROM sim.result WHERE run_id=:r ORDER BY period_start",
                 r=hit["run_id"])
    assert [r["period_start"] for r in hit_rows] == [r["period_start"] for r in rows]


def test_unpinned_dataset_is_missing_input_never_zero(env: Env) -> None:
    sid = create(env, spec(env, countries=["FRA", "USA"]))
    ref = run(env, sid, "L1")
    assert ref["status"] == "missing_input"
    assert [(p["country"], p["input"], p.get("kind")) for p in ref["problems"]] == [("USA", "table", "dataset")]
    assert q(env.owner, "SELECT count(*) AS n FROM sim.result WHERE run_id=:r", r=ref["run_id"])[0]["n"] == 0


def test_result_period_constraints(env: Env) -> None:
    [r] = q(env.owner, "SELECT * FROM sim.result WHERE period_start IS NOT NULL LIMIT 1")
    for ps, pe in ((r["period_start"], None), (r["period_end"], r["period_start"])):
        with pytest.raises(DBAPIError, match="result_period_half_open"), env.owner.begin() as c:
            c.execute(text("""INSERT INTO sim.result(run_id, output_key, subject_id, variant, value, uncertainty,
                              applicability, provenance_activity_id, period_start, period_end)
                              SELECT run_id, output_key || 'x', subject_id, variant, value, uncertainty,
                                     applicability, provenance_activity_id, :ps, :pe
                              FROM sim.result WHERE id = :i"""), {"ps": ps, "pe": pe, "i": r["id"]})
    with pytest.raises(DBAPIError, match="result_identity"), env.owner.begin() as c:
        c.execute(text("""INSERT INTO sim.result(run_id, output_key, subject_id, variant, value, uncertainty,
                          applicability, provenance_activity_id, period_start, period_end)
                          SELECT run_id, output_key, subject_id, variant, value, uncertainty, applicability,
                                 provenance_activity_id, period_start, period_end FROM sim.result WHERE id = :i"""),
                  {"i": r["id"]})


def test_dataset_recorded_after_cutoff_is_not_pinned(env: Env) -> None:
    with env.worker.begin() as conn:
        acq = conn.execute(text("SELECT id FROM evidence.acquisition LIMIT 1")).scalars().all()
        newer = register_dataset(conn, env.store, dataset_key="test.v2.age_table", concept_id=CONCEPT,
                                 subject_key="FRA", table=pa.table({"age": [0], "sex": ["F"], "persons": [5]}),
                                 source_acquisition_ids=acq, parser_version="test-parser@2",
                                 origin=Origin.test_fixture)
    # the published release still pins the old dataset; the next release pins the newer one
    pins = q(env.owner, "SELECT dataset_id FROM belief.release_dataset WHERE release_id=:r", r=env.release_id)
    assert [p["dataset_id"] for p in pins] == [env.dataset_id]
    rel2 = publish_release(env.worker, env.store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test")
    pins2 = q(env.owner, "SELECT dataset_id FROM belief.release_dataset WHERE release_id=:r", r=rel2.release_id)
    assert [p["dataset_id"] for p in pins2] == [newer.id]
    with pytest.raises(DBAPIError, match="recorded after release cutoff"), env.owner.begin() as c:
        old_rel = c.execute(text("""SELECT r.id FROM belief.release r WHERE r.id = :r"""),
                            {"r": env.release_id}).scalar_one()
        # a fresh unpublished release with an early cutoff cannot pin the later dataset
        ctx = repo.create_context(c, mode="belief", origin="test_fixture",
                                  manifest_sha256=q(env.owner, "SELECT manifest_sha256 FROM belief.release "
                                                               "WHERE id=:r", r=old_rel)[0]["manifest_sha256"],
                                  knowledge_cutoff=env.cutoff)
        rid = c.execute(text("""INSERT INTO belief.release(context_id, origin, manifest_sha256,
                                 ontology_manifest_sha256, model_manifest_sha256, knowledge_cutoff, method,
                                 provenance_activity_id)
                                SELECT :c, origin, manifest_sha256, ontology_manifest_sha256, model_manifest_sha256,
                                       :cut, method, provenance_activity_id FROM belief.release WHERE id = :r
                                RETURNING id"""), {"c": ctx, "cut": env.cutoff, "r": old_rel}).scalar_one()
        c.execute(text("""INSERT INTO belief.release_dataset(release_id, dataset_id, concept_id, subject_key,
                          artifact_sha256) VALUES (:r, :d, :c, 'FRA', :a)"""),
                  {"r": rid, "d": newer.id, "c": CONCEPT, "a": newer.artifact_sha256})
