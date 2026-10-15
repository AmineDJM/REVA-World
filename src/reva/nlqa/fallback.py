"""Keyword planner used when the LLM is disabled: deterministic, lexical, documented as limited.

It recognises (accent/case-insensitive) catalog concept labels (fr/en), crosswalk country
names and ISO3 codes, years, ranking words, relation words ("lié", "connected", …) and
capitalised names. The plan it writes goes through the same :func:`validate_plan` as an LLM
plan and is labelled ``planner: keyword_fallback``. It never guesses beyond these rules: a
question it cannot map yields an empty plan and an unsupported item.
"""

from __future__ import annotations

import re
from typing import Any

from reva.nlqa.catalog import QaCatalog, fold
from reva.nlqa.plan import detect_language

_RANK_DESC = re.compile(
    r"\b(plus fort\w*|plus [ée]lev[ée]\w*|plus grand\w*|highest|largest|biggest|most|top|classement|rank\w*|"
    r"maximum|le plus|la plus|les plus)\b",
    re.IGNORECASE,
)
_RANK_ASC = re.compile(r"\b(plus faible\w*|plus bas\w*|plus petit\w*|lowest|smallest|least|moins)\b", re.IGNORECASE)
_RELATION = re.compile(r"\b(li[ée]e?s?|reli[ée]e?s?|rapport|lien|connect\w*|link\w*|relat\w*)\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
_CAP_SPAN = re.compile(
    r"(?:[A-ZÀ-ÖØ-Þ][\w'’-]+)(?:\s+(?:de|du|des|d'|la|le|of|the|von|van|da|di)?\s*[A-ZÀ-ÖØ-Þ][\w'’-]+)*"
)
_QUESTION_WORDS = {
    "comment",
    "quel",
    "quelle",
    "quels",
    "quelles",
    "pourquoi",
    "quand",
    "ou",
    "qui",
    "que",
    "how",
    "what",
    "which",
    "why",
    "when",
    "where",
    "who",
    "position",
    "est",
    "is",
    "are",
    "les",
    "le",
    "la",
    "des",
    "in",
    "en",
    "the",
    "dans",
}


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None


_STOP = {"de", "du", "des", "la", "le", "les", "of", "the", "and", "et", "per", "par", "en", "in", "a", "au", "aux"}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", fold(text)) if w not in _STOP and len(w) >= 3]


def match_concept(question: str, catalog: QaCatalog) -> tuple[str | None, list[str]]:
    """Best concept by the share of its label words (parenthetical parts ignored) found in the question:
    at least half of the words and at least one word of 5+ letters; a tie returns no concept and the
    tied candidates."""
    qwords = set(_words(question))
    scored: dict[str, float] = {}
    for c in catalog.concepts.values():
        best = 0.0
        for lbl in c.labels.values():
            words = _words(re.sub(r"\(.*?\)", " ", lbl))
            if not words:
                continue
            hit = [w for w in words if w in qwords or (len(w) >= 6 and any(q.startswith(w[:6]) for q in qwords))]
            if hit and any(len(w) >= 5 for w in hit):
                best = max(best, len(hit) / len(words))
        if best >= 0.5:
            scored[c.id] = best
    if not scored:
        return None, []
    top = max(scored.values())
    winners = sorted(k for k, v in scored.items() if v == top)
    return (winners[0], []) if len(winners) == 1 else (None, winners)


MAX_TIED_CONCEPTS = 4


def keyword_plan(question: str, catalog: QaCatalog) -> dict[str, Any]:
    q = fold(question)
    lang = detect_language(question)
    years = [int(y) for y in _YEAR.findall(question)]
    ops: list[dict[str, Any]] = []

    def op(**kw: Any) -> dict[str, Any]:
        d = {"id": f"s{len(ops) + 1}", **kw}
        ops.append(d)
        return d

    concept, ambiguous = match_concept(question, catalog)
    # countries: crosswalk names / ISO3 codes, in order of appearance
    found: list[tuple[int, str]] = []
    for c in catalog.countries.values():
        for n in c.names:
            m = re.search(r"(?<!\w)" + re.escape(fold(n)) + r"(?!\w)", q)
            if m and len(n) >= 4:
                found.append((m.start(), c.iso3))
                break
        else:
            m = re.search(r"(?<![A-Za-z])" + c.iso3 + r"(?![A-Za-z])", question)
            if m:
                found.append((m.start(), c.iso3))
    countries = [iso for _, iso in sorted(set(found))]
    if concept:
        if _RANK_ASC.search(question) or _RANK_DESC.search(question):
            order = "asc" if _RANK_ASC.search(question) else "desc"
            op(op="atlas_rank", concept=concept, year=years[0] if years else None, order=order, limit=10)
        elif countries and len(years) >= 2:
            for iso in countries[:3]:
                op(op="atlas_series", concept=concept, country=iso, from_year=min(years), to_year=max(years))
        elif countries:
            for iso in countries[:5]:
                op(op="atlas_value", concept=concept, country=iso, year=years[0] if years else None)
        else:
            op(op="atlas_rank", concept=concept, year=years[0] if years else None, order="desc", limit=10)
    elif ambiguous and len(ambiguous) <= MAX_TIED_CONCEPTS and countries and not _RANK_DESC.search(question):
        # a few equally matching concepts for named countries: each is queried and named in the answer
        for cid in ambiguous:
            for iso in countries[:2]:
                op(op="atlas_value", concept=cid, country=iso, year=years[0] if years else None)
        ambiguous = []
    else:
        spans = []
        for m in _CAP_SPAN.finditer(question):
            span = m.group(0).strip()
            words = span.split()
            while words and fold(words[0]) in _QUESTION_WORDS:
                words = words[1:]
            span = " ".join(words)
            if len(span) >= 3 and fold(span) not in _QUESTION_WORDS:
                spans.append(span)
        spans = list(dict.fromkeys(spans))[:4]
        lookups = [op(op="entity_lookup", text=s, kind=None)["id"] for s in spans]
        if len(lookups) >= 2 and _RELATION.search(question):
            op(op="kg_path", from_entity=lookups[0], to_entity=lookups[1], max_hops=3, as_of=None)
        elif lookups and len(years) >= 2:
            op(op="kg_timeline", entity=lookups[0], from_year=min(years), to_year=max(years))
        else:
            for ref in lookups[:2]:
                op(op="kg_neighbours", entity=ref, properties=[], as_of=str(years[0]) if years else None)
    unsupported = []
    if ambiguous:
        unsupported.append(
            {"text": question[:300], "reason": "several catalog concepts match equally: " + ", ".join(ambiguous[:5])}
        )
    if not ops:
        unsupported.append({"text": question[:300], "reason": "no catalog concept, country or name recognised"})
    return {
        "input_language": lang,
        "restatement": "",
        "operations": ops,
        "unsupported": unsupported,
    }
