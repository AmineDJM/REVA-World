"""Shared plumbing of the science & technology atlas connectors (V3, agent A-SCI).

Used by :mod:`atlas_openalex`, :mod:`atlas_nobel` and :mod:`atlas_wipo`. Three things only:

* :func:`atlas_spec` — a :class:`~reva.ports.connectors.ConnectorSpec` for an atlas bulk connector.
  ``ConnectorSpec.template`` is a closed ``Literal`` that has no atlas value yet; every other field
  is validated normally and the template is set to :data:`ATLAS_TEMPLATE` afterwards (requested
  change for the lead: add ``"atlas_bulk"`` to the ``Literal`` in ``src/reva/ports/connectors.py``;
  once it is there the spec is built fully validated, see :func:`template_supported`).
* :func:`archive_raw` — archive one raw download as an ``evidence.acquisition`` (zero observation
  batches) so ``register_atlas_table`` can cite it; returns the raw sha256.
* :func:`atlas_frame` / :func:`canon` — rows -> a polars frame in ``ATLAS_SCHEMA``.

Nothing here makes a network request.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from typing import Any, get_args

from reva.ports.connectors import ConnectorSpec, RawAcquisition

ATLAS_TEMPLATE = "atlas_bulk"
_VALID_PLACEHOLDER = "naturalearth_asset"  # only to validate the other fields; replaced before use


def template_supported() -> bool:
    return ATLAS_TEMPLATE in get_args(ConnectorSpec.model_fields["template"].annotation)


def atlas_spec(**fields: Any) -> ConnectorSpec:
    """A connector spec with ``template = "atlas_bulk"`` (every other field validated)."""
    if template_supported():
        return ConnectorSpec(template=ATLAS_TEMPLATE, **fields)  # type: ignore[arg-type]
    checked = ConnectorSpec(template=_VALID_PLACEHOLDER, **fields)  # type: ignore[arg-type]  # validates the rest
    return checked.model_copy(update={"template": ATLAS_TEMPLATE})


def code_digest(*modules: Any) -> str:
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), *modules):
        h.update(inspect.getsource(m).encode())
    return "sha256:" + h.hexdigest()


def canon(obj: Mapping[str, Any]) -> str:
    """Canonical JSON object text (flags / selector columns)."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def atlas_frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame({c: [r[c] for r in rows] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)


def archive_raw(engine: Any, store: Any, spec: ConnectorSpec, raw: RawAcquisition, *, actor: str) -> str:
    """Archive the raw bytes as an acquisition of ``spec`` (no observations); return its sha256.

    Idempotent: identical bytes for the same request are a duplicate acquisition (same sha256).
    """
    from reva.evidence.ingest import publish_acquisition

    summary = publish_acquisition(engine, store, spec, raw, [], [], actor=actor)
    if summary.namespace == "quarantine":  # pragma: no cover - no quality report is ever blocking here
        raise RuntimeError("raw acquisition was quarantined")
    return summary.raw_sha256


def register_connector(engine: Any, store: Any, spec: ConnectorSpec) -> Any:
    """Register ``spec`` as a connector version (tests / throwaway databases).

    The seed registers the built-in connectors (``reva.adapters.connectors.registry``, lead-owned);
    until the A-SCI connectors are listed there, tests and the throwaway live ingest register them
    with the seed's own function. The ``registry/sources/<source>.yaml`` entry must be seeded.
    """
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version

    with engine.begin() as conn:
        act = repo.simple_activity("registry.seed", "review", "system:a_sci", {"connector": spec.name})
        repo.record_activity(conn, act)
        return register_connector_version(conn, store, spec, act.id)


__all__ = [
    "ATLAS_TEMPLATE",
    "archive_raw",
    "atlas_frame",
    "atlas_spec",
    "canon",
    "code_digest",
    "register_connector",
    "template_supported",
]
