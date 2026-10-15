# V2 step 1: the crude border-price scenario for every country with data (agent ALLC)

Status: implemented and tested. Live run on 2026-10-15 into the throwaway database
`reva_allc_live_20261015`. The dev database `reva` was not touched. Everything below is
measured in that run. Gaps are listed and were never filled.

## What was built

| Piece | Where |
|---|---|
| Crosswalk builder (offline: `build`, `verify`) | `src/reva/adapters/connectors/country_crosswalk.py` |
| Crosswalk (250 entities in V2 — 251 since V3 crosswalk 1.1.0 adds ISO 3166-1 `TWN`, see `docs/dev/v3_xwalk.md`; flagged unmapped codes, source digests, `content_sha256`) | `registry/entities/country_crosswalk.json` |
| Raw provider reference lists (archived bytes, digests in the manifest) | `registry/reference/{unsd_m49_overview.html.gz, comtrade_Reporters.json, comtrade_partnerAreas.json, worldbank_country.json}`, `registry/reference/manifest.yaml` |
| Seeding of crosswalk entities (small block marked "V2 step 1 (ALLC)"; the crosswalk digest is part of the seed fingerprint) | `src/reva/cli/seed.py` |
| Comtrade connector for every reporter: multi-reporter requests, rotating | `src/reva/adapters/connectors/comtrade_batch.py` (`un.comtrade.public_batch@1.0.0`) |
| World Bank connector for every economy | `src/reva/adapters/connectors/worldbank_all.py` (`worldbank.wdi.all_economies@1.0.0`) |
| Mappings (same semantics as the reviewed ones, subject scope = the crosswalk) | `registry/mappings/comtrade_batch.yaml`, `registry/mappings/worldbank_all.yaml` |
| Schedules | `ingest:un.comtrade.public_batch` runs daily, one slice per run. `ingest:worldbank.wdi.all_economies` runs every 30 days. |
| `countries: ["ALL"]` resolved at compile time | `src/reva/simulation/compile.py` (`resolve_all_countries`) |
| `missing_input_policy: skip_missing_countries` | `src/reva/simulation/crude_plugin.py` (`CompiledScenario.skipped`, `run_applicability`) |
| Schema: crude specs at 2.0.0 accept registry keys, `["ALL"]` and the skip policy; 1.0.0 specs keep the V1 contract | `src/reva/simulation/scenario_schema.py`, `src/reva/simulation/spec.py`, regenerated `contracts/scenario.schema.json` |

## Crosswalk rules (decided conservatively)

The crosswalk joins ISO3, UN M49, Comtrade and World Bank codes. It is built mechanically from
the providers' own lists, so its review status is `machine_derived_pending_maintainer_review`.

* **M49 authority.** The M49 codes come from UNSD's M49 overview page, which lists 248 rows
  with ISO 3166 codes. Comtrade codes are based on M49 but are not M49. The crosswalk records
  five that differ: FRA 251 vs 250, USA 842 vs 840, CHE 757 vs 756, NOR 579 vs 578, IND 699
  vs 356.
* **World Bank economies.** There are 217 World Bank economies. The 79 rows whose region is
  "Aggregates" (regions, income groups, lending groups) are excluded and listed in
  `excluded_worldbank_aggregates`. Two economies have no ISO 3166 code: CHI (Channel Islands)
  and XKX (Kosovo). They are keyed by their World Bank code and flagged `not_iso3166`.
* **When a Comtrade code maps to a country.** A Comtrade reporter or partner code maps to an
  entity only when all of these hold:
  * it is not a group;
  * it is not a "nes" area;
  * it is valid during 2019–2024;
  * its provider ISO3 field is an ISO3 code that M49 lists;
  * no other code meets the same conditions for that ISO3.

  Everything else is listed with flags under `unmapped_provider_codes` and is never mapped
  to an ISO3:
  * 36 reporter codes, including 490 "Other Asia, nes", 97 "European Union", 975 ASEAN and
    the historical "(...1990)" entities;
  * 72 partner codes, including 0 World and 899 "Areas, nes".

  Partner 492 "Europe EU, nes" shows why the provider ISO3 field is not trusted on its own:
  it carries ISO3 `MCO`, and it stays unmapped.
* **Reporters that cover a wider territory.** Some reporter notes list more areas than the
  label: 251 "France, Monaco", 842 "USA, Puerto Rico and US Virgin Islands",
  757 "Switzerland, Liechtenstein" and 579 "Norway, Svalbard and Jan Mayen". These are mapped,
  as the reviewed FRA and USA aliases already were, but flagged
  `comtrade_territory_note_lists_additional_areas`. Their trade value covers a wider territory
  than the World Bank GDP denominator. As a result, PRI, VIR, MCO and LIE have no reporter of
  their own.
* **Partner codes are recorded but not seeded.** Bilateral rows keep the provider partner code
  and resolve to provider-declared partner entities, which is the existing seed behaviour.
  Composite partners such as 899, 490 and 568 stay as they are.
* **How aliases are seeded.** Crosswalk aliases are seeded with `confidence_kind =
  provider_declared` and `mapping_version = crosswalk-1.0.0`. Entities defined in
  `countries.yaml` (FRA, DZA, CHN, USA) keep their human-reviewed aliases and get nothing
  from the crosswalk. The compiler now reports `provider_declared_alias:iso3166_alpha3` for
  crosswalk entities instead of claiming `reviewed_alias`.
* **Taiwan.** M49 has no separate Taiwan row, and Comtrade 490 is a "nes" area. Taiwan is
  therefore not an entity, by design.

To refresh the archives, a maintainer downloads the four URLs (see the module docstring) and
updates `manifest.yaml`. Then run `python -m reva.adapters.connectors.country_crosswalk build`
followed by `verify`. The module itself makes no network request, because invariant 30
requires every live path in `src/reva` to be budgeted.

## How the connectors request data (live probes, evidence in `tests/fixtures/source/un.comtrade.public_batch/probes/`)

* **What the Comtrade public preview accepts.** It accepts a comma-separated `reporterCode`
  list together with `flowCode=M,X`. A list of all 219 mapped reporters was accepted and
  returned 223 rows with no duplicates. It refuses more than one period with HTTP 400
  "Maximum number of periods for preview is 1".
* **World totals.** Each request covers one period and up to 110 reporters, which gives at
  most 220 rows, well under `maxRecords=500`. With 2 batches per year over 6 years, this is
  12 requests.
* **Bilateral (2023).** Each request covers 4 reporters and both flows. A probe with 12 large
  importers hit 500 rows and dropped the USA. A batch that reaches `maxRecords` is blocked as
  `possible_truncation`, and two half batches are queued as follow-ups. No batch needed a
  split in the live run.
* **Rotation.** A full sweep is 67 requests. A scheduled run plans at most 30, starting at
  the cursor offset. The cursor advances only when the slice's required items were published,
  and wraps around to start the next sweep. The daily budget for `un.comtrade.public_preview`
  is 100, which covers 30 here plus the 48-request monthly run of the 4-country connector. The
  budgets file was **not** changed or re-pinned.
* **Missing values and unreviewed classifications.** A requested reporter that returns no row
  is recorded as `Missing(unavailable)` with `provider_returned_no_rows=true`. A row reported
  under H0–H3 is stored with its `classificationCode`. It stays ineligible because the mapping
  scope is H4–H6 (`evidence.observation_is_eligible`), and a warning is raised. An unknown
  edition (for example H7) still blocks the whole batch.
* **Shared history with the reviewed connectors.** The logical keys, supports and flags are
  the same as the reviewed connectors. A test checks that the semantic hashes are identical,
  so FRA, DZA, CHN and USA share one source history with the reviewed connectors.
* **World Bank.** The World Bank API accepted all 217 economy codes in one path. With
  `per_page` set high enough, each indicator comes back as a single page, so every
  (economy, year) is an expected key.

## Live run (2026-10-15, throwaway DB, release `6ea5c14e-2036-4a93-9aa0-36e4de08b660`)

| Connector | Runs | Requests (ledger reserved = consumed) | Bytes | Outcomes |
|---|---|---|---|---|
| `worldbank.wdi.all_economies` | 1 | 5 | 617,882 | 5 published |
| `un.comtrade.public_batch` | 3 (slices 0–30, 30–60, 60–67, then wrapped to sweep 1) | 30 + 30 + 7 = 67 | 3,189,895 | 67 published, 0 quarantined, 0 splits |

Bilateral 2023 data: 1,951 partner rows from 137 reporters and 178 partner codes. Composite
partners were kept as provider codes: 899 "Areas, nes" (19 rows), 490 "Other Asia, nes" (10),
568 (7), 473, 637 and 839 (1 each).

### World totals by flow and year (219 mapped reporters; World Bank 217 economies)

| Year | M value (eligible) | M no rows | M unreviewed HS | X value (eligible) | X no rows | X unreviewed HS | GDP value | GDP null |
|---|---|---|---|---|---|---|---|---|
| 2019 | 134 | 82 | 3 | 101 | 116 | 2 | 211 | 6 |
| 2020 | 128 | 90 | 1 | 97 | 121 | 1 | 210 | 7 |
| 2021 | 123 | 94 | 2 | 97 | 120 | 2 | 210 | 7 |
| 2022 | 122 | 94 | 3 | 100 | 117 | 2 | 209 | 8 |
| 2023 | 120 | 98 | 1 | 100 | 117 | 2 | 204 | 13 |
| 2024 | 113 | 105 | 1 | 84 | 135 | 0 | 200 | 17 |

"No rows" means the provider returned nothing for a reporter that was requested. It is a typed
Missing and is never zero. Comtrade publishes no zero rows, so "no rows" cannot be told apart
from "did not report".

### Scenario `countries: ["ALL"]`, `missing_input_policy: skip_missing_countries`, L1, 25% shock

| Reference year | Resolved (any required concept present) | Countries with complete inputs (computed) | Skipped (typed reasons) | Result rows |
|---|---|---|---|---|
| 2020 | 229 | **88** | 141 | 352 |
| 2021 | 229 | **85** | 144 | 340 |
| 2022 | 229 | **89** | 140 | 356 |
| 2023 | 229 | **85** | 144 | 340 |
| 2024 | 229 | **74** | 155 | 296 |

Every run's status is `succeeded`. The skipped countries are listed in the run applicability
(`skipped_countries`), each with its typed `missing_input` problems and its per-input estimate
id and value. The year 2019 was ingested, but the crude model's registered applicability is
2020–2024, so 2019 is refused as a reference year.

Check against the V1 demo (2023, same release): FRA 0.2576 % and USA 0.0497 % of GDP. These
match `docs/dev/numeric_audit.md`. CHN is 0.4609 % and CHE is 0.0471 %.

### Countries with complete inputs in 2023 (85)

AGO AND ARE ARG AUS AZE BEL BIH BOL BRA BRN BWA CAN CHE CHL CHN CIV CMR COD COG COL CRI CZE DEU
DNK DOM ECU EGY ESP FIN FRA GAB GBR GHA GRC GTM GUY HRV HUN IDN IRL ITA JOR KAZ KOR LAO LBR LTU
LUX LVA MDA MNG MOZ MWI MYS NAM NLD NOR NZL OMN PAK PER PHL PNG POL QAT ROU SEN SGP SRB SVK SVN
SWE TGO THA TTO TUN TZA UGA UKR USA VNM WSM ZAF ZMB

### Gap listing, 2023 (144 skipped plus 21 not resolved)

| Countries | Cause |
|---|---|
| 62 | No row for imports or exports: ABW AFG ALB BDI BGD BLR BMU BTN CPV CUW CYM DJI DZA FJI FRO FSM GIN GMB GNB GNQ GRL HKG HND HTI KHM KIR KNA LBY LCA MAC MAR MDG MDV MHL MKD MLI MNE MUS NCL NPL NRU PAN PLW PSE PYF RUS SDN SLB SLE SMR SOM STP SXM TCA TCD TKM TON TUR TUV VCT VEN VUT |
| 34 | No export row: ARM AUT BEN BFA BGR BHR BHS CAF COM CYP DMA EST ETH GEO GRD IND ISL ISR JAM JPN KGZ LKA LSO MLT NIC PRT PRY RWA SWZ SYC TJK URY UZB ZWE |
| 14 | No import row (several large exporters): BLZ BRB IRN KEN KWT LBN MEX MMR MRT NER NGA SAU SLV TLS |
| 12 | No rows and no World Bank economy: AIA BES BLM COK MSR MTQ NIU SHN SPM TKL VAT WLF |
| 8 | No rows and GDP null at the World Bank: CUB ERI GIB MNP PRK SSD SYR VGB |
| 6 | No Comtrade reporter (covered by another reporter's territory, or none exists): CHI IMN LIE MCO PRI XKX |
| 4 | No Comtrade reporter and GDP null: ASM GUM MAF VIR |
| 3 | Value reported under an unreviewed HS edition (stored, ineligible): IRQ exports H2 (about 97.6 bn USD), SUR exports H3, ATG imports H3 |
| 1 | GDP null at the World Bank: YEM |
| 21 not resolved | No estimate of any required concept, because they are neither a Comtrade reporter nor a World Bank economy: ALA ATA ATF BVT CCK CXR ESH FLK GGY GLP GUF HMD IOT JEY MYT NFK PCN REU SGS SJM UMI |

## Open questions for the owner (not decided by the agent)

1. **HS editions H2/H3 for heading 2709.** Iraq's 2023 crude exports (about 97.6 bn USD) are
   reported under H2. Admitting them requires a reviewed mapping decision that the 2709 heading
   is unchanged across HS editions. An agent cannot record that review, so H2/H3 values stay
   ineligible.
2. **A missing row for one flow.** When a reporter reports one flow but not the other (for
   example SAU, NGA and KWT report exports but have no import row), the missing flow stays
   Missing and the country is skipped. Treating it as zero would be imputation, which the data
   constitution forbids. This needs a provider statement or a different source.
3. **Wider reporter territories.** The territory-flagged reporters (FRA+MCO, USA+PRI+VIR,
   CHE+LIE, NOR+SJM) are mapped as the reviewed FRA and USA aliases already were. A maintainer
   should confirm this for CHE and NOR.

## Cross-agent changes requested (not made: files owned by others)

* **SEC, `src/reva/api/routes/sim.py::create_simulation`.** `_preflight_inputs` treats every
  scenario as `missing_input_policy=fail` and looks up the literal country "ALL". Requested
  change: skip the preflight when `spec.get("missing_input_policy") == "skip_missing_countries"`
  or `spec.get("countries") == ["ALL"]`. The engine and compiler then report missing or
  skipped countries themselves, as typed results. Until this change lands, these two options
  run through the worker or job path (`handle_simulation` → `plan_run`) but are refused at
  `POST /v1/simulations` with a typed `missing_input`, never a false success.
* **DOC2, `docs/limitations.md` "Coverage limits".** The line "Countries: France, Algeria,
  China, United States profiles" is out of date. Point to this document for the all-country
  coverage and gap list.

## Tests

* `tests/unit/test_country_crosswalk.py`: rebuild equals the committed file; composite, group
  and historical codes are flagged and never mapped; aggregates are excluded; territory flags;
  agreement with the reviewed entries; mapping scopes; tamper detection; a partial M49 table
  is refused.
* `tests/unit/test_comtrade_worldbank_all_connectors.py`: one period per request; every
  reporter covered; budget rotation (a full sweep in order, each run within the daily cap
  together with the legacy run, no advance on failure, the slice shrinks with the budget);
  typed Missing; H2/H3 per row versus H7 blocking; shared lineage (semantic-hash equality with
  the reviewed connector); truncation split; partner codes; World Bank single page.
* `tests/integration/test_comtrade_worldbank_all_countries.py` (db): seeded aliases and
  confidence; fixture ingest into a release (DZA typed Missing, IRQ H2 not selected).
* `tests/integration/test_scenario_all_countries.py` (db): ALL with skip; ALL with fail;
  explicit list with skip; skip when everything is missing is refused; the skip policy gives
  the same output digest as fail for complete countries; the V1 contract is kept for 1.0.0.
