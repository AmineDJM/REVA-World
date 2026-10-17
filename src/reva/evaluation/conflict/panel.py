"""Country-year conflict panel built from pinned atlas frames and KG borders (pure: no database).

Keys are the Gleditsch-Ward codes the UCDP tables publish (``provider_geo = gw:<code>``); ISO3 comes from the same
cells (KGGEOPOL's date-aware crosswalk) and is used only to read covariates published by ISO3 (V-Dem, WDI). No
name is ever matched. Missing covariates stay ``None`` here; models decide (and record) how they treat them.

Inputs and what they contribute:

* ``atlas.ucdp.orgvio_cy`` ``sb_total_deaths_best`` (1989-): the outcome and the fatality history (UCDP codes 0
  when GED records no state-based death in a country-year -- a provider zero, kept);
* ``atlas.ucdp.acd_cy`` ``acd_conflicts_located`` (1946-): years with >= 1 UCDP/PRIO armed conflict located in the
  country (only years with >= 1 conflict are listed: an absent year means none coded);
* V-Dem v16 (``atlas.vdem.indices``): ``v2x_polyarchy`` (electoral democracy), ``v2x_clphy`` (physical violence
  index); WDI (``atlas.wb.wdi_econ`` / ``wdi_social``): GDP per capita (constant 2015 US$), GDP growth, population,
  infant mortality;
* KG ``cshapes:shares_border_with`` (CShapes 2.0, land borders, valid 1886-2019): neighbours by GW code; years after
  2019 use the borders valid on 2019-12-31 (CShapes ends there; carried forward and labelled).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl

CSHAPES_LAST_YEAR = 2019
COVARIATES: dict[str, tuple[str, str]] = {  # name -> (atlas dataset_key, indicator)
    "polyarchy": ("atlas.vdem.indices", "v2x_polyarchy"),
    "clphy": ("atlas.vdem.indices", "v2x_clphy"),
    "gdppc": ("atlas.wb.wdi_econ", "NY.GDP.PCAP.KD"),
    "growth": ("atlas.wb.wdi_econ", "NY.GDP.MKTP.KD.ZG"),
    "pop": ("atlas.wb.wdi_social", "SP.POP.TOTL"),
    "imr": ("atlas.wb.wdi_social", "SP.DYN.IMRT.IN"),
}
OUTCOME = ("atlas.ucdp.orgvio_cy", "sb_total_deaths_best")
ACD = ("atlas.ucdp.acd_cy", "acd_conflicts_located")


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None
    return x if math.isfinite(x) else None


@dataclass
class Panel:
    deaths: dict[str, dict[int, float]] = field(default_factory=dict)  # gw -> year -> deaths
    iso3: dict[str, dict[int, str | None]] = field(default_factory=dict)
    acd_years: dict[str, set[int]] = field(default_factory=dict)
    borders: list[tuple[str, str, int, int]] = field(default_factory=list)  # (gw_a, gw_b, first_year, last_year)
    cov: dict[str, dict[tuple[str, int], float]] = field(default_factory=dict)  # name -> (iso3, year) -> value
    notes: list[dict[str, Any]] = field(default_factory=list)
    _nb_cache: dict[int, dict[str, set[str]]] = field(default_factory=dict, repr=False)

    @property
    def last_year(self) -> int:
        return max(y for d in self.deaths.values() for y in d)

    @property
    def first_year(self) -> int:
        return min(y for d in self.deaths.values() for y in d)

    def geos(self) -> list[str]:
        return sorted(self.deaths)

    def y(self, g: str, year: int) -> float | None:
        return self.deaths.get(g, {}).get(year)

    def iso(self, g: str, year: int) -> str | None:
        return self.iso3.get(g, {}).get(year)

    def neighbours(self, g: str, year: int) -> set[str]:
        yy = min(year, CSHAPES_LAST_YEAR)
        if yy not in self._nb_cache:
            nb: dict[str, set[str]] = defaultdict(set)
            for a, b, y0, y1 in self.borders:
                if y0 <= yy <= y1:
                    nb[a].add(b)
                    nb[b].add(a)
            self._nb_cache[yy] = dict(nb)
        return self._nb_cache[yy].get(g, set())

    def covariate(self, name: str, g: str, year: int) -> float | None:
        iso = self.iso(g, year)
        if iso is None:
            return None
        return self.cov.get(name, {}).get((iso, year))


def _annual_rows(frame: pl.DataFrame, indicator: str) -> Iterable[dict[str, Any]]:
    f = frame.filter(pl.col("indicator") == indicator)
    f = f.filter(pl.col("period_end").dt.year() == pl.col("period_start").dt.year() + 1)
    yield from f.select("iso3", "provider_geo", "period_start", "value").iter_rows(named=True)


def build_panel(
    frames: Mapping[str, pl.DataFrame],
    borders: Iterable[tuple[str, str, int, int]] = (),
) -> Panel:
    """``frames``: dataset_key -> atlas frame (already restricted to the indicators used is fine)."""
    p = Panel()
    dk, ind = OUTCOME
    dup = 0
    for r in _annual_rows(frames[dk], ind):
        g, yr, v = str(r["provider_geo"]), r["period_start"].year, _num(r["value"])
        if v is None:
            continue
        if yr in p.deaths.get(g, {}):
            dup += 1
            continue
        p.deaths.setdefault(g, {})[yr] = v
        p.iso3.setdefault(g, {})[yr] = r["iso3"]
    if dup:
        p.notes.append({"reason": "duplicate outcome cells ignored (first kept)", "n": dup})
    if ACD[0] in frames:
        for r in _annual_rows(frames[ACD[0]], ACD[1]):
            v = _num(r["value"])
            if v is not None and v >= 1:
                p.acd_years.setdefault(str(r["provider_geo"]), set()).add(r["period_start"].year)
    for name, (cdk, cind) in COVARIATES.items():
        fr = frames.get(cdk)
        if fr is None:
            p.notes.append({"reason": f"covariate table {cdk} not pinned", "covariate": name})
            continue
        vals: dict[tuple[str, int], float] = {}
        for r in _annual_rows(fr, cind):
            v = _num(r["value"])
            if v is not None and r["iso3"]:
                vals.setdefault((str(r["iso3"]), r["period_start"].year), v)
        p.cov[name] = vals
    p.borders = [(a, b, y0, y1) for a, b, y0, y1 in borders]
    return p


__all__ = ["ACD", "COVARIATES", "CSHAPES_LAST_YEAR", "OUTCOME", "Panel", "build_panel"]
