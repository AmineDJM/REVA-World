"""Religious composition by country -> atlas tables ``rcs.dem2`` and ``wrp.national`` (agent A-SOCIETY).

Sources
-------
* **RCS-Dem 2.0** -- Brown, Davis and Patrick James, *Religious Characteristics of States Dataset
  Project - Demographics v. 2.0 (RCS-Dem 2.0), COUNTRIES ONLY* (2019), OSF project ``7sr4m``
  (https://doi.org/10.17605/OSF.IO/7SR4M), file ``... COUNTRIES ONLY.XLSX`` (40 MB, one sheet
  ``Data``, 42,747 unit-years 1700s-2015, 289 units incl. dependent territories and historical states).
  **Terms**: the OSF project declares no licence (``node_license`` null, checked every run). The
  project's own "Usage Agreement" file (the ARDA usage agreement, archived with every run) asks users to
  acknowledge ARDA and the principal investigators and to make "responsible use"; it contains no
  non-commercial or no-redistribution clause. Not an open licence: the rights record says so
  (``open_licence: false``, ``licence_status: usage_agreement_attribution``).
* **World Religion Project (WRP), national data v1.1** -- Maoz, Zeev and Errol A. Henderson (2013),
  hosted by the Correlates of War project (``WRP_national.csv``, 1945-2010 every five years, 200 COW
  state codes). **Terms**: the COW site's terms (COW FAQ: data must be cited; users cannot charge for
  access), the same terms A-HISTECON records for ``cow.data`` -- not an open licence, flagged.

Pew Research Center's religious composition tables are **not** ingested: Pew's terms of use (section 1,
https://www.pewresearch.org/about/terms-and-conditions/, read 2026-10-15) say "Under no circumstances
may the Content be reproduced in principal part, mirrored, catalogued, framed, or otherwise republished
in its entirety or in principal part without the express written permission" of the Center. The World
Religion Database (Brill) is proprietary.

What a cell is
--------------
RCS-Dem: one cell per (unit, year, variable) as published, for a curated set of 38 percentage variables
(religion families and main branches, ``*PC`` = "Percentage of <group>" of the total population, 0-100;
unit ``%``), the RCS population estimates ``TOTPP`` / ``RELPP`` (persons) and the codes of the majority
(``MAJID``) / supermajority (``SUPID``) religion with the codebook label in ``value_text``. Zeros are the
provider's values and are kept; blank cells are omitted. The 64 finer denominations and the population
counts per group stay in the archived raw file (selectable by adding codes to :data:`RCS_PC`).
``provider_geo`` = the RCS country code (``CCODE``); ``iso3`` = the RCS ``ISO3`` field when it is a
crosswalk key (RCS's own continuity: e.g. unit 365 ``RUS`` covers the Russian Empire, the USSR and Russia,
unit 345 ``YUG`` carries ISO3 ``SRB``; each row keeps ``flags.rcs_abbrev``). Units without an ISO3 or
with a code that is not a crosswalk key (``CSK``, ``DDR``, ``ANT``, the German and Italian states...) are
kept unmapped and listed in the manifest.

WRP: one cell per (COW state, year, variable) for every adherent count (persons) and every share column
(``*pct``, a **proportion 0-1** as published; unit ``proportion (0-1)``). ``totpct`` (a check sum) is not
an indicator. Per-row provider metadata go to flags: ``datatype`` (with the codebook label; ``3`` in the
code = interpolated, ``4`` = adjusted), ``recreliab``, ``reliabilevel``, ``sourcecode``, ``dualrelig``
(``1`` = dual religion: shares may sum above 1). COW codes map through the same date-aware resolver as
A-HISTECON's COW tables (KGGEOPOL's countrycode panel + policy windows), evaluated on 31 December of
the year; the countrycode tarball is fetched and archived with each run.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from typing import Any

from reva.adapters.connectors.atlas_society_common import (
    RowBuffer,
    XlsxStream,
    canon,
    concept_hints,
    crosswalk_key,
    err,
    fetch_archive,
    header_map,
    num,
    read_csv,
    sha256_hex,
    year_period,
)
from reva.adapters.connectors.base import module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem

PARSER_VERSION = "atlas_society_religion/1"
CATALOG_FILE = "society.yaml"

# ----------------------------------------------------------------------------- RCS-Dem 2.0 (OSF)
RCS_CONNECTOR = "atlas.rcs_dem"
RCS_VERSION = "1.0.0"
RCS_SOURCE_KEY = "rcs_dem.osf"
RCS_ATLAS_SOURCE = "rcs"
RCS_DATASET = "dem2"
OSF_NODE = "7sr4m"
RCS_TITLE = "Religious Characteristics of States Dataset Project - Demographics v. 2.0 (RCS-Dem 2.0), COUNTRIES ONLY"
RCS_XLSX_NAME = RCS_TITLE + ".XLSX"
RCS_USAGE_NAME = RCS_TITLE + " Usage Agreement.txt"
RCS_XLSX_GUID = "asg25"
RCS_USAGE_GUID = "6c3xq"
RCS_SHEET = "Data"
RCS_MIN_ROWS = 40_000
RCS_HOSTS = ("api.osf.io", "osf.io", "files.osf.io", "storage.googleapis.com")
RCS_RIGHTS: dict[str, Any] = {
    "licence": "No licence declared (OSF node_license null); ARDA usage agreement: attribution, responsible use",
    "licence_status": "usage_agreement_attribution",
    "open_licence": False,
    "redistribution": "not restricted by the usage agreement (no non-commercial or no-redistribution clause); "
    "not an open licence -- flagged for maintainer review",
    "terms_url": f"https://osf.io/download/{RCS_USAGE_GUID}/",
    "attribution": "The data were downloaded from the Association of Religion Data Archives (www.theARDA.com) "
    "and the Open Science Framework (doi:10.17605/OSF.IO/7SR4M), and were collected by Davis Brown and "
    "Patrick James: Religious Characteristics of States Dataset Project - Demographics v. 2.0 (RCS-Dem 2.0).",
    "source_key": RCS_SOURCE_KEY,
}

#: RCS-Dem 2.0 religion codes (codebook ``cb_descr``: "<code>-Percentage of <label>"), used for the
#: ``*PC`` labels and to name the MAJID / SUPID codes.
RCS_CODES: dict[str, tuple[str, str]] = {
    "1000": ("CHR", "Christians"),
    "1001": ("UCHR", "unspecified Christians"),
    "1100": ("WCHR", "Western Christians"),
    "1150": ("ECHR", "Eastern Christians"),
    "1200": ("CAT", "Catholics"),
    "1220": ("RCC", "Roman Catholics"),
    "1222": ("RCL", "Latin-Rite Roman Catholics"),
    "1223": ("RCN", "Non-Latin-Rite Roman Catholics"),
    "1270": ("OCAT", "Other Catholics"),
    "1300": ("XPRT", "Extended Protestants"),
    "1320": ("PTAN", "Protestants and Anglicans"),
    "1330": ("PTPN", "Protestants and Pentecostals"),
    "1400": ("PRT", "Protestants"),
    "1401": ("UPRT", "unspecified Protestants"),
    "1410": ("LUT", "Lutherans"),
    "1420": ("REF", "Reformed Protestants"),
    "1421": ("UREF", "unspecified Reformed Protestants"),
    "1422": ("PRS", "Presbyterial Reformed Protestants"),
    "1423": ("CNG", "Congregational Reformed Protestants"),
    "1430": ("ABP", "Anabaptists, Brethren, and Pietists"),
    "1440": ("BAP", "Baptists"),
    "1450": ("ADV", "Adventists"),
    "1460": ("WES", "Wesleyans"),
    "1470": ("MPRT", "Multidenominational Protestants"),
    "1480": ("OPRT", "Other Protestants"),
    "1500": ("QPRT", "Quasi-Protestants"),
    "1510": ("ANG", "Anglicans"),
    "1520": ("PEN", "Pentecostals"),
    "1600": ("ORT", "Orthodox"),
    "1601": ("UORT", "unspecified Orthodox"),
    "1610": ("OORT", "Oriental Orthodox"),
    "1620": ("EORT", "Eastern Orthodox"),
    "1700": ("OCHR", "Other Non-Liminal Christians"),
    "1800": ("LCHR", "Liminal Christians"),
    "1801": ("ULCH", "unspecified Liminal Christians"),
    "1810": ("ECC", "Extra-Canonical Christians"),
    "1812": ("LDS", "Latter-Day Saints (Mormons)"),
    "1813": ("OECC", "Other Extra-Canonical Christians"),
    "1820": ("NTC", "Non-Trinitarian Christians"),
    "1822": ("JWT", "Jehovah's Witnesses"),
    "1823": ("UNI", "Unitarians"),
    "1827": ("ONTC", "Other Non-Trinitarian Christians"),
    "1870": ("OLCH", "Other Liminal Christians"),
    "1900": ("CSYN", "Christian Syncretics"),
    "2100": ("JEW", "Jews"),
    "2500": ("MAN", "Mandaeans"),
    "3000": ("MUS", "Muslims"),
    "3001": ("UMUS", "unspecified Muslims"),
    "3100": ("SUN", "Sunni Muslims"),
    "3101": ("USUN", "unspecified Sunni Muslims"),
    "3110": ("HNF", "Hanafi Sunnis"),
    "3120": ("MLK", "Maliki Sunnis"),
    "3130": ("SHF", "Shafii Sunnis"),
    "3140": ("HNB", "Hanbali Sunnis"),
    "3170": ("OSUN", "Other Sunnis"),
    "3200": ("SHI", "Shiites"),
    "3201": ("USHI", "unspecified Shiites"),
    "3210": ("TWL", "Twelvers"),
    "3211": ("UTWL", "unspecified Twelvers"),
    "3212": ("JAF", "Jafari Twelvers"),
    "3213": ("BAT", "Batini Twelvers"),
    "3220": ("ISM", "Ismailis (Seveners)"),
    "3230": ("ZAY", "Zaydis"),
    "3270": ("OSHI", "Other Shiites"),
    "3300": ("KHA", "Khariji Muslims"),
    "3310": ("IBA", "Ibadi Muslims"),
    "3700": ("OMUS", "Other Non-Liminal Muslims"),
    "3800": ("LMUS", "Liminal Muslims"),
    "3810": ("DRZ", "Druzes"),
    "3820": ("AHM", "Ahmadi Muslims"),
    "3870": ("OLMU", "Other Liminal Muslims"),
    "3900": ("MSYN", "Muslim Syncretics"),
    "4100": ("ZOR", "Zoroastrians"),
    "4300": ("BAH", "Bahais"),
    "4500": ("JAI", "Jains"),
    "4800": ("SIK", "Sikhs"),
    "5000": ("HIN", "Hindus"),
    "6000": ("BUD", "Buddhists"),
    "6001": ("UBUD", "unspecified Buddhists"),
    "6100": ("THE", "Theravada Buddhists"),
    "6200": ("MAH", "Mahayana Buddhists"),
    "6300": ("TIB", "Tibetan Buddhists"),
    "6700": ("OBUD", "Other Buddhists"),
    "6900": ("BSYN", "Buddhists Syncretics"),
    "7000": ("EAC", "East Asian Religious Complex"),
    "7100": ("SHN", "Shintoists"),
    "7300": ("CNF", "Confucianists"),
    "7500": ("TAO", "Taoists"),
    "7700": ("CHF", "Chinese Folk Religionists"),
    "7900": ("BUDC", "Buddhist Complex"),
    "8000": ("IND", "Indigenous Religionists (Ethnoreligionists)"),
    "8001": ("UIND", "unspecified Indigenous Religionists (Ethnoreligionists)"),
    "8100": ("ANM", "Animists"),
    "8200": ("SHM", "Shamanists"),
    "8700": ("OIND", "Other Indigenous Religionists (Ethnoreligionists)"),
    "8900": ("NEW", "New Age Religionists"),
    "9000": ("NRE", "Not Religious"),
    "9001": ("UNRE", "unspecified Not Religious"),
    "9100": ("ATH", "Atheist"),
    "9800": ("ORE", "Other Religionists"),
    "9900": ("UNK", "Unknown"),
}
_PREFIX_LABEL = {p: (code, label) for code, (p, label) in RCS_CODES.items()}

#: Curated percentage variables (families and main branches). Codebook prefix + "PC".
RCS_PC: tuple[str, ...] = (
    "CHR", "CAT", "XPRT", "PRT", "ANG", "PEN", "ORT", "OORT", "EORT", "LCHR", "CSYN",
    "JEW", "MUS", "SUN", "SHI", "IBA", "LMUS", "AHM", "DRZ", "MSYN",
    "ZOR", "BAH", "JAI", "SIK", "HIN", "BUD", "THE", "MAH", "TIB",
    "EAC", "SHN", "CNF", "TAO", "CHF", "IND", "NEW", "NRE", "ATH", "ORE", "UNK",
)  # fmt: skip
RCS_EXTRA: dict[str, str] = {"RELPC": "%", "TOTPP": "persons", "RELPP": "persons"}
RCS_EXTRA_LABELS = {
    "RELPC": "Total percentage of known religionists (vice non-religionists)",
    "TOTPP": "RCS estimate of total population",
    "RELPP": "Total population of known religionists (vice non-religionists)",
}
RCS_CODE_VARS = {
    "MAJID": "ID code of religion of majority of population (50% plus one)",
    "SUPID": "ID code of religion of supermajority of population (66 2/3% or greater)",
}
#: RCS units whose ISO3 field is empty or not an ISO code but which denote a crosswalk entity. Evidence from
#: the RCS file's own fields: 347 ``KOS`` (COW state number 347 = Kosovo, no ISO3 given); 984 ``NMR`` has
#: ISO3 "NMP" (not an ISO code) but NUMISO/UNCODE 580 = Northern Mariana Islands (MNP); 2020 ``CNL`` has
#: no ISO3 but NUMISO/UNCODE 830 = Channel Islands (crosswalk CHI).
RCS_ALIASES: dict[str, str] = {"347": "XKX", "984": "MNP", "2020": "CHI"}
RCS_REQUIRED = ("CCODE", "ABBREV", "YEAR", "ISO3", "INDEP", *RCS_CODE_VARS, *RCS_EXTRA)


def osf_node_url() -> str:
    return f"https://api.osf.io/v2/nodes/{OSF_NODE}/"


def osf_files_url() -> str:
    return f"https://api.osf.io/v2/nodes/{OSF_NODE}/files/osfstorage/?page%5Bsize%5D=100"


def rcs_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="rcs.node", url=osf_node_url(), max_bytes=2_000_000),
        FetchItem(key="rcs.files", url=osf_files_url(), max_bytes=2_000_000),
        FetchItem(
            key="rcs.usage",
            url=f"https://osf.io/download/{RCS_USAGE_GUID}/",
            max_bytes=200_000,
            expected_media_types=("text/plain", "application/octet-stream"),
        ),
        FetchItem(
            key="rcs.xlsx",
            url=f"https://osf.io/download/{RCS_XLSX_GUID}/",
            max_bytes=80_000_000,
            expected_media_types=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/octet-stream",
            ),
        ),
    )


def check_osf(node: bytes, files: bytes, payloads: dict[str, bytes]) -> dict[str, Any]:
    """OSF node (title, no licence) + file listing (name, sha256 of each payload)."""
    try:
        n = json.loads(node)["data"]
        attrs = n["attributes"]
        listing = json.loads(files)["data"]
    except (ValueError, KeyError, TypeError) as e:
        raise err(f"OSF node/file listing is not the expected JSON ({e})") from e
    if attrs.get("title") != RCS_TITLE:
        raise err(f"OSF node title changed: {attrs.get('title')!r}", kind="stale_release")
    if attrs.get("node_license") not in (None, {}):
        raise err(
            f"OSF node now declares a licence ({attrs.get('node_license')!r}): review the rights record",
            kind="licence",
        )
    by_name = {}
    for f in listing:
        a = f.get("attributes", {})
        by_name[a.get("name")] = {
            "sha256": ((a.get("extra") or {}).get("hashes") or {}).get("sha256"),
            "size": a.get("size"),
            "modified": a.get("date_modified"),
            "download": (f.get("links") or {}).get("download"),
        }
    out = {
        "node": OSF_NODE,
        "title": attrs.get("title"),
        "node_license": None,
        "date_modified": attrs.get("date_modified"),
    }
    for name, payload in payloads.items():
        rec = by_name.get(name)
        if rec is None:
            raise err(f"OSF file {name!r} is no longer listed", kind="stale_release")
        got = sha256_hex(payload)
        if rec["sha256"] != got:
            raise err(f"{name}: sha256 {got} does not match the OSF listing ({rec['sha256']})", kind="integrity")
        out[name] = rec
    return out


def parse_rcs(body: bytes, *, raw_sha256: str, min_rows: int = RCS_MIN_ROWS) -> tuple[Any, dict[str, Any]]:
    """The RCS-Dem 2.0 workbook -> atlas frame (``rcs.dem2``) and a parse report."""
    wb = XlsxStream(body)
    if RCS_SHEET not in wb.sheets:
        raise err(f"RCS-Dem workbook has no sheet {RCS_SHEET!r} ({sorted(wb.sheets)})")
    rows = wb.rows(RCS_SHEET)
    try:
        _hnum, hcells = next(rows)
    except StopIteration as e:
        raise err("RCS-Dem sheet is empty") from e
    hdr = header_map(hcells)
    pcs = [p + "PC" for p in RCS_PC]
    missing = [c for c in (*RCS_REQUIRED, *pcs) if c not in hdr]
    if missing:
        raise err(f"RCS-Dem header lacks {missing}")
    hints = concept_hints(CATALOG_FILE, RCS_ATLAS_SOURCE, RCS_DATASET)
    buf = RowBuffer(RCS_ATLAS_SOURCE, RCS_DATASET)
    seen: set[tuple[str, int]] = set()
    rep: Counter[str] = Counter()
    unmapped: dict[str, str] = {}
    undocumented: Counter[str] = Counter()
    pc_units = {v: "%" for v in pcs}
    var_units = {**pc_units, **RCS_EXTRA}
    cols = [(v, hdr[v], var_units[v]) for v in (*pcs, *RCS_EXTRA)]
    for rnum, cells in rows:

        def text(col: str, cells: dict[int, Any] = cells) -> str | None:
            c = cells.get(hdr[col])
            return c.text.strip() if c is not None and c.text.strip() != "" else None

        cc, yr = text("CCODE"), text("YEAR")
        if cc is None and yr is None:
            continue
        if cc is None or yr is None or num(cc) is None or num(yr) is None:
            raise err(f"RCS-Dem row {rnum}: CCODE/YEAR not numeric ({cc!r}, {yr!r})")
        ccode, year = str(int(float(cc))), int(float(yr))
        if (ccode, year) in seen:
            raise err(f"RCS-Dem row {rnum}: duplicate unit-year {ccode}/{year}")
        seen.add((ccode, year))
        rep["unit_years"] += 1
        abbrev, iso_txt, indep = text("ABBREV"), text("ISO3"), text("INDEP")
        iso3 = crosswalk_key(iso_txt) or RCS_ALIASES.get(ccode)
        if iso3 is None:
            unmapped[ccode] = f"{abbrev}|iso3={iso_txt}"
        flags = canon({"rcs_abbrev": abbrev, "rcs_iso3": iso_txt, "independent_dec31": indep})
        period = year_period(year)
        for var, ci, unit in cols:
            c = cells.get(ci)
            if c is None or c.text.strip() == "":
                rep["blank_cells"] += 1
                continue
            v = num(c.text)
            sel = canon(sheet=RCS_SHEET, row=rnum, column=var)
            if v is None:
                buf.add(
                    indicator=var, provider_geo=ccode, period=period, value=None, value_text=c.text[:4000],
                    unit=unit, flags=canon(json.loads(flags), non_numeric_text=True), raw_sha256=raw_sha256,
                    selector=sel, iso3=iso3, concept_id=hints.get(var),
                )  # fmt: skip
                rep["non_numeric_cells"] += 1
                continue
            buf.add(
                indicator=var, provider_geo=ccode, period=period, value=v, unit=unit, flags=flags,
                raw_sha256=raw_sha256, selector=sel, iso3=iso3, concept_id=hints.get(var),
            )  # fmt: skip
        for var in RCS_CODE_VARS:
            raw = text(var)
            if raw is None:
                continue
            v = num(raw)
            if v is None:
                raise err(f"RCS-Dem row {rnum}: {var}={raw!r} is not a code")
            label = RCS_CODES.get(v, (None, None))[1]
            f = flags
            if label is None:
                undocumented[f"{var}={v}"] += 1
                f = canon(json.loads(flags), undocumented_code=True)
            buf.add(
                indicator=var, provider_geo=ccode, period=period, value=v, value_text=label, unit="code",
                flags=f, raw_sha256=raw_sha256, selector=canon(sheet=RCS_SHEET, row=rnum, column=var),
                iso3=iso3, concept_id=hints.get(var),
            )  # fmt: skip
    if rep["unit_years"] < min_rows:
        raise err(f"RCS-Dem: only {rep['unit_years']} unit-years (expected >= {min_rows}): truncated file?")
    report = {
        **dict(rep),
        "cells": len(buf),
        "units": len({g for g, _ in seen}),
        "years": [min(y for _, y in seen), max(y for _, y in seen)] if seen else None,
        "unmapped_units": dict(sorted(unmapped.items(), key=lambda kv: int(kv[0]))),
        "undocumented_codes": dict(undocumented),
    }
    return buf.frame(), report


# ----------------------------------------------------------------------------- WRP (COW)
WRP_CONNECTOR = "atlas.wrp"
WRP_VERSION = "1.0.0"
WRP_SOURCE_KEY = "cow.data"
WRP_ATLAS_SOURCE = "wrp"
WRP_DATASET = "national"
WRP_HOST = "correlatesofwar.org"
WRP_URL = f"https://{WRP_HOST}/wp-content/uploads/WRP_national.csv"
WRP_FILE = "WRP_national.csv"
WRP_MIN_ROWS = 1900
WRP_RIGHTS: dict[str, Any] = {
    "licence": "COW terms of use (not an open licence): data must be cited; users cannot charge for access",
    "licence_status": "free_with_citation_no_charging",
    "open_licence": False,
    "licence_url": "https://correlatesofwar.org/faq/",
    "attribution": 'Zeev Maoz and Errol A. Henderson, 2013, "The World Religion Dataset, 1945-2010: Logic, '
    'Estimates, and Trends," International Interactions 39: 265-291. World Religion Project national data v1.1, '
    "hosted by the Correlates of War Project.",
    "source_key": WRP_SOURCE_KEY,
}
#: Adherent count columns (persons) and their codebook labels (WRP codebook, national dataset table).
WRP_COUNTS: dict[str, str] = {
    "chrstprot": "Christianity - Protestants", "chrstcat": "Christianity - Roman Catholics",
    "chrstorth": "Christianity - Eastern Orthodox", "chrstang": "Christianity - Anglican",
    "chrstothr": "Christianity - Others", "chrstgen": "Christianity - Total",
    "judorth": "Judaism - Orthodox", "jdcons": "Judaism - Conservatives", "judref": "Judaism - Reform",
    "judothr": "Judaism - Others", "judgen": "Judaism - Total",
    "islmsun": "Islam - Sunni", "islmshi": "Islam - Shi'a", "islmibd": "Islam - Ibadhi",
    "islmnat": "Islam - Nation of Islam", "islmalw": "Islam - Alawite", "islmahm": "Islam - Ahmadiyya",
    "islmothr": "Islam - Other", "islmgen": "Islam - Total",
    "budmah": "Buddhism - Mahayana", "budthr": "Buddhism - Theravada", "budothr": "Buddhism - Other",
    "budgen": "Buddhism - Total", "zorogen": "Zoroastrian", "hindgen": "Hindu", "sikhgen": "Sikh",
    "shntgen": "Shinto", "bahgen": "Baha'i", "taogen": "Taoism", "jaingen": "Jain", "confgen": "Confucianism",
    "syncgen": "Syncretic Religions", "anmgen": "Animist Religions", "nonrelig": "Non-Religious",
    "othrgen": "Other Religions", "sumrelig": "Sum of religion adherents", "pop": "Total population",
}  # fmt: skip
WRP_PCTS = {f"{k}pct": v for k, v in WRP_COUNTS.items() if k != "pop"}
WRP_PCTS["judconspct"] = WRP_PCTS.pop("jdconspct")  # the share column is spelled judconspct in the file
WRP_FLAG_COLS = ("datatype", "sourcereliab", "recreliab", "reliabilevel", "sourcecode", "dualrelig", "Version")
#: WRP codebook, variable ``datatype`` value labels.
WRP_DATATYPE = {
    "1": "single source", "2": "multiple sources", "3": "interpolated", "4": "adjusted",
    "13": "single source, interpolated", "14": "single source, adjusted",
    "134": "single source, interpolated, adjusted", "23": "mult. sources, interpolated",
    "24": "mult. sources, adjusted", "234": "mult. sources, interpolated, adjusted",
    "34": "interpolated, adjusted",
}  # fmt: skip
COUNT_UNIT = "persons"
SHARE_UNIT = "proportion (0-1)"


def wrp_items(with_crosswalk: bool = True) -> tuple[FetchItem, ...]:
    items = [
        FetchItem(
            key="wrp.national",
            url=WRP_URL,
            max_bytes=10_000_000,
            expected_media_types=("text/csv", "application/octet-stream"),
        )
    ]
    if with_crosswalk:
        from reva.adapters.connectors.atlas_geopol_common import COUNTRYCODE_URL

        items.append(
            FetchItem(
                key="countrycode",
                url=COUNTRYCODE_URL,
                max_bytes=5_000_000,
                expected_media_types=("application/x-gzip", "application/gzip", "application/octet-stream"),
            )
        )
    return tuple(items)


def parse_wrp(
    body: bytes, *, raw_sha256: str, resolver: Any, min_rows: int = WRP_MIN_ROWS
) -> tuple[Any, dict[str, Any]]:
    required = ("year", "state", "name", *WRP_COUNTS, *WRP_PCTS, *WRP_FLAG_COLS)
    rows = read_csv(body, WRP_FILE, required=required)
    if len(rows) < min_rows:
        raise err(f"{WRP_FILE}: only {len(rows)} rows (expected >= {min_rows}): truncated file?")
    hints = concept_hints(CATALOG_FILE, WRP_ATLAS_SOURCE, WRP_DATASET)
    buf = RowBuffer(WRP_ATLAS_SOURCE, WRP_DATASET)
    seen: set[tuple[int, int]] = set()
    rep: Counter[str] = Counter()
    unresolved: dict[str, list[int]] = {}
    for i, r in enumerate(rows, start=2):
        try:
            ccode, year = int(r["state"]), int(r["year"])
        except ValueError as e:
            raise err(f"{WRP_FILE} line {i}: state/year not integers ({r['state']!r}, {r['year']!r})") from e
        if (ccode, year) in seen:
            raise err(f"{WRP_FILE} line {i}: duplicate state-year {ccode}/{year}")
        seen.add((ccode, year))
        iso3, reason, q = resolver.resolve(ccode, date(year, 12, 31))
        if iso3 is None:
            unresolved.setdefault(str(ccode), []).append(year)
        dt = r["datatype"].strip()
        base = {
            "cow_stateabb": r["name"].strip(),
            "iso3_resolution": reason,
            "datatype": dt,
            "datatype_label": WRP_DATATYPE.get(dt),
            "interpolated": "3" in dt if dt in WRP_DATATYPE else None,
            "sourcereliab": r["sourcereliab"].strip(),
            "recreliab": r["recreliab"].strip(),
            "reliabilevel": r["reliabilevel"].strip(),
            "sourcecode": r["sourcecode"].strip(),
            "dualrelig": r["dualrelig"].strip(),
            "wrp_version": r["Version"].strip(),
            "terms": "cite WRP/COW; no charging for access",
        }
        if dt not in WRP_DATATYPE:
            base["datatype_undocumented"] = True
            rep["undocumented_datatype_rows"] += 1
        if q is not None:
            base["entity_mapping"] = {
                "qid": q["qid"],
                "label": q.get("wikidata_label_en"),
                "review": q.get("review_status", "agent_mapped"),
            }
        flags = canon(base)
        period = year_period(year)
        for col, unit in [(c, COUNT_UNIT) for c in WRP_COUNTS] + [(c, SHARE_UNIT) for c in WRP_PCTS]:
            txt = r[col].strip()
            if txt == "":
                rep["blank_cells"] += 1
                continue
            v = num(txt)
            if v is None:
                raise err(f"{WRP_FILE} line {i}: {col}={txt!r} is not a number")
            buf.add(
                indicator=col, provider_geo=str(ccode), period=period, value=v, unit=unit, flags=flags,
                raw_sha256=raw_sha256, selector=canon(file=WRP_FILE, row=i, column=col), iso3=iso3,
                concept_id=hints.get(col), entity_qid=q["qid"] if q else None,
            )  # fmt: skip
    report = {
        **dict(rep),
        "rows": len(rows),
        "cells": len(buf),
        "states": len({c for c, _ in seen}),
        "years": sorted({y for _, y in seen}),
        "code_resolution": dict(resolver.reasons),
        "codes_without_iso3": {k: [min(v), max(v)] for k, v in sorted(unresolved.items(), key=lambda kv: int(kv[0]))},
    }
    return buf.frame(), report


# ----------------------------------------------------------------------------- specs + ingest
def _spec(which: str) -> AtlasSourceSpec:
    if which == "rcs":
        return AtlasSourceSpec(
            name=RCS_CONNECTOR,
            version=RCS_VERSION,
            source_key=RCS_SOURCE_KEY,
            budget_key=RCS_SOURCE_KEY,
            atlas_source=RCS_ATLAS_SOURCE,
            provider="Religious Characteristics of States project (Davis Brown, Patrick James), via OSF / ARDA",
            licence=RCS_RIGHTS["licence"],
            allowed_hosts=RCS_HOSTS,
            datasets=(RCS_DATASET,),
            dataset_ids=("doi:10.17605/OSF.IO/7SR4M",),
            cadence_seconds=90 * 86400,
            planned_requests=len(rcs_items()),
            max_response_bytes=80_000_000,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(parse_rcs),
            field_mappings={
                "provider_geo": "CCODE",
                "iso3": "ISO3 (when a crosswalk key)",
                "indicators": [p + "PC" for p in RCS_PC] + list(RCS_EXTRA) + list(RCS_CODE_VARS),
            },
            notes="OSF node record + file listing (sha256) + usage agreement + XLSX; not an open licence (flagged).",
        )
    return AtlasSourceSpec(
        name=WRP_CONNECTOR,
        version=WRP_VERSION,
        source_key=WRP_SOURCE_KEY,
        budget_key=WRP_SOURCE_KEY,
        atlas_source=WRP_ATLAS_SOURCE,
        provider="World Religion Project (Maoz & Henderson), hosted by the Correlates of War Project",
        licence=WRP_RIGHTS["licence"],
        allowed_hosts=(WRP_HOST, "cran.r-project.org"),
        datasets=(WRP_DATASET,),
        dataset_ids=("WRP national v1.1",),
        cadence_seconds=90 * 86400,
        planned_requests=len(wrp_items()),
        max_response_bytes=10_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(parse_rcs),
        field_mappings={"provider_geo": "state (COW code)", "counts": list(WRP_COUNTS), "shares": list(WRP_PCTS)},
        notes="WRP_national.csv + countrycode tarball (COW code resolution, archived); COW terms (flagged).",
    )


def atlas_source() -> list[AtlasSourceSpec]:
    return [_spec("rcs"), _spec("wrp")]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == RCS_CONNECTOR:
        return await _ingest_rcs(run)
    return await _ingest_wrp(run)


async def _ingest_rcs(run: Any) -> dict[str, Any]:
    if not run.wants(RCS_DATASET):
        return {"dataset": RCS_DATASET, "skipped": True}
    got = await fetch_archive(run, rcs_items())
    meta = check_osf(
        got.bodies["rcs.node"],
        got.bodies["rcs.files"],
        {RCS_XLSX_NAME: got.bodies["rcs.xlsx"], RCS_USAGE_NAME: got.bodies["rcs.usage"]},
    )
    usage = got.bodies["rcs.usage"].decode("utf-8", "replace")
    if "Usage Agreement" not in usage or "acknowledge the Association of Religion Data Archives" not in usage:
        raise err("RCS-Dem usage agreement text changed: review the rights record", kind="licence")
    live = run.origin == "live"
    frame, rep = parse_rcs(
        got.bodies.pop("rcs.xlsx"), raw_sha256=got.shas["rcs.xlsx"], min_rows=RCS_MIN_ROWS if live else 1
    )
    note = (
        f"RCS-Dem 2.0 (OSF 7sr4m, {RCS_XLSX_NAME}); {len(RCS_PC)} percentage variables (% of total population, "
        "0-100) + TOTPP, RELPP, RELPC + MAJID/SUPID codes; zeros as published, blanks omitted; iso3 from the RCS "
        "ISO3 field (RCS unit continuity, flags.rcs_abbrev). Not an open licence: ARDA usage agreement (attribution)."
    )
    reg = run.register(
        dataset=RCS_DATASET,
        frame=frame,
        raw_sha256s=[got.shas["rcs.xlsx"]],
        coverage_note=note,
        geo_scheme=None,
        rights={**RCS_RIGHTS, "osf": meta, "usage_agreement_raw_sha256": got.shas["rcs.usage"],
                "record_raw_sha256s": [got.shas["rcs.node"], got.shas["rcs.files"]]},
    )  # fmt: skip
    return {"dataset": RCS_DATASET, "items": len(got.shas), "parse": rep, "atlas": reg}


async def _ingest_wrp(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_cow import CodeResolver, load_crosswalk

    if not run.wants(WRP_DATASET):
        return {"dataset": WRP_DATASET, "skipped": True}
    got = await fetch_archive(run, wrp_items())
    resolver = CodeResolver(load_crosswalk(got.bodies["countrycode"]))
    live = run.origin == "live"
    frame, rep = parse_wrp(
        got.bodies["wrp.national"],
        raw_sha256=got.shas["wrp.national"],
        resolver=resolver,
        min_rows=WRP_MIN_ROWS if live else 1,
    )
    note = (
        "World Religion Project national v1.1 (WRP_national.csv), 1945-2010 every 5 years; adherent counts "
        "(persons) and shares (proportion 0-1, as published); datatype/reliability per row in flags; COW codes "
        "resolved on 31 Dec with KGGEOPOL's countrycode panel (archived). COW terms: cite, no charging (flagged)."
    )
    reg = run.register(
        dataset=WRP_DATASET,
        frame=frame,
        raw_sha256s=[got.shas["wrp.national"], got.shas["countrycode"]],
        coverage_note=note,
        geo_scheme=None,
        rights={**WRP_RIGHTS, "code_crosswalk_raw_sha256": got.shas["countrycode"]},
    )
    return {"dataset": WRP_DATASET, "items": len(got.shas), "parse": rep, "atlas": reg}


__all__ = [
    "RCS_CODES",
    "RCS_PC",
    "WRP_COUNTS",
    "WRP_PCTS",
    "atlas_ingest",
    "atlas_source",
    "check_osf",
    "parse_rcs",
    "parse_wrp",
    "rcs_items",
    "wrp_items",
]
