# REVA World V5 — decision-conditional prediction engine

Owner decision (2026-10-17): build the prediction engine; the lead decides the details. Baseline analysis:
`docs/strategy/world_map.md` (§5). REVA proves skill before it claims it.

## Decisions (lead, 2026-10-17)

1. **First domains:** supply-chain & price shocks (energy, minerals, food) and macro (inflation, unemployment,
   GDP growth) at **1–8 quarters** (prices: 1–12 months). Conflict risk comes next.
2. **Probabilities only for models that passed the judge** (as-of replay + calibration). Others answer with
   labelled scenarios/ranges and no probability, as in V1–V2.
3. **Effect estimates are allowed as attributed numbers**: every effect size carries its source (a published
   estimate with citation, a REVA estimation run with method/data/as-of pinned, or an accounting/physical
   identity). REVA still never asserts causation itself; an un-quantified causal claim stays without a number.
4. **No look-ahead, ever:** every backtest uses vintage data as published at the time (vintages layer) or is
   labelled "final-vintage pseudo-real-time" and never reported as real-time skill.
5. **Not financial advice;** no market-moving, insider or personal data.

## Components (agents)

| Agent | Builds |
|---|---|
| PRED-VINTAGE | vintage (real-time) datasets: IMF WEO historical vintages (forecasts + data), ALFRED/FRED vintage CSVs, OECD real-time database (MEI/EO vintages), Philadelphia Fed SPF and ECB SPF, Consensus-like public surveys; stored as atlas tables keyed by (as_of, period) |
| PRED-HF | daily series not covered by V4ECON (which owns monthly/quarterly macro): ECB reference FX rates, BIS policy rates daily, EIA daily spot prices (Brent, WTI, Henry Hub), US Treasury yield curve daily, gold/commodity daily where open |
| PRED-EFFECT | migration `0019_effect_estimates`: `kg.effect_estimate` (append-only) — cause variable/event, effect variable, sign, size, unit/elasticity type, horizon/lag profile, uncertainty (SE / CI / distribution), population/scope, method, source (citation/DOI or REVA run id), as-of, applicability; API `/v1/effects/*`; loader; first imports from openly available meta-analyses / published estimates (oil-price pass-through to CPI, exchange-rate pass-through, fiscal multipliers, trade elasticities, Okun's law, Phillips curve slopes) with verbatim citations |
| PRED-EVENTSTUDY | event-study / local-projection engine on atlas panels using coded events (coups, sanctions, banking crises, disasters, conflicts) as treatments; as-of aware; reproduces ≥ 3 published results before any new estimate (e.g. banking-crisis output losses, sanctions → GDP growth, disasters → GDP); writes REVA-run effect estimates into PRED-EFFECT's layer |
| PRED-REGISTRY | prospective forecast registry in production use: question schema, ≥ 100 questions/quarter (macro, prices, energy, trade), baselines (persistence/random walk, AR, IMF WEO, SPF), frozen forecasts, automatic resolution jobs and scores (Brier, log, CRPS, interval coverage), leaderboard API/UI; reuses `src/reva/evaluation/**` |
| PRED-ENGINE | decision → distribution: decision schema (who, what, when, magnitude), propagation through the causal graph using effect estimates + existing simulators (IO price model, crude exposure, population, retirement) with Monte-Carlo over estimate uncertainty, counterfactual vs baseline path, provenance of every hop; API `/v1/predict/decision`, UI "Décision" screen; 5 reference decisions as tests: OPEC+ cut of 2 mb/d, India rice export ban, ECB +50 bp, EU ban on Russian oil imports, France retirement age 65 in 2027 |

Coordination: PRED-EFFECT lands the schema first (others code against `docs/dev/v5_effect_interface.md`, written
by PRED-EFFECT in its first commit). Migrations: only PRED-EFFECT (0019). Disk is tight (~5 GB): raw archive
≤ 100 MB per agent. Same honesty and git rules as V4 (`scratchpad v4_common.txt` restated in each brief).
