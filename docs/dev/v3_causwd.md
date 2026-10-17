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

## Results (2026-10-16 20:30 → 2026-10-17 01:43 UTC)

### Access and budget

| | 2026-10-16 | 2026-10-17 |
|---|---|---|
| CAUSWD wbapi share (30 % of remaining at first session) | 322 | 1,196 |
| wbapi requests used (ledger, settled) | 236 | 1,196 (one killed session settled by hand from its log: 35) |
| WDQS | reservation refused by the ledger (see finding) — 0 requests | 1 attempt at 00:53 → 429; next attempts are blocked by cool-downs that other agents renew (Retry-After 1,000 s) |

The action API (`wbgetentities`, CirrusSearch) answered 429 to almost every request on 2026-10-16 evening
(0 successes in 11 min); `Special:EntityData` flowed (~5/min on 10-16, ~25/min after midnight). Disk: DB
`reva_kg_causwd` 46 MB, archived raw (gzip) 11.5 MB; host free space fell to 6.5 GB (other agents) — the
harvester stops below 5 GB.

### Harvest

* **1,406 entities** snapshotted (1,105 seeds, 301 hop-1 neighbours; 886 of 1,165 recorded carry a
  start/end/point-in-time), every one with its `lastrevid`; 828 carry at least one causal statement.
* **1,675 causal statements** current (P1542 476, P828 444, P1478 383, P1479 147, P1536 121, P1537 104),
  1,671 with an entity object, 1,433 distinct (cause, effect) pairs, 229 pairs asserted by more than one
  statement (mostly the same link written on both items — one source, not corroboration).
* References: none 1,306 (78 %), "imported from Wikimedia project" only 198, referenced 168, scholarly
  (DOI/ISBN) 3.
* CirrusSearch event-filtered listings (`haswbstatement:<pid> haswbstatement:P580|P585 -haswbstatement:P279`):
  dated items carrying P1478 **454**, P1479 **133**, P1536 **1,415**, P1537 **81**, P828 **4,601**, P1542
  **1,386** (unfiltered P1478: 31,958, mostly diseases/concepts — that first page was deferred). 3,110
  items stay queued (`status`), resumable with the same command.

### Grades and consistency

CAUSCORE `refresh` ran on the first 389 claims (load 1/2): T3 112 / T4 277; grades B 2, C 97, D 273,
E 17; temporal consistent 65, overlapping 19, inconsistent 13, undetermined 292; corroborated 2, contested
4. **A later refresh over all 1,675 statements fails** (CAUSCORE bug, reported): a statement whose references
carry a DOI is tiered T2, and `kg.causal_claim`'s check only allows a statement row id on T3/T4, so the whole
transaction aborts. CAUSWD's own report (provisional reading of the roadmap rules, CAUSCORE grades used where
they exist): C 225, D 1,401, E 43, B 2; temporal consistent 378, overlapping 70, **inconsistent 57**,
undetermined 1,166 (762 of 1,663 nodes have a start date).

* Of the 57 time-inconsistent statements, **33 have an effect dated only by its inception (P571)** — "X has
  effect <state>" (World War I → German Empire / Ottoman Empire / Russian Empire / Austria-Hungary, WWII →
  Soviet Union / League of Nations): the effect is a polity, and the statement means its end or
  transformation; precedence on inception misleads (suggestion for CAUSCORE: use the end date when the
  effect is not an event, or report `undetermined`). The 24 others are genuine order problems at day
  precision, e.g. Truman Doctrine (1947) → Cold War (start 1945), Ethiopian Airlines 302 (2019-03-10) →
  Boeing 737 MAX groundings (start 2018-10-29), Iran–Iraq War (1980) → 1979 energy crisis, 2024 Russian
  presidential election → full-scale Russo-Ukrainian war (P828 on the war), Protestant Church in the
  Netherlands (2004) → Eighty Years' War.
* Self-loop: peace treaty (Q625298). **7 cycles**: Cold War ↔ Operation Barbarossa ↔ anti-communism ↔
  Eastern Front; violence ↔ conflict ↔ parental alienation; inflation ↔ Q13649246; People's Defense Units ↔
  Turkish operation in Afrin; Q1036897 ↔ 2022 US infant formula shortage; occurrence ↔ process ↔ state;
  January 2017 Central Italy earthquakes ↔ 2017 Farindola avalanche.
* Top-connected: Q19600530 (45 incoming P1537, all "contributing factor of" from individual US police-killing
  items), full-scale Russo-Ukrainian war 30, World War II 27, World War I 24, controlled flight into terrain
  20, Eastern Front 17, racism 17, Gaza War 17, American Civil War 17, COVID-19 17.

### Chains (every hop = Wikidata statements with id and revision; compositions are REVA's)

* Assassination of Archduke Franz Ferdinand → World War I: `Q192050$11fe2652…` (P1536 on the assassination,
  rev 2555974503) and `Q361$91cbeb80…` (P1478 on WWI, rev 2556293116), both unreferenced.
* World War I → Russian Revolution → dissolution of the Russian Empire: `Q361$3550a097…` (P1542, rev
  2556293116, ref = imported from azwiki only) then `Q8729$e50f92c2…` (P1542, rev 2556342729) mirrored by
  `Q12096626$56d5367a…` (P828); World War I → February Revolution: `Q101534$095aa975…` (P828, rev
  2555607989, ref = azwiki import URL).
* Wall Street crash of 1929 → Great Depression: `Q201684$35074256…` (P1542 on the crash, rev 2526486888,
  unreferenced); Great Depression → New Deal: `Q186356$b40cb442…` (P828 on New Deal, ruwiki import). The
  Great Depression item itself (Q8698) is still queued (its own P828 claims not yet read).
* World War I → Paris Peace Conference → Treaty of Versailles → World War II → Cold War → dissolution of the
  Soviet Union → Russo-Ukrainian war (6 hops, all D): exists only as a composition of unreferenced statements.
* Many long chains pass through class-level items ("war", "conflict escalation", "casus belli", "peace
  treaty", "occurrence/process/state"): suggestion for CAUSCORE/EVENTS — restrict chain search to event-kind
  nodes or flag class nodes.

Full detail: `var/kg/reports/causwd_report.json` (inconsistencies with dates and statement ids, cycles with
edges, 40 chains with every hop's statements, references and grades).

## Follow-ups

* Resume (`run` per UTC day; the per-day 30 % share is enforced across sessions via the ledger) for the 3,110
  queued items; WDQS listings when the shared WDQS ban lifts (one attempt per hour at most).
* WDHARVEST: fix the ledger operation-id collision (suggested task). CAUSCORE: T2 check violation; P143/P4656
  "imported from Wikimedia" references are pointers, not sources (currently T3); WDQS rows have unknown,
  not absent, references.
