"""UN layer of the V3 atlas (agent A-UN): WPP 2024, UN DESA migrant stock, SDG API, UNHCR, UNSD.

Five connectors, one per provider source (``registry/sources/<file>.yaml``), each producing
atlas tables in the ATLASCORE long schema (``reva.atlas.tables``, see
``docs/dev/v3_atlas_interface.md``):

=====================  =====================  ==============================================
connector              source_key             atlas tables (``<source>.<dataset>``)
=====================  =====================  ==============================================
``atlas.un_wpp``       ``un.wpp``             ``un_wpp.wpp2024_demo`` (61 demographic
                                              indicators, estimates 1950-2023 + medium
                                              projection 2024-2100), ``un_wpp.wpp2024_variants``
                                              (total population, every projection variant),
                                              ``un_wpp.wpp2024_age5`` (5-year age group x sex)
``atlas.un_ims``       ``un.desa_ims``        ``un_desa.ims2024`` (bilateral migrant stock,
                                              destination x origin x sex, 1990-2024)
``atlas.un_sdg``       ``un.sdg``             ``un_sdg.sdg_headline`` (selected SDG series)
``atlas.unhcr``        ``unhcr.population``   ``unhcr.pop_bilateral``, ``unhcr.pop_by_asylum``,
                                              ``unhcr.pop_by_origin``
``atlas.unsd_syb``     ``unsd.syb``           ``unsd.syb68_t02`` (population, density and
                                              surface area; area from the Demographic Yearbook)
=====================  =====================  ==============================================

Honesty rules applied here
--------------------------
* Values are the provider's numbers as text. A value that is not a strict decimal is normalised
  only when it is a plain re-spelling of the same number (``"7,021.73"`` -> ``"7021.73"``,
  ``"5."`` -> ``"5"``) and the original text is kept in ``flags.provider_text``; anything else
  becomes ``value_text`` (``">95"``) or the cell is omitted and counted, never coerced.
* **Projections are separate series.** WPP years >= 2024 are projections: their indicator code
  carries ``|proj=<variant>`` and their flags ``projection: true`` with the variant, so a
  "best available" lookup for an estimate concept can never return a projected value.
* Provider geographies are kept verbatim (``provider_geo``) and mapped to crosswalk keys through
  the ``un_m49x`` scheme (crosswalk M49 + the evidence-backed aliases in
  ``registry/atlas/geo_aliases/a_un.yaml``); aggregates (World, regions, income groups) and areas
  outside the crosswalk (e.g. "China, Taiwan Province of China", M49 158) are kept with
  ``iso3 = null`` and listed in the table manifest -- never dropped, never guessed.
* Bilateral tables (migrant stock, UNHCR by origin x asylum) put the reporting side in
  ``provider_geo`` (destination / country of asylum) and fold the counterpart into the indicator
  (``migrant_stock|origin=250|sex=T``, ``refugees|origin=SYR``); ``flags`` repeat the counterpart's
  provider code, crosswalk key (or null) and label for the LINKS layer.
* UNHCR renders a cell without a figure as the *string* ``"0"`` (and ``"-"`` for not applicable);
  real figures are JSON integers. Only integers are stored; string cells are counted and omitted.

Every cell keeps the sha256 of the archived raw artifact and a selector into it. Parsing is a
pure function of the archived bytes; :func:`run` archives (``publish_acquisition`` with zero
observation batches), parses and registers (``register_atlas_table``).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import inspect
import io
import json
import re
import zipfile
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import polars as pl

from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-un-1"
GEO_SCHEME = "un_m49x"  # registry/atlas/geo_aliases/a_un.yaml: crosswalk M49 + evidence-backed aliases
STRICT_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
_PLAIN_NUMBER = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)?(\.\d*)?([eE][-+]?\d{1,3})?$")
MAX_TEXT = 4000


def _err(msg: str, kind: str = "schema_drift", retryable: bool = False, **details: Any) -> SourceResponseError:
    return SourceResponseError(msg, retryable=retryable, kind=kind, **details)


def canon(obj: Mapping[str, Any]) -> str:
    """Canonical JSON object text (sorted keys, no spaces) for ``flags`` / ``selector``."""
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def strict_decimal(text: str) -> tuple[str | None, bool]:
    """``(decimal_text, changed)`` for a reported number, or ``(None, False)`` when it is not one.

    A strict decimal is returned unchanged. A plain re-spelling of a number (thousands commas,
    a leading ``+``, ``5.``, ``.5``, leading zeros) is normalised to the exact same value; the
    caller keeps the original text. Non-numbers (``">95"``, ``"NaN"``, ``"..``) give ``None``.
    """
    s = text.strip()
    if STRICT_DECIMAL.match(s):
        return s, s != text
    if not s or not _PLAIN_NUMBER.match(s) or not any(ch.isdigit() for ch in s):
        return None, False
    try:
        d = Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None, False
    if not d.is_finite():
        return None, False
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    if out in ("-0", ""):
        out = "0"
    if not STRICT_DECIMAL.match(out):
        return None, False
    return out, True


def year_bounds(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year + 1, 1, 1)


def m49_code(code: Any) -> str:
    """Provider M49 / UN location code as the canonical digit string (``"004"`` -> ``"4"``)."""
    s = str(code).strip()
    if not s.isdigit():
        raise _err(f"location code {code!r} is not numeric")
    return str(int(s))


def geo_iso3(provider_geo: str | None, scheme: str = GEO_SCHEME) -> str | None:
    from reva.atlas.tables import country_iso3

    if provider_geo is None:
        return None
    try:
        return country_iso3(provider_geo, scheme)
    except ValueError:  # scheme file missing: report unmapped rather than guess
        return None


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(inspect.getmodule(code_digest)).encode()).hexdigest()  # type: ignore[arg-type]


def gunzip_bounded(body: bytes, limit: int) -> bytes:
    """Decompress a (possibly multi-member) gzip body incrementally, refusing more than ``limit``
    bytes (bomb guard), a truncated member or non-gzip trailing data."""
    out = io.BytesIO()
    data = body
    members = 0
    while True:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        while data:
            out.write(d.decompress(data, 1 << 20))
            if out.tell() > limit:
                raise _err(f"decompressed body exceeds {limit} bytes", kind="decompression_limit")
            data = d.unconsumed_tail
            if d.eof:
                break
        out.write(d.flush())
        if not d.eof:
            raise _err("truncated gzip body", kind="decoding_error", retryable=True)
        members += 1
        rest = d.unused_data
        if rest[:2] == b"\x1f\x8b" and members < 10_000:
            data = rest
            continue
        if rest.strip(b"\x00"):
            raise _err("trailing data after the gzip member", kind="decoding_error")
        break
    if out.tell() > limit:
        raise _err(f"decompressed body exceeds {limit} bytes", kind="decompression_limit")
    return out.getvalue()


def decode_text(data: bytes) -> tuple[str, str]:
    """``(text, encoding)``: UTF-8 (BOM stripped) or, failing that, Windows-1252 (UNSD CSVs)."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="strict"), "cp1252"


def _reject_html(body: bytes) -> None:
    head = body[:512].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html")):
        raise _err("HTML page received instead of data", kind="html_body", retryable=True)


# ----------------------------------------------------------------------------- table plans
@dataclass
class TablePlan:
    source: str
    dataset: str
    frame: pl.DataFrame
    raw_sha256s: list[str]
    coverage_note: str
    geo_scheme: str | None = GEO_SCHEME
    report: dict[str, Any] = field(default_factory=dict)


@dataclass
class RowBuffer:
    """Columnar row buffer for the atlas schema (cheaper than a list of dicts)."""

    source: str
    dataset: str
    cols: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))

    def add(
        self,
        *,
        indicator: str,
        provider_geo: str,
        period: tuple[date, date],
        value: str | None,
        unit: str,
        flags: Mapping[str, Any],
        raw_sha256: str,
        selector: Mapping[str, Any],
        value_text: str | None = None,
        concept_id: str | None = None,
        iso3: str | None = None,
    ) -> None:
        c = self.cols
        c["indicator"].append(indicator)
        c["concept_id"].append(concept_id)
        c["iso3"].append(iso3)
        c["provider_geo"].append(provider_geo)
        c["period_start"].append(period[0])
        c["period_end"].append(period[1])
        c["value"].append(value)
        c["value_text"].append(value_text)
        c["unit"].append(unit)
        c["flags"].append(canon(flags))
        c["raw_sha256"].append(raw_sha256)
        c["selector"].append(canon(selector))

    def __len__(self) -> int:
        return len(self.cols["indicator"])

    def frame(self) -> pl.DataFrame:
        from reva.atlas.tables import ATLAS_SCHEMA

        n = len(self)
        data = {
            "source": [self.source] * n,
            "dataset": [self.dataset] * n,
            "entity_qid": [None] * n,
            **{k: v for k, v in self.cols.items()},
        }
        if n == 0:
            return pl.DataFrame(schema=ATLAS_SCHEMA)
        return pl.DataFrame(data, schema=ATLAS_SCHEMA).select(list(ATLAS_SCHEMA.names()))


# ============================================================================= WPP 2024
WPP_SOURCE = "un_wpp"
WPP_HOST = "population.un.org"
WPP_BASE = "https://population.un.org/wpp/assets/Excel%20Files/1_Indicator%20(Standard)/CSV_FILES/"
WPP_REVISION = "WPP 2024"
WPP_FIRST_PROJECTION_YEAR = 2024  # WPP 2024: estimates 1950-2023, projections 2024-2100
WPP_MAX_DECOMPRESSED = 1_200_000_000


@dataclass(frozen=True)
class WppFile:
    key: str
    file: str
    dataset: str
    max_bytes: int

    @property
    def url(self) -> str:
        return WPP_BASE + self.file


WPP_DI = WppFile("wpp:demo", "WPP2024_Demographic_Indicators_Medium.csv.gz", "wpp2024_demo", 40_000_000)
WPP_VARIANTS = WppFile("wpp:variants", "WPP2024_TotalPopulationBySex.csv.gz", "wpp2024_variants", 40_000_000)
WPP_AGE5 = WppFile("wpp:age5", "WPP2024_PopulationByAge5GroupSex_Medium.csv.gz", "wpp2024_age5", 70_000_000)
WPP_FILES: tuple[WppFile, ...] = (WPP_DI, WPP_VARIANTS, WPP_AGE5)

#: Demographic-indicator columns: (unit, time reference, short English definition). Units and
#: definitions as documented by the Population Division for the WPP 2024 CSV files.
_STOCK_JUL = "as of 1 July"
_STOCK_JAN = "as of 1 January"
_FLOW = "calendar year (1 January - 31 December)"
_LIFE = "period life table of the calendar year"
WPP_DI_COLUMNS: dict[str, tuple[str, str, str]] = {
    "TPopulation1Jan": ("thousands", _STOCK_JAN, "Total population, as of 1 January"),
    "TPopulation1July": ("thousands", _STOCK_JUL, "Total population, as of 1 July"),
    "TPopulationMale1July": ("thousands", _STOCK_JUL, "Male population, as of 1 July"),
    "TPopulationFemale1July": ("thousands", _STOCK_JUL, "Female population, as of 1 July"),
    "PopDensity": ("persons per square km", _STOCK_JUL, "Population density, as of 1 July"),
    "PopSexRatio": ("males per 100 females", _STOCK_JUL, "Population sex ratio, as of 1 July"),
    "MedianAgePop": ("years", _STOCK_JUL, "Median age, as of 1 July"),
    "NatChange": ("thousands", _FLOW, "Natural change, births minus deaths"),
    "NatChangeRT": ("per 1,000 population", _FLOW, "Rate of natural change"),
    "PopChange": ("thousands", _FLOW, "Population change"),
    "PopGrowthRate": ("percentage", _FLOW, "Population growth rate"),
    "DoublingTime": ("years", _FLOW, "Population annual doubling time"),
    "Births": ("thousands", _FLOW, "Births"),
    "Births1519": ("thousands", _FLOW, "Births by women aged 15 to 19"),
    "CBR": ("births per 1,000 population", _FLOW, "Crude birth rate"),
    "TFR": ("live births per woman", _FLOW, "Total fertility rate"),
    "NRR": ("surviving daughters per woman", _FLOW, "Net reproduction rate"),
    "MAC": ("years", _FLOW, "Mean age at childbearing"),
    "SRB": ("males per 100 female births", _FLOW, "Sex ratio at birth"),
    "Deaths": ("thousands", _FLOW, "Total deaths"),
    "DeathsMale": ("thousands", _FLOW, "Male deaths"),
    "DeathsFemale": ("thousands", _FLOW, "Female deaths"),
    "CDR": ("deaths per 1,000 population", _FLOW, "Crude death rate"),
    "LEx": ("years", _LIFE, "Life expectancy at birth, both sexes"),
    "LExMale": ("years", _LIFE, "Male life expectancy at birth"),
    "LExFemale": ("years", _LIFE, "Female life expectancy at birth"),
    "LE15": ("years", _LIFE, "Life expectancy at age 15, both sexes"),
    "LE15Male": ("years", _LIFE, "Male life expectancy at age 15"),
    "LE15Female": ("years", _LIFE, "Female life expectancy at age 15"),
    "LE65": ("years", _LIFE, "Life expectancy at age 65, both sexes"),
    "LE65Male": ("years", _LIFE, "Male life expectancy at age 65"),
    "LE65Female": ("years", _LIFE, "Female life expectancy at age 65"),
    "LE80": ("years", _LIFE, "Life expectancy at age 80, both sexes"),
    "LE80Male": ("years", _LIFE, "Male life expectancy at age 80"),
    "LE80Female": ("years", _LIFE, "Female life expectancy at age 80"),
    "InfantDeaths": ("thousands", _FLOW, "Infant deaths, under age 1"),
    "IMR": ("infant deaths per 1,000 live births", _FLOW, "Infant mortality rate"),
    "LBsurvivingAge1": ("thousands", _FLOW, "Live births surviving to age 1"),
    "Under5Deaths": ("thousands", _FLOW, "Deaths under age 5"),
    "Q5": ("deaths under age 5 per 1,000 live births", _LIFE, "Under-five mortality"),
    "Q0040": ("deaths under age 40 per 1,000 live births", _LIFE, "Mortality before age 40, both sexes"),
    "Q0040Male": ("deaths under age 40 per 1,000 male live births", _LIFE, "Male mortality before age 40"),
    "Q0040Female": ("deaths under age 40 per 1,000 female live births", _LIFE, "Female mortality before age 40"),
    "Q0060": ("deaths under age 60 per 1,000 live births", _LIFE, "Mortality before age 60, both sexes"),
    "Q0060Male": ("deaths under age 60 per 1,000 male live births", _LIFE, "Male mortality before age 60"),
    "Q0060Female": ("deaths under age 60 per 1,000 female live births", _LIFE, "Female mortality before age 60"),
    "Q1550": ("deaths under age 50 per 1,000 alive at age 15", _LIFE, "Mortality between age 15 and 50, both sexes"),
    "Q1550Male": ("deaths under age 50 per 1,000 males alive at age 15", _LIFE, "Male mortality between age 15 and 50"),
    "Q1550Female": (
        "deaths under age 50 per 1,000 females alive at age 15",
        _LIFE,
        "Female mortality between age 15 and 50",
    ),
    "Q1560": ("deaths under age 60 per 1,000 alive at age 15", _LIFE, "Mortality between age 15 and 60, both sexes"),
    "Q1560Male": ("deaths under age 60 per 1,000 males alive at age 15", _LIFE, "Male mortality between age 15 and 60"),
    "Q1560Female": (
        "deaths under age 60 per 1,000 females alive at age 15",
        _LIFE,
        "Female mortality between age 15 and 60",
    ),
    "NetMigrations": ("thousands", _FLOW, "Net number of migrants"),
    "CNMR": ("per 1,000 population", _FLOW, "Net migration rate"),
}
WPP_ID_COLUMNS = (
    "SortOrder",
    "LocID",
    "Notes",
    "ISO3_code",
    "ISO2_code",
    "SDMX_code",
    "LocTypeID",
    "LocTypeName",
    "ParentID",
    "Location",
    "VarID",
    "Variant",
    "Time",
)
#: Variants taken from TotalPopulationBySex (Medium is in the demographic-indicator table).
WPP_VARIANT_VALUE_COLUMNS = ("PopTotal",)
WPP_AGE5_VALUE_COLUMNS = {"PopTotal": "both sexes", "PopMale": "males", "PopFemale": "females"}


def wpp_concept_id(column: str, projection: bool) -> str | None:
    """Catalog concept hint of a demographic-indicator series (``registry/atlas/catalog/un.yaml``)."""
    base = WPP_CONCEPTS.get(column)
    if base is None:
        return None
    if projection:
        return f"{base}_proj_medium" if column in WPP_PROJECTION_CONCEPTS else None
    return base


WPP_CONCEPTS: dict[str, str] = {
    "TPopulation1July": "un.wpp.population",
    "TPopulation1Jan": "un.wpp.population_1jan",
    "TPopulationMale1July": "un.wpp.population_male",
    "TPopulationFemale1July": "un.wpp.population_female",
    "PopDensity": "un.wpp.population_density",
    "PopSexRatio": "un.wpp.sex_ratio",
    "MedianAgePop": "un.wpp.median_age",
    "NatChange": "un.wpp.natural_change",
    "NatChangeRT": "un.wpp.natural_change_rate",
    "PopChange": "un.wpp.population_change",
    "PopGrowthRate": "un.wpp.population_growth_rate",
    "DoublingTime": "un.wpp.doubling_time",
    "Births": "un.wpp.births",
    "Births1519": "un.wpp.births_15_19",
    "CBR": "un.wpp.crude_birth_rate",
    "TFR": "un.wpp.total_fertility_rate",
    "NRR": "un.wpp.net_reproduction_rate",
    "MAC": "un.wpp.mean_age_childbearing",
    "SRB": "un.wpp.sex_ratio_at_birth",
    "Deaths": "un.wpp.deaths",
    "DeathsMale": "un.wpp.deaths_male",
    "DeathsFemale": "un.wpp.deaths_female",
    "CDR": "un.wpp.crude_death_rate",
    "LEx": "un.wpp.life_expectancy",
    "LExMale": "un.wpp.life_expectancy_male",
    "LExFemale": "un.wpp.life_expectancy_female",
    "LE15": "un.wpp.life_expectancy_15",
    "LE65": "un.wpp.life_expectancy_65",
    "LE65Male": "un.wpp.life_expectancy_65_male",
    "LE65Female": "un.wpp.life_expectancy_65_female",
    "LE80": "un.wpp.life_expectancy_80",
    "InfantDeaths": "un.wpp.infant_deaths",
    "IMR": "un.wpp.infant_mortality_rate",
    "Under5Deaths": "un.wpp.under5_deaths",
    "Q5": "un.wpp.under5_mortality",
    "Q0060": "un.wpp.mortality_before_60",
    "Q1560": "un.wpp.adult_mortality_15_60",
    "NetMigrations": "un.wpp.net_migrants",
    "CNMR": "un.wpp.net_migration_rate",
}
#: Medium-variant projection concepts (``<concept>_proj_medium``).
WPP_PROJECTION_CONCEPTS = frozenset(
    {
        "TPopulation1July",
        "TPopulationMale1July",
        "TPopulationFemale1July",
        "PopDensity",
        "MedianAgePop",
        "PopGrowthRate",
        "Births",
        "Deaths",
        "TFR",
        "LEx",
        "LExMale",
        "LExFemale",
        "IMR",
        "Q5",
        "NetMigrations",
        "CNMR",
        "CBR",
        "CDR",
    }
)
#: Total-population projection variants with a catalog concept (``un.wpp.population_proj_<slug>``).
WPP_VARIANT_CONCEPTS: dict[str, str] = {
    "High": "high",
    "Low": "low",
    "Constant fertility": "constant_fertility",
    "Zero migration": "zero_migration",
    "Lower 95 PI": "lower95",
    "Upper 95 PI": "upper95",
    "Lower 80 PI": "lower80",
    "Upper 80 PI": "upper80",
    "Median PI": "median_pi",
}


def wpp_variant_indicator(column: str, variant: str) -> str:
    return f"{column}|variant={variant}"


def wpp_projection_suffix(variant: str = "Medium") -> str:
    return f"|proj={variant.lower()}"


def read_wpp_csv(body: bytes, *, file: str, limit: int = WPP_MAX_DECOMPRESSED) -> tuple[pl.DataFrame, str]:
    """A WPP CSV(.gz) as an all-string frame with a 0-based ``record`` index, plus its encoding."""
    _reject_html(body)
    data = gunzip_bounded(body, limit) if body[:2] == b"\x1f\x8b" else body
    text, enc = decode_text(data)
    del data
    try:
        frame = pl.read_csv(io.StringIO(text), infer_schema=False, has_header=True)
    except pl.exceptions.PolarsError as e:  # malformed CSV
        raise _err(f"{file}: not a readable CSV ({type(e).__name__})") from e
    missing = [c for c in WPP_ID_COLUMNS if c not in frame.columns and c not in ("MidPeriod",)]
    if missing:
        raise _err(f"{file}: missing identifier columns {missing}", columns=frame.columns[:30])
    return frame.with_row_index("record"), enc


def _normalise_value_column(frame: pl.DataFrame, file: str) -> pl.DataFrame:
    """Keep strict decimals; re-spell plain numbers (original kept); refuse anything else."""
    bad = frame.filter(~pl.col("value").str.contains(STRICT_DECIMAL.pattern))
    if bad.height == 0:
        return frame.with_columns(pl.lit(None, dtype=pl.String).alias("provider_text"))
    fixes: dict[str, str] = {}
    for v in bad["value"].unique().to_list():
        out, _ = strict_decimal(v)
        if out is None:
            raise _err(f"{file}: non-numeric value {v[:40]!r} in a numeric column")
        fixes[v] = out
    return frame.with_columns(
        pl.when(pl.col("value").is_in(list(fixes))).then(pl.col("value")).otherwise(None).alias("provider_text"),
        pl.col("value").replace(fixes).alias("value"),
    )


def _wpp_long(
    frame: pl.DataFrame,
    *,
    value_columns: Sequence[str],
    extra_index: Sequence[str] = (),
) -> pl.DataFrame:
    idx = ["record", "LocID", "LocTypeName", "Location", "VarID", "Variant", "Time", *extra_index]
    long = frame.select([*idx, *value_columns]).unpivot(
        on=list(value_columns), index=idx, variable_name="column", value_name="value"
    )
    return long.filter(pl.col("value").is_not_null() & (pl.col("value").str.strip_chars() != ""))


def _flags_join(long: pl.DataFrame, keys: Sequence[str], make: Callable[[dict[str, Any]], str]) -> pl.DataFrame:
    """Attach a ``flags`` JSON column computed once per distinct ``keys`` combination."""
    combos = long.select(list(keys)).unique()
    flags = [make(r) for r in combos.iter_rows(named=True)]
    table = combos.with_columns(pl.Series("flags", flags, dtype=pl.String))
    return long.join(table, on=list(keys), how="left", nulls_equal=True)


def _wpp_frame(
    long: pl.DataFrame,
    *,
    dataset: str,
    file: str,
    raw_sha256: str,
    indicator: pl.Expr,
    unit: pl.Expr,
    concept: pl.Expr,
) -> pl.DataFrame:
    from reva.atlas.tables import ATLAS_SCHEMA

    year = pl.col("Time").cast(pl.Int32, strict=True)
    selector = pl.concat_str(
        [
            pl.lit('{"column":"'),
            pl.col("column"),
            pl.lit(f'","file":"{file}","record":'),
            pl.col("record").cast(pl.String),
            pl.lit("}"),
        ]
    )
    flags = (
        pl.when(pl.col("provider_text").is_not_null())
        .then(
            pl.concat_str(
                [
                    pl.col("flags").str.slice(0, pl.col("flags").str.len_chars() - 1),
                    pl.lit(',"provider_text":"'),
                    pl.col("provider_text"),
                    pl.lit('"}'),
                ]
            )
        )
        .otherwise(pl.col("flags"))
    )
    return long.select(
        pl.lit(WPP_SOURCE).alias("source"),
        pl.lit(dataset).alias("dataset"),
        indicator.alias("indicator"),
        concept.alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        pl.col("LocID").alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(year, 1, 1).alias("period_start"),
        pl.date(year + 1, 1, 1).alias("period_end"),
        pl.col("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        unit.alias("unit"),
        flags.alias("flags"),
        pl.lit(raw_sha256).alias("raw_sha256"),
        selector.alias("selector"),
    ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]


def _wpp_base_flags(r: Mapping[str, Any], projection: bool, variant: str) -> dict[str, Any]:
    return {
        "wpp_revision": WPP_REVISION,
        "variant": variant,
        "projection": projection,
        "estimate": not projection,
        "loc_type": r.get("LocTypeName") or None,
        "geo_label": r.get("Location"),
    }


def _check_wpp_identity(frame: pl.DataFrame, file: str) -> None:
    bad_loc = frame.filter(~pl.col("LocID").str.contains(r"^[0-9]+$"))
    if bad_loc.height:
        raise _err(f"{file}: non-numeric LocID {bad_loc['LocID'][0]!r}")
    bad_t = frame.filter(~pl.col("Time").str.contains(r"^[0-9]{4}$"))
    if bad_t.height:
        raise _err(f"{file}: non-year Time {bad_t['Time'][0]!r}")


def build_wpp_demo(body: bytes, raw_sha256: str) -> TablePlan:
    """Demographic indicators (medium file): estimates 1950-2023 and medium projections 2024-2100."""
    f = WPP_DI
    frame, enc = read_wpp_csv(body, file=f.file)
    _check_wpp_identity(frame, f.file)
    cols = [c for c in WPP_DI_COLUMNS if c in frame.columns]
    missing = sorted(set(WPP_DI_COLUMNS) - set(cols))
    if missing:
        raise _err(f"{f.file}: expected indicator columns are missing: {missing}")
    extra = sorted(set(frame.columns) - set(WPP_DI_COLUMNS) - set(WPP_ID_COLUMNS) - {"record"})
    variants = frame["Variant"].unique().to_list()
    if variants != ["Medium"]:
        raise _err(f"{f.file}: expected only the Medium variant, found {variants}")
    long = _wpp_long(frame, value_columns=cols)
    del frame
    long = _normalise_value_column(long, f.file)
    long = long.with_columns((pl.col("Time").cast(pl.Int32) >= WPP_FIRST_PROJECTION_YEAR).alias("proj"))

    def make(r: dict[str, Any]) -> str:
        _unit, tref, label = WPP_DI_COLUMNS[r["column"]]
        return canon({**_wpp_base_flags(r, r["proj"], "Medium"), "time_reference": tref, "label": label})

    long = _flags_join(long, ["column", "proj", "LocTypeName", "Location"], make)
    concept_map = {(c, p): wpp_concept_id(c, p) for c in cols for p in (False, True)}
    concept_frame = pl.DataFrame(
        {
            "column": [k[0] for k in concept_map],
            "proj": [k[1] for k in concept_map],
            "concept_hint": [v for v in concept_map.values()],
        },
        schema={"column": pl.String, "proj": pl.Boolean, "concept_hint": pl.String},
    )
    long = long.join(concept_frame, on=["column", "proj"], how="left")
    out = _wpp_frame(
        long,
        dataset=f.dataset,
        file=f.file,
        raw_sha256=raw_sha256,
        indicator=pl.when(pl.col("proj"))
        .then(pl.concat_str([pl.col("column"), pl.lit(wpp_projection_suffix("Medium"))]))
        .otherwise(pl.col("column")),
        unit=pl.col("column").replace_strict({c: WPP_DI_COLUMNS[c][0] for c in cols}, return_dtype=pl.String),
        concept=pl.col("concept_hint"),
    )
    report = {
        "file": f.file,
        "encoding": enc,
        "rows": out.height,
        "columns": cols,
        "unused_columns": extra,
        "projection_rows": int(long["proj"].sum()),
        "respelled_values": int(long["provider_text"].is_not_null().sum()),
    }
    note = (
        "UN WPP 2024 demographic indicators (medium file): estimates 1950-2023 under the bare column code; "
        "medium-variant projections 2024-2100 under '<code>|proj=medium' (flags.projection=true). "
        "Aggregates (regions, income groups, World) kept with iso3=null. Values as reported (thousands etc.)."
    )
    return TablePlan(WPP_SOURCE, f.dataset, out, [raw_sha256], note, report=report)


def build_wpp_variants(body: bytes, raw_sha256: str) -> TablePlan:
    """Total population of every projection variant except Medium (Medium is in wpp2024_demo)."""
    f = WPP_VARIANTS
    frame, enc = read_wpp_csv(body, file=f.file)
    _check_wpp_identity(frame, f.file)
    for c in (*WPP_VARIANT_VALUE_COLUMNS, "VarID", "Variant"):
        if c not in frame.columns:
            raise _err(f"{f.file}: column {c} missing")
    medium_rows = int((frame["Variant"] == "Medium").sum())
    frame = frame.filter(pl.col("Variant") != "Medium")
    early = frame.filter(pl.col("Time").cast(pl.Int32) < WPP_FIRST_PROJECTION_YEAR)
    if early.height:
        raise _err(f"{f.file}: a non-medium variant has years before {WPP_FIRST_PROJECTION_YEAR}")
    long = _wpp_long(frame, value_columns=WPP_VARIANT_VALUE_COLUMNS)
    variants = sorted(frame["Variant"].unique().to_list())
    del frame
    long = _normalise_value_column(long, f.file)

    def make(r: dict[str, Any]) -> str:
        v = r["Variant"]
        return canon(
            {
                **_wpp_base_flags(r, True, v),
                "variant_id": r["VarID"],
                "probabilistic_interval": v.endswith(" PI"),
                "time_reference": _STOCK_JUL,
                "label": "Total population, as of 1 July",
            }
        )

    long = _flags_join(long, ["Variant", "VarID", "LocTypeName", "Location"], make)
    concept = pl.col("Variant").replace_strict(
        {v: f"un.wpp.population_proj_{s}" for v, s in WPP_VARIANT_CONCEPTS.items()},
        default=None,
        return_dtype=pl.String,
    )
    out = _wpp_frame(
        long,
        dataset=f.dataset,
        file=f.file,
        raw_sha256=raw_sha256,
        indicator=pl.concat_str([pl.col("column"), pl.lit("|variant="), pl.col("Variant")]),
        unit=pl.lit("thousands"),
        concept=concept,
    )
    report = {
        "file": f.file,
        "encoding": enc,
        "rows": out.height,
        "variants": variants,
        "medium_rows_skipped_duplicate_of_demo": medium_rows,
    }
    note = (
        "UN WPP 2024 total population (thousands, 1 July) for every projection variant 2024-2100 except "
        "Medium (in un_wpp.wpp2024_demo as TPopulation1July|proj=medium); indicator 'PopTotal|variant=<name>'; "
        "all rows are projections (flags.projection=true; PI variants are probabilistic interval bounds)."
    )
    return TablePlan(WPP_SOURCE, f.dataset, out, [raw_sha256], note, report=report)


def build_wpp_age5(body: bytes, raw_sha256: str) -> Iterator[TablePlan]:
    """Population by 5-year age group and sex (medium), 1950-2100, as two tables: estimates
    1950-2023 (``wpp2024_age5``) and medium projections 2024-2100 (``wpp2024_age5_proj``, indicator
    suffix ``|proj=medium``); one table would exceed the ~5 M row guidance (556 locations)."""
    f = WPP_AGE5
    frame, enc = read_wpp_csv(body, file=f.file)
    _check_wpp_identity(frame, f.file)
    for c in (*WPP_AGE5_VALUE_COLUMNS, "AgeGrp", "AgeGrpStart", "AgeGrpSpan"):
        if c not in frame.columns:
            raise _err(f"{f.file}: column {c} missing")
    variants = frame["Variant"].unique().to_list()
    if variants != ["Medium"]:
        raise _err(f"{f.file}: expected only the Medium variant, found {variants}")
    groups = frame.select("AgeGrp", "AgeGrpStart").unique().sort(pl.col("AgeGrpStart").cast(pl.Int32))["AgeGrp"]
    for proj in (False, True):
        part = frame.filter((pl.col("Time").cast(pl.Int32) >= WPP_FIRST_PROJECTION_YEAR) == proj)
        long = _wpp_long(part, value_columns=list(WPP_AGE5_VALUE_COLUMNS), extra_index=("AgeGrp", "AgeGrpSpan"))
        del part
        long = _normalise_value_column(long, f.file).with_columns(pl.lit(proj).alias("proj"))

        def make(r: dict[str, Any]) -> str:
            return canon(
                {
                    **_wpp_base_flags(r, r["proj"], "Medium"),
                    "age_group": r["AgeGrp"],
                    "age_span": r["AgeGrpSpan"],
                    "sex": WPP_AGE5_VALUE_COLUMNS[r["column"]],
                    "time_reference": _STOCK_JUL,
                }
            )

        long = _flags_join(long, ["column", "proj", "AgeGrp", "AgeGrpSpan", "LocTypeName", "Location"], make)
        indicator = pl.concat_str(
            [
                pl.col("column"),
                pl.lit("|age="),
                pl.col("AgeGrp"),
                pl.lit(wpp_projection_suffix("Medium") if proj else ""),
            ]
        )
        concept = (
            pl.when(pl.col("column") == "PopTotal")
            .then(
                pl.concat_str(
                    [
                        pl.lit("un.wpp.population_age_"),
                        pl.col("AgeGrp").str.replace_all(r"[-+]", "_").str.strip_chars("_"),
                    ]
                )
            )
            .otherwise(pl.lit(None, dtype=pl.String))
            if not proj
            else pl.lit(None, dtype=pl.String)
        )
        dataset = f"{f.dataset}_proj" if proj else f.dataset
        out = _wpp_frame(
            long,
            dataset=dataset,
            file=f.file,
            raw_sha256=raw_sha256,
            indicator=indicator,
            unit=pl.lit("thousands"),
            concept=concept,
        )
        del long
        report = {
            "file": f.file,
            "encoding": enc,
            "rows": out.height,
            "age_groups": groups.to_list(),
            "projection": proj,
        }
        what = (
            "medium-variant projections 2024-2100, indicator '<PopTotal|PopMale|PopFemale>|age=<group>|proj=medium' "
            "(flags.projection=true)"
            if proj
            else "estimates 1950-2023, indicator '<PopTotal|PopMale|PopFemale>|age=<group>'"
        )
        note = (
            f"UN WPP 2024 population by 5-year age group and sex (thousands, 1 July), {what}. Aggregates kept "
            "with iso3=null. Single-age files not ingested (size; see docs/dev/v3_a_un.md)."
        )
        yield TablePlan(WPP_SOURCE, dataset, out, [raw_sha256], note, report=report)
        del out


WPP_BUILDERS: dict[str, Callable[[bytes, str], Iterator[TablePlan]]] = {
    WPP_DI.key: lambda b, s: iter([build_wpp_demo(b, s)]),
    WPP_VARIANTS.key: lambda b, s: iter([build_wpp_variants(b, s)]),
    WPP_AGE5.key: build_wpp_age5,
}


# ============================================================================= xlsx (stdlib)
_XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
XLSX_MAX_UNCOMPRESSED = 300_000_000
_CELL_RE = re.compile(r"^([A-Z]{1,3})([0-9]+)$")


class Xlsx:
    """Minimal, bounded reader of an .xlsx workbook (cell values as text; no formulas evaluated)."""

    def __init__(self, body: bytes):
        _reject_html(body)
        if not body.startswith(b"PK"):
            raise _err("workbook is not a ZIP (xlsx) archive", kind="not_zip")
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile as e:
            raise _err("corrupt xlsx archive", kind="decoding_error") from e
        total = sum(i.file_size for i in self.zip.infolist())
        if total > XLSX_MAX_UNCOMPRESSED:
            raise _err(f"xlsx expands to {total} bytes (> {XLSX_MAX_UNCOMPRESSED})", kind="decompression_limit")
        self.sheets = self._sheets()
        self.shared = self._shared_strings()

    def _xml(self, name: str) -> ET.Element:
        data = self._read(name)
        try:
            return ET.fromstring(data)  # noqa: S314 - DTD/entity declarations refused in _read; expat fetches nothing
        except ET.ParseError as e:
            raise _err(f"xlsx part {name} is not well-formed XML") from e

    def _read(self, name: str) -> bytes:
        try:
            data = self.zip.read(name)
        except KeyError as e:
            raise _err(f"xlsx part {name} missing") from e
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:  # whole part: no DTD may hide behind a long prolog
            raise _err(f"xlsx part {name} declares a DTD/entities (refused)", kind="unsafe_xml")
        return data

    def _sheets(self) -> dict[str, str]:
        wb = self._xml("xl/workbook.xml")
        rels = self._xml("xl/_rels/workbook.xml.rels")
        targets = {r.get("Id"): r.get("Target", "") for r in rels.iter(f"{_REL_NS}Relationship")}
        out: dict[str, str] = {}
        for s in wb.iter(f"{_XL_NS}sheet"):
            t = targets.get(s.get(_DOC_REL), "")
            t = t.lstrip("/")
            out[str(s.get("name"))] = t if t.startswith("xl/") else f"xl/{t}"
        return out

    def _shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self.zip.namelist():
            return []
        root = self._xml("xl/sharedStrings.xml")
        return ["".join(t.text or "" for t in si.iter(f"{_XL_NS}t")) for si in root.findall(f"{_XL_NS}si")]

    def rows(self, sheet: str) -> Iterator[tuple[int, dict[str, str]]]:
        """``(row_number, {column_letters: text})`` for every row with at least one value."""
        if sheet not in self.sheets:
            raise _err(f"sheet {sheet!r} not found (sheets: {sorted(self.sheets)})")
        data = self._read(self.sheets[sheet])
        try:
            yield from self._rows(data)
        except ET.ParseError as e:
            raise _err(f"sheet {sheet!r} is not well-formed XML") from e

    def _rows(self, data: bytes) -> Iterator[tuple[int, dict[str, str]]]:
        for _ev, el in ET.iterparse(io.BytesIO(data)):  # noqa: S314 - DTD/entities refused in _read
            if el.tag != f"{_XL_NS}row":
                continue
            cells: dict[str, str] = {}
            for c in el.findall(f"{_XL_NS}c"):
                m = _CELL_RE.match(c.get("r") or "")
                if not m:
                    continue
                t = c.get("t")
                v = c.find(f"{_XL_NS}v")
                if t == "inlineStr":
                    is_ = c.find(f"{_XL_NS}is")
                    val = "".join(x.text or "" for x in is_.iter(f"{_XL_NS}t")) if is_ is not None else None
                elif v is None or v.text is None:
                    val = None
                elif t == "s":
                    idx = int(v.text)
                    if idx >= len(self.shared):
                        raise _err("shared string index out of range")
                    val = self.shared[idx]
                else:
                    val = v.text
                if val is not None and val != "":
                    cells[m.group(1)] = val
            if cells:
                yield int(el.get("r") or 0), cells
            el.clear()


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


# ============================================================================= UN DESA IMS 2024
IMS_SOURCE = "un_desa"
IMS_DATASET = "ims2024"
IMS_HOST = "www.un.org"
IMS_FILE = "undesa_pd_2024_ims_stock_by_sex_destination_and_origin.xlsx"
IMS_URL = f"https://www.un.org/development/desa/pd/sites/www.un.org.development.desa.pd/files/{IMS_FILE}"
IMS_TABLE = "Table 1"
IMS_NOTES = "Migrant notes"
IMS_SEX_GROUPS = (("both sexes", "T"), ("females", "F"), ("males", "M"))  # 'females' tested before 'males'
IMS_DATA_TYPES = {
    "B": "foreign-born population",
    "C": "foreign citizens",
    "R": "refugees (UNHCR/UNRWA) added",
    "I": "imputed (no data)",
}


def ims_year_columns(header: Mapping[str, str], group_cells: Sequence[tuple[int, str]]) -> dict[str, tuple[int, str]]:
    """Year columns of Table 1 -> (year, sex). The year columns form three equal consecutive blocks
    (both sexes, males, females); each block must repeat the same years and contain (or directly
    follow) its heading cell, whose text names the sex."""
    cols = sorted((col_index(c), c, int(t)) for c, t in header.items() if re.fullmatch(r"(19|20)[0-9]{2}", t.strip()))
    groups = sorted(group_cells)
    if len(cols) < 3 or len(cols) % 3 or [g[1] for g in groups] != ["T", "M", "F"]:
        raise _err(
            "IMS Table 1 year columns / sex headings not in the expected layout", year_columns=len(cols), groups=groups
        )
    n = len(cols) // 3
    blocks = [cols[k * n : (k + 1) * n] for k in range(3)]
    years = [b[2] for b in blocks[0]]
    out: dict[str, tuple[int, str]] = {}
    for block, (gcol, sex) in zip(blocks, groups, strict=True):
        if [b[2] for b in block] != years or [b[0] for b in block] != list(range(block[0][0], block[0][0] + n)):
            raise _err("IMS Table 1 year blocks differ or are not contiguous")
        if not (block[0][0] - 1 <= gcol <= block[-1][0]):
            raise _err(f"IMS Table 1 heading for sex {sex} lies outside its year block")
        for _ci, col, year in block:
            out[col] = (year, sex)
    return out


def parse_ims_notes(xl: Xlsx) -> dict[str, str]:
    notes: dict[str, str] = {}
    if IMS_NOTES not in xl.sheets:
        return notes
    for _r, cells in xl.rows(IMS_NOTES):
        a, b = cells.get("A"), cells.get("B")
        if a and b and a.strip().isdigit():
            notes[a.strip()] = b.strip()[:1000]
    return notes


def build_ims(body: bytes, raw_sha256: str) -> TablePlan:
    """International migrant stock 2024, Table 1: destination x origin x sex x year (persons, 1 July)."""
    xl = Xlsx(body)
    notes = parse_ims_notes(xl)
    header_row: dict[str, str] | None = None
    group_cells: list[tuple[int, str]] = []  # (column index, sex code) of the group headings
    year_cols: dict[str, tuple[int, str]] = {}  # column letters -> (year, sex)
    buf = RowBuffer(IMS_SOURCE, IMS_DATASET)
    stats: Counter[str] = Counter()
    dests: dict[str, str] = {}
    origins: dict[str, str] = {}
    seen: set[tuple[str, str, str, int]] = set()
    for rnum, cells in xl.rows(IMS_TABLE):
        if header_row is None:
            for col, text in cells.items():
                low = text.lower()
                if "international migrant stock at mid-year" in low:
                    for label, code in IMS_SEX_GROUPS:
                        if low.endswith(label) or f", {label}" in low:
                            group_cells.append((col_index(col), code))
                            break
            if cells.get("A", "").strip() == "Index":
                header_row = cells
                if [cells.get(k) for k in ("E", "G")] != ["Location code of destination", "Location code of origin"]:
                    raise _err("IMS Table 1 header changed (location code columns)", header=cells)
                year_cols = ims_year_columns(cells, group_cells)
            continue
        dest, orig = cells.get("E"), cells.get("G")
        if dest is None and orig is None:
            continue
        if dest is None or orig is None:
            raise _err(f"IMS Table 1 row {rnum}: a location code is missing")
        dest, orig = m49_code(dest), m49_code(orig)
        dest_label, orig_label = cells.get("B", ""), cells.get("F", "")
        dests[dest] = dest_label
        origins[orig] = orig_label
        dtype = (cells.get("D") or "").strip() or None
        note_refs = [n.strip() for n in re.split(r"[,; ]+", cells.get("C") or "") if n.strip()]
        base_flags: dict[str, Any] = {
            "time_reference": "as of 1 July",
            "counterpart_role": "origin",
            "counterpart_provider_geo": orig,
            "counterpart_iso3": geo_iso3(orig),
            "counterpart_label": orig_label,
            "geo_label": dest_label,
            "revision": "POP/DB/MIG/Stock/Rev.2024",
        }
        if dtype:
            base_flags["data_type"] = dtype
            base_flags["data_type_meaning"] = [IMS_DATA_TYPES.get(t, "unknown code") for t in dtype.split()]
        if note_refs:
            base_flags["notes"] = {n: notes.get(n, "(note text not found)") for n in note_refs}
        for col, (year, sex) in year_cols.items():
            raw_v = cells.get(col)
            if raw_v is None:
                stats["empty_cells"] += 1
                continue
            key = (dest, orig, sex, year)
            if key in seen:
                raise _err(f"IMS Table 1 duplicate cell {key}")
            seen.add(key)
            val, changed = strict_decimal(raw_v)
            flags = dict(base_flags)
            text_val = None
            if val is None:
                txt = raw_v.strip()
                if txt == "..":
                    stats["not_available_cells"] += 1
                    continue
                text_val = txt[:MAX_TEXT]
                flags["non_numeric"] = True
                stats["text_cells"] += 1
            elif changed:
                flags["provider_text"] = raw_v
                stats["respelled_values"] += 1
            buf.add(
                indicator=f"migrant_stock|origin={orig}|sex={sex}",
                provider_geo=dest,
                period=year_bounds(year),
                value=val,
                value_text=text_val,
                unit="persons",
                flags=flags,
                raw_sha256=raw_sha256,
                selector={"file": IMS_FILE, "sheet": IMS_TABLE, "cell": f"{col}{rnum}"},
                concept_id=_ims_concept(orig, sex),
            )
    if header_row is None:
        raise _err("IMS Table 1 header row ('Index') not found")
    if len(buf) == 0:
        raise _err("IMS Table 1 has no data rows")
    frame = buf.frame()
    report = {
        "file": IMS_FILE,
        "cells": len(buf),
        "destinations": len(dests),
        "origins": len(origins),
        "years": sorted({y for y, _s in year_cols.values()}),
        "notes": len(notes),
        **dict(stats),
        "unmapped_origins": sorted(
            ({"code": c, "label": lab} for c, lab in origins.items() if geo_iso3(c) is None),
            key=lambda d: int(d["code"]),
        ),
    }
    note = (
        "UN DESA International Migrant Stock 2024 (Rev.2024), Table 1: persons at 1 July of 1990..2024 by "
        "destination (provider_geo, M49) x origin x sex; indicator 'migrant_stock|origin=<M49>|sex=<T|M|F>'; "
        "origin 900 = World (total stock in the destination). Regions/aggregates kept with iso3=null. "
        "flags carry data type (B/C/R/I) and migrant notes."
    )
    return TablePlan(IMS_SOURCE, IMS_DATASET, frame, [raw_sha256], note, report=report)


def _ims_concept(orig: str, sex: str) -> str | None:
    if orig == "900":
        return {"T": "un.ims.migrant_stock", "F": "un.ims.migrant_stock_female", "M": "un.ims.migrant_stock_male"}[sex]
    return None


# ============================================================================= UN SDG API v5
SDG_SOURCE = "un_sdg"
SDG_DATASET = "sdg_headline"
SDG_HOST = "unstats.un.org"
SDG_API = "https://unstats.un.org/sdgs/UNSDGAPIV5/v1/sdg/"
SDG_PAGE_SIZE = 10000
SDG_MAX_PAGES_PER_SERIES = 20
SDG_PAGE_MAX_BYTES = 50_000_000


@dataclass(frozen=True)
class SdgSeries:
    code: str
    concept: str  # catalog concept for the headline (total) cells
    filters: tuple[tuple[str, str], ...] = ()  # (dimension, code) server-side filters (heavy series only)


#: Headline series (goal order). Most are fetched with every disaggregation the database holds;
#: the two heaviest are filtered server-side to their totals (declared here, checked on every row).
SDG_SERIES: tuple[SdgSeries, ...] = (
    SdgSeries("SI_POV_DAY1", "un.sdg.poverty_international_line"),
    SdgSeries("SI_POV_NAHC", "un.sdg.poverty_national_line"),
    SdgSeries("SI_COV_BENFTS", "un.sdg.social_protection_coverage"),
    SdgSeries("SN_ITK_DEFC", "un.sdg.undernourishment"),
    SdgSeries("SH_STA_STNT", "un.sdg.stunting"),
    SdgSeries("SH_STA_WAST", "un.sdg.wasting"),
    SdgSeries("SH_STA_MORT", "un.sdg.maternal_mortality_ratio"),
    SdgSeries("SH_STA_BRTC", "un.sdg.skilled_birth_attendance"),
    SdgSeries("SH_DYN_MORT", "un.sdg.under5_mortality"),
    SdgSeries("SH_DYN_IMRT", "un.sdg.infant_mortality"),
    SdgSeries("SH_DYN_NMRT", "un.sdg.neonatal_mortality"),
    SdgSeries("SH_HIV_INCD", "un.sdg.hiv_incidence", (("Age", "ALLAGE"),)),
    SdgSeries("SH_TBS_INCD", "un.sdg.tuberculosis_incidence"),
    SdgSeries("SH_DTH_NCOM", "un.sdg.ncd_mortality"),
    SdgSeries("SH_STA_SCIDE", "un.sdg.suicide_rate"),
    SdgSeries("SH_STA_TRAF", "un.sdg.road_traffic_deaths"),
    SdgSeries("SH_STA_ASAIRP", "un.sdg.air_pollution_mortality"),
    SdgSeries("SP_DYN_ADKL", "un.sdg.adolescent_birth_rate"),
    SdgSeries("SH_ACS_DTP3", "un.sdg.dtp3_coverage"),
    SdgSeries("SH_ACS_UNHC_25", "un.sdg.uhc_service_coverage"),
    SdgSeries("SE_TOT_CPLR", "un.sdg.completion_rate", (("Location", "ALLAREA"), ("Quantile", "_T"))),
    SdgSeries("SE_PRE_PARTN", "un.sdg.pre_primary_participation"),
    SdgSeries("SG_GEN_PARL", "un.sdg.women_in_parliament"),
    SdgSeries("SP_DYN_MRBF18", "un.sdg.child_marriage_18"),
    SdgSeries("SH_H2O_SAFE", "un.sdg.safe_drinking_water"),
    SdgSeries("SH_SAN_SAFE", "un.sdg.safe_sanitation"),
    SdgSeries("EG_ACS_ELEC", "un.sdg.electricity_access"),
    SdgSeries("EG_EGY_CLEAN", "un.sdg.clean_cooking"),
    SdgSeries("EG_FEC_RNEW", "un.sdg.renewable_energy_share"),
    SdgSeries("NY_GDP_PCAP", "un.sdg.gdp_per_capita_growth"),
    SdgSeries("SL_TLF_UEM", "un.sdg.unemployment_rate"),
    SdgSeries("SL_TLF_NEET", "un.sdg.youth_neet"),
    SdgSeries("GB_XPD_RSDV", "un.sdg.rd_expenditure"),
    SdgSeries("EN_ATM_CO2", "un.sdg.co2_fuel_combustion"),
    SdgSeries("EN_ATM_PM25", "un.sdg.pm25_exposure"),
    SdgSeries("EN_LND_SLUM", "un.sdg.urban_slum_population"),
    SdgSeries("AG_LND_FRST", "un.sdg.forest_area_share"),
    SdgSeries("ER_MRN_MPA", "un.sdg.marine_kba_protected"),
    SdgSeries("VC_IHR_PSRC", "un.sdg.homicide_rate"),
    SdgSeries("VC_DTH_TOTR", "un.sdg.conflict_deaths_rate"),
    SdgSeries("SG_REG_BRTH", "un.sdg.birth_registration"),
    SdgSeries("IT_USE_ii99", "un.sdg.internet_users"),
    SdgSeries("IT_NET_BBND", "un.sdg.fixed_broadband"),
    SdgSeries("SM_POP_REFG_OR", "un.sdg.refugees_by_origin_rate"),
)
SDG_BY_CODE = {s.code: s for s in SDG_SERIES}
#: Dimension codes that denote "total" (the headline cell); used for concept hints only.
SDG_TOTAL_CODES = {"BOTHSEX", "ALLAGE", "ALLAREA", "_T", "TOTAL", "ANYS", "ANNUAL"}


def sdg_series_list_item() -> FetchItem:
    return FetchItem(key="sdg:series_list", url=f"{SDG_API}Series/List?allreleases=false", max_bytes=10_000_000)


def sdg_data_url(series: SdgSeries, page: int = 1) -> str:
    url = f"{SDG_API}Series/Data?seriesCode={series.code}&pageSize={SDG_PAGE_SIZE}&page={page}"
    if series.filters:
        dims = [{"name": d, "values": [c]} for d, c in series.filters]
        url += "&dimensions=" + quote(json.dumps(dims, separators=(",", ":")), safe="")
    return url


def sdg_data_item(series: SdgSeries, page: int = 1) -> FetchItem:
    return FetchItem(
        key=f"sdg:{series.code}:p{page}",
        url=sdg_data_url(series, page),
        max_bytes=SDG_PAGE_MAX_BYTES,
        params={"series": series.code, "page": page},
    )


def _json_body(body: bytes, what: str) -> Any:
    _reject_html(body)
    try:
        return json.loads(body)
    except ValueError as e:
        raise _err(f"{what}: malformed JSON", kind="malformed_json") from e


def sdg_page_meta(body: bytes, series: str) -> dict[str, int]:
    doc = _json_body(body, f"SDG {series}")
    if not isinstance(doc, dict) or "data" not in doc:
        raise _err(f"SDG {series}: response is not an observation page")
    try:
        return {k: int(doc[k]) for k in ("totalElements", "totalPages", "pageNumber", "size")}
    except (KeyError, TypeError, ValueError) as e:
        raise _err(f"SDG {series}: page metadata missing") from e


def sdg_follow_ups(item: FetchItem, raw: RawAcquisition) -> list[FetchItem]:
    code = item.params.get("series")
    if not code or item.params.get("page") != 1:
        return []
    meta = sdg_page_meta(raw.body, code)
    pages = meta["totalPages"]
    if pages > SDG_MAX_PAGES_PER_SERIES:
        raise _err(f"SDG {code}: {pages} pages exceed the cap {SDG_MAX_PAGES_PER_SERIES}", kind="size_cap")
    return [sdg_data_item(SDG_BY_CODE[code], p) for p in range(2, pages + 1)]


def _num_attr(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "null", "none"):
        return None
    out, _ = strict_decimal(s)
    return out if out is not None else s[:100]


def _sdg_year(r: Mapping[str, Any]) -> int:
    t = r.get("timePeriodStart")
    try:
        d = Decimal(str(t))
    except InvalidOperation as e:
        raise _err(f"SDG timePeriodStart {t!r} is not a number") from e
    if d != d.to_integral_value() or not (1900 <= d <= 2100):
        raise _err(f"SDG timePeriodStart {t!r} is not a calendar year")
    return int(d)


def _sdg_concept_hints(buf: RowBuffer, row_dims: Sequence[tuple[int, Mapping[str, Any], int]], concept: str) -> None:
    """Concept hint on the series' headline cells: every dimension (other than Reporting Type) is a
    total code or has a single value in the whole series (e.g. Sex=FEMALE for maternal mortality)."""
    values: dict[str, set[Any]] = defaultdict(set)
    for _i, dims, _n in row_dims:
        for k, v in dims.items():
            values[k].add(v)
    for i, dims, n in row_dims:
        if n == 1 and all(
            v in SDG_TOTAL_CODES or len(values[k]) == 1 for k, v in dims.items() if k != "Reporting Type"
        ):
            buf.cols["concept_id"][i] = concept


def build_sdg(pages: Mapping[str, RawAcquisition], series_list: RawAcquisition | None) -> TablePlan:
    """All fetched SDG pages -> one atlas table. A series with a missing page is left out (reported)."""
    listing: dict[str, dict[str, Any]] = {}
    if series_list is not None:
        doc = _json_body(series_list.body, "SDG series list")
        if not isinstance(doc, list):
            raise _err("SDG series list is not a JSON array")
        listing = {str(x.get("code")): x for x in doc if isinstance(x, dict)}
    by_series: dict[str, dict[int, RawAcquisition]] = defaultdict(dict)
    for raw in pages.values():
        by_series[str(raw.item.params["series"])][int(raw.item.params["page"])] = raw
    buf = RowBuffer(SDG_SOURCE, SDG_DATASET)
    report: dict[str, Any] = {"series": {}, "incomplete_series": [], "missing_series": [], "not_in_series_list": []}
    stats: Counter[str] = Counter()
    for s in SDG_SERIES:
        got = by_series.get(s.code)
        if not got:
            report["missing_series"].append(s.code)
            continue
        if s.code not in listing and listing:
            report["not_in_series_list"].append(s.code)
        meta = sdg_page_meta(got[min(got)].body, s.code) if 1 in got else None
        if meta is None or sorted(got) != list(range(1, meta["totalPages"] + 1)):
            report["incomplete_series"].append(
                {"series": s.code, "pages": sorted(got), "expected": meta["totalPages"] if meta else None}
            )
            continue
        rows_seen = 0
        start = len(buf)
        row_dims: list[tuple[int, dict[str, Any], int]] = []
        local_keys: Counter[tuple[str, str, int]] = Counter()
        for page in sorted(got):
            raw = got[page]
            sha = raw.raw_sha256 or hashlib.sha256(raw.body).hexdigest()
            doc = _json_body(raw.body, f"SDG {s.code} page {page}")
            data = doc.get("data")
            if not isinstance(data, list):
                raise _err(f"SDG {s.code} page {page}: 'data' is not a list")
            for i, r in enumerate(data):
                rows_seen += 1
                if r.get("series") != s.code:
                    raise _err(f"SDG {s.code} page {page} record {i}: series {r.get('series')!r}")
                dims = r.get("dimensions") or {}
                for d, c in s.filters:
                    if dims.get(d) != c:
                        raise _err(f"SDG {s.code}: record outside the requested filter {d}={c}: {dims.get(d)!r}")
                geo = m49_code(r.get("geoAreaCode"))
                year = _sdg_year(r)
                indicator = s.code + "".join(f"|{k}={dims[k]}" for k in sorted(dims))
                if len(indicator) > 200:
                    raise _err(f"SDG {s.code}: indicator code too long ({len(indicator)})")
                attrs = r.get("attributes") or {}
                raw_value = r.get("value")
                value, changed = strict_decimal(str(raw_value)) if raw_value is not None else (None, False)
                flags: dict[str, Any] = {
                    "series_description": (r.get("seriesDescription") or "")[:300],
                    "sdg_indicator": r.get("indicator"),
                    "geo_label": r.get("geoAreaName"),
                    "nature": attrs.get("Nature"),
                    "release": (listing.get(s.code) or {}).get("release"),
                }
                for k_src, k_dst in (
                    ("UnitMultiplier", "unit_multiplier"),
                    ("Observation Status", "observation_status"),
                    ("Source Type", "source_type"),
                ):
                    if attrs.get(k_src) not in (None, ""):
                        flags[k_dst] = attrs[k_src]
                for k_src, k_dst in (
                    ("lowerBound", "lower_bound"),
                    ("upperBound", "upper_bound"),
                    ("basePeriod", "base_period"),
                ):
                    nv = _num_attr(r.get(k_src))
                    if nv is not None:
                        flags[k_dst] = nv
                for k_src, k_dst in (("time_detail", "time_detail"), ("timeCoverage", "time_coverage")):
                    if r.get(k_src) not in (None, ""):
                        flags[k_dst] = str(r[k_src])[:100]
                if r.get("source"):
                    flags["provider_source"] = str(r["source"])[:600]
                foot = [str(x)[:500] for x in (r.get("footnotes") or []) if x]
                if foot:
                    flags["footnotes"] = foot[:5]
                if r.get("valueType") not in (None, "Float"):
                    flags["value_type"] = r.get("valueType")
                text_val = None
                if value is None:
                    txt = "" if raw_value is None else str(raw_value).strip()
                    if txt == "" or txt.lower() in ("nan", "null"):
                        stats["empty_values_omitted"] += 1
                        continue
                    text_val = txt[:MAX_TEXT]
                    flags["non_numeric"] = True
                    stats["text_values"] += 1
                elif changed:
                    flags["provider_text"] = str(raw_value)
                    stats["respelled_values"] += 1
                local_keys[(indicator, geo, year)] += 1
                n = local_keys[(indicator, geo, year)]
                if n > 1:
                    # The database holds several records for one cell (different source/nature).
                    # Kept, not merged: the extra ones get an occurrence suffix and are flagged.
                    indicator = f"{indicator}|dup={n}"
                    flags["duplicate_cell_in_source"] = n
                    stats["duplicate_cells"] += 1
                row_dims.append((len(buf), dims, n))
                buf.add(
                    indicator=indicator,
                    provider_geo=geo,
                    period=year_bounds(year),
                    value=value,
                    value_text=text_val,
                    unit=str(attrs.get("Units") or "UNSPECIFIED"),
                    flags={k: v for k, v in flags.items() if v is not None},
                    raw_sha256=sha,
                    selector={"item": raw.item.key, "record": i, "series": s.code},
                    concept_id=None,
                )
        if meta and rows_seen != meta["totalElements"]:
            # A page changed under us (the database is live): drop this series' rows, report it.
            for col in buf.cols.values():
                del col[start:]
            report["incomplete_series"].append({"series": s.code, "rows": rows_seen, "expected": meta["totalElements"]})
            continue
        _sdg_concept_hints(buf, row_dims, s.concept)
        report["series"][s.code] = {
            "records": rows_seen,
            "cells": len(buf) - start,
            "release": (listing.get(s.code) or {}).get("release"),
        }
    report.update(dict(stats))
    if len(buf) == 0:
        raise _err("no complete SDG series")
    left_out = [x["series"] for x in report["incomplete_series"]] + report["missing_series"]
    note = (
        f"UN SDG Global Database API v5, {len(report['series'])} headline series (all disaggregations folded "
        "into the indicator: '<series>|<dimension>=<code>|...'); provider_geo = M49 geoAreaCode; regions kept "
        "with iso3=null. Bounds, nature, footnotes and custodian source in flags."
        + (f" Left out (incomplete fetch): {', '.join(left_out)}." if left_out else "")
    )
    return TablePlan(
        SDG_SOURCE, SDG_DATASET, buf.frame(), sorted({r for r in buf.cols["raw_sha256"]}), note, report=report
    )


# ============================================================================= UNHCR
UNHCR_SOURCE = "unhcr"
UNHCR_HOST = "api.unhcr.org"
UNHCR_API = "https://api.unhcr.org/population/v1/"
UNHCR_LIMIT = 10000
UNHCR_FIRST_YEAR = 1951
UNHCR_FIELDS: dict[str, str] = {
    "refugees": "Refugees under UNHCR's mandate",
    "asylum_seekers": "Asylum-seekers",
    "returned_refugees": "Returned refugees",
    "idps": "Internally displaced persons of concern to UNHCR",
    "returned_idps": "Returned IDPs",
    "stateless": "Stateless persons",
    "ooc": "Others of concern to UNHCR",
    "oip": "Other people in need of international protection",
    "hst": "Host community",
}
UNHCR_VIEWS = {
    "bilateral": ("pop_bilateral", "coo_all=true&coa_all=true"),
    "by_asylum": ("pop_by_asylum", "coa_all=true"),
    "by_origin": ("pop_by_origin", "coo_all=true"),
}
UNHCR_CONCEPTS = {
    ("pop_by_asylum", "refugees"): "un.unhcr.refugees_hosted",
    ("pop_by_asylum", "asylum_seekers"): "un.unhcr.asylum_seekers_hosted",
    ("pop_by_asylum", "stateless"): "un.unhcr.stateless",
    ("pop_by_origin", "refugees"): "un.unhcr.refugees_originating",
    ("pop_by_origin", "asylum_seekers"): "un.unhcr.asylum_seekers_originating",
    ("pop_by_origin", "idps"): "un.unhcr.idps",
}


def unhcr_years_item() -> FetchItem:
    return FetchItem(key="unhcr:years", url=f"{UNHCR_API}years/", max_bytes=1_000_000)


def unhcr_item(view: str, year_from: int, year_to: int, page: int = 1) -> FetchItem:
    q = UNHCR_VIEWS[view][1]
    span = f"{year_from}" if year_from == year_to else f"{year_from}-{year_to}"
    return FetchItem(
        key=f"unhcr:{view}:{span}:p{page}",
        url=f"{UNHCR_API}population/?limit={UNHCR_LIMIT}&page={page}&yearFrom={year_from}&yearTo={year_to}&{q}",
        max_bytes=20_000_000,
        params={"view": view, "year_from": year_from, "year_to": year_to, "page": page},
    )


def unhcr_years(body: bytes) -> list[int]:
    doc = _json_body(body, "UNHCR years")
    try:
        years = sorted({int(x["year"]) for x in doc["items"]})
    except (KeyError, TypeError, ValueError) as e:
        raise _err("UNHCR years: unexpected shape") from e
    return years


def unhcr_follow_ups(item: FetchItem, raw: RawAcquisition, *, max_year: int) -> list[FetchItem]:
    """Years list -> one bilateral request per year + the two totals views; page 1 -> further pages."""
    if item.key == "unhcr:years":
        years = [y for y in unhcr_years(raw.body) if UNHCR_FIRST_YEAR <= y <= max_year]
        if not years:
            raise _err("UNHCR years list holds no usable year")
        lo, hi = years[0], years[-1]
        return [unhcr_item("bilateral", y, y) for y in years] + [
            unhcr_item("by_asylum", lo, hi),
            unhcr_item("by_origin", lo, hi),
        ]
    p = item.params
    if p.get("page") != 1:
        return []
    doc = _json_body(raw.body, item.key)
    pages = int(doc.get("maxPages") or 1)
    if pages > 50:
        raise _err(f"{item.key}: {pages} pages exceed the cap", kind="size_cap")
    return [unhcr_item(p["view"], p["year_from"], p["year_to"], k) for k in range(2, pages + 1)]


def build_unhcr(raws: Mapping[str, RawAcquisition], *, max_year: int) -> list[TablePlan]:
    plans: list[TablePlan] = []
    groups: dict[str, dict[tuple[int, int], dict[int, RawAcquisition]]] = defaultdict(lambda: defaultdict(dict))
    for raw in raws.values():
        p = raw.item.params
        if "view" in p:
            groups[p["view"]][(p["year_from"], p["year_to"])][p["page"]] = raw
    for view, (dataset, _q) in UNHCR_VIEWS.items():
        spans = groups.get(view)
        if not spans:
            continue
        buf = RowBuffer(UNHCR_SOURCE, dataset)
        stats: Counter[str] = Counter()
        shas: set[str] = set()
        incomplete: list[str] = []
        seen: set[tuple[str, str, str, int]] = set()
        for span in sorted(spans):
            pages = spans[span]
            first = _json_body(pages[min(pages)].body, f"UNHCR {view} {span}") if 1 in pages else None
            want = int(first.get("maxPages") or 1) if isinstance(first, dict) else None
            if want is None or sorted(pages) != list(range(1, want + 1)):
                incomplete.append(f"{span[0]}-{span[1]}")
                continue
            for page in sorted(pages):
                raw = pages[page]
                sha = raw.raw_sha256 or hashlib.sha256(raw.body).hexdigest()
                doc = _json_body(raw.body, raw.item.key)
                items = doc.get("items")
                if not isinstance(items, list):
                    raise _err(f"{raw.item.key}: 'items' is not a list")
                shas.add(sha)
                for i, r in enumerate(items):
                    year = int(r["year"])
                    if not (span[0] <= year <= span[1]) or year > max_year:
                        raise _err(f"{raw.item.key} record {i}: year {year} outside the request")
                    if view == "by_origin":
                        geo, label, other = _unhcr_geo(r, "coo"), r.get("coo_name"), None
                    else:
                        geo, label = _unhcr_geo(r, "coa"), r.get("coa_name")
                        other = _unhcr_geo(r, "coo") if view == "bilateral" else None
                    for fname, flabel in UNHCR_FIELDS.items():
                        v = r.get(fname)
                        if isinstance(v, bool) or v is None:
                            stats["null_cells_omitted"] += 1
                            continue
                        if isinstance(v, str):
                            stats[
                                f"string_{'dash' if v.strip() == '-' else 'zero' if v.strip() == '0' else 'other'}_omitted"
                            ] += 1
                            continue
                        if not isinstance(v, int):
                            raise _err(f"{raw.item.key} record {i}: {fname} is {type(v).__name__}")
                        key = (fname, geo, other or "", year)
                        if key in seen:
                            raise _err(f"UNHCR {view}: duplicate cell {key}")
                        seen.add(key)
                        flags: dict[str, Any] = {
                            "time_reference": "end of year (stock at 31 December)",
                            "label": flabel,
                            "geo_label": (label or "").strip(),
                            "unhcr_code": r.get("coo" if view == "by_origin" else "coa"),
                        }
                        indicator = fname
                        if other is not None:
                            indicator = f"{fname}|origin={other}"
                            flags.update(
                                {
                                    "counterpart_role": "origin",
                                    "counterpart_provider_geo": other,
                                    "counterpart_iso3": geo_iso3(other, "iso3"),
                                    "counterpart_label": (r.get("coo_name") or "").strip(),
                                    "counterpart_unhcr_code": r.get("coo"),
                                }
                            )
                        buf.add(
                            indicator=indicator,
                            provider_geo=geo,
                            period=year_bounds(year),
                            value=str(v),
                            unit="persons",
                            flags=flags,
                            raw_sha256=sha,
                            selector={"item": raw.item.key, "record": i, "field": fname},
                            concept_id=UNHCR_CONCEPTS.get((dataset, fname)),
                        )
        if incomplete:
            # Never register a partial table: the missing years would read as "no refugees".
            plans.append(
                TablePlan(
                    UNHCR_SOURCE,
                    dataset,
                    pl.DataFrame(),
                    sorted(shas),
                    "",
                    report={"refused": "incomplete", "missing_spans": incomplete},
                )
            )
            continue
        if len(buf) == 0:
            continue
        side = {
            "bilateral": "country of asylum (counterpart origin in the indicator)",
            "by_asylum": "country of asylum",
            "by_origin": "country of origin",
        }[view]
        note = (
            f"UNHCR population statistics ({view}): persons at end of year by {side}; provider_geo = UNHCR ISO3 "
            "field (STA/XXA stateless, UKN/UNK unknown and TIB kept unmapped). Cells the API renders as the "
            "string '0' or '-' (no figure) are omitted, never stored as zero."
        )
        plans.append(
            TablePlan(
                UNHCR_SOURCE,
                dataset,
                buf.frame(),
                sorted(shas),
                note,
                geo_scheme="iso3",
                report={"cells": len(buf), **dict(stats)},
            )
        )
    return plans


def _unhcr_geo(r: Mapping[str, Any], side: str) -> str:
    iso = (r.get(f"{side}_iso") or "").strip()
    if iso:
        return iso
    code = (r.get(side) or "").strip()
    if not code:
        raise _err(f"UNHCR record without {side} code")
    return f"unhcr:{code}"


# ============================================================================= UNSD SYB
SYB_SOURCE = "unsd"
SYB_DATASET = "syb68_t02"
SYB_HOST = "data.un.org"
SYB_FILE = "SYB68_1_202511_Population, Surface Area and Density.csv"
SYB_URL = "https://data.un.org/_Docs/SYB/CSV/" + quote(SYB_FILE, safe=",")
SYB_SERIES: dict[str, tuple[str, str | None]] = {
    "Population mid-year estimates (millions)": ("millions", None),
    "Population mid-year estimates for males (millions)": ("millions", None),
    "Population mid-year estimates for females (millions)": ("millions", None),
    "Sex ratio (males per 100 females)": ("males per 100 females", None),
    "Population aged 0 to 14 years old (percentage)": ("percentage", None),
    "Population aged 60+ years old (percentage)": ("percentage", None),
    "Population density": ("persons per square km", None),
    "Surface area (thousand km2)": ("thousand km2", "un.unsd.surface_area"),
}


def build_syb(body: bytes, raw_sha256: str) -> TablePlan:
    _reject_html(body)
    text, enc = decode_text(body)
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3 or rows[1][:5] != ["Region/Country/Area", "", "Year", "Series", "Value"]:
        raise _err("SYB table header changed", header=rows[1][:7] if len(rows) > 1 else None)
    title = rows[0][1] if len(rows[0]) > 1 else ""
    buf = RowBuffer(SYB_SOURCE, SYB_DATASET)
    stats: Counter[str] = Counter()
    unknown_series: Counter[str] = Counter()
    for i, r in enumerate(rows[2:], start=3):
        if not any(c.strip() for c in r):
            continue
        if len(r) < 7:
            raise _err(f"SYB line {i}: {len(r)} fields")
        code, label, year, series, raw_v, foot, src = (c.strip() for c in r[:7])
        if series not in SYB_SERIES:
            unknown_series[series] += 1
            unit = series[series.rfind("(") + 1 : -1] if series.endswith(")") else "as labelled"
            concept = None
        else:
            unit, concept = SYB_SERIES[series]
        if not re.fullmatch(r"[0-9]{4}", year):
            raise _err(f"SYB line {i}: year {year!r}")
        val, changed = strict_decimal(raw_v)
        flags: dict[str, Any] = {"geo_label": label, "table": title}
        if foot:
            flags["footnote"] = foot[:1000]
        if src:
            flags["provider_source"] = src[:600]
        if series.startswith("Surface area"):
            flags["rounding"] = "published in whole thousand km2 (0 = under 500 km2)"
        text_val = None
        if val is None:
            if raw_v in ("", "..", "...", "-"):
                stats["missing_omitted"] += 1
                continue
            text_val, flags["non_numeric"] = raw_v[:MAX_TEXT], True
        elif changed:
            flags["provider_text"] = raw_v
            stats["respelled_values"] += 1
        buf.add(
            indicator=series,
            provider_geo=m49_code(code),
            period=year_bounds(int(year)),
            value=val,
            value_text=text_val,
            unit=unit,
            flags=flags,
            raw_sha256=raw_sha256,
            selector={"file": SYB_FILE, "line": i, "column": "Value"},
            concept_id=concept,
        )
    if len(buf) == 0:
        raise _err("SYB table has no data rows")
    report = {
        "file": SYB_FILE,
        "encoding": enc,
        "cells": len(buf),
        "unknown_series": dict(unknown_series),
        **dict(stats),
    }
    note = (
        "UNSD Statistical Yearbook 68 (Nov 2025) table T02: surface area (thousand km2, 2023, from the "
        "Demographic Yearbook 2023; whole thousands, so small territories read 0 or 1) and WPP-derived "
        "population/density series (same lineage as un_wpp). Thousands separators removed (provider_text kept)."
    )
    return TablePlan(SYB_SOURCE, SYB_DATASET, buf.frame(), [raw_sha256], note, report=report)


# ============================================================================= sources
@dataclass(frozen=True)
class UnConnector:
    """One provider source: its atlas source spec, fetch plan, follow-ups and table builders."""

    name: str
    source_key: str
    atlas_source_key: str
    provider: str
    licence: str
    hosts: tuple[str, ...]
    datasets: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    cadence_days: int
    planned_requests: int
    max_response_bytes: int = 52_428_800
    min_interval_seconds: float = 1.0

    def spec(self) -> Any:
        from reva.atlas.sources import AtlasSourceSpec

        return AtlasSourceSpec(
            name=self.name,
            version=CONNECTOR_VERSION,
            source_key=self.source_key,
            atlas_source=self.atlas_source_key,
            provider=self.provider,
            licence=self.licence,
            allowed_hosts=self.hosts,
            datasets=self.datasets,
            dataset_ids=self.dataset_ids,
            cadence_seconds=self.cadence_days * 86400,
            planned_requests=self.planned_requests,
            max_response_bytes=self.max_response_bytes,
            min_interval_seconds=self.min_interval_seconds,
            parser_version=PARSER_VERSION,
            code_digest=code_digest(),
            field_mappings={"geo_scheme": GEO_SCHEME, "module": "reva.adapters.connectors.atlas_un"},
        )

    # -- plan
    def plan(self, wants: Callable[[str], bool] = lambda _d: True) -> list[FetchItem]:
        if self.name == WPP_CONNECTOR.name:
            out = []
            for f in WPP_FILES:
                ds = (f.dataset, f"{f.dataset}_proj") if f is WPP_AGE5 else (f.dataset,)
                if any(wants(d) for d in ds):
                    out.append(
                        FetchItem(
                            key=f.key,
                            url=f.url,
                            max_bytes=f.max_bytes,
                            expected_media_types=("application/x-gzip", "application/gzip", "application/octet-stream"),
                        )
                    )
            return out
        if self.name == IMS_CONNECTOR.name:
            return [
                FetchItem(
                    key="ims:table1",
                    url=IMS_URL,
                    max_bytes=30_000_000,
                    expected_media_types=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
                )
            ]
        if self.name == SDG_CONNECTOR.name:
            return [sdg_series_list_item(), *(sdg_data_item(s) for s in SDG_SERIES)]
        if self.name == UNHCR_CONNECTOR.name:
            return [unhcr_years_item()]
        if self.name == SYB_CONNECTOR.name:
            return [FetchItem(key="syb:t02", url=SYB_URL, max_bytes=20_000_000, expected_media_types=("text/csv",))]
        raise ValueError(self.name)

    def follow_ups(self, item: FetchItem, raw: RawAcquisition, *, max_year: int) -> list[FetchItem]:
        if self.name == SDG_CONNECTOR.name:
            return sdg_follow_ups(item, raw)
        if self.name == UNHCR_CONNECTOR.name:
            return unhcr_follow_ups(item, raw, max_year=max_year)
        return []

    # -- build
    def build_tables(self, raws: Mapping[str, RawAcquisition], *, max_year: int) -> Iterator[TablePlan]:
        """Table plans from archived raws (``raw.raw_sha256`` set). Lazy: one table in memory at a time."""

        def sha(r: RawAcquisition) -> str:
            return r.raw_sha256 or hashlib.sha256(r.body).hexdigest()

        if self.name == WPP_CONNECTOR.name:
            for f in WPP_FILES:
                if f.key in raws:
                    yield from WPP_BUILDERS[f.key](raws[f.key].body, sha(raws[f.key]))
        elif self.name == IMS_CONNECTOR.name:
            if "ims:table1" in raws:
                yield build_ims(raws["ims:table1"].body, sha(raws["ims:table1"]))
        elif self.name == SDG_CONNECTOR.name:
            pages = {k: r for k, r in raws.items() if "series" in r.item.params}
            if pages:
                yield build_sdg(pages, raws.get("sdg:series_list"))
        elif self.name == UNHCR_CONNECTOR.name:
            yield from build_unhcr(raws, max_year=max_year)
        elif self.name == SYB_CONNECTOR.name:
            if "syb:t02" in raws:
                yield build_syb(raws["syb:t02"].body, sha(raws["syb:t02"]))


WPP_CONNECTOR = UnConnector(
    name="atlas.un_wpp",
    source_key="un.wpp",
    atlas_source_key=WPP_SOURCE,
    provider="UN DESA Population Division",
    licence="CC BY 3.0 IGO",
    hosts=(WPP_HOST,),
    datasets=(*(f.dataset for f in WPP_FILES), f"{WPP_AGE5.dataset}_proj"),
    dataset_ids=tuple(f.file for f in WPP_FILES),
    cadence_days=180,
    planned_requests=3,
    max_response_bytes=max(f.max_bytes for f in WPP_FILES),
)
IMS_CONNECTOR = UnConnector(
    name="atlas.un_ims",
    source_key="un.desa_ims",
    atlas_source_key=IMS_SOURCE,
    provider="UN DESA Population Division",
    licence="CC BY 3.0 IGO",
    hosts=(IMS_HOST,),
    datasets=(IMS_DATASET,),
    dataset_ids=(IMS_FILE,),
    cadence_days=365,
    planned_requests=1,
    max_response_bytes=30_000_000,
)
SDG_CONNECTOR = UnConnector(
    name="atlas.un_sdg",
    source_key="un.sdg",
    atlas_source_key=SDG_SOURCE,
    provider="UN Statistics Division (SDG Global Database)",
    licence="UNdata terms of use (free use with attribution)",
    hosts=(SDG_HOST,),
    datasets=(SDG_DATASET,),
    dataset_ids=tuple(s.code for s in SDG_SERIES),
    cadence_days=90,
    planned_requests=1 + len(SDG_SERIES),
    max_response_bytes=SDG_PAGE_MAX_BYTES,
    min_interval_seconds=2.0,
)
UNHCR_CONNECTOR = UnConnector(
    name="atlas.unhcr",
    source_key="unhcr.population",
    atlas_source_key=UNHCR_SOURCE,
    provider="UNHCR",
    licence="CC BY 4.0",
    hosts=(UNHCR_HOST,),
    datasets=tuple(v[0] for v in UNHCR_VIEWS.values()),
    dataset_ids=("population/v1",),
    cadence_days=90,
    planned_requests=80,
    max_response_bytes=20_000_000,
)
SYB_CONNECTOR = UnConnector(
    name="atlas.unsd_syb",
    source_key="unsd.syb",
    atlas_source_key=SYB_SOURCE,
    provider="UN Statistics Division",
    licence="UNdata terms of use (free use with attribution)",
    hosts=(SYB_HOST,),
    datasets=(SYB_DATASET,),
    dataset_ids=(SYB_FILE,),
    cadence_days=365,
    planned_requests=1,
    max_response_bytes=20_000_000,
)
CONNECTORS: dict[str, UnConnector] = {
    c.name: c for c in (WPP_CONNECTOR, IMS_CONNECTOR, SDG_CONNECTOR, UNHCR_CONNECTOR, SYB_CONNECTOR)
}


def atlas_source() -> list[Any]:
    """The five UN atlas sources (ATLASCORE protocol, ``reva.atlas.sources``)."""
    return [c.spec() for c in CONNECTORS.values()]


def latest_complete_year() -> int:
    """Latest complete calendar year: UNHCR end-year stocks of the running year do not exist yet."""
    return date.today().year - 1


# ============================================================================= ingest
def _trim_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    cov = summary.get("coverage") or {}
    unmapped = summary.get("unmapped_provider_geo") or []
    return {
        **{k: summary.get(k) for k in ("dataset_key", "artifact_sha256", "manifest_sha256", "row_count", "created")},
        **{
            k: cov.get(k)
            for k in ("countries", "indicators", "period_min", "period_max", "numeric_cells", "text_cells")
        },
        "unmapped_provider_geo_count": len(unmapped),
        "unmapped_rows": sum(u["rows"] for u in unmapped),
        "unmapped_provider_geo": unmapped,
    }


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch (``run.get``: budget-metered live client or fixture client) -> archive (``run.archive``)
    -> parse -> register (``run.register``) every wanted atlas table of ``run.spec``.

    Failed items are reported with a typed reason and the run continues; a table whose inputs are
    incomplete is not registered (except SDG, where each series is complete or left out and named
    in the table's coverage note). A budget refusal propagates (the platform reports
    ``budget_exhausted`` and keeps what was archived/registered).
    """
    from reva.operations.budgets import BudgetExhausted

    connector = CONNECTORS[run.spec.name]
    max_year = latest_complete_year()
    report: dict[str, Any] = {
        "connector": connector.name,
        "origin": run.origin,
        "acquisitions": {},
        "tables": {},
        "errors": [],
        "table_reports": {},
    }
    queue = list(connector.plan(run.wants))
    raws: dict[str, RawAcquisition] = {}
    while queue:
        item = queue.pop(0)
        try:
            raw = await run.get(
                item.url,
                key=item.key,
                media_types=item.expected_media_types,
                max_bytes=item.max_bytes,
                params=item.params,
            )
            _reject_html(raw.body)
        except BudgetExhausted:
            raise
        except Exception as e:  # typed source errors (timeouts, HTTP status, fixture miss) are reported
            report["errors"].append(
                {
                    "item": item.key,
                    "stage": "fetch",
                    "error": type(e).__name__,
                    "kind": getattr(e, "details", {}).get("kind"),
                    "detail": str(getattr(e, "message", e))[:300],
                }
            )
            continue
        sha = await asyncio.to_thread(run.archive, raw)
        raw = raw.model_copy(update={"raw_sha256": sha})
        raws[item.key] = raw
        report["acquisitions"][item.key] = {
            "raw_sha256": sha,
            "bytes": len(raw.body),
            "retrieved_at": raw.retrieved_at.isoformat(),
        }
        try:
            queue.extend(connector.follow_ups(item, raw, max_year=max_year))
        except SourceResponseError as e:
            report["errors"].append(
                {"item": item.key, "stage": "follow_up", "error": e.details.get("kind"), "detail": e.message[:300]}
            )
    try:
        for plan in connector.build_tables(raws, max_year=max_year):
            if not run.wants(plan.dataset):
                continue
            report["table_reports"][plan.dataset] = plan.report
            if plan.frame.height == 0:
                report["errors"].append(
                    {
                        "table": plan.dataset,
                        "stage": "build",
                        "error": "refused",
                        "detail": json.dumps(plan.report)[:300],
                    }
                )
                continue
            summary = await asyncio.to_thread(
                run.register,
                dataset=plan.dataset,
                frame=plan.frame,
                raw_sha256s=plan.raw_sha256s,
                coverage_note=plan.coverage_note[:MAX_TEXT],
                geo_scheme=plan.geo_scheme,
            )
            report["tables"][plan.dataset] = _trim_summary(summary)
            del plan
    except SourceResponseError as e:
        report["errors"].append({"stage": "build", "error": e.details.get("kind"), "detail": e.message[:500]})
    return report


__all__ = [
    "CONNECTORS",
    "GEO_SCHEME",
    "IMS_CONNECTOR",
    "SDG_CONNECTOR",
    "SDG_SERIES",
    "SYB_CONNECTOR",
    "UNHCR_CONNECTOR",
    "WPP_CONNECTOR",
    "TablePlan",
    "UnConnector",
    "Xlsx",
    "atlas_ingest",
    "atlas_source",
    "build_ims",
    "build_sdg",
    "build_syb",
    "build_unhcr",
    "build_wpp_age5",
    "build_wpp_demo",
    "build_wpp_variants",
    "strict_decimal",
]
