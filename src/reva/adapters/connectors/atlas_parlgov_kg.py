"""ParlGov cabinets, parties and elections as dated KG events + cabinet atlas tables (agent V6POLITICS).

Source and licence
------------------
**ParlGov 2024 stable release** (Döring & Manow, *Parliaments and governments database*), Harvard Dataverse
doi:10.7910/DVN/2VZ5ZC, licence **CC0 1.0** (the Dataverse record's ``license.name``; checked on every run together
with the MD5 of each file, exactly as A-SOCIETY's :mod:`atlas_society_gov` does for ``view_election``). Files:
``view_cabinet.csv`` (one row per party in parliament per cabinet: ``cabinet_party`` 1 = in the cabinet,
``prime_minister`` 1 = the prime minister's party, ``caretaker``, ``start_date``, ``previous_cabinet_id``),
``view_party.csv`` (party names, ParlGov party family, left-right 0-10, Manifesto Project id) and
``view_election.csv`` (party vote shares and seats; A-SOCIETY already loads them as ``atlas.parlgov.elections``,
the bytes are re-fetched here for the election events and are deduplicated by sha256 in the archive). 37 EU/OECD
democracies; elections since 1900, cabinets 1900-2023.

Outputs
-------
* KG (source ``parlgov.dataverse``): parties ``parlgov:party:<party_id>`` (kind ``political_party``),
  elections ``parlgov:election:<election_id>`` (kind ``event``: ``P585`` election day, ``P17`` country,
  ``parlgov:election_type``, ``parlgov:seats_total``, ``parlgov:previous_election``, one ``parlgov:party_result``
  per party with vote share and seats as qualifiers -- verbatim), cabinets ``parlgov:cabinet:<cabinet_id>`` (kind
  ``cabinet``; ``P580`` = ``start_date``, the day the cabinet took office; ``P17``; ``parlgov:cabinet_name``;
  ``parlgov:caretaker``; ``parlgov:after_election``; ``parlgov:previous_cabinet``; ``parlgov:cabinet_party`` per
  party with ``cabinet_party`` = 1; ``parlgov:prime_minister_party``). ParlGov publishes no end date per cabinet:
  none is stated (the next cabinet's start is reachable through ``parlgov:previous_cabinet`` in reverse).
  Countries: ParlGov's ``country_name_short`` is ISO 3166 alpha-3; P17 points to the country QID of
  ``registry/entities/country_qids.json`` (a code without a QID gets no P17 and is counted, never guessed).
* atlas ``parlgov.cabinets``: one-day period = ``start_date``, per ISO3: ``CABINET_PARTIES`` (number of rows with
  ``cabinet_party`` = 1, computed), ``CABINET_SEATS`` (sum of their ``seats``, computed), ``PARLIAMENT_SEATS``
  (``election_seats_total``, provider) and ``CABINET_CARETAKER`` (provider code 1/0); cabinet id, name, election and
  previous cabinet in flags.
* atlas ``parlgov.cabinets_cy``: exact counts per ISO3 x calendar year of cabinets that took office
  (``CABINETS_STARTED``, ``CARETAKER_CABINETS_STARTED``); only non-zero cells (a year without a row is "no cabinet
  start listed", which ParlGov's coverage makes a zero only inside its covered period).

Nothing is causal: an election "before" a cabinet is ParlGov's parliament reference (``election_id``), not a cause.
"""

from __future__ import annotations

import csv
import io
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from reva.adapters.connectors import v6politics_common as vc
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement
from reva.adapters.connectors.atlas_society_common import (
    DATAVERSE,
    S3_HOST,
    RowBuffer,
    canon,
    check_dataverse_record,
    dataverse_file_url,
    dataverse_record_url,
    fetch_archive,
    num,
    year_period,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem

CONNECTOR = "atlas.parlgov_cabinets"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_parlgov_kg/1"
SOURCE_KEY = "parlgov.dataverse"
ATLAS_SOURCE = "parlgov"
DOI = "doi:10.7910/DVN/2VZ5ZC"
FILES: dict[str, tuple[int, str]] = {
    "cabinet": (10437088, "view_cabinet.csv"),
    "party": (10437089, "view_party.csv"),
    "election": (10437092, "view_election.csv"),
}
MIN_ROWS = {"cabinet": 12000, "party": 1600, "election": 8500}
DATASETS = ("cabinets", "cabinets_cy")
RIGHTS: dict[str, Any] = {
    "licence": "CC0-1.0",
    "licence_url": "http://creativecommons.org/publicdomain/zero/1.0",
    "open_licence": True,
    "attribution": "Döring, Holger and Philip Manow. 2024. Parliaments and governments database (ParlGov): "
    "Information on parties, elections and cabinets in established democracies. Stable version 2024 (12 August "
    "2024), Harvard Dataverse doi:10.7910/DVN/2VZ5ZC.",
    "source_key": SOURCE_KEY,
}
CAB_COLS = ("country_name_short", "country_name", "election_date", "start_date", "cabinet_name", "caretaker",
            "cabinet_party", "prime_minister", "seats", "election_seats_total", "party_name_short", "party_id",
            "election_id", "cabinet_id", "previous_cabinet_id")  # fmt: skip
PARTY_COLS = ("country_name_short", "country_name", "party_name_short", "party_name_english", "party_name",
              "family_name", "left_right", "cmp", "party_id")  # fmt: skip
ELEC_COLS = ("country_name_short", "country_name", "election_type", "election_date", "vote_share", "seats",
             "seats_total", "party_id", "election_id", "previous_parliament_election_id")  # fmt: skip
ELECTION_TYPES = {"parliament": "national parliament election", "ep": "European Parliament election"}


def fetch_items() -> tuple[FetchItem, ...]:
    media = ("text/csv", "application/octet-stream", "text/plain")
    return (
        FetchItem(key="parlgov.record", url=dataverse_record_url(DOI), max_bytes=2_000_000),
        *(
            FetchItem(key=f"parlgov.{k}", url=dataverse_file_url(fid, original=True), max_bytes=10_000_000,
                      expected_media_types=media)
            for k, (fid, _) in FILES.items()
        ),
    )  # fmt: skip


def _rows(body: bytes, name: str, required: Sequence[str], min_rows: int) -> list[dict[str, str]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise vc.err(f"{name}: not UTF-8 ({e})") from e
    rd = csv.DictReader(io.StringIO(text))
    missing = [c for c in required if c not in (rd.fieldnames or [])]
    if missing:
        raise vc.err(f"{name}: missing columns {missing}")
    rows = list(rd)
    if len(rows) < min_rows:
        raise vc.err(f"{name}: only {len(rows)} rows (expected >= {min_rows}): truncated file?")
    return rows


def _day(text: str, what: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as e:
        raise vc.err(f"{what}: {text!r} is not an ISO date") from e


def _id(text: str, what: str) -> str:
    t = text.strip()
    if not t.isdigit():
        raise vc.err(f"{what}: {t!r} is not a ParlGov id")
    return t


# ----------------------------------------------------------------------------- parse
@dataclass
class Party:
    party_id: str
    iso3: str
    short: str
    english: str
    native: str
    family: str
    left_right: str | None
    cmp: str | None
    line: int


@dataclass
class Cabinet:
    cabinet_id: str
    iso3: str
    country: str
    start: date
    name: str
    caretaker: str
    election_id: str
    election_date: str
    previous_cabinet_id: str | None
    seats_total: str | None
    lines: list[int] = field(default_factory=list)
    parties: list[tuple[str, str, str, int]] = field(default_factory=list)  # party_id, seats, pm, line (cabinet rows)
    pm_parties: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class Election:
    election_id: str
    iso3: str
    country: str
    etype: str
    day: date
    seats_total: str | None
    previous_id: str | None
    results: list[tuple[str, str | None, str | None, int]] = field(default_factory=list)  # party, vote%, seats, line
    first_line: int = 0


def parse_parties(body: bytes, *, min_rows: int = 1) -> dict[str, Party]:
    out: dict[str, Party] = {}
    for i, r in enumerate(_rows(body, "view_party.csv", PARTY_COLS, min_rows), start=2):
        pid = _id(r["party_id"], f"view_party.csv line {i} party_id")
        if pid in out:
            raise vc.err(f"view_party.csv line {i}: duplicate party_id {pid}")
        cmp = r["cmp"].strip()
        out[pid] = Party(
            pid, r["country_name_short"].strip(), r["party_name_short"].strip(), r["party_name_english"].strip(),
            r["party_name"].strip(), r["family_name"].strip(), num(r["left_right"]),
            cmp if cmp.isdigit() else None, i,
        )  # fmt: skip
    return out


def parse_cabinets(body: bytes, *, min_rows: int = 1) -> dict[str, Cabinet]:
    out: dict[str, Cabinet] = {}
    for i, r in enumerate(_rows(body, "view_cabinet.csv", CAB_COLS, min_rows), start=2):
        cid = _id(r["cabinet_id"], f"view_cabinet.csv line {i} cabinet_id")
        prev = r["previous_cabinet_id"].strip() or None
        if prev is not None:
            prev = _id(prev, f"view_cabinet.csv line {i} previous_cabinet_id")
        st = r["election_seats_total"].strip() or None
        head = (r["country_name_short"].strip(), _day(r["start_date"], f"view_cabinet.csv line {i} start_date"),
                r["cabinet_name"].strip(), r["caretaker"].strip(), _id(r["election_id"], f"line {i} election_id"),
                prev)  # fmt: skip
        c = out.get(cid)
        if c is None:
            c = out[cid] = Cabinet(cid, head[0], r["country_name"].strip(), head[1], head[2], head[3], head[4],
                                   r["election_date"].strip(), prev, st)  # fmt: skip
        elif (c.iso3, c.start, c.name, c.caretaker, c.election_id, c.previous_cabinet_id) != head:
            raise vc.err(f"view_cabinet.csv line {i}: cabinet {cid} attributes differ between its rows")
        if c.caretaker not in ("0", "1"):
            raise vc.err(f"view_cabinet.csv line {i}: caretaker {c.caretaker!r} is not 0/1")
        c.lines.append(i)
        pid = _id(r["party_id"], f"view_cabinet.csv line {i} party_id")
        cp, pm = r["cabinet_party"].strip(), r["prime_minister"].strip()
        if cp not in ("0", "1") or pm not in ("0", "1"):
            raise vc.err(f"view_cabinet.csv line {i}: cabinet_party/prime_minister not 0/1")
        if cp == "1":
            c.parties.append((pid, r["seats"].strip(), pm, i))
        if pm == "1":
            c.pm_parties.append((pid, i))
    return out


def parse_elections(body: bytes, *, min_rows: int = 1) -> dict[str, Election]:
    out: dict[str, Election] = {}
    for i, r in enumerate(_rows(body, "view_election.csv", ELEC_COLS, min_rows), start=2):
        eid = _id(r["election_id"], f"view_election.csv line {i} election_id")
        etype = r["election_type"].strip()
        if etype not in ELECTION_TYPES:
            raise vc.err(f"view_election.csv line {i}: election_type {etype!r} is not parliament/ep")
        prev = r["previous_parliament_election_id"].strip() or None
        day = _day(r["election_date"], f"view_election.csv line {i} election_date")
        e = out.get(eid)
        if e is None:
            e = out[eid] = Election(eid, r["country_name_short"].strip(), r["country_name"].strip(), etype, day,
                                    r["seats_total"].strip() or None, prev, first_line=i)  # fmt: skip
        elif (e.iso3, e.etype, e.day) != (r["country_name_short"].strip(), etype, day):
            raise vc.err(f"view_election.csv line {i}: election {eid} attributes differ between its rows")
        e.results.append((_id(r["party_id"], f"line {i} party_id"), num(r["vote_share"]), num(r["seats"]), i))
    return out


# ----------------------------------------------------------------------------- atlas
def cabinet_frames(cabs: Mapping[str, Cabinet], *, raw_sha256: str) -> tuple[Any, Any, dict[str, Any]]:
    buf = RowBuffer(ATLAS_SOURCE, "cabinets")
    cy = RowBuffer(ATLAS_SOURCE, "cabinets_cy")
    rep: Counter[str] = Counter()
    per_year: dict[tuple[str, int], list[Cabinet]] = defaultdict(list)
    seen: set[tuple[str, date]] = set()
    file = FILES["cabinet"][1]
    for c in sorted(cabs.values(), key=lambda x: (x.iso3, x.start, x.cabinet_id)):
        if (c.iso3, c.start) in seen:  # never observed in the 2024 release; refuse rather than mix two cabinets
            raise vc.err(f"two cabinets of {c.iso3} start on {c.start}: atlas cell would be ambiguous")
        seen.add((c.iso3, c.start))
        per_year[(c.iso3, c.start.year)].append(c)
        flags = {
            "parlgov_cabinet_id": c.cabinet_id, "cabinet_name": c.name, "parlgov_election_id": c.election_id,
            "election_date": c.election_date, "previous_cabinet_id": c.previous_cabinet_id,
            "pm_party_ids": [p for p, _ in c.pm_parties], "cabinet_party_ids": [p for p, _, _, _ in c.parties],
        }  # fmt: skip
        period = (c.start, date.fromordinal(c.start.toordinal() + 1))
        f_comp = canon({**flags, "computed": "by REVA from the cabinet's rows with cabinet_party = 1"})
        seats = [s for _, s, _, _ in c.parties]
        cells: list[tuple[str, str | None, str, str, str]] = [
            ("CABINET_PARTIES", str(len(c.parties)), "parties", f_comp, canon(file=file, rows=c.lines)),
        ]
        if c.parties and all(s.isdigit() for s in seats):
            cells.append(("CABINET_SEATS", str(sum(int(s) for s in seats)), "seats", f_comp,
                          canon(file=file, rows=[ln for *_, ln in c.parties], column="seats")))  # fmt: skip
        elif c.parties:
            rep["cabinet_seats_not_numeric"] += 1
        f_prov = canon(flags)
        if c.seats_total is not None and num(c.seats_total) is not None:
            cells.append(("PARLIAMENT_SEATS", num(c.seats_total), "seats", f_prov,
                          canon(file=file, row=c.lines[0], column="election_seats_total")))  # fmt: skip
        cells.append(("CABINET_CARETAKER", c.caretaker, "code (1 = caretaker cabinet, 0 = not)", f_prov,
                      canon(file=file, row=c.lines[0], column="caretaker")))  # fmt: skip
        for ind, val, unit, fl, sel in cells:
            buf.add(indicator=ind, provider_geo=c.iso3, period=period, value=val, unit=unit, flags=fl,
                    raw_sha256=raw_sha256, selector=sel)  # fmt: skip
        rep["cabinets"] += 1
    for (iso3, year), cs in sorted(per_year.items()):
        for ind, sub in (
            ("CABINETS_STARTED", cs),
            ("CARETAKER_CABINETS_STARTED", [c for c in cs if c.caretaker == "1"]),
        ):
            if not sub:
                continue
            cy.add(
                indicator=ind, provider_geo=iso3, period=year_period(year), value=str(len(sub)), unit="cabinets",
                flags=canon(computed="exact count of ParlGov cabinets whose start_date falls in the year",
                            parlgov_cabinet_ids=[c.cabinet_id for c in sub]),
                raw_sha256=raw_sha256, selector=canon(file=file, rows=[c.lines[0] for c in sub], column="start_date"),
            )  # fmt: skip
    return buf.frame(), cy.frame(), {**dict(rep), "cabinets_cy_cells": len(cy)}


# ----------------------------------------------------------------------------- KG
def _t(d: date) -> dict[str, Any]:
    return {"type": "time", "time": d.isoformat(), "precision": 11}


def _s(v: str) -> dict[str, Any]:
    return {"type": "string", "value": v}


def _q(v: str, unit: str | None = None) -> dict[str, Any]:
    return {"type": "quantity", "amount": v, **({"unit": unit} if unit else {})}


def kg_records(
    parties: Mapping[str, Party],
    cabs: Mapping[str, Cabinet],
    elecs: Mapping[str, Election],
    *,
    shas: Mapping[str, str],
    qids: Mapping[str, str],
) -> tuple[list[KgEntity], list[KgStatement], dict[str, Any]]:
    ents: list[KgEntity] = []
    sts: list[KgStatement] = []
    rep: Counter[str] = Counter()
    no_qid: set[str] = set()

    def country(subject: str, iso3: str, key: str, sha: str, sel: Mapping[str, Any], **when: Any) -> None:
        q = qids.get(iso3)
        if q is None:
            no_qid.add(iso3)
            return
        sts.append(KgStatement(key=key, subject=subject, property="P17", object_qid=q,
                               qualifiers={"parlgov_country": iso3}, raw_sha256=sha, selector=sel, **when))  # fmt: skip

    pf, cf, ef = FILES["party"][1], FILES["cabinet"][1], FILES["election"][1]
    ps, cs, es = shas["party"], shas["cabinet"], shas["election"]
    for p in parties.values():
        pid = f"parlgov:party:{p.party_id}"
        sel = {"file": pf, "row": p.line}
        label = p.english or p.native or p.short
        ents.append(KgEntity(qid=pid, kind="political_party", labels={"en": label},
                             descriptions={"en": f"political party in ParlGov ({p.iso3}, {p.short})"}))  # fmt: skip
        country(pid, p.iso3, f"{pid}:country", ps, sel)
        sts.append(KgStatement(key=f"{pid}:id", subject=pid, property="parlgov:party_id", value=_s(p.party_id),
                               raw_sha256=ps, selector=sel))  # fmt: skip
        if p.short:
            sts.append(KgStatement(key=f"{pid}:short", subject=pid, property="parlgov:party_short_name",
                                   value=_s(p.short), qualifiers={"party_name": p.native}, raw_sha256=ps,
                                   selector=sel))  # fmt: skip
        if p.family:
            sts.append(KgStatement(key=f"{pid}:family", subject=pid, property="parlgov:party_family",
                                   value=_s(p.family), raw_sha256=ps, selector=sel))  # fmt: skip
        if p.left_right is not None:
            sts.append(KgStatement(key=f"{pid}:lr", subject=pid, property="parlgov:left_right",
                                   value=_q(p.left_right), raw_sha256=ps, selector=sel))  # fmt: skip
        if p.cmp is not None:
            sts.append(KgStatement(key=f"{pid}:cmp", subject=pid, property="parlgov:cmp_id", value=_s(p.cmp),
                                   raw_sha256=ps, selector=sel))  # fmt: skip
    for e in elecs.values():
        eid = f"parlgov:election:{e.election_id}"
        sel = {"file": ef, "row": e.first_line}
        what = ELECTION_TYPES[e.etype]
        ents.append(KgEntity(qid=eid, kind="event", labels={"en": f"{e.country} {what}, {e.day.isoformat()}"},
                             descriptions={"en": f"{what} as recorded by ParlGov (election_id {e.election_id})"}))  # fmt: skip
        day = e.day.isoformat()
        sts += [
            KgStatement(key=f"{eid}:date", subject=eid, property="P585", value=_t(e.day), raw_sha256=es, selector=sel),
            KgStatement(key=f"{eid}:type", subject=eid, property="parlgov:election_type", value=_s(e.etype),
                        raw_sha256=es, selector=sel),
        ]  # fmt: skip
        country(eid, e.iso3, f"{eid}:country", es, sel, point_in_time=day, pit_precision=11)
        if e.seats_total and num(e.seats_total) is not None:
            sts.append(KgStatement(key=f"{eid}:seats_total", subject=eid, property="parlgov:seats_total",
                                   value=_q(num(e.seats_total) or "", "seats"), raw_sha256=es, selector=sel))  # fmt: skip
        if e.previous_id:
            sts.append(KgStatement(key=f"{eid}:previous", subject=eid, property="parlgov:previous_election",
                                   object_qid=f"parlgov:election:{e.previous_id}", raw_sha256=es, selector=sel))  # fmt: skip
        for party, vote, seats, line in e.results:
            if party not in parties:
                rep["election_party_unknown"] += 1
                continue
            q: dict[str, Any] = {}
            if vote is not None:
                q["vote_share_percent"] = vote
            if seats is not None:
                q["seats"] = seats
            sts.append(KgStatement(key=f"{eid}:result:{party}", subject=eid, property="parlgov:party_result",
                                   object_qid=f"parlgov:party:{party}", point_in_time=day, pit_precision=11,
                                   qualifiers=q, raw_sha256=es, selector={"file": ef, "row": line}))  # fmt: skip
        rep["elections"] += 1
    for c in cabs.values():
        cid = f"parlgov:cabinet:{c.cabinet_id}"
        sel = {"file": cf, "row": c.lines[0]}
        ents.append(KgEntity(
            qid=cid, kind="cabinet",
            labels={"en": f"{c.name} cabinet ({c.country}, from {c.start.isoformat()})"},
            descriptions={"en": f"cabinet as recorded by ParlGov (cabinet_id {c.cabinet_id}); P580 = the day it took office"},
        ))  # fmt: skip
        sts += [
            KgStatement(key=f"{cid}:start", subject=cid, property="P580", value=_t(c.start), raw_sha256=cs,
                        selector={**sel, "column": "start_date"}),
            KgStatement(key=f"{cid}:name", subject=cid, property="parlgov:cabinet_name", value=_s(c.name),
                        raw_sha256=cs, selector=sel),
            KgStatement(key=f"{cid}:caretaker", subject=cid, property="parlgov:caretaker", value=_s(c.caretaker),
                        qualifiers={"code_meaning": "1 = caretaker cabinet, 0 = not"}, raw_sha256=cs, selector=sel),
        ]  # fmt: skip
        country(cid, c.iso3, f"{cid}:country", cs, sel, start=c.start.isoformat(), start_precision=11)
        if c.election_id in elecs:
            sts.append(KgStatement(key=f"{cid}:election", subject=cid, property="parlgov:after_election",
                                   object_qid=f"parlgov:election:{c.election_id}",
                                   qualifiers={"election_date": c.election_date}, raw_sha256=cs, selector=sel))  # fmt: skip
        else:
            rep["cabinet_election_not_in_view_election"] += 1
        if c.previous_cabinet_id:
            sts.append(KgStatement(key=f"{cid}:previous", subject=cid, property="parlgov:previous_cabinet",
                                   object_qid=f"parlgov:cabinet:{c.previous_cabinet_id}", raw_sha256=cs,
                                   selector=sel))  # fmt: skip
        for party, seats, pm, line in c.parties:
            sts.append(KgStatement(key=f"{cid}:party:{party}", subject=cid, property="parlgov:cabinet_party",
                                   object_qid=f"parlgov:party:{party}", start=c.start.isoformat(), start_precision=11,
                                   qualifiers={"seats": seats, "prime_minister": pm}, raw_sha256=cs,
                                   selector={"file": cf, "row": line}))  # fmt: skip
        for party, line in c.pm_parties:
            sts.append(KgStatement(key=f"{cid}:pm_party:{party}", subject=cid, property="parlgov:prime_minister_party",
                                   object_qid=f"parlgov:party:{party}", start=c.start.isoformat(), start_precision=11,
                                   raw_sha256=cs, selector={"file": cf, "row": line}))  # fmt: skip
        if len(c.pm_parties) != 1:
            rep[f"cabinets_with_{len(c.pm_parties)}_pm_party_rows"] += 1
        rep["cabinets"] += 1
    rep["parties"] = len(parties)
    return ents, sts, {**dict(rep), "iso3_without_qid": sorted(no_qid)}


# ----------------------------------------------------------------------------- spec + ingest
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="ParlGov (Döring & Manow), Harvard Dataverse",
        licence="CC0-1.0",
        allowed_hosts=(DATAVERSE, S3_HOST),
        datasets=DATASETS,
        dataset_ids=(DOI,),
        cadence_seconds=90 * 86400,
        planned_requests=len(fetch_items()),
        max_response_bytes=10_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=vc.code_digest(sys.modules[__name__]),
        field_mappings={
            "geo_scheme": "iso3",
            "kg": "parlgov:party|election|cabinet:<id>",
            "predicates": "registry/kg/predicates_politics.yaml",
        },
        notes="Dataverse record (licence CC0 + MD5 of 3 files) checked on every run; KG events + cabinet tables.",
    )


def build(
    bodies: Mapping[str, bytes], shas: Mapping[str, str], *, qids: Mapping[str, str], live: bool
) -> dict[str, Any]:
    mins = MIN_ROWS if live else dict.fromkeys(MIN_ROWS, 1)
    parties = parse_parties(bodies["party"], min_rows=mins["party"])
    cabs = parse_cabinets(bodies["cabinet"], min_rows=mins["cabinet"])
    elecs = parse_elections(bodies["election"], min_rows=mins["election"])
    cab_frame, cy_frame, arep = cabinet_frames(cabs, raw_sha256=shas["cabinet"])
    ents, sts, krep = kg_records(parties, cabs, elecs, shas=shas, qids=qids)
    return {"cabinets": cab_frame, "cabinets_cy": cy_frame, "entities": ents, "statements": sts,
            "report": {"atlas": arep, "kg": krep, "kg_entities": len(ents), "kg_statements": len(sts)}}  # fmt: skip


async def atlas_ingest(run: Any) -> dict[str, Any]:
    live = run.origin == "live"
    got = await fetch_archive(run, fetch_items())
    meta = check_dataverse_record(
        got.bodies["parlgov.record"], doi=DOI,
        files={fid: (name, got.bodies[f"parlgov.{k}"]) for k, (fid, name) in FILES.items()}, licences=("CC0 1.0",),
    )  # fmt: skip
    bodies = {k: got.bodies[f"parlgov.{k}"] for k in FILES}
    shas = {k: got.shas[f"parlgov.{k}"] for k in FILES}
    out = build(bodies, shas, qids=vc.iso3_qids(), live=live)
    rights = {**RIGHTS, "record": meta, "record_raw_sha256": got.shas["parlgov.record"]}
    res: dict[str, Any] = {"record": meta, "report": out["report"], "atlas": {}}
    notes = {
        "cabinets": "ParlGov 2024 view_cabinet: one row per cabinet on the day it took office (start_date); "
        "CABINET_PARTIES and CABINET_SEATS computed from the rows with cabinet_party = 1; PARLIAMENT_SEATS and "
        "CABINET_CARETAKER as published. CC0.",
        "cabinets_cy": "COMPUTED from ParlGov 2024 view_cabinet: exact counts of cabinets (and caretaker cabinets) "
        "whose start_date falls in the calendar year; only non-zero cells. CC0.",
    }
    for ds in DATASETS:
        if run.wants(ds):
            r = run.register(dataset=ds, frame=out[ds], raw_sha256s=[shas["cabinet"]], coverage_note=notes[ds],
                             geo_scheme="iso3", rights=rights)  # fmt: skip
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    if run.dataset is None and vc.kg_available(run.engine):
        res["kg"] = vc.load_kg(
            run.engine, run.store, out["entities"], out["statements"], properties=vc.kg_properties("parlgov:"),
            source_key=SOURCE_KEY, source_revision=f"parlgov-2024-{meta['version']}", raw_sha256s=sorted(shas.values()),
            retrieved_at=datetime.fromisoformat(got.meta["parlgov.cabinet"]["retrieved_at"]), origin=run.origin,
        )  # fmt: skip
    return res


__all__ = ["atlas_ingest", "atlas_source", "build", "cabinet_frames", "fetch_items", "kg_records", "parse_cabinets",
           "parse_elections", "parse_parties"]  # fmt: skip
