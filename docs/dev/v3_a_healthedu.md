# V3 atlas — health and education (agent A-HEALTHEDU)

Status: done. Two atlas sources (`atlas.who_gho`, `atlas.unesco_uis`) on ATLASCORE's atlas source platform,
catalogs, real fixtures, offline tests and one live ingest into a throwaway database (section 5).

## 1. Sources researched (2026-10-15)

| Source | Access used | Licence | Notes |
|---|---|---|---|
| **WHO Global Health Observatory** | OData API `https://ghoapi.azureedge.net/api` (documented at who.int/data/gho/info/gho-odata-api), one request per indicator, `$filter=SpatialDimType eq 'COUNTRY'`, fixed `$select` | CC BY 4.0 + WHO additional terms (data.who.int terms) | WHO's legacy page announced (for "end of 2025") a replacement OData implementation with standardised field names; no replacement URL is published and the current feed still serves 2026 data (life-expectancy rows dated 2026-10-01). A field rename is caught as schema drift (item quarantined, nothing registered). No bulk download exists for GHO; each indicator returns all rows in one response (no paging; largest ~38 MB). |
| **UNESCO UIS** | Public Data API `https://api.uis.unesco.org/api/public` (2024; SDMX API retired), `/versions/default` + batched `/data/indicators` queries pinned to that version (`version=20260507-91260335`, "February 2026 Data Release") | CC BY-SA 4.0 (databrowser.uis.unesco.org/terms-and-conditions) | No rate limit stated; **100,000 records per query** (HTTP 400 beyond) — 5 batches of < 55k records each. Alternative: Bulk Data Download Service (BDDS, multi-hundred-MB zips per theme). Values are served as JSON binary floats (some `97.29000091552734`): kept as served, never rounded by REVA. |
| IHME GBD | **skipped** | IHME free-of-charge **non-commercial** user agreement; download requires a registered account and accepting the agreement (click-through) | Not openly downloadable and not usable for commercial purposes; an agent cannot accept the agreement on the owner's behalf. If the owner wants GBD headlines, they must register and accept the terms themselves. |

## 2. What was built

* `src/reva/adapters/connectors/atlas_who.py` — atlas source `atlas.who_gho` (budget/source key `who.gho`,
  atlas table `who.gho`): 48 curated GHO indicators, one request each + the indicator list (49 requests).
* `src/reva/adapters/connectors/atlas_uis.py` — atlas source `atlas.unesco_uis` (`unesco.uis`, atlas table
  `uis.api`): 36 UIS indicators in 5 batched queries + the version request (6 requests).
* Both declare `atlas_source() -> AtlasSourceSpec` and `async atlas_ingest(run)` (docs/dev/v3_atlas_interface.md
  §7): no HTTP client is built in the modules; requests go through `run.get` (budget-metered live client or
  `FixtureHttp`), raw bytes through `run.archive`, tables through `run.register`. Run:
  `uv run python -m reva.atlas.ingest atlas.who_gho` / `atlas.unesco_uis` (`--fixture` replays the captured
  subsets). **Note:** that CLI writes to the database of the current settings; use a throwaway database.
* Shared `atlas_healthedu_common.py` (`fetch_archive_register`, `fit_flags`): every response is archived as
  soon as it is fetched; each archived body is read back from the store and checked structurally; **only if
  every item passed** are the archived bytes parsed and the table registered. A partial or drifted run
  registers nothing (a table version missing indicators would replace the complete one in the snapshot) and
  raises a typed `ValidationProblem` listing the failed items (raw bytes stay archived).
* UIS queries are pinned to the data version returned by `/versions/default` (follow-up items), so all
  batches come from one release.
* Catalogs: `registry/atlas/catalog/health.yaml` (56 concepts), `education.yaml` (36 concepts incl. `rd.*`
  research & development). All `provider_declared: true`.
* Sources: `registry/sources/who_gho.yaml`, `unesco_uis.yaml`. Budget request
  `registry/atlas/budget_requests/a_healthedu.yaml` — merged by ATLASCORE (budgets-1.3.2: `who.gho` 120/day,
  `unesco.uis` 30/day).
* Fixtures (real captures, 2026-10-15): `tests/fixtures/source/atlas.who_gho/` (6 indicators ×
  DZA/FRA/HKG/IND/NGA, 1.3 MB incl. the 415 KB indicator list), `tests/fixtures/source/atlas.unesco_uis/`
  (5 indicators × DZA/FRA/IND/NGA/TUR, 170 KB). Subsets = `FIXTURE_*` constants of each module.
* Tests: `tests/unit/test_atlas_healthedu.py` (17, offline), `tests/integration/test_atlas_healthedu_db.py`
  (3, fixture DB via `run_atlas_ingest(mode="fixture")`, idempotence, drift blocks registration),
  `tests/live/test_atlas_healthedu_live.py` (1, `-m live`, throwaway DB via the `db` fixture).

## 3. Units, dimensions and estimate types (honesty choices)

* **Dimensions are explicit in the indicator key.** WHO: every non-null GHO dimension is folded in API
  order — `WHOSIS_000001|sex=SEX_BTSX`, `NCDMORT3070|sex=SEX_FMLE|agegroup=AGEGROUP_YEARS30-69`,
  `MDG_0000000007|sex=SEX_BTSX|agegroup=AGEGROUP_YEARSUNDER5|wealthquintile=WEALTHQUINTILE_TOTL`. UIS: sex
  and age are part of the UIS id (`LR.AG15T99.F`); each catalog concept names them. Catalog concepts point
  at the both-sexes series (plus female/male concepts for life expectancy, literacy, mean years of
  schooling); all other sex/age/cause/wealth/residence series stay in the tables, addressable by key.
* **Units** are declared per indicator in the connector (as WHO/UIS name them, e.g. `per 100 000
  population (age-standardized)`, `% (crude)`, `per 10 000 population`) and checked against the catalog
  units by a test.
* **WHO comparable estimates vs country-reported data** — every WHO cell carries
  `flags.estimate_class`:
  * `who_comparable_estimate`: WHO or inter-agency modelled series made comparable across countries
    (GHE life expectancy/HALE/causes of death/suicide, UN IGME child mortality, MMEIG maternal mortality,
    NCD-RisC obesity/hypertension, WUENIC immunisation, JME stunting, JMP water/sanitation, WHO
    TB/malaria/air-pollution/road-safety estimates, UNAIDS HIV, WHO tobacco and alcohol estimates, UHC
    index);
  * `country_reported`: NHWA workforce densities, hospital beds, skilled birth attendance, the
    country-reported maternal mortality ratio (MDG_0000000032, 1987-2009);
  * `country_reported_with_who_estimates`: GHED health expenditure (country health accounts where
    reported, WHO estimates otherwise — not separable per cell in the API).
  `flags.producer` names the estimation group; `classified_by` says the class is this agent's catalog
  classification from WHO metadata (not a per-row WHO field).
* `value` = WHO `NumericValue` / UIS `value` exactly as served (decimal text; trailing zeros stripped).
  WHO's rounded display string and uncertainty bounds are kept in `flags.display_value`, `flags.low`,
  `flags.high` (`interval_level: not_stated_per_row`). WHO rows with only a display string (country-
  reported MMR; Hong Kong/Macao road deaths `"0.0"`) keep it in `value_text` with
  `numeric_value_absent: true` — never parsed into a number.
* WHO row comments are kept verbatim (`flags.comments`, e.g. "The UN IGME estimates are not the official
  statistics of India…", "estimates for 2020 and 2021 may be underestimated"); `flags.projection: true`
  only where WHO's comment says "This is a projection" (tobacco series run to 2030).
* UIS `qualifier` (`UIS_EST`, `NAT_EST`), `magnitude` (`NIL`: a 0 that UIS reports as nil/negligible) and
  footnotes (data source, partial coverage, national literacy definitions) are kept verbatim; UIS
  `estimate_class`: `country_reported`, `uis_modelled` (`CR.MOD.*`), `uis_estimate` (mobility ratios).
* Flags longer than 4,000 characters are shortened with `notes_truncated: true` (never silently).
* Catalog fallbacks to `wb.wdi_social.*` are listed only where the World Bank series is a redistribution
  of the same WHO/UIS series with the same definition and unit (e.g. `SH.STA.MMRT`, `SE.ADT.LITR.ZS`);
  they share the lineage (not independent evidence). Not listed: WB life expectancy (UN WPP, different
  model), WB physicians (per 1,000, different unit), WB primary completion (different definition).

## 4. Coverage (live ingest 2026-10-15)

Crosswalk: 250 entities (`registry/entities/country_crosswalk.json`; Taiwan is not in it — WHO and UIS do not
publish Taiwan either).

| table | rows | indicator series (folded) | countries | years | numeric / text cells | unmapped |
|---|---|---|---|---|---|---|
| `who.gho` | 606,051 | 193 (48 GHO codes) | 230 | 1931–2030 | 605,881 / 170 | none |
| `uis.api` | 118,837 | 36 | 225 | 1970–2025 | 118,837 / 0 | `XDN` "Sudan (pre-secession)", 7 rows (kept, not mapped to SDN) |

Per concept (countries with ≥ 1 value; in brackets: with a value in the last 5 available years):

* **WHO GHE series** (life expectancy, HALE, adult mortality, NCD 30–70, NCD rate, causes-of-death shares,
  suicide): 185 countries (185), 2000–2021/2023. **Child mortality** (UN IGME): 200, back to 1931 for some.
  **MMR** (MMEIG): 195, 1985–2023; country-reported MMR: 169 cells, 1987–2009, all text.
* Health expenditure (GHED), UHC index, immunisation (WUENIC): 195 (195). Workforce (NHWA): 191–194
  (169–183). Hospital beds: 193 (147). Skilled birth attendance: 193 (148).
* Obesity (adults/children): 199; hypertension: 194 (1990–2019 only); alcohol: 207; tobacco: 167
  (2000–2030, years after the last survey are WHO projections); stunting: 162; TB: 197; HIV 15–49: 145
  (UNAIDS publishes no estimate for many low-prevalence/high-income countries); malaria: 110 (endemic
  countries only — absence is not "malaria-free").
* Environment: PM2.5: 227; water/sanitation (JMP): 226; air-pollution deaths: 181–185 (2021 only); WASH
  deaths: 183 (2019 only); road deaths: 195–197 (2021 only).
* Education: GER primary 221 (195), out-of-school rates 203–217, net enrolment 218, expenditure % GDP 206
  (183), mean years of schooling 207 (130), school life expectancy 205, tertiary GER 206 (151);
  completion rates (surveys) 159–167, modelled 164–165 (all recent); literacy 179–182 (102–106);
  minimum proficiency (lower secondary) 94–103.
* R&D: GERD % GDP 156 (110), researchers per million FTE 146 (102), female researchers 153 (102).

## Gaps and where else to look

| gap | why | alternative (owner / status) |
|---|---|---|
| 20 crosswalk entities in no WHO series and 25 in no UIS series: uninhabited/dependent territories (ATA, BVT, HMD, SGS, UMI, IOT, ATF, CXR, CCK, NFK, PCN, SJM, ALA, BLM, MAF, GGY, JEY, CHI, ESH, VAT; UIS also BES, IMN, MNP, MYT, XKX) | not WHO/UNESCO reporting units | none needed for most; Kosovo (XKX): World Bank WDI (A-WB), Eurostat enlargement data |
| WHO GHE (life expectancy, HALE, causes of death) missing 11 member states with < 90,000 inhabitants (AND, COK, DMA, KNA, MCO, MHL, NIU, NRU, PLW, SMR, TUV) and territories (HKG, MAC, GRL, FRO, French overseas departments GLP/GUF/MTQ/REU, …) | GHE excludes small populations | **UN WPP 2024** (A-UN; all 237 areas; different model → its own concept, never blended); national/Eurostat life tables for overseas departments (FRA DOM via INSEE), HKG/MAC Census & Statistics Departments |
| Literacy absent for most high-income countries (AUS, CAN, DEU, FRA, GBR, JPN, USA, Nordics…) | not collected by UIS (universal schooling assumed) | OECD PIAAC adult skills (different construct: proficiency levels, not literacy) — not ingested; never imputed as 100 % |
| R&D (GERD, researchers) missing for ~70 countries, mostly small/low-income | no R&D survey | OECD MSTI (OECD + Chinese Taipei, Argentina, China, Romania, Russia, Singapore, South Africa); RICYT (Latin America); Eurostat `rd_e_gerdtot` |
| Learning outcomes (minimum proficiency) for ≤ 103 countries | assessment participation | PISA/TIMSS/PIRLS microdata (OECD/IEA; licence-restricted micro, open aggregates) — not ingested |
| HIV prevalence absent for 80 entities incl. CHN, RUS, USA, JPN | UNAIDS does not publish 15–49 prevalence for them | UNAIDS AIDSinfo (same lineage, more indicators); national surveillance |
| Taiwan (not in crosswalk) | not a WHO/UNESCO member | Taiwan MOHW / Ministry of Education statistics; OECD MSTI for R&D — needs a crosswalk decision (ALLC) first |
| IHME GBD (cause-specific burden for all locations incl. subnational) | licence: non-commercial agreement + login | owner decision (section 1) |
| Historical Sudan (XDN, UIS) | historical unit | KG historical states (KGHIST); kept unmapped in the table |

## 5. Live ingest

`uv run pytest -m live tests/live/test_atlas_healthedu_live.py` (2026-10-15 22:51–23:01 UTC), throwaway test
database (created and dropped by the `db` fixture; the dev database `reva` was never touched), through
`run_atlas_ingest(mode="live")` → `reserve_ingest` → metered SafeHttpClient → `settle_metered`:

| source | requests (reserved/used/refused) | raw archived | Parquet | registered |
|---|---|---|---|---|
| `atlas.unesco_uis` | 6 / 6 / 0 | 34.6 MB | 1.6 MB | `atlas.uis.api`, 118,837 rows, UIS version 20260507-91260335 ("February 2026 Data Release") |
| `atlas.who_gho` | 49 / 49 / 0 | 259.8 MB | 16.3 MB | `atlas.who.gho`, 606,051 rows |

Per full run ≈ 295 MB raw + 18 MB Parquet (budget ≤ 1.5 GB: one archived vintage of each fits five times;
WHO raw is the cost driver — consider keeping only the latest WHO vintage). Duration ≈ 10 min (WHO 49
sequential requests at ≥ 1 s spacing; parsing 606k rows ≈ 20 s). The scratch object store of the run was
deleted afterwards.

An earlier attempt (before ATLASCORE re-pinned `budgets.yaml` after merging the request) was refused by the
budget gate (`policy budgets.yaml changed … gates refuse until a maintainer re-pins it`): fail-closed as
designed; nothing was registered.

## 6. Requests to other owners

* **lead (seed)**: call `reva.atlas.sources.register_atlas_connector_versions` in `reva.cli.seed` (as
  documented by ATLASCORE) so `atlas.who_gho@1.0.0` and `atlas.unesco_uis@1.0.0` are registered in the
  deployment; and schedule `atlas_ingest` jobs: `atlas.who_gho` monthly, `atlas.unesco_uis` monthly (UIS
  releases twice a year; an unchanged version re-registers nothing).
* **ATLASCORE**: none outstanding (budget keys merged and pinned).
* **A-WB**: catalog fallbacks reference `wb.wdi_social.<code>` for 23 WDI series that are WHO/UIS
  redistributions (SH.*, SP.DYN.IMRT.IN, SE.*); if WDI's split puts any of them elsewhere, the key must follow.
* **A-SCI**: R&D concepts live in `education.yaml` as `rd.gerd_gdp`, `rd.researchers_per_million`,
  `rd.female_researchers_share` (tag `science`) — reference them rather than duplicating.
* **A-UN / A-QOL**: life expectancy from UN WPP / UNDP HDI and mean years of schooling from UNDP are
  different series; give them their own concepts (do not add them to the WHO/UIS concepts' source_order).
