"""Read-only exploration of any KG entity by universal relation family (REVA V4, owner V4CORE).

* :func:`entity_families` — the neighbours of one entity (any kind: compound, taxon, company, region, …)
  grouped by family (``made_of``, ``located_in``, ``produced_by``…), each with its statement provenance, the
  source's own property, the share when the mapping declares one (Missing = ``None``, never 0) and the licence of
  the raw artifact the statement was parsed from. Unmapped entity-valued properties are counted, not hidden.
* :func:`family_paths` — :func:`reva.kg.query.paths` restricted to the families asked for, in the reading
  direction asked for ("part of" is not "has part"), each hop annotated with its family.
* :func:`annotate_hops` — adds the family reading to the hops of any KG path result.

Honesty: a family is never causal. Causal claims are served by ``/v1/causal`` as attributed claims; the KG
causal properties (P828…) belong to no family and are not traversed by a family-constrained path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import Connection, text

from reva.kernel.errors import ValidationProblem
from reva.kg import query as kq
from reva.kg.families import (
    CONCORDANCE_FAMILY,
    IDENTITY_FAMILY,
    FamilyError,
    FamilyRegistry,
    concordance_flags,
    load_registry,
    measured_by_target,
    share_of,
)
from reva.kg.model import CAUSAL_PIDS

MAX_PER_FAMILY = 200
#: Rows of family edges read around one entity (a hub such as a country can have 100k+ incoming statements).
MAX_SCAN = 20_000
MAX_LITERALS = 300
FAMILY_NOTE = (
    "Relation families group the statements sources publish (part of, made of, located in, produced by…). "
    "No family is causal: a link is a sourced statement, not an explanation; causal claims are served by "
    "/v1/causal as attributed claims."
)
ASSOCIATION_NOTE = "associated_with is an association or co-occurrence reported by a source, never a cause."
WIKIDATA_RIGHTS: dict[str, Any] = {
    "licence": "CC0-1.0 (Wikidata)",
    "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "attribution": None,
    "commercial_use": "permitted",
    "non_commercial": False,
    "share_alike": False,
    "no_derivatives": False,
    "open_licence": True,
    "restricted": False,
    "basis": "wikidata_cc0",
}


def registry() -> FamilyRegistry:
    try:
        return load_registry()
    except FamilyError as e:  # a broken agent drop-in must be visible, not silently ignored
        raise ValidationProblem("relation family registry is invalid", detail=str(e)[:500]) from e


def check_families(reg: FamilyRegistry, families: Iterable[str] | None) -> list[str] | None:
    if families is None:
        return None
    fams = sorted(set(families))
    unknown = [f for f in fams if f not in reg.families]
    if unknown or not fams:
        raise ValidationProblem("unknown relation family", unknown=unknown[:10], known=sorted(reg.families))
    return fams


# ---------------------------------------------------------------------------------------------- rights
def rights_for(conn: Connection, rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Normalised rights per raw artifact sha256 (``meta.artifact.rights`` as the connector recorded them;
    Wikidata sources are CC0). ``None`` = the artifact records no licence (shown as unknown, never as open)."""
    from reva.atlas.snapshot import normalise_rights

    out: dict[str, dict[str, Any] | None] = {}
    need = sorted({str(r["raw_sha256"]) for r in rows if r.get("raw_sha256")})
    if need:
        got = {
            x["sha256"]: x["rights"]
            for x in conn.execute(
                text("SELECT sha256, rights FROM meta.artifact WHERE sha256 = ANY(:s)"), {"s": need}
            ).mappings()
        }
        for sha in need:
            raw = got.get(sha)
            out[sha] = normalise_rights(raw, basis="raw_artifact_rights") if raw else None
            if out[sha] is not None and isinstance(raw, Mapping) and raw.get("source_key"):
                out[sha] = {**out[sha], "recorded_for_source_key": str(raw["source_key"])}  # type: ignore[dict-item]
    return out


def _statement_licence(r: Mapping[str, Any]) -> str | None:
    refs = r.get("refs")
    for ref in refs if isinstance(refs, list) else []:
        if isinstance(ref, Mapping) and isinstance(ref.get("licence"), str) and ref["licence"].strip():
            if not ref.get("raw_sha256") or ref.get("raw_sha256") == r.get("raw_sha256"):
                return str(ref["licence"])
    return None


def _rights(r: Mapping[str, Any], by_sha: Mapping[str, dict[str, Any] | None]) -> dict[str, Any] | None:
    """Rights of one statement: the raw artifact's recorded rights, except when that artifact was recorded for
    *another* source (one connector run fetched two providers' files, e.g. Natural Earth admin-1 polygons archived
    by the GHCN-M run under NOAA's terms, AUDIT3 L2): then the licence the statement's own reference states."""
    if kq.is_community_edited(r.get("source_key")):
        return dict(WIKIDATA_RIGHTS)
    art = by_sha.get(str(r.get("raw_sha256")))
    rec = (art or {}).get("recorded_for_source_key")
    own = _statement_licence(r)
    if own and rec and r.get("source_key") and rec != r.get("source_key"):
        from reva.atlas.snapshot import normalise_rights

        fixed = normalise_rights({"licence": own}, basis="statement_reference")
        if fixed is not None:
            return {**fixed, "artifact_recorded_for_source_key": rec}
    return art


# ---------------------------------------------------------------------------------------------- families
def entity_families(
    conn: Connection,
    qid: str,
    *,
    families: Iterable[str] | None = None,
    per_family: int = 50,
    time_window: kq.TimeWindow | None = None,
    origin: str | None = None,
    include_literals: bool = True,
    reg: FamilyRegistry | None = None,
) -> dict[str, Any]:
    """Neighbours of ``qid`` grouped by relation family (see module doc)."""
    kq._check_qid(qid)
    kq._check_origin(origin)
    reg = reg or registry()
    fams = check_families(reg, families)
    per_family = max(1, min(int(per_family), MAX_PER_FAMILY))
    pids = sorted(reg.pids_for(fams) if fams is not None else reg.mappings)
    tsql, tparams = kq._time_sql(time_window)
    osql = kq._origin_sql(origin)
    conn.exec_driver_sql("SET LOCAL jit = off")
    rows = [
        dict(r)
        for r in conn.execute(
            text(f"""WITH e AS (
                       SELECT {kq._ST_SELECT}, (s.subject_qid = :q) AS is_out FROM kg.statement_current s
                        WHERE (s.subject_qid = :q OR s.object_qid = :q) AND s.object_qid IS NOT NULL
                          AND s.subject_qid <> s.object_qid AND s.property_pid = ANY(:pids){tsql}{osql}
                        ORDER BY s.id LIMIT :scan)
                     SELECT * FROM (
                       SELECT e.*, row_number() OVER (PARTITION BY e.property_pid, e.is_out ORDER BY e.id) AS rn,
                              count(*) OVER (PARTITION BY e.property_pid, e.is_out) AS n_part FROM e) x
                      WHERE x.rn <= :per ORDER BY x.id"""),
            {"q": qid, "pids": pids, "scan": MAX_SCAN + 1, "per": per_family, "origin": origin, **tparams},
        ).mappings()
    ]
    total_scanned = _count_scanned(rows)
    capped = total_scanned > MAX_SCAN
    unmapped = _unmapped(conn, qid, reg, tsql, tparams, origin)
    literals = _literals(conn, qid, origin) if include_literals else []
    qids = {qid} | {r["subject_qid"] for r in rows} | {r["object_qid"] for r in rows}
    labels = kq.labels_for(conn, qids)
    kinds = kq.kinds_for(conn, qids)
    loaded = loaded_qids(conn, qids)
    props = kq.property_info(conn, {r["property_pid"] for r in rows} | {r["property_pid"] for r in literals})
    rights = rights_for(conn, [*rows, *literals])
    groups: dict[str, dict[str, Any]] = {}
    counted: set[tuple[str, bool]] = set()
    for r in rows:
        ef = reg.edge_family(r["property_pid"], "out" if r["is_out"] else "in")
        if ef is None:  # pragma: no cover - pids come from the registry
            continue
        if fams is not None and ef.family not in fams:
            continue
        g = groups.setdefault(ef.family, {"family": ef.family, "count": 0, "neighbours": [], "properties": {}})
        part = (r["property_pid"], bool(r["is_out"]))
        if part not in counted:
            counted.add(part)
            g["count"] += int(r["n_part"])
            g["properties"][r["property_pid"]] = g["properties"].get(r["property_pid"], 0) + int(r["n_part"])
        if len(g["neighbours"]) >= per_family:
            continue
        st = kq._statement_out(r, labels, props, kinds)
        st["direction"] = "out" if r["is_out"] else "in"
        other = r["object_qid"] if r["is_out"] else r["subject_qid"]
        m = ef.mapping
        g["neighbours"].append(
            {
                "neighbour": {**kq._ref(other, labels, kinds), "entity_loaded": other in loaded},
                "reading": {"label": dict(ef.label), "inverse": ef.inverse},
                "subtype": m.subtype,
                "mapping": {"origin": m.origin, "agent": m.agent, "direction": m.direction},
                "share": share_of(m, r.get("qualifiers")) if m.share_qualifier else None,
                "atlas": _atlas_hint(ef.family, other, r.get("qualifiers"), kinds.get(other)),
                "identity": ef.family == IDENTITY_FAMILY,
                "flags": concordance_flags(r.get("qualifiers")) if ef.family == CONCORDANCE_FAMILY else {},
                "rights": _rights(r, rights),
                "statement": st,
            }
        )
    out_groups = []
    for fid, fam in reg.families.items():
        if fid not in groups:
            continue
        g = groups[fid]
        out_groups.append(
            {
                "family": fid,
                "label": dict(fam.label),
                "inverse_label": dict(fam.inverse_label),
                "group": fam.group,
                "symmetric": fam.symmetric,
                "count": g["count"],
                "count_is_lower_bound": capped,
                "shown": len(g["neighbours"]),
                "truncated": capped or g["count"] > len(g["neighbours"]),
                "properties": [{"pid": p, "count": n, "label_fr": (props.get(p) or {}).get("label_fr"),
                                "label_en": (props.get(p) or {}).get("label_en")}
                               for p, n in sorted(g["properties"].items())],
                "neighbours": g["neighbours"],
                "causal": False,
            }
        )  # fmt: skip
    lit_out = []
    for r in literals:
        st = kq._statement_out(r, labels, props, kinds)
        st["rights"] = _rights(r, rights)
        lit_out.append(st)
    notes = [FAMILY_NOTE]
    if "associated_with" in groups:
        notes.append(ASSOCIATION_NOTE)
    if any(not n["neighbour"]["entity_loaded"] for g in out_groups for n in g["neighbours"]):
        notes.append(UNLOADED_NOTE)
    origins = sorted({r["origin"] for r in rows} | {r["origin"] for r in literals})
    return {
        "qid": qid,
        "label": kq._display(qid, labels),
        "labels": labels.get(qid, {}),
        "kind": kinds.get(qid),
        "families": out_groups,
        "unmapped": unmapped,
        "literals": lit_out,
        "literals_truncated": len(literals) >= MAX_LITERALS,
        "scan_capped": capped,
        "registry": {"families": len(reg.families), "mappings": len(reg.mappings), "files": list(reg.files)},
        "notes": notes,
        "origins": origins,
    }


UNLOADED_NOTE = (
    "Neighbours with entity_loaded = false are references to entities the KG has not loaded (no label, no statements "
    "of their own yet, e.g. UniProt proteins named by HGNC): the edge is kept as the source states it."
)


def loaded_qids(conn: Connection, qids: Iterable[str]) -> set[str]:
    """Which of ``qids`` exist as KG entities (any origin) -- an edge may name an entity no source has loaded."""
    qs = sorted(set(qids))
    if not qs:
        return set()
    return set(
        conn.execute(text("SELECT DISTINCT qid FROM kg.entity_current WHERE qid = ANY(:q)"), {"q": qs}).scalars()
    )


def _count_scanned(rows: list[dict[str, Any]]) -> int:
    seen: dict[tuple[str, bool], int] = {}
    for r in rows:
        seen[(r["property_pid"], bool(r["is_out"]))] = int(r["n_part"])
    return sum(seen.values())


def _atlas_hint(family: str, other: str, qualifiers: Any, kind: str | None = None) -> dict[str, Any] | None:
    """For ``measured_by``: where the series lives in the atlas (:func:`reva.kg.families.measured_by_target`):
    a catalog concept (``kind: concept``, geography ``iso3``) or an atlas table entity (``kind: table``, with the
    ``provider_geo`` / indicator filters the statement carries). ``None`` otherwise."""
    if family != "measured_by":
        return None
    return measured_by_target(other, qualifiers, kind)


def _unmapped(
    conn: Connection, qid: str, reg: FamilyRegistry, tsql: str, tparams: Mapping[str, Any], origin: str | None
) -> list[dict[str, Any]]:
    """Entity-valued properties around ``qid`` that no family maps (counted on a bounded scan)."""
    rows = conn.execute(
        text(f"""SELECT property_pid, is_out, count(*) AS n FROM (
                   SELECT s.property_pid, (s.subject_qid = :q) AS is_out FROM kg.statement_current s
                    WHERE (s.subject_qid = :q OR s.object_qid = :q) AND s.object_qid IS NOT NULL
                      AND NOT (s.property_pid = ANY(:pids)){tsql}{kq._origin_sql(origin)}
                    LIMIT :scan) x GROUP BY 1, 2 ORDER BY 3 DESC, 1 LIMIT 100"""),
        {"q": qid, "pids": sorted(reg.mappings), "scan": MAX_SCAN, "origin": origin, **tparams},
    ).all()
    props = kq.property_info(conn, {r[0] for r in rows})
    return [
        {
            "pid": pid,
            "direction": "out" if is_out else "in",
            "count": int(n),
            "label_fr": (props.get(pid) or {}).get("label_fr"),
            "label_en": (props.get(pid) or {}).get("label_en"),
            "causal_assertion": pid in CAUSAL_PIDS,
        }
        for pid, is_out, n in rows
    ]


def _literals(conn: Connection, qid: str, origin: str | None) -> list[dict[str, Any]]:
    """Literal-valued outgoing statements (identifiers, quantities, dates, coordinates)."""
    return [
        dict(r)
        for r in conn.execute(
            text(f"""SELECT {kq._ST_SELECT} FROM kg.statement_current s
                      WHERE s.subject_qid = :q AND s.object_qid IS NULL{kq._origin_sql(origin)}
                      ORDER BY s.property_pid, s.id LIMIT :lim"""),
            {"q": qid, "lim": MAX_LITERALS, "origin": origin},
        ).mappings()
    ]


# ---------------------------------------------------------------------------------------------- paths
def annotate_hops(result: dict[str, Any], reg: FamilyRegistry | None = None) -> dict[str, Any]:
    """Add ``family`` ({id, label}) to every hop of a :func:`reva.kg.query.paths` result (``None`` when the hop's
    property is mapped to no family)."""
    try:
        reg = reg or load_registry()
    except FamilyError:  # pragma: no cover - the path stays valid without families
        return result
    for p in result.get("paths", []):
        for h in p["hops"]:
            pid = h["property"]["pid"]
            ef = reg.edge_family(pid, "out" if h["direction"] == "forward" else "in")
            h["family"] = None if ef is None else {"id": ef.family, "label": dict(ef.label), "inverse": ef.inverse}
    return result


def family_paths(
    conn: Connection,
    from_qid: str,
    to_qid: str,
    *,
    families: Iterable[str],
    reg: FamilyRegistry | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Shortest connections whose every hop reads, in path order, as one of ``families``."""
    reg = reg or registry()
    fams = check_families(reg, families)
    assert fams is not None
    allowed = reg.properties_for(fams)
    pids = sorted({p for p, _ in allowed})
    # No mapped property yet (an agent has not loaded its source): the search runs on a property no statement
    # uses, so the result keeps its full shape and says why nothing was found.
    pids = pids or ["reva:no_mapped_property"]

    def edge_ok(e: Mapping[str, Any], start: str) -> bool:
        d = "out" if e["subject_qid"] == start else "in"
        return (e["property_pid"], d) in allowed

    res = kq.paths(conn, from_qid, to_qid, allowed_properties=pids, edge_filter=edge_ok, **kwargs)
    res["families"] = fams
    if not allowed and not res.get("found"):
        res["not_found_reason"] = f"no source property is mapped to the families {fams} yet"
    return annotate_hops(res, reg)


# ---------------------------------------------------------------------------------------------- places
MAX_PLACE_DEPTH = 6


def entity_places(
    conn: Connection, qid: str, *, origin: str | None = None, reg: FamilyRegistry | None = None
) -> dict[str, Any]:
    """Where an entity is: the evidenced ``located_in`` chain upward (ADM2 → ADM1 → country …), coordinates
    (P625) of the entity and of each level, and the ISO3 of the country level when the reviewed registry has it.
    """
    from reva.kg import spacetime as stime

    kq._check_qid(qid)
    kq._check_origin(origin)
    reg = reg or registry()
    up = sorted(p for p, d in reg.properties_for({"located_in"}) if d == "out")
    edges: list[dict[str, Any]] = []
    frontier = {qid}
    seen = {qid}
    for _ in range(MAX_PLACE_DEPTH):
        if not frontier:
            break
        rows = [
            dict(r)
            for r in conn.execute(
                text(f"""SELECT s.subject_qid, s.property_pid, s.object_qid, s.statement_key, s.source_key,
                                s.origin, (s.end_time IS NOT NULL) AS ended FROM kg.statement_current s
                          WHERE s.subject_qid = ANY(:f) AND s.property_pid = ANY(:p) AND s.object_qid IS NOT NULL
                            {kq._origin_sql(origin)}
                          ORDER BY s.id LIMIT 500"""),
                {"f": sorted(frontier), "p": up, "origin": origin},
            ).mappings()
        ]
        edges += rows
        frontier = {r["object_qid"] for r in rows} - seen
        seen |= frontier
    reg_iso3 = kq.country_qid_registry()
    by_iso2 = {i2: reg_iso3.get(i3, f"iso3:{i3}") for i2, i3 in stime.iso2_to_iso3().items()}
    # the chain prefers administrative containment; a generic "location" (P276) is used only when nothing else
    order = [*stime.ADMIN_ORDER, *sorted(set(up) - set(stime.ADMIN_ORDER))]
    chain = stime.place_chain(qid, edges, located_in_pids=order, max_depth=MAX_PLACE_DEPTH, country_by_iso2=by_iso2)
    nodes = [s.qid for s in chain]
    labels = kq.labels_for(conn, nodes)
    kinds = kq.kinds_for(conn, nodes)
    coords: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        text(f"""SELECT s.subject_qid, s.value, s.statement_key, s.source_key FROM kg.statement_current s
                  WHERE s.subject_qid = ANY(:n) AND s.property_pid = 'P625' AND s.value IS NOT NULL
                    {kq._origin_sql(origin)} ORDER BY s.subject_qid, (s.rank = 'preferred') DESC, s.id"""),
        {"n": nodes, "origin": origin},
    ).mappings():
        v = r["value"] or {}
        if r["subject_qid"] in coords or "latitude" not in v or "longitude" not in v:
            continue
        coords[r["subject_qid"]] = {"latitude": v["latitude"], "longitude": v["longitude"],
                                    "statement_key": r["statement_key"], "source_key": r["source_key"]}  # fmt: skip
    iso3_by_qid = {q: i for i, q in reg_iso3.items()}
    out_chain = []
    for s in chain:
        iso3 = iso3_by_qid.get(s.qid) or (s.qid.split(":", 1)[1] if s.qid.startswith("iso3:") else None)
        out_chain.append(
            {
                "entity": kq._ref(s.qid, labels, kinds),
                "level": s.level,
                "via": s.via,
                "statement_key": s.statement_key,
                "basis": s.basis,
                "iso3": iso3,
                "coordinates": coords.get(s.qid),
            }
        )
    return {
        "qid": qid,
        "label": kq._display(qid, labels),
        "chain": out_chain,
        "levels": stime.chain_levels(chain),
        "country_iso3": next((c["iso3"] for c in out_chain if c["iso3"]), None),
        "coordinates": coords.get(qid),
        "located_in_properties": up,
        "note": "Each level is an evidenced located_in statement (or, for an ISO 3166-2 code without a stated "
        "parent, the country its code prefix names). Nothing is inferred from names or geometry here.",
    }
