# V5 effect estimates — interface for other agents (PRED-EFFECT)

Agent PRED-EFFECT, 2026-10-17. Spec: `docs/roadmap/v5_prediction_engine.md` (decision 3: effect sizes are
allowed **only as attributed numbers**). This page is the contract PRED-EVENTSTUDY (writes REVA-run estimates),
PRED-ENGINE (reads estimates, samples their uncertainty) and the UI code against. Implementation notes, sources
and counts: `docs/dev/v5_pred_effect.md`.

**REVA asserts no effect.** Every number in `kg.effect_estimate` is attributed to exactly one of:

* a **publication** (citation verbatim, DOI/URL, table/figure/page locator, the verbatim quote containing the
  number as printed, and the sha256 of the archived source file it was read from);
* a **REVA estimation run** (run id, method, data snapshot, code version, as-of — written by PRED-EVENTSTUDY);
* an **identity** (accounting identity or physical law, formula and reference verbatim).

An un-quantified causal claim stays a claim (`kg.causal_claim`, `docs/dev/v3_causal_interface.md`); an estimate
may *link* to claims (`causal_claim_keys`) but never creates or grades one.

## Storage (schema `kg`, migration `0019_effect_estimates`, down_revision `0018_retraction_reasons`)

| Table / view | What |
|---|---|
| `kg.effect_load` | One row per write batch: `kind` (`publication` / `reva_run` / `identity`), source key, dataset (curation file or run family), origin, raw sha256s (each a verified `meta.artifact`; ≥ 1 for `publication`), retrieval time, loader version, parameters. Server `recorded_at`/`recorded_by`. Rows below must be inserted in the transaction that created their load. |
| `kg.effect_estimate` | Append-only estimate versions (a changed estimate = a new row with the same key). |
| `kg.effect_estimate_retraction` | An estimate left the current view (nothing is deleted). Reason: `withdrawn_by_curator` (transcription/curation corrected), `removed_at_source`, `absent_from_delivery`, `run_invalidated`; `detail` jsonb. |
| `kg.effect_estimate_current` | Latest row per (estimate key, origin), not retracted, with the load's `recorded_at`. |

Grants: `reva_worker` INSERT + SELECT; `reva_api`, `reva_sim` SELECT; `reva_gate` nothing; append-only for
everyone (`meta.reject_mutation`); restricted-deletion role covers the tables like `kg.causal_*`.

### Columns of `kg.effect_estimate`

| Column | Type | Meaning / constraint |
|---|---|---|
| `estimate_key` | text | `pub:<source_key>:<entry_id>:<estimate_id>` · `run:<run_id>:<estimate_id>` · `idn:<identity_id>:<estimate_id>` (`^(pub|run|idn):[A-Za-z0-9_.:-]{3,300}$`). Identity = (key, origin). |
| `origin` | text | `live` / `test_fixture` |
| `content_sha256` | text | sha256 of the canonical content (idempotent reloads) |
| `cause_kind` | text | `atlas_concept` · `variable` · `event_type` · `event` |
| `cause_id` | text | atlas concept id (`registry/atlas/catalog/**`) · effect variable id (`registry/effects/variables.yaml`) · event type id (`registry/events/taxonomy.yaml`) · KG event id (`Q…` / `<source>:<id>`) |
| `cause_definition` | text | the shock **as the source defines it**, verbatim ("a 10 percent increase in oil prices") |
| `cause_transform` | text | how the cause is measured: `log_change`, `percent_change`, `pp_change`, `level_change`, `share_of_gdp_change`, `std_dev_shock`, `occurrence` (event), `level`, `growth_rate`, `gap` |
| `effect_kind` | text | `atlas_concept` · `variable` |
| `effect_id` | text | as above |
| `effect_definition` | text | the outcome as the source defines it, verbatim |
| `effect_transform` | text | same vocabulary as `cause_transform` (minus `occurrence`, `std_dev_shock`) |
| `measure` | text | what the number is: `elasticity` (% effect per 1 % cause), `pass_through` (cumulative % of a cause % change passed to the effect, as a ratio), `semi_elasticity` (% effect per unit/pp of cause), `slope` (units of effect per unit of cause, e.g. pp per pp), `multiplier` (currency of effect per currency of cause), `response` (change of the effect for the stated shock), `identity_coefficient` |
| `unit` | text | `ratio`, `percent_per_percent`, `percent_per_pp`, `pp_per_pp`, `pp_per_percent`, `currency_per_currency`, `percent`, `percentage_points`, `log_points`, `unit_per_unit`; allowed per measure (table below) |
| `shock_size`, `shock_unit` | numeric, text | required for `measure = response` (e.g. `10`, `percent`; `1`, `std_dev`; `1`, `percent_of_gdp`; `1`, `occurrence`) |
| `meta.shock_profile` | (jsonb key) | how the cause moves in the experiment the number refers to: `one_time_impulse` (structural shock at t=0, the cause then follows its own dynamics), `sustained` (held over the horizon), `permanent_level_shift` (static / permanent level change), `state_indicator` (episode or status on while it lasts), `contemporaneous_relation` (slope between co-moving variables, no shock path), `cross_sectional`, `unspecified`; `EffectEstimateIn.shock_profile`, JSON `shock_profile`. PRED-ENGINE reads lag profiles as responses to this shock. |
| `sign` | text | `positive` · `negative` · `zero` · `ambiguous` — direction of the effect for an increase (or occurrence) of the cause; must equal sign(point) when a point is given |
| `point` | numeric | point estimate (nullable: sign-only estimates are allowed) |
| `point_kind` | text | `point` · `mean` · `median` |
| `as_printed` | text | the number exactly as printed ("0.36", "−0.4", ".52") |
| `se` | numeric | standard error (> 0) |
| `ci_low`, `ci_high`, `ci_level` | numeric | interval bounds and level in (0,1); `ci_low ≤ point ≤ ci_high` |
| `interval_kind` | text | `confidence` · `credible` · `range_across_estimates` · `interquartile` · `min_max` (null without bounds) |
| `draws` | jsonb | `{"raw_sha256", "path", "n"}`: sample draws archived as an artifact (REVA runs) |
| `uncertainty_status` | text | `reported` (SE or CI) · `partial` (only a range/IQR or only significance) · `not_reported` |
| `horizon_kind` | text | `impact` · `at_horizon` · `cumulative` · `peak` · `average` · `long_run` · `unspecified` |
| `horizon_value`, `horizon_unit` | numeric, text | e.g. `4`, `quarter`; unit ∈ `day`, `month`, `quarter`, `year` (null for `long_run`/`unspecified`) |
| `irf` | jsonb | lag profile: `[{"h": 0, "unit": "quarter", "point", "se"?, "ci_low"?, "ci_high"?, "as_printed"?}, …]`, h ascending, each checked like the main number |
| `scope` | jsonb | `{"countries": ["FRA", …] (ISO 3166-1 alpha-3, or "WLD"), "country_group": "<verbatim, e.g. advanced economies>", "period": {"from": "1970", "to": "2019"}, "population": "<verbatim>", "frequency": "annual|quarterly|monthly", "n_obs": int, "n_countries": int}` |
| `method` | text | `meta_analysis`, `rct`, `iv`, `did`, `rdd`, `synthetic_control`, `event_study`, `local_projection`, `var`, `svar`, `bvar`, `dsge`, `structural_model`, `input_output`, `panel_regression`, `time_series_regression`, `cross_section_regression`, `gravity`, `calibration`, `accounting_identity`, `physical_law`, `other` |
| `identification` | text | identification strategy, verbatim from the source when quoted |
| `meta` | jsonb | meta-analyses: `{"n_studies", "n_estimates", "correction": "<verbatim, e.g. PET-PEESE>"}` |
| `source_kind` | text | `publication` · `reva_run` · `identity` (= the load's kind) |
| `source_key` | text | registry source key (`^[a-z0-9][a-z0-9_.-]*$`) |
| `citation` | jsonb | publication: `{"text": "<reference verbatim>", "doi", "url", "year", "title", "authors"}`; identity: the reference |
| `locator` | text | table / figure / page / equation (publication: required) |
| `quote` | text | verbatim passage containing `as_printed` (publication, `transcribed*`: required, checked) |
| `selector` | jsonb | machine-readable sources: `{"file", "sheet", "row", "column"}` |
| `transcription` | text | `machine_readable` (read by code from a data file) · `transcribed` (typed from a PDF/HTML by an agent) · `transcribed_double_checked` (and the quote was found again, automatically, in text extracted from the archived bytes; the check is recorded in the load parameters) · `computed` (REVA run / identity) |
| `raw_sha256` | text | archived source file (publication: required, ∈ load raw sha256s) |
| `run` | jsonb | REVA run: `{"run_id", "code_version", "data_snapshot", "as_of", "spec_sha256", "replicates": [estimate keys]?}` (required for `reva_run`) |
| `identity` | jsonb | `{"name", "formula": "<verbatim>"}` (required for `identity`) |
| `as_of` | date | knowledge date: when the estimate became knowable (publication date of the version read; a run's data as-of). PRED-ENGINE as-of replays must filter `as_of ≤ t`. |
| `applicability` | text | REVA curator's transportability note, labelled as REVA's (never the source's words) |
| `source_caveats` | text | caveats verbatim from the source |
| `causal_claim_keys` | text[] | optional links to `kg.causal_claim.claim_key` |
| `rights` | jsonb | licence of the source text (`{"licence", "url", "restricted"}`) |
| `curation` | jsonb | `{"curated_by", "curated_at", "review_status": "maintainer_review_pending"}` |
| `retrieved_at` | timestamptz | when the source bytes were fetched (run: when the run finished) |
| `reinstates_row_id` | bigint | an estimate that came back after a retraction |

Measure → allowed units: `elasticity` / `pass_through` → `ratio`, `percent_per_percent`; `semi_elasticity` →
`percent_per_pp`, `percent_per_unit`; `slope` → `pp_per_pp`, `pp_per_percent`, `unit_per_unit`; `multiplier` →
`currency_per_currency`, `ratio`; `response` → `percent`, `percentage_points`, `log_points`; `identity_coefficient`
→ any.

### Horizon convention (FIX4B, audit AUDIT3 M5a)

`horizon_value` / `irf[].h` count periods **from the impact period**: h = 0 is the period of the shock or of the
episode's onset (for an annual event study, the first year of the episode = the onset year), h = 1 the next
period, and so on. A source that numbers years "1, 2, …" from the onset year is stored as h = 0, 1, …
(`horizon_kind = impact` for its first year alone). PRED-ENGINE puts h = 0 on the decision's start year.

### Level vs growth (FIX4B, audit AUDIT3 M3)

A level response (percent / log change, cumulative sum of growth rates ≈ % change of the level) is stored on a
**level** variable; a growth-rate response (pp) on a **growth-rate** variable or concept. Curated identifier links
in `registry/effects/variables.yaml`: a level variable declares `growth_variable` (its growth rate as an effect
variable, e.g. `output.real_gdp_per_capita` → `output.real_gdp_per_capita_growth`) and `growth_concepts` (atlas
concepts that are its growth rate, e.g. `wb.ny.gdp.pcap.kd.zg`). The loader (`reva.effects.consistency`) rejects
`level_transform_on_growth_concept`, `percent_response_on_growth_concept`, `cumulative_lhs_on_growth_concept`
and `growth_rate_on_level_variable` (prefixed `cause_` / `effect_`); PRED-ENGINE refuses the same rows (excluded,
`not_propagatable`) and never converts a level response into a growth rate (a difference of local-projection
horizons is not exactly the growth response: each horizon is a separate regression).

## Effect variables (`registry/effects/variables.yaml`)

Variables that published estimates talk about but that the atlas catalog does not (yet) carry, e.g.
`oil_price.crude_real`, `exchange_rate.nominal_effective`, `fiscal.government_spending`, `trade.trade_cost`,
`labour.unemployment_gap`. Each has `id` (`^[a-z0-9][a-z0-9_.]{2,99}$`), `labels` (en, fr), `definition`
(REVA's, labelled), `unit`, and optional `atlas_concepts: [<concept id>, …]` — a **curated** link to atlas concepts
that measure the same thing (declared by a curator with a note; never inferred from names). PRED-ENGINE resolves
an atlas concept to estimates through `cause_id`/`effect_id` directly *and* through variables listing it.

## Python API (`reva.effects`)

```python
from reva.effects import (
    EffectEstimateIn, Uncertainty, Horizon, IrfPoint, RevaRun,       # inputs
    load_effect_estimates, EffectLoadReport,                            # writer (worker engine)
    list_estimates, get_estimate, compare_estimates, effect_variables,  # readers (any engine)
    normal_approx, sample,                                              # uncertainty for Monte-Carlo
    validate_estimate,                                                  # pure validation (no DB)
)

rep = load_effect_estimates(
    engine, store,
    source_kind="reva_run",              # "publication" | "reva_run" | "identity"
    source_key="reva.eventstudy",        # ^[a-z0-9][a-z0-9_.-]*$
    dataset="banking_crises_gdp",        # curation file / run family (^[a-z0-9][a-z0-9_.-]*$)
    raw_sha256s=[sha],                   # archived artifacts (publication: >= 1; run: results/draws, optional)
    retrieved_at=dt,                     # tz-aware
    origin="live",                       # or "test_fixture"
    complete=True,                       # estimates of (source_key, dataset) absent from the delivery are retracted
    retraction_reason="absent_from_delivery",
    estimates=[
        EffectEstimateIn(
            estimate_id="lp_h0_h8",                       # -> key run:<run_id>:<estimate_id> (pub:<source_key>:<entry_id>:<estimate_id>)
            entry_id=None,                                 # publication: the curation entry id (one paper / table)
            cause_kind="event_type", cause_id="financial_crisis",
            cause_definition="systemic banking crisis onset (Laeven-Valencia 2020 coding)",
            cause_transform="occurrence",
            effect_kind="atlas_concept", effect_id="imf.gdp_real_growth",
            effect_definition="real GDP, log level, percent deviation from pre-crisis path",
            effect_transform="log_level",
            measure="response", unit="percent", shock_size=1, shock_unit="occurrence",
            sign="negative", point=-4.1, point_kind="point", as_printed="-4.1",
            uncertainty=Uncertainty(se=0.9, ci_low=None, ci_high=None, ci_level=None, interval_kind=None,
                                    draws=None, status="reported"),
            horizon=Horizon(kind="at_horizon", value=5, unit="year"),
            irf=[IrfPoint(h=0, unit="year", point=-1.2, se=0.4), ...],
            scope={"countries": ["USA", "FRA"], "period": {"from": "1970", "to": "2017"}, "frequency": "annual"},
            method="local_projection", identification="Jordà local projections; crisis onset as treatment date",
            run=RevaRun(run_id="es-2026-10-17-001", code_version="<git sha>", data_snapshot="atlas:<snapshot id>",
                        as_of="2026-10-17", spec_sha256="<sha>", replicates=["pub:…"]),
            as_of="2026-10-17",
            applicability="advanced and emerging economies 1970-2017; not a forecast for a given country",
            causal_claim_keys=(),
        ),
    ],
)
rep.as_dict()  # {"load_id", "inserted", "new", "new_revision", "unchanged", "reinstated", "retracted",
               #  "rejected_counts", "rejected_sample", "unknown_variables", "elapsed_s"}
```

Rules: idempotent (an unchanged reload writes nothing — no load row); a changed estimate with the same key is a
new row; a retracted estimate that comes back is stored with `reinstates_row_id`; ≤ 20,000 estimates per call;
every rejection is per estimate with a reason (`unit_not_allowed_for_measure`, `sign_mismatch`,
`point_outside_interval`, `ci_level_out_of_range`, `se_not_positive`, `quote_missing_number`, `unknown_cause`,
`unknown_effect`, `raw_not_in_load`, `run_required`, …), never silent. A partly rejected `complete` delivery is
downgraded to partial (nothing retracted). `cause_kind="event"` ids absent from the KG are counted
(`unknown_events`), not rejected; unknown atlas concepts / variables / event types **are** rejected.

Readers (plain dicts in the JSON shape below; `conn` = any SQLAlchemy connection):

* `list_estimates(conn, *, cause=None, effect=None, cause_kind=None, effect_kind=None, country=None,
  method=None, measure=None, source_kind=None, as_of_max=None, include_variables=True, origin="any",
  limit=100, after_id=None) -> list[dict]` — `cause`/`effect` match `cause_id`/`effect_id`; with
  `include_variables`, an atlas concept id also matches estimates on variables whose `atlas_concepts` list it
  (each such hit carries `matched_via: "variable:<id>"`). `country` matches `scope.countries` (ISO3) or
  `"WLD"`. `as_of_max` (date) keeps estimates knowable then (as-of replays).
* `get_estimate(conn, estimate_key, *, origin="any") -> dict | None`.
* `compare_estimates(conn, *, cause, effect, …same filters) -> dict` — every current estimate for the pair,
  grouped by **comparability key** (`measure`, `unit`, `cause_transform`, `effect_transform`, `horizon_kind`,
  `horizon_value`, `horizon_unit`, `shock_size`, `shock_unit`); per group: n, min, max, median of the points
  (descriptive only — REVA does **not** pool or meta-analyse), methods, countries. Groups are never merged.
* `effect_variables() -> list[dict]` — the vocabulary with labels and curated atlas links.
* `normal_approx(est) -> {"mean", "sd", "basis"}` — `basis` = `se` (sd = SE), `ci` (sd = half-width / z(level),
  symmetric-interval assumption stated), `range` (`partial`: half-range / 2, labelled `heuristic_range`) or
  `None` (no uncertainty: PRED-ENGINE must not invent one; it should treat the estimate as a scenario point).
* `sample(est, n, rng) -> numpy.ndarray` — draws from `normal_approx` (or the archived draws when `draws` is
  set and the caller passes the store); raises when `basis` is `None`.

## HTTP API (`read` scope, RFC 9457 problems, envelope as `/v1/kg`)

| Endpoint | Data |
|---|---|
| `GET /v1/effects/estimates?cause&effect&cause_kind&effect_kind&country&method&measure&source_kind&as_of_max&origin&limit≤500&cursor` | current estimates (JSON below), newest first by id; envelope warning `attributed_estimates` |
| `GET /v1/effects/estimates/{estimate_key}` | one estimate (404 unknown) |
| `GET /v1/effects/compare?cause&effect&…` | side-by-side groups (above) with the warning `no_pooling` |
| `GET /v1/effects/variables` | effect variables vocabulary |

### Estimate JSON

```json
{
  "estimate_key": "pub:effects.literature:ecb_op113_2010:consumer_prices_total_ea",
  "row_id": 12,
  "cause": {"kind": "variable", "id": "energy_price.all_products", "label": "Energy prices", "label_fr": "Prix de l'énergie",
            "definition": "10% increase in energy prices: coal/lignite/peat (NACE 10), …", "transform": "percent_change"},
  "effect": {"kind": "atlas_concept", "id": "imf.cpi_index", "label": "…", "label_fr": "…",
             "definition": "Total (direct + indirect) increase in consumer prices before taxes", "transform": "percent_change"},
  "measure": "response", "unit": "percent", "shock": {"size": 10, "unit": "percent"}, "shock_profile": "permanent_level_shift",
  "sign": "positive", "point": 0.36, "point_kind": "point", "as_printed": "0.36",
  "uncertainty": {"se": null, "ci_low": null, "ci_high": null, "ci_level": null, "interval_kind": null,
                  "draws": null, "status": "not_reported"},
  "horizon": {"kind": "unspecified", "value": null, "unit": null}, "irf": [],
  "scope": {"country_group": "Euro area (12-country weighted average)", "period": {"from": "2005", "to": "2005"}},
  "method": "input_output", "identification": "Static Leontief cost-push calculation …", "meta": null,
  "source": {"kind": "publication", "source_key": "effects.literature", "dataset": "ecb_op113_2010",
             "citation": {"text": "…verbatim…", "doi": null, "url": "https://www.ecb.europa.eu/pub/pdf/scpops/ecbocp113.pdf", "year": 2010},
             "locator": "Table 12, row 'Total', column 'euro area' (pdf p.77)", "quote": "…0.36…",
             "selector": null, "transcription": "transcribed_double_checked", "raw_sha256": "d38a…",
             "run": null, "identity": null, "rights": {"licence": "…"}, "retrieved_at": "2026-10-17T…"},
  "as_of": "2010-06-01",
  "applicability": "REVA note: …",
  "source_caveats": null,
  "causal_claim_keys": [],
  "curation": {"curated_by": "agent PRED-EFFECT", "review_status": "maintainer_review_pending"},
  "wording": "According to <citation> (Table 12 …), a 10 percent increase in energy prices is associated with a 0.36 percent change in consumer prices (input-output computation); REVA does not establish this effect.",
  "wording_fr": "Selon <citation> (…), …; REVA n'établit pas cet effet.",
  "origin": "live", "recorded_at": "…", "load_id": 3
}
```

(The example's numbers are illustrative of the shape; the real rows are listed in `docs/dev/v5_pred_effect.md`.)

## For PRED-EVENTSTUDY

* Write with `source_kind="reva_run"`, one `dataset` per run family; `run.replicates` lists the `pub:` keys of the
  published results you reproduce; the replication comparison itself is `compare_estimates`.
* Archive result tables / draws as artifacts first (`store.put` + `meta.artifact` via the usual helpers) and pass
  their sha256s; `draws` points at one of them.
* Never write `source_kind="publication"`: published numbers are PRED-EFFECT's curation files.

## For PRED-ENGINE

* Use only `kg.effect_estimate_current` (or the readers above); show `wording`/`wording_fr` and the source for
  every hop; respect `as_of_max` in replays; never fabricate uncertainty (`normal_approx(...)["basis"] is None`
  ⇒ scenario point only, no probability).
* Comparability: do not average estimates across comparability keys; pick explicitly and record the choice.

## Curated publications (implementation note, PRED-EFFECT)

Published numbers come from `registry/effects/literature/<entry>.yaml` (one publication = one `dataset`), loaded
by the connector `atlas.effects_lit` (`reva.effects.curation`): the document is fetched through the metered client
and archived, its sha256 must equal the pinned one, and every `quote` is re-found automatically on its `pdf_page`
in the text extracted from the archived bytes before the estimate is loaded (`transcribed_double_checked`).
`curation.printed_transform = "negated"` marks a coefficient the source prints with the opposite sign convention
(e.g. a Phillips curve written `π = … − κ u`): `point` is the response of the effect to an increase of the cause.
