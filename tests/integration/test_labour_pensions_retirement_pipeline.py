"""eurostat.labour_pensions -> dataset inputs -> belief release -> social.retirement_age job path (France).

Offline: recorded Eurostat fixtures (this connector; FR demography recorded with agent POP's
connector under tests/fixtures/social). Runs in its own throwaway database, bootstrapped like a
deployment, so its datasets never enter releases of other test modules.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.connectors import eurostat_demography as ed
from reva.adapters.connectors import eurostat_labour as lab
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.time import utc_now
from reva.models.social import retirement_age as ra
from reva.operations.context import JobContext
from reva.settings import Settings
from reva.simulation.jobs import handle_simulation
from reva.simulation.spec import validate_scenario_spec

pytestmark = pytest.mark.db
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


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
    root = tmp_path_factory.mktemp("pen_objects")
    with throwaway_database(base, prefix="reva_test_pen") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        seed(engines["migration"], store)
        conn_ = lab.EurostatLabourPensionsConnector()
        report = asyncio.run(run_connector(conn_, FixtureHttp(lab.CONNECTOR_NAME), store, engines["worker"],
                                           persist_cursor=False, post_publish=lab.post_publish_datasets))
        demo = ed.EurostatDemographyConnector(countries=("FR",), include_europop=False)
        asyncio.run(run_connector(demo, FixtureHttp(ed.NAME, root=FIXTURES / "social"), store, engines["worker"],
                                  persist_cursor=False, post_publish=ed.post_publish_datasets))
        wb = builtin_connectors()["worldbank.wdi.indicators"]  # a release needs selected estimates
        asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), store, engines["worker"],
                                  persist_cursor=False))
        rel = publish_release(engines["worker"], store, cutoff=utc_now(), origin=Origin.test_fixture,
                              environment="test")
        try:
            yield Env(settings, engines["migration"], engines["api"], engines["worker"], engines["sim"], store,
                      rel.release_id, rel.knowledge_cutoff, report.as_dict())
        finally:
            for e in engines.values():
                e.dispose()


def pure_rows(doc: dict[str, Any]) -> list[Any]:
    from reva.ports.connectors import FetchBudget
    from reva.simulation.plugins import ResolvedInputs

    async def raws(c: Any, http: Any) -> list[Any]:
        return [await c.fetch(i, http) for i in c.plan(None, FetchBudget()).items]

    tables: dict[str, Any] = {}
    names = {"lfsa_ergan": "employment_rate", "lfsa_argan": "activity_rate", "spr_pns_ben": "pension_beneficiaries",
             "spr_exp_pens": "pension_expenditure"}
    for r in asyncio.run(raws(lab.EurostatLabourPensionsConnector(), FixtureHttp(lab.CONNECTOR_NAME))):
        code = r.item.params["table"]
        if code in names:
            tables[names[code]] = lab.derive_dataset_tables(r.body, code)["FRA"]
    demo = {"demo_pjan": "population", "demo_magec": "deaths", "demo_fasec": "births"}
    for r in asyncio.run(raws(ed.EurostatDemographyConnector(countries=("FR",), include_europop=False),
                              FixtureHttp(ed.NAME, root=FIXTURES / "social"))):
        tables[demo[r.item.params["dataset"]]] = ed.dataset_tables(r)["FRA"]
    reqs = ra.MODEL.requirements(doc)
    inputs = ResolvedInputs.from_values(reqs, datasets={(n, "FRA"): (t, {}) for n, t in tables.items()})
    return ra.MODEL.compute(ra.MODEL.compile(inputs, doc), doc["fidelity"])


def q(engine: Engine, sql: str, **p: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings()]


def spec(env: Env, **over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0", "title": "Test: France 2023 reform as amended vs 2010 law",
        "baseline_release_id": str(env.release_id), "knowledge_cutoff": env.cutoff.isoformat(),
        "model_key": ra.MODEL_KEY, "model_version": ra.MODEL_VERSION, "countries": ["FRA"],
        "reference_year": 2024, "horizon": {"periods": 16, "unit": "calendar_year"},
        "intervention": {"kind": "retirement_age_rule_path", "scenario_path": "law_in_force_2026",
                         "reference_path": "law_2010_as_amended_2011"},
        "assumptions": {"observed_path": "law_in_force_2026"},
        "fidelity": "L1", "missing_input_policy": "fail", "outputs": list(ra.DEFAULT_OUTPUTS), "seed": 0,
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
                           {"p": json.dumps(payload), "k": f"pen-{sid}-{fidelity}-{utc_now().timestamp()}"}
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


def test_labour_pension_ingest_publishes_observations_and_dataset_inputs(env: Env) -> None:
    assert env.report["errors"] == [] and env.report["inserted"] > 0
    assert {o["status"] for o in env.report["outcomes"]} == {"published"}
    rows = q(env.owner, """SELECT concept_id, subject_key, dataset_key, parser_version, source_acquisition_ids
                           FROM evidence.dataset WHERE dataset_key LIKE 'eurostat.labour_pensions.%'""")
    got = {(r["concept_id"], r["subject_key"]) for r in rows}
    assert got == {(t.concept_id, iso) for t in lab.TABLES for iso in lab.EUROSTAT_GEO_TO_ISO3.values()}
    assert all(r["parser_version"] == lab.PARSER_VERSION and len(r["source_acquisition_ids"]) == 1 for r in rows)
    missing = q(env.owner, """SELECT count(*) AS n FROM evidence.observation o
                              JOIN evidence.connector_version v ON v.id = o.connector_version_id
                              WHERE v.name = :n AND o.value->>'kind' = 'missing'""", n=lab.CONNECTOR_NAME)
    assert missing[0]["n"] > 0  # suppressed cells stored as typed Missing, never 0


def test_retirement_scenario_runs_through_the_job_path_with_pinned_datasets(env: Env) -> None:
    pins = {(p["concept_id"], p["subject_key"]) for p in q(
        env.owner, "SELECT concept_id, subject_key FROM belief.release_dataset WHERE release_id=:r", r=env.release_id)}
    assert {(c, "FRA") for c in ra.CONCEPTS.values()} <= pins
    sid = create(env, spec(env))
    res = run(env, sid, "L1")
    assert res["status"] == "succeeded", res
    rows = q(env.owner, """SELECT output_key, variant, value, period_start, period_end, applicability
                           FROM sim.result WHERE run_id=:r""", r=res["run_id"])
    assert len(rows) == 16 * len(ra.DEFAULT_OUTPUTS)
    assert all(r["applicability"]["model_label"] == ra.EXPERIMENTAL_LABEL for r in rows)
    assert all(r["applicability"]["label"] == ra.LABEL for r in rows)
    chg = {r["period_start"].year: Decimal(r["value"]["value"]) for r in rows
           if r["output_key"] == "pension_recipients_change"}
    assert set(chg) == set(range(2025, 2041)) and all(v <= 0 for v in chg.values()) and chg[2035] < chg[2025]
    # same numbers as the pure plugin on tables derived directly from the fixture bytes
    pure = {(r.output_key, r.period[0].year): r.value["value"] for r in pure_rows(spec(env))}
    assert all(pure[(r["output_key"], r["period_start"].year)] == r["value"]["value"] for r in rows)


def test_retirement_l2_and_l0_cache(env: Env) -> None:
    sid = create(env, spec(env, fidelity="L2", outputs=["employment_change_55_69"]))
    res = run(env, sid, "L2")
    assert res["status"] == "succeeded", res
    rows = q(env.owner, "SELECT variant, value FROM sim.result WHERE run_id=:r", r=res["run_id"])
    assert any(r["variant"] == ra.SENSITIVITY_VARIANT and r["value"]["kind"] == "sensitivity_range" for r in rows)
    again = run(env, sid, "L0")
    assert again["status"] in ("succeeded", "cache_miss"), again


def test_country_without_rule_table_is_a_typed_capability_gap(env: Env) -> None:
    from reva.kernel.errors import CapabilityGap

    with pytest.raises(CapabilityGap):
        validate_scenario_spec(spec(env, countries=["ITA"]))
