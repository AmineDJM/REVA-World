# ADR 0003: Immutable evidence with explicit revisions

Status: accepted (2026-10-14)

## Context
Sources revise and correct values; replay must answer what was known at a cutoff.

## Decision
Observations, acquisitions, releases, model versions and scenarios are
append-only (triggers reject UPDATE/DELETE/TRUNCATE, also for the owner). A
logical key (dataset, series dimensions, valid support) excludes retrieval time and
value; a changed value appends a revision with `supersedes_id`; an identical
consecutive poll creates none; retractions are records. Source publication time is
never inferred from the reference period. Legally required deletion of restricted
data has a reviewed-ticket path; V1 stores none.

## Consequences
* As-of queries filter by `recorded_at` before revision selection.
* Corrections never alter an earlier as-of answer.
