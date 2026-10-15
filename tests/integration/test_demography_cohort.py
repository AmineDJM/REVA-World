"""eurostat.demography -> dataset inputs -> belief release -> demography.cohort_component job path.

Offline: the recorded Eurostat fixtures (LI, LU since 2008 + EUROPOP2023). Runs in its own
throwaway database (bootstrapped like a deployment) so its datasets never enter releases of
other test modules.
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

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.connectors import eurostat_demography as ed
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.time import utc_now
from reva.models.population import cohort as C
from reva.operations.context import JobContext
from reva.settings import Settings
from reva.simulation.jobs import handle_simulation
from reva.simulation.spec import validate_scenario_spec

pytestmark = pytest.mark.db


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
    report: dict[str, Any]


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
    root = tmp_path_factory.mktemp("demography_objects")
    with throwaway_database(base, prefix="reva_test_demog") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        seed(engines["migration"], store)
        conn_ = ed.EurostatDemographyConnector(countries=("LI", "LU"), since_year=2008)
        report = asyncio.run(run_connector(conn_, FixtureHttp(ed.NAME), store, engines["worker"],
                                           persist_cursor=False, post_publish=ed.post_publish_datasets))
        rel = publish_release(engines["worker"], store, cutoff=utc_now(), origin=Origin.test_fixture,
                              environment="test")
        try:
            yield Env(settings, engines["migration"], engines["api"], engines["worker"], engines["sim"], store,
                      rel.release_id, rel.knowledge_cutoff, report.as_dict())
        finally:
            for e in engines.values():
                e.dispose()


def q(engine: Engine, sql: str, **p: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings()]


def spec(env: Env, **over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0", "title": "Test: cohort-component projection",
        "baseline_release_id": str(env.release_id), "knowledge_cutoff": env.cutoff.isoformat(),
        "model_key": "demography.cohort_component", "model_version": "1.0.0", "countries": ["LUX", "LIE"],
        "reference_year": 2025, "horizon": {"periods": 10, "unit": "calendar_year"},
        "intervention": {"kind": "baseline_projection"},
        "assumptions": {"mortality": "constant_base_period_rates", "fertility": "constant_base_period_rates",
                        "migration": "average_residual", "base_period": {"first_year": 2019, "last_year": 2023}},
        "fidelity": "L1", "missing_input_policy": "fail",
        "outputs": ["population.total.all_ages", "population.female.age_65_plus",
                    "old_age_dependency_ratio_65plus_per_15_64", "old_age_dependency_ratio_67plus_per_20_66",
                    "births.total", "deaths.total", "net_migration.total"],
        "seed": 0,
    }
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
                           {"p": json.dumps(payload), "k": f"demog-{sid}-{fidelity}-{utc_now().timestamp()}"}
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


def test_ingest_publishes_observations_and_registers_dataset_inputs(env: Env) -> None:
    assert env.report["errors"] == [] and env.report["inserted"] > 0
    assert {o["status"] for o in env.report["outcomes"]} == {"published"}
    rows = q(env.owner, """SELECT concept_id, subject_key, dataset_key, row_count, source_acquisition_ids,
                                  parser_version, coverage FROM evidence.dataset ORDER BY concept_id, subject_key""")
    got = {(r["concept_id"], r["subject_key"]) for r in rows}
    observed = {ed.DATASETS[d].concept_id for d in ed.OBSERVED}
    europop = {ed.DATASETS[d].concept_id for d in ed.EUROPOP}
    assert got == {(c, s) for c in observed for s in ("LIE", "LUX")} | {(c, "LUX") for c in europop}
    assert all(r["parser_version"] == ed.PARSER_VERSION and len(r["source_acquisition_ids"]) == 1 for r in rows)
    # every dataset names an acquisition whose archived raw bytes it was derived from
    acq = q(env.owner, """SELECT d.concept_id, a.raw_sha256 FROM evidence.dataset d
                          JOIN evidence.acquisition a ON a.id = d.source_acquisition_ids[1]""")
    assert len(acq) == len(rows) and all(a["raw_sha256"] for a in acq)
    # projections are dataset inputs only, never observations
    obs = q(env.owner, """SELECT DISTINCT c.concept_id FROM evidence.observation o
                          JOIN ontology.concept_version c ON c.id = o.concept_version_id
                          JOIN evidence.connector_version v ON v.id = o.connector_version_id
                          WHERE v.name = 'eurostat.demography'""")
    assert {r["concept_id"] for r in obs} == observed


def test_release_pins_datasets_and_selects_no_single_age_beliefs(env: Env) -> None:
    pins = q(env.owner, "SELECT concept_id, subject_key FROM belief.release_dataset WHERE release_id=:r",
             r=env.release_id)
    assert ("demography.population.jan1_age_sex", "LUX") in {(p["concept_id"], p["subject_key"]) for p in pins}
    est = q(env.owner, """SELECT count(*) AS n FROM belief.estimate e
                          JOIN ontology.concept_version c ON c.id = e.concept_version_id
                          WHERE e.release_id = :r AND c.concept_id = 'demography.deaths.age_sex'""",
            r=env.release_id)
    assert est[0]["n"] == 0  # no selection preference for this source: evidence only


def test_l1_projection_runs_through_the_job_path(env: Env) -> None:
    sid = create(env, spec(env))
    ref = run(env, sid, "L1")
    assert ref["status"] == "succeeded", ref
    rows = q(env.owner, """SELECT output_key, variant, value, subject_id, period_start, period_end, applicability
                           FROM sim.result WHERE run_id=:r""", r=ref["run_id"])
    pop = sorted((r for r in rows if r["output_key"] == "population.total.all_ages"),
                 key=lambda r: (str(r["subject_id"]), r["period_start"]))
    assert len(pop) == 2 * 11
    assert {r["period_start"].year for r in pop} == set(range(2025, 2036))
    assert all(r["period_end"] == date(r["period_start"].year, 1, 2) for r in pop)
    births = [r for r in rows if r["output_key"] == "births.total"]
    assert all(r["period_end"] == date(r["period_start"].year + 1, 1, 1) for r in births)
    assert all(r["applicability"]["label"] == C.LABEL for r in rows)
    lux2025 = next(r for r in pop if r["period_start"].year == 2025 and r["applicability"]["country"] == "LUX")
    with env.owner.connect() as c:
        tab = c.execute(text("""SELECT d.artifact_sha256 FROM belief.release_dataset p JOIN evidence.dataset d
                                ON d.id = p.dataset_id WHERE p.release_id=:r AND p.subject_key='LUX'
                                AND p.concept_id='demography.population.jan1_age_sex'"""),
                        {"r": env.release_id}).scalar_one()
    import io

    import pyarrow.parquet as pq

    t = pq.read_table(io.BytesIO(env.store.get("normalised", tab))).to_pylist()
    observed_total = next(Decimal(r["value"]) for r in t
                          if r["year"] == 2025 and r["sex"] == "T" and r["age"] == "TOTAL")
    unknown = sum(Decimal(r["value"]) for r in t if r["year"] == 2025 and r["sex"] != "T" and r["age"] == "UNK"
                  and r["value"])
    assert Decimal(lux2025["value"]["value"]) == observed_total - unknown  # jump-off = observed (UNK excluded)
    [outcome] = q(env.owner, "SELECT applicability FROM sim.run_outcome WHERE run_id=:r", r=ref["run_id"])
    app = outcome["applicability"]
    assert app["model_label"] == "experimental model, not yet reviewed"
    assert app["label"] == C.LABEL and app["countries"]["LIE"]["open_age"] >= C.MIN_OPEN_AGE
    hit = run(env, sid, "L0")
    assert hit["status"] == "succeeded" and hit["cache_hit_of"] == ref["run_id"]


def test_l2_sensitivity_grid_and_range_rows(env: Env) -> None:
    doc = spec(env, fidelity="L2", countries=["LUX"], horizon={"periods": 5, "unit": "calendar_year"},
               assumptions={**spec(env)["assumptions"],
                            "sensitivity": {"migration": ["zero", "average_residual", "europop2023_bsl"],
                                            "fertility": ["constant_base_period_rates", "europop2023_bsl"]}})
    ref = run(env, create(env, doc), "L2")
    assert ref["status"] == "succeeded", ref
    rows = q(env.owner, "SELECT output_key, variant, value, uncertainty, period_start FROM sim.result "
                        "WHERE run_id=:r", r=ref["run_id"])
    assert len({r["variant"] for r in rows}) == 7
    rng = [r for r in rows if r["variant"] == "L2:sensitivity_range"]
    assert rng and all(r["uncertainty"]["kind"] == "sensitivity_range" and r["uncertainty"].get("level") is None
                       for r in rng)
    l1 = {(r["output_key"], r["period_start"]): Decimal(r["value"]["value"]) for r in rows if r["variant"] == "L1"}
    for r in rng:
        assert Decimal(r["value"]["lower"]) <= l1[(r["output_key"], r["period_start"])] <= Decimal(
            r["value"]["upper"])


def test_missing_europop_for_liechtenstein_is_missing_input(env: Env) -> None:
    doc = spec(env, countries=["LIE"], assumptions={**spec(env)["assumptions"], "migration": "europop2023_bsl"})
    ref = run(env, create(env, doc), "L1")
    assert ref["status"] == "missing_input"
    assert {p.get("input") for p in ref["problems"]} == {"europop_migration"}
