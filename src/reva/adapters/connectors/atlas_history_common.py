"""Shared plumbing of the long-run history atlas connectors (V3, agent A-HISTECON).

Used by :mod:`atlas_maddison`, :mod:`atlas_clio`, :mod:`atlas_gapminder` and :mod:`atlas_cow`.

* :func:`atlas_spec` -- a :class:`~reva.ports.connectors.ConnectorSpec` with template ``atlas_bulk``.
  ``ConnectorSpec.template`` is a closed ``Literal`` without an atlas value yet (the convention of the
  other V3 atlas agents; requested change for the lead: add ``"atlas_bulk"`` in
  ``src/reva/ports/connectors.py``). Every other field is validated with a placeholder template first.
* :class:`AtlasBuild` -- one atlas table ready for :func:`reva.atlas.tables.register_atlas_table`.
* :func:`ingest` -- fetch (planned items + follow-ups) -> archive every raw response as an
  ``evidence.acquisition`` (zero observation batches) -> parse -> register the atlas tables. Parsing
  happens before any table is registered, so a parse failure registers nothing.
* :func:`run_live` -- the same through the budget ledger (``reserve_ingest`` + ``MeteredHttp``): a source
  missing from the pinned ``registry/policies/budgets.yaml`` is refused (fail closed).
* :func:`historical_qids` -- the agent-mapped provider-code -> Wikidata QID table
  ``registry/entities/historical_states_qids.json`` (historical states that are not crosswalk countries).

Nothing here makes a network request by itself; the ``http`` object passed in does.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, get_args

import polars as pl

from reva.adapters.connectors.base import to_raw
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

ATLAS_TEMPLATE = "atlas_bulk"
_VALID_PLACEHOLDER = "naturalearth_asset"  # validates the other fields; replaced before use
ACTOR = "worker:atlas_a_histecon"
REGISTRY_ROOT = Path(__file__).resolve().parents[4] / "registry"
HISTORICAL_QIDS_PATH = REGISTRY_ROOT / "entities" / "historical_states_qids.json"
DAY = 86_400


def template_supported() -> bool:
    return ATLAS_TEMPLATE in get_args(ConnectorSpec.model_fields["template"].annotation)


def atlas_spec(**fields: Any) -> ConnectorSpec:
    """A connector spec with ``template = "atlas_bulk"`` (every other field validated)."""
    if template_supported():
        return ConnectorSpec(template=ATLAS_TEMPLATE, **fields)
    checked = ConnectorSpec(template=_VALID_PLACEHOLDER, **fields)
    return checked.model_copy(update={"template": ATLAS_TEMPLATE})


def code_digest(*modules: Any) -> str:
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), *modules):
        h.update(inspect.getsource(m).encode())  # type: ignore[arg-type]
    return "sha256:" + h.hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canon(obj: Mapping[str, Any] | None = None, **kw: Any) -> str:
    """Canonical JSON object text (``flags`` / ``selector`` columns)."""
    return json.dumps({**dict(obj or {}), **kw}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


_DEC = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
_LEADING_ZERO_INT = re.compile(r"^(-?)0+([0-9])")


def decimal_text(s: str | None) -> str | None:
    """The provider's numeric text as a strict decimal string, or ``None`` when it is not a number.

    Only lossless normalisations: surrounding whitespace, a leading ``+``, a trailing ``.``, a bare
    leading ``.`` (``.5`` -> ``0.5``, as in the COW CSVs), redundant leading zeros and ``-0``. Anything
    else (``NaN``, ``inf``, thousands separators, footnote letters, empty text) is not a number here.
    """
    if s is None:
        return None
    t = s.strip()
    if t.startswith("+"):
        t = t[1:]
    if t.endswith(".") and t[:-1].lstrip("-").isdigit():
        t = t[:-1]
    if t.startswith(".") or t.startswith("-."):
        t = t.replace(".", "0.", 1)
    t = _LEADING_ZERO_INT.sub(r"\1\2", t)
    if re.fullmatch(r"-0(\.0+)?", t):
        t = "0"
    return t if _DEC.match(t) else None


def year_period(year: int) -> tuple[date, date]:
    """Calendar year as a half-open interval (years 1..9998 CE; BCE years are not representable)."""
    if not 1 <= year <= 9998:
        raise ValueError(f"year {year} outside the representable range 1..9998")
    return date(year, 1, 1), date(year + 1, 1, 1)


def frame(rows: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame({c: [r.get(c) for r in rows] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)


@dataclass
class AtlasBuild:
    """One atlas table ready for :func:`reva.atlas.tables.register_atlas_table`."""

    source: str
    dataset: str
    frame: pl.DataFrame
    raw_sha256s: list[str]
    parser_version: str
    coverage_note: str
    geo_scheme: str | None = None
    rights: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)  # parse diagnostics (not registered)


class AtlasConnector(Protocol):
    def describe(self) -> ConnectorSpec: ...

    def fetch_items(self) -> tuple[FetchItem, ...]: ...

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]: ...

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition: ...

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]: ...


async def fetch_plain(item: FetchItem, http: Any, origin: str | None = None) -> RawAcquisition:
    """GET ``item.url`` through the given client and wrap it as a raw acquisition."""
    r = await http.get(item.url, max_bytes=item.max_bytes)
    return to_raw(item, r, origin or getattr(http, "origin", "live"))


async def fetch_all(connector: AtlasConnector, http: Any, *, max_requests: int = 400) -> list[RawAcquisition]:
    """Fetch every planned item and every follow-up item (breadth-first, bounded, de-duplicated by key)."""
    queue = list(connector.fetch_items())
    seen: set[str] = set()
    raws: list[RawAcquisition] = []
    while queue:
        item = queue.pop(0)
        if item.key in seen:
            continue
        seen.add(item.key)
        if len(seen) > max_requests:
            raise RuntimeError(f"{connector.describe().name}: more than {max_requests} requests planned")
        raw = await connector.fetch(item, http)
        raws.append(raw)
        queue.extend(connector.follow_up(raw))
    return raws


def archive_raw(engine: Any, store: Any, spec: ConnectorSpec, raw: RawAcquisition, *, actor: str = ACTOR) -> str:
    """Archive the raw bytes as an acquisition of ``spec`` (no observations); return its sha256."""
    from reva.evidence.ingest import publish_acquisition

    summary = publish_acquisition(engine, store, spec, raw, [], [], actor=actor)
    if summary.namespace == "quarantine":  # pragma: no cover - no blocking report is produced here
        raise RuntimeError("raw acquisition was quarantined")
    if summary.raw_sha256 != sha256_hex(raw.body):  # pragma: no cover - store integrity
        raise RuntimeError("archived raw sha256 differs from the body hash")
    return summary.raw_sha256


def register_connector(owner_engine: Any, store: Any, spec: ConnectorSpec, actor: str = "system:a_histecon") -> Any:
    """Register ``spec`` as a connector version (tests / throwaway databases only).

    The seed registers the built-in connectors (``reva.adapters.connectors.registry``, lead-owned);
    until the A-HISTECON connectors are listed there, tests and the throwaway live ingest register them
    with the seed's own function (migration owner). The ``registry/sources/*.yaml`` entry is seeded.
    """
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version

    with owner_engine.begin() as conn:
        act = repo.simple_activity("registry.seed", "review", actor, {"connector": spec.name})
        repo.record_activity(conn, act)
        return register_connector_version(conn, store, spec, act.id)


def register_builds(engine: Any, store: Any, builds: Iterable[AtlasBuild], *, origin: str) -> list[dict[str, Any]]:
    from reva.atlas.tables import register_atlas_table

    out = []
    for b in builds:
        out.append(
            register_atlas_table(
                engine,
                store,
                source=b.source,
                dataset=b.dataset,
                frame=b.frame,
                raw_sha256s=b.raw_sha256s,
                parser_version=b.parser_version,
                origin=origin,
                coverage_note=b.coverage_note[:4000],
                geo_scheme=b.geo_scheme,
                rights=b.rights,
                actor=ACTOR,
            )
        )
    return out


async def ingest(
    engine: Any,
    store: Any,
    connector: AtlasConnector,
    http: Any,
    *,
    origin: str | None = None,
    on_raw: Callable[[RawAcquisition], None] | None = None,
    max_requests: int = 400,
) -> dict[str, Any]:
    """Fetch -> archive every raw response -> parse -> register the atlas tables (worker engine)."""
    spec = connector.describe()
    org = origin or getattr(http, "origin", "live")
    raws = await fetch_all(connector, http, max_requests=max_requests)
    for raw in raws:
        archive_raw(engine, store, spec, raw)
        if on_raw is not None:
            on_raw(raw)
    builds = connector.build(raws)
    tables = register_builds(engine, store, builds, origin=org)
    return {
        "connector": spec.name,
        "requests": len(raws),
        "raw_bytes": sum(len(r.body) for r in raws),
        "raws": raws,
        "builds": builds,
        "tables": tables,
        "reports": {f"{b.source}.{b.dataset}": b.report for b in builds},
    }


async def run_live(
    engine: Any,
    store: Any,
    connector: AtlasConnector,
    settings: Any,
    *,
    job_id: str,
    planned_requests: int,
    max_requests: int = 400,
) -> dict[str, Any]:
    """Live ingest through the persistent budget ledger (fail closed when the source has no budget)."""
    from reva.evidence.runner import make_live_client
    from reva.operations.budgets import metered, reserve_ingest, settle_metered

    spec = connector.describe()
    reservation = reserve_ingest(
        engine, job_id=job_id, attempt=1, source_key=spec.source_key, planned_requests=planned_requests
    )
    client = make_live_client(spec, settings)
    http = metered(client, engine, reservation)
    try:
        out = await ingest(engine, store, connector, http, origin="live", max_requests=max_requests)
    finally:
        out_budget = settle_metered(engine, http)
        await client.aclose()
    out["budget"] = out_budget
    return out


# --------------------------------------------------------------------------- historical QIDs
@lru_cache(maxsize=1)
def _historical_doc() -> dict[str, Any]:
    if not HISTORICAL_QIDS_PATH.exists():
        return {"entries": []}
    return json.loads(HISTORICAL_QIDS_PATH.read_text())


def historical_qids(provider: str) -> list[dict[str, Any]]:
    """Mapping entries of one provider (``maddison``, ``cow``, ``clio``, ``gapminder``)."""
    return [e for e in _historical_doc().get("entries", []) if e.get("provider") == provider]


def historical_qid(provider: str, code: str, year: int | None = None) -> dict[str, Any] | None:
    """The entry for ``code`` (valid in ``year`` when the entry is period-bounded), or ``None``."""
    hits = []
    for e in historical_qids(provider):
        if str(e.get("code")) != str(code):
            continue
        lo, hi = e.get("valid_from_year"), e.get("valid_to_year")
        if year is not None and ((lo is not None and year < lo) or (hi is not None and year > hi)):
            continue
        if year is None and (lo is not None or hi is not None):
            continue
        hits.append(e)
    if len(hits) > 1:
        raise ValueError(f"historical_states_qids.json: {provider}:{code} matches {len(hits)} entries for {year}")
    return hits[0] if hits else None
