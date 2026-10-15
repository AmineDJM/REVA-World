"""UN WPP 2024 dataset inputs -> :class:`reva.models.population.cohort.CountryDemography` (V3 SIMWORLD).

Pure (no database, network or clock). The tables are the dataset inputs of the reviewed
connector ``un.wpp`` (schema ``reva-wpp-demography-table/1``,
:mod:`reva.adapters.connectors.un_wpp_demography`): 1 January population and calendar-year
deaths by single age 0..99 and 100+ and sex; births by mother's five-year age group
(10-14 .. 50-54, sex ``T``) and births by newborn sex (``M``/``F`` ``TOTAL`` cells, derived from
WPP total births and the WPP sex ratio at birth). The age codes and cell kinds are the
ones :mod:`reva.models.population.cohort` already reads, so the projection numerics are
exactly the 1.0.0 numerics; only the data differ.

What differs from the Eurostat path (stated in every run):

* WPP estimates are model-based reconstructions, not registered events: residual
  "migration" against them is the migration implied by WPP's own reconstruction
  (plus the difference between WPP's single-year and REVA's base-period survival).
* Fertility rates are estimated by five-year mother's age groups (the rate is applied to
  the group's female exposure, i.e. uniform within the group), not single ages.
* There are no status flags, so no series-break exclusion can occur.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from reva.models.population import cohort as C

DATASETS = {
    "population": "wpp2024_pop_jan1",
    "deaths": "wpp2024_deaths",
    "births": "wpp2024_births",
}
CONCEPTS = {
    "population": "demography.wpp.population.jan1_age_sex",
    "deaths": "demography.wpp.deaths.age_sex",
    "births": "demography.wpp.live_births.mother_age5_newborn_sex",
    "wpp_indicators": "demography.wpp.indicators_summary",
}
FIRST_YEAR = 1990
LAST_POPULATION_YEAR = 2023  # last 1 January population estimate kept (jump-off <= 2023)
LAST_EVENT_YEAR = 2023  # last calendar year of deaths/births estimates kept
DATA_LABEL = (
    "UN World Population Prospects 2024 estimates (model-based reconstructions by the UN Population "
    "Division, not registered events); fertility by five-year mother's age groups; births by sex derived "
    "from WPP total births and the WPP sex ratio at birth"
)
#: WPP 2024 countries/areas (crosswalk keys) present in the 1 January single-age file (237).
COUNTRIES: tuple[str, ...] = (
    "ABW", "AFG", "AGO", "AIA", "ALB", "AND", "ARE", "ARG", "ARM", "ASM", "ATG", "AUS", "AUT", "AZE", "BDI",
    "BEL", "BEN", "BES", "BFA", "BGD", "BGR", "BHR", "BHS", "BIH", "BLM", "BLR", "BLZ", "BMU", "BOL", "BRA",
    "BRB", "BRN", "BTN", "BWA", "CAF", "CAN", "CHE", "CHL", "CHN", "CIV", "CMR", "COD", "COG", "COK", "COL",
    "COM", "CPV", "CRI", "CUB", "CUW", "CYM", "CYP", "CZE", "DEU", "DJI", "DMA", "DNK", "DOM", "DZA", "ECU",
    "EGY", "ERI", "ESH", "ESP", "EST", "ETH", "FIN", "FJI", "FLK", "FRA", "FRO", "FSM", "GAB", "GBR", "GEO",
    "GGY", "GHA", "GIB", "GIN", "GLP", "GMB", "GNB", "GNQ", "GRC", "GRD", "GRL", "GTM", "GUF", "GUM", "GUY",
    "HKG", "HND", "HRV", "HTI", "HUN", "IDN", "IMN", "IND", "IRL", "IRN", "IRQ", "ISL", "ISR", "ITA", "JAM",
    "JEY", "JOR", "JPN", "KAZ", "KEN", "KGZ", "KHM", "KIR", "KNA", "KOR", "KWT", "LAO", "LBN", "LBR", "LBY",
    "LCA", "LIE", "LKA", "LSO", "LTU", "LUX", "LVA", "MAC", "MAF", "MAR", "MCO", "MDA", "MDG", "MDV", "MEX",
    "MHL", "MKD", "MLI", "MLT", "MMR", "MNE", "MNG", "MNP", "MOZ", "MRT", "MSR", "MTQ", "MUS", "MWI", "MYS",
    "MYT", "NAM", "NCL", "NER", "NGA", "NIC", "NIU", "NLD", "NOR", "NPL", "NRU", "NZL", "OMN", "PAK", "PAN",
    "PER", "PHL", "PLW", "PNG", "POL", "PRI", "PRK", "PRT", "PRY", "PSE", "PYF", "QAT", "REU", "ROU", "RUS",
    "RWA", "SAU", "SDN", "SEN", "SGP", "SHN", "SLB", "SLE", "SLV", "SMR", "SOM", "SPM", "SRB", "SSD", "STP",
    "SUR", "SVK", "SVN", "SWE", "SWZ", "SXM", "SYC", "SYR", "TCA", "TCD", "TGO", "THA", "TJK", "TKL", "TKM",
    "TLS", "TON", "TTO", "TUN", "TUR", "TUV", "TWN", "TZA", "UGA", "UKR", "URY", "USA", "UZB", "VAT", "VCT",
    "VEN", "VGB", "VIR", "VNM", "VUT", "WLF", "WSM", "XKX", "YEM", "ZAF", "ZMB", "ZWE",
)  # fmt: skip


def country_demography(
    iso3: str,
    *,
    population: Any,
    deaths: Any,
    births: Any,
    provenance: Mapping[str, Any] | None = None,
) -> C.CountryDemography:
    """Build the cohort model's country data from the three WPP dataset-input tables."""
    return C.CountryDemography(
        iso3,
        C._table(C._rows(population), DATASETS["population"]),
        C._table(C._rows(deaths), DATASETS["deaths"]),
        C._table(C._rows(births), DATASETS["births"]),
        provenance=dict(provenance or {}),
    )


def indicator_series(table: Any, indicator: str) -> dict[int, Decimal]:
    """``{year: value}`` of one WPP summary indicator (persons for counts; None values left out)."""
    out: dict[int, Decimal] = {}
    for r in C._rows(table):
        if r.get("age") == indicator and r.get("value") is not None:
            out[int(r["year"])] = Decimal(str(r["value"]))
    return out


def migration_check(rates: C.BaseRates, years: Iterable[int], indicators: Any) -> dict[str, Any] | None:
    """Diagnostic (never used to fit anything): REVA's residual net migration (both sexes) per year
    next to WPP's own net migration estimate for the same calendar years."""
    wpp = indicator_series(indicators, "NetMigrations")
    rows = {}
    for y in years:
        if y not in rates.residual_migration:
            continue
        mine = rates.residual_total(y)
        theirs = wpp.get(y)
        rows[str(y)] = {
            "reva_residual_persons": C.dstr(C._CTX.quantize(mine, Decimal(1))),
            "wpp_net_migrations_persons": C.dstr(theirs) if theirs is not None else None,
        }
    if not rows:
        return None
    return {
        "definition": (
            "REVA residual (net migration plus statistical adjustment, base-period survival) vs WPP 2024 "
            "NetMigrations (Demographic Indicators) for the same calendar years; a consistency diagnostic only"
        ),
        "years": rows,
    }
