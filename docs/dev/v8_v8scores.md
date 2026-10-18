# V8 — Scores: the honest scoreboard (agent V8SCORES)

Agent V8SCORES, 2026-10-17/18. Feature `scores` (route `/v8/scores`, order 60, `g p`). It shows REVA's numeric
forecasts against every official forecaster (IMF WEO, SPF, ECB SPF, EIA STEO, CDC hubs, ViEWS, GDPNow, NY Fed,
Cleveland Fed…), with wins **and** losses. Owned files: `registry/scoreboard/`, `src/reva/v8/scores/`,
`src/reva/api/routes/v8_scores.py`, `ui/src/v8/features/scores/`, `tests/unit/v8/test_v8_scores_*.py`,
`tests/integration/v8/test_v8_scores_live_db.py`.

## 1. Answer (as the page shows it, registry built 2026-10-18)

**Against official forecasters, REVA beats 7 · does not beat 160 · ties 3 · awaits 18** (188 judged comparisons:
one target × horizon × metric × Holm family each).

| Track | bat | ne bat pas | égalité | en attente |
|---|---|---|---|---|
| BEAT-MACRO (holdout, 7 hypotheses; dev cells without a hypothesis) | 1 | 22 | 0 | 1 (sealed `forecast.bm_stack`, resolves with the Oct. 2027 WEO) |
| BEAT-ENERGY (127 pre-registered tests on archived STEO issues) | 6 | 121 | 0 | 0 |
| BEAT-HEALTH (dev; H1/H2 holdouts) | 0 | 3 | 3 | 6 (H1 not before 2027-07-03; H2 undated) |
| BEAT-CONFLICT (vs ViEWS) | 0 | 1 (informational V6-seen window) | 0 | 6 (W3 sealed, months to 2027-04) |
| BEAT-NOWCAST (holdout, 5 hypotheses) | 0 | 5 | 0 | 0 |
| PRED-MODELS V6 (points vs WEO/SPF, QA-PRED) | 0 | 4 | 0 | 0 |
| FIX7 fair benchmark (dev) + prospective probation | 0 | 3 | 0 | 2 (April 2027 WEO) |
| PRED-ENERGY V6 (points vs STEO) | 0 | 1 | 0 | 0 |
| BEAT-ENSEMBLE (live registry) | 0 | 0 | 0 | 3 (≥ 20 resolved pairs per cell) |

The 7 wins, verbatim from their sources: G7 unemployment, current year: *REVA's combination of the WEO with its
monthly nowcast beats the WEO alone on the same date* (AE ×0.63, Holm p 0.026, m = 7; uses the IMF forecast); Brent
and WTI monthly mean, **current month only**, between two STEO releases: 3 + 3 tests (daily-price nowcast, its
combination with STEO, its density; ×0.44–0.58), i.e. **timeliness wins** (fresher daily prices; final-vintage
outcome; Refinitiv licence to check). The BEAT-ENERGY W2 formal Holm win (non-OPEC liquids h3) is **downgraded** to
"ne bat pas" with the report's verbatim quote ("void: its outcome series was redefined").

Counted apart, never in the headline: 3 V6 "wins" against a **weak reference** (REVA-built density around the
IMF/SPF point, QA-PRED X1) and 9 non-wins; naive references (persistence, random walk): 12 bat (BEAT-CONFLICT W1,
final-vintage backtest), 18 ne bat pas, 3 pending.

## 2. Registry `registry/scoreboard/tracks.yaml` (machine-readable)

Generated, never hand-edited: `uv run python -m reva.v8.scores.build` (`--check` exits 1 if stale). 233 rows from
18 source files (result JSON/YAML + reports), each row: track, window, target, horizon, official forecaster,
benchmark + kind (`official_point`, `official_fair_density`, `official_ensemble`, `weak_reference`, `naive`),
candidate + kind (pure REVA / combination with the official / post-processed official / REVA ensemble), evaluation
kind (development / pre-registered holdout / pre-registered backtest on vintages / prospective), metric, sample,
ratio (or ratio range for QA-PRED quotes), raw p, Holm p (`source` or `computed` over a family the source lists in
full), family size, coverage, verdict, verdict basis, caveat codes (22 French texts: final vintage, weak
benchmark, outcome-vintage mismatch, timeliness edge, uses the official forecast, licence, report pending…), source
`{file, JSON pointer, fields, sha256}`, report, protocol `{file, sha256}`, resolution `{date|month|not_before,
note, verbatim quote}` and `registry_model` (to check live whether it is sealed). Plus forecasters, domains,
targets, tracks, "pas encore de piste" entries (each with a verbatim reason) and dated events.

**Validator** (`reva.v8.scores.registry.validate`, used on load): `bat` only if the source file exists, the pointed
record has a true win flag, ratio < 1 and the evaluation is not development, never from a quote; a source win
shown otherwise needs a verbatim report quote (downgrade only); `égalité` must be quoted; every quote must appear
verbatim (whitespace-normalised) in its file; ratio and p must equal the source's; a computed Holm p must agree with
the source's own Holm decision; pending rows need a date or rule; ids unique; vocabularies closed. On load, a row
whose source no longer supports it is **quarantined** (shown, never counted) instead of breaking the page; strict
mode raises. Changed source files are listed as stale.

## 3. API (`/v1/v8/scores`, read scope, envelope `context_mode=evaluation`)

| Endpoint | What | Warm time (dev box, loaded) |
|---|---|---|
| `GET /v1/v8/scores` (alias `/matrix`) | page bundle: headline, by-evaluation counts, wins, matrix, tracks, calendar, vocab, stale/quarantined, sealed models | 124–205 ms (40 KB); page computation itself 19 ms |
| `GET /v1/v8/scores/tracks/{id}` | all rows, Holm families, charts copied from result files (ratio by origin month / STEO delay / horizon, PIT histograms, coverage), calendar | 120–230 ms |
| `GET /v1/v8/scores/cells/{target~forecaster}` | rows of one cell | 80 ms |
| `GET /v1/v8/scores/live` | registry counts (both registries), next expected resolutions per family with the officials sealed beside them, ensemble cells by verdict | warm < 1 ms; cold: partial counts within 2.5 s, full state 15–34 s in the background (manifest seal verification), then cached per manifest sha forever and refreshed stale-while-revalidate on registry change |
| `GET /v1/v8/scores/calendar?live=` | pending tests + events (+ live dates) | static part < 50 ms |
| `GET /v1/v8/scores/doc?path=` | text of a report/protocol cited by the registry (whitelist only) | < 20 ms |

Live (2026-10-17 run): 25 sealed batches, 5,950 questions, 28,599 forecasts, **0 resolved**, first expected
resolutions 2026-10-18/25 (NHSN, CDC ensembles sealed alongside). The Accueil feature (V8BOARDS) already consumes
`/live` and `/calendar?live=false`; a cold `/live` returns `partial: true` with only the counts it knows (its zod
schema then falls back to its own path — no resolution count is ever invented).

## 4. UI (`ui/src/v8/features/scores/`)

- **Headline**: one sentence with the four counts, a segmented bar, then the wins grouped by target (3 lines: G7
  unemployment, Brent, WTI), each test listed with its benchmark, metric, ratio, Holm p and caveat chips; the
  by-evaluation table; the weak-reference and naive counts stated apart; quarantine/stale warnings.
- **Matrix** (rows = targets by domain, columns = 12 official forecasters + naive column): each cell shows ✓/✗/=/⏳
  counts, a segmented bar (losses never hidden behind one colour) and the ratio range; "pas encore de piste"
  cells hatched with their quoted reason; filters `?f=` (one forecaster, with its own count) and `?d=` (domain);
  arrow-key navigation; a cell opens a drawer (`?cell=`) with its rows.
- **Track view** (`?track=`, = `v8Links.scoreboard(track)`): summary, protocol + report links (reader overlay
  `?doc=`, sha256 shown; "rapport attendu … pas encore publié" when absent), counts, Holm families, charts, every row
  (expand: basis, evaluation, kinds, coverage, resolution + sealed status, source file#pointer + sha, quote,
  caveats).
- **Calendar**: pending tests (H1 2027-07-03, W3 after 2027-04-30, Oct. 2027 WEO, April 2027 WEO…) and quoted
  events (22 Oct. NHSN, mid-Dec. FFPI/HICP/Brent, Nov. STEO git record), then the live registry's next dates with
  J−n and the official forecasts sealed beside them; overdue groups stated.
- **Registre prospectif** stats, **Pistes** list, **Comment REVA est jugée** (same date, vintages,
  pre-registration, proper scores, Holm, what does not count).
- Palette: « scores », « scores FMI/IMF/WEO », « scores SPF », « scores BCE », « scores EIA », « scores CDC »,
  « scores ViEWS », « scores GDPNow »…, « scores énergie/macro/santé/conflits/agriculture/finance/nowcast »,
  « calendrier des résolutions », « comment REVA est jugée ».

Screenshots (scratchpad `v8/v8scores/shots/`, reviewed and iterated): desktop 1440 — headline fits the first
screen with the 3 grouped win lines; matrix sparse by nature (most forecasters publish few of the targets), WEO/EIA
columns dense, nowcast row now real (BEAT-NOWCAST); drawer at right with green-tinted win rows; track view with 7
ratio-by-month charts (WEO combination below 1 from April on), coverage dot plot (densities under-cover: 56–67 %);
dark theme readable; phone 390 px: no horizontal page scroll (matrix and tables scroll inside their boxes, sticky
target column), drawer as bottom sheet. Fixed during review: filters overflowing at 390 px (185 px), naive column
showing "à juger" (counted with headline rule), chart titles using model keys, heavy tint in dark mode, matrix
cut at WASDE.

## 5. Honesty choices

- Headline = comparisons with an official forecaster only; development rows can be "ne bat pas"/"égalité" but
  never "bat"; weak references and naive references counted apart and labelled on every row.
- Every win carries `uses_official_forecast` when the candidate uses it, `timeliness_edge` when it uses fresher
  data; wording follows the reports ("la combinaison bat l'officiel seul").
- Downgrades only, quoted; no verdict computed by this agent; Holm p recomputed only where the family is fully
  listed and checked against the source's own decisions.
- Missing ratios render as "manquant", pending as "à venir", never 0; a partial live state shows no resolution
  count.

## 6. Tests

`tests/unit/v8/test_v8_scores_registry.py` (20: committed file valid; unchanged-source rows equal a rebuild;
11 tamper cases caught; quarantine on load; Holm; headline excludes weak/naive; wins structured with caveats;
known verdicts copied; bundle/calendar; track detail + doc whitelist), `test_v8_scores_api.py` (2, minimal app),
`tests/integration/v8/test_v8_scores_live_db.py` (2, dev DB read-only: counts equal SQL, warm path, cold partial).
UI: `model.test.ts` (5), `ScoresPage.test.tsx` (6). ruff, mypy, tsc clean on my files.

## 7. Requests / open

1. Lead: link `/v8/scores` from the V8 nav is automatic; consider a shell-level warm-up hook (the app uses a
   lifespan, so routers cannot register startup handlers) to prefetch `/v1/v8/scores/live` at boot.
2. When BEAT-AGRI opens its windows or BEAT-NOWCAST/BEAT-MACRO reports change, rerun
   `uv run python -m reva.v8.scores.build` (the page flags stale sources meanwhile).
3. Contract: no change needed (`v8Links.scoreboard(track)` → `?track=`).
