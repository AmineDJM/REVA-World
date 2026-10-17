# V4LINK — evidenced bridges between the V4 domains and the V3 world

Agent V4LINK, 2026-10-17. Spec: `docs/roadmap/v4_world_model.md` (V4LINK row). Dev database `reva`, origin `live`.

**Bottom line.** Two loaders, both identifier-only (no name or label is ever matched):

1. **Classification concordances (UNSD)** — `atlas.unsd_classif`: 29,399 classification codes (HS 2022 / 2017 / 1992,
   CPC 2.1, ISIC Rev.4, SITC Rev.4) with their hierarchy (29,055 `v4link:class_parent`) and **56,254 concordance rows**
   (`v4link:corresponds_to`) from five UNSD correspondence tables, every row with the table's own flags. A BACI trade
   cell `X|partner=<j>|hs2=27` now reaches ISIC 0610 "Extraction of crude petroleum" in 6 sourced hops.
2. **Wikidata identifier bridges** — `atlas.v4link_wikidata`: rules in `registry/kg/identity_rules_v4.yaml`
   (one-value-per-holder, value-on-several-items = ambiguous, best rank only, format check), 8 packs (places,
   classifications, health, anatomy, chemistry, taxa, genes, companies). Numbers in section 3.

## 1. Sources

| source (connector) | URL | licence | fetched | bytes |
|---|---|---|---|---|
| UNSD classifications (`atlas.unsd_classif` 1.0.0, source key `unsd.classifications`, budget 30/day new) | https://unstats.un.org/unsd/classifications/Econ — `HSCodeandDescription.xlsx` (sheets HS22, HS17, HS92), `SITCCodeandDescription.xlsx` (SITC4), `CPC_Ver_2_1_english_structure.txt`, `ISIC_Rev_4_english_structure.Txt`, `tables/CPC/CPCv21_HS2017/CPC21-HS2017.csv`, `tables/CPC/CPCv21_ISIC4/cpc21-isic4.txt` (+ readme), `tables/HS2022toHS2017…`, `HS2022toHS1992…`, `HS2022toSITC4ConversionAndCorrelationTables.xlsx` | UN terms of use (free download). **HS descriptions reproduce the WCO Harmonized System nomenclature; UNSD states no redistribution licence → every HS entity/statement is flagged `redistribution: unverified (HS nomenclature (c) WCO …)`** | 10 files, one run (load 424) | 4,057,126 |
| Wikidata WDQS (`atlas.v4link_wikidata` 1.0.0, source key `wikidata.wdqs`, WDHARVEST's budget, no increase) | https://query.wikidata.org/sparql — one query per pack + P17 for places + duplicate checks for the filtered packs | CC0, community-edited | see section 3 | see section 3 |

Raw bytes are archived content-addressed (UNSD: evidence acquisitions of the connector version; Wikidata: the
WikidataClient archive, query text + gzip response); every statement carries `raw_sha256`, a selector (file / sheet /
row) and a source revision (`<file>@<Last-Modified date>` for UNSD, `wdqs@<retrieval time>` for Wikidata).

## 2. Classification layer (UNSD) — what was loaded

| scheme | KG id | entities | hierarchy rule |
|---|---|---:|---|
| HS 2022 | `hs2022:<2/4/6 digits>` (kind `hs_code`) | 6,939 | file column `Parent Code` |
| HS 2017 | `hs2017:…` | 6,708 | idem |
| HS 1992 | `hs1992:…` (BACI's nomenclature) | 6,379 | idem (legacy non-WCO `9999AA` refused) |
| CPC Ver. 2.1 | `cpc21:<1-5 digits>` (`cpc_code`) | 4,596 | code structure (each level adds one digit), parent must be a row of the file |
| ISIC Rev.4 | `isic4:<A-U or 2-4 digits>` (`isic_code`) | 766 | class→group→division by code prefix, division→the section row before it |
| SITC Rev.4 | `sitc4:<1-5 digits>` (`sitc_code`) | 4,011 | file column `Parent Code` (12 parents absent from UNSD's file: divisions 09, 28, 91 — reported, not invented) |

Concordances (`v4link:corresponds_to`, subject = first column of the table):

| table (UNSD) | rows loaded | qualifiers (verbatim) | not loaded |
|---|---:|---|---|
| CPC21-HS2017 (HS 2017 → CPC 2.1) | 5,843 | `hs_partial`, `cpc_partial`, `hs_code_as_stated` (`2709.00` → key `270900`) | 0 |
| cpc21-isic4 (CPC 2.1 → ISIC Rev.4) | 2,663 | `cpc_partial`, `isic_partial` | 52 rows stating `n/a` (no correspondent) |
| HS2022-HS2017 Correlations | 15,657 | `relationship` (1:1, 1:n, n:1, n:n), `unsd_conversion: true` when the pair is also UNSD's one-way conversion row | 0 |
| HS2022-HS1992 Correlations | 18,900 | idem | 0 |
| HS22-SITC4 Correlation | 13,191 | idem | 0 |

The `partial` flags are not defined in the files; the doc string says so and records UNSD's convention (1 = only
part of the code is covered) as convention, not as a quoted definition. No weight or share is computed; a chain HS →
CPC → ISIC is two hops, each with its own table.

Atlas link: `registry/kg/v4link_atlas_dimensions.yaml` declares that BACI's folded dimension `hs2=<CC>`
(`atlas.baci.hs92_pair_hs2`) names the KG entity `hs1992:<CC>` (BACI is built on HS 1992; the chapter is the first
two digits of BACI's product code). Sections (I..XXI) are BACI's own grouping: no KG entity claimed.

## 3. Wikidata identifier bridges

**Endpoint.** WDQS throttled the shared egress all afternoon (429 with `Retry-After` 900–1,030 s, repeatedly,
machine-wide cool-down in `var/locks/wikidata.state.json`), and waiting held the dev-DB lock. The same packs are
therefore read from **QLever** (`https://qlever.dev/api/wikidata`, University of Freiburg: the complete Wikidata TTL
dump of 2026-08-10, "updated LIVE"; index name archived each run) through a second source spec,
`atlas.v4link_qlever` (source key `qlever.wikidata`, 60/day, budgets-1.3.24, fetched with `run.get`, raw archived).
Same statements and statement ids as WDQS (QLever writes `q42-GUID`; normalised to `Q42$GUID`). Two QLever defects
found and avoided: `FILTER(?rank != wikibase:DeprecatedRank)` and numeric `FILTER(?sl >= n)` returned partial,
non-deterministic results (71 / 161 / 305 rows instead of 9,044; 0 items) — ranks are filtered in Python and huge
packs are bounded by `ORDER BY DESC(sitelinks) LIMIT N` (`max_items`) instead.

Rules (`registry/kg/identity_rules_v4.yaml`, engine `v4link_common.derive_bridges`): best rank only (preferred if
any); a value held by several items = ambiguous (never linked); an item holding several values of an identity
property = ambiguous holder (never linked); value format checked, never repaired; for bounded packs a whole-Wikidata
duplicate query per property completes the ambiguity check. Keys follow the owners' entity ids as loaded in the dev
DB (`iso3166_2:<code>`, `nuts:<code>`, `chebi:<n>`, `pubchem:cid:<n>`, `gbif:<key>`, `ncbitaxon:<n>`, `hgnc:<n>`,
`mondo:<7 digits>`, `doid:<as stated>`, `uberon:<7 digits>`, `lei:<LEI>`).

PACKS_TABLE

## 4. Relation families

`registry/kg/relations_v4.d/v4link.yaml` (validated by `reva.kg.families check`): `v4link:class_parent` → `part_of`
(subtype `classification_subdivision`), `v4link:classified_as` → `is_a` (`classified_under_code`).
**Unmapped on purpose** until V4CORE adds the families (request, section 6): `v4link:corresponds_to` (needs
`corresponds_to`, symmetric, group classification — a concordance row is not part_of / is_a / identity) and
`v4link:wikidata_item` (needs `same_as`, symmetric, group identity; V4BIO's `bio:wikidata_item` is unmapped for the
same reason). Both stay traversable as KG edges (`/v1/kg/path`, `reva.kg.query.paths`). Nothing is causal.

## 5. Example paths (dev DB, `reva.kg.query.paths`, each hop a sourced statement)

PATHS_PLACEHOLDER

## 6. Missing, and owner decisions

* **V4CORE families** `corresponds_to` and `same_as` (section 4); SendMessage to V4CORE failed (agent not
  reachable), so the request is recorded here.
* **Domain entities not loaded yet in the dev DB.** On 2026-10-17 14:00 no `chebi:`, `pubchem_cid:`, `gbif:`,
  `ncbitaxon:`, `mondo:`, `doid:`, `uberon:`, `hgnc:`, `lei:` entity was loaded live by the domain agents (their
  connectors are committed with fixtures). The bridges are keyed exactly as those connectors key their entities
  (checked in code: `chebi:<int>`, `gbif:<taxonKey>`, `ncbitaxon:<n>`, `mondo:<7 digits>`, `doid:<n>`,
  `uberon:<7 digits>`, `hgnc:<n>`, `lei:<LEI>`), so they resolve as soon as the owners load; `python -m
  reva.adapters.connectors.v4link_report` counts `resolved_to_loaded_entities_by_scheme`. PubChem: V4CHEM's
  compound key was not visible yet; V4LINK uses `pubchem_cid:<CID>` — change `prefix` in the rules file if V4CHEM
  chooses another (one place, re-run retracts and reloads).
* **GBIF backbone 2026.** Wikidata renamed P846 "GBIF-species-ID (before 2026 update)" and created P14607 "GBIF
  taxon ID" (571 items with NCBI on 2026-10-17). V4LINK bridges P846 (`gbif:`); P14607 is an inactive rule until
  V4BIO says which key its taxa carry.
* **GEM wiki ids (P13333, 13,809 items)** are page titles (spaces, accents), not valid KG keys: inactive rule; owner
  decision on a key encoding, or V4ENERGY bridges GEM units through the wiki URL its tracker rows carry.
* **NACE Rev.2 ↔ ISIC Rev.4**: Eurostat's RAMON correspondence pages are retired (404) and the ShowVoc service has no
  documented download; not loaded. Wikidata gives 340 items holding both ISIC Rev.4 (P1796) and NACE (P4496) codes
  (classified_as edges to `isic4:` resolve; `nace2:` codes have no entity yet). Eurostat's SDMX codelist `NACE_R2`
  (codes `B0610`…, CC BY 4.0) could load the NACE structure later; the code-format mapping `B0610` ↔ `06.10` would
  need Eurostat's own statement, not REVA's reformatting.
* **ISCO-08 / ISCED**: no ILO / UNESCO open structure file located in the time box; ILO publishes no ISCO ↔ ISIC
  correspondence. Wikidata ISCO-08 codes (P8283, 1,674 items) are loaded as `classified_as → isco08:<code>`
  (unresolved until a structure is loaded; V4MIND's `atlas.ilostat_occupation` may carry ILOSTAT's major groups).
* **ICD-10 ↔ ICD-11 WHO mapping tables**: published under **CC BY-ND 3.0 IGO** (no derivatives): storing them as KG
  statements is an owner/legal decision; not fetched. ICD-10 ↔ ICD-11 identity comes only from Wikidata items here.
* **geoBoundaries**: Wikidata has no geoBoundaries id property; ADM1 regions meet through ISO 3166-2 codes
  (geoBoundaries' own `shapeISO` field) — V4ECON/V4CLIMATE loaders should key ADM1 by `iso3166_2:<code>` when the
  source publishes it. GADM not used (non-commercial licence).
* **Ambiguity check scope**: for unfiltered packs (places, classifications, anatomy) every holder is fetched, so the
  check is complete; for filtered packs (`min_sitelinks`, `require_two`) a WDQS duplicate query per property
  completes it; when that query times out the property's check is recorded `incomplete` in the run summary.
* **Budget invariant**: `tests/invariants/test_invariant_30_budgets.py::…cover_every_connector` fails on
  `gleif.api` (V4INDUSTRY's registered source without a budget request), not on V4LINK's sources.

## 7. Commands

```bash
# dev DB (each write under the shared lock); disk >= 5 GB checked before
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.unsd_classif
# Wikidata packs one at a time (the runner waits for the machine-wide WDQS cool-down WITHOUT holding the DB lock):
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.v4link_wikidata --option packs=places
#   ... packs: places classifications companies anatomy health chemistry genes taxa
uv run python -m reva.adapters.connectors.v4link_report --paths     # counts + example paths (read-only)
uv run pytest -q tests/unit/test_v4link.py tests/integration/test_v4link_ingest.py
```

Bigger machine: drop `min_sitelinks` in `identity_rules_v4.yaml` for chemistry (152k items), taxa (596k), genes
(43.6k) — about 0.8 M identity statements, ~150 MB of WDQS JSON; run the taxa pack with the duplicate check split by
property (P685/P846 duplicate GROUP BY is the query most likely to time out).

## 8. Files

`src/reva/adapters/connectors/atlas_unsd_classif.py`, `atlas_v4link_wikidata.py`, `v4link_common.py` (predicates,
loader, identity rules engine), `v4link_report.py`; `registry/kg/identity_rules_v4.yaml`,
`registry/kg/relations_v4.d/v4link.yaml`, `registry/kg/v4link_atlas_dimensions.yaml`,
`registry/sources/unsd_classifications.yaml`, `registry/atlas/budget_requests/v4link.yaml` (budgets-1.3.14,
digest re-pinned in `src/reva/autonomy/policy.py`); tests `tests/unit/test_v4link.py` (13),
`tests/integration/test_v4link_ingest.py` (1), fixture `tests/fixtures/source/atlas.unsd_classif/` (trimmed from the
live files: HS chapters 26–27, SITC section 3, CPC 1/12/14/3/33, ISIC B/C 05–09/19; licence in the manifest).
