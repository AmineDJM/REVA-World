"""Performance: France-like demo_pjan recipe materialised at 100,000 records."""

from __future__ import annotations

import resource
import sys
import time
from datetime import date

import numpy as np
import pytest

from reva.population import (
    SourceCell,
    build_recipe,
    diagnose,
    exact_total_weight,
    materialise,
    table_logical_hash,
    to_parquet_bytes,
)

AGES = ["Y_LT1", *[f"Y{k}" for k in range(1, 100)], "Y_OPEN", "UNK"]


def _france_like_cells() -> list[SourceCell]:
    rng = np.random.default_rng(2024)
    cells = []
    for sex in ("F", "M"):
        counts = rng.integers(300_000, 370_000, size=len(AGES))
        for age, c in zip(AGES, counts, strict=True):
            cells.append(SourceCell(age, sex, int(c), ("p",) if age == "UNK" else (), f"{sex}.{age}"))
    total = sum(c.count or 0 for c in cells)
    cells.append(SourceCell("TOTAL", "T", total, (), "T.TOTAL"))
    return cells


def _peak_rss_bytes() -> int:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru if sys.platform == "darwin" else ru * 1024


@pytest.mark.perf
def test_materialise_100k_records_france_like():
    cells = _france_like_cells()
    t0 = time.perf_counter()
    recipe = build_recipe(
        cells,
        geography="FR-like-synthetic",
        reference_date=date(2024, 1, 1),
        source_ref={"dataset": "demo_pjan", "fixture": "synthetic"},
        seed=11,
    )
    assert len(recipe.leaf_cells) == 2 * 102
    assert 60_000_000 < recipe.total_count < 80_000_000
    t1 = time.perf_counter()
    table = materialise(recipe, 100_000)
    t2 = time.perf_counter()
    assert table.num_rows == 100_000
    assert exact_total_weight(table) == recipe.total_count
    digest = table_logical_hash(table)
    t3 = time.perf_counter()
    data = to_parquet_bytes(table)
    t4 = time.perf_counter()
    diag = diagnose(table, recipe)
    t5 = time.perf_counter()
    peak = _peak_rss_bytes()
    print(
        f"\n[perf] recipe={t1 - t0:.3f}s materialise={t2 - t1:.3f}s "
        f"table_hash={t3 - t2:.3f}s parquet={t4 - t3:.3f}s ({len(data)} bytes) "
        f"diagnose={t5 - t4:.3f}s total={t5 - t0:.3f}s peak_rss={peak / 2**20:.1f} MiB "
        f"ess={diag['effective_sample_size']:.1f} hash={digest[:12]}"
    )
    assert diag["calibration_error"]["joint_age_sex"]["max_abs_error"] == "0"
    assert t5 - t0 < 60
    assert peak < 2**30
