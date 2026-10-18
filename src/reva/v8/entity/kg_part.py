"""KG sections of the Fiche: identity (labels, ids with authority links, aliases, licence), facts (literal
statements grouped by property, dated values as a series), relations by family (counts + top items), related forms
(e.g. a drug's salt), places (located_in chain, coordinates, display geometry) and dated statements."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Literal

from sqlalchemy import Connection, text

from reva.kg import family_query as fq
from reva.kg import query as kq
from reva.v8.entity import common as C
from reva.v8.entity import profiles as P

PER_FAMILY = 12
DEGREE_CAP = 2_000
MAX_FACT_PROPS = 40
MAX_POINTS = 400
#: Properties whose literal value is the entity's own name or a long text: shown in the header, not as a fact row.
HEADER_PROPS = {"chebi:definition", "mondo:definition"}
#: Families never shown as plain relations (identity is shown in the header).
IDENTITY_FAMILY = "same_as"


# ------------------------------------------------------------------------------------------------ identity
def identity(conn: Connection, qid: str, origin: str | None) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(f"""SELECT e.id, e.kind, e.descriptions, e.aliases, e.sitelinks, e.source_key, e.source_revision,
                            e.retrieved_at, e.recorded_at, e.origin, l.raw_sha256s
                       FROM kg.entity_current e JOIN kg.load l ON l.id = e.load_id
                      WHERE e.qid = :q{kq._origin_sql(origin, "e")} ORDER BY (e.origin = 'live') DESC LIMIT 1"""),
            {"q": qid, "origin": origin},
        )
        .mappings()
        .one_or_none()
    )
    labels: dict[str, str] = {}
    if row is not None:
        for lang, lab in conn.execute(
            text("SELECT lang, label FROM kg.entity_label WHERE entity_id = :i AND NOT is_alias ORDER BY lang"),
            {"i": row["id"]},
        ).all():
            labels[lang] = lab
    deg = degree(conn, qid)
    if row is None and not (deg["statements_out"] or deg["statements_in"]):
        return None
    desc = (row["descriptions"] if row else None) or {}
    aliases = (row["aliases"] if row else None) or {}
    al: list[str] = []
    for lang in ("fr", "en"):
        for a in aliases.get(lang) or []:
            if a not in al and a not in labels.values():
                al.append(str(a))
    community = kq.is_community_edited(row["source_key"] if row else None)
    rights = None
    if row is not None and row["raw_sha256s"]:
        got = fq.rights_for(
            conn, [{"raw_sha256": s, "source_key": row["source_key"]} for s in list(row["raw_sha256s"])[:1]]
        )
        rights = next(iter(got.values()), None) if got else None
        if rights is None and community:
            rights = fq.WIKIDATA_RIGHTS
    label_fr, label_en = labels.get("fr"), labels.get("en")
    return {
        "qid": qid,
        "kg_kind": row["kind"] if row else None,
        "label": label_fr or label_en or next(iter(labels.values()), None) or qid,
        "label_fr": label_fr,
        "label_en": label_en,
        "description": desc.get("fr") or desc.get("en"),
        "aliases": al[:16],
        "sitelinks": row["sitelinks"] if row else None,
        "snapshot_loaded": row is not None,
        "community_edited": community,
        "degree": {
            "statements_out": deg["statements_out"],
            "statements_in": deg["statements_in"],
            "capped_at": DEGREE_CAP,
        },
        "source": {
            "source": row["source_key"],
            "label": C.source_name(row["source_key"]),
            "dataset": None,
            "statement_key": None,
            "series": None,
            "property": None,
            "raw_sha256": (list(row["raw_sha256s"]) or [None])[0],
            "selector": None,
            "retrieved_at": C.iso(row["retrieved_at"]),
            "period": None,
            "licence": C.licence_of(rights),
            "verification": "community_edited_not_verified" if community else "source_reported_not_verified",
            "origin": row["origin"],
            "href": P.authority_url(qid),
        }
        if row is not None
        else None,
    }


def degree(conn: Connection, qid: str, cap: int = DEGREE_CAP) -> dict[str, int]:
    """Statement counts around the record, capped, read on the indexed base table (all recorded versions: an
    approximation shown as such, never used as a value)."""
    out_n, in_n = conn.execute(
        text("""SELECT (SELECT count(*) FROM (SELECT 1 FROM kg.statement WHERE subject_qid = :q LIMIT :cap) a),
                       (SELECT count(*) FROM (SELECT 1 FROM kg.statement WHERE object_qid = :q LIMIT :cap) b)"""),
        {"q": qid, "cap": cap},
    ).one()
    return {"statements_out": int(out_n), "statements_in": int(in_n)}


def own_id(qid: str, kg_kind: str | None, source_key: str | None) -> dict[str, Any]:
    scheme = "Wikidata" if qid.startswith("Q") else qid.split(":", 1)[0].upper()
    return {
        "scheme": scheme,
        "value": qid if qid.startswith("Q") else qid.split(":", 1)[1],
        "url": P.authority_url(qid),
        "source": source_key,
        "property": None,
        "label_fr": "identifiant de la fiche",
        "ref": None,
    }


# ------------------------------------------------------------------------------------------------ families
#: Incoming statements of one property above which its family is read in "hub mode" (a country is the object of
#: thousands of "country" / "located in" statements; scanning them all for a page costs seconds).
HUB_PROPERTY_IN = 500


def families(conn: Connection, qid: str, origin: str | None, per_family: int = PER_FAMILY) -> dict[str, Any]:
    """Relations by family. Hub mode: families with a heavily used incoming property are left out of the full scan
    and read as one page of statements plus an approximate count (``extra_groups``, ``count_approx``)."""
    reg = fq.registry()
    rows = conn.execute(
        text("SELECT property_pid, count(*) FROM kg.statement WHERE object_qid = :q GROUP BY property_pid"), {"q": qid}
    ).all()
    heavy: dict[str, int] = {}
    for pid, n in rows:
        ef = reg.edge_family(pid, "in")
        if ef is not None and int(n) > HUB_PROPERTY_IN:
            heavy[ef.family] = heavy.get(ef.family, 0) + int(n)
    if not heavy:
        res = fq.entity_families(conn, qid, per_family=per_family, origin=origin, include_literals=True)
        res["extra_groups"] = []
        return res
    keep = [f for f in reg.families if f not in heavy]
    res = fq.entity_families(conn, qid, families=keep, per_family=per_family, origin=origin, include_literals=True)
    out_counts: dict[str, int] = dict(
        conn.execute(
            text("SELECT property_pid, count(*) FROM kg.statement WHERE subject_qid = :q GROUP BY property_pid"),
            {"q": qid},
        ).all()
    )
    extra: list[dict[str, Any]] = []
    for fam, n_in in heavy.items():
        page = relation_page(conn, qid, fam, origin, limit=per_family)
        n_out = 0
        for p, c in out_counts.items():
            efo = reg.edge_family(p, "out")
            if efo is not None and efo.family == fam:
                n_out += int(c)
        f = reg.families[fam]
        extra.append(
            {
                "family": fam,
                "label_fr": page["label_fr"],
                "group": f.group,
                "count": n_in + n_out,
                "count_is_lower_bound": False,
                "count_approx": True,
                "items": page["items"],
                "more": True,
                "via": None,
                "properties": [],
            }
        )
    res["extra_groups"] = extra
    return res


def relation_groups(fam: Mapping[str, Any], *, via: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Compact family groups: count (lower bound when the scan was capped), top items with their source chip."""
    out = []
    for g in fam.get("families") or []:
        items = []
        for n in g["neighbours"]:
            st = n["statement"]
            items.append(
                {
                    "ref": C.kg_ref(n["neighbour"]),
                    "reading_fr": n["reading"]["label"].get("fr") or n["reading"]["label"].get("en") or g["family"],
                    "property": st["property"]["pid"],
                    "property_label": st["property"].get("label_fr") or st["property"].get("label_en"),
                    "period": C.statement_period(st),
                    "share": (n.get("share") or {}).get("value") if n.get("share") else None,
                    "share_unit": (n.get("share") or {}).get("unit") if n.get("share") else None,
                    "identity": bool(n.get("identity")),
                    "via": C.kg_ref({"qid": via["qid"], "label": via["label"], "kind": via.get("kind")})
                    if via
                    else None,
                    "source": C.kg_source(st, n.get("rights"), C.statement_period(st)),
                }
            )
        out.append(
            {
                "family": g["family"],
                "label_fr": g["label"].get("fr") or g["label"].get("en"),
                "group": g["group"],
                "count": int(g["count"]),
                "count_is_lower_bound": bool(g["count_is_lower_bound"]),
                "count_approx": False,
                "items": items,
                "more": bool(g["truncated"]),
                "via": C.kg_ref({"qid": via["qid"], "label": via["label"], "kind": via.get("kind")}) if via else None,
                "properties": [
                    {"pid": p["pid"], "label": p.get("label_fr") or p.get("label_en") or p["pid"], "count": p["count"]}
                    for p in g.get("properties") or []
                ],
            }
        )
    return out


def related_forms(
    conn: Connection, qid: str, spec: Mapping[str, Any] | None, origin: str | None
) -> list[dict[str, Any]]:
    """Records the profile declares as forms of this one (ChEMBL: salts whose parent molecule is this record)."""
    if not spec:
        return []
    pid = str(spec["property"])
    col_self, col_other = (
        ("object_qid", "subject_qid") if spec.get("direction", "in") == "in" else ("subject_qid", "object_qid")
    )
    qids = list(
        conn.execute(
            text(f"""SELECT DISTINCT s.{col_other} FROM kg.statement_current s
                      WHERE s.{col_self} = :q AND s.property_pid = :p AND s.{col_other} IS NOT NULL{kq._origin_sql(origin)}
                      LIMIT 5"""),
            {"q": qid, "p": pid, "origin": origin},
        ).scalars()
    )
    labels, kinds = kq.labels_for(conn, qids), kq.kinds_for(conn, qids)
    return [
        {"qid": q, "label": kq._display(q, labels), "kind": kinds.get(q), "relation_fr": spec.get("label_fr")}
        for q in qids
    ]


def first_neighbour(conn: Connection, qid: str, pid: str, direction: str, origin: str | None) -> dict[str, Any] | None:
    res = kq.neighbours(
        conn, qid, direction="in" if direction == "in" else "out", properties=[pid], limit=3, origin=origin
    )
    for e in res["edges"]:
        other = e["subject"] if direction == "in" else e["object"]
        if other:
            return {"ref": C.kg_ref(other), "statement": e, "n": len(res["edges"])}
    return None


# ------------------------------------------------------------------------------------------------ facts
def facts(fam: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Literal statements grouped by property: ``(facts, external ids, coordinates)``. A property with several
    dated numeric values gives a series (points), its latest value leading."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for st in fam.get("literals") or []:
        by[st["property"]["pid"]].append(st)
    out: list[dict[str, Any]] = []
    ids: list[dict[str, Any]] = []
    coords: dict[str, Any] | None = None
    for pid, sts in by.items():
        p = sts[0]["property"]
        label = p.get("label_fr") or p.get("label_en") or pid
        vals = [(st, C.literal_value(pid, st.get("value"))) for st in sts]
        typ = (sts[0].get("value") or {}).get("type") if isinstance(sts[0].get("value"), dict) else None
        if typ == "globe_coordinate" or pid in ("P625", "gppd:coordinates"):
            v = sts[0].get("value") or {}
            try:
                lat, lon = float(str(v.get("lat", v.get("latitude")))), float(str(v.get("lon", v.get("longitude"))))
                coords = coords or {"lat": lat, "lon": lon, "source": C.kg_source(sts[0], sts[0].get("rights"))}
            except (TypeError, ValueError):
                pass
            continue
        if typ == "external_id":
            for st, lv in vals:
                ids.append(
                    {
                        "scheme": _scheme(pid, lv["text"], label),
                        "value": lv["text"].split(":", 1)[1]
                        if pid == "mondo:xref" and ":" in lv["text"]
                        else lv["text"],
                        "url": lv["url"],
                        "source": (st.get("provenance") or {}).get("source_key"),
                        "property": pid,
                        "label_fr": label,
                        "ref": None,
                    }
                )
            continue
        dated = [(C.statement_x(st), st, lv) for st, lv in vals]
        numeric = [(x, st, lv) for x, st, lv in dated if lv["value"] is not None]
        has_dates = sum(1 for x, _, _ in dated if x is not None)
        # latest dated value leads; undated: the first (preferred rank first)
        ordered = sorted(dated, key=lambda t: (t[0] is not None, t[0] or 0.0), reverse=True)
        if not has_dates:
            ordered = sorted(dated, key=lambda t: (t[1].get("provenance") or {}).get("rank") != "preferred")
        _x, st, lv = ordered[0]
        points = []
        if len(numeric) >= 2 and has_dates >= 2:
            pts: dict[float, dict[str, Any]] = {}
            for px, pst, plv in numeric:
                if px is None:
                    continue
                pts.setdefault(
                    px, {"t": C.statement_period(pst) or str(int(px)), "x": px, "v": plv["value"], "projection": False}
                )
            points = sorted(pts.values(), key=lambda p: p["x"])[-MAX_POINTS:]
        out.append(
            {
                "property": pid,
                "label_fr": label,
                "value": lv["value"],
                "value_text": lv["text"],
                "unit": lv["unit"],
                "value_lo": lv["lo"],
                "value_hi": lv["hi"],
                "url": lv["url"],
                "period": C.statement_period(st),
                "count": len(sts),
                "points": points,
                "long_text": pid in HEADER_PROPS or len(lv["text"]) > 160,
                "source": C.kg_source(st, st.get("rights"), C.statement_period(st)),
                "others": [
                    {"value_text": olv["text"], "period": C.statement_period(ost)}
                    for _, ost, olv in ordered[1:6]
                    if not points
                ],
            }
        )
    out.sort(key=lambda f: (f["long_text"], -len(f["points"]), f["label_fr"].lower()))
    return out[:MAX_FACT_PROPS], ids, coords


def _scheme(pid: str, value: str, label: str) -> str:
    if pid == "mondo:xref" and ":" in value:
        return value.split(":", 1)[0]
    short = {
        "unichem:drugbank_id": "DrugBank",
        "eml:drugbank_id": "DrugBank",
        "hgnc:ensembl_gene_id": "Ensembl",
        "hgnc:ncbi_gene_id": "NCBI Gene",
        "chembl:atc_code": "ATC",
        "eml:atc_code": "ATC",
        "gleif:opencorporates_id": "OpenCorporates",
        "gppd:wepp_id": "WEPP (Platts)",
        "P298": "ISO 3166-1 alpha-3",
        "P297": "ISO 3166-1 alpha-2",
    }
    return short.get(pid, label.split("(")[0].strip()[:40] or pid)


def identity_ids(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identity neighbours (same_as): other records that an item of a source declares to be the same thing."""
    out = []
    for g in groups:
        if g["family"] != IDENTITY_FAMILY:
            continue
        for it in g["items"]:
            q = it["ref"]["id"]
            out.append(
                {
                    "scheme": "Wikidata" if q.startswith("Q") else q.split(":", 1)[0].upper(),
                    "value": q if q.startswith("Q") else q.split(":", 1)[1],
                    "url": P.authority_url(q),
                    "source": it["source"]["source"],
                    "property": it["property"],
                    "label_fr": "même entité selon " + it["source"]["label"],
                    "ref": it["ref"],
                }
            )
    return out


# ------------------------------------------------------------------------------------------------ places & time
def places(conn: Connection, qid: str, origin: str | None) -> dict[str, Any]:
    res = fq.entity_places(conn, qid, origin=origin)
    levels_fr = {0: "pays", 1: "région (ADM1)", 2: "département / district (ADM2)"}
    chain = []
    for c in res["chain"][1:]:
        e = c["entity"]
        chain.append(
            {
                "ref": C.kg_ref(e),
                "level": c["level"],
                "level_fr": levels_fr.get(c["level"]) if c["level"] is not None else None,
                "iso3": c["iso3"],
                "via": c["via"],
                "basis": c["basis"],
                "lat": (c.get("coordinates") or {}).get("latitude"),
                "lon": (c.get("coordinates") or {}).get("longitude"),
            }
        )
    co = res.get("coordinates")
    return {
        "chain": chain,
        "country_iso3": res.get("country_iso3"),
        "point": {"lat": float(co["latitude"]), "lon": float(co["longitude"]), "source_key": co["source_key"]}
        if co
        else None,
    }


def country_geometry(conn: Connection, iso3: str) -> dict[str, Any] | None:
    """Simplified display outline of a country (Natural Earth display layer; display only, not a boundary claim)."""
    try:
        row = (
            conn.execute(
                text("""SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(g.geometry_4326, 0.08), 2)::jsonb AS geom,
                               g.feature_key
                          FROM world.display_geometry g
                         WHERE split_part(g.feature_key, ':', 1) = :k
                         ORDER BY g.recorded_at DESC LIMIT 1"""),
                {"k": {"SSD": "SDS", "XKX": "KOS", "ESH": "SAH", "PSE": "PSX"}.get(iso3, iso3)},
            )
            .mappings()
            .one_or_none()
        )
    except Exception:  # pragma: no cover - a DB without the world schema
        return None
    if row is None or not row["geom"]:
        return None
    return {
        "geojson": row["geom"],
        "feature_key": row["feature_key"],
        "note_fr": "Contour d’affichage (Natural Earth), pas une frontière revendiquée.",
    }


def dated_statements(
    conn: Connection, qid: str, origin: str | None, skip_props: set[str], limit: int = 40
) -> list[dict[str, Any]]:
    res = kq.timeline(conn, qid, include_incoming=False, limit=300, origin=origin)
    out = []
    for e in res["events"]:
        pid = e["property"]["pid"]
        if pid in skip_props:
            continue
        lv = C.literal_value(pid, e.get("value")) if e.get("value") is not None else None
        out.append(
            {
                "date": C.statement_period(e) or (lv["text"] if lv else None) or (e.get("sort_date") or "")[:10],
                "x": C.statement_x(e) or C.time_x(e.get("value") if isinstance(e.get("value"), dict) else None),
                "label_fr": e["property"].get("label_fr") or e["property"].get("label_en") or pid,
                "property": pid,
                "ref": C.kg_ref(e["object"]) if e.get("object") else None,
                "value_text": lv["text"] if lv and e.get("object") is None else None,
                "source": C.kg_source(e, None, C.statement_period(e)),
            }
        )
    out.sort(key=lambda r: r["x"] if r["x"] is not None else -1e9)
    return out[-limit:]


# ------------------------------------------------------------------------------------------------ relation pages
def relation_page(
    conn: Connection, qid: str, family: str, origin: str | None, *, limit: int = 50, after_id: int | None = None
) -> dict[str, Any]:
    """One page of a family's relations (the bundle shows the first items; this pages the rest)."""
    reg = fq.registry()
    if family not in reg.families:
        raise ValueError(f"unknown relation family {family}")
    pids = sorted(reg.pids_for([family]))
    res = kq.neighbours(conn, qid, direction="both", properties=pids, limit=limit, after_id=after_id, origin=origin)
    rows = [
        {"raw_sha256": e["provenance"]["raw_sha256"], "source_key": e["provenance"]["source_key"]} for e in res["edges"]
    ]
    rights = fq.rights_for(conn, rows)
    items = []
    for e in res["edges"]:
        d: Literal["out", "in"] = "out" if e["subject"]["qid"] == qid else "in"
        ef = reg.edge_family(e["property"]["pid"], d)
        if ef is None or ef.family != family:
            continue
        other = e["object"] if d == "out" else e["subject"]
        if other is None:
            continue
        r = rights.get(str(e["provenance"]["raw_sha256"])) if e["provenance"]["raw_sha256"] else None
        if kq.is_community_edited(e["provenance"]["source_key"]):
            r = fq.WIKIDATA_RIGHTS
        items.append(
            {
                "ref": C.kg_ref(other),
                "reading_fr": ef.label.get("fr") or ef.label.get("en") or family,
                "property": e["property"]["pid"],
                "property_label": e["property"].get("label_fr") or e["property"].get("label_en"),
                "period": C.statement_period(e),
                "share": None,
                "share_unit": None,
                "identity": family == IDENTITY_FAMILY,
                "via": None,
                "source": C.kg_source(e, r, C.statement_period(e)),
            }
        )
    fam = reg.families[family]
    return {
        "family": family,
        "label_fr": fam.label.get("fr") or fam.label.get("en") or family,
        "items": items,
        "next_after_id": res["next_after_id"],
    }
