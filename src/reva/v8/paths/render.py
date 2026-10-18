"""Rendering of V8 Liens results (agent V8LIENS): node references, one sentence per hop, provenance, licence and
evidence strength, all read from the database for the few rows a result shows.

Reuses the existing readers instead of re-implementing them: KG statements (``reva.kg.query`` statement output,
``reva.kg.family_query`` rights), causal claims (``reva.causal.graph`` claim output with grade, tier, source
rights), effect estimates (``reva.effects.query.estimate_json``), effect variable / atlas concept / event type labels
(``reva.effects.variables.default_resolver``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any

from sqlalchemy import Connection, text

from reva.v8.paths.index import (
    EDGE_BRIDGE,
    EDGE_CLAIM,
    EDGE_ESTIMATE,
    EDGE_KINDS,
    EDGE_SCOPE,
    EDGE_STATEMENT,
    GRADES,
    UNGRADED,
    PathIndex,
    split_virtual,
)
from reva.v8.paths.search import EdgeFilter, FoundPath, Hop, parallel_count

NOTE_FR = (
    "Un chemin est un ensemble de liens documentés, pas une preuve causale : chaque maillon cite sa source et sa "
    "licence. Les maillons causaux sont des affirmations de leurs sources (palier T1–T4, note A–E) et les maillons "
    "« estimation » des estimations publiées par leurs auteurs ; REVA n'affirme pas la causalité et ne compose pas "
    "les maillons en une chaîne causale."
)
NOTE_EN = (
    "A path is a set of documented links, not a causal proof: every hop cites its source and licence. Causal hops "
    "are claims made by their sources (tier T1–T4, grade A–E) and estimate hops are estimates published by their "
    "authors; REVA does not assert causation and does not compose the hops into a causal chain."
)
WIKIDATA_SOURCE = {"fr": "Wikidata (contribution communautaire)", "en": "Wikidata (community-edited)"}
KIND_LABELS = {
    "effect_variable": {"fr": "variable d'effet", "en": "effect variable"},
    "atlas_concept": {"fr": "indicateur atlas", "en": "atlas indicator"},
    "event_type": {"fr": "type d'événement", "en": "event type"},
}
ESTIMATE_LABEL = {
    True: {"fr": "a un effet estimé sur", "en": "has an estimated effect on"},
    False: {"fr": "subit un effet estimé de", "en": "has an estimated response to"},
}
SCOPE_LABEL = {
    True: {"fr": "estimé pour le pays", "en": "estimated for the country"},
    False: {"fr": "est le périmètre de l'estimation", "en": "is the scope of the estimate"},
}
STRENGTH_FR = {
    "community_edited_not_verified": "déclaration Wikidata (communautaire, non vérifiée)",
    "source_reported_not_verified": "déclaré par la source (non vérifié par REVA)",
    "derived": "dérivé par REVA à partir de sources (méthode dans les qualificatifs)",
}


@lru_cache(maxsize=1)
def _resolver() -> Any:
    from reva.effects.variables import default_resolver

    return default_resolver()


def warm() -> None:
    """Load the registries rendering reads (effect variable / atlas concept labels, relation families, causal
    vocabulary and grading rules)."""
    from reva.causal import grading as gr
    from reva.causal.vocab import vocabulary
    from reva.kg.family_query import registry

    _resolver()
    registry()
    vocabulary()
    gr.load_rules()


# ---------------------------------------------------------------------------------------------- nodes
def node_refs(conn: Connection, idx: PathIndex | None, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    from reva.kg.query import country_qid_registry, kinds_for, labels_for

    ids = sorted(set(ids))
    iso_qid = country_qid_registry()
    qid_iso = {q: i for i, q in iso_qid.items()}
    virt = {q: split_virtual(q) for q in ids}
    kg_ids = set(ids) | {iso_qid[v[2]] for v in virt.values() if v and v[2] in iso_qid}
    labels = labels_for(conn, kg_ids, ("fr", "en"))
    kinds = kinds_for(conn, kg_ids)
    res = _resolver()
    out: dict[str, dict[str, Any]] = {}
    for q in ids:
        v = virt[q]
        lab = labels.get(q, {})
        deg = None
        if idx is not None and (i := idx.idx(q)) is not None:
            deg = int(idx.degree[i])
        if v is not None and (v[2] is not None or not lab):
            kind, base, iso = v
            rl = res.labels(kind, base) or {}
            en = rl.get("en") or base
            fr = rl.get("fr") or rl.get("en") or base
            if iso:
                cl = labels.get(iso_qid.get(iso, ""), {})
                en = f"{en} — {cl.get('en') or iso}"
                fr = f"{fr} — {cl.get('fr') or cl.get('en') or iso}"
            vk = "effect_variable" if kind == "variable" else kind
            out[q] = {
                "id": q,
                "label": fr,
                "label_en": en,
                "label_loaded": bool(rl),
                "kind": vk,
                "kind_label": KIND_LABELS.get(vk, {}).get("fr"),
                "virtual": True,
                "scope_iso3": iso,
                "fiche": None,
                "degree": deg,
            }
            continue
        name_fr = lab.get("fr") or lab.get("en")
        name_en = lab.get("en") or lab.get("fr")
        ekind = kinds.get(q)
        cty = qid_iso.get(q)
        out[q] = {
            "id": q,
            "label": name_fr or f"{q} (libellé non chargé)",
            "label_en": name_en or f"{q} (label not loaded)",
            "label_loaded": bool(name_fr),
            "kind": ekind,
            "kind_label": None,
            "virtual": False,
            "scope_iso3": None,
            "fiche": {"kind": "country", "id": cty} if cty else {"kind": ekind or "entity", "id": q},
            "degree": deg,
        }
    return out


# ---------------------------------------------------------------------------------------------- hops
def _source_titles(conn: Connection, keys: Iterable[str]) -> dict[str, str]:
    ks = sorted(set(keys))
    if not ks:
        return {}
    return {
        k: t
        for k, t in conn.execute(
            text("SELECT source_key, title FROM evidence.source WHERE source_key = ANY(:k)"), {"k": ks}
        ).all()
    }


def _licence(r: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not r:
        return None
    keep = (
        "licence",
        "licence_url",
        "attribution",
        "commercial_use",
        "non_commercial",
        "share_alike",
        "no_derivatives",
        "open_licence",
        "restricted",
        "restriction",
        "required_notice",
        "basis",
    )
    return {k: r.get(k) for k in keep if k in r}


def _statements(conn: Connection, row_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    from reva.kg import family_query as fq
    from reva.kg import query as kq

    if not row_ids:
        return {}
    rows = [
        dict(r)
        for r in conn.execute(
            text(f"SELECT {kq._ST_SELECT} FROM kg.statement_current s WHERE s.id = ANY(:ids)"),
            {"ids": list(row_ids)},
        ).mappings()
    ]
    by_sha = fq.rights_for(conn, rows)
    props = kq.property_info(conn, {r["property_pid"] for r in rows})
    labels = kq.labels_for(conn, {q for r in rows for q in (r["subject_qid"], r["object_qid"]) if q})
    out = {}
    for r in rows:
        st = kq._statement_out(r, labels, props, None)
        st["rights"] = fq._rights(r, by_sha)
        st["property_info"] = props.get(r["property_pid"], {})
        out[int(r["id"])] = st
    return out


def _claims(conn: Connection, row_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    from reva.causal import graph as cg

    if not row_ids:
        return {}
    rows, _ = cg._claims(conn, "c.id = ANY(:ids)", {"ids": list(row_ids)}, origin=None, limit=cg.MAX_CLAIMS)
    return {int(c["row_id"]): c for c in cg._render(conn, rows)}


def _estimates(conn: Connection, row_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    from reva.effects.query import estimate_json

    if not row_ids:
        return {}
    res = _resolver()
    return {
        int(r["id"]): estimate_json(dict(r), res)
        for r in conn.execute(
            text("SELECT e.* FROM kg.effect_estimate_current e WHERE e.id = ANY(:ids)"), {"ids": list(row_ids)}
        ).mappings()
    }


class HopDetails:
    """Database details of every edge a response shows, fetched in one query per edge kind."""

    def __init__(self, conn: Connection, idx: PathIndex, edges: Iterable[int]) -> None:
        es = sorted(set(edges))
        by_kind: dict[int, list[int]] = {}
        for e in es:
            by_kind.setdefault(int(idx.e_kind[e]), []).append(int(idx.e_ref[e]))
        scope_est = [r for si in by_kind.get(EDGE_SCOPE, []) for r in idx.scopes[si]["estimates"][:3]]
        self.statements = _statements(conn, by_kind.get(EDGE_STATEMENT, []))
        self.claims = _claims(conn, by_kind.get(EDGE_CLAIM, []))
        self.estimates = _estimates(conn, by_kind.get(EDGE_ESTIMATE, []) + scope_est)
        keys = [s["provenance"]["source_key"] for s in self.statements.values()]
        keys += [c["source"]["source_key"] for c in self.claims.values()]
        keys += [x["source"]["source_key"] for x in self.estimates.values()]
        self.titles = _source_titles(conn, keys)
        from reva.kg.family_query import registry

        self.registry = registry()


def _grade(idx: PathIndex, e: int) -> str | None:
    g = int(idx.e_grade[e])
    return GRADES[g] if 0 <= g < UNGRADED else None


def _source_label(key: str, titles: Mapping[str, str]) -> str:
    from reva.kg.query import is_community_edited

    if is_community_edited(key):
        return WIKIDATA_SOURCE["fr"]
    return titles.get(key) or key


def _fr_num(x: Any) -> str:
    t = str(x)
    return t.replace(".", ",") if all(c in "+-0123456789.eE" for c in t) else t


def _estimate_value(est: Mapping[str, Any]) -> str:
    """French short value of an estimate as published: ``0,34 % pour un choc de 10 percent``; the sign only when the
    source gives no number (never a filled zero)."""
    from reva.effects.query import UNIT_WORDS

    v = est.get("as_printed") or (None if est.get("point") is None else str(est["point"]))
    if v is None:
        return f"signe {est.get('sign')}, valeur non chiffrée"
    unit = UNIT_WORDS.get(est.get("unit") or "", ("", ""))[1]
    shock = est.get("shock") or None
    sunit = {"percent": "%", "percentage_points": "point(s) de %", "log_points": "point(s) log"}.get(
        str((shock or {}).get("unit")), str((shock or {}).get("unit"))
    )
    tail = f" pour un choc de {_fr_num(shock['size'])} {sunit}" if shock else ""
    return f"{_fr_num(v)}{unit}{tail}"


def render_hop(
    idx: PathIndex, h: Hop, nodes: Mapping[str, Mapping[str, Any]], det: HopDetails, flt: EdgeFilter
) -> dict[str, Any]:
    e = h.edge
    k = int(idx.e_kind[e])
    a, b = idx.node_ids[h.frm], idx.node_ids[h.to]
    na, nb = nodes[a], nodes[b]
    out: dict[str, Any] = {
        "from": a,
        "to": b,
        "kind": EDGE_KINDS[k],
        "direction": "forward" if h.rec_fwd else "inverse",
        "cost": round(h.cost, 3),
        "node_penalty": round(h.penalty, 3),
        "alternatives": parallel_count(idx, h, flt),
        "family": None,
        "property": None,
        "relation": None,
        "sentence": None,
        "source": None,
        "licence": None,
        "strength": None,
        "dated": None,
        "detail": None,
        "origin": "test_fixture" if bool(idx.e_fixture[e]) else "live",
    }
    if k == EDGE_STATEMENT:
        st = det.statements.get(int(idx.e_ref[e]))
        pid = idx.pids[int(idx.e_pid[e])]
        ef = det.registry.edge_family(pid, "out" if h.rec_fwd else "in")
        out["family"] = None if ef is None else {"id": ef.family, "label": dict(ef.label), "inverse": ef.inverse}
        rel_fr = (ef.label.get("fr") if ef else None) or pid
        rel_en = (ef.label.get("en") if ef else None) or pid
        if st is not None:
            pi = st["property_info"]
            p_fr = pi.get("label_fr") if h.rec_fwd else pi.get("inverse_label_fr")
            p_en = pi.get("label_en") if h.rec_fwd else pi.get("inverse_label_en")
            out["property"] = {
                "pid": pid,
                "label_fr": p_fr or pi.get("label_fr"),
                "label_en": p_en or pi.get("label_en"),
            }
            prov = st["provenance"]
            out["source"] = {
                "key": prov["source_key"],
                "label": _source_label(prov["source_key"], det.titles),
                "statement_id": prov["statement_id"] or prov["statement_key"],
                "revision": prov["source_revision"],
                "retrieved_at": prov["retrieved_at"].isoformat() if prov.get("retrieved_at") else None,
            }
            out["licence"] = _licence(st.get("rights"))
            out["strength"] = {
                "level": "documented",
                "verification": prov["verification"],
                "label": STRENGTH_FR.get(prov["verification"], prov["verification"]),
                "grade": None,
                "tier": None,
            }
            out["dated"] = {"from": st["valid_from"], "to": st["valid_to"]}
            out["detail"] = {k2: st[k2] for k2 in ("row_id", "qualifiers", "start", "end", "point_in_time")}
            out["detail"]["references"] = prov.get("references")
            out["origin"] = prov["origin"]
        else:
            out["property"] = {"pid": pid, "label_fr": None, "label_en": None}
            out["strength"] = {"level": "missing", "label": "déclaration introuvable (index plus récent que la base ?)"}
        out["relation"] = {"fr": rel_fr, "en": rel_en}
    elif k == EDGE_CLAIM:
        c = det.claims.get(int(idx.e_ref[e]))
        grade = _grade(idx, e)
        if c is not None:
            lab = c["relation_label"] if h.rec_fwd else c["relation_inverse_label"]
            out["relation"] = {"fr": lab["fr"], "en": lab["en"]}
            src = c["source"]
            out["source"] = {
                "key": src["source_key"],
                "label": _source_label(src["source_key"], det.titles),
                "statement_id": src.get("statement_id") or src.get("statement_key"),
                "revision": src.get("source_revision"),
                "citation": (src.get("citation") or {}).get("short") or (src.get("citation") or {}).get("text"),
                "dataset": src.get("dataset"),
            }
            out["licence"] = _licence(src.get("rights"))
            g = c.get("grade") or {}
            out["strength"] = {
                "level": "causal_claim",
                "grade": g.get("grade") or grade,
                "tier": src.get("tier"),
                "label": f"affirmation causale de la source, note {g.get('grade') or grade or '—'} ({src.get('tier')})",
                "reasons": [r.get("fr") or r.get("en") or r.get("rule_id") for r in g.get("reasons") or []][:6],
                "temporal_check": g.get("temporal_check"),
            }
            out["detail"] = {
                "claim_key": c["claim_key"],
                "wording_fr": c["wording_fr"],
                "wording": c["wording"],
                "mechanism": c.get("mechanism"),
                "attribution": (c.get("attribution") or {}).get("note_fr"),
                "polarity": c.get("polarity"),
                "lane": c.get("lane"),
            }
            out["origin"] = c["origin"]
        else:
            out["relation"] = {"fr": "affirmation causale", "en": "causal claim"}
            out["strength"] = {"level": "missing", "label": "affirmation introuvable (index plus récent que la base ?)"}
    elif k == EDGE_ESTIMATE:
        est = det.estimates.get(int(idx.e_ref[e]))
        lab = ESTIMATE_LABEL[h.rec_fwd]
        out["relation"] = dict(lab)
        if est is not None:
            src = est["source"]
            cit = (src.get("citation") or {}).get("short") or (src.get("citation") or {}).get("text")
            out["source"] = {
                "key": src["source_key"],
                "label": cit or _source_label(src["source_key"], det.titles),
                "statement_id": est["estimate_key"],
                "revision": None,
                "citation": cit,
                "locator": src.get("locator"),
            }
            out["licence"] = _licence(src.get("rights")) if src.get("rights") else None
            unc = est["uncertainty"]["status"]
            out["strength"] = {
                "level": "estimate",
                "grade": None,
                "tier": None,
                "label": f"estimation publiée ({est['method']}, incertitude {unc})",
                "method": est["method"],
                "uncertainty": unc,
            }
            out["detail"] = {
                "estimate_key": est["estimate_key"],
                "value": _estimate_value(est),
                "measure": est["measure"],
                "unit": est["unit"],
                "point": est["point"],
                "shock": est["shock"],
                "horizon": est["horizon"],
                "scope": est["scope"],
                "wording_fr": est["wording_fr"],
                "wording": est["wording"],
                "ci": [est["uncertainty"]["ci_low"], est["uncertainty"]["ci_high"]],
            }
            out["relation"] = {
                "fr": f"{lab['fr']} ({_estimate_value(est)})",
                "en": f"{lab['en']} ({_estimate_value(est)})",
            }
        else:
            out["strength"] = {"level": "missing", "label": "estimation introuvable"}
    elif k == EDGE_SCOPE:
        sc = idx.scopes[int(idx.e_ref[e])]
        out["relation"] = dict(SCOPE_LABEL[h.rec_fwd])
        ests = [det.estimates[r] for r in sc["estimates"][:3] if r in det.estimates]
        cits = sorted({(x["source"].get("citation") or {}).get("short") or x["source"]["source_key"] for x in ests})
        out["source"] = {
            "key": ests[0]["source"]["source_key"] if ests else "effects",
            "label": "; ".join(cits) or "estimations d'effet",
            "statement_id": None,
            "revision": None,
            "citation": "; ".join(cits) or None,
        }
        out["licence"] = _licence(ests[0]["source"].get("rights")) if ests and ests[0]["source"].get("rights") else None
        out["strength"] = {
            "level": "declared_scope",
            "label": f"périmètre déclaré de {len(sc['estimates'])} estimation(s) (scope.countries)",
            "grade": None,
            "tier": None,
        }
        out["detail"] = {"iso3": sc["iso3"], "estimates": len(sc["estimates"])}
    elif k == EDGE_BRIDGE:
        br = idx.bridges[int(idx.e_ref[e])]
        out["relation"] = (
            {"fr": br.label_fr, "en": br.label_en} if h.rec_fwd else {"fr": br.inverse_fr, "en": br.inverse_en}
        )
        out["source"] = {"key": "reva.registry", "label": br.basis, "statement_id": None, "revision": None}
        out["strength"] = {"level": "declared_identifier_link", "label": f"lien d'identifiant déclaré ({br.basis})"}
    rel = (out["relation"] or {}).get("fr") or "?"
    out["sentence"] = {
        "fr": f"{na['label']} — {rel} → {nb['label']}",
        "en": f"{na['label_en']} — {(out['relation'] or {}).get('en') or '?'} → {nb['label_en']}",
    }
    return out


def path_kind(hops: Sequence[Mapping[str, Any]]) -> str:
    causal = sum(1 for h in hops if h["kind"] in ("causal_claim", "effect_estimate"))
    if causal == 0:
        return "documented"
    return "causal" if causal == len(hops) else "mixed"


def render_path(
    idx: PathIndex,
    p: FoundPath,
    rank: int,
    nodes: Mapping[str, Mapping[str, Any]],
    det: HopDetails,
    flt: EdgeFilter,
) -> dict[str, Any]:
    hops = [render_hop(idx, h, nodes, det, flt) for h in p.hops]
    grades = [h["strength"].get("grade") for h in hops if h["kind"] == "causal_claim" and h.get("strength")]
    weakest = max((g for g in grades if g), key=GRADES.index, default=None) if grades else None
    restricted = sorted({h["licence"]["licence"] for h in hops if h.get("licence") and h["licence"].get("restricted")})
    return {
        "rank": rank,
        "cost": round(p.cost, 3),
        "length": len(hops),
        "kind": path_kind(hops),
        "nodes": [idx.node_ids[n] for n in p.nodes],
        "hops": hops,
        "weakest_grade": weakest,
        "restricted_licences": restricted,
        "unknown_licences": sum(1 for h in hops if h.get("licence") is None and h["kind"] != "declared_bridge"),
        "variants": {
            "count": p.variants,
            "position": p.variant_position,
            "examples": [idx.node_ids[n] for n in p.variant_examples],
        }
        if p.variants
        else None,
    }
