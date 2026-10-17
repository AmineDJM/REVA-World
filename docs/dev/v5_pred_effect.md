# V5 PRED-EFFECT — effect-estimates layer (report)

Agent PRED-EFFECT, 2026-10-17. Spec: `docs/roadmap/v5_prediction_engine.md` (decision 3: effect sizes only as
attributed numbers). Contract for other agents: `docs/dev/v5_effect_interface.md` (first commit, 81bf633).

## What was built

| Piece | Where |
|---|---|
| Migration `0019_effect_estimates` (down_revision `0018_retraction_reasons`): `kg.effect_load`, `kg.effect_estimate`, `kg.effect_estimate_retraction` (append-only, `meta.reject_mutation`), view `kg.effect_estimate_current`; load/row/retraction triggers (verified raw artifacts, rows restate their load, sealed loads); CHECKs for unit-vs-measure, sign = sign(point), interval order / point inside / level, uncertainty status, horizon, per-source-kind requirements (publication: citation + locator + raw sha256 + quote; run: run record; identity: formula); grants as `kg.causal_*` (worker INSERT, api/sim/worker SELECT, gate nothing, restricted-deletion role) | `migrations/versions/0019_effect_estimates.py` |
| Schema contract regenerated (2,292 objects, fingerprint `d2fe2f9235dbed10`, head 0019) | `migrations/schema_contract.json` |
| Backup manifest | no change needed: `scripts/backup_restore.py` digests every table of schema `kg` generically |
| Python package (vocabularies, validation, loader, readers, wording, uncertainty for Monte-Carlo, curation + automatic quote re-check) | `src/reva/effects/{vocab,variables,model,query,uncertainty,curation}.py` |
| Effect-variable vocabulary (25 variables, en/fr, REVA definitions, curated atlas links: `prices.consumer_level -> imf.cpi_index`) | `registry/effects/variables.yaml` |
| Curated publications (10 entries, 73 estimates; every estimate states its `shock_profile`) | `registry/effects/literature/*.yaml` |
| Connector `atlas.effects_lit@1.0.1` (source `effects.literature`, metered through `run_atlas_ingest`, raw PDFs archived, pinned sha256, quote re-check, load per publication) | `src/reva/adapters/connectors/atlas_effects_lit.py`, `registry/sources/effects_literature.yaml`, `registry/atlas/budget_requests/pred_effect.yaml` (merged: 40 req/day) |
| API `/v1/effects/estimates`, `/v1/effects/estimates/{key}`, `/v1/effects/compare`, `/v1/effects/variables` (read scope, envelope warnings `attributed_estimates`, `no_pooling`, `licence_restricted`) | `src/reva/api/routes/effects.py` (+1 router line in `app.py`), `contracts/openapi.json` |
| Tests: 39 unit (vocab = migration, every curated entry validates, 27 rejection rules, quote re-check, pdftotext on stdin, uncertainty bases, connector spec) + 6 integration on a throwaway DB (45 in all, passing) (loader idempotence / revision / retraction / reinstatement / partial delivery, DB CHECKs and append-only, grants, sealed loads, REVA-run rows and readers, connector fixture run, API) | `tests/unit/effects/`, `tests/integration/test_effects_store.py`, synthetic fixture `tests/fixtures/effects/`, `tests/fixtures/source/atlas.effects_lit/` |

### Honesty rules as implemented

* Every number is quoted: `as_printed` must appear in the verbatim `quote` (and `se_as_printed` too); `point` must
  equal the printed number (or its negation when the source writes the slope with a minus sign — Phillips curve
  κ/ψ — recorded as `curation.printed_transform = negated`).
* **Double check**: at load time the connector extracts the text of the archived bytes (`pdftotext -layout`,
  bytes on stdin) and re-finds every quote on the declared `pdf_page`; a missing quote or another page refuses the
  estimate (`quote_not_in_source`, `locator_page_mismatch`). Only re-found estimates are
  `transcribed_double_checked` (the check is stored in `curation.text_check` and in the load parameters). The check
  caught my own wrong page guesses for Head & Mayer, Hazell et al. and Choi et al. during curation.
* A document whose bytes differ from the pinned sha256 is not loaded (`document_changed`).
* No invented uncertainty: SEs only where printed; ranges across countries are `range_across_estimates`
  (`partial`); meta-analytic dispersion stays in `meta.sd_across_estimates`; `normal_approx()` returns
  `basis: null` when nothing is reported (PRED-ENGINE must then treat the estimate as a scenario point).
* No unverified DOIs: DOIs are recorded only when printed in the archived document (none of the 7 are); citations
  are assembled from the title pages of the version read and say so (`text_basis`), with `version_read`.
* Country codes in `scope.countries` were assigned by the curator from the printed English names / two-letter
  column heads, and the printed name is kept in `scope.sample_note`.
* REVA's own notes are in `applicability` and start with "REVA note:"; source caveats are verbatim in
  `source_caveats`.

## Sources (live fetch, 2026-10-17)

| Entry (dataset) | Document | Bytes | Licence | Estimates |
|---|---|---|---|---|
| `ball_leigh_loungani_2013` | Ball, Leigh & Loungani, *Okun's Law: Fit at Fifty?*, NBER WP 18668 (Jan 2013), https://www.nber.org/system/files/working_papers/w18668/w18668.pdf | 690,678 | © authors, short quotation with credit | 24: US annual levels λ=100/1,000 and changes (Table 1), US quarterly with two lags (Table 2, lag profile in `irf`), 20 advanced economies (Table 5) |
| `head_mayer_2013` | Head & Mayer, *Gravity Equations: Workhorse, Toolkit, and Cookbook*, CEPII WP 2013-27 (Sep 2013), https://www.cepii.fr/PDF_PUB/wp/2013/wp2013-27.pdf | 738,834 | no licence statement (treated as all rights reserved) | 8: trade elasticities (Table 5: full, structural, tariff-identified −5.03 = authors' preferred, price/wage/FX-identified), distance, RTA/FTA, EU, common currency (Table 4, structural medians) |
| `choi_furceri_loungani_mishra_poplawski_ribeiro_2017` | Choi et al., *Oil Prices and Inflation Dynamics*, IMF WP/17/196 (Sep 2017), https://www.imf.org/-/media/files/publications/wp/2017/wp17196.pdf | 1,179,217 | © IMF | 1: +10 % global oil price → +0.4 pp domestic inflation on impact (72 economies, 1970-2015) |
| `ortega_osbat_2020` | Ortega & Osbat (eds.), *Exchange rate pass-through in the euro area and EU countries*, ECB OP 241 (Apr 2020), https://www.ecb.europa.eu/pub/pdf/scpops/ecb.op241~c7c3080d60.en.pdf | 1,534,158 | © ECB, all rights reserved | 8: 1 % euro depreciation → import prices 0.30 %, HICP 0.03 % (range −0.06..0.17) within a year; time-varying Bayesian estimates EA import 0.22 / consumer 0.01 and DE/FR/IT/ES HICP 0.05/0.03/0.04/0.09 |
| `ramey_zubairy_2014` | Ramey & Zubairy, *Government Spending Multipliers in Good Times and in Bad*, NBER WP 20719 (Nov 2014), https://www.nber.org/system/files/working_papers/w20719/w20719.pdf | 269,086 | © authors, short quotation with credit | 6: 2- and 4-year integral multipliers, linear / high / low unemployment (Table 1, with SEs) |
| `hazell_herreno_nakamura_steinsson_2021` | Hazell, Herreño, Nakamura & Steinsson, *The Slope of the Phillips Curve: Evidence from U.S. States*, NBER WP 28005 (rev. May 2021), https://www.nber.org/system/files/working_papers/w28005/w28005.pdf | 546,497 | © authors, short quotation with credit | 6: κ (cols 3, 4) and ψ (cols 3, 4) of Table 1; κ pre/post-1990 with time FE (Table 2 cols 5, 6) |
| `ecb_op113_2010` | ESCB Task Force, *Energy markets and the euro area macroeconomy*, ECB OP 113 (Jun 2010), https://www.ecb.europa.eu/pub/pdf/scpops/ecbocp113.pdf | 2,836,963 | © ECB (no licence text captured) | 4: +10 % energy prices → consumer prices EA 0.36 %, FR 0.34 %, DE 0.37 %, producer prices EA 0.39 % (input-output, first round) |
| `baumeister_hamilton_2018` | Baumeister & Hamilton, *Structural Interpretation of VARs with Incomplete Identification: Revisiting the Role of Oil Supply and Demand Shocks*, NBER WP 24167 (rev. Aug 2018), https://www.nber.org/system/files/working_papers/w24167/w24167.pdf | 1,214,556 | © authors, short quotation with credit | 3: short-run oil supply elasticity 0.15 (68% band 0.09-0.22), demand elasticity −0.35 (−0.51..−0.24), oil supply shock raising the real oil price 10% → world industrial production −0.50 % after 12 months (−0.91..−0.17) (Table 3 col. 1) |
| `bobasu_ciccarelli_notarpietro_2025` | Bobasu, Ciccarelli & Notarpietro (eds.), *Monetary policy transmission: a reference guide through ESCB models and empirical benchmarks*, ECB OP 377 (2025, doi:10.2866/0318025), https://www.ecb.europa.eu/pub/pdf/scpops/ecb.op377en.pdf | 7,546,684 | © ECB, all rights reserved | 10: meta-analysis (409 studies) of an unexpected +25 bp policy-rate shock: output peak −0.25 % mean / −0.15 % median after ~2 years, price level −0.18 / −0.11 % after ~4 years, inflation −0.05 / −0.03 pp after ~3 years, publication-bias corrected output −0.14 / −0.08 %, price level −0.08 / −0.05 % |
| `gutmann_neuenkirch_neumeier_2022` | Gutmann, Neuenkirch & Neumeier, *The Economic Effects of International Sanctions: An Event Study*, Trier Research Papers in Economics 3/21 (version 23 Dec 2022), https://www.uni-trier.de/fileadmin/fb4/prof/VWL/EWF/Research_Papers/2021-03.pdf | 945,852 | no licence statement (treated as all rights reserved) | 3: sanction episode (`event_type:sanction_episode`) → GDP per capita growth −1.32 pp (year 1), −1.50 pp (year 2), GDP per capita level −2.82 % over the first two years (158 countries, 1960-2016) |

Raw archive: 10 PDFs, 17.5 MB (budget ≤ 100 MB). The PDFs are not redistributable; they live only in the raw
store (provenance) and are **not** committed (fixture tests use a synthetic CC0 text document).

## Load (dev DB)

Dev DB `reva` (migration applied with `flock /tmp/reva-devdb.lock uv run reva db bootstrap`, schema fingerprint
`d2fe2f92…`, head `0019_effect_estimates`; connector `atlas.effects_lit` registered with `reva seed`, now 1.0.3):

* **72 current `publication` estimates in 9 loads** (one per publication), all `transcribed_double_checked`
  (every quote re-found on its declared page in the archived bytes; 0 refused by the text check; every pinned
  sha256 matched): Ball-Leigh-Loungani 24, Bobasu-Ciccarelli-Notarpietro 10, Head-Mayer 8, Ortega-Osbat 8,
  Hazell et al. 6, Ramey-Zubairy 6, ECB OP 113 4, Baumeister-Hamilton 3, Gutmann et al. 3.
* **Choi et al. (IMF WP/17/196) not loaded live**: `www.imf.org` answered HTTP 403 to the project's metered client
  (a plain download of the same URL worked during curation). Not worked around: reported as `fetch_failed`
  (connector 1.0.3 continues with the other entries; 1.0.2 aborted the run at that entry, after loading the first
  three). The curated entry stays in the registry; owner decision below.
* A second full run (connector 1.0.3) re-fetched the 9 reachable PDFs (10 requests, 16.3 MB metered,
  budget `budgets-1.3.25`) and wrote nothing: 72 unchanged (idempotent; identical raw bytes are deduplicated).
* PRED-EVENTSTUDY has already written 5 `reva_run` estimates (`run:es-…:h5`) through the same loader.
* Disk: 6.7 GB free at the first live run; the last idempotent run started at 4.9 GB free (just under the 5 GB
  rule; it added no new raw bytes).

## Examples (ids)

* `pub:effects.literature:ball_leigh_loungani_2013:country_fra_levels_hp100` — cause `variable:output.gap` (gap),
  effect `variable:labour.unemployment_gap` (gap), slope −0.367 pp per 1 % (SE 0.0441), France 1980-2011, Table 5,
  pdf p.29, quote "France -0.367*** (0.0441) 32 0.681 0.394".
* `pub:effects.literature:ball_leigh_loungani_2013:us_annual_changes` — cause `atlas_concept:imf.gdp_real_growth`
  (growth rate) → effect `atlas_concept:imf.unemployment_rate` (pp change): −0.405 (SE 0.029), so PRED-ENGINE can
  propagate directly between atlas concepts.
* `pub:effects.literature:choi_furceri_loungani_mishra_poplawski_ribeiro_2017:headline_inflation_impact_10pct_oil` —
  `variable:oil_price.global` +10 % → `atlas_concept:imf.inflation_cpi_avg` +0.4 pp on impact (no SE: `basis: null`).
* `pub:effects.literature:head_mayer_2013:trade_elasticity_tariff_identified_structural` — trade elasticity −5.03
  (median of 435 estimates, s.d. across estimates 9.3).
* `pub:effects.literature:ramey_zubairy_2014:linear_2y` — U.S. government purchases multiplier 0.76 (SE 0.102),
  2-year integral.
* `/v1/effects/estimates?effect=imf.cpi_index` also returns ECB OP 113 and OP 241 consumer-price estimates through
  the curated link `prices.consumer_level -> imf.cpi_index` (`matched_via: variable:prices.consumer_level`).

## PRED-ENGINE first links (lead request, 2026-10-17)

| Reference decision link | Status |
|---|---|
| oil supply → oil price | **Loaded** (Baumeister & Hamilton 2018): supply elasticity 0.15, demand elasticity −0.35 (68% credible bands), and the activity effect of a supply shock that raises the real oil price 10%. The paper prints no "price response to an X mb/d cut"; the inverse-demand composition `Δln p = Δln q_supply_shift / (α_supply − β_demand)` would be REVA's computation and must be labelled as such (an `identity`-kind estimate or engine-side composition), not a quoted number. Kilian & Murphy and Caldara-Cavallo-Iacoviello are cited inside BH but not archived. |
| ECB +50 bp → HICP / GDP / unemployment | **Loaded** (ECB OP 377 meta-analysis) for output, price level and inflation (+25 bp unexpected shock, `one_time_impulse`, mean / median / bias-corrected). The ESCB model simulations in the same paper are charts only (not transcribed). No unemployment response is printed in a table; none loaded. Scaling 25 bp → 50 bp is linear extrapolation by the engine, not a source number. |
| India rice export ban → world rice price | **Not loaded.** Martin & Anderson (2011/2012; World Bank WPS 5645 / CEPR DP 8494: "about 45 percent of the 2006-8 rice price increase" due to insulating behaviour): the CEPR PDF answered HTTP 403 to a non-browser client (not worked around) and the World Bank PDF URL was not found in this session; moreover the number is a *contribution share of an observed price change*, which the closed `measure` vocabulary of 0019 cannot represent honestly (it is not a response to a defined shock). Owner/lead decision: add a `contribution_share` measure (needs a migration) or keep such numbers out. Variable `trade.export_restriction` added. |
| EU ban on Russian oil / embargo → target GDP | **Loaded (generic)**: Gutmann, Neuenkirch & Neumeier (2022) sanction-episode event study (`event_type:sanction_episode`, `state_indicator`): −1.32 pp / −1.50 pp GDP-per-capita growth in years 1-2, −2.82 % level after two years. Not specific to oil embargoes or to Russia. Neuenkirch & Neumeier (2015, UN vs US sanctions) is behind a JavaScript repository page (not fetched). |
| France retirement age 65 → employment | Variable `pension.minimum_pension_age` added. The published employment responses already curated by agent LIT in `registry/assumptions/retirement_employment_response.yaml` (verified quotes, but source texts *not archived*) are not yet effect estimates: importing them needs their source PDFs fetched and archived through `atlas.effects_lit` (next step). |

Shock definitions: every curated estimate now records `shock_profile` (`meta.shock_profile`, JSON `shock_profile`):
one-time structural impulses (Choi et al., Ramey-Zubairy, Baumeister-Hamilton activity effect, OP 377), state
indicators (gravity dummies, sanction episodes), permanent level shifts (ECB OP 113 input-output), contemporaneous
relations (Okun, Phillips, oil elasticities), cross-sectional (gravity elasticities) and `unspecified` (ECB OP 241,
whose text does not say how the depreciation path is defined).

## What is missing and why

* **Fiscal-multiplier meta-analysis datasets** (Gechert 2015; Gechert & Rannenberg): no open download found from
  an allowed host in this session; only Ramey & Zubairy's Table 1 was loaded. Blanchard & Leigh (IMF WP/13/1,
  archived copy checked, β = −1.095, t = −4.294) was not loaded: its estimand is a *forecast-error* coefficient
  (multiplier minus the forecasters' implicit multiplier), which needs its own variable and careful wording.
* **IMF WEO box estimates**: none loaded (not machine-readable in what I fetched; transcription from boxes deferred).
* **Head & Mayer gravity elasticities by sector** (machine-readable): the open data files were not checked; the
  Table 4/5 meta-analytic medians are loaded instead.
* **Published-version numbers**: every entry is a working-paper version; journal versions may print different
  numbers (said in `version_read`). DOIs are left null rather than unverified.
* **IRFs**: only Okun's distributed-lag coefficients; the oil and fiscal IRFs are figures (not transcribed —
  reading numbers off a chart would be invented precision).
* **Causal-claim links** (`causal_claim_keys`): none yet; there is no KG claim these estimates quantify.

## Owner decisions

1. Quoting single numbers with short verbatim passages from all-rights-reserved working papers (NBER explicitly
   allows short quotation with credit; IMF/ECB/CEPII do not say): OK for research use? Archived PDFs are kept for
   provenance only.
2. Curator-assigned ISO3 codes from printed country names (kept verbatim beside the code): acceptable, or should a
   maintainer review each?
3. Whether a maintainer review (`curation.review_status`) is required before PRED-ENGINE may use an estimate.
4. `www.imf.org` refuses the project client (HTTP 403) for IMF working-paper PDFs: request access / an allowed
   download route, or drop IMF sources (Choi et al. oil pass-through stays curated but unloaded).
5. Contribution-share numbers (e.g. "45 percent of the 2006-8 rice price increase was due to insulating
   behaviour"): add a `contribution_share` measure (new migration) or keep them out of the effects layer.

## Commands (to repeat or extend)

```bash
uv run python scripts/update_schema_contract.py            # after any migration change (done)
flock /tmp/reva-devdb.lock uv run reva db bootstrap        # applies 0019 (done)
flock /tmp/reva-devdb.lock uv run reva seed                # registers atlas.effects_lit@1.0.1
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.effects_lit            # all entries
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.effects_lit --option entry=<id>   # one entry
uv run pytest -q tests/unit/effects tests/integration/test_effects_store.py
```

Adding a publication: write `registry/effects/literature/<id>.yaml` (pin the document sha256, give `pdf_page`,
`locator` and the verbatim `quote` of every number), run the unit tests (every entry must validate), then ingest;
the connector re-finds every quote in the archived bytes before loading.
