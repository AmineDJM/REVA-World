"""Shared plumbing of the laws & legal-systems atlas connectors (V3, agent A-LAW).

Used by :mod:`atlas_wbl`, :mod:`atlas_constitute`, :mod:`atlas_ccp`, :mod:`atlas_factbook` and
:mod:`atlas_ssa_ssptw`. Each of those modules declares ``atlas_source()`` and ``async
atlas_ingest(run)`` (ATLASCORE platform, ``reva.atlas.sources`` / ``reva.atlas.ingest``): every
request goes through ``run.get`` (budget-metered live client or fixture replay), raw bytes are
archived with ``run.archive`` and tables registered with ``run.register``. Nothing here builds an
HTTP client.

* :func:`fetch` — ``run.get`` with provider refusals turned into a typed :class:`Blocked`
  (never an empty "success"); a budget refusal is not caught (the platform types it).
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
from typing import Any, cast

from reva.kernel.errors import SourceResponseError, UnsafeRequest

AGENT = "A-LAW"
VERBATIM = "verbatim"


def transcribed(doc: str) -> str:
    """The flags.transcription marker of an agent-applied rule (never presented as verified)."""
    return f"transcribed by agent {AGENT} from {doc}, maintainer review pending"


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
    """A source item the connector could not read: typed, never a silent empty success."""

    source_key: str
    item: str
    # http_403 | http_status | response_cap | unsafe_request | transport | fixture_missing | parser_not_implemented
    reason: str
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


async def fetch(
    run: Any,
    url: str,
    *,
    key: str,
    source_key: str,
    media_types: tuple[str, ...] = ("application/json",),
    max_bytes: int | None = None,
) -> Any:
    """``run.get`` one item -> ``RawAcquisition``, or a typed :class:`Blocked` for a provider refusal,
    an oversized response, a transport failure or a missing fixture."""
    try:
        raw = await run.get(url, key=key, media_types=media_types, max_bytes=max_bytes)
    except SourceResponseError as e:
        status = e.details.get("status")
        if status == 403:
            reason = "http_403"
        elif status is not None:
            reason = "http_status"
        elif e.details.get("kind") == "fixture_missing":
            reason = "fixture_missing"
        else:
            reason = "transport"
        return Blocked(source_key, key, reason, str(e)[:500], status)
    except UnsafeRequest as e:
        reason = "response_cap" if "exceeds limit" in str(e) else "unsafe_request"
        return Blocked(source_key, key, reason, str(e)[:500], None)
    status = (raw.response_metadata or {}).get("status", 200)
    if status != 200:  # recorded fixtures replay the provider status without raising
        return Blocked(source_key, key, "http_403" if status == 403 else "http_status", f"HTTP {status}", status)
    return raw


def table_summary(out: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a ``run.register`` result worth reporting."""
    cov = out.get("coverage") or {}
    return {
        "dataset_key": out["dataset_key"],
        "artifact_sha256": out["artifact_sha256"],
        "row_count": out["row_count"],
        "created": out["created"],
        "countries": cov.get("countries"),
        "period_min": cov.get("period_min"),
        "period_max": cov.get("period_max"),
        "unmapped_provider_geo": out.get("unmapped_provider_geo", []),
    }


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
    "VERBATIM",
    "Blocked",
    "IngestReport",
    "atlas_frame",
    "canon",
    "clip_text",
    "code_digest",
    "fetch",
    "row",
    "table_summary",
    "transcribed",
]
