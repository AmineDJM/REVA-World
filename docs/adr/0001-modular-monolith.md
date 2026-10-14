# ADR 0001: Modular monolith with two processes

Status: accepted (2026-10-14)

## Context
V1 serves one user, small data and one scenario family. Distributed services
would add network failure modes, deployment work and cost without a measured need.

## Decision
One Python package (`reva`) with package-level ownership boundaries enforced by
import-linter, deployed as two processes from one image: the API/UI (one Uvicorn
worker) and one durable worker. They share PostgreSQL and the object store.

## Consequences
* Domain packages talk through typed ports; only adapters know physical layouts.
* CPU-heavy work goes through the queue, never the API process.
* Scaling triggers (§28) are measured queue delay, memory or DB contention, not
  observation counts; adapters can be replaced behind stable ports.
