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
``countryCode`` (ISO 3166-1 alpha-2, except ``QM`` Midway, ``QW`` Wake, ``XU`` Johnston Atoll -> ``UMI`` via
scheme ``nga_wpi`` of ``registry/atlas/geo_aliases/a_infra.yaml``):

* ``PORTS_LISTED``: records of the country;
* ``PORTS_LISTED|harbor_size=<L|M|S|V>``: by the WPI harbour size code (Large, Medium, Small, Very small);
* ``PORTS_LISTED|container_facilities=Y``: records with ``loContainer`` = ``Y``.

UNCTAD's liner shipping connectivity (LSCI/LSBCI, a country-pair shipping network) is A-TRADE's
(``unctad.lsbci``); it is referenced, not duplicated, here.
"""

from __future__ import annotations

import csv
import io
import sys
from collections import defaultdict
from typing import Any

from reva.adapters.connectors import atlas_infra_common as ic
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.nga_wpi"
VERSION = "1.0.0"
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


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="U.S. National Geospatial-Intelligence Agency (NGA), Maritime Safety Information: World Port Index",
        licence=f"{RIGHTS['licence']}; attribution: {RIGHTS['attribution']}",
        allowed_hosts=("msi.nga.mil",),
        datasets=(DATASET,),
        dataset_ids=("nga:pub150:world-port-index",),
        cadence_seconds=90 * ic.DAY,
        planned_requests=1,
        max_response_bytes=10_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="Ports listed in the WPI per country (not a census of ports).",
    )


def build(raw: ic.Fetched) -> tuple[ic.RowBuilder, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    rows = list(csv.DictReader(io.StringIO(raw.body.decode("utf-8-sig"))))
    need = {"portNumber", "countryCode", "countryName", "harborSize", "loContainer"}
    if not rows or not need <= set(rows[0]):
        raise SourceResponseError("WPI CSV lacks expected columns", retryable=False, kind="schema_drift")
    if len(rows) < MIN_PORTS:
        raise SourceResponseError(f"WPI CSV has only {len(rows)} ports (truncated?)", retryable=False,
                                  kind="schema_drift")  # fmt: skip
    total: dict[str, int] = defaultdict(int)
    size: dict[tuple[str, str], int] = defaultdict(int)
    cont: dict[str, int] = defaultdict(int)
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
        if r["loContainer"].strip() == "Y":
            cont[c] += 1
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
        emit("PORTS_LISTED|container_facilities=Y", cont.get(c, 0),
             "count of WPI records with countryCode = the code and loContainer = Y", {"countryCode": c, "loContainer": "Y"})  # fmt: skip
    report = {"ports": len(rows), "countries": len(total), "without_country": no_country,
              "unmapped": sorted(c for c in total if country_iso3(c, "nga_wpi") is None)}  # fmt: skip
    return rb, report


async def atlas_ingest(run: Any) -> dict[str, Any]:
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
    return {"ok": True, "tables": [table], "report": report}
