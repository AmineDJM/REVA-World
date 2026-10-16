# V3 A-GEO — geography for every country (coverage report)

Agent A-GEO, 2026-10-15. Implemented, tested offline, and ingested once live through the single atlas ingest
entry point (`reva.atlas.ingest.run_atlas_ingest`, budget-metered, policy budgets-1.3.2) into a throwaway
database that was dropped after the run (the dev database `reva` was never touched).

## What was built

| atlas source (connector) | module | atlas tables | requests / run |
|---|---|---|---:|
| `atlas.naturalearth_10m` (source key `naturalearth.admin0_10m`, public domain) | `src/reva/adapters/connectors/atlas_naturalearth10m.py` | `naturalearth.admin0_10m` (per country), `naturalearth.admin0_10m_borders` (per country pair, both directions), `naturalearth.admin0_10m_disputed` (per disputed area) | 6 (16.4 MB) |
| `atlas.geonames_countryinfo` (`geonames.countryinfo`, CC BY 4.0) | `atlas_geonames.py` | `geonames.countryinfo` | 1 (32 kB) |
| `atlas.cckp_climatology` (`worldbank.cckp`, CC BY 4.0) | `atlas_climate.py` | `cckp.cru_ts4_09_clim_1991_2020`, `cckp.era5_clim_1991_2020` | 3 (0.22 MB) |
| `atlas.inform_risk_hazards` (`jrc.inform_risk`; licence to confirm, see below) | `atlas_hazards.py` | `inform.risk_2026_natural_hazards` | 2 (0.21 MB) |

Shared code: `atlas_geodesy.py` (pure-numpy WGS 84 area and Vincenty lengths) and `atlas_geo_common.py`.
Catalog `registry/atlas/catalog/geography.yaml` (27 provider-declared concepts); sources
`registry/sources/{naturalearth_10m,geonames,worldbank_cckp,jrc_inform}.yaml`; budget request
`registry/atlas/budget_requests/a_geo.yaml` (ATLASCORE has merged it); geo alias
`registry/atlas/geo_aliases/a_geo.yaml` (CCKP `KSV` → `XKX`, because the World Bank API resolves `KSV` to
`XKX` "Kosovo"). The Natural Earth 1:110m display connector (`naturalearth.py`) is unchanged.

All four modules follow the atlas contract: `atlas_source()` → `AtlasSourceSpec`, and `async
atlas_ingest(run)` fetches only through `run.get`, archives with `run.archive` before deriving anything, and
registers with `run.register`. None of them builds an HTTP client.

Run: `uv run python -m reva.atlas.ingest atlas.naturalearth_10m` (and likewise for `atlas.geonames_countryinfo`,
`atlas.cckp_climatology`, `atlas.inform_risk_hazards`); all four also run in `--fixture` mode.
`--dataset admin0_10m|admin0_10m_borders|admin0_10m_disputed` fetches only the layers that dataset is computed
from (country table: map units + the two country layers; borders: map units + the two boundary-line layers;
disputed: the disputed-areas layer) and registers the same rows a full run does.
Natural Earth's four large layers (16 MB) are not committed: fixture mode replays a **derived excerpt** of
them (real release-5.1.1 features of the closed country groups Ireland+UK, Hispaniola, Cyprus with its
buffer zone/base areas/Northern Cyprus, and Saint-Martin; ~300 KB), cut reproducibly by
`tests/fixtures/source/atlas.naturalearth_10m/cut_excerpt.py` (its `--verify` checks that every value
computed from the excerpt equals the whole-world value). Each excerpt ZIP carries a `REVA_EXCERPT.json`
member and a `derived` block in the manifest; the connector accepts such a layer only from a `test_fixture`
acquisition and flags every row computed from it `ne_excerpt`. The two small layers are full captures.
Unit tests also use a synthetic shapefile world (`synthetic_world.py`) for closed-form checks.

## Methods and limits

* **No shapely/pyproj.** `pyproject.toml` is untouched and `uv lock` was not run.
  * Area is the WGS 84 ellipsoidal area, computed with the authalic-latitude equal-area transform: Green's
    theorem in (λ, β), Simpson's rule on each edge. Edges are taken as straight in lon/lat, as in Natural
    Earth's data model.
  * The area code reproduces closed forms: the whole ellipsoid gives 510,065,621.72 km² (published value
    510,065,621.7); lon/lat boxes match to 1e-12 relative; polar caps split at ±180° are handled.
  * Lengths use Vincenty's inverse formula. It reproduces the Flinders Peak–Buninyong test line (54,972.271 m)
    to under 1 mm.
* **Topology.** In the 1:10m map-unit layer, each of the 477,728 distinct edges is used by at most 2 polygons.
  If a layer breaks that rule, the computation refuses it.
  * A shared edge is a land border; an unshared edge is coastline.
  * Each shared edge is typed by matching it to Natural Earth's boundary-line layers: international, disputed,
    line of control, indefinite, lease or overlay. Edges that NE marks as "water indicator" are river or lake
    boundaries.
  * 99.65 % of the shared length gets a type. The rest is kept and labelled `untyped`.
* **Coastline depends on the map scale.** The indicator is named `coastline_length_km|scale=ne_1:10m` and
  flagged `scale_dependent`. For example, Canada's coastline is 137,274 km at 1:10m against 202,080 km in the
  CIA World Factbook.
  * Caspian shores are reported separately (`caspian_shore_km|scale=ne_1:10m`). They are identified with a
    fixed box that contains no ocean.
  * Antimeridian and pole cut edges (2,311 km) are excluded.
* **Landlocked** means no 1:10m coastline. Countries whose only shore is the Caspian count as landlocked, the
  usual convention. The result matches the conventional list exactly, including Kosovo and South Sudan.
  Doubly landlocked comes out as exactly Liechtenstein and Uzbekistan.
* **Exclaves and enclaves.** Polygons joined by shared internal edges, or across the antimeridian, form one
  component.
  * Any component other than the largest that touches foreign land is an exclave, labelled `enclosed` or
    `coastal`.
  * A country or component with no coast and exactly one land neighbour is an enclave.
  * Small exclaves such as Baarle, Büsingen and Campione do not exist at 1:10m.
* **Political view.** Natural Earth's *default* layers show a de facto view. In release 5.1.1 Crimea is
  administered by Russia, Western Sahara is split along the berm, and Kashmir is split by control.
  * Every row carries `ne_view = default (de facto)`.
  * The disputed-areas table (99 areas) quotes NE's note verbatim, e.g. "Admin. by Russia; Claimed by
    Ukraine".
  * Features that are not crosswalk countries keep their NE code with `iso3 = null`: Somaliland, Northern
    Cyprus, the UN buffer zone, Akrotiri and Dhekelia, Siachen, the Southern Patagonian Ice Field, Bir Tawil,
    Guantánamo, Spratly, Scarborough, Bajo Nuevo and Serranilla. Taiwan was also unmapped in the live run;
    the crosswalk added `TWN` after the run, and a re-run maps it.
  * Borders with these features stay borders with that feature (for example Ethiopia–Somaliland). They are
    never reassigned to a claimant.
* **Map units → countries** follows a fixed order: `ISO_A3`, then `ADM0_A3`, then `ISO_A3_EH`, then the
  reviewed alias `KOS → XKX`.
  * This gives France's overseas regions, Svalbard, Caribbean Netherlands, Cocos/Christmas Islands and Tokelau
    their own ISO entities.
  * Some units are assigned through `ISO_A3_EH` even though NE types them Indeterminate, Lease, Disputed or
    Overlay: Brazilian Island → BRA, Baikonur → KAZ, the UNDOF zone → SYR. These are flagged on the country's
    rows.
* **Periods.**
  * Natural Earth and GeoNames do not date their figures, so those rows use a one-day retrieval snapshot,
    flagged `period_basis`.
  * Climate normals cover `[1991-01-01, 2021-01-01)`.
  * INFORM rows use the edition year (2026), flagged as such.
* **Elevation was skipped.** I found no small open per-country DEM summary; computing one would mean
  downloading global DEM tiles (GB scale), which exceeds the disk budget. Highest points are available in
  Wikidata (P610/P2044), which belongs to the KG domain agents.
* **Hazards** cover only INFORM Risk 2026's natural-hazard dimension: the composite plus earthquake, river
  flood, coastal flood, tropical cyclone, drought, tsunami and epidemic. These are modelled 0–10 indices,
  flagged "not an observation".
  * INFORM says it is "open-source" and "open, also for integration in other applications", but no
    dataset-specific licence was found. The EC reuse policy is recorded **pending maintainer confirmation**.
* **Climate.** The tables hold CCKP national aggregates of two products, kept per source and never blended:
  CRU TS 4.09 (annual tas/pr, monthly tas/tasmax/tasmin/pr) and ERA5 (annual tas/pr).
  * The run is refused unless the 12 monthly precipitation totals add up to CCKP's annual value, within
    max(1 mm, 0.5 %).
  * CRU lists Saint-Barthélemy and Bouvet Island with null series (sub-grid islands). They are omitted and
    named in the coverage note.
  * The "1991-07" key is CCKP's own label for the climatology and is kept in the selector.

## Gaps and alternatives

| gap | where | alternative |
|---|---|---|
| CCKP has no aggregate for ATA, ESH, FLK, SGS, CHI; null for BLM, BVT | climate | none open at country level found; the CRU/ERA5 grids would need REVA's own zonal statistics (not done: that would be a REVA estimate, not a source value) |
| INFORM covers 191 countries (no small territories) | hazards | WorldRiskIndex (CC BY) could fill some; not ingested |
| Elevation | — | Wikidata highest point (KG), or zonal stats on GMTED2010 (heavy) |
| Official (UN Demographic Yearbook) areas | area check | the DYB table is published as PDF/xls, not a stable API; WB (FAO-sourced) and GeoNames used instead |
| KG statements need ISO3→QID | KG | `registry/entities/country_qids.json` (WDHARVEST) does not exist yet: `atlas_ingest` reports the KG load as *blocked* (nothing is guessed). The live run used a one-off Wikidata P298 lookup only inside the throwaway DB. |

## Live run (2026-10-15, throwaway database `reva_ageo_live_757a542f`, dropped afterwards)

The crosswalk still had 250 entities without Taiwan, so `TWN` appears among the unmapped geographies
below; the crosswalk now lists `TWN`.

| source | status | requests / bytes (metered) | atlas tables (rows) |
|---|---|---|---|
| atlas.naturalearth_10m | completed | 6 / 16,351,353 | admin0_10m (1,647), admin0_10m_borders (676), admin0_10m_disputed (99) |
| atlas.geonames_countryinfo | completed | 1 / 31,690 | countryinfo (3,420) |
| atlas.cckp_climatology | completed | 3 / 218,002 | cru_ts4_09_clim_1991_2020 (12,200), era5_clim_1991_2020 (492) |
| atlas.inform_risk_hazards | completed | 2 / 207,800 | risk_2026_natural_hazards (1,528) |

Natural Earth release 5.1.1. Diagnostics: 4,292 polygons, 548,514 edges, 70,786 shared; 232,600 km of shared
boundary, 99.65 % typed; the map-unit and countries layers agree on area to 1e-14.

**KG.** The live KG load did not run. WDQS was throttled machine-wide (HTTP 429, Retry-After up to 1,000 s,
caused by other processes), and my two one-off ISO3→QID / P47 queries timed out after 30 minutes. The
KG path (`load_adjacency_kg`, `load_neighbours_kg` → KGCORE `load_batch`) is covered offline by the DB
integration test:
* P47 statements are written in both directions, with the border length as a decimal-string quantity, the
  boundary classes, a disputed flag, and a reference to the NE raw sha256;
* reloading the same batch inserts nothing;
* without QIDs the load is reported as blocked.

The live load will work once WDHARVEST publishes `registry/entities/country_qids.json`. Expected size: 321
mapped pairs → up to 642 NE statements, and 323 GeoNames pairs → up to 646 statements.

**Adjacency vs Wikidata P47:** not available in this run (same WDQS throttling). The comparison code is in
place and needs only the P47 pairs.

### Summary

* NE 1:10m entities with a computed area: 249 crosswalk countries + 14 unmapped NE features.
* vs WB land area (AG.LND.TOTL.K2): 215 countries compared; median |difference| 2.17%; > 5 %: 58; > 10 %: 32.
* vs WB surface area (AG.SRF.TOTL.K2): 215 countries compared; median |difference| 1.43%; > 5 %: 56; > 10 %: 34.
* vs GeoNames area: 248 countries compared; median |difference| 1.76%; > 5 %: 71; > 10 %: 45.
* **Flagged** (no reference within 5 %, or no reference at all): 60 countries (table below).

### Area discrepancies (NE computed area vs references; flagged = no reference within ±5 %)

| ISO3 | NE area km² | boundary lakes km² | WB land km² | Δ | WB surface km² | Δ | GeoNames km² | Δ | explanation (rule-based) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| UMI | 28 |  | — | — | — | — | 0 | — | includes NE-administered disputed area(s): Wake Atoll (4 km²); Navassa I. (5 km²); small/insular: 1:10m generalisation |
| VAT | 0.01 |  | — | — | — | — | 0 | -97.2% | small/insular: 1:10m generalisation |
| MCO | 19 |  | 2 | +804.0% | 75 | -74.8% | 2 | +866.1% | small/insular: 1:10m generalisation; WB surface area includes waters (surface/land = 35.94) |
| ESH | 90,496 |  | — | — | — | — | 266,000 | -66.0% | part of the territory attached elsewhere by NE (de facto): W. Sahara 177,850 km² attached to MAR |
| MDV | 109 |  | 298 | -63.5% | 300 | -63.8% | 300 | -63.8% | small/insular: 1:10m generalisation |
| TKL | 4.23 |  | — | — | — | — | 10 | -57.7% | small/insular: 1:10m generalisation |
| WLF | 139 |  | — | — | — | — | 274 | -49.1% | small/insular: 1:10m generalisation |
| GIB | 3.69 |  | 10 | -63.1% | 10 | -63.1% | 6 | -43.2% | includes NE-administered disputed area(s): Gibraltar (4 km²); small/insular: 1:10m generalisation |
| CYP | 5,395 |  | 9,240 | -41.6% | 9,250 | -41.7% | 9,250 | -41.7% | excludes adjacent NE feature(s) CNM, ESB, WSB |
| ALA | 942 |  | — | — | — | — | 1,580 | -40.4% | small/insular: 1:10m generalisation |
| NRU | 29 |  | 21 | +37.0% | 21 | +37.0% | 21 | +37.0% | small/insular: 1:10m generalisation |
| MAR | 591,744 |  | 446,300 | +32.6% | 446,550 | +32.5% | 446,550 | +32.5% | includes NE-administered disputed area(s): W. Sahara (177,850 km²) |
| MAF | 68 |  | 53 | +28.8% | 53 | +28.8% | 53 | +28.8% | small/insular: 1:10m generalisation |
| CXR | 99 |  | — | — | — | — | 135 | -26.9% | small/insular: 1:10m generalisation |
| SGP | 511 |  | 724 | -29.4% | 734 | -30.5% | 693 | -26.3% | small/insular: 1:10m generalisation |
| SOM | 471,815 |  | 627,340 | -24.8% | 637,657 | -26.0% | 637,657 | -26.0% | excludes adjacent NE feature(s) SOL |
| MNP | 579 |  | 457 | +26.8% | 457 | +26.8% | 477 | +21.4% | small/insular: 1:10m generalisation |
| COK | 191 |  | — | — | — | — | 240 | -20.4% | small/insular: 1:10m generalisation |
| NFK | 41 |  | — | — | — | — | 35 | +18.9% | small/insular: 1:10m generalisation |
| IOT | 49 |  | — | — | — | — | 60 | -18.2% | includes NE-administered disputed area(s): Br. Indian Ocean Ter. (7 km²); small/insular: 1:10m generalisation |
| KIR | 950 |  | 811 | +17.1% | 811 | +17.1% | 811 | +17.1% | small/insular: 1:10m generalisation |
| CYM | 309 |  | 240 | +28.8% | 264 | +17.1% | 262 | +18.0% | small/insular: 1:10m generalisation |
| BLM | 24 |  | — | — | — | — | 21 | +16.3% | small/insular: 1:10m generalisation |
| TON | 603 |  | 720 | -16.2% | 749 | -19.5% | 748 | -19.4% | small/insular: 1:10m generalisation |
| BMU | 62 |  | 54 | +15.4% | 4,290 | -98.5% | 53 | +17.6% | small/insular: 1:10m generalisation; WB surface area includes waters (surface/land = 79.44) |
| NIU | 221 |  | — | — | — | — | 260 | -15.2% | small/insular: 1:10m generalisation |
| AIA | 87 |  | — | — | — | — | 102 | -14.3% | small/insular: 1:10m generalisation |
| LIE | 137 |  | 160 | -14.2% | 160 | -14.2% | 160 | -14.2% | small/insular: 1:10m generalisation |
| YEM | 453,075 |  | 527,970 | -14.2% | 527,968 | -14.2% | 527,970 | -14.2% | unexplained — needs review |
| BVT | 55 |  | — | — | — | — | 49 | +12.7% | small/insular: 1:10m generalisation |
| ATA | 12,358,218 |  | — | — | — | — | 14,000,000 | -11.7% | unexplained — needs review |
| TUV | 23 |  | 26 | -11.5% | 26 | -11.5% | 26 | -11.5% | small/insular: 1:10m generalisation |
| SRB | 77,629 |  | 87,589 | -11.4% | 88,490 | -12.3% | 88,361 | -12.1% | unexplained — needs review |
| SXM | 23 |  | 34 | -31.3% | 34 | -31.3% | 21 | +11.2% | small/insular: 1:10m generalisation |
| SHN | 367 |  | — | — | — | — | 410 | -10.6% | small/insular: 1:10m generalisation |
| NLD | 37,102 |  | 33,670 | +10.2% | 41,540 | -10.7% | 41,526 | -10.7% | NE map units carve out BES (separate crosswalk entities); WB surface area includes waters (surface/land = 1.23) |
| COM | 1,672 |  | 1,861 | -10.1% | 1,861 | -10.1% | 2,170 | -22.9% | small/insular: 1:10m generalisation |
| BES | 295 |  | — | — | — | — | 328 | -10.0% | small/insular: 1:10m generalisation |
| MAC | 30 |  | 33 | -9.7% | 33 | -9.7% | 254 | -88.2% | small/insular: 1:10m generalisation |
| SUR | 145,123 |  | 160,508 | -9.6% | 163,820 | -11.4% | 163,270 | -11.1% | unexplained — needs review |
| FSM | 634 |  | 701 | -9.6% | 701 | -9.6% | 702 | -9.7% | small/insular: 1:10m generalisation |
| PCN | 43 |  | — | — | — | — | 47 | -9.4% | small/insular: 1:10m generalisation |
| ASM | 180 |  | 199 | -9.4% | 199 | -9.4% | 199 | -9.4% | includes NE-administered disputed area(s): Swains Island (2 km²); small/insular: 1:10m generalisation |
| BHS | 12,604 |  | 10,010 | +25.9% | 13,878 | -9.2% | 13,940 | -9.6% | WB surface area includes waters (surface/land = 1.39) |
| GNB | 32,829 |  | 28,120 | +16.7% | 36,125 | -9.1% | 36,120 | -9.1% | WB surface area includes waters (surface/land = 1.28) |
| MHL | 165 |  | 181 | -8.9% | 181 | -8.9% | 181 | -9.1% | small/insular: 1:10m generalisation |
| GUF | 83,184 |  | — | — | — | — | 91,000 | -8.6% | unexplained — needs review |
| PAK | 872,939 |  | 770,880 | +13.2% | 796,100 | +9.7% | 803,940 | +8.6% | excludes adjacent NE feature(s) KAS; includes NE-administered disputed area(s): Gilgit-Baltistan (65,244 km²); Azad Kashmir (11,963 km²) |
| LBY | 1,623,759 |  | 1,759,540 | -7.7% | 1,759,540 | -7.7% | 1,759,540 | -7.7% | unexplained — needs review |
| PLW | 492 |  | 459 | +7.2% | 459 | +7.2% | 458 | +7.4% | small/insular: 1:10m generalisation |
| ATF | 7,280 |  | — | — | — | — | 7,829 | -7.0% | includes NE-administered disputed area(s): Juan De Nova I. (4 km²); Europa Island (19 km²); Bassas da India (0 km²) |
| GLP | 1,657 |  | — | — | — | — | 1,780 | -6.9% | small/insular: 1:10m generalisation |
| NER | 1,181,302 |  | 1,266,700 | -6.7% | 1,267,000 | -6.8% | 1,267,000 | -6.8% | unexplained — needs review |
| ABW | 170 |  | 180 | -5.7% | 180 | -5.7% | 193 | -12.0% | small/insular: 1:10m generalisation |
| CCK | 13 |  | — | — | — | — | 14 | -5.7% | small/insular: 1:10m generalisation |
| MYT | 395 |  | — | — | — | — | 374 | +5.6% | includes NE-administered disputed area(s): Mayotte (395 km²); small/insular: 1:10m generalisation |
| VCT | 368 |  | 389 | -5.5% | 389 | -5.5% | 389 | -5.5% | small/insular: 1:10m generalisation |
| BTN | 40,421 |  | 38,140 | +6.0% | 38,390 | +5.3% | 47,000 | -14.0% | includes NE-administered disputed area(s): Bhutan (northwest valleys) (1,237 km²); Bhutan (Chumbi salient) (146 km²) |
| GGY | 74 |  | — | — | — | — | 78 | -5.1% | small/insular: 1:10m generalisation |
| DJI | 21,848 |  | 23,180 | -5.7% | 23,200 | -5.8% | 23,000 | -5.0% | excludes adjacent NE feature(s) SOL |

Large countries (for orientation):

| ISO3 | NE km² | WB land | Δ | WB surface | Δ | GeoNames | Δ |
|---|---:|---:|---:|---:|---:|---:|---:|
| RUS | 16,980,192 | 16,376,870 | +3.7% | 17,125,190 | -0.8% | 17,100,000 | -0.7% |
| ATA | 12,358,218 | — | — | — | — | 14,000,000 | -11.7% |
| CAN | 9,945,630 | 8,788,700 | +13.2% | 15,634,410 | -36.4% | 9,984,670 | -0.4% |
| USA | 9,464,218 | 9,147,420 | +3.5% | 9,831,510 | -3.7% | 9,629,091 | -1.7% |
| CHN | 9,375,219 | 9,388,210 | -0.1% | 9,562,910 | -2.0% | 9,596,960 | -2.3% |
| BRA | 8,472,666 | 8,358,140 | +1.4% | 8,510,420 | -0.4% | 8,511,965 | -0.5% |
| AUS | 7,691,174 | 7,692,020 | -0.0% | 7,741,220 | -0.6% | 7,686,850 | +0.1% |
| IND | 3,150,745 | 2,973,190 | +6.0% | 3,287,263 | -4.2% | 3,287,590 | -4.2% |
| ARG | 2,782,896 | 2,736,690 | +1.7% | 2,780,400 | +0.1% | 2,766,890 | +0.6% |
| KAZ | 2,720,767 | 2,699,700 | +0.8% | 2,724,902 | -0.2% | 2,717,300 | +0.1% |
| COD | 2,325,240 | 2,267,050 | +2.6% | 2,345,410 | -0.9% | 2,345,410 | -0.9% |
| DZA | 2,308,858 | 2,381,740 | -3.1% | 2,381,740 | -3.1% | 2,381,740 | -3.1% |
| GRL | 2,154,380 | 410,450 | +424.9% | 410,450 | +424.9% | 2,166,086 | -0.5% |
| MEX | 1,957,845 | 1,943,950 | +0.7% | 1,957,194 | +0.0% | 1,972,550 | -0.7% |
| SAU | 1,921,725 | 2,149,690 | -10.6% | 2,149,690 | -10.6% | 1,960,582 | -2.0% |

### Land-border adjacency: counts

| set | pairs |
|---|---:|
| NE 1:10m shared polygon edges, both sides crosswalk countries | 321 |
| NE pairs involving an unmapped NE feature (kept, flagged) | 17 |
| NE pairs with a disputed boundary class on some stretch | 37 |
| GeoNames `neighbours` (symmetrised) | 323 |
| Wikidata P47 (current, non-deprecated, no end time; both items with ISO 3166 alpha-3) | n/a |
| NE ∩ GeoNames | 320 |

* In NE but not GeoNames (1): CHN–HKG
* In GeoNames but not NE (3): ALB–SRB, CUB–USA, DJI–SOM
* NE pairs with an unmapped NE feature: ARG–SPI, CHL–SPI, CHN–KAS, CUB–USG, CYP–CNM, CYP–ESB, CYP–WSB, DJI–SOL, EGY–BRT, ETH–SOL, IND–KAS, PAK–KAS, SDN–BRT, SOM–SOL, CNM–CYN, CNM–ESB, CYN–ESB
* NE pairs with a disputed stretch: ARG–CHL (1 km), ARG–SPI (71 km), BLZ–GTM (249 km), BRA–URY (41 km), BTN–CHN (145 km), CHL–SPI (87 km), CHN–IND (1,631 km), CHN–PAK (173 km), CHN–KAS (75 km), DZA–MAR (1,115 km), EGY–SDN (289 km), EGY–BRT (1 km), ESH–MAR (1,323 km), ESP–PRT (32 km), ETH–SOM (858 km), ETH–SSD (168 km), GUF–SUR (148 km), GUY–SUR (280 km), GUY–VEN (610 km), HRV–SRB (10 km), HRV–SVN (5 km), IND–PAK (795 km), IND–KAS (81 km), ISR–JOR (0 km), ISR–LBN (98 km), ISR–PSE (384 km), ISR–SYR (89 km), KEN–SSD (235 km), KOR–PRK (198 km), LBN–SYR (286 km), MWI–TZA (323 km), PAK–KAS (64 km), RUS–UKR (147 km), SDN–SSD (159 km), SDN–BRT (91 km), SOM–SOL (391 km), CNM–CYN (145 km)

Longest NE 1:10m land borders:

| pair | km (1:10m) | classes |
|---|---:|---|
| CAN–USA | 7,936 | international 7,907, untyped 29 |
| KAZ–RUS | 6,658 | international 6,635, untyped 22 |
| ARG–CHL | 4,946 | international 4,931, untyped 14, indefinite 1 |
| CHN–MNG | 4,378 | international 4,378, untyped 0 |
| CHN–RUS | 3,544 | international 3,544 |
| MNG–RUS | 2,913 | international 2,913 |
| BOL–BRA | 2,664 | international 2,663, untyped 1 |
| IND–PAK | 2,657 | international 1,861, line_of_control 795, untyped 1 |
| MEX–USA | 2,641 | international 2,634, untyped 7 |
| BGD–IND | 2,383 | international 2,381, untyped 2 |
| AGO–COD | 2,303 | international 2,297, untyped 6 |
| CHN–IND | 2,246 | disputed 1,631, international 599, untyped 15 |

### Landlocked (45 crosswalk countries; Caspian-only shores count as landlocked)

AFG, AND, ARM, AUT, AZE, BDI, BFA, BLR, BOL, BTN, BWA, CAF, CHE, CZE, ETH, HUN, KAZ, KGZ, LAO, LIE, LSO, LUX, MDA, MKD, MLI, MNG, MWI, NER, NPL, PRY, RWA, SMR, SRB, SSD, SVK, SWZ, TCD, TJK, TKM, UGA, UZB, VAT, XKX, ZMB, ZWE

Doubly landlocked: LIE, UZB. Caspian shores (km, 1:10m): AZE 725, IRN 732, KAZ 2,095, RUS 1,307, TKM 1,887

Fully surrounded countries (enclave_of): LSO in ZAF; SMR in ITA; VAT in ITA

Exclave components (48; non-largest pieces touching foreign land):

* AGO: 6,702 km² coastal (touches COD, COG)
* ARE: 7 km² enclosed (touches OMN)
* ARG: 20,552 km² coastal (touches CHL)
* ARG: 28 km² coastal (touches CHL)
* ARM: 35 km² enclosed (touches AZE)
* AZE: 4,783 km² enclosed (touches ARM, IRN, TUR)
* AZE: 27 km² enclosed (touches ARM)
* AZE: 7 km² enclosed (touches ARM)
* BRN: 1,299 km² coastal (touches MYS)
* CAN: 203,080 km² coastal (touches USA)
* CHL: 26,556 km² coastal (touches ARG)
* CIV: 65 km² coastal (touches GHA)
* CYP: 194 km² coastal (touches CNM, ESB)
* CYP: 2 km² coastal (touches ESB)
* CYP: 1 km² enclosed (touches ESB)
* DEU: 375 km² coastal (touches POL)
* ESP: 43 km² coastal (touches MAR)
* ESP: 18 km² coastal (touches MAR)
* ESP: 13 km² enclosed (touches FRA)
* GBR: 14,239 km² coastal (touches IRL)
* HRV: 750 km² coastal (touches BIH, MNE)
* IDN: 381,927 km² coastal (touches PNG)
* IDN: 13,830 km² coastal (touches TLS)
* IDN: 212 km² coastal (touches MYS)
* LTU: 133 km² coastal (touches RUS)
* MWI: 119 km² enclosed (touches MOZ)
* MWI: 80 km² enclosed (touches MOZ)
* MYS: 130,471 km² coastal (touches THA)
* MYS: 212 km² coastal (touches IDN)
* NIC: 1 km² coastal (touches CRI)
* NLD: 825 km² coastal (touches BEL)
* OMN: 1,579 km² coastal (touches ARE)
* OMN: 98 km² enclosed (touches ARE)
* PSE: 339 km² coastal (touches EGY, ISR)
* RUS: 27,095 km² coastal (touches UKR)
* RUS: 13,451 km² coastal (touches LTU, POL)
* TJK: 119 km² enclosed (touches KGZ)
* TJK: 30 km² enclosed (touches UZB)
* TLS: 739 km² coastal (touches IDN)
* TUR: 23,387 km² coastal (touches BGR, GRC)
* USA: 1,427,509 km² coastal (touches CAN)
* USA: 5 km² coastal (touches CAN)
* UZB: 399 km² enclosed (touches KGZ)
* UZB: 58 km² enclosed (touches KGZ)
* CNM: 12 km² coastal (touches CYN, CYP, ESB)
* CNM: 12 km² coastal (touches CYN, CYP)
* CYN: 6 km² coastal (touches CNM)
* USG: 27 km² coastal (touches CUB)

### Coverage per atlas table (live run)

| atlas table | rows | crosswalk countries | unmapped provider geos |
|---|---:|---:|---|
| atlas.naturalearth.admin0_10m | 1647 | 249 | BJN, BRT, CNM, CYN, ESB, KAS, PGA, SCR, SER, SOL, SPI, TWN, USG, WSB |
| atlas.naturalearth.admin0_10m_borders | 676 | 165 | BRT, CNM, CYN, ESB, KAS, SOL, SPI, USG, WSB |
| atlas.naturalearth.admin0_10m_disputed | 99 | 46 | B20, B30, B41, B42, B43, B45, B46, B50, B70, B77, B94, B97 |
| atlas.geonames.countryinfo | 3420 | 249 | ANT, SCG, TWN |
| atlas.cckp.cru_ts4_09_clim_1991_2020 | 12200 | 243 | TWN |
| atlas.cckp.era5_clim_1991_2020 | 492 | 245 | TWN |
| atlas.inform.risk_2026_natural_hazards | 1528 | 191 |  |
### Reading the area table

* The geometry is Natural Earth's, so an entity's area includes inland water inside its polygons. It also
  includes what NE's de facto view attaches to it, and excludes features NE keeps apart. WB "surface area"
  sometimes includes coastal/territorial waters (Canada 15.6 M km², Bermuda 4,290 km²), and WB "land area"
  excludes all inland water. None of the three references is the same concept as the NE polygon area. That
  is why the catalog keeps the computed and GeoNames-declared areas as **separate concepts**.
* Rows marked "unexplained — needs review" have no rule-based explanation.
  * Serbia: WB/GeoNames include Kosovo, which NE and the crosswalk treat as XKX.
  * Antarctica: GeoNames 14,000,000 is rounded and includes ice shelves.
  * Yemen, Saudi Arabia, Libya, Niger, Suriname, French Guiana: the boundary lines NE draws differ from those
    behind the official totals (desert and Guiana Shield boundaries). These are not errors of computation —
    the geodesic area of the polygons is exact to < 1e-9.
* Small islands are underestimated or overestimated at 1:10m (generalisation). Their areas should not be
  used as official figures.
