"""World knowledge graph (V3, agent KGCORE): storage interface, bulk loader and queries.

* ``reva.kg.model`` — dataclasses (``KgEntity``, ``KgStatement``, ``KgTime``, ``KgProperty``), the
  property registry writer and the bounded, idempotent bulk loader ``load_batch``.
* ``reva.kg.query`` — read-only queries: entity, neighbours, timeline, label search and the path engine
  (``paths``), which says "connected via" and never "caused".
"""
