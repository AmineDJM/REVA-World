# ADR 0012: Three explicit replay modes

Status: accepted (2026-10-14)

## Context
Historical evaluation with today's revised data leaks future information.

## Decision
Every replay names one mode: `operational_as_known` (what REVA had published at
the cutoff, exact manifests), `available_then_reconstruction` (archived vintages
with proven availability; records without proof excluded) or
`revised_data_retrospective` (current revised data, temporally split, labelled as
not a real-time backtest). Training, scaling, mappings, model selection and
evidence obey the cutoff; strict mode blocks the six injected leakage cases.

## Consequences
* V1 cannot reconstruct pre-REVA beliefs from current provider responses.
* Scores always carry their replay mode.
