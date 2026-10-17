"""Shared helpers of the V6 politics layer (agent V6POLITICS): KG predicates, bounded KG loading, latest
registered atlas input tables (for the derived change tables) and small parsing helpers.

Not an atlas source module (no ``atlas_`` prefix): the sources are ``atlas_parlgov_kg``, ``atlas_nelda``,
``atlas_oxcgrt``, ``atlas_whogov`` and ``atlas_politics_derived``. Report: ``docs/dev/v6_politics.md``.
"""

from __future__ import annotations

import hashlib
import inspect
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.errors import SourceResponseError

REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_politics.yaml"
_PROPERTY_KEYS = ("pid", "value_kind", "reva_predicate", "label_en", "label_fr")
#: Wikidata properties the politics layer states (WDHARVEST allow-list definitions are reused unchanged).
WIKIDATA_PIDS = ("P17", "P580", "P582", "P585")


class PoliticsParseError(SourceResponseError):
    pass


def err(msg: str, kind: str = "schema_drift") -> PoliticsParseError:
    return PoliticsParseError(msg, retryable=False, kind=kind)


def day_period(d: date) -> tuple[date, date]:
    return d, d + timedelta(days=1)


def code_digest(*objs: Any) -> str:
    h = hashlib.sha256()
    for o in objs:
        h.update(inspect.getsource(o).encode())
    h.update(PREDICATES_FILE.read_bytes())
    return "sha256:" + h.hexdigest()


# ----------------------------------------------------------------------------- predicates
def load_predicates(prefix: str | None = None) -> list[Any]:
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(PREDICATES_FILE.read_text(encoding="utf-8"))
    return [
        KgProperty(**{k: p[k] for k in _PROPERTY_KEYS if k in p})
        for p in doc["predicates"]
        if prefix is None or str(p["pid"]).startswith(prefix)
    ]


def kg_properties(prefix: str) -> list[Any]:
    """The namespaced predicates starting with ``prefix`` plus the Wikidata P17/P580/P582/P585 definitions."""
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties as wd_props

    wd = [p for p in wd_props(load_allowlist()) if p.pid in WIKIDATA_PIDS]
    if len(wd) != len(WIKIDATA_PIDS):
        raise ValueError(f"{WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    return [*wd, *load_predicates(prefix)]


def kg_available(engine: Any) -> bool:
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one())


def load_kg(
    engine: Any,
    store: Any,
    entities: Sequence[Any],
    statements: Iterable[Any],
    *,
    properties: Sequence[Any],
    source_key: str,
    source_revision: str,
    raw_sha256s: Sequence[str],
    retrieved_at: Any,
    origin: str,
) -> dict[str, Any]:
    """Register ``properties`` and load the neutral records (``atlas_geopol_common.KgEntity`` /
    ``KgStatement``) in bounded batches, entities first."""
    from itertools import islice

    from reva.adapters.connectors.atlas_geopol_common import to_kg_model
    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_STATEMENTS_PER_BATCH, load_batch, register_properties

    register_properties(engine, list(properties))
    totals: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    samples: list[tuple[str, str | None]] = []
    batches = 0

    def _load(be: list[Any], bs: list[Any]) -> None:
        nonlocal batches
        used = sorted({s.raw_sha256 for s in bs if s.raw_sha256} or set(raw_sha256s))
        rep = load_batch(
            engine, store, entities=be, statements=bs, source_key=source_key, raw_sha256s=used,
            retrieved_at=retrieved_at, origin=origin,
        )  # fmt: skip
        batches += 1
        for k in ("entities_inserted", "entities_unchanged", "statements_inserted", "statements_unchanged",
                  "statements_new_revision", "statements_conflicting", "statements_stale"):  # fmt: skip
            totals[k] += getattr(rep, k)
        rejected.update(rep.rejected_counts)
        samples.extend((r.reason, r.key) for r in rep.rejected[:5])

    ents, _ = to_kg_model(entities, (), source_key=source_key, source_revision=source_revision)
    for i in range(0, len(ents), MAX_ENTITIES_PER_BATCH):
        _load(ents[i : i + MAX_ENTITIES_PER_BATCH], [])
    it = iter(statements)
    while chunk := list(islice(it, min(MAX_STATEMENTS_PER_BATCH, 100_000))):
        _, sts = to_kg_model((), chunk, source_key=source_key, source_revision=source_revision)
        _load([], sts)
    return {**totals, "rejected": dict(rejected), "rejected_sample": samples[:20], "batches": batches}


# ----------------------------------------------------------------------------- derived inputs
def latest_atlas_table(engine: Any, dataset_key: str, origin: str) -> dict[str, Any] | None:
    """The latest registration of an atlas table (``atlas.<source>.<dataset>``) with ``origin``."""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = (
            conn.execute(
                text("""SELECT id::text AS id, artifact_sha256, manifest_sha256, row_count, recorded_at
                        FROM evidence.dataset WHERE dataset_key = :k AND origin = :o
                        ORDER BY recorded_at DESC, id DESC LIMIT 1"""),
                {"k": dataset_key, "o": origin},
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    return {**dict(row), "recorded_at": row["recorded_at"].isoformat(), "dataset_key": dataset_key}


def iso3_qids() -> Mapping[str, str]:
    from reva.adapters.connectors.atlas_geopol_common import load_qid_for_iso3

    return load_qid_for_iso3()


__all__ = [
    "PREDICATES_FILE",
    "PoliticsParseError",
    "code_digest",
    "day_period",
    "err",
    "iso3_qids",
    "kg_available",
    "kg_properties",
    "latest_atlas_table",
    "load_kg",
    "load_predicates",
]
