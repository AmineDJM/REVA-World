"""Answer bundle of the V8 « Demander » page: the NLQA answer (sentences + evidence, unchanged) plus what the page
needs to show it *answer first, then evidence*:

* ``headline`` — the short answer: one exact number (decimal string, unit, period, place, honesty kind) or one
  sentence, or « REVA ne sait pas » with what is missing;
* ``viz`` — the evidence view chosen from the shape of the evidence: ``timeseries`` (one series over time →
  line), ``bars`` (one value per item of a cross-section, same unit → ranked bars), ``forecast`` (sealed forecasts:
  point + 5–95 % band per model), ``graph`` (relations of one entity), ``path`` (causal claims / chain hops),
  ``estimates`` (attributed effect estimates, one by one), ``scenario`` (decision engine output) or ``null``;
* ``how`` — « Comment c'est calculé »: planner, operations, tables/series read, periods, as-of (atlas snapshot,
  registered table versions, registry seals, KG retrieval dates), latency;
* ``sources`` — one chip per source with its licence and flags (non-commercial, restricted, community-edited);
* ``targets`` — deep-link targets (Fiche / Monde / Comparer / Liens / Décision / Scores), built by the UI with
  ``v8Links``; ``followups`` — next questions (rules over the plan, never generated text); ``pin`` — what
  « épingler » stores (references, never values).

Nothing here creates a value: every number shown is an evidence item's exact decimal string; a time series is
re-read from the same atlas (or registered table) the answer cited, each point keeping its source.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from reva.v8.ask.golden import classify, missing_text

MAX_POINTS = 600
MAX_BARS = 30
MAX_GRAPH = 14
NC_RE = re.compile(r"non[- ]?commercial|\bNC\b|CC[- ]BY[- ]NC", re.I)
COMMUNITY_SOURCES = ("wikidata", "qlever.wikidata", "kg")

SUBSTANTIVE = ("fact", "causal", "forecast", "scenario", "estimate", "path", "link", "chain", "rank", "series")


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _ev(ans: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in ans.get("evidence") or []}


def _ops(ans: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list((ans.get("plan") or {}).get("operations") or [])


def _is_projection(cell: Mapping[str, Any]) -> bool:
    f = cell.get("flags") or {}
    return bool(f.get("projection") or f.get("is_projection") or f.get("projection_kind") or f.get("estimate_kind"))


def _rights_text(c: Mapping[str, Any]) -> str | None:
    r = c.get("rights")
    if isinstance(r, Mapping):
        return str(r.get("licence") or r.get("license") or "") or None
    return None


# --------------------------------------------------------------------------- sources
def sources(ans: Mapping[str, Any]) -> list[dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for e in ans.get("evidence") or []:
        if e.get("kind") in ("absence", "entity_identification", "registry_term"):
            continue
        c = e.get("citation") or {}
        src = str(c.get("source") or e.get("kind"))
        if e.get("kind") == "atlas_cell" and c.get("dataset"):
            src = f"{c.get('source')}/{c.get('dataset')}"
        row = by.setdefault(
            src,
            {
                "source": src,
                "licence": None,
                "n": 0,
                "kinds": set(),
                "non_commercial": False,
                "restricted": False,
                "community": False,
                "tiers": set(),
                "outside_snapshot": False,
            },
        )
        row["n"] += 1
        row["kinds"].add(e["kind"])
        lic = _rights_text(c)
        if lic and not row["licence"]:
            row["licence"] = lic
        raw_rights = c.get("rights")
        rights: Mapping[str, Any] = raw_rights if isinstance(raw_rights, Mapping) else {}
        row["restricted"] = row["restricted"] or bool(rights.get("restricted"))
        row["non_commercial"] = row["non_commercial"] or bool(lic and NC_RE.search(lic))
        row["community"] = row["community"] or c.get("status") == "community_edited" or "wikidata" in src
        if c.get("tier"):
            row["tiers"].add(str(c["tier"]))
        row["outside_snapshot"] = row["outside_snapshot"] or bool(c.get("outside_snapshot"))
    out = []
    for r in sorted(by.values(), key=lambda r: (-r["n"], r["source"])):
        r["kinds"] = sorted(r["kinds"])
        r["tiers"] = sorted(r["tiers"])
        out.append(r)
    return out


# --------------------------------------------------------------------------- headline
def headline(ans: Mapping[str, Any], status: str, missing: Sequence[str]) -> dict[str, Any]:
    ev = _ev(ans)
    sentences = ans.get("sentences") or []
    if status == "unknown":
        first_missing = next((s for s in sentences if s.get("kind") in ("missing", "unavailable", "unsupported")), None)
        return {
            "kind": "unknown",
            "text": "REVA ne sait pas" if ans.get("language", "fr") == "fr" else "REVA does not know",
            "detail": first_missing["text"] if first_missing else None,
            "missing": list(missing),
            "citations": first_missing["citations"] if first_missing else [],
        }
    direct = [s for s in sentences if s.get("kind") in SUBSTANTIVE]
    if status == "partial" or not direct:
        rel = next((s for s in sentences if s.get("kind") in ("missing",)), None)
        return {
            "kind": "partial",
            "text": rel["text"] if rel else "Réponse partielle",
            "detail": "Seules des données voisines, non équivalentes, sont chargées.",
            "missing": list(missing),
            "citations": rel["citations"] if rel else [],
        }
    s0 = direct[0]
    cited = [ev[c] for c in s0.get("citations") or [] if c in ev]
    cells = [c for c in cited if c.get("kind") == "atlas_cell" and c.get("value") is not None]
    if s0["kind"] == "fact" and cells:
        if len(cells) > 1:  # a period summary: the latest published value leads
            cells = sorted(cells, key=lambda c: str(c.get("period_start")))[-1:]
        c = cells[0]
        same_kind = [s for s in direct if s["kind"] == "fact"]
        return {
            "kind": "number",
            "value": c.get("value"),
            "unit": c.get("unit"),
            "period": _period_label(c),
            "iso3": c.get("iso3") or None,
            "concept": c.get("concept_id"),
            "honesty": "projection" if _is_projection(c) else "observed",
            "text": s0["text"],
            "more": max(0, len(same_kind) - 1),
            "citations": [c["id"]],
        }
    fc = [c for c in cited if c.get("kind") == "sealed_forecast"]
    if s0["kind"] == "forecast" and fc:
        q = fc[0]
        issued = [f for f in q.get("forecasts") or [] if f.get("status") == "issued" and f.get("point") is not None]
        f = issued[0] if issued else None
        qs = (f or {}).get("quantiles") or {}
        return {
            "kind": "number",
            "value": f.get("point") if f else None,
            "unit": q.get("unit"),
            "period": q.get("reference_period"),
            "iso3": q.get("geo"),
            "honesty": "forecast",
            "status": q.get("status"),
            "model": f.get("model") if f else None,
            "band": [qs.get("0.05"), qs.get("0.95")] if qs.get("0.05") is not None else None,
            "text": s0["text"],
            "more": max(0, len(issued) - 1),
            "citations": [q["id"]],
        }
    kinds = {
        "causal": "claim",
        "scenario": "scenario",
        "estimate": "estimate",
        "path": "path",
        "forecast": "forecast",
    }
    count = None
    m = re.search(r"\((\d+) montrée\(s\) sur (\d+)\)|(\d+) trouvée\(s\)|(\d+) found|shown of (\d+)", s0["text"])
    if m:
        count = next(int(g) for g in (m.group(2), m.group(3), m.group(4), m.group(5)) if g)
    return {
        "kind": "sentence",
        "text": s0["text"],
        "honesty": kinds.get(s0["kind"], "community" if _community(cited) else "observed"),
        "count": count,
        "citations": s0.get("citations") or [],
    }


def _community(items: Sequence[Mapping[str, Any]]) -> bool:
    return any("wikidata" in str((i.get("citation") or {}).get("source") or "") for i in items)


def _period_label(c: Mapping[str, Any]) -> str:
    ps, pe = str(c.get("period_start") or ""), str(c.get("period_end") or "")
    if len(ps) >= 10 and len(pe) >= 10:
        if ps[5:] == "01-01" and pe[5:] == "01-01" and int(pe[:4]) - int(ps[:4]) == 1:
            return ps[:4]
        if ps[8:] == "01" and pe[8:] == "01":
            return ps[:7]
        return ps[:10]
    return ps


# --------------------------------------------------------------------------- viz
def viz(ans: Mapping[str, Any], series_reader: Any = None) -> dict[str, Any] | None:
    ev = _ev(ans)
    kinds = Counter(e["kind"] for e in ev.values())
    ops = [o["op"] for o in _ops(ans)]
    cells = [e for e in ev.values() if e["kind"] == "atlas_cell" and e.get("value") is not None]
    if kinds.get("sealed_forecast"):
        return _forecast_viz(ev)
    if "decision" in ops and kinds.get("decision_scenario"):
        return _scenario_viz(ans, ev)
    if kinds.get("effect_estimate"):
        return _estimates_viz(ans, ev)
    if kinds.get("causal_claim"):
        return _path_viz(ev)
    if cells:
        ts = series_reader(ans, cells) if series_reader is not None else None
        if isinstance(ts, dict):
            return ts
        by_series = Counter((e["citation"].get("source"), e["citation"].get("dataset"), e["citation"].get("indicator"),
                             e.get("iso3")) for e in cells)  # fmt: skip
        if len(by_series) == 1 and len(cells) >= 3:
            return _timeseries_from_cells(cells)
        if len(cells) >= 2:
            return _bars_viz(cells)
    if kinds.get("kg_statement"):
        return _graph_viz(ans, ev)
    return None


def _point(e: Mapping[str, Any]) -> dict[str, Any]:
    c = e.get("citation") or {}
    return {
        "t": str(e.get("period_start") or "")[:10],
        "t_end": str(e.get("period_end") or "")[:10],
        "v": e.get("value"),
        "projection": _is_projection(e),
        "eid": e.get("id"),
        "source": f"{c.get('source')}/{c.get('dataset')}",
    }


def _timeseries_from_cells(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    c0 = cells[0]
    pts = sorted((_point(e) for e in cells), key=lambda p: p["t"])
    return {
        "type": "timeseries",
        "series": [
            {
                "label": c0.get("concept_id"),
                "concept": c0.get("concept_id"),
                "unit": c0.get("unit"),
                "iso3": c0.get("iso3") or None,
                "source": f"{c0['citation'].get('source')}/{c0['citation'].get('dataset')}",
                "indicator": c0["citation"].get("indicator"),
                "licence": _rights_text(c0["citation"]),
                "points": pts[:MAX_POINTS],
            }
        ],
        "note": "valeurs citées par la réponse",
    }


def _bars_viz(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    units = {c.get("unit") for c in cells}
    if len(units) != 1:
        return {
            "type": "table",
            "rows": [_bar_row(c) for c in cells][:MAX_BARS],
            "note": "unités différentes : pas de barres",
        }
    rows = [_bar_row(c) for c in cells if _dec(c.get("value")) is not None]
    rows.sort(key=lambda r: _dec(r["value"]) or Decimal(0), reverse=True)
    return {"type": "bars", "unit": next(iter(units)), "rows": rows[:MAX_BARS]}


def _bar_row(c: Mapping[str, Any]) -> dict[str, Any]:
    cit = c.get("citation") or {}
    return {
        "label": c.get("concept_id") or cit.get("indicator"),
        "concept": c.get("concept_id"),
        "iso3": c.get("iso3") or None,
        "value": c.get("value"),
        "unit": c.get("unit"),
        "period": _period_label(c),
        "projection": _is_projection(c),
        "eid": c.get("id"),
        "source": f"{cit.get('source')}/{cit.get('dataset')}",
    }


def _forecast_viz(ev: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    qs = []
    for e in ev.values():
        if e["kind"] != "sealed_forecast":
            continue
        models = []
        for f in e.get("forecasts") or []:
            q = f.get("quantiles") or {}
            models.append(
                {
                    "model": f.get("model"),
                    "method": f.get("method"),
                    "status": f.get("status"),
                    "reason": f.get("reason"),
                    "point": f.get("point"),
                    "lo": q.get("0.05"),
                    "hi": q.get("0.95"),
                    "score": f.get("score"),
                }
            )
        qs.append(
            {
                "eid": e["id"],
                "question_id": e.get("question_id"),
                "family": e.get("family"),
                "geo": e.get("geo"),
                "period": e.get("reference_period"),
                "unit": e.get("unit"),
                "status": e.get("status"),
                "outcome": e.get("outcome"),
                "issued_at": (e.get("citation") or {}).get("issued_at"),
                "models": models,
            }
        )
    return {"type": "forecast", "questions": qs[:6]}


def _scenario_viz(ans: Mapping[str, Any], ev: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    sc = next(e for e in ev.values() if e["kind"] == "decision_scenario")
    rows = [s["text"] for s in ans.get("sentences") or [] if s.get("kind") == "scenario"][1:]
    hops = [
        {
            "from": (e.get("hop") or {}).get("from"),
            "to": (e.get("hop") or {}).get("to"),
            "label": (e.get("hop") or {}).get("label_fr"),
            "estimate_key": e.get("estimate_key"),
            "source": (e.get("citation") or {}).get("source"),
            "eid": e["id"],
        }
        for e in ev.values()
        if e["kind"] == "decision_hop"
    ]
    return {
        "type": "scenario",
        "decision": (sc.get("decision") or {}).get("id"),
        "label": sc.get("label"),
        "validation": (sc.get("validation") or {}).get("status"),
        "rows": rows,
        "hops": hops[:20],
    }


def _estimates_viz(ans: Mapping[str, Any], ev: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    direct = {c for s in ans.get("sentences") or [] if s.get("kind") == "estimate" for c in s.get("citations") or []}
    rows = []
    for e in ev.values():
        if e["kind"] != "effect_estimate":
            continue
        cit = e.get("citation") or {}
        ref = cit.get("reference") or {}
        rows.append(
            {
                "eid": e["id"],
                "related": e["id"] not in direct,
                "cause": (e.get("cause") or {}).get("label_fr") or (e.get("cause") or {}).get("id"),
                "effect": (e.get("effect") or {}).get("label_fr") or (e.get("effect") or {}).get("id"),
                "point": e.get("as_printed") or (None if e.get("point") is None else str(e.get("point"))),
                "unit": e.get("unit"),
                "measure": e.get("measure"),
                "shock": e.get("shock"),
                "horizon": e.get("horizon"),
                "method": e.get("method"),
                "reference": ref.get("short") if isinstance(ref, Mapping) else str(ref or ""),
                "locator": cit.get("locator"),
            }
        )
    return {"type": "estimates", "rows": rows, "note": "estimations listées une par une, jamais moyennées"}


def _path_viz(ev: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    hops = []
    for e in ev.values():
        if e["kind"] != "causal_claim":
            continue
        cit = e.get("citation") or {}
        hops.append(
            {
                "eid": e["id"],
                "from": {"id": (e.get("cause") or {}).get("id"), "label": _lab(e.get("cause"))},
                "to": {"id": (e.get("effect") or {}).get("id"), "label": _lab(e.get("effect"))},
                "relation": e.get("relation"),
                "stance": e.get("stance"),
                "grade": e.get("grade"),
                "tier": cit.get("tier"),
                "source": cit.get("source"),
            }
        )
    return {
        "type": "path",
        "hops": hops[:MAX_GRAPH],
        "note": "affirmations attribuées à leurs sources ; REVA n'établit aucune causalité",
    }


def _lab(x: Any) -> str:
    if not isinstance(x, Mapping):
        return ""
    labels = x.get("labels") or {}
    return str(labels.get("fr") or labels.get("en") or x.get("label") or x.get("id") or "")


def _graph_viz(ans: Mapping[str, Any], ev: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    edges = []
    nodes: dict[str, dict[str, Any]] = {}
    for e in ev.values():
        if e["kind"] != "kg_statement" or not (e.get("object") or {}).get("qid"):
            continue
        s, o = e.get("subject") or {}, e.get("object") or {}
        for n in (s, o):
            nodes.setdefault(n["qid"], {"id": n["qid"], "label": _lab(n)})
        fam = e.get("family") or {}
        reading = (fam.get("reading") or {}).get("fr") or ((e.get("property") or {}).get("labels") or {}).get("fr")
        edges.append(
            {
                "from": s["qid"],
                "to": o["qid"],
                "label": reading or (e.get("property") or {}).get("id"),
                "inverse": bool(fam.get("inverse")),
                "eid": e["id"],
                "source": (e.get("citation") or {}).get("source"),
            }
        )
    if not edges:
        return None
    deg = Counter([x["from"] for x in edges] + [x["to"] for x in edges])
    center = deg.most_common(1)[0][0]
    return {
        "type": "graph",
        "center": nodes[center],
        "nodes": list(nodes.values())[: MAX_GRAPH + 1],
        "edges": edges[:MAX_GRAPH],
        "total": len(edges),
        "note": "relations publiées par les sources ; une relation n'est pas une cause",
    }


# --------------------------------------------------------------------------- how it was computed
def how(ans: Mapping[str, Any]) -> dict[str, Any]:
    ev = _ev(ans)
    tables: Counter[str] = Counter()
    periods: list[str] = []
    snapshots: set[str] = set()
    registered: dict[str, dict[str, Any]] = {}
    seals: set[str] = set()
    retrieved: list[str] = []
    for e in ev.values():
        c = e.get("citation") or {}
        if e["kind"] == "atlas_cell":
            tables[f"{c.get('source')}.{c.get('dataset')} · {c.get('indicator')}"] += 1
            periods.append(str(e.get("period_start") or "")[:10])
            if c.get("atlas_snapshot_sha256"):
                snapshots.add(str(c["atlas_snapshot_sha256"]))
            if c.get("outside_snapshot"):
                registered[str(c.get("dataset_key"))] = {
                    "dataset_key": c.get("dataset_key"),
                    "artifact_sha256": c.get("table_artifact_sha256"),
                    "registered_at": c.get("dataset_registered_at"),
                }
        elif e["kind"] == "sealed_forecast":
            tables[f"registre de prévisions · {e.get('family')}"] += 1
            if c.get("issued_at"):
                seals.add(str(c["issued_at"]))
        elif e["kind"] in ("kg_statement", "causal_claim"):
            tables[f"graphe · {c.get('source')}"] += 1
            if c.get("retrieved_at"):
                retrieved.append(str(c["retrieved_at"]))
        elif e["kind"] == "effect_estimate":
            tables[f"couche d'effets · {c.get('source')}"] += 1
        elif e["kind"] in ("decision_scenario", "decision_hop"):
            tables[f"moteur de décision · {c.get('source')}"] += 1
    ps = sorted(p for p in periods if p)
    return {
        "planner": ans.get("planner"),
        "prompt_version": ans.get("prompt_version"),
        "restatement": ans.get("restatement"),
        "operations": [
            {"op": o.get("op"), "args": {k: v for k, v in o.items() if k not in ("id", "op") and v is not None}}
            for o in _ops(ans)
        ],
        "rejected": list((ans.get("plan") or {}).get("rejected") or []),
        "tables": [{"table": k, "n": n} for k, n in tables.most_common(20)],
        "periods": {"first": ps[0], "last": ps[-1]} if ps else None,
        "as_of": {
            "atlas_snapshots": sorted(snapshots),
            "registered_tables": list(registered.values()),
            "forecast_seals": sorted(seals),
            "kg_retrieved": {"first": min(retrieved), "last": max(retrieved)} if retrieved else None,
        },
        "evidence_count": len(ev),
        "latency_ms": ans.get("latency_ms"),
        "warnings": [w for w in ans.get("warnings") or [] if not w.startswith("llm_not_enabled")],
    }


# --------------------------------------------------------------------------- targets, follow-ups, pin
def targets(ans: Mapping[str, Any]) -> list[dict[str, Any]]:
    ev = _ev(ans)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(t: dict[str, Any]) -> None:
        k = (t["kind"], str(t.get("id") or t.get("series") or t.get("ref") or t.get("track") or t.get("from")))
        if k not in seen and len(out) < 10:
            seen.add(k)
            out.append(t)

    for e in ev.values():
        k = e["kind"]
        c = e.get("citation") or {}
        if k == "entity_identification" and e.get("qid"):
            add({"kind": "fiche", "entity_kind": e.get("entity_kind") or "entity", "id": e["qid"],
                 "label": _lab({"labels": e.get("labels"), "id": e["qid"]})})  # fmt: skip
        elif k == "atlas_cell" and not c.get("outside_snapshot"):
            series = f"{c.get('source')}.{c.get('dataset')}.{c.get('indicator')}"
            table = f"{c.get('source')}.{c.get('dataset')}"
            label = e.get("concept_id") or c.get("indicator")
            add({"kind": "monde", "table": table, "indicator": c.get("indicator"), "series": series,
                 "iso3": e.get("iso3") or None, "period": str(e.get("period_start") or "")[:4], "label": label})  # fmt: skip
            if e.get("iso3"):
                add({"kind": "comparer", "series": series, "iso3": e["iso3"], "label": label})
                add({"kind": "fiche", "entity_kind": "country", "id": e["iso3"], "label": e["iso3"]})
        elif k == "sealed_forecast":
            add({"kind": "scores", "track": e.get("family"), "label": e.get("family")})
        elif k == "decision_scenario":
            ref = (e.get("decision") or {}).get("id")
            if ref:
                add({"kind": "decision", "ref": str(ref).removesuffix(".ask"), "label": ref})
        elif k == "causal_claim":
            ca, ef = (e.get("cause") or {}).get("id"), (e.get("effect") or {}).get("id")
            if ca and ef:
                add(
                    {
                        "kind": "liens",
                        "from": ca,
                        "to": ef,
                        "label": f"{_lab(e.get('cause'))} → {_lab(e.get('effect'))}",
                    }
                )
    return out


def followups(ans: Mapping[str, Any], status: str) -> list[dict[str, Any]]:
    fr = ans.get("language", "fr") == "fr"
    ev = _ev(ans)
    out: list[dict[str, Any]] = []
    ops = _ops(ans)
    for o in ops:
        if o.get("op") == "atlas_value" and o.get("concept") and len(ops) == 1:
            cell = next((e for e in ev.values() if e["kind"] == "atlas_cell"), None)
            lab = (cell or {}).get("concept_id") or o["concept"]
            out.append({"label": "Classement des pays sur cet indicateur", "brainstorm": None,
                        "q": f"Classement des pays : {lab}" if fr else f"Country ranking: {lab}"})  # fmt: skip
        if o.get("op") in ("forecast",) and o.get("text"):
            out.append({"label": f"Brainstormer : prévoir « {o['text']} »", "brainstorm": o["text"]})
        if o.get("op") == "effects" and o.get("effect_text"):
            out.append({"label": f"Brainstormer : prévoir « {o['effect_text']} »", "brainstorm": o["effect_text"]})
        if o.get("op") == "atlas_period" and o.get("concept"):
            out.append({"label": "Prévisions scellées sur cette série", "q": f"Prévision {o['concept']}"})
    for e in ev.values():
        if e["kind"] == "sealed_forecast" and e.get("family"):
            out.append(
                {
                    "label": "Ces prévisions battent-elles les prévisionnistes officiels ?",
                    "target": {"kind": "scores", "track": e["family"]},
                }
            )
            break
    if status != "answered":
        out.append({"label": "Brainstormer : que manque-t-il pour répondre ?", "brainstorm": ans.get("question") or ""})
    seen, uniq = set(), []
    for f in out:
        k = f["label"]
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq[:6]


def pin(ans: Mapping[str, Any], question: str, hl: Mapping[str, Any]) -> dict[str, Any]:
    """What « épingler » stores: a note with the question and a link to re-ask it (values are re-read live)."""
    text = hl.get("text") or ""
    md = f"**{question}**\n\n{text}\n\n[Redemander]({'/v8/demander?' + _q(question)})"
    p: dict[str, Any] = {"kind": "note", "label": question[:400], "markdown": md[:20000]}
    if hl.get("kind") == "number" and hl.get("concept") and hl.get("iso3") and hl.get("honesty") != "forecast":
        p["series"] = {"kind": "series", "concept": hl["concept"], "geo": hl["iso3"], "label": question[:400],
                       "unit": hl.get("unit") or None}  # fmt: skip
    return p


def _q(q: str) -> str:
    from urllib.parse import urlencode

    return urlencode({"q": q})


def build(ans: Mapping[str, Any], question: str, *, series_reader: Any = None) -> dict[str, Any]:
    c = classify(ans)
    miss = missing_text(c["causes"])
    hl = headline(ans, c["status"], miss)
    a = dict(ans)
    a["question"] = question
    return {
        "question": question,
        "language": ans.get("language"),
        "status": c["status"],
        "headline": hl,
        "missing": miss,
        "viz": viz(ans, series_reader),
        "how": how(ans),
        "sources": sources(ans),
        "targets": targets(ans),
        "followups": followups(a, c["status"]),
        "pin": pin(ans, question, hl),
        "sentences": ans.get("sentences") or [],
        "evidence": ans.get("evidence") or [],
        "answer_text": ans.get("answer_text"),
        "warnings": ans.get("warnings") or [],
        "latency_ms": ans.get("latency_ms"),
    }
