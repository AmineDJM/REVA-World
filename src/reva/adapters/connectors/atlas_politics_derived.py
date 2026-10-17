"""Dated policy-change events DERIVED by REVA from registered atlas series (agent V6POLITICS): central-bank policy
rate changes (BIS daily policy rates) and statutory corporate income tax rate changes (OECD Corporate Tax
Statistics).

Neither table fetches anything: each reads the latest registration of its input atlas table for the run's origin
(``atlas.bis.cbpol_d`` of :mod:`atlas_hf_bis`, ``atlas.oecd.cts_cit`` of :mod:`atlas_oecd_fiscal`), and every
derived row keeps the input cells' raw sha256 and selectors (provenance to the provider bytes the input was parsed
from) plus the input table's artifact sha256. Rights are the inputs' (BIS terms of permitted use; OECD open by
default, CC BY 4.0). Both specs reserve one request on the input's budget key (the minimum the platform takes) and
use none.

* ``bis.cbpol_d_changes`` -- one row per BIS reference area and day on which the daily policy rate differs from the
  previous observation: value = the new rate (verbatim), flags ``previous_value``, ``previous_date``, ``change_pp``
  (new - previous, percentage points, exact decimal), ``direction``, ``held_until`` (the last day the new rate is
  observed before the next change; ``null`` while it is still the latest observation), ``gap_days`` and the BIS
  ``COMPILATION`` text of the series (which rate the series is in which period). **A change in the series is not
  necessarily a policy decision**: for periods in which BIS compiles a market rate (e.g. an effective overnight
  rate, a rediscount or an interbank rate, per COMPILATION) the series moves without a decision. Nothing here is
  labelled a decision; consumers filter with the COMPILATION text and ``held_until``.
* ``oecd.cts_cit_changes`` -- one row per country and year in which OECD's statutory CIT rate
  (``CIT_C`` combined central + sub-central, ``CIT`` central government; standard ``targeting=ST``) differs from
  the previous year's value: value = the new rate, flags ``previous_value``, ``previous_year``, ``change_pp``,
  ``direction``. A rate change is a dated tax-policy event at annual resolution as OECD records it (the rate in force
  for the tax year), not the enactment date of the law.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any

from reva.adapters.connectors import v6politics_common as vc
from reva.adapters.connectors.atlas_society_common import RowBuffer, canon
from reva.atlas.sources import AtlasSourceSpec

VERSION = "1.0.0"
PARSER_VERSION = "atlas_politics_derived/1"


@dataclass(frozen=True)
class Derivation:
    name: str
    atlas_source: str
    dataset: str
    input_key: str
    budget_key: str
    host: str
    provider: str
    licence: str


BIS = Derivation("atlas.bis_cbpol_changes", "bis", "cbpol_d_changes", "atlas.bis.cbpol_d", "bis.bulk",
                 "data.bis.org", "Bank for International Settlements (derived by REVA)",
                 "BIS terms of permitted use of BIS statistics (input atlas.bis.cbpol_d)")  # fmt: skip
CIT = Derivation("atlas.oecd_cit_changes", "oecd", "cts_cit_changes", "atlas.oecd.cts_cit", "oecd.sdmx",
                 "sdmx.oecd.org", "OECD Corporate Tax Statistics (derived by REVA)",
                 "OECD terms: open by default, CC BY 4.0 since 2024-07-01 (input atlas.oecd.cts_cit)")  # fmt: skip
SERIES_KEY = "atlas.bis.cbpol_d_series"
CIT_INDICATORS = ("CIT_C|targeting=ST|sector=S13|unit_measure=PT_INC_TAX",
                  "CIT|targeting=ST|sector=S1311|unit_measure=PT_INC_TAX")  # fmt: skip


def _pp(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "") else s


def bis_changes(frame: Any, *, input_sha: str, compilation: dict[str, str]) -> tuple[Any, dict[str, Any]]:
    """Rate-change rows from an ``atlas.bis.cbpol_d`` frame (one indicator ``CBPOL``, one row per area and day)."""
    import polars as pl

    buf = RowBuffer(BIS.atlas_source, BIS.dataset)
    f = frame.filter(pl.col("indicator") == "CBPOL").sort(["provider_geo", "period_start"])
    rep: dict[str, Any] = {"input_rows": f.height, "areas": 0, "changes": 0}
    cols = f.select("provider_geo", "iso3", "period_start", "value", "raw_sha256", "selector").rows()
    by_geo: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for r in cols:
        if r[3] is not None:
            by_geo[r[0]].append(r)
    for geo, rows in sorted(by_geo.items()):
        rep["areas"] += 1
        changes: list[tuple[int, int]] = []  # (index of previous obs, index of change obs)
        for i in range(1, len(rows)):
            if Decimal(rows[i][3]) != Decimal(rows[i - 1][3]):
                changes.append((i - 1, i))
        for n, (p, i) in enumerate(changes):
            new, prev = rows[i], rows[p]
            nxt = changes[n + 1][1] if n + 1 < len(changes) else None
            delta = Decimal(new[3]) - Decimal(prev[3])
            flags: dict[str, Any] = {
                "previous_value": prev[3], "previous_date": prev[2].isoformat(), "change_pp": _pp(delta),
                "direction": "increase" if delta > 0 else "decrease",
                "held_until": rows[nxt - 1][2].isoformat() if nxt is not None else None,
                "derived": "REVA: the BIS daily policy rate series differs from its previous observation; not "
                "necessarily a policy decision (see bis_compilation)",
                "bis_compilation": compilation.get(geo),
            }  # fmt: skip
            gap = (new[2] - prev[2]).days - 1
            if gap:
                flags["gap_days"] = gap
            buf.add(
                indicator="CBPOL_CHANGE", provider_geo=geo, period=(new[2], date.fromordinal(new[2].toordinal() + 1)),
                value=new[3], unit="Per cent per year", flags=canon(flags), raw_sha256=new[4], iso3=new[1],
                selector=canon(derived_from=BIS.input_key, artifact_sha256=input_sha,
                               cells=[json.loads(prev[5]), json.loads(new[5])]),
            )  # fmt: skip
            rep["changes"] += 1
    return buf.frame(), rep


def cit_changes(frame: Any, *, input_sha: str) -> tuple[Any, dict[str, Any]]:
    import polars as pl

    buf = RowBuffer(CIT.atlas_source, CIT.dataset)
    f = frame.filter(pl.col("indicator").is_in(CIT_INDICATORS) & pl.col("value").is_not_null()).sort(
        ["indicator", "provider_geo", "period_start"]
    )
    rep: dict[str, Any] = {"input_rows": f.height, "changes": 0, "series": 0}
    rows_by: dict[tuple[str, str], list[tuple[Any, ...]]] = defaultdict(list)
    for r in f.select("indicator", "provider_geo", "iso3", "period_start", "period_end", "value", "unit", "raw_sha256",
                      "selector").rows():  # fmt: skip
        if (r[4] - r[3]).days not in (365, 366):
            raise vc.err(f"{CIT.input_key}: {r[0]} {r[1]} {r[3]} is not an annual period")
        rows_by[(r[0], r[1])].append(r)
    for (ind, geo), rows in sorted(rows_by.items()):
        rep["series"] += 1
        for prev, new in pairwise(rows):
            delta = Decimal(new[5]) - Decimal(prev[5])
            if delta == 0:
                continue
            flags: dict[str, Any] = {
                "previous_value": prev[5], "previous_year": prev[3].year, "change_pp": _pp(delta),
                "direction": "increase" if delta > 0 else "decrease",
                "derived": "REVA: OECD statutory CIT rate differs from the previous reported year",
            }  # fmt: skip
            if new[3].year - prev[3].year != 1:
                flags["years_between"] = new[3].year - prev[3].year
            head, _, rest = ind.partition("|")
            buf.add(
                indicator=f"{head}_CHANGE|{rest}", provider_geo=geo, period=(new[3], new[4]), value=new[5],
                unit=new[6], flags=canon(flags), raw_sha256=new[7], iso3=new[2],
                selector=canon(derived_from=CIT.input_key, artifact_sha256=input_sha,
                               cells=[json.loads(prev[8]), json.loads(new[8])]),
            )  # fmt: skip
            rep["changes"] += 1
    return buf.frame(), rep


def compilation_texts(frame: Any) -> dict[str, str]:
    import polars as pl

    f = frame.filter(pl.col("indicator") == "CBPOL_SERIES|attr=COMPILATION")
    return {g: t for g, t in f.select("provider_geo", "value_text").rows() if t}


def _spec(d: Derivation) -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=d.name,
        version=VERSION,
        source_key=d.budget_key,
        budget_key=d.budget_key,
        atlas_source=d.atlas_source,
        provider=d.provider,
        licence=d.licence,
        allowed_hosts=(d.host,),
        datasets=(d.dataset,),
        dataset_ids=(d.input_key,),
        cadence_seconds=7 * 86400,
        planned_requests=1,
        max_response_bytes=1_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=vc.code_digest(sys.modules[__name__]),
        field_mappings={"derived_from": d.input_key, "table": f"{d.atlas_source}.{d.dataset}"},
        notes="No request: derived from the latest registered input atlas table of the run's origin.",
    )


def atlas_source() -> list[AtlasSourceSpec]:
    return [_spec(BIS), _spec(CIT)]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.atlas.tables import read_atlas_table

    d = BIS if run.spec.name == BIS.name else CIT
    if not run.wants(d.dataset):
        return {"skipped": True}
    inp = vc.latest_atlas_table(run.engine, d.input_key, run.origin)
    if inp is None:
        return {"skipped": f"{d.input_key} is not registered with origin {run.origin}: ingest it first"}
    frame = read_atlas_table(run.store, inp["artifact_sha256"])
    rights_extra: dict[str, Any] = {"derived_from": inp}
    if d is BIS:
        ser = vc.latest_atlas_table(run.engine, SERIES_KEY, run.origin)
        comp = compilation_texts(read_atlas_table(run.store, ser["artifact_sha256"])) if ser else {}
        rights_extra["series_texts_from"] = ser
        out, rep = bis_changes(frame, input_sha=inp["artifact_sha256"], compilation=comp)
        note = ("DERIVED by REVA from atlas.bis.cbpol_d (BIS daily central bank policy rates): one row per area and day "
                "where the rate differs from the previous observation, previous value/date, change in percentage "
                "points, held_until, BIS COMPILATION text. Series changes, not necessarily policy decisions.")  # fmt: skip
        licence = "BIS terms of permitted use of BIS statistics (cite the BIS as the source)"
    else:
        out, rep = cit_changes(frame, input_sha=inp["artifact_sha256"])
        note = ("DERIVED by REVA from atlas.oecd.cts_cit (OECD Corporate Tax Statistics): one row per country and year "
                "where the standard statutory CIT rate (CIT_C combined, CIT central) differs from the previous reported "
                "year; previous value/year and change in percentage points.")  # fmt: skip
        licence = "OECD open by default (CC BY 4.0 since 2024-07-01); cite the OECD"
    raws = sorted(set(out.get_column("raw_sha256").to_list())) if out.height else []
    res: dict[str, Any] = {"input": inp, "report": rep}
    if not raws:
        res["atlas"] = {"skipped": "no change rows"}
        return res
    r = run.register(dataset=d.dataset, frame=out, raw_sha256s=raws, coverage_note=note,
                     rights={"licence": licence, "derived_by": "REVA (agent V6POLITICS)", **rights_extra})  # fmt: skip
    res["atlas"] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    return res


__all__ = ["BIS", "CIT", "atlas_ingest", "atlas_source", "bis_changes", "cit_changes", "compilation_texts"]
