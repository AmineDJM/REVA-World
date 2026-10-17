"""Causal claims (in and out) and effect estimates about the entity: always attributed to their source with tier and
grade; REVA asserts no causation and no effect (``reva.causal.graph``, ``reva.effects.query``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, text

from reva.causal import graph as cg
from reva.effects import query as eq
from reva.v8.entity import common as C

PER_SIDE = 8
CLAIM_NOTE_FR = (
    "Affirmations causales attribuées à leur source (« selon … »), avec leur niveau de source (T1 jeu codé par des "
    "experts … T4 Wikidata sans référence) et leur note A–E. REVA n’affirme aucune causalité."
)
EFFECT_NOTE_FR = eq.ESTIMATE_NOTE_FR
TIER_FR = {
    "T1": "T1 · jeu de données codé par des experts",
    "T2": "T2 · référence savante",
    "T3": "T3 · Wikidata avec références",
    "T4": "T4 · Wikidata sans référence",
}


def _claim(c: Mapping[str, Any]) -> dict[str, Any]:
    src = c["source"]
    g = c.get("grade") or {}
    cause, effect = c["cause"], c["effect"]
    reasons = [r.get("fr") or r.get("en") or r.get("rule_id") for r in (g.get("reasons") or [])][:4]
    return {
        "claim_key": c["claim_key"],
        "cause": C.ref(
            cause.get("kind"),
            cause["id"],
            cause.get("label_fr") or cause.get("label"),
            loaded=bool(cause.get("label_loaded", True)),
        ),
        "effect": C.ref(
            effect.get("kind"),
            effect["id"],
            effect.get("label_fr") or effect.get("label"),
            loaded=bool(effect.get("label_loaded", True)),
        ),
        "relation_fr": (c.get("relation_label") or {}).get("fr") or c.get("relation"),
        "wording_fr": c.get("wording_fr") or c.get("wording"),
        "stance": c.get("stance"),
        "polarity": c.get("polarity"),
        "tier": src.get("tier"),
        "tier_fr": TIER_FR.get(str(src.get("tier")), str(src.get("tier"))),
        "grade": g.get("grade"),
        "grade_reasons_fr": [r for r in reasons if r],
        "mechanism": (c.get("mechanism") or None) if isinstance(c.get("mechanism"), str) else None,
        "attribution_fr": (c.get("attribution") or {}).get("note_fr"),
        "contested": bool(c.get("contests") or c.get("disputed_by")),
        "source": {
            "source": src.get("source_key"),
            "label": C.source_name(src.get("source_key")) + (f" · {src['dataset']}" if src.get("dataset") else ""),
            "dataset": src.get("dataset"),
            "statement_key": src.get("statement_key"),
            "series": None,
            "property": None,
            "raw_sha256": src.get("raw_sha256"),
            "selector": src.get("selector") if isinstance(src.get("selector"), dict) else None,
            "retrieved_at": C.iso(src.get("retrieved_at")),
            "period": None,
            "licence": C.licence_of(src.get("rights")),
            "verification": src.get("verification"),
            "origin": c.get("origin"),
            "href": None,
        },
    }


def causal(conn: Connection, qid: str, origin: str | None) -> dict[str, Any]:
    ocond = " AND origin = :o" if origin else ""
    n_in, n_out = conn.execute(
        text(f"""SELECT count(*) FILTER (WHERE effect_id = :q), count(*) FILTER (WHERE cause_id = :q)
                   FROM kg.causal_claim_current WHERE (cause_id = :q OR effect_id = :q){ocond}"""),
        {"q": qid, "o": origin},
    ).one()
    inc = cg.causes(conn, qid, origin=origin, limit=PER_SIDE)["claims"] if n_in else []
    out = cg.effects(conn, qid, origin=origin, limit=PER_SIDE)["claims"] if n_out else []
    return {
        "incoming": [_claim(c) for c in inc],
        "outgoing": [_claim(c) for c in out],
        "n_in": int(n_in),
        "n_out": int(n_out),
        "note_fr": CLAIM_NOTE_FR,
    }


def _effect(e: Mapping[str, Any], role: str) -> dict[str, Any]:
    src = e["source"]
    unc = e.get("uncertainty") or {}
    cit = src.get("citation")
    cit_text = None
    if isinstance(cit, Mapping):
        cit_text = cit.get("short") or cit.get("title") or cit.get("text")
    elif cit:
        cit_text = str(cit)
    return {
        "estimate_key": e["estimate_key"],
        "role": role,
        "cause_fr": e["cause"].get("label_fr") or e["cause"].get("label") or e["cause"].get("id"),
        "effect_fr": e["effect"].get("label_fr") or e["effect"].get("label") or e["effect"].get("id"),
        "cause_id": e["cause"].get("id"),
        "effect_id": e["effect"].get("id"),
        "wording_fr": e.get("wording_fr"),
        "measure": e.get("measure"),
        "unit": e.get("unit"),
        "point": None if e.get("point") is None else str(e["point"]),
        "ci_low": None if unc.get("ci_low") is None else str(unc["ci_low"]),
        "ci_high": None if unc.get("ci_high") is None else str(unc["ci_high"]),
        "uncertainty_status": unc.get("status"),
        "horizon": e.get("horizon"),
        "method": e.get("method"),
        "countries": list((e.get("scope") or {}).get("countries") or []),
        "source": {
            "source": src.get("source_key") or src.get("kind"),
            "label": (cit_text or C.source_name(src.get("source_key")))[:120],
            "dataset": src.get("dataset"),
            "statement_key": e["estimate_key"],
            "series": None,
            "property": None,
            "raw_sha256": src.get("raw_sha256"),
            "selector": None,
            "retrieved_at": src.get("retrieved_at"),
            "period": None,
            "licence": C.licence_of(src.get("rights") if isinstance(src.get("rights"), Mapping) else None),
            "verification": src.get("kind"),
            "origin": e.get("origin"),
            "href": None,
        },
    }


def effects(
    conn: Connection, *, origin: str | None, ids: list[str], country: str | None, limit: int = 12
) -> dict[str, Any]:
    """Effect estimates where one of ``ids`` (variables / atlas concepts / event types) is the cause or the effect,
    and — for a country — estimates scoped to it."""
    o = origin or "any"
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    n_country: int | None = None
    for i in ids:
        for role, kw in (("cause", {"cause": i}), ("effect", {"effect": i})):
            rows = eq.list_estimates(conn, origin=o, limit=limit, **kw)
            for e in rows:
                if e["estimate_key"] not in seen:
                    seen.add(e["estimate_key"])
                    out.append(_effect(e, role))
    if country:
        n_country = conn.execute(
            text("""SELECT count(*) FROM kg.effect_estimate_current e
                     WHERE e.origin = ANY(:o) AND e.scope->'countries' ? :c"""),
            {"o": ["live", "test_fixture"] if o == "any" else [o], "c": country},
        ).scalar_one()
        for e in eq.list_estimates(conn, origin=o, country=country, limit=limit):
            if e["estimate_key"] in seen or country not in (e.get("scope") or {}).get("countries", []):
                continue
            seen.add(e["estimate_key"])
            out.append(_effect(e, "scope"))
    return {
        "items": out[: limit * 2],
        "n": len(seen),
        "n_country_scope": None if n_country is None else int(n_country),
        "note_fr": EFFECT_NOTE_FR,
    }
