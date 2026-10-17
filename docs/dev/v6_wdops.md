# V6 — Wikidata harvest manager (agent WDOPS)

Status: running (started 2026-10-17 16:20 UTC). Single agent talking to Wikidata (WDQS + Wikibase API) for the
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
| `tests/unit/test_wdops.py` | ledger routing (shared vs. load DB), share arithmetic |

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

## Results per spec

RESULTS_TABLE

## Conditions (throttling, budget, disk)

CONDITIONS

## Not done, and how to finish

TODO
