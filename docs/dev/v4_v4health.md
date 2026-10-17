# V4 V4HEALTH — human body and diseases: anatomy, diseases, pathogens, genes, burden

Agent V4HEALTH, 2026-10-17. Scope (docs/roadmap/v4_world_model.md): anatomy (UBERON), diseases (Human Disease
Ontology, Mondo), disease → anatomy / pathogen / gene / symptom, ICD cross-references, gene–disease associations,
burden (WHO Global Health Estimates), drugs (WHO EML → compounds, via Wikidata identifiers). Extends, does not
duplicate, A-HEALTHEDU's `atlas.who_gho` (GHE *headline* indicators only — life expectancy, NCD 30-70, suicide —
no cause-of-death breakdown; GHO's own `GHE_*` indicators stop at WHO regions, 2012 vintage).

## Sources

| Atlas source (source key) | What was fetched (live, 2026-10-17) | Bytes archived | Licence |
|---|---|---|---|
| `atlas.uberon` (`uberon.obo`) | `human-view.obo` of UBERON release v2026-10-01 (github.com release asset; data-version `uberon/releases/2026-10-01/subsets/human-view.owl`) | 12,572,132 | CC BY 3.0 (header checked every run) |
| `atlas.doid` (`doid.obo`) | `doid.json` (OBO Graphs JSON; version IRI `…/doid/releases/2026-09-30/doid.owl`). The OBO flat release `doid.obo` has **no** logical axioms (location, pathogen, symptom live only in OWL/JSON) | 24,209,079 | CC0 1.0 (graph meta checked) |
| `atlas.mondo` (`mondo.obo`) | `mondo.obo` release v2026-10-06 | 53,320,617 | CC BY 4.0 (header checked) |
| (all three) | Relation Ontology `ro.obo` (releases/2026-09-04), fetched by every ontology run: relation hierarchy + verbatim definitions | 457,912 (one object) | CC0 1.0 |
| `atlas.clingen` (`clingen.gene_validity`) | ClinGen Gene-Disease Validity summary CSV (`FILE CREATED 2026-10-17`) | 1,130,959 | CC0 1.0 (terms of use: "All curated content published by ClinGen is available free of restriction under the CC0 1.0 Universal (CC0 1.0) Public Domain Dedication") |
| `atlas.who_ghe` (`who.ghe`) | WHO data portal xMart `DEX_CMS/GHE_FULL`. Not run by V4HEALTH (disk below 5 GB); **run live later by the lead**: table `atlas.who.ghe` registered 2026-10-17 14:16 UTC, 620,004 rows (correction FIX4C, AUDIT3 L6) | ~48 MB per run | CC BY 4.0 + WHO terms (same as `who.gho`) |

Raw archive written by V4HEALTH: **91.7 MB** (cap 150 MB); the lead's WHO GHE run added ~48 MB.

Connectors: `src/reva/adapters/connectors/atlas_health_onto.py` (UBERON, DOID, Mondo), `atlas_clingen.py`,
`atlas_who_ghe.py` (reuses `atlas_healthedu_common.fetch_archive_register` of A-HEALTHEDU), shared parsing in
`health_obo_common.py` (strict OBO reader, CURIE/KG ids, Relation Ontology index, causal rule, KG loader). All
fetch through `run.get` (budget-metered, `registry/atlas/budget_requests/v4health.yaml` → budgets-1.3.16, digest
re-pinned). Source registry: `registry/sources/{uberon,doid,mondo,clingen,who_ghe}.yaml`.

## Loaded into the dev DB `reva` (origin `live`)

| Source | Entities | KG statements (current) | Causal claims |
|---|---|---|---|
| UBERON human view | 14,644 (13,194 `anatomical_structure`, 1,450 `cell_type`) | 61,151 (is_a 19,417, part_of 9,456, has_part 612, located_in 145, develops_from 1,775, definitions 12,239, xrefs FMA/MeSH/NCIt/UMLS/SNOMED 17,507) | — |
| Human Disease Ontology | 15,436 (12,335 `disease`; from DOID's import modules: 1,232 `taxon`, 895 `symptom`, 577 `phenotype`, 269 `sequence_feature`, 59 `food_material`, 32 `transmission_process`, 29 `genetic_inheritance_pattern`, 8 `disease_driver`) | 75,918 (is_a 21,232, has_location 1,446, has_symptom 2,263, has_phenotype 215, has_feature 137, onset_during 361, transmitted_by 174, derives_from 420, definitions 10,065, xrefs 39,599 incl. ICD-10-CM 3.7k, ICD-9-CM, ICD-11, OMIM, ORDO, MeSH, UMLS, NCIt, SNOMED, GARD) | **5,082 T1** |
| Mondo | 32,134 `disease` (3,921 obsolete skipped) | 152,899 (is_a 49,653, has_location 2,715, inflammation_site 257, has_feature 1,272, has_characteristic 1,403, in_taxon 2,247, transmitted_by 76, **equivalent_doid 11,817**, definitions 19,505, xrefs 63,897: ICD10WHO, ICD10CM, icd11.foundation, OMIM/OMIMPS, Orphanet, MeSH, UMLS, NCIt) | **9,426 T1** |
| ClinGen | — (genes `hgnc:<n>` = V4BIO, diseases = Mondo) | 3,706 `clingen:gene_disease_validity` (3,059 genes, 2,170 diseases; Definitive 2,311, Strong 84, Moderate 460, Limited 555, Disputed 199, Refuted 49, No Known Disease Relationship 48) | — |
| **Total** | **62,214** | **293,674** | **14,508** |

Zero rejected rows in every load; zero entity conflicts. 33 namespaced predicates registered
(`registry/kg/predicates_health.yaml`). Claims are loaded but **not yet graded**: `reva causal refresh --origin
live` (lead) grades them (T1 + undetermined time → B by grading.yaml v3).

## Examples (ids from the dev DB)

* Tuberculosis: `doid:399` "tuberculosis" — is_a → `doid:0050338` primary bacterial infectious disease → `doid:104` →
  `doid:0050117` → `doid:4` disease; `doid:399 —transmitted_by→ trans:0000008` droplet spread transmission;
  **causal claim** `ncbitaxon:1773` *Mycobacterium tuberculosis* `—has_cause→ doid:399` (T1, DOID axiom
  `IDO:0000664` "has material basis in", codebook definition quoted from doid.json: "Relating the disease to it’s
  causation (disease and disorder).").
* Same disease in Mondo: `mondo:0018076` tuberculosis `—mondo:equivalent_doid→ doid:399` (Mondo's curated
  `MONDO:equivalentTo` xref), xrefs `ICD10WHO:A15-A19`, `icd11.foundation:2072728114`, `Orphanet:3389`; claim
  `ncbitaxon:1773 —has_cause→ mondo:0018076` (T1, `RO:0014001` disease has infectious agent, RO definition quoted).
  Two sources, two claims: they corroborate each other only once V4LINK turns the equivalence into a `same_as`.
* Disease → organ → body: `doid:0060570` cardiac tuberculosis `—has_location→ uberon:0000948` heart
  `—part_of→ uberon:0015410` heart plus pericardium; liver `uberon:0002107 —part_of→ uberon:0002423` hepatobiliary
  system `—part_of→ uberon:0001007` digestive system.
* Malaria: `ncbitaxon:5820` *Plasmodium* `—has_cause→ doid:12365` malaria (T1); `doid:12365 —has_symptom→
  symp:0019174` chills; `mim:609148 —contributing_factor→ doid:12365` (DOID "has major susceptibility factor").
* COVID-19: `ncbitaxon:2697049` SARS-CoV-2 `—has_cause→ mondo:0100096` (T1, Mondo "disease has primary infectious
  agent").
* Gene → disease: `hgnc:4851` (HTT) `—has_cause→ mondo:0007739` Huntington disease (T1, Mondo `RO:0004003` "has
  material basis in germline mutation in", OMIM source annotation kept in the selector) **and**, separately,
  `hgnc:4851 —clingen:gene_disease_validity→ mondo:0007739` classification *Definitive* (association, not a claim);
  `hgnc:1884` (CFTR) → `mondo:0009061` cystic fibrosis, *Definitive*.

## Causal axioms — decision (owner of the rule: V4HEALTH, documented here)

An ontology axiom "disease D — R some X" is an **expert-curated coded relation with a published relation
definition**: the closest V3 tier is **T1** (expert-coded research resource with a codebook), not T2 (that needs a
verbatim DOI/ISBN citation per claim). Rule (`health_obo_common.RelationIndex.is_causal`):

1. R is causal when the Relation Ontology places it under `RO:0002410` "causally related to" (e.g. `RO:0014001`
   disease has infectious agent, `RO:0004003` has material basis in germline mutation in, `RO:0004019` disease has
   basis in and its children, `RO:0001022` has allergic trigger, `RO:0004005` has major susceptibility factor,
   `RO:0004028` realized in response to stimulus), or when it is on the reviewed allow-list of relations the
   ontology defines as causation (`DEFINED_AS_CAUSATION`: DOID `IDO:0000664`, defining phrase checked verbatim).
   **FIX4A (AUDIT3 M9): labels are never read** — the former label-marker rule made `RO:0007001` "has disease
   driver" causal by the word "driver"; it is now the plain link `doid:has_disease_driver` (family context).
   **FIX4A (M1):** `IDO:0000664` with a `GENO:` filler (mode of inheritance) is the KG statement
   `doid:inheritance_mode`, never a claim. **FIX4A (H1):** for "disruption / dysfunction / alteration in structure"
   relations the cause (or, for RO:0004024/0004025, the effect) is a REVA-made node `reva_state:<state>:<filler>`
   ("disruption of GO:0003824"), never the normal process or structure. See docs/dev/v6_fix4a.md.
2. A causal axiom is **never** a KG statement (no predicate in `predicates_health.yaml` is causal; the families
   drop-in maps none). It is one claim per (disease, relation, filler) through `reva.causal.load_dataset_claims`
   (via `causdata_common.deliver_claims`): cause = filler, effect = disease, except "disease causes
   disruption/dysfunction of" (`RO:0004024/0004025`) where the disease is the cause; relation `has_cause`
   (`has_effect` for those two, `contributing_factor` for susceptibility factors).
3. Codebook = the relation's definition quoted verbatim from the archived ro.obo / doid.json (`quote_source`:
   document, sha256, `[Typedef] id: …`). When RO gives none, the text says so explicitly and quotes the nearest
   ancestor's definition (e.g. RO:0004003 → RO:0002410 "relation that links two events, processes, states, or
   objects such that one … (a cause) contributes to the production of another …").
4. A causal relation defined nowhere is **not loaded at all** (Mondo: `predisposes_towards` 501 axioms,
   `disease_triggers` 2, `disease_has_basis_in_development_of` 2, `…accumulation_of` 1 — counted in the run report).

Claims by relation — DOID: has material basis in 4,097 (of which inheritance modes `geno:` 3,751, pathogens
`ncbitaxon:` 284, cell types, SO features, CHEBI), disease has basis in 538, susceptibility factor 263, allergic
trigger 123, disease driver 61. Mondo: germline mutation in (HGNC/NCBI Gene) 7,249, infectious agent 777 + primary
infectious agent 164 + reactivation of latent agent 4, arises from structure 490, basis in disruption 259 /
dysfunction 74, arises from feature 162, causes disruption of 90, realized in response to (stimulus) 132,
gain-of-function / somatic mutation 19.
Caveats (in the claims, not hidden): they are **class-level** ("a disease of this class has its material basis in
…"), not about an outbreak or a patient; DOID models modes of inheritance (`geno:0000148` autosomal recessive 1,975
claims, `geno:0000147` autosomal dominant 1,413) with the same causal relation, so these hubs appear as "causes" —
exactly what the source asserts; the chain engine can only start, never pass, through them. Unknown endpoints (CHEBI
compounds, HGNC genes, MIM entries not yet in the KG): DOID 443 claims, Mondo 4,425 — counted by the loader, filled
when V4CHEM / V4BIO load those ids.

## Gene–disease

* Mondo "has material basis in germline mutation in" is RO-causal → T1 claims (above).
* ClinGen gene-disease validity → `associated_with` only for Definitive / Strong / Moderate / Limited. **FIX4A
  (AUDIT3 H2):** Refuted / Disputed / No Known Disease Relationship are `clingen:gene_disease_curation_negative`
  (classification verbatim, unmapped in the families: never a relation hop). A ClinGen classification grades the *evidence* for a
  gene–disease relationship; REVA does not promote it to a causal claim (owner decision if wanted: the SOP
  definitions would have to be archived and quoted as codebook).
* Ids exactly as published (`HGNC:n`, `MONDO:n`, `http://identifiers.org/hgnc/n`, `…/ncbigene/n`), never symbols.
  ClinGen statement id = the curation's assertion id (`CGGV:assertion_…`, legacy `CGGCIEX:assertion_n`).

## Families (`registry/kg/relations_v4.d/v4health.yaml`)

is_a: `uberon:/doid:/mondo:subclass_of`; part_of / has_part: `uberon:part_of`, `uberon:has_part`, `mondo:part_of`;
located_in: `uberon:located_in`, `doid:has_location`, `mondo:has_location`, `mondo:inflammation_site`;
occurs_during: `doid:onset_during`; associated_with: symptoms, phenotypes, features, `mondo:shares_features_of`,
`mondo:responds_to`, `clingen:gene_disease_validity`, Wikidata P2176 / P2175 (requested); context:
`uberon:develops_from`, `doid:transmitted_by`, `doid:derives_from`, `mondo:transmitted_by`, `mondo:in_taxon`,
`mondo:has_characteristic`. Unmapped on purpose: definitions, xrefs (literals) and `mondo:equivalent_doid`
(identity, for V4LINK `same_as`). `uv run python -m reva.kg.families check` passes.

## Burden: WHO Global Health Estimates (`atlas.who_ghe`, table `who.ghe`)

Indicators `ghe.{deaths,deaths_rate100k,dalys,dalys_rate100k}|sex=TOTAL|age=TOTAL|cause=<GHE code>`, per country
(ISO3, 183 countries) and year (2000–2023 in the 2026-09 vintage): the 27 cause groups of levels 0–2 every year,
all 226 causes (levels 0–5) for the latest year. Death counts carry WHO's uncertainty bounds (`flags.low/high`,
level not stated per row); a null is Missing, never zero. Catalog `registry/atlas/catalog/health_body.yaml`: 54
concepts (deaths and death rates of the 27 groups; French labels are REVA translations of WHO's English titles).
GHE causes are **not** linked to DOID/Mondo: the API publishes no ICD code per cause and REVA never matches by
name; the evidenced bridge is the ICD-10 code list per GHE cause in the WHO methods annex (CC BY-NC-SA 3.0 IGO —
owner decision before archiving it) joined to the ICD-10 xrefs already loaded from DOID/Mondo (V4LINK).

## Drugs, vaccines, Wikidata bridges (specs, not harvested: WDQS throttled)

`registry/kg/queries/health/wikidata/`: `disease_ontology_ids` (P699), `mondo_ids` (P5270), `uberon_ids` (P1554) —
the identifier bridges Wikidata QID ↔ `doid:`/`mondo:`/`uberon:` entities (V4LINK joins them; that also links the
V3 `science.vaccines` items: vaccine `—P1924 vaccine for→` disease Q `—P699→ doid:n`);
`disease_treatments_symptoms` (P2176, P780 for items with P699); `essential_medicines` (seeds items pointing to the
item labelled "WHO Model List of Essential Medicines"; their PubChem CID P662 / ChEBI P683 claims — requested by
V4CHEM — are the hand-off to V4CHEM compounds by identifier). Property requests:
`registry/kg/property_requests/v4health.yaml` (P699, P5270, P1554, P2176, P2175, P494, P7807). The EML anchor is
selected by its exact English label because its QID could not be resolved (a `harvest try` timed out on the
machine-wide Wikidata lock); WDHARVEST should pin the QID after the first run.

## Missing, and why

* **WHO GHE live load** — done after this report by the lead (`atlas.who.ghe`, 620,004 rows, 2026-10-17 14:16 UTC);
  V4HEALTH itself had stopped at 4.8 GB free disk (< 5 GB rule). Command below for refreshes.
* **IHME GBD** — skipped: IHME Free-of-Charge Non-commercial User Agreement (no redistribution, commercial use
  forbidden) and the GBD Results tool needs an account. Owner decision.
* **WHO priority pathogen lists** (bacterial BPPL 2024, fungal FPPL 2022, R&D Blueprint) — published only as PDF
  reports with pathogen *names*; linking them to `ncbitaxon:` ids would be name matching. Needs a reviewed manual
  crosswalk (name → NCBI Taxonomy id) by a maintainer, then a small connector archiving the PDF. Owner decision.
* **ICD-11 linearization API** — requires WHO ICD API OAuth credentials (registration). ICD-11 *foundation* ids come
  from Mondo xrefs (icd11.foundation) instead; DOID has 5 ICD11 xrefs.
* **HPO as an ontology** — not loaded (HPO licence: free use with attribution, ontology not to be modified;
  needs an owner licence review). The 577 HP classes DOID imports are loaded with their labels as DOID imported
  them (flagged here: labels are HPO content).
* **WHO EML as a list** — no open machine-readable EML export without the eEML web app; covered by the Wikidata
  spec above.
* UBERON relations other than is_a/part_of/has_part/located_in/develops_from (adjacent_to, connected_to,
  innervated_by, vessel_supplies_blood_to, …) are counted, not loaded (no family yet); UBERON xrefs kept: FMA, MeSH,
  NCIt, UMLS, SNOMED; Mondo xrefs dropped: MedGen, GARD, SCTID, ICD9, EFO, NANDO, OMIA, VeNom, ….
* DALY uncertainty bounds of GHE not requested (raw cap); GHE age/sex breakdowns and level 3–5 causes for years
  before the latest one wait for a bigger machine.
* Taxa: the 1,232 `ncbitaxon:` entities are DOID's imported labels. If V4BIO loads `ncbitaxon:` ids too, the
  later load becomes the entity's current revision (no conflict, same id); pathogens referenced only by Mondo stay
  unlabeled until then.

## Owner decisions

1. GBD (non-commercial licence) — load or not. 2. HPO licence review. 3. Reviewed pathogen-list → NCBI Taxonomy
crosswalk. 4. Archiving the WHO GHE methods annex (CC BY-NC-SA 3.0 IGO) for the ICD-10 ↔ GHE-cause bridge. 5. Whether
ClinGen *Definitive/Strong* classifications should become T1 claims (requires archiving the ClinGen SOP as codebook).
6. ~~Whether DOID inheritance-mode axioms (`geno:` causes) should be kept as claims~~ — resolved by AUDIT3 M1 /
FIX4A: they are not causes; KG statement `doid:inheritance_mode` (docs/dev/v6_fix4a.md).

## Tests

`tests/unit/test_v4health.py` (13 tests: OBO reader, CURIEs, RO causal rule, no causal predicate, families, UBERON,
licence drift, DOID locations/symptoms/claims/codebook, Mondo equivalences/claims, ClinGen ids incl. legacy, GHE
rows/flags/Missing/follow-ups/drift), `tests/integration/test_v4health_ingest.py` (all five sources in fixture mode
on a throwaway DB: KG, T1 claims with quoted codebook, ClinGen, GHE atlas table, idempotence). Fixtures (trimmed
real bytes, licences in each manifest): `tests/fixtures/source/atlas.{uberon,doid,mondo,clingen,who_ghe}`.

## Commands to finish later (bigger machine / free disk ≥ 5 GB)

```bash
df -h /                                                         # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed                     # if connector versions changed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.who_ghe   # ~186 requests, ~48 MB raw
flock /tmp/reva-devdb.lock uv run reva causal refresh --origin live # grade the 14,508 ontology T1 claims (lead)
uv run python -m reva.kg.harvest merge-property-requests          # WDHARVEST: P699 P5270 P1554 P2176 P2175 P494 P7807
uv run python -m reva.kg.harvest run registry/kg/queries/health --via api
# monthly refresh (retracts what a new release drops):
for s in atlas.uberon atlas.doid atlas.mondo atlas.clingen; do flock /tmp/reva-devdb.lock uv run reva atlas ingest $s; done
```
