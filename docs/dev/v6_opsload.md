# V6 OPSLOAD — live loads for the belief release (FIGARO, retirement inputs) and V4ECO pending ingests

Agent OPSLOAD, 2026-10-17. Brief: run the pending live loads of `docs/dev/v6_integ.md` §3 and `docs/dev/v4_v4eco.md`
on the dev DB `reva`, one at a time, each under `flock /tmp/reva-devdb.lock`, with the 5 GB free-disk floor
checked **after** the lock is acquired (wrapper: `df` → `reva objects compress` if below → stop if still below →
command → object-store size and `df` after). No code changed.

## Log

| # | Step | Lock held (UTC) | Free disk before → after | Object store (MB) | Result |
|---|---|---|---|---|---|
| 1 | `reva ingest --live --connector eurostat.figaro.iciot_ii` | 20:42:18–20:44:00 (102 s) | 5.2 GB → 5.1 GB | 3549 → 3611 | **ok** — 27 requests (budget `eurostat.dissemination`, policy budgets-1.3.32), 52,661,297 raw bytes, 0 refused, 0 errors; 27 items published (1 constraint + 26 country-pair / DOM slices of `NAIO_10_FCP_II4` 2024); dataset registered (see below) |
| 2a | `reva ingest --live --connector eurostat.demography` (run 1) | 20:49:49–21:04:39 (889 s) | 5.1 GB → 4.6 GB | 3637 → 3636 | **partial** — 82 published, 18 failed `policy_integrity` (`registry/policies/budgets.yaml` was edited by another agent mid-run — sha 548ca392… ≠ pinned 200140ee…; re-pinned by that agent minutes later); 82 requests, 2,285,163 bytes; reserved 100 of the 282-request cycle (resumable cursor) |
| 2a′ | same (resume) | 21:18:36 | 4.3 GB; `objects compress` freed nothing (4.53 GB decimal) | — | **stopped** — below the 5 GB floor |

Free disk is shared with ~11 agents; the drop 5.4 → 4.3 GB during these steps is mostly other agents' work
(my own object-store growth was +62 MB for FIGARO and ~2 MB for demography).

### Step 1 — FIGARO 2024 (`economics.io.figaro_ii_table`)

* `evidence.dataset` `aac50f81-f99a-4121-925c-1a0281b1c6a4`, key `figaro.iciot_ii.2024`, subject `WORLD`, origin
  `live`, **11,063,900 rows**, Parquet artifact `129093eb85064a25…` (12,598,303 bytes), manifest `d521ea69…`.
* Identity report (`reva.io_identity_report/1`) passed: dense completeness 11,060,700 / 11,060,700 expected cells;
  structural zeros 0 / 0; 67 zero-output industries (published as such); totals (exact decimal, million EUR):
  gross output 197,538,681.936, intermediate 98,063,687.183, final demand 99,474,994.753.
* Notice printed by every `reva ingest` run (not an error for these built-in connectors): "promoted connector
  eurostat.labour_pensions@1.0.0 / eurostat.demography@1.0.0 disabled: template_changed" — these are old
  *promoted template* copies with the same names; built-ins take precedence (`reva.autonomy.promotion.active_specs`).

### Step 2a — Eurostat demography (partial)

Registered per-country inputs (complete windows only; an incomplete table is never registered): `demo_pjan`
(`demography.population.jan1_age_sex`, 72,306 rows), `demo_magec` (`demography.deaths.age_sex`, 69,525),
`demo_fasec` (`demography.live_births.mother_age_newborn_sex`, 22,356), each for 9 countries: AUT BEL BGR CHE CYP
CZE DEU DNK EST. **FRA is not yet covered** (the cycle is alphabetical, 282 requests, 100 per run).

## Not run (free disk 4.3 GB, below the 5 GB floor)

A 30-minute bounded wait (21:19–21:49 UTC) did not bring free disk back to 5 GB; `reva objects compress` had
nothing left to compress. Steps 2–5 are therefore **not done**:

* 2a (rest): `eurostat.demography` — ~200 of 282 requests remain (FRA among them); resumable, two more runs.
* 2b: `eurostat.labour_pensions` — not started (LFS employment/activity, ESSPROS beneficiaries/expenditure).
* 3: `reva beliefs publish --origin live` — not run, so **no release pins FIGARO 2024 yet** (it is registered and
  eligible) and the retirement inputs are incomplete anyway.
* 4: V4ECO `atlas.gbif_occ` (~267 requests, ~5 MB raw), `atlas.un_sdg`, `atlas.owid`, `atlas.fao --dataset qcl` — not run.
* 5: `reva.predict.run` — not re-run: without a new release its output is identical to
  `docs/dev/v5_pred_engine.md` §7b (France 65 and the IO plugin still stop on `missing_input` /
  `plugin_data_missing`). No numbers appended there.

### Commands to finish (each under the lock, `df -h /` ≥ 5 GB first)

```sh
flock /tmp/reva-devdb.lock uv run reva ingest --live --connector eurostat.demography      # repeat until the cursor is complete (≈2 runs)
flock /tmp/reva-devdb.lock uv run reva ingest --live --connector eurostat.labour_pensions
flock /tmp/reva-devdb.lock uv run reva beliefs publish --origin live                       # pins FIGARO 2024 + retirement inputs
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.gbif_occ
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.un_sdg
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.owid
flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.fao --dataset qcl
uv run python -m reva.predict.run --reference all --draws 2000 --seed 0 --out var/predict/reference.json
```

Check after the demography runs: `SELECT count(*) FROM evidence.dataset WHERE concept_id =
'demography.population.jan1_age_sex' AND subject_key = 'FRA'` must be 1 before publishing, otherwise the retirement
model still refuses France. If a step meets `policy_integrity` again, another agent is mid-way through a budgets
merge: wait for the re-pin (`sha256sum registry/policies/budgets.yaml` = the `budgets.yaml` digest in
`src/reva/autonomy/policy.py`) and resume — the cursor keeps completed items.

## Owner decisions

* Disk: the dev box cannot keep 5 GB free with ~11 agents writing; the DB volume is 9.1 GB (`reva` 7.9 GB) and
  `/tmp` 9.2 GB (includes the atlas query cache, not touched). The remaining loads need roughly 60–120 MB of raw +
  Parquet; they are blocked by the floor, not by their own size.
* Shared `budgets.yaml` edits invalidate the policy pin for any concurrently running ingest (18 demography items
  refused here); merges should be serialised with the dev-DB lock or done between runs.
