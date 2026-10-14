# ADR 0011: Population recipe instead of a materialised synthetic universe

Status: accepted (2026-10-14)

## Context
A permanent synthetic humanity would be expensive and would invent detail the
data cannot identify.

## Decision
The canonical object is a recipe (pinned source cells, constraints, seed, supported
attributes, diagnostics). V1: France age × recorded sex from Eurostat `demo_pjan`
(1 January), disjoint leaf partition only, unknown/top-coded ages explicit;
≤ 100,000 weighted records materialised to Parquet on demand with exact rational
weights; coarsen/refine conserve weight and refuse unsupported dimensions.

## Consequences
* No income, household, health or other attribute can be simulated from it.
* No synthetic record is linked to a real person.
