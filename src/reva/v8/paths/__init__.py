"""V8 Liens (agent V8LIENS): how anything connects to anything.

* :mod:`reva.v8.paths.index` — an in-memory adjacency index (CSR, numpy) over every KG statement whose property is
  mapped to a V4 relation family, plus — as distinct edge types — attributed causal claims (tier, grade, source)
  and published effect estimates (size, unit, source), with the declared bridges between them (estimate scope →
  country, effect variable → atlas concept, event type → verified Wikidata class).
* :mod:`reva.v8.paths.search` — k cheapest, diverse paths (hop-limited bidirectional relaxation, hub damping) and
  the ego graph of one node.
* :mod:`reva.v8.paths.render` — hop sentences, provenance, licences and evidence strength read from the database.

Honesty: a path is a set of documented links, never a causal proof. A causal hop is a claim made by its source
(tier, grade A–E); an estimate hop is a published estimate by its authors; REVA asserts neither.
"""
