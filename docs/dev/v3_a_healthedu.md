# V3 atlas — health and education (agent A-HEALTHEDU)

Status: connectors, catalogs, fixtures and offline tests done; live ingest: see section 5.

## 1. Sources researched (2026-10-15)

| Source | Access used | Licence | Notes |
|---|---|---|---|
| **WHO Global Health Observatory** | OData API `https://ghoapi.azureedge.net/api` (documented at who.int/data/gho/info/gho-odata-api), one request per indicator, `$filter=SpatialDimType eq 'COUNTRY'`, fixed `$select` | CC BY 4.0 + WHO additional terms (data.who.int terms) | WHO's legacy page announced (for "end of 2025") a replacement OData implementation with standardised field names; no replacement URL is published and the current feed still serves 2026 data (life-expectancy rows dated 2026-10-01). A field rename is caught as schema drift (item quarantined, nothing registered). No bulk download exists for GHO; each indicator returns all rows in one response (no paging; largest ~38 MB). |
| **UNESCO UIS** | Public Data API `https://api.uis.unesco.org/api/public` (2024; SDMX API retired), `/versions/default` + batched `/data/indicators` queries pinned to that version (`version=20260507-91260335`, "February 2026 Data Release") | CC BY-SA 4.0 (databrowser.uis.unesco.org/terms-and-conditions) | No rate limit stated; **100,000 records per query** (HTTP 400 beyond) — 5 batches of < 55k records each. Alternative: Bulk Data Download Service (BDDS, multi-hundred-MB zips per theme). Values are served as JSON binary floats (some `97.29000091552734`): kept as served, never rounded by REVA. |
| IHME GBD | **skipped** | IHME free-of-charge **non-commercial** user agreement; download requires a registered account and accepting the agreement (click-through) | Not openly downloadable and not usable for commercial purposes; an agent cannot accept the agreement on the owner's behalf. If the owner wants GBD headlines, they must register and accept the terms themselves. |

## 2. What was built

* `src/reva/adapters/connectors/atlas_who.py` — `WhoGhoConnector` (48 curated GHO indicators,
  atlas table `who.gho`), `src/reva/adapters/connectors/atlas_uis.py` — `UisConnector` (36 UIS
  indicators in 5 batches, atlas table `uis.api`), shared `atlas_healthedu_common.py`
  (`run_and_register`, `AtlasConnectorSpec`, `fit_flags`, `register_connector`).
* Flow: `run_connector` archives every raw response as an `evidence.acquisition` (zero observation
  batches; drift → quarantine); **only if every item is archived un-quarantined** the archived bytes are
  read back from the artifact store, parsed into the ATLASCORE long schema and registered with
  `register_atlas_table`. A partial run registers nothing (a table version missing indicators would
  otherwise replace the complete one in the atlas snapshot); the result is `status: blocked` with the
  failed items.
* Catalogs: `registry/atlas/catalog/health.yaml` (56 concepts), `education.yaml` (36 concepts incl.
  `rd.*` research & development). All `provider_declared: true`.
* Sources: `registry/sources/who_gho.yaml`, `unesco_uis.yaml`. Budget request:
  `registry/atlas/budget_requests/a_healthedu.yaml` (`who.gho` 120/day, `unesco.uis` 30/day).
* Fixtures (real captures, 2026-10-15): `tests/fixtures/source/atlas.who_gho/` (6 indicators ×
  DZA/FRA/HKG/IND/NGA, 1.3 MB incl. the 415 KB indicator list), `tests/fixtures/source/atlas.unesco_uis/`
  (5 indicators × DZA/FRA/IND/NGA/TUR, 170 KB).
* Tests: `tests/unit/test_atlas_healthedu.py` (18, offline), `tests/integration/test_atlas_healthedu_db.py`
  (3, fixture DB), `tests/live/test_atlas_healthedu_live.py` (1, `-m live`, throwaway DB).

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

## 4. Coverage (live ingest)

_Filled from the live run below._

## 5. Live ingest

_Pending._
