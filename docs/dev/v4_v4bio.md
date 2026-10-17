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

LIVE_RESULTS_PLACEHOLDER
