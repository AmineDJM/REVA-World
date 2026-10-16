"""LIVE (V3 SIMWORLD): WPP 2024 + ILOSTAT ingest -> release -> cohort projections for every country and
retirement-age scenarios for every country with complete inputs, plus the 2013 -> 2023 back-test of
every country against the pinned WPP datasets, in a THROWAWAY database. Deselected by default (``-m live``).

Egress is reserved and metered against ``registry/policies/budgets.yaml`` (sources ``un.wpp``,
``ilostat.rplumber``, ``worldbank.wdi``) through the throwaway database's budget ledger, as the ingest
job does. ``REVA_SIMWORLD_LIVE_OUT`` (optional): directory for the run summaries and the back-test
table (JSON); ``REVA_SIMWORLD_LIVE_OBJECTS`` (optional): object-store root (default: pytest tmp).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from reva.adapters.connectors import ilostat_labour as IL
from reva.adapters.connectors import un_wpp_demography as W
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.evidence.runner import make_live_client, run_connector
from reva.kernel.context import Origin
from reva.kernel.errors import RevaError
from reva.kernel.time import utc_now
from reva.models.population import cohort as C
from reva.models.population import wpp
from reva.operations.budgets import budget_source_key, metered, reserve_ingest, settle_metered
from reva.operations.context import JobContext
from reva.ports.connectors import FetchBudget
from reva.settings import Settings
from reva.simulation.jobs import handle_simulation
from reva.simulation.plugins import InputRequirement
from reva.simulation.spec import validate_scenario_spec

pytestmark = [pytest.mark.live, pytest.mark.db]


def _ingest(
    connector: Any, engines: dict[str, Any], store: Any, settings: Settings, post_publish: Any, extra: int = 0
) -> dict[str, Any]:
    spec = connector.describe()
    planned = extra + (
        len(connector.all_items())
        if hasattr(connector, "all_items")
        else len(connector.plan(None, FetchBudget()).items)
    )
    reservation = reserve_ingest(
        engines["worker"], job_id=uuid.uuid4(), attempt=1, source_key=budget_source_key(spec), planned_requests=planned
    )
    http = metered(make_live_client(spec, settings), engines["worker"], reservation)

    async def go() -> Any:
        try:
            return await run_connector(
                connector,
                http,
                store,
                engines["worker"],
                persist_cursor=False,
                budget=FetchBudget(max_requests=planned),
                post_publish=post_publish,
            )
        finally:
            await http.aclose()

    t0 = time.perf_counter()
    report = asyncio.run(go())
    budget = settle_metered(engines["worker"], http)
    return {
        "report": report.as_dict(),
        "budget": budget,
        "seconds": round(time.perf_counter() - t0, 1),
        "planned": planned,
    }


def _job(engines: dict[str, Any], settings: Settings, store: Any, sid: uuid.UUID, fidelity: str) -> dict[str, Any]:
    payload = {"scenario_id": str(sid), "fidelity": fidelity}
    with engines["migration"].begin() as conn:
        jid = conn.execute(
            text("""INSERT INTO ops.job(job_type, payload, idempotency_key, created_by)
                    VALUES ('simulation', CAST(:p AS jsonb), :k, 'human:test') RETURNING id"""),
            {"p": json.dumps(payload), "k": f"simworld-live-{sid}-{fidelity}"},
        ).scalar_one()
        token = conn.execute(
            text("""UPDATE ops.job SET status='running', worker_id='t', attempts=attempts+1,
                    lease_until=now()+interval '60 minutes', fencing_token=nextval('ops.fencing_seq')
                    WHERE id=:j RETURNING fencing_token"""),
            {"j": jid},
        ).scalar_one()

    def hb(_p: Any = None) -> str:
        with engines["migration"].connect() as c:
            return str(c.execute(text("SELECT status FROM ops.job WHERE id=:j"), {"j": jid}).scalar_one())

    ctx = JobContext(
        job_id=jid,
        job_type="simulation",
        worker_id="t",
        fencing_token=int(token),
        attempt=1,
        settings=settings,
        worker_engine=engines["worker"],
        sim_engine=engines["sim"],
        store=store,
        heartbeat=hb,
    )
    t0 = time.perf_counter()
    out = handle_simulation(payload, ctx)
    out["seconds"] = round(time.perf_counter() - t0, 1)
    return out


def _create(engines: dict[str, Any], doc: dict[str, Any]) -> uuid.UUID:
    s = validate_scenario_spec(doc)
    with engines["api"].begin() as conn:
        return uuid.UUID(
            str(
                conn.execute(
                    text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :b, :d, 'human:test')"),
                    {"spec": json.dumps(s.as_json()), "b": s.canonical_bytes(), "d": s.digest()},
                ).scalar_one()
            )
        )


def _summary(engines: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    with engines["migration"].connect() as c:
        app = c.execute(
            text("SELECT applicability FROM sim.run_outcome WHERE run_id=:r"), {"r": ref["run_id"]}
        ).scalar_one_or_none()
        n = c.execute(text("SELECT count(*) FROM sim.result WHERE run_id=:r"), {"r": ref["run_id"]}).scalar_one()
    countries = (app or {}).get("countries") or {}
    by_status: dict[str, list[str]] = {}
    reasons: dict[str, Any] = {}
    for iso, info in sorted(countries.items()):
        by_status.setdefault(info.get("status", "?"), []).append(iso)
        if info.get("status") != "ready":
            p = (info.get("problems") or [{}])[0]
            reasons[iso] = {
                "code": p.get("code"),
                "capability": (p.get("details") or {}).get("capability") or p.get("capability"),
                "detail": (p.get("detail") or p.get("reason") or p.get("input") or "")[:220],
            }
    return {
        "status": ref.get("status"),
        "run_id": str(ref.get("run_id")),
        "seconds": ref.get("seconds"),
        "result_rows": n,
        "countries_by_status": {k: len(v) for k, v in by_status.items()},
        "ready": by_status.get("ready", []),
        "not_ready": reasons,
        "selection": (app or {}).get("country_selection"),
    }


def test_live_world_ingest_projections_backtest_and_retirement(tmp_path: Path) -> None:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import seed
    from reva.simulation.datasets import resolve_dataset

    out_dir = Path(os.environ["REVA_SIMWORLD_LIVE_OUT"]) if os.environ.get("REVA_SIMWORLD_LIVE_OUT") else None
    root = Path(os.environ.get("REVA_SIMWORLD_LIVE_OBJECTS") or (tmp_path / "objects"))
    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    with create_engine(base.migration_url().set(database="postgres")).connect() as c:
        c.execute(text("SELECT 1"))
    stats: dict[str, Any] = {"started": utc_now().isoformat()}

    def dump() -> None:  # partial results survive a failure later in the run
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "live_stats.json").write_text(json.dumps(stats, indent=1, default=str))

    def job(key: str, doc: dict[str, Any], fidelity: str) -> dict[str, Any] | None:
        try:
            ref = _job(engines, settings, store, _create(engines, doc), fidelity)
        except Exception as e:  # recorded, the remaining runs still go ahead
            stats[key] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:500]}"}
            dump()
            return None
        stats[key] = _summary(engines, ref)
        dump()
        return ref

    with throwaway_database(base, prefix="reva_live_simworld") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        stats["database"] = settings.migration_url().database
        assert stats["database"] != "reva"
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        try:
            seed(engines["migration"], store)
            stats["ingest_wpp"] = _ingest(
                W.UnWppDemographyConnector(), engines, store, settings, W.post_publish_datasets
            )
            assert stats["ingest_wpp"]["report"]["errors"] == [], stats["ingest_wpp"]["report"]["errors"]
            stats["ingest_ilostat"] = _ingest(
                IL.IlostatLabourConnector(), engines, store, settings, IL.post_publish_datasets
            )
            assert stats["ingest_ilostat"]["report"]["errors"] == []
            wb = builtin_connectors()["worldbank.wdi.indicators"]  # a release needs one selected estimate (live)
            stats["ingest_wb"] = _ingest(wb, engines, store, settings, None, extra=10)  # paging follow-ups
            with engines["migration"].connect() as c:
                stats["datasets"] = {
                    r[0]: r[1]
                    for r in c.execute(
                        text("SELECT concept_id, count(*) FROM evidence.dataset GROUP BY concept_id")
                    ).all()
                }
            t0 = time.perf_counter()
            rel = publish_release(engines["worker"], store, cutoff=utc_now(), origin=Origin.live, environment="test")
            stats["release"] = {"id": str(rel.release_id), "seconds": round(time.perf_counter() - t0, 1)}
            dump()

            common = {
                "schema_version": "2.0.0",
                "baseline_release_id": str(rel.release_id),
                "knowledge_cutoff": rel.knowledge_cutoff.isoformat(),
                "seed": 0,
                "missing_input_policy": "skip_missing_countries",
            }
            cohort = {
                **common,
                "title": "Live SIMWORLD: cohort-component projection 2023-2050, every WPP country",
                "model_key": "demography.cohort_component",
                "model_version": "1.1.0",
                "countries": ["ALL"],
                "reference_year": 2023,
                "horizon": {"periods": 27, "unit": "calendar_year"},
                "intervention": {"kind": "baseline_projection"},
                "assumptions": {
                    "data_source": "un_wpp2024",
                    "mortality": "constant_base_period_rates",
                    "fertility": "constant_base_period_rates",
                    "migration": "average_residual",
                    "base_period": {"first_year": 2018, "last_year": 2022},
                },
                "fidelity": "L1",
                # two outputs: every country x 28 years x 2 rows stays under the 32 MiB result manifest
                "outputs": ["population.total.all_ages", "old_age_dependency_ratio_65plus_per_15_64"],
            }
            ref = job("cohort_all", cohort, "L1")
            assert ref is not None, stats["cohort_all"]
            with engines["migration"].connect() as c:
                rows = c.execute(
                    text("""SELECT applicability->>'country' AS c, output_key, period_start, value->>'value' AS v
                            FROM sim.result WHERE run_id=:r AND period_start IN ('2023-01-01', '2050-01-01')"""),
                    {"r": ref["run_id"]},
                ).all()
            stats["cohort_all"]["values_2023_2050"] = {}
            for iso, key, p, v in rows:
                stats["cohort_all"]["values_2023_2050"].setdefault(iso, {})[f"{key}@{p.year}"] = v

            retire = {
                **common,
                "title": "Live SIMWORLD: retirement age +2 years from the 1965 cohort",
                "model_key": "social.retirement_age",
                "model_version": "1.1.0",
                "countries": ["ALL"],
                "reference_year": 2022,
                "horizon": {"periods": 13, "unit": "calendar_year"},
                "intervention": {
                    "kind": "retirement_age_shift",
                    "reference_path": "wbl2026_held_constant",
                    "shift_months": 24,
                    "first_birth_year": 1965,
                },
                "assumptions": {"employment_response": {"central": "rabate_rochut_2020_fra"}},
                "fidelity": "L1",  # every country at L1 (L2 x every country exceeds the 32 MiB result manifest)
                "outputs": ["employment_change_55_64", "pension_recipients_change_55_64"],
            }
            job("retirement_shift_plus_24m", retire, "L1")
            l2 = {
                **retire,
                "title": "Live SIMWORLD: retirement age +2 years, L2 grid, five countries",
                "countries": ["SEN", "BRA", "JPN", "IND", "MEX"],
                "fidelity": "L2",
                "outputs": ["employed_persons_55_64", "employment_change_55_64", "pension_recipients_change_55_64"],
            }
            ref = job("retirement_shift_l2_five", l2, "L2")
            if ref is not None:
                with engines["migration"].connect() as c:
                    rows = c.execute(
                        text("""SELECT applicability->>'country' AS c, variant, value FROM sim.result
                                WHERE run_id=:r AND period_start = '2032-01-01'
                                  AND output_key = 'employment_change_55_64'"""),
                        {"r": ref["run_id"]},
                    ).all()
                stats["retirement_shift_l2_five"]["employment_change_2032"] = {
                    f"{iso}|{var}": (v.get("value") or [v.get("lower"), v.get("upper")]) for iso, var, v in rows
                }
                dump()
            target = {**retire, "title": "Live SIMWORLD: retirement age 62 from the 1965 cohort", "fidelity": "L1"}
            target["intervention"] = {
                "kind": "retirement_age_target",
                "reference_path": "wbl2026_held_constant",
                "target_age_months": 744,
                "first_birth_year": 1965,
            }
            ref = job("retirement_target_62", target, "L1")
            with engines["migration"].connect() as c:
                rows = c.execute(
                    text("""SELECT applicability->>'country' AS c, output_key, value->>'value' AS v FROM sim.result
                            WHERE run_id=:r AND period_start = '2030-01-01'
                              AND output_key IN ('employment_change_55_64', 'pension_recipients_change_55_64')"""),
                    {"r": ref["run_id"]},
                ).all()
            stats["retirement_target_62"]["values_2030"] = {}
            for iso, key, v in rows:
                stats["retirement_target_62"]["values_2030"].setdefault(iso, {})[key] = v

            # back-test 2013 -> 2023 of every country, from the datasets the release pinned
            t0 = time.perf_counter()
            backtest: dict[str, Any] = {}
            with engines["sim"].connect() as conn:
                subjects = [
                    r[0]
                    for r in conn.execute(
                        text("""SELECT DISTINCT subject_key FROM belief.release_dataset WHERE release_id=:r
                                AND concept_id='demography.wpp.population.jan1_age_sex' ORDER BY 1"""),
                        {"r": rel.release_id},
                    ).all()
                ]
                for iso in subjects:
                    tabs = {
                        n: resolve_dataset(
                            conn, store, rel.release_id, InputRequirement(n, wpp.CONCEPTS[n], iso, (), "dataset")
                        ).table
                        for n in ("population", "deaths", "births")
                    }
                    d = wpp.country_demography(iso, **tabs)
                    try:
                        bt = C.backtest(d, jump_off=2013, target_year=2023, base_period=(2008, 2012))
                        backtest[iso] = {
                            "open_age": bt["open_age"],
                            "total_ape": {k.split(";")[0]: v.get("total_ape") for k, v in bt["variants"].items()},
                            "group_ape_avg_residual": {
                                g: v["ape"]
                                for g, v in bt["variants"]["mig=average_residual;fert=constant_base_period_rates"][
                                    "groups"
                                ].items()
                            },
                            "mape_single_age_avg_residual": bt["variants"][
                                "mig=average_residual;fert=constant_base_period_rates"
                            ]["groups"]["all_ages"]["mape_single_age_sex"],
                            "naive_no_change_total_ape": bt["naive_no_change_total_ape"],
                            "constant_growth_total_ape": bt["constant_growth_total_ape"],
                            "linear_trend_total_ape": bt["linear_trend_total_ape"],
                            "observed_total_2023": bt["benchmarks"]["observed_total"],
                            "data_notes": bt["data_notes"][:3],
                        }
                    except RevaError as e:
                        backtest[iso] = {"refused": e.code, "detail": e.message[:200]}
            stats["backtest"] = {"seconds": round(time.perf_counter() - t0, 1), "countries": len(backtest)}
            stats["finished"] = utc_now().isoformat()
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "live_stats.json").write_text(json.dumps(stats, indent=1, default=str))
                (out_dir / "backtest.json").write_text(json.dumps(backtest, indent=1, default=str))
            assert stats["cohort_all"]["status"] == "succeeded", stats["cohort_all"]
            assert len(stats["cohort_all"]["ready"]) > 200
            assert stats["retirement_shift_plus_24m"]["status"] == "succeeded"
            assert stats["retirement_shift_l2_five"]["status"] == "succeeded"
            assert stats["retirement_target_62"]["status"] == "succeeded"
            ok = [v for v in backtest.values() if "total_ape" in v]
            assert len(ok) > 200 and all(Decimal(v["naive_no_change_total_ape"]) >= 0 for v in ok)
        finally:
            for e in engines.values():
                e.dispose()
