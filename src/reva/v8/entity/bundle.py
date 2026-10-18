"""The Fiche bundle: everything one entity page shows, in ONE call (``GET /v1/v8/entity/{kind}/{id}``).

Sections: identity, headline (3–6 numbers chosen per kind), facts, relations by family (+ related forms), series
(atlas), space & time, causal claims in/out, effect estimates, forecasts, "ce qui manque" (expected facts/series
that are absent, with the reason when known), sources. A section that fails is reported in ``meta.errors`` and as a
missing item; it never fails the page. Results are cached per process, keyed by the data version (KG / causal /
effect loads, forecast registry, atlas snapshot) so a new load is visible at once.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

from reva.v8.entity import atlas_part as A
from reva.v8.entity import claims_part as CL
from reva.v8.entity import common as C
from reva.v8.entity import forecast_part as F
from reva.v8.entity import kg_part as K
from reva.v8.entity import profiles as P
from reva.v8.entity.resolve import Resolved, resolve

log = logging.getLogger(__name__)
CACHE_SIZE = 256
CACHE_TTL_S = 900.0
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="v8fiche")
_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()

ConnFactory = Callable[[], AbstractContextManager[Connection]]

VERSION_SQL = """SELECT (SELECT max(id) FROM kg.load), (SELECT max(id) FROM kg.causal_load),
                        (SELECT max(id) FROM kg.effect_load), (SELECT max(recorded_at) FROM model.evaluation)"""


def data_version(conn: Connection) -> tuple[Any, ...]:
    try:
        return tuple(str(x) for x in conn.execute(text(VERSION_SQL)).one())
    except Exception:  # pragma: no cover - a schema without one of the tables
        return ("?",)


def cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _RES.clear()
        _Q.clear()


ATLAS_TTL_S = 120.0
_Q: dict[str | None, tuple[float, Any]] = {}
_RES: OrderedDict[tuple[str, str], tuple[float, Resolved]] = OrderedDict()


def _atlas(factory: Callable[[], Any], origin: str | None) -> Any:
    """The atlas query of the latest snapshot, re-resolved at most every two minutes (the manifest read is costly)."""
    hit = _Q.get(origin)
    if hit and time.monotonic() - hit[0] < ATLAS_TTL_S:
        return hit[1]
    q = factory()
    _Q[origin] = (time.monotonic(), q)
    return q


def _resolve_cached(conn: Connection, kind: str, ident: str) -> Resolved:
    key = (kind.strip().lower(), ident.strip())
    with _CACHE_LOCK:
        hit = _RES.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]
    res = resolve(conn, kind, ident)
    with _CACHE_LOCK:
        _RES[key] = (time.monotonic(), res)
        while len(_RES) > 4 * CACHE_SIZE:
            _RES.popitem(last=False)
    return res


class Section:
    """Times a section and turns an exception into a reported error (the page still renders). With ``conn``, the
    section runs in a SAVEPOINT so a failed query (e.g. a statement timeout) does not abort the next sections."""

    def __init__(self, meta: dict[str, Any], name: str, conn: Connection | None = None) -> None:
        self.meta, self.name, self.conn = meta, name, conn
        self.sp: Any = None

    def __enter__(self) -> Section:
        self.t0 = time.perf_counter()
        if self.conn is not None:
            self.sp = self.conn.begin_nested()
        return self

    def __exit__(self, et: Any, ev: Any, tb: Any) -> bool:
        self.meta["timings_ms"][self.name] = round((time.perf_counter() - self.t0) * 1000, 1)
        if self.sp is not None:
            try:
                (self.sp.rollback if ev is not None else self.sp.commit)()
            except Exception:  # pragma: no cover - the outer transaction is gone too
                log.warning("v8 fiche savepoint of %s could not be closed", self.name)
        if ev is not None:
            log.warning("v8 fiche section %s failed: %s", self.name, ev, exc_info=(et, ev, tb))
            self.meta["errors"].append({"section": self.name, "error": f"{type(ev).__name__}: {str(ev)[:300]}"})
            return True
        return False


def empty_bundle(res: Resolved) -> dict[str, Any]:
    return {
        "canonical": {"kind": res.kind, "id": res.id},
        "resolved_via": res.via,
        "identity": None,
        "headline": [],
        "facts": [],
        "relations": [],
        "related_forms": [],
        "series": [],
        "tops": [],
        "space": {"chain": [], "country_iso3": res.iso3, "point": None, "geometry": None},
        "time": [],
        "claims": {"incoming": [], "outgoing": [], "n_in": 0, "n_out": 0, "note_fr": CL.CLAIM_NOTE_FR},
        "effects": {"items": [], "n": 0, "n_country_scope": None, "note_fr": CL.EFFECT_NOTE_FR},
        "forecasts": {
            "questions": [],
            "n_total": 0,
            "n_pending": 0,
            "n_resolved": 0,
            "families": [],
            "note_fr": F.NOTE_FR,
        },
        "missing": [],
        "sources": [],
        "notes": list(res.notes),
        "meta": {
            "timings_ms": {},
            "errors": [],
            "cached": False,
            "generated_at": None,
            "atlas_snapshot": None,
            "atlas_cutoff": None,
            "data_version": None,
            "warming": [],
        },
    }


def build(
    conn_factory: ConnFactory,
    atlas_factory: Callable[[], Any],
    store_factory: Callable[[], Any],
    kind: str,
    ident: str,
    *,
    origin: str | None,
    use_cache: bool = True,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    with conn_factory() as conn:
        res = _resolve_cached(conn, kind, ident) if use_cache else resolve(conn, kind, ident)
        version = data_version(conn)
    key = (res.kind, res.id, origin, version)
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
                _CACHE.move_to_end(key)
                out = {
                    **hit[1],
                    "meta": {
                        **hit[1]["meta"],
                        "cached": True,
                        "served_ms": round((time.perf_counter() - t0) * 1000, 1),
                    },
                }
                return out
    b = empty_bundle(res)
    meta = b["meta"]
    meta["data_version"] = list(version)
    if res.special:
        from reva.v8.entity import special

        with Section(meta, "special"), conn_factory() as conn:
            special.fill(b, conn, store_factory, res, origin)
    else:
        _fill_entity(b, res, conn_factory, atlas_factory, store_factory, origin)
    b["sources"] = sources_summary(b)
    meta["generated_at"] = datetime.now(UTC).isoformat()
    meta["timings_ms"]["total"] = round((time.perf_counter() - t0) * 1000, 1)
    if use_cache and not meta["errors"] and not meta["warming"]:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), b)
            while len(_CACHE) > CACHE_SIZE:
                _CACHE.popitem(last=False)
    return b


def _fill_entity(
    b: dict[str, Any],
    res: Resolved,
    conn_factory: ConnFactory,
    atlas_factory: Callable[[], Any],
    store_factory: Callable[[], Any],
    origin: str | None,
) -> None:
    meta = b["meta"]
    prof = P.profile_for(res.kind) if not res.anchor else {}
    anchor = res.anchor
    store = None
    with Section(meta, "store"):
        store = store_factory()
    # ---- atlas work runs beside the KG work (DuckDB, no Postgres connection held)
    q = None
    with Section(meta, "atlas_open"):
        q = _atlas(atlas_factory, origin)
        meta["atlas_snapshot"] = q.snapshot.sha256
        meta["atlas_cutoff"] = q.snapshot.cutoff.isoformat()
    recorded: dict[str, Any] = {}
    if q is not None and (res.iso3 or anchor):
        with Section(meta, "atlas_recorded"), conn_factory() as conn:
            recorded = A.recorded_tables(conn)
    fut: Future[dict[str, Any]] | None = None
    if q is not None and (res.iso3 or anchor):
        fut = _POOL.submit(_atlas_work, q, res, anchor, recorded, store)
    fam: dict[str, Any] = {"families": [], "literals": [], "unmapped": []}
    with conn_factory() as conn:
        if res.qid:
            with Section(meta, "identity", conn):
                ident = K.identity(conn, res.qid, origin)
                b["identity"] = _identity_out(res, ident, prof)
            with Section(meta, "families", conn):
                fam = K.families(conn, res.qid, origin)
                b["relations"] = [
                    g for g in K.relation_groups(fam) + fam.get("extra_groups", []) if g["family"] != K.IDENTITY_FAMILY
                ]
                facts, ids, coords = K.facts(fam)
                b["facts"] = facts
                if b["identity"] is not None:
                    b["identity"]["ids"] += ids + K.identity_ids(K.relation_groups(fam))
                    defs = [f for f in facts if f["long_text"] and "definition" in f["property"]]
                    if defs and not b["identity"].get("definition"):
                        b["identity"]["definition"] = defs[0]["value_text"]
                        b["identity"]["definition_source"] = defs[0]["source"]
                if coords:
                    b["space"]["point"] = {
                        "lat": coords["lat"],
                        "lon": coords["lon"],
                        "source_key": coords["source"]["source"],
                    }
            with Section(meta, "related_forms", conn):
                forms = K.related_forms(conn, res.qid, prof.get("related_forms"), origin)
                for f in forms:
                    ffam = K.families(conn, f["qid"], origin, per_family=8)
                    groups = [
                        g for g in K.relation_groups(ffam, via=f) if g["family"] not in (K.IDENTITY_FAMILY, "context")
                    ]
                    b["related_forms"].append(
                        {"ref": C.kg_ref(f), "relation_fr": f.get("relation_fr"), "relations": groups}
                    )
            with Section(meta, "space", conn):
                pl = K.places(conn, res.qid, origin)
                b["space"]["chain"] = pl["chain"]
                b["space"]["country_iso3"] = res.iso3 or pl["country_iso3"]
                if pl["point"] and not b["space"]["point"]:
                    b["space"]["point"] = pl["point"]
            with Section(meta, "geometry", conn):
                iso = res.iso3 or b["space"]["country_iso3"]
                if iso:
                    b["space"]["geometry"] = K.country_geometry(conn, iso)
            with Section(meta, "time", conn):
                skip = {f["property"] for f in b["facts"] if f["points"]}
                b["time"] = K.dated_statements(conn, res.qid, origin, skip)
            with Section(meta, "causal", conn):
                b["claims"] = CL.causal(conn, res.qid, origin)
        elif anchor:
            b["identity"] = _anchor_identity(res, anchor)
            with Section(meta, "anchor_kg", conn):
                b["relations"] = _anchor_relations(conn, anchor, origin)
        else:  # a country the KG does not hold: atlas only
            b["identity"] = _identity_out(res, None, prof)
        with Section(meta, "effects", conn):
            eff_ids = [str(x) for x in anchor.get("effect_variables") or []] if anchor else []
            country = res.iso3 if prof.get("effects") == "country" else None
            if eff_ids or country:
                b["effects"] = CL.effects(conn, origin=origin, ids=eff_ids, country=country)
        with Section(meta, "forecasts", conn):
            try:
                if store is not None and res.iso3:
                    b["forecasts"] = F.forecasts(conn, store, origin, geo=res.iso3)
                elif store is not None and anchor and anchor.get("forecast_families"):
                    b["forecasts"] = F.forecasts(conn, store, origin, families=anchor["forecast_families"])
                elif store is not None:
                    F.warm(conn, store, origin)
            except F.IndexWarming as w:
                meta["warming"].append("forecasts")
                b["missing"].append({"section": "forecasts", "what_fr": "prévisions du registre", "reason_code": "warming",
                                     "reason_fr": f"index du registre des prévisions en construction (premier accès de ce processus : {w}) ; rechargez dans une minute"})  # fmt: skip
        if res.qid and q is not None:
            with Section(meta, "measured_by", conn):
                if any(g["family"] == "measured_by" for g in fam.get("families") or []):
                    rec = A.recorded_tables(conn)
                    b["series"] += A.measured_by(q, b["relations"], fam, rec)
        with Section(meta, "missing_facts", conn):
            b["missing"] += _missing_facts(conn, b, prof)
    if fut is not None:
        with Section(meta, "atlas"):
            ar = fut.result(timeout=60)
            meta["timings_ms"].update(ar.pop("timings", {}))
            b["series"] = ar.get("series", []) + b["series"]
            b["tops"] = ar.get("tops", [])
            b["missing"] += ar.get("missing", [])
            for e in ar.get("errors", []):
                meta["errors"].append(e)
    with Section(meta, "headline"):
        b["headline"] = headline(b, res, prof, anchor)
    _series_from_facts(b)
    _expected_series(b, prof)
    for e in meta["errors"]:
        b["missing"].append({"section": e["section"], "what_fr": f"section « {e['section']} »", "reason_code": "error",
                             "reason_fr": "erreur de lecture côté serveur : " + e["error"][:160]})  # fmt: skip


def _atlas_work(
    q: Any, res: Resolved, anchor: Mapping[str, Any] | None, recorded: Mapping[str, Any], store: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {"series": [], "tops": [], "missing": [], "errors": [], "timings": {}}
    t0 = time.perf_counter()
    try:
        if res.iso3:
            cs = A.country_series(q, res.iso3)
            out["series"] = cs["cards"]
            for m in cs["missing"]:
                out["missing"].append({"section": "series", "what_fr": _concept_label(m["key"]), "reason_code": m["reason_code"],
                                       "reason_fr": m["reason_fr"]})  # fmt: skip
        if anchor:
            out["series"] = A.anchor_series(q, recorded, anchor, store)
            for c in out["series"]:
                if c["missing"]:
                    out["missing"].append({"section": "series", "what_fr": c["label_fr"], "reason_code": c["missing"]["code"],
                                           "reason_fr": c["missing"]["reason_fr"]})  # fmt: skip
            t1 = time.perf_counter()
            out["tops"] = A.anchor_tops(q, anchor)
            out["timings"]["atlas_tops"] = round((time.perf_counter() - t1) * 1000, 1)
    except Exception as e:  # reported, never fatal
        log.warning("v8 fiche atlas part failed: %s", e, exc_info=True)
        out["errors"].append({"section": "atlas", "error": f"{type(e).__name__}: {str(e)[:300]}"})
    out["timings"]["atlas_work"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def _concept_label(cid: str) -> str:
    try:
        from reva.atlas.source_order import cached_headlines

        c = cached_headlines().concepts.get(cid)
        if c is not None:
            return str(c.labels.get("fr") or c.labels.get("en") or cid)
    except Exception:  # pragma: no cover
        log.debug("headline registry unavailable for %s", cid)
    return cid


# ------------------------------------------------------------------------------------------------ identity
def _identity_out(res: Resolved, ident: Mapping[str, Any] | None, prof: Mapping[str, Any]) -> dict[str, Any]:
    kg_kind = (ident or {}).get("kg_kind") or res.kg_kind
    ids = []
    if res.qid:
        ids.append(K.own_id(res.qid, kg_kind, ((ident or {}).get("source") or {}).get("source")))
    if res.iso3:
        ids.append({"scheme": "ISO 3166-1 alpha-3", "value": res.iso3, "url": None, "source": "registry/entities/country_qids.json",
                    "property": None, "label_fr": "clé de l’atlas", "ref": None})  # fmt: skip
    label = (ident or {}).get("label") or res.iso3 or res.id
    if res.iso3 and not ident:
        from reva.api.routes.atlas import _country

        try:
            label = _country(res.iso3)[1] or label
        except Exception:  # pragma: no cover
            log.debug("no atlas label for %s", res.iso3)
    return {
        "qid": res.qid,
        "kind": res.kind,
        "kg_kind": kg_kind,
        "kind_label_fr": P.kind_label_fr(kg_kind or res.kind),
        "label": label,
        "label_fr": (ident or {}).get("label_fr"),
        "label_en": (ident or {}).get("label_en"),
        "description": (ident or {}).get("description"),
        "definition": None,
        "definition_source": None,
        "aliases": (ident or {}).get("aliases") or [],
        "ids": ids,
        "anchor": False,
        "anchor_note": None,
        "source": (ident or {}).get("source"),
        "sitelinks": (ident or {}).get("sitelinks"),
        "snapshot_loaded": bool((ident or {}).get("snapshot_loaded")),
        "community_edited": bool((ident or {}).get("community_edited")),
        "degree": (ident or {}).get("degree") or {"statements_out": 0, "statements_in": 0, "capped_at": K.DEGREE_CAP},
        "iso3": res.iso3,
    }


def _anchor_identity(res: Resolved, anchor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "qid": None,
        "kind": res.kind,
        "kg_kind": None,
        "kind_label_fr": P.profile_for(res.kind).get("label_fr") or res.kind,
        "label": anchor.get("label_fr") or res.id,
        "label_fr": anchor.get("label_fr"),
        "label_en": anchor.get("label_en"),
        "description": anchor.get("description_fr"),
        "definition": None,
        "definition_source": None,
        "aliases": [a for a in anchor.get("aliases") or [] if a != anchor.get("label_fr")],
        "ids": [
            {
                "scheme": "REVA (ancre)",
                "value": f"{res.kind}/{res.id}",
                "url": None,
                "source": "src/reva/v8/entity/profiles.yaml",
                "property": None,
                "label_fr": "page d’ancrage REVA, pas un enregistrement de source",
                "ref": None,
            }
        ],
        "anchor": True,
        "anchor_note": "Page d’ancrage REVA : ce regroupement est fait par REVA ; chaque valeur garde sa propre source.",
        "source": None,
        "sitelinks": None,
        "snapshot_loaded": False,
        "community_edited": False,
        "degree": {"statements_out": 0, "statements_in": 0, "capped_at": K.DEGREE_CAP},
        "iso3": None,
    }


def _anchor_relations(conn: Connection, anchor: Mapping[str, Any], origin: str | None) -> list[dict[str, Any]]:
    """The KG records an anchor names (nomenclature codes, energy-source records), as one 'codes' group."""
    from reva.kg import query as kq

    qids = [str(x) for x in anchor.get("kg") or []]
    labels, kinds = kq.labels_for(conn, qids), kq.kinds_for(conn, qids)
    present = [q for q in qids if q in kinds or q in labels]
    items = [
        {
            "ref": C.ref(kinds.get(q), q, kq._display(q, labels)),
            "reading_fr": P.kind_label_fr(kinds.get(q)),
            "property": "reva:anchor",
            "property_label": "déclaré par la page d’ancrage",
            "period": None,
            "share": None,
            "share_unit": None,
            "identity": False,
            "via": None,
            "source": {
                "source": "reva.v8.anchor",
                "label": "REVA (ancre)",
                "dataset": None,
                "statement_key": None,
                "series": None,
                "property": None,
                "raw_sha256": None,
                "selector": None,
                "retrieved_at": None,
                "period": None,
                "licence": None,
                "verification": "reva_grouping",
                "origin": None,
                "href": None,
            },
        }
        for q in present
    ]
    if not items:
        return []
    return [{"family": "context", "label_fr": "Codes de nomenclature et enregistrements liés", "group": "classification",
             "count": len(items), "count_is_lower_bound": False, "count_approx": False, "items": items, "more": False, "via": None,
             "properties": []}]  # fmt: skip


# ------------------------------------------------------------------------------------------------ headline
def _stat(key: str, label: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"key": key, "label_fr": label, "value": None, "value_text": None, "unit": None, "period": None, "kind": "observed",
            "ref": None, "source": None, "missing": None, "points": [], "sub": None, "link": None}  # fmt: skip
    base.update(kw)
    return base


def headline(
    b: Mapping[str, Any], res: Resolved, prof: Mapping[str, Any], anchor: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    specs = list((anchor or {}).get("headline") or prof.get("headline") or [])
    facts = {f["property"]: f for f in b["facts"]}
    ids = (b.get("identity") or {}).get("ids") or []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for g in b["relations"]:
        groups.setdefault(g["family"], []).append(g)
    for f in b["related_forms"]:
        for g in f["relations"]:
            groups.setdefault("related:" + g["family"], []).append(g)
    series = {s["key"]: s for s in b["series"]}
    tops = {t["key"]: t for t in b["tops"]}
    out: list[dict[str, Any]] = []
    for sp in specs:
        label = str(sp.get("label_fr"))
        if "atlas_headline" in sp:
            k = sp["atlas_headline"]
            s = series.get(k)
            if s is None:
                out.append(
                    _stat(
                        k, label, missing=_missing_for(b, k, "aucune source déclarée ne publie de valeur pour ce pays")
                    )
                )
            else:
                out.append(_stat(k, label, value=s["value"], unit=s["unit"], period=s["period"], kind=s["kind"], source=s["source"],
                                 points=s["points"][-30:], link=s["link"]))  # fmt: skip
        elif "anchor_series" in sp:
            k = sp["anchor_series"]
            s = series.get(k)
            if s is None or s["value"] is None:
                out.append(
                    _stat(
                        k,
                        label,
                        unit=(s or {}).get("unit"),
                        missing=(s or {}).get("missing") or {"code": "absent", "reason_fr": "série non déclarée"},
                    )
                )
            else:
                out.append(_stat(k, label, value=s["value"], unit=s["unit"], period=s["period"], kind=s["kind"], source=s["source"],
                                 points=s["points"][-60:], link=s["link"],
                                 sub="lu hors instantané atlas (table publiée plus récente)" if s.get("out_of_snapshot") else None))  # fmt: skip
        elif "anchor_top" in sp:
            t = tops.get(sp["anchor_top"])
            if not t or not t["items"]:
                out.append(
                    _stat(
                        sp["anchor_top"],
                        label,
                        missing={"code": "no_value", "reason_fr": "aucune année avec au moins 20 pays renseignés"},
                    )
                )
            else:
                first = t["items"][0]
                out.append(_stat(sp["anchor_top"], label, value=first["value"], unit=t["unit"], period=str(t["year"]),
                                 ref=C.ref("country", first["iso3"], first["iso3"]), source=first["source"],
                                 sub=" · ".join(i["iso3"] for i in t["items"][1:4])))  # fmt: skip
        elif "literal" in sp:
            pid = sp["literal"]
            if sp.get("match"):
                scheme = str(sp["match"]).rstrip(":")
                hit = next((i for i in ids if i.get("property") == pid and i.get("scheme") == scheme), None)
                if hit:
                    out.append(_stat(pid + "|" + scheme, label, value_text=hit["value"], kind="literal", link={"url": hit["url"]} if hit["url"] else None,
                                     source={"source": hit["source"], "label": C.source_name(hit["source"])} | _blank_source()))  # fmt: skip
                else:
                    out.append(
                        _stat(
                            pid + "|" + scheme,
                            label,
                            missing={
                                "code": "not_reported",
                                "reason_fr": "la source ne publie pas de correspondance " + scheme,
                            },
                        )
                    )
                continue
            f = facts.get(pid)
            idh = next((i for i in ids if i.get("property") == pid), None)
            if f is None and idh is not None:
                out.append(_stat(pid, label, value_text=idh["value"], kind="literal", link={"url": idh["url"]} if idh["url"] else None,
                                 source={"source": idh["source"], "label": C.source_name(idh["source"])} | _blank_source()))  # fmt: skip
                continue
            if f is None:
                out.append(
                    _stat(
                        pid,
                        label,
                        missing={
                            "code": "not_reported",
                            "reason_fr": "aucune source chargée ne publie cette valeur pour cette entité",
                        },
                    )
                )
                continue
            numeric = f["value"] is not None
            out.append(
                _stat(
                    pid,
                    label,
                    value=f["value"] if numeric else None,
                    value_text=None if numeric else f["value_text"],
                    unit=f["unit"],
                    period=f["period"],
                    kind="observed" if numeric else "literal",
                    source=f["source"],
                    points=f["points"][-40:],
                    sub=_bounds(f) or (f"{f['count']} valeurs datées" if f["points"] else None),
                    link={"url": f["url"]} if f.get("url") else None,
                )
            )
        elif "family_count" in sp:
            fam = sp["family_count"]
            gs = list(groups.get(fam, []))
            if sp.get("include_related"):
                gs += groups.get("related:" + fam, [])
            if not gs:
                out.append(
                    _stat(
                        fam,
                        label,
                        missing={
                            "code": "not_reported",
                            "reason_fr": "aucune relation de ce type dans les données chargées",
                        },
                    )
                )
                continue
            n = sum(int(g["count"]) for g in gs)
            lower = any(g["count_is_lower_bound"] for g in gs)
            names = []
            for g in gs:
                for it in g["items"]:
                    if it["ref"]["label"] not in names:
                        names.append(it["ref"]["label"])
            via = next((g["via"] for g in gs if g.get("via")), None)
            out.append(_stat(fam, label, value=str(n), kind="count", sub=", ".join(names[:3]) + ("…" if len(names) > 3 else ""),
                             unit="au moins" if lower else None,
                             link={"section": "relations", "family": fam},
                             ref=via))  # fmt: skip
        elif "relation" in sp:
            pid = sp["relation"]
            hit = _relation_hit(b, pid)
            if hit is None:
                reason = "aucune source chargée ne déclare cette relation pour cette entité"
                lit = sp.get("missing_from_literal")
                if lit and lit in facts:
                    reason = f"exception déclarée à la source : {facts[lit]['value_text']}"
                out.append(_stat(pid, label, missing={"code": "not_reported", "reason_fr": reason}))
            else:
                out.append(
                    _stat(
                        pid,
                        label,
                        value_text=hit["ref"]["label"],
                        kind="relation",
                        ref=hit["ref"],
                        source=hit["source"],
                        period=hit["period"],
                    )
                )
    if not specs:
        out = _auto_headline(b)
    return out[:6]


def _blank_source() -> dict[str, Any]:
    return {"dataset": None, "statement_key": None, "series": None, "property": None, "raw_sha256": None, "selector": None,
            "retrieved_at": None, "period": None, "licence": None, "verification": None, "origin": None, "href": None}  # fmt: skip


def _bounds(f: Mapping[str, Any]) -> str | None:
    if f.get("value_lo") and f.get("value_hi"):
        return f"intervalle de la source : {f['value_lo']} – {f['value_hi']}"
    return None


def _relation_hit(b: Mapping[str, Any], pid: str) -> dict[str, Any] | None:
    for g in b["relations"]:
        for it in g["items"]:
            if it["property"] == pid and not it["identity"]:
                return dict(it)
    return None


def _auto_headline(b: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in b["facts"]:
        if f["long_text"] or len(out) >= 3:
            continue
        numeric = f["value"] is not None
        out.append(_stat(f["property"], f["label_fr"], value=f["value"] if numeric else None, value_text=None if numeric else f["value_text"],
                         unit=f["unit"], period=f["period"], kind="observed" if numeric else "literal", source=f["source"],
                         points=f["points"][-40:]))  # fmt: skip
    for g in sorted(b["relations"], key=lambda g: -int(g["count"]))[:2]:
        out.append(_stat(g["family"], g["label_fr"], value=str(g["count"]), kind="count", unit="au moins" if g["count_is_lower_bound"] else None,
                         sub=", ".join(i["ref"]["label"] for i in g["items"][:3]), link={"section": "relations", "family": g["family"]}))  # fmt: skip
    return out


def _missing_for(b: Mapping[str, Any], key: str, default: str) -> dict[str, Any]:
    for m in b["missing"]:
        if m.get("key") == key or m.get("what_fr") == _concept_label(key):
            return {"code": m["reason_code"], "reason_fr": m["reason_fr"]}
    return {"code": "not_reported", "reason_fr": default}


def _series_from_facts(b: dict[str, Any]) -> None:
    """Dated numeric KG facts (UCDP deaths per year, GPPD generation per year…) are series too."""
    have = {s["key"] for s in b["series"]}
    for f in b["facts"]:
        if len(f["points"]) < 2 or f["property"] in have:
            continue
        b["series"].append(
            {
                "key": f["property"],
                "label_fr": f["label_fr"],
                "theme": "kg",
                "theme_fr": "Faits datés (graphe de connaissances)",
                "unit": f["unit"],
                "value": f["value"],
                "value_text": None,
                "period": f["period"],
                "kind": "observed",
                "source": f["source"],
                "points": f["points"],
                "n_points": len(f["points"]),
                "first": f["points"][0]["t"],
                "last": f["points"][-1]["t"],
                "source_breaks": 0,
                "sources_used": [f["source"]["source"]],
                "link": None,
                "out_of_snapshot": False,
                "missing": None,
                "consulted": [],
            }
        )


def _expected_series(b: dict[str, Any], prof: Mapping[str, Any]) -> None:
    for e in prof.get("expected_series") or []:
        b["missing"].append(
            {"section": "series", "what_fr": e["label_fr"], "reason_code": "not_loaded", "reason_fr": e["reason_fr"]}
        )


def _missing_facts(conn: Connection, b: Mapping[str, Any], prof: Mapping[str, Any]) -> list[dict[str, Any]]:
    have = {f["property"] for f in b["facts"]} | {i.get("property") for i in (b.get("identity") or {}).get("ids") or []}
    for g in b["relations"]:
        have |= {p["pid"] for p in g.get("properties") or []}
    for f in b["related_forms"]:
        for g in f["relations"]:
            have |= {p["pid"] for p in g.get("properties") or []}
    out: list[dict[str, Any]] = []
    for e in prof.get("expected_facts") or []:
        pid = e["property"]
        if pid in have:
            continue
        loaded = conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM kg.statement WHERE property_pid = :p)"), {"p": pid}
        ).scalar_one()
        out.append(
            {
                "section": "facts",
                "what_fr": e["label_fr"],
                "reason_code": "not_reported" if loaded else "property_not_loaded",
                "reason_fr": "les sources chargées publient cette propriété pour d’autres entités, pas pour celle-ci"
                if loaded
                else f"propriété {pid} jamais chargée dans le graphe (aucune source ne la fournit encore)",
            }
        )
    return out


# ------------------------------------------------------------------------------------------------ sources
def sources_summary(b: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every source behind a value shown, with its licence and how many values it backs."""
    acc: dict[str, dict[str, Any]] = {}

    def add(src: Mapping[str, Any] | None) -> None:
        if not src or not src.get("source"):
            return
        k = str(src["source"])
        e = acc.setdefault(k, {"source": k, "label": src.get("label") or k, "n_values": 0, "licences": [], "restricted": False,
                               "non_commercial": False, "verification": src.get("verification")})  # fmt: skip
        e["n_values"] += 1
        lic = src.get("licence") or {}
        if lic.get("licence") and lic["licence"] not in e["licences"]:
            e["licences"].append(lic["licence"])
        e["restricted"] = e["restricted"] or bool(lic.get("restricted"))
        e["non_commercial"] = e["non_commercial"] or bool(lic.get("non_commercial"))

    add((b.get("identity") or {}).get("source"))
    for s in b["headline"]:
        add(s.get("source"))
    for f in b["facts"]:
        add(f["source"])
    for g in b["relations"]:
        for it in g["items"]:
            add(it["source"])
    for f in b["related_forms"]:
        for g in f["relations"]:
            for it in g["items"]:
                add(it["source"])
    for s in b["series"]:
        add(s.get("source"))
    for t in b["tops"]:
        for it in t["items"]:
            add(it["source"])
    for c in b["claims"]["incoming"] + b["claims"]["outgoing"]:
        add(c["source"])
    for e in b["effects"]["items"]:
        add(e["source"])
    return sorted(acc.values(), key=lambda e: -e["n_values"])
