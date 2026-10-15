"""Shared helpers for the A-GEO atlas connectors (Natural Earth 1:10m, GeoNames, CCKP climate, INFORM).

* Connector specs: ``ConnectorSpec.template`` is a closed ``Literal`` without an atlas value yet, so
  :func:`atlas_spec` validates every field with a valid placeholder template and then sets the template to
  ``"atlas_bulk"`` (the convention used by the other V3 atlas agents).
* Raw bytes are archived as evidence acquisitions (zero observation batches) with
  :func:`archive_raw` *before* any atlas table or KG statement is registered from them.
* :func:`register_connector` registers a connector version in a throwaway/test database with the seed's
  own function: the built-in connector list (``reva.adapters.connectors.registry``) is lead-owned and does
  not list the A-GEO connectors yet.
* Values are emitted as strict decimal strings; computed values are rounded explicitly (:func:`dec`).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, get_args

from reva.adapters.connectors.base import dec_str, to_raw
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

ATLAS_TEMPLATE = "atlas_bulk"
_VALID_PLACEHOLDER = "naturalearth_asset"


def template_supported() -> bool:
    return ATLAS_TEMPLATE in get_args(ConnectorSpec.model_fields["template"].annotation)


def atlas_spec(**fields: Any) -> ConnectorSpec:
    """A connector spec with ``template = "atlas_bulk"`` (every other field validated)."""
    if template_supported():
        return ConnectorSpec(template=ATLAS_TEMPLATE, **fields)  # type: ignore[arg-type]
    checked = ConnectorSpec(template=_VALID_PLACEHOLDER, **fields)  # type: ignore[arg-type]
    return checked.model_copy(update={"template": ATLAS_TEMPLATE})


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


def archive_raw(engine: Any, store: Any, spec: ConnectorSpec, raw: RawAcquisition, *, actor: str) -> str:
    """Archive the raw bytes as an acquisition of ``spec`` (no observations); return its sha256."""
    from reva.evidence.ingest import publish_acquisition

    summary = publish_acquisition(engine, store, spec, raw, [], [], actor=actor)
    if summary.namespace == "quarantine":  # pragma: no cover - no blocking report is passed
        raise RuntimeError("raw acquisition was quarantined")
    if summary.raw_sha256 != sha256_hex(raw.body):  # pragma: no cover - store integrity
        raise RuntimeError("archived raw sha256 differs from the body hash")
    return str(summary.raw_sha256)


def register_connector(owner_engine: Any, store: Any, spec: ConnectorSpec, actor: str = "system:a_geo") -> Any:
    """Register ``spec`` as a connector version (tests / throwaway databases; migration-owner engine).
    Its ``registry/sources/*.yaml`` entry must already be seeded."""
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version

    with owner_engine.begin() as conn:
        act = repo.simple_activity("registry.seed", "review", actor, {"connector": spec.name})
        repo.record_activity(conn, act)
        return register_connector_version(conn, store, spec, act.id)


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
