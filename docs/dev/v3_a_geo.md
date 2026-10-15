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
`atlas.cckp_climatology`, `atlas.inform_risk_hazards`). The other three also run in `--fixture` mode.
Natural Earth has no offline fixture mode: its four large layers (16 MB) are not committed. Offline tests
replace them with a synthetic shapefile world (`tests/fixtures/source/atlas.naturalearth_10m/synthetic_world.py`)
plus the two small real layers, which are captured.

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
    Guantánamo, Taiwan, Spratly, Scarborough, Bajo Nuevo and Serranilla.
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
| CCKP has no aggregate for ATA, ESH, FLK, SGS, CHI; null for BLM, BVT; TWN unmapped | climate | none open at country level found; the CRU/ERA5 grids would need REVA's own zonal statistics (not done: that would be a REVA estimate, not a source value) |
| INFORM covers 191 countries (no small territories) | hazards | WorldRiskIndex (CC BY) could fill some; not ingested |
| Elevation | — | Wikidata highest point (KG), or zonal stats on GMTED2010 (heavy) |
| Official (UN Demographic Yearbook) areas | area check | the DYB table is published as PDF/xls, not a stable API; WB (FAO-sourced) and GeoNames used instead |
| KG statements need ISO3→QID | KG | `registry/entities/country_qids.json` (WDHARVEST) does not exist yet: `atlas_ingest` reports the KG load as *blocked* (nothing is guessed). The live run used a one-off Wikidata P298 lookup only inside the throwaway DB. |
