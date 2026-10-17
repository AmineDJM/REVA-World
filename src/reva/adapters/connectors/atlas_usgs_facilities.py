"""USGS Minerals Yearbook 2024, vol. III — facilities table -> KG mines and plants (V4, agent V4ENERGY).

Who produces which mineral commodity where: ~12,000 mines, quarries, brine operations and processing plants (smelters,
refineries, cement plants…) in ~170 countries, ~14,750 facility × commodity entries, with the commodity (USGS
5-level classification), annual production capacity (with USGS's own estimated / shared-capacity codes), status
(assumed active / inactive, for 2024), first-level administrative division *name*, location text, the major operating
company and up to eight equity owners **with their percent shares** — all as USGS publishes them.

Source: the ScienceBase data release "2024 Minerals Yearbook, volume III, Area Reports — International"
(item ``6a8490171ba49b3133cd8055``, doi:10.5066/P1KEQASH, USGS National Minerals Information Center), file
``2024MYBv3_Facilities_table.csv`` (~7.7 MB) and its FGDC metadata ``2024_MYBv3_metadata.xml`` — the same item
whose production table A-RESOURCES loads as ``atlas.usgs.myb2024`` (:mod:`atlas_usgs`; its item/licence helpers are
reused). The run reads the item JSON, takes the two files' URLs from the item's own file list, archives all three,
and fails closed unless the item or the metadata says CC0 / public domain.

KG (``source_key`` ``usgs.sciencebase``; predicates ``usgs_myb:*`` in ``registry/kg/predicates_energy.yaml``)
-----------------------------------------------------------------------------------------------------------------
* entity ``usgs_myb:<USGS Facility ID>`` (kind ``facility``; label = Facility Name; other names of the same id as
  aliases). The id is USGS's ("GENC country code combined with an internally assigned number").
* entity ``usgs_myb:commodity:<code>`` (kind ``commodity``) per "Level 2 (Commodity)" value (code = the value
  lower-cased, non-alphanumerics -> ``_``); label verbatim; not mapped to any other vocabulary (no identifier).
* facility statements: ``usgs_myb:country`` (country QID of the GENC trigraph that prefixes the USGS Facility ID,
  else ``iso3:<ISO3>``; the country-name column is kept verbatim as ``usgs_country``), ``usgs_myb:feature_type``, ``usgs_myb:adm1_name`` (a *name* as listed in the NGA GNS, with
  the division type — never linked to a region entity: no code is published), ``usgs_myb:location``,
  ``usgs_myb:operator_name``.
* entry statements (``Global Entry`` key in the statement id): ``usgs_myb:produces`` -> commodity (qualifiers: the
  five classification levels, descriptor, commodity unit, status, footnote); ``usgs_myb:annual_capacity``
  (quantity + Capacity Unit, ``point_in_time`` = publication year, ``capacity_code`` with its metadata meaning,
  ``shared_capacity`` = the Global Entry keys sharing it — never summed); ``usgs_myb:equity_owner_name`` per owner
  with ``share_percent`` when given; ``usgs_myb:data_source`` (URL or text as given).
* ``-999`` ("Value is not known or not available", per the metadata) never becomes a value: the statement or the
  share is simply absent (counted). Company names are strings: USGS publishes no company identifier, and REVA never
  matches companies by name.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from reva.adapters.connectors.atlas_usgs import Release, decode_csv, licence_ok, parse_item, parse_metadata
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.usgs_myb_facilities"
# 1.0.2 (FIX4C, AUDIT3 section 5): usgs_myb:country from the Facility ID's GENC trigraph, not from the country name.
CONNECTOR_VERSION = "1.0.2"
FACILITY_ID_RE = re.compile(r"^([A-Z]{3})[0-9]+$")
PARSER_VERSION = "atlas_usgs_facilities/1"
SOURCE_KEY = "usgs.sciencebase"
SOURCE = "usgs_myb_fac"
DATASET = "facilities_kg"
HOST = "www.sciencebase.gov"
GEO_SCHEME = "usgs_name"
LICENCE = "U.S. public domain; ScienceBase data releases marked CC0 1.0 (USGS National Minerals Information Center)"
RELEASE = Release(
    "myb2024_facilities",
    "6a8490171ba49b3133cd8055",
    "2024MYBv3_Facilities_table.csv",
    "2024_MYBv3_metadata.xml",
    "facilities",
    "2024 Minerals Yearbook, volume III, Area Reports — International: facilities table",
    "https://doi.org/10.5066/P1KEQASH",
    max_bytes=30_000_000,
)
CSV_MEDIA = ("text/csv", "application/octet-stream", "text/plain")
XML_MEDIA = ("application/fgdc+xml", "application/xml", "text/xml")
REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_energy.yaml"
NOT_KNOWN = "-999"
DEC_RE = re.compile(r"^[+-]?[0-9]{1,40}(\.[0-9]{1,40})?$")
ID_RE = re.compile(r"^[A-Z]{3}[0-9]{4}$")
ENTRY_RE = re.compile(r"^FAC_[A-Z]{3}[0-9]{4}_[0-9]{2}$")
MIN_ENTRIES_LIVE = 10_000
REQUIRED = (
    "Global Entry",
    "Publication Year",
    "Currentness Date",
    "USGS Facility ID",
    "Country (Short Form) or Locality",
    "Facility Name",
    "Feature Type",
    "Level 1 (Commodity Group)",
    "Level 2 (Commodity)",
    "Level 3 (Type)",
    "Level 4 (Phase)",
    "Level 5 (Form)",
    "Descriptor",
    "Commodity Unit",
    "Commodity Footnote",
    "Annual Production Capacity",
    "Capacity Unit",
    "Capacity Code",
    "Shared Capacity",
    "Capacity Notes",
    "Facility Status",
    "Location Description",
    "First-Level Administrative Division Type",
    "First-Level Administrative Division Name",
    "Major Operating Company",
    "Data Source 1",
    "Data Source 2",
)
#: Capacity Code meanings are read from the archived FGDC metadata (attribute "Capacity Code"); these are the codes
#: the connector expects to find there.
CAPACITY_CODES = ("E", "C", "C,E")


class UsgsFacilitiesParseError(SourceResponseError):
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
        provider="U.S. Geological Survey, National Minerals Information Center (Minerals Yearbook 2024 facilities)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(RELEASE.doi,),
        cadence_seconds=90 * 86_400,
        planned_requests=3,
        max_response_bytes=RELEASE.max_bytes,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"kg": "usgs_myb:* predicates (registry/kg/predicates_energy.yaml)"},
        notes="ScienceBase item JSON + FGDC metadata + facilities CSV; KG facilities only, no atlas table",
    )


# ============================================================================================== metadata
def capacity_code_meanings(meta_xml: bytes) -> dict[str, str]:
    """The ``Capacity Code`` value meanings of the archived FGDC metadata (verbatim)."""
    text = meta_xml.decode("utf-8", errors="replace")
    m = re.search(r"<attrlabl>Capacity Code</attrlabl>(.*?)</attr>", text, re.S)
    if not m:
        raise UsgsFacilitiesParseError("metadata: no 'Capacity Code' attribute")
    out: dict[str, str] = {}
    for e in re.finditer(r"<edomv>(.*?)</edomv>\s*<edomvd>(.*?)</edomvd>", m.group(1), re.S):
        out[re.sub(r"\s+", "", e.group(1))] = re.sub(r"\s+", " ", e.group(2)).strip()
    missing = [c for c in CAPACITY_CODES if c not in out]
    if missing:
        raise UsgsFacilitiesParseError(f"metadata: Capacity Code meanings missing for {missing}")
    return out


def not_known_is_declared(meta_xml: bytes) -> bool:
    """The metadata must still define -999 as 'not known or not available' for the capacity and share fields."""
    text = meta_xml.decode("utf-8", errors="replace")
    for lab in ("Annual Production Capacity", "Share of Equity Owner 1"):
        m = re.search(r"<attrlabl>" + re.escape(lab) + r"</attrlabl>(.*?)</attr>", text, re.S)
        if not m or not re.search(r"<edomv>\s*-999\s*</edomv>\s*<edomvd>\s*Value is not known", m.group(1)):
            return False
    return True


# ============================================================================================== parsing
@dataclass
class Entry:
    row: int
    data: dict[str, str]

    def g(self, k: str) -> str:
        return (self.data.get(k) or "").strip()


def parse_csv(body: bytes, *, min_entries: int = 1) -> tuple[list[Entry], dict[str, Any]]:
    text, _enc = decode_csv(body)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    cols = [c.lstrip("﻿") for c in (reader.fieldnames or [])]
    reader.fieldnames = cols
    miss = [c for c in REQUIRED if c not in cols]
    if miss:
        raise UsgsFacilitiesParseError(f"facilities CSV: missing columns {miss}")
    owners = sorted(int(m.group(1)) for c in cols if (m := re.match(r"^Equity Owner ([0-9])$", c)))
    entries: list[Entry] = []
    seen: set[str] = set()
    skipped: Counter[str] = Counter()
    for i, r in enumerate(reader, start=2):
        e = Entry(i, {k: v for k, v in r.items() if k is not None})
        key, fid = e.g("Global Entry"), e.g("USGS Facility ID")
        if not ENTRY_RE.match(key) or not ID_RE.match(fid):
            skipped["bad_key"] += 1
            continue
        if key in seen:
            raise UsgsFacilitiesParseError(f"facilities CSV: duplicate Global Entry {key}")
        seen.add(key)
        entries.append(e)
    if len(entries) < min_entries:
        raise UsgsFacilitiesParseError(f"facilities CSV: {len(entries)} entries < {min_entries}")
    info = {
        "entries": len(entries),
        "facilities": len({e.g("USGS Facility ID") for e in entries}),
        "equity_owner_columns": owners,
        "skipped": dict(skipped),
        "publication_years": sorted({e.g("Publication Year") for e in entries}),
    }
    return entries, info


# ============================================================================================== KG
def slug(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", v.lower()).strip("_")[:100]


def commodity_qid(level2: str) -> str:
    return f"usgs_myb:commodity:{slug(level2)}"


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
        KgProperty(**{k: p[k] for k in keys if k in p})
        for p in doc["predicates"]
        if str(p["pid"]).startswith("usgs_myb:")
    ]


@dataclass
class KgParts:
    entities: list[Any]
    statements: list[Any]
    counters: dict[str, int]


def build_kg(
    entries: Sequence[Entry], *, raw_sha256: str, capacity_codes: Mapping[str, str], country_qids: Mapping[str, str]
) -> KgParts:
    from reva.kg.model import KgEntity, KgStatement, KgTime

    rev = "myb2024v3"
    ref = {"stated_in": RELEASE.title, "doi": RELEASE.doi}
    ents: list[Any] = []
    sts: list[Any] = []
    c: Counter[str] = Counter()

    def st(sid: str, subj: str, pid: str, *, obj: str | None = None, value: Any = None, **kw: Any) -> None:
        if "qualifiers" in kw:
            kw["qualifiers"] = {k: v for k, v in kw["qualifiers"].items() if v not in (None, "")}
        sts.append(
            KgStatement(
                subject_qid=subj,
                property_pid=pid,
                object_qid=obj,
                value=value,
                statement_id=sid,
                source_revision=rev,
                raw_sha256=raw_sha256,
                references=[ref],
                **kw,
            )
        )
        c[pid] += 1

    def s(v: str) -> dict[str, str]:
        return {"type": "string", "value": v}

    commodities: dict[str, tuple[str, str]] = {}
    by_fac: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        by_fac[e.g("USGS Facility ID")].append(e)
        l2 = e.g("Level 2 (Commodity)")
        if l2:
            commodities.setdefault(commodity_qid(l2), (l2, e.g("Level 1 (Commodity Group)")))
    for q, (l2, l1) in sorted(commodities.items()):
        ents.append(
            KgEntity(
                qid=q,
                kind="commodity",
                labels={"en": l2},
                descriptions={"en": f"mineral commodity (USGS Minerals Yearbook classification; group: {l1})"},
                source_revision=rev,
            )
        )
    for fid, es in sorted(by_fac.items()):
        q = f"usgs_myb:{fid}"
        first = es[0]
        names = list(dict.fromkeys(e.g("Facility Name") for e in es if e.g("Facility Name")))
        ftypes = sorted({e.g("Feature Type") for e in es if e.g("Feature Type")})
        ents.append(
            KgEntity(
                qid=q,
                kind="facility",
                labels={"en": names[0]} if names else {},
                aliases={"en": names[1:]} if len(names) > 1 else {},
                descriptions={"en": f"{' / '.join(ftypes) or 'facility'} (USGS Minerals Yearbook 2024 facilities)"},
                source_revision=rev,
            )
        )
        sel = {"file": RELEASE.data_file, "row": first.row}
        base = f"fac:{fid}"
        country = first.g("Country (Short Form) or Locality")
        # AUDIT3 section 5: the country is the trigraph USGS puts in its own Facility ID ("GENC country code combined
        # with an internally assigned number"), an identifier -- not the country *name* column. The name-based
        # answer is only a cross-check (counted when it differs, never used).
        m = FACILITY_ID_RE.match(fid)
        iso = country_iso3(m.group(1), "iso3") if m else None
        if iso:
            cq = country_qids.get(iso)
            c["country_qid" if cq else "country_iso3_node"] += 1
            by_name = country_iso3(country, GEO_SCHEME) if country else None
            if by_name != iso:
                c["country_name_crosscheck_differs"] += 1
            st(
                f"{base}:country_by_id",
                q,
                "usgs_myb:country",
                obj=cq or f"iso3:{iso}",
                qualifiers={
                    "selector": sel,
                    "usgs_country": country,
                    "genc_trigraph": m.group(1) if m else None,
                    "basis": "USGS Facility ID prefix (GENC trigraph) -> ISO 3166-1 alpha-3",
                },
            )
        else:
            c["country_unmapped_id_prefix"] += 1
        for k, ft in enumerate(ftypes):
            st(f"{base}:feature_type:{k}", q, "usgs_myb:feature_type", value=s(ft), qualifiers={"selector": sel})
        adm = first.g("First-Level Administrative Division Name")
        if adm and adm != "NA":
            st(
                f"{base}:adm1_name",
                q,
                "usgs_myb:adm1_name",
                value=s(adm),
                qualifiers={
                    "selector": sel,
                    "division_type": first.g("First-Level Administrative Division Type"),
                    "gazetteer": "NGA Geographic Names Server (as stated in the metadata)",
                },
            )
        loc = first.g("Location Description")
        if loc:
            st(f"{base}:location", q, "usgs_myb:location", value=s(loc), qualifiers={"selector": sel})
        op = first.g("Major Operating Company")
        if op:
            st(f"{base}:operator_name", q, "usgs_myb:operator_name", value=s(op), qualifiers={"selector": sel})
        for e in es:
            key = e.g("Global Entry")
            esel = {"file": RELEASE.data_file, "row": e.row, "global_entry": key}
            year = e.g("Publication Year")
            l2 = e.g("Level 2 (Commodity)")
            if l2:
                st(
                    f"entry:{key}:produces",
                    q,
                    "usgs_myb:produces",
                    obj=commodity_qid(l2),
                    qualifiers={
                        "selector": esel,
                        "level1_group": e.g("Level 1 (Commodity Group)"),
                        "level3_type": e.g("Level 3 (Type)"),
                        "level4_phase": e.g("Level 4 (Phase)"),
                        "level5_form": e.g("Level 5 (Form)"),
                        "descriptor": e.g("Descriptor"),
                        "commodity_unit": e.g("Commodity Unit"),
                        "facility_status": e.g("Facility Status"),
                        "commodity_footnote": e.g("Commodity Footnote"),
                        "currentness_date": e.g("Currentness Date"),
                    },
                    point_in_time=KgTime(f"{int(year):04d}-01-01T00:00:00Z", 9) if year.isdigit() else None,
                )
            cap = e.g("Annual Production Capacity")
            if cap == NOT_KNOWN or not cap:
                c["capacity_not_known"] += 1
            elif DEC_RE.match(cap):
                code = e.g("Capacity Code").replace(" ", "")
                shared = [k for k in e.g("Shared Capacity").split(",") if k and k != key]
                st(
                    f"entry:{key}:annual_capacity",
                    q,
                    "usgs_myb:annual_capacity",
                    value={"type": "quantity", "amount": cap, "unit": e.g("Capacity Unit") or None},
                    point_in_time=KgTime(f"{int(year):04d}-01-01T00:00:00Z", 9) if year.isdigit() else None,
                    qualifiers={
                        "selector": esel,
                        "commodity": commodity_qid(l2) if l2 else None,
                        "capacity_code": code or None,
                        "capacity_code_meaning": capacity_codes.get(code) if code else None,
                        "shared_capacity_with": shared or None,
                        "capacity_notes": e.g("Capacity Notes"),
                    },
                )
            else:
                c["capacity_not_decimal"] += 1
            for k in range(1, 9):
                owner = e.g(f"Equity Owner {k}")
                if not owner:
                    continue
                share = e.g(f"Share of Equity Owner {k}")
                qual: dict[str, Any] = {"selector": {**esel, "column": f"Equity Owner {k}"}, "owner_rank": k}
                if share and share != NOT_KNOWN and DEC_RE.match(share):
                    qual["share_percent"] = [{"amount": share, "unit": "%"}]
                else:
                    c["owner_share_not_known"] += 1
                st(f"entry:{key}:equity_owner:{k}", q, "usgs_myb:equity_owner_name", value=s(owner), qualifiers=qual)
            for k in (1, 2):
                src = e.g(f"Data Source {k}")
                if src:
                    st(
                        f"entry:{key}:data_source:{k}",
                        q,
                        "usgs_myb:data_source",
                        value=s(src),
                        qualifiers={"selector": esel},
                    )
    return KgParts(ents, sts, dict(c))


def current_keys(engine: Any, origin: str) -> set[str]:
    from sqlalchemy import text

    like = f"{SOURCE_KEY}:fac:%"
    like2 = f"{SOURCE_KEY}:entry:FAC_%"
    with engine.connect() as cx:
        return set(
            cx.execute(
                text("""SELECT statement_key FROM kg.statement_current
                        WHERE source_key = :s AND origin = :o AND (statement_key LIKE :a OR statement_key LIKE :b)"""),
                {"s": SOURCE_KEY, "o": origin, "a": like, "b": like2},
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
        retract = sorted(current_keys(engine, origin) - new)
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
    raw_item = await run.get(RELEASE.item_url, key=f"item:{RELEASE.item_id}", media_types=("application/json",))
    item_sha = run.archive(raw_item)
    item = parse_item(raw_item.body, RELEASE)
    meta_url = item["files"][RELEASE.meta_file]["url"]
    raw_meta = await run.get(meta_url, key=f"metadata:{RELEASE.item_id}", media_types=XML_MEDIA, max_bytes=2_000_000)
    meta_sha = run.archive(raw_meta)
    meta = parse_metadata(raw_meta.body)
    if not licence_ok(item, meta):
        raise UsgsFacilitiesParseError("neither the ScienceBase item nor the metadata states CC0 / public domain")
    if not not_known_is_declared(raw_meta.body):
        raise UsgsFacilitiesParseError("metadata no longer defines -999 as 'Value is not known or not available'")
    codes = capacity_code_meanings(raw_meta.body)
    raw = await run.get(
        item["files"][RELEASE.data_file]["url"],
        key=f"csv:{RELEASE.data_file}",
        media_types=CSV_MEDIA,
        max_bytes=RELEASE.max_bytes,
    )
    sha = run.archive(raw)
    entries, info = parse_csv(raw.body, min_entries=MIN_ENTRIES_LIVE if run.origin == "live" else 1)
    retrieved = raw.retrieved_at
    del raw
    parts = build_kg(entries, raw_sha256=sha, capacity_codes=codes, country_qids=load_country_qids() or {})
    out: dict[str, Any] = {
        "parse": info,
        "capacity_codes": codes,
        "kg_built": {"entities": len(parts.entities), "statements": len(parts.statements), **parts.counters},
    }
    if load:
        out["kg"] = load_kg(
            run.engine,
            run.store,
            parts,
            raw_sha256s=[sha, item_sha, meta_sha],
            retrieved_at=retrieved,
            origin=run.origin,
        )
    return out


__all__ = [
    "CONNECTOR_NAME",
    "UsgsFacilitiesParseError",
    "atlas_ingest",
    "atlas_source",
    "build_kg",
    "capacity_code_meanings",
    "commodity_qid",
    "parse_csv",
]
