# V3 adversarial review, second pass (agent V3REVIEW2, 2026-10-16)

Scope: the V3 components added after `docs/dev/review_v3.md`: the headline/source-order layer
(`reva/atlas/{query,source_order}.py`, `registry/atlas/source_order/**`, `/v1/atlas/headline/*`), the links store and
pair view (`reva/links/**`, migrations 0012/0013, `/v1/links/*`), the world simulations (`cohort_world.py`,
`retirement_world.py`, `retirement_coverage.py`, `registry/rules/retirement_age_wbl2026/`), a sample of the new sources
(UNTC/OHCHR, Olympics, KG polity overlay, Glottolog, BACI, peacekeeping, SPC/INSEE/Eurostat gap-fill, infra), the NLQA
additions (`links_pair`, headline tie-break in `fallback.py`) and the Pays ↔ Pays screen (`RelationPage.tsx`,
`relation.ts`). Fixed findings of the first review are not repeated. Other agents were editing during the review;
line numbers are those of the working tree at the end of the review.

Method: code reading; reads of the dev database `reva` (PostgreSQL, read-only queries only) and of its live atlas
snapshot `b11f1c51…` (159 tables) through the real `AtlasQuery` / `AtlasLinkReader` on the cached DuckDB query file,
opened read-only; values compared with the providers' published values fetched live, one request at a time.
Reproductions are in `tests/review_v3b/` (10 tests, all `xfail(strict=True)`; each becomes an XPASS failure once its
defect is fixed, so the marker goes with the fix). Synthetic tests build a verified query file in `tmp_path` and run
the real code; `dev_atlas` tests read the dev snapshot and skip when its query file is absent (they replace the
6.4 GB sha256 check by a meta check so a test can never delete the file).

Test status: `uv run pytest tests/review_v3b` → 10 xfailed (11 s). Related unit suites (`tests/unit/links`,
`tests/unit/nlqa`, `test_simfix_coverage.py`, `test_atlas_query_periods.py`, `test_atlas_treaties.py`,
`test_atlas_cities.py`, `test_atlas_culture.py`, `test_atlas_fiscal.py`) → 338 passed. DB integration suites were not
run (they create databases; this review wrote nothing to the server).

## Findings by severity

| ID | Sev. | Owner | Where | Repro |
|---|---|---|---|---|
| HL-2 | high | ATLASOPS | `src/reva/atlas/query.py:1029` (`PROJECTION_SQL`), `:679-735` (`_resolve`), `:958`; `registry/atlas/source_order/population.yaml`, `health.yaml` | `test_review_v3b_headline.py::test_headline_wdi_value_equal_to_the_excluded_wpp_projection_is_not_an_actual`, `::test_dev_india_2025_population_is_not_presented_as_an_actual` |
| HL-1 | high | ATLASOPS | `query.py:790-819` (`_headline`), `:759-788`; `source_order/population.yaml`, `health.yaml` | `::test_headline_series_marks_a_switch_of_answering_source`, `::test_dev_moldova_population_series_break_is_flagged` |
| LIC-1 | high | ATLASCORE / ATLASOPS / NLQA / UI3 | `query.py:368-400` (`_cell`); `atlas/snapshot.py:40-45`, `:130`; `nlqa/compose.py`; `ui/src/v3` atlas pages | `test_review_v3b_atlas.py::test_dev_non_commercial_ipu_cells_name_their_licence` |
| VOTE-1 | medium | LINKS | `src/reva/links/atlas_links.py:547-591`, `:598-612`; `links/service.py` `pair_view`; `nlqa` `links_pair` | `test_review_v3b_links.py::test_decade_vote_agreement_with_a_year_window_states_the_years_it_was_computed_on` |
| TRUNC-1 | medium | LINKS | `src/reva/links/service.py:425-475` (`_country_on_demand`), `atlas_links.py:299-316` | `::test_on_demand_country_view_reports_a_capped_atlas_read` |
| ASK-7 | medium | NLQA | `src/reva/nlqa/fallback.py:116-149` (`headline_tiebreak`) | `test_review_v3b_nlqa.py` |
| SEC-3 | medium | ATLASOPS | `api/routes/atlas.py:35`, `:414-420`; `atlas/query.py:186-213`, `:68`, `:278-285`, `:1164` | code + dev-DB figures |
| CSH-1 | medium | LINKS (+ KGGEOPOL) | `src/reva/links/dataset_kg.py:123-176` | `test_review_v3b_links.py::test_cshapes_colonial_period_starting_at_coverage_start_is_marked_left_censored` |
| LIC-2 | medium | LINKS | `src/reva/links/derive.py:745-810` (`derive_co_party`); `api/routes/links.py` `_envelope` | `::test_co_party_links_carry_the_untc_licence_restriction` |
| VOTE-2 | low | UI3 / NLQA | `ui/src/v3/lib/relation.ts:256`; `nlqa/compose.py:990` | code |
| ASK-8 | low | NLQA | `nlqa/catalog.py` country names (crosswalk English only); `fallback.py` `keyword_plan` | code + run |
| LAB-1 | low | A-ILO / ATLASOPS | ILOSTAT modelled estimates, vintage year | dev data |
| VOTE-3 | info | LINKS | `atlas_links.py:52-58` | — |

No critical finding: no SQL/DuckDB injection (every interpolated value is an int, a validated ISO3, or escaped with
`_sql_str`; store reads use bound parameters), `kg.link_*` grants are as documented (api/sim SELECT, worker
INSERT+SELECT, gate none; checked in the dev DB), and no fabricated value was found in the cells checked.

### High

**HL-2 — UN projections come back as "reported" headline values through the WDI.** The headline concepts
`reva.population.total` and `reva.health.life_expectancy` list WPP 2024 estimates first and say "projection years are
a separate indicator (`|proj=medium`), never used here". For 2024-2025 WPP has no estimate, so the WDI answers — but
the WDI takes WPP for most economies, i.e. its 2024-2025 values *are* the WPP 2024 medium-variant projections. In the
dev snapshot, the WDI population equals the WPP projection to the person for **129 of 216** countries in 2024 and
**120 of 216** in 2025 (143 within 0.05 %); WDI life expectancy 2024 is within 0.01 years of the WPP projection for
162 of 216. Example: India 2025 = 1,463,865,525 (WB API, live) = WPP `TPopulation1July|proj=medium` 1,463,865.525
thousand; the headline answer has `method: reported`, `answered_by: wb.wdi_social.SP.POP.TOTL`, `flags: {}`, and the
dossier's `latest` (`query.py:958`) shows it as the current population. 22 other WDI 2025 cells carry the footnote
"Extrapolated assuming the same growth rate as previous 6 months" — also not treated as a projection.
`PROJECTION_SQL` only reads the answering cell's own flags. Fix: in the WB connector, flag WDI cells whose year is after
the WPP estimate horizon (or whose footnote says extrapolated) as `estimate_kind: projection` /
`source_projection: un_wpp2024`; or in the headline layer, when an earlier-listed series' provider publishes a
projection for the same cell in the snapshot and the later-listed value equals it (or the year is past the earlier
series' estimate horizon), mark the answer `projection` and exclude it by default. State it in the population and
health caveats.

**HL-1 — the per-cell fallback stitches different definitions into one headline series, without saying so.**
`reva.population.total` declares WPP and WDI "the same definition", and `_headline` picks the first series per
(country, year). WPP ends in 2023, so every country's series switches source between 2023 and 2024. They are not the
same quantity for many countries: WPP "France" is metropolitan France (the overseas departments are separate WPP
locations), WDI France includes them; WPP Moldova includes Transnistria, WDI does not. In 2023 the two disagree by
more than 1 % for **39 of 216** countries and by more than 5 % for **11** (VIR +22 %, MDA −20 %, CUW −17 %, ALB −14 %,
TGO −12 %, QAT −11 %, TTO −9 %, ISR +6 %, SYC, POL −5 %, BGR); life expectancy differs by more than 1 year for 10.
Resulting headline series: France 66,438,822 (2023, WPP) → 68,551,653 (2024, WDI), +3.2 %; Moldova 3,067,070 →
2,402,306, **−21.7 % in one year**; USA 343.5 M → 340.0 M. Each point names its series, but nothing marks the break,
and `headline_dossier` compares "latest" with earlier years as one line. This is the blending the roadmap rules out
("no blending"). Fix: choose the answering series per country for the whole series (first listed series that covers
the country, falling back only for years before/after its coverage with a `series_break` flag carrying both values
of the overlap year), or flag every cell where `answered_by` changes from the previous year with
`series_break: {from, to, overlap_year, ratio}`; drop the "same definition" claim for WPP/WDI population (territorial
scope differs) and list the known scope differences (FRA, MDA, SRB/XKX, CYP, GEO…).

**LIC-1 — licence restrictions are not visible to API, `/v1/ask` or UI users of the atlas.** The dev snapshot serves
IPU Parline (CC BY-NC-SA 4.0: non-commercial, share-alike), V-Dem (CC BY-SA), TI CPI (CC BY-ND), Constitute and CCP
(commercial use prohibited), COW/WRP ("no charging for access"). An atlas cell (`AtlasQuery._cell`, `query.py:368`)
carries value, unit, flags and provenance only; snapshot manifest rows (`snapshot.py:40-45`) carry no rights, so the
query layer cannot add them. Whether a user sees a restriction depends on whether the connector copied the licence
into its own flags: Constitute, CCP, SSA, Gapminder do; IPU, V-Dem, TI, WPP do not (IPU cell for Angola 2023:
flags `{ipu_chamber, provider_interval, selection}`). Some catalog concepts mention it in free-text `notes`/`tags`
(`politics.yaml`), the dossier and headline answers do not, `/v1/ask` never mentions a licence (no occurrence in
`src/reva/nlqa`), and the V3 atlas UI shows none (the relation page now shows `basis.licence` of link kinds that
have one, see LIC-2). Fix: carry `rights` (licence, commercial_use, share_alike, no_derivatives, attribution) from the
dataset artifact into the snapshot manifest rows and into every cell/headline answer (`provenance.rights`); add an
envelope warning `licence_restricted` (API) and a line in `/v1/ask` answers and the UI whenever an answer contains a
non-open source.

### Medium

**VOTE-1 — a decade vote-agreement value computed on part of the decade is labelled and cited as the whole decade.**
`_vote_groups` filters roll calls by `year_lo/year_hi` (`atlas_links.py:569-572`) but `_vote_link` labels the link
`[decade, decade+9]` and writes `"years": [lo, hi]` of the *decade* into the evidence selector (`:600`, `:608`). On the
dev snapshot, USA–GBR colonialism-coded votes: whole 1960s 0.8673 (98/113); with `from_year=1965` the pair view
returns a link "1960–1969" of **0.8636 (57/66)** whose selector says `"years":[1960,1969]` — the citation no longer
reproduces the number. Reached through `/v1/links/pair?from_year=…` and NLQA `links_pair` with years ("liens entre
les États-Unis et le Royaume-Uni 1965 1975"). Fix: write the effective years (`max(lo, year_lo)`, `min(hi, year_hi)`)
in the selector and the temporal, or compute decades only over whole decades and drop partial ones (flagged).

**TRUNC-1 — the on-demand country view ranks over a silently truncated read.** Without a materialisation (the dev DB
has none), `_country_on_demand` reads each atlas kind with `cap=50_000` (votes `20_000`) and ignores
`d.skipped["row_cap_reached"]`; the capped SQL is ordered by `(subject, object, year, facet)`, so what is cut is the
alphabetical tail of partners, and the top-N is then computed in Python on what is left; `truncated` is not set, so
the route emits no warning. Repro: 250 partners × 201 years for USA → the top export partner shown is `JNQ`, not the
largest (`ZWE`). On the dev snapshot USA reads 38,699 BACI rows (under the cap), so it is latent there, but a full
BACI load (30 years instead of 13) crosses it. Fix: rank in SQL per (kind, facet, direction, latest period) as the
store does, or at least set `truncated` and add the kind to the warning when any reader reports `row_cap_reached`.

**ASK-7 — the headline tie-break picks a series the headline declares not equivalent.** "Population de la France en
2025" ties five catalog concepts; `headline_tiebreak` counts membership of `related_catalog_concepts` as coverage and
never reads `not_equivalent`, so it returns `imf.population` (WEO `LP`) with `resolution: headline,
headline: reva.population.total` — the series that headline lists under `not_equivalent` ("national definitions …
WEO years after the latest actual are projections"). The answer is then presented as REVA's declared best-available
quantity. Fix: drop candidates whose `source_order` intersects the headline's `not_equivalent` series; and, when the
headline concept itself is the topic, answer with the headline concept (its answering source per country) rather than
one catalog concept.

**SEC-3 — a read-scope `?snapshot=` forces full query-file builds in the request and evicts the live one.**
`/v1/atlas/*` and the new `/v1/atlas/headline/*` accept any recorded snapshot sha (`SNAPSHOT_Q`, `routes/atlas.py:35`);
`_query` → `query_for` → `AtlasQuery` → `build_snapshot_db` builds a missing query file synchronously (dev: 886 s,
6.4 GB, `REVA_ATLAS_BUILD_MEMORY` 3 GB, spill on disk) and `_evict` keeps only `KEEP_SNAPSHOT_DBS = 2` files, so
alternating three old shas evicts the current live file and every following request rebuilds it. The links routes
(`_atlas_factory`) also build the latest snapshot's file in the request when it is missing. With a daily snapshot schedule, production accumulates shas quickly. Fix:
build query files only in the worker (snapshot job); the API serves only files that exist (otherwise 503
`snapshot_not_materialised`), never evicts the latest live one, and limits `?snapshot=` to a small allow-list (latest
live + pinned) for the `read` scope.

**CSH-1 — CShapes coverage start presented as the start of colonial rule.** CShapes 2.0 begins on 1886-01-01.
`derive_cshapes_colonial` copies the dependency period as-is, so Algeria — French rule from 1830 — is served as
"territory ruled by France, 1886-01-01 → 1962-07-04" at day precision (fixture parse: four periods from 1886-01-01,
colony of gw 220; independent from 1962-07-05). The taxonomy notes mention the 1886 start, but nothing on the link,
in the API, the NLQA sentence or the UI does. Fix: when a period starts on the dataset's first day, set
`basis.start_basis = "dataset coverage start (CShapes 2.0 begins 1886-01-01); rule began earlier or at this date"` and
render the start as "≤ 1886" (left-censored), not as a date.

**LIC-2 — `co_party` links drop the UN Treaty Collection terms.** A-TREATIES records UNTC data as "not an open licence"
(UN Terms of Use: personal, non-commercial use, no derivative works; `open_licence: false`), but `derive_co_party`
builds its basis without any licence (`derive.py:796-806`), while CShapes, OpenFlights and GCAT links do carry
`basis.licence`; the relation page (which now lists `basis.licence` per kind) therefore shows nothing for treaties,
and `/v1/links/*` emits no licence warning for any kind. Fix: add `licence` / `open_licence: false` to the co_party
basis (and `untc:party` path hops), and an envelope warning when any returned link has a non-open licence.

### Low / info

* **VOTE-2** — the UI vote table (`relation.ts:256`) and the NLQA sentence (`compose.py:990`) print the decade label
  ("2020–2029") although the data stop in 2024; `basis.roll_call_years` holds the real span and is not used. Show
  `roll_call_years` (and "partial decade").
* **ASK-8** — the keyword planner knows only the crosswalk's English names (`BRA: ('Brazil',)`), so "Quelle est la
  population totale du Brésil ?" yields a world ranking (`atlas_rank` of `un.wpp.population`) instead of Brazil's
  value. Add French names (and common forms) to the planner's country table.
* **LAB-1** — ILOSTAT modelled unemployment (Nov. 2025 vintage): 2026-2027 are flagged projections, but 2025 (the
  vintage year, without `obs_status` "R") is not; `reva.labour.unemployment_rate` therefore answers 2025 with a nowcast
  as an actual (FRA 7.542). Flag the vintage year as `nowcast` (or exclude cells without `obs_status`).
* **VOTE-3 (info)** — REVA's index is a strict share of identical votes (abstain = abstain counts, abstain vs yes
  counts 0). It is not Voeten/Bailey's `agree` index (half credit when one abstains), so REVA's numbers are not
  comparable with the literature's; the caveat says "share of identical votes" but could name the difference.

## Spot checks (REVA vs provider, live 2026-10-16)

REVA values come from the dev snapshot `b11f1c51…` through `AtlasQuery` / `AtlasLinkReader` (read-only) unless noted.

| # | Cell | REVA | Provider (live) | Result | Provider URL |
|---|---|---|---|---|---|
| 1 | FRA population 2025 (headline, WDI) | 68,720,337 | 68,720,337 | match | https://api.worldbank.org/v2/country/FRA/indicator/SP.POP.TOTL?format=json&date=2023:2025 |
| 2 | FRA population 2023 (headline) | 66,438,822 (WPP, metropolitan) | WB 68,372,286 (incl. DOM) | faithful to WPP; **headline stitches the two (HL-1)** | same |
| 3 | MDA population 2023 → 2024 (headline) | 3,067,070 (WPP) → 2,402,306 (WDI) | WB 2,457,783 / 2,402,306 | WDI match; −21.7 % break (HL-1) | https://api.worldbank.org/v2/country/IND;MDA/indicator/SP.POP.TOTL?format=json&date=2023:2025 |
| 4 | IND population 2025 (headline, WDI) | 1,463,865,525, flags `{}` | WB 1,463,865,525 = WPP 2024 medium projection | match; **projection served as actual (HL-2)** | same |
| 5 | BRA → PRT exports 2024 (IMF IMTS) | 3,416,220,938 USD | 3416220938 (SCALE 6 kept as `display_scale`) | match | https://api.imf.org/external/sdmx/2.1/data/IMF.STA,IMTS/BRA.XG_FOB_USD.PRT.A?startPeriod=2024&endPeriod=2024 |
| 6 | USA–GBR share of identical votes, colonialism, 1960s | 0.8673 (98/113) | recomputed from the KG `harvard.dataverse.unvotes` statements (independent loader, same archived RData `987f89d8…`): 98/113 | match | Voeten UNVotes (Harvard Dataverse) |
| 7 | same, pair view `from_year=1965` | "1960–1969" 0.8636 (57/66), selector years [1960,1969] | 57/66 is 1965-1969 only | **mislabelled (VOTE-1)** | — |
| 8 | Algeria colonial period (CShapes 2.0 gw) | colony of 220 (France) 1886-01-01 → 1962-07-04 (4 periods); independent from 1962-07-05 | CRAN `cshapes_2.0.tar.gz` (recorded fixture, parsed by the connector) | match; start = coverage start (CSH-1) | https://cran.r-project.org/src/contrib/cshapes_2.0.tar.gz |
| 9 | WHO life expectancy JPN 2021 (both sexes / M / F) | 84.3 / 81.6 / 86.9 | 84.3 / 81.6 / 86.9 | match | https://ghoapi.azureedge.net/api/WHOSIS_000001?$filter=SpatialDim eq 'JPN' and TimeDim eq 2021 |
| 10 | OECD total tax revenue % GDP, DNK 2024 | not in the dev snapshot (`oecd.rs_global` not loaded); live response through `atlas_oecd_fiscal.oecd_build` → 45.177061 "Percentage of GDP" (2021-2023: 47.470033, 42.054919, 43.991573) | 45.177061 | parser faithful; n/a in DB | https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_REV_COMP_GLOBAL@DF_RSGLOBAL,/DNK.TAX_REV.S13._T._T.PT_B1GQ.A?format=csvfilewithlabels&startPeriod=2021 |
| 11 | Glottolog Portuguese (port1283) ancestors | 15: braz1247 … roma1334 … indo1319 (connector `chain` on the recorded v5.3 release) | same 15, same order | match | https://glottolog.org/resource/languoid/id/port1283.json |
| 12 | UN WUP 2025 Tokyo (city 6030) 2025 | 33,412.512 thousand (mid-year `Pop`, `projection: false`, capital) — connector `build_cities` on the live file | `Pop` 33412.512, `Pop_1Jan` 33441.148 | match (not in the dev snapshot) | https://population.un.org/wup/assets/Download/Cities/WUP2025-DB-DEGURBA-Cities-Population-Surface-Data.csv.gz |
| 13 | USGS lithium mine production 2025 (top producer) | AUS 92,000 t, CHN 62,000, CHL 56,000, ZWE 28,000, ARG 23,000; `estimate_kind: estimated`; USA `missing: confidential` (no value) | AUS 92,000 (2025e), CHL 56,000, ARG 23,000; USA "W" | match; withheld kept as missing, never 0 | https://pubs.usgs.gov/periodicals/mcs2026/mcs2026-lithium.pdf |
| 14 | USGS lithium reserves AUS / CHL | 8,400,000 / 9,200,000 t | 8,400,000 / 9,200,000 | match | same |
| 15 | USA GDP current US$ 2024 (WDI) | 29,298,013,000,000 | 29298013000000 | match | https://api.worldbank.org/v2/country/USA/indicator/NY.GDP.MKTP.CD?format=json&date=2024 |
| 16 | Eurostat land/total area, Guadeloupe (FRY1) gap-fill | headline series `reg_area3` `L0008` = land area, `TOTAL` = total area | codelist: L0008 "Land area - total", TOTAL "Total area"; FRY1 1,680 / 1,685 km² | code meanings match (series not in the dev snapshot) | https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/reg_area3?geo=FRY1&unit=KM2&landuse=L0008&landuse=TOTAL |
| 17 | WDI life expectancy 2024 vs WPP 2024 projection | 162 of 216 within 0.01 y (JPN 84.036 WDI vs WPP 2023 est. 84.712) | — | projection served as actual (HL-2), break (HL-1) | — |
| 18 | UN DESA migrant stock, born in BRA living in PRT, 2024 | 173,266 (F 100,009, M 73,257) | not refetched (6 MB xlsx); equals LINKS2's live-ingest value | consistent | — |
| 19 | UNDP HDI NOR 2023 | 0.97 | 0.97 (HDR25, fetched in review V3) | match (now readable from the dev DB) | https://hdr.undp.org/sites/default/files/2025_HDR/HDR25_Composite_indices_complete_time_series.csv |
| 20 | OWID CO₂ per capita CHN 2022 | 8.218 t | 8.217776 (grapher, fetched in review V3) | match (3 d.p. as in the OWID CSV) | https://ourworldindata.org/grapher/co-emissions-per-capita.csv |

ILOSTAT's API answered HTTP 200 with an empty body during this review, so no new ILOSTAT value was rechecked (review
V3 checked SEN/NOR/FRA).

## Verified as sound

* **Headline registry and query**: equivalences validated by schema and by `same_source` for derived pairs (a concept
  pair expands into same-provider series pairs); `reported_unit` mismatches are refused (`unit_mismatch`), never
  converted; WPP thousands are scaled exactly in `Decimal`; derived ratios refuse a non-positive denominator, cite both
  input cells and lift their estimate/projection flags; "named per cell" units keep the cell's own currency;
  countries without a value are listed with every consulted status; concept, year and ISO3 are validated before any
  query; `headline_coverage` is memoized per registry fingerprint.
* **Links store**: append-only (`immutable_record`), rows sealed to the transaction of their materialisation,
  `evidence` non-empty by CHECK, no zero-weight row, grants exactly as documented (checked in the dev DB);
  `links_materialise` is allow-listed in `ops.submit_job` but no API route submits it.
* **Pair view**: inputs validated (ISO3, kinds against the taxonomy, years, vote detail ≤ 11 years); 8 s budget
  checked between kinds; mirror flows kept apart; historical voters keep their own node (USSR never RUS); vote
  formula = identical / common (yes/no/abstain; absent and non-member excluded), N shown, `n_common ≥ 10`; constrained
  path search bounded (2,000,000 DFS steps, 20 s, `max_hops ≤ 8`, ≤ 8 steps).
* **BACI**: exact integer thousandths; only float-noise tails (≥ 6 zeros/nines after the 3rd decimal) are rounded and
  counted, any other extra precision is refused; chapter → section map checked; mirror symmetry stated as CEPII's
  reconciliation; "Other Asia, nes" (490) and historical codes kept unmapped.
* **Retirement 1.2.0**: coverage is a dataset input (latest year ≤ reference year, ≤ 10 years old, by sex or total,
  0-100 checked), never assumed; `c = 1` reproduces 1.1.0; employment and pension-receipt changes are scaled by `c`
  consistently (`retirement_coverage.py:328-383`); refusals typed (`missing_input`); the non-contributory-pension bias
  is stated in every row. Rule tables are WBL 2026 cells cited per row with the "held constant" path labelled a
  counterfactual. **Cohort 1.1.0** (WPP): jump-off ≤ 1 January 2023, projections never used as observed input,
  EUROPOP options refused for WPP, back-test framed as consistency with WPP rather than truth.
* **Sources sampled**: UNTC parses only what the page states (brackets, notes with dates of effect, successions kept
  as written) and records the UN terms as non-open; Olympics rows are labelled `community_edited_cites_ioc` with the
  page revision, historical NOCs unmapped; the KG overlay is labelled derived (planar lon/lat intersection, approximate
  Cliopatria borders, nothing about sovereignty); peacekeeping keeps reported zeros, never imputes, leaves dissolved
  predecessors unmapped; SPC keeps only cells whose `DATA_YEAR` is the cell year and spells the unit multiplier out;
  USGS keeps "W" as `missing: confidential`; OpenFlights and Wikidata cables carry their staleness/licence or
  community-edited caveats; no "alliance" or causal wording in the vote and pair texts (test-enforced in the UI).
