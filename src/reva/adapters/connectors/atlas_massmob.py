"""Mass Mobilization Project protests against governments, 1990-2020 (agent A-CONTENTION, V3).

Source and access
-----------------
David H. Clark & Patrick M. Regan, *Mass Mobilization Protest Data*, Harvard Dataverse ``doi:10.7910/DVN/HTTWYL``,
version **5.1** (2022-10-10), licence **CC0 1.0** (version record, checked on every run). File 4291456
``mmALL_073120_csv.tab`` (Dataverse tab export of ``mmALL_073120.csv``; 17,145 rows: 15,239 protests and 1,906
country-years coded ``protest = 0``), checked against the version record's ``filesize``; the users manual
(``MM_users_manual_0515.pdf``, file 2775560, md5 pinned) is the codebook quoted below.

The project codes "any protest event where the protest targets the government, and where it involves at least
50 people", from Lexis-Nexis searches of major newspapers (manual): start/end dates, location, protester
violence, participants (as reported and, part-way through the coding, a category), protester identity, up to
four demands (7 categories) and up to seven state responses (7 categories).

**Not stored**: the ``sources`` (newspaper citations with journalists' names) and ``notes`` (often verbatim news
excerpts) columns -- copyrighted third-party text and personal names; they are read only for the row checks.

Outputs
-------
* atlas ``mmp.protests_cy`` -- COMPUTED counts per country-year (exact): protests, with protester violence,
  per demand, per state response, per participants category. Country-years that MMP codes ``protest = 0`` are
  **provider-reported zeros** (flagged); country-years absent from the file are not covered. Countries via geo
  scheme ``mmp_loc``; the protest's year is MMP's ``year`` (= start year).
* KG: protest events ``mmp:<id>`` and, when the state responded (any response other than ``ignore``), a
  state-response event ``mmp:<id>:state_response`` linked with ``contention:directed_at``.
* Causal claims (tier T1): ``reaction_to`` -- state response -> the protest it answers ("We identify seven
  types of actions (or inaction) the state might take in response to protests", manual). ``ignore`` makes no
  claim: the manual says it may be the press ignoring the response rather than the state not responding.
  Demands are goals, not causes: no claim.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import AtlasRows, atlas_frame, kg_available
from reva.adapters.connectors.atlas_navco import (
    HOSTS,
    LICENCE,
    LICENCE_URL,
    ContentionParseError,
    Counts,
    DvFile,
    Kg,
    Parsed,
    Places,
    claim,
    claim_counts,
    count_rows,
    deliver,
    delivery,
    fetch_parse_file,
    flag01,
    geo_key,
    int_val,
    load_contention_kg,
)

SOURCE_KEY = "harvard.dataverse.massmob"
CONNECTOR_NAME = "atlas.massmob"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-contention-massmob-1"
ATLAS_SOURCE = "mmp"

FILE = DvFile(
    "mmp",
    "Mass Mobilization v5.1",
    "doi:10.7910/DVN/HTTWYL",
    "5.1",
    4291456,
    "mmALL_073120_csv.tab",
    16000,
    (
        "id",
        "country",
        "ccode",
        "year",
        "region",
        "protest",
        "protestnumber",
        "startday",
        "startmonth",
        "startyear",
        "endday",
        "endmonth",
        "endyear",
        "protesterviolence",
        "location",
        "participants_category",
        "participants",
        "protesteridentity",
        *(f"protesterdemand{i}" for i in range(1, 5)),
        *(f"stateresponse{i}" for i in range(1, 8)),
    ),
    2775560,
    "MM_users_manual_0515.pdf",
    "efb7ca11ee0eadae9122b2b7cee08560",
    "03541007078e661e4df11863f5c62fbe9adb3f745e91547446d05737fcf7cd45",
    "Clark, David; Regan, Patrick, 'Mass Mobilization Protest Data', Harvard Dataverse, doi:10.7910/DVN/HTTWYL, V5.1",
)

#: Users manual (May 2015), quoted verbatim (whitespace joined; typographic quotes as ASCII).
MANUAL = {
    "protest": "The project defines a protest as a gathering of 50 or more people to make a demand of the government. "
    "A protest action must be targeted at the state or state policy.",
    "state_response": "We identify seven types of actions (or inaction) the state might take in response to "
    "protests. The state often responds in multiple ways; we code up to seven government responses to a protest "
    "event.",
    "killings": "it is important to note government intent to kill is not part of the coding process; we code "
    "whether or not government kills protesters, not whether they mean to.",
    "ignore": "ignoring a protest action can be the result of two different processes. One is that the news outlets "
    "simply ignore the response by the state; the other is that the state does not react to the protest action.",
    "participants": "We relied on the lower of any estimate at one point in time, but if a protest extended beyond "
    "one day and the number of protesters grew with time, we would rely on the maximum number of participants at "
    "any point in the protest.",
}

DEMANDS = {
    "labor wage dispute": "labour_wage_dispute",
    "land farm issue": "land_farm_issue",
    "police brutality": "police_brutality",
    "political behavior, process": "political_behaviour_process",
    "price increases, tax policy": "price_increases_tax_policy",
    "removal of politician": "removal_of_politician",
    "social restrictions": "social_restrictions",
}
RESPONSES = {
    "accomodation": "accommodation",  # provider spelling
    "arrests": "arrests",
    "beatings": "beatings",
    "crowd dispersal": "crowd_dispersal",
    "ignore": "ignore",
    "killings": "killings",
    "shootings": "shootings",
}
PARTICIPANT_CATEGORIES = {
    "50-99": "50_99",
    "100-999": "100_999",
    "1000-1999": "1000_1999",
    "2000-4999": "2000_4999",
    "5000-10000": "5000_10000",
    ">10000": "gt_10000",
}
BLANK = ("", ".")


@dataclass
class Protest:
    line: int
    pid: str
    country: str
    ccode: str
    year: int
    start: date
    end: date
    violence: bool | None
    location: str
    participants: str
    category: str | None
    identity: str
    demands: list[str]
    responses: list[str]


@dataclass
class NoProtestYear:
    line: int
    pid: str
    country: str
    ccode: str
    year: int


def _date(r: Mapping[str, str], p: str, w: str) -> date:
    try:
        return date(
            int_val(r[f"{p}year"], what=w) or 0,
            int_val(r[f"{p}month"], what=w) or 0,
            int_val(r[f"{p}day"], what=w) or 0,
        )
    except ValueError as e:
        raise ContentionParseError(f"{w}: invalid {p} date", retryable=False, kind="schema_drift") from e


def mmp_rows(p: Parsed) -> tuple[list[Protest], list[NoProtestYear], dict[str, Any]]:
    protests: list[Protest] = []
    zeros: list[NoProtestYear] = []
    stats: Counter[str] = Counter()
    for line, r in p.table:
        w = f"MMP line {line}"
        flag = flag01(r["protest"], what=f"{w} protest")
        year = int_val(r["year"], what=f"{w} year")
        if year is None or flag is None or not r["country"] or not r["id"]:
            raise ContentionParseError(f"{w}: missing id/country/year/protest", retryable=False, kind="schema_drift")
        if not flag:
            zeros.append(NoProtestYear(line, r["id"], r["country"], r["ccode"], year))
            continue
        start, end = _date(r, "start", w), _date(r, "end", w)
        if end < start or start.year != year:
            raise ContentionParseError(f"{w}: dates inconsistent with year", retryable=False, kind="schema_drift")
        demands = []
        for i in range(1, 5):
            v = r[f"protesterdemand{i}"]
            if v in BLANK:
                stats["blank_demand_slots"] += v == "."
                continue
            if v not in DEMANDS:
                raise ContentionParseError(f"{w}: unknown demand {v!r}", retryable=False, kind="schema_drift")
            if v not in demands:
                demands.append(v)
        responses = []
        for i in range(1, 8):
            v = r[f"stateresponse{i}"]
            if v in BLANK:
                continue
            if v not in RESPONSES:
                raise ContentionParseError(f"{w}: unknown state response {v!r}", retryable=False, kind="schema_drift")
            if v not in responses:
                responses.append(v)
        cat = r["participants_category"] or None
        if cat is not None and cat not in PARTICIPANT_CATEGORIES:
            raise ContentionParseError(f"{w}: participants_category {cat!r}", retryable=False, kind="schema_drift")
        pv = r["protesterviolence"]
        protests.append(
            Protest(
                line=line,
                pid=r["id"],
                country=r["country"],
                ccode=r["ccode"],
                year=year,
                start=start,
                end=end,
                violence=None if pv in BLANK else flag01(pv, what=f"{w} protesterviolence"),
                location=r["location"],
                participants=r["participants"] if r["participants"] not in BLANK else "",
                category=cat,
                identity=r["protesteridentity"],
                demands=demands,
                responses=responses,
            )
        )
    ids = [x.pid for x in protests] + [z.pid for z in zeros]
    if len(set(ids)) != len(ids):
        raise ContentionParseError("MMP: duplicate ids", retryable=False, kind="schema_drift")
    stats.update(protests=len(protests), no_protest_country_years=len(zeros))
    return protests, zeros, dict(stats)


INDICATORS: dict[str, str] = {
    "protests": "protest events (>= 50 people, demand on the government) with this year (MMP 'year' = start year)",
    "protests|protester_violence=1": "protests with protesterviolence = 1",
    "protests|protester_violence=0": "protests with protesterviolence = 0",
    **{f"protests|demand={s}": f"protests with demand '{d}' among protesterdemand1-4" for d, s in DEMANDS.items()},
    **{
        f"protests|response={s}": f"protests with state response '{d}' among stateresponse1-7"
        for d, s in RESPONSES.items()
    },
    **{
        f"protests|participants_category={s}": f"protests with participants_category '{c}' (category added part-way "
        "through the coding; not available for all protests)"
        for c, s in PARTICIPANT_CATEGORIES.items()
    },
}


def protests_cy(
    protests: Sequence[Protest], zeros: Sequence[NoProtestYear], places: Places, *, raw_sha256: str
) -> AtlasRows:
    c = Counts()
    zero_lines: dict[tuple[str, int], int] = {}
    for z in zeros:
        c.touch(geo_key(places, z.country), z.year, z.country)
        zero_lines[(geo_key(places, z.country), z.year)] = z.line
    for p in protests:
        g = geo_key(places, p.country)
        c.touch(g, p.year, p.country)
        inds = ["protests"]
        if p.violence is not None:
            inds.append(f"protests|protester_violence={int(p.violence)}")
        inds += [f"protests|demand={DEMANDS[d]}" for d in p.demands]
        inds += [f"protests|response={RESPONSES[r]}" for r in p.responses]
        if p.category:
            inds.append(f"protests|participants_category={PARTICIPANT_CATEGORIES[p.category]}")
        for ind in inds:
            c.add(g, p.year, ind, p.pid, p.line, p.country)
    rows = count_rows(
        c,
        source=ATLAS_SOURCE,
        dataset="protests_cy",
        indicators=INDICATORS,
        raw_sha256=raw_sha256,
        file=FILE.file_name,
        method="count of MMP protest rows (protest = 1; items = MMP ids) per country and year; countries mapped "
        "through geo scheme mmp_loc; a country-year coded protest = 0 gives provider-reported zeros",
        computed_from=f"Mass Mobilization Protest Data v5.1 ({FILE.revision}); 15,239 protests 1990-2020",
        extra_flags={"protest_definition": MANUAL["protest"]},
        zero_lines=zero_lines,
    )
    zero_keys = {(geo_key(places, z.country), z.year) for z in zeros}
    _mark_zero_years(rows, zero_keys)
    return rows


def _mark_zero_years(rows: AtlasRows, zero_keys: set[tuple[str, int]]) -> None:
    """Flag the cells of country-years MMP codes ``protest = 0`` as provider-reported zeros."""
    import json

    cols = rows.cols
    for i in range(len(rows)):
        geo = cols["iso3"][i] or f"name:{cols['provider_geo'][i]}"
        if (geo, cols["period_start"][i].year) in zero_keys:
            f = json.loads(cols["flags"][i])
            f["zero_semantics"] = "MMP codes this country-year protest = 0 (no protest coded): provider-reported zero"
            cols["flags"][i] = json.dumps(f, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _label(p: Protest) -> str:
    who = p.identity if p.identity and p.identity.lower() not in ("unspecified", "protesters", "demonstrators") else ""
    return f"Protest{' by ' + who if who else ''} in {p.country}, {p.start.isoformat()} (MMP {p.pid})"


def mmp_kg(protests: Sequence[Protest], places: Places, *, raw_sha256: str) -> Kg:
    kg = Kg("mmp", raw_sha256, FILE.file_name)
    for p in protests:
        ev = f"mmp:{p.pid}"
        k = p.pid
        pl = places.resolve(p.country, p.year)
        kg.entity(
            ev,
            "event",
            _label(p),
            f"protest against the government coded by the Mass Mobilization Project ({p.country}, {p.start.isoformat()} - {p.end.isoformat()})",
        )
        kg.string(
            f"{k}:event_type", ev, "contention:event_type", "protest", line=p.line, qualifiers={"dataset": FILE.title}
        )
        kg.item(
            f"{k}:location",
            ev,
            "contention:location",
            pl.node,
            line=p.line,
            column="country",
            qualifiers={"provider_country": p.country, "polity_ccode": p.ccode, "resolution": pl.how},
        )
        if p.location:
            kg.string(f"{k}:location_text", ev, "contention:location_text", p.location, line=p.line, column="location")
        kg.time(
            f"{k}:start",
            ev,
            "P580",
            p.start.isoformat(),
            11,
            line=p.line,
            column=["startyear", "startmonth", "startday"],
        )
        kg.time(f"{k}:end", ev, "P582", p.end.isoformat(), 11, line=p.line, column=["endyear", "endmonth", "endday"])
        if p.violence is not None:
            kg.string(
                f"{k}:violence",
                ev,
                "contention:protester_violence",
                "yes" if p.violence else "no",
                line=p.line,
                column="protesterviolence",
            )
        if p.participants:
            kg.string(
                f"{k}:participants",
                ev,
                "contention:participants_reported",
                p.participants,
                line=p.line,
                column="participants",
            )
        if p.category:
            kg.string(
                f"{k}:participants_category",
                ev,
                "contention:participants_category",
                p.category,
                line=p.line,
                column="participants_category",
            )
        if p.identity:
            kg.string(
                f"{k}:identity",
                ev,
                "contention:protester_identity",
                p.identity,
                line=p.line,
                column="protesteridentity",
            )
        for d in p.demands:
            kg.string(
                f"{k}:demand:{DEMANDS[d]}",
                ev,
                "contention:protester_demand",
                d,
                line=p.line,
                column=[f"protesterdemand{i}" for i in range(1, 5)],
            )
        for r in p.responses:
            kg.string(
                f"{k}:response:{RESPONSES[r]}",
                ev,
                "contention:state_response",
                r,
                line=p.line,
                column=[f"stateresponse{i}" for i in range(1, 8)],
            )
        acts = [r for r in p.responses if r != "ignore"]
        if acts:
            sr = f"{ev}:state_response"
            kg.entity(
                sr,
                "event",
                f"State response to protest MMP {p.pid} ({p.country}, {p.start.isoformat()}): {', '.join(acts)}",
                "state response coded by the Mass Mobilization Project; dated by the protest it answers",
            )
            kg.string(f"{k}:sr:type", sr, "contention:event_type", "state_response", line=p.line)
            for r in acts:
                q = {"note": MANUAL["killings"]} if r in ("killings", "shootings", "beatings") else {}
                kg.string(
                    f"{k}:sr:{RESPONSES[r]}",
                    sr,
                    "contention:state_response",
                    r,
                    line=p.line,
                    column=[f"stateresponse{i}" for i in range(1, 8)],
                    qualifiers=q,
                )
            kg.item(
                f"{k}:sr:directed_at",
                sr,
                "contention:directed_at",
                ev,
                line=p.line,
                qualifiers={"definition": MANUAL["state_response"]},
            )
            kg.item(f"{k}:sr:location", sr, "contention:location", pl.node, line=p.line, column="country")
            kg.time(
                f"{k}:sr:start",
                sr,
                "P580",
                p.start.isoformat(),
                11,
                line=p.line,
                column=["startyear", "startmonth", "startday"],
                qualifiers={"dating": "the protest's dates (MMP codes no separate date for the response)"},
            )
            kg.time(
                f"{k}:sr:end", sr, "P582", p.end.isoformat(), 11, line=p.line, column=["endyear", "endmonth", "endday"]
            )
    kg.entities.extend(places.entities())
    return kg


def mmp_claims(protests: Sequence[Protest], *, raw_sha256: str) -> list[dict[str, Any]]:
    out = []
    for p in protests:
        acts = [r for r in p.responses if r != "ignore"]
        if not acts:
            continue
        out.append(
            claim(
                key=f"{SOURCE_KEY}:mmp:{p.pid}:state_response:reaction_to",
                cause=f"mmp:{p.pid}",
                effect=f"mmp:{p.pid}:state_response",
                relation="reaction_to",
                f=FILE,
                source_key=SOURCE_KEY,
                raw_sha256=raw_sha256,
                selector={
                    "file": FILE.file_name,
                    "line": p.line,
                    "columns": [f"stateresponse{i}" for i in range(1, 8)],
                },
                coded_value=", ".join(acts),
                codebook_term="state_response",
                scope={"place": p.country},
            )
        )
    return out


def build(p: Parsed, *, qid_for_iso3: Mapping[str, str] | None = None) -> dict[str, Any]:
    protests, zeros, stats = mmp_rows(p)
    places = Places("mmp_loc", "mmp", qid_for_iso3)
    kg = mmp_kg(protests, places, raw_sha256=p.sha)
    return {
        "protests_cy": protests_cy(protests, zeros, places, raw_sha256=p.sha),
        "entities": kg.entities,
        "statements": kg.statements,
        "claims": {"mmp": mmp_claims(protests, raw_sha256=p.sha)},
        "stats": stats,
        "years": [min(x.year for x in protests), max(x.year for x in protests)] if protests else None,
        "unmapped_locations": sorted({x.country for x in protests if places.iso3(x.country) is None}),
    }


RIGHTS = {"licence": LICENCE, "licence_url": LICENCE_URL, "attribution": FILE.citation}


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Harvard Dataverse (Clark & Regan, Mass Mobilization Project)",
        licence=LICENCE,
        allowed_hosts=HOSTS,
        datasets=("protests_cy",),
        dataset_ids=(FILE.revision,),
        cadence_seconds=180 * 86400,
        planned_requests=3,
        max_response_bytes=20_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "geo": "country names -> geo scheme mmp_loc",
            "time": "protest start/end dates",
            "kg": "mmp:<id>",
        },
        notes="Pinned Dataverse version 5.1; sources/notes columns (news citations and excerpts) are not stored.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    p = await fetch_parse_file(run, FILE, strict=run.origin == "live", max_bytes=20_000_000)
    out = build(p)
    run.archive(p.meta_raw)
    run.archive(p.data_raw)
    res: dict[str, Any] = {
        "stats": out["stats"],
        "years": out["years"],
        "unmapped_locations": out["unmapped_locations"],
        "atlas": {},
    }
    if run.wants("protests_cy"):
        r = run.register(
            dataset="protests_cy",
            frame=atlas_frame(out["protests_cy"]),
            raw_sha256s=[p.sha],
            rights=RIGHTS,
            coverage_note="COMPUTED counts per country-year from the Mass Mobilization Protest Data v5.1 (protests "
            ">= 50 people against the government, 1990-2020); protest = 0 country-years are provider-reported zeros",
        )
        res["atlas"]["protests_cy"] = {
            k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")
        }
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    res["claims"] = claim_counts(out["claims"])
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = load_contention_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision="mmp-v5.1",
            raw_sha256s=[p.sha],
            retrieved_at=p.data_raw.retrieved_at,
            origin=run.origin,
        )
    if run.dataset is None:
        d = delivery(
            FILE,
            "mmp",
            out["claims"]["mmp"],
            source_key=SOURCE_KEY,
            raw_sha256=p.sha,
            retrieved_at=p.data_raw.retrieved_at,
            origin=run.origin,
        )
        res["claims_delivery"] = deliver(run, [d])
    return res
