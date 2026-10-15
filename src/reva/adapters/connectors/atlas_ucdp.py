"""UCDP (Uppsala Conflict Data Program) armed conflicts and fatalities (agent KGGEOPOL).

Source and access
-----------------
UCDP datasets, version **26.1** (released 2026-03/06), are downloaded as the official CSV zips from
``https://ucdp.uu.se/downloads/`` -- one request per dataset. The UCDP API
(``ucdpapi.pcr.uu.se``) now requires an access token (``x-ucdp-access-token``; HTTP 401 without it,
checked 2026-10-15), so it is not used. Licence: **CC BY 4.0** (downloads page: "All datasets are free
of charge and licensed under CC BY 4.0 ... provided you cite the relevant publications").

Files (``FILES``):

* ``acd`` -- UCDP/PRIO Armed Conflict Dataset 26.1: one row per conflict-year 1946-2025 (state-based
  armed conflicts, >= 25 battle-related deaths in a year): sides, GW codes of the primary (``gwno_a``,
  ``gwno_b``) and secondary (``*_2nd``) state parties, location (``gwno_loc``), incompatibility, type,
  intensity (1 = 25-999, 2 = >= 1000 battle-related deaths), start dates and episode ends.
* ``brd_conf`` -- UCDP Battle-Related Deaths Dataset 26.1 (conflict level), 1989-2025: best/low/high.
* ``orgvio_cy`` -- UCDP Country-Year Dataset on Organized Violence within Country Borders 26.1,
  1989-2025: UCDP's own country-year aggregation of the georeferenced events (GED) for state-based,
  non-state and one-sided violence. **UCDP codes 0 when GED records no fatalities** for a
  country-year (codebook 26.1 §2); those zeros are provider-reported and kept, with the codebook's
  caveat in the flags.
* ``actor`` -- UCDP Actor Dataset 26.1 (actor id -> name), for actor labels.

Outputs
-------
* Atlas table ``ucdp.orgvio_cy`` -- the CY fatality columns verbatim (indicator = UCDP column name),
  provider_geo ``gw:<country_id>``.
* Atlas table ``ucdp.acd_cy`` -- **computed** counts from the ACD (flagged ``computed_from``):
  number of active conflicts per country-year by role (location / primary party / secondary party)
  and at war intensity. Pre-1989 UCDP has no fatality figures, only these.
* KG entities ``ucdp:conflict:<id>``, ``ucdp:actor:<id>`` and statements (see :func:`kg_records`):
  primary/secondary party, location, non-state side, incompatibility, type, start date, intensity per
  year and battle-related deaths per year (best, with low/high bounds).

GW codes map to crosswalk entities through :class:`StateCodeCrosswalk` (date-aware, at the year's
1 July); unmapped codes are kept with ``iso3 = null``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import (
    COUNTRYCODE_VERSION,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
    atlas_frame,
    code_correspondence,
    fetch_countrycode,
    int_or_none,
    iso_date_or_none,
    kg_available,
    load_kg,
    load_qid_for_iso3,
    merge_years,
    read_csv_member,
)
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

SOURCE_KEY = "ucdp.downloads"
CONNECTOR_NAME = "atlas.ucdp"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-ucdp-parse-1"
UCDP_VERSION = "26.1"
HOST = "ucdp.uu.se"
BASE = f"https://{HOST}/downloads/"
ATLAS_SOURCE = "ucdp"
KG_SOURCE_KEY = "ucdp.downloads"

#: key -> (path under /downloads/, CSV member, minimum expected rows)
FILES: dict[str, tuple[str, str, int]] = {
    "acd": ("ucdpprio/ucdp-prio-acd-261-csv.zip", "UcdpPrioConflict_v26_1.csv", 2500),
    "brd_conf": ("brd/ucdp-brd-conf-261-csv.zip", "BattleDeaths_v26_1_conf.csv", 1400),
    "orgvio_cy": ("organizedviolencecy/organizedviolencecy-261-csv.zip", "OrganizedViolenceCYDataSet26_1.csv", 6500),
    "actor": ("actor/ucdp-actor-261-csv.zip", "Actor_v26_1.csv", 1800),
}
MAX_BYTES = 5_000_000
#: The actor list is Windows-1252 encoded ("Government of R\xe9publique ..."); the others are UTF-8.
ENCODINGS = {"actor": "cp1252"}

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "Uppsala Conflict Data Program (UCDP), Department of Peace and Conflict Research, Uppsala "
    "University, datasets version 26.1 (ucdp.uu.se/downloads)",
    "citations": "Gleditsch et al. 2002 (JPR 39(5)); Davies et al. 2025 (JPR) for ACD/BRD; Sundberg & Melander "
    "2013 (JPR 50(4)) for GED/OrganizedViolenceCY",
}

INCOMPATIBILITY = {"1": "territory", "2": "government", "3": "government_and_territory"}
CONFLICT_TYPE = {"1": "extrasystemic", "2": "interstate", "3": "intrastate", "4": "internationalized_intrastate"}
#: ACD start_prec / ep_end_prec -> Wikibase time precision (UCDP codebook: 1 day ... 5 year; 6/7 = a
#: year-level estimate / "unclear"): 1 -> 11 (day), 2-3 -> 10 (month-ish: UCDP 'week'/'month'), 4-5 -> 9.
_PREC = {"1": 11, "2": 10, "3": 10, "4": 9, "5": 9, "6": 9, "7": 9}

#: orgvio_cy columns kept as atlas indicators (unit) -- fatality estimates and dyad counts.
CY_INDICATORS: dict[str, str] = {
    **{f"sb_total_deaths_{k}": "deaths" for k in ("best", "low", "high")},
    "sb_deaths_parties": "deaths",
    "sb_deaths_civilians": "deaths",
    "sb_deaths_unknown": "deaths",
    **{f"sb_intrastate_deaths_{k}": "deaths" for k in ("best", "low", "high")},
    **{f"sb_interstate_deaths_{k}": "deaths" for k in ("best", "low", "high")},
    **{f"ns_total_deaths_{k}": "deaths" for k in ("best", "low", "high")},
    **{f"os_total_deaths_{k}": "deaths" for k in ("best", "low", "high")},
    "os_govt_killings_best": "deaths",
    "os_nsgroup_killings_best": "deaths",
    **{f"cumulative_total_deaths_in_orgvio_{k}": "deaths" for k in ("best", "low", "high")},
    "sb_dyad_count": "count",
    "ns_dyad_count": "count",
    "os_dyad_count": "count",
}
CY_ZERO_NOTE = (
    "UCDP codes 0 when GED records no fatalities for the country-year; fatalities of dyads "
    "below the 25-deaths inclusion threshold are not counted (codebook 26.1 section 2)"
)


class UcdpParseError(SourceResponseError):
    pass


def fetch_items() -> tuple[FetchItem, ...]:
    return tuple(
        FetchItem(
            key=k,
            url=BASE + path,
            max_bytes=MAX_BYTES,
            expected_media_types=("application/zip", "application/octet-stream", "application/x-zip-compressed"),
        )
        for k, (path, _m, _n) in FILES.items()
    )


def parse_file(key: str, body: bytes, *, strict: bool = True) -> list[dict[str, str]]:
    """``strict``: enforce the minimum row count (truncation guard); relaxed only for trimmed test fixtures."""
    _path, member, min_rows = FILES[key]
    rows = read_csv_member(body, member, encoding=ENCODINGS.get(key, "utf-8-sig"))
    if strict and len(rows) < min_rows:
        raise UcdpParseError(
            f"UCDP {key}: {len(rows)} rows < expected {min_rows} (truncated?)", retryable=False, kind="incomplete"
        )
    versions = {r.get("version") or r.get("Version") for r in rows}
    if versions != {UCDP_VERSION}:
        raise UcdpParseError(
            f"UCDP {key}: unexpected version column {sorted(map(str, versions))}", retryable=False, kind="schema_drift"
        )
    return rows


def _codes(field: str | None) -> list[int]:
    out = []
    for part in (field or "").split(","):
        v = int_or_none(part)
        if v is not None:
            out.append(v)
    return out


def _mid(year: int) -> date:
    return date(year, 7, 1)


# ----------------------------------------------------------------------------- atlas: orgvio_cy


def orgvio_atlas_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    raw_sha256: str,
    crosswalk: StateCodeCrosswalk,
    file: str = FILES["orgvio_cy"][1],
) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj, year_period

    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        gw = int_or_none(r["country_id"])
        year = int_or_none(r["year"])
        if gw is None or year is None:
            raise UcdpParseError(f"orgvio_cy row {i + 2}: missing country_id/year", retryable=False)
        res = crosswalk.resolve("ucdp", gw, _mid(year))
        start, end = year_period(year)
        for col, unit in CY_INDICATORS.items():
            v = (r.get(col) or "").strip()
            if v == "":
                continue  # not reported: omitted (never a filled zero)
            if int_or_none(v) is None or str(int_or_none(v)) != v:
                raise UcdpParseError(f"orgvio_cy row {i + 2} {col}: non-integer {v!r}", retryable=False)
            flags: dict[str, Any] = {
                "provider_country": r["country"],
                "ucdp_version": UCDP_VERSION,
                "code_resolution": res.reason,
            }
            if v == "0" and unit == "deaths":
                flags["zero_semantics"] = CY_ZERO_NOTE
            if res.iso3 is None and res.label:
                flags["historical_state"] = res.label
            out.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": "orgvio_cy",
                    "indicator": col,
                    "concept_id": None,
                    "iso3": res.iso3,
                    "provider_geo": f"gw:{gw}",
                    "entity_qid": res.qid,
                    "period_start": start,
                    "period_end": end,
                    "value": v,
                    "value_text": None,
                    "unit": unit,
                    "flags": json_obj(flags),
                    "raw_sha256": raw_sha256,
                    "selector": json_obj(file=file, row=i + 2, column=col),
                }
            )
    return out


# ----------------------------------------------------------------------------- atlas: acd_cy (computed)

ACD_CY_INDICATORS = {
    "acd_conflicts_located": "active state-based conflicts whose location (gwno_loc) includes the country",
    "acd_wars_located": "conflicts at war intensity (>= 1000 battle-related deaths in the year) located in the country",
    "acd_conflicts_primary_party": "active conflicts in which the country's government is a primary party (gwno_a/gwno_b)",
    "acd_conflicts_secondary_party": "active conflicts in which the country's government is a secondary (supporting) "
    "party with troops (gwno_a_2nd/gwno_b_2nd)",
}


def acd_country_year_rows(
    rows: Sequence[Mapping[str, str]], *, raw_sha256: str, crosswalk: StateCodeCrosswalk, file: str = FILES["acd"][1]
) -> list[dict[str, Any]]:
    """Counts of distinct conflicts per (GW code, year) and role, computed from the ACD rows.

    Only country-years with at least one conflict are emitted (the ACD lists conflicts, not countries;
    the absence of a row is not a reported zero).
    """
    from reva.atlas.tables import json_obj, year_period

    sets: dict[tuple[str, int, int], set[str]] = defaultdict(set)
    lines: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        year = int(r["year"])
        cid = r["conflict_id"]
        roles = {
            "acd_conflicts_located": _codes(r["gwno_loc"]),
            "acd_conflicts_primary_party": _codes(r["gwno_a"]) + _codes(r["gwno_b"]),
            "acd_conflicts_secondary_party": _codes(r["gwno_a_2nd"]) + _codes(r["gwno_b_2nd"]),
        }
        if r["intensity_level"] == "2":
            roles["acd_wars_located"] = roles["acd_conflicts_located"]
        for ind, codes in roles.items():
            for gw in set(codes):
                sets[(ind, gw, year)].add(cid)
                lines[(ind, gw, year)].append(i + 2)
    out = []
    for (ind, gw, year), cids in sorted(sets.items()):
        res = crosswalk.resolve("ucdp", gw, _mid(year))
        start, end = year_period(year)
        flags: dict[str, Any] = {
            "computed_from": f"UCDP/PRIO Armed Conflict Dataset {UCDP_VERSION}",
            "method": "count of distinct conflict_id among conflict-year rows; " + ACD_CY_INDICATORS[ind],
            "conflict_ids": sorted(cids, key=int),
            "code_resolution": res.reason,
        }
        if res.iso3 is None and res.label:
            flags["historical_state"] = res.label
        rows_ = lines[(ind, gw, year)]
        out.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": "acd_cy",
                "indicator": ind,
                "concept_id": None,
                "iso3": res.iso3,
                "provider_geo": f"gw:{gw}",
                "entity_qid": res.qid,
                "period_start": start,
                "period_end": end,
                "value": str(len(cids)),
                "value_text": None,
                "unit": "count",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=file, rows=rows_[:50], rows_total=len(rows_), year=year),
            }
        )
    return out


# ----------------------------------------------------------------------------- KG records


def _conflict_label(rows: Sequence[Mapping[str, str]]) -> str:
    r = rows[-1]
    incompat = INCOMPATIBILITY.get(r["incompatibility"], r["incompatibility"])
    what = r["territory_name"] if incompat == "territory" and r["territory_name"] else "Government"
    return f"{r['location']}: {what} (UCDP conflict {r['conflict_id']})"


def kg_records(
    acd: Sequence[Mapping[str, str]],
    brd: Sequence[Mapping[str, str]],
    actors: Sequence[Mapping[str, str]],
    *,
    shas: Mapping[str, str],
    resolver: StateEntityResolver,
) -> tuple[list[KgEntity], list[KgStatement]]:
    """Entities and statements for every ACD conflict (1946-2025) and BRD conflict-year (1989-2025).

    Relations that hold in several years become one statement per run of consecutive years
    (start/end = first/last year, precision 9) with the exact years in ``qualifiers.years``.
    """
    actor_name = {a["ActorId"]: a["NameData"] for a in actors}
    by_conflict: dict[str, list[tuple[int, Mapping[str, str]]]] = defaultdict(list)
    for i, r in enumerate(acd):
        by_conflict[r["conflict_id"]].append((i + 2, r))
    ents: list[KgEntity] = []
    stmts: list[KgStatement] = []
    acd_file, acd_sha = FILES["acd"][1], shas["acd"]
    used_actors: set[str] = set()
    for cid, items in sorted(by_conflict.items(), key=lambda x: int(x[0])):
        items.sort(key=lambda x: int(x[1]["year"]))
        rows = [r for _, r in items]
        subj = f"ucdp:conflict:{cid}"
        years_all = [int(r["year"]) for r in rows]
        ents.append(
            KgEntity(
                qid=subj,
                kind="armed_conflict",
                labels={"en": _conflict_label(rows)},
                descriptions={
                    "en": f"UCDP/PRIO armed conflict {cid}, active years "
                    f"{min(years_all)}-{max(years_all)} (UCDP {UCDP_VERSION})"
                },
            )
        )
        # role relations, grouped by resolved entity
        rel: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)  # (pred, obj) -> [(year, line)]
        for line, r in items:
            y = int(r["year"])
            for pred, field, side in (
                ("ucdp:primary_party", "gwno_a", "a"),
                ("ucdp:primary_party", "gwno_b", "b"),
                ("ucdp:secondary_party", "gwno_a_2nd", "a"),
                ("ucdp:secondary_party", "gwno_b_2nd", "b"),
                ("ucdp:location", "gwno_loc", ""),
            ):
                for gw in _codes(r[field]):
                    obj = resolver.entity("ucdp", gw, _mid(y))
                    rel[(pred + (f"|side={side}" if side else ""), obj)].append((y, line))
            if r["gwno_b"].strip() == "":  # non-state side B: actor ids
                for aid in _codes(r["side_b_id"]):
                    used_actors.add(str(aid))
                    rel[("ucdp:nonstate_side|side=b", f"ucdp:actor:{aid}")].append((y, line))
        for (pred_side, obj), yl in sorted(rel.items()):
            pred, _, side = pred_side.partition("|")
            years = [y for y, _ in yl]
            for a, b in merge_years(years):
                run = [y for y in sorted(set(years)) if a <= y <= b]
                qual: dict[str, Any] = {"years": run}
                if side:
                    qual["side"] = side.split("=")[1]
                stmts.append(
                    KgStatement(
                        key=f"{cid}:{pred.split(':')[1]}{(':' + side) if side else ''}:{obj}:{a}",
                        subject=subj,
                        property=pred,
                        object_qid=obj,
                        start=f"{a}-01-01",
                        start_precision=9,
                        end=f"{b}-12-31",
                        end_precision=9,
                        qualifiers=qual,
                        raw_sha256=acd_sha,
                        selector={"file": acd_file, "rows": [ln for y, ln in yl if a <= y <= b][:50]},
                    )
                )
        last_line, last = items[-1]
        first_line, first = items[0]
        for pred, val in (
            ("ucdp:incompatibility", INCOMPATIBILITY.get(last["incompatibility"])),
            ("ucdp:conflict_type", CONFLICT_TYPE.get(last["type_of_conflict"])),
            ("ucdp:territory_name", last["territory_name"] or None),
        ):
            if val:
                stmts.append(
                    KgStatement(
                        key=f"{cid}:{pred.split(':')[1]}",
                        subject=subj,
                        property=pred,
                        value={"type": "string", "value": val},
                        qualifiers={"as_of_year": int(last["year"])},
                        raw_sha256=acd_sha,
                        selector={"file": acd_file, "row": last_line},
                    )
                )
        types = sorted({CONFLICT_TYPE.get(r["type_of_conflict"], "") for r in rows} - {""})
        if len(types) > 1:  # e.g. intrastate -> internationalized: keep the history
            for t in types:
                yrs = [int(r["year"]) for r in rows if CONFLICT_TYPE.get(r["type_of_conflict"]) == t]
                stmts.append(
                    KgStatement(
                        key=f"{cid}:conflict_type_in:{t}",
                        subject=subj,
                        property="ucdp:conflict_type",
                        value={"type": "string", "value": t},
                        start=f"{min(yrs)}-01-01",
                        start_precision=9,
                        end=f"{max(yrs)}-12-31",
                        end_precision=9,
                        qualifiers={"years": sorted(yrs)},
                        raw_sha256=acd_sha,
                        selector={"file": acd_file, "conflict_id": cid},
                    )
                )
        sd = iso_date_or_none(first["start_date"])
        if sd:
            stmts.append(
                KgStatement(
                    key=f"{cid}:start_date",
                    subject=subj,
                    property="ucdp:start_date",
                    value={"type": "time", "time": sd.isoformat(), "precision": _PREC.get(first["start_prec"], 9)},
                    qualifiers={
                        "ucdp_start_prec": first["start_prec"],
                        "definition": "date of the first battle-related death",
                    },
                    raw_sha256=acd_sha,
                    selector={"file": acd_file, "row": first_line, "column": "start_date"},
                )
            )
        for line, r in items:
            y = int(r["year"])
            stmts.append(
                KgStatement(
                    key=f"{cid}:intensity:{y}",
                    subject=subj,
                    property="ucdp:intensity_level",
                    value={"type": "quantity", "amount": r["intensity_level"], "unit": "1"},
                    point_in_time=f"{y}-01-01",
                    pit_precision=9,
                    qualifiers={"scale": "1 = 25-999 battle-related deaths in the year; 2 = at least 1000"},
                    raw_sha256=acd_sha,
                    selector={"file": acd_file, "row": line, "column": "intensity_level"},
                )
            )
            if r["ep_end"] == "1" and iso_date_or_none(r["ep_end_date"]):
                stmts.append(
                    KgStatement(
                        key=f"{cid}:episode_end:{y}",
                        subject=subj,
                        property="ucdp:episode_end",
                        value={
                            "type": "time",
                            "time": r["ep_end_date"][:10],
                            "precision": _PREC.get(r["ep_end_prec"] or "1", 11),
                        },
                        qualifiers={
                            "definition": "last date of the episode (no 25 battle-related deaths in the "
                            "following year)",
                            "ucdp_ep_end_prec": r["ep_end_prec"],
                        },
                        raw_sha256=acd_sha,
                        selector={"file": acd_file, "row": line, "column": "ep_end_date"},
                    )
                )
    brd_file, brd_sha = FILES["brd_conf"][1], shas["brd_conf"]
    for i, r in enumerate(brd):
        cid, y = r["conflict_id"], int(r["year"])
        q = {k: r[k] for k in ("bd_low", "bd_high") if (r.get(k) or "").strip()}
        stmts.append(
            KgStatement(
                key=f"{cid}:battle_deaths:{y}",
                subject=f"ucdp:conflict:{cid}",
                property="ucdp:battle_related_deaths",
                value={
                    "type": "quantity",
                    "amount": r["bd_best"],
                    "unit": "deaths",
                    **({"lowerBound": q["bd_low"]} if "bd_low" in q else {}),
                    **({"upperBound": q["bd_high"]} if "bd_high" in q else {}),
                },
                point_in_time=f"{y}-01-01",
                pit_precision=9,
                qualifiers={"estimate": "best", "battle_location": r.get("battle_location") or None},
                raw_sha256=brd_sha,
                selector={"file": brd_file, "row": i + 2, "column": "bd_best"},
            )
        )
    for actor in sorted(used_actors, key=int):
        name = actor_name.get(actor)
        ents.append(
            KgEntity(
                qid=f"ucdp:actor:{actor}",
                kind="armed_group",
                labels={"en": (name or "").strip() or f"UCDP actor {actor}"},
                descriptions={"en": f"UCDP actor {actor} (UCDP Actor Dataset {UCDP_VERSION})"},
            )
        )
    return ents, stmts


# ----------------------------------------------------------------------------- atlas source + ingest


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Uppsala Conflict Data Program (Uppsala University)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST, "cran.r-project.org"),
        datasets=("orgvio_cy", "acd_cy"),
        dataset_ids=(*tuple(f"ucdp-{k}-{UCDP_VERSION}" for k in FILES), f"countrycode-{COUNTRYCODE_VERSION}"),
        cadence_seconds=30 * 86400,
        planned_requests=8,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "geo": "Gleditsch-Ward codes (UCDP variant: 345 kept for Serbia after 2006)",
            "time": "calendar year",
            "kg": "ucdp:conflict:<id>, ucdp:actor:<id>",
        },
        notes="UCDP API needs a token since 2025; the official CSV downloads are used. Also fetches the "
        "countrycode R package (CRAN) for GW/COW -> ISO3.",
    )


def build(
    raws: Mapping[str, RawAcquisition],
    *,
    crosswalk: StateCodeCrosswalk,
    qid_for_iso3: Mapping[str, str],
    countrycode_sha256: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Parse raw bytes into the two atlas frames and the KG records (no I/O)."""
    import hashlib

    shas = {k: hashlib.sha256(r.body).hexdigest() for k, r in raws.items()}
    parsed = {k: parse_file(k, r.body, strict=strict) for k, r in raws.items()}
    cy = orgvio_atlas_rows(parsed["orgvio_cy"], raw_sha256=shas["orgvio_cy"], crosswalk=crosswalk)
    acd_cy = acd_country_year_rows(parsed["acd"], raw_sha256=shas["acd"], crosswalk=crosswalk)
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    ents, stmts = kg_records(parsed["acd"], parsed["brd_conf"], parsed["actor"], shas=shas, resolver=resolver)
    ents = ents + resolver.code_entities()
    if countrycode_sha256:
        stmts = stmts + code_correspondence(resolver, raw_sha256=countrycode_sha256)
    return {
        "shas": shas,
        "orgvio_cy": cy,
        "acd_cy": acd_cy,
        "entities": ents,
        "statements": stmts,
        "row_counts": {k: len(v) for k, v in parsed.items()},
        "unresolved_state_codes": {k: sorted(v) for k, v in resolver.unresolved.items()},
    }


def unmapped_geo(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["iso3"] is None:
            e = seen.setdefault(r["provider_geo"], {"provider_geo": r["provider_geo"], "rows": 0})
            e["rows"] += 1
    return sorted(seen.values(), key=lambda e: e["provider_geo"])


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the four UCDP zips + countrycode through the run, archive them, register ``ucdp.orgvio_cy`` and
    ``ucdp.acd_cy``, and load the KG records when the ``kg`` schema exists."""
    crosswalk, cc_sha = await fetch_countrycode(run)
    raws = {}
    for it in fetch_items():
        raws[it.key] = await run.get(
            it.url, key=f"{it.key}:{UCDP_VERSION}", media_types=it.expected_media_types, max_bytes=it.max_bytes
        )
    out = build(
        raws,
        crosswalk=crosswalk,
        qid_for_iso3=load_qid_for_iso3(),
        countrycode_sha256=cc_sha,
        strict=run.origin == "live",
    )
    for raw in raws.values():  # parsed first: nothing archived on schema drift
        run.archive(raw)
    shas = out["shas"]
    res: dict[str, Any] = {"row_counts": out["row_counts"], "atlas": {}}
    if run.wants("orgvio_cy"):
        r = run.register(
            dataset="orgvio_cy",
            frame=atlas_frame(out["orgvio_cy"]),
            raw_sha256s=[shas["orgvio_cy"]],
            rights=RIGHTS,
            coverage_note=f"UCDP Country-Year Dataset on Organized Violence within Country Borders {UCDP_VERSION}, "
            "1989-2025, all GW states; indicators = UCDP columns verbatim; zeros are UCDP-coded",
        )
        res["atlas"]["orgvio_cy"] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    if run.wants("acd_cy"):
        r = run.register(
            dataset="acd_cy",
            frame=atlas_frame(out["acd_cy"]),
            raw_sha256s=[shas["acd"]],
            rights=RIGHTS,
            coverage_note=f"COMPUTED from UCDP/PRIO ACD {UCDP_VERSION} (1946-2025): distinct active conflicts per "
            "country-year by role; only country-years with >= 1 conflict are listed",
        )
        res["atlas"]["acd_cy"] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=KG_SOURCE_KEY,
            source_revision=f"ucdp-{UCDP_VERSION}",
            raw_sha256s=sorted(set(shas.values()) | {cc_sha}),
            retrieved_at=max(r.retrieved_at for r in raws.values()),
            origin=run.origin,
        )
    res["unresolved_state_codes"] = {
        k: v[:3] + (["..."] if len(v) > 3 else []) for k, v in out["unresolved_state_codes"].items()
    }
    return res
