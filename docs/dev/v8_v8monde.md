# V8MONDE — Monde (space-time explorer) and Comparer

Owner: V8MONDE (agent index 4). Features `monde` (route `/v8/monde`, order 20, `g m`) and `comparer`
(route `/v8/comparer`, order 30, `g c`); backend `/v1/v8/series/*`.

## What was built

### Backend — `src/reva/api/routes/v8_series.py` + `src/reva/v8/series/`

Everything is read through the existing atlas query layer (`reva.atlas.query.AtlasQuery`, the per-snapshot DuckDB
file; `/tmp/reva-atlas-cache-*` is only opened by that layer, never touched). Read-only: no DB write, no migration.

| Endpoint | What it returns |
|---|---|
| `GET /v1/v8/series/catalog?q=&theme=` | Catalog concepts (fr/en labels, id, unit, theme, definition, source keys; accents folded) with the first source of each concept present in the snapshot (table, measure, filters, series), raw measure codes (e.g. `NY.GDP`) and tables. Concepts not loaded in the snapshot are listed last, `in_snapshot: false`. |
| `GET /v1/v8/series/table?table=` | Measures of one atlas table (label when a catalog concept reads it). |
| `GET /v1/v8/series/measure?table=&measure=&f=k=v` | Filter facets of a measure and the series the filters resolve to. |
| `GET /v1/v8/series/map?series=…` or `table=&measure=&f=` `&period=&precision=&level=adm0\|adm1&projections=exclude\|include\|only` | One series, one period, every place (the page bundle): meta (labels, unit, precisions, levels, licences, transform rules), periods at the series' precision with observed/projection counts, values (exact decimal strings, period, basis, flags, raw artifact sha, licence index), **every country without a value with its typed reason**, provider aggregates (WLD…), scale hint, counts, names. |
| `GET /v1/v8/series/place?geo=` | One place, every period (projections flagged, missing typed). |
| `GET /v1/v8/series/multi?s=…&g=FRA&g=DEU&population=true` | 1–6 series × 1–12 countries aligned by period, per-series transform rules, and on request the atlas headline population (`reva.population.total`) per country/year with the answering source per year. |
| `GET /v1/v8/series/vintages?series=imf.weo.X&g=` | WEO vintages (`atlas.vintage_weo.weo_imf`, then `weo_dbnomics`, read via `reva.evaluation.vintages.load_vintage_cells`): the list of vintages, "first publication" per period (earliest non-projection vintage by `available_at`), and each vintage as published. |
| `GET /v1/v8/series/places` | Country referential (crosswalk: ISO3, ISO2, labels, UN region). |

Model of a series: one atlas `<source>.<dataset>.<indicator>`; the indicator splits into a **measure** (leading
tokens without `=`, e.g. `UNE_2EAP_SEX_AGE_RT`, FAO `2511|5123`) and **filters** (`sex=SEX_F`, …); open filters take
the variant with the most "total" values. ~597 k series in 221 tables are indexed in memory per snapshot
(≈ 5–15 s once, ≈ 110 MB).

Periods: the year view is the atlas's own year attachment (calendar years, fiscal years, point-in-time cells), plus
calendar-year cells of a second provider geography of the same country (sub-national rows the attachment skips).
Sub-annual precisions come from the cell span: month (28–31 d), quarter (89–92), semester (181–184), day (≤ 1,
only for cells the year view did not attach). Codes: `2020`, `2020-S1`, `2020-Q3`, `2020-07`, `2020-07-14`.
Default precision: year when it holds ≥ half of the value cells, else the most populated sub-annual one. Default
period: the latest period whose observed count reaches half of the best-covered period (so WEO's current year, where
5 countries are observed and 186 are projections, is not the default).

Missing reasons in a slice (ADM0): `provider:<reason>` (the provider published an empty cell, e.g.
`not_reported`), `projection_hidden` (only a projection exists and projections are excluded), `other_granularity`
(monthly/quarterly cells overlap the year), `not_this_period` (the series has values for the place in other
periods), `not_in_series` (never reported). Every crosswalk country is either valued or missing, never both (tested).

Levels: ADM0 = countries. ADM1/NUTS = provider geographies that are sub-national codes (NUTS, ISO 3166-2, GeoNames
admin1) and not a country code. In the current live snapshot only `eurostat.nuts2_gapfill` carries NUTS codes (used
as territories); REVA serves **no sub-national geometry**, so ADM1 values are listed (ranking), never drawn, with
that warning (`no_subnational_geometry`).

Transform rules (server, `core.transform_rules`, French reasons when refused):
index base 100 — numeric, strictly positive, not a percentage; year-on-year — regular precision, positive, not a
percentage or rate; per capita — annual, ISO3 countries, an *extensive* unit (amounts, counts, mass, energy…) and
not already per capita/per 1000/per km² (unit, code or concept label: `NY.GDP.PCAP.CD` is refused).

Caching (a snapshot is immutable): series meta and periods (LRU 512), map slices and multi answers (LRU 512 each),
headline population per country, and the snapshot resolution reused 30 s per route (resolving it re-reads the
latest ref, re-verifies the manifest and the rights of every table: ~100 ms per request).

### UI — `ui/src/v8/features/monde/`

`index.ts` (feature + palette provider "Séries" → map links), `MondePage.tsx`, `MondeMap.tsx` (maplibre-gl, display
geometry from `/v1/locations` like V3 AtlasMap, `iso3OfFeatureKey` reused from V3 choropleth), `IndicatorPicker.tsx`
(catalog search popover: themes chips, concepts / measure codes / tables, table → measures drill-down; `/` opens it;
`FilterBar` selects per filter key), `PeriodBar.tsx` (slider at the series' precision, play/pause, ← → and Space,
projection-only periods ticked in violet), `PlacePanel.tsx`, `Scatter.tsx` (bivariate SVG), `scale.ts`, `url.ts`,
`api.ts` (zod client shared with Comparer), `places.ts`, `useAsync.ts`, `monde.css`.

- **Answer first**: indicator, period, definition (provider-declared), unit, series key, places with/without value,
  max/min country, provider world aggregate, projection counts, source chip with licence.
- **Choropleth**: classes quantile / linear / log / diverging (0 centred, red ↔ grey ↔ blue); `auto` follows the
  server hint (log when positive, right-skewed and ≥ 2 orders of magnitude; diverging with both signs; linear for few
  or symmetric values; quantile otherwise). Sequential blue ramp stepped separately for dark mode (small values recede
  into the dark surface). Legend with the reported values at class ends, then each missing reason with its count.
- **Missing** hatched with the token stripe (never a value colour); tooltip "manquant : <raison>".
- **Projections** hidden by default; "montrées à part" draws them with their class colour **plus a dot overlay**,
  violet italic `ᴾ` in the ranking, projection badge; they never define the classes when observations exist.
- **Tooltip**: name, exact value + unit, period (true period when not the slider's), basis, source series, licence
  (non-commercial flagged).
- **Click** (map or ranking row) → place panel: latest observed value, sparkline (projections dashed from their first
  period), every period with source chip, links Fiche (`v8Links.entity('country', ISO3)`) and Comparer.
- **Ranking**: shell `VirtualTable` (virtualised, sortable), optional "+ N sans valeur" rows with typed MissingValue.
- **Bivariate** (`mode=bivarie`, Y via `ytable/ymeasure/yf.*` or `yseries`): scatter of countries with both values in
  the same period, extreme points + selection labelled with ISO3, log toggles per axis (refused with a note when
  values ≤ 0), hollow dashed dots when a coordinate is a projection, hover shows both exact values; hovering a dot
  outlines the country on the side map; clicking selects it (shared `sel`). Note "une corrélation n’est pas une
  cause".
- **URL state**: `table, measure, f.<k>` (or `series`, or `table+indicator` from `v8Links.series`), `period, prec,
  level, mode, scale, proj, sel, ytable, ymeasure, yf.<k>, yseries, logx, logy`.

### UI — `ui/src/v8/features/comparer/`

`index.ts` (feature + palette command), `ComparerPage.tsx`, `Chart.tsx` (uPlot), `transforms.ts`, `parse.ts`,
`csv.ts`, `comparer.css`.

- Up to 6 countries × 6 indicators; one aligned uPlot panel per indicator (shared x range, **synchronised crosshair**
  `cursor.sync`, legend reading the value at the shared cursor, or the latest *observed* value). Observations solid;
  projections a separate dashed series per place (starting at the last observation); gaps for missing.
- Transforms: Valeurs / Base 100 / Variation sur un an / Par habitant — a button is disabled with the reason when no
  series allows it; a series that does not allow the chosen transform is shown untransformed with the reason.
  Base 100 uses the first period observed for every place. Per capita: "calcul REVA" badge and a note naming the
  population concept and the series that answered each year; a year without population is missing.
- Vintages (when the series has a vintage table — WEO today): "dernière version (instantané)" / "telle que publiée
  la première fois" / any vintage; each point keeps its vintage and dataset.
- Table view (period × place, missing typed, projections `ᴾ`, hover = reported value, computation, source).
- **CSV export** of exactly what is shown: indicator, series, place, period (+ start/end), shown value & unit,
  transform, reported value & unit, projection, missing reason, source, licence, non-commercial, raw artifact sha,
  vintage, computation, population value & series.
- Palette: command `comparer FRA DEU inflation` → `/v8/comparer?q=…`; the page splits ISO3 codes (any case unless a
  common word like "can"/"and") and country names (fr/en, up to three words, accents ignored) from the indicator
  words, resolves the words with the catalog (first loaded series) and rewrites the URL to `?g=FRA,DEU&s=imf.weo.…`.

## Tests

- `tests/unit/v8/test_v8_series.py` (11): indicator parsing, period codes, default precision/period, unit kinds and
  transform rules, scale hint; on a synthetic snapshot (no DB): catalog search with accent folding, filter facets and
  resolution, map slice values + every missing reason, projections hidden then flagged, monthly slice, ADM1 vs ADM0,
  place series, multi series and transform rules.
- `tests/integration/v8/test_v8_series_devdb.py` (db marker, read-only, skipped if the live query file is not
  materialised): slice equals a direct read of the same file, valued/missing disjoint, warm p95 < 300 ms.
- `ui/src/v8/features/monde/monde.test.tsx` (12) and `ui/src/v8/features/comparer/comparer.test.tsx` (12): scale,
  URL state (incl. `v8Links.series`), palette provider, page answer/legend/missing/tooltip/period/panel/projection
  toggle; transforms (index, YoY, per capita with missing population), palette parsing, CSV columns, page transforms
  disabled/untransformed, per-capita label, table view missing, palette query resolution.
- `ui/e2e/v8_monde.spec.ts`: Monde + Comparer against the e2e stack with V3 atlas fixtures (series discovered through
  the catalog), no horizontal scroll at 375 px, screenshots with `REVA_E2E_SHOTS`.

## Performance (measured)

Live dev snapshot (221 tables, 93.5 M cells, 597 k series), HTTP through my own API instance on a machine shared
with ~10 agents (numbers are upper bounds):

| Call | p50 | p95 |
|---|---|---|
| map slice, ~190–217 countries, new period each call (102 calls, 3 series) | 121 ms | 223 ms |
| catalog search (30) | 172 ms | 403 ms |
| place series (30) | 190 ms | 265 ms |
| multi 3 series × 4 countries (12) | 474 ms | 635 ms |

Those were measured **before** the 30 s snapshot-resolution reuse and the slice/multi caches; ~100 ms of every
call was the snapshot re-resolution, so warm calls now cost the query alone (in-process: map slice 15–25 ms, place
≈ 50 ms, catalog search ≈ 20–25 ms). Cold costs: first open of the live query file 60–180 s (its ATLAS-1 hash
verification; the API warms it at startup), index build 5–15 s once, first slice of a new series 0.4–5 s (series
meta + periods, then cached). Map slice payload 30–90 KB.

## Screenshots (described)

Taken against the live snapshot (light 1440 px, dark, projection map, Comparer) and through the e2e stack spec.
- Monde light, GDP per capita 2025, log classes: blue ramp from Burundi to Luxembourg, Greenland / Western Sahara /
  Afghanistan… hatched; legend "37 jamais rapporté par cette série", "28 pas de cellule pour cette période";
  France selected with its 1960–2025 sparkline and the per-year table with WB source chips.
- Monde dark: same map with the dark-stepped ramp; hover tooltip on Colombia "8 562 Current US$ · 2025 · source … ·
  licence CC-BY-4.0".
- Projection map: UN WPP medium variant 2050 ("montrées à part"): every country dotted, ranking in violet italic with
  `ᴾ`, period pill "projection", slider ticks violet.
- Comparer France/Germany/Italy: inflation (WEO, projections dashed after 2025, legend shows latest observed) and GDP
  per capita panels aligned on the same 1960–2030 axis; Par habitant disabled (one percent series, one already per
  capita), Millésime select present (WEO).
- Critique applied: map first opened too zoomed (fixed: fit to the inhabited world), auto scale picked "linear" for
  GDP per capita (fixed hint: log for right-skewed positive spans), a duplicate Fiche link (removed), legend said
  "observées" for projection-only maps (fixed), WEO default year had 5 observations (fixed default period).

## Honesty choices

- One series per map/panel: no cross-source blending (the catalog picks a concept's first loaded source and names it).
- Exact decimal strings end to end; client zod schema refuses JSON numbers for values.
- Missing never 0: typed reasons on map, legend, tooltip, ranking, panel, table and CSV (empty value + reason).
- Projections excluded by default, dotted/dashed/violet when shown, never in the default period, never shaping
  classes when observations exist, never joined to the observed line.
- Per capita only from the atlas headline population of the same country and year, labelled "calcul REVA" with both
  sources; YoY/index refused when meaningless (rates, ≤ 0, no common base).
- Scatter says correlation is not causation; ADM1 says there is no geometry rather than drawing points at random.

## Missing / not done and why

- No ADM1/NUTS geometry in REVA (`world.display_geometry` holds 177 Natural Earth 110 m countries only): regional
  values are listed, not mapped. Request: a sub-national display geometry ingest (NUTS 2021 / GeoBoundaries ADM1).
- Real sub-national atlas data is nearly absent from the pinned snapshot (only `eurostat.nuts2_gapfill`).
- Vintages only for IMF WEO series (the other vintage tables — RTDSM, SPF, ECB SPF, OECD STES, STEO — have no matching
  atlas series in the snapshot); they are not pinned by the atlas snapshot and are read from their latest table.
- Daily precision is supported but no daily series is in the current snapshot.
- Measure labels exist only where a catalog concept reads the series (~10 k of 597 k); others show the provider code.

## Requests to the lead

- No contract change, no new dependency.
- Fiche: links use `v8Links.entity('country', ISO3)` — V8FICHE should accept `kind=country`, `id=ISO3`.
- `ui/e2e/v8_monde.spec.ts` is a new file outside my folder (screenshots through the e2e stack as instructed).
- contracts/openapi.json not regenerated (no test requires it); the new routes are append-only when it is.
