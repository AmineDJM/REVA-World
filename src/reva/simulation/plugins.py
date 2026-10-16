"""Simulation model plugins: protocol, typed resolved inputs and the model registry (V2).

A simulation model is a plugin selected by ``model_key`` in the scenario spec. The
platform (``reva.simulation.engine`` / ``compile`` / ``jobs``) owns everything that
touches the database: loading the pinned baseline release, resolving the inputs a
model declares, the L0 exact-spec cache, persistence and the job path. A plugin is
pure: it validates its part of the spec, declares its inputs, compiles resolved
inputs into a plan and computes Decimal result rows. It never reads the database,
the network or the clock.

This module must stay importable from ``reva.models`` (import-linter forbids
``reva.models`` -> sqlalchemy/adapters, transitively): standard library, kernel,
pyarrow and polars only.

Lifecycle of one run (engine side, for reference)::

    model = get(spec["model_key"])            # CapabilityGap if unknown
    model.validate(spec)                      # ValidationProblem / CapabilityGap
    reqs = model.requirements(spec)           # list[InputRequirement]
    inputs = resolve_inputs(conn, release, reqs)   # ResolvedInputs (DB side)
    compiled = model.compile(inputs, spec)    # pure
    rows = model.compute(compiled, fidelity)  # pure, Decimal -> list[ResultRow]

Missing inputs: unless the compiled object exposes its own ``missing`` list (the
plugin then owns missing-input reporting, e.g. per country), the engine refuses
with ``missing_input`` when ``inputs.missing_problems()`` is non-empty, before
``compute`` is called. A plugin never receives a silently zero-filled input.

Optional hooks (looked up with ``getattr``; defaults in brackets):

* ``spec_schema() -> dict`` — JSON Schema fragments for the model branch of
  ``contracts/scenario.schema.json`` with keys ``intervention``, ``assumptions``,
  ``outputs`` (and optionally ``horizon``, ``countries``, ``fidelity``)
  [permissive objects; ``validate`` is then the only check].
* ``explanation(fidelity) -> dict`` — human-readable explanation block for the run
  [``{"model": key@version}``].
* ``run_applicability(compiled, fidelity) -> dict`` — run-level applicability
  attached to the run outcome [``{}``].
* ``seed_subjects(spec) -> list[str]`` — entities named in the run seed plan
  [``spec["countries"]``].
* ``cost(spec) -> dict`` — extra cost counters [``{}``].
* ``implementation_files() -> tuple[Path, ...]`` — source files hashed into the
  model manifest's ``implementation_digest`` [the module file of the plugin class].
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol, runtime_checkable
from uuid import UUID

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.values import Missing, MissingReason, parse_value

if TYPE_CHECKING:  # pragma: no cover - typing only
    import polars as pl
    import pyarrow as pa

__all__ = [
    "KNOWN_MODEL_MODULES",
    "REGISTRY",
    "VERSIONS",
    "DatasetInput",
    "InputRequirement",
    "ResolvedInputs",
    "ResultRow",
    "ScalarInput",
    "SimulationModel",
    "SubjectRef",
    "get",
    "load_all",
    "register",
    "source_digest",
    "versions_of",
]

InputKind = Literal["scalar", "dataset"]


# --------------------------------------------------------------------------- requirements
class InputRequirement(NamedTuple):
    """One input a model needs, declared before any database access.

    ``kind="scalar"``: one ``selected_report`` estimate of ``concept`` for ``subject``
    per calendar year in ``years`` (support exactly that Gregorian year), read from
    the scenario's pinned baseline release.

    ``kind="dataset"``: the dataset of ``concept`` for ``subject`` pinned by the
    baseline release (``years`` names the reference years the model needs inside the
    table; the platform does not filter rows).
    """

    name: str  # model-local input name
    concept: str  # registry concept id (dataset concept for datasets)
    subject: str  # ISO3 or a registry entity key
    years: tuple[int, ...]
    kind: InputKind


# --------------------------------------------------------------------------- resolved inputs
@dataclass(frozen=True)
class SubjectRef:
    """How a requirement's ``subject`` key resolved to a world entity."""

    key: str
    entity_id: UUID
    resolution: str  # "reviewed_alias:iso3166_alpha3" | "deterministic_entity_id" | ...


@dataclass(frozen=True)
class ScalarInput:
    """A resolved scalar: a kernel value or a typed ``Missing``, never a bare zero.

    ``value_dict`` is the stored kernel value dict (``None`` when no estimate exists);
    ``value`` is its parsed kernel object (``Missing`` when absent or typed missing).
    ``estimate_id`` is the provenance id of the belief estimate the value came from.
    """

    requirement: InputRequirement
    year: int
    subject_id: UUID | None
    value: Any
    value_dict: Mapping[str, Any] | None = None
    estimate_id: str | None = None
    temporal_extent: Mapping[str, Any] | None = None
    aggregation: str | None = None
    source_flags: Mapping[str, Any] = field(default_factory=dict)
    modality: str | None = None

    @property
    def is_missing(self) -> bool:
        return self.value is None or isinstance(self.value, Missing)

    @property
    def missing_reason(self) -> str | None:
        if isinstance(self.value, Missing):
            return self.value.reason.value
        return MissingReason.unavailable.value if self.value is None else None

    def provenance(self) -> dict[str, Any]:
        return {
            "estimate_id": self.estimate_id,
            "concept": self.requirement.concept,
            "subject": self.requirement.subject,
            "year": self.year,
        }


@dataclass(frozen=True)
class DatasetInput:
    """A resolved dataset input: a read-only Arrow table loaded from the pinned artifact.

    ``table`` is a ``pyarrow.Table`` (immutable). ``frame()`` returns a Polars view of
    it; a Polars frame is a new object per call, so a model cannot alter what another
    model or a later call sees. ``manifest`` is the dataset manifest (artifact sha256,
    source acquisition ids, parser version, schema, row count). ``missing`` is set
    (and ``table`` is None) when the release pins no such dataset.
    """

    requirement: InputRequirement
    dataset_id: str | None
    artifact_sha256: str | None
    manifest: Mapping[str, Any]
    table: pa.Table | None
    missing: Missing | None = None

    @property
    def is_missing(self) -> bool:
        return self.table is None or self.missing is not None

    @property
    def missing_reason(self) -> str | None:
        if self.missing is not None:
            return self.missing.reason.value
        return MissingReason.unavailable.value if self.table is None else None

    def frame(self) -> pl.DataFrame:
        import polars as pl

        if self.table is None:
            raise ValidationProblem(f"dataset input {self.requirement.name!r} is missing")
        return pl.from_arrow(self.table)  # type: ignore[return-value]

    def provenance(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "artifact_sha256": self.artifact_sha256,
            "concept": self.requirement.concept,
            "subject": self.requirement.subject,
        }


def _freeze(d: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return MappingProxyType(dict(d))


@dataclass(frozen=True)
class ResolvedInputs:
    """Inputs resolved from the scenario's pinned baseline release (and nothing else).

    Keys: scalars by ``(name, subject, year)``, datasets by ``(name, subject)``,
    subjects by subject key. ``release`` is the pinned release row (id, manifest,
    knowledge cutoff, ...). The mappings are read-only.
    """

    release: Mapping[str, Any]
    requirements: tuple[InputRequirement, ...]
    subjects: Mapping[str, SubjectRef]
    scalars: Mapping[tuple[str, str, int], ScalarInput]
    datasets: Mapping[tuple[str, str], DatasetInput]

    def __post_init__(self) -> None:
        object.__setattr__(self, "subjects", _freeze(self.subjects))
        object.__setattr__(self, "scalars", _freeze(self.scalars))
        object.__setattr__(self, "datasets", _freeze(self.datasets))
        object.__setattr__(self, "release", _freeze(self.release))
        object.__setattr__(self, "requirements", tuple(self.requirements))

    # -- accessors ------------------------------------------------------------------
    def scalar(self, name: str, subject: str, year: int) -> ScalarInput:
        try:
            return self.scalars[(name, subject, year)]
        except KeyError as e:
            raise ValidationProblem(f"input {name!r} for {subject} {year} was not declared by the model") from e

    def dataset(self, name: str, subject: str) -> DatasetInput:
        try:
            return self.datasets[(name, subject)]
        except KeyError as e:
            raise ValidationProblem(f"dataset input {name!r} for {subject} was not declared by the model") from e

    def subject(self, key: str) -> SubjectRef:
        try:
            return self.subjects[key]
        except KeyError as e:
            raise ValidationProblem(f"subject {key!r} was not declared by the model") from e

    # -- missing inputs -------------------------------------------------------------
    def missing_problems(self, subjects: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Typed ``missing_input`` problems in requirement order (optionally for some subjects).

        Same shape as the crude model's per-country problems: ``code``, ``country``
        (the subject key), ``input``, ``concept``, ``reference_year`` (or ``years`` for
        a dataset) and ``reason``.
        """
        wanted = None if subjects is None else set(subjects)
        out: list[dict[str, Any]] = []
        for req in self.requirements:
            if wanted is not None and req.subject not in wanted:
                continue
            if req.kind == "scalar":
                for y in req.years:
                    s = self.scalars.get((req.name, req.subject, y))
                    if s is None or s.is_missing:
                        out.append(
                            {
                                "code": "missing_input",
                                "country": req.subject,
                                "input": req.name,
                                "concept": req.concept,
                                "reference_year": y,
                                "reason": s.missing_reason if s else MissingReason.unavailable.value,
                            }
                        )
            else:
                d = self.datasets.get((req.name, req.subject))
                if d is None or d.is_missing:
                    out.append(
                        {
                            "code": "missing_input",
                            "country": req.subject,
                            "input": req.name,
                            "concept": req.concept,
                            "kind": "dataset",
                            "years": list(req.years),
                            "reason": d.missing_reason if d else MissingReason.unavailable.value,
                        }
                    )
        return out

    def provenance(self) -> dict[str, Any]:
        """Provenance ids of every resolved input (for run manifests)."""
        return {
            "release_id": str(self.release.get("id")),
            "scalars": [
                {"name": k[0], "subject": k[1], "year": k[2], **v.provenance()} for k, v in sorted(self.scalars.items())
            ],
            "datasets": [{"name": k[0], "subject": k[1], **v.provenance()} for k, v in sorted(self.datasets.items())],
        }

    def digest(self) -> str:
        return logical_hash(self.provenance())

    # -- pure construction (tests, model agents) -------------------------------------
    @classmethod
    def from_values(
        cls,
        requirements: Iterable[InputRequirement],
        *,
        scalars: Mapping[tuple[str, str, int], Mapping[str, Any] | None] | None = None,
        datasets: Mapping[tuple[str, str], tuple[pa.Table, Mapping[str, Any]] | None] | None = None,
        subject_ids: Mapping[str, UUID] | None = None,
        release: Mapping[str, Any] | None = None,
        aggregation: str = "period_flow_total",
    ) -> ResolvedInputs:
        """Build resolved inputs without a database (unit tests and model development).

        ``scalars`` maps ``(name, subject, year)`` to a kernel value dict (``None`` or a
        ``{"kind": "missing", ...}`` dict = missing); ``datasets`` maps ``(name, subject)``
        to ``(arrow_table, manifest)`` or ``None`` (missing). Anything declared but not
        supplied is missing — never zero.
        """
        from reva.kernel.ids import external_id

        reqs = tuple(requirements)
        scalars = dict(scalars or {})
        datasets = dict(datasets or {})
        ids = dict(subject_ids or {})
        subjects = {
            r.subject: SubjectRef(r.subject, ids.get(r.subject) or external_id("reva.entity", r.subject), "fixture")
            for r in reqs
        }
        sc: dict[tuple[str, str, int], ScalarInput] = {}
        ds: dict[tuple[str, str], DatasetInput] = {}
        for r in reqs:
            if r.kind == "scalar":
                for y in r.years:
                    vd = scalars.get((r.name, r.subject, y))
                    value = parse_value(dict(vd)) if vd is not None else None
                    sc[(r.name, r.subject, y)] = ScalarInput(
                        r,
                        y,
                        subjects[r.subject].entity_id,
                        value,
                        vd,
                        None,
                        {"kind": "interval", "precision": "year", "start": f"{y}-01-01", "end": f"{y + 1}-01-01"},
                        aggregation,
                        {},
                        "selected_report",
                    )
            else:
                got = datasets.get((r.name, r.subject))
                if got is None:
                    ds[(r.name, r.subject)] = DatasetInput(
                        r, None, None, {}, None, Missing(reason=MissingReason.unavailable)
                    )
                else:
                    table, manifest = got
                    ds[(r.name, r.subject)] = DatasetInput(
                        r, manifest.get("dataset_id"), manifest.get("artifact_sha256"), dict(manifest), table
                    )
        return cls(release=dict(release or {"id": None}), requirements=reqs, subjects=subjects, scalars=sc, datasets=ds)


# --------------------------------------------------------------------------- results
@dataclass
class ResultRow:
    """One published result value. ``period`` is a half-open [start, end) UTC-date pair
    for multi-period models and ``None`` for single-period models (stored as NULLs)."""

    output_key: str
    iso3: str
    subject_id: UUID
    variant: str
    value: dict[str, Any]
    uncertainty: dict[str, Any]
    applicability: dict[str, Any]
    period: tuple[date, date] | None = None

    def __post_init__(self) -> None:
        if self.period is not None:
            start, end = self.period
            if not (isinstance(start, date) and isinstance(end, date)) or not start < end:
                raise ValidationProblem("result period must be a half-open (start, end) date pair with start < end")

    def digest_view(self) -> dict[str, Any]:
        view: dict[str, Any] = {
            "output_key": self.output_key,
            "subject_id": str(self.subject_id),
            "variant": self.variant,
            "value": self.value,
        }
        if self.period is not None:  # single-period digests are unchanged from V1
            view["period"] = [self.period[0].isoformat(), self.period[1].isoformat()]
        return view

    def sort_key(self) -> tuple[str, str, str, str]:
        p = "" if self.period is None else self.period[0].isoformat()
        return (self.output_key, str(self.subject_id), self.variant, p)


# --------------------------------------------------------------------------- protocol
@runtime_checkable
class SimulationModel(Protocol):
    """A simulation model plugin (see module docstring for optional hooks)."""

    model_key: str  # "economic.crude_exposure"
    model_version: str  # "1.0.0"
    fidelities: frozenset[str]  # computed fidelities, e.g. {"L1", "L2"}; L0 is the platform cache

    def validate(self, spec: dict[str, Any]) -> None: ...  # raises ValidationProblem / CapabilityGap

    def requirements(self, spec: dict[str, Any]) -> list[InputRequirement]: ...

    def compile(self, inputs: ResolvedInputs, spec: dict[str, Any]) -> Any: ...  # pure, no DB

    def compute(self, compiled: Any, fidelity: str) -> list[ResultRow]: ...  # pure, Decimal

    def manifest(self) -> dict[str, Any]: ...  # implementation digest, assumptions, review_status


def source_digest(*paths: Path) -> str:
    """sha256 over (file name, bytes) of the given sources: any change invalidates L0 reuse."""
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode())
        h.update(Path(p).read_bytes())
    return f"sha256:{h.hexdigest()}"


# --------------------------------------------------------------------------- registry
REGISTRY: dict[str, SimulationModel] = {}
#: Every registered version per model key (V3 SIMWORLD). ``REGISTRY[key]`` stays the *default*
#: version: the first one registered (e.g. ``demography.cohort_component@1.0.0``), so every
#: consumer that looks a model up by key alone behaves exactly as before. Additional versions
#: (``@1.1.0``) are reachable only by naming the version (``get(key, version)``): a spec that
#: names ``model_version`` "1.0.0" always runs the 1.0.0 plugin.
VERSIONS: dict[str, dict[str, SimulationModel]] = {}

#: Modules imported by ``load_all()``; each calls ``register(...)`` at import time.
#: A module that does not exist yet is skipped (its model is then simply unknown).
KNOWN_MODEL_MODULES: tuple[str, ...] = (
    "reva.simulation.crude_plugin",
    "reva.models.economic.io_price",
    "reva.models.population.cohort_component",
    "reva.models.social.retirement_age",
    # V3 SIMWORLD: second registered versions (1.1.0) of the two models above (every country)
    "reva.models.population.cohort_world",
    "reva.models.social.retirement_world",
)

_REQUIRED_ATTRS = (
    "model_key",
    "model_version",
    "fidelities",
    "validate",
    "requirements",
    "compile",
    "compute",
    "manifest",
)


def register(model: SimulationModel, *, replace: bool = False) -> SimulationModel:
    """Register a plugin under its ``model_key`` and ``model_version``. Re-registering the same
    object is a no-op; a different object for a taken (key, version) is an error unless
    ``replace=True`` (tests). The first version registered for a key is its default
    (``REGISTRY[key]``); later versions are reachable with ``get(key, version)`` only."""
    missing = [a for a in _REQUIRED_ATTRS if not hasattr(model, a)]
    if missing:
        raise TypeError(f"{type(model).__name__} is not a SimulationModel (missing {missing})")
    key = model.model_key
    if not isinstance(key, str) or not key or "@" in key:
        raise ValueError(f"invalid model_key {key!r}")
    bad = set(model.fidelities) - {"L1", "L2", "L3", "L4"}
    if bad:
        raise ValueError(f"{key}: unknown fidelities {sorted(bad)} (L0 is the platform cache, never a model)")
    version = model.model_version
    versions = VERSIONS.setdefault(key, {})
    prior = versions.get(version)
    if prior is not None and prior is not model and not replace:
        raise ValueError(f"model {key}@{version} is already registered by {type(prior).__name__}")
    default = REGISTRY.get(key)
    if default is not None and default is not model and default.model_version == version and not replace:
        raise ValueError(f"model_key {key!r} is already registered by {type(default).__name__}")
    versions[version] = model
    if default is None or replace:  # another version of a registered key never displaces the default
        REGISTRY[key] = model
    return model


def unregister(model_key: str) -> None:
    """Remove a plugin and all its versions (tests only)."""
    REGISTRY.pop(model_key, None)
    VERSIONS.pop(model_key, None)


def versions_of(model_key: str) -> dict[str, SimulationModel]:
    """Registered versions of ``model_key`` (``{}`` when unknown); loads the known modules first."""
    if model_key not in VERSIONS:
        load_all()
    return dict(VERSIONS.get(model_key, {}))


def load_all() -> dict[str, SimulationModel]:
    """Import every known model module (idempotent) and return the registry."""
    for mod in KNOWN_MODEL_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as e:
            # Only "this plugin module does not exist (yet)"; a broken import inside an
            # existing plugin propagates.
            if e.name is None or not (mod == e.name or mod.startswith(e.name + ".")):
                raise
    return REGISTRY


def get(model_key: str, model_version: str | None = None) -> SimulationModel:
    """The plugin for ``model_key`` (its default version, or exactly ``model_version`` when
    given) or a ``CapabilityGap`` (never a fallback model or a fallback version)."""
    if model_key not in REGISTRY:
        load_all()
    model = REGISTRY.get(model_key)
    if model is None:
        raise CapabilityGap(
            f"no simulation model is registered for model_key {model_key!r}",
            capability=f"model.{model_key}",
            available=sorted(REGISTRY),
        )
    if model_version is None or model_version == model.model_version:
        return model
    other = VERSIONS.get(model_key, {}).get(model_version)
    if other is None:
        raise CapabilityGap(
            f"model {model_key}@{model_version} is not available",
            capability=f"model.{model_key}",
            available_versions=sorted(VERSIONS.get(model_key, {})),
        )
    return other


def optional_hook(model: SimulationModel, name: str) -> Any:
    """An optional plugin hook or None."""
    hook = getattr(model, name, None)
    return hook if callable(hook) else None
