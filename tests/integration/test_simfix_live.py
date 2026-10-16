"""LIVE (V3 SIMFIX): WPP 2024 + ILOSTAT rates + ILOSTAT SDG 1.3.1 coverage ingest -> release -> retirement age
62 for cohorts born 1965+ for every country with ``social.retirement_age`` 1.1.0 and 1.2.0 (pension coverage),
in a THROWAWAY database. Deselected by default (``-m live``).

Egress is reserved and metered against ``registry/policies/budgets.yaml`` (sources ``un.wpp``,
``ilostat.rplumber``, ``worldbank.wdi``) through the throwaway database's budget ledger.
``REVA_SIMFIX_LIVE_OUT`` (optional): directory for the comparison (JSON); ``REVA_SIMFIX_LIVE_OBJECTS``
(optional): object-store root (default: pytest tmp).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_simworld_live import _create, _ingest, _job, _summary

from reva.adapters.connectors import ilostat_labour as IL
from reva.adapters.connectors import ilostat_social_protection as SP
from reva.adapters.connectors import un_wpp_demography as W
from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.objects.filesystem import FilesystemArtifactStore
from reva.adapters.postgres.engine import make_engine
from reva.belief.publication import publish_release
from reva.kernel.context import Origin
from reva.kernel.time import utc_now
from reva.settings import Settings

pytestmark = [pytest.mark.live, pytest.mark.db]

FOCUS = ("IND", "SEN", "BRA", "MEX", "IDN", "JPN", "KOR", "NGA", "PAK", "EGY")


def test_live_retirement_target_62_with_and_without_coverage(tmp_path: Path) -> None:
    from reva.adapters.postgres.schema_contract import throwaway_database
    from reva.cli.seed import seed

    out_dir = Path(os.environ["REVA_SIMFIX_LIVE_OUT"]) if os.environ.get("REVA_SIMFIX_LIVE_OUT") else None
    root = Path(os.environ.get("REVA_SIMFIX_LIVE_OBJECTS") or (tmp_path / "objects"))
    base = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if base.reva_migration_database_url is None:
        pytest.skip("REVA_MIGRATION_DATABASE_URL not configured")
    with create_engine(base.migration_url().set(database="postgres")).connect() as c:
        c.execute(text("SELECT 1"))
    stats: dict[str, Any] = {"started": utc_now().isoformat()}

    def dump() -> None:
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "simfix_live.json").write_text(json.dumps(stats, indent=1, default=str))

    with throwaway_database(base, prefix="reva_live_simfix") as s0:
        settings = Settings.model_validate({**s0.model_dump(), "reva_object_root": root})
        stats["database"] = settings.migration_url().database
        assert stats["database"] != "reva"
        store = FilesystemArtifactStore(root, settings.reva_s3_prefix)
        engines = {k: make_engine(k, settings) for k in ("migration", "api", "worker", "sim")}
        try:
            seed(engines["migration"], store)
            for key, conn, hook in (
                ("ingest_wpp", W.UnWppDemographyConnector(), W.post_publish_datasets),
                ("ingest_ilostat", IL.IlostatLabourConnector(), IL.post_publish_datasets),
                ("ingest_coverage", SP.IlostatSocialProtectionConnector(), SP.post_publish_datasets),
            ):
                stats[key] = _ingest(conn, engines, store, settings, hook)
                assert stats[key]["report"]["errors"] == [], stats[key]["report"]["errors"]
                dump()
            wb = builtin_connectors()["worldbank.wdi.indicators"]  # a release needs one selected estimate (live)
            stats["ingest_wb"] = _ingest(wb, engines, store, settings, None, extra=10)
            with engines["migration"].connect() as c:
                stats["datasets"] = {
                    r[0]: r[1]
                    for r in c.execute(
                        text("SELECT concept_id, count(*) FROM evidence.dataset GROUP BY concept_id")
                    ).all()
                }
            rel = publish_release(engines["worker"], store, cutoff=utc_now(), origin=Origin.live, environment="test")
            stats["release"] = str(rel.release_id)
            dump()
            doc = {
                "schema_version": "2.0.0",
                "baseline_release_id": str(rel.release_id),
                "knowledge_cutoff": rel.knowledge_cutoff.isoformat(),
                "seed": 0,
                "missing_input_policy": "skip_missing_countries",
                "model_key": "social.retirement_age",
                "countries": ["ALL"],
                "reference_year": 2022,
                "horizon": {"periods": 13, "unit": "calendar_year"},
                "intervention": {
                    "kind": "retirement_age_target",
                    "reference_path": "wbl2026_held_constant",
                    "target_age_months": 744,
                    "first_birth_year": 1965,
                },
                "assumptions": {"employment_response": {"central": "rabate_rochut_2020_fra"}},
                "fidelity": "L1",
                "outputs": ["employment_change_55_64", "pension_recipients_change_55_64"],
            }
            values: dict[str, dict[str, dict[str, str]]] = {}
            for version in ("1.1.0", "1.2.0"):
                key = f"target_62_{version}"
                ref = _job(
                    engines,
                    settings,
                    store,
                    _create(engines, {**doc, "model_version": version, "title": f"Live SIMFIX: age 62 ({version})"}),
                    "L1",
                )
                stats[key] = _summary(engines, ref)
                with engines["migration"].connect() as c:
                    rows = c.execute(
                        text("""SELECT applicability->>'country' AS c, output_key, value->>'value' AS v
                                FROM sim.result WHERE run_id=:r AND period_start = '2030-01-01'"""),
                        {"r": ref["run_id"]},
                    ).all()
                    app = c.execute(
                        text("SELECT applicability FROM sim.run_outcome WHERE run_id=:r"), {"r": ref["run_id"]}
                    ).scalar_one()
                values[version] = {}
                for iso, k, v in rows:
                    values[version].setdefault(iso, {})[k] = v
                if version == "1.2.0":
                    stats["coverage_used"] = {
                        iso: e["pension_coverage"]
                        for iso, e in app["countries"].items()
                        if e.get("pension_coverage") is not None
                    }
                    stats["coverage_refusals"] = {
                        iso: [p for p in e["problems"] if p.get("input") == "pension_coverage"]
                        for iso, e in app["countries"].items()
                        if any(p.get("input") == "pension_coverage" for p in e["problems"])
                    }
                dump()
            stats["values_2030"] = values
            stats["focus"] = {
                iso: {
                    "1.1.0": values["1.1.0"].get(iso),
                    "1.2.0": values["1.2.0"].get(iso),
                    "coverage": stats["coverage_used"].get(iso),
                    "status_1_2_0": (stats["target_62_1.2.0"]["not_ready"].get(iso) or "ready"),
                }
                for iso in FOCUS
            }
            stats["finished"] = utc_now().isoformat()
            dump()
            assert stats["target_62_1.1.0"]["status"] == "succeeded"
            assert stats["target_62_1.2.0"]["status"] == "succeeded"
            ready12 = set(stats["target_62_1.2.0"]["ready"])
            assert ready12 <= set(stats["target_62_1.1.0"]["ready"])  # coverage only ever removes countries
            assert all(iso in stats["coverage_used"] for iso in ready12)
        finally:
            for e in engines.values():
                e.dispose()
