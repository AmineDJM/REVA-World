"""Values a dossier query returns: each sourced (``Val``) or explicitly missing (``Miss``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reva.v8.dossier.fmt import fr_number, fr_period, to_decimal, with_unit


@dataclass(frozen=True)
class Src:
    """Footnote content (numbered when the document is assembled)."""

    source: str
    title: str
    dataset: str | None = None
    indicator: str | None = None
    period: str | None = None
    licence: str | None = None
    licence_url: str | None = None
    restricted: bool = False
    non_commercial: bool = False
    required_notice: str | None = None
    ref: str | None = None
    url: str | None = None
    retrieved: str | None = None
    verification: str | None = None
    method: str | None = None

    def key(self) -> tuple[Any, ...]:
        return (self.source, self.dataset, self.indicator, self.period, self.ref, self.method, self.title)

    def with_period(self, period: str | None) -> Src:
        return Src(**{**self.__dict__, "period": period})


@dataclass(frozen=True)
class Val:
    text: str
    src: Src
    kind: str = "observed"
    exact: str | None = None
    unit: str | None = None
    num: float | None = None
    href: str | None = None


@dataclass(frozen=True)
class Miss:
    what: str
    reason: str


Item = Val | Miss


@dataclass
class SeriesData:
    label: str
    kind: str
    src: Src
    unit: str
    x_kind: str
    points: list[tuple[str, float | None, str | None, float | None, float | None]]  # x, y, exact, lo, hi


@dataclass
class Result:
    values: dict[str, Item] = field(default_factory=dict)
    rows: list[dict[str, Item]] = field(default_factory=list)
    series: list[SeriesData] = field(default_factory=list)
    more: int = 0
    missing: list[Miss] = field(default_factory=list)
    empty_reason: str | None = None


def num_val(
    v: Any,
    src: Src,
    *,
    unit: str | None = None,
    kind: str = "observed",
    digits: int | None = None,
    show_unit: bool = True,
) -> Item:
    """A number as reported (exact decimal string kept), formatted in French with its unit."""
    d = to_decimal(v)
    if d is None:
        return Miss(src.title, "valeur non numérique ou absente chez la source")
    txt = fr_number(d, digits, unit) or str(v)
    return Val(
        text=with_unit(txt, unit) if show_unit else txt,
        src=src,
        kind=kind,
        exact=str(v),
        unit=unit,
        num=float(d),
    )


def label_val(text: str | None, src: Src, *, href: str | None = None, kind: str = "label") -> Item:
    if text is None or str(text).strip() == "":
        return Miss(src.title, "libellé absent chez la source")
    return Val(text=str(text), src=src, kind=kind, href=href)


def period_val(p: str | None, src: Src) -> Item:
    t = fr_period(p)
    if not t:
        return Miss(src.title, "période non indiquée par la source")
    return Val(text=t, src=src, kind="label", exact=p)
