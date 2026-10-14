"""Synthetic demo_pjan-like fixture (no real data, no network).

Ages Y_LT1, Y1..Y9, an open group Y_OPEN (>= 10) and UNK; recorded sex F/M,
plus Eurostat-style totals (sex T, age TOTAL) and age-band subtotals.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date

import pytest

from reva.population import PopulationRecipe, SourceCell, build_recipe

AGES = ["Y_LT1", *[f"Y{k}" for k in range(1, 10)], "Y_OPEN", "UNK"]
F_COUNTS = [101, 98, 97, 103, 99, 100, 95, 90, 92, 91, 340, 7]
M_COUNTS = [105, 101, 99, 0, 100, 102, 97, 94, 90, 88, 250, 5]  # M|Y3 is an observed zero
BANDS = {"Y_LT5": range(0, 5), "Y5-9": range(5, 10)}  # index ranges into AGES


def make_cells(
    f_counts: list[int | None] | None = None,
    m_counts: list[int | None] | None = None,
    *,
    total_offsets: dict[str, int] | None = None,
    drop: Iterable[tuple[str, str]] = (),
    flags: dict[tuple[str, str], tuple[str, ...]] | None = None,
) -> list[SourceCell]:
    """Leaves + consistent totals/subtotals; ``total_offsets`` perturbs reported totals."""
    f = list(F_COUNTS if f_counts is None else f_counts)
    m = list(M_COUNTS if m_counts is None else m_counts)
    offsets = total_offsets or {}
    dropped = set(drop)
    flags = flags or {}
    leaf = {"F": f, "M": m}
    cells: list[SourceCell] = []

    def add(age: str, sex: str, count: int | None) -> None:
        if (sex, age) in dropped:
            return
        cells.append(
            SourceCell(age, sex, count, flags.get((sex, age), ()), f"demo_pjan.XX.2024.{sex}.{age}")
        )

    def ssum(values: Iterable[int | None]) -> int | None:
        vals = list(values)
        return None if any(v is None for v in vals) else sum(v for v in vals if v is not None)

    for sex in ("F", "M"):
        for age, c in zip(AGES, leaf[sex], strict=True):
            add(age, sex, c)
    for i, age in enumerate(AGES):
        add(age, "T", ssum([f[i], m[i]]))
    for sex, vals in (("F", f), ("M", m), ("T", [ssum(p) for p in zip(f, m, strict=True)])):
        tot = ssum(vals)
        add("TOTAL", sex, None if tot is None else tot + offsets.get(f"{sex}|TOTAL", 0))
        for band, idx in BANDS.items():
            add(band, sex, ssum(vals[i] for i in idx))
        add("Y_GE5", sex, ssum([*[vals[i] for i in range(5, 10)], vals[10]]))
    return cells


def recipe_from(cells: list[SourceCell], seed: int = 7, **kw: object) -> PopulationRecipe:
    return build_recipe(
        cells,
        geography="XX-synthetic",
        reference_date=date(2024, 1, 1),
        source_ref={"dataset": "demo_pjan", "fixture": "synthetic"},
        seed=seed,
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
def cells() -> list[SourceCell]:
    return make_cells()


@pytest.fixture
def recipe(cells: list[SourceCell]) -> PopulationRecipe:
    return recipe_from(cells)


@pytest.fixture
def cell_factory() -> Callable[..., list[SourceCell]]:
    return make_cells


@pytest.fixture
def recipe_factory() -> Callable[..., PopulationRecipe]:
    return recipe_from
