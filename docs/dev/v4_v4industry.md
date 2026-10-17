# V4 V4INDUSTRY: companies, ownership, facilities, products, industries, supply chains

Agent V4INDUSTRY (2026-10-17). Scope: legal entities and who consolidates whom (GLEIF), notable companies and
factories (Wikidata), country x industry supply-chain flows in value added (OECD TiVA), the identifier bridges
that join them, and the relation-family mappings so the graph can be walked (company -> owned_by -> parent ->
located_in -> country; company -> GEM entity -> plants (V4ENERGY)).

Existing coverage checked first and **not duplicated**: goods trade (`atlas_baci.py` BACI HS92, `comtrade*.py`),
services trade (`atlas_trade_batis.py` BaTIS), UNCTAD trade / FDI / shipping (`atlas_unctad.py`), Eurostat FIGARO
inter-country input-output tables (`io_tables.py`), Wikidata companies spec `economy.major_companies` (KGSCIECO).
ISIC / CPC / HS / SITC classification entities and their UNSD correspondences are **V4LINK's**
(`atlas_unsd_classif.py`, `v4link:*` predicates): I had started an ISIC/CPC loader under the same module name and
withdrew it when V4LINK's broader one landed (my UNSD fixture excerpts remain in
`tests/fixtures/source/atlas.unsd_classif/` for V4LINK to keep or replace). Wikidata company properties
(P127, P749, P355, P1830, P1056, P176, P159, P452) are mapped to families by V4CORE in `relations_v4.yaml`.

## Status

* **Code, fixtures, tests: done and committed** (commits `4e325d2`, catalog commit after it).
* **Live ingest: done for GLEIF, the P1278 bridge and TiVA** (dev DB `reva`, 2026-10-17, after free disk rose to
  6.6 GB; budgets-1.3.23 / 1.3.25):

  | run | requests / bytes | loaded |
  |---|---|---|
  | `atlas.gleif_wikidata` 1.0.1 | 1 WDQS / 33.7 MB | 53,463 `gleif:wikidata_item` statements (53,523 rows; 60 invalid LEI values skipped; 53,267 LEIs, 53,118 items; 194 LEIs on several items, 325 items with several LEIs -- kept as stated) |
  | `atlas.gleif` 1.0.1 (all 4 datasets, one run) | 2,214 / 22.4 MB | atlas `gleif.lei_counts` 1,190 rows (250 countries; XK unmapped), `gleif.legal_form_counts` 568 rows (20 countries); KG 3,398 `lei:` entities (2,486 legal_entity, 891 government_entity -- municipalities and public bodies Wikidata gives an LEI --, 11 international_organisation, 9 fund, 1 sole_proprietor), 43,237 `gleif:*` statements: 221 direct / 234 ultimate parents, 5,315 reporting exceptions (NO_KNOWN_PERSON 1,634 direct, NON_CONSOLIDATING 675, NATURAL_PERSONS 212, NON_PUBLIC 73, NO_LEI 69), 290 GEM ids, 1,102 OpenCorporates ids, 30 successors; 200 largest groups with child counts; seeds 3,276 (3,181 from P1278 >= 20 sitelinks + 95 groups), 0 missing from the index, 122 parents fetched |
  | `atlas.oecd_tiva` 1.0.1 | 12 / 42.3 MB | `oecd.tiva_principal` 181,454 rows, `oecd.tiva_fdva` 330,000 rows (2010-2022; aggregates W, OECD, EU27_2020, ASEAN, G20 ... unmapped as intended) |

  Known blemish of the 1.0.1 GLEIF run: the 200 `gleif:legal_form` statements of the group records were first
  loaded without ELF names (groups dataset) and the entities dataset's richer version was reported `conflicting`
  (same Golden Copy revision) and not stored. Fixed in 1.0.2 (groups fetch the ELF list too); it takes effect on
  the next Golden Copy run.
* **Wikidata company / factory specs: not loaded.** `reva.kg.harvest run --db reva_kg_v4industry` selected the
  seeds (3,104 companies, 3,567 factories via WDQS) but `wbgetentities` stayed throttled (HTTP 429 "You are making
  too many requests", 13 attempts per spec, the shared-egress throttle known since V3); nothing was loaded and the
  throwaway DB was dropped. Re-run on a quiet day (command below). P1278 / P5531 / P2139 / P2403 / P2295 / P2226 are
  now allow-listed (property requests merged).
* Tests: `tests/unit/test_v4industry.py` (16 passed), `tests/integration/test_v4industry_ingest.py` (3 passed:
  fixture ingest of every dataset into a throwaway DB, idempotent re-run, the Toyota LEI -> Wikidata / GEM bridge);
  ruff, ruff format, mypy clean on all V4INDUSTRY modules; `tests/unit/test_kg_families.py` and
  `tests/unit/test_atlas_catalog.py` pass with the new files.

## Sources

| source (key) | URL | licence | what is fetched | live size (measured) |
|---|---|---|---|---|
| GLEIF Global LEI Index API (`gleif.api`, new) | `https://api.gleif.org/api/v1` | **CC0 1.0** (GLEIF LEI data terms) | pagination totals of filtered `lei-records` (page size 1); `/countries`; `/entity-legal-forms` (18 pages); `sort=-ultimateChildCount` top 200; `ultimate-children` / `direct-children` totals; `filter[lei]=<200 LEIs>&include=direct-parent,ultimate-parent` + parent pages | ~3.3 kB per count; ELF list 1.7 MB; ~1-2 MB per page of 200 records; ≈ 45-60 MB for a full run (~2,300 requests at 1.1 s) |
| Wikidata WDQS (`wikidata.wdqs`, existing) | `query.wikidata.org/sparql` | CC0 | one query: every non-deprecated P1278 (LEI) statement + item sitelinks | 33.7 MB JSON (53,523 rows), archived gzip (~4 MB) |
| OECD TiVA 2025 (`oecd.sdmx`, existing key) | `https://sdmx.oecd.org/sti-public/rest` (TiVA flows are external references of `/public`) | OECD CC BY 4.0 default (dataset statement not verified: `licence_status` flag, as BaTIS) | CL_TIVA_MEASURE (26 kB), OECD CL_ACTIVITY_ISIC4 (0.9 MB), available constraint (16 kB), 7 x MAINLV (2010-2022), 2 x FDVA | ≈ 50 MB per run (FDVA all-activities 2010-2022 ≈ 10.6 MB; by-section 2022 ≈ 17 MB) |
| Wikidata harvest specs (WDHARVEST executor) | `registry/kg/queries/industry/v4_*.yaml` | CC0 | 3,104 LEI-bearing items with >= 20 sitelinks; 3,567 factories with coordinates + owner/operator/product | ~65 + ~75 `wbgetentities` calls (+ label snapshots of objects) |

Not used (owner decisions, below): SEC EDGAR company facts, Open Supply Hub, UNIDO INDSTAT.

## What the connectors publish

### `atlas.gleif` (src/reva/adapters/connectors/atlas_gleif.py; datasets run in this order in one run)

| dataset | kind | content |
|---|---|---|
| `lei_counts` | atlas `gleif.lei_counts` | per legal-address country (GLEIF's 250 ISO countries), `lei_records|filter=all / issued / general / fund / branch`; provider_geo = ISO alpha-2 (`iso2` scheme; XK Kosovo stays unmapped), period = Golden Copy publish day; a country whose `all` total is 0 gets that reported 0 and no other filter rows (not fetched, never assumed) |
| `legal_form_counts` | atlas `gleif.legal_form_counts` | `lei_records|legal_form=<ELF>` for every ISO 20275 code GLEIF lists for 20 countries (DE GB IT ES NL CN JP IN LU IE SE CH AU BR KR DK PL AT NO SA; ~570 codes); ELF local names in flags; residual not derived |
| `groups` | KG | the 200 LEI records with the most ultimately-consolidated LEI records, each with `gleif:ultimate_child_count` / `gleif:direct_child_count` (point in time = Golden Copy day) |
| `entities` | KG | Level 1 + Level 2 of the seeds: run option `leis`, `lei:` keys other sources put in the KG, P1278 LEIs of Wikidata items with >= 20 sitelinks (3,104 at 2026-10-17), the groups; parents named by `include` fetched in a 2nd pass (Level 1 only, one hop) |

`atlas.gleif_wikidata` (same module, source key `wikidata.wdqs`): `lei:<LEI> --gleif:wikidata_item--> Q...` for
every valid P1278 value (ISO 17442 MOD 97-10 check); 60 of the 53,523 live values are not valid LEIs and are skipped
and counted, never repaired (e.g. `95980020140005000000`).

Measured live (2026-10-17 Golden Copy): FR legal-address LEI records 192,860 (ISSUED 96,297); JP 19,391, of which
legal form T417 (株式会社, kabushiki kaisha) 3,437; all LEI records 3,465,300; largest groups by ultimately
consolidated LEI records: Morgan Stanley 1,018 (direct 173), Bank of America 760 (155), Citigroup 749 (24),
Siemens AG 573 (direct 460).

### `atlas.oecd_tiva` (src/reva/adapters/connectors/atlas_tiva.py)

| table | indicator | rows (full run, estimate) |
|---|---|---|
| `oecd.tiva_principal` | `<MEASURE>|activity=<_T or ISIC section A..T>` for PROD, VALU, EXGR, EXGR_DVA, EXGR_FVA, FFD_DVA, DFD_FVA, partner World, 2010-2022, ~99 areas | ~190 k |
| `oecd.tiva_fdva` | `FDVA|fd_area=<c>|va_activity=<j>`: geography = value-added **source** economy s; j = `_T` for 2010-2022 and each section for 2022 | ~130 k + ~200 k |

Values are model-based ICIO estimates (`flags.estimate`), million USD; the OECD measure / activity labels come
from the code lists fetched in the same run; `flags.isic4_section` is set only when the TiVA activity code *is* an
ISIC Rev.4 section letter, so the series can be read against V4LINK's `isic4:<section>` industries. FDVA is an
aggregate value-added flow ("value added from s embodied in c's final demand"), **not** a firm-level supplier
relation and not causal: it is kept in the atlas, not turned into `supplies` edges.

Catalog: `registry/atlas/catalog/industry.yaml` (11 concepts, themes `industry` / `companies`), every series
checked against the fixture tables.

### KG predicates and relation families

`registry/kg/predicates_industry.yaml` (registered by the loaders): `gleif:direct_parent`, `gleif:ultimate_parent`
(accounting consolidation as reported by the child; no share), `gleif:parent_reporting_exception` (verbatim
`<category>: <reason>`, e.g. `NATURAL_PERSONS`, `NO_KNOWN_PERSON`, `NON_CONSOLIDATING`), `gleif:successor`,
`gleif:legal_address_country` / `gleif:headquarters_country` (country QID through ISO code -> crosswalk ->
`country_qids.json`), ISO 3166-2 regions + headquarters city (literals until ADM1 entities exist), jurisdiction,
ELF legal form (+ local names), category, entity / registration status, creation date, `gleif:gem_entity`
(`gem:<id>`, GLEIF's own LEI->GEM mapping), `gleif:opencorporates_id`, child counts, `gleif:wikidata_item`.

`registry/kg/relations_v4.d/v4industry.yaml`: `gleif:direct_parent` / `gleif:ultimate_parent` -> **owned_by**
(subtypes directly / ultimately consolidated_by), legal / HQ country -> **located_in**, successor and GEM id ->
**context**; `gleif:wikidata_item` deliberately unmapped (identity bridge). Entity kinds: `legal_entity`
(GLEIF category GENERAL -- GLEIF does not say "company"), `fund`, `branch`, `sole_proprietor`,
`government_entity`, `international_organisation`. Families used: owned_by, located_in, context (+ V4CORE's
produces / produced_by / owns / located_in mappings of the Wikidata company and factory claims).

### Wikidata specs and property requests

* `registry/kg/queries/industry/v4_companies_lei.yaml` (seed, 3,104 items, `expand_hops: 1`) and
  `v4_factories.yaml` (seed, ~3,567 factories with P625 and P127/P137/P1056; facility layer: factory -> owned_by /
  operator -> company, factory -> produces -> product, factory -> located_in). Validated (`harvest validate`).
* `registry/kg/property_requests/v4industry.yaml`: P1278 (LEI), P5531 (SEC CIK), P2139 revenue, P2403 total
  assets, P2295 net profit, P2226 market capitalisation -- to merge with `merge-property-requests`.

## Examples (ids; from the fixture loads, which replay real live responses)

* **Ownership chain**: `lei:529900IR3Y1XBIR1GX29` Siemens Healthcare (Private) Limited (Lahore) -> gleif:direct_parent
  (owned_by) -> `lei:529900QBVWXMWANH7H45` Siemens Healthineers AG -> ... ; gleif:ultimate_parent (owned_by) ->
  `lei:W38RGI023J3WT1HWRP32` Siemens Aktiengesellschaft -> gleif:legal_address_country (located_in) -> Q183 (Germany).
* **No parent, stated reason**: Apple Inc. `lei:HWUPKR0MPOU8FGXBT394` -> gleif:parent_reporting_exception
  "DIRECT_ACCOUNTING_CONSOLIDATION_PARENT: NATURAL_PERSONS" (never read as "no owner"); Toyota and GM: NO_KNOWN_PERSON.
* **Identifier bridges to Wikidata and energy**: Toyota `lei:5493006W3QUS5LMH6R84` (labels ja トヨタ自動車株式会社 /
  en Toyota Motor Corporation) -> gleif:wikidata_item -> Q53268 (Wikidata P1278) and -> gleif:gem_entity ->
  `gem:E100001016208` (GLEIF's mapping), legal form T417 株式会社, legal address Q17 (Japan). With V4ENERGY's GEM
  ownership data on `gem:` keys, Q53268 -> LEI -> GEM entity -> plants is a chain of published identifiers only.
* **Supply chain (atlas, live)**: `oecd.tiva_fdva` `FDVA|fd_area=FRA|va_activity=C`, 2022: value added of
  manufacturing embodied in French final demand = 36,242.3 million USD from Germany, 28,579.2 from China, 14,642.8
  from the USA (ICIO estimates), `flags.isic4_section = C` -> V4LINK's `isic4:C` -> CPC products
  (`v4link:corresponds_to`) -> HS codes -> BACI trade flows.
* **Live KG chains**: Google LLC `lei:` -> gleif:ultimate_parent -> Alphabet Inc. `lei:5493006MHB84DD0ZWV18`
  (Wikidata Q95 via P1278); Juventus F.C. S.p.A. (Q1422) -> gleif:ultimate_parent -> EXOR N.V.
  `lei:5493002ENHZ6NYET7405`; Microsoft (Q2283) -> gleif:gem_entity -> `gem:E100001000937`; City of Los Angeles
  (Q65) -> `gem:E100001000317` (municipal utilities appear in GEM ownership data).

## What is missing and why

* **Wikidata company and factory snapshots** (wbapi throttled, see Status): the companies' industry, products,
  employees, revenue and the factory layer wait for the harvest re-run.
* **GLEIF relationship details** (relationship periods, accounting standard, validation documents of each Level 2
  record) need one request per child (`/direct-parent-relationship`); the batch `include` gives only the parent
  LEI. Not fetched; statements carry `as_of_golden_copy` instead of a start date (no date is invented).
* **Parents of parents**: one hop only; the ultimate parent is always stated directly, so chains to the top exist.
* **Legal-form counts** for US and CA (one ELF list per state / province: 700+ and 200+ codes), FR (270+), RU (127)
  are left out of the default (budget); pass `--option legal_form_countries=FR` etc. on a bigger budget day.
* **Children of the largest groups** (full subsidiary lists): Morgan Stanley alone has 1,018 LEI children; listing
  them for 200 groups is ~200 MB of records. Only counts are loaded.
* **Firm-level supplier relations** (`supplies` / `buys_from`): no open source in reach states them -- Open Supply
  Hub needs an account; Wikidata has no supplier property in use at scale. Nothing inferred from co-occurrence.
* **Company financials** (revenue, assets, employees per year): SEC EDGAR skipped (below); Wikidata P2139/P2403
  pending the property merge; P1128 (employees) is already allow-listed and comes with the company spec.
* **NACE / NAICS concordances**: not loaded (V4LINK owns classification bridges; FIGARO already uses NACE codes).

## Owner decisions

1. **SEC EDGAR** (`data.sec.gov` companyfacts XBRL: revenue, assets, employees of US filers): SEC requires a contact
   e-mail in the User-Agent; `REVA_HTTP_CONTACT_EMAIL` is empty and the owner's personal e-mail must never be used.
   Set a project ops contact (e.g. a role mailbox) in `REVA_HTTP_CONTACT_EMAIL`; then a connector keyed by CIK
   (`P5531`, requested) can be added. No SEC request was made.
2. **Open Supply Hub** (facilities / suppliers, data CC BY-SA 4.0): the API answers 401 "Authentication credentials
   were not provided" for every endpoint; an account token would be needed (and share-alike terms reviewed).
3. **UNIDO INDSTAT**: not attempted in this session (its access terms were not checked); TiVA + FIGARO cover
   industry value added for now.
4. **Licence reviews**: TiVA (OECD default CC BY 4.0, dataset-level statement not verified).
5. **Kind vocabulary**: GLEIF records are typed `legal_entity` / `fund` / `branch` ..., not `company`, because GLEIF's
   GENERAL category includes associations, public bodies and trusts. V4CORE may alias `legal_entity` in the
   generic entity page.

## Commands to finish / refresh (when `df -h /` shows >= 5 GB free)

```bash
cd /home/user/REVA-World
flock /tmp/reva-devdb.lock uv run reva seed                                   # registers atlas.gleif 1.0.2
# 1-3 were run live on 2026-10-17 (see Status); refresh monthly:
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gleif_wikidata
# GLEIF, all datasets in ONE run (planned 2,600 requests of the 3,000/day budget; ~40 min at 1.1 s)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gleif
#    (bigger budget day: --option legal_form_countries=US,CA,FR,RU --dataset legal_form_counts)
# OECD TiVA (12 requests, 42 MB)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.oecd_tiva
# 4. Wikidata companies + factories (throwaway harvest DB, then governed import into reva) -- NOT YET RUN (wbapi 429)
uv run python -m reva.kg.harvest run --db reva_kg_v4industry \
    registry/kg/queries/industry/v4_companies_lei.yaml registry/kg/queries/industry/v4_factories.yaml
flock /tmp/reva-devdb.lock uv run python -m reva.kg.import_harvest --from-db reva_kg_v4industry
dropdb reva_kg_v4industry                                                     # throwaway
```

On a bigger machine: raise `top_groups` (max 200 per page; more needs paging code), `min_sitelinks` 10 (4,918
seeds), add `legal_form_countries` for US/CA/FR/RU, and fetch the full GLEIF Level 2 relationship file (RR CDF
golden copy, ~100 MB zipped) instead of per-seed pages.
