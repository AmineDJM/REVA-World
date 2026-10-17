# V6 EFFECTS-MORE — 225 more attributed effect estimates (report)

Agent EFFECTS-MORE, 2026-10-17. Contract: `docs/dev/v5_effect_interface.md`; predecessor `docs/dev/v5_pred_effect.md`.
REVA asserts no effect: every number below is a published number with its citation, locator, PDF page, verbatim
quote and the sha256 of the archived document; the quote was re-found automatically on its page in the archived
bytes at load time (`transcribed_double_checked`). Nothing was read off a chart; no uncertainty was invented
(`not_reported` wherever the source prints none).

## Result

* **225 new current estimates loaded live** (dev DB, `atlas.effects_lit@1.1.1`, loads 21–38), **0 refused** by the text
  check or the validator; ~~302~~ **301** current published + REVA-run estimates in all (296 + 5; 77 before; corrected
  by FIX6, AUDIT4 L6).
* 18 new curated publications (`registry/effects/literature/*.yaml`), all `maintainer_review_pending`; 44 estimates
  are `evidence: secondary` (another study's number as printed in a survey/comparison table, labelled as such).
* Every estimate carries a `shock_profile` field; ~~it is stated~~ **20 current rows say `unspecified`** (the 12 new
  Ramey survey multipliers and 8 V5 Ortega–Osbat rows: the source does not state the profile; FIX6, AUDIT4 L6), the
  281 others state one. 32 new effect variables (`registry/effects/variables.yaml`), 4 curated atlas
  links (below).
* Raw archive: 18 PDFs, ~24 MB new (OP 113 deduplicated); metered run: 28 requests, 42.6 MB (budget `budgets-1.3.33`).
* Re-run of the 5 reference decisions appended to `docs/dev/v5_pred_engine.md` §7c: the OPEC+ and ECB decisions now
  produce attributed, dated scenario numbers (oil price +12.5 % in 2027Q1; country GDP / unemployment / deflator
  paths for DEU FRA ITA ESP NLD).

## Publications (all open downloads; URL = `document.url` in each file)

| Entry | Source | Host | Est. | Domain / what |
|---|---|---|---|---|
| `caldara_cavallo_iacoviello_2016` | Fed IFDP 1173 (DOI 10.17016/IFDP.2016.1173, printed) | federalreserve.gov | 8 | **oil supply → oil price**: 1 % supply decline (≈0.8 mb/d) → +5 % oil price within a month; identified elasticities 0.11 / −0.11; literature medians 0.13 / −0.13 (6 and 30 studies); 1-s.d. supply shock → oil price, advanced/emerging IP |
| `van_els_locarno_morgan_villetelle_2001` | ECB WP 94 | ecb.europa.eu | 42 | **monetary policy, euro area by country**: 100 bp for 2 years → real GDP, consumption deflator, **unemployment rate** for 12 countries + aggregate + AWM, annual profile 2001–05 (`irf`) |
| `angeloni_kashyap_mojon_terlizzese_2002` | ECB WP 114 | ecb.europa.eu | 12 | sustained +50 bp and 5 % NEER appreciation → CPI, GDP (EMM, AWM, FRB-US), quarters 4 and 12 |
| `ramey_2016` | NBER WP 21978 | nber.org | 46 | monetary shocks → output/IP (11 survey rows, 7 own), government-spending multipliers by horizon with HAC SEs (16 own, 6 survey), tax multipliers (6 survey) |
| `romer_romer_2004` | NBER WP 9866 | nber.org | 4 | US 100 bp narrative shock → IP (−4.8 % at month 22, ±2 SE interval), PPI (−5.9 % at month 48) |
| `cloyne_hurtgen_2014` | Bank of England WP 493 | bankofengland.co.uk | 6 | **UK**: +1 pp Bank Rate → IP −0.61 %, inflation −1.00 pp (VAR), GDP −0.5 %, ADL permanent-shock responses |
| `dell_jones_olken_2008` | NBER WP 14132 | nber.org | 12 | **climate**: +1 °C → growth of GDP per capita in poor countries (0–10 lags, SEs), rich countries, agriculture/industry/investment growth |
| `bilal_kanzig_2024` | NBER WP 32450 (rev. Jan 2026) | nber.org | 4 | **climate**: global temperature shock scaled to 1 °C → world GDP pc peak −14 % (95 % CI −22..−6), −18 % (BU); permanent 1 °C −20 % / −29 % (the same version's introduction prints 22–34 %: recorded in `source_caveats`) |
| `hsiang_jina_2014` | NBER WP 20352 | nber.org | 8 | **disasters**: +1 m/s cyclone wind exposure → GDP pc −0.37 % after 20 years; 1-s.d. cyclone −3.6 %; **conflict / crises** (secondary, Cerra–Saxena): civil war −3.0 %, currency crisis −4.0 %, banking crisis −7.5 % after 10 years; financial crisis −9.0 % (Reinhart–Rogoff); tax +1 % GDP −3.1 % (Romer–Romer); +1 °C poor −1.0 % (DJO 2012) |
| `amiti_redding_weinstein_2019` | NBER WP 25672 | nber.org | 8 | **tariff pass-through**: before-duty price elasticity −0.003 (0.024) → complete pass-through; import quantity/value; US export price/qty/value to retaliation; duty-inclusive price index 1.044 |
| `fajgelbaum_goldberg_kennedy_khandelwal_2019` | NBER WP 25638 | nber.org | 10 | variety import values −1.52, quantities −1.47, before-duty 0.00, duty-inclusive 0.58; import demand −2.53 (0.26); retaliation on US exports; foreign demand −1.04 |
| `cavallo_gopinath_neiman_tang_2019` | NBER WP 26396 | nber.org | 4 | ex-tariff Chinese import price −0.079 % per pp tariff (SE 0.026); ERPT 0.219; US export prices −0.541 / −0.628 per pp of retaliation |
| `giordani_rocha_ruta_2012` | WTO ERSD-2012-08 | wto.org | 1 | **export restrictions → world food prices**: elasticity 1.103 [0.584] w.r.t. the share of trade covered (29 products, 2008–10, 2SLS) |
| `anderson_ivanic_martin_2013` | NBER WP 19530 | nber.org | 8 | 2006–08 price insulation of all countries raised world prices: rice +51.9 % (50.5 % alt.), wheat 17.6, maize 18, edible oils 30.9 |
| `laudati_pesaran_2021` | arXiv:2110.09400v1 | arxiv.org | 4 | **sanctions on an oil exporter (Iran)**: output growth −2 %/yr at median sanctions intensity; 1-s.e. sanctions shock → rial −3 %, inflation +0.3 pp/quarter; Gharehgozli SCM −17 % (secondary) |
| `atav_jongen_rabate_2021` | IZA DP 14150 | docs.iza.org | 18 | **retirement age → employment of older workers**: Table B.1 rows AUT (m/f), AUS, GBR, NLD (De Vos; this paper ×2), FRA (Rabaté–Rochut +20.9 pp / −47.8 pp), DEU; Mastrobuoni (USA) retirement age per month of NRA |
| `manoli_weber_2016` | NBER WP 22561 | nber.org | 8 | Austria 2000 reform kink: exit age +0.39/+0.358/+0.391/+0.320 years per ERA year, claim age +0.54.. (SEs) |
| `ecb_op113_2010_countries` | ECB OP 113 (same PDF as `ecb_op113_2010`) | ecb.europa.eu | 22 | **energy prices → inflation, 12 more countries**: +10 % energy → consumer prices IE GR ES IT NL AT PT SI SK FI; producer prices for all 12 countries (input-output, first round) |

Licences: NBER © authors (short quotation with credit, notice recorded verbatim); ECB "Reproduction for educational
and non-commercial purposes is permitted provided that the source is acknowledged" (verbatim); Bank of England,
Fed, IZA, WTO, arXiv: no licence text captured → treated as all rights reserved. All `restricted: true`; PDFs live
only in the raw store (provenance), never in git.

## Variables and atlas links

New variables (REVA definitions, en/fr): `output.industrial_production`, `output.real_activity` (survey rows whose
output measure is unnamed), `prices.consumption_deflator`, `prices.inflation_rate`, `labour.employment_rate_affected_ages`,
`pension.receipt_rate_affected_ages`, `labour.labour_market_exit_age`, `pension.claiming_age`, `climate.country_temperature`,
`climate.global_mean_temperature`, `output.agricultural_value_added`, `output.industrial_value_added`,
`investment.real_investment`, `disaster.cyclone_wind_exposure`, `trade.import_tariff_factor`, `trade.import_tariff_rate`,
`trade.export_tariff_factor_abroad`, `prices.import_price_before_duty`, `prices.import_price_duty_inclusive`,
`trade.import_quantity`, `trade.import_value`, `prices.export_price`, `trade.export_quantity`, `trade.export_value`,
`exchange_rate.bilateral_depreciation`, `trade.export_restriction_share`, `trade.price_insulation_global`,
`prices.international_food_commodity`, `fiscal.tax_revenue`, `sanctions.intensity_newspaper`,
`output.real_gdp_growth_quarterly`. (GDP growth estimates use FIX4B's `output.real_gdp_growth` /
`output.real_gdp_per_capita_growth`.)

Curated atlas links (identifier links with a note, never by name): `oil.world_production → resources.energy.eia.crude_oil_production`
(this is what lets the OPEC+ decision reach an estimate), `output.industrial_production → macro_hf.industrial_production_m_sa`,
`climate.country_temperature → environment.temperature_anomaly` (changes only; anomaly = temperature − constant).
Monetary-policy causes use the atlas concept `imf.policy_rate` directly; unemployment effects `imf.unemployment_rate`.

## Examples (keys)

* `pub:effects.literature:caldara_cavallo_iacoviello_2016:oil_price_response_to_supply_decline_08mbd` — cause
  `variable:oil.world_production` (+0.8 mb/d; stored with `printed_transform: negated` because the source states a
  decline), effect `oil_price.global` −5 % on impact; quote p.15 "(approximately 0.8 million barrels per day) should
  lead, over a one-month horizon, to an increase in oil prices of 5 percent".
* `pub:effects.literature:van_els_locarno_morgan_villetelle_2001:unemployment_rate_esp` — `imf.policy_rate` +100 bp
  for two years → Spain unemployment +0.06, +0.31, +0.52, +0.52, +0.38 pp (2001–05), Table A.2 p.59.
* `pub:effects.literature:dell_jones_olken_2008:growth_poor_temperature_10_lags` — +1 °C → −2.006 pp (SE 0.866)
  cumulated over 10 lags, poor countries, 1950–2003.
* `pub:effects.literature:atav_jongen_rabate_2021:employment_rabate_rochut_2019` — France NRA 60 → 61: employment at
  the affected ages +20.9 pp (secondary, DID).

## Not done / why

* **Kilian (2009), Känzig (2021)**: no open PDF on an allowed host (Kilian's university page 403/404; Känzig's site has
  data vintages on GitHub but not the paper). Baumeister–Hamilton (V5) + Caldara et al. cover the oil-supply link.
* **Burke–Hsiang–Miguel, Kalkuhl–Wenz, Kotz et al.**: open replication data give coefficients only after a REVA
  estimation run (not a printed number); Kotz et al. 2024 was not used (its status is contested). DJO + Bilal–Känzig
  + Hsiang–Jina cover temperature/disaster damages.
* **Health (vaccination → mortality)**: open meta-analyses print relative risks (e.g. RR 0.70), which the closed
  measure vocabulary of 0019 cannot store as printed (a ratio is not `response`/`elasticity`); NCBI E-utilities was
  rate-limited for this shared IP. Owner decision: add a `relative_risk` measure (migration) or keep RRs out.
* **Mueller / Collier conflict costs**: civil-war effect taken from Cerra–Saxena via Hsiang–Jina (secondary); no open
  primary table found in the time box.
* **Single-country export ban → world rice price**: no publication prints it; the WTO elasticity needs India's share of
  world rice trade covered (REVA computation, not done). Vestad (2013, ERA lowered) skipped: storing it as the effect of
  a rise would assume symmetry.
* Choi et al. (IMF WP/17/196): still HTTP 403 from www.imf.org (unchanged, not worked around).
* IFS WP 14/19 and IZA DP 10681 (retirement, already curated in the assumptions registry) not archived in this run.
* Survey tables (Ramey; Hsiang–Jina; Atav et al.) are secondary evidence: the original studies were not read.

## Owner decisions

1. Secondary-evidence numbers (44) from survey tables: keep them in the effect layer (labelled), or require the
   original publication?
2. Model simulations (ECB WP 94/114, `method: structural_model`) now drive the ECB decision's country paths ahead of
   the OP 377 meta-analysis (exact scope ranks first): acceptable, or should econometric estimates outrank simulations?
3. `relative_risk` / `contribution_share` measures (migration) for health and food-crisis attribution numbers.
4. Engine artefact seen in the OPEC+ run: world-scoped variables are replicated per decision country.

## Commands

```bash
uv run pytest -q tests/unit/effects                      # every curated entry validates (unit)
flock /tmp/reva-devdb.lock uv run reva seed              # registers atlas.effects_lit@1.1.1
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.effects_lit                 # all entries (28 requests)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.effects_lit --option entry=<id>
uv run python -m reva.predict.run --reference all --draws 2000 --seed 0
```
Note: the connector's code digest covers `reva.effects.model`; any change there needs a version bump + `reva seed`
before the next ingest (1.1.0 → 1.1.1 happened for that reason).
