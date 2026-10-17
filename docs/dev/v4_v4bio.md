# V4 V4BIO — tree of life, genes, proteins, genomes

Agent V4BIO (2026-10-17). Brief: taxonomy backbone (GBIF backbone, Catalogue of Life counts, NCBI taxon ids only
through published mappings, fr/en common names), human genes (HGNC), proteins (UniProt reviewed), genome summaries
(Ensembl) and the links gene → encodes → protein, gene → located_in → chromosome, species → is_a → genus → …

Everything is KG (entities + statements, `load_batch`, append-only); no atlas table (nothing here is per country).

## Code and registry files

| File | What |
|---|---|
| `src/reva/adapters/connectors/atlas_bio_taxonomy.py` | sources `atlas.gbif_backbone`, `atlas.col_checklist`, `atlas.bio_wikidata` |
| `src/reva/adapters/connectors/atlas_bio_molecular.py` | sources `atlas.hgnc`, `atlas.uniprot`, `atlas.ensembl_genomes`, `atlas.ncbi_genomes` |
| `src/reva/adapters/connectors/bio_common.py` | predicate loader, bounded KG loader with retraction, typed literals |
| `registry/kg/predicates_bio.yaml` | 39 namespaced predicates (`gbif:`, `col:`, `bio:`, `hgnc:`, `uniprot:`, `ensembl:`, `ncbi:`) |
| `registry/kg/relations_v4.d/v4bio.yaml` | family mappings (10 entity-valued predicates) |
| `registry/bio/taxon_scope.yaml` | REVA's selection: species groups with full lists + 178 curated scope species |
| `registry/sources/{gbif_backbone,col_checklistbank,hgnc_complete_set,uniprot_rest,ensembl_rest,ncbi_datasets}.yaml` | licences, hosts |
| `registry/atlas/budget_requests/v4bio.yaml` | merged as `budgets-1.3.22` (digest re-pinned in `src/reva/autonomy/policy.py`) |
| `tests/unit/test_bio_v4.py`, `tests/integration/test_bio_v4_ingest.py` | 14 offline tests + 1 DB test (all 7 sources in fixture mode, paths) |
| `tests/fixtures/source/atlas.{gbif_backbone,col_checklist,bio_wikidata,hgnc,uniprot,ensembl_genomes,ncbi_genomes}/` | trimmed live captures (licence + trimming note per manifest entry) |

## Entity keys and kinds

| key | kind | authority |
|---|---|---|
| `gbif:<key>` | taxon | GBIF Backbone Taxonomy 2023-08-28, integer keys (= Wikidata P846, = V4LINK's `gbif:` prefix) |
| `col:<id>` | taxon | Catalogue of Life release (ChecklistBank 3LR, e.g. `col:6224G` Mammalia) |
| `col:release.<key>` | dataset | the COL release itself (metrics) |
| `ncbitaxon:<id>` | taxon | NCBI Taxonomy id as UniProt / Ensembl / NCBI Datasets state it (= V4HEALTH / V4LINK prefix) |
| `hgnc:<n>` | gene | HGNC:<n> (= V4HEALTH ClinGen / V4LINK prefix) |
| `uniprot:<acc>` | protein | UniProtKB/Swiss-Prot accession |
| `assembly:<GCA_…/GCF_…>` | genome_assembly | INSDC / RefSeq assembly accession |
| `chromosome:<taxid>:<name>` | chromosome | **REVA-composed** (NCBI taxon id + Ensembl karyotype seq-region name), labelled so |

## Sources (all open, no account)

| Source (atlas source / source key) | URL | Licence | What is fetched |
|---|---|---|---|
| GBIF Backbone Taxonomy (`atlas.gbif_backbone` / `gbif.backbone`) | api.gbif.org/v1 (dataset record, `/species/{key}`, species-search facets) + api.checklistbank.org dataset 53147 (same backbone, GBIF keys as ids; `gbifKey` and version checked against GBIF on every run) | CC BY 4.0 | accepted usages of rank kingdom, phylum, class, order, family (ChecklistBank search, 1,000/page); accepted species of the species groups (default Mammalia) with their genera; 178 curated scope species (`/species/{key}`); accepted-species counts from the `higherTaxonKey` facet (rank=SPECIES, status=ACCEPTED), partitioned because GBIF lists at most 50,000 facet values and refuses `facetOffset` |
| Catalogue of Life (`atlas.col_checklist` / `col.checklistbank`) | api.checklistbank.org `dataset/3LR`, `dataset/{key}/import`, name-usage search | CC BY 4.0 | release record (alias, DOI, issued), import metrics (taxa/names by rank, extinct taxa by rank, synonyms, vernacular names...), accepted taxa of rank domain/realm/kingdom/phylum/class, `TAXON_ID` facet counts of accepted species (all, and `extinct=false`) |
| Wikidata (`atlas.bio_wikidata` / `wikidata.wdqs`, community-edited) | query.wikidata.org through WDHARVEST's `WikidataClient` (machine-wide lock, archive) | CC0 | P846 (GBIF key), P685 (NCBI taxon), P10585 (COL id), P1843 (common names fr/en): 5 per-rank queries, then VALUES chunks (scope species, NCBI ids of genomes / organisms with >= 100 Swiss-Prot entries, the other loaded species, the other NCBI ids) up to `max_queries` |
| HGNC (`atlas.hgnc` / `hgnc.complete_set`) | storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt | CC0 ("all data is released under CC0", genenames.org/about/license) | the complete set (one request); md5 = Cloud Storage ETag checked |
| UniProtKB (`atlas.uniprot` / `uniprot.rest`) | rest.uniprot.org streams with field selection; ftp.uniprot.org `reldate.txt` (before and after: the release must not change) | CC BY 4.0 | organism id of every reviewed entry (exact counts per NCBI taxon id); UniProt taxonomy records of those organisms (100 ids per request: the API refuses more OR conditions); reviewed human entries (accession, entry name, primary gene, protein names, length, function comment, HGNC xref) |
| Ensembl (`atlas.ensembl_genomes` / `ensembl.rest`) | rest.ensembl.org `info/species` (vertebrates), `info/assembly/{homo_sapiens,mus_musculus}` | Ensembl: no restriction | assembly (INSDC GCA accession) used per species; karyotypes with chromosome lengths |
| NCBI Datasets (`atlas.ncbi_genomes` / `ncbi.datasets`) | api.ncbi.nlm.nih.gov/datasets/v2 genome dataset reports, `reference_only` + `has_annotation`, taxa 7742 (Vertebrata) and 33090 (Viridiplantae) | U.S. Government work, no restriction | annotation gene counts per category (total, protein_coding, non_coding, pseudogene, other), assembly name/level/length, RefSeq↔INSDC pairing |

Why these choices (measured on 2026-10-17):

* GBIF's species search returns 3-10 kB per usage (descriptions and vernacular names in every language: 66 % +
  22 % of the bytes for mammals); ChecklistBank's search of the same backbone returns ~1.3-1.5 kB. Listing the
  27,016 accepted families through GBIF would have cost ~80 MB of raw archive, through ChecklistBank ~33 MB.
  Counts stay GBIF's own (GBIF API facet), never recomputed.
* Ensembl REST publishes no per-species gene counts and Ensembl BioMart answers 308 to an archive host (retired);
  gene counts therefore come from NCBI Datasets annotation reports, attached to the RefSeq assembly, which NCBI
  pairs with the INSDC assembly Ensembl uses (`ncbi:paired_assembly`: identifiers, not names).
* UniProt's release headers (`X-UniProt-Release`) are not kept in archived response metadata (SafeHttpClient keeps
  a fixed header list and must not be edited), so the release is read from `reldate.txt`, archived twice.
* The GBIF backbone served by GBIF's API is still the 2023-08-28 backbone (integer keys, Wikidata P846, which
  Wikidata now labels "before 2026 update"); Wikidata P14607 "GBIF taxon ID (2026 backbone)" holds COL-XR-style ids
  (2,701 items on 2026-10-17). REVA keys `gbif:` stay the integer backbone keys (same as V4LINK's identity rules).

## Relation families used (registry/kg/relations_v4.d/v4bio.yaml)

| predicate | family | example |
|---|---|---|
| `gbif:parent`, `col:parent` | `is_a` | Puma concolor (gbif:2435099) is_a Puma (gbif:2435098) is_a Felidae (gbif:9703) is_a Carnivora (gbif:732) is_a Mammalia (gbif:359) is_a Chordata (gbif:44) is_a Animalia (gbif:1) |
| `hgnc:encodes` / `uniprot:encoded_by` | `produces` / `produced_by` | TP53 (hgnc:11998) produces "Cellular tumor antigen p53" (uniprot:P04637); P04637 produced_by hgnc:11998 (each source's own cross-reference) |
| `hgnc:located_on_chromosome` | `located_in` | hgnc:11998 located_in chromosome 17 of Homo sapiens (chromosome:9606:17); SHOX located_in X and Y (pseudo-autosomal "Xp22.33 and Yp11.32") |
| `ensembl:has_chromosome` | `has_part` | GRCh38.p14 (assembly:GCA_000001405.29) has_part chromosome:9606:17 (length_bp qualifier) |
| `ensembl:assembly_of`, `ncbi:assembly_of`, `hgnc:in_taxon`, `uniprot:in_taxon` | `context` | assembly:GCA_000001405.29 context ncbitaxon:9606; uniprot:P04637 context ncbitaxon:9606 |
| `bio:wikidata_item`, `bio:same_taxon_via_wikidata`, `ncbi:paired_assembly` | (unmapped: identity) | ncbitaxon:9606 names the same taxon as gbif:2436436 because one Wikidata item holds both P685 9606 and P846 2436436 |

Literal predicates: ranks, authorships, species counts (`gbif:accepted_species_count`, `col:species_count`,
`col:release_metric`), HGNC symbol/name/locus type/group/cytogenetic location/Ensembl and NCBI Gene ids, UniProt
gene name/length/function (verbatim, cut at 1,200 characters with `truncated` + `characters_in_source`)/reviewed
counts, Ensembl assembly name/genebuild/golden path, NCBI gene counts/assembly name/level/length, Wikidata common
names (fr/en, verbatim). No predicate is causal.

LIVE_RESULTS_PLACEHOLDER

## Live loads in the dev DB `reva` (2026-10-17, origin `live`)

| Source (connector version) | Requests | Raw archived (distinct) | Entities | Statements | Notes |
|---|---|---|---|---|---|
| `atlas.hgnc` (1.0.0) | 1 | 17.0 MB | 45,277 genes | 424,384 | complete set 2026-10-16, md5 = ETag checked; 19,299 protein-coding, 10,304 ncRNA, 14,671 pseudogenes, 1,003 other; 20,377 `hgnc:encodes`; 36 locations name no chromosome (19 empty, 8 reserved, 6 unplaced, 3 not on reference assembly) |
| `atlas.uniprot` (1.0.1) | 154 | 20.1 MB | 14,919 `ncbitaxon:` + 20,431 proteins | 113,787 | release 2026_03 (2026-09-02); 575,748 reviewed entries in 14,919 organisms (human 20,431, mouse 17,283, Arabidopsis 16,421, rat 8,233, yeast 6,733); 17,341 function texts; 20,382 `uniprot:encoded_by` |
| `atlas.ensembl_genomes` (1.0.1) | 3 | 0.1 MB | 343 assemblies, 47 chromosomes, 99 new `ncbitaxon:` | 737 | release 116, 356 vertebrate species (13 without INSDC accession: not loaded); karyotypes human (25) and mouse (22) |
| `atlas.ncbi_genomes` (1.0.1) | 5 | 7.7 MB | 1,922 assemblies (+ new taxa) | 16,530 | reference annotated genomes: 1,364 Vertebrata + 558 Viridiplantae; 7,748 gene-count statements; 1,094 RefSeq↔INSDC pairings |
| `atlas.col_checklist` (1.0.2) | 9 | 4.3 MB | 649 (648 taxa + release) | 1,912 | COL26.10 (2026-10-14, doi:10.48580/dgzh4): 3 domains, 10 realms, 24 kingdoms, 146 phyla, 465 classes; 602 species counts (facet top 50,000: 237 small taxa without a count); 28 release metrics |
| `atlas.gbif_backbone` (1.0.2) | 161 (+800 by 1.0.1, see below) | 78.8 MB | 58,785 taxa | 175,327 | 8 kingdoms, 257 phyla, 749 classes, 2,791 orders, 27,015 families, 21,100 Mammalia species + 6,740 genera, 170/178 scope species; 36,641 species counts from 101 facet partitions (4 truncated: root, Animalia, Arthropoda, Insecta); 6,144 taxa stated 0 (absent from an exhaustive partition), 920 without count (only under truncated partitions) |
| `atlas.bio_wikidata` (1.0.2) | 0 answered | 0 | — | 0 | **not loaded**: WDQS answered `429 ... 1 req / min ... active wdqs outage` with `Retry-After: 1000`; two attempts (the first lost to a container restart, the second stopped after 20 min so as not to hold the shared dev-DB lock while cooling down). Code + fixture test pass; run the command below when WDQS is healthy |
| **Total** | 333 | **128.0 MB** (≤ 150 MB bound; 631 distinct raw artifacts) | **~165k** | **732,677** | rejected statements: 0 in every load |

Examples (ids in the dev DB):

* **Tree of life**: Puma concolor `gbif:2435099` → is_a → Puma `gbif:2435098` (3 accepted species) → is_a → Felidae
  `gbif:9703` (205) → is_a → Carnivora `gbif:732` → is_a → Mammalia `gbif:359` (21,100 accepted species, fossil
  and extant) → is_a → Chordata `gbif:44` → is_a → Animalia `gbif:1` (1,822,234).
* **Gene → protein → organism**: TP53 `hgnc:11998` → produces (HGNC `uniprot_ids`) → Cellular tumor antigen p53
  `uniprot:P04637` → produced_by (UniProt HGNC xref) → `hgnc:11998`; P04637 → context → Homo sapiens
  `ncbitaxon:9606` (20,431 Swiss-Prot entries).
* **Gene → chromosome → genome**: `hgnc:11998` → located_in → chromosome 17 `chromosome:9606:17` ← has_part ←
  GRCh38.p14 `assembly:GCA_000001405.29` (Ensembl, length in bp as qualifier) ← NCBI pairing ←
  `assembly:GCF_000001405.40` (annotation GCF_000001405.40-RS_2025_08 of 2025-08-01: 59,792 genes total, 20,076
  protein-coding, 22,235 non-coding, 17,070 pseudogenes, 411 other).
* **Refused scope entries** (the check working): 8 pathogen keys from GBIF's name match (e.g. 11286021 for
  "Escherichia coli") are, in the backbone, GTDB cluster names ("Escherichia coli_E", "Acinetobacter baumannii_B"):
  the scientific name differs from the scope entry, so they are not loaded (listed in the run report).

GBIF budget note: connector 1.0.1 opened one facet partition per family whose count the parent partition did not
list (thousands of families with no accepted species), consumed the whole 800-request budget and loaded nothing
(its 458 archived responses are the same bytes 1.0.2 re-read). 1.0.2 opens partitions on kingdoms..orders only.
Facet values of a partition's *ancestors* are partial counts (restricted to the partition): the run keeps the
first value seen, which (breadth-first from the root) is always the full count, and lists the later partial
values under `counts.conflicts` in the run report (they are not conflicts between sources).

## Hand-offs

* **V4HEALTH** — gene–disease associations are yours, as `associated_with` (never causal): key genes as
  `hgnc:<n>` (numeric part of HGNC:<n>, 45,277 genes loaded), proteins as `uniprot:<acc>`, taxa as `ncbitaxon:<id>`
  (14,919 organisms with Swiss-Prot entries have entities) — the same prefixes as your ClinGen / Mondo loaders.
  Pathogens of `registry/bio/taxon_scope.yaml` (group `pathogens`, 46 accepted backbone species) carry GBIF keys;
  their NCBI ids reach them only through `bio:same_taxon_via_wikidata` (Wikidata P685 + P846 on one item).
* **V4ECO** — species distributions are yours: GBIF taxon keys of every loaded taxon are the `gbif:<key>` ids
  (kingdom..family, all accepted Mammalia species and genera, the 178 scope species: crops, livestock, pathogens,
  model organisms, iconic / threatened). `gbif:accepted_species_count` gives the backbone's species count per group.
  IUCN threat statuses are not loaded here (IUCN Red List data on GBIF is CC BY-NC; your IUCN source decides).
* **V4LINK** — identity rules already use `gbif:` (P846 legacy backbone), `ncbitaxon:` (P685), `hgnc:` (P354):
  my entities match them. `bio:wikidata_item` / `bio:same_taxon_via_wikidata` (mine) cover taxa your bounded pack
  (>= 30 sitelinks) leaves out; both can coexist (different predicates reading the same Wikidata statements).

## What is missing, and why

* **Species lists beyond Mammalia** (Aves 14,641, Amphibia 9,815, reptiles ~14,200, fishes) are declared in
  `registry/bio/taxon_scope.yaml` with `load: false`: the raw archive is bounded at 150 MB on this machine
  (ChecklistBank returns ~1.5 kB per species: Aves alone ~22 MB). Command below.
* **Extant vs fossil**: the GBIF backbone (2023-08-28) mixes fossil and extant species (Mammalia: 21,100 accepted
  species, of which GBIF flags 6,323 extant and 12,917 extinct, the rest unflagged); counts are stated with that
  definition. COL counts are given both ways (all / `extinct=false`).
* **Genus/species counts under truncated facet partitions**: GBIF lists at most 50,000 facet values per query;
  families under a truncated order partition with few species get no count statement (listed in the run report,
  `counts.missing_sample`), never a zero.
* **Gene counts from Ensembl**: not published by the REST API (BioMart retired) -> NCBI annotation reports.
* **Wikidata bridges are partial**: WDQS answered `429 Aggressively rate-limiting to 1 req / min - this rule was
  created during active wdqs outage` on 2026-10-17 (shared egress); the run keeps what was answered and never
  retracts on an incomplete run (`complete: false`).
* **Common names**: only Wikidata P1843 (fr/en). GBIF's own vernacular names are per source checklist and cost one
  request per taxon (not fetched); COL vernacular names (190k) would need the ColDP export (login required).
* **Threat status / IUCN**: not loaded (V4ECO; NC licence).
* **Withdrawn HGNC symbols, non-human genes**: only the human approved complete set (HGNC scope).

## Owner decisions

* **GBIF 2026 backbone**: GBIF announced a COL-XR-based backbone (Wikidata P14607, alphanumeric ids, 2,701 items
  on 2026-10-17) while api.gbif.org still serves the 2023-08-28 integer-key backbone. When GBIF switches its API,
  decide whether `gbif:` keys stay the legacy keys (and a `gbif2026:` scheme is added, V4LINK's inactive rule) or
  are re-keyed.
* **Raw archive budget**: loading all vertebrate species needs ~+80 MB (see command below).
* **Budget**: `gbif.backbone` was raised to 1,200 requests/day after the 1.0.1 run spent 800 requests opening a
  facet partition per family (fixed in 1.0.2). The 78.5 MB that run archived are the same pages the 1.0.2 run reads
  (content-addressed, not stored twice).

## Commands (resume / bigger machine)

```bash
df -h /                                                   # >= 5 GB free first
flock /tmp/reva-devdb.lock uv run reva seed              # registers atlas_bio_* connector versions
for s in atlas.hgnc atlas.uniprot atlas.ensembl_genomes atlas.ncbi_genomes atlas.col_checklist \
         atlas.gbif_backbone atlas.bio_wikidata; do
  flock /tmp/reva-devdb.lock uv run reva atlas ingest $s
done
# all vertebrate species (bigger machine, ~+80 MB raw): a partial run never retracts
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gbif_backbone --option groups=212,131,11592253,11418114,11493978,11569602,121,204
# more Wikidata bridges when WDQS is healthy (budget wikidata.wdqs 1,500/day)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.bio_wikidata --option max_queries=400
# offline checks
uv run pytest tests/unit/test_bio_v4.py tests/integration/test_bio_v4_ingest.py
uv run python -m reva.kg.families check
```
