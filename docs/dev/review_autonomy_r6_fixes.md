# R6 autonomy review: fixes (agent AFX)

Source review: `docs/dev/review_autonomy_r6.md`.
Regression tests:
- `tests/integration/test_autonomy_r6.py`: one or more tests per finding;
- `tests/integration/test_eurostat_discovery.py::test_r6_05_rollback_hold_is_keyed_on_content`: changes promotion state, so it runs after the other promotion tests.

Each regression test is a scratch attack from `scratchpad/review6/` turned into an assertion of the fixed behaviour.

Re-running the scratch files against the fixed tree:
- every attack in my area now fails to reproduce;
- R6-08 (HOOK) and R6-14 (HOOK) still reproduce. Their patches are below.

## Status

| Finding | Status | Where |
|---|---|---|
| R6-01 years list / untyped DB error | **Fixed** | Lead fixed `years` → `{"in": [...]}` in `jobs.py` at 00:58. `handle_discovery` turns any `SQLAlchemyError` into a typed result: `failed`, `code=database_refused`, `retryable=false`. The result carries only the type and the SQLSTATE, never parameters. |
| R6-02 gate pass spent on another manifest | **Fixed app-side**, DB patch below | See the R6-02 bullets after this table. |
| R6-03 gate trusts the caller's object | **Fixed app-side**, DB patch below | See the R6-03 bullets after this table. |
| R6-04 provider chooses unit/dimensions | **Fixed** | `promotion.yaml` → `approved_exact_mappings[].structure`, used in `binding._structure_problems` and the mapper. See the R6-04 bullets after this table. |
| R6-05 rollback hold bypass / confusable version | **Fixed** | See the R6-05 bullets after this table. |
| R6-06 forged reports | **Fixed** | For T1, the gate re-runs the validator (captured replay of the reported bank, no network) and the sandbox on the stored proposal. Both must pass. An `additivity_mismatch` blocks T1. A report with no bank cannot be recomputed, so the proposal is rejected. |
| R6-07 template pin covers one file | **Fixed**, re-pinned | See the R6-07 bullets after this table. |
| R6-08 repair pause lifted by an unrelated pass | **Patch for HOOK** (`repair.py`) | Below. |
| R6-09 validator sample not charged | **Fixed** | `budget.DiscoveryMeteredHttp` (a `MeteredHttp`): one meter per live run, used by the catalogue, the inspection and the validator sample. The run is hard-capped at the reservation, settled from the meter, and the client is closed in `finally`. |
| R6-10 kill file dropped | **Fixed** (minimal edit) | `scripts/_runtime_env.EXTRA_REVA_CONFIG` now forwards `REVA_AUTONOMY_KILL_FILE`. A DB-level flag is still recommended (review). |
| R6-11 identifier anchoring / name collisions | **Fixed** | See the R6-11 bullets after this table. |
| R6-12 free manifest fields | **Fixed** | See the R6-12 bullets after this table. |
| R6-13 sandbox filesystem reach | **Partially fixed** (Low) | See the R6-13 bullets after this table. |
| R6-14 flags non-semantic in repair replay | **Patch for HOOK** | Below. |
| R6-15 fixture mode in production | **Fixed** | `handle_discovery` refuses `mode=fixture` when `reva_env == production`, with `code=fixture_mode`. |
| R6-16 registry ground truth at T1 | **Fixed** | The following are now T3: `registry/concepts`, `registry/entities`, `registry/requirements`, `src/reva/ontology`, `src/reva/ports`. `registry/mappings` is at least T2, so the repair flow stays T2. |
| P6 sandbox stderr in evidence | **Fixed** | The child output and any external-runner response pass through `telemetry.redact_obj`. The strict xfail in `tests/security/test_redaction_r7.py` was removed. |
| Triage: `gaps.py` year-scoped eligibility | **Fixed** | Uses `evidence.observation_is_eligible(l.id, :cutoff)`. |
| Triage: health test | **Fixed** | The health test now looks up the connector by name *and* version. |

**R6-02.** `promote_connector(engine, io, ctx, decision, policy, *, guard, expected_manifest)` no longer takes a manifest or mapping:
- `load_promotion` takes the gate result, then `ops.proposal` (`manifest_sha256`), then the digest-verified proposal document, then the change, then the manifest and mapping.
- The manifest and mapping digests must equal those recorded in the gate evidence (`t1_scope.manifest_sha256`, `mapping_manifest_sha256`, `proposal_sha256`).
- `expected_manifest` is checked against what was loaded. It is never used as the source.
- `binding_problems` now also checks:
  - the approved entry (dataset/concept);
  - the geography scope;
  - verified rights;
  - the pinned structure;
  - the mapping manifest.
- The gate requires `ops.proposal.summary.scope` (what the DB binds the approval to) to equal the derived scope: concept, dataset, `subject_ids`, `years`, `manifest_sha256` and `mapping_key`.

**R6-03.**
- `PolicyPromotionGate.decide` accepts a proposal id, or an object that must equal the stored document; any other object raises `PolicyDenied` and nothing is recorded.
- The tier is derived from the stored row, with a minimum per type: `model_registration` and `mapping_equivalence` are at least T2.
- T1 covers only `connector_promotion`.
- `PostgresModelRegistry.register` refuses every gate pass: a model needs an approving named human review.
- The gate stays callable as a pure function of stored rows, for GATE's `reva gate` process.

**R6-04.**
- Pins for `demo_pjan`:
  - dimension order `[freq, unit, age, sex, geo, time]`;
  - unit `NR` only;
  - `freq: [A]`;
  - age vocabulary `^(TOTAL|Y_LT1|Y[1-9][0-9]?|Y_OPEN|UNK)$`, which excludes overlapping aggregates such as `Y15-64`;
  - sex vocabulary `^(T|M|F)$`;
  - totals `{age: [TOTAL], sex: [T]}`.
- `THS`, an extra `citizen` dimension or an aggregate age code produces:
  - a mapping that is not exact (`review_required`);
  - a gate `reject`.

**R6-05.**
- `SEMVER_RE` (fullmatch, no leading zeros or suffix) is enforced in `validate_manifest`, `binding` and `_semver`.
- The gate requires `version == resolve_connector_version(...)`.
- `promotion.rolled_back_content` holds by content key, across every version and template digest, whenever the latest decision is a non-supersession `retired`. It is checked in `_existing_state` and inside the promotion transaction.

**R6-07.**
- `template_digest()` is the sha256 over the sorted `module:sha256(file bytes)` lines of the template's whole `reva` import closure, enumerated from the import statements (module-level and lazy), plus `contracts/connector.schema.json`.
- Today the closure is 11 modules:
  - `adapters/connectors/{eurostat_template,base}`;
  - `adapters/http/{safe_client,rate_limit}`;
  - `kernel/{errors,ids,support,time,uncertainty,values}`;
  - `ports/connectors`.
- If a name the template imported is rebound at runtime, the digest records it.

**R6-11.**
- Every template identifier regex uses `fullmatch`. Explicit checks were added for `name` and `version`.
- `filters` and `pinned_categories` are now validated separately. Before, a merged dict let the pinned list hide an unchecked filter list such as `"FR\n"`.
- Geo is canonical (upper-case, sorted, unique), and names join codes with `.`.

**R6-12.**
- Unknown `request_parameters` keys are refused. The generator no longer copies `dataset_label` or `observed_status_labels`.
- Derived or pinned values:
  - `licence` comes from the registered source;
  - `rights_ref` comes from `approved_templates.<id>.rights_ref`;
  - `special_categories` comes from the template constant;
  - `mapping_version` comes from the derived mapping key;
  - `lang`, the status vocabulary and `max_pages` are pinned.
- The `semantics` keys must be exactly the template's set.

**R6-13.**
- The child sets `RLIMIT_NPROC=1` itself. Its HOME/TMPDIR is private (0700) and its cwd is read-only.
- The child measures filesystem reach and records it under `filesystem` in the evidence: it tries to write to a 0700 directory owned by the worker. A `filesystem_shared` warning is always recorded.
- Not done:
  - **Mount isolation of `/`.** `bwrap` is not installed, and a mapped-uid user namespace still has the worker's host uid.
  - **`PR_SET_DUMPABLE`** in the worker. It is process-global, and the worker entrypoint is not mine. This is recommended to the lead or I.

## Policy re-pin (maintainer action taken, T3)

`registry/policies/promotion.yaml` was re-pinned, with `structure`, `rights_ref` and the closure `template_sha256`. Its pin is in `src/reva/autonomy/policy.py`.
- **Label.** It keeps the label `promotion-1.1.0`. Bumping it would break 4 tests owned by other agents (evaluation service, tournament).
- **Identity.** The SHA-256 is the identity. The lead may bump the label to 1.2.0 together with those tests.
- **Future edits.** Because the closure includes `kernel/*` and `ports/connectors.py`, any merged edit to those files disables T1 until a maintainer re-pins. This is deliberate (R4-11/R6-07). `ports/connectors.py` was being edited by another agent while this work was done. Re-pin after it lands:
  1. `uv run python -c "from reva.adapters.connectors import eurostat_template as et; print(et.template_digest())"` gives the new `template_sha256`.
  2. Then `sha256sum registry/policies/promotion.yaml` gives the value for `PINNED_POLICY_DIGESTS`.

## DB-side patches (for GOV/GATE: 0007 governance section)

**R6-02, bind the approval to the evaluated connector spec.** The DB cannot read artifact bytes, so the binding goes through `declared_capabilities`.

1. In `cli/seed.register_connector_version` (GOV), record the manifest digest:
   ```python
   "caps": json.dumps({"template": spec.template, "datasets": list(spec.dataset_ids),
                       "allowed_hosts": list(spec.allowed_hosts),
                       "manifest_sha256": (spec.field_mappings or {}).get("manifest_sha256")}),
   ```
2. In `evidence.approve_eligibility`, after the `years` check:
   ```sql
   SELECT declared_capabilities INTO caps FROM evidence.connector_version WHERE id = p_connector_version;
   IF scope->>'manifest_sha256' IS NULL
      OR caps->>'manifest_sha256' IS DISTINCT FROM scope->>'manifest_sha256' THEN
     RAISE EXCEPTION 'approval: connector version % is not the manifest the proposal scope names', p_connector_version;
   END IF;
   IF NOT COALESCE(caps->'datasets' = jsonb_build_array(scope->>'dataset'), false) THEN
     RAISE EXCEPTION 'approval: connector version % serves another dataset than %', p_connector_version, scope->>'dataset';
   END IF;
   ```
   (declare `caps jsonb;`)

`summary.scope.manifest_sha256` is `et.manifest_digest(manifest)`, the same value as `spec.field_mappings.manifest_sha256`, and the gate refuses T1 unless the recorded scope equals the derived one.

*Stronger, optional.*
- `ops.record_gate_result` takes a `p_scope jsonb`. The gate's `t1_scope.db_scope` is computed and recorded in the evidence already.
- `approve_eligibility` then compares against `g.scope` instead of the proposer-written `summary.scope`.

**R6-03 / R4-05, no gate pass approves a model.** In `ops.consume_approval`, at the top of the gate-result branch:

```sql
IF p.proposal_type IN ('model_registration','mapping_equivalence') OR p.permission_tier >= 2 THEN
  RAISE EXCEPTION 'approval: % proposals need an approving human review, not a gate pass', p.proposal_type;
END IF;
```

Also revoke `EXECUTE ON ops.record_gate_result` from `reva_worker` (GATE's `reva_gate` login is in progress).

## Patches for HOOK (`src/reva/autonomy/repair.py`)

**R6-08.** In `resume_belief_publication`, replace the gate-evaluator branch:

```python
        marker_row = conn.execute(text("SELECT accumulated_error_bound, updated_at FROM ops.dirty_node "
                                       "WHERE node_key = :k FOR UPDATE"), {"k": PAUSE_PREFIX + concept_id}
                                  ).mappings().one_or_none()
        marker = (marker_row or {}).get("accumulated_error_bound")
        ...
        if actor.startswith("gate-evaluator:"):
            if gate_result_id is None:
                raise AutonomyBlocked("a gate evaluator resumes publication only with its promote decision")
            g = conn.execute(text(
                """SELECT g.decision, g.evaluator, g.proposal_id, g.recorded_at, p.proposal_type
                   FROM ops.gate_result g JOIN ops.proposal p ON p.id = g.proposal_id WHERE g.id = :g"""),
                {"g": gate_result_id}).mappings().one_or_none()
            if g is None or g["decision"] != "pass" or g["evaluator"] != actor:
                raise AutonomyBlocked("gate result is not a promote decision by this evaluator")
            if marker_row is None or str(g["proposal_id"]) != str(pause_proposal) \
                    or not str(g["proposal_type"]).startswith("connector_repair:") \
                    or g["recorded_at"] < marker_row["updated_at"]:
                raise AutonomyBlocked("the gate pass is not for the repair proposal that paused publication")
```

**R6-14.** Estimation, status and classification flags must be semantic for the replay check.

In `FixtureReplayChecker._compare`, count the flag changes on reviewed semantic flag keys separately:

```python
SEMANTIC_FLAG_KEYS = frozenset({"isReported", "isAggregate", "legacyEstimationFlag", "isQtyEstimated",
                                "isNetWgtEstimated", "estimation_flag", "obs_status", "status",
                                "classificationCode"})  # source_flags keys of the built-in connectors
...
        sem_flags = sum(1 for k in keys if any((base[1].get(k) or {}).get(f) != (prop[1].get(k) or {}).get(f)
                                               for f in SEMANTIC_FLAG_KEYS))
        return {..., "flag_changes": flag_changes, "semantic_flag_changes": sem_flags}
```

In `check`, before the `historical_flags_changed` warning:

```python
            elif v.get("semantic_flag_changes"):
                findings.append(_finding("historical_flag_reinterpretation", "blocker",
                                         f"{v.get('url') or v.get('item')}: {v['semantic_flag_changes']} "
                                         "estimation/status/classification flag sets differ"))
```

After `history = self.accepted_history(connector)`:

```python
            if len(history) >= 500:
                findings.append(_finding("accepted_history_truncated", "warning",
                                         "replayed the 500 most recent accepted vintages only"))
```

Check the exact `source_flags` key names against `comtrade.py` and `worldbank.py`. The current Comtrade 1.2.0 normalises the estimation flags under its own keys.

## Test results

- **Before the fixes**, the autonomy suites showed 6 failures: R6-01 (5 promotion tests) and the DZA gap test.
- **After the fixes**, see the AFX report. The DB fixture was briefly broken by GATE's in-flight `reva_gate` role provisioning (SQLSTATE 42704) while this was written.
