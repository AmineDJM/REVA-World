"""Shared plumbing of the long-run history atlas connectors (V3, agent A-HISTECON).

Used by :mod:`atlas_maddison`, :mod:`atlas_clio`, :mod:`atlas_gapminder` and :mod:`atlas_cow`.

* :func:`source_spec` -- an :class:`reva.atlas.sources.AtlasSourceSpec` (the atlas platform's source
  declaration; each module exposes it as ``atlas_source()``).
* :class:`AtlasBuild` -- one atlas table parsed from raw bytes.
* :func:`ingest_with_run` -- the body of each module's ``atlas_ingest(run)``: fetch through ``run.http``
  (budget-metered live client or fixture client built by ``reva.atlas.ingest``), archive with
  ``run.archive``, parse, register with ``run.register``.
* :func:`historical_qids` -- the agent-mapped provider-code -> Wikidata QID table
  ``registry/entities/historical_states_qids.json`` (historical states that are not crosswalk countries).

Nothing here makes a network request by itself; the ``http`` object passed in does.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from reva.adapters.connectors.base import to_raw
from reva.ports.connectors import FetchItem, RawAcquisition

ACTOR = "worker:atlas_ingest"
REGISTRY_ROOT = Path(__file__).resolve().parents[4] / "registry"
HISTORICAL_QIDS_PATH = REGISTRY_ROOT / "entities" / "historical_states_qids.json"
DAY = 86_400


def source_spec(
    *,
    units: Mapping[str, str] | None = None,
    time_semantics: str = "",
    revision_strategy: str = "",
    lineage_family: str = "",
    geography: Sequence[str] = (),
    **fields: Any,
) -> Any:
    """An :class:`reva.atlas.sources.AtlasSourceSpec`; descriptive extras are folded into ``notes``."""
    from reva.atlas.sources import AtlasSourceSpec

    extra = {
        "units": dict(units or {}),
        "time_semantics": time_semantics,
        "revision_strategy": revision_strategy,
        "lineage_family": lineage_family,
        "geography": list(geography),
    }
    return AtlasSourceSpec(notes=json.dumps(extra, sort_keys=True, ensure_ascii=False)[:4000], **fields)


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
    def describe(self) -> Any: ...

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


async def ingest_with_run(run: Any, connector: AtlasConnector, *, max_requests: int = 400) -> dict[str, Any]:
    """The atlas platform's ``atlas_ingest(run)`` body for these connectors.

    Fetch (planned items + follow-ups) through ``run.http`` -- the budget-metered live client or the
    fixture client the platform built, never one built here -- archive every raw response with
    ``run.archive``, parse, and register each table the run wants with ``run.register``. Parsing happens
    before any table is registered, so a parse failure registers nothing.
    """
    raws = await fetch_all(connector, run.http, max_requests=max_requests)
    shas = [run.archive(raw) for raw in raws]
    builds = connector.build(raws)
    tables = []
    for b in builds:
        if not run.wants(b.dataset):
            continue
        out = run.register(
            dataset=b.dataset,
            frame=b.frame,
            raw_sha256s=b.raw_sha256s,
            coverage_note=b.coverage_note[:4000],
            geo_scheme=b.geo_scheme,
            rights=b.rights or None,
        )
        tables.append(
            {k: out[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created")}
            | {
                "coverage": {k: v for k, v in out["coverage"].items() if k != "unmapped_provider_geo"},
                "unmapped_provider_geo": out["unmapped_provider_geo"][:200],
            }
        )
    return {
        "requests": len(raws),
        "raw_bytes": sum(len(r.body) for r in raws),
        "raw": [
            {
                "key": r.item.key,
                "url": r.item.url,
                "sha256": sha,
                "bytes": len(r.body),
                "retrieved_at": r.retrieved_at.isoformat(),
            }
            for r, sha in zip(raws, shas, strict=True)
        ],
        "tables": tables,
        "reports": {f"{b.source}.{b.dataset}": b.report for b in builds},
        "_raws": raws,
        "_builds": builds,
    }


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
