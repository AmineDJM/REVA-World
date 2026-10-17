"""World Bank CCKP country climate *series*: observed monthly/annual history and CMIP6 projections (V4CLIMATE).

Extends :mod:`atlas_climate` (V3, A-GEO: 1991-2020 climatological normals only) with the time series the same
CCKP API publishes per country, past -> present -> projected future. Same provider, same licence (CC BY 4.0),
same geography scheme (``cckp``: ISO 3166 alpha-3 plus the World Bank legacy ``KSV`` -> ``XKX``), same parser
(:func:`atlas_climate.parse_response`, reused, not copied).

Source: ``https://cckpapi.worldbank.org/cckp/v1/<collection>_<type>_<vars>_<product>_<aggregation>_<period>_
<percentile>_<scenario>_<model>_<calculation>/<geocode>?_format=json``. Every request is one whole-world response
(``all_countries``); a run restricted to a few countries (run option ``geos``, used by the offline fixtures) asks for
a comma list of codes instead and says so in its coverage note.

Datasets (one atlas table each; ``tas`` = mean near-surface air temperature in °C, ``pr`` = precipitation):

* ``cru_monthly`` / ``cru_annual`` -- CRU TS 4.09 (0.5°, station-based gridded observations, UEA CRU), monthly
  1901-01..2024-12 (``pr`` in mm/month) and annual 1901..2024 (``pr`` in mm/year), CCKP national aggregates;
* ``era5_monthly`` / ``era5_annual`` -- ERA5 reanalysis (0.25°, ECMWF/Copernicus C3S), monthly 1950-01..2024-12
  and annual 1950..2024. A methodologically independent product; CRU and ERA5 are kept per source, never blended;
* ``cmip6_annual`` -- **projections** (and model history): CMIP6 multi-model ensemble (CCKP ``ensemble_all``,
  0.25° downscaled and bias-corrected), annual 2015..2100 for SSP1-2.6, SSP2-4.5, SSP3-7.0 and SSP5-8.5, and the
  ``historical`` experiment 1950..2014, each as the ensemble median, 10th and 90th percentile;
* ``cmip6_monthly_clim`` -- **projections**: 20-year monthly climatologies (ensemble median) for 2020-2039,
  2040-2059, 2060-2079 and 2080-2099 per SSP, plus the model reference climatology 1995-2014 (historical
  experiment).

Projection honesty: every CMIP6 row carries ``flags.estimate_kind`` (``projection`` for SSP rows,
``model_simulation`` for the historical experiment) **and** a truthy ``flags.projection`` text, so the atlas
(``reva.atlas.query.PROJECTION_SQL``) never answers an observation question with them; ``flags.projection_upstream``
names scenario, model ensemble, percentile, product and period. Scenarios, percentiles and periods are separate
indicators (``tas|scenario=ssp245|stat=median``), never mixed with each other or with CRU/ERA5.

Periods: the provider keys annual values ``"<year>-07"`` (mid-year label) and monthly values ``"<year>-<MM>"``; REVA
stores the calendar year ``[Y-01-01, Y+1-01-01)`` or month, keeping the provider key in the selector. A 20-year
climatology covers ``[first year-01-01, last year+1-01-01)`` with the month in the indicator (``|month=MM``).

Checks that block a run (computed on the provider's own two products, nothing derived is stored):
* annual ``pr`` = sum of the 12 monthly ``pr`` within max(1 mm, 0.5 %), and annual ``tas`` = day-weighted mean of
  the 12 monthly ``tas`` within 0.03 °C (CRU and ERA5; observed maximum 0.011 °C / 0.15 % in the 2026-10 release);
* whole-world responses must cover >= 200 geographies; the latest year must be the one the product code names.

Provider-null series (``BLM``/``BVT`` on the 0.5° CRU grid) are omitted and listed, never filled.
Subnational (ADM1) aggregates: CCKP computes them (its metadata names "World Bank shapefiles for national (ADM0)
and subnational (ADM1) units"), but the API's ADM1 geocodes are not discoverable from the API itself and the portal
pages that document them answer HTTP 403 to non-browser clients -- not worked around (docs/dev/v4_v4climate.md).
"""

from __future__ import annotations

import calendar
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from reva.adapters.connectors import atlas_climate as cl
from reva.adapters.connectors import atlas_infra_common as ic
from reva.adapters.connectors.base import dec_str
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.cckp_series"
VERSION = "1.0.0"
SOURCE_KEY = cl.SOURCE_KEY  # worldbank.cckp (same provider / licence as the V3 climatologies)
ATLAS_SOURCE = "cckp_ts"
PARSER_VERSION = "atlas-cckp-series/1"
HOST = cl.HOST
API = cl.API
GEO_SCHEME = cl.GEO_SCHEME
MAX_BYTES = 30_000_000
MIN_COUNTRIES = 200
RIGHTS = cl.RIGHTS | {
    "attribution": "World Bank Climate Change Knowledge Portal (climateknowledgeportal.worldbank.org); underlying "
    "data CRU TS 4.09 (UEA Climatic Research Unit), ERA5 (Copernicus C3S/ECMWF), CMIP6 (WCRP; CCKP downscaled and "
    "bias-corrected 0.25° ensemble)"
}

DS_CRU_M, DS_CRU_A = "cru_monthly", "cru_annual"
DS_ERA_M, DS_ERA_A = "era5_monthly", "era5_annual"
DS_PROJ_A, DS_PROJ_C = "cmip6_annual", "cmip6_monthly_clim"
DATASETS = (DS_CRU_M, DS_CRU_A, DS_ERA_M, DS_ERA_A, DS_PROJ_A, DS_PROJ_C)
VARS = ("tas", "pr")
SSPS = ("ssp126", "ssp245", "ssp370", "ssp585")
SSP_LABELS = {
    "ssp126": "SSP1-2.6",
    "ssp245": "SSP2-4.5",
    "ssp370": "SSP3-7.0",
    "ssp585": "SSP5-8.5",
    "historical": "CMIP6 historical experiment",
}
PERCENTILES = ("median", "p10", "p90")
CLIM_PERIODS = ((2020, 2039), (2040, 2059), (2060, 2079), (2080, 2099))
REF_PERIOD = (1995, 2014)
CRU_YEARS = (1901, 2024)
ERA_YEARS = (1950, 2024)
PROJ_YEARS = (2015, 2100)
HIST_YEARS = (1950, 2014)
PRODUCTS = {
    "cru": "CRU TS 4.09 (0.5°, UEA Climatic Research Unit) via CCKP",
    "era5": "ERA5 reanalysis (0.25°, ECMWF/Copernicus C3S) via CCKP",
    "cmip6": "CMIP6 multi-model ensemble (CCKP ensemble_all, 0.25° downscaled and bias-corrected) via CCKP",
}
MODEL = "CMIP6 multi-model ensemble (CCKP 'ensemble_all')"


@dataclass(frozen=True)
class Item:
    key: str
    dataset: str
    path: str
    product: str  # cru | era5 | cmip6
    aggregation: str  # monthly | annual | clim_monthly
    years: tuple[int, int]
    scenario: str | None = None
    percentile: str | None = None


def _items() -> tuple[Item, ...]:
    v = ",".join(VARS)
    out = [
        Item(
            "cru_monthly",
            DS_CRU_M,
            f"cru-x0.5_timeseries_{v}_timeseries_monthly_1901-2024_mean_historical_cru_ts4.09_mean",
            "cru",
            "monthly",
            CRU_YEARS,
        ),
        Item(
            "cru_annual",
            DS_CRU_A,
            f"cru-x0.5_timeseries_{v}_timeseries_annual_1901-2024_mean_historical_cru_ts4.09_mean",
            "cru",
            "annual",
            CRU_YEARS,
        ),
        Item(
            "era5_monthly",
            DS_ERA_M,
            f"era5-x0.25_timeseries_{v}_timeseries_monthly_1950-2024_mean_historical_era5_x0.25_mean",
            "era5",
            "monthly",
            ERA_YEARS,
        ),
        Item(
            "era5_annual",
            DS_ERA_A,
            f"era5-x0.25_timeseries_{v}_timeseries_annual_1950-2024_mean_historical_era5_x0.25_mean",
            "era5",
            "annual",
            ERA_YEARS,
        ),
    ]
    for pct in PERCENTILES:
        out.append(
            Item(
                f"cmip6_hist_{pct}",
                DS_PROJ_A,
                f"cmip6-x0.25_timeseries_{v}_timeseries_annual_1950-2014_{pct}_historical_ensemble_all_mean",
                "cmip6",
                "annual",
                HIST_YEARS,
                "historical",
                pct,
            )
        )
        for ssp in SSPS:
            out.append(
                Item(
                    f"cmip6_{ssp}_{pct}",
                    DS_PROJ_A,
                    f"cmip6-x0.25_timeseries_{v}_timeseries_annual_2015-2100_{pct}_{ssp}_ensemble_all_mean",
                    "cmip6",
                    "annual",
                    PROJ_YEARS,
                    ssp,
                    pct,
                )
            )
    a, b = REF_PERIOD
    out.append(
        Item(
            f"cmip6_clim_historical_{a}_{b}",
            DS_PROJ_C,
            f"cmip6-x0.25_climatology_{v}_climatology_monthly_{a}-{b}_median_historical_ensemble_all_mean",
            "cmip6",
            "clim_monthly",
            REF_PERIOD,
            "historical",
            "median",
        )
    )
    for ssp in SSPS:
        for a, b in CLIM_PERIODS:
            out.append(
                Item(
                    f"cmip6_clim_{ssp}_{a}_{b}",
                    DS_PROJ_C,
                    f"cmip6-x0.25_climatology_{v}_climatology_monthly_{a}-{b}_median_{ssp}_ensemble_all_mean",
                    "cmip6",
                    "clim_monthly",
                    (a, b),
                    ssp,
                    "median",
                )
            )
    return tuple(out)


ITEMS: tuple[Item, ...] = _items()
BY_KEY = {it.key: it for it in ITEMS}


class CckpSeriesError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> CckpSeriesError:
    return CckpSeriesError(msg, retryable=False, kind=kind)


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__], cl)


def geo_part(geos: Sequence[str] | None) -> str:
    if not geos:
        return "all_countries"
    bad = [g for g in geos if not (isinstance(g, str) and g.isalpha() and g.isupper() and len(g) == 3)]
    if bad:
        raise _err(f"geos option must be ISO3-style codes: {bad[:5]}", kind="invalid_option")
    return ",".join(sorted(set(geos)))


def item_url(it: Item, geos: Sequence[str] | None = None) -> str:
    return f"{API}{it.path}/{geo_part(geos)}?_format=json"


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="World Bank Climate Change Knowledge Portal",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(it.path for it in ITEMS),
        cadence_seconds=90 * ic.DAY,
        planned_requests=len(ITEMS),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "geo": "ISO3 keys of data.<var> (KSV -> XKX via geo scheme cckp)",
            "date_key": "YYYY-07 annual label / YYYY-MM month",
        },
        notes="CRU/ERA5 observed series (monthly, annual) + CMIP6 SSP projections (flagged); never blended",
    )


# --------------------------------------------------------------------------- parsing / checks
Parsed = dict[str, dict[str, dict[str, Decimal]]]  # var -> geo -> provider date key -> value


def _year_month(key: str) -> tuple[int, int]:
    y, _, m = key.partition("-")
    if not (len(y) == 4 and y.isdigit() and len(m) == 2 and m.isdigit() and 1 <= int(m) <= 12):
        raise _err(f"unexpected CCKP date key {key!r}")
    return int(y), int(m)


def check_item(it: Item, parsed: Parsed, *, whole_world: bool) -> None:
    """Coverage and date-key checks of one response (blocking)."""
    for var in VARS:
        geos = parsed.get(var) or {}
        if whole_world and len(geos) < MIN_COUNTRIES:
            raise _err(f"{it.key}/{var}: only {len(geos)} geographies (truncated?)", kind="incomplete")
        last = None
        for g, cells in geos.items():
            for k in cells:
                y, m = _year_month(k)
                if not it.years[0] <= y <= it.years[1]:
                    raise _err(f"{it.key}/{var}/{g}: key {k} outside {it.years}")
                if it.aggregation == "annual" and m != 7:
                    raise _err(f"{it.key}/{var}/{g}: annual key {k} is not the mid-year label")
                if it.aggregation == "clim_monthly" and y != it.years[0]:
                    raise _err(f"{it.key}/{var}/{g}: climatology key {k} is not labelled by the first year")
                last = max(last or y, y)
        if geos and it.aggregation in ("monthly", "annual") and last != it.years[1]:
            raise _err(f"{it.key}/{var}: last year {last} != {it.years[1]} named by the product", kind="incomplete")


def check_annual_monthly(annual: Parsed, monthly: Parsed) -> list[str]:
    """Annual pr = sum of monthly pr; annual tas = day-weighted mean of monthly tas (provider's own products)."""
    problems: list[str] = []
    for geo, cells in (annual.get("pr") or {}).items():
        m = (monthly.get("pr") or {}).get(geo) or {}
        for k, v in cells.items():
            y = k[:4]
            ms = [m.get(f"{y}-{i:02d}") for i in range(1, 13)]
            if any(x is None for x in ms):
                continue
            s = sum(ms, Decimal(0))  # type: ignore[arg-type]
            if abs(s - v) > max(Decimal(1), abs(v) * Decimal("0.005")):
                problems.append(f"pr {geo} {y}: monthly sum {s} != annual {v}")
    for geo, cells in (annual.get("tas") or {}).items():
        m = (monthly.get("tas") or {}).get(geo) or {}
        for k, v in cells.items():
            y = int(k[:4])
            ms = [m.get(f"{y}-{i:02d}") for i in range(1, 13)]
            if any(x is None for x in ms):
                continue
            days = [calendar.monthrange(y, i)[1] for i in range(1, 13)]
            mean = sum((x * d for x, d in zip(ms, days, strict=True)), Decimal(0)) / sum(days)  # type: ignore[operator]
            if abs(mean - v) > Decimal("0.03"):
                problems.append(f"tas {geo} {y}: day-weighted monthly mean {mean:.3f} != annual {v}")
    return problems


# --------------------------------------------------------------------------- rows
def _unit(var: str, aggregation: str) -> str:
    if var != "pr":
        return "°C"
    return "mm/year" if aggregation == "annual" else "mm/month"


def _flags(it: Item, var: str) -> dict[str, Any]:
    f: dict[str, Any] = {
        "product": PRODUCTS[it.product],
        "variable": cl.LABELS[var],
        "aggregation": "CCKP national aggregate of the gridded product",
    }
    if it.product == "cmip6":
        up = {
            "scenario": it.scenario,
            "scenario_label": SSP_LABELS[it.scenario or ""],
            "model": MODEL,
            "percentile": it.percentile,
            "product_code": it.path,
            "period": f"{it.years[0]}-{it.years[1]}",
        }
        f["projection_upstream"] = up
        if it.scenario == "historical":
            f["estimate_kind"] = "model_simulation"
            f["projection"] = "CMIP6 historical experiment: model simulation, not an observation"
        else:
            f["estimate_kind"] = "projection"
            f["projection"] = f"CMIP6 {SSP_LABELS[it.scenario or '']} scenario projection ({it.percentile})"
        if it.aggregation == "clim_monthly":
            f["climatology"] = f"{it.years[0]}-{it.years[1]} 20-year mean of the given month"
    else:
        f["estimate_kind"] = "observation_gridded" if it.product == "cru" else "reanalysis"
    return f


def _indicator(it: Item, var: str, month: int | None) -> str:
    if it.product != "cmip6":
        return var
    ind = f"{var}|scenario={it.scenario}|stat={it.percentile}"
    if it.aggregation == "clim_monthly":
        ind += f"|clim={it.years[0]}-{it.years[1]}|month={month:02d}"
    return ind


def _period(it: Item, key: str) -> tuple[date, date]:
    y, m = _year_month(key)
    if it.aggregation == "annual":
        return ic.year_span(y)
    if it.aggregation == "monthly":
        return ic.month_span(y, m)
    return date(it.years[0], 1, 1), date(it.years[1] + 1, 1, 1)


def rows_for(it: Item, parsed: Parsed, raw_sha256: str) -> list[dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    out: list[dict[str, Any]] = []
    for var in VARS:
        flags = ic.canon(_flags(it, var))
        unit = _unit(var, "monthly" if it.aggregation == "clim_monthly" else it.aggregation)
        for geo, cells in sorted((parsed.get(var) or {}).items()):
            iso3 = country_iso3(geo, GEO_SCHEME)
            for k, x in sorted(cells.items()):
                _, m = _year_month(k)
                p0, p1 = _period(it, k)
                out.append(
                    {
                        "source": ATLAS_SOURCE,
                        "dataset": it.dataset,
                        "indicator": _indicator(it, var, m),
                        "concept_id": None,
                        "iso3": iso3,
                        "provider_geo": geo,
                        "entity_qid": None,
                        "period_start": p0,
                        "period_end": p1,
                        "value": dec_str(x),
                        "value_text": None,
                        "unit": unit,
                        "flags": flags,
                        "raw_sha256": raw_sha256,
                        "selector": ic.canon({"item": it.key, "path": ["data", var, geo, k]}),
                    }
                )
    return out


def build(
    bodies: Mapping[str, bytes], shas: Mapping[str, str], *, whole_world: bool = True
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Parse, check and turn the fetched items into atlas rows per dataset (only the items given)."""
    parsed: dict[str, Parsed] = {}
    for key, body in bodies.items():
        it = BY_KEY[key]
        p = cl.parse_response(body, VARS)
        check_item(it, p, whole_world=whole_world)
        parsed[key] = p
    problems: list[str] = []
    for prod in ("cru", "era5"):
        a, m = parsed.get(f"{prod}_annual"), parsed.get(f"{prod}_monthly")
        if a is not None and m is not None:
            problems += [f"{prod}: {x}" for x in check_annual_monthly(a, m)]
    if problems:
        raise _err("annual/monthly inconsistency: " + "; ".join(problems[:5]), kind="inconsistent")
    out: dict[str, list[dict[str, Any]]] = {}
    for key, p in parsed.items():
        it = BY_KEY[key]
        out.setdefault(it.dataset, []).extend(rows_for(it, p, shas[key]))
    nulls = {k: cl.null_series(b) for k, b in bodies.items()}
    report = {
        "items": {k: {v: len(p.get(v) or {}) for v in VARS} for k, p in parsed.items()},
        "null_series": {k: v for k, v in nulls.items() if v},
    }
    return out, report


# --------------------------------------------------------------------------- ingest
async def atlas_ingest(run: Any) -> dict[str, Any]:
    """One whole-world GET per item (archived first), checks, one atlas table per dataset."""
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    geos = run.options.get("geos")
    if isinstance(geos, str):
        geos = [g.strip() for g in geos.split(",") if g.strip()]
    wanted = [d for d in DATASETS if run.wants(d)]
    need = [it for it in ITEMS if it.dataset in wanted]
    # the annual/monthly consistency check needs both products of an observed series
    for prod, (dm, da) in {"cru": (DS_CRU_M, DS_CRU_A), "era5": (DS_ERA_M, DS_ERA_A)}.items():
        if (dm in wanted) != (da in wanted):
            need += [BY_KEY[f"{prod}_annual"] if dm in wanted else BY_KEY[f"{prod}_monthly"]]
    bodies: dict[str, bytes] = {}
    shas: dict[str, str] = {}
    for it in need:
        f = await ic.fetch_archived(
            run, item_url(it, geos), name=it.key, key=it.key, media_types=("application/json",), max_bytes=MAX_BYTES
        )
        bodies[it.key], shas[it.key] = f.body, f.sha256
    rows, report = build(bodies, shas, whole_world=not geos)
    scope = "whole world (all_countries)" if not geos else f"RESTRICTED run: geos={','.join(sorted(set(geos)))}"
    tables = []
    for ds in wanted:
        rs = rows.get(ds) or []
        if not rs:
            raise _err(f"{ds}: no rows", kind="incomplete")
        frame = pl.DataFrame(rs, schema=ATLAS_SCHEMA)
        nulls = sorted({g for k, v in report["null_series"].items() if BY_KEY[k].dataset == ds for g in v})
        out = run.register(
            dataset=ds,
            frame=frame,
            raw_sha256s=sorted(set(frame["raw_sha256"].to_list())),
            geo_scheme=GEO_SCHEME,
            rights={**RIGHTS, "source_key": SOURCE_KEY},
            coverage_note=f"CCKP national aggregates, {scope}; KSV->XKX alias; codes outside the crosswalk kept "
            f"unmapped; provider-null series omitted: {','.join(nulls) or 'none'}"
            + ("; CMIP6 rows are projections/model simulations (flags.estimate_kind)" if ds.startswith("cmip6") else ""),
        )
        tables.append(
            {
                "table": out["dataset_key"].removeprefix("atlas."),
                "rows": out["row_count"],
                "created": out["created"],
                "artifact_sha256": out["artifact_sha256"],
            }
        )
    return {"ok": True, "scope": scope, "tables": tables, "report": report}
