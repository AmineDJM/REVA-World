# REVA World V6 — plan update (2026-10-17)

Owner request: a complete, sourced copy of the world in every domain (diseases, wars, medicines, finance,
energy, culture, …) and highly effective predictions in every domain, finance included. REVA's answer: build
the most complete *sourced* model possible and make every prediction *prove* its skill against strong
benchmarks before it is trusted (`docs/strategy/world_map.md` §5). Nothing below promises skill in advance:
skill is measured by the forecast registry (sealed forecasts, proper scores, as-of vintages).

## State after V4/V5 (dev DB, 2026-10-17)

Atlas ≈ 100 M values (now with monthly/quarterly/daily series, regions NUTS/OECD/ADM1, vintages); KG ≈ 3.3 M
statements (chemistry, anatomy, diseases, genes in progress, species interactions, ecoregions, power plants,
mines, companies/LEI, regions, weather stations); ≈ 65 k causal claims; 77 effect estimates; 575 sealed
questions / 2,243 baseline forecasts; decision engine running with attributed, unvalidated scenarios.

## Bottlenecks (honest)

1. **Disk (~5 GB free in this container)** — blocks most live loads. Fix: transparent compression of the raw
   archive (V6 DISKZ) and, for full-resolution sources, a bigger machine (owner: run Claude Code on a larger
   computer via Claude Desktop or `claude remote-control`).
2. **Wikimedia throttling** — one long-running harvest manager (V6 WDOPS) instead of every agent retrying.
3. **Accounts/keys/licences** — listed per report; no workarounds.
4. **Dev-DB single writer** — `flock /tmp/reva-devdb.lock`; keep loads small and sequential.

## V6 agents (wave 1)

| Agent | Goal |
|---|---|
| DISKZ | zstd-compressed object store with sha256 of the *raw* bytes unchanged, transparent reads, verified migration of existing objects; frees GBs |
| INTEG | integration fixes from V4/V5 reports: measured_by bridge (concept vs table entities), NY Fed notice display, release pinning FIGARO + retirement inputs, red tests, docs |
| WDOPS | single Wikidata harvest manager for all queued specs (causal queue, chemistry, energy, health, industry, ecology, culture), throttle-aware, imports + refresh |
| PRED-MODELS | real forecasting models (AR/ARDL with vintages, BVAR/dynamic-factor nowcasts) in the tournament vs WEO/SPF on vintage backtests; only winners join the registry |
| PRED-FIN | finance: questions + baselines for FX, yields, policy rates, Brent, spreads; random-walk/efficient-market benchmarks; honest evaluation (no trading claims) |
| PRED-ENERGY | energy price/production forecasts benchmarked against EIA STEO (incl. STEO archive vintages) |
| PRED-CONFLICT | conflict-risk forecasting (UCDP GED / ViEWS benchmark), registry questions, as-of backtests |
| PRED-HEALTH | disease burden / outbreak forecasting benchmarks (WHO GHE trends, outbreak data), registry questions |
| EFFECTS-MORE | 100+ more attributed effect estimates across domains (climate damages, health interventions, trade, energy, conflict economics) with verbatim quotes |
| V6DRUGS | medicines: WHO EML, ChEMBL (mechanisms, targets, indications), openFDA, ClinicalTrials.gov — linked to V4 chemistry/diseases/genes |
| V6CONFLICT | wars & security data: UCDP GED georeferenced events (ADM1), military/arms where open, peace processes |
| V6FINDATA | finance data: open equity/bond indices, central-bank balance sheets, BIS credit, IMF COFER, sovereign ratings where open |
| V6CULTURE | culture: films, books, music, art, heritage (MusicBrainz CC0, Open Library, UNESCO stats, Wikidata specs) |
| V6INFRA | transport & logistics: World Port Index, UN/LOCODE, airports (OurAirports retry), shipping/trade routes where open |
| V6SIGNALS | daily event/news signals (GDELT aggregated counts) as predictors, licence-checked |
| AUDIT3 | adversarial honesty audit of all V4/V5 layers (provenance, licences, name-matching, projections vs observations, causal wording) |

Then: lead final pass (atlas snapshot, links, causal refresh, full sweep, data snapshot on `reva-data-*`).
Common agent rules: scratchpad `v4_common.txt` (restated in each brief). Migrations: DISKZ may add
`0020_*` only if unavoidable; nobody else.
