"""Shared KG plumbing of the V4MIND sources (psychology, behaviour, sociology).

The V4MIND atlas modules (``atlas_mind_*``) load a few dataset entities into the KG next to their atlas tables:

* ``survey_wave`` -- a national survey round a series rests on (UN SDG 5.4.1 time-use surveys), keyed by the
  provider's own geography code, year and source text (``unsd_tus:<M49>:<year>:<hash of source text>``);
* ``occupation`` -- ISCO-08 groups as ILOSTAT publishes them in its ``classif1`` code dictionary
  (``isco08:<code>``);
* ``atlas_table`` -- the atlas table a ``measured_by`` statement points at (``atlas:<source>.<dataset>``). This key
  is REVA-made (labelled so in the entity description): it names an atlas table, not a real-world thing.

Namespaced predicates (registered with :func:`reva.kg.model.register_properties` before use; families in
``registry/kg/relations_v4.d/v4mind.yaml``): see :data:`PROPERTIES`. No predicate is causal; ``measured_by`` says
that a table holds values about the subject, nothing more.

Every statement keeps the raw artifact it was read from (``raw_sha256``) and a selector reference.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

#: pid -> (value kind, reva predicate, label en, label fr, inverse name, inverse label en)
PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "unsd_tus:survey_in": (
        "entity", "time_use_survey_in", "time-use survey conducted in", "enquête emploi du temps menée dans",
        "has_time_use_survey", "has time-use survey",
    ),
    "unsd_tus:reference_year": ("time", "reference_year", "reference year", "année de référence", None, None),
    "unsd_tus:source_text": ("string", "provider_source_text", "source (provider text)", "source (texte du fournisseur)", None, None),
    "unsd_tus:data_source_for": (
        "entity", "data_source_for", "data source for (atlas table)", "source de données pour (table de l'atlas)",
        "has_data_source", "has data source",
    ),
    "isco:part_of": (
        "entity", "isco_group_part_of", "ISCO-08 group part of", "groupe CITP-08 inclus dans",
        "isco_group_has_part", "ISCO-08 group has part",
    ),
    "isco:code": ("external_id", "isco08_code", "ISCO-08 code", "code CITP-08", None, None),
    "ilostat:measured_by": (
        "entity", "measured_by_atlas_table", "measured by (atlas table)", "mesuré par (table de l'atlas)",
        "measures", "measures",
    ),
}  # fmt: skip


def kg_properties() -> list[Any]:
    from reva.kg.model import KgProperty

    return [
        KgProperty(pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv,
                   inverse_label_en=ile)
        for p, (k, rp, le, lf, inv, ile) in PROPERTIES.items()
    ]  # fmt: skip


def short_hash(text: str, n: int = 10) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def atlas_table_entity(source: str, dataset: str, title: str) -> Any:
    """KG entity for one atlas table (REVA-made key, labelled so)."""
    from reva.kg.model import KgEntity

    return KgEntity(
        qid=f"atlas:{source}.{dataset}",
        kind="atlas_table",
        labels={"en": f"{title} (atlas table {source}.{dataset})"},
        descriptions={
            "en": "REVA atlas table (REVA-made key naming a table of provider values, not a real-world entity)",
            "fr": "Table de l'atlas REVA (clé créée par REVA, désigne une table de valeurs du fournisseur)",
        },
    )


def year_time(year: int) -> Any:
    from reva.kg.model import KgTime

    return KgTime(f"+{year:04d}-01-01T00:00:00Z", 9)


def load(
    engine: Any,
    store: Any,
    *,
    entities: Sequence[Any],
    statements: Iterable[Any],
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
) -> dict[str, Any]:
    """Register the V4MIND predicates, then load entities and statements in bounded batches."""
    from itertools import islice

    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_STATEMENTS_PER_BATCH, load_batch, register_properties

    register_properties(engine, kg_properties())
    totals: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []

    def _one(ents: list[Any], sts: list[Any]) -> None:
        used = sorted({s.raw_sha256 for s in sts if s.raw_sha256} or set(raw_sha256s))
        rep = load_batch(
            engine, store, entities=ents, statements=sts, source_key=source_key, raw_sha256s=used,
            retrieved_at=retrieved_at, origin=origin,
        )  # fmt: skip
        for k in ("entities_inserted", "entities_unchanged", "statements_inserted", "statements_unchanged",
                  "statements_new_revision", "statements_conflicting", "statements_stale"):  # fmt: skip
            totals[k] += getattr(rep, k)
        rejected.update(rep.rejected_counts)
        samples.extend({"reason": r.reason, "key": r.key} for r in rep.rejected[:5])

    ents = list(entities)
    for i in range(0, len(ents), MAX_ENTITIES_PER_BATCH):
        _one(ents[i : i + MAX_ENTITIES_PER_BATCH], [])
    it = iter(statements)
    while chunk := list(islice(it, MAX_STATEMENTS_PER_BATCH)):
        _one([], chunk)
    return {**totals, "rejected": dict(rejected), "rejected_sample": samples[:20]}


def kg_available(engine: Any) -> bool:
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one())


def country_qids() -> Mapping[str, str]:
    """ISO3 -> Wikidata QID from WDHARVEST's reviewed ``registry/entities/country_qids.json``."""
    from reva.kg.query import country_qid_registry

    return country_qid_registry()


__all__ = [
    "PROPERTIES",
    "atlas_table_entity",
    "country_qids",
    "kg_available",
    "kg_properties",
    "load",
    "short_hash",
    "year_time",
]
