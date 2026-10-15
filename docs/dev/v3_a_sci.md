# V3 — A-SCI: science & technology atlas (OpenAlex, Nobel, WIPO) — coverage report

Agent A-SCI, 2026-10-15. Status: four atlas sources declared through ATLASCORE's protocol
(`atlas_source()` / `atlas_ingest(run)`, discovered by `reva.atlas.sources`; fetch only via `run.get`,
budget-metered live); catalog, fixtures and offline tests done; one live run through
`reva.atlas.ingest.run_atlas_ingest` into a throwaway database (`reva_asci_live_1016`, dropped
afterwards) for Nobel, WIPO bulk and GII; OpenAlex: see §2.1.

| file | role |
|---|---|
| `src/reva/adapters/connectors/atlas_openalex.py` | source `atlas.openalex` (`openalex.api`): `group_by=authorships.countries` aggregates -> `openalex.works_country_year`, `openalex.works_country_domain` (+ optional `works_country_field`) |
| `src/reva/adapters/connectors/atlas_nobel.py` | source `atlas.nobel` (`nobelprize.api`): laureates -> `nobel.laureates` counts + KG statements (P166 award received, P19 place of birth) via `reva.kg.model.load_batch` |
| `src/reva/adapters/connectors/atlas_wipo.py` | sources `atlas.wipo_ipstats` (`wipo.ipstats_bulk`, 6 tables) and `atlas.wipo_gii` (`wipo.gii`, `wipo.gii_2025`); stdlib xlsx reader (DTD/entity refusal, zip-bomb cap) |
| `src/reva/adapters/connectors/atlas_sci_common.py` | helpers (canonical JSON, frame builder, code digest) |
| `registry/atlas/catalog/science.yaml` | 123 provider-declared concepts (domain `science`) |
| `registry/sources/{openalex,nobelprize,wipo_ipstats,wipo_gii}.yaml` | source entries (rights, hosts, editorial status) |
| `registry/atlas/budget_requests/a_sci.yaml` | budget request (openalex.api 400/day, nobelprize.api 5, wipo.ipstats_bulk 5, wipo.gii 3) |
| `tests/unit/test_atlas_sci.py` (22 tests), `tests/integration/test_atlas_sci_ingest.py` (4 db tests, through `run_atlas_ingest`) | offline |
| `tests/fixtures/source/atlas.{nobel,wipo_ipstats,wipo_gii,openalex}/` | real captures or labelled excerpts of real captures (manifest `note`, `excerpt_of_sha256`) |

## 1. Sources researched

| source | licence (checked 2026-10-15) | access used | result |
|---|---|---|---|
| **OpenAlex** | CC0 | `GET /works?filter=publication_year:Y,type:article\|review[,primary_topic.domain.id:D]&group_by=authorships.countries&per_page=200&cursor=*` (+`mailto` = configured contact) | Since 2025 OpenAlex meters requests: without an API key, requests draw on a free daily budget **shared by everyone behind the same IP**; it was exhausted when first tried (HTTP 429 "Insufficient budget", `retry-after` to 00:00 UTC). Getting a key requires the owner to confirm by email (not done: owner away; the connector accepts `OPENALEX_API_KEY`). See §2.1 for the live result. |
| **Nobel Prize API v2.1** | CC0 (terms page: "free to use according to the Creative Commons Zero (CC0) license"; do not alter, cache locally, no trademarks) | one request `laureates?limit=1100` (3.9 MB, 1,028 laureates) | done |
| **WIPO IP Statistics** | CC BY 4.0 per WIPO Open Access Policy (site-level; files carry no licence) | the three "Complete data sets for Indicators by class and by technology" ZIPs (patents 4 MB, trademarks 17 MB, designs 1.5 MB) | done. Files dated **2023-12-06**, years 2000/2004–**2022**. The Data Center's own backend (`api.ipstatsdc…`) is undocumented and not used. |
| **Global Innovation Index 2025** | CC BY 4.0 (same policy; no file licence); underlying indicators are third-party | `wipo-pub-2000-2025-tech1.xlsx` ("Download Data") | done — composite levels only (Index, 2 sub-indices, 7 pillars, 21 sub-pillars; score + rank); the 78 third-party indicator values are **not** ingested |
| Scientific journal articles (WB IP.JRN.ARTC.SC), high-tech exports | — | — | A-WB's (skipped as instructed) |
| UNESCO UIS R&D, researchers | — | — | A-HEALTHEDU's |
| Space launches / satellites (optional) | — | — | not done (time spent on the mandatory sources). Candidates: UNOOSA Online Index (no documented bulk export), J. McDowell's GCAT (`planet4589.org`, CC-BY) — licence/format to verify before use. |
| WIPO total filings by origin (resident + abroad) | — | — | **not openly downloadable in bulk** (only interactive Data Center). WIPO-sourced WDI series `IP.PAT.RESD/NRES`, `IP.TMK.*`, `IP.IDS.*` (by office) come with A-WB's WDI bulk. |

## 2. Atlas tables (live run, throwaway DB)

| table | rows | indicators | countries mapped / 250 | years | unmapped provider geos (kept) |
|---|---|---|---|---|---|
| `nobel.laureates` | 2,893 | 26 | 83 (born) / 27 (affiliated) | 1901–2026 | `Taiwan` (not a crosswalk entity) |
| `wipo.gii_2025` | 8,580 | 62 | 139 | edition 2025 | — |
| `wipo.patent_pub_tech` | 663,584 | 3,015 | 199 | 2000–2022 | `**`, `AN`, `CS`, `DD`, `YU` |
| `wipo.patent_grant_tech` | 428,490 | 2,352 | 194 | 2000–2022 | `**`, `AN`, `CS`, `DD`, `YU` |
| `wipo.tm_appl_class` | 2,362,827 | 6,822 | 201 | 2004–2022 | `**`, `AN`, `CS`, `YU` |
| `wipo.tm_reg_class` | 2,381,783 | 6,781 | 200 | 2004–2022 | `**`, `AN`, `DD`, `YU` |
| `wipo.design_appl_class` | 202,802 | 3,997 | 186 | 2000–2022 | `**`, `AN`, `EU`, `YU` |
| `wipo.design_reg_class` | 202,591 | 3,881 | 187 | 2000–2022 | `**`, `AN`, `EU` |

Disk: raw + Parquet for all of the above = 63 MB (budget 1.5 GB). Ledger usage of the live run:
nobelprize.api 1 request / 3.9 MB, wipo.gii 1 / 0.9 MB, wipo.ipstats_bulk 3 / 22.8 MB (reserved =
consumed, 0 refused). Fixture mode (`--fixture`) replays excerpts: Nobel then accepts the 18-record
excerpt (threshold 1 instead of 1,000), OpenAlex runs only the recorded 2020 queries.

KG (Nobel): 2,007 statements loaded (1,036 `award_received` with `point_in_time` = award year,
motivation, portion, status, affiliations-at-award in qualifiers; 971 `place_of_birth` to the city QID
published by the Nobel API), 0 rejected, re-load idempotent (0 inserted, all unchanged). Subjects and
objects are the API's own Wikidata QIDs (every laureate has one). Prize QIDs (Q38104 physics, Q44585
chemistry, Q80061 medicine, Q37922 literature, Q35637 peace, Q47170 economics) were checked against the
`wikibase_item` of the English Wikipedia articles (the Wikidata API was in a machine-wide 429 cool-down).
No `kg.entity` rows are written from Nobel data (an entity snapshot from Nobel would replace the richer
Wikidata snapshot in `kg.entity_current`): **WDHARVEST/KGPEOPLE should snapshot the 1,028 laureate QIDs
and the 6 prize QIDs** (the subjects of `kg.statement` rows with `source_key = 'nobelprize.api'`).

### 2.1 OpenAlex

_Filled in after the live attempt at the OpenAlex budget reset (see the end of this section)._

## 3. What the numbers mean (honesty notes)

* **Nobel counts are REVA aggregations** of the provider's complete list (`flags.aggregation=count`,
  `computed_by=reva`), per laureate-prize record (Marie Curie counts in 1903 and 1911). Birth country is
  the API's **present-day** country (`countryNow`; born in Breslau -> Poland); Scotland and Northern
  Ireland -> GBR (flag `subnational_mapped`; the API itself gives Scotland the UK's QID Q145); affiliation
  counts use full counting. Organisations (Peace) have no birth country. Declined/restricted prizes are
  counted and flagged. A country without a row has no laureate in the list — not a reported zero.
* **WIPO cells are verbatim** (one cell per provider row; selector = file + row). Nothing is summed:
  technology fields may overlap, trademark/design figures are per-class counts (one filing may designate
  several classes), so per-origin totals cannot be derived from these files. `office` is folded into
  the indicator (`patent_publications|office=EP|tech=6`). The code `**` (as office and as origin) is
  **undocumented** in the download: kept verbatim, flagged `code_undocumented`, never mapped. Id `0` =
  "Unknown" (from WIPO's field-name table; assumed alike for classes, flagged). Historical origins
  (`YU`, `CS`, `DD`, `AN`) and `EU` stay unmapped.
* **GII** values are WIPO's stored decimal text (no float round-trip); `flags.dmc_not_met` keeps
  WIPO's data-minimum-coverage note; period = edition year (component data years vary).
* **OpenAlex** counts are full counting by affiliation country, articles + reviews, as indexed at
  retrieval (live index; drift recorded via `flags.retrieved_at`); `_all_works` and `unknown` rows keep
  the denominator and the no-country gap visible. Not comparable with WDI `IP.JRN.ARTC.SC` (Scopus,
  fractional counting) — both are shown per source, never blended.

## 4. Gaps and where else to look

| gap | alternative |
|---|---|
| WIPO bulk lacks 51 crosswalk entities — mostly territories (ABW, BMU, CYM, GIB, GRL, PRI, REU…), plus **PSE, TLS, XKX** | WDI IP.* (A-WB) by office; national office reports |
| WIPO bulk stops at 2022 and has no totals by origin | WDI IP.PAT.RESD/NRES (2023+ when WIPO updates WDI); GII 2025 sub-pillar "Knowledge creation" (score only) |
| GII covers 139 economies (missing e.g. AFG, COD, IRQ, PRK, SYR) | none open with the same definition |
| Nobel birth country absent for organisations; Taiwan unmapped | KG statements keep the laureate–prize link regardless of country |
| OpenAlex per-field series (26 fields) not run by default (request cost) | enable `field_years` once a key/budget exists |
| R&D expenditure, researchers per million | UNESCO UIS (A-HEALTHEDU), WDI GB.XPD.RSDV.GD.ZS (A-WB) |

## 5. Requests for owners of shared files

1. **lead** (seed): add ATLASCORE's `register_atlas_connector_versions(conn, store, act)` to the seed so
   the four A-SCI connector versions are registered in the deployment (tests use `ensure_registered`).
2. **WDHARVEST / KGPEOPLE**: entity snapshots for the 1,028 laureate QIDs and the 6 prize QIDs (§2).
3. **Owner**: an OpenAlex API key (free; requested by `POST https://api.openalex.org/api-keys` with the
   owner's email and confirmed via the emailed link — not done on the owner's behalf), exported as
   `OPENALEX_API_KEY`, removes the shared-IP daily limit.
4. Budgets: `registry/atlas/budget_requests/a_sci.yaml` was merged by ATLASCORE (budgets-1.3.2).

## 6. Running

`uv run python -m reva.atlas.ingest atlas.nobel | atlas.wipo_gii | atlas.wipo_ipstats [--dataset D] |
atlas.openalex [--dataset works_country_year]` (worker credentials of the configured database;
`--fixture` replays the recorded fixtures). The throwaway live run used `run_atlas_ingest` with a
`Settings` copy pointing at `reva_asci_live_1016` (created, bootstrapped and seeded like the test
fixture, `REVA_ENV=test` so the schema-contract check only warns about the in-flight 0009 migration),
`ensure_registered` as the migration owner, then the budget-metered live path as `reva_worker`.
