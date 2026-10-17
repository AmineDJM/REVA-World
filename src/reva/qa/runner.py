"""Orchestrates the QA domains over one backend and returns a :class:`~reva.qa.report.QaReport`."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from reva.atlas.catalog import Catalog
from reva.atlas.source_order import HeadlineRegistry
from reva.qa import db_checks as dbc
from reva.qa.atlas_checks import run_atlas
from reva.qa.backend import QaBackend
from reva.qa.config import DOMAINS, QaConfig, load_config
from reva.qa.report import CheckResult, QaReport

ATLAS_DOMAINS = {"cross_source", "units", "time", "licence", "provenance"}


def _guard(results: list[CheckResult], cid: str, domain: str, fn: Callable[[], list[CheckResult]]) -> None:
    """A check that raises becomes an ``error`` result; the run continues."""
    try:
        results += fn()
    except Exception as e:
        results.append(CheckResult(cid, domain, "check raised", "error", note=f"{type(e).__name__}: {str(e)[:300]}"))


def run_qa(
    be: QaBackend,
    *,
    domains: Iterable[str] | None = None,
    origin: str = "live",
    cfg: QaConfig | None = None,
    catalog: Catalog | None = None,
    headlines: HeadlineRegistry | None = None,
    tables: list[str] | None = None,
    verify_all: bool = False,
    skip_atlas: bool = False,
    log: Callable[[str], None] | None = None,
) -> QaReport:
    cfg = cfg or load_config()
    doms = [d for d in DOMAINS if d in set(domains or DOMAINS)]
    dset = set(doms)
    t0 = time.time()
    report = QaReport(
        datetime.now(UTC).isoformat(timespec="seconds"), be.today.isoformat(), origin, be.mode, cfg.sha256, doms
    )
    results: list[CheckResult] = []
    ctx: dict[str, Any] = {}
    referenced: dict[str, str] = {}

    if dset & ATLAS_DOMAINS and not skip_atlas:
        if catalog is None:
            from reva.atlas.catalog import load_catalog

            catalog = load_catalog(strict=False)
        if headlines is None:
            from reva.atlas.source_order import load_headlines

            headlines = load_headlines(strict=False)

        def atlas() -> list[CheckResult]:
            scan, res = run_atlas(be, cfg, origin, catalog, headlines, dset, tables=tables, log=log)
            ctx["atlas_tables"] = scan.tables_scanned
            ctx["atlas_rows"] = scan.rows_scanned
            referenced.update(scan.raw_refs)
            for sha, key in scan.table_shas.items():
                referenced.setdefault(sha, f"table {key}")
            return res

        _guard(results, "atlas", "units", atlas)
        if log:
            log(f"[qa] atlas pass done in {time.time() - t0:.0f}s")

    kg_agg: list[dict[str, Any]] | None = None
    if dset & {"graph", "time", "licence", "provenance", "causal"}:

        def kg() -> list[CheckResult]:
            nonlocal kg_agg
            kg_agg = dbc.kg_aggregate(be, origin, be.today.isoformat())
            ctx["kg_statements"] = sum(int(r["n"]) for r in kg_agg)
            return []

        _guard(results, "kg.aggregate", "graph", kg)
        if log:
            log(f"[qa] kg aggregate done in {time.time() - t0:.0f}s")
    causal_agg: list[dict[str, Any]] | None = None
    if dset & {"licence", "provenance", "causal"}:

        def ca() -> list[CheckResult]:
            nonlocal causal_agg
            causal_agg = dbc.causal_aggregate(be, origin)
            ctx["causal_claims"] = sum(int(r["n"]) for r in causal_agg)
            return []

        _guard(results, "causal.aggregate", "causal", ca)
    effects_agg: list[dict[str, Any]] | None = None
    if dset & {"licence", "provenance"}:

        def ef() -> list[CheckResult]:
            nonlocal effects_agg
            effects_agg = dbc.effects_aggregate(be, origin)
            return []

        _guard(results, "effects.aggregate", "provenance", ef)

    if "time" in dset and kg_agg is not None:
        _guard(results, "time.kg_future_statement", "time", lambda: [dbc.kg_time_check(cfg, kg_agg or [])])
    if "graph" in dset and kg_agg is not None:
        _guard(results, "graph", "graph", lambda: dbc.graph_checks(be, cfg, origin, kg_agg or []))
        if log:
            log(f"[qa] graph done in {time.time() - t0:.0f}s")
    if dset & {"licence", "provenance"}:
        _guard(
            results,
            "licence_provenance",
            "licence",
            lambda: dbc.licence_provenance_checks(cfg, kg_agg, causal_agg, effects_agg, dset),
        )
    if "provenance" in dset:
        _guard(
            results,
            "provenance.objects",
            "provenance",
            lambda: dbc.object_checks(be, cfg, referenced, verify_all=verify_all, log=log),
        )
        if log:
            log(f"[qa] objects done in {time.time() - t0:.0f}s")
    if "causal" in dset and causal_agg is not None:
        pids: dict[str, int] = {}
        for r in kg_agg or []:
            pids[r["property_pid"]] = pids.get(r["property_pid"], 0) + int(r["n"])
        _guard(results, "causal", "causal", lambda: dbc.causal_checks(be, cfg, origin, causal_agg or [], pids))

    order = {d: i for i, d in enumerate(DOMAINS)}
    report.checks = sorted([r for r in results if r.domain in dset], key=lambda r: (order.get(r.domain, 99), r.id))
    ctx["seconds"] = round(time.time() - t0, 1)
    report.context = ctx
    return report
