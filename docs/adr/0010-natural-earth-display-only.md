# ADR 0010: Natural Earth geometry is display-only

Status: accepted (2026-10-14)

## Context
A map is required; authoritative jurisdiction data is not available in V1.

## Decision
Natural Earth 1:110m admin-0 (public domain, release and hash pinned) is stored as
a display geometry support. It is never used as statistical territory, for
area/aggregation weights or to settle disputed borders. Source-specific supports
(e.g. Comtrade reporter territory) stay distinct.

## Consequences
* The UI labels the map as display geometry; overseas territories may differ from
  statistical definitions.
