"""Deterministic answer composer (fr/en): execution results → cited sentences.

Rules the templates follow (tested):

* every sentence carries citation ids of evidence items (sourced statements, atlas cells,
  derived links, entity identifications, or *absence* records naming what was consulted);
* a number appears only as an atlas cell reports it (rounded for display with ``≈`` when the
  display differs; the exact decimal string stays in the evidence);
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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from reva.nlqa.executor import Execution, OpResult, Resolved, _dec
from reva.nlqa.plan import in_question
from reva.nlqa.ports import AtlasCell, Entity, KgStatement, pick_label

MAX_SENTENCE_STATEMENTS = 12
MAX_RANK_LIST = 50
MAX_SERIES_LIST = 30
MAX_LINKS = 15


@dataclass
class Sentence:
    id: str
    text: str
    citations: list[str]
    kind: str  # fact | path | missing | unavailable | unsupported | note | identification
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


def period_label(cell: AtlasCell) -> str:
    """``2019`` for a calendar-year cell (half-open [2019-01-01, 2020-01-01)), else ``start–end``."""
    s, e = str(cell.period_start), str(cell.period_end)
    if s[4:10] == "-01-01" and e[4:10] == "-01-01" and s[:4].lstrip("-").isdigit() and e[:4].isdigit():
        if int(e[:4]) == int(s[:4]) + 1:
            return s[:4]
    if s[:4] == e[:4] or not e:
        return s[:4] if not e else f"{s}–{e}"
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


def _flags(c: AtlasCell, lang: str) -> str:
    if not c.flags:
        return ""
    flags = ", ".join(f"{k}={v}" if not isinstance(v, bool) else k for k, v in sorted(c.flags.items()))[:160]
    return f" ; signalements de la source : {flags}" if lang == "fr" else f"; source flags: {flags}"


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
        return pick_label(self.x.country_labels.get(iso3), self.lang, self.catalog.country_label(iso3))

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
        if not any(s.kind in ("fact", "path") for s in self.out.sentences):
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
                        f"{period_label(cell)} : {_value(cell, self.lang)} (source : {cell.source}/{cell.dataset}, indicateur "
                        f"{cell.indicator}{_flags(cell, self.lang)})."
                        if fr
                        else f"Latest available value (another year): {self.concept(c)} — {self.country(cell.iso3)}, "
                        f"{period_label(cell)}: {_value(cell, self.lang)} (source: {cell.source}/{cell.dataset}, indicator "
                        f"{cell.indicator}{_flags(cell, self.lang)}).",
                        [eid],
                        "fact",
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
        year = period_label(cell)
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
            years = sorted({cell.period_start[:4] for cell, _ in d["cells"]})
            span = years[0] if len(years) == 1 else f"{years[0]}–{years[-1]}"
            self.add(
                f"{self.concept(c)} : valeurs disponibles pour {d['n']} pays ({span}) selon {d['source']} ; "
                f"chaque valeur figure dans les preuves."
                if fr
                else f"{self.concept(c)}: values available for {d['n']} countries ({span}) according to {d['source']}; "
                f"each value is listed in the evidence.",
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
        unit = rows[0][0].unit if rows and rows[0][0].unit else ""
        items = (" ; " if fr else "; ").join(
            f"{i}. {self.country(cell.iso3)} : {format_number(cell.value, self.lang)}"
            if fr
            else f"{i}. {self.country(cell.iso3)}: {format_number(cell.value, self.lang)}"
            for i, (cell, _) in enumerate(rows, 1)
        )
        order = (
            ("les plus élevées" if res.op.args["order"] == "desc" else "les plus faibles")
            if fr
            else ("highest" if res.op.args["order"] == "desc" else "lowest")
        )
        u = f" ({unit})" if unit else ""
        self.add(
            f"{self.concept(c)}{u}, {d['year']}, valeurs {order} selon {d['source']} : {items}."
            if fr
            else f"{self.concept(c)}{u}, {d['year']}, {order} values according to {d['source']}: {items}.",
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
            unit = shown[0][0].unit if shown and shown[0][0].unit else ""
            pts = (" ; " if fr else "; ").join(
                f"{cell.period_start[:4]} : {format_number(cell.value, self.lang)}"
                if fr
                else f"{cell.period_start[:4]}: {format_number(cell.value, self.lang)}"
                for cell, _ in shown
                if cell.value is not None
            )
            u = f" ({unit})" if unit else ""
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


def compose(execution: Execution, question: str, catalog: Any) -> Composed:
    return Composer(execution, question, catalog).compose()
