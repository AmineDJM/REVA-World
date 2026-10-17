"""Keyword planner used when the LLM is disabled: deterministic, lexical, documented as limited.

It recognises (accent/case-insensitive) catalog concept labels (fr/en), crosswalk country
names and ISO3 codes, years, ranking words, relation words ("lié", "connected", …),
capitalised names, and causal questions ("pourquoi …", "quelles sont les causes de …",
"conséquences de …", "chaîne causale entre X et Y", "why did …", "how did X lead to Y") which
become ``why`` / ``causal_chain`` over a looked-up event. The plan it writes goes through the same :func:`validate_plan` as an LLM
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
#: "liens entre X et Y", "relation(s) entre X et Y", "links/relations between X and Y" (links_pair)
_PAIR = re.compile(
    r"\b(liens?|relations?|rapports?|connexions?|links?|relations?hips?|ties|connections?)\s+(entre|between)\b",
    re.IGNORECASE,
)
# --- causal questions (why / causal_chain): the event phrase is taken from the question as written
_END = r"\s*[?!.…]*\s*$"
_CHAIN_PATTERNS = (
    re.compile(
        r"\b(?:cha[iî]nes?\s+causales?|liens?\s+(?:de\s+causalit[ée]|causals?)|causal\s+(?:chains?|links?|paths?))\s+"
        r"(?:entre|between)\s+(?P<a>.+?)\s+(?:et|and)\s+(?P<b>.+?)" + _END,
        re.IGNORECASE,
    ),
    re.compile(
        r"\bcomment\s+(?P<a>.+?)\s+(?:a-t-(?:il|elle)|ont-(?:ils|elles))\s+(?:conduit|men[ée]|abouti|d[ée]bouch[ée])\s+"
        r"(?:à|a|au|aux|sur)\s+(?P<b>.+?)" + _END,
        re.IGNORECASE,
    ),
    re.compile(r"\bhow\s+did\s+(?P<a>.+?)\s+(?:lead|contribute)\s+to\s+(?P<b>.+?)" + _END, re.IGNORECASE),
)
_EFFECT_PATTERNS = (
    re.compile(
        r"\b(?:cons[ée]quences?|effets?|r[ée]percussions?)\s+(?:de\s+la|de\s+l['’]|du|des|de|d['’])\s*(?P<x>.+?)"
        + _END,
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:consequences?|effects?|outcomes?|results?)\s+of\s+(?P<x>.+?)" + _END, re.IGNORECASE),
)
_CAUSE_PATTERNS = (
    re.compile(
        r"\b(?:causes?|raisons?)\s+(?:de\s+la|de\s+l['’]|du|des|de|d['’])\s*(?P<x>.+?)" + _END,
        re.IGNORECASE,
    ),
    re.compile(r"\bpourquoi\s+(?P<why>.+?)" + _END, re.IGNORECASE),
    re.compile(r"\b(?:causes?|reasons?)\s+(?:of|for)\s+(?P<x>.+?)" + _END, re.IGNORECASE),
    re.compile(r"\bwhy\s+(?:did|was|were|is|are|does|do|has|had)\s+(?P<why>.+?)" + _END, re.IGNORECASE),
    re.compile(r"\bwhat\s+caused\s+(?P<x>.+?)" + _END, re.IGNORECASE),
)
#: words that end an event phrase ("pourquoi la Révolution française a-t-elle eu lieu" → "Révolution française")
_PHRASE_STOP = {
    "a",
    "ont",
    "est",
    "sont",
    "fut",
    "furent",
    "eut",
    "eurent",
    "s'est",
    "se",
    "y",
    "il",
    "elle",
    "ils",
    "elles",
    "éclate",
    "eclate",
    "éclaté",
    "commence",
    "commencé",
    "commencee",
    "eu",
    "lieu",
    "survenu",
    "survenue",
    "happen",
    "happened",
    "start",
    "started",
    "begin",
    "began",
    "break",
    "broke",
    "occur",
    "occurred",
    "end",
    "ended",
    "take",
    "took",
    "fail",
    "failed",
    "collapse",
    "collapsed",
    "fall",
    "fell",
}
_LEADING_ARTICLES = re.compile(r"^(?:(?:le|la|les|un|une|the|a|an)\s+|l['’]\s*)", re.IGNORECASE)
#: "pourquoi …" / "why …" name an event only with a capitalised word (also after an elided article: "l'URSS"), a
#: year, or an event noun with a qualifier ("pourquoi la guerre russo-ukrainienne ?"); "pourquoi le ciel est-il
#: bleu" and "why did the war start" are not event questions (no event named). The nouns are kinds of events (a
#: closed list of words), never names: the event itself is found by the entity lookup in the KG label index.
_NAMED = re.compile(r"(?:^|[\s'’-])[A-ZÀ-ÖØ-Þ]|\d{3,4}")
_EVENT_NOUNS = re.compile(
    r"(?<!\w)(?:guerres?|crises?|r[ée]volutions?|dissolution|dislocation|chute|effondrement|d[ée]pression|"
    r"r[ée]cession|krach|famines?|pand[ée]mies?|[ée]pid[ée]mies?|g[ée]nocides?|invasion|coup d['’ ]?[ée]tat|"
    r"attentats?|assassinats?|batailles?|conflits?|ind[ée]pendance|r[ée]unification|partition|annexion|"
    r"insurrection|r[ée]voltes?|soul[èe]vements?|r[ée]bellion|massacres?|s[ée]ismes?|tremblements? de terre|"
    r"tsunamis?|[ée]ruptions?|ouragans?|cyclones?|inondations?|s[ée]cheresses?|"
    r"wars?|crisis|crises|revolutions?|collapse|fall|depression|recession|crash|pandemics?|epidemics?|"
    r"invasion|coup|assassination|battles?|conflicts?|independence|reunification|annexation|uprising|"
    r"rebellion|earthquakes?|eruptions?|hurricanes?|floods?|droughts?)(?!\w)",
    re.IGNORECASE,
)


def _names_an_event(phrase: str) -> bool:
    """A capitalised word or a year, else an event noun with a qualifier ("guerre russo-ukrainienne", not "war")."""
    return bool(_NAMED.search(phrase)) or (bool(_EVENT_NOUNS.search(phrase)) and len(phrase.split()) >= 2)


def event_phrase(text: str) -> str:
    """The event named in a causal question, as written: cut at the verb, leading article dropped."""
    words = text.strip().split()
    out: list[str] = []
    for w in words:
        lw = w.lower()
        if out and (lw in _PHRASE_STOP or "-t-" in lw or re.search(r"-(?:il|elle|ils|elles)$", lw)):
            break
        out.append(w)
    phrase = " ".join(out).strip(" ,;:«»\"'")
    return _LEADING_ARTICLES.sub("", phrase, count=1).strip()


def causal_plan(question: str) -> list[dict[str, Any]] | None:
    """Operations for a causal question ("pourquoi …", "quelles sont les causes de …", "conséquences de …",
    "chaîne causale entre X et Y", "how did X lead to Y"), or ``None`` when the question is not causal."""
    q = question.strip()
    for rx in _CHAIN_PATTERNS:
        m = rx.search(q)
        if m:
            a, b = event_phrase(m.group("a")), event_phrase(m.group("b"))
            if a and b:
                return [
                    {"id": "s1", "op": "entity_lookup", "text": a, "kind": None},
                    {"id": "s2", "op": "entity_lookup", "text": b, "kind": None},
                    {"id": "s3", "op": "causal_chain", "from_entity": "s1", "to_entity": "s2", "max_hops": 4},
                ]
    for direction, patterns in (("effects", _EFFECT_PATTERNS), ("causes", _CAUSE_PATTERNS)):
        for rx in patterns:
            m = rx.search(q)
            if m:
                x = event_phrase(m.group("why") if "why" in m.groupdict() else m.group("x"))
                if "why" in m.groupdict() and not _names_an_event(x):
                    continue
                if x:
                    return [
                        {"id": "s1", "op": "entity_lookup", "text": x, "kind": None},
                        {"id": "s2", "op": "why", "entity": "s1", "causal_direction": direction},
                    ]
    return None


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


_UNSET: Any = object()


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", fold(text)) if w not in _STOP and len(w) >= 3]


def _label_score(labels: Any, qwords: set[str]) -> float:
    """Share of a label's words (parenthetical parts ignored) found in the question: at least one hit
    word of 5+ letters, best label wins; a word of 6+ letters also matches a question word sharing its
    first 6 letters."""
    best = 0.0
    for lbl in labels.values():
        words = _words(re.sub(r"\(.*?\)", " ", lbl))
        if not words:
            continue
        hit = [w for w in words if w in qwords or (len(w) >= 6 and any(q.startswith(w[:6]) for q in qwords))]
        if hit and any(len(w) >= 5 for w in hit):
            best = max(best, len(hit) / len(words))
    return best


def _tied(question: str, catalog: QaCatalog) -> list[str]:
    qwords = set(_words(question))
    scored = {c.id: s for c in catalog.concepts.values() if (s := _label_score(c.labels, qwords)) >= 0.5}
    if not scored:
        return []
    top = max(scored.values())
    return sorted(k for k, v in scored.items() if v == top)


def _default_headlines() -> Any:
    """ATLASOPS's headline/source-order registry (``registry/atlas/source_order``), read only; ``None``
    when unavailable (the tie is then not broken)."""
    try:
        from reva.atlas.source_order import cached_headlines

        return cached_headlines()
    except Exception:  # a broken registry must not break the keyword planner
        return None


def _not_equivalent_keys(h: Any) -> list[str]:
    """Series named under a headline's ``not_equivalent`` ("a / b / c" lists, dataset-level keys allowed)."""
    out = []
    for x in getattr(h, "not_equivalent", ()) or ():
        for part in str(x.get("series") or "").split(" / "):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _names_series(declared: str, key: str) -> bool:
    """``declared`` (a not_equivalent entry: a series key, or a dataset key ``source.dataset``) covers ``key``."""
    return key == declared or key.startswith(declared + "|") or key.startswith(declared + ".")


def headline_tiebreak(question: str, tied: list[str], catalog: QaCatalog, headlines: Any) -> tuple[str, str] | None:
    """``(catalog concept, headline concept)`` chosen among equally matching catalog concepts by the
    headline layer, or ``None`` when it does not decide.

    A headline concept *covers* a tied catalog concept when it lists it in ``related_catalog_concepts``
    or when one of its ordered series is in the concept's ``source_order``. When exactly one headline
    concept covers tied candidates (after keeping, if several do, only those whose own label matches the
    question best), the covered candidate holding its best-ranked series is chosen (then the order of
    ``related_catalog_concepts``, then the id): REVA's declared best-available quantity for the topic.
    Several headline concepts left → genuine ambiguity, nothing is chosen."""
    if headlines is None or len(tied) < 2:
        return None
    covering: dict[str, list[tuple[int, int, str]]] = {}
    for h in sorted(headlines.concepts.values(), key=lambda h: h.id):
        keys = [s.key for s in h.series]
        related = list(h.related_catalog_concepts)
        excluded = _not_equivalent_keys(h)
        for cid in tied:
            c = catalog.concepts.get(cid)
            if c is None:
                continue
            if any(_names_series(x, k) for k in c.source_order for x in excluded):
                continue  # ASK-7: a series the headline declares not equivalent never stands for it
            ranks = [i for i, k in enumerate(keys) if k in c.source_order]
            if ranks or cid in related:
                rel = related.index(cid) if cid in related else len(related)
                covering.setdefault(h.id, []).append((min(ranks) if ranks else len(keys), rel, cid))
    if len(covering) > 1:
        qwords = set(_words(question))
        scores = {hid: _label_score(headlines.concepts[hid].labels, qwords) for hid in covering}
        top = max(scores.values())
        covering = {hid: v for hid, v in covering.items() if scores[hid] == top and top >= 0.5}
    if len(covering) != 1:
        return None
    ((hid, cands),) = covering.items()
    return min(cands)[2], hid


def match_concept(question: str, catalog: QaCatalog, headlines: Any = _UNSET) -> tuple[str | None, list[str]]:
    """Best concept by the share of its label words (parenthetical parts ignored) found in the question:
    at least half of the words and at least one word of 5+ letters. A tie is broken by the headline
    layer (:func:`headline_tiebreak`) when it decides; otherwise no concept and the tied candidates."""
    tied = _tied(question, catalog)
    if len(tied) <= 1:
        return (tied[0] if tied else None), []
    hb = headline_tiebreak(question, tied, catalog, _default_headlines() if headlines is _UNSET else headlines)
    return (hb[0], []) if hb else (None, tied)


#: Equally matching concepts queried one by one (each answered with its own unit and source) at most.
MAX_TIED_CONCEPTS = 5


def _spans(question: str) -> list[str]:
    """Capitalised names of the question (leading question words dropped), at most 4, in order."""
    spans = []
    for m in _CAP_SPAN.finditer(question):
        words = m.group(0).strip().split()
        while words and fold(words[0]) in _QUESTION_WORDS:
            words = words[1:]
        span = " ".join(words)
        if len(span) >= 3 and fold(span) not in _QUESTION_WORDS:
            spans.append(span)
    return list(dict.fromkeys(spans))[:4]


def countries_in(question: str, catalog: QaCatalog) -> list[str]:
    """ISO3 codes of the countries the question names (crosswalk names in English and French, ISO3 codes), in
    order of appearance; a name inside a longer matched name of another country is not a mention."""
    q = fold(question)
    # countries: crosswalk names (English and French, ASK-8) / ISO3 codes, in order of appearance; a name inside a
    # longer matched name of another country ("Guinée" in "Guinée-Bissau") is not a mention
    name_spans: list[tuple[int, int, str]] = []
    for c in catalog.countries.values():
        best: tuple[int, int, str] | None = None
        for n in c.names:
            m = re.search(r"(?<!\w)" + re.escape(fold(n)) + r"(?!\w)", q)
            if m and len(n) >= 4 and (best is None or m.end() - m.start() > best[1] - best[0]):
                best = (m.start(), m.end(), c.iso3)
        if best is None:
            m = re.search(r"(?<![A-Za-z])" + c.iso3 + r"(?![A-Za-z])", question)
            if m:
                best = (m.start(), m.end(), c.iso3)  # question offsets; ISO3 codes never overlap names
        if best is not None:
            name_spans.append(best)
    found = [
        (a, iso)
        for a, z, iso in name_spans
        if not any(a2 <= a and z <= z2 and (z2 - a2) > (z - a) and iso2 != iso for a2, z2, iso2 in name_spans)
    ]
    countries = [iso for _, iso in sorted(set(found))]
    return countries


def keyword_plan(question: str, catalog: QaCatalog, headlines: Any = _UNSET) -> dict[str, Any]:
    q = fold(question)
    lang = detect_language(question)
    years = [int(y) for y in _YEAR.findall(question)]
    ops: list[dict[str, Any]] = []
    causal = causal_plan(question)
    if causal is not None:
        return {"input_language": lang, "restatement": "", "operations": causal, "unsupported": [], "ambiguities": []}

    def op(**kw: Any) -> dict[str, Any]:
        d = {"id": f"s{len(ops) + 1}", **kw}
        ops.append(d)
        return d

    ambiguous = _tied(question, catalog)
    concept = ambiguous[0] if len(ambiguous) == 1 else None
    ambiguity: dict[str, Any] | None = None
    if len(ambiguous) > 1:
        hb = headline_tiebreak(question, ambiguous, catalog, _default_headlines() if headlines is _UNSET else headlines)
        ambiguity = {"candidates": ambiguous[:12], "resolution": "unresolved"}
        if hb:
            concept = hb[0]
            ambiguity.update(resolution="headline", chosen=hb[0], headline=hb[1])
    ambiguous = [] if concept else ambiguous
    countries = countries_in(question, catalog)
    if _PAIR.search(question):
        # links between two countries: crosswalk names / ISO3 codes, else the capitalised names (resolved by the
        # server to countries through the KG; a name that is not a country leaves the operation unresolved)
        ends: list[str] = countries[:2]
        if len(ends) < 2:
            ends = [op(op="entity_lookup", text=sp, kind="country")["id"] for sp in _spans(question)[:2]]
        if len(ends) == 2:
            span = sorted(years)
            op(
                op="links_pair",
                country=ends[0],
                other_country=ends[1],
                link_kinds=[],
                from_year=span[0] if span else None,
                to_year=span[-1] if span else None,
            )
            return {
                "input_language": lang,
                "restatement": "",
                "operations": ops,
                "unsupported": [],
                "ambiguities": [],
            }
        ops.clear()
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
        # a few equally matching concepts for named countries: each is queried and answered separately
        # (its own unit and source), never merged into one sentence
        for cid in ambiguous:
            for iso in countries[:2]:
                op(op="atlas_value", concept=cid, country=iso, year=years[0] if years else None)
        assert ambiguity is not None
        ambiguity["resolution"] = "each_queried"
        ambiguous = []
    elif not ambiguous:
        spans = _spans(question)
        lookups = [op(op="entity_lookup", text=s, kind=None)["id"] for s in spans]
        if len(lookups) >= 2 and _RELATION.search(question):
            op(op="kg_path", from_entity=lookups[0], to_entity=lookups[1], max_hops=3, as_of=None)
        elif lookups and len(years) >= 2:
            op(op="kg_timeline", entity=lookups[0], from_year=min(years), to_year=max(years))
        else:
            for ref in lookups[:2]:
                op(op="kg_neighbours", entity=ref, properties=[], as_of=str(years[0]) if years else None)
    # A genuine ambiguity (several concepts match equally and nothing decides) is reported as such, with
    # its candidates (``ambiguities``), never as "not covered": the data may well exist for each candidate.
    unsupported = []
    if not ops and not ambiguous:
        unsupported.append({"text": question[:300], "reason": "no catalog concept, country or name recognised"})
    return {
        "input_language": lang,
        "restatement": "",
        "operations": ops,
        "unsupported": unsupported,
        "ambiguities": [ambiguity] if ambiguity else [],
    }
