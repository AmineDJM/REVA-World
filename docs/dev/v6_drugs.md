# V6 V6DRUGS — medicines: essential medicines, drugs, targets, indications, adverse-event reports, trials

Agent V6DRUGS, 2026-10-17. Scope (docs/roadmap/v6_plan.md): a sourced medicines layer linked to V4 chemistry
(ChEBI), diseases (Mondo/DOID) and genes/proteins (UniProt → HGNC) through **published identifiers only**.

Rules held throughout: an indication or an essential-medicine listing is an **association** (family
`associated_with`), never a causal claim and never an efficacy estimate; a mechanism of action is "acts on a target"
(`interacts_with`, subtype `targets`); adverse-event counts are counts of **reports**, not of drug-caused events
(FDA's own caveat quoted on every value); counts the provider does not return are Missing (no statement), never 0.

## Sources

| Atlas source (source key) | What is fetched | Licence (per value) | Live run 2026-10-17 |
|---|---|---|---|
| `atlas.chembl_drugs` (`chembl.api`) | ChEMBL API: `status.json` (release pinned, re-checked at the end), `molecule.json?max_phase=4` (5 pages, selected fields), `mechanism.json?max_phase=4` (4 pages), `target.json?target_chembl_id__in=` (scalar fields, chunks of 100), `drug_indication.json?max_phase_for_ind=4` (9 pages, with references); FTP `ChEMBLdb/latest/chembl_uniprot_mapping.txt` (header release must equal the API release) | **CC BY-SA 3.0** (ShareAlike flagged); ChEMBL ids preserved and release `ChEMBL_37` on every statement as `REQUIRED.ATTRIBUTION` asks; ATC codes = WHOCC material, flagged `licence_noncommercial` | 30 requests, 16.9 MB |
| `atlas.unichem` (`unichem.mapping`) | UniChem whole-source mappings ChEMBL→ChEBI (src7), →DrugBank (src2), →FDA SRS/UNII (src14) | EMBL-EBI terms, no licence file → `licence_review`; only identifier pairs (same standard InChI); DrugBank content (CC BY-NC) not fetched | 3 requests, 0.9 MB |
| `atlas.who_eml` (`who.eeml`) | WHO eEML list page, every medicine page, every recommendation page (HTML; the XLSX export has no ids) | **CC BY 3.0 IGO** with WHO's restrictions (no advertising/promotion use, no endorsement suggestion, no logo) and attribution/adaptation disclaimer | EML_RUN |
| `atlas.openfda` (`openfda.api`) | openFDA `count` aggregations only: FAERS reports per year × occurrence country (2004–2026), recall enforcement reports per year × class (2012–2026), top-100 substances by FAERS reports, top-1,000 by labels, substances in recalls, serious / non-serious report counts per substance in scope | **CC0 1.0**; FDA caveat quoted verbatim on every FAERS value | FDA_RUN |
| `atlas.ctgov` (`ctgov.api`) | ClinicalTrials.gov API v2: `/version`, `/stats/field/values` (7 fields, top 250 values), study **counts** (`countTotal`, one NCT id returned) per country × phase 1–4 and per condition MeSH × phase 1–4 | public domain (NLM) with ClinicalTrials.gov terms → `licence_review` | CT_RUN |
| WHO ATC/DDD index | **not fetched**: WHOCC terms "Copying and distribution for commercial purposes is not allowed. Changing or manipulating the material is not allowed." ATC *codes* appear only as published inside ChEMBL / eEML records, flagged non-commercial; no ATC level names or DDDs are loaded | — | — |

Raw archive written by V6DRUGS: RAW_TOTAL (cap 100 MB). Budgets: `registry/atlas/budget_requests/v6drugs.yaml`
(merged: chembl.api 120, unichem.mapping 10, who.eeml 2000, openfda.api 900, ctgov.api 2200 per day).

## Code and registry files

| File | What |
|---|---|
| `src/reva/adapters/connectors/atlas_drugs_chembl.py` | ChEMBL drugs, mechanisms, targets (+ UniProt components), approved indications |
| `src/reva/adapters/connectors/atlas_drugs_unichem.py` | ChEMBL ↔ ChEBI / DrugBank / UNII crosswalk (scope: approved drugs already in the KG) |
| `src/reva/adapters/connectors/atlas_drugs_eml.py` | WHO eEML medicines, recommendations (ICD-11), sections, DrugBank bridge to ChEMBL |
| `src/reva/adapters/connectors/atlas_drugs_openfda.py` | FAERS / labels / recalls counts (atlas + KG) |
| `src/reva/adapters/connectors/atlas_drugs_ctgov.py` | registry counts (atlas) + MeSH aggregate entities (KG) |
| `src/reva/adapters/connectors/drugs_common.py` | predicates loader, bounded retracting KG loader, literals, stdlib HTML tree |
| `registry/kg/predicates_drugs.yaml` | 33 namespaced predicates (`chembl:`, `unichem:`, `eml:`, `openfda:`, `ctgov:`), none causal |
| `registry/kg/relations_v4.d/v6drugs.yaml` | 16 family mappings (`reva.kg.families check` passes) |
| `registry/atlas/geo_aliases/v6drugs.yaml` | scheme `ctgov_country`: 203 exact name equalities with the crosswalk, 23 names left unmapped |
| `registry/sources/{chembl_api,unichem_mapping,who_eeml,openfda_api,ctgov_api}.yaml` | licences, hosts, release checked |
| `tests/unit/test_v6_drugs.py` (9), `tests/integration/test_v6_drugs_ingest.py` (1, all five sources in fixture mode on a throwaway DB, paths, idempotence) | |
| `tests/fixtures/source/atlas.{chembl_drugs,unichem,who_eml,openfda,ctgov}/` | real captures (scope: aspirin, imatinib + mesylate, metformin, abatacept, nicotine; eEML atropine, ASA, imatinib, metformin, NRT; France/Nigeria; MeSH D003924/D008288); trimmed files carry the sha256 of the full response in `fixture_note` |

## KG kinds and relation families

| kind | key | source |
|---|---|---|
| `drug` | `chembl:CHEMBL<n>` (approved molecule), `eml:medicine:<n>` (eEML medicine) | ChEMBL, eEML |
| `drug_target` | `chembl:CHEMBL<n>` (target id) | ChEMBL |
| `clinical_trial` (aggregate only) | `ctgov:condition_mesh:<D…>`, `ctgov:intervention_mesh:<D…>` — "studies registered whose NLM-derived MeSH terms include …"; counts only, no trial record | ClinicalTrials.gov |
| `drug_substance` | `unii:<code>` (label composed from the code, said so) | UniChem / openFDA |
| `mesh_descriptor` | `mesh:<D…>` labelled with the heading ChEMBL publishes | ChEMBL |
| `disease_or_phenotype` | `efo:`, `hp:`, `orphanet:`, `go:` indication terms labelled with ChEMBL's `efo_term` (Mondo/DOID terms are V4HEALTH's entities, never re-created) | ChEMBL |
| `icd11_category` | `icd11mms:<code>` labelled with the eEML indication text, ICD entity URI as literal | eEML |
| `eml_section`, `essential_medicines_list` | `eml:section:<id>`; `eml:list:who_eml`, `eml:list:emlc` (REVA-composed, said so) | eEML |

| predicate | family | note |
|---|---|---|
| `chembl:mechanism_target` | `interacts_with` (subtype `targets`) | action type and mechanism text verbatim, references; **family request to V4CORE**: a dedicated `targets` family (not blocking) |
| `chembl:target_component` | `has_part` | target → `uniprot:<acc>` (V4BIO entities) |
| `chembl:target_organism` | `context` | → `ncbitaxon:<id>` |
| `chembl:parent_molecule` | `context` | salt/form → parent (ChEMBL hierarchy) |
| `chembl:indication`, `chembl:indication_mesh` | `associated_with` (subtype `indicated_for`) | max_phase_for_ind, references (DailyMed, EMA, FDA, ClinicalTrials ids) |
| `unichem:chebi`, `unichem:fda_unii` | `context` | identifier bridges (same standard InChI) |
| `eml:on_list`, `eml:in_section`, `eml:section_parent` | `part_of` | |
| `eml:recommended_for` | `associated_with` | WHO listing for an indication (ICD-11) |
| `eml:therapeutic_alternative`, `eml:chembl_molecule` | `context` | square-box alternatives; eEML DrugBank link = UniChem DrugBank id of exactly one approved ChEMBL drug (ambiguous → no link, counted) |
| `ctgov:condition_mesh`, `ctgov:intervention_mesh` | `context` | aggregate → `mesh:` |

Literal predicates (values, unmapped): molecule type, first approval year, max phase, ATC codes, route / boxed
warning / withdrawn flags, target type, DrugBank ids, eEML medicine type, status history (year + TRS), ICD-11 URI,
FAERS / label / recall counts, study counts.

## Loaded (live, dev DB `reva`, origin `live`)

LOADED_TABLE

## Examples (live ids)

* **Imatinib, from the WHO list to the gene**: `eml:medicine:95` (Imatinib) —`eml:chembl_molecule` (DrugBank
  DB00619 = UniChem)→ `chembl:CHEMBL941` (IMATINIB) ←`chembl:parent_molecule`— `chembl:CHEMBL1642` (imatinib
  mesylate, the approved form ChEMBL attaches mechanisms to) —`chembl:mechanism_target` (INHIBITOR, "Tyrosine-protein
  kinase ABL inhibitor")→ `chembl:CHEMBL1862` (Tyrosine-protein kinase ABL1) —`chembl:target_component`→
  `uniprot:P00519` —`uniprot:encoded_by` (V4BIO)→ `hgnc:76` (ABL1). Also → `chembl:CHEMBL2096618` Bcr/Abl fusion
  protein (components P00519 + P11274), PDGFRβ `CHEMBL1913`, KIT `CHEMBL1936`.
* **Imatinib indications (associations, phase 4)**: `chembl:CHEMBL1642` —`chembl:indication`→ `efo:0000339` chronic
  myelogenous leukemia (MeSH D015464), `mondo:0000870` childhood acute lymphoblastic leukemia (V4HEALTH Mondo entity),
  `efo:1001467` hypereosinophilic syndrome …
* **Metformin**: `chembl:CHEMBL1703` (metformin hydrochloride) —`chembl:mechanism_target`→ `CHEMBL2363065`
  mitochondrial complex I, `CHEMBL3391681` mitochondrial glycerol-3-phosphate dehydrogenase (INHIBITOR, as ChEMBL
  curates them).
* **Chemistry bridge**: `chembl:CHEMBL941` —`unichem:chebi`→ `chebi:45783`; aspirin `chembl:CHEMBL25` →
  `chebi:15365` (V4CHEM's ChEBI subset holds 44 of the 3,288 ChEBI targets today; the rest wait for a larger ChEBI load).
* **Reports, not causation**: FDA_EXAMPLE
* **Trials**: CT_EXAMPLE

Cross-layer resolution today: 1,810 of 1,822 Mondo indication edges point to existing V4HEALTH Mondo entities;
1,241 of 1,851 target components to existing V4BIO UniProt entities (V4BIO loads reviewed human entries; the rest
are other organisms or unreviewed); 823 of 831 target organisms to existing taxa; 44 of 3,288 ChEBI bridges.

## Efficacy and the effects layer

Nothing here is an effect size. ChEMBL indications and eEML listings say a drug is approved / listed for a
condition; trial counts say studies are registered. An efficacy number may enter REVA only through the effects
layer (`docs/dev/v5_effect_interface.md`) as an attributed estimate **with its published confidence interval**:
ClinicalTrials.gov results modules (`resultsSection.outcomeMeasuresModule` analyses: paramValue, ciLower/ciUpper)
are open and are the hand-off to EFFECTS-MORE — e.g. NCT ids listed in ChEMBL indication references (kept in the
indication qualifiers, `nct_ids_listed`) are candidates. Not loaded by V6DRUGS (per-study records, out of the
aggregate scope).

## Missing, and why

* **openFDA API key**: without a key the event endpoint refuses `count` with `limit` > 100 (`API_KEY_MISSING`) and
  allows 1,000 requests/day; so substance counts are the top 100 by FAERS reports plus the serious / non-serious split
  for the substances in scope (UNIIs of the ChEMBL drugs linked to eEML medicines and their salts). A key needs a
  registration with an e-mail → **owner decision**. openFDA matches products to the UNII of the marketed active
  ingredient (often a salt): a parent UNII without a match is recorded "not found" in the run report, never 0.
* **MedDRA reaction terms** (FAERS `reactionmeddrapt`) not loaded: MedDRA is licensed by the MSSO; owner licence
  review before loading per-reaction counts.
* **DrugBank content** (CC BY-NC 4.0, account for downloads): not used beyond accession numbers.
* **ATC hierarchy names / DDDs** (WHOCC, non-commercial, no manipulation): not loaded; codes kept and flagged.
* **ChEMBL**: 398 approved-drug mechanisms have no target id; 226 indications have no / an unparseable EFO id (kept
  only as MeSH); 14 target components are Ensembl gene ids (nucleic-acid targets), not UniProt; non-approved
  indications (phase 1–3, ~51k rows) not fetched (the brief bounds to approved drugs; `--option` free to extend).
* **eEML**: no edition number on the site (revision = retrieval day + list-page hash); the list page says 667
  medicines but lists 650 entries (the rest appear only as therapeutic equivalents); recommendations without an
  ICD-11 code keep their indication text only; "EMLc" is linked only where the page shows the badge.
* **ICD-11 → Mondo**: eEML gives ICD-11 MMS codes + entity URIs; Mondo carries `icd11.foundation` xrefs. The bridge
  (only for URIs without a residual `/mms/unspecified|other`) is V4LINK's identity rule — hand-off.
* **MeSH ↔ Mondo**: ChEMBL indications and ClinicalTrials.gov aggregates share `mesh:` ids; Mondo has 8,090 `MESH:`
  xrefs. Turning those into `same_as` is V4LINK's (hand-off), not done here.
* **ClinicalTrials.gov country names**: 23 of 226 names have no exact crosswalk name (e.g. "South Korea", "Russia",
  "Turkey (Türkiye)", "Vietnam", "Laos", "Burma", "Republic of the Congo", historical "Serbia and Montenegro"):
  `iso3` null until a reviewed alias with evidence is added → owner/maintainer decision (no fuzzy match).
* **Intervention MeSH ↔ ChEMBL drug**: no open published crosswalk used (Wikidata P486/P592 would be one; WDQS is
  throttled — WDOPS queue).

## Owner decisions

1. openFDA API key (registration with an e-mail) — unlocks top-1,000 counts and all approved substances.
2. MedDRA licence for per-reaction FAERS counts.
3. Licence reviews: UniChem (no licence file), ClinicalTrials.gov terms, ChEMBL ShareAlike for redistribution of
   derived data, WHOCC non-commercial ATC codes (flagged on every value).
4. Reviewed aliases for the 23 unmapped ClinicalTrials.gov country names.
5. V4CORE: a `targets` family (fr "cible / est la cible de"), replacing `interacts_with` subtype `targets`.

## Commands (refresh / bigger machine)

```bash
df -h /                                                        # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva seed                    # registers atlas_drugs_* connector versions
for s in atlas.chembl_drugs atlas.unichem atlas.who_eml atlas.openfda atlas.ctgov; do   # order matters (scopes)
  flock /tmp/reva-devdb.lock uv run reva atlas ingest $s
done
# with an openFDA key / more substances: --option max_substances=3000 (needs the key's 120k/day quota)
uv run pytest tests/unit/test_v6_drugs.py tests/integration/test_v6_drugs_ingest.py
uv run python -m reva.kg.families check
```
