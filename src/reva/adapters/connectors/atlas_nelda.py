"""NELDA -- National Elections Across Democracy and Autocracy (Hyde & Marinov), election rounds as dated KG events
and atlas tables (agent V6POLITICS).

Source and terms (checked 2026-10-17)
-------------------------------------
* Harvard Dataverse doi:10.7910/DVN/DDNWOR, "National Elections Across Democracy and Autocracy", published
  2013-01-08 (dataset version 1): ``id & q-wide.tab`` (the Dataverse tab-delimited export of the deposited
  ``id & q-wide.dta``; 2,974 election rounds, 1945-2010 plus a few 2011 rows) and ``NELDA_codebook_2011.pdf``
  (codebook "Last Updated November, 10 2011"). **No licence**: the record's ``license`` is empty, ``termsOfUse`` =
  "public", ``citationRequirements`` = Hyde & Marinov (2012) "Which Elections Can Be Lost?", Political Analysis
  20(2). Free download without registration; no redistribution or commercial-use grant (recorded in rights,
  like Archigos / Powell & Thyne in A-COUPS).
* The newer NELDA releases (to 2020, nelda.co) are available only on request from the PIs ("Access to data
  available under the contact heading"): **not ingested** (owner decision, docs/dev/v6_politics.md).
* Integrity: the record's MD5 is that of the deposited ``.dta`` (Dataverse "original"), not of the tab export, so
  the connector checks the tab export's byte size against the record's ``filesize`` (exact) and the codebook PDF
  against the record's MD5; a mismatch registers nothing.

What a record is
----------------
"The unit of observation is the election round" (codebook p. 1). ``electionID`` = ``XXX-YYYY-MMDD-TR`` (XXX the
Gleditsch & Ward state code, YYYY-MMDD the first day of voting of the election event, T = P presidential /
L legislative or parliamentary / A constituent assembly, R the round); ``year`` + ``mmdd`` = the day voting
started for *that round* (codebook p. 4). 58 coded questions ``nelda1``-``nelda58``: all but 43, 44, 54 take
"yes", "no", "N/A" or "unclear" (codebook p. 3); 43/44 name the leader before/after the election and 54 a
Western country or IGO (free text). The question texts below are transcribed from the codebook's variable
section (PDF pp. 4-23); the PDF's "fi"/"ffi"/"fl" ligature glyphs carry no text layer and are restored.

Outputs
-------
* atlas ``nelda.election_rounds``: per round (period = the round's voting day; month precision when NELDA's mmdd
  has day 00), ``provider_geo`` = ``gw:<ccode>`` (ISO3 through the date-aware KGGEOPOL crosswalk at that day),
  indicators ``ELECTION_ROUND|tr=<T><R>`` (value = round number, value_text = NELDA ``types``, NELDA's general notes
  in flags) and ``nelda<N>|tr=<T><R>`` (value_text = the coded answer verbatim, question + that variable's notes in
  flags).
* atlas ``nelda.elections_cy``: computed exact counts per GW state x year of election rounds by office type
  (``ELECTION_ROUNDS|office=<legislative|executive|constituent_assembly>``); non-zero cells only.
* KG (source ``nelda.dataverse``): ``nelda:election:<electionID>`` (kind ``event``): ``P585`` (the round's day),
  ``P17`` (state: country QID via the crosswalk, else ``gw:<code>``), ``nelda:election_type``, ``nelda:round``,
  ``nelda:gw_code`` and the coded answers of :data:`KG_VARIABLES` (``nelda:coded_answer``, variable + question in
  qualifiers). Leader names (nelda43/44) stay text in the atlas: they are never matched to person entities by name.

Nothing is causal: "Did the incumbent's party lose?" is NELDA's coded outcome of the round, not an effect.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from reva.adapters.connectors import v6politics_common as vc
from reva.adapters.connectors.atlas_geopol_common import (
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
    code_correspondence,
    fetch_countrycode,
)
from reva.adapters.connectors.atlas_society_common import DATAVERSE, S3_HOST, RowBuffer, canon, year_period
from reva.atlas.sources import AtlasSourceSpec

CONNECTOR = "atlas.nelda"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_nelda/1"
SOURCE_KEY = "nelda.dataverse"
ATLAS_SOURCE = "nelda"
DOI = "doi:10.7910/DVN/DDNWOR"
TAB_ID, TAB_NAME = 2427654, "id & q-wide.tab"
CODEBOOK_ID, CODEBOOK_NAME = 2427655, "NELDA_codebook_2011.pdf"
RECORD_URL = f"https://{DATAVERSE}/api/datasets/:persistentId/?persistentId={DOI}"
TAB_URL = f"https://{DATAVERSE}/api/access/datafile/{TAB_ID}"
CODEBOOK_URL = f"https://{DATAVERSE}/api/access/datafile/{CODEBOOK_ID}"
MIN_ROWS = 2900
DATASETS = ("election_rounds", "elections_cy")
OFFICE = {"L": "legislative", "P": "executive", "A": "constituent_assembly"}
TYPES = {"Legislative/Parliamentary": "L", "Executive": "P", "Constituent Assembly": "A"}
ANSWERS = frozenset({"yes", "no", "N/A", "unclear"})
FREE_TEXT = frozenset({43, 44, 54})
RIGHTS: dict[str, Any] = {
    "licence": "No licence stated (checked 2026-10-17): Harvard Dataverse record license empty, termsOfUse "
    "'public'; free download without registration; citation required. No redistribution or commercial-use grant.",
    "licence_url": f"https://doi.org/{DOI[4:]}",
    "attribution": "Hyde, Susan D. and Nikolay Marinov. 2012. Which Elections Can Be Lost? Political Analysis 20(2): "
    "191-210. Data: National Elections Across Democracy and Autocracy, Harvard Dataverse doi:10.7910/DVN/DDNWOR.",
    "commercial_use": "not_granted",
    "redistribution": "not_granted",
    "source_key": SOURCE_KEY,
}
#: Questions (codebook variable section, ligatures restored). Variable -> (question, physical PDF page of the
#: variable's heading in NELDA_codebook_2011.pdf).
QUESTIONS: dict[int, tuple[str, int]] = {
    1: ("Were regular elections suspended before this election?", 4),
    2: ("Were these the first multiparty elections?", 5),
    3: ("Was opposition allowed?", 5),
    4: ("Was more than one party legal?", 6),
    5: ("Was there a choice of candidates on the ballot?", 6),
    6: ("If regular, were these elections early or late relative to the date they were supposed to be held per "
        "established procedure?", 6),
    7: ("Before elections, were there clear indications that the incumbent had made a prior decision to give up "
        "power?", 7),
    8: ("Did the incumbent reach his or her term limit?", 7),
    9: ("Had the incumbent extended his or her term in office or eligibility to run in elections at any point in "
        "the past?", 8),
    10: ('Was the country ruled by "transitional leadership" tasked with "holding elections"?', 8),
    11: ("Before elections, are there significant concerns that elections will not be free and fair?", 8),
    12: ("Was the incumbent or ruling party confident of victory before elections?", 9),
    13: ("Were opposition leaders prevented from running?", 9),
    14: ("Did some opposition leaders boycott the election?", 9),
    15: ("Is there evidence that the government harassed the opposition?", 10),
    16: ("In the run-up to the election, were there allegations of media bias in favor of the incumbent?", 10),
    17: ("Is economic growth in the country said to be good?", 10),
    18: ("Is country said to be in an economic crisis?", 11),
    19: ("Is country said to be a large recipient of outside economic aid?", 11),
    20: ("Was the office of the incumbent leader contested in this election?", 11),
    21: ("Did the incumbent run?", 12),
    22: ("If no (nelda21): was there a chosen successor?", 12),
    23: ("If yes (nelda22): did a successor assume power as a result of the elections?", 12),
    24: ("Did the incumbent's party lose?", 12),
    25: ("Were there reliable polls that indicated popularity of ruling political party or of the candidates for "
         "office before elections?", 13),
    26: ("If yes (nelda25): were they favorable for the incumbent?", 13),
    27: ("Was the vote count a gain for the opposition?", 13),
    28: ("Is there evidence that reports critical of the government's handling of the election reached large "
         "numbers of people?", 14),
    29: ("Were there riots and protests after the election?", 14),
    30: ("If yes (nelda29): did they involve allegations of vote fraud?", 15),
    31: ("If yes (nelda29): did the government use violence against demonstrators?", 15),
    32: ("Were results that did not favor the incumbent canceled?", 15),
    33: ("Was there significant violence involving civilian deaths immediately before, during, or after the "
         "election?", 16),
    34: ("Were results that were favorable to the incumbent canceled?", 16),
    35: ("If yes (nelda34): was this in part a result of wide-spread protests?", 16),
    36: ("If yes (nelda34): was this in part a result of outside pressure?", 17),
    37: ("If yes (nelda34), was a new election held?", 17),
    38: ('If yes (nelda37), did victory go to a different party or candidate than at the "initial" stage?', 17),
    39: ("Was the incumbent replaced?", 17),
    40: ("If yes (nelda39), did the leader step down because the vote count gave victory to some other political "
         "actor?", 18),
    41: ("If yes (nelda39), was the leader replaced as a result of widespread protests?", 18),
    42: ("If yes (nelda39), was there a coup that prevented the elected leader from taking office?", 18),
    43: ("What was the name of the leader who was in office before the election?", 18),
    44: ("What was the name leader who was in office after the election?", 19),
    45: ("Were international monitors present?", 19),
    46: ("If yes (nelda45), were Western monitors present?", 19),
    47: ("If yes (nelda46), were there allegations by Western monitors of significant vote-fraud?", 20),
    48: ("Were some monitors denied the opportunity to be present by the government holding elections?", 20),
    49: ("Did any monitors refuse to go to an election because they believed that it would not be free and "
         "fair?", 20),
    50: ("Is country said to be in good relations with the US before the elections?", 20),
    51: ("Is there a negative change in relations with the US after the election?", 21),
    52: ("Is there a positive change in relations with the US after the election?", 21),
    53: ("Is the country said to have a substantial economic, military or political relationship with a Western "
         "country or IGO?", 21),
    54: ("If yes (nelda53), which one?", 21),
    55: ("Is there a negative change in the country's economic, military or political relationship with a Western "
         "country or IGO after the election?", 22),
    56: ("Is there a positive change in the country's economic, military or political relationship with a Western "
         "country or IGO after the election?", 22),
    57: ("Is aid cut-off, or threatened to be cut-off, by an outside actor at any point before or after the "
         "election?", 22),
    58: ("Did an outside actor attempt to influence the outcome of the election by making threats to withhold, or "
         "by withholding, something of value to the country?", 23),
}  # fmt: skip
#: Coded answers also stated in the KG (all 58 are in the atlas).
KG_VARIABLES = (3, 4, 5, 11, 13, 14, 15, 20, 21, 24, 27, 29, 33, 39, 45, 47)
REQUIRED = ("ccode", "country", "electionid", "year", "mmdd", "types", "notes",
            *(f"nelda{i}" for i in QUESTIONS), *(f"nelda{i}notes" for i in QUESTIONS))  # fmt: skip


@dataclass(frozen=True)
class Round:
    election_id: str
    ccode: int
    country: str
    day: date
    precision: int  # 11 day, 10 month
    types: str
    office: str  # L / P / A
    round: int
    notes: str
    answers: Mapping[int, str]
    answer_notes: Mapping[int, str]
    line: int


# ----------------------------------------------------------------------------- record
def check_record(body: bytes, *, tab: bytes, codebook: bytes) -> dict[str, Any]:
    try:
        v = json.loads(body)["data"]["latestVersion"]
    except (ValueError, KeyError, TypeError) as e:
        raise vc.err(f"{DOI}: Dataverse record is not the expected JSON ({e})") from e
    lic = v.get("license")
    if lic not in (None, "", {}):
        raise vc.err(f"{DOI}: a licence is now declared ({lic!r}); review the rights before ingesting", kind="licence")
    if (v.get("termsOfUse") or "").strip() != "public":
        raise vc.err(f"{DOI}: termsOfUse changed ({str(v.get('termsOfUse'))[:200]!r})", kind="licence")
    files = {f["dataFile"]["id"]: f for f in v.get("files", []) if isinstance(f, dict) and "dataFile" in f}
    t, c = files.get(TAB_ID), files.get(CODEBOOK_ID)
    if t is None or c is None:
        raise vc.err(f"{DOI}: data file or codebook no longer in the latest version", kind="stale_release")
    if t.get("restricted") or c.get("restricted"):
        raise vc.err(f"{DOI}: files are now restricted", kind="licence")
    if t["dataFile"].get("filename") != TAB_NAME or int(t["dataFile"].get("filesize", -1)) != len(tab):
        raise vc.err(f"{TAB_NAME}: name/size differ from the record ({t['dataFile'].get('filesize')} vs {len(tab)})",
                     kind="integrity")  # fmt: skip
    md5 = (c["dataFile"].get("checksum") or {}).get("value")
    if md5 != hashlib.md5(codebook, usedforsecurity=False).hexdigest():
        raise vc.err(f"{CODEBOOK_NAME}: MD5 does not match the record", kind="integrity")
    return {
        "doi": DOI,
        "version": f"{v.get('versionNumber')}.{v.get('versionMinorNumber')}",
        "release_time": v.get("releaseTime"),
        "terms_of_use": v.get("termsOfUse"),
        "citation_requirements": v.get("citationRequirements"),
        "tab_filesize": len(tab),
        "tab_original": t["dataFile"].get("originalFileName"),
        "original_md5": (t["dataFile"].get("checksum") or {}).get("value"),
        "unf": t["dataFile"].get("UNF"),
        "codebook_md5": md5,
    }


# ----------------------------------------------------------------------------- parse
def _day(year: str, mmdd: str, what: str) -> tuple[date, int]:
    try:
        y, md = int(year), int(mmdd)
    except ValueError as e:
        raise vc.err(f"{what}: year/mmdd {year!r}/{mmdd!r} not numeric") from e
    mm, dd = md // 100, md % 100
    try:
        if dd == 0:
            return date(y, mm, 1), 10
        return date(y, mm, dd), 11
    except ValueError as e:
        raise vc.err(f"{what}: {year}-{mmdd} is not a date") from e


def parse_tab(body: bytes, *, min_rows: int = MIN_ROWS) -> list[Round]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise vc.err(f"{TAB_NAME}: not UTF-8 ({e})") from e
    csv.field_size_limit(1_000_000)
    rd = csv.DictReader(io.StringIO(text), delimiter="\t")
    missing = [c for c in REQUIRED if c not in (rd.fieldnames or [])]
    if missing:
        raise vc.err(f"{TAB_NAME}: missing columns {missing[:8]}")
    out: list[Round] = []
    seen: set[str] = set()
    for i, r in enumerate(rd, start=2):
        eid = r["electionid"].strip()
        parts = eid.split("-")
        if len(parts) != 4 or len(parts[3]) != 2 or parts[3][0] not in OFFICE or not parts[3][1].isdigit():
            raise vc.err(f"{TAB_NAME} line {i}: electionid {eid!r} is not XXX-YYYY-MMDD-TR")
        if eid in seen:
            raise vc.err(f"{TAB_NAME} line {i}: duplicate electionid {eid}")
        seen.add(eid)
        ccode = int(r["ccode"])
        if int(parts[0]) != ccode:
            raise vc.err(f"{TAB_NAME} line {i}: electionid state {parts[0]} differs from ccode {ccode}")
        if TYPES.get(r["types"].strip()) != parts[3][0]:
            raise vc.err(f"{TAB_NAME} line {i}: types {r['types']!r} does not match electionid office {parts[3][0]}")
        day, prec = _day(r["year"], r["mmdd"], f"{TAB_NAME} line {i}")
        answers: dict[int, str] = {}
        anotes: dict[int, str] = {}
        for q in QUESTIONS:
            a = r[f"nelda{q}"].strip()
            if q not in FREE_TEXT and a not in ANSWERS and a != "":
                raise vc.err(f"{TAB_NAME} line {i}: nelda{q} = {a!r} is not yes/no/N/A/unclear")
            if a:
                answers[q] = a
            n = r[f"nelda{q}notes"].strip()
            if n:
                anotes[q] = n
        out.append(Round(eid, ccode, r["country"].strip(), day, prec, r["types"].strip(), parts[3][0],
                         int(parts[3][1]), r["notes"].strip(), answers, anotes, i))  # fmt: skip
    if len(out) < min_rows:
        raise vc.err(f"{TAB_NAME}: only {len(out)} rounds (expected >= {min_rows}): truncated file?")
    return out


# ----------------------------------------------------------------------------- atlas
def _period(r: Round) -> tuple[date, date]:
    if r.precision == 11:
        return r.day, r.day + timedelta(days=1)
    nxt = date(r.day.year + (r.day.month == 12), r.day.month % 12 + 1, 1)
    return r.day, nxt


def atlas_frames(rounds: Sequence[Round], *, raw_sha256: str, crosswalk: StateCodeCrosswalk) -> tuple[Any, Any]:
    buf = RowBuffer(ATLAS_SOURCE, "election_rounds")
    cy = RowBuffer(ATLAS_SOURCE, "elections_cy")
    counts: dict[tuple[int, int, str], list[Round]] = defaultdict(list)
    seen: set[tuple[str, str, date]] = set()
    for r in rounds:
        res = crosswalk.resolve("gw", r.ccode, r.day)
        geo = f"gw:{r.ccode}"
        tr = f"{r.office}{r.round}"
        if (geo, tr, r.day) in seen:
            raise vc.err(f"two NELDA rounds {tr} of {geo} on {r.day}: atlas cell would be ambiguous")
        seen.add((geo, tr, r.day))
        counts[(r.ccode, r.day.year, OFFICE[r.office])].append(r)
        base = {"electionid": r.election_id, "nelda_country": r.country, "date_precision": "day" if r.precision == 11
                else "month (NELDA mmdd day 00)", "code_resolution": res.reason}  # fmt: skip
        period = _period(r)
        buf.add(indicator=f"ELECTION_ROUND|tr={tr}", provider_geo=geo, period=period, value=str(r.round),
                value_text=r.types, unit="round number", flags=canon({**base, "notes": r.notes} if r.notes else base),
                raw_sha256=raw_sha256, selector=canon(file=TAB_NAME, row=r.line, column="electionid"),
                iso3=res.iso3, entity_qid=res.qid)  # fmt: skip
        for q, a in sorted(r.answers.items()):
            fl = {**base, "question": QUESTIONS[q][0], "codebook_pdf_page": QUESTIONS[q][1]}
            if q in r.answer_notes:
                fl["variable_notes"] = r.answer_notes[q]
            buf.add(indicator=f"nelda{q}|tr={tr}", provider_geo=geo, period=period, value=None, value_text=a,
                    unit="free text" if q in FREE_TEXT else "coded answer (yes/no/N/A/unclear)", flags=canon(fl),
                    raw_sha256=raw_sha256, selector=canon(file=TAB_NAME, row=r.line, column=f"nelda{q}"),
                    iso3=res.iso3, entity_qid=res.qid)  # fmt: skip
    for (ccode, year, office), rs in sorted(counts.items()):
        res = crosswalk.resolve("gw", ccode, date(year, 7, 1))
        cy.add(indicator=f"ELECTION_ROUNDS|office={office}", provider_geo=f"gw:{ccode}", period=year_period(year),
               value=str(len(rs)), unit="election rounds",
               flags=canon(computed="exact count of NELDA election rounds whose voting day falls in the year",
                           electionids=[r.election_id for r in rs], code_resolution=res.reason),
               raw_sha256=raw_sha256, selector=canon(file=TAB_NAME, rows=[r.line for r in rs], column="electionid"),
               iso3=res.iso3, entity_qid=res.qid)  # fmt: skip
    return buf.frame(), cy.frame()


# ----------------------------------------------------------------------------- KG
def kg_records(
    rounds: Sequence[Round], *, raw_sha256: str, resolver: StateEntityResolver
) -> tuple[list[KgEntity], list[KgStatement]]:
    ents: list[KgEntity] = []
    sts: list[KgStatement] = []
    for r in rounds:
        eid = f"nelda:election:{r.election_id}"
        sel = {"file": TAB_NAME, "row": r.line}
        office = {"L": "legislative", "P": "presidential", "A": "constituent assembly"}[r.office]
        when = r.day.isoformat() if r.precision == 11 else r.day.isoformat()[:7]
        ents.append(KgEntity(
            qid=eid, kind="event",
            labels={"en": f"{r.country} {office} election, round {r.round} ({when})"},
            descriptions={"en": f"national election round as coded by NELDA (electionID {r.election_id})"},
        ))  # fmt: skip
        t = r.day.isoformat()
        q0 = {"electionid": r.election_id}
        sts += [
            KgStatement(key=f"{eid}:date", subject=eid, property="P585",
                        value={"type": "time", "time": t, "precision": r.precision}, qualifiers=q0,
                        raw_sha256=raw_sha256, selector={**sel, "column": "year+mmdd"}),
            KgStatement(key=f"{eid}:state", subject=eid, property="P17",
                        object_qid=resolver.entity("gw", r.ccode, r.day, r.country), point_in_time=t,
                        pit_precision=r.precision, qualifiers={**q0, "gw_code": r.ccode}, raw_sha256=raw_sha256,
                        selector={**sel, "column": "ccode"}),
            KgStatement(key=f"{eid}:type", subject=eid, property="nelda:election_type",
                        value={"type": "string", "value": r.types}, raw_sha256=raw_sha256,
                        selector={**sel, "column": "types"}),
            KgStatement(key=f"{eid}:round", subject=eid, property="nelda:round",
                        value={"type": "quantity", "amount": str(r.round)}, raw_sha256=raw_sha256,
                        selector={**sel, "column": "electionid"}),
            KgStatement(key=f"{eid}:gw", subject=eid, property="nelda:gw_code",
                        value={"type": "string", "value": str(r.ccode)}, raw_sha256=raw_sha256,
                        selector={**sel, "column": "ccode"}),
        ]  # fmt: skip
        for q in KG_VARIABLES:
            a = r.answers.get(q)
            if a is None:
                continue
            qual: dict[str, Any] = {"variable": f"nelda{q}", "question": QUESTIONS[q][0]}
            if q in r.answer_notes:
                qual["variable_notes"] = r.answer_notes[q]
            sts.append(KgStatement(key=f"{eid}:nelda{q}", subject=eid, property="nelda:coded_answer",
                                   value={"type": "string", "value": a}, point_in_time=t, pit_precision=r.precision,
                                   qualifiers=qual, raw_sha256=raw_sha256,
                                   selector={**sel, "column": f"nelda{q}"}))  # fmt: skip
    return ents, sts


# ----------------------------------------------------------------------------- spec + ingest
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Susan D. Hyde & Nikolay Marinov (NELDA), Harvard Dataverse",
        licence=RIGHTS["licence"],
        allowed_hosts=(DATAVERSE, S3_HOST, "cran.r-project.org"),
        datasets=DATASETS,
        dataset_ids=(DOI,),
        cadence_seconds=180 * 86400,
        planned_requests=5,
        max_response_bytes=10_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=vc.code_digest(sys.modules[__name__]),
        field_mappings={"geo": "Gleditsch-Ward ccode (date-aware crosswalk at the round's day)",
                        "kg": "nelda:election:<electionID>", "predicates": "registry/kg/predicates_politics.yaml"},  # fmt: skip
        notes="Dataverse record (terms 'public', tab size, codebook MD5) + tab export + codebook PDF + countrycode.",
    )


def build(tab: bytes, *, crosswalk: StateCodeCrosswalk, qid_for_iso3: Mapping[str, str], live: bool,
          countrycode_sha256: str | None = None) -> dict[str, Any]:  # fmt: skip
    sha = hashlib.sha256(tab).hexdigest()
    rounds = parse_tab(tab, min_rows=MIN_ROWS if live else 1)
    rounds_frame, cy_frame = atlas_frames(rounds, raw_sha256=sha, crosswalk=crosswalk)
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    ents, sts = kg_records(rounds, raw_sha256=sha, resolver=resolver)
    ents += resolver.code_entities()
    if countrycode_sha256:
        sts += code_correspondence(resolver, raw_sha256=countrycode_sha256)
    return {
        "sha256": sha, "rounds": rounds, "election_rounds": rounds_frame, "elections_cy": cy_frame,
        "entities": ents, "statements": sts,
        "report": {"rounds": len(rounds), "offices": dict(Counter(r.office for r in rounds)),
                   "years": [min(r.day.year for r in rounds), max(r.day.year for r in rounds)] if rounds else None,
                   "month_precision": sum(r.precision == 10 for r in rounds),
                   "unresolved_state_codes": {k: sorted(v)[:3] for k, v in resolver.unresolved.items()},
                   "kg_entities": len(ents), "kg_statements": len(sts)},
    }  # fmt: skip


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_coups import _geopol_correspondence_property

    live = run.origin == "live"
    crosswalk, cc_sha = await fetch_countrycode(run)
    rec = await run.get(RECORD_URL, key="nelda.record", media_types=("application/json",), max_bytes=2_000_000)
    tab = await run.get(TAB_URL, key="nelda.tab", max_bytes=10_000_000,
                        media_types=("text/tab-separated-values", "text/plain", "application/octet-stream"))  # fmt: skip
    cb = await run.get(CODEBOOK_URL, key="nelda.codebook", media_types=("application/pdf",), max_bytes=2_000_000)
    meta = check_record(rec.body, tab=tab.body, codebook=cb.body)
    out = build(tab.body, crosswalk=crosswalk, qid_for_iso3=vc.iso3_qids(), live=live, countrycode_sha256=cc_sha)
    shas = {k: run.archive(r) for k, r in (("record", rec), ("tab", tab), ("codebook", cb))}
    rights = {**RIGHTS, "record": meta, "record_raw_sha256": shas["record"], "codebook_raw_sha256": shas["codebook"]}
    res: dict[str, Any] = {"record": meta, "report": out["report"], "atlas": {}}
    notes = {
        "election_rounds": "NELDA (Hyde & Marinov; Harvard Dataverse 2013 release, rounds 1945-2010/11): one cell per "
        "round and coded variable (yes/no/N/A/unclear, verbatim; 43/44/54 free text), question text in flags; "
        "period = the round's voting day. No licence stated (terms 'public', citation required).",
        "elections_cy": "COMPUTED from NELDA: exact counts of election rounds per Gleditsch-Ward state, year and "
        "office; non-zero cells only.",
    }
    for ds in DATASETS:
        if run.wants(ds):
            r = run.register(dataset=ds, frame=out[ds], raw_sha256s=[shas["tab"]], coverage_note=notes[ds],
                             rights=rights)  # fmt: skip
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    if run.dataset is None and vc.kg_available(run.engine):
        res["kg"] = vc.load_kg(
            run.engine, run.store, out["entities"], out["statements"],
            properties=vc.kg_properties("nelda:") + _geopol_correspondence_property(), source_key=SOURCE_KEY,
            source_revision=f"nelda-dataverse-{meta['version']}", raw_sha256s=sorted({shas["tab"], cc_sha}),
            retrieved_at=tab.retrieved_at if isinstance(tab.retrieved_at, datetime) else datetime.now(),
            origin=run.origin,
        )  # fmt: skip
    return res


__all__ = ["KG_VARIABLES", "QUESTIONS", "atlas_frames", "atlas_ingest", "atlas_source", "build", "check_record",
           "kg_records", "parse_tab"]  # fmt: skip
