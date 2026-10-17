"""WRI Global Power Plant Database v1.3.0 -> KG power plants (facilities) (V4, agent V4ENERGY).

~35,000 power plants in 167 countries: name, GPPD id, country (ISO3), coordinates, capacity (MW), primary and
other fuels, capacity-weighted commissioning year, owner (free text), upstream data source, Platts WEPP id when
cited, and reported annual generation 2013-2019 (GWh) where an official source gave it.

Source
------
GitHub repository ``wri/global-power-plant-database`` (branch ``master``), directory ``output_database``:
``global_power_plant_database.csv`` (~12 MB) and ``DATABASE_VERSION`` (``1.3.0`` = the last release; the
project is no longer maintained: "There are no planned updates as of this time (early 2022)"), plus the
repository ``README.md`` whose licence sentence is checked on every run (fails closed if it no longer says
"Creative Commons-Attribution 4.0 (CC BY 4.0)"). All three files are archived content-addressed. Cite:
Global Energy Observatory, Google, KTH Royal Institute of Technology in Stockholm, Enipedia, World Resources
Institute. 2021. Global Power Plant Database v1.3.0. World Resources Institute (CC BY 4.0).

KG (``source_key`` ``wri.gppd``; predicates ``registry/kg/predicates_energy.yaml``; families
``registry/kg/relations_v4.d/v4energy.yaml``)
---------------------------------------------------------------------------------------------------------
* entity ``gppd:<gppd_idnr>`` (kind ``facility``; label = the plant name verbatim, en; description "power plant
  (GPPD <primary_fuel>)");
* entity ``gppd:fuel:<code>`` (kind ``energy_source``) per GPPD fuel category, label verbatim;
* statements: ``gppd:country`` -> the country QID (``registry/entities/country_qids.json``) else
  ``iso3:<ISO3>``; ``gppd:primary_fuel`` / ``gppd:other_fuel`` -> fuel category; ``gppd:capacity_mw``
  (quantity, MW; ``point_in_time`` = ``year_of_capacity_data`` when given); ``gppd:commissioning_year``
  (decimal year verbatim); ``gppd:coordinates`` (WGS84 as given, ``geolocation_source`` qualifier);
  ``gppd:owner_name`` (string — no company entity, GPPD publishes no owner id); ``gppd:generation_gwh``
  (one per reported year, ``point_in_time`` = year, qualifier ``generation_data_source``); ``gppd:wepp_id``;
  ``gppd:data_source`` (with the record ``url`` as qualifier).
* Not loaded: ``estimated_generation_gwh_2013..2017`` (WRI model output, with its method note — modelled
  estimates are not reports); they stay in the archived CSV.

Statement ids are stable (``plant:<id>:<predicate>[:<year>|:<n>]``): an unchanged fact is not re-stored; a fact
a new delivery drops is retracted (``plant:`` / ``fuel:`` prefixes). Nothing is matched by name.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.wri_gppd"
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas_gppd/1"
SOURCE_KEY = "wri.gppd"
SOURCE = "wri_gppd"
DATASET = "power_plants_kg"
HOST = "raw.githubusercontent.com"
REPO = "https://raw.githubusercontent.com/wri/global-power-plant-database/master"
CSV_URL = f"{REPO}/output_database/global_power_plant_database.csv"
VERSION_URL = f"{REPO}/output_database/DATABASE_VERSION"
README_URL = f"{REPO}/README.md"
LICENCE = "CC-BY-4.0"
GEO_SCHEME = "gppd"
LICENCE_SENTENCE = "Creative Commons-Attribution 4.0 (CC BY 4.0)"
MAX_CSV_BYTES = 40_000_000
REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_energy.yaml"
EXPECTED_COLUMNS = (
    "country",
    "country_long",
    "name",
    "gppd_idnr",
    "capacity_mw",
    "latitude",
    "longitude",
    "primary_fuel",
    "other_fuel1",
    "other_fuel2",
    "other_fuel3",
    "commissioning_year",
    "owner",
    "source",
    "url",
    "geolocation_source",
    "wepp_id",
    "year_of_capacity_data",
)
GEN_YEARS = tuple(range(2013, 2020))
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
DEC_RE = re.compile(r"^[+-]?[0-9]{1,40}(\.[0-9]{1,40})?$")
YEAR_RE = re.compile(r"^[0-9]{4}$")
MIN_PLANTS_LIVE = 30_000
REFERENCE = {"stated_in": "Global Power Plant Database v1.3.0 (WRI)", "reference_url": CSV_URL}


class GppdParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="schema_drift")


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="World Resources Institute (WRI) et al., Global Power Plant Database",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("global_power_plant_database",),
        cadence_seconds=90 * 86_400,
        planned_requests=3,
        max_response_bytes=MAX_CSV_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"kg": "gppd:* predicates (registry/kg/predicates_energy.yaml)"},
        notes="README (licence check) + DATABASE_VERSION + output CSV; KG facilities only, no atlas table",
    )


# ============================================================================================== parsing
@dataclass(frozen=True)
class Plant:
    idnr: str
    name: str
    country: str
    capacity_mw: str
    lat: str
    lon: str
    primary_fuel: str
    other_fuels: tuple[str, ...]
    commissioning_year: str
    owner: str
    source: str
    url: str
    geolocation_source: str
    wepp_id: str
    year_of_capacity_data: str
    generation: tuple[tuple[int, str], ...]
    generation_data_source: str
    row: int


@dataclass
class ParseReport:
    rows: int = 0
    plants: int = 0
    countries: int = 0
    fuels: dict[str, int] = field(default_factory=dict)
    with_owner: int = 0
    with_wepp: int = 0
    generation_values: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def check_licence(readme: bytes) -> None:
    if LICENCE_SENTENCE not in readme.decode("utf-8", errors="replace"):
        raise GppdParseError(f"README no longer states '{LICENCE_SENTENCE}': licence unverified")


def parse_version(body: bytes) -> str:
    v = body.decode("utf-8", errors="replace").strip()
    if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", v):
        raise GppdParseError(f"DATABASE_VERSION {v[:40]!r} is not a version")
    return v


def parse_csv(body: bytes, *, min_plants: int = 1) -> tuple[list[Plant], ParseReport]:
    rep = ParseReport()
    text = body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    cols = tuple(reader.fieldnames or ())
    missing = [c for c in EXPECTED_COLUMNS if c not in cols]
    if missing:
        raise GppdParseError(f"GPPD CSV: missing columns {missing}")
    gen_cols = [f"generation_gwh_{y}" for y in GEN_YEARS]
    plants: list[Plant] = []
    seen: set[str] = set()
    skipped: Counter[str] = Counter()
    fuels: Counter[str] = Counter()
    countries: set[str] = set()
    for i, r in enumerate(reader, start=2):
        rep.rows += 1
        idnr = (r.get("gppd_idnr") or "").strip()
        if not ID_RE.match(idnr):
            skipped["bad_gppd_idnr"] += 1
            continue
        if idnr in seen:
            raise GppdParseError(f"GPPD CSV: duplicate gppd_idnr {idnr}")
        seen.add(idnr)
        gen: list[tuple[int, str]] = []
        for y, c in zip(GEN_YEARS, gen_cols, strict=True):
            v = (r.get(c) or "").strip()
            if v:
                if not DEC_RE.match(v):
                    skipped[f"generation_not_decimal:{c}"] += 1
                    continue
                gen.append((y, v))
        p = Plant(
            idnr=idnr,
            name=(r.get("name") or "").strip(),
            country=(r.get("country") or "").strip(),
            capacity_mw=(r.get("capacity_mw") or "").strip(),
            lat=(r.get("latitude") or "").strip(),
            lon=(r.get("longitude") or "").strip(),
            primary_fuel=(r.get("primary_fuel") or "").strip(),
            other_fuels=tuple(f for f in ((r.get(f"other_fuel{k}") or "").strip() for k in (1, 2, 3)) if f),
            commissioning_year=(r.get("commissioning_year") or "").strip(),
            owner=(r.get("owner") or "").strip(),
            source=(r.get("source") or "").strip(),
            url=(r.get("url") or "").strip(),
            geolocation_source=(r.get("geolocation_source") or "").strip(),
            wepp_id=(r.get("wepp_id") or "").strip(),
            year_of_capacity_data=(r.get("year_of_capacity_data") or "").strip(),
            generation=tuple(gen),
            generation_data_source=(r.get("generation_data_source") or "").strip(),
            row=i,
        )
        plants.append(p)
        countries.add(p.country)
        for f in (p.primary_fuel, *p.other_fuels):
            if f:
                fuels[f] += 1
        rep.with_owner += bool(p.owner)
        rep.with_wepp += bool(p.wepp_id)
        rep.generation_values += len(gen)
    rep.plants = len(plants)
    rep.countries = len(countries)
    rep.fuels = dict(fuels.most_common())
    rep.skipped = dict(skipped)
    if rep.plants < min_plants:
        raise GppdParseError(f"GPPD CSV: {rep.plants} plants < {min_plants} expected")
    return plants, rep


# ============================================================================================== KG
def fuel_code(category: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")


def fuel_qid(category: str) -> str:
    return f"gppd:fuel:{fuel_code(category)}"


def load_predicates(path: Path = PREDICATES_FILE) -> list[Any]:
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = (
        "pid",
        "value_kind",
        "reva_predicate",
        "label_en",
        "label_fr",
        "inverse_name",
        "inverse_label_en",
        "inverse_label_fr",
    )
    return [
        KgProperty(**{k: p[k] for k in keys if k in p}) for p in doc["predicates"] if str(p["pid"]).startswith("gppd:")
    ]


def _year(y: str) -> Any:
    from reva.kg.model import KgTime

    return KgTime(f"{int(y):04d}-01-01T00:00:00Z", 9)


def _kg_dec(v: str) -> str | None:
    """A GPPD decimal as a KG quantity string (no exponent), or None if it is not one."""
    return v if DEC_RE.match(v) else None


@dataclass
class KgParts:
    entities: list[Any]
    statements: list[Any]
    counters: dict[str, int]


def build_kg(plants: Sequence[Plant], *, raw_sha256: str, version: str, country_qids: Mapping[str, str]) -> KgParts:
    from reva.kg.model import KgEntity, KgStatement

    rev = f"gppd-{version}"
    ents: list[Any] = []
    sts: list[Any] = []
    c: Counter[str] = Counter()
    cats = sorted({f for p in plants for f in (p.primary_fuel, *p.other_fuels) if f})
    for cat in cats:
        ents.append(
            KgEntity(
                qid=fuel_qid(cat),
                kind="energy_source",
                labels={"en": cat},
                descriptions={"en": "fuel category of the Global Power Plant Database (primary_fuel / other_fuel)"},
                source_revision=rev,
            )
        )

    def st(sid: str, subj: str, pid: str, *, obj: str | None = None, value: Any = None, **kw: Any) -> None:
        if "qualifiers" in kw:
            kw["qualifiers"] = {k: v for k, v in kw["qualifiers"].items() if v is not None}
        sts.append(
            KgStatement(
                subject_qid=subj,
                property_pid=pid,
                object_qid=obj,
                value=value,
                statement_id=sid,
                source_revision=rev,
                raw_sha256=raw_sha256,
                references=[REFERENCE],
                **kw,
            )
        )
        c[pid] += 1

    for p in plants:
        q = f"gppd:{p.idnr}"
        sel = {"file": "global_power_plant_database.csv", "row": p.row}
        ents.append(
            KgEntity(
                qid=q,
                kind="facility",
                labels={"en": p.name} if p.name else {},
                descriptions={"en": f"power plant (GPPD primary fuel: {p.primary_fuel or 'not stated'})"},
                source_revision=rev,
            )
        )
        if not p.name:
            c["plant_without_name"] += 1
        base = f"plant:{p.idnr}"
        iso = country_iso3(p.country, GEO_SCHEME) if p.country else None
        if iso:
            cq = country_qids.get(iso)
            obj = cq or f"iso3:{iso}"
            c["country_qid" if cq else "country_iso3_node"] += 1
            st(f"{base}:country", q, "gppd:country", obj=obj, qualifiers={"selector": sel, "gppd_country": p.country})
        else:
            c["country_unmapped"] += 1
        if p.primary_fuel:
            st(
                f"{base}:primary_fuel",
                q,
                "gppd:primary_fuel",
                obj=fuel_qid(p.primary_fuel),
                qualifiers={"selector": sel},
            )
        for k, f in enumerate(p.other_fuels, start=1):
            st(
                f"{base}:other_fuel:{k}",
                q,
                "gppd:other_fuel",
                obj=fuel_qid(f),
                qualifiers={"selector": sel, "column": f"other_fuel{k}"},
            )
        cap = _kg_dec(p.capacity_mw)
        if cap is not None:
            kw: dict[str, Any] = {}
            if YEAR_RE.match(p.year_of_capacity_data):
                kw["point_in_time"] = _year(p.year_of_capacity_data)
            st(
                f"{base}:capacity_mw",
                q,
                "gppd:capacity_mw",
                value={"type": "quantity", "amount": cap, "unit": "MW"},
                qualifiers={"selector": sel},
                **kw,
            )
        elif p.capacity_mw:
            c["capacity_not_decimal"] += 1
        cy = _kg_dec(p.commissioning_year)
        if cy is not None:
            st(
                f"{base}:commissioning_year",
                q,
                "gppd:commissioning_year",
                value={"type": "quantity", "amount": cy, "unit": "year (capacity-weighted, GPPD)"},
                qualifiers={"selector": sel},
            )
        if _kg_dec(p.lat) is not None and _kg_dec(p.lon) is not None:
            st(
                f"{base}:coordinates",
                q,
                "gppd:coordinates",
                value={"type": "globe_coordinate", "lat": p.lat, "lon": p.lon, "globe": "Q2"},
                qualifiers={"selector": sel, "geolocation_source": p.geolocation_source or None},
            )
        else:
            c["coordinates_missing"] += 1
        if p.owner:
            st(
                f"{base}:owner_name",
                q,
                "gppd:owner_name",
                value={"type": "string", "value": p.owner},
                qualifiers={"selector": sel},
            )
        if p.wepp_id:
            st(
                f"{base}:wepp_id",
                q,
                "gppd:wepp_id",
                value={"type": "external_id", "value": p.wepp_id},
                qualifiers={"selector": sel},
            )
        if p.source:
            st(
                f"{base}:data_source",
                q,
                "gppd:data_source",
                value={"type": "string", "value": p.source},
                qualifiers={"selector": sel, "url": p.url or None},
            )
        for y, v in p.generation:
            st(
                f"{base}:generation_gwh:{y}",
                q,
                "gppd:generation_gwh",
                value={"type": "quantity", "amount": v, "unit": "GWh"},
                point_in_time=_year(str(y)),
                qualifiers={
                    "selector": {**sel, "column": f"generation_gwh_{y}"},
                    "generation_data_source": p.generation_data_source or None,
                },
            )
    return KgParts(ents, sts, dict(c))


def current_keys(engine: Any, origin: str) -> set[str]:
    from sqlalchemy import text

    with engine.connect() as cx:
        return set(
            cx.execute(
                text("SELECT statement_key FROM kg.statement_current WHERE source_key = :s AND origin = :o"),
                {"s": SOURCE_KEY, "o": origin},
            ).scalars()
        )


def load_kg(
    engine: Any, store: Any, parts: KgParts, *, raw_sha256s: Sequence[str], retrieved_at: datetime, origin: str
) -> dict[str, Any]:
    from sqlalchemy.exc import DBAPIError

    from reva.kernel.errors import RevaError
    from reva.kg.model import (
        MAX_ENTITIES_PER_BATCH,
        MAX_RETRACTIONS_PER_BATCH,
        MAX_STATEMENTS_PER_BATCH,
        load_batch,
        register_properties,
        statement_key,
    )

    try:
        register_properties(engine, load_predicates())
        new = {statement_key(s, SOURCE_KEY) for s in parts.statements}
        retract = sorted(k for k in current_keys(engine, origin) - new if k.startswith(f"{SOURCE_KEY}:plant:"))
        n = max(
            1,
            -(-len(parts.statements) // MAX_STATEMENTS_PER_BATCH),
            -(-len(parts.entities) // MAX_ENTITIES_PER_BATCH),
            -(-len(retract) // MAX_RETRACTIONS_PER_BATCH),
        )
        reports = []
        for i in range(n):
            rep = load_batch(
                engine,
                store,
                entities=parts.entities[i * MAX_ENTITIES_PER_BATCH : (i + 1) * MAX_ENTITIES_PER_BATCH],
                statements=parts.statements[i * MAX_STATEMENTS_PER_BATCH : (i + 1) * MAX_STATEMENTS_PER_BATCH],
                source_key=SOURCE_KEY,
                raw_sha256s=sorted(set(raw_sha256s)),
                retrieved_at=retrieved_at,
                origin=origin,
                retract_keys=retract[i * MAX_RETRACTIONS_PER_BATCH : (i + 1) * MAX_RETRACTIONS_PER_BATCH],
                parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION},
            )
            reports.append(rep.as_dict())
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": getattr(e, "details", None)}
    except DBAPIError as e:
        return {"status": "failed", "error": f"{type(e.orig).__name__}: {str(e.orig)[:300]}"}
    tot: Counter[str] = Counter()
    for r in reports:
        for k, v in r.items():
            if isinstance(v, int):
                tot[k] += v
    return {"status": "loaded", "batches": len(reports), "totals": dict(tot), "retracted_candidates": len(retract)}


# ============================================================================================== ingest
async def atlas_ingest(run: Any, *, load: bool = True) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    if not run.wants(DATASET):
        return {"skipped": DATASET}
    raw_readme = await run.get(README_URL, key="readme", media_types=("text/plain",), max_bytes=1_000_000)
    readme_sha = run.archive(raw_readme)
    check_licence(raw_readme.body)
    raw_ver = await run.get(VERSION_URL, key="database_version", media_types=("text/plain",), max_bytes=10_000)
    ver_sha = run.archive(raw_ver)
    version = parse_version(raw_ver.body)
    raw = await run.get(CSV_URL, key=f"csv:{version}", media_types=("text/plain", "text/csv"), max_bytes=MAX_CSV_BYTES)
    sha = run.archive(raw)
    plants, rep = parse_csv(raw.body, min_plants=MIN_PLANTS_LIVE if run.origin == "live" else 1)
    retrieved = raw.retrieved_at
    del raw
    parts = build_kg(plants, raw_sha256=sha, version=version, country_qids=load_country_qids() or {})
    out: dict[str, Any] = {
        "version": version,
        "parse": rep.as_dict(),
        "kg_built": {"entities": len(parts.entities), "statements": len(parts.statements), **parts.counters},
    }
    if load:
        out["kg"] = load_kg(
            run.engine,
            run.store,
            parts,
            raw_sha256s=[sha, readme_sha, ver_sha],
            retrieved_at=retrieved,
            origin=run.origin,
        )
    return out


__all__ = [
    "CONNECTOR_NAME",
    "GppdParseError",
    "atlas_ingest",
    "atlas_source",
    "build_kg",
    "check_licence",
    "fuel_qid",
    "parse_csv",
    "parse_version",
]
