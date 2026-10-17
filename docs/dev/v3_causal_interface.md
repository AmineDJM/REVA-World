# V3 causality — interface for other agents (CAUSCORE)

Agent CAUSCORE, 2026-10-16. Spec: `docs/roadmap/v3_causality.md`. This page is the contract that EVENTS,
CAUSWD, CAUSDATA, A-COUPS / A-CONTENTION / A-HAZARDS / A-ECRISES / A-SANCTIONS, CAUSCAND and CAUSUI code
against. Implementation notes and measurements are in `docs/dev/v3_causcore.md`.

**REVA asserts nothing.** Every causal edge is a *claim* made by a named source. REVA stores it, checks it
against time, counts independent corroboration, keeps dissent, and grades it A–E with declared rules.

## Storage (schema `kg`, migration `0015_causal`)

| Table / view | What |
|---|---|
| `kg.causal_load` | One row per write batch (`kind`: `kg_statements`, `dataset`, `grading`, `candidates`): source key, origin, raw sha256s (each a verified `meta.artifact`), retrieval time, loader version, parameters (codebook, rules version...). Server `recorded_at`/`recorded_by`. Rows of the tables below must be inserted in the transaction that created their load. |
| `kg.causal_claim` | Append-only claim versions (a changed claim = a new row). |
| `kg.causal_claim_retraction` | A claim left the current view (nothing is deleted). Reason, derived since FIXA (migration `0018_retraction_reasons`): `reclassified_by_reva` (the retracting load has the claim's raw bytes: the source still has the row), `removed_at_source` (row verified absent from a new revision), `absent_from_delivery` (raw changed, not checked), `no_longer_causal` / `source_statement_retracted` (KG claims); `detail` names the basis. |
| `kg.retraction_annotation`, `kg.retraction_reason_current` | Append-only corrected reasons of older retraction rows (claim and KG statement retractions), with evidence; the view gives each retraction's `effective_reason`. Written by `reva causal annotate-retractions`. |
| `kg.causal_grade` | Grades as their own rows: claim row, grading-rules version + sha256, grade A–E, rule ids, temporal check, corroboration, contesting claims, inputs sha256. A recompute writes rows only when the inputs changed. |
| `kg.causal_candidate` | Quarantined T5/T6 candidates (text-mined / model-proposed). Never mixed with claims. |
| `kg.causal_review` | Maintainer reviews (claims and candidates). **No runtime role may insert**: agents cannot record reviews. |
| `kg.causal_claim_current`, `kg.causal_grade_current`, `kg.causal_candidate_current` | Current views. |

Grants: `reva_worker` INSERT (not on `causal_review`); `reva_api`, `reva_sim` SELECT; `reva_gate` nothing.
Everything append-only (`meta.reject_mutation`), even for the owner.

## Vocabulary (`registry/causal/relations.yaml`)

Relations (closed): `has_cause`, `immediate_cause`, `underlying_cause`, `contributing_factor`, `has_effect`,
`enabled`, `precipitated`, `prevented_or_delayed`, `reaction_to`, `coded_trigger`, `coded_outcome`.
Each has en/fr labels read cause→effect and effect→cause, a *family* (`cause` or `prevention`; corroboration
is counted per (cause, effect, family)) and a *lane* for the "why" view (`immediate`, `underlying`,
`contributing`, `general`, `prevention`, `reaction`, `coded`, `outcome`). Since APIFIX (`docs/dev/v3_apifix.md`)
`coded_outcome` has its own lane `outcome` (how the event ended, never a cause); every relation of that lane must
be in `chain_excluded` (checked when the registry is loaded).

Direction is always normalised: a claim row has `cause_id` and `effect_id`. "X has effect Y" (P1542 on X) and
"Y has cause X" (P828 on Y) are both stored as cause X, effect Y; `asserted_on` says on which side the
source wrote it (`cause` / `effect`).

| Wikidata | Stored relation | cause | effect | asserted_on |
|---|---|---|---|---|
| P828 has cause | `has_cause` | object | subject | effect |
| P1542 has effect | `has_effect` | subject | object | cause |
| P1478 has immediate cause | `immediate_cause` | object | subject | effect |
| P1536 immediate cause of | `immediate_cause` | subject | object | cause |
| P1479 has contributing factor | `contributing_factor` | object | subject | effect |
| P1537 contributing factor of | `contributing_factor` | subject | object | cause |

P1344 (participant in) and P710 (participant) are **context**, never claims.

Entity ids: Wikidata QID (`Q8698`) or provider-scoped key (`icb:crisis:412`), same pattern as `kg.entity`.
Self-loops are stored (they exist in sources) and reported by `/consistency`.

## Source tiers

| Tier | Meaning | Who writes it |
|---|---|---|
| T1 | expert-coded research dataset, with a codebook definition | `load_dataset_claims` (CAUSDATA, A-*) |
| T2 | scholarly reference with a verbatim DOI/ISBN citation, only when it arrives through a source's own references | `load_dataset_claims(tier="T2", citation=...)`; the KG loader when a Wikidata reference carries a DOI (P356) or ISBN (P212/P957) |
| T3 | Wikidata causal statement with at least one source reference | `load_kg_claims` |
| T4 | Wikidata causal statement without a source reference: none, or only non-source pointers (P143 / P4656 imported from Wikimedia, P3452 inferred from, P887 heuristic; grading.yaml v2). WDQS rows (`wikidata.wdqs`, references never read) get no tier: the loader defers them (`deferred_counts["references_unknown"]`) | `load_kg_claims` |
| T5 / T6 | candidate space only: text-mined (T5) / model-proposed (T6) | `insert_candidates` (CAUSCAND) |

## Python API (`reva.causal`)

```python
from reva.causal import (
    Codebook, DatasetClaim, load_dataset_claims,      # T1/T2 dataset agents
    CandidateIn, insert_candidates,                    # CAUSCAND
    load_kg_claims, grade_claims, refresh,             # loader / grader (worker)
)
```

### `load_dataset_claims` (T1 / T2)

```python
rep = load_dataset_claims(
    engine,                      # reva_worker engine
    store,                       # artifact store: unregistered raws present in the store are registered
    source_key="icb.data",       # like kg source keys: ^[a-z0-9][a-z0-9_.-]*$
    dataset="icb2",              # dataset name within the source (^[a-z0-9][a-z0-9_.-]*$)
    dataset_revision="v16",      # source's version label (required)
    codebook=Codebook(           # required for T1: the definition the coded relation rests on
        name="International Crisis Behavior, actor-level codebook",
        version="16", url="https://...",
        definitions={"trigger": "verbatim definition text from the codebook"},
    ),
    raw_sha256s=[sha],           # raw artifacts the rows were parsed from (verified or in store)
    retrieved_at=retrieved_at,   # tz-aware
    origin="live",               # or "test_fixture"
    claims=[
        DatasetClaim(
            cause_id="icb:crisis:412:trigger", effect_id="icb:crisis:412",
            relation="coded_trigger",            # closed vocabulary above
            selector={"file": "icb2v16.csv", "row": 412, "columns": ["TRIGGR"]},  # dataset row selector
            raw_sha256=sha,                      # one of raw_sha256s (default: the only one)
            codebook_term="trigger",             # key in codebook.definitions (T1: required)
            source_claim_id="412:trigger",       # optional stable id; default sha of the selector
            polarity=None,                       # "promotes" | "inhibits" when the source states it
            mechanism=None,                      # verbatim source text only, never paraphrased
            scope={},                            # {"place": [...], "period": {...}, "population": "..."}
            stance="asserts",                    # or "denies" (a source denying the link)
            contests=(),                         # claim keys this source explicitly contests
            tier="T1",                           # "T1" or "T2" (T2 requires citation)
            citation=None,                       # T2: {"doi": "10...."} / {"isbn": "..."} verbatim
        ),
    ],
    complete=True,   # the delivery is the whole dataset: claims of (source_key, dataset) absent from it
                     # are retracted; reason derived (FIXA): reclassified_by_reva when its raw artifact is in this
                     # delivery, else source_row_present(stored claim) -> False removed_at_source / True
                     # reclassified_by_reva, else absent_from_delivery. False = partial delivery, nothing retracted.
    source_row_present=None,  # optional callable(stored claim: selector, raw_sha256, ...) -> bool | None
)
rep.as_dict()  # {"load_id", "inserted", "new_revision", "unchanged", "reinstated", "retracted",
               #  "rejected_counts", "rejected_sample", "unknown_entities", ...}
```

Claim key: `ds:<source_key>:<dataset>:<source_claim_id or sha(selector)[:32]>`. Idempotent: an identical
reload inserts nothing; a changed row (same key) is a new version; a claim that comes back after a
retraction is reinstated. Bounds: 100,000 claims per call. Rejections are per claim with a reason, never
silent. Entities need not exist yet in the KG (they are counted in `unknown_entities`); EVENTS/the dataset
agent loads them through `reva.kg.model.load_batch`.

### `insert_candidates` (T5 / T6, quarantined)

```python
rep = insert_candidates(
    engine, store,
    extractor_id="causcand.patterns", extractor_version="1",
    source_key="wikipedia.en", raw_sha256s=[sha], retrieved_at=dt, origin="live",
    candidates=[
        CandidateIn(
            cause_id="Q131074", effect_id="Q8698", relation="has_cause",
            tier="T5",                                   # T5 text-mined, T6 model-proposed
            source_revision="1234567890",                # required: revision-pinned
            source_sentence="exact sentence, verbatim",  # required
            selector={"page": "Great_Depression", "section": "Causes", "sentence": 3},
            raw_sha256=sha, polarity=None, mechanism=None,
        ),
    ],
)
```

`promotion_status` is `not_promoted` until a maintainer review (`kg.causal_review`, owner-only) — no API
or agent can change it. Candidates never appear in claim answers.

### Loader, grader (worker)

* `load_kg_claims(engine, *, origin=None, source_keys=None) -> ClaimLoadReport` — reads the causal
  properties of `kg.statement_current` (community-edited `wikidata*` sources), normalises direction, keeps
  statement key/id/row id, revision, raw sha256 and references verbatim; T3 with references, T4 without
  (T2 if a reference carries a DOI/ISBN). Idempotent; a statement that left the KG current view retracts its
  claim (`source_statement_retracted`). The qualifier P1310 (*statement disputed by*) marks the claim as
  contested by a source. A row the database refuses is rejected per statement (`rejected_counts`
  `db_refused:<constraint>`), never aborting the load (CAUSFIX, `docs/dev/v3_causfix.md`).
* `grade_claims(engine, *, origin=None) -> GradeReport` — applies `registry/causal/grading.yaml`.
* `refresh(engine, store=None, *, origin=None)` — `load_kg_claims` then `grade_claims`.

Dataset agents call `grade_claims` (or `refresh`) after their loads, or leave it to the worker.

## Grading (`registry/causal/grading.yaml`, versioned)

* **Temporal check** from KG dates with precision bounds (astronomical years): event start = P580 (start
  time), else P585 (point in time), else P571 (inception); end = P582, else P585, else P576. Each date is a
  window at its precision (year 1929 → 1929-01-01..1929-12-31; century widened one unit each side).
  `consistent`: the cause's start window ends before the effect's start window begins; `inconsistent`: the
  cause's start window begins after the effect's start window ends (beyond precision); `overlapping`: the
  windows overlap; `undetermined`: a date is missing or outside the supported range.
* **Corroboration**: number of independent source groups asserting the same (cause, effect, family) with
  stance `asserts`, same origin. Claims sharing an upstream (same reference URL, same "stated in" item,
  same reference hash, same dataset, same DOI/ISBN) count once; unreferenced Wikidata statements count as one
  "Wikidata editors" group, and not at all when a referenced Wikidata group exists.
* **Contestation**: `denies` claims on the same (cause, effect, family), claims whose `contests` names it,
  and P1310 qualifiers.
* **Version 2** (CAUSFIX / SAMEAS, `docs/dev/v3_causfix.md`): an endpoint dated only by P571 inception is an
  entity compared through its existence span (`undetermined` unless the whole span decides the order);
  P3680 (*statement supported by*), P5102 (*nature of statement*) and `scope.source_validity[].certainty =
  doubtful` cap the grade at D and are worded (claim JSON `attribution`); self-loops and cycle members carry
  `consistency.self_loop` / `consistency.cycle`; chains skip those, class items (P279 / P31-only targets) and
  `coded_outcome` (relations.yaml `chain_excluded`).
* **Version 3** (FIXA, `docs/dev/v3_fixa.md`): P1480 (*sourcing circumstances*) caps at D, is worded ("Wikidata
  qualifie : …") and is never chained (`nature.sourcing_circumstances`); P518/P1001/P1011/P1012/P1013 are worded
  (`scope.qualified`); a coded outcome dated by its cause's own end carries `temporal.reason =
  effect_dated_by_cause_end` and its `consistent` does not count toward A; a Wikidata T2 (DOI/ISBN reference)
  claim alone is capped at C (`grade.C.t2_wikidata_single`, `tier.T2.wikidata_reference`).
* **Grade**, first matching rule: E (temporal inconsistent) · E (contested, not corroborated) · D (attributed,
  nature-qualified, sourcing-hedged, source-doubtful) · C (Wikidata T2, not corroborated; v3) · A (T1/T2 +
  consistent + corroborated, not dated by the cause's own end) · B (T1/T2) · B (T3 corroborated) · C (T3) · D (T4). Every grade row carries
  the rule ids (tier, temporal, corroboration, contest, grade rule) and en/fr reasons.

## HTTP API (`read` scope, RFC 9457 problems, envelope as `/v1/kg`)

| Endpoint | Data |
|---|---|
| `GET /v1/causal/events/{id}` | event: labels, kind, dates (start/end windows), counts of causes/effects by family and grade (coded outcomes in their own bucket `outcome`, APIFIX), context edges (labelled *not causal*), candidate count |
| `GET /v1/causal/events/{id}/causes` | claims with effect = id (`relation`, `family`, `min_grade`, `valid_from/valid_to` on the cause start, `limit` ≤ 500) |
| `GET /v1/causal/events/{id}/effects` | claims with cause = id (same filters) |
| `GET /v1/causal/events/{id}/why` | causes by lane (immediate / underlying / contributing / general / coded / reaction / prevention; `outcome` apart), mechanisms quoted, dissent; `causal_claims` / `outcome_claims` counts |
| `GET /v1/causal/chain?from&to&max_hops≤6&min_grade&max_paths≤10` | causal chains from cause `from` to effect `to`, only causal edges |
| `GET /v1/causal/consistency` | self-loops, cycles, temporal violations, contested claims (counts + capped samples) |
| `GET /v1/causal/candidates?event=` | quarantined candidates, labelled "non sourcé — candidats" |

`origin` (`live` | `test_fixture` | `any`) as in `/v1/kg`. 404 for an unknown event, 422 for invalid ids or
bounds.

### Claim JSON (every list above)

Real example (fixture `tests/unit/causal/fixtures/wikidata_causal_fixture.json`, abbreviated):

```json
{
  "claim_key": "kg:wikidata.causal_fixture:Q8698$2d3e4107-4d1b-b343-6f93-a76055caa805",
  "row_id": 41,
  "cause": {"id": "Q201684", "label": "Wall Street crash of 1929", "label_fr": "krach de 1929", "kind": "event"},
  "effect": {"id": "Q8698", "label": "Great Depression", "label_fr": "Grande Dépression", "kind": "event"},
  "relation": "has_cause",
  "relation_label": {"en": "caused", "fr": "a causé"},
  "relation_inverse_label": {"en": "has cause", "fr": "a pour cause"},
  "family": "cause",
  "lane": "general",
  "polarity": null,
  "stance": "asserts",
  "asserted_on": "effect",
  "source": {
    "source_key": "wikidata.causal_fixture", "tier": "T4", "dataset": null, "source_revision": "2553658571",
    "statement_key": "wikidata.causal_fixture:Q8698$2d3e…", "statement_id": "Q8698$2d3e…", "statement_row_id": 9,
    "selector": {"entity": "Q8698", "property": "P828", "statement_id": "Q8698$2d3e…", "rank": "normal"},
    "raw_sha256": "…", "references": [], "citation": null, "codebook": null,
    "verification": "community_edited_not_verified", "retrieved_at": "2026-10-16T20:37:40Z"
  },
  "mechanism": null,
  "scope": {},
  "qualifiers": {"P3831": ["Q20204892"]},
  "contests": [],
  "disputed_by": [],
  "grade": {
    "grade": "D", "rules_version": "1", "rules_sha256": "…", "rules_current": true,
    "rule_ids": ["tier.T4", "temporal.consistent", "corroboration.single", "contest.none", "grade.D.t4"],
    "reasons": [{"rule_id": "grade.D.t4", "en": "Wikidata statement without references (community-edited, unsourced).",
                 "fr": "Déclaration Wikidata sans référence (édition communautaire, non sourcée)."}, "…"],
    "temporal_check": "consistent",
    "temporal": {"check": "consistent", "rule": "cause start window vs effect start window, at date precision",
                 "cause_start": {"from": "1929-10-24", "to": "1929-10-24", "basis": "P580", "precision": 11,
                                 "times": ["+1929-10-24T00:00:00Z"]},
                 "effect_start": {"from": "1929-10-29", "to": "1929-10-29", "basis": "P580", "precision": 11,
                                  "times": ["+1929-10-29T00:00:00Z"]}},
    "corroboration": 1, "corroborating_claim_keys": ["kg:wikidata.causal_fixture:Q201684$3507…"],
    "upstream_groups": [["community:wikidata"]], "contested_by": [], "graded_at": "2026-10-16T…"
  },
  "review_status": "unreviewed",
  "wording": "According to Wikidata (statement Q8698$2d3e…, revision 2553658571, community-edited), Great Depression has cause Wall Street crash of 1929; REVA does not establish causation.",
  "wording_fr": "Selon Wikidata (déclaration Q8698$2d3e…, révision 2553658571, édition communautaire), Grande Dépression a pour cause krach de 1929 ; REVA n'établit pas de causalité.",
  "origin": "test_fixture",
  "recorded_at": "2026-10-16T…",
  "load_id": 1
}
```

Since FIXA (`docs/dev/v3_fixa.md`): every ref (`cause`, `effect`, attribution values) has `label_loaded`; without a
KG label `label` is `"Q7747 (label not loaded)"` and `label_fr` `"Q7747 (libellé non chargé)"` (never a bare id,
never a hand-written label). `source.rights` is the asserting source's licence (registry rights, normalised like atlas
cells; `null` = no terms recorded) and the envelope adds a `licence_restricted` warning naming restricted sources.
`attribution` also carries `sourcing_circumstances` (P1480) and `scope_qualifiers` (P518/P1001/P1011/P1012/P1013,
each with its `property`). In `/why`, a claim whose cause or effect is a class carries `generic` and sits in the lane
`generic` (counted in `generic_claims`, not in `causal_claims`). Chain `search` adds `unloaded_nodes_skipped` /
`unloaded_nodes_sample`: entities the KG knows nothing about are never intermediate nodes.

`relation_label` reads cause → effect, `relation_inverse_label` effect → cause; the wording uses the side the
source wrote it on (`asserted_on`). `grade` is `null` until the grader has run on that claim row. Dates are
ISO 8601 with astronomical years (`-0043-03-15` = 15 March 44 BCE); `times` are the source's raw strings.

### Chain JSON

```json
{
  "from": {"id": "Q192050", "label": "assassination of Archduke Franz Ferdinand", …},
  "to": {"id": "Q178275", "label": "1918–1920 flu pandemic", …},
  "found": true,
  "paths": [{
    "length": 2, "chain_grade": "D", "weakest_hop": 0,
    "hops": [{"from": {…}, "to": {…}, "hop_grade": "D", "claims": [<claim JSON>, …]}, …],
    "wording": "Composition by REVA of 2 separately asserted claim(s); chain grade = weakest hop (D). Each hop is asserted by its own source; REVA does not assert the chain.",
    "wording_fr": "Composition par REVA de 2 affirmation(s) énoncée(s) séparément ; …"
  }],
  "paths_found": 1,
  "note": "composition by REVA of separately asserted claims; chain grade = weakest hop. …",
  "note_fr": "composition par REVA d'affirmations causales énoncées séparément ; …",
  "not_found_reason": null,
  "search": {"max_hops": 6, "min_grade": "D", "levels_expanded": 2, "edges_scanned": 16, "ungraded_skipped": 0,
             "below_min_grade_skipped": 4, "truncated": false, "truncation_reasons": [], "elapsed_ms": 7,
             "time_budget_ms": 5000}
}
```

Hop grade = best grade among the claims for that hop (each listed with its own source and grade); chain
grade = weakest hop. Only minimal-length chains are returned (≤ `max_paths`, best chain grade first).
Ungraded claims are not traversed (`search.ungraded_skipped`). Only `asserts` claims of the `cause` family are
traversed (prevention and denials never compose into a chain); self-loops are skipped. Bounds: `max_hops` ≤ 6,
`max_paths` ≤ 10, 5 s time budget, 5,000 frontier nodes and 20,000 edges per level, 2,000 enumerated paths;
hitting one sets `search.truncated` with the reason and an envelope `truncated` warning. Paths made of the same hops
(claim keys / same_as statement keys) are returned once (`search.duplicate_paths_merged`, APIFIX); a search to or
from a coded outcome record says that outcomes are never composed into chains.

### NLQA

`reva.causal.graph.why(conn, id, origin=, min_grade=)`, `effects(conn, id, origin=, limit=)` and
`chain(conn, a, b, max_hops=, min_grade=, max_paths=, origin=)` return the JSON above as plain dicts (the API
wraps them in the envelope). Answers must say "selon <source>" per hop (use `wording_fr`) and, for a chain,
the `note_fr`.
