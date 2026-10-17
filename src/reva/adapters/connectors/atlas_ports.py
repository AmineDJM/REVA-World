"""Ports: NGA World Port Index (Pub. 150) -> ports listed per country (agent A-INFRA, V3).

Source ``atlas.nga_wpi`` (``source_key`` ``nga.wpi``): the U.S. National Geospatial-Intelligence Agency's
Maritime Safety Information portal serves the World Port Index as one CSV
(``https://msi.nga.mil/api/publications/world-port-index?output=csv``, ~1.3 MB, ~2,950 ports). The WPI is a
work of the U.S. Government (17 U.S.C. 105: no copyright in the United States); NGA publishes it for free
public download. The CSV itself carries no licence statement and no edition date (recorded as
``licence_status: us_government_work_not_stated_in_file``; the retrieval day is the period).

What the counts mean
--------------------
The WPI lists ports chosen for navigation (sailing directions); it is **not a census** of a country's ports
and its coverage is uneven (the United States and Canada are listed in far more detail than, e.g., India or
China). Every row is a derived count of WPI records (``derived: count`` with its formula), per
``countryCode`` (ISO 3166-1 alpha-2, scheme ``nga_wpi``). ``QM`` (Midway), ``QW`` (Wake) and ``XU`` (Johnston
Atoll) are parts of ISO ``UM`` (UMI): they stay unmapped (kept, listed) rather than being merged into one country.

* ``PORTS_LISTED``: records of the country;
* ``PORTS_LISTED|harbor_size=<L|M|S|V>``: by the WPI harbour size code (Large, Medium, Small, Very small).

Facility columns (e.g. ``loContainer``) are not counted: in the 2026-10-16 export 2,905 of 2,951 records say
``U`` (unknown) for container handling, so a count would read as "no container port" where the source says
"unknown".

UNCTAD's liner shipping connectivity (LSCI/LSBCI, a country-pair shipping network) is A-TRADE's
(``unctad.lsbci``); it is referenced, not duplicated, here.

KG layer (V6INFRA, version 1.1.0; source key ``nga.wpi``): every WPI record becomes a ``port`` entity
``wpi:<portNumber>`` with ``P17`` (``countryCode`` ISO alpha-2 -> crosswalk -> country item), ``P625`` (the CSV's
degrees-minutes-seconds text converted to decimal degrees, text kept in a qualifier), ``wpi:harbor_size`` /
``wpi:harbor_type`` (codes verbatim; the size labels are the WPI's L/M/S/V letters), ``wpi:unlocode`` (the
UN/LOCODE the WPI publishes for the port, verbatim) and ``wpi:at_unlocode`` -> ``unlocode:<CC><LLL>`` (the UN/LOCODE
location the WPI names; identifiers only, the two records are not merged), and ``wpi:in_adm1`` ->
``iso3166_2:<code>`` by REVA point in polygon against Natural Earth admin-1, else (offshore point of a coastal port)
the unique nearest polygon within 0.05 degrees, labelled ``nearest_polygon`` (:mod:`v6infra_common`; the WPI publishes no
subdivision).
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections import defaultdict
from typing import Any

from reva.adapters.connectors import atlas_infra_common as ic
from reva.adapters.connectors import v6infra_common as vc
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.nga_wpi"
VERSION = "1.1.0"  # 1.1.0 (V6INFRA): KG layer (ports, UN/LOCODE, ADM1); Natural Earth admin-1 fetched
SOURCE_KEY = "nga.wpi"
ATLAS_SOURCE = "nga"
DATASET = "wpi"
PARSER_VERSION = "atlas_nga_wpi/1"
URL = "https://msi.nga.mil/api/publications/world-port-index?output=csv"
MIN_PORTS = 2000  # truncation guard (the WPI lists ~2,950 ports; trimmed test fixtures lower it)
SIZES = {"L": "large", "M": "medium", "S": "small", "V": "very small"}
RIGHTS = {
    "licence": "U.S. Government work (17 U.S.C. 105), public domain in the United States",
    "licence_status": "us_government_work_not_stated_in_file",
    "attribution": "National Geospatial-Intelligence Agency, World Port Index (Pub. 150), https://msi.nga.mil",
}


KG_PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "wpi:harbor_size": ("string", "wpi_harbor_size", "WPI harbour size code", "code de taille de port WPI"),
    "wpi:harbor_type": ("string", "wpi_harbor_type", "WPI harbour type code", "code de type de port WPI"),
    "wpi:unlocode": ("external_id", "wpi_unlocode", "UN/LOCODE published by the WPI", "UN/LOCODE publié par le WPI"),
    "wpi:at_unlocode": (
        "item",
        "wpi_at_unlocode",
        "at UN/LOCODE location (code published by the WPI)",
        "au lieu UN/LOCODE (code publié par le WPI)",
    ),
    "wpi:in_adm1": (
        "item",
        "wpi_in_adm1",
        "located in first-level subdivision (point in polygon)",
        "situé dans la subdivision de premier niveau (point dans polygone)",
    ),
}
WIKIDATA_PIDS = ("P17", "P625")
UNLOCODE_RE = re.compile(r"^([A-Z]{2}) ?([A-Z0-9]{3})$")
COASTAL_DEG = 0.05  # ports: nearest admin-1 polygon within 0.05 degrees when the point is offshore (labelled)


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__], vc, vc.gh)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="U.S. National Geospatial-Intelligence Agency (NGA), Maritime Safety Information: World Port Index",
        licence=f"{RIGHTS['licence']}; attribution: {RIGHTS['attribution']}",
        allowed_hosts=("msi.nga.mil", vc.NE_HOST),
        datasets=(DATASET,),
        dataset_ids=("nga:pub150:world-port-index",),
        cadence_seconds=90 * ic.DAY,
        planned_requests=2,
        max_response_bytes=30_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="Ports listed in the WPI per country (not a census of ports); every port as a KG entity "
        "(country, coordinates, UN/LOCODE, ADM1 by point in polygon).",
    )


def build(raw: ic.Fetched) -> tuple[ic.RowBuilder, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    rows = list(csv.DictReader(io.StringIO(raw.body.decode("utf-8-sig"))))
    need = {"portNumber", "countryCode", "countryName", "harborSize"}
    if not rows or not need <= set(rows[0]):
        raise SourceResponseError("WPI CSV lacks expected columns", retryable=False, kind="schema_drift")
    if len(rows) < MIN_PORTS:
        raise SourceResponseError(f"WPI CSV has only {len(rows)} ports (truncated?)", retryable=False,
                                  kind="schema_drift")  # fmt: skip
    total: dict[str, int] = defaultdict(int)
    size: dict[tuple[str, str], int] = defaultdict(int)
    names: dict[str, str] = {}
    no_country = 0
    for r in rows:
        c = r["countryCode"].strip()
        if not c:
            no_country += 1
            continue
        names.setdefault(c, r["countryName"].strip())
        total[c] += 1
        s = r["harborSize"].strip()
        if s in SIZES:
            size[(c, s)] += 1
    period = ic.day_span(raw.retrieved_at.date())
    rb = ic.RowBuilder(ATLAS_SOURCE, DATASET)
    extra = {"coverage": "WPI lists ports for navigation; not a census of ports", "licence": "US Government work"}
    for c in sorted(total):
        iso3 = country_iso3(c, "nga_wpi")
        fl = {**extra, "provider_country_name": names[c]}

        def emit(ind: str, v: int, formula: str, where: dict[str, Any], c: str = c, iso3: Any = iso3,
                 fl: dict[str, Any] = fl) -> None:  # fmt: skip
            rb.count(indicator=ind, provider_geo=c, iso3=iso3, period=period, value=v, unit="ports",
                     raw_sha256=raw.sha256, selector={"file": "world-port-index.csv", "aggregate": "count",
                                                      "where": where}, flags=ic.derived(formula, **fl))  # fmt: skip

        emit("PORTS_LISTED", total[c], "count of WPI records with countryCode = the code", {"countryCode": c})
        for s, label in SIZES.items():
            emit(f"PORTS_LISTED|harbor_size={s}", size.get((c, s), 0),
                 f"count of WPI records with countryCode = the code and harborSize = {s} ({label})",
                 {"countryCode": c, "harborSize": s})  # fmt: skip
    report = {"ports": len(rows), "countries": len(total), "without_country": no_country,
              "unmapped": sorted(c for c in total if country_iso3(c, "nga_wpi") is None)}  # fmt: skip
    return rb, report


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    if not run.wants(DATASET):
        return {"ok": True, "tables": [], "skipped": DATASET}
    raw = await ic.fetch_archived(
        run, URL, name="world-port-index.csv", key="nga:wpi:csv",
        media_types=("text/csv", "application/octet-stream"), max_bytes=10_000_000,
    )  # fmt: skip
    rb, report = build(raw)
    out = run.register(
        dataset=DATASET,
        frame=rb.frame(),
        raw_sha256s=[raw.sha256],
        coverage_note=f"NGA World Port Index: {report['ports']} ports in {report['countries']} countries/territories "
        f"(not a census); unmapped: {', '.join(report['unmapped']) or 'none'}",
        geo_scheme="nga_wpi",
        rights={**RIGHTS, "source_key": SOURCE_KEY},
    )
    table = {
        "table": out["dataset_key"].removeprefix("atlas."),
        "rows": out["row_count"],
        "created": out["created"],
        "artifact_sha256": out["artifact_sha256"],
        "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []],
    }
    idx = await vc.fetch_ne_admin1(run)
    kb = build_kg(raw, idx, load_country_qids())
    kg = vc.load(run, kb, KG_PREDICATES, WIKIDATA_PIDS, [raw.sha256, idx.fetched.sha256], raw.retrieved_at,
                 {"connector": NAME, "parser": PARSER_VERSION})  # fmt: skip
    kg["counts"] = dict(kb.counts)
    return {"ok": True, "tables": [table], "report": report, "kg": kg}


def build_kg(raw: ic.Fetched, idx: vc.Adm1Index | None, qids: dict[str, str]) -> vc.KgBuilder:
    """One ``port`` entity per WPI record (see the module docstring)."""
    from reva.atlas.tables import country_iso3

    rows = list(csv.DictReader(io.StringIO(raw.body.decode("utf-8-sig"))))
    kb = vc.KgBuilder(SOURCE_KEY, f"wpi-{raw.sha256[:16]}")
    ref = [vc.kg_ref(raw, file="world-port-index.csv", licence=RIGHTS["licence"],
                     licence_status=RIGHTS["licence_status"])]  # fmt: skip
    adm_ref = [*ref, idx.ref()] if idx is not None else ref
    for r in rows:
        num = r["portNumber"].strip()
        if not re.match(r"^\d{1,9}$", num):
            kb.counts["skipped_bad_port_number"] += 1
            continue
        qid = f"wpi:{num}"
        name = r["portName"].strip() or num
        alt = [a.strip() for a in (r.get("alternateName") or "").split(";") if a.strip()]
        kb.entity(qid, "port", name, aliases=[*alt, f"WPI {num}"],
                  description=f"World Port Index port {num} ({r['countryName'].strip()}; WPI region "
                  f"{r['regionName'].strip()} {r['regionNumber'].strip()})")  # fmt: skip
        q0 = {"wpi_port_number": num}

        def st(pid: str, *, obj: str | None = None, value: dict[str, Any] | None = None,
               quals: dict[str, Any] | None = None, refs: list[dict[str, Any]] = ref, qid: str = qid, q0: dict[str, Any] = q0) -> None:  # fmt: skip
            kb.st(qid, pid, raw=raw.sha256, refs=refs, obj=obj, value=value, quals={**q0, **(quals or {})})

        c = r["countryCode"].strip()
        iso3 = country_iso3(c, "nga_wpi") if c else None
        cq = vc.country_qid(iso3, qids)
        lat, lon = vc.dms_to_decimal(r["latitude"]), vc.dms_to_decimal(r["longitude"])
        place = (vc.place_facility(idx, country_iso2=c or None, lat=lat, lon=lon, coastal_deg=COASTAL_DEG)
                 if lat and lon else None)  # fmt: skip
        if cq:
            pq: dict[str, Any] = {"basis": "WPI countryCode (ISO 3166-1 alpha-2, scheme nga_wpi)", "country_code": c}
            if place is not None and "coordinates_fall_in" in place.quals:
                pq["coordinates_fall_in"] = place.quals["coordinates_fall_in"]
            st("P17", obj=cq, quals=pq)
        else:
            kb.counts["country_unmapped_or_no_qid"] += 1
        if lat and lon:
            st("P625", value=vc.coordinate_value(lat, lon),
               quals={"provider_text": f"{r['latitude'].strip()} {r['longitude'].strip()}",
                      "conversion": "degrees + minutes/60 + seconds/3600 (S and W negative)"})  # fmt: skip
        else:
            kb.counts["coordinates_malformed"] += 1
        hs = r["harborSize"].strip()
        if hs:
            st("wpi:harbor_size", value={"type": "string", "value": hs}, quals={"label": SIZES.get(hs)})
        ht = r["harborType"].strip()
        if ht:
            st("wpi:harbor_type", value={"type": "string", "value": ht})
        lo = r["unloCode"].strip()
        if lo:
            st("wpi:unlocode", value={"type": "external_id", "value": lo})
            m = UNLOCODE_RE.match(lo)
            if m:
                st("wpi:at_unlocode", obj=f"unlocode:{m.group(1)}{m.group(2)}",
                   quals={"basis": "UN/LOCODE published in the WPI unloCode column", "unlocode_text": lo})  # fmt: skip
            else:
                kb.counts["unlocode_malformed"] += 1
        if place is not None:
            if place.code:
                st("wpi:in_adm1", obj=f"iso3166_2:{place.code}", quals=place.quals, refs=adm_ref)
            kb.counts[f"adm1_{place.reason}"] += 1
    return kb
