"""V8 series queries over one atlas snapshot (read-only; the atlas query layer's DuckDB file).

* :func:`series_meta`: what a series is (labels, unit, precisions, periods, levels, licences), cached per series.
* :func:`map_slice`: every place's cell for one period (ADM0 countries or sub-national provider geographies),
  with the typed reason of every country without a value.
* :func:`place_series`: one place's cells for every period.
* :func:`multi_series`: N series x M countries aligned by period (Comparer), with transform rules and, on request,
  the atlas population of the same countries for a per-capita computation.

Cells keep the exact decimal strings of the source. Missing is a reason, never zero. Projections are flagged (the
atlas projection rules) and, unless asked, never answer a slice.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from datetime import date
from functools import lru_cache
from typing import Any

from reva.atlas.projection import projection_sql, vocabulary
from reva.atlas.query import CELL_SELECT, AtlasQuery, licence_summary
from reva.atlas.tables import CROSSWALK_PATH, known_country_keys
from reva.kernel.errors import ValidationProblem
from reva.v8.series.core import (
    PRECISIONS,
    SPAN_SQL,
    PeriodSel,
    default_precision,
    parse_indicator,
    parse_period,
    period_code,
    period_label_fr,
    scale_hint,
    split_series_key,
    transform_rules,
)
from reva.v8.series.index import SeriesIndex

#: Provider geography codes that are sub-national units (NUTS 1-3, ISO 3166-2, GeoNames admin1 codes).
SUBNATIONAL_RE = re.compile(r"^(?:[A-Z]{2}[0-9A-Z]{1,3}|[A-Z]{2}-[A-Z0-9]{1,3}|[A-Z]{2}\.[0-9A-Z]{1,3})$")

MISSING_FR = {
    "provider": "la source publie une cellule sans valeur",
    "projection_hidden": "seule une projection existe pour cette période (masquée : jamais mêlée aux observations)",
    "not_this_period": "pas de cellule pour cette période (la série a des valeurs à d’autres périodes)",
    "other_granularity": "valeurs à une autre granularité (mensuelle, trimestrielle…) pour cette année",
    "not_in_series": "jamais rapporté par cette série (absent, pas zéro)",
}

PROVIDER_MISSING_FR = {
    "not_reported": "non rapporté par la source",
    "confidential": "confidentiel",
    "not_applicable": "sans objet",
    "not_available": "non disponible chez la source",
    "unreliable": "jugé non fiable par la source",
    "provider_missing_code": "code « manquant » du fournisseur",
}

POPULATION_HEADLINE = "reva.population.total"


# --------------------------------------------------------------------------- places


@lru_cache(maxsize=1)
def places() -> list[dict[str, Any]]:
    """The country referential (crosswalk): key, iso2, English label, French label when known, UN region."""
    doc = json.loads(CROSSWALK_PATH.read_text())
    return [
        {
            "iso3": e["key"],
            "iso2": e.get("iso2"),
            "label": e.get("label") or e["key"],
            "label_fr": e.get("label_fr"),
            "region": e.get("m49_region"),
        }
        for e in doc["entities"]
    ]


@lru_cache(maxsize=1)
def place_labels() -> dict[str, str]:
    return {p["iso3"]: p["label_fr"] or p["label"] for p in places()}


# --------------------------------------------------------------------------- resolution


def resolve(
    idx: SeriesIndex,
    *,
    series: str | None = None,
    table: str | None = None,
    measure: str | None = None,
    filters: dict[str, str] | None = None,
) -> tuple[int, str, str, str]:
    """(sid, source, dataset, indicator) of a series key, or of a table + measure + filters (open filters at their
    default value). Raises ValidationProblem (422) when nothing matches."""
    if series:
        parts = split_series_key(series)
        sid = idx.sid_of.get(parts) if parts else None
        if parts is None or sid is None:
            raise ValidationProblem("unknown atlas series in this snapshot", series=series[:200])
        return sid, *parts
    if not table or measure is None:
        raise ValidationProblem("give either series or table + measure")
    m = idx.measure(table, measure)
    if m is None:
        raise ValidationProblem("unknown table/measure in this snapshot", table=table[:80], measure=measure[:120])
    v = m.resolve(filters or {})
    if v is None:
        raise ValidationProblem("no series of this measure matches the filters", filters=json.dumps(filters)[:200])
    src, ds = table.split(".", 1)
    return v.sid, src, ds, v.indicator


# --------------------------------------------------------------------------- meta (cached per series)

_META: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()
_PERIODS: OrderedDict[tuple[str, int, str], list[dict[str, Any]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_MAX = 2048


def _cache_get(cache: OrderedDict[Any, Any], key: Any) -> Any:
    with _CACHE_LOCK:
        v = cache.get(key)
        if v is not None:
            cache.move_to_end(key)
        return v


def _cache_put(cache: OrderedDict[Any, Any], key: Any, value: Any) -> None:
    with _CACHE_LOCK:
        cache[key] = value
        while len(cache) > _MAX:
            cache.popitem(last=False)


def _table_recorded(q: AtlasQuery, source: str, dataset: str) -> str | None:
    dk = f"atlas.{source}.{dataset}"
    rec = [str(t.get("recorded_at"))[:10] for t in q.tables.values() if t.get("dataset_key") == dk]
    rec = [r for r in rec if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r)]
    return max(rec) if rec else None


def _proj_sql(q: AtlasQuery, source: str, dataset: str) -> str:
    rec = _table_recorded(q, source, dataset)
    exempt = f"{source}.{dataset}" in vocabulary().date_rule_exempt_tables
    return projection_sql(
        "x", period=True, recorded_sql=f"DATE '{rec}'" if rec else None, table_exempt_sql="TRUE" if exempt else None
    )


def _context(indicator: str, concept: Any) -> str:
    """Text the per-capita rule reads besides the unit: the indicator code and the concept's labels and id."""
    if concept is None:
        return indicator
    return " ".join([indicator, concept.id, *concept.labels.values()])


def series_meta(q: AtlasQuery, idx: SeriesIndex, sid: int) -> dict[str, Any]:
    key = (q.snapshot.sha256, sid)
    hit = _cache_get(_META, key)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    src, ds, ind = q.series_of[sid]
    measure, dims = parse_indicator(ind)
    skey = f"{src}.{ds}.{ind}"
    spans = q._rows(
        f"""SELECT {SPAN_SQL} AS span, (x.year IS NOT NULL) AS att, count(*) AS n
            FROM cells_s x WHERE x.sid = ? AND x.has_value GROUP BY 1, 2""",
        [sid],
    )
    counts = {p: 0 for p in PRECISIONS}
    for r in spans:
        if r["att"]:
            counts["year"] += int(r["n"])
        if r["span"] in ("month", "quarter", "semester"):
            counts[r["span"]] += int(r["n"])
        elif r["span"] == "day" and not r["att"]:
            counts["day"] += int(r["n"])
    units = q._rows(
        """SELECT x.unit, count(*) AS n, count(x.value) AS n_num, count(x.value_text) AS n_text,
                  min(TRY_CAST(x.value AS DOUBLE)) AS vmin, max(TRY_CAST(x.value AS DOUBLE)) AS vmax,
                  count(DISTINCT x.iso3) AS n_iso, count(DISTINCT x.provider_geo) AS n_geo
           FROM cells_s x WHERE x.sid = ? AND x.has_value GROUP BY 1 ORDER BY 2 DESC""",
        [sid],
    )
    geo_rows = q._rows("SELECT DISTINCT x.iso3, x.provider_geo FROM cells_s x WHERE x.sid = ? AND x.has_value", [sid])
    shas = q._rows("SELECT DISTINCT x.table_sha FROM cells_s x WHERE x.sid = ? LIMIT 20", [sid])
    countries = sorted({r["iso3"] for r in geo_rows if r["iso3"]})
    subnational = sorted({r["provider_geo"] for r in geo_rows if r["iso3"] and SUBNATIONAL_RE.match(r["provider_geo"])})
    aggregates = sorted({r["provider_geo"] for r in geo_rows if not r["iso3"]})
    concept = idx.concept_for(skey)
    unit = units[0]["unit"] if units else None
    n_num = sum(int(u["n_num"]) for u in units)
    vmin = min((u["vmin"] for u in units if u["vmin"] is not None), default=None)
    prec = default_precision(counts)
    meta = {
        "series": skey,
        "table": f"{src}.{ds}",
        "source": src,
        "dataset": ds,
        "indicator": ind,
        "measure": measure,
        "filters": [{"key": k, "value": v} for k, v in dims],
        "concept": (
            {
                "id": concept.id,
                "labels": dict(concept.labels),
                "definition": concept.definition,
                "unit": concept.unit,
                "theme": concept.theme,
                "review_status": "provider_declared_not_human_reviewed",
            }
            if concept
            else None
        ),
        "label": (concept.labels.get("fr") or concept.labels.get("en")) if concept else None,
        "unit": unit,
        "units": [{"unit": u["unit"], "cells": int(u["n"])} for u in units],
        "value_kind": "numeric" if n_num else ("text" if units else "none"),
        "min_value": vmin,
        "precisions": {p: n for p, n in counts.items() if n},
        "default_precision": prec,
        "countries": countries,
        "levels": {"adm0": len(countries), "adm1": len(subnational)},
        "aggregates": aggregates[:60],
        "recorded": _table_recorded(q, src, ds),
        "licences": licence_summary({"source": src, "rights": q.rights_of(t["table_sha"], src)} for t in shas),
        "transforms": {
            k: {"ok": r.ok, "reason": r.reason}
            for k, r in transform_rules(
                unit=unit,
                precision=prec,
                numeric=n_num > 0,
                min_value=vmin,
                countries=bool(countries),
                context=_context(ind, concept),
            ).items()
        },
    }
    _cache_put(_META, key, meta)
    return meta


def periods(q: AtlasQuery, sid: int, precision: str) -> list[dict[str, Any]]:
    """Periods of a series at ``precision`` with, per period, the number of places with an observed value and
    with a projection only."""
    key = (q.snapshot.sha256, sid, precision)
    hit = _cache_get(_PERIODS, key)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    src, ds, _ = q.series_of[sid]
    p_sql = _proj_sql(q, src, ds)
    if precision == "year":
        rows = q._rows(
            f"""SELECT x.year AS y, CAST(min(x.period_start) AS VARCHAR) AS ps,
                       count(*) FILTER (WHERE x.has_value AND NOT {p_sql}) AS obs,
                       count(*) FILTER (WHERE x.has_value AND {p_sql}) AS proj
                FROM cells_s x WHERE x.sid = ? AND x.year IS NOT NULL AND x.iso3 IS NOT NULL
                GROUP BY 1 ORDER BY 1""",
            [sid],
        )
        out = [
            {"code": str(r["y"]), "label": str(r["y"]), "obs": int(r["obs"]), "proj": int(r["proj"])}
            for r in rows
            if r["obs"] or r["proj"]
        ]
    elif precision in PRECISIONS:
        cond = f"{SPAN_SQL} = ?" + (" AND x.year IS NULL" if precision == "day" else "")
        rows = q._rows(
            f"""SELECT strftime(x.period_start, '%Y-%m-%d') AS ps,
                       count(*) FILTER (WHERE x.has_value AND NOT {p_sql}) AS obs,
                       count(*) FILTER (WHERE x.has_value AND {p_sql}) AS proj
                FROM cells_s x WHERE x.sid = ? AND x.iso3 IS NOT NULL AND year(x.period_start) > 0 AND {cond}
                GROUP BY 1 ORDER BY 1""",
            [sid, precision],
        )
        out = []
        for r in rows:
            if not (r["obs"] or r["proj"]):
                continue
            code = period_code(precision, r["ps"])
            out.append(
                {"code": code, "label": period_label_fr(precision, code), "obs": int(r["obs"]), "proj": int(r["proj"])}
            )
    else:
        raise ValidationProblem("unknown precision", precision=precision[:20])
    _cache_put(_PERIODS, key, out)
    return out


# --------------------------------------------------------------------------- cells → compact points


def _cell(q: AtlasQuery, r: dict[str, Any]) -> dict[str, Any]:
    """The atlas query layer's cell (provenance, rights, projection rule) plus the row's country key."""
    return {**q._cell(r), "iso3": r.get("iso3")}


class _Lic:
    """Licences of an answer, deduplicated; each point carries the index of its licence."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._ix: dict[tuple[str, str], int] = {}

    def of(self, cell: dict[str, Any]) -> int | None:
        r = cell.get("rights")
        if not r:
            return None
        k = (str(cell.get("source")), str(r.get("licence")))
        if k not in self._ix:
            self._ix[k] = len(self.items)
            self.items.append({"source": cell.get("source"), **r})
        return self._ix[k]


_FLAG_KEEP = ("estimate_kind", "missing", "note", "status", "label_year", "qa", "break", "provisional", "estimated")


def _point(cell: dict[str, Any], precision: str, lic: _Lic) -> dict[str, Any]:
    raw_flags = cell.get("flags")
    flags: dict[str, Any] = raw_flags if isinstance(raw_flags, dict) else {}
    has = cell.get("value") is not None or cell.get("value_text") is not None
    miss = None
    if not has:
        reason = flags.get("missing")
        miss = str(reason) if isinstance(reason, str) else "provider"
    code = period_code(precision, cell["period_start"], cell.get("year") if precision == "year" else None)
    return {
        "p": code,
        "ps": cell["period_start"],
        "pe": cell["period_end"],
        "basis": cell.get("period_basis"),
        "v": cell.get("value"),
        "t": cell.get("value_text"),
        "unit": cell.get("unit"),
        "proj": bool(cell.get("projection")),
        "miss": miss,
        "flags": {k: flags[k] for k in flags if k in _FLAG_KEEP or k.startswith("qa")},
        "geo": cell.get("provider_geo"),
        "iso3": cell.get("iso3"),
        "series": cell.get("series"),
        "raw": (cell.get("provenance") or {}).get("raw_sha256"),
        "table_sha": (cell.get("provenance") or {}).get("table_artifact_sha256"),
        "lic": lic.of(cell),
    }


def _period_where(sel: PeriodSel) -> tuple[str, list[Any]]:
    if sel.precision == "year":
        return "x.year = ?", [sel.year]
    cond = f"x.period_start = ? AND {SPAN_SQL} = ?"
    if sel.precision == "day":
        cond += " AND x.year IS NULL"
    return cond, [sel.start, sel.precision]


def _choose(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """One cell per place: the provider geography equal to the ISO3 first, then a cell with a value."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = r[key]
        cur = out.get(k)
        rank = (r["provider_geo"] == r.get("iso3"), r["value"] is not None or r["value_text"] is not None)
        if cur is None or rank > (
            cur["provider_geo"] == cur.get("iso3"),
            cur["value"] is not None or cur["value_text"] is not None,
        ):
            out[k] = r
    return out


# --------------------------------------------------------------------------- map slice


def map_slice(
    q: AtlasQuery,
    idx: SeriesIndex,
    sid: int,
    *,
    precision: str | None,
    period: str | None,
    level: str = "adm0",
    projections: str = "exclude",
) -> dict[str, Any]:
    meta = series_meta(q, idx, sid)
    prec = precision or meta["default_precision"] or "year"
    if prec not in PRECISIONS:
        raise ValidationProblem("unknown precision", precision=prec[:20])
    pers = periods(q, sid, prec)
    if period is None:
        # latest period with observations (projections never pick the default period)
        obs = [p for p in pers if p["obs"]]
        period = (obs or pers)[-1]["code"] if pers else None
    out: dict[str, Any] = {
        "meta": meta,
        "precision": prec,
        "periods": pers,
        "period": period,
        "period_label": period_label_fr(prec, period) if period else None,
        "level": level,
        "projections": projections,
        "values": [],
        "missing": [],
        "missing_labels": MISSING_FR | {f"provider:{k}": v for k, v in PROVIDER_MISSING_FR.items()},
        "aggregates": [],
        "licences": [],
        "scale_hint": "quantile",
        "counts": {},
        "names": {},
    }
    if period is None:
        return out
    sel = parse_period(prec, period)
    if sel is None:
        raise ValidationProblem("period does not fit the precision", period=period[:20], precision=prec)
    where, params = _period_where(sel)
    rows = q._rows(f"SELECT {CELL_SELECT} FROM cells_s x WHERE x.sid = ? AND {where}", [sid, *params])
    lic = _Lic()
    values: list[dict[str, Any]] = []
    hidden: set[str] = set()
    provider_missing: dict[str, str] = {}
    if level == "adm1":
        chosen = _choose([r for r in rows if r["iso3"] and SUBNATIONAL_RE.match(r["provider_geo"])], "provider_geo")
    else:
        chosen = _choose([r for r in rows if r["iso3"]], "iso3")
    for r in chosen.values():
        cell = _cell(q, r)
        pt = _point(cell, prec, lic)
        if pt["miss"]:
            provider_missing[pt["iso3"] if level != "adm1" else pt["geo"]] = pt["miss"]
            continue
        if pt["proj"] and projections == "exclude":
            hidden.add(pt["iso3"] if level != "adm1" else pt["geo"])
            continue
        if not pt["proj"] and projections == "only":
            continue
        values.append(pt)
    aggs = [_cell(q, r) for r in rows if not r["iso3"]]
    out["aggregates"] = [_point(c, prec, lic) for c in aggs if c.get("value") is not None][:40]
    if level != "adm1":
        have = {v["iso3"] for v in values}
        series_countries = set(meta["countries"])
        other: set[str] = set()
        if prec == "year" and sel.year is not None and meta["precisions"].keys() - {"year"}:
            orows = q._rows(
                """SELECT DISTINCT x.iso3 FROM cells_s x WHERE x.sid = ? AND x.iso3 IS NOT NULL AND x.has_value
                     AND x.year IS NULL AND x.period_start < make_date(?, 1, 1) AND x.period_end > make_date(?, 1, 1)""",
                [sid, sel.year + 1, sel.year],
            )
            other = {r["iso3"] for r in orows}
        missing = []
        for iso in sorted(known_country_keys() - have):
            if iso in provider_missing:
                missing.append({"geo": iso, "reason": f"provider:{provider_missing[iso]}"})
            elif iso in hidden:
                missing.append({"geo": iso, "reason": "projection_hidden"})
            elif iso in other:
                missing.append({"geo": iso, "reason": "other_granularity"})
            elif iso in series_countries:
                missing.append({"geo": iso, "reason": "not_this_period"})
            else:
                missing.append({"geo": iso, "reason": "not_in_series"})
        out["missing"] = missing
    else:
        out["missing"] = [{"geo": g, "reason": f"provider:{m}"} for g, m in sorted(provider_missing.items())] + [
            {"geo": g, "reason": "projection_hidden"} for g in sorted(hidden)
        ]
    nums = [float(v["v"]) for v in values if v["v"] is not None and not v["proj"]] or [
        float(v["v"]) for v in values if v["v"] is not None
    ]
    for v in values:  # one series per slice: its key and table artifact are in meta (smaller payload)
        v.pop("table_sha", None)
        if v.get("series") == meta["series"]:
            v["series"] = None
    out["values"] = sorted(values, key=lambda v: v["iso3"] or "")
    out["scale_hint"] = scale_hint(nums)
    out["counts"] = {
        "values": len(values),
        "observed": sum(1 for v in values if not v["proj"]),
        "projection": sum(1 for v in values if v["proj"]),
        "projection_hidden": len(hidden),
        "provider_missing": len(provider_missing),
        "missing": len(out["missing"]),
    }
    out["licences"] = lic.items
    labels = place_labels()
    out["names"] = {
        k: labels[k] for k in {*(v["iso3"] for v in values), *(m["geo"] for m in out["missing"])} if k in labels
    }
    return out


# --------------------------------------------------------------------------- one place, every period


def place_series(
    q: AtlasQuery, idx: SeriesIndex, sid: int, geo: str, *, precision: str | None = None
) -> dict[str, Any]:
    meta = series_meta(q, idx, sid)
    prec = precision or meta["default_precision"] or "year"
    by_iso = re.fullmatch(r"[A-Z]{3}", geo) is not None
    if by_iso:
        rows = q._rows(
            f"SELECT {CELL_SELECT} FROM cells x WHERE x.iso3 = ? AND x.sid = ? ORDER BY x.period_start", [geo, sid]
        )
    else:
        rows = q._rows(
            f"SELECT {CELL_SELECT} FROM cells_s x WHERE x.sid = ? AND x.provider_geo = ? ORDER BY x.period_start",
            [sid, geo],
        )
    lic = _Lic()
    pts = _points_at(q, rows, prec, lic, geo if by_iso else None)
    return {
        "meta": meta,
        "geo": geo,
        "name": place_labels().get(geo),
        "precision": prec,
        "points": pts,
        "licences": lic.items,
    }


def _points_at(
    q: AtlasQuery, rows: list[dict[str, Any]], prec: str, lic: _Lic, iso3: str | None
) -> list[dict[str, Any]]:
    """Points of one place at ``prec``: year view cells (attached years) or cells of the sub-annual span class."""
    keep = []
    for r in rows:
        if prec == "year":
            if r["year"] is None:
                continue
        else:
            span = (_iso_date(r["period_end"]) - _iso_date(r["period_start"])).days
            cls = (
                "month" if 28 <= span <= 31 else "quarter" if 89 <= span <= 92 else "semester" if 181 <= span <= 184
                else "day" if span <= 1 else "other"
            )  # fmt: skip
            if cls != prec or (prec == "day" and r["year"] is not None):
                continue
        keep.append(r)
    per: dict[str, dict[str, Any]] = {}
    for r in keep:
        pkey = str(r["year"]) if prec == "year" else r["period_start"]
        cur = per.get(pkey)
        if cur is None or (iso3 is not None and r["provider_geo"] == iso3 and cur["provider_geo"] != iso3):
            per[pkey] = r
    pts = [_point(_cell(q, r), prec, lic) for r in per.values()]
    pts.sort(key=lambda p: p["ps"])
    return pts


def _iso_date(text: str) -> date:
    if text.startswith("-"):
        return date(1, 1, 1)  # BCE periods: only year cells (span class unused)
    return date.fromisoformat(text[:10])


# --------------------------------------------------------------------------- Comparer


def multi_series(
    q: AtlasQuery,
    idx: SeriesIndex,
    sids: list[int],
    geos: list[str],
    *,
    population: bool = False,
    headlines: Any = None,
) -> dict[str, Any]:
    """Each series for each country (ISO3), at the series' default precision, plus the transform rules. With
    ``population``, the atlas headline population (``reva.population.total``) of the same countries per year."""
    lic = _Lic()
    isos = [g for g in geos if re.fullmatch(r"[A-Z]{3}", g)]
    rows = (
        q._rows(
            f"""SELECT {CELL_SELECT} FROM cells_s x
                WHERE x.sid IN ({",".join(str(int(s)) for s in sids)}) AND x.iso3 IN ({",".join("?" for _ in isos)})
                ORDER BY x.iso3, x.sid, x.period_start""",
            isos,
        )
        if isos and sids
        else []
    )
    by: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault((r["sid"], r["iso3"]), []).append(r)
    out_series = []
    for sid in sids:
        meta = series_meta(q, idx, sid)
        prec = meta["default_precision"] or "year"
        per_geo = {}
        for g in isos:
            per_geo[g] = _points_at(q, by.get((sid, g), []), prec, lic, g)
        nums = [float(p["v"]) for pts in per_geo.values() for p in pts if p["v"] is not None]
        rules = transform_rules(
            unit=meta["unit"],
            precision=prec,
            numeric=bool(nums),
            min_value=min(nums) if nums else None,
            countries=bool(isos),
            context=_context(meta["indicator"], idx.concept_for(meta["series"])),
        )
        out_series.append(
            {
                "meta": meta,
                "precision": prec,
                "places": per_geo,
                "transforms": {k: {"ok": r.ok, "reason": r.reason} for k, r in rules.items()},
            }
        )
    out: dict[str, Any] = {
        "series": out_series,
        "geos": isos,
        "names": {g: place_labels().get(g, g) for g in isos},
        "licences": lic.items,
        "population": None,
    }
    if population and headlines is not None and isos:
        out["population"] = population_series(q, headlines, isos)
    return out


def population_series(q: AtlasQuery, headlines: Any, isos: list[str]) -> dict[str, Any]:
    """Atlas headline population per country and year (observed values only; the answering source per year)."""
    per: dict[str, list[dict[str, Any]]] = {}
    lic: list[dict[str, Any]] = []
    concept = headlines.concepts.get(POPULATION_HEADLINE)
    if concept is None:
        return {"concept": None, "places": {}, "licences": [], "note": "concept de population absent du registre"}
    for g in isos:
        ck = (q.snapshot.sha256, -1, f"pop:{g}")
        h = _cache_get(_PERIODS, ck)
        if h is None:
            h = q.headline_series(headlines, POPULATION_HEADLINE, g, projections="exclude")
            _cache_put(_PERIODS, ck, h)
        per[g] = [
            {
                "year": c["year"],
                "v": c["value"],
                "unit": c["unit"],
                "series": (c.get("answered_by") or {}).get("series"),
                "method": c.get("method"),
            }
            for c in h["best"]
            if c.get("value") is not None and not c.get("projection")
        ]
        lic.extend(h.get("licences") or [])
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for x in lic:
        seen.setdefault((str(x.get("source")), str(x.get("licence"))), x)
    return {
        "concept": {"id": concept.id, "labels": dict(concept.labels), "unit": concept.unit},
        "places": per,
        "licences": list(seen.values()),
    }
