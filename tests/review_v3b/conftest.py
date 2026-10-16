"""Review V3b (agent V3REVIEW2): reproductions of defects found in the second V3 review (docs/dev/review_v3b.md).

Every test that demonstrates a defect is ``xfail(strict=True)``: the suite stays green while the defect exists
and turns red (XPASS) the day it is fixed, so the marker must then be removed with the fix.

Two kinds of tests:

* synthetic: a DuckDB query file shaped like ``reva.atlas.query.build_snapshot_db`` (tables ``series``,
  ``cells``, ``cells_s``, ``meta`` + the verified ``.sha256`` sidecar) built in ``tmp_path`` and opened through
  the real :class:`reva.atlas.query.AtlasQuery`; or real ``reva.links`` derivations over synthetic statements;
* dev snapshot (``dev_atlas``): the live dev-DB snapshot ``b11f1c51…`` read **read-only** through
  ``AtlasQuery`` (its query file in the private cache dir). Skipped when the file is not there. The full-file
  sha256 verification is replaced by a sidecar/meta check so that a test can never delete the 6.4 GB file.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from v3b_helpers import DEV_SNAPSHOT, SynthAtlas  # noqa: E402


@pytest.fixture
def synth_atlas(tmp_path: Path) -> SynthAtlas:
    return SynthAtlas(tmp_path / "atlas-cache")


def _dev_cache() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(os.environ.get("REVA_REVIEW_DEV_ATLAS_CACHE", f"/tmp/reva-atlas-cache-{uid}"))


@pytest.fixture(scope="session")
def dev_atlas() -> Iterator[Any]:
    """The dev-DB live snapshot through AtlasQuery, read-only; skipped when its query file is absent."""
    from reva.atlas import query as Q
    from reva.atlas.snapshot import AtlasSnapshot

    d = _dev_cache()
    final = d / f"atlas-{DEV_SNAPSHOT}{Q.DB_SUFFIX}"
    side = final.with_name(final.name + ".sha256")
    if not final.exists() or not side.exists():
        pytest.skip("dev atlas snapshot query file not available")
    want = json.loads(side.read_text())
    if want.get("format") != Q.DB_FORMAT or want.get("snapshot_sha256") != DEV_SNAPSHOT:
        pytest.skip("dev atlas query file is of another format/snapshot")
    original = Q._verify_db

    def _sidecar_only(path: Path, snapshot: Any) -> None:  # never unlinks the dev file
        con = duckdb.connect(str(path), read_only=True)
        try:
            meta = con.execute("SELECT format, snapshot_sha256 FROM meta").fetchone()
        finally:
            con.close()
        if meta != (Q.DB_FORMAT, DEV_SNAPSHOT):
            pytest.skip("dev atlas query file meta does not match")

    Q._verify_db = _sidecar_only  # type: ignore[assignment]
    try:
        snap = AtlasSnapshot(DEV_SNAPSHOT, "live", datetime(2026, 10, 16, 19, 19, 27, tzinfo=UTC), ())
        q = Q.AtlasQuery(snap, None, d)  # type: ignore[arg-type]
    finally:
        Q._verify_db = original  # type: ignore[assignment]
    try:
        yield q
    finally:
        q.close()
