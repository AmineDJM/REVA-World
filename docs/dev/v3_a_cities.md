# V3 A-CITIES — cities and urbanisation for every country

Agent A-CITIES, 2026-10-15/16. Two atlas sources and one KG link, all through the single atlas ingest entry point
(`reva.atlas.ingest.run_atlas_ingest`) and WDHARVEST's Wikidata client. No HTTP client is built in these modules.

## What was built

| atlas source (connector) | module | atlas tables | requests / run |
|---|---|---|---:|
| `atlas.geonames_cities` (source key `geonames.cities`, CC BY 4.0) | `src/reva/adapters/connectors/atlas_cities.py` | `geonames.cities15000`, `geonames.cities15000_by_country`, `geonames.admin1`, `geonames.cities15000_names` (on request) | 2 (3 with names) |
| `atlas.un_wup` (source key `un.wup`, CC BY 3.0 IGO) | `src/reva/adapters/connectors/atlas_wup.py` | `un_wup.wup2025_urban_rural`, `un_wup.wup2025_degurba`, `un_wup.wup2025_cities`, `un_wup.wup2025_cities_area`, `un_wup.wup2025_capitals` | 4 |

Other files:

* sources `registry/sources/{geonames_cities,un_wup}.yaml`;
* budget request `registry/atlas/budget_requests/a_cities.yaml`;
* catalog `registry/atlas/catalog/urban.yaml` (20 provider-declared concepts, each with an explicit `theme`);
* geo alias `registry/atlas/geo_aliases/a_cities.yaml` (scheme `geonames_cc` = ISO alpha-2 + `XK` → `XKX`,
  quoted from GeoNames countryInfo);
* KG query specs `registry/kg/queries/places/wikidata/{geonames_ids,country_capitals}.yaml`;
* fixtures `tests/fixtures/source/atlas.{geonames_cities,un_wup}/` (excerpts of the real bytes, ~110 kB);
* tests `tests/unit/test_atlas_cities.py` (17, offline) and `tests/integration/test_atlas_cities_ingest.py`
  (2, `db`, fixture mode through the entry point).

Run:

```bash
uv run python -m reva.atlas.ingest atlas.geonames_cities [--dataset cities15000] [--option altnames=1] [--fixture]
uv run python -m reva.atlas.ingest atlas.un_wup [--dataset wup2025_capitals] [--fixture]
# Wikidata links (throwaway reva_* database that holds the GeoNames ingest):
uv run python -m reva.adapters.connectors.atlas_cities wikidata --db reva_xxx --atlas-object-root <root> \
    [--labels none|capitals|all] [--max-chunks N] [--report out.json]
```

## Design and honesty decisions

### GeoNames (cities15000, admin1, alternate names)

* **One row per place, folded into per-country atlas rows.** Atlas rows are per country, so `provider_geo` is
  the GeoNames country code (verbatim) and the place is folded into the indicator, as other agents fold a
  counterpart: `population|geonameid=2267057`, `name|…`, `asciiname|…`, `feature_code|…`, `admin1_code|…`,
  `latitude|…`, `longitude|…`, `elevation|…`, `dem_elevation|…`, `timezone|…`, `cc2|…`, `modification_date|…`.
  `entity_qid` stays null: the place's Wikidata item lives in the KG link (below), not in the atlas table.
* **Values as GeoNames states them.** GeoNames dates nothing, so every row uses the one-day retrieval snapshot as
  period (`flags.period_basis`, as A-GEO does for countryInfo). The place record's `modification date` is kept in
  the population flags; it is the last edit of the record, not the date of the figure, and the flags say so.
* **No filled numbers.** A population of `0` means "not known" in GeoNames (3 places, e.g. Plymouth, Montserrat;
  Ngerulmud, Palau). Those cells and DEM `-9999` ("no data") are omitted and counted, never stored as numbers.
  Empty elevations (most places) are omitted too.
* **Per-country aggregates only when exact.** They are counts and selections over this one file, so they are
  exact with respect to GeoNames:
  * places with population > 15,000, with and without PPLX (sections of a populated place), PPLH, PPLQ, PPLW
    and PPLCH;
  * the largest place and its population (ties listed);
  * the PPLC places and their count.
  * When a country has several PPLC places, all are listed (`A | B`, `multiple_pplc`) and none is chosen. A capital
    population is given only when there is exactly one PPLC place.
  * Three country codes have **no** PPLC place at all: EH, IL and PS. Jerusalem is coded PPLA. This is reported,
    not filled.
* **fr/en alternate names** need the 205 MB `alternateNamesV2.zip`; the 19-column cities file has no language
  tags. They are fetched only on request (`--dataset cities15000_names` or `--option altnames=1`). The file is
  streamed line by line, never held decompressed. Only `fr`/`en` names of cities15000 places are kept, at most 5
  per place and language:
  * order: preferred, then short, then the others; colloquial and historic names come last;
  * GeoNames' preferred/short/colloquial/historic/from/to flags are kept;
  * the number of names not kept is counted.
* **admin1CodesASCII** becomes `geonames.admin1`: name, ASCII name and geonameid per division, plus the count per
  country. Each place's `admin1_code` row carries the division name and geonameid in its flags.

### UN World Urbanization Prospects

* **Latest revision: WUP 2025.** It was released 2025-11-18 and is the 22nd edition. Its FAQ says it "supersedes
  all previous estimates and projections"; the previous edition was WUP 2018.
  * Licence statement in every WUP 2025 workbook: "Copyright © 2025 by United Nations, made available under a
    Creative Commons license CC BY 3.0 IGO".
  * The connector checks that this statement is still in the capitals workbook.
* **Two concepts of "urban", never mixed:**
  * national definitions (`wup2025_urban_rural`: Urban, Rural, Total, 1950–2050), which is the WUP 2018 concept;
  * the harmonised **Degree of Urbanisation** (`wup2025_degurba`: Cities, Towns, Rural, Cities and Towns, Total;
    population, share, land and built-up areas from 1975). This is new in 2025 and based on EC-JRC grids.
* **Cities.** WUP 2025 dropped the WUP 2018 "urban agglomerations ≥ 300,000" list (1950–2035). It publishes
  16,828 Degree-of-Urbanisation cities instead: ≥ 50,000 inhabitants and ≥ 1,500 per km², 1975–2050.
  * `wup2025_cities`: mid-year population per city and year, plus the population-weighted centroid of the city's
    latest estimate year.
  * `wup2025_cities_area`: land area, built-up area and density.
  * `flags.pop_plausibility` (Low/Moderate/High) is kept as the UN states it.
  * The WUP 2018 agglomeration list (1950–1970 values) is **not** ingested: it is superseded, and it ships only as
    legacy `.xls` workbooks, for which the project has no reader. See gaps.
* **Projections are separate series.** Every year ≥ 2026 carries the indicator suffix `|proj=wup2025` and
  `flags.projection = true`. The catalog has separate `_proj` concepts, so an estimate lookup can never return a
  projection.
* **Capitals (`wup2025_capitals`, file F20).**
  * Every capital row is kept, with its `Capital_Type` verbatim. There are 33 types, from "National capital" and
    "Seat of Government/Economic Capital/De facto capital" to "Territorial capital/De jure capital (evacuated due
    to volcano)".
  * Several capitals of one country are all kept. Malaysia lists Kuala Lumpur and Putrajaya; Sri Lanka lists the
    same settlement twice with two roles.
  * `national_capital_pop` exists only when exactly one row is a "National capital…".
  * A capital that is no DEGURBA settlement has no city code and no population (Plymouth in Montserrat, Alofi in
    Niue, Ngerulmud in Palau). It is named and given no number.
* **Unit metadata.** The UN indicator list gives "thousands" as unit of the mid-year `AREA_km2`/`BU_km2` columns.
  Those columns equal the 1 January km² columns, so they are stored as `km2` and the discrepancy is flagged
  (degurba rows) or noted (city coverage note).
* Aggregates (World, SDG regions, income groups…) are kept with `iso3 = null`. Geography uses A-UN's `un_m49x`
  scheme: 412 → XKX; 158 → TWN through the crosswalk.

### KG link: GeoNames place → Wikidata item (never by name)

* A place is linked to a Wikidata item **only** through Wikidata's own GeoNames ID statements (P1566):
  * Spec `geography.places_geonames_ids` uses `VALUES ?gn { "…" }`, up to 300 ids per WDQS request.
  * It runs through WDHARVEST's locked, paced, budget-metered `WikidataClient` (`reva.kg.harvest.budgeted_client`).
  * Rows are converted by WDHARVEST's `Harvester.rows_to_statements` and loaded by its `KgDbSink` as
    `wikidata.wdqs` P1566 statements, with statement ids and rank.
  * Chunks are resumable and bounded to 4 rows per id (`row_bound_exceeded`, never truncated).
  * Ids are sent in priority order: capitals and seats of government, admin-1 divisions, PPLA seats, then places by
    population. A throttled partial run therefore maps the important places first.
* A geonameid held by **exactly one** item is mapped. Held by several items, it is reported as ambiguous and not
  used.
* The GeoNames-sourced statements (source `geonames.cities`, reference = cities15000 line + raw sha256) are loaded
  only on mapped items:
  * `P17` country;
  * `P1376` capital of (PPLC only);
  * `P131` admin-1 division (only when the division's own geonameid is mapped);
  * `P1082` population (no date: GeoNames states none).
* Every statement records `subject_mapped_via: {property: P1566, wikidata_statement_id}`.
* Country objects come from `registry/entities/country_qids.json` (WDHARVEST). Without it, the country-valued
  statements are reported as blocked, as in A-GEO.
* Chain: "Lisbonne → capitale du Portugal" = Q597 —P1376 (GeoNames PPLC, `geonames.cities`)→ Q45. Wikidata's own
  P36/P1376 claims come from the core harvest. The fr label "Lisbonne" comes from the Wikidata label snapshot
  (`--labels`) or from the GeoNames fr alternate names.
* **Capital cross-check** (spec `geography.places_country_capitals`, one WDQS request): GeoNames PPLC is compared
  with Wikidata P36 for every item holding a P298 code. Only current statements count (no end time; rank
  preferred/normal). Agreement means a PPLC geonameid is one of the capital's own P1566 values. Each country gets
  a status, listed with both sides and never resolved:
  * `agree`;
  * `agree_partial_multiple`;
  * `disagree`;
  * `geonames_only`;
  * `wikidata_only`.
* The two specs are `enabled: false` for the generic harvester:
  * the first fans out over GeoNames ids, while the harvester splits only by QIDs;
  * the second is a cross-check, because countries' P36 claims already come from `core.countries`.
* They live in `places/wikidata/` with `domain: geography`, because the spec schema's domain enum has no `places`
  (request below).

LIVE_SECTION

## Licences

| source | licence | statement |
|---|---|---|
| GeoNames cities15000 / admin1CodesASCII / alternateNamesV2 | CC BY 4.0 | readme.txt: "This work is licensed under a Creative Commons Attribution 4.0 License"; attribution "GeoNames (www.geonames.org)" on every table |
| UN WUP 2025 | CC BY 3.0 IGO | every WUP 2025 workbook, `info` sheet: "Copyright © 2025 by United Nations, made available under a Creative Commons license CC BY 3.0 IGO" (checked by the connector); citation "United Nations, DESA, Population Division (2025). World Urbanization Prospects: The 2025 Revision, Online Edition." |
| Wikidata P1566/P36 | CC0 | community-edited, never "verified" (KG labels it) |

## Requests to other owners

1. **ATLASCORE/ATLASOPS:** merge `registry/atlas/budget_requests/a_cities.yaml` (`geonames.cities` 6/day,
   `un.wup` 10/day). Until then `tests/invariants/test_invariant_30_budgets.py::test_budget_keys_are_registered…`
   lists `geonames.cities` and `un.wup`, next to other agents' pending keys. My modules create no HTTP client: the
   request-path invariant passes.
2. **WDHARVEST:**
   * add `places` to the domain enum of `registry/kg/queries/_schema.json`; I will then move the two specs to
     `places/`;
   * a `split: {by: literal_values}` option would let the generic harvester run the GeoNames-id fan-out itself.
3. **Lead:**
   * the seed hook registers both atlas sources automatically;
   * `migrations/versions/0011_kg_fixes.py` (REVFIX, in flight) is not yet in `migrations/schema_contract.json`, so
     `bootstrap()` raises a schema-drift error on a fresh database. My throwaway database was bootstrapped with
     the error caught: migrations and roles complete, then the registry seeded.
