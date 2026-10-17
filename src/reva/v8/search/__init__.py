"""V8 universal search (agent V8API): a prebuilt SQLite index over every searchable REVA record.

``build.py`` collects documents (KG entities, atlas concepts/tables, countries/regions, forecast questions and
families, reference decisions, effect estimates, sources) and writes ``var/v8/search_index/search.sqlite``;
``index.py`` opens it read-only and ranks hits (exact id/code, exact label, prefix, word prefix, substring /
trigram). Search is lookup, never identity: two records with the same name stay two hits.
"""
