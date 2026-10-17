# REVA World — V4 / V5 completion report

Date: 2026-10-17 (UTC). Branch: `claude/kind-darwin-kyy8re`. Written by agent V6 INTEG. V1/V2: `docs/final_report.md`;
V3: `docs/final_report_v3.md`. Plans: `docs/roadmap/v4_world_model.md`, `docs/roadmap/v5_prediction_engine.md`,
`docs/roadmap/v6_plan.md`.

**Where the numbers come from.** Every number below is quoted from the agent report named next to it
(`docs/dev/v4_*.md`, `docs/dev/v5_*.md`) or from the V6 plan's state summary (`docs/roadmap/v6_plan.md`, dev DB
2026-10-17). This report ran no data load. The integration checks it ran are in `docs/dev/v6_integ.md`.

## 1. What exists

### V4 — the world model (what things are, where, who, how much)

| Layer (agent, report) | What is in the dev DB `reva` (origin `live`) | Main gaps |
|---|---|---|
| Relation families (V4CORE, `v4_v4core.md`) | `registry/kg/relations_v4.yaml`: **23 families** (21 by V4CORE + `corresponds_to`, `same_as` added by V6 INTEG for V4LINK), all `causal: false`; **158 mappings from 14 files** (`python -m reva.kg.families check`); `/v1/kg/families`, `/v1/kg/entities/{id}/families`, `/places`, `/families/path`; Fiche screen `/v3/fiche`; space-time helpers | `supplies` / `buys_from` / `trades_with` have no source yet (no open firm-level supply chain) |
| Chemistry (V4CHEM, `v4_v4chem.md`) | 118 elements (PubChem / AMDC / NIST CODATA), ChEBI 256 subset 1,356 entities, PubChem compound links (`pubchem:cid:`) | Wikidata chemistry specs not harvested (WDQS 429); IAEA LiveChart behind a Cloudflare challenge |
| Biology (V4BIO, `v4_v4bio.md`) | GBIF backbone partitions, Catalogue of Life counts, HGNC genes, UniProt reviewed proteins, Ensembl / NCBI genome summaries (KG only) | GBIF 2026 backbone re-issue (P14607) changes keys; vertebrate species lists need ~+80 MB |
| Health (V4HEALTH, `v4_v4health.md`) | UBERON, Human Disease Ontology (15,436 entities / 75,918 statements), Mondo (32,134 diseases / 152,899 statements), ClinGen gene validity; 14,508 causal claims to grade | WHO GHE (cause-of-death burden) not run live (disk); GBD non-commercial |
| Ecology (V4ECO, `v4_v4eco.md`) | GloBI 23,091 taxa / 39,230 interaction statements, RESOLVE ecoregions 869 entities, FAO FishStat 136,317 rows | GBIF occurrence facets, UN SDG, OWID LPI, FAO QCL live runs pending (disk) |
| Climate (V4CLIMATE, `v4_v4climate.md`) | CCKP monthly country series, NOAA GML concentrations, four global anomaly series, stations (27.9 k), ADM1, 739 T2 attribution claims; ~3.3 M atlas rows | CCKP ADM1 geocodes (403), Carbon Brief NC-ND licence review |
| Energy (V4ENERGY, `v4_v4energy.md`) | World Bank Pink Sheet 73,749 cells, EIA STEO 718,234 observed + 35,213 forecast cells (kept apart), WRI GPPD 34,951 plants / 291,310 statements, USGS facilities 12,134 / 116,335 | GEM trackers (form), Energy Institute (blocks clients); Wikidata energy specs not harvested |
| Economy (V4ECON, `v4_v4econ.md`) | 22 atlas tables, **1,300,766 cells** (IMF / Eurostat / OECD monthly & quarterly, NUTS / OECD TL2-TL3 regions, ICP 2021, FAO FFPI); 5,628 region entities, 27,793 statements (`econ:measured_by` 12,010) | IMF terms review; FFPI non-commercial |
| Industry (V4INDUSTRY, `v4_v4industry.md`) | GLEIF 3,398 legal entities / 43 k statements + counts, P1278 bridge 53,463 statements, OECD TiVA 511 k rows | SEC EDGAR (contact e-mail), Open Supply Hub (account), Wikidata company / factory harvest throttled |
| Mind & society (V4MIND, `v4_v4mind.md`) | 682,021 atlas rows (time use, ILOSTAT occupations, Hofstede research-only, OECD How's Life?), ISCO-08 groups with `ilostat:measured_by` | WVS / EVS, GPS (registration), OpenAlex key |
| Cross-domain bridges (V4LINK, `v4_v4link.md`) | 56,254 concordance rows (HS / CPC / ISIC / SITC), 29,055 class hierarchy edges, 6,848 Wikidata identity edges (722 items bridge ISO 3166-2 and NUTS) | Packs companies / health / anatomy / chemistry / genes / taxa not loaded (disk); NACE ↔ ISIC source retired |

### V5 — prediction (measured, not promised)

| Layer (agent, report) | What exists | Main gaps |
|---|---|---|
| Effect interface + estimates (PRED-EFFECT, `v5_effect_interface.md`, `v5_pred_effect.md`; migration `0019_effect_estimates`) | `kg.effect_estimate`, `/v1/effects/*`; **72 publication estimates** from 9 publications (verbatim quotes, shock profiles); the V6 plan counts **77** with the 5 REVA-run estimates | IMF working papers answer 403 (Choi et al. curated, not loaded) |
| Event studies / local projections (PRED-EVENTSTUDY, `v5_pred_eventstudy.md`) | `reva estimate` (`src/reva/estimation/`), 6 runs archived, **5 REVA-run estimates** written, 1 refused (pre-trend) | Quarterly panels thin; LV reproduction weak (Spearman 0.756) |
| Forecast registry (PRED-REGISTRY, `v5_pred_registry.md`) | `reva forecasts issue/resolve/backtest/status`; **586 sealed questions / 2,287 forecasts** (2,176 baselines + 111 attributed IMF WEO projections), cycle 2026-Q4 | 0 resolved yet (first resolutions as data arrive) |
| Vintages (PRED-VINTAGE, `v5_pred_vintage.md`) | **1,568,788 vintage cells** (WEO 37 vintages, RTDSM 244, US SPF 232, ECB SPF 111, OECD MEI 111), real-time backtests | ALFRED / FRED and IMF WEO historical files blocked |
| Daily markets (PRED-HF, `v5_pred_hf.md`) | 11 atlas tables, **2,023,365 rows** (ECB, BIS, US Treasury, EIA spot, BoE, NY Fed), 35.3 MB raw | NY Fed notice (now displayed, see §2), EIA spot licence review, LBMA / CME need licences |
| Decision engine (PRED-ENGINE, `v5_pred_engine.md`) | `python -m reva.predict.run --reference all`, `/v1/predict/*`, UI « Décision »; 5 reference decisions: ECB hike and sanctions give attributed, **unvalidated** scenarios, 3 stop with typed gaps | IO and retirement plugins had no pinned inputs (see §2 item 3) |

State of the dev DB summarised by the V6 plan: atlas ≈ 100 M values, KG ≈ 3.3 M statements, ≈ 65 k causal claims.

## 2. Integration fixed by V6 INTEG (`docs/dev/v6_integ.md`)

1. **measured_by bridge.** Two conventions coexisted: V4CORE's `atlas:<catalog concept>` and the
   `atlas:<source>.<dataset>` table entities of V4ECON / V4MIND / V4ECO (qualifiers `table`, `provider_geo`,
   `classif1`, `indicators`). `reva.kg.families.measured_by_target` resolves both; new
   `GET /v1/atlas/tables/{source}/{dataset}/cells` reads the subject's cells (region codes never become countries; a
   table missing from the snapshot is `in_snapshot: false`, never empty-as-zero); the Fiche shows both kinds and
   Neurones links atlas edges to the Fiche series.
2. **Provider notices.** `rights.required_notice` (from the table's recorded rights or `registry/sources/*.yaml`
   `rights.required_notice`) is served on every atlas cell, in `licences`, and as a `required_notice` warning; the UI
   quotes it verbatim (`RequiredNotices`). The New York Fed notice is declared for `nyfed.rates_d`.
3. **Belief release pinning.** Not a selection bug: the dev DB holds **no** FIGARO table and **none** of the
   retirement model's Eurostat datasets (`evidence.dataset` has only atlas tables, crisis episodes, coded relations,
   polygons) — V2 loaded them only in throwaway databases. In addition, `reva ingest --connector
   eurostat.figaro.iciot_ii` (CLI) never registered the dataset (only the worker job did): fixed. The loads were not
   run (free disk below the 5 GB floor at the time); the commands are in `docs/dev/v6_integ.md` §3.
4. **V4LINK families.** `corresponds_to` (concordance rows; table flags shown) and `same_as` (identity evidence;
   labelled as identity, never a merge) added and mapped; traversable in Neurones, the Fiche and path search.

## 3. Gaps (consolidated)

* **Disk**: most pending live loads (WHO GHE, GBIF facets, V4LINK packs, FIGARO + retirement datasets, vertebrate
  lists) wait for ≥ 5 GB free; DISKZ's `reva objects compress` frees space (verified zstd, sha256 of raw bytes kept).
* **Wikidata**: chemistry, energy, health, industry, ecology, culture specs not harvested (WDQS throttling); V6 WDOPS
  owns one harvest manager.
* **Lead operations still to run**: `reva atlas snapshot --warm` (pins V4/V5 tables: until then many new tables answer
  `in_snapshot: false`), `reva links materialise`, `reva links prune`, `reva causal refresh` (grade V4HEALTH's 14,508
  claims), then `reva beliefs publish` after the FIGARO / retirement loads, then
  `python -m reva.predict.run --reference all`.
* **No skill claimed**: 586 sealed questions, 0 resolved; every scenario is attributed and unvalidated.

## 4. Owner decisions (one list)

Accounts / keys / access:
1. IAEA NDS LiveChart access (Cloudflare challenge) — V4CHEM.
2. mindat.org account — V4CHEM.
3. IUCN token + licence; LPI data-use agreement — V4ECO.
4. Global Data Lab account (non-commercial licence) — V4ECON.
5. GEM tracker download form; Energy Institute allowed URL — V4ENERGY.
6. SEC EDGAR contact header; Open Supply Hub account; UNIDO INDSTAT terms — V4INDUSTRY.
7. WVS / EVS and Global Preference Survey registration; OpenAlex API key — V4MIND.
8. CCKP ADM1 geocode list (403) — V4CLIMATE.
9. GBD results (account, non-commercial) — V4HEALTH.
10. IMF working-paper PDFs and WEO historical files (403); ALFRED / FRED vintages — PRED-EFFECT, PRED-VINTAGE.
11. LBMA / CME licences for metals / futures — PRED-HF.

Licence reviews:
12. NIST SRD (CODATA), AMDC / IAEA disclaimer, CIAAW bylaws, Jefferson Lab notice, ChEBI CC BY vs CC BY-SA — V4CHEM.
13. Carbon Brief CC BY-NC-ND (attribution claims) — V4CLIMATE.
14. GloBI per-dataset licences; GBIF CC BY-NC share of counted records — V4ECO.
15. FAO FFPI non-commercial (also: keep FFPI questions private?) — V4ECON, PRED-REGISTRY.
16. IMF terms; OECD TiVA dataset statement; Hofstede research-only; How's Life? third-party cells; HPO — V4ECON,
    V4INDUSTRY, V4MIND, V4HEALTH.
17. EIA spot prices (Refinitiv / LSEG source); NY Fed notice text as now displayed — PRED-HF.
18. Laeven-Valencia / JST terms; quoting numbers from all-rights-reserved working papers — PRED-EVENTSTUDY, PRED-EFFECT.
19. Philadelphia Fed RTDSM / SPF ("all rights reserved") — PRED-VINTAGE.

Method / scope:
20. Keep 27.9 k weather stations as entities or only the 646 selected; ADM1 point-in-polygon rule — V4CLIMATE.
21. Facility ADM1 by coordinates; fuel vocabulary equivalence (GPPD vs USGS) — V4ENERGY.
22. DOID inheritance-mode axioms: claims or separate lane; ClinGen promotion to causal (no) — V4HEALTH.
23. GLEIF kinds (`legal_entity` vs `company`) — V4INDUSTRY.
24. GBIF 2026 backbone keys (P14607); GEM wiki ids as keys — V4BIO, V4LINK.
25. Curator ISO3 codes from printed names; maintainer review before PRED-ENGINE uses an estimate — PRED-EFFECT.
26. Step-response reading of IRFs, group scopes, choice rule, comonotone horizons, analogue anchors,
    `oil_price_assumption` — PRED-ENGINE §9.
27. Publish the forecast registry (pre-registration) or keep private; FAOSTAT `E` cells as outcomes; 6-month
    success criterion — PRED-REGISTRY.
28. Relation-family wording: generic family label vs source-specific wording in Neurones — V4CORE.
29. Machine: run the full-resolution loads on a bigger machine (V6 plan bottleneck 1).
