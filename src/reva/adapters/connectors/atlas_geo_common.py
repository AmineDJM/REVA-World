"""Shared helpers for the A-GEO atlas connectors (Natural Earth 1:10m, GeoNames, CCKP climate, INFORM).

* Each A-GEO module declares ``atlas_source() -> AtlasSourceSpec`` and ``async atlas_ingest(run)`` (single
  atlas ingest entry point, ``reva.atlas.ingest``); fetching goes through ``run.get`` (:func:`run_get`), raw
  bytes are archived with ``run.archive`` *before* any atlas table or KG statement is built from them.
* :func:`make_run` wraps an explicit client (tests) in an ``AtlasRun``; :func:`register_connector`
  registers a connector version in a test/throwaway database (``reva.atlas.sources.ensure_registered``).
* Values are emitted as strict decimal strings; computed values are rounded explicitly (:func:`dec`).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from reva.adapters.connectors.base import dec_str, to_raw
from reva.ports.connectors import FetchItem, RawAcquisition


async def fetch_raw(item: FetchItem, http: Any) -> RawAcquisition:
    result = await http.get(item.url, max_bytes=item.max_bytes)
    return to_raw(item, result, getattr(http, "origin", "live"))


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dec(x: float | Decimal | int, places: int) -> str:
    """Strict decimal string of a computed value rounded to ``places`` decimals (half-even)."""
    d = Decimal(str(x)) if not isinstance(x, Decimal) else x
    q = Decimal(1).scaleb(-places)
    return dec_str(d.quantize(q, rounding=ROUND_HALF_EVEN))


def snapshot_period(retrieved_at: datetime) -> tuple[date, date]:
    """Half-open one-day period of a retrieval snapshot (sources that do not date their figures)."""
    d = retrieved_at.date()
    return d, d + timedelta(days=1)


def make_run(spec: Any, engine: Any, store: Any, http: Any) -> Any:
    """An ``AtlasRun`` around an explicit client (tests / ``FixtureHttp``); live runs go through
    ``reva.atlas.ingest.run_atlas_ingest`` (budget-metered client)."""
    from reva.atlas.ingest import AtlasRun

    return AtlasRun(
        spec=spec,
        http=http,
        engine=engine,
        store=store,
        settings=None,
        origin=getattr(http, "origin", "live"),
        actor="worker:atlas_a_geo",
    )


async def run_get(run: Any, item: FetchItem) -> RawAcquisition:
    """Fetch one planned item through the run's client (never a client of our own)."""
    raw: RawAcquisition = await run.get(
        item.url, key=item.key, media_types=item.expected_media_types, max_bytes=item.max_bytes
    )
    return raw


def register_connector(owner_engine: Any, store: Any, spec: Any) -> None:
    """Register an atlas connector version in a test/throwaway database (owner engine)."""
    from reva.atlas.sources import ensure_registered

    ensure_registered(owner_engine, store, spec)


def load_country_qids(path: Any = None) -> dict[str, str]:
    """ISO3 (crosswalk key) -> Wikidata QID from ``registry/entities/country_qids.json`` (WDHARVEST).

    Returns ``{}`` when the file does not exist yet: KG statements then cannot be built (the caller
    reports them as blocked; nothing is guessed)."""
    import json
    from pathlib import Path

    p = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[4] / "registry" / "entities" / "country_qids.json"
    )
    if not p.exists():
        return {}
    doc = json.loads(p.read_text())
    out: dict[str, str] = {}
    countries = doc.get("countries", doc)
    for key, e in countries.items():
        q = e.get("qid") if isinstance(e, dict) else e
        if isinstance(q, str) and q.startswith("Q"):
            out[str(key)] = q
    return out


def ensure_kg_property(engine: Any, pid: str = "P47") -> dict[str, int]:
    """Register ``pid`` in the KG allow-list with WDHARVEST's own definition (``registry/kg/properties.yaml``),
    so the definition hash is identical to the harvester's (an already registered property is unchanged)."""
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import register_properties

    props = [p for p in kg_properties(load_allowlist()) if p.pid == pid]
    if not props:
        raise ValueError(f"{pid} is not in registry/kg/properties.yaml")
    r = register_properties(engine, props)
    return {"inserted": r.properties_inserted, "versions": r.versions_inserted, "unchanged": r.unchanged}
