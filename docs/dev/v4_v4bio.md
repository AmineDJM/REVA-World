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
| UniProtKB (`atlas.uniprot` / `uniprot.rest`) | rest.uniprot.org streams with field selection; ftp.uniprot.org `reldate.txt` (before and after: the release must not change) | CC BY 4.0 | organism id of every reviewed entry (exact counts per NCBI taxon id); UniProt taxonomy records of those organisms (200 ids per request); reviewed human entries (accession, entry name, primary gene, protein names, length, function comment, HGNC xref) |
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
