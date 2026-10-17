"""Data queries a dossier template may call (``query:`` in registry/dossier/*.yaml).

Each query reads an EXISTING service (atlas query layer, registered atlas tables, knowledge graph, families,
causal claims, effect estimates, forecast registry, decision engine, pre-registered evaluations) and returns
``Result``: named values, rows and point series, every value carrying its source (footnote) or an explicit
``Miss`` with its reason. Queries never write text a reader sees except values and their sources; all wording
comes from the template.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import polars as pl

from reva.v8.dossier.context import Ctx, rights_src, source_title
from reva.v8.dossier.fmt import fr_number, fr_period, to_decimal, wd_time
from reva.v8.dossier.values import Item, Miss, Result, SeriesData, Src, Val, label_val, num_val, period_val

REPO = Path(__file__).resolve().parents[4]
QUERIES: dict[str, Callable[..., Result]] = {}

COUNTRY_HREF = "/v8/fiche/country/{}"
ENTITY_HREF = "/v8/fiche/{}/{}"


def query(name: str) -> Callable[[Callable[..., Result]], Callable[..., Result]]:
    def deco(fn: Callable[..., Result]) -> Callable[..., Result]:
        QUERIES[name] = fn
        return fn

    return deco


# ============================================================================== helpers
def _countries() -> dict[str, str]:
    from reva.nlqa.catalog import load_countries

    return {c.iso3: (c.label_fr or c.label) for c in load_countries()}


COUNTRY_SRC = Src(
    source="reva.registry",
    title="Registre des pays REVA (ISO 3166-1 et traduction française iso-codes 4.20.1)",
    dataset="registry/entities/country_crosswalk.json",
    licence="LGPL-2.1 (iso-codes) / registre REVA",
)


def country_val(iso3: str | None) -> Item:
    if not iso3:
        return Miss("pays", "code pays absent")
    name = _countries().get(iso3)
    if name is None:
        return Miss("pays", f"code {iso3} absent du registre des pays")
    return Val(text=name, src=COUNTRY_SRC, kind="label", exact=iso3, href=COUNTRY_HREF.format(iso3))


def _period_of_cell(c: Mapping[str, Any]) -> str:
    if c.get("year") is not None and c.get("period_basis") in (None, "calendar_year"):
        return str(c["year"])
    ps, pe = str(c.get("period_start") or ""), str(c.get("period_end") or "")
    return f"{ps}/{pe}" if ps else str(c.get("year") or "")


def atlas_cell_src(c: Mapping[str, Any], *, title: str | None = None) -> Src:
    ab = c.get("answered_by") or {}
    source = c.get("source") or ab.get("source") or "atlas"
    dataset = c.get("dataset") or ab.get("dataset")
    series = c.get("series") or ab.get("series")
    prov = c.get("provenance") or {}
    return rights_src(
        str(source),
        c.get("rights"),
        title=title,
        dataset=f"{source}.{dataset}" if dataset else None,
        indicator=series,
        period=_period_of_cell(c),
        ref=prov.get("raw_sha256"),
    )


def _cell_kind(c: Mapping[str, Any]) -> str:
    fl = c.get("flags") or {}
    if c.get("projection") or fl.get("projection") or fl.get("estimate_kind") in ("projection", "nowcast"):
        return "projection"
    if fl.get("estimate") or fl.get("estimated") or c.get("method") == "derived":
        return "estimate"
    return "observed"


def cell_val(c: Mapping[str, Any], unit: str | None = None) -> Item:
    u = unit or c.get("unit")
    if c.get("value") is None:
        if c.get("value_text"):
            return label_val(str(c["value_text"]), atlas_cell_src(c))
        reason = (c.get("flags") or {}).get("missing") or "non rapporté par la source"
        return Miss(str(c.get("series") or "valeur"), str(reason))
    return num_val(c["value"], atlas_cell_src(c), unit=u, kind=_cell_kind(c))


def _year(c: Mapping[str, Any]) -> str:
    return str(c.get("year") or str(c.get("period_start") or "")[:4])


def _ref_src(kind: str, ident: str, *, title: str | None = None, method: str | None = None) -> Src:
    return Src(source=kind, title=title or source_title(kind), ref=ident, method=method, licence="REVA (registre public)")


# ============================================================================== atlas (pinned snapshot)
@query("atlas.headline")
def atlas_headline(ctx: Ctx, *, iso3: str, concepts: Mapping[str, str], charts: Iterable[str] = ()) -> Result:
    """Latest best value of headline concepts for one country (+ the annual series for ``charts``)."""
    key = ("headline", iso3)
    hd = ctx.memo.get(key)
    if hd is None:
        hd = ctx.atlas().headline_dossier(ctx.headlines(), iso3, include_series=True)
        ctx.memo[key] = hd
    by = {c["concept_id"]: c for c in hd["concepts"]}
    without = {c["concept_id"] for c in hd["without_data"]}
    out = Result()
    chart_set = set(charts)
    for alias, cid in concepts.items():
        c = by.get(cid)
        if c is None:
            reason = "aucune source consultée ne rapporte de valeur" if cid in without else "concept absent du registre"
            out.values[alias] = Miss(cid, reason)
            out.values[f"{alias}_year"] = Miss(cid, reason)
            continue
        cell = c["latest"]
        out.values[alias] = cell_val(cell, c["unit"])
        out.values[f"{alias}_year"] = period_val(_year(cell), atlas_cell_src(cell))
        if alias in chart_set and c.get("series"):
            src = atlas_cell_src(cell).with_period(f"{c['year_min']}–{c['year_max']}")
            out.series.append(
                SeriesData(
                    label=(c.get("labels") or {}).get("fr") or cid,
                    kind="observed",
                    src=src,
                    unit=c["unit"],
                    x_kind="year",
                    points=[(str(p["year"]), _f(p["value"]), str(p["value"]), None, None) for p in c["series"]],
                )
            )
    return out


@query("atlas.coverage")
def atlas_coverage(ctx: Ctx, *, iso3: str) -> Result:
    """How many headline indicators answer for the country; the ones without any value, with the sources consulted."""
    hd = ctx.memo.get(("headline", iso3))
    if hd is None:
        hd = ctx.atlas().headline_dossier(ctx.headlines(), iso3, include_series=True)
        ctx.memo[("headline", iso3)] = hd
    reg = ctx.headlines()
    src = Src(
        source="reva.atlas",
        title="Atlas REVA — indicateurs phares (ordre de sources déclaré)",
        dataset="registry/atlas/source_order",
        ref=ctx.atlas_sha,
        method="décompte REVA des indicateurs phares avec / sans valeur pour ce pays",
        licence="REVA",
    )
    out = Result()
    n_with, n_without = len(hd["concepts"]), len(hd["without_data"])
    out.values["n_with"] = num_val(n_with, src, kind="derived", digits=0)
    out.values["n_total"] = num_val(n_with + n_without, src, kind="derived", digits=0)
    for w in hd["without_data"]:
        c = reg.concepts.get(w["concept_id"])
        lab = (c.labels or {}).get("fr") if c else w["concept_id"]
        consulted = ", ".join(x.get("series", "") for x in w.get("consulted") or []) or "aucune source déclarée"
        out.missing.append(Miss(str(lab), f"aucune valeur chez les sources consultées ({consulted})"))
    return out


def _latest_complete_year(cells: list[dict[str, Any]], share: float = 0.8) -> int | None:
    by: dict[int, int] = {}
    for c in cells:
        if c.get("year") is not None:
            by[int(c["year"])] = by.get(int(c["year"]), 0) + 1
    if not by:
        return None
    best = max(by.values())
    return max(y for y, n in by.items() if n >= share * best)


@query("atlas.ranking")
def atlas_ranking(ctx: Ctx, *, concept: str, top: int = 10, exclude: Iterable[str] = ()) -> Result:
    """Countries ranked by a catalog concept in the latest year that most countries report (best source order)."""
    cat = ctx.catalog()
    if concept not in cat.concepts:
        return Result(empty_reason=f"concept {concept} absent du catalogue de l'atlas")
    key = ("best_all", concept)
    cells = ctx.memo.get(key)
    if cells is None:
        cells = ctx.atlas().best_cells(cat, [concept])
        ctx.memo[key] = cells
    year = _latest_complete_year(cells)
    out = Result()
    if year is None:
        out.empty_reason = "aucune valeur dans l'instantané de l'atlas"
        return out
    ex = set(exclude)
    rows = [c for c in cells if c.get("year") == year and c.get("iso3") not in ex and _f(c.get("value")) is not None]
    rows.sort(key=lambda c: -(_f(c["value"]) or 0.0))
    unit = cat.concepts[concept].unit
    first_src = atlas_cell_src(rows[0]) if rows else None
    if first_src is not None:
        out.values["year"] = period_val(str(year), first_src)
        out.values["n_countries"] = num_val(len(rows), first_src.with_period(str(year)), kind="derived", digits=0)
    for i, c in enumerate(rows[:top], start=1):
        out.rows.append(
            {
                "rank": num_val(i, atlas_cell_src(c), kind="derived", digits=0),
                "country": country_val(c["iso3"]),
                "value": cell_val(c, unit),
            }
        )
    out.more = max(0, len(rows) - top)
    for i, r in enumerate(out.rows[:3], start=1):
        out.values[f"top{i}"] = r["country"]
        out.values[f"top{i}_value"] = r["value"]
    out.series.append(
        SeriesData(
            label=(cat.concepts[concept].labels or {}).get("fr") or concept,
            kind="observed",
            src=first_src.with_period(str(year)) if first_src else Src(source="atlas", title="atlas"),
            unit=unit,
            x_kind="category",
            points=[(_text(r["country"]), _num(r["value"]), _exact(r["value"]), None, None) for r in out.rows],
        )
    )
    return out


@query("atlas.country_series")
def atlas_country_series(ctx: Ctx, *, iso3: str, concepts: Mapping[str, str], since: int | None = None) -> Result:
    """Latest value and annual best series of catalog concepts for one country (WLD aggregates are not iso3)."""
    cat = ctx.catalog()
    ids = [c for c in concepts.values() if c in cat.concepts]
    cells = ctx.atlas().best_cells(cat, ids, iso3=iso3) if ids else []
    by: dict[str, list[dict[str, Any]]] = {}
    for c in cells:
        by.setdefault(c["concept_id"], []).append(c)
    out = Result()
    for alias, cid in concepts.items():
        pts = sorted(by.get(cid, []), key=lambda c: c["year"])
        if since is not None:
            pts = [p for p in pts if p["year"] >= since]
        if not pts:
            out.values[alias] = Miss(cid, "aucune valeur pour ce pays dans l'instantané de l'atlas")
            continue
        last = pts[-1]
        out.values[alias] = cell_val(last, cat.concepts[cid].unit)
        out.values[f"{alias}_year"] = period_val(str(last["year"]), atlas_cell_src(last))
        out.series.append(
            SeriesData(
                label=(cat.concepts[cid].labels or {}).get("fr") or cid,
                kind="observed",
                src=atlas_cell_src(last).with_period(f"{pts[0]['year']}–{last['year']}"),
                unit=cat.concepts[cid].unit,
                x_kind="year",
                points=[(str(p["year"]), _f(p["value"]), str(p["value"]), None, None) for p in pts],
            )
        )
    return out


# ============================================================================== registered tables (newer than the snapshot)
def _table_src(pin: Mapping[str, Any], indicator: str | None, period: str | None, title: str | None = None) -> Src:
    r = pin.get("rights") or {}
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except ValueError:
            r = {}
    key = str(pin["dataset_key"]).removeprefix("atlas.")
    return rights_src(
        str(r.get("source_key") or key.split(".")[0]),
        r,
        title=title,
        dataset=key,
        indicator=indicator,
        period=period,
        ref=str(pin["artifact_sha256"]),
        retrieved=str(pin.get("recorded_at") or "")[:10] or None,
    )


def _slice(ctx: Ctx, dataset_key: str, indicator: str, geo: str | None = None, geo_col: str = "iso3") -> Any:
    def filt(f: pl.DataFrame) -> pl.DataFrame:
        x = f.filter(pl.col("indicator") == indicator)
        if geo is not None:
            x = x.filter(pl.col(geo_col) == geo)
        return x.select("iso3", "provider_geo", "period_start", "period_end", "value", "unit", "flags")

    return ctx.table_slice(dataset_key, (indicator, geo, geo_col), filt)


def _pfmt(d: Any, freq: str) -> str:
    s = str(d)
    return s[:7] if freq == "M" else s[:4] if freq == "A" else s[:10]


@query("table.series")
def table_series(
    ctx: Ctx,
    *,
    dataset: str,
    indicator: str,
    geo: str | None = None,
    geo_col: str = "iso3",
    freq: str = "A",
    since: str | None = None,
    label: str = "",
    kind: str = "observed",
    title: str | None = None,
) -> Result:
    """One series of a registered atlas table (latest registered version): latest, peak, low, start + chart."""
    got = _slice(ctx, dataset, indicator, geo, geo_col)
    out = Result()
    if got is None:
        out.empty_reason = f"table {dataset} non enregistrée"
        return out
    f, pin = got
    f = f.filter(pl.col("value").is_not_null()).sort("period_start")
    if since:
        f = f.filter(pl.col("period_start").cast(pl.Utf8) >= since)
    if f.height == 0:
        out.empty_reason = f"aucune valeur pour {indicator}" + (f" ({geo})" if geo else "")
        return out
    rows = f.to_dicts()
    unit = rows[-1]["unit"]
    vals = [(r, _f(r["value"])) for r in rows]
    vals = [(r, v) for r, v in vals if v is not None]
    last = vals[-1][0]
    hi = max(vals, key=lambda t: t[1])[0]
    lo = min(vals, key=lambda t: t[1])[0]
    for name, r in (("latest", last), ("max", hi), ("min", lo), ("first", vals[0][0])):
        p = _pfmt(r["period_start"], freq)
        src = _table_src(pin, indicator, p, title)
        out.values[name] = num_val(r["value"], src, unit=unit, kind=kind)
        out.values[f"{name}_period"] = period_val(p, src)
    first_p, last_p = _pfmt(rows[0]["period_start"], freq), _pfmt(last["period_start"], freq)
    out.series.append(
        SeriesData(
            label=label or indicator,
            kind=kind,
            src=_table_src(pin, indicator, f"{first_p}–{last_p}", title),
            unit=unit,
            x_kind={"M": "month", "A": "year", "Q": "quarter"}.get(freq, "year"),
            points=[(_pfmt(r["period_start"], freq), v, str(r["value"]), None, None) for r, v in vals],
        )
    )
    return out


@query("table.ranking")
def table_ranking(
    ctx: Ctx,
    *,
    dataset: str,
    indicator: str,
    top: int = 10,
    title: str | None = None,
    kind: str = "observed",
    sum_label: str | None = None,
) -> Result:
    """Countries ranked by one indicator of a registered table in its latest period (+ optional REVA sum)."""
    got = _slice(ctx, dataset, indicator)
    out = Result()
    if got is None:
        out.empty_reason = f"table {dataset} non enregistrée"
        return out
    f, pin = got
    f = f.filter(pl.col("value").is_not_null() & pl.col("iso3").is_not_null())
    if f.height == 0:
        out.empty_reason = f"aucune valeur pour {indicator}"
        return out
    last = f["period_start"].max()
    f = f.filter(pl.col("period_start") == last)
    rows = sorted(f.to_dicts(), key=lambda r: -(_f(r["value"]) or 0.0))
    p = str(last)[:4]
    src = _table_src(pin, indicator, p, title)
    unit = rows[0]["unit"]
    out.values["period"] = period_val(p, src)
    out.values["n_countries"] = num_val(len(rows), src, kind="derived", digits=0)
    if sum_label:
        total = sum(_f(r["value"]) or 0.0 for r in rows)
        dsrc = Src(**{**src.__dict__, "method": f"somme REVA des {len(rows)} valeurs-pays publiées ({sum_label})"})
        out.values["sum"] = num_val(f"{total:.2f}", dsrc, unit=unit, kind="derived")
    for i, r in enumerate(rows[:top], start=1):
        out.rows.append(
            {
                "rank": num_val(i, src, kind="derived", digits=0),
                "country": country_val(r["iso3"]),
                "value": num_val(r["value"], src, unit=unit, kind=kind),
            }
        )
    for i, r in enumerate(out.rows[:3], start=1):
        out.values[f"top{i}"] = r["country"]
        out.values[f"top{i}_value"] = r["value"]
    out.more = max(0, len(rows) - top)
    out.series.append(
        SeriesData(
            label=title or indicator,
            kind=kind,
            src=src,
            unit=unit,
            x_kind="category",
            points=[(_text(r["country"]), _num(r["value"]), _exact(r["value"]), None, None) for r in out.rows],
        )
    )
    return out


@query("table.geo_value")
def table_geo_value(
    ctx: Ctx, *, dataset: str, indicator: str, geo: str, geo_col: str = "provider_geo", freq: str = "A", title: str | None = None
) -> Result:
    """Latest value of one indicator for one provider geography (e.g. EIA 'WORL' world aggregate)."""
    return table_series(ctx, dataset=dataset, indicator=indicator, geo=geo, geo_col=geo_col, freq=freq, title=title)


@query("table.views")
def table_views(ctx: Ctx, *, iso3: Iterable[str], months: int = 6) -> Result:
    """VIEWS predicted state-based fatalities (latest run) for the next months, per country (third-party projection)."""
    out = Result()
    pin = ctx.table_pin("atlas.views.cm_sb")
    if not pin:
        out.empty_reason = "prévisions VIEWS non chargées"
        return out

    def filt(f: pl.DataFrame) -> pl.DataFrame:
        return f.filter(pl.col("indicator").str.ends_with("|main_mean")).select(
            "indicator", "iso3", "period_start", "value", "unit", "flags"
        )

    got = ctx.table_slice("atlas.views.cm_sb", ("main_mean",), filt)
    assert got is not None
    f, pin = got
    runs = sorted(f["indicator"].unique().to_list())
    if not runs:
        out.empty_reason = "aucune exécution VIEWS"
        return out
    run = runs[-1]  # fatalitiesNNN_YYYY_MM_t01: the newest run sorts last within a model generation
    runs_by_date = sorted(runs, key=lambda r: (r.split("_")[1:3], r))
    run = runs_by_date[-1]
    g = f.filter((pl.col("indicator") == run) & pl.col("iso3").is_in(list(iso3))).sort("period_start")
    run_label = run.split("|")[0]
    for code in iso3:
        rows = g.filter(pl.col("iso3") == code).head(months).to_dicts()
        if not rows:
            out.missing.append(Miss(f"prévision VIEWS {code}", "pays absent de la dernière exécution VIEWS"))
            continue
        for r in rows:
            p = str(r["period_start"])[:7]
            src = _table_src(pin, run, p, "VIEWS — décès prévus (violence étatique), exécution " + run_label)
            out.rows.append(
                {
                    "country": country_val(code),
                    "period": period_val(p, src),
                    "value": num_val(r["value"], src, unit="deaths", kind="projection", digits=0),
                }
            )
        src0 = _table_src(pin, run, None, "VIEWS — décès prévus (violence étatique)")
        out.series.append(
            SeriesData(
                label=_text(country_val(code)),
                kind="projection",
                src=src0,
                unit="deaths",
                x_kind="month",
                points=[(str(r["period_start"])[:7], _f(r["value"]), str(r["value"]), None, None) for r in rows],
            )
        )
    out.values["run"] = label_val(run_label, _table_src(pin, run, None, "VIEWS"))
    return out


# ============================================================================== knowledge graph
def _kg_rights(ctx: Ctx, edges: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    from reva.kg import family_query as fq

    return fq.rights_for(ctx.conn, [{"raw_sha256": e["provenance"]["raw_sha256"]} for e in edges])


def kg_src(ctx: Ctx, e: Mapping[str, Any], rights: Mapping[str, Any] | None, period: str | None = None) -> Src:
    from reva.kg import family_query as fq
    from reva.kg import query as kq

    p = e["provenance"]
    community = kq.is_community_edited(p["source_key"])
    r = dict(fq.WIKIDATA_RIGHTS) if community else (rights.get(p["raw_sha256"]) if rights else None)
    prop = e["property"]
    return rights_src(
        p["source_key"],
        r,
        dataset=f"{p['source_key']} {p.get('source_revision') or ''}".strip(),
        indicator=f"{prop.get('label_fr') or prop.get('label_en') or prop['pid']} ({prop['pid']})",
        period=period,
        ref=p.get("statement_key") or p.get("statement_id"),
        retrieved=str(p.get("retrieved_at") or "")[:10] or None,
        verification=p.get("verification"),
    )


def _labels(ctx: Ctx, qids: Iterable[str]) -> dict[str, str]:
    from reva.kg import query as kq

    lab = kq.labels_for(ctx.conn, [q for q in qids if q])
    return {q: (v.get("fr") or v.get("en") or q) for q, v in lab.items()}


def _entity_href(kind: str | None, qid: str) -> str:
    return ENTITY_HREF.format(kind or "entity", qid)


def _time_text(t: Mapping[str, Any] | None) -> str | None:
    if not t:
        return None
    return wd_time(str(t.get("time") or ""), int(t.get("precision") or 9))


def _name(ctx: Ctx, o: Mapping[str, Any], src: Src) -> Item:
    """Label of a neighbour entity (French, else English); an entity the KG has not labelled is stated missing."""
    lab = _labels(ctx, [o["qid"]]).get(o["qid"])
    if not lab or lab == o["qid"]:
        return Miss(o["qid"], f"entité {o['qid']} sans libellé chargé dans le graphe")
    return label_val(lab, src, href=_entity_href(o.get("kind"), o["qid"]))


def _group_src(src: Src) -> Src:
    """Footnote shared by the labels of one property (one note per source property, not per statement)."""
    return Src(**{**src.__dict__, "ref": None, "period": None, "retrieved": None})


def _literal_val(ctx: Ctx, e: Mapping[str, Any], src: Src) -> Item:
    v = e.get("value") or {}
    t = v.get("type")
    if t == "quantity":
        return num_val(v.get("amount"), src, unit=v.get("unit") if v.get("unit") not in ("1", None) else None, kind="observed")
    if t == "time":
        return label_val(_time_text(v), src)
    if t in ("string", "external_id", "monolingualtext", "url"):
        return label_val(str(v.get("value") or v.get("text") or ""), src)
    if e.get("object"):
        return _name(ctx, e["object"], src)
    return Miss(src.title, "valeur de forme inconnue")


def _edges(ctx: Ctx, ids: Iterable[str], properties: Iterable[str] | None, direction: str = "out", limit: int = 500) -> list[dict[str, Any]]:
    from reva.kg import query as kq

    out = []
    for qid in ids:
        if not kq.exists(ctx.conn, qid):
            continue
        res = kq.neighbours(
            ctx.conn,
            qid,
            direction=direction,  # type: ignore[arg-type]
            properties=list(properties) if properties else None,
            include_literals=True,
            limit=limit,
            origin=ctx.origin,
        )
        for e in res["edges"]:
            e["_from"] = qid
            out.append(e)
    return out


def _current(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preferred-rank statements if any, else those without an end date (source ranks, never REVA's choice)."""
    pref = [e for e in edges if e["provenance"].get("rank") == "preferred"]
    if pref:
        return pref
    open_ = [e for e in edges if not e.get("end")]
    return open_ or edges


@query("kg.literals")
def kg_literals(ctx: Ctx, *, ids: Iterable[str], properties: Mapping[str, str], current: bool = True) -> Result:
    """One value per alias: the (current) statement of property ``pid`` of the first entity that has one."""
    ids = list(ids)
    edges = _edges(ctx, ids, set(properties.values()), "out")
    rights = _kg_rights(ctx, edges)
    out = Result()
    for alias, pid in properties.items():
        es = [e for e in edges if e["property"]["pid"] == pid]
        if current:
            es = _current(es)
        if not es:
            out.values[alias] = Miss(pid, "aucune déclaration chargée dans le graphe pour cette propriété")
            continue
        es.sort(key=lambda e: (ids.index(e["_from"]), str((e.get("point_in_time") or e.get("start") or {}).get("time") or "")), reverse=False)
        e = es[-1] if any(x.get("point_in_time") for x in es) else es[0]
        when = _time_text(e.get("point_in_time") or e.get("start"))
        src = kg_src(ctx, e, rights, when)
        out.values[alias] = _literal_val(ctx, e, src)
        if when:
            out.values[f"{alias}_when"] = label_val(when, src)
        out.values[f"{alias}_n"] = num_val(len(es), src, kind="derived", digits=0)
    return out


@query("kg.edges")
def kg_edges(
    ctx: Ctx,
    *,
    ids: Iterable[str],
    properties: Iterable[str] | None = None,
    direction: str = "out",
    limit: int = 12,
    current: bool = False,
    dedupe: bool = True,
    sort: str = "label",
    exclude_objects: Iterable[str] = (),
) -> Result:
    """Neighbours through the given properties: one row per neighbour (label linked, property, dates)."""
    edges = _edges(ctx, ids, properties, direction)
    if current:
        edges = _current(edges)
    rights = _kg_rights(ctx, edges)
    excl = set(exclude_objects)
    unlabelled = 0
    rows: list[tuple[str, dict[str, Item]]] = []
    seen: set[str] = set()
    for e in edges:
        other = e["object"] if e["direction"] == "out" else e["subject"]
        if other is None:
            continue
        if other["qid"] in excl or other["qid"] in set(ids):
            continue
        if dedupe and other["qid"] in seen:
            continue
        seen.add(other["qid"])
        when = _time_text(e.get("point_in_time") or e.get("start"))
        end = _time_text(e.get("end"))
        src = _group_src(kg_src(ctx, e, rights))
        prop = e["property"]
        nm = _name(ctx, other, src)
        if isinstance(nm, Miss):
            unlabelled += 1
            continue
        name = nm.text
        row: dict[str, Item] = {
            "name": nm,
            "property": label_val(prop.get("label_fr") or prop.get("label_en") or prop["pid"], src),
            "when": label_val(when, src) if when else Miss("date", "non datée par la source"),
            "end": label_val(end, src) if end else Miss("fin", "non indiquée"),
            "source": label_val(src.title, src),
        }
        qual = e.get("qualifiers") or {}
        for qk in ("phase", "max_phase_for_ind", "share"):
            if qk in qual:
                row[qk] = label_val(str(qual[qk]), src)
        rows.append((name.lower() if sort == "label" else str(when or ""), row))
    rows.sort(key=lambda t: t[0], reverse=(sort == "date_desc"))
    out = Result(rows=[r for _, r in rows[:limit]], more=max(0, len(rows) - limit) + unlabelled)
    out.values["n"] = (
        num_val(len(rows), rows[0][1]["name"].src if rows and isinstance(rows[0][1]["name"], Val) else Src("kg", "graphe"), kind="derived", digits=0)
        if rows
        else Miss("nombre", "aucune déclaration")
    )
    if not rows:
        out.empty_reason = "aucune déclaration de ce type n'est chargée dans le graphe de connaissances"
    return out


@query("kg.quantity_series")
def kg_quantity_series(ctx: Ctx, *, id: str, property: str, label: str = "", kind: str = "estimate") -> Result:
    """Yearly quantity statements (point in time) of one entity as a series, with published bounds."""
    edges = [e for e in _edges(ctx, [id], [property], "out") if (e.get("value") or {}).get("type") == "quantity"]
    rights = _kg_rights(ctx, edges)
    out = Result()
    pts: dict[str, dict[str, Any]] = {}
    for e in edges:
        y = _time_text(e.get("point_in_time"))
        if y:
            pts[y] = e
    if not pts:
        out.empty_reason = "aucune valeur annuelle chargée"
        return out
    years = sorted(pts)
    unit = (pts[years[-1]]["value"] or {}).get("unit")
    for name, y in (("latest", years[-1]), ("first", years[0])):
        e = pts[y]
        src = kg_src(ctx, e, rights, y)
        out.values[name] = num_val(e["value"]["amount"], src, unit=unit, kind=kind)
        out.values[f"{name}_year"] = label_val(y, src)
        lo, hi = e["value"].get("lowerBound"), e["value"].get("upperBound")
        out.values[f"{name}_lo"] = num_val(lo, src, unit=unit, kind=kind) if lo else Miss("borne basse", "non publiée")
        out.values[f"{name}_hi"] = num_val(hi, src, unit=unit, kind=kind) if hi else Miss("borne haute", "non publiée")
    peak = max(years, key=lambda y: _f(pts[y]["value"]["amount"]) or 0.0)
    psrc = kg_src(ctx, pts[peak], rights, peak)
    out.values["max"] = num_val(pts[peak]["value"]["amount"], psrc, unit=unit, kind=kind)
    out.values["max_year"] = label_val(peak, psrc)
    total = sum(_f(pts[y]["value"]["amount"]) or 0.0 for y in years)
    tsrc = Src(**{**kg_src(ctx, pts[years[-1]], rights, f"{years[0]}–{years[-1]}").__dict__,
                  "method": f"somme REVA des {len(years)} valeurs annuelles publiées (meilleure estimation)"})
    out.values["total"] = num_val(f"{total:.0f}", tsrc, unit=unit, kind="derived", digits=0)
    out.values["n_years"] = num_val(len(years), tsrc, kind="derived", digits=0)
    out.series.append(
        SeriesData(
            label=label or property,
            kind=kind,
            src=kg_src(ctx, pts[years[-1]], rights, f"{years[0]}–{years[-1]}"),
            unit=unit or "",
            x_kind="year",
            points=[
                (
                    y,
                    _f(pts[y]["value"]["amount"]),
                    str(pts[y]["value"]["amount"]),
                    _f(pts[y]["value"].get("lowerBound")),
                    _f(pts[y]["value"].get("upperBound")),
                )
                for y in years
            ],
        )
    )
    return out


@query("kg.timeline")
def kg_timeline(ctx: Ctx, *, id: str, limit: int = 12, properties: Iterable[str] | None = None, since: str | None = None) -> Result:
    """Dated statements about an entity (newest first), each with its source."""
    from reva.kg import query as kq

    out = Result()
    if not kq.exists(ctx.conn, id):
        out.empty_reason = "entité absente du graphe de connaissances"
        return out
    res = kq.timeline(ctx.conn, id, include_incoming=True, limit=800, origin=ctx.origin)
    evs = [e for e in res["events"] if not properties or e["property"]["pid"] in set(properties)]
    if since:
        evs = [e for e in evs if str(e.get("sort_date") or "") >= since]
    evs.sort(key=lambda e: str(e.get("sort_date") or ""), reverse=True)
    rights = _kg_rights(ctx, evs[: limit * 3])
    seen = set()
    for e in evs:
        other = e["object"] if e["direction"] == "out" else e["subject"]
        k = (e["property"]["pid"], other["qid"] if other else json.dumps(e.get("value"), sort_keys=True))
        if k in seen:
            continue
        seen.add(k)
        when = _time_text(e.get("point_in_time") or e.get("start")) or _time_text(e.get("value"))
        src = kg_src(ctx, e, rights, when)
        prop = e["property"]
        what = _name(ctx, other, _group_src(src)) if other else _literal_val(ctx, e, src)
        if isinstance(what, Miss):
            continue
        out.rows.append(
            {
                "when": label_val(when, src) if when else Miss("date", "non datée"),
                "property": label_val(prop.get("label_fr") or prop.get("label_en") or prop["pid"], _group_src(src)),
                "what": what,
                "direction": label_val("entrant" if e["direction"] == "in" else "sortant", src),
            }
        )
        if len(out.rows) >= limit:
            break
    out.more = max(0, len(seen) - len(out.rows))
    if not out.rows:
        out.empty_reason = "aucune déclaration datée"
    return out


@query("kg.facilities")
def kg_facilities(
    ctx: Ctx,
    *,
    property: str,
    object: str,
    group_property: str,
    sum_property: str,
    top: int = 10,
    title: str = "",
) -> Result:
    """Facilities with ``property`` = ``object`` grouped by ``group_property`` (count + sum of a quantity)."""
    from sqlalchemy import text

    rows = ctx.conn.execute(
        text(
            """SELECT g.object_qid AS grp, count(*) AS n,
                      sum((c.value->>'amount')::numeric) AS total,
                      min(f.source_key) AS source_key, min(f.source_revision) AS rev, min(f.raw_sha256) AS raw
                 FROM kg.statement_current f
                 JOIN kg.statement_current g ON g.subject_qid = f.subject_qid AND g.property_pid = :gp
                 LEFT JOIN kg.statement_current c ON c.subject_qid = f.subject_qid AND c.property_pid = :sp
                WHERE f.property_pid = :p AND f.object_qid = :o AND f.origin = :origin
                GROUP BY 1 ORDER BY 3 DESC NULLS LAST"""
        ),
        {"p": property, "o": object, "gp": group_property, "sp": sum_property, "origin": ctx.origin},
    ).mappings().all()
    out = Result()
    if not rows:
        out.empty_reason = "aucune installation de ce type dans le graphe"
        return out
    from reva.kg import family_query as fq

    rights = fq.rights_for(ctx.conn, [{"raw_sha256": rows[0]["raw"]}])
    src = rights_src(
        rows[0]["source_key"],
        rights.get(rows[0]["raw"]),
        dataset=f"{rows[0]['source_key']} {rows[0]['rev'] or ''}".strip(),
        indicator=f"{property} = {object}",
        ref=rows[0]["raw"],
        method=f"décompte et somme REVA ({sum_property}) des installations par {group_property}",
    )
    labels = _labels(ctx, [r["grp"] for r in rows])
    n_all = sum(int(r["n"]) for r in rows)
    tot_all = sum(float(r["total"] or 0) for r in rows)
    out.values["n"] = num_val(n_all, src, kind="derived", digits=0)
    out.values["total"] = num_val(f"{tot_all:.0f}", src, unit="MW", kind="derived", digits=0)
    out.values["n_groups"] = num_val(len(rows), src, kind="derived", digits=0)
    for r in rows[:top]:
        grp = str(r["grp"])
        iso = grp.split(":")[-1] if grp.startswith("iso3:") else None
        out.rows.append(
            {
                "group": country_val(iso) if iso else label_val(labels.get(grp, grp), src),
                "n": num_val(int(r["n"]), src, kind="derived", digits=0),
                "total": num_val(f"{float(r['total'] or 0):.0f}", src, unit="MW", kind="derived", digits=0)
                if r["total"] is not None
                else Miss("capacité", "non publiée"),
            }
        )
    out.more = max(0, len(rows) - top)
    return out


# ============================================================================== causal claims
def claim_src(c: Mapping[str, Any]) -> Src:
    s = c["source"]
    g = (c.get("grade") or {}).get("grade")
    return rights_src(
        s["source_key"],
        s.get("rights"),
        dataset=s.get("dataset") or s["source_key"],
        indicator=f"affirmation causale, niveau {s.get('tier')}, note {g}",
        ref=s.get("statement_key") or c.get("claim_key"),
        retrieved=str(s.get("retrieved_at") or "")[:10] or None,
        verification=s.get("verification"),
        url=(s.get("citation") or {}).get("url") if isinstance(s.get("citation"), dict) else None,
    )


@query("causal.claims")
def causal_claims(ctx: Ctx, *, ids: Iterable[str], side: str = "causes", limit: int = 8, min_grade: str | None = None, generic: bool = False) -> Result:
    """Attributed causal claims whose effect (``causes``) or cause (``effects``) is one of ``ids``."""
    from reva.causal import graph as cg

    out = Result()
    claims: list[dict[str, Any]] = []
    seen = set()
    for qid in ids:
        if side == "causes":
            res = cg.why(ctx.conn, qid, origin=ctx.origin, min_grade=min_grade)
            lanes = [ln for ln in res["lanes"] if generic or ln.get("lane") not in ("generic", "outcome")]
            got = [c for ln in lanes for c in ln.get("claims", [])]
        else:
            try:
                got = cg.effects(ctx.conn, qid, origin=ctx.origin, min_grade=min_grade, limit=200)["claims"]
            except Exception:  # noqa: BLE001 - an unknown id has no claims (stated as missing below)
                got = []
        for c in got:
            if c["claim_key"] not in seen:
                seen.add(c["claim_key"])
                claims.append(c)
    order = {g: i for i, g in enumerate("ABCDE")}
    claims.sort(key=lambda c: (order.get((c.get("grade") or {}).get("grade") or "E", 9), c["claim_key"]))
    for c in claims[:limit]:
        src = claim_src(c)
        g = (c.get("grade") or {}).get("grade") or "?"
        out.rows.append(
            {
                "cause": label_val(c["cause"].get("label_fr") or c["cause"]["label"], src, href=_entity_href(c["cause"].get("kind"), c["cause"]["id"])),
                "effect": label_val(c["effect"].get("label_fr") or c["effect"]["label"], src, href=_entity_href(c["effect"].get("kind"), c["effect"]["id"])),
                "relation": label_val((c.get("relation_label") or {}).get("fr") or c["relation"], src, kind="claim"),
                "source": label_val(src.title, src),
                "tier": label_val(str(c["source"].get("tier") or "?"), src),
                "grade": label_val(g, src, kind="claim"),
            }
        )
    out.more = max(0, len(claims) - limit)
    if not claims:
        out.empty_reason = "aucune affirmation causale sourcée n'est chargée pour ce sujet"
    return out


# ============================================================================== effect estimates
def estimate_src(e: Mapping[str, Any]) -> Src:
    s = e["source"]
    cit = s.get("citation") or {}
    return Src(
        source=s.get("source_key") or "effects",
        title=cit.get("short") or s.get("dataset") or "publication",
        dataset=s.get("dataset"),
        indicator=e["estimate_key"],
        period=f"{(e.get('scope') or {}).get('period', {}).get('from', '?')}–{(e.get('scope') or {}).get('period', {}).get('to', '?')}",
        licence="citation (valeur publiée, citée)",
        ref=e["estimate_key"],
        url=cit.get("url") or (f"https://doi.org/{cit['doi']}" if cit.get("doi") else None),
        verification="published_estimate",
    )


@query("effects.estimates")
def effects_estimates(
    ctx: Ctx, *, cause: str | None = None, effect: str | None = None, country: str | None = None, limit: int = 8
) -> Result:
    """Published effect estimates (attributed to their publication; never pooled)."""
    from reva.effects import query as eq

    rows = eq.list_estimates(ctx.conn, cause=cause, effect=effect, country=country, origin=ctx.origin, limit=100)
    out = Result()
    for e in rows[:limit]:
        src = estimate_src(e)
        u = e.get("unit")
        unc = e.get("uncertainty") or {}
        out.rows.append(
            {
                "cause": label_val(e["cause"].get("label_fr") or e["cause"].get("label") or e["cause"]["id"], src),
                "effect": label_val(e["effect"].get("label_fr") or e["effect"].get("label") or e["effect"]["id"], src),
                "measure": label_val({"elasticity": "élasticité", "response": "réponse", "semi_elasticity": "semi-élasticité"}.get(e["measure"], e["measure"]), src),
                "point": num_val(e.get("as_printed") or e.get("point"), src, unit=None if u in ("ratio", None) else u, kind="estimate"),
                "ci_low": num_val(unc.get("ci_low"), src, kind="estimate") if unc.get("ci_low") is not None else Miss("intervalle", "non publié par la source"),
                "ci_high": num_val(unc.get("ci_high"), src, kind="estimate") if unc.get("ci_high") is not None else Miss("intervalle", "non publié par la source"),
                "citation": label_val(src.title, src),
                "method": label_val(str(e.get("method") or ""), src),
            }
        )
    out.more = max(0, len(rows) - limit)
    if not rows:
        out.empty_reason = "aucune estimation d'effet publiée n'est chargée pour ce sujet"
    return out


# ============================================================================== forecast registry
def _registry_src(question_id: str, title: str, period: str | None, method: str | None = None) -> Src:
    return Src(
        source="reva.forecast_registry",
        title=title,
        dataset="registre prospectif (questions scellées)",
        indicator=question_id,
        period=period,
        licence="REVA (prévisions scellées, publiques)",
        ref=question_id,
        verification="sealed_forecast",
        method=method,
    )


MODEL_FR = {
    "reva.ensemble": "ensemble REVA",
    "forecast.reference_eia_steo": "EIA STEO (référence officielle)",
    "forecast.reference_imf_weo": "FMI WEO (référence officielle)",
    "forecast.registry_persistence": "persistance",
    "forecast.energy_rw": "marche aléatoire (énergie)",
    "forecast.registry_ar": "AR(p)",
}


@query("forecasts.family")
def forecasts_family(ctx: Ctx, *, family: str, geo: str | None = None, models: Iterable[str] = ("reva.ensemble",), reference: str | None = None, limit: int = 6) -> Result:
    """Pending / resolved sealed questions of one family: REVA's forecast, the official reference, the outcome."""
    from reva.evaluation.registry import read as R

    out = Result()
    qs, total = R.questions(ctx.conn, ctx.store, ctx.origin, family=family, geo=geo, limit=200)
    if not qs:
        out.empty_reason = f"aucune question scellée dans la famille {family}"
        return out
    qs.sort(key=lambda q: q["reference_period"])
    n_res = sum(1 for q in qs if q["status"] == "resolved")
    n_pend = sum(1 for q in qs if q["status"] == "pending")
    want = list(models)
    first = qs[0]
    fsrc = _registry_src(first["question_id"], f"Registre prospectif REVA — {first['label_fr']}", None)
    out.values["n_pending"] = num_val(n_pend, fsrc, kind="derived", digits=0)
    out.values["n_resolved"] = num_val(n_res, fsrc, kind="derived", digits=0)
    out.values["label"] = label_val(first["label_fr"], fsrc)
    last_obs = None
    for q in qs[:limit]:
        d = R.question(ctx.conn, ctx.store, q["question_id"])
        if d is None:
            continue
        qq = d["question"]
        last_obs = last_obs or qq.get("last_observed")
        per = q["reference_period"]
        src = _registry_src(q["question_id"], f"Registre prospectif REVA — {q['label_fr']}", per)
        row: dict[str, Item] = {"period": period_val(per, src), "status": label_val(
            {"pending": "en attente", "resolved": "résolue", "void": "annulée"}.get(q["status"], q["status"]), src)}
        by = {f["model"]: f for f in d["forecasts"]}
        for i, m in enumerate(want):
            f = by.get(m)
            key = "reva" if i == 0 else f"m{i}"
            if f is None:
                row[key] = Miss(MODEL_FR.get(m, m), "pas de prévision de ce modèle")
                continue
            qs_ = f.get("quantiles") or {}
            row[key] = num_val(f.get("point"), src, unit=qq.get("unit"), kind="forecast")
            row[f"{key}_lo"] = num_val(qs_.get("0.05"), src, unit=qq.get("unit"), kind="forecast") if qs_.get("0.05") else Miss("intervalle", "non publié")
            row[f"{key}_hi"] = num_val(qs_.get("0.95"), src, unit=qq.get("unit"), kind="forecast") if qs_.get("0.95") else Miss("intervalle", "non publié")
        if reference:
            f = by.get(reference)
            row["ref"] = num_val(f.get("point"), src, unit=qq.get("unit"), kind="projection") if f else Miss(MODEL_FR.get(reference, reference), "pas de prévision de référence")
        oc = d.get("outcome")
        row["outcome"] = num_val(oc.get("value"), src, unit=qq.get("unit")) if isinstance(oc, dict) and oc.get("value") is not None else Miss("résultat", "question non encore résolue")
        out.rows.append(row)
    if last_obs:
        lsrc = _registry_src(first["question_id"], f"Registre prospectif REVA — dernière observation ({first['label_fr']})", last_obs.get("period"))
        out.values["last_value"] = num_val(last_obs.get("value"), lsrc, unit=first.get("unit"))
        out.values["last_period"] = period_val(last_obs.get("period"), lsrc)
    out.values["judged"] = label_val(
        "non jugée (aucune question résolue)" if n_res == 0 else f"{n_res} question(s) résolue(s)", fsrc, kind="forecast"
    )
    return out


@query("forecasts.geo")
def forecasts_geo(ctx: Ctx, *, geo: str, limit: int = 8) -> Result:
    """Families with sealed questions about one geography (counts by status) and REVA's next forecasts."""
    from reva.evaluation.registry import read as R

    out = Result()
    qs, total = R.questions(ctx.conn, ctx.store, ctx.origin, geo=geo, limit=1000)
    if not qs:
        out.empty_reason = f"aucune question scellée sur {geo}"
        return out
    fams: dict[str, list[dict[str, Any]]] = {}
    for q in qs:
        fams.setdefault(q["family"], []).append(q)
    src0 = _registry_src(qs[0]["question_id"], "Registre prospectif REVA", None)
    out.values["n_questions"] = num_val(len(qs), src0, kind="derived", digits=0)
    out.values["n_families"] = num_val(len(fams), src0, kind="derived", digits=0)
    out.values["n_resolved"] = num_val(sum(1 for q in qs if q["status"] == "resolved"), src0, kind="derived", digits=0)
    rows = []
    for fam, lst in sorted(fams.items(), key=lambda t: -len(t[1])):
        lst.sort(key=lambda q: q["reference_period"])
        nxt = next((q for q in lst if q["status"] == "pending"), lst[0])
        src = _registry_src(nxt["question_id"], f"Registre prospectif REVA — {nxt['label_fr']}", nxt["reference_period"])
        row: dict[str, Item] = {
            "family": label_val(nxt["label_fr"], src),
            "n": num_val(len(lst), src, kind="derived", digits=0),
            "next_period": period_val(nxt["reference_period"], src),
            "resolution": label_val(fr_period(nxt.get("expected_resolution")) or str(nxt.get("expected_resolution")), src),
        }
        d = R.question(ctx.conn, ctx.store, nxt["question_id"])
        f = next((x for x in (d or {}).get("forecasts", []) if x["model"] == "reva.ensemble"), None) or next(
            (x for x in (d or {}).get("forecasts", []) if x["model"] == "forecast.registry_persistence"), None
        )
        if f is not None and f.get("point") is not None:
            row["forecast"] = num_val(f["point"], src, unit=nxt.get("unit"), kind="forecast")
            row["model"] = label_val(MODEL_FR.get(f["model"], f["model"]), src)
        else:
            row["forecast"] = Miss("prévision", "question binaire ou sans prévision ponctuelle")
            row["model"] = Miss("modèle", "—")
        rows.append(row)
    out.rows = rows[:limit]
    out.more = max(0, len(rows) - limit)
    return out


# ============================================================================== pre-registered evaluations (BEAT tracks)
@query("beat.tests")
def beat_tests(ctx: Ctx, *, file: str, code: str, benchmark_fr: str, title: str, limit: int = 6) -> Result:
    """Pre-registered tests of a BEAT track for one series code: ratio vs the official forecaster, Holm verdict."""
    p = REPO / file
    out = Result()
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        out.empty_reason = f"résultat {file} illisible"
        return out
    tests = [t for t in d.get("tests", []) if t.get("code") == code and t.get("loss") == "ae"]
    src = Src(
        source="reva.beat",
        title=title,
        dataset=file,
        indicator=d.get("protocol"),
        licence="REVA (protocole pré-enregistré, résultat publié)",
        ref=d.get("digest"),
        verification="preregistered_evaluation",
        method=f"Diebold-Mariano, correction de Holm sur {d.get('family_size')} tests",
    )
    seen = set()
    for t in sorted(tests, key=lambda t: (t.get("h", 0), t.get("ratio") or 9)):
        k = (t.get("window"), t.get("h"))
        if k in seen:
            continue
        seen.add(k)
        out.rows.append(
            {
                "window": label_val(str(t.get("window")), src),
                "h": num_val(t.get("h"), src, kind="derived", digits=0),
                "ratio": num_val(f"{t.get('ratio'):.2f}", src, kind="derived", digits=2) if t.get("ratio") is not None else Miss("ratio", "non calculé"),
                "n": num_val(t.get("n"), src, kind="derived", digits=0),
                "verdict": label_val(
                    f"bat {benchmark_fr} (Holm)" if t.get("holm_win") else f"ne bat pas {benchmark_fr}", src, kind="forecast"
                ),
            }
        )
    out.rows = out.rows[:limit]
    wins = [t for t in d.get("wins", []) if t.get("code") == code]
    out.values["n_wins"] = num_val(len({(t.get("window"), t.get("h")) for t in wins}), src, kind="derived", digits=0)
    out.values["n_tests"] = num_val(len(tests), src, kind="derived", digits=0)
    if wins:
        best = min(wins, key=lambda t: t.get("ratio") or 9)
        out.values["best_ratio"] = num_val(f"{best['ratio']:.2f}", src, kind="derived", digits=2)
        out.values["best_h"] = num_val(best.get("h"), src, kind="derived", digits=0)
    else:
        out.values["best_ratio"] = Miss("ratio", "aucun test gagné après Holm")
        out.values["best_h"] = Miss("horizon", "aucun test gagné après Holm")
    if not tests:
        out.empty_reason = f"aucun test pré-enregistré pour {code}"
    return out


# ============================================================================== decision engine
_DECISION_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def _run_decision(ctx: Ctx, reference_id: str) -> dict[str, Any] | None:
    from reva.predict.decision import reference_decisions
    from reva.predict.run import run_on_connection

    k = (reference_id, ctx.snapshot_id)
    if k in _DECISION_CACHE:
        return _DECISION_CACHE[k]
    d = reference_decisions().get(reference_id)
    if d is None:
        return None
    out = run_on_connection(ctx.conn, ctx.store, d, draws=2000, seed=20261017, as_of_max=None, origin=ctx.origin)
    _DECISION_CACHE[k] = out
    return out


def _decision_src(out: Mapping[str, Any], ident: str, title: str = "Moteur de décision REVA (scénario non validé)") -> Src:
    return Src(
        source="reva.predict",
        title=title,
        dataset=str(out.get("engine")),
        indicator=ident,
        licence="REVA",
        ref=(out.get("validation") or {}).get("chain_signature"),
        verification="unvalidated_scenario",
        method="propagation Monte-Carlo (2000 tirages, graine 20261017) des estimations attribuées ; ni prévision ni probabilité",
    )


@query("predict.decision")
def predict_decision(ctx: Ctx, *, reference_id: str, limit: int = 8) -> Result:
    """One reference decision propagated by the decision engine: direct change, variables, hops, gaps."""
    out = Result()
    res = _run_decision(ctx, reference_id)
    if res is None:
        out.empty_reason = f"décision de référence {reference_id} inconnue"
        return out
    dsrc = _decision_src(res, reference_id, "Registre des décisions de référence REVA")
    dec = res["decision"]
    out.values["label"] = label_val(dec.get("label_fr"), dsrc)
    out.values["actor"] = label_val((dec.get("actor") or {}).get("label_fr"), dsrc)
    out.values["start"] = period_val(dec.get("start"), dsrc)
    mag = dec.get("magnitude") or {}
    out.values["magnitude"] = num_val(mag.get("value"), dsrc, unit=mag.get("unit"), kind="scenario", show_unit=True)
    out.values["horizon"] = num_val(dec.get("horizon_quarters"), dsrc, kind="label", digits=0)
    out.values["validation"] = label_val((res.get("validation") or {}).get("label"), _decision_src(res, "validation"), kind="scenario")
    for i, dc in enumerate(res.get("direct_changes") or []):
        s = _decision_src(res, f"direct_change:{i}", "Moteur de décision REVA — hypothèse de scénario")
        out.values[f"direct_{i}"] = num_val(dc.get("change"), s, unit=dc.get("unit"), kind="scenario")
        out.values[f"direct_{i}_basis"] = label_val(dc.get("basis_fr"), s, kind="scenario")
    vsrc = _decision_src(res, "variables")
    for v in res.get("variables") or []:
        per = next((p for p in v["periods"] if p.get("p50") is not None), None)
        lab = (v.get("variable") or {}).get("label_fr") or v["id"]
        unit = {"percent": "%", "log_points": "points log", "pp": "points de %"}.get(v.get("unit"), v.get("unit"))
        row: dict[str, Item] = {"variable": label_val(lab, vsrc)}
        if per is None:
            reason = next((p.get("missing_reason") for p in v["periods"] if p.get("missing_reason")), "non quantifié")
            row.update({"period": Miss("période", str(reason)), "p50": Miss(lab, str(reason)), "p05": Miss(lab, str(reason)), "p95": Miss(lab, str(reason))})
        else:
            psrc = _decision_src(res, f"{v['id']}@{per['period']}")
            row["period"] = period_val(per["period"], psrc)
            for q in ("p05", "p50", "p95"):
                row[q] = num_val(f"{per[q]:.3f}", psrc, unit=unit, kind="scenario", digits=2)
            n_miss = sum(1 for p in v["periods"] if p.get("p50") is None)
            row["n_missing"] = num_val(n_miss, psrc, kind="derived", digits=0)
        out.rows.append(row)
    hops = []
    for h in res.get("hops") or []:
        if h.get("kind") != "estimate":
            continue
        key = h.get("estimate_key") or ""
        cit = key.split(":")[2] if key.count(":") >= 3 else key
        hsrc = Src(
            source="effects.literature",
            title=cit.replace("_", " "),
            dataset="kg.effect_estimate_current",
            indicator=key,
            licence="citation (valeur publiée, citée)",
            ref=key,
            verification="published_estimate",
        )
        coef = h.get("coefficient") or {}
        hops.append(
            {
                "cause": label_val(h.get("cause_definition"), hsrc),
                "effect": label_val(h.get("effect_definition"), hsrc),
                "coef": num_val(coef.get("point"), hsrc, kind="estimate"),
                "citation": label_val(cit.replace("_", " "), hsrc),
                "applicability": label_val("; ".join((h.get("applicability") or {}).get("notes") or []) or "—", hsrc),
            }
        )
    out.values["n_hops"] = num_val(len(hops), vsrc, kind="derived", digits=0)
    ctx.memo[("decision_hops", reference_id)] = hops
    ctx.memo[("decision_unq", reference_id)] = res.get("unquantified_links") or []
    for g in res.get("gaps") or []:
        msg = g.get("message_fr") or "; ".join(g.get("message") or []) or g.get("code")
        out.missing.append(Miss(str(g.get("node") or g.get("plugin") or g.get("code")), str(msg)))
    return out


@query("predict.hops")
def predict_hops(ctx: Ctx, *, reference_id: str) -> Result:
    if ("decision_hops", reference_id) not in ctx.memo:
        predict_decision(ctx, reference_id=reference_id)
    rows = ctx.memo.get(("decision_hops", reference_id)) or []
    return Result(rows=rows, empty_reason=None if rows else "aucune estimation chiffrée sur le chemin")


@query("predict.unquantified")
def predict_unquantified(ctx: Ctx, *, reference_id: str, limit: int = 6) -> Result:
    if ("decision_unq", reference_id) not in ctx.memo:
        predict_decision(ctx, reference_id=reference_id)
    out = Result()
    for c in (ctx.memo.get(("decision_unq", reference_id)) or [])[:limit]:
        s = c.get("source") or {}
        src = Src(
            source=s.get("source_key") or "kg",
            title=source_title(s.get("source_key") or "kg"),
            indicator=f"affirmation causale, niveau {s.get('tier')}, note {c.get('grade')}",
            ref=c.get("claim_key"),
            licence="CC0-1.0" if str(s.get("source_key", "")).startswith("wikidata") else None,
            verification="community_edited_not_verified" if str(s.get("source_key", "")).startswith("wikidata") else None,
        )
        out.rows.append(
            {
                "cause": label_val((c.get("from") or {}).get("label"), src),
                "effect": label_val((c.get("to") or {}).get("label"), src),
                "grade": label_val(str(c.get("grade") or "?"), src, kind="claim"),
                "source": label_val(src.title, src),
            }
        )
    if not out.rows:
        out.empty_reason = "aucun lien causal non chiffré"
    return out


@query("predict.references")
def predict_references(ctx: Ctx, *, instruments: Iterable[str] = (), ids: Iterable[str] = ()) -> Result:
    """Reference decisions of the engine that touch this subject (by instrument or id)."""
    from reva.predict.decision import reference_decisions

    out = Result()
    inst, idset = set(instruments), set(ids)
    for d in reference_decisions().values():
        dd = d.model_dump(mode="json")
        if dd["instrument"] in inst or dd["id"] in idset:
            src = Src(source="reva.predict", title="Registre des décisions de référence REVA", indicator=dd["id"], licence="REVA", ref=dd["id"])
            out.rows.append(
                {
                    "label": label_val(dd.get("label_fr"), src, href=f"/v8/dossier/decision/{dd['id']}"),
                    "start": period_val(dd.get("start"), src),
                }
            )
    if not out.rows:
        out.empty_reason = "aucune décision de référence ne porte sur ce sujet"
    return out


# ============================================================================== small helpers
def _f(v: Any) -> float | None:
    d = to_decimal(v)
    return float(d) if d is not None else None


def _text(i: Item) -> str:
    return i.text if isinstance(i, Val) else "manquant"


def _num(i: Item) -> float | None:
    return i.num if isinstance(i, Val) else None


def _exact(i: Item) -> str | None:
    return i.exact if isinstance(i, Val) else None


__all__ = ["QUERIES", "country_val", "fr_number"]
