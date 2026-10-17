# V4 V4CLIMATE — weather & climate in space and time (past → present → projected future)

Agent V4CLIMATE (2026-10-17). Scope: monthly temperature and precipitation per country (observed and projected),
global climate indicators (greenhouse gases, temperature anomalies, ENSO, sea ice, sea level), weather stations as
entities located in country and ADM1, and sourced extreme-event attribution findings as attributed causal claims.

Existing coverage was **extended, not duplicated**:

* `atlas_climate.py` (V3, A-GEO) has only the CCKP **1991-2020 climatological normals**. The new
  `atlas_cckp_series.py` reuses its parser (`atlas_climate.parse_response`, `null_series`), labels, geography scheme
  (`cckp`, KSV→XKX) and rights, and adds the *time series* the same API publishes.
* `atlas_noaa_hazards.py` (earthquakes, tsunamis, eruptions) and `atlas_hazards.py` (INFORM exposure scores) are
  geophysical / exposure layers: untouched. The attribution layer types its extremes as generic `disaster` events in
  the same event taxonomy.
* OWID (`atlas_owid.py`) carries CO2 *emissions* (Global Carbon Budget) and ERA5 *annual* country anomalies; V4CLIMATE
  adds *concentrations* (NOAA GML), monthly global anomalies from four independent groups and the monthly country
  series (CCKP CRU/ERA5), so nothing is loaded twice.

## Status

* Code, fixtures, tests: done and committed (`5c8bdf2`, `0d79123`, `48dbc0d`, `de23725`, `dd7d577`, catalog in a WIP
  snapshot `4150e4e`).
* Live load into the dev DB: LIVE_STATUS
* Tests: `tests/unit/test_atlas_cckp_series.py` (6), `test_atlas_climate_indices.py` (7), `test_atlas_ghcnm.py` (4),
  `test_atlas_attribution.py` (5) and `tests/integration/test_v4climate_ingest.py` (4: fixture ingest of the four
  sources into a throwaway DB, KG loads, T2 claims, idempotent re-runs) — all pass; ruff / ruff format / mypy clean on
  the V4CLIMATE modules; `python -m reva.kg.families check` OK.

## Sources (all open, no account)

| connector (`atlas.*`) | provider / file | licence (per value) | fetched per run |
|---|---|---|---|
| `cckp_series` 1.0.1 | World Bank CCKP API `cckpapi.worldbank.org/cckp/v1/...` — 36 `all_countries` requests: CRU TS 4.09 monthly+annual 1901-2024, ERA5 monthly+annual 1950-2024 (`tas,pr`), CMIP6 `ensemble_all` annual 2015-2100 × {SSP1-2.6, 2-4.5, 3-7.0, 5-8.5} × {median, p10, p90} + historical 1950-2014, monthly 20-yr climatologies 2020-39/40-59/60-79/80-99 per SSP + 1995-2014 reference | CC BY 4.0 (World Bank; CRU, ERA5, CMIP6 upstream) | ~31 MB (CRU monthly 11.5 MB, ERA5 monthly 7.1 MB, CMIP6 ~10 MB) |
| `climate_indices` 1.0.0 | NOAA GML `co2_mm_mlo.csv`, `co2/ch4/n2o_mm_gl.csv`; NASA GISTEMP v4 `GLB.Ts+dSST.csv`; Met Office HadCRUT 5.2.0.0 global monthly summary; Berkeley Earth `Land_and_Ocean_complete.txt`; NOAA CPC `oni.ascii.txt` + NOAA PSL `oni.data`; NSIDC Sea Ice Index G02135 v4.0 (24 monthly CSVs); NOAA STAR LSA `slr_sla_gbl_free_ref_90.csv`; US EPA sea-level indicator CSV (CSIRO + NOAA) | US Gov works (NOAA/NASA/EPA, cite); **OGL v3** (HadCRUT5); **CC BY-NC 4.0 — non-commercial** (Berkeley Earth); NSIDC: citation required | 35 requests, 0.75 MB on the wire (~1.5 MB raw) |
| `ghcnm` 1.0.1 | NOAA NCEI GHCN-M v4 `ghcnm.tavg.latest.qcf.tar.gz` (inventory + QCF TAVG), `readme.txt`, `ghcnm-countries.txt`; Natural Earth 1:10m `ne_10m_admin_1_states_provinces.zip` | US Gov work (NOAA NCEI, cite); Natural Earth public domain | 4 requests, ~60 MB |
| `attribution_cb` 1.0.2 | Carbon Brief attribution map: `index.html` (licence + revision) + `data/papers-download.csv` | **CC BY-NC-ND 4.0 — non-commercial, no derivatives** | 2 requests, 2.1 MB |

Raw archive of one full V4CLIMATE run ≈ 95 MB (cap 150 MB). Budget request `registry/atlas/budget_requests/v4climate.yaml`
merged as budgets-1.3.19 (worldbank.cckp 6→80, climate.global_indices 80, noaa.ghcnm 10, carbonbrief.attribution 6,
naturalearth.admin1_10m 2) and the digest re-pinned in `src/reva/autonomy/policy.py`. Source entries:
`registry/sources/{climate_global_indices,noaa_ghcnm,naturalearth_admin1_10m,carbonbrief_attribution}.yaml`.

## Atlas tables (month precision wherever the provider has it)

| table | content | rows (live) |
|---|---|---|
| `cckp_ts.cru_monthly` / `cru_annual` | CRU TS 4.09 `tas` (°C), `pr` (mm/month, mm/year), 246 CCKP geographies (BLM, BVT null on the 0.5° grid) | 726,144 / 60,512 |
| `cckp_ts.era5_monthly` / `era5_annual` | ERA5 `tas`, `pr` | 442,800 / 36,900 |
| `cckp_ts.cmip6_annual` | `tas|scenario=<ssp>|stat=<median|p10|p90>` 2015-2100, `scenario=historical` 1950-2014 | 603,684 |
| `cckp_ts.cmip6_monthly_clim` | `…|clim=2040-2059|month=07` (period = the 20 years) | 100,368 |
| `climind.noaa_gml` | `co2_mlo|average|deseasonalized`, `co2_gl|average|trend`, `ch4_gl|…`, `n2o_gl|…` (ppm/ppb) | 4,432 |
| `climind.gistemp` | `land_ocean_anomaly` monthly 1880-2026 + `|annual=J-D` | 1,907 |
| `climind.hadcrut5` | `anomaly` monthly 1850-2026-08, 95 % limits in flags | 2,120 |
| `climind.berkeley` | `anomaly|sea_ice=air|water` monthly 1850-2024 (NC licence) | 4,200 |
| `climind.enso_oni` | `oni_anomaly`, `nino34_sst_3month_mean` per 3-month season 1950-2026 | 1,840 |
| `climind.sea_ice` | `extent`, `area` (10^6 km²) NH/SH monthly 1978-2026 | 2,291 |
| `climind.sea_level` | `gmsl_anomaly|mission=<TOPEX/Poseidon…Sentinel-6MF>` (~10-day, mm), EPA `csiro_adjusted_sea_level` / `noaa_adjusted_sea_level` (annual, inches) | 1,822 |
| `ghcnm.tavg_qcf_monthly` | QCF TAVG (°C) of the selected stations | GHCN_ROWS |
| `attribution_cb.findings` | `FINDINGS|class=<…>` per country × single event year (derived count) | 371 |

Catalog: `registry/atlas/catalog/climate.yaml` (domain/theme `climate`): observed annual temperature and precipitation
(CRU first, ERA5 second, never blended), 8 projection concepts (tas/pr × 4 SSPs, ensemble median — separate concepts,
flagged projections), attribution findings count.

**Honesty rules applied.** Provider sentinels (`***`, `NaN`, `-9999`, `-99.9`, `-9.99` uncertainties) are never rows
(16 dropped in the indices run) — never zero. Every CMIP6 row carries `flags.estimate_kind` (`projection`, or
`model_simulation` for the historical experiment) **and** a truthy `flags.projection`, so `PROJECTION_SQL` keeps them out
of observation answers; `flags.projection_upstream` = scenario, model ensemble, percentile, product code, period.
Checks that block a run: CCKP annual `pr` = Σ monthly (±max(1 mm, 0.5 %)) and annual `tas` = day-weighted monthly mean
(±0.03 °C; observed max 0.011 °C); CCKP latest year = the year the product code names; ≥ 200 geographies; CPC ONI
values must equal PSL's centre-month listing (this *proves* the season convention: `DJF 1950` = Dec 1949–Feb 1950);
GHCN readme must still state the quoted definitions; Carbon Brief licence marker and revision date. STAR rows: day =
Jan 1 + round(fraction × days in year) from the provider's decimal year (formula in flags, decimal year in selector).
Definitions quoted verbatim from the archived files (GML unit sentences, GHCN readme, Berkeley header, PSL ONI
definition, STAR title); where a file does not state its unit/baseline (MLO unit, HadCRUT/GISTEMP baselines) the flag
says it comes from the provider documentation.

## Knowledge graph

| source key | entities | statements |
|---|---|---|
| `climate.global_indices` | 14 `climate_index` (`climind:co2_mlo`, `climind:oni`, `climind:sea_ice_extent_arctic`, …) | 69 (atlas series address, unit, verbatim definition, baseline, licence, provider file) |
| `noaa.ghcnm` | GHCN_ENTS `weather_station` (`ghcnm:<GHCN id>`, every inventory station) | GHCN_STS: `P625`, `P2044`, `P17` (FIPS prefix → ISO3 → QID), `ghcnm:fips_country_code`, `ghcnm:in_adm1` → `iso3166_2:<code>`, `ghcnm:valid_months` / `first_year` / `last_year`, `ghcnm:tavg_series` |
| `naturalearth.admin1_10m` | ADM1_ENTS `region_adm1` (`iso3166_2:<code>`) | ADM1_STS: `P17`, `naturalearth:adm1_code`, `naturalearth:type_en`, `naturalearth:wikidataid` |
| `carbonbrief.attribution` | 948: 1 `climate_driver` (`cbattr:human_caused_climate_change`), extremes `cbattr:extreme:<h>` (`extreme_event`), trends `cbattr:trend:<h>` (`climate_trend`) | 5,383: event type, period (`P585` / `P580`+`P582`, verbatim text), `cbattr:country` → country QID, region, one `cbattr:finding` per study (classification + citation, link, summary statement as qualifiers) |

Relation families (`registry/kg/relations_v4.d/v4climate.yaml`): `ghcnm:in_adm1` → `located_in`
(station_in_subdivision), `cbattr:country` → `located_in` (extreme_event_country); stations' `P17` is already
`located_in` (V4CORE core mapping). Literals are not mapped. No family is causal.

**ADM1 placement.** REVA point-in-polygon (shapely STRtree, `covered_by`) of each GHCN coordinate against the Natural
Earth admin-1 polygons; the subdivision is identified **only** by the ISO 3166-2 code Natural Earth publishes for the
polygon (never by name). Placeholder codes (`-99-X..`, `XK-X..`, 188 features) and codes Natural Earth uses for several
polygons (60 codes) are not used. 25,885 of 27,898 stations fall in a usable polygon (offshore/islands outside the
polygons get none). When the FIPS-prefix country and the subdivision's country disagree (179 stations, e.g. RQ Puerto
Rico vs `US-PR`, SU Sudan stations now in South Sudan `SS-*`, FG/GP French overseas vs `FR-*`, and GHCN-Daily
`ACW00011604` "SAVE" coded AC=Antigua but at 57.77 N 11.87 E in Sweden) the station gets **no** ADM1 link and its `P17`
carries `coordinates_fall_in`. ADM1 entities keep Natural Earth's `iso_a2` beside the ISO prefix when its de facto view
differs (`UA-43` drawn under RU). Ids follow V4CORE's place convention (`iso3166_2:<code>`, `reva.kg.spacetime`), so
V4LINK's Wikidata P300 bridges attach to them.

**Station selection for the atlas series** (size bound, not a quality judgement): per GHCN country code the 3 stations
with the most valid QCF monthly values (ties by id) + the 50 longest records worldwide → 646 stations, ~710 k monthly
values. Every station keeps its KG entity and its valid-months count.

Example paths (ids from the live dev DB):

* `ghcnm:ACW00011604` (SAVE) —P17 (FIPS AC → ATG, `coordinates_fall_in: SE-…`)→ Antigua and Barbuda: the GHCN code
  is kept as GHCN asserts it, the contradiction is shown, no ADM1 edge is drawn.
* GHCN_EXAMPLE
* `climind:co2_mlo` —`climind:atlas_series`→ `climind.noaa_gml.co2_mlo|average @ provider_geo=MLO` (1958-03: 315.71
  ppm; flagged interpolated months quote GML's note); `climind:oni` definition "represents 3 month running mean of
  ERSST.v6 SST anomalies in the Niño 3.4 region (5N-5oS, 120o-170oW)".
* `cbattr:human_caused_climate_change` —contributing_factor (promotes, T2, DOI 10.1029/2011GL050422, Carbon Brief
  classification "More severe or more likely to occur")→ `cbattr:extreme:36aa2255eb39a121` "Russia summer heatwave,
  2010" —`cbattr:country`→ Q159 (Russia, `located_in`), type Heat, P585 2010.

## Causal claims (attributed, never REVA's)

`reva.causal.load_dataset_claims`, dataset `carbonbrief.attribution_studies`, tier **T2** (scholarly reference with its
DOI stored verbatim; Carbon Brief is the compiler, the study is the asserting reference; mapping documented in
`registry/causal/relations_v4climate.yaml`):

| Carbon Brief classification (verbatim) | claim | live count |
|---|---|---|
| More severe or more likely to occur | `contributing_factor`, promotes | 598 |
| Decrease, less severe or less likely to occur | `contributing_factor`, inhibits (family prevention) | 72 |
| No discernible human influence | `contributing_factor`, stance **denies** | 69 |
| Insufficient data/inconclusive | no claim (KG finding only) | 67 rows |
| any class, no DOI in citation or link | no claim (T2 needs DOI/ISBN) | 161 rows (119 rapid studies, mostly World Weather Attribution web pages) |

739 claims loaded (0 unknown endpoints). The study's summary statement is the claim `mechanism` (verbatim). DOIs from
the citation text (649) or, failing that, from a DOI link (90; `scope.doi_basis = link` with a note: a BAMS
"Explaining Extreme Events" link names the whole supplement, so several studies share that DOI and count once as an
upstream). 1.0.2 strips publisher URL tails (`/abstract`) from link DOIs (8 claims): re-ingest pending (see commands).
Event typing: `registry/events/taxonomy.yaml` dataset kind `cbattr:extreme:` → `disaster`; "Impact" records excluded
(impacts, not weather events); trends never events. Nothing is extracted from IPCC or other prose; no claim is made by
joining datasets (a GHCN anomaly near an event is never evidence).

## Missing, and why

* **CCKP subnational (ADM1) series**: CCKP computes them (its metadata names "World Bank shapefiles for national (ADM0)
  and subnational (ADM1) units"), but the API does not list its ADM1 geocodes (`all_subnationals` and ~30 tried
  patterns return empty data) and the portal pages that document them (`/download-data`, `/guidance-note`) answer
  HTTP 403 to non-browser clients. Not worked around → owner decision (ask the World Bank for the geocode list, or
  allow a browser fetch). Watersheds work (`all_watersheds`) but are not ADM1.
* **Berkeley Earth country/regional files**: ~200 files × ~320 kB = ~65 MB, CC BY-NC, and the file names are country
  *names* (mapping would need a reviewed name→ISO3 alias table, i.e. a name match). Only the global series is loaded;
  CCKP CRU/ERA5 cover country monthly series. Owner decision: accept NC + a reviewed alias file, then load on a bigger
  disk.
* **Copernicus CDS / ERA5 direct**: needs a CDS account → not used (ERA5 enters only through CCKP aggregates).
* **NASA PO.DAAC GMSL**: Earthdata login → not used (NOAA STAR + EPA/CSIRO used instead); `www.cmar.csiro.au` refused
  the connection on 2026-10-17.
* **NASA POWER**: not needed — every country code with GHCN stations has a selected station; POWER (model-based point
  data) is not an observation and would only be added for station-less capitals.
* **Capital-city stations**: GHCN names are free text (matching them to capitals would be a name match). A geometric
  "nearest station to the capital" needs capital coordinates from an archived source (GeoNames PPLC in
  `atlas_cities`); left for a follow-up — selection today is by record length.
* **GHCN precipitation**: GHCN-M v4 publishes only TAVG; precipitation per station would need GHCN-Daily/GSOM
  (`ncei.noaa.gov/access/services/data/v1?dataset=global-summary-of-the-month`), not loaded (size/time).
* **Event attribution beyond Carbon Brief**: World Weather Attribution publishes no structured dataset (web pages
  only); its studies enter through Carbon Brief rows, as claims only when a DOI exists.
* **Links to compounds** (CO2/CH4/N2O series ↔ V4CHEM compounds): would need an identifier the GML files publish;
  they publish none → not linked (V4LINK could bridge through Wikidata if it loads the series items).
* GISTEMP host `data.giss.nasa.gov` resets some TLS connections (3/5 attempts succeeded on 2026-10-17): a failed run
  is safe to re-run.

## Owner decisions

1. **Licences**: Carbon Brief CC BY-NC-ND 4.0 (REVA restructures rows into KG statements, claims and counts — the ND
   clause may forbid redistributing those adaptations; every value flagged `non_commercial`/`no_derivatives`);
   Berkeley Earth CC BY-NC 4.0 (flagged). HadCRUT5 OGL v3 requires the acknowledgement sentence (in `rights`).
2. CCKP ADM1 geocodes (above).
3. Whether to keep all 27.9 k stations as KG entities (current) or only the selected 646.
4. Review of the ADM1 point-in-polygon rule (derived `located_in`, labelled `derived: point_in_polygon`).

## Commands (bigger machine / later run)

```bash
df -h /                                                    # >= 5 GB free first
flock /tmp/reva-devdb.lock uv run reva seed                # registers connector versions (code digests)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.climate_indices
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.attribution_cb      # 1.0.2 (DOI tail fix)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.cckp_series
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ghcnm
# then (lead): reva atlas snapshot --skip-unchanged; reva links materialise; reva causal refresh --origin live
# offline checks
uv run pytest -q tests/unit/test_atlas_cckp_series.py tests/unit/test_atlas_climate_indices.py \
  tests/unit/test_atlas_ghcnm.py tests/unit/test_atlas_attribution.py tests/integration/test_v4climate_ingest.py
```

Larger loads for a bigger disk: all GHCN stations' series (drop the selection: ~170 MB parquet), GHCN-Daily/GSOM
precipitation, Berkeley Earth country files, CCKP extra variables (`tasmax`, `tasmin`, extreme indices `txx`, `rx1day`,
`cdd`, `hi35` …) per SSP, CMIP6 per-model series (`ensemble_all` → individual models).
