"""Stored back-tests of V2 models: worker job ``backtest`` and the read helpers of the API.

Job payload (enqueued with :func:`enqueue_backtest` or ``python -m reva.evaluation.backtests``)::

    {"model_key": "demography.cohort_component" | "social.retirement_age",
     "release_id": "<uuid>"   # optional: default = the current published release
     "countries": ["LUX", ...] # optional, population only: default = every country whose
                               # population table the release pins
     "reuse": true}            # optional: return the stored back-test of the same model version,
                               # release and code instead of recomputing (default true)

For a model with a back-test function the handler reads ONLY the dataset inputs pinned by a
published belief release (``reva.simulation.datasets.resolve_dataset`` re-hashes the bytes),
calls the model's own back-test function and stores a ``model.evaluation`` row per evaluated
unit, the content-addressed evaluation manifest (namespace ``evaluation``: inputs' digests,
design, metrics) and its provenance activity, in a new ``evaluation``-mode context:

* ``demography.cohort_component`` -> :func:`reva.evaluation.population_backtest.run_population_evaluation`
  (agent POP; it stores its own manifest and one row per country);
* ``social.retirement_age`` -> :func:`reva.models.social.backtest.run` (agent PEN, the pure
  pre-registered France 2010 reform back-test); this module stores its report.

Replay mode is always ``revised_data_retrospective``: the historical inputs are today's
(revised) provider vintage, not what a forecaster had at the time. Every row says so. A model
without a back-test function is a typed :class:`CapabilityGap`, never an empty success.
Writes use the worker credentials (``model.evaluation`` + direct artifact registration), so
``backtest`` is not a simulation-engine job type.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.kernel.errors import CapabilityGap, MissingInput, ValidationProblem

JOB_TYPE = "backtest"
REPLAY_MODE = "revised_data_retrospective"
REPLAY_MODE_LABEL = "revised-data retrospective — not a real-time backtest"
EXPERIMENTAL_LABEL = "experimental model, not yet reviewed"
RETIREMENT_SCHEMA = "reva-retirement-backtest/1"
RETIREMENT_STATEMENT = (
    "Out-of-sample back-test of a conditional projection on the current (revised) Eurostat LFS vintage, protocol "
    "fixed before the test years were examined; no parameter was fitted on the test years. A good fit is not "
    "validation of the mechanism (see the placebo on ages 55-59 in the manifest)."
)
MAX_COUNTRIES = 60
PAYLOAD_KEYS = frozenset({"model_key", "release_id", "countries", "reuse"})


@dataclass(frozen=True)
class BacktestSpec:
    model_key: str
    model_version: str
    task: str  # model.evaluation.task_scope->>'task' of the stored rows
    function: str  # dotted path of the model's own back-test function (documentation + API)
    activity_kind: str
    description: str


BACKTESTS: dict[str, BacktestSpec] = {
    "demography.cohort_component": BacktestSpec(
        "demography.cohort_component",
        "1.0.0",
        "population_backtest",
        "reva.evaluation.population_backtest:run_population_evaluation",
        "evaluation.population_backtest",
        "Cohort-component projection from 1 January 2013 (rates of 2008-2012) compared with the observed "
        "population of 2023, per country; plus a labelled EUROPOP2023 plausibility comparison (not validation).",
    ),
    "social.retirement_age": BacktestSpec(
        "social.retirement_age",
        "1.0.0",
        "retirement_backtest",
        "reva.models.social.backtest:run",
        "evaluation.retirement_backtest",
        "France 2010 pension reform: employment rate 60-64 by sex predicted for 2012-2019 from 2010 data, the "
        "as-applied rule path and the literature grid (pre-registered central point), against observations.",
    ),
}
BACKTEST_TASKS = tuple(sorted(b.task for b in BACKTESTS.values()))


def backtest_spec(model_key: str) -> BacktestSpec:
    spec = BACKTESTS.get(model_key)
    if spec is None:
        raise CapabilityGap(
            f"model {model_key!r} has no back-test function",
            capability="backtest",
            model_key=str(model_key)[:200],
            supported=sorted(BACKTESTS),
        )
    return spec


# --------------------------------------------------------------------------- payload
def parse_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - PAYLOAD_KEYS)
    if unknown:
        raise ValidationProblem("unknown backtest payload keys", keys=unknown)
    key = payload.get("model_key")
    if not isinstance(key, str) or not key:
        raise ValidationProblem("backtest payload needs model_key")
    spec = backtest_spec(key)
    rid = payload.get("release_id")
    try:
        release_id = UUID(str(rid)) if rid is not None else None
    except ValueError:
        raise ValidationProblem("release_id must be a UUID") from None
    countries = payload.get("countries")
    if countries is not None:
        if spec.task != "population_backtest":
            raise ValidationProblem(
                "countries applies to the population back-test only (the retirement back-test is "
                "the pre-registered France 2010 protocol)"
            )
        if (
            not isinstance(countries, list)
            or not 1 <= len(countries) <= MAX_COUNTRIES
            or not all(isinstance(c, str) and len(c) == 3 and c.isalpha() and c.isupper() for c in countries)
        ):
            raise ValidationProblem(f"countries must be 1-{MAX_COUNTRIES} ISO 3166 alpha-3 codes")
        countries = sorted(set(countries))
    reuse = payload.get("reuse", True)
    if not isinstance(reuse, bool):
        raise ValidationProblem("reuse must be a boolean")
    return {"spec": spec, "release_id": release_id, "countries": countries, "reuse": reuse}


# --------------------------------------------------------------------------- helpers
def _release(conn: Connection, release_id: UUID | None) -> dict[str, Any]:
    from reva.belief.snapshots import release_as_of, release_by_id

    rel = release_by_id(conn, release_id) if release_id else release_as_of(conn, None)
    if rel is None or rel.get("sequence") is None:
        raise MissingInput(
            "no published belief release is available for the back-test",
            release_id=str(release_id) if release_id else None,
        )
    return rel


def _model_version_id(conn: Connection, spec: BacktestSpec) -> UUID:
    mv = conn.execute(
        text("SELECT id FROM model.version WHERE model_key=:k AND version=:v"),
        {"k": spec.model_key, "v": spec.model_version},
    ).scalar_one_or_none()
    if mv is None:
        raise MissingInput("model version is not registered", model=f"{spec.model_key}@{spec.model_version}")
    return UUID(str(mv))


def pinned_population_countries(conn: Connection, release_id: Any) -> list[str]:
    from reva.models.population.cohort_component import DATASET_CONCEPTS

    return [
        str(r)
        for r in conn.execute(
            text(
                """SELECT DISTINCT subject_key FROM belief.release_dataset WHERE release_id = :r AND concept_id = :c
           ORDER BY 1"""
            ),
            {"r": release_id, "c": DATASET_CONCEPTS["population"]},
        ).scalars()
    ]


def stored_backtests(
    conn: Connection,
    spec: BacktestSpec,
    release_id: Any,
    code: str | None = None,
    countries: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Back-test rows of this model version computed from ``release_id`` (optionally by the same code)."""
    rows = (
        conn.execute(
            text(
                """SELECT ev.id, ev.evaluation_manifest_sha256, ev.context_id, ev.task_scope, a.parameters
           FROM model.evaluation ev JOIN model.version mv ON mv.id = ev.model_version_id
           JOIN meta.activity a ON a.id = ev.provenance_activity_id
           WHERE mv.model_key = :k AND mv.version = :v AND ev.task_scope->>'task' = :t
             AND a.kind = :ak AND a.parameters->>'release_id' = :r
             AND (CAST(:code AS text) IS NULL OR a.code_digest = :code)
           ORDER BY ev.recorded_at DESC, ev.id DESC"""
            ),
            {
                "k": spec.model_key,
                "v": spec.model_version,
                "t": spec.task,
                "ak": spec.activity_kind,
                "r": str(release_id),
                "code": code,
            },
        )
        .mappings()
        .all()
    )
    out = [dict(r) for r in rows]
    if countries is not None and out:
        # one manifest = one run: reuse only a run over exactly this country set
        by_sha: dict[str, list[dict[str, Any]]] = {}
        for r in out:
            by_sha.setdefault(r["evaluation_manifest_sha256"], []).append(r)
        for group in by_sha.values():
            if sorted((group[0]["parameters"] or {}).get("countries") or []) == sorted(countries):
                return group
        return []
    if out:
        latest = out[0]["evaluation_manifest_sha256"]
        out = [r for r in out if r["evaluation_manifest_sha256"] == latest]
    return out


def _summary(rows: Sequence[Mapping[str, Any]], reused: bool) -> dict[str, Any]:
    return {
        "status": "succeeded",
        "reused": reused,
        "replay_mode": REPLAY_MODE,
        "evaluation_ids": [str(r["id"]) for r in rows],
        "evaluation_manifest_sha256": sorted({r["evaluation_manifest_sha256"] for r in rows}),
        "context_ids": sorted({str(r["context_id"]) for r in rows}),
    }


# --------------------------------------------------------------------------- retirement (PEN)
def _err(by_sex: Mapping[str, Any]) -> dict[str, Any]:
    return {
        s: {"mean_error_pp": v.get("mean_error_pp"), "mean_absolute_error_pp": v.get("mean_absolute_error_pp")}
        for s, v in by_sex.items()
    }


def retirement_metrics(report: Mapping[str, Any]) -> dict[str, Any]:
    """Compact metrics of the PEN report (the full per-year table stays in the manifest)."""
    results = report["results"]
    central, bench = report["central_point"], report["benchmark"]
    placebo = {}
    for s, per in (report.get("placebo_55_59") or {}).items():
        errs = [float(x["error_pp"]) for x in per]
        placebo[s] = {"mean_error_pp": round(sum(errs) / len(errs), 2) if errs else None}
    return {
        "label": "conditional projection under stated assumptions, not a forecast",
        "model_label": EXPERIMENTAL_LABEL,
        "statement": RETIREMENT_STATEMENT,
        "status": "evaluated",
        "unit": "percentage points of the employment rate (predicted - observed)",
        "central_point": central,
        "central": _err(results[central]),
        "benchmark": bench,
        "benchmark_errors": _err(results[bench]),
        "grid": {pid: _err(v) for pid, v in sorted(results.items())},
        "excluded_points": [e["point_id"] for e in report.get("excluded_points") or []],
        "placebo_55_59": placebo,
        "report_digest": report.get("digest"),
    }


def run_retirement_backtest(
    engine: Engine, store: Any, release_id: Any, *, actor: str = "worker:backtest", lease: Any = None
) -> dict[str, Any]:
    """Run the PEN back-test on the release's pinned FRA LFS tables and store it (one row)."""
    from reva.adapters.postgres import repositories as repo
    from reva.models.social import backtest as bt
    from reva.models.social import retirement_age as ra
    from reva.simulation.datasets import resolve_dataset
    from reva.simulation.plugins import InputRequirement

    spec = BACKTESTS["social.retirement_age"]
    with engine.begin() as conn:
        if lease is not None:
            lease(conn)
        rel = _release(conn, UUID(str(release_id)))
        inputs = {
            name: resolve_dataset(
                conn, store, rel["id"], InputRequirement(name, ra.CONCEPTS[name], bt.COUNTRY, (), "dataset")
            )
            for name in ("employment_rate", "activity_rate")
        }
        missing = sorted(n for n, d in inputs.items() if d.is_missing)
        if missing:
            raise MissingInput(
                "the release pins no Eurostat LFS table needed by the retirement back-test",
                missing=missing,
                country=bt.COUNTRY,
                release_id=str(rel["id"]),
                concepts=[ra.CONCEPTS[n] for n in missing],
            )
        emp = inputs["employment_rate"].table.to_pylist()  # type: ignore[union-attr]
        act = inputs["activity_rate"].table.to_pylist()  # type: ignore[union-attr]
        try:
            report = bt.run(emp, act)
        except (StopIteration, KeyError) as e:
            raise MissingInput(
                "pinned LFS tables lack rows required by the back-test protocol",
                country=bt.COUNTRY,
                detail=type(e).__name__,
            ) from None
        prov = {n: {**d.provenance(), "manifest_sha256": d.manifest.get("manifest_sha256")} for n, d in inputs.items()}
        design = {
            "protocol": report["protocol"],
            "country": report["country"],
            "age_group": report["age_group"],
            "base_year": report["base_year"],
            "test_years": report["test_years"],
            "scenario_path": report["scenario_path"],
            "reference_path": report["reference_path"],
            "central_point": report["central_point"],
            "benchmark": report["benchmark"],
            "weights": report["weights"],
            "rule_table_digest": report["rule_table_digest"],
            "assumption_registry_digest": report["assumption_registry_digest"],
            "information_set": f"LFS rows with year <= {report['base_year']} only (the prediction function "
            "refuses later rows)",
        }
        metrics = retirement_metrics(report)
        manifest = {
            "schema": RETIREMENT_SCHEMA,
            "model": f"{ra.MODEL_KEY}@{ra.MODEL_VERSION}",
            "implementation_digest": ra.manifest_dict()["implementation_digest"],
            "backtest_function": spec.function,
            "label": metrics["label"],
            "model_label": EXPERIMENTAL_LABEL,
            "replay_mode": REPLAY_MODE,
            "replay_mode_label": REPLAY_MODE_LABEL,
            "statement": RETIREMENT_STATEMENT,
            "baseline_release": {
                "id": str(rel["id"]),
                "manifest_sha256": rel["manifest_sha256"],
                "knowledge_cutoff": rel["knowledge_cutoff"].isoformat(),
                "origin": rel["origin"],
            },
            "inputs": prov,
            "design": design,
            "metrics": metrics,
            "report": report,
        }
        sha = repo.store_json_artifact(conn, store, manifest, "evaluation", schema_version="evaluation-manifest/1")
        act_inputs = [(rel["manifest_sha256"], {"role": "baseline_release"})]
        act_inputs += [
            (p["artifact_sha256"], {"role": "dataset_input", "name": n, "country": bt.COUNTRY}) for n, p in prov.items()
        ]
        activity = repo.simple_activity(
            spec.activity_kind,
            "evaluation",
            actor,
            {
                "model": manifest["model"],
                "replay_mode": REPLAY_MODE,
                "release_id": str(rel["id"]),
                "countries": [bt.COUNTRY],
                "protocol": report["protocol"],
            },
        )
        repo.record_activity(conn, activity, inputs=act_inputs, outputs=[sha])
        ctx_id = repo.create_context(
            conn,
            mode="evaluation",
            origin=rel["origin"],
            manifest_sha256=sha,
            knowledge_cutoff=rel["knowledge_cutoff"],
            parent_context_id=UUID(str(rel["context_id"])) if rel["context_id"] else None,
        )
        mv = _model_version_id(conn, spec)
        task_scope = {
            "task": spec.task,
            "country": bt.COUNTRY,
            **{
                k: design[k]
                for k in (
                    "protocol",
                    "age_group",
                    "base_year",
                    "test_years",
                    "scenario_path",
                    "reference_path",
                    "central_point",
                    "benchmark",
                )
            },
        }
        eid = conn.execute(
            text(
                """INSERT INTO model.evaluation(model_version_id, context_id, evaluation_manifest_sha256, replay_mode,
                 task_scope, metrics, provenance_activity_id)
               VALUES (:m, :c, :s, :rm, CAST(:ts AS jsonb), CAST(:mt AS jsonb), :a) RETURNING id"""
            ),
            {
                "m": mv,
                "c": ctx_id,
                "s": sha,
                "rm": REPLAY_MODE,
                "ts": json.dumps(task_scope),
                "mt": json.dumps(metrics, default=str),
                "a": activity.id,
            },
        ).scalar_one()
    return {
        "evaluation_manifest_sha256": sha,
        "context_id": str(ctx_id),
        "evaluation_ids": {bt.COUNTRY: str(eid)},
        "manifest": manifest,
    }


# --------------------------------------------------------------------------- job handler
def handle_backtest(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Job handler for ``backtest`` (see module docstring)."""
    from reva.kernel.context import Origin, assert_publishable
    from reva.operations.environment import code_digest

    p = parse_payload(payload or {})
    spec: BacktestSpec = p["spec"]
    code = code_digest()
    with ctx.worker_engine.connect() as conn:
        rel = _release(conn, p["release_id"])
        assert_publishable(Origin(str(rel["origin"])), ctx.settings.reva_env)
        _model_version_id(conn, spec)
        countries = p["countries"]
        if spec.task == "population_backtest" and countries is None:
            countries = pinned_population_countries(conn, rel["id"])[:MAX_COUNTRIES]
            if not countries:
                raise MissingInput(
                    "the release pins no population table: nothing to back-test",
                    release_id=str(rel["id"]),
                    model=spec.model_key,
                )
        if p["reuse"]:
            have = stored_backtests(
                conn, spec, rel["id"], code, countries if spec.task == "population_backtest" else None
            )
            if have:
                return {**_summary(have, True), "model_key": spec.model_key, "release_id": str(rel["id"])}
    if ctx.heartbeat({"stage": "backtest", "model_key": spec.model_key}) == "cancel_requested":
        return {"status": "cancelled"}
    actor = f"worker:{ctx.worker_id}"
    if spec.task == "population_backtest":
        from reva.evaluation.population_backtest import run_population_evaluation

        with ctx.worker_engine.begin() as conn:
            ctx.assert_lease(conn)
        out = run_population_evaluation(ctx.worker_engine, ctx.store, rel["id"], countries, actor=actor)
    else:
        out = run_retirement_backtest(ctx.worker_engine, ctx.store, rel["id"], actor=actor, lease=ctx.assert_lease)
    ids = out["evaluation_ids"]
    return {
        "status": "succeeded",
        "reused": False,
        "replay_mode": REPLAY_MODE,
        "model_key": spec.model_key,
        "release_id": str(rel["id"]),
        "evaluation_ids": sorted(ids.values()),
        "evaluation_manifest_sha256": [out["evaluation_manifest_sha256"]],
        "context_ids": [out["context_id"]],
        "units": sorted(ids),
    }


def enqueue_backtest(
    engine: Engine,
    model_key: str,
    *,
    release_id: Any = None,
    countries: Sequence[str] | None = None,
    created_by: str = "worker:backtest",
) -> UUID:
    """Enqueue (idempotently) a ``backtest`` job with worker credentials. The release defaults to the
    current published release, resolved now so the job key names it."""
    from reva.adapters.postgres import queue

    payload: dict[str, Any] = {"model_key": model_key}
    if countries is not None:
        payload["countries"] = sorted(set(countries))
    parse_payload(payload)
    with engine.begin() as conn:
        rel = _release(conn, UUID(str(release_id)) if release_id else None)
        payload["release_id"] = str(rel["id"])
        parse_payload(payload)
        suffix = ",".join(payload.get("countries") or [])
        key = f"backtest:{model_key}:{rel['id']}" + (f":{suffix}" if suffix else "")
        return queue.enqueue(conn, JOB_TYPE, payload, key[:200], created_by=created_by)


# --------------------------------------------------------------------------- API read helpers
def design_of(task_scope: Mapping[str, Any]) -> dict[str, Any]:
    """Design of a stored back-test row (its task scope without the bookkeeping keys)."""
    design = {k: v for k, v in (task_scope or {}).items() if k not in ("task", "country")}
    task = (task_scope or {}).get("task")
    for b in BACKTESTS.values():
        if b.task == task:
            design.setdefault("description", b.description)
    return design


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m reva.evaluation.backtests --model <key> [--release <uuid>] [--country ISO3 ...]``:
    enqueue a back-test job (worker credentials from the environment; the worker runs it)."""
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import Settings

    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--model", required=True, choices=sorted(BACKTESTS))
    ap.add_argument("--release")
    ap.add_argument("--country", action="append")
    args = ap.parse_args(argv)
    engine = make_engine("worker", Settings())
    try:
        jid = enqueue_backtest(
            engine, args.model, release_id=args.release, countries=args.country, created_by="worker:backtest-cli"
        )
    finally:
        engine.dispose()
    print(json.dumps({"job_id": str(jid), "job_type": JOB_TYPE}), file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
