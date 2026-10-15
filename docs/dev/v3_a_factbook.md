# V3 A-FACTBOOK: universal gap fillers (CIA World Factbook + UNSD AMA)

Owner request: cover **every** country and territory, and when one source lacks a country, look
elsewhere. The multilateral sources (World Bank, IMF, UN, ILO …) skip small states and
territories. This agent adds two sources whose coverage is near-universal by design.

Status: connectors, catalogs, geo aliases, fixtures and tests done. A-LAW's Factbook legal
connector is merged into the same module (lead decision 2026-10-15; see "Merge with A-LAW").
LIVE_SUMMARY
The dev database `reva` was never touched. Nothing is committed.

## Sources researched (2026-10-15)

| Option | Finding | Used? |
|---|---|---|
| **CIA World Factbook**, cia.gov | **Discontinued.** On 2026-02-04 the CIA published "Spotlighting The World Factbook as We Bid a Fond Farewell": "One of CIA's oldest and most recognizable intelligence publications, The World Factbook, has sunset." `www.cia.gov/the-world-factbook/` now redirects (HTTP 302) to that story. The last official downloadable archive (zip) was the 2020 edition. | No (gone) |
| **factbook/factbook.json** (GitHub, CC0) | Weekly auto-update of every Factbook profile from the CIA site since August 2021, one JSON per entity (GEC file names), 261 entities incl. the 5 oceans and the World. Last data update: commit `783ed27` "auto-update week 4" (2026-01-22), i.e. the final online edition. Later commits only touch the README and move `aj`/`am`/`gg` between region folders. Full history kept (332 commits). | **Yes**: pinned commit `144d697` (data = `783ed27`) |
| Same mirror, commit `b1e7065` (2024-04-18) | The CIA removed the field "Disputes - international" on 2024-04-25 (mirror commit `4a26af5`). `b1e7065` is the last edition with it (259 files). | **Yes**: disputes only |
| Factbook 2020 zip mirrors / Internet Archive | Older edition; less current than the 2026-01 mirror. | No |
| **UNSD National Accounts - Analysis of Main Aggregates (AMA)** | Bulk XLSX per series for **220 countries/areas, 1970–2024**, complete by construction (UNSD fills gaps with estimates). Covers Anguilla, Montserrat, Cook Islands, Curaçao, Sint Maarten, Cayman, Turks and Caicos, Bermuda, Greenland, Monaco, Liechtenstein, San Marino, Andorra, French Polynesia, New Caledonia, Puerto Rico, Cuba, **North Korea**, Somalia, Kosovo, Zanzibar, plus former states (USSR, Yugoslavia, Czechoslovakia, Netherlands Antilles, the two Yemens…). Terms: UNdata ("free of charge … provided that UNdata is cited"). API `unstats.un.org/unsd/amaapi/api/file/<id>` answers 200 (the AMA web page itself answered 503). | **Yes** (second fallback) |
| UNSD Statistical Yearbook (UNdata SYB) | Near-universal too, but A-UN already ingests table T02 (population, area); its other tables re-publish WPP/AMA/other agencies. | No (A-UN) |
| UN M49 standard area table | Already the backbone of the crosswalk (ALLC/ATLASCORE); no indicators. | No |
| SPC Pacific Data Hub (.Stat) | Regional (22 Pacific territories); Factbook + AMA already cover these entities for headline figures. | Not now (candidate if Pacific detail is wanted) |
| Wikipedia / national statistics offices | Not multilateral, too many sources, licences vary. | No |

**Why these two.** The Factbook is the only open source with one template for all 261 entities
(texts and numbers, incl. uninhabited territories, Antarctica and disputed areas). It is a US
government compilation of estimates of various years, so it ranks **last** in any best-available
order. AMA is the official UN series with the widest economic coverage and a long history
(1970–), so it fills GDP/GNI/population for territories and former states where WB/IMF have
nothing, with an official multilateral provenance.

## What was built

* `src/reva/adapters/connectors/atlas_factbook.py` — two atlas sources (`atlas_source()` returns
  both; budget key `factbook.json`):
  * `atlas.factbook` (v1.1.0; 261 requests, one fetch → three tables):
    `legal_2026_01` (A-LAW's legal fields, unchanged indicators), `figures_2026_01` (numbers),
    `texts_2026_01` (every text verbatim);
  * `atlas.factbook_disputes` (v1.0.0; 259 requests): `disputes_2024_04`.
* `src/reva/adapters/connectors/atlas_unsd_ama.py` — `atlas.unsd_ama` (10 requests): table
  `unsd.ama` (9 bulk files + the file list).
* `registry/sources/factbook.yaml` (rewritten for the merged connector), `registry/sources/unsd_ama.yaml`
  (new), `registry/atlas/geo_aliases/a_factbook.yaml` (schemes `factbook_gec` and `unsd_ama`),
  `registry/atlas/catalog/factbook.yaml` (123 concepts: 94 numeric, 29 text; domain `factbook`),
  `registry/atlas/catalog/unsd_ama.yaml` (17 concepts; domain `unsd_ama`),
  `registry/atlas/budget_requests/a_factbook.yaml` (merged by ATLASCORE: `factbook.json` 900/day,
  `unsd.ama` 20/day).
* Fixtures: `tests/fixtures/source/atlas.factbook/` (12 profiles: A-LAW's FR, US, SA, TW, KV + GL,
  WI, WE, VT, XX, KN, SV; blob bytes of the pinned commit, identical to the live responses — the
  France file has the sha256 A-LAW recorded live), `atlas.factbook_disputes/` (FR, WI, TW at
  `b1e7065`), `atlas.unsd_ama/` (recorded file list + files 2, 9, 30 trimmed to six countries:
  derived fixtures, every other workbook part unchanged).
* Tests: `tests/unit/test_atlas_factbook.py` (26), `tests/unit/test_atlas_unsd_ama.py` (13),
  `tests/integration/test_atlas_factbook_db.py` (2, `db`: archive → register → read back →
  idempotent, through `run_atlas_ingest(mode="fixture")`). A-LAW's tests updated for the merge and
  passing: `tests/unit/test_atlas_law.py`, `tests/integration/test_atlas_law_ingest.py`,
  `tests/live/test_atlas_law_live.py` (spec list handling only).

Run options (`--option` / job payload `options`): `gecs=fr,tw,…` restricts Factbook entities,
`files=2,9` restricts AMA files.

## Honesty rules as implemented

**Factbook**

* Every numeric cell: `flags.estimate = true`, `flags.provider_nature = "US government (CIA)
  compilation/estimate; not an official national statistic"`, `flags.est_marker` (whether the
  text says "est."), `flags.source_text` (the text the number was read from), `flags.provider_note`
  (the field note, e.g. "data are in 2021 dollars" for real GDP), `flags.edition` (mirror commit,
  2026-01-22 edition, discontinuation date).
* Period = the year **as stated**: "(2024 est.)" → 2024; "(2020-25 est.)" → [2020, 2026);
  "(July 2020 est.)" → 2020. Year-keyed fields ("Real GDP 2024/2023/2022", "Exchange rates
  2020–2024", "Military expenditures 2021–2025") give one row per stated year. A sub-field without
  a year (e.g. population "male"/"female", age structure 0-14) takes the year of the only dated
  sibling of the same field, flagged `year_from_sibling`. Otherwise (area, coastline, elevation,
  borders) the as-published day of the file, flagged `period_basis = as_published`.
* Numbers are parsed only when the text matches the field's expected unit (e.g. population must
  not be followed by "%"; birth rate must say "births/1,000 population"); "NA", "uninhabited",
  "negligible", "less than 1%" give no number (the text row keeps them). Never a filled zero.
* Lists (languages, religions, ethnic groups, export/import partners, land borders) are split
  only into clean "name N%" / "name N km" segments, flagged `transcription = "list split … by
  agent A-FACTBOOK, maintainer review pending"`, with the full source text in flags; partners
  carry `partner_gec`/`partner_iso3` (name → GEC from the profiles' conventional short forms +
  a documented alias list).
* Texts verbatim (HTML tags removed, entities decoded). The two "Diplomatic representation"
  sections (addresses, phone numbers) are left out.
* Disputed entities (`DISPUTED`: Western Sahara, Kosovo, Taiwan, West Bank, Gaza Strip, Paracel
  and Spratly Islands, Falklands) carry `flags.disputed_territory`; every text that mentions a
  dispute/claim/occupation carries `flags.mentions_dispute`; disputes rows carry
  `disputed_wording_verbatim = true`. The wording is the US government's, not a legal finding.
* Staleness: `flags.mirror_last_changed` (per-file `git log -1` date at the pinned commit, embedded
  in the module). Western Sahara was dropped by the Factbook in December 2020 (US recognition of
  Moroccan sovereignty); its file last changed on 2021-01-01 and every row says so
  (`stale_profile`), with that date as as-published period.
* Geography: scheme `factbook_gec` = 240 aliases, each the GeoNames FIPS 10-4 code of the same
  entity (GEC codes are FIPS successors) checked against the Factbook name; Taiwan TW → TWN since
  ATLASCORE added TWN to the crosswalk. Partial territories stay **unmapped** with
  `flags.partial_of_iso3`: WE West Bank and GZ Gaza Strip (⊂ PSE), SV Svalbard and JN Jan Mayen
  (⊂ SJM), UM Pacific Island Wildlife Refuges, WQ Wake, BQ Navassa (⊂ UMI). Non-crosswalk
  entities (AX Akrotiri, DX Dhekelia, AT Ashmore and Cartier, CR Coral Sea Islands, IP
  Clipperton, PF Paracel, PG Spratly) and aggregates (EE EU, XX World, 5 oceans) are kept unmapped
  and listed in each manifest. France `FR` includes the overseas departments (as WB's FRA).

**UNSD AMA**

* Values = the exact cell text (strict decimals), never rescaled; units spelled out (`current US$`,
  `constant 2020 US$`, `% annual growth (constant 2020 prices)`, `persons`, `national currency per
  US dollar` + `flags.currency`). UNSD footnotes of a (country, item, year) cell are copied into
  `flags.footnotes`. `flags.provider_nature` says the series mix official data and UNSD gap
  estimates without marking which.
* Scheme `unsd_ama` = M49 + Kosovo 412 → XKX (from the AMA country list). Former states (200, 230,
  530, 720, 736, 810, 886, 890) and Tanzania Mainland 835 / Zanzibar 836 (AMA has **no United
  Republic of Tanzania total**) stay unmapped and listed. AMA "China (mainland)" 156 → CHN (as the
  ISO entity, excluding Hong Kong, Macao and Taiwan).

## Merge with A-LAW (lead decision 2026-10-15)

A-LAW had created `atlas_factbook.py` (connector `atlas.factbook`, table `factbook.legal_2026_01`,
255 profiles, `fips` scheme) and `registry/sources/factbook.yaml` before this agent started; this
agent first built a separate module (`atlas_factbook_profiles.py`) to avoid clobbering in-flight
work, then merged on the lead's instruction:

* one module `atlas_factbook.py`, one source key `factbook.json`, one `registry/sources/factbook.yaml`;
  the separate module was removed;
* `atlas.factbook` keeps A-LAW's name and its `legal_2026_01` table **with the same indicators**
  (`legal_system`, `suffrage`, `constitution.*`, `citizenship.*`, `international_law_participation`,
  `suffrage.minimum_age`, `legal_system.families_mentioned`; A-LAW's `transcribed by agent A-LAW`
  markers unchanged), so `registry/atlas/catalog/law.yaml` is untouched; A-LAW's functions
  (`parse_profile`, `families_mentioned`, `items`, `clean_text`, `_AGE`, `ATLAS_DATASET`,
  `CONNECTOR_NAME`) are kept. Version bumped to 1.1.0 (code changed);
* changes visible in the legal table: 261 profiles fetched (oceans/World added; they have no legal
  fields except XX), geography through `factbook_gec` instead of `fips` (TW now TWN; West Bank and
  Svalbard **no longer** mapped to PSE / SJM — they are only parts of them), period = each file's
  as-published day (Western Sahara 2021-01-01, flagged stale), extra flags (edition, disputed,
  partial). Full-mirror parse: 2,372 rows, 240 entities (A-LAW: 2,369 / 240);
* fixtures merged into `tests/fixtures/source/atlas.factbook/` (A-LAW's 5 + 7 new);
* A-LAW's tests adjusted: 261 planned requests, two sources discovered, `factbook_gec` scheme,
  249 fixture_missing items, three tables.

COVERAGE_SECTIONS

## Cross-owner notes / requests

* **Lead (seed)**: `register_atlas_connector_versions` will register `atlas.factbook` 1.1.0,
  `atlas.factbook_disputes` 1.0.0 and `atlas.unsd_ama` 1.0.0 (sources `factbook.json`, `unsd.ama`
  are in `registry/sources`).
* **A-LAW / docs**: `docs/dev/v3_a_law.md` still describes the legal table as 255 profiles mapped
  via `fips`; the table now uses `factbook_gec` (see "Merge"). Their live test
  (`tests/live/test_atlas_law_live.py`) runs `atlas.factbook`, which now fetches 261 files and
  registers three tables.
* **ATLASCORE**: thanks for adding TWN; the `fips` scheme still maps FIPS WE → PSE and SV → SJM
  (correct for GeoNames, but a Factbook "West Bank" or "Svalbard" is only part of those entities):
  sources using Factbook codes should use `factbook_gec`. Consider crosswalk entries for
  Akrotiri/Dhekelia (UK sovereign base areas) only if another source reports them.
* **LINKS**: `factbook.figures_2026_01` has country–country rows: `land_boundary|partner=<name>`
  (km, `flags.partner_iso3`), `export_partner_share|name=…` / `import_partner_share|name=…`
  (%, `flags.partner_iso3`); `factbook.disputes_2024_04` has dispute texts per country.
* **KGCULT**: `language_share|name=…`, `religion_share|name=…`, `ethnic_group_share|name=…`
  (Factbook names verbatim, with % and year).
* **UI3 / NLQA**: Factbook cells must be displayed with "US government estimate (CIA World
  Factbook, final edition Jan 2026)"; texts of disputed entities with the flag.

## How to re-run

```bash
uv run python -m reva.atlas.ingest atlas.factbook            # 261 requests (legal + figures + texts)
uv run python -m reva.atlas.ingest atlas.factbook_disputes   # 259 requests
uv run python -m reva.atlas.ingest atlas.unsd_ama            # 10 requests
uv run python -m reva.atlas.ingest atlas.factbook --fixture --option gecs=fr,tw,kv
```
