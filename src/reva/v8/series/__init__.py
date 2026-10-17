"""V8 series service (agent V8MONDE): atlas catalog search, map slices, place series and multi-series.

Everything is read through the existing atlas query layer (:mod:`reva.atlas.query`, one DuckDB file per pinned
snapshot): no new storage, no writes. A *series* is one atlas ``<source>.<dataset>.<indicator>``; its indicator
splits into a *measure* (the provider code) and *filters* (``key=value`` dimensions), so a table can be browsed
measure by measure. Values are served as the exact decimal strings the source reports; a missing cell is typed
(the provider's missing reason, or why REVA has no cell) and never zero; projections are flagged and never
mixed with observations.
"""
