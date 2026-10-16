"""Shared plumbing of the physical-network atlas connectors (V3, agent A-INFRA).

Used by :mod:`atlas_flights` (OpenFlights routes, OurAirports airports), :mod:`atlas_space` (GCAT),
:mod:`atlas_ports` (NGA World Port Index) and :mod:`atlas_cables` (Wikidata submarine cables).

* Fetching goes only through the run (``run.get`` + ``run.archive``): atlas modules never build an
  HTTP client (invariant 30).
* Every value these connectors emit is a **derived count** of provider records. Each row says so in
  its flags (``derived: "count"`` + the exact ``formula``) and names the raw file it was counted from
  in ``raw_sha256``/``selector``; the other raw files the count depends on (look-up tables) are listed
  in ``flags.inputs``. A count is emitted only when at least one record exists: an absent row means
  "no such record in the source", never a measured zero (``ABSENT_NOTE``).
* Dyadic (country-pair) rows fold the partner into the indicator (``|partner=<ISO3>``) and carry
  ``flags.partner_iso3`` / ``flags.counterpart_iso3`` (the conventions read by LINKS); a partner that
  does not map to a crosswalk entity is folded as ``|partner_code=<provider code>`` with
  ``partner_iso3: null`` so it is kept but never becomes a link.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

DAY = 86_400
ACTOR = "worker:atlas_ingest"
ABSENT_NOTE = "rows exist only for counts >= 1; an absent row means no such record in the source, not zero"


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


def year_span(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year + 1, 1, 1)


def day_span(d: date) -> tuple[date, date]:
    return d, d + timedelta(days=1)


def month_span(year: int, month: int) -> tuple[date, date]:
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return date(year, month, 1), end


@dataclass
class Fetched:
    """One archived raw download."""

    name: str
    url: str
    body: bytes
    sha256: str
    retrieved_at: datetime
    headers: dict[str, str] = field(default_factory=dict)

    def ref(self) -> dict[str, str]:
        return {"file": self.name, "raw_sha256": self.sha256, "url": self.url}


async def fetch_archived(
    run: Any,
    url: str,
    *,
    name: str,
    key: str,
    media_types: tuple[str, ...],
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> Fetched:
    """GET ``url`` through the run's (metered or fixture) client and archive the raw bytes first."""
    raw = await run.get(url, key=key, media_types=media_types, max_bytes=max_bytes, headers=headers)
    sha = run.archive(raw)
    meta_headers = dict((raw.response_metadata or {}).get("headers") or {})
    return Fetched(name=name, url=url, body=raw.body, sha256=sha, retrieved_at=raw.retrieved_at, headers=meta_headers)


@dataclass(frozen=True)
class Partner:
    iso3: str | None
    code: str

    def dim(self) -> str:
        return f"partner={self.iso3}" if self.iso3 else f"partner_code={self.code}"

    def flags(self) -> dict[str, Any]:
        return {"partner_iso3": self.iso3, "counterpart_iso3": self.iso3, "partner_code": self.code}


class RowBuilder:
    """Collects atlas rows of one table (``ATLAS_SCHEMA``), refusing duplicate cells."""

    def __init__(self, source: str, dataset: str) -> None:
        self.source = source
        self.dataset = dataset
        self.rows: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str, date, date]] = set()

    def count(
        self,
        *,
        indicator: str,
        provider_geo: str,
        iso3: str | None,
        period: tuple[date, date],
        value: int,
        unit: str,
        raw_sha256: str,
        selector: Mapping[str, Any],
        flags: Mapping[str, Any],
        concept_id: str | None = None,
        entity_qid: str | None = None,
    ) -> None:
        if value < 1:  # no zero for missing: absence of records is not a measured zero
            return
        key = (indicator, provider_geo, period[0], period[1])
        if key in self._seen:
            raise ValueError(f"duplicate atlas cell {key}")
        self._seen.add(key)
        self.rows.append(
            {
                "source": self.source,
                "dataset": self.dataset,
                "indicator": indicator,
                "concept_id": concept_id,
                "iso3": iso3,
                "provider_geo": provider_geo,
                "entity_qid": entity_qid,
                "period_start": period[0],
                "period_end": period[1],
                "value": str(int(value)),
                "value_text": None,
                "unit": unit,
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(selector),
            }
        )

    def text(
        self,
        *,
        indicator: str,
        provider_geo: str,
        iso3: str | None,
        period: tuple[date, date],
        value_text: str,
        unit: str,
        raw_sha256: str,
        selector: Mapping[str, Any],
        flags: Mapping[str, Any],
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
                "entity_qid": None,
                "period_start": period[0],
                "period_end": period[1],
                "value": None,
                "value_text": value_text[:4000],
                "unit": unit,
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(selector),
            }
        )

    def frame(self) -> pl.DataFrame:
        from reva.atlas.tables import ATLAS_SCHEMA

        cols = ATLAS_SCHEMA.names()
        return pl.DataFrame({c: [r[c] for r in self.rows] for c in cols}, schema=ATLAS_SCHEMA)


def derived(formula: str, *, inputs: Iterable[Fetched] = (), **extra: Any) -> dict[str, Any]:
    """Flags of a derived count: what was counted, from which raw files, and the absent-row rule."""
    out: dict[str, Any] = {"derived": "count", "formula": formula, "absent_rows": ABSENT_NOTE}
    ins = [f"{f.name}:{f.sha256}" for f in inputs]
    if ins:
        out["inputs"] = ins
    out.update(extra)
    return out


def unmapped_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Provider geographies left unmapped (iso3 null) with their row counts (kept, listed)."""
    c: dict[str, int] = {}
    for r in rows:
        if r.get("iso3") is None:
            c[str(r["provider_geo"])] = c.get(str(r["provider_geo"]), 0) + 1
    return [{"provider_geo": k, "rows": v} for k, v in sorted(c.items())]


def read_tsv(body: bytes, *, comment_prefix: str = "# ") -> list[dict[str, str]]:
    """GCAT-style TSV: a ``#``-prefixed header line, ``# ``-prefixed comment lines; fields stripped."""
    lines = [ln for ln in body.decode("utf-8").splitlines() if ln and not ln.startswith(comment_prefix)]
    if not lines or not lines[0].startswith("#"):
        raise ValueError("TSV header line (starting with '#') not found")
    hdr = lines[0].lstrip("#").split("\t")
    out = []
    for ln in lines[1:]:
        parts = [x.strip() for x in ln.split("\t")]
        if len(parts) < len(hdr):
            parts += [""] * (len(hdr) - len(parts))
        out.append(dict(zip(hdr, parts, strict=False)))
    return out
