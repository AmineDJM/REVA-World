# V6 V6FINDATA — finance data with clean licences

Agent V6FINDATA, 2026-10-17. Plan: `docs/roadmap/v6_plan.md` (V6FINDATA row). Rules: V4 common rules (no fabricated
values, typed Missing, provenance to raw bytes, licence per value, never map by name, ≤ 100 MB raw archive, no live
ingest below 5 GB free). Existing coverage reused, not duplicated: A-FISCAL `atlas_bis.py` (`bis.tc` credit to the
non-financial sector, `bis.dsr` debt service ratios, `bis.credit_gap`, `bis.spp`, monthly `bis.cbpol` / `bis.eer`),
PRED-HF daily series (`bis.cbpol_d`, `bis.eer_d`, `ecb.fx_ref`, `ecb.estr`, `ecb.key_rates`, `ust.*`, `boe.rates_d`,
`nyfed.rates_d`), V4ECON / A-IMF IMF tables (`imf.il` reserves, `imf.mfs_ir_m`, `imf.qgfs_q`, WEO debt).

## 1. Table names for PRED-FIN (atlas `source.dataset`, indicator ids)

| table | indicator(s) | frequency / precision | geography | licence |
|---|---|---|---|---|
| `bis.cbta` | `CBTA\|freq=M\|unit_measure=USD\|transformation=B\|comp_method=B` (also `XDC` domestic currency, `XDF_R_B1GQ` % GDP; `freq=Q/A`) | month / quarter / year | 53 areas (ISO2→iso3; `XM` unmapped) | BIS terms (citation) |
| `bis.gli` | `GLI\|curr_denom=USD\|borrowers_sector=N\|lenders_sector=A\|l_pos_type=I\|l_instr=B\|unit_measure=USD` (USD credit to non-banks outside the US, geo `3P`), EUR/JPY variants, % GDP (`770`), growth (`771`) | quarter | aggregates + 25 areas | BIS |
| `bis.lbs_rep` | `LBS_REP\|l_measure=S\|l_position=C` (cross-border claims of banks located in the country), `…=L` liabilities, `l_measure=F` FX/break-adjusted changes | quarter | reporting countries | BIS |
| `bis.lbs_cp` | `LBS_CP\|l_measure=S\|l_position=C` (cross-border bank claims on residents of the country), `…=L` | quarter | ~250 counterparties | BIS |
| `bis.dsec_gov` | `DSEC\|ref_sector=S13\|valuation=M` (general-government debt securities, market value, USD bn), `S1311` central gov, `N` nominal, `F` face value | quarter | 53 areas | BIS |
| `imf.cofer` | `AFXRA\|fxr_currency=CI_USD\|type_of_transformation=SHRO_PT` (USD share of allocated reserves), `CI_EUR/CNY/JPY/GBP/…`, `TFXRA\|fxr_currency=CI_T\|type_of_transformation=NV_USD` (total reserves, USD) | quarter | `G001` world, `G110` AE, `G200` EMDE | IMF terms (review pending) |
| `imf.gdd` | `FL_S13_POGDP_PT` general gov debt % GDP, `FL_S1311MIXED_POGDP_PT`, `FL_PS_POGDP_PT` private, `FL_S11_POGDP_PT` NFC, `FL_S14_POGDP_PT` households, `F3T4_*`, `FL_S1ZS_POGDP_PT`, `FL_S11001_POGDP_PT` | year | 190 economies | IMF (review pending) |
| `imf.hpd` | `G63G_S13_POFYGDP` gross gen. gov. debt % fiscal-year GDP (from the 1800s) | year | 191 | IMF (review pending) |
| `ecb.wfs_w` | `WFS\|bs_item=T000000\|count_area=Z5\|currency_trans=Z01` (Eurosystem total assets), `A050000` MPO lending, `A070100` securities held for monetary policy, `L020100` current accounts, `L020200` deposit facility, `L010000` banknotes … (43 items) | ISO week (statement Friday) | `U2` | ECB copyright (free use, cite) |
| `ecb.lty_m` | `LTIR\|currency_trans=EUR` (10-year convergence yields; `BGN, CZK, DKK, HUF, PLN, RON, SEK, GBP` for non-euro) | month | EU states (ISO2→iso3) + `U2`, `I9`, `I10`, `D0`, `V5` | ECB |
| `ecb.yc_d` | `YC\|instrument_fm=G_N_A\|data_type_fm=SR_10Y` (AAA 10-y spot), `SR_2Y`, `G_N_C` all issuers | TARGET business day | `U2` | ECB (MTS/Fitch inputs noted) |
| `frb.h41_w` | Board series names, e.g. `RESPPMA_N.WW` total assets (Wednesday level), `RESH4A_XAW_N.WW` (week average) … 338 series | day (Wednesday level) / week (7 days ended Wednesday) | `US` | public domain (cite the Board) |
| `boe.weekly_w` | `RPWB55A` notes, `RPWB56A` reserve balances, `RPWZ4TM` APF loan, `RPWZOQ4` TFSME … (11) | day (Wednesday close) | `GB` | UK OGL v3 |
| `boe.bs_q` | `RPQB75A` total assets/liabilities, … (26) | day (quarter-end), published 5 quarters later | `GB` | UK OGL v3 |
| `kfrench.ff3`, `kfrench.ff3_d`, `kfrench.ff5`, `kfrench.mom` | `FF\|factor=Mkt-RF`, `SMB`, `HML`, `RMW`, `CMA`, `RF`, `Mom` (`\|freq=A` annual) | month / day / year | `US` (iso3 empty: a stock universe) | **no licence granted — restricted** |

Catalog: `registry/atlas/catalog/finance_v6.yaml` (17 provider-declared concepts, theme `finance`), e.g.
`finance.reserves.usd_share_allocated` → `imf.cofer.AFXRA|fxr_currency=CI_USD|type_of_transformation=SHRO_PT`,
`finance.cb.fed_total_assets_wednesday` → `frb.h41_w.RESPPMA_N.WW`, `finance.yields.ea_aaa_10y_spot` →
`ecb.yc_d.YC|instrument_fm=G_N_A|data_type_fm=SR_10Y`, `finance.debt.gg_debt_pct_gdp_gdd` → `imf.gdd.FL_S13_POGDP_PT`.

## 2. What was built

| piece | file |
|---|---|
| shared plumbing: `PeriodRows` (rows over any half-open period, `flags.precision`), ISO-week periods, streaming BIS flat-file reader | `src/reva/adapters/connectors/findata_common.py` (reuses PRED-HF `hf_daily_common`: metered fetch + archive, licence / as-of / publication / revision flags, quote re-check, registration) |
| BIS: `atlas.bis_fin` (API `?format=csv`, key `bis.stats`) + `atlas.bis_fin_bulk` (flat zips, key `bis.bulk`) | `atlas_bis_fin.py` |
| IMF: `atlas.imf_fin` (COFER, GDD, HPD; key `imf.sdmx`) | `atlas_imf_fin.py` |
| ECB `atlas.ecb_fin`, Fed `atlas.frb_h41` (new key `frb.releases`), BoE `atlas.boe_balance` (key `boe.iadb`) | `atlas_cb_balance.py` |
| Fama/French `atlas.ff_factors` (new key `kfrench.library`) | `atlas_ff_factors.py` |
| sources | `registry/sources/frb_releases.yaml`, `kfrench_library.yaml` (new); notes appended to `bis_stats`, `bis_bulk`, `ecb_stats`, `imf`, `boe_iadb` |
| budgets | `registry/atlas/budget_requests/v6findata.yaml` → `budgets-1.3.31` (frb.releases 8, kfrench.library 12; bis.stats 24, bis.bulk 14, ecb.stats 28, boe.iadb 16, imf.sdmx 80), digest re-pinned in `src/reva/autonomy/policy.py` |
| tests | `tests/unit/test_v6findata.py` (10, offline), `tests/integration/test_v6findata_ingest.py` (fixture mode, throwaway DB, every table, idempotent re-run, iso3 mapping) |
| fixtures | `tests/fixtures/source/atlas.{bis_fin,bis_fin_bulk,imf_fin,ecb_fin,frb_h41,boe_balance,ff_factors}/` — excerpts of the real 2026-10-17 bytes, licence per file (~0.43 MB) |

No migration, no KG statements (measurements only; nothing causal).

## 3. Semantics (every table)

* **Precision**: `flags.precision` = day / week / month / quarter / year, periods half-open as published; nothing
  averaged, carried forward or interpolated. Weekly stocks: Fed and BoE positions are **day** cells on the Wednesday
  ("Wednesday level", "close of Wednesday"); Fed week averages span the 7 days ended Wednesday; ECB weekly
  statements span the provider's ISO week (`flags.provider_period` `2026-W41`; the ECB says they relate to the
  Friday). BoE quarterly balance-sheet positions are day cells on the quarter-end date.
* **Missing is no row**, counted per reason in the run report: BIS status `M`/`H`/`L`/`Q`, `NaN`; ECB `M`/`Q`;
  Fed `OBS_STATUS=NA` (value `-999999`); empty BoE cells; IMF observations without `OBS_VALUE`; French `-99.99` /
  `-999`. Other statuses are kept with their quoted labels (`B: Break`, `E: Estimated value`, `P: Provisional`).
* **Units** verbatim; multipliers in the unit text (`x10^6`) and `flags.unit_mult`, never applied. IMF values are in
  full units (display `SCALE` in flags).
* **As-of / vintages**: `flags.as_of` = fetch date; a changed release registers a new table version and the atlas
  keeps every earlier version with its raw bytes — **table versions are the vintages** (final-vintage
  pseudo-real-time unless a backtest reads the version registered at the time). Provider release markers kept:
  IMF `flags.provider_update_date`, French `flags.provider_vintage` (CRSP vintage), BIS bulk `Last-Modified`
  (archived headers).
* **Publication / revision**: `flags.publication`, `flags.pub_lag` (`same_day` YC, `next_business_day` H.4.1 /
  BoE weekly, `weekly_release` ECB WFS, `five_quarters` BoE balance sheet, `not_stated` elsewhere),
  `flags.revision_policy`. Provider sentences re-checked against archived pages at every run
  (`report.policy_quotes_found`, all found in fixtures and live), e.g. ECB: "The weekly financial statements of the
  Eurosystem are published on a Tuesday, and they relate to the preceding Friday."; BoE: "This is published every
  quarter and includes data for five quarters earlier."; Fed: "is typically published on Thursday afternoon around
  4:30 p.m."; ECB yield curve: "It is updated every TARGET business day at noon (12:00 CET)."
* **Licence per value**: `flags.licence` on every row; `flags.licence_review` on IMF rows (terms page 403 from the
  container, as for A-IMF); `flags.data_licence_note` on ECB yield-curve rows (MTS Markets prices, Fitch ratings);
  `flags.licence_restriction = no_licence_granted` + `redistribution = not_established` on French rows, whose
  table rights carry `open_licence: false` (the atlas marks them restricted next to any answer).
* **Geography**: BIS/ECB ISO alpha-2 → iso3 through the crosswalk; aggregates (`5A`, `5J`, `XM`, `U2`, `I9`,
  `G001` …) unmapped. Fama/French rows: no geography scheme (the CRSP stock universe is not a country statistic).
* **Fail closed**: BIS API files carry codes only, so every folded code must be in the quoted BIS codelists
  (`L_MEASURE`, `L_POSITION`, `REF_SECTOR`, `VALUATION`, units); IMF folded codes must be in the quoted COFER /
  GDD / HPD codelists; ECB units in the quoted codelists; BoE series must state "(in sterling millions)".

## 4. Live load (dev DB `reva`)

LIVE_RESULTS

## 5. Not taken, and why

* **Equity and bond indices**: no open, redistributable source found. MSCI, S&P, FTSE, Bloomberg, ICE indices are
  licensed. **Shiller's US stock market data** (S&P Composite prices, dividends, earnings, CAPE): moved to
  `shillerdata.com` (unreachable from this client) with a commercial contact (The Index Standard); no licence stated
  and the prices are S&P-derived → not taken. IMF `MFS_FMP` (financial market prices: national share-price indices,
  incl. "Equities, Primary Market Instruments, Standard and Poor's") republishes exchange/index-provider data under
  IMF terms still under review → not taken (candidate after the IMF licence review). OECD share prices: same
  third-party question. Bond **yields** are covered instead (US Treasury par curve by PRED-HF; ECB AAA / all-issuer
  euro curve and 10-year convergence yields for every EU state here; IMF `mfs_ir_m` government bond yields by V4ECON).
* **Fama/French factors** are loaded but restricted (no licence; CRSP, Ibbotson and ICE BofA inputs) — owner decision.
* **Sovereign credit ratings**: no open source (S&P, Moody's, Fitch ratings are licensed; Wikidata has no maintained
  rating statements). Not taken; the ECB curve's use of Fitch ratings is noted only as provenance.
* **Crypto**: skipped (no open-licence reference price source; exchange data under exchange terms).
* **FRED**: unreachable from this client (and many FRED series are third-party licensed).
* **BIS consolidated banking (WS_CBS_PUB)**, **international debt securities (WS_DEBT_SEC2_PUB)**, **OTC derivatives**:
  bulk files 86–147 MB zipped; the needed summaries were taken from LBS and WS_NA_SEC_DSS instead. Candidates on a
  bigger machine (same `atlas_bis_fin` flow pattern, restricted keys).
* **H.4.1 per-district balance sheets** (Reserve Banks 1–12) and derived change series (`*_XCH*`): not taken (the
  consolidated series are the reference; changes are derivable). The Fed's Data Download Program announces its
  retirement; the connector uses the stable release-page file `FRB_h41_xml.zip`.
* **IMF vintage dataflows** (`COFER_2025_OCT_VINTAGE` …) not read (real-time vintages are PRED-VINTAGE's pattern).
* **IMF GDD observation `SOURCE` attribute not copied**: every observation of the 2025-09-17 release carries
  `SOURCE="NSATH"` (CL_ORGANIZATION "National Statistical Office (Thailand)") whatever the country — a placeholder,
  not a source. The series-level `DATA_SOURCE` text is kept (`flags.data_source`).
* **ECB Data Portal firewall** refuses keys with three consecutive dots (`W.U2...`, HTTP 400 "access blocked"); the
  connector lists the 43 weekly-statement items explicitly.
* **BoE weekly report** starts in 2014 (the reporting regime changed in 2014; the older Bank Return series are not in
  the same IADB codes); the IADB export refuses `Datefrom` before a series' start (error 905).

## 6. Owner decisions

1. **Fama/French factors**: copyright Fama & French, no licence, CRSP-derived. Loaded as restricted for internal
   benchmarking only; ask the authors (and check CRSP terms) before any publication / data snapshot export, or
   drop `kfrench.*` from exports.
2. **IMF terms** (COFER, GDD, HPD): same pending review as `registry/sources/imf.yaml` (`flags.licence_review`).
3. **Equity indices / sovereign ratings**: require a commercial licence (MSCI, S&P DJI, FTSE Russell, rating
   agencies) — no workaround attempted.
4. **ECB yield curve inputs** (MTS Markets prices, Fitch ratings): ECB publishes the curve under its website terms;
   flagged for awareness (`flags.data_licence_note`).

## 7. Commands (re-run / bigger machine)

```bash
cd /home/user/REVA-World
uv run pytest -q tests/unit/test_v6findata.py tests/invariants/test_invariant_30_budgets.py
uv run pytest -q tests/integration/test_v6findata_ingest.py           # throwaway DB, fixture mode
df -h /                                                               # >= 5 GB free, else stop
flock /tmp/reva-devdb.lock uv run reva seed                           # registers connector versions 1.0.1
for s in atlas.boe_balance atlas.ff_factors atlas.imf_fin atlas.bis_fin atlas.bis_fin_bulk atlas.ecb_fin atlas.frb_h41; do
  flock /tmp/reva-devdb.lock uv run reva atlas ingest "$s"
done
# one table only: uv run reva atlas ingest atlas.ecb_fin --dataset yc_d
# the lead then runs: reva atlas snapshot --skip-unchanged ; reva links materialise
```

Cadence declared: weekly (ECB, Fed, BoE), monthly (BIS, IMF, French). Each full run is one request per file plus
policy pages; `atlas.bis_fin_bulk` streams the 155 MB inflated CBTA member twice (~1 min), `atlas.frb_h41` the
127 MB H.4.1 SDMX-ML member (~1.5 min).
