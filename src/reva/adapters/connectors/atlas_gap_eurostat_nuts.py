"""Eurostat NUTS-2 regions that are ISO 3166 entities of their own — gap fill (V3, GAPHUNT).

Five French overseas departments, Åland and Svalbard/Jan Mayen are separate ISO 3166-1 entities
(crosswalk GLP, MTQ, GUF, REU, MYT, ALA, SJM) but the World Bank, IMF, ILO, WHO … publish them only
inside France, Finland and Norway. Eurostat publishes them as NUTS-2 regions (NUTS 2021):

    FRY1 Guadeloupe -> GLP   FRY2 Martinique -> MTQ   FRY3 Guyane -> GUF   FRY4 La Réunion -> REU
    FRY5 Mayotte -> MYT      FI20 Åland -> ALA         NO0B Svalbard og Jan Mayen -> SJM

(``registry/atlas/geo_aliases/gaphunt.yaml`` scheme ``nuts_gaphunt``; the Eurostat label of each code
is in every response and checked by the parser). Seven requests to the Eurostat statistics API
(JSON-stat 2.0, GET, no key), one atlas table ``eurostat.nuts2_gapfill``:

``demo_r_d2jan``  population on 1 January (persons)              ``reg_area3``    total / land area (km2)
``demo_r_d3dens`` population density (persons per km2)            ``nama_10r_2gdp`` GDP (million EUR, EUR per inhabitant)
``lfst_r_lfu3rt`` unemployment rate, 15-74, LFS (%)               ``demo_r_mlifexp`` life expectancy at birth (years)
``isoc_r_iuse_i`` individuals who used the internet in the last 3 months / 12 months (% of individuals)

Rules: values are the JSON numbers' literal text (parsed with ``parse_float=str``), never rescaled or
converted (GDP stays in euro); Eurostat status codes (``p`` provisional, ``e`` estimated, ``b`` break …)
are kept with their labels from the response; cells without a value are not stored. Population is on
1 January (not mid-year) and the unemployment rate is for ages 15-74: their indicator says so.
Budget: the existing ``eurostat.dissemination`` key (same provider service as A-ILO's earn_mw_cur).
Licence: Eurostat reuse policy (CC BY 4.0; acknowledgement required).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from reva.adapters.connectors import atlas_gap_common as common
from reva.adapters.connectors.atlas_gap_common import Fetched, GapItem, GapTable, canon, strict_decimal
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

NAME = "atlas.gap_eurostat_nuts"
SOURCE_KEY = "eurostat.dissemination"
HOST = "ec.europa.eu"
BASE = f"https://{HOST}/eurostat/api/dissemination/statistics/1.0/data"
ATLAS_SOURCE = "eurostat"
DATASET = "nuts2_gapfill"
GEO_SCHEME = "nuts_gaphunt"
PARSER_VERSION = "atlas_gap_eurostat_nuts/1"
RIGHTS = {
    "licence": "Eurostat reuse policy (CC BY 4.0; acknowledgement required)",
    "licence_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
    "attribution": "Source: Eurostat (regional statistics, NUTS 2021)",
}
#: NUTS code -> Eurostat label expected in the response (checked: a relabelled code fails closed)
GEOS: dict[str, str] = {
    "FRY1": "Guadeloupe",
    "FRY2": "Martinique",
    "FRY3": "Guyane",
    "FRY4": "La Réunion",
    "FRY5": "Mayotte",
    "FI20": "Åland",
    "NO0B": "Svalbard og Jan Mayen",
}
#: Eurostat dataset -> fixed filters (every other dimension except geo/time must be single-valued or folded)
QUERIES: dict[str, str] = {
    "demo_r_d2jan": "sex=T&age=TOTAL&unit=NR",
    "reg_area3": "unit=KM2&landuse=TOTAL&landuse=L0008",
    "demo_r_d3dens": "unit=PER_KM2",
    "nama_10r_2gdp": "unit=MIO_EUR&unit=EUR_HAB",
    "lfst_r_lfu3rt": "sex=T&age=Y15-74&isced11=TOTAL&unit=PC",
    "demo_r_mlifexp": "sex=T&age=Y_LT1&unit=YR",
    "isoc_r_iuse_i": "unit=PC_IND&indic_is=I_IU3&indic_is=I_ILT12",
}
#: what the year of a cell means, per dataset (kept in flags.reference)
REFERENCE = {
    "demo_r_d2jan": "stock on 1 January of the year",
    "reg_area3": "area as of the year (NUTS boundaries of the release)",
    "demo_r_d3dens": "Eurostat: average population of the year / land area",
    "nama_10r_2gdp": "calendar-year flow, current prices",
    "lfst_r_lfu3rt": "annual average, Labour Force Survey, ILO definition, ages 15-74",
    "demo_r_mlifexp": "period life table of the year",
    "isoc_r_iuse_i": "ICT household survey of the year, individuals aged 16-74",
}
UNITS = {
    "NR": "persons",
    "KM2": "km2",
    "PER_KM2": "persons per km2",
    "MIO_EUR": "million EUR",
    "EUR_HAB": "EUR per inhabitant",
    "PC": "%",
    "YR": "years",
    "PC_IND": "% of individuals",
}


def url(dataset: str) -> str:
    geo = "&".join(f"geo={g}" for g in GEOS)
    return f"{BASE}/{dataset}?{QUERIES[dataset]}&{geo}&format=JSON&lang=EN"


def items(*, fixture: bool = False) -> list[GapItem]:
    return [
        GapItem(
            key=f"data:{ds}", url=url(ds), datasets=(DATASET,), media_types=("application/json",), max_bytes=5_000_000
        )
        for ds in QUERIES
    ]


def parse_jsonstat(dataset: str, f: Fetched) -> list[dict[str, Any]]:
    """One Eurostat JSON-stat 2.0 response -> atlas rows (exact value text, status flags)."""
    try:
        d = json.loads(f.body.decode("utf-8"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SourceResponseError(f"Eurostat {dataset}: not JSON", retryable=False) from e
    if "error" in d:
        raise SourceResponseError(f"Eurostat {dataset}: provider error", detail=str(d["error"])[:300], retryable=False)
    ids, size = d.get("id"), d.get("size")
    if not isinstance(ids, list) or not isinstance(size, list) or "geo" not in ids or "time" not in ids:
        raise SourceResponseError(f"Eurostat {dataset}: not a JSON-stat dataset", retryable=False)
    size = [int(s) for s in size]
    cats: list[list[str]] = []
    labels: dict[str, dict[str, str]] = {}
    for dim in ids:
        c = d["dimension"][dim]["category"]
        idx = c["index"]
        order = sorted(idx, key=lambda k: int(idx[k])) if isinstance(idx, dict) else list(idx)
        cats.append(order)
        labels[dim] = dict(c.get("label") or {})
    for g, want in GEOS.items():
        got = labels["geo"].get(g)
        if got is not None and got != want:
            raise SourceResponseError(
                f"Eurostat {dataset}: NUTS label changed", geo=g, label=str(got)[:60], retryable=False
            )
    status_labels = ((d.get("extension") or {}).get("status") or {}).get("label") or {}
    status = d.get("status") or {}
    stride = [1] * len(size)
    for i in range(len(size) - 2, -1, -1):
        stride[i] = stride[i + 1] * size[i + 1]
    folded = [dim for dim in ids if dim not in ("geo", "time", "freq", "unit")]
    rows = []
    for k, v in (d.get("value") or {}).items():
        pos = int(k)
        code = {dim: cats[i][(pos // stride[i]) % size[i]] for i, dim in enumerate(ids)}
        value = strict_decimal(v)
        if value is None:
            continue
        if code.get("freq", "A") != "A":
            raise SourceResponseError(f"Eurostat {dataset}: non-annual cell", retryable=False)
        unit_code = code.get("unit", "")
        if unit_code not in UNITS:
            raise SourceResponseError(f"Eurostat {dataset}: unexpected unit", unit=unit_code[:20], retryable=False)
        start, end, kind = common.period_of(code["time"])
        if kind != "year":
            raise SourceResponseError(f"Eurostat {dataset}: non-annual period", retryable=False)
        indicator = f"{dataset}|unit={unit_code}" + "".join(f"|{dim}={code[dim]}" for dim in folded)
        flags: dict[str, Any] = {
            "geo_label": labels["geo"].get(code["geo"], ""),
            "dataset_label": str(d.get("label", ""))[:300],
            "dataset_updated": str(d.get("updated", "")),
            "nuts_region_of": {"FR": "France", "FI": "Finland", "NO": "Norway"}[code["geo"][:2]],
            "reference": REFERENCE[dataset],
        }
        for dim in folded:
            flags[f"{dim}_label"] = labels[dim].get(code[dim], "")
        st = status.get(k)
        if st:
            flags["provider_flag"] = st
            flags["provider_flag_label"] = status_labels.get(st, "(not labelled in the response)")
            if "e" in st:
                flags["estimate_kind"] = "estimate"
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": DATASET,
                "indicator": indicator,
                "iso3": country_iso3(code["geo"], GEO_SCHEME),
                "provider_geo": code["geo"],
                "period_start": start,
                "period_end": end,
                "value": value,
                "unit": UNITS[unit_code],
                "flags": canon(flags),
                "raw_sha256": f.sha256,
                "selector": canon({"dataset": dataset, "value_index": pos, **{x: code[x] for x in ids}}),
            }
        )
    return rows


def build(got: Mapping[str, Fetched]) -> list[GapTable]:
    rows: list[dict[str, Any]] = []
    shas: list[str] = []
    per: dict[str, int] = {}
    for ds in QUERIES:
        f = got.get(f"data:{ds}")
        if f is None:
            continue
        r = parse_jsonstat(ds, f)
        per[ds] = len(r)
        rows.extend(r)
        shas.append(f.sha256)
    if not rows:
        return []
    common.dedupe_check(rows, "Eurostat NUTS-2")
    note = (
        "Eurostat statistics API (JSON-stat 2.0), NUTS-2 regions FRY1-FRY5, FI20, NO0B; cells per dataset: "
        + ", ".join(f"{k} {v}" for k, v in per.items())
        + ". Population on 1 January; GDP in euro (not converted)."
    )
    return [GapTable(DATASET, common.frame(rows), tuple(dict.fromkeys(shas)), GEO_SCHEME, note, RIGHTS)]


def _module() -> Any:
    import sys

    return sys.modules[__name__]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version="1.0.0",
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Eurostat",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']})",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(QUERIES),
        cadence_seconds=30 * 86400,
        planned_requests=len(QUERIES),
        max_response_bytes=5_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(_module()),
        field_mappings={"geo_scheme": GEO_SCHEME, "geos": GEOS},
        notes="French overseas departments, Åland, Svalbard/Jan Mayen as NUTS-2 regions.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await common.ingest_items(run, items(fixture=run.origin == "test_fixture"), build)


__all__ = [
    "DATASET",
    "GEOS",
    "NAME",
    "QUERIES",
    "atlas_ingest",
    "atlas_source",
    "build",
    "items",
    "parse_jsonstat",
    "url",
]
