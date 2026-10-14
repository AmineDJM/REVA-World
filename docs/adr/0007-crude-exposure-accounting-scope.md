# ADR 0007: Crude exposure model is a one-period accounting identity

Status: accepted (2026-10-14)

## Context
The MVP needs one meaningful scenario without claiming unsupported economics.

## Decision
`economic.crude_exposure` 1.0.0: L1 fixed-quantity border-value change and
exposure as % of same-year nominal GDP; L2 explicit elasticity sensitivity grid
(ε = 0 equals L1 exactly); L0 exact cache; L3/L4 unavailable. One annual period,
same reference year and explicit valuation bases; missing inputs block the run
(DZA 2023 has no Comtrade preview rows, so the default scenario uses FRA/CHN/USA).

## Consequences
* Dynamic GDP, welfare, inflation or fiscal questions return capability gaps.
* Exposure numbers are assumption-conditioned, never forecasts.
