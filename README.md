# REVA World

REVA World is a private, single-user research platform: a versioned register of
official statistics (evidence with revisions and provenance down to the raw
bytes), immutable release-pinned belief estimates, and a small set of scenario
models that run against a pinned release. It is not a world model and makes no
causal, welfare or forecast claims about the economy. Unsupported questions get a
typed `capability_gap` or `missing_input`, never a made-up number.

**Status (2026-10-16): not deployed.** Nothing runs in production yet (see
"Blocked on credentials" below). The phase/gate ledger is
[docs/implementation_status.md](docs/implementation_status.md).

## What it does

### V1 (MVP, phases P0–P7)

* **Evidence**: World Bank WDI (population, nominal GDP), UN Comtrade public
  preview (HS 2709 crude values), Natural Earth 1:110m (display geometry only),
  Crossref (DOI metadata only) and, through the template-only autonomy path,
  Eurostat `demo_pjan`. Missing source values stay typed `Missing`, never zero
  (e.g. Algeria has no Comtrade crude rows for 2020–2023).
* **Beliefs**: immutable releases selected by `source_select_v1`, with as-of
  reads of what REVA actually published.
* **Scenario**: `economic.crude_exposure` 1.0.0, a one-year border-value
  accounting stress test (L0 cache, L1 fixed quantities, L2 labelled elasticity
  sensitivity range with no probability level). L3/L4 and phases P8–P12 are
  interface-only and refuse with structured capability gaps.
* **Evaluation**: a revised-data retrospective forecast tournament and three
  labelled replay modes.
* **Governance**: approvals are enforced in the database; the promotion gate is a
  separate process with its own login (`reva_gate`).

### V2 (2026-10-15; new models are experimental, maintainer review pending)

Every V2 model result is labelled "experimental model, not yet reviewed".

| Capability | Label / limit | Details |
|---|---|---|
| Crude scenario for every country with data (`countries: ["ALL"]`, `skip_missing_countries`) | Countries without complete inputs are skipped with typed reasons, never zero-filled; in the live throwaway run 74–89 countries per reference year (2020–2024) had complete inputs | [docs/dev/v2_allc.md](docs/dev/v2_allc.md) |
| Natural language → draft scenario (`POST /v1/scenario-drafts`, French/English) | The LLM only drafts; nothing runs until the user submits the spec. Disabled by default (`REVA_LLM_ENABLED=false`). OpenAI `gpt-4.1` verified live; the Gemini fallback was not (the provider answered 402) | [docs/dev/v2_nlc.md](docs/dev/v2_nlc.md) |
| Cohort-component population projection `demography.cohort_component` (EU-27 + EFTA) | "Conditional projection under stated assumptions, not a forecast"; on the 2013 → 2023 back-test a constant-growth benchmark beats it on totals in most countries | [docs/dev/v2_pop.md](docs/dev/v2_pop.md), [back-test](docs/dev/v2_pop_backtest.md) |
| Multi-region input-output price model `economic.io_price` (Eurostat FIGARO 2024, 49 countries) | "First-round cost-push under full pass-through; not an inflation forecast". The household basket is at basic prices, and a purchasers'-price reading is attached | [docs/dev/v2_io.md](docs/dev/v2_io.md) |
| Retirement-age model `social.retirement_age` (FRA, DEU, ESP, NLD rule tables; ITA, BEL, SWE, POL refused) and household margins | Conditional projection to 2040. The France 2010-reform back-test fits, but two errors of opposite sign offset each other: the fit is not validation | [docs/dev/v2_pen.md](docs/dev/v2_pen.md), [back-test](docs/dev/v2_pen_backtest.md) |
| Back-test API (`/v1/models/{key}/backtests`) | Revised-data retrospectives, shown whether good or bad | [docs/api.md](docs/api.md) |
| UI V2: world map, scenario composer, multi-period run pages | E2E uses recorded LLM responses and fixture data (labelled TEST FIXTURE); the IO screens run on a synthetic table | [docs/dev/ui_v2.md](docs/dev/ui_v2.md) |

An independent scientific review of the V2 models is in
[docs/dev/review_science_v2.md](docs/dev/review_science_v2.md). Its findings and
the fixes are tracked in the agents' reports.

### V3 "the whole planet, connected" (2026-10-16; completion report [docs/final_report_v3.md](docs/final_report_v3.md))

V3 adds four connected layers. The [world graph](docs/roadmap/v3_world_graph.md) and
[causality](docs/roadmap/v3_causality.md) roadmaps hold the honesty rules: every
statement keeps its source, a path is not a cause, a causal link is a claim made
by a named source and graded, and missing data stays missing.

| Capability | API (read scope) | Label / limit |
|---|---|---|
| **Atlas**: 108 sources, ~9,250 concepts, 251 country entities. A headline layer (27 concepts) names the answering source and every series consulted | `/v1/atlas/{concepts,countries/{iso3},map,series,coverage}`, `/v1/atlas/headline/{concepts,map,series,coverage}` | Cells are what each source reports. Projections never answer by default. Source changes inside a series are flagged. Licences are shown per cell (`licence_restricted`) |
| **Knowledge graph**: Wikidata plus specialised datasets (UCDP, UN votes, CShapes, Cliopatria, Glottolog, treaties, …) | `/v1/kg/entities/{qid}[/neighbours,/timeline]`, `/v1/kg/path`, `/v1/kg/search` | Wikidata is labelled community-edited. The Wikidata harvest is partial: WDQS was throttled, and countries plus historical and sovereign states are loaded in a separate harvest database |
| **Links** ("neurons"): 38 country–country link types, materialised store, constrained path engine | `/v1/links/{iso3}`, `/v1/links/pair?a=BRA&b=PRT`, `/v1/links/path` | "Connected via", never a cause. Every link cites its statement or raw cell |
| **Events and causality**: 82 event types; causal claims from Wikidata (T3/T4) and expert-coded datasets (T1: ICB, MID, UCDP, NAVCO, Archigos, …), deterministic A–E grades; quarantined Wikipedia candidates (T5) | `/v1/events[/{id},/search,/types]`, `/v1/causal/events/{id}[/causes,/effects,/why]`, `/v1/causal/chain`, `/v1/causal/consistency`, `/v1/causal/candidates` | REVA asserts no causation ("selon <source>"). A chain is REVA's composition, graded by its weakest hop. No claim is reviewed |
| **Simulations for all countries**: cohort projection 1.1.0 (UN WPP 2024, 231 countries), retirement age 1.1.0 / 1.2.0 (163 WBL 2026 rule tables, pension coverage) | as V2 (`/v1/simulations`, `model_version`) | Experimental, maintainer review pending. Conditional projections, not forecasts |
| **Questions**: `POST /v1/ask` (French/English), answers only from cited KG, atlas, links and causal evidence | `/v1/ask` | The LLM planner is not verified live (no credits); the deterministic keyword planner runs |
| **UI "Planète"** (`/v3`): Neurones, Chemin, Frise, Atlas, Dossier pays, Pays ↔ Pays, Causes & conséquences, Demander | — | Same API, same labels |

## Run it locally

Requirements: Docker (PostGIS), [uv](https://docs.astral.sh/uv/) with Python
3.12, Node 22 for the UI. `make help` lists every target.

```sh
make env          # .env from .env.example (local-only passwords)
make up           # PostgreSQL 17 + PostGIS in docker compose
make sync         # locked Python dependencies
make bootstrap    # migrations + runtime role provisioning (same code as the Render pre-deploy)
make seed         # registry: concepts, entities, sources, models, schedules
make fixtures     # captured source fixtures (origin test_fixture), or: make ingest-live
make api          # API + UI on http://localhost:8000
make worker       # durable worker and scheduler
make gate         # independent promotion gate (login reva_gate), next to the worker
make ui-dev       # optional: Vite dev server proxying /v1 to the API
```

Without `make gate`, autonomous discovery reports `gate_pending` and promotes
nothing. The worker's `gate_followup` schedule promotes a late pass once the gate
has recorded it. See [docs/operations.md](docs/operations.md) ("Promotion gate").

Checks:

* `make check`: lint, mypy, registry/contract validation, Blueprint validation and
  offline pytest. DB tests create a fresh database per session; the dev database is
  never used.
* `make ci`: also the UI checks and the e2e suite.
* `make e2e` (= `uv run python scripts/e2e_stack.py --run`): a disposable stack
  (fresh database, offline fixtures) and the Playwright suite.
* V2 suites: `uv run python scripts/e2e_stack.py --recorded-drafter --v2-fixtures --run`.
  This replays recorded LLM responses (no live LLM call) and ingests the V2 fixtures.
* `make test-live`: live provider smoke tests (network).

### V3 quick start (atlas, graph, links, causality)

Run these after `make bootstrap` (migrations to head `0017_links_retention`). Ingests are
budget-metered: the daily cap is 3 GiB in all, so run big sources one at a time and
keep at least 5 GB of disk free. Resume notes are in
[docs/dev/HANDOFF_V3.md](docs/dev/HANDOFF_V3.md).

```sh
uv run reva atlas list                             # every atlas source (discovered from atlas_*.py)
uv run reva atlas ingest atlas.worldbank           # one source at a time; --fixture replays recorded responses
uv run reva atlas snapshot --skip-unchanged --warm # pin the tables, build the query file
uv run reva links materialise                      # country–country link store (worker job links_materialise)
uv run reva causal refresh                         # KG causal statements -> claims, then A–E grades
uv run python -m reva.kg.harvest status            # Wikidata harvest (resumable per spec)
uv run python -m reva.kg.harvest run registry/kg/queries/history --via api
make api                                           # then open http://localhost:8000/v3 ("Planète")
```

The worker schedules the same jobs (monthly atlas sources, daily snapshot, then
links, then the causal refresh). Example reads with a `read` token:
`GET /v1/atlas/headline/series?concept=reva.population.total&iso3=FRA`,
`GET /v1/links/pair?a=DZA&b=FRA`, `GET /v1/causal/events/Q361/why`,
`POST /v1/ask {"question": "Liens entre le Brésil et le Portugal"}`.

V3 end-to-end suite on a disposable stack (KG/links fixture, 12 atlas sources and
the causal fixture replayed offline):
`uv run python scripts/e2e_stack.py --v3-fixtures --run`.

## Documentation

* Read before relying on any output: [limitations](docs/limitations.md),
  [methodology](docs/methodology.md) and
  [scientific validation](docs/scientific_validation.md).
* System: [architecture](docs/architecture.md), [API](docs/api.md) and
  [data constitution](docs/data_constitution.md) (the 35 invariants).
* Security and operations: [threat model](docs/threat_model.md),
  [operations](docs/operations.md) and [deployment](docs/deployment.md).
* Decisions: [ADRs](docs/adr/) and the [V2 roadmap](docs/roadmap/v2_design.md).
* Status: [phase/gate ledger](docs/implementation_status.md); agents' and
  reviewers' reports are in [docs/dev/](docs/dev/).

## Blocked on credentials

The following cannot be done from the repository. See
[docs/deployment.md](docs/deployment.md).

* **Render deployment**: a Render account/API key.
* **Production object storage**: an S3-compatible private bucket with three
  scoped key pairs (read-only for `reva-api`, prefix-scoped write for
  `reva-worker`, a separate prefix-scoped pair for `reva-gate`). The S3 adapter
  and the credential split were verified locally against a self-built MinIO,
  not against a provider bucket.
* **Measurements on the named Render plans**: available only after deployment.
* **Live NL drafting in a deployment**: `REVA_LLM_ENABLED=true` and an egress
  proxy that injects the OpenAI credential (REVA reads no API key). In
  production, settings also require `REVA_LLM_MONTHLY_BUDGET_USD > 0` when the
  LLM is enabled. The Gemini fallback needs a funded account.
* **Optional**: `REVA_COMTRADE_API_KEY` and an external sandbox runner
  (`REVA_SANDBOX_ENDPOINT`). Without the runner, generated code stays blocked.
