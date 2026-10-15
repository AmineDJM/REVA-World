# V2 literature parameters (agent LIT)

Date: 2026-10-15. Status: `maintainer_review_pending`. No human has reviewed these extractions.
Registry files: `registry/assumptions/retirement_employment_response.yaml`,
`registry/assumptions/io_energy_passthrough_benchmarks.yaml`, schema `registry/assumptions/schema.json`,
test `tests/unit/test_assumptions_registry.py` (validates the schema, the quote/locator/digest rules,
and recomputes every derived ratio).

Method. DOIs and bibliographic data were checked against the Crossref works API. Numbers were read
only in texts fetched in this session. The SHA-256 of the bytes received is recorded; the texts
themselves are not archived. Where the cited article was not accessible, a number is recorded only
if a later paper tabulates it with a primary locator (`evidence: secondary_compilation`); otherwise
the entry is `metadata_only` with no number. No number in this document or the registry comes from
memory.

## 1. Retirement age and employment

### Entries and what was verified

| Entry id | Country, reform | Read in | Employment effect | Pension/retirement effect | Derived share s |
|---|---|---|---|---|---|
| `staubli_zweimuller_2013_aut` | AUT, ERA 60→62 (men), 55→58.25 (women) | author manuscript (PMC), Table 3 col. 2/6 | men +9.75 pp (SE 0.30); women +11.00 pp (SE 0.34) | men −24.85 pp; women −24.63 pp | men 0.392, women 0.447 |
| `geyer_welteke_2017_deu_wp` | DEU, women's ERA 60→63 | IZA DP 10681, Table 1 | +0.144 (SE 0.0271), women 60–61 | −0.249 | 0.578 (authors: "About 58%") |
| `geyer_welteke_2021_deu` (JHR) | same | compilation (IZA DP 14150, Table B.1) | +13.5 pp | −27.6 pp | 0.489 |
| `cribb_emmerson_tetlow_2014_gbr_wp` | GBR, women's SPA 60→62 | IFS WP W14/19, abstract and Table 3.1 | +5.9 pp (SE 1.5), women 60–61 | not reported | none |
| `cribb_emmerson_tetlow_2016_gbr` (Labour Econ.) | same | compilation, Table B.1 | +6.3 pp | −11.5 pp (retired) | 0.548 |
| `atav_jongen_rabate_2021_nld_wp` | NLD, SRA 65y6m→65y9m (cutoff 4) | IZA DP 14150, Table 4 | +18.943 pp (SE 0.446) | −58.945 pp | 0.321 (authors: "32%") |
| `rabate_rochut_2020_fra` | FRA, 2010 reform, first step 60→61 | compilation, Table B.1 | +20.9 pp | −47.8 pp | 0.437 |
| `ardito_2021_ita` | ITA, NRA 60→65, male private-sector employees | published article (CC BY), Table 4 | +0.087 (staggered DD: 0.090, SE 0.008) | −0.440 (−0.552) | 0.198 (0.163) |
| `atalay_barrett_2015_aus` | AUS, women's pension age 60→65 | compilation, Table B.1 | +7.7 pp | not tabulated | none |
| `manoli_weber_2016_aut` | AUT, ERA (2000 kink), short contribution years | NBER WP 22561, Table 3 | job-exit age +0.390 (women), +0.358 (men) years per ERA year | claim age +0.540 / +0.539 | exit/claim 0.722 / 0.664 (years-based, cross-check only) |
| `mastrobuoni_2009_usa` | USA, NRA +2 months per cohort | 2006 working paper, abstract | mean retirement age rose "about half as much as the increase in the NRA" | — | cross-check only |
| `rabate_jongen_atav_2024_nld` (AEJ:EP) | NLD | metadata and abstract only | number not verified | — | — |
| `geyer_haan_hammerschmid_peters_2020_deu` | DEU | metadata only | number not verified | — | — |

The numbers marked "compilation" were read in Atav, Jongen and Rabaté (2021), IZA DP 14150, Appendix
Table B.1, whose Table C.1 gives each primary locator. One compilation author (Rabaté) also wrote the
French study. The French primary text was not readable (Cambridge paywall; the HAL copy
halshs-01622346 sits behind a bot challenge), so its standard errors are not verified.

### Why a share, not a percentage-point effect

The percentage-point employment effects range from 5.9 to 20.9 pp across these studies.
Atav, Jongen and Rabaté (IZA DP 14150, Section 5, pdf p.27–28) show that the effect "increases almost
linearly with the magnitude of the pre-reform bunching at the retirement age". The pp effect
therefore depends on how many people would have claimed at the old age, which is country- and
cohort-specific. The quantity that transfers more plausibly is

    s = employment effect / (− effect on pension receipt or retirement)

meaning: of the people kept out of pension receipt by the higher age, the share employed instead.
The rest goes to unemployment, disability or inactivity. PEN's model already has this structure
(`src/reva/models/social/assumptions.py`): the employment change of a fully affected cell is
`s × rho`, where `rho` is the pension-receipt drop. For France, `rho` should come from French data
(the share of each cohort-age cell that would have claimed at the old legal age), not from a study
abroad.

The verified values of s (main estimator, one per study and population; DEU is listed in both
versions) are:
0.198 (ITA, men) · 0.321 (NLD) · 0.392 (AUT, men) · 0.437 (FRA) · 0.447 (AUT, women) ·
0.489 (DEU, women, JHR via the compilation) · 0.548 (GBR, women) · 0.578 (DEU, women, WP).

Definitions differ: the denominator is pension receipt (AUT, DEU, ITA), "retired" status (GBR, NLD),
or the compilation's retirement rate (FRA). The populations also differ (single sex, eligibility
subgroups, ages 60–65). These values are not one estimand measured eight times.

### Proposed sensitivity grid for `social.retirement_age@1.0.0`

All points are stated scenario assumptions with citations. They are a sensitivity range, not a
probability distribution, and carry no confidence level. The grid uses registry ids, so PEN can
select points by `id`.

| Grid point | s (by sex) | Registry entry | Applies to | Why this point |
|---|---|---|---|---|
| reference | 0 | (PEN's `reference_no_employment_response`) | — | Mechanical bound: nobody stays employed |
| low | 0.198 (men; applied to women as a transfer) | `ardito_2021_ita` | Italian male private-sector employees, 1992 reform, cohorts 1934–36 | Lowest verified share, primary-read |
| central (back-test of the 2010 reform) | 0.392 men / 0.447 women | `staubli_zweimuller_2013_aut` | Austrian men 57–64 and women 52–59, 1997–2010 | Primary-read, sex-specific, with standard errors. It is also closest to the median of the non-French values (0.42) |
| central (projection of the 2023 reform only) | 0.437 (pooled) | `rabate_rochut_2020_fra` | French workers at 60 in the first cohorts of the 2010 reform | Only French estimate, read in the compilation |
| high | 0.578 (women; applied to men as a transfer) | `geyer_welteke_2017_deu_wp` | German women 60–61 eligible for the abolished women's pension | Highest verified share, primary-read. The published JHR version gives 0.489 via the compilation, so run that point too if the grid allows a fifth point |

Back-test rule (no leakage). PEN back-tests on the 2010 French reform with Eurostat 2010→2019
data. `rabate_rochut_2020_fra` is estimated on that reform with French administrative data. Using
it to parameterise the back-test would let the test data's answer into the model, so the back-test
grid must exclude it. It may still be reported alongside the back-test as an external benchmark,
labelled as such. Any parameter chosen after looking at back-test errors is tuning on the test
period, which the roadmap forbids.

Cross-checks (not grid points). Manoli and Weber find that job-exit age rises by 0.36–0.39 years per
year of ERA (exit/claim 0.66–0.72). Mastrobuoni finds that the US mean retirement age rose by about
half of the NRA increase. If the model's implied average exit-age shift per year of legal-age
increase falls far outside about 0.3–0.7 years, report it as a warning. These are years-based
measures, so they are not directly comparable with s.

### Transfer to France: an assumption, not a finding

Every grid point applies to its own country, period and population. Applying any of them to France,
and in particular to the 2023 reform, is a modelling assumption for these reasons:

* Different margin. The French estimate covers the first step of the 2010 reform (60→61). The 2023
  reform moves the legal age above 62 (62→64 per v2_design.md; the dates and cohorts belong in PEN's
  reviewed rule table `registry/rules/**`, which this document does not restate). Employment rates,
  health and bunching at 62–64 differ from those at 60.
* Different institutions. The share depends on unemployment-insurance, disability and long-career
  routes, and on employer behaviour. The NLD study points to employers' effects. These differ by
  country and changed in France between 2010 and 2023.
* Single-sex and subgroup estimates. AUT gives both sexes; DEU, GBR and AUS are women only; ITA is
  men only. PEN flags `sex_transfer` when a one-sex estimate is applied to both sexes.
* Short-run estimates. Most studies measure the first affected cohorts. Upstream effects (earlier
  behaviour changes) and long-run adaptation are mostly outside them.
* No ex-post estimate of the 2023 reform was verified for this registry.

Output labels must therefore say "conditional projection under stated assumptions", name the grid
point, and state that its `s` comes from another country, period or reform. That holds for the
French point too, because it comes from the 2010 reform.

## 2. Energy price pass-through benchmarks for the IO model

| Entry id | Shock | Data | First-round result | Derived elasticity |
|---|---|---|---|---|
| `ecb_op113_2010_io_energy` | +10% on all energy products (NACE 10, 11, 23, 40) | Eurostat IOT 2005, 12 euro area countries | Consumer prices: EA +0.36% (direct 0.22, indirect 0.14), FR +0.34%. Producer prices: EA +0.39%, FR +0.30% (Tables 10, 12) | 0.036 (EA consumer), 0.034 (FR consumer), 0.039 (EA producer) |
| `cnb_mpr_spring2026_box2_cze` | +100% oil and natural gas | CZSO symmetric IOT; 2024 scaled from the direct share | Czech consumer prices +3.6%, full cost pass-through | 0.036 |
| `bdf_eco_notepad_50_2018_fra` | +EUR 10 per barrel of oil | National-accounts IOT (indirect part only), 2016 imports | HICP +0.4 pp in FR (0.25 direct, econometric; 0.15 indirect, IO) and EA (0.3 + 0.1) | none (no reference price stated) |
| `ecb_mb_dec2014_box3_ea` (not IO) | +10% oil | Eurosystem macro models (2010 study) | HICP +0.4 pp via the energy component, then about +0.2 pp via other components over up to three years | none; cross-check only |

How IO can use these:

* An IO model with full pass-through and fixed coefficients should give consumer-basket effects of
  the same order as the ECB OP 113 table **for the same shock definition**. A crude-only shock that
  does not force refined products, electricity and gas up by the same percentage should normally
  come out *below* the all-energy result (EA about 0.036, FR about 0.034 per 1% energy price, 2005
  structure). A crude-only result clearly above it needs an explanation, for example a higher energy
  cost share in the FIGARO year or a different shock definition.
* The ECB authors note that the static assumptions tend to overstate effects, while leaving out the
  pass-through to non-energy import prices understates them (Annex 2.7). The 2005 table is old, and
  energy shares were exceptionally high in 2022 (CNB box).
* FIGARO is in basic prices. Household energy prices include volume-based excise duties, which damp
  percentage pass-through (BdF note). An IO consumer-basket effect is therefore not an HICP
  forecast, and the roadmap's label "first-round cost-push, full pass-through" applies.
* None of these is a calibration target. Use them as warnings in a plausibility test, with a wide
  tolerance and the shock definition stated.

## 3. Not verified / open items for a human reviewer

* Rabaté & Rochut (2020): primary Table 5, and standard errors. Try an institutional copy or the
  HAL PDF from a browser.
* Cribb, Emmerson & Tetlow (2016): the published Labour Economics article is CC BY according to
  Crossref, but ScienceDirect returned 403 to this client. Its Table 4/5 should be checked against
  the compilation's +6.3 / −11.5.
* Geyer & Welteke (2021, JHR) and Atalay & Barrett (2015): paywalled; numbers come from the
  compilation only.
* Rabaté, Jongen & Atav (2024, AEJ:EP): the published numbers may differ from the IZA working paper.
* No IO estimate built specifically on FIGARO for a crude-only shock was found and verified. The
  Central Bank of Ireland's FIGARO-based monitoring note was located but not read for numbers.
* Requested change outside LIT's files: none required. `src/reva/models/social/assumptions.py` (PEN)
  already reads `retirement_employment_response.yaml` in this format. Recommendation to PEN: exclude
  `rabate_rochut_2020_fra` from the grid of the 2010 back-test (see above).
