"""Global climate indicators as monthly (or finer) series + ``climate_index`` KG entities (V4CLIMATE).

One atlas source (``atlas.climate_indices``, source key ``climate.global_indices``) over several public providers;
every table keeps its own provider rights. All files are small plain-text/CSV downloads (~1.5 MB per run):

=================  =====================================================================  =========================
dataset            provider file(s)                                                       rights
=================  =====================================================================  =========================
``noaa_gml``       NOAA GML ``co2_mm_mlo.csv`` (Mauna Loa CO2, monthly, ppm), ``co2_mm_gl``,  US Gov (NOAA), cite GML
                   ``ch4_mm_gl`` (ppb), ``n2o_mm_gl`` (ppb): global marine surface means
``gistemp``        NASA GISS GISTEMP v4 ``GLB.Ts+dSST.csv`` (land-ocean global means, °C     US Gov (NASA), cite
                   anomaly vs 1951-1980), monthly + Jan-Dec annual
``hadcrut5``       Met Office HadCRUT 5.2.0.0 global monthly summary series (°C anomaly vs   Open Government Licence v3
                   1961-1990) with its 95 % confidence limits
``berkeley``       Berkeley Earth ``Land_and_Ocean_complete.txt`` global monthly anomaly       CC BY-NC 4.0 (non-commercial)
                   (vs Jan 1951-Dec 1980), both sea-ice variants
``enso_oni``       NOAA CPC ``oni.ascii.txt`` Oceanic Niño Index (3-month seasons); checked   US Gov (NOAA)
                   against NOAA PSL ``oni.data`` (the same index listed by centre month)
``sea_ice``        NSIDC Sea Ice Index G02135 v4.0 monthly extent and area, Arctic (N) and   US Gov (NOAA@NSIDC), cite
                   Antarctic (S), 24 files ``<H>_<MM>_extent_v4.0.csv``                       doi:10.7265/a98x-0f50
``sea_level``      NOAA STAR Laboratory for Satellite Altimetry global mean sea level         US Gov (NOAA), acknowledge
                   (``slr_sla_gbl_free_ref_90.csv``, per altimetry mission); EPA Climate      US Gov (EPA)
                   Indicators sea-level figure 1 (CSIRO reconstruction + NOAA, annual, inches)
=================  =====================================================================  =========================

Rules: values are the provider's text as strict decimals; provider sentinels (``***``, ``NaN``, ``-9999``,
``-99.9``) are never rows (listed in the report). Every row's flags carry the provider definition (quoted from the
archived file when the file states it), the baseline of an anomaly, uncertainty fields as published, and
``licence``. Geography: these are global/hemispheric/station series, never countries (``provider_geo`` =
``GLOBAL``, ``NH``/``SH``, ``MLO``; ``iso3`` null, listed as unmapped by design).

Periods: month rows ``[Y-M-01, next month)``; ONI rows the 3-month season (``DJF 1950`` = 1949-12-01..1950-03-01;
CPC labels a season by the year of its centre month, which REVA checks at every run against PSL's centre-month
listing); GISTEMP ``J-D`` = calendar year; EPA annual rows = calendar year; STAR rows are dated by the provider's
decimal year, converted to the day ``Jan 1 + round(fraction x days in year)`` (formula in the flags; the decimal
year is kept verbatim in the selector).

KG: one ``climate_index`` entity per published series (``climind:<key>``) with its atlas series address
(``climind:atlas_series``), unit, provider definition (verbatim), baseline and licence. Nothing causal: an index
is a measurement series, it explains nothing by itself.
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from reva.adapters.connectors import atlas_climate_common as cc
from reva.adapters.connectors import atlas_infra_common as ic
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.climate_indices"
VERSION = "1.0.0"
SOURCE_KEY = "climate.global_indices"
ATLAS_SOURCE = "climind"
PARSER_VERSION = "atlas-climate-indices/1"
DATASETS = ("noaa_gml", "gistemp", "hadcrut5", "berkeley", "enso_oni", "sea_ice", "sea_level")
MAX_BYTES = 2_000_000

GML = "https://gml.noaa.gov/webdata/ccgg/trends/"
NSIDC = "https://noaadata.apps.nsidc.org/NOAA/G02135/"
HADCRUT_VERSION = "5.2.0.0"


@dataclass(frozen=True)
class File:
    key: str
    dataset: str
    url: str
    media: tuple[str, ...] = ("text/csv", "text/plain", "application/octet-stream")


FILES: tuple[File, ...] = (
    File("co2_mm_mlo", "noaa_gml", GML + "co2/co2_mm_mlo.csv"),
    File("co2_mm_gl", "noaa_gml", GML + "co2/co2_mm_gl.csv"),
    File("ch4_mm_gl", "noaa_gml", GML + "ch4/ch4_mm_gl.csv"),
    File("n2o_mm_gl", "noaa_gml", GML + "n2o/n2o_mm_gl.csv"),
    File("gistemp_glb", "gistemp", "https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.csv"),
    File(
        "hadcrut5_global",
        "hadcrut5",
        f"https://www.metoffice.gov.uk/hadobs/hadcrut5/data/HadCRUT.{HADCRUT_VERSION}/analysis/diagnostics/"
        f"HadCRUT.{HADCRUT_VERSION}.analysis.summary_series.global.monthly.csv",
    ),
    File(
        "berkeley_land_ocean",
        "berkeley",
        "https://berkeley-earth-temperature.s3.us-west-1.amazonaws.com/Global/Land_and_Ocean_complete.txt",
    ),
    File("cpc_oni", "enso_oni", "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"),
    File("psl_oni", "enso_oni", "https://psl.noaa.gov/data/correlation/oni.data"),
    *(
        File(
            f"nsidc_{h}_{m:02d}",
            "sea_ice",
            f"{NSIDC}{'north' if h == 'N' else 'south'}/monthly/data/{h}_{m:02d}_extent_v4.0.csv",
        )
        for h in ("N", "S")
        for m in range(1, 13)
    ),
    File(
        "star_gmsl",
        "sea_level",
        "https://www.star.nesdis.noaa.gov/socd/lsa/SeaLevelRise/slr/slr_sla_gbl_free_ref_90.csv",
    ),
    File("epa_sea_level", "sea_level", "https://www.epa.gov/system/files/other-files/2024-06/sea-level_fig-1.csv"),
)
BY_KEY = {f.key: f for f in FILES}
HOSTS = tuple(sorted({re.sub(r"^https://([^/]+)/.*$", r"\1", f.url) for f in FILES}))

RIGHTS: dict[str, dict[str, str]] = {
    "noaa_gml": {
        "licence": "US Government work (NOAA), public domain in the US; cite NOAA GML",
        "terms_quote": "These data are made freely available to the public and the scientific community ... To ensure "
        "that GML receives fair credit for their work please include relevant citation text in publications.",
        "attribution": "NOAA Global Monitoring Laboratory (gml.noaa.gov/ccgg/trends); Mauna Loa 1958-1974 data from "
        "C. D. Keeling, Scripps Institution of Oceanography",
    },
    "gistemp": {
        "licence": "US Government work (NASA), public domain in the US; cite GISTEMP",
        "attribution": "GISTEMP Team: GISS Surface Temperature Analysis (GISTEMP), version 4. NASA Goddard Institute "
        "for Space Studies, https://data.giss.nasa.gov/gistemp/ (dataset accessed on the retrieval date)",
    },
    "hadcrut5": {
        "licence": "Open Government Licence v3 (Crown copyright, Met Office)",
        "licence_url": "http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
        "attribution": f"HadCRUT.{HADCRUT_VERSION} data were obtained from http://www.metoffice.gov.uk/hadobs/hadcrut5 "
        "on the retrieval date and are © British Crown Copyright, Met Office, provided under an Open Government "
        "License; Morice et al. (2021) doi:10.1029/2019JD032361",
    },
    "berkeley": {
        "licence": "CC BY-NC 4.0 (non-commercial use only)",
        "licence_url": "https://creativecommons.org/licenses/by-nc/4.0/",
        "non_commercial": "true",
        "terms_quote": "In general, Berkeley Earth's data is licensed under Creative Commons BY-NC 4.0 International "
        "for non-commercial use only.",
        "attribution": "Berkeley Earth (www.berkeleyearth.org); Rohde, R. A. and Hausfather, Z.: The Berkeley Earth "
        "Land/Ocean Temperature Record, Earth Syst. Sci. Data, 12, 3469-3479, doi:10.5194/essd-12-3469-2020",
    },
    "enso_oni": {
        "licence": "US Government work (NOAA), public domain in the US",
        "attribution": "NOAA Climate Prediction Center (cpc.ncep.noaa.gov); NOAA Physical Sciences Laboratory listing",
    },
    "sea_ice": {
        "licence": "US Government work (NOAA@NSIDC); citation required",
        "terms_quote": "As a condition of using these data, you must cite the use of this data set.",
        "attribution": "Fetterer, F. et al.: Sea Ice Index, Version 4 (G02135), NSIDC, doi:10.7265/a98x-0f50",
    },
    "sea_level": {
        "licence": "US Government works (NOAA STAR; US EPA), public domain in the US; NOAA LSA acknowledgement",
        "terms_quote": "Altimetry data are provided by NOAA Laboratory for Satellite Altimetry.",
        "attribution": "NOAA Laboratory for Satellite Altimetry; US EPA Climate Change Indicators (data: CSIRO, 2017; "
        "NOAA, 2024)",
    },
}


class IndicesError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> IndicesError:
    return IndicesError(msg, retryable=False, kind=kind)


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__], cc)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="NOAA GML/CPC/PSL/STAR/NSIDC, NASA GISS, Met Office Hadley Centre, Berkeley Earth, US EPA",
        licence="per table: US Government works (NOAA, NASA, EPA), Open Government Licence v3 (HadCRUT5), "
        "CC BY-NC 4.0 (Berkeley Earth, non-commercial)",
        allowed_hosts=HOSTS,
        datasets=DATASETS,
        dataset_ids=tuple(f.url for f in FILES),
        cadence_seconds=30 * ic.DAY,
        planned_requests=len(FILES),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="global monthly CO2/CH4/N2O, temperature anomalies, ENSO ONI, sea ice, sea level; not countries",
    )


# --------------------------------------------------------------------------- series registry (KG entities)
@dataclass(frozen=True)
class Series:
    key: str  # climind:<key>
    dataset: str
    indicator: str
    geo: str
    unit: str
    label_en: str
    label_fr: str
    baseline: str | None = None


SERIES: tuple[Series, ...] = (
    Series(
        "co2_mlo",
        "noaa_gml",
        "co2_mlo|average",
        "MLO",
        "ppm",
        "Mauna Loa monthly mean CO2",
        "CO2 moyen mensuel à Mauna Loa",
    ),
    Series(
        "co2_mlo_deseasonalized",
        "noaa_gml",
        "co2_mlo|deseasonalized",
        "MLO",
        "ppm",
        "Mauna Loa monthly CO2, seasonal cycle removed",
        "CO2 mensuel à Mauna Loa, désaisonnalisé",
    ),
    Series(
        "co2_global",
        "noaa_gml",
        "co2_gl|average",
        "GLOBAL",
        "ppm",
        "Global marine surface monthly mean CO2",
        "CO2 moyen mensuel global (surface marine)",
    ),
    Series(
        "ch4_global",
        "noaa_gml",
        "ch4_gl|average",
        "GLOBAL",
        "ppb",
        "Global marine surface monthly mean CH4",
        "CH4 moyen mensuel global (surface marine)",
    ),
    Series(
        "n2o_global",
        "noaa_gml",
        "n2o_gl|average",
        "GLOBAL",
        "ppb",
        "Global marine surface monthly mean N2O",
        "N2O moyen mensuel global (surface marine)",
    ),
    Series(
        "gistemp_global",
        "gistemp",
        "land_ocean_anomaly",
        "GLOBAL",
        "°C",
        "GISTEMP v4 global land-ocean temperature anomaly",
        "Anomalie de température globale terre-océan GISTEMP v4",
        "1951-1980",
    ),
    Series(
        "hadcrut5_global",
        "hadcrut5",
        "anomaly",
        "GLOBAL",
        "°C",
        "HadCRUT5 global near-surface temperature anomaly",
        "Anomalie de température globale HadCRUT5",
        "1961-1990",
    ),
    Series(
        "berkeley_global_air_ice",
        "berkeley",
        "anomaly|sea_ice=air",
        "GLOBAL",
        "°C",
        "Berkeley Earth global land/ocean anomaly (air temperature over sea ice)",
        "Anomalie globale Berkeley Earth terre/océan (température de l'air sur la banquise)",
        "Jan 1951-Dec 1980",
    ),
    Series(
        "berkeley_global_water_ice",
        "berkeley",
        "anomaly|sea_ice=water",
        "GLOBAL",
        "°C",
        "Berkeley Earth global land/ocean anomaly (water temperature under sea ice)",
        "Anomalie globale Berkeley Earth terre/océan (température de l'eau sous la banquise)",
        "Jan 1951-Dec 1980",
    ),
    Series(
        "oni",
        "enso_oni",
        "oni_anomaly",
        "NINO34",
        "°C",
        "Oceanic Niño Index (ONI, ENSO)",
        "Indice océanique Niño (ONI, ENSO)",
    ),
    Series(
        "sea_ice_extent_arctic",
        "sea_ice",
        "extent",
        "NH",
        "10^6 km2",
        "Arctic sea ice extent (monthly)",
        "Étendue de la banquise arctique (mensuelle)",
    ),
    Series(
        "sea_ice_extent_antarctic",
        "sea_ice",
        "extent",
        "SH",
        "10^6 km2",
        "Antarctic sea ice extent (monthly)",
        "Étendue de la banquise antarctique (mensuelle)",
    ),
    Series(
        "gmsl_altimetry",
        "sea_level",
        "gmsl_anomaly|mission=all",
        "GLOBAL",
        "mm",
        "Global mean sea level anomaly from satellite altimetry (NOAA STAR)",
        "Anomalie du niveau moyen global des mers par altimétrie satellitaire (NOAA STAR)",
    ),
    Series(
        "gmsl_csiro_reconstruction",
        "sea_level",
        "csiro_adjusted_sea_level",
        "GLOBAL",
        "inches",
        "Global average absolute sea level change, CSIRO reconstruction (EPA indicator)",
        "Variation du niveau absolu moyen des mers, reconstruction CSIRO (indicateur EPA)",
        "1880",
    ),
)

KG_PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "climind:atlas_series": ("string", "climind_atlas_series", "atlas series", "série de l'atlas"),
    "climind:unit": ("string", "climind_unit", "unit (as published)", "unité (publiée)"),
    "climind:provider_definition": (
        "string",
        "climind_provider_definition",
        "provider definition (verbatim)",
        "définition du fournisseur (verbatim)",
    ),
    "climind:baseline": ("string", "climind_baseline", "anomaly baseline period", "période de référence de l'anomalie"),
    "climind:licence": ("string", "climind_licence", "licence of the values", "licence des valeurs"),
    "climind:provider_file": ("url", "climind_provider_file", "provider file", "fichier du fournisseur"),
}


# --------------------------------------------------------------------------- parsing
@dataclass
class Fetched:
    key: str
    body: bytes
    sha: str
    url: str
    retrieved_at: datetime

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _lines(t: str) -> list[str]:
    return t.replace("\r\n", "\n").split("\n")


def _flags(ds: str, **kw: Any) -> dict[str, Any]:
    return {"licence": RIGHTS[ds]["licence"], "estimate_kind": "observation", **kw}


def parse_gml(f: Fetched, rows: cc.ValueRows, report: dict[str, Any]) -> dict[str, str]:
    """NOAA GML monthly CSV (``#`` header). Returns the verbatim definition lines (units sentence)."""
    lines = _lines(f.text)
    header = [ln[1:].strip() for ln in lines if ln.startswith("#")]
    data = [ln for ln in lines if ln and not ln.startswith("#")]
    rdr = csv.DictReader(io.StringIO("\n".join(data)), skipinitialspace=True)
    gas, geo_code = f.key.split("_mm_")
    unit = "ppm" if gas == "co2" else "ppb"
    geo = "MLO" if geo_code == "mlo" else "GLOBAL"
    if geo_code == "mlo":  # the MLO file states the method, not the unit (ppm per gml.noaa.gov/ccgg/trends)
        unit_line = next((h for h in header if h.startswith("Monthly mean CO2 constructed from daily mean")), "")
        unit_note = "ppm: unit not stated in co2_mm_mlo.csv (NOAA GML trends pages); same series family as co2_mm_gl"
    else:
        unit_line = next((h for h in header if "expressed as a mole fraction" in h), "")
        unit_note = None
    if not unit_line or (geo_code != "mlo" and f"abbreviated as {unit}" not in unit_line):
        raise _err(f"{f.key}: definition/unit sentence ({unit}) not found in the file header")
    prefix = f"{gas}_{geo_code}"
    if geo_code == "mlo":
        cols = {"average": "average", "deseasonalized": "deseasonalized"}
        need = {"year", "month", "decimal date", "average", "deseasonalized", "ndays", "sdev", "unc"}
    else:
        cols = {"average": "average", "trend": "trend"}
        need = {"year", "month", "decimal", "average", "average_unc", "trend", "trend_unc"}
    if not need <= set(rdr.fieldnames or ()):
        raise _err(f"{f.key}: columns {rdr.fieldnames} lack {sorted(need - set(rdr.fieldnames or ()))}")
    n = 0
    for i, r in enumerate(rdr):
        y, m = int(r["year"]), int(r["month"])
        for col, sub in cols.items():
            v = cc.decimal_text(r[col])
            if v is None or Decimal(v) < 0:
                report.setdefault("sentinels", []).append(f"{f.key}:{y}-{m:02d}:{col}")
                continue
            fl = _flags("noaa_gml", gas=gas.upper(), definition=unit_line)
            if unit_note:
                fl["unit_note"] = unit_note
            if geo_code == "mlo":
                fl |= {"ndays": r["ndays"], "sdev": r["sdev"], "unc": r["unc"]}
                if Decimal(r["sdev"]) < 0 or Decimal(r["unc"]) < 0:
                    fl["provider_note"] = (
                        "Missing months have been interpolated, for NOAA data indicated by negative stdev and "
                        "uncertainty. We have no information for SIO data about Ndays, stdv, unc so that they are "
                        "also indicated by negative numbers"
                    )
            else:
                u = r["average_unc"] if sub == "average" else r["trend_unc"]
                fl["unc"] = u
                if Decimal(u) < 0:
                    fl["provider_note"] = "A default value (-9.9) indicates the uncertainty has not yet been calculated"
            if sub in ("trend", "deseasonalized"):
                fl["series_kind"] = "provider-smoothed / deseasonalized series (as published)"
            rows.add(
                indicator=f"{prefix}|{sub}",
                provider_geo=geo,
                period=ic.month_span(y, m),
                value=v,
                unit=unit,
                raw_sha256=f.sha,
                selector={"file": f.key, "data_row": i + 1, "column": col},
                flags=fl,
            )
            n += 1
    report.setdefault("rows", {})[f.key] = n
    return {prefix: unit_line}


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def parse_gistemp(f: Fetched, rows: cc.ValueRows, report: dict[str, Any]) -> None:
    lines = _lines(f.text)
    if not lines[0].startswith("Land-Ocean: Global Means"):
        raise _err(f"GISTEMP title line changed: {lines[0][:80]!r}")
    rdr = csv.DictReader(io.StringIO("\n".join(lines[1:])))
    if not {"Year", *MONTHS, "J-D"} <= set(rdr.fieldnames or ()):
        raise _err("GISTEMP columns changed")
    fl = _flags("gistemp", baseline="1951-1980 (GISTEMP)", title=lines[0].strip())
    n = 0
    for i, r in enumerate(rdr):
        y = int(r["Year"])
        for mi, mon in enumerate(MONTHS, start=1):
            v = cc.decimal_text(r[mon])
            if v is None:
                continue  # '***' = not yet available
            rows.add(indicator="land_ocean_anomaly", provider_geo="GLOBAL", period=ic.month_span(y, mi), value=v,
                     unit="°C", raw_sha256=f.sha, selector={"file": f.key, "data_row": i + 1, "column": mon},
                     flags=fl)  # fmt: skip
            n += 1
        v = cc.decimal_text(r["J-D"])
        if v is not None:
            rows.add(indicator="land_ocean_anomaly|annual=J-D", provider_geo="GLOBAL", period=ic.year_span(y), value=v,
                     unit="°C", raw_sha256=f.sha, selector={"file": f.key, "data_row": i + 1, "column": "J-D"},
                     flags=fl | {"aggregation": "provider annual mean (J-D)"})  # fmt: skip
            n += 1
    report.setdefault("rows", {})[f.key] = n


def parse_hadcrut(f: Fetched, rows: cc.ValueRows, report: dict[str, Any]) -> None:
    rdr = csv.DictReader(io.StringIO(f.text))
    cols = ["Time", "Anomaly (deg C)", "Lower confidence limit (2.5%)", "Upper confidence limit (97.5%)"]
    if list(rdr.fieldnames or ()) != cols:
        raise _err(f"HadCRUT columns changed: {rdr.fieldnames}")
    n = 0
    for i, r in enumerate(rdr):
        y, m = (int(x) for x in r["Time"].split("-"))
        v = cc.decimal_text(r["Anomaly (deg C)"])
        if v is None:
            continue
        fl = _flags("hadcrut5", version=HADCRUT_VERSION, baseline="1961-1990 (HadCRUT5 documentation)",
                    lower_2_5=r[cols[2]], upper_97_5=r[cols[3]])  # fmt: skip
        rows.add(indicator="anomaly", provider_geo="GLOBAL", period=ic.month_span(y, m), value=v, unit="°C",
                 raw_sha256=f.sha, selector={"file": f.key, "data_row": i + 1, "column": cols[1]}, flags=fl)  # fmt: skip
        n += 1
    report.setdefault("rows", {})[f.key] = n


_BE_SECTIONS = {
    "Global Average Temperature Anomaly with Sea Ice Temperature Inferred from Air Temperatures": "air",
    "Global Average Temperature Anomaly with Sea Ice Temperature Inferred from Water Temperatures": "water",
}


def parse_berkeley(f: Fetched, rows: cc.ValueRows, report: dict[str, Any]) -> None:
    lines = _lines(f.text)
    head = " ".join(ln[1:].strip() for ln in lines if ln.startswith("%"))
    baseline = "relative to the Jan 1951-Dec 1980 average"
    if baseline not in head or "Temperatures are in Celsius and reported as anomalies" not in head:
        raise _err("Berkeley Earth header no longer states the units/baseline")
    section = None
    seen: set[str] = set()
    n = 0
    for i, ln in enumerate(lines):
        if ln.startswith("%"):
            t = ln[1:].strip()
            if t in _BE_SECTIONS:
                section = _BE_SECTIONS[t]
                seen.add(section)
            continue
        parts = ln.split()
        if not parts:
            continue
        if section is None or len(parts) != 12:
            raise _err(f"Berkeley line {i + 1}: unexpected layout")
        y, m = int(parts[0]), int(parts[1])
        v = cc.decimal_text(parts[2]) if parts[2] != "NaN" else None
        if v is None:
            continue
        fl = _flags("berkeley", baseline="Jan 1951-Dec 1980", uncertainty_95=parts[3],
                    definition="Temperatures are in Celsius and reported as anomalies relative to the Jan 1951-Dec "
                    "1980 average. Uncertainties represent the 95% confidence interval for statistical and spatial "
                    "undersampling effects as well as ocean biases.", sea_ice_variant=section)  # fmt: skip
        rows.add(indicator=f"anomaly|sea_ice={section}", provider_geo="GLOBAL", period=ic.month_span(y, m), value=v,
                 unit="°C", raw_sha256=f.sha, selector={"file": f.key, "line": i + 1, "column": "Monthly Anomaly"},
                 flags=fl)  # fmt: skip
        n += 1
    if seen != {"air", "water"}:
        raise _err(f"Berkeley sections found: {sorted(seen)}")
    report.setdefault("rows", {})[f.key] = n


SEASONS = ("DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ")


def season_span(season: str, year: int) -> tuple[date, date, int]:
    """CPC season label + year of its centre month -> (start, end, centre month)."""
    centre = SEASONS.index(season) + 1  # DJF -> January
    cy, cm = year, centre
    sy, sm = (cy - 1, 12) if cm == 1 else (cy, cm - 1)
    ey, em = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
    return date(sy, sm, 1), ic.month_span(ey, em)[1], centre


def parse_psl_oni(f: Fetched) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    for ln in _lines(f.text)[1:]:
        p = ln.split()
        if len(p) == 13 and p[0].isdigit():
            for m, x in enumerate(p[1:], start=1):
                if x not in ("-99.90", "-99.9"):
                    out[(int(p[0]), m)] = cc.decimal_text(x) or x
    if len(out) < 100:
        raise _err("PSL oni.data: too few values")
    return out


def parse_oni(f: Fetched, psl: Fetched, rows: cc.ValueRows, report: dict[str, Any]) -> None:
    lines = [ln for ln in _lines(f.text) if ln.strip()]
    if lines[0].split() != ["SEAS", "YR", "TOTAL", "ANOM"]:
        raise _err("CPC ONI header changed")
    centre = parse_psl_oni(psl)
    defn = " ".join(ln.strip() for ln in _lines(psl.text) if "running mean" in ln or "Niño 3.4" in ln or "120o" in ln)
    mismatch = []
    n = 0
    for i, ln in enumerate(lines[1:], start=2):
        s, yr, total, anom = ln.split()
        p0, p1, cm = season_span(s, int(yr))
        a = cc.decimal_text(anom)
        if a is None:
            raise _err(f"ONI line {i}: anomaly {anom!r}")
        ref = centre.get((int(yr), cm))
        if ref is not None and Decimal(ref) != Decimal(a):
            mismatch.append(f"{s} {yr}: CPC {a} != PSL {ref}")
        fl = _flags("enso_oni", season=s, season_year_label=yr, definition_psl=defn,
                    season_convention="CPC labels a 3-month season by the year of its centre month (checked "
                    "against NOAA PSL oni.data, listed by centre month)")  # fmt: skip
        rows.add(indicator="oni_anomaly", provider_geo="NINO34", period=(p0, p1), value=a, unit="°C", raw_sha256=f.sha,
                 selector={"file": f.key, "line": i, "column": "ANOM"}, flags=fl)  # fmt: skip
        t = cc.decimal_text(total)
        if t is not None:
            rows.add(indicator="nino34_sst_3month_mean", provider_geo="NINO34", period=(p0, p1), value=t, unit="°C",
                     raw_sha256=f.sha, selector={"file": f.key, "line": i, "column": "TOTAL"}, flags=fl)  # fmt: skip
        n += 1
    if mismatch:
        raise _err("CPC ONI vs PSL centre-month listing: " + "; ".join(mismatch[:5]), kind="inconsistent")
    report.setdefault("rows", {})[f.key] = n


def parse_nsidc(f: Fetched, rows: cc.ValueRows, report: dict[str, Any]) -> None:
    rdr = csv.DictReader(io.StringIO(f.text), skipinitialspace=True)
    if [c.strip() for c in rdr.fieldnames or ()] != ["year", "mo", "source_dataset", "region", "extent", "area"]:
        raise _err(f"{f.key}: columns changed {rdr.fieldnames}")
    hemi = f.key.split("_")[1]
    n = 0
    for i, r in enumerate(rdr):
        r = {k.strip(): (v or "").strip() for k, v in r.items()}
        if r["region"] != hemi:
            raise _err(f"{f.key} row {i + 1}: region {r['region']} != {hemi}")
        y, m = int(r["year"]), int(r["mo"])
        if not f.key.endswith(f"_{m:02d}"):
            raise _err(f"{f.key} row {i + 1}: month {m}")
        for col in ("extent", "area"):
            v = cc.decimal_text(r[col])
            if v is None or Decimal(v) < 0:  # -9999: no data (e.g. Dec 1987-Jan 1988 satellite gap)
                report.setdefault("sentinels", []).append(f"{f.key}:{y}-{m:02d}:{col}")
                continue
            fl = _flags("sea_ice", source_dataset=r["source_dataset"], version="G02135 v4.0",
                        unit_note="million square kilometres (Sea Ice Index user guide)")  # fmt: skip
            rows.add(indicator=col, provider_geo="NH" if hemi == "N" else "SH", period=ic.month_span(y, m), value=v,
                     unit="10^6 km2", raw_sha256=f.sha, selector={"file": f.key, "data_row": i + 1, "column": col},
                     flags=fl)  # fmt: skip
            n += 1
    report.setdefault("rows", {})[f.key] = n


def decimal_year_day(x: Decimal) -> date:
    y = int(x)
    days = 366 if calendar.isleap(y) else 365
    return date(y, 1, 1) + timedelta(days=int((x - y) * days + Decimal("0.5")))


def parse_star(f: Fetched, rows: cc.ValueRows, report: dict[str, Any]) -> str:
    lines = _lines(f.text)
    head = {
        ln[1:].split("=", 1)[0].strip(): ln[1:].split("=", 1)[1].strip()
        for ln in lines
        if ln.startswith("#") and "=" in ln
    }
    title = head.get("title", "")
    if "mean sea level anomaly global ocean" not in title:
        raise _err(f"STAR title changed: {title[:80]!r}")
    data = [ln for ln in lines if ln and not ln.startswith("#")]
    rdr = csv.reader(io.StringIO("\n".join(data)))
    cols = next(rdr)
    if cols[0] != "year" or len(cols) < 2:
        raise _err(f"STAR columns {cols}")
    n = 0
    for i, r in enumerate(rdr):
        t = Decimal(r[0])
        day = decimal_year_day(t)
        for c, x in zip(cols[1:], r[1:], strict=False):
            v = cc.decimal_text(x)
            if v is None:
                continue
            fl = _flags("sea_level", mission=c, title=title, trend_note=head.get("trend"),
                        gia="no glacial isostatic adjustment correction (file header)",
                        period_basis="provider decimal year; day = Jan 1 + round(fraction x days in year)")  # fmt: skip
            rows.add(indicator=f"gmsl_anomaly|mission={c}", provider_geo="GLOBAL", period=ic.day_span(day), value=v,
                     unit="mm", raw_sha256=f.sha,
                     selector={"file": f.key, "data_row": i + 1, "column": c, "decimal_year": r[0]}, flags=fl)  # fmt: skip
            n += 1
    report.setdefault("rows", {})[f.key] = n
    return title


def parse_epa(f: Fetched, rows: cc.ValueRows, report: dict[str, Any]) -> str:
    lines = _lines(f.text.lstrip("﻿"))
    title = lines[0].split(",")[0].strip('"')
    if "Global Average Absolute Sea Level Change" not in title or not any(
        ln.startswith("Units: inches") for ln in lines
    ):
        raise _err("EPA sea level file header changed")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Year,"))
    rdr = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    want = {
        "CSIRO - Adjusted sea level (inches)": "csiro_adjusted_sea_level",
        "NOAA - Adjusted sea level (inches)": "noaa_adjusted_sea_level",
    }
    if not set(want) <= set(rdr.fieldnames or ()):
        raise _err(f"EPA columns {rdr.fieldnames}")
    n = 0
    for i, r in enumerate(rdr):
        if not (r.get("Year") or "").strip().isdigit():
            continue
        y = int(r["Year"])
        for col, ind in want.items():
            v = cc.decimal_text(r[col])
            if v is None:
                continue
            fl = _flags("sea_level", title=title, data_source=lines[2].split(",")[0].strip('"'))
            if ind.startswith("csiro"):
                fl |= {"lower_bound": r["CSIRO - Lower error bound (inches)"],
                       "upper_bound": r["CSIRO - Upper error bound (inches)"]}  # fmt: skip
            rows.add(indicator=ind, provider_geo="GLOBAL", period=ic.year_span(y), value=v, unit="inches",
                     raw_sha256=f.sha, selector={"file": f.key, "line": start + 2 + i, "column": col}, flags=fl)  # fmt: skip
            n += 1
    report.setdefault("rows", {})[f.key] = n
    return title


def build(
    files: Mapping[str, Fetched], wanted: tuple[str, ...] = DATASETS
) -> tuple[dict[str, cc.ValueRows], dict[str, Any]]:
    out = {ds: cc.ValueRows(ATLAS_SOURCE, ds) for ds in wanted}
    report: dict[str, Any] = {}
    defs: dict[str, str] = {}
    if "noaa_gml" in wanted:
        for k in ("co2_mm_mlo", "co2_mm_gl", "ch4_mm_gl", "n2o_mm_gl"):
            defs |= parse_gml(files[k], out["noaa_gml"], report)
    if "gistemp" in wanted:
        parse_gistemp(files["gistemp_glb"], out["gistemp"], report)
    if "hadcrut5" in wanted:
        parse_hadcrut(files["hadcrut5_global"], out["hadcrut5"], report)
    if "berkeley" in wanted:
        parse_berkeley(files["berkeley_land_ocean"], out["berkeley"], report)
    if "enso_oni" in wanted:
        parse_oni(files["cpc_oni"], files["psl_oni"], out["enso_oni"], report)
    if "sea_ice" in wanted:
        for f in FILES:
            if f.dataset == "sea_ice":
                parse_nsidc(files[f.key], out["sea_ice"], report)
    if "sea_level" in wanted:
        defs["star"] = parse_star(files["star_gmsl"], out["sea_level"], report)
        defs["epa"] = parse_epa(files["epa_sea_level"], out["sea_level"], report)
    report["definitions"] = defs
    report["sentinels_dropped"] = len(report.pop("sentinels", []))
    return out, report


# --------------------------------------------------------------------------- KG
SERIES_FILE = {
    "co2_mlo": "co2_mm_mlo", "co2_mlo_deseasonalized": "co2_mm_mlo", "co2_global": "co2_mm_gl",
    "ch4_global": "ch4_mm_gl", "n2o_global": "n2o_mm_gl", "gistemp_global": "gistemp_glb",
    "hadcrut5_global": "hadcrut5_global", "berkeley_global_air_ice": "berkeley_land_ocean",
    "berkeley_global_water_ice": "berkeley_land_ocean", "oni": "cpc_oni", "sea_ice_extent_arctic": "nsidc_N_09",
    "sea_ice_extent_antarctic": "nsidc_S_02", "gmsl_altimetry": "star_gmsl", "gmsl_csiro_reconstruction": "epa_sea_level",
}  # fmt: skip


def kg_items(
    files: Mapping[str, Fetched], defs: Mapping[str, str], wanted: tuple[str, ...]
) -> tuple[list[Any], list[Any]]:
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    psl_def = " ".join(ln.strip() for ln in _lines(files["psl_oni"].text) if "running mean" in ln or "Niño 3.4" in ln
                       or "120o" in ln) if "psl_oni" in files else ""  # fmt: skip
    for s in SERIES:
        if s.dataset not in wanted:
            continue
        f = files[SERIES_FILE[s.key]]
        qid = f"climind:{s.key}"
        ents.append(KgEntity(qid=qid, kind="climate_index", labels={"en": s.label_en, "fr": s.label_fr},
                             descriptions={"en": f"climate index series ({s.dataset}); values in the REVA atlas"},
                             source_revision=f.sha[:16]))  # fmt: skip
        ref = [cc.kg_ref(file=f.key, url=f.url, raw_sha256=f.sha, retrieved_at=f.retrieved_at,
                         licence=RIGHTS[s.dataset]["licence"])]  # fmt: skip
        series = f"{ATLAS_SOURCE}.{s.dataset}.{s.indicator}"
        if s.dataset == "sea_level" and s.key == "gmsl_altimetry":
            series = f"{ATLAS_SOURCE}.sea_level.gmsl_anomaly|mission=<mission>"
        vals: dict[str, str | None] = {
            "climind:atlas_series": f"{series} @ provider_geo={s.geo}",
            "climind:unit": s.unit,
            "climind:baseline": s.baseline,
            "climind:licence": RIGHTS[s.dataset]["licence"],
            "climind:provider_definition": {
                "co2_mlo": defs.get("co2_mlo"),
                "co2_mlo_deseasonalized": defs.get("co2_mlo"),
                "co2_global": defs.get("co2_gl"),
                "ch4_global": defs.get("ch4_gl"),
                "n2o_global": defs.get("n2o_gl"),
                "oni": psl_def or None,
                "gmsl_altimetry": defs.get("star"),
                "gmsl_csiro_reconstruction": defs.get("epa"),
            }.get(s.key),
        }
        for pid, v in vals.items():
            if v:
                sts.append(KgStatement(subject_qid=qid, property_pid=pid, value={"type": "string", "value": v},
                                       references=ref, source_key=SOURCE_KEY, source_revision=f.sha[:16],
                                       statement_id=f"climind:{s.key}:{pid.split(':')[1]}", raw_sha256=f.sha))  # fmt: skip
        sts.append(KgStatement(subject_qid=qid, property_pid="climind:provider_file", value={"type": "url", "value": f.url},
                               references=ref, source_key=SOURCE_KEY, source_revision=f.sha[:16],
                               statement_id=f"climind:{s.key}:provider_file", raw_sha256=f.sha))  # fmt: skip
    return ents, sts


# --------------------------------------------------------------------------- ingest
async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files: dict[str, Fetched] = {}
    for f in FILES:
        if f.dataset not in wanted:
            continue
        got = await ic.fetch_archived(run, f.url, name=f.key, key=f.key, media_types=f.media, max_bytes=MAX_BYTES)
        files[f.key] = Fetched(f.key, got.body, got.sha256, f.url, got.retrieved_at)
    builders, report = build(files, wanted)
    tables = []
    for ds in wanted:
        rb = builders[ds]
        if not rb.rows:
            raise _err(f"{ds}: no rows", kind="incomplete")
        frame = rb.frame()
        out = run.register(
            dataset=ds,
            frame=frame,
            raw_sha256s=sorted(set(frame["raw_sha256"].to_list())),
            geo_scheme="iso3",
            rights={**RIGHTS[ds], "source_key": SOURCE_KEY},
            coverage_note="global/hemispheric/station series (not countries): provider_geo GLOBAL, MLO, NH, SH, "
            "NINO34 kept unmapped by design; provider sentinels dropped (never zero)",
        )
        tables.append({"table": out["dataset_key"].removeprefix("atlas."), "rows": out["row_count"],
                       "created": out["created"], "artifact_sha256": out["artifact_sha256"]})  # fmt: skip
    kg: dict[str, Any] = {"loaded": False}
    if run.engine is not None:
        ents, sts = kg_items(files, report["definitions"], wanted)
        cc.register_props(run.engine, KG_PREDICATES, ())
        kg = {
            "loaded": True,
            "entities": len(ents),
            "statements": len(sts),
            "report": cc.load_kg(
                run.engine,
                run.store,
                entities=ents,
                statements=sts,
                source_key=SOURCE_KEY,
                raw_sha256s=[f.sha for f in files.values()],
                retrieved_at=max(f.retrieved_at for f in files.values()),
                origin=run.origin,
                parameters={"connector": NAME, "parser": PARSER_VERSION},
            ),
        }
    return {"ok": True, "tables": tables, "kg": kg, "report": {k: v for k, v in report.items() if k != "definitions"}}
