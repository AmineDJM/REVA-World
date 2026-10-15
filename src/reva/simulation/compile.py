"""Scenario compilation: pinned baseline release, input resolution, plugin compile.

Inputs come only from the scenario's pinned *published* belief release:

* scalars: ``selected_report`` estimates whose support is exactly the calendar year
  requested (``InputRequirement.years``); an absent estimate or a typed ``Missing``
  value is a missing input (never omitted silently, never replaced by zero);
* datasets: the dataset artifacts the release pinned at publication
  (``belief.release_dataset``), loaded read-only from the artifact store and checked
  against their registered sha256 (:mod:`reva.simulation.datasets`).

The model-specific parts (typed intervention, per-country plans, coupling) live in
the model plugin (for the crude model: :mod:`reva.simulation.crude_plugin`, whose V1
names are re-exported here for compatibility).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from reva.adapters.postgres.repositories import resolve_alias
from reva.belief.snapshots import estimates, release_by_id
from reva.kernel.errors import CapabilityGap, IncompatibleSemantics, LeakageBlocked, ValidationProblem
from reva.kernel.ids import external_id
from reva.kernel.values import parse_value
from reva.ports.storage import ArtifactStore
from reva.simulation import plugins
from reva.simulation.crude_plugin import (  # noqa: F401  (V1 names, re-exported)
    SUPPORTED_CHANGES,
    CompiledScenario,
    CountryPlan,
    TypedChange,
    build_coupling,
    calendar_year_of,
    compile_intervention,
)
from reva.simulation.plugins import DatasetInput, InputRequirement, ResolvedInputs, ScalarInput, SubjectRef

_year_of_calendar_support = calendar_year_of  # V1 name


def resolve_subject(conn: Connection, iso3: str) -> tuple[UUID, str]:
    sid = resolve_alias(conn, "iso3166_alpha3", iso3)
    if sid is not None:
        return sid, "reviewed_alias:iso3166_alpha3"
    return external_id("reva.entity", iso3), "deterministic_entity_id"


def load_release(conn: Connection, release_id: UUID, knowledge_cutoff: str) -> dict[str, Any]:
    rel = release_by_id(conn, release_id)
    if rel is None or rel.get("sequence") is None:
        raise ValidationProblem(f"baseline release {release_id} is not a published release")
    cutoff = datetime.fromisoformat(knowledge_cutoff.replace("Z", "+00:00"))
    if rel["knowledge_cutoff"] > cutoff:
        raise LeakageBlocked(
            f"baseline release cutoff {rel['knowledge_cutoff'].isoformat()} is after the scenario knowledge "
            f"cutoff {cutoff.isoformat()}"
        )
    return rel


def _resolve_scalars(conn: Connection, release_id: UUID, req: InputRequirement,
                     subj: SubjectRef) -> dict[tuple[str, str, int], ScalarInput]:
    rows = [r for r in estimates(conn, release_id, subject_id=subj.entity_id, concept_id=req.concept)
            if r["modality"] == "selected_report"]
    out: dict[tuple[str, str, int], ScalarInput] = {}
    for year in req.years:
        hits = [r for r in rows if calendar_year_of(r["temporal_extent"]) == year]
        if not hits:
            out[(req.name, req.subject, year)] = ScalarInput(req, year, subj.entity_id, None)
            continue
        if len({str(r["id"]) for r in hits}) > 1:
            raise IncompatibleSemantics(
                f"{len(hits)} distinct selected estimates for {req.concept} {year}; "
                "the baseline is ambiguous", input=req.name)
        r = hits[0]
        out[(req.name, req.subject, year)] = ScalarInput(
            req, year, subj.entity_id, parse_value(dict(r["value"])), dict(r["value"]), str(r["id"]),
            dict(r["temporal_extent"]), r["aggregation"], dict(r.get("source_flags") or {}), r["modality"])
    return out


def resolve_inputs(conn: Connection, release: dict[str, Any], requirements: Iterable[InputRequirement],
                   store: ArtifactStore | None = None) -> ResolvedInputs:
    """Resolve every declared requirement against the pinned release (read only)."""
    reqs = tuple(requirements)
    for r in reqs:
        if r.kind not in ("scalar", "dataset"):
            raise ValidationProblem(f"input {r.name!r}: unknown requirement kind {r.kind!r}")
    subjects: dict[str, SubjectRef] = {}
    for r in reqs:
        if r.subject not in subjects:
            sid, how = resolve_subject(conn, r.subject)
            subjects[r.subject] = SubjectRef(r.subject, sid, how)
    scalars: dict[tuple[str, str, int], ScalarInput] = {}
    datasets: dict[tuple[str, str], DatasetInput] = {}
    for r in reqs:
        if r.kind == "scalar":
            scalars.update(_resolve_scalars(conn, release["id"], r, subjects[r.subject]))
        else:
            from reva.simulation.datasets import resolve_dataset

            if store is None:
                raise CapabilityGap("dataset inputs need the artifact store; this execution path has none",
                                    capability="simulation.dataset_inputs", input=r.name)
            datasets[(r.name, r.subject)] = resolve_dataset(conn, store, release["id"], r)
    return ResolvedInputs(release=release, requirements=reqs, subjects=subjects, scalars=scalars,
                          datasets=datasets)


def compile_run(conn: Connection, spec_json: dict[str, Any], store: ArtifactStore | None = None
                ) -> tuple[plugins.SimulationModel, Any, ResolvedInputs]:
    """Model validation, pinned release, input resolution and plugin compile (in this order).

    Raises on unsupported requests; missing inputs are reported on the compiled object
    (or by ``ResolvedInputs.missing_problems()``), never raised away or zeroed.
    """
    model = plugins.get(spec_json["model_key"])
    if spec_json.get("model_version") != model.model_version:
        raise CapabilityGap(
            f"model {spec_json['model_key']}@{spec_json.get('model_version')} is not available "
            f"(registered: {model.model_version})", capability=f"model.{spec_json['model_key']}")
    model.validate(spec_json)
    release = load_release(conn, UUID(spec_json["baseline_release_id"]), spec_json["knowledge_cutoff"])
    inputs = resolve_inputs(conn, release, model.requirements(spec_json), store)
    return model, model.compile(inputs, spec_json), inputs


def compile_scenario(conn: Connection, spec: Any, store: ArtifactStore | None = None) -> Any:
    """V1 entry point: the compiled plan of a validated spec (any registered model)."""
    return compile_run(conn, spec.as_json(), store)[1]
