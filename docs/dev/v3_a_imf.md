# V3 atlas — IMF layer (agent A-IMF)

Status: connector, catalog, geo aliases, fixtures and tests done. One live ingest into a throwaway
database (`reva_aimf_live_08e904b6`, dropped afterwards) on 2026-10-15: 29 requests, 166.9 MB of raw
bytes archived, 7 atlas tables registered, 0 failures, 7 min 19 s. The dev database `reva` was never
touched. Nothing is committed.

## Sources: what is open and stable (researched 2026-10-15)

| Option | Status | Used? |
|---|---|---|
| **IMF data portal SDMX 2.1 API** `https://api.imf.org/external/sdmx/2.1` | Open, no key, `PUBLIC_OPEN` datasets. It serves structure (dataflow/codelist) and data in SDMX-ML 2.1 (structure-specific XML is compact; CSV repeats every attribute on every row and is ~10x larger). Series-level metadata includes WEO `LATEST_ACTUAL_ANNUAL_DATA`. | **Yes: everything** |
| IMF DataMapper API `…/datamapper/api/v1` | Open JSON, WEO headline series only, no vintage label and no latest-actual year, so projections cannot be flagged. | No |
| WEO bulk files (`WEO<Mon><yyyy>all.xls`) | The new portal replaced them with the SDMX `WEO` dataflow (current vintage) plus `WEO_<yyyy>_<MON>_VINTAGE` dataflows for past vintages (`WEO_2025_OCT_VINTAGE`, `WEO_2026_APR_VINTAGE` are listed). | No (SDMX instead) |
| Legacy IFS / DOTS / `dataservices.imf.org` | Retired by the IMF in 2025. Their successors on the new portal are ER, MFS_IR and IL (marked with `IFS_FLAG`) and IMTS. | Successors used |

The IMF terms page (`imf.org/external/terms.htm`) returned HTTP 403 from the CDN inside the container, so
I could not quote it. The datasets carry `LICENSE="© International Monetary Fund Copyright. All Rights
Reserved."` and a suggested citation. Rights are recorded as *pending maintainer review* in
`registry/sources/imf.yaml`.

## What was built

* `src/reva/adapters/connectors/atlas_imf.py`: connector `atlas.imf` (source key `imf.sdmx`, template
  `atlas_bulk`). It has a streaming SDMX-ML parser that refuses DTD/entity declarations, one parser per
  dataset, `ingest()` (fetch, archive the raw bytes as an `evidence.acquisition`, parse, then
  `register_atlas_table`) and `register_connector()` for tests and throwaway databases.
* `registry/sources/imf.yaml` (source `imf.sdmx`), `registry/atlas/geo_aliases/a_imf.yaml` (scheme
  `imf`), `registry/atlas/catalog/imf.yaml` (63 concepts, domain `imf`),
  `registry/atlas/budget_requests/a_imf.yaml`.
* Fixtures `tests/fixtures/source/atlas.imf/` (8 captured live responses, 1.1 MB). Tests:
  `tests/unit/test_atlas_imf.py` (20 tests, offline), `tests/integration/test_atlas_imf_db.py` (3 tests,
  `db`: archive, register, read back, idempotent re-run) and `tests/integration/test_atlas_imf_live.py`
  (1 test, `live`, passed on 2026-10-15).

### Atlas tables (`source = imf`)

| dataset | IMF dataflow | content | rows (live) | economies (mapped) | years |
|---|---|---|---|---|---|
| `weo` | IMF.RES:WEO 9.0.0 = **WEO October 2026** (published 2026-10-13) | 30 indicators: GDP (national currency, USD, PPP, per capita), real growth, deflator, savings, investment, CPI and inflation (average and end of period), unemployment, employment, population, current account (USD and % GDP), export/import volumes, general government revenue/expenditure/balance/primary balance/gross and net debt | 237,117 | 196 (+14 unmapped: 13 aggregates, TWN) | 1980–2031 |
| `bop` | IMF.STA:BOP (BPM6) | current account, goods, services, goods & services, primary and secondary income; credit, debit and net; USD | 129,176 | 202 (+ANT, CWX, 2 aggregates) | 1948–2025 |
| `gfs_soo` | IMF.STA:GFS_SOO | revenue, expense, expenditure, net operating balance, net lending/borrowing; general government (S13) and budgetary central government (S1311); % of GDP | 23,753 | 142 | 1972–2025 |
| `er` | IMF.STA:ER (ex-IFS) | national currency per USD, period average and end of period | 28,420 | 213 | 1940–2025 |
| `mfs_ir` | IMF.STA:MFS_IR (ex-IFS) | policy-related rate (82 economies), discount (66), money market (89), deposit (140), lending (135) | 16,522 | 157 | 1950–2025 |
| `il` | IMF.STA:IL (ex-IFS) | total reserves (gold at market value), reserves excluding gold; USD | 22,844 | 183 | 1950–2025 |
| `imts` | IMF.STA:IMTS (successor of DOTS) | bilateral exports FOB / imports CIF, USD, reporter × partner × year | 1,525,117 | 209 reporters, 209 mapped partners, 40,093 mapped country pairs | 2000–2025 |

Parquet artifacts total 15.0 MB (`imts` 10.9 MB). One full run archives about 167 MB of raw bytes. The
throwaway store was deleted after the analysis.

Units are stored as the IMF reports them. `OBS_VALUE` is in **full units** (US dollars, persons,
national currency), not billions. The IMF display scale (`SCALE`, e.g. 9) is kept in
`flags.display_scale`, and values are never rescaled. The catalog spells every unit out: `US dollars`,
`% of GDP`, `% change`, `% per annum`, `national currency per US dollar`, `persons`, `international
dollars`. A cell with no `OBS_VALUE` produces no row (counted in parse stats). For example, 22,223 of
the GFS observations in the probe were empty.

WEO `BX`/`BM` (exports/imports of goods and services, USD) exist in the WEO for country groups only, so
they are kept in the table without a catalog concept. BOP `GS` credit/debit is the per-economy source.

### Projection and vintage labelling (WEO)

* Every WEO row carries `flags.vintage = "2026-10"`, and the coverage note says "WEO October 2026".
  The vintage comes from the message's `PUBLICATION_DATE` (no date → the parse is refused).
* `flags.latest_actual` is the series' own `LATEST_ACTUAL_ANNUAL_DATA` (the old "estimates start
  after" column). Every year after it carries **`flags.estimate_kind = "projection"`**: 31,960 rows in
  the live table.
* Fiscal-year labels (`FY2024/25`) are converted with the mapping the series states in
  `METHODOLOGY_NOTES` (`FY(t-1/t) = CY(t)` → 2025; `FY(t/t+1) = CY(t)` → 2024):
  `boundary_rule = fiscal_year_mapped` (387 series). When no mapping is stated, the earlier year is
  used, so a value can be marked projection wrongly but never actual wrongly:
  `fiscal_year_conservative` (54 series). Consequence: Bangladesh, Nepal and Pakistan show 2026 as
  actual (FY2025/26, which ended June 2026, is CY 2026 per WEO notes).
* PPP series (`PPPGDP`, `PPPPC`, `PPPEX`, 592 series) and aggregates (246 series) declare no latest
  actual year. Rows after the country's NGDP boundary, or from the publication year on, are
  `projection`. Earlier rows are `estimate_kind = "unknown_boundary"` (34,069 rows), never shown as
  plain observations.
* Distribution of NGDP latest actual: 2025 for 139 economies, 2024 for 24, FY2024/25 for 13, 2023 for
  8, FY2025/26 for 4, 2019 for 3, 2022 for 2, FY2020/21, FY2021/22 and 2010 for one each.
* Example (Algeria, WEO Oct 2026): real GDP growth latest actual is 2024, so 2025 3.93 % is a
  projection. The current account (latest actual 2025) is −8.88 % of GDP in 2025 (actual) and −0.75 %
  in 2026 (projection).
* Other datasets: `DERIVATION_TYPE` (`SP` staff projection, `SE*` staff estimate, `O` official…)
  and `STATUS` (`F` forecast, `E/e` estimate, `P` provisional, `B` break) go to flags. `SP/F` →
  `estimate_kind = "projection"`, `SE*/E/e` → `"estimate"`. In `imts`, 83,425 rows carry status `e`
  (IMF estimate). IMTS also contains IMF estimates for non-reporting economies that are *not* marked per
  cell: every observation has `DERIVATION_TYPE = M` (mixed). This is documented in the catalog notes.

### Geography

Scheme `imf` = ISO3 + `KOS → XKX` (IMF "Kosovo, Republic of") + `WBG → PSE` (IMF "West Bank and
Gaza"; the crosswalk PSE is the World Bank "West Bank and Gaza" entity). The evidence is quoted from the
IMF `CL_IMTS_COUNTRY` codelist. Kept unmapped and listed in each manifest: aggregates (G001 World, G110,
G163, …, U0xx UN regions, TXnnn "not specified"), IMF department areas, TWN (Taiwan Province of China,
not a crosswalk entity), CWX (Curaçao and Sint Maarten combined), and historical areas (ANT, CSK, DDR,
SCG, SUN, VDR, YAR, YMD, YUG). In `imts`, the partner stays verbatim in the indicator
(`XG_FOB_USD|partner=DEU`) and is mapped into `flags.partner_iso3`; unmapped partners get
`flags.partner_unmapped = true` (31 codes). For LINKS: a country–country edge is a row with non-null
`iso3` and `flags.partner_iso3`. Mirror flows (A→B exports as reported by A, B←A imports as reported by
B) are both present and differ, e.g. 2024 USA imports from CHN = 438.9 bn USD vs CHN exports to USA =
525.2 bn USD. They must not be merged or averaged.

## Trade surplus / deficit coverage (all 250 crosswalk entities)

| measure (latest year) | entities | latest year (mode) |
|---|---|---|
| WEO current account balance, % of GDP (`imf.current_account_balance_gdp`), actual years only | 195 | 2025 (150), 2024 (23), 2023 (12) |
| BOP goods balance, USD (`imf.goods_balance_usd`) | 202 | 2025 (151), 2024 (22), 2023 (10) |
| BOP goods & services balance / current account, USD | 202 | 2025 |
| IMTS merchandise exports and imports to/from the world (`imf.goods_exports_world_usd`, `…imports…`) | 209 | 2025 (all) |
| **any of the above** | **213** | |

* In 2024, 173 economies report a BOP goods balance: 59 surpluses and 114 deficits. In 2025, 193
  economies have a WEO current account value: 153 actual and 40 projection, 75 of them in surplus.
* WEO only, no BOP: LIE, SOM, TKM. BOP only, no WEO: AIA, BMU, CUW, CYM, FRO, MSR, NCL, PYF, SXM, TCA.
  IMTS only (merchandise X and M, no balance-of-payments measure): ASM, CUB, FLK, GIB, GRL, GUM, PRK,
  VAT.
* Stale BOP (last year before 2020): BRB 2017, CAF 1994, ERI 2000, FRO 2011, FSM 2014, GAB 2015,
  GNQ 1996, IRN 2000, MMR 2019, NCL 2016, PYF 2016, SYR 2010, TCA 2018, TCD 1994, YEM 2016. For most of
  these, WEO and IMTS have recent years.
* **No IMF measure (37 entities):** ALA, ATA, ATF, BES, BLM, BVT, CCK, CHI, COK, CXR, ESH, GGY, GLP,
  GUF, HMD, IMN, IOT, JEY, MAF, MCO, MNP, MTQ, MYT, NFK, NIU, PCN, PRI, REU, SGS, SHN, SJM, SPM, TKL, UMI,
  VGB, VIR, WLF. These are mostly uninhabited territories, French overseas departments (inside FRA in
  IMF data) and Crown dependencies. None appears as a mapped IMTS partner either. Where to look: UN
  Comtrade (MCO, territories as reporters), Eurostat (French DOMs), national statistics offices
  (PRI, GGY/JEY/IMN), WTO.
* IMTS export FOB minus import CIF is **not** the BOP goods balance (valuation and coverage differ).
  The catalog says so. Use `imf.goods_balance_usd` or `imf.current_account_balance_*` for
  surplus/deficit.

## Gaps vs World Bank

* World Bank economies in the crosswalk: 217. WEO economies mapped: 196, all of them WB economies.
  **WB economies missing from WEO (21):** ASM, BMU, CHI, CUB, CUW, CYM, FRO, GIB, GRL, GUM, IMN, MAF,
  MCO, MNP, NCL, PRK, PYF, SXM, TCA, VGB, VIR. Fillers: BOP covers BMU, CUW, CYM, FRO, NCL, PYF, SXM and
  TCA. IMTS covers ASM, CUB, GIB, GRL, GUM and PRK (merchandise only). WDI (A-WB) and UN sources
  (A-UN) cover the rest.
* Crosswalk entities in no IMF table (28): ALA, ATA, ATF, BES, BLM, BVT, CCK, CHI, COK, CXR, ESH, HMD,
  IOT, MAF, MCO, MNP, MYT, NFK, NIU, PCN, SGS, SHN, SJM, TKL, UMI, VGB, VIR, WLF.
* Concept gaps: unemployment (WEO `LUR`) for only 117 economies, so ILO modelled estimates (A-ILO) are
  the broader source. Employment (`LE`) covers 40. Net debt covers 89. GFS headlines cover 142 (S13
  for 115–118). The policy rate covers 82 economies: euro-area members have no national
  policy/money-market series (the ECB rate sits under the euro-area code) and France has no `ER`
  series (euro; the euro rate is published for the euro area).

## Requests for owners of shared files

1. **Lead (`src/reva/ports/connectors.py`)**: add `"atlas_bulk"` to the `ConnectorSpec.template`
   `Literal`. Until then `describe()` validates with a placeholder template and swaps it, which is the
   same approach as A-SCI; mypy needs `# type: ignore[arg-type]`.
2. **Lead (`src/reva/adapters/connectors/registry.py`)**: so that the seed registers the connector,
   add `from reva.adapters.connectors.atlas_imf import ImfAtlasConnector` and `ImfAtlasConnector()` to
   the `conns` list. Tests and the throwaway ingest use `atlas_imf.register_connector(owner_engine,
   store)` meanwhile.
3. **ATLASCORE (`registry/policies/budgets.yaml`)**: merge `registry/atlas/budget_requests/a_imf.yaml`
   (`imf.sdmx`: 60 requests/day, 400 MB/day). Until merged,
   `tests/invariants/test_invariant_30_budgets.py::test_budget_keys_are_registered_sources_and_cover_every_connector`
   lists `imf.sdmx` among the many new V3 sources without a budget (expected; same as the other atlas
   agents).
4. **Job wiring (agent D/HOOK)**: no job handler yet. A scheduled run would call
   `await atlas_imf.ingest(worker_engine, store, metered_http, origin="live")` with a `SafeHttpClient`
   for `api.imf.org`, metered through `reva.operations.budgets.metered` once the budget is merged. The
   throwaway run here was not metered, because the source is not yet in the pinned policy.

## How to re-run

```python
from reva.adapters.connectors import atlas_imf
atlas_imf.register_connector(owner_engine, store)            # once per database
res = await atlas_imf.ingest(worker_engine, store, http, origin="live")
# res.tables / res.stats / res.raws / res.failures; a failed request leaves its table unregistered
```

A new WEO vintage (April/October) becomes a new version of `atlas.imf.weo`, and its rows say which
vintage they are. Past vintages are available as separate dataflows (`WEO_2025_OCT_VINTAGE`,
`WEO_2026_APR_VINTAGE`) and could feed replay/vintage tests (H1). They are not ingested here.
