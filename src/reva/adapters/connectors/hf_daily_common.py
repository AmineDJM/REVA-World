"""Shared plumbing of the PRED-HF daily-series connectors (V5, ``docs/roadmap/v5_prediction_engine.md``).

PRED-HF owns **daily** market / reference series only (V4ECON owns monthly/quarterly macro, V4ENERGY monthly
energy). Connectors: :mod:`atlas_hf_ecb` (ECB euro reference FX rates, euro short-term rate, key ECB rates),
:mod:`atlas_hf_bis` (BIS daily central bank policy rates and daily effective exchange rates, bulk files),
:mod:`atlas_hf_ust` (US Treasury daily par / real par yield curves), :mod:`atlas_hf_eia` (EIA daily spot prices:
crude oil, petroleum products, Henry Hub natural gas), :mod:`atlas_hf_boe` (Bank of England Bank Rate and SONIA),
:mod:`atlas_hf_nyfed` (New York Fed EFFR, OBFR, SOFR).

Rules shared by every table (checked by ``tests/unit/test_atlas_hf_daily.py``):

* **Day precision.** One row per (series, provider geography, day); period ``[day, day + 1)``; ``flags.precision
  = "day"``. Nothing is averaged, filled forward or interpolated: a day the provider does not report (weekend,
  holiday, ``N/A``, empty cell, BIS status ``M``/``H``) is **no row** and is counted in the report.
* **Values** are the provider's text when it is already a strict decimal (``"18.60"`` stays ``"18.60"``); other
  finite numbers are normalised (``".5"`` -> ``"0.5"``) with ``flags.value_normalised``; anything else is no row.
* **As-of / publication semantics.** ``flags.as_of`` = the UTC date REVA fetched the raw file (the vintage of
  the value; a later re-fetch that changes a value registers a new table version, the atlas keeps every earlier
  version). ``flags.publication`` = the provider's own publication rule for an observation of that day and
  ``flags.revision_policy`` = the provider's revision policy, both short paraphrases whose verbatim source text
  is quoted in the connector's ``RIGHTS[...]["policy_quotes"]`` and checked against the archived policy page at
  every run (``report.policy_quotes_found``; a quote that disappeared is reported, never silently kept).
* **Licence per value**: ``flags.licence`` on every row; third-party data republished by a public body (EIA spot
  prices from Refinitiv/LSEG, NY Fed SOFR computed from DTCC data) carry ``flags.licence_review``.
* **Geography**: these are market/reference series, not country statistics, unless the provider codes a country
  (BIS ``REF_AREA`` ISO 3166 alpha-2 -> ``iso3``); otherwise ``provider_geo`` is the provider's area code (``U2``
  euro area, ``US``/``GB`` for the issuing jurisdiction of a national rate) and ``iso3`` is set only through the
  crosswalk (never by name).
* **Observations only**: ``flags.estimate_kind = "observation"``; no projection is ever written here.
"""

from __future__ import annotations

import hashlib
import html
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl

from reva.kernel.errors import SourceResponseError

MAX_JSON_CHARS = 4000
STRICT_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
DAY = 86_400


class HfError(SourceResponseError):
    pass


def err(msg: str, kind: str = "schema_drift") -> HfError:
    return HfError(msg, retryable=False, kind=kind)


def canon(obj: Mapping[str, Any] | None = None, **kw: Any) -> str:
    return json.dumps({**dict(obj or {}), **kw}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def code_digest(*modules: Any) -> str:
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), *modules):
        h.update(inspect.getsource(m).encode())
    return "sha256:" + h.hexdigest()


def number(text: str | None) -> tuple[str | None, bool]:
    """``(strict decimal text, normalised?)`` of a provider cell; ``(None, False)`` when it is not a finite number."""
    if text is None:
        return None, False
    t = text.strip()
    if not t:
        return None, False
    if STRICT_DECIMAL.match(t):
        return t, False
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None, False
    if not d.is_finite():
        return None, False
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    s = "0" if s in ("-0", "") else s
    return (s, True) if STRICT_DECIMAL.match(s) else (None, False)


def day_span(d: date) -> tuple[date, date]:
    return d, d + timedelta(days=1)


@dataclass
class Fetched:
    """One archived raw download (``key`` names it in selectors)."""

    key: str
    url: str
    body: bytes
    sha256: str
    retrieved_at: datetime

    @property
    def as_of(self) -> str:
        return self.retrieved_at.astimezone(UTC).date().isoformat()

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, "replace")


@dataclass(frozen=True)
class FileSpec:
    key: str
    url: str
    datasets: tuple[str, ...]
    media: tuple[str, ...]
    max_bytes: int
    role: str = "data"  # "data" | "policy" (terms / methodology page archived as evidence of the quotes)


async def fetch(run: Any, spec: FileSpec) -> Fetched:
    """GET through the run's metered (or fixture) client and archive the raw bytes before parsing."""
    raw = await run.get(spec.url, key=spec.key, media_types=spec.media, max_bytes=spec.max_bytes)
    sha = run.archive(raw)
    return Fetched(spec.key, spec.url, raw.body, sha, raw.retrieved_at)


async def fetch_all(run: Any, files: Sequence[FileSpec], wanted: Iterable[str]) -> dict[str, Fetched]:
    want = set(wanted)
    out: dict[str, Fetched] = {}
    for f in files:
        if want & set(f.datasets):
            out[f.key] = await fetch(run, f)
    return out


def page_text(body: bytes) -> str:
    """Visible text of an archived HTML page (scripts/styles dropped, tags removed, whitespace collapsed)."""
    t = body.decode("utf-8", "replace")
    t = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def _norm(s: str) -> str:
    """Whitespace/typography-insensitive form for quote checks (a link inside a sentence leaves ``days .``)."""
    s = s.replace("’", "'").replace("“", '"').replace("”", '"').replace("\xa0", " ")
    return re.sub(r"\s+([.,;:)])", r"\1", re.sub(r"\s+", " ", s)).strip()


def quotes_found(pages: Mapping[str, Fetched], quotes: Mapping[str, tuple[str, str]]) -> dict[str, bool]:
    """``{quote_id: present?}`` for ``quotes = {id: (page_key, verbatim text)}`` against the archived pages."""
    texts = {k: _norm(page_text(f.body)) for k, f in pages.items()}
    return {q: (k in texts and _norm(s) in texts[k]) for q, (k, s) in quotes.items()}


class DailyRows:
    """Columnar atlas rows of one daily table; one row per (indicator, provider_geo, day)."""

    COLS = ("indicator", "iso3", "provider_geo", "period_start", "period_end", "value", "value_text", "unit",
            "flags", "raw_sha256", "selector")  # fmt: skip

    def __init__(self, source: str, dataset: str) -> None:
        self.source = source
        self.dataset = dataset
        self.cols: dict[str, list[Any]] = {c: [] for c in self.COLS}
        self._seen: set[tuple[str, str, date]] = set()
        self._flags: dict[tuple[tuple[str, Any], ...], str] = {}

    def __len__(self) -> int:
        return len(self.cols["indicator"])

    def flags(self, **kw: Any) -> str:
        """Canonical flags text, interned (thousands of rows share a handful of flag objects)."""
        key = tuple(sorted((k, json.dumps(v, sort_keys=True, default=str)) for k, v in kw.items()))
        got = self._flags.get(key)
        if got is None:
            got = canon(kw)
            if not 2 <= len(got) <= MAX_JSON_CHARS:
                raise err(f"{self.dataset}: flags of {len(got)} characters", kind="invalid")
            self._flags[key] = got
        return got

    def add(
        self,
        *,
        indicator: str,
        provider_geo: str,
        day: date,
        value: str,
        unit: str,
        flags: str,
        raw_sha256: str,
        selector: Mapping[str, Any],
        iso3: str | None = None,
    ) -> None:
        k = (indicator, provider_geo, day)
        if k in self._seen:
            raise err(f"{self.dataset}: duplicate cell {k}", kind="invalid")
        self._seen.add(k)
        c = self.cols
        c["indicator"].append(indicator)
        c["iso3"].append(iso3)
        c["provider_geo"].append(provider_geo)
        c["period_start"].append(day)
        c["period_end"].append(day + timedelta(days=1))
        c["value"].append(value)
        c["value_text"].append(None)
        c["unit"].append(unit)
        c["flags"].append(flags)
        c["raw_sha256"].append(raw_sha256)
        c["selector"].append(canon(selector))

    def add_text(
        self,
        *,
        indicator: str,
        provider_geo: str,
        span: tuple[date, date],
        text: str,
        unit: str,
        flags: str,
        raw_sha256: str,
        selector: Mapping[str, Any],
        iso3: str | None = None,
    ) -> None:
        """A provider text cell over a span (series metadata such as a compilation note), never a number."""
        k = (indicator, provider_geo, span[0])
        if k in self._seen or not text or len(text) > MAX_JSON_CHARS:
            raise err(f"{self.dataset}: duplicate or invalid text cell {k}", kind="invalid")
        self._seen.add(k)
        c = self.cols
        for col, v in (("indicator", indicator), ("iso3", iso3), ("provider_geo", provider_geo),
                       ("period_start", span[0]), ("period_end", span[1]), ("value", None), ("value_text", text),
                       ("unit", unit), ("flags", flags), ("raw_sha256", raw_sha256), ("selector", canon(selector))):
            c[col].append(v)  # fmt: skip

    def frame(self) -> pl.DataFrame:
        from reva.atlas.tables import ATLAS_SCHEMA

        n = len(self)
        data: dict[str, Any] = {
            "source": [self.source] * n,
            "dataset": [self.dataset] * n,
            "concept_id": [None] * n,
            "entity_qid": [None] * n,
            **self.cols,
        }
        return pl.DataFrame({c: data[c] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)

    def rows(self) -> list[dict[str, Any]]:
        """Row dicts (tests and small tables only)."""
        return self.frame().to_dicts()


@dataclass
class TableBuild:
    rows: DailyRows
    report: dict[str, Any] = field(default_factory=dict)


def base_flags(rights: Mapping[str, Any], f: Fetched, **extra: Any) -> dict[str, Any]:
    """The flags every daily row carries (licence, as-of, publication and revision rule, precision)."""
    out: dict[str, Any] = {
        "licence": rights["licence"],
        "as_of": f.as_of,
        "publication": rights["publication"],
        "revision_policy": rights["revision_policy"],
        "precision": "day",
        "estimate_kind": "observation",
    }
    if rights.get("licence_review"):
        out["licence_review"] = rights["licence_review"]
    out.update({k: v for k, v in extra.items() if v not in (None, "")})
    return out


def register_tables(
    run: Any,
    builds: Mapping[str, TableBuild],
    *,
    rights: Mapping[str, Mapping[str, Any]],
    notes: Mapping[str, str],
    source_key: str,
    geo_scheme: str | None = None,
) -> list[dict[str, Any]]:
    tables = []
    for ds, b in builds.items():
        if not len(b.rows):
            raise err(f"{ds}: no rows", kind="incomplete")
        frame = b.rows.frame()
        r = {k: v for k, v in rights[ds].items() if k != "policy_quotes"}
        out = run.register(
            dataset=ds,
            frame=frame,
            raw_sha256s=sorted(set(frame["raw_sha256"].to_list())),
            geo_scheme=geo_scheme,
            rights={**r, "source_key": source_key},
            coverage_note=notes[ds][:MAX_JSON_CHARS],
        )
        tables.append(
            {
                "table": out["dataset_key"].removeprefix("atlas."),
                "rows": out["row_count"],
                "created": out["created"],
                "artifact_sha256": out.get("artifact_sha256"),
            }
        )
    return tables


def check_policy_pages(
    files: Mapping[str, Fetched], specs: Sequence[FileSpec], rights: Mapping[str, Mapping[str, Any]], wanted: Iterable[str]
) -> dict[str, bool]:
    pages = {s.key: files[s.key] for s in specs if s.role == "policy" and s.key in files}
    quotes: dict[str, tuple[str, str]] = {}
    for ds in wanted:
        for qid, (page, text) in (rights[ds].get("policy_quotes") or {}).items():
            quotes[qid] = (page, text)
    return quotes_found(pages, quotes)


__all__ = [
    "DailyRows",
    "Fetched",
    "FileSpec",
    "HfError",
    "TableBuild",
    "base_flags",
    "canon",
    "check_policy_pages",
    "code_digest",
    "day_span",
    "err",
    "fetch",
    "fetch_all",
    "number",
    "page_text",
    "quotes_found",
    "register_tables",
]
