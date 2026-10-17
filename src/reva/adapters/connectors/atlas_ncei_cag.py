"""NOAA NCEI Climate at a Glance: U.S. agricultural-belt monthly temperature and precipitation (V7, BEAT-AGRI).

Source ``atlas.ncei_cag`` -> table ``ncei_cag.agbelt_monthly``. NCEI's "Climate at a Glance" regional time series
for the agricultural belts it defines (area-weighted averages of the nClimDiv climate-division data):
``https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/regional/time-series/<region>/<param>/1/0/<y0>-<y1>/data.csv``
— one small CSV (~5 kB) per region x parameter, every month since ``FIRST_YEAR``.

* Regions (NCEI's codes, label quoted from each file's header): 260 "Primary Corn and Soybean Belt", 261 "Corn
  Belt", 262 "Soybean Belt" (all "(Area Weighted)"). Parameters ``tavg`` (average temperature, degrees Fahrenheit)
  and ``pcp`` (precipitation, inches), as the file header states.
* Identity: ``indicator`` = ``cag:<region>:<param>``; ``provider_geo`` = ``ncei_cag:<region>`` (an NCEI region, not a
  country); period = the calendar month; value = the CSV text as a strict decimal; the header line (title, units)
  quoted in ``flags``.
* **Vintage.** One current release (nClimDiv is recomputed with every monthly release; past months can change
  slightly). Values are therefore ``final_vintage``; evaluations that use them must apply a publication-lag rule
  (BEAT-AGRI: month m usable from day 12 of m + 1; NCEI released September 2026 on 2026-10-06, the file stamp
  of ``climdiv-*-v1.0.0-20261006``) and label the input ``final_vintage_with_publication_lag``.

Licence: U.S. Government work (NOAA NCEI), public domain in the United States; NCEI asks users to cite the dataset
("NOAA National Centers for Environmental Information, Climate at a Glance: Regional Time Series").
"""

from __future__ import annotations

import html
import re
import sys
from datetime import UTC, date, datetime
from typing import Any

from reva.adapters.connectors.agri_common import canon, module_digest
from reva.adapters.connectors.vintage_common import Rows, month_bounds
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.ncei_cag"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_ncei_cag/1"
SOURCE_KEY = "noaa.ncei_cag"
ATLAS_SOURCE = "ncei_cag"
DS = "agbelt_monthly"
HOST = "www.ncei.noaa.gov"
URL = f"https://{HOST}/access/monitoring/climate-at-a-glance/regional/time-series/{{region}}/{{param}}/1/0/{{y0}}-{{y1}}/data.csv"
REGIONS = {260: "Primary Corn and Soybean Belt", 261: "Corn Belt", 262: "Soybean Belt"}
PARAMS = ("tavg", "pcp")
FIRST_YEAR = 1990
LICENCE = "U.S. Government work (NOAA NCEI Climate at a Glance): public domain in the United States"
ATTRIBUTION = "NOAA National Centers for Environmental Information, Climate at a Glance: Regional Time Series"
CSV_MEDIA = ("text/csv", "text/plain", "application/octet-stream")
_ROW = re.compile(r"^(\d{4})(\d{2}),(-?\d+(?:\.\d+)?)$")


class CagError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


def parse_csv(body: bytes, *, region: int, param: str) -> tuple[dict[str, str], list[tuple[int, int, str]]]:
    """(header {title, units}, [(year, month, value)]) of one CAG CSV; layout surprises are typed errors."""
    text = body.decode("utf-8", errors="strict").replace("\r\n", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) < 4 or not lines[0].startswith("#") or not lines[1].startswith("# Units:"):
        raise CagError(f"region {region} {param}: unexpected header {lines[:2]}")
    title = html.unescape(lines[0].lstrip("#").strip()).strip('"')
    if REGIONS[region] not in title:
        raise CagError(f"region {region}: title {title!r} does not name {REGIONS[region]!r}")
    if lines[2] != "Date,Value":
        raise CagError(f"region {region} {param}: column line {lines[2]!r}")
    out = []
    for ln in lines[3:]:
        m = _ROW.match(ln)
        if not m:
            raise CagError(f"region {region} {param}: row {ln[:40]!r}")
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            raise CagError(f"region {region} {param}: month {mo}")
        if m.group(3) in ("-99.9", "-9999", "-99.99"):
            continue  # missing-value codes: never a row
        out.append((y, mo, m.group(3)))
    return {"title": title, "units": lines[1].split(":", 1)[1].strip()}, out


def rights() -> dict[str, Any]:
    return {
        "licence": "Public domain (U.S. Government work, NOAA)",
        "licence_url": "https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/",
        "attribution": ATTRIBUTION,
        "provider": "NOAA National Centers for Environmental Information",
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


def code_digest() -> str:
    return module_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="NOAA NCEI Climate at a Glance (regional agricultural-belt time series, nClimDiv)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DS,),
        dataset_ids=("climate-at-a-glance-regional",),
        cadence_seconds=30 * 86_400,
        planned_requests=len(REGIONS) * len(PARAMS),
        max_response_bytes=1_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"indicator": "cag:<region>:<param>", "geo": "ncei_cag:<region>"},
        notes="6 small CSVs (corn/soybean belts x tavg/pcp), monthly since 1990; final vintage (nClimDiv).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DS):
        return {"tables": {}}
    y1 = int(run.options.get("last_year") or datetime.now(UTC).year)
    rows = Rows()
    shas: list[str] = []
    files: list[dict[str, Any]] = []
    for region in sorted(REGIONS):
        for param in PARAMS:
            url = URL.format(region=region, param=param, y0=FIRST_YEAR, y1=y1)
            raw = await run.get(url, key=f"cag:{region}:{param}", media_types=CSV_MEDIA, params={"region": region})
            sha = run.archive(raw)
            shas.append(sha)
            head, vals = parse_csv(raw.body, region=region, param=param)
            for y, mo, v in vals:
                s, e = month_bounds(y, mo)
                rows.add(
                    source=ATLAS_SOURCE,
                    dataset=DS,
                    indicator=f"cag:{region}:{param}",
                    provider_geo=f"ncei_cag:{region}",
                    start=s,
                    end=e,
                    value=v,
                    unit=head["units"],
                    flags=canon(
                        {
                            "title": head["title"],
                            "units": head["units"],
                            "region_code": region,
                            "parameter": param,
                            "estimate_kind": "final_vintage",
                            "vintage_note": "nClimDiv current release; past months may be revised slightly",
                            "licence": "public domain (U.S. Government work)",
                        }
                    ),
                    raw_sha256=sha,
                    selector={"region": region, "param": param, "date": f"{y}{mo:02d}"},
                )
            files.append(
                {
                    "region": region,
                    "param": param,
                    "rows": len(vals),
                    "last": f"{vals[-1][0]}-{vals[-1][1]:02d}" if vals else None,
                }
            )
    frame = rows.frame()
    if frame.height == 0:
        raise CagError("no CAG row", kind="no_data")
    note = (
        f"NOAA NCEI Climate at a Glance regional series {sorted(REGIONS)} x {list(PARAMS)}, monthly {FIRST_YEAR}-{y1}; "
        f"current nClimDiv release (final vintage). Cite: {ATTRIBUTION}."
    )
    reg = run.register(dataset=DS, frame=frame, raw_sha256s=shas, coverage_note=note, rights=rights())
    return {"tables": {DS: {"dataset_key": reg["dataset_key"], "rows": reg["row_count"]}}, "files": files,
            "as_of": date.today().isoformat()}  # fmt: skip


__all__ = ["NAME", "REGIONS", "CagError", "atlas_ingest", "atlas_source", "parse_csv"]
