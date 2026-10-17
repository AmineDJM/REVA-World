# V6 V6AGRI — food systems for prediction: balance sheets, WASDE vintages, bilateral staples trade, export restrictions

Agent V6AGRI (2026-10-17). Scope: `docs/roadmap/v6_plan.md`; data gap named by PRED-ENGINE
(`docs/dev/v5_pred_engine.md`: the India rice export-ban decision stops at `trade.export_restriction`). Everything
below is sourced to archived raw bytes, licensed per value, identifier-linked (no name matching) and nothing is
causal. Wikidata was not queried (WDOPS owns it).

## What already existed (not duplicated)

`atlas.fao` (A-FAO, CC BY 4.0) is loaded live: QCL production / area harvested / yield for ~60 crops incl. wheat 15,
rice 27, maize 56, soya beans 236 (`fao.qcl.<item>|5510|5312|5412`), producer prices PP (`fao.pp.<item>|5530/5532/5539`),
food balance sheets FBS/FBSH (supply and utilisation: production, imports, exports, stock variation, food, feed …
for wheat 2511, rice 2805, maize 2514, soybeans 2555), agricultural trade totals TCL (incl. wheat/rice/maize/soy
import and export values), and the FAO Food Price Index (`fao_ffpi.ffpi_m`, V4ECON, monthly, non-commercial). V6AGRI
adds what was missing for prediction:

| New | Why it matters for prediction |
|---|---|
| USDA PSD balance sheets (country × commodity × marketing year: production, area, yield, imports, exports, consumption, **ending stocks**) | the stock-to-use and exporter-share inputs of every food-price / export-restriction model; USDA's own projection year flagged |
| USDA WASDE **monthly vintages** (as published, 2021-01 → now) | as-of forecasting: what USDA projected for a marketing year at each report date — benchmark and real-time input (vintage family `usda_wasde` in the forecast accessor) |
| FAOSTAT detailed trade matrix, **bilateral**, staples, Asian reporters | who buys rice / wheat / maize / soy from whom (India → partners), i.e. exposure of importers to an exporter's restriction |
| IFPRI Food Export Restrictions Tracker → KG `export_restriction` **events** | dated, country × product (HS4) restriction episodes for event studies; HS headings linked by code to V4LINK's HS 2017 entities |

## Sources

| Source (connector, source_key) | URL | Licence | Fetched per full run | Bytes |
|---|---|---|---|---|
| USDA FAS PSD Online (`atlas.usda_psd` 1.0.0, `usda.psd`) | `https://apps.fas.usda.gov/psdonline/downloads/psd_grains_pulses_csv.zip`, `.../psd_oilseeds_csv.zip` | Public domain (U.S. Government work, 17 U.S.C. 105); attribution kept | 2 zips | 2,873,599 + 3,844,435 |
| USDA WASDE, OCE "Historical WASDE Report Data" (`atlas.usda_wasde` 1.0.0, `usda.wasde`) | `https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-<YYYY>-<MM>[-V2].csv` | Public domain | one CSV per report 2021-01..current (~69 files + a 404 probe for the 2 `-V2` months and October 2025, which has no report) | ~1 MB each, ~70 MB fetched; ~3 MB on disk after DISKZ zstd (25× on CSV) |
| FAOSTAT TM detailed trade matrix (`atlas.fao_tm` 1.0.0, `fao.faostat_tm`) | `https://bulks-faostat.fao.org/production/Trade_DetailedTradeMatrix_E_Asia.zip` | CC BY 4.0 (FAO database terms) | 2 HTTP byte ranges: tail 64 KiB (central directory + code lists) and the wide member only | 65,536 + 57,766,824 (already deflated; the 554 MB inflated CSV is streamed, never stored) |
| IFPRI Food Export Restrictions Tracker (`atlas.ifpri_er` 1.0.0, `ifpri.export_restrictions`) | tool page `https://www.foodsecurityportal.org/node/25` + the two workbooks it links (`…PublicVersion_IFPRI_ExpRestrictions_Covid19_0.xlsx`, `…_UKRRUS.xlsx`) | **ODC-By** ("License: ODC-By" cell B1 of each workbook, checked every run; page: "The Food Export Restrictions Tracker data is made available under the Open Data Commons Attribution License" and "… is licensed under a Creative Commons Attribution 4.0 International License") | 3 GETs | 91,092 + 31,925 + 24,342 |

Raw archive of one full run ≈ 137 MB fetched, ≈ 70 MB on disk (the TM member dominates). Budgets: request file
`registry/atlas/budget_requests/v6agri.yaml` merged into budgets-1.3.35 (usda.psd 6/day, usda.wasde 100/day,
fao.faostat_tm 4/day, ifpri.export_restrictions 6/day; digest re-pinned in `src/reva/autonomy/policy.py`; a later
merge by another agent re-pinned again). Source registry: `registry/sources/{usda_psd,usda_wasde,fao_faostat_tm,ifpri_export_restrictions}.yaml`.

USDA's note on the WASDE data page, quoted: "The files below contain data as it was reported and appeared at the
time of publication in each World Agricultural Supply and Demand Estimates (WASDE) report from April 2010 to
current. … These data do not include subsequent revisions to estimates made to any underlying data series after
each report was released and thus reflect the evolution of estimates over time as new information developed and
record the supply and demand conditions as they were believed to exist at the time each report was released."

## Tables and KG (design; see "Loaded" for what is in the dev DB)

### `usda_psd.grains`, `usda_psd.oilseeds`
* `indicator` = `<Commodity_Code>|<Attribute_ID>` (PSD's codes; names verbatim in flags), e.g. `0422110|176` = Rice,
  Milled — Ending Stocks (1000 MT); `0410000|028` Wheat Production; `0440000|088` Corn Exports; `2222000|057`
  Soybean imports.
* Scope: all 9 grains of the grains file (wheat, rice milled, corn, barley, sorghum, millet, oats, rye, mixed
  grain), 15 oilseed-complex commodities (soybean / rapeseed / sunflowerseed oilseed-meal-oil, palm oil, palm
  kernel). Parsed on the live files of 2026-10-09: **581,535 + 459,823 cells**.
* Period = PSD `Market_Year` Y as calendar year Y (= marketing year Y/Y+1; local MY months are not in the file —
  `flags.period_basis` says so; WASDE uses the same first-year convention so the two align).
* **Projection rule** `psd_latest_market_years` (PSD marks nothing per cell): per commodity the latest market year
  in the file is USDA's projection year → `estimate_kind = projection` (excluded from the atlas answer layer), the
  year before → `estimated`, earlier → `published_history`. Checked against the WASDE CSV of the same day: India
  rice ending stocks PSD 2025/26 = 59,000 (1000 MT, `estimated`) = WASDE Oct 2026 "Est." 59.00 MMT; 2026/27 = 57,000
  `projection` = WASDE "Proj.".
* Zeros: PSD writes 0 both for a reported zero and "no activity"; kept as published with `flags.zero_semantics`
  (340,606 such cells in the two files), never presented as a measured zero.
* Geography: PSD `Country_Code` (USDA's 2-letter codes). Scheme `usda_psd` (`registry/atlas/geo_aliases/v6agri.yaml`)
  maps a code **only when the PSD code equals the GeoNames FIPS 10-4 code and the PSD name equals the GeoNames
  name exactly** (157 of 188 codes). Unmapped and listed (31): Burma (BM), the two Koreas (KN/KS), Côte d'Ivoire
  (IV), both Congos, Czech Republic (EZ), Netherlands (NL), Gambia, Macau, Vanuatu, and old USDA codes that are not
  FIPS (DM Benin, GU Guinea, PN Panama, RB Serbia, RH Zimbabwe, TC UAE, GE Germany), the EU (E4, E2), historical
  areas. No alias from a name alone (owner decision 2 gives the clean fix).

### `usda_wasde.wasde` (vintage table, family `usda_wasde`)
* Conventions of `vintage_common` (PRED-VINTAGE): indicator `usda_wasde:<slug(table title)>.<slug(commodity)>.<slug(attribute)>|v=<YYYY-MM>`
  (slug = lower-case re-spelling of USDA's own labels, each label verbatim in `flags`); `provider_geo` = WASDE
  `Region` text ("World", "India", "Major Exporters" …) — **no iso3: region names are never mapped to countries**.
* `Proj.`/`(Proj.)` → `projection`; `Est.`/unflagged → `published_history` (label kept in `flags.wasde_status`).
* Availability (new rules in `vintage_common.AVAILABILITY_RULES`): `wasde_csv_release_date` = day after the
  report's `ReleaseDate` 00:00 UTC when the file's `Last-Modified` is on/before release day;
  `wasde_csv_file_last_modified` = day after `Last-Modified` otherwise (e.g. the January 2021 file was posted on
  19 March 2021 → available 2021-03-20; June 2023 posted 3 days late). Conservative by construction.
* Reliability tables (statistics of past forecast errors, 630 rows per report) are skipped and counted; quarterly
  U.S. animal-product rows get `.quarterly` and calendar-quarter periods.
* Registered in `reva.evaluation.vintages.VINTAGE_DATASET_KEYS`: `CutoffRepository.vintages(...).vintage_in_force("usda_wasde")`
  works (DB test). One report = 4,339–4,420 cells (~300 k cells for 2021-01..2026-10).

### `fao_tm.tm_staples_asia`
* Indicator `<item>|<element>|partner=<FAO partner area code>`; `provider_geo` = FAO reporter area code; reporter
  iso3 via the row's own M49 code (`fao_m49`, same rule as A-FAO); partner M49 / iso3 / name in flags; FAO flag
  letter + meaning (A official, E estimated, I imputed by a receiving agency, X external) verbatim; item CPC code
  as FAO publishes it in `flags.item_cpc`.
* Items 15 wheat, 16 wheat flour, 27/28/29/30/31/32 rice (paddy, husked, milled-husked, paddy milled-equivalent,
  milled, broken), 56 maize, 236/237/238 soya beans / oil / cake; all elements (import/export quantity, value);
  1986-2024. Reporters = FAO's Asia region (India, China, Thailand, Viet Nam, Pakistan, Myanmar, Cambodia, Japan,
  Indonesia, Philippines, Bangladesh, Iran, Saudi Arabia, Türkiye …); partners worldwide (so US / Brazil / Russia /
  Ukraine exports appear as Asian imports, i.e. mirror flows, not reconciled).
* The world file is 420 MB and the Europe/Americas files 189 / 63 MB: only the Asia wide member (57.8 MB) fits the
  V6 raw bound; the parser is region-agnostic (`REGION`) for a bigger machine.

### `ifpri_er.measures` + KG events
* 82 measures (COVID-19 workbook 44: Ban 34, Licensing 7, Announcement 2, Not Binding 1; Ukraine-crisis workbook 38:
  Ban 28, Licensing 10), 2020-03 → 2023-12 (end dates as coded).
* KG: entity `ifpri_er:<covid19|ukrrus>:<Code>` kind `export_restriction`; statements `ifpri_er:imposed_by` →
  country QID (`registry/entities/country_qids.json`, by IFPRI's ISO 3166-1 alpha-3) with the start/end interval,
  P580 / P582, `ifpri_er:announced`, `ifpri_er:last_update`, `ifpri_er:measure_category`, `ifpri_er:products`,
  `ifpri_er:description`, `ifpri_er:verification_status`, `ifpri_er:source_url` (one per cited URL, not fetched),
  `ifpri_er:wto_notified`, `ifpri_er:protected_countries` (text, never mapped), `ifpri_er:hs4_as_stated`, and
  `ifpri_er:covers_hs_heading` → `hs2017:<HS4>` (598 links). 1,607 statements on the fixture replay = the full data.
* HS edition: IFPRI does not state it; HS 2017 is REVA's assumption, written on every link
  (`hs_version_basis`). 11 numeric 3-digit cells (Excel drops the leading 0 of chapters 01-09, e.g. `407` eggs) are
  zero-padded and flagged `hs_code_repair`; string codes are never repaired.
* Definitions quoted from the workbook's definition row, except `EndDate`: that row's 5th cell defines a "Year"
  column, i.e. the row is misaligned there, so EndDate carries "definition not given … end date as coded".
* Event taxonomy (`registry/events/taxonomy.yaml`): new type `export_restriction` (parent `event`, role `actor`,
  no Wikidata class verified), dataset kind `ifpri_er:` and property `ifpri_er:imposed_by` → participant/actor.
  Predicates `registry/kg/predicates_agri.yaml`; family mapping `registry/kg/relations_v4.d/v6agri.yaml`
  (`covers_hs_heading` → `associated_with`, subtype `export_restriction_covers_product`; `reva.kg.families check` ok,
  23 families, 192 mappings). Nothing causal.
* Atlas table: per measure the tracker's numeric fields (`Share_of_exports_Kcal|measure=covid19:61` …, 556 cells),
  period = StartingDate..EndDate, IFPRI's column definition quoted in each cell's flags.

## Examples (ids; each hop a sourced statement or a code published by the source)

* **Viet Nam rice ban 2020 → rice product codes → FAO trade item.** `ifpri_er:covid19:61` (kind
  `export_restriction`, "Vietnam halted new rice export contracts as it reviews stocks …", Ban, checked) —
  `ifpri_er:imposed_by` [2020-04-10, 2020-04-30] → `Q881` Viet Nam — `ifpri_er:covers_hs_heading` → `hs2017:1006`
  (Rice) ← `v4link:class_parent` — `hs2017:100630` — `v4link:corresponds_to` → `cpc21:23161` (Rice, semi- or wholly
  milled; UNSD CPC21-HS2017 table, dev KG). FAOSTAT TM item 31 "Rice; milled" publishes CPC `23161.02` (FAO's
  extension of CPC 23161) in `flags.item_cpc` of every `fao_tm.tm_staples_asia.31|…` cell (a code match, not a KG
  statement). Atlas: `ifpri_er.measures.Share_in_World_Kcal|measure=covid19:61` (VNM, 2020-04-10..2020-05-01).
* **India rice stocks, as USDA saw them.** `usda_psd.grains.0422110|176` IND 2025 (MY 2025/26) = 59000 (1000 MT,
  estimated), 2026 = 57000 (projection, excluded from observations); `usda_wasde.wasde`
  `usda_wasde:world_rice_supply_and_use_milled_basis.rice.ending_stocks|v=2026-10` @ "India" 2025 = 59
  (published_history, Est.), 2026 = 57 (projection), available_at 2026-10-10T00:00Z; the May 2026 vintage (`-V2`
  file) holds that report's numbers for the same marketing years.
* **Who imports from whom (fixture replay).** `fao_tm.tm_staples_asia.27|5610|partner=165` … reporter `2`
  (Afghanistan, AFG) rice imports by partner (partner M49 / iso3 in flags). Live: `31|5910|partner=<FAO code>`
  @ India = India's milled-rice exports to each partner, 1986-2024.

## Loaded (dev DB) — honest status

**Not loaded live.** Free disk on `/` was 4.9 GB → 4.3 GB from 20:45 UTC onwards (other agents' loads), below the
5 GB floor of the common rules, so no live ingest was started. `reva seed` (connector registration) was queued
behind the dev-DB lock (see "Commands"). What is verified:

| Check | Result |
|---|---|
| Parsers on the **live** files (scratch copies, 2026-10-17) | PSD 581,535 + 459,823 cells, validate_atlas_frame ok; WASDE 2021-01 / 2023-06 / 2026-05-V2 / 2026-10: 4,339 / 4,354 / 4,354 / 4,420 cells; IFPRI 82 measures, 1,607 statements, 556 atlas cells |
| `tests/unit/test_v6agri_parsers.py` | 16 passed (geo rule, projection rule, zeros, WASDE availability + periods + vintage cells, TM byte ranges + CRC refusal, IFPRI KG/licence refusal) |
| `tests/integration/test_v6agri_ingest.py` (throwaway DB, fixture mode) | 7 passed: every table registered from archived raw, idempotent re-run; WASDE `-V2` fallback and missing months; `vintage_in_force("usda_wasde")` = 2026-05 at 2026-07-01; 82 IFPRI entities in `kg.entity`, `hs2017:1006` link in `kg.statement` |
| `tests/integration/test_atlas_vintage_db.py` (WASDE added to the vintage specs) | 14 passed incl. `tests/invariants/test_invariant_30_budgets.py` |
| ruff / mypy on all new modules and tests | clean |

## For PRED-ENGINE (India rice export-ban decision)

* The decision's direct variable `trade.export_restriction` now has **dated events** to estimate from (82 IFPRI
  measures, incl. rice: Viet Nam 2020 ban `covid19:61`, Cambodia, Myanmar, Kazakhstan/Kyrgyzstan cereals, Russia /
  Ukraine 2022 grain bans), and the **inputs** an exposure model needs: exporter shares and stocks (PSD, WASDE
  vintages), bilateral rice flows from Asian exporters (TM), and the FAO Food Price Index (V4ECON). REVA still
  states **no effect**: an export restriction → price estimate must come from an attributed publication
  (EFFECTS-MORE added WTO / Anderson-Ivanic-Martin estimates) or a pre-registered event study; association between
  these events and prices is not a cause.
* **Missing for the rice decision itself:** India's own measures (wheat ban 13 May 2022, broken-rice ban and 20 %
  duty 8-9 Sept 2022, non-basmati white rice prohibition 20 July 2023, DGFT Notification 20/2023) are **not** in the
  open IFPRI files (they stop in April 2022). See owner decisions 1 and 3.

## Missing, and why

| Item | Why | Unblock |
|---|---|---|
| Live load of all four sources | free disk < 5 GB during the session (rule) | commands below, ≥ 5 GB free |
| IFPRI measures after April 2022 (incl. India 2022-2023 rice/wheat) | the page's "csv format here" Google-Sheet link answers **410 Gone**; the Shiny app needs a browser session; no Dataverse deposit found | owner: ask IFPRI for the current ODC-By extract (or a stable CSV URL) |
| WASDE 2010-04 … 2020-12 vintages | the two zips and the listing page on `www.usda.gov` answer **403 Access Denied** to this client (monthly CSVs from 2021 are served). A copy exists on USDA's CDN host `usda.azureedge.us` and per-report XML/XLS on ESMIS (`esmis.nal.usda.gov`, 3.5 MB XML per report) | owner: allow the CDN host (not used: it would bypass the www block) or a parser for the ESMIS XML (no block) |
| PSD country codes → ISO for 31 codes (Burma, Koreas, EU …) | PSD publishes no crosswalk in the bulk files; the PSD API returns GENC codes but needs an api.data.gov key | owner: api.data.gov key (free) → GENC crosswalk by identifier |
| TM for exporters outside Asia (US, Brazil, Argentina, Russia, Ukraine, EU) as reporters | regional files 63-189 MB, world file 420 MB > V6 raw bound | bigger machine: `REGION` = Americas / Europe (same parser) |
| FAO GIEWS crop calendar | API open (`api-cropcalendar.apps.fao.org/api/v1`, 58 countries — not India/China/US), licence not stated on the API (FAO website terms → likely non-commercial) | owner licence check; small connector (≈ 15 MB) |
| AMIS Market Monitor balances / IGC prices | JS application, balances compiled from AMIS members and IGC (proprietary IGC indices) | not loaded; FAO FFPI + WASDE cover the open part |
| USDA crop calendars (IPAD) | published as images / PDFs | not loaded |
| FAOSTAT SUA (supply utilisation accounts, 87 MB) | FBS already gives supply/utilisation for the staples; SUA adds sub-products | bigger machine |

## Owner decisions

1. **IFPRI current tracker data** (measures after April 2022; India 2022-2023): request an ODC-By extract.
2. **api.data.gov key** for the PSD API (GENC country codes → map the 31 unmapped PSD codes by identifier).
3. **India's DGFT notifications** as primary-source events (curated, quoted verbatim) — would give the rice decision
   its own historical analogue (July 2023 prohibition) independently of IFPRI.
4. **WASDE 2010-2020**: allow `usda.azureedge.us` or fund an ESMIS XML parser.
5. **FAO crop calendar licence** (FAO website terms vs CC BY 4.0).

## Commands (finish on a bigger machine / when ≥ 5 GB are free)

```bash
df -h /                                                             # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed                         # registers atlas.usda_psd / usda_wasde / fao_tm / ifpri_er 1.0.0
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ifpri_er  # 3 requests, ~0.15 MB; KG events
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.usda_psd  # 2 requests, ~7 MB
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.usda_wasde  # ~72 requests, ~72 MB (2021-01..now)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.fao_tm    # 2 byte ranges, ~58 MB
# monthly refresh of WASDE only (one report): --option first=2026-11 --option last=2026-11
# then (lead): reva atlas snapshot, reva links materialise
```

Tests: `tests/unit/test_v6agri_parsers.py`, `tests/integration/test_v6agri_ingest.py`, WASDE in
`tests/integration/test_atlas_vintage_db.py`; fixtures `tests/fixtures/source/atlas.{usda_psd,usda_wasde,fao_tm,ifpri_er}`
(excerpts of the live responses, licence and live sha256 in each manifest). Code: `src/reva/adapters/connectors/`
`agri_common.py`, `atlas_usda_psd.py`, `atlas_usda_wasde.py`, `atlas_fao_tm.py`, `atlas_ifpri_er.py`.
