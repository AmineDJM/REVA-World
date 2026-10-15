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
        # V2 step 1 (ALLC): crosswalk aliases are provider_declared, not reviewed; say which.
        from sqlalchemy import text

        kind = conn.execute(text(
            "SELECT confidence_kind FROM world.alias WHERE provider = 'iso3166_alpha3' AND external_key = :k "
            "AND entity_id = :e ORDER BY recorded_at DESC LIMIT 1"), {"k": iso3, "e": sid}).scalar_one_or_none()
        if kind == "provider_declared":
            return sid, "provider_declared_alias:iso3166_alpha3"
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
    selection = None
    if is_all_countries(spec_json.get("countries")):  # V2 step 1 (ALLC)
        selection = resolve_all_countries(conn, model, release, spec_json)
        spec_json = {**spec_json, "countries": selection["resolved"]}
    inputs = resolve_inputs(conn, release, model.requirements(spec_json), store)
    compiled = model.compile(inputs, spec_json)
    if selection is not None:
        compiled.country_selection = selection
    return model, compiled, inputs


# --- V2 step 1 (agent ALLC): countries ["ALL"] -------------------------------------------
ALL_COUNTRIES = "ALL"
ALL_RULE = ("every entity.country subject of the pinned release holding a selected_report estimate (a value or "
            "a typed Missing) of at least one concept the model requires, for a required year; keyed by its "
            "ISO 3166 alpha-3 alias, or by its World Bank code when it has none (CHI, XKX)")


def is_all_countries(countries: Any) -> bool:
    return isinstance(countries, list | tuple) and len(countries) == 1 and countries[0] == ALL_COUNTRIES


def resolve_all_countries(conn: Connection, model: Any, release: dict[str, Any],
                          spec_json: dict[str, Any]) -> dict[str, Any]:
    """Resolve countries ["ALL"] against the pinned release (deterministic, read only).

    Only models declaring ``supports_all_countries`` accept it. The required concepts
    and years come from the model's own ``requirements`` (probed with subject "ALL").
    A country lacking some inputs is still selected: the missing-input policy then
    refuses the run (``fail``) or lists it as skipped (``skip_missing_countries``).
    """
    from sqlalchemy import text

    if not getattr(model, "supports_all_countries", False):
        raise CapabilityGap(f"model {model.model_key} does not accept countries [\"ALL\"]",
                            capability="scenario.all_countries", model=model.model_key)
    probe = model.requirements(spec_json)
    concepts = sorted({r.concept for r in probe if r.kind == "scalar"})
    years = sorted({y for r in probe if r.kind == "scalar" for y in r.years})
    rows = conn.execute(text(
        """SELECT DISTINCT e.subject_id FROM belief.estimate e
           JOIN ontology.concept_version c ON c.id = e.concept_version_id
           JOIN world.entity ent ON ent.id = e.subject_id
           JOIN ontology.concept_version k ON k.id = ent.kind_concept_id AND k.concept_id = 'entity.country'
           WHERE e.release_id = :r AND e.modality = 'selected_report' AND c.concept_id = ANY(:c)
             AND world.support_ref_year(e.support_id) = ANY(:y)"""),
        {"r": release["id"], "c": concepts, "y": years}).scalars().all()
    ids = sorted({UUID(str(i)) for i in rows}, key=str)
    keys = conn.execute(text(
        """SELECT DISTINCT ON (entity_id) entity_id, external_key FROM world.alias
           WHERE entity_id = ANY(:ids) AND provider IN ('iso3166_alpha3', 'worldbank')
             AND external_key ~ '^[A-Z]{3}$'
           ORDER BY entity_id, (provider = 'iso3166_alpha3') DESC, recorded_at DESC"""),
        {"ids": ids}).all()
    resolved: dict[str, UUID] = {}
    for eid, key in keys:
        if resolve_subject(conn, key)[0] == UUID(str(eid)):  # the key must resolve back to this entity
            resolved[key] = UUID(str(eid))
    unkeyed = sorted(str(i) for i in set(ids) - set(resolved.values()))
    if not resolved:
        raise ValidationProblem("countries [\"ALL\"] resolved to no country: the pinned release holds no estimate "
                                "of the required concepts for the reference year", concepts=concepts, years=years)
    return {"requested": [ALL_COUNTRIES], "rule": ALL_RULE, "concepts": concepts, "years": years,
            "resolved": sorted(resolved), "resolved_count": len(resolved), "unkeyed_subject_ids": unkeyed}
# --- end V2 step 1 (ALLC) -------------------------------------------------------------------


def compile_scenario(conn: Connection, spec: Any, store: ArtifactStore | None = None) -> Any:
    """V1 entry point: the compiled plan of a validated spec (any registered model)."""
    return compile_run(conn, spec.as_json(), store)[1]


def preflight_missing(conn: Connection, spec_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Missing inputs of a stored spec against its pinned release, without loading datasets.

    For the API's submit-time check (``missing_input`` 422 before a job is queued):
    scalars are resolved exactly as the engine resolves them; a dataset counts as
    present when the release pins one for (concept, subject) — its bytes are verified
    by the engine at run time. Same problem shape as ``ResolvedInputs.missing_problems``.
    """
    from sqlalchemy import text

    from reva.simulation.datasets import dataset_schema_present

    model = plugins.get(spec_json["model_key"])
    release_id = UUID(str(spec_json["baseline_release_id"]))
    if is_all_countries(spec_json.get("countries")):
        sel = resolve_all_countries(conn, model, {"id": release_id}, spec_json)
        spec_json = {**spec_json, "countries": sel["resolved"]}
    reqs = model.requirements(spec_json)
    scalar_reqs = [r for r in reqs if r.kind == "scalar"]
    inputs = resolve_inputs(conn, {"id": release_id}, scalar_reqs)
    problems = inputs.missing_problems()
    has_datasets = dataset_schema_present(conn)
    for r in reqs:
        if r.kind != "dataset":
            continue
        pinned = has_datasets and conn.execute(
            text("""SELECT 1 FROM belief.release_dataset WHERE release_id = :r AND concept_id = :c
                    AND subject_key = :s"""), {"r": release_id, "c": r.concept, "s": r.subject},
        ).first() is not None
        if not pinned:
            problems.append({"code": "missing_input", "country": r.subject, "input": r.name, "concept": r.concept,
                             "kind": "dataset", "years": list(r.years), "reason": "unavailable"})
    return problems
