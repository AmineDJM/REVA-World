# V3 atlas — society & culture (agent A-SOCIETY)

Status 2026-10-15: four connector modules, nine atlas sources, nine atlas tables, one catalog
(`registry/atlas/catalog/society.yaml`, 87 concepts, all `provider_declared: true`). Offline unit tests
and fixture DB tests pass; one live ingest into a throwaway database (section 5). The dev database `reva`
was never touched.

## 1. Sources: licence decisions (checked 2026-10-15)

| Topic | Source | Licence / terms (quoted) | Decision |
|---|---|---|---|
| Religion | **RCS-Dem 2.0** (Brown & James 2019), OSF `7sr4m` (doi:10.17605/OSF.IO/7SR4M), 40 MB XLSX | OSF node declares no licence (`node_license: null`). The project's own "Usage Agreement" (ARDA): "the author should acknowledge the Association of Religion Data Archives and the original collector(s)", "make responsible use"; no non-commercial or no-redistribution clause | **Ingested, flagged** `open_licence: false`, `licence_status: usage_agreement_attribution` (as A-QOL did for WHR) |
| Religion | **World Religion Project** national v1.1 (Maoz & Henderson 2013), correlatesofwar.org | COW FAQ terms (same record as A-HISTECON's `cow.data`): "data be properly cited", "users cannot charge for access" | **Ingested, flagged** `open_licence: false` |
| Religion | Pew Research Center religious composition 2010-2020 | Terms §1: "Under no circumstances may the Content be reproduced in principal part, mirrored, catalogued, framed, or otherwise republished in its entirety or in principal part without the express written permission" (pewresearch.org/about/terms-and-conditions) | Skipped |
| Religion | World Religion Database (Brill) | proprietary subscription | Skipped |
| Religion | ARDA national profiles | ARDA pages = WRP/RCS data re-served; the underlying datasets are ingested at their source | Not separately ingested |
| Fractionalisation | **HIEF** (Drazanova 2019), Harvard Dataverse doi:10.7910/DVN/4JQRCL | Dataverse record `license.name = "CC0 1.0"` | **Ingested** |
| Fractionalisation | **SSCP Fractionalisation v1.0** (Gordon 2026), doi:10.7910/DVN/EVCQXC — contains Alesina et al. (2003) values + an extension for 38/18/25 small polities | Dataverse record `"CC BY 4.0"` | **Ingested** (origin of every value in `flags.value_origin`) |
| Fractionalisation | Alesina et al. 2003 original file (Harvard/NBER pages), Fearon 2003 (Stanford page) | no licence statement | Not fetched; Alesina values come only through the CC BY 4.0 SSCP release |
| Crime | **WHO GHO homicide estimates** (`VIOLENCE_HOMICIDERATE`, `VIOLENCE_HOMICIDENUM`, Global Health Estimates) | CC BY 4.0 + WHO terms (A-HEALTHEDU's record `who.gho`); not in A-HEALTHEDU's curated list | **Ingested** as `who.gho_violence` |
| Crime | UNODC dataUNODC (homicide, prisons) | portal links to the UN copyright page: "None of the materials provided on this web site may be used, reproduced or transmitted, in whole or in part ... without permission in writing from the publisher" (un.org/en/about-us/copyright); unodc.org/unodc/legal.html has no licence text | Skipped; the UNODC homicide series is already open via WDI (`wb.vc.ihr.psrc.p5`, A-WB) and the UN SDG API (`VC_IHR_PSRC`, A-UN) |
| Justice | World Prison Brief (ICPR) | no reuse terms beyond "the source should be cited as: World Prison Brief, Institute for Crime & Justice Policy Research"; per-country web pages, no bulk file | Skipped |
| Justice | Amnesty death-penalty figures | Amnesty content CC BY-NC-ND 4.0 (non-commercial) | Skipped |
| Justice | **CDPD — Comparative Death Penalty Database** v1 (Anckar & Denk 2024), doi:10.7910/DVN/LI3WYK | Dataverse record `"CC0 1.0"` | **Ingested** (death-penalty status 1800-2022) |
| Digital | ITU DataHub | "licensed to the public through a Creative Commons Attribution-Non-Commercial-ShareAlike 3.0 IGO license" (datahub.itu.int/about) | Skipped; the ITU series are in WDI (A-WB): `wb.it.net.user.zs`, `wb.it.cel.sets.p2`, `wb.it.net.bbnd.p2`, and in the SDG table (`IT_USE_ii99`, `IT_NET_BBND`, A-UN) |
| Tourism | UN Tourism (UNWTO) | "All UN Tourism publications are protected by copyright ... no part ... may be reproduced"; data licensing via elibrary@unwto.org | Skipped; WDI copies (A-WB): `wb.st.int.arvl`, `wb.st.int.dprt`, `wb.st.int.rcpt.cd` |
| Elections | International IDEA Voter Turnout | idea.int/terms-and-conditions: "you may only make non commercial use of the text", "you are not permitted to benefit commercially from their use" (A-POL concluded the same) | Skipped; turnout via IPU Parline / V-Dem (A-POL) |
| Elections | **ParlGov 2024 stable release** (Döring & Manow), doi:10.7910/DVN/2VZ5ZC | Dataverse record `"CC0 1.0"` | **Ingested** (party vote shares and seats, 37 EU/OECD countries since 1900) |
| Elections | Constituency-Level Elections Archive (CLEA) | download behind registration and acceptance of terms; no open licence found | Skipped (not verified as redistributable) |
| Elections | Women in parliament | IPU Parline (A-POL `politics.parliament.women_lower_chamber_pct`) | Referenced |
| Gender / family | **OECD SIGI 2023** + GID-DB 2023 legal-framework variables (sdmx.oecd.org) | OECD terms (record `oecd.sdmx`, A-ILO): open access, CC BY 4.0 by default since 2024-07-01; third-party material excluded | **Ingested**: SIGI + 4 sub-indices + 15 `LAW` variables (OECD's own coding). GID-DB "practices"/"attitudes" (DHS, MICS, Gallup World Poll, WVS…) **not** ingested (third-party data) |
| Gender / family | marriage age, WBL, family law | A-LAW (WBL, SSA), A-UN (SDG 5.3.1 child marriage) | Referenced |
| Freedom | Freedom House | content-permissions: "Use of Freedom House content for noncommercial purposes is permitted"; commercial use "must be formally approved by Freedom House prior to publication"; data files on request | Skipped |
| Freedom | RSF World Press Freedom Index | rsf.org/cgu: "reproduction ou représentation totale ou partielle du site de RSF ou d'éléments les composants sont strictement interdits", "Toute extraction et toute réutilisation ... de la totalité ou d'une partie du contenu" of its databases prohibited | Skipped |
| Freedom | **Transparency International CPI** 2012-2025 | CPI 2025 page footer: "Except where otherwise noted, this work is licensed under CC BY-ND 4.0" (page archived with every run, the statement is checked) | **Ingested unchanged** (ND: values verbatim; long layout is a format change, CC BY-ND 4.0 §2(a)(4)); the 13 third-party source scores in the workbook are **not** ingested |
| Culture | languages / religions / ethnic groups as text | CIA World Factbook (A-FACTBOOK `factbook.text.*`), KG (KGCULT) | Referenced |

## 2. What was built

* `src/reva/adapters/connectors/atlas_society_common.py` — `fetch_archive` (every response archived
  at once through `run.archive`, bodies read back from the store), `check_dataverse_record` (licence
  name + file MD5 + file still in the latest version), `XlsxStream` (bounded streaming XLSX reader for
  **both** transitional and *strict* OOXML namespaces — the CPI workbook is strict, which the shared
  `atlas_xlsx` reader does not read), `RowBuffer` (columnar long-schema builder), helpers.
* `atlas_society_religion.py` — `atlas.rcs_dem` (`rcs.dem2`) and `atlas.wrp` (`wrp.national`).
* `atlas_society_frac.py` — `atlas.hief` (`hief.hief`) and `atlas.sscp_frac` (`sscp.frac_v1`).
* `atlas_society_justice.py` — `atlas.cdpd` (`cdpd.cdpd_v1`) and `atlas.who_violence` (`who.gho_violence`,
  parsed with A-HEALTHEDU's GHO functions, budget key `who.gho`).
* `atlas_society_gov.py` — `atlas.ti_cpi` (`ti.cpi`), `atlas.oecd_sigi` (`oecd.sigi2023`, budget key
  `oecd.sdmx`), `atlas.parlgov` (`parlgov.elections`).
* All modules fetch only through `run.get` / `run.archive` / `run.register`; none builds an HTTP client
  (unit test + invariant 30 `test_every_live_provider_request_path_is_budgeted` pass).
* Sources: `registry/sources/{rcs_dem_osf,hief_dataverse,sscp_dataverse,cdpd_dataverse,ti_cpi,parlgov_dataverse}.yaml`
  (new keys `rcs_dem.osf`, `hief.dataverse`, `sscp.dataverse`, `cdpd.dataverse`, `ti.cpi`,
  `parlgov.dataverse`); WRP reuses `cow.data` (same host and terms), WHO homicide `who.gho`, SIGI `oecd.sdmx`.
* Geo aliases `registry/atlas/geo_aliases/a_society.yaml`: schemes `soc_hief` (162 HIEF names),
  `soc_cdpd` (207 CDPD names), `soc_sscp` (m49 + 689→XKX, 830→CHI), `soc_ti` (iso3 + KSV→XKX),
  `soc_oecd` (iso3 + XKV→XKX); every alias carries its evidence.
* Budget request `registry/atlas/budget_requests/a_society.yaml`.
* Catalog `registry/atlas/catalog/society.yaml` (87 concepts; themes culture / law / politics / society).
* Fixtures `tests/fixtures/source/atlas.{rcs_dem,wrp,hief,sscp_frac,cdpd,who_violence,ti_cpi,oecd_sigi,parlgov}/`
  (~0.97 MB in total, of which 0.54 MB is the real countrycode tarball shared with A-HISTECON's fixtures):
  real responses captured 2026-10-15, large files cut to documented subsets with `derived_from_sha256`;
  integrity records (Dataverse MD5, OSF sha256) are the real records with the subset's checksum.
* Tests: `tests/unit/test_atlas_society.py` (17), `tests/integration/test_atlas_society_ingest.py` (11, fixture
  DB through `run_atlas_ingest(mode="fixture")`, idempotence, licence drift blocks registration),
  `tests/integration/test_atlas_society_live.py` (9, `-m "live and db"`).

## 3. Honesty choices

* **Values as published.** RCS shares are percent (0-100); WRP shares are proportions (0-1) — different
  units, so different catalog concepts (`society.religion.rcs.*_pct` vs `society.religion.wrp.*_share`),
  never mixed in one `source_order`. Zeros published by RCS/WRP are kept; blank cells are omitted.
  Float-artefact texts (`97.1171112060547`, CPI `1.8822840000000001`) are kept as served, never rounded.
* **Period of cross-sections.** SSCP has no year column: the period is the reference year the README
  states for each value origin (`rcs_dem_2.0` → 2015, Britannica `eb` → 2014 edition) or, where none is
  stated, the source's publication year (`alesina_2003` → 2003; `cia` / `wdm` → 2026, the SSCP v1.0 release),
  always explained in `flags.period_basis`. The README statements are checked on every run.
* **Units that change referent** (HIEF "German Federal Republic" 1949-2013, "Democratic Republic of
  Vietnam" 1945-2013, "Yemen Arab Republic" 1945-2013, "Serbia" 1991-2013 without a Montenegro unit) are
  mapped only for the years in which they denote the crosswalk entity (DEU/YEM from 1991, VNM from 1976,
  SRB from 2007); earlier years stay unmapped.
* **Provider continuity is flagged, not rewritten.** RCS maps unit 365 `RUS` 1800-2015 and unit 345 `YUG` to
  ISO3 `SRB` itself; the cells keep `flags.rcs_abbrev` and `rcs_iso3`. CDPD splits predecessor states into
  own units (unmapped). WRP COW codes go through the same date-aware resolver as A-HISTECON (countrycode panel
  + KGGEOPOL policy windows, 31 December); COW 713 resolves to a historical-state QID (Republic of China) under
  that policy and so stays without iso3.
* **HIEF V2 repeats 20 country-years on identical lines** (24 extra lines, e.g. Rwanda 1991, Argentina 2000 ×3):
  kept once with `flags.provider_duplicate_lines`; a repeat with a different value would block registration.
* **Codes keep their labels**: CDPD status 0-4 and RCS MAJID/SUPID carry the codebook label in `value_text`;
  RCS code `0` (no group with a (super)majority, undocumented in the codebook) and `8300` are flagged
  `undocumented_code`.
* **Licence drift blocks**: a Dataverse licence change, an MD5 mismatch, a licence appearing on the OSF node,
  a changed usage agreement, a missing CC BY-ND statement on the CPI page, or the two CPI sheets disagreeing
  → typed `SourceResponseError`, raw bytes archived, nothing registered (tested).

## 4. Catalog

`society.yaml` (domain `society`, themes set per concept): RCS religion shares (28 groups + known
religionists, population estimate, majority/supermajority codes), WRP shares (22 groups),
fractionalisation (SSCP ethnic/language/religion, HIEF ethnic), death-penalty status, WHO homicide rate
(both sexes, female, male) and count, CPI score/rank/standard error/number of sources, SIGI score + 4
sub-indices + 15 legal-discrimination variables. ParlGov per-party series have no concept (addressed by key
`parlgov.elections.vote_share|election=<parliament|ep>|party=<id>`). WDI/SDG/IPU/Factbook series listed in
section 1 are referenced in the file header, not duplicated.

## 5. Live ingest

LIVE_SECTION

## 6. Coverage and gaps

COVERAGE_SECTION

## 7. Shared-file notes for the lead / ATLASCORE

1. **Budgets**: merge `registry/atlas/budget_requests/a_society.yaml` (six new keys + increases on
   `cow.data`, `who.gho`, `oecd.sdmx`). Until then `tests/invariants/test_invariant_30_budgets.py::
   test_budget_keys_are_registered_sources_and_cover_every_connector` lists the six new keys among the
   registered sources without a budget (it already failed before for other agents' pending keys:
   `cshapes.cran`, `ucdp.downloads`, `harvard.dataverse.unvotes`, `unctad.bulk`, `cepii.baci`,
   `cliopatria.github`…). The other eight invariant-30 tests pass. The live test overlays the requested limits
   on the pinned policy in memory only (the pinned file is untouched).
2. Seeding: the nine sources are discovered by `register_atlas_connector_versions` (their source keys are in
   `registry/sources`).
3. `src/reva/adapters/connectors/atlas_xlsx.py` (A-HISTECON) cannot read strict-OOXML workbooks; A-SOCIETY's
   `XlsxStream` handles both namespaces and could replace it if wanted.
4. Since the crosswalk gained `TWN` (2026-10-15), Taiwan maps in HIEF, CDPD, RCS, SSCP (158), CPI and SIGI.
