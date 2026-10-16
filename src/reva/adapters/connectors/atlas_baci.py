"""CEPII BACI bilateral trade by product -> trade atlas tables (V3, agent A-TRADE).

Source
------
BACI ("Base pour l'Analyse du Commerce International"), CEPII: one reconciled value per
(year, exporter, importer, HS 6-digit product), ~200 countries. **Licence: Etalab Open Licence 2.0**
(BACI page, checked 2026-10-15: "Licence: Etalab 2.0"); cite Gaulier & Zignago (2010), CEPII WP
2010-23, and "CEPII BACI, version 202601". Release used: **V202601** (released 2026-01-22, page
"Last update: January 30, 2026"), nomenclature **HS92** (1995-2024, the longest consistent series).

Reconciled values are **CEPII estimates**, not customs declarations: CEPII removes estimated CIF
costs from importer declarations (FOB valuation) and weights exporter and importer declarations by
an estimated reporter reliability (BACI methodology, quoted in ``docs/dev/v3_a_trade.md``). Every
cell says so (``flags.estimate = "cepii_reconciled"``); exports of i to j therefore equal imports
of j from i *by construction*, unlike the mirror flows of IMF IMTS (both kept per source).

Access: ``https://www.cepii.fr/DATA_DOWNLOAD/baci/data/BACI_HS92_V202601.zip`` (2,417,732,497
bytes, ETag ``"2a6b593bd68bdc1:0"``; one deflated CSV member per year ``t,i,j,k,v,q`` -- year,
exporter, importer, HS6 product, value in **thousand USD** (3 decimals), quantity in metric tons
-- plus ``country_codes_V202601.csv``, ``product_codes_HS92_V202601.csv`` and ``Readme.txt``). The
server honours byte ranges, so the connector reads **only** what it needs, exactly as served:

1. the archive tail (``Range: bytes=-65536``) -> central directory (member names, offsets, sizes,
   CRC-32);
2. ``country_codes_V202601.csv`` and ``Readme.txt`` (archived; the country codes are the evidence
   of the ``baci`` geo scheme, ``registry/atlas/geo_aliases/a_trade.yaml``);
3. one year member per selected year (:data:`YEARS`, ~45-99 MB compressed each), every request
   carrying ``If-Range: <ETag of the tail>`` so a replaced archive can never be mixed in.

Each archived raw artifact is exactly the bytes served for its range (local header + deflate
stream + data descriptor). The year member is inflated *incrementally* (CRC-32 and size checked
against the central directory) and aggregated on the fly: the HS6 detail itself is never stored.

Disk budget (V3 roadmap: <= 1.5 GB raw + Parquet per agent): the 30 HS92 year members total
2.42 GB, so a *selection* of 13 years is archived (:data:`YEARS`: 1995, 2000, 2005, 2010, 2015,
2017-2024 = 1.14 GB). Other years are one constant away once the disk budget allows.

Atlas tables (``source = "baci"``)
----------------------------------
Values are exact sums of the reported thousandths (integer arithmetic, no float), unit
``thousand US dollars (current)``. Indicators (partner = BACI importer/exporter code verbatim,
also mapped in ``flags.partner_iso3`` or ``flags.partner_unmapped``):

* ``hs92_country``: per country ``X|section=<S>`` exports, ``M|section=<S>`` imports (S = HS
  section I..XXI, ``NS`` = BACI's legacy "not specified" code 9999AA, ``TOTAL``) and **derived**
  ``BAL|section=<S>`` = X - M of the same year and file, only when both exist
  (``flags.derived``, both input indicators cited);
* ``hs92_pair_total``: ``X|partner=<j>|section=TOTAL`` (exports of i to j, all products) and
  derived ``BAL|partner=<j>|section=TOTAL`` = X(i->j) - X(j->i) when both directions exist;
* ``hs92_pair_section_1995_2010`` / ``_2011_2019`` / ``_2020_2024``: ``X|partner=<j>|section=<S>``
  (split by year range so one table stays below ~2 M rows and registers in bounded memory);
* ``hs92_pair_hs2``: ``X|partner=<j>|hs2=<CC>`` for the chapters of :data:`HS2_CHAPTERS`
  (cereals, ores, mineral fuels, pharmaceuticals, gold/precious stones, iron and steel,
  machinery, electrical equipment, vehicles, aircraft, arms...).

A flow absent from BACI is absent here (never a zero). Geography: ``baci`` scheme (UN Comtrade
reporter codes, e.g. 251 France, 842 USA, 699 India, 757 Switzerland, 579 Norway, + ten
territories whose BACI code is their M49 code); historical and residual codes stay unmapped and
are listed in each manifest: 58 Belgium-Luxembourg (...1998), 200, 278, 280, 490 "Other Asia,
nes", 530, 697, 711, 736 Sudan (...2011), 810, 849, 891.
"""

from __future__ import annotations

import gc
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_trade_common as common
from reva.adapters.connectors.atlas_trade_common import (
    ZipError,
    ZipMember,
    central_directory,
    iter_csv_blocks,
    iter_member,
    parse_content_range,
    scaled_to_text_expr,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.baci"
CONNECTOR_VERSION = "1.0.1"  # ATLASOPS: patch bump, code changed after 1.0.0 was registered in the dev DB (TWNMAP/agent edits)
PARSER_VERSION = "atlas_baci/1"
SOURCE_KEY = "cepii.baci"
SOURCE = "baci"
HOST = "www.cepii.fr"
RELEASE = "202601"
HS = "HS92"
ARCHIVE = f"BACI_{HS}_V{RELEASE}.zip"
URL = f"https://{HOST}/DATA_DOWNLOAD/baci/data/{ARCHIVE}"
TAIL_BYTES = 65_536
GEO_SCHEME = "baci"
UNIT = "thousand US dollars (current)"
SCALE = 3  # BACI values: thousand USD with at most 3 decimals -> exact integer thousandths (= dollars)
MAX_MEMBER_BYTES = 110_000_000  # largest HS92 V202601 year member: 99.1 MB compressed
CSV_HEADER = b"t,i,j,k,v,q"

#: Years archived and aggregated (disk budget; see module doc). Fixture mode: FIXTURE_YEARS.
YEARS: tuple[int, ...] = (1995, 2000, 2005, 2010, 2015, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024)
FIXTURE_YEARS: tuple[int, ...] = (2023, 2024)

RIGHTS: dict[str, str] = {
    "licence": "Etalab Open Licence 2.0",
    "licence_url": "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    "terms_url": "https://www.cepii.fr/CEPII/en/bdd_modele/bdd_modele_item.asp?id=37",
    "attribution": "CEPII BACI (version 202601), Gaulier & Zignago (2010), CEPII Working Paper 2010-23",
}

#: HS sections (WCO Harmonized System nomenclature, identical in HS1992..HS2022): chapter ranges.
SECTIONS: tuple[tuple[str, tuple[int, int], str], ...] = (
    ("I", (1, 5), "Live animals; animal products"),
    ("II", (6, 14), "Vegetable products"),
    ("III", (15, 15), "Animal or vegetable fats and oils"),
    ("IV", (16, 24), "Prepared foodstuffs; beverages, spirits and vinegar; tobacco"),
    ("V", (25, 27), "Mineral products"),
    ("VI", (28, 38), "Products of the chemical or allied industries"),
    ("VII", (39, 40), "Plastics and rubber"),
    ("VIII", (41, 43), "Raw hides and skins, leather, furskins"),
    ("IX", (44, 46), "Wood, cork, straw and plaiting materials"),
    ("X", (47, 49), "Pulp of wood; paper and paperboard"),
    ("XI", (50, 63), "Textiles and textile articles"),
    ("XII", (64, 67), "Footwear, headgear, umbrellas"),
    ("XIII", (68, 70), "Articles of stone, plaster, cement; ceramics; glass"),
    ("XIV", (71, 71), "Pearls, precious stones and metals; jewellery; coin"),
    ("XV", (72, 83), "Base metals and articles of base metal"),
    ("XVI", (84, 85), "Machinery and mechanical appliances; electrical equipment"),
    ("XVII", (86, 89), "Vehicles, aircraft, vessels and transport equipment"),
    ("XVIII", (90, 92), "Optical, medical, measuring instruments; clocks; musical instruments"),
    ("XIX", (93, 93), "Arms and ammunition"),
    ("XX", (94, 96), "Miscellaneous manufactured articles"),
    ("XXI", (97, 97), "Works of art, collectors' pieces and antiques"),
)
NOT_SPECIFIED = "NS"  # BACI legacy non-WCO code 9999AA "Commodities not specified according to kind"
SECTION_CODES: tuple[str, ...] = (*tuple(s for s, _, _ in SECTIONS), NOT_SPECIFIED)
CHAPTER_SECTION: dict[str, str] = {
    f"{c:02d}": s for s, (lo, hi), _ in SECTIONS for c in range(lo, hi + 1) if c != 77
} | {"99": NOT_SPECIFIED}

#: HS2 chapters kept per country pair (interest list of the V3 owner request + strategic goods).
HS2_CHAPTERS: dict[str, str] = {
    "10": "Cereals",
    "12": "Oil seeds and oleaginous fruits",
    "26": "Ores, slag and ash",
    "27": "Mineral fuels, mineral oils and products of their distillation",
    "28": "Inorganic chemicals; compounds of precious metals, rare-earth metals",
    "30": "Pharmaceutical products",
    "71": "Pearls, precious stones and metals (incl. gold)",
    "72": "Iron and steel",
    "84": "Nuclear reactors, boilers, machinery and mechanical appliances",
    "85": "Electrical machinery and equipment",
    "87": "Vehicles other than railway or tramway rolling stock",
    "88": "Aircraft, spacecraft and parts",
    "93": "Arms and ammunition",
}

DS_COUNTRY = "hs92_country"
DS_PAIR_TOTAL = "hs92_pair_total"
DS_PAIR_HS2 = "hs92_pair_hs2"
#: pair x section tables by year range (one table stays below ~2 M rows: bounded memory when registering)
PAIR_SECTION_TABLES: dict[str, tuple[int, int]] = {
    "hs92_pair_section_1995_2010": (1995, 2010),
    "hs92_pair_section_2011_2019": (2011, 2019),
    "hs92_pair_section_2020_2024": (2020, 2024),
}
DATASETS: tuple[str, ...] = (DS_COUNTRY, DS_PAIR_TOTAL, *PAIR_SECTION_TABLES, DS_PAIR_HS2)

_VALUE_RE = r"^[0-9]+(\.[0-9]+)?$"
#: binary floating-point noise after the 3rd decimal, e.g. "34.471000000000004", "3.3729999999999998"
_NOISE_RE = r"^(0{6,}[0-9]{1,3}|9{6,}[0-9]{1,3})$"
_CODE_RE = r"^[0-9]{1,4}$"
_PRODUCT_RE = r"^[0-9]{4}[0-9A-Z]{2}$"


def year_member_name(year: int) -> str:
    return f"BACI_{HS}_Y{year}_V{RELEASE}.csv"


COUNTRY_CODES_MEMBER = f"country_codes_V{RELEASE}.csv"
README_MEMBER = "Readme.txt"


# --------------------------------------------------------------------------- aggregation
@dataclass
class YearAgg:
    """One year member aggregated: exporter x importer x section, and x selected HS2 chapter."""

    year: int
    member: str
    raw_sha256: str
    by_section: pl.DataFrame  # i, j, section, v (Int64 thousandths), n
    by_hs2: pl.DataFrame  # i, j, hs2, v, n
    rows: int = 0
    stats: dict[str, Any] = field(default_factory=dict)


def _check_block(df: pl.DataFrame, year: int, member: str) -> None:
    bad = df.filter(
        (pl.col("t") != str(year))
        | ~pl.col("i").str.contains(_CODE_RE)
        | ~pl.col("j").str.contains(_CODE_RE)
        | ~pl.col("k").str.contains(_PRODUCT_RE)
        | pl.col("v").is_null()
        | ~pl.col("v").str.contains(_VALUE_RE)
    )
    if bad.height:
        raise SourceResponseError(
            "BACI member has unexpected cells", member=member, examples=bad.head(3).to_dicts(), retryable=False
        )
    unknown = set(df["k"].str.slice(0, 2).unique().to_list()) - set(CHAPTER_SECTION)
    if unknown:
        raise SourceResponseError(
            "BACI product chapter outside the HS sections", chapters=sorted(unknown)[:10], retryable=False
        )


def thousandths(v: pl.Series) -> list[pl.Series]:
    """BACI value text -> exact Int64 thousandths ``v`` and a 0/1 ``r`` "rounded" marker.

    BACI writes values with at most 3 decimals, except a few (~120 of ~11 M per year) printed with
    binary floating-point noise (``34.471000000000004``, ``3.3729999999999998``): those, and only
    those (6+ zeros or nines after the 3rd decimal), are rounded to the thousandth they denote and
    counted (``r``). Any other value with more than 3 decimals is refused."""
    parts = v.str.split_exact(".", 1)
    whole = parts.struct.field("field_0")
    frac = parts.struct.field("field_1").fill_null("")
    tail = frac.str.slice(3)
    long = tail.str.len_chars() > 0
    bad = long & ~tail.str.contains(_NOISE_RE)
    if bad.any():
        raise SourceResponseError(
            "BACI value with more than 3 decimals that is not float noise",
            examples=v.filter(bad).head(3).to_list(),
            retryable=False,
        )
    base = whole.cast(pl.Int64) * 1000 + frac.str.slice(0, 3).str.pad_end(SCALE, "0").cast(pl.Int64)
    up = (long & tail.str.starts_with("9")).cast(pl.Int64)
    return [(base + up).alias("v"), long.cast(pl.Int64).alias("r")]


def aggregate_year(blocks: Iterable[bytes], *, year: int, member: str, raw_sha256: str) -> YearAgg:
    """Aggregate the CSV blocks of one year member (consumes the iterator fully)."""
    schema = {c: pl.String for c in ("t", "i", "j", "k", "v", "q")}
    sec_parts: list[pl.DataFrame] = []
    hs2_parts: list[pl.DataFrame] = []
    rows = 0
    for block in blocks:
        df = pl.read_csv(io.BytesIO(block), schema=schema, has_header=True, quote_char=None)
        _check_block(df, year, member)
        rows += df.height
        df = df.select(
            "i",
            "j",
            pl.col("k").str.slice(0, 2).alias("hs2"),
            *thousandths(df["v"]),
        ).with_columns(pl.col("hs2").replace_strict(CHAPTER_SECTION, return_dtype=pl.String).alias("section"))
        aggs = (pl.col("v").sum(), pl.len().alias("n"), pl.col("r").sum().alias("r"))
        sec_parts.append(df.group_by("i", "j", "section").agg(*aggs))
        hs2_parts.append(df.filter(pl.col("hs2").is_in(list(HS2_CHAPTERS))).group_by("i", "j", "hs2").agg(*aggs))
    if not rows:
        raise SourceResponseError("BACI year member has no rows", member=member, retryable=False)

    def merge(parts: list[pl.DataFrame], keys: Sequence[str]) -> pl.DataFrame:
        return (
            pl.concat(parts)
            .group_by(*keys)
            .agg(pl.col("v").sum(), pl.col("n").sum().cast(pl.Int64), pl.col("r").sum().cast(pl.Int64))
            .sort(*keys)
        )

    by_section = merge(sec_parts, ("i", "j", "section"))
    rounded = int(by_section["r"].sum())
    by_hs2 = merge(hs2_parts, ("i", "j", "hs2"))
    return YearAgg(
        year=year,
        member=member,
        raw_sha256=raw_sha256,
        by_section=by_section,
        by_hs2=by_hs2,
        rows=rows,
        stats={"hs6_rows": rows, "pairs": by_section.select("i", "j").unique().height, "rounded_values": rounded},
    )


# --------------------------------------------------------------------------- table building
def _partner_map(codes: Iterable[str]) -> dict[str, str | None]:
    from reva.atlas.tables import country_iso3

    return {c: country_iso3(c, GEO_SCHEME) for c in codes}


def _partner_flags(col: str, pmap: dict[str, str | None]) -> pl.Expr:
    """``{"baci_release":..,"estimate":..,"hs_revision":..,"partner_iso3":"USA","valuation":"FOB"}``."""
    mapped = pl.col(col).replace_strict(pmap, default=None, return_dtype=pl.String)
    part = (
        pl.when(mapped.is_null())
        .then(pl.lit('"partner_unmapped":true'))
        .otherwise(pl.lit('"partner_iso3":"') + mapped + pl.lit('"'))
    )
    return pl.concat_str(
        [
            pl.lit(f'{{"baci_release":"{RELEASE}","estimate":"cepii_reconciled","hs_revision":"{HS}",'),
            part,
            pl.lit(',"valuation":"FOB"}'),
        ]
    )


def _frame(df: pl.DataFrame, *, dataset: str, year: int) -> pl.DataFrame:
    """``df`` with columns indicator, provider_geo, value (text), flags, raw_sha256, selector."""
    return df.select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(dataset).alias("dataset"),
        pl.col("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        pl.col("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.lit(date(year, 1, 1)).alias("period_start"),
        pl.lit(date(year + 1, 1, 1)).alias("period_end"),
        pl.col("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        pl.lit(UNIT).alias("unit"),
        pl.col("flags"),
        pl.col("raw_sha256"),
        pl.col("selector"),
    )


def _sel(member: str, filt: Sequence[tuple[str, pl.Expr | str]], n: pl.Expr | None) -> pl.Expr:
    """Selector JSON ``{"aggregate":"sum(v)","filter":{...},"member":"...","rounded":r,"rows":n}``
    (``rounded``: inputs rounded from float noise, see :func:`thousandths`)."""
    parts: list[pl.Expr] = [pl.lit('{"aggregate":"sum(v)","filter":{')]
    for idx, (k, v) in enumerate(filt):
        parts.append(pl.lit(("," if idx else "") + f'"{k}":"'))
        parts.append(v if isinstance(v, pl.Expr) else pl.lit(v))
        parts.append(pl.lit('"'))
    parts.append(pl.lit(f'}},"member":"{member}"'))
    if n is not None:
        parts += [pl.lit(',"rounded":'), pl.col("r").cast(pl.String), pl.lit(',"rows":'), n.cast(pl.String)]
    parts.append(pl.lit("}"))
    return pl.concat_str(parts)


def pair_section_rows(agg: YearAgg, dataset: str, pmap: dict[str, str | None]) -> pl.DataFrame:
    df = agg.by_section.with_columns(
        pl.concat_str([pl.lit("X|partner="), pl.col("j"), pl.lit("|section="), pl.col("section")]).alias("indicator"),
        pl.col("i").alias("provider_geo"),
        scaled_to_text_expr("v", SCALE).alias("value"),
        _partner_flags("j", pmap).alias("flags"),
        pl.lit(agg.raw_sha256).alias("raw_sha256"),
        _sel(agg.member, [("i", pl.col("i")), ("j", pl.col("j")), ("section", pl.col("section"))], pl.col("n")).alias(
            "selector"
        ),
    )
    return _frame(df, dataset=dataset, year=agg.year)


def pair_hs2_rows(agg: YearAgg, pmap: dict[str, str | None]) -> pl.DataFrame:
    df = agg.by_hs2.with_columns(
        pl.concat_str([pl.lit("X|partner="), pl.col("j"), pl.lit("|hs2="), pl.col("hs2")]).alias("indicator"),
        pl.col("i").alias("provider_geo"),
        scaled_to_text_expr("v", SCALE).alias("value"),
        _partner_flags("j", pmap).alias("flags"),
        pl.lit(agg.raw_sha256).alias("raw_sha256"),
        _sel(agg.member, [("i", pl.col("i")), ("j", pl.col("j")), ("hs2", pl.col("hs2"))], pl.col("n")).alias(
            "selector"
        ),
    )
    return _frame(df, dataset=DS_PAIR_HS2, year=agg.year)


def pair_total_rows(agg: YearAgg, pmap: dict[str, str | None]) -> pl.DataFrame:
    tot = agg.by_section.group_by("i", "j").agg(pl.col("v").sum(), pl.col("n").sum(), pl.col("r").sum()).sort("i", "j")
    x = tot.with_columns(
        pl.concat_str([pl.lit("X|partner="), pl.col("j"), pl.lit("|section=TOTAL")]).alias("indicator"),
        pl.col("i").alias("provider_geo"),
        scaled_to_text_expr("v", SCALE).alias("value"),
        _partner_flags("j", pmap).alias("flags"),
        pl.lit(agg.raw_sha256).alias("raw_sha256"),
        _sel(agg.member, [("i", pl.col("i")), ("j", pl.col("j"))], pl.col("n")).alias("selector"),
    )
    back = tot.select(pl.col("j").alias("i"), pl.col("i").alias("j"), pl.col("v").alias("v_back"))
    bal = tot.join(back, on=["i", "j"], how="inner").with_columns((pl.col("v") - pl.col("v_back")).alias("b"))
    mapped = pl.col("j").replace_strict(pmap, default=None, return_dtype=pl.String)
    partner = (
        pl.when(mapped.is_null())
        .then(pl.lit('"partner_unmapped":true'))
        .otherwise(pl.lit('"partner_iso3":"') + mapped + pl.lit('"'))
    )
    b = bal.with_columns(
        pl.concat_str([pl.lit("BAL|partner="), pl.col("j"), pl.lit("|section=TOTAL")]).alias("indicator"),
        pl.col("i").alias("provider_geo"),
        scaled_to_text_expr("b", SCALE).alias("value"),
        pl.concat_str(
            [
                pl.lit(
                    f'{{"baci_release":"{RELEASE}","derived":"exports_minus_imports","estimate":"cepii_reconciled",'
                ),
                pl.lit('"hs_revision":"HS92","inputs":["X|partner='),
                pl.col("j"),
                pl.lit("|section=TOTAL of "),
                pl.col("i"),
                pl.lit('","X|partner='),
                pl.col("i"),
                pl.lit("|section=TOTAL of "),
                pl.col("j"),
                pl.lit('"],'),
                partner,
                pl.lit(',"valuation":"FOB"}'),
            ]
        ).alias("flags"),
        pl.lit(agg.raw_sha256).alias("raw_sha256"),
        pl.concat_str(
            [
                pl.lit('{"derived":"sum(v | i=A, j=B) - sum(v | i=B, j=A)","filter":{"A":"'),
                pl.col("i"),
                pl.lit('","B":"'),
                pl.col("j"),
                pl.lit(f'"}},"member":"{agg.member}"}}'),
            ]
        ).alias("selector"),
    )
    cols = ["indicator", "provider_geo", "value", "flags", "raw_sha256", "selector"]
    return _frame(pl.concat([x.select(cols), b.select(cols)]), dataset=DS_PAIR_TOTAL, year=agg.year)


def country_rows(agg: YearAgg) -> pl.DataFrame:
    s = agg.by_section
    sums = (pl.col("v").sum(), pl.col("n").sum(), pl.col("r").sum())
    xs = s.group_by(pl.col("i").alias("c"), "section").agg(*sums)
    ms = s.group_by(pl.col("j").alias("c"), "section").agg(*sums)
    xt = xs.group_by("c").agg(*sums).with_columns(pl.lit("TOTAL").alias("section"))
    mt = ms.group_by("c").agg(*sums).with_columns(pl.lit("TOTAL").alias("section"))
    x = pl.concat([xs, xt.select(xs.columns)])
    m = pl.concat([ms, mt.select(ms.columns)])
    base = f'{{"baci_release":"{RELEASE}","estimate":"cepii_reconciled","hs_revision":"{HS}","valuation":"FOB"}}'

    def flow(df: pl.DataFrame, code: str, side: str) -> pl.DataFrame:
        filt = [(side, pl.col("c"))]
        sec = pl.when(pl.col("section") == "TOTAL").then(None).otherwise(pl.col("section"))
        sel = (
            pl.when(sec.is_null())
            .then(_sel(agg.member, filt, pl.col("n")))
            .otherwise(_sel(agg.member, [*filt, ("section", pl.col("section"))], pl.col("n")))
        )
        return df.with_columns(
            pl.concat_str([pl.lit(f"{code}|section="), pl.col("section")]).alias("indicator"),
            pl.col("c").alias("provider_geo"),
            scaled_to_text_expr("v", SCALE).alias("value"),
            pl.lit(base).alias("flags"),
            pl.lit(agg.raw_sha256).alias("raw_sha256"),
            sel.alias("selector"),
        )

    bal = x.join(m.drop("n", "r"), on=["c", "section"], how="inner", suffix="_m").with_columns(
        (pl.col("v") - pl.col("v_m")).alias("b")
    )
    b = bal.with_columns(
        pl.concat_str([pl.lit("BAL|section="), pl.col("section")]).alias("indicator"),
        pl.col("c").alias("provider_geo"),
        scaled_to_text_expr("b", SCALE).alias("value"),
        pl.concat_str(
            [
                pl.lit(
                    f'{{"baci_release":"{RELEASE}","derived":"exports_minus_imports","estimate":"cepii_reconciled",'
                    '"hs_revision":"HS92","inputs":["X|section='
                ),
                pl.col("section"),
                pl.lit('","M|section='),
                pl.col("section"),
                pl.lit('"],"valuation":"FOB"}'),
            ]
        ).alias("flags"),
        pl.lit(agg.raw_sha256).alias("raw_sha256"),
        pl.concat_str(
            [
                pl.lit('{"derived":"sum(v | i=C[, section]) - sum(v | j=C[, section])","filter":{"C":"'),
                pl.col("c"),
                pl.lit('","section":"'),
                pl.col("section"),
                pl.lit(f'"}},"member":"{agg.member}"}}'),
            ]
        ).alias("selector"),
    )
    cols = ["indicator", "provider_geo", "value", "flags", "raw_sha256", "selector"]
    out = pl.concat([flow(x, "X", "i").select(cols), flow(m, "M", "j").select(cols), b.select(cols)])
    return _frame(out, dataset=DS_COUNTRY, year=agg.year)


def dataset_years(dataset: str, years: Iterable[int]) -> list[int]:
    """The years whose aggregates feed ``dataset``."""
    if dataset in PAIR_SECTION_TABLES:
        lo, hi = PAIR_SECTION_TABLES[dataset]
        return [y for y in years if lo <= y <= hi]
    return list(years)


def partner_map(aggs: Sequence[YearAgg]) -> dict[str, str | None]:
    codes: set[str] = set()
    for a in aggs:
        codes |= set(a.by_section["j"].unique().to_list())
    return _partner_map(sorted(codes))


def build_frame(aggs: Sequence[YearAgg], dataset: str, pmap: dict[str, str | None]) -> pl.DataFrame | None:
    """The atlas frame (not yet validated) of one dataset from the year aggregates (``None``: no year)."""
    years = set(dataset_years(dataset, [a.year for a in aggs]))
    parts = []
    for a in aggs:
        if a.year not in years:
            continue
        if dataset == DS_COUNTRY:
            parts.append(country_rows(a))
        elif dataset == DS_PAIR_TOTAL:
            parts.append(pair_total_rows(a, pmap))
        elif dataset == DS_PAIR_HS2:
            parts.append(pair_hs2_rows(a, pmap))
        else:
            parts.append(pair_section_rows(a, dataset, pmap))
    return pl.concat(parts) if parts else None


def build_frames(aggs: Sequence[YearAgg], wanted: Iterable[str]) -> dict[str, pl.DataFrame]:
    """Atlas frames of every wanted dataset (tests; the ingest builds one table at a time)."""
    pmap = partner_map(aggs)
    out = {d: build_frame(aggs, d, pmap) for d in DATASETS if d in set(wanted)}
    return {d: f for d, f in out.items() if f is not None}


def parse_country_codes(body: bytes) -> list[dict[str, str]]:
    df = common.read_csv_all_text(body, what="BACI country codes")
    need = ["country_code", "country_name", "country_iso2", "country_iso3"]
    if df.columns != need:
        raise SourceResponseError("BACI country codes: unexpected columns", columns=df.columns, retryable=False)
    return df.to_dicts()


# --------------------------------------------------------------------------- ATLASCORE entry point
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="CEPII (Centre d'études prospectives et d'informations internationales) - BACI",
        licence=f"{RIGHTS['licence']} ({RIGHTS['terms_url']}); attribution: {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=(f"BACI_{HS}_V{RELEASE}",),
        cadence_seconds=90 * 86400,  # yearly release (January); quarterly check
        planned_requests=3 + len(YEARS),
        max_response_bytes=MAX_MEMBER_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(_module()),
        field_mappings={
            "archive": ARCHIVE,
            "years": list(YEARS),
            "unit": UNIT,
            "sections": {s: [lo, hi] for s, (lo, hi), _ in SECTIONS},
            "hs2_chapters": sorted(HS2_CHAPTERS),
            "pair_section_tables": {k: list(v) for k, v in PAIR_SECTION_TABLES.items()},
            "geo_scheme": GEO_SCHEME,
        },
        notes="Byte ranges of one BACI zip: tail + country codes + Readme + one member per selected year.",
    )


def _module() -> Any:
    import sys

    return sys.modules[__name__]


async def _range(run: Any, m: ZipMember, *, key: str, etag: str) -> tuple[Any, int]:
    raw = await run.get_range(
        URL,
        key=key,
        start=m.offset,
        end=m.end,
        if_range=etag,
        media_types=("application/x-zip-compressed", "application/zip", "application/octet-stream"),
        params={"member": m.name},
    )
    start, _end, _total = parse_content_range(raw.response_metadata["headers"].get("content-range", ""))
    return raw, start


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Tail -> central directory -> country codes + Readme -> year members -> tables."""
    from reva.operations.budgets import BudgetExhausted

    years = FIXTURE_YEARS if run.origin == "test_fixture" else YEARS
    wanted = [d for d in DATASETS if run.wants(d)]
    media = ("application/x-zip-compressed", "application/zip", "application/octet-stream")
    tail = await run.get_range(URL, key="tail", suffix=TAIL_BYTES, media_types=media)
    t_start, _t_end, total = parse_content_range(tail.response_metadata["headers"].get("content-range", ""))
    etag = (tail.response_metadata.get("headers") or {}).get("etag")
    if not etag:
        raise ZipError("BACI archive response has no ETag (cannot pin member ranges)")
    tail_sha = run.archive(tail)
    members = {m.name: m for m in central_directory(tail.body, t_start, total)}
    fetched = [{"item": "tail", "bytes": len(tail.body), "sha256": tail_sha}]
    errors: list[dict[str, Any]] = []
    shas = [tail_sha]
    for name in (COUNTRY_CODES_MEMBER, README_MEMBER):
        if name not in members:
            raise ZipError("BACI archive lacks a reference member", member=name)
        raw, start = await _range(run, members[name], key=f"member:{name}", etag=etag)
        sha = run.archive(raw)
        body = b"".join(iter_member(raw.body, start, members[name]))
        if name == COUNTRY_CODES_MEMBER:
            parse_country_codes(body)  # shape check; mapping evidence lives in the geo alias file
        shas.append(sha)
        fetched.append({"item": name, "bytes": len(raw.body), "sha256": sha})
    aggs: list[YearAgg] = []
    for y in years:
        name = year_member_name(y)
        if y not in range(1995, 2025) or name not in members:
            errors.append({"item": name, "error": "member_absent", "message": "year not in this BACI archive"})
            continue
        try:
            raw, start = await _range(run, members[name], key=f"member:{name}", etag=etag)
            sha = run.archive(raw)
            blocks = iter_csv_blocks(iter_member(raw.body, start, members[name]), header=CSV_HEADER)
            agg = aggregate_year(blocks, year=y, member=name, raw_sha256=sha)
        except BudgetExhausted:
            raise
        except Exception as e:  # typed provider errors are reported per year
            errors.append({"item": name, "error": type(e).__name__, "message": str(e)[:300]})
            continue
        del raw
        aggs.append(agg)
        shas.append(sha)
        fetched.append(
            {"item": name, "bytes": members[name].end - members[name].offset + 1, "sha256": sha, **agg.stats}
        )
    tables: list[dict[str, Any]] = []
    years_done = sorted(a.year for a in aggs)
    pmap = partner_map(aggs)
    ref_shas = shas[:3]  # tail, country codes, Readme
    for ds in wanted:
        frame = build_frame(aggs, ds, pmap)
        if frame is None:
            continue
        used = [a for a in aggs if a.year in set(dataset_years(ds, years_done))]
        note = (
            f"CEPII BACI {HS} V{RELEASE} ({ARCHIVE}, ETag {etag}, {total} bytes) read by byte ranges; years "
            f"{', '.join(str(a.year) for a in used)}. Values: exact sums of BACI HS6 values (thousand USD, FOB, CEPII "
            "reconciled estimates). BAL rows are derived (exports - imports, same year and file). Codes follow UN "
            "Comtrade reporters: 251 France incl. Monaco, 842 USA incl. Puerto Rico and US Virgin Islands, 757 "
            "Switzerland incl. Liechtenstein, 579 Norway incl. Svalbard and Jan Mayen (crosswalk notes)."
        )
        try:
            out = run.register(
                dataset=ds,
                frame=frame,
                raw_sha256s=[*ref_shas, *(a.raw_sha256 for a in used)],
                coverage_note=note,
                geo_scheme=GEO_SCHEME,
                rights=dict(RIGHTS),
            )
        except Exception as e:
            errors.append(
                {"stage": "register", "table": f"{SOURCE}.{ds}", "error": type(e).__name__, "message": str(e)[:500]}
            )
            continue
        finally:
            del frame
            gc.collect()
        cov = out.get("coverage") or {}
        tables.append(
            {
                "table": f"{SOURCE}.{ds}",
                "dataset_key": out.get("dataset_key"),
                "created": out.get("created"),
                "rows": out.get("row_count"),
                "countries": cov.get("countries"),
                "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []],
                "artifact_sha256": out.get("artifact_sha256"),
            }
        )
    return {"fetched": fetched, "errors": errors, "tables": tables, "years": years_done, "ok": not errors}


__all__ = [
    "CHAPTER_SECTION",
    "CONNECTOR_NAME",
    "DATASETS",
    "FIXTURE_YEARS",
    "HS2_CHAPTERS",
    "SECTIONS",
    "URL",
    "YEARS",
    "YearAgg",
    "aggregate_year",
    "atlas_ingest",
    "atlas_source",
    "build_frames",
    "country_rows",
    "pair_total_rows",
]
