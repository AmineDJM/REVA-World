"""WHO Global Health Observatory -> atlas table ``who.gho`` (agent A-HEALTHEDU, V3 atlas).

Source
------
WHO Global Health Observatory (GHO) OData API, ``https://ghoapi.azureedge.net/api`` (documented at
https://www.who.int/data/gho/info/gho-odata-api; checked 2026-10-15: still the documented feed
root, still serving current data — e.g. life expectancy rows dated 2026-10-01). WHO's legacy page
(https://www.who.int/data/gho/legacy) announced that this OData implementation will be replaced
by a new one with standardised field names "near the end of 2025"; no replacement URL was
published as of 2026-10-15. A field rename is caught as schema drift (the item is quarantined and
nothing is registered), never guessed.

Licence: WHO datasets are provided under **CC BY 4.0** with WHO's additional terms
(https://data.who.int/redirect-pages/footer/learn-more/terms-and-conditions): attribution per
dataset metadata, no misrepresentation, no re-identification. Recorded in :data:`RIGHTS`.

Requests (one run): the indicator list (names; a planned code that disappeared or was archived
blocks registration) and one request per indicator — ``$filter=SpatialDimType eq 'COUNTRY'``
(country rows only; WHO regions/income groups/global aggregates are not requested) with a fixed
``$select`` of the columns parsed. The API returns every row of an indicator in one response (no
paging). ~300 MB per full run; budget source key ``who.gho``.

What a cell is
--------------
* ``indicator`` = GHO code with every non-null dimension folded in, in the API's order
  (``Dim1``, ``Dim2``, ``Dim3``, ``DataSourceDim``) as ``<type lower-case>=<code>``:
  ``WHOSIS_000001|sex=SEX_BTSX``, ``NCDMORT3070|sex=SEX_FMLE|agegroup=AGEGROUP_YEARS30-69``.
  Sex and age are therefore always explicit (``SEX_BTSX`` = both sexes).
* ``value`` = ``NumericValue`` exactly as served (JSON decimal text, trailing zeros removed);
  ``flags.display_value`` = WHO's rounded display string (``"48.1 [45.4-50.8]"``); ``flags.low`` /
  ``flags.high`` = the uncertainty interval when published (WHO does not state its level per row;
  for GHE/IGME/MMEIG it is the 95 % uncertainty interval per their methods — recorded as
  ``interval_level: "not_stated_per_row"``).
* A row with ``NumericValue`` null but a ``Value`` string keeps the string in ``value_text`` and
  ``flags.numeric_value_absent: true`` (e.g. country-reported MMR); it is never parsed into a number.
* ``flags.estimate_class`` says what kind of number it is (catalog classification by this agent
  from WHO's indicator metadata, recorded as ``classified_by``):
  ``who_comparable_estimate`` (WHO or inter-agency modelled estimates made comparable across
  countries — GHE, UN IGME, MMEIG, NCD-RisC, WUENIC, JME, JMP, UNAIDS, WHO TB/malaria),
  ``country_reported`` (national data compiled by WHO: NHWA workforce, hospital beds, skilled birth
  attendance, country-reported MMR), ``country_reported_with_who_estimates`` (GHED health accounts:
  country-reported where available, WHO-estimated otherwise; not separable per row in the API).
  ``flags.producer`` names the estimation group.
* ``flags.comments`` = WHO's row comment verbatim (sources, caveats such as "The UN IGME estimates
  are not the official statistics of India…"); ``flags.projection: true`` only when that comment
  says "This is a projection" (tobacco trend estimates run to 2030).
* ``flags.provider_updated_at`` = the row's ``Date`` (WHO's last update of the row).
* Period: ``TimeDimType == "YEAR"`` → the calendar year. Any other time type is drift (refused).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from reva.adapters.connectors.atlas_healthedu_common import canon, fetch_archive_register, fit_flags
from reva.adapters.connectors.base import dec_str, json_decimal, module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.who_gho"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-who-gho-parse-1"
SOURCE_KEY = "who.gho"
HOST = "ghoapi.azureedge.net"
BASE = f"https://{HOST}/api"
MAX_ITEM_BYTES = 104_857_600  # the largest response (NCDMORTPERCENT, all rows) was ~38 MB on 2026-10-15

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0 (WHO additional terms)",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "terms_url": "https://data.who.int/redirect-pages/footer/learn-more/terms-and-conditions",
    "attribution": "World Health Organization, Global Health Observatory (GHO) data",
    "source_key": SOURCE_KEY,
}

#: Columns requested with ``$select`` (and required in every row).
SELECT: tuple[str, ...] = (
    "Id",
    "IndicatorCode",
    "SpatialDimType",
    "SpatialDim",
    "TimeDimType",
    "TimeDim",
    "Dim1Type",
    "Dim1",
    "Dim2Type",
    "Dim2",
    "Dim3Type",
    "Dim3",
    "DataSourceDimType",
    "DataSourceDim",
    "Value",
    "NumericValue",
    "Low",
    "High",
    "Comments",
    "Date",
)
DIM_FIELDS: tuple[tuple[str, str], ...] = (
    ("Dim1Type", "Dim1"),
    ("Dim2Type", "Dim2"),
    ("Dim3Type", "Dim3"),
    ("DataSourceDimType", "DataSourceDim"),
)

COMPARABLE = "who_comparable_estimate"
COUNTRY = "country_reported"
MIXED = "country_reported_with_who_estimates"
CLASSIFIED_BY = "A-HEALTHEDU catalog, from WHO GHO indicator metadata"


@dataclass(frozen=True)
class WhoIndicator:
    code: str
    unit: str
    estimate_class: str
    producer: str
    note: str = ""


#: Curated headline indicators (all countries). Units as WHO declares them in the indicator names.
INDICATORS: tuple[WhoIndicator, ...] = (
    # life expectancy and mortality (WHO Global Health Estimates, UN IGME, MMEIG)
    WhoIndicator("WHOSIS_000001", "years", COMPARABLE, "WHO Global Health Estimates (GHE)"),
    WhoIndicator("WHOSIS_000015", "years", COMPARABLE, "WHO Global Health Estimates (GHE)"),
    WhoIndicator("WHOSIS_000002", "years", COMPARABLE, "WHO Global Health Estimates (GHE)"),
    WhoIndicator("WHOSIS_000007", "years", COMPARABLE, "WHO Global Health Estimates (GHE)"),
    WhoIndicator(
        "WHOSIS_000004",
        "per 1000 population (probability of dying between 15 and 60)",
        COMPARABLE,
        "WHO Global Health Estimates (GHE)",
    ),
    WhoIndicator(
        "MDG_0000000026",
        "per 100 000 live births",
        COMPARABLE,
        "UN Maternal Mortality Estimation Inter-agency Group (MMEIG)",
    ),
    WhoIndicator(
        "MDG_0000000032",
        "per 100 000 live births",
        COUNTRY,
        "country-reported (WHO compilation)",
        "country-reported estimates, not comparable across countries",
    ),
    WhoIndicator(
        "MDG_0000000007",
        "per 1000 live births",
        COMPARABLE,
        "UN Inter-agency Group for Child Mortality Estimation (UN IGME)",
        "wealth-quintile rows come from household surveys",
    ),
    WhoIndicator("MDG_0000000001", "per 1000 live births", COMPARABLE, "UN IGME"),
    WhoIndicator("WHOSIS_000003", "per 1000 live births", COMPARABLE, "UN IGME"),
    # causes of death headlines (GHE)
    WhoIndicator("NCDMORT3070", "%", COMPARABLE, "WHO Global Health Estimates (GHE)"),
    WhoIndicator(
        "WHS2_131", "per 100 000 population (age-standardized)", COMPARABLE, "WHO Global Health Estimates (GHE)"
    ),
    WhoIndicator("NCDMORTPERCENT", "% of total deaths", COMPARABLE, "WHO Global Health Estimates (GHE)"),
    WhoIndicator("MH_12", "per 100 000 population (age-standardized)", COMPARABLE, "WHO Global Health Estimates (GHE)"),
    WhoIndicator("SDGSUICIDE", "per 100 000 population (crude)", COMPARABLE, "WHO Global Health Estimates (GHE)"),
    WhoIndicator("RS_198", "per 100 000 population", COMPARABLE, "WHO Global status report on road safety"),
    WhoIndicator("RS_196", "deaths", COMPARABLE, "WHO Global status report on road safety"),
    # environment
    WhoIndicator(
        "SDGAIRBODA",
        "per 100 000 population (age-standardized)",
        COMPARABLE,
        "WHO burden of disease from air pollution",
    ),
    WhoIndicator(
        "AIR_42", "per 100 000 population (age-standardized)", COMPARABLE, "WHO burden of disease from air pollution"
    ),
    WhoIndicator("SDGPM25", "µg/m³ (annual mean)", COMPARABLE, "WHO modelled PM2.5 concentrations"),
    WhoIndicator("SDGWSHBOD", "per 100 000 population", COMPARABLE, "WHO burden of disease from unsafe WASH"),
    WhoIndicator("WSH_WATER_BASIC", "%", COMPARABLE, "WHO/UNICEF Joint Monitoring Programme (JMP)"),
    WhoIndicator("WSH_SANITATION_BASIC", "%", COMPARABLE, "WHO/UNICEF Joint Monitoring Programme (JMP)"),
    # health expenditure (GHED)
    WhoIndicator("GHED_CHEGDP_SHA2011", "% of GDP", MIXED, "WHO Global Health Expenditure Database (GHED)"),
    WhoIndicator(
        "GHED_CHE_pc_US_SHA2011", "current US$ per capita", MIXED, "WHO Global Health Expenditure Database (GHED)"
    ),
    WhoIndicator(
        "GHED_GGHE-DCHE_SHA2011",
        "% of current health expenditure",
        MIXED,
        "WHO Global Health Expenditure Database (GHED)",
    ),
    WhoIndicator(
        "GHED_OOPSCHE_SHA2011",
        "% of current health expenditure",
        MIXED,
        "WHO Global Health Expenditure Database (GHED)",
    ),
    # workforce and capacity (country-reported)
    WhoIndicator("HWF_0001", "per 10 000 population", COUNTRY, "National Health Workforce Accounts (NHWA)"),
    WhoIndicator("HWF_0006", "per 10 000 population", COUNTRY, "National Health Workforce Accounts (NHWA)"),
    WhoIndicator("HWF_0010", "per 10 000 population", COUNTRY, "National Health Workforce Accounts (NHWA)"),
    WhoIndicator("HWF_0014", "per 10 000 population", COUNTRY, "National Health Workforce Accounts (NHWA)"),
    WhoIndicator("WHS6_102", "per 10 000 population", COUNTRY, "country/regional databases compiled by WHO"),
    WhoIndicator("UHC_INDEX_REPORTED", "index (0-100)", COMPARABLE, "WHO/World Bank UHC service coverage index"),
    # immunisation (WUENIC)
    WhoIndicator("WHS4_100", "%", COMPARABLE, "WHO/UNICEF estimates of national immunization coverage (WUENIC)"),
    WhoIndicator("WHS8_110", "%", COMPARABLE, "WHO/UNICEF estimates of national immunization coverage (WUENIC)"),
    WhoIndicator("MCV2", "%", COMPARABLE, "WHO/UNICEF estimates of national immunization coverage (WUENIC)"),
    WhoIndicator("WHS4_117", "%", COMPARABLE, "WHO/UNICEF estimates of national immunization coverage (WUENIC)"),
    # risk factors
    WhoIndicator("NCD_BMI_30A", "% (age-standardized)", COMPARABLE, "NCD Risk Factor Collaboration (NCD-RisC)/WHO"),
    WhoIndicator("NCD_BMI_PLUS2C", "% (crude)", COMPARABLE, "NCD Risk Factor Collaboration (NCD-RisC)/WHO"),
    WhoIndicator(
        "NCD_HYP_PREVALENCE_A", "% (age-standardized)", COMPARABLE, "NCD Risk Factor Collaboration (NCD-RisC)/WHO"
    ),
    WhoIndicator(
        "M_Est_tob_curr_std",
        "% (age-standardized)",
        COMPARABLE,
        "WHO global report on trends in prevalence of tobacco use",
    ),
    WhoIndicator(
        "M_Est_smk_curr_std",
        "% (age-standardized)",
        COMPARABLE,
        "WHO global report on trends in prevalence of tobacco use",
    ),
    WhoIndicator(
        "SA_0000001688",
        "litres of pure alcohol per capita (15+), three-year average",
        COMPARABLE,
        "WHO Global Information System on Alcohol and Health (GISAH)",
    ),
    WhoIndicator("NUTSTUNTINGPREV", "%", COMPARABLE, "UNICEF/WHO/World Bank Joint Malnutrition Estimates (JME)"),
    WhoIndicator("MDG_0000000025", "%", COUNTRY, "household surveys and routine data compiled by WHO/UNICEF"),
    # infectious diseases
    WhoIndicator("MDG_0000000020", "per 100 000 population per year", COMPARABLE, "WHO Global TB Programme"),
    WhoIndicator("MDG_0000000029", "%", COMPARABLE, "UNAIDS/WHO estimates"),
    WhoIndicator("MALARIA_EST_INCIDENCE", "per 1000 population at risk", COMPARABLE, "WHO World Malaria Report"),
)
INDICATORS_BY_CODE = {i.code: i for i in INDICATORS}


class WhoParseError(SourceResponseError):
    pass


def _err(msg: str) -> WhoParseError:
    return WhoParseError(msg, retryable=False, kind="schema_drift")


def data_url(code: str, countries: Sequence[str] | None = None) -> str:
    flt = "SpatialDimType eq 'COUNTRY'"
    if countries:
        flt += " and (" + " or ".join(f"SpatialDim eq '{c}'" for c in countries) + ")"
    return f"{BASE}/{code}?$filter={quote(flt, safe=chr(39) + '()')}&$select={','.join(SELECT)}"


INDICATOR_LIST_URL = f"{BASE}/Indicator"


def fold_indicator(code: str, row: Mapping[str, Any]) -> str:
    parts = [code]
    for tfield, vfield in DIM_FIELDS:
        t, v = row.get(tfield), row.get(vfield)
        if v is None and t is None:
            continue
        if not t or v is None:
            raise _err(f"{code}: dimension {tfield}={t!r} with value {v!r}")
        parts.append(f"{str(t).lower()}={v}")
    return "|".join(parts)


class WhoGhoConnector:
    """WHO GHO OData connector for the atlas (archives raw; :meth:`atlas_frame` builds the table)."""

    ATLAS_SOURCE = "who"
    ATLAS_DATASET = "gho"
    GEO_SCHEME = "iso3"
    RIGHTS = RIGHTS

    def __init__(self, indicators: Sequence[str] | None = None, countries: Sequence[str] | None = None) -> None:
        codes = tuple(indicators) if indicators else tuple(i.code for i in INDICATORS)
        unknown = [c for c in codes if c not in INDICATORS_BY_CODE]
        if unknown:
            raise ValueError(f"indicators not in the curated list: {unknown}")
        self.indicators = codes
        self.countries = tuple(sorted(countries)) if countries else None

    # ------------------------------------------------------------------ run protocol (atlas_healthedu_common)
    def items(self) -> tuple[FetchItem, ...]:
        out = [
            FetchItem(
                key="gho.indicator_list", url=INDICATOR_LIST_URL, max_bytes=5_000_000, params={"kind": "indicator_list"}
            )
        ]
        for code in self.indicators:
            out.append(
                FetchItem(
                    key=f"gho.data:{code}",
                    url=data_url(code, self.countries),
                    max_bytes=MAX_ITEM_BYTES,
                    params={"kind": "data", "indicator": code},
                )
            )
        return tuple(out)

    def follow_ups(self, item: FetchItem, body: bytes) -> tuple[FetchItem, ...]:
        return ()

    def check(self, item: FetchItem, body: bytes) -> dict[str, Any]:
        """Structural check of one archived response (drift -> ``WhoParseError``)."""
        if item.params.get("kind") == "indicator_list":
            return {"indicators_listed": len(parse_indicator_list(body))}
        code = str(item.params["indicator"])
        rows = parse_data(body, code)
        if not rows:
            raise _err(f"{code}: the API returned no country rows")
        return {"rows": len(rows)}

    # ------------------------------------------------------------------ atlas table
    def atlas_frame(self, bodies: Mapping[str, bytes], raws: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]]:
        """Build the atlas frame from the archived bodies (item key -> bytes, item key -> sha256)."""
        names = parse_indicator_list(bodies["gho.indicator_list"])
        problems = []
        for code in self.indicators:
            n = names.get(code)
            if n is None:
                problems.append(f"{code}: not in the GHO indicator list")
            elif n.lower().startswith("archived"):
                problems.append(f"{code}: archived by WHO ({n})")
        if problems:
            raise ValidationProblem("WHO indicator list drift: registration refused", problems=problems)
        import polars as pl

        parts = []
        for code in self.indicators:
            key = f"gho.data:{code}"
            rows = atlas_rows(
                parse_data(bodies[key], code),
                INDICATORS_BY_CODE[code],
                raw_sha256=raws[key],
                item_key=key,
                indicator_name=names[code],
            )
            parts.append(_frame(rows))
        frame = pl.concat(parts, how="vertical")
        note = (
            f"WHO GHO OData, {len(self.indicators)} indicators, country rows only "
            f"(SpatialDimType COUNTRY){'; subset ' + ','.join(self.countries) if self.countries else ''}. "
            "Values = NumericValue as served; flags.estimate_class separates WHO comparable estimates "
            "from country-reported data."
        )
        return frame, note, {"indicator_names": {c: names[c] for c in self.indicators}}


# ---------------------------------------------------------------------- parsing (pure functions)
def parse_indicator_list(body: bytes) -> dict[str, str]:
    try:
        doc = json.loads(body)
        vals = doc["value"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("indicator list is not the expected OData JSON") from e
    out: dict[str, str] = {}
    for v in vals:
        if not isinstance(v, dict) or "IndicatorCode" not in v or "IndicatorName" not in v:
            raise _err("indicator list entry lacks IndicatorCode/IndicatorName")
        out[str(v["IndicatorCode"])] = str(v["IndicatorName"])
    if not out:
        raise _err("indicator list is empty")
    return out


def parse_data(body: bytes, code: str) -> list[dict[str, Any]]:
    """Rows of one indicator response (decimals exact); structural drift raises ``WhoParseError``."""
    try:
        doc = json_decimal(body)
        rows = doc["value"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err(f"{code}: response is not the expected OData JSON") from e
    if not isinstance(rows, list):
        raise _err(f"{code}: 'value' is not a list")
    if "@odata.nextLink" in doc:
        raise _err(f"{code}: response is paged (@odata.nextLink); paging is not implemented")
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise _err(f"{code}: row {i} is not an object")
        missing = [f for f in SELECT if f not in r]
        if missing:
            raise _err(f"{code}: row {i} lacks {missing}")
        if r["IndicatorCode"] != code:
            raise _err(f"{code}: row {i} has IndicatorCode {r['IndicatorCode']!r}")
        if r["SpatialDimType"] != "COUNTRY":
            raise _err(f"{code}: row {i} has SpatialDimType {r['SpatialDimType']!r}")
        if r["TimeDimType"] != "YEAR":
            raise _err(f"{code}: row {i} has TimeDimType {r['TimeDimType']!r} (only YEAR is supported)")
        if not isinstance(r["TimeDim"], Decimal) or r["TimeDim"] != int(r["TimeDim"]):
            raise _err(f"{code}: row {i} has a non-integer TimeDim {r['TimeDim']!r}")
        for f in ("NumericValue", "Low", "High"):
            if r[f] is not None and not isinstance(r[f], Decimal):
                raise _err(f"{code}: row {i} {f} is not a number ({r[f]!r})")
    return rows


def _num(v: Any) -> str | None:
    return None if v is None else dec_str(v)


def atlas_rows(
    rows: Sequence[Mapping[str, Any]], ind: WhoIndicator, *, raw_sha256: str, item_key: str, indicator_name: str
) -> list[dict[str, Any]]:
    from reva.atlas.tables import missing_flags, year_period

    out: list[dict[str, Any]] = []
    for idx, r in enumerate(rows):
        folded = fold_indicator(ind.code, r)
        start, end = year_period(int(r["TimeDim"]))
        flags: dict[str, Any] = {
            "estimate_class": ind.estimate_class,
            "producer": ind.producer,
            "classified_by": CLASSIFIED_BY,
            "provider_updated_at": r["Date"],
        }
        if r["Value"] is not None:
            flags["display_value"] = str(r["Value"])
        if r["Low"] is not None or r["High"] is not None:
            flags["low"] = _num(r["Low"])
            flags["high"] = _num(r["High"])
            flags["interval_level"] = "not_stated_per_row"
        if r["Comments"]:
            flags["comments"] = str(r["Comments"])
            if "this is a projection" in str(r["Comments"]).lower():
                flags["projection"] = True
        if ind.note:
            flags["indicator_note"] = ind.note
        value = _num(r["NumericValue"])
        value_text = None
        if value is None:
            if r["Value"] not in (None, ""):
                value_text = str(r["Value"])[:4000]
                flags["numeric_value_absent"] = True
            else:
                flags = json.loads(missing_flags("not_reported", **flags))
        out.append(
            {
                "source": WhoGhoConnector.ATLAS_SOURCE,
                "dataset": WhoGhoConnector.ATLAS_DATASET,
                "indicator": folded,
                "concept_id": CONCEPT_HINTS.get(folded),
                "iso3": None,
                "provider_geo": str(r["SpatialDim"]),
                "entity_qid": None,
                "period_start": start,
                "period_end": end,
                "value": value,
                "value_text": value_text,
                "unit": ind.unit,
                "flags": fit_flags(flags, long_keys=("comments",)),
                "raw_sha256": raw_sha256,
                "selector": canon({"item": item_key, "Id": _num(r["Id"]), "index": idx}),
            }
        )
    return out


def _frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


#: Fixture mode (``--fixture`` / tests) replays the captured subset in tests/fixtures/source/atlas.who_gho.
FIXTURE_INDICATORS = ("WHOSIS_000001", "MDG_0000000032", "MDG_0000000007", "M_Est_tob_curr_std",
                      "GHED_CHEGDP_SHA2011", "RS_198")  # fmt: skip
FIXTURE_COUNTRIES = ("DZA", "FRA", "HKG", "IND", "NGA")


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=WhoGhoConnector.ATLAS_SOURCE,
        provider="World Health Organization",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(WhoGhoConnector.ATLAS_DATASET,),
        dataset_ids=tuple(i.code for i in INDICATORS),
        cadence_seconds=30 * 86400,
        planned_requests=1 + len(INDICATORS),
        max_response_bytes=MAX_ITEM_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(WhoGhoConnector),
        field_mappings={
            "value": "NumericValue",
            "display": "Value",
            "interval": ["Low", "High"],
            "dimensions": [f for _, f in DIM_FIELDS],
            "select": list(SELECT),
            "units": {i.code: i.unit for i in INDICATORS},
        },
        notes="One request per curated GHO indicator, country rows only; a partial run registers nothing.",
    )


def connector_for(run: Any) -> WhoGhoConnector:
    if run.origin == "test_fixture":
        return WhoGhoConnector(FIXTURE_INDICATORS, FIXTURE_COUNTRIES)
    return WhoGhoConnector()


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """ATLASCORE entry point (``uv run python -m reva.atlas.ingest atlas.who_gho``)."""
    return await fetch_archive_register(run, connector_for(run), dataset=WhoGhoConnector.ATLAS_DATASET)


#: Folded indicator -> catalog concept (registry/atlas/catalog/health.yaml; a test keeps them in sync).
CONCEPT_HINTS: dict[str, str] = {}


def _load_hints() -> None:
    from reva.ontology.registry import REGISTRY_ROOT

    path = REGISTRY_ROOT / "atlas" / "catalog" / "health.yaml"
    if not path.exists():
        return
    import yaml

    doc = yaml.safe_load(path.read_text())
    for c in doc.get("concepts") or []:
        for s in c.get("source_order") or []:
            key = s if isinstance(s, str) else f"{s['source']}.{s['dataset']}.{s['indicator']}"
            prefix = f"{WhoGhoConnector.ATLAS_SOURCE}.{WhoGhoConnector.ATLAS_DATASET}."
            if key.startswith(prefix):
                CONCEPT_HINTS.setdefault(key[len(prefix) :], c["id"])


_load_hints()

__all__ = [
    "CONNECTOR_NAME",
    "INDICATORS",
    "RIGHTS",
    "SOURCE_KEY",
    "WhoGhoConnector",
    "atlas_ingest",
    "atlas_rows",
    "atlas_source",
    "data_url",
    "fold_indicator",
    "parse_data",
    "parse_indicator_list",
]
