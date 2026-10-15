"""Shared plumbing of the laws & legal-systems atlas connectors (V3, agent A-LAW).

Used by :mod:`atlas_wbl`, :mod:`atlas_constitute`, :mod:`atlas_ccp`, :mod:`atlas_factbook` and
:mod:`atlas_ssa_ssptw`. Nothing here makes a network request on its own.

* :func:`law_spec` — a :class:`~reva.ports.connectors.ConnectorSpec` for an atlas bulk connector.
  ``ConnectorSpec.template`` is a closed ``Literal`` without an atlas value; every other field is
  validated normally and the template is then set to ``"atlas_bulk"`` (same approach as agent
  A-SCI; requested lead change: add ``"atlas_bulk"`` to the Literal).
* :func:`fetch_raw` — one GET through the caller's client (``SafeHttpClient`` live, ``FixtureHttp``
  in tests) into a :class:`RawAcquisition`. Provider refusals become a typed :class:`Blocked`.
* :func:`archive_raw` — archive raw bytes as an ``evidence.acquisition`` (zero observation batches)
  so ``register_atlas_table`` can cite them.
* :func:`row` / :func:`atlas_frame` — rows in the ATLASCORE long schema.

Provenance conventions of every A-LAW atlas cell:

* ``selector`` locates the cell in the archived raw artifact (file/sheet/row/column, or JSON path);
* ``flags.transcription`` says how the value got there: ``"verbatim"`` (provider cell copied as is) or
  ``"transcribed by agent A-LAW from <doc>, maintainer review pending"`` when the agent applied a
  rule (code -> label, regex on text); such cells keep the provider's original text in flags.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast, get_args

from reva.adapters.connectors.base import to_raw
from reva.kernel.errors import SourceResponseError, UnsafeRequest
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

ATLAS_TEMPLATE = "atlas_bulk"
_VALID_PLACEHOLDER = "naturalearth_asset"  # only to validate the other fields; replaced before use
AGENT = "A-LAW"
VERBATIM = "verbatim"


def transcribed(doc: str) -> str:
    """The flags.transcription marker of an agent-applied rule (never presented as verified)."""
    return f"transcribed by agent {AGENT} from {doc}, maintainer review pending"


def template_supported() -> bool:
    return ATLAS_TEMPLATE in get_args(ConnectorSpec.model_fields["template"].annotation)


def law_spec(**fields: Any) -> ConnectorSpec:
    """A connector spec with ``template = "atlas_bulk"`` (every other field validated)."""
    if template_supported():
        return ConnectorSpec(template=cast(Any, ATLAS_TEMPLATE), **fields)
    checked = ConnectorSpec(template=cast(Any, _VALID_PLACEHOLDER), **fields)
    return checked.model_copy(update={"template": ATLAS_TEMPLATE})


def code_digest(*modules: Any) -> str:
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), *modules):
        h.update(inspect.getsource(cast(Any, m)).encode())
    return "sha256:" + h.hexdigest()


def canon(obj: Mapping[str, Any]) -> str:
    """Canonical JSON object text (flags / selector columns)."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


# ----------------------------------------------------------------------------- typed outcomes


@dataclass(frozen=True)
class Blocked:
    """A source the connector could not read: typed, never a silent empty success."""

    source_key: str
    item: str
    reason: str  # http_403 | http_status | response_cap | unsafe_request | transport | fixture_missing | policy
    detail: str
    http_status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "blocked",
            "source_key": self.source_key,
            "item": self.item,
            "reason": self.reason,
            "detail": self.detail,
            "http_status": self.http_status,
        }


@dataclass
class IngestReport:
    """What one connector run produced (one entry per atlas table) and what it could not read."""

    connector: str
    tables: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    raw: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.tables) and not self.blocked

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "status": "ok" if self.ok else ("partial" if self.tables else "blocked"),
            "tables": self.tables,
            "blocked": [b.as_dict() for b in self.blocked],
            "raw": self.raw,
        }


async def fetch_raw(http: Any, item: FetchItem, source_key: str) -> RawAcquisition | Blocked:
    """GET one item; a provider refusal or transport failure is a typed :class:`Blocked`."""
    try:
        result = await http.get(item.url, max_bytes=item.max_bytes)
    except SourceResponseError as e:
        status = e.details.get("status")
        kind = e.details.get("kind")
        if status == 403:
            reason = "http_403"
        elif status is not None:
            reason = "http_status"
        elif kind == "fixture_missing":
            reason = "fixture_missing"
        else:
            reason = "transport"
        return Blocked(source_key, item.key, reason, str(e)[:500], status)
    except UnsafeRequest as e:
        reason = "response_cap" if "exceeds limit" in str(e) else "unsafe_request"
        return Blocked(source_key, item.key, reason, str(e)[:500], None)
    if result.status != 200:  # recorded fixtures replay the provider status without raising
        reason = "http_403" if result.status == 403 else "http_status"
        return Blocked(source_key, item.key, reason, f"HTTP {result.status}", result.status)
    return to_raw(item, result, getattr(http, "origin", "live"))


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
    until the A-LAW connectors are listed there, tests and the throwaway live ingest register them
    with the seed's own function. The ``registry/sources/<source>.yaml`` entry must be seeded.
    """
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version

    with engine.begin() as conn:
        act = repo.simple_activity("registry.seed", "review", "system:a_law", {"connector": spec.name})
        repo.record_activity(conn, act)
        return register_connector_version(conn, store, spec, act.id)


# ----------------------------------------------------------------------------- rows


def row(
    *,
    source: str,
    dataset: str,
    indicator: str,
    provider_geo: str,
    period: tuple[date, date],
    raw_sha256: str,
    selector: Mapping[str, Any],
    flags: Mapping[str, Any],
    unit: str,
    value: str | None = None,
    value_text: str | None = None,
    concept_id: str | None = None,
    iso3: str | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "dataset": dataset,
        "indicator": indicator,
        "concept_id": concept_id,
        "iso3": iso3,
        "provider_geo": provider_geo,
        "entity_qid": None,
        "period_start": period[0],
        "period_end": period[1],
        "value": value,
        "value_text": value_text,
        "unit": unit,
        "flags": canon(flags),
        "raw_sha256": raw_sha256,
        "selector": canon(selector),
    }


def atlas_frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame({c: [r[c] for r in rows] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)


def clip_text(text: str, limit: int = 3000) -> tuple[str, bool]:
    """Text bounded for a flags/value_text cell (atlas MAX_TEXT is 4000 with the JSON around it)."""
    clean = "".join(" " if (ch < " " or ch == "\x7f") else ch for ch in text)
    if len(clean) <= limit:
        return clean, False
    return clean[:limit], True


__all__ = [
    "AGENT",
    "ATLAS_TEMPLATE",
    "VERBATIM",
    "Blocked",
    "IngestReport",
    "archive_raw",
    "atlas_frame",
    "canon",
    "clip_text",
    "code_digest",
    "fetch_raw",
    "law_spec",
    "register_connector",
    "row",
    "template_supported",
    "transcribed",
]
