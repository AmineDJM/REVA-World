# V3 A-FISCAL — taxes, public and social spending, pensions, learning outcomes, BIS finance

Agent A-FISCAL, 2026-10-16. Three atlas sources, thirteen atlas tables, three catalog files (110 provider-declared
concepts). Tested offline (recorded fixtures) and ingested once live through the single atlas entry point
(`reva.atlas.ingest.run_atlas_ingest`, budget-metered) into throwaway databases (`reva_afiscal_live_*`, dropped
afterwards with their object stores). The dev database `reva` was never touched.

## 1. What was built

| atlas source (connector) | source key / terms | module | atlas tables | requests / bytes per full run (live) |
|---|---|---|---|---:|
| `atlas.oecd.fiscal` | `oecd.sdmx` (existing key, shared with A-ILO/A-TRADE/A-SOCIETY) — OECD Terms & Conditions, CC BY 4.0 default | `atlas_oecd_fiscal.py` | `oecd.rs_global`, `oecd.cts_cit`, `oecd.tw_comp`, `oecd.socx_agg`, `oecd.pag`, `oecd.gov_cofog` | 8 / 182.3 MB |
| `atlas.oecd.skills` | `oecd.sdmx` | `atlas_pisa.py` | `oecd.hsl_skills` | 1 / 0.7 MB |
| `atlas.bis` | `bis.stats` (**new**, `registry/sources/bis_stats.yaml`) — BIS terms of permitted use (citation) | `atlas_bis.py` | `bis.cbpol`, `bis.tc`, `bis.spp`, `bis.dsr`, `bis.eer`, `bis.credit_gap` | 6 / 126.5 MB |

Shared plumbing: `src/reva/adapters/connectors/atlas_fiscal_common.py` (labelled SDMX-CSV reader, generic
flow → atlas frame, periods, ingest loop). Every module declares `atlas_source()` + `async atlas_ingest(run)`,
fetches only through `run.get` (GET), archives each response with `run.archive` before parsing and registers with
`run.register`; no HTTP client is built (invariant 30 scan passes). No personal data in requests
(`REVA_HTTP_CONTACT_EMAIL` empty, verified in the live script).

Other files: catalogs `registry/atlas/catalog/fiscal.yaml` (76), `finance.yaml` (21), `education_outcomes.yaml` (13);
budget request `registry/atlas/budget_requests/a_fiscal.yaml`; fixtures `tests/fixtures/source/atlas.oecd.fiscal/`
(0.3 MB), `atlas.oecd.skills/` (24 KB), `atlas.bis/` (0.22 MB) — real provider responses to restricted keys and
`startPeriod` (2016 OECD, 2023 BIS); tests `tests/unit/test_atlas_fiscal.py` (23) and
`tests/integration/test_atlas_fiscal_ingest.py` (5: fixture ingest of all three sources → registered tables,
idempotent re-run, single-dataset run, live run without budget refused before any request). No geo-alias file was
needed: OECD uses ISO3, BIS ISO 3166 alpha-2; every non-ISO code is an aggregate (kept unmapped, listed below).

Run: `uv run python -m reva.atlas.ingest atlas.oecd.fiscal | atlas.oecd.skills | atlas.bis` (`--dataset rs_global`
etc.; `--fixture` offline). Live runs need the budget keys merged (`oecd.sdmx` 76/day, `bis.stats` 14/day:
`python -m reva.atlas.budget_merge` plans both).

## 2. How the data is fetched and parsed

* **OECD** — SDMX REST `https://sdmx.oecd.org/public/rest/data/<agency>,<flow>,/<key>?format=csvfilewithlabels`
  (latest dataflow version; a label column follows each code column, so no structure request). The CSV header must
  show the expected dimension order (the SDMX keys depend on it) and every row must belong to the flow, else the
  table is refused with a typed `FiscalParseError`.
* **BIS** — SDMX REST `https://stats.bis.org/api/v1/data/<flow>/<key>` with
  `Accept: application/vnd.sdmx.data+csv;version=2.0.0;labels=both` (coded cells `"<code>: <label>"`, the same
  layout as the Data Portal "flat" bulk CSV). Keys restrict each request to one frequency: the bulk ZIPs also carry
  daily series (policy-rate flat CSV = 476 MB inflated). BIS writes series-level attributes (unit, collection,
  title) on separate rows without period/value; those are merged into the series' observation rows.
* **Cells**: `indicator` = measure code (OECD) or table prefix (BIS) then `|<dimension>=<code>` for the folded
  dimensions (`_Z` "not applicable" left out), e.g. `TAX_REV|sector=S13|standard_revenue=T_5111|unit_measure=PT_B1GQ`
  (VAT, % of GDP), `TC|tc_borrowers=P|tc_lenders=A|valuation=M|unit_type=770|tc_adjust=A`. `value` = provider number
  verbatim (strict decimal; reported zeros kept). `unit` = provider `UNIT_MEASURE` label; `UNIT_MULT` ≠ 0 is appended
  as `x10^n` and kept in `flags.unit_mult(_label)` — values are never rescaled. `flags.currency` = OECD `CURRENCY`.
* **Flags (verbatim)**: `obs_status` + `obs_status_label` whenever the status is not `A` (Normal value): OECD `P`
  Provisional (COFOG 1,102 cells), `E` Estimated (44), `B` Time series break (36 COFOG; BIS TC 224), `I` Value imputed
  by a receiving agency (CIT 26). BIS `obs_conf` when not `F` (Free), `pre_break_value`, `collection` (E end of
  period / A average), series `title_ts`; policy rates also carry the BIS `compilation` text (which rate is used in
  which period) and `source_ref`.
* **Missing**: a row without a value is **not a cell** (never a zero); counts by status are in the report and the
  coverage note (SOCX: `M` "data cannot exist" 26,558, `L` "not collected" 4,691, `K` "included in another
  category" 332, empty `A` 9,890; Revenue Statistics: 20,708 empty rows; BIS policy rates: 53 `M` rows).
* **Periods**: OECD annual = calendar year. BIS policy rates and EER are **monthly**, credit, property prices, DSR and
  credit gaps **quarterly**: kept at the provider's frequency (no averaging); the atlas year view does not attach
  sub-annual cells, the per-source series show them (catalog notes say so).

## 3. Live run results (2026-10-16)

| table | cells | indicators | geographies (mapped countries) | periods | unmapped (kept) |
|---|---:|---:|---|---|---|
| `oecd.rs_global` Global Revenue Statistics | 249,074 | 141 | 146 (**142**) | 1990–2024 | A9 LAC, F Africa, SO Asia-Pacific, OECD_REP |
| `oecd.cts_cit` statutory CIT rates | 13,752 | 10 | 146 (**146**, incl. GGY, JEY, IMN, CUW, BMU, CYM…) | 2000–2026 | – |
| `oecd.tw_comp` Taxing Wages | 123,968 | 120 | 40 (38) | 2000–2025 | EU22OECD, OECD_REP |
| `oecd.socx_agg` SOCX % GDP | 35,706 | 44 | 43 (42) | 1980–2024 | OECD |
| `oecd.pag` Pensions at a Glance | 17,960 | 69 | 54 (51: OECD + G20 incl. ARG, BRA, CHN, IDN, IND, RUS, SAU, ZAF) | 1950–2084 (projections) | EU27, EU28, OECD_REP |
| `oecd.gov_cofog` expenditure by function | 29,710 | 48 | 40 (37) | 2007–2025 | EUOECD, OECD, OECD_REP |
| `oecd.hsl_skills` PISA + PIAAC means | 2,826 | 43 | 46 (46) | 2006–2023 | – |
| `bis.cbpol` policy rates (monthly) | 25,081 | 1 | 49 (48) | 1945–2026 | XM euro area |
| `bis.tc` credit to non-financial sector (quarterly) | 187,266 | 32 | 48 (43) | 1940–2026 | 4T, 5A, 5R, G2, XM |
| `bis.spp` residential property prices (quarterly) | 35,780 | 4 | 61 (57) | 1927–2026 | 4T, 5R, XM, XW |
| `bis.dsr` debt service ratios (quarterly) | 7,182 | 3 | 32 (32) | 1999–2026 | – |
| `bis.eer` effective exchange rates (monthly) | 90,032 | 4 | 64 (63) | 1964–2026 | XM |
| `bis.credit_gap` credit-to-GDP gaps (quarterly) | 24,620 | 3 | 44 (43) | 1947–2026 | XM |

Total ≈ 0.84 M cells (OECD 0.47 M, BIS 0.37 M). Budget consumed: oecd.sdmx 9 requests / 183 MB, bis.stats 6 / 126.5
MB, 0 refused. Store per full run (raw + Parquet): 186 MB (OECD) + 129 MB (BIS) + 0.8 MB (skills).

Headline coverage: **tax revenue % GDP for 142 countries** (Revenue Statistics: the OECD members plus the Latin America & Caribbean, Africa and Asia-Pacific
Revenue Statistics economies and Bulgaria, Croatia, Liechtenstein, Malta, Moldova, Romania, Ukraine), 1990–2024; by tax
type (income, SSC, payroll, property, goods & services, VAT, excises, customs) for the same countries. Note: % of
total taxation (`PT_OTR_REV_CAT`) is published for only 25 economies in the comparative flow. Statutory CIT rates for
146 jurisdictions; tax wedges for the 38 OECD members; SOCX for 42; pensions for 51; COFOG for 37; PISA means for
~40 (PISA 2006–2022), PIAAC for ~29; BIS series for 32–63 economies.

## 4. Catalog (110 concepts, all `provider_declared: true`, theme set per concept)

* `fiscal` (theme finance): tax-to-GDP total and 12 categories, 5 tax-mix shares, total in national currency / USD,
  tax shares by level of government (4), CIT combined/central/small-business rates, tax wedge and net personal average
  tax rate (single; one-earner couple with 2 children), marginal tax wedge, SOCX (public, mandatory/voluntary private,
  net public, net total, 7 public policy areas), pensions (normal/future retirement ages by sex, effective exit age by
  sex, gross/net replacement rates, public pension spending and its projections, old-age poverty, old-age to
  working-age ratio), COFOG (10 functions × % GDP and % of total expenditure).
* `finance` (theme finance): policy rate, credit (6), property prices (4), DSR (3), EER (4), credit gap (3).
* `education_outcomes` (theme education): PISA math/reading/science (total, girls, boys), low-skill share, PIAAC
  numeracy/literacy, low numeracy.

Single-source `source_order` throughout: no other atlas table publishes the same definitions (WDI `GC.TAX.TOTL.GD.ZS`
is central-government tax revenue from IMF GFS, excluding social contributions; IMF WEO `GGR_NGDP` is total general
government revenue incl. non-tax; CIA Factbook "taxes and other revenues" — different concepts, not listed).

## 5. Licences

* **OECD**: the Data Explorer shows "© OECD Terms & conditions" on every table; the SDMX dataflow metadata
  carries no dataset-level licence annotation (checked on the RSGLOBAL structure; the Data Explorer viewer
  configuration links every table to the OECD Terms & conditions). The OECD
  open-by-default policy makes CC BY 4.0 the default since 2024-07-01; content before that date is under the OECD terms
  "on terms similar to CC BY 4.0"; third-party material excepted. www.oecd.org answers 403 to this container, so the
  terms page text could not be archived (same as A-ILO). Attribution recorded in table rights.
* **BIS**: "Terms of permitted use of BIS statistics" (data.bis.org/help/legal, read 2026-10-16): use unrestricted
  provided the BIS is cited, translations marked unofficial, no misleading use/endorsement, no extra charge in
  commercial products, no warranty. Not a Creative Commons licence — reuse with citation; quoted in
  `registry/sources/bis_stats.yaml` and every table's rights.

## 6. Skipped sources (with reasons)

| source | reason |
|---|---|
| UNU-WIDER Government Revenue Dataset (GRD) | download behind a personal-information form; no explicit open licence found (fair-use policy for publications only). OECD Revenue Statistics already covers 142 countries. |
| IEA TIMSS / PIRLS | IEA data repository licence agreement; reproduction needs IEA permission, commercial redistribution restricted. Not ingested. |
| PISA 2022 full participant tables (81 economies) | OECD PISA result workbooks on www.oecd.org answer 403; the PISA Data Explorer is interactive (no bulk endpoint); computing means from microdata (plausible values, weights) would be REVA computation, not provider values. How's Life covers ~40 economies. World Bank EdStats `LO.PISA.*` (≤ 2018) is already in A-WB's `wb.edstats` table. |
| OECD regional Revenue Statistics non-tax revenue (Africa, Asia-Pacific) | not taken in this pass (country dataflows `DSD_REV_AFRICA@DF_REV<ISO3>` / comparative `DF_RSAFRICA`, `DF_RSASAP`); candidate follow-up for non-tax revenue of ~60 developing economies. |
| IMF GFS / COFOG | A-IMF's domain (GFS_SOO ingested; GFS COFOG on the IMF portal would extend expenditure by function beyond the 37 OECD countries — suggested to A-IMF). |
| BIS daily series, LBS/CBS banking, debt securities, CPI, USD exchange rates | daily data too large for the value; CPI/exchange rates already from IMF; banking statistics are bilateral (LINKS candidate). |

## 7. Gaps and where other sources fill them

* Tax revenue for the ~109 crosswalk entities outside Revenue Statistics (most of the Middle East, Central Asia,
  smaller island states, conflict states): IMF WEO general government revenue (`imf.gg_revenue_gdp`, 190+ economies,
  revenue incl. non-tax), IMF GFS_SOO revenue (142), WDI tax revenue (central government), CIA Factbook. Different
  definitions — shown per source, not blended.
* Social expenditure beyond OECD: ILO World Social Protection data (not ingested; candidate for A-ILO).
* Policy rates beyond BIS's 48 economies: IMF MFS_IR policy-related rate (A-IMF, 82 economies).
* Sub-annual BIS cells do not appear in the atlas year view by design (no averaging); an annual BIS table would need
  a provider-published annual series (BIS publishes none for these flows).

## 8. Notes for owners of shared files

* ATLASCORE/ATLASOPS: merge `a_fiscal.yaml` (`oecd.sdmx` → 76/day, new `bis.stats` 14/day). Until merged,
  `test_budget_keys_are_registered_sources_and_cover_every_connector` lists `bis.stats` (expected).
* Themes: each concept sets `theme:` itself, so `registry/atlas/themes.yaml` needs no rule for the new domains.
* Seed: the three sources are discovered by module name and registered like every atlas source.
