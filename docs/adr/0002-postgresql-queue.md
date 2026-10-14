# ADR 0002: PostgreSQL queue and scheduler

Status: accepted (2026-10-14)

## Context
Jobs (ingestion, publication, simulation, replay, discovery, GC) need durability,
idempotency and fencing; volumes are small.

## Decision
`ops.job` with `FOR UPDATE SKIP LOCKED` claims, leases with heartbeats, a
monotonically increasing fencing token checked by `ops.assert_lease` /
`ops.complete_job`, idempotency keys, bounded retries with backoff, dead-lettering,
and a source-aware scheduler that creates exactly one job per schedule slot. No
Redis, Celery or Kafka.

## Consequences
* At-least-once execution plus idempotent publication gives effectively-once
  visible results; a holder that lost its lease cannot commit.
* Replacing the queue adapter is possible once a bottleneck is measured.
