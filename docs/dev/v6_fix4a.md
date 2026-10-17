# V6 FIX4A — AUDIT3 fixes H1, H2, M1, M9, L8 and grading (M2)

Agent FIX4A, 2026-10-17. Input: `docs/dev/review_v4_v5_audit.md` (AUDIT3). Scope: the V4HEALTH ontology / ClinGen
connectors and the V4CLIMATE / V4ECO name-keyed identities. Every change is append-only: dropped claims and
statements are retracted by the loaders (claims: reason `reclassified_by_reva`), nothing is edited or deleted.

## Status per audit id

| id | fix | dataset state (dev DB `reva`, origin `live`) |
|---|---|---|
| **H1** | Mondo (and DOID, if present) axioms with RO:0004021 / RO:0004020 / RO:0004030 (filler = cause) and RO:0004024 / RO:0004025 (filler = effect) no longer make the normal process or structure the cause or effect. The endpoint is a REVA-made node `reva_state:<state>:<filler>` (kind `reva_altered_state`, label e.g. "disruption of GO:0003824", description "REVA-made node, not a class of Mondo …"), linked to the filler by `mondo:altered_state_of` (family `context`). The verbatim source relation and its label stay in the selector (`relation`, `relation_label`, `filler`, `reva_node`, `reva_node_rule`). | **done**: 919 claims re-keyed (823 audit H1 + the 96 subject-side "disease causes disruption / dysfunction of" claims with the same defect): 919 new claims, 919 old ones retracted `reclassified_by_reva`; 671 REVA-made nodes + 671 `mondo:altered_state_of` statements; 0 claims of these relations with a bare filler endpoint |
| **H2** | ClinGen: only Definitive / Strong / Moderate / Limited stay `clingen:gene_disease_validity` (`associated_with`); Refuted / Disputed / No Known Disease Relationship become `clingen:gene_disease_curation_negative` (statement id `<assertion>#curation_negative`, classification verbatim, **unmapped** in the families: never a hop). Any other classification blocks the load (drift). `atlas.clingen` 1.1.0. | **code done, DB load queued**: free disk was 4.3 GB (< 5 GB rule) when the chain reached it. The live DB still has 296 negative curations under `associated_with` until the queued ingest runs (command below). |
| **M1** | DOID `IDO:0000664` "has material basis in" with a `GENO:` filler → KG statement `doid:inheritance_mode` (family `context`; qualifiers `source_relation`, `source_relation_label`), never a claim. Owner decision 6 of V4HEALTH closed. | **done**: 3,751 claims retracted `reclassified_by_reva`; 3,751 `doid:inheritance_mode` statements |
| **M9** | `RelationIndex.is_causal` reads no label word: causal = under `RO:0002410` in the archived `ro.obo`, or on the explicit allow-list `DEFINED_AS_CAUSATION` (only `IDO:0000664`, whose archived definition must still contain "Relating the disease to it’s causation", else drift). `RO:0007001` "has disease driver" (parent `RO:0007000`) → `doid:has_disease_driver` statement, **unmapped** in the families (QA-DATA's wording check reads "driver" as causal wording, so it is neither a claim nor a hop). Relations caught only by the label rule before and never loaded (Mondo `predisposes_towards` 501, `disease_triggers` 2, …) are now counted as `relation_not_loaded`. | **done**: 61 claims retracted `reclassified_by_reva`; 61 unmapped `doid:has_disease_driver` statements |
| **L8** | Neither source publishes an id: the Carbon Brief CSV has no event id column, RESOLVE 2017 has only the `REALM` name. Each REVA-made entity now carries an identity-rule statement: `cbattr:identity_rule` (948 extremes / trends; "REVA-made identifier: first 16 hex digits of the sha256 of the exact 'Name' text …; two events sharing a name would be merged and one event spelled two ways would be split") and `resolve:identity_rule` (8 realms). Entity descriptions are unchanged: same source revision + new content is refused by the KG loader as `conflicting`, so the rule is a new statement. `atlas.attribution_cb` 1.1.0, `atlas.ecoregions` 1.1.0. | **code done, DB load queued** (disk < 5 GB) |
| **M2** | `reva causal refresh --origin live` after the DOID and Mondo reloads. | **done**: 0 ungraded live claims (was 15,247 at the audit, 11,435 in QA-DATA's run) |

## Before / after (dev DB, origin `live`)

| measure | before (20:20 UTC) | after (21:21 UTC) |
|---|---:|---:|
| current live causal claims | 65,492 | 61,680 |
| ungraded live claims | 15,247 | **0** |
| DOID claims | 5,082 | 1,270 |
| DOID claims with a `geno:` cause | 3,751 | **0** |
| DOID `RO:0007001` claims | 61 | **0** |
| Mondo claims | 9,426 | 9,426 (919 re-keyed) |
| Mondo disruption / dysfunction / alteration claims with the bare filler as endpoint | 919 (823 cause side + 96 effect side) | **0** |
| Mondo claims ending on a `reva_state:` node | 0 | 919 |
| `doid:inheritance_mode` / `doid:has_disease_driver` / `mondo:altered_state_of` statements | 0 / 0 / 0 | 3,751 / 61 / 671 |
| ClinGen negative curations under `associated_with` | 296 | 296 (queued) |
| Carbon Brief claims | 739 | 739 |

Grades after the refresh: DOID 1,270 B, Mondo 9,426 B (T1 + undetermined time), Carbon Brief 737 B + 2 E.

Loads: `atlas.doid` (KG +3,812 statements, 0 conflicting; causal load 67: 1,270 unchanged, 3,812 retracted
`reclassified_by_reva`), `atlas.mondo` (KG +671 entities, +671 statements, 0 conflicting; causal load 68: 919 new,
8,507 unchanged, 919 retracted `reclassified_by_reva`). Both files were byte-identical to the audited ones
(doid.json `aff12407…`, mondo.obo `2631e8e1…`, ro.obo `f2145eae…`): **no new raw bytes archived**. Where a future
release changes the file, the health connector passes `source_row_present` (is the stored claim's (subject,
relation, filler) axiom still in the new file?) so a reclassified claim is still recorded `reclassified_by_reva` and a
vanished one `removed_at_source` (new optional argument of `causdata_common.deliver_claims`).

### QA-DATA `reva qa run --domain causal` (before = docs/dev/qa_report_latest.md, after = this run)

| check | before | after |
|---|---:|---:|
| `causal.disruption_axioms_unqualified` | warn 823 | **pass 0** |
| `causal.ungraded` | warn 11,435 | **pass 0** |
| `causal.wording_leak_predicates` (D7 `doid:has_disease_driver`) | warn 1 | **pass 0** |
| `causal.missing_endpoint` | warn 2,361 | warn 2,361 (CHEBI / HGNC / MIM / NCBI Gene / Wikidata endpoints owned by V4CHEM, V4BIO, WDHARVEST) |
| `causal.self_loop` | warn 1 | warn 1 (not in scope) |

The H1 QA check (`src/reva/qa/db_checks.py`) now also accepts the REVA-made node as the fix (its note says so);
before it only looked for a `source_relation` qualifier, which dataset claims cannot carry (the causal loader stores
`qualifiers = {}` for dataset claims; the source relation is in the selector).

Example: `ds:mondo.obo:mondo:MONDO:0020584|RO:0004021|reva_state:disruption_of:go:0003824` —
`reva_state:disruption_of:go:0003824` "disruption of GO:0003824" `—has_cause→ mondo:0020584` (selector relation
`disease_has_basis_in_disruption_of`, label "disease caused by disruption of", filler `GO:0003824`), and
`reva_state:disruption_of:go:0003824 —mondo:altered_state_of→ go:0003824`; the former claim
`…|RO:0004021|GO:0003824` ("catalytic activity causes …") is retracted `reclassified_by_reva`.
`doid:0112012 —doid:inheritance_mode→ geno:0000149` replaces claim 102966.

## Files

Code: `src/reva/adapters/connectors/health_obo_common.py` (rule, `DEFINED_AS_CAUSATION`),
`atlas_health_onto.py` (1.1.0: `ALTERED_STATE`, `state_node`, `add_state_nodes`, `row_present`, inheritance routing),
`atlas_clingen.py` (1.1.0), `atlas_attribution.py` (1.1.0), `atlas_ecoregions.py` (1.1.0), `causdata_common.py`
(optional `source_row_present`), `src/reva/qa/db_checks.py` (H1 check). Registry: `registry/kg/predicates_health.yaml`
(+5 predicates), `registry/kg/relations_v4.d/v4health.yaml`, `registry/kg/predicates_eco.yaml`
(`resolve:identity_rule`). Tests: `tests/unit/test_v6_fix4a.py` (12 tests; synthetic stanzas appended in memory to
the captured fixtures, marked as test input), `tests/unit/test_v4health.py` unchanged and passing. Report updates:
`docs/dev/v4_v4health.md`, the status column of `docs/dev/review_v4_v5_audit.md`.

Not run: `tests/integration/test_v4climate_ingest.py` errors before reaching the attribution test because
`atlas.ghcnm@1.0.1` is registered with different content (another agent's in-flight ghcnm change without a version
bump) — not caused by FIX4A.

## Owner decisions

1. `RO:0007001` "has disease driver": its parent `RO:0007000` is defined as "any natural or human-influenced factor
   that directly or indirectly causes a change in the other entity". Adding `RO:0007001` (with that quote) to
   `DEFINED_AS_CAUSATION` would make the 61 axioms T1 claims again; until then they are unmapped statements.
2. The 7,268 gene "has material basis in germline mutation in" claims keep the gene as cause (audit: "less
   misleading"); a `reva_state:germline_mutation_in:` node would be the same pattern if the owner wants it.

## Commands to finish (free disk ≥ 5 GB)

```bash
df -h /                                                                  # >= 5 GB free
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.clingen        # H2: 296 -> clingen:gene_disease_curation_negative (~1.1 MB, 0 if unchanged)
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.attribution_cb # L8: 948 cbattr:identity_rule
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.ecoregions     # L8: 8 resolve:identity_rule (2 byte ranges)
flock /tmp/reva-devdb.lock uv run reva causal refresh --origin live      # keep 0 ungraded
uv run reva qa run --domain causal --domain kg                           # expect H1/ungraded/leak checks pass
```
(The connector versions are already registered by `reva seed`.)
