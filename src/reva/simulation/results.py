"""Immutable publication of simulation runs, results and outcomes (reva_sim role only).

Everything for one run — model manifest artifact, result manifest artifact,
sim.run, meta.activity, sim.result rows and sim.run_outcome — is written in the
caller's transaction, which also completes the job under its fencing token, so a
worker that lost its lease can never make results visible.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.adapters.objects.common import lock_object_xact
from reva.adapters.postgres import repositories as repo
from reva.belief.snapshots import release_by_id
from reva.kernel.errors import IntegrityFailure, ValidationProblem
from reva.kernel.ids import canonical_bytes
from reva.ports.storage import ArtifactStore
from reva.simulation.engine import RunPlan

RESULT_SCHEMA = "reva-simulation-result/1"


def _j(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, default=str, allow_nan=False)


def store_sim_json_artifact(conn: Connection, store: ArtifactStore, payload: Any) -> str:
    """Store a simulation manifest and register it through ``sim.register_artifact``.

    The simulation role has no INSERT on ``meta.artifact`` (review R5-05): the
    database computes sha256 and size from the bytes and derives the schema
    label from the document's ``schema`` field, so nothing is caller-chosen.
    The location row is then written under the R-18 policy (``scenarios``
    namespace, artifact created in this transaction or already sim-located).
    """
    data = canonical_bytes(payload)
    obj = store.put_bytes(data, "scenarios", "application/json")
    lock_object_xact(conn, obj.sha256)  # shared with orphan GC (see repositories.register_artifact)
    sha = conn.execute(text("SELECT sim.register_artifact(:b)"), {"b": data}).scalar_one()
    if sha != obj.sha256 or obj.byte_size != len(data):
        raise IntegrityFailure(
            "stored simulation artifact does not match the registered bytes", sha256=obj.sha256, registered_sha256=sha
        )
    conn.execute(
        text("""INSERT INTO meta.artifact_location(artifact_sha256, storage_uri, namespace,
                                                   verified_byte_size, checksum_verified_at)
                VALUES (:sha, :uri, :ns, :size, clock_timestamp())
                ON CONFLICT (artifact_sha256, storage_uri) DO NOTHING"""),
        {"sha": obj.sha256, "uri": obj.storage_uri, "ns": obj.namespace, "size": obj.byte_size},
    )
    return sha


def _dataset_inputs(plan: RunPlan) -> list[tuple[str, dict[str, Any]]]:
    """Pinned dataset artifacts read by the run (provenance inputs of the run activity)."""
    prov = plan.input_provenance or {}
    seen: dict[str, dict[str, Any]] = {}
    for d in prov.get("datasets", []):
        if d.get("artifact_sha256") and d["artifact_sha256"] not in seen:
            seen[d["artifact_sha256"]] = {
                "role": "dataset_input",
                "dataset_id": d.get("dataset_id"),
                "input": d.get("name"),
                "subject": d.get("subject"),
            }
    return sorted(seen.items())


def existing_run(conn: Connection, idempotency_key: str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text("""SELECT r.id AS run_id, r.scenario_id, r.fidelity, o.status, o.output_digest,
                       o.output_manifest_sha256, o.cache_hit_of, o.problems,
                       COALESCE(o.applicability->>'run_status', o.status) AS run_status
                FROM sim.run r LEFT JOIN sim.run_outcome o ON o.run_id = r.id
                WHERE r.idempotency_key = :k"""),
            {"k": idempotency_key},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


def result_ref(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(run["run_id"]),
        "scenario_id": str(run["scenario_id"]),
        "fidelity": run["fidelity"],
        "status": run.get("run_status") or run["status"],
        "output_digest": run.get("output_digest"),
        "result_manifest_sha256": run.get("output_manifest_sha256"),
        "cache_hit_of": str(run["cache_hit_of"]) if run.get("cache_hit_of") else None,
        "problems": run.get("problems") or [],
    }


def publish_run(
    conn: Connection,
    store: ArtifactStore,
    plan: RunPlan,
    *,
    scenario: dict[str, Any],
    manifest: dict[str, Any],
    idempotency_key: str,
    execution_profile: dict[str, Any],
    job_id: UUID | None,
    actor: str,
) -> dict[str, Any]:
    """Write one run. Returns the job result reference."""
    release = release_by_id(conn, scenario["baseline_release_id"])
    if release is None:
        raise ValidationProblem("pinned baseline release not found")
    onto_sha = release["ontology_manifest_sha256"]
    model_sha = store_sim_json_artifact(conn, store, manifest)  # labelled model-manifest/1 in-DB
    run_id = UUID(
        str(
            conn.execute(
                text("""INSERT INTO sim.run(scenario_id, fidelity, model_manifest_sha256, ontology_manifest_sha256,
                                    idempotency_key, seed_plan, execution_profile, job_id)
                VALUES (:s, :f, :mm, :om, :k, CAST(:sp AS jsonb), CAST(:ep AS jsonb), :j) RETURNING id"""),
                {
                    "s": plan.scenario_id,
                    "f": plan.fidelity,
                    "mm": model_sha,
                    "om": onto_sha,
                    "k": idempotency_key,
                    "sp": _j(plan.seed_plan),
                    "ep": _j(execution_profile),
                    "j": job_id,
                },
            ).scalar_one()
        )
    )

    activity = repo.simple_activity(
        "simulation.run",
        "simulation",
        actor,
        {
            "run_id": str(run_id),
            "scenario_id": str(plan.scenario_id),
            "fidelity": plan.fidelity,
            "status": plan.status,
            "spec_digest": plan.spec_digest,
            "model": manifest["models"][0]["model_key"] + "@" + manifest["models"][0]["version"],
        },
    )
    results_json = [
        {
            "output_key": r.output_key,
            "country": r.iso3,
            "subject_id": str(r.subject_id),
            "variant": r.variant,
            "value": r.value,
            "uncertainty": r.uncertainty,
            **(
                {}
                if r.period is None
                else {"period": {"start": r.period[0].isoformat(), "end": r.period[1].isoformat(), "bounds": "[)"}}
            ),
        }
        for r in sorted(
            plan.results,
            key=lambda r: (r.output_key, r.iso3, r.variant, "" if r.period is None else r.period[0].isoformat()),
        )
    ]
    result_manifest = {
        "schema": RESULT_SCHEMA,
        "run_id": str(run_id),
        "scenario_id": str(plan.scenario_id),
        "scenario_spec_sha256": scenario["spec_sha256"],
        "spec_digest": plan.spec_digest,
        "fidelity": plan.fidelity,
        "status": plan.status,
        "context_id": str(scenario["context_id"]),
        "baseline": {
            "release_id": str(release["id"]),
            "manifest_sha256": release["manifest_sha256"],
            "knowledge_cutoff": release["knowledge_cutoff"],
            "origin": release["origin"],
        },
        "model_manifest_sha256": model_sha,
        "ontology_manifest_sha256": onto_sha,
        "seed_plan": plan.seed_plan,
        "execution_profile": execution_profile,
        "output_digest": plan.output_digest,
        "cache_hit_of": str(plan.cache_hit_of) if plan.cache_hit_of else None,
        "problems": plan.problems,
        "applicability": plan.applicability,
        "results": results_json,
        "provenance_activity_id": str(activity.id),
    }
    manifest_sha = store_sim_json_artifact(conn, store, result_manifest)  # simulation-result/1
    repo.record_activity(
        conn,
        activity,
        inputs=[
            (release["manifest_sha256"], {"role": "baseline_release", "release_id": str(release["id"])}),
            (scenario["spec_sha256"], {"role": "scenario_spec", "scenario_id": str(plan.scenario_id)}),
            (model_sha, {"role": "model_manifest"}),
            (onto_sha, {"role": "ontology_manifest"}),
            *_dataset_inputs(plan),
        ],
        outputs=[manifest_sha],
    )
    for r in plan.results:
        params = {
            "run": run_id,
            "k": r.output_key,
            "s": r.subject_id,
            "v": r.variant,
            "val": _j(r.value),
            "u": _j(r.uncertainty),
            "ap": _j(r.applicability),
            "sha": manifest_sha,
            "a": activity.id,
        }
        if r.period is None:
            # Single-period rows use the V1 statement (period columns stay NULL), so this
            # path also works on a database without migration 0008.
            conn.execute(
                text("""INSERT INTO sim.result(run_id, output_key, subject_id, variant, value, uncertainty,
                                               applicability, artifact_sha256, provenance_activity_id)
                        VALUES (:run, :k, :s, :v, CAST(:val AS jsonb), CAST(:u AS jsonb), CAST(:ap AS jsonb),
                                :sha, :a)"""),
                params,
            )
        else:
            conn.execute(
                text("""INSERT INTO sim.result(run_id, output_key, subject_id, variant, value, uncertainty,
                                               applicability, artifact_sha256, provenance_activity_id,
                                               period_start, period_end)
                        VALUES (:run, :k, :s, :v, CAST(:val AS jsonb), CAST(:u AS jsonb), CAST(:ap AS jsonb),
                                :sha, :a, :ps, :pe)"""),
                {**params, "ps": r.period[0], "pe": r.period[1]},
            )
    conn.execute(
        text("""INSERT INTO sim.run_outcome(run_id, status, output_manifest_sha256, output_digest, applicability,
                                            problems, cost, cache_hit_of, provenance_activity_id)
                VALUES (:r, :st, :m, :d, CAST(:ap AS jsonb), CAST(:p AS jsonb), CAST(:c AS jsonb), :ch, :a)"""),
        {
            "r": run_id,
            "st": plan.outcome_status,
            "m": manifest_sha,
            "d": plan.output_digest,
            "ap": _j({"run_status": plan.status, **plan.applicability}),
            "p": _j(plan.problems),
            "c": _j(plan.cost),
            "ch": plan.cache_hit_of,
            "a": activity.id,
        },
    )
    return {
        "run_id": str(run_id),
        "scenario_id": str(plan.scenario_id),
        "fidelity": plan.fidelity,
        "status": plan.status,
        "output_digest": plan.output_digest,
        "result_manifest_sha256": manifest_sha,
        "cache_hit_of": str(plan.cache_hit_of) if plan.cache_hit_of else None,
        "problems": plan.problems,
    }
