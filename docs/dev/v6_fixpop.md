# V6 FIXPOP — eurostat.demography resumable cursor; `template_changed` diagnosis

Agent FIXPOP, 2026-10-17/18. Scope: the reference decision `france_retirement_65_2027` stopped
with gap `missing_input` for FRA (`demography.population.jan1_age_sex`,
`demography.deaths.age_sex`, `demography.live_births.mother_age_newborn_sex`, all
`kind: dataset, reason: unavailable`; predict_run2.json, release a44a0c9c). The labour/pension
inputs were not missing: the population, deaths and births tables were the only gaps.

## 1. Resumability fix (connector-only, no runner change)

**Cause (confirmed).** `reva.evidence.runner.run_connector` saves a cursor only when
`new_cursor.state["complete"]` is true (runner.py l.324). `eurostat.demography@1.0.0`
returned `complete: false` with a `done` list while a cycle was unfinished, so its checkpoint
was never saved. The windowed built-in has 31 countries x 3 tables x 3 windows + 3 EUROPOP =
**282 requests per cycle**, and each CLI run gets `FetchBudget.max_requests = 100`. Every run
therefore restarted at `demo_pjan:AT:2000` and ended at `demo_pjan:FI:2000` (the 100th item),
which is what both runs in `retire_chain.log` (22:06 and 22:15 UTC) show. FR comes at items
109-117 and was never reached. Registered datasets before the fix: 11 countries (AUT…GRC),
for each of the 3 observed tables.

**Fix: `eurostat.demography@1.1.0`**
(`src/reva/adapters/connectors/eurostat_demography.py`):

* `next_cursor()` always returns `complete: true`, which here means "valid, savable checkpoint".
  The cycle's progress is in its own keys:
  `{"format": "eurostat-demography-cycle/2", "cycle_done": [...], "cycle_finished": bool,
  "cycle_progress": [n, 282], "quarantined_this_run": [...], "since_year": 2000}`.
* `plan()` reads `cycle_done_from(cursor)`. It skips the items already visited in an unfinished
  cycle, and starts a new cycle once `cycle_finished` is true. A 1.0.0 cursor state is still read.
* Visited means durably recorded: `published`, `unchanged` or `quarantined`. A quarantine depends
  only on the archived bytes, so retrying it in the same cycle would stall the cycle forever. That
  country's dataset stays unregistered until a later cycle passes. `failed` items (network errors,
  budget) are not visited, so the next run retries them first.
* The runner contract "the cursor advances only after durable publication" still holds: only
  outcomes recorded after `publish_acquisition` committed count.
* Nothing else changed: parsing, validation, `PARSER_VERSION`, dataset assembly, the hooks and
  other connectors are untouched. `runner.py` is not modified. It is also outside the template
  closure (closure listed in §2).
* Version bump to 1.1.0. A (name, version) row is immutable and the code digest changed.
  `registry/mappings/eurostat_demography.yaml` now has `connector.version: "1.1.0"`, with the same
  mapping keys, versions, targets and applicability; this follows the precedent of `ilostat_labour`
  and `un_wpp`. The 1.0.0 eligibility rows stay. `reva seed` on the dev DB registered 1.1.0
  (code digest `sha256:ff1ba0a9…c77d`) and 3 eligibility rows.
  Dataset assembly (`assemble_datasets`) selects acquisitions by connector *name*, so the windows
  already archived by 1.0.0 still count toward a country's tables.

**Tests**
* `tests/unit/test_eurostat_demography_connector.py`:
  * the existing resume test is updated to the new format;
  * `test_cursor_is_always_a_savable_checkpoint_and_resumes_across_budgeted_runs`: 3 runs of 100
    requests cover the 282 items exactly once each, and FR comes in run 2;
  * `test_failed_items_are_retried_and_quarantined_items_do_not_stall_the_cycle`;
  * `test_legacy_1_0_0_cursor_states_are_read`.

  13 passed.
* `tests/integration/test_demography_resume.py` (db, fixture mode, throwaway DB, real
  `run_connector` with `persist_cursor=True` and 6 requests per run over the 15 LI/LU fixture
  items):
  * run 1 saves the cursor (`cursor_advanced`) and registers LI;
  * run 2 resumes at LU (1.0.0 would have restarted at LI) and registers LU;
  * run 3 fetches EUROPOP and finishes the cycle;
  * run 4 starts a new cycle.

  1 passed (9.5 min, mostly the throwaway DB bootstrap, which is dropped afterwards).
* `tests/invariants` + `tests/unit/test_fixture_plans.py`: 210 passed, 1 skipped. Ruff is clean.
  Mypy is clean on the new test. The 3 mypy errors in `eurostat_demography.py` (pyarrow stubs,
  `assemble_datasets` l.1277/1279) are older than this change and sit in untouched code. The
  project's mypy config only covers `reva.kernel`.

These files were committed to the branch through the lead's WIP snapshots (c34c360, then
16d0bc5). This report is committed separately.

**Same latent pattern elsewhere (not changed, out of scope):** `ilostat_labour.py:327` and
`un_wpp_demography.py:932` read `done` only while `complete` is false, as 1.0.0 did. They only
break the same way if a cycle can exceed one run's request budget. Worth checking if either ever
logs `budget_exhausted`.

## 2. `template_changed`: no template file changed (false positive)

The template digest matches the pin byte for byte:

| | value |
|---|---|
| pin (`registry/policies/promotion.yaml` → `approved_templates.eurostat_jsonstat.template_sha256`) | `sha256:4467985201296671882d9ca57fd7d414617537a63a423d0f61c8e20da9ec3443` |
| `eurostat_template.template_digest()` now (working tree) | `sha256:4467985201296671882d9ca57fd7d414617537a63a423d0f61c8e20da9ec3443` |
| `promotion.yaml` file digest vs `PINNED_POLICY_DIGESTS` | loads OK (`115a9ab5…94aa`) |

The closure (13 entries) has no uncommitted edits:
* `contracts/connector.schema.json`;
* `reva.adapters.connectors.{base,eurostat_template}`;
* `reva.adapters.http.{rate_limit,safe_client}`;
* `reva.kernel.{errors,ids,support,time,uncertainty,values}`;
* `reva.ports.connectors`.

So no commit changed the closure since the pin.

**Real cause.** `reva.autonomy.promotion.connector_states()` treats *every*
`evidence.connector_version` row whose `declared_capabilities.template = 'eurostat_jsonstat'`
as a promoted template connector. It then compares that row's `code_digest` with the template
digest. The two **built-in** connectors `eurostat.demography` and `eurostat.labour_pensions`
declare `template="eurostat_jsonstat"` in `describe()`. Both have done so since they were created
(754fa31 / f9c9688, 2026-10-15), and that value drives their post-publish hook choice. Their
`code_digest` is their own `module_digest(...)`, which can never equal the template digest. Both
1.0.0 rows have had state `template_changed` since they were registered (2026-10-15 03:09:38),
and the new `eurostat.demography@1.1.0` row has it too. The dev DB has no actual
autonomy-promoted `eurostat_jsonstat` connector (these 2-3 rows are the only ones). The warning is
noise for these rows:
* `active_specs()` would skip built-in names anyway;
* `promoted_connector_availability()` is only reached for names with the template prefix;
* ingestion of the built-ins is unaffected (the 22:21 labour_pensions run published normally).

**Owner decision (T3, `src/reva/autonomy` and `registry/policies` paths). Proposed, not applied.**
Do not re-pin anything: the pin is correct. Either:
* (a) exclude built-in names in `connector_states()` (e.g. filter with
  `reva.autonomy.binding.builtin_connector_names()`, already used by `active_specs()`); or
* (b) let built-ins declare a distinct capability (e.g. `template: "builtin:eurostat_jsonstat"`)
  and dispatch post-publish hooks on it.

  Option (b) changes their spec, so each connector needs a version bump and a new mapping pointer.

Option (a) is the smaller change. No revert is needed: no closure file was edited by mistake.

## 3. Live run

| step | result |
|---|---|
| `flock … reva seed` | rc 0. 1.1.0 is registered and has its eligibility rows. |
| budget for `eurostat.demography` on 2026-10-17 | 282 of 300 used (82 + 100 + 100), all by 1.0.0. |
| budget on 2026-10-18 | 0 used: 300 available. |
| `df -h /` before the ingest (2026-10-18 16:47 and 16:48 UTC) | **3.9 GB, then 4.8 GB free, below the 5 GB floor: live ingest NOT run** (it read 4.7-4.9 GB on 10-17 and 4.9 GB at 16:36). |
| saved cursor `ops.connector_cursor['eurostat.demography']` | none (1.0.0 never saved one): the first 1.1.0 run starts at AT. |

The pending load is small: about 2.8 MB of raw data per 100 requests, observed on 10-17.

**Commands to finish** (any day with ≥ 5 GB free; at most 2 runs, ≤ 200 of the 300 daily
requests):

```
df -h /                                                         # must be >= 5 GB
flock /tmp/reva-devdb.lock uv run reva ingest --live --connector eurostat.demography   # run 1: items 1-100 (AT..FI:2000); cursor now saved
flock /tmp/reva-devdb.lock uv run reva ingest --live --connector eurostat.demography   # run 2: items 101-200, includes FR (109-117)
docker exec reva-db psql -U reva_owner -d reva -Atc "select dataset_key from evidence.dataset where subject_key='FRA' and dataset_key like 'eurostat.demography.%'"   # expect demo_pjan, demo_magec, demo_fasec
flock /tmp/reva-devdb.lock uv run reva beliefs publish --origin live
uv run python -m reva.predict.run --reference all --draws 2000 --seed 0 > <scratchpad>/ops/predict_run3.json
```

Each run's report should show `"cursor_advanced": true` and
`cursor.cycle_progress` = [100, 282], then [200, 282]. A third run (82 requests, the next day or
the same day) finishes the cycle with the remaining countries and EUROPOP2023. FR does not need
the EUROPOP2023 tables: the decision uses the default `average_residual` migration and
`constant_base_period_rates` fertility.

**France 65 outcome: unchanged for now.** The chain still stops at `missing_input` (FRA
population / deaths / births: datasets unavailable). In predict_run2 the decision was otherwise
complete: `social.retirement_age@1.0.0` hop, `unvalidated` label, release a44a0c9c. The labour
and pension inputs (`eurostat.labour_pensions`, cursor complete for FR, DE, IT, ES, NL, BE, SE,
PL, 22:21 UTC) were not reported missing. After the two runs above, expect attributed,
unvalidated FRA outputs, labelled "scénario non validé — pas une probabilité", because the chain
has no back-test record in `registry/predict/validated_chains.yaml`. Nothing is zero-filled: if
any FR window is quarantined, the dataset stays unregistered and the gap stays typed.

## 3b. Live load done by the lead (2026-10-18)

The lead ran the two ingests once the disk allowed it. Run 2 went from FI to MT, so FR is loaded.
The lead then published the beliefs (release `ff1b49fe-70c2-4a2e-bddc-baf8be4b87e4`).
`france_retirement_65_2027` then produced outputs (`predict_run4.json`), and those outputs exposed the bug below.

## 4. The decision start is binding for the retirement plugin (France 65 coherence fix)

**Bug.** The decision starts 2027Q1, and the engine horizon runs 2027Q1-2028Q4. The plugin
options in `registry/predict/reference_decisions.yaml` set `first_birth_year: 1962`
("2027 - 65"). The engine's own fallback (`engine._retirement`) used the same arithmetic:
`start_year - target_years`. Under the reference path `law_in_force_2026`, the cohorts
1962-1964 reach their legal age (62y6m-62y9m) in 2024-2026. Moving them to 65 therefore changed
eligibility **before** the decision starts. The plugin ran 8 calendar years (2025-2032), and the
engine reported all of them on a quarterly horizon. predict_run4 showed:

| output | 2025 | 2026 |
|---|---|---|
| employment 55-69 | +191,209 | +366,994 |
| pension spending | −8.33 bn € | −15.99 bn € |
| pension recipients | −437,550 | −839,803 |

**Fix** (`src/reva/predict/propagators.py`, `src/reva/predict/engine.py`, reference decision YAML):

* `retirement_first_cohort(iso3, reference_path, target_months, start_year, start_quarter)`
  derives the earliest birth year whose members, and all later cohorts, first reach the
  eligibility age on or after the decision's first month. The eligibility age is the reference
  legal age, or the decided level if that is lower.
  * The arithmetic is the model's own: a person born in month m of year c reaches age A during
    month 12c + m − 1 + A.
  * For FRA 65 from 2027Q1 the result is **1965**: it first reaches 62y9m-63y in 2027-10.
    Cohort 1964 is excluded because it reaches 62y9m from 2026-10.
  * Cohorts are whole birth years, so a cohort partly eligible before the start keeps the
    reference path; the derivation states this.
  * For a lowering (target 60), the decided level binds instead: 1967.
  * An unknown table, path or claim-year basis is a typed problem. The engine then reports gap
    `first_cohort_underivable` and does not run the model.
* An explicit `first_birth_year` earlier than the derived cohort is refused with gap
  `decision_start_conflict`: the model is not run and nothing is reported, never zeroed. A later
  explicit cohort is accepted.
* The model horizon is set to cover the decision horizon exactly:
  `periods = last horizon year − reference_year`, so 4 years (2025-2028). Only the calendar
  years of the horizon (2027, 2028) are reported.
* Every output carries the note "annual calendar-year values 2027-2028 (not quarterly)", and
  the provenance has `reported_years`.
* A non-zero value before the start year is refused with gap `effect_before_start` and the output
  is dropped. This is a guard; with the derived cohort the model's pre-start values are exactly 0,
  which the tests check.
* The provenance records `first_birth_year_derivation`: the rule, the first cohort, the excluded
  cohort and the rule-table digest.
* `registry/predict/reference_decisions.yaml` no longer states `first_birth_year`/`periods`
  for France 65. They are derived, and a comment explains why.

**V8DECISION cache** (`docs/dev/v8_v8decision.md`). The bundle cache key is
`scenario_hash:data_fingerprint`. The data fingerprint covers the effect layer and the latest
release, but not the code. A run cached before this fix would therefore keep serving the old numbers
for up to 30 minutes in a running process. `src/reva/v8/decision/runner.py`:
* `scenario_hash` now includes `code_fingerprint()`;
* `code_fingerprint()` is a content digest of `src/reva/{predict,models/social,v8/decision}/*.py`,
  `registry/predict/*.yaml`, `registry/rules/retirement_age/*.yaml` and
  `registry/assumptions/retirement_*.yaml`;
* it is re-hashed only when a file's size or mtime changes.

A code or registry change now makes a new scenario hash, so the cache misses and recomputes.
The UI shows `scenario_hash[:12]` as "entrées", so that value changes once.

**Tests**
* `tests/unit/social/test_fixpop_predict_retirement_start.py` (4 tests). The real
  `social.retirement_age` plugin runs on the captured Eurostat fixtures:
  * the derivation gives 1965, 1967 for a lowering to 60, and a typed problem for an unknown country;
  * the reference decision runs the model with `first_birth_year` 1965 and 4 periods, and reports
    only 2027 and 2028, annual, labelled unvalidated;
  * the model's pre-start rows are exactly 0;
  * a stated 1962 gives `decision_start_conflict` and no output, while 1966 is accepted;
  * a non-zero pre-start value gives `effect_before_start`, and 2029 is dropped.
* `tests/unit/predict/test_predict_engine.py`: the expected cohort is updated from 1962 to 1965.
  35 passed in `tests/unit/predict`, together with the new test file.
* `tests/unit/v8/test_fixpop_decision_cache_key.py` (2 tests) and
  `tests/unit/v8/test_v8_decision_studio.py`: 18 passed.

**Re-run** (read-only): `uv run python -m reva.predict.run --reference all --draws 2000 --seed 0`
writes `predict_run5.json`, on release `ff1b49fe…`. The other 4 reference decisions are
identical to run4 (variables and gaps).

France 65 (`france_retirement_65_2027`):
* label: **« scénario non validé — pas une probabilité »** (`unvalidated`; no back-test record in
  `validated_chains.yaml`);
* deterministic model output, so no sampled uncertainty and degenerate quantiles;
* **L1** fidelity; model label "conditional projection under stated assumptions, not a forecast";
  model status "experimental model, not yet reviewed";
* results "qualified": early-retirement routes (long careers, disability) are not modelled;
* employment response taken from the cited literature
  (`registry/assumptions/retirement_employment_response.yaml`);
* first affected cohort 1965 (derived); no gaps.

| annual value (calendar year) | 2027 | 2028 |
|---|---|---|
| employment 55-69, scenario − reference (persons) | **+8,834** | **+118,437** |
| old-age pension recipients, scenario − reference (persons) | **−20,215** | **−271,023** |
| pension spending, approx. (nominal € of the average's year, not indexed) | **−0.385 bn €** | **−5.159 bn €** |

The 2027 values are small because cohort 1965 reaches its reference age only from October 2027.
No year before 2027 is reported, and the model computes exactly 0 for 2025 and 2026.
