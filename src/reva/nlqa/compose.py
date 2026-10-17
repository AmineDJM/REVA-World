"""Deterministic answer composer (fr/en): execution results → cited sentences.

Rules the templates follow (tested):

* every sentence carries citation ids of evidence items (sourced statements, atlas cells,
  derived links, entity identifications, or *absence* records naming what was consulted);
* a number appears only as an atlas cell reports it (rounded for display with ``≈`` when the
  display differs; the exact decimal string stays in the evidence), with its own unit: values in
  different units (a currency per country) are never ranked or labelled with one unit;
* a projection or estimate says so next to the value, in every sentence (value, series point,
  ranking item); the honesty-relevant source flags are never truncated;
* missing data is said as missing, naming the sources consulted ("aucune source consultée ne
  fournit…"), never filled;
* a path says "relié via" / "connected via" and "connexion, pas une cause" — causal wording only
  for a hop whose property is a Wikidata causal property (P828, P1542, …), labelled "affirmée par
  Wikidata";
* Wikidata statements are labelled community-edited;
* a string written by the LLM (lookup term, unsupported part) is quoted only when it literally
  occurs in the user's question.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from reva.nlqa.executor import Execution, OpResult, Resolved, _dec
from reva.nlqa.plan import in_question
from reva.nlqa.ports import AtlasCell, Entity, KgStatement, estimate_status, pick_label

MAX_SENTENCE_STATEMENTS = 12
MAX_RANK_LIST = 50
MAX_SERIES_LIST = 30
MAX_LINKS = 15
#: Causal claims worded one by one in a ``why`` answer (the rest are cited in one note).
MAX_CAUSAL_SENTENCES = 12

# --------------------------------------------------------------------------- causal wording (v3_causality.md)
#: Every causal sentence names its asserting source and tier ("selon <source> (T3, …)"); REVA asserts nothing.
TIER_TEXT = {
    "fr": {
        "T1": "jeu de données codé par des experts",
        "T2": "référence savante citée",
        "T3": "déclaration Wikidata avec références",
        "T4": "déclaration Wikidata sans référence",
    },
    "en": {
        "T1": "expert-coded dataset",
        "T2": "cited scholarly reference",
        "T3": "Wikidata statement with references",
        "T4": "Wikidata statement without references",
    },
}
#: relation read effect → cause ("<effect> a pour cause immédiate <cause>")
EFFECT_SIDE = {
    "fr": {
        "has_cause": "a pour cause",
        "has_effect": "a pour cause",
        "immediate_cause": "a pour cause immédiate",
        "underlying_cause": "a pour cause profonde",
        "contributing_factor": "a pour facteur contributif",
        "enabled": "a été rendu(e) possible par",
        "precipitated": "a été précipité(e) par",
        "prevented_or_delayed": "a été empêché(e) ou retardé(e) par",
        "reaction_to": "est une réaction à",
        "coded_trigger": "a pour déclencheur codé",
        "coded_outcome": "est l'issue codée de",
    },
    "en": {
        "has_cause": "has cause",
        "has_effect": "has cause",
        "immediate_cause": "has immediate cause",
        "underlying_cause": "has underlying cause",
        "contributing_factor": "has contributing factor",
        "enabled": "was enabled by",
        "precipitated": "was precipitated by",
        "prevented_or_delayed": "was prevented or delayed by",
        "reaction_to": "is a reaction to",
        "coded_trigger": "has coded trigger",
        "coded_outcome": "is the coded outcome of",
    },
}
#: relation read cause → effect ("<cause> a pour effet <effect>")
CAUSE_SIDE = {
    "fr": {
        "has_cause": "a pour effet",
        "has_effect": "a pour effet",
        "immediate_cause": "est une cause immédiate de",
        "underlying_cause": "est une cause profonde de",
        "contributing_factor": "est un facteur contributif de",
        "enabled": "a rendu possible",
        "precipitated": "a précipité",
        "prevented_or_delayed": "a empêché ou retardé",
        "reaction_to": "a suscité en réaction",
        "coded_trigger": "est le déclencheur codé de",
        "coded_outcome": "a pour issue codée",
    },
    "en": {
        "has_cause": "has effect",
        "has_effect": "has effect",
        "immediate_cause": "is an immediate cause of",
        "underlying_cause": "is an underlying cause of",
        "contributing_factor": "is a contributing factor of",
        "enabled": "enabled",
        "precipitated": "precipitated",
        "prevented_or_delayed": "prevented or delayed",
        "reaction_to": "prompted in reaction",
        "coded_trigger": "is the coded trigger of",
        "coded_outcome": "has coded outcome",
    },
}
TEMPORAL_TEXT = {
    "fr": {
        "consistent": "la cause précède l'effet, à la précision des dates",
        "overlapping": "périodes qui se chevauchent, antériorité non établie",
        "inconsistent": "incohérence temporelle signalée : la cause commence après l'effet",
        "undetermined": "antériorité non vérifiable : date absente ou hors de la plage prise en charge",
    },
    "en": {
        "consistent": "the cause precedes the effect, at date precision",
        "overlapping": "overlapping periods, precedence not established",
        "inconsistent": "temporal inconsistency flagged: the cause starts after the effect",
        "undetermined": "precedence not checkable: a date is missing or outside the supported range",
    },
}
#: ``undetermined`` by its reason (CAUSCORE ``grade.temporal``): an entity dated by inception (a state, a polity) is
#: compared through its existence span — its dates are not missing; an undated side is named.
TEMPORAL_UNDETERMINED_TEXT = {
    "fr": {
        "entity": "{who} datée par sa création (P571) : une entité, pas un événement ; sa période d'existence est "
        "comparée, antériorité non établie",
        "undated": "pas de date de début dans le graphe pour {who}, antériorité non vérifiable",
        "who": {"cause": "la cause", "effect": "l'effet", "both": "la cause et l'effet"},
    },
    "en": {
        "entity": "{who} dated by its inception (P571): an entity, not an event; its existence span is compared, "
        "precedence not established",
        "undated": "no start date in the knowledge graph for {who}, precedence not checkable",
        "who": {"cause": "the cause", "effect": "the effect", "both": "the cause and the effect"},
    },
}


def temporal_text(c: Any, lang: str) -> str:
    """The temporal check of a claim in words, with its reason when the check is ``undetermined``."""
    fr = lang == "fr"
    check = c.temporal_check or ""
    if check == "undetermined":
        u = TEMPORAL_UNDETERMINED_TEXT[lang]
        for key, sides in (("entity", c.temporal_entity_sides), ("undated", c.temporal_undated_sides)):
            if sides:
                who = u["who"]["both"] if len(set(sides)) > 1 else u["who"].get(sides[0], sides[0])
                return str(u[key]).format(who=who)
    return TEMPORAL_TEXT[lang].get(check, "vérification temporelle non calculée" if fr else "temporal check not computed")


LANE_ORDER = ("immediate", "underlying", "contributing", "coded", "general", "reaction", "prevention", "outcome")
_RELATION_LANE = {
    "immediate_cause": "immediate",
    "precipitated": "immediate",
    "underlying_cause": "underlying",
    "contributing_factor": "contributing",
    "enabled": "contributing",
    "coded_trigger": "coded",
    "coded_outcome": "outcome",
    "reaction_to": "reaction",
    "prevented_or_delayed": "prevention",
}


def _grade_rank(g: str | None) -> int:
    return "ABCDE".index(g) if g and g in "ABCDE" else 5


def claim_lane(c: Any) -> str:
    return c.lane if c.lane in LANE_ORDER else _RELATION_LANE.get(c.relation, "general")


@dataclass
class Sentence:
    id: str
    text: str
    citations: list[str]
    kind: str  # fact | path | causal | missing | unavailable | unsupported | ambiguous | note | identification
    op_id: str | None = None

    def as_json(self, evidence: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "text": self.text, "kind": self.kind, "citations": self.citations}
        if evidence is not None:
            out["citation_refs"] = [short_ref(evidence[c]) for c in self.citations if c in evidence]
        return out


@dataclass
class Composed:
    sentences: list[Sentence] = field(default_factory=list)

    @property
    def text(self) -> str:
        blocks: list[list[str]] = []
        last = object()
        for s in self.sentences:
            if s.op_id != last:
                blocks.append([])
                last = s.op_id
            blocks[-1].append(s.text)
        return "\n\n".join(" ".join(b) for b in blocks)


def short_ref(item: Mapping[str, Any]) -> str:
    c = item.get("citation") or {}
    k = item.get("kind")
    if k == "kg_statement":
        rev = f" rev {c['revision']}" if c.get("revision") else ""
        return f"{item['id']}: {c.get('source')} {c.get('statement_id')}{rev} {c.get('url')}"
    if k == "atlas_cell":
        sha = (c.get("raw_sha256") or "")[:12]
        return (
            f"{item['id']}: {c.get('source')}/{c.get('dataset')} {c.get('indicator')} {item.get('iso3')} "
            f"{item.get('period_start')} sha256:{sha} {c.get('selector') or ''}"
        ).strip()
    if k == "entity_identification":
        return f"{item['id']}: {c.get('source')} {item.get('qid')} {c.get('url')}"
    if k == "link":
        return f"{item['id']}: link {item.get('link_kind')} {item.get('a')}-{item.get('b')} ({c.get('source')})"
    if k == "causal_claim":
        where = c.get("statement_id") or (c.get("selector") and str(c.get("selector"))) or c.get("dataset") or ""
        rev = f" rev {c['revision']}" if c.get("revision") else ""
        return (
            f"{item['id']}: claim by {c.get('source')} ({c.get('tier')}) {where}{rev} grade {item.get('grade') or 'ungraded'}"
        ).strip()
    if k == "absence":
        return f"{item['id']}: consulted {', '.join(item.get('sources_consulted') or [])}"
    return str(item.get("id"))


# --------------------------------------------------------------------------- formatting
def format_number(value: str | None, lang: str) -> str:
    """Display form of a decimal string: ≤ 2 decimals (≥ 1) or 3 significant digits (< 1), grouped
    thousands; prefixed ``≈`` when the display differs from the exact value."""
    d = _dec(value)
    if d is None:
        return str(value)
    if d == 0:
        shown = Decimal(0)
    elif abs(d) >= 1:
        shown = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    else:
        exp = abs(d).adjusted()  # e.g. 0.0123 -> -2
        shown = d.quantize(Decimal(1).scaleb(exp - 2), rounding=ROUND_HALF_EVEN)
    approx = shown != d
    s = format(shown.normalize() if shown != shown.to_integral() else shown.to_integral(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    neg = s.startswith("-")
    s = s.lstrip("-")
    whole, _, frac = s.partition(".")
    sep, dec = (" ", ",") if lang == "fr" else (",", ".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    out = sep.join(groups) + (dec + frac if frac else "")
    return ("≈ " if approx else "") + ("-" if neg else "") + out


_ISO_DAY = re.compile(r"^([+-]?\d+)-(\d{2})-(\d{2})")


def _year_text(y: int) -> str:
    return f"-{abs(y)}" if y < 0 else str(y)


def period_label(cell: AtlasCell) -> str:
    """``2019`` for a calendar-year cell (half-open [2019-01-01, 2020-01-01)), else ``start–end``.
    Years are astronomical ISO years (``-0043-01-01`` → ``-43``)."""
    s, e = str(cell.period_start), str(cell.period_end)
    ms, me = _ISO_DAY.match(s), _ISO_DAY.match(e)
    if ms and me and ms.group(2, 3) == ("01", "01") and me.group(2, 3) == ("01", "01"):
        ys, ye = int(ms.group(1)), int(me.group(1))
        if ye == ys + 1:
            return _year_text(ys)
    if not e:
        return _year_text(int(ms.group(1))) if ms else s
    return f"{s}–{e}"


def _q(text: str, lang: str) -> str:
    return f"« {text} »" if lang == "fr" else f"“{text}”"


def _period(s: KgStatement, lang: str) -> str:
    fr = lang == "fr"
    if s.point_in_time:
        return f" (en {s.point_in_time.display()})" if fr else f" (in {s.point_in_time.display()})"
    a, b = s.start.display() if s.start else None, s.end.display() if s.end else None
    if a and b:
        return f" (de {a} à {b})" if fr else f" (from {a} to {b})"
    if a:
        return f" (depuis {a})" if fr else f" (since {a})"
    if b:
        return f" (jusqu'en {b})" if fr else f" (until {b})"
    return ""


def _value(c: AtlasCell, lang: str) -> str:
    if c.value is None:
        return _q(c.value_text or "", lang)
    unit = f" {c.unit}" if c.unit else ""
    return f"{format_number(c.value, lang)}{unit}"


#: Flags that change what a value *is* (projection, estimate, missing, period basis): rendered first and
#: never truncated. The others are shown after them, truncated to :data:`MAX_OTHER_FLAGS_CHARS`.
HONESTY_FLAGS = (
    "estimate_kind",
    "projection",
    "period_after_vintage",
    "estimate_type",
    "missing",
    "provider_flag",
    "period_basis",
    "reference_period",
    "reporting_year",
)
MAX_OTHER_FLAGS_CHARS = 160


def _flag_text(k: str, v: Any) -> str:
    return k if isinstance(v, bool) and v else f"{k}={v}"


def _flags(c: AtlasCell, lang: str) -> str:
    if not c.flags:
        return ""
    key = [k for k in HONESTY_FLAGS if k in c.flags and c.flags[k] not in (None, "")]
    rest = ", ".join(_flag_text(k, v) for k, v in sorted(c.flags.items()) if k not in key)
    if len(rest) > MAX_OTHER_FLAGS_CHARS:
        rest = rest[:MAX_OTHER_FLAGS_CHARS].rstrip() + "…"
    flags = ", ".join([*(_flag_text(k, c.flags[k]) for k in key), *([rest] if rest else [])])
    return f" ; signalements de la source : {flags}" if lang == "fr" else f"; source flags: {flags}"


def _status(c: AtlasCell, lang: str) -> str:
    """`` (projection)`` / `` (estimate)`` next to a value the source projects or estimates."""
    st = estimate_status(c.flags)
    if st == "projection":
        return " (projection de la source)" if lang == "fr" else " (source projection)"
    if st == "estimate":
        return " (estimation de la source)" if lang == "fr" else " (source estimate)"
    return ""


class Composer:
    def __init__(self, execution: Execution, question: str, catalog: Any) -> None:
        self.x = execution
        self.q = question
        self.catalog = catalog
        self.lang = execution.plan.language
        self.out = Composed()

    # -- helpers
    def add(self, text: str, cites: Sequence[str], kind: str, op_id: str | None) -> None:
        cites = list(dict.fromkeys(c for c in cites if c))
        self.out.sentences.append(Sentence(f"S{len(self.out.sentences) + 1}", text, cites, kind, op_id))

    def fr(self) -> bool:
        return self.lang == "fr"

    def country(self, iso3: str) -> str:
        return pick_label(self.x.country_labels.get(iso3), self.lang, self.catalog.country_label(iso3, self.lang))

    def concept(self, c: Any) -> str:
        return c.label(self.lang)

    def entity(self, e: Entity) -> str:
        return e.label(self.lang)

    def lookup_term(self, r: Resolved) -> str:
        return (
            _q(r.text, self.lang)
            if in_question(r.text, self.q)
            else ("un terme de la question" if self.fr() else "a term of the question")
        )

    def source_name(self, src: str) -> str:
        return src

    # -- main
    def compose(self) -> Composed:
        fr = self.fr()
        for op in self.x.plan.operations:
            if op.op == "entity_lookup":
                self.identification(op.id, self.x.lookups[op.id])
        for res in self.x.results:
            getattr(self, f"op_{res.op.op}")(res) if res.status == "ok" else self.not_ok(res)
        for amb in self.x.plan.ambiguities:
            self.ambiguity(amb)
        instruction_said = False
        for u in self.x.plan.unsupported:
            if u.get("source") == "server":
                continue
            if u.get("source") == "server_guard" or "instruction" in str(u.get("reason", "")).lower():
                if not instruction_said:
                    instruction_said = True
                    eid = self.x.evidence.absence("instruction_in_question", ["server guard", "query plan"])
                    self.add(
                        "Les instructions contenues dans la question ont été traitées comme des données et n'ont "
                        "pas été suivies."
                        if fr
                        else "Instructions contained in the question were treated as data and not followed.",
                        [eid],
                        "unsupported",
                        "unsupported",
                    )
                continue
            part = (
                _q(u["text"], self.lang)
                if u.get("in_question")
                else ("une partie de la question" if fr else "a part of the question")
            )
            eid = self.x.evidence.absence("unsupported", ["query plan"], planner=self.x.plan.planner)
            self.add(
                f"Non couvert par les données de REVA : {part}." if fr else f"Not covered by REVA's data: {part}.",
                [eid],
                "unsupported",
                "unsupported",
            )
        rejected = self.x.plan.rejected
        if rejected:
            eid = self.x.evidence.absence(
                "rejected_operations",
                ["server plan validation"],
                reasons=",".join(sorted({r["reason"] for r in rejected})),
            )
            n = len(rejected)
            self.add(
                (
                    f"{n} opération(s) proposée(s) par le planificateur ont été refusées par le serveur "
                    f"(identifiant hors catalogue ou argument invalide) et n'ont pas été exécutées."
                )
                if fr
                else (
                    f"{n} operation(s) proposed by the planner were refused by the server (identifier outside the "
                    f"catalogue or invalid argument) and were not run."
                ),
                [eid],
                "unsupported",
                "rejected",
            )
        self.licences()
        if not any(s.kind in ("fact", "path", "causal") for s in self.out.sentences):
            eid = self.x.evidence.absence(
                "answer", sorted({c for r in self.x.results for c in r.consulted}) or ["no source"]
            )
            self.add(
                "Aucune réponse sourcée n'a pu être construite à partir des données consultées."
                if fr
                else "No sourced answer could be built from the data consulted.",
                [eid],
                "missing",
                "summary",
            )
        return self.out

    # -- licences (review V3b LIC-1): restricted terms of the atlas sources cited, once per licence
    def licences(self) -> None:
        fr = self.fr()
        by: dict[tuple[str, str], tuple[Mapping[str, Any], list[str]]] = {}
        for item in self.x.evidence.items:
            if item.get("kind") != "atlas_cell":
                continue
            c = item.get("citation") or {}
            r = c.get("rights")
            if not isinstance(r, Mapping) or not r.get("restricted"):
                continue
            by.setdefault((str(c.get("source")), str(r.get("licence"))), (r, []))[1].append(str(item["id"]))
        for (src, lic), (r, eids) in sorted(by.items()):
            terms = [
                t
                for t, on in (
                    (("usage non commercial" if fr else "non-commercial use"), r.get("non_commercial")),
                    (("partage dans les mêmes conditions" if fr else "share-alike"), r.get("share_alike")),
                    (("pas de modification" if fr else "no derivatives"), r.get("no_derivatives")),
                    (
                        ("accès non payant" if fr else "no charging for access"),
                        r.get("commercial_use") == "restricted",
                    ),
                    (("licence non ouverte" if fr else "not an open licence"), r.get("open_licence") is False),
                )
                if on
            ]
            self.add(
                f"Licence de {src} : {lic} ({', '.join(terms)}) ; toute réutilisation doit respecter ces conditions."
                if fr
                else f"Licence of {src}: {lic} ({', '.join(terms)}); any reuse must respect these terms.",
                eids,
                "note",
                "licence",
            )

    # -- concept ambiguity (keyword planner)
    def ambiguity(self, a: Mapping[str, Any]) -> None:
        """Several catalog concepts match the question equally: say so and name the candidates (never
        "not covered"); say which one REVA's headline layer chose, or that each is answered separately."""
        fr = self.fr()
        cands = [self.catalog.concepts[c] for c in a["candidates"] if c in self.catalog.concepts]
        labels = "; ".join(f"{self.concept(c)} ({c.id})" for c in cands)
        consulted = ["query plan", "catalog concept labels"]
        if a["resolution"] == "headline":
            consulted.append("registry/atlas/source_order")
        eid = self.x.evidence.absence(
            "concept_ambiguity", consulted, candidates=",".join(c.id for c in cands), resolution=a["resolution"]
        )
        n = len(cands)
        if a["resolution"] == "headline":
            chosen = self.catalog.concepts[a["chosen"]]
            others = "; ".join(f"{self.concept(c)} ({c.id})" for c in cands if c.id != chosen.id)
            text = (
                f"La question correspond également à {n} concepts du catalogue ; REVA retient {self.concept(chosen)} "
                f"({chosen.id}), le concept désigné par sa référence {a['headline']} (ordre des sources REVA). "
                f"Autres concepts possibles, non interrogés (préciser pour les obtenir) : {others}."
                if fr
                else f"The question matches {n} catalog concepts equally; REVA uses {self.concept(chosen)} "
                f"({chosen.id}), the concept designated by its headline {a['headline']} (REVA source order). "
                f"Other possible concepts, not queried (ask for them explicitly): {others}."
            )
            kind = "note"
        elif a["resolution"] == "each_queried":
            text = (
                f"La question correspond également à {n} concepts du catalogue ; chacun est interrogé et donné "
                f"séparément, avec sa propre unité et sa source : {labels}."
                if fr
                else f"The question matches {n} catalog concepts equally; each is queried and answered separately, "
                f"with its own unit and source: {labels}."
            )
            kind = "note"
        else:
            text = (
                f"Question ambiguë : {n} concepts du catalogue correspondent également ({labels}) ; préciser "
                f"lequel (et le pays) pour obtenir une valeur."
                if fr
                else f"Ambiguous question: {n} catalog concepts match equally ({labels}); say which one (and the "
                f"country) to get a value."
            )
            kind = "ambiguous"
        self.add(text, [eid], kind, "ambiguity")

    # -- identification
    def identification(self, op_id: str, r: Resolved) -> None:
        fr = self.fr()
        if r.entity is not None:
            e = r.entity
            how = {
                "label_exact": "libellé exact" if fr else "exact label",
                "alias_exact": "alias exact" if fr else "exact alias",
                "label_prefix": "début de libellé" if fr else "label prefix",
            }.get(r.match or "", r.match or "")
            where = (
                (
                    "recherche Wikidata en ligne, absente du graphe REVA"
                    if fr
                    else "live Wikidata search, not in REVA's graph"
                )
                if e.source != "kg"
                else ("graphe REVA" if fr else "REVA graph")
            )
            text = (
                f"Entité retenue pour {self.lookup_term(r)} : {self.entity(e)} ({e.qid} ; {where} ; {how})."
                if fr
                else f"Entity used for {self.lookup_term(r)}: {self.entity(e)} ({e.qid}; {where}; {how})."
            )
            if r.alternatives:
                alts = ", ".join(f"{a.label(self.lang)} ({a.qid})" for a in r.alternatives[:3])
                text += f" Autres candidats possibles : {alts}." if fr else f" Other possible candidates: {alts}."
            self.add(text, [r.evidence_id or ""], "identification", op_id)
        elif r.iso3 is not None:
            return  # country known by its crosswalk name only; atlas sentences name it
        else:
            consulted = ", ".join(r.consulted) or ("aucun index d'entités" if fr else "no entity index")
            self.add(
                f"Aucune entité ne correspond à {self.lookup_term(r)} (consulté : {consulted})."
                if fr
                else f"No entity matches {self.lookup_term(r)} (consulted: {consulted}).",
                [r.evidence_id or ""],
                "missing",
                op_id,
            )

    # -- non-ok results
    def not_ok(self, res: OpResult) -> None:
        fr = self.fr()
        op = res.op
        if res.status == "unavailable":
            backend = {
                "kg": "le graphe de connaissances" if fr else "the knowledge graph",
                "atlas": "l'atlas" if fr else "the atlas",
                "links": "la couche d'interconnexions" if fr else "the interconnection layer",
                "causal": "le service des affirmations causales" if fr else "the causal claims service",
            }.get(str(res.data.get("backend")), str(res.data.get("backend")))
            eid = self.x.evidence.absence("capability_unavailable", [str(res.data.get("backend"))], operation=op.op)
            self.add(
                f"Indisponible : {backend} n'est pas accessible sur ce serveur ; l'opération {op.op} n'a pas été "
                f"exécutée (capability_unavailable)."
                if fr
                else f"Unavailable: {backend} is not accessible on this server; operation {op.op} was not run "
                f"(capability_unavailable).",
                [eid],
                "unavailable",
                op.id,
            )
            return
        if res.status == "unresolved":
            eid = self.x.evidence.absence("unresolved_entity", ["entity resolution"], operation=op.op)
            self.add(
                f"L'opération {op.op} n'a pas été exécutée : une entité ou un pays de la question n'a pas été "
                f"identifié."
                if fr
                else f"Operation {op.op} was not run: an entity or country of the question was not identified.",
                [eid],
                "missing",
                op.id,
            )
            return
        if res.status == "refused":
            self.refused(res)
            return
        if res.status == "error":
            eid = self.x.evidence.absence("backend_error", res.consulted or [op.op], error=res.message)
            self.add(
                f"L'opération {op.op} a échoué côté serveur ({res.message}) ; aucune donnée n'en est tirée."
                if fr
                else f"Operation {op.op} failed on the server ({res.message}); no data is drawn from it.",
                [eid],
                "unavailable",
                op.id,
            )
            return
        # empty
        consulted = ", ".join(res.consulted)
        d = res.data
        if d.get("only_projections"):
            self.add(
                f"{self.concept(d['concept'])} : la source ne fournit que des projections pour les années récentes ; "
                f"aucune année n'est choisie par défaut parmi des projections (préciser une année)."
                if fr
                else f"{self.concept(d['concept'])}: the source only has projections for recent years; no projection "
                f"year is chosen by default (ask for a specific year).",
                [d["absence"]],
                "missing",
                op.id,
            )
            return
        if op.op in ("kg_neighbours", "kg_timeline"):
            e = d["entity"]
            self.add(
                f"Aucune déclaration correspondante sur {self.entity(e)} ({e.qid}) dans les sources consultées "
                f"({consulted})."
                if fr
                else f"No matching statement about {self.entity(e)} ({e.qid}) in the sources consulted ({consulted}).",
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op == "kg_path":
            a, b = d["from"], d["to"]
            self.add(
                f"Aucun chemin d'au plus {d['max_hops']} étapes entre {self.entity(a)} ({a.qid}) et "
                f"{self.entity(b)} ({b.qid}) n'a été trouvé dans les sources consultées ({consulted})."
                if fr
                else f"No path of at most {d['max_hops']} hops between {self.entity(a)} ({a.qid}) and "
                f"{self.entity(b)} ({b.qid}) was found in the sources consulted ({consulted}).",
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op in ("atlas_value", "atlas_series", "atlas_rank"):
            c = d["concept"]
            iso3 = d.get("iso3")
            where = self.country(iso3) if iso3 and iso3 != "ALL" else ("aucun pays" if fr else "any country")
            a = op.args
            if op.op == "atlas_series":
                when = f"{a['from_year'] or '…'}–{a['to_year'] or '…'}"
            else:
                when = str(a.get("year")) if a.get("year") is not None else ("aucune année" if fr else "any year")
            self.add(
                f"Aucune source consultée ne fournit {self.concept(c)} pour {where} ({when}) "
                f"(sources consultées : {consulted})."
                if fr
                else f"No source consulted provides {self.concept(c)} for {where} ({when}) "
                f"(sources consulted: {consulted}).",
                [d["absence"]],
                "missing",
                op.id,
            )
            for _src, cells in d.get("latest_instead") or []:
                for cell, eid in cells[:1]:
                    self.add(
                        f"Dernière valeur disponible (autre année) : {self.concept(c)} — {self.country(cell.iso3)}, "
                        f"{period_label(cell)}{_status(cell, self.lang)} : {_value(cell, self.lang)} (source : {cell.source}/{cell.dataset}, indicateur "
                        f"{cell.indicator}{_flags(cell, self.lang)})."
                        if fr
                        else f"Latest available value (another year): {self.concept(c)} — {self.country(cell.iso3)}, "
                        f"{period_label(cell)}{_status(cell, self.lang)}: {_value(cell, self.lang)} (source: {cell.source}/{cell.dataset}, indicator "
                        f"{cell.indicator}{_flags(cell, self.lang)}).",
                        [eid],
                        "fact",
                        op.id,
                    )
        elif op.op == "links_pair":
            self.add(
                f"Aucun lien entre {self.country(d['a'])} et {self.country(d['b'])} dans les données consultées "
                f"({consulted}) : absence de lien, pas une valeur nulle."
                if fr
                else f"No link between {self.country(d['a'])} and {self.country(d['b'])} in the data consulted "
                f"({consulted}): no link, not a zero value.",
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op == "why":
            e = d["entity"]
            what = (
                ("les conséquences" if d["direction"] == "effects" else "les causes")
                if fr
                else ("the consequences" if d["direction"] == "effects" else "the causes")
            )
            self.add(
                f"Aucune affirmation causale sourcée sur {what} de {self.entity(e)} ({e.qid}) dans les sources "
                f"consultées ({consulted}) ; REVA ne déduit aucune cause de la chronologie, d'une succession ou "
                f"d'une participation."
                if fr
                else f"No sourced causal claim about {what} of {self.entity(e)} ({e.qid}) in the sources consulted "
                f"({consulted}); REVA infers no cause from chronology, sequence or participation.",
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op == "causal_chain":
            a, b = d["from"], d["to"]
            self.add(
                f"Aucune chaîne d'affirmations causales sourcées d'au plus {d['max_hops']} maillons de "
                f"{self.entity(a)} ({a.qid}) à {self.entity(b)} ({b.qid}) dans les sources consultées ({consulted}) ; "
                f"cela ne prouve ni l'absence ni l'existence d'un lien causal."
                if fr
                else f"No chain of sourced causal claims of at most {d['max_hops']} hops from {self.entity(a)} "
                f"({a.qid}) to {self.entity(b)} ({b.qid}) in the sources consulted ({consulted}); this proves "
                f"neither the absence nor the existence of a causal link.",
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op == "links":
            self.add(
                f"Aucun lien de {self.country(d['iso3'])} dans les sources consultées ({consulted})."
                if fr
                else f"No link of {self.country(d['iso3'])} in the sources consulted ({consulted}).",
                [d["absence"]],
                "missing",
                op.id,
            )

    def refused(self, res: OpResult) -> None:
        fr = self.fr()
        d = res.data
        c = self.concept(d["concept"])
        units = [u for u in d.get("units") or [] if u]
        shown = ", ".join(units[:6]) + (" …" if len(units) > 6 else "")
        reason = d.get("reason")
        if reason == "only_projections":
            text = (
                f"Pas de classement de {c} : la source ne fournit que des projections pour les années récentes, et "
                f"aucune année de projection n'est choisie par défaut (préciser une année)."
                if fr
                else f"No ranking of {c}: the source only has projections for recent years, and no projection year "
                f"is chosen by default (ask for a specific year)."
            )
        else:
            text = (
                f"Pas de classement de {c} : les valeurs sont exprimées dans une unité (monnaie) propre à chaque pays"
                + (f" ({shown})" if shown else "")
                + " ; REVA ne les compare pas et ne convertit pas les monnaies (demander la valeur d'un pays)."
                if fr
                else f"No ranking of {c}: the values are in a unit (currency) specific to each country"
                + (f" ({shown})" if shown else "")
                + "; REVA does not compare them and does not convert currencies (ask for one country's value)."
            )
        self.add(text, [d["absence"]], "unsupported", res.op.id)

    # -- KG
    def statement_text(self, s: KgStatement) -> str:
        fr = self.fr()
        prop = _q(s.property_label(self.lang), self.lang)
        causal = ""
        if s.causal:
            causal = " — relation causale affirmée par Wikidata" if fr else " — causal relation asserted by Wikidata"
        return (
            f"{s.subject_label(self.lang)} — {prop} : {s.object_label(self.lang)}{_period(s, self.lang)}{causal}"
            if fr
            else f"{s.subject_label(self.lang)} — {prop}: {s.object_label(self.lang)}{_period(s, self.lang)}{causal}"
        )

    def hop_text(self, hop: Any, *, tag: bool = True) -> str:
        fr = self.fr()
        if hop.statement is not None:
            s = hop.statement
            src = (
                (" (Wikidata, donnée communautaire)" if fr else " (Wikidata, community-edited)")
                if s.source_key.startswith("wikidata")
                else f" ({s.source_key})"
            )
            return self.statement_text(s) + (src if tag else "")
        lk = hop.link
        subj = pick_label(lk.subject_labels, self.lang, lk.subject_qid)
        obj = pick_label(lk.object_labels, self.lang, lk.object_qid)
        period = f" ({lk.period})" if lk.period else ""
        n = len(lk.statements) + len(lk.cells)
        return (
            f"{subj} — « lien dérivé : {lk.link_type} » : {obj}{period}"
            + (f" (dérivé par {lk.derived_by} de {n} enregistrement(s) sourcé(s))" if tag else "")
            if fr
            else f"{subj} — “derived link: {lk.link_type}”: {obj}{period}"
            + (f" (derived by {lk.derived_by} from {n} sourced record(s))" if tag else "")
        )

    def op_kg_neighbours(self, res: OpResult) -> None:
        self._statements(res, timeline=False)

    def op_kg_timeline(self, res: OpResult) -> None:
        self._statements(res, timeline=True)

    def _statements(self, res: OpResult, *, timeline: bool) -> None:
        fr = self.fr()
        rows = res.data["statements"]
        for s, eid in rows[:MAX_SENTENCE_STATEMENTS]:
            body = self.statement_text(s)
            if timeline and s.when() is not None:
                body = f"{s.when().display()} : {body}" if fr else f"{s.when().display()}: {body}"  # type: ignore[union-attr]
            tag = " (Wikidata, donnée communautaire)" if fr else " (Wikidata, community-edited)"
            if not s.source_key.startswith("wikidata"):
                tag = f" ({s.source_key})"
            self.add(body + tag + ".", [eid], "fact", res.op.id)
        rest = rows[MAX_SENTENCE_STATEMENTS:]
        if rest:
            self.add(
                f"{len(rest)} autre(s) déclaration(s) figurent dans les preuves."
                if fr
                else f"{len(rest)} more statement(s) are listed in the evidence.",
                [eid for _, eid in rest],
                "note",
                res.op.id,
            )

    def op_kg_path(self, res: OpResult) -> None:
        fr = self.fr()
        d = res.data
        a, b = d["from"], d["to"]
        for i, (path, hops) in enumerate(d["paths"]):
            cites = [e for _, eids in hops for e in eids]
            n = len(hops)
            causal_any = any(h.causal for h, _ in hops)
            engine = path.engine
            if i == 0:
                head = (
                    f"{self.entity(a)} est relié(e) à {self.entity(b)} via {n} étape(s) dans le graphe "
                    f"(chemin trouvé par {engine}) ; ce chemin montre une connexion, pas une cause"
                    if fr
                    else f"{self.entity(a)} is connected to {self.entity(b)} via {n} hop(s) in the graph "
                    f"(path found by {engine}); this path shows a connection, not a cause"
                )
                if causal_any:
                    head += (
                        ", sauf à l'étape où Wikidata affirme lui-même une relation causale"
                        if fr
                        else ", except at the hop where Wikidata itself asserts a causal relation"
                    )
                self.add(head + ".", cites, "path", res.op.id)
                for j, (hop, eids) in enumerate(hops, 1):
                    self.add(
                        (f"Étape {j} : " if fr else f"Hop {j}: ") + self.hop_text(hop) + ".",
                        eids,
                        "path",
                        res.op.id,
                    )
            else:
                chain = (" ; " if fr else "; ").join(self.hop_text(h, tag=False) for h, _ in hops)
                self.add(
                    (f"Autre chemin relié via : {chain}." if fr else f"Another path, connected via: {chain}."),
                    cites,
                    "path",
                    res.op.id,
                )

    # -- atlas
    def _cell_sentence(self, concept: Any, cell: AtlasCell, src: str, *, first: bool) -> str:
        fr = self.fr()
        year = period_label(cell) + _status(cell, self.lang)
        lead = "" if first else ("Autre source : " if fr else "Other source: ")
        return (
            f"{lead}{self.concept(concept)} — {self.country(cell.iso3)}, {year} : {_value(cell, self.lang)} "
            f"(source : {cell.source}/{cell.dataset}, indicateur {cell.indicator}{_flags(cell, self.lang)})."
            if fr
            else f"{lead}{self.concept(concept)} — {self.country(cell.iso3)}, {year}: {_value(cell, self.lang)} "
            f"(source: {cell.source}/{cell.dataset}, indicator {cell.indicator}{_flags(cell, self.lang)})."
        )

    def op_atlas_value(self, res: OpResult) -> None:
        fr = self.fr()
        d = res.data
        c = d["concept"]
        if d.get("all_countries"):
            cites = [eid for _, eid in d["cells"]]
            span = _year_text(d["year"])
            units = [u for u in d.get("units") or [] if u]
            if len(units) == 1:
                unit = f" ({units[0]})"
            elif units:
                unit = (
                    f" (unités différentes selon le pays : {len(units)} ; non comparables entre elles)"
                    if fr
                    else f" (units differ by country: {len(units)}; not comparable with each other)"
                )
            else:
                unit = ""
            np_, n = d.get("n_projection") or 0, d["n"]
            proj = ""
            if np_:
                proj = (
                    f" ; {np_} de ces valeurs sont des projections de la source"
                    if fr
                    else f"; {np_} of these values are projections of the source"
                )
            if not d.get("year_asked"):
                span += (
                    " (dernière année bien couverte hors projections)"
                    if fr
                    else " (latest well-covered year, projections excluded)"
                )
            listed = (
                (
                    f"les {len(cites)} premières valeurs (sur {n}) figurent dans les preuves (liste tronquée)"
                    if fr
                    else f"the first {len(cites)} values (of {n}) are listed in the evidence (list truncated)"
                )
                if d.get("truncated")
                else ("chaque valeur figure dans les preuves" if fr else "each value is listed in the evidence")
            )
            self.add(
                f"{self.concept(c)}{unit} : valeurs disponibles pour {n} pays en {span} selon {d['source']}{proj} ; "
                f"{listed}."
                if fr
                else f"{self.concept(c)}{unit}: values available for {n} countries in {span} according to "
                f"{d['source']}{proj}; {listed}.",
                cites,
                "fact",
                res.op.id,
            )
            return
        first = True
        for src, cells in d["groups"]:
            for cell, eid in cells[:1]:
                self.add(self._cell_sentence(c, cell, src, first=first), [eid], "fact", res.op.id)
                first = False
        if len(d["groups"]) > 1:
            self.add(
                "Les valeurs de sources différentes sont montrées séparément, sans moyenne ni fusion."
                if fr
                else "Values from different sources are shown separately, never averaged or merged.",
                [eid for _, cells in d["groups"] for _, eid in cells[:1]],
                "note",
                res.op.id,
            )

    def op_atlas_rank(self, res: OpResult) -> None:
        fr = self.fr()
        d = res.data
        c = d["concept"]
        rows = d["rows"][:MAX_RANK_LIST]
        units = {cell.unit or "" for cell, _ in rows}
        assert len(units) <= 1, "rank_cells ranks one unit only"
        unit = next(iter(units), "")
        items = (" ; " if fr else "; ").join(
            f"{i}. {self.country(cell.iso3)} : {format_number(cell.value, self.lang)}{_status(cell, self.lang)}"
            if fr
            else f"{i}. {self.country(cell.iso3)}: {format_number(cell.value, self.lang)}{_status(cell, self.lang)}"
            for i, (cell, _) in enumerate(rows, 1)
        )
        order = (
            ("les plus élevées" if res.op.args["order"] == "desc" else "les plus faibles")
            if fr
            else ("highest" if res.op.args["order"] == "desc" else "lowest")
        )
        u = f" ({unit})" if unit else ""
        year = _year_text(d["year"])
        n_proj = d.get("n_projection") or 0
        if n_proj and n_proj == len(rows):
            year += " (projections de la source)" if fr else " (source projections)"
        elif n_proj:
            year += (
                f" ({n_proj} valeur(s) projetée(s) par la source)"
                if fr
                else f" ({n_proj} value(s) projected by the source)"
            )
        elif not d.get("year_asked"):
            year += " (dernière année bien couverte hors projections)" if fr else " (latest well-covered actual year)"
        self.add(
            f"{self.concept(c)}{u}, {year}, valeurs {order} selon {d['source']} : {items}."
            if fr
            else f"{self.concept(c)}{u}, {year}, {order} values according to {d['source']}: {items}.",
            [eid for _, eid in rows],
            "fact",
            res.op.id,
        )
        note = (
            f"Classement établi sur les {d['n_with_value']} pays ayant une valeur pour {d['year']} dans cette "
            f"source ; les pays sans valeur ne sont pas classés"
            if fr
            else f"Ranking over the {d['n_with_value']} countries with a value for {d['year']} in this source; "
            f"countries without a value are not ranked"
        )
        if d.get("other_sources"):
            note += (
                f" ; autres sources non mélangées : {', '.join(d['other_sources'])}"
                if fr
                else f"; other sources not mixed in: {', '.join(d['other_sources'])}"
            )
        self.add(note + ".", [eid for _, eid in rows], "note", res.op.id)

    def op_atlas_series(self, res: OpResult) -> None:
        fr = self.fr()
        d = res.data
        c = d["concept"]
        for src, cells, total in d["groups"][:2]:
            shown = cells[-MAX_SERIES_LIST:]
            units = {cell.unit or "" for cell, _ in shown if cell.value is not None}
            one_unit = len(units) == 1
            unit = next(iter(units)) if one_unit else ""

            def point(cell: AtlasCell, one_unit: bool = one_unit) -> str:
                v = format_number(cell.value, self.lang) + ("" if one_unit or not cell.unit else f" {cell.unit}")
                sep = " : " if fr else ": "
                return f"{period_label(cell)}{_status(cell, self.lang)}{sep}{v}"

            pts = (" ; " if fr else "; ").join(point(cell) for cell, _ in shown if cell.value is not None)
            u = f" ({unit})" if unit else ""
            if not one_unit and units:
                u = (
                    " (l'unité change dans la série : chaque valeur porte la sienne)"
                    if fr
                    else " (the unit changes within the series: each value carries its own)"
                )
            more = ""
            if total > len(shown):
                more = (
                    f" ({total - len(shown)} valeur(s) antérieure(s) dans les preuves)"
                    if fr
                    else f" ({total - len(shown)} earlier value(s) in the evidence)"
                )
            self.add(
                f"{self.concept(c)}{u} — {self.country(d['iso3'])}, selon {src} : {pts}{more}."
                if fr
                else f"{self.concept(c)}{u} — {self.country(d['iso3'])}, according to {src}: {pts}{more}.",
                [eid for _, eid in shown],
                "fact",
                res.op.id,
            )

    # -- causality: every causal sentence names its asserting source and tier; chains are REVA's compositions
    def selon(self, c: Any) -> str:
        """``selon Wikidata (T3, déclaration Wikidata avec références ; donnée communautaire)``."""
        fr = self.fr()
        name = "Wikidata" if c.community_edited else (f"{c.source_key}/{c.dataset}" if c.dataset else c.source_key)
        tier = TIER_TEXT[self.lang if self.lang in TIER_TEXT else "en"].get(c.tier, c.tier)
        community = (" ; donnée communautaire" if fr else "; community-edited") if c.community_edited else ""
        return (
            f"selon {name} ({c.tier}, {tier}{community})"
            if fr
            else f"according to {name} ({c.tier}, {tier}{community})"
        )

    def grade_text(self, c: Any) -> str:
        fr = self.fr()
        if c.grade is None:
            return "pas encore gradée" if fr else "not graded yet"
        rules = f" ; règles {', '.join(c.grade_rules)}" if fr and c.grade_rules else ""
        if not fr and c.grade_rules:
            rules = f"; rules {', '.join(c.grade_rules)}"
        return f"preuve {c.grade}{rules}" if fr else f"evidence grade {c.grade}{rules}"

    def claim_sentence(self, c: Any, *, direction: str) -> str:
        fr = self.fr()
        lang = "fr" if fr else "en"
        cause, effect = c.cause_label(self.lang), c.effect_label(self.lang)
        if direction == "effects":
            core = f"{cause} {CAUSE_SIDE[lang].get(c.relation, c.relation)} {effect}"
        else:
            core = f"{effect} {EFFECT_SIDE[lang].get(c.relation, c.relation)} {cause}"
        selon = self.selon(c)
        if getattr(c, "supported_by", ()):  # Wikidata records who makes the claim: never "according to Wikidata"
            who = (" et " if fr else " and ").join(c.supported_by)
            selon = (
                f"{selon}, qui attribue cette affirmation à {who} (Wikidata ne l'affirme pas lui-même)"
                if fr
                else f"{selon}, which attributes this claim to {who} (Wikidata does not assert it itself)"
            )
        note = (getattr(c, "attribution_note", None) or {}).get(lang)
        if note:
            core = f"{core} ({note})"
        if c.stance == "denies":
            head = (
                f"{selon[0].upper()}{selon[1:]}, il est faux que « {core} » (la source conteste ce lien)"
                if fr
                else f"{selon[0].upper()}{selon[1:]}, it is not the case that “{core}” (the source denies this link)"
            )
        else:
            head = f"{selon[0].upper()}{selon[1:]}, {core}"
        mech = ""
        if c.mechanism:
            mech = (
                f" ; mécanisme cité par la source : « {c.mechanism} »"
                if fr
                else f"; mechanism quoted from the source: “{c.mechanism}”"
            )
        tt = temporal_text(c, lang)
        corr = ""
        if c.corroboration is not None:
            corr = (
                f" ; {c.corroboration} groupe(s) de sources indépendant(s) affirment ce lien"
                if fr
                else f"; {c.corroboration} independent source group(s) assert this link"
            )
        contested = ""
        if c.contested_by:
            contested = (
                f" ; contestée par {len(c.contested_by)} autre(s) affirmation(s)"
                if fr
                else f"; contested by {len(c.contested_by)} other claim(s)"
            )
        return (
            f"{head}{mech} ({tt}{corr}{contested} ; {self.grade_text(c)})."
            if fr
            else (f"{head}{mech} ({tt}{corr}{contested}; {self.grade_text(c)}).")
        )

    def op_why(self, res: OpResult) -> None:
        fr = self.fr()
        d = res.data
        e = d["entity"]
        rows = sorted(
            d["claims"],
            key=lambda r: (LANE_ORDER.index(claim_lane(r[0])), _grade_rank(r[0].grade), int(r[1][1:])),
        )
        effects = d["direction"] == "effects"
        # coded outcomes (how an event ended, according to a dataset) are listed, never counted as causes/effects
        k = sum(1 for c, _ in rows if claim_lane(c) == "outcome")
        n = len(rows) - k
        what = ("conséquences" if effects else "causes") if fr else ("consequences" if effects else "causes")
        outc = ""
        if k:
            outc = (
                f" ; s'y ajoutent {k} issue(s) codée(s) par des jeux de données (comment l'événement s'est terminé), "
                f"qui ne sont pas des {what}"
                if fr
                else f"; plus {k} dataset-coded outcome(s) (how the event ended), which are not {what}"
            )
        self.add(
            f"{n} affirmation(s) de sources sur les {what} de {self.entity(e)} ({e.qid}){outc} ; REVA n'établit aucune "
            f"causalité : chaque lien ci-dessous est attribué à la source qui l'affirme, avec son niveau de source et "
            f"son grade de preuve (ordinal, pas une probabilité)."
            if fr
            else f"{n} claim(s) by sources about the {what} of {self.entity(e)} ({e.qid}){outc}; REVA establishes no "
            f"causation: each link below is attributed to the source asserting it, with its source tier and evidence "
            f"grade (ordinal, not a probability).",
            [eid for _, eid in rows],
            "note",
            res.op.id,
        )
        for c, eid in rows[:MAX_CAUSAL_SENTENCES]:
            self.add(self.claim_sentence(c, direction=d["direction"]), [eid], "causal", res.op.id)
        rest = rows[MAX_CAUSAL_SENTENCES:]
        if rest:
            self.add(
                f"{len(rest)} autre(s) affirmation(s) causale(s), chacune attribuée à sa source, figurent dans les preuves."
                if fr
                else f"{len(rest)} more causal claim(s), each attributed to its source, are listed in the evidence.",
                [eid for _, eid in rest],
                "note",
                res.op.id,
            )
        for c, eid in d.get("dissent") or []:
            self.add(self.claim_sentence(c, direction=d["direction"]), [eid], "causal", res.op.id)
        if d.get("truncated"):
            self.add(
                "Liste bornée par le service : d'autres affirmations existent."
                if fr
                else "List bounded by the service: more claims exist.",
                [eid for _, eid in rows],
                "note",
                res.op.id,
            )

    def op_causal_chain(self, res: OpResult) -> None:
        from reva.nlqa.ports import chain_grade, hop_grade

        fr = self.fr()
        d = res.data
        a, b = d["from"], d["to"]
        for i, (ch, hops) in enumerate(d["chains"]):
            cites = [e for _, eids in hops for e in eids]
            g = chain_grade(ch)
            gtxt = (
                (f"preuve {g}" if g else "non calculable (maillon non gradé)")
                if fr
                else (f"evidence grade {g}" if g else "not computable (ungraded hop)")
            )
            n = len(hops)
            lead = "" if i == 0 else ("Autre chaîne : " if fr else "Another chain: ")
            self.add(
                f"{lead}Chaîne composée par REVA à partir de {n} affirmation(s) distincte(s), de {self.entity(a)} à "
                f"{self.entity(b)} : aucune source n'affirme la chaîne entière ; force = maillon le plus faible ({gtxt})."
                if fr
                else f"{lead}Chain composed by REVA from {n} separately asserted claim(s), from {self.entity(a)} to "
                f"{self.entity(b)}: no source asserts the whole chain; strength = weakest link ({gtxt}).",
                cites,
                "path",
                res.op.id,
            )
            for j, (h, eids) in enumerate(hops, 1):
                asserting = [c for c in h.claims if c.stance == "asserts"]
                frm = pick_label(h.from_labels, self.lang, h.from_id)
                to = pick_label(h.to_labels, self.lang, h.to_id)
                lang = "fr" if fr else "en"
                parts = [
                    f"{self.selon(c)}, « {CAUSE_SIDE[lang].get(c.relation, c.relation)} » ({self.grade_text(c)})"
                    if fr
                    else f"{self.selon(c)}, “{CAUSE_SIDE[lang].get(c.relation, c.relation)}” ({self.grade_text(c)})"
                    for c in asserting
                ]
                hg = hop_grade(h)
                self.add(
                    f"Maillon {j} : {frm} → {to}, lien affirmé {' ; et '.join(parts)} ; grade du maillon (meilleure "
                    f"affirmation) : {hg or 'non gradé'}."
                    if fr
                    else f"Hop {j}: {frm} → {to}, link asserted {'; and '.join(parts)}; hop grade (best claim): "
                    f"{hg or 'ungraded'}.",
                    eids,
                    "causal",
                    res.op.id,
                )

    # -- links
    def op_links(self, res: OpResult) -> None:
        fr = self.fr()
        for lk, eid, cited in res.data["links"][:MAX_LINKS]:
            period = f" ({lk.period})" if lk.period else ""
            weight = ""
            if lk.weight is not None:
                weight = f", {format_number(lk.weight, self.lang)}{(' ' + lk.unit) if lk.unit else ''}"
            self.add(
                f"{self.country(lk.a_iso3)} — lien {_q(lk.link_type or lk.kind, self.lang)} — {self.country(lk.b_iso3)}"
                f"{period}{weight} (dérivé par {lk.derived_by})."
                if fr
                else f"{self.country(lk.a_iso3)} — {_q(lk.link_type or lk.kind, self.lang)} link — {self.country(lk.b_iso3)}"
                f"{period}{weight} (derived by {lk.derived_by}).",
                [eid, *cited],
                "fact",
                res.op.id,
            )

    # -- links between two countries
    def node(self, iso3: str, node: str | None, pair: Any) -> str:
        if node is None and re.fullmatch(r"[A-Z]{3}", iso3 or ""):
            return self.country(iso3)
        n = node or iso3
        return pick_label(pair.node_labels.get(n), self.lang, n)

    def type_label(self, link_type: str, pair: Any, group: Any = None) -> str:
        labels = (group.labels if group is not None and group.labels else None) or pair.type_labels.get(link_type)
        return pick_label(labels, self.lang, link_type)

    def pair_link_text(self, lk: Any, pair: Any, *, directed: bool, label: str | None = None) -> str:
        fr = self.fr()
        a = self.node(lk.a_iso3, lk.a_node, pair)
        b = self.node(lk.b_iso3, lk.b_node, pair)
        rel = label or self.type_label(lk.link_type or lk.kind, pair)
        via = ", ".join(pick_label(pair.node_labels.get(v), self.lang, v) for v in lk.via)
        via = f" (via {via})" if via else ""
        when = lk.period or ("non daté dans la source" if fr else "undated in the source")
        weight = f", {format_number(lk.weight, self.lang)}{(' ' + lk.unit) if lk.unit else ''}" if lk.weight else ""
        return f"{a} — {rel}{via} {'→' if directed else '—'} {b} ({when}{weight})"

    def op_links_pair(self, res: OpResult) -> None:
        fr = self.fr()
        d = res.data
        pair = d["pair"]
        a, b = self.country(d["a"]), self.country(d["b"])
        op_id = res.op.id
        for g, shown in d["groups"]:
            if not shown:
                continue
            label = self.type_label(g.link_type, pair, g)
            if g.link_type == "unga_vote_agreement":
                self._vote_agreement(g, shown, a, b, op_id)
                continue
            series = all(
                lk.weight is not None and re.fullmatch(r"-?\d{1,4}", str(lk.period or "")) for lk, _, _ in shown
            )
            if series:
                by_dir: dict[tuple[str, str, str | None], list[tuple[Any, str, list[str]]]] = {}
                for item in shown:
                    lk = item[0]
                    by_dir.setdefault((lk.a_iso3, lk.b_iso3, lk.facet), []).append(item)
                for (ia, ib, facet), items in by_dir.items():
                    total = sum(1 for lk in g.links if (lk.a_iso3, lk.b_iso3, lk.facet) == (ia, ib, facet))
                    values = " ; ".join(
                        f"{format_number(lk.weight, self.lang)}{(' ' + lk.unit) if lk.unit else ''} ({lk.period})"
                        for lk, _, _ in items
                    )
                    src = ", ".join(sorted({lk.source for lk, _, _ in items if lk.source})) or g.link_type
                    fac = f" [{facet}]" if facet else ""
                    self.add(
                        f"{label}{fac}, {self.country(ia)} → {self.country(ib)} : {values} ; {total} valeur(s) "
                        f"annuelle(s) dans ce sens (source : {src}, telle que rapportée)."
                        if fr
                        else f"{label}{fac}, {self.country(ia)} → {self.country(ib)}: {values}; {total} yearly "
                        f"value(s) in this direction (source: {src}, as reported).",
                        [c for _, eid, cited in items for c in (eid, *cited)],
                        "fact",
                        op_id,
                    )
                continue
            texts = [self.pair_link_text(lk, pair, directed=g.directed, label=label) for lk, _, _ in shown]
            more = g.count - len(shown)
            tail = (f" ; et {more} autre(s)" if fr else f"; and {more} more") if more > 0 else ""
            self.add(
                (" ; " if fr else "; ").join(texts) + tail + ".",
                [c for _, eid, cited in shown for c in (eid, *cited)],
                "fact",
                op_id,
            )
        for p, (lk, eid, cited), chain in d["predecessors"][:MAX_LINKS]:
            succ = " ; ".join(self.pair_link_text(c, pair, directed=True) for c, _, _ in chain)
            self.add(
                f"Lien avec un État prédécesseur de {self.country(p.predecessor_of)} : "
                f"{self.pair_link_text(lk, pair, directed=True)} ; succession : {succ}."
                if fr
                else f"Link with a predecessor state of {self.country(p.predecessor_of)}: "
                f"{self.pair_link_text(lk, pair, directed=True)}; succession: {succ}.",
                [eid, *cited, *(x for _, ceid, ccited in chain for x in (ceid, *ccited))],
                "fact",
                op_id,
            )
        if d.get("absent"):
            names = ", ".join(self.type_label(t, pair) for t in pair.kinds_without_links)
            self.add(
                f"Aucun lien entre {a} et {b} dans les données consultées pour : {names} (absence de lien, pas une "
                f"valeur nulle)."
                if fr
                else f"No link between {a} and {b} in the data consulted for: {names} (no link, not a zero value).",
                [d["absent"]],
                "missing",
                op_id,
            )
        if pair.truncated:
            cites = (
                [d["absent"]] if d.get("absent") else [eid for _, items in d["groups"][:1] for _, eid, _ in items[:1]]
            )
            self.add(
                f"Lecture bornée par le serveur pour : {', '.join(pair.truncated)} ; d'autres liens peuvent exister."
                if fr
                else f"Bounded read on the server for: {', '.join(pair.truncated)}; more links may exist.",
                cites,
                "note",
                op_id,
            )

    def _vote_agreement(self, g: Any, shown: Sequence[Any], a: str, b: str, op_id: str) -> None:
        fr = self.fr()
        by_issue: dict[str, list[Any]] = {}
        for item in shown:
            by_issue.setdefault(item[0].facet or "issue=all", []).append(item)
        issue_fr = {"issue=all": "tous les votes enregistrés", "issue=co": "votes codés « colonialisme » (Voeten)"}
        issue_en = {"issue=all": "all recorded votes", "issue=co": "votes coded “colonialism” (Voeten)"}
        for issue, items in by_issue.items():
            parts = []
            for lk, _, _ in sorted(items, key=lambda x: str(x[0].period)):
                yrs = re.findall(r"-?\d{4}", str(lk.period or ""))
                span = "–".join(dict.fromkeys(yrs[:1] + yrs[-1:])) or (lk.period or "")
                n, k = lk.basis.get("n_common"), lk.basis.get("n_identical")
                counts = (
                    (f" ({k} identiques sur {n} votes communs)" if fr else f" ({k} identical of {n} common votes)")
                    if n is not None and k is not None
                    else ""
                )
                # the share exactly as computed (4 decimals), never rounded further
                share = str(lk.weight).replace(".", ",") if fr else str(lk.weight)
                if lk.basis.get("partial_period"):  # VOTE-1/2: the years used are not the whole decade
                    span += " (décennie partielle)" if fr else " (partial decade)"
                parts.append(f"{span} : {share}{counts}" if fr else f"{span}: {share}{counts}")
            self.add(
                f"Part de votes identiques à l'AG de l'ONU entre {a} et {b} ({issue_fr.get(issue, issue)} ; indice "
                f"calculé par REVA = votes identiques / votes où les deux ont voté oui, non ou abstention) : "
                + " ; ".join(parts)
                + ". C'est une part stricte de votes identiques (une abstention face à un oui ou un non compte 0 ; ce "
                "n'est pas l'indice « agree » de Voeten, qui compte une abstention pour moitié) : elle ne mesure ni "
                "une amitié ni une convergence d'intérêts."
                if fr
                else f"Share of identical UN General Assembly votes between {a} and {b} ({issue_en.get(issue, issue)}; "
                f"index computed by REVA = identical votes / votes where both voted yes, no or abstain): "
                + "; ".join(parts)
                + ". It is a strict share of identical votes (an abstention against yes or no counts 0; not Voeten's "
                "'agree' index, which counts an abstention as half): it measures neither friendship nor a "
                "convergence of interests.",
                [c for _, eid, cited in items for c in (eid, *cited)],
                "fact",
                op_id,
            )


def compose(execution: Execution, question: str, catalog: Any) -> Composed:
    return Composer(execution, question, catalog).compose()
