"""Shared helpers of tests/review_v3b (see conftest.py)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

DEV_SNAPSHOT = "b11f1c51bc14197497d167e2bbc7d5d6b4ee320fa79a9db6d1d6ca1444fa9768"
SHA = "a" * 64
TSHA = "b" * 64


class SynthAtlas:
    """Cells -> a verified query file in ``directory`` -> :class:`AtlasQuery` (no database, no store)."""

    def __init__(self, directory: Path, snapshot_sha: str = "d" * 64) -> None:
        self.directory = directory
        self.snapshot_sha = snapshot_sha
        self.series: dict[tuple[str, str, str], int] = {}
        self.rows: list[list[Any]] = []

    def cell(
        self,
        series: str,
        iso3: str,
        year: int,
        value: str | None,
        *,
        unit: str,
        flags: dict[str, Any] | None = None,
    ) -> None:
        src, ds, ind = series.split(".", 2)
        key = (src, ds, ind)
        if key not in self.series:
            self.series[key] = len(self.series) + 1
        self.rows.append(
            [
                self.series[key],
                iso3,
                year,
                value is not None,
                None,
                iso3,
                None,
                date(year, 1, 1),
                date(year + 1, 1, 1),
                value,
                None,
                unit,
                json.dumps(flags or {}, sort_keys=True),
                SHA,
                json.dumps({"row": len(self.rows)}),
                TSHA,
                "calendar_year",
            ]
        )

    def query(self) -> Any:
        from reva.atlas.query import DB_FORMAT, DB_SUFFIX, DDL, AtlasQuery
        from reva.atlas.snapshot import AtlasSnapshot

        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        final = self.directory / f"atlas-{self.snapshot_sha}{DB_SUFFIX}"
        con = duckdb.connect(str(final))
        try:
            con.execute(DDL)
            for (src, ds, ind), sid in self.series.items():
                con.execute("INSERT INTO series VALUES (?, ?, ?, ?)", [sid, src, ds, ind])
            for r in self.rows:
                con.execute("INSERT INTO cells VALUES (" + ", ".join("?" * len(r)) + ")", r)
            con.execute("INSERT INTO cells_s SELECT * FROM cells ORDER BY sid, year, iso3")
            con.execute(
                "CREATE TABLE meta AS SELECT ? AS format, ? AS snapshot_sha256, ? AS origin, ? AS cutoff",
                [DB_FORMAT, self.snapshot_sha, "test_fixture", "2026-10-16T00:00:00+00:00"],
            )
            con.execute("CHECKPOINT")
        finally:
            con.close()
        h = hashlib.sha256(final.read_bytes()).hexdigest()
        final.with_name(final.name + ".sha256").write_text(
            json.dumps({"format": DB_FORMAT, "snapshot_sha256": self.snapshot_sha, "sha256": h})
        )
        snap = AtlasSnapshot(self.snapshot_sha, "test_fixture", datetime(2026, 10, 16, tzinfo=UTC), ())
        return AtlasQuery(snap, None, self.directory)  # type: ignore[arg-type]


def rows(q: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return q._rows(sql, params)  # type: ignore[no-any-return]
