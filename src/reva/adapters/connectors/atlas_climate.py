"""World Bank Climate Change Knowledge Portal (CCKP) country climatologies 1991-2020 (V3, agent A-GEO).

Source: CCKP API ``https://cckpapi.worldbank.org/cckp/v1/<collection>_<vars>_<product>/all_countries``
(World Bank; CCKP data licence CC BY 4.0). Three requests per run, each one whole-world response:

* ``cru_annual``  — CRU TS 4.09 (0.5°) 1991-2020 climatology, annual: ``tas`` (mean near-surface air
  temperature, °C) and ``pr`` (precipitation, mm per year);
* ``cru_monthly`` — CRU TS 4.09 1991-2020 climatology per calendar month: ``tas``, ``tasmax``, ``tasmin``
  (°C) and ``pr`` (mm per month);
* ``era5_annual`` — ERA5 (0.25° reanalysis) 1991-2020 climatology, annual ``tas`` and ``pr``: a second,
  methodologically independent product (observations-based gridded CRU vs reanalysis). Both are kept,
  per source; nothing is blended.

The values are CCKP's national aggregates of the gridded products (spatial aggregation as documented by
CCKP); the period of every cell is the climatological normal ``[1991-01-01, 2021-01-01)``. The provider's
date key (``"1991-07"`` for the annual climatology, ``"1991-01".."1991-12"`` for months) is kept in the
selector. Consistency check (blocks the run): the 12 monthly precipitation totals must add up to the annual
value within 1 mm (or 0.5 %) for every country present in both responses.

Geography: CCKP keys are ISO 3166 alpha-3 plus ``KSV`` (Kosovo, World Bank legacy code; mapped to the
crosswalk key ``XKX`` through ``registry/atlas/geo_aliases/a_geo.yaml``) and ``TWN`` (kept unmapped). CCKP
has no aggregate for ATA, ESH, FLK, SGS and CHI.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from reva.adapters.connectors.atlas_geo_common import atlas_spec, fetch_raw
from reva.adapters.connectors.base import dec_str, json_decimal, module_digest, schema_fingerprint
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchOutcome,
    FetchPlan,
    Finding,
    ObservationBatch,
    QualityReport,
    RawAcquisition,
)

CONNECTOR_NAME = "atlas.cckp_climatology"
CONNECTOR_VERSION = "1.0.0"
SOURCE_KEY = "worldbank.cckp"
PARSER_VERSION = "atlas-cckp-climatology/1"
ATLAS_SOURCE = "cckp"
DS_CRU = "cru_ts4_09_clim_1991_2020"
DS_ERA5 = "era5_clim_1991_2020"
HOST = "cckpapi.worldbank.org"
API = f"https://{HOST}/cckp/v1/"
GEO_SCHEME = "cckp"
MAX_BYTES = 2_000_000
MIN_COUNTRIES = 200
PERIOD = (date(1991, 1, 1), date(2021, 1, 1))

CRU_VERSION = "cru_ts4.09"
ITEMS: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    # key: (dataset, collection product path, variables, aggregation)
    "cru_annual": (
        DS_CRU,
        f"cru-x0.5_climatology_{{v}}_climatology_annual_1991-2020_mean_historical_{CRU_VERSION}_mean",
        ("tas", "pr"),
        "annual",
    ),
    "cru_monthly": (
        DS_CRU,
        f"cru-x0.5_climatology_{{v}}_climatology_monthly_1991-2020_mean_historical_{CRU_VERSION}_mean",
        ("tas", "pr", "tasmax", "tasmin"),
        "monthly",
    ),
    "era5_annual": (
        DS_ERA5,
        "era5-x0.25_climatology_{v}_climatology_annual_1991-2020_mean_historical_era5_x0.25_mean",
        ("tas", "pr"),
        "annual",
    ),
}
PRODUCTS = {
    DS_CRU: f"CRU TS {CRU_VERSION[-4:]} (0.5°, University of East Anglia CRU) via CCKP",
    DS_ERA5: "ERA5 reanalysis (0.25°, ECMWF/Copernicus C3S) via CCKP",
}
UNITS = {"tas": "°C", "tasmax": "°C", "tasmin": "°C"}
LABELS = {
    "tas": "mean near-surface air temperature",
    "tasmax": "mean daily maximum near-surface air temperature",
    "tasmin": "mean daily minimum near-surface air temperature",
    "pr": "precipitation",
}

RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "World Bank Climate Change Knowledge Portal (climateknowledgeportal.worldbank.org); "
    "underlying data CRU TS (UEA Climatic Research Unit) and ERA5 (Copernicus C3S/ECMWF)",
}


class CckpError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> CckpError:
    return CckpError(msg, retryable=False, kind=kind)


def item_url(key: str) -> str:
    _, path, variables, _ = ITEMS[key]
    return f"{API}{path.format(v=','.join(variables))}/all_countries?_format=json"


def fetch_items() -> tuple[FetchItem, ...]:
    return tuple(FetchItem(key=k, url=item_url(k), max_bytes=MAX_BYTES) for k in ITEMS)


def parse_response(body: bytes, variables: Sequence[str]) -> dict[str, dict[str, dict[str, Decimal]]]:
    """``{variable: {geo: {date_key: Decimal}}}``; schema drift and non-numeric cells fail."""
    try:
        doc = json_decimal(body)
    except ValueError as e:
        raise _err("CCKP response is not JSON") from e
    if not isinstance(doc, dict) or (doc.get("metadata") or {}).get("status") != "success":
        raise _err(f"CCKP status is not success: {str(doc.get('metadata') if isinstance(doc, dict) else doc)[:200]}")
    data = doc.get("data")
    if not isinstance(data, dict):
        raise _err("CCKP data is not an object (empty or unknown product)", kind="incomplete")
    if len(variables) == 1 and variables[0] not in data:
        data = {variables[0]: data}
    out: dict[str, dict[str, dict[str, Decimal]]] = {}
    for v in variables:
        per = data.get(v)
        if not isinstance(per, dict):
            raise _err(f"variable {v} missing from the response", kind="incomplete")
        out[v] = {}
        for geo, series in per.items():
            if series is None:
                continue  # provider lists the country with a null series (e.g. BLM/BVT on the 0.5° CRU grid)
            if not isinstance(series, dict):
                raise _err(f"{v}/{geo}: series is not an object")
            cells: dict[str, Decimal] = {}
            for k, x in series.items():
                if x is None:
                    continue
                if not isinstance(x, Decimal):
                    raise _err(f"{v}/{geo}/{k}: non-numeric value {x!r}")
                cells[str(k)] = x
            out[v][str(geo)] = cells
    return out


def null_series(body: bytes) -> list[str]:
    """Countries the provider lists with a null series (reported in the coverage note, never filled)."""
    data = (json.loads(body) or {}).get("data") or {}
    return sorted({g for per in data.values() if isinstance(per, dict) for g, x in per.items() if x is None})


def check_consistency(
    annual: Mapping[str, Mapping[str, Mapping[str, Decimal]]],
    monthly: Mapping[str, Mapping[str, Mapping[str, Decimal]]],
) -> list[str]:
    """Monthly precipitation totals must sum to the annual value (CCKP's own two products)."""
    problems = []
    for geo, a in annual.get("pr", {}).items():
        m = monthly.get("pr", {}).get(geo)
        if not a or not m or len(m) != 12:
            continue
        av = next(iter(a.values()))
        s = sum(m.values(), Decimal(0))
        if abs(s - av) > max(Decimal(1), abs(av) * Decimal("0.005")):
            problems.append(f"{geo}: monthly pr sum {s} != annual {av}")
    return problems


def _indicator(var: str, aggregation: str, date_key: str) -> tuple[str, str]:
    if aggregation == "annual":
        return (f"{var}|period=annual", "mm/year" if var == "pr" else UNITS[var])
    month = date_key.split("-")[-1]
    if not (len(month) == 2 and month.isdigit() and 1 <= int(month) <= 12):
        raise _err(f"unexpected monthly key {date_key!r}")
    return (f"{var}|month={month}", "mm/month" if var == "pr" else UNITS[var])


def atlas_rows(
    parsed: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Decimal]]]], raw_sha256s: Mapping[str, str]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {DS_CRU: [], DS_ERA5: []}
    for key, data in parsed.items():
        ds, _, variables, aggregation = ITEMS[key]
        for var in variables:
            for geo, cells in sorted(data[var].items()):
                if aggregation == "annual" and len(cells) != 1:
                    raise _err(f"{key}/{var}/{geo}: expected one annual climatology value, got {len(cells)}")
                for dk, x in sorted(cells.items()):
                    ind, unit = _indicator(var, aggregation, dk)
                    out[ds].append(
                        {
                            "source": ATLAS_SOURCE,
                            "dataset": ds,
                            "indicator": ind,
                            "concept_id": None,
                            "iso3": None,
                            "provider_geo": geo,
                            "entity_qid": None,
                            "period_start": PERIOD[0],
                            "period_end": PERIOD[1],
                            "value": dec_str(x),
                            "value_text": None,
                            "unit": unit,
                            "flags": json.dumps(
                                {
                                    "climatology": "1991-2020 mean",
                                    "product": PRODUCTS[ds],
                                    "variable": LABELS[var],
                                    "aggregation": "CCKP national aggregate of the gridded product",
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            "raw_sha256": raw_sha256s[key],
                            "selector": json.dumps(
                                {"item": key, "path": ["data", var, geo, dk]}, sort_keys=True, separators=(",", ":")
                            ),
                        }
                    )
    return out


class CckpClimatologyConnector:
    def describe(self) -> ConnectorSpec:
        return atlas_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="World Bank Climate Change Knowledge Portal",
            licence=RIGHTS["licence"],
            allowed_hosts=(HOST,),
            dataset_ids=(DS_CRU, DS_ERA5),
            field_mappings={"geo": "ISO3 keys of data.<var>", "date_key": "climatology label"},
            units={"tas": "°C", "tasmax": "°C", "tasmin": "°C", "pr": "mm/year or mm/month"},
            geography=("world",),
            time_semantics="climatological normal 1991-2020 (half-open 1991-01-01..2021-01-01)",
            lineage_family="worldbank_cckp",
            cadence_seconds=180 * 86400,
            revision_strategy="product version in the URL (cru_ts4.09); identical bytes are a no-op",
            max_response_bytes=MAX_BYTES,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(CckpClimatologyConnector),
        )

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        return FetchPlan(connector=CONNECTOR_NAME, items=fetch_items(), pagination="one all_countries response")

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch_raw(item, http)

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        _, _, variables, _ = ITEMS[raw.item.key]
        data = parse_response(raw.body, variables)
        counts = {v: len(d) for v, d in data.items()}
        yield ObservationBatch(
            connector=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION,
            item_key=raw.item.key,
            observations=(),
            metadata={"countries": counts},
            schema_fingerprint=schema_fingerprint({v: "geo->date->number" for v in variables}),
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        counts = batch.metadata.get("countries") or {}
        low = {v: n for v, n in counts.items() if n < MIN_COUNTRIES}
        findings = tuple(
            Finding(code="incomplete", severity="blocker", message=f"{v}: only {n} countries") for v, n in low.items()
        )
        return QualityReport(
            outcome="blocked" if findings else "pass", findings=findings, received_count=min(counts.values(), default=0)
        )

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        return Cursor(state={"complete": all(o.status in ("published", "unchanged") for o in outcomes)})


def build(raws: Mapping[str, RawAcquisition], raw_sha256s: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    conn = CckpClimatologyConnector()
    parsed = {}
    for k in ITEMS:
        for b in conn.parse(raws[k]):
            q = conn.validate(b)
            if q.blocking:
                raise _err(f"{k}: " + "; ".join(f.message for f in q.findings), kind="incomplete")
        parsed[k] = parse_response(raws[k].body, ITEMS[k][2])
    problems = check_consistency(parsed["cru_annual"], parsed["cru_monthly"])
    if problems:
        raise _err("annual/monthly inconsistency: " + "; ".join(problems[:5]), kind="inconsistent")
    return atlas_rows(parsed, raw_sha256s)


async def ingest(engine: Any, store: Any, http: Any) -> dict[str, Any]:
    import polars as pl

    from reva.adapters.connectors.atlas_geo_common import archive_raw
    from reva.atlas.tables import ATLAS_SCHEMA, register_atlas_table

    conn = CckpClimatologyConnector()
    spec = conn.describe()
    raws = {it.key: await conn.fetch(it, http) for it in fetch_items()}
    shas = {k: archive_raw(engine, store, spec, r, actor="worker:atlas_a_geo") for k, r in raws.items()}
    rows = build(raws, shas)
    origin = raws["cru_annual"].origin
    nulls = {
        ITEMS[k][0]: sorted({g for kk in ITEMS if ITEMS[kk][0] == ITEMS[k][0] for g in null_series(raws[kk].body)})
        for k in ITEMS
    }
    tables = {}
    for ds, rs in rows.items():
        frame = pl.DataFrame(rs, schema=ATLAS_SCHEMA)
        tables[ds] = register_atlas_table(
            engine,
            store,
            source=ATLAS_SOURCE,
            dataset=ds,
            frame=frame,
            raw_sha256s=sorted(set(frame["raw_sha256"].to_list())),
            parser_version=PARSER_VERSION,
            origin=origin,
            geo_scheme=GEO_SCHEME,
            rights=RIGHTS,
            actor="worker:atlas_a_geo",
            coverage_note=f"{PRODUCTS[ds]}: 1991-2020 climatology national aggregates; KSV->XKX alias; "
            "TWN kept unmapped; no CCKP aggregate for ATA/ESH/FLK/SGS/CHI; null series omitted: "
            + (",".join(nulls[ds]) or "none"),
        )
    return {"connector": CONNECTOR_NAME, "raw_sha256s": shas, "tables": tables}
