"""FAO FishStat Global Production (agent V4ECO): how much fish, shellfish and algae each country catches or farms.

Source
------
FAO Fisheries and Aquaculture, "Global production by production source" release zip
(``https://www.fao.org/fishery/static/Data/GlobalProduction_<release>.zip``, :data:`RELEASE`), CC BY 4.0 (the
release ``ReadMe.txt`` quotes the FAO Statistical Database terms; the connector checks the quote on every run and
blocks if it is gone). One request (6.4 MB). V3's A-FAO did not ingest FishStat because it is published per
species x fishing area only (``docs/dev/v3_a_fao.md``); this connector keeps FAO's dimensions and sums only what
FAO itself tells users to aggregate.

What is computed (deterministic, declared)
------------------------------------------
For each country (``COUNTRY.UN_CODE`` -> ``ISO3_Code`` from FAO's own code list ``CL_FI_COUNTRY_GROUPS.csv``),
calendar year and production source (capture; aquaculture freshwater / brackishwater / marine, as coded in
``CL_FI_PRODUCTION_SOURCE_DET.csv``), the quantities of ``Global_production_quantity.csv`` are **summed over fishing
areas and species** within

* the species' *Yearbook group* (``CL_FI_SPECIES_GROUPS.csv``: "Aquatic animals (Fish, crustaceans and molluscs,
  etc.)", "Algae (Aquatic plants)", "Other aquatic animals & products"; the ReadMe says the FAO Yearbook
  aggregates use the aquatic-animals group), indicator ``<source>.yb_<group>``;
* the species' *Major group* (PISCES, MOLLUSCA, CRUSTACEA, ...), indicator ``<source>.mg_<group>``.

Measures are never mixed: ``Q_tlw`` (tonnes live weight) gives the ``.tonnes`` indicators, ``Q_no_1`` (number of
individuals, e.g. crocodiles, pearls) the ``.number`` indicators. FAO status symbols per summed cell are counted
in ``flags`` (``E``/``I``/``X``/``P`` estimated, imputed, international, provisional). Cells with a *missing*
symbol (``L``, ``M``, ``O``, ``Q``) contribute nothing: a sum that has any of them is flagged
``lower_bound: true``; when all its cells are missing the value is Missing (``flags.missing``), never zero. ``N``
cells ("Not significant (<0.5)") contribute their published 0 and are counted. Species without a Yearbook group in
FAO's code list are summed under ``yb_unassigned`` (reported). Historical / non-ISO FAO areas (Belgium-Luxembourg,
USSR successor splits, "Other NEI", Channel Islands, Sark...) keep their FAO code as ``provider_geo``
``un:<code>`` and are reported unmapped, never re-assigned.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from reva.adapters.connectors.eco_common import canon
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.fishstat"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-fishstat-1"
SOURCE_KEY = "fao.fishstat"
ATLAS_SOURCE = "fishstat"
DATASET = "production"
HOST = "www.fao.org"
RELEASE = "2026.1.0"
URL = f"https://{HOST}/fishery/static/Data/GlobalProduction_{RELEASE}.zip"
MAX_BYTES = 40_000_000
MAX_MEMBER_BYTES = 400_000_000
LICENCE_QUOTE = "Creative Commons Attribution-4.0"
RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0",
    "licence_url": "https://www.fao.org/contact-us/terms/db-terms-of-use/en",
    "attribution": f"FAO. FishStat Data, Global production by production source (release {RELEASE}). Licence: CC-BY-4.0.",
}
MISSING_SYMBOLS = {"L": "not_available", "M": "not_applicable", "O": "not_reported", "Q": "confidential"}
NOTABLE_SYMBOLS = ("E", "I", "X", "P", "N", "B", "F", "G", "S", "U", "V")
SOURCE_SLUG = {
    "CAPTURE": "capture",
    "FRESHWATER": "aquaculture_freshwater",
    "BRACKISHWATER": "aquaculture_brackishwater",
    "MARINE": "aquaculture_marine",
}
MEASURE_SLUG = {"Q_tlw": ("tonnes", "tonnes live weight"), "Q_no_1": ("number", "number of individuals")}
QTY_COLUMNS = ("COUNTRY.UN_CODE", "SPECIES.ALPHA_3_CODE", "AREA.CODE", "PRODUCTION_SOURCE_DET.CODE", "MEASURE",
               "PERIOD", "VALUE", "STATUS")  # fmt: skip


class FishstatError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "unassigned"


def _member(zf: zipfile.ZipFile, name: str) -> str:
    try:
        info = zf.getinfo(name)
    except KeyError as e:
        raise FishstatError(f"{name} missing from the release zip") from e
    if info.file_size > MAX_MEMBER_BYTES:
        raise FishstatError(f"{name} inflates to {info.file_size} bytes", kind="too_large")
    return zf.read(name).decode("utf-8-sig")


def _rows(text: str, required: tuple[str, ...], what: str) -> list[dict[str, str]]:
    rd = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in required if c not in (rd.fieldnames or [])]
    if missing:
        raise FishstatError(f"{what} lacks columns {missing}")
    return list(rd)


@dataclass(frozen=True)
class Species:
    code: str
    yearbook_group: str
    major_group: str


@dataclass
class Release:
    countries: dict[str, str]  # UN code -> ISO3 ("" when FAO gives none)
    country_names: dict[str, str]
    species: dict[str, Species]
    sources: dict[str, str]
    readme_ok: bool
    rows: list[dict[str, str]] = field(default_factory=list)


def parse_release(body: bytes) -> Release:
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise FishstatError("release is not a zip") from e
    readme = _member(zf, "ReadMe.txt")
    if LICENCE_QUOTE not in readme:
        raise SourceResponseError("FishStat ReadMe no longer states CC BY 4.0: run blocked", retryable=False,
                                  kind="licence")  # fmt: skip
    cty = _rows(_member(zf, "CL_FI_COUNTRY_GROUPS.csv"), ("UN_Code", "ISO3_Code", "Name_En"), "country list")
    sp = _rows(_member(zf, "CL_FI_SPECIES_GROUPS.csv"), ("3A_Code", "Yearbook_Group_En", "Major_Group"), "species")
    src = _rows(_member(zf, "CL_FI_PRODUCTION_SOURCE_DET.csv"), ("Code", "Name_En"), "production sources")
    unknown_src = sorted({r["Code"] for r in src} - set(SOURCE_SLUG))
    if unknown_src:
        raise FishstatError(f"unknown production source codes {unknown_src}")
    qty = _rows(_member(zf, "Global_production_quantity.csv"), QTY_COLUMNS, "production quantities")
    return Release(
        countries={r["UN_Code"]: r["ISO3_Code"].strip() for r in cty},
        country_names={r["UN_Code"]: r["Name_En"] for r in cty},
        species={
            r["3A_Code"]: Species(r["3A_Code"], r["Yearbook_Group_En"].strip(), r["Major_Group"].strip()) for r in sp
        },  # fmt: skip
        sources={r["Code"]: r["Name_En"] for r in src},
        readme_ok=True,
        rows=qty,
    )


@dataclass
class Cell:
    total: Decimal = Decimal(0)
    n_values: int = 0
    missing: Counter[str] = field(default_factory=Counter)
    symbols: Counter[str] = field(default_factory=Counter)
    species: set[str] = field(default_factory=set)


def aggregate(rel: Release) -> tuple[dict[tuple[str, str, int], Cell], dict[str, Any]]:
    """(provider_geo, indicator, year) -> Cell; plus a parse report."""
    cells: dict[tuple[str, str, int], Cell] = defaultdict(Cell)
    rep: dict[str, Any] = {"rows": len(rel.rows), "unknown_species": Counter(), "unassigned_yearbook": set(),
                           "unmapped_countries": {}, "unknown_country_codes": set()}  # fmt: skip
    for i, r in enumerate(rel.rows):
        un = r["COUNTRY.UN_CODE"]
        if un not in rel.countries:
            rep["unknown_country_codes"].add(un)
        iso3 = rel.countries.get(un, "")
        geo = iso3 if iso3 else f"un:{un}"
        if not iso3:
            rep["unmapped_countries"][geo] = rel.country_names.get(un, "(not in FAO country list)")
        measure = MEASURE_SLUG.get(r["MEASURE"])
        if measure is None:
            raise FishstatError(f"row {i + 2}: unknown measure {r['MEASURE']!r}")
        src = SOURCE_SLUG.get(r["PRODUCTION_SOURCE_DET.CODE"])
        if src is None:
            raise FishstatError(f"row {i + 2}: unknown production source {r['PRODUCTION_SOURCE_DET.CODE']!r}")
        try:
            year = int(r["PERIOD"])
        except ValueError as e:
            raise FishstatError(f"row {i + 2}: PERIOD {r['PERIOD']!r}") from e
        sp = rel.species.get(r["SPECIES.ALPHA_3_CODE"])
        if sp is None:
            rep["unknown_species"][r["SPECIES.ALPHA_3_CODE"]] += 1
            yb, mg = "unassigned", "unassigned"
        else:
            yb = slug(sp.yearbook_group) if sp.yearbook_group else "unassigned"
            mg = slug(sp.major_group) if sp.major_group else "unassigned"
            if not sp.yearbook_group:
                rep["unassigned_yearbook"].add(sp.code)
        status = r["STATUS"].strip()
        value: Decimal | None = None
        if status not in MISSING_SYMBOLS:
            try:
                value = Decimal(r["VALUE"])
            except InvalidOperation as e:
                raise FishstatError(f"row {i + 2}: VALUE {r['VALUE']!r} is not a number") from e
        for indicator in (f"{src}.yb_{yb}.{measure[0]}", f"{src}.mg_{mg}.{measure[0]}"):
            c = cells[(geo, indicator, year)]
            c.species.add(r["SPECIES.ALPHA_3_CODE"])
            if value is None:
                c.missing[status] += 1
            else:
                c.total += value
                c.n_values += 1
            if status in NOTABLE_SYMBOLS:
                c.symbols[status] += 1
    rep["unknown_species"] = dict(rep["unknown_species"])
    rep["unassigned_yearbook"] = sorted(rep["unassigned_yearbook"])
    rep["unknown_country_codes"] = sorted(rep["unknown_country_codes"])
    return cells, rep


def _decimal_text(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def atlas_rows(cells: Mapping[tuple[str, str, int], Cell], *, raw_sha256: str) -> list[dict[str, Any]]:
    rows = []
    for (geo, indicator, year), c in sorted(cells.items()):
        unit = MEASURE_SLUG["Q_tlw"][1] if indicator.endswith(".tonnes") else MEASURE_SLUG["Q_no_1"][1]
        flags: dict[str, Any] = {
            "aggregation": "sum over FAO fishing areas and ASFIS species of the group",
            "release": RELEASE,
            "cells_summed": c.n_values,
            "species_codes": len(c.species),
        }
        if c.symbols:
            flags["fao_symbols"] = dict(sorted(c.symbols.items()))
        value: str | None = _decimal_text(c.total)
        if c.missing:
            flags["missing_cells"] = dict(sorted(c.missing.items()))
            if c.n_values == 0:
                value = None
                reasons = {MISSING_SYMBOLS[s] for s in c.missing}
                flags["missing"] = reasons.pop() if len(reasons) == 1 else "provider_missing_code"
            else:
                flags["lower_bound"] = True
        rows.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": DATASET,
                "indicator": indicator,
                "concept_id": f"ecology.fishstat.{indicator}",
                "iso3": None,
                "provider_geo": geo,
                "entity_qid": None,
                "period_start": date(year, 1, 1),
                "period_end": date(year + 1, 1, 1),
                "value": value,
                "value_text": None,
                "unit": unit,
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon(
                    {"member": "Global_production_quantity.csv", "group_by": indicator, "year": year, "country": geo}
                ),  # fmt: skip
            }
        )
    return rows


def atlas_frame(rows: list[dict[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(rows, schema=ATLAS_SCHEMA)


def atlas_source() -> Any:
    from reva.adapters.connectors.base import module_digest
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="FAO Fisheries and Aquaculture Division, FishStat (Global production by production source)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']})",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(f"GlobalProduction_{RELEASE}",),
        cadence_seconds=30 * 86400,
        planned_requests=2,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(atlas_source),
        field_mappings={"atlas_tables": [f"{ATLAS_SOURCE}.{DATASET}"], "geo": "ISO3_Code of CL_FI_COUNTRY_GROUPS"},
        notes="Country x production source x Yearbook/Major group sums over areas and species; measures kept apart.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": DATASET}
    raw = await run.get(URL, key=f"GlobalProduction_{RELEASE}", max_bytes=MAX_BYTES,
                        media_types=("application/zip", "application/x-zip-compressed", "application/octet-stream"))  # fmt: skip
    rel = parse_release(raw.body)
    if run.origin != "test_fixture" and len(rel.rows) < 1_000_000:
        raise FishstatError(f"only {len(rel.rows)} quantity rows: truncated release?", kind="incomplete")
    sha = run.archive(raw)
    cells, rep = aggregate(rel)
    rows = atlas_rows(cells, raw_sha256=sha)
    reg = run.register(
        dataset=DATASET,
        frame=atlas_frame(rows),
        raw_sha256s=[sha],
        geo_scheme="iso3",
        rights=dict(RIGHTS),
        coverage_note=(
            f"FAO FishStat Global Production release {RELEASE}: {len(rel.rows)} country x species x area x source x "
            "year cells summed by REVA per country, production source and Yearbook / Major species group (FAO "
            "code lists); tonnes live weight and numbers kept apart; missing-symbol cells make a sum a lower bound"
        ),
    )
    return {
        "release": RELEASE,
        "quantity_rows": len(rel.rows),
        "report": {k: (sorted(v)[:50] if isinstance(v, set) else v) for k, v in rep.items()},
        "atlas": {k: reg[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")},
    }


__all__ = ["CONNECTOR_NAME", "aggregate", "atlas_ingest", "atlas_rows", "atlas_source", "parse_release", "slug"]
