"""World Happiness Report — life evaluations and their explained components -> atlas table (V3, A-QOL).

Source: World Happiness Report "Data Sharing" page, https://worldhappiness.report/data-sharing/
(checked 2026-10-15): "The data we present in Figure 2.1 are available to download for free. This
includes: the three-year averages for life evaluation from 2012 onwards, the 95% confidence intervals
for those averages, the contributions of the six explanatory factors." Underlying microdata: Gallup
World Poll (proprietary; *not* ingested — only the free published aggregates are).

**Licence status (honest):** free download, but **no explicit open licence** is stated on the page
or in the file (no CC/ODbL text; checked the data-sharing page and FAQ on 2026-10-15). The table
is registered with ``rights.licence_status = "free_download_no_explicit_licence"`` and
``redistribution = "unclear"`` so publication layers can gate it; cite as
Helliwell, Layard, Sachs, De Neve, Aknin & Wang (eds.), World Happiness Report 2026.

File: ``WHR26_Data_Figure_2.1.xlsx`` (one sheet "Data for Figure 2.1 (2011–2025)"): one row per
(Year, country) with Rank, "Life evaluation (3-year average)", lower/upper whisker (95% CI), the six
"Explained by: …" contributions and "Dystopia + residual". Years 2011-2025 (no 2013: there was no
2014 report).

Time semantics: ``Year`` is the last survey year of the three-year window (WHR 2026 = Gallup
2023-2025; checked against WHR 2013 = 2010-2012 and WHR 2015 = 2012-2014 values in the file), so a
cell's period is ``[Year-2-01-01, Year+1-01-01)``. The 2011 rows (WHR 2012 edition) are **not**
three-year averages (the data page says "from 2012 onwards") and the file does not state their
window: they are excluded and counted in the report rather than given an invented period.
Explained-by contributions and whiskers are only published for recent editions; absent cells are
omitted (not zero).
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

from reva.adapters.connectors import atlas_qol_common as common
from reva.adapters.connectors.atlas_qol_common import AtlasBuild, NameIndex, canon, decimal_text
from reva.adapters.connectors.atlas_xlsx import Workbook
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

CONNECTOR_NAME = "atlas.whr"
CONNECTOR_VERSION = "1.1.0"  # 1.1.0: atlas source platform (metered run.get), same parser
PARSER_VERSION = "atlas_whr/1"
SOURCE_KEY = "whr.figure21"
SOURCE = "whr"
DATASET = "whr26_fig21"
HOST = "files.worldhappiness.report"
FILE = "WHR26_Data_Figure_2.1.xlsx"
URL = f"https://{HOST}/{FILE}"
LICENCE = "free download, no explicit open licence (https://worldhappiness.report/data-sharing/)"
ATTRIBUTION = (
    "Helliwell, J. F., Layard, R., Sachs, J. D., De Neve, J.-E., Aknin, L. B., & Wang, S. (Eds.). "
    "World Happiness Report 2026. Wellbeing Research Centre, University of Oxford. Data: Gallup World Poll."
)

#: header text -> (indicator, unit)
COLUMNS: dict[str, tuple[str, str]] = {
    "Rank": ("rank", "rank"),
    "Life evaluation (3-year average)": ("life_evaluation", "Cantril ladder score (0-10)"),
    "Lower whisker": ("life_evaluation_ci95_lower", "Cantril ladder score (0-10)"),
    "Upper whisker": ("life_evaluation_ci95_upper", "Cantril ladder score (0-10)"),
    "Explained by: Log GDP per capita": ("explained_by_log_gdp_per_capita", "ladder points above Dystopia"),
    "Explained by: Social support": ("explained_by_social_support", "ladder points above Dystopia"),
    "Explained by: Healthy life expectancy": ("explained_by_healthy_life_expectancy", "ladder points above Dystopia"),
    "Explained by: Freedom to make life choices": ("explained_by_freedom", "ladder points above Dystopia"),
    "Explained by: Generosity": ("explained_by_generosity", "ladder points above Dystopia"),
    "Explained by: Perceptions of corruption": (
        "explained_by_perceptions_of_corruption",
        "ladder points above Dystopia",
    ),
    "Dystopia + residual": ("dystopia_plus_residual", "Cantril ladder score (0-10)"),
}
#: WHR spellings that the crosswalk labels do not match (provider string -> crosswalk key, or None
#: when the territory is not a crosswalk entity; such rows are kept unmapped).
NAME_ALIASES: dict[str, str | None] = {
    "DR Congo": "COD",
    "Swaziland": "SWZ",  # former name of Eswatini (WHR's older editions)
    "Hong Kong SAR of China": "HKG",
    "Taiwan Province of China": None,  # not in the ALLC crosswalk (kept unmapped)
    "North Cyprus": None,  # not a crosswalk entity
    "Somaliland Region": None,  # not a crosswalk entity
}
FIRST_THREE_YEAR = 2012


class WhrParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def parse(body: bytes, raw_sha: str, names: NameIndex | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    names = names or NameIndex(NAME_ALIASES)
    wb = Workbook(body)
    if len(wb.sheets) < 1:
        raise WhrParseError(f"{FILE}: no sheet")
    sheet = next(iter(wb.sheets))
    table = wb.table(sheet)
    if not table:
        raise WhrParseError(f"{FILE}: empty sheet")
    header = [str(h).strip() if h is not None else "" for h in table[0]]
    for need in ("Year", "Country name", "Life evaluation (3-year average)"):
        if need not in header:
            raise WhrParseError(f"{FILE}: header lacks {need!r}")
    known = {h for h in header if h}
    unknown = known - set(COLUMNS) - {"Year", "Country name"}
    if unknown:
        raise WhrParseError(f"{FILE}: unknown columns {sorted(unknown)} (declare their unit first)")
    i_year, i_name = header.index("Year"), header.index("Country name")
    rows: list[dict[str, Any]] = []
    rep: dict[str, Any] = {"sheet": sheet, "excluded_rows_unknown_window": 0, "mapping": Counter(), "years": Counter()}
    unmapped: set[str] = set()
    for idx, rec in enumerate(table[1:], start=2):
        rec = rec + [None] * (len(header) - len(rec))
        y_txt, name = rec[i_year], rec[i_name]
        if y_txt is None and name is None:
            continue
        y = decimal_text(str(y_txt))
        if y is None or not name:
            raise WhrParseError(f"{FILE}: row {idx} without a numeric Year and a country name")
        year = int(float(y))
        if year < FIRST_THREE_YEAR:
            rep["excluded_rows_unknown_window"] += 1
            continue
        rep["years"][year] += 1
        name = str(name).strip()
        iso3, how = names.lookup(name)
        rep["mapping"][how] += 1
        if iso3 is None:
            unmapped.add(name)
        for col, cell in zip(header, rec, strict=True):
            if col not in COLUMNS or cell is None or str(cell).strip() == "":
                continue
            d = decimal_text(str(cell))
            if d is None:
                raise WhrParseError(f"{FILE}: row {idx} column {col!r}: {cell!r} is not a number")
            ind, unit = COLUMNS[col]
            if ind == "rank":
                d = decimal_text(d.split(".")[0]) if d.endswith(".0") else d
            flags: dict[str, Any] = {"averaging_window_years": [year - 2, year], "edition": f"WHR {year + 1}"}
            if iso3 is not None:
                flags["iso3_via"] = how
            if ind.startswith("explained_by") or ind == "dystopia_plus_residual":
                flags["note"] = "contribution to the ladder score relative to Dystopia (WHR Figure 2.1)"
            rows.append(
                {
                    "source": SOURCE,
                    "dataset": DATASET,
                    "indicator": ind,
                    "iso3": iso3,
                    "provider_geo": name,
                    "period_start": date(year - 2, 1, 1),
                    "period_end": date(year + 1, 1, 1),
                    "value": d,
                    "value_text": None,
                    "unit": unit,
                    "flags": canon(flags),
                    "raw_sha256": raw_sha,
                    "selector": canon(file=FILE, sheet=sheet, row=idx, column=col),
                }
            )
    rep["mapping"] = dict(rep["mapping"])
    rep["years"] = {str(k): v for k, v in sorted(rep["years"].items())}
    rep["unmapped_names"] = sorted(unmapped)
    return rows, rep


class WhrConnector:
    def describe(self) -> AtlasSourceSpec:
        return AtlasSourceSpec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            atlas_source=SOURCE,
            provider="World Happiness Report (Wellbeing Research Centre, Oxford; Gallup; UN SDSN)",
            licence=LICENCE,
            allowed_hosts=(HOST,),
            datasets=(DATASET,),
            dataset_ids=("WHR26_Data_Figure_2.1",),
            cadence_seconds=30 * common.DAY,
            planned_requests=1,
            max_response_bytes=5_000_000,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            code_digest=common.code_digest(sys.modules[__name__]),
            field_mappings={
                "atlas_tables": [f"{SOURCE}.{DATASET}"],
                "units": {v[0]: v[1] for v in COLUMNS.values()},
                "time_semantics": "three-year survey window ending in Year: [Year-2, Year+1)",
            },
            notes="Annual report (March); a new file name per edition.",
        )

    def fetch_items(self) -> tuple[FetchItem, ...]:
        return (
            FetchItem(
                key="figure21",
                url=URL,
                expected_media_types=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "application/octet-stream",
                ),
                max_bytes=5_000_000,
            ),
        )

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        return ()

    async def fetch(self, item: FetchItem, run: Any) -> RawAcquisition:
        return await common.fetch_plain(item, run)

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        raw = next((r for r in raws if r.item.key == "figure21"), None)
        if raw is None:
            raise WhrParseError("missing raw item figure21")
        sha = common.sha256_hex(raw.body)
        rows, rep = parse(raw.body, sha)
        return [
            AtlasBuild(
                source=SOURCE,
                dataset=DATASET,
                frame=common.frame(rows),
                raw_sha256s=[sha],
                parser_version=PARSER_VERSION,
                coverage_note=(
                    "WHR 2026 Figure 2.1 data: life evaluation 3-year averages (2010-2012 .. 2023-2025 windows), "
                    "95% CI whiskers, six explained-by contributions, Dystopia+residual. 2011 rows excluded "
                    f"(window not stated): {rep['excluded_rows_unknown_window']}. Unmapped: "
                    f"{', '.join(rep['unmapped_names'])}. Licence: free download, no explicit open licence."
                ),
                geo_scheme=None,
                rights={
                    "licence": LICENCE,
                    "licence_status": "free_download_no_explicit_licence",
                    "redistribution": "unclear",
                    "attribution": ATTRIBUTION,
                    "licence_checked": "2026-10-15",
                },
                report=rep,
            )
        ]


def atlas_source() -> AtlasSourceSpec:
    return WhrConnector().describe()


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Atlas platform entry point (``reva atlas ingest {CONNECTOR_NAME}``)."""
    return await common.run_connector(run, WhrConnector())


__all__ = [
    "CONNECTOR_NAME",
    "NAME_ALIASES",
    "WhrConnector",
    "atlas_ingest",
    "atlas_source",
    "parse",
]
