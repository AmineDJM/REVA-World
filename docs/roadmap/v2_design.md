# REVA World V2 roadmap — design and work split

Status: approved by the project owner on 2026-10-15 ("implement the five steps").
Scope: five steps after the MVP (P0–P7). Every step keeps the data constitution:
no fabricated numbers, typed missing values, uncertainty kinds stated, provenance to raw
bytes, capability gaps refused explicitly, LLMs never compute results or create evidence.

## The five steps

| Step | What the user gets | Owner agent |
|---|---|---|
| 1 | The crude border-price scenario for every country with data (≈200 reporters) | ALLC |
| 2 | A sentence in natural language becomes a draft typed scenario the user confirms | NLC |
| 3 | Multi-period dynamics (cohort-component population projection) and a multi-region input-output price model | POP, IO |
| 4 | Households and a retirement-age module for France, back-tested on the 2010 reform | PEN |
| 5 | Retirement-age module extended to other countries; energy price shocks through the IO model for every IO country | PEN (phase 2), IO |

Platform work that the steps share is owned by ENG. The roadmap UI is owned by UIR,
started once the APIs below exist.

## Decisions taken (owner asked the lead to decide alone)

* **New migration `0008_v2.py`** (owner: ENG) for every schema change of this roadmap.
  Migrations 0001–0007 stay with the agents currently hardening them.
* **Model plugins.** A simulation model is a plugin selected by `model_key` in the
  scenario spec. The crude model becomes plugin #1 with bit-identical outputs and digests.
* **Multi-period results.** `sim.result` rows get an optional half-open period
  (`period_start`, `period_end`, UTC dates); single-period models leave them null.
* **Large structured inputs** (IO matrices, age×sex tables) are *dataset inputs*:
  content-addressed Parquet artifacts derived from archived raw source bytes, registered
  with a dataset manifest (source acquisition ids, parser version, schema, row count,
  digest). A belief release pins dataset inputs by digest in its manifest; a scenario
  resolves them only through its baseline release. Scalars stay ordinary estimates.
* **LLM provider.** OpenAI API through the environment's authenticated proxy
  (`api.openai.com`, no key in code or config). Default model `gpt-4.1` (override
  `REVA_LLM_MODEL`), JSON-schema structured output, temperature 0. Gemini is the fallback
  provider behind the same port. The LLM budget becomes non-zero but bounded
  (`budgets.yaml` re-pinned). CI and tests never call an LLM: they use recorded responses.
* **The LLM only drafts.** It turns text into a draft `ScenarioSpec` plus a plain-language
  restatement and a list of unsupported parts. The draft is validated by the same schema
  and compiler as hand-written specs, shown as a diff, and runs only after the user
  confirms it. Unsupported asks become typed `capability_gap` items, never guesses.
* **Literature parameters** (e.g. the employment response to a retirement-age increase)
  enter models as *stated scenario assumptions with citations* and a sensitivity grid,
  not as reviewed evidence claims: no human has reviewed them yet, and the governance
  rules forbid an agent from recording a human review. Output labels say so.
* **New models are experimental.** They are registered through the same seed path as the
  crude model, with `review_status: maintainer_review_pending` in their manifest, and the
  API/UI show "experimental model, not yet reviewed".
* **Real time** means: ingest on schedule, publish a new release when sources change,
  show the knowledge cutoff everywhere. No claim of a live economy.

## Shared interfaces (ENG implements; others code against them)

```python
# src/reva/simulation/plugins.py
class InputRequirement(NamedTuple):
    name: str  # model-local input name
    concept: str  # registry concept id@version
    subject: str  # ISO3 or a registry entity key
    years: tuple[int, ...]
    kind: Literal["scalar", "dataset"]


class SimulationModel(Protocol):
    model_key: str  # "economic.crude_exposure"
    model_version: str
    fidelities: frozenset[str]  # e.g. {"L1", "L2"}

    def validate(self, spec: dict) -> None: ...  # raises ValidationProblem
    def requirements(self, spec: dict) -> list[InputRequirement]: ...
    def compile(self, inputs: ResolvedInputs, spec: dict) -> Any: ...  # pure, no DB
    def compute(self, compiled: Any, fidelity: str) -> list[ResultRow]: ...  # pure, Decimal
    def manifest(self) -> dict: ...  # source digest, assumptions, review_status
```

`ResolvedInputs` gives scalars (Value or typed Missing, with provenance ids) and datasets
(a read-only Polars/Arrow table loaded from the pinned artifact, with its manifest).
A missing required input raises `missing_input` before compute, as today.

`ResultRow` gains `period: tuple[date, date] | None`.

Registration: `src/reva/simulation/plugins.py` keeps `REGISTRY: dict[str, SimulationModel]`;
each model module calls `register(...)` at import; `reva.simulation.plugins.load_all()`
imports the known model modules.

Scenario spec: `contracts/scenario.schema.json` becomes a discriminated union on
`model_key`; common fields stay (baseline release, cutoff, countries, reference year,
horizon, fidelity, missing_input_policy, seed); `intervention`, `assumptions` and
`outputs` are model-specific sub-schemas exported by each plugin. Version 2.0.0; 1.0.0
specs keep validating as crude specs.

## Model specifications

### Step 1 — crude scenario for all countries (ALLC)
* Entities: every UN M49 reporter and World Bank economy (no aggregates), ISO3/M49
  crosswalk from the providers' own reference lists (archived), disputed/composite
  reporters flagged (e.g. 490 "Other Asia, nes" never mapped to an ISO3 silently).
* Ingest within budgets: Comtrade one period per request; reporters batched only if the
  API documents it; World Bank all-country queries paged.
* Gaps typed per country; the scenario runs for the countries with complete inputs and
  refuses with `missing_input` listing the others when `missing_input_policy=fail`; a new
  explicit policy `skip_missing_countries` runs the rest and reports skipped countries.

### Step 2 — natural language to scenario (NLC)
* `src/reva/nl/`: provider port, OpenAI adapter, recorded-response adapter for tests,
  prompt and schema, injection hardening (user text is data; system prompt fixed; no tool
  use; output schema-validated; registry ids only from an allow-list given to the model).
* API: `POST /v1/scenario-drafts` (scope `simulate`) → draft spec, restatement,
  unsupported items, model/provider/version, token usage; nothing runs.
  `POST /v1/scenarios` with the confirmed spec runs as today.
* Budget: per-day token and request caps in `budgets.yaml`, enforced via the ledger.

### Step 3a — cohort-component population projection (POP)
* Built-in reviewed connector `eurostat.demography` (distinct from the autonomy template
  names): population on 1 January by single age and sex (`demo_pjan`), deaths by single
  age (`demo_magec`) or mortality rates (`demo_mlifetable`), live births by mother's age
  (`demo_fasec`), and net migration (residual method documented, or EUROPOP2023
  assumptions `proj_23*` as a labelled alternative). EU/EFTA countries.
* Model `demography.cohort_component@1.0.0`: annual, single age 0–100+, by sex; inputs as
  dataset inputs; assumptions explicit (constant rates, or EUROPOP2023 paths).
* Validation: back-test (project from 2013 → compare to observed 2023 by age and sex);
  publish error metrics; label the projection "conditional projection, not a forecast".

### Step 3b — multi-region input-output price model (IO)
* Data: Eurostat FIGARO inter-country IO tables (EU + main partners incl. USA, CHN, plus
  rest of world), latest year available; or OECD ICIO if FIGARO is unreachable. Archive raw
  bytes; derive Parquet dataset; verify accounting identities (row/column totals).
* Model `economic.io_price@1.0.0`: Leontief cost-push price model, Δp = (I − Aᵀ)⁻¹ Δv
  (equivalently with the inverse transposed), for an exogenous price change of a product
  (crude petroleum / energy) in one or more countries; full pass-through, fixed technical
  coefficients, no substitution, no demand response, single period. Outputs: price change %
  by country × industry and by country for household consumption baskets.
* L2: pass-through share grid (e.g. 0.5, 0.75, 1.0) as a sensitivity range.

### Step 4 — retirement age, France (PEN phase 1)
* Data: Eurostat employment and activity rates by sex and age group (`lfsa_ergan`,
  `lfsa_argan`), pension recipients and expenditure (`spr_pns_ben`, `spr_exp_pens` or
  closest available), population from POP. France legal-age rules as a reviewed rule table
  (2010 reform 60→62 phased by cohort; 2023 reform 62→64 phased) with legal citations.
* Synthetic households: extend the existing population recipe with household size and
  composition margins from Eurostat (`lfst_hhnhtych` or census tables) — P10 capability.
* Model `social.retirement_age@1.0.0`: multi-period (to 2040), by single age 55–70 and sex:
  shift of retirement hazard following the legal-age change; employment response share
  among affected cohorts as a cited assumption with a sensitivity grid; outputs per year:
  employed persons 55–69, pension recipients, old-age dependency ratio, pension expenditure
  change (approximate, average pension × recipients, stated as such).
* Validation: back-test the 2010 reform with observed Eurostat employment rates 60–64
  (2010 → 2019); report predicted vs observed and the error; no tuning on the test period.

### Step 5 — extension (PEN phase 2, IO)
* Retirement-age module for other EU countries with a reviewed legal-age table per country
  (sources cited per row; countries without a sourced table are refused with
  `capability_gap`).
* IO price shocks available for every FIGARO/ICIO country.

## Honesty rules specific to V2

1. Projections are labelled "conditional projection under stated assumptions".
2. Every literature number carries its citation and appears in the sensitivity grid.
3. Back-test results are shown next to the model, including when they are bad.
4. No macro outputs (GDP loss, unemployment, inflation) beyond what a listed model computes;
   the IO model's consumer price effect is labelled "first-round cost-push, full pass-through".
5. Capabilities move from `reserved` to `implemented` in `src/reva/capabilities.py` only
   with a passing test and a doc entry; the capability test enforces this.

## Ownership (V2 agents)

| Agent | Owns |
|---|---|
| ENG | `src/reva/simulation/**`, `migrations/versions/0008_v2.py`, `contracts/scenario.schema.json`, `contracts/scenario.example*.json`, `src/reva/capabilities.py` (V2 entries), simulation sections of `docs/methodology.md` |
| ALLC | entity/country registry files, `src/reva/adapters/connectors/{worldbank,comtrade}.py` country coverage, `registry/entities/**`, crude scenario country handling |
| NLC | `src/reva/nl/**`, `src/reva/api/routes/nl.py` (+ one router registration line in `app.py`), `registry/policies/budgets.yaml` LLM section |
| POP | `src/reva/adapters/connectors/eurostat_demography.py`, `src/reva/models/population/**`, its registry entries |
| IO | `src/reva/adapters/connectors/io_tables.py`, `src/reva/models/economic/io_price.py`, its registry entries |
| PEN | `src/reva/models/social/**`, pension rule tables under `registry/rules/**`, household extension of `src/reva/population/**` |
| UIR | `ui/**` V2 screens (after UI2 finishes) |
