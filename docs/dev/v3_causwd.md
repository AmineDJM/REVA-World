# V3 — Wikidata causal harvest (agent CAUSWD)

Owner files: `registry/kg/queries/causal/*.yaml`, `src/reva/causal_harvest/**`,
`tests/unit/test_causal_harvest.py`, this page. One-line edits elsewhere: `registry/kg/queries/_schema.json`
(domain enum `causal`), `registry/kg/queries/README.md` (owner row).

Mission: every Wikidata causal statement reachable from REVA's event seeds — P828 *has cause*, P1542 *has
effect*, P1478 *has immediate cause*, P1479 *has contributing factor*, P1536 *immediate cause of*, P1537
*contributing factor of* — with statement ids, entity revisions, qualifiers and references, loaded into a
throwaway KG database for CAUSCORE's loader/grader. Wikidata statements are community-edited: every edge is
"asserted by Wikidata", never a REVA statement.

## Paths

| Path | When | Cost |
|---|---|---|
| **WDQS listings** `registry/kg/queries/causal/*.yaml` — one query per property (`?item ?statement ?object ?rank`, deprecated filtered), P828/P1542 split by QID prefix (9 chunks each, response size) | at most one attempt per hour (state `wdqs_last_attempt`), never while another process holds a WDQS cool-down, `max_retries=0` (a 429 ends the attempt) | 22 requests for the whole listing |
| **Snapshots** of seeds and one causal hop, `--mode auto`: one `wbgetentities` (≤ 10 ids) try when the action API is not cooling down, else `Special:EntityData/<id>.json` per entity; archived EntityData < 24 h is reused without a request | always | 1 request per entity while the action API answers 429 |
| **CirrusSearch discovery** `haswbstatement:<pid>` (P828/P1542 with `-haswbstatement:P279`: no classes), `srsort=incoming_links_desc`, 500 hits/page | after the seed frontier is empty | 1 request per page |

The specs are `enabled: false` for the generic `python -m reva.kg.harvest run`: its statements path would
snapshot every subject of the listing (tens of thousands of requests). `python -m reva.causal_harvest` runs
them with bounded enrichment: rows whose subject has a snapshot come from the snapshot (revision, references);
rows that touch an item already reached from the seeds (fetched or queued) are loaded as WDQS rows
(`source_key=wikidata.wdqs`, revision explicitly unknown, **references unknown — not "unreferenced"**); the
other rows stay in the raw archive only (counts in the state file) — scope and disk budget.

## Seeds and expansion

Seed tiers (lower first; `core.prioritise` then orders by causal in-degree, best sitelink count of a
referring entity, search rank, QID):

| Tier | Seeds |
|---|---|
| 0 | KGHIST recorded snapshots (`registry/kg/queries/*/_recorded/snapshot_claims_*.json`): subjects and objects of causal claims |
| 1 | WDHARVEST KG (`reva_kg_wdharvest`, read only): subjects/objects of causal statements; hop-1 neighbours of tier 0 |
| 2 | WDHARVEST KG objects of P793 *significant event*, P1344 *participant in*, P607 *conflict*; recorded objects of those; explicit `VALUES ?item` QIDs of other specs |
| 3 / 5 / 7 | hop-1 neighbours of tiers 2 / 4 / 6 |
| 4 / 6 | CirrusSearch hits for P1478/P1536/P1479/P1537 / for P828/P1542 |
| 8 | items touching the seed reach in a WDQS listing |

One hop only: a fetched hop-1 entity's own causal claims are loaded (so its other causes/effects appear as
edges) but their far ends are not fetched. Bound 20,000 entities (`--max-entities`).

Loaded properties per entity (`core.HARVEST_PIDS`): the six causal ones + P31, P279, P361, P527, P155, P156,
P580, P582, P585, P571, P576, P577, P17, P276, P710, P793, P1344, P607 — what precedence checks and the event
view need (the full allow-list would cost ~100 statements/event against a 400 MB disk budget). **Causal
statements keep their full references** (stated in, URL, retrieved, imported from, quote P1683, pages, title,
author, DOI P356, ISBN P212/P957, PMID, archive URL, inferred-from/heuristic …) and causality qualifiers
(P1480 sourcing circumstances, P1310 disputed by, P5102 nature of statement, P3680, P7452, P1683 …); other
statements are extracted exactly like WDHARVEST's harvester.

## Budget, politeness, safety

* All requests go through `WikidataClient` (machine-wide lock, ≥ 2 s pacing, per-endpoint Retry-After,
  generic project User-Agent, raw archive in `var/kg/objects` + `var/kg/archive_index.jsonl`).
* Ledger: reservations in the shared WDHARVEST ledger `reva_kg_wdharvest.ops.budget_ledger` (only ledger
  rows; the running core harvest is not touched), **one operation id per source key**
  (`ingest:<run>-wbapi:1`, `ingest:<run>-wdqs:1`), metered per request, settled at session end.
* Share: a session's wbapi cap = 30 % of today's remaining `wikidata.wbapi` budget (4,000/day), where
  "used" = max(ledger wbapi account, archived successful responses on the Wikibase host today — all agents).
  A `CappedHttp` refuses beyond the cap before the transport. Sessions end at the UTC day boundary.
* KG loads go to `reva_kg_causwd` (created + migrated by `ensure_database`; `reva` is refused by name).
  Stop conditions: < 5 GB free disk, footprint (DB + my archived objects) > 380 MB.

### Ledger finding (reported, not fixed: WDHARVEST's file)

`reva.kg.harvest.budgeted_client` reserves `wikidata.wdqs` and `wikidata.wbapi` with the same
`job_id`/`attempt`, i.e. the same ledger operation id; `reserve_ingest` inserts with `ON CONFLICT DO NOTHING`,
so the wbapi reservation is dropped and all Wikibase traffic of every `reva.kg.harvest` command is booked to
the `wikidata.wdqs` account (2,174 requests booked to wdqs on 2026-10-16 by 20:20 UTC against a 1,500 limit,
almost all of it `Special:EntityData`). Effects: new WDQS reservations are refused for the rest of the day,
the wbapi account looks unused. CAUSWD therefore computes its share from the archive as well, and did not
send WDQS requests on 2026-10-16 (the ledger refuses them; bypassing it was not considered).

## Commands

```bash
uv run python -m reva.causal_harvest seeds                 # seed counts, no network/DB
uv run python -m reva.causal_harvest run [--discover P1478=1,P828=1,...] [--cap-wdqs 30] [--minutes N --linger]
uv run python -m reva.causal_harvest status                # var/kg/harvest_state/causal.causwd.json
uv run python -m reva.causal_harvest report                # var/kg/reports/causwd_report.json
```

## Results

(see below)
