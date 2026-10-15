"""Stored out-of-sample back-test of ``demography.cohort_component@1.0.0`` (V2 step 3a, agent POP).

Reads ONLY the dataset inputs pinned by a published belief release (re-hashed by
``reva.simulation.datasets.resolve_dataset``), runs the pure back-test of
:mod:`reva.models.population.cohort` per country, stores the evaluation manifest
content-addressed (namespace ``evaluation``) and records one ``model.evaluation`` row per
country in a new evaluation-mode context.

Replay mode is ``revised_data_retrospective``: the historical inputs (2008-2013) are the
*current* Eurostat vintage, not the data as published in 2013, so the errors are not what a
forecaster would have seen in real time (stated in every row). Nothing from the test period
(after the jump-off) is used to fit anything; there is no tuning.

Also records, separately and labelled as such, a plausibility comparison of the 2050
projection with the EUROPOP2023 baseline (another conditional projection, not validation).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from reva.adapters.postgres import repositories as repo
from reva.kernel.errors import MissingInput, RevaError
from reva.models.population import cohort as C
from reva.models.population.cohort_component import DATASET_CONCEPTS
from reva.ports.storage import ArtifactStore
from reva.simulation.datasets import resolve_dataset
from reva.simulation.plugins import InputRequirement

SCHEMA = "reva-population-backtest/1"
REPLAY_MODE = "revised_data_retrospective"
EUROPOP_POPULATION = "demography.projection.europop2023.population_age_sex"
@dataclass(frozen=True)
class Design:
    name: str
    jump_off: int
    base_period: tuple[int, int]
    target_year: int
    note: str


PRIMARY = Design("primary_leak_free", 2013, (2008, 2012), 2023,
                 "jump-off population 1 January 2013; mortality, fertility and migration from the calendar years "
                 "2008-2012 (events before the jump-off only)")
LITERAL = Design("brief_2009_2013_rates", 2014, (2009, 2013), 2023,
                 "rates from 2009-2013 as in the brief; this needs 2013 events and the 1 January 2014 population, so "
                 "the jump-off is 1 January 2014 (9 projected years)")
STATEMENT = ("Out-of-sample back-test on the current (revised) Eurostat vintage; no parameter was tuned on the "
             "test period. Errors describe this method on past data only; they are not a probability statement "
             "about future projections. The EUROPOP2023 comparison is a plausibility check, not validation.")


def _datasets(conn: Any, store: ArtifactStore, release_id: Any, iso3: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    names = {**DATASET_CONCEPTS, "europop_population": EUROPOP_POPULATION}
    for name, concept in names.items():
        d = resolve_dataset(conn, store, release_id, InputRequirement(name, concept, iso3, (), "dataset"))
        out[name] = d
    return out


def evaluate_data(data: C.CountryDemography, comparison_jump_off: int = 2025,
                  comparison_base: tuple[int, int] = (2019, 2023)) -> dict[str, Any]:
    """Pure: both back-test designs and the EUROPOP2023 plausibility comparison for one country."""
    out: dict[str, Any] = {"country": data.iso3, "status": "evaluated", "backtests": {},
                           "europop2023_comparison": {}}
    for bt in (PRIMARY, LITERAL):
        try:
            out["backtests"][bt.name] = {"design": bt.note, **C.backtest(
                data, jump_off=bt.jump_off, target_year=bt.target_year, base_period=bt.base_period)}
        except (MissingInput, RevaError) as e:
            out["backtests"][bt.name] = {"status": "not_evaluated", "problem": e.problem()}
    for mig, fert in (("europop2023_bsl", "europop2023_bsl"), ("average_residual", "constant_base_period_rates"),
                      ("zero", "constant_base_period_rates")):
        a = C.Assumptions(migration=mig, fertility=fert, base_period=comparison_base)
        try:
            p = C.project(data, jump_off=comparison_jump_off, end_year=2050, assumptions=a)
            out["europop2023_comparison"][a.variant] = C.europop_comparison(p, data)
        except (MissingInput, RevaError) as e:
            out["europop2023_comparison"][a.variant] = {"status": "unavailable", "problem": e.problem()}
    return out


def evaluate_country(conn: Any, store: ArtifactStore, release_id: Any, iso3: str,
                     comparison_jump_off: int, comparison_base: tuple[int, int]) -> dict[str, Any]:
    ds = _datasets(conn, store, release_id, iso3)
    missing = [n for n in ("population", "deaths", "births") if ds[n].is_missing]
    prov = {n: d.provenance() for n, d in ds.items() if not d.is_missing}
    if missing:
        return {"country": iso3, "status": "missing_input", "missing": missing, "inputs": prov}
    data = C.CountryDemography.from_tables(
        iso3, population=ds["population"].table, deaths=ds["deaths"].table, births=ds["births"].table,
        europop_migration=None if ds["europop_migration"].is_missing else ds["europop_migration"].table,
        europop_fertility=None if ds["europop_fertility"].is_missing else ds["europop_fertility"].table,
        europop_population=None if ds["europop_population"].is_missing else ds["europop_population"].table)
    return {**evaluate_data(data, comparison_jump_off, comparison_base), "inputs": prov}


def run_population_evaluation(engine: Engine, store: ArtifactStore, release_id: Any, countries: Sequence[str], *,
                              comparison_jump_off: int = 2025, comparison_base: tuple[int, int] = (2019, 2023),
                              actor: str = "worker:population-backtest") -> dict[str, Any]:
    """Back-test every country against the release's pinned datasets and store the evaluation."""
    with engine.begin() as conn:
        rel = conn.execute(text("""SELECT id, manifest_sha256, origin, knowledge_cutoff, context_id
                                   FROM belief.release WHERE id = :r"""), {"r": release_id}).mappings().one()
        results = {iso3: evaluate_country(conn, store, rel["id"], iso3, comparison_jump_off, comparison_base)
                   for iso3 in sorted(countries)}
        manifest = {
            "schema": SCHEMA, "model": f"{C.MODEL_KEY}@{C.MODEL_VERSION}",
            "implementation_digest": C.implementation_digest(), "numeric_type": C.NUMERIC_TYPE,
            "label": C.LABEL, "replay_mode": REPLAY_MODE, "statement": STATEMENT,
            "baseline_release": {"id": str(rel["id"]), "manifest_sha256": rel["manifest_sha256"],
                                 "knowledge_cutoff": rel["knowledge_cutoff"].isoformat(), "origin": rel["origin"]},
            "designs": {"primary": PRIMARY.note, "literal": LITERAL.note,
                        "europop2023_comparison": f"jump-off {comparison_jump_off}, base {comparison_base}"},
            "countries": results,
        }
        sha = repo.store_json_artifact(conn, store, manifest, "evaluation", schema_version="evaluation-manifest/1")
        inputs = [(rel["manifest_sha256"], {"role": "baseline_release"})]
        seen = set()
        for r in results.values():
            for name, p in r.get("inputs", {}).items():
                if p.get("artifact_sha256") and p["artifact_sha256"] not in seen:
                    seen.add(p["artifact_sha256"])
                    inputs.append((p["artifact_sha256"], {"role": "dataset_input", "name": name,
                                                           "country": r["country"]}))
        act = repo.simple_activity("evaluation.population_backtest", "evaluation", actor,
                                   {"model": manifest["model"], "replay_mode": REPLAY_MODE,
                                    "release_id": str(rel["id"]), "countries": sorted(countries)})
        repo.record_activity(conn, act, inputs=inputs, outputs=[sha])
        ctx_id = repo.create_context(conn, mode="evaluation", origin=rel["origin"], manifest_sha256=sha,
                                     knowledge_cutoff=rel["knowledge_cutoff"],
                                     parent_context_id=UUID(str(rel["context_id"])) if rel["context_id"] else None)
        mv = conn.execute(text("SELECT id FROM model.version WHERE model_key=:k AND version=:v"),
                          {"k": C.MODEL_KEY, "v": C.MODEL_VERSION}).scalar_one()
        ids = {}
        for iso3, r in results.items():
            primary = (r.get("backtests") or {}).get(PRIMARY.name) or {}
            metrics = {"label": C.LABEL, "statement": STATEMENT, "status": r["status"],
                       "primary": {k: primary.get(k) for k in ("variants", "naive_no_change_total_ape",
                                                               "open_age", "test_period_status_flags")},
                       "literal": ((r.get("backtests") or {}).get(LITERAL.name) or {}).get("variants")}
            ids[iso3] = str(conn.execute(text(
                """INSERT INTO model.evaluation(model_version_id, context_id, evaluation_manifest_sha256, replay_mode,
                     task_scope, metrics, provenance_activity_id)
                   VALUES (:m, :c, :s, :rm, CAST(:ts AS jsonb), CAST(:mt AS jsonb), :a) RETURNING id"""),
                {"m": mv, "c": ctx_id, "s": sha, "rm": REPLAY_MODE,
                 "ts": json.dumps({"task": "population_backtest", "country": iso3,
                                   "jump_off": PRIMARY.jump_off, "target_year": PRIMARY.target_year,
                                   "base_period": list(PRIMARY.base_period)}),
                 "mt": json.dumps(metrics, default=str), "a": act.id}).scalar_one())
    return {"evaluation_manifest_sha256": sha, "context_id": str(ctx_id), "evaluation_ids": ids,
            "manifest": manifest}
