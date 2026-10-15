# Independent numerical audit — crude border-price demo (live, 2026-10-15)

Scope: the headline numbers in `var/acceptance/demo_live.json` (run started 2026-10-15T00:19:35Z,
git `9ecd563c`, dirty tree): step 5 (L1, +25 % import and export border prices, FRA/CHN/USA,
reference year 2023), step 6 (L2 elasticity sensitivity), and step N (all four countries → DZA blocker).

Method: no REVA code path was used for the computation. I read the stated formula
(`src/reva/models/economic/crude_exposure.py` docstring, `docs/methodology.md` §2,
`contracts/scenario.example.json`, `src/reva/cli/demo.py` `SCENARIOS`/`build_spec`), fetched the raw
provider responses myself, and recomputed with plain Python: L1 in exact rational arithmetic
(`fractions.Fraction` on the JSON number text, never via float), L2 through an algebraic path
(1.25^0.5 = √1.25, 1.25^0.75 = √1.25·⁴√1.25, 1.25^1.25 = 1.25·⁴√1.25 with `Decimal.sqrt` at 80
digits), which is independent of REVA's `exp((1+ε)·ln p)` route.

Raw data (scratch, not committed):
`/tmp/claude-0/-home-user-REVA-World/a82f628c-2790-54df-b0b4-28930bdc7a97/scratchpad/num/raw/`
(16 Comtrade responses + 1 WDI response, each with the request URL and UTC retrieval time),
script `.../num/code/audit.py`, output `.../num/audit_output.txt`.

- UN Comtrade public preview, `https://comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=…&period=…&partnerCode=0&cmdCode=2709&flowCode=M|X`,
  one period per request, 4 s spacing, fetched 2026-10-15T00:25:56Z–00:27:15Z (≈3–6 min after REVA's
  acquisition). I did **not** add REVA's extra filters (`partner2Code=0&customsCode=C00&motCode=0`);
  every non-empty response still contained exactly one row, and that row has exactly those dimension
  values, so the two request shapes select the same record.
- World Bank WDI `NY.GDP.MKTP.CD`, FRA;CHN;USA;DZA, 2023–2024 (`lastupdated` 2026-10-08), fetched 00:27:26Z.

## 1. Formula used (as stated by REVA, reimplemented)

L1, multipliers p_M = p_X = 1.25:
ΔM = M·(p_M − 1), ΔX = X·(p_X − 1), Δnet = ΔM − ΔX, exposure % = 100·Δnet / G.
M = HS 2709 imports from World (`primaryValue`), X = HS 2709 exports to World (`primaryValue`),
G = nominal GDP current USD, all for 2023. All Comtrade values are plain USD (no thousands
scaling); WDI GDP is plain current USD.

L2: ΔM = M·(p_M^(1+ε_M) − 1), ΔX = X·(p_X^(1+ε_X) − 1), grid from `build_spec`:
ε_M ∈ {−0.5, −0.25, 0} × ε_X ∈ {0, 0.25} (6 points, includes (0,0) = L1).

## 2. Inputs: raw provider rows vs REVA's pinned inputs

| Country | Flow | primaryValue (USD) | cifvalue | fobvalue | legacyEstimationFlag | REVA input | Equal |
|---|---|---|---|---|---|---|---|
| FRA | M | 31,530,866,245.503 | = primary | null | 0 | 31530866245.503 | exact |
| FRA | X | 43,219,393.04 | null | = primary | 0 | 43219393.04 | exact |
| CHN | M | 337,644,667,180 | = primary | null | **6** | 337644667180 | exact |
| CHN | X | 843,802,125 | null | = primary | 0 | 843802125 | exact |
| USA | M | 172,426,221,223 | = primary | **165,333,122,697** | 0 | 172426221223 | exact |
| USA | X | 117,160,520,564 | null | = primary | 0 | 117160520564 | exact |
| FRA | G (WDI) | 3,056,250,648,138.29 | | | | 3056250648138.29 | exact |
| CHN | G (WDI) | 18,270,356,654,533.2 | | | | 18270356654533.2 | exact |
| USA | G (WDI) | 27,811,517,000,000 | | | | 27811517000000 | exact |

All rows: `classificationCode` H6, `isOriginalClassification` true, `isReported` false,
`isAggregate` true (true of every World-total row, hence uninformative). No revision occurred
between REVA's acquisition and my fetch. Value field choice: REVA uses `primaryValue`, which for
imports equals `cifvalue` (CIF) and for exports equals `fobvalue` (FOB) in every row, so REVA's
CIF/FOB labels are correct.

## 3. L1 results: REVA vs independent

| Country | ΔM (USD) | ΔX (USD) | Δnet (USD) | Exposure % GDP — REVA | Independent | |diff| |
|---|---|---|---|---|---|---|
| FRA | 7,882,716,561.37575 | 10,804,848.26 | 7,871,911,713.11575 | 0.2575676088 | 0.2575676088 | 2.0e−51 |
| CHN | 84,411,166,795 | 210,950,531.25 | 84,200,216,263.75 | 0.4608569929 | 0.4608569929 | 3.2e−51 |
| USA | 43,106,555,305.75 | 29,290,130,141 | 13,816,425,164.75 | 0.0496787901 | 0.0496787901 | 2.0e−52 |

ΔM, ΔX and Δnet are **exactly equal** (rational equality) to REVA's strings for all three countries.
The exposure % differs only in the 51st–52nd significant digit: REVA rounds the quotient to its
50-digit Decimal context, while my value is the exact rational. Reported precision (4 decimals:
0.2576 / 0.4609 / 0.0497) agrees.

Valuation sensitivity (not an error, a convention): USA is the only row with both bases. With imports
at FOB, ΔM = 41.33 bn, Δnet = 12.04 bn and exposure = **0.0433 %** instead of 0.0497 % (−13 %).
FRA and CHN imports have no `fobvalue`, so no FOB alternative exists for them. REVA's explanation
block says that applying the multiplier to a CIF value includes freight/insurance in the shock;
that is accurate.

## 4. L2 sensitivity range: REVA vs independent

| Country | Range % GDP — REVA | Independent | max |diff| | argmin (ε_M, ε_X) | argmax |
|---|---|---|---|---|---|
| FRA | [0.1213188986, 0.2575676088] | [0.1213188986, 0.2575676088] | 4.3e−50 | (−0.5, 0.25) | (0, 0) = L1 |
| CHN | [0.2166464759, 0.4608569929] | [0.2166464759, 0.4608569929] | 6.2e−50 | (−0.5, 0.25) | (0, 0) = L1 |
| USA | [−0.0623483945, 0.0496787901] | [−0.0623483945, 0.0496787901] | 3.7e−50 | (−0.5, 0.25) | (0, 0) = L1 |

USA grid, independent (Δnet in USD bn, exposure % GDP):

| ε_M \ ε_X | 0 | 0.25 |
|---|---|---|
| −0.5 | −8.938 / −0.032138 | −17.340 / −0.062348 |
| −0.25 | +2.122 / +0.007630 | −6.280 / −0.022581 |
| 0 | +13.816 / +0.049679 | +5.414 / +0.019468 |

Range-row agreement is at the 1e−50 level (the residual comes from REVA's ln/exp vs my sqrt route
at finite precision). REVA's USA ΔM/ΔX/Δnet range bounds (20,352,154,656.02 / 37,692,189,003.64 /
−17,340,034,347.62) match the grid points above.

Sign conventions check:
- Δnet = ΔM − ΔX; positive = higher net external payment for crude. USA L1 is positive (imports
  172.4 bn > exports 117.2 bn by value); the L2 lower bound is negative = net receipt exposure,
  which REVA labels as such. Consistent.
- ε_M < 0 lowers the import quantity as price rises; ε_X > 0 raises the export quantity. Both push
  Δnet down, so with this grid the minimum is always (−0.5, 0.25) and the maximum is always the
  fixed-quantity point (0, 0). The range is therefore **one-sided by construction**: L1 is its
  upper end, not an interior point. This is correct for the stated grid. Present it as "the effect
  of assumed quantity responses that lower the bill", not as symmetric uncertainty around L1.
- Probability level: all 12 range rows (3 countries × 4 outputs) carry `kind = sensitivity_range`
  and `level = null`; the L1 rows carry `uncertainty.status = unquantified` with null bounds.
  No coverage level is attached. Correct.

## 5. Algeria (DZA)

Raw API, reporter 12, partner 0, HS 2709:

| Year | M | X |
|---|---|---|
| 2020 | 0 rows (`count` 0, `error` "") | 0 rows |
| 2021 | 0 rows | 0 rows |
| 2022 | 0 rows | 0 rows |
| 2023 | 0 rows | 0 rows |
| 2024 | 1 row: 5,564 USD (CIF), H4 | 1 row: 14,183,287,575.254 USD (FOB), H4 |

The raw 2023 import response I fetched is byte-identical to REVA's archived artifact (SHA-256
`d5b6bec2ca12c01d3de898153073fea5196e40f417bf4617352282e5478ee1bd`, the `raw_selector` of
`dza_import_2023` in step 2).

REVA's handling is correct: step 2 stores DZA 2023 imports as `{"kind":"missing","reason":"unavailable"}`
with `provider_returned_no_rows: true`. Step 12's gap list shows DZA 2020–2023 as `typed_missing` with
"never impute zero". In step N the all-four run is rejected with a 422 `missing_input` naming only DZA
(M and X; DZA GDP 2023 = 247.9 bn is present and correctly not listed). The worker run ends
`missing_input` with 0 result rows, so nothing is imputed and nothing is zero-filled.

The 2024 rows: both have `legacyEstimationFlag` 6, `isQtyEstimated` and `isNetWgtEstimated` true,
`isReported` false. Exports of 14.18 bn USD over 26.06 Mt (≈ 74 USD/bbl at 7.33 bbl/t) are a
plausible size for Algerian crude exports. Imports of 5,564 USD are negligible, which fits a net
exporter. A 2024 DZA run would therefore be computable but outside the demo's 2023 reference year.

## 6. Finding: what `legacyEstimationFlag` means

In the current responses, `legacyEstimationFlag` = 6 occurs exactly when `isQtyEstimated` and
`isNetWgtEstimated` are both true (CHN M 2023, DZA M/X 2024), and = 0 when neither is. This matches
the UNSD coding: 0 = no estimation, 2 = quantity estimated, 4 = net weight estimated, 6 = both
([UNSD trade data processing method, 2019](https://comtradeapi.un.org/files/v1/app/wiki/UNSD_Method_trade_data_processing_v6-17_Jun_2019.pdf)).
The flag therefore concerns **quantity/weight**, not the USD value.

REVA's connector sets `provider_estimation_flagged = (legacyEstimationFlag != 0)`. The model's
`_input_flags` then attaches the note "provider estimation flagged …: not a direct customs
observation" to CHN 2023 imports. That overstates the issue: the USD value used by the model is
not marked as estimated, and only the tonnage is. The error is conservative and does not change any
number, but the label is inaccurate. Suggested fix (owners: CVAL for `comtrade.py`, agent C/SIMFIX
for the note text): call it `provider_quantity_estimated` or word the note as "quantity and
net-weight estimated by UNSD; value as reported". I edited nothing.

The brief's wording "2024 … flagged estimates" for DZA should be read the same way: the
quantities are estimated. The values carry `isReported=false`, which holds for every World-total
row, FRA and USA included.

## 7. External plausibility check

| Item | REVA/Comtrade | External reference | Assessment |
|---|---|---|---|
| CHN crude imports 2023 | 337.6 bn USD CIF; netWgt 554.0 Mt (UNSD-estimated) | GACC: 563.99 Mt (11.28 mb/d) ([PGJ/Reuters](https://pgjonline.com/news/2024/january/chinas-2023-crude-oil-imports-hit-record-as-fuel-demand-recovers)) | 337.6 bn / (563.99 Mt × 7.33 bbl/t) ≈ 81.7 USD/bbl vs Brent 2023 average ≈ 82 USD/bbl. Consistent. The Comtrade tonnage is 1.8 % below GACC because it is estimated. |
| USA crude imports 2023 | 172.4 bn USD CIF (165.3 bn FOB) | EIA: imports from Canada 93 bn USD, Canada = 60 % of import volume ([EIA](https://www.eia.gov/todayinenergy/detail.php?id=62465)) | Canada at 54 % of total value with 60 % of volume fits discounted heavy Canadian crude. Consistent. |
| USA crude exports 2023 | 117.2 bn USD FOB | EIA: record 4.1 mb/d ([EIA](https://www.eia.gov/todayinenergy/detail.php?id=61584)) | 117.2 bn / (4.1 mb/d × 365) ≈ 78 USD/bbl vs WTI 2023 average ≈ 78. Consistent. The Comtrade netWgt (235.7 Mt) implies an odd ≈ 6.4 bbl/t for light US crude; this is a quantity-conversion artefact and the model does not use it. |
| FRA crude imports 2023 | 31.53 bn USD CIF (no tonnage in the row) | SDES via INSEE: 45.5 Mt ([INSEE 2119697](https://www.insee.fr/fr/statistiques/2119697)); SDES crude bill 27.3 bn EUR ([bilan énergétique 2023](https://www.connaissancedesenergies.org/sites/connaissancedesenergies.org/files/pdf-actualites/bilan-energetique-france-2023.pdf)) | 27.3 bn EUR × 1.0813 (2023 average USD/EUR) ≈ 29.5 bn USD, so Comtrade is **≈ 7 % higher**. The implied unit value, 31.53 bn / (45.5 Mt × 7.33) ≈ 94.5 USD/bbl, is ≈ 15 % above Brent. Same order of magnitude, so not implausible, but this is the least tight of the cross-checks. Likely causes: customs (general-trade, CIF) vs energy-balance scope and valuation; this was not resolved. Using the SDES level would give 0.241 % instead of 0.258 %. |
| GDP 2023 | FRA 3.056 tn, CHN 18.27 tn, USA 27.81 tn | WDI (fetched independently) | Identical to the last digit. |

The ordering CHN (0.46 %) > FRA (0.26 %) > USA (0.05 %) fits the structure: China is the largest
importer relative to GDP and nearly has no exports. France imports almost all of its crude. The US
nets 172 bn of imports against 117 bn of exports on a 27.8 tn economy.

## 8. Verdict

**Pass.** Every headline number in the live demo reproduces independently from raw provider data:
L1 ΔM/ΔX/Δnet exactly, exposure % to about 1e−50, and the L2 ranges for all three countries to
about 1e−50, including USA −0.0623 % to +0.0497 %. Inputs match the providers exactly with no
revision, and the CIF/FOB labels match the raw `cifvalue`/`fobvalue` fields. DZA 2020–2023 is
confirmed empty at source and REVA reports it as typed missing input, never as zero. The sensitivity
range carries no probability level.

Caveats, none of which changes a number:
1. `legacyEstimationFlag` 6 marks estimated quantity/weight, not an estimated value. REVA's "not a
   direct customs observation" note on CHN imports overstates this (§6).
2. The L2 grid makes L1 the upper bound of every range by construction (§4). This needs wording, not
   a fix.
3. Valuation convention matters: USA imports at FOB would give 0.0433 % instead of 0.0497 %.
4. The French Comtrade import value sits ≈ 7 % above the SDES crude bill (§7). It is plausible but
   unexplained here.
