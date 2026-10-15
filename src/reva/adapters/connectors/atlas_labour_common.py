"""Shared plumbing of the labour atlas connectors (V3, agent A-ILO).

Used by :mod:`atlas_ilo` (ILOSTAT) and :mod:`atlas_wages_gapfill` (Eurostat ``earn_mw_cur``,
OECD earnings). Those modules declare ``atlas_source()`` / ``atlas_ingest(run)`` for the ATLASCORE
platform (:mod:`reva.atlas.sources`, :mod:`reva.atlas.ingest`); every request goes through
``run.get`` (budget-metered live client or fixture client, never one built here), raw bytes are
archived with ``run.archive`` and tables registered with ``run.register``. This module only

* runs a connector's fetch plan through an :class:`~reva.atlas.ingest.AtlasRun`
  (:func:`ingest_with_run`), refusing empty bodies and unexpected media types (ILOSTAT answers
  ``200`` with an empty body to some clients), reporting optional items that failed, and never
  swallowing a budget refusal;
* small exact helpers: bounded gunzip, strict decimal text, canonical JSON, CSV reading.

Nothing here interprets a provider value. Every failure is typed (``SourceResponseError`` /
``ValidationProblem`` / ``BudgetExhausted``); a table that does not validate is not registered
and the summary reports it -- nothing is stubbed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import polars as pl

from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchItem, RawAcquisition

#: strict decimal accepted by reva.atlas.tables (kept in sync with tables.DECIMAL_RE)
_STRICT_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")


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


class LabourConnector(Protocol):
    """A connector object: a fetch plan and a pure parser from archived raw bytes to table plans."""

    def plan(self) -> Sequence[FetchItem]: ...

    def datasets_of(self, item: FetchItem) -> set[str] | None: ...

    def build_tables(self, raws: Mapping[str, RawAcquisition]) -> list[AtlasTablePlan]: ...


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


def _plain_details(e: Exception) -> Any:
    d = getattr(e, "details", None)
    return json.loads(json.dumps(d, default=str)) if d else None


async def ingest_with_run(run: Any, connector: LabourConnector) -> dict[str, Any]:
    """Fetch (``run.get``) -> archive (``run.archive``) -> parse -> register (``run.register``).

    ``run`` is a :class:`reva.atlas.ingest.AtlasRun`. Items whose datasets the run does not want
    (``--dataset``) are not requested. A failed *required* item stops table building; a failed
    optional item (one indicator file) is reported and its table is simply absent. A budget refusal
    (``BudgetExhausted``) always propagates (the platform ends the run as ``budget_exhausted``).
    """
    from reva.operations.budgets import BudgetExhausted

    fetched: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    raws: dict[str, RawAcquisition] = {}
    for it in connector.plan():
        wanted = connector.datasets_of(it)
        if wanted is not None and not any(run.wants(d) for d in wanted):
            continue
        try:
            raw = await run.get(it.url, key=it.key, media_types=it.expected_media_types, max_bytes=it.max_bytes)
            check_body(it, raw)
        except BudgetExhausted:
            raise
        except Exception as e:  # typed provider errors are reported per item
            errors.append({"item": it.key, "error": type(e).__name__, "message": str(e)[:300], "required": it.required})
            continue
        sha = run.archive(raw)
        raws[it.key] = raw.model_copy(update={"raw_sha256": sha})
        fetched.append({"item": it.key, "bytes": len(raw.body), "sha256": sha})
    tables: list[dict[str, Any]] = []
    if any(e["required"] for e in errors):
        return {"fetched": fetched, "errors": errors, "tables": tables, "ok": False}
    try:
        plans = connector.build_tables(raws)
    except Exception as e:
        errors.append({"stage": "parse", "error": type(e).__name__, "message": str(e)[:500]})
        return {"fetched": fetched, "errors": errors, "tables": tables, "ok": False}
    for p in plans:
        if not run.wants(p.dataset):
            continue
        try:
            out = run.register(
                dataset=p.dataset,
                frame=p.frame,
                raw_sha256s=list(p.raw_sha256s),
                coverage_note=p.coverage_note,
                geo_scheme=p.geo_scheme,
                rights=dict(p.rights),
            )
        except Exception as e:
            errors.append(
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
        tables.append(
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
    return {"fetched": fetched, "errors": errors, "tables": tables, "ok": not errors}


__all__ = [
    "AtlasTablePlan",
    "LabourConnector",
    "canon",
    "check_body",
    "code_digest",
    "gunzip_bounded",
    "ingest_with_run",
    "read_csv_text",
    "strict_decimal",
]
