"""ILOSTAT labour atlas connector (V3, agent A-ILO).

Source
------
ILOSTAT, the ILO's statistical database. Licence **CC BY 4.0** for datasets and their referential
metadata since 2023-05-03 (ILO "Rights and permissions"); attribution "ILOSTAT, International Labour
Organization". Access (checked 2026-10-15): the ILOSTAT bulk/API service ``rplumber.ilo.org``.
The former bulk area ``www.ilo.org/ilostat-files/WEB_bulk_download`` redirects to
``webapps.ilo.org`` and answers 404. ``rplumber.ilo.org`` answers ``200`` with an **empty body** to
clients without a descriptive User-Agent (curl's default), so an empty body is treated as a failed
request, never as "no data".

One bulk request per indicator: ``/data/indicator/?id=<ID>&format=.csv.gz`` returns every reference
area and year in one gzip CSV (columns ``ref_area, source, indicator, sex?, classif1?, classif2?, time,
obs_value, obs_status?, note_classif?, note_indicator?, note_source?``). The service returns the
ILO-selected *best source* per (area, year); this was verified on every file used here (one source
per area-year; no duplicate cells). If a future file carries two sources for one cell, the table is
refused (typed ``ValidationProblem``), never silently deduplicated.

Code dictionaries (``/metadata/dic/?var=<var>``) give the labels of the source, observation status and
note codes; they are archived with each run and cited by every table, and the labels are copied into
each row's ``flags`` so a cell explains itself (e.g. ``"T30:110": "Currency: AFG - Afghani (AFN)"``,
``"I33:..": "ILO calculation: Hourly figure multiplied by 40 hours and 4.33 weeks"``).

Indicator codes: ILOSTAT renamed the earnings series. The owner's request named
``EAR_4MMN_CUR_NB`` (minimum wage) and ``EAR_4MTH_SEX_ECO_CUR_NB`` (mean monthly earnings); neither is
in the 2026-10 table of contents. Their current equivalents are ``EAR_INEE_CUR_NB_A`` ("Monthly minimum
wage by currency") and ``EAR_EMTA_SEX_ECO_CUR_NB_A`` / ``EAR_EMTA_SEX_CUR_NB_A`` ("Average monthly
earnings of employees by sex(, economic activity) and currency").

Atlas tables
------------
One table per indicator: ``source = "ilostat"``, ``dataset = <indicator id lower-case>``
(``ilostat.ear_inee_cur_nb_a``). ``indicator`` = the ILOSTAT indicator code with every classification
folded in verbatim: ``UNE_2EAP_SEX_AGE_RT|sex=SEX_T|classif1=AGE_YTHADULT_YGE15``. Geography: ILOSTAT
``ref_area`` through the ``ilo`` scheme (``registry/atlas/geo_aliases/ilo.yaml``: ISO3 + ``KOS``→XKX,
``CHA``→CHI); aggregates (``X..``), ``TWN`` and ``ANT`` stay unmapped and are listed in the manifest.

Estimate types are never mixed: the ``*_2*`` indicators and ``SDG_0111`` (working poverty; every row
sourced "ILO - Modelled Estimates") are **ILO modelled estimates** (harmonised, cover every country incl.
imputed ones; ``flags.estimate_type = "ilo_modelled"``; cells for years after the series vintage, e.g.
"Nov. 2025" in the TOC label, carry ``flags.projection``). The others are **national estimates** as
reported by countries (``"national"``); rows inside them whose source is an ILO estimate (regional
aggregates ``X..``; "ILO - SIALC Estimates" minimum wages for nine Latin American countries) are flagged
``"ilo_estimate_in_national_series"``. They are separate tables and separate catalog concepts.

Currencies: for ``CUR_TYPE_LCU`` rows the unit names the local currency taken from the row's own
``T30`` note (``"AFN per month"``); ``CUR_TYPE_USD`` and ``CUR_TYPE_PPP`` rows are the ILO's own
conversions (U.S. dollars; 2021 PPP $), labelled as such. This connector never converts a currency.

Not available from ILOSTAT (searched 2026-10-15, table of contents of 1,964 series): real wage growth
(the Global Wage Report series are not published as an ILOSTAT indicator).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import polars as pl

from reva.adapters.connectors import atlas_labour_common as common
from reva.adapters.connectors.atlas_labour_common import AtlasTablePlan, canon, gunzip_bounded, read_csv_text
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, ValidationProblem
from reva.ports.connectors import FetchItem, RawAcquisition

SOURCE = "ilostat"
SOURCE_KEY = "ilostat.rplumber"
HOST = "rplumber.ilo.org"
BASE = f"https://{HOST}"
CONNECTOR_NAME = "atlas.ilostat"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-ilostat-parse-1"
GEO_SCHEME = "ilo"
GZIP_MEDIA = ("application/octet-stream", "application/gzip", "application/x-gzip")
MAX_CSV_BYTES = 400_000_000  # decompressed size cap per file (largest used: ~70 MB)

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "terms_url": "https://www.ilo.org/node/9321",
    "attribution": "ILOSTAT, International Labour Organization (https://ilostat.ilo.org/data/)",
}

Estimate = Literal["ilo_modelled", "national"]


@dataclass(frozen=True)
class IloIndicator:
    id: str  # ILOSTAT indicator+frequency id, e.g. "UNE_2EAP_SEX_AGE_RT_A"
    estimate: Estimate
    unit: str  # unit of non-currency rows; currency rows get a per-row unit
    max_bytes: int
    label: str

    @property
    def code(self) -> str:
        return self.id[:-2] if self.id.endswith("_A") else self.id

    @property
    def dataset(self) -> str:
        return self.id.lower()


#: Every series requested, with the unit ILOSTAT declares in its label.
INDICATORS: tuple[IloIndicator, ...] = (
    # --- unemployment
    IloIndicator(
        "UNE_2EAP_SEX_AGE_RT_A",
        "ilo_modelled",
        "%",
        5_000_000,
        "Unemployment rate by sex and age -- ILO modelled estimates (%)",
    ),
    IloIndicator("UNE_DEAP_SEX_AGE_RT_A", "national", "%", 15_000_000, "Unemployment rate by sex and age (%)"),
    # --- labour force participation / employment-to-population
    IloIndicator(
        "EAP_2WAP_SEX_AGE_RT_A",
        "ilo_modelled",
        "%",
        15_000_000,
        "Labour force participation rate by sex and age -- ILO modelled estimates (%)",
    ),
    IloIndicator(
        "EAP_DWAP_SEX_AGE_RT_A", "national", "%", 20_000_000, "Labour force participation rate by sex and age (%)"
    ),
    IloIndicator(
        "EMP_2WAP_SEX_AGE_RT_A",
        "ilo_modelled",
        "%",
        5_000_000,
        "Employment-to-population ratio by sex and age -- ILO modelled estimates (%)",
    ),
    # --- employment structure
    IloIndicator(
        "EMP_2EMP_SEX_ECO_NB_A",
        "ilo_modelled",
        "thousands of persons",
        30_000_000,
        "Employment by sex and economic activity -- ILO modelled estimates (thousands)",
    ),
    IloIndicator(
        "EMP_2EMP_SEX_STE_NB_A",
        "ilo_modelled",
        "thousands of persons",
        15_000_000,
        "Employment by sex and status in employment -- ILO modelled estimates (thousands)",
    ),
    # --- informality
    IloIndicator(
        "SDG_0831_SEX_ECO_RT_A",
        "national",
        "%",
        5_000_000,
        "SDG 8.3.1: Proportion of informal employment in total employment by sex and economic activity (%)",
    ),
    IloIndicator(
        "EMP_2IFL_SEX_RT_A",
        "ilo_modelled",
        "%",
        1_000_000,
        "Informal employment rate by sex -- ILO modelled estimates (%)",
    ),
    # --- hours
    IloIndicator(
        "HOW_2EMP_SEX_NB_A",
        "ilo_modelled",
        "hours per week",
        2_000_000,
        "Average weekly hours actually worked per employed person by sex -- ILO modelled estimates",
    ),
    IloIndicator(
        "HOW_TEMP_SEX_NB_A",
        "national",
        "hours per week",
        2_000_000,
        "Average weekly hours actually worked per employed person by sex",
    ),
    # --- wages
    IloIndicator(
        "EAR_INEE_CUR_NB_A", "national", "per month (currency per row)", 2_000_000, "Monthly minimum wage by currency"
    ),
    IloIndicator(
        "EAR_EMTA_SEX_CUR_NB_A",
        "national",
        "per month (currency per row)",
        3_000_000,
        "Average monthly earnings of employees by sex and currency",
    ),
    IloIndicator(
        "EAR_EMTA_SEX_ECO_CUR_NB_A",
        "national",
        "per month (currency per row)",
        30_000_000,
        "Average monthly earnings of employees by sex, economic activity and currency",
    ),
    IloIndicator(
        "EAR_EMTM_SEX_CUR_NB_A",
        "national",
        "per month (currency per row)",
        2_000_000,
        "Median monthly earnings of employees by sex and currency",
    ),
    # --- income share, poverty, child labour, industrial relations
    IloIndicator(
        "LAP_2GDP_NOC_RT_A",
        "ilo_modelled",
        "% of GDP",
        1_000_000,
        "Labour income share as a percent of GDP -- ILO modelled estimates (%)",
    ),
    IloIndicator(
        "SDG_0111_SEX_AGE_RT_A",
        "ilo_modelled",
        "%",
        3_000_000,
        "SDG 1.1.1: Working poverty rate by sex and age (%) (every row's source is 'ILO - Modelled Estimates')",
    ),
    IloIndicator(
        "CLD_XCHL_SEX_AGE_RT_A", "national", "%", 1_000_000, "Share of children in child labour by sex and age (%)"
    ),
    IloIndicator("ILR_TUMT_NOC_RT_A", "national", "%", 500_000, "Trade union density rate (%)"),
    IloIndicator("ILR_CBCT_NOC_RT_A", "national", "%", 500_000, "Collective bargaining coverage rate (%)"),
)

#: Code dictionaries whose labels are copied into row flags.
DICTIONARIES: tuple[str, ...] = ("source", "obs_status", "note_indicator", "note_source", "note_classif")
DIMENSIONS: tuple[str, ...] = ("sex", "classif1", "classif2")
NOTE_COLUMNS: tuple[str, ...] = ("note_indicator", "note_source", "note_classif")
REQUIRED_COLUMNS: tuple[str, ...] = ("ref_area", "source", "indicator", "time", "obs_value")
CURRENCY_CODES = {"CUR_TYPE_LCU", "CUR_TYPE_USD", "CUR_TYPE_PPP"}
_CURRENCY_NOTE = re.compile(r"^Currency: .*\(([A-Z]{3})\)\s*$")
_YEAR = r"^[12][0-9]{3}$"


def data_url(indicator_id: str, ref_areas: Sequence[str] | None = None) -> str:
    q = f"id={indicator_id}"
    if ref_areas:
        q += "&ref_area=" + "+".join(ref_areas)
    return f"{BASE}/data/indicator/?{q}&format=.csv.gz"


def dictionary_url(var: str) -> str:
    return f"{BASE}/metadata/dic/?var={var}&lang=en&format=.csv.gz"


TOC_URL = f"{BASE}/metadata/toc/indicator/?lang=en&format=.csv.gz"


class IlostatConnector:
    """ILOSTAT bulk indicators -> one atlas table per indicator."""

    parser_version = PARSER_VERSION

    def __init__(self, indicators: Sequence[IloIndicator] = INDICATORS, ref_areas: Sequence[str] | None = None) -> None:
        self.indicators = tuple(indicators)
        # ref_areas restricts the requests (tests capture small real responses); None = every area
        self.ref_areas = tuple(ref_areas) if ref_areas else None

    # ------------------------------------------------------------------ descriptor
    def datasets_of(self, item: FetchItem) -> set[str] | None:
        """Atlas datasets that need ``item`` (``None``: every dataset, e.g. dictionaries / TOC)."""
        ind = item.params.get("indicator")
        return {ind.lower()} if ind else None

    def plan(self) -> list[FetchItem]:
        items = [
            FetchItem(
                key="toc",
                url=TOC_URL,
                expected_media_types=GZIP_MEDIA,
                max_bytes=5_000_000,
                params={"kind": "toc"},
                required=False,
            )
        ]
        items += [
            FetchItem(
                key=f"dic:{v}",
                url=dictionary_url(v),
                expected_media_types=GZIP_MEDIA,
                max_bytes=2_000_000,
                params={"kind": "dictionary", "var": v},
            )
            for v in DICTIONARIES
        ]
        items += [
            FetchItem(
                key=f"data:{i.id}",
                url=data_url(i.id, self.ref_areas),
                expected_media_types=GZIP_MEDIA,
                max_bytes=i.max_bytes,
                params={"kind": "indicator", "indicator": i.id},
                required=False,
            )
            for i in self.indicators
        ]
        return items

    # ------------------------------------------------------------------ parsing
    def build_tables(self, raws: Mapping[str, RawAcquisition]) -> list[AtlasTablePlan]:
        missing_dics = [v for v in DICTIONARIES if f"dic:{v}" not in raws]
        if missing_dics:
            raise SourceResponseError(
                "ILOSTAT code dictionaries missing; no table built", dictionaries=missing_dics, retryable=False
            )
        dicts = {v: parse_dictionary(raws[f"dic:{v}"].body, v) for v in DICTIONARIES}
        dic_shas = [_sha(raws[f"dic:{v}"]) for v in DICTIONARIES]
        toc = parse_toc(raws["toc"].body) if "toc" in raws else {}
        toc_sha = [_sha(raws["toc"])] if "toc" in raws else []
        plans = []
        for ind in self.indicators:
            raw = raws.get(f"data:{ind.id}")
            if raw is None:
                continue  # reported by the fetch step; no table, never an empty stand-in
            meta = toc.get(ind.id, {})
            frame = parse_indicator(raw.body, _sha(raw), ind, dicts, vintage=modelled_vintage(meta))
            note = (
                f"ILOSTAT {ind.id}: {ind.label}. Estimate type: {ind.estimate}. "
                f"ILOSTAT last update: {meta.get('last.update', 'n/a')}; TOC n.records {meta.get('n.records', 'n/a')}, "
                f"n.ref_area {meta.get('n.ref_area', 'n/a')}. Best source per area-year as selected by ILOSTAT."
            )
            if self.ref_areas:
                note += f" Restricted request (ref_area={'+'.join(self.ref_areas)})."
            plans.append(
                AtlasTablePlan(
                    source=SOURCE,
                    dataset=ind.dataset,
                    frame=frame,
                    raw_sha256s=tuple([_sha(raw), *dic_shas, *toc_sha]),
                    geo_scheme=GEO_SCHEME,
                    coverage_note=note,
                    rights=RIGHTS,
                )
            )
        return plans


#: Areas of the recorded offline fixtures (tests/fixtures/source/atlas.ilostat): in fixture mode the
#: connector requests exactly these restricted URLs, which is what was captured live on 2026-10-15.
FIXTURE_REF_AREAS: tuple[str, ...] = ("FRA", "DZA", "KOS", "AUT", "TWN", "X01")


def atlas_source() -> AtlasSourceSpec:
    """ATLASCORE atlas source declaration (``uv run python -m reva.atlas.ingest atlas.ilostat``)."""
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="International Labour Organization (ILOSTAT)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['terms_url']}); attribution: {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=tuple(i.dataset for i in INDICATORS),
        dataset_ids=tuple(i.id for i in INDICATORS),
        cadence_seconds=30 * 86400,
        planned_requests=1 + len(DICTIONARIES) + len(INDICATORS),
        max_response_bytes=max(i.max_bytes for i in INDICATORS),
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(common, _module()),
        field_mappings={
            "tables": {i.id: f"{SOURCE}.{i.dataset}" for i in INDICATORS},
            "estimate_type": {i.id: i.estimate for i in INDICATORS},
            "units": {i.id: i.unit for i in INDICATORS},
            "geo_scheme": GEO_SCHEME,
            "dictionaries": list(DICTIONARIES),
        },
        notes="One gzip CSV per ILOSTAT indicator (all areas, all years) + code dictionaries + TOC.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """ATLASCORE entry point: every ILOSTAT table (or ``run.dataset`` only)."""
    areas = FIXTURE_REF_AREAS if run.origin == "test_fixture" else None
    return await common.ingest_with_run(run, IlostatConnector(ref_areas=areas))


def _module() -> Any:
    import sys

    return sys.modules[__name__]


def _sha(raw: RawAcquisition) -> str:
    if raw.raw_sha256:
        return raw.raw_sha256
    import hashlib

    return hashlib.sha256(raw.body).hexdigest()


def parse_dictionary(body: bytes, var: str) -> dict[str, str]:
    """``code -> label`` of one ILOSTAT dictionary (first label wins; codes are unique per var)."""
    df = read_csv_text(gunzip_bounded(body, 50_000_000), what=f"ILOSTAT dictionary {var}")
    label_col = f"{var}.label"
    if var not in df.columns or label_col not in df.columns:
        raise SourceResponseError(
            f"ILOSTAT dictionary {var}: unexpected columns", columns=df.columns[:10], retryable=False
        )
    out: dict[str, str] = {}
    for code, label in df.select(var, label_col).iter_rows():
        if code is not None and code not in out:
            out[code] = label or ""
    if not out:
        raise SourceResponseError(f"ILOSTAT dictionary {var} is empty", retryable=False)
    return out


def parse_toc(body: bytes) -> dict[str, dict[str, str]]:
    df = read_csv_text(gunzip_bounded(body, 50_000_000), what="ILOSTAT table of contents")
    if "id" not in df.columns:
        raise SourceResponseError("ILOSTAT table of contents: no id column", retryable=False)
    keep = [
        c
        for c in ("id", "indicator.label", "last.update", "n.records", "n.ref_area", "data.start", "data.end")
        if c in df.columns
    ]
    return {r["id"]: r for r in df.select(keep).to_dicts()}


_VINTAGE = re.compile(r"ILO modelled estimates, ([A-Z][a-z]{2,8}\.? ([12][0-9]{3}))")


def modelled_vintage(meta: Mapping[str, str]) -> tuple[str, int] | None:
    """``("Nov. 2025", 2025)`` from a TOC label "... -- ILO modelled estimates, Nov. 2025 (%)"."""
    m = _VINTAGE.search(meta.get("indicator.label") or "")
    return (m.group(1), int(m.group(2))) if m else None


def _currency_unit(cur_type: str | None, local_currency: str | None) -> str:
    if cur_type == "CUR_TYPE_LCU":
        return f"{local_currency} per month" if local_currency else "local currency per month (currency not stated)"
    if cur_type == "CUR_TYPE_USD":
        return "USD per month (ILOSTAT conversion from local currency)"
    if cur_type == "CUR_TYPE_PPP":
        return "2021 PPP $ per month (ILOSTAT conversion)"
    raise ValidationProblem("unknown ILOSTAT currency classification", classif=cur_type)


def _flags_and_unit(
    ind: IloIndicator,
    combo: Mapping[str, Any],
    dicts: Mapping[str, Mapping[str, str]],
    cur_col: str | None,
    vintage: tuple[str, int] | None = None,
) -> tuple[str, str]:
    flags: dict[str, Any] = {"estimate_type": ind.estimate}
    src = combo.get("source")
    if src:
        flags["ilo_source"] = src
        flags["ilo_source_label"] = label = dicts["source"].get(src, "(code not in ILOSTAT dictionary)")
        if ind.estimate == "national" and label.startswith("ILO - "):
            # e.g. ILO regional aggregates or "ILO - SIALC Estimates" inside a national-estimates series
            flags["estimate_type"] = "ilo_estimate_in_national_series"
    status = combo.get("obs_status")
    if status:
        flags["obs_status"] = status
        flags["obs_status_label"] = dicts["obs_status"].get(status, "(code not in ILOSTAT dictionary)")
    notes: dict[str, str] = {}
    for col in NOTE_COLUMNS:
        v = combo.get(col)
        if v:
            for code in v.split("_"):
                notes[code] = dicts[col].get(code, "(code not in ILOSTAT dictionary)")
    local = None
    for code, label in notes.items():
        if code.startswith("T30:"):
            m = _CURRENCY_NOTE.match(label)
            if m:
                local = m.group(1)
    if notes:
        flags["notes"] = notes
    unit = ind.unit
    if cur_col is not None:
        cur = str(combo.get(cur_col))
        unit = _currency_unit(cur, local)
        flags["currency_basis"] = {
            "CUR_TYPE_LCU": "local currency as reported",
            "CUR_TYPE_USD": "converted to U.S. dollars by ILOSTAT",
            "CUR_TYPE_PPP": "converted to 2021 PPP $ by ILOSTAT",
        }[cur]
        if local:
            flags["local_currency"] = local
    if combo.get("_after_vintage") and vintage is not None:
        flags["projection"] = (
            f"reference year after the series vintage ({vintage[0]}): an ILO projection, not an observation"
        )
    if combo.get("_null"):
        flags["missing"] = "unreliable" if status == "U" else "not_reported"
        if status:
            flags["provider_flag"] = status
    text = canon(flags)
    if len(text) > 3900:  # flags column limit is 4000 chars: keep codes, drop long labels
        flags["notes"] = {k: "" for k in notes}
        flags["notes_labels_dropped"] = "label text too long; see ILOSTAT note dictionaries (raw_sha256s)"
        text = canon(flags)
    return text, unit


def parse_indicator(
    body: bytes,
    raw_sha: str,
    ind: IloIndicator,
    dicts: Mapping[str, Mapping[str, str]],
    *,
    vintage: tuple[str, int] | None = None,
) -> pl.DataFrame:
    """One ILOSTAT indicator gzip CSV -> atlas frame (not yet validated/mapped)."""
    df = read_csv_text(gunzip_bounded(body, MAX_CSV_BYTES), what=f"ILOSTAT {ind.id}")
    miss = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if miss:
        raise SourceResponseError(
            f"ILOSTAT {ind.id}: missing columns", missing=miss, columns=df.columns[:15], retryable=False
        )
    codes = df["indicator"].unique().to_list()
    if codes != [ind.code]:
        raise SourceResponseError(f"ILOSTAT {ind.id}: unexpected indicator codes", found=codes[:5], retryable=False)
    bad_time = df.filter(~pl.col("time").str.contains(_YEAR))
    if bad_time.height:
        raise SourceResponseError(
            f"ILOSTAT {ind.id}: non-annual time values", examples=bad_time["time"].head(3).to_list(), retryable=False
        )
    df = df.with_row_index("_row", offset=1)
    dims = [d for d in DIMENSIONS if d in df.columns]
    cur_col = next(
        (
            d
            for d in ("classif1", "classif2")
            if d in dims and set(df[d].drop_nulls().unique().to_list()) <= CURRENCY_CODES and df[d].null_count() == 0
        ),
        None,
    )

    # value: strict decimal text (verbatim when already strict)
    vals = df["obs_value"].str.strip_chars()
    df = df.with_columns(vals.alias("obs_value"))
    odd = df.filter(
        pl.col("obs_value").is_not_null()
        & (pl.col("obs_value") != "")
        & ~pl.col("obs_value").str.contains(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
    )
    fix = {v: common.strict_decimal(v) for v in odd["obs_value"].unique().to_list()}
    value = pl.when(pl.col("obs_value") == "").then(None).otherwise(pl.col("obs_value"))
    if fix:
        value = value.replace(fix)
    df = df.with_columns(value.alias("_value"))
    df = df.with_columns(
        pl.col("_value").is_null().alias("_null"),
        (
            pl.col("time").cast(pl.Int32) > vintage[1]
            if (vintage is not None and ind.estimate == "ilo_modelled")
            else pl.lit(False)
        ).alias("_after_vintage"),
    )

    # flags + unit, computed once per distinct (source, status, notes, currency, null) combination
    combo_cols = ["source", "_null", "_after_vintage"] + [c for c in ("obs_status", *NOTE_COLUMNS) if c in df.columns]
    if cur_col:
        combo_cols.append(cur_col)
    combos = df.select(combo_cols).unique()
    rows = combos.to_dicts()
    fu = [_flags_and_unit(ind, r, dicts, cur_col, vintage) for r in rows]
    combos = combos.with_columns(
        pl.Series("flags", [f for f, _ in fu], dtype=pl.String), pl.Series("unit", [u for _, u in fu], dtype=pl.String)
    )
    df = df.join(combos, on=combo_cols, how="left", nulls_equal=True)

    parts: list[pl.Expr] = [pl.col("indicator")]
    for d in dims:
        parts.append(pl.when(pl.col(d).is_null()).then(pl.lit("")).otherwise(pl.lit(f"|{d}=") + pl.col(d)))
    year = pl.col("time").cast(pl.Int32)
    out = df.select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(ind.dataset).alias("dataset"),
        pl.concat_str(parts).alias("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        pl.col("ref_area").alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(year, 1, 1).alias("period_start"),
        pl.date(year + 1, 1, 1).alias("period_end"),
        pl.col("_value").alias("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        pl.col("unit"),
        pl.col("flags"),
        pl.lit(raw_sha).alias("raw_sha256"),
        pl.concat_str([pl.lit(f'{{"file":"{ind.id}.csv","row":'), pl.col("_row").cast(pl.String), pl.lit("}")]).alias(
            "selector"
        ),
    )
    keys = ["indicator", "provider_geo", "period_start"]
    if out.select(pl.struct(keys).is_duplicated().any()).item():
        raise ValidationProblem(
            f"ILOSTAT {ind.id}: more than one source for one cell; refusing to choose",
            examples=out.filter(pl.struct(keys).is_duplicated()).select(keys).head(3).to_dicts(),
        )
    return out


__all__ = [
    "CONNECTOR_NAME",
    "DICTIONARIES",
    "FIXTURE_REF_AREAS",
    "INDICATORS",
    "RIGHTS",
    "SOURCE",
    "SOURCE_KEY",
    "IloIndicator",
    "IlostatConnector",
    "atlas_ingest",
    "atlas_source",
    "parse_dictionary",
    "parse_indicator",
]
