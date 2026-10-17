"""Atlas checks: one pass over the latest version of every atlas table (Parquet read through the store API).

Per table: unit/scale sanity (bounded shares, negative counts), time sanity (future-dated observations, periods not
ended, forecasts carried as observations, projection tables without projection flags, period precision), licence
of the table and provenance of its cells (raw sha256 listed by the table manifest). The cells of the series that
the cross-source pairs and the per-capita identities need are collected on the way and compared at the end.

Projection rule = :data:`reva.atlas.query.PROJECTION_SQL` (estimate_kind projection / unknown boundary, a truthy
``projection`` flag, ``period_after_vintage``). Nothing is modified; findings name table, indicator, provider
geography and period, which identify a cell uniquely.
"""

from __future__ import annotations

import gc
import itertools
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from reva.atlas.catalog import Catalog
from reva.atlas.snapshot import normalise_rights
from reva.atlas.source_order import HeadlineRegistry
from reva.qa.backend import QaBackend, lit
from reva.qa.config import QaConfig
from reva.qa.report import CheckResult, status_for

PROJECTION_KINDS = ("projection", "unknown_boundary", "published_history_boundary_unknown")
READ_COLUMNS = [
    "source",
    "dataset",
    "indicator",
    "iso3",
    "provider_geo",
    "period_start",
    "period_end",
    "value",
    "unit",
    "flags",
    "raw_sha256",
]
EX = 8  # examples kept per check


@dataclass
class AtlasTable:
    dataset_key: str
    source: str
    dataset: str
    sha256: str
    row_count: int
    rights: dict[str, Any] | None
    raw_sha256s: list[str]

    @property
    def key(self) -> str:
        return f"{self.source}.{self.dataset}"


def atlas_tables(be: QaBackend, origin: str) -> list[AtlasTable]:
    rows = be.rows(
        "SELECT x.dataset_key, x.subject_key, x.artifact_sha256, x.row_count, CAST(x.coverage AS TEXT) AS coverage, "
        "CAST(a.rights AS TEXT) AS rights FROM (SELECT d.dataset_key, d.subject_key, d.artifact_sha256, d.row_count, "
        "d.coverage, ROW_NUMBER() OVER (PARTITION BY d.dataset_key ORDER BY d.recorded_at DESC) AS rn "
        f"FROM evidence.dataset d WHERE d.concept_id = 'atlas.table' AND d.origin = {lit(origin)}) x "
        "LEFT JOIN meta.artifact a ON a.sha256 = x.artifact_sha256 WHERE x.rn = 1 ORDER BY x.dataset_key"
    )
    out = []
    for r in rows:
        source, dataset = str(r["subject_key"]).split(".", 1)
        cov = json.loads(r["coverage"]) if r["coverage"] else {}
        rights = json.loads(r["rights"]) if r["rights"] else None
        out.append(
            AtlasTable(
                r["dataset_key"],
                source,
                dataset,
                r["artifact_sha256"],
                int(r["row_count"]),
                rights,
                list(cov.get("raw_sha256s") or []),
            )
        )
    return out


def projection_frame(df: pl.DataFrame) -> pl.DataFrame:
    """Adds ``is_proj`` (bool) and ``available_at`` (date or null) parsed only where the flags mention them."""
    has = pl.col("flags").str.contains("estimate_kind|projection|period_after_vintage|available_at")
    df = df.with_row_index("_r")
    sub = (
        df.filter(has)
        .select(
            "_r",
            pl.col("flags").str.json_path_match("$.estimate_kind").alias("_ek"),
            pl.col("flags").str.json_path_match("$.projection").alias("_pj"),
            pl.col("flags").str.json_path_match("$.period_after_vintage").alias("_pav"),
            pl.col("flags")
            .str.json_path_match("$.available_at")
            .str.slice(0, 10)
            .str.to_date("%Y-%m-%d", strict=False)
            .alias("available_at"),
        )
        .with_columns(
            (
                pl.col("_ek").is_in(PROJECTION_KINDS).fill_null(False)
                | (pl.col("_pj").is_not_null() & ~pl.col("_pj").is_in(["false", ""])).fill_null(False)
                | (pl.col("_pav") == "true").fill_null(False)
            ).alias("is_proj")
        )
        .select("_r", "is_proj", "available_at")
    )
    return df.join(sub, on="_r", how="left").with_columns(pl.col("is_proj").fill_null(False)).drop("_r")


def _calendar_year(df: pl.DataFrame) -> pl.DataFrame:
    cal = (
        (pl.col("period_start").dt.month() == 1)
        & (pl.col("period_start").dt.day() == 1)
        & (pl.col("period_end") == pl.col("period_start").dt.offset_by("1y"))
    )
    return df.filter(cal).with_columns(pl.col("period_start").dt.year().alias("year"))


def _cell(t: str, r: dict[str, Any], *extra: str) -> dict[str, Any]:
    out = {
        "table": t,
        "indicator": r.get("indicator"),
        "geo": r.get("provider_geo"),
        "period": f"{r.get('period_start')}..{r.get('period_end')}",
        "value": r.get("value"),
    }
    for k in extra:
        out[k] = r.get(k)
    if r.get("raw_sha256"):
        out["raw"] = str(r["raw_sha256"])[:16]
    return out


@dataclass
class _Acc:
    count: int = 0
    checked: int = 0
    by: Counter[str] = field(default_factory=Counter)
    examples: list[dict[str, Any]] = field(default_factory=list)

    def add(self, key: str, n: int, examples: list[dict[str, Any]]) -> None:
        if n:
            self.count += n
            self.by[key] += n
            if len(self.examples) < EX:
                self.examples += examples[: EX - len(self.examples)]


class AtlasScan:
    def __init__(self, cfg: QaConfig, catalog: Catalog, headlines: HeadlineRegistry, today: date) -> None:
        self.cfg = cfg
        self.today = today
        self.catalog = catalog
        self.headlines = headlines
        u = cfg.section("units")
        t = cfg.section("time")
        self.bounded = u.get("bounded") or []
        self.btol = float(u.get("bounded_tolerance") or 0.0)
        self.counts = u.get("counts") or {}
        self.identities = u.get("identities") or []
        self.grace = int(t.get("future_grace_days") or 0)
        self.proj_tables = set(t.get("projection_tables") or [])
        self.suffixes = {k: tuple(v) for k, v in (t.get("precision_suffixes") or {}).items()}
        # series -> (catalog concept id, unit)
        self.series_concept: dict[tuple[str, str, str], tuple[str, str]] = {}
        for c in catalog.concepts.values():
            for s in c.source_order:
                self.series_concept.setdefault((s.source, s.dataset, s.indicator), (c.id, c.unit))
        self.pairs = self._pairs()
        need: dict[tuple[str, str], set[str]] = defaultdict(set)
        for p in self.pairs:
            for side in ("a", "b"):
                src, ds, ind = p[side]["series"].split(".", 2)
                need[(src, ds)].add(ind)
        for idn in self.identities:
            for s in [*idn["product"], idn["total"]]:
                src, ds, ind = s.split(".", 2)
                need[(src, ds)].add(ind)
        self.need = need
        self.series: dict[str, list[pl.DataFrame]] = defaultdict(list)
        self.acc: dict[str, _Acc] = defaultdict(_Acc)
        self.tables_scanned = 0
        self.rows_scanned = 0
        self.raw_refs: dict[str, str] = {}  # raw sha -> first table citing it
        self.table_shas: dict[str, str] = {}
        self.licence_rows: list[dict[str, Any]] = []
        self.mixed_precision: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ pair declarations
    def _pairs(self) -> list[dict[str, Any]]:
        pairs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for c in self.headlines.concepts.values():
            for x, y in itertools.combinations(c.series, 2):
                k = tuple(sorted((x.key, y.key)))
                if k in seen:
                    continue
                seen.add(k)  # type: ignore[arg-type]
                excl = {**x.excluded_countries(), **y.excluded_countries()}
                pairs.append(
                    {
                        "kind": "headline",
                        "name": c.id,
                        "a": {"series": x.key, "scale": float(x.scale)},
                        "b": {"series": y.key, "scale": float(y.scale)},
                        "exclude": excl,
                        **self.cfg.tolerance(c.id),
                    }
                )
        for cc in self.catalog.concepts.values():
            if cc.value_kind != "numeric":
                continue
            for sx, sy in itertools.combinations(cc.source_order, 2):
                k = tuple(sorted((sx.key, sy.key)))
                if sx.source == sy.source or k in seen:
                    continue
                seen.add(k)  # type: ignore[arg-type]
                pairs.append(
                    {
                        "kind": "catalog",
                        "name": cc.id,
                        "a": {"series": sx.key},
                        "b": {"series": sy.key},
                        "exclude": {},
                        **self.cfg.tolerance(cc.id),
                    }
                )
        for p in self.cfg.section("cross_source").get("explicit") or []:
            tol = self.cfg.tolerance(p["name"])
            tol.update({k: p[k] for k in ("rel_tol", "abs_tol", "max_diverging_share", "min_overlap") if k in p})
            pairs.append(
                {
                    "kind": "explicit",
                    "name": p["name"],
                    "a": dict(p["a"]),
                    "b": dict(p["b"]),
                    "exclude": dict(p.get("exclude") or {}),
                    "note": p.get("note"),
                    **tol,
                }
            )
        return pairs

    # ------------------------------------------------------------------ per table
    def scan(self, t: AtlasTable, df: pl.DataFrame) -> None:
        self.tables_scanned += 1
        self.rows_scanned += df.height
        self.table_shas[t.sha256] = t.key
        df = projection_frame(df)
        self._units(t, df)
        self._time(t, df)
        self._provenance(t, df)
        self._licence(t)
        self._collect(t, df)

    def _rule_frame(self, t: AtlasTable, df: pl.DataFrame) -> pl.DataFrame | None:
        groups = df.group_by("indicator", "unit").len()
        rows = []
        for g in groups.iter_rows(named=True):
            concept, eff = self.series_concept.get((t.source, t.dataset, g["indicator"]), (None, g["unit"]))
            rule = None
            for b in self.bounded:
                if not re.search(b["unit_regex"], eff or ""):
                    continue
                if b.get("concept_regex"):
                    if concept is None or not re.search(b["concept_regex"], concept):
                        continue
                    if b.get("concept_exempt_regex") and re.search(b["concept_exempt_regex"], concept):
                        continue
                rule = (b["name"], float(b["min"]), float(b["max"]), eff)
                break
            if rule is None and self.counts and re.search(self.counts["unit_regex"], eff or ""):
                ex = self.counts.get("exempt_regex")
                if not ex or not (re.search(ex, g["indicator"]) or (concept and re.search(ex, concept))):
                    rule = ("non_negative_count", 0.0, float("inf"), eff)
            if rule:
                rows.append(
                    {
                        "indicator": g["indicator"],
                        "unit": g["unit"],
                        "_rule": rule[0],
                        "_lo": rule[1],
                        "_hi": rule[2],
                        "_eff_unit": rule[3],
                    }
                )
        return pl.DataFrame(rows) if rows else None

    def _units(self, t: AtlasTable, df: pl.DataFrame) -> None:
        rules = self._rule_frame(t, df)
        if rules is None:
            return
        x = (
            df.join(rules, on=["indicator", "unit"], how="inner")
            .with_columns(pl.col("value").cast(pl.Float64, strict=False).alias("_v"))
            .filter(pl.col("_v").is_not_null())
        )
        for rule in ("bounded", "count"):
            sel = (
                x.filter(pl.col("_rule") != "non_negative_count")
                if rule == "bounded"
                else x.filter(pl.col("_rule") == "non_negative_count")
            )
            acc = self.acc["units.bounded_share" if rule == "bounded" else "units.negative_count"]
            acc.checked += sel.height
            bad = sel.filter((pl.col("_v") < pl.col("_lo") - self.btol) | (pl.col("_v") > pl.col("_hi") + self.btol))
            if bad.height:
                ex = [
                    _cell(t.key, r, "_rule", "_eff_unit", "is_proj")
                    for r in bad.sort("_v", descending=True).head(3).iter_rows(named=True)
                ]
                acc.add(t.key, bad.height, ex)

    def _time(self, t: AtlasTable, df: pl.DataFrame) -> None:
        obs = df.filter(~pl.col("is_proj"))
        today = self.today
        limit = today + timedelta(days=self.grace)
        a = self.acc["time.future_observation"]
        a.checked += obs.height
        fut = obs.filter(pl.col("period_start") > limit)
        a.add(t.key, fut.height, [_cell(t.key, r, "flags") for r in fut.head(2).iter_rows(named=True)])
        a = self.acc["time.period_not_ended"]
        a.checked += obs.height
        cur = obs.filter((pl.col("period_start") <= limit) & (pl.col("period_end") > limit + timedelta(days=1)))
        a.add(t.key, cur.height, [_cell(t.key, r) for r in cur.head(2).iter_rows(named=True)])
        a = self.acc["time.forecast_as_observation"]
        av = obs.filter(pl.col("available_at").is_not_null())
        a.checked += av.height
        bad = av.filter(pl.col("period_start") >= pl.col("available_at"))
        a.add(t.key, bad.height, [_cell(t.key, r, "available_at", "flags") for r in bad.head(2).iter_rows(named=True)])
        a = self.acc["time.period_open_at_publication"]
        a.checked += av.height
        bad = av.filter(
            (pl.col("period_start") < pl.col("available_at"))
            & (pl.col("period_end") > pl.col("available_at") + pl.duration(days=1))
        )
        a.add(t.key, bad.height, [_cell(t.key, r, "available_at") for r in bad.head(2).iter_rows(named=True)])
        if t.key in self.proj_tables:
            a = self.acc["time.projection_table_unflagged"]
            a.checked += df.height
            a.add(t.key, obs.height, [_cell(t.key, r, "flags") for r in obs.head(2).iter_rows(named=True)])
        days = (pl.col("period_end") - pl.col("period_start")).dt.total_days()
        for suf, (lo, hi) in self.suffixes.items():
            if t.dataset.endswith(suf):
                a = self.acc["time.precision_mismatch"]
                a.checked += df.height
                bad = df.filter((days < lo) | (days > hi))
                a.add(t.key, bad.height, [_cell(t.key, r) for r in bad.head(2).iter_rows(named=True)])
                break
        cls = (
            pl.when(days <= 1)
            .then(pl.lit("day"))
            .when(days <= 7)
            .then(pl.lit("week"))
            .when(days.is_between(28, 31))
            .then(pl.lit("month"))
            .when(days.is_between(89, 92))
            .then(pl.lit("quarter"))
            .when(days.is_between(181, 184))
            .then(pl.lit("half"))
            .when(days.is_between(365, 366))
            .then(pl.lit("year"))
            .otherwise(pl.lit("other"))
        )
        mix = df.group_by("indicator").agg(cls.unique().alias("classes")).filter(pl.col("classes").list.len() > 1)
        a = self.acc["time.mixed_precision_indicator"]
        a.checked += df.select(pl.col("indicator").n_unique()).item()
        a.add(
            t.key,
            mix.height,
            [
                {"table": t.key, "indicator": r["indicator"], "classes": sorted(r["classes"])}
                for r in mix.head(2).iter_rows(named=True)
            ],
        )

    def _provenance(self, t: AtlasTable, df: pl.DataFrame) -> None:
        shas = df.group_by("raw_sha256").len()
        listed = set(t.raw_sha256s)
        a = self.acc["provenance.atlas_cell_raw"]
        a.checked += df.height
        bad_n, ex = 0, []
        for r in shas.iter_rows(named=True):
            sha = r["raw_sha256"]
            if sha:
                self.raw_refs.setdefault(sha, t.key)
            if not sha or sha not in listed:
                bad_n += r["len"]
                ex.append(
                    {
                        "table": t.key,
                        "raw_sha256": sha,
                        "rows": r["len"],
                        "why": "null" if not sha else "not in the table manifest raw_sha256s",
                    }
                )
        a.add(t.key, bad_n, ex[:2])

    def _licence(self, t: AtlasTable) -> None:
        from reva.atlas.query import source_spec_rights

        r = normalise_rights(t.rights, basis="table_artifact")
        basis = "table_artifact"
        if r is None:
            r = source_spec_rights(t.source)
            basis = "atlas_source_spec" if r else "none"
        pending = json.dumps(t.rights or {}).lower()
        review = [v for v in self.cfg.section("licence").get("pending_review_values") or [] if v in pending]
        self.licence_rows.append(
            {
                "table": t.key,
                "rows": t.row_count,
                "basis": basis,
                "licence": (r or {}).get("licence"),
                "restricted": bool(r and r.get("restricted")),
                "commercial_use": (r or {}).get("commercial_use"),
                "review_pending": bool(review),
            }
        )

    def _collect(self, t: AtlasTable, df: pl.DataFrame) -> None:
        inds = self.need.get((t.source, t.dataset))
        if not inds:
            return
        x = (
            df.filter(pl.col("indicator").is_in(sorted(inds)) & pl.col("iso3").is_not_null() & ~pl.col("is_proj"))
            .with_columns(pl.col("value").cast(pl.Float64, strict=False).alias("v"))
            .filter(pl.col("v").is_not_null())
        )
        for ind, g in x.partition_by("indicator", as_dict=True).items():
            name = ind[0] if isinstance(ind, tuple) else ind
            self.series[f"{t.source}.{t.dataset}.{name}"].append(
                g.select("iso3", "period_start", "period_end", "v", "unit", "raw_sha256")
            )

    # ------------------------------------------------------------------ after the pass
    def _yearly(self, series: str, aggregate: str | None = None) -> pl.DataFrame | None:
        frames = self.series.get(series)
        if not frames:
            return None
        df = pl.concat(frames)
        if aggregate == "annual_mean":
            days = (pl.col("period_end") - pl.col("period_start")).dt.total_days()
            m = df.filter(days.is_between(28, 31)).with_columns(pl.col("period_start").dt.year().alias("year"))
            return (
                m.group_by("iso3", "year")
                .agg(pl.col("v").mean(), pl.len().alias("n"), pl.col("unit").first(), pl.col("raw_sha256").first())
                .filter(pl.col("n") == 12)
                .drop("n")
            )
        y = _calendar_year(df)
        return y.unique(subset=["iso3", "year"], keep="first").select("iso3", "year", "v", "unit", "raw_sha256")

    def cross_source(self) -> list[CheckResult]:
        out = []
        for kind in ("headline", "catalog", "explicit"):
            rows: list[dict[str, Any]] = []
            examples: list[dict[str, Any]] = []
            n_div = n_overlap = 0
            worst = "pass"
            for p in [p for p in self.pairs if p["kind"] == kind]:
                a = self._yearly(p["a"]["series"], p["a"].get("aggregate"))
                b = self._yearly(p["b"]["series"], p["b"].get("aggregate"))
                base = {"concept": p["name"], "a": p["a"]["series"], "b": p["b"]["series"]}
                if a is None or b is None:
                    if kind != "catalog":  # catalog pairs over tables not loaded are too many to list
                        rows.append({**base, "overlap": 0, "diverging": 0, "note": "series absent from the atlas"})
                    continue
                sa, sb = float(p["a"].get("scale", 1)), float(p["b"].get("scale", 1))
                j = a.join(b, on=["iso3", "year"], suffix="_b").with_columns(
                    (pl.col("v") * sa).alias("va"), (pl.col("v_b") * sb).alias("vb")
                )
                excl = set(p.get("exclude") or {})
                if excl:
                    j = j.filter(~pl.col("iso3").is_in(sorted(excl)))
                j = j.with_columns((pl.col("va") - pl.col("vb")).abs().alias("d")).with_columns(
                    (pl.col("d") / pl.max_horizontal(pl.col("va").abs(), pl.col("vb").abs())).fill_nan(0.0).alias("rel")
                )
                rel_tol, abs_tol = float(p.get("rel_tol", 0.05)), float(p.get("abs_tol", 0.0))
                div = j.filter((pl.col("d") > abs_tol) & (pl.col("rel") > rel_tol))
                n = j.height
                if n < int(p.get("min_overlap", 5)):
                    if kind != "catalog" or n:
                        rows.append(
                            {**base, "overlap": n, "diverging": div.height, "note": "overlap below min_overlap"}
                        )
                    continue
                share = div.height / n
                n_div += div.height
                n_overlap += n
                status = (
                    "warn" if share > float(p.get("max_diverging_share", 0.1)) else ("info" if div.height else "pass")
                )
                if status == "warn" or (status == "info" and worst == "pass"):
                    worst = status
                top = div.sort("rel", descending=True)
                ctry = top.group_by("iso3").len().sort("len", descending=True).head(5)
                rows.append(
                    {
                        **base,
                        "overlap": n,
                        "diverging": div.height,
                        "share": round(share, 4),
                        "max_rel": round(float(top["rel"][0]), 4) if div.height else 0.0,
                        "rel_tol": rel_tol,
                        "abs_tol": abs_tol,
                        "status": status,
                        "top_countries": ", ".join(f"{r['iso3']}×{r['len']}" for r in ctry.iter_rows(named=True)),
                        "units": f"{a['unit'][0]} / {b['unit'][0]}",
                    }
                )
                for r in top.head(2).iter_rows(named=True):
                    examples.append(
                        {
                            "concept": p["name"],
                            "iso3": r["iso3"],
                            "year": r["year"],
                            "a": p["a"]["series"],
                            "a_value": r["va"],
                            "a_raw": str(r["raw_sha256"])[:16],
                            "b": p["b"]["series"],
                            "b_value": r["vb"],
                            "b_raw": str(r["raw_sha256_b"])[:16],
                            "rel_diff": round(float(r["rel"]), 4),
                        }
                    )
            rows.sort(key=lambda r: (-(r.get("share") or 0), r["concept"]))
            examples.sort(key=lambda e: -e["rel_diff"])
            out.append(
                CheckResult(
                    f"cross_source.{kind}",
                    "cross_source",
                    {
                        "headline": "headline concepts (source_order series of one quantity)",
                        "catalog": "catalog concepts listing several sources",
                        "explicit": "declared source pairs (registry/qa/checks.yaml)",
                    }[kind],
                    worst,  # type: ignore[arg-type]
                    n_div,
                    n_overlap,
                    examples[:EX],
                    rows,
                    note=(
                        "Cell pairs (country, calendar year, non-projection) beyond tolerance; status warn when a pair's "
                        "diverging share exceeds max_diverging_share. Report only: no value is chosen, blended or fixed."
                    ),
                )
            )
        return out

    def identities_check(self) -> CheckResult:
        rows, examples = [], []
        total = checked = 0
        for idn in self.identities:
            frames = [self._yearly(s) for s in idn["product"]]
            tot = self._yearly(idn["total"])
            if tot is None or any(f is None for f in frames):
                rows.append({"identity": idn["name"], "checked": 0, "violations": 0, "note": "series absent"})
                continue
            j = frames[0].select("iso3", "year", pl.col("v").alias("p0"))  # type: ignore[union-attr]
            for i, f in enumerate(frames[1:], 1):
                j = j.join(f.select("iso3", "year", pl.col("v").alias(f"p{i}")), on=["iso3", "year"])  # type: ignore[union-attr]
            j = j.join(tot.select("iso3", "year", pl.col("v").alias("total"), "raw_sha256"), on=["iso3", "year"])
            prod = pl.lit(float(idn.get("scale", 1)))
            for i in range(len(frames)):
                prod = prod * pl.col(f"p{i}")
            j = j.with_columns(prod.alias("product")).with_columns(
                ((pl.col("product") - pl.col("total")).abs() / pl.col("total").abs()).alias("rel")
            )
            bad = j.filter(pl.col("rel") > float(idn.get("rel_tol", 0.01))).sort("rel", descending=True)
            total += bad.height
            checked += j.height
            rows.append(
                {
                    "identity": idn["name"],
                    "checked": j.height,
                    "violations": bad.height,
                    "rel_tol": idn.get("rel_tol"),
                    "worst": (f"{bad['iso3'][0]} {bad['year'][0]} rel {bad['rel'][0]:.3g}" if bad.height else ""),
                }
            )
            examples += [
                {
                    "identity": idn["name"],
                    "iso3": r["iso3"],
                    "year": r["year"],
                    "product": r["product"],
                    "total": r["total"],
                    "rel_diff": round(float(r["rel"]), 5),
                }
                for r in bad.head(3).iter_rows(named=True)
            ]
        return CheckResult(
            "units.per_capita_identity",
            "units",
            "per-capita × population vs published total",
            status_for(total, "warn"),
            total,
            checked,
            examples[:EX],
            rows,
            note="Same provider's per-capita value × population (× scale) compared with its total.",
        )

    def results(self) -> list[CheckResult]:
        titles = {
            "units.bounded_share": ("shares / bounded indices outside their declared range", "warn"),
            "units.negative_count": ("negative values in count-like units", "warn"),
            "time.future_observation": ("non-projection cells starting after today", "fail"),
            "time.period_not_ended": ("non-projection cells for a period that has not ended", "info"),
            "time.forecast_as_observation": ("non-projection cells starting at/after their availability date", "fail"),
            "time.period_open_at_publication": (
                "non-projection cells whose period was still open when published",
                "info",
            ),
            "time.projection_table_unflagged": ("cells of projection tables without a projection flag", "fail"),
            "time.precision_mismatch": ("period length differs from the table's declared frequency", "warn"),
            "time.mixed_precision_indicator": ("indicators mixing period lengths (day/month/quarter/year…)", "info"),
            "provenance.atlas_cell_raw": ("atlas cells whose raw_sha256 is not a raw object of the table", "fail"),
        }
        out = []
        for cid, (title, sev) in titles.items():
            a = self.acc.get(cid, _Acc())
            dom = cid.split(".", 1)[0]
            out.append(
                CheckResult(
                    cid,
                    dom,
                    title,
                    status_for(a.count, sev),  # type: ignore[arg-type]
                    a.count,
                    a.checked,
                    a.examples,
                    [{"table": k, "cells": v} for k, v in a.by.most_common()],
                )
            )
        lic = self.licence_rows
        none = [r for r in lic if r["basis"] == "none"]
        out.append(
            CheckResult(
                "licence.atlas_tables",
                "licence",
                "atlas tables without a licence",
                status_for(len(none), "fail"),
                len(none),
                len(lic),
                none[:EX],
                [
                    {
                        "basis": b,
                        "tables": sum(1 for r in lic if r["basis"] == b),
                        "rows": sum(r["rows"] for r in lic if r["basis"] == b),
                    }
                    for b in ("table_artifact", "atlas_source_spec", "none")
                ],
                note="Licence from the table artifact's rights, else the atlas source spec (as the atlas API serves it).",
            )
        )
        restricted = [r for r in lic if r["restricted"]]
        out.append(
            CheckResult(
                "licence.atlas_restricted",
                "licence",
                "atlas tables under restricted terms (must be named with values)",
                "info" if restricted else "pass",
                sum(r["rows"] for r in restricted),
                sum(r["rows"] for r in lic),
                [],
                [
                    {k: r[k] for k in ("table", "rows", "commercial_use", "licence")}
                    for r in sorted(restricted, key=lambda r: -r["rows"])
                ],
                note="Not a defect: non-commercial / share-alike / no-derivatives / not-open terms, flagged per value.",
            )
        )
        pend = [r for r in lic if r["review_pending"]]
        out.append(
            CheckResult(
                "licence.atlas_review_pending",
                "licence",
                "atlas tables whose licence review is pending",
                "info" if pend else "pass",
                len(pend),
                len(lic),
                [],
                [{"table": r["table"], "rows": r["rows"]} for r in sorted(pend, key=lambda r: -r["rows"])],
            )
        )
        return out


def run_atlas(
    be: QaBackend,
    cfg: QaConfig,
    origin: str,
    catalog: Catalog,
    headlines: HeadlineRegistry,
    domains: set[str],
    *,
    tables: list[str] | None = None,
    log: Any = None,
) -> tuple[AtlasScan, list[CheckResult]]:
    scan = AtlasScan(cfg, catalog, headlines, be.today)
    refs = atlas_tables(be, origin)
    if tables:
        refs = [r for r in refs if r.key in tables or any(re.fullmatch(p, r.key) for p in tables)]
    for i, t in enumerate(refs):
        try:
            df = be.read_parquet(t.sha256, READ_COLUMNS)
        except Exception as e:
            scan.acc["provenance.atlas_table_unreadable"].add(t.key, 1, [{"table": t.key, "error": type(e).__name__}])
            continue
        scan.scan(t, df)
        del df
        if i % 25 == 0:
            gc.collect()
            if log:
                log(f"[qa atlas] {i + 1}/{len(refs)} tables, {scan.rows_scanned:,} rows")
    results: list[CheckResult] = []
    for r in scan.results():
        if r.domain in domains:
            results.append(r)
    if "cross_source" in domains:
        results += scan.cross_source()
    if "units" in domains:
        results.append(scan.identities_check())
    if "provenance" in domains and scan.acc.get("provenance.atlas_table_unreadable"):
        a = scan.acc["provenance.atlas_table_unreadable"]
        results.append(
            CheckResult(
                "provenance.atlas_table_unreadable",
                "provenance",
                "atlas tables that cannot be read",
                "fail",
                a.count,
                len(refs),
                a.examples,
            )
        )
    return scan, results
