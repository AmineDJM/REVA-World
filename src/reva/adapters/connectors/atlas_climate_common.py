"""Shared plumbing of the V4CLIMATE connectors (climate indices, GHCN-Monthly stations, attribution studies).

* :class:`ValueRows` collects atlas rows (``ATLAS_SCHEMA``) of *reported* numbers: the provider's decimal text is
  kept as a strict decimal string, a missing value (provider sentinel) is never a row, a duplicate cell is refused.
* :func:`load_kg` loads entities and statements in bounded batches through ``reva.kg.model.load_batch`` (append
  only) after registering the namespaced properties a connector declares (``register_properties``) and the Wikidata
  properties it reuses with WDHARVEST's own definitions (``registry/kg/properties.yaml``).
* :func:`kg_ref` is the reference attached to every statement: the archived raw file, its sha256, the URL and the
  licence of the value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl

MAX_JSON_CHARS = 4000


def canon(obj: Mapping[str, Any] | None = None, **kw: Any) -> str:
    return json.dumps({**dict(obj or {}), **kw}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def decimal_text(s: str | None) -> str | None:
    """A provider number (``"-.19"``, ``"315.71"``, ``"1.2e-3"``) as a strict decimal string; ``None`` if not a
    finite number (sentinels such as ``***``/``NaN`` are handled by the callers, which drop the cell)."""
    from reva.adapters.connectors.base import dec_str

    if s is None:
        return None
    t = s.strip()
    if not t:
        return None
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return dec_str(d)


class ValueRows:
    """Atlas rows of one table; one row per (indicator, provider_geo, period)."""

    def __init__(self, source: str, dataset: str) -> None:
        self.source = source
        self.dataset = dataset
        self.rows: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str, date, date]] = set()

    def add(
        self,
        *,
        indicator: str,
        provider_geo: str,
        period: tuple[date, date],
        value: str,
        unit: str,
        raw_sha256: str,
        selector: Mapping[str, Any],
        flags: Mapping[str, Any] | str,
        iso3: str | None = None,
        entity_qid: str | None = None,
    ) -> None:
        key = (indicator, provider_geo, period[0], period[1])
        if key in self._seen:
            raise ValueError(f"duplicate atlas cell {key}")
        self._seen.add(key)
        self.rows.append(
            {
                "source": self.source,
                "dataset": self.dataset,
                "indicator": indicator,
                "concept_id": None,
                "iso3": iso3,
                "provider_geo": provider_geo,
                "entity_qid": entity_qid,
                "period_start": period[0],
                "period_end": period[1],
                "value": value,
                "value_text": None,
                "unit": unit,
                "flags": flags if isinstance(flags, str) else canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(selector),
            }
        )

    def frame(self) -> pl.DataFrame:
        from reva.atlas.tables import ATLAS_SCHEMA

        for r in self.rows:
            for col in ("flags", "selector"):
                if not 2 <= len(r[col]) <= MAX_JSON_CHARS:
                    raise ValueError(f"{col} of {r['indicator']}/{r['provider_geo']} has {len(r[col])} characters")
        cols = ATLAS_SCHEMA.names()
        return pl.DataFrame({c: [r[c] for r in self.rows] for c in cols}, schema=ATLAS_SCHEMA)


def kg_ref(
    *, file: str, url: str, raw_sha256: str, retrieved_at: datetime, licence: str, **extra: Any
) -> dict[str, Any]:
    return {
        "source_file": file,
        "url": url,
        "raw_sha256": raw_sha256,
        "retrieved_at": retrieved_at.isoformat(),
        "licence": licence,
        **extra,
    }


def statement_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


def register_props(
    engine: Any, namespaced: Mapping[str, tuple[str, str, str, str]], wikidata_pids: Sequence[str]
) -> Any:
    """Register ``{pid: (value_kind, reva_predicate, label_en, label_fr)}`` plus Wikidata pids (WDHARVEST defs)."""
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties as wd_props
    from reva.kg.model import KgProperty, register_properties

    wd = [p for p in wd_props(load_allowlist()) if p.pid in set(wikidata_pids)]
    if len(wd) != len(set(wikidata_pids)):
        raise ValueError(f"{sorted(wikidata_pids)} must all be in registry/kg/properties.yaml")
    own = [
        KgProperty(pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf)
        for p, (k, rp, le, lf) in namespaced.items()
    ]
    return register_properties(engine, [*wd, *own])


def load_kg(
    engine: Any,
    store: Any,
    *,
    entities: Sequence[Any],
    statements: Sequence[Any],
    source_key: str,
    raw_sha256s: Iterable[str],
    retrieved_at: datetime,
    origin: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Bounded ``load_batch`` calls; returns summed counters and a sample of rejections."""
    from reva.kg.model import MAX_ENTITIES_PER_BATCH, load_batch

    step = 100_000
    n = max(1, -(-len(statements) // step), -(-len(entities) // MAX_ENTITIES_PER_BATCH))
    totals: dict[str, int] = {}
    rejected: list[Any] = []
    raws = sorted(set(raw_sha256s))
    for i in range(n):
        sts = statements[i * step : (i + 1) * step]
        used = sorted({s.raw_sha256 for s in sts if s.raw_sha256}) or raws
        rep = load_batch(
            engine,
            store,
            entities=entities[i * MAX_ENTITIES_PER_BATCH : (i + 1) * MAX_ENTITIES_PER_BATCH],
            statements=sts,
            source_key=source_key,
            raw_sha256s=used,
            retrieved_at=retrieved_at,
            origin=origin,
            parameters=dict(parameters),
        ).as_dict()
        for k, v in rep.items():
            if isinstance(v, int) and not isinstance(v, bool):
                totals[k] = totals.get(k, 0) + v
        rejected += list(rep.get("rejected") or [])[:20]
    return {**totals, "batches": n, "rejected_sample": rejected[:20]}
