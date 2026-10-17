# V3 SWEEP2: full offline suite after the second causal audit fixes (2026-10-17)

Agent SWEEP2. Goal: the whole offline suite green on the current tree, after grading v3, migration
`0018_retraction_reasons`, the new connectors (`atlas.cowwars`, `atlas.idmc`, `atlas.pax`, `atlas.ucdp_dyadterm`) and
the FIXA / FIXB fixes (`docs/dev/v3_fixa.md`, `docs/dev/v3_fixb.md`).

Run conditions: shared 4-vCPU container, other agents active (SAVE dumping databases), ~6 GB free disk throughout.
Every DB test used throwaway `reva_test_*` / `reva_e2e_*` databases, all dropped afterwards. The dev database
`reva` was not read or written. pytest ran in sequential chunks, one session database per chunk (a chunk shares one
database across its files, as the full run does; see §2).

## 1. Results (final run, current tree `9c7c49b` + this report)

| Check | Command | Result |
|---|---|---|
| unit + invariants + property + contracts + scientific + replay + review_v3/v3b | `uv run pytest tests/unit tests/invariants tests/property tests/contracts tests/scientific tests/replay tests/review_v3 tests/review_v3b -m "not live and not perf" -p no:cacheprovider` | **2,778 passed, 4 skipped** (4 min 27 s) |
| security + chaos + e2e + performance + live dirs | `uv run pytest tests/security tests/chaos tests/e2e tests/performance tests/live -m "not live and not perf"` | **690 passed, 5 skipped, 20 deselected** (3 min 53 s) |
| integration, files 1–43 (`test_api_*` … `test_atlas_society_*`) | same flags, one session | **157 passed, 30 deselected** (3 min 40 s) |
| integration, files 44–86 (`test_atlas_trade_*` … `test_gate_separation`) | same flags, one session | **338 passed, 9 deselected** (10 min 56 s) |
| integration, files 87–131 (`test_io_*` … `test_world_graph`) | same flags, one session | **293 passed, 25 skipped, 7 deselected** (6 min 52 s) |
| **pytest total** | | **4,256 passed, 0 failed, 34 skipped, 66 deselected** |
| `ruff check .` | | **pass** |
| `ruff format --check .` (enforced by CI, `.github/workflows/ci.yml`) | | **17 files would be reformatted → 0** (938 files formatted) |
| `mypy` (project config, `reva.kernel`) / CI `mypy -p reva.kernel -p reva.ports` | | **pass** (9 / 13 source files) |
| import-linter (`scripts/verify_imports.py`), `scripts/validate_contracts.py`, `scripts/validate_blueprint.py` | | **5 contracts kept**; contracts valid; blueprint valid |
| `scripts/export_contracts.py --check` | | `contracts/openapi.json` up to date (no API change) |
| UI | `npx vitest --run`, `npm run typecheck`, `npm run build` | **248 passed, 1 skipped** (33 files + 1 skipped); typecheck pass; build pass (the pre-existing untracked `src/reva/api/static` build was restored afterwards) |
| UI e2e (V3 stack) | `uv run python scripts/e2e_stack.py --run --v3-fixtures` | **80 passed, 2 failed, 8 skipped**. The 2 specs expected an old label; after the fix the causal specs give **13 passed** (§4) |

## 2. Failures found and root causes

### 2.1 FIXA's 3 integration failures: two are test-order dependence (one hides a connector defect)

`test_causmore_ingest::test_idmc_ingest`, `test_events_dataset_kinds::test_causal_claims_attach_to_typed_events` and
`test_events_datasets::test_cow_war_interval_is_derived_from_spells` all **pass alone** on the current tree (3 passed).
`test_idmc_ingest` passed in every later run too. Its FIXA failure most likely came from FIXB's files changing during that run.
The other two fail whenever `test_causmore_ingest.py` runs earlier in the same session database. That is the
alphabetical order of the full suite, so they fail in a full run. Reproduced with the three files in one session:
2 failed, 15 passed.

* **`test_causal_claims_attach_to_typed_events`** (`AssertionError: ('cow.data', 'cowwar:outcome:327', 'war_outcome')`).
  The test keeps the claims whose `source_key` is one of its own ingests'. `cow.data` is shared by `atlas.mid`
  / `atlas.icow` / `atlas.cowtc` (this test's sources) and by CAUSMORE's `atlas.cowwars`, which `test_causmore_ingest`
  loads first. The claims of COW war outcomes are another test's data. **Fix (test):** the claims are also restricted
  to `raw_sha256 = ANY(<sha256 of the raw fixture files this module replays>)`, read from the 22 fixture manifests.
  A claim cites the raw it was coded from, and none of these raws is shared with `atlas.cowwars`. Nothing else
  changed: the 500-claim floor, the per-source coverage and the event/non-event checks are the same.
* **`test_cow_war_interval_is_derived_from_spells`** (`'source_dates' == 'derived_from_participation_spells'`).
  `atlas.cowwars` 1.1.0 writes `P580`/`P582` on `cow:war:<n>`: the earliest coded start and latest coded end over
  the war's records. That is REVA's summary of the coded spells. The connector documents it as a summary, but its
  qualifier used a free-form key `derived`. `reva.kg.query.derivation_of` recognises only `derivation`, so:
  * **the KG provenance labelled these computed dates `source_reported_not_verified`;**
  * the event view (`reva.events.view._interval`) labelled the interval `basis: source_dates`, without the
    "derived by REVA" note.

  This is a product honesty defect: the test was correct. **Fix (code):**
  * `atlas.cowwars` **1.1.1** writes `qualifiers = {"derivation": "participation_spells_summary", "method": …}`.
  * The event view treats time statements whose derivation is in `SPELLS_DERIVATIONS` as
    `derived_from_participation_spells`, with the existing note.
  * `tests/unit/test_fixb.py::test_connector_versions_bumped` pins 1.1.1.

  The test is unchanged and passes in both orders. **The dev DB still holds the 1.1.0 statements (`derived` key)
  until `atlas.cowwars` 1.1.1 is re-ingested (not done here).**

### 2.2 Demo workflow e2e: the autonomy template closure had changed (security-relevant)

`tests/e2e/test_demo_workflow.py::test_all_non_blocked_steps_pass` failed at step 9. The independent gate rejected
the Eurostat connector: `template code digest differs from the digest pinned in the signed policy`. The cause was
the WIP snapshot `b26d455` (SAMEAS). It edited `src/reva/adapters/http/safe_client.py` to read `Content-Encoding:
none` (sent by www.peaceagreements.org) as identity. `safe_client.py` is in the reviewed closure of
`eurostat_template.template_closure()`, which is pinned in `registry/policies/promotion.yaml`. The edit had moved
the digest from `sha256:4467985…` to `sha256:1dc0ce2…`, which **disabled T1 auto-promotion**.

**Fix:**
* The reviewed `safe_client.py` bytes are restored, and the closure digest is back on the pinned value. The digest
  was **not** re-pinned.
* The `none` alias moved to the atlas live client (`reva.atlas.ingest._live_client`). This is the only path that
  fetches PA-X. An httpx response hook rewrites a lone `none` label to `identity` and records the original label in
  `x-reva-original-content-encoding`. Same byte bound, no decoding.
* The safe client itself still refuses `none`.
* `tests/unit/test_safe_client_codings.py` was rewritten. It checks that:
  * the safe client still refuses `none` / `br` / stacked codings;
  * a new unit test **pins the template closure digest to `promotion.yaml`**, so this is caught offline next time;
  * the hook reads `none` as identity end to end through `SafeHttpClient`;
  * the hook leaves `br`, `gzip, none` and `none, none` to the safe client's refusal;
  * `_live_client` installs the hook.
* `docs/dev/v3_sameas.md` notes the move.

### 2.3 `ruff format --check .` (CI-enforced): 17 files

Earlier reports left 17 files for their owners (`docs/final_report_v3.md` §3). All 17 are now formatted:
* 3 `docs/dev` code blocks;
* `wikidata.py` and `kg/harvest.py` (no code digest);
* 12 atlas connector modules.

The atlas modules' code digests are hashed from their source, so a reformat makes `_check_registered` refuse the
registered version. Their versions were therefore bumped:
* the **14 specs whose digest changed** (computed before and after for all 112 atlas specs; no other spec changed);
* `atlas.baci`, `atlas.bmr`, `atlas.lied`, `atlas.cckp_climatology`, `atlas.constitute`, `atlas.fao`,
  `atlas.geonames_countryinfo`, `atlas.oecd.batis`, `atlas.rcs_dem`, `atlas.wrp`, `atlas.unctad`, `atlas.vdem` and
  `atlas.worldbank` go from 1.0.1 to **1.0.2**;
* `atlas.unesco_whc` goes from 1.0.0 to **1.0.1**.

`reva db seed` registers the new versions. The template-closure files stay excluded from the formatter.

### 2.4 `test_links_db::test_path_columbus_to_portuguese_from_db`: equal-cost tie broken by row order

The test passes alone. It fails after `test_kg_lang_ingest.py` in the same session. Columbus → Tordesillas has two
paths at **exactly the same cost (2.0)**, through Isabella (Q45859) and through Ferdinand (Q12860), both P1891
signatories. `find_paths` returned networkx/Yen's first path, and the order of equal-cost paths follows the
insertion order of the graph, which is the database row order. Unrelated rows (the language layer) flipped which
monarch the chain showed.

**Fix (code):**
* The unconstrained search also collects the candidates tied with the k-th path. It ranks them like the constrained
  search does: `(cost, hops, node ids)`.
* The answer no longer depends on what else the database holds. Collecting ties never adds a truncation when k paths
  are already kept.
* The deterministic choice is Ferdinand (`Q12860 < Q45859`). `tests/unit/links/test_links_fixture.py` now asserts
  the tie and its ranking explicitly, and `test_links_db.py` expects Ferdinand.
* The reverse order also failed: `test_kg_lang_ingest` after `test_links_db` found the fixture's direct P37 hops
  shorter than the language path. That test now scopes its `kq.paths` checks to the four `glottolog:*` properties.
  The `test_links_db` / `test_kg_lang_ingest` pair passes in both orders.

## 3. Files changed

* Formatting and version bumps: the 17 files of §2.3.
* `src/reva/adapters/http/safe_client.py` (restored), `src/reva/atlas/ingest.py`, `tests/unit/test_safe_client_codings.py`,
  `docs/dev/v3_sameas.md`.
* `src/reva/adapters/connectors/atlas_cowwars.py` (1.1.1), `src/reva/events/view.py`, `tests/unit/test_fixb.py`,
  `tests/integration/test_events_dataset_kinds.py`.
* `src/reva/links/paths.py`, `tests/unit/links/test_links_fixture.py`, `tests/integration/test_links_db.py`,
  `tests/integration/test_kg_lang_ingest.py`.
* `ui/e2e/v3_causal.spec.ts`, `ui/e2e/v3_causal_live.spec.ts` (§4).

No test was skipped, marked xfail or loosened to pass. No honesty or invariant assertion was removed. Generated files
(`contracts/openapi.json`, `migrations/schema_contract.json`) did not need regeneration (`--check` and
`test_schema_contract` pass).

## 4. UI e2e (V3 stack)

`uv run python scripts/e2e_stack.py --run --v3-fixtures` (whole Playwright suite, 1.4 min): **80 passed, 2 failed,
8 skipped**. The 8 skipped are the V2 composer / runs specs, which need `--recorded-drafter --v2-fixtures`, as in
the earlier sweeps.

The 2 failures had one stale cause. `v3_causal.spec.ts` "candidate space: OFF by default…" and
`v3_causal_live.spec.ts` "candidate space is off by default and separate when on" expected the old panel label
"candidats extraits de textes — non sourcés, non vérifiés". UICHECK changed `CANDIDATE_LABEL` on purpose
(`docs/dev/v3_uicheck.md` row 11) to "non sourcé — candidats (extraits de textes ou proposés par un modèle, non
vérifiés)", and the empty state to "Aucun candidat (non sourcé) n’a été extrait pour cet événement.". Both specs now
expect the new text, which still says "non sourcé" and "non vérifiés".

Re-run of the two causal specs (`… --run --v3-fixtures -- e2e/v3_causal.spec.ts e2e/v3_causal_live.spec.ts`):
**13 passed**. The Chemin specs (`v3_live_stack` "Chemin from Columbus…", "Chemin advanced options",
`v3_planet` "Chemin") passed with the deterministic tie-break of §2.4.

## 5. Follow-ups (not done here)

* Re-ingest `atlas.cowwars` 1.1.1 into the dev DB so that its war dates carry `derivation` (and `reva causal refresh`).
* Run `reva db seed` on the dev DB to register the 14 bumped atlas versions before their next live runs.
* A live PA-X re-run should confirm the hook against the real server (the fixture path does not use HTTP).
