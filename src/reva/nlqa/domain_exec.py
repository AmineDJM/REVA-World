"""Executor of the V6-domain operations (agent NLQA6): relation families, cross lists, effect estimates, sealed
forecasts, decision scenarios and geography-free atlas series.

Same rules as :mod:`reva.nlqa.executor`: every record read becomes an evidence item with its provenance and
licence; an absent backend is ``capability_unavailable``; nothing found is an *absence* naming what was
consulted. Nothing is computed: effect estimates are listed one by one (never pooled or averaged), forecasts are
the sealed records as frozen, a decision scenario is the engine's output labelled unvalidated; for a period of an
indicator without country, the first, last, lowest and highest *published* values are selected (no mean).
Registry terms (effect variables, forecast families, reference decisions) are matched to the question's words by
their own fr/en labels (:func:`reva.nlqa.domains.match_terms`), and every match is shown.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from reva.kernel.errors import RevaError
from reva.nlqa.bridges import STRICT_BRIDGE_MATCHES
from reva.nlqa.domains import match_terms
from reva.nlqa.ports import AtlasCell, CapabilityUnavailable, FamilyNeighbour, FamilyView

if TYPE_CHECKING:
    from reva.nlqa.executor import Evidence, OpResult, Resolved
    from reva.nlqa.plan import PlanOp

log = logging.getLogger(__name__)
MAX_FAMILY_NEIGHBOURS = 40
MAX_IDENTITY_HOPS = 3
MAX_CROSS = 50
MAX_EFFECT_ESTIMATES = 40
MAX_RELATED_ESTIMATES = 4
MAX_FORECAST_FAMILIES = 3
MAX_FORECAST_QUESTIONS = 2
DECISION_DRAWS = 1000
#: Literal statements shown with a family (the source publishes a name or a place as text, not as an entity):
#: matched on the property id / label words, never on values.
LITERAL_HINTS: dict[str, tuple[str, ...]] = {
    "owned_by": ("owner", "propriétaire", "proprietaire", "shareholder", "actionnaire"),
    "owns": ("subsidiar", "filiale"),
    "located_in": ("location", "localisation", "adm1", "address", "adresse", "coordinates", "coordonnées"),
    "produces": ("commodity", "produces", "product"),
    "made_of": ("formula", "formule"),
}
#: Effect-term sources named in the "consulted" list.
EFFECT_VOCAB = "effects vocabulary (registry/effects/variables.yaml, atlas catalog labels, event taxonomy)"


def _rights_text(r: Mapping[str, Any] | None) -> str | None:
    if not isinstance(r, Mapping):
        return None
    return str(r.get("licence") or r.get("license") or "") or None


# --------------------------------------------------------------------------- evidence helpers
def ev_family_edge(ev: Evidence, n: FamilyNeighbour) -> str:
    eid = ev.statement(n.statement)
    item = ev.get(eid)
    item.setdefault("family", {"id": n.family, "reading": dict(n.reading), "inverse": n.inverse, "causal": False})
    if n.subtype:
        item["family"]["subtype"] = n.subtype
    if n.share is not None:
        item["family"]["share"] = dict(n.share)
    if n.rights and "rights" not in item["citation"]:
        item["citation"]["rights"] = dict(n.rights)
    return eid


def ev_literal(ev: Evidence, stmt: Any, rights: Mapping[str, Any] | None) -> str:
    eid = ev.statement(stmt)
    item = ev.get(eid)
    if rights and "rights" not in item["citation"]:
        item["citation"]["rights"] = dict(rights)
    return eid


def ev_term(ev: Evidence, *, registry: str, term_id: str, kind: str, labels: Mapping[str, str], query: str) -> str:
    return ev._add(
        ("term", registry, term_id, query),
        {
            "kind": "registry_term",
            "registry": registry,
            "term_id": term_id,
            "term_kind": kind,
            "labels": dict(labels),
            "query": query,
            "match": "label_words",
            "citation": {"source": registry, "status": "reva_registry"},
        },
    )


def ev_effect(ev: Evidence, e: Mapping[str, Any]) -> str:
    src = e.get("source") or {}
    return ev._add(
        ("effect", e.get("estimate_key")),
        {
            "kind": "effect_estimate",
            "estimate_key": e.get("estimate_key"),
            "cause": {k: (e.get("cause") or {}).get(k) for k in ("kind", "id", "label", "label_fr", "definition")},
            "effect": {k: (e.get("effect") or {}).get(k) for k in ("kind", "id", "label", "label_fr", "definition")},
            "measure": e.get("measure"),
            "unit": e.get("unit"),
            "point": e.get("point"),
            "as_printed": e.get("as_printed"),
            "sign": e.get("sign"),
            "uncertainty": e.get("uncertainty"),
            "horizon": e.get("horizon"),
            "shock": e.get("shock"),
            "scope": e.get("scope"),
            "method": e.get("method"),
            "as_of": e.get("as_of"),
            "applicability": e.get("applicability"),
            "source_caveats": e.get("source_caveats"),
            "wording": e.get("wording"),
            "wording_fr": e.get("wording_fr"),
            "origin": e.get("origin"),
            "citation": {
                "source": src.get("source_key"),
                "source_kind": src.get("kind"),
                "dataset": src.get("dataset"),
                "reference": src.get("citation"),
                "locator": src.get("locator"),
                "quote": src.get("quote"),
                "raw_sha256": src.get("raw_sha256"),
                "rights": src.get("rights"),
                "retrieved_at": src.get("retrieved_at"),
                "status": "attributed_estimate",
            },
        },
    )


def ev_forecast(ev: Evidence, detail: Mapping[str, Any], models: Mapping[str, tuple[str, str]]) -> str:
    q = detail.get("question") or {}
    seal = detail.get("seal") or {}
    fcs = []
    for f in detail.get("forecasts") or []:
        fcs.append(
            {
                "model": f.get("model"),
                "method": (models.get(str(f.get("model"))) or ("", ""))[1] or None,
                "status": f.get("status"),
                "reason": f.get("reason"),
                **{k: f.get(k) for k in ("point", "quantiles", "interval", "prob_above") if f.get(k) is not None},
                "score": f.get("score"),
            }
        )
    return ev._add(
        ("forecast", q.get("question_id")),
        {
            "kind": "sealed_forecast",
            "question_id": q.get("question_id"),
            "family": q.get("family"),
            "geo": q.get("geo"),
            "reference_period": q.get("reference_period"),
            "type": q.get("type"),
            "unit": q.get("unit"),
            "text_fr": q.get("text_fr"),
            "text_en": q.get("text_en"),
            "status": detail.get("status"),
            "outcome": detail.get("outcome"),
            "forecasts": fcs,
            "citation": {
                "source": "reva.forecast_registry",
                "batch_sha256": seal.get("batch_sha256"),
                "issued_at": seal.get("issued_at"),
                "seal_verified": seal.get("verified"),
                "label": seal.get("label"),
                "resolution_source": (q.get("resolution") or {}).get("source"),
                "origin": seal.get("origin"),
                "status": "sealed_forecast",
            },
        },
    )


def ev_scenario(ev: Evidence, out: Mapping[str, Any]) -> str:
    v = out.get("validation") or {}
    return ev._add(
        ("scenario", v.get("chain_signature"), (out.get("decision") or {}).get("id")),
        {
            "kind": "decision_scenario",
            "engine": out.get("engine"),
            "label": out.get("label"),
            "validation": dict(v),
            "decision": out.get("decision"),
            "settings": out.get("settings"),
            "effect_layer": out.get("effect_layer"),
            "gaps": list(out.get("gaps") or [])[:20],
            "citation": {
                "source": "reva.predict (decision engine)",
                "chain_signature": v.get("chain_signature"),
                "status": "unvalidated_scenario" if v.get("status") != "validated" else "validated_scenario",
            },
        },
    )


def ev_hop(ev: Evidence, h: Mapping[str, Any]) -> str:
    raw_src = h.get("source")
    src: Mapping[str, Any] = raw_src if isinstance(raw_src, Mapping) else {}
    key = h.get("estimate_key") or src.get("estimate_key")
    return ev._add(
        ("scenario_hop", h.get("id"), key or h.get("plugin")),
        {
            "kind": "decision_hop",
            "hop": {
                k: h.get(k)
                for k in ("id", "kind", "from", "to", "label_fr", "plugin", "measure", "unit", "coefficient")
                if h.get(k) is not None
            },
            "estimate_key": key,
            "citation": {
                "source": src.get("source_key") or h.get("plugin") or "decision",
                "estimate_key": key,
                "reference": src.get("citation"),
                "doi": src.get("doi"),
                "locator": src.get("locator"),
                "quote": src.get("quote"),
                "raw_sha256": src.get("raw_sha256"),
                "rights": src.get("rights"),
                "status": "attributed_estimate" if key else "scenario_assumption",
            },
        },
    )


# --------------------------------------------------------------------------- operations
class DomainOps:
    """Mixed into :class:`reva.nlqa.executor.Executor` (``self.b`` = backends)."""

    b: Any

    if TYPE_CHECKING:  # provided by Executor

        def _entity(self, ref: str, lookups: dict[str, Resolved]) -> Resolved: ...

        def _iso3(self, country: str, lookups: dict[str, Resolved]) -> str | None: ...

    def _need(self, attr: str, what: str) -> Any:
        port = getattr(self.b, attr, None)
        if port is None:
            raise CapabilityUnavailable(f"{what} is not available on this server", backend=attr)
        return port

    # -- relation families
    def _kg_families(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        from reva.nlqa.executor import OpResult

        a = op.args
        r = self._entity(a["entity"], lookups)
        if r.entity is None:
            return OpResult(op, "unresolved", data={"lookup": a["entity"]})
        port = self._need("families", "the relation-family index of the KG")
        consulted = [port.name]
        approx = _approximate(op, r, ev, consulted)
        if approx is not None:
            return approx
        # the chosen record first, then the records with the same label match (several sources publish the
        # same taxon or compound under one exact label): the answer names the record that answered
        view = port.families(r.entity.qid, families=a["families"], per_family=MAX_FAMILY_NEIGHBOURS)
        groups = _kept_groups(view, a["reading"])
        answered_by = None
        for alt in r.alternatives[:MAX_IDENTITY_HOPS]:
            if any(g[3] for g in groups):
                break
            other = port.families(alt.qid, families=a["families"], per_family=MAX_FAMILY_NEIGHBOURS)
            more = _kept_groups(other, a["reading"])
            if any(g[3] for g in more) or _literals(other, a["families"]):
                view, groups, answered_by = other, more, alt
        via: list[tuple[FamilyNeighbour, str]] = []
        if not any(g[3] for g in groups) and view.identities:
            # nothing on this record: the records a source identifies with it (same_as, identifier evidence)
            for idn in view.identities[:MAX_IDENTITY_HOPS]:
                other = port.families(idn.neighbour.qid, families=a["families"], per_family=MAX_FAMILY_NEIGHBOURS)
                more = _kept_groups(other, a["reading"])
                if any(g[3] for g in more):
                    via.append((idn, ev_family_edge(ev, idn)))
                    groups.extend(more)
        bridge = None
        if not any(g[3] for g in groups) and not via and not _literals(view, a["families"]):
            # V8ASK: the other records carrying one of the chosen record's own labels (reva.nlqa.bridges)
            for other, via_lang, via_label in _label_bridges(self.b, r):
                more_view = port.families(other.qid, families=a["families"], per_family=MAX_FAMILY_NEIGHBOURS)
                more = _kept_groups(more_view, a["reading"])
                if any(g[3] for g in more):
                    view, groups, answered_by = more_view, more, other
                    bridge = {"lang": via_lang, "label": via_label}
                    break
        literals = _literals(view, a["families"])
        data: dict[str, Any] = {
            "entity": answered_by or r.entity,
            "chosen": r.entity,
            "answered_by": answered_by,
            "bridge": bridge,
            "match": r.match,
            "families": a["families"],
            "reading": a["reading"],
            "count": a["count"],
            "kind": view.kind,
        }
        if answered_by is not None:
            data["answered_by_eid"] = ev.entity(answered_by, query=r.text, match=r.match or "")
        if not any(g[3] for g in groups) and not literals:
            data["absence"] = ev.absence(
                "kg_families", consulted, qid=r.entity.qid, families=",".join(a["families"]), reading=a["reading"]
            )
            return OpResult(op, "empty", data=data, consulted=consulted)
        data["fam_groups"] = [
            (fam, label, count, [(n, ev_family_edge(ev, n)) for n in ns], lower)
            for fam, label, count, ns, lower in groups
            if ns
        ]
        data["literals"] = [(s, ev_literal(ev, s, rt)) for s, rt in literals]
        data["via"] = via
        return OpResult(op, "ok", data=data, consulted=consulted)

    def _kg_cross(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        from reva.nlqa.executor import OpResult

        a = op.args
        ra, rb = self._entity(a["entity"], lookups), self._entity(a["other_entity"], lookups)
        if ra.entity is None or rb.entity is None:
            return OpResult(op, "unresolved", data={"lookup": a["entity"] if ra.entity is None else a["other_entity"]})
        port = self._need("families", "the relation-family index of the KG")
        consulted = [port.name]
        for r in (ra, rb):
            approx = _approximate(op, r, ev, consulted)
            if approx is not None:
                return approx
        lim = min(a["limit"], MAX_CROSS)
        res = port.cross(ra.entity.qid, a["families"] or None, rb.entity.qid, a["other_families"], limit=lim)
        data: dict[str, Any] = {"a": ra.entity, "b": rb.entity, "families": a["families"], "other": a["other_families"]}
        if not res.rows:
            # V8ASK: the first side's equally matching records, then the records carrying one of its labels
            # (« charbon » → Wikidata Q24489, labelled "coal" in English → GPPD "Coal"); the answer names it
            cands: list[tuple[Any, dict[str, str] | None]] = []
            if ra.match in ("label_exact", "alias_exact") or ra.match in STRICT_BRIDGE_MATCHES:
                cands += [(alt, None) for alt in ra.alternatives[:MAX_IDENTITY_HOPS]]
            cands += [(o, {"lang": lg, "label": lb}) for o, lg, lb in _label_bridges(self.b, ra)]
            for other, bridge in cands:
                more = port.cross(other.qid, a["families"] or None, rb.entity.qid, a["other_families"], limit=lim)
                if more.rows:
                    res = more
                    data.update(a=other, chosen=ra.entity, answered_by=other, bridge=bridge, match=ra.match)
                    data["answered_by_eid"] = ev.entity(other, query=ra.text, match=ra.match or "")
                    break
        if not res.rows:
            data["absence"] = ev.absence(
                "kg_cross", consulted, a=ra.entity.qid, b=rb.entity.qid, b_families=",".join(a["other_families"])
            )
            return OpResult(op, "empty", data=data, consulted=consulted)
        data.update(
            total=res.total,
            lower_bound=res.total_is_lower_bound,
            cross_rows=[(row, ev_family_edge(ev, row.a), ev_family_edge(ev, row.b)) for row in res.rows],
        )
        return OpResult(op, "ok", data=data, consulted=consulted)

    # -- effect estimates
    def _effects(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        from reva.nlqa.executor import OpResult

        a = op.args
        port = self._need("effects", "the effect-estimate layer")
        consulted = [port.name, EFFECT_VOCAB]
        terms = {t.id: t for t in port.terms()}
        labels = {tid: t.labels for tid, t in terms.items()}
        country = self._iso3(a["country"], lookups) if a.get("country") else None
        sides: dict[str, list[str]] = {}
        data: dict[str, Any] = {"cause_text": a["cause_text"], "effect_text": a["effect_text"], "country": country}
        for side in ("cause", "effect"):
            text = a[f"{side}_text"]
            ids = match_terms(text, labels) if text else []
            if text and not ids:
                data["unmatched"] = side
                data["absence"] = ev.absence(
                    f"effect_{side}_term", consulted, query=text, note="no term of the effect layer matches these words"
                )
                return OpResult(op, "empty", data=data, consulted=consulted)
            sides[side] = ids
            data[f"{side}_terms"] = [
                (
                    terms[i],
                    ev_term(
                        ev, registry=EFFECT_VOCAB, term_id=i, kind=terms[i].kind, labels=terms[i].labels, query=text
                    ),
                )
                for i in ids
            ]
        rows = port.estimates(sides["cause"], sides["effect"], country=country, limit=MAX_EFFECT_ESTIMATES)
        if not rows:
            data["absence"] = ev.absence(
                "effect_estimates",
                consulted,
                causes=",".join(sides["cause"]),
                effects=",".join(sides["effect"]),
                country=country or "any",
            )
            related = []
            if sides["cause"] and sides["effect"]:
                related = list(port.estimates(sides["cause"], [], country=country, limit=MAX_RELATED_ESTIMATES))
                related += list(port.estimates([], sides["effect"], country=country, limit=MAX_RELATED_ESTIMATES))
            data["related"] = [(e, ev_effect(ev, e)) for e in related]
            return OpResult(op, "empty", data=data, consulted=consulted)
        data["estimates"] = [(e, ev_effect(ev, e)) for e in rows]
        return OpResult(op, "ok", data=data, consulted=consulted)

    # -- sealed forecasts
    def _forecast(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        from reva.nlqa.executor import OpResult

        a = op.args
        port = self._need("forecasts", "the forecast registry")
        consulted = [port.name]
        fams = {str(f["key"]): f for f in port.families()}
        labels = {k: {"fr": str(f.get("label_fr") or k), "en": str(f.get("label_en") or k)} for k, f in fams.items()}
        keys = match_terms(a["text"], labels, limit=8)
        iso3 = self._iso3(a["country"], lookups) if a.get("country") else None
        data: dict[str, Any] = {"text": a["text"], "iso3": iso3}
        if not keys:
            data["absence"] = ev.absence("forecast_family", consulted, query=a["text"])
            return OpResult(op, "empty", data=data, consulted=consulted)
        if iso3:
            covering = [k for k in keys if iso3 in (fams[k].get("countries") or [])]
            data["not_covering"] = [k for k in keys if k not in covering][:MAX_FORECAST_FAMILIES]
            if data["not_covering"]:
                data["not_covering_eid"] = ev.absence(
                    "forecast_geo", consulted, families=",".join(data["not_covering"]), geo=iso3
                )
            keys = covering
        data["family_terms"] = [
            (
                fams[k],
                ev_term(ev, registry="registry/forecasts (question families)", term_id=k, kind="forecast_family",
                        labels=labels[k], query=a["text"]),
            )
            for k in keys[:MAX_FORECAST_FAMILIES]
        ]  # fmt: skip
        models = port.models()
        shown: list[tuple[Mapping[str, Any], Mapping[str, Any], str]] = []
        for k in keys[:MAX_FORECAST_FAMILIES]:
            qs = list(port.questions(k, iso3, limit=200))
            for summ in _pick_questions(qs):
                detail = port.question(str(summ["question_id"]))
                if detail is not None:
                    shown.append((fams[k], detail, ev_forecast(ev, detail, models)))
        data["models"] = models
        if not shown:
            data["absence"] = ev.absence(
                "sealed_forecasts", consulted, families=",".join(keys) or "none", geo=iso3 or "any"
            )
            data["families"] = [fams[k] for k in keys[:MAX_FORECAST_FAMILIES]]
            return OpResult(op, "empty", data=data, consulted=consulted)
        data["questions"] = shown
        return OpResult(op, "ok", data=data, consulted=consulted)

    # -- decision scenarios
    def _decision(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        from reva.nlqa.executor import OpResult

        a = op.args
        port = self._need("decisions", "the decision engine")
        consulted = [port.name, "registry/predict/reference_decisions.yaml"]
        refs = {str(d["id"]): d for d in port.references()}
        instr = {str(i["id"]): i for i in port.instruments()}
        labels = {k: {"fr": str(d.get("label_fr") or ""), "en": str(d.get("label_en") or "")} for k, d in refs.items()}
        extra = {
            k: " ".join(str((instr.get(str(d.get("instrument"))) or {}).get(x) or "") for x in ("label_fr", "label_en"))
            for k, d in refs.items()
        }
        ids = match_terms(a["text"], labels, extra_words=extra)
        data: dict[str, Any] = {"text": a["text"], "references": list(refs.values())}
        if len(ids) != 1:
            data["candidates"] = [refs[i] for i in ids]
            data["absence"] = ev.absence(
                "reference_decision", consulted, query=a["text"], candidates=",".join(ids) or "none"
            )
            return OpResult(op, "empty", data=data, consulted=consulted)
        ref = refs[ids[0]]
        decision = dict(ref)
        units = set((instr.get(str(ref.get("instrument"))) or {}).get("magnitude_units") or [])
        data["reference"] = ref
        data["term"] = ev_term(
            ev, registry="registry/predict/reference_decisions.yaml", term_id=ids[0], kind="reference_decision",
            labels=labels[ids[0]], query=a["text"],
        )  # fmt: skip
        if a.get("magnitude") is not None:
            asked = {"value": a["magnitude"], "unit": a["unit"]}
            ref_mag = ref.get("magnitude") or {}
            if a["unit"] not in units:
                data["magnitude_not_applied"] = asked
            elif not (_same(ref_mag.get("value"), a["magnitude"]) and ref_mag.get("unit") == a["unit"]):
                decision["magnitude"] = asked
                decision["id"] = f"{ref['id']}.ask"[:100]
                data["magnitude_changed"] = {"from": dict(ref_mag), "to": asked}
        out = port.run(decision, draws=DECISION_DRAWS, seed=0)
        scen = ev_scenario(ev, out)
        hops = {str(h.get("id")): (h, ev_hop(ev, h)) for h in out.get("hops") or []}
        data.update(decision=decision, scenario=out, scenario_eid=scen, hops=hops)
        return OpResult(op, "ok", data=data, consulted=consulted)

    # -- an indicator without country, over a period
    def _atlas_period(self, op: PlanOp, lookups: dict[str, Resolved], ev: Evidence) -> OpResult:
        from reva.nlqa.executor import OpResult, _by_source, _consulted, _dec

        a = op.args
        atlas = self._need("atlas", "the atlas")
        fn = getattr(atlas, "period_cells", None)
        if not callable(fn):
            raise CapabilityUnavailable("the atlas backend cannot read series without country", backend="atlas")
        concept = self.catalog.concepts[a["concept"]]  # type: ignore[attr-defined]
        order = list(concept.source_order)
        res = fn(concept.id, a["from_year"], a["to_year"])
        consulted = _consulted(res, order, atlas.name)
        data: dict[str, Any] = {"concept": concept, "from_year": a["from_year"], "to_year": a["to_year"]}
        groups = _by_source(res.cells, order)
        tables = getattr(self.b, "tables", None)
        if not groups and tables is not None:
            # V8ASK: the declared series may sit in a table registered after the atlas snapshot
            # (reva.nlqa.registered_tables); every cell names its table version and "outside_snapshot"
            series = [tuple(str(k).split(".", 2)) for k in order if str(k).count(".") >= 2]
            try:
                cells, pins = tables.period_cells(concept.id, series, a["from_year"], a["to_year"])
            except (RevaError, OSError, ValueError, LookupError) as e:  # the snapshot's absence stays the answer
                log.warning("registered table read failed: %s", type(e).__name__)
                cells, pins = [], []
            consulted = [*consulted, tables.name]
            groups = _by_source(cells, order)
            if groups:
                data["registered_tables"] = pins
        if not groups:
            data["absence"] = ev.absence(
                "atlas_period", consulted, concept=concept.id, from_year=a["from_year"], to_year=a["to_year"]
            )
            return OpResult(op, "empty", data=data, consulted=consulted)
        out = []
        for src, cells in groups[:3]:
            cells = sorted(cells, key=lambda c: (c.period_start, c.period_end))
            numeric = [c for c in cells if _dec(c.value) is not None]
            picks: dict[str, AtlasCell] = {"first": cells[0], "last": cells[-1]}
            if len(numeric) >= 3:
                picks["min"] = min(numeric, key=lambda c: (_dec(c.value), c.period_start))
                picks["max"] = max(numeric, key=lambda c: (_dec(c.value), c.period_start))
            out.append(
                (
                    src,
                    len(cells),
                    {k: (c, ev.cell(c)) for k, c in picks.items()},
                    sorted({c.year for c in cells if c.year is not None}),
                )
            )
        data["period_groups"] = out
        return OpResult(op, "ok", data=data, consulted=consulted)


def _label_bridges(b: Any, r: Any) -> list[tuple[Any, str, str]]:
    """Records carrying one of the chosen record's own labels (only after an exact or strict match)."""
    kg = getattr(b, "kg", None)
    fn = getattr(kg, "label_bridges", None)
    if not callable(fn) or r.entity is None or r.match not in STRICT_MATCHES:
        return []
    try:
        return list(fn(r.entity.qid, exclude=[x.qid for x in r.alternatives]))
    except CapabilityUnavailable:
        return []


def _same(a: Any, b: Any) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError):
        return False


def _kept_groups(
    view: FamilyView, reading: str
) -> list[tuple[str, Mapping[str, str], int, list[FamilyNeighbour], bool]]:
    out = []
    for g in view.groups:
        ns = [
            n
            for n in g.neighbours
            if not n.identity and (reading == "any" or (reading == "inverse") == bool(n.inverse))
        ]
        out.append((g.family, g.label, g.count, ns, g.count_is_lower_bound))
    return out


def _literals(view: FamilyView, families: Sequence[str]) -> list[tuple[Any, Mapping[str, Any] | None]]:
    hints = [h for f in families for h in LITERAL_HINTS.get(f, ())]
    if not hints:
        return []
    out = []
    seen: set[tuple[str, str]] = set()
    for s, rights in view.literals:
        words = " ".join([s.property, *[str(v) for v in s.property_labels.values()]]).lower()
        key = (s.property, s.object_label("en"))
        if any(h in words for h in hints) and key not in seen:
            seen.add(key)
            out.append((s, rights))
    return out[:12]


#: Matches the V6 domain operations accept: a family answer about an entity found only by the beginning of a
#: label ("wolf" → "Wolfiporia cocos") or a fuzzy match would be an answer about another thing.
STRICT_MATCHES = frozenset({"label_exact", "alias_exact", "label_words", "crosswalk_label"}) | STRICT_BRIDGE_MATCHES


def _approximate(op: Any, r: Any, ev: Any, consulted: list[str]) -> OpResult | None:
    from reva.nlqa.executor import OpResult

    if r.match in STRICT_MATCHES or r.match is None:
        return None
    eid = ev.absence(
        "exact_entity",
        consulted,
        query=r.text,
        match=str(r.match),
        candidates=",".join([r.entity.qid] + [x.qid for x in r.alternatives[:3]]),
    )
    return OpResult(op, "empty", data={"approximate": r, "absence": eid}, consulted=consulted)


def _pick_questions(qs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The nearest pending question(s) (earliest reference period not yet resolved) and the latest resolved one."""
    pending = sorted((q for q in qs if q.get("status") == "pending"), key=lambda q: str(q.get("reference_period")))
    resolved = sorted((q for q in qs if q.get("status") == "resolved"), key=lambda q: str(q.get("reference_period")))
    out = pending[:1] + resolved[-1:]
    if not out:
        out = sorted(qs, key=lambda q: str(q.get("reference_period")))[-1:]
    return out[:MAX_FORECAST_QUESTIONS]
