# V8 PERFQ — forecast question read path

**Result:** `GET /v1/forecasts/questions/{id}` takes **p95 30 ms** warm on the dev DB. Before, it took **p95 7.9 s**.
The output is byte-identical on 131 questions. Every manifest is still verified, once per sha256.

## Root cause

`registry.read.question()` looped over every sealed batch. On each request it:

* called `batches()` once per batch (about 26 SQL queries);
* fetched every manifest through `ManifestCache(16)`.

The dev DB has 25 registry batches (62 MB of JSON). The 16-entry LRU therefore evicted manifests on every request,
so each call re-read, re-hashed, re-parsed and re-canonicalised about 4 s of manifests. Every other caller that went
through that cache (`questions()`, the ensemble scoreboard, the change feed) thrashed it too. That is why V8SCORES,
V8DECISION and V8DOSSIER each built their own workaround.

## Fix: one shared, verified manifest index

`src/reva/evaluation/registry/manifest_index.py` (new) holds `INDEX`, keyed by manifest sha256.

* **Verification.** Each manifest is read through `load_frozen_manifest` once per process, which checks the byte
  sha256 and the canonical form. Only verified content is indexed. A manifest that fails verification raises
  `IntegrityFailure` and is not cached, so it fails again on every read. This is the same per-sha caching as
  `ManifestCache` and V8SCORES.
* **Storage.** Each manifest is kept as zlib-compressed pickles:
  * one slice per `question_id`, holding the first question with that id and its forecasts in manifest order;
  * the questions list;
  * the forecast order;
  * every other top-level key.

  Slices share a per-manifest preset zlib dictionary, which makes them about 5× smaller.
* **Accessors.** `get(qid)`, `questions()`, `forecasts()`, `header()`, `n_issued`, `meta` and `manifest()`.
  * `manifest()` rebuilds a dict equal to the verified manifest: same keys, order and types. It costs 100–270 ms
    for the largest manifests, against 1.3 s to load them.
  * Every accessor returns fresh objects.
* **Invalidation.** None is needed. The cache key is a content hash, so an entry cannot go stale. Callers still read
  the list of batches, their order, and the score/resolution rows from the database on every request, so new
  batches, supplements, resolutions and voids show up immediately. No fingerprint is required.
* **Size.** All 36 manifests the registry references (25 batches, health batches and resolutions) take 7.7–8.7 MB.
  The old cache needed about 470 MB of RSS for 16 parsed manifests.

### `read.py` changes

* `question()`:
  * calls `batches()` and `_score_rows()` once per request, grouping the score rows by `batch_sha256` in Python;
  * reads manifests through `INDEX`.
  * The algorithm is otherwise unchanged:
    * the newest original batch containing the question wins;
    * supplements are matched on the manifest's `supplement_of`;
    * scores are keyed by `(model, model_version)`, which keeps ensemble v1 and v2 separate (BEAT-ENSEMBLE);
    * `_display` still applies point-only masking (BEAT-MACRO).
* `questions()` uses `INDEX.questions()` and `n_issued`. It no longer parses manifests.
* `outcomes()` was split so the shared logic lives in `_outcomes_of(store, rows)`. The result is unchanged.

### Callers switched to `INDEX` (one mechanism instead of three)

Each change is one or two lines and a true drop-in, because `manifest()` equals `load_frozen_manifest`. The dev-DB
test checks this for every manifest.

| Caller | Before | Now |
|---|---|---|
| V8DECISION `registry_paths` (`src/reva/v8/decision/runner.py`) | `R.CACHE.get` (thrashed) | `INDEX.get(...).manifest()` |
| V8DOSSIER `build_index` (`src/reva/v8/dossier/registry_index.py`) | `load_frozen_manifest` per batch per build | `INDEX.get(...).manifest()` |
| V8SCORES `_manifest_summary` (`src/reva/v8/scores/live.py`) | its own `load_frozen_manifest` | `INDEX.get(...).manifest()`; keeps its derived per-sha `_SUMMARIES` |
| Ensemble `scoreboard` (`src/reva/evaluation/registry/ensemble.py`) | `CACHE.get` (12 ensemble manifests, thrashed) | `INDEX.get(...).manifest()` |
| Change feed `resolutions` (`src/reva/v8/changes/feed.py`) | `R.CACHE.get` | `INDEX` (`.manifest()` for the resolution, `.questions()` for the batch) |

The derived caches are kept: V8DOSSIER's per-batch-set JSON, V8DECISION's 15-minute `registry_paths`, and V8SCORES'
summaries. They are cheap, and they now cost about 1 s to rebuild instead of about 4.4 s.

`R.question` is still used per question by `predict/db.py` (V5 `RegistryBaseline`) and by nlqa. Both get the
speed-up without any change.

`read.CACHE` (`ManifestCache(16)`) still serves the back-test manifests (3) and the resolution manifests. Neither
thrashes now.

## Measurements (dev DB, this container, warm)

| | Before | After |
|---|---|---|
| `GET /v1/forecasts/questions/{id}` (HTTP, 131 questions) | p50 7,161 ms · p95 7,933 ms · max 8,326 ms (n = 131) | p50 22 ms · p95 30 ms (n = 393, 3 passes) |
| `R.question()` (direct) | 3.6–4.9 s | p50 18.7 ms · p95 28.3 ms (about 17 ms of it is the `batches()` query) |
| `R.questions()` listing (4,740 questions) | 3.05–3.27 s | 146–200 ms |
| Integration test route p95 (109 questions × 3) | — | p95 31.8 ms |
| First call in a cold process (verifies all 25 batches once) | ~4–8 s on every call | 4.8 s once; peak RSS +160 MB transient while the 15 MB manifests are parsed |
| Memory | `ManifestCache(16)` ≈ 470 MB RSS | index 7.7 MB (25 batches) |

The route timings were measured through FastAPI `TestClient` with the forecasts router, using the same script before
and after (`baseline.py` in the PERFQ scratchpad).

## Equality

* **HTTP sample.** 131 questions, drawn as follows:
  * the first and last question id of every family of every original batch;
  * all 7 point-only `forecast.bm_stack` questions;
  * all 5 voids;
  * one unknown id (404).

  The `data` JSON is byte-identical for all of them (2.37 MB compared). The envelopes differ only in
  `cost.wall_time_ms`.
* **`tests/integration/v8/test_v8_perfq_devdb.py`** (reads the dev DB only):
  1. `manifest()` matches `load_frozen_manifest` for all 36 manifests (`==`, key order, and `repr`).
  2. `question()` equals the pre-index algorithm (kept verbatim in the test) on 109 sampled ids, compared by `repr`
     and by `QuestionDetail` JSON. The sample covers:
     * 108 found questions;
     * 101 with supplementary forecasts;
     * 97 with both ensemble v1 and v2;
     * 7 point-only questions;
     * 5 voids or outcomes.
  3. `questions()` equals the pre-index listing, both unfiltered and filtered by family.
  4. The five switched callers give identical outputs when `INDEX` is replaced by a direct verified-manifest reader.
  5. The route p95 is under 300 ms.
* **`tests/unit/v8/test_v8_perfq_registry_index.py`** (11 tests, no DB):
  * equality with the verbatim pre-index `question` and `questions` on a fake registry. The fake covers duplicate
    question ids, an older batch with the same question, ensemble v1/v2 supplements, a point-only record, resolved
    and void outcomes, abstentions, `Decimal` and `datetime` values, and a header-only manifest;
  * each manifest is verified once, and a second pass reads nothing from the store;
  * a tampered manifest is refused on every call and never cached;
  * a new batch, supplement or void is visible on the next call.

## Checks

* `ruff`, `ruff format --check` and `mypy` pass on every touched file.
* The new unit and integration tests pass.
* These also pass: `tests/unit/v8`, the registry, ensemble and beat unit suites, `test_v8_scores_live_db`,
  `test_v8api_devdb`, `test_registry_ensemble_db` and `tests/invariants` (191 passed).

Failures I saw that come from code I did not touch:

* `tests/unit/test_fix5_projection_kinds.py::test_every_connector_kind_is_classified`: `final_vintage` is missing
  from `projection_kinds.yaml`.
* `tests/integration/test_forecast_registry_db.py::test_05`: `void_questions(... counterfactual=False)` did not
  raise. That path lives in `registry/service.py`, which never calls `read.py`. The failure depends on the date
  (`period_ended`).

## Notes and limits

* **Cold start.** The first registry read in a process verifies every batch once (about 5 s on the dev DB). Nothing
  warms the index at startup, because `app.py` is shared. If cold latency matters, V8SHELL or the lead could call
  `R.questions(conn, store, None, limit=1)` in a background thread at startup to warm it.
* **Remaining warm cost.** About 17 ms of each warm `question()` is the `batches()` query, which reads the
  `model.evaluation` rows with their JSON metrics. It runs live on purpose, so that nothing can go stale.
* **Tampering after load.** A manifest file altered on disk after it was verified is not re-detected until the
  process restarts. The old cache behaved the same way on a hit, and the brief asks for exactly this ("cache
  verification results per manifest sha").
* **Not changed:** `backtests()` (3 manifests, no thrash) and `reva.evaluation.registry.service` write paths (they
  load manifests once per run).
