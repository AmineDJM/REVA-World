"""Deterministic time-series evaluation splits and cutoff-scoped data access.

``rolling_origins`` enumerates expanding-window forecast origins. For origin
index ``i`` (the position of the last training year in the sorted ``years``):

* ``train_years``  = ``years[0 .. i]`` (expanding window, at least ``min_train``)
* for each requested horizon ``h`` the target year is
  ``years[i] + embargo + h`` and is included only if that year is present.
  ``embargo`` years immediately after the training window are skipped (neither
  trained on nor scored), so the step from the training end is ``embargo + h``,
  reported in ``steps``. With ``embargo=0`` (default) ``steps == horizons``.

Horizons are calendar-year offsets, so gaps in ``years`` never silently turn a
2-year-ahead forecast into a "1-step" one.

``CutoffScopedSeries`` exposes only observations with ``year <= cutoff``; asking
for anything later raises :class:`reva.kernel.errors.LeakageBlocked`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import NamedTuple

from reva.kernel.errors import LeakageBlocked


class RollingOrigin(NamedTuple):
    origin_index: int
    train_years: tuple[int, ...]
    target_years: tuple[int, ...]
    steps: tuple[int, ...]  # embargo + h for each included target, aligned with target_years


def rolling_origins(
    years: Iterable[int], min_train: int, horizons: Sequence[int], embargo: int = 0
) -> list[RollingOrigin]:
    ys = sorted(set(years))
    if min_train < 1:
        raise ValueError("min_train must be >= 1")
    if embargo < 0:
        raise ValueError("embargo must be >= 0")
    hs = tuple(horizons)
    if not hs or any(h < 1 for h in hs) or len(set(hs)) != len(hs):
        raise ValueError("horizons must be distinct positive integers")
    present = set(ys)
    out: list[RollingOrigin] = []
    for i in range(min_train - 1, len(ys)):
        targets: list[int] = []
        steps: list[int] = []
        for h in sorted(hs):
            t = ys[i] + embargo + h
            if t in present:
                targets.append(t)
                steps.append(embargo + h)
        if targets:
            out.append(RollingOrigin(i, tuple(ys[: i + 1]), tuple(targets), tuple(steps)))
    return out


class CutoffScopedSeries:
    """Read-only view of a series that refuses information after ``cutoff``."""

    __slots__ = ("_cutoff", "_data")

    def __init__(self, observations: Iterable[tuple[int, object]], cutoff: int) -> None:
        data: dict[int, object] = {}
        for year, value in observations:
            if year in data:
                raise ValueError(f"duplicate year {year}")
            if year <= cutoff:
                data[year] = value
        self._data = dict(sorted(data.items()))
        self._cutoff = cutoff

    @property
    def cutoff(self) -> int:
        return self._cutoff

    def _guard(self, year: int) -> None:
        if year > self._cutoff:
            raise LeakageBlocked(
                f"year {year} is after the cutoff {self._cutoff}",
                year=year,
                cutoff=self._cutoff,
            )

    def value(self, year: int) -> object:
        self._guard(year)
        if year not in self._data:
            raise KeyError(year)
        return self._data[year]

    def window(self, start: int | None = None, end: int | None = None) -> list[tuple[int, object]]:
        """Observations with ``start <= year <= end`` (``end`` defaults to the cutoff)."""
        if end is not None:
            self._guard(end)
        hi = self._cutoff if end is None else end
        return [(y, v) for y, v in self._data.items() if (start is None or y >= start) and y <= hi]

    def observations(self) -> list[tuple[int, object]]:
        return list(self._data.items())

    def years(self) -> tuple[int, ...]:
        return tuple(self._data)

    def __len__(self) -> int:
        return len(self._data)
