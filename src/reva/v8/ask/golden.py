"""Golden set of « Demander » (47 questions of NLQA6, ``tests/unit/nlqa/test_nlqa_domains.py``) and its coverage
classifier, for CI (fixture backends) and for the live measure on the dev database (read-only).

Classification of one answer (``as_json()`` of :class:`reva.nlqa.service.AskResult`):

* ``answered`` — at least one substantive sentence (fact, causal claim, sealed forecast, scenario, attributed
  estimate, path, link …) citing at least one *sourced* evidence item (not an absence, an identification or a
  registry term);
* ``partial`` — no direct answer, but sourced neighbouring evidence explicitly labelled non-equivalent
  (``related`` sentences of the effect layer);
* ``unknown`` — « REVA ne sait pas »: only absences, identifications or refusals. The cause class says what is
  missing (entity not in the label index, approximate name only, relation not loaded, series not loaded, no
  published estimate, no reference decision, no sourced causal claim, …).

Usage (live, read-only): ``uv run python -m reva.v8.ask.golden [--json out.json]``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

GOLDEN: tuple[str, ...] = (
    "De quoi est fait le méthane ?",
    "What is methane made of?",
    "De quoi est fait le pétrole ?",
    "Composition du pétrole ?",
    "De quoi est composé le cœur ?",
    "Quelles sont les parties du cœur ?",
    "Quels gènes sur le chromosome 17 ?",
    "De quoi est fait le granite ?",
    "Composition chimique de l'eau ?",
    "Que mange le loup ?",
    "What does the wolf eat?",
    "Qui mange le lapin ?",
    "Que mange le lapi ?",
    "Où vit le lion ?",
    "Where does the lion live?",
    "Combien d'espèces de mammifères ?",
    "How many species of mammals?",
    "Que mange le tigre ?",
    "Habitat du loup ?",
    "Qui possède la mine KOV ?",
    "Who owns the KOV mine?",
    "Que possède Glencore ?",
    "Qui produit du cuivre ?",
    "Quelles centrales au charbon en Inde ?",
    "Which coal power plants are in India?",
    "Prix du Brent en 2020",
    "Quel est le prix du Brent ?",
    "Quelles maladies causées par Mycobacterium tuberculosis ?",
    "Diseases caused by Mycobacterium tuberculosis?",
    "Causes de décès en France",
    "Causes of death in France in 2021",
    "Causes de décès au Sénégal",
    "Quelles maladies causées par le virus Zika ?",
    "Quelle est la prévision d'inflation pour la France ?",
    "What is the inflation forecast for France?",
    "Prévision d'inflation pour le Japon ?",
    "Prévision du chômage en France ?",
    "Que se passe-t-il si la BCE monte ses taux de 50 pb ?",
    "What happens if the ECB raises rates by 25 bp?",
    "Et si l'OPEP+ réduit sa production de 2 mb/j ?",
    "Que se passe-t-il si la BCE monte ses taux de 2 % ?",
    "Que se passerait-il si les licornes votaient ?",
    "Quel est l'effet d'une hausse du pétrole sur l'inflation ?",
    "What is the effect of an oil price increase on inflation?",
    "Quel est l'impact d'une hausse des taux directeurs sur le PIB ?",
    "Quel est l'effet d'une hausse du pétrole sur le chômage ?",
    "Quel est l'effet du pétrole sur le PIB ?",
)

#: Sentence kinds that are not an answer by themselves.
NON_ANSWER_KINDS = frozenset(
    {"identification", "missing", "unavailable", "unsupported", "ambiguous", "note", "sources", "related"}
)
#: Evidence kinds that are not a sourced record.
UNSOURCED_EVIDENCE = frozenset({"absence", "entity_identification", "registry_term"})

#: absence ``what`` → cause class (what is missing), in French for the report and the UI.
CAUSES: dict[str, str] = {
    "entity": "entité absente de l'index des libellés (fr/en) du graphe",
    "entity_approximate": "seulement des correspondances approchées du nom",
    "kg_families": "relation non chargée pour cette entité",
    "kg_cross": "aucune entité chargée au croisement des deux",
    "atlas_period": "série absente de l'instantané atlas et des tables enregistrées",
    "atlas_value": "valeur absente des sources déclarées",
    "effect_estimates": "aucune estimation publiée chargée pour ce couple cause → effet",
    "effect_cause_term": "aucun terme de la couche d'effets pour la cause",
    "effect_effect_term": "aucun terme de la couche d'effets pour l'effet",
    "forecast_family": "aucune famille de prévisions scellées",
    "decision_reference": "aucune décision de référence du moteur",
    "causal_effects": "aucune affirmation causale sourcée",
    "causal_causes": "aucune affirmation causale sourcée",
}


def _sourced_ids(ans: Mapping[str, Any]) -> set[str]:
    return {e["id"] for e in ans.get("evidence") or [] if e.get("kind") not in UNSOURCED_EVIDENCE}


def classify(ans: Mapping[str, Any]) -> dict[str, Any]:
    """``{"status": answered|partial|unknown, "causes": [...], "kinds": [...]}`` for one answer."""
    sourced = _sourced_ids(ans)
    sentences = ans.get("sentences") or []
    direct = [
        s
        for s in sentences
        if s.get("kind") not in NON_ANSWER_KINDS and any(c in sourced for c in s.get("citations") or [])
    ]
    related = [
        s for s in sentences if s.get("kind") == "related" and any(c in sourced for c in s.get("citations") or [])
    ]
    status = "answered" if direct else ("partial" if related else "unknown")
    causes: list[str] = []
    if status != "answered":
        for e in ans.get("evidence") or []:
            if e.get("kind") != "absence":
                continue
            what = str(e.get("what") or "")
            key = "entity_approximate" if what == "kg_families" and "candidates" in e else what
            causes.append(key)
        for s in sentences:
            if s.get("kind") == "missing" and "correspondances approchées" in s.get("text", ""):
                causes.append("entity_approximate")
    return {
        "status": status,
        "causes": sorted(set(causes)),
        "kinds": sorted({str(s.get("kind")) for s in direct}),
    }


def missing_text(causes: Sequence[str]) -> list[str]:
    return [CAUSES.get(c, c) for c in causes]


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    st = Counter(r["status"] for r in rows)
    lat = sorted(float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None)

    def pct(p: float) -> float | None:
        if not lat:
            return None
        k = min(len(lat) - 1, max(0, round(p * (len(lat) - 1))))
        return round(lat[k], 1)

    return {
        "n": len(rows),
        "answered": st.get("answered", 0),
        "partial": st.get("partial", 0),
        "unknown": st.get("unknown", 0),
        "latency_ms": {
            "median": round(statistics.median(lat), 1) if lat else None,
            "p90": pct(0.9),
            "max": lat[-1] if lat else None,
        },
    }


async def run(
    questions: Sequence[str], *, catalog: Any, backends: Any, ask: Any = None, progress: bool = False
) -> list[dict[str, Any]]:
    """Answer every question with the keyword planner (no LLM) and classify it."""
    from reva.nlqa.service import AskInput, answer_question

    out = []
    for q in questions:
        t0 = time.perf_counter()
        if ask is not None:
            ans = await ask(q)
        else:
            r = await answer_question(
                AskInput(q, "a" * 64), catalog=catalog, backends=backends, drafters=None, ledger=None, budget=None
            )
            ans = r.as_json()
        ms = (time.perf_counter() - t0) * 1000
        c = classify(ans)
        if progress:
            print(f"{c['status']:9s} {ms:8.0f} ms  {q}", file=sys.stderr, flush=True)
        out.append(
            {
                "question": q,
                **c,
                "missing": missing_text(c["causes"]),
                "latency_ms": round(ms, 1),
                "operations": [o["op"] for o in ans["plan"]["operations"]],
                "first": next((s["text"] for s in ans["sentences"] if s["kind"] not in ("identification",)), ""),
            }
        )
    return out


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live tool
    ap = argparse.ArgumentParser(description="Run the « Demander » golden set on the dev database (read-only).")
    ap.add_argument("--json", help="write the per-question results here")
    ap.add_argument("--repeat", type=int, default=2, help="passes (the last one is reported: warm)")
    args = ap.parse_args(argv)
    from reva.adapters.postgres.engine import make_engine
    from reva.nlqa.adapters import build_backends
    from reva.nlqa.catalog import build_catalog
    from reva.settings import get_settings

    s = get_settings()
    b = build_backends(make_engine("api", s), s)
    cat = build_catalog()
    warm = getattr(b.atlas, "warm", None)
    t0 = time.perf_counter()
    if callable(warm):
        warm()
    print(f"atlas warm-up {time.perf_counter() - t0:.1f} s", file=sys.stderr, flush=True)
    rows: list[dict[str, Any]] = []
    for _ in range(max(1, args.repeat)):
        rows = asyncio.run(run(GOLDEN, catalog=cat, backends=b, progress=True))
    summ = summarise(rows)
    for r in rows:
        print(f"{r['status']:9s} {r['latency_ms']:8.0f} ms  {r['question']}  {'; '.join(r['missing'])}")
    print(json.dumps(summ, ensure_ascii=False))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"summary": summ, "rows": rows}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
