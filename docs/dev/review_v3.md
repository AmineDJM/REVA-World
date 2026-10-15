# V3 adversarial review (agent V3REVIEW, 2026-10-15)

Scope: the finished V3 components — KG (`0009_kg.py`, `reva.kg.model/query`, `/v1/kg`), atlas core
(`reva.atlas.*`, `/v1/atlas`), interconnections (`reva.links.*`, `registry/links`, `/v1/links`), questions
(`reva.nlqa.*`, `POST /v1/ask`) and the atlas sources IMF, UNDP, WHR, OWID, WID, ILOSTAT, Eurostat/OECD wages
(+ their catalogs). Read against `docs/roadmap/v3_world_graph.md` (honesty rules), `docs/data_constitution.md`
and `docs/threat_model.md`. Other agents were editing during the review (ATLASOPS added the headline layer in
`reva/atlas/query.py` + `source_order.py` while this was written); line numbers are those of the working tree
at the end of the review.

Method: code reading; reproductions as `xfail(strict=True)` tests in `tests/review_v3/` (14 tests, all xfail —
each turns into an XPASS failure once its defect is fixed, so remove the marker with the fix); the recorded
provider fixtures parsed through the real connectors (no DB) and compared with values fetched live from the
providers (one small request at a time); the requested unit and integration suites.

Test status (working tree at review end): `tests/unit/links tests/unit/nlqa tests/unit/test_kg_model.py
tests/unit/test_atlas_*.py` — 471 passed (first run); integration `test_kg_api, test_kg_paths, test_kg_store,
test_atlas_api, test_atlas_ingest, test_links_db, test_nlqa_api` + `tests/review_v3` — 60 passed, 14 xfailed.

## Findings by severity

| ID | Sev. | Owner | Where | Repro |
|---|---|---|---|---|
| ASK-1 | high | NLQA | `src/reva/nlqa/compose.py:582`, `:624`; `executor.py:579` | `test_review_v3_nlqa.py::test_rank_never_compares_or_mislabels_different_units`, `::test_series_sentence_never_labels_mixed_currencies_with_one_unit` |
| ASK-2 | high | NLQA | `compose.py:577-615`, `:618-646`; `executor.py:579-604` | `::test_series_sentence_marks_projections`, `::test_rank_latest_year_is_not_silently_a_projection` |
| KG-2 | high | WDHARVEST | `src/reva/kg/harvest.py:874-884` | live Wikidata check below |
| ATLAS-2 | high | ATLASOPS | `src/reva/atlas/query.py:148`, `:317`, `:338`, `:380` | `test_review_v3_atlas.py::test_non_calendar_cells_are_not_reported_as_absent` (2 cases) |
| ATLAS-1 | medium | ATLASOPS | `query.py:89-90`, `:114-118`, `:201-204` | `::test_tampered_duckdb_cache_is_refused` |
| ATLAS-3 | medium | ATLASOPS | `query.py:531-539`; `api/routes/atlas.py:108-113` | `::test_dossier_series_points_carry_their_unit` |
| ASK-3 | medium | NLQA | `compose.py:155-159` | `::test_value_sentence_keeps_the_projection_flag` |
| ASK-4 | medium | NLQA | `rephrase.py:39`, `:151-175` | `::test_rephrase_that_swaps_values_between_countries_is_rejected`, `::test_rephrase_that_drops_the_projection_label_is_rejected` |
| KG-1 | medium | KGCORE | `src/reva/kg/model.py:954-960`; `0009_kg.py:199` | `test_review_v3_kg_db.py` (db) |
| PROV-1 | medium | WDHARVEST | `harvest.py:864-865` | code |
| LINKS-1 | medium | LINKS | `src/reva/links/paths.py:479`, `:762`; `service.py:78`, `:110` | `test_review_v3_links.py` |
| SEC-1 | medium | KGCORE / LINKS / NLQA | `kg/query.py:599-621`, `api/routes/kg.py:372`; `links/paths.py:728-762`, `:540`; `nlqa/executor.py:482-493` | code + bounds arithmetic |
| ATLAS-4 | medium | A-IMF / ATLASCORE | `adapters/connectors/atlas_imf.py:547-548`, `:626` | fixture cell IND NGDPD 2024 |
| ATLAS-5 | low | ATLASOPS | `api/routes/atlas.py:30`, `:398-400`; `atlas/snapshot.py` `LATEST_SQL` | code |
| ATLAS-6 | low (in flight) | ATLASOPS | `query.py:640`, `:713-732` | code |
| KG-3 | low | KGCORE | `api/routes/kg.py:232-239`; `kg/query.py:344`, `:520` | code |
| LINKS-2 | low | LINKS | `links/paths.py:713-724`; `nlqa/adapters.py:204-216` | live Wikidata check below |
| LINKS-3 | low | LINKS | `api/routes/links.py:42`; `links/model.py:445-455` | code |
| ASK-5 | low | NLQA | `nlqa/ports.py` `AtlasCell.year`; `compose.py:119-127`, `:626` | `::test_bce_atlas_cell_year` |
| ASK-6 | low | NLQA | `nlqa/plan.py` `_validate_args` (`entity_lookup`), `restate` | code |
| SEC-2 | low | KGCORE | `migrations/versions/0009_kg.py:406` | code |
| DATA-1 | low | A-ILO | `registry/atlas/catalog/labour.yaml:381-393`; `atlas_wages_gapfill.py` `EUROSTAT_CURRENCY_UNITS` | fixture cells |
| KG-4 | low | KGCORE | `kg/query.py:448-480` | code |

No critical finding (no write path for runtime roles into `kg`, no SQL injection, no fabricated value found in
the connectors reviewed).

### High

**ASK-1 — `/v1/ask` compares and labels different currencies as one unit.**
`op_atlas_rank` labels the whole ranking with `rows[0][0].unit` (`compose.py:582`) and `rank_cells`
(`executor.py:579`) ranks raw numbers of one series across countries even when the cells' units differ. ILOSTAT
`CUR_TYPE_LCU` (and OECD `unit=NCU`) series name a different currency per country, so "highest minimum wages"
yields: *"Monthly minimum wage (local currency) (KRW per month), 2019, highest values … 1. Republic of Korea:
1,745,150; 2. Senegal: 58,900; 3. France: 1,521.22."* — XOF and EUR values printed as KRW and ranked against won.
`op_atlas_series` (`compose.py:624`) does the same over time: ILOSTAT's real Austrian mean-earnings series
switches from ATS (1969-1970) to EUR (2000-) inside one series (fixture `atlas.ilostat`, AUT
`EAR_EMTA_SEX_CUR_NB|…|CUR_TYPE_LCU`), so EUR values are printed "(ATS per month)".
Fix: in `rank_cells` refuse (typed `unsupported: units_differ`) when the ranked cells do not share one unit;
mark currency-per-cell concepts as not rankable in the catalog (e.g. `rankable: false`); in `op_atlas_series`
split the points by unit (one sentence per unit) or print the unit on every point.

**ASK-2 — projections stated as facts.** Series and ranking sentences never render cell flags
(`op_atlas_series`, `op_atlas_rank`), and a ranking without a year picks "the most recent year covering ≥ 80 %
of the best year" (`RANK_LATEST_COVERAGE`), which for IMF WEO is a projection year (every country is projected
to 2031). Repro answers: *"Unemployment rate …, 2031, highest values according to …: 1. France: 7.1 …"* and
*"… 2024: ≈ 4.02; 2025: ≈ 4.28; 2030: ≈ 3.9."* with WEO `estimate_kind=projection` cells and no mention of it.
The same applies to `atlas_value` with `country=ALL` (one "fact" sentence citing every year, projections
included, `executor.py:482-493`). Fix: reuse ATLASOPS' `PROJECTION_SQL` semantics (`estimate_kind`, truthy
`projection`, `period_after_vintage`) in a shared `is_projection(flags)` helper; exclude projections from the
default "latest year" choice; when a requested year/series includes them, say so per point ("2030 (IMF
projection)") and add `projection` to the rephrase keep-markers (ASK-4).

**KG-2 — false date precision for every WDQS-harvested date (honesty: "never false precision" is false).**
`coarse_precision` (`harvest.py:874-884`) assigns precision 9/10/11 from the shape of the `xsd:dateTime`;
no query spec selects `wikibase:timePrecision` (0 of 63 specs under `registry/kg/queries`). Every
century/decade/millennium date becomes a year. Live check (WDQS,
`SELECT … wikibase:timeValue ?t ; wikibase:timePrecision ?p` for Q139377): Kingdom of Aksum inception
`-0400-01-01`, precision **7** (century) → stored as precision 9, `valid_lo/valid_hi` = one year, so timelines
and `valid_from/valid_to` filters assert a precision Wikidata does not. Fix: select
`psv:Pxxx/wikibase:timePrecision` (and `timeCalendarModel`) in the spec templates for every time value and
qualifier (`pqv:` for P580/P582/P585), or fetch dates through `wbgetentities` (the links fixture already
carries exact precision and revision ids); until then flag inferred precision < 9 as `precision_unknown` and
treat the bounds as unknown rather than one year.

**ATLAS-2 — non-calendar cells are invisible and reported as absent.** Best-available, map, coverage and
dossier only use cells with `period = [Jan 1, Jan 1 next year)` (`query.py:148`, `:317`). The WHR table is
entirely three-year windows (`atlas_whr.py`: `[Y-2, Y+1)`), Eurostat `earn_mw_cur` is semesters, the gMPI
survey windows are often two years. Result: the 11 `quality_of_life.whr_*` concepts and
`labour.minimum_wage.monthly.eur`/`.pps` never have a value in `/v1/atlas/map|countries|coverage`, and the map
lists Finland under `countries_without_value` with the note *"no declared source reports the cell (absent, not
zero)"* although WHR reports 7.736 (concept coverage 0 while the per-source line says 1 country). The ILO doc's
coverage table (`docs/dev/v3_a_ilo.md:138-139`: "monthly.eur 31 countries") is therefore not what the API
serves. `/v1/ask` inherits it (`nlqa/adapters.py` `_per_source` drops `year is None`). Fix: give each concept a
declared period rule — e.g. attach a non-calendar cell to the calendar year containing `period_end - 1 day`
with `flags.period_basis = "three_year_window" | "semester"` and its exact period, or at minimum exclude
countries with any non-calendar cell from `countries_without_value` and report `non_calendar_cells` per
concept; validate the catalog so that a concept whose every series is non-annual is flagged.

### Medium

**ATLAS-1 — cached DuckDB file is trusted on existence.** `build_snapshot_db` returns `final` if the file
exists (`query.py:114-118`) and `AtlasQuery` opens it (`:204`) without checking `meta.snapshot_sha256`,
`DB_FORMAT` or any digest. The default cache directory is the shared temp dir (`/tmp/reva-atlas-cache`,
`:89-90`, predictable names `atlas-<sha>-v3.duckdb`; the lock file is opened with `"w"` and follows symlinks).
Anyone able to write that directory changes every value the API serves while responses still cite the pinned,
verified Parquet sha256s; a half-written/stale file from an older format is also served. Repro:
`test_tampered_duckdb_cache_is_refused` (UPDATE cells → served). Fix: write a sidecar `<db>.sha256` (digest of
the finished file, computed once at build) and verify it (or at least `meta.snapshot_sha256 == snapshot.sha256`
and `meta.format == DB_FORMAT`) on open; default the cache to a private directory (`0700`, under the data dir)
and open the lock with `O_NOFOLLOW`.

**ATLAS-3 — dossier series points have no unit.** `dossier()` series items (`query.py:531-539`) and
`AtlasSeriesPoint` (`routes/atlas.py:108-113`) carry year/value/series/flags only; the concept unit
"national currency per month (named per cell)" cannot be applied by a client, and a series that switches
currency (ATS → EUR above) or source reads as one homogeneous line (a halving between 1970 and 2000). Fix: add
`unit` to every point (and `period_start/period_end` when ATLAS-2 admits non-calendar cells).

**ASK-3 — the projection flag can be cut from the sentence.** `_flags` sorts keys and truncates the rendered
text at 160 characters (`compose.py:158`); an ILOSTAT modelled-estimate cell's `projection` key sorts after its
long `notes` and disappears. Fix: render honesty-relevant flags first and never truncate them
(`estimate_kind`, `projection`, `missing`, `provider_flag`, `estimate_type`), truncate only the rest.

**ASK-4 — the rephrase faithfulness check is set-based.** `check_sentence` (`rephrase.py:151`) accepts any
rephrase whose numbers/names occur somewhere in the template or cited evidence: swapping two countries' values
("58,900 EUR … in France and 1,747.2 XOF … in Senegal") passes, and dropping "estimate_kind=projection" passes
(`KEEP_MARKERS`, `:39`, has no projection/estimate marker). LLM text then replaces the template as the answer.
Fix: require the rephrase to keep each (entity label, number, unit) triple in the same clause — simplest: have
the model return per-sentence slots (`{entity, value_ref}`) and render numbers server-side from evidence ids;
add `projection`, `estimate`, `modelled` and the period markers to `KEEP_MARKERS`.

**KG-1 — a retracted statement of a revision-less source can never come back.** In `_load_statements`
(`model.py:954-960`) the `ex` lookup (same key/origin/`source_revision IS NOT DISTINCT FROM`) classifies the
reload as `unchanged` before the retraction check, and `statement_version_identity UNIQUE NULLS NOT DISTINCT
(statement_key, origin, source_revision)` (`0009_kg.py:199`) would refuse a new row anyway. Sources without
revision ids (WDQS statements — see PROV-1 — UCDP/UNGA/CShapes dataset statements) therefore lose a statement
for good after one delivery omits it. Repro: `test_review_v3_kg_db.py` (load → retract → reload; current view
stays empty, report says `unchanged=1`). Fix: classify against the latest row first (`lt` retracted ⇒
`revision`), and make the identity `(statement_key, origin, source_revision, load_id)` or key reinstatements by
the load's raw sha256 when `source_revision` is NULL.

**PROV-1 — WDQS statements carry no revision id and no references.** `harvest.py:864-865` builds every WDQS
statement with `references=[]`, `source_revision=None` (the selector holds `wdqs_data_as_of`). The roadmap rule
"every statement keeps … revision id … and the statement's own references when present" is not met for the
bulk of the KG; `/v1/kg` and `/v1/ask` then cite statements without revision. Fix: select
`prov:wasDerivedFrom` references in the specs (or enrich via `wbgetentities`, as the links fixture does) and
record the entity `lastrevid` (from `wbgetentities`, already fetched for snapshots) as `source_revision`; say
in the provenance which statements lack them.

**LINKS-1 — truncated neighbourhoods are not reported.** `KgNeighbourhood.expand` drops a node's statements
beyond `per_node_limit=2000` / `max_rows_per_hop=50000` (rows taken with `LIMIT` and no `ORDER BY`) and
`graph_from_db` records them in `g.truncated_nodes` (`paths.py:762`), but `find_paths` sets
`res.truncated = trunc_t or trunc_s` only (`:479`), so `/v1/links/path` answers `truncated: false` and
"no_path_within_max_hops" over a cut graph (France is the object of far more than 2,000 traversable P17
statements). `links_for_country` ignores the language-genealogy truncation (`service.py:78`) and returns
`truncated` in data without an envelope warning. Violates invariant 17. Fix:
`res.truncated |= bool(graph.truncated_nodes)` with a reason listing them; add the API warning; order the
capped query deterministically.

**SEC-1 — request cost is bounded only by the statement timeout.** `/v1/kg/path` accepts `hub_limit` up to
100,000 (`routes/kg.py:372`): `_degrees` then counts up to 2×100,001 rows for each of up to 50,000 candidate
nodes per level (`kg/query.py:621`), up to 5 levels, two statements each — up to ~100 s of DB time per request
at the 10 s statement timeout. `/v1/links/path` loads up to 3 hops × 50,000 rows per side into Python, derives
links over all of them and runs up to 400 Yen candidates in the request thread; `via` multiplies it by up to 5
segments (`links/paths.py:728-762`, `:540`). `/v1/ask` allows 12 operations per question (12 path searches),
and `atlas_value country=ALL` with no year turns every best cell of every year into an evidence item (≈15k
items, `executor.py:482-493`). Fix: cap `hub_limit` at the default for the `read` scope, add a per-request
wall-clock budget (checked between levels/segments) and a per-actor concurrency limit on the path endpoints,
and cap `ALL` answers to one year (latest non-projection) with a count.

**ATLAS-4 — fiscal-year cells are stored as calendar years.** `parse_weo` writes `period_start = YYYY-01-01,
period_end = YYYY+1-01-01` for every WEO label (`atlas_imf.py:547-548`; months read at `:626`), also for fiscal-year economies; the
real window only survives in `flags.reporting_year` (fixture: IND NGDPD 2024 = 3,759,774,349,000 USD flagged
`"reporting_year":"April/March"`, i.e. FY2024/25). Best-available, maps and `/v1/ask` (`period_label`) present
it as calendar 2024, and a lower-ranked calendar-year source for the same concept would be treated as the same
cell. Fix: when `START_END_MONTHS_OF_REPORTING_YEAR` is not January/December, store the true period (e.g.
`[2024-04-01, 2025-04-01)`) and flag `period_basis: fiscal_year`, or keep the WEO label but add
`flags.period_basis = "fiscal_year_labelled_as_calendar"` and have the query layer and the composer render it.

### Low

* **ATLAS-5** — the atlas API defaults to "latest snapshot of any origin" (`routes/atlas.py:30`, `:398-400`).
  `create_atlas_snapshot(origin="test_fixture")` has no production guard, so one mistaken
  `python -m reva.atlas.snapshot --origin test_fixture` in production shadows the live snapshot with an empty
  fixture snapshot and every map says "absent". Fix: default `origin=live` when `REVA_ENV=production` and
  refuse fixture snapshots there.
* **ATLAS-6 (headline layer, in flight, partially reviewed)** — `_reported_cell` replaces the cell's unit by the
  concept unit (`query.py:640`, `"unit": unit or c["unit"]`): for `reva.labour.minimum_wage_monthly_lcu` the
  top-level unit becomes "national currency per month (named per cell)" and the actual currency is only in
  `reported.unit`; a headline map of it compares currencies. Derived cells (`:713-732`) compute values no source
  reports (`ratio`/`difference` across two providers) — labelled `method: derived`, but the roadmap rule "Atlas
  cells are what a source reports … no blending" needs an explicit amendment/ADR, and `flags: {}` drops the
  inputs' estimate flags at top level. Fix: keep `unit = c["unit"]` when the concept unit is "named per cell";
  propagate the union of input flags; document the derived rule in the roadmap.
* **KG-3** — `/v1/kg/*` never add the fixture warning (`routes/kg.py:232-239` has no `origin_warnings`) although
  `kg.statement_current` mixes `live` and `test_fixture` rows and path/neighbour queries read both
  (`kg/query.py:344`, `:520`); the links routes do warn. Fix: add `origin_warnings(origin_of(...))`, and filter
  `origin = 'live'` by default in production.
* **LINKS-2** — `iso3_to_qid` resolves duplicate P298 values by `rank`, then by the *string* order of QIDs
  (`paths.py:713-724`); `labels_for_iso3` keeps whichever row comes first (`nlqa/adapters.py:204-216`, no ORDER
  BY). Live Wikidata (non-deprecated P298 held by >1 item): PSE → Q219060 State of Palestine / Q407199
  Palestinian territories; YUG → Q83286 SFR Yugoslavia / Q838261 FR Yugoslavia; ATA → Q51 / Q10372207. The
  choice is silent. Fix: map ISO3 through the crosswalk's QIDs (`registry/entities/country_crosswalk.json`), and
  report ambiguous codes instead of picking.
* **LINKS-3** — `_origins` labels evidence without an origin as `live` (`routes/links.py:42`) and
  `Evidence.of_row` never sets one (`links/model.py:445-455`); atlas-row evidence of a fixture table would be
  reported live (not reachable from the API today: no atlas provider is wired). Fix: carry the table origin on
  `AtlasRowRef`, default to `unknown`.
* **ASK-5** — `AtlasCell.year` is `int(period_start[:4])`; atlas periods are now astronomical ISO text
  (`iso_date_sql`), so `-0043-01-01` gives year −4 and `compose` prints `-004` (`compose.py:119-127`, `:626`).
  Fix: parse the year with a regex `^(-?\d+)-`.
* **ASK-6** — `entity_lookup.text` is written by the planner and is not required to occur in the question; it
  enters the server restatement in quotes and drives entity resolution, so an injected question can make the
  server look up and answer about entities the user never named. Labelled as a search term, hence low. Fix: flag
  lookups whose text is not `in_question` and list them as `planner_added` in the answer.
* **SEC-2** — `GRANT SELECT ON ALL TABLES IN SCHEMA kg TO … reva_sim, reva_gate` (`0009_kg.py:406`) widens the
  gate's read set that `docs/threat_model.md` says is "nothing else"; harmless data, but the threat model and
  the grant disagree. Fix: drop gate/sim or update the threat model table.
* **DATA-1** — `labour.minimum_wage.monthly.local_currency` stitches ILOSTAT (value of a reference month: FRA note
  "Data reference period: July"), Eurostat semesters (dead, see ATLAS-2) and OECD annual averages (FRA 2023
  1,734.56 = mean of 1,709.28 × 4 and 1,747.20 × 8) into one best-available series without a flag; ILOSTAT's
  "local currency" is EUR for Norway (note `T30:391` "Currency: NOR - Euro (EUR)") and Bulgaria 2025, and
  Eurostat's `NAC` unit does not name the currency. Values are faithful; the concept label is not. Fix: add
  `reference_period` to the flags and the catalog definition, name the currency for Eurostat NAC cells.
* **KG-4** — `search` fetches `limit × 5` label rows then de-duplicates by entity (`kg/query.py:448-480`):
  entities with many matching labels/aliases can push others out with no truncation flag. Fix: `DISTINCT ON
  (qid)` in SQL, or return `truncated`.

## Spot checks (REVA value vs provider, live 2026-10-15)

REVA values come from the recorded provider responses (`tests/fixtures/source/atlas.*`, captured 2026-10-15)
parsed by the real connectors, or, where the fixture lacks the country, from the live-ingest coverage tables of
the agent reports (`docs/dev/v3_a_ilo.md`). The persisted throwaway databases were dropped by their owners, so
NOR HDI, CHN CO2 and SEN data could not be read back from a REVA store; they are marked accordingly.

| # | Cell | REVA | Provider (live) | Result | Provider URL |
|---|---|---|---|---|---|
| 1 | USA current account, % GDP, 2024 (IMF WEO Oct 2026) | −4.079572, `latest_actual 2025` | −4.079572 | match | https://api.imf.org/external/sdmx/2.1/data/IMF.RES,WEO/USA+FRA+IND.BCA_NGDPD+LUR+NGDPD.A?startPeriod=2024&endPeriod=2024 |
| 2 | USA current account, % GDP, 2026 | −3.868129, `estimate_kind=projection` | same, beyond LATEST_ACTUAL 2025 | match, flagged | same |
| 3 | FRA unemployment, 2024 (WEO LUR) | 7.441667 % | 7.441667 | match | same |
| 4 | IND GDP current USD, 2024 (scale) | 3,759,774,349,000 USD (full units; `display_scale 9`) | 3759774349000, SCALE=9 (= 3,759.774 bn) | match, units correct; period see ATLAS-4 | same |
| 5 | BRA current account, BOP 2023 | −26,981,475,122.16 USD | −26981475122.16497 | match | https://api.imf.org/external/sdmx/2.1/data/IMF.STA,BOP/BRA.NETCD_T.CAB.USD.A?startPeriod=2023&endPeriod=2023 |
| 6 | FRA HDI 2023 | 0.92 (rank 26) | 0.92, rank 26 | match | https://hdr.undp.org/sites/default/files/2025_HDR/HDR25_Composite_indices_complete_time_series.csv |
| 7 | USA HDI 2023 | 0.938 (rank 17) | 0.938, rank 17 | match | same |
| 8 | NOR HDI 2023 | not readable (row not in fixture, DB dropped) | 0.97, rank 2 | n/a (parser verified on rows 6-7 of the same file) | same |
| 9 | FIN WHR life evaluation, window 2022-2024 | 7.736, period [2022, 2025), "WHR 2025" | WHR 2025 file, Year 2024: 7.736 (DNK 7.521 = REVA 7.521) | match; window mapping confirmed | https://files.worldhappiness.report/WHR25_Data_Figure_2.1.xlsx |
| 10 | FRA CO2 per capita 2022 (OWID) | 4.456 t | 4.4555726 (grapher) | match (3 d.p. in the OWID CSV) | https://ourworldindata.org/grapher/co-emissions-per-capita.csv?country=FRA~DZA~CHN&csvType=filtered&useColumnShortNames=true |
| 11 | DZA CO2 per capita 2022 / CHN | 4.239 / not readable | 4.238998 / CHN 8.217776 | match / n/a | same |
| 12 | FRA minimum wage 2024 (ILOSTAT LCU) | 1,767 EUR per month | 1767 | match (reference month July, DATA-1) | https://rplumber.ilo.org/data/indicator/?id=EAR_INEE_CUR_NB_A&ref_area=FRA+SEN+CHN+DZA&format=.csv |
| 13 | SEN minimum wage 2024 / DZA 2024 / CHN 2024 | 64,175 XOF / 20,000 DZD / 1,930 CNY (regional) | 64175 / 20000 / 1930 | match | same |
| 14 | SEN unemployment, national, 2025 / NOR 2025 | 4.571 % / 4.456 % | 4.571 / 4.456 | match | https://rplumber.ilo.org/data/indicator/?id=UNE_DEAP_SEX_AGE_RT_A&ref_area=SEN+NOR&format=.csv |
| 15 | NOR mean monthly earnings 2025 (ILOSTAT "LCU") | 5,698.918 EUR per month | 5698.918, note T30:391 "Currency: NOR - Euro (EUR)" | match; concept label issue (DATA-1) | https://rplumber.ilo.org/data/indicator/?id=EAR_EMTA_SEX_CUR_NB_A&ref_area=NOR&format=.csv |
| 16 | FRA minimum wage 2025-S1/S2 (Eurostat EUR) | 1,802 / 1,802 | 1802 / 1802 | match (but invisible in best-available, ATLAS-2) | https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/earn_mw_cur?geo=FR&currency=EUR&time=2025-S1&time=2025-S2 |
| 17 | FRA minimum wage 2023 monthly (OECD NCU) | 1,734.56 EUR | SMIC 2023: 1,709.28 (Jan-Apr), 1,747.20 (May-Dec) → annual mean 1,734.56 | match (annual average, DATA-1) | official SMIC decrees; OECD fixture `DSD_EARNINGS@MW_CURP` |
| 18 | BRA official language (KG / links fixture) | Q155 —P37→ Q5146 Portuguese, preferred, rev 2556213157, refs kept | Q5146 preferred (+ Q3436689 Brazilian Sign Language, normal) | match | https://query.wikidata.org/sparql (`wd:Q155 p:P37`) |
| 19 | Aksum inception precision (KG-2) | WDQS path would store year precision 9 | `-0400-01-01`, precision 7 | **mismatch (false precision)** | https://query.wikidata.org/sparql (`wd:Q139377 psv:P571`) |
| 20 | WID FRA wealth share top 10 % 2022 | `0.5961000000000001` | the WID file itself contains `0.5961000000000001` | faithful (provider float artifact, kept verbatim) | fixture `atlas.wid` |

No scale, %/fraction or currency conversion error was found in the cells checked: IMF values are full units with
the display scale kept as a flag; WID shares are fractions and the catalog says "share (fraction of total, 0-1)";
OWID units are copied from OWID's metadata; ILOSTAT USD/PPP cells are labelled as ILOSTAT conversions. An
automated pass over all 325 (concept, series) pairs present in the fixtures found the cell unit consistent with
the concept unit everywhere except the currency-per-cell concepts (DATA-1/ASK-1) and IMF `LP`/`LE`, whose
"index (country group)" unit only occurs on unmapped aggregate rows.

## Verified as sound

* **KG storage**: deprecated rank refused at load and by CHECK; append-only + immutable triggers; rows sealed to
  their load's transaction; every load's raw sha256s must be verified artifacts; statements' `raw_sha256` must be
  one of the load's; property allow-list enforced set-wise; `reva_api/sim/gate` read-only, only `reva_worker`
  inserts (`test_kg_store.py::test_read_only_roles_cannot_write` passes); functions revoked from PUBLIC.
* **KG queries**: all user inputs bound as parameters (QID/PID regex-checked, LIKE patterns escaped, languages
  regex-checked); every result bounded with limits/truncation flags (except KG-4); the bidirectional BFS returns
  only minimal-length paths (shortest filter after meeting) and reports skipped hubs; causal wording only for
  P828/P1542/P1478/P1479/P1536/P1537 and attributed ("asserted by wikidata …, not established by REVA");
  Wikidata statements labelled `community_edited_not_verified`; Julian→Gregorian conversion and BCE year
  handling in `parse_time` checked by hand.
* **Atlas tables**: strict decimal values only, `missing` reason required for value-less rows, duplicates and
  two provider codes mapping to one country refused, unmapped geographies kept and listed, raw sha256 must be an
  archived acquisition of the same origin; snapshot manifests and pinned Parquet bytes re-hashed on build;
  fixture ingestion refused in production; DuckDB queries use bound parameters and integer-only interpolation.
* **Connectors**: IMF keeps full units and flags WEO projections from `LATEST_ACTUAL_ANNUAL_DATA` (fiscal-year
  labels resolved conservatively), aggregates/TWN/CWX/historical areas unmapped; UNDP carried-forward cells
  flagged, MPI windows kept as multi-year periods, names mapped by exact match only (ambiguous names never
  map); WHR 2011 rows (unknown window) excluded rather than given an invented period, licence status recorded;
  ILOSTAT modelled vs national estimates kept apart, currency taken from the row's own note; Eurostat "m" flag
  kept as "no national minimum wage", never zero; OECD unit multiplier kept in the unit, never applied.
* **Links**: every derived link requires evidence; shared-* links use the intersection of periods and flag
  undated overlap; blocked hubs never intermediate; causal edge wording reserved to the six causal properties by
  the taxonomy validator; Yen paths restricted to `max_hops` and bounded candidates.
* **Ask**: the planner never supplies QIDs (lookup refs only), concept/ISO3/property/link-kind allow-lists
  reject rather than substitute, instruction-like text is reported as ignored, the question travels as one JSON
  data field, audit rows hold only the question's sha256/length, LLM use is opt-in and budgeted before the call,
  and LLM-written strings are quoted only when they occur in the question.
