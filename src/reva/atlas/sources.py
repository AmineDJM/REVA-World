"""Atlas source specs and the atlas source registry (V3, ATLASCORE; lead decision 2026-10-15).

Atlas connectors are **not** ``ConnectorSpec`` templates and not ``builtin_connectors()``
(``ports/connectors.py`` is inside the digest-pinned autonomy template closure). An atlas
connector module ``reva.adapters.connectors.atlas_<name>`` declares instead:

* ``atlas_source()`` → an :class:`AtlasSourceSpec` or a list of them (only this function name
  is the protocol; module constants are ignored);
* ``async def atlas_ingest(run: reva.atlas.ingest.AtlasRun) -> dict`` — fetch through
  ``run.http`` (budget-metered live client or fixture client, never one it builds itself),
  archive raw bytes with ``run.archive(...)`` and register atlas tables with
  ``run.register(...)``. A module with several sources dispatches on ``run.spec.name``.

:func:`atlas_sources` discovers these modules by name (no shared list to edit) and imports them
lazily; a module that fails to import or declares an invalid spec is reported in
:func:`atlas_source_errors`, never silently dropped and never fatal for the others.

An :class:`AtlasSourceSpec` is duck-compatible with what ``publish_acquisition`` and the seed's
``register_connector_version`` read (name, version, template, source_key, parser_version,
dataset_ids, allowed_hosts, field_mappings, code_digest, ``model_dump``), so raw acquisitions
of atlas sources are ordinary ``evidence.acquisition`` rows of a registered connector version.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import threading
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reva.atlas.tables import DATASET_RE, SOURCE_RE

CONNECTOR_PACKAGE = "reva.adapters.connectors"
MODULE_PREFIX = "atlas_"
ATLAS_TEMPLATE: Literal["atlas_bulk"] = "atlas_bulk"
MAX_ATLAS_RESPONSE_BYTES = 1024**3  # 1 GiB per response (bulk files); daily byte budget still applies
NAME_RE = r"^atlas\.[a-z0-9][a-z0-9_.-]{0,99}$"
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


class AtlasSourceSpec(BaseModel):
    """One atlas source (a provider service whose datasets become atlas tables)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_RE, description="Connector name, e.g. atlas.imf (unique)")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    template: Literal["atlas_bulk"] = ATLAS_TEMPLATE
    source_key: str = Field(min_length=1, max_length=100, description="registry/sources/*.yaml source_key")
    atlas_source: str = Field(description="Short atlas table source key (no dots), e.g. imf")
    provider: str = Field(min_length=1, max_length=300)
    licence: str = Field(min_length=1, max_length=500)
    allowed_hosts: tuple[str, ...] = Field(min_length=1)
    datasets: tuple[str, ...] = Field(min_length=1, description="Atlas dataset keys this source produces")
    dataset_ids: tuple[str, ...] = Field(default=(), description="Provider dataset identifiers")
    cadence_seconds: int = Field(ge=3600, description="Expected refresh cadence")
    budget_key: str = Field(default="", description="Budget ledger key (default: source_key)")
    planned_requests: int = Field(ge=1, le=5000, description="Requests reserved up front for one full run")
    max_response_bytes: int = Field(52_428_800, ge=1, le=MAX_ATLAS_RESPONSE_BYTES)
    min_interval_seconds: float = Field(1.0, ge=0.2, le=60, description="Politeness: >= this between requests")
    parser_version: str = Field(min_length=1, max_length=200)
    code_digest: str = ""
    field_mappings: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""

    @field_validator("atlas_source")
    @classmethod
    def _atlas_source(cls, v: str) -> str:
        if not SOURCE_RE.match(v):
            raise ValueError("atlas_source must match [a-z0-9][a-z0-9_-]{0,23} (no dots)")
        return v

    @field_validator("datasets")
    @classmethod
    def _datasets(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [d for d in v if not DATASET_RE.match(d)]
        if bad or len(set(v)) != len(v):
            raise ValueError(f"datasets must be unique atlas dataset keys (no dots): {bad}")
        return v

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [h for h in v if not HOST_RE.match(h)]
        if bad:
            raise ValueError(f"allowed_hosts must be lower-case DNS names: {bad}")
        return v

    @model_validator(mode="after")
    def _budget(self) -> AtlasSourceSpec:
        if not self.budget_key:
            object.__setattr__(self, "budget_key", self.source_key)
        return self

    # ConnectorSpec-compatible views (publish_acquisition / register_connector_version / budgets)
    def describe(self) -> AtlasSourceSpec:
        return self

    @property
    def geography(self) -> tuple[str, ...]:
        return ("see atlas tables",)


class AtlasSourceEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    spec: AtlasSourceSpec
    module: str


_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}


def _module_names() -> list[str]:
    pkg = importlib.import_module(CONNECTOR_PACKAGE)
    return sorted(
        f"{CONNECTOR_PACKAGE}.{m.name}"
        for m in pkgutil.iter_modules(pkg.__path__)
        if m.name.startswith(MODULE_PREFIX) and not m.ispkg
    )


def _specs_of(mod: Any) -> list[AtlasSourceSpec]:
    """The module's ``atlas_source()`` result (a spec or a list of specs); other modules: none.

    Only the function name is part of the protocol: several atlas modules use a module constant
    ``ATLAS_SOURCE`` for their short table key, which is not a spec."""
    fn = getattr(mod, "atlas_source", None)
    if not callable(fn):
        return []
    got = fn()
    items = list(got) if isinstance(got, (list, tuple)) else [got]
    out = []
    for it in items:
        if not isinstance(it, AtlasSourceSpec):
            raise TypeError(f"{mod.__name__}: atlas_source() must return AtlasSourceSpec(s), got {type(it).__name__}")
        out.append(it)
    if out and not callable(getattr(mod, "atlas_ingest", None)):
        raise TypeError(f"{mod.__name__}: declares atlas sources but no atlas_ingest(run) function")
    return out


def discover(names: Iterable[str] | None = None) -> tuple[dict[str, AtlasSourceEntry], list[dict[str, str]]]:
    """Import ``atlas_*`` connector modules and collect their sources (no caching)."""
    sources: dict[str, AtlasSourceEntry] = {}
    errors: list[dict[str, str]] = []
    for name in names if names is not None else _module_names():
        try:
            mod = importlib.import_module(name)
            specs = _specs_of(mod)
        except Exception as e:  # one broken module never hides the others
            errors.append({"module": name, "error": f"{type(e).__name__}: {str(e)[:300]}"})
            continue
        for s in specs:
            if s.name in sources:
                errors.append({"module": name, "error": f"duplicate atlas source name {s.name}"})
                continue
            sources[s.name] = AtlasSourceEntry(spec=s, module=name)
    return dict(sorted(sources.items())), errors


def _cached() -> tuple[dict[str, AtlasSourceEntry], list[dict[str, str]]]:
    with _LOCK:
        if "v" not in _CACHE:
            _CACHE["v"] = discover()
        return _CACHE["v"]  # type: ignore[no-any-return]


def atlas_sources() -> dict[str, AtlasSourceEntry]:
    """Every declared atlas source by connector name (lazy discovery, cached per process)."""
    return _cached()[0]


def atlas_source_errors() -> list[dict[str, str]]:
    return _cached()[1]


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def get_source(name: str) -> AtlasSourceEntry:
    from reva.kernel.errors import ValidationProblem

    entry = atlas_sources().get(name)
    if entry is None:
        raise ValidationProblem(
            "unknown atlas source",
            source=name[:128],
            known=sorted(atlas_sources()),
            import_errors=atlas_source_errors(),
        )
    return entry


def register_atlas_connector_versions(conn: Any, store: Any, activity_id: Any) -> list[str]:
    """Register every atlas source's connector version (seed, migration-owner connection).

    Seed hook (``reva.cli.seed``, after ``builtin_connectors``): a source whose ``source_key`` is
    not registered is skipped and returned, never fatal. A source already registered under the same
    version with *different* spec/code is also skipped and returned (logged): the existing version
    row stays the truth, and ``run_atlas_ingest`` refuses that source until its version is bumped —
    one atlas module being edited never blocks seeding the rest.
    """
    import logging

    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version
    from reva.kernel.errors import ImmutableRecordViolation

    skipped = []
    for entry in atlas_sources().values():
        if repo.source_by_key(conn, entry.spec.source_key) is None:
            skipped.append(entry.spec.name)
            continue
        try:
            register_connector_version(conn, store, entry.spec, activity_id)
        except ImmutableRecordViolation:  # raised before anything is written
            logging.getLogger("reva.atlas").warning(
                "atlas connector %s@%s is registered with different content; bump its version",
                entry.spec.name,
                entry.spec.version,
            )
            skipped.append(entry.spec.name)
    return skipped


def ensure_registered(owner_engine: Any, store: Any, spec: AtlasSourceSpec) -> None:
    """Register one atlas connector version (tests / throwaway databases; owner engine)."""
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version

    with owner_engine.begin() as conn:
        act = repo.simple_activity("seed.atlas_connector", "source", "owner:atlas", {"connector": spec.name})
        repo.record_activity(conn, act)
        register_connector_version(conn, store, spec, act.id)
