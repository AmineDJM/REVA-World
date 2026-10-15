"""Our World in Data compilations (energy, CO2/greenhouse gases, selected grapher charts) -> atlas tables.

V3, agent A-QOL. **OWID is a compiler, not an original producer.** Every cell carries the original
source OWID cites for its series (``flags.original_source``), and the table rights list each
original producer with its own licence. Catalog ``source_order`` puts original producers first
whenever another atlas agent ingests them (World Bank, UN, Maddison...); OWID is the fallback.

Licence: "All visualizations, data, and code produced by Our World in Data are completely open
access under the Creative Commons BY license ... The data produced by third parties and made
available by Our World in Data is subject to the license terms from the original third-party
authors" (README of github.com/owid/co2-data, checked 2026-10-15).

Inputs (bulk, one request each):

* ``owid/energy-data`` — ``owid-energy-data.csv`` + ``owid-energy-codebook.csv`` (column, title,
  description, unit, source). Original sources: Energy Institute Statistical Review of World Energy
  (© Energy Institute; OWID redistributes it under EI's terms), Ember (CC BY 4.0), U.S. EIA
  (public domain), The Shift Data Portal.
* ``owid/co2-data`` — ``owid-co2-data.csv`` + ``owid-co2-codebook.csv``. Original sources: Global
  Carbon Budget (CC BY 4.0), Jones et al. National contributions to climate change (CC BY 4.0).
  Raw files come from ``raw.githubusercontent.com/owid/<repo>/master`` (GitHub's API, which would
  give the commit id, is not reachable from this environment): the raw sha256 and the response ETag
  pin the exact bytes.
* Grapher charts (:data:`GRAPHER_CHARTS`): ``<slug>.csv`` + ``<slug>.metadata.json`` (short column
  names) and, per column, the indicator's full metadata
  (``api.ourworldindata.org/v1/indicators/<id>.metadata.json``) whose ``origins`` give each original
  producer and licence. A column whose full metadata says ``nonRedistributable`` is refused (skipped,
  reported). Selection (licences checked 2026-10-15): Climate Watch GHG emissions by sector
  (CC BY 4.0) and ERA5-based annual temperature anomalies (Copernicus licence, attribution).
  Rejected: IHME GBD air-pollution deaths (non-redistributable, non-commercial agreement),
  WHO/UNICEF JMP drinking water (CC BY-NC-SA 3.0 IGO), UNODC homicide ("© United Nations" terms),
  WID series (ingested at the source by :mod:`atlas_wid`), WHR/UNDP/World Bank series (ingested at
  the source by this agent or A-WB).

Excluded columns of the GitHub CSVs: ``population`` and ``gdp`` (OWID pass-through of UN WPP /
Maddison, which A-UN / A-HISTECON ingest at the source). Everything else is kept as published,
including OWID-derived ratios (per capita, shares, growth), whose ``original_source`` names the
inputs OWID used.

Geography: ``iso_code``/``code`` when present (ISO3 or ``OWID_*``), else the entity name (OWID
leaves ``iso_code`` empty for Kosovo, historical states and source-specific regions such as
"Africa (EI)"); mapped through scheme ``owid`` (``registry/atlas/geo_aliases/a_qol.yaml``).
"""

from __future__ import annotations

import csv
import io
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import polars as pl

from reva.adapters.connectors import atlas_qol_common as common
from reva.adapters.connectors.atlas_qol_common import AtlasBuild, canon
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

CONNECTOR_NAME = "atlas.owid"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_owid/1"
SOURCE_KEY = "owid.compilations"
SOURCE = "owid"
GH_HOST = "raw.githubusercontent.com"
GRAPHER_HOST = "ourworldindata.org"
API_HOST = "api.ourworldindata.org"
LICENCE = "CC BY 4.0 (OWID-produced data); third-party data under each original producer's terms"
ATTRIBUTION = "Our World in Data (ourworldindata.org), compiled from the original sources cited per series"
GEO_SCHEME = "owid"


@dataclass(frozen=True)
class GithubDataset:
    dataset: str
    repo: str
    data_file: str
    codebook_file: str
    excluded: tuple[str, ...]

    @property
    def data_url(self) -> str:
        return f"https://{GH_HOST}/owid/{self.repo}/master/{self.data_file}"

    @property
    def codebook_url(self) -> str:
        return f"https://{GH_HOST}/owid/{self.repo}/master/{self.codebook_file}"


GITHUB_DATASETS: tuple[GithubDataset, ...] = (
    GithubDataset("energy", "energy-data", "owid-energy-data.csv", "owid-energy-codebook.csv", ("population", "gdp")),
    GithubDataset("co2", "co2-data", "owid-co2-data.csv", "owid-co2-codebook.csv", ("population", "gdp")),
)
#: grapher slug -> (atlas dataset key, short reason)
GRAPHER_CHARTS: dict[str, tuple[str, str]] = {
    "ghg-emissions-by-sector": ("g_ghg_by_sector", "Climate Watch sectoral GHG emissions (CC BY 4.0)"),
    "annual-temperature-anomalies": ("g_temperature_anomaly", "ERA5 annual temperature anomalies (Copernicus)"),
}
GRAPHER_QS = "v=1&csvType=full&useColumnShortNames=true"
ID_COLUMNS_GH = ("country", "year", "iso_code")  # file order
ID_ROLES_GH = ("country", "iso_code", "year")  # (name, code, year)
ID_COLUMNS_GRAPHER = ("entity", "code", "year")  # file order = (name, code, year)


class OwidParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


# --------------------------------------------------------------------------- parsing helpers
def parse_codebook(body: bytes) -> dict[str, dict[str, str]]:
    text = body.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    if not rows or not {"column", "title", "description", "unit", "source"} <= set(rows[0]):
        raise OwidParseError("codebook lacks column/title/description/unit/source")
    return {
        r["column"]: {k: (r.get(k) or "").strip() for k in ("title", "description", "unit", "source")} for r in rows
    }


def _long_frame(
    body: bytes,
    *,
    id_cols: tuple[str, str, str],
    keep: Sequence[str],
    file: str,
    raw_sha: str,
    dataset: str,
    units: dict[str, str],
    flags: dict[str, str],
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Wide OWID CSV -> atlas long frame (strings kept verbatim). ``id_cols`` = (name, code, year) columns."""
    name_col, code_col, year_col = id_cols
    df = pl.read_csv(io.BytesIO(body), infer_schema=False, encoding="utf8")
    for c in id_cols:
        if c not in df.columns:
            raise OwidParseError(f"{file}: missing id column {c!r}")
    missing = [c for c in keep if c not in df.columns]
    if missing:
        raise OwidParseError(f"{file}: missing indicator columns {missing[:10]}")
    df = df.with_row_index("row", offset=2)  # 1-based file line (header is line 1)
    long = (
        df.select(["row", name_col, code_col, year_col, *keep])
        .unpivot(index=["row", name_col, code_col, year_col], on=list(keep), variable_name="indicator", value_name="v")
        .with_columns(pl.col("v").str.strip_chars())
        .filter(pl.col("v").is_not_null() & (pl.col("v") != ""))
    )
    bad_year = long.filter(~pl.col(year_col).str.contains(r"^-?[0-9]{1,4}$"))
    if bad_year.height:
        raise OwidParseError(f"{file}: non-integer year {bad_year[year_col][0]!r}")
    bad = long.filter(~pl.col("v").str.contains(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$"))
    if bad.height:
        r = bad.row(0, named=True)
        raise OwidParseError(f"{file}: row {r['row']} column {r['indicator']}: {r['v']!r} is not a strict decimal")
    y = pl.col(year_col).cast(pl.Int32)
    geo = (
        pl.when(pl.col(code_col).is_not_null() & (pl.col(code_col).str.strip_chars() != ""))
        .then(pl.col(code_col).str.strip_chars())
        .otherwise(pl.col(name_col))
    )
    out = long.select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(dataset).alias("dataset"),
        pl.col("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        geo.alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(y, 1, 1).alias("period_start"),
        pl.date(y + 1, 1, 1).alias("period_end"),
        pl.col("v").alias("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        pl.col("indicator").replace_strict(units, return_dtype=pl.String).alias("unit"),
        pl.col("indicator").replace_strict(flags, return_dtype=pl.String).alias("flags"),
        pl.lit(raw_sha).alias("raw_sha256"),
        pl.format('{{"column":"{}","file":"{}","row":{}}}', pl.col("indicator"), pl.lit(file), pl.col("row")).alias(
            "selector"
        ),
    )
    rep = {
        "rows_in_file": df.height,
        "indicators": len(keep),
        "cells": out.height,
        "years": [str(long[year_col].cast(pl.Int32).min()), str(long[year_col].cast(pl.Int32).max())],
    }
    return out, rep


def parse_github(
    spec: GithubDataset, data: bytes, codebook: bytes, data_sha: str
) -> tuple[pl.DataFrame, dict[str, Any]]:
    cb = parse_codebook(codebook)
    header = next(csv.reader(io.StringIO(data[:200_000].decode("utf-8-sig"), newline="")))
    if tuple(header[:3]) != ID_COLUMNS_GH:
        raise OwidParseError(f"{spec.data_file}: leading columns {header[:3]} != {ID_COLUMNS_GH}")
    keep = [c for c in header[3:] if c not in spec.excluded]
    no_cb = [c for c in keep if c not in cb]
    if no_cb:
        raise OwidParseError(f"{spec.data_file}: columns without a codebook entry {no_cb[:10]}")
    units = {c: (cb[c]["unit"] or "as defined in codebook (no unit)")[:100] for c in keep}
    flags = {c: canon(original_source=cb[c]["source"][:3500], owid_title=cb[c]["title"][:300]) for c in keep}
    frame, rep = _long_frame(
        data,
        id_cols=ID_ROLES_GH,
        keep=keep,
        file=spec.data_file,
        raw_sha=data_sha,
        dataset=spec.dataset,
        units=units,
        flags=flags,
    )
    rep["excluded_columns"] = list(spec.excluded)
    rep["lineage"] = {c: {"title": cb[c]["title"], "unit": cb[c]["unit"], "source": cb[c]["source"]} for c in keep}
    return frame, rep


def _origin_summary(full: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for o in full.get("origins") or []:
        lic = o.get("license") or {}
        out.append(
            {
                "producer": o.get("producer"),
                "title": o.get("title"),
                "date_published": o.get("datePublished"),
                "licence": lic.get("name"),
                "licence_url": lic.get("url"),
            }
        )
    return out


def parse_grapher(
    slug: str, dataset: str, csv_body: bytes, meta_body: bytes, full_meta: dict[str, bytes], csv_sha: str
) -> tuple[pl.DataFrame | None, dict[str, Any]]:
    meta = json.loads(meta_body)
    cols = meta.get("columns") or {}
    header = next(csv.reader(io.StringIO(csv_body[:100_000].decode("utf-8-sig"), newline="")))
    if tuple(header[:3]) != ID_COLUMNS_GRAPHER:
        raise OwidParseError(f"{slug}.csv: leading columns {header[:3]} != {ID_COLUMNS_GRAPHER}")
    keep: list[str] = []
    rep: dict[str, Any] = {"slug": slug, "refused_columns": [], "lineage": {}}
    units: dict[str, str] = {}
    flags: dict[str, str] = {}
    for c in header[3:]:
        m = cols.get(c)
        if m is None:
            raise OwidParseError(f"{slug}: column {c!r} has no metadata")
        vid = str(m.get("owidVariableId"))
        fm_raw = full_meta.get(vid)
        if fm_raw is None:
            raise OwidParseError(f"{slug}: no full metadata fetched for variable {vid}")
        fm = json.loads(fm_raw)
        origins = _origin_summary(fm)
        if fm.get("nonRedistributable"):
            rep["refused_columns"].append({"column": c, "reason": "nonRedistributable", "origins": origins})
            continue
        keep.append(c)
        units[c] = (m.get("unit") or m.get("shortUnit") or "as defined by OWID (no unit)")[:100]
        flags[c] = canon(
            original_source=(m.get("citationShort") or "")[:2000],
            original_producers=sorted({o["producer"] for o in origins if o.get("producer")}),
            owid_processing=fm.get("processingLevel"),
            owid_variable_id=vid,
        )
        rep["lineage"][c] = {
            "title": m.get("titleShort"),
            "unit": units[c],
            "origins": origins,
            "processing": fm.get("processingLevel"),
        }
    if not keep:
        return None, rep
    frame, r2 = _long_frame(
        csv_body,
        id_cols=ID_COLUMNS_GRAPHER,
        keep=keep,
        file=f"{slug}.csv",
        raw_sha=csv_sha,
        dataset=dataset,
        units=units,
        flags=flags,
    )
    rep.update(r2)
    return frame, rep


# --------------------------------------------------------------------------- connector
class OwidConnector:
    def __init__(self, charts: dict[str, tuple[str, str]] | None = None) -> None:
        self.charts = dict(GRAPHER_CHARTS if charts is None else charts)

    def describe(self) -> ConnectorSpec:
        return common.atlas_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="Our World in Data (compiler; original producers cited per series)",
            licence=LICENCE,
            allowed_hosts=(GH_HOST, GRAPHER_HOST, API_HOST),
            dataset_ids=tuple(d.repo for d in GITHUB_DATASETS) + tuple(f"grapher:{s}" for s in self.charts),
            field_mappings={
                "atlas_tables": [f"{SOURCE}.{d.dataset}" for d in GITHUB_DATASETS]
                + [f"{SOURCE}.{v[0]}" for v in self.charts.values()]
            },
            units={},
            geography=("all",),
            time_semantics="calendar years",
            lineage_family="owid_compilation",
            cadence_seconds=7 * common.DAY,
            release_aware=False,
            revision_strategy="OWID republishes continuously; each changed file = a new atlas table version",
            max_response_bytes=52_428_800,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=common.code_digest(sys.modules[__name__]),
        )

    def fetch_items(self) -> tuple[FetchItem, ...]:
        csv_t = ("text/csv", "text/plain", "application/octet-stream")
        items: list[FetchItem] = []
        for d in GITHUB_DATASETS:
            items.append(
                FetchItem(key=f"gh:{d.dataset}:data", url=d.data_url, expected_media_types=csv_t, max_bytes=50_000_000)
            )
            items.append(
                FetchItem(
                    key=f"gh:{d.dataset}:codebook", url=d.codebook_url, expected_media_types=csv_t, max_bytes=2_000_000
                )
            )
        for slug in self.charts:
            base = f"https://{GRAPHER_HOST}/grapher/{slug}"
            items.append(
                FetchItem(
                    key=f"g:{slug}:csv",
                    url=f"{base}.csv?{GRAPHER_QS}",
                    expected_media_types=csv_t,
                    max_bytes=30_000_000,
                )
            )
            items.append(
                FetchItem(
                    key=f"g:{slug}:meta",
                    url=f"{base}.metadata.json?{GRAPHER_QS}",
                    max_bytes=2_000_000,
                    params={"slug": slug},
                )
            )
        return tuple(items)

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        if not raw.item.key.endswith(":meta"):
            return ()
        meta = json.loads(raw.body)
        out = []
        for c in (meta.get("columns") or {}).values():
            url = c.get("fullMetadata")
            if not url or urlsplit(url).hostname != API_HOST:
                raise OwidParseError(f"{raw.item.key}: fullMetadata URL {url!r} is not on {API_HOST}")
            out.append(
                FetchItem(
                    key=f"fm:{c.get('owidVariableId')}",
                    url=url,
                    max_bytes=2_000_000,
                    params={"variable": str(c.get("owidVariableId"))},
                )
            )
        return tuple(out)

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await common.fetch_plain(item, http)

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        by = {r.item.key: r for r in raws}
        sha = {k: common.sha256_hex(r.body) for k, r in by.items()}
        builds: list[AtlasBuild] = []
        for d in GITHUB_DATASETS:
            kd, kc = f"gh:{d.dataset}:data", f"gh:{d.dataset}:codebook"
            if kd not in by or kc not in by:
                raise OwidParseError(f"missing raw items for {d.dataset}")
            frame, rep = parse_github(d, by[kd].body, by[kc].body, sha[kd])
            etag = by[kd].response_metadata.get("etag") or (by[kd].response_metadata.get("headers") or {}).get("etag")
            producers = sorted({v["source"] for v in rep["lineage"].values()})
            builds.append(
                AtlasBuild(
                    source=SOURCE,
                    dataset=d.dataset,
                    frame=frame,
                    raw_sha256s=[sha[kd], sha[kc]],
                    parser_version=PARSER_VERSION,
                    coverage_note=(
                        f"OWID {d.repo} ({d.data_file}, ETag {etag}); codebook {d.codebook_file} sha256 {sha[kc]}. "
                        f"Excluded pass-through columns: {', '.join(d.excluded)}. Original source per cell in "
                        "flags.original_source. Regions/historical areas kept unmapped."
                    ),
                    geo_scheme=GEO_SCHEME,
                    rights={
                        "licence": LICENCE,
                        "attribution": ATTRIBUTION,
                        "original_sources": producers[:200],
                        "licence_checked": "2026-10-15",
                    },
                    report=rep,
                )
            )
        for slug, (dataset, _why) in self.charts.items():
            kc, km = f"g:{slug}:csv", f"g:{slug}:meta"
            if kc not in by or km not in by:
                raise OwidParseError(f"missing raw items for grapher chart {slug}")
            fm = {str(r.item.params.get("variable")): r.body for k, r in by.items() if k.startswith("fm:")}
            gframe, rep = parse_grapher(slug, dataset, by[kc].body, by[km].body, fm, sha[kc])
            if gframe is None:
                continue
            used_fm = [
                sha[f"fm:{v}"] for v in (json.loads(x)["owid_variable_id"] for x in gframe["flags"].unique().to_list())
            ]
            origins = sorted(
                {f"{o['producer']} ({o['licence']})" for li in rep["lineage"].values() for o in li["origins"]}
            )
            builds.append(
                AtlasBuild(
                    source=SOURCE,
                    dataset=dataset,
                    frame=gframe,
                    raw_sha256s=sorted({sha[kc], sha[km], *used_fm}),
                    parser_version=PARSER_VERSION,
                    coverage_note=(
                        f"OWID grapher chart '{slug}' (CSV + chart metadata + per-indicator full metadata). "
                        f"Original producers: {'; '.join(origins)}. Refused columns: "
                        f"{[c['column'] for c in rep['refused_columns']]}."
                    ),
                    geo_scheme=GEO_SCHEME,
                    rights={
                        "licence": LICENCE,
                        "attribution": ATTRIBUTION,
                        "original_sources": origins,
                        "licence_checked": "2026-10-15",
                    },
                    report=rep,
                )
            )
        return builds


__all__ = [
    "CONNECTOR_NAME",
    "GITHUB_DATASETS",
    "GRAPHER_CHARTS",
    "OwidConnector",
    "parse_codebook",
    "parse_github",
    "parse_grapher",
]
