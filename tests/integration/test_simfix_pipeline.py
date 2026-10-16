"""V3 SIMFIX: un.wpp + ilostat.labour_age5 + ilostat.social_protection -> dataset inputs -> belief release ->
``social.retirement_age`` 1.1.0 and 1.2.0 (pension coverage) through the job path.

Offline: excerpts of the real WPP 2024 / ILOSTAT bytes (SEN, JPN, BRA; SDG 1.3.1 retrieved 2026-10-16).
Own throwaway database (bootstrapped like a deployment), never the dev database.
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
from reva.adapters.connectors import ilostat_social_protection as SP
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
    root = tmp_path_factory.mktemp("simfix_objects")
    with throwaway_database(base, prefix="reva_test_simfix") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        assert settings.migration_url().database != "reva"
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        seed(engines["migration"], store)
        wpp_c = SF.wpp_connector()
        reports: dict[str, Any] = {}
        for key, conn, http, hook in (
            ("wpp", wpp_c, FixtureHttp(W.NAME), W.post_publish_for_connector(wpp_c)),
            ("ilo", IL.IlostatLabourConnector(), FixtureHttp(IL.NAME), IL.post_publish_datasets),
            ("sp", SP.IlostatSocialProtectionConnector(), FixtureHttp(SP.NAME), SP.post_publish_datasets),
        ):
            reports[key] = asyncio.run(
                run_connector(conn, http, store, engines["worker"], persist_cursor=False, post_publish=hook)
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


def retirement_doc(env: Env, version: str, **over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0",
        "title": f"SIMFIX: retirement age 62 from the 1965 cohort ({version})",
        "baseline_release_id": str(env.release_id),
        "knowledge_cutoff": env.cutoff.isoformat(),
        "model_key": "social.retirement_age",
        "model_version": version,
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
        "fidelity": "L1",
        "missing_input_policy": "fail",
        "outputs": ["employment_change_55_64", "pension_recipients_change_55_64"],
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
            {"p": json.dumps(payload), "k": f"simfix-{sid}-{fidelity}-{utc_now().timestamp()}"},
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


def values(env: Env, run_id: Any) -> dict[tuple[str, str, int], Decimal]:
    rows = q(
        env.owner,
        "SELECT output_key, value, applicability, period_start FROM sim.result WHERE run_id=:r AND variant='L1'",
        r=run_id,
    )
    return {
        (r["applicability"]["country"], r["output_key"], r["period_start"].year): Decimal(r["value"]["value"])
        for r in rows
    }


def test_coverage_ingest_registers_dataset_inputs(env: Env) -> None:
    sp = env.reports["sp"]
    assert sp["errors"] == [] and {o["status"] for o in sp["outcomes"]} == {"published"}
    rows = q(
        env.owner,
        """SELECT subject_key, dataset_key, source_acquisition_ids, parser_version, coverage FROM evidence.dataset
           WHERE concept_id = :c""",
        c=SP.CONCEPT_ID,
    )
    assert {"SEN", "BRA", "JPN", "IND", "MEX", "IDN", "XKX"} <= {r["subject_key"] for r in rows}
    assert {r["parser_version"] for r in rows} == {SP.PARSER_VERSION}
    assert all(len(r["source_acquisition_ids"]) == 1 for r in rows)  # the archived raw file
    bra = next(r for r in rows if r["subject_key"] == "BRA")
    assert bra["coverage"]["function"] == "SOC_CONTIG_PENSION" and bra["coverage"]["sexes"] == ["F", "M", "T"]
    obs = q(
        env.owner,
        """SELECT count(*) AS n FROM evidence.observation o JOIN evidence.connector_version v
           ON v.id = o.connector_version_id WHERE v.name = 'ilostat.social_protection'""",
    )
    assert obs[0]["n"] == 0  # dataset inputs only
    pins = q(
        env.owner,
        "SELECT subject_key FROM belief.release_dataset WHERE release_id=:r AND concept_id=:c",
        r=env.release_id,
        c=SP.CONCEPT_ID,
    )
    assert "SEN" in {p["subject_key"] for p in pins}


def test_retirement_1_2_0_scales_with_coverage_through_the_job_path(env: Env) -> None:
    r11 = run(env, create(env, retirement_doc(env, "1.1.0")), "L1")
    r12 = run(env, create(env, retirement_doc(env, "1.2.0")), "L1")
    assert r11["status"] == "succeeded" and r12["status"] == "succeeded", (r11, r12)
    v11, v12 = values(env, r11["run_id"]), values(env, r12["run_id"])
    for t in range(2023, 2033):
        assert v12[("JPN", "employment_change_55_64", t)] == v11[("JPN", "employment_change_55_64", t)]
        assert abs(
            v12[("SEN", "employment_change_55_64", t)] - Decimal("0.253") * v11[("SEN", "employment_change_55_64", t)]
        ) <= Decimal("0.002")
    assert 0 < v12[("SEN", "employment_change_55_64", 2030)] < v11[("SEN", "employment_change_55_64", 2030)]
    rows = q(env.owner, "SELECT applicability FROM sim.result WHERE run_id=:r", r=r12["run_id"])
    assert {r["applicability"]["model"] for r in rows} == {"social.retirement_age@1.2.0"}
    sen = next(r["applicability"] for r in rows if r["applicability"]["country"] == "SEN")
    assert sen["pension_coverage"]["year"] == 2022 and sen["pension_coverage"]["total_percent"] == "25.3"
    [outcome] = q(env.owner, "SELECT applicability FROM sim.run_outcome WHERE run_id=:r", r=r12["run_id"])
    assert outcome["applicability"]["countries"]["BRA"]["pension_coverage"]["breakdown"] == "by_sex"
    old = q(env.owner, "SELECT applicability FROM sim.result WHERE run_id=:r LIMIT 1", r=r11["run_id"])
    assert "pension_coverage" not in old[0]["applicability"]  # 1.1.0 unchanged


def test_retirement_1_2_0_refuses_countries_without_coverage(env: Env) -> None:
    # GHA: WBL table, but the release pins no WPP/ILOSTAT/coverage dataset -> missing_input incl. coverage
    ref = run(env, create(env, retirement_doc(env, "1.2.0", countries=["SEN", "GHA"])), "L1")
    assert ref["status"] == "missing_input"
    gha = [p for p in ref["problems"] if p["country"] == "GHA"]
    assert any(p.get("concept") == SP.CONCEPT_ID for p in gha) and {p["country"] for p in ref["problems"]} == {"GHA"}
    ok = run(
        env,
        create(env, retirement_doc(env, "1.2.0", countries=["ALL"], missing_input_policy="skip_missing_countries")),
        "L1",
    )
    assert ok["status"] == "succeeded", ok
    got = q(env.owner, "SELECT DISTINCT applicability->>'country' AS c FROM sim.result WHERE run_id=:r", r=ok["run_id"])
    assert {r["c"] for r in got} == set(SF.COUNTRIES)  # IND/MEX/IDN have coverage only: listed as skipped
    [outcome] = q(env.owner, "SELECT applicability FROM sim.run_outcome WHERE run_id=:r", r=ok["run_id"])
    assert {"IND", "MEX", "IDN"} <= set(outcome["applicability"]["skipped_countries"])
