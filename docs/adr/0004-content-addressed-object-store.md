# ADR 0004: Content-addressed object store, objects before metadata

Status: accepted (2026-10-14)

## Context
Raw bytes, estimate tables and manifests must be immutable and verifiable; the
database and object store cannot share a transaction.

## Decision
Keys are `reva/<namespace>/sha256/<aa>/<bb>/<sha256>`, written with conditional
create and verified size/hash; metadata becomes visible only in the PostgreSQL
transaction that references the object. Orphans are collected after 7 days and a
second reference scan. Filesystem adapter locally, S3-compatible in production;
never filesystem in production.

## Consequences
* A crash between upload and commit leaves an invisible orphan, never a partial
  release.
* The worker's S3 credential needs `DeleteObject` for GC; bucket versioning in a
  separate admin account mitigates misuse.
* Backups record a sha256 inventory and check every artifact location.
