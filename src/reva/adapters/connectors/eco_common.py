"""Shared helpers of the V4ECO ecology connectors (``atlas_gbif_occ``, ``atlas_globi``, ``atlas_ecoregions``).

Not an atlas module (no ``atlas_`` prefix): it declares no source. It holds

* the dataset predicates of ``registry/kg/predicates_eco.yaml`` and their registration in the KG allow-list;
* the *fill-missing* entity policy: V4BIO owns taxon entities (``gbif:<taxonKey>`` and the other taxonomies'
  ids). An ecology connector writes a minimal entity snapshot (scientific name, rank) only for an id the KG has no
  entity for yet in that origin, so it never overwrites a richer snapshot with its own and never flip-flops
  revisions with another loader;
* a guarded ``load_batch`` call that returns a typed ``blocked`` / ``failed`` result instead of raising (the atlas
  table of the same run stays registered);
* GloBI / GBIF identifier normalisation into KG node ids (``COL:4CGXP`` -> ``col:4CGXP``; ``WD:Q140`` -> ``Q140``).
  Identifiers are only re-spelled, never matched by name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PREDICATES_FILE = Path(__file__).resolve().parents[4] / "registry" / "kg" / "predicates_eco.yaml"

#: KG node id pattern (reva.kg.model.QID_RE).
_NODE_RE = re.compile(r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$")
_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{1,31}):(.+)$")
_WD_RE = re.compile(r"^Q[1-9][0-9]{0,11}$")


def canon(obj: Mapping[str, Any]) -> str:
    """Canonical compact JSON (sorted keys) for atlas ``flags`` / ``selector`` cells."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def load_predicates(pids: Iterable[str] | None = None, path: Path = PREDICATES_FILE) -> list[Any]:
    """``KgProperty`` records of ``predicates_eco.yaml`` (all, or the ``pids`` subset; unknown pid -> KeyError)."""
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = (
        "pid",
        "value_kind",
        "reva_predicate",
        "label_en",
        "label_fr",
        "inverse_name",
        "inverse_label_en",
        "inverse_label_fr",
    )
    by_pid = {p["pid"]: KgProperty(**{k: p[k] for k in keys if k in p}) for p in doc["predicates"]}
    if pids is None:
        return list(by_pid.values())
    return [by_pid[p] for p in pids]


def taxon_node(external_id: str | None) -> str | None:
    """KG node id for a taxon identifier as GloBI / GBIF publish it, or ``None`` when it cannot be one.

    ``GBIF:5219404`` -> ``gbif:5219404``; ``COL:4CGXP`` -> ``col:4CGXP``; ``NCBI:9606`` -> ``ncbi:9606``;
    ``INAT_TAXON:41964`` -> ``inat_taxon:41964``; ``WD:Q140`` -> ``Q140`` (a Wikidata item is the KG's own id).
    ``no:match``, single-letter prefixes, URLs and ids with characters outside the KG pattern give ``None``
    (counted and reported by the caller, never repaired by guessing)."""
    if not external_id:
        return None
    s = external_id.strip()
    m = _PREFIX_RE.match(s)
    if not m:
        return None
    prefix, local = m.group(1), m.group(2).strip()
    if prefix.lower() in ("no", "http", "https", "urn"):
        return None
    if prefix.upper() == "WD":
        return local if _WD_RE.match(local) else None
    node = f"{prefix.lower()}:{local}"
    return node if _NODE_RE.match(node) else None


def existing_entities(engine: Any, qids: Iterable[str], origin: str) -> set[str]:
    """The subset of ``qids`` that already has a ``kg.entity`` row in ``origin`` (any source)."""
    from sqlalchemy import text

    ids = sorted(set(qids))
    if not ids:
        return set()
    out: set[str] = set()
    with engine.connect() as c:
        for i in range(0, len(ids), 5000):
            rows = c.execute(
                text("SELECT DISTINCT qid FROM kg.entity WHERE origin = :o AND qid = ANY(:q)"),
                {"o": origin, "q": ids[i : i + 5000]},
            ).scalars()
            out.update(str(r) for r in rows)
    return out


def guarded_load(
    engine: Any,
    store: Any,
    *,
    pids: Sequence[str],
    entities: Sequence[Any],
    statements: Sequence[Any],
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    parameters: Mapping[str, Any],
    fill_missing_only: bool = True,
) -> dict[str, Any]:
    """Register ``pids`` and ``load_batch`` the entities/statements; typed result, never raises on DB errors.

    ``fill_missing_only``: entities whose id already has a snapshot in ``origin`` are not written (see module doc).
    """
    from sqlalchemy.exc import DBAPIError

    from reva.kernel.errors import RevaError
    from reva.kg.model import load_batch, register_properties

    try:
        register_properties(engine, load_predicates(pids))
    except (RevaError, DBAPIError, KeyError) as e:
        return {"status": "blocked", "reason": f"KG properties not registrable: {str(getattr(e, 'message', e))[:300]}"}
    ents = list(entities)
    skipped = 0
    try:
        if fill_missing_only and ents:
            have = existing_entities(engine, (e.qid for e in ents), origin)
            skipped = sum(1 for e in ents if e.qid in have)
            ents = [e for e in ents if e.qid not in have]
        rep = load_batch(
            engine,
            store,
            entities=ents,
            statements=list(statements),
            source_key=source_key,
            raw_sha256s=list(raw_sha256s),
            retrieved_at=retrieved_at,
            origin=origin,
            parameters=dict(parameters),
        )
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": e.details}
    except DBAPIError as e:
        return {"status": "failed", "error": f"{type(e.orig).__name__}: {str(e.orig)[:300]}"}
    d = rep.as_dict()
    d.update(
        status="loaded",
        statements_built=len(statements),
        entities_built=len(entities),
        entities_skipped_existing=skipped,
    )
    return d


def country_node(iso3: str, qids: Mapping[str, str]) -> str:
    """Country node: the reviewed Wikidata QID (registry/entities/country_qids.json) or ``iso3:<ISO3>``."""
    q = qids.get(iso3)
    return q if q else f"iso3:{iso3}"


__all__ = [
    "PREDICATES_FILE",
    "canon",
    "country_node",
    "existing_entities",
    "guarded_load",
    "load_predicates",
    "taxon_node",
]
