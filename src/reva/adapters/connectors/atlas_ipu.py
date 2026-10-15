"""IPU Parline open-data API -> atlas tables (V3, agent A-POL).

Source
------
Inter-Parliamentary Union, Parline (``https://api.data.ipu.org/v1``): chambers, parliaments,
countries and elections of the 193 parliaments followed by the IPU. Every response states
``meta.licence`` = **CC BY-NC-SA 4.0** (non-commercial, share-alike); the connector refuses a response
that declares anything else. **Non-commercial**: a commercial deployment must exclude source ``ipu``
(recorded in the source registry, the dataset rights and every catalog concept that lists it).

Four requests per run (one page each, ``page[size]`` above the provider's totals; a response whose
``meta.total`` differs from the rows received, or that links a next page, is refused as truncated).

Tables
------
* ``ipu.parline`` -- dated chamber, parliament and country fields:

  - each provider value with its validity interval, exactly as reported: ``period = [date_from,
    date_to + 1 day)``; an open-ended current value ends at the retrieval date (exclusive end = the
    day after retrieval) and is flagged ``open_ended``;
  - for the same fields, **year-end readings** (indicator suffix ``|at=year_end``): for every year Y
    whose 31 December lies inside a reported validity interval (and is not after the retrieval date),
    a calendar-year cell with that interval's value, flagged ``selection`` + ``provider_interval``.
    This is a selection of the provider's own value valid on that day, never an interpolation;
  - undated "current" fields (members directly / indirectly elected / appointed, structure of
    parliament) as one cell for the retrieval day, flagged ``as_of_retrieval``.

  Chambers are folded into the indicator: ``|chamber=lower`` (IPU ``LC01``: lower or unicameral) and
  ``|chamber=upper`` (``UC01``).
* ``ipu.elections`` -- one cell per election record and field (turnout, registered electors, voters,
  seats at stake, women elected, scope, timing; ``election_date`` as ``value_text``), period = the
  election's date range; plus a calendar-year cell (``|at=year``) when the chamber held exactly one
  election that year.

``provider_geo`` is the IPU parliament code (ISO 3166 alpha-2), mapped with ``geo_scheme="iso2"``.
Numbers keep the provider's JSON text (no float rounding). A value the provider marks with a
``missing_reason`` is kept as a missing cell with that reason.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from reva.adapters.connectors.base import dec_str, json_decimal, to_raw
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

SOURCE = "ipu"
SOURCE_KEY = "ipu.parline"
CONNECTOR_NAME = "atlas.ipu"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_ipu/1"
HOST = "api.data.ipu.org"
BASE = f"https://{HOST}/v1"
LICENCE_MARK = "Attribution-NonCommercial-ShareAlike 4.0"
ENDPOINTS: dict[str, int] = {"chambers": 300, "parliaments": 300, "countries": 300, "elections": 3500}
MAX_BYTES = 45_000_000

RIGHTS: dict[str, Any] = {
    "licence": "CC-BY-NC-SA-4.0",
    "licence_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "attribution": "Inter-Parliamentary Union (IPU), Parline database, https://data.ipu.org",
    "non_commercial": True,
    "share_alike": "derived tables must be shared under CC BY-NC-SA 4.0",
    "restriction": "NON-COMMERCIAL: exclude source 'ipu' from any commercial use or service",
}

CHAMBER_KIND = {"LC01": "lower", "UC01": "upper"}

#: Dated chamber fields: field -> (kind, unit). kinds: num | term | bool
CHAMBER_DATED: dict[str, tuple[str, str]] = {
    "current_women_percent": ("num", "%"),
    "current_women_number": ("num", "persons"),
    "current_members_number": ("num", "persons"),
    "statutory_members_number": ("num", "seats"),
    "parliamentary_term": ("num", "years"),
    "min_age_vote_elect": ("num", "years"),
    "min_age_member_parl": ("num", "years"),
    "age_average": ("num", "years"),
    "electoral_system": ("term", "category"),
    "designation_mode": ("term", "category"),
    "is_electoral_quota_women": ("bool", "yes/no"),
    "gender_quota_or_reserved_seats": ("bool", "yes/no"),
    "is_reserved_seats": ("bool", "yes/no"),
}
CHAMBER_STATIC: dict[str, tuple[str, str]] = {
    "directly_elected_number": ("num", "persons"),
    "indirectly_elected_number": ("num", "persons"),
    "appointed_members_number": ("num", "persons"),
}
PARLIAMENT_DATED: dict[str, tuple[str, str]] = {
    "structure_of_parliament": ("term", "category"),
    "compulsory_voting": ("term", "category"),
    "head_gov_is_head_state": ("bool", "yes/no"),
}
COUNTRY_DATED: dict[str, tuple[str, str]] = {"political_system": ("term", "category")}
ELECTION_FIELDS: dict[str, tuple[str, str]] = {
    "turnout": ("num", "%"),
    "number_registered_electors": ("num", "persons"),
    "number_of_voters": ("num", "persons"),
    "number_of_seats_at_stake": ("num", "seats"),
    "num_women_elected": ("num", "persons"),
    "percentage_women_elected": ("num", "%"),
    "scope_of_elections": ("term", "category"),
    "election_timing": ("term", "category"),
}
MISSING_MAP = {"NOT_APPLICABLE": "not_applicable", "NOT_AVAILABLE": "not_available", "UNKNOWN": "not_available"}


class IpuParseError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> IpuParseError:
    return IpuParseError(msg, retryable=False, kind=kind)


# ----------------------------------------------------------------------------- fetch plan
def endpoint_url(name: str) -> str:
    return f"{BASE}/{name}?page%5Bsize%5D={ENDPOINTS[name]}"


def fetch_items() -> tuple[FetchItem, ...]:
    return tuple(FetchItem(key=f"ipu.{n}", url=endpoint_url(n), max_bytes=MAX_BYTES) for n in ENDPOINTS)


async def fetch(item: FetchItem, http: Any) -> RawAcquisition:
    result = await http.get(item.url, max_bytes=item.max_bytes)
    return to_raw(item, result, getattr(http, "origin", "live"))


def connector_spec() -> Any:
    from reva.adapters.connectors import atlas_sci_common as common

    return common.atlas_spec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        provider="Inter-Parliamentary Union (IPU)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        dataset_ids=("ipu.parline", "ipu.elections"),
        field_mappings={"geo": "parliament code (ISO 3166 alpha-2)", "chamber": "LC01 lower / UC01 upper"},
        units={"current_women_percent": "%", "turnout": "%"},
        geography=("IPU parliaments (193)",),
        time_semantics="provider validity intervals (date_from..date_to); election date ranges; year-end readings",
        lineage_family="ipu_parline",
        cadence_seconds=30 * 86400,
        release_aware=False,
        revision_strategy="new atlas table version when response bytes change",
        max_response_bytes=MAX_BYTES,
        max_pages=1,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(__import__(__name__, fromlist=["_"])),
    )


# ----------------------------------------------------------------------------- response checks
def parse_response(body: bytes, name: str) -> list[dict[str, Any]]:
    """One API response -> its ``data`` records; checks licence and completeness (fails closed)."""
    try:
        doc = json_decimal(body)
    except ValueError as e:
        raise _err(f"{name}: not JSON ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise _err(f"{name}: no data list")
    meta = doc.get("meta") or {}
    lic = ((meta.get("licence") or {}).get("en")) or ""
    if LICENCE_MARK not in lic:
        raise _err(f"{name}: declared licence changed ({lic[:120]!r}); not publishing under assumed rights", kind="licence")
    total = meta.get("total")
    if total is None or int(total) != len(doc["data"]):
        raise _err(f"{name}: received {len(doc['data'])} of {total} records (truncated page)", kind="incomplete")
    if (doc.get("links") or {}).get("next"):
        raise _err(f"{name}: response links a next page (truncated)", kind="incomplete")
    return doc["data"]


def date_accessed(body: bytes) -> date:
    meta = json.loads(body).get("meta") or {}
    s = meta.get("date_accessed")
    if not s:
        raise _err("response has no meta.date_accessed")
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()


# ----------------------------------------------------------------------------- value helpers
def _iso_day(s: Any) -> date | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError as e:
        raise _err(f"bad date {s!r}") from e


def _value(v: Any, kind: str) -> tuple[str | None, str | None]:
    """(value, value_text) of a provider value of ``kind``; (None, None) when absent."""
    if v is None:
        return None, None
    if kind == "num":
        if isinstance(v, bool):
            raise _err(f"boolean {v!r} where a number was expected")
        if isinstance(v, Decimal):
            return dec_str(v), None
        if isinstance(v, str):
            try:
                return dec_str(Decimal(v.strip())), None
            except Exception:
                return None, v.strip()[:4000] or None
        raise _err(f"unexpected numeric value {v!r}")
    if kind == "bool":
        if isinstance(v, bool):
            return None, "yes" if v else "no"
        raise _err(f"unexpected boolean value {v!r}")
    if kind == "term":
        if isinstance(v, dict) and isinstance(v.get("term"), str):
            return None, v["term"]
        if isinstance(v, list) and all(isinstance(x, dict) and isinstance(x.get("term"), str) for x in v):
            return None, "|".join(x["term"] for x in v) or None
        raise _err(f"unexpected category value {str(v)[:80]!r}")
    raise ValueError(kind)


def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _missing_reason(entry: Mapping[str, Any]) -> str | None:
    r = (entry.get("missing_reason") or "").strip()
    return r or None


# ----------------------------------------------------------------------------- rows
class _Rows:
    def __init__(self, dataset: str, raw_sha256: str, retrieved: date) -> None:
        self.dataset = dataset
        self.raw = raw_sha256
        self.retrieved = retrieved
        self.rows: list[dict[str, Any]] = []
        self.skipped: Counter[str] = Counter()
        self._keys: set[tuple[str, str, date, date]] = set()

    def add(self, *, indicator: str, geo: str, start: date, end: date, value: str | None, value_text: str | None,
            unit: str, flags: Mapping[str, Any], selector: Mapping[str, Any]) -> None:
        if start >= end:
            self.skipped["empty_or_inverted_interval"] += 1
            return
        key = (indicator, geo, start, end)
        if key in self._keys:  # overlapping provider intervals: the first reported value is kept, counted
            self.skipped["duplicate_cell_overlapping_intervals"] += 1
            return
        self._keys.add(key)
        self.rows.append({
            "source": SOURCE, "dataset": self.dataset, "indicator": indicator, "concept_id": None, "iso3": None,
            "provider_geo": geo, "entity_qid": None, "period_start": start, "period_end": end, "value": value,
            "value_text": value_text, "unit": unit, "flags": _canon(flags), "raw_sha256": self.raw,
            "selector": _canon(selector),
        })

    def frame(self) -> pl.DataFrame:
        from reva.atlas.tables import ATLAS_SCHEMA

        return pl.DataFrame({c: [r[c] for r in self.rows] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA)


def _dated(rows: _Rows, *, entries: Any, field: str, kind: str, unit: str, geo: str, indicator: str,
           selector: dict[str, Any], base_flags: Mapping[str, Any]) -> None:
    """Interval cells + year-end readings of one dated field; undated values as of retrieval."""
    if entries is None:
        return
    if isinstance(entries, dict):  # static field
        entries = [{**entries, "date_from": "", "date_to": ""}]
    if not isinstance(entries, list):
        raise _err(f"{selector}: {field} is neither a list nor an object")
    seen: set[tuple[date, date]] = set()
    for k, e in enumerate(entries):
        sel = {**selector, "field": field, "index": k}
        val, txt = _value(e.get("value"), kind)
        reason = _missing_reason(e)
        flags: dict[str, Any] = dict(base_flags)
        if val is None and txt is None:
            if reason is None:
                rows.skipped["null_without_reason"] += 1
                continue
            flags.update({"missing": MISSING_MAP.get(reason, "provider_missing_code"), "provider_flag": reason})
        elif reason:
            flags["provider_flag"] = reason
        start = _iso_day(e.get("date_from"))
        stop = _iso_day(e.get("date_to"))
        if start is None:  # undated: the provider's current value, as of retrieval
            rows.add(indicator=indicator, geo=geo, start=rows.retrieved, end=rows.retrieved + timedelta(days=1),
                     value=val, value_text=txt, unit=unit, flags={**flags, "as_of_retrieval": True}, selector=sel)
            continue
        if start > rows.retrieved:
            rows.skipped["future_dated"] += 1
            continue
        open_ended = stop is None
        end = (rows.retrieved if open_ended else min(stop, rows.retrieved)) + timedelta(days=1)  # type: ignore[type-var]
        if (start, end) in seen:
            rows.skipped["duplicate_interval"] += 1
            continue
        seen.add((start, end))
        iflags = {**flags, **({"open_ended": True, "valid_as_of": rows.retrieved.isoformat()} if open_ended else {})}
        rows.add(indicator=indicator, geo=geo, start=start, end=end, value=val, value_text=txt, unit=unit,
                 flags=iflags, selector=sel)
        last = end - timedelta(days=1)  # inclusive last day covered by the provider's interval
        for y in range(start.year, last.year + 1):
            dec31 = date(y, 12, 31)
            if start <= dec31 <= last and dec31 <= rows.retrieved:
                rows.add(indicator=f"{indicator}|at=year_end", geo=geo, start=date(y, 1, 1), end=date(y + 1, 1, 1),
                         value=val, value_text=txt, unit=unit, selector=sel,
                         flags={**flags, "selection": f"value valid on {dec31.isoformat()}",
                                "provider_interval": f"{start.isoformat()}/{'' if open_ended else stop.isoformat()}"})  # type: ignore[union-attr]


def _chamber_kind(chamber_id: str) -> tuple[str, str]:
    geo, _, code = chamber_id.partition("-")
    kind = CHAMBER_KIND.get(code)
    if not geo or kind is None:
        raise _err(f"unexpected chamber id {chamber_id!r}")
    return geo, kind


def parline_frame(chambers: Sequence[Mapping[str, Any]], parliaments: Sequence[Mapping[str, Any]],
                  countries: Sequence[Mapping[str, Any]], *, raw: Mapping[str, str], retrieved: date) -> tuple[pl.DataFrame, dict[str, int]]:
    """``ipu.parline`` frame from the three parsed responses. ``raw``: endpoint -> raw sha256."""
    out: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    rc = _Rows("parline", raw["chambers"], retrieved)
    for c in chambers:
        geo, kind = _chamber_kind(c["id"])
        a = c.get("attributes") or {}
        base = {"ipu_chamber": c["id"]}
        if (a.get("is_abolished_chamber") or {}).get("value"):
            base["abolished_chamber"] = True
        for f, (k, unit) in CHAMBER_DATED.items():
            _dated(rc, entries=a.get(f), field=f, kind=k, unit=unit, geo=geo, indicator=f"{f}|chamber={kind}",
                   selector={"file": "chambers.json", "id": c["id"]}, base_flags=base)
        for f, (k, unit) in CHAMBER_STATIC.items():
            _dated(rc, entries=a.get(f), field=f, kind=k, unit=unit, geo=geo, indicator=f"{f}|chamber={kind}",
                   selector={"file": "chambers.json", "id": c["id"]}, base_flags=base)
    rp = _Rows("parline", raw["parliaments"], retrieved)
    for p in parliaments:
        a = p.get("attributes") or {}
        for f, (k, unit) in PARLIAMENT_DATED.items():
            _dated(rp, entries=a.get(f), field=f, kind=k, unit=unit, geo=p["id"], indicator=f,
                   selector={"file": "parliaments.json", "id": p["id"]}, base_flags={})
        fw = a.get("first_woman_in_parliament_year") or {}
        if isinstance(fw, dict) and fw.get("value"):
            y = str(fw["value"])[:4]
            if y.isdigit():
                rp.add(indicator="first_woman_in_parliament_year", geo=p["id"], start=retrieved,
                       end=retrieved + timedelta(days=1), value=str(int(y)), value_text=None, unit="year",
                       flags={"as_of_retrieval": True},
                       selector={"file": "parliaments.json", "id": p["id"], "field": "first_woman_in_parliament_year"})
        for k, s in enumerate((a.get("suffrage") or {}).get("value") or []):
            scope = ((s.get("national_or_local") or {}).get("term")) or "unspecified"
            restr = ((s.get("restricted_or_unrestricted") or {}).get("term")) or "unspecified"
            y = str(s.get("right_to_vote") or "")[:4]
            if not y.isdigit():
                skipped["suffrage_without_year"] += 1
                continue
            rp.add(indicator=f"suffrage_right_to_vote_year|scope={scope}|restriction={restr}", geo=p["id"],
                   start=retrieved, end=retrieved + timedelta(days=1), value=str(int(y)), value_text=None, unit="year",
                   flags={"as_of_retrieval": True, **({"detail": str(s["detail_of_restrictions"])[:500]}
                                                      if s.get("detail_of_restrictions") else {})},
                   selector={"file": "parliaments.json", "id": p["id"], "field": "suffrage", "index": k})
    rk = _Rows("parline", raw["countries"], retrieved)
    for c in countries:
        a = c.get("attributes") or {}
        for f, (k, unit) in COUNTRY_DATED.items():
            _dated(rk, entries=a.get(f), field=f, kind=k, unit=unit, geo=c["id"], indicator=f,
                   selector={"file": "countries.json", "id": c["id"]}, base_flags={})
    for r in (rc, rp, rk):
        out.extend(r.rows)
        skipped.update(r.skipped)
    out = _dedupe_suffrage(out, skipped)
    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame({c: [r[c] for r in out] for c in ATLAS_SCHEMA.names()}, schema=ATLAS_SCHEMA), dict(skipped)


def _dedupe_suffrage(rows: list[dict[str, Any]], skipped: Counter[str]) -> list[dict[str, Any]]:
    """Two suffrage entries with the same scope/restriction: keep the earliest year, flag the count."""
    key = defaultdict(list)
    for i, r in enumerate(rows):
        if r["indicator"].startswith("suffrage_right_to_vote_year"):
            key[(r["indicator"], r["provider_geo"])].append(i)
    drop: set[int] = set()
    for idx in key.values():
        if len(idx) > 1:
            idx.sort(key=lambda i: int(rows[i]["value"]))
            keep = rows[idx[0]]
            flags = json.loads(keep["flags"])
            flags["provider_entries"] = [rows[i]["value"] for i in idx]
            keep["flags"] = _canon(flags)
            drop.update(idx[1:])
            skipped["suffrage_same_category_merged"] += len(idx) - 1
    return [r for i, r in enumerate(rows) if i not in drop]


def elections_frame(elections: Sequence[Mapping[str, Any]], *, raw_sha256: str, retrieved: date) -> tuple[pl.DataFrame, dict[str, int]]:
    """``ipu.elections`` frame (exact election date ranges + unique-in-year calendar cells)."""
    rows = _Rows("elections", raw_sha256, retrieved)
    per_year: dict[tuple[str, str, int], list[tuple[Mapping[str, Any], date, date]]] = defaultdict(list)
    parsed: list[tuple[Mapping[str, Any], str, str, date, date]] = []
    for e in elections:
        a = e.get("attributes") or {}
        ch = ((a.get("chamber") or {}).get("value")) or ""
        geo, kind = _chamber_kind(ch)
        d = (a.get("election_date") or {}).get("value") or {}
        start = _iso_day(d.get("from"))
        if start is None:
            rows.skipped["election_without_date"] += 1
            continue
        stop = _iso_day(d.get("to")) or start
        if stop < start:
            rows.skipped["election_date_inverted"] += 1
            continue
        parsed.append((e, geo, kind, start, stop))
        per_year[(geo, kind, start.year)].append((e, start, stop))
    for e, geo, kind, start, stop in parsed:
        a = e.get("attributes") or {}
        base: dict[str, Any] = {"ipu_election": e["id"], "ipu_chamber": a["chamber"]["value"]}
        if start > retrieved:  # an announced date, not a held election
            base["scheduled_after_retrieval"] = retrieved.isoformat()
        end = stop + timedelta(days=1)
        unique = len(per_year[(geo, kind, start.year)]) == 1
        label = start.isoformat() if stop == start else f"{start.isoformat()}/{stop.isoformat()}"
        sel = {"file": "elections.json", "id": e["id"], "field": "election_date"}
        rows.add(indicator=f"election_date|chamber={kind}", geo=geo, start=start, end=end, value=None,
                 value_text=label, unit="date", flags=base, selector=sel)
        if unique:
            rows.add(indicator=f"election_date|chamber={kind}|at=year", geo=geo, start=date(start.year, 1, 1),
                     end=date(start.year + 1, 1, 1), value=None, value_text=label, unit="date",
                     flags={**base, "selection": "the only election of this chamber in the year"}, selector=sel)
        for f, (k, unit) in ELECTION_FIELDS.items():
            ent = a.get(f)
            if not isinstance(ent, dict):
                continue
            val, txt = _value(ent.get("value"), k)
            reason = _missing_reason(ent)
            flags = dict(base)
            if val is None and txt is None:
                if reason is None:
                    continue
                flags.update({"missing": MISSING_MAP.get(reason, "provider_missing_code"), "provider_flag": reason})
            sel = {"file": "elections.json", "id": e["id"], "field": f}
            rows.add(indicator=f"{f}|chamber={kind}", geo=geo, start=start, end=end, value=val, value_text=txt,
                     unit=unit, flags=flags, selector=sel)
            if unique:
                rows.add(indicator=f"{f}|chamber={kind}|at=year", geo=geo, start=date(start.year, 1, 1),
                         end=date(start.year + 1, 1, 1), value=val, value_text=txt, unit=unit, selector=sel,
                         flags={**flags, "selection": "the only election of this chamber in the year",
                                "election_dates": label})
    return rows.frame(), dict(rows.skipped)


# ----------------------------------------------------------------------------- ingest
def ingest(engine: Any, store: Any, raws: Mapping[str, RawAcquisition], *, origin: str,
           actor: str = "worker:atlas_ipu") -> dict[str, Any]:
    from reva.adapters.connectors import atlas_sci_common as common
    from reva.atlas.tables import register_atlas_table

    data = {k.split(".", 1)[1]: parse_response(r.body, k) for k, r in raws.items()}
    missing = sorted(set(ENDPOINTS) - set(data))
    if missing:
        raise _err(f"missing responses {missing}", kind="incomplete")
    retrieved = min(date_accessed(r.body) for r in raws.values())
    spec = connector_spec()
    shas = {k.split(".", 1)[1]: common.archive_raw(engine, store, spec, r, actor=actor) for k, r in raws.items()}
    pf, pskip = parline_frame(data["chambers"], data["parliaments"], data["countries"], raw=shas, retrieved=retrieved)
    ef, eskip = elections_frame(data["elections"], raw_sha256=shas["elections"], retrieved=retrieved)
    out: dict[str, Any] = {"retrieved": retrieved.isoformat()}
    for ds, frame, rawlist, skip in (
        ("parline", pf, [shas["chambers"], shas["parliaments"], shas["countries"]], pskip),
        ("elections", ef, [shas["elections"]], eskip),
    ):
        out[ds] = register_atlas_table(
            engine, store, source=SOURCE, dataset=ds, frame=frame, raw_sha256s=rawlist,
            parser_version=PARSER_VERSION, origin=origin, geo_scheme="iso2",
            coverage_note=(f"IPU Parline API retrieved {retrieved.isoformat()}; provider validity intervals kept, "
                           f"year-end readings selected (|at=year_end); not loaded: {json.dumps(skip, sort_keys=True)}. "
                           "Licence CC BY-NC-SA 4.0 (NON-COMMERCIAL)."),
            rights=RIGHTS, actor=actor,
        )
        out[ds]["not_loaded"] = skip
    return out


def unmapped_codes(records: Iterable[Mapping[str, Any]]) -> list[str]:
    from reva.atlas.tables import country_iso3

    return sorted({r["id"] for r in records if country_iso3(r["id"], "iso2") is None})


__all__ = [
    "ENDPOINTS",
    "RIGHTS",
    "connector_spec",
    "elections_frame",
    "fetch",
    "fetch_items",
    "ingest",
    "parline_frame",
    "parse_response",
]
