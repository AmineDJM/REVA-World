# V3 — A-ECRISES: economic and financial crises (events, coded relations, atlas series)

Agent A-ECRISES, 2026-10-16. One live ingest of all four sources into the per-session throwaway database of
`tests/integration/test_atlas_ecrises_live.py` (dropped afterwards) through the atlas platform
(`reva.atlas.ingest.run_atlas_ingest`, live mode, budget-metered with the requested limits overlaid on
`budgets-1.3.6`): **5 requests, 3.3 MB, 0 refused, 90 s**. The dev database `reva` was never touched. Nothing is
committed.

## 1. Sources: access, terms, decision

| Source | Access | Terms (read 2026-10-16) | Decision |
|---|---|---|---|
| **Laeven & Valencia, Systemic Banking Crises Database II** (IMF Econ. Review 2020; 1970-2017) | data file = Springer ESM `41308_2020_107_MOESM1_ESM.xlsx` (121 kB, static-content.springer.com). The IMF copy of the 2018 WP dataset (`imf.org/.../datasets/wp18206.ashx`) answers 404 (429 before). | © IMF 2020, article not open access. IMF "Special Terms for Statistical Data": reuse/redistribution with attribution; general IMF content is personal/non-commercial. **Not an open licence**; whether the data terms cover this research file is flagged for maintainer review. | ingested: `laeven_valencia.sbcd` |
| **Reinhart & Rogoff "This Time Is Different"** crisis data (70 countries, 1800-2016) | HBS Behavioral Finance & Financial Stability: `20160923_global_crisis_data.xlsx` (1.6 MB) | **No licence stated** ("keeps this data updated and available for download"; Harvard copyright footer). Not open; flagged for review, citation on every row (COW precedent). | ingested: `reinhart_rogoff.global_crises` |
| **Jordà-Schularick-Taylor Macrohistory Database R6** (18 economies, 1870-2020) | `JSTdatasetR6.xlsx` (1.4 MB; 302 to `sfff5b3ac9317c4be.jimcontent.com`) + licence page | **CC BY-NC-SA 4.0**, commercial data providers expressly forbidden. Followed the CShapes / IPU precedent: ingested with `non_commercial: true`, `share_alike`, `restriction` ("exclude source `jst` from any commercial deployment"); the licence page is fetched and checked on **every run** (other licence = refused). | ingested: `jst.macrohistory` |
| **BIS banking crises** | BIS SDMX dataflow list checked: no crisis dataflow; no open BIS crisis list. | — | not available |
| **ESRB European financial crises database** (ESRB/ECB, Lo Duca et al. 2017 OP 13, updated) — the official-sector replacement for a BIS list | `esrb.fcdb20220120.en.xlsx` (92 kB) | ESRB copyright: free use, accurate reproduction, ESRB cited. | ingested: `esrb.fcdb` |

No source needs personal data. RR cell comments carry the coder's first name in their text ("Carmen: Not a
systemic banking crisis"); they are kept verbatim as provenance flags.

## 2. What was built (all files owned by A-ECRISES)

| File | Purpose |
|---|---|
| `src/reva/adapters/connectors/atlas_ecrises.py` | four atlas sources (`atlas.ecrises.{laeven_valencia,reinhart_rogoff,jst,esrb}`), parsers, events, coded relations, KG load, evidence datasets, T1 claim delivery to CAUSCORE |
| `registry/sources/{laeven_valencia,reinhart_rogoff,jst_macrohistory,esrb_fcdb}.yaml` | rights with quoted evidence, hosts, review flags |
| `registry/atlas/catalog/crises_economic.yaml` | 21 provider-declared concepts (`ecrises.*`, theme economy; one concept per dataset coding, never blended) |
| `registry/atlas/geo_aliases/a_ecrises.yaml` | scheme `laeven_valencia` (177 country names → crosswalk, each with its evidence; `Yugoslavia, SFR` unmapped) and `esrb` (ISO2 + `UK`/`UK*` → GBR, quoting the file's note) |
| `registry/atlas/budget_requests/a_ecrises.yaml` | 3-6 requests/day per source |
| `registry/causal/relations_ecrises.yaml` | every coded relation rule with its codebook text, rationale, extraction; the fields deliberately **not** mapped and why |
| `tests/fixtures/source/atlas.ecrises.*` (0.95 MB) | live captures; RR and JST workbooks TRIMMED to 7 / 5 countries (manifest says how) |
| `tests/unit/test_atlas_ecrises.py` (18), `tests/integration/test_atlas_ecrises_ingest.py` (2, `db`), `tests/integration/test_atlas_ecrises_live.py` (1, `live`+`db`, passed 2026-10-16) | |

Run: `uv run python -m reva.atlas.ingest atlas.ecrises.esrb [--fixture]` (option `kg=false` skips KG/datasets/claims).

## 3. Atlas tables (live, exactly as published)

| table | rows | indicators | countries (crosswalk) | years | notes |
|---|---|---|---|---|---|
| `atlas.laeven_valencia.sbcd` | 3,531 | 57 | 156 (+`Yugoslavia, SFR`, world counts) | 1971-2017 | `crisis_start|type=…` (1 at each listed start year: 151 banking, 240 currency, 80 sovereign debt, 75 restructurings), outcomes at the start year (`output_loss`, `fiscal_cost_gross/net/financial_assets`, `peak_liquidity`, `liquidity_support`, `peak_npl`, `public_debt_increase`, `banking_crisis_end_year` incl. "ongoing"), 1,783 `detail|row=n|item=…` cells (spreadsheet dates → ISO text with `excel_serial`), world `crisis_count|type=…` incl. twin/triple with the definition footnote; `...`/`…`/`n.a.`/empty cells omitted (304) |
| `atlas.reinhart_rogoff.global_crises` | 171,546 | 18 | 70 | 1800-2016 | crisis/default/gold/independence dummies, inflation, `exch_usd*` (provider source notes in flags), notes as text; `n/a`/blank/`#REF!`/`#VALUE!` omitted and counted; column `<` (no definition) skipped; currency code **2** (38 cells, undocumented) kept and flagged; 167 provider cell comments in flags |
| `atlas.jst.macrohistory` | 116,882 | 55 | 18 | 1870-2020 | every R6 variable verbatim; labels/units from the R6 documentation table; `crisisJST` with the chronology definition |
| `atlas.esrb.fcdb` | 1,785 | 34 | 28 | 1973-2020 | one cell per event field at the start year (`systemic|…`, `residual|…`): dates `YYYY-MM`, dummies, texts |

Cell values are the workbook's cell text (strict decimals, e.g. `58.171234800000008`); never rescaled.

## 4. Events

Events are `kg.entity` kind `event` (`ecrises:<lv|rr|jst|esrb>:<type>:<ISO3>:<start>`), statements P31 (class),
P17 (country QID from `country_qids.json`), **P580/P582** (the dates the causal grader reads),
`ecrises:crisis_type`, `ecrises:coded_date` (other coded dates with the field name), and evidence datasets
`ecrises.<source>.events` (concept `events.crisis_episodes`, attributes JSON: description, borderline,
provider notes/comments, ESRB categories, origin domestic/imported…).

| source | events (live) | types | dates |
|---|---|---|---|
| Laeven-Valencia | 546 | 151 systemic banking, 240 currency, 80 sovereign debt, 75 restructurings | start year; month when the monthly sheet / start-date cells give it (precision 10); banking end year (with the authors' footnote: "truncated at 5 years", "end dates based on GDP growth only"); "date when crisis became systemic"; footnote 8/ → `borderline` |
| Reinhart-Rogoff | 1,696 | 303 banking, 644 currency, 485 inflation, 208 external default (series 2; years also in series 1 listed), 56 domestic default | **episodes = maximal runs of consecutive years coded 1** (rule `rr_consecutive_years`, stated on each event; RR code years, not episodes) |
| JST | 88 | systemic banking (`crisisJST`) | start year only (the variable codes the first year) |
| ESRB | 119 | 77 systemic, 42 residual stress events | start month, end of crisis management (P582), back-to-normal and first-default dates |

Classes (P31): Q806729 banking crisis, Q1417912 currency crisis, Q1092023 sovereign default, Q1364290 debt
restructuring, Q114380 financial crisis (labels/descriptions quoted in `WIKIDATA_CLASSES`); inflation crises and
residual stress events get no P31. 161 crosswalk countries have at least one event (52 in the 1800s, 149 in the
1900s, 100 since 2000). No cross-dataset `same_as` was asserted (left to EVENTS, see §7).

## 5. Coded relations (`registry/causal/relations_ecrises.yaml`)

| rule | family / relation | count | basis |
|---|---|---|---|
| `esrb_fcdb_accelerator` | **causal** `contributing_factor` (T1, polarity promotes) from the ESRB intermediate objective (condition entity `ecrises:condition:esrb_objective:*`) to the event | **96 claims on 47 events** (credit growth 37, exposure concentration 25, mismatches 16, misaligned incentives 16, infrastructure 2) | OP 13 Annex A defines the field as "indicator of one or more of the five ESRB intermediate macroprudential objectives"; only the text before "Motivation:" is read; mechanism = whole cell verbatim. 41 events name no objective (Covid "exogenous health crisis", "External shock.", …) → no claim, listed |
| `lv2020_twin_*`, `lv2020_triple` | **context** `same_period` (KG predicate `ecrises:same_crisis_window`, `not_causal: true`) | 77 edges | data-file footnotes 1/ 2/ (crises within [t-1, t+1]); co-occurrence, explicitly not causal |

Live: the 96 claims went through `reva.causal.model.load_dataset_claims` (complete delivery, codebook + term
`accelerator`, 0 rejected, 0 unknown entities) and `grade_claims`: **all B, temporal `undetermined`** (the
vulnerability is undated in the source, so the grade cannot reach A). Re-loading is a no-op (tested).

Twin/triple check (REVA applies the authors' definition to their dates and compares with their yearly counts):
triple 11/11, banking-sovereign 3/3, currency-sovereign 20/20 match (reading: currency crises belonging to a triple
are excluded); banking-currency **32 vs 31** (1992: 3 vs 2) — consistent with the authors' own yearly currency count
(1992: 6 published vs 9 listed in "Crisis Years"; 2012: 3 vs 4), probably the early-transition exclusion of
WP/18/206 footnote 11. Reported per edge (`published_count_check`), never "fixed". Banking counts match exactly;
the frequency sheet's "Sovereign" column (91) does not match the listed sovereign debt crises (80) year by year.

Not mapped (reasons in the YAML): LV restructuring↔default pairing (the authors do not pair them), the WP's
aggregate sequencing pattern, LV/ESRB narrative descriptions, RR notes ("Oil and interest rate shocks",
"Depression"…), JST chronology narratives, ESRB "domestic vs imported" (attribute: no originating event named)
and the ESRB category dummies (symptoms).

## 6. Coverage and gaps

* Crisis coverage by country: LV 156 countries (1970-2017), RR 70 (1800-2016), JST 18 advanced economies
  (1870-2020), ESRB 28 EU/EEA (+ the UK until 2019). Union: 161 crosswalk countries with ≥ 1 crisis event; small
  territories and most micro-states have none in any dataset.
* After 2017 (LV) / 2016 (RR) only ESRB (Covid 2020 events for 27 countries) and JST (to 2020; last crisis start
  2008) extend coverage; no open global crisis list covers 2018-2026.
* Historical states: `Yugoslavia, SFR` (LV) kept unmapped (no QID asserted); RR/JST use current codes.

## 7. Requests for other agents (no shared file edited)

1. **ATLASCORE**: merge `registry/atlas/budget_requests/a_ecrises.yaml` (until then invariant 30
   `test_budget_keys_are_registered_sources_and_cover_every_connector` lists the four keys, like other new V3
   sources, and a live run is refused outside the overlay test).
2. **CAUSCORE**: `reva/causal/__init__.py` is still empty; this connector imports `reva.causal.model.Codebook /
   load_dataset_claims` and `reva.causal.grader.grade_claims` directly (switch to the package exports when they
   land). Context edges are KG statements `ecrises:same_crisis_window` (no context-edge table in the interface).
3. **EVENTS**: candidate `same_as` evidence — the JST Financial Crisis Chronology (section 4, PDF) tabulates JST
   crises against Laeven-Valencia, Reinhart-Rogoff, Bordo et al. and BVX (an authors' mapping table, not parsed:
   PDF check-mark layout). The ESRB OP 13 says events marked "L&V" were imported from Laeven-Valencia (none are
   marked in the 2022 file). Event kinds/types here: `event` + `ecrises:crisis_type`; map to
   `registry/events/taxonomy.yaml` (crisis → financial) when it exists.
4. **KGCORE / WDHARVEST**: please confirm the dataset predicates `ecrises:crisis_type` (string),
   `ecrises:coded_date` (time) and `ecrises:same_crisis_window` (item, context, not causal).
5. **Maintainers**: rights review for Laeven-Valencia (IMF) and Reinhart-Rogoff (no terms); JST is
   non-commercial.
