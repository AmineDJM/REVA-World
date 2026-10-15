"""Shared plumbing of the quality-of-life / environment / energy atlas connectors (V3, agent A-QOL).

Used by :mod:`atlas_undp`, :mod:`atlas_whr`, :mod:`atlas_owid` and :mod:`atlas_wid`, which are atlas
sources of the atlas platform (``docs/dev/v3_atlas_interface.md`` section 7): each declares
``atlas_source()`` (an :class:`~reva.atlas.sources.AtlasSourceSpec`) and ``async atlas_ingest(run)``,
which calls :func:`run_connector`. Nothing here builds an HTTP client: every request goes through the
run (``run.get`` / ``run.get_range``: the budget-metered live client or the fixture client).

* :class:`AtlasBuild` — one atlas table to register (frame + the raw artifacts it was parsed from).
* :func:`run_connector` — fetch every planned item through the run, archive each raw response as an
  ``evidence.acquisition`` as soon as it arrives (``run.archive``), parse, and register the atlas
  tables (``run.register``). Nothing is registered when a parse fails.
* :class:`NameIndex` — provider country *names* -> crosswalk keys, by exact (normalised) match on the
  crosswalk's own labels plus explicit, per-provider aliases. Unmatched names stay unmapped (kept).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import polars as pl

from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem, RawAcquisition

DAY = 86_400


def code_digest(*modules: Any) -> str:
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), *modules):
        h.update(inspect.getsource(m).encode())
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
    leading ``.`` (``.5`` -> ``0.5``) and redundant leading zeros of the integer part. Everything
    else (``NaN``, ``inf``, thousands separators, footnote letters) is *not* a number here.
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
    if t in ("-0",):
        t = "0"
    return t if _DEC.match(t) else None


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
    def describe(self) -> AtlasSourceSpec: ...

    def fetch_items(self) -> tuple[FetchItem, ...]: ...

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]: ...

    async def fetch(self, item: FetchItem, run: Any) -> RawAcquisition: ...

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]: ...


async def fetch_plain(item: FetchItem, run: Any) -> RawAcquisition:
    """GET ``item.url`` through the atlas run (``params`` stay on the acquisition's item)."""
    raw: RawAcquisition = await run.get(
        item.url,
        key=item.key,
        media_types=item.expected_media_types or ("application/octet-stream",),
        max_bytes=item.max_bytes,
        params=item.params,
    )
    return raw


async def fetch_all(
    connector: AtlasConnector, run: Any, *, max_requests: int = 1000, on_raw: Any = None
) -> list[RawAcquisition]:
    """Fetch every planned item and every follow-up item (breadth-first, bounded).

    ``on_raw(raw)`` runs as soon as each response arrives (the platform archives it there, so the
    raw bytes of a run cut short by its budget are kept)."""
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
        raw = await connector.fetch(item, run)
        if on_raw is not None:
            on_raw(raw)
        raws.append(raw)
        queue.extend(connector.follow_up(raw))
    return raws


async def run_connector(run: Any, connector: AtlasConnector) -> dict[str, Any]:
    """Atlas platform run: fetch (metered) -> archive each raw response -> parse -> register.

    Parsing happens before any table is registered, so a parse failure registers nothing (the raw
    bytes already archived stay archived: they are what the provider served). Builds of datasets
    the run does not want (``--dataset``) are not registered."""
    spec = connector.describe()
    if spec.name != run.spec.name:
        raise ValueError(f"connector {spec.name} run as {run.spec.name}")
    shas: dict[str, str] = {}

    def archive(raw: RawAcquisition) -> None:
        sha = run.archive(raw)
        if sha != sha256_hex(raw.body):  # pragma: no cover - store integrity
            raise RuntimeError("archived raw sha256 differs from the body hash")
        shas[raw.item.key] = sha

    raws = await fetch_all(connector, run, max_requests=max(1000, spec.planned_requests * 2), on_raw=archive)
    builds = connector.build(raws)
    tables = []
    for b in builds:
        if b.source != spec.atlas_source:
            raise ValueError(f"build {b.source}.{b.dataset} is not of source {spec.atlas_source}")
        if not run.wants(b.dataset):
            continue
        tables.append(
            run.register(
                dataset=b.dataset,
                frame=b.frame,
                raw_sha256s=b.raw_sha256s,
                coverage_note=b.coverage_note[:4000],
                geo_scheme=b.geo_scheme,
                rights={**b.rights, "source_key": spec.source_key},
            )
        )
    return {
        "connector": spec.name,
        "requests": len(raws),
        "raw_bytes": sum(len(r.body) for r in raws),
        "tables": [{k: v for k, v in t.items() if k != "coverage"} | {"coverage": _light(t)} for t in tables],
        "reports": {f"{b.source}.{b.dataset}": b.report for b in builds},
    }


def _light(t: Mapping[str, Any]) -> dict[str, Any]:
    cov = dict(t.get("coverage") or {})
    cov.pop("unmapped_provider_geo", None)
    return cov


# --------------------------------------------------------------------------- country names
def norm_name(s: str) -> str:
    """Case/accent/punctuation-insensitive form of a country name (exact matching only)."""
    t = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().casefold()
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


@lru_cache(maxsize=1)
def _crosswalk_names() -> dict[str, str]:
    from reva.atlas.tables import CROSSWALK_PATH

    doc = json.loads(CROSSWALK_PATH.read_text())
    out: dict[str, str] = {}
    clash: set[str] = set()
    for e in doc["entities"]:
        for fld in ("label", "worldbank_name", "comtrade_reporter_label"):
            v = e.get(fld)
            if not v:
                continue
            k = norm_name(v)
            if k in out and out[k] != e["key"]:
                clash.add(k)
            out.setdefault(k, e["key"])
    for k in clash:  # ambiguous names never map
        out.pop(k, None)
    return out


class NameIndex:
    """Provider country names -> crosswalk entity keys.

    Order: explicit provider aliases (each documented in the connector module, with the provider's
    exact spelling), then exact normalised match on crosswalk ``label`` / ``worldbank_name`` /
    ``comtrade_reporter_label``. Names that match nothing return ``None`` (kept unmapped, listed).
    """

    def __init__(self, aliases: Mapping[str, str | None] | None = None, extra: Mapping[str, str] | None = None):
        from reva.atlas.tables import known_country_keys

        keys = known_country_keys()
        self.aliases: dict[str, str | None] = {}
        for name, key in (aliases or {}).items():
            if key is not None and key not in keys:
                raise ValueError(f"alias {name!r} -> {key!r} is not a crosswalk entity")
            self.aliases[norm_name(name)] = key
        self.extra = {norm_name(k): v for k, v in (extra or {}).items() if v in keys}

    def lookup(self, name: str | None) -> tuple[str | None, str]:
        """``(iso3 | None, how)`` with ``how`` in alias / crosswalk / provider_table / unmapped."""
        if not name:
            return None, "unmapped"
        k = norm_name(name)
        if k in self.aliases:
            v = self.aliases[k]
            return (v, "alias") if v else (None, "alias_unmapped")
        if k in self.extra:
            return self.extra[k], "provider_table"
        v = _crosswalk_names().get(k)
        return (v, "crosswalk") if v else (None, "unmapped")


__all__ = [
    "DAY",
    "AtlasBuild",
    "AtlasConnector",
    "NameIndex",
    "canon",
    "code_digest",
    "decimal_text",
    "fetch_all",
    "fetch_plain",
    "frame",
    "norm_name",
    "run_connector",
    "sha256_hex",
]
