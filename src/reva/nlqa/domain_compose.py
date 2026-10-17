"""fr/en templates of the V6-domain operations (agent NLQA6), mixed into :class:`reva.nlqa.compose.Composer`.

Wording rules: a family relation is a sourced statement, never a cause (``associated_with`` is said to be an
association, never a cause); a count of links is not a census; effect estimates keep the attributed wording of
the effect layer ("Selon …, … ; REVA n'établit pas cet effet") one by one, never averaged; a forecast names its
method and its score status (pending → no score yet); a decision scenario starts with « scénario non validé —
pas une probabilité »; missing data is said as "pas de donnée chargée", never as zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from reva.nlqa.bridges import MATCH_TEXT as BRIDGE_MATCH_TEXT
from reva.nlqa.bridges import bridge_sentence, same_rule_sentence
from reva.nlqa.ports import Entity, FamilyNeighbour

MAX_LISTED = 12
MAX_SCENARIO_VARIABLES = 8
DOMAIN_OPS = ("kg_families", "kg_cross", "effects", "forecast", "decision", "atlas_period")
#: Families whose wording must say "not a cause" (they record associations / interactions / context).
NON_CAUSAL_NOTE = {
    "associated_with": (
        "association signalée par la source, jamais une cause",
        "an association reported by the source, never a cause",
    ),
    "interacts_with": ("interaction enregistrée, pas une cause", "a recorded interaction, not a cause"),
    "context": ("relation de contexte, non causale", "a context relation, not causal"),
}
UNIT_TEXT = {
    "percent": (" %", " %"),
    "pp": (" point(s) de pourcentage", " percentage point(s)"),
    "log_points": (" point(s) log", " log point(s)"),
    "pp_gdp": (" point(s) de PIB", " percentage point(s) of GDP"),
}
NO_DATA = ("Pas de donnée chargée", "No data loaded")


def _licence_of(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """(source, licence text) of an evidence item, or None for identifications and absences."""
    k = item.get("kind")
    c = item.get("citation") or {}
    rights = c.get("rights") if isinstance(c.get("rights"), Mapping) else None
    lic = str(rights.get("licence") or rights.get("license") or "") if rights else ""
    if k == "kg_statement":
        src = str(c.get("source") or "?")
        if not lic and src.startswith("wikidata"):
            lic = "CC0-1.0 (Wikidata)"
        return src, lic
    if k == "atlas_cell":
        return f"{c.get('source')}/{c.get('dataset')}", lic
    if k in ("causal_claim", "effect_estimate"):
        return str(c.get("source") or "?"), lic
    if k == "sealed_forecast":
        return "reva.forecast_registry", "REVA (calcul sur tables atlas déclarées)"
    if k == "decision_scenario":
        return "reva.predict", "REVA (calcul)"
    if k == "decision_hop" and c.get("estimate_key"):
        return str(c.get("source") or "?"), lic
    return None


def day_or_period(cell: Any) -> str:
    """A one-day cell ([d, d+1) half-open, or start = end) is named by its day; else :func:`period_label`."""
    import datetime as dt

    from reva.nlqa.compose import period_label

    s, e = str(cell.period_start)[:10], str(cell.period_end)[:10]
    try:
        if s == e or dt.date.fromisoformat(e) - dt.date.fromisoformat(s) == dt.timedelta(days=1):
            return s
    except ValueError:
        pass
    return period_label(cell)


class DomainComposer:
    """Templates for the V6 operations; ``self`` is a :class:`reva.nlqa.compose.Composer`."""

    x: Any
    lang: str

    if TYPE_CHECKING:  # provided by Composer

        def add(self, text: str, cites: Sequence[str], kind: str, op_id: str | None) -> None: ...

        def fr(self) -> bool: ...

        def entity(self, e: Entity) -> str: ...

        def country(self, iso3: str) -> str: ...

        def concept(self, c: Any) -> str: ...

    def _t(self, fr: str, en: str) -> str:
        return fr if self.fr() else en

    def _nodata(self) -> str:
        return NO_DATA[0] if self.fr() else NO_DATA[1]

    def _lic(self, rights: Mapping[str, Any] | None, source: str) -> str:
        lic = str((rights or {}).get("licence") or "") if isinstance(rights, Mapping) else ""
        if not lic and source.startswith("wikidata"):
            lic = "CC0-1.0"
        return lic or self._t("licence non renseignée", "licence not stated")

    def _neighbour(self, n: FamilyNeighbour) -> str:
        from reva.nlqa.compose import format_number

        s = n.statement
        prop = s.property_label(self.lang)
        bits = [n.neighbour.qid, f"« {prop} »" if self.fr() else f"“{prop}”"]
        if n.share is not None:
            v = n.share.get("fraction")
            if v is not None:
                pct = format_number(str(float(v) * 100), self.lang)
                bits.append(
                    self._t(f"part {pct} % ({n.share.get('qualifier')})", f"share {pct}% ({n.share.get('qualifier')})")
                )
            else:
                bits.append(
                    self._t(
                        f"part {n.share.get('value')} {n.share.get('unit') or ''}",
                        f"share {n.share.get('value')} {n.share.get('unit') or ''}",
                    ).strip()
                )
        return f"{n.neighbour.label(self.lang)} ({' ; '.join(bits) if self.fr() else '; '.join(bits)})"

    # ------------------------------------------------------------------ families
    def op_kg_families(self, res: Any) -> None:
        d, op = res.data, res.op
        e = d["entity"]
        for idn, eid in d.get("via") or []:
            self.add(
                self._t(
                    f"Aucune relation demandée n'est chargée sur {self.entity(e)} ({e.qid}) lui-même ; les relations "
                    f"ci-dessous sont lues sur {idn.neighbour.label(self.lang)} ({idn.neighbour.qid}), que "
                    f"{idn.statement.source_key} identifie à la même entité (identité selon « "
                    f"{idn.statement.property_label(self.lang)} », pas une fusion de fiches).",
                    f"No requested relation is loaded on {self.entity(e)} ({e.qid}) itself; the relations below are "
                    f"read on {idn.neighbour.label(self.lang)} ({idn.neighbour.qid}), which {idn.statement.source_key} "
                    f"identifies with the same entity (identity per “{idn.statement.property_label(self.lang)}”, not a "
                    f"merge of records).",
                ),
                [eid],
                "note",
                op.id,
            )
        if d.get("answered_by") is not None and (d.get("bridge") or d.get("match") in BRIDGE_MATCH_TEXT):
            ab, ch, br = d["answered_by"], d["chosen"], d.get("bridge")
            what = "relation"
            self.add(
                bridge_sentence(self.lang, ch, ab, br["lang"], br["label"], what)
                if br
                else str(same_rule_sentence(self.lang, ch, ab, d.get("match"), what)),
                [d.get("answered_by_eid") or ""],
                "identification",
                op.id,
            )
        elif d.get("answered_by") is not None:
            ab, ch = d["answered_by"], d["chosen"]
            self.add(
                self._t(
                    f"Rien de chargé sur {self.entity(ch)} ({ch.qid}) pour cette relation ; réponse lue sur "
                    f"{self.entity(ab)} ({ab.qid}), autre fiche au même libellé dans l'index du graphe (fiches non "
                    f"fusionnées).",
                    f"Nothing loaded on {self.entity(ch)} ({ch.qid}) for this relation; answer read on "
                    f"{self.entity(ab)} ({ab.qid}), another record with the same label in the graph index (records "
                    f"not merged).",
                ),
                [d.get("answered_by_eid") or ""],
                "identification",
                op.id,
            )
        for fam, label, count, items, lower in d.get("fam_groups") or []:
            reading = items[0][0].reading.get(self.lang) or items[0][0].reading.get("en") or label.get(self.lang, fam)
            subject = items[0][0].statement.subject_label(self.lang) if not items[0][0].inverse else None
            who = self.entity(e) if subject is None else subject
            caveat = NON_CAUSAL_NOTE.get(fam)
            tail = f" — {caveat[0] if self.fr() else caveat[1]}" if caveat else ""
            n_total = f"{count}{'+' if lower else ''}"
            if d.get("count"):
                ex = ", ".join(n.neighbour.label(self.lang) for n, _ in items[:3])
                self.add(
                    self._t(
                        f"Rattachements directs chargés pour {who} (relation « {reading} ») : {n_total} déclaration(s) "
                        f"sourcée(s) dans le graphe REVA (exemples : {ex}) — un nombre de déclarations chargées, pas un "
                        f"recensement.",
                        f"Direct links loaded for {who} (relation “{reading}”): {n_total} sourced statement(s) in REVA's "
                        f"graph (examples: {ex}) — a number of loaded statements, not a census.",
                    ),
                    [eid for _, eid in items[:3]],
                    "fact",
                    op.id,
                )
                continue
            listed = (
                " ; ".join(self._neighbour(n) for n, _ in items[:MAX_LISTED])
                if self.fr()
                else "; ".join(self._neighbour(n) for n, _ in items[:MAX_LISTED])
            )
            more = ""
            if count > min(len(items), MAX_LISTED):
                more = self._t(
                    f" ({min(len(items), MAX_LISTED)} montrée(s) sur {n_total})",
                    f" ({min(len(items), MAX_LISTED)} shown of {n_total})",
                )
            srcs = sorted({n.statement.source_key for n, _ in items[:MAX_LISTED]})
            community = any("wikidata" in x for x in srcs)
            src_txt = self._t(
                f" (source : {', '.join(srcs)}{' ; donnée communautaire' if community else ''})",
                f" (source: {', '.join(srcs)}{'; community-edited' if community else ''})",
            )
            self.add(
                self._t(
                    f"{who} — « {reading} »{more} : {listed}{src_txt}{tail}.",
                    f"{who} — “{reading}”{more}: {listed}{src_txt}{tail}.",
                ),
                [eid for _, eid in items[:MAX_LISTED]],
                "fact",
                op.id,
            )
        lits = d.get("literals") or []
        if lits:
            parts = []
            for s, _ in lits:
                share = next(
                    (
                        str(v)
                        for k, v in (s.qualifiers or {}).items()
                        if "share" in str(k) and isinstance(v, str | int | float)
                    ),
                    None,
                )
                extra = self._t(f" (part publiée : {share})", f" (published share: {share})") if share else ""
                parts.append(
                    f"{s.property_label(self.lang)} : « {s.object_label(self.lang)} »{extra}"
                    if self.fr()
                    else f"{s.property_label(self.lang)}: “{s.object_label(self.lang)}”{extra}"
                )
            src = sorted({s.source_key for s, _ in lits})
            self.add(
                self._t(
                    f"Valeurs publiées comme texte par la source ({', '.join(src)}), non reliées à une entité du graphe "
                    f"(aucun rapprochement par le nom) : {' ; '.join(parts)}.",
                    f"Values published as text by the source ({', '.join(src)}), not linked to a graph entity (no "
                    f"matching by name): {'; '.join(parts)}.",
                ),
                [eid for _, eid in lits],
                "fact",
                op.id,
            )

    def op_kg_cross(self, res: Any) -> None:
        d, op = res.data, res.op
        a, b = d["a"], d["b"]
        rows = d["cross_rows"]
        items = []
        for row, _ea, _eb in rows[:MAX_LISTED]:
            ra = row.a.statement.property_label(self.lang)
            rb = row.b.statement.property_label(self.lang)
            items.append(
                f"{row.entity.label(self.lang)} ({row.entity.qid} ; « {ra} » → {self.entity(a)} ; « {rb} » → "
                f"{self.entity(b)} ; {row.a.statement.source_key})"
                if self.fr()
                else f"{row.entity.label(self.lang)} ({row.entity.qid}; “{ra}” → {self.entity(a)}; “{rb}” → "
                f"{self.entity(b)}; {row.a.statement.source_key})"
            )
        total = f"{d['total']}{'+' if d.get('lower_bound') else ''}"
        cites = [x for _, ea, eb in rows[:MAX_LISTED] for x in (ea, eb)]
        self.add(
            self._t(
                f"Entités du graphe REVA reliées à {self.entity(a)} ({a.qid}) et à {self.entity(b)} ({b.qid}) "
                f"(« {', '.join(d['other'])} ») : {total} trouvée(s), {min(len(rows), MAX_LISTED)} montrée(s) : "
                f"{' ; '.join(items)}. Chaque lien est une déclaration de la source ; ce qui n'est pas chargé n'y "
                f"figure pas.",
                f"Entities of REVA's graph linked to {self.entity(a)} ({a.qid}) and to {self.entity(b)} ({b.qid}) "
                f"(“{', '.join(d['other'])}”): {total} found, {min(len(rows), MAX_LISTED)} shown: {'; '.join(items)}. "
                f"Each link is a statement of the source; what is not loaded is not listed.",
            ),
            cites,
            "fact",
            op.id,
        )

    # ------------------------------------------------------------------ effects
    def _terms_sentence(self, d: Mapping[str, Any], op_id: str) -> None:
        for side, fr_side, en_side in (("cause", "la cause", "the cause"), ("effect", "l'effet", "the effect")):
            terms = d.get(f"{side}_terms") or []
            if not terms:
                continue
            names = ", ".join(
                f"{t.labels.get(self.lang) or t.labels.get('en') or t.id} ({t.id}, {t.kind})" for t, _ in terms
            )
            text = d[f"{side}_text"]
            self.add(
                self._t(
                    f"Termes retenus pour {fr_side} « {text} » (libellés du registre des effets) : {names}.",
                    f"Terms used for {en_side} “{text}” (labels of the effect registry): {names}.",
                ),
                [eid for _, eid in terms],
                "identification",
                op_id,
            )

    def _estimate_sentence(self, e: Mapping[str, Any], eid: str, op_id: str, kind: str) -> None:
        src = e.get("source") or {}
        ref = src.get("citation") or {}
        cite = str(ref.get("short") or ref.get("text") or src.get("source_key") or "")
        if ref.get("doi"):
            cite += f", doi:{ref['doi']}"
        loc = f", {src['locator']}" if src.get("locator") else ""
        quote = str(src.get("quote") or "")
        q = (
            f" ; extrait : « {quote[:220]}{'…' if len(quote) > 220 else ''} »"
            if quote and self.fr()
            else (f"; excerpt: “{quote[:220]}{'…' if len(quote) > 220 else ''}”" if quote else "")
        )
        lic = self._lic(src.get("rights"), str(src.get("source_key") or ""))
        wording = str(e.get("wording_fr") if self.fr() else e.get("wording"))
        self.add(
            self._t(
                f"{wording} Référence : {cite}{loc} ({lic}){q}.",
                f"{wording} Reference: {cite}{loc} ({lic}){q}.",
            ),
            [eid],
            kind,
            op_id,
        )

    def op_effects(self, res: Any) -> None:
        d, op = res.data, res.op
        self._terms_sentence(d, op.id)
        for e, eid in d["estimates"]:
            self._estimate_sentence(e, eid, op.id, "estimate")
        self.add(
            self._t(
                f"{len(d['estimates'])} estimation(s) présentée(s) une à une, chacune attribuée à sa source : REVA ne "
                f"les moyenne pas, ne les combine pas et n'établit pas ces effets ; leur transposition à un autre "
                f"pays, une autre période ou un autre choc n'est pas impliquée.",
                f"{len(d['estimates'])} estimate(s) shown one by one, each attributed to its source: REVA does not "
                f"average or combine them and does not establish these effects; transport to another country, period "
                f"or shock is not implied.",
            ),
            [eid for _, eid in d["estimates"]],
            "note",
            op.id,
        )

    # ------------------------------------------------------------------ forecasts
    def _fmt(self, v: Any) -> str:
        from reva.nlqa.compose import format_number

        return format_number(None if v is None else str(v), self.lang)

    def op_forecast(self, res: Any) -> None:
        d, op = res.data, res.op
        models = d.get("models") or {}
        terms = d.get("family_terms") or []
        if terms:
            names = ", ".join(
                f"{(f.get('label_fr') if self.fr() else f.get('label_en'))} ({f.get('key')})" for f, _ in terms
            )
            self.add(
                self._t(
                    f"Familles de questions retenues pour « {d['text']} » (libellés du registre de prévisions) : {names}.",
                    f"Question families used for “{d['text']}” (labels of the forecast registry): {names}.",
                ),
                [eid for _, eid in terms],
                "identification",
                op.id,
            )
        self._not_covering(d, op.id)
        for fam, detail, eid in d["questions"]:
            q = detail.get("question") or {}
            seal = detail.get("seal") or {}
            label = fam.get("label_fr") if self.fr() else fam.get("label_en")
            geo = q.get("geo_label_fr") if self.fr() else q.get("geo_label_en")
            parts = []
            for f in detail.get("forecasts") or []:
                desc = (models.get(str(f.get("model"))) or ("", ""))[1]
                meth = self._t(f" (méthode : {desc})", f" (method: {desc})") if desc else ""
                if f.get("status") != "issued":
                    parts.append(
                        self._t(
                            f"{f.get('model')} : abstention ({f.get('reason')})",
                            f"{f.get('model')}: abstained ({f.get('reason')})",
                        )
                    )
                    continue
                val = self._fmt(f.get("point"))
                qs = f.get("quantiles") or {}
                band = ""
                if qs.get("0.05") is not None and qs.get("0.95") is not None:
                    band = self._t(
                        f" [5 %–95 % : {self._fmt(qs['0.05'])} à {self._fmt(qs['0.95'])}]",
                        f" [5%–95%: {self._fmt(qs['0.05'])} to {self._fmt(qs['0.95'])}]",
                    )
                prob = ""
                if f.get("prob_above") is not None:
                    prob = self._t(
                        f", P(> seuil) = {self._fmt(f['prob_above'])}",
                        f", P(> threshold) = {self._fmt(f['prob_above'])}",
                    )
                parts.append(
                    f"{f.get('model')} : {val}{band}{prob}{meth}"
                    if self.fr()
                    else f"{f.get('model')}: {val}{band}{prob}{meth}"
                )
            status = detail.get("status")
            oc = detail.get("outcome") or {}
            if status == "resolved":
                scored = []
                for f in detail.get("forecasts") or []:
                    sc = f.get("score") or {}
                    nums = [
                        f"{k} {self._fmt(v)}"
                        for k, v in sc.items()
                        if isinstance(v, int | float) and not isinstance(v, bool)
                    ]
                    if nums:
                        scored.append(
                            f"{f.get('model')} : {', '.join(nums[:4])}"
                            if self.fr()
                            else f"{f.get('model')}: {', '.join(nums[:4])}"
                        )
                st = self._t(
                    f"Statut : résolue — valeur publiée {self._fmt(oc.get('value'))} ({oc.get('source') or q.get('resolution', {}).get('source')}) ; "
                    f"scores : {' ; '.join(scored) or 'non calculés'}.",
                    f"Status: resolved — published value {self._fmt(oc.get('value'))} ({oc.get('source') or q.get('resolution', {}).get('source')}); "
                    f"scores: {'; '.join(scored) or 'not computed'}.",
                )
            elif status == "void":
                st = self._t(
                    "Statut : annulée (aucune valeur publiée à temps) — pas de score.",
                    "Status: void (no value published in time) — no score.",
                )
            else:
                exp = (q.get("resolution") or {}).get("expected_date")
                st = self._t(
                    f"Statut : en attente de résolution (attendue vers {exp}) — aucun score encore.",
                    f"Status: pending resolution (expected around {exp}) — no score yet.",
                )
            sha = str(seal.get("batch_sha256") or "")[:12]
            self.add(
                self._t(
                    f"Prévisions scellées du registre REVA — {label} ({fam.get('key')}), {geo}, {q.get('reference_period')} "
                    f"(unité : {q.get('unit')}) : {' ; '.join(parts) or 'aucune'}. {st} Scellées le {seal.get('issued_at')} "
                    f"(lot {sha}, sceau vérifié à la lecture) ; ce sont des prévisions statistiques de référence, pas un conseil.",
                    f"Sealed forecasts of REVA's registry — {label} ({fam.get('key')}), {geo}, {q.get('reference_period')} "
                    f"(unit: {q.get('unit')}): {'; '.join(parts) or 'none'}. {st} Sealed on {seal.get('issued_at')} "
                    f"(batch {sha}, seal verified on read); these are statistical baseline forecasts, not advice.",
                ),
                [eid],
                "forecast",
                op.id,
            )

    def _not_covering(self, d: Mapping[str, Any], op_id: str) -> None:
        nc = d.get("not_covering") or []
        if nc:
            self.add(
                self._t(
                    f"Famille(s) de questions correspondante(s) qui ne couvrent pas {self.country(d['iso3'])} : "
                    f"{', '.join(nc)} ; non montrée(s).",
                    f"Matching question famil(y/ies) that do not cover {self.country(d['iso3'])}: {', '.join(nc)}; "
                    f"not shown.",
                ),
                [d.get("not_covering_eid") or ""],
                "note",
                op_id,
            )

    # ------------------------------------------------------------------ decision scenarios
    def op_decision(self, res: Any) -> None:
        from reva.nlqa.compose import format_number

        d, op = res.data, res.op
        out = d["scenario"]
        ref = d["reference"]
        scen = d["scenario_eid"]
        v = out.get("validation") or {}
        label_fr = "scénario non validé — pas une probabilité"
        label = out.get("label") or label_fr
        changed = ""
        if d.get("magnitude_changed"):
            c = d["magnitude_changed"]
            changed = self._t(
                f" ; grandeur de la question appliquée : {c['to']['value']} {c['to']['unit']} au lieu de "
                f"{c['from'].get('value')} {c['from'].get('unit')}",
                f"; magnitude of the question applied: {c['to']['value']} {c['to']['unit']} instead of "
                f"{c['from'].get('value')} {c['from'].get('unit')}",
            )
        name = ref.get("label_fr") if self.fr() else (ref.get("label_en") or ref.get("label_fr"))
        self.add(
            self._t(
                f"Scénario non validé — pas une probabilité{'' if label == label_fr else f' ({label})'} : moteur de décision REVA, décision de référence "
                f"« {name} » ({ref.get('id')}){changed}. Validation : {v.get('status')} ({v.get('reason') or 'aucune'}).",
                f"Unvalidated scenario — not a probability: REVA decision engine, reference decision "
                f"“{name}” ({ref.get('id')}){changed}. Validation: {v.get('status')} ({v.get('reason') or 'none'}).",
            ),
            [scen, d.get("term") or ""],
            "scenario",
            op.id,
        )
        if d.get("magnitude_not_applied"):
            m = d["magnitude_not_applied"]
            self.add(
                self._t(
                    f"La grandeur de la question ({m['value']} {m['unit']}) n'est pas une unité de cet instrument : le "
                    f"scénario de référence est montré tel quel.",
                    f"The magnitude of the question ({m['value']} {m['unit']}) is not a unit of this instrument: the "
                    f"reference scenario is shown as is.",
                ),
                [scen],
                "note",
                op.id,
            )
        hops = d.get("hops") or {}
        shown = 0
        for var in out.get("variables") or []:
            if shown >= MAX_SCENARIO_VARIABLES:
                break
            periods = [p for p in var.get("periods") or [] if p.get("p50") is not None]
            und = var.get("undated") if isinstance(var.get("undated"), Mapping) else None
            if periods:
                p = dict(periods[-1])
            elif und and und.get("p50") is not None:
                p = {**und, "period": None}
            else:
                continue
            vv = var.get("variable") or {}
            vlabel = vv.get("label_fr") if self.fr() else (vv.get("label_en") or vv.get("label_fr"))
            u = UNIT_TEXT.get(str(var.get("unit")), (f" {var.get('unit')}", f" {var.get('unit')}"))
            unit = u[0] if self.fr() else u[1]

            def num(x: Any) -> str:
                return format_number(str(x), self.lang)

            band = (
                self._t("aucune incertitude publiée sur cette chaîne", "no published uncertainty on this chain")
                if p.get("degenerate")
                else self._t(
                    f"5 %–95 % : {num(p['p05'])} à {num(p['p95'])}", f"5%–95%: {num(p['p05'])} to {num(p['p95'])}"
                )
            )
            path_eids = [hops[h][1] for h in var.get("path") or [] if h in hops]
            srcs = sorted(
                {
                    str(hops[h][0].get("estimate_key"))
                    for h in var.get("path") or []
                    if h in hops and hops[h][0].get("estimate_key")
                }
            )
            via = self._t(f" ; estimations : {', '.join(srcs)}", f"; estimates: {', '.join(srcs)}") if srcs else ""
            self.add(
                self._t(
                    f"{vlabel} — {var.get('geo')} : variation {num(p['p50'])}{unit} "
                    f"{('en ' + str(p['period'])) if p.get('period') else '(non datée : horizon de la source)'} ({band}{via}).",
                    f"{vlabel} — {var.get('geo')}: change {num(p['p50'])}{unit} "
                    f"{('in ' + str(p['period'])) if p.get('period') else '(undated: source horizon)'} ({band}{via}).",
                ),
                [scen, *path_eids],
                "scenario",
                op.id,
            )
            shown += 1
        n_unq = len(out.get("unquantified_links") or [])
        if n_unq:
            self.add(
                self._t(
                    f"{n_unq} lien(s) causal(aux) sourcé(s) sans estimation chiffrée : listé(s) dans les preuves, sans nombre.",
                    f"{n_unq} sourced causal link(s) without a quantified estimate: listed in the evidence, without a number.",
                ),
                [scen],
                "note",
                op.id,
            )
        disc = [str(x) for x in out.get("disclaimers_fr") or []]
        if disc and self.fr():
            self.add(" ".join(disc), [scen], "note", op.id)
        elif disc:
            self.add(
                "Conditional scenario: neither a forecast nor a probability until the model chain passes the judge; "
                "intervals reflect only the published uncertainty of the estimates used; REVA establishes no effect; "
                "one estimate per link and one path per variable, nothing summed or averaged; research, not advice.",
                [scen],
                "note",
                op.id,
            )

    # ------------------------------------------------------------------ atlas without country
    def op_atlas_period(self, res: Any) -> None:
        from reva.nlqa.compose import _status, _value

        d, op = res.data, res.op
        c = d["concept"]
        for src, n, picks, years in d["period_groups"]:
            span = f"{years[0]}–{years[-1]}" if years and years[0] != years[-1] else (str(years[0]) if years else "")
            names = {
                "first": ("première", "first"),
                "last": ("dernière", "last"),
                "min": ("plus basse", "lowest"),
                "max": ("plus haute", "highest"),
            }
            parts = []
            for k, (cell, _eid) in picks.items():
                nm = names[k][0] if self.fr() else names[k][1]
                parts.append(
                    f"{nm} ({day_or_period(cell)}{_status(cell, self.lang)}) : {_value(cell, self.lang)}"
                    if self.fr()
                    else f"{nm} ({day_or_period(cell)}{_status(cell, self.lang)}): {_value(cell, self.lang)}"
                )
            self.add(
                self._t(
                    f"{self.concept(c)}, {span} — {n} valeur(s) publiée(s) par {src} : {' ; '.join(parts)} (valeurs "
                    f"publiées sélectionnées, aucune moyenne calculée).",
                    f"{self.concept(c)}, {span} — {n} value(s) published by {src}: {'; '.join(parts)} (published values "
                    f"selected, no mean computed).",
                ),
                [eid for _, eid in picks.values()],
                "fact",
                op.id,
            )

    # ------------------------------------------------------------------ empty results
    def domain_empty(self, res: Any) -> None:
        d, op = res.data, res.op
        consulted = ", ".join(res.consulted)
        nd = self._nodata()
        if d.get("approximate") is not None:
            r = d["approximate"]
            cands = ", ".join(f"{e.label(self.lang)} ({e.qid})" for e in [r.entity, *r.alternatives[:3]])
            self.add(
                self._t(
                    f"Aucune entité au libellé ou alias exact pour « {r.text} » dans l'index du graphe REVA : seules des "
                    f"correspondances approchées existent ({cands}) ; aucune réponse n'est donnée sur une entité "
                    f"incertaine (préciser le nom).",
                    f"No entity with this exact label or alias for “{r.text}” in REVA's graph index: only approximate "
                    f"matches exist ({cands}); no answer is given about an uncertain entity (give the exact name).",
                ),
                [d["absence"]],
                "missing",
                op.id,
            )
            return
        if op.op == "kg_families":
            e = d["entity"]
            fams = ", ".join(op.args["families"])
            what = self._t("pour compter", "to count") if d.get("count") else ""
            self.add(
                self._t(
                    f"{nd}{' ' + what if what else ''} : aucune relation « {fams} » de {self.entity(e)} ({e.qid}) dans le "
                    f"graphe REVA (consulté : {consulted}) ; une absence de donnée chargée, pas une absence dans le monde.",
                    f"{nd}{' ' + what if what else ''}: no “{fams}” relation of {self.entity(e)} ({e.qid}) in REVA's "
                    f"graph (consulted: {consulted}); data not loaded, not an absence in the world.",
                ),
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op == "kg_cross":
            a, b = d["a"], d["b"]
            self.add(
                self._t(
                    f"{nd} : aucune entité du graphe REVA n'est reliée à la fois à {self.entity(a)} ({a.qid}) et à "
                    f"{self.entity(b)} ({b.qid}) (consulté : {consulted}).",
                    f"{nd}: no entity of REVA's graph is linked both to {self.entity(a)} ({a.qid}) and to "
                    f"{self.entity(b)} ({b.qid}) (consulted: {consulted}).",
                ),
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op == "effects":
            if d.get("unmatched"):
                text = d[f"{d['unmatched']}_text"]
                self.add(
                    self._t(
                        f"{nd} : aucun terme de la couche d'estimations d'effet ne correspond à « {text} » (consulté : "
                        f"{consulted}).",
                        f"{nd}: no term of the effect-estimate layer matches “{text}” (consulted: {consulted}).",
                    ),
                    [d["absence"]],
                    "missing",
                    op.id,
                )
                return
            self._terms_sentence(d, op.id)
            causes = ", ".join(t.id for t, _ in d.get("cause_terms") or []) or self._t("toute cause", "any cause")
            effects = ", ".join(t.id for t, _ in d.get("effect_terms") or []) or self._t("tout effet", "any effect")
            where = f" ({d['country']})" if d.get("country") else ""
            self.add(
                self._t(
                    f"{nd} : aucune estimation publiée de l'effet de {causes} sur {effects}{where} dans la couche "
                    f"d'estimations d'effet (consulté : {consulted}) ; REVA ne déduit pas d'effet d'autres estimations.",
                    f"{nd}: no published estimate of the effect of {causes} on {effects}{where} in the effect-estimate "
                    f"layer (consulted: {consulted}); REVA does not derive an effect from other estimates.",
                ),
                [d["absence"]],
                "missing",
                op.id,
            )
            rel = d.get("related") or []
            if rel:
                self.add(
                    self._t(
                        "Estimations chargées voisines (même cause et autre effet, ou même effet et autre cause) — non "
                        "équivalentes, elles ne répondent pas à la question :",
                        "Neighbouring loaded estimates (same cause and another effect, or same effect and another "
                        "cause) — not equivalent, they do not answer the question:",
                    ),
                    [eid for _, eid in rel],
                    "note",
                    op.id,
                )
                for e, eid in rel:
                    self._estimate_sentence(e, eid, op.id, "related")
        elif op.op == "forecast":
            if not d.get("family_terms") and "families" not in d:
                self.add(
                    self._t(
                        f"{nd} : aucune famille de questions du registre de prévisions ne correspond à « {d['text']} » "
                        f"(consulté : {consulted}).",
                        f"{nd}: no question family of the forecast registry matches “{d['text']}” (consulted: "
                        f"{consulted}).",
                    ),
                    [d["absence"]],
                    "missing",
                    op.id,
                )
                return
            self._not_covering(d, op.id)
            fam_rows = d.get("families") or []
            why = " ; ".join(
                f"{f.get('key')} : {f.get('status')}{(' — ' + str(f.get('status_reason'))) if f.get('status_reason') else ''}"
                for f in fam_rows
            )
            where = self.country(d["iso3"]) if d.get("iso3") else self._t("toute zone", "any area")
            self.add(
                self._t(
                    f"{nd} : aucune prévision scellée pour « {d['text']} » ({where}) dans le registre"
                    + (f" (familles : {why})" if why else "")
                    + f" (consulté : {consulted}).",
                    f"{nd}: no sealed forecast for “{d['text']}” ({where}) in the registry"
                    + (f" (families: {why})" if why else "")
                    + f" (consulted: {consulted}).",
                ),
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op == "decision":
            cand_rows: list[Any] = d.get("candidates") or []
            avail = " ; ".join(
                f"{r.get('id')} (« {r.get('label_fr')} »)"
                if self.fr()
                else f"{r.get('id')} (“{r.get('label_en') or r.get('label_fr')}”)"
                for r in (cand_rows or d.get("references") or [])
            )
            self.add(
                self._t(
                    f"Aucune décision de référence unique du moteur ne correspond à la question "
                    f"({'candidates' if cand_rows else 'décisions disponibles'} : {avail}) ; le moteur ne compose pas de "
                    f"décision à partir d'un texte libre, aucun scénario n'est calculé.",
                    f"No single reference decision of the engine matches the question "
                    f"({'candidates' if cand_rows else 'available decisions'}: {avail}); the engine does not compose a "
                    f"decision from free text, no scenario is computed.",
                ),
                [d["absence"]],
                "missing",
                op.id,
            )
        elif op.op == "atlas_period":
            c = d["concept"]
            span = f"{d.get('from_year') or '…'}–{d.get('to_year') or '…'}"
            self.add(
                self._t(
                    f"Aucune source consultée ne fournit {self.concept(c)} pour {span} (sources consultées : {consulted}).",
                    f"No source consulted provides {self.concept(c)} for {span} (sources consulted: {consulted}).",
                ),
                [d["absence"]],
                "missing",
                op.id,
            )

    # ------------------------------------------------------------------ sources and licences (every answer)
    def sources(self) -> None:
        by: dict[str, set[str]] = {}
        cites: list[str] = []
        for item in self.x.evidence.items:
            sl = _licence_of(item)
            if sl is None:
                continue
            src, lic = sl
            by.setdefault(src, set())
            if lic:
                by[src].add(lic)
            cites.append(str(item["id"]))
        if not by:
            return
        unk = self._t("licence non renseignée dans la réponse", "licence not stated in the answer")

        def short(t: str) -> str:
            return t if len(t) <= 90 else t[:89].rstrip() + "…"

        parts = [f"{s} ({' | '.join(short(x) for x in sorted(lics)) or unk})" for s, lics in sorted(by.items())]
        self.add(
            self._t(f"Sources et licences : {' ; '.join(parts)}.", f"Sources and licences: {'; '.join(parts)}."),
            cites,
            "sources",
            "sources",
        )
