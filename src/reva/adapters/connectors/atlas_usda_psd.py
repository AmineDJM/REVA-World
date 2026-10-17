"""USDA FAS Production, Supply and Distribution (PSD) -> atlas tables (V6, agent V6AGRI).

Source (researched 2026-10-17): the PSD Online bulk downloads
``https://apps.fas.usda.gov/psdonline/downloads/psd_grains_pulses_csv.zip`` (2.9 MB, one CSV member, 51.6 MB)
and ``.../psd_oilseeds_csv.zip`` (3.8 MB, 78.4 MB). Both are rewritten by FAS on each WASDE release day (the
``Last-Modified`` header, archived with the bytes, dates the copy). ``psd_alldata_csv.zip`` is not used (all
commodities, larger). The PSD *API* (``apps.fas.usda.gov/OpenData/api/psd``) answers ``403 "Bad API Key"`` without
an api.data.gov key and is not used (owner decision: a key would give GENC country codes, see below).

Licence: work of the U.S. Department of Agriculture (Foreign Agricultural Service), a U.S. Government work: public
domain in the United States (17 U.S.C. 105); no licence restriction is stated on PSD Online. Attribution kept:
"USDA Foreign Agricultural Service, Production, Supply and Distribution (PSD Online)".

Cells (one per CSV row in scope, values verbatim):

* ``indicator`` = ``<Commodity_Code>|<Attribute_ID>`` (PSD's own codes; names verbatim in ``flags``);
* ``provider_geo`` = PSD ``Country_Code``; ``iso3`` through geo scheme ``usda_psd``
  (``registry/atlas/geo_aliases/v6agri.yaml``: an alias exists only when the PSD code equals the GeoNames FIPS
  10-4 code **and** the PSD name equals the GeoNames name; Burma, the Koreas, the EU, historical areas ... stay
  unmapped and are listed in the manifest);
* period = the PSD ``Market_Year`` label ``Y`` as ``[Y-01-01, Y+1-01-01)``. PSD's ``Market_Year`` 2025 is the
  marketing year 2025/26 (the year it begins in); local marketing-year months differ by country and commodity and
  are not in the file (``flags.period_basis`` says so; WASDE's ``2025/26`` labels use the same first-year
  convention in ``atlas_usda_wasde``);
* ``flags.estimate_kind``: PSD does not mark projections per cell. Rule ``psd_latest_market_years`` (stated in every
  cell): per commodity, the latest ``Market_Year`` in the file is USDA's current projection year (WASDE ``Proj.``)
  -> ``projection``; the year before it is WASDE's ``Est.`` year -> ``estimated``; earlier years are
  ``published_history``. Projections are therefore excluded from the atlas answer layer like every other
  projection;
* ``flags.record_updated`` = ``Calendar_Year``-``Month`` of the row (the PSD release in which the row last
  changed), verbatim;
* zeros: PSD writes ``0`` both for a reported zero and for "no activity" (e.g. Rough Production outside rice
  countries); the value is kept as published and flagged ``zero_semantics`` (never presented as a measured zero
  by REVA).

Scope (bounded): every commodity of the grains file (wheat, rice milled, corn, barley, sorghum, millet, oats, rye,
mixed grain; pulses are not in it any more) and the soybean, rapeseed, sunflowerseed and palm complexes of the
oilseeds file (:data:`OILSEED_COMMODITIES`).
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from reva.adapters.connectors.agri_common import canon, module_digest
from reva.adapters.connectors.vintage_common import Rows
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.usda_psd"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_usda_psd/1"
SOURCE_KEY = "usda.psd"
SOURCE = "usda_psd"
GEO_SCHEME = "usda_psd"
HOST = "apps.fas.usda.gov"
BASE = f"https://{HOST}/psdonline/downloads"
LICENCE = "U.S. Government work (USDA FAS PSD Online): public domain in the United States, attribution kept"
TERMS_URL = "https://apps.fas.usda.gov/psdonline/app/index.html#/app/about"
ZIP_MEDIA = ("application/x-zip-compressed", "application/zip", "application/octet-stream")
MAX_ZIP_BYTES = 30_000_000
MAX_MEMBER_BYTES = 400_000_000
HEADER = (
    "Commodity_Code",
    "Commodity_Description",
    "Country_Code",
    "Country_Name",
    "Market_Year",
    "Calendar_Year",
    "Month",
    "Attribute_ID",
    "Attribute_Description",
    "Unit_ID",
    "Unit_Description",
    "Value",
)
#: oilseeds file: soybean, rapeseed, sunflowerseed and palm complexes (oilseed / meal / oil; local soybean series).
OILSEED_COMMODITIES = frozenset(
    {
        "2222000",  # Oilseed, Soybean
        "2222001",  # Oilseed, Soybean (Local)
        "0813100",  # Meal, Soybean
        "0813101",  # Meal, Soybean (Local)
        "4232000",  # Oil, Soybean
        "4232001",  # Oil, Soybean (Local)
        "2226000",  # Oilseed, Rapeseed
        "0813600",  # Meal, Rapeseed
        "4239100",  # Oil, Rapeseed
        "2224000",  # Oilseed, Sunflowerseed
        "0813500",  # Meal, Sunflowerseed
        "4236000",  # Oil, Sunflowerseed
        "4243000",  # Oil, Palm
        "2232000",  # Oilseed, Palm Kernel
        "4244000",  # Oil, Palm Kernel
    }
)
ZERO_NOTE = "PSD writes 0 both for a reported zero and for no activity / no data; kept as published"
PERIOD_BASIS = (
    "PSD Market_Year Y = the marketing year beginning in Y (Y/Y+1); stored as calendar year Y; local "
    "marketing-year months are not stated in the file"
)
ESTIMATE_RULE = "psd_latest_market_years: latest Market_Year per commodity = projection, the year before = estimated"
ATTRIBUTION = "USDA Foreign Agricultural Service, Production, Supply and Distribution (PSD Online)"


@dataclass(frozen=True)
class PsdFile:
    dataset: str
    file: str
    member: str
    commodities: frozenset[str] | None  # None = every commodity of the file

    @property
    def url(self) -> str:
        return f"{BASE}/{self.file}"


FILES: tuple[PsdFile, ...] = (
    PsdFile("grains", "psd_grains_pulses_csv.zip", "psd_grains_pulses.csv", None),
    PsdFile("oilseeds", "psd_oilseeds_csv.zip", "psd_oilseeds.csv", OILSEED_COMMODITIES),
)
FILES_BY_DATASET = {f.dataset: f for f in FILES}


class PsdError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


@dataclass
class PsdReport:
    dataset: str
    rows_in_file: int = 0
    rows_in_scope: int = 0
    cells: int = 0
    zero_values: int = 0
    empty_values: int = 0
    commodities: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)
    latest_market_year: dict[str, int] = field(default_factory=dict)
    estimate_kinds: dict[str, int] = field(default_factory=dict)
    countries: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _member_text(body: bytes, member: str) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise PsdError(f"not a zip archive ({e})") from e
    names = zf.namelist()
    if names != [member]:
        raise PsdError(f"expected the single member {member}, found {names}")
    info = zf.getinfo(member)
    if info.file_size > MAX_MEMBER_BYTES:
        raise PsdError(f"{member}: {info.file_size} bytes uncompressed exceeds {MAX_MEMBER_BYTES}", kind="too_large")
    try:
        data = zf.read(member)  # CRC checked by zipfile
    except (zipfile.BadZipFile, EOFError) as e:
        raise PsdError(f"{member}: corrupt member ({e})") from e
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise PsdError(f"{member}: not UTF-8") from e


def _decimal(v: str) -> str | None:
    from reva.adapters.connectors.vintage_common import DECIMAL_RE

    s = v.strip()
    return s if DECIMAL_RE.match(s) else None


def parse_psd(f: PsdFile, body: bytes, raw_sha256: str, *, last_modified: str | None) -> tuple[Any, PsdReport]:
    """One PSD zip -> (atlas frame, report). Every layout surprise is a typed :class:`PsdError`."""
    text = _member_text(body, f.member)
    reader = csv.reader(io.StringIO(text, newline=""))
    head = tuple(h.strip() for h in next(reader, []))
    if head != HEADER:
        raise PsdError(f"{f.member}: header changed: {head}")
    rep = PsdReport(dataset=f.dataset)
    kept: list[tuple[int, list[str]]] = []
    for line, r in enumerate(reader, start=2):
        if not r:
            continue
        if len(r) != len(HEADER):
            raise PsdError(f"{f.member}: line {line} has {len(r)} fields")
        rep.rows_in_file += 1
        if f.commodities is not None and r[0] not in f.commodities:
            continue
        kept.append((line, r))
    if not kept:
        raise PsdError(f"{f.member}: no row in scope")
    latest: dict[str, int] = {}
    for _line, r in kept:
        if not r[4].isdigit() or len(r[4]) != 4:
            raise PsdError(f"{f.member}: unexpected Market_Year {r[4]!r}")
        latest[r[0]] = max(latest.get(r[0], 0), int(r[4]))
    rep.latest_market_year = dict(sorted(latest.items()))
    rows = Rows()
    kinds: Counter[str] = Counter()
    seen: set[tuple[str, str, str, str]] = set()
    for line, r in kept:
        code, cname, geo, gname, my, cy, mon, attr, aname, _uid, unit, raw_value = r
        rep.rows_in_scope += 1
        rep.commodities.setdefault(code, cname)
        rep.attributes.setdefault(attr, f"{aname} {unit}".strip())
        rep.countries.setdefault(geo, gname)
        key = (code, attr, geo, my)
        if key in seen:
            raise PsdError(f"{f.member}: duplicate row for {key} (line {line})")
        seen.add(key)
        value = _decimal(raw_value)
        if value is None:
            if raw_value.strip():
                raise PsdError(f"{f.member}: line {line}: value {raw_value[:30]!r} is not a number")
            rep.empty_values += 1  # no cell (never a zero)
            continue
        y = int(my)
        kind = "projection" if y == latest[code] else "estimated" if y == latest[code] - 1 else "published_history"
        kinds[kind] += 1
        flags: dict[str, Any] = {
            "commodity": cname,
            "attribute": aname,
            "country_name": gname,
            "market_year": f"{y}/{(y + 1) % 100:02d}",
            "period_basis": PERIOD_BASIS,
            "record_updated": f"{cy}-{mon}",
            "estimate_kind": kind,
            "estimate_rule": ESTIMATE_RULE,
            "file_last_modified": last_modified,
        }
        if float(value) == 0.0:
            rep.zero_values += 1
            flags["zero_semantics"] = ZERO_NOTE
        u = unit.strip().removeprefix("(").removesuffix(")") or "not stated by PSD"
        rows.add(
            source=SOURCE,
            dataset=f.dataset,
            indicator=f"{code}|{attr}",
            provider_geo=geo,
            start=date(y, 1, 1),
            end=date(y + 1, 1, 1),
            value=value,
            unit=u,
            flags=canon(flags),
            raw_sha256=raw_sha256,
            selector={"zip": f.file, "file": f.member, "row": line},
        )
    rep.cells = len(rows)
    rep.estimate_kinds = dict(kinds)
    return rows.frame(), rep


def rights() -> dict[str, Any]:
    return {
        "licence": "Public domain (U.S. Government work, 17 U.S.C. 105)",
        "licence_url": TERMS_URL,
        "attribution": ATTRIBUTION,
        "provider": "USDA Foreign Agricultural Service (FAS)",
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


def coverage_note(f: PsdFile, rep: PsdReport, *, last_modified: str | None, etag: str | None) -> str:
    return (
        f"USDA FAS PSD Online bulk file {f.url} (Last-Modified {last_modified}, ETag {etag}). "
        f"{rep.cells} cells from {rep.rows_in_scope} rows in scope ({rep.rows_in_file} rows in the file); "
        f"{rep.empty_values} empty values not stored; {rep.zero_values} zeros kept as published (PSD does not "
        f"distinguish zero from no activity). Commodities: {len(rep.commodities)}; latest market year per commodity "
        f"{rep.latest_market_year} -> projection, the year before -> estimated (rule {ESTIMATE_RULE}); estimate kinds "
        f"{rep.estimate_kinds}. Period = Market_Year as calendar year (first year of the marketing year). Countries: "
        f"PSD codes, iso3 via scheme usda_psd (FIPS 10-4 + exact name agreement only). Public domain; "
        f"cite {ATTRIBUTION}."
    )[:4000]


def code_digest() -> str:
    return module_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="USDA Foreign Agricultural Service, Production, Supply and Distribution (PSD Online)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(f.dataset for f in FILES),
        dataset_ids=tuple(f.file for f in FILES),
        cadence_seconds=30 * 86_400,
        planned_requests=len(FILES),
        max_response_bytes=MAX_ZIP_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{SOURCE}.{f.dataset}" for f in FILES], "geo_scheme": GEO_SCHEME},
        notes="PSD Online bulk zips (grains; oilseeds bounded to soy/rapeseed/sunflower/palm); ~7 MB per run.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"tables": {}}
    for f in FILES:
        if not run.wants(f.dataset):
            continue
        raw = await run.get(f.url, key=f"psd:{f.file}", media_types=ZIP_MEDIA, max_bytes=MAX_ZIP_BYTES)
        if not raw.body:
            raise SourceResponseError(f"PSD: empty body for {f.file}", retryable=True)
        sha = run.archive(raw)
        headers = {str(k).lower(): v for k, v in (raw.response_metadata.get("headers") or {}).items()}
        lm, etag = headers.get("last-modified"), headers.get("etag")
        frame, rep = parse_psd(f, raw.body, sha, last_modified=lm)
        del raw
        reg = run.register(
            dataset=f.dataset,
            frame=frame,
            raw_sha256s=[sha],
            coverage_note=coverage_note(f, rep, last_modified=lm, etag=etag),
            geo_scheme=GEO_SCHEME,
            rights=rights(),
        )
        out["tables"][f.dataset] = {
            "dataset_key": reg["dataset_key"],
            "rows": reg["row_count"],
            "created": reg["created"],
            "countries": reg.get("coverage", {}).get("countries"),
            "unmapped": reg.get("unmapped"),
            "report": {k: v for k, v in rep.as_dict().items() if k not in ("countries",)},
        }
    return out


__all__ = [
    "CONNECTOR_NAME",
    "FILES",
    "FILES_BY_DATASET",
    "PsdError",
    "atlas_ingest",
    "atlas_source",
    "parse_psd",
]
