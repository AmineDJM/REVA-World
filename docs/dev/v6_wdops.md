# V6 — Wikidata harvest manager (agent WDOPS)

Status: stopped 2026-10-18 (session limit; free disk below the 5 GB ingest floor). Ran 2026-10-17 16:20 UTC →
2026-10-18 ~18:40 UTC. Single agent talking to Wikidata (WDQS + Wikibase API) for the
queued work, in priority order: (1) causal harvester queue → `reva kg import-harvest --from-db reva_kg_causwd` →
`reva causal refresh --origin live`; (2) pending property requests; (3) V4 domain specs; (4) WDRUN groups.

## Rules applied

* All traffic through `reva.adapters.connectors.wikidata.WikidataClient` (machine-wide lock
  `var/locks/wikidata.lock`, ≥ 2 s pacing, per-endpoint Retry-After honoured in full + 30 s, 10 min recovery
  pacing, generic project User-Agent with the project URL — no e-mail, no personal data).
* Budgets `wikidata.wdqs` 1,500/day and `wikidata.wbapi` 4,000/day: **one ledger** for every WDOPS run,
  `reva_kg_wdharvest.ops.budget_ledger` (new `--ledger-db` option of `python -m reva.kg.harvest run`; before,
  each throwaway load DB carried its own ledger, so the daily budget was enforced per database).
* Archive reuse (`--reuse-hours`): archived snapshots are served from `var/kg/objects`, citing their sha and
  original retrieval time; only the rest is requested.
* Disk: harvests (throwaway DBs, tens of MB) stop below 4,800 MiB free (≥ 5.0 GB in SI units); dev-DB imports run
  only with ≥ 5 GiB free. Harvest DBs created by WDOPS are dropped only after a successful, verified import.

## Code changes

| File | Change |
|---|---|
| `src/reva/kg/harvest.py` | `run --ledger-db <db>` / `budgeted_client(ledger_db=…)`: reserve, meter and settle in a shared ledger DB while loading into another throwaway DB |
| `src/reva/causal_harvest/{live,__main__}.py` | `run --share F`: explicit share of today's *global* remaining wbapi budget (replaces the per-day 30 % CAUSWD fairness share, designed for parallel agents); `run --min-free-mb N` (default 5 GiB, unchanged) |
| `src/reva/adapters/connectors/atlas_wdops_qlever.py` + `registry/kg/qlever_packs_wdops.yaml` (new) | `atlas.wdops_qlever`: the V4 *statements* specs (ecology, chemistry, health) read as statement packs from QLever (see below) |
| `src/reva/adapters/connectors/atlas_bio_taxonomy.py` (V4BIO, 1.0.2 → 1.0.3) | new source `atlas.bio_qlever`: V4BIO's identifier bridges / common names read from QLever (POST, 3,000 VALUES per query instead of 350, rank filtered in Python, QLever's lower-case statement IRIs normalised) |
| `tests/unit/test_wdops.py` | ledger routing (shared vs. load DB), share arithmetic, pack parsing (dedupe, deprecated, non-item objects, snapshot precedence), bio QLever query/parse |

## Property requests (priority 2)

`python -m reva.kg.harvest merge-property-requests` (2026-10-17 16:28–16:30 UTC, 3 EntityData requests):

* **P952 rejected — wrong property.** V4MIND requested P952 as "ISCO-08 occupation class"; Wikidata's own label is
  **"ISCO-88 occupation class"** (datatype string, raw `62e09566…`). The ISCO-08 property is **P8283** ("ISCO-08
  occupation class", datatype string, raw `e0e3d8a6…`). The request (`registry/kg/property_requests/v4mind.yaml`)
  and the spec `society.mind_isco08_occupations` now use P8283; P8283 merged as `isco08_code` (kind string).
  Left for V4MIND: the docstring of `src/reva/adapters/connectors/atlas_mind_occupations.py` still says P952
  (not edited: connector code digest).
* P642 (qualifier "of", A-LAW) rejected again: deleted on Wikidata.
* Every other request (13 files) was already merged.

## Paths

| Path | Used for | Cost |
|---|---|---|
| `python -m reva.causal_harvest run --share F` (EntityData / wbgetentities) | causal queue (priority 1) | ~1 wbapi request per entity (wbgetentities batches of 10 when the API answers) |
| `python -m reva.kg.harvest run … --db reva_kg_wdops --ledger-db reva_kg_wdharvest` | *seed* specs (entities with all allow-listed claims, revisions, references) | 1 wbapi request per entity + WDQS listing (≈ 1 answer per 13–17 min ban window) |
| `reva atlas ingest atlas.wdops_qlever --option packs=…` | *statements* specs (one query per pack) | 2 `qlever.wikidata` requests per run (60/day shared with V4LINK/V4BIO) |
| `reva atlas ingest atlas.v4link_qlever --option packs=…` | V4LINK identifier bridges | 43 requests reserved per run (settled on use) |

A QLever row is a Wikidata statement (same statement id) **without** entity revision, qualifiers or references:
recorded as `qlever@<time>`, `_revision.wikidata_revision = unknown`, references "unknown (not read)". A statement
the KG already holds from a Wikidata snapshot is not loaded twice (snapshot wins; the QLever copy is retracted on the
next run).

## Results per spec

### Priority 1 — causal queue (`reva_kg_causwd`)

| | |
|---|---|
| session | `kgcauswd-20261017T164718-de8df9`, 16:47–18:25 UTC, `--share 0.7` (cap 1,806 of the day's 2,581 remaining wbapi) |
| stop | `budget:in_run_requests_limit` — the shared ledger reached the 4,000/day `wikidata.wbapi` budget (1,090 CAUSWD requests + spec runs + other agents earlier in the day) |
| entities fetched | 1,406 → **3,086** (+1,680); the whole APIFIX queue (tiers 1–3: 659 claim endpoints incl. **Q8698 Great Depression**, 15 qualifier items, 467 places) is fetched; 10,172 discovery/listing items (tiers 4–8) remain |
| causal statements seen | 1,675 → **2,997** |
| import into `reva` (22:14 UTC) | 1,055 loads seen, 772 imported (260 already imported, 23 `wikidata.wdqs` row loads skipped by design); 1,652 raw objects copied; entities 1,662 inserted / 676 unchanged / 461 conflicting (same revision, other kind) / 25 stale; statements **10,678 inserted**, 7,159 unchanged; verification 24,528 identical / 3 differing (newer allow-list adds ISBN refs, as before) / 0 missing |
| `reva causal refresh --origin live` | 2,986 Wikidata claims scanned, **1,314 new**, 1,672 unchanged; 352 endpoints still without a KG entity (385 claims); grading over all 62,994 live claims: B 59,198 · C 221 · D 3,502 · E 73 |
| example | Great Depression `Q8698` → P828 *has cause* → Wall Street crash of 1929 `Q201684` (statement on Q8698, rev 2553658571, unreferenced) |

### Priority 3 — V4 specs

| spec / pack | path | rows | entities | statements | imported into `reva` | throttled / notes |
|---|---|---:|---:|---:|---|---|
| `science.chemical_elements` | harvest (EntityData) | 174 seeds | 274 (+ labels) | 3,562 | yes: 3,562 identical, harvest DB dropped | stopped by the wbapi day budget before the last batches (resumes) |
| `science.chem_petroleum_composition` | WDQS | — | — | — | — | WDQS `wdqs_timeout` (the `?prop wikibase:claim` join over `P279*`): moved to QLever pack `chem_petroleum` |
| `science.chem_element_discovery`, `chem_compound_identifiers` | harvest | — | — | — | — | not reached (dependency / budget) |
| `geography.ecology.main_food_source` → pack `eco_food` | QLever | 2,372 | 1,516 new | **2,326** P1034 (783 taxa → 891 food items; 2 deprecated, 1 non-item dropped) | yes (direct atlas load) | — |
| packs `eco_host, chem_petroleum, chem_elements, chem_materials, health_treat` | QLever | — | — | — | — | **HTTP 429 from qlever.dev** (22:10 UTC); queued again after 00:05 UTC |

### V4LINK bridge packs via QLever (`atlas.v4link_qlever`)

| pack | rows | items | statements inserted | entities new | ambiguity (never linked) |
|---|---:|---:|---:|---:|---|
| companies (P1278 LEI + P5531 CIK) | 4,510 | 2,164 | 4,078 | 2,134 | P1278: 2,018 linked, 203 ambiguous holders, 47 ambiguous values |
| health (ICD-10/11, MONDO, DOID) | 43,896 | 16,951 | 36,250 | 16,774 | P494: 1,577 linked, 1,344 ambiguous values, 913 ambiguous holders, 52 malformed (e.g. `C83.783.7`) |
| anatomy (P1554 UBERON) | 6,462 | 6,370 | 6,142 | 4,873 | 140 ambiguous values, 177 ambiguous holders, 2 malformed |
| chemistry, genes, taxa | — | — | — | — | refused by the `qlever.wikidata` budget (43 requests reserved per run, 18 used + 43 > 60): queued after 00:05 UTC |

## Conditions (throttling, budget, disk)

* **Wikibase API / EntityData** (`wikidata.wbapi`, 4,000/day): the binding limit. With the action API mostly
  answering 429, the clients fall back to `Special:EntityData` (1 request per entity, ~15–30 entities/min at the 2 s
  machine-wide pacing). 2026-10-17: the shared ledger reached 4,000 at 18:25 UTC (CAUSWD 1,196 + 1,090, spec runs,
  property merge, other agents earlier). 2026-10-18: 1,342 by 00:06 (causal 692, spec run 650 — 650 label snapshots
  of `science.chemical_elements` objects, `expand_hops: 1`) before the container restart.
* **WDQS** (`wikidata.wdqs`, 1,500/day): almost unused (≤ 20/day): one answer per ~13–17 min ban window
  (`429 Retry-After ≈ 1,000 s` renewed by global traffic on the shared egress); `science.chem_petroleum_composition`
  timed out (60 s) on the one answer it got.
* **QLever** (`qlever.wikidata`, 60/day, shared with V4LINK/V4BIO): 6 pack runs succeeded on 2026-10-17
  (V4LINK companies/health/anatomy/…, WDOPS `eco_food`), then `HTTP 429 from qlever.dev` at 22:10 UTC. A V4LINK run
  reserves 43 requests (all packs), so at most one V4LINK run fits a day next to the WDOPS/V4BIO runs.
* **Disk**: free space oscillated between 4.3 and 5.7 GB (other agents' loads, Postgres temp files); every dev-DB
  step waited for ≥ 5,120 MiB, harvests into throwaway DBs stopped below 4,800 MiB. The two container restarts
  (2026-10-17 ~20:20 and 2026-10-18 ~00:10 UTC) killed the background runs; state files made every run resumable.
* **Other Wikidata traffic seen**: V4BIO's `atlas.bio_wikidata` (WDQS, through the machine-wide lock) ran on
  2026-10-17 ~20:20 UTC from another session; the lead later handed it to WDOPS.

## Not done, and how to finish

Everything is resumable; nothing is lost when a run stops (harvest state per spec / per causal entity, archive
reuse, idempotent loads and imports).

1. **Causal queue** (9,510 left at tiers 4–8: discovery hits and WDQS listing items): one session per UTC day,
   `uv run python -m reva.causal_harvest run --share 0.6 --mode auto --min-free-mb 4800`, then
   `flock /tmp/reva-devdb.lock uv run reva kg import-harvest --from-db reva_kg_causwd` and
   `flock /tmp/reva-devdb.lock uv run reva causal refresh --origin live`. The six WDQS causal listings (22 requests)
   still wait for a WDQS answer window (`--cap-wdqs 30` tries once an hour).
2. **QLever loads** (dev DB, need ≥ 5 GB free; run right after 00:00 UTC for the fresh `qlever.wikidata` budget, in
   this order): `atlas.v4link_qlever --option packs=chemistry,genes,taxa` (V4LINK's last 3 packs),
   `atlas.wdops_qlever --option packs=chem_petroleum,chem_elements,eco_host,chem_materials,health_treat`,
   `atlas.bio_qlever --option max_queries=40` (V4BIO bridges, ~33 queries of 3,000 VALUES; then capture its fixture
   from the archived raw and drop `PENDING_LIVE_FIXTURE` in `tests/unit/test_bio_v4.py`),
   `atlas.wdops_qlever --option packs=eco_iucn,eco_endemic,eco_range` (largest: ~100k statements, ~150 MB in `reva`).
   Script: `<scratch>/wdops/qlever_day.sh`. On `429 from qlever.dev` retry the next day; do not raise the budget
   (QLever publishes no usage policy).
3. **Seed specs** (need entity snapshots; ≥ 7 GB free for the large ones): `python <scratch>/wdops/specs_driver.py
   <scratch>/wdops/groups_all.json` (V4: chemical elements/discovery/compound ids, energy sources, mind ×3 incl.
   ISCO-08 P8283, WHO EML, industry companies/factories, energy companies/sites/mines/power stations; then WDRUN:
   treaties/wars/colonies, `core.historical_states --force`, geopolitics, events, people, history). Load DB
   `reva_kg_wdops`, ledger `reva_kg_wdharvest`; import with
   `flock /tmp/reva-devdb.lock uv run reva kg import-harvest --from-db reva_kg_wdops`, verify, then drop
   `reva_kg_wdops`. At ~3,000 entities/day this is weeks of budget: a bigger egress quota or a Wikidata dump
   (owner decision) is the real fix.
4. **Not retried**: `atlas.ourairports`, `atlas.rcs_dem` (free disk 4.9 GB < 5 GB on 2026-10-18).

## Owner decisions

* **Throughput**: the Wikidata budgets are not the limit, the shared egress is (WDQS bans, action-API 429). Options:
  a dedicated egress IP, or a local Wikidata dump subset (JSON dump, ~130 GB compressed — a bigger machine).
* **QLever** as a regular path: in use since V4LINK (owner note of 2026-10-17); it has no published usage policy —
  keep 60/day unless the operators confirm more.
* **V4MIND P952**: was ISCO-88; fixed to P8283 (ISCO-08). The connector docstring in `atlas_mind_occupations.py`
  still mentions P952 (V4MIND to update with a version bump).
