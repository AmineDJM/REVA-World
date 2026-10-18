"""Build ``registry/scoreboard/tracks.yaml`` from the agents' result files and reports.

Copy, never judge: each row's numbers come from a result file (JSON pointer recorded) and its verdict is the
source's own win flag, or a *downgrade* quoted verbatim from the agent's report (never an upgrade). The only
derived numbers are Holm-adjusted p-values over a family that the source lists in full (``p_holm_basis:
computed``); :mod:`reva.v8.scores.registry` re-checks them against the source's own Holm decisions.

    uv run python -m reva.v8.scores.build            # rewrite registry/scoreboard/tracks.yaml
    uv run python -m reva.v8.scores.build --check    # exit 1 if the committed file differs from a rebuild
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml

from reva.v8.scores import catalog as C

REPO = Path(__file__).resolve().parents[4]
OUT = "registry/scoreboard/tracks.yaml"

Row = dict[str, Any]


# --------------------------------------------------------------------------------------------- helpers
class Sources:
    """Reads repository files (relative paths), only those tracked by git unless ``tracked_only`` is off."""

    def __init__(self, root: Path = REPO, tracked_only: bool = True) -> None:
        self.root = root
        self._tracked: set[str] | None = None
        if tracked_only:
            try:
                out = subprocess.run(
                    ["git", "ls-files"],  # noqa: S607 - developer CLI, git from PATH
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                self._tracked = set(out.splitlines())
            except (OSError, subprocess.CalledProcessError):
                self._tracked = None
        self._cache: dict[str, Any] = {}

    def has(self, rel: str | None) -> bool:
        if not rel or not (self.root / rel).is_file():
            return False
        return self._tracked is None or rel in self._tracked

    def text(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def sha(self, rel: str) -> str:
        return hashlib.sha256((self.root / rel).read_bytes()).hexdigest()

    def load(self, rel: str) -> Any:
        if rel not in self._cache:
            raw = self.text(rel)
            self._cache[rel] = json.loads(raw) if rel.endswith(".json") else yaml.safe_load(raw)
        return self._cache[rel]


def esc(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def holm_adjust(ps: Sequence[float | None]) -> list[float]:
    """Holm step-down adjusted p-values (missing p counts as 1, as every pre-registration here declares)."""
    vals = [1.0 if p is None else float(p) for p in ps]
    m = len(vals)
    order = sorted(range(m), key=lambda i: vals[i])
    adj = [1.0] * m
    run = 0.0
    for k, i in enumerate(order):
        run = max(run, min(1.0, (m - k) * vals[i]))
        adj[i] = run
    return adj


def r6(x: float | None) -> float | None:
    return None if x is None else float(f"{x:.6g}")


def source(src: Sources, rel: str, pointer: str, **fields: str | None) -> dict[str, Any]:
    return {"file": rel, "pointer": pointer, "fields": {k: v for k, v in fields.items() if v}, "sha256": src.sha(rel)}


def quote(rel: str, text: str) -> dict[str, str]:
    return {"file": rel, "text": text}


def protocol(src: Sources, rel: str | None) -> dict[str, Any] | None:
    if not rel or not src.has(rel):
        return None
    return {"file": rel, "sha256": src.sha(rel)}


def report_of(src: Sources, tid: str) -> str | None:
    rel = C.track(tid)["report"]
    return rel if src.has(rel) else None


def base_caveats(src: Sources, tid: str, extra: Iterable[str]) -> list[str]:
    out = list(dict.fromkeys(extra))
    if report_of(src, tid) is None and "report_pending" not in out:
        out.append("report_pending")
    return out


# --------------------------------------------------------------------------------------------- BEAT-MACRO
MACRO_TARGET = {
    ("weo_cpi", "cpi_inflation"): "macro.weo_cpi",
    ("g7rt", "cpi_inflation"): "macro.g7_cpi",
    ("g7rt", "unemployment_rate"): "macro.g7_unemployment",
    ("g7rt", "real_gdp_growth"): "macro.g7_gdp",
    ("ecb_hicp", "hicp_inflation"): "macro.ea_hicp",
}
MACRO_BENCH = {
    "benchmark.imf_weo": ("imf_weo", "official_point", "point WEO en vigueur à t"),
    "benchmark.imf_weo_errfan": ("imf_weo", "official_fair_density", "densité équitable autour du point WEO"),
    "benchmark.ecb_spf": ("ecb_spf", "official_point", "point moyen du SPF BCE en vigueur à t"),
    "benchmark.ecb_spf_errfan": ("ecb_spf", "official_fair_density", "densité équitable autour du SPF BCE"),
}
MACRO_CAND = {
    "forecast.bm_stack": ("combination_with_official", "Combinaison officiel + nowcast REVA"),
    "forecast.bm_stack_oil": ("combination_with_official", "Combinaison officiel + nowcast REVA (pétrole)"),
    "forecast.bm_bridge": ("pure_reva", "Nowcast REVA (bridge mensuel)"),
    "forecast.bm_bridge_oil": ("pure_reva", "Nowcast REVA + transmission pétrole attribuée"),
}


def _macro_row(src: Sources, t: dict[str, Any], *, window: str, evaluation: str, rel: str, pointer: str,
               win_field: str, p_holm: float, family: int, holm_rejected: bool, verdict: str,
               basis: str) -> Row:  # fmt: skip
    fid, bkind, blabel = MACRO_BENCH[t["benchmark"]]
    ckind, clabel = MACRO_CAND[t["candidate"]]
    cav = ["timeliness_edge", "lagged_final_inputs"]
    if ckind == "combination_with_official":
        cav.insert(0, "uses_official_forecast")
    if fid == "imf_weo":
        cav.append("same_institution_outcome")
    if evaluation == "development":
        cav.append("development_only")
    if window == "holdout":
        cav.append("targets_seen")
    cov = t.get("cov80_candidate")
    return {
        "id": f"beat_macro.{window}.{t['task']}.{t['variable']}.{t['horizon']}.{t['candidate'].split('.')[-1]}"
        f".{t['benchmark'].split('.')[-1]}.{t['loss']}",
        "track": "beat_macro",
        "window": window,
        "target": MACRO_TARGET[(t["task"], t["variable"])],
        "horizon": t["horizon"],
        "horizon_label_fr": "année en cours (nowcast mensuel)",
        "forecaster": fid,
        "benchmark": t["benchmark"],
        "benchmark_label_fr": blabel,
        "benchmark_kind": bkind,
        "candidate": t["candidate"],
        "candidate_label_fr": clabel,
        "candidate_kind": ckind,
        "evaluation": evaluation,
        "metric": t["loss"],
        "sample": {"n_origins": t.get("n_origins"), "n_pairs": t.get("n_pairs"),
                   "period": "2019-2025" if window == "holdout" else "2008-2018 (BCE 2000-2018)"},
        "ratio": t["relative"],
        "p_raw": t["p_better"],
        "p_holm": r6(p_holm),
        "p_holm_basis": "computed",
        "holm_family_size": family,
        "holm_rejected": holm_rejected,
        "coverage": {"0.8": cov} if cov is not None else None,
        "verdict": verdict,
        "verdict_basis_fr": basis,
        "caveats": base_caveats(src, "beat_macro", cav),
        "source": source(src, rel, pointer, ratio="relative", p="p_better", win=win_field),
    }  # fmt: skip


def beat_macro(src: Sources) -> list[Row]:
    rows: list[Row] = []
    hold_rel, dev_rel = "registry/forecasts/beat_macro/holdout.json", "registry/forecasts/beat_macro/development.json"
    proto = protocol(src, C.track("beat_macro")["protocol"])
    if src.has(hold_rel):
        hold = src.load(hold_rel)
        res = hold["results"]
        adj = holm_adjust([r["p_better"] for r in res])
        for i, (r, a) in enumerate(zip(res, adj, strict=True)):
            ok = bool(r["confirmed"])
            if ok:
                basis = f"confirmé : Holm ✓ (famille {hold['family_size']}), ratio < 1"
            elif r["holm_rejected"]:
                basis = "Holm ✓ mais non confirmé (règle de couverture 80 % des densités)"
            else:
                basis = f"non confirmé : Holm ✗ (p brut {r['p_better']:.3g}, famille {hold['family_size']})"
            rows.append(_macro_row(src, r, window="holdout", evaluation="preregistered_holdout", rel=hold_rel,
                                   pointer=f"/results/{i}", win_field="confirmed", p_holm=a,
                                   family=hold["family_size"], holm_rejected=bool(r["holm_rejected"]),
                                   verdict="bat" if ok else "ne_bat_pas", basis=basis))  # fmt: skip
    if src.has(dev_rel):
        dev = src.load(dev_rel)
        tests = dev["tests"]
        adj = holm_adjust([t["p_better"] for t in tests])
        hyp_rel = "registry/forecasts/beat_macro/hypotheses.yaml"
        hyp_cells = set()
        if src.has(hyp_rel):
            hyp_cells = {(h["task"], h["variable"], h["loss"]) for h in src.load(hyp_rel)["hypotheses"]}
        for i, (t, a) in enumerate(zip(tests, adj, strict=True)):
            if (t["task"], t["variable"], t["loss"]) in hyp_cells or t["holm_rejected"]:
                continue  # selected cells are judged on the holdout rows above
            basis = f"aucune hypothèse retenue pour cette cellule (Holm ✗ en développement, famille {len(tests)})"
            rows.append(_macro_row(src, t, window="dev", evaluation="development", rel=dev_rel,
                                   pointer=f"/tests/{i}", win_field="holm_rejected", p_holm=a, family=len(tests),
                                   holm_rejected=False, verdict="ne_bat_pas", basis=basis))  # fmt: skip
    led_rel = "registry/forecasts/beat_macro/holdout_ledger.yaml"
    if src.has(led_rel):
        led = src.load(led_rel)
        for oi, op in enumerate(led.get("openings") or []):
            for ci, key in enumerate(op.get("confirmed") or []):
                task, var, hz, cand, bench, loss = key.split("|")
                fid, bkind, blabel = MACRO_BENCH[bench]
                ckind, clabel = MACRO_CAND[cand]
                rows.append({
                    "id": f"beat_macro.prospective.{task}.{var}.{hz}.{cand.split('.')[-1]}.{bench.split('.')[-1]}.{loss}",
                    "track": "beat_macro", "window": "prospective", "target": MACRO_TARGET[(task, var)],
                    "horizon": hz, "horizon_label_fr": "année en cours (cibles 2026)", "forecaster": fid,
                    "benchmark": bench, "benchmark_label_fr": blabel, "benchmark_kind": bkind, "candidate": cand,
                    "candidate_label_fr": clabel, "candidate_kind": ckind, "evaluation": "prospective",
                    "metric": loss, "verdict": "en_attente",
                    "verdict_basis_fr": "portée confirmée au holdout : seules ces portées émettent des prévisions "
                    "scellées ; jugement sur les questions résolues du registre",
                    "caveats": base_caveats(src, "beat_macro", ["uses_official_forecast", "timeliness_edge"]),
                    "source": source(src, led_rel, f"/openings/{oi}/confirmed/{ci}"),
                    "resolution": {"month": "2027-10", "note_fr": "cibles 2026 résolues avec le WEO d'octobre 2027",
                                   "quote": quote(C.track("beat_macro")["protocol"],
                                                  "targets 2026 resolve with the October 2027 WEO")},
                    "registry_model": cand,
                })  # fmt: skip
    for r in rows:
        r["report"] = report_of(src, "beat_macro")
        r["protocol"] = proto
    return rows


# --------------------------------------------------------------------------------------------- BEAT-ENERGY
ENERGY_TARGET = {
    ("W1", "BREPUUS"): "energy.brent",
    ("W1", "WTIPUUS"): "energy.wti",
    ("W1", "NGHHUUS"): "energy.henry_hub",
    ("W3", "COPRPUS"): "energy.us_crude_production",
    ("W3", "COSXPUS"): "energy.us_crude_stocks",
}
ENERGY_CAND = {
    "daily_rw": ("pure_reva", "Nowcast REVA (prix quotidiens)"),
    "daily_rw_ln": ("pure_reva", "Nowcast REVA (densité log-normale)"),
    "combo_steo_daily": ("combination_with_official", "Combinaison STEO + prix quotidiens"),
    "steo_bc": ("post_processed_official", "STEO corrigé de son biais passé"),
    "steo_bc_n": ("post_processed_official", "STEO corrigé (densité normale)"),
    "combo_steo_reva": ("combination_with_official", "Combinaison STEO + modèle REVA"),
    "wpsr_nowcast": ("pure_reva", "Nowcast REVA (données hebdomadaires WPSR)"),
}
ENERGY_BENCH = {
    "steo": ("official_point", "point STEO en vigueur à t"),
    "steo_fair_ln": ("official_fair_density", "densité équitable log-normale autour du STEO"),
    "steo_fair_n": ("official_fair_density", "densité équitable normale autour du STEO"),
}
HORIZON_MONTH = {0: "mois en cours", 1: "mois suivant", 2: "dans 2 mois", 3: "dans 3 mois", 6: "dans 6 mois",
                 12: "dans 12 mois"}  # fmt: skip
ENERGY_VOID_QUOTE = "is **void**: its outcome series was redefined"


def beat_energy(src: Sources) -> list[Row]:
    rel = "docs/dev/v7_beat_energy_w3_result.json"
    if not src.has(rel):
        rel = "docs/dev/v7_beat_energy_result.json"
    if not src.has(rel):
        return []
    doc = src.load(rel)
    tests = doc["tests"]
    family = int(doc.get("family_size") or len(tests))
    adj = holm_adjust([t.get("p_challenger_better") for t in tests])
    rep = report_of(src, "beat_energy")
    void_ok = rep is not None and ENERGY_VOID_QUOTE in " ".join(src.text(rep).split())
    base_rel = "docs/dev/v7_beat_energy_result.json"
    covs = doc.get("coverage_80") or (src.load(base_rel).get("coverage_80") if src.has(base_rel) else None)
    rows: list[Row] = []
    for i, (t, a) in enumerate(zip(tests, adj, strict=True)):
        w, code, h = t["window"], t["code"], int(t["h"])
        ckind, clabel = ENERGY_CAND.get(t["challenger"], ("pure_reva", t["challenger"]))
        bkind, blabel = ENERGY_BENCH[t["benchmark"]]
        cav = ["final_vintage_outcome"]
        if ckind != "pure_reva":
            cav.insert(0, "uses_official_forecast")
        if w in ("W1", "W3") and t["challenger"] in ("daily_rw", "daily_rw_ln", "combo_steo_daily", "wpsr_nowcast"):
            cav.append("timeliness_edge")
        if w == "W1":
            cav.append("licence_restricted")
        status = t.get("status")
        if status and "not evaluable" in str(status):
            cav.append("not_evaluable")
        win = bool(t.get("holm_win"))
        verdict, basis, vq = ("bat" if win else "ne_bat_pas"), "", None
        if win:
            basis = f"Holm ✓ (famille {family}), ratio < 1"
        elif status and "not evaluable" in str(status):
            basis = f"non évaluable ({status}) : compte p = 1"
        else:
            basis = f"Holm ✗ (p brut {t['p_challenger_better']:.3g}, famille {family})"
        if win and w == "W2" and code == "PAPR_NONOPEC" and void_ok:
            verdict, basis = "ne_bat_pas", "victoire formelle annulée par le rapport : série de résultat redéfinie"
            vq = quote(rep or "", ENERGY_VOID_QUOTE)
            cav.append("voided_by_report")
        if t.get("n") is not None and int(t["n"]) < 20:
            cav.append("small_n")
        row: Row = {
            "id": f"beat_energy.{w}.{code}.h{h}.{t['challenger']}.{t['benchmark']}.{t['loss']}",
            "track": "beat_energy", "window": w,
            "target": ENERGY_TARGET.get((w, code), "energy.quantities_w2"),
            "series": code, "horizon": f"h{h}", "horizon_label_fr": HORIZON_MONTH.get(h, f"h = {h}"),
            "forecaster": "eia_steo", "benchmark": t["benchmark"], "benchmark_label_fr": blabel,
            "benchmark_kind": bkind, "candidate": t["challenger"], "candidate_label_fr": f"{clabel} — {code}",
            "candidate_kind": ckind, "evaluation": "preregistered_backtest", "metric": t["loss"],
            "sample": {"n_issues": t.get("n"), "period": "STEO 2013-07 … 2026-10"},
            "ratio": t.get("ratio"), "p_raw": t.get("p_challenger_better"), "p_holm": r6(a),
            "p_holm_basis": "computed", "holm_family_size": family, "holm_rejected": win,
            "verdict": verdict, "verdict_basis_fr": basis,
            "caveats": base_caveats(src, "beat_energy", cav),
            "source": source(src, rel, f"/tests/{i}", ratio="ratio", p="p_challenger_better", win="holm_win"),
            "report": rep, "protocol": protocol(src, C.track("beat_energy")["protocol"]),
        }  # fmt: skip
        if vq:
            row["verdict_quote"] = vq
        cov = (covs or {}).get(f"{w}|{code}|{t['challenger']}")
        if cov is not None:
            row["coverage"] = {"0.8": cov}
        rows.append(row)
    return rows


# --------------------------------------------------------------------------------------------- BEAT-HEALTH
HEALTH_FAM = {"nhsn.flu_hosp": ("health.flu_hosp", "cdc_flusight", "FluSight-ensemble"),
              "nhsn.covid_hosp": ("health.covid_hosp", "cdc_covidhub", "CovidHub-ensemble")}  # fmt: skip
HEALTH_CAND = {"beat.qa_blend": ("combination_with_official", "Empilement ensemble + REVA blend"),
               "beat.width_recal": ("post_processed_official", "Ensemble recalibré en largeur"),
               "reva.blend_alone": ("pure_reva", "REVA health.blend seul")}  # fmt: skip
HEALTH_QUOTE = {
    ("nhsn.flu_hosp", "beat.qa_blend"): ("ne_bat_pas", "1.034 (0.089) — loss"),
    ("nhsn.flu_hosp", "beat.width_recal"): ("egalite", "0.998 (0.94) — tie"),
    ("nhsn.covid_hosp", "beat.qa_blend"): ("egalite", "1.004 (0.35) — tie"),
    ("nhsn.covid_hosp", "beat.width_recal"): ("egalite", "1.006 (0.30) — tie"),
    ("nhsn.flu_hosp", "reva.blend_alone"): ("ne_bat_pas", "1.137 (0.026) — loss"),
    ("nhsn.covid_hosp", "reva.blend_alone"): ("ne_bat_pas", "1.241 (7e-6) — loss"),
}


def _health_base(fam: str, cand: str) -> Row:
    target, fid, ens = HEALTH_FAM[fam]
    ckind, clabel = HEALTH_CAND[cand]
    return {"track": "beat_health", "target": target, "horizon": "0-3", "horizon_label_fr": "0 à 3 semaines",
            "forecaster": fid, "benchmark": ens, "benchmark_label_fr": f"{ens} du même tour (attribué)",
            "benchmark_kind": "official_ensemble", "candidate": cand, "candidate_label_fr": clabel,
            "candidate_kind": ckind, "metric": "wis"}  # fmt: skip


def beat_health(src: Sources) -> list[Row]:
    rel, prel = "registry/forecasts/beat_health/dev_result.json", C.track("beat_health")["protocol"]
    rep = report_of(src, "beat_health")
    proto = protocol(src, prel)
    rows: list[Row] = []
    if src.has(rel):
        doc = src.load(rel)
        verd = doc["verdicts"]
        for key, v in verd["tests"].items():
            _, fam, cand = key.split("|")
            word, q = HEALTH_QUOTE[(fam, cand)]
            m = doc["families"][fam]["comparisons"]["dev"]["final"]
            mm, ens = m["models"][cand], m["ensemble"]
            cav = ["uses_official_forecast", "development_only", "attribution_pending"]
            rows.append({
                **_health_base(fam, cand), "id": f"beat_health.dev.{fam}.{cand}", "window": "dev",
                "evaluation": "development",
                "sample": {"n_rounds": mm.get("dm_vs_ensemble", {}).get("n"), "period": "tours V6 2024-11 … 2026-10"},
                "ratio": v["relative_wis"], "p_raw": v["p_value"], "p_holm": v["holm_p"], "p_holm_basis": "source",
                "holm_family_size": verd["family_size"], "holm_rejected": bool(v["win"]),
                "coverage": {"0.5": mm.get("coverage50"), "0.9": mm.get("coverage90")},
                "benchmark_coverage": {"0.5": ens.get("coverage50"), "0.9": ens.get("coverage90")},
                "by_horizon": mm.get("by_horizon_relative_wis"),
                "verdict": word, "verdict_basis_fr": f"rapport : « {q} » (dev, Holm ✗, famille {verd['family_size']})",
                "verdict_quote": quote(rep or C.track("beat_health")["report"], q),
                "caveats": base_caveats(src, "beat_health", cav),
                "source": source(src, rel, f"/verdicts/tests/{esc(key)}", ratio="relative_wis", p="p_value", win="win"),
            })  # fmt: skip
        for fam in HEALTH_FAM:
            m = doc["families"][fam]["comparisons"]["dev"]["final"]
            mm = m["models"].get("reva.blend_alone")
            if not mm:
                continue
            word, q = HEALTH_QUOTE[(fam, "reva.blend_alone")]
            rows.append({
                **_health_base(fam, "reva.blend_alone"), "id": f"beat_health.dev.{fam}.reva.blend_alone",
                "window": "dev", "evaluation": "development",
                "sample": {"n_rounds": mm["dm_vs_ensemble"].get("n"), "period": "tours V6 2024-11 … 2026-10"},
                "ratio": mm["relative_wis_vs_ensemble"], "p_raw": mm["dm_vs_ensemble"]["p_value"],
                "p_holm": None, "p_holm_basis": None, "holm_family_size": None, "holm_rejected": None,
                "coverage": {"0.5": mm.get("coverage50"), "0.9": mm.get("coverage90")},
                "benchmark_coverage": {"0.5": m["ensemble"].get("coverage50"), "0.9": m["ensemble"].get("coverage90")},
                "by_horizon": mm.get("by_horizon_relative_wis"),
                "verdict": word, "verdict_basis_fr": f"rapport : « {q} » (hors famille Holm, informatif)",
                "verdict_quote": quote(rep or C.track("beat_health")["report"], q),
                "caveats": base_caveats(src, "beat_health", ["development_only", "attribution_pending"]),
                "source": source(src, rel, f"/families/{fam}/comparisons/dev/final/models/reva.blend_alone",
                                 ratio="relative_wis_vs_ensemble", p="dm_vs_ensemble/p_value"),
            })  # fmt: skip
    if prel and src.has(prel):
        p = src.load(prel)
        fam_list = p["decision_rule"]["family"]
        h1 = p["holdouts"]["H1"]
        for i, key in enumerate(fam_list):
            hz, fam, cand = key.split("|")
            if hz == "H1":
                res = {
                    "date": str(h1["evaluate_not_before"]),
                    "note_fr": "lecture unique du holdout H1 (saison 2026-27) au plus tôt à cette date",
                    "quote": quote(prel, f'evaluate_not_before: "{h1["evaluate_not_before"]}"'),
                }
            else:
                res = {
                    "note_fr": "ingestion FluSight 2023-24 en attente (disque) ; sans vintages archivés, "
                    "les 2 tests H2 comptent p = 1",
                    "quote": quote(C.track("beat_health")["report"], "its 2 tests count as p = 1"),
                }
            rows.append({
                **_health_base(fam, cand), "id": f"beat_health.{hz}.{fam}.{cand}", "window": hz,
                "evaluation": "preregistered_holdout", "verdict": "en_attente",
                "verdict_basis_fr": f"holdout {hz} pré-enregistré, famille Holm de {len(fam_list)} tests",
                "holm_family_size": len(fam_list),
                "caveats": base_caveats(src, "beat_health", ["uses_official_forecast", "attribution_pending"]),
                "source": source(src, prel, f"/decision_rule/family/{i}"), "resolution": res,
            })  # fmt: skip
    for r in rows:
        r["report"], r["protocol"] = rep, proto
    return rows


# --------------------------------------------------------------------------------------------- BEAT-CONFLICT
CONFLICT_METRIC_TARGET = {"brier": "conflict.cm_conflict25", "logloss": "conflict.cm_conflict25",
                          "crps_log1p": "conflict.cm_deaths", "msle": "conflict.cm_deaths"}  # fmt: skip
CONFLICT_MODEL = {
    "forecast.cm_hurdle": ("pure_reva", "Modèle REVA pays-mois (hurdle)"),
    "forecast.cm_stack_views": ("combination_with_official", "Combinaison ViEWS + modèle REVA"),
}


def beat_conflict(src: Sources) -> list[Row]:
    prel, rel = C.track("beat_conflict")["protocol"], "docs/dev/v7_beat_conflict_result.json"
    if not src.has(prel):
        return []
    p = src.load(prel)
    rep = report_of(src, "beat_conflict")
    res_doc = src.load(rel) if src.has(rel) else None
    rows: list[Row] = []
    for i, key in enumerate(p["family"]):
        win_, a, _vs, b, metric, steps = key.split("|")
        views = b == "forecast.reference_views"
        ckind, clabel = CONFLICT_MODEL[a]
        base: Row = {
            "track": "beat_conflict", "window": win_, "target": CONFLICT_METRIC_TARGET[metric],
            "horizon": steps, "horizon_label_fr": f"{steps} mois", "forecaster": "views" if views else None,
            "benchmark": b,
            "benchmark_label_fr": "ViEWS, run en vigueur au scellement (attribué)" if views
            else "persistence (même intensité, transitions passées)",
            "benchmark_kind": "official_point" if views else "naive", "candidate": a, "candidate_label_fr": clabel,
            "candidate_kind": ckind, "metric": metric, "holm_family_size": len(p["family"]),
        }  # fmt: skip
        if win_ == "W3":
            cav = ["mixed_vintage_outcome"] + (["uses_official_forecast"] if ckind != "pure_reva" else [])
            rows.append({
                **base, "id": f"beat_conflict.W3.{a.split('.')[-1]}.{b.split('.')[-1]}.{metric}.{steps}",
                "evaluation": "prospective", "verdict": "en_attente",
                "verdict_basis_fr": "prévisions scellées au registre ; jugées quand UCDP Candidate couvre les mois cibles",
                "caveats": base_caveats(src, "beat_conflict", cav),
                "source": source(src, prel, f"/family/{i}"),
                "resolution": {"not_before": "2027-04-30",
                               "note_fr": "mois cibles 2026-11 … 2027-04 ; issue = première version UCDP Candidate "
                               "couvrant le mois cible",
                               "quote": quote(prel, "the first atlas.ucdp_ged.cand_country_month version registered after the seal")},
                "registry_model": a,
            })  # fmt: skip
            continue
        if res_doc is None or key not in res_doc.get("family", {}):
            continue
        t = res_doc["family"][key]
        win = bool(t.get("win"))
        rows.append({
            **base, "id": f"beat_conflict.W1.{a.split('.')[-1]}.{b.split('.')[-1]}.{metric}.{steps}",
            "evaluation": "preregistered_backtest",
            "sample": {"n": t.get("n"), "n_countries": t.get("n_clusters"), "period": "origines 2012-01 … 2021-12"},
            "ratio": t.get("ratio"), "p_raw": t.get("p_one_sided_t"), "p_holm": t.get("p_holm"),
            "p_holm_basis": "source", "holm_rejected": win,
            "verdict": "bat" if win else "ne_bat_pas",
            "verdict_basis_fr": f"Holm ✓ (famille {len(p['family'])}), ratio < 1" if win
            else f"Holm ✗ (famille {len(p['family'])})",
            "caveats": base_caveats(src, "beat_conflict", ["final_vintage_inputs", "final_vintage_outcome"]),
            "source": source(src, rel, f"/family/{esc(key)}", ratio="ratio", p="p_one_sided_t", win="win"),
        })  # fmt: skip
    vq = "REVA's point losses are lower than VIEWS' on every metric, but **no difference is significant**"
    if rep and vq in " ".join(src.text(rep).split()):
        rows.append({
            "id": "beat_conflict.V.cm_hurdle.reference_views.informational", "track": "beat_conflict", "window": "V",
            "target": "conflict.cm_deaths", "horizon": "1-12", "horizon_label_fr": "1 à 12 mois", "forecaster": "views",
            "benchmark": "forecast.reference_views", "benchmark_label_fr": "ViEWS, runs chargés en V6 (attribué)",
            "benchmark_kind": "official_point", "candidate": "forecast.cm_hurdle",
            "candidate_label_fr": "Modèle REVA pays-mois (hurdle)", "candidate_kind": "pure_reva",
            "evaluation": "development", "metric": "msle", "verdict": "ne_bat_pas",
            "verdict_basis_fr": "fenêtre informative déjà vue (hors famille Holm) : aucune différence significative",
            "verdict_quote": quote(rep, vq),
            "caveats": base_caveats(src, "beat_conflict", ["development_only", "information_tilt", "final_vintage_inputs"]),
            "source": {"file": rep, "quote": vq, "sha256": src.sha(rep)},
        })  # fmt: skip
    for r in rows:
        r["report"], r["protocol"] = rep, protocol(src, prel)
    return rows


# --------------------------------------------------------------------------------------------- BEAT-NOWCAST
NOWCAST_CELL = {"gdp_gdpnow": "nowcast.us_gdp", "gdp_nyfed": "nowcast.us_gdp", "cpi_clev": "nowcast.us_inflation"}
NOWCAST_BENCH = {
    "benchmark.atl_gdpnow": ("atlanta_gdpnow", "official_point", "GDPNow publié à la même date"),
    "benchmark.atl_gdpnow_errfan": ("atlanta_gdpnow", "official_fair_density", "densité équitable autour de GDPNow"),
    "benchmark.nyfed_nowcast": ("nyfed_nowcast", "official_point", "NY Fed Staff Nowcast à la même date"),
    "benchmark.nyfed_nowcast_errfan": ("nyfed_nowcast", "official_fair_density", "densité équitable autour du NY Fed"),
    "benchmark.clev_nowcast": ("cleveland_nowcast", "official_point", "nowcast IPC de la Fed de Cleveland, même date"),
}
NOWCAST_CAND = {
    "forecast.nc_stack_reva": "Combinaison nowcast officiel + nowcast REVA",
    "forecast.nc_stack_all": "Combinaison des nowcasts officiels + REVA",
    "forecast.nc_biascorr": "Nowcast officiel corrigé de son biais passé",
}


def beat_nowcast(src: Sources) -> list[Row]:
    rel = "registry/forecasts/beat_nowcast/holdout.json"
    if not src.has(rel):
        return []
    d = src.load(rel)
    res = d["results"]
    fam = int(d.get("family_size") or len(res))
    adj = holm_adjust([r["p_better"] for r in res])
    rows: list[Row] = []
    for i, (r, a) in enumerate(zip(res, adj, strict=True)):
        fid, bkind, blabel = NOWCAST_BENCH[r["benchmark"]]
        uses = bool(r.get("uses_official_forecast"))
        ckind = "combination_with_official" if uses else "pure_reva"
        if "biascorr" in r["candidate"]:
            ckind = "post_processed_official"
        ok = bool(r["confirmed"])
        if ok:
            basis = f"confirmé : Holm ✓ (famille {fam}), ratio < 1"
        elif r["holm_rejected"]:
            basis = "Holm ✓ mais non confirmé (règle de couverture 80 % des densités)"
        else:
            basis = f"non confirmé : Holm ✗ (p brut {r['p_better']:.3g}, famille {fam})"
        cav = (["uses_official_forecast"] if uses else []) + ["timeliness_edge"]
        if (r.get("n_origins") or 0) < 20:
            cav.append("small_n")
        cov = r.get("cov80_candidate")
        rows.append({
            "id": f"beat_nowcast.holdout.{r['cell']}.{r['candidate'].split('.')[-1]}.{r['benchmark'].split('.')[-1]}.{r['loss']}",
            "track": "beat_nowcast", "window": "holdout", "target": NOWCAST_CELL[r["cell"]],
            "horizon": "nowcast", "horizon_label_fr": "trimestre / mois en cours", "forecaster": fid,
            "benchmark": r["benchmark"], "benchmark_label_fr": blabel, "benchmark_kind": bkind,
            "candidate": r["candidate"], "candidate_label_fr": NOWCAST_CAND.get(r["candidate"], r["candidate"]),
            "candidate_kind": ckind, "evaluation": "preregistered_holdout", "metric": r["loss"],
            "sample": {"n_origins": r.get("n_origins"), "n_pairs": r.get("n_pairs")},
            "ratio": r["relative"], "p_raw": r["p_better"], "p_holm": r6(a), "p_holm_basis": "computed",
            "holm_family_size": fam, "holm_rejected": bool(r["holm_rejected"]),
            "coverage": {"0.8": cov} if cov is not None else None,
            "verdict": "bat" if ok else "ne_bat_pas", "verdict_basis_fr": basis,
            "caveats": base_caveats(src, "beat_nowcast", cav),
            "source": source(src, rel, f"/results/{i}", ratio="relative", p="p_better", win="confirmed"),
            "report": report_of(src, "beat_nowcast"),
            "protocol": protocol(src, C.track("beat_nowcast")["protocol"]),
        })  # fmt: skip
    return rows


# --------------------------------------------------------------------------------------------- PRED-MODELS (V6)
PM_TARGET = {("weo", "real_gdp_growth"): "macro.weo_gdp", ("weo", "cpi_inflation"): "macro.weo_cpi",
             ("weo", "unemployment_rate"): "macro.weo_unemployment", ("us", "real_gdp_growth"): "macro.us_gdp",
             ("us", "unemployment_rate"): "macro.us_unemployment", ("us", "cpi_inflation"): "macro.us_inflation"}  # fmt: skip
PM_HZ = {"h0": "année en cours", "h1": "année suivante", "spf2": "trimestre en cours", "spf3": "trimestre suivant",
         "spf4": "dans 2 trimestres", "spf5": "dans 3 trimestres", "spf6": "dans 4 trimestres"}  # fmt: skip
PM_CAND = {
    "forecast.weo_conformal": ("post_processed_official", "Densité conforme autour du point WEO"),
    "forecast.weo_bias_corrected": ("post_processed_official", "WEO corrigé du biais (densité)"),
    "forecast.spf_conformal": ("post_processed_official", "Densité conforme autour de la médiane SPF"),
    "forecast.direct_ar": ("pure_reva", "AR direct REVA"),
    "forecast.bvar_minnesota": ("pure_reva", "BVAR Minnesota REVA"),
    "forecast.pool_bvar_ar": ("pure_reva", "Combinaison BVAR + AR REVA"),
}
PM_POINTS_QUOTE = (
    "holdout AE ratios vs SPF: GDP 0.98–1.86 (none significantly below 1), unemployment 1.20–6.67, "
    "inflation 1.02–1.23; vs WEO GDP 1.03–2.24"
)


def pred_models(src: Sources) -> list[Row]:
    rel = "registry/forecasts/pred_models/holdout.json"
    rep, proto = report_of(src, "pred_models"), protocol(src, C.track("pred_models")["protocol"])
    rows: list[Row] = []
    if src.has(rel):
        hyps = src.load(rel)["hypotheses"]
        adj = holm_adjust([h["holdout"]["p_better"] for h in hyps])
        for i, (h, a) in enumerate(zip(hyps, adj, strict=True)):
            o = h["holdout"]
            bench = h["benchmark"]
            if bench == "benchmark.persistence":
                fid, bkind, blabel = None, "naive", "persistance (même vintage)"
            else:
                fid = "imf_weo" if h["task"] == "weo" else "us_spf"
                bkind = "weak_reference"
                blabel = f"N(point {'WEO' if fid == 'imf_weo' else 'SPF'}, écart de la persistance) — repère faible"
            ckind, clabel = PM_CAND[h["candidate"]]
            cav: list[str] = []
            if bkind == "weak_reference":
                cav += ["weak_benchmark"]
            if ckind == "post_processed_official":
                cav += ["not_a_better_point", "uses_official_forecast"]
            if h["task"] == "weo":
                cav += ["outcome_vintage_mismatch", "same_institution_outcome"]
            ok = bool(h["confirmed"])
            if ok:
                basis = "confirmé : Holm ✓ (30 hypothèses) et couverture 80 % dans la bande"
            elif h["holm_rejected"]:
                basis = "Holm ✓ mais calibration hors bande : non confirmé"
            else:
                basis = f"non confirmé : Holm ✗ (p brut {o['p_better']:.3g}, 30 hypothèses)"
            rows.append({
                "id": f"pred_models.holdout.{h['task']}.{h['variable']}.{h['horizon']}.{h['candidate'].split('.')[-1]}"
                f".{bench.split('.')[-1]}",
                "track": "pred_models", "window": "holdout", "target": PM_TARGET[(h["task"], h["variable"])],
                "horizon": h["horizon"], "horizon_label_fr": PM_HZ.get(h["horizon"], h["horizon"]),
                "forecaster": fid, "benchmark": bench, "benchmark_label_fr": blabel, "benchmark_kind": bkind,
                "candidate": h["candidate"], "candidate_label_fr": clabel, "candidate_kind": ckind,
                "evaluation": "preregistered_holdout", "metric": h["loss"],
                "sample": {"n_origins": o.get("n_origins"), "n_pairs": o.get("n_pairs"),
                           "period": "WEO 2019-2025" if h["task"] == "weo" else "SPF 2013-2026"},
                "ratio": o["relative"], "p_raw": o["p_better"], "p_holm": r6(a), "p_holm_basis": "computed",
                "holm_family_size": len(hyps), "holm_rejected": bool(h["holm_rejected"]),
                "coverage": {"0.8": h.get("holdout_coverage80")} if h.get("holdout_coverage80") is not None else None,
                "verdict": "bat" if ok else "ne_bat_pas", "verdict_basis_fr": basis,
                "caveats": base_caveats(src, "pred_models", cav),
                "source": source(src, rel, f"/hypotheses/{i}", ratio="holdout/relative", p="holdout/p_better",
                                 win="confirmed"),
            })  # fmt: skip
    review = "docs/dev/review_v6_predictions.md"
    if src.has(review):
        q = quote(review, PM_POINTS_QUOTE)
        for fid, target, rng, label in (
            ("us_spf", "macro.us_gdp", [0.98, 1.86], "PIB"),
            ("us_spf", "macro.us_unemployment", [1.20, 6.67], "chômage"),
            ("us_spf", "macro.us_inflation", [1.02, 1.23], "inflation"),
            ("imf_weo", "macro.weo_gdp", [1.03, 2.24], "PIB"),
        ):
            rows.append({
                "id": f"pred_models.holdout_points.{fid}.{target}", "track": "pred_models", "window": "holdout",
                "target": target, "horizon": "tous", "horizon_label_fr": "tous horizons du holdout",
                "forecaster": fid, "benchmark": "benchmark.imf_weo" if fid == "imf_weo" else "benchmark.spf_median",
                "benchmark_label_fr": "point officiel (WEO / médiane SPF)", "benchmark_kind": "official_point",
                "candidate": "reva_models", "candidate_label_fr": f"Modèles REVA (AR direct, BVAR, DFM, combinaisons) — {label}",
                "candidate_kind": "pure_reva", "evaluation": "preregistered_holdout", "metric": "ae",
                "ratio_range": rng, "verdict": "ne_bat_pas",
                "verdict_basis_fr": "QA-PRED : aucun ratio significativement < 1 sur le holdout verrouillé",
                "verdict_quote": q,
                "caveats": base_caveats(src, "pred_models", ["same_institution_outcome"] if fid == "imf_weo" else []),
                "source": {"file": review, "quote": PM_POINTS_QUOTE, "sha256": src.sha(review)},
            })  # fmt: skip
    for r in rows:
        r["report"], r["protocol"] = rep, proto
    return rows


def fix7_fair(src: Sources) -> list[Row]:
    rel = "registry/forecasts/pred_models/registered_models.yaml"
    if not src.has(rel):
        return []
    rep = report_of(src, "fix7_fair")
    rows: list[Row] = []
    for i, r in enumerate(src.load(rel)["registered"]):
        dev = r.get("development_vs_fair_benchmark")
        if not dev:
            continue
        fid = "imf_weo" if r["task"] == "weo" else "us_spf"
        ckind, clabel = PM_CAND[r["candidate"]]
        target = PM_TARGET[(r["task"], r["variable"])]
        common = {"track": "fix7_fair", "target": target, "horizon": r["horizon"],
                  "horizon_label_fr": PM_HZ.get(r["horizon"], r["horizon"]), "forecaster": fid,
                  "benchmark_kind": "official_fair_density", "candidate": r["candidate"], "candidate_label_fr": clabel,
                  "candidate_kind": ckind, "metric": r["loss"], "report": rep, "protocol": None}  # fmt: skip
        rows.append({
            **common, "id": f"fix7_fair.dev.{r['task']}.{r['variable']}.{r['horizon']}.{r['candidate'].split('.')[-1]}",
            "window": "dev", "evaluation": "development",
            "benchmark": "benchmark.imf_weo_errfan" if fid == "imf_weo" else "benchmark.spf_errfan",
            "benchmark_label_fr": "N(point officiel, RMS de ses propres erreurs passées)",
            "sample": {"n_origins": dev.get("n_origins")}, "ratio": dev["crps_ratio"], "p_raw": dev["p_better"],
            "p_holm": None, "p_holm_basis": None, "holm_rejected": bool(dev["holm_rejected"]),
            "verdict": "ne_bat_pas",
            "verdict_basis_fr": "Holm ✗ contre le repère équitable (développement seulement, holdout dépensé)",
            "caveats": base_caveats(src, "fix7_fair", ["development_only", "not_a_better_point", "uses_official_forecast"]),
            "source": source(src, rel, f"/registered/{i}/development_vs_fair_benchmark", ratio="crps_ratio",
                             p="p_better", win="holm_rejected"),
        })  # fmt: skip
        if r["task"] == "weo":
            rows.append({
                **common, "id": f"fix7_fair.prospective.{r['variable']}.{r['horizon']}.{r['candidate'].split('.')[-1]}",
                "window": "prospective", "evaluation": "prospective", "benchmark": "forecast.weo_error_fan",
                "benchmark_label_fr": "forecast.weo_error_fan scellé sur les mêmes questions",
                "verdict": "en_attente",
                "verdict_basis_fr": "mise à l'épreuve : ≥ 30 questions résolues en commun, CRPS, DM unilatéral",
                "caveats": base_caveats(src, "fix7_fair", ["not_a_better_point", "uses_official_forecast",
                                                          "outcome_vintage_mismatch", "awaiting_resolutions"]),
                "source": source(src, rel, f"/registered/{i}/issuance"),
                "resolution": {"month": "2027-04", "note_fr": "premières issues weo.gdp_growth h0 avec le WEO d'avril 2027",
                               "quote": quote("docs/dev/v6_fix7.md", "outcomes arrive with the April 2027 WEO")},
                "registry_model": "forecast.weo_error_fan",
            })  # fmt: skip
    return rows


def pred_energy_v6(src: Sources) -> list[Row]:
    review = "docs/dev/review_v6_predictions.md"
    text = "vs STEO on squared error: 1 raw p < 0.05 of 334 tests (COPR_WORLD h1, n = 8)"
    if not src.has(review):
        return []
    return [{
        "id": "pred_energy_v6.points.steo", "track": "pred_energy_v6", "window": "backtest",
        "target": "energy.steo_v6", "horizon": "h1-12", "horizon_label_fr": "1 à 12 mois", "forecaster": "eia_steo",
        "benchmark": "steo", "benchmark_label_fr": "point STEO (attribué)", "benchmark_kind": "official_point",
        "candidate": "reva_energy_models", "candidate_label_fr": "Modèles énergie REVA (AR, ARDL, inventaires, moyennes)",
        "candidate_kind": "pure_reva", "evaluation": "development", "metric": "ae",
        "sample": {"n_tests": 334, "period": "STEO 2013-07 … 2026-10"}, "verdict": "ne_bat_pas",
        "verdict_basis_fr": "QA-PRED : « Any REVA model beats EIA STEO » jugé invalide",
        "verdict_quote": quote(review, text),
        "caveats": base_caveats(src, "pred_energy_v6", ["in_sample_selection", "final_vintage_outcome"]),
        "source": {"file": review, "quote": text, "sha256": src.sha(review)},
        "report": report_of(src, "pred_energy_v6"), "protocol": None,
    }]  # fmt: skip


def ensemble(src: Sources) -> list[Row]:
    rep = report_of(src, "ensemble")
    if rep is None:
        return []
    q = "127 scoreboard cells all `awaiting_resolutions`"
    if q not in " ".join(src.text(rep).split()):
        q = "awaiting_resolutions"
    out = []
    for fid, target, label in (("imf_weo", "macro.registry_weo", "FMI WEO"),
                               ("eia_steo", "energy.registry_steo", "EIA STEO"),
                               ("views", "conflict.registry_views", "ViEWS")):  # fmt: skip
        out.append({
            "id": f"ensemble.live.{fid}", "track": "ensemble", "window": "live", "target": target,
            "horizon": "registre", "horizon_label_fr": "horizons des questions scellées", "forecaster": fid,
            "benchmark": f"forecast.reference_{fid}", "benchmark_label_fr": f"{label} attribué, scellé sur les mêmes questions",
            "benchmark_kind": "official_point", "candidate": "reva.ensemble", "candidate_label_fr": "Ensemble REVA (en ligne)",
            "candidate_kind": "reva_ensemble", "evaluation": "prospective", "metric": "mixed", "verdict": "en_attente",
            "verdict_basis_fr": "tableau en direct /v1/forecasts/ensemble : aucune cellule testée tant que < 20 paires résolues",
            "caveats": ["uses_official_forecast", "awaiting_resolutions"],
            "source": {"file": rep, "quote": q, "sha256": src.sha(rep)},
            "resolution": {"note_fr": "au moins 20 questions résolues en commun par cellule (règle du tableau)",
                           "quote": quote(rep, "scored on ≥ max(20, 50 %)")},
            "registry_model": "reva.ensemble", "report": rep, "protocol": None,
        })  # fmt: skip
    return out


EVENTS: list[dict[str, Any]] = [
    {"id": "event.first_prospective_nhsn", "date": "2026-10-22", "label_fr": "Premières résolutions prospectives (NHSN)",
     "quote": quote("docs/dev/review_v6_predictions.md",
                    "The first honest prospective numbers arrive from 2026-10-22 (NHSN) and mid-December 2026 (FFPI, HICP, Brent)")},
    {"id": "event.first_prospective_dec", "month": "2026-12", "label_fr": "Mi-décembre : FFPI, IPCH, Brent",
     "quote": quote("docs/dev/review_v6_predictions.md", "mid-December 2026 (FFPI, HICP, Brent)")},
    {"id": "event.energy_git_record", "month": "2026-11",
     "label_fr": "Nowcast Brent/WTI d'octobre 2026 (enregistrement git, non scellé) : se résout avec le STEO de novembre",
     "quote": quote("docs/dev/v7_beat_energy_prospective_2026-10.json", "resolves with the November 2026 STEO")},
]  # fmt: skip

GENERATORS: tuple[Callable[[Sources], list[Row]], ...] = (
    beat_macro,
    beat_energy,
    beat_health,
    beat_conflict,
    beat_nowcast,
    pred_models,
    fix7_fair,
    pred_energy_v6,
    ensemble,
)


def build(src: Sources | None = None) -> dict[str, Any]:
    src = src or Sources()
    rows: list[Row] = []
    for g in GENERATORS:
        rows.extend(g(src))
    files = sorted(
        {r["source"]["file"] for r in rows}
        | {str(p["file"]) for r in rows if (p := r.get("protocol")) and p.get("file")}
    )
    tracks = []
    for t in C.TRACKS:
        tracks.append({**t, "report": t["report"] if src.has(t["report"]) else None,
                       "report_expected": t["report"],
                       "protocol": protocol(src, t["protocol"]),
                       "files": [{"file": f, "sha256": src.sha(f)} for f in t["files"] if src.has(f)]})  # fmt: skip
    return {
        "schema": C.SCHEMA,
        "statement": "Generated by reva.v8.scores.build from result files and reports; never edited by hand. "
        "Verdicts are copied (or downgraded with a verbatim quote), never upgraded; checked by "
        "reva.v8.scores.registry.validate.",
        "sources": [{"file": f, "sha256": src.sha(f)} for f in files],
        "verdicts": C.VERDICT_FR,
        "evaluations": C.EVALUATIONS,
        "benchmark_kinds": C.BENCHMARK_KINDS,
        "headline_benchmark_kinds": list(C.HEADLINE_BENCHMARKS),
        "candidate_kinds": C.CANDIDATE_KINDS,
        "metrics": C.METRICS,
        "caveats": C.CAVEATS,
        "forecasters": C.FORECASTERS,
        "domains": C.DOMAINS,
        "targets": [{"id": k, **v} for k, v in C.TARGETS.items()],
        "tracks": tracks,
        "no_track": C.NO_TRACK,
        "events": [e for e in EVENTS if src.has(e["quote"]["file"])],
        "rows": rows,
    }


def dump(doc: dict[str, Any]) -> str:
    head = "# GENERATED by `uv run python -m reva.v8.scores.build` — do not edit by hand (see registry.validate).\n"
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file differs from a rebuild")
    ap.add_argument("--all-files", action="store_true", help="also read files git does not track (preview only)")
    a = ap.parse_args(argv)
    text = dump(build(Sources(tracked_only=not a.all_files)))
    out = REPO / OUT
    if a.check:
        same = out.is_file() and out.read_text(encoding="utf-8") == text
        print("up to date" if same else f"{OUT} differs from a rebuild")
        return 0 if same else 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    from reva.v8.scores.registry import headline, validate

    doc = yaml.safe_load(text)
    problems = validate(doc, REPO)
    print(f"wrote {OUT}: {len(doc['rows'])} rows; headline {headline(doc['rows'])}; problems {len(problems)}")
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
