"""Curated vocabulary of the V8 scoreboard: official forecasters (columns), targets (rows), tracks, caveats.

Only labels and pointers live here. Every number and every verdict in ``registry/scoreboard/tracks.yaml`` is copied
by :mod:`reva.v8.scores.build` from a result file or quoted verbatim from a report; :mod:`reva.v8.scores.registry`
re-checks each one against its source.
"""

from __future__ import annotations

from typing import Any

SCHEMA = "reva-v8-scoreboard/1"

VERDICTS = ("bat", "ne_bat_pas", "egalite", "en_attente")
VERDICT_FR = {"bat": "bat", "ne_bat_pas": "ne bat pas", "egalite": "égalité", "en_attente": "en attente"}

EVALUATIONS = {
    "development": "développement (données de sélection, pas une preuve)",
    "preregistered_holdout": "holdout pré-enregistré (ouvert une seule fois)",
    "preregistered_backtest": "évaluation pré-enregistrée sur vintages archivés",
    "prospective": "prospectif (prévisions scellées avant l'issue)",
}

# What the comparison is measured against. Only the first three count in the headline "REVA vs officiels".
BENCHMARK_KINDS = {
    "official_point": "point officiel publié (attribué)",
    "official_fair_density": "densité équitable autour du point officiel (ses propres erreurs passées)",
    "official_ensemble": "ensemble officiel publié (attribué)",
    "weak_reference": "repère faible construit par REVA autour du point officiel (QA-PRED X1)",
    "naive": "référence naïve (persistance, marche aléatoire)",
}
HEADLINE_BENCHMARKS = ("official_point", "official_fair_density", "official_ensemble")

CANDIDATE_KINDS = {
    "pure_reva": "REVA seul (aucune prévision officielle en entrée)",
    "combination_with_official": "combinaison REVA + prévision officielle",
    "post_processed_official": "prévision officielle corrigée/recalibrée par REVA",
    "reva_ensemble": "ensemble REVA (combine aussi les officiels)",
}

METRICS = {
    "ae": "erreur absolue (points)",
    "crps": "CRPS (densités)",
    "wis": "WIS (quantiles)",
    "brier": "score de Brier (probabilités)",
    "logloss": "log loss (probabilités)",
    "msle": "erreur quadratique log (points)",
    "crps_log1p": "CRPS sur log(1+décès)",
    "mixed": "selon la cellule (AE, CRPS, intervalle, Brier)",
}

FORECASTERS: list[dict[str, Any]] = [
    {"id": "imf_weo", "label_fr": "FMI — WEO", "short": "FMI", "org": "Fonds monétaire international",
     "product": "World Economic Outlook (avril / octobre)", "domain": "macro", "status": "active"},
    {"id": "us_spf", "label_fr": "SPF (Fed de Philadelphie)", "short": "SPF US", "org": "Federal Reserve Bank of Philadelphia",
     "product": "Survey of Professional Forecasters (trimestriel)", "domain": "macro", "status": "active"},
    {"id": "ecb_spf", "label_fr": "SPF BCE", "short": "SPF BCE", "org": "Banque centrale européenne",
     "product": "ECB Survey of Professional Forecasters (trimestriel)", "domain": "macro", "status": "active"},
    {"id": "eia_steo", "label_fr": "EIA — STEO", "short": "EIA", "org": "U.S. Energy Information Administration",
     "product": "Short-Term Energy Outlook (mensuel)", "domain": "energy", "status": "active"},
    {"id": "cdc_flusight", "label_fr": "CDC — FluSight (ensemble)", "short": "FluSight", "org": "US CDC",
     "product": "FluSight-ensemble (hebdomadaire)", "domain": "health", "status": "active"},
    {"id": "cdc_covidhub", "label_fr": "CDC — COVID-19 Hub (ensemble)", "short": "CovidHub", "org": "US CDC",
     "product": "CovidHub-ensemble (hebdomadaire)", "domain": "health", "status": "active"},
    {"id": "views", "label_fr": "ViEWS", "short": "ViEWS", "org": "Uppsala University / PRIO",
     "product": "ViEWS fatalities, mensuel", "domain": "conflict", "status": "active"},
    {"id": "atlanta_gdpnow", "label_fr": "Fed d'Atlanta — GDPNow", "short": "GDPNow", "org": "Federal Reserve Bank of Atlanta",
     "product": "nowcast du PIB américain", "domain": "nowcast", "status": "planned"},
    {"id": "nyfed_nowcast", "label_fr": "Fed de New York — Staff Nowcast", "short": "NY Fed", "org": "Federal Reserve Bank of New York",
     "product": "nowcast du PIB américain", "domain": "nowcast", "status": "planned"},
    {"id": "cleveland_nowcast", "label_fr": "Fed de Cleveland — nowcast d'inflation", "short": "Cleveland",
     "org": "Federal Reserve Bank of Cleveland", "product": "nowcast de l'inflation américaine", "domain": "nowcast",
     "status": "planned"},
    {"id": "usda_wasde", "label_fr": "USDA — WASDE", "short": "WASDE", "org": "US Department of Agriculture",
     "product": "World Agricultural Supply and Demand Estimates (mensuel)", "domain": "agri", "status": "planned"},
    {"id": "ecdc_respicast", "label_fr": "ECDC — RespiCast (ensemble)", "short": "RespiCast", "org": "ECDC",
     "product": "European Respiratory Diseases Forecasting Hub", "domain": "health", "status": "planned"},
]  # fmt: skip

DOMAINS: list[dict[str, str]] = [
    {"id": "macro", "label_fr": "Macroéconomie"},
    {"id": "nowcast", "label_fr": "Nowcasting (PIB, inflation US)"},
    {"id": "energy", "label_fr": "Énergie"},
    {"id": "health", "label_fr": "Santé (hospitalisations respiratoires)"},
    {"id": "conflict", "label_fr": "Conflits (risque pays-mois)"},
    {"id": "agri", "label_fr": "Agriculture"},
    {"id": "finance", "label_fr": "Finance (taux, change)"},
]

TARGETS: dict[str, dict[str, str]] = {
    "macro.weo_gdp": {"domain": "macro", "label_fr": "PIB réel — panel FMI (≈190 pays)"},
    "macro.weo_cpi": {"domain": "macro", "label_fr": "Inflation IPC — panel FMI"},
    "macro.weo_unemployment": {"domain": "macro", "label_fr": "Chômage — panel FMI"},
    "macro.g7_gdp": {"domain": "macro", "label_fr": "PIB réel — G7"},
    "macro.g7_cpi": {"domain": "macro", "label_fr": "Inflation — G7"},
    "macro.g7_unemployment": {"domain": "macro", "label_fr": "Chômage — G7"},
    "macro.ea_hicp": {"domain": "macro", "label_fr": "Inflation IPCH — zone euro"},
    "macro.us_gdp": {"domain": "macro", "label_fr": "PIB réel — États-Unis (trimestriel)"},
    "macro.us_unemployment": {"domain": "macro", "label_fr": "Chômage — États-Unis"},
    "macro.us_inflation": {"domain": "macro", "label_fr": "Inflation — États-Unis"},
    "macro.registry_weo": {"domain": "macro", "label_fr": "Questions FMI du registre (PIB, IPC, chômage)"},
    "nowcast.us_gdp": {"domain": "nowcast", "label_fr": "PIB américain — trimestre en cours"},
    "nowcast.us_inflation": {"domain": "nowcast", "label_fr": "Inflation américaine — mois en cours"},
    "energy.brent": {"domain": "energy", "label_fr": "Brent — moyenne mensuelle"},
    "energy.wti": {"domain": "energy", "label_fr": "WTI — moyenne mensuelle"},
    "energy.henry_hub": {"domain": "energy", "label_fr": "Gaz Henry Hub — moyenne mensuelle"},
    "energy.us_crude_production": {"domain": "energy", "label_fr": "Production de brut — États-Unis"},
    "energy.us_crude_stocks": {"domain": "energy", "label_fr": "Stocks de brut hors SPR — États-Unis"},
    "energy.quantities_w2": {
        "domain": "energy",
        "label_fr": "8 séries de quantités STEO (production, consommation, stocks)",
    },
    "energy.steo_v6": {"domain": "energy", "label_fr": "7 séries STEO de V6 (prix, production)"},
    "energy.registry_steo": {"domain": "energy", "label_fr": "Questions EIA du registre"},
    "health.flu_hosp": {"domain": "health", "label_fr": "Hospitalisations grippe — NHSN (US + États)"},
    "health.covid_hosp": {"domain": "health", "label_fr": "Hospitalisations COVID-19 — NHSN (US + États)"},
    "health.eu_resp": {"domain": "health", "label_fr": "Infections respiratoires — ERVISS (Europe)"},
    "conflict.cm_conflict25": {"domain": "conflict", "label_fr": "Conflit ≥ 25 décès — pays-mois (probabilité)"},
    "conflict.cm_deaths": {"domain": "conflict", "label_fr": "Décès de conflit étatique — pays-mois"},
    "conflict.registry_views": {"domain": "conflict", "label_fr": "Questions conflit du registre"},
    "agri.wasde": {"domain": "agri", "label_fr": "Bilans offre/demande agricoles (blé, maïs, soja…)"},
    "finance.rates_fx": {"domain": "finance", "label_fr": "Taux, changes, décisions de banques centrales"},
}

CAVEATS: dict[str, str] = {
    "final_vintage_outcome": "Issue mesurée sur la donnée finale (révisée), pas sur la première publication.",
    "final_vintage_inputs": "Entrées en vintage final (données révisées) : pas une compétence en temps réel.",
    "lagged_final_inputs": "IPC/IPCH et Brent lus en vintage final avec délai de publication (non révisés par "
    "convention ; vérifié sur le G7).",
    "weak_benchmark": "Repère faible : densité construite par REVA autour du point officiel, trop large "
    "(couvre 93–100 % des issues) — ne jamais écrire « REVA bat le FMI/SPF ».",
    "not_a_better_point": "Intervalle calibré autour du point officiel : le point reste celui de l'institution.",
    "outcome_vintage_mismatch": "Validé sur le vintage d'octobre t+1, résolu au registre sur celui d'avril : "
    "la calibration du holdout ne prouve pas la calibration sur le vintage de résolution.",
    "same_institution_outcome": "Issue = estimation du WEO lui-même (souvent une estimation du personnel du FMI) : "
    "favorise le WEO et tout modèle qui l'enveloppe (QA-PRED X7).",
    "uses_official_forecast": "Utilise la prévision officielle en entrée : une victoire se dit « la combinaison "
    "REVA bat <officiel> seul », jamais « REVA bat <officiel> ».",
    "timeliness_edge": "Avantage de fraîcheur : REVA utilise des données publiées après la prévision officielle "
    "(même date d'information t, prévision officielle en vigueur à t).",
    "development_only": "Développement : données déjà vues, servent à choisir les hypothèses — pas une preuve.",
    "in_sample_selection": "Sélection dans l'échantillon (pas de holdout) : admission au registre, pas une preuve.",
    "licence_restricted": "Prix spot quotidiens Refinitiv (LSEG) republiés par l'EIA : licence à vérifier.",
    "undercoverage": "Intervalles mal calibrés (couverture 80 % nettement sous 0,80).",
    "targets_seen": "Années cibles déjà vues dans un holdout V6 ; dates d'information et nowcasts nouveaux.",
    "not_evaluable": "Test déclaré non évaluable : compte p = 1 dans la famille Holm.",
    "voided_by_report": "Victoire formelle annulée par le rapport (série de résultat redéfinie).",
    "report_pending": "Rapport de l'agent pas encore publié : chiffres tirés du fichier de résultats.",
    "attribution_pending": "Demande d'attribution des hubs CDC en attente avant toute publication externe.",
    "awaiting_resolutions": "Aucune question résolue : pas encore de preuve, rien ne peut être rétro-rempli.",
    "not_sealed_yet": "Portée confirmée mais pas encore de lot scellé au registre.",
    "near_nowcast": "Questions de l'année en cours (≈ 80 % écoulés au scellement) : quasi-nowcasts.",
    "small_n": "Petit échantillon : puissance statistique faible.",
    "mixed_vintage_outcome": "Issue sur UCDP Candidate (préliminaire), version finale indicative.",
}

TRACKS: list[dict[str, Any]] = [
    {"id": "beat_macro", "label_fr": "BEAT-MACRO — nowcasts et combinaisons vs FMI / SPF BCE", "agent": "BEAT-MACRO",
     "version": "V7", "report": "docs/dev/v7_beat_macro.md",
     "protocol": "registry/forecasts/beat_macro/protocol.yaml",
     "files": ["registry/forecasts/beat_macro/hypotheses.yaml", "registry/forecasts/beat_macro/holdout_ledger.yaml",
               "registry/forecasts/beat_macro/development.json", "registry/forecasts/beat_macro/holdout.json"],
     "summary_fr": "Même date d'information t : REVA (nowcast mensuel, combinaison avec le WEO) contre la dernière "
     "prévision officielle publiée à t. Développement 2008–2018, 7 hypothèses figées, holdout 2019–2025 ouvert une "
     "fois, une seule famille Holm."},
    {"id": "beat_energy", "label_fr": "BEAT-ENERGY — prix et quantités vs EIA STEO", "agent": "BEAT-ENERGY",
     "version": "V7", "report": "docs/dev/v7_beat_energy.md", "protocol": "registry/forecasts/beat_energy/protocol.yaml",
     "files": ["registry/forecasts/beat_energy/code_relocation.yaml", "registry/forecasts/beat_energy/w3_addendum.yaml",
               "docs/dev/v7_beat_energy_result.json"],
     "summary_fr": "Une famille Holm de 127 tests pré-enregistrés sur 54 numéros archivés du STEO : W1 nowcasts de "
     "prix entre deux STEO, W2 correction de biais / combinaisons sur 8 séries de quantités, W3 données "
     "hebdomadaires."},
    {"id": "beat_health", "label_fr": "BEAT-HEALTH — post-traitement des ensembles CDC", "agent": "BEAT-HEALTH",
     "version": "V7", "report": "docs/dev/v7_beat_health.md", "protocol": "registry/forecasts/beat_health/protocol.yaml",
     "files": ["registry/forecasts/beat_health/dev_result.json"],
     "summary_fr": "Les candidats prennent l'ensemble du hub en entrée (empilement, recalibrage de largeur). "
     "Développement sur les tours V6 ; holdouts H1 (saison 2026-27) et H2 (FluSight 2023-24) pré-enregistrés."},
    {"id": "beat_conflict", "label_fr": "BEAT-CONFLICT — risque de conflit pays-mois", "agent": "BEAT-CONFLICT",
     "version": "V7", "report": "docs/dev/v7_beat_conflict.md",
     "protocol": "registry/forecasts/beat_conflict/protocol.yaml", "files": ["docs/dev/v7_beat_conflict_result.json"],
     "summary_fr": "Une famille Holm de 21 tests : W1 backtest en vintage final 2012–2021 contre la persistance, "
     "W3 prospectif contre ViEWS (mois cibles 2026-11 … 2027-04). Estimations de risque agrégées, jamais la "
     "prédiction d'une attaque."},
    {"id": "pred_models", "label_fr": "PRED-MODELS — tournoi en holdout verrouillé (V6)", "agent": "PRED-MODELS",
     "version": "V6", "report": "docs/dev/v6_pred_models.md", "protocol": "registry/forecasts/pred_models/protocol.yaml",
     "files": ["registry/forecasts/pred_models/holdout.json", "registry/forecasts/pred_models/holdout_ledger.yaml",
               "registry/forecasts/pred_models/registered_models.yaml"],
     "summary_fr": "30 hypothèses figées avant l'ouverture unique du holdout, Holm sur les 30. Les 3 « victoires » "
     "sont des densités calibrées autour du point FMI/SPF, gagnées contre un repère faible."},
    {"id": "fix7_fair", "label_fr": "FIX7 — repère équitable (erreurs passées de l'officiel)", "agent": "FIX7",
     "version": "V6", "report": "docs/dev/v6_fix7.md", "protocol": None,
     "files": ["registry/forecasts/pred_models/fair_benchmark_development.json",
               "registry/forecasts/pred_models/registered_models.yaml"],
     "summary_fr": "Les densités enregistrées refaites contre N(point officiel, RMS des propres erreurs passées de "
     "l'officiel), sur le développement seulement ; mise à l'épreuve prospective contre forecast.weo_error_fan."},
    {"id": "pred_energy_v6", "label_fr": "PRED-ENERGY — backtest STEO (V6)", "agent": "PRED-ENERGY", "version": "V6",
     "report": "docs/dev/v6_pred_energy.md", "protocol": None, "files": ["registry/forecasts/energy_winners.yaml"],
     "summary_fr": "54 numéros archivés du STEO, 7 séries ; les « gagnants » battent une marche aléatoire, aucun "
     "modèle REVA ne bat le STEO sur les points."},
    {"id": "ensemble", "label_fr": "BEAT-ENSEMBLE — ensemble REVA en ligne (registre prospectif)",
     "agent": "BEAT-ENSEMBLE", "version": "V7", "report": "docs/dev/v7_beat_ensemble.md", "protocol": None,
     "files": [],
     "summary_fr": "Le registre scelle un ensemble REVA sur chaque question ouverte, qui combine aussi les "
     "prévisions officielles attribuées ; poids appris uniquement sur les questions résolues. Tableau en direct : "
     "/v1/forecasts/ensemble."},
]  # fmt: skip

# Domains/forecasters with no attempt yet: shown as "pas encore de piste", each with the reason quoted from a doc.
NO_TRACK: list[dict[str, Any]] = [
    {"target": "nowcast.us_gdp", "forecaster": "atlanta_gdpnow", "reason_fr": "Piste BEAT-NOWCAST annoncée, pas commencée.",
     "quote": {"file": "docs/roadmap/v8_console.md", "text": "BEAT-NOWCAST vs GDPNow/NY Fed/Cleveland Fed"}},
    {"target": "nowcast.us_gdp", "forecaster": "nyfed_nowcast", "reason_fr": "Piste BEAT-NOWCAST annoncée, pas commencée.",
     "quote": {"file": "docs/roadmap/v8_console.md", "text": "BEAT-NOWCAST vs GDPNow/NY Fed/Cleveland Fed"}},
    {"target": "nowcast.us_inflation", "forecaster": "cleveland_nowcast",
     "reason_fr": "Piste BEAT-NOWCAST annoncée, pas commencée.",
     "quote": {"file": "docs/roadmap/v8_console.md", "text": "BEAT-NOWCAST vs GDPNow/NY Fed/Cleveland Fed"}},
    {"target": "agri.wasde", "forecaster": "usda_wasde", "reason_fr": "Piste BEAT-AGRI annoncée, pas commencée.",
     "quote": {"file": "docs/roadmap/v8_console.md", "text": "BEAT-AGRI vs USDA WASDE"}},
    {"target": "health.eu_resp", "forecaster": "ecdc_respicast",
     "reason_fr": "Sorties RespiCast non listables sans l'API GitHub (bloquée) et disque < 5 Go : aucun test possible.",
     "quote": {"file": "docs/dev/v7_beat_health.md", "text": "The RespiCast ensemble is the official benchmark for ERVISS"}},
    {"target": "macro.us_gdp", "forecaster": "us_spf",
     "reason_fr": "V7 : pas de données mensuelles en temps réel entre deux SPF (ALFRED bloqué) — aucune hypothèse "
     "BEAT-MACRO contre le SPF américain (les résultats V6 restent affichés).",
     "quote": {"file": "registry/forecasts/beat_macro/protocol.yaml", "text": "no real-time monthly data with vintages between SPF releases"},
     "partial": True},
    {"target": "finance.rates_fx", "forecaster": None,
     "reason_fr": "Aucun prévisionniste officiel ouvert : le repère pertinent (futures, OIS, forwards) est sous licence ; "
     "V6 PRED-FIN ne bat même pas la marche aléatoire.",
     "quote": {"file": "docs/dev/review_v6_predictions.md", "text": "no market-implied benchmark (futures/OIS/forwards are licensed)"}},
]  # fmt: skip


def forecaster(fid: str) -> dict[str, Any]:
    for f in FORECASTERS:
        if f["id"] == fid:
            return f
    raise KeyError(fid)


def track(tid: str) -> dict[str, Any]:
    for t in TRACKS:
        if t["id"] == tid:
            return t
    raise KeyError(tid)
