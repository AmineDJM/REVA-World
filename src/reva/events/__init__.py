"""Event backbone (V3 causality, agent EVENTS): the event view over the knowledge graph.

* ``reva.events.taxonomy`` — ``registry/events/taxonomy.yaml``: event types (fr/en labels, verified Wikidata
  class QIDs, parent types, typical participant roles) and dataset-kind mappings.
* ``reva.events.view`` — read-only event view: classification (rank-aware ``P31`` + recorded ``P279`` chains),
  time interval with precision, places, participants with roles, structure, listing.
* ``reva.events.same_as`` — explicit ``same_as`` statements between records of the same real event, created only
  from evidence (shared identifiers, evidenced mapping tables), never from fuzzy name/date matching.
* ``reva.events.verify`` — budget-metered Wikidata lookups that confirm every QID/PID the taxonomy and the event
  specs use.

Nothing here asserts causation: structure (part of, follows) and participation are context, not causes.
"""
