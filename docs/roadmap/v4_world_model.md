# REVA World V4 — toward a sourced model of the whole world

Owner request (2026-10-17): extend REVA from countries, history, events and causality to *everything that
exists* — chemistry (atoms, molecules), materials and energy (what oil is made of, where it is, who produces
it, at what price, why), biology (species, genes, anatomy, diseases at body level), animals and ecology (where
they live, how many, who eats whom), weather and climate (space-time series), micro- and macro-economics
(firms, prices, quarters, regions), industry (plants, supply chains), psychology, behaviour, sociology — all
linked in space and time, past, present and projected future, with causal links between them.

REVA does not claim to be a copy of the world. It builds a **sourced, linked, space-time model** of what
public sources state, and says exactly what is missing. The V1–V3 constitution applies unchanged: no
fabricated values, typed Missing (never zero), provenance to raw bytes, licences per value, sources never
merged by name, causation only as attributed claims (`v3_causality.md`), candidates quarantined.

## Architecture (additions)

1. **One entity graph for every kind of thing.** Every domain loads into the existing KG (`kg.entity`,
   `kg.statement`, `load_batch`, append-only) with a namespaced `kind` (`chemical_element`, `isotope`,
   `compound`, `material`, `energy_source`, `taxon`, `gene`, `protein`, `anatomical_structure`, `disease`,
   `company`, `facility`, `product`, `commodity`, `region_adm1`, `region_adm2`, `ecoregion`,
   `weather_station`, `psych_construct`, `occupation`, …). Identifiers are the domain's own authority
   (ChEBI, PubChem CID, NCBI/GBIF taxon key, HGNC, UBERON, MONDO/DOID, ICD-11, LEI, GEM id, ISO 3166-2,
   geoBoundaries id, GHCN station id, ISIC/HS/CPC codes), never REVA-made unless labelled so.
2. **Universal relation families** (`registry/kg/relations_v4.yaml`, owner V4CORE): every source property
   (`chebi:has_part`, `uberon:part_of`, `globi:eats`, `gem:owned_by`, `wikidata:P186 made from material`,
   `hgnc:located_on_chromosome`, …) maps to one family — `is_a`, `part_of` / `has_part`, `made_of`
   (composition, with share when the source gives it), `located_in` (space), `occurs_during` (time),
   `produces` / `produced_by`, `owns` / `owned_by`, `supplies` / `buys_from`, `trades_with`, `eats` /
   `interacts_with` (ecology), `associated_with` (gene–disease etc.: association, never causation),
   `measured_by` (a series about an entity), `discovered_by` / `invented_by`, `regulates`, `context`.
   Families are traversable by "Neurones" and paths; **none of them is causal**. Causal statements a source
   makes (e.g. a disease ontology "has material basis in", a pathogen "causes" a disease, an expert dataset
   "event → effect") go through the causal loader as claims with tiers and grades.
3. **Space-time cube.** Places: country (V3) → ADM1/ADM2 (geoBoundaries, ISO 3166-2), cities (V3),
   ecoregions, grid cells only as aggregated series. Time: every value carries period + precision
   (day/month/quarter/year/decade), BCE astronomical years; projections carry `projection_upstream`
   (scenario, model) and are never mixed with observations.
4. **Series, not dumps.** Disk is ~6 GB in this container, so large sources (GBIF occurrences, ERA5,
   PubChem, GLEIF, Exiobase) are reached through the provider's own aggregation (API facets, monthly country
   series, summary files) and stored as compact atlas tables + KG entities; the raw bytes actually fetched are
   archived as usual. Each agent keeps its raw archive ≤ 300 MB and checks `df -h /` ≥ 5 GB before each
   ingest. Full-resolution loads wait for a bigger machine (documented per source in the agent report).
5. **Atlas themes** gain `chemistry`, `materials`, `biology`, `ecology`, `climate`, `health_body`,
   `industry`, `companies`, `prices`, `behaviour` (owner V4CORE adds them to `registry/atlas/themes.yaml`).

## Work split (V4 agents)

| Agent | Owns (new files only unless stated) | First sources (open, no account) |
|---|---|---|
| V4CORE | `registry/kg/relations_v4.yaml`, relation-family resolver in `src/reva/kg/families.py`, links/Neurones traversal of families, generic entity page API/UI for any kind, themes additions | — |
| V4CHEM | chemistry & physics: elements, isotopes, compounds, materials composition | PubChem (periodic table, compound summaries via PUG-REST for the compounds other agents reference), ChEBI ontology subset, IAEA LiveChart isotopes, NIST constants, Wikidata P186/P527 |
| V4ENERGY | energy & resources: oil/gas/coal/uranium/renewables, reserves, production, plants, prices, history, discoverers, producers | Energy Institute Statistical Review, Global Energy Monitor trackers (CC BY), WRI Global Power Plant DB, EIA international API, USGS mineral commodity summaries, World Bank Pink Sheet prices, crude oil assays/composition where published |
| V4INDUSTRY | companies, facilities, ownership, supply chains, products | GLEIF LEI (country/sector aggregates + parent relationships for referenced firms), SEC EDGAR company facts (XBRL, US filers), Wikidata companies, OECD TiVA / ICIO, UNIDO INDSTAT (if open), Open Supply Hub |
| V4ECON | macro quarterly/monthly + subnational + prices/micro | IMF data API (IFS/QNA), OECD SDMX (QNA, prices, regional), Eurostat quarterly & NUTS, Global Data Lab subnational indices, World Bank ICP prices, FAO food price index |
| V4BIO | taxonomy, species, genes, proteins | GBIF backbone (API), Catalogue of Life, NCBI taxonomy summaries, HGNC, UniProt reviewed counts per organism, Ensembl summaries |
| V4ECO | ecology & animals: where species live, how many, interactions, threats | GBIF occurrence facets per country/taxon, IUCN (if token absent: Red List summary tables), Living Planet Index, GloBI interactions (eats/parasite-of), WDPA protected areas (V3 nature), FAOSTAT livestock |
| V4CLIMATE | weather & climate space-time series, projections | NOAA GHCN-Monthly / GSOD aggregates, Berkeley Earth country series, NASA POWER, World Bank CCKP (CMIP6 projections per country), NOAA Mauna Loa CO2, sea level (NOAA/CSIRO), Copernicus only if no account needed |
| V4HEALTH | anatomy, diseases at body level, burden, causes | UBERON (anatomy), MONDO/Disease Ontology (disease → anatomy, pathogen), ICD-11 linearization (if API open), WHO GHO (causes of death), IHME GBD (if licence allows), WHO pathogen lists |
| V4MIND | psychology, behaviour, sociology | OECD Better Life/well-being, WHO mental health atlas, World Values Survey aggregates (if downloadable), Global Preference Survey (if open), OpenAlex topic counts by country, time-use (UN/OECD), Pew aggregates where licence allows |
| V4LINK | cross-domain links between the new layers and the V3 world (commodity ↔ trade HS codes ↔ countries ↔ companies ↔ plants ↔ elements; species ↔ ecoregions ↔ countries; disease ↔ deaths ↔ countries) via evidenced identifiers only (HS↔CPC↔ISIC concordances, Wikidata external ids) | UN/WCO concordance tables, Wikidata external-id properties |

Shared files (taxonomy.yaml, themes.yaml, budgets, source_order, link_types.yaml): append only, `git pull
--rebase --autostash` before editing, budgets through `python -m reva.atlas.budget_merge --write` and the
budgets digest re-pin in `src/reva/autonomy/policy.py`. **Never** re-pin the autonomy template digest
(`registry/policies/promotion.yaml` closure, incl. `src/reva/adapters/http/safe_client.py`). Migrations: only
V4CORE may add one (next free number after `0018`).
