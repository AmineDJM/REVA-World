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
| `kg.causal_claim_retraction` | A source dropped a claim: it leaves the current view. Nothing is deleted. |
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
`contributing`, `general`, `prevention`, `reaction`, `coded`).

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
| T3 | Wikidata causal statement with references | `load_kg_claims` |
| T4 | Wikidata causal statement without references | `load_kg_claims` |
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
                     # are retracted (removed_at_source). False = partial delivery, nothing retracted.
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
  contested by a source.
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
* **Grade**, first matching rule: E (temporal inconsistent) · E (contested, not corroborated) · A (T1/T2 +
  consistent + corroborated) · B (T1/T2) · B (T3 corroborated) · C (T3) · D (T4). Every grade row carries
  the rule ids (tier, temporal, corroboration, contest, grade rule) and en/fr reasons.

## HTTP API (`read` scope, RFC 9457 problems, envelope as `/v1/kg`)

| Endpoint | Data |
|---|---|
| `GET /v1/causal/events/{id}` | event: labels, kind, dates (start/end windows), counts of causes/effects by family and grade, context edges (labelled *not causal*), candidate count |
| `GET /v1/causal/events/{id}/causes` | claims with effect = id (`relation`, `family`, `min_grade`, `valid_from/valid_to` on the cause start, `limit` ≤ 500) |
| `GET /v1/causal/events/{id}/effects` | claims with cause = id (same filters) |
| `GET /v1/causal/events/{id}/why` | causes by lane (immediate / underlying / contributing / general / coded / reaction / prevention), mechanisms quoted, dissent |
| `GET /v1/causal/chain?from&to&max_hops≤6&min_grade&max_paths≤10` | causal chains from cause `from` to effect `to`, only causal edges |
| `GET /v1/causal/consistency` | self-loops, cycles, temporal violations, contested claims (counts + capped samples) |
| `GET /v1/causal/candidates?event=` | quarantined candidates, labelled "non sourcé — candidats" |

`origin` (`live` | `test_fixture` | `any`) as in `/v1/kg`. 404 for an unknown event, 422 for invalid ids or
bounds.

### Claim JSON (every list above)

```json
{
  "claim_key": "kg:wikidata:Q8698$…",
  "row_id": 41,
  "cause": {"id": "Q131074", "label": "Wall Street Crash of 1929", "kind": "event"},
  "effect": {"id": "Q8698", "label": "Great Depression", "kind": "event"},
  "relation": "has_cause",
  "relation_label": {"en": "has cause", "fr": "a pour cause"},
  "family": "cause",
  "lane": "general",
  "polarity": null,
  "stance": "asserts",
  "asserted_on": "effect",
  "source": {
    "source_key": "wikidata", "tier": "T3", "dataset": null, "source_revision": "2551267717",
    "statement_key": "wikidata:Q8698$…", "statement_id": "Q8698$…", "statement_row_id": 9,
    "selector": {"entity": "Q8698", "property": "P828", "statement_id": "Q8698$…"},
    "raw_sha256": "…", "references": [{"P854": ["https://…"]}], "citation": null, "codebook": null,
    "verification": "community_edited_not_verified"
  },
  "mechanism": null,
  "scope": {},
  "qualifiers": {},
  "grade": {
    "grade": "C", "rules_version": "1", "rules_sha256": "…",
    "rule_ids": ["tier.T3", "temporal.overlapping", "corroboration.single", "contest.none", "grade.C.t3_single"],
    "reasons": [{"rule_id": "grade.C.t3_single", "en": "…", "fr": "…"}],
    "temporal_check": "overlapping",
    "temporal": {"cause_start": {"from": "1929-10-24", "to": "1929-10-24", "basis": "P585", "precision": 11},
                 "effect_start": {"from": "1929-01-01", "to": "1929-12-31", "basis": "P580", "precision": 9}},
    "corroboration": 1, "corroborating_claim_keys": [], "upstream_groups": [["url:https://…"]],
    "contested_by": [], "graded_at": "2026-10-16T…"
  },
  "review_status": "unreviewed",
  "wording": "According to Wikidata (statement Q8698$…, community-edited), Great Depression has cause Wall Street Crash of 1929; REVA does not establish causation.",
  "wording_fr": "Selon Wikidata (…), Grande Dépression a pour cause Krach de 1929 ; REVA n'établit pas de causalité.",
  "origin": "test_fixture",
  "recorded_at": "2026-10-16T…"
}
```

`grade` is `null` until the grader has run on that claim row.

### Chain JSON

```json
{
  "from": "Q361", "to": "Q…", "found": true, "max_hops": 6, "min_grade": "D",
  "paths": [{
    "length": 2, "chain_grade": "C", "weakest_hop": 1,
    "hops": [{"from": {…}, "to": {…}, "hop_grade": "C", "claims": [<claim JSON>, …]}, …],
    "wording": "Composition by REVA of 2 separately asserted claims; chain grade = weakest hop (C). Each hop is asserted by its own source; REVA does not assert the chain."
  }],
  "note": "composition by REVA of separately asserted claims; chain grade = weakest hop",
  "search": {"levels_expanded": 2, "edges_scanned": 14, "truncated": false, "truncation_reasons": [],
             "elapsed_ms": 12, "time_budget_ms": 5000}
}
```

Hop grade = best grade among the claims for that hop (each listed with its own source and grade); chain
grade = weakest hop. Only minimal-length chains are returned (≤ `max_paths`, best chain grade first).
Ungraded claims are not traversed (`search.ungraded_skipped`).
