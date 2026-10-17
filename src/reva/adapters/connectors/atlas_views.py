"""VIEWS (Violence & Impacts Early-Warning System, Uppsala University / PRIO) conflict forecasts -> atlas (V6 PRED-CONFLICT).

VIEWS publishes monthly *forecasts* of fatalities in state-based armed conflict (UCDP definition) per country-month
(``cm``) and PRIO-GRID-month, 1-36 months ahead, through a public REST API (``https://api.viewsforecasting.org``,
no key). REVA uses them as an **attributed benchmark forecaster** for its conflict-risk questions
(``reva.evaluation.conflict``): they are forecasts, never observations.

Requests (all through ``run.get``: budget-metered live, captured fixtures offline):

* ``https://raw.githubusercontent.com/wiki/prio-data/views_api/Available-datasets.md`` -- the VIEWS API wiki table
  of datasets with *Last input data*, *Forecasting window* and *Release date* per run (verbatim strings kept on
  every cell: the release date is what makes a VIEWS forecast "as of" a date);
* ``https://api.viewsforecasting.org/`` -- the list of runs (each requested run must be listed);
* per run: ``/{run}/codebook`` (variable definitions, quoted verbatim in the coverage note; archived) and, per
  variable, ``/{run}/cm/sb/{variable}?pagesize=10000`` following ``next_page`` (same host and path only).

Variables (by model generation; checked against each run's codebook bytes):

* ``fatalities002`` / ``fatalities003``: ``main_mean`` ("Predicted number of fatalities in state-based armed
  conflict"; the codebook says it originates from ``pred_ln_ged_sb_dep``, i.e. a point prediction made on the
  log scale and back-transformed, not an expected value) and ``main_dich`` ("Predicted probability of at least 25
  battle-related deaths (BRDs) in state-based armed conflict per country-month");
* ``fatalities001``: ``sc_cm_sb_main`` (fatalities "expressed in natural logged form plus 1 (ln(fatalities+1))")
  and ``sc_cm_sb_dich_main`` (probability). Only the requested variable becomes cells; any other column a page
  carries is counted in the run report, never stored.

Cells (table ``views.cm_sb``): ``indicator`` = ``<run>|<variable>`` verbatim; ``provider_geo`` = ``gw:<gwcode>``
(Gleditsch-Ward code as published by VIEWS -- the same key UCDP tables use); ``iso3`` = VIEWS ``isoab`` when it is
a known ISO3 code (never derived from names); period = the forecast month; ``value`` = the JSON number text
verbatim. ``flags``: ``estimate_kind = "projection"``, ``projection_upstream`` (model, run, release date, last
input data, forecasting window -- wiki strings verbatim), VIEWS ``country_id`` / ``month_id`` / ``name``, and
``step`` = months after the last input month.

Licence: **not stated** by the provider on the pages fetched (data page, API README, API wiki, checked
2026-10-17); VIEWS asks to be cited (Hegre et al. 2021, 2022). Flagged ``open_licence: false`` and
``licence_review`` until the owner confirms the terms (the V6 brief describes the forecasts as CC BY; that was not
verifiable from the provider's pages).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import polars as pl

from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.views"
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas_views/1"
SOURCE_KEY = "views.api"
SOURCE = "views"
DATASET = "cm_sb"
API_HOST = "api.viewsforecasting.org"
WIKI_HOST = "raw.githubusercontent.com"
API = f"https://{API_HOST}"
WIKI_URL = f"https://{WIKI_HOST}/wiki/prio-data/views_api/Available-datasets.md"
PAGESIZE = 10_000
MAX_BYTES = 30_000_000
MAX_PAGES = 20
LICENCE = (
    "Not stated by VIEWS on the pages checked (2026-10-17); citation requested (Hegre et al. 2021, JPR 58(3); "
    "Hegre et al. 2022, Forecasting fatalities); licence_review"
)
RUN_RE = re.compile(r"^fatalities(00[1-9])_(20[2-9][0-9])_(0[0-9]|1[0-2])_t0[1-9]$")
#: model generation -> (fatalities variable, probability variable)
VARIABLES: dict[str, tuple[str, str]] = {
    "001": ("sc_cm_sb_main", "sc_cm_sb_dich_main"),
    "002": ("main_mean", "main_dich"),
    "003": ("main_mean", "main_dich"),
}
UNITS = {
    "sc_cm_sb_main": "ln(fatalities+1)",
    "main_mean": "fatalities",
    "main_mean_ln": "ln(fatalities+1)",
    "sc_cm_sb_dich_main": "probability",
    "main_dich": "probability",
}
#: Runs fetched by default: the December runs (windows = the next calendar years, for the country-year benchmark),
#: quarterly 2025 runs (horizon profile) and the latest run at writing time (sealed forecasts). The 2021-12 run is
#: left out: VIEWS released it on 2023-04-27, after the year it forecasts.
DEFAULT_RUNS: tuple[str, ...] = (
    "fatalities001_2022_12_t01",
    "fatalities002_2023_12_t01",
    "fatalities002_2024_12_t01",
    "fatalities002_2025_03_t01",
    "fatalities002_2025_06_t01",
    "fatalities002_2025_09_t01",
    "fatalities003_2025_12_t01",
    "fatalities003_2026_08_t01",
)
DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
ISO3_RE = re.compile(r"^[A-Z]{3}$")


class ViewsParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="VIEWS (Violence & Impacts Early-Warning System), Uppsala University and PRIO -- public forecast API",
        licence=LICENCE,
        allowed_hosts=(API_HOST, WIKI_HOST),
        datasets=(DATASET,),
        dataset_ids=DEFAULT_RUNS,
        cadence_seconds=30 * 86_400,
        planned_requests=2 + 3 * len(DEFAULT_RUNS),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{SOURCE}.{DATASET}"], "geo_scheme": "gw"},
        notes="VIEWS API country-month state-based forecasts per run (forecasts only, estimate_kind=projection) + "
        "wiki release dates + codebooks",
    )


def rights() -> dict[str, Any]:
    return {
        "licence": "not stated by the provider (licence_review)",
        "open_licence": False,
        "licence_review": True,
        "licence_checked": "2026-10-17",
        "licence_evidence": "viewsforecasting.org/data (Data Access page), github.com/prio-data/views_api README and "
        "wiki: no licence statement found; prio-data/viewsforecasting README 'How to cite' asks for Hegre et al. "
        "(2021) and Hegre et al. (2022)",
        "attribution": "VIEWS -- Violence & Impacts Early-Warning System (Uppsala University, Department of Peace "
        "and Conflict Research; PRIO). Hegre, H. et al. (2021) 'ViEWS2020: Revising and evaluating the ViEWS "
        "political Violence Early-Warning System', Journal of Peace Research 58(3), 599-611; Hegre, H. et al. "
        "(2022) 'Forecasting fatalities', Uppsala University, urn:nbn:se:uu:diva-476476.",
        "provider": "VIEWS (Uppsala University / PRIO)",
        "source_key": SOURCE_KEY,
        "note": "forecasts attributed to VIEWS; REVA does not relicense them",
    }


# --------------------------------------------------------------------------- parsing (pure)


@dataclass(frozen=True)
class RunInfo:
    run: str
    model_version: str
    last_input: str | None
    window: str | None
    release_date: str | None


def parse_run(run: str) -> tuple[str, int, int]:
    m = RUN_RE.match(run)
    if not m:
        raise ViewsParseError(f"not a VIEWS fatalities run id: {run!r}")
    return m.group(1), int(m.group(2)), int(m.group(3))


def parse_wiki(body: bytes) -> dict[str, RunInfo]:
    """Rows of the wiki's dataset tables: ``| run | model | version | last input | window | release | codebook |``.
    Strings are kept verbatim (stripped); a run listed twice keeps its first row (the newest table comes first)."""
    try:
        text_ = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ViewsParseError(f"wiki page is not UTF-8 ({e})") from e
    out: dict[str, RunInfo] = {}
    for line in text_.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or not RUN_RE.match(cells[0]):
            continue
        run = cells[0]
        if run in out:
            continue
        out[run] = RunInfo(run, cells[2], cells[3] or None, cells[4] or None, cells[5] or None)
    if not out:
        raise ViewsParseError("wiki page lists no fatalities run")
    return out


def parse_run_list(body: bytes) -> set[str]:
    try:
        runs = json.loads(body)["runs"]
    except (ValueError, KeyError, TypeError) as e:
        raise ViewsParseError(f"run list: not the expected JSON ({e})") from e
    if not isinstance(runs, list) or not runs:
        raise ViewsParseError("run list: empty")
    return {str(r) for r in runs}


def month_id(year: int, month: int) -> int:
    """VIEWS ``month_id``: 1 = January 1980."""
    return (year - 1980) * 12 + month


@dataclass
class PageResult:
    rows: list[dict[str, Any]]
    next_page: str
    row_count: int


def parse_page(body: bytes, variable: str) -> PageResult:
    try:
        d = json.loads(body, parse_float=str, parse_int=str)
    except ValueError as e:
        raise ViewsParseError(f"{variable}: page is not JSON ({e})") from e
    if not isinstance(d, dict) or not isinstance(d.get("data"), list):
        raise ViewsParseError(f"{variable}: page without a data array ({str(d)[:200]})")
    return PageResult(d["data"], str(d.get("next_page") or ""), int(d.get("row_count") or 0))


def check_next(url: str, nxt: str) -> str:
    """A ``next_page`` link must stay on the API host and the same path."""
    a, b = urlsplit(url), urlsplit(nxt)
    if b.scheme != "https" or b.netloc != API_HOST or b.path != a.path:
        raise ViewsParseError(f"next_page leaves the requested resource: {nxt[:200]}")
    return nxt


@dataclass
class ViewsReport:
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    cells: int = 0
    other_columns: dict[str, int] = field(default_factory=dict)
    null_values: int = 0
    no_iso3: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["no_iso3"] = sorted(self.no_iso3)
        return d


def _flags(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rows_to_cells(
    rows: list[dict[str, Any]],
    *,
    run: str,
    variable: str,
    info: RunInfo | None,
    raw_sha: str,
    page_url: str,
    rep: ViewsReport,
) -> dict[str, list[Any]]:
    gen, ly, lm = parse_run(run)
    last_mid = month_id(ly, lm)
    up = {
        "model": f"VIEWS fatalities{gen}",
        "run": run,
        "release_date": info.release_date if info else "not_stated",
        "last_input_data": info.last_input if info else "not_stated",
        "forecasting_window": info.window if info else "not_stated",
    }
    c: dict[str, list[Any]] = {k: [] for k in ("ind", "geo", "iso3", "y", "m", "value", "flags", "unit", "sel")}
    for i, r in enumerate(rows):
        for k in r:
            if k not in ("country_id", "month_id", "name", "gwcode", "isoab", "year", "month", variable):
                rep.other_columns[k] = rep.other_columns.get(k, 0) + 1
        v = r.get(variable)
        if v is None:
            rep.null_values += 1
            continue
        if not DECIMAL_RE.match(str(v)):
            raise ViewsParseError(f"{run} {variable}: value {v!r} is not a number")
        try:
            y, mo, mid, gw = int(r["year"]), int(r["month"]), int(r["month_id"]), int(r["gwcode"])
        except (KeyError, TypeError, ValueError) as e:
            raise ViewsParseError(f"{run} {variable}: row {i} lacks year/month/month_id/gwcode ({e})") from e
        if mid != month_id(y, mo):
            raise ViewsParseError(f"{run}: month_id {mid} does not match {y}-{mo:02d}")
        if mid <= last_mid:
            raise ViewsParseError(f"{run}: forecast month {y}-{mo:02d} is not after the last input month")
        iso = str(r.get("isoab") or "")
        iso3 = country_iso3(iso, "iso3") if ISO3_RE.match(iso) else None
        if iso3 is None:
            rep.no_iso3.add(f"gw:{gw} {iso or '-'}")
        c["ind"].append(f"{run}|{variable}")
        c["geo"].append(f"gw:{gw}")
        c["iso3"].append(iso3)
        c["y"].append(y)
        c["m"].append(mo)
        c["value"].append(str(v))
        c["unit"].append(UNITS.get(variable, "not stated"))
        c["flags"].append(
            _flags(
                {
                    "estimate_kind": "projection",
                    "projection_upstream": up,
                    "views_country_id": r.get("country_id"),
                    "views_name": r.get("name"),
                    "views_isoab": iso or None,
                    "month_id": mid,
                    "step": mid - last_mid,
                }
            )
        )
        c["sel"].append(_flags({"url": page_url, "row": i, "column": variable}))
    rep.cells += len(c["ind"])
    _ = raw_sha
    return c


def build_frame(chunks: list[tuple[dict[str, list[Any]], str]]) -> pl.DataFrame:
    """Atlas frame from ``(cells, raw_sha256)`` chunks (one per fetched page)."""
    frames = []
    for c, sha in chunks:
        if not c["ind"]:
            continue
        df = pl.DataFrame(
            c,
            schema={
                "ind": pl.String,
                "geo": pl.String,
                "iso3": pl.String,
                "y": pl.Int32,
                "m": pl.Int8,
                "value": pl.String,
                "flags": pl.String,
                "unit": pl.String,
                "sel": pl.String,
            },
        )
        frames.append(
            df.select(
                pl.lit(SOURCE).alias("source"),
                pl.lit(DATASET).alias("dataset"),
                pl.col("ind").alias("indicator"),
                pl.lit(None, dtype=pl.String).alias("concept_id"),
                "iso3",
                pl.col("geo").alias("provider_geo"),
                pl.lit(None, dtype=pl.String).alias("entity_qid"),
                pl.date(pl.col("y"), pl.col("m"), 1).alias("period_start"),
                pl.date(
                    pl.when(pl.col("m") == 12).then(pl.col("y") + 1).otherwise(pl.col("y")),
                    pl.when(pl.col("m") == 12).then(1).otherwise(pl.col("m") + 1),
                    1,
                ).alias("period_end"),
                "value",
                pl.lit(None, dtype=pl.String).alias("value_text"),
                "unit",
                "flags",
                pl.lit(sha).alias("raw_sha256"),
                pl.col("sel").alias("selector"),
            ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
        )
    if not frames:
        raise ViewsParseError("no forecast cell")
    out = pl.concat(frames)
    dup = out.group_by("indicator", "provider_geo", "period_start").len().filter(pl.col("len") > 1)
    if dup.height:
        raise ViewsParseError(f"{dup.height} duplicated (run, variable, country, month) cells")
    return out


def codebook_quotes(body: bytes, variables: tuple[str, ...]) -> dict[str, str]:
    """Verbatim name/description of each variable from a run's codebook (both codebook layouts)."""
    try:
        d = json.loads(body)
    except ValueError as e:
        raise ViewsParseError(f"codebook is not JSON ({e})") from e
    out: dict[str, str] = {}
    for v in variables:
        if isinstance(d, dict) and isinstance(d.get(v), dict):  # 002/003 layout
            ent = d[v]
            out[v] = f"{ent.get('name', '')}: {ent.get('description', '')}".strip()
            continue
        found = None
        stack: list[Any] = [d]
        while stack and found is None:  # 001 layout: nested sections with "Variable ID"
            x = stack.pop()
            if isinstance(x, dict):
                if x.get("Variable ID") == v:
                    found = f"{x.get('Outcome', '')} {x.get('Description', '')}".strip()
                else:
                    stack.extend(x.values())
        if found is None:
            raise ViewsParseError(f"codebook does not describe {v}")
        out[v] = found
    return out


def coverage_note(rep: ViewsReport, quotes: dict[str, dict[str, str]]) -> str:
    runs = "; ".join(
        f"{r} (released {m.get('release_date')}, last input {m.get('last_input_data')}, {m.get('cells')} cells)"
        for r, m in sorted(rep.runs.items())
    )
    q = " | ".join(f"{v}: {t[:300]}" for qs in list(quotes.values())[-1:] for v, t in qs.items())
    return (
        f"VIEWS country-month state-based (sb) FORECASTS from {API} (estimate_kind=projection, never observations); "
        f"runs: {runs}. Codebook (latest run, verbatim): {q}. provider_geo = gw:<VIEWS gwcode>. Licence not stated "
        "by the provider (licence_review); cite Hegre et al. 2021/2022."
    )[:4000]


# --------------------------------------------------------------------------- ingest


def _runs_option(run: Any) -> tuple[str, ...]:
    opt = run.options.get("runs")
    if not opt:
        return DEFAULT_RUNS
    runs = tuple(x.strip() for x in (opt if isinstance(opt, list) else str(opt).split(",")) if x.strip())
    for r in runs:
        parse_run(r)
    return runs


def _iso_filter(run: Any) -> str:
    """Optional ``iso`` run option (comma list): restricts the API query (used by the captured test fixture)."""
    opt = run.options.get("iso")
    if not opt:
        return ""
    isos = [x.strip().upper() for x in (opt if isinstance(opt, list) else str(opt).split(",")) if x.strip()]
    if any(not ISO3_RE.match(x) for x in isos):
        raise ViewsParseError("iso option must be ISO3 codes")
    return "".join(f"&iso={x}" for x in isos)


async def atlas_ingest(run: Any) -> dict[str, Any]:
    run.wants(DATASET)
    runs = _runs_option(run)
    iso_q = _iso_filter(run)
    wiki = await run.get(WIKI_URL, key="wiki:available-datasets", media_types=("text/plain",))
    wiki_sha = run.archive(wiki)
    infos = parse_wiki(wiki.body)
    lst = await run.get(API + "/", key="api:runs")
    lst_sha = run.archive(lst)
    listed = parse_run_list(lst.body)
    missing = [r for r in runs if r not in listed]
    if missing:
        raise ViewsParseError(f"runs not listed by the API: {missing}")
    rep = ViewsReport()
    chunks: list[tuple[dict[str, list[Any]], str]] = []
    raw_shas = [wiki_sha, lst_sha]
    quotes: dict[str, dict[str, str]] = {}
    for r in runs:
        gen, _, _ = parse_run(r)
        if gen not in VARIABLES:
            raise ViewsParseError(f"{r}: unknown model generation {gen}")
        variables = VARIABLES[gen]
        cb = await run.get(f"{API}/{r}/codebook", key=f"codebook:{r}")
        raw_shas.append(run.archive(cb))
        quotes[r] = codebook_quotes(cb.body, variables)
        info = infos.get(r)
        n_cells = 0
        for v in variables:
            url = f"{API}/{r}/cm/sb/{v}?pagesize={PAGESIZE}{iso_q}"
            seen = 0
            for page in range(1, MAX_PAGES + 1):
                raw = await run.get(url, key=f"data:{r}:{v}:{page}", params={"run": r, "variable": v, "page": page})
                sha = run.archive(raw)
                raw_shas.append(sha)
                res = parse_page(raw.body, v)
                cells = rows_to_cells(res.rows, run=r, variable=v, info=info, raw_sha=sha, page_url=url, rep=rep)
                chunks.append((cells, sha))
                n_cells += len(cells["ind"])
                seen += len(res.rows)
                if not res.next_page:
                    break
                url = check_next(url, res.next_page)
            else:
                raise ViewsParseError(f"{r} {v}: more than {MAX_PAGES} pages")
            if res.row_count and seen != res.row_count:
                raise ViewsParseError(f"{r} {v}: {seen} rows read, API row_count {res.row_count}")
        rep.runs[r] = {
            "release_date": info.release_date if info else "not_stated",
            "last_input_data": info.last_input if info else "not_stated",
            "window": info.window if info else "not_stated",
            "variables": list(variables),
            "cells": n_cells,
        }
    frame = build_frame(chunks)
    reg = run.register(
        dataset=DATASET,
        frame=frame,
        raw_sha256s=sorted(set(raw_shas)),
        coverage_note=coverage_note(rep, quotes),
        rights=rights(),
    )
    return {
        "tables": {
            DATASET: {
                "dataset_key": reg["dataset_key"],
                "rows": reg["row_count"],
                "countries": reg["coverage"]["countries"],
                "created": reg["created"],
            }
        },
        "report": rep.as_dict(),
        "codebook_quotes": quotes,
    }


__all__ = [
    "CONNECTOR_NAME",
    "DATASET",
    "DEFAULT_RUNS",
    "VARIABLES",
    "ViewsParseError",
    "atlas_ingest",
    "atlas_source",
    "build_frame",
    "codebook_quotes",
    "month_id",
    "parse_page",
    "parse_run",
    "parse_run_list",
    "parse_wiki",
    "rows_to_cells",
]
