# ADR 0009: Separate runtime roles and scrubbed process startup

Status: accepted (2026-10-14)

## Context
The Blueprint generates role passwords on the API service and gives the API the
database owner URL for pre-deploy. A runtime process holding the owner credential
could alter schema or history.

## Decision
* Logins `reva_api`, `reva_worker`, `reva_sim` (NOINHERIT, no special
  attributes, owning nothing) with per-role grants; migration owner only in
  `scripts/bootstrap_runtime.py` (advisory lock, capability checks, Alembic, role
  passwords, ownership verification, JSON report without secrets).
* `scripts/start_api.py` / `start_worker.py` validate configuration, build an
  **allowlisted** child environment (owner URL, `DATABASE_URL` and other
  components' secrets removed; unknown secret-looking variables dropped; thread
  variables set from the explicit budget) and `exec` the process. The worker
  refuses an owner URL in production and waits with bounded retry for the schema.
* DSNs are built with `URL.create`, never string concatenation.
* Tests assert the environment the child actually receives.

## Consequences
* A compromised runtime process cannot use DDL or the owner credential.
* Managed PostgreSQL owners are not superusers: role provisioning must only set
  `LOGIN/NOINHERIT/CONNECTION LIMIT/PASSWORD` and verify other attributes
  (open change request; see deployment.md).
