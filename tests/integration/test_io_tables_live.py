"""LIVE: Eurostat FIGARO 2024 ingest -> verified dataset -> live release -> IO price scenario (worker path).

Marked ``live`` (deselected by default; ``pytest -m live``). Uses a throwaway database and a
temporary object root; never the dev database. Each phase runs in its own process so its
peak resident memory is measured alone (the simulation worker budget is 2 GB). Timings and
memory are written to ``$REVA_IO_LIVE_REPORT`` when set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from reva.adapters.connectors import io_tables as it
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres import repositories as repo
from reva.models.economic import io_price as m
from reva.settings import Settings

pytestmark = [pytest.mark.live, pytest.mark.db]

PHASE = Path(__file__).resolve().parents[1] / "fixtures" / "io_tables" / "live_phase.py"
WORKER_MEMORY_MB = 2048
YEAR = 2024


def phase(env: dict[str, str], name: str, **args: Any) -> dict[str, Any]:
    out = subprocess.run([sys.executable, str(PHASE), name, json.dumps(args)], env=env, capture_output=True,
                         text=True, timeout=3600, cwd=Path(__file__).resolve().parents[2])
    assert out.returncode == 0, out.stderr[-4000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_live_figaro_to_scenario(tmp_path: Path) -> None:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import register_connector_version, seed
    from reva.simulation.spec import CONTRACTS, validate_scenario_spec

    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    with throwaway_database(base, prefix="reva_test_iolive") as s0:
        root = tmp_path / "objects"
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        owner = create_engine(settings.migration_url())
        seed(owner, store)
        with owner.begin() as conn:
            act = repo.simple_activity("registry.seed", "review", "maintainer:registry-seed", {"test": it.NAME})
            repo.record_activity(conn, act)
            register_connector_version(conn, store, it.FigaroConnector().describe(), act.id)
        env = {**os.environ, "REVA_MIGRATION_DATABASE_URL": settings.migration_url().render_as_string(
            hide_password=False), "REVA_DB_NAME": settings.reva_db_name, "REVA_ENV": "test",
            "REVA_OBJECT_ROOT": str(root), "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
            "POLARS_MAX_THREADS": "2"}
        timings: dict[str, Any] = {}

        fig = phase(env, "ingest_figaro", year=YEAR)
        timings["ingest_figaro"] = {k: fig[k] for k in ("seconds", "max_rss_mb")}
        assert not fig["result"]["errors"], fig["result"]["errors"]
        assert len(fig["result"]["outcomes"]) == 1 + len(it.origin_groups())
        ds = phase(env, "dataset", year=YEAR)
        timings["dataset"] = {k: ds[k] for k in ("seconds", "max_rss_mb")}
        rep = ds["result"]["identity_report"]
        assert rep["passed"], rep
        checks = {c["check"]: c for c in rep["checks"]}
        assert checks["dense_completeness"]["rows"] == it.FIGARO_DIMS.dense_cells() == 11_060_700
        assert checks["row_total_equals_column_total"]["industries"] == 3200
        wdi = phase(env, "ingest_wdi")
        assert not wdi["result"]["errors"]
        rel = phase(env, "release")
        timings["release"] = {k: rel[k] for k in ("seconds", "max_rss_mb")}

        doc = json.loads((CONTRACTS / "scenario.example.json").read_text())
        doc.update(schema_version="2.0.0", title="Live: +10% NACE B output price in every FIGARO economy",
                   model_key=m.MODEL_KEY, model_version=m.MODEL_VERSION,
                   baseline_release_id=rel["result"]["release_id"],
                   knowledge_cutoff=rel["result"]["knowledge_cutoff"],
                   countries=[k for k in m.FIGARO_KEYS if k != m.REST_OF_WORLD_KEY], reference_year=YEAR,
                   horizon={"periods": 1, "unit": "annual_accounting_period"},
                   intervention={"kind": "set_exogenous_industry_price_change", "shocks": [
                       {"origin": k, "industry": "B", "price_change_percent": "10"} for k in m.FIGARO_KEYS]},
                   assumptions={"pass_through_grid": ["0.5", "0.75", "1"]}, outputs=list(m.OUTPUT_GROUPS),
                   missing_input_policy="fail", fidelity="L2")
        s = validate_scenario_spec(doc)
        from reva.adapters.postgres.engine import make_engine

        api = make_engine("api", settings)
        with api.begin() as conn:
            sid = conn.execute(text("SELECT sim.create_scenario(CAST(:spec AS jsonb), :b, :d, 'human:test')"),
                               {"spec": json.dumps(s.as_json()), "b": s.canonical_bytes(),
                                "d": s.digest()}).scalar_one()
        for fid in ("L1", "L2"):
            run = phase(env, "simulate", scenario_id=str(sid), fidelity=fid)
            timings[f"simulate_{fid}"] = {k: run[k] for k in ("seconds", "max_rss_mb")}
            assert run["result"]["status"] == "succeeded", run["result"]
            assert run["max_rss_mb"] < WORKER_MEMORY_MB
        with owner.connect() as conn:
            rows = conn.execute(text("""SELECT output_key, applicability->>'country' AS c, variant, value
                                         FROM sim.result WHERE run_id = :r AND variant = 'L1'"""),
                                {"r": run["result"]["run_id"]}).mappings().all()
        fra = {r["output_key"]: r["value"] for r in rows if r["c"] == "FRA"}
        basket = float(fra[m.BASKET_KEY]["value"])
        assert 0 < basket < 10  # +10 % on B cannot raise a basket by more than the shock
        avg: dict[str, list[float]] = {}
        for r in rows:
            if r["output_key"].startswith(m.INDUSTRY_PREFIX) and r["value"]["kind"] == "decimal":
                avg.setdefault(r["output_key"].split(":")[1], []).append(float(r["value"]["value"]))
        ranking = sorted(((sum(v) / len(v), k) for k, v in avg.items()), reverse=True)
        top = [k for _, k in ranking[1:6]]  # [0] is B itself (exogenous)
        assert "C19" in top[:2] and {"C24", "D35"} & set(top)  # energy-intensive industries rank highest
        timings["fra_basket_L1_percent"] = fra[m.BASKET_KEY]["value"]
        timings["ranking_top"] = [(k, round(v, 3)) for v, k in ranking[:8]]
        if os.environ.get("REVA_IO_LIVE_REPORT"):
            Path(os.environ["REVA_IO_LIVE_REPORT"]).write_text(json.dumps(timings, indent=1))
        owner.dispose()
        api.dispose()
