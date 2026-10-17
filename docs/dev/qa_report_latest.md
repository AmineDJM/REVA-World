# REVA data-quality report (`reva qa run`)

Generated 2026-10-17T21:09:12+00:00 · as of 2026-10-17 · origin `live` · mode `db` · config `registry/qa/checks.yaml` sha256 `3e80e5fb961d…`

Read-only: every finding is reported, nothing is fixed, retracted or rewritten. A cross-source divergence is a difference between two publishers to review, never a reason to blend or pick a value.

Summary: fail 2, warn 16, info 5, pass 15

Context: atlas_tables 331, atlas_rows 108175861, kg_statements 4975018, causal_claims 61680, seconds 326.9

| domain | check | status | issues | checked |
|---|---|---|---:|---:|
| cross_source | `cross_source.catalog` catalog concepts listing several sources | **warn** | 4,086 | 208,498 |
| cross_source | `cross_source.explicit` declared source pairs (registry/qa/checks.yaml) | **warn** | 5,217 | 29,390 |
| cross_source | `cross_source.headline` headline concepts (source_order series of one quantity) | **warn** | 12,876 | 147,797 |
| units | `units.bounded_share` shares / bounded indices outside their declared range | **warn** | 531 | 7,788,151 |
| units | `units.negative_count` negative values in count-like units | **warn** | 942 | 15,579,743 |
| units | `units.per_capita_identity` per-capita × population vs published total | **warn** | 217 | 29,965 |
| time | `time.forecast_as_observation` non-projection cells starting at/after their availability date | **pass** | 0 | 507,569 |
| time | `time.future_observation` non-projection cells starting after today | **fail** | 15,329 | 96,754,615 |
| time | `time.kg_future_statement` KG statements dated after today | **pass** | 0 | 4,975,018 |
| time | `time.mixed_precision_indicator` indicators mixing period lengths (day/month/quarter/year…) | **info** | 167 | 1,101,007 |
| time | `time.period_not_ended` non-projection cells for a period that has not ended | **info** | 37,710 | 96,754,615 |
| time | `time.period_open_at_publication` non-projection cells whose period was still open when published | **info** | 37 | 507,569 |
| time | `time.precision_mismatch` period length differs from the table's declared frequency | **pass** | 0 | 5,904,549 |
| time | `time.projection_table_unflagged` cells of projection tables without a projection flag | **pass** | 0 | 4,090,991 |
| time | `time.projection_word_unflagged` non-projection cells whose estimate kind / flag says forecast or projection | **fail** | 10,804 | 25,071,385 |
| graph | `graph.cycles.is_a` cycles in is_a (child → parent edges) | **warn** | 1 | 162,380 |
| graph | `graph.cycles.part_of` cycles in part_of (child → parent edges) | **pass** | 0 | 119,874 |
| graph | `graph.dangling_object` edges whose object is not a KG entity (by predicate) | **warn** | 121,597 | 2,201,685 |
| graph | `graph.dangling_subject` statements whose subject is not a KG entity | **warn** | 75,217 | 4,975,018 |
| graph | `graph.located_in_no_country` located_in chains that never reach a country | **warn** | 921 | 165,878 |
| licence | `licence.atlas_restricted` atlas tables under restricted terms (must be named with values) | **info** | 16,631,319 | 108,175,861 |
| licence | `licence.atlas_review_pending` atlas tables whose licence review is pending | **info** | 30 | 331 |
| licence | `licence.atlas_tables` atlas tables without a licence | **pass** | 0 | 331 |
| licence | `licence.causal_raw` causal rows whose raw object carries no licence | **pass** | 0 | 61,680 |
| licence | `licence.effects_raw` effects rows whose raw object carries no licence | **pass** | 0 | 301 |
| licence | `licence.kg_raw` kg rows whose raw object carries no licence | **pass** | 0 | 4,975,018 |
| provenance | `provenance.atlas_cell_raw` atlas cells whose raw_sha256 is not a raw object of the table | **pass** | 0 | 108,175,861 |
| provenance | `provenance.causal_no_raw` causal rows without a raw object (null or not archived) | **pass** | 0 | 61,680 |
| provenance | `provenance.effects_no_raw` effects rows without a raw object (null or not archived) | **warn** | 5 | 301 |
| provenance | `provenance.kg_no_raw` kg rows without a raw object (null or not archived) | **pass** | 0 | 4,975,018 |
| provenance | `provenance.objects` archived objects missing or corrupt (store API, decoded) | **pass** | 0 | 13,799 |
| provenance | `provenance.referenced_not_archived` raw sha256 cited by atlas cells / tables without an archived location | **pass** | 0 | 4,486 |
| causal | `causal.disruption_axioms_unqualified` 'disruption / dysfunction / alteration of X' axioms stored as 'X causes' without the source relation | **warn** | 823 | 61,680 |
| causal | `causal.missing_endpoint` claim endpoints (cause / effect) that are not KG entities | **warn** | 2,361 | 123,360 |
| causal | `causal.self_loop` claims whose cause is their effect | **warn** | 1 | 61,680 |
| causal | `causal.ungraded` current causal claims without a grade for their row | **warn** | 11,435 | 61,680 |
| causal | `causal.wording_leak_labels` non-causal family / link-type labels worded causally | **pass** | 0 |  |
| causal | `causal.wording_leak_predicates` causal-worded KG predicates read as a non-causal family | **warn** | 1 | 1,047 |

## cross_source

### `cross_source.catalog` — catalog concepts listing several sources (warn, 4,086 issues)

Cell pairs (country, calendar year, non-projection) beyond tolerance; status warn when a pair's diverging share exceeds max_diverging_share. Report only: no value is chosen, blended or fixed.

| concept | a | b | overlap | diverging | share | max_rel | sign_flips | rel_tol | abs_tol | status | top_countries | units |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| history.population_persons | gapminder.fasttrack.pop | hyde.hyde321.popc | 6,878 | 1,404 | 0.2041 | 0.9863 | 0 | 0.15 | 0 | warn | ISR×38, ERI×34, GNQ×33, TON×33, DJI×32 | persons / inhabitants |
| water.stress.total | fao.sdgb.24027-_T\|6121 | wb.wdi_social.ER.H2O.FWST.ZS | 3,953 | 484 | 0.1224 | 0.9999 | 0 | 0.1 | 1 | warn | MLT×23, BLZ×23, MWI×23, ATG×22, YEM×22 | % / % (ratio) |
| politics.parliament.women_lower_chamber_pct | ipu.parline.current_women_percent\|chamber=lower\|at=year_end | vdem.institutions.v2lgfemleg | 9,611 | 801 | 0.0833 | 1 | 0 | 0.05 | 0 | info | TCD×27, GUY×26, ESP×20, BRA×19, NLD×18 | % / % |
| history.life_expectancy_at_birth | clio.infra.Total_life_expectancy-historical | gapminder.fasttrack.lex | 12,756 | 874 | 0.0685 | 0.7786 | 0 | 0.15 | 0 | info | SLE×47, MOZ×40, TLS×36, AFG×35, SOM×35 | years / years |
| politics.voting_age_minimum | ipu.parline.min_age_vote_elect\|chamber=lower\|at=year_end | vdem.institutions.v2elage | 1,026 | 42 | 0.0409 | 0.1667 | 0 | 0.05 | 0 | info | KGZ×10, IRN×10, KWT×6, ARG×4, JPN×4 | years / years |
| food_agriculture.land.forest_share | fao.sdgb.24034\|6121 | wb.wdi_social.AG.LND.FRST.ZS | 1,017 | 26 | 0.0256 | 1 | 0 | 0.05 | 0 | info | NOR×5, EGY×5, GRL×5, OMN×5, ETH×1 | % / % (share) of land area |
| food_agriculture.land.irrigated_share_agri | fao.rl.6611\|7208 | wb.wdi_social.AG.LND.IRIG.AG.ZS | 1,321 | 31 | 0.0235 | 1 | 0 | 0.05 | 0 | info | BWA×10, ISL×7, MNG×5, LTU×4, LSO×2 | % / % (share) of total agricultural land |
| history.population_thousands | maddison.mpd2023.pop | clio.infra.Total_Population-historical | 1,730 | 30 | 0.0173 | 0.8997 | 0 | 0.15 | 0 | info | DMA×7, PER×5, DJI×4, SRB×3, MEX×2 | thousands (mid-year population) / in thousands |
| food_agriculture.land.forest_share | fao.rl.6646\|7209 | wb.wdi_social.AG.LND.FRST.ZS | 7,135 | 105 | 0.0147 | 1 | 0 | 0.05 | 0 | info | GRL×35, OMN×35, EGY×35 | % / % (share) of land area |
| health.immunization_hepb3 | who.gho.WHS4_117 | wb.wdi_social.SH.IMM.HEPB | 4,208 | 46 | 0.0109 | 0.4433 | 0 | 0.1 | 0.5 | info | KAZ×9, COG×6, TJK×5, HND×5, GNB×4 | % / Percentage |
| food_agriculture.employment_agriculture_share | fao.oea.21156\|6121\|sex=1\|src=3043 | wb.wdi_social.SL.AGR.EMPL.ZS | 6,528 | 59 | 0.009 | 0.3172 | 0 | 0.05 | 0 | info | SGP×31, HKG×22, GBR×2, LUX×2, ISR×1 | % / % of total employment |
| health.immunization_dtp3 | who.gho.WHS4_100 | wb.wdi_social.SH.IMM.IDPT | 4,805 | 42 | 0.0087 | 0.449 | 0 | 0.1 | 0.5 | info | KAZ×9, HND×5, TJK×5, GNB×4, ETH×3 | % / Percentage |

… 26 more rows in the JSON report.

Examples:

* `{"concept": "food_agriculture.land.forest_share", "iso3": "GRL", "year": 1992, "a": "fao.rl.6646|7209", "a_value": 0.0, "a_raw": "39b739fbfb694971", "b": "wb.wdi_social.AG.LND.FRST.ZS", "b_value": 0.0006438396254023998, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 1.0}`
* `{"concept": "food_agriculture.land.forest_share", "iso3": "GRL", "year": 2019, "a": "fao.rl.6646|7209", "a_value": 0.0, "a_raw": "39b739fbfb694971", "b": "wb.wdi_social.AG.LND.FRST.ZS", "b_value": 0.0005359970763795834, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 1.0}`
* `{"concept": "food_agriculture.land.forest_share", "iso3": "EGY", "year": 2000, "a": "fao.sdgb.24034|6121", "a_value": 0.0, "a_raw": "ef0358f2cc8cb786", "b": "wb.wdi_social.AG.LND.FRST.ZS", "b_value": 0.003927871816766287, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 1.0}`
* `{"concept": "food_agriculture.land.forest_share", "iso3": "EGY", "year": 2015, "a": "fao.sdgb.24034|6121", "a_value": 0.0, "a_raw": "ef0358f2cc8cb786", "b": "wb.wdi_social.AG.LND.FRST.ZS", "b_value": 0.004098648852277864, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 1.0}`
* `{"concept": "food_agriculture.land.irrigated_share_agri", "iso3": "BWA", "year": 2012, "a": "fao.rl.6611|7208", "a_value": 0.0, "a_raw": "39b739fbfb694971", "b": "wb.wdi_social.AG.LND.IRIG.AG.ZS", "b_value": 0.004249237068799011, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 1.0}`

### `cross_source.explicit` — declared source pairs (registry/qa/checks.yaml) (warn, 5,217 issues)

Cell pairs (country, calendar year, non-projection) beyond tolerance; status warn when a pair's diverging share exceeds max_diverging_share. Report only: no value is chosen, blended or fixed.

| concept | a | b | overlap | diverging | share | max_rel | sign_flips | rel_tol | abs_tol | status | top_countries | units |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| population.weo_vs_wdi | imf.weo.LP | wb.wdi_social.SP.POP.TOTL | 7,695 | 2,024 | 0.263 | 0.55 | 0 | 0.03 | 0 | warn | ETH×46, CYP×46, EGY×46, UGA×45, PAK×45 | persons / Unit |
| unemployment.ilo_vs_weo | ilostat.une_2eap_sex_age_rt_a.UNE_2EAP_SEX_AGE_RT\|sex=SEX_T\|classif1=AGE_YTHADULT_YGE15 | imf.weo.LUR | 3,536 | 813 | 0.2299 | 0.926 | 0 | 0.15 | 1 | warn | PHL×35, JAM×34, PAK×33, PER×33, SGP×30 | % / % of labour force |
| co2.owid_vs_wdi_edgar | owid.co2.co2 | wb.wdi_social.EN.GHG.CO2.MT.CE.AR5 | 10,604 | 2,259 | 0.213 | 0.9988 | 0 | 0.1 | 0.5 | warn | PLW×55, BHS×54, MDA×51, MNG×50, TTO×50 | million tonnes (Mt) / Mt CO2eq |
| unemployment.ilo_vs_eurostat | ilostat.une_2eap_sex_age_rt_a.UNE_2EAP_SEX_AGE_RT\|sex=SEX_T\|classif1=AGE_YTHADULT_YGE15 | eurostat.une_m.une_rt_m\|s_adj=NSA\|age=TOTAL\|unit=PC_ACT\|sex=T | 1,024 | 121 | 0.1182 | 0.3919 | 0 | 0.1 | 0.5 | warn | NLD×27, ROU×17, PRT×12, ISL×10, FIN×10 | % / Percentage of population in the labour force |
| unemployment.ilo_vs_wdi | ilostat.une_2eap_sex_age_rt_a.UNE_2EAP_SEX_AGE_RT\|sex=SEX_T\|classif1=AGE_YTHADULT_YGE15 | wb.wdi_social.SL.UEM.TOTL.ZS | 6,531 | 0 | 0 | 0 | 0 | 0.02 | 0.1 | pass |  | % / % of total labor force |

Examples:

* `{"concept": "co2.owid_vs_wdi_edgar", "iso3": "GRL", "year": 1977, "a": "owid.co2.co2", "a_value": 0.722, "a_raw": "7f78e2b218ce4bb8", "b": "wb.wdi_social.EN.GHG.CO2.MT.CE.AR5", "b_value": 0.0009, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 0.9988}`
* `{"concept": "co2.owid_vs_wdi_edgar", "iso3": "GRL", "year": 1972, "a": "owid.co2.co2", "a_value": 0.553, "a_raw": "7f78e2b218ce4bb8", "b": "wb.wdi_social.EN.GHG.CO2.MT.CE.AR5", "b_value": 0.0008, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 0.9986}`
* `{"concept": "unemployment.ilo_vs_weo", "iso3": "PAK", "year": 2003, "a": "ilostat.une_2eap_sex_age_rt_a.UNE_2EAP_SEX_AGE_RT|sex=SEX_T|classif1=AGE_YTHADULT_YGE15", "a_value": 0.612, "a_raw": "798a48432590eaed", "b": "imf.weo.LUR", "b_value": 8.27, "b_raw": "a5b8f62b26f71d4e", "rel_diff": 0.926}`
* `{"concept": "unemployment.ilo_vs_weo", "iso3": "PAK", "year": 2002, "a": "ilostat.une_2eap_sex_age_rt_a.UNE_2EAP_SEX_AGE_RT|sex=SEX_T|classif1=AGE_YTHADULT_YGE15", "a_value": 0.615, "a_raw": "798a48432590eaed", "b": "imf.weo.LUR", "b_value": 8.27, "b_raw": "a5b8f62b26f71d4e", "rel_diff": 0.9256}`
* `{"concept": "population.weo_vs_wdi", "iso3": "YEM", "year": 2019, "a": "imf.weo.LP", "a_value": 15800133.0, "a_raw": "a5b8f62b26f71d4e", "b": "wb.wdi_social.SP.POP.TOTL", "b_value": 35111408.0, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 0.55}`

### `cross_source.headline` — headline concepts (source_order series of one quantity) (warn, 12,876 issues)

Cell pairs (country, calendar year, non-projection) beyond tolerance; status warn when a pair's diverging share exceeds max_diverging_share. Report only: no value is chosen, blended or fixed.

| concept | a | b | overlap | diverging | share | max_rel | sign_flips | rel_tol | abs_tol | status | top_countries | units |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| reva.economy.gdp_per_capita_usd | imf.weo.NGDPDPC | spc.pocket.GDPCPCUSD | 109 | 85 | 0.7798 | 0.9044 | 0 | 0.05 | 0 | warn | KIR×19, PNG×17, VUT×15, TUV×14, FJI×12 | US dollars per person (current prices) / USD per person |
| reva.finance.government_net_lending_gdp | imf.gfs_soo.GNLB_T\|sector=S13 | imf.weo.GGXCNL_NGDP | 2,206 | 1,150 | 0.5213 | 1.984 | 0 | 0.05 | 0 | warn | GBR×42, SGP×34, ISL×28, MNG×28, ROU×26 | % of GDP / % of GDP |
| reva.economy.gdp_usd | imf.weo.NGDPD | spc.pocket.GDPCUSD | 109 | 42 | 0.3853 | 0.9045 | 0 | 0.05 | 0 | warn | VUT×17, FJI×9, TUV×6, KIR×6, SLB×4 | US dollars (current prices) / thousand USD |
| reva.economy.gdp_per_capita_usd | wb.wdi_econ.NY.GDP.PCAP.CD | imf.weo.NGDPDPC | 7,232 | 2,551 | 0.3527 | 0.9483 | 0 | 0.05 | 0 | warn | KEN×46, GNQ×45, GAB×45, MWI×44, CAF×43 | Current US$ / US dollars per person (current prices) |
| reva.trade.current_account_gdp | imf.weo.BCA_NGDPD | wb.wdi_econ.BN.CAB.XOKA.GD.ZS | 6,757 | 2,011 | 0.2976 | 1.999 | 3 | 0.1 | 0.5 | warn | NPL×38, PAK×38, STP×38, WSM×36, GIN×33 | % of GDP / % |
| reva.economy.gdp_usd | wb.wdi_econ.NY.GDP.MKTP.CD | imf.weo.NGDPD | 7,353 | 2,095 | 0.2849 | 0.9465 | 0 | 0.05 | 0 | warn | GIN×44, CAF×43, UGA×39, ZWE×36, SUR×35 | Current US$ / US dollars (current prices) |
| reva.economy.gdp_real_growth | wb.wdi_econ.NY.GDP.MKTP.KD.ZG | imf.weo.NGDP_RPCH | 7,258 | 1,904 | 0.2623 | 2 | 2 | 0.05 | 0 | warn | BTN×41, NZL×38, SLE×37, UGA×35, KNA×32 | % / % change (constant prices) |
| reva.economy.gdp_per_capita_usd | wb.wdi_econ.NY.GDP.PCAP.CD | spc.pocket.GDPCPCUSD | 313 | 60 | 0.1917 | 0.9076 | 0 | 0.05 | 0 | warn | NRU×13, VUT×11, FJI×9, WSM×7, TUV×6 | Current US$ / USD per person |
| reva.economy.gdp_usd | wb.wdi_econ.NY.GDP.MKTP.CD | spc.pocket.GDPCUSD | 313 | 58 | 0.1853 | 0.9077 | 0 | 0.05 | 0 | warn | NRU×12, VUT×11, FJI×9, WSM×8, TON×5 | Current US$ / thousand USD |
| reva.economy.inflation_cpi | imf.weo.PCPIPCH | wb.wdi_econ.FP.CPI.TOTL.ZG | 7,146 | 992 | 0.1388 | 1.961 | 0 | 0.1 | 0.5 | warn | ETH×35, NPL×33, TON×30, BTN×28, EGY×28 | % change (CPI, period average) / % |
| reva.environment.co2_per_capita | owid.co2.co2_per_capita | undp.hdr25.co2_prod | 6,543 | 825 | 0.1261 | 0.9695 | 0 | 0.05 | 0 | warn | GNB×34, SAU×32, ERI×30, SOM×23, CIV×23 | tonnes per person (t/person) / tonnes per capita |
| reva.science.internet_users | wb.wdi_econ.IT.NET.USER.ZS | spc.digital.DD024 | 230 | 14 | 0.0609 | 0.4495 | 0 | 0.05 | 0 | info | FJI×6, SLB×4, MHL×3, NRU×1 | % of population / percent |

… 42 more rows in the JSON report.

Examples:

* `{"concept": "reva.economy.gdp_real_growth", "iso3": "TKM", "year": 1996, "a": "wb.wdi_econ.NY.GDP.MKTP.KD.ZG", "a_value": 6.699991986819853, "a_raw": "1cbb81b7e8e656e0", "b": "imf.weo.NGDP_RPCH", "b_value": -6.7, "b_raw": "a5b8f62b26f71d4e", "rel_diff": 2.0}`
* `{"concept": "reva.trade.current_account_gdp", "iso3": "IRQ", "year": 2024, "a": "imf.weo.BCA_NGDPD", "a_value": -2.990081, "a_raw": "a5b8f62b26f71d4e", "b": "wb.wdi_econ.BN.CAB.XOKA.GD.ZS", "b_value": 2.9918689650248886, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 1.9994}`
* `{"concept": "reva.economy.gdp_real_growth", "iso3": "JAM", "year": 1988, "a": "wb.wdi_econ.NY.GDP.MKTP.KD.ZG", "a_value": 3.974302015402472, "a_raw": "1cbb81b7e8e656e0", "b": "imf.weo.NGDP_RPCH", "b_value": -3.992571, "b_raw": "a5b8f62b26f71d4e", "rel_diff": 1.9954}`
* `{"concept": "reva.trade.current_account_gdp", "iso3": "MDV", "year": 1985, "a": "imf.weo.BCA_NGDPD", "a_value": 4.345395, "a_raw": "a5b8f62b26f71d4e", "b": "wb.wdi_econ.BN.CAB.XOKA.GD.ZS", "b_value": -4.324213557820116, "b_raw": "1cbb81b7e8e656e0", "rel_diff": 1.9951}`
* `{"concept": "reva.finance.government_net_lending_gdp", "iso3": "COG", "year": 2004, "a": "imf.gfs_soo.GNLB_T|sector=S13", "a_value": -3.27355417792539, "a_raw": "60836e5484699aae", "b": "imf.weo.GGXCNL_NGDP", "b_value": 3.325542, "b_raw": "a5b8f62b26f71d4e", "rel_diff": 1.9844}`

## units

### `units.bounded_share` — shares / bounded indices outside their declared range (warn, 531 issues)

| where | cells |
|---|---|
| wb.wdi_social \| EG.ELC.RNWX.ZS \| % (share) of total electricity production \| share_pct_of_total | 453 |
| wrp.national \| sumreligpct \| proportion (0-1) \| fraction_0_1 | 42 |
| wb.wdi_social \| ER.H2O.FWDM.ZS \| % (share) of total freshwater withdrawal \| share_pct_of_total | 27 |
| wb.wdi_social \| ER.H2O.FWAG.ZS \| % (share) of total freshwater withdrawal \| share_pct_of_total | 4 |
| sscp.frac_v1 \| language_frac \| index (0-1) \| fraction_0_1 | 2 |
| wb.gender \| SL.TLF.BASC.MA.ZS \| % of male working-age population with basic education \| share_pct_of_total | 1 |
| wb.wdi_social \| EG.USE.CRNW.ZS \| % (share) of total energy \| share_pct_of_total | 1 |
| wb.wdi_social \| SL.TLF.BASC.MA.ZS \| % of male working-age population with basic education \| share_pct_of_total | 1 |

Examples:

* `{"table": "sscp.frac_v1", "indicator": "language_frac", "geo": "772", "period": "2026-01-01..2027-01-01", "value": "-0.09804699999999976", "_rule": "fraction_0_1", "_eff_unit": "index (0-1)", "is_proj": false, "raw": "13718be677d1e686"}`
* `{"table": "sscp.frac_v1", "indicator": "language_frac", "geo": "184", "period": "2026-01-01..2027-01-01", "value": "-0.33402900000000013", "_rule": "fraction_0_1", "_eff_unit": "index (0-1)", "is_proj": false, "raw": "13718be677d1e686"}`
* `{"table": "wb.gender", "indicator": "SL.TLF.BASC.MA.ZS", "geo": "SGP", "period": "2014-01-01..2015-01-01", "value": "113.29", "_rule": "share_pct_of_total", "_eff_unit": "% of male working-age population with basic education", "is_proj": false, "raw": "2990bb8c96ded0b3"}`
* `{"table": "wb.wdi_social", "indicator": "ER.H2O.FWAG.ZS", "geo": "BOL", "period": "2021-01-01..2022-01-01", "value": "533.2062658", "_rule": "share_pct_of_total", "_eff_unit": "% (share) of total freshwater withdrawal", "is_proj": false, "raw": "1cbb81b7e8e656e0"}`
* `{"table": "wb.wdi_social", "indicator": "ER.H2O.FWAG.ZS", "geo": "BOL", "period": "2020-01-01..2021-01-01", "value": "521.9285082", "_rule": "share_pct_of_total", "_eff_unit": "% (share) of total freshwater withdrawal", "is_proj": false, "raw": "1cbb81b7e8e656e0"}`

### `units.negative_count` — negative values in count-like units (warn, 942 issues)

| where | cells |
|---|---|
| wb.edstats \| UIS.MENF.56 \| number \| non_negative_count | 942 |

Examples:

* `{"table": "wb.edstats", "indicator": "UIS.MENF.56", "geo": "GIB", "period": "2002-01-01..2003-01-01", "value": "-2", "_rule": "non_negative_count", "_eff_unit": "number", "is_proj": false, "raw": "8ccdd02f411e3f06"}`
* `{"table": "wb.edstats", "indicator": "UIS.MENF.56", "geo": "ABW", "period": "2003-01-01..2004-01-01", "value": "-4", "_rule": "non_negative_count", "_eff_unit": "number", "is_proj": false, "raw": "8ccdd02f411e3f06"}`

### `units.per_capita_identity` — per-capita × population vs published total (warn, 217 issues)

Same provider's per-capita value × population (× scale) compared with its total.

| identity | checked | violations | rel_tol | worst |
|---|---|---|---|---|
| wdi.gdp_per_capita_x_population | 11,764 | 217 | 0.01 | CYP 2018 rel 0.304 |
| weo.gdp_per_capita_x_population | 7,036 | 0 | 0.01 |  |
| wdi.co2_per_capita_x_population | 11,165 | 0 | 0.02 |  |

Examples:

* `{"identity": "wdi.gdp_per_capita_x_population", "iso3": "CYP", "year": 2018, "product": 37004352857.82422, "total": 25754011492.220375, "rel_diff": 0.30403}`
* `{"identity": "wdi.gdp_per_capita_x_population", "iso3": "CYP", "year": 2019, "product": 37605749437.51953, "total": 26196660680.117603, "rel_diff": 0.30339}`
* `{"identity": "wdi.gdp_per_capita_x_population", "iso3": "CYP", "year": 2017, "product": 32921538966.89453, "total": 22946583375.55063, "rel_diff": 0.30299}`

## time

### `time.future_observation` — non-projection cells starting after today (fail, 15,329 issues)

| where | cells |
|---|---|
| fao.gt | 10,804 |
| ilostat.eap_dwap_sex_age_rt_a | 2,349 |
| ilostat.une_deap_sex_age_rt_a | 783 |
| oecd.pag | 503 |
| eia_steo.steo_observed | 354 |
| ilostat.how_temp_sex_nb_a | 261 |
| ilostat.sdg_0831_sex_eco_rt_a | 261 |
| untc.treaty_status | 7 |
| who.gho | 6 |
| imf.er_m | 1 |

Examples:

* `{"table": "eia_steo.steo_observed", "indicator": "STEO.COPRAP.A", "geo": "not_stated", "period": "2027-01-01..2028-01-01", "value": "0.20265311761", "flags": "{\"frequency\":\"A\",\"last_historical_period\":\"2027\",\"series_last_updated\":\"2026-10-06T16:02:16-04:00\",\"series_name\":\"Crude Oil Production: Appalachia, Annual\"}", "raw": "828caff63e730383"}`
* `{"table": "eia_steo.steo_observed", "indicator": "STEO.COPRAP.M", "geo": "not_stated", "period": "2026-11-01..2026-12-01", "value": "0.204620601", "flags": "{\"frequency\":\"M\",\"last_historical_period\":\"202712\",\"series_last_updated\":\"2026-10-06T16:02:16-04:00\",\"series_name\":\"Crude Oil Production: Appalachia, Monthly\"}", "raw": "828caff63e730383"}`
* `{"table": "fao.gt", "indicator": "5058|7225|src=3050", "geo": "1", "period": "2030-01-01..2031-01-01", "value": "32.211300", "flags": "{\"estimate_kind\":\"forecast\",\"flag\":\"F\",\"flag_meaning\":\"Forecast value\"}", "raw": "d2c47e116553711f"}`
* `{"table": "fao.gt", "indicator": "5058|7225|src=3050", "geo": "1", "period": "2050-01-01..2051-01-01", "value": "35.934100", "flags": "{\"estimate_kind\":\"forecast\",\"flag\":\"F\",\"flag_meaning\":\"Forecast value\"}", "raw": "d2c47e116553711f"}`
* `{"table": "ilostat.eap_dwap_sex_age_rt_a", "indicator": "EAP_DWAP_SEX_AGE_RT|sex=SEX_F|classif1=AGE_AGGREGATE_Y15-24", "geo": "X01", "period": "2027-01-01..2028-01-01", "value": "34.896", "flags": "{\"estimate_type\":\"ilo_estimate_in_national_series\",\"ilo_source\":\"XA:8405\",\"ilo_source_label\":\"ILO - Modelled Estimates\"}", "raw": "c4ce86e73efaa271"}`

### `time.projection_word_unflagged` — non-projection cells whose estimate kind / flag says forecast or projection (fail, 10,804 issues)

| where | cells |
|---|---|
| fao.gt | 10,804 |

Examples:

* `{"table": "fao.gt", "indicator": "5058|7225|src=3050", "geo": "1", "period": "2030-01-01..2031-01-01", "value": "32.211300", "kind_words": "forecast Forecast value", "raw": "d2c47e116553711f"}`
* `{"table": "fao.gt", "indicator": "5058|7225|src=3050", "geo": "1", "period": "2050-01-01..2051-01-01", "value": "35.934100", "kind_words": "forecast Forecast value", "raw": "d2c47e116553711f"}`

### `time.mixed_precision_indicator` — indicators mixing period lengths (day/month/quarter/year…) (info, 167 issues)

| where | cells |
|---|---|
| factbook.figures_2026_01 | 55 |
| undp.gmpi2026 | 24 |
| ipu.parline | 22 |
| cowtc.entity_status | 19 |
| ipu.elections | 18 |
| cow.wars4 | 10 |
| coldat.dyads_v3 | 6 |
| icow.colhist_v11 | 5 |
| cshapes.polities_cow | 4 |
| cshapes.polities_gw | 4 |

Examples:

* `{"table": "coldat.dyads_v3", "indicator": "colonial_rule|colonizer=portugal|agg=mean", "classes": ["other", "year"]}`
* `{"table": "coldat.dyads_v3", "indicator": "colonial_rule|colonizer=portugal|agg=max", "classes": ["other", "year"]}`
* `{"table": "cowtc.entity_status", "indicator": "status|relation=claimed_by|counterpart=200", "classes": ["other", "year"]}`
* `{"table": "cowtc.entity_status", "indicator": "status|relation=part_of|counterpart=810", "classes": ["other", "year"]}`
* `{"table": "cow.wars4", "indicator": "batdeath|type=inter|war=211|side=1", "classes": ["other", "year"]}`

### `time.period_not_ended` — non-projection cells for a period that has not ended (info, 37,710 issues)

| where | cells |
|---|---|
| pubchem.nuclides | 17,168 |
| unga.vote_shares | 6,755 |
| ilostat.eap_dwap_sex_age_rt_a | 2,349 |
| pubchem.elements | 1,555 |
| inform.risk_2026_natural_hazards | 1,528 |
| chebi.composition | 1,408 |
| fao.oa | 1,335 |
| ilostat.une_deap_sex_age_rt_a | 783 |
| unpk.contrib_annual | 696 |
| unpk.totals | 590 |
| oecd.cts_cit | 506 |
| pubchem.compounds | 479 |

… 20 more rows in the JSON report.

Examples:

* `{"table": "chebi.composition", "indicator": "element_mass_share|chebi=131366|element=B", "geo": "not_geographic", "period": "2026-01-01..2027-01-01", "value": "0.103596", "raw": "5c5d6d4b2c84d7bc"}`
* `{"table": "chebi.composition", "indicator": "element_mass_share|chebi=131366|element=H", "geo": "not_geographic", "period": "2026-01-01..2027-01-01", "value": "0.0579598", "raw": "5c5d6d4b2c84d7bc"}`
* `{"table": "eia_steo.steo_observed", "indicator": "STEO.COPRAP.A", "geo": "not_stated", "period": "2026-01-01..2027-01-01", "value": "0.20024897556", "raw": "828caff63e730383"}`
* `{"table": "eia_steo.steo_observed", "indicator": "STEO.COPRAP.M", "geo": "not_stated", "period": "2026-10-01..2026-11-01", "value": "0.204832761", "raw": "828caff63e730383"}`
* `{"table": "eurostat.earn_mw_cur", "indicator": "earn_mw_cur|currency=EUR", "geo": "AL", "period": "2026-07-01..2027-01-01", "value": "531", "raw": "399fda45581619de"}`

### `time.period_open_at_publication` — non-projection cells whose period was still open when published (info, 37 issues)

| where | cells |
|---|---|
| vintage_weo.weo_imf | 37 |

Examples:

* `{"table": "vintage_weo.weo_imf", "indicator": "BCA_NGDPD|v=2025-10", "geo": "BGD", "period": "2025-01-01..2026-01-01", "value": "0.031", "available_at": "2025-10-14", "raw": "c92a01195a26908a"}`
* `{"table": "vintage_weo.weo_imf", "indicator": "BCA_NGDPD|v=2025-10", "geo": "HTI", "period": "2025-01-01..2026-01-01", "value": "0.028", "available_at": "2025-10-14", "raw": "c92a01195a26908a"}`

### `time.forecast_as_observation` — non-projection cells starting at/after their availability date (pass, 0 issues)

### `time.kg_future_statement` — KG statements dated after today (pass, 0 issues)

31 more on predicates declared future-valid (kg_future_ok_predicates).

### `time.precision_mismatch` — period length differs from the table's declared frequency (pass, 0 issues)

### `time.projection_table_unflagged` — cells of projection tables without a projection flag (pass, 0 issues)

## graph

### `graph.cycles.is_a` — cycles in is_a (child → parent edges) (warn, 1 issues)

| cycles | nodes_in_cycles | edges | predicates |
|---|---|---|---|
| 1 | 2 | 162,380 | P279, chebi:has_role, chebi:is_a, col:parent, doid:subclass_of, gbif:parent, globi:resolved_within, mondo:subclass_of, p |

Examples:

* `{"nodes": ["cl:0000163", "cl:0000164"], "size": 2, "edges": ["cl:0000163 -uberon:subclass_of-> cl:0000164", "cl:0000164 -uberon:subclass_of-> cl:0000163"]}`

### `graph.dangling_object` — edges whose object is not a KG entity (by predicate) (warn, 121,597 issues)

Object id has no kg.entity row in this origin (unlabeled node in family pages).

| predicate | statements | with_object | dangling_object | dangling_subject | example | subject_example | share |
|---|---|---|---|---|---|---|---|
| gleif:wikidata_item | 53,463 | 53,463 | 51,183 | 50,142 | row 3267523 → Q1000065 | lei:004L5FPTUREIWK9T2N63 | 0.9574 |
| P31 | 12,503 | 12,503 | 11,776 | 0 | row 1043615 → Q154547 |  | 0.9419 |
| P2936 | 9,844 | 9,843 | 9,843 | 0 | row 1065903 → Q1568 |  | 1 |
| P463 | 6,051 | 6,051 | 5,161 | 0 | row 983130 → Q1043527 |  | 0.8529 |
| unichem:chebi | 3,288 | 3,288 | 3,244 | 0 | row 4939058 → chebi:100147 |  | 0.9866 |
| v4link:classified_as | 3,431 | 3,431 | 2,633 | 0 | row 4473482 → hs2022:252400 |  | 0.7674 |
| P36 | 2,974 | 2,960 | 2,509 | 0 | row 1065902 → Q213026 |  | 0.8476 |
| P710 | 21,244 | 21,244 | 2,421 | 0 | row 2447219 → Q7885457 |  | 0.114 |
| P37 | 1,864 | 1,861 | 1,861 | 0 | row 1055425 → Q9205 |  | 1 |
| P122 | 1,766 | 1,766 | 1,750 | 0 | row 1065909 → Q184558 |  | 0.9909 |
| P527 | 2,237 | 2,237 | 1,746 | 0 | row 2444687 → Q130803598 |  | 0.7805 |
| P1344 | 2,711 | 2,711 | 1,695 | 0 | row 1065923 → Q3249599 |  | 0.6252 |

… 116 more rows in the JSON report.

Examples:

* `{"predicate": "gleif:wikidata_item", "example": "row 3267523 → Q1000065"}`
* `{"predicate": "P31", "example": "row 1043615 → Q154547"}`
* `{"predicate": "P2936", "example": "row 1065903 → Q1568"}`
* `{"predicate": "P463", "example": "row 983130 → Q1043527"}`
* `{"predicate": "unichem:chebi", "example": "row 4939058 → chebi:100147"}`

### `graph.dangling_subject` — statements whose subject is not a KG entity (warn, 75,217 issues)

| predicate | statements | dangling_subject | example |
|---|---|---|---|
| gleif:wikidata_item | 53,463 | 50,142 | lei:004L5FPTUREIWK9T2N63 |
| v4link:wikidata_item | 53,318 | 21,595 | uberon:0000018 |
| P166 | 1,096 | 1,034 | Q1002250 |
| P19 | 972 | 971 | Q1002250 |
| untc:party | 8,389 | 586 | Q208645 |
| P1891 | 5,219 | 444 | Q208645 |
| chembl:indication_mesh | 8,683 | 200 | chembl:CHEMBL1187724 |
| chembl:indication | 8,457 | 191 | chembl:CHEMBL1187724 |
| P17 | 100,174 | 48 | Q8470 |
| untc:mtdsg_number | 55 | 3 | Q208645 |
| P580 | 69,168 | 1 | ucdp:conflict:16379 |
| icowcol:cow_system_entry | 217 | 1 | Q165040 |

… 1 more rows in the JSON report.

### `graph.located_in_no_country` — located_in chains that never reach a country (warn, 921 issues)

Country = one of 498 crosswalk QIDs / iso3: nodes; anatomical/genomic located_in excluded.

| predicate | subjects | not_reaching | share |
|---|---|---|---|
| P17 | 97,577 | 539 | 0.0055 |
| P276 | 205 | 170 | 0.8293 |
| P131 | 277 | 145 | 0.5235 |
| P706 | 61 | 49 | 0.8033 |
| ucdp:location | 1,682 | 7 | 0.0042 |
| P159 | 14 | 5 | 0.3571 |
| gppd:country | 34,936 | 3 | 0.0001 |
| glottolog:spoken_in | 9,094 | 2 | 0.0002 |
| P2541 | 1 | 1 | 1 |
| P740 | 1 | 0 | 0 |
| cbattr:country | 658 | 0 | 0 |
| ghcnm:in_adm1 | 2 | 0 | 0 |

… 6 more rows in the JSON report.

Examples:

* `{"subject": "Q2465074", "predicate": "P131", "chain": "Q2465074 → Q12548 → Q12548 (loop)"}`
* `{"subject": "Q21077045", "predicate": "P131", "chain": "Q21077045 → Q12548 → Q12548 (loop)"}`
* `{"subject": "Q322763", "predicate": "P131", "chain": "Q322763 → Q12548 → Q12548 (loop)"}`
* `{"subject": "Q42585", "predicate": "P131", "chain": "Q42585 → Q131964 → Q131964 (loop)"}`
* `{"subject": "Q235030", "predicate": "P131", "chain": "Q235030 → Q207118"}`

### `graph.cycles.part_of` — cycles in part_of (child → parent edges) (pass, 0 issues)

| cycles | nodes_in_cycles | edges | predicates |
|---|---|---|---|
| 0 | 0 | 119,874 | P361, P527, chebi:has_part, chembl:target_component, cliopatria:member_of, ensembl:has_chromosome, glottolog:family, glo |

## licence

### `licence.atlas_restricted` — atlas tables under restricted terms (must be named with values) (info, 16,631,319 issues)

Not a defect: non-commercial / share-alike / no-derivatives / not-open terms, flagged per value.

| table | rows | commercial_use | licence |
|---|---|---|---|
| vdem.indices | 5,177,398 | permitted | CC-BY-SA-4.0 |
| vdem.institutions | 3,674,843 | permitted | CC-BY-SA-4.0 |
| vdem.liberties | 2,890,806 | permitted | CC-BY-SA-4.0 |
| vintage_weo.weo_dbnomics | 973,864 | unknown | IMF Copyright and Usage terms |
| clio.infra | 951,495 | permitted | per dataset (CC0 1.0 or CC BY-SA 4.0, stated in each DataverseNL record and on every row) |
| unsd.ama | 794,323 | unknown | UNdata terms of use (free use with attribution) |
| un_sdg.sdg_headline | 354,639 | unknown | UNdata terms of use (free use with attribution) |
| vintage_us.rtdsm | 333,823 | unknown | no data licence stated; site footer: Copyright 2026. All rights reserved. Federal Reserve Bank of Philadelphia |
| reinhart_rogoff.global_crises | 171,546 | unknown | no licence stated by the provider (Copyright President & Fellows of Harvard College); data offered for download with cit |
| vintage_weo.weo_imf | 165,965 | unknown | IMF Copyright and Usage terms |
| wrp.national | 145,630 | restricted | COW terms of use (not an open licence): data must be cited; users cannot charge for access |
| uis.api | 118,837 | permitted | CC BY-SA 4.0 |

… 38 more rows in the JSON report.

### `licence.atlas_review_pending` — atlas tables whose licence review is pending (info, 30 issues)

| table | rows |
|---|---|
| imf.imts | 1,525,117 |
| imf.weo | 237,117 |
| reinhart_rogoff.global_crises | 171,546 |
| imf.er_m | 157,246 |
| imf.cpi_m | 131,444 |
| imf.bop | 129,176 |
| views.cm_sb | 110,016 |
| eia.spot_d | 90,255 |
| imf.pi_m | 71,397 |
| imf.ls_mq | 62,427 |
| imf.mfs_ir_m | 58,095 |
| oecd.hsl_mind_current | 57,765 |

… 18 more rows in the JSON report.

### `licence.atlas_tables` — atlas tables without a licence (pass, 0 issues)

Licence from the table artifact's rights, else the atlas source spec (as the atlas API serves it).

| basis | tables | rows |
|---|---|---|
| table_artifact | 331 | 108,175,861 |
| atlas_source_spec | 0 | 0 |
| none | 0 | 0 |

### `licence.causal_raw` — causal rows whose raw object carries no licence (pass, 0 issues)

Licence read from meta.artifact.rights of the cited raw object (as the families API does).

| source_key | rows | no_licence | restricted | review_pending |
|---|---|---|---|---|
| carbonbrief.attribution | 739 | 0 | 739 | 0 |
| cow.data | 3,311 | 0 | 3,311 | 0 |
| esrb.fcdb | 69 | 0 | 69 | 0 |
| icb.data | 4,393 | 0 | 4,393 | 0 |
| idmc.gidd | 20,898 | 0 | 20,898 | 0 |

### `licence.effects_raw` — effects rows whose raw object carries no licence (pass, 0 issues)

Licence read from meta.artifact.rights of the cited raw object (as the families API does).

| source_key | rows | no_licence | restricted | review_pending |
|---|---|---|---|---|
| effects.literature | 296 | 0 | 296 | 0 |

### `licence.kg_raw` — kg rows whose raw object carries no licence (pass, 0 issues)

Licence read from meta.artifact.rights of the cited raw object (as the families API does).

| source_key | rows | no_licence | restricted | review_pending |
|---|---|---|---|---|
| pubchem.pug | 46,133 | 0 | 46,133 | 0 |
| un.psdata | 20,430 | 0 | 0 | 20,430 |
| unsd.classifications | 85,309 | 0 | 85,309 | 0 |
| wikipedia.olympic_medal_tables | 1,952 | 0 | 1,952 | 0 |
| untc.mtdsg | 13,664 | 0 | 13,664 | 0 |
| carbonbrief.attribution | 5,383 | 0 | 5,383 | 0 |
| chembl.api | 46,164 | 0 | 46,164 | 0 |
| laeven_valencia.sbcd | 2,773 | 0 | 2,773 | 0 |
| cow.data | 88,893 | 0 | 88,893 | 0 |
| unichem.mapping | 8,655 | 0 | 8,655 | 8,655 |
| icb.data | 29,904 | 0 | 29,904 | 0 |
| unesco.ich | 2,244 | 0 | 2,244 | 0 |

… 13 more rows in the JSON report.

## provenance

### `provenance.effects_no_raw` — effects rows without a raw object (null or not archived) (warn, 5 issues)

| source_key | rows | no_raw | raw_not_archived |
|---|---|---|---|
| reva.eventstudy | 5 | 5 | 0 |

Examples:

* `{"source_key": "reva.eventstudy", "no_raw": 5, "raw_not_archived": 0}`

### `provenance.atlas_cell_raw` — atlas cells whose raw_sha256 is not a raw object of the table (pass, 0 issues)

### `provenance.causal_no_raw` — causal rows without a raw object (null or not archived) (pass, 0 issues)

### `provenance.kg_no_raw` — kg rows without a raw object (null or not archived) (pass, 0 issues)

### `provenance.objects` — archived objects missing or corrupt (store API, decoded) (pass, 0 issues)

Existence of every meta.artifact_location object via the store (zstd-transparent); a seeded sample is decoded and re-hashed (--verify-all re-hashes all).

| state | objects | bytes |
|---|---|---|
| ok | 13,799 |  |
| fully re-hashed | 200 | 57,735,324 |

### `provenance.referenced_not_archived` — raw sha256 cited by atlas cells / tables without an archived location (pass, 0 issues)

## causal

### `causal.disruption_axioms_unqualified` — 'disruption / dysfunction / alteration of X' axioms stored as 'X causes' without the source relation (warn, 823 issues)

AUDIT3 H1: relations RO:0004021, RO:0004020, RO:0004030; fixed when qualifiers carry source_relation.

| source_key | claims |
|---|---|
| mondo.obo | 823 |

Examples:

* `{"source_key": "mondo.obo", "claim_key": "ds:mondo.obo:mondo:MONDO:0000009|RO:0004020|reva_state:dysfunction_of:cl:0000233"}`

### `causal.missing_endpoint` — claim endpoints (cause / effect) that are not KG entities (warn, 2,361 issues)

Shown without a label (label_loaded false) until the endpoint is loaded; not a wrong claim by itself.

| source_key | side | claims | id_prefix |
|---|---|---|---|
| mondo.obo | cause | 1,226 | chebi |
| wikidata.wbapi | cause | 509 | wikidata |
| doid.obo | cause | 335 | mim |
| wikidata.wbapi | effect | 291 | wikidata |

Examples:

* `{"source_key": "mondo.obo", "side": "cause", "example": "chebi:17514"}`
* `{"source_key": "wikidata.wbapi", "side": "cause", "example": "Q5365420"}`
* `{"source_key": "doid.obo", "side": "cause", "example": "mim:102300"}`
* `{"source_key": "wikidata.wbapi", "side": "effect", "example": "Q19600530"}`

### `causal.self_loop` — claims whose cause is their effect (warn, 1 issues)

| source_key | claims |
|---|---|
| wikidata.wbapi | 1 |

### `causal.ungraded` — current causal claims without a grade for their row (warn, 11,435 issues)

stale_grade = a grade exists for the claim key but for an older row (re-delivered claim).

| source_key | claims | ungraded | stale_grade |
|---|---|---|---|
| mondo.obo | 9,426 | 9,426 | 0 |
| doid.obo | 1,270 | 1,270 | 0 |
| carbonbrief.attribution | 739 | 739 | 0 |

Examples:

* `{"source_key": "carbonbrief.attribution", "claim_key": "ds:carbonbrief.attribution:carbonbrief.attribution_studies:cbattr:00d19c8ba50096775b358b34"}`
* `{"source_key": "doid.obo", "claim_key": "ds:doid.obo:doid:DOID:0050425|RO:0004005|MIM:102300"}`
* `{"source_key": "doid.obo", "claim_key": "ds:doid.obo:doid:DOID:0040001|RO:0001022|FOODON:00002239"}`
* `{"source_key": "mondo.obo", "claim_key": "ds:mondo.obo:mondo:MONDO:0000009|RO:0004020|reva_state:dysfunction_of:cl:0000233"}`
* `{"source_key": "mondo.obo", "claim_key": "ds:mondo.obo:mondo:MONDO:0000004|RO:0004024|reva_state:disruption_of:go:0034651"}`

### `causal.wording_leak_predicates` — causal-worded KG predicates read as a non-causal family (warn, 1 issues)

Causal statements must be read through the causal loader as attributed claims; unmapped rows are listed for review only.

| predicate | statements | family | status |
|---|---|---|---|
| P1478 | 383 | None | unmapped (not traversed) |
| P1479 | 147 | None | unmapped (not traversed) |
| P1536 | 121 | None | unmapped (not traversed) |
| P1537 | 104 | None | unmapped (not traversed) |
| P1542 | 476 | None | unmapped (not traversed) |
| P828 | 445 | None | unmapped (not traversed) |
| doid:has_disease_driver | 61 | context | LEAK: read as a family |
| icb:trigger_location | 1,122 | None | unmapped (not traversed) |
| icb:trigger_type | 1,637 | None | unmapped (not traversed) |
| icb:triggering_entity | 1,212 | None | unmapped (not traversed) |
| icb:triggering_entity_kind | 425 | None | unmapped (not traversed) |
| ncei:tsunami_cause | 3,134 | None | unmapped (not traversed) |

Examples:

* `{"predicate": "doid:has_disease_driver", "statements": 61, "family": "context", "status": "LEAK: read as a family"}`

### `causal.wording_leak_labels` — non-causal family / link-type labels worded causally (pass, 0 issues)

