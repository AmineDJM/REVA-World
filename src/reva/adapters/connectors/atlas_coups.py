"""Coups d'État: Powell & Thyne "Global Instances of Coups, 1950 - present" (agent A-COUPS, V3 causality).

Source and access
-----------------
Jonathan Powell & Clayton Thyne publish the coup list on ``https://jonathanmpowell.com/coups/`` as a CSV whose
file name carries the update date (``wp-content/uploads/2026/08/pt_20260829.csv`` on 2026-10-16). The former
location (``www.uky.edu/~clthyn2/coup_data/powell_thyne_coups_final.txt``) answers HTTP 403 since 2026. Every run
fetches the page first (one request), takes the newest ``pt_YYYYMMDD.csv`` link on it, and archives both: the
page is the evidence for the coup definition quoted below and for the provider's note on provisional codings.

Licence: **none stated** (checked 2026-10-16). The page asks users to cite the article (Powell & Thyne 2011,
Journal of Peace Research 48(2): 249-259) and says the data "are updated regularly". Free public download,
no registration; redistribution and commercial use are not granted by any licence (recorded in rights, like
CCP in A-LAW). Definition (quoted from the archived page): coups are "illegal and overt attempts by the
military or other elites within the state apparatus to unseat the sitting executive"; "Successes are treated as
episodes in which the perpetrators (or their agents) control power for at least 7 days." ``coup`` 2 =
successful, 1 = failed. Country code = Correlates of War ``ccode`` (``ccode_gw``/``ccode_polity`` are empty in
the 2026 file).

Outputs
-------
* atlas ``powell_thyne.coup_events`` -- one row per coup event (indicator ``COUP_EVENT``, period = the day
  ``[d, d+1)``, ``value`` = provider code 1/2, ``value_text`` = successful/failed, flags = the provider's country
  name, dataset version, event id).
* atlas ``powell_thyne.coups_cy`` -- **computed** counts per country-year (``COUP_EVENTS``,
  ``COUPS_SUCCESSFUL``, ``COUPS_FAILED``), exactly the number of listed events; only country-years with at least
  one event are emitted (a year without a row is not a reported zero, although the list is meant to be global).
* KG: one event entity per coup ``ptcoup:<ccode>:<YYYY-MM-DD>`` (kind ``event``; P&T has no event ids, and no
  two events share code and day), statements: ``coup:event_type`` = ``coup``, ``coup:outcome``, ``P585`` (point in
  time, day precision; what the causal grader reads), ``coup:against_state`` (the state the coup targeted: a
  Wikidata QID through the crosswalk, else ``cow:<code>``). Leaders removed are linked by
  :mod:`reva.adapters.connectors.atlas_leaders` (Archigos), never here: P&T do not code the leader.

COW codes map to crosswalk entities through ``StateCodeCrosswalk`` (KGGEOPOL, date-aware, at the event day;
countrycode 1.9.0 panel fetched and archived by every run). Historical states (South Vietnam 817, South Yemen
680, the USSR for 365 in 1991) stay ``iso3 = null`` with their QID.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import (
    COUNTRYCODE_MEMBER,
    COUNTRYCODE_VERSION,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
    atlas_frame,
    code_correspondence,
    fetch_countrycode,
    kg_available,
    load_qid_for_iso3,
)
from reva.kernel.errors import SourceResponseError

SOURCE_KEY = "powell_thyne.coups"
CONNECTOR_NAME = "atlas.coups"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-coups-pt-1"
ATLAS_SOURCE = "powell_thyne"
HOST = "jonathanmpowell.com"
PAGE_URL = f"https://{HOST}/coups/"
#: newest-first discovery of the coup list on the page (the file name carries the update date)
CSV_LINK_RE = re.compile(
    r"https://jonathanmpowell\.com/wp-content/uploads/(\d{4})/(\d{2})/pt_(\d{8})(?:-\d{1,2})?\.csv"
)
MAX_PAGE_BYTES = 2_000_000
MAX_CSV_BYTES = 2_000_000
MIN_ROWS_LIVE = 450  # 497 events on 2026-08-29; fewer than this = truncated or a different file
REQUIRED_COLUMNS = ("country", "ccode", "year", "month", "day", "coup", "version")
OUTCOME = {"2": "successful", "1": "failed"}
VERSION_RE = re.compile(r"^V\d{4}\.\d{2}\.\d{2}$")
DEFINITION_RE = re.compile(r"We define coups as\s*(.{10,400}?)\s*Successes are treated as\s*(.{10,300}?\.)", re.S)
NOTE_RE = re.compile(r"Note\s*\((\d{1,2})\.(\d{1,2})\.(\d{4})\)\s*:\s*(.{1,600}?)(?:\s+This project is|\Z)", re.S)

#: The definitions REVA quotes when it presents P&T events (verbatim from the archived page; checked by
#: :func:`parse_page` on every run: a changed definition fails the run as schema drift).
DEFINITION_COUP = (
    "“illegal and overt attempts by the military or other elites within the state apparatus to unseat the "
    "sitting executive.”"
)
DEFINITION_SUCCESS = "episodes in which the perpetrators (or their agents) control power for at least 7 days."

RIGHTS: dict[str, str] = {
    "licence": "No licence stated (checked 2026-10-16): free public download without registration; the authors "
    "ask users to cite Powell & Thyne (2011). No redistribution or commercial-use grant.",
    "licence_url": PAGE_URL,
    "attribution": "Powell, Jonathan M. & Clayton L. Thyne (2011). Global instances of coups from 1950 to 2010: A "
    "new dataset. Journal of Peace Research 48(2): 249-259 (data updated regularly at jonathanmpowell.com/coups)",
    "commercial_use": "not_granted",
    "redistribution": "not_granted",
}


class CoupParseError(SourceResponseError):
    pass


def _drift(msg: str) -> CoupParseError:
    return CoupParseError(msg, retryable=False, kind="schema_drift")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


# ----------------------------------------------------------------------------- page


@dataclass(frozen=True)
class PageInfo:
    csv_url: str
    csv_stamp: str  # YYYYMMDD from the file name
    definition_coup: str
    definition_success: str
    note_date: date | None
    note_text: str | None


def parse_page(body: bytes) -> PageInfo:
    """Newest coup-list CSV link, the quoted definitions and the provider note (fail closed on drift)."""
    try:
        page = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _drift("coups page is not UTF-8") from e
    links = {m.group(0): m.group(3) for m in CSV_LINK_RE.finditer(page)}
    if not links:
        raise _drift("no pt_YYYYMMDD.csv link on the coups page")
    url, stamp = max(links.items(), key=lambda kv: (kv[1], kv[0]))
    body_html = re.split(r"<body[^>]*>", page, maxsplit=1)[-1]  # the <head> repeats the note, truncated
    plain = _text(body_html)
    m = DEFINITION_RE.search(plain)
    if not m:
        raise _drift("coup definition not found on the coups page")
    d_coup, d_succ = m.group(1).strip(), m.group(2).strip()
    if d_coup != DEFINITION_COUP or d_succ != DEFINITION_SUCCESS:
        raise _drift(f"coup definition changed on the page: {d_coup!r} / {d_succ!r}")
    note_date = note_text = None
    n = NOTE_RE.search(plain)
    if n:
        try:
            note_date = date(int(n.group(3)), int(n.group(1)), int(n.group(2)))
        except ValueError:
            note_date = None
        note_text = n.group(4).strip()[:600]
    return PageInfo(url, stamp, d_coup, d_succ, note_date, note_text)


# ----------------------------------------------------------------------------- CSV


@dataclass(frozen=True)
class CoupEvent:
    row: int  # 1-based line number in the CSV (header = 1)
    country: str
    ccode: int
    day: date
    coup: str  # "1" | "2"
    version: str

    @property
    def outcome(self) -> str:
        return OUTCOME[self.coup]

    @property
    def event_id(self) -> str:
        return event_id(self.ccode, self.day)


def event_id(ccode: int, day: date) -> str:
    return f"ptcoup:{ccode}:{day.isoformat()}"


def parse_csv(body: bytes, *, strict: bool = True) -> list[CoupEvent]:
    """Every row validated (code, calendar date, outcome code, one dataset version); ``strict`` adds the
    truncation guard (relaxed for trimmed fixtures only)."""
    try:
        txt = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise _drift("coup list is not UTF-8") from e
    reader = csv.DictReader(io.StringIO(txt, newline=""))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise _drift(f"coup list columns missing: {missing} (have {reader.fieldnames})")
    out: list[CoupEvent] = []
    seen: dict[tuple[int, date], int] = {}
    for i, r in enumerate(reader, start=2):
        if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
            continue
        try:
            cc = int(r["ccode"].strip())
            d = date(int(r["year"]), int(r["month"]), int(r["day"]))
        except (TypeError, ValueError) as e:
            raise _drift(f"coup list line {i}: invalid ccode/date {r!r}") from e
        coup = (r["coup"] or "").strip()
        if coup not in OUTCOME:
            raise _drift(f"coup list line {i}: coup code {coup!r} not in {{1, 2}}")
        ver = (r["version"] or "").strip()
        if not VERSION_RE.match(ver):
            raise _drift(f"coup list line {i}: version {ver!r}")
        if (cc, d) in seen:  # P&T have no event id: (code, day) must stay a key
            raise _drift(f"coup list lines {seen[(cc, d)]} and {i}: two events with the same code and day")
        seen[(cc, d)] = i
        out.append(CoupEvent(i, (r["country"] or "").strip(), cc, d, coup, ver))
    versions = {e.version for e in out}
    if len(versions) != 1:
        raise _drift(f"coup list: several dataset versions {sorted(versions)}")
    if strict and len(out) < MIN_ROWS_LIVE:
        raise CoupParseError(f"coup list: {len(out)} rows < {MIN_ROWS_LIVE} (truncated?)", retryable=False, kind="incomplete")
    return out


def csv_file_name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


# ----------------------------------------------------------------------------- atlas rows


def _base_flags(e: CoupEvent, res: Any) -> dict[str, Any]:
    flags: dict[str, Any] = {"pt_country": e.country, "cow_code": e.ccode, "code_resolution": res.reason}
    if res.iso3 is None and res.label:
        flags["historical_state"] = res.label
    return flags


def event_rows(
    events: Sequence[CoupEvent], *, raw_sha256: str, crosswalk: StateCodeCrosswalk, file: str, page: PageInfo | None
) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj

    out = []
    for e in events:
        res = crosswalk.resolve("cow", e.ccode, e.day)
        flags = _base_flags(e, res)
        flags.update(event_id=e.event_id, dataset_version=e.version, outcome_code_meaning="2=successful; 1=failed")
        if page and page.note_date and e.day >= page.note_date and "tentative" in (page.note_text or "").lower():
            flags["provisional"] = f"provider note ({page.note_date.isoformat()}): {page.note_text}"
        out.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": "coup_events",
                "indicator": "COUP_EVENT",
                "concept_id": None,
                "iso3": res.iso3,
                "provider_geo": f"cow:{e.ccode}",
                "entity_qid": res.qid,
                "period_start": e.day,
                "period_end": e.day + timedelta(days=1),
                "value": e.coup,
                "value_text": e.outcome,
                "unit": "code",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=file, row=e.row),
            }
        )
    return out


CY_INDICATORS: dict[str, tuple[str, frozenset[str]]] = {
    "COUP_EVENTS": ("count of listed coup events (successful + failed) dated in the year", frozenset({"1", "2"})),
    "COUPS_SUCCESSFUL": ("count of listed coup events coded 2 (successful) dated in the year", frozenset({"2"})),
    "COUPS_FAILED": ("count of listed coup events coded 1 (failed) dated in the year", frozenset({"1"})),
}


def country_year_rows(
    events: Sequence[CoupEvent], *, raw_sha256: str, crosswalk: StateCodeCrosswalk, file: str
) -> list[dict[str, Any]]:
    """Exact counts of listed events per (COW code, year, indicator); only non-zero cells."""
    from reva.atlas.tables import json_obj, year_period

    groups: dict[tuple[str, int, int], list[CoupEvent]] = defaultdict(list)
    for e in events:
        for ind, (_d, codes) in CY_INDICATORS.items():
            if e.coup in codes:
                groups[(ind, e.ccode, e.day.year)].append(e)
    out = []
    for (ind, cc, year), evs in sorted(groups.items()):
        res = crosswalk.resolve("cow", cc, date(year, 7, 1))
        # an event-day resolution that differs from mid-year (e.g. a state born or dissolved that year) is said
        day_res = sorted({(crosswalk.resolve("cow", cc, e.day).iso3 or "-") for e in evs})
        flags = _base_flags(evs[0], res)
        flags.update(
            computed_from=f"Powell & Thyne coup list {evs[0].version}",
            method=CY_INDICATORS[ind][0],
            event_ids=[e.event_id for e in evs],
        )
        if day_res != [res.iso3 or "-"]:
            flags["event_day_iso3"] = day_res
        start, end = year_period(year)
        out.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": "coups_cy",
                "indicator": ind,
                "concept_id": None,
                "iso3": res.iso3,
                "provider_geo": f"cow:{cc}",
                "entity_qid": res.qid,
                "period_start": start,
                "period_end": end,
                "value": str(len(evs)),
                "value_text": None,
                "unit": "events",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=file, rows=[e.row for e in evs]),
            }
        )
    return out


# ----------------------------------------------------------------------------- KG

#: Dataset predicates: pid -> (value kind, reva predicate, en, fr, inverse name, inverse en, inverse fr)
COUP_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None, str | None]] = {
    "coup:event_type": ("string", "coup_event_type", "event type (coup dataset)", "type d'événement (coups)",
                        None, None, None),
    "coup:outcome": ("string", "coup_outcome", "coup outcome as coded (successful / failed)",
                     "issue du coup d'État telle que codée (réussi / échoué)", None, None, None),
    "coup:against_state": ("item", "coup_against_state", "coup attempt against the government of",
                           "tentative de coup d'État contre le gouvernement de", "coup_attempt_in",
                           "coup attempts in", "tentatives de coup d'État"),
    "coup:leader_removed": ("item", "coup_leader_removed", "leader removed (coded by Archigos, same state and day)",
                            "dirigeant renversé (codé par Archigos, même État et même jour)", "removed_in_coup",
                            "removed in coup event", "renversé lors du coup d'État"),
}  # fmt: skip


def kg_properties() -> list[Any]:
    """The dataset predicates + Wikidata P585 exactly as WDHARVEST's allow-list defines it (the causal grader
    reads event dates from P585)."""
    from reva.kg.model import KgProperty

    props = [
        KgProperty(pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv,
                   inverse_label_en=ile, inverse_label_fr=ilf)
        for p, (k, rp, le, lf, inv, ile, ilf) in COUP_PROPERTIES.items()
    ]  # fmt: skip
    return props + wikidata_time_properties()


def wikidata_time_properties(pids: Sequence[str] = ("P585",)) -> list[Any]:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties as wd_kg_properties

    return [p for p in wd_kg_properties(load_allowlist()) if p.pid in pids]


def kg_records(
    events: Sequence[CoupEvent], *, raw_sha256: str, resolver: StateEntityResolver, file: str, page: PageInfo | None
) -> tuple[list[KgEntity], list[KgStatement]]:
    ents: list[KgEntity] = []
    sts: list[KgStatement] = []
    for e in events:
        eid = e.event_id
        sel = {"file": file, "row": e.row}
        res = resolver.crosswalk.resolve("cow", e.ccode, e.day)
        place = res.label or e.country
        ents.append(
            KgEntity(
                qid=eid,
                kind="event",
                labels={
                    "en": f"{'Coup' if e.coup == '2' else 'Failed coup attempt'} in {e.country}, {e.day.isoformat()}",
                    "fr": f"{'Coup d’État' if e.coup == '2' else 'Tentative de coup d’État échouée'} "
                    f"({e.country}, {e.day.isoformat()})",
                },
                descriptions={"en": f"coup event as listed by Powell & Thyne ({e.version}); state: {place}"},
            )
        )
        q = {"dataset_version": e.version, "pt_country": e.country, "cow_code": e.ccode}
        if page and page.note_date and e.day >= page.note_date and "tentative" in (page.note_text or "").lower():
            q["provisional"] = f"provider note ({page.note_date.isoformat()}): {page.note_text}"
        day = e.day.isoformat()
        sts += [
            KgStatement(key=f"{eid}:type", subject=eid, property="coup:event_type",
                        value={"type": "string", "value": "coup"}, qualifiers=q, raw_sha256=raw_sha256, selector=sel),
            KgStatement(key=f"{eid}:outcome", subject=eid, property="coup:outcome",
                        value={"type": "string", "value": e.outcome}, point_in_time=day, pit_precision=11,
                        qualifiers={**q, "provider_code": e.coup, "definition_success": DEFINITION_SUCCESS},
                        raw_sha256=raw_sha256, selector=sel),
            KgStatement(key=f"{eid}:date", subject=eid, property="P585",
                        value={"type": "time", "time": day, "precision": 11}, qualifiers=q,
                        raw_sha256=raw_sha256, selector=sel),
            KgStatement(key=f"{eid}:state", subject=eid, property="coup:against_state",
                        object_qid=resolver.entity("cow", e.ccode, e.day, e.country), point_in_time=day,
                        pit_precision=11, qualifiers={**q, "code_resolution": res.reason},
                        raw_sha256=raw_sha256, selector=sel),
        ]  # fmt: skip
    return ents, sts


def load_kg_records(
    engine: Any,
    store: Any,
    entities: Sequence[KgEntity],
    statements: Sequence[KgStatement],
    *,
    properties: Sequence[Any],
    source_key: str,
    source_revision: str,
    raw_sha256s: Sequence[str],
    retrieved_at: Any,
    origin: str,
) -> dict[str, Any]:
    """Register ``properties`` and load entities then statements in bounded batches (``reva.kg.model``)."""
    from collections import Counter

    from reva.adapters.connectors.atlas_geopol_common import to_kg_model
    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_STATEMENTS_PER_BATCH, load_batch, register_properties

    register_properties(engine, properties)
    totals: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    samples: list[tuple[str, str | None]] = []
    ents, sts = to_kg_model(entities, statements, source_key=source_key, source_revision=source_revision)
    chunks = [(ents[i : i + MAX_ENTITIES_PER_BATCH], []) for i in range(0, len(ents), MAX_ENTITIES_PER_BATCH)]
    chunks += [([], sts[i : i + MAX_STATEMENTS_PER_BATCH]) for i in range(0, len(sts), MAX_STATEMENTS_PER_BATCH)]
    for be, bs in chunks:
        used = sorted({s.raw_sha256 for s in bs if s.raw_sha256} or set(raw_sha256s))
        rep = load_batch(engine, store, entities=be, statements=bs, source_key=source_key, raw_sha256s=used,
                         retrieved_at=retrieved_at, origin=origin)  # fmt: skip
        for k in ("entities_inserted", "entities_unchanged", "statements_inserted", "statements_unchanged",
                  "statements_new_revision", "statements_conflicting", "statements_stale"):  # fmt: skip
            totals[k] += getattr(rep, k)
        rejected.update(rep.rejected_counts)
        samples.extend((r.reason, r.key) for r in rep.rejected[:5])
    return {**totals, "rejected": dict(rejected), "rejected_sample": samples[:20], "batches": len(chunks)}


# ----------------------------------------------------------------------------- build / fetch / ingest


def build(
    page_body: bytes | None,
    csv_body: bytes,
    *,
    csv_url: str,
    crosswalk: StateCodeCrosswalk,
    qid_for_iso3: Mapping[str, str],
    countrycode_sha256: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Parse raw bytes into both atlas tables and the KG records (no I/O)."""
    page = parse_page(page_body) if page_body is not None else None
    sha = hashlib.sha256(csv_body).hexdigest()
    file = csv_file_name(csv_url)
    events = parse_csv(csv_body, strict=strict)
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    ents, sts = kg_records(events, raw_sha256=sha, resolver=resolver, file=file, page=page)
    ents = ents + resolver.code_entities()
    if countrycode_sha256:
        sts = sts + code_correspondence(resolver, raw_sha256=countrycode_sha256)
    return {
        "sha256": sha,
        "page": page,
        "events": events,
        "coup_events": event_rows(events, raw_sha256=sha, crosswalk=crosswalk, file=file, page=page),
        "coups_cy": country_year_rows(events, raw_sha256=sha, crosswalk=crosswalk, file=file),
        "entities": ents,
        "statements": sts,
        "version": events[0].version if events else None,
        "unresolved_state_codes": {k: sorted(v) for k, v in resolver.unresolved.items()},
    }


async def fetch_coup_list(run: Any) -> tuple[Any, Any, PageInfo]:
    """Page + newest CSV through the run's client (not archived yet: callers archive after parsing)."""
    page_raw = await run.get(PAGE_URL, key="powell_thyne:coups_page", media_types=("text/html",),
                             max_bytes=MAX_PAGE_BYTES)  # fmt: skip
    page = parse_page(page_raw.body)
    if not page.csv_url.startswith(f"https://{HOST}/"):
        raise _drift("coup list link leaves the provider host")
    csv_raw = await run.get(page.csv_url, key=f"powell_thyne:{page.csv_stamp}",
                            media_types=("text/csv", "application/octet-stream"), max_bytes=MAX_CSV_BYTES)  # fmt: skip
    return page_raw, csv_raw, page


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Jonathan Powell & Clayton Thyne (University of Central Florida / University of Kentucky)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST, "cran.r-project.org"),
        datasets=("coups_cy", "coup_events"),
        dataset_ids=("powell-thyne-coups:latest", f"countrycode-{COUNTRYCODE_VERSION}"),
        cadence_seconds=30 * 86400,
        planned_requests=4,
        max_response_bytes=5_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "geo": "Correlates of War ccode (date-aware crosswalk at the event day / mid-year)",
            "time": "event day; country-year counts by calendar year",
            "kg": "ptcoup:<ccode>:<YYYY-MM-DD> event entities",
        },
        notes="The coups page is fetched first (newest pt_YYYYMMDD.csv link + definitions); also fetches the "
        "countrycode R package (CRAN) for COW -> ISO3.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    crosswalk, cc_sha = await fetch_countrycode(run)
    page_raw, csv_raw, page = await fetch_coup_list(run)
    out = build(
        page_raw.body,
        csv_raw.body,
        csv_url=page.csv_url,
        crosswalk=crosswalk,
        qid_for_iso3=load_qid_for_iso3(),
        countrycode_sha256=cc_sha,
        strict=run.origin == "live",
    )
    page_sha = run.archive(page_raw)  # parsed first: nothing archived on drift
    sha = run.archive(csv_raw)
    res: dict[str, Any] = {
        "csv_url": page.csv_url,
        "dataset_version": out["version"],
        "events": len(out["events"]),
        "successful": sum(e.coup == "2" for e in out["events"]),
        "failed": sum(e.coup == "1" for e in out["events"]),
        "years": [min(e.day.year for e in out["events"]), max(e.day.year for e in out["events"])],
        "provider_note": {"date": page.note_date.isoformat() if page.note_date else None, "text": page.note_text},
        "atlas": {},
    }
    note = (
        f"Powell & Thyne coup list {out['version']} ({csv_file_name(page.csv_url)}), "
        f"{res['years'][0]}-{res['years'][1]}, COW codes"
    )
    rights = {**RIGHTS, "definition_coup": page.definition_coup, "definition_success": page.definition_success,
              "definition_evidence_sha256": page_sha}  # fmt: skip
    for ds, cov in (
        ("coup_events", note + "; one row per listed event (value 2 = successful, 1 = failed)"),
        ("coups_cy", "COMPUTED from the " + note + "; exact counts of listed events per country-year, only "
         "country-years with at least one event"),
    ):  # fmt: skip
        if run.wants(ds):
            r = run.register(dataset=ds, frame=atlas_frame(out[ds]), raw_sha256s=[sha], rights=rights,
                             coverage_note=cov)  # fmt: skip
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = load_kg_records(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            properties=kg_properties() + _geopol_correspondence_property(),
            source_key=SOURCE_KEY,
            source_revision=out["version"],
            raw_sha256s=sorted({sha, cc_sha}),
            retrieved_at=csv_raw.retrieved_at,
            origin=run.origin,
        )
    res["unresolved_state_codes"] = {k: v[:3] for k, v in out["unresolved_state_codes"].items()}
    res["countrycode_member"] = COUNTRYCODE_MEMBER
    return res


def _geopol_correspondence_property() -> list[Any]:
    """``geopol:corresponds_to_iso3`` (KGGEOPOL's definition, reused unchanged for ``cow:<code>`` entities)."""
    from reva.adapters.connectors.atlas_geopol_common import kg_properties as geopol_props

    return [p for p in geopol_props() if p.pid == "geopol:corresponds_to_iso3"]
