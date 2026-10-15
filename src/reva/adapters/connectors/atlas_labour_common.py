"""Shared plumbing of the labour atlas connectors (V3, agent A-ILO).

Used by :mod:`atlas_ilo` (ILOSTAT) and :mod:`atlas_wages_gapfill` (Eurostat ``earn_mw_cur``,
OECD earnings). Nothing here interprets a provider value; it only

* builds a :class:`~reva.ports.connectors.ConnectorSpec` for an atlas bulk connector
  (:func:`atlas_spec`; ``ConnectorSpec.template`` is a closed ``Literal`` without an atlas value yet,
  so every other field is validated normally and the template is set to ``"atlas_bulk"`` afterwards —
  requested change for the lead: add ``"atlas_bulk"`` to that ``Literal``);
* fetches a connector's plan through any ``get(url, max_bytes=)`` client (live
  ``SafeHttpClient`` behind the egress budget, or ``FixtureHttp``), refusing empty bodies and
  unexpected media types (ILOSTAT answers ``200`` with an empty body to some clients);
* archives each raw download as an ``evidence.acquisition`` (zero observation batches) and
  registers the connector's atlas tables with :func:`reva.atlas.tables.register_atlas_table`;
* small exact helpers: bounded gunzip, strict decimal text, canonical JSON.

Every failure is typed (``SourceResponseError`` / ``ValidationProblem`` / ``BudgetExhausted``);
a table that does not validate is not registered and the run reports it — nothing is stubbed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, get_args
from uuid import uuid4

import polars as pl

from reva.adapters.connectors.base import to_raw
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

ATLAS_TEMPLATE = "atlas_bulk"
_VALID_PLACEHOLDER = "naturalearth_asset"  # only to validate the other fields; replaced before use

#: strict decimal accepted by reva.atlas.tables (kept in sync with tables.DECIMAL_RE)
_STRICT_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")


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


def canon(obj: Mapping[str, Any]) -> str:
    """Canonical JSON object text (flags / selector columns)."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def strict_decimal(text: str | None) -> str | None:
    """The provider's number as a strict decimal string, or ``None`` for an empty cell.

    Source text that already is a strict decimal is kept verbatim (no float round trip);
    other numeric spellings (``.5``, ``+3``, ``1E5``) are rewritten exactly through ``Decimal``.
    Anything non-numeric (``NaN``, ``inf``, ``1,000``, ``..``) raises ``ValidationProblem``.
    """
    if text is None:
        return None
    t = text.strip()
    if t == "":
        return None
    if _STRICT_DECIMAL.match(t):
        return t
    try:
        d = Decimal(t)
    except InvalidOperation as e:
        raise ValidationProblem("provider value is not a number", value=t[:40]) from e
    if not d.is_finite():
        raise ValidationProblem("provider value is not finite", value=t[:40])
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    s = "0" if s in ("-0", "") else s
    if not _STRICT_DECIMAL.match(s):  # pragma: no cover - format(Decimal, 'f') always matches
        raise ValidationProblem("provider value could not be written as a strict decimal", value=t[:40])
    return s


def gunzip_bounded(data: bytes, limit: int) -> bytes:
    """Inflate a gzip member, refusing output larger than ``limit`` bytes (untrusted input)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, limit + 1)
        if len(out) > limit or d.unconsumed_tail:
            raise SourceResponseError("gzip payload exceeds the decompressed size limit", retryable=False, limit=limit)
        out += d.flush()
    except zlib.error as e:
        raise SourceResponseError("payload is not valid gzip", retryable=False) from e
    if len(out) > limit:
        raise SourceResponseError("gzip payload exceeds the decompressed size limit", retryable=False, limit=limit)
    if not d.eof:
        raise SourceResponseError("truncated gzip payload", retryable=False)
    return out


def read_csv_text(data: bytes, *, what: str) -> pl.DataFrame:
    """CSV bytes -> all-string polars frame (UTF-8, BOM stripped, nothing inferred)."""
    try:
        txt = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SourceResponseError(f"{what}: not UTF-8 CSV", retryable=False) from e
    if not txt.strip():
        raise SourceResponseError(f"{what}: empty CSV", retryable=False)
    import io

    try:
        return pl.read_csv(io.StringIO(txt), infer_schema_length=0, quote_char='"')
    except Exception as e:  # polars raises several error types for malformed CSV
        raise SourceResponseError(f"{what}: malformed CSV ({type(e).__name__})", retryable=False) from e


# --------------------------------------------------------------------------- tables & runs
@dataclass(frozen=True)
class AtlasTablePlan:
    """One atlas table a connector wants registered (not yet validated)."""

    source: str
    dataset: str
    frame: pl.DataFrame
    raw_sha256s: tuple[str, ...]
    geo_scheme: str | None
    coverage_note: str
    rights: Mapping[str, Any]


class AtlasConnector(Protocol):
    parser_version: str

    def describe(self) -> ConnectorSpec: ...

    def plan(self) -> Sequence[FetchItem]: ...

    def build_tables(self, raws: Mapping[str, RawAcquisition]) -> list[AtlasTablePlan]: ...


@dataclass
class AtlasRunReport:
    connector: str
    origin: str
    fetched: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "origin": self.origin,
            "fetched": self.fetched,
            "tables": self.tables,
            "errors": self.errors,
            "budget": self.budget,
            "ok": self.ok,
        }


def check_body(item: FetchItem, raw: RawAcquisition) -> None:
    """Refuse empty bodies and media types the item does not expect (HTML error pages...)."""
    if not raw.body:
        raise SourceResponseError(f"{item.key}: empty response body", retryable=True, item=item.key)
    if raw.media_type not in item.expected_media_types:
        raise SourceResponseError(
            f"{item.key}: unexpected media type {raw.media_type!r}",
            retryable=False,
            item=item.key,
            expected=list(item.expected_media_types),
        )


async def fetch_plan(http: Any, items: Sequence[FetchItem], report: AtlasRunReport) -> dict[str, RawAcquisition]:
    """Fetch every item; a failed *required* item is an error, optional ones are only reported."""
    raws: dict[str, RawAcquisition] = {}
    origin = getattr(http, "origin", "live")
    for it in items:
        try:
            res = await http.get(it.url, max_bytes=it.max_bytes)
            raw = to_raw(it, res, origin)
            check_body(it, raw)
        except Exception as e:  # typed errors are reported per item; the table needing it is skipped
            entry = {"item": it.key, "error": type(e).__name__, "message": str(e)[:300], "required": it.required}
            (report.errors if it.required else report.fetched).append(entry)
            continue
        raws[it.key] = raw
        report.fetched.append({"item": it.key, "bytes": len(raw.body), "sha256": hashlib.sha256(raw.body).hexdigest()})
    return raws


def archive_raw(engine: Any, store: Any, spec: ConnectorSpec, raw: RawAcquisition, *, actor: str) -> str:
    """Archive the raw bytes as an acquisition of ``spec`` (no observations); return the sha256."""
    from reva.evidence.ingest import publish_acquisition

    summary = publish_acquisition(engine, store, spec, raw, [], [], actor=actor)
    if summary.namespace == "quarantine":  # pragma: no cover - no quality report can block here
        raise ValidationProblem("raw acquisition was quarantined", item=raw.item.key)
    return summary.raw_sha256


def register_connector(engine: Any, store: Any, spec: ConnectorSpec, actor: str = "system:a_ilo") -> Any:
    """Register ``spec`` as a connector version (tests / throwaway databases).

    The seed registers built-in connectors (``reva.adapters.connectors.registry``, lead-owned);
    until these connectors are listed there, tests and the throwaway live ingest register them with
    the seed's own function. The ``registry/sources/<source>.yaml`` entry must be seeded.
    """
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version

    with engine.begin() as conn:
        act = repo.simple_activity("registry.seed", "review", actor, {"connector": spec.name})
        repo.record_activity(conn, act)
        return register_connector_version(conn, store, spec, act.id)


def _plain_details(e: Exception) -> Any:
    d = getattr(e, "details", None)
    if not d:
        return None
    return (
        json.loads(json.dumps(d, default=str))[:20] if isinstance(d, list) else json.loads(json.dumps(d, default=str))
    )


async def run_atlas_ingest(
    engine: Any,
    store: Any,
    connector: AtlasConnector,
    http: Any,
    *,
    budget: Any = None,
    actor: str = "worker:atlas_labour",
    register: Callable[..., dict[str, Any]] | None = None,
) -> AtlasRunReport:
    """Fetch -> archive raw -> parse -> register every atlas table of ``connector``.

    Live clients (``origin == "live"``) are charged to the connector's ``source_key`` through the
    persistent egress ledger (``reserve_ingest`` / ``metered`` / ``settle_metered``); a source without
    a budget is refused before any request (``BudgetExhausted``, fail closed). Fixture clients are
    never charged.
    """
    from reva.atlas.tables import register_atlas_table
    from reva.operations import budgets as B

    spec = connector.describe()
    origin = getattr(http, "origin", "live")
    report = AtlasRunReport(spec.name, origin)
    items = list(connector.plan())
    client = http
    if origin == "live":
        reservation = B.reserve_ingest(
            engine,
            job_id=f"atlas-{spec.name}-{uuid4()}",
            attempt=1,
            source_key=B.budget_source_key(spec),
            planned_requests=len(items),
            budget=budget,
        )
        client = B.metered(http, engine, reservation, budget=budget)
    try:
        raws = await fetch_plan(client, items, report)
    finally:
        if origin == "live":
            report.budget = B.settle_metered(engine, client)
    for key, raw in list(raws.items()):
        sha = archive_raw(engine, store, spec, raw, actor=actor)
        raws[key] = raw.model_copy(update={"raw_sha256": sha})
    try:
        plans = connector.build_tables(raws)
    except Exception as e:
        report.errors.append({"stage": "parse", "error": type(e).__name__, "message": str(e)[:500]})
        return report
    reg = register or register_atlas_table
    for p in plans:
        try:
            out = reg(
                engine,
                store,
                source=p.source,
                dataset=p.dataset,
                frame=p.frame,
                raw_sha256s=list(p.raw_sha256s),
                parser_version=connector.parser_version,
                origin=origin,
                coverage_note=p.coverage_note,
                geo_scheme=p.geo_scheme,
                rights=dict(p.rights),
                actor=actor,
            )
        except Exception as e:
            report.errors.append(
                {
                    "stage": "register",
                    "table": f"{p.source}.{p.dataset}",
                    "error": type(e).__name__,
                    "message": str(e)[:500],
                    "details": _plain_details(e),
                }
            )
            continue
        cov = out.get("coverage") or {}
        report.tables.append(
            {
                "table": f"{p.source}.{p.dataset}",
                "dataset_key": out.get("dataset_key"),
                "created": out.get("created"),
                "rows": out.get("row_count"),
                "countries": cov.get("countries"),
                "indicators": cov.get("indicators"),
                "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []],
                "artifact_sha256": out.get("artifact_sha256"),
            }
        )
    return report


__all__ = [
    "ATLAS_TEMPLATE",
    "AtlasConnector",
    "AtlasRunReport",
    "AtlasTablePlan",
    "archive_raw",
    "atlas_spec",
    "canon",
    "check_body",
    "code_digest",
    "fetch_plan",
    "gunzip_bounded",
    "read_csv_text",
    "register_connector",
    "run_atlas_ingest",
    "strict_decimal",
    "template_supported",
]
