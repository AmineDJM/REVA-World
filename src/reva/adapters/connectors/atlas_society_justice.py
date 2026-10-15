"""Crime & justice -> atlas tables ``cdpd.cdpd_v1`` (death penalty status) and ``who.gho_violence``
(WHO homicide estimates) (agent A-SOCIETY, V3 atlas).

Sources
-------
* **CDPD** -- Anckar, Carsten and Thomas Denk (2024), *Comparative Death Penalty Database* version 1
  (June 2024), Harvard Dataverse doi:10.7910/DVN/LI3WYK, file ``CDPD version 1 June 2024.xlsx``. Licence
  **CC0 1.0** (Dataverse record; licence + MD5 checked every run). 19,204 country-years, 1800-2022, 207
  units (current independent states plus their predecessors, e.g. "GERMANY WEST", "USSR", "KOREA").
  ``Deathpenalty`` = status on 31 December (codebook): 0 abolished, 1 abolished for ordinary crimes only,
  2 abolished for ordinary crimes only but used during the last 10 years, 3 abolished in practice,
  4 retained. The cell keeps the code as ``value`` and the codebook label as ``value_text``; the provider's
  derived ``Flag_change``, ``Direction_change`` and ``Break_time`` and the COW codes are kept in flags.
  Units are keyed by CDPD's own (upper-case) names, mapped through scheme ``soc_cdpd``
  (``registry/atlas/geo_aliases/a_society.yaml``); predecessor units (GERMANY EAST/WEST, USSR, YUGOSLAVIA,
  CZECHOSLOVAKIA, KOREA 1800-1909, VIETNAM NORTH/SOUTH, the two Yemens) stay unmapped.
* **WHO homicide estimates** -- WHO Global Health Observatory indicators ``VIOLENCE_HOMICIDERATE``
  ("Estimates of rates of homicides per 100 000 population") and ``VIOLENCE_HOMICIDENUM`` ("Estimates of
  number of homicides"), WHO Global Health Estimates, 2000-2021, by sex, with uncertainty intervals.
  **CC BY 4.0** + WHO terms. Same API, parser and cell rules as A-HEALTHEDU's ``who.gho`` table
  (:mod:`reva.adapters.connectors.atlas_who`); a separate table so the two agents' curated lists stay
  independent. Budget key ``who.gho`` (shared).

Not ingested (licence, checked 2026-10-15):

* UNODC dataUNODC homicide / prisons: the portal links to the UN copyright page
  (https://www.un.org/en/about-us/copyright): "None of the materials provided on this web site may be
  used, reproduced or transmitted, in whole or in part, in any form or by any means ... without
  permission in writing from the publisher". The UNODC homicide series are available openly through the
  World Bank WDI copy (``wb.wdi_econ.VC.IHR.PSRC.P5``, CC BY 4.0, agent A-WB) and the UN SDG API copy
  (``un_sdg.sdg_headline`` ``VC_IHR_PSRC``, agent A-UN).
* World Prison Brief (ICPR, Birkbeck): no licence or reuse statement beyond a citation request; data are
  web pages per country (no bulk file).
* Amnesty International death-sentence and execution figures: Amnesty content is CC BY-NC-ND 4.0
  (non-commercial). CDPD (CC0) is used for status instead.
"""

from __future__ import annotations

import json
from collections import Counter
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
    err,
    fetch_archive,
    header_map,
    num,
    year_period,
)
from reva.adapters.connectors.base import module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem

PARSER_VERSION = "atlas_society_justice/1"
CATALOG_FILE = "society.yaml"

# ----------------------------------------------------------------------------- CDPD
CDPD_CONNECTOR = "atlas.cdpd"
CDPD_VERSION = "1.0.0"
CDPD_SOURCE_KEY = "cdpd.dataverse"
CDPD_DOI = "doi:10.7910/DVN/LI3WYK"
CDPD_FILE_ID = 10251764
CDPD_FILE = "CDPD version 1 June 2024.xlsx"
CDPD_DATASET = "cdpd_v1"
CDPD_SCHEME = "soc_cdpd"
CDPD_MIN_ROWS = 19_000
CDPD_RIGHTS: dict[str, Any] = {
    "licence": "CC0-1.0",
    "licence_url": "http://creativecommons.org/publicdomain/zero/1.0",
    "open_licence": True,
    "attribution": "Anckar, Carsten and Denk, Thomas. 2024. Introducing Comparative Death Penalty Database. "
    "European Political Science, doi:10.1057/s41304-024-00491-8. Data: Harvard Dataverse doi:10.7910/DVN/LI3WYK.",
    "source_key": CDPD_SOURCE_KEY,
}
#: CDPD codebook (version 1, 2024), variable ``Deathpenalty`` ("death penalty status on December 31").
CDPD_STATUS = {
    "0": "Abolished",
    "1": "Abolished for ordinary crimes only",
    "2": "Abolished for ordinary crimes only, but used during the last 10 years",
    "3": "Abolished in practice",
    "4": "Retained",
}
CDPD_COLS = ("Country", "Year", "COWCODE", "COWCODE2", "Deathpenalty", "Flag_change", "Direction_change",
             "Break_time", "CDPD_version")  # fmt: skip


def cdpd_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="cdpd.record", url=dataverse_record_url(CDPD_DOI), max_bytes=2_000_000),
        FetchItem(
            key="cdpd.data",
            url=dataverse_file_url(CDPD_FILE_ID),
            max_bytes=10_000_000,
            expected_media_types=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/octet-stream",
            ),
        ),
    )


def parse_cdpd(body: bytes, *, raw_sha256: str, min_rows: int = CDPD_MIN_ROWS) -> tuple[Any, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    wb = XlsxStream(body)
    if len(wb.sheets) != 1:
        raise err(f"{CDPD_FILE}: expected one sheet, got {sorted(wb.sheets)}")
    sheet = next(iter(wb.sheets))
    rows = wb.rows(sheet)
    try:
        _h, hcells = next(rows)
    except StopIteration as e:
        raise err(f"{CDPD_FILE}: empty sheet") from e
    hdr = header_map(hcells)
    missing = [c for c in CDPD_COLS if c not in hdr]
    if missing:
        raise err(f"{CDPD_FILE}: header lacks {missing}")
    hints = concept_hints(CATALOG_FILE, "cdpd", CDPD_DATASET)
    buf = RowBuffer("cdpd", CDPD_DATASET)
    seen: set[tuple[str, int]] = set()
    rep: Counter[str] = Counter()
    unmapped: dict[str, list[int]] = {}
    n = 0
    for rnum, cells in rows:

        def t(col: str, cells: dict[int, Any] = cells) -> str | None:
            c = cells.get(hdr[col])
            return c.text.strip() if c is not None and c.text.strip() != "" else None

        name, ytxt = t("Country"), t("Year")
        if name is None and ytxt is None:
            continue
        n += 1
        if name is None or ytxt is None or num(ytxt) is None:
            raise err(f"{CDPD_FILE} row {rnum}: Country/Year missing or not a year ({name!r}, {ytxt!r})")
        year = int(float(ytxt))
        if (name, year) in seen:
            raise err(f"{CDPD_FILE} row {rnum}: duplicate country-year {name}/{year}")
        seen.add((name, year))
        status = t("Deathpenalty")
        if status is None:
            rep["status_blank"] += 1
            continue
        code = num(status)
        if code not in CDPD_STATUS:
            raise err(f"{CDPD_FILE} row {rnum}: Deathpenalty={status!r} is not a codebook value")
        iso3 = country_iso3(name, CDPD_SCHEME)
        if iso3 is None:
            unmapped.setdefault(name, []).append(year)
        flags = {
            "reference_date": "31 December",
            "cowcode": t("COWCODE"),
            "cowcode_vdem": t("COWCODE2"),
            "flag_change": t("Flag_change"),
            "direction_change": t("Direction_change"),
            "break_time": t("Break_time"),
            "cdpd_version": t("CDPD_version"),
        }
        buf.add(
            indicator="Deathpenalty",
            provider_geo=name,
            period=year_period(year),
            value=code,
            value_text=CDPD_STATUS[code],
            unit="code",
            flags=canon({k: v for k, v in flags.items() if v is not None}),
            raw_sha256=raw_sha256,
            selector=canon(file=CDPD_FILE, sheet=sheet, row=rnum, column="Deathpenalty"),
            iso3=iso3,
            concept_id=hints.get("Deathpenalty"),
        )
        rep[f"status_{code}"] += 1
    if n < min_rows:
        raise err(f"{CDPD_FILE}: only {n} rows (expected >= {min_rows}): truncated file?")
    return buf.frame(), {
        **dict(rep),
        "rows": n,
        "cells": len(buf),
        "units": len({u for u, _ in seen}),
        "years": [min(y for _, y in seen), max(y for _, y in seen)] if seen else None,
        "unmapped": {k: [min(v), max(v)] for k, v in sorted(unmapped.items())},
    }


# ----------------------------------------------------------------------------- WHO homicide
WHOV_CONNECTOR = "atlas.who_violence"
WHOV_VERSION = "1.0.0"
WHOV_DATASET = "gho_violence"
WHOV_FIXTURE_COUNTRIES = ("BRA", "FRA", "JPN", "NGA", "ZAF")


def who_indicators() -> tuple[Any, ...]:
    from reva.adapters.connectors.atlas_who import COMPARABLE, WhoIndicator

    ghe = "WHO Global Health Estimates (GHE)"
    return (
        WhoIndicator("VIOLENCE_HOMICIDERATE", "per 100 000 population", COMPARABLE, ghe),
        WhoIndicator("VIOLENCE_HOMICIDENUM", "homicides (number)", COMPARABLE, ghe),
    )


def who_items(countries: tuple[str, ...] | None = None) -> tuple[FetchItem, ...]:
    from reva.adapters.connectors.atlas_who import INDICATOR_LIST_URL, MAX_ITEM_BYTES, data_url

    out = [FetchItem(key="gho.indicator_list", url=INDICATOR_LIST_URL, max_bytes=5_000_000)]
    for ind in who_indicators():
        out.append(FetchItem(key=f"gho.data:{ind.code}", url=data_url(ind.code, countries), max_bytes=MAX_ITEM_BYTES))
    return tuple(out)


def who_frame(bodies: dict[str, bytes], shas: dict[str, str]) -> tuple[Any, dict[str, Any]]:
    import polars as pl

    from reva.adapters.connectors.atlas_who import atlas_rows, parse_data, parse_indicator_list
    from reva.atlas.tables import ATLAS_SCHEMA

    names = parse_indicator_list(bodies["gho.indicator_list"])
    hints = concept_hints(CATALOG_FILE, "who", WHOV_DATASET)
    parts, counts = [], {}
    for ind in who_indicators():
        name = names.get(ind.code)
        if name is None or name.lower().startswith("archived"):
            raise err(f"{ind.code}: not (or no longer) a current GHO indicator ({name!r})")
        key = f"gho.data:{ind.code}"
        rows = parse_data(bodies[key], ind.code)
        if not rows:
            raise err(f"{ind.code}: the API returned no country rows")
        recs = atlas_rows(rows, ind, raw_sha256=shas[key], item_key=key, indicator_name=name)
        for r in recs:
            r["dataset"] = WHOV_DATASET
            r["concept_id"] = hints.get(r["indicator"])
            f = json.loads(r["flags"])
            f["indicator_name"] = name
            r["flags"] = canon(f)
        counts[ind.code] = len(recs)
        parts.append(pl.DataFrame(recs, schema=ATLAS_SCHEMA))
    return pl.concat(parts, how="vertical"), {
        "rows": counts,
        "indicator_names": {i.code: names[i.code] for i in who_indicators()},
    }


# ----------------------------------------------------------------------------- specs + ingest
def _spec(which: str) -> AtlasSourceSpec:
    if which == "cdpd":
        return AtlasSourceSpec(
            name=CDPD_CONNECTOR,
            version=CDPD_VERSION,
            source_key=CDPD_SOURCE_KEY,
            budget_key=CDPD_SOURCE_KEY,
            atlas_source="cdpd",
            provider="Comparative Death Penalty Database, Anckar & Denk (Harvard Dataverse)",
            licence="CC0-1.0",
            allowed_hosts=(DATAVERSE, S3_HOST),
            datasets=(CDPD_DATASET,),
            dataset_ids=(CDPD_DOI,),
            cadence_seconds=90 * 86400,
            planned_requests=len(cdpd_items()),
            max_response_bytes=10_000_000,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(parse_cdpd),
            field_mappings={"geo_scheme": CDPD_SCHEME, "value": "Deathpenalty", "labels": CDPD_STATUS},
            notes="Dataverse record (licence + MD5) checked on every run; nothing registered on mismatch.",
        )
    from reva.adapters.connectors.atlas_who import HOST, MAX_ITEM_BYTES, RIGHTS

    return AtlasSourceSpec(
        name=WHOV_CONNECTOR,
        version=WHOV_VERSION,
        source_key="who.gho",
        budget_key="who.gho",
        atlas_source="who",
        provider="World Health Organization",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(WHOV_DATASET,),
        dataset_ids=tuple(i.code for i in who_indicators()),
        cadence_seconds=30 * 86400,
        planned_requests=len(who_items()),
        max_response_bytes=MAX_ITEM_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION + "+atlas-who-gho-parse-1",
        code_digest=module_digest(parse_cdpd),
        field_mappings={"value": "NumericValue", "units": {i.code: i.unit for i in who_indicators()}},
        notes="WHO GHO homicide estimates (GHE), country rows only; parsed with atlas_who's GHO rules.",
    )


def atlas_source() -> list[AtlasSourceSpec]:
    return [_spec("cdpd"), _spec("who")]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    live = run.origin == "live"
    if run.spec.name == CDPD_CONNECTOR:
        if not run.wants(CDPD_DATASET):
            return {"skipped": True}
        got = await fetch_archive(run, cdpd_items())
        meta = check_dataverse_record(
            got.bodies["cdpd.record"], doi=CDPD_DOI, files={CDPD_FILE_ID: (CDPD_FILE, got.bodies["cdpd.data"])},
            licences=("CC0 1.0",),
        )  # fmt: skip
        frame, rep = parse_cdpd(
            got.bodies["cdpd.data"], raw_sha256=got.shas["cdpd.data"], min_rows=CDPD_MIN_ROWS if live else 1
        )
        note = (
            "CDPD v1 (June 2024), death penalty status on 31 December, 1800-2022, 0-4 codebook codes with labels; "
            "units keyed by CDPD name (predecessor states unmapped). CC0."
        )
        reg = run.register(
            dataset=CDPD_DATASET, frame=frame, raw_sha256s=[got.shas["cdpd.data"]], coverage_note=note,
            geo_scheme=CDPD_SCHEME, rights={**CDPD_RIGHTS, "record": meta, "record_raw_sha256": got.shas["cdpd.record"]},
        )  # fmt: skip
        return {"record": meta, "parse": rep, "atlas": reg}
    from reva.adapters.connectors.atlas_who import RIGHTS

    if not run.wants(WHOV_DATASET):
        return {"skipped": True}
    got = await fetch_archive(run, who_items(None if live else WHOV_FIXTURE_COUNTRIES))
    frame, rep = who_frame(got.bodies, got.shas)
    note = (
        "WHO GHO homicide estimates (Global Health Estimates), VIOLENCE_HOMICIDERATE and VIOLENCE_HOMICIDENUM, "
        "country rows, by sex, with WHO uncertainty intervals in flags; parsed with A-HEALTHEDU's GHO rules."
        + ("" if live else f" Fixture subset {','.join(WHOV_FIXTURE_COUNTRIES)}.")
    )
    data_shas = sorted({got.shas[k] for k in got.shas if k.startswith("gho.data:")})
    reg = run.register(
        dataset=WHOV_DATASET, frame=frame, raw_sha256s=data_shas, coverage_note=note, geo_scheme="iso3",
        rights={**RIGHTS, "indicator_list_raw_sha256": got.shas["gho.indicator_list"]},
    )  # fmt: skip
    return {"parse": rep, "atlas": reg}


__all__ = [
    "CDPD_STATUS",
    "atlas_ingest",
    "atlas_source",
    "cdpd_items",
    "parse_cdpd",
    "who_frame",
    "who_indicators",
    "who_items",
]
