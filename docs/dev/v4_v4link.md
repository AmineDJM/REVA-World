# V4LINK — evidenced bridges between the V4 domains and the V3 world

Agent V4LINK, 2026-10-17. Spec: `docs/roadmap/v4_world_model.md` (V4LINK row). Dev database `reva`, origin `live`.

**Bottom line.** Two loaders, both identifier-only (no name or label is ever matched):

1. **Classification concordances (UNSD)** — `atlas.unsd_classif`: 29,399 classification codes (HS 2022 / 2017 / 1992,
   CPC 2.1, ISIC Rev.4, SITC Rev.4) with their hierarchy (29,055 `v4link:class_parent`) and **56,254 concordance rows**
   (`v4link:corresponds_to`) from five UNSD correspondence tables, every row with the table's own flags. A BACI trade
   cell `X|partner=<j>|hs2=27` now reaches ISIC 0610 "Extraction of crude petroleum" in 6 sourced hops.
2. **Wikidata identifier bridges** — `atlas.v4link_wikidata` (WDQS) / `atlas.v4link_qlever` (QLever mirror, used
   because WDQS throttled): rules in `registry/kg/identity_rules_v4.yaml` (one-value-per-holder,
   value-on-several-items = ambiguous, best rank only, format check), 8 packs. Loaded: places (6,848 identity edges,
   722 items bridging ISO 3166-2 ↔ NUTS, 5,331 edges hitting entities other V4 agents loaded) and classifications
   (3,431 item → HS/CPC/ISIC/NACE/ISCO edges). The 6 other packs wait for disk (≥ 5 GB floor; section 3).

## 1. Sources

| source (connector) | URL | licence | fetched | bytes |
|---|---|---|---|---|
| UNSD classifications (`atlas.unsd_classif` 1.0.0, source key `unsd.classifications`, budget 30/day new) | https://unstats.un.org/unsd/classifications/Econ — `HSCodeandDescription.xlsx` (sheets HS22, HS17, HS92), `SITCCodeandDescription.xlsx` (SITC4), `CPC_Ver_2_1_english_structure.txt`, `ISIC_Rev_4_english_structure.Txt`, `tables/CPC/CPCv21_HS2017/CPC21-HS2017.csv`, `tables/CPC/CPCv21_ISIC4/cpc21-isic4.txt` (+ readme), `tables/HS2022toHS2017…`, `HS2022toHS1992…`, `HS2022toSITC4ConversionAndCorrelationTables.xlsx` | UN terms of use (free download). **HS descriptions reproduce the WCO Harmonized System nomenclature; UNSD states no redistribution licence → every HS entity/statement is flagged `redistribution: unverified (HS nomenclature (c) WCO …)`** | 10 files, one run (load 424) | 4,057,126 |
| Wikidata WDQS (`atlas.v4link_wikidata` 1.1.1, source key `wikidata.wdqs`, WDHARVEST's budget, no increase) | https://query.wikidata.org/sparql — one query per pack + P17 for places + duplicate checks for the bounded packs | CC0, community-edited | nothing loaded (429 cool-downs) | 0 |
| Wikidata via QLever (`atlas.v4link_qlever` 1.1.0/1.1.1, source key `qlever.wikidata`, 60/day new) | https://qlever.dev/api/wikidata (index stats + same queries) | CC0 data, community-edited; public research endpoint (Univ. Freiburg) | packs places (1.1.1) and classifications (1.1.0) | 8.4 MB |

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

Loaded in the dev DB (origin `live`, source `qlever.wikidata`):

| pack | items fetched | statements read | linked | not linked (reason) | KG writes |
|---|---:|---:|---:|---|---|
| places (P300 ISO 3166-2, P605 NUTS; + P17) | 6,408 | 7,967 rows (7,361 best-rank) | 6,848 (P300 5,229, P605 1,619) | P300: 228 value-on-several-items, 8 holders with several codes, 12 malformed (e.g. `NO-3905`, `VI`); P605: 244 holders with several NUTS codes (e.g. Ile-de-France FR1 + FR10), 21 values on several items | 6,848 identity + 6,585 best-rank P17 (6,406 items; 56 items with several countries) = 13,433 statements, 6,341 new `wikidata_item` entities; raw 6.7 MB |
| classifications (P5471 HS 2022, P11373 CPC 2.1, P1796 ISIC 4, P4496 NACE 2, P8283 ISCO-08) | 3,060 | 3,450 | 3,431 `classified_as` | 17 malformed (e.g. Wikidata gives petroleum Q22656 the "HS 2022 code" `331`, which is not an HS code) | 3,431 statements, 3,044 new item entities; raw 1.7 MB (connector 1.1.0) |

Resolution against entities other V4 agents loaded: **3,938 of 5,229** ISO 3166-2 bridges hit a `region_adm1` entity
(Natural Earth admin-1, `naturalearth.admin1_10m`), **1,393 of 1,619** NUTS bridges hit a `nuts_region` entity;
**722 Wikidata items bridge an ISO 3166-2 code and a NUTS code** (two V4 region systems meet, e.g. Bavaria). Of the
classification edges, 169/169 CPC, 425/429 ISIC, 151/156 HS 2022 targets are codes loaded by `atlas.unsd_classif`;
ISCO-08 53/1,674 (only codes another loader created), NACE 0/1,003 (no NACE structure loaded).

**Not loaded (disk below the 5 GB floor at 16:15–16:30, shared by ~11 agents):** packs companies (LEI ↔ SEC CIK,
~2.2k items), health (ICD-10 / ICD-11 / MONDO / DOID, 16,956 items), anatomy (UBERON, ~6.4k), chemistry (top
15,000 ChEBI ∧ PubChem items, 14 MB response tested), genes (top 10,000 HGNC ∧ NCBI Gene), taxa (top 20,000 GBIF
∧ NCBI taxon). Queries verified live on QLever (counts above); the commands in section 7 finish them.

## 4. Relation families

`registry/kg/relations_v4.d/v4link.yaml` (validated by `reva.kg.families check`): `v4link:class_parent` → `part_of`
(subtype `classification_subdivision`), `v4link:classified_as` → `is_a` (`classified_under_code`).
**Unmapped on purpose** until V4CORE adds the families (request, section 6): `v4link:corresponds_to` (needs
`corresponds_to`, symmetric, group classification — a concordance row is not part_of / is_a / identity) and
`v4link:wikidata_item` (needs `same_as`, symmetric, group identity; V4BIO's `bio:wikidata_item` is unmapped for the
same reason). Both stay traversable as KG edges (`/v1/kg/path`, `reva.kg.query.paths`). Nothing is causal.

## 5. Example paths (dev DB, `reva.kg.query.paths`, each hop a sourced statement)

From `uv run python -m reva.adapters.connectors.v4link_report --paths` (each hop one statement with its raw sha256;
"A path is a chain of sourced statements … not a causal explanation"):

1. **Country → crude-oil trade → industry.** `atlas.baci.hs92_pair_hs2` row `X|partner=842|hs2=27`, iso3 `SAU`, 2015:
   21,845,716.997 thousand USD (CEPII BACI HS92 V202601, raw `1ad9979c…`, selector member `BACI_HS92_Y2015_V202601.csv`
   i=682 j=842) → dimension `hs2=27` names `hs1992:27` (Mineral fuels, mineral oils …) ← `hs1992:2709` part_of ←
   `hs1992:270900` ← corresponds_to (HS2022-HS1992, 1:1, UNSD conversion) `hs2022:270900` → corresponds_to
   (HS2022-HS2017, 1:1) `hs2017:270900` → corresponds_to (CPC21-HS2017, partial 0/0, `2709.00`) `cpc21:12010`
   "Petroleum oils … crude" → corresponds_to (cpc21-isic4) `isic4:0610` "Extraction of crude petroleum" → part_of
   `isic4:061` → `isic4:06` → `isic4:B` "Mining and quarrying". The last hops to companies and oil fields
   (`isic4:0610` → firms classified there → GEM / GPPD facilities → ADM1) need V4INDUSTRY/V4ENERGY to state an ISIC or
   NACE code on their firms (none does yet: Wikidata P1796/P4496 on companies is rare; GLEIF has no activity code).
2. **Mineral → product → industry.** `Q111044` chalcopyrite —classified_as (Wikidata P5471)→ `hs2022:260300` "Copper
   ores and concentrates" → `hs2017:260300` → `cpc21:14210` "Copper, ores and concentrates" → `isic4:0729` "Mining of
   other non-ferrous metal ores" (4 hops; also `hs2022:270900` → `sitc4:3330` "Crude petroleum" for SITC series).
3. **Two region systems meet.** `iso3166_2:DE-BY` (Natural Earth admin-1 entity "Bavaria") —wikidata_item→ `Q980`
   ←wikidata_item— `nuts:DE2` (Eurostat NUTS entity "Bayern"); `Q980` —P17 (preferred; the normal-rank historical
   states are not loaded)→ `Q183` Germany. Same for `iso3166_2:SA-04` (Eastern Province) → `Q851` Saudi Arabia.
   Ile-de-France shows the guard: `Q13917` holds NUTS `FR1` and `FR10` → ambiguous holder, no NUTS bridge; its
   ISO code is the preferred `FR-IDF`, the normal-rank `FR-J` is not loaded.
4. **ISIC class → section** (hierarchy from the code structure): `isic4:0610` → `isic4:061` → `isic4:06` → `isic4:B`.
5. **Species / disease / compound / company** chains (`gbif:2436436` ≡ `Q15978631` ≡ `ncbitaxon:9606`; `icd10:E11`
   ≡ item ≡ `mondo:0005148`; `chebi:15377` ≡ `Q283` ≡ `pubchem:cid:962`; `lei:HWUPKR0MPOU8FGXBT394` ≡ `Q312` ≡
   `sec_cik:320193`) are the shapes the pending packs produce; they are **not in the dev DB** (packs not run, see
   section 3) — the report lists them as `found: false`.

Cross-domain bridges in the dev DB now: **56,254** concordance rows between classification schemes, **3,431**
item → code classifications, **6,848** identity edges (5,331 resolved to entities loaded by V4 domain agents),
**722** Wikidata items bridging two schemes (ISO 3166-2 ↔ NUTS), 6,585 P17 country edges for those regions.

## 6. Missing, and owner decisions

* **V4CORE families** `corresponds_to` and `same_as` (section 4); SendMessage to V4CORE failed (agent not
  reachable), so the request is recorded here.
* **Keys of the domain owners.** By 16:30 the domain agents had loaded live `iso3166_2:` (4,253 region_adm1),
  `nuts:` (2,211), `hgnc:` (45,277 genes), `mondo:` (32,134), `doid:` (12,335), `uberon:` (13,194), `gbif:` (1,039),
  `ncbitaxon:` (1,232), `chebi:` (1,356), `pubchem:cid:` (110), `lei:` (3,398) entities; the rules use exactly these
  key forms (PubChem was switched from `pubchem_cid:` to V4CHEM's `pubchem:cid:` before any chemistry load), so the
  pending packs resolve on load; `python -m reva.adapters.connectors.v4link_report` counts
  `resolved_to_loaded_entities_by_scheme`.
* **Company → industry codes.** No V4 firm loader states an ISIC/NACE activity code yet (GLEIF has none; Wikidata
  P452 "industry" points at items, P1796/P4496 sit on industry items, not firms). The product → industry chain
  therefore stops at `isic4:<class>`; SEC EDGAR SIC codes (V4INDUSTRY) would need the SIC ↔ ISIC table, which the
  US Census publishes for NAICS only (NAICS 2017 ↔ ISIC 4 is in UNSD's list: `tables/ISIC/ISIC4-NAICS2017US.zip`).
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
* **Disk.** The shared disk sat at 4.86–5.00 GiB free from 15:40; V4LINK ran the places pack at 5,001 MiB
  (5.24 GB) and stopped below 5,000 MiB. V4LINK's own raw archive: 4.1 MB (UNSD) + 8.4 MB (QLever).
* **Budget invariant**: `tests/invariants/test_invariant_30_budgets.py::…cover_every_connector` fails on
  `gleif.api` (V4INDUSTRY's registered source without a budget request), not on V4LINK's sources.

## 7. Commands

```bash
# dev DB (each write under the shared lock); disk >= 5 GB checked before
flock /tmp/reva-devdb.lock uv run reva seed
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.unsd_classif
# Wikidata packs one at a time, from QLever (WDQS: same with atlas.v4link_wikidata once its 429 cool-down is over):
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.v4link_qlever --option packs=places
#   still to run (disk floor): companies health anatomy chemistry genes taxa  (+ classifications again at 1.1.1)
for p in companies health anatomy chemistry genes taxa classifications; do
  [ "$(df --output=avail -B1M / | tail -1)" -ge 5120 ] || break
  flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.v4link_qlever --option packs=$p
done
uv run python -m reva.adapters.connectors.v4link_report --paths     # counts + example paths (read-only)
uv run pytest -q tests/unit/test_v4link.py tests/integration/test_v4link_ingest.py
```

Bigger machine: drop `max_items` in `identity_rules_v4.yaml` for chemistry (152k items), taxa (596k), genes
(43.6k) — about 1.6 M identity statements, ~250 MB of SPARQL JSON (split taxa by a VALUES list of GBIF kingdoms or by
item-id ranges if QLever times out).

## 8. Files

`src/reva/adapters/connectors/atlas_unsd_classif.py`, `atlas_v4link_wikidata.py`, `v4link_common.py` (predicates,
loader, identity rules engine), `v4link_report.py`; `registry/kg/identity_rules_v4.yaml`,
`registry/kg/relations_v4.d/v4link.yaml`, `registry/kg/v4link_atlas_dimensions.yaml`,
`registry/sources/unsd_classifications.yaml`, `registry/atlas/budget_requests/v4link.yaml` (budgets-1.3.14,
digest re-pinned in `src/reva/autonomy/policy.py`); tests `tests/unit/test_v4link.py` (14),
`tests/integration/test_v4link_ingest.py` (2), fixtures `tests/fixtures/source/atlas.v4link_qlever/` (trimmed live QLever places responses + index stats) and `tests/fixtures/source/atlas.unsd_classif/` (trimmed from the
live files: HS chapters 26–27, SITC section 3, CPC 1/12/14/3/33, ISIC B/C 05–09/19; licence in the manifest).
