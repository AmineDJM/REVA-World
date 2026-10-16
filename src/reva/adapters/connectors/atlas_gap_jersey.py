"""Statistics Jersey open data — monthly earnings of Jersey (V3, GAPHUNT).

Jersey (ISO 3166-1 ``JEY``) is in no ILOSTAT or OECD earnings table: the average wage was an absolute gap.
Statistics Jersey publishes payroll-based monthly earnings on the Government of Jersey open-data portal
(CKAN, ``https://opendata.gov.je``), licence **Open Government Licence – Jersey v1.0** (CKAN ``license_id``
``OGL-J-1.0`` of the dataset "earnings-in-jersey"). One request (GET CSV):

``earnings``  "Earnings by sector": "Monthly Earnings on a full-time equivalency basis from January 2022
              onwards", mean and median, per sector and "Overall" (pounds per month, FTE).

Cells are monthly (``[YYYY-MM-01, next month)``), values verbatim. The CSV has no geography column: the
whole file is Jersey, recorded as ``provider_geo = "Jersey"`` with ``iso3 = JEY`` and
``flags.geo_basis = "single-territory dataset"``. Nothing is annualised or converted.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from datetime import date
from typing import Any

from reva.adapters.connectors import atlas_gap_common as common
from reva.adapters.connectors.atlas_gap_common import Fetched, GapItem, GapTable, canon, strict_decimal
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.gap_jersey"
SOURCE_KEY = "statistics_jersey.opendata"
HOST = "opendata.gov.je"
ATLAS_SOURCE = "statsjersey"
PARSER_VERSION = "atlas_gap_jersey/1"
EARNINGS_URL = (
    f"https://{HOST}/dataset/8a23152e-e04a-4d43-9081-06817e8d881d/resource/"
    "47246565-a300-4d7c-b273-4eb55be82615/download/earnings_by_sector.csv"
)
RIGHTS = {
    "licence": "Open Government Licence - Jersey v1.0 (OGL-J-1.0, CKAN licence of dataset 'earnings-in-jersey')",
    "licence_url": "https://www.gov.je/ServiceManual/opendata/Pages/open-government-licence-jersey-ogl-j-v1-0.aspx",
    "attribution": "Statistics Jersey, Earnings in Jersey (opendata.gov.je)",
}
HEADER = ["Year", "Month", "Sector", "Median earnings", "Mean earnings"]
UNIT = "GBP per month (full-time equivalent)"


def items(*, fixture: bool = False) -> list[GapItem]:  # noqa: ARG001 - the fixture is the full file
    return [GapItem("data:earnings_by_sector", EARNINGS_URL, ("earnings",), ("text/csv", "application/octet-stream"), max_bytes=5_000_000)]


def parse_earnings(f: Fetched) -> list[dict[str, Any]]:
    try:
        text = f.body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SourceResponseError("Jersey earnings: not UTF-8", retryable=False) from e
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [h.strip() for h in rows[0]] != HEADER:
        raise SourceResponseError("Jersey earnings: unexpected columns", columns=rows[0][:8] if rows else [], retryable=False)
    out = []
    for n, r in enumerate(rows[1:], start=2):
        if not r or not any(x.strip() for x in r):
            continue
        if len(r) != len(HEADER):
            raise SourceResponseError("Jersey earnings: ragged row", row=n, retryable=False)
        y, m, sector = int(r[0]), int(r[1]), r[2].strip()
        if not 1 <= m <= 12 or not sector:
            raise SourceResponseError("Jersey earnings: bad period or sector", row=n, retryable=False)
        start = date(y, m, 1)
        end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        for col, stat in ((3, "median"), (4, "mean")):
            value = strict_decimal(r[col])
            if value is None:
                continue
            out.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": "earnings",
                    "indicator": f"{stat}_monthly_earnings_fte|sector={sector}",
                    "iso3": "JEY",
                    "provider_geo": "Jersey",
                    "period_start": start,
                    "period_end": end,
                    "value": value,
                    "unit": UNIT,
                    "flags": canon(
                        {
                            "basis": "monthly earnings on a full-time equivalency basis (Statistics Jersey, payroll data)",
                            "statistic": stat,
                            "geo_basis": "single-territory dataset (no geography column)",
                        }
                    ),
                    "raw_sha256": f.sha256,
                    "selector": canon({"file": "earnings_by_sector.csv", "row": n, "column": HEADER[col]}),
                }
            )
    if not out:
        raise SourceResponseError("Jersey earnings: no values", retryable=False)
    common.dedupe_check(out, "Jersey earnings")
    return out


def build(got: Mapping[str, Fetched]) -> list[GapTable]:
    f = got.get("data:earnings_by_sector")
    if f is None:
        return []
    rows = parse_earnings(f)
    note = f"Statistics Jersey 'Earnings by sector' CSV (monthly, FTE, mean and median): {len(rows)} cells."
    return [GapTable("earnings", common.frame(rows), (f.sha256,), None, note, RIGHTS)]


def _module() -> Any:
    import sys

    return sys.modules[__name__]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version="1.0.0",
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Statistics Jersey (Government of Jersey)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']})",
        allowed_hosts=(HOST,),
        datasets=("earnings",),
        dataset_ids=("earnings-in-jersey/earnings_by_sector.csv",),
        cadence_seconds=30 * 86400,
        planned_requests=1,
        max_response_bytes=5_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(_module()),
        field_mappings={"iso3": "JEY"},
        notes="Monthly FTE earnings of Jersey (mean, median) by sector.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await common.ingest_items(run, items(fixture=run.origin == "test_fixture"), build)


__all__ = ["NAME", "atlas_ingest", "atlas_source", "build", "items", "parse_earnings"]
