"""Keyword routes of the V6 domains (agent NLQA6) and the registry-vocabulary matcher.

The keyword planner recognises, before the causal and atlas rules, a closed set of question *shapes* (never
names): composition ("de quoi est fait X", "what is X made of"), parts and contents ("quels gènes sur X"),
diet ("que mange X"), place ("où vit X"), ownership ("qui possède X"), production, counts ("combien d'espèces
de X"), cross lists ("quelles centrales au charbon en Inde"), diseases caused by an agent, causes of death in a
country, effect estimates ("effet d'une hausse de X sur Y"), sealed forecasts ("prévision d'inflation pour la
France") and decision scenarios ("que se passe-t-il si la BCE monte ses taux de 50 pb"). Every entity named in
the question is resolved later by the server through the KG label index (``entity_lookup``); every registry
term (effect variable, forecast family, reference decision) by :func:`match_terms` over the registry's own
fr/en labels. No name → id table exists here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from reva.nlqa.catalog import QaCatalog, fold

_END = r"\s*[?!.…]*\s*$"
_DE = r"(?:de\s+la\s+|de\s+l['’]\s*|du\s+|des\s+|de\s+|d['’]\s*)"
_ART = re.compile(r"^(?:(?:le|la|les|un|une|des|du|the|a|an)\s+|l['’]\s*)", re.IGNORECASE)


def _x(text: str) -> str:
    """An entity phrase as written: trailing punctuation and one leading article removed."""
    t = text.strip().strip(" ,;:«»\"'?!.…")
    return _ART.sub("", t, count=1).strip()


def _lookup(text: str, kind: str | None = None) -> dict[str, Any]:
    return {"op": "entity_lookup", "text": text, "kind": kind}


# --------------------------------------------------------------------------- relation-family shapes
#: (patterns, families, reading, count) — the families are V4CORE relation-family ids.
_FAMILY_RULES: tuple[tuple[tuple[re.Pattern[str], ...], tuple[str, ...], str, bool], ...] = (
    (  # composition / parts
        (
            re.compile(r"\b(?:de|en)\s+quoi\s+(?:est|sont)\s+(?:fai|compos[ée]|constitu[ée])\w*\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bqu['’]est-ce\s+qui\s+(?:compose|constitue)\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bcomposition\s+(?:chimique\s+)?" + _DE + r"(?P<x>.+?)" + _END, re.I),
            re.compile(
                r"\b(?:constituants|composants|composantes|parties|[ée]l[ée]ments)\s+" + _DE + r"(?P<x>.+?)" + _END,
                re.I,
            ),
            re.compile(r"\bwhat\s+(?:is|are)\s+(?P<x>.+?)\s+made\s+(?:of|from)" + _END, re.I),
            re.compile(r"\bwhat\s+(?:does|do)\s+(?P<x>.+?)\s+consist\s+of" + _END, re.I),
            re.compile(r"\b(?:composition|components|parts|constituents)\s+of\s+(?P<x>.+?)" + _END, re.I),
        ),
        ("made_of", "has_part"),
        "forward",
        False,
    ),
    (  # predators (inverse diet) — before the diet rule
        (
            re.compile(r"\bqui\s+mangen?t?\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bpr[ée]dateurs?\s+" + _DE + r"(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bwhat\s+eats\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bpredators?\s+of\s+(?P<x>.+?)" + _END, re.I),
        ),
        ("eats",),
        "inverse",
        False,
    ),
    (  # diet
        (
            re.compile(r"\bque\s+mangen?t?\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bde\s+quoi\s+se\s+nourrissen?t?\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\br[ée]gime\s+alimentaire\s+" + _DE + r"(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bwhat\s+(?:does|do)\s+(?P<x>.+?)\s+eat" + _END, re.I),
            re.compile(r"\bdiet\s+of\s+(?P<x>.+?)" + _END, re.I),
        ),
        ("eats",),
        "forward",
        False,
    ),
    (  # ownership
        (
            re.compile(r"\bqui\s+(?:poss[èe]de|d[ée]tient|contr[ôo]le)\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\b(?:propri[ée]taires?|actionnaires?)\s+" + _DE + r"(?P<x>.+?)" + _END, re.I),
            re.compile(r"\b[àa]\s+qui\s+appartien(?:t|nent)\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bwho\s+owns\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bowners?\s+of\s+(?P<x>.+?)" + _END, re.I),
        ),
        ("owned_by",),
        "forward",
        False,
    ),
    (  # holdings
        (
            re.compile(r"\bque\s+(?:poss[èe]de|d[ée]tient|contr[ôo]le)\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bwhat\s+does\s+(?P<x>.+?)\s+own" + _END, re.I),
        ),
        ("owns",),
        "forward",
        False,
    ),
    (  # producers
        (
            re.compile(r"\bqui\s+produi(?:t|sent)\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bproducteurs?\s+" + _DE + r"(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bwho\s+produces\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bproducers?\s+of\s+(?P<x>.+?)" + _END, re.I),
        ),
        ("produced_by",),
        "forward",
        False,
    ),
    (  # products
        (
            re.compile(r"\bque\s+produi(?:t|sent)\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bwhat\s+(?:does|do)\s+(?P<x>.+?)\s+produce" + _END, re.I),
        ),
        ("produces",),
        "forward",
        False,
    ),
    (  # place / habitat
        (
            re.compile(r"\bo[uù]\s+vi(?:t|vent)\s+(?P<x>.+?)" + _END, re.I),
            re.compile(r"\bhabitat\s+" + _DE + r"(?P<x>.+?)" + _END, re.I),
            re.compile(
                r"\bo[uù]\s+(?:se\s+trouven?t?|(?:est|sont)\s+situ[ée]e?s?|(?:est|sont)\s+localis[ée]e?s?|est|sont)\s+"
                r"(?P<x>.+?)" + _END,
                re.I,
            ),
            re.compile(r"\bwhere\s+(?:does|do)\s+(?P<x>.+?)\s+live" + _END, re.I),
            re.compile(r"\bwhere\s+(?:is|are)\s+(?P<x>.+?)(?:\s+located|\s+found)?" + _END, re.I),
            re.compile(r"\bhabitat\s+of\s+(?P<x>.+?)" + _END, re.I),
        ),
        ("located_in",),
        "forward",
        False,
    ),
)
#: "quels gènes sur le chromosome 17", "which genes are on chromosome 17": what is located in / part of X
_CONTENTS = (
    re.compile(r"^\s*quel(?:le)?s\s+(?:sont\s+les\s+)?(?P<noun>[\w-]+)\s+(?:se\s+trouvent\s+)?(?:sur|dans)\s+(?P<x>.+?)" + _END, re.I),
    re.compile(r"^\s*(?:which|what)\s+(?P<noun>\w+)\s+(?:are\s+)?(?:located\s+)?(?:on|in)\s+(?P<x>.+?)" + _END, re.I),
)
#: "combien d'espèces de mammifères", "how many species of mammals"
_COUNT = (
    re.compile(
        r"\bcombien\s+(?:d['’]\s*|de\s+)(?P<noun>[\w-]+)\s+(?:y\s+a-t-il\s+)?(?:" + _DE[3:-1] + r"|chez\s+les\s+|"
        r"parmi\s+les\s+|dans\s+(?:la\s+|le\s+|les\s+|l['’]\s*)?)(?P<x>.+?)" + _END,
        re.I,
    ),
    re.compile(r"\bhow\s+many\s+(?P<noun>\w+)\s+(?:of|in|among|are\s+there\s+in)\s+(?P<x>.+?)" + _END, re.I),
)
#: "quelles centrales au charbon en Inde", "which coal power plants are in India": linked to A and located in B
_CROSS = (
    re.compile(
        r"^\s*(?:quel(?:le)?s\s+(?:sont\s+les\s+)?|liste\s+des\s+|les\s+)(?P<noun>[\w-]+(?:\s+[\w-]+)?)\s+"
        r"(?:au|aux|à\s+la|à\s+l['’]\s*|à|a)\s+(?P<a>[^?]+?)\s+(?:en|au|aux|dans\s+(?:le\s+|la\s+|les\s+|l['’]\s*)?)\s*"
        r"(?P<b>[^?]+?)" + _END,
        re.I,
    ),
    re.compile(
        r"^\s*(?:which|what|list(?:\s+the)?)\s+(?P<a>[\w-]+)\s+(?P<noun>(?:power\s+)?(?:plants?|stations?|mines?|"
        r"facilities|companies|firms|fields))\s+(?:are\s+)?(?:there\s+)?in\s+(?P<b>[^?]+?)" + _END,
        re.I,
    ),
)
#: diseases a pathogen causes: causal claims (effects) + non-causal associations, worded apart
_CAUSED_BY = (
    re.compile(
        r"\b(?:maladies?|infections?|pathologies?|affections?)\s+(?:sont\s+)?(?:caus[ée]e?s?|provoqu[ée]e?s?|"
        r"d[uû]e?s?|entra[iî]n[ée]e?s?)\s+(?:par|à)\s+(?P<x>.+?)" + _END,
        re.I,
    ),
    re.compile(r"\b(?:diseases?|infections?|illness(?:es)?)\s+(?:are\s+)?caused\s+by\s+(?P<x>.+?)" + _END, re.I),
    re.compile(r"\bwhat\s+does\s+(?P<x>.+?)\s+cause" + _END, re.I),
)
#: a proper name (capital then lower case: "Première Guerre"); acronyms such as "BCE" are not
_PROPER = re.compile(r"(?:^|[\s'’-])[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]")
#: causes of death in a country: atlas concepts of the catalog sub-domain ``causes_of_death`` (WHO GHE)
_DEATH_CAUSES = re.compile(
    r"\bcauses?\s+(?:de\s+|des\s+|du\s+)?(?:d[ée]c[èe]s|mortalit[ée]|morts?)\b|\bcauses?\s+of\s+(?:death|mortality)\b",
    re.I,
)
DEATH_SUBDOMAIN = "causes_of_death"
MAX_SUBDOMAIN_CONCEPTS = 8
# --------------------------------------------------------------------------- effects / forecasts / decisions
_EFFECT = (
    re.compile(
        r"\b(?:effets?|impacts?|incidences?|cons[ée]quences?|r[ée]percussions?|influence)\s+"
        r"(?:d['’]\s*une?\s+|d['’]\s*|de\s+la\s+|de\s+l['’]\s*|du\s+|des\s+|de\s+)(?P<c>.+?)\s+sur\s+"
        r"(?P<e>.+?)" + _END,
        re.I,
    ),
    re.compile(
        r"\b(?:effects?|impacts?|consequences?|influence)\s+of\s+(?:an?\s+|the\s+)?(?P<c>.+?)\s+on\s+(?P<e>.+?)" + _END,
        re.I,
    ),
    re.compile(r"\bhow\s+(?:does|do)\s+(?:an?\s+|the\s+)?(?P<c>.+?)\s+affect\s+(?P<e>.+?)" + _END, re.I),
)
_FORECAST_WORD = re.compile(r"\b(?:pr[ée]visions?|pr[ée]vu(?:e|s|es)?|forecasts?|projections?|pronostics?)\b", re.I)
_FORECAST_TOPIC = (
    re.compile(
        r"\b(?:pr[ée]visions?|forecasts?|projections?|pronostics?)\s+(?:" + _DE[3:-1] + r"|pour\s+(?:la\s+|le\s+|l['’]\s*|les\s+)?|"
        r"of\s+(?:the\s+)?|for\s+(?:the\s+)?)(?P<t>.+?)(?:\s+(?:pour|en|au|aux|dans|in|for)\s+[^?]+)?" + _END,
        re.I,
    ),
    re.compile(r"\b(?:the|une?|la|le)\s+(?P<t>[\w\s'’-]+?)\s+(?:forecasts?|projections?)\b", re.I),
)
_WHAT_IF = re.compile(
    r"\b(?:que\s+se\s+passe(?:rait)?-t-il|qu['’]\s*arrive(?:rait)?-t-il|que\s+se\s+passerait-il|et\s+si|"
    r"what\s+(?:happens|would\s+happen|if)|what\s+would\s+be\s+the\s+effect)\b",
    re.I,
)
_IF_CLAUSE = re.compile(r"\b(?:si|s['’]|if)\s*(?P<t>.+?)" + _END, re.I)
_MAGNITUDE = re.compile(
    r"(?P<v>\d+(?:[.,]\d+)?)\s*(?P<u>pb\b|points?\s+de\s+base|bp\b|bps\b|basis\s+points?|pp\b|points?\s+de\s+"
    r"pourcentage|percentage\s+points?|%|pour\s*cent|percent|mb/[jd]\b|millions?\s+de\s+barils\s+par\s+jour|"
    r"million\s+barrels\s+(?:per|a)\s+day|ans\b|years?\b)",
    re.I,
)
_DECREASE = re.compile(
    r"\b(?:baisse\w*|r[ée]dui\w*|diminu\w*|abaiss\w*|coupe\w*|cuts?|lowers?|reduces?|decreases?|falls?)\b", re.I
)
UNIT_OF = (
    ("bp", re.compile(r"^(?:pb|points?\s+de\s+base|bps?|basis\s+points?)$", re.I)),
    ("pp", re.compile(r"^(?:pp|points?\s+de\s+pourcentage|percentage\s+points?)$", re.I)),
    ("percent", re.compile(r"^(?:%|pour\s*cent|percent)$", re.I)),
    ("mb/d", re.compile(r"^(?:mb/[jd]|millions?\s+de\s+barils\s+par\s+jour|million\s+barrels\s+(?:per|a)\s+day)$", re.I)),
    ("years", re.compile(r"^(?:ans|years?)$", re.I)),
)


def magnitude(question: str) -> tuple[str | None, str | None]:
    """The decided amount of a what-if question (decimal string, signed by a decrease verb) and its unit."""
    m = _MAGNITUDE.search(question)
    if not m:
        return None, None
    unit = next((u for u, rx in UNIT_OF if rx.match(re.sub(r"\s+", " ", m.group("u").strip()))), None)
    if unit is None:
        return None, None
    v = m.group("v").replace(",", ".")
    if _DECREASE.search(question):
        v = "-" + v
    return v, unit


def _family_ops(x: str, families: Sequence[str], reading: str, count: bool, catalog: QaCatalog) -> list[dict]:
    fams = [f for f in families if f in catalog.families]
    if not x or not fams:
        return []
    return [_lookup(x), {"op": "kg_families", "entity": "s1", "families": fams, "reading": reading, "count": count}]


def domain_plan(question: str, catalog: QaCatalog, *, concept_matched: bool, countries: Sequence[str]) -> list[dict] | None:
    """Operations for a V6-domain question shape, or ``None`` (the classic keyword rules then apply)."""
    q = question.strip()
    if _WHAT_IF.search(q):
        m = _IF_CLAUSE.search(q)
        text = (m.group("t") if m else q).strip()
        v, unit = magnitude(q)
        return [{"op": "decision", "text": text, "magnitude": v, "unit": unit}]
    for rx in _EFFECT:
        m = rx.search(q)
        if m and not _PROPER.search(m.group("c")):  # "conséquences de la Première Guerre mondiale sur …": causal
            cause, effect = _x(m.group("c")), m.group("e").strip()
            country = countries[0] if countries else None
            if country:  # "sur l'inflation en France": the country is the scope, not part of the effect words
                effect = re.split(r"\s+(?:en|au|aux|dans|in|for|pour)\s+", effect)[0]
            return [{"op": "effects", "cause_text": cause, "effect_text": _x(effect), "country": country}]
    if _FORECAST_WORD.search(q):
        for rx in _FORECAST_TOPIC:
            m = rx.search(q)
            if m and _x(m.group("t")):
                topic = _x(m.group("t"))
                return [{"op": "forecast", "text": topic, "country": countries[0] if countries else None}]
    for rx in _CAUSED_BY:
        m = rx.search(q)
        if m and _x(m.group("x")):
            x = _x(m.group("x"))
            ops = [_lookup(x), {"op": "why", "entity": "s1", "causal_direction": "effects"}]
            fams = [f for f in ("associated_with", "interacts_with", "context") if f in catalog.families]
            if fams:
                ops.append({"op": "kg_families", "entity": "s1", "families": fams, "reading": "any", "count": False})
            return ops
    if _DEATH_CAUSES.search(q):
        concepts = sorted(c.id for c in catalog.concepts.values() if c.subdomain == DEATH_SUBDOMAIN)
        if concepts and countries:
            years = [int(y) for y in re.findall(r"\b(1[89][0-9]{2}|20[0-9]{2})\b", q)]
            return [
                {"op": "atlas_value", "concept": cid, "country": iso, "year": years[0] if years else None}
                for iso in countries[:2]
                for cid in concepts[:MAX_SUBDOMAIN_CONCEPTS]
            ]
    for patterns, families, reading, count in _FAMILY_RULES:
        for rx in patterns:
            m = rx.search(q)
            if m:
                ops = _family_ops(_x(m.group("x")), families, reading, count, catalog)
                if ops:
                    return ops
    if concept_matched:
        return None
    for rx in _COUNT:
        m = rx.search(q)
        if m and _x(m.group("x")):
            x = _x(m.group("x"))
            return [
                _lookup(x),
                {"op": "kg_families", "entity": "s1", "families": ["is_a"], "reading": "inverse", "count": True},
                {"op": "kg_families", "entity": "s1", "families": ["has_part"], "reading": "forward", "count": True},
            ]
    for rx in _CROSS:
        m = rx.search(q)
        if m and _x(m.group("a")) and _x(m.group("b")):
            a, b = _x(m.group("a")), _x(m.group("b"))
            return [
                _lookup(a),
                _lookup(b, "country" if b in _country_names(catalog, countries) else None),
                {
                    "op": "kg_cross",
                    "entity": "s1",
                    "families": [],
                    "other_entity": "s2",
                    "other_families": ["located_in"],
                    "limit": 25,
                },
            ]
    for rx in _CONTENTS:
        m = rx.search(q)
        if m and _x(m.group("x")):
            x = _x(m.group("x"))
            return [
                _lookup(x),
                {"op": "kg_families", "entity": "s1", "families": ["located_in"], "reading": "inverse", "count": False},
                {"op": "kg_families", "entity": "s1", "families": ["has_part"], "reading": "forward", "count": False},
            ]
    return None


def _country_names(catalog: QaCatalog, countries: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for iso in countries:
        c = catalog.countries.get(iso)
        if c is not None:
            out.update(n for n in c.names)
            out.update(x for x in (c.label, c.label_fr) if x)
    return out


# --------------------------------------------------------------------------- registry vocabulary matching
#: Words that say how a quantity moves, not which quantity it is ("hausse du pétrole" → "pétrole").
CHANGE_WORDS = frozenset(
    "hausse hausses baisse baisses augmentation augmentations diminution diminutions choc chocs montee envolee "
    "chute variation variations increase increases rise rises rising shock shocks higher lower drop drops fall "
    "falls change changes cut cuts".split()
)
_STOP = frozenset(
    "a an and at au aux d de des du en et for from in l la le les of on par pour s sur the to un une "
    "quel quelle quels quelles est sont what which is are effet effets effect effects prevision previsions "
    "forecast forecasts".split()
)


def content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", fold(text)) if len(w) >= 3 and w not in _STOP and w not in CHANGE_WORDS]


def _hit(w: str, words: Iterable[str]) -> bool:
    return any(w == x or (len(w) >= 5 and len(x) >= 5 and w[:5] == x[:5]) for x in words)


def match_terms(
    text: str, terms: Mapping[str, Mapping[str, str]], *, limit: int = 6, extra_words: Mapping[str, str] | None = None
) -> list[str]:
    """Registry ids whose fr/en labels hold the most of the text's content words (a word matches a label word
    equal to it or sharing its first 5 letters); at least half of the words must match. Ties are all returned
    (fewest label words first, then id) up to ``limit``: the answer names every candidate it queried."""
    words = list(dict.fromkeys(content_words(text)))
    if not words:
        return []
    scored: list[tuple[float, int, str]] = []
    for tid, labels in terms.items():
        lw = set()
        for v in labels.values():
            lw.update(content_words(re.sub(r"\(.*?\)", " ", str(v))))
        if extra_words and tid in extra_words:
            lw.update(content_words(extra_words[tid]))
        hits = sum(1 for w in words if _hit(w, lw))
        if hits and hits / len(words) >= 0.5:
            scored.append((hits / len(words), len(lw), tid))
    if not scored:
        return []
    top = max(s for s, _, _ in scored)
    return [tid for _, _, tid in sorted((x for x in scored if x[0] == top), key=lambda x: (x[1], x[2]))][:limit]
