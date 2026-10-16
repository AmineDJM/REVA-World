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

LIVE_SECTION_PLACEHOLDER
