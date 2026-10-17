"""Markdown brief of one scenario, with every citation (export of the decision studio).

The brief restates the result exactly as the studio shows it: the label (« scénario non validé — pas une
probabilité » unless validated), the decision and its stated assumptions, the quantified variables with their
5–95 % range, each link's estimate as published with citation, locator, verbatim quote and licence, competing
estimates side by side, the links without numbers, where and why the chain stops, the sensitivity, and the
provenance of the run. It never adds a number.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from reva.v8.decision.bundle import fmt_num, geo_fr, node_label, unit_fr


def _md(s: Any) -> str:
    """Escape what would break a Markdown table cell or start markup; keep the text verbatim otherwise."""
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _range(v: Mapping[str, Any] | None) -> str:
    if not v or v.get("p50") is None:
        return "manquant"
    if v.get("degenerate"):
        return f"{fmt_num(v['p50'])} (point, sans incertitude publiée)"
    return f"{fmt_num(v['p50'])} [{fmt_num(v.get('p05'))} ; {fmt_num(v.get('p95'))}]"


def brief_markdown(
    bundle: Mapping[str, Any],
    *,
    url: str | None = None,
    sensitivity: Mapping[str, Any] | None = None,
    scenario_hash: str | None = None,
) -> str:
    r = bundle["result"]
    d = r.get("decision") or {}
    hon = bundle.get("honesty") or {}
    ev: Mapping[str, Mapping[str, Any]] = bundle.get("evidence") or {}
    out: list[str] = []
    w = out.append
    w(f"# {d.get('label_fr') or d.get('id')}")
    w("")
    w(
        f"**{hon.get('label') or r.get('label')}**"
        + ("" if hon.get("validated") else " — ni une prévision, ni une probabilité.")
    )
    w(f"*{hon.get('not_advice_fr') or 'Recherche, pas un conseil financier ni une recommandation.'}*")
    w("")
    if url:
        w(f"Scénario : `{url}`")
    if scenario_hash:
        w(f"Empreinte des entrées : `{scenario_hash[:16]}`")
    w("")
    w("## Décision (hypothèses énoncées)")
    w("")
    mag = d.get("magnitude") or {}
    w(
        f"- Acteur : {d.get('actor', {}).get('label_fr')}"
        + (f" ({d['actor']['geo']})" if (d.get("actor") or {}).get("geo") else "")
    )
    w(f"- Instrument : `{d.get('instrument')}` — grandeur {mag.get('value')} {mag.get('unit')}")
    w(f"- Cible : {(d.get('target') or {}).get('label_fr')}")
    dur = (
        "maintenue sur tout l'horizon"
        if d.get("duration_quarters") is None
        else f"{d.get('duration_quarters')} trimestre(s)"
    )
    w(f"- Début : {d.get('start')}, durée : {dur}, horizon : {d.get('horizon_quarters')} trimestres")
    if d.get("countries"):
        w(
            f"- Pays d'intérêt : {', '.join(d['countries'])}"
            + (f" — {d['countries_note_fr']}" if d.get("countries_note_fr") else "")
        )
    for dc in r.get("direct_changes") or []:
        w(
            f"- Changement direct supposé : `{(dc.get('variable') or {}).get('id')}` @ {dc.get('geo')} = {dc.get('change')} {unit_fr(dc.get('unit'))} — {dc.get('basis_fr')}"
        )
    w("")

    w("## Résultat")
    w("")
    diag = bundle.get("diagnosis") or {}
    w(diag.get("headline_fr") or "")
    w("")
    rows = bundle.get("headline") or []
    if rows:
        w("| Variable | Pays | Valeur médiane [5 % ; 95 %] | Unité | Période | Sources | Drapeaux |")
        w("|---|---|---|---|---|---|---|")
        for h in rows:
            v = h.get("value")
            per = (v or {}).get("period_label_fr") or "manquant"
            src = ", ".join(s["short"] for s in h.get("sources") or []) or ", ".join(h.get("models") or []) or "—"
            flags = ", ".join(f["label_fr"] for f in h.get("flags") or [])
            w(
                f"| {_md(h['label_fr'])} | {_md(h['geo_fr'])} | {_md(_range(v))} | {_md(h['unit_fr'])} | {_md(per)} | {_md(src)} | {_md(flags)} |"
            )
        w("")
        w("Les intervalles ne reflètent que l'incertitude publiée par les estimations utilisées.")
        w("")

    w("## Chaîne de propagation et preuves")
    w("")
    edges = [e for e in (bundle.get("graph") or {}).get("edges") or [] if e.get("kind") == "estimate"]
    seen: set[str] = set()
    labels = bundle.get("labels") or {}
    for e in edges:
        k = e["estimate_key"]
        x = ev.get(k) or {}
        frm = _node(e["from"], labels)
        to = _node(e["to"], labels)
        w(f"### {frm} → {to}")
        w("")
        eff = e.get("effect") or {}
        w(f"- Estimation : {x.get('published_fr') or '—'} ; {x.get('published_uncertainty_fr') or ''}")
        w(
            f"- Coefficient appliqué : {eff.get('beta_label_fr')}"
            + (
                f", écart-type {fmt_num(eff.get('beta_sd')).lstrip('+')}"
                if eff.get("beta_sd") is not None
                else " (sans incertitude publiée)"
            )
        )
        if e.get("applicability_notes_fr"):
            w(
                f"- Applicabilité : {(e.get('applicability') or {}).get('status')} — "
                + " ; ".join(e["applicability_notes_fr"])
            )
        if k not in seen:
            seen.add(k)
            _cite(w, x)
        else:
            w(f"- Source : voir {x.get('short')} ci-dessus (`{k}`)")
        w("")

    conf = bundle.get("conflicts") or []
    if conf:
        w("## Estimations concurrentes (côte à côte, jamais moyennées)")
        w("")
        for c in conf:
            w(
                f"**{c['title_fr']}** — liens : {', '.join(_node(n, labels) for n in dict.fromkeys(c.get('nodes') or []))}"
            )
            w("")
            w("| Estimation | Retenue | Taille publiée | Incertitude | Méthode |")
            w("|---|---|---|---|---|")
            for x in c.get("estimates") or []:
                w(
                    f"| {_md(x['short'])} (`{_md(x['estimate_key'])}`) | {'oui' if x.get('chosen') else 'non'} | {_md(x.get('published_fr') or fmt_num(x.get('point_used')))} | {_md(x.get('published_uncertainty_fr'))} | {_md(x.get('method'))} |"
                )
            w("")
            for x in c.get("estimates") or []:
                if not x.get("chosen") and x["estimate_key"] not in seen:
                    seen.add(x["estimate_key"])
                    w(f"- {x['short']} :")
                    _cite(w, ev.get(x["estimate_key"]) or {}, indent="  ")
            w("")

    unq = r.get("unquantified_links") or []
    if unq:
        w("## Liens non chiffrés (aucun nombre)")
        w("")
        for u in unq:
            grade = f" (grade {u['grade']})" if u.get("grade") else ""
            w(f"- {u.get('wording_fr') or u.get('note_fr')}{grade}")
        w("")

    w("## Où la chaîne s'arrête, et pourquoi")
    w("")
    for b in [
        *(diag.get("blocking") or []),
        *(diag.get("plugins") or []),
        *(diag.get("ties") or []),
        *(diag.get("other") or []),
    ]:
        w(f"- **{b['title_fr']}** — {b.get('node_label') or b.get('plugin') or ''} : {b.get('detail_fr')}")
        if b.get("missing_fr"):
            w(f"  - Il manque : {b['missing_fr']}")
        for m in b.get("missing") or []:
            w(f"  - Il manque : `{m}`")
        if b.get("unblock_fr"):
            w(f"  - Pour débloquer : {b['unblock_fr']}")
        for n in b.get("near") or []:
            w(f"  - Estimation voisine non applicable : `{n.get('estimate_key')}` (cause `{n.get('cause')}`)")
    ends = diag.get("chain_ends") or []
    if ends:
        w(f"- Fin de chaîne (aucune estimation ne part de ces variables) : {', '.join(_md(e['label']) for e in ends)}")
    for m in diag.get("missing_periods") or []:
        w(
            f"- Périodes non calculées pour {m['label']} ({geo_fr(m['geo'])}) : {', '.join(m['periods'])} — {m['reason_fr']}"
        )
    w("")

    if sensitivity:
        w("## Sensibilité (un à la fois)")
        w("")
        w(sensitivity.get("method_fr") or "")
        w("")
        for v in sensitivity.get("variables") or []:
            head = v.get("headline_period")
            if not v.get("bars") and not v.get("no_uncertainty"):
                continue
            w(f"**{v['label_fr']} ({geo_fr(v['geo'])})** — période {head or 'aucune'}")
            w("")
            for b in v.get("bars") or []:
                lo, hi = (b.get("low") or {}).get(head or ""), (b.get("high") or {}).get(head or "")
                w(
                    f"- {b['short']} : de {fmt_num(lo)} à {fmt_num(hi)} {unit_fr(v.get('unit'))} (écart {fmt_num((b.get('swing') or {}).get(head or '')).lstrip('+')}, r² MC {(b.get('mc_r2') or {}).get(head or '')})"
                )
            for f in v.get("no_uncertainty") or []:
                w(f"- {f['short']} : incertitude non publiée — pas de barre")
            w("")

    w("## Validation et provenance")
    w("")
    val = r.get("validation") or {}
    w(f"- Statut : {val.get('status')} — {val.get('label')}")
    if val.get("reason"):
        w(f"- Raison : {val['reason']}")
    for j in val.get("judge_requirements") or []:
        w(f"- Exigence du juge : {j}")
    w(f"- Signature de la chaîne : `{val.get('chain_signature')}`")
    st = r.get("settings") or {}
    w(f"- Moteur : `{r.get('engine')}`, {st.get('draws')} tirages, graine {st.get('seed')}")
    prov = r.get("provenance") or {}
    if prov.get("atlas_snapshot"):
        w(f"- Instantané de l'atlas : `{prov['atlas_snapshot']}`")
    rel = (prov.get("release") or {}).get("release_id") if isinstance(prov.get("release"), Mapping) else None
    if rel:
        w(f"- Publication de référence : `{rel}`")
    eff = r.get("effect_layer") or {}
    w(f"- Couche d'effets : {eff.get('layer')} ({eff.get('detail')})")
    w("")
    w("## Avertissements")
    w("")
    for t in r.get("disclaimers_fr") or []:
        w(f"- {t}")
    w("")
    return "\n".join(out).rstrip() + "\n"


def _node(nid: str, labels: Mapping[str, str]) -> str:
    return node_label(nid, labels)


def _cite(w: Any, x: Mapping[str, Any], indent: str = "") -> None:
    w(f"{indent}- Source : {x.get('citation') or x.get('short')} (`{x.get('estimate_key')}`)")
    if x.get("doi"):
        w(f"{indent}- DOI : {x['doi']}")
    if x.get("url"):
        w(f"{indent}- Lien : {x['url']}")
    if x.get("locator"):
        w(f"{indent}- Emplacement : {x['locator']}")
    if x.get("quote"):
        w(f"{indent}- Citation verbatim : « {x['quote']} »")
    run = x.get("run")
    if isinstance(run, Mapping) and run.get("run_id"):
        w(
            f"{indent}- Calcul REVA : run `{run.get('run_id')}`, données au {run.get('as_of')}, code `{run.get('code_version')}`"
        )
    if x.get("method"):
        w(f"{indent}- Méthode : {x['method']}" + (f" — {x['identification']}" if x.get("identification") else ""))
    if x.get("applicability_note"):
        w(f"{indent}- Note d'applicabilité : {x['applicability_note']}")
    rights = x.get("rights") or {}
    if rights:
        flag = " (usage restreint)" if x.get("restricted") else ""
        flag += " (non commercial)" if x.get("non_commercial") else ""
        w(f"{indent}- Licence : {rights.get('licence') or '—'}{flag}")
    if x.get("as_of"):
        w(f"{indent}- Connue au : {x['as_of']}")
    if x.get("wording_fr"):
        w(f"{indent}- Formulation : {x['wording_fr']}")
