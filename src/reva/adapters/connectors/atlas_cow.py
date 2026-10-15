"""Correlates of War -> atlas tables ``cow.nmc7``, ``cow.system2024``, ``cow.wars4`` and KG statements
(state-system membership, major-power status and war participation with dates) (V3, agent A-HISTECON).

Source
------
Correlates of War Project (https://correlatesofwar.org), bulk files under ``/wp-content/uploads/``:

* State System Membership v2024 (``States2024.zip``: ``statelist2024.csv`` -- membership spells with day
  dates; ``System2024.zip``: ``system2024.csv`` -- one row per member state and year) and Major Powers
  v2024 (``MajorPowers2024.zip``). Citation: Correlates of War Project. 2025. "State System Membership,
  v2024."
* National Material Capabilities v7.0 (``NMCv7.zip`` -> ``NMC-v7-abridged.zip`` -> ``NMC-70-abridged.csv``,
  1816-2022). Citation: Singer, Bremer and Stuckey (1972), NMC v7.0 (2026).
* COW War Data v4.0 (``Inter-StateWarData_v4.0.csv``, ``Extra-StateWarData_v4.0.csv``). Citation: Sarkees
  and Wayman (2010), *Resort to War: 1816-2007*.

**Terms** (COW FAQ, read 2026-10-15): "We encourage the use of COW data under certain guidelines. 1) The
data be properly cited. 2) COW data users cannot charge for access." This is *not* an open licence:
the rights record says so (``open_licence: false``) and every row carries the citation.

Codes and countries
-------------------
COW state numbers (``ccode``) are mapped to crosswalk entities with KGGEOPOL's date-aware
``StateCodeCrosswalk`` (``atlas_geopol_common``: the ``countrycode`` R package panel + KGGEOPOL's explicit
policy windows for the USSR, both Germanies, Yugoslavia, Czechoslovakia, the Yemens, the Vietnams,
Zanzibar...) -- the same resolution KGGEOPOL uses, not a second COW->ISO table. The countrycode tarball is
fetched and archived by this connector too (its provenance). A code that resolves to nothing keeps
``iso3`` null (provider code verbatim). Agent-mapped QIDs of historical states for given periods
(``registry/entities/historical_states_qids.json``, provider ``cow``) fill ``entity_qid``.

What is parsed (no value is computed)
-------------------------------------
* ``cow.nmc7``: milex, milper, irst, pec, tpop, upop, cinc per state-year; the provider's missing code
  ``-9`` is omitted (counted in the report). **milex is in thousands of current British pounds for
  1816-1913 and thousands of current US dollars from 1914** (NMC v7 documentation, "Military
  Expenditures"); the row unit says which. ``cinc`` is a share of the system total (0-1).
* ``cow.system2024``: ``system_member`` = 1 for every (state, year) row the provider lists.
* ``cow.wars4``: ``batdeath`` per state participation in an inter-state or extra-state war
  (indicator ``batdeath|type=inter|war=<n>``), over the participation years; ``-9`` (unknown) omitted.
* KG (``source_key`` ``cow.data``): entities ``cow:<ccode>`` (state codes; same id scheme as KGGEOPOL),
  ``cow:system:interstate``, ``cow:system:major_powers``, ``cow:war:<n>``; statements ``P463`` (member of)
  per membership / major-power spell with day-precision start/end (an end of 2024-12-31 is the end of
  the data, not of membership: kept open, qualifier ``observed_until``), ``P1344`` (participant in) per
  war participation spell (``-9`` day/month lowers the precision; ``-7`` = ongoing), ``P31`` war
  entities -> Q198 (war), and ``cow:code_denotes`` (state code -> Wikidata QID, dated, agent-mapped)
  for the historical periods of ``historical_states_qids.json``.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from reva.adapters.connectors.atlas_history_common import (
    DAY,
    AtlasBuild,
    atlas_spec,
    canon,
    code_digest,
    decimal_text,
    fetch_plain,
    frame,
    historical_qid,
    historical_qids,
    sha256_hex,
    year_period,
)
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

NAME = "atlas.cow"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_cow/1"
SOURCE_KEY = "cow.data"
ATLAS_SOURCE = "cow"
HOST = "correlatesofwar.org"
BASE = f"https://{HOST}/wp-content/uploads"
MAX_BYTES = 12_000_000
FILES = {
    "states": "States2024.zip",
    "system": "System2024.zip",
    "majors": "MajorPowers2024.zip",
    "nmc": "NMCv7.zip",
    "inter": "Inter-StateWarData_v4.0.csv",
    "extra": "Extra-StateWarData_v4.0.csv",
}
MEMBERS = {
    "states": "States2024/statelist2024.csv",
    "system": "System2024/system2024.csv",
    "majors": "MajorPowers2024/majors2024.csv",
    "nmc": "NMCv7/NMC-v7-abridged.zip",
}
NMC_INNER = "NMC-70-abridged.csv"
MAX_MEMBER_BYTES = 64 * 1024 * 1024
DATA_END = date(2024, 12, 31)  # v2024 coverage end: an end date equal to it means "still a member"
DATA_START = date(1816, 1, 1)

RIGHTS: dict[str, Any] = {
    "licence": "COW terms of use (not an open licence): cite the data; users cannot charge for access",
    "terms_url": "https://correlatesofwar.org/faq/",
    "open_licence": False,
    "attribution": (
        "Correlates of War Project. 2025. State System Membership, v2024; Major Powers v2024. "
        "Singer, J. David, Stuart Bremer and John Stuckey (1972), National Material Capabilities v7.0. "
        "Sarkees, Meredith Reid and Frank Wayman (2010), Resort to War: 1816-2007 (COW War Data v4.0). "
        "Online, https://correlatesofwar.org"
    ),
}
NMC_UNITS = {
    "milper": "thousands of military personnel",
    "irst": "thousands of tons (iron and steel production)",
    "pec": "thousands of coal-ton equivalents (primary energy consumption)",
    "tpop": "thousands (total population)",
    "upop": "thousands (population of cities of 100,000 or more)",
    "cinc": "share of the state system total (0-1)",
}
MILEX_UNIT_GBP = "thousands of current-year British pounds (1816-1913)"
MILEX_UNIT_USD = "thousands of current-year US dollars (1914-)"
WAR_KINDS = {"inter": "inter-state war", "extra": "extra-state war"}
Q_WAR = "Q198"


class CowParseError(SourceResponseError):
    pass


def _err(msg: str) -> CowParseError:
    return CowParseError(msg, retryable=False, kind="schema_drift")


# --------------------------------------------------------------------------- readers
def zip_member(body: bytes, name: str, *, max_bytes: int = MAX_MEMBER_BYTES) -> bytes:
    """One named regular member of a zip (bounded; no path tricks: exact name match)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err(f"not a zip archive (looking for {name})") from e
    with zf:
        infos = [i for i in zf.infolist() if i.filename == name]
        if len(infos) != 1:
            raise _err(f"zip member {name} missing or ambiguous ({len(infos)})")
        info = infos[0]
        if info.file_size > max_bytes or (info.compress_size and info.file_size / info.compress_size > 200):
            raise _err(f"zip member {name} exceeds the size/ratio bound")
        return zf.read(info)


def read_csv(body: bytes, what: str) -> list[dict[str, str]]:
    """COW CSVs: latin-1 (some names carry accents), CR or LF line ends, header names trimmed."""
    text = body.decode("utf-8") if _is_utf8(body) else body.decode("latin-1")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise _err(f"{what} is empty")
    head = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        if len(r) != len(head):
            raise _err(f"{what}: row with {len(r)} fields, header has {len(head)}")
        out.append({h: c.strip() for h, c in zip(head, r, strict=True)})
    return out


def _is_utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _int(s: str | None) -> int | None:
    if s is None or s.strip() == "":
        return None
    return int(float(s))


def _need(rows: Sequence[Mapping[str, str]], cols: Sequence[str], what: str) -> None:
    if not rows:
        raise _err(f"{what}: no rows")
    missing = [c for c in cols if c not in rows[0]]
    if missing:
        raise _err(f"{what}: columns {missing} missing")


# --------------------------------------------------------------------------- parsed records
@dataclass(frozen=True)
class Spell:
    ccode: int
    abb: str
    name: str
    start: date
    end: date
    row: int


def parse_spells(rows: Sequence[Mapping[str, str]], what: str, *, names: Mapping[int, str] | None = None) -> list[Spell]:
    cols = ["stateabb", "ccode", "styear", "stmonth", "stday", "endyear", "endmonth", "endday"]
    _need(rows, cols, what)
    out = []
    for i, r in enumerate(rows, start=2):
        try:
            c = int(r["ccode"])
            s = date(int(r["styear"]), int(r["stmonth"]), int(r["stday"]))
            e = date(int(r["endyear"]), int(r["endmonth"]), int(r["endday"]))
        except ValueError as ex:
            raise _err(f"{what} line {i}: bad code/date ({ex})") from ex
        if e < s:
            raise _err(f"{what} line {i}: end before start")
        nm = r.get("statenme") or (names or {}).get(c) or r["stateabb"]
        out.append(Spell(c, r["stateabb"], nm, s, e, i))
    return out


def _war_date(y: str, m: str, d: str) -> tuple[str | None, int | None, str]:
    """COW (year, month, day) -> (ISO date text, Wikibase precision, status).

    -9 = unknown (precision lowered), -7 = ongoing (no date), -8 = not applicable (no date)."""
    yi, mi, di = _int(y), _int(m), _int(d)
    if yi is None or yi == -8:
        return None, None, "not_applicable"
    if yi == -7:
        return None, None, "ongoing"
    if yi < 0:
        return None, None, "unknown"
    if mi is None or mi < 0:
        return f"{yi:04d}-01-01", 9, "year"
    if di is None or di < 0:
        return f"{yi:04d}-{mi:02d}-01", 10, "month"
    return date(yi, mi, di).isoformat(), 11, "day"


@dataclass
class Participation:
    kind: str  # inter | extra
    war: int
    war_name: str
    ccode: int
    state_name: str
    side: str
    spells: list[tuple[tuple[str | None, int | None, str], tuple[str | None, int | None, str]]]
    batdeath: str | None
    initiator: str | None
    outcome: str | None
    row: int
    extra: dict[str, Any] = field(default_factory=dict)


def parse_inter(rows: Sequence[Mapping[str, str]]) -> list[Participation]:
    _need(rows, ["WarNum", "WarName", "ccode", "StateName", "Side", "StartYear1", "EndYear1", "BatDeath"], "inter-state")
    out = []
    for i, r in enumerate(rows, start=2):
        spells = []
        for k in ("1", "2"):
            s = _war_date(r[f"StartYear{k}"], r[f"StartMonth{k}"], r[f"StartDay{k}"])
            e = _war_date(r[f"EndYear{k}"], r[f"EndMonth{k}"], r[f"EndDay{k}"])
            if s[0] is not None:
                spells.append((s, e))
        bd = r["BatDeath"]
        out.append(Participation(
            "inter", int(r["WarNum"]), r["WarName"], int(r["ccode"]), r["StateName"], r["Side"], spells,
            decimal_text(bd) if _int(bd) is not None and _int(bd) >= 0 else None,
            r.get("Initiator"), r.get("Outcome"), i, {"where_fought": r.get("WhereFought")},
        ))
    return out


def parse_extra(rows: Sequence[Mapping[str, str]]) -> list[Participation]:
    _need(rows, ["WarNum", "WarName", "ccode1", "SideA", "ccode2", "SideB", "StartYear1", "BatDeath"], "extra-state")
    out = []
    for i, r in enumerate(rows, start=2):
        spells = []
        for k in ("1", "2"):
            s = _war_date(r[f"StartYear{k}"], r[f"StartMonth{k}"], r[f"StartDay{k}"])
            e = _war_date(r[f"EndYear{k}"], r[f"EndMonth{k}"], r[f"EndDay{k}"])
            if s[0] is not None:
                spells.append((s, e))
        for col, side_col, side in (("ccode1", "SideA", "A"), ("ccode2", "SideB", "B")):
            c = _int(r[col])
            if c is None or c < 0:
                continue  # non-state side
            bd = r["BatDeath"]
            out.append(Participation(
                "extra", int(r["WarNum"]), r["WarName"], c, r[side_col], side, spells,
                decimal_text(bd) if _int(bd) is not None and _int(bd) >= 0 else None,
                r.get("Initiator"), r.get("Outcome"), i,
                {"where_fought": r.get("WhereFought"), "batdeath_scope": "state participant(s) battle deaths"},
            ))
    return out


# --------------------------------------------------------------------------- code resolution
class CodeResolver:
    """COW code + date -> (iso3 | None, reason, entity_qid | None, qid entry)."""

    def __init__(self, crosswalk: Any | None) -> None:
        self.crosswalk = crosswalk
        self.reasons: Counter[str] = Counter()

    def resolve(self, ccode: int, when: date) -> tuple[str | None, str, dict[str, Any] | None]:
        iso3, reason = None, "no_crosswalk_loaded"
        if self.crosswalk is not None:
            r = self.crosswalk.resolve("cow", ccode, when)
            iso3, reason = r.iso3, r.reason
        q = historical_qid("cow", str(ccode), when.year)
        self.reasons[reason] += 1
        return iso3, reason, q


def load_crosswalk(body: bytes | None) -> Any | None:
    if body is None:
        return None
    from reva.adapters.connectors.atlas_geopol_common import crosswalk_from_countrycode_tarball, crosswalk_keys

    return crosswalk_from_countrycode_tarball(body, known_keys=crosswalk_keys())


# --------------------------------------------------------------------------- atlas rows
def _row(dataset: str, indicator: str, ccode: int, abb: str, period: tuple[date, date], value: str, unit: str,
         raw_sha: str, selector: Mapping[str, Any], flags: dict[str, Any], resolver: CodeResolver,
         when: date) -> dict[str, Any]:
    iso3, reason, q = resolver.resolve(ccode, when)
    flags = {**flags, "cow_stateabb": abb, "iso3_resolution": reason, "terms": "cite COW; no charging for access"}
    if q is not None:
        flags["entity_mapping"] = {"qid": q["qid"], "label": q.get("wikidata_label_en"),
                                   "review": q.get("review_status", "agent_mapped")}
    return {
        "source": ATLAS_SOURCE, "dataset": dataset, "indicator": indicator, "concept_id": None,
        "iso3": iso3, "provider_geo": str(ccode), "entity_qid": q["qid"] if q else None,
        "period_start": period[0], "period_end": period[1], "value": value, "value_text": None, "unit": unit,
        "flags": canon(flags), "raw_sha256": raw_sha, "selector": canon(selector),
    }


def nmc_rows(rows: Sequence[Mapping[str, str]], raw_sha: str, resolver: CodeResolver) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cols = ["stateabb", "ccode", "year", "milex", "milper", "irst", "pec", "tpop", "upop", "cinc", "version"]
    _need(rows, cols, NMC_INNER)
    out = []
    missing: Counter[str] = Counter()
    seen: set[tuple[int, int]] = set()
    for i, r in enumerate(rows, start=2):
        c, y = int(r["ccode"]), int(r["year"])
        if (c, y) in seen:
            raise _err(f"{NMC_INNER}: duplicate state-year {c}/{y}")
        seen.add((c, y))
        for ind in ("milex", *NMC_UNITS):
            txt = r[ind]
            if txt in ("-9", "-9.0", ""):
                missing[ind] += 1
                continue
            v = decimal_text(txt)
            if v is None:
                raise _err(f"{NMC_INNER} line {i}: {ind}={txt!r} is not a number")
            if ind == "milex":
                unit = MILEX_UNIT_GBP if y <= 1913 else MILEX_UNIT_USD
            else:
                unit = NMC_UNITS[ind]
            flags = {"nmc_version": r["version"], "release": "NMC v7.0 (2026-02-20 documentation)"}
            if ind == "milex":
                flags["currency_break"] = "GBP until 1913, USD from 1914 (NMC documentation)"
            out.append(_row("nmc7", ind, c, r["stateabb"], year_period(y), v, unit, raw_sha,
                            {"file": f"NMCv7.zip!NMCv7/NMC-v7-abridged.zip!{NMC_INNER}", "line": i, "column": ind},
                            flags, resolver, date(y, 7, 1)))
    return out, {"state_years": len(seen), "missing_code_minus9": dict(missing)}


def system_rows(rows: Sequence[Mapping[str, str]], raw_sha: str, resolver: CodeResolver) -> list[dict[str, Any]]:
    _need(rows, ["stateabb", "ccode", "year"], "system2024.csv")
    out = []
    seen: set[tuple[int, int]] = set()
    for i, r in enumerate(rows, start=2):
        c, y = int(r["ccode"]), int(r["year"])
        if (c, y) in seen:
            continue  # the provider lists a state once per year; a repeat adds nothing (counted below)
        seen.add((c, y))
        out.append(_row("system2024", "system_member", c, r["stateabb"], year_period(y), "1",
                        "1 = member of the interstate system in that year", raw_sha,
                        {"file": "System2024.zip!System2024/system2024.csv", "line": i},
                        {"ssm_version": r.get("version")}, resolver, date(y, 7, 1)))
    return out


def war_rows(parts: Sequence[Participation], shas: Mapping[str, str], resolver: CodeResolver) -> list[dict[str, Any]]:
    out = []
    seen: set[tuple[str, int, int, str]] = set()
    for p in parts:
        if p.batdeath is None or not p.spells:
            continue
        years = [int(s[0][:4]) for (s, e) in p.spells if s[0]] + [int(e[0][:4]) for (s, e) in p.spells if e[0]]
        lo, hi = min(years), max(years)
        key = (p.kind, p.war, p.ccode, p.side)
        if key in seen:
            raise _err(f"duplicate war participation {key}")
        seen.add(key)
        flags = {
            "war_name": p.war_name, "war_type": WAR_KINDS[p.kind], "side": p.side, "outcome_code": p.outcome,
            "initiator_code": p.initiator, "state_name": p.state_name,
            "participation_spells": [{"start": s[0], "end": e[0], "end_status": e[2]} for s, e in p.spells],
            "period_note": "battle deaths over the whole participation (calendar years spanned)",
            **{k: v for k, v in p.extra.items() if v not in (None, "")},
        }
        ind = f"batdeath|type={p.kind}|war={p.war}|side={p.side}"
        out.append(_row("wars4", ind, p.ccode, p.state_name[:32], (date(lo, 1, 1), date(hi + 1, 1, 1)), p.batdeath,
                        "battle deaths (persons)", shas[p.kind],
                        {"file": FILES[p.kind], "line": p.row, "column": "BatDeath"}, flags, resolver,
                        date(lo, 7, 1)))
    return out


# --------------------------------------------------------------------------- KG
def kg_records(
    spells: Sequence[Spell],
    majors: Sequence[Spell],
    parts: Sequence[Participation],
    *,
    shas: Mapping[str, str],
    resolver: CodeResolver,
) -> dict[str, Any]:
    """Entities and statements (``reva.kg.model`` dataclasses) for :func:`reva.kg.model.load_batch`."""
    from reva.kg.model import KgEntity, KgStatement, KgTime

    ents: dict[str, Any] = {}
    sts: list[Any] = []
    names: dict[int, str] = {}
    for s in spells:
        names.setdefault(s.ccode, s.name)
    for p in parts:
        names.setdefault(p.ccode, p.state_name)
    for c, nm in sorted(names.items()):
        eid = f"cow:{c}"
        ents[eid] = KgEntity(qid=eid, kind="state_code", labels={"en": f"{nm} (COW {c})"},
                             descriptions={"en": "state as coded by a Correlates-of-War state number"})
    ents["cow:system:interstate"] = KgEntity(
        qid="cow:system:interstate", kind="international_system",
        labels={"en": "interstate system (Correlates of War)", "fr": "système interétatique (Correlates of War)"},
        descriptions={"en": "COW State System Membership v2024 (1816-2024)"})
    ents["cow:system:major_powers"] = KgEntity(
        qid="cow:system:major_powers", kind="international_system",
        labels={"en": "major powers (Correlates of War)", "fr": "grandes puissances (Correlates of War)"},
        descriptions={"en": "COW Major Powers v2024"})

    def t(d: date | str, prec: int = 11) -> Any:
        return KgTime(iso=d.isoformat() if isinstance(d, date) else d, precision=prec)

    for what, group, obj in (("ssm", spells, "cow:system:interstate"), ("major", majors, "cow:system:major_powers")):
        for s in group:
            q: dict[str, Any] = {"cow_stateabb": s.abb, "dataset": "COW State System Membership v2024"
                                 if what == "ssm" else "COW Major Powers v2024"}
            end = None
            if s.end >= DATA_END:
                q["observed_until"] = DATA_END.isoformat()
                q["note"] = "membership continues at the end of the data (2024-12-31)"
            else:
                end = t(s.end)
            if s.start <= DATA_START:
                q["start_note"] = "COW data begin on 1816-01-01; the state may have existed earlier"
            sts.append(KgStatement(
                subject_qid=f"cow:{s.ccode}", property_pid="P463", object_qid=obj, start=t(s.start), end=end,
                qualifiers=q, statement_id=f"{what}:{s.ccode}:{s.start.isoformat()}",
                raw_sha256=shas["states" if what == "ssm" else "majors"],
                references=[{"stated_in": "Correlates of War Project", "file": FILES["states" if what == "ssm" else "majors"],
                             "line": s.row}]))
    wars: dict[int, tuple[str, str]] = {}
    for p in parts:
        wars.setdefault(p.war, (p.war_name, p.kind))
        for k, (s, e) in enumerate(p.spells, start=1):
            q = {"side": p.side, "outcome_code": p.outcome, "initiator_code": p.initiator,
                 "war_type": WAR_KINDS[p.kind], "state_name_in_source": p.state_name}
            if p.batdeath is not None:
                q["battle_deaths_participant"] = p.batdeath
            if e[2] == "ongoing":
                q["end_status"] = "ongoing at the end of the data"
            sts.append(KgStatement(
                subject_qid=f"cow:{p.ccode}", property_pid="P1344", object_qid=f"cow:war:{p.war}",
                start=t(s[0], s[1]) if s[0] else None, end=t(e[0], e[1]) if e[0] else None, qualifiers=q,
                statement_id=f"war:{p.kind}:{p.war}:{p.ccode}:{p.side}:{k}", raw_sha256=shas[p.kind],
                references=[{"stated_in": "COW War Data v4.0", "file": FILES[p.kind], "line": p.row}]))
    for w, (nm, kind) in sorted(wars.items()):
        eid = f"cow:war:{w}"
        ents[eid] = KgEntity(qid=eid, kind="war", labels={"en": nm},
                             descriptions={"en": f"{WAR_KINDS[kind]} #{w} (COW War Data v4.0)"})
        sts.append(KgStatement(subject_qid=eid, property_pid="P31", object_qid=Q_WAR,
                               qualifiers={"cow_war_type": WAR_KINDS[kind]}, statement_id=f"wartype:{w}",
                               raw_sha256=shas[kind]))
    for e in historical_qids("cow"):
        lo, hi = e.get("valid_from_year"), e.get("valid_to_year")
        sts.append(KgStatement(
            subject_qid=f"cow:{e['code']}", property_pid="cow:code_denotes", object_qid=e["qid"],
            start=t(f"{lo:04d}-01-01", 9) if lo else None, end=t(f"{hi:04d}-01-01", 9) if hi else None,
            qualifiers={"mapping": e.get("review_status", "agent_mapped"), "evidence": e.get("evidence", "")[:500],
                        "file": "registry/entities/historical_states_qids.json"},
            statement_id=f"denotes:{e['code']}:{lo}:{hi}:{e['qid']}", raw_sha256=shas["states"]))
    return {"entities": list(ents.values()), "statements": sts}


KG_CUSTOM_PROPERTIES = [
    {"pid": "cow:code_denotes", "value_kind": "item", "reva_predicate": "code_denotes",
     "label_en": "COW state code denotes", "label_fr": "le code d'État COW désigne",
     "inverse_name": "denoted_by_cow_code", "inverse_label_en": "denoted by COW state code",
     "inverse_label_fr": "désigné par le code d'État COW"},
]
KG_WIKIDATA_PIDS = ("P31", "P463", "P1344")


def ensure_kg_properties(engine: Any) -> dict[str, Any]:
    """Register P31/P463/P1344 with WDHARVEST's own definitions and the dataset predicate ``cow:code_denotes``."""
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import KgProperty, register_properties

    allow = load_allowlist()
    props = [p for p in kg_properties(allow) if p.pid in KG_WIKIDATA_PIDS]
    if len(props) != len(KG_WIKIDATA_PIDS):
        raise ValueError(f"{KG_WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    props += [KgProperty(**p) for p in KG_CUSTOM_PROPERTIES]
    return register_properties(engine, props).__dict__


def load_kg(engine: Any, store: Any, kg: Mapping[str, Any], *, raw_sha256s: Sequence[str], retrieved_at: Any,
            origin: str) -> dict[str, Any]:
    from reva.kg.model import load_batch

    ensure_kg_properties(engine)
    rep = load_batch(engine, store, entities=kg["entities"], statements=kg["statements"], source_key=SOURCE_KEY,
                     raw_sha256s=sorted(set(raw_sha256s)), retrieved_at=retrieved_at, origin=origin,
                     parameters={"connector": NAME, "parser": PARSER_VERSION})
    return rep.as_dict()


# --------------------------------------------------------------------------- connector
class CowConnector:
    """COW State System Membership v2024, Major Powers v2024, NMC v7.0, War Data v4.0 (+ countrycode)."""

    def __init__(self, *, with_crosswalk: bool = True) -> None:
        self.with_crosswalk = with_crosswalk
        self.kg: dict[str, Any] | None = None

    def describe(self) -> ConnectorSpec:
        return atlas_spec(
            name=NAME,
            version=VERSION,
            source_key=SOURCE_KEY,
            provider="Correlates of War Project",
            licence="COW terms of use: cite; no charging for access (not an open licence)",
            allowed_hosts=(HOST, "cran.r-project.org"),
            dataset_ids=("ssm_v2024", "majors_v2024", "nmc_v7.0", "war_v4.0"),
            field_mappings={"nmc": NMC_INNER, "system": MEMBERS["system"], "wars": "BatDeath"},
            units={"milex": f"{MILEX_UNIT_GBP} / {MILEX_UNIT_USD}", **NMC_UNITS},
            geography=("COW state numbers (ccode), resolved by date through KGGEOPOL's StateCodeCrosswalk",),
            time_semantics="state-year (calendar year); membership spells and war participation with day dates",
            lineage_family="correlates_of_war",
            cadence_seconds=90 * DAY,
            revision_strategy="file bytes changed -> new atlas table version; new KG statement revision",
            max_response_bytes=MAX_BYTES,
            max_pages=len(FILES) + 1,
            parser_version=PARSER_VERSION,
            code_digest=code_digest(),
        )

    def fetch_items(self) -> tuple[FetchItem, ...]:
        items = [FetchItem(key=k, url=f"{BASE}/{f}", max_bytes=MAX_BYTES,
                           expected_media_types=("application/zip", "text/csv", "application/octet-stream"))
                 for k, f in FILES.items()]
        if self.with_crosswalk:
            from reva.adapters.connectors.atlas_geopol_common import COUNTRYCODE_URL

            items.append(FetchItem(key="countrycode", url=COUNTRYCODE_URL, max_bytes=5_000_000,
                                   expected_media_types=("application/x-gzip", "application/gzip",
                                                         "application/octet-stream")))
        return tuple(items)

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        return ()

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch_plain(item, http)

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        by = {r.item.key: r for r in raws}
        need = set(FILES) | ({"countrycode"} if self.with_crosswalk else set())
        if set(by) != need:
            raise _err(f"expected {sorted(need)}, got {sorted(by)}")
        shas = {k: sha256_hex(r.body) for k, r in by.items()}
        resolver = CodeResolver(load_crosswalk(by["countrycode"].body) if self.with_crosswalk else None)
        states = read_csv(zip_member(by["states"].body, MEMBERS["states"]), "statelist2024.csv")
        spells = parse_spells(states, "statelist2024.csv")
        names = {s.ccode: s.name for s in spells}
        majors = parse_spells(read_csv(zip_member(by["majors"].body, MEMBERS["majors"]), "majors2024.csv"),
                              "majors2024.csv", names=names)
        system = read_csv(zip_member(by["system"].body, MEMBERS["system"]), "system2024.csv")
        inner = zip_member(by["nmc"].body, MEMBERS["nmc"])
        nmc = read_csv(zip_member(inner, NMC_INNER), NMC_INNER)
        parts = parse_inter(read_csv(by["inter"].body, FILES["inter"])) + \
            parse_extra(read_csv(by["extra"].body, FILES["extra"]))

        nmc_r, nmc_rep = nmc_rows(nmc, shas["nmc"], resolver)
        sys_r = system_rows(system, shas["system"], resolver)
        war_r = war_rows(parts, shas, resolver)
        self.kg = kg_records(spells, majors, parts, shas=shas, resolver=resolver)
        xw = [shas["countrycode"]] if self.with_crosswalk else []
        unresolved = sorted({int(r["provider_geo"]) for r in nmc_r if r["iso3"] is None})
        report = {
            "spells": len(spells), "major_spells": len(majors), "states": len(names),
            "nmc": nmc_rep, "system_rows": len(sys_r), "war_participations": len(parts),
            "war_rows": len(war_r), "kg_entities": len(self.kg["entities"]),
            "kg_statements": len(self.kg["statements"]), "code_resolution": dict(resolver.reasons),
            "nmc_codes_without_iso3": unresolved,
        }
        cite = RIGHTS["attribution"]
        return [
            AtlasBuild(ATLAS_SOURCE, "nmc7", frame(nmc_r), [shas["nmc"], *xw], PARSER_VERSION,
                       "COW National Material Capabilities v7.0 (1816-2022), abridged file; -9 omitted; milex GBP "
                       "until 1913 and USD from 1914. " + cite, rights=dict(RIGHTS), report=report),
            AtlasBuild(ATLAS_SOURCE, "system2024", frame(sys_r), [shas["system"], *xw], PARSER_VERSION,
                       "COW State System Membership v2024, state-year list (system2024.csv). " + cite,
                       rights=dict(RIGHTS), report={"rows": len(sys_r)}),
            AtlasBuild(ATLAS_SOURCE, "wars4", frame(war_r), [shas["inter"], shas["extra"], *xw], PARSER_VERSION,
                       "COW War Data v4.0: battle deaths per state participation in inter-state and extra-state "
                       "wars (-9 unknown omitted). " + cite, rights=dict(RIGHTS), report={"rows": len(war_r)}),
        ]


def coverage_by_century(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        y = r["period_start"].year
        out[f"{(y - 1) // 100 + 1:02d}"] += 1
    return dict(sorted(out.items()))
