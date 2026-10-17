"""IMF IMTS **monthly** bilateral goods trade -> atlas tables (V6, agent V6TRADEHF).

Monthly companion of the annual IMTS table of :mod:`atlas_imf` (``imf.imts``, A-IMF). Same IMF data portal SDMX 2.1
API (``https://api.imf.org/external/sdmx/2.1``, open, no key), same source key ``imf.sdmx`` and budget, same
SDMX-ML parser (:func:`atlas_imf.parse_sdmx`), same geography scheme ``imf`` and the same indicator convention
``<INDICATOR>|partner=<IMF code>`` with ``flags.partner_iso3`` (so the annual and monthly cells of one flow read
alike). Dataflow ``IMF.STA:IMTS`` "International Trade in Goods (by partner country)", successor of DOTS.

==============  ======================================================================================
dataset         content (frequency as published, never averaged or summed)
==============  ======================================================================================
imts_world_m    every reporter's goods exports FOB (``XG_FOB_USD``) and imports CIF (``MG_CIF_USD``) to /
                from partner ``G001`` (World), US dollars, monthly, from ``world_start`` (default 2000-01)
imts_m          every reporter x every partner (bilateral), same two flows, monthly, from ``start``
                (default 2026-01: the disk bound of this container; see docs/dev/v6_trade_hf.md)
==============  ======================================================================================

Requests: one all-reporter world-total request (its ``COUNTRY`` set is the reporter list: no codelist guess),
then the bilateral flow in chunks of ``CHUNK`` reporters (an all-reporter bilateral request answered in 3.5 min
on 2026-10-17; 30-reporter chunks answer in ~30 s).

Honesty rules (as :mod:`atlas_imf_hf`):

* ``OBS_VALUE`` verbatim as a strict decimal; absent -> no cell (counted). ``SCALE`` is the IMF display scale,
  kept in ``flags.display_scale`` and never applied (values are in US dollars, full units).
* ``2026-M03`` -> ``[2026-03-01, 2026-04-01)``; ``flags.frequency = "M"``.
* IMTS has no seasonal-adjustment dimension or attribute: ``flags.seasonal_adjustment = "not_stated_by_provider"``.
* ``DERIVATION_TYPE`` kept with its quoted IMF label (``M`` "Mixed-type data" on every observation measured
  2026-10-17: reported data and IMF estimates for non-reporting economies are not separated per observation);
  ``STATUS`` kept verbatim; projections / estimates flagged as in :mod:`atlas_imf_hf`.
* Partner aggregates (``G001`` World, regional groups) stay unmapped (``partner_iso3`` null, ``partner_unmapped``).
* Exports reported by A and imports reported by B are mirror flows: never merged, averaged or reconciled.
* Vintages: the response's ``UPDATE_DATE`` is in the coverage note; a changed release becomes a new version of the
  atlas table (the atlas revision mechanism keeps earlier vintages for as-of backtests).

    uv run reva atlas ingest atlas.imf_imts_m [--dataset imts_m] [--option start=2025-01] [--fixture]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import atlas_imf as imf
from reva.adapters.connectors import atlas_imf_hf as ih
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.imf_imts_m"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_imf_imts_m/1"
SOURCE = "imf"
SOURCE_KEY = imf.SOURCE_KEY
HOST = imf.HOST
BASE = imf.BASE
GEO_SCHEME = imf.GEO_SCHEME
MEDIA = ih.MEDIA
MAX_BYTES = 60_000_000
LICENCE = imf.LICENCE
RIGHTS: dict[str, Any] = {
    **ih.RIGHTS,
    "attribution": "International Monetary Fund, International Trade in Goods "
    "(by partner country) (IMTS), https://data.imf.org/en/datasets/IMF.STA:IMTS",
}

FLOW = "IMTS"
INDICATORS = {"XG_FOB_USD": "exports of goods, FOB", "MG_CIF_USD": "imports of goods, CIF"}
WORLD = "G001"
DIMS = ("COUNTRY", "INDICATOR", "COUNTERPART_COUNTRY", "FREQUENCY")
SERIES_ATTRS = ("METHODOLOGY", "OVERLAP")
CHUNK = 30
WORLD_START = "2000-01"
BILATERAL_START = "2026-01"
DATASETS = ("imts_world_m", "imts_m")
TITLES = {
    "imts_world_m": "IMF IMTS monthly goods exports FOB / imports CIF with the World (G001), every reporter",
    "imts_m": "IMF IMTS monthly bilateral goods exports FOB / imports CIF, every reporter x partner",
}
#: offline fixtures: small real responses (captured 2026-10-17)
FIXTURE_REPORTERS = ("FRA", "KOS")
FIXTURE_WORLD_START = "2025-01"
FIXTURE_START = "2026-05"
_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


class ImtsMError(SourceResponseError):
    def __init__(self, msg: str, **ctx: Any) -> None:
        super().__init__(msg, retryable=False, kind="parse_error", **ctx)


def _key(reporters: Sequence[str], partner: str = "") -> str:
    return f"{'+'.join(reporters)}.{'+'.join(INDICATORS)}.{partner}.M"


def world_url(start: str, reporters: Sequence[str] = ()) -> str:
    return f"{BASE}/data/IMF.STA,{FLOW}/{_key(reporters, WORLD)}?startPeriod={start}"


def bilateral_url(reporters: Sequence[str], start: str) -> str:
    return f"{BASE}/data/IMF.STA,{FLOW}/{_key(reporters)}?startPeriod={start}"


def chunks(reporters: Sequence[str], size: int = CHUNK) -> list[list[str]]:
    rs = sorted(set(reporters))
    return [rs[i : i + size] for i in range(0, len(rs), size)]


def check_month(v: Any, name: str) -> str:
    s = str(v).strip()
    if not _MONTH.match(s):
        raise ImtsMError("run option must be YYYY-MM", option=name, value=s[:20])
    return s


@dataclass
class ImtsReport:
    dataset: str
    series: int = 0
    obs: int = 0
    cells: int = 0
    no_value: int = 0
    zeros: int = 0
    kinds: Counter[str] = field(default_factory=Counter)
    derivation: Counter[str] = field(default_factory=Counter)
    reporters: set[str] = field(default_factory=set)
    partners: set[str] = field(default_factory=set)
    flow_ref: str = ""
    update_dates: set[str] = field(default_factory=set)
    periods: tuple[str, str] | None = None
    requests: int = 0
    failed_chunks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "flow": self.flow_ref,
            "update_dates": sorted(self.update_dates),
            "requests": self.requests,
            "series": self.series,
            "obs": self.obs,
            "cells": self.cells,
            "no_value_obs": self.no_value,
            "reported_zeros": self.zeros,
            "estimate_kinds": dict(self.kinds),
            "derivation_types": dict(self.derivation),
            "reporters": len(self.reporters),
            "unmapped_reporters": sorted(g for g in self.reporters if country_iso3(g, GEO_SCHEME) is None),
            "partners": len(self.partners),
            "unmapped_partners": sorted(p for p in self.partners if country_iso3(p, GEO_SCHEME) is None),
            "periods": list(self.periods) if self.periods else None,
            "failed_chunks": self.failed_chunks,
        }


def parse_response(
    body: bytes, sha: str, label: str, rep: ImtsReport, cols: dict[str, list[Any]], *, world_only: bool
) -> list[str]:
    """Append the cells of one IMTS SDMX-ML response to ``cols``; return the reporters seen (sorted)."""
    try:
        msg = imf.parse_sdmx(body)
    except imf.SdmxError as e:
        raise ImtsMError(f"IMF {FLOW}: {e}") from e
    if msg.structure.get("id") != FLOW:
        raise ImtsMError(f"IMF {FLOW}: response is for another dataflow", got=msg.flow_ref)
    rep.flow_ref = msg.flow_ref
    rep.requests += 1
    if msg.dataset_attrs.get("UPDATE_DATE"):
        rep.update_dates.add(msg.dataset_attrs["UPDATE_DATE"])
    seen: set[str] = set()
    names = {g["INDICATOR"]: g["SERIES_NAME"] for g in msg.groups if g.get("INDICATOR") and g.get("SERIES_NAME")}
    pmap: dict[str, str | None] = {}
    for sattrs, obs in msg.series:
        rep.series += 1
        missing = [d for d in DIMS if d not in sattrs]
        if missing:
            raise ImtsMError(f"IMF {FLOW}: series without dimension(s)", missing=missing)
        geo, ind, partner, freq = (sattrs[d] for d in DIMS)
        if ind not in INDICATORS:
            raise ImtsMError(f"IMF {FLOW}: unrequested indicator", indicator=ind[:40])
        if freq != "M":
            raise ImtsMError(f"IMF {FLOW}: unrequested frequency", freq=freq[:4])
        if world_only and partner != WORLD:
            raise ImtsMError(f"IMF {FLOW}: unrequested partner in the world-total request", partner=partner[:20])
        seen.add(geo)
        if partner not in pmap:
            pmap[partner] = country_iso3(partner, GEO_SCHEME)
        base: dict[str, Any] = {
            "frequency": "M",
            "seasonal_adjustment": "not_stated_by_provider",
            "partner": partner,
            "partner_iso3": pmap[partner],
            "flow_valuation": "FOB" if ind.startswith("XG") else "CIF",
            "flow_label": INDICATORS[ind],
        }
        if ind in names:
            base["series_name"] = names[ind]  # the IMF's own series name (group attribute SERIES_NAME)
        if pmap[partner] is None:
            base["partner_unmapped"] = True
        scale = sattrs.get("SCALE")
        if scale not in (None, "", "0"):
            base["display_scale"] = scale
        for a in SERIES_ATTRS:
            if sattrs.get(a):
                base[a.lower()] = sattrs[a]
        skey = ".".join(sattrs[d] for d in DIMS)
        indicator = f"{ind}|partner={partner}"
        for o in obs:
            rep.obs += 1
            tp = o.get("TIME_PERIOD", "")
            val = imf.decimal_text(o.get("OBS_VALUE"))
            if val is None:
                rep.no_value += 1
                continue
            start, end = ih.imf_period("M", tp)
            flags = dict(base)
            dt = o.get("DERIVATION_TYPE")
            if dt:
                flags["derivation_type"] = dt
                flags["derivation_type_label"] = ih.DERIVATION_LABELS.get(dt, "(not in the quoted IMF codelist)")
                rep.derivation[dt] += 1
            if o.get("STATUS"):
                flags["provider_status"] = o["STATUS"]
            if o.get("CONFIDENTIALITY_STATUS") not in (None, "", "F"):
                flags["confidentiality_status"] = o["CONFIDENTIALITY_STATUS"]
            k = ih._kind(o)
            if k:
                flags["estimate_kind"] = k
                rep.kinds[k] += 1
            if float(val) == 0:
                rep.zeros += 1
            rep.reporters.add(geo)
            rep.partners.add(partner)
            cols["indicator"].append(indicator)
            cols["provider_geo"].append(geo)
            cols["period_start"].append(start)
            cols["period_end"].append(end)
            cols["value"].append(val)
            cols["unit"].append("US dollars")
            cols["flags"].append(fc.canon(flags))
            cols["raw_sha256"].append(sha)
            cols["selector"].append(fc.canon({"file": label, "flow": msg.flow_ref, "series": skey, "time_period": tp}))
    return sorted(seen)


def frame(cols: Mapping[str, list[Any]], dataset: str, rep: ImtsReport) -> pl.DataFrame:
    if not cols["indicator"]:
        raise ImtsMError(f"IMF {FLOW}: no cell with a value", dataset=dataset)
    rep.cells = len(cols["indicator"])
    starts = cols["period_start"]
    rep.periods = (min(starts).isoformat(), max(starts).isoformat())
    df = ih._frame(cols, dataset)
    dup = df.select(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated().sum()).item()
    if dup:
        raise ImtsMError(f"IMF {FLOW}: {dup} duplicate cells", dataset=dataset)
    return df


def coverage_note(dataset: str, rep: ImtsReport, start: str) -> str:
    d = rep.as_dict()
    return (
        f"{TITLES[dataset]}. IMF IMF.STA:{FLOW} 'International Trade in Goods (by partner country)' ({rep.flow_ref};"
        f" dataset UPDATE_DATE {', '.join(d['update_dates'])}); startPeriod {start}; {rep.requests} request(s);"
        f" {rep.series} series, {rep.cells} cells ({rep.no_value} observations without a value are not cells),"
        f" {len(rep.reporters)} reporters, {len(rep.partners)} partners, periods {d['periods']}."
        f" Derivation types: {dict(rep.derivation)}; estimate kinds: {dict(rep.kinds)}. US dollars, full units"
        " (flags.display_scale is the IMF display scale, not applied). Seasonal adjustment not stated by the provider."
        f" Exports FOB as reported by the exporter and imports CIF as reported by the importer are mirror flows,"
        f" never reconciled here. Unmapped reporters (aggregates, kept): {', '.join(d['unmapped_reporters']) or 'none'}."
        + (f" INCOMPLETE: reporter chunks that failed: {', '.join(rep.failed_chunks)}." if rep.failed_chunks else "")
        + f" Licence: {LICENCE}."
    )[:4000]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="International Monetary Fund (data portal SDMX 2.1 API), IMTS monthly bilateral goods trade",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=(f"IMF.STA:{FLOW}",),
        cadence_seconds=30 * 86_400,
        planned_requests=10,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(imf, ih, sys.modules[__name__]),
        field_mappings={
            "geo_scheme": GEO_SCHEME,
            "tables": {d: f"{SOURCE}.{d}" for d in DATASETS},
            "time_semantics": "calendar months as published (no averaging, no summing)",
            "indicator": "<XG_FOB_USD|MG_CIF_USD>|partner=<IMF code>; flags.partner_iso3",
        },
        notes="1 world-total request (~10 MB from 2000-01) + ceil(reporters/30) bilateral requests"
        " (~37 MB for 2026-01..2026-08, measured 2026-10-17).",
    )


async def _fetch(run: Any, url: str, key: str) -> tuple[bytes, str]:
    raw = await run.get(
        url,
        key=key,
        media_types=MEDIA,
        headers={"Accept": imf.ACCEPT},
        max_bytes=MAX_BYTES,
        params={"kind": "sdmx_data", "flow": f"IMF.STA:{FLOW}"},
    )
    if not raw.body:
        raise SourceResponseError(f"IMF {FLOW}: empty response body", retryable=True)
    return raw.body, run.archive(raw)


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    fixture = run.origin == "test_fixture"
    opts = run.options or {}
    world_start = FIXTURE_WORLD_START if fixture else check_month(opts.get("world_start", WORLD_START), "world_start")
    start = FIXTURE_START if fixture else check_month(opts.get("start", BILATERAL_START), "start")
    out: dict[str, Any] = {"fetched": [], "errors": [], "tables": [], "reports": {}}
    built: dict[str, tuple[pl.DataFrame, ImtsReport, list[str], str]] = {}

    # 1. world totals (also the reporter list of the bilateral requests)
    reporters: list[str] = []
    rep_w = ImtsReport("imts_world_m")
    cols_w: dict[str, list[Any]] = {c: [] for c in fc._COLS}
    try:
        url = world_url(world_start, FIXTURE_REPORTERS if fixture else ())
        body, sha = await _fetch(run, url, f"data:{FLOW}:world")
        out["fetched"].append({"item": f"data:{FLOW}:world", "bytes": len(body), "sha256": sha})
        reporters = parse_response(body, sha, f"IMF.STA_{FLOW}_world_M.xml", rep_w, cols_w, world_only=True)
        if run.wants("imts_world_m"):
            built["imts_world_m"] = (frame(cols_w, "imts_world_m", rep_w), rep_w, [sha], world_start)
    except BudgetExhausted:
        raise
    except Exception as e:
        out["errors"].append({"dataset": "imts_world_m", "error": type(e).__name__, "message": str(e)[:300]})

    # 2. bilateral, chunked by reporter
    if run.wants("imts_m") and reporters:
        rep_b = ImtsReport("imts_m")
        cols_b: dict[str, list[Any]] = {c: [] for c in fc._COLS}
        shas: list[str] = []
        failed: list[list[str]] = []
        for part in chunks(reporters):
            key = f"data:{FLOW}:{part[0]}-{part[-1]}"
            try:
                body, sha = await _fetch(run, bilateral_url(part, start), key)
                out["fetched"].append({"item": key, "bytes": len(body), "sha256": sha})
                parse_response(body, sha, f"IMF.STA_{FLOW}_{part[0]}-{part[-1]}_M.xml", rep_b, cols_b, world_only=False)
                shas.append(sha)
            except BudgetExhausted:
                raise
            except Exception as e:
                failed.append(part)
                out["errors"].append(
                    {"dataset": "imts_m", "chunk": key, "error": type(e).__name__, "message": str(e)[:300]}
                )
        if shas:
            try:
                df = frame(cols_b, "imts_m", rep_b)
                built["imts_m"] = (df, rep_b, shas, start)
                rep_b.failed_chunks = [f"{p[0]}-{p[-1]}" for p in failed]
            except Exception as e:
                out["errors"].append({"dataset": "imts_m", "error": type(e).__name__, "message": str(e)[:300]})

    for dataset, (df, rep, shas, st) in built.items():
        try:
            reg = run.register(
                dataset=dataset,
                frame=df,
                raw_sha256s=shas,
                coverage_note=coverage_note(dataset, rep, st),
                geo_scheme=GEO_SCHEME,
                rights=dict(RIGHTS),
            )
        except BudgetExhausted:
            raise
        except Exception as e:
            details = getattr(e, "details", None)
            out["errors"].append(
                {
                    "dataset": dataset,
                    "stage": "register",
                    "error": type(e).__name__,
                    "message": str(e)[:500],
                    "details": json.loads(json.dumps(details, default=str)) if details else None,
                }
            )
            continue
        out["reports"][dataset] = rep.as_dict()
        out["tables"].append(
            {
                "table": f"{SOURCE}.{dataset}",
                "dataset_key": reg.get("dataset_key"),
                "created": reg.get("created"),
                "rows": reg.get("row_count"),
                "countries": (reg.get("coverage") or {}).get("countries"),
                "artifact_sha256": reg.get("artifact_sha256"),
            }
        )
    out["ok"] = not out["errors"]
    return out


__all__ = [
    "CONNECTOR_NAME",
    "DATASETS",
    "atlas_ingest",
    "atlas_source",
    "bilateral_url",
    "chunks",
    "parse_response",
    "world_url",
]
