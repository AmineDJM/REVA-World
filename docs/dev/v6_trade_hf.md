# V6TRADEHF — higher-frequency (monthly) merchandise trade

Agent V6TRADEHF, 2026-10-17. Goal: monthly trade series usable as predictors and forecast targets (as-of
vintages through atlas table versions), next to the annual trade layer that already exists.

## 0. Status

* **Built and tested with captured fixtures**: two atlas connectors, four monthly tables.
* **Live load not run**: free disk fell from 5.3 GB to **2.1 GB** while the connectors were being built (other
  agents' loads); the rule is no live ingest below 5 GB. `reva objects compress --dry-run` found nothing left to
  gain (DISKZ already compressed the archive; new puts are compressed on write). The live run is queued (section 6).
* Everything below marked "measured" was measured with direct requests on 2026-10-17 (probe files deleted).

## 1. Existing coverage (not duplicated)

| Layer | Owner | Frequency |
|---|---|---|
| `imf.imts` (IMF IMTS, ex-DOTS: bilateral goods exports FOB / imports CIF, 2000–2025, 209 reporters) | A-IMF `atlas_imf.py` | annual |
| `baci.*` (CEPII BACI HS92 bilateral by HS6/HS2/section) | A-TRADE `atlas_baci.py` | annual |
| UNCTADstat, OECD-WTO BaTIS services, OECD TiVA | A-TRADE / others | annual |
| UNSD HS 1992/2017/2022 codes + concordances (`hs2022:<code>` KG entities) | V4LINK `atlas.unsd_classif` | — |
| IMF BOP goods & services balance | V4ECON `atlas.imf_hf bop_q` | quarterly |
| link types `bilateral_trade`, `bilateral_trade_imports`, `bilateral_trade_baci` | LINKS | annual |

## 2. Sources

| Source | URL | Licence | Key? | Used |
|---|---|---|---|---|
| IMF IMTS (`IMF.STA:IMTS` "International Trade in Goods (by partner country) (IMTS)") | `https://api.imf.org/external/sdmx/2.1/data/IMF.STA,IMTS/…` | IMF terms (`imf.LICENCE`, PUBLIC_OPEN, citation) — same as A-IMF | no | **yes** (`atlas.imf_imts_m`) |
| Eurostat Comext `DS-045409` "EU trade since 1988 by HS2-4-6 and CN8" | `https://ec.europa.eu/eurostat/api/comext/dissemination/sdmx/2.1/data/DS-045409/…?format=TSV` | Eurostat reuse policy (CC BY 4.0, acknowledgement) | no | **yes** (`atlas.eurostat_comext`) |
| US Census international trade API (`api.census.gov/data/timeseries/intltrade/{exports,imports}/hs`) | — | US public domain | **yes**: every request without `key=` answers `302` → `missing_key.html` with header `X-DataWebAPI-KeyError: 1` (measured) | no — owner decision |
| UN Comtrade monthly | `comtradeapi.un.org/public/v1/preview/C/M/HS` answers without key but is capped at 500 records per call (measured: 1 record for USA→World 2026-06); full API needs a subscription key | UN Comtrade terms | yes (full) | no — owner decision |
| WTO Timeseries API | `api.wto.org/timeseries/v1/…` → **401** "missing subscription key" (measured) | WTO terms | yes | no — owner decision |

Bytes per full run (measured with direct requests): IMF world totals 2000-01..2026-06 **10.3 MB**; IMF bilateral
2026-01..2026-08 **37.0 MB** (378k observations, 68.6k series, 235 reporters); Comext per reporter ≈ 0.70 MB
(HS2, 2022-01..) + 0.15 MB (partners, 2024-01..), ≈ **23 MB** for 27 reporters + code list 0.09 MB. Total ≈ **70 MB**
(≤ 100 MB bound).

## 3. Atlas tables (month precision)

All cells: `period_start`/`period_end` = one calendar month; `flags.frequency = "M"`; `flags.seasonal_adjustment =
"not_stated_by_provider"` (neither provider states an adjustment); values are the provider's decimal strings; an
absent value is no cell (counted), never zero.

| Table | Content | Indicator | Units |
|---|---|---|---|
| `imf.imts_world_m` | every IMTS reporter × partner `G001` World, exports FOB / imports CIF, from 2000-01 (option `world_start`) | `XG_FOB_USD\|partner=G001`, `MG_CIF_USD\|partner=G001` | US dollars (full units; `flags.display_scale` = IMF SCALE, not applied) |
| `imf.imts_m` | every reporter × every partner, same flows, from 2026-01 (option `start`) | `<flow>\|partner=<IMF code>`; `flags.partner_iso3` | US dollars |
| `eurostat.comext_hs2_m` | 27 EU reporters × partners WORLD / EXT_EU27_2020 / INT_EU27_2020 × product TOTAL + every 2-digit chapter (01..99 minus 77, 98 refused by the API) × flow 1 IMPORT / 2 EXPORT × `VALUE_IN_EUROS`, `QUANTITY_IN_100KG`, from 2022-01 (option `hs2_start`) | `<indicators>\|flow=<1\|2>\|partner=<code>\|product=<TOTAL\|CC>` | `euro`, `100 kg` (from the indicator code) |
| `eurostat.comext_partner_m` | 27 EU reporters × every partner of the code list × TOTAL × flows × `VALUE_IN_EUROS`, from 2024-01 (option `partner_start`) | same | euro |

Flags kept verbatim: IMF `DERIVATION_TYPE` with its quoted label (every observation measured is `M` "Mixed-type
data": the IMF does not separate reported data from its own estimates per observation in IMTS), `STATUS`,
`OVERLAP`, `METHODOLOGY`, the IMF group attribute `SERIES_NAME` (e.g. "Exports of goods, Free on board (FOB), US
dollar") as `flags.series_name`; Comext partner label from the archived code list `CXT_FREE_ISO`, any provider flag
after a value (`p`, `e`, `c`) as `flags.provider_flag`.

Timeliness measured 2026-10-17: IMF IMTS bilateral latest month **2026-06** (USA), Comext latest **2026-08**
(Comext DS-045409 update 2026-10-16).

Geography: IMF codes through the existing `imf` scheme (KOS → XKX, WBG → PSE, aggregates such as G001 unmapped).
Comext uses ISO 3166 alpha-2 (Greece **GR**, United Kingdom **GB** — not the main-database `EL`/`UK`), so a new
scheme `comext` (`registry/atlas/geo_aliases/v6tradehf.yaml`, base `iso2`) adds only `XK` → XKX, quoted from the
code list ("Kosovo"). `XS` ("Serbia", obsolete Comext code), `QP`/`QR`/`QS`/`QV`/`QW`/`QY`, `XC`, `XL` and every
aggregate stay unmapped.

Vintages: each run that sees a changed release registers a new version of the table (atlas revision mechanism); the
IMF `UPDATE_DATE` and the Comext request window are in each table's coverage note, so as-of backtests can pick the
version that existed at a forecast origin. Monthly tables are not attached to the atlas year view.

Catalog: `registry/atlas/catalog/trade_hf.yaml` (domain `trade_hf`, theme `trade`, 6 provider-declared concepts:
IMF monthly exports/imports with the World; Comext exports/imports with WORLD and EXT_EU27_2020). IMF US dollars
and Comext euros are never put under one concept.

## 4. Products ↔ HS codes (by code only)

`eurostat.comext_hs2_m` cells with a 2-digit chapter `01`..`97` in a period from 2022-01 carry
`flags.hs_chapter_kg = "hs2022:<CC>"` (the CN of 2022 onwards is built on HS 2022 and keeps its chapters); the
dimension is declared in `registry/kg/v4link_atlas_dimensions.yaml` (`eurostat.comext_hs2_m`, dimension `product`,
prefix `hs2022:`). `TOTAL` and chapter `99` (CN special codes, no HS chapter) name nothing. No product label is
copied (the KG holds UNSD's HS labels, with V4LINK's WCO redistribution flag). Example path (once loaded):
`eurostat.comext_hs2_m` row `VALUE_IN_EUROS|flow=1|partner=EXT_EU27_2020|product=27`, reporter `FR` (iso3 FRA),
2026-03 → `hs2022:27` "Mineral fuels, mineral oils …" → corresponds_to (UNSD HS2022-HS1992) → `hs1992:27` ← BACI
`hs2=27` annual cells (V4LINK path 1) — chapter-level only, no weights computed.

## 5. Links

No monthly country↔country link kind was added. `src/reva/links/derive.py::derive_atlas_dyadic` builds
`Temporal("snapshot", year=…)`: a monthly table would give twelve links per pair and year with the same temporal
key, i.e. months collapsed into a year. The annual `bilateral_trade` / `bilateral_trade_imports` (IMF IMTS) already
give the country↔country edge; a monthly kind needs a month-grained `Temporal` in the links layer first (owner of
`derive.py`). The monthly cells keep `flags.partner_iso3` in the same convention, so such a kind is a registry
append (`indicator_prefix: "XG_FOB_USD|partner="`, `datasets: [imts_m]`) once months are supported.

## 6. Files, budgets, tests

* `src/reva/adapters/connectors/atlas_imf_imts_m.py` — `atlas.imf_imts_m` 1.0.0 (source key `imf.sdmx`): 1
  all-reporter world-total request (its `COUNTRY` set is the reporter list) + bilateral requests in chunks of 30
  reporters (an all-reporter bilateral request took 3 min 27 s; 30-reporter chunks ~27 s). A failed chunk is listed
  in the coverage note (table marked INCOMPLETE), never padded.
* `src/reva/adapters/connectors/atlas_eurostat_comext.py` — `atlas.eurostat_comext` 1.0.0 (new source key
  `eurostat.comext`, `registry/sources/eurostat_comext.yaml`): code list + per reporter 4 HS-chapter groups of ≤ 25
  chapters + 1 partner request (TSV). Measured: multi-reporter requests are **queued asynchronously** by the API
  (SOAP `syncResponse` / `SUBMITTED`), a 98-chapter single-reporter request took 96–128 s or was reset, 25-chapter
  requests 1–45 s. Each failed request is retried once in-run.
* Budgets: `registry/atlas/budget_requests/v6tradehf.yaml` → `budgets-1.3.38` (`imf.sdmx` 80 → 98, new
  `eurostat.comext` 300/day); digest re-pinned in `src/reva/autonomy/policy.py`.
* Fixtures (captured 2026-10-17, licence in each manifest entry): `tests/fixtures/source/atlas.imf_imts_m` (FRA,
  KOS; world 2025-01.., bilateral 2026-05..; 208 kB), `tests/fixtures/source/atlas.eurostat_comext` (code list + FR,
  GR from 2026-03; 0.4 MB).
* Tests: `tests/unit/test_v6tradehf.py` (11), `tests/integration/test_v6tradehf_ingest.py` (4, fixture ingest on a
  throwaway DB, idempotent re-run, KOS → XKX, GR → GRC). ruff + mypy clean.

## 7. Live run (queued — needs free disk ≥ 5 GB)

```bash
df -h /    # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.imf_imts_m                 # ~47 MB raw, ~5 min
REVA_HTTP_TIMEOUT_SECONDS=300 flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.eurostat_comext   # ~23 MB, 136 requests
```

On a bigger machine (longer bilateral history; ~3.5–4 MB raw per extra IMF month, ~0.35 MB per extra Comext month
for 27 reporters):

```bash
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.imf_imts_m --dataset imts_m --option start=2015-01
REVA_HTTP_TIMEOUT_SECONDS=300 flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.eurostat_comext \
  --option hs2_start=2015-01 --option partner_start=2015-01
```

(HS chapter → `hs2022:` links are written only for periods from 2022-01; earlier Comext chapters follow the CN of
their year and get no KG link.)

## 8. Owner decisions

1. **US Census international trade API key** (free registration at api.census.gov/data/key_signup.html): without
   it every request is refused. With a key, a connector could read monthly US exports/imports by HS2 × country
   (`timeseries/intltrade/exports/hs`, `COMM_LVL=HS2`, `ALL_VAL_MO`) — US public domain.
2. **UN Comtrade subscription key** for monthly HS bilateral trade beyond the 500-record public preview.
3. **WTO Timeseries API key** (401 without).
4. **IMF terms**: as for A-IMF, licence review pending (datasets PUBLIC_OPEN, citation requested).
5. **Links layer**: month-grained temporal in `derive_atlas_dyadic` before any monthly link kind (section 5).
