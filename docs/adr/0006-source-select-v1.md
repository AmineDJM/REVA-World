# ADR 0006: `source_select_v1` as the initial belief method

Status: accepted (2026-10-14)

## Context
A full Bayesian world posterior is neither feasible nor honest in V1.

## Decision
Publish `selected_report` estimates: eligible observations at the cutoff → latest
eligible revision per lineage → published per-estimand source preference → keep
the source's uncertainty status, alternatives and discrepancy flags; never average
incomparable definitions. Method key `belief.source_select` 1.0.0, policy
`selection-1.0.0`.

## Consequences
* Outputs are selected reports, not calibrated posteriors; the UI says so.
* Blockwise assimilation can be added later as a new method version.
