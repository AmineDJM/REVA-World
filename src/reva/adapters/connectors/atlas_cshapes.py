"""CShapes 2.0 historical state borders, 1886-2019 (agent KGGEOPOL).

Source and licence
------------------
CShapes 2.0 (Schvitz, Rüegger, Girardin, Cederman, Weidmann & Gleditsch 2022, "Mapping the International
System, 1886-2017: The CShapes 2.0 Dataset", Journal of Conflict Resolution 66(1): 144-161), ETH Zürich
ICR, https://icr.ethz.ch/data/cshapes/. **Data licence: CC BY-NC-SA 4.0** (dataset page, checked
2026-10-15: "licensed under a Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
License"). Consequences recorded in :data:`RIGHTS`: attribution, **non-commercial use only**, derived
tables (simplified polygons, adjacency, statements) are shared under the same licence. If REVA is ever
used commercially, this source must be excluded.

Access: the authors' own CRAN package ``cshapes`` 2.0 (``cran.r-project.org/src/contrib/cshapes_2.0.tar.gz``,
4.2 MB) ships the complete dataset -- independent states *and* dependencies (colony, protectorate,
mandate, occupied, leased), owner state, capital, border-definition flag -- as two simplified TopoJSON
files (Gleditsch-Ward and Correlates-of-War system membership codings). This is smaller than the ICR
shapefile (13 MB, GW independent states only, no status/owner fields) and the ICR host resets
connections through the egress proxy intermittently. The package code is GPL; the data inside keep the
CC BY-NC-SA licence above.

Outputs
-------
* atlas ``cshapes.polities_gw`` / ``cshapes.polities_cow``: one row per polity period and attribute
  (``status``, ``owner``, ``capital``, ``borders_defined``), provider_geo ``gw:<code>`` / ``cow:<code>``,
  period ``[start, end + 1 day)``. ``iso3`` is the crosswalk entity the code denotes at the period's start
  (dependencies: the state the territory later became -- flagged ``iso3_meaning``; borders may differ).
* dataset ``geo.cshapes.polygons`` (``register_dataset``): one row per polity period (both codings) with
  the authors' simplified geometry as GeoJSON (coordinates rounded to 1e-4 degree, ~11 m) -- compact
  Parquet, ~1,500 rows.
* KG (GW coding): territory entities ``cshapes:gw:<code>``; statements ``cshapes:polity_period``
  (status/name/capital per period, day precision), ``cshapes:ruled_by`` (dependency -> owner state, dated),
  ``cshapes:shares_border_with`` (two polities valid at the same time share a TopoJSON boundary arc;
  dated by the overlap of their validity, consecutive periods merged) and ``geopol:corresponds_to_iso3``.

Adjacency caveat: computed from the shared arcs of the authors' simplified topology; land borders only,
no maritime boundaries; a border shorter than the simplification tolerance can be missed.
"""

from __future__ import annotations

import hashlib
import json
import lzma
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import (
    COUNTRYCODE_VERSION,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
    atlas_frame,
    code_correspondence,
    extract_tar_member,
    fetch_countrycode,
    kg_available,
    load_kg,
    load_qid_for_iso3,
)
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import RawAcquisition

SOURCE_KEY = "cshapes.cran"
CONNECTOR_NAME = "atlas.cshapes"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-cshapes-parse-1"
ATLAS_SOURCE = "cshapes"
CSHAPES_VERSION = "2.0"
PACKAGE_URL = "https://cran.r-project.org/src/contrib/cshapes_2.0.tar.gz"
PACKAGE_ARCHIVE_URL = "https://cran.r-project.org/src/contrib/Archive/cshapes/cshapes_2.0.tar.gz"
MEMBERS = {
    "gw": "cshapes/inst/extdata/cshapes_2_gw.topojson.xz",
    "cow": "cshapes/inst/extdata/cshapes_2_cow.topojson.xz",
}
CODE_FIELD = {"gw": "gwcode", "cow": "cowcode"}
MAX_BYTES = 10_000_000
MAX_TOPOJSON = 60_000_000
MIN_POLITIES = {"gw": 650, "cow": 750}
STATUSES = ("independent", "colony", "protectorate", "mandate", "occupied", "leased", "N/A")
POLYGON_DATASET_KEY = "geo.cshapes.polygons"
ROUND = 4

RIGHTS: dict[str, str] = {
    "licence": "CC BY-NC-SA 4.0",
    "licence_url": "http://creativecommons.org/licenses/by-nc-sa/4.0/",
    "attribution": "Schvitz, Rüegger, Girardin, Cederman, Weidmann & Gleditsch (2022), CShapes 2.0, ETH Zürich ICR "
    "(https://icr.ethz.ch/data/cshapes/); data via the authors' CRAN package cshapes 2.0",
    "non_commercial": "non-commercial use only; exclude this source from any commercial deployment",
    "share_alike": "derived tables (simplified polygons, adjacency, KG statements) are shared under CC BY-NC-SA 4.0",
    "package_code_licence": "GPL (>= 2) (R code only)",
}


class CshapesParseError(SourceResponseError):
    pass


@dataclass(frozen=True)
class Polity:
    system: str
    code: int
    fid: int
    name: str
    start: date
    end: date  # inclusive
    status: str
    owner: int | None
    capname: str | None
    caplong: float | None
    caplat: float | None
    b_def: int | None
    arcs: frozenset[int]
    geometry: dict[str, Any]
    index: int  # position in the TopoJSON geometry collection


def _decode_arcs(topo: Mapping[str, Any]) -> list[list[list[float]]]:
    arcs = topo["arcs"]
    tr = topo.get("transform")
    if not tr:
        return arcs
    (sx, sy), (tx, ty) = tr["scale"], tr["translate"]
    out = []
    for arc in arcs:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append([x * sx + tx, y * sy + ty])
        out.append(pts)
    return out


def _ring(arcs: list[list[list[float]]], idx: Sequence[int]) -> list[list[float]]:
    pts: list[list[float]] = []
    for a in idx:
        seg = arcs[a] if a >= 0 else list(reversed(arcs[~a]))
        if pts:
            seg = seg[1:]
        pts.extend(seg)
    return [[round(p[0], ROUND), round(p[1], ROUND)] for p in pts]


def _arc_ids(geom: Mapping[str, Any]) -> frozenset[int]:
    ids: set[int] = set()

    def walk(x: Any) -> None:
        if isinstance(x, int):
            ids.add(x if x >= 0 else ~x)
        else:
            for y in x:
                walk(y)

    walk(geom.get("arcs", []))
    return frozenset(ids)


def _geojson(arcs: list[list[list[float]]], geom: Mapping[str, Any]) -> dict[str, Any]:
    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [_ring(arcs, r) for r in geom["arcs"]]}
    if geom["type"] == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[_ring(arcs, r) for r in p] for p in geom["arcs"]]}
    raise CshapesParseError(f"unsupported geometry type {geom['type']}", retryable=False, kind="schema_drift")


def _num(v: Any) -> float | None:
    return None if v is None or v == "" else float(v)


def parse_topojson(system: str, blob_xz: bytes, *, strict: bool = True) -> list[Polity]:
    try:
        data = lzma.LZMADecompressor().decompress(blob_xz, max_length=MAX_TOPOJSON)
        topo = json.loads(data)
    except (lzma.LZMAError, ValueError) as e:
        raise CshapesParseError(f"CShapes {system} TopoJSON unreadable", retryable=False, kind="schema_drift") from e
    if topo.get("type") != "Topology" or len(topo.get("objects", {})) != 1:
        raise CshapesParseError("unexpected TopoJSON structure", retryable=False, kind="schema_drift")
    arcs = _decode_arcs(topo)
    geoms = next(iter(topo["objects"].values()))["geometries"]
    if strict and len(geoms) < MIN_POLITIES[system]:
        raise CshapesParseError(f"only {len(geoms)} polities (truncated?)", retryable=False, kind="incomplete")
    out = []
    for i, g in enumerate(geoms):
        p = g["properties"]
        status = p.get("status")
        if status not in STATUSES:
            raise CshapesParseError(f"unknown status {status!r}", retryable=False, kind="schema_drift")
        owner = p.get("owner")
        out.append(
            Polity(
                system=system,
                code=int(p[CODE_FIELD[system]]),
                fid=int(p["fid"]),
                name=str(p["country_name"]),
                start=date.fromisoformat(p["start"]),
                end=date.fromisoformat(p["end"]),
                status=status,
                owner=int(owner) if owner not in (None, "", "NA") and str(owner).lstrip("-").isdigit() else None,
                capname=p.get("capname") or None,
                caplong=_num(p.get("caplong")),
                caplat=_num(p.get("caplat")),
                b_def=None if p.get("b_def") is None else int(p["b_def"]),
                arcs=_arc_ids(g),
                geometry=_geojson(arcs, g),
                index=i,
            )
        )
    return out


def adjacency(polities: Sequence[Polity]) -> list[tuple[int, int, date, date, list[int]]]:
    """(code_a < code_b, start, end, fids) intervals in which two polities valid at the same time share an arc;
    overlapping/contiguous intervals of the same pair are merged."""
    by_arc: dict[int, list[Polity]] = defaultdict(list)
    for p in polities:
        for a in p.arcs:
            by_arc[a].append(p)
    pair_iv: dict[tuple[int, int], list[tuple[date, date, int, int]]] = defaultdict(list)
    seen: set[tuple[int, int]] = set()
    for ps in by_arc.values():
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                a, b = ps[i], ps[j]
                if a.code == b.code:
                    continue
                key = (min(a.fid, b.fid), max(a.fid, b.fid))
                if key in seen:
                    continue
                seen.add(key)
                lo, hi = max(a.start, b.start), min(a.end, b.end)
                if lo > hi:
                    continue
                x, y = (a, b) if a.code < b.code else (b, a)
                pair_iv[(x.code, y.code)].append((lo, hi, x.fid, y.fid))
    out = []
    for (ca, cb), ivs in sorted(pair_iv.items()):
        ivs.sort()
        cur_lo, cur_hi, fids = ivs[0][0], ivs[0][1], [ivs[0][2], ivs[0][3]]
        for lo, hi, fa, fb in ivs[1:]:
            if lo <= cur_hi + timedelta(days=1):
                cur_hi = max(cur_hi, hi)
                fids += [fa, fb]
            else:
                out.append((ca, cb, cur_lo, cur_hi, sorted(set(fids))))
                cur_lo, cur_hi, fids = lo, hi, [fa, fb]
        out.append((ca, cb, cur_lo, cur_hi, sorted(set(fids))))
    return out


# ----------------------------------------------------------------------------- atlas + polygons


def polity_rows(polities: Sequence[Polity], *, raw_sha256: str, crosswalk: StateCodeCrosswalk) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj

    out = []
    system = polities[0].system
    for p in polities:
        res = crosswalk.resolve(system, p.code, p.start)
        base: dict[str, Any] = {
            "country_name": p.name,
            "fid": p.fid,
            "status": p.status,
            "code_resolution": res.reason,
            "cshapes_version": CSHAPES_VERSION,
        }
        if res.iso3 and p.status != "independent":
            base["iso3_meaning"] = (
                "dependency period: iso3 is the state this territory code later denotes; borders may differ"
            )
        if res.iso3 is None and res.label:
            base["historical_state"] = res.label
        cells = [("status", p.status, {})]
        if p.owner is not None and p.status != "independent":
            own = crosswalk.resolve(system, p.owner, p.start)
            cells.append(("owner", f"{system}:{p.owner}", {"owner_iso3": own.iso3, "owner_label": own.label}))
        if p.capname:
            cells.append(("capital", p.capname, {"caplong": p.caplong, "caplat": p.caplat}))
        if p.b_def is not None:
            cells.append(("borders_defined", "yes" if p.b_def == 1 else "partly undefined", {"b_def": p.b_def}))
        for ind, val, extra in cells:
            out.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": f"polities_{system}",
                    "indicator": ind,
                    "concept_id": None,
                    "iso3": res.iso3,
                    "provider_geo": f"{system}:{p.code}",
                    "entity_qid": res.qid,
                    "period_start": p.start,
                    "period_end": p.end + timedelta(days=1),
                    "value": None,
                    "value_text": val,
                    "unit": "category" if ind != "capital" else "name",
                    "flags": json_obj({**base, **extra}),
                    "raw_sha256": raw_sha256,
                    "selector": json_obj(file=MEMBERS[system], geometry_index=p.index, fid=p.fid),
                }
            )
    return out


def polygon_table(polities: Sequence[Polity], *, raw_sha256: str) -> Any:
    import pyarrow as pa

    cols: dict[str, list[Any]] = defaultdict(list)
    for p in polities:
        cols["code_system"].append(p.system)
        cols["code"].append(p.code)
        cols["fid"].append(p.fid)
        cols["name"].append(p.name)
        cols["valid_from"].append(p.start)
        cols["valid_to_inclusive"].append(p.end)
        cols["status"].append(p.status)
        cols["owner_code"].append(p.owner)
        cols["capital"].append(p.capname)
        cols["borders_defined"].append(p.b_def)
        cols["geometry_geojson"].append(json.dumps(p.geometry, separators=(",", ":")))
        cols["raw_sha256"].append(raw_sha256)
        cols["selector"].append(json.dumps({"file": MEMBERS[p.system], "geometry_index": p.index}))
    schema = pa.schema(
        [
            ("code_system", pa.string()),
            ("code", pa.int32()),
            ("fid", pa.int32()),
            ("name", pa.string()),
            ("valid_from", pa.date32()),
            ("valid_to_inclusive", pa.date32()),
            ("status", pa.string()),
            ("owner_code", pa.int32()),
            ("capital", pa.string()),
            ("borders_defined", pa.int8()),
            ("geometry_geojson", pa.string()),
            ("raw_sha256", pa.string()),
            ("selector", pa.string()),
        ]
    )
    return pa.table({f.name: cols[f.name] for f in schema}, schema=schema)


# ----------------------------------------------------------------------------- KG


def kg_records(
    polities: Sequence[Polity], *, raw_sha256: str, resolver: StateEntityResolver
) -> tuple[list[KgEntity], list[KgStatement]]:
    """GW coding only (one territory entity per GW code)."""
    member = MEMBERS["gw"]
    by_code: dict[int, list[Polity]] = defaultdict(list)
    for p in polities:
        by_code[p.code].append(p)
    ents, stmts = [], []
    for code, ps in sorted(by_code.items()):
        ps.sort(key=lambda p: p.start)
        names = sorted({p.name for p in ps}, key=lambda n: -sum(1 for p in ps if p.name == n))
        eid = f"cshapes:gw:{code}"
        ents.append(
            KgEntity(
                qid=eid,
                kind="territory",
                labels={"en": f"{ps[-1].name} (CShapes GW {code})"},
                descriptions={
                    "en": f"territory coded GW {code} in CShapes {CSHAPES_VERSION}, {ps[0].start.year}-"
                    f"{ps[-1].end.year}; names: {'; '.join(names)[:300]}"
                },
            )
        )
        for p in ps:
            sel = {"file": member, "geometry_index": p.index, "fid": p.fid}
            val: dict[str, Any] = {"type": "cshapes_period", "status": p.status, "name": p.name}
            if p.capname:
                val["capital"] = p.capname
            if p.b_def is not None:
                val["borders_defined"] = p.b_def
            stmts.append(
                KgStatement(
                    key=f"gw{code}:period:{p.fid}",
                    subject=eid,
                    property="cshapes:polity_period",
                    value=val,
                    start=p.start.isoformat(),
                    start_precision=11,
                    end=p.end.isoformat(),
                    end_precision=11,
                    qualifiers={"fid": p.fid, "dataset_window": "1886-01-01..2019-12-31"},
                    raw_sha256=raw_sha256,
                    selector=sel,
                )
            )
            if p.owner is not None and p.status not in ("independent", "N/A") and p.owner != p.code:
                owner = resolver.entity("gw", p.owner, p.start)
                stmts.append(
                    KgStatement(
                        key=f"gw{code}:ruled_by:{p.fid}",
                        subject=eid,
                        property="cshapes:ruled_by",
                        object_qid=owner,
                        start=p.start.isoformat(),
                        start_precision=11,
                        end=p.end.isoformat(),
                        end_precision=11,
                        qualifiers={"status": p.status, "owner_gwcode": p.owner, "fid": p.fid},
                        raw_sha256=raw_sha256,
                        selector=sel,
                    )
                )
    for ca, cb, lo, hi, fids in adjacency(polities):
        stmts.append(
            KgStatement(
                key=f"gw{ca}:border:gw{cb}:{lo.isoformat()}",
                subject=f"cshapes:gw:{ca}",
                property="cshapes:shares_border_with",
                object_qid=f"cshapes:gw:{cb}",
                start=lo.isoformat(),
                start_precision=11,
                end=hi.isoformat(),
                end_precision=11,
                qualifiers={
                    "method": "shared boundary arc in the CShapes 2.0 simplified TopoJSON topology, polities valid "
                    "at the same time; land borders only",
                    "fids": fids[:40],
                },
                raw_sha256=raw_sha256,
                selector={"file": member, "fids": fids[:40]},
            )
        )
    # territory entity -> crosswalk entity, by year (states only after they exist in the crosswalk)
    for code, ps in sorted(by_code.items()):
        years = {y for p in ps for y in range(p.start.year, p.end.year + 1)}
        runs: dict[str, list[int]] = defaultdict(list)
        for y in sorted(years):
            r = resolver.crosswalk.resolve("gw", code, date(y, 7, 1))
            if r.iso3:
                runs[r.iso3].append(y)
        from reva.adapters.connectors.atlas_geopol_common import merge_years

        for iso3, ys in runs.items():
            for a, b in merge_years(ys):
                stmts.append(
                    KgStatement(
                        key=f"gw{code}:iso3:{iso3}:{a}",
                        subject=f"cshapes:gw:{code}",
                        property="geopol:corresponds_to_iso3",
                        value={"type": "string", "value": iso3},
                        start=f"{a}-01-01",
                        start_precision=9,
                        end=f"{b}-12-31",
                        end_precision=9,
                        qualifiers={"method": "GW code -> crosswalk entity (countrycode panel + STATE_CODE_POLICY)"},
                        raw_sha256=raw_sha256,
                        selector={"file": member, "gwcode": code},
                    )
                )
    return ents, stmts


# ----------------------------------------------------------------------------- atlas source + ingest


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="ETH Zürich ICR (CShapes 2.0, via the authors' CRAN package)",
        licence=RIGHTS["licence"],
        allowed_hosts=("cran.r-project.org",),
        datasets=("polities_gw", "polities_cow"),
        dataset_ids=(f"cshapes-{CSHAPES_VERSION}", f"countrycode-{COUNTRYCODE_VERSION}"),
        cadence_seconds=180 * 86400,
        planned_requests=4,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={"geo": "GW / COW codes", "time": "polity period start..end (day)", "kg": "cshapes:gw:<code>"},
        notes="CC BY-NC-SA 4.0: non-commercial, share-alike. Polygons registered as dataset geo.cshapes.polygons.",
    )


def build(
    package_body: bytes,
    *,
    crosswalk: StateCodeCrosswalk,
    qid_for_iso3: Mapping[str, str],
    countrycode_sha256: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    sha = hashlib.sha256(package_body).hexdigest()
    pols = {
        s: parse_topojson(s, extract_tar_member(package_body, m, max_bytes=MAX_BYTES), strict=strict)
        for s, m in MEMBERS.items()
    }
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    ents, stmts = kg_records(pols["gw"], raw_sha256=sha, resolver=resolver)
    if countrycode_sha256:
        stmts += code_correspondence(resolver, raw_sha256=countrycode_sha256)
    return {
        "sha": sha,
        "polities": pols,
        "atlas": {f"polities_{s}": polity_rows(p, raw_sha256=sha, crosswalk=crosswalk) for s, p in pols.items()},
        "polygons": polygon_table([*pols["gw"], *pols["cow"]], raw_sha256=sha),
        "entities": ents + resolver.code_entities(),
        "statements": stmts,
    }


def register_polygons(run: Any, table: Any, raw_sha256: str) -> dict[str, Any]:
    from sqlalchemy import text

    from reva.evidence.locks import take_evidence_write_lock
    from reva.simulation.datasets import register_dataset

    with run.engine.begin() as conn:
        take_evidence_write_lock(conn)
        acq = conn.execute(
            text(
                "SELECT id::text FROM evidence.acquisition WHERE raw_sha256 = :s AND origin = :o "
                "ORDER BY recorded_at DESC, id DESC LIMIT 1"
            ),
            {"s": raw_sha256, "o": run.origin},
        ).scalar_one()
        rec = register_dataset(
            conn,
            run.store,
            dataset_key=POLYGON_DATASET_KEY,
            concept_id="geo.polygons",
            subject_key="cshapes.polygons",
            table=table,
            source_acquisition_ids=[acq],
            parser_version=PARSER_VERSION,
            origin=run.origin,
            coverage={
                "rows": table.num_rows,
                "codings": ["gw", "cow"],
                "window": "1886-01-01..2019-12-31",
                "geometry": f"GeoJSON, authors' simplified topology, coordinates rounded to 1e-{ROUND} deg",
            },
            actor=run.actor,
            rights=RIGHTS,
        )
    return {
        "dataset_key": rec.dataset_key,
        "row_count": rec.row_count,
        "created": rec.created,
        "artifact_sha256": rec.artifact_sha256,
    }


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.kernel.errors import SourceResponseError as _SRE

    crosswalk, cc_sha = await fetch_countrycode(run)
    media = ("application/x-gzip", "application/gzip", "application/octet-stream", "application/x-tar")
    try:
        raw: RawAcquisition = await run.get(
            PACKAGE_URL, key=f"cshapes:{CSHAPES_VERSION}", media_types=media, max_bytes=MAX_BYTES
        )
    except _SRE:
        raw = await run.get(
            PACKAGE_ARCHIVE_URL, key=f"cshapes:{CSHAPES_VERSION}", media_types=media, max_bytes=MAX_BYTES
        )
    out = build(
        raw.body,
        crosswalk=crosswalk,
        qid_for_iso3=load_qid_for_iso3(),
        countrycode_sha256=cc_sha,
        strict=run.origin == "live",
    )
    sha = run.archive(raw)
    res: dict[str, Any] = {"atlas": {}, "polities": {s: len(p) for s, p in out["polities"].items()}}
    for ds, rows in out["atlas"].items():
        if run.wants(ds):
            r = run.register(
                dataset=ds,
                frame=atlas_frame(rows),
                raw_sha256s=[sha],
                rights=RIGHTS,
                coverage_note=f"CShapes {CSHAPES_VERSION} polity periods 1886-2019 ({ds[-3:].upper()} "
                "coding), incl. dependencies; CC BY-NC-SA 4.0",
            )
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created")}
            res["atlas"][ds]["unmapped_provider_geo"] = len(r["unmapped_provider_geo"])
    if run.dataset is None:
        res["polygons"] = register_polygons(run, out["polygons"], sha)
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision=f"cshapes-{CSHAPES_VERSION}",
            raw_sha256s=[sha, cc_sha],
            retrieved_at=raw.retrieved_at,
            origin=run.origin,
        )
    return res
