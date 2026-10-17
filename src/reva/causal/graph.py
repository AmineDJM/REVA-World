"""Read-only causal graph services (``reva_api`` read transaction): causes / effects of an event, the "why"
view, causal chains between two events, the consistency report and the quarantined candidate space.

Honesty rules (docs/roadmap/v3_causality.md):

* every edge is a claim with its asserting source, tier, grade (+ rule ids and reasons), verbatim mechanism and
  temporal check; the wording says "according to <source>" and that REVA does not establish causation;
* a chain is REVA's *composition* of separately asserted claims; chain grade = weakest hop; ≤ 6 hops; only
  causal edges of the ``cause`` family with stance ``asserts`` are traversed (context edges never count);
  intermediate nodes must not be *classes* by the KG's own statements (:func:`class_nodes`: "war", "peace
  treaty", "casus belli" are kinds of events, not events); self-loops are reported by :func:`consistency`, never
  traversed;
* candidates (T5/T6) are only returned by :func:`candidates`, labelled unsourced;
* every result is bounded (row caps, time budget, k paths) and says when it was cut.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import Connection, text

from reva.causal import grading as gr
from reva.causal import identity as idn
from reva.causal.grader import fetch_event_dates
from reva.causal.labels import QUALIFIER_ITEM_PIDS, unloaded_label
from reva.causal.vocab import FAMILIES, GENERIC_LANE, LANES, OUTCOME_LANE, RELATIONS, Vocabulary, vocabulary
from reva.kernel.errors import ValidationProblem
from reva.kg.model import QID_RE
from reva.kg.query import is_community_edited, kinds_for, labels_for, verification_of

MAX_HOPS = 6
MAX_PATHS = 10
MAX_CLAIMS = 500
CHAIN_TIME_BUDGET_S = 5.0
MAX_FRONTIER = 5_000
MAX_EDGES_PER_LEVEL = 20_000
MAX_PATH_CANDIDATES = 2_000
MAX_CYCLE_EDGES = 200_000
CONSISTENCY_TIME_BUDGET_S = 8.0
MAX_CONTEXT = 50
ORIGINS = ("live", "test_fixture")

CHAIN_NOTE = (
    "composition by REVA of separately asserted claims; chain grade = weakest hop. Each hop is asserted by "
    "its own source; REVA does not assert the chain, and a chain is not evidence that the first event caused "
    "the last."
)
CHAIN_NOTE_FR = (
    "composition par REVA d'affirmations causales énoncées séparément ; note de la chaîne = maillon le plus "
    "faible. Chaque maillon est affirmé par sa propre source ; REVA n'affirme pas la chaîne."
)
CLAIM_NOTE = (
    "Causal links are claims made by the named sources, graded A-E by declared rules "
    "(registry/causal/grading.yaml); REVA does not establish causation."
)
CANDIDATE_NOTE = (
    "Unsourced candidates (text-mined T5 / model-proposed T6), quarantined: never claims, never graded, "
    "never used in answers. Non sourcé — candidats."
)
CONTEXT_NOTE = "Context edges (part of, follows, participants, places) are not causal and are never counted."
LANE_OF_RELATION_ORDER = LANES

_CLAIM_SELECT = """c.id, c.claim_key, c.origin, c.cause_id, c.effect_id, c.relation, c.family, c.polarity, c.stance,
  c.asserted_on, c.source_key, c.source_tier, c.dataset, c.source_revision, c.statement_key, c.statement_id,
  c.statement_row_id, c.selector, c.raw_sha256, c.refs, c.citation, c.codebook, c.mechanism, c.scope, c.qualifiers,
  c.contests, c.disputed_by, c.retrieved_at, c.recorded_at, c.review_status, c.load_id,
  g.grade, g.rules_version, g.rules_sha256, g.rule_ids, g.temporal_check, g.temporal, g.corroboration,
  g.corroborating_claim_keys, g.upstream_groups, g.contested_by, g.graded_at"""
_GRADE_ORDER = "CASE g.grade WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 WHEN 'D' THEN 3 WHEN 'E' THEN 4 ELSE 5 END"


# ---------------------------------------------------------------------------------------------- checks
def check_id(q: str) -> str:
    if not isinstance(q, str) or not QID_RE.match(q):
        raise ValidationProblem("invalid event id (expected a Wikidata Q-id or a provider-scoped key)", id=str(q)[:64])
    return q


def check_origin(origin: str | None) -> str | None:
    if origin is not None and origin not in ORIGINS:
        raise ValidationProblem("origin must be live or test_fixture", origin=str(origin)[:32])
    return origin


def check_grade(g: str | None) -> str | None:
    if g is not None and g not in gr.GRADES:
        raise ValidationProblem("min_grade must be one of A, B, C, D, E", min_grade=str(g)[:8])
    return g


def _grades_at_least(min_grade: str | None) -> list[str] | None:
    return None if min_grade is None else list(gr.GRADES[: gr.GRADES.index(min_grade) + 1])


# ---------------------------------------------------------------------------------------------- claim JSON
def _source_name(source_key: str) -> str:
    return "Wikidata" if is_community_edited(source_key) else source_key


def _source_ref(r: Mapping[str, Any], lang: str = "en") -> str:
    fr = lang == "fr"
    if r["statement_id"]:
        parts = [f"{'déclaration' if fr else 'statement'} {r['statement_id']}"]
        if r["source_revision"]:
            parts.append(f"{'révision' if fr else 'revision'} {r['source_revision']}")
        if is_community_edited(r["source_key"]):
            parts.append("édition communautaire" if fr else "community-edited")
        return ", ".join(parts)
    parts = [f"{'jeu de données' if fr else 'dataset'} {r['dataset']}"]
    if r["source_revision"]:
        parts.append(f"version {r['source_revision']}")
    parts.append(("ligne " if fr else "row ") + gr._ENC.encode(r["selector"])[:120])
    return ", ".join(parts)


def _name(ref: Mapping[str, Any], lang: str = "en") -> str:
    return str((ref.get("label_fr") if lang == "fr" else None) or ref["label"])


def _qual_items(r: Mapping[str, Any], pid: str) -> list[str]:
    vals = (r.get("qualifiers") or {}).get(pid) or []
    out = []
    for x in vals if isinstance(vals, list) else [vals]:
        q = gr._item(x)
        if q and QID_RE.match(q):
            out.append(q)
    return out


def _and(names: Sequence[str], fr: bool) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + (" et " if fr else " and ") + names[-1]


def attribution_of(r: Mapping[str, Any], labels: Mapping[str, Mapping[str, str]]) -> dict[str, Any] | None:
    """Who Wikidata says makes the claim (P3680 statement supported by), how it qualifies it (P5102 nature of
    statement), and the source's own doubtful certainty codes (``scope.source_validity``) — audit H3 / H4. ``None``
    when the claim carries none of them."""
    sup, nat = _qual_items(r, gr.SUPPORTED_BY_PID), _qual_items(r, gr.NATURE_OF_STATEMENT_PID)
    hedge = _qual_items(r, gr.SOURCING_CIRCUMSTANCES_PID)
    scope = [(p, q) for p in gr.SCOPE_QUALIFIER_PIDS for q in _qual_items(r, p)]
    validity = [
        x for x in (r.get("scope") or {}).get("source_validity") or [] if isinstance(x, Mapping) and x.get("certainty")
    ]
    doubtful = [x for x in validity if x.get("certainty") == "doubtful"]
    if not (sup or nat or hedge or scope or doubtful):
        return None
    sup_refs, nat_refs = [_ref(q, labels, {}) for q in sup], [_ref(q, labels, {}) for q in nat]
    hedge_refs = [_ref(q, labels, {}) for q in hedge]
    scope_refs = [{"property": p, **_ref(q, labels, {})} for p, q in scope]
    en, fr = [], []
    if hedge_refs:  # audit2 N2: Wikidata hedges the statement itself (P1480), verbatim value
        en.append(
            "Wikidata qualifies: " + _and([x["label"] for x in hedge_refs], False) + " (sourcing circumstances, P1480)"
        )
        fr.append(
            "Wikidata qualifie : "
            + _and([x["label_fr"] or x["label"] for x in hedge_refs], True)
            + " (circonstances de la source, P1480)"
        )
    if nat_refs:
        en.append("Wikidata qualifies the nature of this statement as: " + _and([x["label"] for x in nat_refs], False))
        fr.append(
            "Wikidata qualifie la nature de cette déclaration : "
            + _and([x["label_fr"] or x["label"] for x in nat_refs], True)
        )
    for p in gr.SCOPE_QUALIFIER_PIDS:  # audit2 N2: the scope the statement is restricted to, worded
        vals = [x for x in scope_refs if x["property"] == p]
        if vals:
            w_en, w_fr = gr.SCOPE_QUALIFIER_WORDS[p]
            en.append(f"{w_en}: " + _and([x["label"] for x in vals], False))
            fr.append(f"{w_fr} : " + _and([x["label_fr"] or x["label"] for x in vals], True))
    for x in doubtful:
        code = f"{x.get('field')} = {x.get('code')}"
        en.append(f"the source codes it as doubtful: {x.get('label')} ({code})")
        fr.append(f"la source le code comme douteux : {x.get('label')} ({code})")
    return {
        "supported_by": sup_refs,
        "nature_of_statement": nat_refs,
        "sourcing_circumstances": hedge_refs,
        "scope_qualifiers": scope_refs,
        "source_validity": validity,
        "note_en": "; ".join(en) or None,
        "note_fr": " ; ".join(fr) or None,
    }


def _wording(
    r: Mapping[str, Any],
    cause: Mapping[str, Any],
    effect: Mapping[str, Any],
    v: Vocabulary,
    attribution: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    rel = v.relation(r["relation"])
    src, ref, ref_fr = _source_name(r["source_key"]), _source_ref(r), _source_ref(r, "fr")
    effect_first = r["asserted_on"] == "effect"
    if effect_first:
        en = f"{_name(effect)} {rel.inverse_label_en} {_name(cause)}"
        fr = f"{_name(effect, 'fr')} {rel.inverse_label_fr} {_name(cause, 'fr')}"
    else:
        en = f"{_name(cause)} {rel.label_en} {_name(effect)}"
        fr = f"{_name(cause, 'fr')} {rel.label_fr} {_name(effect, 'fr')}"
    att = attribution or {}
    note_en = f" ({att['note_en']})" if att.get("note_en") else ""
    note_fr = f" ({att['note_fr']})" if att.get("note_fr") else ""
    if att.get("supported_by"):
        who_en = _and([x["label"] for x in att["supported_by"]], False)
        who_fr = _and([x["label_fr"] or x["label"] for x in att["supported_by"]], True)
        many = len(att["supported_by"]) > 1
        if r["stance"] == "denies":
            verb_en, verb_fr = ("deny", "nient") if many else ("denies", "nie")
        else:
            verb_en, verb_fr = ("assert", "affirment") if many else ("asserts", "affirme")
        return (
            f"{src} ({ref}) records that {who_en} {verb_en} that {en}{note_en}; {src} does not assert it itself, "
            "and REVA does not establish causation.",
            f"{src} ({ref_fr}) indique que {who_fr} {verb_fr} que : {fr}{note_fr} ; {src} ne l'affirme pas lui-même et "
            "REVA n'établit pas de causalité.",
        )
    if r["stance"] == "denies":
        return (
            f"{src} ({ref}) denies that {en}{note_en}; REVA does not settle the disagreement.",
            f"{src} ({ref_fr}) nie que : {fr}{note_fr} ; REVA ne tranche pas le désaccord.",
        )
    return (
        f"According to {src} ({ref}), {en}{note_en}; REVA does not establish causation.",
        f"Selon {src} ({ref_fr}), {fr}{note_fr} ; REVA n'établit pas de causalité.",
    )


def _ref(q: str, labels: Mapping[str, Mapping[str, str]], kinds: Mapping[str, str], lang: str = "en") -> dict[str, Any]:
    """An item as shown: its KG label, else ``Q7747 (label not loaded)`` / ``(libellé non chargé)`` — never a bare id
    standing in for a name, never a label written by hand (audit2 N3)."""
    lab = labels.get(q, {})
    name = lab.get(lang) or lab.get("en") or lab.get("fr")
    return {
        "id": q,
        "label": name or unloaded_label(q, "en"),
        "label_fr": lab.get("fr") or (None if name else unloaded_label(q, "fr")),
        "label_loaded": bool(name),
        "kind": kinds.get(q),
    }


def _grade_out(r: Mapping[str, Any], rules: gr.Rules) -> dict[str, Any] | None:
    if r["grade"] is None:
        return None
    same = r["rules_sha256"] == rules.sha256
    reasons = [rules.reason(i) if same else {"rule_id": i, "en": None, "fr": None} for i in r["rule_ids"]]
    return {
        "grade": r["grade"],
        "rules_version": r["rules_version"],
        "rules_sha256": r["rules_sha256"],
        "rules_current": same,
        "rule_ids": list(r["rule_ids"]),
        "reasons": reasons,
        "temporal_check": r["temporal_check"],
        "temporal": r["temporal"],
        "corroboration": r["corroboration"],
        "corroborating_claim_keys": list(r["corroborating_claim_keys"] or []),
        "upstream_groups": r["upstream_groups"],
        "contested_by": list(r["contested_by"] or []),
        "graded_at": r["graded_at"],
    }


def claim_out(
    r: Mapping[str, Any],
    labels: Mapping[str, Mapping[str, str]],
    kinds: Mapping[str, str],
    v: Vocabulary,
    rules: gr.Rules,
    rights: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rel = v.relation(r["relation"])
    cause, effect = _ref(r["cause_id"], labels, kinds), _ref(r["effect_id"], labels, kinds)
    attribution = attribution_of(r, labels)
    en, fr = _wording(r, cause, effect, v, attribution)
    verification, _, _ = verification_of(r["source_key"], r["qualifiers"])
    return {
        "claim_key": r["claim_key"],
        "row_id": r["id"],
        "cause": cause,
        "effect": effect,
        "relation": r["relation"],
        "relation_label": {"en": rel.label_en, "fr": rel.label_fr},
        "relation_inverse_label": {"en": rel.inverse_label_en, "fr": rel.inverse_label_fr},
        "family": r["family"],
        "lane": rel.lane if r["family"] == "cause" else "prevention",
        "polarity": r["polarity"],
        "stance": r["stance"],
        "asserted_on": r["asserted_on"],
        "source": {
            "source_key": r["source_key"],
            "tier": r["source_tier"],
            "dataset": r["dataset"],
            "source_revision": r["source_revision"],
            "statement_key": r["statement_key"],
            "statement_id": r["statement_id"],
            "statement_row_id": r["statement_row_id"],
            "selector": r["selector"],
            "raw_sha256": r["raw_sha256"],
            "references": r["refs"],
            "citation": r["citation"],
            "codebook": r["codebook"],
            "verification": verification,
            "retrieved_at": r["retrieved_at"],
            "rights": (rights or {}).get(r["source_key"]) or (rights or {}).get("sha:" + str(r["raw_sha256"])),
        },
        "mechanism": r["mechanism"],
        "scope": r["scope"],
        "qualifiers": r["qualifiers"],
        "contests": list(r["contests"] or []),
        "disputed_by": r["disputed_by"],
        "attribution": attribution,
        "grade": _grade_out(r, rules),
        "review_status": r["review_status"],
        "wording": en,
        "wording_fr": fr,
        "origin": r["origin"],
        "recorded_at": r["recorded_at"],
        "load_id": r["load_id"],
    }


def _claims(
    conn: Connection,
    where: str,
    params: Mapping[str, Any],
    *,
    origin: str | None,
    relation: Sequence[str] | None = None,
    family: str | None = None,
    min_grade: str | None = None,
    limit: int = MAX_CLAIMS,
) -> tuple[list[dict[str, Any]], bool]:
    if relation is not None and not set(relation) <= set(RELATIONS):
        raise ValidationProblem("unknown relation", allowed=list(RELATIONS))
    if family is not None and family not in FAMILIES:
        raise ValidationProblem("family must be cause or prevention")
    check_grade(min_grade)
    if not 1 <= limit <= MAX_CLAIMS:
        raise ValidationProblem("limit out of range", max=MAX_CLAIMS)
    cond = [where]
    p: dict[str, Any] = dict(params)
    if origin:
        cond.append("c.origin = :origin")
        p["origin"] = origin
    if relation:
        cond.append("c.relation = ANY(:rel)")
        p["rel"] = list(relation)
    if family:
        cond.append("c.family = :fam")
        p["fam"] = family
    if min_grade:
        cond.append("g.grade = ANY(:grades)")
        p["grades"] = _grades_at_least(min_grade)
    p["lim"] = limit + 1
    rows = (
        conn.execute(
            text(f"""SELECT {_CLAIM_SELECT}
                       FROM kg.causal_claim_current c
                       LEFT JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                      WHERE {" AND ".join(cond)}
                      ORDER BY {_GRADE_ORDER}, c.id LIMIT :lim"""),
            p,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows[:limit]], len(rows) > limit


def _render(conn: Connection, rows: Sequence[Mapping[str, Any]], extra_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    ids = {r["cause_id"] for r in rows} | {r["effect_id"] for r in rows} | set(extra_ids)
    ids |= {q for r in rows for p in QUALIFIER_ITEM_PIDS for q in _qual_items(r, p)}
    labels, kinds = labels_for(conn, ids), kinds_for(conn, ids)
    v, rules = vocabulary(), gr.load_rules()
    rights = source_rights(conn, rows)
    return [claim_out(r, labels, kinds, v, rules, rights) for r in rows]


RIGHTS_SQL = """SELECT src.source_key, COALESCE(m.rights, src.rights) AS rights
  FROM evidence.source src
  LEFT JOIN LATERAL (SELECT v.rights FROM evidence.source_metadata_version v
                      WHERE v.source_id = src.id ORDER BY v.version DESC LIMIT 1) m ON true
 WHERE src.source_key = ANY(:k)"""


def source_rights(conn: Connection, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Licence of each asserting source (audit2 N9), as ``/v1/sources`` records it (latest source metadata version,
    else the source row) and normalised with the atlas rule (``reva.atlas.snapshot.normalise_rights``: explicit
    flags first, then the licence text; ``restricted`` for non-commercial / share-alike / no-derivatives / not-open
    terms). A source without registry rights falls back to the rights recorded with the claim's raw artifact
    (``meta.artifact.rights``, key ``sha:<raw sha256>``). Unknown terms are ``None``, never "open"."""
    from reva.atlas.snapshot import normalise_rights

    keys = sorted({str(r["source_key"]) for r in rows})
    if not keys:
        return {}
    out: dict[str, Any] = {}
    for k, raw in conn.execute(text(RIGHTS_SQL), {"k": keys}).all():
        n = normalise_rights(raw, basis="source_registry")
        if n is not None:
            out[k] = n
    need = sorted({str(r["raw_sha256"]) for r in rows if r["source_key"] not in out and r.get("raw_sha256")})
    if need:
        for sha, raw in conn.execute(
            text("SELECT sha256, rights FROM meta.artifact WHERE sha256 = ANY(:s)"), {"s": need}
        ).all():
            n = normalise_rights(raw, basis="raw_artifact")
            if n is not None:
                out["sha:" + sha] = n
    return out


def licence_warning(claims: Iterable[Mapping[str, Any]]) -> dict[str, str] | None:
    """One ``licence_restricted`` warning naming every restricted licence among the claims' sources (as LIC-1)."""
    seen: dict[str, Mapping[str, Any]] = {}
    for c in claims:
        rr = (c.get("source") or {}).get("rights")
        if isinstance(rr, Mapping) and rr.get("restricted"):
            seen.setdefault(str(c["source"]["source_key"]), rr)
    if not seen:
        return None
    parts = []
    for k, x in sorted(seen.items()):
        terms = [
            t
            for t, on in (
                ("non-commercial", x.get("non_commercial")),
                ("share-alike", x.get("share_alike")),
                ("no derivatives", x.get("no_derivatives")),
                ("no charging for access", x.get("commercial_use") == "restricted"),
                ("not an open licence", x.get("open_licence") is False),
            )
            if on
        ]
        parts.append(f"{k}: {x.get('licence')} ({', '.join(terms)})")
    return {
        "code": "licence_restricted",
        "message": (
            "Some claims come from sources under restricted terms; reuse must respect them: " + "; ".join(parts)
        )[:1000],
    }


# ---------------------------------------------------------------------------------------------- event
def known(conn: Connection, qid: str) -> bool:
    check_id(qid)
    return bool(
        conn.execute(
            text("""SELECT EXISTS (SELECT 1 FROM kg.entity WHERE qid = :q)
                       OR EXISTS (SELECT 1 FROM kg.statement WHERE subject_qid = :q)
                       OR EXISTS (SELECT 1 FROM kg.statement WHERE object_qid = :q)
                       OR EXISTS (SELECT 1 FROM kg.causal_claim WHERE cause_id = :q)
                       OR EXISTS (SELECT 1 FROM kg.causal_claim WHERE effect_id = :q)
                       OR EXISTS (SELECT 1 FROM kg.causal_candidate WHERE cause_id = :q)
                       OR EXISTS (SELECT 1 FROM kg.causal_candidate WHERE effect_id = :q)"""),
            {"q": qid},
        ).scalar_one()
    )


def event(conn: Connection, qid: str, *, origin: str | None = None) -> dict[str, Any] | None:
    """Causal summary of an event: labels, kind, dates, counts of causes/effects by family and grade, context
    edges (not causal), candidate count."""
    check_id(qid)
    check_origin(origin)
    if not known(conn, qid):
        return None
    labels, kinds = labels_for(conn, [qid]), kinds_for(conn, [qid])
    dates = fetch_event_dates(conn, [qid], origin).get(qid)
    counts: dict[str, dict[str, dict[str, int]]] = {"causes": {}, "effects": {}}
    origin_sql = " AND c.origin = :origin" if origin else ""
    v = vocabulary()
    # coded outcomes (how an event ended) are never causes nor consequences: their own bucket, "outcome"
    for side, col in (("causes", "effect_id"), ("effects", "cause_id")):
        for fam, grade, n in conn.execute(
            text(f"""SELECT CASE WHEN c.relation = ANY(CAST(:outc AS text[])) THEN 'outcome' ELSE c.family END,
                            COALESCE(g.grade, 'ungraded'), count(*)
                       FROM kg.causal_claim_current c LEFT JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                      WHERE c.{col} = :q{origin_sql} GROUP BY 1, 2"""),
            {"q": qid, "origin": origin, "outc": sorted(v.outcome_relations)},
        ).all():
            counts[side].setdefault(fam, {})[grade] = int(n)
    ctx_pids = sorted(v.context)
    ctx_rows = (
        conn.execute(
            text(f"""SELECT s.id, s.subject_qid, s.property_pid, s.object_qid, s.source_key, s.statement_key, s.origin
                       FROM kg.statement_current s
                      WHERE (s.subject_qid = :q OR s.object_qid = :q) AND s.object_qid IS NOT NULL
                        AND s.property_pid = ANY(:p){" AND s.origin = :origin" if origin else ""}
                      ORDER BY s.id LIMIT :lim"""),
            {"q": qid, "p": ctx_pids, "origin": origin, "lim": MAX_CONTEXT + 1},
        )
        .mappings()
        .all()
    )
    ctx_ids = {r["subject_qid"] for r in ctx_rows} | {r["object_qid"] for r in ctx_rows}
    clabels, ckinds = labels_for(conn, ctx_ids), kinds_for(conn, ctx_ids)
    context = [
        {
            "subject": _ref(r["subject_qid"], clabels, ckinds),
            "property": r["property_pid"],
            "label": v.context[r["property_pid"]],
            "object": _ref(r["object_qid"], clabels, ckinds),
            "source_key": r["source_key"],
            "statement_key": r["statement_key"],
            "origin": r["origin"],
            "causal": False,
        }
        for r in ctx_rows[:MAX_CONTEXT]
    ]
    cand = conn.execute(
        text(f"""SELECT count(*) FROM kg.causal_candidate_current c
                  WHERE (c.cause_id = :q OR c.effect_id = :q){origin_sql}"""),
        {"q": qid, "origin": origin},
    ).scalar_one()
    origins = [
        o
        for (o,) in conn.execute(
            text(f"""SELECT DISTINCT c.origin FROM kg.causal_claim_current c
                      WHERE (c.cause_id = :q OR c.effect_id = :q){origin_sql}"""),
            {"q": qid, "origin": origin},
        ).all()
    ]
    return {
        "id": qid,
        "event": _ref(qid, labels, kinds),
        "dates": dates.as_dict() if dates else {"start": None, "end": None},
        "causes": counts["causes"],
        "effects": counts["effects"],
        "context": context,
        "context_truncated": len(ctx_rows) > MAX_CONTEXT,
        "context_note": CONTEXT_NOTE,
        "candidates": int(cand),
        "candidates_note": CANDIDATE_NOTE,
        "note": CLAIM_NOTE,
        "origins": sorted(origins),
    }


def causes(conn: Connection, qid: str, **kw: Any) -> dict[str, Any]:
    return _side(conn, qid, "c.effect_id = :q", **kw)


def effects(conn: Connection, qid: str, **kw: Any) -> dict[str, Any]:
    return _side(conn, qid, "c.cause_id = :q", **kw)


def _side(
    conn: Connection,
    qid: str,
    where: str,
    *,
    origin: str | None = None,
    relation: Sequence[str] | None = None,
    family: str | None = None,
    min_grade: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    check_id(qid)
    check_origin(origin)
    rows, truncated = _claims(
        conn, where, {"q": qid}, origin=origin, relation=relation, family=family, min_grade=min_grade, limit=limit
    )
    out = _render(conn, rows, [qid])
    return {"id": qid, "claims": out, "truncated": truncated, "note": CLAIM_NOTE}


def why(conn: Connection, qid: str, *, origin: str | None = None, min_grade: str | None = None) -> dict[str, Any]:
    """Causes of an event by lane (immediate / underlying / contributing / general / coded / reaction /
    prevention), mechanisms quoted verbatim, dissent (contested and denying claims)."""
    check_id(qid)
    check_origin(origin)
    rows, truncated = _claims(conn, "c.effect_id = :q", {"q": qid}, origin=origin, min_grade=min_grade)
    out = _render(conn, rows, [qid])
    v = vocabulary()
    # audit M1 remainder: a claim with a class endpoint (by the KG's own P279 / P31 statements) is a generic statement
    # about a kind of event, not a cause of this event: filed in its own lane, counted apart.
    classes = class_nodes(conn, {c["cause"]["id"] for c in out} | {qid}, origin)
    lanes: dict[str, list[dict[str, Any]]] = {lane: [] for lane in LANES}
    dissent = []
    mechanisms = []
    for c in out:
        ends = [side for side in ("cause", "effect") if c[side]["id"] in classes]
        if ends:
            c["generic"] = {"class_endpoints": ends, "rule": CLASS_NODE_RULE}
        if c["stance"] == "denies":
            dissent.append(c)
            continue
        lanes[GENERIC_LANE if ends and c["lane"] != OUTCOME_LANE else c["lane"]].append(c)
        if c["grade"] and c["grade"]["contested_by"]:
            dissent.append(c)
        if c["mechanism"]:
            mechanisms.append(
                {"claim_key": c["claim_key"], "mechanism": c["mechanism"], "source_key": c["source"]["source_key"]}
            )
    labels = labels_for(conn, [qid])
    lab = labels.get(qid, {})
    name_en, name_fr = lab.get("en") or lab.get("fr") or qid, lab.get("fr") or lab.get("en") or qid
    k = len(lanes[OUTCOME_LANE])  # this record is the coded outcome of k events: how they ended, not caused by them
    gen = len(lanes[GENERIC_LANE])
    n = sum(len(x) for lane, x in lanes.items() if lane not in (OUTCOME_LANE, GENERIC_LANE))
    gen_en = (
        f" {gen} generic statement(s) (class level: the cause or the effect is a kind of event, not an event) are "
        "listed apart; they are not causes of this event."
        if gen
        else ""
    )
    gen_fr = (
        f" {gen} énoncé(s) générique(s) (classe : la cause ou l'effet est un type d'événement, pas un événement) "
        "listé(s) à part ; ce ne sont pas des causes de cet événement."
        if gen
        else ""
    )
    out_en = (
        f" {k} coded outcome claim(s): a dataset codes {name_en} as how an event ended; an outcome is not a cause."
        if k
        else ""
    )
    out_fr = (
        f" {k} issue(s) codée(s) : un jeu de données code {name_fr} comme la façon dont un événement s'est terminé ; "
        "une issue n'est pas une cause."
        if k
        else ""
    )
    return {
        "id": qid,
        "label": name_en,
        "lanes": [
            {"lane": lane, "label": dict(v.lanes.get(lane, {})), "claims": lanes[lane]} for lane in LANES if lanes[lane]
        ],
        "mechanisms": mechanisms,
        "dissent": dissent,
        "truncated": truncated,
        "causal_claims": n,
        "outcome_claims": k,
        "generic_claims": gen,
        "summary": f"{n} sourced causal claim(s) about {name_en}; each is attributed to its source. "
        + ("No cause is asserted by any loaded source." if n == 0 else CLAIM_NOTE)
        + gen_en
        + out_en,
        "summary_fr": f"{n} affirmation(s) causale(s) sourcée(s) sur {name_fr} ; chacune est attribuée à sa source."
        + (" Aucune cause n'est affirmée par les sources chargées." if n == 0 else " REVA n'établit pas de causalité.")
        + gen_fr
        + out_fr,
        "note": CLAIM_NOTE,
    }


# ---------------------------------------------------------------------------------------------- chain
def _edges(
    conn: Connection, nodes: Sequence[str], *, forward: bool, origin: str | None, cap: int
) -> tuple[list[tuple[str, str, str | None, bool]], bool]:
    """Causal edges out of (forward) / into ``nodes``: (cause, effect, grade, quarantined). Relations excluded from
    chains (relations.yaml ``chain_excluded``) and self-loops are not returned; ``quarantined`` marks a claim whose
    grade carries a chain-quarantine fact (cycle member, attributed or nature-qualified statement)."""
    col = "cause_id" if forward else "effect_id"
    rows = conn.execute(
        text(f"""SELECT DISTINCT c.cause_id, c.effect_id, g.grade,
                        COALESCE(g.rule_ids && CAST(:qf AS text[]), false) AS quarantined
                   FROM kg.causal_claim_current c LEFT JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                  WHERE c.{col} = ANY(:n) AND c.stance = 'asserts' AND c.family = 'cause'
                    AND c.relation <> ALL(CAST(:nx AS text[]))
                    AND c.cause_id <> c.effect_id{" AND c.origin = :origin" if origin else ""}
                  LIMIT :cap"""),
        {
            "n": list(nodes),
            "origin": origin,
            "cap": cap + 1,
            "qf": list(gr.CHAIN_QUARANTINE_FACTS),
            "nx": sorted(vocabulary().chain_excluded),
        },
    ).all()
    return [(a, b, g, bool(q)) for a, b, g, q in rows[:cap]], len(rows) > cap


CLASS_NODE_RULE = (
    "intermediate nodes exclude classes: items with a P279 (subclass of) statement, or items that are only "
    "targets of P31 (instance of) and have no P31 of their own (KG statements, not names)"
)


UNLOADED_NODE_RULE = (
    "intermediate nodes exclude entities not loaded in the knowledge graph (no kg.entity row and no statement about "
    "them: no label, kind or dates, so REVA cannot say what they are); the endpoints asked for are kept"
)


def unloaded_nodes(conn: Connection, ids: Iterable[str]) -> set[str]:
    """The ids the KG knows nothing about — no ``kg.entity`` row and no statement about them, the loader's
    ``unknown_endpoint_claims`` rule (audit M2): never traversed as intermediate chain nodes."""
    qs = sorted(set(ids))
    if not qs:
        return set()
    return {
        q
        for (q,) in conn.execute(
            text("""SELECT q.id FROM unnest(CAST(:q AS text[])) q(id)
                     WHERE NOT EXISTS (SELECT 1 FROM kg.entity e WHERE e.qid = q.id)
                       AND NOT EXISTS (SELECT 1 FROM kg.statement s WHERE s.subject_qid = q.id)"""),
            {"q": qs},
        ).all()
    }


def class_nodes(conn: Connection, ids: Iterable[str], origin: str | None = None) -> set[str]:
    """The ids the KG shows to be classes, not events: a P279 *subclass of* statement of their own, or targets
    of P31 *instance of* with no P31 of their own. Ids the KG knows nothing about are not classes."""
    qs = sorted(set(ids))
    if not qs:
        return set()
    o_sql = " AND s.origin = :origin" if origin else ""
    return {
        q
        for (q,) in conn.execute(
            text(f"""SELECT q.id FROM unnest(CAST(:q AS text[])) q(id)
                      WHERE EXISTS (SELECT 1 FROM kg.statement_current s
                                     WHERE s.subject_qid = q.id AND s.property_pid = 'P279'{o_sql})
                         OR (EXISTS (SELECT 1 FROM kg.statement_current s
                                      WHERE s.object_qid = q.id AND s.property_pid = 'P31'{o_sql})
                             AND NOT EXISTS (SELECT 1 FROM kg.statement_current s
                                              WHERE s.subject_qid = q.id AND s.property_pid = 'P31'{o_sql}))"""),
            {"q": qs, "origin": origin},
        ).all()
    }


def chain(
    conn: Connection,
    frm: str,
    to: str,
    *,
    max_hops: int = 4,
    min_grade: str = "D",
    max_paths: int = 5,
    origin: str | None = None,
    time_budget_s: float = CHAIN_TIME_BUDGET_S,
) -> dict[str, Any]:
    """Causal chains from cause ``frm`` to effect ``to`` (bidirectional BFS over graded claims, minimal length,
    up to ``max_paths``, best chain grade first)."""
    check_id(frm)
    check_id(to)
    check_origin(origin)
    check_grade(min_grade)
    if frm == to:
        raise ValidationProblem("from and to are the same event")
    if not 1 <= max_hops <= MAX_HOPS:
        raise ValidationProblem("max_hops out of range", max=MAX_HOPS)
    if not 1 <= max_paths <= MAX_PATHS:
        raise ValidationProblem("max_paths out of range", max=MAX_PATHS)
    t0 = time.perf_counter()
    allowed = set(_grades_at_least(min_grade) or gr.GRADES)
    dist = ({frm: 0}, {to: 0})
    links: tuple[dict[str, set[str]], dict[str, set[str]]] = (defaultdict(set), defaultdict(set))
    frontier = ({frm}, {to})
    depth = [0, 0]
    search: dict[str, Any] = {
        "max_hops": max_hops,
        "min_grade": min_grade,
        "levels_expanded": 0,
        "edges_scanned": 0,
        "ungraded_skipped": 0,
        "below_min_grade_skipped": 0,
        "quarantined_skipped": 0,
        "class_nodes_skipped": 0,
        "class_nodes_sample": [],
        "unloaded_nodes_skipped": 0,
        "unloaded_nodes_sample": [],
        "node_rule": CLASS_NODE_RULE + "; " + UNLOADED_NODE_RULE,
        "truncated": False,
        "truncation_reasons": [],
        "time_budget_ms": int(time_budget_s * 1000),
    }
    best_len: int | None = None
    meet: set[str] = set()
    is_class: dict[str, bool] = {}
    skipped_classes: set[str] = set()
    is_unloaded: dict[str, bool] = {}
    skipped_unloaded: set[str] = set()
    # Identity hops: records of the same event joined by evidenced same_as links (reva.causal.identity) are
    # crossed at no causal cost, so a chain can continue from one source's record of an event to another's.
    ident = idn.load(conn, origin=origin)
    id_pred: tuple[dict[str, str], dict[str, str]] = ({}, {})
    search["identity"] = {
        "same_as_links_loaded": len(ident.links),
        "identity_hops_note": IDENTITY_HOP_NOTE,
    }
    if ident.canon(frm) == ident.canon(to):
        search["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        lab = labels_for(conn, {frm, to})
        return {
            "from": _ref(frm, lab, {}),
            "to": _ref(to, lab, {}),
            "found": False,
            "paths": [],
            "note": CHAIN_NOTE,
            "note_fr": CHAIN_NOTE_FR,
            "not_found_reason": "from and to are records of the same event (evidenced same_as link)",
            "same_as": [x.as_dict() for x in ident.links_of(frm) if to in (x.left, x.right)],
            "search": search,
        }

    def close(side: int, nodes: Iterable[str], d: int) -> set[str]:
        """Add the same_as cluster members of ``nodes`` at distance ``d`` (identity predecessor recorded)."""
        added: set[str] = set()
        for n in sorted(nodes):
            for m in ident.members(n):
                if m not in dist[side]:
                    dist[side][m] = d
                    id_pred[side][m] = n
                    added.add(m)
        return added

    close(0, [frm], 0)
    close(1, [to], 0)
    frontier = (set(dist[0]), set(dist[1]))
    while depth[0] + depth[1] < max_hops and frontier[0] and frontier[1]:
        if time.perf_counter() - t0 > time_budget_s:
            search["truncated"] = True
            search["truncation_reasons"].append("time budget exhausted")
            break
        side = 0 if len(frontier[0]) <= len(frontier[1]) else 1
        nodes = sorted(frontier[side])
        if len(nodes) > MAX_FRONTIER:
            search["truncated"] = True
            search["truncation_reasons"].append(f"frontier capped at {MAX_FRONTIER} nodes")
            nodes = nodes[:MAX_FRONTIER]
        edges, cut = _edges(conn, nodes, forward=side == 0, origin=origin, cap=MAX_EDGES_PER_LEVEL)
        if cut:
            search["truncated"] = True
            search["truncation_reasons"].append(f"edges per level capped at {MAX_EDGES_PER_LEVEL}")
        search["levels_expanded"] += 1
        search["edges_scanned"] += len(edges)
        d = depth[side]
        nxt: set[str] = set()
        usable: set[tuple[str, str]] = set()
        for a, b, g, quarantined in edges:
            if g is None:
                search["ungraded_skipped"] += 1
            elif quarantined:
                search["quarantined_skipped"] += 1
            elif g not in allowed:
                search["below_min_grade_skipped"] += 1
            else:
                usable.add((a, b))
        reached = {(b if side == 0 else a) for a, b in usable} - {frm, to} - set(is_class)
        found = class_nodes(conn, reached, origin)
        missing = unloaded_nodes(conn, reached - found)
        is_class.update({q: q in found for q in reached})
        is_unloaded.update({q: q in missing for q in reached})
        for a, b in usable:
            u, w = (a, b) if side == 0 else (b, a)
            if is_class.get(w):  # an intermediate class node (endpoints as asked are kept)
                skipped_classes.add(w)
                continue
            if is_unloaded.get(w):  # audit M2: an entity the KG has not loaded is never an intermediate node
                skipped_unloaded.add(w)
                continue
            if w not in dist[side]:
                dist[side][w] = d + 1
                nxt.add(w)
            if dist[side][w] == d + 1:
                links[side][w].add(u)
        nxt |= close(side, nxt, d + 1)
        depth[side] += 1
        frontier = (nxt, frontier[1]) if side == 0 else (frontier[0], nxt)
        common = set(dist[0]) & set(dist[1])
        if common:
            best_len = min(dist[0][m] + dist[1][m] for m in common)
            meet = {m for m in common if dist[0][m] + dist[1][m] == best_len}
            break
    search["class_nodes_skipped"] = len(skipped_classes)
    search["class_nodes_sample"] = sorted(skipped_classes)[:20]
    search["unloaded_nodes_skipped"] = len(skipped_unloaded)
    search["unloaded_nodes_sample"] = sorted(skipped_unloaded)[:20]
    search["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    labels_ids = {frm, to}
    if best_len is None:
        lab = labels_for(conn, labels_ids)
        reason = "no causal chain within max_hops among claims graded at least " + min_grade
        if search["truncated"] or search["ungraded_skipped"] or search["below_min_grade_skipped"]:
            reason += " (bounds, ungraded claims or the grade filter may hide one)"
        if search["quarantined_skipped"]:
            reason += " (claims in cycles, self-loops, or attributed/qualified/hedged by Wikidata are not composed)"
        if skipped_classes:
            reason += f" ({len(skipped_classes)} class node(s) were not traversed)"
        if skipped_unloaded:
            reason += (
                f" ({len(skipped_unloaded)} entit(y/ies) not loaded in the knowledge graph were not traversed: "
                + ", ".join(sorted(skipped_unloaded)[:5])
                + ")"
            )
        outcome_ends = _outcome_records(conn, [frm, to], origin)
        if outcome_ends:
            reason = (
                f"{' and '.join(outcome_ends)} "
                f"{'is a coded outcome record' if len(outcome_ends) == 1 else 'are coded outcome records'} "
                "(how an event ended according to a dataset): outcomes are never composed into causal chains "
                "(registry/causal/relations.yaml chain_excluded); " + reason
            )
        return {
            "from": _ref(frm, lab, {}),
            "to": _ref(to, lab, {}),
            "found": False,
            "paths": [],
            "note": CHAIN_NOTE,
            "note_fr": CHAIN_NOTE_FR,
            "not_found_reason": reason,
            "search": search,
        }

    # enumerate minimal paths through the meeting nodes (bounded)
    def walk(node: str, side: int) -> list[list[str]]:
        root = frm if side == 0 else to
        out: list[list[str]] = []
        stack: list[tuple[str, list[str]]] = [(node, [node])]
        while stack and len(out) < MAX_PATH_CANDIDATES:
            n, acc = stack.pop()
            if n == root:
                out.append(acc)
                continue
            preds = set(links[side][n])
            if n in id_pred[side]:
                preds.add(id_pred[side][n])
            for p in sorted(preds - set(acc), reverse=True):
                stack.append((p, [*acc, p]))
        return out

    paths: list[list[str]] = []
    for m in sorted(meet):
        for left in walk(m, 0):
            for right in walk(m, 1):
                paths.append(list(reversed(left)) + right[1:])
                if len(paths) >= MAX_PATH_CANDIDATES:
                    break
    if len(paths) >= MAX_PATH_CANDIDATES:
        search["truncated"] = True
        search["truncation_reasons"].append(f"path enumeration capped at {MAX_PATH_CANDIDATES}")

    def same_event(a: str, b: str) -> bool:
        return a != b and ident.canon(a) == ident.canon(b)

    pairs = sorted({(p[i], p[i + 1]) for p in paths for i in range(len(p) - 1) if not same_event(p[i], p[i + 1])})
    rows = (
        conn.execute(
            text(f"""SELECT {_CLAIM_SELECT}
                       FROM kg.causal_claim_current c
                       JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                       JOIN unnest(CAST(:a AS text[]), CAST(:b AS text[])) AS h(cause_id, effect_id)
                         ON h.cause_id = c.cause_id AND h.effect_id = c.effect_id
                      WHERE c.stance = 'asserts' AND c.family = 'cause' AND g.grade = ANY(:grades)
                        AND NOT (g.rule_ids && CAST(:qf AS text[])) AND c.relation <> ALL(CAST(:nx AS text[]))
                            {" AND c.origin = :origin" if origin else ""}
                      ORDER BY {_GRADE_ORDER}, c.id"""),
            {
                "a": [p[0] for p in pairs],
                "b": [p[1] for p in pairs],
                "grades": sorted(allowed),
                "origin": origin,
                "qf": list(gr.CHAIN_QUARANTINE_FACTS),
                "nx": sorted(vocabulary().chain_excluded),
            },
        )
        .mappings()
        .all()
    )
    rendered = _render(conn, [dict(r) for r in rows], labels_ids)
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in rendered:
        by_pair[(c["cause"]["id"], c["effect"]["id"])].append(c)
    labels, kinds = labels_for(conn, {x for p in paths for x in p}), kinds_for(conn, {x for p in paths for x in p})
    built: list[tuple[tuple[int, int, list[str]], dict[str, Any]]] = []
    # Two meeting nodes joined by an identity hop (both records of one event reached from both sides) enumerate the
    # same chain twice; a chain is identified by its sequence of hops (claim keys / same_as statement keys).
    seen: set[tuple[tuple[str, ...], ...]] = set()
    duplicates = 0
    for p in paths:
        hops: list[dict[str, Any]] = []
        for i in range(len(p) - 1):
            if same_event(p[i], p[i + 1]):
                hops.append(_identity_hop(p[i], p[i + 1], ident, labels, kinds))
                continue
            cl = by_pair.get((p[i], p[i + 1]), [])
            if not cl:
                break
            hops.append(
                {
                    "kind": "claim",
                    "from": _ref(p[i], labels, kinds),
                    "to": _ref(p[i + 1], labels, kinds),
                    "hop_grade": gr.best(c["grade"]["grade"] for c in cl),
                    "claims": cl,
                }
            )
        if len(hops) != len(p) - 1:
            continue  # a claim changed between the search and the read
        causal = [h for h in hops if h["kind"] == "claim"]
        if not causal:
            continue
        signature = tuple(_hop_signature(h) for h in hops)
        if signature in seen:
            duplicates += 1
            continue
        seen.add(signature)
        grades: list[str] = [h["hop_grade"] for h in causal]
        cg = gr.weakest(grades)
        n_id = len(hops) - len(causal)
        sources = sorted({c["source"]["source_key"] for h in causal for c in h["claims"]})
        id_en = (
            f" It crosses {n_id} identity link(s): records of the same event in two sources, joined by an evidenced "
            "same_as link (shown with its evidence) -- an identity, not a causal claim."
            if n_id
            else ""
        )
        id_fr = (
            f" Elle traverse {n_id} lien(s) d'identité : enregistrements du même événement dans deux sources, reliés "
            "par un lien same_as fondé sur une preuve (montrée) -- une identité, pas une affirmation causale."
            if n_id
            else ""
        )
        built.append(
            (
                (gr.GRADES.index(cg), sum(gr.GRADES.index(g) for g in grades), p),
                {
                    "length": len(causal),
                    "identity_hops": n_id,
                    "cross_source": len(sources) > 1,
                    "chain_grade": cg,
                    "weakest_hop": hops.index(causal[grades.index(cg)]),
                    "hops": hops,
                    "wording": f"Composition by REVA of {len(causal)} separately asserted claim(s); chain grade = "
                    f"weakest hop ({cg}). Each hop is asserted by its own source; REVA does not assert the chain."
                    + id_en,
                    "wording_fr": f"Composition par REVA de {len(causal)} affirmation(s) énoncée(s) séparément ; note "
                    f"de la chaîne = maillon le plus faible ({cg}). REVA n'affirme pas la chaîne." + id_fr,
                },
            )
        )
    built.sort(key=lambda x: x[0])
    search["duplicate_paths_merged"] = duplicates
    lab = labels_for(conn, labels_ids)
    return {
        "from": _ref(frm, lab, kinds),
        "to": _ref(to, lab, kinds),
        "found": bool(built),
        "paths": [b for _, b in built[:max_paths]],
        "paths_found": len(built),
        "note": CHAIN_NOTE,
        "note_fr": CHAIN_NOTE_FR,
        "not_found_reason": None if built else "claims changed during the search",
        "search": search,
    }


def _outcome_records(conn: Connection, ids: Sequence[str], origin: str | None) -> list[str]:
    """Those of ``ids`` that are the outcome end of a coded-outcome claim (``<event>:outcome`` records)."""
    v = vocabulary()
    found = {
        q
        for (q,) in conn.execute(
            text(f"""SELECT DISTINCT c.effect_id FROM kg.causal_claim_current c
                      WHERE c.effect_id = ANY(CAST(:ids AS text[])) AND c.relation = ANY(CAST(:outc AS text[]))
                            {" AND c.origin = :origin" if origin else ""}"""),
            {"ids": list(ids), "outc": sorted(v.outcome_relations), "origin": origin},
        ).all()
    }
    return [q for q in ids if q in found]


def _hop_signature(hop: Mapping[str, Any]) -> tuple[str, ...]:
    """What a hop is made of: its claims (claim keys), or its two records and identity links (same_as keys)."""
    if hop["kind"] == "identity":
        keys = sorted(str(x.get("statement_key") or x) for x in hop["same_as"])
        return ("identity", hop["from"]["id"], hop["to"]["id"], *keys)
    return ("claim", *sorted(c["claim_key"] for c in hop["claims"]))


IDENTITY_HOP_NOTE = (
    "An identity hop joins two records of the same event (an evidenced events:same_as link: shared identifier or "
    "provider crosswalk, never name or date matching); it is not a causal claim and adds no causal hop."
)


def _identity_hop(
    a: str, b: str, ident: idn.Identity, labels: Mapping[str, Mapping[str, str]], kinds: Mapping[str, str]
) -> dict[str, Any]:
    """An identity hop of a chain: the same_as links between ``a`` and ``b`` (direct, else the cluster's links)."""
    direct = [x for x in ident.links if {x.left, x.right} == {a, b}]
    members = set(ident.members(a))
    used = direct or [x for x in ident.links if x.left in members and x.right in members]
    return {
        "kind": "identity",
        "from": _ref(a, labels, kinds),
        "to": _ref(b, labels, kinds),
        "relation": "same_as",
        "same_as": [x.as_dict() for x in used],
        "note": IDENTITY_HOP_NOTE,
    }


# ---------------------------------------------------------------------------------------------- consistency
def _sccs(adj: Mapping[str, Sequence[str]], deadline: float) -> tuple[list[list[str]], bool]:
    """Strongly connected components of size >= 2 (iterative Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in sorted(adj):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            if time.perf_counter() > deadline:
                return out, True
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on.add(v)
            succ = adj.get(v, ())
            if i < len(succ):
                work.append((v, i + 1))
                w = succ[i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out, False


def _cycle_in(comp: Sequence[str], adj: Mapping[str, Sequence[str]]) -> list[str]:
    """One cycle inside a strongly connected component (BFS back to its first node)."""
    members = set(comp)
    start = comp[0]
    prev: dict[str, str] = {}
    queue = [start]
    seen = {start}
    while queue:
        u = queue.pop(0)
        for w in adj.get(u, ()):
            if w not in members:
                continue
            if w == start:
                path = [u]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                return [*reversed(path), start]
            if w not in seen:
                seen.add(w)
                prev[w] = u
                queue.append(w)
    return [start]


def consistency(conn: Connection, *, origin: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Self-loops, cycles, temporal violations and contested claims among current claims (counts + samples)."""
    check_origin(origin)
    if not 1 <= limit <= 200:
        raise ValidationProblem("limit out of range", max=200)
    t0 = time.perf_counter()
    o_sql = " AND c.origin = :origin" if origin else ""
    self_rows, self_trunc = _claims(conn, "c.cause_id = c.effect_id", {}, origin=origin, limit=limit)
    n_self = conn.execute(
        text(f"SELECT count(*) FROM kg.causal_claim_current c WHERE c.cause_id = c.effect_id{o_sql}"),
        {"origin": origin},
    ).scalar_one()
    temp_rows, _ = _claims(conn, "g.temporal_check = 'inconsistent'", {}, origin=origin, limit=limit)
    n_temp = conn.execute(
        text(f"""SELECT count(*) FROM kg.causal_claim_current c JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                  WHERE g.temporal_check = 'inconsistent'{o_sql}"""),
        {"origin": origin},
    ).scalar_one()
    cont_rows, _ = _claims(conn, "cardinality(g.contested_by) > 0", {}, origin=origin, limit=limit)
    n_cont = conn.execute(
        text(f"""SELECT count(*) FROM kg.causal_claim_current c JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                  WHERE cardinality(g.contested_by) > 0{o_sql}"""),
        {"origin": origin},
    ).scalar_one()
    n_ungraded = conn.execute(
        text(f"""SELECT count(*) FROM kg.causal_claim_current c
                  WHERE NOT EXISTS (SELECT 1 FROM kg.causal_grade g WHERE g.claim_row_id = c.id){o_sql}"""),
        {"origin": origin},
    ).scalar_one()
    edges = conn.execute(
        text(f"""SELECT DISTINCT c.cause_id, c.effect_id FROM kg.causal_claim_current c
                  WHERE c.stance = 'asserts' AND c.family = 'cause' AND c.cause_id <> c.effect_id{o_sql}
                  LIMIT :cap"""),
        {"origin": origin, "cap": MAX_CYCLE_EDGES + 1},
    ).all()
    truncated_edges = len(edges) > MAX_CYCLE_EDGES
    adj: dict[str, list[str]] = defaultdict(list)
    for a, b in edges[:MAX_CYCLE_EDGES]:
        adj[a].append(b)
    for k in adj:
        adj[k].sort()
    comps, timed_out = _sccs(adj, t0 + CONSISTENCY_TIME_BUDGET_S)
    comps.sort(key=lambda c: (-len(c), c))
    cycle_ids = {x for c in comps[:limit] for x in c}
    labels, kinds = labels_for(conn, cycle_ids), kinds_for(conn, cycle_ids)
    cycles = [
        {
            "size": len(c),
            "members": [_ref(x, labels, kinds) for x in c[:50]],
            "example_cycle": [_ref(x, labels, kinds) for x in _cycle_in(c, adj)],
        }
        for c in comps[:limit]
    ]
    return {
        "self_loops": {"count": int(n_self), "sample": _render(conn, self_rows), "truncated": self_trunc},
        "temporal_violations": {"count": int(n_temp), "sample": _render(conn, temp_rows)},
        "contested": {"count": int(n_cont), "sample": _render(conn, cont_rows)},
        "cycles": {
            "count": len(comps),
            "sample": cycles,
            "edges_examined": min(len(edges), MAX_CYCLE_EDGES),
            "truncated": truncated_edges or timed_out,
            "note": "A cycle means sources assert causation in both directions along a loop; REVA keeps every "
            "claim and does not resolve it.",
        },
        "ungraded_claims": int(n_ungraded),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "note": CLAIM_NOTE,
    }


# ---------------------------------------------------------------------------------------------- candidates
def candidates(
    conn: Connection,
    *,
    event_id: str | None = None,
    origin: str | None = None,
    limit: int = 100,
    after_id: int | None = None,
) -> dict[str, Any]:
    """Quarantined T5/T6 candidates, separate from claims and labelled unsourced."""
    check_origin(origin)
    if event_id is not None:
        check_id(event_id)
    if not 1 <= limit <= MAX_CLAIMS:
        raise ValidationProblem("limit out of range", max=MAX_CLAIMS)
    cond = ["true"]
    p: dict[str, Any] = {"lim": limit + 1}
    if event_id:
        cond.append("(c.cause_id = :q OR c.effect_id = :q)")
        p["q"] = event_id
    if origin:
        cond.append("c.origin = :origin")
        p["origin"] = origin
    if after_id is not None:
        cond.append("c.id > :after")
        p["after"] = after_id
    rows = (
        conn.execute(
            text(f"""SELECT c.id, c.candidate_key, c.origin, c.cause_id, c.effect_id, c.relation, c.family, c.polarity,
                            c.tier, c.extractor_id, c.extractor_version, c.source_key, c.source_revision,
                            c.source_sentence, c.selector, c.raw_sha256, c.mechanism, c.retrieved_at, c.recorded_at,
                            c.promotion_status
                       FROM kg.causal_candidate_current c WHERE {" AND ".join(cond)} ORDER BY c.id LIMIT :lim"""),
            p,
        )
        .mappings()
        .all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    ids = {r["cause_id"] for r in rows} | {r["effect_id"] for r in rows}
    labels, kinds = labels_for(conn, ids), kinds_for(conn, ids)
    v = vocabulary()
    out = []
    for r in rows:
        rel = v.relation(r["relation"])
        out.append(
            {
                "candidate_key": r["candidate_key"],
                "row_id": r["id"],
                "cause": _ref(r["cause_id"], labels, kinds),
                "effect": _ref(r["effect_id"], labels, kinds),
                "relation": r["relation"],
                "relation_label": {"en": rel.label_en, "fr": rel.label_fr},
                "family": r["family"],
                "polarity": r["polarity"],
                "tier": r["tier"],
                "extractor": {"id": r["extractor_id"], "version": r["extractor_version"]},
                "source": {
                    "source_key": r["source_key"],
                    "source_revision": r["source_revision"],
                    "selector": r["selector"],
                    "raw_sha256": r["raw_sha256"],
                    "retrieved_at": r["retrieved_at"],
                },
                "source_sentence": r["source_sentence"],
                "mechanism": r["mechanism"],
                "promotion_status": r["promotion_status"],
                "label": "unsourced candidate (non sourcé — candidat)",
                "origin": r["origin"],
                "recorded_at": r["recorded_at"],
            }
        )
    return {
        "event": event_id,
        "candidates": out,
        "next_after_id": rows[-1]["id"] if more and rows else None,
        "note": CANDIDATE_NOTE,
    }
