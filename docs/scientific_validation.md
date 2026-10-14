# Scientific validation programme

The stages follow §13. Status is honest: a stage is "in place" only when the
named evidence exists in the repository and runs in CI.

| Stage | Question | Required evidence | V1 status |
|---|---|---|---|
| V0 verification | Does code implement the stated mathematics? | unit, dimensional, analytic, property, conservation tests | **In place** for the MVP models: hand-computed crude exposure cases, exact ε = 0 identity, Decimal rounding checks (`tests/scientific/test_crude_exposure.py`); forecasting formulas and metrics (`tests/unit/forecasting/`); exact population conservation, including property tests (`tests/unit/population/`, `tests/property/`); VOI analytic cases (`tests/scientific/test_voi_analytic.py`); spherical-integration verification (`tests/unit/test_extensions_physical.py`); units (`tests/unit/test_ontology_units.py`). |
| V1 measurement | Are source values interpreted correctly? | source fixtures, semantic review, support and revision audits | **Partly in place**: captured WDI/Comtrade/Natural Earth/Crossref fixtures with hashes and licences; drift/truncation/missingness checks (`tests/security/test_http_connectors.py`); revision semantics (`tests/integration/test_revisions.py`); registry definitions checked against provider metadata on 2026-10-14. Human semantic review of each concept is recorded in the registry, not automated. |
| V2 prediction | Does it outperform simple baselines? | locked vintage-aware temporal/geographic holdouts | **Machinery only**: rolling-origin tournament with permanent naive baseline, block bootstrap and locked holdout (`tests/unit/forecasting/test_forecast_tournament.py`, `tests/integration/test_evaluation_service.py`). The available panels are revised-data retrospective, so no real-time predictive superiority is claimed. The prospective prediction ledger (`reva.evaluation.ledger`) freezes forecasts for later scoring; no outcomes have been scored yet. |
| V3 intervention | Are intervention assumptions defensible? | identification review, external intervention evidence | **Not started.** The MVP scenario is an accounting identity under stated assumptions; it makes no identified intervention claim. |
| V4 decision | Does use improve choices? | prospective decision trials, shadow use | **Not started.** |
| V5 durability | Does it stay useful through regime change? | live prediction ledger, drift analysis, adversarial reviews | **Not started** beyond connector drift detection and the ledger mechanism. |

## Rules that apply to every stage

* Negative findings and unsupported cases are published, not filtered.
* Higher resolution or attractive maps are not validation.
* Holdouts are not reused by the challenger; repeated use is recorded.
* Scores report their denominator (number of origin-series pairs, horizons,
  subgroups) and the replay mode.
* Promotion requires the policy gates in `registry/policies/promotion.yaml`;
  insufficient evidence keeps the incumbent.
