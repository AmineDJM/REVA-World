# V8DECISION: the decision studio (`/v8/decision`)

Agent V8DECISION, 2026-10-17/18. Brief: `docs/roadmap/v8_console.md` (row « Décision »), contract `ui/src/v8/contract.ts`
(route `decision`, order 50, `g s`). It builds on the V5 engine (`src/reva/predict/**`, `docs/dev/v5_pred_engine.md` §7b–7c).
I made no edit in `src/reva/predict/**`. The studio wraps the engine; it does not fork it.

**What it answers:** "what happens if `<actor>` does `<decision>`?" The answer is a **scenario conditional on stated
assumptions**. Each scenario carries the label « scénario non validé — pas une probabilité » (the judge registry is
empty) and the notice « pas un conseil financier ». Every number comes from an attributed estimate or a REVA model, and
its source is one click away. When the chain stops, the page says exactly which link or data is missing.

## 1. What was built

| Piece | Path |
|---|---|
| Templates and schema limits (5 references, instruments with the geographies they need, comparison presets = *labelled variants* of references) | `src/reva/v8/decision/templates.py` |
| Pure bundle transforms: headline rows + flags, propagation graph (stops / ties / blocked models as nodes), evidence per estimate key, competing estimates side by side, « pourquoi pas de résultat » diagnosis, French notes | `src/reva/v8/decision/bundle.py` |
| Runner: read-only ports (same construction as `run_on_connection`), memo proxies, **cross-request shared caches** (registry baseline index, baselines, FIGARO IO arrays/gap; 15 min), one-at-a-time sensitivity, paired A/B comparison, input-hash LRU, background warm-up | `src/reva/v8/decision/runner.py` |
| Service (cache by scenario hash + data fingerprint) and Markdown brief | `src/reva/v8/decision/service.py`, `brief.py` |
| API router (auto-included) | `src/reva/api/routes/v8_decision.py` |
| UI feature | `ui/src/v8/features/decision/` (index, DecisionStudio, Composer, Effects, Propagation, Evidence, Sensitivity, Compare, Why, state, model, api, css) |
| Tests | `tests/unit/v8/test_v8_decision_studio.py` (16), `tests/integration/v8/test_v8_decision_live_db.py` (2, dev DB read-only), `ui/src/v8/features/decision/decision.test.tsx` (17; fixtures = real dev-DB responses) |

### Endpoints (`read` scope, read-only transactions, envelope `context_mode: simulation`, `modality: assumption_conditioned_scenario`, warnings `unvalidated_scenario` + `not_financial_advice`)

| Endpoint | Returns |
|---|---|
| `GET /v1/v8/decision/templates` | references, instruments (`needs`: actor/target geo, countries, occurrence = magnitude 1, level), schema limits, comparison presets, engine warm-up state (the first call starts the warm-up) |
| `POST /v1/v8/decision/run` `{reference_id \| decision, draws, seed, as_of_max, origin}` | `result` (the V5 engine result, validated by the V5 contract; hop provenance moved to `evidence`), `headline`, `graph`, `evidence`, `conflicts`, `diagnosis`, `honesty`, `scenario_hash`, `cached` |
| `POST /v1/v8/decision/sensitivity` (+ `variable`) | tornado: for each estimate on the path, the result with that estimate at its 5 % / 95 % (normal approximation of its *published* uncertainty) and the others at z = 0, plus the Monte-Carlo variance share r² of its draw; estimates with no published uncertainty are listed with no bar |
| `POST /v1/v8/decision/compare` `{a, b}` | side by side and **paired difference B − A draw by draw** (the engine's common random numbers), the fields that changed, and the stops present in only one of the two |
| `POST /v1/v8/decision/brief` (+ `url`, `sensitivity`) | Markdown with the label, the decision and its stated assumptions, a results table, and for each link the estimate as published, the coefficient applied, applicability, citation / DOI / locator / **verbatim quote** / licence / method. It also has competing estimates, links with no number, stops with what would unblock them, sensitivity, validation and provenance. |

### UI (`/v8/decision`)

* **Composer:** five templates (one click; `Shift+1…5`) and free composition (instrument, magnitude + unit, start,
  duration, horizon, countries, actor / target and their geographies, title). Validation mirrors the server schema
  (patterns, limits, units of the instrument, occurrence = 1, required geographies). An invalid decision is refused
  before any request (tested). When a variant changes the magnitude or the start, a value the reference *derived* from
  them is dropped so the engine derives it again. Example: France's first birth cohort, 1962 = 2027 − 65.
* **Effects:** answer sentence; a dense matrix of variables × countries (median at the last computed period, a 5–95 %
  glyph on a scale shared per row, ◌ = distribution incomplete, missing hatched). The selected cell opens the V5
  `FanChart` (restyled with V8 tokens, scenario colour, dark mode), or `IntervalBar` for undated results. It also shows
  the path's sources, flags (estimation ancienne, groupe non vérifié, modèle REVA, extrapolation) and the engine's notes
  in French.
* **Propagation:** a cytoscape graph laid out by path length: decision → direct changes (dashed, *hypothèse*) →
  variables → stops. A link with no estimate, a blocked model or an unresolved tie appears as a hatched node. You can
  filter by country. There is also an accessible table of links. Clicking a link opens the evidence panel: the
  estimate as published, the published uncertainty, the β applied ± its 90 % range, applicability, and **competing
  estimates side by side** (chosen / not chosen, never averaged; each with its quote). Then come citation, DOI, locator,
  verbatim quote, licence (restricted flagged), method / identification, review status, and the attributed wording.
* **Sensitivity:** an SVG tornado for each variable and period. Hatched rows list the estimates with no published
  uncertainty.
* **Comparer A / B:** presets « BCE +25 pb contre +50 pb » and « OPEP+ −1 contre −2 mb/j », or B = copy of A. Both
  scenarios live in the URL (`b.*` keys). The table shows A, B and B − A with its 5–95 %, plus a dot glyph per row.
* **Pourquoi / manques:** each stop with « Il manque » and « Pour débloquer ». Nearby estimates that do not apply as
  they stand are listed (rice: `trade.export_restriction_share`; France: 12 estimates of a *change* of the pension age
  that cannot apply to a target *level*). It also shows missing lag periods, chain ends, unquantified causal claims
  (grades) and excluded estimates by reason.
* **Brief:** preview, download `.md`, copy. **Épingler** goes through `pinItem` of Tableaux (`kind: 'decision'`,
  `ref` = reference id or the encoded state). `v8Links.decision(ref)` round-trips because `ref` may hold a whole
  state. **Copier le lien**.
* **URL = state:** `ref` | composed fields `i m u s d h c ag al tg tl l`, `b.*` for B, `n`/`seed`, and the view
  `tab v e g p`. Keyboard: `1–6` switch views, `j/k` change the variable, `Shift+1–5` pick a reference decision.
* **Palette:** 5 « Décision : … » commands, « Comparer BCE +25 pb et +50 pb », variants typed as « bce 25 » /
  « opep -1 », and a local search provider (group « Décisions »).

## 2. The five reference decisions and one comparison (dev DB, read-only, 2026-10-18)

Screenshots, all at 1440 px unless noted: `/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/v8/V8DECISION/shots/`.
I reviewed each one critically and fixed what looked wrong (see § 5).

| Scenario | What the page shows |
|---|---|
| **OPEP+ −2 mb/j** (`1_opec_effets`, `1b_opec_propagation`, `1c_opec_sens`) | 3 variables have a number. World oil price +12,5 % in 2027Q1: a *point* (Caldara–Cavallo–Iacoviello's 0,8 mb/j response scaled linearly; no published uncertainty, flagged), with later quarters marked missing (lag not published). World consumption −4,27 log pts and world production +1,9 come from Baumeister–Hamilton elasticities. World industrial production is missing (hatched). The graph shows the IO and crude-exposure models as **blocked** (no input over the full first year). Evidence for price → consumption shows BH −0,35 (68 % credible interval [−0,51 ; −0,24], verbatim quote of the table) **side by side** with the two CCI elasticities −0,13 / −0,11 (not chosen). In the tornado only BH has a bar (−7,17 → −1,58, r² = 1); CCI is a hatched row (« incertitude non publiée »). |
| **India rice export ban** (`2_india_why`, `7_phone_india`) | « Aucun résultat chiffré : la propagation s'arrête dès le premier lien ». The page says what is missing: an attributed estimate whose cause is `trade.export_restriction`. The nearby estimate, Giordani–Rocha–Ruta 2012 (cause `trade.export_restriction_share`), is listed as not applicable as it stands. |
| **BCE +50 pb** (`3_ecb_effets`) | 20 variables (5 countries × CPI inflation, unemployment, real GDP, consumption deflator), all flagged *distribution incomplète* and *estimation ancienne* (van Els et al. 2001 country rows; OP 377 median for inflation, undated). The consumer price level is listed as an **unresolved tie** (Angeloni et al. AWM −0,42 vs EMM −0,30 per pp, no number), with « Pour débloquer : déclarer une préférence ». |
| **EU ban on Russian oil** (`4_eu_propagation`, dark mode `8_dark_eu`) | RUS GDP per capita: the REVA event-study run +0,18 % median, 5–95 % [−0,79 ; +0,43] by 2028, which is **no detectable effect**. It sits **side by side with the competing Gutmann–Neuenkirch–Neumeier −2,82 %** (cumulative, 2 years, printed « 2.82 » with the drop transformed to a sign; quote shown), never averaged. Growth: Gutmann −1,32 / −1,50 pp (points, « groupe non vérifié »). 8 Wikidata / EUSANCT causal claims appear as unquantified links. |
| **France 65 in 2027** (`5_france_why`) | No result. The retirement model is blocked by **3** demography datasets that are not pinned in the reference release (`demography.population.jan1_age_sex`, `deaths.age_sex`, `live_births…`). The LFS and ESSPROS inputs are now pinned, so the list is shorter than the 7 in V5 § 7. « Niveau cible, pas une variation » lists the 12 estimates of an age *change* that cannot apply to a target *level*. |
| **Comparison BCE +25 vs +50 pb** (`6_ecb_compare`) | Changed field: `magnitude`. The difference is paired draw by draw. Every B − A is exactly half of B (−0,07 % for FRA real GDP in 2028Q4, −0,03 pp inflation…), as the linear chain with common random numbers implies. All are marked « (point) » because no estimate on these chains publishes an uncertainty. |
| **Brief** (`9_brief`) | EU-ban brief preview: label, assumptions, table, each link with citation and verbatim quote, competing estimates, stops. |

## 3. Performance (dev API on this container, warm)

| Measure | Value |
|---|---|
| `GET /templates` | p50 2,2 ms, p95 2,4 ms |
| `POST /run`, cached (5 references × 20 calls) | p50 6,5–11 ms, p95 7,3–12,5 ms (ECB response is 533 KB) |
| `POST /sensitivity`, cached | p95 ≤ 10,4 ms |
| `POST /run`, first computation once warm | India 0,34 s, ECB 1,3 s, EU 1,7 s, France 1,6 s, OPEC 14 s (FIGARO parquet read; now preloaded by the warm-up) |
| Sensitivity, first computation | 0,15–3,5 s (ECB: 33 propagations) |
| Comparison, first computation | 0,96 s |
| Engine warm-up (atlas catalogue + query-file hash + registry index + IO), background thread | ~137 s once per process. The page says so while it runs. |

**Found and fixed on the way (big):** the first ECB run took **616 s**. `RegistryBaseline.baseline` calls
`registry.read.question` once per registry question. That call scans every batch manifest, and the registry's
`ManifestCache` holds 16 manifests while there are 25 batches, so every call re-verifies (re-hashes) all of them. One
variable cost 35–105 s. My `registry_paths` reads each manifest **once** and gives the same values and models; the
equivalence is tested against the V5 adapter on the dev DB. The batch it names is the one that sealed the chosen
forecast; the V5 adapter named the batch of the question listing. With this, ECB runs in 1,3 s.

## 4. Honesty choices

* There is no number that the engine or an estimate's source did not produce. Missing periods, variables and inputs
  are hatched with their reason and never drawn as 0. The fan charts never join a line across a missing quarter.
* The label is on every result, every comparison side and every brief. « pas un conseil financier » shows on the
  market instruments (oil, rates) and in every brief.
* Competing estimates are shown side by side with chosen / not chosen and the engine's rule. They are never averaged.
  An unresolved tie stays without a number.
* Sensitivity uses only *published* uncertainty. An estimate without uncertainty gets no bar, rather than a zero bar,
  and the bars are labelled as one-at-a-time around z = 0. That centre can differ from the Monte-Carlo median and the
  page says so.
* The stored, signed point is shown, with the printed value next to it when they differ. Example: Gutmann « −2,82 % ;
  imprimé : « 2.82 » ».
* A variant is labelled as a variant, both in its title and its id (`<ref>.variant-<hash>`). Presets are inputs, not
  results.

## 5. Critical review of the screenshots (what I changed)

* A missing quarter in the fan chart was drawn as a wide hatch-coloured bar that read like data. It is now a thin
  dotted line.
* The matrix overflowed with 5 countries. Cells are now stacked, the glyph has a track, and the detail panel goes
  below the matrix and splits in two.
* The composer said « variante de » for a reference itself. It now says « point de départ ».
* A variant changed `actor`/`target` by dropping `kg_id`/`label_en`, so the comparison wrongly listed 3 changed
  fields. Fixed: only `magnitude` is listed now.
* « Pourquoi » repeated the answer sentence inside « Effets ». Fixed, and nearby estimates are now open by default.
* Tornado value labels collided. Values now sit in a right-hand column.
* The evidence panel showed competing estimates below a long citation. Competing estimates now come first.
* Direct changes showed raw atlas ids, and plugin stops showed model keys. Both now use French labels (catalogue and
  plugin registry).
* « −2e3 » became « −2 000 ».
* The graph was too small when fitted. It now has a minimum zoom of 0.72 and starts at the decision; pan / pinch
  shows the rest.
* Residual issues, not mine:
  * The V5 `FanChart` prints the « 0 » axis label twice when the maximum is 0 (a small overlap).
  * At the minimum zoom the right part of the graph needs panning on wide chains.

## 6. Missing, and why

* **No validated chain:** `registry/predict/validated_chains.yaml` is empty, so no probability is ever shown. That is
  correct.
* **Rice ban:** it needs an estimate with a single country's ban as its cause, or REVA's own conversion of the ban into
  a share of world rice trade (trade data). Neither exists.
* **France:** 3 Eurostat demography datasets must be pinned in a belief release (a lead operation).
* **ECB:** every chain is a point (no published uncertainty), so the tornado has no bar. Owner decision: declare a
  preference between the Angeloni AWM and EMM models.
* **Server cache is in-process:** it is lost on restart and the warm-up is recomputed. A per-process LRU is enough for
  the dev API.

## 7. Requests to the lead

1. **Contracts:** `contracts/openapi.json` has no `/v1/v8/*` path yet, from any V8 agent, so
   `tests/integration/test_api_contract.py::test_committed_contract_is_current` will fail until someone regenerates
   it. I did not regenerate it, to avoid conflicting regenerations with in-flight routes. Run
   `uv run python scripts/export_contracts.py` then `(cd ui && npm run gen:types)` once V8 routes settle.
2. **Registry read performance (affects `/v1/forecasts/*`, V8SCORES and the V5 `/v1/predict/decision`):**
   `reva.evaluation.registry.read.CACHE = ManifestCache(16)` is smaller than the 25 batches, so `question()` re-hashes
   every manifest on every call. That is 35–105 s per question on the dev DB. Suggested fix, for the owner of
   `evaluation/registry`: size ≥ the number of batches, or index question → batch.
3. **Dev DB schema contract:** the DB is at `0020_v8_boards` while `migrations/schema_contract.json` is still at 0019.
   `reva api` refuses to start unless `REVA_SCHEMA_CONTRACT=warn` is set.
4. **No dependency or contract changes needed.** The only import from another feature is `../tableaux/pin`, the public
   pin API.

## 8. Commands

```bash
uv run pytest -q tests/unit/v8/test_v8_decision_studio.py            # 16 unit (synthetic estimates, API with stubbed auth)
uv run pytest -q tests/integration/v8/test_v8_decision_live_db.py    # 2 on the dev DB, read-only (~60 s cold)
(cd ui && npx vitest run src/v8/features/decision)                    # 17 UI (fixtures = real dev-DB responses)
uv run ruff check src/reva/v8/decision src/reva/api/routes/v8_decision.py && uv run mypy src/reva/v8/decision src/reva/api/routes/v8_decision.py
```

Checks at hand-off: ruff, mypy, tsc clean on my files; 16 + 2 + 17 tests green; `tests/invariants` 191 passed.
