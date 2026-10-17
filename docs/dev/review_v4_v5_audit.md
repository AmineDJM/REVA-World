# V4/V5 adversarial honesty audit (agent AUDIT3, 2026-10-17)

Scope: the V4 world-model layers (KG statements, ontology and attribution causal claims, atlas cells) and the V5
prediction layers (effect estimates, forecast registry, decision engine) in the dev database `reva`, origin `live`,
read-only. Method follows `review_v3_causal_audit.md` / `review_v3_causal_audit2.md`: every check is an independent
re-implementation that reads the archived raw bytes and does not call the loaders under audit. No code, registry,
database row or archived object was changed; this file is the only output. Snapshot: DB state between 16:05 and
16:50 UTC (at the end: `max(kg.statement.id)` 4,607,964, `max(kg.causal_claim.id)` 114,261, 65,492 current live
claims, 77 current effect estimates — EFFECTS-MORE committed new curation files during the audit but had not loaded
them). Scripts and extracted samples are in the AUDIT3 scratchpad (`a3py/`, `audit3/`), not committed.

## 1. Summary

**Mechanical fidelity is clean.** 0 transcription errors in 1,160 sampled items: every sampled value, identifier,
quote and seal matches the archived raw bytes, all 418 cited raw objects re-hash to their sha256, all 72 published
effect estimates' quotes are re-found on their declared PDF page, the 4 forecast-registry seals verify, and no
forecast question was sealed after its outcome was observable.

**The honesty defects are semantic, not mechanical** — 2 High, 9 Medium, 9 Low:

* **H1** 823 Mondo claims say a *normal* process or structure "causes" a disease where Mondo says the disease is
  caused by its *disruption / dysfunction / alteration* (e.g. "catalytic activity → has cause → MONDO:0020584").
* **H2** 296 ClinGen *Refuted / Disputed / No Known Disease Relationship* curations are traversed as
  `associated_with` ("est associé à").
* Medium: 3,751 DOID "inheritance mode causes disease" claims; 15,247 V4 causal claims ungraded; a cumulative level
  response shown by the decision engine as an annual growth rate; an alphabetical tie-break that picks the
  publication-bias-*uncorrected* ECB mean; sanction-episode timing/units; 1,978 STEO cells that escape the projection
  filter; fiscal-year WEO cells labelled as calendar years (5 sealed questions affected); ambiguous LEI↔QID identity
  links kept; one causal relation classified causal by its label word.
* Name-based mappings are inventoried in §5 (7 found; none is a similarity match, all are exact-string or curator
  assignments; one — USGS facility countries — could use the identifier the source already publishes).

## 2. Method

### 2.1 Sample (seed `AUDIT3-2026101703`, recorded)

KG statements and causal claims were drawn per stratum with `ORDER BY md5(<key> || 'AUDIT3-2026101703') LIMIT n`
on `kg.statement_current` / `kg.causal_claim_current` (origin `live`); atlas cells with
`polars.DataFrame.sample(n, seed=2026101703)` on the latest live version of each table (Parquet read from the object
store). Effect estimates, registry questions/forecasts and the five reference decisions were audited as a census.

| layer | stratum | n | check against archived bytes |
|---|---|---:|---|
| KG | `chebi:formula_element` (made_of, mass shares) | 25 | formula on the cited `chemical_data.tsv` line; atom count; mass share recomputed from the archived PubChem periodic table (ff0f7597…) |
| KG | `pubchem:formula_element` | 10 | idem |
| KG | `uberon:part_of` | 25 | `relationship:` / `intersection_of:` line in the term stanza of `human-view.obo` |
| KG | `doid:has_location`, `mondo:has_location` | 10 | edge / logical definition in `doid.json`, stanza in `mondo.obo` |
| KG | `clingen:gene_disease_validity` | 20 | CSV line: assertion id, HGNC id, MONDO id, classification, symbol |
| KG | `globi.*` (all 9 predicates) | 25 | row (source id, target id, interaction type, count) in the archived API response; id prefix mapping |
| KG | `resolve:in_biome` / `in_realm` | 15 | DBF record decoded from the archived zip byte range (raw deflate) |
| KG | `usgs_myb:equity_owner_name` / `gppd:owner_name` / `usgs_myb:country` | 15 / 10 / 10 | CSV row and column |
| KG | `gleif:direct_parent` / `ultimate_parent` | 20 | `relationships.<x>-parent` of the record in the archived page |
| KG | `nuts:part_of` / `oecdreg:part_of` | 10 / 10 | both codes in the response; parent = code minus last character / `CL_REGIONAL` `<Parent>` |
| KG | `ghcnm:in_adm1` | 25 | **point-in-polygon recomputed** (shapely + pyshp) from the inventory line and the archived Natural Earth zip; feature index, `iso_3166_2` uniqueness, no second covering polygon |
| KG | GHCN `P17` (FIPS prefix) | 10 | inventory line, FIPS → ISO3 |
| KG | `gleif:wikidata_item`, `v4link:wikidata_item` | 10 / 10 | statement GUID, value and item in the archived WDQS / QLever JSON |
| KG | `v4link:corresponds_to` | 10 | both codes in the UNSD table |
| KG | `hgnc:located_on_chromosome` / `encodes` | 10 | HGNC TSV line |
| causal | DOID: `geno:` / `ncbitaxon:` / other causes | 6 / 8 / 6 | axiom in `doid.json`; direction; codebook definition verbatim |
| causal | Mondo: gene / taxon / other causes | 7 / 7 / 6 | stanza in `mondo.obo` (fillers as `identifiers.org` IRIs); codebook verbatim in `mondo.obo` / `ro.obo` |
| causal | Carbon Brief T2 (`carbonbrief.attribution_studies`) | 25 | CSV row: classification → polarity/stance, summary = mechanism, DOI in citation/link, ISO3 list |
| atlas | 21 tables × 5 cells: `wb_cmo.monthly_prices`, `eurostat.hicp_m`, `eurostat.gdp_q`, `imf.cpi_m`, `imf.qnea_q`, `oecd.kei_mq`, `fao_ffpi.ffpi_m`, `cckp_ts.cru_monthly`, `cckp_ts.cmip6_annual`, `ghcnm.tavg_qcf_monthly`, `ecb.fx_ref`, `ust.par_yield`, `eia.spot_d`, `nyfed.rates_d`, `bis.cbpol_d`, `vintage_weo.weo_dbnomics`, `vintage_weo.weo_imf`, `vintage_us.spf_median`, `eia_steo.steo_forecast`, `eia_steo.steo_observed`, `who.ghe` | 105 | selector resolved in the raw (xlsx cell, JSON path, JSON-stat index, SDMX series/obs, STEO line, GHCN fixed columns, CSV line/record, EIA HTML offset) and value compared as decimals |
| atlas | zero-valued cells (missing-never-zero) in 7 tables | 42 | as above; FishStat zeros traced to the FAO cells and their `STATUS` |
| effects | census | 77 | PDF text (`pdftotext -layout`) of the archived document: quote on the declared page, `as_printed` in quote, point = printed (or negated), sign, CI order, measure/unit/shock, `shock_profile`, rights |
| registry | census: 3 issue batches + 1 back-test | 586 questions, 2,287 forecasts | manifest bytes re-hashed = seal; pinned table recorded before issue; period vs issue time; target cell not observed in the pinned version; thresholds; persistence point = last observed |
| engine | 5 reference decisions (lead's read-only run, `ops/predict_run.json`, 15:50 UTC) | 5 | every hop's estimate key, coefficient and scaling against `kg.effect_estimate_current`; alternatives; gaps |

Population checks (not sampled): USGS equity shares (11,679 statements vs CSV), USGS country vs facility-ID prefix
(12,027), ClinGen classifications (3,706), ontology claims by relation and cause namespace (14,508), LEI↔QID ambiguity
(53,463), dangling objects of V4 predicates, projection flags of 10 projection/vintage tables (3.0 M cells),
missing-vs-zero in 14 tables, period precision of 13 sub-annual tables, vintage `available_at` of 6 vintage tables,
licence flags of 11 restricted tables, `meta.artifact.rights` of every raw object cited by V4 KG sources.

<details><summary>Sampled ids</summary>

`kg.statement.id` (280): 2707699, 2708363, 2711986, 2732077, 2736244, 2752098, 2752683, 2753109, 2753242, 2754337,
2766603, 2874102, 2874369, 2875739, 2876889, 2877163, 2877257, 2877449, 2877637, 2877670, 2877708, 2878161, 2878258,
2878514, 2878707, 2878774, 2878836, 2879471, 2879906, 2879929, 2880370, 2880910, 2881425, 2881673, 2882006, 2882010,
2900758, 2900904, 2901219, 2901457, 2901459, 2901885, 2901971, 2902435, 2902469, 2902540, 2902579, 2902733, 2902760,
2902789, 2902813, 2902858, 2903029, 2903136, 2903142, 2903714, 2925711, 2925877, 2925983, 2926768, 2927780, 2927794,
2928005, 2928185, 2928256, 3224680, 3224852, 3225056, 3225073, 3225250, 3225947, 3226161, 3226415, 3227417, 3227520,
3227625, 3227842, 3227868, 3228167, 3228617, 3228812, 3229183, 3229226, 3229227, 3229410, 3229454, 3230044, 3230067,
3230444, 3230665, 3230724, 3230780, 3230931, 3230970, 3230989, 3230996, 3231137, 3231171, 3231343, 3231567, 3238920,
3239668, 3239978, 3240096, 3241383, 3242018, 3245660, 3246647, 3250928, 3250983, 3254647, 3256300, 3257325, 3257646,
3259009, 3259190, 3260063, 3263086, 3263529, 3264686, 3265001, 3265057, 3265309, 3265424, 3265907, 3265919, 3265987,
3266314, 3266500, 3266515, 3266630, 3266668, 3266855, 3267076, 3267230, 3269181, 3270515, 3275022, 3281568, 3283583,
3295604, 3298169, 3299191, 3304936, 3304959, 3325095, 3327693, 3328274, 3329618, 3330114, 3337832, 3339806, 3342118,
3342725, 3347526, 3349746, 3351506, 3351802, 3352458, 3352535, 3354839, 3357723, 3359464, 3359547, 3362010, 3371251,
3416004, 3443240, 3498621, 3509989, 3518078, 3567102, 3584202, 3619699, 3622585, 3672310, 3672978, 3673733, 3675761,
3680282, 3682848, 3686393, 3692968, 3693504, 3719035, 3720150, 3730500, 3734043, 3736212, 3752863, 3761645, 3766184,
3767971, 3772935, 3779978, 3796809, 3799296, 3801233, 3814418, 3820975, 3829836, 3835891, 3840348, 3853570, 3861363,
3868729, 3871153, 3879730, 3881166, 3883557, 3896069, 3896391, 3899006, 3904759, 3904960, 3906876, 3907231, 3910751,
3911898, 3912621, 3912626, 3914236, 3918084, 3918509, 3919161, 3919766, 3922190, 3925250, 3925890, 3927712, 3930004,
3930268, 3931173, 3931733, 3932480, 3964553, 3993507, 4062336, 4105151, 4125587, 4147378, 4196073, 4226316, 4235874,
4261377, 4361925, 4369510, 4373521, 4374748, 4381758, 4383093, 4383912, 4389334, 4389437, 4391413, 4396338, 4404730,
4406249, 4415564, 4418992, 4420761, 4423873, 4438687, 4439042, 4449748, 4461025, 4461362, 4466099, 4466591, 4470425,
4476788, 4476964, 4477826, 4477835, 4478375, 4480464, 4481039, 4481420, 4483027, 4483374.

`kg.causal_claim.id` (65): 99070, 99132, 99167, 99393, 99503, 99557, 99587, 100229, 100328, 101943, 102112, 102966,
103307, 103382, 103401, 103459, 103561, 103704, 103884, 103995, 104206, 104344, 104487, 104536, 104667, 104691, 104701,
107142, 108096, 108951, 109131, 109484, 109846, 109931, 110446, 111381, 112485, 112508, 112905, 113091, 113592, 113596,
113606, 113614, 113639, 113686, 113714, 113763, 113777, 113861, 113871, 113906, 113911, 113972, 113996, 114009, 114041,
114051, 114068, 114086, 114092, 114148, 114149, 114151, 114203.

</details>

### 2.2 Note on the object store during the audit

DISKZ began wrapping archived objects in place (magic `\x89RVZ\r\n\x1a\n`, same file name = sha256 of the *raw*
bytes) at about 16:43 UTC. 44 of the 418 cited objects were already wrapped when re-checked; all were read through
`reva.adapters.objects.compression.decode_bytes` and re-hash to their names. No object was missing or corrupt. Anyone
re-running these checks must decode wrapped objects first (a plain `sha256sum` of the file no longer matches).

## 3. Error rates per layer

"Fidelity" = the stored value / identifier / quote equals what the archived bytes say. "Honesty" = the wording,
family, flag or label a reader sees is true to the source.

| layer | items checked | fidelity errors | honesty defects (sample → population) |
|---|---:|---:|---|
| KG statements (V4) | 280 | **0** | 20/20 GLEIF parent edges carry the child's registration status as relationship qualifiers (L1, 455 in population); 0/20 ClinGen in sample but 296/3,706 (8.0 %) refuted/disputed/no-relationship traversed as associated_with (H2); 0/25 ADM1 placements wrong; 0/35 made_of shares wrong |
| causal claims (V4) | 65 (40 codebook quotes) | **0** (codebook 40/40 verbatim) | 3/6 Mondo "other" claims inverted (H1: 823/9,426 = 8.7 % of Mondo); 6/6 DOID `geno:` claims are inheritance modes as causes (M1: 3,751/5,082 = 73.8 % of DOID); 65/65 ungraded (M2: 15,247 claims) |
| atlas cells (V4/V5) | 105 + 42 zero cells | **0** | projection filter misses 1,978 STEO cells (M6); 30 calendar-2026 WEO cells of fiscal-year economies shown as observations (M7); 0 missing-as-zero (all 42 zeros are zeros in the source) |
| effect estimates (V5) | 77 (census) | **0** (72/72 quotes on the declared page) | 5/77 without `shock_profile` (L3); 4/5 REVA runs carry a growth-rate effect id for a cumulative level response (M3); 1 publication horizon/unit convention (M5) |
| forecast registry (V5) | 586 questions, 2,287 forecasts, 4 seals | **0** (4/4 seals verify; 0 look-ahead) | 5/586 (0.9 %) WEO questions worded as calendar years resolve on fiscal-year values (M7) |
| decision engine (V5) | 5 decisions, 27 hops | **0** numbers without an attributed estimate; 0 sums/averages | 3 defects (M3, M4, M5) |

Verified clean (population): USGS equity shares 10,309/10,309 equal the CSV, 1,370 `-999` → Missing (never 0);
USGS country 12,027/12,027 consistent with the facility-ID ISO3 prefix; STEO observed table has 0 monthly cells after
`last_historical_period`; every CMIP6 cell (704,052) is excluded by `PROJECTION_SQL`; every WEO-vintage cell with a
period ≥ its vintage year is a projection except 37 fiscal-year *actuals*; RTDSM/OECD history cells never start after
their `available_at`; 0 null-value rows without a `missing` reason in 14 tables; daily/monthly/quarterly period
lengths exact in 13 tables; registry thresholds = last observed value 586/586; pinned tables recorded before issue
586/586.

## 4. Defects (ranked)

### High

**H1 — Mondo "disruption / dysfunction / alteration" axioms become claims that the normal process or structure causes
the disease (823 claims).** Mondo relations RO:0004021 *disease caused by disruption of* (259), RO:0004020 *disease
has basis in dysfunction of* (74) and RO:0004030 *disease arises from alteration in structure* (490) are loaded as
`relation = has_cause`, `cause_id = <filler>` (a GO process, a cell type, an UBERON structure, a chromosome band), with
no qualifier and `polarity` null. `reva.causal.graph._wording` builds the sentence from the relation's generic label
only, so the reader gets "According to mondo.obo, *catalytic activity* causes …" where Mondo says the disease is caused
by the *disruption* of catalytic activity. Evidence: `ds:mondo.obo:mondo:MONDO:0020584|RO:0004021|GO:0003824` (claim
in the sample), `ds:mondo.obo:mondo:MONDO:0012761|RO:0004030|CHR:9606-chr3q29`; selector `relation_label` holds the
true relation but is not shown. The same shorthand (gene instead of "germline mutation in gene") affects 7,268
`RO:0004003/4001/4004` claims, less misleadingly. *Fix (V4HEALTH + causal owner):* keep the source relation in the claim
(`qualifiers.source_relation` + label) and use it in the wording ("…is caused by disruption of …"), or make the cause a
REVA-labelled node "disruption of <X>"; until then retract the 823 claims (append-only retraction, reason
`withdrawn_by_curator`).

**H2 — Refuted, disputed and "no known" gene–disease curations are traversed as associations (296 statements).**
`registry/kg/relations_v4.d/v4health.yaml` maps the whole predicate `clingen:gene_disease_validity` to
`associated_with`; classifications Refuted 49, Disputed 199, No Known Disease Relationship 48 are therefore family
hops "est associé à" in `/v1/kg/entities/{id}/families`, `/v1/kg/families/path` and Neurones. The classification sits in
`qualifiers.classification` only. A source statement that a gene is *not* (or not shown to be) associated is shown as
an association. *Fix:* emit those three classes under a separate predicate (e.g. `clingen:gene_disease_curation_negative`,
unmapped) or teach the family resolver a qualifier filter; re-load ClinGen (retracts by delivery).

### Medium

**M1 — DOID modes of inheritance as causes (3,751 claims).** DOID `has material basis in` (IDO:0000664) with fillers
`GENO:0000148` autosomal recessive (1,975), `GENO:0000147` autosomal dominant (1,413), other `GENO:` modes become `geno:* →
has_cause → doid:*` T1 claims (73.8 % of DOID claims; sample: 6/6 in the targeted stratum, e.g. claim 102966
`ds:doid.obo:doid:DOID:0112012|IDO:0000664|GENO:0000149`). The source does write it, but an inheritance pattern is not
a causal agent and will grade B (T1 + undetermined time). Already listed as V4HEALTH owner decision 6. *Fix:* route
`geno:` fillers to a KG statement `doid:mode_of_inheritance` (not causal) and retract the claims.

**M2 — 15,247 V4 causal claims are current but ungraded** (DOID 5,082, Mondo 9,426, Carbon Brief 739;
`kg.causal_grade_current` has no row for any of them; audit 2 had 0 ungraded claims). They are served by
`/v1/causal` and the chain service without a grade. *Fix (lead):* `reva causal refresh --origin live` after H1/M1 are
resolved, before the snapshot.

**M3 — Event-study level responses labelled as growth rates; the engine prints them as annual growth.** Four REVA runs
(`run:es-1b3eff04653ec3971c9c:h5` sanctions, `run:es-9af1a4b329e01ee5e382:h5` coups, `run:es-76c2041e480734d2686a:h5`
and `run:es-c3a9b13419c5b3a1f0ec:h5` earthquakes) have LHS = cumulative sum of growth from t to t+h ("≈ % change in the
level", in `effect_definition`) but `effect_id` = the *growth* concepts `wb.ny.gdp.pcap.kd.zg` / `wb.ny.gdp.mktp.kd.zg`.
The engine therefore shows, for the EU ban on Russian oil, "Croissance du PIB par habitant (% annuel)" RUS 2028 =
−0.197 (median) — that is the cumulative h=1 level response; the growth it implies for 2028 is h1 − h0 = −0.375.
*Fix:* PRED-EVENTSTUDY writes these against a level variable (e.g. `output.real_gdp_per_capita`, transform
`log_change`); PRED-ENGINE refuses an IRF whose `effect_definition` declares a cumulative LHS on a growth-rate concept.

**M4 — The engine's tie-break silently picks the publication-bias-uncorrected mean.** For the ECB +50 bp decision all
ten ECB OP 377 estimates tie on every rule of `estimates.CHOICE_RULE` until the last one (estimate key), so `…_mean`
wins alphabetically over `…_mean_bias_corrected`, `…_median` and `…_median_bias_corrected`. Result shown: real GDP
−0.5 %, consumer prices −0.36 %, inflation −0.1 pp; the same source's bias-corrected medians give −0.16 %, −0.10 %
(alternatives listed in the hops, never averaged — that part is correct). `v5_pred_engine.md` §7b reports the numbers
without saying which variant. *Fix:* a curated preference per publication entry (`curation.preferred_variant`) or one
scenario per variant; state the chosen variant in the result label.

**M5 — Sanction-episode estimates: timing, unit and applicability.** (a) Gutmann–Neuenkirch–Neumeier "first year of a
sanction episode" is stored as `horizon_kind at_horizon, 1 year`, so the engine puts −1.32 pp in 2028 and reports 2027
as `lag_not_published`; in the REVA runs the onset year is h = 0. (b) The same estimate has `effect_id
output.real_gdp_per_capita` (a level) with unit `percentage_points` of growth, displayed as "PIB réel par habitant
… pp". (c) The engine marks the REVA run `applicability.status = exact` for RUS because RUS is in the run's treated
country list — a pooled panel average is not a Russia-specific estimate. *Fix:* horizon 0 = impact year in curation
(document the convention in `v5_effect_interface.md`); growth-rate effect variable; applicability `panel_member`.

**M6 — 1,978 STEO cells escape the projection filter.** `atlas.eia_steo.steo_forecast` keeps the 16 series EIA
publishes without a history boundary with `estimate_kind = unknown_boundary` and no `projection` flag; `PROJECTION_SQL`
(`src/reva/atlas/query.py`) only catches `projection`, a truthy `projection` flag or `period_after_vintage`. 85 of those
cells are dated 2026-10 … 2027-12 (e.g. `STEO.PAPR_VE.M` Venezuela liquids, `STEO.COPS_OPEC_ROT.M`). No catalog
`source_order` references the table today, so no headline is affected yet. *Fix:* set `projection: "unknown_boundary"`
on those cells (connector bump) or extend `PROJECTION_SQL` to `unknown_boundary` / `published_history_boundary_unknown`.

**M7 — Fiscal-year WEO cells labelled as calendar years (V3 table `atlas.imf.weo`, used by PRED-REGISTRY).** For
economies whose WEO `latest_actual` is a fiscal year, NGDP_RPCH is fiscal-mapped (`period_basis = fiscal_year`) but
PCPIPCH, LUR, BCA, trade volumes are placed on calendar years (6,956 cells). 30 of them are non-projection cells for
calendar 2026 (BGD, EGY, NPL, PAK, WSM: e.g. EGY PCPIPCH 2026 = 13.275754 = FY2025/26 actual), i.e. an unfinished
calendar year shown as observed. Five sealed questions are worded as calendar years but will resolve on fiscal-year
values: `fq_ce2ce06b59d841b5091a9d62`, `fq_2cd61181ed8f5ad5b27bad82` (IND CPI 2026/2027), `fq_c1f49d578e1d3333dacee22e`
(EGY CPI 2027), `fq_7f4930b1540b2285e6aae2a0`, `fq_bd1b7d6205294f4f2ab21f02` (EGY unemployment 2026/2027). *Fix:*
fiscal mapping for every WEO indicator whose `latest_actual` is `FY…` (new `atlas.imf` version); registry: add a
resolution note / void those five at resolution (sealed manifests cannot be edited), and skip fiscal economies for
CPI/LUR in future cycles as already done for GDP.

**M8 — Ambiguous LEI ↔ Wikidata identity links are kept.** `gleif:wikidata_item` (53,463, source `wikidata.wdqs`)
keeps 193 LEIs held by several items and 324 items holding several LEIs ("kept as stated"), while V4LINK's
`identity_rules_v4.yaml` refuses exactly these cases. V4INDUSTRY's report advertises QID → LEI → GEM chains on this
bridge. *Fix:* apply the identity-rule engine (`v4link_common.derive_bridges`) or flag `ambiguous: true` and exclude
flagged rows from identity traversal.

**M9 — A relation classified causal by its label word.** `health_obo_common.RelationIndex.is_causal` accepts a relation
whose label carries a marker (`driver`, `trigger`, `basis in`, …). RO:0007001 *has disease driver* is **not** under
RO:0002410 *causally related to* in the archived `ro.obo` (f2145eae…; its parent is RO:0007000 *has driver*), so its 61
DOID claims are causal only because of the word "driver". The seven other relations traced (RO:0009501, RO:0004028,
RO:0004030, RO:0004022, RO:0004021, RO:0004005, RO:0004019) are under RO:0002410; the remaining ones were not traced. *Fix:* classify by RO hierarchy / an explicit id allow-list reviewed by the owner; never by label.

### Low

* **L1 — GLEIF parent edges carry the child's registration as relationship qualifiers.** All 455 `gleif:direct_parent`
  / `ultimate_parent` statements put the child record's `registration.corroborationLevel` / `status` into
  `corroboration_level` / `registration_status` of the *ownership edge*, and `relationship_type`
  (`IS_DIRECTLY_CONSOLIDATED_BY`) is composed by the loader (not in the archived bytes; the relationship record was not
  fetched). Read as "relationship fully corroborated". *Fix:* rename `child_registration_*`, mark
  `relationship_type_derived: true`.
* **L2 — Wrong licence on the Natural Earth admin-1 artifact.** `meta.artifact.rights` of efc59726… (the Natural Earth
  zip) says "US Government work (NOAA NCEI) … cite GHCN-M v4", `source_key noaa.ghcnm`; the families API takes licences
  from `meta.artifact.rights`, so the 16,721 Natural Earth statements are shown with NOAA's attribution (the statement
  `refs` say "Public domain (Natural Earth)" correctly). *Fix:* the families API prefers a statement-level `refs.licence`;
  register the next NE fetch with its own rights.
* **L3 — `shock_profile` missing on the 5 REVA-run estimates** (`meta.shock_profile` null, API shows `unspecified`),
  contrary to the interface ("every estimate states its shock_profile"). *Fix:* PRED-EVENTSTUDY writes
  `state_indicator` (onset dummies).
* **L4 — FishStat "not significant" cells summed silently.** 35,674 raw cells are `0` with `STATUS = N` (< half a
  unit); sums that contain them carry no flag, all-N sums read as plain 0. *Fix:* `flags.includes_not_significant`.
* **L5 — Licence review only at table level.** OECD How's Life (`oecd.hsl_mind_*`, 73,953 cells, Gallup upstream for
  some measures) carries `licence_review` in the table rights but not per cell; the rule asks for licence per value.
* **L6 — Report drift.** `v4_v4bio.md` still contains `LIVE_RESULTS_PLACEHOLDER` (HGNC was loaded live: 424,384
  statements; no GBIF/COL/UniProt/Ensembl/NCBI statement is in the dev DB); `v4_v4health.md` says WHO GHE was "not run
  live" but `atlas.who.ghe` (620,004 rows) was registered at 14:16; `v6_plan.md` says 575 questions / 2,243 forecasts,
  the registry holds 586 / 2,287.
* **L7 — Edges to entities that do not exist.** `hgnc:in_taxon`, `hgnc:located_on_chromosome`, `hgnc:encodes` (110,931
  statements → `ncbitaxon:9606`, `chromosome:9606:*`, `uniprot:*`), `globi:resolved_within` (141 → `gbif:*`),
  `gleif:gem_entity` (290 → `gem:*`), `mondo:in_taxon` 1,847: family pages show unlabeled nodes. Expected until V4BIO /
  GEM loads; say so in the API (`label_loaded: false` already exists for causal claims).
* **L8 — REVA-made identities keyed by a source name.** `cbattr:extreme:<sha256(Name)>` groups Carbon Brief rows by the
  exact `Name` string (948 entities; two events with one name merge, one event spelled twice splits);
  `resolve_realm:<REALM>` uses the realm name (8). Both are described as source-named. *Fix:* label the grouping rule
  in the entity description ("grouped by identical Carbon Brief Name").
* **L9 — Day precision overstated.** `climind.sea_level` NOAA STAR rows (~10-day altimetry cycles) are 1-day periods
  computed from a decimal year (1,657 cells; the formula is in `flags`).

## 5. Name-based mappings found (inventory)

None is a similarity or fuzzy match. All are exact strings or curator assignments; each should stay listed as an
owner decision.

| # | Where | What is mapped by name | Size | Comment |
|---|---|---|---:|---|
| 1 | `atlas_usgs_facilities.py`, scheme `usgs_name` (A-RESOURCES + `geo_aliases/v4energy.yaml`) | USGS country short names → ISO3 → QID (`usgs_myb:country`) | 12,027 | 0 disagreements with the ISO3 prefix of the USGS Facility ID; use the ID prefix, which is the source's own identifier |
| 2 | `geo_aliases/v4mind.yaml`, scheme `hofstede` | Hofstede country labels → ISO3 | 101 aliases, 666 cells | documented by V4MIND |
| 3 | `registry/effects/literature/*.yaml` `scope.countries` | curator-assigned ISO3 from printed country names / column heads | 72 estimates | documented (owner decision 2 of PRED-EFFECT); printed name kept in `scope.sample_note` |
| 4 | `health_obo_common.CAUSAL_MARKERS` | causal status of an ontology relation from its label | 61 claims (RO:0007001) | M9 |
| 5 | `registry/kg/queries/health/wikidata/essential_medicines` | EML anchor item selected by exact English label | 0 (not harvested) | pin the QID before the first harvest |
| 6 | `registry/entities/country_qids.json` (V3, used by every V4 country edge) | CHI and XKX resolved by enwiki sitelink title | 2 countries | other 247 by P298 |
| 7 | `atlas_attribution.ids_by_name`, `resolve_realm:` | event / realm identity from the source's own name string | 948 + 8 entities | L8 (within one source, not a cross-source merge) |

Checked and **not** name-based: GHCN ADM1 (ISO 3166-2 code of the covering polygon only), GHCN P17 (FIPS → ISO via
GeoNames countryInfo), GLEIF parents and countries (LEI, ISO codes), V4LINK bridges (Wikidata identifier properties),
GloBI partners (ids GloBI returns), ClinGen / Mondo / DOID / UBERON ids, NUTS / OECD regions (codes), element symbols
(IUPAC identifiers), effect-estimate → engine matching (`cause_id` / curated `atlas_concepts` links).

## 6. Checks that passed (worth keeping)

* Provenance: 418/418 cited raw objects present, sha256 verified (44 through the DISKZ decoder); every sampled
  statement, claim and cell names its raw sha256 and a selector that resolves.
* Chemistry: 35/35 mass shares recomputed to ≤ 5·10⁻⁶ from the archived periodic table (hydrates included); every
  share is flagged `computed_by: reva` with the table's sha256.
* GHCN ADM1: 25/25 points re-placed in the same polygon; no second covering polygon; `iso_3166_2` unique.
* Effects: 72/72 quotes re-found on the declared page; 0 sign / interval / unit-measure violations; uncertainty never
  invented (`not_reported` 11, `partial` 22, `reported` 39 + 5 runs); all publication rights flagged `restricted`.
* Registry: 4/4 seals verify; 0 question whose period ended before its seal; 0 target cell observed in the pinned
  version (EGY CPI 2026 was correctly skipped as "already observed"); 0 forecast trained past the last observed period;
  persistence points equal the last observed value (14 differ only by the documented 10-significant-digit rounding).
* Engine: every quantified variable traces to one estimate key; alternatives are listed "jamais additionnée ni
  moyennée"; degenerate draws are flagged `uncertainty_complete: false`; all results labelled « scénario non validé —
  pas une probabilité ».
* Projections: STEO observed/forecast split exact at `last_historical_period`; 704,052 CMIP6 cells and every WEO/SPF
  forecast cell are excluded from observation answers.
* Missing never zero: 0 null-value rows without a reason in 14 tables; all sampled zeros (STEO, WHO GHE, TiVA, IMF CPI,
  CRU, WEO, GPPD generation 4,683) are zeros in the source.

## 7. Proposed order of fixes

1. H1, H2, M1, M9 (V4HEALTH): reload with the source relation kept, negative ClinGen classes split, `geno:` out of
   the claims, causal classification by RO id only; then M2 (lead: grade).
2. M3, M5, L3 (PRED-EVENTSTUDY / PRED-EFFECT) and M4 (PRED-ENGINE): effect variables and horizon conventions, variant
   preference; re-run the five reference decisions.
3. M6, M7 (V4ENERGY, A-IMF owner / PRED-REGISTRY): projection flag, fiscal mapping, resolution notes on 5 questions.
4. M8, L1 (V4INDUSTRY), L2 (V4CORE API), the Low items as convenient.
