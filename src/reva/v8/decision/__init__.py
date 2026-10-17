"""V8 decision studio (agent V8DECISION): thin bundles over the V5 decision engine (``reva.predict``).

* :mod:`.templates` — the reference decisions, instruments and schema limits the composer validates against;
* :mod:`.bundle` — pure transforms of an engine result into the studio's page bundle (headline effects,
  propagation graph with attributed edges, evidence, conflicting estimates side by side, "why no result");
* :mod:`.runner` — read-only contexts on the database, an input-hash cache, the one-at-a-time sensitivity
  (tornado) and the paired A/B comparison (common random numbers);
* :mod:`.brief` — the Markdown brief of a scenario with every citation.

Nothing here writes to the database. Every result stays « scénario non validé — pas une probabilité » unless the
engine's judge registry says otherwise; nothing is presented as a forecast.
"""
