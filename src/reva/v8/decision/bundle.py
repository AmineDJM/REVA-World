"""Pure transforms of a decision-engine result into the V8 studio's page bundle (no database, no I/O).

The engine (``reva.predict.engine``) already carries every honesty gate; this module only *re-shapes* its JSON:

* ``headline`` — one row per quantified variable with the value at its last computed period (or its undated
  interval), its flags (distribution incomplète, extrapolation, estimation ancienne) and the sources on its path;
* ``graph`` — decision → direct changes → variables, each estimate edge with the estimate as published (size,
  unit, shock, horizon, published uncertainty, source, method); stops (no estimate, simulator inputs missing,
  unresolved ties) are nodes of their own, so the graph shows where and why the chain ends;
* ``evidence`` — per estimate key: citation, locator, verbatim quote, licence, method, applicability;
* ``conflicts`` — estimates of one edge side by side (the chosen one and the alternatives, or an unresolved tie),
  never averaged;
* ``diagnosis`` — « pourquoi pas de résultat » : the exact link or data that is missing.

No number is created here: every value shown comes from the engine result or from the estimate as published.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

Z90 = 1.6448536269514722  # two-sided 90 % of a standard normal (the engine's p05-p95)

# ------------------------------------------------------------------------------------------- labels
UNIT_FR: dict[str, str] = {
    "percent": "%",
    "pp": "pt de %",
    "percentage_points": "pt de %",
    "log_points": "points log",
    "pp_gdp": "pt de PIB",
    "occurrence": "survenue",
    "std_dev": "écart-type",
    "pp_per_pp": "pt de % par pt de %",
    "percent_per_percent": "% par %",
    "elasticity": "élasticité",
}

MEASURE_FR: dict[str, str] = {
    "response": "réponse",
    "elasticity": "élasticité",
    "semi_elasticity": "semi-élasticité",
    "pass_through": "transmission",
    "slope": "pente",
    "multiplier": "multiplicateur",
    "sign_only": "signe seulement",
}

GEO_FR: dict[str, str] = {"WLD": "Monde"}

HORIZON_FR: dict[str, str] = {
    "impact": "impact",
    "at_horizon": "à l'horizon",
    "cumulative": "cumulé",
    "peak": "pic",
    "average": "moyenne",
    "long_run": "long terme",
    "unspecified": "horizon non précisé",
}
HORIZON_UNIT_FR: dict[str, str] = {"year": "an(s)", "quarter": "trim.", "month": "mois", "week": "sem.", "day": "j"}

NOTE_FR: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^annual lag profile read on quarters.*"),
        "profil annuel lu par trimestre : la réponse de l'année h s'applique à ses quatre trimestres",
    ),
    (
        re.compile(r"^monthly lag profile read on quarters.*"),
        "profil mensuel lu par trimestre : moyenne des mois publiés de chaque trimestre",
    ),
    (
        re.compile(r"^single published horizon.*"),
        "un seul horizon publié : les autres horizons ne sont pas calculés (jamais interpolés)",
    ),
    (
        re.compile(r"^horizon of (\d+) months read as quarter offset (\d+)"),
        r"horizon de \1 mois lu comme le trimestre +\2",
    ),
    (
        re.compile(r"^sd derived from the published interval assuming symmetry.*"),
        "écart-type déduit de l'intervalle publié (symétrie supposée, approximation normale)",
    ),
    (
        re.compile(r"^uncertainty from a published range only.*"),
        "incertitude tirée d'une simple fourchette publiée (heuristique, étiquetée)",
    ),
    (
        re.compile(r"^sd = dispersion across the published estimates.*"),
        "écart-type = dispersion entre estimations publiées (hétérogénéité, pas erreur d'échantillonnage)",
    ),
    (
        re.compile(r"^estimated on data up to (\d{4}); the decision starts in (\d{4}) \(more than (\d+) years.*"),
        r"estimée sur des données jusqu'en \1 ; la décision commence en \2 (plus de \3 ans d'écart)",
    ),
    (
        re.compile(r"^estimated on data up to (\d{4}); applied to (\d{4}) \(out-of-sample period\)"),
        r"estimée sur des données jusqu'en \1 ; appliquée à \2 (hors période d'estimation)",
    ),
    (
        re.compile(r"^pooled panel \((\d+) countries; (\w{3}) is in the listed sample\).*"),
        r"panel groupé (\1 pays, \2 dans l'échantillon) : une moyenne du panel, pas une estimation propre à \2",
    ),
    (
        re.compile(r"^pooled panel \('(.+)', verbatim\); (\w{3}) membership not checked"),
        r"panel groupé (« \1 ») : appartenance de \2 non vérifiée",
    ),
    (
        re.compile(r"^estimate from a world-level scope applied to (\w{3}).*"),
        r"estimation de portée mondiale appliquée à \1 (moyenne transposée)",
    ),
    (re.compile(r"^the estimate states no geographic scope"), "l'estimation ne précise pas sa portée géographique"),
    (
        re.compile(r"^published response to a shock of (.+) scaled linearly to the decision's change.*"),
        r"réponse publiée à un choc de \1, mise à l'échelle linéairement (linéarité supposée)",
    ),
    (
        re.compile(
            r"^the change reaching this estimate \(([^)]+)\) is ([\d.]+) times the published shock \(([^)]+)\).*"
        ),
        r"le changement qui atteint cette estimation (\1) vaut \2 fois le choc publié (\3) : extrapolation linéaire hors du domaine estimé",
    ),
    (
        re.compile(r"^undated estimate applied to the decided change.*"),
        "estimation non datée appliquée au changement décidé (la durée de la décision n'est pas utilisée)",
    ),
    (
        re.compile(r"^undated estimate applied to the upstream value at (\S+) \(last available\)"),
        r"estimation non datée appliquée à la valeur amont de \1 (dernière disponible)",
    ),
    (
        re.compile(r"^undated upstream change treated as a step.*"),
        "changement amont non daté traité comme une marche au début de la décision",
    ),
    (
        re.compile(r"^approximation: log points ≈ percent.*"),
        "approximation : points log ≈ % (valable pour de petites variations)",
    ),
    (
        re.compile(r"^occurrence at the start quarter.*"),
        "survenue au trimestre de départ : les réponses publiées à une survenue sont lues telles quelles",
    ),
    (
        re.compile(r"^target level, not a change.*"),
        "niveau cible, pas une variation : seuls les modèles qui lisent un niveau l'utilisent",
    ),
]


def note_fr(note: str) -> str:
    for pat, rep in NOTE_FR:
        if pat.match(note):
            return pat.sub(rep, note)
    return note


def unit_fr(u: str | None) -> str:
    if not u:
        return ""
    if u in UNIT_FR:
        return UNIT_FR[u]
    if u.startswith("level:"):
        return u[6:]
    return u.replace("_", " ")


def geo_fr(g: str | None) -> str:
    return GEO_FR.get(g or "", g or "")


def split_node(nid: str) -> tuple[str, str, str]:
    """``kind:id@GEO`` → (kind, id, geo)."""
    ref, _, geo = nid.rpartition("@")
    if not ref:
        ref, geo = nid, ""
    kind, _, ident = ref.partition(":")
    return kind, ident, geo


def fmt_num(x: Any) -> str:
    """Compact French number (3 significant digits), for labels only (values travel as numbers)."""
    if x is None:
        return "?"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if v != v:
        return "?"
    s = f"{v:.3g}"
    if "e" in s:
        mant, ex = s.split("e")
        s = f"{mant}e{int(ex)}"
    sign = "+" if v > 0 else ""
    return (sign + s).replace(".", ",").replace("-", "−")


def short_source(ev: Mapping[str, Any] | None, estimate_key: str) -> str:
    """Short author-year label of an estimate's source (no number)."""
    if ev:
        cit = ev.get("citation_obj") or {}
        if isinstance(cit, Mapping) and cit.get("short"):
            return str(cit["short"])
        kind = ev.get("source_kind")
        if kind == "reva_run":
            run = ev.get("run") or {}
            rid = str(run.get("run_id") or ([*estimate_key.split(":"), "?"])[1])
            return f"REVA · calcul {rid[:14]}"
        if kind == "identity":
            ident = ev.get("identity") or {}
            return f"identité « {ident.get('name') or '?'} »"
    parts = estimate_key.split(":")
    if parts[0] == "pub" and len(parts) >= 3:
        toks = parts[2].split("_")
        year = toks[-1] if toks and re.fullmatch(r"\d{4}", toks[-1]) else ""
        names = [t for t in toks if not re.fullmatch(r"\d{4}", t)]
        who = names[0].capitalize() + (
            " et al." if len(names) > 2 else (f" & {names[1].capitalize()}" if len(names) == 2 else "")
        )
        return f"{who} {year}".strip()
    if parts[0] == "run" and len(parts) >= 2:
        return f"REVA · calcul {parts[1][:14]}"
    return estimate_key


# ------------------------------------------------------------------------------------------- evidence
def evidence_entry(estimate_key: str, prov: Mapping[str, Any] | None, est: Mapping[str, Any] | None) -> dict[str, Any]:
    """One estimate's evidence, merging the hop's provenance (engine) with the estimate JSON (effects layer)."""
    prov = dict(prov or {})
    est = dict(est or {})
    src = dict(est.get("source") or {})
    cit = src.get("citation") if isinstance(src.get("citation"), Mapping) else None
    rights = src.get("rights") if src else prov.get("rights")
    rights = dict(rights) if isinstance(rights, Mapping) else None
    unc = dict(est.get("uncertainty") or {})
    out: dict[str, Any] = {
        "estimate_key": estimate_key,
        "source_kind": src.get("kind") or prov.get("source_kind"),
        "source_key": src.get("source_key") or prov.get("source_key"),
        "citation": (cit or {}).get("text") if cit else prov.get("citation"),
        "citation_obj": dict(cit) if cit else None,
        "doi": (cit or {}).get("doi") if cit else prov.get("doi"),
        "url": (cit or {}).get("url") if cit else prov.get("url"),
        "locator": src.get("locator") or prov.get("locator"),
        "quote": src.get("quote") or prov.get("quote"),
        "transcription": src.get("transcription") or prov.get("transcription"),
        "raw_sha256": src.get("raw_sha256") or prov.get("raw_sha256"),
        "run": src.get("run") or prov.get("run"),
        "identity": src.get("identity") or prov.get("identity"),
        "rights": rights,
        "restricted": bool(rights and rights.get("restricted")),
        "non_commercial": bool(rights and rights.get("non_commercial")),
        "as_of": est.get("as_of") if est.get("as_of") is not None else prov.get("as_of"),
        "method": est.get("method") or prov.get("method"),
        "identification": est.get("identification") or prov.get("identification"),
        "wording_fr": est.get("wording_fr") or prov.get("wording_fr"),
        "applicability_note": est.get("applicability") or prov.get("applicability_note"),
        "source_caveats": est.get("source_caveats") or prov.get("source_caveats"),
        "curation_review": ((est.get("curation") or prov.get("curation") or {}) or {}).get("review_status"),
        "origin": est.get("origin") or prov.get("origin"),
        "cause": _side(est.get("cause")),
        "effect": _side(est.get("effect")),
        "measure": est.get("measure"),
        "unit": est.get("unit"),
        "point": est.get("point"),
        "as_printed": est.get("as_printed"),
        "shock": est.get("shock"),
        "horizon": est.get("horizon"),
        "irf": list(est.get("irf") or []),
        "scope": est.get("scope"),
        "uncertainty": {k: unc.get(k) for k in ("se", "ci_low", "ci_high", "ci_level", "interval_kind", "status")}
        if unc
        else None,
        "loaded": bool(est),
    }
    out["short"] = short_source(out, estimate_key)
    out["published_size_fr"] = published_size_fr(out)
    out["published_uncertainty_fr"] = published_uncertainty_fr(out)
    return out


def _side(s: Any) -> dict[str, Any] | None:
    if not isinstance(s, Mapping):
        return None
    return {k: s.get(k) for k in ("kind", "id", "label", "label_fr", "transform") if k in s}


def published_size_fr(ev: Mapping[str, Any]) -> str | None:
    """The estimate as published, in words (point, unit, shock, horizon); None when no point."""
    if ev.get("point") is None:
        return None
    measure = MEASURE_FR.get(str(ev.get("measure")), str(ev.get("measure") or ""))
    s = f"{measure} {ev.get('as_printed') or fmt_num(ev['point'])} {unit_fr(ev.get('unit'))}".strip()
    shock = ev.get("shock") or None
    if isinstance(shock, Mapping) and shock.get("size") is not None:
        s += f" pour un choc de {fmt_num(shock['size'])} {unit_fr(shock.get('unit'))}"
    hz = ev.get("horizon") or {}
    if isinstance(hz, Mapping) and hz.get("kind"):
        kind = HORIZON_FR.get(str(hz["kind"]), str(hz["kind"]))
        if hz.get("value") is not None:
            unit = HORIZON_UNIT_FR.get(str(hz.get("unit")), str(hz.get("unit") or ""))
            s += f" ({kind}, {fmt_num(hz['value']).lstrip('+')} {unit})"
        elif hz.get("kind") != "unspecified":
            s += f" ({kind})"
    return s


def published_uncertainty_fr(ev: Mapping[str, Any]) -> str:
    u = ev.get("uncertainty") or {}
    if u.get("se") is not None:
        return f"écart-type publié {fmt_num(u['se']).lstrip('+')}"
    if u.get("ci_low") is not None and u.get("ci_high") is not None:
        lvl = u.get("ci_level")
        lab = f"{round(float(lvl) * 100)} %" if lvl else "niveau non précisé"
        kind = {"credible": "intervalle crédible", "range": "fourchette"}.get(str(u.get("interval_kind")), "intervalle")
        return f"{kind} {lab} [{fmt_num(u['ci_low'])} ; {fmt_num(u['ci_high'])}]"
    if ev.get("loaded") is False:
        return "incertitude : estimation non relue (clé seule)"
    return "incertitude non publiée (scénario ponctuel)"


# ------------------------------------------------------------------------------------------- headline
def _flags(v: Mapping[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not v.get("uncertainty_complete", True):
        out.append(
            {
                "code": "incomplete",
                "label_fr": "distribution incomplète",
                "title_fr": "une estimation du chemin ne publie pas d'incertitude : l'éventail est trop étroit (scénario ponctuel)",
            }
        )
    notes = " ".join(v.get("notes") or [])
    if "linear extrapolation" in notes:
        out.append(
            {
                "code": "extrapolation",
                "label_fr": "extrapolation",
                "title_fr": "choc bien plus grand que le choc publié",
            }
        )
    if "more than 20 years" in notes or re.search(r"more than \d+ years", notes):
        out.append(
            {
                "code": "old",
                "label_fr": "estimation ancienne",
                "title_fr": "données d'estimation antérieures de plus de 20 ans",
            }
        )
    if "membership not checked" in notes:
        out.append(
            {
                "code": "group",
                "label_fr": "groupe non vérifié",
                "title_fr": "appartenance du pays au panel non vérifiée",
            }
        )
    if v.get("kind") == "plugin_output":
        out.append(
            {
                "code": "model",
                "label_fr": "modèle REVA",
                "title_fr": f"sortie du modèle {v.get('model')} sous hypothèses énoncées",
            }
        )
    return out


def headline_value(v: Mapping[str, Any]) -> dict[str, Any] | None:
    if v.get("frequency") == "undated":
        u = v.get("undated") or None
        if not u:
            return None
        return {
            "period": None,
            "period_label_fr": u.get("kind_label_fr") or u.get("kind") or "non daté",
            **{k: u.get(k) for k in ("p05", "p25", "p50", "p75", "p95", "mean", "degenerate")},
        }
    done = [p for p in v.get("periods") or [] if not p.get("missing_reason") and p.get("p50") is not None]
    if not done:
        return None
    p = done[-1]
    return {
        "period": p["period"],
        "period_label_fr": f"en {p['period']}",
        **{k: p.get(k) for k in ("p05", "p25", "p50", "p75", "p95", "mean", "degenerate")},
    }


def headline(result: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    hops = {h["id"]: h for h in result.get("hops") or []}
    order = {g: i for i, g in enumerate([*(result.get("decision") or {}).get("countries", []), "WLD"])}
    rows = []
    for v in result.get("variables") or []:
        keys = [hops[h]["estimate_key"] for h in v.get("path") or [] if h in hops and hops[h].get("kind") == "estimate"]
        models = [hops[h]["model"] for h in v.get("path") or [] if h in hops and hops[h].get("kind") == "plugin"]
        periods = v.get("periods") or []
        rows.append(
            {
                "id": v["id"],
                "label_fr": (v.get("variable") or {}).get("label_fr")
                or (v.get("variable") or {}).get("label_en")
                or (v.get("variable") or {}).get("id"),
                "geo": v.get("geo"),
                "geo_fr": geo_fr(v.get("geo")),
                "unit": v.get("unit"),
                "unit_fr": unit_fr(v.get("unit")),
                "kind": v.get("kind"),
                "frequency": v.get("frequency"),
                "depth": v.get("depth"),
                "value": headline_value(v),
                "n_periods": len(periods),
                "n_missing": sum(1 for p in periods if p.get("missing_reason")),
                "flags": _flags(v),
                "sources": [
                    {"estimate_key": k, "short": (evidence.get(k) or {}).get("short") or short_source(None, k)}
                    for k in keys
                ],
                "models": models,
                "notes_fr": [note_fr(n) for n in v.get("notes") or []],
            }
        )
    rows.sort(key=lambda r: (r["depth"] or 0, order.get(r["geo"], 99), r["label_fr"] or ""))
    return rows


# ------------------------------------------------------------------------------------------- graph
def node_label(nid: str, labels: Mapping[str, str]) -> str:
    kind, ident, geo = split_node(nid)
    base = labels.get(f"{kind}:{ident}") or ident
    return f"{base} · {geo_fr(geo)}" if geo else base


def _labels(result: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for ev in evidence.values():
        for side in (ev.get("cause"), ev.get("effect")):
            if side and side.get("kind") and side.get("id") and (side.get("label_fr") or side.get("label")):
                labels.setdefault(f"{side['kind']}:{side['id']}", str(side.get("label_fr") or side.get("label")))
    for v in result.get("variables") or []:
        var = v.get("variable") or {}
        lab = var.get("label_fr") or var.get("label_en")
        if lab:
            labels[f"{var.get('kind')}:{var.get('id')}"] = str(lab)
    return labels


def _edge_effect(h: Mapping[str, Any], ev: Mapping[str, Any]) -> dict[str, Any]:
    c = h.get("coefficient") or {}
    point, sd = c.get("point"), c.get("sd")
    return {
        "beta": point,
        "beta_sd": sd,
        "beta_ci90": [point - Z90 * sd, point + Z90 * sd] if point is not None and sd is not None else None,
        "uncertainty_basis": c.get("uncertainty_basis"),
        "in_unit": c.get("in_unit"),
        "out_unit": c.get("out_unit"),
        "beta_label_fr": f"β = {fmt_num(point)} {unit_fr(c.get('out_unit'))} par {unit_fr(c.get('in_unit'))}".strip(),
        "published_fr": ev.get("published_size_fr"),
        "published_uncertainty_fr": ev.get("published_uncertainty_fr"),
        "has_uncertainty": sd is not None,
    }


def graph(
    result: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]], *, labels: Mapping[str, str] | None = None
) -> dict[str, Any]:
    labels = dict(labels or _labels(result, evidence))
    d = result.get("decision") or {}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def node(nid: str, kind: str, **kw: Any) -> dict[str, Any]:
        if nid not in nodes:
            _, _, geo = split_node(nid) if "@" in nid else ("", "", kw.get("geo", ""))
            nodes[nid] = {
                "id": nid,
                "kind": kind,
                "label": kw.pop("label", None) or node_label(nid, labels),
                "geo": geo,
                **kw,
            }
        return nodes[nid]

    node("decision", "decision", label=d.get("label_fr") or "Décision", depth=-1, geo="")
    vars_by_id = {v["id"]: v for v in result.get("variables") or []}
    direct_ids: set[str] = set()
    for h in result.get("hops") or []:
        if h["kind"] == "decision":
            dc = h.get("direct_change") or {}
            direct_ids.add(h["to"])
            node(h["to"], "direct", depth=0)
            edges.append(
                {
                    "id": h["id"],
                    "from": "decision",
                    "to": h["to"],
                    "kind": "assumption",
                    "label": f"{fmt_num(dc.get('change'))} {unit_fr(dc.get('unit'))} (hypothèse)",
                    "basis_fr": dc.get("basis_fr"),
                }
            )
        elif h["kind"] == "estimate":
            ev = evidence.get(h["estimate_key"]) or {}
            v = vars_by_id.get(h["to"]) or {}
            hv = headline_value(v) if v else None
            node(h["to"], "variable", depth=v.get("depth"), value=hv, unit=v.get("unit"))
            edges.append(
                {
                    "id": h["id"],
                    "from": h["from"],
                    "to": h["to"],
                    "kind": "estimate",
                    "estimate_key": h["estimate_key"],
                    "source_short": ev.get("short") or short_source(None, h["estimate_key"]),
                    "method": (ev.get("method") or (h.get("source") or {}).get("method")),
                    "effect": _edge_effect(h, ev),
                    "applicability": h.get("applicability"),
                    "applicability_notes_fr": [note_fr(n) for n in (h.get("applicability") or {}).get("notes") or []],
                    "notes_fr": [note_fr(n) for n in h.get("notes") or []],
                    "time_profile": h.get("time_profile"),
                    "n_alternatives": len(h.get("alternatives") or []),
                    "restricted": bool(ev.get("restricted")),
                    "label": f"{ev.get('short') or short_source(None, h['estimate_key'])}",
                }
            )
        elif h["kind"] == "plugin":
            pid = f"plugin:{h['model']}@{split_node(h['from'])[2]}"
            node(
                pid,
                "plugin",
                label=h.get("label_fr") or h["model"],
                depth=None,
                model=h["model"],
                blocked=not h.get("to"),
            )
            edges.append(
                {
                    "id": h["id"],
                    "from": h["from"],
                    "to": pid,
                    "kind": "plugin",
                    "label": h["model"],
                    "assumption_fr": h.get("assumption_fr"),
                }
            )
            for out in h.get("to") or []:
                v = vars_by_id.get(out) or {}
                node(out, "variable", depth=v.get("depth"), value=headline_value(v) if v else None, unit=v.get("unit"))
                edges.append(
                    {
                        "id": f"{h['id']}>{out}",
                        "from": pid,
                        "to": out,
                        "kind": "model_output",
                        "label": "sortie du modèle",
                    }
                )

    # stops: where the chain ends, and why
    for i, g in enumerate(result.get("gaps") or []):
        code = g.get("code")
        nid = g.get("node")
        if code in ("no_effect_estimate_for_cause", "variable_not_in_effect_vocabulary") and nid:
            if nid in direct_ids or nid not in nodes:
                node(nid, "direct" if nid in direct_ids else "variable", depth=0)
                sid = f"stop:{nid}"
                stop = node(
                    sid, "stop", label="aucune estimation chiffrée", depth=None, geo=split_node(nid)[2], reasons=[]
                )
                stop["reasons"].append(gap_fr(g))
                if not any(e["id"] == f"gap>{sid}" for e in edges):
                    edges.append(
                        {"id": f"gap>{sid}", "from": nid, "to": sid, "kind": "missing", "label": "lien manquant"}
                    )
            else:
                nodes[nid]["chain_end"] = True
        elif code == "unresolved_tie" and nid:
            src = _tie_source(nid, g, evidence, nodes)
            node(
                nid,
                "tie",
                label=f"{node_label(nid, labels)} — égalité non résolue",
                depth=None,
                keys=g.get("estimate_keys"),
            )
            edges.append(
                {
                    "id": f"tie{i}",
                    "from": src,
                    "to": nid,
                    "kind": "tie",
                    "label": " vs ".join(fmt_num(p) for p in g.get("points") or []),
                    "estimate_keys": list(g.get("estimate_keys") or []),
                }
            )
        elif code in ("plugin_input_missing", "plugin_data_missing", "missing_input") and g.get("plugin"):
            model = str(g["plugin"])
            base = nid or next(
                (h["from"] for h in result.get("hops") or [] if h.get("kind") == "plugin" and h.get("model") == model),
                None,
            )
            if base is None:
                continue
            pid = f"plugin:{model}@{split_node(base)[2]}"
            if pid not in nodes:
                hit = [
                    p
                    for p in nodes
                    if p.startswith(f"plugin:{model.split('@')[0]}") and p.endswith(f"@{split_node(base)[2]}")
                ]
                pid = hit[0] if hit else pid
            p = node(pid, "plugin", label=model, depth=None, model=model, blocked=True)
            p["blocked"] = True
            p.setdefault("reasons", []).append(gap_fr(g))
            if not any(e["to"] == pid for e in edges):
                edges.append({"id": f"plug{i}", "from": base, "to": pid, "kind": "missing", "label": "modèle bloqué"})

    # sign-only estimates: dashed, no number
    for j, u in enumerate(result.get("unquantified_links") or []):
        if u.get("kind") != "sign_only_estimate" or not u.get("from"):
            continue
        to = u.get("to") or {}
        tid = f"{to.get('kind')}:{to.get('id')}@{split_node(u['from'])[2]}"
        node(tid, "unquantified", depth=None)
        edges.append(
            {
                "id": f"sign{j}",
                "from": u["from"],
                "to": tid,
                "kind": "sign_only",
                "label": f"signe {u.get('sign') or '?'} (non chiffré)",
                "estimate_key": u.get("estimate_key"),
            }
        )

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "geos": sorted({n["geo"] for n in nodes.values() if n["geo"]}),
    }


def _tie_source(
    nid: str, g: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]], nodes: Mapping[str, Any]
) -> str:
    geo = split_node(nid)[2]
    for k in g.get("estimate_keys") or []:
        cause = (evidence.get(k) or {}).get("cause") or {}
        cand = f"{cause.get('kind')}:{cause.get('id')}@{geo}"
        if cand in nodes:
            return cand
        cand_w = f"{cause.get('kind')}:{cause.get('id')}@WLD"
        if cand_w in nodes:
            return cand_w
    same_geo = [n for n, v in nodes.items() if v.get("kind") == "direct" and v.get("geo") == geo]
    return same_geo[0] if same_geo else "decision"


# ------------------------------------------------------------------------------------------- conflicts
def conflicts(result: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Estimates of one edge side by side: the chosen one and its alternatives, or an unresolved tie.

    Several hops can use the same estimates (one per country): identical groups are listed once with every
    edge they concern."""
    groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for h in result.get("hops") or []:
        if h.get("kind") != "estimate" or not h.get("alternatives"):
            continue
        keys = (h["estimate_key"], *[a["estimate_key"] for a in h["alternatives"]])
        g = groups.get(("alternatives", keys))
        if g is None:
            g = groups[("alternatives", keys)] = {
                "kind": "alternatives",
                "title_fr": "Estimations concurrentes du même lien — une seule retenue (règle déterministe), jamais moyennées",
                "edges": [],
                "nodes": [],
                "estimates": [
                    {
                        **_conflict_row(h["estimate_key"], evidence),
                        "chosen": True,
                        "applicability": (h.get("applicability") or {}).get("status"),
                        "reason_fr": "retenue",
                    }
                ]
                + [
                    {
                        **_conflict_row(a["estimate_key"], evidence),
                        "chosen": False,
                        "applicability": a.get("applicability"),
                        "reason_fr": a.get("reason_fr"),
                        "point_used": a.get("point"),
                    }
                    for a in h["alternatives"]
                ],
                "choice_rule": h.get("choice_rule"),
            }
        g["edges"].append(h["id"])
        g["nodes"].append(h["to"])
    for g in result.get("gaps") or []:
        if g.get("code") != "unresolved_tie":
            continue
        keys = tuple(g.get("estimate_keys") or [])
        grp = groups.get(("tie", keys))
        if grp is None:
            grp = groups[("tie", keys)] = {
                "kind": "tie",
                "title_fr": "Égalité non résolue — aucune estimation retenue, aucun nombre propagé",
                "edges": [],
                "nodes": [],
                "estimates": [
                    {**_conflict_row(k, evidence), "chosen": False, "point_used": p, "reason_fr": "à égalité"}
                    for k, p in zip(keys, g.get("points") or [None] * len(keys), strict=False)
                ],
                "message_fr": g.get("message_fr"),
            }
        grp["nodes"].append(g.get("node"))
    return list(groups.values())


def _conflict_row(key: str, evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    ev = evidence.get(key) or {}
    return {
        "estimate_key": key,
        "short": ev.get("short") or short_source(None, key),
        "published_fr": ev.get("published_size_fr"),
        "published_uncertainty_fr": ev.get("published_uncertainty_fr"),
        "point": ev.get("point"),
        "unit": ev.get("unit"),
        "method": ev.get("method"),
        "scope": ev.get("scope"),
        "effect_label_fr": ((ev.get("effect") or {}).get("label_fr")),
        "loaded": ev.get("loaded", False),
    }


# ------------------------------------------------------------------------------------------- diagnosis
GAP_TITLE_FR: dict[str, str] = {
    "no_effect_estimate_for_cause": "Aucune estimation d'effet n'a cette variable pour cause",
    "variable_not_in_effect_vocabulary": "Variable absente du vocabulaire des effets",
    "unresolved_tie": "Égalité non résolue entre estimations",
    "missing_input": "Entrées du modèle absentes de la publication de référence",
    "plugin_data_missing": "Données du modèle non épinglées",
    "plugin_input_missing": "Entrée du modèle absente sur la première année",
    "upstream_missing": "Valeur amont manquante",
    "max_depth": "Profondeur maximale atteinte",
    "baseline_unavailable": "Trajectoire de référence indisponible",
}


def gap_fr(g: Mapping[str, Any]) -> str:
    if isinstance(g.get("message_fr"), str):
        return str(g["message_fr"])
    title = GAP_TITLE_FR.get(str(g.get("code")), str(g.get("code")))
    msg = g.get("message")
    if isinstance(msg, list):
        msg = "; ".join(str(m) for m in msg)
    return f"{title} : {msg}" if msg else title


def _missing_inputs(g: Mapping[str, Any]) -> list[str]:
    out = []
    for p in g.get("problems") or []:
        if isinstance(p, Mapping):
            c = p.get("concept") or p.get("input")
            if c:
                out.append(f"{c} ({p.get('reason') or 'absent'})")
    if not out and isinstance(g.get("message"), str):
        m = re.search(r"missing: (.+)$", str(g["message"]))
        if m:
            out = [s.strip() for s in m.group(1).split(",")]
    return out


def diagnosis(
    result: Mapping[str, Any],
    near: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    *,
    labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """« Pourquoi pas de résultat » : the exact link or data where each chain stops."""
    near = near or {}
    labels = dict(labels or {})
    direct = {h["to"]: h for h in result.get("hops") or [] if h.get("kind") == "decision"}
    variables = result.get("variables") or []
    quantified = [v for v in variables if headline_value(v) is not None]
    blocking: list[dict[str, Any]] = []
    ties: list[dict[str, Any]] = []
    chain_ends: list[dict[str, Any]] = []
    plugins: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for g in result.get("gaps") or []:
        code = str(g.get("code"))
        nid = g.get("node")
        item: dict[str, Any] = {
            "code": code,
            "node": nid,
            "node_label": node_label(nid, labels) if nid else None,
            "title_fr": GAP_TITLE_FR.get(code, code),
            "detail_fr": gap_fr(g),
        }
        if code in ("no_effect_estimate_for_cause", "variable_not_in_effect_vocabulary"):
            kind, ident, geo = split_node(nid or "")
            if nid in direct:
                item["missing_fr"] = (
                    f"une estimation d'effet attribuée (publication citée, calcul REVA ou identité) dont la cause est "
                    f"« {labels.get(f'{kind}:{ident}') or ident} » ({kind}:{ident})"
                    if code == "no_effect_estimate_for_cause"
                    else f"la variable {ident} dans le vocabulaire des effets (registry/effects/variables.yaml)"
                )
                item["unblock_fr"] = (
                    "La propagation reprend sans changement du moteur dès qu'une telle estimation est chargée dans "
                    "kg.effect_estimate_current."
                    if code == "no_effect_estimate_for_cause"
                    else "Tant que la variable n'est pas dans le vocabulaire, aucune estimation ne peut la citer."
                )
                item["near"] = [dict(n) for n in near.get(nid, [])]
                _merge(blocking, item)
            else:
                chain_ends.append({"node": nid, "label": item["node_label"], "geo": geo})
        elif code == "unresolved_tie":
            item["estimate_keys"] = list(g.get("estimate_keys") or [])
            item["points"] = list(g.get("points") or [])
            item["unblock_fr"] = (
                "Déclarer une préférence entre ces estimations (registry/predict/estimate_preferences.yaml, "
                "décision du mainteneur) ; REVA ne tranche pas seul et ne moyenne pas."
            )
            ties.append(item)
        elif code in ("missing_input", "plugin_data_missing", "plugin_input_missing"):
            item["plugin"] = g.get("plugin")
            item["geo"] = g.get("geo")
            item["missing"] = _missing_inputs(g)
            item["unblock_fr"] = {
                "missing_input": "Épingler ces jeux de données dans une publication de référence (opération du "
                "responsable) ; aucune entrée n'est remplacée par zéro.",
                "plugin_data_missing": "Épingler le jeu de données du modèle dans la publication de référence.",
                "plugin_input_missing": "Le chemin amont n'a pas de valeur sur toute la première année (délais "
                "non publiés par la source, jamais interpolés).",
            }[code]
            plugins.append(item)
        else:
            other.append(item)
    for nid, h in direct.items():
        dc = h.get("direct_change") or {}
        if dc.get("transform") != "level":
            continue
        kind, ident, geo = split_node(nid)
        rows = [dict(n) for n in near.get(nid, [])]
        other.append(
            {
                "code": "level_target",
                "node": nid,
                "node_label": node_label(nid, labels),
                "title_fr": "Niveau cible, pas une variation",
                "detail_fr": (
                    f"La décision fixe un niveau de « {labels.get(f'{kind}:{ident}') or ident} » : seul un modèle qui "
                    "lit un niveau (ici le modèle de retraite) peut l'utiliser. "
                    + (
                        f"{len(rows)} estimation(s) ont pour cause une variation de cette variable : elles ne "
                        "s'appliquent pas à un niveau (par construction, jamais converties)."
                        if rows
                        else "Aucune estimation n'a cette variable pour cause."
                    )
                ),
                "near": rows,
            }
        )
    missing_periods = []
    for v in variables:
        miss = [p for p in v.get("periods") or [] if p.get("missing_reason")]
        if miss:
            missing_periods.append(
                {
                    "variable": v["id"],
                    "label": (v.get("variable") or {}).get("label_fr") or v["id"],
                    "geo": v.get("geo"),
                    "periods": [p["period"] for p in miss],
                    "reason_fr": "délai non publié par la source (jamais interpolé)"
                    if all(str(p["missing_reason"]).startswith("lag_not_published") for p in miss)
                    else "; ".join(sorted({str(p["missing_reason"]) for p in miss})),
                }
            )
    has = bool(quantified)
    if has:
        head = (
            f"{len(quantified)} variable(s) chiffrée(s) ; la chaîne s'arrête ensuite là où aucune estimation ne part "
            "de la variable atteinte."
        )
    elif blocking or plugins:
        first = (blocking or plugins)[0]
        head = f"Aucun résultat chiffré : la propagation s'arrête dès le premier lien — {first['title_fr'].lower()}."
    elif ties:
        head = "Aucun résultat chiffré : les estimations disponibles sont à égalité et aucune n'est retenue."
    else:
        head = "Aucun résultat chiffré."
    return {
        "has_result": has,
        "headline_fr": head,
        "blocking": blocking,
        "ties": ties,
        "plugins": plugins,
        "chain_ends": chain_ends,
        "missing_periods": missing_periods,
        "other": other,
    }


def _merge(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    for it in items:
        if it["node"] == item["node"]:
            it["detail_fr"] = f"{it['detail_fr']} ; {item['detail_fr']}"
            if item["code"] == "variable_not_in_effect_vocabulary":
                it["missing_fr"] = item["missing_fr"] + " ; " + it.get("missing_fr", "")
                it["code"] = item["code"]
            return
    items.append(item)


# ------------------------------------------------------------------------------------------- bundle
def estimate_keys(result: Mapping[str, Any]) -> list[str]:
    """Every estimate key the studio shows evidence for: chosen, alternatives, ties, sign-only."""
    keys: list[str] = []
    for h in result.get("hops") or []:
        if h.get("kind") == "estimate":
            keys.append(h["estimate_key"])
            keys += [a["estimate_key"] for a in h.get("alternatives") or []]
    for g in result.get("gaps") or []:
        keys += list(g.get("estimate_keys") or [])
    for u in result.get("unquantified_links") or []:
        if u.get("estimate_key"):
            keys.append(u["estimate_key"])
    return list(dict.fromkeys(keys))


def evidence_map(
    result: Mapping[str, Any], lookup: Mapping[str, Mapping[str, Any] | None]
) -> dict[str, dict[str, Any]]:
    prov: dict[str, Mapping[str, Any]] = {}
    for h in result.get("hops") or []:
        if h.get("kind") == "estimate":
            prov[h["estimate_key"]] = h.get("source") or {}
    for u in result.get("unquantified_links") or []:
        if u.get("estimate_key"):
            prov.setdefault(u["estimate_key"], u.get("source") or {})
    return {k: evidence_entry(k, prov.get(k), lookup.get(k)) for k in estimate_keys(result)}


def honesty(result: Mapping[str, Any], market: bool) -> dict[str, Any]:
    val = result.get("validation") or {}
    return {
        "label": result.get("label"),
        "validated": val.get("status") == "validated",
        "status": val.get("status"),
        "chain_signature": val.get("chain_signature"),
        "judge_requirements": list(val.get("judge_requirements") or []),
        "disclaimers_fr": list(result.get("disclaimers_fr") or []),
        "market": market,
        "not_advice_fr": "Recherche, pas un conseil financier ni une recommandation.",
    }


def make_bundle(
    result: Mapping[str, Any],
    lookup: Mapping[str, Mapping[str, Any] | None],
    near: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    *,
    market: bool = False,
) -> dict[str, Any]:
    ev = evidence_map(result, lookup)
    labels = _labels(result, ev)
    return {
        "headline": headline(result, ev),
        "graph": graph(result, ev, labels=labels),
        "evidence": ev,
        "conflicts": conflicts(result, ev),
        "diagnosis": diagnosis(result, near, labels=labels),
        "honesty": honesty(result, market),
        "labels": labels,
    }
