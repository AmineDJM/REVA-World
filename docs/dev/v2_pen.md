# V2 steps 4 and 5 (retirement age, households) - agent PEN

Status: implemented, experimental (`review_status: maintainer_review_pending`); nothing here
records a human review. Everything is a **conditional projection under stated assumptions,
not a forecast**. Back-test: [v2_pen_backtest.md](v2_pen_backtest.md) - read its first section.

## What was built

| Piece | Where |
|---|---|
| Built-in reviewed connector `eurostat.labour_pensions@1.0.0` | `src/reva/adapters/connectors/eurostat_labour.py` (registered in `registry.py`) |
| Concepts (8) and exact mappings/eligibility | `registry/concepts/labour_social.yaml`, `registry/mappings/eurostat_labour.yaml` |
| Dataset inputs (per table x country, Parquet via ENG's `register_dataset`) | `post_publish_datasets` in the connector |
| Retirement-age rule tables + archived legal sources | `registry/rules/retirement_age/{FRA,DEU,ESP,NLD}.yaml`, `capability_gaps.yaml`, `registry/rules/sources/` |
| Rule loader + exact eligibility arithmetic | `src/reva/models/social/rules.py` |
| Literature grid loader (LIT registry) | `src/reva/models/social/assumptions.py` |
| Model plugin `social.retirement_age@1.0.0` | `src/reva/models/social/retirement_age.py`, `registry/models/methods.yaml` (candidate) |
| 2010 back-test | `src/reva/models/social/backtest.py`, `docs/dev/v2_pen_backtest.{md,json}` |
| Household extension (capability `population.household_margins`) | `src/reva/population/households.py` |
| Tests | `tests/unit/social/` (82 offline tests), `tests/integration/test_labour_pensions_retirement_pipeline.py` (DB, fixtures, job path), `tests/integration/test_labour_pensions_live.py` (`live`) |

## Data (exact tables)

One JSON-stat request per table for FR, DE, IT, ES, NL, BE, SE, PL (8 requests per run, budget
source `eurostat.dissemination`, 100/day; `budgets.yaml` did not need a re-pin):

* `lfsa_ergan` employment rate, `lfsa_argan` activity rate, `lfsa_egan` employed persons (thousands):
  sex F/M/T x age `Y55-59`, `Y60-64`, `Y65-69`, citizenship TOTAL, 2000-2025. **No Eurostat LFS
  table offers single years or anything finer than five-year groups for 55-69** (catalogue checked
  2026-10-15), so the model works with five-year groups for rates and single ages for eligibility
  and population.
* `spr_pns_ben` pension beneficiaries, `spdepb=OLD_TOT` (old-age + anticipated + partial), all
  schemes, means-testing TOTAL, sex F/M/T, 2006-2024 (no age dimension exists).
* `spr_exp_pens` pension expenditure, `spdepb` TOTAL/OLD/AOLD/PART, MIO_EUR, 2000-2024.
* `lfst_hhnhtych` private households by composition x number of children (agechild TOTAL),
  thousands, 2006-2025; `ilc_lvph03` household size distribution and `ilc_lvph01` average size
  (EU-SILC) as held-out checks.

Values stay verbatim in the provider scale (`scale_factor` 1000 / 1000000), flags verbatim, missing
cells typed Missing. Subjects are named by ISO3 (`iso3166_alpha3` aliases, same convention as
`eurostat.demography`); Eurostat geo codes are mapped by a reviewed table in the connector.
Raw bytes are archived by the normal acquisition path. Live ingest into a throwaway DB on
2026-10-15: 8/8 tables published, 8 metered requests (235,726 bytes), datasets registered and
pinned by a live release (`test_labour_pensions_live.py`).

## Rule tables

Each row cites a source file archived under `registry/rules/sources/` and quotes a verbatim
locator; the loader refuses a row whose locator is not found in the archived text
(`test_every_row_is_cited_and_its_locator_is_in_the_archived_source`). All tables carry
`review_status: maintainer_review_pending` and "transcribed from law text by an agent;
maintainer review pending".

* **France.** Légifrance refused automated retrieval (HTTP 403 bot challenge), so the official
  text was read from the codes.droit.org consolidated *Code de la sécurité sociale* (edition
  2026-10-14, generated from the Légifrance LEGI open data; 37 MB PDF hashed, verbatim excerpt
  of L161-17-2, L351-8 and D161-2-1-9 committed) and cross-checked against service-public.fr
  F14043 (DILA, "vérifié le 14 août 2026", archived). Paths: `pre_2010_counterfactual` (60),
  `law_2010_as_amended_2011` (60 -> 62 by cohorts 1951 H2-1955 in 4-5-month steps as accelerated
  by LFSS 2012; 62 after), `law_2023_as_enacted` (Loi 2023-270 / décret 2023-436: 62 -> 64,
  cohorts Sept 1961-1968), `law_in_force_2026` (**L161-17-2 as amended by Loi n° 2025-1403, LFSS
  2026: the 2023 increase is suspended at 62 years 9 months for cohorts born 1963 - March 1965,
  then 3-month steps to 64 for cohorts from 1969**). The roadmap's "1961-1968" schedule is the
  as-enacted 2023 law; the law in force differs and both are recorded. Full-rate ages 65 -> 67
  (1951 H2-1955) are not in the consolidated code any more (abrogated wording is excluded); they
  come from a **secondary source**, the IRCEC statutory fund's 2022 table (archived), marked
  `secondary_table`. The model does not use full-rate ages (no verified effect size).
* **Germany.** SGB VI § 35, § 36, § 235, § 236 from gesetze-im-internet.de (archived): Regelaltersgrenze
  65 -> 67 by cohort 1947-1964; early claim from 63 with 35 years recorded separately.
* **Spain.** LGSS art. 205 and DT 7ª from the BOE consolidated text (excerpt archived, full hash
  recorded): age by **claim year** and **contribution track** (short track 65+1m in 2013 -> 67
  from 2027; long track 65).
* **Netherlands.** AOW art. 7a from wetten.overheid.nl (excerpt archived): AOW age by claim year
  65 -> 67 (2013-2025). Ages for 2026+ are set by yearly regulation that was not archived: the
  table stops at 2025 and the model refuses later years (`retirement_rule_coverage_gap`), never
  extrapolates.
* **Capability gaps** (`capability_gaps.yaml`): ITA (Normattiva text not retrievable; life-expectancy
  decrees not archived), BEL (eJustice page not retrievable), SWE (SFB 56:3 archived: minimum age =
  riktålder − 3, but the yearly riktålder values are set outside the archived text), POL (ISAP 403).
  OECD Pensions at a Glance / MISSOC tables were not fetched.

## Model `social.retirement_age@1.0.0`

Annual, reference (base) year + 1 .. at most 2040; single ages 55-69 by recorded sex.

1. **Eligibility.** Exact share of person-time below the eligibility age per single age and year
   (12 x 12 grid of observation month x month of exact age; births uniform within month cohorts;
   eligible from the month the age is reached). Birth-cohort and claim-year rules both supported.
   **Claim-year ages are attained once** (SFX1, review M2): a person is eligible from the first month
   whose age reaches that calendar year's age and stays eligible - AOW art. 7a(1) says so verbatim
   ("Op pensioengerechtigden die in een bepaald kalenderjaar de pensioengerechtigde leeftijd hebben
   bereikt zijn de pensioengerechtigde leeftijd ... in de kalenderjaren daarna niet van toepassing";
   cited in `NLD.yaml` `claim_year_semantics`, locator checked against the archived text); for ESP the
   archived excerpt has no such sentence and the same rule is a stated **model assumption**
   (`ESP.yaml`). The previous code re-tested every year and revoked eligibility for 22 Dutch birth
   months (e.g. born August 1950 in January 2016). Effect, NLD `law_in_force` vs 65, base 2015:
   recipients change 2016 -86,345 -> -82,748, 2019 -235,157 -> -227,883 (-3 %); 2025 unchanged.
   **Age range:** eligibility is computed at single ages 55-69 only; a request whose scenario,
   reference or observed eligibility differs at any age outside 55-69 in a modelled year (e.g. DEU
   +60 months from 1964: ages 70-71) is refused with `capability_gap`
   (`capability = social.retirement_age.age_range`), never silently cut (review M3).
2. **Affected population** `A = N * (I_scenario - I_reference)`; `N` = mean of 1 January
   populations of `t` and `t+1` from **agent POP's** `cohort.project` (constant base-period rates,
   average-residual migration by default; base period = the `base_period_years` before the jump-off;
   residual years spanning a Eurostat series break are excluded by default and a one-year base
   period with average-residual migration is refused unless
   `assumptions.population.allow_single_year_migration: true` - see `v2_pop.md`; the projection's
   typed warnings are repeated in every row's `caveats`).
3. **Pension-receipt drop** `rho(g, s) = 1 - activity_rate(g, s)/100` of the base year - the
   country's own share outside the labour force of the five-year group, applied at each affected
   single age (LIT asked that the drop come from national data, not foreign studies). Pension
   recipients change `-rho * A`. **It is a proxy of unknown direction** (review H4; earlier versions
   of this page called it an upper bound, which is not established): it overstates at ages still
   mostly active (60-61 in the 2010 back-test) but can understate at ages already mostly retired
   under the base-year regime (62-64 for the 2023 reform, base 2024); it counts non-pensioner
   inactivity and omits unemployed claimants. L2 adds two **bounding variants** of the central
   point: rho of the 60-64 group and rho of the 65-69 group applied at every age
   (`L2:<central>;pension_drop=Y60-64` / `=Y65-69`), which enter the sensitivity range.
4. **Employment response** `s * rho * A`, `s` = literature share of the pension-receipt drop
   employed instead (LIT registry, `verified_from_text` only). Central point: own-country estimate
   when exactly one exists (FRA: Rabaté & Rochut 2020, secondary-compilation numbers, no SE;
   NLD: Atav, Jongen & Rabaté 2021), otherwise the spec must name one (`ValidationProblem`).
5. **Levels.** Employment rates by group held at base-year values and shifted relative to the regime
   observed in the base year (`observed_path`); bounds checked on group rates. Pension recipients
   level = base-year ESSPROS beneficiaries x (65+ population ratio) + modelled change (stated).
6. **Expenditure (approximate)** = average old-age pension (OLD+AOLD+PART expenditure / OLD_TOT
   beneficiaries, latest year where all exist; nominal euro of that year, not indexed) x recipients
   change. If a component is missing in ESSPROS the output is **not computed** (missing is never 0).
7. **Old-age dependency ratio** (65+ per 100 aged 15-64, 1 January) from the POP projection.

L1 = central point; L2 = every grid point + a labelled no-response reference point + the two
pension-drop bounding variants, each a variant, plus `L2:sensitivity_range` rows (min/max,
`sensitivity_range` uncertainty, no level). No GDP, unemployment, wage or welfare output exists
(`CapabilityGap`).

**Labels (review M4).** Every country is `qualified`, France included: each rule table states a
`model_lever.qualification` (FRA: long-career early retirement, disability and incapacity routes
are not modelled, so persons using them are counted as affected - the same overstatement as
Germany's 63-with-35-years route; DEU: the central value is an upper side for the affected
population, "alternative routes not modelled"; ESP: short-track age applied to everyone; NLD: rho
proxy). The qualification is repeated in **each value's `basis.notes`** ("… QUALIFIED (DEU): Upper
side …") and in `applicability.qualification`, so no central value is published unqualified.

**Interventions.** `retirement_age_rule_path` (scenario path of the table), `retirement_age_shift`
(shift of a path for cohorts from a birth year) and, since SFX1 (review M6),
**`retirement_age_target`**: `{"kind": "retirement_age_target", "reference_path": …,
"target_age_months": 780, "first_birth_year": 1969, "phase_in_months_per_cohort": 0}` sets the
table's lever age (FRA minimum age, DEU Regelaltersgrenze, NLD AOW age, ESP short-track age) to a
**level** (months of age; 780 = 65 years) for cohorts born in `first_birth_year` or later; earlier
cohorts keep the reference path; an optional phase-in moves each cohort towards the target by at
most k months per birth year. It is exported in the plugin `spec_schema` and in
`contracts/scenario.schema.json` (schema range 660-840 months). A stated age level ("passe à 65
ans") must map to this kind, not to a shift. Tested: a target of 65 from 1969 on FRA
`law_in_force_2026` (64 for those cohorts) gives exactly the results of a +12-month shift.

### Results (fixtures captured 2026-10-15; L2; central point and sensitivity range)

Restated 2026-10-15 by agent SFX1 after the review: the ranges now include the two pension-drop
bounding variants, Germany's population excludes its 2022 census-break residual, and every value is
labelled `qualified`.

France, `law_in_force_2026` vs `law_2010_as_amended_2011` (effect of the 2023 reform as amended),
base 2024, central Rabaté & Rochut; qualified (long-career, disability and incapacity routes not
modelled):

| Year | Employment change 55-69 | Pension recipients change | Expenditure change (approx., EUR 2024 nominal) | Old-age dependency |
|---|---|---|---|---|
| 2025 | +125,000 (0 .. +202,000; was 0 .. +165,000) | -286,000 (-461,000 .. -286,000) | -5.4 bn (-8.8 .. -5.4 bn) | 35.5 |
| 2030 | +292,000 (0 .. +471,000; was 0 .. +386,000) | -669,000 (-1.08 m .. -669,000) | -12.7 bn (-20.5 .. -12.7 bn) | 37.9 |
| 2040 | +388,000 (0 .. +626,000; was 0 .. +513,000) | -888,000 (-1.43 m .. -888,000) | -16.9 bn (-27.3 .. -16.9 bn) | 41.8 |

For France every affected age (62-64) is in the 60-64 group, so the central point equals the
`Y60-64` bounding variant; the `Y65-69` variant (65-69-year-olds are mostly retired) gives a
1.6 times larger drop. Before this revision the recipient and expenditure ranges were a single
point (every grid point shares rho), which hid this dependence.

Germany, `law_in_force` vs `pre_2007_counterfactual` (65 -> 67), base 2023, population base period
2019-2023 (fertility without 2022, whose births by mother's age are incomplete; the 2022 migration
residual excluded: census break flagged on 1 January 2023), central Staubli & Zweimüller
(transferred from AUT). **Qualified - upper side for the affected population: the early claim from
63 with 35 insurance years is not modelled**: employment +790,000 (0 .. +1.08 m; was +794,000) and
recipients -1.88 m (-1.88 m .. -0.78 m; was -1.89 m) in 2030; expenditure approx. -36.2 bn EUR
(-36.2 .. -15.0 bn; 2023 nominal; was -36.4 bn). Here the affected ages 65-66 lie in the 65-69 group,
so the central point equals the `Y65-69` variant and the `Y60-64` variant is 58 % smaller.

Netherlands, AOW `law_in_force` vs `pre_2013_counterfactual`, base 2024, horizon 2025 only (rule
coverage): employment +103,000 (0 .. +185,000), recipients -320,000 (-320,000 .. -134,000);
expenditure not computed (ESSPROS AOLD/PART missing). (2025 is not affected by the claim-year fix:
the AOW age is 67 in both 2024 and 2025.)

Spain: **refused by the data** - applying the short-contribution-track age to the whole population
drives the reference-path employment rate of women 65-69 below 0 (persons with long careers keep
65; REVA has no data on the track shares). This is the honest outcome, not a bug.

Italy, Belgium, Sweden, Poland: `capability_gap` (no sourced rule table).

The recipient and expenditure numbers depend on the rho proxy, whose direction is unknown; the
bounding variants show how much (factor about 1.6 for France, 2.4 for Germany). The back-test shows
the proxy over-attributes employment change to the 2010 reform (affected ages 60-61); that finding
does not carry over to reforms affecting ages already mostly retired.

## Households (`population.household_margins`, V2; P10 `population.households` stays reserved)

`src/reva/population/households.py`: a household recipe from the published joint
composition x number-of-children cells of `lfst_hhnhtych`; structural zeros (households "without
resident children" with 1+ children and vice versa) are exact and a non-zero source value there is
refused; other missing cells stay Missing with the totals gate reporting the remainder; household
size intervals follow from category definitions only (open for 3+ children, "other" and no-response
households - never imputed); weighted representative households with exact rational weights
(largest remainder, min one record per non-zero cell, seed only breaks ties); diagnostics
(ESS, weight concentration, margin errors, support violations, persons-in-households bounds vs the
person recipe total) and a **held-out comparison with EU-SILC** size distribution and average size.
Result for 2024: SILC one-person share lies *outside* the LFS-implied interval in all eight
countries (LFS one-adult households > SILC one-person households, e.g. FR 41.6 % vs 39.3 %), and
the SILC average size lies outside the LFS-implied bound for SWE: reported as source discrepancies,
not corrected. Records carry **no person attributes and no person linkage** (no joint
person x household source is used); member ages, sexes, income etc. are unsupported associations.

## Decisions taken alone (owner asleep)

* Connector template literal `eurostat_jsonstat` (format), own parser: the autonomy template
  only supports `point:MM-DD` temporal support, LFS rates are annual averages.
* ISO3 subjects via `iso3166_alpha3` aliases (no new `eurostat_geo` aliases: they feed autonomy T1
  scope) - same as POP.
* France default central = Rabaté & Rochut for projections (own country, later reform); the back-test
  excludes it (leakage) and uses Staubli & Zweimüller as central (described as pre-registered, which
  git history cannot verify - see `v2_pen_backtest.md`).
* Pension-receipt drop from own-country LFS inactivity (proxy of unknown direction, with two bounding
  variants in L2 since SFX1) instead of foreign studies' values.
* (SFX1) Claim-year eligibility attained once: cited for NLD, model assumption for ESP. Ages outside
  55-69 refused rather than extending the age range (LFS rates exist only for 55-69 in the tables
  used). Every country labelled `qualified`. A `retirement_age_target` level intervention added.
* Back-test uses the as-applied 2010 schedule (LFSS 2012 acceleration); uniform weights within 60-64.
* Phase-2 central for DEU/ESP = Staubli & Zweimüller (the German estimates concern the abolition
  of the women's pension, a different lever).
* `population.households` (P10) is **not** marked implemented: the catalogue rule (and
  `tests/unit/test_capabilities.py`) forbids P8-P12 capabilities becoming implemented, and its
  gates (person-household linkage, household-aware IPU, multiple completed populations) are not
  met. What was built is declared as the V2 capability `population.household_margins`
  (implemented, tests and doc cited), with `population.households.available_instead` pointing to it.
  `social.retirement_age` is implemented (experimental).
* Unregistered unit refs `unit.households`, `unit.persons_per_household` used in two concepts:
  agent K should add them to `reva.ontology.units` (no conversion path uses them today).

## Requests to other owners

* **(SFX1) registry owner of `registry/models/methods.yaml`:** lines ~171 and ~202 still call the
  pension-receipt drop an "upper-bound proxy"; replace with "proxy of unknown direction (bounding
  variants rho 60-64 / 65-69 in L2)".
* **(SFX1) SFX2 / NL drafts:** a stated age level ("passe à 65 ans", "to 65") maps to
  `{"kind": "retirement_age_target", "reference_path": <path>, "target_age_months": 12 * years +
  months, "first_birth_year": <first cohort>}`, never to `retirement_age_shift`.
* **(SFX1) DOC2 / `docs/limitations.md`:** V2 limits now include: residual migration excludes
  flagged series breaks only (unflagged revisions stay), the rho proxy has unknown direction, every
  retirement result is `qualified`.
* **lead / D (runner, jobs, CLI):** dispatch a connector's own post-publish hook:
  `post_publish = getattr(connector, "post_publish", None) or post_publish_for(spec.template)` in
  `operations/jobs.py` (handle_ingest) and `cli/main.py` (ingest). Without it, scheduled/CLI ingests
  publish observations but do not register the dataset inputs (tests pass the hook explicitly).
  Same need for `eurostat.demography`.
* **lead:** add a schedule `("ingest:eurostat.labour_pensions", "ingest", {"connector": "eurostat.labour_pensions"}, 30 * 86400)` to `cli/seed.py` `DEFAULT_SCHEDULES` if periodic ingest is wanted.
* **K (units):** register `unit.households` (count of households) and `unit.persons_per_household`.
* **POP:** `demo_fasec` DE has missing 2024 births by newborn sex and 2022 `Y10-14`; `cohort.project`
  refuses such base periods (correct), which forces a one-year base period for DEU.
* **NLC:** `tests/unit/test_nl_drafts.py::test_retirement_age_before_the_model_exists_is_a_capability_gap`
  assumes the retirement model does not exist; it now does.
* **LIT:** Rabaté & Rochut working paper (HAL halshs-01622346, PSE WP 2017-46, sha256
  `5797dfc8...ecd3c`) Table 6, primary text: employment +0.193 (SE 0.008), retirement −0.478
  (SE 0.004) at age 60 (Deaton specification) -> share 0.404; the registry's compilation values
  (+20.9 / −47.8 -> 0.437) differ slightly. Consider adding the primary working-paper entry.

## How to run

```
uv run pytest tests/unit/social -q
uv run pytest tests/integration/test_labour_pensions_retirement_pipeline.py -q      # DB, offline fixtures
REVA_PEN_LIVE_OUT=/tmp/x uv run pytest -m live tests/integration/test_labour_pensions_live.py   # live
```
