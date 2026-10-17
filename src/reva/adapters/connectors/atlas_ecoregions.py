"""RESOLVE Ecoregions 2017 (agent V4ECO): ecoregions, biomes and biogeographic realms as KG entities.

Source
------
Dinerstein, E. et al. (2017), "An Ecoregion-Based Approach to Protecting Half the Terrestrial Realm",
BioScience 67(6): 534-545; data release ``Ecoregions2017.zip`` (CC BY 4.0, "(c) Resolve", stated in the
``LICENSE`` attribute of every record) at ``storage.googleapis.com/teow2016``. The release is a 149 MB zip
holding a shapefile; REVA reads **only its attribute table** ``Ecoregions2017.dbf`` (~1 MB, 55 kB compressed)
with two HTTP byte ranges (the zip's central directory at the end of the archive, then the dbf member at its
offset), pinned by the archive ETag (``If-Range``): the 243 MB polygons are never downloaded.

KG (``load_batch``, source key ``resolve.ecoregions2017``):

* ``resolve_eco:<ECO_ID>`` (kind ``ecoregion``, label ``ECO_NAME``), ``resolve_biome:<BIOME_NUM>`` (kind ``biome``,
  label ``BIOME_NAME``), ``resolve_realm:<REALM>`` (kind ``biogeographic_realm``; the realm name is the release's
  only code for it, used verbatim with spaces as ``_``);
* ``resolve:in_biome`` and ``resolve:in_realm`` (ecoregion -> biome / realm; family ``part_of``) and
  ``resolve:nature_needs_half`` (NNH category 1-4 with its name, verbatim).

Not loaded: ecoregion <-> country membership (the release has no such table; an overlay of the polygons with
country borders would be a REVA derivation, which V4CORE's rules keep out of ``located_in``) and the polygon
areas (``SHAPE_AREA`` is in squared decimal degrees, not an area unit: kept as a qualifier, never converted).
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from reva.adapters.connectors.eco_common import guarded_load
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.ecoregions"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-ecoregions-1"
SOURCE_KEY = "resolve.ecoregions2017"
ATLAS_SOURCE = "ecoregions"
DATASET = "ecoregions2017_kg"  # KG only: no atlas table
HOST = "storage.googleapis.com"
ARCHIVE_URL = f"https://{HOST}/teow2016/Ecoregions2017.zip"
DBF_MEMBER = "Ecoregions2017.dbf"
TAIL_BYTES = 65_536
MAX_DBF_BYTES = 5_000_000
MIN_ECOREGIONS = 800  # 847 records (846 ecoregions + "Rock and Ice") in the 2017 release
EXPECTED_LICENCE = "CC-BY 4.0"
REQUIRED_FIELDS = ("ECO_NAME", "BIOME_NUM", "BIOME_NAME", "REALM", "ECO_ID", "NNH", "NNH_NAME", "LICENSE")
PIDS = ("resolve:in_biome", "resolve:in_realm", "resolve:nature_needs_half")
RELEASE_YEAR = 2017


class EcoregionsError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


# ----------------------------------------------------------------------------- dBase III table
@dataclass(frozen=True)
class DbfField:
    name: str
    ftype: str
    length: int
    decimals: int


def parse_dbf(data: bytes) -> tuple[list[DbfField], list[dict[str, str]], str]:
    """Fields and records (values stripped, as text) of a dBase III/IV table; deleted records are skipped.

    Text is decoded as strict UTF-8; when that fails the table is decoded as cp1252 and the encoding is returned
    so the caller can record it (never silently)."""
    if len(data) < 33:
        raise EcoregionsError("dbf shorter than its header")
    nrec, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields: list[DbfField] = []
    p = 32
    while p < hlen - 1 and data[p] != 0x0D:
        d = data[p : p + 32]
        name = d[:11].split(b"\x00", 1)[0].decode("ascii", "strict")
        fields.append(DbfField(name, chr(d[11]), d[16], d[17]))
        p += 32
    if not fields or sum(f.length for f in fields) + 1 != rlen:
        raise EcoregionsError("dbf field descriptors do not add up to the record length")
    if hlen + nrec * rlen > len(data):
        raise EcoregionsError(f"dbf holds fewer bytes than {nrec} records of {rlen}")
    encoding = "utf-8"
    try:
        data[hlen : hlen + nrec * rlen].decode("utf-8", "strict")
    except UnicodeDecodeError:
        encoding = "cp1252"
    rows: list[dict[str, str]] = []
    for i in range(nrec):
        rec = data[hlen + i * rlen : hlen + (i + 1) * rlen]
        if rec[:1] == b"*":
            continue
        q = 1
        row: dict[str, str] = {}
        for f in fields:
            row[f.name] = rec[q : q + f.length].decode(encoding, "strict").strip()
            q += f.length
        rows.append(row)
    return fields, rows, encoding


# ----------------------------------------------------------------------------- records
@dataclass(frozen=True)
class Ecoregion:
    eco_id: int
    name: str
    biome_num: int
    biome_name: str
    realm: str
    nnh: int | None
    nnh_name: str | None
    eco_biome_code: str | None
    shape_area_deg2: str | None


def _intval(s: str, what: str) -> int:
    try:
        f = float(s)
    except ValueError as e:
        raise EcoregionsError(f"{what} {s!r} is not numeric") from e
    if f != int(f):
        raise EcoregionsError(f"{what} {s!r} is not an integer")
    return int(f)


def ecoregions(rows: Sequence[Mapping[str, str]]) -> list[Ecoregion]:
    if not rows:
        raise EcoregionsError("dbf has no records")
    missing = [f for f in REQUIRED_FIELDS if f not in rows[0]]
    if missing:
        raise EcoregionsError(f"dbf lacks fields {missing}")
    licences = {r["LICENSE"] for r in rows}
    if licences != {EXPECTED_LICENCE}:
        raise SourceResponseError(
            f"Ecoregions2017 LICENSE values {sorted(licences)} != {EXPECTED_LICENCE!r}", retryable=False, kind="licence"
        )
    out: list[Ecoregion] = []
    seen: set[int] = set()
    for r in rows:
        eid = _intval(r["ECO_ID"], "ECO_ID")
        if eid in seen:
            raise EcoregionsError(f"duplicate ECO_ID {eid}")
        seen.add(eid)
        nnh = _intval(r["NNH"], "NNH") if r["NNH"] else None
        out.append(
            Ecoregion(
                eco_id=eid,
                name=r["ECO_NAME"],
                biome_num=_intval(r["BIOME_NUM"], "BIOME_NUM"),
                biome_name=r["BIOME_NAME"],
                realm=r["REALM"],
                nnh=nnh,
                nnh_name=r["NNH_NAME"] or None,
                eco_biome_code=r.get("ECO_BIOME_") or None,
                shape_area_deg2=r.get("SHAPE_AREA") or None,
            )
        )
    if len(out) < MIN_ECOREGIONS:
        raise EcoregionsError(f"only {len(out)} ecoregions (< {MIN_ECOREGIONS}): truncated table?", kind="incomplete")
    return out


def _na(v: str | None) -> bool:
    return v is None or v.strip().upper() in ("", "N/A", "NA")


def realm_node(realm: str) -> str | None:
    """``resolve_realm:<REALM>`` (spaces -> ``_``); ``None`` for an empty or N/A realm (Rock and Ice)."""
    if _na(realm):
        return None
    return "resolve_realm:" + realm.strip().replace(" ", "_")


def kg_parts(ecos: Sequence[Ecoregion], *, raw_sha256: str, source_revision: str) -> tuple[list[Any], list[Any]]:
    from reva.kg.model import KgEntity, KgStatement, KgTime

    pit = KgTime(str(RELEASE_YEAR), 9)
    ref = [{"stated_in": "RESOLVE Ecoregions 2017 (Dinerstein et al. 2017)", "P854": ARCHIVE_URL,
            "member": DBF_MEMBER, "licence": "CC BY 4.0"}]  # fmt: skip
    ents: list[Any] = []
    sts: list[Any] = []
    biomes: dict[int, str] = {}
    realms: dict[str, str] = {}
    for e in ecos:
        node = f"resolve_eco:{e.eco_id}"
        has_biome = not _na(e.biome_name)  # "Rock and Ice" (ECO_ID 0): BIOME_NAME, REALM and NNH_NAME are "N/A"
        if has_biome:
            if e.biome_num in biomes and biomes[e.biome_num] != e.biome_name:
                raise EcoregionsError(f"BIOME_NUM {e.biome_num} has two names")
            biomes[e.biome_num] = e.biome_name
        ents.append(
            KgEntity(
                qid=node,
                kind="ecoregion",
                labels={"en": e.name},
                descriptions={
                    "en": f"terrestrial ecoregion (RESOLVE Ecoregions 2017, ECO_ID {e.eco_id}); {e.biome_name}; "
                    f"{e.realm or 'no realm'}",
                    "fr": f"écorégion terrestre (Écorégions RESOLVE 2017, ECO_ID {e.eco_id})",
                },
                source_revision=source_revision,
            )
        )
        quals = {"ECO_ID": e.eco_id, "ECO_BIOME_": e.eco_biome_code, "SHAPE_AREA_deg2": e.shape_area_deg2}
        if has_biome:
            sts.append(
                KgStatement(
                    subject_qid=node,
                    property_pid="resolve:in_biome",
                    object_qid=f"resolve_biome:{e.biome_num}",
                    qualifiers={**quals, "BIOME_NAME": e.biome_name},
                    point_in_time=pit,
                    references=ref,
                    statement_id=f"in_biome:{e.eco_id}",
                    source_revision=source_revision,
                    raw_sha256=raw_sha256,
                )
            )
        rn = realm_node(e.realm)
        if rn is not None:
            realms[rn] = e.realm
            sts.append(
                KgStatement(
                    subject_qid=node,
                    property_pid="resolve:in_realm",
                    object_qid=rn,
                    qualifiers={"REALM": e.realm},
                    point_in_time=pit,
                    references=ref,
                    statement_id=f"in_realm:{e.eco_id}",
                    source_revision=source_revision,
                    raw_sha256=raw_sha256,
                )
            )
        if e.nnh is not None and not _na(e.nnh_name):
            sts.append(
                KgStatement(
                    subject_qid=node,
                    property_pid="resolve:nature_needs_half",
                    value={"type": "string", "value": str(e.nnh)},
                    qualifiers={"NNH_NAME": e.nnh_name},
                    point_in_time=pit,
                    references=ref,
                    statement_id=f"nnh:{e.eco_id}",
                    source_revision=source_revision,
                    raw_sha256=raw_sha256,
                )
            )
    for num, name in sorted(biomes.items()):
        ents.append(
            KgEntity(
                qid=f"resolve_biome:{num}",
                kind="biome",
                labels={"en": name},
                descriptions={"en": f"biome {num} of RESOLVE Ecoregions 2017", "fr": f"biome {num} (RESOLVE 2017)"},
                source_revision=source_revision,
            )
        )
    for node, name in sorted(realms.items()):
        ents.append(
            KgEntity(
                qid=node,
                kind="biogeographic_realm",
                labels={"en": name},
                descriptions={
                    "en": "biogeographic realm of RESOLVE Ecoregions 2017",
                    "fr": "domaine biogéographique (Écorégions RESOLVE 2017)",
                },  # fmt: skip
                source_revision=source_revision,
            )
        )
    return ents, sts


# ----------------------------------------------------------------------------- atlas source protocol
def atlas_source() -> Any:
    from reva.adapters.connectors.base import module_digest
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="RESOLVE (Dinerstein et al. 2017), Ecoregions 2017 release",
        licence="CC BY 4.0 (LICENSE attribute of the release); attribution Dinerstein et al. 2017, (c) Resolve",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("Ecoregions2017.zip:Ecoregions2017.dbf",),
        cadence_seconds=365 * 86400,
        planned_requests=2,
        max_response_bytes=MAX_DBF_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(atlas_source),
        field_mappings={
            "kg": list(PIDS),
            "entities": ["resolve_eco:<ECO_ID>", "resolve_biome:<BIOME_NUM>", "resolve_realm:<REALM>"],
        },  # fmt: skip
        notes="Two byte ranges of the release zip (central directory, dbf member); polygons never downloaded.",
    )


async def atlas_ingest(run: Any, *, load_kg: bool = True) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_wid import content_range, inflate_member, parse_central_directory

    if not run.wants(DATASET):
        return {"skipped": DATASET}
    tail = await run.get_range(ARCHIVE_URL, key="zip:tail", suffix=TAIL_BYTES)
    start, _end, size = content_range(tail)
    etag = (tail.response_metadata.get("headers") or {}).get("etag")
    members, implied = parse_central_directory(tail.body, start)
    if implied != size:
        raise EcoregionsError(f"zip central directory implies {implied} bytes, server says {size}")
    member = next((m for m in members if m.name == DBF_MEMBER), None)
    if member is None:
        raise EcoregionsError(f"{DBF_MEMBER} not in the archive ({[m.name for m in members]})")
    if member.csize > MAX_DBF_BYTES - 2048:
        raise EcoregionsError(
            f"{DBF_MEMBER} is {member.csize} bytes compressed (cap {MAX_DBF_BYTES})", kind="too_large"
        )
    hi = member.offset + 30 + member.name_len + member.extra_len + 1024 + member.csize - 1
    part = await run.get_range(ARCHIVE_URL, key="zip:dbf", start=member.offset, end=min(hi, size - 1), if_range=etag)
    served_start, _e, _s = content_range(part)
    dbf = inflate_member(part.body, served_start, member)
    tail_sha, dbf_sha = run.archive(tail), run.archive(part)
    fields, rows, encoding = parse_dbf(dbf)
    ecos = ecoregions(rows)
    revision = f"etag:{(etag or '').strip(chr(34))}" if etag else f"sha256:{dbf_sha[:16]}"
    out: dict[str, Any] = {
        "archive_bytes": size,
        "etag": etag,
        "dbf_bytes": len(dbf),
        "dbf_encoding": encoding,
        "fields": [f.name for f in fields],
        "ecoregions": len(ecos),
        "biomes": len({e.biome_num for e in ecos}),
        "realms": sorted({e.realm for e in ecos}),
    }
    if load_kg:
        ents, sts = kg_parts(ecos, raw_sha256=dbf_sha, source_revision=revision[:100])
        out["kg"] = guarded_load(
            run.engine,
            run.store,
            pids=PIDS,
            entities=ents,
            statements=sts,
            source_key=SOURCE_KEY,
            raw_sha256s=[tail_sha, dbf_sha],
            retrieved_at=part.retrieved_at,
            origin=run.origin,
            parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "encoding": encoding},
            fill_missing_only=False,
        )
    return out


def release_date() -> date:
    return date(RELEASE_YEAR, 1, 1)


__all__ = [
    "CONNECTOR_NAME",
    "PIDS",
    "atlas_ingest",
    "atlas_source",
    "ecoregions",
    "kg_parts",
    "parse_dbf",
    "realm_node",
]
