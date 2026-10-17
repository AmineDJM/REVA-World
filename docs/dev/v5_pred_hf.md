# V5 PRED-HF — daily market and reference series

Agent PRED-HF, 2026-10-17. Roadmap: `docs/roadmap/v5_prediction_engine.md` (PRED-HF row: daily series not covered by
V4ECON, which owns monthly/quarterly macro, nor by V4ENERGY, which owns monthly energy production/prices). Rules:
V4 common rules (no fabricated values, typed Missing, provenance to raw bytes, licence per value, ≤ 100 MB raw
archive, no live ingest below 5 GB free).

## 1. What was built

| Piece | File |
|---|---|
| Shared daily-series plumbing (day rows, strict decimals, as-of / publication / revision flags, policy-quote checks) | `src/reva/adapters/connectors/hf_daily_common.py` |
| ECB: euro reference FX rates, €STR, key ECB rates | `src/reva/adapters/connectors/atlas_hf_ecb.py` (`atlas.ecb_daily`, source key `ecb.stats`) |
| BIS: daily policy rates (+ series texts), daily nominal effective exchange rates (bulk flat files) | `src/reva/adapters/connectors/atlas_hf_bis.py` (`atlas.bis_daily`, `bis.bulk`) |
| US Treasury: daily par yield curve, daily par real yield curve | `src/reva/adapters/connectors/atlas_hf_ust.py` (`atlas.ust_yields`, `ustreasury.rates`) |
| EIA: daily spot prices (WTI, Brent, 9 petroleum products, Henry Hub) | `src/reva/adapters/connectors/atlas_hf_eia.py` (`atlas.eia_spot`, `eia.dnav`) |
| Bank of England: Official Bank Rate, SONIA and its percentiles | `src/reva/adapters/connectors/atlas_hf_boe.py` (`atlas.boe_daily`, `boe.iadb`) |
| New York Fed: EFFR, OBFR, SOFR (rate, percentiles, volume, FOMC target, revision marks) | `src/reva/adapters/connectors/atlas_hf_nyfed.py` (`atlas.nyfed_rates`, `nyfed.markets`) |
| Atlas concepts (75; themes `prices` 12 and `finance` 63) | `registry/atlas/catalog/daily_markets.yaml` |
| Source registry (rights, terms quotes, revision policies) | `registry/sources/{ecb_stats,bis_bulk,ustreasury_rates,eia_dnav,boe_iadb,nyfed_markets}.yaml` |
| Budgets | `registry/atlas/budget_requests/pred_hf.yaml`, merged into `budgets-1.3.23`, digest re-pinned in `src/reva/autonomy/policy.py` |
| Tests | `tests/unit/test_atlas_hf_daily.py` (25 tests, offline), `tests/integration/test_atlas_hf_daily_ingest.py` (throwaway DB, fixture mode, idempotence) |
| Fixtures (excerpts of the real 2026-10-17 bytes, licence per file) | `tests/fixtures/source/atlas.{ecb_daily,bis_daily,ust_yields,eia_spot,boe_daily,nyfed_rates}/` (≈ 0.5 MB in all) |

No migration, no KG entity (the concepts live in the atlas catalog; the series are measurements, nothing causal).

## 2. Semantics shared by every daily table

* **Day precision.** One row per (indicator, provider geography, day); period `[day, day+1)`, `flags.precision =
  "day"`. Nothing is averaged, carried forward or interpolated. A day the provider does not report is **no row**,
  counted in the run report: ECB `N/A`, empty CSV cells, EIA `-`/`--`/`NA`/`W`, NY Fed `"NA"`, BIS status
  `M: Missing value; data cannot exist` and `H: Missing value; holiday or weekend` — the BIS EER file *does* carry
  a number on holiday/weekend cells (320,516 of them), REVA never turns a flagged-missing cell into an observation.
* **Values** are the provider's text when it is already a strict decimal (`"18.60"` stays `"18.60"`, WTI
  `"-36.98"` on 2020-04-20 kept); otherwise normalised with `flags.value_normalised`.
* **As-of.** `flags.as_of` = UTC date REVA fetched the raw file. A re-fetch that changes any value registers a new
  version of the atlas table (the atlas keeps every earlier version, with its raw bytes): the table versions are the
  vintages. Daily market data are rarely revised, so a backtest that reads today's table for 1990 values is
  **final-vintage pseudo-real-time** (roadmap decision 4) unless it uses versions registered at the time.
* **Publication / revision.** `flags.publication` (provider rule, short), `flags.pub_lag` (machine code:
  `same_day`, `next_business_day`, `second_business_day`, `weekly_release`, `not_stated`) and
  `flags.revision_policy` on every row. The verbatim source sentences are in each connector's `RIGHTS[...]
  ["policy_quotes"]` and are **re-checked against the archived policy/terms page at every run**
  (`report.policy_quotes_found`; all 29 quotes found on 2026-10-17). Per-value revision marks exist only at the NY
  Fed (`flags.revision_indicator = "r"`, `flags.footnote_id`) and are kept verbatim.

| table | publication (provider) | pub_lag | revision policy (provider) |
|---|---|---|---|
| `ecb.fx_ref` | "usually updated at around 16:00 CET every working day, except on TARGET closing days"; "for information purposes only" | same_day | none stated |
| `ecb.estr` | "published on each TARGET2 business day based on transactions conducted and settled on the previous TARGET2 business day" (08:00 CET) | next_business_day | revised the same day, no later than 11:00 CET, only for errors > 2 bp (ECB €STR methodology PDF, quoted, not machine-checked) |
| `ecb.key_rates` | level in force per calendar day ("date of changes" series) | not_stated | none stated |
| `bis.cbpol_d`, `bis.eer_d` | bulk files replaced at each BIS release | not_stated | none stated |
| `ust.par_yield`, `ust.real_yield` | inputs "at or near 3:30 PM each trading day"; publication time not stated | not_stated | "Treasury reserves the option to make changes to the yield curve as appropriate and in its sole discretion"; method switch 2021-12-06 in `flags.curve_method` |
| `eia.spot_d` | weekly data-browser release; `flags.release_date` / `next_release_date` from the archived page | weekly_release | none stated |
| `boe.rates_d` | SONIA "published by authorised distributors at 9am … made freely available, via our Database by 10am on the business day after it is first published" | second_business_day (SONIA) | none stated |
| `nyfed.rates_d` | EFFR/OBFR ~9:00 a.m. ET, SOFR ~8:00 a.m. ET, business day after | next_business_day | same-day revision ~2:30 p.m. ET only if > 1 bp, footnoted |

* **Licence per value** (`flags.licence` on every row; `rights` on every table registration):

| table | licence | flag |
|---|---|---|
| `ecb.*` | ECB website copyright: free use, cite the ECB, state modifications; "The ECB does not charge for the €STR or license its use." | — |
| `bis.*` | BIS terms of permitted use (unrestricted with citation; no extra charge in commercial products) | — |
| `ust.*` | U.S. Government work (17 U.S.C. 105) | — |
| `eia.spot_d` | EIA public-domain statement, **but** EIA names "Refinitiv, an LSEG business" as the price source and warns that licensed third-party material "may be protected" | `flags.licence_review` on every row |
| `boe.rates_d` | UK Open Government Licence v3.0; SONIA rows carry the BoE attribution statement | — |
| `nyfed.rates_d` | NY Fed Terms of Use: use/distribution with the prescribed notice (filled in `flags.required_notice`); SOFR computed from DTCC-licensed data (`flags.data_licence_note`) | notice required |

* **Geography.** BIS `REF_AREA` (ISO alpha-2) → `iso3` through the crosswalk (aggregates such as `XM` stay unmapped);
  Treasury / NY Fed rows `US` → USA, BoE `GB` → GBR (the issuing jurisdiction). ECB rows are `U2` (euro area,
  unmapped). EIA rows carry the trading location printed in the EIA title (`Cushing, OK`, `Europe`, `New York
  Harbor`, `U.S. Gulf Coast`, `Los Angeles`, `Mont Belvieu, TX`, `Henry Hub`) — never mapped to a country by name.

## 3. Live load (dev DB `reva`, 2026-10-17, under `flock /tmp/reva-devdb.lock`)

All six sources ingested live (budget policy `budgets-1.3.24`, 94 requests, **35.3 MB raw archived** — well under
the 100 MB cap), 11 atlas tables, **2,023,365 rows**, every policy quote found (29/29):

| table | rows | span | content / notes |
|---|---:|---|---|
| `ecb.fx_ref` | 221,412 | 1999-01-04 … 2026-10-16 | 41 currencies (30 still quoted); 70,344 `N/A` cells → no row |
| `ecb.estr` | 12,628 | 2019-10-01 … 2026-10-15 | 7 series (WT rate, R25/R75, TT volume in EUR millions, NT, NB, VL) |
| `ecb.key_rates` | 30,456 | 1999-01-01 … 2026-10-17 | DFR, MLFR, MRR_FR, MRR_MBR (level in force per calendar day) |
| `bis.cbpol_d` | 638,911 | 1945 … 2026-10-13 | 49 areas; 69,861 status-M cells dropped |
| `bis.cbpol_d_series` | 167 | per series | COMPILATION / SOURCE_REF / SUPP_INFO_BREAKS / TITLE texts (`value_text`) |
| `bis.eer_d` | 801,278 | 1983-10-03 … 2026-10-13 | 90 nominal EER series (64 broad, 26 narrow); 320,516 status-H cells dropped |
| `ust.par_yield` | 99,824 | 1990-01-02 … 2026-10-16 | 37 yearly files, 14 tenors over time; 1,019 empty cells |
| `ust.real_yield` | 27,584 | 2003-01-02 … 2026-10-16 | 24 yearly files, 5 tenors |
| `eia.spot_d` | 90,255 | 1986-01-02 … 2026-10-14 | 12 series, release 2026-10-15 (next 2026-10-21); licence review flagged |
| `boe.rates_d` | 29,190 | 1975-01-02 … 2026-10-15 | Bank Rate 13,092; SONIA 7,526; 4 percentiles × 2,143 |
| `nyfed.rates_d` | 71,660 | 2000-07-03 … 2026-10-15 | EFFR 6,606 records (35 revised, `r`), OBFR 2,670 (1 revised), SOFR 2,133 (8 `NA` percentiles → no row) |

Examples (ids as stored): `ecb.fx_ref` `EUR_REF|currency=USD` / `U2` / 2026-10-16 = `1.1253` `USD per EUR`
(`flags.pub_lag = same_day`, `as_of = 2026-10-17`); `eia.spot_d` `EIA_SPOT|series=RWTC` / `Cushing, OK` / 2020-04-20
= `-36.98` `Dollars per Barrel` (selector: week "2020 Apr-20 to Apr-24", column Mon, byte offset in the archived
page); `bis.cbpol_d` `CBPOL` / `US` (iso3 USA) / 1954-07-01 = `1.13`, whose meaning is in `bis.cbpol_d_series`
`CBPOL_SERIES|attr=COMPILATION` / `US` ("From 19 Dec 1985 onwards: mid-point of the Federal Reserve target rate; from
1 Jul 1954 to 18 Dec 1985: US Fed Funds effective rate."); `nyfed.rates_d` `EFFR|field=percentRate` rows with
`flags.revision_indicator = "r"` are the NY Fed's same-day revised values. Catalog concepts point at these series,
e.g. `prices.daily.brent_spot` → `eia.spot_d.EIA_SPOT|series=RBRTE`, `finance.daily.policy_rate` →
`bis.cbpol_d.CBPOL`, `finance.daily.ust_par_10yr` → `ust.par_yield.UST_PAR|tenor=10 Yr`. PRED-REGISTRY already
resolves questions on `atlas.eia.spot_d` (Brent monthly mean) and `atlas.ecb.fx_ref` (USD/CNY/JPY monthly means)
and on `bis.cbpol_d`.

Not run here (lead's job): `reva atlas snapshot`, `reva links materialise`.

## 4. Missing, and why

* **Gold, silver, platinum, palladium daily prices: not taken.** The LBMA Gold/Silver Prices (ICE Benchmark
  Administration) and LPPM platinum/palladium prices are licensed benchmarks (FRED withdrew the LBMA series in 2022
  for that reason); no open, redistributable daily source was found. COMEX/NYMEX futures (CME) and the NYMEX
  natural-gas futures EIA republishes are likewise exchange-licensed: not taken. Monthly precious-metal prices exist
  in the World Bank Pink Sheet (V4ENERGY, `wb_cmo.*`, CC BY 4.0).
* **Other daily commodities** (metals, grains): no open daily source with clear redistribution terms identified;
  monthly prices are V4ENERGY's / V4ECON's.
* **BIS daily real EERs** do not exist (monthly only, A-FISCAL's `bis.eer`). BIS bulk files carry no
  per-observation publication date.
* **EIA spot prices: XLS workbooks not used** (BIFF8, no reader in the project); the HTML history pages carry the
  same numbers as printed. `PET.zip` bulk (55 MB) not used (disk). The EIA API v2 needs a key: not used.
* **US Treasury all-years CSV** answers 403 to non-browser clients; the documented per-year CSVs are used (62
  requests per run). Bill rates and long-term rates (`daily_treasury_bill_rates`, `daily_treasury_long_term_rate`)
  not taken yet (same connector pattern).
* **SONIA total volume (IUDZLT2)** not taken: its unit is not stated in the export.
* **No per-row availability instant.** Rows carry the provider rule (`publication`, `pub_lag`) and `as_of`; a
  timestamp per row would defeat flag interning (2 M rows). Consumers derive availability from `pub_lag`; vintages
  are table versions.
* Other open central-bank daily series (Federal Reserve H.15, Bank of Canada Valet, Riksbank, Norges Bank, SNB) are
  candidates for a later run with the same `hf_daily_common` plumbing.

## 5. Owner decisions

1. **EIA spot prices (Refinitiv/LSEG source)**: EIA's public-domain statement vs. its warning on licensed
   third-party material. Every `eia.spot_d` row carries `flags.licence_review`; decide whether REVA may redistribute
   these values (or show them only internally).
2. **NY Fed Terms of Use notice**: any page or export that shows `nyfed.rates_d` values must display the notice in
   `flags.required_notice` (UI/API work, not done here).
3. **LBMA / CME data**: a licence would be needed for gold/silver/futures daily prices.

## 6. Commands (re-run / bigger machine)

```bash
uv run pytest -q tests/unit/test_atlas_hf_daily.py tests/invariants/test_invariant_30_budgets.py
uv run pytest -q tests/integration/test_atlas_hf_daily_ingest.py         # throwaway DB, fixture mode
df -h /                                                                 # >= 5 GB free before a live run
for s in atlas.ecb_daily atlas.ust_yields atlas.eia_spot atlas.boe_daily atlas.nyfed_rates atlas.bis_daily; do
  flock /tmp/reva-devdb.lock uv run reva atlas ingest "$s"
done
# one table only: uv run reva atlas ingest atlas.bis_daily --dataset eer_d
```

Daily cadence (ECB, Treasury, BoE, NY Fed) and weekly (BIS, EIA) are declared in the specs; budgets allow one run
per day plus a retry. `atlas.bis_daily` streams 772 MB of inflated CSV twice (~2.5 min, ~1 GB RAM peak): run it
alone. Treasury requests grow by 2 per calendar year (budget 130/day).
