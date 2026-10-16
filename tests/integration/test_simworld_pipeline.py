"""V3 SIMWORLD: un.wpp + ilostat.labour_age5 -> dataset inputs -> belief release -> the 1.1.0 models
(cohort-component projection and retirement age) for non-EU countries through the job path.

Offline: excerpts of the real WPP 2024 / ILOSTAT bytes (SEN, JPN, BRA), fetched in byte-range parts by
the connector from the fixture bank. Own throwaway database (bootstrapped like a deployment).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "simworld"))

import simworld_fixtures as SF

from reva.adapters.connectors import ilostat_labour as IL
from reva.adapters.connectors import un_wpp_demography as W
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import FixtureHttp, run_connector
from reva.kernel.context import Origin
from reva.kernel.time import utc_now
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
    reports: dict[str, Any]


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
    root = tmp_path_factory.mktemp("simworld_objects")
    with throwaway_database(base, prefix="reva_test_simworld") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        seed(engines["migration"], store)
        wpp_c = SF.wpp_connector()
        reports = {
            "wpp": asyncio.run(
                run_connector(
                    wpp_c,
                    FixtureHttp(W.NAME),
                    store,
                    engines["worker"],
                    persist_cursor=False,
                    post_publish=W.post_publish_for_connector(wpp_c),
                )
            ).as_dict()
        }
        ilo_c = IL.IlostatLabourConnector()
        reports["ilo"] = asyncio.run(
            run_connector(
                ilo_c,
                FixtureHttp(IL.NAME),
                store,
                engines["worker"],
                persist_cursor=False,
                post_publish=IL.post_publish_datasets,
            )
        ).as_dict()
        wb = builtin_connectors()["worldbank.wdi.indicators"]  # a release needs one selected estimate
        asyncio.run(run_connector(wb, FixtureHttp(wb.describe().name), store, engines["worker"], persist_cursor=False))
        rel = publish_release(
            engines["worker"], store, cutoff=utc_now(), origin=Origin.test_fixture, environment="test"
        )
        try:
            yield Env(
                settings,
                engines["migration"],
                engines["api"],
                engines["worker"],
                engines["sim"],
                store,
                rel.release_id,
                rel.knowledge_cutoff,
                reports,
            )
        finally:
            for e in engines.values():
                e.dispose()


def q(engine: Engine, sql: str, **p: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings()]


def cohort_doc(env: Env, **over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0",
        "title": "SIMWORLD: cohort-component projection on WPP 2024",
        "baseline_release_id": str(env.release_id),
        "knowledge_cutoff": env.cutoff.isoformat(),
        "model_key": "demography.cohort_component",
        "model_version": "1.1.0",
        "countries": ["SEN", "BRA", "JPN"],
        "reference_year": 2023,
        "horizon": {"periods": 12, "unit": "calendar_year"},
        "intervention": {"kind": "baseline_projection"},
        "assumptions": {
            "data_source": "un_wpp2024",
            "mortality": "constant_base_period_rates",
            "fertility": "constant_base_period_rates",
            "migration": "average_residual",
            "base_period": {"first_year": 2018, "last_year": 2022},
        },
        "fidelity": "L1",
        "missing_input_policy": "fail",
        "outputs": ["population.total.all_ages", "old_age_dependency_ratio_65plus_per_15_64", "net_migration.total"],
        "seed": 0,
    }
    doc.update(over)
    return doc


def retirement_doc(env: Env, **over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0",
        "title": "SIMWORLD: retirement age 62 from the 1965 cohort",
        "baseline_release_id": str(env.release_id),
        "knowledge_cutoff": env.cutoff.isoformat(),
        "model_key": "social.retirement_age",
        "model_version": "1.1.0",
        "countries": ["SEN", "BRA", "JPN"],
        "reference_year": 2022,
        "horizon": {"periods": 10, "unit": "calendar_year"},
        "intervention": {
            "kind": "retirement_age_target",
            "reference_path": "wbl2026_held_constant",
            "target_age_months": 744,
            "first_birth_year": 1965,
            "phase_in_months_per_cohort": 0,
        },
        "assumptions": {"employment_response": {"central": "rabate_rochut_2020_fra"}},
        "fidelity": "L2",
        "missing_input_policy": "fail",
        "outputs": [
            "employed_persons_55_64",
            "employment_change_55_64",
            "pension_recipients_change_55_64",
            "old_age_dependency_ratio",
        ],
        "seed": 0,
    }
    doc.update(over)
    return doc


def create(env: Env, doc: dict[str, Any]) -> UUID:
    s = validate_scenario_spec(doc)
    with env.api.begin() as conn:
        return UUID(
            str(
                conn.execute(
                    text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :b, :d, 'human:test')"),
                    {"spec": json.dumps(s.as_json()), "b": s.canonical_bytes(), "d": s.digest()},
                ).scalar_one()
            )
        )


def run(env: Env, sid: UUID, fidelity: str) -> dict[str, Any]:
    payload = {"scenario_id": str(sid), "fidelity": fidelity}
    with env.owner.begin() as conn:
        jid = conn.execute(
            text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                    VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
            {"p": json.dumps(payload), "k": f"simworld-{sid}-{fidelity}-{utc_now().timestamp()}"},
        ).scalar_one()
        token = conn.execute(
            text("""UPDATE ops.job SET status='running', worker_id='t', attempts=attempts+1,
                    lease_until=now()+interval '10 minutes', fencing_token=nextval('ops.fencing_seq')
                    WHERE id=:j RETURNING fencing_token"""),
            {"j": jid},
        ).scalar_one()

    def hb(_p: Any = None) -> str:
        with env.owner.connect() as c:
            return str(c.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": jid}).scalar_one())

    ctx = JobContext(
        job_id=jid,
        job_type="simulation",
        worker_id="t",
        fencing_token=int(token),
        attempt=1,
        settings=env.settings,
        worker_engine=env.worker,
        sim_engine=env.sim,
        store=env.store,
        heartbeat=hb,
    )
    return handle_simulation(payload, ctx)


def test_ingest_archives_parts_and_registers_dataset_inputs(env: Env) -> None:
    wpp = env.reports["wpp"]
    assert wpp["errors"] == [] and {o["status"] for o in wpp["outcomes"]} == {"published"}
    assert len(wpp["outcomes"]) == len(SF.wpp_connector().all_items()) > 4  # several byte-range parts
    rows = q(
        env.owner,
        "SELECT concept_id, subject_key, dataset_key, source_acquisition_ids, parser_version FROM evidence.dataset",
    )
    got = {(r["concept_id"], r["subject_key"]) for r in rows}
    for d in W.DATASETS.values():
        assert {(d.concept_id, c) for c in SF.COUNTRIES} <= got
    for i in IL.INDICATORS:
        assert {(i.concept_id, c) for c in (*SF.COUNTRIES, "XKX")} <= got
    pop = next(r for r in rows if r["concept_id"] == "demography.wpp.population.jan1_age_sex")
    parts = [i for i in SF.wpp_connector().all_items() if i.params["file_key"] == "pop"]
    assert len(pop["source_acquisition_ids"]) == len(parts) and pop["parser_version"] == W.PARSER_VERSION
    births = next(r for r in rows if r["concept_id"] == "demography.wpp.live_births.mother_age5_newborn_sex")
    n_parts = len([i for i in SF.wpp_connector().all_items() if i.params["file_key"] in ("fert5", "indicators")])
    assert len(births["source_acquisition_ids"]) == n_parts  # two files
    obs = q(
        env.owner,
        """SELECT count(*) AS n FROM evidence.observation o JOIN evidence.connector_version v
           ON v.id = o.connector_version_id WHERE v.name IN ('un.wpp', 'ilostat.labour_age5')""",
    )
    assert obs[0]["n"] == 0  # dataset inputs only, never scalar observations
    pins = q(
        env.owner, "SELECT concept_id, subject_key FROM belief.release_dataset WHERE release_id=:r", r=env.release_id
    )
    assert ("demography.wpp.population.jan1_age_sex", "SEN") in {(p["concept_id"], p["subject_key"]) for p in pins}


def test_cohort_1_1_0_runs_for_non_eu_countries(env: Env) -> None:
    ref = run(env, create(env, cohort_doc(env)), "L1")
    assert ref["status"] == "succeeded", ref
    rows = q(
        env.owner,
        "SELECT output_key, value, applicability, period_start FROM sim.result WHERE run_id=:r",
        r=ref["run_id"],
    )
    pop = [r for r in rows if r["output_key"] == "population.total.all_ages"]
    assert {r["applicability"]["country"] for r in pop} == set(SF.COUNTRIES) and len(pop) == 3 * 13
    assert all(r["applicability"]["model"] == "demography.cohort_component@1.1.0" for r in rows)
    assert all(r["applicability"]["data_source"] == "un_wpp2024" for r in rows)
    [outcome] = q(env.owner, "SELECT applicability FROM sim.run_outcome WHERE run_id=:r", r=ref["run_id"])
    app = outcome["applicability"]
    assert app["model_label"] == "experimental model, not yet reviewed" and app["data_source"] == "un_wpp2024"
    assert app["countries"]["SEN"]["wpp_net_migration_check"]["years"]
    hit = run(env, create(env, cohort_doc(env)), "L0")
    assert hit["status"] == "succeeded"


def test_cohort_all_countries_resolves_from_pinned_datasets(env: Env) -> None:
    ref = run(env, create(env, cohort_doc(env, countries=["ALL"], missing_input_policy="skip_missing_countries")), "L1")
    assert ref["status"] == "succeeded", ref
    rows = q(
        env.owner, "SELECT DISTINCT applicability->>'country' AS c FROM sim.result WHERE run_id=:r", r=ref["run_id"]
    )
    assert {r["c"] for r in rows} == set(SF.COUNTRIES)


def test_retirement_1_1_0_runs_for_non_eu_countries(env: Env) -> None:
    ref = run(env, create(env, retirement_doc(env)), "L2")
    assert ref["status"] == "succeeded", ref
    rows = q(
        env.owner,
        "SELECT output_key, variant, value, applicability, period_start FROM sim.result WHERE run_id=:r",
        r=ref["run_id"],
    )
    l2 = {
        (r["applicability"]["country"], r["period_start"].year): Decimal(r["value"]["value"])
        for r in rows
        if r["output_key"] == "employment_change_55_64" and r["variant"] == "L2:rabate_rochut_2020_fra"
    }
    assert l2[("SEN", 2030)] > 0 > l2[("JPN", 2030)]
    rng = [r for r in rows if r["variant"] == "L2:sensitivity_range"]
    assert rng and all(r["value"]["kind"] == "sensitivity_range" for r in rng)
    assert all(r["applicability"]["model"] == "social.retirement_age@1.1.0" for r in rows)


def test_retirement_refusals_are_typed(env: Env) -> None:
    # GHA has a WBL 2026 table but the release pins no WPP/ILOSTAT dataset for it: missing_input, listed
    ref = run(env, create(env, retirement_doc(env, countries=["SEN", "GHA"], fidelity="L1")), "L1")
    assert ref["status"] == "missing_input"
    assert {p["country"] for p in ref["problems"]} == {"GHA"}
    from reva.kernel.errors import CapabilityGap

    with pytest.raises(CapabilityGap):  # USA: WBL age 66.8 is not a whole number of months
        validate_scenario_spec(retirement_doc(env, countries=["USA"]))
