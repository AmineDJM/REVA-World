"""« Brainstormer » (agent V8ASK): for a forecasting target ("prévoir l'inflation française à 4 trimestres", "prévoir
le prix du Brent"), list what REVA's own registries say — pure retrieval and rules, no LLM, no network:

1. **cibles** — the registry terms the target's words designate: sealed-forecast question families
   (``registry/forecasts``), atlas catalog concepts, honest-scoreboard targets (``registry/scoreboard``) and
   effect-layer variables (``kg.effect_estimate`` terms; matched by label, or — labelled « proche » — by sharing
   ≥ 2 content words with an anchor concept/family label);
2. **pilotes** — the causes of published effect estimates on those variables, grouped by cause, each attributed
   (reference, method, sign counts — never averaged), plus sourced causal claims on the target when a KG entity
   carries the target's exact label;
3. **données** — for each driver, the atlas series a curator linked to it (``registry/effects/variables.yaml``
   ``atlas_concepts``) or the driver itself when it is an atlas concept, with its last loaded period (freshness)
   for the country; drivers without a loaded series are listed as missing with candidate catalog concepts (by label
   words, *not curated*) and queued loads quoted from ``docs/dev``;
4. **prévisions existantes** — sealed questions of the matched families (pending / resolved, models and methods)
   and the scoreboard verdicts vs official forecasters (copied, never upgraded);
5. **prochaines étapes** — ranked by explicit rules (missing series of a well-documented driver, stale series, no
   factor model in the registry, no question family); suggestions, not promises of skill.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from reva.nlqa.catalog import fold
from reva.nlqa.domains import content_words, match_terms

REPO = Path(__file__).resolve().parents[4]
SCOREBOARD = REPO / "registry/scoreboard/tracks.yaml"
EFFECT_VARIABLES = REPO / "registry/effects/variables.yaml"
MAX_DRIVERS = 12
MAX_ESTIMATES = 300
STALE_YEARS = 2

_VERB = re.compile(
    r"^\s*(?:pr[ée]voir|pr[ée]dire|anticiper|projeter|mod[ée]liser|forecast(?:ing)?|predict(?:ing)?|nowcast(?:er)?)\s+",
    re.I,
)
_HORIZON = re.compile(
    r"\b(?:à|a|sur|dans|at|over|in|for)?\s*(?P<n>\d{1,3})\s*(?P<u>trimestres?|quarters?|mois|months?|ans?|ann[ée]es?|years?|semaines?|weeks?|jours?|days?)\b",
    re.I,
)
#: Country adjectives (fr/en) → ISO3: a parsing aid for "l'inflation française" (not data).
DEMONYMS: dict[str, str] = {
    "francais": "FRA", "francaise": "FRA", "french": "FRA", "allemand": "DEU", "allemande": "DEU", "german": "DEU",
    "italien": "ITA", "italienne": "ITA", "italian": "ITA", "espagnol": "ESP", "espagnole": "ESP", "spanish": "ESP",
    "britannique": "GBR", "british": "GBR", "americain": "USA", "americaine": "USA", "american": "USA",
    "japonais": "JPN", "japonaise": "JPN", "japanese": "JPN", "chinois": "CHN", "chinoise": "CHN", "chinese": "CHN",
    "indien": "IND", "indienne": "IND", "indian": "IND", "bresilien": "BRA", "bresilienne": "BRA", "brazilian": "BRA",
    "canadien": "CAN", "canadienne": "CAN", "canadian": "CAN", "russe": "RUS", "russian": "RUS",
    "senegalais": "SEN", "senegalaise": "SEN", "senegalese": "SEN", "nigerian": "NGA", "nigeriane": "NGA",
    "mexicain": "MEX", "mexicaine": "MEX", "mexican": "MEX", "turc": "TUR", "turque": "TUR", "turkish": "TUR",
    "suisse": "CHE", "swiss": "CHE", "belge": "BEL", "belgian": "BEL", "neerlandais": "NLD", "dutch": "NLD",
}  # fmt: skip


def parse_target(target: str, catalog: Any) -> dict[str, Any]:
    t = target.strip()
    t2 = _VERB.sub("", t)
    horizon = None
    m = _HORIZON.search(t2)
    if m:
        horizon = {"n": int(m.group("n")), "unit": fold(m.group("u"))}
        t2 = (t2[: m.start()] + " " + t2[m.end() :]).strip()
    iso3 = None
    words = re.findall(r"[\wÀ-ÿ'’-]+", t2)
    rest: list[str] = []
    for w in words:
        f = fold(w).strip("'’")
        f = re.sub(r"^(?:l|d)['’]", "", f)
        if f in DEMONYMS and iso3 is None:
            iso3 = DEMONYMS[f]
            continue
        rest.append(w)
    text = " ".join(rest)
    if iso3 is None:
        ft = f" {fold(text)} "
        for c in catalog.countries.values():
            for n in sorted(c.names, key=len, reverse=True):
                fn = fold(n)
                if len(fn) >= 4 and f" {fn} " in ft:
                    iso3 = c.iso3
                    text = re.sub(re.escape(n), " ", text, flags=re.I).strip()
                    break
            if iso3:
                break
    text = re.sub(r"\b(?:en|au|aux|in|for|pour|de la|du|des|le|la|les|l['’])\s*$", "", text.strip(), flags=re.I)
    text = re.sub(r"^(?:(?:le|la|les|un|une|des|du|de la|the|a|an)\s+|l['’]\s*)", "", text.strip(" ,.?"), flags=re.I)
    return {"target": t, "text": text.strip(" ,.?"), "iso3": iso3, "horizon": horizon}


def _words(s: str) -> set[str]:
    return set(content_words(re.sub(r"\(.*?\)", " ", s)))


def _overlap(a: set[str], b: set[str]) -> int:
    return sum(1 for w in a if any(w == x or (len(w) >= 5 and x[:5] == w[:5]) for x in b))


@lru_cache(maxsize=1)
def _scoreboard() -> dict[str, Any]:
    if not SCOREBOARD.is_file():
        return {}
    return yaml.safe_load(SCOREBOARD.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def _variables() -> dict[str, dict[str, Any]]:
    if not EFFECT_VARIABLES.is_file():
        return {}
    doc = yaml.safe_load(EFFECT_VARIABLES.read_text(encoding="utf-8")) or {}
    return {str(v["id"]): v for v in doc.get("variables") or []}


@lru_cache(maxsize=1)
def _queued_lines() -> tuple[tuple[str, int, str], ...]:
    """Lines of the tracked reports in docs/dev that speak of queued / not loaded sources (for "what is missing")."""
    out: list[tuple[str, int, str]] = []
    pat = re.compile(
        r"queued|en file|file d'attente|not loaded|non charg|pas encore charg|à charger|owner decision", re.I
    )
    try:
        files = subprocess.run(
            ["git", "ls-files", "docs/dev/*.md"],  # noqa: S607 - developer tool, git from PATH
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        files = [str(p.relative_to(REPO)) for p in (REPO / "docs/dev").glob("*.md")]
    for rel in files:
        try:
            for i, line in enumerate((REPO / rel).read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    out.append((rel, i, line.strip()[:400]))
        except OSError:
            continue
    return tuple(out)


def queued_mentions(keywords: Sequence[str], limit: int = 3) -> list[dict[str, Any]]:
    kws = [fold(k) for k in keywords if len(k) >= 6 and "." in k]
    if not kws:
        return []
    hits = []
    for rel, i, line in _queued_lines():
        fl = fold(line)
        n = sum(1 for k in kws if k in fl)
        if n:
            hits.append((n, rel, i, line))
    hits.sort(key=lambda h: (-h[0], h[1], h[2]))
    return [{"file": rel, "line": i, "text": line} for _, rel, i, line in hits[:limit]]


class Brainstormer:
    def __init__(self, backends: Any, catalog: Any) -> None:
        self.b = backends
        self.catalog = catalog

    # ------------------------------------------------------------------ anchors
    def _families(self, text: str) -> list[dict[str, Any]]:
        port = getattr(self.b, "forecasts", None)
        if port is None:
            return []
        fams = {str(f["key"]): f for f in port.families()}
        labels = {k: {"fr": str(f.get("label_fr") or k), "en": str(f.get("label_en") or k)} for k, f in fams.items()}
        return [dict(fams[k]) for k in match_terms(text, labels, limit=8)]

    def _concepts(self, text: str) -> list[Any]:
        labels = {cid: c.labels for cid, c in self.catalog.concepts.items()}
        ids = match_terms(text, labels, limit=6)
        return [self.catalog.concepts[i] for i in ids]

    def _score_targets(self, text: str) -> list[dict[str, Any]]:
        sb = _scoreboard()
        targets = {str(t["id"]): {"fr": str(t.get("label_fr") or t["id"])} for t in sb.get("targets") or []}
        ids = match_terms(text, targets, limit=8)
        by = {str(t["id"]): t for t in sb.get("targets") or []}
        return [dict(by[i]) for i in ids]

    def _effect_targets(self, text: str, anchor_labels: Sequence[str]) -> list[dict[str, Any]]:
        port = getattr(self.b, "effects", None)
        if port is None:
            return []
        terms = {t.id: t for t in port.terms()}
        labels = {tid: t.labels for tid, t in terms.items()}
        words = list(dict.fromkeys(content_words(text)))
        direct = [
            tid
            for tid in match_terms(text, labels, limit=8)
            # every word of the target must be in the term's labels ("prix du Brent" is not every price)
            if all(_overlap({w}, set().union(*(_words(str(v)) for v in labels[tid].values()))) for w in words)
        ]
        out = [{"id": i, "kind": terms[i].kind, "labels": dict(terms[i].labels), "match": "libellé"} for i in direct]
        if not out and anchor_labels:
            anchor = set().union(*(_words(a) for a in anchor_labels))
            near = []
            for tid, t in terms.items():
                lw = set().union(*(_words(str(v)) for v in t.labels.values())) if t.labels else set()
                n = _overlap(lw, anchor)
                if n >= 2:
                    near.append((n, tid))
            near.sort(key=lambda x: (-x[0], x[1]))
            out = [
                {
                    "id": tid,
                    "kind": terms[tid].kind,
                    "labels": dict(terms[tid].labels),
                    "match": "proche (non équivalent)",
                }
                for _, tid in near[:4]
            ]
        return out

    # ------------------------------------------------------------------ drivers
    def _drivers(self, targets: Sequence[Mapping[str, Any]], iso3: str | None) -> list[dict[str, Any]]:
        port = getattr(self.b, "effects", None)
        if port is None or not targets:
            return []
        rows = port.estimates([], [t["id"] for t in targets], country=None, limit=MAX_ESTIMATES)
        by: dict[str, dict[str, Any]] = {}
        for e in rows:
            cause = e.get("cause") or {}
            cid = str(cause.get("id"))
            d = by.setdefault(
                cid,
                {
                    "id": cid,
                    "kind": cause.get("kind"),
                    "label": cause.get("label_fr") or cause.get("label") or cid,
                    "effects": set(),
                    "n": 0,
                    "signs": Counter(),
                    "methods": Counter(),
                    "references": [],
                    "countries": set(),
                    "covers_country": False,
                    "example": None,
                },
            )
            d["n"] += 1
            d["effects"].add((e.get("effect") or {}).get("label_fr") or (e.get("effect") or {}).get("id"))
            d["signs"][str(e.get("sign") or "?")] += 1
            d["methods"][str(e.get("method") or "?")] += 1
            src = e.get("source") or {}
            ref = src.get("citation") or {}
            short = ref.get("short") if isinstance(ref, Mapping) else None
            if short and short not in d["references"]:
                d["references"].append(short)
            scope = e.get("scope") or {}
            cs = set(scope.get("countries") or [])
            d["countries"].update(cs)
            if iso3 and iso3 in cs:
                d["covers_country"] = True
            if d["example"] is None:
                d["example"] = {"estimate_key": e.get("estimate_key"), "wording_fr": e.get("wording_fr")}
        out = []
        for d in sorted(by.values(), key=lambda d: (-int(d["covers_country"]), -d["n"], d["id"]))[:MAX_DRIVERS]:
            d["effects"] = sorted(x for x in d["effects"] if x)
            d["signs"] = dict(d["signs"])
            d["methods"] = dict(d["methods"].most_common(4))
            d["countries"] = sorted(d["countries"])[:20]
            d["references"] = d["references"][:6]
            out.append(d)
        return out

    def _claims(self, text: str) -> list[dict[str, Any]]:
        """Sourced causal claims whose effect is the KG entity carrying the target's exact label (T1–T4)."""
        kg, causal = getattr(self.b, "kg", None), getattr(self.b, "causal", None)
        if kg is None or causal is None or not text:
            return []
        try:
            matches = [m for m in kg.search_entities(text, kind=None, lang="fr", limit=5) if m.match == "label_exact"]
        except Exception:  # an unavailable KG only removes this section
            return []
        out = []
        for m in matches[:2]:
            try:
                res = causal.explain(m.entity.qid, direction="causes", limit=12)
            except Exception:  # noqa: S112 - an unavailable causal service only removes this section
                continue
            for c in res.claims[:8]:
                out.append(
                    {
                        "effect": {"id": c.effect_id, "label": c.effect_labels.get("fr") or c.effect_labels.get("en")},
                        "cause": {"id": c.cause_id, "label": c.cause_labels.get("fr") or c.cause_labels.get("en")},
                        "relation": c.relation,
                        "source": c.source_key,
                        "tier": c.tier,
                        "grade": getattr(c, "grade", None),
                    }
                )
        return out

    # ------------------------------------------------------------------ data status
    def _series_status(self, concept_id: str, iso3: str | None) -> dict[str, Any]:
        atlas = getattr(self.b, "atlas", None)
        c = self.catalog.concepts.get(concept_id)
        out: dict[str, Any] = {
            "concept": concept_id,
            "label": c.label("fr") if c else concept_id,
            "unit": c.unit if c else None,
        }
        if atlas is None or c is None:
            out["status"] = "inconnu"
            return out
        try:
            if iso3:
                res = atlas.series(concept_id, iso3, None, None)
            else:
                res = atlas.period_cells(concept_id, None, None) if hasattr(atlas, "period_cells") else None
        except Exception:  # the atlas being unavailable is said, not hidden
            out["status"] = "atlas indisponible"
            return out
        cells = [x for x in (res.cells if res else ()) if x.value is not None]
        tables = getattr(self.b, "tables", None)
        if not cells and tables is not None and not iso3:
            series = [tuple(str(k).split(".", 2)) for k in c.source_order if str(k).count(".") >= 2]
            try:
                got, _pins = tables.period_cells(concept_id, series, None, None)
            except Exception:  # no registered version: the snapshot absence stands
                got = []
            cells = [x for x in got if x.value is not None]
            if cells:
                out["outside_snapshot"] = True
        if not cells:
            out["status"] = "manquant"
            out["consulted"] = list(res.sources_consulted) if res else []
            return out
        from reva.nlqa.ports import cell_is_projection

        observed = [x for x in cells if not cell_is_projection(x)]
        projected = [x for x in cells if cell_is_projection(x)]
        if projected:
            out["projections_to"] = str(max(projected, key=lambda x: str(x.period_start)).period_start)[:10]
        if not observed:
            out["status"] = "projections seulement"
            return out
        last = max(observed, key=lambda x: (str(x.period_end), str(x.period_start)))
        out.update(
            status="chargé",
            last_period=str(last.period_start)[:10],
            source=f"{last.source}/{last.dataset}",
            n=len(observed),
        )
        y = last.year
        out["stale"] = bool(y is not None and datetime.now(UTC).year - y > STALE_YEARS)
        return out

    def _driver_data(self, d: Mapping[str, Any], iso3: str | None) -> dict[str, Any]:
        var = _variables().get(str(d["id"]))
        concepts: list[str] = []
        how = None
        if d.get("kind") == "atlas_concept" and d["id"] in self.catalog.concepts:
            concepts, how = [d["id"]], "la variable est un concept de l'atlas"
        elif var and var.get("atlas_concepts"):
            concepts, how = [str(x) for x in var["atlas_concepts"]], "lien curé (registry/effects/variables.yaml)"
        elif var and var.get("growth_concepts"):
            concepts, how = [str(x) for x in var["growth_concepts"]], "lien curé (taux de croissance)"
        series = [self._series_status(c, iso3) for c in concepts[:3]]
        out: dict[str, Any] = {"link": how, "series": series}
        if not series or all(s["status"] != "chargé" for s in series):
            label = str(d.get("label") or d["id"])
            lw = _words(label) | (_words(str(var.get("labels", {}).get("en", ""))) if var else set())
            cands = []
            for cid, c in self.catalog.concepts.items():
                cw = set().union(*(_words(str(v)) for v in c.labels.values())) if c.labels else set()
                n = _overlap(lw, cw)
                if n >= 2:
                    cands.append((-n, len(cw), cid))
            out["candidates"] = [
                {"concept": cid, "label": self.catalog.concepts[cid].label("fr"), "curated": False}
                for _, _, cid in sorted(cands)[:3]
            ]
            # queued loads quoted only when a report names the variable or a candidate concept by its id
            out["queued"] = queued_mentions([str(d["id"]), *(c["concept"] for c in out["candidates"])])
        return out

    # ------------------------------------------------------------------ forecasts
    def _forecasts(self, families: Sequence[Mapping[str, Any]], iso3: str | None) -> list[dict[str, Any]]:
        port = getattr(self.b, "forecasts", None)
        if port is None:
            return []
        models = port.models()
        out = []
        for f in families[:4]:
            geo = iso3 if iso3 and iso3 in (f.get("countries") or []) else None
            covers = geo is not None or not iso3
            qs = list(port.questions(str(f["key"]), geo, limit=400)) if covers else []
            st = Counter(str(q.get("status") or "?") for q in qs)
            model_ids: Counter[str] = Counter()
            if qs:
                det = port.question(str(qs[0]["question_id"]))
                for fc in (det or {}).get("forecasts") or []:
                    model_ids[str(fc.get("model"))] += 1
            periods = sorted({str(q.get("reference_period")) for q in qs})
            out.append(
                {
                    "family": f["key"],
                    "label": f.get("label_fr") or f["key"],
                    "covers": covers,
                    "geo": geo,
                    "n_questions": len(qs),
                    "statuses": dict(st),
                    "periods": {"first": periods[0], "last": periods[-1]} if periods else None,
                    "models": [{"model": m, "method": (models.get(m) or ("", ""))[1] or None} for m in model_ids],
                }
            )
        return out

    def _verdicts(self, targets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        sb = _scoreboard()
        ids = {str(t["id"]) for t in targets}
        rows = [r for r in sb.get("rows") or [] if str(r.get("target")) in ids]
        headline = set(sb.get("headline_benchmark_kinds") or [])
        verdicts = sb.get("verdicts") or {}
        out = []
        for tid in ids:
            rs = [r for r in rows if r.get("target") == tid]
            if not rs:
                continue
            vs = Counter(str(r.get("verdict")) for r in rs if r.get("benchmark_kind") in headline)
            best = sorted(
                (r for r in rs if r.get("benchmark_kind") in headline),
                key=lambda r: (r.get("verdict") != "bat", r.get("window") != "holdout", r.get("ratio") or 9),
            )[:3]
            out.append(
                {
                    "target": tid,
                    "label": next((t.get("label_fr") for t in targets if t["id"] == tid), tid),
                    "verdicts": {verdicts.get(k, k): n for k, n in vs.items()},
                    "n_rows": len(rs),
                    "examples": [
                        {
                            "id": r.get("id"),
                            "verdict": verdicts.get(str(r.get("verdict")), r.get("verdict")),
                            "candidate": r.get("candidate_label_fr"),
                            "benchmark": r.get("benchmark_label_fr"),
                            "evaluation": r.get("evaluation"),
                            "ratio": r.get("ratio"),
                            "basis": r.get("verdict_basis_fr"),
                            "track": r.get("track"),
                        }
                        for r in best
                    ],
                }
            )
        return out

    # ------------------------------------------------------------------ next steps
    @staticmethod
    def _next(drivers: Sequence[Mapping[str, Any]], forecasts: Sequence[Mapping[str, Any]], iso3: str | None,
              text: str) -> list[dict[str, Any]]:  # fmt: skip
        steps: list[tuple[float, dict[str, Any]]] = []
        loaded = [d for d in drivers if any(s.get("status") == "chargé" for s in d["data"]["series"])]
        for d in drivers:
            ser = d["data"]["series"]
            if not any(s.get("status") == "chargé" for s in ser):
                cand = (d["data"].get("candidates") or [{}])[0].get("concept")
                steps.append(
                    (
                        3 + d["n"] + (2 if d.get("covers_country") else 0),
                        {
                            "kind": "données",
                            "text": f"Relier ou charger une série pour « {d['label']} » ({d['n']} estimation(s) publiée(s) "
                            f"l'attribuent comme pilote)"
                            + (f" ; candidat du catalogue à vérifier : {cand}" if cand else "")
                            + ".",
                            "driver": d["id"],
                        },
                    )
                )
            for s in ser:
                if s.get("status") == "chargé" and s.get("stale"):
                    steps.append(
                        (
                            2 + d["n"] / 2,
                            {
                                "kind": "fraîcheur",
                                "text": f"Rafraîchir {s['concept']} (dernière période chargée {s.get('last_period')}).",
                                "driver": d["id"],
                            },
                        )
                    )
        if not forecasts:
            steps.append(
                (
                    6,
                    {
                        "kind": "registre",
                        "text": f"Aucune famille de questions scellées pour « {text} » : en déclarer une "
                        "(registry/forecasts/question_specs.yaml) pour que REVA soit jugé sur cette cible.",
                    },
                )
            )
        else:
            baseline = all(
                all(re.search(r"persistence|_ar\b|registry_ar|reference_|random|naive|ensemble", m["model"] or "")
                    for m in f["models"])
                for f in forecasts
                if f["models"]
            )  # fmt: skip
            if baseline and loaded:
                names = ", ".join(d["label"] for d in loaded[:3])
                steps.append(
                    (
                        5,
                        {
                            "kind": "modèle",
                            "text": "Les prévisions scellées n'utilisent que des références (persistance, AR, point "
                            f"officiel) : pré-enregistrer un modèle à facteurs avec les pilotes chargés ({names}) "
                            "et le juger contre le prévisionniste officiel.",
                        },
                    )
                )
            if iso3 and not any(f["covers"] for f in forecasts):
                steps.append(
                    (
                        4,
                        {
                            "kind": "registre",
                            "text": f"Les familles trouvées ne couvrent pas {iso3} : étendre leur liste de pays.",
                        },
                    )
                )
        steps.sort(key=lambda x: -x[0])
        return [dict(s, rank=i + 1) for i, (_, s) in enumerate(steps[:6])]

    # ------------------------------------------------------------------ entry point
    def run(self, target: str) -> dict[str, Any]:
        p = parse_target(target, self.catalog)
        text = p["text"]
        fams = self._families(text)
        concepts = self._concepts(text)
        score_targets = self._score_targets(text)
        anchor_labels = [str(f.get("label_fr") or "") for f in fams] + [c.label("fr") for c in concepts]
        eff_targets = self._effect_targets(text, anchor_labels)
        drivers = self._drivers(eff_targets, p["iso3"])
        for d in drivers:
            d["data"] = self._driver_data(d, p["iso3"])
        forecasts = self._forecasts(fams, p["iso3"])
        verdicts = self._verdicts(score_targets)
        target_series = [self._series_status(c.id, p["iso3"]) for c in concepts[:3]]
        claims = self._claims(text)
        nxt = self._next(drivers, forecasts, p["iso3"], text)
        known = bool(fams or concepts or eff_targets or score_targets)
        return {
            "target": p,
            "known": known,
            "anchors": {
                "families": [{"key": f["key"], "label": f.get("label_fr")} for f in fams],
                "concepts": [{"id": c.id, "label": c.label("fr"), "unit": c.unit} for c in concepts],
                "scoreboard": [{"id": t["id"], "label": t.get("label_fr")} for t in score_targets],
                "effect_variables": eff_targets,
            },
            "target_series": target_series,
            "drivers": drivers,
            "claims": claims,
            "forecasts": forecasts,
            "verdicts": verdicts,
            "next_steps": nxt,
            "method": (
                "Recherche dans les registres de REVA (familles de prévisions, catalogue atlas, couche d'effets, "
                "tableau des scores, affirmations causales) et règles explicites ; aucun modèle de langage, aucune "
                "donnée inventée. Les pilotes sont attribués à leurs sources ; les estimations ne sont jamais moyennées."
            ),
            "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        }
