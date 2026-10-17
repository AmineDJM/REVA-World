"""Vintage-aware data access: "what was published, and known, at date t" (V5, agent PRED-VINTAGE).

REVA's own clock starts on 2026-10-14 (``docs/strategy/world_map.md`` §5): everything REVA recorded is
"known" only from then on, so an honest backtest at an earlier origin needs the data **as published at the
time**. The vintage atlas tables (``reva.adapters.connectors.atlas_vintage_*``, conventions in
:mod:`reva.adapters.connectors.vintage_common`) store every value with its provider vintage and an evidenced,
conservative availability instant (``available_at`` + ``availability_basis``). This module selects from them.

:class:`VintageAccessor` (one cutoff, one mode; usually from :meth:`CutoffRepository.vintages`):

* ``available_then`` (reconstruction, :data:`reva.evaluation.replay.AVAILABLE_THEN`): a cell is admissible iff
  its ``available_at`` is known (with a basis) and ``<= cutoff``. No proof means exclusion (counted).
* ``as_known`` (operational): additionally the atlas table holding the cell must have been recorded by REVA at
  or before the cutoff — before 2026-10 nothing is admissible, which is the truth about REVA's own knowledge.
* ``revised_data_retrospective``: only :meth:`VintageAccessor.final_vintage` (the latest vintage, whatever the
  cutoff), labelled "final-vintage pseudo-real-time" everywhere; the as-of methods refuse this mode.

Selection always filters by time **before** choosing a vintage (the latest admissible vintage per series key
``(family, variable, provider_geo, period)``), so a later vintage can never change an earlier as-of answer.
Every returned row is also checked by a :class:`reva.evaluation.leakage.LeakageGuard` (defence in depth).
Projections are returned only by :meth:`forecasts` (or ``include_projections=True``), never as observations.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from reva.evaluation.cutoff import _GUARD_FOR, CutoffMode
from reva.evaluation.leakage import (
    RETROSPECTIVE_DISCLAIMER,
    GuardMode,
    InputRef,
    LeakageGuard,
    RefKind,
)
from reva.kernel.errors import CapabilityGap, LeakageBlocked, ValidationProblem

#: The registered vintage atlas tables (dataset keys).
VINTAGE_DATASET_KEYS: tuple[str, ...] = (
    "atlas.vintage_weo.weo_dbnomics",
    "atlas.vintage_weo.weo_imf",
    "atlas.vintage_us.rtdsm",
    "atlas.vintage_us.spf_median",
    "atlas.vintage_ea.ecb_spf",
    "atlas.vintage_oecd.stes_revisions",
)
FINAL_VINTAGE_LABEL = "final_vintage_pseudo_real_time"
FINAL_VINTAGE_DISCLAIMER = (
    "final-vintage pseudo-real-time: the latest published vintage is used at every origin; " + RETROSPECTIVE_DISCLAIMER
)
SERIES_KEY = ("family", "variable", "provider_geo", "period_start", "period_end")
_FLAG_FIELDS: dict[str, Any] = {
    "family": pl.String,
    "vintage": pl.String,
    "vintage_date_text": pl.String,
    "available_at": pl.String,
    "availability_basis": pl.String,
    "variable": pl.String,
    "provider_variable": pl.String,
    "estimate_kind": pl.String,
    "horizon": pl.String,
}
CELL_SCHEMA = pl.Schema(
    {  # type: ignore[arg-type]
        "dataset_key": pl.String,
        "table_recorded_at": pl.Datetime("us", "UTC"),
        "family": pl.String,
        "variable": pl.String,
        "provider_variable": pl.String,
        "indicator": pl.String,
        "provider_geo": pl.String,
        "iso3": pl.String,
        "period_start": pl.Date,
        "period_end": pl.Date,
        "vintage": pl.String,
        "vintage_date_text": pl.String,
        "available_at": pl.Datetime("us", "UTC"),
        "availability_basis": pl.String,
        "estimate_kind": pl.String,
        "horizon": pl.String,
        "value": pl.String,
        "unit": pl.String,
        "raw_sha256": pl.String,
        "selector": pl.String,
    }
)


def _utc(dt: datetime, name: str) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        raise ValidationProblem(f"{name} must be a timezone-aware datetime")
    return dt.astimezone(UTC)


def cells_from_atlas_frame(
    frame: pl.DataFrame, *, dataset_key: str, table_recorded_at: datetime | None
) -> pl.DataFrame:
    """Normalise a vintage atlas table (flags JSON -> columns) into :data:`CELL_SCHEMA`."""
    if frame.height == 0:
        return pl.DataFrame(schema=CELL_SCHEMA)
    rec = _utc(table_recorded_at, "table_recorded_at") if table_recorded_at is not None else None
    fl = frame.select(pl.col("flags").str.json_decode(pl.Struct(_FLAG_FIELDS)).alias("f")).unnest("f")
    missing = fl.select(pl.col("family").is_null().sum(), pl.col("variable").is_null().sum()).row(0)
    if any(missing):
        raise ValidationProblem("not a vintage table: cells without family/variable flags", dataset_key=dataset_key)
    out = pl.concat([frame.drop("flags"), fl], how="horizontal").select(
        pl.lit(dataset_key).alias("dataset_key"),
        pl.lit(rec, dtype=pl.Datetime("us", "UTC")).alias("table_recorded_at"),
        "family",
        "variable",
        "provider_variable",
        "indicator",
        "provider_geo",
        "iso3",
        "period_start",
        "period_end",
        "vintage",
        "vintage_date_text",
        pl.col("available_at").str.to_datetime(time_zone="UTC", time_unit="us", strict=True).alias("available_at"),
        "availability_basis",
        "estimate_kind",
        "horizon",
        "value",
        "unit",
        "raw_sha256",
        "selector",
    )
    bad = out.filter(pl.col("available_at").is_not_null() != pl.col("availability_basis").is_not_null())
    if bad.height:
        raise ValidationProblem("available_at without availability_basis (or the reverse)", dataset_key=dataset_key)
    return out.cast(dict(CELL_SCHEMA))  # type: ignore[arg-type]


def load_vintage_cells(
    conn: Any,
    store: Any,
    *,
    origin: str = "live",
    tables_cutoff: datetime | None = None,
    dataset_keys: Sequence[str] = VINTAGE_DATASET_KEYS,
) -> pl.DataFrame:
    """Vintage cells of the atlas table versions recorded at ``tables_cutoff`` (default: now)."""
    from sqlalchemy import text

    from reva.atlas.snapshot import atlas_tables_as_of
    from reva.atlas.tables import read_atlas_table

    cut = tables_cutoff or conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    frames = []
    for t in atlas_tables_as_of(conn, cutoff=cut, origin=origin):
        if t["dataset_key"] not in dataset_keys:
            continue
        df = read_atlas_table(store, t["artifact_sha256"])
        frames.append(
            cells_from_atlas_frame(
                df, dataset_key=t["dataset_key"], table_recorded_at=datetime.fromisoformat(t["recorded_at"])
            )
        )
    return pl.concat(frames, how="vertical") if frames else pl.DataFrame(schema=CELL_SCHEMA)


@dataclass
class VintageScope:
    """Selected vintage cells plus exclusion accounting (counts are over candidate cells)."""

    mode: str
    cutoff: datetime
    rows: pl.DataFrame
    exclusions: Counter[str] = field(default_factory=Counter)
    label: str | None = None

    def exclusion_report(self) -> list[dict[str, Any]]:
        why = {
            "no_availability_proof": "available_at/availability_basis missing: no proof means exclusion",
            "available_after_cutoff": "the vintage was published after the cutoff",
            "recorded_after_cutoff": "as_known: REVA recorded the table after the cutoff",
            "only_projection_at_cutoff": "the latest admissible vintage holds only a projection for this period",
        }
        return [{"reason": k, "count": v, "explanation": why.get(k, k)} for k, v in sorted(self.exclusions.items())]

    def as_records(self) -> list[dict[str, Any]]:
        return self.rows.to_dicts()


def _ref_id(r: dict[str, Any]) -> str:
    h = hashlib.sha256(f"{r['dataset_key']}|{r['indicator']}|{r['provider_geo']}|{r['period_start']}".encode())
    return f"{r['dataset_key']}#{h.hexdigest()[:16]}"


class VintageAccessor:
    """As-of selection over vintage cells (see the module docstring)."""

    __slots__ = ("_cells", "_cutoff", "_guard", "_mode")

    def __init__(self, cells: pl.DataFrame, cutoff: datetime, mode: str | CutoffMode = CutoffMode.available_then):
        try:
            self._mode = CutoffMode(mode)
        except ValueError as e:
            raise CapabilityGap(f"unknown cutoff mode {mode!r}", supported=[m.value for m in CutoffMode]) from e
        self._cutoff = _utc(cutoff, "cutoff")
        if set(cells.columns) != set(CELL_SCHEMA.names()):
            raise ValidationProblem("vintage cells must have CELL_SCHEMA (use cells_from_atlas_frame)")
        self._cells = cells.select(CELL_SCHEMA.names())
        # final_vintage() is the labelled downgrade: its guard labels, never silently admits as real-time
        gm = GuardMode.revised_data_retrospective if self._mode is CutoffMode.revised_data_retrospective else None
        self._guard = LeakageGuard(self._cutoff, gm or _GUARD_FOR[self._mode])

    @property
    def cutoff(self) -> datetime:
        return self._cutoff

    @property
    def mode(self) -> CutoffMode:
        return self._mode

    @property
    def guard(self) -> LeakageGuard:
        return self._guard

    # ------------------------------------------------------------------ filtering
    @staticmethod
    def _filter(
        df: pl.DataFrame,
        family: str | None,
        variable: str | Iterable[str] | None,
        geos: Iterable[str] | None,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> pl.DataFrame:
        if family is not None:
            df = df.filter(pl.col("family") == family)
        if variable is not None:
            vs = [variable] if isinstance(variable, str) else list(variable)
            df = df.filter(pl.col("variable").is_in(vs))
        if geos is not None:
            g = list(geos)
            df = df.filter(pl.col("provider_geo").is_in(g) | pl.col("iso3").is_in(g))
        if period_start is not None:
            df = df.filter(pl.col("period_start") >= period_start)
        if period_end is not None:
            df = df.filter(pl.col("period_end") <= period_end)
        return df

    def _admissible(self, df: pl.DataFrame) -> tuple[pl.DataFrame, Counter[str]]:
        if self._mode is CutoffMode.revised_data_retrospective:
            raise CapabilityGap(
                "as-of vintage selection is not available in revised_data_retrospective mode; "
                "use final_vintage() (labelled final-vintage pseudo-real-time)",
                supported=[CutoffMode.available_then.value, CutoffMode.as_known.value],
            )
        c = self._cutoff
        reason = (
            pl.when(pl.col("available_at").is_null() | pl.col("availability_basis").is_null())
            .then(pl.lit("no_availability_proof"))
            .when(pl.col("available_at") > c)
            .then(pl.lit("available_after_cutoff"))
        )
        if self._mode is CutoffMode.as_known:
            reason = reason.when(pl.col("table_recorded_at").is_null() | (pl.col("table_recorded_at") > c)).then(
                pl.lit("recorded_after_cutoff")
            )
        tagged = df.with_columns(reason.otherwise(pl.lit(None, dtype=pl.String)).alias("_excl"))
        counts = Counter(
            {
                str(k): int(n)
                for k, n in tagged.filter(pl.col("_excl").is_not_null()).group_by("_excl").len().iter_rows()
            }
        )
        return tagged.filter(pl.col("_excl").is_null()).drop("_excl"), counts

    def _check(self, rows: pl.DataFrame) -> None:
        """Vectorised assertion + per-row typed guard (defence in depth: raises LeakageBlocked)."""
        if rows.height == 0:
            return
        if self._mode is not CutoffMode.revised_data_retrospective:
            late = rows.filter(pl.col("available_at").is_null() | (pl.col("available_at") > self._cutoff))
            if late.height:
                raise LeakageBlocked(
                    "vintage cell published after the cutoff was selected", cutoff=self._cutoff.isoformat()
                )
        for r in rows.select(
            "dataset_key",
            "indicator",
            "provider_geo",
            "period_start",
            "table_recorded_at",
            "available_at",
            "availability_basis",
            "vintage",
            "vintage_date_text",
        ).iter_rows(named=True):
            self._guard.check(
                InputRef(
                    RefKind.observation_revision,
                    _ref_id(r),
                    recorded_at=r["table_recorded_at"],
                    source_available_at=r["available_at"],
                    availability_basis=(
                        {
                            "rule": r["availability_basis"],
                            "vintage": r["vintage"],
                            "vintage_date_text": r["vintage_date_text"],
                        }
                        if r["availability_basis"]
                        else None
                    ),
                    detail={"dataset_key": r["dataset_key"], "indicator": r["indicator"]},
                )
            )

    @staticmethod
    def _latest_per_key(df: pl.DataFrame) -> pl.DataFrame:
        return (
            df.sort(["available_at", "vintage", "dataset_key"], descending=True, nulls_last=True)
            .unique(subset=list(SERIES_KEY), keep="first", maintain_order=True)
            .sort(list(SERIES_KEY))
        )

    # ------------------------------------------------------------------ as-of API
    def latest(
        self,
        *,
        family: str | None = None,
        variable: str | Iterable[str] | None = None,
        geos: Iterable[str] | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        include_projections: bool = False,
    ) -> VintageScope:
        """Per series key, the value in the latest vintage published at/before the cutoff.

        Without ``include_projections`` a period whose latest admissible vintage holds only a projection is
        dropped and counted (``only_projection_at_cutoff``): a forecast is never returned as an observation."""
        df = self._filter(self._cells, family, variable, geos, period_start, period_end)
        adm, excl = self._admissible(df)
        sel = self._latest_per_key(adm)
        if not include_projections:
            proj = sel.filter(pl.col("estimate_kind") == "projection")
            if proj.height:
                excl["only_projection_at_cutoff"] += proj.height
            sel = sel.filter(pl.col("estimate_kind") != "projection")
        self._check(sel)
        return VintageScope(self._mode.value, self._cutoff, sel, excl)

    def forecasts(
        self,
        *,
        family: str,
        variable: str | Iterable[str] | None = None,
        geos: Iterable[str] | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> VintageScope:
        """Projections of the latest admissible vintage per series key (e.g. the WEO or SPF forecast an
        analyst could have used at the cutoff)."""
        df = self._filter(self._cells, family, variable, geos, period_start, period_end)
        adm, excl = self._admissible(df)
        sel = self._latest_per_key(adm).filter(pl.col("estimate_kind") == "projection")
        self._check(sel)
        return VintageScope(self._mode.value, self._cutoff, sel, excl)

    def vintage_in_force(self, family: str) -> dict[str, Any] | None:
        """The latest vintage of ``family`` published at/before the cutoff (label, availability, basis)."""
        adm, _ = self._admissible(self._filter(self._cells, family, None, None))
        if adm.height == 0:
            return None
        top = (
            adm.group_by("vintage", "dataset_key")
            .agg(
                pl.col("available_at").max(), pl.col("availability_basis").first(), pl.col("vintage_date_text").first()
            )
            .sort(["available_at", "vintage"], descending=True)
            .row(0, named=True)
        )
        return {**top, "available_at": top["available_at"].isoformat()}

    def history(
        self, *, family: str, variable: str, geo: str, period_start: date, period_end: date | None = None
    ) -> VintageScope:
        """Every admissible vintage of one cell, oldest first (the revision triangle as known at the cutoff)."""
        df = self._filter(self._cells, family, variable, [geo], period_start, period_end).filter(
            pl.col("period_start") == period_start
        )
        adm, excl = self._admissible(df)
        adm = adm.sort(["available_at", "vintage"])
        self._check(adm)
        return VintageScope(self._mode.value, self._cutoff, adm, excl)

    def first_release(
        self,
        *,
        family: str,
        variable: str | Iterable[str] | None = None,
        geos: Iterable[str] | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> VintageScope:
        """Per series key, the earliest admissible non-projection vintage (the first release as known at the
        cutoff; scoring a forecast at time T must use an accessor whose cutoff is T)."""
        df = self._filter(self._cells, family, variable, geos, period_start, period_end)
        adm, excl = self._admissible(df)
        sel = (
            adm.filter(pl.col("estimate_kind") != "projection")
            .sort(["available_at", "vintage"], nulls_last=True)
            .unique(subset=list(SERIES_KEY), keep="first", maintain_order=True)
            .sort(list(SERIES_KEY))
        )
        self._check(sel)
        return VintageScope(self._mode.value, self._cutoff, sel, excl)

    def final_vintage(
        self,
        *,
        family: str | None = None,
        variable: str | Iterable[str] | None = None,
        geos: Iterable[str] | None = None,
        include_projections: bool = False,
    ) -> VintageScope:
        """The latest vintage whatever the cutoff — only in ``revised_data_retrospective`` mode, labelled."""
        if self._mode is not CutoffMode.revised_data_retrospective:
            raise CapabilityGap(
                "final_vintage() is the explicit pseudo-real-time downgrade: open the accessor in "
                "revised_data_retrospective mode",
                supported=[CutoffMode.revised_data_retrospective.value],
            )
        df = self._filter(self._cells, family, variable, geos).filter(pl.col("available_at").is_not_null())
        sel = self._latest_per_key(df)
        if not include_projections:
            sel = sel.filter(pl.col("estimate_kind") != "projection")
        self._check(sel)
        return VintageScope(self._mode.value, self._cutoff, sel, Counter(), label=FINAL_VINTAGE_LABEL)


# --------------------------------------------------------------------------- replay manifest
def vintage_replay(
    conn: Any,
    store: Any,
    cutoff: datetime,
    *,
    family: str,
    variable: str | Sequence[str] | None = None,
    geos: Sequence[str] | None = None,
    include_projections: bool = False,
    origins: Sequence[str] = ("live",),
) -> dict[str, Any]:
    """An ``available_then_reconstruction`` replay manifest of vintage data at ``cutoff`` (inputs with their
    availability proof, exclusions, the vintage in force). Same schema as :mod:`reva.evaluation.replay`."""
    from reva.evaluation.cutoff import CutoffRepository
    from reva.evaluation.replay import AVAILABLE_THEN, _base, _check_cutoff, _plain

    now = _check_cutoff(conn, cutoff)
    m = _base(AVAILABLE_THEN, cutoff, now)
    repo = CutoffRepository(conn, cutoff, CutoffMode.available_then, origins=origins)
    acc = repo.vintages(store)
    scope = acc.latest(family=family, variable=variable, geos=geos, include_projections=include_projections)
    m["vintage_in_force"] = acc.vintage_in_force(family)
    m["inputs"] = [
        {
            "dataset_key": r["dataset_key"],
            "indicator": r["indicator"],
            "provider_geo": r["provider_geo"],
            "period": [str(r["period_start"]), str(r["period_end"])],
            "vintage": r["vintage"],
            "estimate_kind": r["estimate_kind"],
            "value": r["value"],
            "unit": r["unit"],
            "raw_sha256": r["raw_sha256"],
            "selector": json.loads(r["selector"]),
            "availability_proof": {
                "kind": "source_availability",
                "source_available_at": _plain(r["available_at"]),
                "availability_basis": {
                    "rule": r["availability_basis"],
                    "vintage": r["vintage"],
                    "vintage_date_text": r["vintage_date_text"],
                },
            },
        }
        for r in scope.rows.to_dicts()
    ]
    m["exclusions"] = scope.exclusion_report()
    m["family"] = family
    if not m["inputs"]:
        m["status"] = "no_available_inputs"
        m["detail"] = "no vintage cell has proven availability at/before the cutoff"
    return m


__all__ = [
    "CELL_SCHEMA",
    "FINAL_VINTAGE_DISCLAIMER",
    "FINAL_VINTAGE_LABEL",
    "VINTAGE_DATASET_KEYS",
    "VintageAccessor",
    "VintageScope",
    "cells_from_atlas_frame",
    "load_vintage_cells",
    "vintage_replay",
]
