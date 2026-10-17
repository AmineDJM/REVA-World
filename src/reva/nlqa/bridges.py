"""Entity-resolution bridges of the V8 « Demander » (agent V8ASK): stricter-than-fuzzy ways to reach the record
that carries the data, each one *named* in the answer, never a silent substitution.

Three label rules extend the exact-label lookup when it finds nothing exact (``search_entities``):

* ``label_folded`` — the label is the question's term up to accents and case only ("méthane" → ChEBI
  "methane"): the same spelling, not a translation (no translation table exists in REVA by design);
* ``label_legal_form`` — the label is the term followed by a company legal form ("Glencore" → "GLENCORE PLC",
  "GLENCORE AG"); every such record is a candidate and the answer lists them;
* ``label_qualified`` — the label is the term followed by a parenthesised qualifier ("chromosome 17" →
  "chromosome 17 (Human)", "chromosome 17 (Mouse)").

One guard removes a wrong exact match: an all-capital code alias of four letters or fewer (an airport code
"EAU") never matches a word written in lower case ("eau").

One record bridge (:func:`label_bridges`) is tried by the relation-family and causal operations only when nothing
is loaded on the chosen record, its same-label alternatives and its identity neighbours: the *other records that
carry one of the chosen record's own labels* (e.g. Wikidata ``Q1072`` is labelled « cœur » in French and "heart" in
English; UBERON ``uberon:0000948`` is labelled "heart"). It is matching by a published label, worded as such
(« fiche distincte portant le libellé anglais « heart » de cœur (Q1072) ; rapprochement par le libellé, fiches non
fusionnées »), never an identity claim.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from reva.nlqa.ports import Entity, EntityMatch

#: Legal-form suffixes recognised after a company name (lower case, dots removed).
LEGAL_FORMS = frozenset(
    "plc ag sa sas sarl se nv bv ab asa oyj spa srl gmbh kg kgaa ltd limited inc incorporated corp corporation "
    "co llc lp llp pte pty bhd tbk jsc pjsc oao ooo kk sab".split()
)
#: New match kinds (ranked after the exact ones, before prefixes) and their answer wording.
MATCH_RANK = {"label_folded": 2, "label_legal_form": 2, "label_qualified": 2}
STRICT_BRIDGE_MATCHES = frozenset(MATCH_RANK)
MATCH_TEXT = {
    "label_folded": (
        "même libellé aux accents et à la casse près — pas une traduction",
        "same label up to accents and case — not a translation",
    ),
    "label_legal_form": (
        "libellé = le nom de la question suivi d'une forme juridique",
        "label = the name in the question followed by a legal form",
    ),
    "label_qualified": (
        "libellé = le terme de la question suivi d'une précision entre parenthèses",
        "label = the term in the question followed by a qualifier in parentheses",
    ),
}
_MAX_BRIDGE = 8
_CODE_ALIAS = re.compile(r"^[A-Z0-9]{2,4}$")


def fold_accents(s: str) -> str:
    t = unicodedata.normalize("NFKD", s)
    return "".join(c for c in t if not unicodedata.combining(c)).casefold().strip()


def is_code_alias_false_match(m: EntityMatch, query: str) -> bool:
    """An exact *alias* match on an all-capital short code ("EAU") for a word not written in capitals ("eau")."""
    return m.match == "alias_exact" and bool(_CODE_ALIAS.match(m.matched_text or "")) and not query.strip().isupper()


def legal_form_rest(label: str, term: str) -> str | None:
    """The legal-form suffix when ``label`` is ``term`` + a legal form (else ``None``)."""
    lab, t = fold_accents(label), fold_accents(term)
    if not lab.startswith(t + " "):
        return None
    rest = re.sub(r"[.,]", "", lab[len(t) :]).strip()
    words = rest.split()
    return rest if words and all(w in LEGAL_FORMS for w in words) and len(words) <= 2 else None


def qualified_rest(label: str, term: str) -> str | None:
    lab, t = label.strip(), term.strip()
    if not fold_accents(lab).startswith(fold_accents(t) + " ("):
        return None
    rest = lab[len(t) :].strip()
    return rest[1:-1] if rest.startswith("(") and rest.endswith(")") and rest.count("(") == 1 else None


_LABEL_ROWS = """
SELECT DISTINCT e.qid, e.kind, e.sitelinks, l.label
  FROM kg.entity_label l JOIN kg.entity_current e ON e.id = l.entity_id
 WHERE l.lang = ANY(:langs) AND {cond}
 LIMIT 60
"""


def bridge_matches(conn: Any, kq: Any, term: str, *, iso_of: Any = None, limit: int = 10) -> list[EntityMatch]:
    """Matches by the three label rules, in that order; the first rule that finds something wins."""
    t = term.strip()
    if not t or len(t) > 200:
        return []
    langs = ["fr", "en"]
    low = t.casefold()
    folded = fold_accents(t)
    found: list[tuple[str, str | None, int, str, str]] = []  # qid, kind, sitelinks, label, match
    # 1. accents / case only
    pat = "".join(c if c.isascii() else "_" for c in low)
    rows = conn.execute(
        text(_LABEL_ROWS.format(cond="(lower(l.label) = :f OR lower(l.label) LIKE :p)")),
        {"langs": langs, "f": folded, "p": pat},
    ).all()
    for q, kind, sl, label in rows:
        if fold_accents(label) == folded and label.casefold() != low:
            found.append((q, kind, int(sl or 0), label, "label_folded"))
    # 2. legal form / 3. qualifier
    if not found:
        prefix = low.replace("\\", "").replace("%", "").replace("_", "")
        rows = conn.execute(
            text(_LABEL_ROWS.format(cond="lower(l.label) LIKE :p")), {"langs": langs, "p": prefix + " %"}
        ).all()
        if t[:1].isupper():  # a company name is written as a proper noun ("Glencore", not "wolf")
            for q, kind, sl, label in rows:
                if legal_form_rest(label, t) is not None:
                    found.append((q, kind, int(sl or 0), label, "label_legal_form"))
        if not found:
            for q, kind, sl, label in rows:
                if qualified_rest(label, t) is not None:
                    found.append((q, kind, int(sl or 0), label, "label_qualified"))
            # a disambiguation set of one kind (one chromosome per species) is a bridge; mixed kinds
            # ("Wolf (band)", "Wolf (film)") are a genuine ambiguity: refused
            if len({f[1] for f in found}) > 1:
                found = []
    if not found:
        return []
    seen: dict[str, tuple[str | None, int, str, str]] = {}
    for q, kind, sl, label, match in found:
        seen.setdefault(q, (kind, sl, label, match))
    qids = list(seen)[:limit]
    labels = kq.labels_for(conn, qids)
    iso = iso_of(conn, qids) if iso_of is not None else {}
    return [
        EntityMatch(
            Entity(
                qid=q,
                labels=labels.get(q) or {"en": seen[q][2]},
                kind=seen[q][0],
                sitelinks=seen[q][1],
                iso3=iso.get(q),
                source="kg",
            ),
            seen[q][3],
            seen[q][2],
        )
        for q in qids
    ]


def refine(matches: Sequence[EntityMatch], term: str) -> list[EntityMatch]:
    """Drop the code-alias false matches."""
    return [m for m in matches if not is_code_alias_false_match(m, term)]


def needs_bridge(matches: Sequence[EntityMatch]) -> bool:
    return not any(m.match in ("label_exact", "alias_exact", "label_words") for m in matches)


_BRIDGE_SQL = """
SELECT DISTINCT e2.qid, e2.kind, l1.lang AS via_lang, l1.label AS via_label
  FROM kg.entity_current e1
  JOIN kg.entity_label l1 ON l1.entity_id = e1.id AND NOT l1.is_alias AND l1.lang IN ('fr', 'en')
  JOIN kg.entity_label l2 ON lower(l2.label) = lower(l1.label) AND NOT l2.is_alias AND l2.lang IN ('fr', 'en')
  JOIN kg.entity_current e2 ON e2.id = l2.entity_id AND e2.qid <> e1.qid
 WHERE e1.qid = :q
 ORDER BY e2.qid
 LIMIT :lim
"""


def label_bridges(
    conn: Any, kq: Any, qid: str, *, exclude: Sequence[str] = (), limit: int = _MAX_BRIDGE
) -> list[tuple[Entity, str, str]]:
    """Other records carrying one of ``qid``'s own fr/en labels (case-insensitive), with the label and its
    language: ``[(entity, via_lang, via_label)]``."""
    rows = conn.execute(text(_BRIDGE_SQL), {"q": qid, "lim": int(limit) + len(exclude)}).all()
    out: dict[str, tuple[str | None, str, str]] = {}
    for q, kind, lang, label in rows:
        if q in exclude or q in out:
            continue
        out[q] = (kind, lang, label)
    qids = list(out)[:limit]
    labels = kq.labels_for(conn, qids)
    return [
        (Entity(qid=q, labels=labels.get(q) or {}, kind=out[q][0], source="kg"), out[q][1], out[q][2]) for q in qids
    ]


LANG_NAME = {"fr": ("français", "French"), "en": ("anglais", "English")}


def bridge_sentence(lang: str, chosen: Entity, other: Entity, via_lang: str, via_label: str, what: str) -> str:
    """The identification sentence of an answer read through :func:`label_bridges`."""
    fr = lang == "fr"
    ln = LANG_NAME.get(via_lang, (via_lang, via_lang))[0 if fr else 1]
    c, o = chosen.label(lang), other.label(lang)
    if fr:
        return (
            f"Rien de chargé sur {c} ({chosen.qid}) pour cette {what} ; réponse lue sur {o} ({other.qid}), fiche "
            f"distincte portant le libellé {ln} « {via_label} » de {c} ({chosen.qid}) — rapprochement par le "
            f"libellé publié, pas une identité affirmée par une source (fiches non fusionnées)."
        )
    return (
        f"Nothing loaded on {c} ({chosen.qid}) for this {what}; answer read on {o} ({other.qid}), a distinct record "
        f"carrying the {ln} label “{via_label}” of {c} ({chosen.qid}) — matched by the published label, not an "
        f"identity asserted by a source (records not merged)."
    )


def same_rule_sentence(lang: str, chosen: Entity, other: Entity, match: str | None, what: str) -> str | None:
    """Wording for an answer read on an equally matching record found by one of the bridge rules."""
    if match not in MATCH_TEXT:
        return None
    rule = MATCH_TEXT[match][0 if lang == "fr" else 1]
    c, o = chosen.label(lang), other.label(lang)
    if lang == "fr":
        return (
            f"Rien de chargé sur {c} ({chosen.qid}) pour cette {what} ; réponse lue sur {o} ({other.qid}), autre "
            f"fiche retenue par la même règle ({rule}) ; fiches non fusionnées."
        )
    return (
        f"Nothing loaded on {c} ({chosen.qid}) for this {what}; answer read on {o} ({other.qid}), another record "
        f"found by the same rule ({rule}); records not merged."
    )
