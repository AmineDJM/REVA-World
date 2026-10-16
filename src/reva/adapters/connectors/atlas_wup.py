"""UN World Urbanization Prospects 2025 -> atlas tables (V3, agent A-CITIES).

Source: UN DESA Population Division, *World Urbanization Prospects: The 2025 Revision* (released
2025-11-18; supersedes WUP 2018), ``https://population.un.org/wup/``. Licence stated in every WUP 2025
workbook (``info`` sheet): "Copyright © 2025 by United Nations, made available under a Creative Commons
license CC BY 3.0 IGO". Estimates 1950-2025, projections 2026-2050; consistent with WPP 2024 totals.

Files (bulk CSV/XLSX, one request each):

* ``WUP2025-DB-National-Definitions-Population-Data.csv.gz`` (3.4 MB) -> ``un_wup.wup2025_urban_rural``:
  urban / rural / total population (mid-year, thousands), percentage, annual rate of change, by **national
  definitions** of "urban" (the WUP 2018 concept), 1950-2050, countries and UN aggregates.
* ``WUP2025-DB-DEGURBA-Level1-Population-Surface-Data.csv.gz`` (8.5 MB) -> ``un_wup.wup2025_degurba``: the
  harmonised **Degree of Urbanisation** (cities / towns / rural areas, plus "Cities and Towns" and total):
  population, share, land area, built-up area, built-up area per capita (areas from 1975).
* ``WUP2025-DB-DEGURBA-Cities-Population-Surface-Data.csv.gz`` (58.7 MB) -> ``un_wup.wup2025_cities``: every
  DEGURBA city (>= 50,000 inhabitants in a given year; 16,828 city codes) 1975-2050: population, land area,
  built-up area, density; population-weighted centroid of the latest estimate year. WUP 2025 replaces the
  WUP 2018 "urban agglomerations >= 300,000" list (1950-2035), which is not ingested (superseded, legacy
  ``.xls`` workbooks).
* ``WUP2025-F20-DEGURBA-Capital_Cities.xlsx`` (63 kB) -> ``un_wup.wup2025_capitals``: population of every
  capital in mid-2025 with its capital type (national capital, seat of government, territorial capital ...);
  several capitals of one country are all kept.

Honesty: values as reported (exact text; plain re-spellings normalised with the original kept); years >= 2026
are projections — a separate indicator suffix ``|proj=wup2025`` plus ``flags.projection`` — so an estimate
concept can never return a projected value; UN aggregates are kept with ``iso3 = null``. The provider's
indicator list gives "thousands" as unit of the mid-year ``AREA_km2``/``BU_km2`` columns, but those columns
hold square kilometres (equal to the 1 January km² columns for the same rows): the unit is recorded as
``km2`` and the discrepancy is flagged (``provider_unit_metadata``).
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from typing import Any

import polars as pl

from reva.adapters.connectors.atlas_un import STRICT_DECIMAL, decode_text, gunzip_bounded, strict_decimal
from reva.adapters.connectors.base import module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.un_wup"
CONNECTOR_VERSION = "1.0.0"
SOURCE_KEY = "un.wup"
PARSER_VERSION = "atlas-un-wup/1"
ATLAS_SOURCE = "un_wup"
REVISION = "WUP 2025"
PROJ_SUFFIX = "|proj=wup2025"
FIRST_PROJECTION_YEAR = 2026  # WUP 2025 FAQ: estimates 1950-2025, projections up to 2050
GEO_SCHEME = "un_m49x"  # A-UN's scheme: crosswalk M49 + 412 -> XKX, 830 -> CHI

HOST = "population.un.org"
BASE = f"https://{HOST}/wup/assets/Download"
DS_URBAN = "wup2025_urban_rural"
DS_DEGURBA = "wup2025_degurba"
DS_CITIES = "wup2025_cities"
DS_CAPITALS = "wup2025_capitals"
DS_CITIES_AREA = "wup2025_cities_area"
DATASETS = (DS_URBAN, DS_DEGURBA, DS_CITIES, DS_CITIES_AREA, DS_CAPITALS)
FILES = {
    DS_URBAN: "Countries%20and%20Aggregates/WUP2025-DB-National-Definitions-Population-Data.csv.gz",
    DS_DEGURBA: "Countries%20and%20Aggregates/WUP2025-DB-DEGURBA-Level1-Population-Surface-Data.csv.gz",
    DS_CITIES: "Cities/WUP2025-DB-DEGURBA-Cities-Population-Surface-Data.csv.gz",
    DS_CAPITALS: "Cities/WUP2025-F20-DEGURBA-Capital_Cities.xlsx",
}
FILES[DS_CITIES_AREA] = FILES[DS_CITIES]  # one raw file, two tables
MAX_BYTES = 100_000_000
MAX_DECOMPRESSED = 1_500_000_000
LICENCE = "CC BY 3.0 IGO"
RIGHTS = {
    "licence": LICENCE,
    "licence_url": "http://creativecommons.org/licenses/by/3.0/igo/",
    "attribution": "United Nations, Department of Economic and Social Affairs, Population Division (2025). "
    "World Urbanization Prospects: The 2025 Revision, Online Edition.",
}

#: column -> (unit, definition note) for the national-definitions file
URBAN_COLUMNS = {
    "Pop": ("thousands", "population at mid-year"),
    "percPop": ("%", "percentage of the total population at mid-year"),
    "Pop_rate": ("%", "annual rate of change of the population"),
}
URBAN_CATEGORIES = ("Urban", "Rural", "Total")
DEGURBA_COLUMNS = {
    "Pop": ("thousands", "population at mid-year"),
    "percPop": ("%", "percentage of the total population at mid-year"),
    "AREA_km2": ("km2", "land area at mid-year"),
    "percAREA_km2": ("%", "percentage of the land area at mid-year"),
    "BU_km2": ("km2", "built-up area at mid-year"),
    "BU_m2_per_capita": ("m2 per person", "built-up area per capita at mid-year"),
}
DEGURBA_CATEGORIES = ("Rural", "Towns", "Cities", "Cities and Towns", "Total")
CITY_COLUMNS = {
    "Pop": ("thousands", "population at mid-year"),
    "AREA_km2": ("km2", "land area at mid-year"),
    "BU_km2": ("km2", "built-up area at mid-year"),
    "Pop_density": ("persons per km2", "population per km2 of land area at mid-year"),
}
#: the provider's own indicator list gives "thousands" for these mid-year area columns (see module doc)
UNIT_METADATA_ISSUE = {"AREA_km2", "BU_km2"}
URBAN_ID = ("LocID", "Location", "Notes", "LocType", "LocTypeName", "Category", "Year")
CITY_ID = ("LocID", "Location", "Notes", "City_Code", "City_Name", "Capital", "Year", "Pop_plausibility")
CAPITAL_HEADER = (
    "Index",
    "LocID",
    "Location",
    "Notes",
    "ISO3_Code",
    "ISO2_Code",
    "City_Code",
    "City_Name",
    "Type_of_Settlement",
    "Capital_Type",
    "PWCent_Latitude",
    "PWCent_Longitude",
    "Pop2025",
)


class WupError(SourceResponseError):
    pass


def _err(msg: str, **details: Any) -> WupError:
    return WupError(msg, retryable=False, kind="schema_drift", **details)


def url_of(dataset: str) -> str:
    return f"{BASE}/{FILES[dataset]}"


def file_name(dataset: str) -> str:
    return FILES[dataset].rsplit("/", 1)[-1]


# ------------------------------------------------------------------------------------------ reading
def read_csv(body: bytes, *, file: str, required: Sequence[str]) -> pl.DataFrame:
    """A WUP CSV(.gz) as an all-string frame with a 0-based ``record`` index."""
    if body[:512].lstrip().lower().startswith((b"<!doctype", b"<html")):
        raise _err(f"{file}: HTML page received instead of data")
    data = gunzip_bounded(body, MAX_DECOMPRESSED) if body[:2] == b"\x1f\x8b" else body
    text, _enc = decode_text(data)
    del data
    try:
        frame = pl.read_csv(io.StringIO(text), infer_schema=False, has_header=True)
    except pl.exceptions.PolarsError as e:
        raise _err(f"{file}: not a readable CSV ({type(e).__name__})") from e
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise _err(f"{file}: missing columns {missing}", columns=frame.columns[:40])
    bad = frame.filter(~pl.col("Year").str.contains(r"^[0-9]{4}$") | ~pl.col("LocID").str.contains(r"^[0-9]+$"))
    if bad.height:
        raise _err(f"{file}: non-numeric Year/LocID in record {bad.with_row_index('i')['i'][0]}")
    return frame.with_row_index("record")


def _long(frame: pl.DataFrame, *, value_columns: Sequence[str], index: Sequence[str]) -> pl.DataFrame:
    long = frame.select(["record", *index, *value_columns]).unpivot(
        on=list(value_columns), index=["record", *index], variable_name="column", value_name="value"
    )
    long = long.filter(pl.col("value").is_not_null() & (pl.col("value").str.strip_chars() != ""))
    bad = long.filter(~pl.col("value").str.contains(STRICT_DECIMAL.pattern))
    if bad.height == 0:
        return long.with_columns(pl.lit(None, dtype=pl.String).alias("provider_text"))
    fixes: dict[str, str] = {}
    for v in bad["value"].unique().to_list():
        out, _ = strict_decimal(v)
        if out is None:
            raise _err(f"non-numeric value {v[:40]!r} in a numeric column")
        fixes[v] = out
    return long.with_columns(
        pl.when(pl.col("value").is_in(list(fixes))).then(pl.col("value")).otherwise(None).alias("provider_text"),
        pl.col("value").replace(fixes).alias("value"),
    )


def _year() -> pl.Expr:
    return pl.col("Year").cast(pl.Int32, strict=True)


def _projection() -> pl.Expr:
    return _year() >= FIRST_PROJECTION_YEAR


def _selector(file: str) -> pl.Expr:
    return pl.concat_str(
        [
            pl.lit('{"column":"'),
            pl.col("column"),
            pl.lit(f'","file":"{file}","record":'),
            pl.col("record").cast(pl.String),
            pl.lit("}"),
        ]
    )


def _flags(fields: Mapping[str, pl.Expr]) -> pl.Expr:
    """JSON object (keys sorted) from expressions; null fields are dropped by the encoder's caller."""
    return pl.struct([fields[k].alias(k) for k in sorted(fields)]).struct.json_encode()


def _atlas(
    long: pl.DataFrame,
    *,
    dataset: str,
    raw_sha256: str,
    file: str,
    indicator: pl.Expr,
    unit: pl.Expr,
    flags: pl.Expr,
    geo: pl.Expr | None = None,
) -> pl.DataFrame:
    from reva.atlas.tables import ATLAS_SCHEMA

    year = _year()
    return long.select(
        pl.lit(ATLAS_SOURCE).alias("source"),
        pl.lit(dataset).alias("dataset"),
        indicator.alias("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        (geo if geo is not None else pl.col("LocID")).alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(year, 1, 1).alias("period_start"),
        pl.date(year + 1, 1, 1).alias("period_end"),
        pl.col("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        unit.alias("unit"),
        flags.alias("flags"),
        pl.lit(raw_sha256).alias("raw_sha256"),
        _selector(file).alias("selector"),
    ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]


def _unit_expr(columns: Mapping[str, tuple[str, str]]) -> pl.Expr:
    return pl.col("column").replace_strict({k: v[0] for k, v in columns.items()}, return_dtype=pl.String)


def _common_flags(definition: str) -> dict[str, pl.Expr]:
    return {
        "wup_revision": pl.lit(REVISION),
        "urban_definition": pl.lit(definition),
        "projection": _projection(),
        "reference": pl.lit("mid-year (1 July)"),
        "provider_text": pl.col("provider_text"),
        "un_note": pl.col("Notes"),
        "unit_metadata_note": pl.when(pl.col("column").is_in(list(UNIT_METADATA_ISSUE)))
        .then(
            pl.lit("provider indicator list states 'thousands'; the column holds km2 (equal to its 1 Jan km2 column)")
        )
        .otherwise(None),
    }


def _drop_null_json(expr: pl.Expr) -> pl.Expr:
    """Remove ``"key":null`` members from a compact JSON object string."""
    return expr.str.replace_all(r',?"[a-z_0-9]+":null', "").str.replace(r"^\{,", "{")


# ------------------------------------------------------------------------------------------ tables
def build_urban_rural(body: bytes, raw_sha256: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    file = file_name(DS_URBAN)
    frame = read_csv(body, file=file, required=[*URBAN_ID, *URBAN_COLUMNS])
    skipped = frame.filter(~pl.col("Category").is_in(URBAN_CATEGORIES) | pl.col("Category").is_null())
    frame = frame.filter(pl.col("Category").is_in(URBAN_CATEGORIES))
    long = _long(frame, value_columns=list(URBAN_COLUMNS), index=URBAN_ID)
    ind = pl.concat_str(
        [
            pl.col("column"),
            pl.lit("|cat="),
            pl.col("Category"),
            pl.when(_projection()).then(pl.lit(PROJ_SUFFIX)).otherwise(pl.lit("")),
        ]
    )
    flags = _drop_null_json(
        _flags(
            {
                **_common_flags("national definitions of urban areas"),
                "loc_type": pl.col("LocTypeName"),
                "geo_label": pl.col("Location"),
            }
        )
    )
    out = _atlas(
        long,
        dataset=DS_URBAN,
        raw_sha256=raw_sha256,
        file=file,
        indicator=ind,
        unit=_unit_expr(URBAN_COLUMNS),
        flags=flags,
    )
    return out, {
        "records": frame.height,
        "skipped_records_without_category": skipped.height,
        "locations": frame["LocID"].n_unique(),
        "years": [int(frame["Year"].min()), int(frame["Year"].max())],
    }


def build_degurba(body: bytes, raw_sha256: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    file = file_name(DS_DEGURBA)
    frame = read_csv(body, file=file, required=[*URBAN_ID, *DEGURBA_COLUMNS])
    skipped = frame.filter(~pl.col("Category").is_in(DEGURBA_CATEGORIES) | pl.col("Category").is_null())
    frame = frame.filter(pl.col("Category").is_in(DEGURBA_CATEGORIES))
    long = _long(frame, value_columns=list(DEGURBA_COLUMNS), index=URBAN_ID)
    ind = pl.concat_str(
        [
            pl.col("column"),
            pl.lit("|cat="),
            pl.col("Category"),
            pl.when(_projection()).then(pl.lit(PROJ_SUFFIX)).otherwise(pl.lit("")),
        ]
    )
    flags = _drop_null_json(
        _flags(
            {
                **_common_flags("Degree of Urbanisation (level 1), UN/EC-JRC harmonised"),
                "loc_type": pl.col("LocTypeName"),
                "geo_label": pl.col("Location"),
            }
        )
    )
    out = _atlas(
        long,
        dataset=DS_DEGURBA,
        raw_sha256=raw_sha256,
        file=file,
        indicator=ind,
        unit=_unit_expr(DEGURBA_COLUMNS),
        flags=flags,
    )
    return out, {
        "records": frame.height,
        "skipped_records_without_category": skipped.height,
        "locations": frame["LocID"].n_unique(),
        "years": [int(frame["Year"].min()), int(frame["Year"].max())],
    }


def _read_cities(body: bytes) -> pl.DataFrame:
    file = file_name(DS_CITIES)
    frame = read_csv(body, file=file, required=[*CITY_ID, *CITY_COLUMNS, "PWCent_Latitude", "PWCent_Longitude"])
    dup = frame.group_by("City_Code", "Year").len().filter(pl.col("len") > 1)
    if dup.height:
        raise _err(f"{file}: city {dup['City_Code'][0]} has several rows for year {dup['Year'][0]}")
    ident = frame.group_by("City_Code").agg(pl.col("LocID").n_unique().alias("n"))
    if ident.filter(pl.col("n") > 1).height:
        raise _err(f"{file}: a city code appears under several locations")
    return frame


def _city_flags() -> pl.Expr:
    # kept short (3 M rows): the definitions are in the coverage note and the catalog
    return _drop_null_json(
        _flags(
            {
                "city": pl.col("City_Name"),
                "city_code": pl.col("City_Code"),
                "capital": pl.col("Capital") == "1",
                "pop_plausibility": pl.col("Pop_plausibility"),
                "projection": _projection(),
                "provider_text": pl.col("provider_text"),
                "un_note": pl.col("Notes"),
            }
        )
    )


def _city_frame(frame: pl.DataFrame, columns: Sequence[str], dataset: str, raw_sha256: str) -> pl.DataFrame:
    long = _long(frame, value_columns=list(columns), index=CITY_ID)
    ind = pl.concat_str(
        [
            pl.col("column"),
            pl.lit("|city="),
            pl.col("City_Code"),
            pl.when(_projection()).then(pl.lit(PROJ_SUFFIX)).otherwise(pl.lit("")),
        ]
    )
    return _atlas(
        long,
        dataset=dataset,
        raw_sha256=raw_sha256,
        file=file_name(DS_CITIES),
        indicator=ind,
        unit=_unit_expr(CITY_COLUMNS),
        flags=_city_flags(),
    )


def _city_report(frame: pl.DataFrame) -> dict[str, Any]:
    return {
        "records": frame.height,
        "city_codes": frame["City_Code"].n_unique(),
        "locations": frame["LocID"].n_unique(),
        "years": [int(frame["Year"].min()), int(frame["Year"].max())],
        "cities_2025": frame.filter(pl.col("Year") == "2025").height,
        "capital_city_codes": frame.filter(pl.col("Capital") == "1")["City_Code"].n_unique(),
        "estimate_records": frame.filter(_year() < FIRST_PROJECTION_YEAR).height,
    }


def build_cities(body: bytes, raw_sha256: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    """``wup2025_cities``: city population (mid-year) + population-weighted centroid."""
    frame = _read_cities(body)
    main = _city_frame(frame, ["Pop"], DS_CITIES, raw_sha256)
    # population-weighted centroid of each city's latest estimate year (or first projection year)
    yr = _year()
    ref = (
        frame.with_columns(yr.alias("_y"), (yr >= FIRST_PROJECTION_YEAR).alias("_p"))
        .sort(["City_Code", "_p", "_y"], descending=[False, False, True])
        .group_by("City_Code", maintain_order=True)
        .first()
        .drop("_y", "_p")
    )
    cen = _long(ref, value_columns=["PWCent_Latitude", "PWCent_Longitude"], index=CITY_ID)
    cen_ind = pl.concat_str(
        [
            pl.col("column").replace_strict({"PWCent_Latitude": "pwc_latitude", "PWCent_Longitude": "pwc_longitude"}),
            pl.lit("|city="),
            pl.col("City_Code"),
        ]
    )
    cen_flags = _drop_null_json(
        _flags(
            {
                "projection": _projection(),
                "city_code": pl.col("City_Code"),
                "city": pl.col("City_Name"),
                "provider_text": pl.col("provider_text"),
                "definition": pl.lit("population-weighted centroid of the city in this year (WGS84)"),
                "year_basis": pl.lit("latest estimate year of the city (first projection year if it has no estimate)"),
            }
        )
    )
    cen_frame = _atlas(
        cen,
        dataset=DS_CITIES,
        raw_sha256=raw_sha256,
        file=file_name(DS_CITIES),
        indicator=cen_ind,
        unit=pl.lit("degrees (WGS84)"),
        flags=cen_flags,
    )
    return pl.concat([main, cen_frame]), _city_report(frame)


def build_cities_area(body: bytes, raw_sha256: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    """``wup2025_cities_area``: city land area, built-up area and density (same raw file)."""
    frame = _read_cities(body)
    out = _city_frame(frame, ["AREA_km2", "BU_km2", "Pop_density"], DS_CITIES_AREA, raw_sha256)
    return out, _city_report(frame)


def build_capitals(body: bytes, raw_sha256: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError
    from reva.atlas.tables import ATLAS_SCHEMA

    file = file_name(DS_CAPITALS)
    try:
        wb = Workbook(body)
        rows = list(wb.rows("Data"))
        notes = {c[0].text: c[1].text for _, c in wb.rows("NOTES") if 0 in c and 1 in c}
        info = " ".join(c.text for _, cells in wb.rows("info") for c in cells.values())
    except XlsxError as e:
        raise _err(f"{file}: {e}") from e
    if "CC BY 3.0 IGO" not in info:
        raise _err(f"{file}: the licence statement 'CC BY 3.0 IGO' is no longer in the info sheet")
    header = tuple(rows[0][1][i].text for i in sorted(rows[0][1]))
    if header != CAPITAL_HEADER:
        raise _err(f"{file}: Data header changed: {header}")
    out: list[dict[str, Any]] = []
    national: dict[str, list[tuple[int, dict[str, str]]]] = {}
    count: dict[str, int] = {}
    code_rows: dict[str, int] = {}
    for _, cells in rows[1:]:
        if 6 in cells:
            code_rows[cells[6].text] = code_rows.get(cells[6].text, 0) + 1
    for rnum, cells in rows[1:]:
        r = {h: (cells[i].text if i in cells else "") for i, h in enumerate(CAPITAL_HEADER)}
        if not r["LocID"].isdigit() or not r["Index"].isdigit():
            raise _err(f"{file}: Data row {rnum} has no LocID/Index")
        geo = str(int(r["LocID"]))
        count[geo] = count.get(geo, 0) + 1
        # a capital that is no Degree-of-Urbanisation settlement has no City_Code (and no population)
        # (one settlement listed twice with two capital roles, e.g. Sri Lanka: the F20 row index is added)
        key = r["City_Code"] or f"index{r['Index']}"
        if r["City_Code"] and code_rows.get(r["City_Code"], 0) > 1:
            key = f"{r['City_Code']}.index{r['Index']}"
        if r["Capital_Type"].startswith("National capital"):
            national.setdefault(geo, []).append((rnum, r))
        base = {
            "wup_revision": REVISION,
            "capital_type": r["Capital_Type"],
            "settlement_type": r["Type_of_Settlement"],
            "city": r["City_Name"],
            "city_code": r["City_Code"] or None,
            "geo_label": r["Location"],
            "f20_index": r["Index"],
            "reference": "mid-2025",
            "definition": "Degree of Urbanisation settlement of the capital (city, town or village)",
        }
        if r["Notes"]:
            base["un_note"] = r["Notes"]
            if r["Notes"] in notes:
                base["un_note_text"] = notes[r["Notes"]][:1000]
        for col, ind, unit, val, txt in (
            ("Pop2025", f"Pop|capital={key}", "thousands", r["Pop2025"], None),
            ("City_Name", f"name|capital={key}", "1", None, r["City_Name"]),
            ("PWCent_Latitude", f"pwc_latitude|capital={key}", "degrees (WGS84)", r["PWCent_Latitude"], None),
            ("PWCent_Longitude", f"pwc_longitude|capital={key}", "degrees (WGS84)", r["PWCent_Longitude"], None),
        ):
            flags = dict(base)
            if val is not None:
                if not val:
                    continue
                v, changed = strict_decimal(val)
                if v is None:
                    raise _err(f"{file}: Data row {rnum} {col} is not a number: {val[:30]!r}")
                if changed:
                    flags["provider_text"] = val
                val = v
            elif not txt:
                continue
            out.append(_cap_row(ind, geo, val, txt, unit, flags, raw_sha256, file, rnum, col))
    for geo, caps in national.items():
        if len(caps) == 1:
            rnum, r = caps[0]
            v, _ = strict_decimal(r["Pop2025"]) if r["Pop2025"] else (None, False)
            if v is not None:
                out.append(
                    _cap_row(
                        "national_capital_pop",
                        geo,
                        v,
                        None,
                        "thousands",
                        {
                            "wup_revision": REVISION,
                            "city": r["City_Name"],
                            "city_code": r["City_Code"],
                            "settlement_type": r["Type_of_Settlement"],
                            "reference": "mid-2025",
                            "capital_type": r["Capital_Type"],
                            "derived": "the only row whose Capital_Type starts with 'National capital' "
                            "for this location",
                        },
                        raw_sha256,
                        file,
                        rnum,
                        "Pop2025",
                    )
                )
    for geo, n in count.items():
        out.append(
            _cap_row(
                "capital_rows",
                geo,
                str(n),
                None,
                "capitals",
                {
                    "wup_revision": REVISION,
                    "derived": "rows of F20 for this location (all capital types)",
                    "national_capital_rows": len(national.get(geo, [])),
                },
                raw_sha256,
                file,
                None,
                None,
            )
        )
    frame = pl.DataFrame(out, schema=ATLAS_SCHEMA)
    multi = {g: [r["City_Name"] for _, r in v] for g, v in national.items() if len(v) > 1}
    return frame, {
        "rows": len(rows) - 1,
        "locations": len(count),
        "several_national_capitals": multi,
        "capital_types": sorted({cells[9].text for _, cells in rows[1:] if 9 in cells}),
    }


def _cap_row(
    ind: str,
    geo: str,
    value: str | None,
    text: str | None,
    unit: str,
    flags: Mapping[str, Any],
    raw_sha256: str,
    file: str,
    rnum: int | None,
    col: str | None,
) -> dict[str, Any]:
    from datetime import date

    sel: dict[str, Any] = {"file": file, "sheet": "Data"}
    if rnum is not None:
        sel.update(row=rnum, column=col)
    else:
        sel["derivation"] = "rows of the location"
    return {
        "source": ATLAS_SOURCE,
        "dataset": DS_CAPITALS,
        "indicator": ind,
        "concept_id": None,
        "iso3": None,
        "provider_geo": geo,
        "entity_qid": None,
        "period_start": date(2025, 1, 1),
        "period_end": date(2026, 1, 1),
        "value": value,
        "value_text": text,
        "unit": unit,
        "flags": json.dumps(dict(flags), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        "raw_sha256": raw_sha256,
        "selector": json.dumps(sel, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    }


BUILDERS = {
    DS_URBAN: build_urban_rural,
    DS_DEGURBA: build_degurba,
    DS_CITIES: build_cities,
    DS_CITIES_AREA: build_cities_area,
    DS_CAPITALS: build_capitals,
}
COVERAGE = {
    DS_URBAN: "WUP 2025 urban/rural population by national definitions, 1950-2025 estimates, 2026-2050 "
    "projections (|proj=wup2025); countries + UN aggregates (iso3 null)",
    DS_DEGURBA: "WUP 2025 Degree of Urbanisation level 1 (cities, towns, rural): population, land and built-up "
    "area (areas from 1975); projections 2026-2050 flagged",
    DS_CITIES: "WUP 2025 Degree of Urbanisation cities (>= 50,000 inhabitants and >= 1,500 inhabitants per km2), "
    "1975-2050, population at mid-year in thousands (projections from 2026: |proj=wup2025, flags.projection) + "
    "population-weighted centroid (latest estimate year); flags.pop_plausibility as stated by the UN",
    DS_CITIES_AREA: "WUP 2025 Degree of Urbanisation cities: land area (km2), built-up area (km2), population "
    "density (persons per km2), 1975-2050 (projections from 2026 flagged); the provider indicator list gives "
    "'thousands' for the mid-year area columns, which hold km2",
    DS_CAPITALS: "WUP 2025 F20: population of capital cities in mid-2025, every capital type kept",
}


# ------------------------------------------------------------------------------------------ source
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="United Nations, Department of Economic and Social Affairs, Population Division",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(dict.fromkeys(file_name(d) for d in DATASETS)),
        cadence_seconds=90 * 86400,
        planned_requests=len(DATASETS),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(WupError),
        field_mappings={
            "geo": "LocID (M49, un_m49x)",
            "urban": sorted(URBAN_COLUMNS),
            "degurba": sorted(DEGURBA_COLUMNS),
            "cities": sorted(CITY_COLUMNS),
        },
        notes="WUP 2025 revision (2025-11-18); projections from 2026 carry |proj=wup2025",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """One request per WUP file (the cities file feeds two tables): fetch, archive, build, register.
    ``--dataset`` restricts the run to one table."""
    summary: dict[str, Any] = {"connector": CONNECTOR_NAME, "tables": {}, "reports": {}}
    fetched: dict[str, tuple[Any, str]] = {}
    for ds in DATASETS:
        if not run.wants(ds):
            continue
        url = url_of(ds)
        if url not in fetched:
            media = (
                ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",)
                if ds == DS_CAPITALS
                else ("application/x-gzip", "application/gzip")
            )
            raw = await run.get(url, key=file_name(ds), media_types=media, max_bytes=MAX_BYTES)
            fetched.clear()  # keep at most one raw body in memory
            fetched[url] = (raw, run.archive(raw))
        raw, sha = fetched[url]
        frame, report = BUILDERS[ds](raw.body, sha)
        t = run.register(
            dataset=ds, frame=frame, raw_sha256s=[sha], geo_scheme=GEO_SCHEME, rights=RIGHTS, coverage_note=COVERAGE[ds]
        )
        summary["tables"][ds] = {
            k: t.get(k)
            for k in ("dataset_key", "dataset_id", "artifact_sha256", "row_count", "created", "unmapped_provider_geo")
        }
        summary["reports"][ds] = report
        del frame
    return summary
