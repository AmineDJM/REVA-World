# V3 CAUSFIX — causal loader / grader fixes (grading.yaml version 2)

Agent CAUSFIX, 2026-10-17. Inputs: the findings of the Wikidata causal harvest (`docs/dev/v3_causwd.md`, end)
and, by scope extension, the grading-rule side of the independent audit `docs/dev/review_v3_causal_audit.md`
(H1, H2, H3, H4 grading part, L6, L7). Dataset connectors / loader data are AUDFIX's (`docs/dev/v3_audfix.md`),
`same_as` corroboration is SAMEAS's (`docs/dev/v3_sameas.md`); all three share **one `grading.yaml` version 2**
(changelog at the top of the file). No migration was added (head stays `0017_links_retention`).

**REVA still asserts no causation.** Every change below either tells the reader more exactly who asserts what,
or refuses to let weaker evidence look stronger. No claim was edited or deleted: changed claims are new claim
versions, every regrade is a new `kg.causal_grade` row (version 1 rows stay).

## What changed

| # | Defect | Fix | Where |
|---|---|---|---|
| 1 | A statement with a DOI/ISBN reference is T2; on `reva_kg_causwd` the INSERT violated `causal_claim_check` and **aborted the whole load** (1,675 statements) | Root cause: `reva_kg_causwd` (and `reva_kg_wdharvest_drift1016`, `reva_causcand`) were migrated with the **first** `0015_causal`, whose CHECK was `(tier IN (T3,T4)) = (statement_row_id IS NOT NULL)`; the file was later edited in place (commit `3e8ffdd`) to allow T2 statement rows, so the current code and the dev DB agree but those throwaway DBs do not. Code fix (no migration): claim rows are inserted under a savepoint; when the database refuses a batch it is split until the refused rows are isolated, the others are inserted and each refused row is **rejected with its reason** (`db_refused:<constraint>`) in `ClaimLoadReport.rejected_counts/sample`. A load where every row is refused leaves no empty load row. Same path for dataset claims. | `reva.causal.model._insert_claims`, `_close` |
| 2a / H1 | References that are only pointers made a statement T3 | A reference made only of **P143** (imported from Wikimedia project), **P4656** (Wikimedia import URL), **P3452** (inferred from), **P887** (based on heuristic), plus P813 dates, is not a source → **T4**, fact `tier.T4.non_source_references_only` | `grading.NON_SOURCE_REF_PIDS`, `grading.kg_tier`, `grader._notes` |
| 2b | WDQS rows (`wikidata.wdqs`: the listing query never selects references) were tiered T4 as if unreferenced | `kg_tier` returns `(None, "references_unknown")`; the loader **defers** the statement (no claim, no invented tier; `ClaimLoadReport.deferred_counts["references_unknown"]`); a claim already stored for it is kept, not retracted. It becomes a claim when an entity snapshot (`wikidata.wbapi`) of the statement is loaded. (0 WDQS causal rows exist today in `reva` or `reva_kg_causwd`; `kg import-harvest` skips WDQS loads.) | `model.Deferred`, `load_kg_claims` |
| 3 | Effect (or cause) dated only by its inception compared by its founding date ("WWI → German Empire" inconsistent) | Rule below; genuine order problems stay `inconsistent` | `reva.causal.temporal.temporal_detail` |
| 4 / M1 | Chains ran through class items ("war", "casus belli", "peace treaty") | `graph.class_nodes`: an item is a **class** when the KG has its own **P279** statement, or it is a **P31 target with no P31 of its own** (KG data, never names); class items are never intermediate chain nodes (endpoints the reader asked for are kept). `search.class_nodes_skipped/_sample`, `node_rule`, and the not-found reason say so. Items the KG knows nothing about are not called classes. | `reva.causal.graph.chain` |
| H2 | Mirror statements whose second "source" is an import / inference / the same quotation counted as independent | Upstream key of a non-source reference = `community:<root>` (the editing community: never independent, dropped beside a referenced group); `quote:<sha of normalised P1683 text>` added, so two references quoting the same text count once. P143 no longer yields `imported_from:<Q>`. | `grading.reference_keys` |
| H3 | P3680 "statement supported by" rendered "According to Wikidata …"; P5102 "nature of statement" ignored | Grade cap **D** (`grade.D.attributed`, `grade.D.nature_qualified`, facts `attribution.supported_by`, `nature.qualified`). Wording: "Wikidata (statement …) records that **<party>** asserts that …; Wikidata does not assert it itself" (fr: "indique que <partie> affirme que …"); P5102 values are shown verbatim ("Wikidata qualifies the nature of this statement as: …"), not interpreted. Claim JSON gains `attribution` (`supported_by`, `nature_of_statement`, `source_validity`, notes). NLQA: "selon Wikidata (…), qui attribue cette affirmation à <partie> (Wikidata ne l'affirme pas lui-même)". Both kinds are kept out of chain composition. | `graph.attribution_of`, `graph._wording`, `api/routes/causal.py` (`CausalClaim.attribution`), `nlqa/{ports,causal_adapter,compose}.py` |
| H4 (grading) | Claims on events the source itself codes as doubtful graded like definite ones | Generic: any `scope.source_validity[]` entry with `certainty: doubtful` (interface declared by AUDFIX; e.g. NCEI tsunami validity 0–2, cause code 2) → **D** (`grade.D.source_doubtful`, fact `certainty.doubtful`), the code and label are worded. No NOAA-specific code in the grader. | `grader._qualified`, `grading.yaml` |
| L6 | `coded_outcome` composed into chains | Decision: the codebooks (ICB OUTCOM, MID outcome, UCDP termination outcome, NAVCO success/progress, TIES/EUSANCT final outcome, COW war outcome) define the outcome as **how the event ended**, linked to an outcome record keyed under the event — not a separate event the first one brought about. It stays a sourced T1 claim (stored family `cause`, lane `coded`, worded "has coded outcome"), but `relations.yaml chain_excluded: [coded_outcome]`: never traversed by chains, never counted in cycles. (A stored `outcome` family would need a migration *and* re-delivery of every dataset; reports should keep counting it separately.) | `registry/causal/relations.yaml`, `vocab.chain_excluded` |
| L7 | Self-loop and cycles were ordinary claims | The grader flags self-loops (`consistency.self_loop`) and every asserted causal claim whose cause and effect lie in the same strongly connected component (`consistency.cycle`); grades are unchanged, claims stay stored and shown, `/consistency` still reports them, and **chain search never traverses them** (`search.quarantined_skipped`). Self-loops were already skipped by chains. | `grader.structural_flags`, `grading.CHAIN_QUARANTINE_FACTS`, `graph._edges` |

### Temporal rule for entities dated by inception (version 2)

An endpoint whose start comes from **P571 inception** (it has no P580 / P585) is an entity — state, polity,
organisation — not an event. It is compared through its whole **existence span** `[inception window start,
end window end (P576 / P582), open when absent]`, never through its founding date, and REVA does **not**
reinterpret the claim as "… its dissolution":

* `consistent` only when every possible cause time precedes every possible effect time (e.g. WWI 1914 →
  Weimar Republic founded 1918-11-09; a cause entity dissolved before the effect began);
* `inconsistent` only when every one follows (a cause founded after the effect began — Protestant Church in the
  Netherlands 2004 → Eighty Years' War; Truman Doctrine dated by P571 1947 → Cold War 1945; a cause starting
  after the effect entity was dissolved);
* otherwise `undetermined`, with `temporal.entity_dated_by_inception` and the reason "entity dated by inception
  (P571), not an event" in the grade's `temporal` detail (`cause_time` / `effect_time` with `kind:
  entity_existence`). WWI → German Empire [1871, 1918] / Russian Empire / Ottoman Empire / Austria-Hungary are
  `undetermined`, no longer E.
* When the effect is such an entity and the claim carries its **own** time qualifier (P585, else P580), that
  window is the effect time (`claim_time_qualifier`), compared as an event start. (0 such claims today.)
* Event-to-event claims are unchanged (start window vs start window); `overlapping` stays reserved for date
  precision.

## Results

### Throwaway clone of `reva_kg_causwd` (1,671 causal statements with an entity object)

`reva_kg_causwd` itself was not modified. Two clones (`createdb -T`):

* `reva_kg_causfix` keeps the stale CHECK: the **old** code aborts (`CheckViolation … causal_claim_check`, 0 of
  1,282 new claims written — reproduced). The **new** code loads 1,362 rows and rejects exactly 2 statements
  (`db_refused:causal_claim_check`: `Q84263196$2c6ebb7d…`, `Q55811$7c83b922…`, the two DOI-referenced T2
  statements), then grades 1,669 claims.
* `reva_kg_causfix_v1`: CHECK aligned to the current `0015_causal`; version-1 code run first (baseline), then
  version 2 (`reva.causal.refresh`): 214 new claim versions (tier / upstream changes), 1,671 new grade rows,
  1,779 version-1 grade rows kept.

| | version 1 (baseline) | version 2 |
|---|---|---|
| tier T2 / T3 / T4 | 2 / 366 / 1,303 | 2 / **179** / **1,490** |
| grade B / C / D / E | 22 / 311 / 1,279 / 59 | **9** / **154** / **1,481** / **27** |
| temporal consistent / overlapping / inconsistent / undetermined | 331 / 120 / 54 / 1,166 | 317 / 112 / **22** / 1,220 |

Transitions v1 → v2: temporal inconsistent → undetermined 32 (all effects dated by inception: WWI → empires,
WWII → Soviet Union / League of Nations …), consistent → undetermined 14 and overlapping → undetermined 8 (cause
entities founded before the effect: precedence of a founding date says nothing about when the entity acted);
grades C → D 166, E → D 26, B → D 10, E → C 6, B → C 3. Facts on version-2 grades: non-source references 187,
entity dated by inception 92, nature qualified 26, cycle 27 (21 distinct pairs), attributed 12, self-loop 1.
The 22 remaining inconsistencies are genuine order problems at day/year precision (Truman Doctrine → Cold War,
Ethiopian Airlines 302 → 737 MAX groundings, Iran–Iraq War → 1979 energy crisis, 2024 Russian presidential
election → full-scale war, Protestant Church in the Netherlands → Eighty Years' War, WWII → the Holocaust, …).

Chains (clone, `max_hops` 6): the audit's chain *full-scale Russo-Ukrainian war → … → war → peace process →
peace treaty → peace* (`Q110999040 → Q454`) is no longer composed (5 class nodes not traversed; the reason says
so). Sarajevo assassination → WWI and Wall Street crash → Great Depression still compose (grade D).

### Dev DB `reva` (origin live)

`flock /tmp/reva-devdb.lock uv run reva causal refresh --origin live` (02:22 UTC, after DAYOPS had already imported
the CAUSWD harvest with `reva kg import-harvest`: 1,672 Wikidata causal statements in `reva`). The KG load wrote
nothing new (1,672 unchanged: an earlier refresh by another agent had already run the new loader code from the shared
working tree, so the Wikidata tiers were already T2 2 / T3 179 / T4 1,491); 0 rejected, 0 deferred. The grader wrote **55,710**
version-2 grade rows (load 32; every current claim, including ~27k dataset claims added since the last grading),
all version-1 rows kept.

| Wikidata (KG) claims, origin live | version 1 (latest v1 row per claim) | version 2 |
|---|---|---|
| grade B / C / D / E | 22 / 311 / 1,280 / 59 | **9** / **154** / **1,482** / **27** |
| temporal consistent / overlapping / inconsistent / undetermined | 333 / 120 / 54 / 1,165 | 319 / 112 / **22** / 1,219 |

Wikidata grade transitions: C → D 166, E → D 26, B → D 10, E → C 6, B → C 3. Facts on current grades:
`tier.T4.non_source_references_only` 187, `temporal.entity_dated_by_inception` 94, `nature.qualified` 26
(24 decided by `grade.D.nature_qualified`, the others E first), `attribution.supported_by` 12 (8 decided by
`grade.D.attributed`), `consistency.cycle` 27, `consistency.self_loop` 1 (claim 26888, peace treaty).
`certainty.doubtful`: 0 so far — no current claim carries `scope.source_validity` yet (AUDFIX's NOAA re-delivery
had not reached the dev DB at this refresh; the next refresh after it grades those claims D).

All claims, all sources: A 604 / B 53,351 / C 154 / D 1,482 / E 119 (55,710). The 302 dataset claims that moved
B → A come from SAMEAS's same_as corroboration (`corroboration.via_same_as` 604), not from these rules.

Audit ids on the dev DB after the refresh: 26791, 26817, 26818 T4 D (azwiki imports; 26818 WWI → German Empire
`undetermined`, no longer E); H2 pairs 27138/27363, 27157/27377, 27346/27350, 27372/27144, 27064/27076 all
corroboration 1 (no more B); 27214/27568 D (`nature.qualified`), corroboration 1; 27009, 27057, 27278 D
(`grade.D.attributed`), 27145, 27153 E (contested first, attribution fact attached); 26888 flagged self-loop.
Wording of 27009: "Wikidata (statement Q15860072$5683e19c…, revision 2556162208, community-edited) records that
Q7747 and Q1140115 assert that Russo-Ukrainian war has cause eastward expansion of NATO; Wikidata does not assert
it itself, and REVA does not establish causation." (the two parties have no KG label yet, so they are named by
QID). The audit's chain `Q110999040 → Q454` is no longer composed on the dev DB (5 class nodes not traversed).

## Tests

* `tests/unit/causal/test_causfix.py` (22): tiers incl. WDQS deferral, H2 corroboration shapes (import +
  inference, URL + inference, same quotation), temporal entity rule (German Empire, Weimar, Protestant Church,
  Truman Doctrine, claim qualifier), D caps, fact order, self-loop / cycle flags with outcome relations excluded.
* `tests/integration/test_causfix_store.py` (4): a CHECK refusing one statement (the stale shape) → 2 inserted,
  1 rejected with its constraint name, reload writes nothing, no empty load row; WDQS deferral; pointer
  references → T4 / `community:wikidata` / D with the fact; P3680 / P5102 wording and chain quarantine; classes
  (P279, P31-only target), cycles and self-loops skipped by chains.
* Updated to version-2 semantics: `test_causal_grading.py` (version 2, all 2⁴ flag combinations decided,
  reference keys), `test_causal_store.py` (the inconsistency case now uses an event start), `test_causal_api.py`
  (a synthetic 1947 → 1945 order problem stays a violation, WWI → empires no longer), `test_causal_wikidata_demo.py`
  (WWI → Russian Empire: T4 by azwiki import, `undetermined`, span 1721-11-02 .. 1917-09-14, D).
* Unit `tests/unit/causal` + `test_causal_harvest.py` + `tests/unit/nlqa`: all pass except
  `test_nlqa_causal.py::test_ui_fixture_is_the_current_pipeline_output`, which differs only by `catalog_digest`
  (atlas catalog concepts added by other agents; unrelated, not regenerated here). Integration causal suites
  (`test_causal_store`, `_api`, `_wikidata_demo`, `_ops`, `test_causdata_ingest`, `test_causfix_store`): pass.
  `ruff check`, `ruff format --check`, `mypy` (project config): clean. `contracts/openapi.json` regenerated
  (`CausalClaim.attribution`, `ChainSearch.quarantined_skipped / class_nodes_* / node_rule`).

## Audit ids closed (grading-rule side)

| Audit id | Status |
|---|---|
| H1 | closed: P143/P4656/P3452/P887-only references → T4 (`tier.T4.non_source_references_only`) |
| H2 | closed for the listed pairs' mechanism: non-source references are `community:wikidata`, P1683 quotations keyed by text (27138/27363, 27157/27377, 27346/27350, 27372/27144 → one group; 27214/27568 → one group via the shared quotation). 27064/27076 (ruwiki import vs JSTOR article) now counts once too: the import side is the community, dropped beside the referenced JSTOR group. |
| H3 | closed: P3680 / P5102 honoured in grade (D cap + facts), wording (API, why, chains, NLQA) and chain composition |
| H4 | grading part closed: generic `scope.source_validity[].certainty = doubtful` → D (`grade.D.source_doubtful`); takes effect when AUDFIX's connector re-delivers the NOAA claims with the qualifier |
| L6 | decided and documented: `coded_outcome` not traversed by chains nor counted in cycles; storage family unchanged |
| L7 | closed: self-loops / cycle members flagged on the grade, quarantined from chains, still reported |
| M1 (part) | chains do not traverse class items; class-level claims stay stored and graded |

## Follow-ups

* Throwaway DBs with the stale first `0015_causal` CHECK (`reva_kg_causwd`, `reva_kg_wdharvest_drift1016`,
  `reva_causcand`): the loader now rejects their T2 statement rows per statement; recreate them from the current
  migrations if T2 rows matter there. `0015_causal` must never be edited in place again (a schema change is a new
  migration).
* P5102 values are shown verbatim and uniformly capped at D; a registry of natures (often / rarely vs alleged /
  hypothetical) could refine that.
* Reports and UI totals should count `coded_outcome` apart from causal claims (audit L6, display side).
