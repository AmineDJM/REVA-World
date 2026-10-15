"""Shared plumbing of the quality-of-life / environment / energy atlas connectors (V3, agent A-QOL).

Used by :mod:`atlas_undp`, :mod:`atlas_whr`, :mod:`atlas_owid` and :mod:`atlas_wid`.

* :func:`atlas_spec` — a :class:`~reva.ports.connectors.ConnectorSpec` with template ``atlas_bulk``.
  ``ConnectorSpec.template`` is a closed ``Literal`` without an atlas value yet (requested change for
  the lead: add ``"atlas_bulk"`` to it in ``src/reva/ports/connectors.py``). Every other field is
  validated normally; the template is set afterwards.
* :class:`AtlasBuild` — one atlas table to register (frame + the raw artifacts it was parsed from).
* :func:`ingest` — fetch every planned item, archive each raw response as an ``evidence.acquisition``
  (zero observation batches), parse, and register the atlas tables through
  :func:`reva.atlas.tables.register_atlas_table`. Nothing is registered when a parse fails.
* :class:`NameIndex` — provider country *names* -> crosswalk keys, by exact (normalised) match on the
  crosswalk's own labels plus explicit, per-provider aliases. Unmatched names stay unmapped (kept).

Nothing here makes a network request by itself; the ``http`` object passed to :func:`ingest`
(``SafeHttpClient`` live, ``FixtureHttp`` in tests) does.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol, get_args

import polars as pl

from reva.adapters.connectors.base import to_raw
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

ATLAS_TEMPLATE = "atlas_bulk"
_VALID_PLACEHOLDER = "naturalearth_asset"  # only to validate the other fields; replaced before use
DAY = 86_400


def template_supported() -> bool:
    return ATLAS_TEMPLATE in get_args(ConnectorSpec.model_fields["template"].annotation)


def atlas_spec(**fields: Any) -> ConnectorSpec:
    """A connector spec with ``template = "atlas_bulk"`` (every other field validated)."""
    if template_supported():
        return ConnectorSpec(template=ATLAS_TEMPLATE, **fields)  # type: ignore[arg-type]
    checked = ConnectorSpec(template=_VALID_PLACEHOLDER, **fields)  # type: ignore[arg-type]
    return checked.model_copy(update={"template": ATLAS_TEMPLATE})


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
    def describe(self) -> ConnectorSpec: ...

    def fetch_items(self) -> tuple[FetchItem, ...]: ...

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]: ...

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition: ...

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]: ...


async def fetch_plain(item: FetchItem, http: Any, origin: str | None = None) -> RawAcquisition:
    """GET ``item.url`` through the given client and wrap it as a raw acquisition."""
    r = await http.get(item.url, max_bytes=item.max_bytes)
    return to_raw(item, r, str(origin or getattr(http, "origin", "live")))


async def fetch_all(connector: AtlasConnector, http: Any, *, max_requests: int = 500) -> list[RawAcquisition]:
    """Fetch every planned item and every follow-up item (breadth-first, bounded)."""
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


def archive_raw(engine: Any, store: Any, spec: ConnectorSpec, raw: RawAcquisition, *, actor: str) -> str:
    """Archive the raw bytes as an acquisition of ``spec`` (no observations); return its sha256."""
    from reva.evidence.ingest import publish_acquisition

    summary = publish_acquisition(engine, store, spec, raw, [], [], actor=actor)
    if summary.namespace == "quarantine":  # pragma: no cover - no blocking report is ever produced here
        raise RuntimeError("raw acquisition was quarantined")
    if summary.raw_sha256 != sha256_hex(raw.body):  # pragma: no cover - store integrity
        raise RuntimeError("archived raw sha256 differs from the body hash")
    return summary.raw_sha256


def register_connector(owner_engine: Any, store: Any, spec: ConnectorSpec, actor: str = "system:a_qol") -> Any:
    """Register ``spec`` as a connector version (tests / throwaway databases).

    The seed registers the built-in connectors (``reva.adapters.connectors.registry``, lead-owned);
    until the A-QOL connectors are listed there, tests and the throwaway live ingest register them
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
                actor="worker:atlas_a_qol",
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
) -> dict[str, Any]:
    """Fetch -> archive every raw response -> parse -> register the atlas tables (worker engine).

    Parsing happens before any table is registered, so a parse failure registers nothing (the raw
    bytes already archived stay archived: they are what the provider served).
    """
    spec = connector.describe()
    org = str(origin or getattr(http, "origin", "live"))
    raws = await fetch_all(connector, http)
    for raw in raws:
        archive_raw(engine, store, spec, raw, actor="worker:atlas_a_qol")
        if on_raw is not None:
            on_raw(raw)
    builds = connector.build(raws)
    tables = register_builds(engine, store, builds, origin=org)
    return {
        "connector": spec.name,
        "requests": len(raws),
        "raw_bytes": sum(len(r.body) for r in raws),
        "tables": tables,
        "reports": {f"{b.source}.{b.dataset}": b.report for b in builds},
    }


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
    "ATLAS_TEMPLATE",
    "DAY",
    "AtlasBuild",
    "AtlasConnector",
    "NameIndex",
    "archive_raw",
    "atlas_spec",
    "canon",
    "code_digest",
    "decimal_text",
    "fetch_all",
    "fetch_plain",
    "frame",
    "ingest",
    "norm_name",
    "register_builds",
    "register_connector",
    "sha256_hex",
    "template_supported",
]
