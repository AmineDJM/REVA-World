# Scientific review of the V2 models (agent SCIV2, 2026-10-15)

Adversarial referee review of `demography.cohort_component`, `economic.io_price`,
`social.retirement_age`, `nl.scenario_drafts` and the cross-cutting labels and claims. No file in
`src/`, `migrations/` or `tests/` was edited. Reviewer scripts are in the session scratchpad,
`.../scratchpad/sciv2/` (`indep_ccm.py`, `run_model.py`, `bench.py`, `indep_io.py`, `flip.py`,
`nl_probe.py`, `nl_live.py`). The dev database `reva` was not touched. The live LLM calls (3 of the
5 allowed) were charged to a throwaway database ledger (`reva_sciv2_aff6f633`, bootstrapped and
then dropped). The FIGARO 2024 table was re-downloaded from Eurostat (26 requests, ~52 MB) into the
scratchpad.

**Counts:** Critical 0 · High 4 · Medium 9 · Low 7. "Verified" means I reproduced the finding by
running code. "Reasoned" means it follows from the method but I did not measure it.

## What holds up (verified)

* **Population numerics.** All 96 archived Eurostat inputs match the sha256 values in
  `v2_pop_backtest_evaluation.json`. My float64 cohort-component re-implementation
  (`indep_ccm.py`) does not import reva. It reproduces the primary back-test for FRA, AUT and PRT to
  the person. FRA 2023 projected total: 67,637,915 in both. Every group APE matches to 3 decimals,
  for both migration variants and the literal design. TFR 2.01 and e0 84.6 F / 78.0 M (FRA
  2008–12) are plausible. The summary statistics are correct: median APE 2.67 / 4.84 / 5.56; 23 and
  21 of 31 countries. `implementation_digest` equals the committed code.
* **IO numerics.** The synthetic hand case (2/97, 0.6/97, 3.12/0.97 %, θ = 0.5 → 1.007557 %) is
  correct by hand. `indep_io.py` is an independent numpy implementation run on freshly downloaded
  FIGARO 2024 data. It gives max |row − column total| = 1.3e-8 MEUR. For B +10 % in every economy,
  the FRA household basket is **0.387945 % (θ = 1) and 0.117169 % (θ = 0.5)**, both exactly the
  documented values. The all-energy FRA basket is 0.9315 % (doc 0.93). The transpose convention is
  right: `A[i,j] = Z[supplier c_orig/ind_ava, user c_dest/ind_use] / x_j`, and the RHS is
  `A_SEᵀ π`. FRA households' basic-price shares are C19 2.16 % and D35 3.26 %, which confirms the
  "5.4 %, direct ≈ 0.54 %" part of the ECB-gap explanation.
* **Rule transcription.** I spot-checked these rows against the archived excerpts, and all match:
  FRA L161-17-2 1°–7° (incl. 62 y 9 m for 1963–03/1965) and D161-2-1-9 2°–6°; DEU §235 table
  (1959 → 66 y 2 m, 1963 → 66 y 10 m); ESP DT 7ª 2025/2026/2027 short and long tracks; NLD 7a(1)
  e–n.
* **Labels and uncertainty.** "Conditional projection … not a forecast", "first-round cost-push …
  not an inflation forecast" and "experimental model, not yet reviewed" are on every result path
  I read. The kernel (`UncertaintySpec`) rejects a `level` on `sensitivity_range`, and all three
  L2 range builders send none. Every test cited by the five V2 capability descriptors exists.
* **Back-test design (POP).** `project()` refuses a base period or migration window that does not
  end before the jump-off. The primary design (2008–12 → 2013) does not use events after the
  jump-off.

## High

**H1 — Census breaks enter the default population projection as permanent migration.** *(POP,
verified)*
* `average_residual` is the L1 default, and PEN uses it for its population. It averages residuals
  that contain census revisions, and nothing flags this beyond the raw status-flag dump.
* POL, base 2019–23: the 2020 residual is **−784 k**. That is the 2021 census break (`b`); the
  other years are −62 k to +57 k. Averaged, it becomes ≈ −146 k persons per year for 25 years.
  POL 2050 L1 = 25,773,187 (the number in the `v2_pop.md` live table, −25.6 % vs EUROPOP). The same
  run with window 2021–23 gives 29,731,338, and with 2015–19 it gives 30,121,461.
* DEU: the 2022 residual is +172 k against roughly +1.4 M actual net migration (Ukraine). A census
  revision (`2023:b`) is offsetting it.
* DEU 2010: −1.40 M, confirmed independently.
* The back-test doc discusses this for 2008–12. The live-run table and the run output do not.
* **Fix:** when any year of the migration window carries a `b` flag in `demo_pjan` (t or t+1),
  either (a) refuse `average_residual` unless the window is explicit and the break is acknowledged,
  or (b) exclude break years by default and list them in `data_notes`. Add a typed run warning
  `migration_residual_contains_break`. Consider a median, not a mean. Re-state the POL/DEU rows in
  `v2_pop.md`.

**H2 — "tous les pays sauf la Chine" becomes a valid draft that includes China.** *(NL, verified
live with gpt-4.1-2025-04-14)*
* The status is `draft_ready` with `countries: ["ALL"]`.
* The exclusion survives only as a `geography.not_covered` "unsupported" item. Its LLM-written text
  says *"La plateforme traitera la demande et signalera l'exclusion manuellement si nécessaire"*,
  which is false.
* That text is pasted into the server "restatement", which shows "pays ALL". This contradicts
  NLC decision 9 ("the model cannot misdescribe its own draft").
* The same happens with a synthetic output.
* **Fix:** any `geography.*` gap, or the lexical guards `sauf|hors|à l'exception|except|excluding|
  other than`, should force `needs_input` (no `draft_spec`). Support `exclude_countries`, or
  expand `ALL` to an explicit list. Render LLM-authored gap text as a quoted, labelled LLM note,
  never inside the server restatement.

**H3 — "Oil price +25 %" is drafted as a different shock, with no warning.** *(NL × IO, verified
live and on the recorded fixture)*
* Live: "hausse de 25 % du prix du pétrole … en France" → `economic.io_price`, a single shock
  `{origin: FRA, industry: C19, +25}`, `draft_ready`. A world oil-price change becomes a +25 %
  change in French refined-product producer prices. Imported crude and refined products stay
  unshocked.
* Recorded fixture: "crude petroleum and natural gas from Norway +30 %" → `{NOR, B, +30}`,
  `draft_ready`. B is all of mining and quarrying.
* The prompt view lists the industry choices as bare codes `B / C19 / D35`. The reviewed
  descriptions in `io_price.SHOCKABLE_INDUSTRIES` are never shown to the model or the user. The
  restatement prints "industry = B".
* **Fix:** put the industry descriptions in the catalogue view and in the server restatement. Add a
  server warning `industry_broader_than_request` whenever B is shocked, and a check that "oil/
  pétrole/brut/crude" text without stated origins yields `needs_input` for the origin set. Do not
  default to the user's own country.

**H4 — The pension "upper-bound proxy" is not an upper bound in the projections.** *(PEN,
reasoned; not measurable with five-year LFS groups)*
* ρ = 1 − activity rate of the **five-year group** in the base year is applied to the affected
  single ages.
* In the 2010 back-test the affected ages are 60–61. Their inactivity is below the 60–64 group mean,
  so the proxy overstates there.
* For the 2023/2026 reform, base year 2024, the affected ages are 62–64. Under the 2024 regime they
  are mostly retired, so group-mean ρ can **understate** there.
* ρ also omits unemployed people who would claim, and includes long-career, disability and
  homemaker inactivity.
* The outputs and `v2_pen.md` describe the recipient and expenditure numbers as
  "upper-bound-leaning". That direction is not established.
* **Fix:** relabel as a "proxy of unknown direction". Report ρ for the actual affected ages where
  any source exists, or add a bounding variant (ρ_low = share retired at the affected ages from
  national administrative data). Remove "upper bound" from labels until shown.

## Medium

**M1 — POP back-test uses only the weakest benchmark.** *(verified)*
* Only no-change is reported. A constant-growth extrapolation of the 2008→2013 total is equally
  information-free (`bench.py`). It has median / mean total APE **2.53 / 4.36** against the model's
  2.67 / 5.01.
* It **beats the cohort model in 20 of 31 countries.**
* The cohort model adds age structure, but the headline comparison is on totals.
* **Fix:** add the constant-growth and linear-trend benchmarks to the back-test and the doc, and
  state the result.

**M2 — Claim-year rules re-test eligibility every year.** *(PEN, verified; contradicts the cited
text)*
* For NLD and ESP, eligibility = age ≥ threshold of the *observation* year.
* The archived AOW text says: *"Op pensioengerechtigden die in een bepaald kalenderjaar de
  pensioengerechtigde leeftijd hebben bereikt zijn de pensioengerechtigde leeftijd … in de
  kalenderjaren daarna niet van toepassing."* (Later years' ages do not apply to people who have
  already reached pension age.)
* `flip.py` finds 22 NL birth months that become ineligible again in January, e.g. born 1950-08 in
  2016-01.
* **Fix:** for `claim_year` rules, a person is eligible from the first month t with
  age(t) ≥ threshold(year(t)), and stays eligible. Add a monotonicity property test.

**M3 — Shifts beyond age 69 are silently truncated.** *(PEN, verified)*
* The schema allows `shift_months` up to ±60, but the model covers ages 55–69 only.
* DEU law_in_force +60 months from 1964: 2 of the 5 affected cohort-years (ages 70–71) are
  dropped without notice. A negative shift below 55 is likewise uncounted.
* **Fix:** refuse (`CapabilityGap`) any path whose eligibility age leaves [55, 70), or extend AGES.

**M4 — FRA is labelled "applicable" despite the same overstatement as DEU.** *(PEN, verified in
code and docs)*
* DEU is `qualified` for the 63/35-year route. Its central −1.89 M recipients in 2030 is published
  as a central value.
* FRA is `applicable` although carrières longues, disability and inaptitude routes are likewise
  unmodelled (`retirement_age.py:42`). These routes are counted in ρ·A.
* **Fix:** mark FRA `qualified` with the same caveat. For DEU, refuse or publish under an explicit
  "upper bound (alternative routes not modelled)" output name.

**M5 — Pre-registration of the PEN back-test cannot be verified.** *(verified from git)*
* The protocol, grid, central point and results first appear together in WIP snapshot commits.
* The "pre-registered" central point (Staubli & Zweimüller) is also the best-fitting grid point.
* **Fix:** say "not pre-registered (protocol and results committed together)", or commit a
  protocol digest before any future test.

**M6 — A retirement-age level cannot be expressed, and the LLM turns a level into a shift.** *(NL ×
PEN, verified live)*
* "si l'âge de départ passe à 65 ans" → `retirement_age_shift`, `shift_months: 36`. The model read
  65 as +3 years from 62.
* The draft is `draft_invalid` only because `reference_path` and `first_birth_year` are missing.
* With `law_in_force_2026` the shift would give 67 for cohorts born 1969 or later.
* The intervention schema has no "set level" kind.
* **Fix:** add a `set_minimum_age` (level, with cohort start) intervention. Refuse a shift when the
  quoted text states an age level ("à NN ans", "to NN").

**M7 — `docs/limitations.md` has no V2 section.** *(verified)*
* Nothing on residual-migration breaks, constant mortality, basic-price basket vs CPI, B ≠ crude,
  the ρ proxy, unsourced countries, or valid-but-different NL drafts.
* It still says "LLM use: disabled by default (budget 0)", while `llm.scenario_drafts` now allows
  200 requests per day.
* **Fix:** add the V2 limits (owner DOC2).

**M8 — Germany's back-test and projection base period has one year with a large residual.**
*(POP/PEN, verified)*
* The PEN DEU run is forced to a one-year base period (2023) by gaps in `demo_fasec`.
* Its average-residual migration is then the single 2023 value, +675 k per year, held constant to
  2040, with no warning that n = 1.
* **Fix:** warn when the migration window has fewer than 3 years. Default PEN to EUROPOP migration
  or zero, with the grid, for such countries.

**M9 — The CPI reading of the IO basket ignores product taxes.** *(IO, verified)*
* FR household product taxes less subsidies = 180 bn EUR, **13.2 %** of the basic-price basket
  (1,364 bn).
* With per-unit taxes fixed, the all-energy FRA result falls from 0.93 % to 0.82 %. The 0.93 % is
  therefore an upper side for a purchasers'-price reading.
* The docstring phrase "taxes on products, margins already embedded in trade industries" suggests
  that taxes are inside the basket. In a basic-price table they are not; only margins are.
* **Fix:** reword the docstring and `v2_io.md` §4. Optionally report the tax-inclusive denominator
  variant as a labelled alternative.

## Low

* **L1 — EA12 aggregates not reproducible from the doc.** *(IO, verified)* The doc's 0.95 % basket
  and 0.93 % producer figures do not match my pooled basket (0.937), unweighted mean (0.896) or
  output-weighted producer price (0.917). FRA reproduces exactly. **Fix:** document the
  aggregation.
* **L2 — Capability evidence mentions ICIO.** `economic.io_price` lists "FIGARO/ICIO tables", but
  ICIO is not implemented. **Fix:** say FIGARO only.
* **L3 — Back-test open age comes from the target year.** *(POP)* `backtest()` chooses ω with the
  2023 target table (`extra_open_tables`). That is structural information from after the
  jump-off. It is harmless and needed for scoring, but should be documented, and the claim
  "nothing after the jump-off is used" qualified.
* **L4 — Revised data vintage.** *(POP)* The 2013 jump-off population and the 2008–12 residuals come
  from the 2026 vintage. They may embed later intercensal back-revisions (reasoned). This is
  disclosed as `revised_data_retrospective`. **Fix:** add a sentence that this can shrink errors.
* **L5 — Migrants and births.** *(POP)* Migrants are "not exposed in their arrival year", yet they
  are in F(t+1) and so in the birth exposure of that year. This is a small inconsistency. **Fix:**
  state it, or use pre-migration F(t+1).
* **L6 — Pension start month.** *(PEN)* Eligibility counts from the month the age is reached. In
  FRA and DEU the pension starts the month after: a timing bias of about one month that mostly
  cancels in scenario differences. **Fix:** note it, or shift by one month.
* **L7 — EUROPOP age convention unverified.** *(POP)* Adding EUROPOP net migration at age x to
  age x on the following 1 January is an assumption about proj_23nanmig's age convention that I
  did not verify. It affects only the EUROPOP grid variants. **Fix:** check against Eurostat
  methodology.

## Answers to the brief's specific questions (short)

* **q/m conversion, open age:** correct and standard. Preston: a₀ Coale–Demeny,
  L_ω = l_ω/m_ω, S_open = L_ω/(L_{ω−1}+L_ω). The open group is never split.
* **Births by sex ratio:** fine.
* **Conservation:** exact by construction (Decimal with Inexact trapped, asserted every step).
* **Post-2013 data in the back-test:** no event data. Two qualifications: the open-age structure
  of the target (L3) and the revised vintage (L4).
* **EUROPOP framing:** honest as a plausibility check. The issue is the L1 default (H1).
* **IO equation, transpose, θ, exogenous shocked industries, imports, units:** correct. θ compounds
  per round (θ = 0.5 gives 30 % of the θ = 1 effect, not 50 %). This is stated as an assumption.
* **ECB 2.6× gap:** the direct-effect part is confirmed. The rest is plausible and labelled "not
  demonstrated".
* **PEN two-errors-cancel caveat:** present and prominent. Good practice.
* **German overstatement:** flagged, but still published as a central value (M4).
