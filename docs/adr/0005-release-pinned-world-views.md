# ADR 0005: Release-pinned world views

Status: accepted (2026-10-14)

## Context
A mutable "current facts" table would hide which method, inputs and cutoff
produced a displayed value.

## Decision
World read APIs are projections of an immutable belief release, published with a
publication sequence assigned under an advisory lock. Every response states the
release/context and cutoff. Scenarios pin a baseline release plus model and
ontology manifests.

## Consequences
* As-of answers are reproducible (and survive backup/restore, as tested).
* New data never changes an old release; it produces a new one.
