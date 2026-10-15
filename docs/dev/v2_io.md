# V2 step 3b / step 5 (IO part): FIGARO connector and `economic.io_price@1.0.0`

Agent IO, 2026-10-15. Status: implemented, **experimental model (maintainer review pending)**.
Every result is labelled **"first-round cost-push under full pass-through; not an inflation
forecast"** (L2 rows: "...under a stated pass-through share...").

## 1. Data source

| Item | Value |
|---|---|
| Source | Eurostat FIGARO, *EU inter-country input-output table at basic prices, industry by industry* |
| Dataset codes | `NAIO_10_FCP_II1` (2010-13), `II2` (2014-17), `II3` (2018-21), `II4` (2022 onwards) |
| Edition | 2026 edition (released 18-20 July 2026; ESMS: "the data release in 2026 covers 2010-2024") |
| Year used | **2024** (latest; discovered from the dataset's SDMX content constraint, not hard-coded) |
| Access | Eurostat SDMX 2.1 dissemination API, `format=SDMX-CSV&compressed=true` (gzip CSV) |
| Licence | Eurostat reuse policy (CC BY 4.0, acknowledgement "Source: Eurostat") |
| Shape | 50 economies (EU27, NO, CH, UK, ME, MK, AL, RS, TR, RU, ZA, CA, US, MX, AR, BR, CN, JP, KR, IN, ID, SA, AU, `WRL_REST`) x 64 NACE Rev. 2 industries = **N = 3200**; 5 final-demand categories per economy; 6 primary-input rows (`c_orig = DOM`) |
| Cells | 11,060,700 per year (dense; every cell published, no flags, no confidentiality status in 2026/2024) |
| Unit | million EUR, current prices, basic prices, 3 decimals |

OECD ICIO was not needed (FIGARO reachable).

**Fetch plan** (`src/reva/adapters/connectors/io_tables.py`, connector `eurostat.figaro.iciot_ii@1.0.0`,
template `eurostat_sdmx_csv`, source key `eurostat.dissemination`): 1 content-constraint request, then
26 data requests (25 pairs of supplying economies + the `DOM` primary-input rows) = **27 requests**
(within the 100/day `eurostat.dissemination` budget). Eurostat answers requests above roughly half a
million cells asynchronously (a SOAP "queued" envelope; observed live for 4 and 13 economies per request);
two economies per request stayed synchronous. A queued envelope is a typed retryable
`provider_async_queued` source error, never parsed as data. Each response is archived as raw bytes
(content-addressed, `raw` namespace) by the normal evidence ingest path; the connector emits **no scalar
observations** (`inserted = 0`).

**Sizes (2024)**: raw responses 1.2-2.3 MB gzip each, about 55 MB in total (about 930 MB as CSV);
registered dataset Parquet 12.6 MB (11,063,900 rows incl. 3,200 derived gross-output rows; 233 MB in
Arrow memory). Captured fixtures in the repo: only the real content constraint (7 KB) and the real 2024
`DOM` rows (138 KB) — `tests/fixtures/source/eurostat.figaro.iciot_ii/`.

## 2. Dataset input and identity checks

`build_dataset` (pure) parses every archived response (per-item checks: header, dataflow, frequency,
unit, period, pinned code lists, requested origins only, numeric non-empty values, no confidentiality
status, no duplicate keys, exact dense row count; flags are counted, never dropped), writes the long
table item by item (bounded memory) and verifies on exact decimal sums (128-bit integers):

* **row total = column total** for each of the 3200 industries:
  x_row = Σ intermediate use + Σ final demand; x_col = Σ intermediate inputs + D1 + B2A3G + D29X39 + D21X31;
  tolerance |x_row − x_col| ≤ 0.001 million EUR (one unit in the last published decimal);
* gross output ≥ 0; structural zeros: OP_RES/OP_NRES = 0 in industry columns, D1/B2A3G/D29X39 = 0 in
  final-demand columns; dense completeness.

Any failure raises `IdentityCheckFailed` and **nothing is registered**. Otherwise
`publish_io_dataset` registers the table through ENG's `register_dataset` (dataset key
`figaro.iciot_ii.2024`, concept `economics.io.figaro_ii_table@1.0.0` (`registry/concepts/io_tables.yaml`),
subject `WORLD`, parser `figaro-sdmx-csv-1`), with the 27 source acquisition ids and the full identity
report, code lists and economy→ISO3 crosswalk in the dataset's `coverage` (stored in the dataset manifest
artifact and in `evidence.dataset.coverage`). A belief release then pins it by digest.

**Identity results, FIGARO 2026 edition, year 2024 (live, 2026-10-15):**

| Check | Result |
|---|---|
| dense completeness | 11,060,700 / 11,060,700 cells, 0 empty |
| row total = column total | 3200 / 3200 industries, **max \|diff\| = 0.000 million EUR** (exact at published precision) |
| gross output ≥ 0 | passed; 67 industries have zero output (mostly `U` extraterritorial; also e.g. `MT:A02`, `LU:A03`, `LU:C19`, `ME:C19`, `ZA:C33`) |
| structural zeros | 0 violations |
| totals | gross output 197,538,681.936; intermediate 98,063,687.183; final demand 99,474,994.753 (million EUR) |

Other observed properties: no negative intermediate flows (A ≥ 0); 5 industry columns have column sums
of A above 1 (negative value added: `BR:M72`, `CY:H51`, `EE:H51`, `LU:C22`, `ME:C29`); spectral radius
of A ≈ 0.585 (eigenvalues, offline check) and the model's rigorous Collatz-Wielandt bound 0.594 < 1.
Eurostat states that confidential cells are replaced by adjusted values before dissemination (controlled
tabular adjustment); that adjustment is invisible in the data and is not an uncertainty statement.

## 3. Model `economic.io_price@1.0.0` (`src/reva/models/economic/io_price.py`)

Leontief price model with exogenously priced sectors. Shocked industries S (economy, industry) get the
stated price change π_S exactly; every other industry with positive output (E) passes on share θ of its
unit-cost increase:

    (I − θ A_EEᵀ) Δp_E = θ A_SEᵀ π_S,     Δp_S = π_S,     A = Z / x

L1 is θ = 1 (full pass-through). Zero-output industries get a typed `not_applicable` value (never 0).
Household basket of economy d: Σ_k h_kd Δp_k / Σ_k h_kd with h = households' final consumption
(`P3_S14`) of d bought from every industry k (domestic and imported), at basic prices (trade and
transport margins inside, taxes less subsidies on products outside). Each basket row's applicability
also carries `household_product_taxes_less_subsidies_mio_eur` (T_d, FIGARO `DOM` x `D21X31` x `P3_S14`)
and a labelled purchasers'-price reading `purchasers_price_reading_fixed_unit_taxes_percent`
= basket_d x H_d / (H_d + T_d) (H_d = basic-price basket value), i.e. product taxes held fixed in money
per unit, per pass-through point (absent when the table has no tax cell for d). Decompositions
(exact before rounding): domestic vs imported **products**, and domestic vs foreign **shock origin**
(one right-hand side per shocked origin economy, same LU factorisation).

* Shockable industries: `B` Mining and quarrying (NACE B05-B09: coal and lignite; crude petroleum and
  natural gas; metal ores; other mining and quarrying; mining support services) — **broader than crude
  petroleum**: shocking B moves the price of all of it, and the crude share of B differs by country;
  `C19` coke and refined petroleum products; `D35` electricity, gas, steam and air conditioning.
  FIGARO has no CPA B06 (crude petroleum and natural gas) split: the 64-industry level is the finest.
* Envelope: price change in (−100 %, +200 %]; θ in (0, 1]; one annual period; `missing_input_policy=fail`.
* Countries reported: the 49 FIGARO countries (ISO3); the rest of the world (`FIGARO_ROW`) can be a shock
  origin but is not a reported subject (not a country; no world entity). Other countries are refused with
  `capability_gap` (they are inside FIGARO's rest of the world).
* Outputs: `industry_price_change_percent:<industry>` (64 per country), `household_basket_price_change_percent`,
  four `household_basket_contribution_*_percent`; unit `unit.percent`; basis nominal, basic prices.
* Numerics: float64; LU with partial pivoting (SciPy/LAPACK); each right-hand side checked
  ‖Mx − b‖₂/‖b‖₂ < 1e-10 (observed ≤ 2.4e-15 on the live table); productivity checked by a rigorous
  upper bound of ρ(|A|) < 1; results Decimal strings rounded half-even to 6 decimals of a percent.
* Uncertainty: unquantified (FIGARO publishes none).
* Not claimed: inflation forecast, CPI, GDP/welfare/wage/exchange-rate/monetary response,
  substitution, demand response, dynamics. `outputs.macroeconomic` stays reserved.

**L2**: pass-through grid (default {0.5, 0.75, 1}). Each grid point is its own variant
(`L2:pass_through=0.5`, ...), θ = 1 is bit-identical to L1, and a separate `L2:sensitivity_range` row
holds min/max over grid ∪ {θ = 1} with uncertainty kind `sensitivity_range` and **no level**. With
same-sign shocks and A ≥ 0 every price change grows with θ (Neumann series), so the range is
**one-sided**: L1 (full pass-through) is the largest-magnitude end; with mixed-sign shocks the row says
no monotonicity is claimed. θ applies uniformly to all non-shocked industries; it is a scenario
assumption, not an estimate.

## 4. Validation and plausibility

**Hand-computed check**: a synthetic 3-economy x 2-industry table
(`tests/fixtures/io_tables/synthetic.py`) has the closed-form answer p_FR = 2/97 (2.061856 %),
p_DE = 0.6/97 (0.618557 %), FR basket 3.12/0.97 % = 3.216495 %, θ = 0.5: 1.007557 %; matched exactly
in the unit tests and through the full DB/job path.

**No external benchmark was reproduced.** The published IO pass-through studies (collected by agent LIT in
`registry/assumptions/io_energy_passthrough_benchmarks.yaml`, maintainer review pending) use other data
years, national rather than inter-country tables, product-by-product tables and other shock definitions,
so they are order-of-magnitude plausibility checks only. REVA numbers (FIGARO 2024, θ = 1, shocks applied
in every FIGARO economy so imported inputs move too; "basic-price basket" = household basket at basic
prices, i.e. without taxes less subsidies on products; "producer" = output-weighted mean over the
economy's 64 industries). **EA12** = the twelve euro-area countries of the ECB benchmark (ECB OP 113):
DEU, IRL, GRC, ESP, FRA, ITA, NLD, AUT, PRT, SVN, SVK, FIN (not BEL, not LUX); its basket is the
**household-consumption-weighted pooled basket** Σ_{d∈EA12} Σ_k h_kd Δp_k / Σ_{d∈EA12} Σ_k h_kd, and its
producer figure the output-weighted mean Σ x_k Δp_k / Σ x_k over the 12 x 64 industries (aggregation
documented in `docs/methodology.md`; the scientific review's 0.937 used a different twelve-country list,
`docs/dev/review_science_v2.md` L1):

| Benchmark (published) | Published | REVA comparable run | REVA |
|---|---|---|---|
| ECB OP 113 (2010, IOT 2005): +10 % all energy products, consumer prices before taxes, euro area / France | 0.36 % / 0.34 % | B, C19, D35 +10 % (exogenous), basic-price basket, EA12 / FRA | 0.95 % / 0.93 % |
| same | | same run, purchasers'-price reading (product taxes fixed per unit), EA12 / FRA | 0.84 % / 0.82 % |
| same, producer prices EA / FRA | 0.39 % / 0.30 % | same run, output-weighted producer prices EA12 / FRA | 0.93 % / 0.86 % |
| same | | **B only** +10 %, basic-price basket EA12 / FRA | 0.36 % / 0.39 % |
| CNB MPR Spring 2026 Box 2: +100 % oil and gas, Czech CPI, full pass-through (2024 scaled estimate) | 3.6 % | B and C19 +100 %, basic-price basket CZE / B only | 4.94 % / 3.82 % |
| Banque de France Eco Notepad 50 (2018): +EUR 10/bbl | 0.4 pp (0.15 indirect) | not compared: REVA holds no archived reference oil price to turn EUR/bbl into a percentage | — |

**Purchasers'-price reading.** The basic-price basket leaves out taxes less subsidies on products
(VAT, excises): for French households 180 bn EUR, 13.2 % of the 1,364 bn EUR basic-price basket
(`docs/dev/review_science_v2.md` M9). Holding those taxes fixed in money per unit, the same price change
spread over the tax-inclusive basket gives France 0.93 % → **0.82 %** and EA12 0.95 % → **0.84 %**.
Purely ad valorem taxes would leave the basic-price figure unchanged, so the two readings bracket the tax
treatment; the 0.93 % is the upper side for a purchasers'-price reading. Neither reading is a CPI (no
direct purchases abroad, fixed weights, no second-round effects).

Reading: the all-energy-like REVA run is about 2.6x the ECB 2005 result at basic prices (about
2.3-2.4x with the purchasers'-price reading; the ECB figure is itself "before taxes"). Contributing reasons that are
visible in the data: (i) in FIGARO 2024 French households' basic-price purchases of C19 + D35 are 5.4 % of
the basket (2.2 % + 3.3 %), so the direct effect alone is about 0.54 % against the ECB's 0.21 % for France
(energy cost shares after the 2022-23 price surge are much higher than in 2005); (ii) setting B, C19 and
D35 all exogenous at +10 % plus a broad B (metal ores, quarrying) adds non-energy cost; (iii) the
inter-country table propagates foreign cost increases into imports, which the ECB national-table
calculation excluded (the authors note this biases their result downward). The B-only run is about the
ECB all-energy figure rather than clearly below it, as LIT anticipated a crude-only shock would be; B is
not crude-only (metal ores and quarrying feed C23/C24), and a B shock propagates to C19 (+5.0 % on average)
and D35 (+1.9 %). These are explanations offered, not demonstrated decompositions. The macro-model ECB
figure (Monthly Bulletin 2014, about 0.6 pp of HICP for +10 % oil) is not IO-based and not compared.

**Sanity table** (live, +10 % on B in every FIGARO economy, L1, mean price change across the 49 reported
countries): B 10.000 (exogenous), C19 5.007, C24 1.982, D35 1.909, C23 1.406, C20 1.148, H51 0.933,
C25 0.795, H49 0.652, H50 0.628 ... lowest: L 0.101, K65 0.098, N78 0.079, K64 0.062, T 0.000. Energy- and
material-intensive industries rank highest, services lowest. FRA household basket +0.387945 % (L1), range
0.117169-0.387945 % over θ ∈ {0.5, 0.75, 1}; of which imported products 0.104, foreign shocks 0.379.
Highest baskets: SRB 0.77, IDN 0.71, KOR 0.65; lowest: USA 0.21, CHE 0.22, LUX 0.22.

## 5. Live run, timings and memory

`tests/integration/test_io_tables_live.py` (`-m live`): throwaway database, live FIGARO ingest, dataset
build + registration, live WDI ingest (a release needs at least one eligible observation), live release,
one all-country scenario (49 countries, shocks in all 50 economies) through `handle_simulation` at L1 and
L2. Each phase in its own process (peak RSS measured alone; `OMP/OPENBLAS/POLARS` threads = 2).
Run on 2026-10-15, passed in 166 s:

| Phase | Seconds | Peak RSS |
|---|---|---|
| FIGARO ingest (27 requests, raw archive) | 77.5 | 371 MB |
| dataset build + identity checks + register_dataset | 32.4 | 646 MB |
| belief release (pins the dataset) | 2.2 | 161 MB |
| simulation job L1 (3,381 result rows) | 14.7 | 1,057 MB |
| simulation job L2 (16,839 result rows; 3 LU factorisations) | 26.3 | 1,055 MB |

All phases fit the worker's 2 GB. The simulation peak is dominated by the pinned Arrow table (233 MB),
column codes, the dense 3200² matrices (82 MB each: A, the endogenous block and its LU factors) and the
result rows; compute itself is about 2-4 s per θ.

## 6. Decisions taken (owner asleep; conservative)

1. FIGARO via the SDMX API (two economies per request) rather than bulk files: synchronous, archivable
   per request, within the daily request budget; no OECD ICIO fallback was needed.
2. Latest year discovered from the content constraint (2024); code lists pinned and drift is a blocker.
3. Identity tolerance one unit in the last published digit (0.001 million EUR); observed difference 0.
4. Gross output = row total (FIGARO publishes no separate P1 row); verified equal to the column total.
5. Exogenous-price formulation (shocked prices fixed at π, no feedback into them) rather than adding π as
   a primary-cost shock to the shocked industry: the intervention is "the price of B changes by π".
6. Shockable set limited to B, C19, D35, each documented; B is never called "crude".
7. θ applies to every non-shocked industry (uniform); the shocked industries keep the full π.
8. Basic-price basket (no taxes less subsidies on products, no purchases abroad); not a CPI. Each
   basket row also carries a labelled purchasers'-price reading with product taxes held fixed per unit
   (France 0.93 % → 0.82 %, EA12 pooled 0.95 % → 0.84 % for +10 % on B, C19, D35, θ = 1); ad valorem
   taxes would keep the basic-price figure, so the two bracket the tax treatment. Added after the
   scientific review V2 (M9); tested in `tests/unit/test_io_price.py`
   (`test_basket_is_basic_prices_and_carries_a_purchasers_price_reading_with_fixed_unit_taxes`).
9. Rest of the world may be a shock origin but is not reported (not a country, no world entity).
10. Countries without household consumption → typed not-applicable basket rows, not a failed run.
11. No external benchmark reproduced; comparisons in §4 are plausibility only.
12. Dataset subject key `WORLD` (one global table) and concept `economics.io.figaro_ii_table@1.0.0`.
13. Model registered as `candidate` with `review_status: maintainer_review_pending` (experimental label).

## 7. Files

* `src/reva/adapters/connectors/io_tables.py` — connector, parser, identity checks, dataset build/publish.
* `src/reva/models/economic/io_price.py` — plugin (registered at import; listed in `KNOWN_MODEL_MODULES`).
* `registry/concepts/io_tables.yaml`, `registry/models/methods.yaml` (entry `economic.io_price`).
* `src/reva/capabilities.py` — `economic.io_price` → implemented.
* Tests: `tests/unit/test_io_tables.py`, `tests/unit/test_io_price.py`,
  `tests/integration/test_io_price_pipeline.py` (db, synthetic), `tests/integration/test_io_tables_live.py`
  (live), fixtures `tests/fixtures/io_tables/`, `tests/fixtures/source/eurostat.figaro.iciot_ii/`.

## 8. Open items for other owners

* Lead (`src/reva/adapters/connectors/registry.py`): add `FigaroConnector()` to `builtin_connectors` so
  seed registers `eurostat.figaro.iciot_ii@1.0.0` (tests register it themselves today).
* HOOK (`src/reva/operations/jobs.py`): after a complete `eurostat.figaro.iciot_ii` ingest (cursor
  `complete` and `year`), call `io_tables.publish_io_dataset(worker_engine, store, year=cursor["year"],
  origin=...)`; refuse/report on `IdentityCheckFailed`. Optional schedule `ingest:eurostat.figaro.iciot_ii`
  every 30 days in `cli/seed.py DEFAULT_SCHEDULES` (27 requests per run).
* ENG/PUBFIX: `publish_release` refuses a release with no eligible observations even when datasets are
  eligible (a dataset-only release is impossible); fine today, noted.
* UIR: render `industry_price_change_percent:<industry>` keys and the label/experimental flag.
