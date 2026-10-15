"""IO price pipeline end to end on a SYNTHETIC 3-economy x 2-industry table (offline).

connector (FixtureHttp, raw bytes archived) -> dataset build with identity checks ->
register_dataset -> belief release pins the dataset -> scenario -> simulation job (L1, L2,
L0) -> hand-computed values. A table that violates an identity is refused and nothing is
registered. Runs in its own throwaway database (bootstrapped like a deployment).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

from reva.adapters.connectors import io_tables as it
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres import repositories as repo
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, _fixture_fingerprint, run_connector
from reva.kernel.context import Origin
from reva.kernel.ids import sha256_bytes
from reva.kernel.time import utc_now
from reva.models.economic import io_price as m
from reva.operations.context import JobContext
from reva.settings import Settings
from reva.simulation.jobs import handle_simulation
from reva.simulation.spec import CONTRACTS, validate_scenario_spec

pytestmark = pytest.mark.db

_spec = importlib.util.spec_from_file_location(
    "io_synthetic", Path(__file__).resolve().parents[1] / "fixtures" / "io_tables" / "synthetic.py")
assert _spec and _spec.loader
syn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(syn)
DIMS = it.TableDims(countries=syn.COUNTRIES, industries=syn.INDUSTRIES)
BAD_YEAR = 2023


def write_fixtures(root: Path) -> None:
    """Captured-response layout for FixtureHttp: 2024 is balanced, 2023 has one corrupted cell."""
    d = root / it.NAME
    d.mkdir(parents=True)
    entries = []
    conn = it.FigaroConnector(dims=DIMS)
    bodies: dict[str, bytes] = {it.constraint_url("NAIO_10_FCP_II4"): syn.constraint_xml()}
    for year, over in ((syn.YEAR, None), (BAD_YEAR, {("FR", "B", "DE", "B"): "1.000"})):
        for item in conn._data_items(year, "NAIO_10_FCP_II4"):
            bodies[item.url] = syn.body(tuple(item.params["origins"]), overrides=over, year=year)
    for url, body in bodies.items():
        fp = _fixture_fingerprint(url)
        (d / f"{fp[:16]}.bin").write_bytes(body)
        entries.append({"request_fingerprint": fp, "url": url, "file": f"{fp[:16]}.bin", "sha256": sha256_bytes(body),
                        "status": 200, "headers": {"content-type": "application/vnd.sdmx.data+csv"},
                        "retrieved_at": "2026-10-15T00:00:00+00:00", "licence": it.LICENCE})
    (d / "manifest.json").write_text(json.dumps(entries))


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
    published: dict[str, Any]
    refused: Exception


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import register_connector_version, seed

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    try:
        with create_engine(base.migration_url().set(database="postgres")).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable")
    root = tmp_path_factory.mktemp("io_objects")
    fixtures = tmp_path_factory.mktemp("io_fixtures")
    write_fixtures(fixtures)
    with throwaway_database(base, prefix="reva_test_io") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        eng = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        seed(eng["migration"], store)
        with eng["migration"].begin() as conn:  # the built-in registry entry is requested from the lead
            act = repo.simple_activity("registry.seed", "review", "maintainer:registry-seed", {"test": it.NAME})
            repo.record_activity(conn, act)
            register_connector_version(conn, store, it.FigaroConnector(dims=DIMS).describe(), act.id)

        async def ingest(year: int) -> Any:
            c = it.FigaroConnector(year, dims=DIMS)
            return await run_connector(c, FixtureHttp(it.NAME, root=fixtures), store, eng["worker"],
                                       persist_cursor=False)

        # a release needs at least one eligible observation (V1 rule): ingest the captured WDI fixtures
        from reva.adapters.connectors.registry import builtin_connectors

        wb = builtin_connectors()["worldbank.wdi.indicators"]
        asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), store, eng["worker"], persist_cursor=False))
        for year in (syn.YEAR, BAD_YEAR):
            rep = asyncio.run(ingest(year))
            assert not rep.errors and all(o.status in ("published", "unchanged") for o in rep.outcomes), rep.as_dict()
            assert rep.inserted == 0  # a dataset input, never scalar observations
        published = it.publish_io_dataset(eng["worker"], store, year=syn.YEAR, origin="test_fixture", dims=DIMS)
        try:
            it.publish_io_dataset(eng["worker"], store, year=BAD_YEAR, origin="test_fixture", dims=DIMS)
            refused: Exception = AssertionError("not refused")
        except it.IdentityCheckFailed as e:
            refused = e
        rel = publish_release(eng["worker"], store, cutoff=utc_now(), origin=Origin.test_fixture,
                              environment="test")
        try:
            yield Env(settings, eng["migration"], eng["api"], eng["worker"], eng["sim"], store, rel.release_id,
                      rel.knowledge_cutoff, published, refused)
        finally:
            for e in eng.values():
                e.dispose()


def q(engine: Engine, sql: str, **p: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings()]


def scenario_doc(env: Env, **over: Any) -> dict[str, Any]:
    doc = json.loads((CONTRACTS / "scenario.example.json").read_text())
    doc.update(schema_version="2.0.0", title="Test: synthetic IO cost push", model_key=m.MODEL_KEY,
               model_version=m.MODEL_VERSION, baseline_release_id=str(env.release_id),
               knowledge_cutoff=env.cutoff.isoformat(), countries=["FRA", "DEU"], reference_year=2024,
               horizon={"periods": 1, "unit": "annual_accounting_period"},
               intervention={"kind": "set_exogenous_industry_price_change",
                             "shocks": [{"origin": "DEU", "industry": "B", "price_change_percent": "10"}]},
               assumptions={"pass_through_grid": ["0.5", "0.75", "1"]}, outputs=list(m.OUTPUT_GROUPS),
               missing_input_policy="fail", fidelity="L1")
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
                           {"p": json.dumps(payload), "k": f"io-{sid}-{fidelity}-{utc_now().timestamp()}"}
                           ).scalar_one()
        token = conn.execute(text("""UPDATE ops.job SET status='running', worker_id='t', attempts=attempts+1,
                                     lease_until=now()+interval '10 minutes', fencing_token=nextval('ops.fencing_seq')
                                     WHERE id=:j RETURNING fencing_token"""), {"j": jid}).scalar_one()
    ctx = JobContext(job_id=jid, job_type="simulation", worker_id="t", fencing_token=int(token), attempt=1,
                     settings=env.settings, worker_engine=env.worker, sim_engine=env.sim, store=env.store,
                     heartbeat=lambda _p=None: "running")
    return handle_simulation(payload, ctx)


def results(env: Env, run_id: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = q(env.owner, """SELECT r.output_key, e.id AS subject_id, r.variant, r.value, r.uncertainty, r.applicability
                           FROM sim.result r JOIN world.entity e ON e.id = r.subject_id WHERE r.run_id = :r""",
             r=run_id)
    return {(r["output_key"], r["applicability"]["country"], r["variant"]): r for r in rows}


def test_dataset_registered_with_identity_report_and_raw_provenance(env: Env) -> None:
    pub = env.published
    assert pub["created"] and pub["identity_report"]["passed"] and pub["acquisitions"] == 3
    [ds] = q(env.owner, "SELECT * FROM evidence.dataset WHERE dataset_key = 'figaro.iciot_ii.2024'")
    assert ds["concept_id"] == m.DATASET_CONCEPT and ds["subject_key"] == "WORLD" and ds["origin"] == "test_fixture"
    assert ds["coverage"]["identity_report"]["passed"] is True and ds["coverage"]["reference_year"] == 2024
    assert ds["row_count"] == DIMS.dense_cells() + 6
    raw = {r["raw_sha256"] for r in q(env.owner, "SELECT raw_sha256 FROM evidence.acquisition WHERE id = ANY(:i)",
                                       i=list(ds["source_acquisition_ids"]))}
    assert raw == {a for a in json.loads(env.store.get("normalised", ds["manifest_sha256"]))["source_raw_sha256"]}
    pins = q(env.owner, "SELECT dataset_id FROM belief.release_dataset WHERE release_id = :r", r=env.release_id)
    assert [p["dataset_id"] for p in pins] == [ds["id"]]


def test_identity_failure_refuses_registration(env: Env) -> None:
    assert isinstance(env.refused, it.IdentityCheckFailed)
    assert env.refused.details["report"]["passed"] is False
    assert q(env.owner, "SELECT count(*) AS n FROM evidence.dataset WHERE dataset_key = 'figaro.iciot_ii.2023'"
             )[0]["n"] == 0


def test_l1_scenario_end_to_end_matches_hand_computation(env: Env) -> None:
    sid = create(env, scenario_doc(env))
    ref = run(env, sid, "L1")
    assert ref["status"] == "succeeded", ref
    r = results(env, ref["run_id"])
    assert r[("industry_price_change_percent:C10-12", "FRA", "L1")]["value"]["value"] == "2.061856"
    assert r[("industry_price_change_percent:C10-12", "DEU", "L1")]["value"]["value"] == "0.618557"
    assert r[(m.BASKET_KEY, "FRA", "L1")]["value"]["value"] == "3.216495"
    assert r[(m.BASKET_KEY, "DEU", "L1")]["value"]["kind"] == "missing"
    assert r[(m.BASKET_KEY, "FRA", "L1")]["applicability"]["label"] == m.LABEL
    [outcome] = q(env.owner, "SELECT applicability FROM sim.run_outcome WHERE run_id = :r", r=ref["run_id"])
    app = outcome["applicability"]
    assert app["model_label"] == "experimental model, not yet reviewed"
    assert app["input_provenance"]["datasets"][0]["dataset_id"] == env.published["dataset_id"]
    assert app["diagnostics"]["max_relative_residual"]["1"] < 1e-10
    [run_row] = q(env.owner, "SELECT model_manifest_sha256 FROM sim.run WHERE id = :r", r=ref["run_id"])
    mm = json.loads(env.store.get("scenarios", run_row["model_manifest_sha256"]))
    assert mm["models"][0]["review_status"] == "maintainer_review_pending" and mm["models"][0]["experimental"]
    l0 = run(env, sid, "L0")
    assert l0["status"] == "succeeded" and l0["cache_hit_of"] == ref["run_id"]


def test_l2_publishes_grid_and_level_free_sensitivity_range(env: Env) -> None:
    ref = run(env, create(env, scenario_doc(env, fidelity="L2")), "L2")
    assert ref["status"] == "succeeded", ref
    r = results(env, ref["run_id"])
    key = ("industry_price_change_percent:C10-12", "FRA")
    assert r[(*key, "L2:pass_through=0.5")]["value"]["value"] == "1.007557"
    rng = r[(*key, m.SENSITIVITY_VARIANT)]
    assert (rng["value"]["lower"], rng["value"]["upper"]) == ("1.007557", "2.061856")
    assert rng["uncertainty"]["kind"] == "sensitivity_range" and rng["uncertainty"].get("level") is None


def test_wrong_year_and_uncovered_country_are_typed(env: Env) -> None:
    ref = run(env, create(env, scenario_doc(env, reference_year=2023)), "L1")
    assert ref["status"] == "unsupported" and ref["problems"][0]["code"] == "incompatible_semantics"
    from reva.kernel.errors import ValidationProblem

    with pytest.raises(ValidationProblem, match="ScenarioSpec"):  # DZA is not a FIGARO economy (schema enum)
        create(env, scenario_doc(env, countries=["DZA"]))
