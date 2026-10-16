"""Pattern-based causal-candidate extraction from Wikipedia article text (agent CAUSCAND).

Tier **T5** (text-mined), candidate space only (``docs/roadmap/v3_causality.md`` principle 7): a
candidate is never a claim, never shown without the "candidats non sourcés" toggle, never used by NLQA
and never promoted automatically (``promotion_status`` changes only through a human review).

Pipeline (each step deterministic, versioned by :data:`EXTRACTOR_VERSION`):

1. :func:`sentence_records` -- an article's wikitext (one pinned revision) is rendered
   (:mod:`reva.causal_candidates.wikitext`) and split into sentences; only sentences matching the broad
   :data:`PREFILTER` (connective stems) are kept, with their verbatim wikitext slice and the article's own
   wikilinks inside them. Nothing else of the article is stored.
2. :func:`extract_sentence` -- each connective of :data:`reva.causal_candidates.patterns.CONNECTIVES` found
   in a sentence proposes cause/effect arguments **only among the sentence's wikilinks**, by fixed
   positional rules (clause limits, prepositional chains, coordination, relative bridges, fronted
   adverbials). No name matching, no fuzzy lookup: a side without a usable wikilink yields no candidate.
3. :func:`link_candidates` -- each argument's link target is resolved by the MediaWiki API of the same
   wiki (redirects followed, ``pageprops.wikibase_item``) to a Wikidata QID; unresolved, disambiguation,
   non-article, date/year links and self-loops are dropped (counted).
4. Negation and hedge cues (:data:`reva.causal_candidates.patterns.NEGATION` near the connective,
   :data:`reva.causal_candidates.patterns.HEDGE` in the sentence) set flags; the written direction is kept.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from reva.causal_candidates import patterns as P
from reva.causal_candidates.wikitext import Paragraph, paragraphs, sentences

EXTRACTOR_ID = "reva.causal_candidates.extract"
EXTRACTOR_VERSION = "1.0.0"
SOURCE_KEY = "wikipedia.causal_text"
TIER = "T5"
LICENCE = "CC BY-SA 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

#: Broad stems: a sentence is stored only if it contains one of them (superset of every connective rule,
#: so rules can be re-tuned offline on the stored sentences without re-reading articles).
PREFILTER = {
    "en": re.compile(
        r"\b(?:caus\w*|led|lead\w*|result\w*|trigger\w*|due|because|spark\w*|precipitat\w*|"
        r"contribut\w*)\b",
        re.I,
    ),
    "fr": re.compile(
        r"\b(?:provoqu\w*|entra[îi]n\w*|dû|due|dus|dues|cause\w*|caus\w*|déclench\w*|contribu\w*|"
        r"raison|conséquences?)\b",
        re.I,
    ),
}
MAX_LEFT_GAP = 60
MAX_RIGHT_GAP = 50
MAX_SENTENCE_CHARS = 1200
MAX_PAIRS = 4

_DATE_TARGET = re.compile(
    r"^(?:\d{1,4}(?:s)?(?:\s*(?:BC|BCE|AD|CE|av\.?\s*J\.?-C\.?))?|"
    r"\d{1,2}(?:er)?\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)"
    r"(?:\s+\d{1,4})?|(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+\d{1,2})?(?:,?\s+\d{1,4})?|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)|\d{3,4}\s+(?:en|in|au|aux)\s+.+|(?:[IVXLC]+e|\d+(?:st|nd|rd|th|e|er))\s+"
    r"(?:siècle|century|millénaire|millennium)(?:\s+.*)?|(?:Décennie|Années)\s+\d+)$",
    re.I,
)


def is_date_target(target: str) -> bool:
    return bool(_DATE_TARGET.match(target.strip()))


# ----------------------------------------------------------------------------- stored sentences


@dataclass
class SentenceRecord:
    """A stored sentence: rendered text, verbatim wikitext, its wikilinks, and where it comes from."""

    lang: str
    title: str
    page_id: int
    article_qid: str | None
    revision_id: int
    revision_timestamp: str
    section: str
    paragraph: int
    index: int
    sentence: str  # rendered (REVA's rendering of the wikitext; whitespace kept)
    wikitext: str  # verbatim slice of the revision's wikitext
    links: list[dict[str, Any]]  # {target, anchor, start, end} in ``sentence`` offsets
    in_list: bool = False
    templates_dropped: int = 0
    raw_sha256: str = ""
    retrieved_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.lang}:{self.revision_id}:{self.paragraph}:{self.index}"


def sentence_records(
    *,
    lang: str,
    title: str,
    page_id: int,
    article_qid: str | None,
    revision_id: int,
    revision_timestamp: str,
    wikitext: str,
    raw_sha256: str = "",
    retrieved_at: str = "",
    prefilter: bool = True,
) -> list[SentenceRecord]:
    out: list[SentenceRecord] = []
    pre = PREFILTER[lang]
    for pi, para in enumerate(paragraphs(wikitext)):
        for si, (a, b) in enumerate(sentences(para.text, lang)):
            text = para.text[a:b]
            if prefilter and not pre.search(text):
                continue
            if len(text) > MAX_SENTENCE_CHARS:
                continue
            s0, s1 = para.src_span(a, b)
            links = [
                {"target": lk.target, "anchor": lk.anchor, "start": lk.start - a, "end": lk.end - a}
                for lk in para.links_in(a, b)
            ]
            out.append(
                SentenceRecord(
                    lang=lang,
                    title=title,
                    page_id=page_id,
                    article_qid=article_qid,
                    revision_id=revision_id,
                    revision_timestamp=revision_timestamp,
                    section=para.section,
                    paragraph=pi,
                    index=si,
                    sentence=text,
                    wikitext=wikitext[s0:s1],
                    links=links,
                    in_list=para.in_list,
                    templates_dropped=para.dropped_between(a, b),
                    raw_sha256=raw_sha256,
                    retrieved_at=retrieved_at,
                )
            )
    return out


def _para_for_tests(text: str) -> Paragraph:  # pragma: no cover - debugging helper
    return paragraphs(text)[0]


# ----------------------------------------------------------------------------- argument rules


@dataclass
class Arg:
    target: str
    anchor: str
    start: int
    end: int
    modifier_head: str | None = None  # unlinked head noun when the link only modifies the argument


@dataclass
class RawCandidate:
    record: SentenceRecord
    connective_id: str
    connective_family: str
    connective_text: str
    connective_span: tuple[int, int]
    direction: str
    relation_hint: str
    listed_connective: bool
    cause: Arg
    effect: Arg
    flags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _L:
    target: str
    anchor: str
    start: int
    end: int


def _links(rec: SentenceRecord) -> list[_L]:
    ls = [_L(d["target"], d["anchor"], int(d["start"]), int(d["end"])) for d in rec.links]
    return sorted((x for x in ls if x.target and not is_date_target(x.target)), key=lambda x: x.start)


def _modifier(text: str, before_end: int, floor: int, lang: str) -> str | None:
    m = P.MODIFIER_HEAD[lang].search(text[floor:before_end])
    if not m:
        return None
    head = m.group(1).lower()
    if head in P.NOT_HEADS[lang] or head in P.TRANSPARENT_HEADS[lang]:
        return None
    return head


def _follower_modifier(text: str, end: int, lang: str) -> str | None:
    """English: the word right after a link when it shows the link is a pre-modifier (``None`` if fine)."""
    if lang != "en":
        return None
    rest = text[end : end + 40]
    if re.match(r"^['’]s\b", rest):
        return "'s"
    m = re.match(r"^\s+([A-Za-z][\w-]*)", rest)
    if not m:
        return None  # punctuation, end of sentence, digits
    w = m.group(1)
    if w.lower() in P.EN_FOLLOWERS or w.lower().endswith(("ly", "ed")) or w[0].isupper():
        return None
    return w.lower()


def _left_args(
    text: str, links: Sequence[_L], cs: int, floor: int, lang: str, backward: bool = False
) -> tuple[list[Arg], dict[str, Any]]:
    info: dict[str, Any] = {}
    region = floor
    for m in P.CLAUSE_BREAK[lang].finditer(text, floor, cs):
        region = max(region, m.end())
    cands = [x for x in links if x.start >= region and x.end <= cs]
    if not cands:
        return [], {"left": "no_link"}
    k = len(cands) - 1
    gap = text[cands[k].end : cs]
    if P.RELATIVE[lang].match(gap):
        info["relative"] = True
    elif backward and not P.BACKWARD_LEFT_GAP[lang].match(gap):
        return [], {"left": "effect_not_adjacent"}
    elif P.ANAPHORA[lang].search(gap):
        return [], {"left": "anaphoric"}
    elif len(gap) > MAX_LEFT_GAP:
        return [], {"left": "gap_too_long"}
    elif gap.count(",") % 2:  # "..., encountering little opposition due to": the link is in another clause
        return [], {"left": "gap_crosses_clause"}
    while k > 0 and P.PP_GAP[lang].match(text[cands[k - 1].end : cands[k].start]):
        k -= 1
    first = k
    args_idx = [k]
    closed = False  # an "and"/"or" seen: earlier bare commas are list separators
    while first > 0:
        g = text[cands[first - 1].end : cands[first].start]
        if P.COORD_GAP[lang].match(g):
            closed = True
        elif not (closed and P.LIST_COMMA_GAP[lang].match(g)):
            break
        first -= 1
        args_idx.insert(0, first)
    if len(args_idx) > 1:
        info["coordinated_left"] = True
    # possessor ("[[Germany]]'s [[invasion of Poland]]") belongs to the argument's noun phrase
    lead = first
    while lead > 0 and re.match(r"^['’]s\s+(?:\w+\s+){0,2}$", text[cands[lead - 1].end : cands[lead].start]):
        lead -= 1
    # subject position: the argument opens its clause (after at most an introductory phrase and a few
    # determiners/adjectives); a link deeper in the clause is an object or a modifier, not the subject
    before = text[region : cands[lead].start]
    intro = INTRO_RE.match(before)
    seg = before[intro.end() :] if intro else before
    seg = re.split(r",|\b(?:and|that|et|que|qui)\b", seg, flags=re.I)[-1] if intro or lead == 0 else seg
    if any(x.end <= cands[lead].start and x.start >= region + len(before) - len(seg) for x in cands[:lead]):
        return [], {"left": "not_clause_subject"}
    words = seg.split()
    if len(words) > 4 or (lang == "en" and any(w.lower().endswith("ing") for w in words)):
        return [], {"left": "not_clause_subject"}
    if intro:
        info["introductory_phrase"] = True
    out = []
    for i in args_idx:
        x = cands[i]
        head = _modifier(text, x.start, region, lang) or _follower_modifier(text, x.end, lang)
        out.append(Arg(x.target, x.anchor, x.start, x.end, head))
    _share_modifier(out)
    return out, info


def _share_modifier(args: list[Arg]) -> None:
    """Coordinated links share one noun phrase: if one only modifies an unlinked head, all do."""
    head = next((a.modifier_head for a in args if a.modifier_head), None)
    if head:
        for a in args:
            a.modifier_head = a.modifier_head or head


INTRO_RE = re.compile(
    r"^\s*(?:in|during|after|before|following|by|with|under|from|at|on|amid|despite|since|"
    r"throughout|en|pendant|après|avant|dès|lors|sous|à|au|dans|malgré|suite|depuis|le|la|les)\b"
    r"[^,]*,\s*",
    re.I,
)


def _right_args(
    text: str, links: Sequence[_L], ce: int, ceiling: int, lang: str, stop_at_comma: bool = False
) -> tuple[list[Arg], dict[str, Any]]:
    region_end = ceiling
    m = P.CLAUSE_BREAK[lang].search(text, ce, ceiling)
    if m:
        region_end = m.start()
    cands = [x for x in links if x.start >= ce and x.end <= region_end]
    if not cands:
        return [], {"right": "no_link"}
    first = cands[0]
    gap = text[ce : first.start]
    if len(gap) > MAX_RIGHT_GAP:
        return [], {"right": "gap_too_long"}
    if P.RIGHT_GAP_STOP[lang].search(gap):
        return [], {"right": "gap_crosses_clause"}
    out = [
        Arg(
            first.target,
            first.anchor,
            first.start,
            first.end,
            _modifier(text, first.start, ce, lang) or _follower_modifier(text, first.end, lang),
        )
    ]
    info: dict[str, Any] = {}
    coord: list[_L] = []
    closed = False
    for j in range(len(cands) - 1):
        g = text[cands[j].end : cands[j + 1].start]
        if P.COORD_GAP[lang].match(g):
            coord.append(cands[j + 1])
            closed = True
            break
        if not P.LIST_COMMA_GAP[lang].match(g):
            break
        coord.append(cands[j + 1])
    if coord and closed:
        tail = text[coord[-1].end : region_end]
        if re.match(r"^\s*(?:[,.;]|$)", tail) or len(tail.split()) <= 3:
            out += [Arg(x.target, x.anchor, x.start, x.end, _follower_modifier(text, x.end, lang)) for x in coord]
            info["coordinated_right"] = True
            _share_modifier(out)
    return out, info


def _top_level_comma(text: str, start: int, end: int) -> int:
    depth = 0
    for i in range(start, end):
        ch = text[i]
        if ch in "([«“":
            depth += 1
        elif ch in ")]»”":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            return i
    return -1


def _fronted_effect(text: str, links: Sequence[_L], after: int, ceiling: int, lang: str) -> list[Arg]:
    comma = _top_level_comma(text, after, ceiling)
    if comma < 0:
        return []
    cands = [x for x in links if x.start > comma and x.end <= ceiling]
    if not cands:
        return []
    x = cands[0]
    gap = text[comma + 1 : x.start]
    if len(gap) > 30 or P.RIGHT_GAP_STOP[lang].search(gap):
        return []
    # the main clause's subject: a verb follows it, so only the preposition rule applies
    return [Arg(x.target, x.anchor, x.start, x.end, _modifier(text, x.start, comma + 1, lang))]


def find_connectives(text: str, lang: str) -> list[tuple[P.Connective, re.Match[str]]]:
    """Non-overlapping connective matches (passives win over actives), in text order."""
    found = [(c, m) for c in P.CONNECTIVES if c.lang == lang for m in c.pattern.finditer(text)]
    found.sort(key=lambda cm: (cm[0].priority, cm[1].start()))
    taken: list[tuple[P.Connective, re.Match[str]]] = []
    for c, m in found:
        if any(m.start() < t.end() and t.start() < m.end() for _, t in taken):
            continue
        taken.append((c, m))
    return sorted(taken, key=lambda cm: cm[1].start())


def cues(text: str, lang: str, cs: int, ce: int) -> dict[str, Any]:
    """Negation cues near the connective (same clause, up to 50 chars before it and 15 after) and hedge cues
    anywhere in the sentence."""
    lo = max(0, cs - 50)
    for m in P.CLAUSE_BREAK[lang].finditer(text, lo, cs):
        lo = max(lo, m.end())
    neg = [m.group(0) for m in P.NEGATION[lang].finditer(text, lo, min(len(text), ce + 15))]
    hedge = [m.group(0) for m in P.HEDGE[lang].finditer(text)]
    return {"negated": bool(neg), "hedged": bool(hedge), "negation_cues": neg, "hedge_cues": hedge}


def extract_sentence(rec: SentenceRecord) -> tuple[list[RawCandidate], list[dict[str, Any]]]:
    """Candidates of one stored sentence, and the reasons connectives yielded none (for the report)."""
    text, lang = rec.sentence, rec.lang
    links = _links(rec)
    conns = find_connectives(text, lang)
    out: list[RawCandidate] = []
    misses: list[dict[str, Any]] = []
    for n, (c, m) in enumerate(conns):
        cs, ce = m.start(), m.end()
        floor = conns[n - 1][1].end() if n else 0
        ceiling = conns[n + 1][1].start() if n + 1 < len(conns) else len(text)
        left, linfo = _left_args(text, links, cs, floor, lang, backward=c.direction == "cause_right")
        right, rinfo = _right_args(text, links, ce, ceiling, lang)
        flags: dict[str, Any] = {**linfo, **rinfo}
        if c.direction == "cause_right" and not left and P.FRONT_OK.match(text[floor:cs]):
            eff = _fronted_effect(text, links, right[0].end, ceiling, lang) if right else []
            if eff:
                left, right = eff, right[:1]
                flags = {"fronted": True}
        if not left or not right:
            misses.append({"connective": c.id, "key": rec.key, **flags})
            continue
        causes, effects = (left, right) if c.direction == "cause_left" else (right, left)
        flags.update(cues(text, lang, cs, ce))
        flags.update(in_list=rec.in_list, templates_dropped=rec.templates_dropped)
        pairs = [(a, b) for a in causes for b in effects if a.target != b.target][:MAX_PAIRS]
        for a, b in pairs:
            out.append(
                RawCandidate(
                    record=rec,
                    connective_id=c.id,
                    connective_family=c.family,
                    connective_text=m.group(0),
                    connective_span=(cs, ce),
                    direction=c.direction,
                    relation_hint=c.relation_hint,
                    listed_connective=c.listed,
                    cause=a,
                    effect=b,
                    flags=dict(flags),
                )
            )
    return out, misses


def extract(records: Iterable[SentenceRecord]) -> tuple[list[RawCandidate], list[dict[str, Any]]]:
    cands: list[RawCandidate] = []
    misses: list[dict[str, Any]] = []
    for r in records:
        c, m = extract_sentence(r)
        cands += c
        misses += m
    return cands, misses


# ----------------------------------------------------------------------------- linking and records


@dataclass(frozen=True)
class Resolution:
    """A link target resolved by the wiki's own API (``action=query&redirects=1&prop=pageprops``)."""

    target: str
    title: str | None  # final page title after redirects (None: missing page)
    qid: str | None
    disambiguation: bool = False
    namespace: int = 0
    redirected: bool = False


def needed_targets(cands: Iterable[RawCandidate]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for c in cands:
        out.setdefault(c.record.lang, set()).update((c.cause.target, c.effect.target))
    return out


_STOP = frozenset(
    "the a an of in on at to and or for by with from de du des la le les l d un une et en au aux "
    "à par sur dans pour".split()
)


def anchor_matches_target(anchor: str, *titles: str | None) -> bool:
    """Whether the displayed anchor shares a content-word stem (5 letters) with its own link target / page
    title. A reviewer hint for piped links to broader articles; never used to link anything."""

    def stems(s: str) -> set[str]:
        return {w[:5] for w in re.findall(r"[^\W\d_]+", s.lower()) if w not in _STOP and len(w) > 2}

    a = stems(anchor)
    return any(a & stems(t) for t in titles if t)


def _arg_doc(a: Arg, r: Resolution) -> dict[str, Any]:
    return {
        "qid": r.qid,
        "link_target": a.target,
        "resolved_title": r.title,
        "redirected": r.redirected,
        "anchor": a.anchor,
        "span": [a.start, a.end],
        "modifier_head": a.modifier_head,
        "anchor_matches_target": anchor_matches_target(a.anchor, a.target, r.title),
    }


def candidate_id(c: RawCandidate, cause_qid: str, effect_qid: str) -> str:
    key = "|".join(
        map(
            str,
            (
                c.record.lang,
                c.record.revision_id,
                c.record.paragraph,
                c.record.index,
                c.connective_span[0],
                c.connective_id,
                cause_qid,
                effect_qid,
                EXTRACTOR_VERSION,
            ),
        )
    )
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def wiki_url(lang: str, revid: int) -> str:
    return f"https://{lang}.wikipedia.org/w/index.php?oldid={int(revid)}"


def link_candidates(
    cands: Iterable[RawCandidate],
    resolved: Mapping[str, Mapping[str, Resolution]],
    *,
    linked_at: str = "",
    keep_modifier_arguments: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Candidate documents (both arguments resolved to QIDs) and drop counts by reason.

    An argument whose link only modifies an unlinked head noun ("the collapse of the [[Soviet Union]]") does
    not denote the linked entity: such candidates are dropped (``modifier_argument``) unless
    ``keep_modifier_arguments``."""
    docs: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    seen: set[str] = set()

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for c in cands:
        if not keep_modifier_arguments and (c.cause.modifier_head or c.effect.modifier_head):
            drop("modifier_argument")
            continue
        res = resolved.get(c.record.lang, {})
        rc, re_ = res.get(c.cause.target), res.get(c.effect.target)
        bad = None
        for r in (rc, re_):
            if r is None:
                bad = bad or "unresolved"
            elif r.title is None:
                bad = bad or "missing_page"
            elif r.namespace != 0:
                bad = bad or "not_an_article"
            elif r.disambiguation:
                bad = bad or "disambiguation_page"
            elif not r.qid:
                bad = bad or "no_wikidata_item"
            elif is_date_target(r.title):
                bad = bad or "date_page"
        if bad:
            drop(bad)
            continue
        assert rc and re_ and rc.qid and re_.qid
        if rc.qid == re_.qid:
            drop("self_loop")
            continue
        cid = candidate_id(c, rc.qid, re_.qid)
        if cid in seen:
            drop("duplicate")
            continue
        seen.add(cid)
        rec = c.record
        flags = dict(c.flags)
        flag = (
            "negated+hedged"
            if flags.get("negated") and flags.get("hedged")
            else ("negated" if flags.get("negated") else "hedged" if flags.get("hedged") else "none")
        )
        docs.append(
            {
                "candidate_id": cid,
                "tier": TIER,
                "source_key": SOURCE_KEY,
                "extractor": {"id": EXTRACTOR_ID, "version": EXTRACTOR_VERSION},
                "cause_qid": rc.qid,
                "effect_qid": re_.qid,
                "relation_hint": c.relation_hint,
                "direction_rule": f"{c.connective_id}:{c.direction}",
                "connective": {
                    "id": c.connective_id,
                    "family": c.connective_family,
                    "text": c.connective_text,
                    "span": list(c.connective_span),
                    "listed": c.listed_connective,
                },
                "cause": _arg_doc(c.cause, rc),
                "effect": _arg_doc(c.effect, re_),
                "negation_hedge_flag": flag,
                "flags": flags,
                "sentence": rec.sentence,
                "sentence_wikitext": rec.wikitext,
                "sentence_sha256": hashlib.sha256(rec.sentence.encode()).hexdigest(),
                "source": {
                    "wiki": f"{rec.lang}wiki",
                    "lang": rec.lang,
                    "article_title": rec.title,
                    "article_qid": rec.article_qid,
                    "page_id": rec.page_id,
                    "revision_id": rec.revision_id,
                    "revision_timestamp": rec.revision_timestamp,
                    "section": rec.section or "(lead)",
                    "paragraph": rec.paragraph,
                    "sentence_index": rec.index,
                    "url": wiki_url(rec.lang, rec.revision_id),
                    "raw_sha256": rec.raw_sha256,
                    "retrieved_at": rec.retrieved_at,
                },
                "rights": {
                    "licence": LICENCE,
                    "licence_url": LICENCE_URL,
                    "attribution": f'Wikipedia contributors, "{rec.title}", {rec.lang}.wikipedia.org, '
                    f"revision {rec.revision_id}",
                    "share_alike": "the stored sentence is CC BY-SA 4.0 text; redistribute under the same licence",
                },
                "promotion_status": "not_promoted",
                "review_status": "unreviewed",
                "linked_at": linked_at,
            }
        )
    return docs, dropped


def record_to_dict(r: SentenceRecord) -> dict[str, Any]:
    return asdict(r)


def record_from_dict(d: Mapping[str, Any]) -> SentenceRecord:
    return SentenceRecord(**dict(d))
