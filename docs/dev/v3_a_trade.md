# V3 A-TRADE — trade interconnections (BACI, UNCTADstat, OECD-WTO BaTIS)

Agent A-TRADE, 2026-10-15/16. Owner request: trade links between every pair of countries, by product, for
"pays excédentaires / déficitaires" and the "neurons" link layer. Status: three atlas sources on the ATLASCORE
platform (`atlas_source()` / `atlas_ingest(run)`, fetch only through `run.get` / `run.get_range`), catalog,
geo aliases, fixtures, offline + DB tests, one live ingest into a throwaway database (dropped afterwards). The
dev database `reva` was never touched. Nothing committed by this agent.

## 1. Sources researched (2026-10-15)

| Source | Access | Licence | Decision |
|---|---|---|---|
| **CEPII BACI** V202601 (released 2026-01-22), HS92 1995–2024 (also HS96…HS22) | one bulk zip per HS revision on `www.cepii.fr` (HS92: 2,417,732,497 B; HS17 795 MB; HS22 301 MB); **HTTP byte ranges honoured** (ETag, `Accept-Ranges`) | **Etalab Open Licence 2.0** ("Licence: Etalab 2.0" on the BACI page) | **used** (HS92, 13 years, byte ranges) |
| **UNCTADstat** bulk files `unctadstat-api.unctad.org/bulkdownload/<report>/<file>` | one **7z** (LZMA2) CSV per report, no key (HEAD → 403 from Cloudflare, GET works) | **CC BY 3.0 IGO** ("All materials … copyrighted under the Creative Commons Attribution 3.0 IGO license … may be copied freely … provided that United Nations Trade and Development Data Hub is cited", About/FAQ pages) | **used**: services by category, goods & services BPM6, merchandise 1948–2025, FDI, LSCI, bilateral LSCI |
| UNCTADstat datamart API | needs a registered client | — | no (bulk files instead) |
| UNCTAD creative goods `US.CreativeGoodsValue` | 335 MB 7z | CC BY 3.0 IGO | **no**: disk budget (documented gap) |
| **OECD-WTO BaTIS** (BPM6 edition *December2025*, 2005–2024, 202 economies, balanced bilateral services) | OECD SDMX REST `sdmx.oecd.org` (no key; one SDMX-CSV per year ~3.6 MB) or bulk zip 478 MB | OECD default **CC BY 4.0** for content from 2024-07-01; dataset-level statement not found (oecd.org → 403); joint OECD-WTO product | **used** (total services, balanced, all pairs); licence recorded `oecd_default_cc_by_4.0_dataset_statement_not_verified` — **maintainer review requested** |
| WTO Timeseries API `api.wto.org` | **401 "missing subscription key"** | WTO terms | **no** (key required; skip as instructed) |
| WTO tariff profiles / `ttd.wto.org` | 403 from proxy / key | — | no |
| IMF IMTS (bilateral merchandise totals) | already ingested by A-IMF (`imf.imts`, 2000–2025, 209 reporters) | — | not duplicated |

## 2. What was built

| Path | What |
|---|---|
| `src/reva/adapters/connectors/atlas_trade_common.py` | zip-by-range helpers (central directory incl. zip64, exact member ranges, **incremental inflate** with CRC-32/size checks, CSV re-blocking), a minimal **7z single-file reader** (LZMA/LZMA2; start-header, header and data CRCs checked; anything else refused) + writer for fixtures, exact scaled-integer decimal helpers, quarter periods |
| `src/reva/adapters/connectors/atlas_baci.py` | atlas source `atlas.baci` (source key `cepii.baci`): tail → central directory → country codes + Readme → 13 year members, each streamed and aggregated on the fly (HS6 never stored) → 6 tables `baci.*` |
| `src/reva/adapters/connectors/atlas_unctad.py` | atlas source `atlas.unctad` (`unctad.bulk`): 6 reports → 6 tables `unctad.*` with derived balances |
| `src/reva/adapters/connectors/atlas_trade_batis.py` | atlas source `atlas.oecd.batis` (`oecd.sdmx`, shared with A-ILO): dataflow (edition) + 20 yearly files → `oecd.batis_services` |
| `registry/sources/cepii_baci.yaml`, `registry/sources/unctad.yaml` | new source keys (rights, lineage, notes) |
| `registry/atlas/geo_aliases/a_trade.yaml` | schemes `baci` (Comtrade reporter codes + 10 territory aliases quoted from BACI's `country_codes_V202601.csv`), `unctad` (238 zero-padded M49 codes observed with labels + 412 Kosovo → XKX), `batis` (ISO3 + XKV → XKX) |
| `registry/atlas/catalog/trade.yaml` | 106 provider-declared country concepts, domain `trade` |
| `registry/atlas/budget_requests/a_trade.yaml` | `cepii.baci` 40/day 1.25 GB, `unctad.bulk` 15/day 35 MB, `oecd.sdmx` 50/day 80 MB |
| `tests/unit/test_atlas_trade.py` (11), `tests/integration/test_atlas_trade_ingest.py` (3, `db`) | offline; fixtures `tests/fixtures/source/atlas.baci/` (rebuilt excerpt zip served by byte range, 227 KB), `atlas.unctad/` (LSCI whole real file + rebuilt 7z excerpts of original lines, 330 KB), `atlas.oecd.batis/` (real responses restricted to FRA+USA+DZA+CHN+XKV+W, 2023–2024, + dataflow, 200 KB) |

Run: `uv run python -m reva.atlas.ingest atlas.baci|atlas.unctad|atlas.oecd.batis [--dataset D] [--fixture]`.

### BACI by byte ranges (provenance to raw bytes within the disk budget)

The full HS92 zip (2.42 GB) exceeds the 1.5 GB agent budget, so the connector never downloads it whole: it
requests the archive tail (64 KB, central directory), `country_codes_V202601.csv`, `Readme.txt` and one member
per **selected year** — **1995, 2000, 2005, 2010, 2015, 2017–2024** (13 members, 1.14 GB) — each with
`If-Range: <ETag>` so a replaced archive can never be mixed in. Every range is archived content-addressed exactly
as served (local header + deflate stream + data descriptor) and cited by `raw_sha256`; the parser checks the local
header name, CRC-32 and size against the central directory. Members are inflated in 8 MB steps and parsed in
32 MB CSV blocks (≈15 s and < 1 GB per year); tables are then built and registered **one at a time** (the first
live attempt that built all six tables at once was OOM-killed after every fetch; fixed, see §5).

Exactness: BACI values (thousand USD, ≤ 3 decimals) are summed as integer thousandths (= US dollars). About
120 of ~11 M values per year are printed with binary floating-point noise (`34.471000000000004`,
`3.3729999999999998`): only that pattern (≥ 6 zeros/nines after the 3rd decimal) is rounded to the thousandth it
denotes and **counted per cell** (`selector.rounded`); any other value with more decimals is refused.

## 3. Atlas tables

### `baci` (CEPII BACI HS92 V202601; unit `thousand US dollars (current)`; every cell `flags.estimate = "cepii_reconciled"`, FOB)

| table | indicators | content |
|---|---|---|
| `hs92_country` | `X\|section=S`, `M\|section=S`, derived `BAL\|section=S` (S = I…XXI, `NS` = BACI 9999AA, `TOTAL`) | per-country exports, imports and goods balance, by HS section |
| `hs92_pair_total` | `X\|partner=j\|section=TOTAL`, derived `BAL\|partner=j\|section=TOTAL` | exporter × importer totals and bilateral balances |
| `hs92_pair_section_1995_2010` / `_2011_2019` / `_2020_2024` | `X\|partner=j\|section=S` | exporter × importer × 21 sections |
| `hs92_pair_hs2` | `X\|partner=j\|hs2=CC` | 13 chapters: 10 cereals, 12 oilseeds, 26 ores, 27 mineral fuels, 28 inorganic chemicals/rare earths, 30 pharma, 71 precious stones/gold, 72 iron & steel, 84 machinery, 85 electrical, 87 vehicles, 88 aircraft, 93 arms |

Selectors: `{"aggregate":"sum(v)","filter":{"i":"842","j":"156","section":"XVI"},"member":"BACI_HS92_Y2024_V202601.csv","rounded":0,"rows":N}`;
derived rows: `{"derived":"sum(v | i=A, j=B) - sum(v | i=B, j=A)",...}` and `flags.inputs` naming both input indicators.

### `unctad` (UNCTADstat; CC BY 3.0 IGO)

`usd_millions|flow=02|category=SC` (exports 02 / imports 01, EBOPS category), `usd_millions_balance|category=S`
(derived, top-level categories only), `usd_millions|flow=..|series=3503|2102|2106` (goods / services / goods and
services, BPM6), `usd_millions|flow=..` (merchandise, 1948–2025), `usd_millions|flow=08|09|direction=1|2` and
`pct_gdp|…` (FDI flows/stocks inward/outward), `index` (LSCI, quarterly), `index|partner=…` (bilateral LSCI,
Q1 each year). Cells UNCTAD marks missing are omitted and counted in the coverage note; footnotes kept in
`flags.footnote` (`unctad_estimate: true` for "Estimated").

### `oecd.batis_services` (OECD-WTO BaTIS, balanced, millions USD)

`X|partner=…` for every reporter × partner (incl. aggregates W, WXD, WXOECD, WXEU27_2020, OECD, EU27_2020 as
partners) and derived `BAL|partner=…` between economies (aggregates excluded). `flags.estimate =
"oecd_wto_balanced"`, `obs_status` (I = imputed on nearly every cell), `batis_edition = December2025`.

## 4. Live ingest (2026-10-15/16, throwaway database `reva_atrade_live_86f5efc2`, dropped)

Platform path (`run_atlas_ingest(..., mode="live")`: reservation → metered SafeHttpClient → settle), with the
pinned budgets-1.3.4 policy plus exactly the per-source limits of `budget_requests/a_trade.yaml` (in-process, since
`cepii.baci` / `unctad.bulk` are not merged yet). All runs `completed`, 0 errors, 0 refused requests.

| source | requests | bytes | time | tables |
|---|---|---|---|---|
| `atlas.unctad` | 6 | 15.4 MB | 37 s | 6 |
| `atlas.oecd.batis` | 21 | 71.4 MB | 72 s | 1 |
| `atlas.baci` | 16 | 1,144.7 MB | 541 s | 6 |

Disk: archived raw + Parquet = **1.33 GB** in the throwaway store (BACI raw 1.14 GB, Parquet 100 MB), within the
1.5 GB agent budget; the store and all scratch copies were deleted afterwards.

| table | rows | mapped entities | unmapped (kept, listed) | years |
|---|---|---|---|---|
| `baci.hs92_country` | 188,754 | 226 | 490, 530, 58, 711, 736, 849, 891 | 13 years 1995–2024 |
| `baci.hs92_pair_total` | 731,019 | 226 | same | idem |
| `baci.hs92_pair_section_1995_2010` | 1,003,200 | 220 | same | 1995, 2000, 2005, 2010 |
| `baci.hs92_pair_section_2011_2019` | 1,342,826 | 225 | 490 | 2015, 2017–2019 |
| `baci.hs92_pair_section_2020_2024` | 1,688,033 | 225 | 490 | 2020–2024 |
| `baci.hs92_pair_hs2` | 1,790,912 | 226 | as country | 13 years |
| `unctad.services_cat` | 536,813 (77,939 derived) | 204 | groups (0000, 1400…), 158 | 2005–2025 |
| `unctad.goods_services` | 52,442 (17,480 derived) | 219 | groups, 158, 530, 736, 891 | 2005–2025 |
| `unctad.merch_total` | 62,245 (20,741 derived) | 222 | groups, 158, historical areas | 1948–2025 |
| `unctad.fdi` | 70,183 | 211 | groups, 158, historical | 1990–2025 |
| `unctad.lsci` | 14,724 | 187 | 158, 530, 736, 891 | 2006Q1–2026Q3 |
| `unctad.lsbci` | 655,370 | 187 | idem | Q1 2006–Q1 2026 |
| `oecd.batis_services` | 1,592,180 (792,030 derived) | 200 | W, OECD, EU27_2020, ANT_F, SCG_F | 2005–2024 |

BACI HS6 rows read: 5.0 M (1995) to 11.2 M (2023) per year; values with float noise rounded to the thousandth:
96–342 per year (cell-level count in `selector.rounded`).

### Coverage: pairs × years × sections (BACI, mapped exporter and importer)

| year | 1995 | 2000 | 2005 | 2010 | 2015 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| country pairs with trade | 17,512 | 24,890 | 27,670 | 29,939 | 31,445 | 32,179 | 32,238 | 32,085 | 31,404 | 32,265 | 32,074 | 31,869 | 29,454 |
| pair × section cells | 173,303 | 244,339 | 279,143 | 306,415 | 325,282 | 337,397 | 339,625 | 340,522 | 331,105 | 345,352 | 344,865 | 344,169 | 322,542 |
| countries with a goods balance | 206 | 218 | 218 | 220 | 225 | 225 | 225 | 225 | 225 | 225 | 225 | 225 | 225 |

All 21 HS sections appear every year (2024 pair counts: from 4,200 for XIX arms to 23,469 for XVI machinery).
BACI's legacy code 9999AA ("not specified") never occurs in V202601 HS92: the `NS` section is handled but has no
catalog concept. HS2 pairs 2024: 84 machinery 20,666, 85 electrical 20,558, 87 vehicles 15,546, 30 pharma 12,678,
71 precious stones/gold 10,883, 27 mineral fuels 10,733, 12 oilseeds 9,683, 28 inorganic chemicals 9,582, 72 iron
& steel 9,050, 88 aircraft 7,573, 10 cereals 6,847, 26 ores 4,114, 93 arms 4,088. 2024 has fewer pairs than 2023
(29,454 vs 31,869): late reporters, as usual for BACI's latest year.

### Surplus / deficit countries as computed (BACI goods, `BAL|section=TOTAL`, bn USD, CEPII reconciled FOB)

| year | top surpluses | top deficits | # surplus / # deficit |
|---|---|---|---|
| 1995 | JPN 139.0, CHN 93.5, DEU 52.1, CAN 30.6, ITA 23.2 | USA −136.5, HKG −123.6, GBR −33.1, SGP −28.7, ESP −23.6 | 66 / 140 |
| 2000 | CHN 184.2, JPN 146.1, DEU 77.9, RUS 69.0, SAU 44.8 | USA −388.8, HKG −150.6, GBR −86.4, ESP −45.0, SGP −36.5 | 71 / 147 |
| 2005 | CHN 412.7, DEU 196.8, JPN 159.1, RUS 134.2, SAU 105.1 | USA −774.5, HKG −228.6, GBR −158.5, ESP −101.0, BEL −62.0 | 67 / 151 |
| 2010 | CHN 705.9, DEU 201.8, RUS 155.3, JPN 144.9, SAU 137.0 | USA −722.5, HKG −369.0, GBR −197.1, IND −118.4, FRA −84.9 | 63 / 157 |
| 2015 | CHN 1053.9, DEU 254.2, RUS 151.6, KOR 119.3, JPN 90.1 | USA −807.1, HKG −475.7, GBR −244.0, IND −104.4, SGP −73.3 | 65 / 160 |
| 2017 | CHN 959.4, DEU 253.3, KOR 133.4, RUS 122.3, JPN 109.0 | USA −866.3, HKG −522.6, GBR −232.1, IND −139.2, FRA −65.9 | 67 / 158 |
| 2018 | CHN 990.1, DEU 259.5, RUS 205.1, SAU 159.2, KOR 113.0 | USA −947.1, HKG −531.1, GBR −225.6, IND −177.4, SGP −71.8 | 62 / 163 |
| 2019 | CHN 955.4, DEU 244.6, RUS 183.0, MYS 113.4, SAU 112.4 | USA −924.7, HKG −473.2, GBR −241.1, IND −152.6, SGP −80.3 | 69 / 156 |
| 2020 | CHN 964.5, DEU 195.8, MYS 119.9, RUS 109.0, IRL 105.5 | USA −943.5, HKG −454.3, GBR −247.5, FRA −85.6, IND −84.3 | 68 / 157 |
| 2021 | CHN 1106.3, RUS 217.1, DEU 182.7, MYS 136.7, SAU 115.0 | USA −1123.6, HKG −583.2, GBR −272.9, IND −171.7, FRA −125.7 | 71 / 154 |
| 2022 | CHN 1322.2, RUS 342.6, SAU 221.2, NOR 168.4, MYS 148.1 | USA −1248.1, HKG −540.8, GBR −314.0, IND −258.1, FRA −177.0 | 66 / 159 |
| 2023 | CHN 1241.2, RUS 194.6, DEU 158.0, SAU 131.2, MYS 116.7 | USA −1107.1, HKG −551.2, GBR −311.9, IND −218.3, FRA −132.9 | 63 / 162 |
| 2024 | CHN 1430.1, DEU 184.6, RUS 175.0, KOR 140.4, VNM 128.4 | USA −1311.6, HKG −538.0, GBR −319.8, IND −229.5, FRA −117.6 | 67 / 158 |

Reading notes (no causal claim): these are CEPII reconciled flows, so they differ from national customs balances and
from IMF BOP goods balances (A-IMF). Hong Kong (an entrepôt economy) shows a large BACI deficit; how BACI treats
re-exports was not checked here, so compare with IMF/UNCTAD before reading it as a national position. 2024 sections: mineral products (V) surplus RUS
239.8, SAU 209.7, AUS 186.5 / deficit CHN −672.5, JPN −183.9, IND −171.2; machinery & electrical (XVI) surplus CHN
1161.8, KOR 171.3, JPN 106.1 / deficit USA −572.9, HKG −384.1, ARE −75.6; precious stones & metals (XIV) surplus
ZAF 50.0, AUS 28.4, RUS 18.1; arms (XIX) surplus BGR 2.8, KOR 2.1, TUR 1.0 / deficit UKR −3.6, POL −1.3, ISR −1.2.
Largest bilateral goods surpluses 2024 (`BAL|partner`): CHN→USA 293.7, MEX→USA 254.4, CHN→HKG 245.2, CAN→USA 141.9,
VNM→USA 127.9, CHN→IND 105.1, CHN→MEX 100.7, CHN→GBR 78.6.

Services (UNCTAD, `usd_millions_balance|category=S`, bn USD): 2024 — 193 economies; surplus USA 327.3, GBR 235.2,
ESP 108.5, IND 105.5, ARE 64.7; deficit CHN −164.7, DEU −76.5, SAU −62.3, BRA −54.3, RUS −38.4. 2025 — 184
economies; surplus USA 327.7, GBR 269.3, IND 133.5, ESP 128.2, ARE 71.2; deficit CHN −116.0, DEU −83.1, SAU −59.3,
BRA −50.6, RUS −48.6. BaTIS bilateral services surpluses 2024 (balanced, bn USD): USA→IRL 111.2, BMU→USA 45.9,
GBR→USA 45.1, USA→CHN 42.3, USA→SGP 31.7.

### Coverage of the 250 crosswalk entities

* Any trade balance (BACI goods, UNCTAD services or merchandise): **232**. None (19): ALA, ATA, BVT, CHI, ESH,
  GGY, HMD, IMN, JEY, LIE, MAF, MCO, PRI, SGS, SJM, TWN, UMI, VAT, VIR — uninhabited territories, Crown
  dependencies, and areas reported inside another economy (BACI/Comtrade: MCO in France, PRI and VIR in the USA, LIE
  in Switzerland, SJM in Norway; UNCTAD likewise).
* BACI: 226 entities (no BACI code besides the above: FRO, GLP, GUF, MTQ, REU, XKX; TWN see next).
  **Taiwan**: BACI code 490 "Other Asia, nes" is in practice Taiwan but CEPII does not say so → kept unmapped (its
  flows are present as partner `490`, `flags.partner_unmapped`). Kosovo has no BACI code.
* UNCTAD merchandise: 222 entities from 1948 (159 with a balance in 1950, 218 in 2024); services 204; FDI 211.
* BaTIS: 200 reporters × partners, 39,800 mapped pairs per year (2024), includes XKX and TWN.

## 5. Problems met and decisions

* **BACI size vs disk budget**: whole HS92 zip 2.42 GB > 1.5 GB → byte ranges of 13 years (1.14 GB) with raw
  provenance preserved per member; other years are a one-line change of `YEARS` once disk allows (the remaining 17
  members are 1.27 GB).
* **OOM on the first live attempt**: building all six BACI frames before registering exceeded the container's free
  memory (process killed after every member had been fetched and archived; the throwaway DB of that attempt was
  dropped and the run repeated from scratch). Fix: tables built and registered one at a time, `pair_section` split
  into three year ranges (≤ 1.7 M rows each); the repeat run completed. The budget request therefore allows a full
  retry (`cepii.baci` 40/day).
* **BACI float noise** in ~0.001 % of values: rounded only when unambiguous, counted per cell; anything else refused.
* **UNCTAD 7z**: no 7z library in the environment; a minimal, strictly checked reader (single file, LZMA/LZMA2, CRCs)
  was written; any other 7z layout is refused.
* **Derived balances**: only from the same source, file, economy/pair and year, when both inputs exist (no zero
  filling); both input indicators named in `flags.inputs`, the selector points at both rows.

## 6. Gaps and where to look

* BACI years not archived (1996–1999, 2001–2004, 2006–2009, 2011–2014, 2016): disk budget only.
* Bilateral services by category (BaTIS has 26 EBOPS categories; only the total is taken: ~20× the rows).
* Creative goods (UNCTAD 335 MB), WTO tariff profiles (key required), WTO timeseries (key required).
* Product-level detail beyond sections + 13 chapters (HS4/HS6) is deliberately not stored.
* Microstates/territories without trade data (list above): national statistics (Monaco IMSEE, Guernsey/Jersey/Isle
  of Man), Eurostat (French DOMs), US Census (Puerto Rico).

## 7. Requests

1. **ATLASOPS/ATLASCORE**: merge `registry/atlas/budget_requests/a_trade.yaml` (`cepii.baci` 40/day, 1.25 GB;
   `unctad.bulk` 15/day, 35 MB; `oecd.sdmx` raised to 50/day, 80 MB). Until merged,
   `tests/invariants/test_invariant_30_budgets.py::test_budget_keys_are_registered_sources_and_cover_every_connector`
   lists `cepii.baci` and `unctad.bulk` (with other agents' unmerged keys); the other 8 invariant-30 tests pass,
   including the "no client outside a budgeted path" check for these modules. BACI alone uses ~1.15 GB of the 3 GiB
   global daily byte cap: schedule it yearly (January release) on a quiet day.
2. **Lead**: seed hook `register_atlas_connector_versions` for `atlas.baci`, `atlas.unctad`, `atlas.oecd.batis`.
3. **Maintainer**: confirm the BaTIS licence (OECD default CC BY 4.0 vs joint OECD-WTO terms); until then
   `oecd.batis_services` carries `licence_status = oecd_default_cc_by_4.0_dataset_statement_not_verified`.
4. **LINKS**: country–country edges = rows of `baci.hs92_pair_*`, `oecd.batis_services`, `unctad.lsbci` with
   non-null `iso3` and `flags.partner_iso3`; BACI/BaTIS flows are reconciled/balanced (A→B exports = B←A imports),
   unlike IMF IMTS mirror flows — keep them as separate edge sources.

## 8. Tests

`uv run pytest tests/unit/test_atlas_trade.py tests/integration/test_atlas_trade_ingest.py` — 14 passed
(exact sums against the raw excerpt with `Decimal`, derived balances, float-noise rounding/refusal, 7z/zip CRC
refusals, geo schemes, catalog series existence, platform fixture ingest + idempotence + `--dataset`, live run
refused without budget). ruff check/format clean.
