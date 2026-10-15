"""REVA Atlas (V3): every indicator, every country, every year, from every open source.

* :mod:`reva.atlas.tables` — the fixed long schema of an *atlas table*, its registration
  (content-addressed Parquet + manifest via ``register_dataset``) and the country-code helper.
* :mod:`reva.atlas.catalog` — the concept catalog (``registry/atlas/catalog/<domain>.yaml``).
* :mod:`reva.atlas.snapshot` — atlas snapshots (the set of atlas tables recorded at a cutoff).
* :mod:`reva.atlas.query` — DuckDB queries over the pinned Parquet files.

Atlas cells are what a source reports. Nothing here imputes, blends or turns a missing
value into zero.
"""
