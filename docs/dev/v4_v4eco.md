# V4 V4ECO — ecology & animals: where species live, how many records, who eats whom, what threatens them, trends

Agent V4ECO (2026-10-17). Scope: `docs/roadmap/v4_world_model.md` (row V4ECO). Everything below is sourced,
identifier-linked and provider-aggregated (no bulk dumps); nothing is merged by name; no causal claim is made.

## What existed before (extended, not duplicated)

| Already in REVA | Owner | V4ECO action |
|---|---|---|
| Threatened species counts per country (mammals, birds, fish, higher plants; IUCN via WDI `EN.*.THRD.NO`) | A-WB | kept; not re-ingested |
| Protected areas % (WDPA via WDI `ER.LND.PTLD.ZS`, `ER.MRN.PTMR.ZS`, `ER.PTD.TOTL.ZS`), marine KBAs protected (SDG `ER_MRN_MPA`) | A-WB / A-UN | kept |
| National parks (Wikidata seed spec `nature/protected`) | KGSCIECO | kept |
| FAOSTAT live animal stocks: cattle, buffalo, sheep, goats, pigs, chickens, camels, horses, asses (+ aggregates) | A-FAO | **extended** with 9 items (below) |
| FAO SDG 14.4.1 fish stocks, 15.x forest | A-FAO | kept |
| Wikidata P183 / P9714 / P1034 allow-listed and mapped to families | V4CORE | V4ECO writes the harvest specs |

## Sources

| Source (source_key) | URL | Licence | What is fetched | Bytes (one full run) |
|---|---|---|---|---|
| GBIF occurrence index (`gbif.occurrence`, connector `atlas.gbif_occ` 1.0.1) | `https://graphql.gbif.org/graphql`, `https://api.gbif.org/v1/species/{key}` (+`/iucnRedListCategory`) | GBIF data user agreement; counts aggregate records licensed CC0 / CC BY 4.0 / CC BY-NC 4.0 (global licence facet 2026-10-17: 2.75 G / 0.65 G / 0.45 G records); IUCN categories: CC BY 4.0 as stated on GBIF dataset 19491596-35ae-4a91-9a98-85cf505f1bd3 | GraphQL `occurrenceSearch` with a `countryCode` facet (size 300) whose buckets carry `count` and nested `cardinality { speciesKey }`; 34 taxon groups, years 1970-2025, 9 IUCN categories, 45 seed taxa; species records | 267 requests, ~5 MB |
| GloBI (`globi.interactions`, `atlas.globi` 1.0.1) | `https://api.globalbioticinteractions.org/interaction` | **unclear**: GloBI aggregates datasets under their own licences; the API response does not state them (flagged `licence_status` on every statement) | taxon-level aggregates for 45 seed taxa x {as source, as target} x 8 interaction types, `limit` 2000 | 90 requests, <= 25 MB |
| RESOLVE Ecoregions 2017 (`resolve.ecoregions2017`, `atlas.ecoregions` 1.0.1) | `https://storage.googleapis.com/teow2016/Ecoregions2017.zip` | CC BY 4.0 (`LICENSE` attribute of every record, checked per run); Dinerstein et al. 2017, BioScience 67(6) | **two HTTP byte ranges** of the 149 MB zip: central directory (64 kB) + the `Ecoregions2017.dbf` member (55 kB compressed, CRC-checked); the 243 MB polygons are never downloaded | 2 requests, 0.12 MB |
| FAO FishStat Global Production (`fao.fishstat`, `atlas.fishstat` 1.0.1) | `https://www.fao.org/fishery/static/Data/GlobalProduction_2026.1.0.zip` | CC BY 4.0 (FAO database terms quoted in the release ReadMe; checked per run) | release zip (code lists + 1,159,616 quantity cells) | 1 request, 6.4 MB |
| Living Planet Index, published trends (extension of `atlas.owid` 1.1.1) | OWID grapher `global-living-planet-index` (csv + metadata + 3 indicator metadata) | OWID origin metadata: CC BY 4.0; ZSL LPI data-use policy: published trends CC BY-SA 4.0 (stricter applies; owner decision below) | world, 5 IPBES regions, freshwater / marine / terrestrial, 1970-2022, with confidence interval | 5 requests, 40 kB |
| Red List Index SDG 15.5.1 (extension of `atlas.un_sdg` 1.1.1) | UN SDG API series `ER_RSK_LST` | UN SDG Global Database terms (as for A-UN's other series) | country/region RLI, custodians IUCN + BirdLife | 1-2 pages |
| FAOSTAT QCL (extension of `atlas.fao` 1.0.3) | existing bulk zip | CC BY 4.0 | stocks of ducks 1068, geese 1072, turkeys 1079, other birds 1083, mules 1110, rabbits 1140, other rodents 1150, other camelids 1157, beehives 1181 | same zip as before |
| Wikidata (WDHARVEST harvester, `wikidata.wdqs`) | specs `registry/kg/queries/geography/ecology/*.yaml` | CC0 | P183 endemic to (>= 5 sitelinks, 28,367 st.), P9714 taxon range (>= 20 sitelinks, 23,357), P1034 main food source (all, 2,330), P2975 host (>= 20 sitelinks, 4,678), P141 IUCN status (>= 20 sitelinks, 16,420) — counts probed on WDQS 2026-10-17 | not yet harvested |

Property requests (`registry/kg/property_requests/v4eco.yaml`), merged into the allow-list on 2026-10-17 with
`python -m reva.kg.harvest merge-property-requests` (ids, datatypes and labels checked against Wikidata):
P141 IUCN conservation status, P627 IUCN taxon ID, P846 GBIF taxon ID (Wikidata now labels it "GBIF-species-ID
(before 2026 update)": the GBIF backbone was re-issued in 2026, see "Missing"), P2975 host.

## What is loaded

Live loads in the dev DB `reva` (origin `live`, 2026-10-17):

| Source | Result |
|---|---|
| `atlas.ecoregions` | KG load 444: **869 entities** (847 `resolve_eco:*` ecoregions, 14 `resolve_biome:*`, 8 `resolve_realm:*`), **2,538 statements** (in_biome 846, in_realm 846, nature_needs_half 846), 0 rejected; 2 requests, 121,980 bytes |
| `atlas.fishstat` | atlas table `fishstat.production`: **136,317 rows** (country x production source x Yearbook / Major group x measure x year, 1950-2024) from 1,159,616 FAO cells; unmapped (kept): ANT, CSK, EAZ, SCG, SUN, YUG (former entities with FAO ISO3) and un:230 Ethiopia PDR, un:736 Sudan [former], un:896 Other NEI; 78 sums flagged lower_bound (suppressed `Q` cells), 8 all-missing sums Missing; 1 request, 6,382,577 bytes |
| `atlas.globi`, `atlas.gbif_occ` | GLOBI_GBIF |
| `atlas.un_sdg` (RLI), `atlas.owid` (LPI), `atlas.fao` QCL (9 items) | code + fixtures + tests done; not re-ingested live (each re-fetches the whole existing source: run the commands below) |

Fixture replays (tests, origin `test_fixture`): GBIF excerpt 4 tables (1,006 + 496 + 494 + 293 rows; 2 groups, 1 year,
1 IUCN category, 2 seeds), GloBI 4 queries -> 2,664 interaction edges + 6 resolutions (351 rows without usable id,
93 rows of type `visits` not mapped), ecoregions 847, FishStat excerpt.


## Relation families (`registry/kg/relations_v4.d/v4eco.yaml`, predicates `registry/kg/predicates_eco.yaml`)

| Predicate | Family | Note |
|---|---|---|
| `gbif_occ:recorded_in` | located_in (`gbif_occurrence_records_in`) | taxon -> country with >= 10 GBIF records (PRESENT, no fossils / living specimens); presence records, not native range |
| `gbif_occ:measured_by` | measured_by | taxon -> `atlas:gbif_occ.<dataset>` entity; qualifier lists the indicator ids |
| `gbif:iucn_red_list_category` | (literal) | global IUCN category per GBIF species |
| `globi:eats`, `globi:preys_on` | eats | as GloBI returns them |
| `globi:parasite_of` (incl. endo/ecto/parasitoid), `globi:pathogen_of`, `globi:pollinates`, `globi:visits_flowers_of`, `globi:vector_of`, `globi:has_host` | interacts_with | recorded interactions, never causal |
| `globi:resolved_within` | is_a (`globi_taxon_resolution`) | GloBI-returned taxon id -> queried GBIF taxon (GloBI's own taxon graph) |
| `resolve:in_biome`, `resolve:in_realm` | part_of | ecoregion -> biome / realm |
| `resolve:nature_needs_half` | (literal) | NNH category 1-4 + name, verbatim |
| Wikidata P2975 host | interacts_with | requested by V4ECO |
| Wikidata P141 IUCN status | context | community-edited copy of an expert assessment |
| Wikidata P183, P9714, P1034 | located_in / eats | mapped by V4CORE; V4ECO wrote the specs |

`python -m reva.kg.families check`: ok (21 families, all mappings valid, none causal).

## Examples (ids)

* **Ecoregion -> biome -> realm** (live): `resolve_eco:686` Western European broadleaf forests --part_of
  (resolve:in_biome)--> `resolve_biome:4` Temperate Broadleaf & Mixed Forests; `resolve_eco:686` --part_of
  (resolve:in_realm)--> `resolve_realm:Palearctic`. `resolve_eco:219` Borneo lowland rain forests -> `resolve_biome:1`
  Tropical & Subtropical Moist Broadleaf Forests, `resolve_realm:Indomalayan`. `resolve_eco:822` East Sahara
  Desert -> `resolve_biome:13` Deserts & Xeric Shrublands.
* **Who eats whom** (GloBI, fixture replay of real responses): `col:4CGXP` (Panthera leo, as GloBI indexes it)
  --is_a (globi:resolved_within)--> `gbif:5219404` (lion, GBIF backbone); `col:4CGXP` --eats--> `col:MLQ5` (Bos
  taurus) and `ncbi:59532` (Kobus megaceros); Apis mellifera (`eol:1045608` --resolved_within--> `gbif:1341976`)
  --interacts_with (globi:pollinates)--> `ncbi:237863` Passiflora ligularis; `eol:13677906` Crithidia mellificae
  --interacts_with (globi:parasite_of)--> `eol:1045608` Apis mellifera.
* **Where it lives / how threatened** (GBIF, fixture replay): `gbif:5219404` Panthera leo --located_in
  (gbif_occ:recorded_in, >= 10 records)--> Kenya (Q114), ...; `gbif:iucn_red_list_category` = VU (IUCN taxon
  15951); `gbif:5219404` --measured_by--> `atlas:gbif_occ.taxon_country` (indicator `occurrences.gbif_5219404`).
  Country series: `gbif_occ.country_group.distinct_species.mammalia` for FRA, `gbif_occ.country_iucn.distinct_species.cr`
  (e.g. snapshot 2026-10-17: 1,155 CR species with records in the US, 209 in France; global: 9,712).
* **How many fish** (live): `fishstat.production` `capture.yb_aquatic_animals_fish_crustaceans_and_molluscs_etc.tonnes`
  NOR 2023 = 2,353,574.223 t live weight (165 FAO cells summed, 149 species codes).
* **Population trends**: `owid.g_living_planet_index.lpi_final` World 2022 = 0.27 x 1970 level (LPI 2026), with
  `ci_low` / `ci_high`; regions (Africa, Latin America and the Caribbean, ...) and systems (Freshwater, Marine,
  Terrestrial) as LPI aggregates.
* **Livestock**: `fao.qcl.1181|5114` beehives, `fao.qcl.1068|5112` ducks ... next to A-FAO's cattle/sheep/chickens.


## Honesty notes

* **GBIF counts are not abundance.** Every cell says `caveat: occurrence records reflect sampling effort and data
  mobilisation, not abundance or population size`. Distinct species are GBIF's approximate cardinality
  (HyperLogLog++), flagged `approximate: true`; they are not national checklists.
* Ray-finned fishes have no `classKey` in the GBIF index (backbone puts them under no class): no fish class group
  exists; they are counted only in Animalia / Chordata. Reptilia is a pro-parte synonym: Squamata, Testudines and
  Crocodylia are used instead.
* GBIF countryCode `XZ` (international waters), `ZZ` and other non-ISO codes stay unmapped; records without a
  country are only in the global totals.
* IUCN categories via GBIF are **global** categories; a species "CR with records in BE" includes vagrants.
* GloBI: partners are kept under the exact ids GloBI returns (`col:`, `ncbi:`, `eol:`, `gbif:`, `inat_taxon:`,
  Wikidata `WD:` -> QID). `no:match` and unusable ids are dropped and counted. Responses reaching the limit are
  reported as truncated (Apis mellifera as source: 2,000 rows). Entities for partner taxa are written only when the
  KG has none (V4BIO owns taxon entities `gbif:<key>`, `col:<id>`; `eco_common.guarded_load(fill_missing_only=True)`).
* Ecoregions: "Rock and Ice" (ECO_ID 0) has BIOME_NAME / REALM / NNH_NAME "N/A": no statement is made for it. The
  dbf is cp1252 (UTF-8 decoding fails); the encoding is recorded in the load parameters. `SHAPE_AREA` is in
  squared degrees and kept only as a qualifier, never converted to km2.
* FishStat sums are REVA's deterministic aggregation of FAO's own cells within FAO's own species groups, measures
  never mixed (tonnes live weight vs number); a sum with any missing-symbol cell (`L`/`M`/`O`/`Q`) is
  `lower_bound: true`; all-missing sums are Missing (`flags.missing`), never zero. FAO areas without ISO3 keep
  `un:<code>`.
* OWID: a chart column with a `conversionFactor` now has its unit and flags say so (the `living-planet-index-by-region`
  chart multiplies by 100; V4ECO uses `global-living-planet-index`, which is unscaled and carries the CI).

## Threats -> species decline as causal claims (IUCN threat classification) — evaluated, blocked

The IUCN Threats Classification Scheme (v3.3) codes, per species assessment, threats with timing, scope and
severity ("Very rapid declines", "Slow, significant declines", "No decline" ...). Under `docs/roadmap/v3_causality.md`
this is an expert-coded dataset with a codebook: a threat coded with a declining severity would qualify as a **T1
claim** `threat class --contributing_factor--> decline of species X` (scope: the assessment; temporal check against
the assessment year; "No decline" / "Unknown" severities would not produce a claim). Access is blocked:
`api.iucnredlist.org/api/v4` answers 403 without a token and `www.iucnredlist.org` returns a Cloudflare 403 to
non-browser clients (2026-10-17); IUCN's terms also restrict redistribution and commercial use. Nothing was loaded;
no threat is inferred from anything else (GBIF only carries the category, not threats).

## Missing and why

| Item | Why | What would unblock it |
|---|---|---|
| IUCN Red List API (threats, habitats, population trend per species) | needs a token; website behind Cloudflare | owner: request an IUCN API token + licence review (non-commercial terms) |
| IUCN summary statistics tables (Table 5 per country) | iucnredlist.org 403 to non-browser clients | owner decision; partly covered by WDI threatened-species counts (A-WB) and GBIF x IUCN facets |
| Living Planet Database (population time series) | download behind a data-use agreement form (Shiny); terms forbid redistribution of the data and commercial use | owner signs the LPI data-use agreement; only the published trends (CC BY-SA) are loaded |
| Ecoregion <-> country membership | not in the RESOLVE release; would need the 243 MB polygons and an overlay (a derived overlap, never a `located_in` statement per V4CORE) | bigger machine: a separate derived table, labelled as REVA's overlay |
| GBIF per-ADM1 counts (`gadmLevel1Gid` facet) | GADM ids are under a non-commercial licence | owner licence decision |
| Ray-finned fish class counts | no `classKey` in the GBIF index | none (documented) |
| GloBI per-dataset licences | not in the API | owner: licence review of the contributing datasets |
| GBIF 2026 backbone update | Wikidata P846 is now "GBIF-species-ID (before 2026 update)"; the occurrence index still answers with the keys used here (lion 5219404 ...) | V4BIO / V4LINK: check whether a new backbone key set must be mapped |
| Wikidata ecology specs | written and validated, not harvested (WDQS budget shared) | run below |

## Owner decisions

1. IUCN token + licence (threat classification as T1 causal claims, per-species data).
2. LPI data-use agreement (population-level data); licence of the published trends: CC BY 4.0 (OWID origin
   metadata) vs CC BY-SA 4.0 (ZSL policy) — REVA applies the stricter.
3. GloBI licence review per contributing dataset (statements flagged `licence_status: unclear`).
4. GBIF: part of the counted records are CC BY-NC 4.0 (counts are facts, no record content stored).

## Commands (to finish / refresh on a bigger machine)

```bash
df -h /                                           # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed      # registers atlas.gbif_occ / globi / ecoregions / fishstat 1.0.1
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ecoregions
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.fishstat
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.globi
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gbif_occ      # ~267 GraphQL/REST requests, ~25 min
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.un_sdg        # picks up ER_RSK_LST (Red List Index)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.owid          # picks up the Living Planet Index chart
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.fao --dataset qcl   # 9 more live-animal stock items
uv run python -m reva.kg.harvest run registry/kg/queries/geography/ecology --via api   # Wikidata (throwaway DB first)
# then (lead): reva atlas snapshot, reva links materialise
```

Tests: `tests/unit/test_atlas_gbif_occ.py`, `tests/integration/test_atlas_v4eco_ingest.py` (fixture replay of all
four connectors), `tests/unit/test_atlas_qol_connectors.py` + `tests/integration/test_atlas_qol_ingest.py` (OWID
LPI). Fixtures: `tests/fixtures/source/atlas.{gbif_occ,globi,ecoregions,fishstat}` (ecoregions and FishStat are
small zips rebuilt from real members, see their manifests).
