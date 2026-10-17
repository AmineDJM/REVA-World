"""WhoGov (Nyrup & Bramwell) Version 4: cabinets of 177 countries since 1966 -- country-year cabinet composition,
dated government and leader starts, and government KG events (agent V6POLITICS).

Source and licence (checked 2026-10-17)
---------------------------------------
DataverseNO doi:10.18710/OLPOZK "The WhoGov Project" (published 2026-08-26, version 1.0), licence **CC BY 4.0**
(record ``license.name``; the archived ``0_ReadMe.txt`` says "Licenses/Restrictions: CC BY 4.0."). Files used:
``WhoGov_crosssectional_V4.0.csv`` (country-year, 9,495 rows; "the .csv version is authoritative"),
``0_ReadMe.txt`` and ``whogov_codebookandappendix.pdf`` (definitions); MD5 of each checked against the record.
The individual-level file (``WhoGov_within_V4.0.csv``, 82 MB, one row per cabinet member) is **not** fetched in this
container (disk budget): queued for a bigger machine (docs/dev/v6_politics.md).

WhoGov's own codebook says ``year`` is a July snapshot ("We are using July for all years apart from 1966, where data
was only available for September and 1970, where we are using January instead of July"), ``leader_start_date`` /
``leader_end_date`` rely on Archigos, ``govern_name`` / ``govern_start_date`` / ``govern_end_date`` are "based on
Döring (2019) or Bértoa (2020)" (ParlGov / Who Governs Europe) and ``system_category`` is "as classified by Cheibub et
al. (2010) and updated by Bjørnskov and Rode (2018)". REVA keeps those attributions in flags; it never matches
WhoGov governments to ParlGov cabinets by name or date.

Outputs
-------
* atlas ``whogov.cabinet_cy``: per ISO3 x year (the July snapshot), the numeric cross-sectional variables
  verbatim (``n_total``, ``n_core``, ``n_female_*``, ``retention_rate*``, ``age_*``, ``n_party`` ...; "NA" omitted)
  and the texts ``leader``, ``leader_party``, ``govern_name``, ``system_category`` (value_text). Ministers' names
  (``m_finance`` ...) are not loaded.
* atlas ``whogov.government_starts``: one-day periods: ``GOVERNMENT_START`` (each distinct ``govern_start_date`` of a
  country; value_text = the government name; all names / end dates / snapshot years WhoGov gives for that start in
  flags) and ``LEADER_START`` (each distinct ``leader_start_date``; value_text = the leader's name).
* KG (source ``whogov.dataverseno``): ``whogov:government:<ISO3>:<govern_start_date>`` (kind ``cabinet``; WhoGov
  publishes no government id, the key is the source's own country code + start day): ``P580`` start, ``P582`` end
  (each distinct ``govern_end_date``), ``P17`` (country QID when ``country_qids.json`` has one), and
  ``whogov:government_name`` (each distinct spelling, verbatim).
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from reva.adapters.connectors import v6politics_common as vc
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement
from reva.adapters.connectors.atlas_society_common import RowBuffer, canon, check_dataverse_record, year_period
from reva.atlas.sources import AtlasSourceSpec

CONNECTOR = "atlas.whogov"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_whogov/1"
SOURCE_KEY = "whogov.dataverseno"
ATLAS_SOURCE = "whogov"
HOST = "dataverse.no"
S3 = "uit-dataverseno-prod01.s3-oslo.educloud.no"
DOI = "doi:10.18710/OLPOZK"
RECORD_URL = f"https://{HOST}/api/datasets/:persistentId/?persistentId={DOI}"
FILES: dict[str, tuple[int, str, tuple[str, ...]]] = {
    "readme": (295623, "0_ReadMe.txt", ("text/plain",)),
    "cs": (290057, "WhoGov_crosssectional_V4.0.csv", ("text/csv", "text/plain", "application/octet-stream")),
    "codebook": (295899, "whogov_codebookandappendix.pdf", ("application/pdf",)),
}
CS_FILE = FILES["cs"][1]
MIN_ROWS = 9000
DATASETS = ("cabinet_cy", "government_starts")
README_LICENCE = "Licenses/Restrictions: CC BY 4.0."
NUM_VARS = ("n_total", "n_individuals", "n_core", "n_minister", "leaderexperience_continuous", "leaderexperience_total",
            "n_female_total", "n_female_core", "n_female_minister", "n_militarytitle_total", "n_militarytitle_core",
            "n_militarytitle_minister", "average_total", "average_core", "average_minister", "retention_rate_total",
            "retention_rate_core", "retention_rate_minister", "retention_rateadj_total", "retention_rateadj_core",
            "retention_rateadj_minister", "age_total", "age_core", "age_minister", "age_share", "n_party",
            "party_share")  # fmt: skip
TEXT_VARS = ("leader", "leader_party", "govern_name", "system_category")
DATE_VARS = ("leader_start_date", "leader_end_date", "govern_start_date", "govern_end_date")
REQUIRED = ("year", "country_isocode", "country_name", *NUM_VARS, *TEXT_VARS, *DATE_VARS)
ATTRIBUTION_NOTES = {
    "leader": "WhoGov codebook: leader dates rely on Archigos (2009)",
    "govern": "WhoGov codebook: government name and dates based on Döring (2019) or Bértoa (2020)",
    "system_category": "WhoGov codebook: as classified by Cheibub et al. (2010) and updated by Bjørnskov and Rode (2018)",
}
RIGHTS: dict[str, Any] = {
    "licence": "CC-BY-4.0",
    "licence_url": "http://creativecommons.org/licenses/by/4.0",
    "open_licence": True,
    "attribution": "Nyrup, Jacob, Stuart Bramwell et al. 2026. The WhoGov Project (Version 4), DataverseNO, "
    "https://doi.org/10.18710/OLPOZK; Nyrup & Bramwell (2020), Who governs? American Political Science Review "
    "114(4): 1366-1374.",
    "source_key": SOURCE_KEY,
}


def unit_of(var: str) -> str:
    if var == "n_party":
        return "parties"
    if var.startswith("n_"):
        return "persons"
    if var.startswith("average_"):
        return "years (average tenure)"
    if var in ("age_share", "party_share") or var.startswith("retention_rate"):
        return "share (0-1)"
    if var.startswith("age_"):
        return "years (average age)"
    return "years"


_MISSING: dict[str, Any] = {"missing": "not_reported"}
_NUM = re.compile(r"^-?[0-9]+(\.[0-9]+)?([eE]-?[0-9]+)?$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _na(v: str | None) -> str | None:
    t = (v or "").strip()
    return None if t in ("", "NA") else t


def _day(v: str | None, what: str) -> date | None:
    t = _na(v)
    if t is None:
        return None
    if not _DAY.match(t):
        raise vc.err(f"{what}: {t!r} is not YYYY-MM-DD")
    return date.fromisoformat(t)


def parse_cs(body: bytes, *, min_rows: int = MIN_ROWS) -> list[tuple[int, dict[str, str]]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise vc.err(f"{CS_FILE}: not UTF-8 ({e})") from e
    rd = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED if c not in (rd.fieldnames or [])]
    if missing:
        raise vc.err(f"{CS_FILE}: missing columns {missing}")
    rows = list(enumerate(rd, start=2))
    if len(rows) < min_rows:
        raise vc.err(f"{CS_FILE}: only {len(rows)} rows (expected >= {min_rows}): truncated file?")
    seen: set[tuple[str, str]] = set()
    for i, r in rows:
        k = (r["country_isocode"].strip(), r["year"].strip())
        if not re.fullmatch(r"[A-Z]{3}", k[0]) or not k[1].isdigit():
            raise vc.err(f"{CS_FILE} line {i}: country/year {k} malformed")
        if k in seen:
            raise vc.err(f"{CS_FILE} line {i}: duplicate country-year {k}")
        seen.add(k)
    return rows


def build_frames(rows: list[tuple[int, dict[str, str]]], *, raw_sha256: str) -> tuple[Any, Any, dict[str, Any]]:
    cy = RowBuffer(ATLAS_SOURCE, "cabinet_cy")
    gs = RowBuffer(ATLAS_SOURCE, "government_starts")
    rep: Counter[str] = Counter()
    govs: dict[tuple[str, date], dict[str, Any]] = {}
    leaders: dict[tuple[str, date], dict[str, Any]] = {}
    for i, r in rows:
        geo, year = r["country_isocode"].strip(), int(r["year"])
        period = year_period(year)
        base = canon(snapshot="WhoGov year = July snapshot (September 1966, January 1970), per the codebook")
        for v in NUM_VARS:
            t = _na(r[v])
            if t is None:
                continue
            if not _NUM.match(t):
                raise vc.err(f"{CS_FILE} line {i}: {v} = {t!r} is not a number")
            cy.add(indicator=v, provider_geo=geo, period=period, value=t, unit=unit_of(v), flags=base,
                   raw_sha256=raw_sha256, selector=canon(file=CS_FILE, row=i, column=v))  # fmt: skip
        for v in TEXT_VARS:
            t = _na(r[v])
            if t is None:
                continue
            note = ATTRIBUTION_NOTES["govern" if v == "govern_name" else "leader" if v.startswith("leader")
                                     else "system_category"]  # fmt: skip
            cy.add(indicator=v, provider_geo=geo, period=period, value=None, value_text=t, unit="text",
                   flags=canon(snapshot="July snapshot", based_on=note), raw_sha256=raw_sha256,
                   selector=canon(file=CS_FILE, row=i, column=v))  # fmt: skip
        gstart = _day(r["govern_start_date"], f"{CS_FILE} line {i} govern_start_date")
        gname = _na(r["govern_name"])
        if gstart is not None:
            g = govs.setdefault(
                (geo, gstart), {"names": [], "ends": [], "years": [], "lines": [], "country": r["country_name"]}
            )
            if gname and gname not in g["names"]:
                g["names"].append(gname)
            end = _day(r["govern_end_date"], f"{CS_FILE} line {i} govern_end_date")
            if end is not None and end not in g["ends"]:
                g["ends"].append(end)
            g["years"].append(year)
            g["lines"].append(i)
        elif gname:
            rep["govern_name_without_start_date"] += 1
        lstart = _day(r["leader_start_date"], f"{CS_FILE} line {i} leader_start_date")
        if lstart is not None:
            lname = _na(r["leader"])
            ld = leaders.setdefault((geo, lstart), {"names": [], "ends": [], "years": [], "lines": [], "parties": []})
            if lname and lname not in ld["names"]:
                ld["names"].append(lname)
            lend = _day(r["leader_end_date"], f"{CS_FILE} line {i} leader_end_date")
            if lend is not None and lend not in ld["ends"]:
                ld["ends"].append(lend)
            lp = _na(r["leader_party"])
            if lp and lp not in ld["parties"]:
                ld["parties"].append(lp)
            ld["years"].append(year)
            ld["lines"].append(i)
    for (geo, d), g in sorted(govs.items()):
        if len(g["names"]) > 1:
            rep["government_starts_with_several_names"] += 1
        gs.add(indicator="GOVERNMENT_START", provider_geo=geo, period=(d, date.fromordinal(d.toordinal() + 1)),
               value=None, value_text=g["names"][0] if g["names"] else None, unit="event",
               flags=canon(names=g["names"], end_dates=[e.isoformat() for e in g["ends"]], snapshot_years=g["years"],
                           based_on=ATTRIBUTION_NOTES["govern"], **({} if g["names"] else _MISSING)),
               raw_sha256=raw_sha256, selector=canon(file=CS_FILE, rows=g["lines"], column="govern_start_date"))  # fmt: skip
    for (geo, d), ld in sorted(leaders.items()):
        if len(ld["names"]) > 1:
            rep["leader_starts_with_several_names"] += 1
        gs.add(indicator="LEADER_START", provider_geo=geo, period=(d, date.fromordinal(d.toordinal() + 1)),
               value=None, value_text=ld["names"][0] if ld["names"] else None, unit="event",
               flags=canon(names=ld["names"], end_dates=[e.isoformat() for e in ld["ends"]], parties=ld["parties"],
                           snapshot_years=ld["years"], based_on=ATTRIBUTION_NOTES["leader"],
                           **({} if ld["names"] else _MISSING)),
               raw_sha256=raw_sha256, selector=canon(file=CS_FILE, rows=ld["lines"], column="leader_start_date"))  # fmt: skip
    rep.update(governments=len(govs), leader_starts=len(leaders), cy_cells=len(cy))
    return cy.frame(), gs.frame(), {**dict(rep), "_govs": govs}


def kg_records(govs: Mapping[tuple[str, date], Mapping[str, Any]], *, raw_sha256: str, qids: Mapping[str, str]
               ) -> tuple[list[KgEntity], list[KgStatement], dict[str, Any]]:  # fmt: skip
    ents: list[KgEntity] = []
    sts: list[KgStatement] = []
    no_qid: Counter[str] = Counter()
    for (geo, d), g in sorted(govs.items()):
        gid = f"whogov:government:{geo}:{d.isoformat()}"
        sel = {"file": CS_FILE, "rows": g["lines"]}
        name = g["names"][0] if g["names"] else "unnamed"
        ents.append(KgEntity(qid=gid, kind="cabinet", labels={"en": f"{name} government ({g['country']}, from {d.isoformat()})"},
                             descriptions={"en": "government as recorded by WhoGov V4 (start/end per WhoGov: based on "
                                                 "Döring 2019 or Bértoa 2020); P580 = the day it entered office"}))  # fmt: skip
        q = {"based_on": ATTRIBUTION_NOTES["govern"]}
        sts.append(KgStatement(key=f"{gid}:start", subject=gid, property="P580",
                               value={"type": "time", "time": d.isoformat(), "precision": 11}, qualifiers=q,
                               raw_sha256=raw_sha256, selector={**sel, "column": "govern_start_date"}))  # fmt: skip
        for e in g["ends"]:
            sts.append(KgStatement(key=f"{gid}:end:{e.isoformat()}", subject=gid, property="P582",
                                   value={"type": "time", "time": e.isoformat(), "precision": 11}, qualifiers=q,
                                   raw_sha256=raw_sha256, selector={**sel, "column": "govern_end_date"}))  # fmt: skip
        for n in g["names"]:
            sts.append(KgStatement(key=f"{gid}:name:{n}"[:200], subject=gid, property="whogov:government_name",
                                   value={"type": "string", "value": n}, raw_sha256=raw_sha256,
                                   selector={**sel, "column": "govern_name"}))  # fmt: skip
        qid = qids.get(geo)
        if qid is None:
            no_qid[geo] += 1
        else:
            sts.append(KgStatement(key=f"{gid}:country", subject=gid, property="P17", object_qid=qid,
                                   start=d.isoformat(), start_precision=11, qualifiers={"whogov_country": geo},
                                   raw_sha256=raw_sha256, selector={**sel, "column": "country_isocode"}))  # fmt: skip
    return ents, sts, {"governments": len(ents), "statements": len(sts), "iso3_without_qid": dict(no_qid)}


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="The WhoGov Project (Nyrup, Bramwell et al.), DataverseNO",
        licence="CC-BY-4.0",
        allowed_hosts=(HOST, S3),
        datasets=DATASETS,
        dataset_ids=(DOI,),
        cadence_seconds=180 * 86400,
        planned_requests=1 + len(FILES),
        max_response_bytes=10_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=vc.code_digest(sys.modules[__name__]),
        field_mappings={"geo_scheme": "iso3", "kg": "whogov:government:<ISO3>:<start>"},
        notes="Dataverse record (CC BY 4.0, MD5 of 3 files) + ReadMe licence line + cross-sectional CSV + codebook PDF.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    live = run.origin == "live"
    rec = await run.get(RECORD_URL, key="whogov.record", media_types=("application/json",), max_bytes=2_000_000)
    raws = {k: await run.get(f"https://{HOST}/api/access/datafile/{fid}", key=f"whogov.{k}", media_types=media,
                             max_bytes=10_000_000) for k, (fid, _, media) in FILES.items()}  # fmt: skip
    meta = check_dataverse_record(rec.body, doi=DOI, files={fid: (name, raws[k].body) for k, (fid, name, _) in FILES.items()},
                                  licences=("CC BY 4.0",))  # fmt: skip
    if README_LICENCE not in raws["readme"].body.decode("utf-8", errors="replace"):
        raise vc.err("0_ReadMe.txt no longer states 'Licenses/Restrictions: CC BY 4.0.'", kind="licence")
    rows = parse_cs(raws["cs"].body, min_rows=MIN_ROWS if live else 1)
    shas = {"record": run.archive(rec), **{k: run.archive(r) for k, r in raws.items()}}
    cy, gs, rep = build_frames(rows, raw_sha256=shas["cs"])
    govs = rep.pop("_govs")
    rights = {**RIGHTS, "record": meta, "record_raw_sha256": shas["record"], "readme_raw_sha256": shas["readme"],
              "codebook_raw_sha256": shas["codebook"], "readme_statement": README_LICENCE}  # fmt: skip
    notes = {
        "cabinet_cy": "WhoGov V4 cross-sectional file: cabinet composition per country and year (July snapshot), "
        "numeric variables and leader / party / government name / regime category texts verbatim. CC BY 4.0.",
        "government_starts": "WhoGov V4: each distinct govern_start_date (GOVERNMENT_START; based on Döring 2019 or "
        "Bértoa 2020 per WhoGov) and leader_start_date (LEADER_START; relies on Archigos per WhoGov) of a country, "
        "one-day periods, names and end dates in flags. CC BY 4.0.",
    }
    res: dict[str, Any] = {"record": meta, "report": rep, "atlas": {}}
    for ds, frame in (("cabinet_cy", cy), ("government_starts", gs)):
        if run.wants(ds):
            r = run.register(dataset=ds, frame=frame, raw_sha256s=[shas["cs"]], coverage_note=notes[ds],
                             geo_scheme="iso3", rights=rights)  # fmt: skip
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    ents, sts, krep = kg_records(govs, raw_sha256=shas["cs"], qids=vc.iso3_qids())
    res["kg_counts"] = krep
    if run.dataset is None and vc.kg_available(run.engine):
        res["kg"] = vc.load_kg(
            run.engine, run.store, ents, sts, properties=vc.kg_properties("whogov:"), source_key=SOURCE_KEY,
            source_revision=f"whogov-v4-{meta['version']}", raw_sha256s=[shas["cs"]],
            retrieved_at=raws["cs"].retrieved_at if isinstance(raws["cs"].retrieved_at, datetime) else datetime.now(),
            origin=run.origin,
        )  # fmt: skip
    return res


__all__ = ["atlas_ingest", "atlas_source", "build_frames", "kg_records", "parse_cs"]
