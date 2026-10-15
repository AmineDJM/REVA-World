"""Corruption perception, gender-discriminatory institutions and election results -> atlas tables
``ti.cpi``, ``oecd.sigi2023`` and ``parlgov.elections`` (agent A-SOCIETY, V3 atlas).

Sources
-------
* **Transparency International, Corruption Perceptions Index** (``atlas.ti_cpi``): the CPI 2025 results
  workbook ``CPI2025_Results.xlsx`` (strict OOXML), sheet "CPI Historical" (2012-2025, one row per country
  and year) cross-checked against sheet "CPI2025". **Licence: CC BY-ND 4.0** -- the CPI 2025 page footer
  reads "Except where otherwise noted, this work is licensed under CC BY-ND 4.0" (the page is fetched and
  archived with every run; the statement must still be there). NoDerivatives allows redistribution of the
  material unchanged; every value is kept exactly as published (no rescaling, rounding or recomputation),
  and the long-format layout is a format change, which CC BY-ND 4.0 s.2(a)(4) says never produces adapted
  material. The rights record says so (``modification: none``). The rescaled scores of the 13 third-party
  sources (EIU, PRS ICRG, S&P Global Insight, ...) that TI also lists are **not** ingested: they are
  proprietary data of those producers.
* **OECD Social Institutions and Gender Index (SIGI) 2023** (``atlas.oecd_sigi``): SDMX dataflows
  ``OECD.DEV.NPG:DSD_SIGI@DF_SIGI_2023(1.0)`` (SIGI score and four dimension sub-indices) and
  ``DSD_GID@DF_GID_2023(1.0)`` (Gender, Institutions and Development Database), of which only the 15
  **legal-framework** variables (``VARIABLE_CATEGORY = LAW``: OECD's coding of discrimination in laws on
  child marriage, household responsibilities, divorce, inheritance, violence against women, FGM,
  reproductive autonomy, land / non-land / financial assets, workplace rights, citizenship, political
  voice, freedom of movement, access to justice; 0-100) are ingested. OECD terms (the same source record
  ``oecd.sdmx`` as A-ILO: open access, CC BY 4.0 by default since 2024-07-01). The GID-DB "practices" and
  "attitudes" variables are survey data of third parties (DHS, MICS, Gallup World Poll, WVS, ILO, IPU) and
  are not ingested (third-party material is excluded from OECD's licence; several are covered at their
  source by A-UN / A-WB / A-POL).
* **ParlGov 2024 release** (``atlas.parlgov``): Döring & Manow, *Parliaments and governments database*,
  Harvard Dataverse doi:10.7910/DVN/2VZ5ZC, ``view_election`` (original ``view_election.csv``): party
  vote shares and seats at every national parliament and European Parliament election in 37 EU/OECD
  democracies since 1900. Licence **CC0 1.0** (Dataverse record; licence + MD5 checked every run).

Not ingested (terms, checked 2026-10-15): Freedom House *Freedom in the World* (content-permissions page:
"Use of Freedom House content for noncommercial purposes is permitted"; commercial use "must be formally
approved by Freedom House prior to publication"; data files only on request); RSF World Press Freedom
Index (https://rsf.org/cgu: total or partial reproduction "strictement interdits" without prior
permission, and "Toute extraction et toute réutilisation ... de la totalité ou d'une partie du contenu"
of its databases prohibited); International IDEA Voter Turnout Database (idea.int/terms-and-conditions:
non-commercial use of the text only, "you are not permitted to benefit commercially from their use";
A-POL reached the same conclusion; turnout is covered by IPU Parline and V-Dem); Constituency-Level
Elections Archive (registration and non-redistribution terms).

What a cell is
--------------
* CPI: indicators ``cpi_score`` (0-100), ``rank``, ``standard_error``, ``number_of_sources``,
  ``lower_ci``, ``upper_ci`` per (ISO3, year), values as stored in the workbook (some are binary-float
  text such as ``1.8822840000000001``: kept, never rounded). ``provider_geo`` = TI's ISO3 column (scheme
  ``soc_ti``: ISO3 + KSV -> XKX). TI says CPI scores are comparable over time from
  2012 (the methodology change of CPI 2012).
* SIGI: indicator = the SDMX ``MEASURE`` code (``SIGI``, ``SIGI_RPI``, ``DF_CM_LAW``...), unit "0-100
  scale" (0 = no discrimination), period = ``TIME_PERIOD`` (2023, the SIGI edition). Empty
  ``OBS_VALUE`` cells are omitted. ``provider_geo`` = ``REF_AREA`` (scheme ``soc_oecd``: ISO3 + XKV ->
  XKX); OECD aggregates (W, A, E, F, S, OECD, EU, G7, G20) are kept unmapped.
* ParlGov: per election date (one-day period) ``vote_share|election=<parliament|ep>|party=<party_id>``
  (% of votes), ``seats|election=...|party=...`` (seats won) and ``seats_total|election=...`` (seats in
  the chamber); party names, ParlGov left-right position (0-10) and election ids in flags. Blank vote
  shares / seats are omitted (counted).
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from reva.adapters.connectors.atlas_society_common import (
    DATAVERSE,
    S3_HOST,
    RowBuffer,
    XlsxStream,
    canon,
    check_dataverse_record,
    concept_hints,
    dataverse_file_url,
    dataverse_record_url,
    day_period,
    err,
    fetch_archive,
    header_map,
    num,
    read_csv,
    year_period,
)
from reva.adapters.connectors.base import module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem

PARSER_VERSION = "atlas_society_gov/1"
CATALOG_FILE = "society.yaml"

# ----------------------------------------------------------------------------- TI CPI
CPI_CONNECTOR = "atlas.ti_cpi"
CPI_VERSION = "1.0.0"
CPI_SOURCE_KEY = "ti.cpi"
CPI_PAGE = "https://www.transparency.org/en/cpi/2025"
CPI_XLSX = "https://images.transparencycdn.org/images/CPI2025_Results.xlsx"
CPI_FILE = "CPI2025_Results.xlsx"
CPI_HIST_SHEET = "CPI Historical"
CPI_LATEST_SHEET = "CPI2025"
CPI_LATEST_YEAR = 2025
CPI_DATASET = "cpi"
CPI_SCHEME = "soc_ti"
CPI_LICENCE_TEXT = "Except where otherwise noted, this work is licensed under CC BY-ND 4.0"
CPI_MIN_ROWS = 2400
CPI_RIGHTS: dict[str, Any] = {
    "licence": "CC-BY-ND-4.0",
    "licence_url": "https://creativecommons.org/licenses/by-nd/4.0/",
    "licence_evidence": f"{CPI_PAGE} footer: '{CPI_LICENCE_TEXT}'",
    "open_licence": True,
    "no_derivatives": True,
    "modification": "none: values verbatim from the workbook; long-format layout only (CC BY-ND 4.0 s.2(a)(4))",
    "third_party_source_scores": "not ingested (proprietary data of the underlying sources)",
    "attribution": "Corruption Perceptions Index 2025 by Transparency International, licensed under CC BY-ND 4.0",
    "source_key": CPI_SOURCE_KEY,
}
#: CPI Historical columns -> (indicator, unit).
CPI_COLS: dict[str, tuple[str, str]] = {
    "CPI score": ("cpi_score", "score (0-100)"),
    "Rank": ("rank", "rank"),
    "Standard error": ("standard_error", "score points"),
    "Number of sources": ("number_of_sources", "count"),
    "Lower CI": ("lower_ci", "score (0-100)"),
    "Upper CI": ("upper_ci", "score (0-100)"),
}
CPI_KEYS = ("Country / Territory", "ISO3", "Year", "Region")


def cpi_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="cpi.page", url=CPI_PAGE, max_bytes=5_000_000, expected_media_types=("text/html",)),
        FetchItem(
            key="cpi.xlsx",
            url=CPI_XLSX,
            max_bytes=30_000_000,
            expected_media_types=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/octet-stream",
            ),
        ),
    )


def check_cpi_page(body: bytes) -> None:
    if CPI_LICENCE_TEXT not in body.decode("utf-8", "replace"):
        raise err(f"{CPI_PAGE}: licence statement {CPI_LICENCE_TEXT!r} not found: review the rights", kind="licence")


def _header_row(rows: Any, first: str, what: str) -> tuple[dict[str, int], Any]:
    for _rnum, cells in rows:
        hdr = header_map(cells)
        if first in hdr:
            return hdr, rows
    raise err(f"{what}: no header row starting with {first!r}")


def parse_cpi(body: bytes, *, raw_sha256: str, min_rows: int = CPI_MIN_ROWS) -> tuple[Any, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    wb = XlsxStream(body)
    for s in (CPI_HIST_SHEET, CPI_LATEST_SHEET):
        if s not in wb.sheets:
            raise err(f"{CPI_FILE}: sheet {s!r} missing ({sorted(wb.sheets)})")
    hdr, rows = _header_row(wb.rows(CPI_HIST_SHEET), "Country / Territory", CPI_HIST_SHEET)
    missing = [c for c in (*CPI_KEYS, *CPI_COLS) if c not in hdr]
    if missing:
        raise err(f"{CPI_FILE} {CPI_HIST_SHEET}: header lacks {missing}")
    hints = concept_hints(CATALOG_FILE, "ti", CPI_DATASET)
    buf = RowBuffer("ti", CPI_DATASET)
    seen: dict[tuple[str, int], str | None] = {}
    rep: Counter[str] = Counter()
    unmapped: dict[str, str] = {}
    n = 0
    for rnum, cells in rows:

        def t(col: str, cells: dict[int, Any] = cells) -> str | None:
            c = cells.get(hdr[col])
            return c.text.strip() if c is not None and c.text.strip() != "" else None

        geo, ytxt = t("ISO3"), t("Year")
        if geo is None and ytxt is None:
            continue
        if geo is None or ytxt is None or num(ytxt) is None:
            raise err(f"{CPI_HIST_SHEET} row {rnum}: ISO3/Year missing ({geo!r}, {ytxt!r})")
        n += 1
        year = int(float(ytxt))
        if (geo, year) in seen:
            raise err(f"{CPI_HIST_SHEET} row {rnum}: duplicate country-year {geo}/{year}")
        seen[(geo, year)] = num(t("CPI score") or "")
        iso3 = country_iso3(geo, CPI_SCHEME)
        if iso3 is None:
            unmapped[geo] = t("Country / Territory") or ""
        flags = canon(ti_country=t("Country / Territory"), ti_region=t("Region"), comparable_since=2012)
        for col, (ind, unit) in CPI_COLS.items():
            txt = t(col)
            if txt is None:
                rep[f"{ind}_blank"] += 1
                continue
            v = num(txt)
            if v is None:
                raise err(f"{CPI_HIST_SHEET} row {rnum}: {col}={txt!r} is not a number")
            buf.add(
                indicator=ind, provider_geo=geo, period=year_period(year), value=v, unit=unit, flags=flags,
                raw_sha256=raw_sha256, selector=canon(file=CPI_FILE, sheet=CPI_HIST_SHEET, row=rnum, column=col),
                iso3=iso3, concept_id=hints.get(ind),
            )  # fmt: skip
    if n < min_rows:
        raise err(f"{CPI_FILE}: only {n} country-years (expected >= {min_rows}): truncated file?")
    # cross-check the latest year against the headline sheet (same workbook)
    lhdr, lrows = _header_row(wb.rows(CPI_LATEST_SHEET), "Country / Territory", CPI_LATEST_SHEET)
    score_col = f"CPI {CPI_LATEST_YEAR} score"
    if "ISO3" not in lhdr or score_col not in lhdr:
        raise err(f"{CPI_LATEST_SHEET}: header lacks ISO3 / {score_col!r}")
    checked, mism = 0, []
    for _r, cells in lrows:
        g = cells.get(lhdr["ISO3"])
        s = cells.get(lhdr[score_col])
        if g is None or s is None:
            continue
        checked += 1
        hist = seen.get((g.text.strip(), CPI_LATEST_YEAR))
        if hist != num(s.text):
            mism.append((g.text.strip(), s.text, hist))
    if mism:
        raise err(f"{CPI_FILE}: {CPI_LATEST_SHEET} and {CPI_HIST_SHEET} disagree for {mism[:5]}")
    years = sorted({y for _, y in seen})
    return buf.frame(), {**dict(rep), "country_years": n, "cells": len(buf), "years": [years[0], years[-1]] if years else None,
                         "latest_sheet_rows_checked": checked, "unmapped": unmapped}  # fmt: skip


# ----------------------------------------------------------------------------- OECD SIGI
SIGI_CONNECTOR = "atlas.oecd_sigi"
SIGI_VERSION = "1.0.0"
SIGI_DATASET = "sigi2023"
SIGI_SCHEME = "soc_oecd"
SDMX = "https://sdmx.oecd.org/public/rest/data"
SIGI_FLOWS = {
    "sigi": "OECD.DEV.NPG,DSD_SIGI@DF_SIGI_2023,1.0",
    "gid": "OECD.DEV.NPG,DSD_GID@DF_GID_2023,1.0",
}
SIGI_MIN_ROWS = {"sigi": 900, "gid": 9000}
SIGI_RIGHTS: dict[str, Any] = {
    "licence": "OECD Terms and Conditions (open access; CC BY 4.0 is the OECD default since 2024-07-01; SIGI 2023 "
    "was released before that date under the earlier OECD terms)",
    "licence_url": "https://www.oecd.org/en/about/oecd-open-by-default-policy.html",
    "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
    "open_licence": True,
    "third_party_material": "GID-DB practices/attitudes (third-party surveys) not ingested",
    "attribution": "OECD (2023), Social Institutions and Gender Index (SIGI) 2023 and Gender, Institutions and "
    "Development Database (GID-DB) 2023, OECD Data Explorer.",
    "source_key": "oecd.sdmx",
}


def sigi_url(flow: str) -> str:
    return f"{SDMX}/{SIGI_FLOWS[flow]}/all?format=csvfilewithlabels"


def sigi_items() -> tuple[FetchItem, ...]:
    return tuple(
        FetchItem(
            key=f"sigi.{f}",
            url=sigi_url(f),
            max_bytes=20_000_000,
            expected_media_types=("application/vnd.sdmx.data+csv", "text/csv"),
        )
        for f in SIGI_FLOWS
    )


def parse_sigi(
    bodies: dict[str, bytes], shas: dict[str, str], *, min_rows: dict[str, int] | None = None
) -> tuple[Any, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    mins = SIGI_MIN_ROWS if min_rows is None else min_rows
    hints = concept_hints(CATALOG_FILE, "oecd", SIGI_DATASET)
    buf = RowBuffer("oecd", SIGI_DATASET)
    rep: Counter[str] = Counter()
    unmapped: dict[str, str] = {}
    seen: set[tuple[str, str, str]] = set()
    for flow in SIGI_FLOWS:
        name = f"{flow}.csv"
        req = ["STRUCTURE_ID", "REF_AREA", "Reference area", "MEASURE", "Measure", "UNIT_MEASURE", "Unit of measure",
               "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"]  # fmt: skip
        if flow == "gid":
            req += ["VARIABLE_CATEGORY", "AGE", "SEX", "SIGI_FRAMEWORK", "SIGI Framework", "CALC_METHOD"]
        rows = read_csv(bodies[f"sigi.{flow}"], name, required=req)
        if len(rows) < mins.get(flow, 0):
            raise err(f"SIGI {flow}: only {len(rows)} rows (expected >= {mins[flow]}): truncated response?")
        for i, r in enumerate(rows, start=2):
            want = SIGI_FLOWS[flow].replace(",", ":", 1).replace(",", "(", 1) + ")"
            if r["STRUCTURE_ID"] != want:
                raise err(f"SIGI {flow} line {i}: STRUCTURE_ID {r['STRUCTURE_ID']!r} != {want!r}")
            if flow == "gid":
                if r["VARIABLE_CATEGORY"] != "LAW":
                    rep["gid_non_law_skipped"] += 1
                    continue
                if r["AGE"] != "_Z" or r["SEX"] != "_Z":
                    raise err(f"SIGI gid line {i}: legal variable with AGE/SEX dimension ({r['AGE']}, {r['SEX']})")
            geo, measure, tp = r["REF_AREA"].strip(), r["MEASURE"].strip(), r["TIME_PERIOD"].strip()
            if not tp.isdigit():
                raise err(f"SIGI {flow} line {i}: TIME_PERIOD {tp!r} is not a year")
            if (geo, measure, tp) in seen:
                raise err(f"SIGI {flow} line {i}: duplicate cell {geo}/{measure}/{tp}")
            seen.add((geo, measure, tp))
            txt = r["OBS_VALUE"].strip()
            if txt == "":
                rep[f"{flow}_empty_obs"] += 1
                continue
            v = num(txt)
            if v is None:
                raise err(f"SIGI {flow} line {i}: OBS_VALUE {txt!r} is not a number")
            iso3 = country_iso3(geo, SIGI_SCHEME)
            if iso3 is None:
                unmapped[geo] = r["Reference area"]
            flags: dict[str, Any] = {
                "measure_label": r["Measure"],
                "obs_status": r["OBS_STATUS"],
                "dataflow": r["STRUCTURE_ID"],
                "edition": "SIGI 2023",
                "scale": "0 = no discrimination, 100 = absolute discrimination",
            }
            if flow == "gid":
                flags["sigi_framework"] = r["SIGI Framework"]
                flags["used_in_sigi"] = r["CALC_METHOD"] == "SIGI_YES"
            buf.add(
                indicator=measure, provider_geo=geo, period=year_period(int(tp)), value=v, unit=r["Unit of measure"],
                flags=canon(flags), raw_sha256=shas[f"sigi.{flow}"],
                selector=canon(file=name, row=i, column="OBS_VALUE", key=f"{geo}.{measure}.{tp}"),
                iso3=iso3, concept_id=hints.get(measure),
            )  # fmt: skip
            rep[f"{flow}_cells"] += 1
    return buf.frame(), {**dict(rep), "cells": len(buf), "unmapped": unmapped}


# ----------------------------------------------------------------------------- ParlGov
PG_CONNECTOR = "atlas.parlgov"
PG_VERSION = "1.0.0"
PG_SOURCE_KEY = "parlgov.dataverse"
PG_DOI = "doi:10.7910/DVN/2VZ5ZC"
PG_FILE_ID = 10437092
PG_FILE = "view_election.csv"
PG_DATASET = "elections"
PG_MIN_ROWS = 8500
PG_RIGHTS: dict[str, Any] = {
    "licence": "CC0-1.0",
    "licence_url": "http://creativecommons.org/publicdomain/zero/1.0",
    "open_licence": True,
    "attribution": "Döring, Holger and Philip Manow. 2024. Parliaments and governments database (ParlGov): "
    "Information on parties, elections and cabinets in established democracies. Stable version 2024 (12 August "
    "2024), Harvard Dataverse doi:10.7910/DVN/2VZ5ZC.",
    "source_key": PG_SOURCE_KEY,
}
PG_COLS = ("country_name_short", "country_name", "election_type", "election_date", "vote_share", "seats",
           "seats_total", "party_name_short", "party_name", "party_name_english", "left_right", "country_id",
           "election_id", "party_id")  # fmt: skip


def parlgov_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="parlgov.record", url=dataverse_record_url(PG_DOI), max_bytes=2_000_000),
        FetchItem(
            key="parlgov.election",
            url=dataverse_file_url(PG_FILE_ID, original=True),
            max_bytes=10_000_000,
            expected_media_types=("text/csv", "application/octet-stream"),
        ),
    )


def parse_parlgov(body: bytes, *, raw_sha256: str, min_rows: int = PG_MIN_ROWS) -> tuple[Any, dict[str, Any]]:
    rows = read_csv(body, PG_FILE, required=PG_COLS)
    if len(rows) < min_rows:
        raise err(f"{PG_FILE}: only {len(rows)} rows (expected >= {min_rows}): truncated file?")
    hints = concept_hints(CATALOG_FILE, "parlgov", PG_DATASET)
    buf = RowBuffer("parlgov", PG_DATASET)
    rep: Counter[str] = Counter()
    seen: set[tuple[str, str, str, str]] = set()
    totals: dict[tuple[str, str, str], tuple[str, int, str]] = {}
    for i, r in enumerate(rows, start=2):
        geo, etype, edate, pid = (
            r[k].strip() for k in ("country_name_short", "election_type", "election_date", "party_id")
        )
        if etype not in ("parliament", "ep"):
            raise err(f"{PG_FILE} line {i}: election_type {etype!r} is not parliament/ep")
        try:
            d = date.fromisoformat(edate)
        except ValueError as e:
            raise err(f"{PG_FILE} line {i}: election_date {edate!r} is not an ISO date") from e
        if not pid.isdigit():
            raise err(f"{PG_FILE} line {i}: party_id {pid!r} is not an id")
        if (geo, etype, edate, pid) in seen:
            raise err(f"{PG_FILE} line {i}: duplicate party result {geo}/{etype}/{edate}/{pid}")
        seen.add((geo, etype, edate, pid))
        flags = {
            "party_name_short": r["party_name_short"],
            "party_name": r["party_name"],
            "party_name_english": r["party_name_english"],
            "parlgov_party_id": pid,
            "parlgov_election_id": r["election_id"],
            "parlgov_country_id": r["country_id"],
        }
        lr = num(r["left_right"])
        if lr is not None:
            flags["left_right_parlgov_0_10"] = lr
        f = canon(flags)
        for col, ind, unit in (("vote_share", "vote_share", "% of votes"), ("seats", "seats", "seats")):
            txt = r[col].strip()
            if txt == "":
                rep[f"{col}_blank"] += 1
                continue
            v = num(txt)
            if v is None:
                raise err(f"{PG_FILE} line {i}: {col}={txt!r} is not a number")
            key = f"{ind}|election={etype}|party={pid}"
            buf.add(
                indicator=key, provider_geo=geo, period=day_period(d), value=v, unit=unit, flags=f,
                raw_sha256=raw_sha256, selector=canon(file=PG_FILE, row=i, column=col), iso3=None,
                concept_id=hints.get(key),
            )  # fmt: skip
        st = r["seats_total"].strip()
        if st != "":
            v = num(st)
            if v is None:
                raise err(f"{PG_FILE} line {i}: seats_total={st!r} is not a number")
            k = (geo, etype, edate)
            if k in totals and totals[k][0] != v:
                raise err(f"{PG_FILE} line {i}: seats_total differs within election {k}")
            totals.setdefault(k, (v, i, r["election_id"]))
    for (geo, etype, edate), (v, line, eid) in sorted(totals.items()):
        key = f"seats_total|election={etype}"
        buf.add(
            indicator=key, provider_geo=geo, period=day_period(date.fromisoformat(edate)), value=v, unit="seats",
            flags=canon(parlgov_election_id=eid), raw_sha256=raw_sha256,
            selector=canon(file=PG_FILE, row=line, column="seats_total"), iso3=None, concept_id=hints.get(key),
        )  # fmt: skip
    return buf.frame(), {
        **dict(rep),
        "rows": len(rows),
        "cells": len(buf),
        "elections": len(totals),
        "countries": len({g for g, _, _, _ in seen}),
        "first_election": min(e for _, _, e, _ in seen) if seen else None,
        "last_election": max(e for _, _, e, _ in seen) if seen else None,
    }


# ----------------------------------------------------------------------------- specs + ingest
def atlas_source() -> list[AtlasSourceSpec]:
    digest = module_digest(parse_cpi)
    return [
        AtlasSourceSpec(
            name=CPI_CONNECTOR,
            version=CPI_VERSION,
            source_key=CPI_SOURCE_KEY,
            budget_key=CPI_SOURCE_KEY,
            atlas_source="ti",
            provider="Transparency International",
            licence="CC-BY-ND-4.0",
            allowed_hosts=("www.transparency.org", "images.transparencycdn.org", "files.transparencycdn.org"),
            datasets=(CPI_DATASET,),
            dataset_ids=("CPI2025_Results.xlsx",),
            cadence_seconds=30 * 86400,
            planned_requests=len(cpi_items()),
            max_response_bytes=30_000_000,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={"geo_scheme": CPI_SCHEME, "columns": {k: v[0] for k, v in CPI_COLS.items()}},
            notes="CPI page (licence statement) + results workbook; values verbatim (CC BY-ND 4.0).",
        ),
        AtlasSourceSpec(
            name=SIGI_CONNECTOR,
            version=SIGI_VERSION,
            source_key="oecd.sdmx",
            budget_key="oecd.sdmx",
            atlas_source="oecd",
            provider="OECD Development Centre (SIGI / GID-DB 2023)",
            licence=SIGI_RIGHTS["licence"],
            allowed_hosts=("sdmx.oecd.org",),
            datasets=(SIGI_DATASET,),
            dataset_ids=tuple(SIGI_FLOWS.values()),
            cadence_seconds=90 * 86400,
            planned_requests=len(sigi_items()),
            max_response_bytes=20_000_000,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={"geo_scheme": SIGI_SCHEME, "gid_filter": "VARIABLE_CATEGORY = LAW"},
            notes="Two SDMX-CSV requests (SIGI + GID-DB); only OECD's own legal-framework coding is kept from GID-DB.",
        ),
        AtlasSourceSpec(
            name=PG_CONNECTOR,
            version=PG_VERSION,
            source_key=PG_SOURCE_KEY,
            budget_key=PG_SOURCE_KEY,
            atlas_source="parlgov",
            provider="ParlGov (Döring & Manow), Harvard Dataverse",
            licence="CC0-1.0",
            allowed_hosts=(DATAVERSE, S3_HOST),
            datasets=(PG_DATASET,),
            dataset_ids=(PG_DOI,),
            cadence_seconds=90 * 86400,
            planned_requests=len(parlgov_items()),
            max_response_bytes=10_000_000,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings={"geo_scheme": "iso3", "indicator": "<vote_share|seats>|election=<type>|party=<party_id>"},
            notes="Dataverse record (licence + MD5) checked on every run; nothing registered on mismatch.",
        ),
    ]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    live = run.origin == "live"
    name = run.spec.name
    if name == CPI_CONNECTOR:
        if not run.wants(CPI_DATASET):
            return {"skipped": True}
        got = await fetch_archive(run, cpi_items())
        check_cpi_page(got.bodies["cpi.page"])
        frame, rep = parse_cpi(
            got.bodies["cpi.xlsx"], raw_sha256=got.shas["cpi.xlsx"], min_rows=CPI_MIN_ROWS if live else 1
        )
        note = (
            "Transparency International CPI 2012-2025 (CPI2025_Results.xlsx, sheet 'CPI Historical', cross-checked "
            "with sheet 'CPI2025'); score, rank, standard error, number of sources, lower/upper CI bounds as published. "
            "CC BY-ND 4.0: values unchanged; third-party source scores not ingested."
        )
        reg = run.register(
            dataset=CPI_DATASET, frame=frame, raw_sha256s=[got.shas["cpi.xlsx"]], coverage_note=note,
            geo_scheme=CPI_SCHEME, rights={**CPI_RIGHTS, "licence_page_raw_sha256": got.shas["cpi.page"]},
        )  # fmt: skip
        return {"parse": rep, "atlas": reg}
    if name == SIGI_CONNECTOR:
        if not run.wants(SIGI_DATASET):
            return {"skipped": True}
        got = await fetch_archive(run, sigi_items())
        frame, rep = parse_sigi(got.bodies, got.shas, min_rows=None if live else {})
        note = (
            "OECD SIGI 2023: SIGI score + 4 sub-indices (DF_SIGI_2023) and the 15 legal-framework variables of the "
            "GID-DB 2023 (DF_GID_2023, VARIABLE_CATEGORY=LAW), 0-100; empty observations omitted; aggregates kept "
            "unmapped. Survey-based GID-DB variables (third-party data) not ingested."
        )
        reg = run.register(
            dataset=SIGI_DATASET, frame=frame, raw_sha256s=sorted(got.shas.values()), coverage_note=note,
            geo_scheme=SIGI_SCHEME, rights=dict(SIGI_RIGHTS),
        )  # fmt: skip
        return {"parse": rep, "atlas": reg}
    if not run.wants(PG_DATASET):
        return {"skipped": True}
    got = await fetch_archive(run, parlgov_items())
    meta = check_dataverse_record(
        got.bodies["parlgov.record"], doi=PG_DOI, files={PG_FILE_ID: (PG_FILE, got.bodies["parlgov.election"])},
        licences=("CC0 1.0",),
    )  # fmt: skip
    frame, rep = parse_parlgov(
        got.bodies["parlgov.election"], raw_sha256=got.shas["parlgov.election"], min_rows=PG_MIN_ROWS if live else 1
    )
    note = (
        "ParlGov 2024 stable release, view_election: party vote shares (%) and seats at national parliament and "
        "European Parliament elections, 37 EU/OECD countries since 1900, one-day periods (election date). CC0."
    )
    reg = run.register(
        dataset=PG_DATASET, frame=frame, raw_sha256s=[got.shas["parlgov.election"]], coverage_note=note,
        geo_scheme="iso3", rights={**PG_RIGHTS, "record": meta, "record_raw_sha256": got.shas["parlgov.record"]},
    )  # fmt: skip
    return {"record": meta, "parse": rep, "atlas": reg}


__all__ = [
    "CPI_COLS",
    "SIGI_FLOWS",
    "atlas_ingest",
    "atlas_source",
    "check_cpi_page",
    "cpi_items",
    "parlgov_items",
    "parse_cpi",
    "parse_parlgov",
    "parse_sigi",
    "sigi_items",
]
