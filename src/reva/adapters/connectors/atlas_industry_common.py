"""Shared helpers of the V4 industry / companies connectors (agent V4INDUSTRY).

Used by :mod:`atlas_gleif` (GLEIF LEI API), :mod:`atlas_tiva` (OECD TiVA) and :mod:`atlas_unsd_classif` (UNSD ISIC /
CPC structures and correspondence). Nothing here fetches anything or builds an HTTP client: those modules request
bytes only through the atlas run (``run.get``), archive them with ``run.archive`` and register atlas tables with
``run.register``. This module holds:

* the dataset predicates of ``registry/kg/predicates_industry.yaml`` and their registration in the KG allow-list;
* a bounded, batched KG loader (``reva.kg.model.load_batch``) with optional retraction of statements a full
  rebuild no longer emits (never for partial selections such as the GLEIF seed batches);
* identifier-only country joins: ISO 3166-1 alpha-2 -> crosswalk key (REVA crosswalk) -> Wikidata QID
  (``registry/entities/country_qids.json``). Nothing is ever matched by name.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_industry.yaml"
#: Generic project User-Agent suffix is added by the safe client; connectors never send personal data.

_PROPERTY_KEYS = (
    "pid",
    "value_kind",
    "reva_predicate",
    "label_en",
    "label_fr",
    "inverse_name",
    "inverse_label_en",
    "inverse_label_fr",
)


def code_digest(*modules: Any) -> str:
    """sha256 over this module's source and the given modules' sources."""
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


def trim_registration(summary: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in summary.items() if k != "coverage"}
    out["unmapped_provider_geo"] = list(summary.get("unmapped_provider_geo", []))[:50]
    return out


# ============================================================================================== predicates
def load_predicates(path: Path = PREDICATES_FILE, *, prefix: str | None = None) -> list[Any]:
    """``KgProperty`` objects of ``predicates_industry.yaml`` (optionally only pids starting with ``prefix``)."""
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        KgProperty(**{k: p[k] for k in _PROPERTY_KEYS if k in p})
        for p in doc["predicates"]
        if prefix is None or str(p["pid"]).startswith(prefix)
    ]


def predicate_ids(prefix: str | None = None) -> frozenset[str]:
    return frozenset(p.pid for p in load_predicates(prefix=prefix))


def ensure_kg_properties(engine: Any, prefix: str | None = None) -> dict[str, Any]:
    from reva.kg.model import register_properties

    return dict(register_properties(engine, load_predicates(prefix=prefix)).__dict__)


def current_keys(engine: Any, source_key: str, origin: str, prefix: str = "") -> set[str]:
    """Current statement keys of a source whose key starts with ``<source_key>:<prefix>``."""
    from sqlalchemy import text

    like = f"{source_key}:{prefix}".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with engine.connect() as c:
        return set(
            c.execute(
                text("""SELECT statement_key FROM kg.statement_current
                        WHERE source_key = :s AND origin = :o AND statement_key LIKE :p"""),
                {"s": source_key, "o": origin, "p": like},
            ).scalars()
        )


def load_kg(
    engine: Any,
    store: Any,
    *,
    entities: Sequence[Any],
    statements: Sequence[Any],
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    parameters: Mapping[str, Any],
    predicate_prefix: str | None = None,
    retract_prefix: str | None = None,
) -> dict[str, Any]:
    """Register the predicates, then load in bounded batches.

    ``retract_prefix`` (a statement-id prefix): statements of this source whose key starts with it and that the new
    build no longer has are retracted (reason derived by the loader). ``None`` (default): no retraction -- used for
    partial selections, where an absent statement says nothing about the source.
    Returns a typed ``blocked`` / ``failed`` dict (nothing written) on a database or validation error."""
    from sqlalchemy.exc import DBAPIError

    from reva.kernel.errors import RevaError
    from reva.kg.model import (
        MAX_ENTITIES_PER_BATCH,
        MAX_RETRACTIONS_PER_BATCH,
        MAX_STATEMENTS_PER_BATCH,
        load_batch,
        statement_key,
    )

    try:
        props = ensure_kg_properties(engine, predicate_prefix)
    except (RevaError, DBAPIError) as e:
        return {"status": "blocked", "reason": f"KG properties not registrable: {str(getattr(e, 'message', e))[:300]}"}
    retract: list[str] = []
    if retract_prefix is not None:
        new = {statement_key(s, source_key) for s in statements}
        retract = sorted(current_keys(engine, source_key, origin, retract_prefix) - new)
    n = max(
        1,
        -(-len(statements) // MAX_STATEMENTS_PER_BATCH),
        -(-len(entities) // MAX_ENTITIES_PER_BATCH),
        -(-len(retract) // MAX_RETRACTIONS_PER_BATCH),
    )
    totals: dict[str, Any] = {"batches": n, "properties": props}
    keys = (
        "entities_inserted",
        "entities_unchanged",
        "entities_conflicting",
        "entities_stale",
        "labels_inserted",
        "statements_inserted",
        "statements_new_revision",
        "statements_unchanged",
        "statements_reinstated",
        "statements_stale",
        "statements_conflicting",
        "retractions_inserted",
    )
    rejected: dict[str, int] = {}
    samples: list[Any] = []
    load_ids: list[int] = []
    try:
        for i in range(n):
            rep = load_batch(
                engine,
                store,
                entities=entities[i * MAX_ENTITIES_PER_BATCH : (i + 1) * MAX_ENTITIES_PER_BATCH],
                statements=statements[i * MAX_STATEMENTS_PER_BATCH : (i + 1) * MAX_STATEMENTS_PER_BATCH],
                source_key=source_key,
                raw_sha256s=sorted(set(raw_sha256s)),
                retrieved_at=retrieved_at,
                origin=origin,
                retract_keys=retract[i * MAX_RETRACTIONS_PER_BATCH : (i + 1) * MAX_RETRACTIONS_PER_BATCH],
                parameters=dict(parameters),
            )
            d = rep.as_dict()
            for k in keys:
                totals[k] = totals.get(k, 0) + int(d.get(k) or 0)
            for k, v in (d.get("rejected_counts") or {}).items():
                rejected[k] = rejected.get(k, 0) + int(v)
            samples.extend(d.get("rejected_sample") or [])
            if d.get("load_id") is not None:
                load_ids.append(int(d["load_id"]))
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": e.details}
    except DBAPIError as e:
        return {"status": "failed", "error": f"{type(e.orig).__name__}: {str(e.orig)[:300]}"}
    totals.update(
        status="loaded",
        entities_built=len(entities),
        statements_built=len(statements),
        retractions_requested=len(retract),
        rejected_counts=rejected,
        rejected_sample=samples[:20],
        load_ids=load_ids,
    )
    return totals


# ============================================================================================== countries
@lru_cache(maxsize=1)
def _geo() -> Any:
    from reva.atlas.tables import geo_index

    return geo_index()


def iso2_to_key(code: str | None) -> str | None:
    """ISO 3166-1 alpha-2 -> REVA crosswalk key (``None`` when the crosswalk has no such code)."""
    if not code:
        return None
    idx = _geo()
    return idx.lookup(code, idx.scheme("iso2"))  # type: ignore[no-any-return]


@lru_cache(maxsize=1)
def country_qids() -> dict[str, str]:
    """Crosswalk key -> Wikidata QID (``registry/entities/country_qids.json``; empty when absent)."""
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    return dict(load_country_qids())


def iso2_to_qid(code: str | None) -> str | None:
    key = iso2_to_key(code)
    return country_qids().get(key) if key else None


__all__ = [
    "PREDICATES_FILE",
    "atlas_frame",
    "canon",
    "code_digest",
    "country_qids",
    "ensure_kg_properties",
    "iso2_to_key",
    "iso2_to_qid",
    "load_kg",
    "load_predicates",
    "predicate_ids",
    "trim_registration",
]
