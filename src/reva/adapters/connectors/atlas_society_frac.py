"""Ethnic, linguistic and religious fractionalisation -> atlas tables ``hief.hief`` and ``sscp.frac_v1``
(agent A-SOCIETY, V3 atlas).

Sources (both read from their Harvard Dataverse release, licence + file MD5 checked on every run)
------------------------------------------------------------------------------------------------
* **HIEF** -- Drazanova, Lenka (2019), *Historical Index of Ethnic Fractionalization Dataset (HIEF)*,
  Harvard Dataverse doi:10.7910/DVN/4JQRCL, V2, file ``HIEF_data.tab`` (original ``HIEF_data.csv``):
  ``Country, Year, EFindex`` for 165 country units, 1945-2013, annual. Licence **CC0 1.0** (Dataverse
  record). Built from Cline Center CREG group shares (Drazanova 2020, Journal of Open Humanities Data).
* **SSCP fractionalisation** -- Gordon, E. R. (2026), *Small State Coverage Project (SSCP):
  Fractionalisation* v1.0, Harvard Dataverse doi:10.7910/DVN/EVCQXC, file
  ``sscp_fractionalisation_extension.tab`` (original ``.csv``) + ``README_frac.txt`` (archived: it states
  the construction and the reference period of each value origin). Licence **CC BY 4.0** (Dataverse
  record). It contains Alesina, Devleeschauwer, Easterly, Kurlat & Wacziarg (2003) "Fractionalization"
  values (``*_source = alesina_2003``) and SSCP's extension computed with the same Herfindahl formula from
  CIA World Factbook group shares (``cia``), Britannica Book of the Year 2014 (``eb``), the World Directory
  of Minorities (``wdm``) and RCS-Dem 2.0 (``rcs_dem_2.0``, 2015 cross-section).

Alesina et al.'s own files (Harvard faculty page / NBER) and Fearon (2003) carry no licence statement;
they are not fetched -- the Alesina values come only through the CC BY 4.0 SSCP release, each cell naming
its origin.

What a cell is
--------------
* HIEF: indicator ``EFindex`` (probability that two randomly drawn individuals belong to different ethnic
  groups, 0-1), one cell per (country unit, year). ``provider_geo`` is HIEF's country name. Names map
  through scheme ``soc_hief`` (``registry/atlas/geo_aliases/a_society.yaml``); four HIEF units that
  change state over the period are mapped only for the years in which they denote the crosswalk entity
  (:data:`HIEF_WINDOWS`, e.g. "German Federal Republic" = DEU from 1991 only; before, it is West Germany
  and stays unmapped). USSR, Yugoslavia, Czechoslovakia, the GDR, South Vietnam and South Yemen stay
  unmapped. HIEF has no row for France.
* SSCP: indicators ``ethnic_frac``, ``language_frac``, ``religion_frac`` (0-1, ``1 - sum(share_i^2)``),
  one cross-sectional value per polity. The file gives no year; the period is the reference year stated
  for the value's origin in README_frac.txt (``rcs_dem_2.0`` -> 2015; ``eb`` -> 2014 edition) and, where
  the README names no year, the publication year of the source (``alesina_2003`` -> 2003; ``cia`` and
  ``wdm`` -> 2026, the SSCP v1.0 release that consulted them), recorded in ``flags.period_basis``.
  ``provider_geo`` is SSCP's ``iso_num`` (ISO 3166 numeric; scheme ``soc_sscp``: m49 + Kosovo 689 ->
  XKX, Channel Islands 830 -> CHI; Taiwan 158 -> TWN through the crosswalk). North Vietnam (896) and the two
  unnamed rows 530 / 891
  (ISO numeric of the Netherlands Antilles and of Serbia and Montenegro) stay unmapped.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from reva.adapters.connectors.atlas_society_common import (
    DATAVERSE,
    S3_HOST,
    RowBuffer,
    canon,
    check_dataverse_record,
    concept_hints,
    dataverse_file_url,
    dataverse_record_url,
    err,
    fetch_archive,
    num,
    read_csv,
    year_period,
)
from reva.adapters.connectors.base import module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem

PARSER_VERSION = "atlas_society_frac/1"
CATALOG_FILE = "society.yaml"

# ----------------------------------------------------------------------------- HIEF
HIEF_CONNECTOR = "atlas.hief"
HIEF_VERSION = "1.0.0"
HIEF_SOURCE_KEY = "hief.dataverse"
HIEF_DOI = "doi:10.7910/DVN/4JQRCL"
HIEF_FILE_ID = 3476857
HIEF_FILE = "HIEF_data.csv"
HIEF_SCHEME = "soc_hief"
HIEF_MIN_ROWS = 8500
HIEF_RIGHTS: dict[str, Any] = {
    "licence": "CC0-1.0",
    "licence_url": "http://creativecommons.org/publicdomain/zero/1.0",
    "open_licence": True,
    "attribution": "Drazanova, Lenka (2019). Historical Index of Ethnic Fractionalization Dataset (HIEF). "
    "Harvard Dataverse, doi:10.7910/DVN/4JQRCL, V2.",
    "source_key": HIEF_SOURCE_KEY,
}
#: HIEF units whose referent changes over 1945-2013: mapped only inside the window (inclusive years).
#: Evidence: the HIEF file's own year ranges (Democratic Republic of Vietnam 1945-2013 with Republic of
#: Vietnam 1954-1975; German Federal Republic 1949-2013 with German Democratic Republic 1949-1990; Yemen
#: Arab Republic 1945-2013 with Yemen PDR 1967-1990; Serbia 1991-2013 with no Montenegro unit) and the
#: unification / dissolution dates (2 Jul 1976, 3 Oct 1990, 22 May 1990, 3 Jun 2006): a year in which
#: both predecessor units still have a row, or in which the unit still included Montenegro, is not mapped.
HIEF_WINDOWS: dict[str, tuple[int, str]] = {
    "Democratic Republic of Vietnam": (1976, "VNM"),
    "German Federal Republic": (1991, "DEU"),
    "Yemen Arab Republic": (1991, "YEM"),
    "Serbia": (2007, "SRB"),
}


def hief_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="hief.record", url=dataverse_record_url(HIEF_DOI), max_bytes=2_000_000),
        FetchItem(
            key="hief.data",
            url=dataverse_file_url(HIEF_FILE_ID, original=True),
            max_bytes=5_000_000,
            expected_media_types=("text/csv", "application/octet-stream"),
        ),
    )


def hief_iso3(name: str, year: int) -> tuple[str | None, str]:
    from reva.atlas.tables import country_iso3

    if name in HIEF_WINDOWS:
        start, window_iso3 = HIEF_WINDOWS[name]
        return (window_iso3, "hief_window") if year >= start else (None, "outside_hief_window")
    iso3 = country_iso3(name, HIEF_SCHEME)
    return iso3, ("alias" if iso3 else "unmapped")


def parse_hief(body: bytes, *, raw_sha256: str, min_rows: int = HIEF_MIN_ROWS) -> tuple[Any, dict[str, Any]]:
    rows = read_csv(body, HIEF_FILE, required=("Country", "Year", "EFindex"))
    if len(rows) < min_rows:
        raise err(f"{HIEF_FILE}: only {len(rows)} rows (expected >= {min_rows}): truncated file?")
    hints = concept_hints(CATALOG_FILE, "hief", "hief")
    buf = RowBuffer("hief", "hief")
    seen: set[tuple[str, int]] = set()
    rep: Counter[str] = Counter()
    unmapped: dict[str, list[int]] = {}
    for i, r in enumerate(rows, start=2):
        name, ytxt, vtxt = r["Country"].strip(), r["Year"].strip(), r["EFindex"].strip()
        if not ytxt.isdigit():
            raise err(f"{HIEF_FILE} line {i}: Year {ytxt!r} is not a year")
        year = int(ytxt)
        if (name, year) in seen:
            raise err(f"{HIEF_FILE} line {i}: duplicate country-year {name}/{year}")
        seen.add((name, year))
        if vtxt in ("", "NA"):
            rep["missing_values"] += 1
            continue
        v = num(vtxt)
        if v is None:
            raise err(f"{HIEF_FILE} line {i}: EFindex {vtxt!r} is not a number")
        iso3, how = hief_iso3(name, year)
        rep[how] += 1
        if iso3 is None:
            unmapped.setdefault(name, []).append(year)
        buf.add(
            indicator="EFindex",
            provider_geo=name,
            period=year_period(year),
            value=v,
            unit="index (0-1)",
            flags=canon(iso3_resolution=how),
            raw_sha256=raw_sha256,
            selector=canon(file=HIEF_FILE, row=i, column="EFindex"),
            iso3=iso3,
            concept_id=hints.get("EFindex"),
        )
    rep_out = {
        **dict(rep),
        "rows": len(rows),
        "cells": len(buf),
        "units": len({n for n, _ in seen}),
        "years": [min(y for _, y in seen), max(y for _, y in seen)] if seen else None,
        "unmapped": {k: [min(v), max(v)] for k, v in sorted(unmapped.items())},
    }
    return buf.frame(), rep_out


# ----------------------------------------------------------------------------- SSCP
SSCP_CONNECTOR = "atlas.sscp_frac"
SSCP_VERSION = "1.0.0"
SSCP_SOURCE_KEY = "sscp.dataverse"
SSCP_DOI = "doi:10.7910/DVN/EVCQXC"
SSCP_FILE_ID = 14330927
SSCP_FILE = "sscp_fractionalisation_extension.csv"
SSCP_README_ID = 14330926
SSCP_README = "README_frac.txt"
SSCP_SCHEME = "soc_sscp"
SSCP_DATASET = "frac_v1"
SSCP_MIN_ROWS = 230
SSCP_RIGHTS: dict[str, Any] = {
    "licence": "CC-BY-4.0",
    "licence_url": "http://creativecommons.org/licenses/by/4.0",
    "open_licence": True,
    "attribution": "Gordon, E. R. 2026. Small State Coverage Project (SSCP): Fractionalisation. v1.0. Harvard "
    "Dataverse, doi:10.7910/DVN/EVCQXC. Base dataset: Alesina, A., Devleeschauwer, A., Easterly, W., Kurlat, S., "
    "& Wacziarg, R. (2003). Fractionalization. Journal of Economic Growth 8(2): 155-194. Religion added from "
    "Brown & James (2019), RCS-Dem 2.0, doi:10.17605/OSF.IO/7SR4M.",
    "source_key": SSCP_SOURCE_KEY,
}
SSCP_VARS = {"ethnic_frac": "ethnic_source", "language_frac": "language_source", "religion_frac": "religion_source"}
#: Value origin -> (reference year, period basis, label). README_frac.txt sections 4a/4b/5.
SSCP_ORIGINS: dict[str, tuple[int, str, str]] = {
    "alesina_2003": (
        2003,
        "publication year of Alesina et al. (2003); the underlying group-share data years vary by country and are "
        "not in this file",
        "Alesina et al. (2003) Fractionalization",
    ),
    "rcs_dem_2.0": (2015, "RCS-Dem 2.0 2015 cross-section (README_frac.txt 4b, 5)", "RCS-Dem 2.0 (Brown & James 2019)"),
    "eb": (
        2014,
        "Britannica Book of the Year 2014 edition (README_frac.txt 4a; Britannica's census/estimate years vary)",
        "Encyclopaedia Britannica, Book of the Year 2014",
    ),
    "cia": (
        2026,
        "SSCP v1.0 release year (README_frac.txt says 'current CIA World Factbook'; edition not stated)",
        "CIA World Factbook (as consulted by SSCP)",
    ),
    "wdm": (
        2026,
        "SSCP v1.0 release year (World Directory of Minorities and Indigenous Peoples; edition not stated)",
        "Minority Rights Group, World Directory of Minorities and Indigenous Peoples",
    ),
}


def sscp_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="sscp.record", url=dataverse_record_url(SSCP_DOI), max_bytes=2_000_000),
        FetchItem(
            key="sscp.data",
            url=dataverse_file_url(SSCP_FILE_ID, original=True),
            max_bytes=2_000_000,
            expected_media_types=("text/csv", "application/octet-stream"),
        ),
        FetchItem(
            key="sscp.readme",
            url=dataverse_file_url(SSCP_README_ID),
            max_bytes=1_000_000,
            expected_media_types=("text/plain", "application/octet-stream"),
        ),
    )


def check_sscp_readme(body: bytes) -> None:
    """The README statements the period rules rely on must still be there (else: review, nothing registered)."""
    t = body.decode("utf-8", "replace")
    for needle in (
        "2015 cross-section",
        "Britannica Book of the Year 2014",
        "current CIA World Factbook",
        "1 - sum(share_i^2)",
    ):
        if needle not in t:
            raise err(f"{SSCP_README}: statement {needle!r} not found; review the period rules", kind="schema_drift")


def parse_sscp(body: bytes, *, raw_sha256: str, min_rows: int = SSCP_MIN_ROWS) -> tuple[Any, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    cols = ("iso_num", "country", "scope_status", "gw_num", "cow_num", *SSCP_VARS, *SSCP_VARS.values())
    rows = read_csv(body, SSCP_FILE, required=cols)
    if len(rows) < min_rows:
        raise err(f"{SSCP_FILE}: only {len(rows)} rows (expected >= {min_rows}): truncated file?")
    hints = concept_hints(CATALOG_FILE, "sscp", SSCP_DATASET)
    buf = RowBuffer("sscp", SSCP_DATASET)
    seen: set[str] = set()
    rep: Counter[str] = Counter()
    unmapped: dict[str, str] = {}
    for i, r in enumerate(rows, start=2):
        geo = r["iso_num"].strip()
        if not geo.isdigit():
            raise err(f"{SSCP_FILE} line {i}: iso_num {geo!r} is not a number")
        if geo in seen:
            raise err(f"{SSCP_FILE} line {i}: duplicate iso_num {geo}")
        seen.add(geo)
        iso3 = country_iso3(geo, SSCP_SCHEME)
        if iso3 is None:
            unmapped[geo] = r["country"]
        for var, src_col in SSCP_VARS.items():
            vtxt, origin = r[var].strip(), r[src_col].strip()
            if vtxt in ("", "NA"):
                if origin not in ("", "NA"):
                    raise err(f"{SSCP_FILE} line {i}: {var} is NA but {src_col} = {origin!r}")
                rep[f"{var}_missing"] += 1
                continue
            v = num(vtxt)
            if v is None:
                raise err(f"{SSCP_FILE} line {i}: {var}={vtxt!r} is not a number")
            if origin not in SSCP_ORIGINS:
                raise err(f"{SSCP_FILE} line {i}: {src_col}={origin!r} is not a documented origin")
            year, basis, label = SSCP_ORIGINS[origin]
            rep[f"{var}:{origin}"] += 1
            flags = {
                "value_origin": origin,
                "value_origin_label": label,
                "period_basis": basis,
                "scope_status": r["scope_status"].strip(),
                "sscp_polity": r["country"].strip(),
                "cross_section": True,
            }
            for k in ("gw_num", "cow_num"):
                if r[k].strip() not in ("", "NA"):
                    flags[k] = r[k].strip()
            buf.add(
                indicator=var,
                provider_geo=geo,
                period=year_period(year),
                value=v,
                unit="index (0-1)",
                flags=canon(flags),
                raw_sha256=raw_sha256,
                selector=canon(file=SSCP_FILE, row=i, column=var),
                iso3=iso3,
                concept_id=hints.get(var),
            )
    return buf.frame(), {**dict(rep), "rows": len(rows), "cells": len(buf), "unmapped": unmapped}


# ----------------------------------------------------------------------------- specs + ingest
def _spec(which: str) -> AtlasSourceSpec:
    hief = which == "hief"
    return AtlasSourceSpec(
        name=HIEF_CONNECTOR if hief else SSCP_CONNECTOR,
        version=HIEF_VERSION if hief else SSCP_VERSION,
        source_key=HIEF_SOURCE_KEY if hief else SSCP_SOURCE_KEY,
        budget_key=HIEF_SOURCE_KEY if hief else SSCP_SOURCE_KEY,
        atlas_source="hief" if hief else "sscp",
        provider="Lenka Drazanova (Harvard Dataverse)"
        if hief
        else "Small State Coverage Project, E. R. Gordon (Harvard Dataverse)",
        licence="CC0-1.0" if hief else "CC-BY-4.0",
        allowed_hosts=(DATAVERSE, S3_HOST),
        datasets=("hief",) if hief else (SSCP_DATASET,),
        dataset_ids=(HIEF_DOI,) if hief else (SSCP_DOI,),
        cadence_seconds=90 * 86400,
        planned_requests=len(hief_items() if hief else sscp_items()),
        max_response_bytes=5_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(parse_hief),
        field_mappings={"geo_scheme": HIEF_SCHEME if hief else SSCP_SCHEME},
        notes="Dataverse record (licence + MD5) checked on every run; nothing registered on mismatch.",
    )


def atlas_source() -> list[AtlasSourceSpec]:
    return [_spec("hief"), _spec("sscp")]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    live = run.origin == "live"
    if run.spec.name == HIEF_CONNECTOR:
        if not run.wants("hief"):
            return {"skipped": True}
        got = await fetch_archive(run, hief_items())
        meta = check_dataverse_record(
            got.bodies["hief.record"], doi=HIEF_DOI, files={HIEF_FILE_ID: (HIEF_FILE, got.bodies["hief.data"])},
            licences=("CC0 1.0",),
        )  # fmt: skip
        frame, rep = parse_hief(
            got.bodies["hief.data"], raw_sha256=got.shas["hief.data"], min_rows=HIEF_MIN_ROWS if live else 1
        )
        note = (
            "HIEF (Drazanova 2019, Dataverse V2), EFindex 1945-2013 annual, 165 units keyed by HIEF name; units "
            "that change state are mapped only inside documented windows (DEU/YEM from 1991, VNM from 1976, SRB "
            "from 2007). CC0."
        )
        reg = run.register(
            dataset="hief", frame=frame, raw_sha256s=[got.shas["hief.data"]], coverage_note=note, geo_scheme=None,
            rights={**HIEF_RIGHTS, "record": meta, "record_raw_sha256": got.shas["hief.record"]},
        )  # fmt: skip
        return {"record": meta, "parse": rep, "atlas": reg}
    if not run.wants(SSCP_DATASET):
        return {"skipped": True}
    got = await fetch_archive(run, sscp_items())
    meta = check_dataverse_record(
        got.bodies["sscp.record"],
        doi=SSCP_DOI,
        files={
            SSCP_FILE_ID: (SSCP_FILE, got.bodies["sscp.data"]),
            SSCP_README_ID: (SSCP_README, got.bodies["sscp.readme"]),
        },
        licences=("CC BY 4.0",),
    )
    check_sscp_readme(got.bodies["sscp.readme"])
    frame, rep = parse_sscp(
        got.bodies["sscp.data"], raw_sha256=got.shas["sscp.data"], min_rows=SSCP_MIN_ROWS if live else 1
    )
    note = (
        "SSCP Fractionalisation v1.0 (Dataverse, CC BY 4.0): ethnic, language and religious fractionalisation "
        "(Herfindahl, 0-1), one cross-section per polity; Alesina et al. (2003) values plus SSCP's extension, origin "
        "per cell in flags.value_origin; period = stated reference year of the origin (flags.period_basis)."
    )
    reg = run.register(
        dataset=SSCP_DATASET,
        frame=frame,
        raw_sha256s=[got.shas["sscp.data"]],
        coverage_note=note,
        geo_scheme=SSCP_SCHEME,
        rights={**SSCP_RIGHTS, "record": meta, "record_raw_sha256": got.shas["sscp.record"], "readme_raw_sha256": got.shas["sscp.readme"]},
    )  # fmt: skip
    return {"record": meta, "parse": rep, "atlas": reg}


__all__ = [
    "HIEF_WINDOWS",
    "SSCP_ORIGINS",
    "atlas_ingest",
    "atlas_source",
    "check_sscp_readme",
    "hief_iso3",
    "hief_items",
    "parse_hief",
    "parse_sscp",
    "sscp_items",
]
