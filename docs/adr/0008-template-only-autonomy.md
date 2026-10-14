# ADR 0008: Template-only autonomy

Status: accepted (2026-10-14)

## Context
Autonomous discovery is required (Eurostat `demo_pjan`), but generated code
cannot be trusted and no isolated runner is available.

## Decision
Autonomy generates declarative manifests for pre-audited templates only, validates
them on bounded samples, and promotes only within signed policy scope (tier T1).
New semantics, parsers or models are proposals for human review (T2/T3). The agent
cannot edit policies, allowlists, budgets or gate code; a kill switch disables it.
The sandbox interface returns a typed blocked result unless an external isolated
runner is configured.

## Consequences
* No generated code runs in the production worker.
* Discovery can legitimately end in "needs review" or "rejected".
