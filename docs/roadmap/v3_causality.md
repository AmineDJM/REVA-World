# REVA World V3 — events and causality

Owner request (2026-10-16): more data, more links between events, and a very robust causal architecture
between events — "ne force pas ce lien mais crée son architecture". REVA never manufactures causation. It
builds the structure that holds every causal claim a source makes, grades it, checks it against time, keeps
competing claims side by side, and shows the reader exactly who asserts what.

## Principles (additions to the data constitution and `v3_world_graph.md`)

1. **A causal link is a claim, never a fact.** Every edge has an asserting source; REVA asserts nothing.
2. **Temporal precedence is necessary, not sufficient.** A cause that starts after its effect (beyond date
   precision) is flagged `temporal_inconsistency`, graded down, and shown — never silently corrected or
   dropped. Precedence alone never creates a claim.
3. **Co-occurrence, sequence, participation and correlation are not causation.** They are *context* edges
   (`part_of`, `follows`, `participant`, `located_in`, `same_period`) in a separate family and never shown
   with causal wording.
4. **Causal pluralism.** Events have many causes. All sourced claims coexist; disagreement and dissent are
   kept as explicit `contests` relations, never resolved by REVA.
5. **No probabilities.** Strength is an ordinal *evidence grade* computed deterministically from declared
   rules and always shown with its reasons.
6. **Chains are compositions.** A path A→B→C through two claims is REVA's composition of two separately
   asserted claims; the UI and API say so. Chain grade = weakest hop. Maximum 6 hops.
7. **Candidates are quarantined.** Text-mined or model-proposed causal links live in a separate candidate
   space, never mix with claims, never appear in answers unless explicitly requested and labelled.
8. **Counterfactuals are never generated as facts.** Only sourced counterfactual statements may be quoted.

## Model

**Event** (`kg.entity` with kind `event` + the event view): canonical id (Wikidata QID when one exists,
else `<source>:<id>`), type from `registry/events/taxonomy.yaml` (war, battle, revolution, coup, treaty,
election, crisis (financial, political, international), disaster (by hazard), epidemic, protest campaign,
sanction episode, discovery, voyage, independence, reform, assassination, migration wave, …), time interval
with precision (astronomical years), places, participants with roles (actor, side, target, victim), and
structural relations `part_of`/`has_part`, `follows`/`followed_by`. The same real event coming from several
sources is linked by explicit `same_as` statements with evidence (shared identifier, or an evidenced
mapping table) — never merged by fuzzy name/date matching.

**Causal claim** (`kg.causal_claim`, append-only, current view):
- `cause` and `effect` (event or entity ids; conditions such as "drought" or "oil price" are allowed as
  entities when the source names them);
- `relation` from a closed vocabulary: `has_cause`, `immediate_cause` (trigger), `underlying_cause`,
  `contributing_factor`, `has_effect`, `enabled`, `precipitated`, `prevented_or_delayed`, `reaction_to`,
  `coded_trigger` (expert-coded dataset trigger), `coded_outcome`; each mapped to inverse labels fr/en;
- `polarity` (`promotes` / `inhibits`) when the source states it;
- `asserting_source` (source key, dataset/revision, statement id or dataset row selector, raw sha256) and
  its **source tier**:
  - **T1** expert-coded research dataset with a codebook definition (e.g. ICB crisis trigger, MID onset,
    UCDP incompatibility, crisis datasets);
  - **T2** scholarly reference with a citation stored verbatim (DOI/ISBN) — only when such a reference
    arrives through a source's own references;
  - **T3** Wikidata causal statement *with* references;
  - **T4** Wikidata causal statement without references;
  - **T5/T6** (candidate space only) text-mined from Wikipedia (T5) / model-proposed (T6).
- `mechanism` text quoted verbatim from the source when present (never paraphrased by REVA);
- `scope` qualifiers (place, period, population) when the source gives them;
- `temporal_check`: `consistent`, `overlapping`, `inconsistent`, `undetermined` (missing dates), computed
  with date precision bounds;
- `corroboration`: number of *independent* sources asserting the same (cause, effect, relation family);
  sources sharing an upstream (e.g. two Wikidata statements citing the same reference) count once;
- `contested_by`: ids of claims that deny or compete with it, when a source says so;
- `evidence_grade` **A–E**, deterministic: A = T1/T2 + consistent time + corroborated; B = T1/T2 or
  T3 corroborated; C = T3 single; D = T4; E = any claim with `temporal_check = inconsistent` or contested
  without corroboration. The rule table lives in `registry/causal/grading.yaml` (versioned) and every grade
  carries the list of rule ids that produced it.
- `review_status`: `unreviewed` until a maintainer review (agents cannot record reviews).

**Causal candidate** (`kg.causal_candidate`, separate table): same fields + extractor id/version, the
exact source sentence and its revision, and a `promotion_status` that only a human review can change.

## Services

- Loader: KG causal statements (P828 has cause, P1542 has effect, P1478 immediate cause, P1479 contributing
  factor, P1536 immediate cause of, P1537 contributing factor of, plus P1344/P710 as context only) and
  dataset-coded relations → claims, idempotent, with retraction when the source drops a claim.
- Grader: recomputes grades when inputs change (new corroboration, new dates) — new rows, never edits.
- Graph services: causes/effects of an event (by relation family, grade, period), "why" explanation
  (immediate vs underlying vs contributing, mechanisms, dissent), causal chain search between two events
  (only causal edges; context edges may be shown alongside but never counted), consistency report (cycles,
  temporal violations, self-loops), and causal neighbourhood for the UI.
- API: `/v1/causal/events/{id}`, `/v1/causal/events/{id}/causes`, `/effects`, `/why`,
  `/v1/causal/chain?from&to`, `/v1/causal/consistency`, `/v1/causal/candidates` (separate, labelled).
- NLQA: `why(event)` and `causal_chain(a, b)` operations; answers name the asserting source of every hop and
  say "selon <source>"; a chain answer says the composition is REVA's.
- UI: "Causes & conséquences" screen (graded causal graph, immediate/underlying lanes, timeline alignment,
  dissent panel, candidate space toggle clearly labelled "non sourcé — candidats").

## Work split (V3 causality agents)

| Agent | Owns |
|---|---|
| CAUSCORE | migration `0015_causal.py`, `src/reva/causal/**` (model, loader, grader, graph services), `registry/causal/**`, `src/reva/api/routes/causal.py` (+1 router line), tests |
| EVENTS | `registry/events/taxonomy.yaml`, `src/reva/events/**` (event view, same_as linking, timeline), `registry/kg/queries/events/**` |
| CAUSWD | Wikidata causal harvest around event seeds (API/EntityData mode while WDQS throttles), `registry/kg/queries/causal/**` |
| CAUSDATA | expert-coded datasets with triggers/outcomes: ICB, COW MID, UCDP incompatibility/termination, crisis datasets → events + T1 claims (`src/reva/adapters/connectors/atlas_icb.py` etc.) |
| A-COUPS, A-CONTENTION, A-HAZARDS, A-ECRISES, A-SANCTIONS | new event datasets (coups/leaders, protest campaigns, natural hazards, economic crises, sanctions) with their own coded relations |
| CAUSCAND | candidate space: pattern-based extraction from Wikipedia article text (revision-pinned), never promoted |
| CAUSUI | `ui/src/v3/**` causality screen + NLQA `why`/`causal_chain` |
