"""Automated, read-only data-quality checks (V6 QA-DATA): ``reva qa run [--domain ...]``.

Seven domains, each a set of checks over the dev database and the object store:

* ``cross_source`` — the same quantity from different sources (headline concepts, catalog concepts, declared pairs),
  flagged when they diverge beyond the tolerances of ``registry/qa/checks.yaml`` (report only, never "fixed");
* ``units`` — bounded shares outside their range, negative counts, per-capita × population vs totals;
* ``time`` — future-dated observations, projections not flagged as projections, period-precision mismatches;
* ``graph`` — dangling references by predicate, cycles in part_of / is_a, located_in chains reaching a country;
* ``licence`` — licence coverage of atlas tables and of the raw objects behind KG statements and causal claims;
* ``provenance`` — rows without a raw object; objects missing or corrupt through the store API (decoded,
  zstd-transparent, re-hashed);
* ``causal`` — ungraded claims, endpoints that are not entities, causal wording in non-causal layers.

Every check reads; nothing is written to the database or the object store. Postgres access runs in READ ONLY
transactions. The same checks run on a fixture backend (DuckDB + in-memory objects) in the tests.
"""

from reva.qa.report import CheckResult, QaReport

__all__ = ["CheckResult", "QaReport"]
