"""Oxford COVID-19 Government Response Tracker (OxCGRT): daily policy indices and dated policy-change events,
national level (agent V6POLITICS).

Source and licence (checked 2026-10-17)
---------------------------------------
``github.com/OxCGRT/covid-policy-dataset`` (the complete v1 dataset compiled June 2023, 185 national
jurisdictions, 2020-01-01 .. 2022-12-31), served from ``raw.githubusercontent.com``:
``data/OxCGRT_compact_national_v1.csv`` (~44 MB; the file type "we expect most people will use", README),
``README.md``, ``LICENSE.txt`` and ``documentation_and_codebook.md`` (the authoritative codebook). Licence **CC BY
4.0**: ``LICENSE.txt`` is the CC BY 4.0 legal code headed "Attribution 4.0 International" and the README says "Our
data is made available free to use for any purpose under a Creative Commons CC BY 4.0 license"; both are archived
and checked on every run (a changed statement registers nothing). Attribution: Hale et al. (2021), Nature Human
Behaviour, doi:10.1038/s41562-021-01079-8; "Oxford COVID-19 Government Response Tracker, Blavatnik School of
Government, University of Oxford".

Not ingested: ``ConfirmedCases`` / ``ConfirmedDeaths`` (epidemiological series OxCGRT took from third parties, not
OxCGRT's own coding), ``MajorityVaccinated`` / ``PopulationVaccinated`` ("We primarily source this data from Our
World In Data"), the categorical vaccine-eligibility indicators ``V1``-``V4`` (category lists, not levels), the
subnational files and the vaccinated / non-vaccinated (``E``/``NV``/``V``) variants (the compact file carries the
``M`` "majority" versions only).

Outputs
-------
* atlas ``oxcgrt.indices_daily``: per ISO3 x day, ``StringencyIndex_Average``, ``GovernmentResponseIndex_Average``,
  ``ContainmentHealthIndex_Average``, ``EconomicSupportIndex`` (0-100, verbatim; blank cells omitted). OxCGRT: the
  ``_Average`` version "is just the weighted average for most jurisdictions (but for which we substitute the simple
  average for jurisdictions where it is not possible to calculate the weighted average". The index "cannot say
  whether a government's policy has been implemented effectively" (README).
* atlas ``oxcgrt.policy_changes`` -- **DERIVED by REVA**: one row per (country, ordinal indicator, day) on which the
  recorded level or its flag differs from the last earlier day with a recorded level (``previous_*`` and
  ``previous_date`` in flags; ``gap_days`` when blank days lie between). value = the new level (verbatim),
  ``direction`` = stricter / less_strict / flag_only. The first recorded level of a series is not a change and is
  not emitted (counted). These are changes in OxCGRT's daily coding, i.e. dated policy events as OxCGRT recorded
  them, not REVA's reading of legal acts.
* atlas ``oxcgrt.fiscal_announcements``: the numeric indicators E3, E4, H4, H5 ("only reported on the day they are
  announced", USD) on days with a non-zero amount, verbatim.

The codebook's description and coding text of every indicator used is quoted verbatim in the table rights
(``codebook``), read from the archived ``documentation_and_codebook.md``. Nothing here is causal.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from reva.adapters.connectors import v6politics_common as vc
from reva.adapters.connectors.atlas_society_common import RowBuffer, canon
from reva.atlas.sources import AtlasSourceSpec

CONNECTOR = "atlas.oxcgrt"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_oxcgrt/1"
SOURCE_KEY = "oxcgrt.github"
ATLAS_SOURCE = "oxcgrt"
HOST = "raw.githubusercontent.com"
BASE = f"https://{HOST}/OxCGRT/covid-policy-dataset/main"
URLS = {
    "readme": f"{BASE}/README.md",
    "licence": f"{BASE}/LICENSE.txt",
    "codebook": f"{BASE}/documentation_and_codebook.md",
    "compact": f"{BASE}/data/OxCGRT_compact_national_v1.csv",
}
DATA_FILE = "OxCGRT_compact_national_v1.csv"
MAX_BYTES = {"readme": 1_000_000, "licence": 1_000_000, "codebook": 2_000_000, "compact": 80_000_000}
MIN_ROWS = 150_000
DATASETS = ("indices_daily", "policy_changes", "fiscal_announcements")
GEO_SCHEME = "iso3"
INDICES = ("StringencyIndex_Average", "GovernmentResponseIndex_Average", "ContainmentHealthIndex_Average",
           "EconomicSupportIndex")  # fmt: skip


@dataclass(frozen=True)
class Ordinal:
    code: str  # indicator key in the atlas (provider column prefix)
    column: str  # provider column, verbatim
    flag: str | None  # provider flag column
    base: str  # codebook id (C1 ... H8)


ORDINALS = (
    Ordinal("C1M", "C1M_School closing", "C1M_Flag", "C1"),
    Ordinal("C2M", "C2M_Workplace closing", "C2M_Flag", "C2"),
    Ordinal("C3M", "C3M_Cancel public events", "C3M_Flag", "C3"),
    Ordinal("C4M", "C4M_Restrictions on gatherings", "C4M_Flag", "C4"),
    Ordinal("C5M", "C5M_Close public transport", "C5M_Flag", "C5"),
    Ordinal("C6M", "C6M_Stay at home requirements", "C6M_Flag", "C6"),
    Ordinal("C7M", "C7M_Restrictions on internal movement", "C7M_Flag", "C7"),
    Ordinal("C8EV", "C8EV_International travel controls", None, "C8"),
    Ordinal("E1", "E1_Income support", "E1_Flag", "E1"),
    Ordinal("E2", "E2_Debt/contract relief", None, "E2"),
    Ordinal("H1", "H1_Public information campaigns", "H1_Flag", "H1"),
    Ordinal("H2", "H2_Testing policy", None, "H2"),
    Ordinal("H3", "H3_Contact tracing", None, "H3"),
    Ordinal("H6M", "H6M_Facial Coverings", "H6M_Flag", "H6"),
    Ordinal("H7", "H7_Vaccination policy", "H7_Flag", "H7"),
    Ordinal("H8M", "H8M_Protection of elderly people", "H8M_Flag", "H8"),
)
NUMERICS = (("E3", "E3_Fiscal measures"), ("E4", "E4_International support"),
            ("H4", "H4_Emergency investment in healthcare"), ("H5", "H5_Investment in vaccines"))  # fmt: skip
REQUIRED = ("CountryName", "CountryCode", "RegionCode", "Jurisdiction", "Date", *INDICES,
            *(o.column for o in ORDINALS), *(o.flag for o in ORDINALS if o.flag), *(c for _, c in NUMERICS))  # fmt: skip
RIGHTS: dict[str, Any] = {
    "licence": "CC-BY-4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "open_licence": True,
    "attribution": "Oxford COVID-19 Government Response Tracker, Blavatnik School of Government, University of "
    "Oxford. Hale, T. et al. (2021). A global panel database of pandemic policies (Oxford COVID-19 Government "
    "Response Tracker). Nature Human Behaviour. https://doi.org/10.1038/s41562-021-01079-8",
    "source_key": SOURCE_KEY,
}
LICENCE_HEAD = "Attribution 4.0 International"
README_LICENCE = "Our data is made available free to use for any purpose under a Creative Commons CC BY 4.0 license"
_ROW = re.compile(r"^\|\s*([CEH][0-9])\s*\|(.*)$")


def check_licence(readme: bytes, licence: bytes) -> dict[str, str]:
    lt = licence.decode("utf-8", errors="replace").lstrip("﻿").lstrip()
    if not lt.startswith(LICENCE_HEAD) or "creativecommons.org/licenses/by/4.0" not in lt:
        raise vc.err("LICENSE.txt is no longer the CC BY 4.0 legal code", kind="licence")
    rt = readme.decode("utf-8", errors="replace")
    if README_LICENCE not in rt:
        raise vc.err("README no longer states the CC BY 4.0 licence sentence", kind="licence")
    return {"licence_head": LICENCE_HEAD, "readme_statement": README_LICENCE}


def parse_codebook(body: bytes) -> dict[str, dict[str, str]]:
    """Description and coding cells (verbatim, markdown kept) of the detailed codebook rows C1..H8."""
    out: dict[str, dict[str, str]] = {}
    for line in body.decode("utf-8", errors="replace").splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 4 or "`" not in cells[0] or m.group(1) in out:
            continue
        out[m.group(1)] = {"columns": cells[0], "description": cells[1], "measurement": cells[2], "coding": cells[3]}
    need = {o.base for o in ORDINALS} | {c for c, _ in NUMERICS}
    missing = sorted(need - set(out))
    if missing:
        raise vc.err(f"documentation_and_codebook.md: no codebook row for {missing}")
    return {k: v for k, v in out.items() if k in need}


def _dec(text: str, what: str) -> Decimal | None:
    t = text.strip()
    if t == "":
        return None
    try:
        return Decimal(t)
    except InvalidOperation as e:
        raise vc.err(f"{what}: {t!r} is not a number") from e


def parse_compact(body: bytes, *, raw_sha256: str, min_rows: int = MIN_ROWS) -> tuple[Any, Any, Any, dict[str, Any]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise vc.err(f"{DATA_FILE}: not UTF-8 ({e})") from e
    rd = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED if c not in (rd.fieldnames or [])]
    if missing:
        raise vc.err(f"{DATA_FILE}: missing columns {missing}")
    idx = RowBuffer(ATLAS_SOURCE, "indices_daily")
    chg = RowBuffer(ATLAS_SOURCE, "policy_changes")
    fis = RowBuffer(ATLAS_SOURCE, "fiscal_announcements")
    rep: Counter[str] = Counter()
    last: dict[tuple[str, str], tuple[Decimal, str, str | None, date, int]] = {}
    prev_day: dict[str, date] = {}
    names: dict[str, str] = {}
    n = 0
    for i, r in enumerate(rd, start=2):
        n += 1
        geo = r["CountryCode"].strip()
        if r["Jurisdiction"].strip() != "NAT_TOTAL" or r["RegionCode"].strip():
            raise vc.err(f"{DATA_FILE} line {i}: not a national row ({r['Jurisdiction']!r}, {r['RegionCode']!r})")
        try:
            day = date(int(r["Date"][:4]), int(r["Date"][4:6]), int(r["Date"][6:8]))
        except ValueError as e:
            raise vc.err(f"{DATA_FILE} line {i}: Date {r['Date']!r} is not YYYYMMDD") from e
        if geo in prev_day and day <= prev_day[geo]:
            raise vc.err(f"{DATA_FILE} line {i}: dates of {geo} are not strictly increasing")
        prev_day[geo] = day
        names.setdefault(geo, r["CountryName"].strip())
        period = (day, day + timedelta(days=1))
        for col in INDICES:
            v = _dec(r[col], f"{DATA_FILE} line {i} {col}")
            if v is None:
                rep["index_blank"] += 1
                continue
            idx.add(indicator=col, provider_geo=geo, period=period, value=r[col].strip(), unit="index (0-100)",
                    flags="{}", raw_sha256=raw_sha256, selector=canon(file=DATA_FILE, row=i, column=col))  # fmt: skip
        for code, col in NUMERICS:
            v = _dec(r[col], f"{DATA_FILE} line {i} {col}")
            if v is None or v == 0:
                continue
            fis.add(indicator=code, provider_geo=geo, period=period, value=r[col].strip(), unit="USD",
                    flags=canon(name=col), raw_sha256=raw_sha256,
                    selector=canon(file=DATA_FILE, row=i, column=col))  # fmt: skip
        for o in ORDINALS:
            v = _dec(r[o.column], f"{DATA_FILE} line {i} {o.column}")
            if v is None:
                continue
            fl = r[o.flag].strip() if o.flag else None
            fl = fl or None
            prev = last.get((geo, o.code))
            last[(geo, o.code)] = (v, r[o.column].strip(), fl, day, i)
            if prev is None:
                rep["first_levels_not_changes"] += 1
                continue
            pv, ptxt, pfl, pday, pline = prev
            if pv == v and pfl == fl:
                continue
            flags: dict[str, Any] = {
                "name": o.column, "previous_value": ptxt, "previous_date": pday.isoformat(),
                "direction": "stricter" if v > pv else "less_strict" if v < pv else "flag_only",
                "derived": "REVA: level or flag differs from the last earlier day with a recorded level",
            }  # fmt: skip
            if o.flag:
                flags.update(flag_column=o.flag, flag=fl, previous_flag=pfl)
            gap = (day - pday).days - 1
            if gap:
                flags["gap_days"] = gap
            chg.add(indicator=o.code, provider_geo=geo, period=period, value=r[o.column].strip(),
                    unit="ordinal level (OxCGRT codebook)", flags=canon(flags), raw_sha256=raw_sha256,
                    selector=canon(file=DATA_FILE, rows=[pline, i], columns=[o.column, *([o.flag] if o.flag else [])]))  # fmt: skip
            rep[f"changes_{flags['direction']}"] += 1
    if n < min_rows:
        raise vc.err(f"{DATA_FILE}: only {n} rows (expected >= {min_rows}): truncated file?")
    rep.update(rows=n, countries=len(prev_day), index_cells=len(idx), change_rows=len(chg), fiscal_rows=len(fis))
    days = sorted(prev_day.values())
    return idx.frame(), chg.frame(), fis.frame(), {
        **dict(rep), "last_day": days[-1].isoformat() if days else None,
        "non_iso_codes": sorted(g for g in prev_day if not re.fullmatch(r"[A-Z]{3}", g)), "names": names,
    }  # fmt: skip


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Oxford COVID-19 Government Response Tracker (Blavatnik School of Government, University of Oxford)",
        licence="CC-BY-4.0",
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=("OxCGRT/covid-policy-dataset:data/OxCGRT_compact_national_v1.csv",),
        cadence_seconds=365 * 86400,
        planned_requests=len(URLS),
        max_response_bytes=max(MAX_BYTES.values()),
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=vc.code_digest(sys.modules[__name__]),
        field_mappings={"geo_scheme": GEO_SCHEME, "tables": [f"oxcgrt.{d}" for d in DATASETS]},
        notes="README + LICENSE.txt (CC BY 4.0 checked) + codebook + compact national CSV; final dataset (2020-2022).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    live = run.origin == "live"
    media = {"readme": ("text/plain", "text/markdown"), "licence": ("text/plain",), "codebook": ("text/plain", "text/markdown"),
             "compact": ("text/plain", "text/csv", "application/octet-stream")}  # fmt: skip
    raws = {
        k: await run.get(u, key=f"oxcgrt.{k}", media_types=media[k], max_bytes=MAX_BYTES[k]) for k, u in URLS.items()
    }
    lic = check_licence(raws["readme"].body, raws["licence"].body)
    codebook = parse_codebook(raws["codebook"].body)
    shas = {k: run.archive(r) for k, r in raws.items()}
    body = run.store.get("raw", shas["compact"])
    del raws
    idx, chg, fis, rep = parse_compact(body, raw_sha256=shas["compact"], min_rows=MIN_ROWS if live else 1)
    del body
    rights = {**RIGHTS, **lic, "licence_raw_sha256": shas["licence"], "readme_raw_sha256": shas["readme"],
              "codebook_raw_sha256": shas["codebook"], "codebook": codebook}  # fmt: skip
    notes = {
        "indices_daily": "OxCGRT compact national v1 (final, 2020-2022): the four policy indices (_Average versions) "
        "per country and day, verbatim. CC BY 4.0.",
        "policy_changes": "DERIVED by REVA from OxCGRT compact national v1: one row per country, ordinal indicator "
        "(C1M-C8EV, E1-E2, H1-H3, H6M-H8M) and day where the coded level or flag differs from the last earlier "
        "recorded day; previous level/flag/date in flags. Changes in OxCGRT's coding, not legal acts. CC BY 4.0.",
        "fiscal_announcements": "OxCGRT compact national v1: E3, E4, H4, H5 (USD, reported on the announcement day) "
        "on days with a non-zero amount, verbatim. OxCGRT stopped updating E3, E4, H4 in August 2021. CC BY 4.0.",
    }
    res: dict[str, Any] = {"report": {k: v for k, v in rep.items() if k != "names"}, "atlas": {}}
    for ds, frame in (("indices_daily", idx), ("policy_changes", chg), ("fiscal_announcements", fis)):
        if run.wants(ds):
            r = run.register(dataset=ds, frame=frame, raw_sha256s=[shas["compact"]], coverage_note=notes[ds],
                             geo_scheme=GEO_SCHEME, rights=rights)  # fmt: skip
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    return res


__all__ = ["DATASETS", "NUMERICS", "ORDINALS", "atlas_ingest", "atlas_source", "check_licence", "parse_codebook",
           "parse_compact"]  # fmt: skip
