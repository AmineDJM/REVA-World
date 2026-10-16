"""UNCTADstat bulk files -> trade, FDI and maritime connectivity atlas tables (V3, agent A-TRADE).

Source
------
UNCTAD Data Hub (UNCTADstat). **Licence: Creative Commons Attribution 3.0 IGO** -- "All materials
provided on this website are copyrighted under the Creative Commons Attribution 3.0 IGO license
... All data and metadata provided on the UNCTAD Data Hub may be copied freely, duplicated and
further distributed provided that United Nations Trade and Development Data Hub is cited as the
source" (unctadstat.unctad.org/EN/About.html and FAQ.html, checked 2026-10-15; "Access to data is
free and there is no registration required").

Access: one bulk file per report, ``https://unctadstat-api.unctad.org/bulkdownload/<report>/<file>``
(no key; the website's "bulk files" button). Each answer is a **7z archive with one CSV**
(``US_<name>.csv.7z``; LZMA2), read by :func:`atlas_trade_common.read_7z_single` with every CRC
checked. The UNCTADstat *API* (``/datamart-api``) needs a registered client and is not used.

Reports used (:data:`SETS`): services trade by EBOPS 2010 category (``US.TradeServCatTotal``),
goods/services/goods-and-services trade BPM6 (``US.GoodsAndServicesBpm6``), merchandise trade
totals since 1948 (``US.TradeMerchTotal``), FDI flows and stocks (``US.FdiFlowsStock``), the liner
shipping connectivity index (``US.LSCI``, quarterly) and the **bilateral** liner shipping
connectivity index (``US.LSBCI``, country pairs, Q1 of each year). Not used: creative goods
(``US.CreativeGoodsValue``, 335 MB compressed: beyond the disk budget; see the report).

Cells
-----
``indicator`` = measure + every dimension code verbatim: ``usd_millions|flow=02|category=SC``
(flow 02 = exports, 01 = imports, UNCTAD codes), ``usd_millions|flow=08|direction=1`` (FDI: flow
08 = flow, 09 = stock; direction 1 = inward, 2 = outward), ``index|partner=250`` (LSBCI). A cell
UNCTAD marks in its "Missing value" column ("Not publishable", "Not applicable", "No value reported
or collected") is **omitted** (counted in the coverage note), never a zero; the "Footnote" column
("Estimated", "Break", "Data refers to South Africa Customs Union"...) goes to ``flags.footnote``
(``flags.unctad_estimate = true`` when it says "Estimated").

Derived balances (trade surplus / deficit): ``usd_millions_balance|...`` = exports (flow 02) minus
imports (flow 01) of the **same report, economy, year and dimension**, computed exactly
(``Decimal``) only when both exist; ``flags.derived = "exports_minus_imports"`` and both input
indicators and file rows are cited. Services: for the top-level EBOPS categories only
(:data:`SERVICE_BALANCE_CATEGORIES`).

Geography: ``unctad`` scheme (``registry/atlas/geo_aliases/a_trade.yaml``: the zero-padded M49
codes observed in these files + 412 Kosovo -> XKX). UNCTAD groups (4-digit codes: 0000 World,
1400 Developing economies...), 158 Taiwan Province of China and historical areas stay unmapped and
are listed in each manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_trade_common as common
from reva.adapters.connectors.atlas_trade_common import canon, quarter_period, read_7z_single, strict_decimal
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, ValidationProblem

CONNECTOR_NAME = "atlas.unctad"
CONNECTOR_VERSION = "1.0.1"  # ATLASOPS: patch bump, code changed after 1.0.0 was registered in the dev DB (TWNMAP/agent edits)
PARSER_VERSION = "atlas_unctad/1"
SOURCE_KEY = "unctad.bulk"
SOURCE = "unctad"
HOST = "unctadstat-api.unctad.org"
GEO_SCHEME = "unctad"
MEDIA = ("application/octet-stream", "application/x-7z-compressed")
MAX_CSV_BYTES = 400_000_000

RIGHTS: dict[str, str] = {
    "licence": "CC BY 3.0 IGO",
    "licence_url": "https://creativecommons.org/licenses/by/3.0/igo/",
    "terms_url": "https://unctadstat.unctad.org/EN/About.html",
    "attribution": "United Nations Trade and Development (UNCTAD) Data Hub, https://unctadstat.unctad.org",
}

USD = "millions of US dollars (current prices)"
EXPORTS, IMPORTS = "02", "01"


@dataclass(frozen=True)
class Measure:
    column: str  # UNCTAD value column (its "<col> Footnote" / "<col> Missing value" companions)
    key: str  # indicator prefix
    unit: str


@dataclass(frozen=True)
class UnctadSet:
    dataset: str  # atlas dataset key
    report: str  # UNCTADstat report id
    period: str  # "Year" | "Quarter"
    dims: tuple[tuple[str, str], ...]  # (column, indicator key), folded verbatim in this order
    measures: tuple[Measure, ...]
    label: str
    max_bytes: int
    partner: str | None = None  # partner column (bilateral)
    balance: bool = False  # derive exports - imports over the "Flow" dimension
    balance_filter: tuple[str, frozenset[str]] | None = None  # (dim column, codes) eligible for a balance

    @property
    def file(self) -> str:
        return "US_" + self.report.split(".", 1)[1]

    @property
    def url(self) -> str:
        return f"https://{HOST}/bulkdownload/{self.report}/{self.file}"

    @property
    def csv_name(self) -> str:
        return self.file + ".csv"


#: top-level EBOPS 2010 categories (+ UNCTAD memo aggregates) that get a derived balance
SERVICE_BALANCE_CATEGORIES = frozenset(
    {"S", "SA", "SB", "SC", "SD", "SE", "SF", "SG", "SH", "SI", "SJ", "SK", "SL", "SN", "SOX", "SPX1", "SPX4"}
)

SETS: tuple[UnctadSet, ...] = (
    UnctadSet(
        "services_cat",
        "US.TradeServCatTotal",
        "Year",
        (("Flow", "flow"), ("Category", "category")),
        (Measure("Millions of US$ at current prices", "usd_millions", USD),),
        "Trade in services by EBOPS 2010 category, exports and imports, annual (BPM6)",
        30_000_000,
        balance=True,
        balance_filter=("Category", SERVICE_BALANCE_CATEGORIES),
    ),
    UnctadSet(
        "goods_services",
        "US.GoodsAndServicesBpm6",
        "Year",
        (("Flow", "flow"), ("Series", "series")),
        (Measure("Millions of US$ at current prices", "usd_millions", USD),),
        "Goods, services and goods-and-services trade (BPM6), exports and imports, annual",
        5_000_000,
        balance=True,
    ),
    UnctadSet(
        "merch_total",
        "US.TradeMerchTotal",
        "Year",
        (("Flow", "flow"),),
        (Measure("Millions of US$ at current prices", "usd_millions", USD),),
        "Merchandise trade, exports and imports, annual (since 1948)",
        5_000_000,
        balance=True,
    ),
    UnctadSet(
        "fdi",
        "US.FdiFlowsStock",
        "Year",
        (("Flow", "flow"), ("Direction", "direction")),
        (
            Measure("Millions of US$ at current prices", "usd_millions", USD),
            Measure("Percentage of gross Domestic Product", "pct_gdp", "% of GDP"),
        ),
        "Foreign direct investment: inward and outward flows and stocks, annual",
        5_000_000,
    ),
    UnctadSet(
        "lsci",
        "US.LSCI",
        "Quarter",
        (),
        (Measure("Index (average Q1 2023 = 100)", "index", "index (average Q1 2023 = 100)"),),
        "Liner shipping connectivity index, quarterly",
        5_000_000,
    ),
    UnctadSet(
        "lsbci",
        "US.LSBCI",
        "Quarter",
        (),
        (Measure("Index", "index", "index (UNCTAD LSBCI, 0-1)"),),
        "Liner shipping bilateral connectivity index (country pairs), Q1 of each year",
        10_000_000,
        partner="Partner",
    ),
)
SETS_BY_DATASET = {s.dataset: s for s in SETS}

#: fixture mode serves small rebuilt excerpts of these files (tests/fixtures/source/atlas.unctad)
FIXTURE_NOTE = "excerpts rebuilt from the 2026-10-15 bulk files; LSCI served whole"


# --------------------------------------------------------------------------- parsing
def _period(s: UnctadSet) -> tuple[pl.Expr, pl.Expr]:
    if s.period == "Year":
        y = pl.col("Year").cast(pl.Int32)
        return pl.date(y, 1, 1), pl.date(y + 1, 1, 1)
    return pl.col("_ps"), pl.col("_pe")


def parse_report(body: bytes, raw_sha: str, s: UnctadSet) -> tuple[pl.DataFrame, dict[str, Any]]:
    """One UNCTADstat bulk 7z -> atlas frame (not yet validated) and parse statistics."""
    name, content = read_7z_single(body, limit=MAX_CSV_BYTES)
    if name != s.csv_name:
        raise SourceResponseError(
            "UNCTAD bulk archive holds an unexpected file", file=name, expected=s.csv_name, retryable=False
        )
    df = common.read_csv_all_text(content, what=f"UNCTADstat {s.report}")
    need = [s.period, "Economy", *(c for c, _ in s.dims), *(m.column for m in s.measures)]
    if s.partner:
        need.append(s.partner)
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise SourceResponseError(f"UNCTADstat {s.report}: missing columns", missing=missing, retryable=False)
    df = df.with_row_index("_row", offset=2)  # CSV line number (header = line 1)
    if s.period == "Year":
        bad = df.filter(~pl.col("Year").str.contains(r"^[12][0-9]{3}$"))
        if bad.height:
            raise SourceResponseError(
                "UNCTADstat: unexpected Year values", examples=bad["Year"].head(3).to_list(), retryable=False
            )
    else:
        qs = df["Quarter"].unique().to_list()
        qmap = {q: quarter_period(q) for q in qs}
        df = df.with_columns(
            pl.col("Quarter").replace_strict({k: v[0] for k, v in qmap.items()}, return_dtype=pl.Date).alias("_ps"),
            pl.col("Quarter").replace_strict({k: v[1] for k, v in qmap.items()}, return_dtype=pl.Date).alias("_pe"),
        )
    pmap: dict[str, str | None] = {}
    if s.partner:
        from reva.atlas.tables import country_iso3

        pmap = {c: country_iso3(c, GEO_SCHEME) for c in df[s.partner].unique().to_list()}
    ps, pe = _period(s)
    frames: list[pl.DataFrame] = []
    stats: dict[str, Any] = {"file_rows": df.height, "omitted_missing": {}}
    for m in s.measures:
        fcol, mcol = f"{m.column} Footnote", f"{m.column} Missing value"
        sub = df.filter(pl.col(m.column).is_not_null() & (pl.col(m.column).str.strip_chars() != ""))
        if mcol in df.columns:
            gone = df.filter(pl.col(m.column).is_null() | (pl.col(m.column).str.strip_chars() == ""))
            stats["omitted_missing"][m.key] = dict(
                gone[mcol].fill_null("(empty, no reason given)").value_counts().iter_rows()
            )
            both = sub.filter(pl.col(mcol).is_not_null())
            if both.height:
                raise SourceResponseError(
                    "UNCTADstat cell has a value and a missing-value reason",
                    report=s.report,
                    rows=both.height,
                    retryable=False,
                )
        vals = sub[m.column].unique().to_list()
        fix = {v: strict_decimal(v) for v in vals}
        if any(v is None for v in fix.values()):
            raise ValidationProblem("UNCTADstat value column holds blanks after filtering", report=s.report)
        foot = pl.col(fcol) if fcol in df.columns else pl.lit(None, dtype=pl.String)
        quoted = foot.str.replace_all('"', "'")
        flags: pl.Expr = (
            pl.when(foot.is_null())
            .then(pl.lit("{}"))
            .when(foot.str.contains("(?i)estimated"))
            .then(pl.lit('{"footnote":"') + quoted + pl.lit('","unctad_estimate":true}'))
            .otherwise(pl.lit('{"footnote":"') + quoted + pl.lit('"}'))
        )
        parts: list[pl.Expr] = [pl.lit(m.key)]
        for col, key in s.dims:
            parts += [pl.lit(f"|{key}="), pl.col(col)]
        if s.partner:
            parts += [pl.lit("|partner="), pl.col(s.partner)]
            mapped = pl.col(s.partner).replace_strict(pmap, default=None, return_dtype=pl.String)
            pflag = (
                pl.when(mapped.is_null())
                .then(pl.lit('"partner_unmapped":true'))
                .otherwise(pl.lit('"partner_iso3":"') + mapped + pl.lit('"'))
            )
            flags = pl.concat_str([pl.lit("{"), pflag, pl.lit("}")])
        frames.append(
            sub.select(
                pl.lit(SOURCE).alias("source"),
                pl.lit(s.dataset).alias("dataset"),
                pl.concat_str(parts).alias("indicator"),
                pl.lit(None, dtype=pl.String).alias("concept_id"),
                pl.lit(None, dtype=pl.String).alias("iso3"),
                pl.col("Economy").alias("provider_geo"),
                pl.lit(None, dtype=pl.String).alias("entity_qid"),
                ps.alias("period_start"),
                pe.alias("period_end"),
                pl.col(m.column).replace_strict(fix, return_dtype=pl.String).alias("value"),
                pl.lit(None, dtype=pl.String).alias("value_text"),
                pl.lit(m.unit).alias("unit"),
                flags.alias("flags"),
                pl.lit(raw_sha).alias("raw_sha256"),
                pl.concat_str(
                    [
                        pl.lit(f'{{"column":"{m.column}","file":"{s.csv_name}","row":'),
                        pl.col("_row").cast(pl.String),
                        pl.lit("}"),
                    ]
                ).alias("selector"),
            )
        )
    out = pl.concat(frames)
    if s.balance:
        bal = derive_balances(out, s)
        stats["derived_balances"] = bal.height
        out = pl.concat([out, bal])
    stats["rows"] = out.height
    return out, stats


def _flow_of(indicator: str) -> tuple[str, str, str]:
    """``usd_millions|flow=02|category=S`` -> (``usd_millions``, ``02``, ``|category=S``)."""
    head, _, rest = indicator.partition("|flow=")
    code, _, tail = rest.partition("|")
    return head, code, ("|" + tail) if tail else ""


def derive_balances(frame: pl.DataFrame, s: UnctadSet) -> pl.DataFrame:
    """Exports (02) minus imports (01): same economy, period, measure and other dimensions."""
    usd = frame.filter(pl.col("indicator").str.contains(r"^usd_millions\|flow=0[12](\||$)"))
    if s.balance_filter is not None:
        col, codes = s.balance_filter
        key = dict(s.dims)[col]
        pat = "|".join(sorted(codes))
        usd = usd.filter(pl.col("indicator").str.contains(rf"\|{key}=({pat})(\||$)"))
    parsed = [_flow_of(i) for i in usd["indicator"].to_list()]
    usd = usd.with_columns(
        pl.Series("_flow", [p[1] for p in parsed], dtype=pl.String),
        pl.Series("_rest", [p[0] + "_balance" + p[2] for p in parsed], dtype=pl.String),
    )
    keys = ["provider_geo", "period_start", "period_end", "_rest"]
    x = usd.filter(pl.col("_flow") == EXPORTS)
    m = usd.filter(pl.col("_flow") == IMPORTS)
    j = x.join(m, on=keys, how="inner", suffix="_m")
    if j.is_empty():
        return frame.clear()
    vals, flags, sels = [], [], []
    for r in j.select(
        "value", "value_m", "indicator", "indicator_m", "flags", "flags_m", "selector", "selector_m"
    ).iter_rows():
        vx, vm, ix, im, fx, fm, sx, sm = r
        d = Decimal(vx) - Decimal(vm)
        vals.append(strict_decimal(format(d, "f")) or "0")
        f: dict[str, Any] = {"derived": "exports_minus_imports", "inputs": [ix, im]}
        fxd, fmd = _json(fx), _json(fm)
        if fxd or fmd:
            f["input_flags"] = {"exports": fxd, "imports": fmd}
        if fxd.get("unctad_estimate") or fmd.get("unctad_estimate"):
            f["unctad_estimate"] = True
        flags.append(canon(f))
        sels.append(canon({"derived": "exports - imports", "inputs": [_json(sx), _json(sm)]}))
    return j.select(
        pl.col("source"),
        pl.col("dataset"),
        pl.col("_rest").alias("indicator"),
        pl.col("concept_id"),
        pl.col("iso3"),
        pl.col("provider_geo"),
        pl.col("entity_qid"),
        pl.col("period_start"),
        pl.col("period_end"),
        pl.Series("value", vals, dtype=pl.String),
        pl.col("value_text"),
        pl.col("unit"),
        pl.Series("flags", flags, dtype=pl.String),
        pl.col("raw_sha256"),
        pl.Series("selector", sels, dtype=pl.String),
    )


def _json(text: str) -> dict[str, Any]:
    import json

    obj = json.loads(text)
    return obj if isinstance(obj, dict) else {}


# --------------------------------------------------------------------------- ATLASCORE entry point
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="UNCTAD (United Nations Trade and Development) Data Hub - UNCTADstat bulk files",
        licence=f"{RIGHTS['licence']} ({RIGHTS['terms_url']}); attribution: {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=tuple(s.dataset for s in SETS),
        dataset_ids=tuple(s.report for s in SETS),
        cadence_seconds=30 * 86400,
        planned_requests=len(SETS),
        max_response_bytes=max(s.max_bytes for s in SETS),
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(_module()),
        field_mappings={
            "tables": {s.report: f"{SOURCE}.{s.dataset}" for s in SETS},
            "measures": {s.dataset: [m.column for m in s.measures] for s in SETS},
            "geo_scheme": GEO_SCHEME,
        },
        notes="One 7z bulk file per UNCTADstat report (no key); derived balances = exports - imports.",
    )


def _module() -> Any:
    import sys

    return sys.modules[__name__]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    fetched: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    for s in SETS:
        if not run.wants(s.dataset):
            continue
        try:
            raw = await run.get(s.url, key=s.report, media_types=MEDIA, max_bytes=s.max_bytes)
            if not raw.body:
                raise SourceResponseError(f"{s.report}: empty response body", retryable=True)
            sha = run.archive(raw)
            frame, stats = parse_report(raw.body, sha, s)
        except BudgetExhausted:
            raise
        except Exception as e:
            errors.append({"item": s.report, "error": type(e).__name__, "message": str(e)[:300]})
            continue
        fetched.append({"item": s.report, "bytes": len(raw.body), "sha256": sha, **stats})
        omitted = "; ".join(f"{k}: {v}" for k, v in stats["omitted_missing"].items())
        note = (
            f"UNCTADstat {s.report} bulk file ({s.file}.csv.7z): {s.label}. Cells marked missing by UNCTAD "
            f"omitted ({omitted or 'none'}). usd_millions_balance rows are derived (exports 02 - imports 01, same "
            "file, economy, year and dimension)."
        )
        if run.origin == "test_fixture":
            note += f" Fixture: {FIXTURE_NOTE}."
        try:
            out = run.register(
                dataset=s.dataset,
                frame=frame,
                raw_sha256s=[sha],
                coverage_note=note,
                geo_scheme=GEO_SCHEME,
                rights=dict(RIGHTS),
            )
        except Exception as e:
            errors.append(
                {
                    "stage": "register",
                    "table": f"{SOURCE}.{s.dataset}",
                    "error": type(e).__name__,
                    "message": str(e)[:500],
                }
            )
            continue
        cov = out.get("coverage") or {}
        tables.append(
            {
                "table": f"{SOURCE}.{s.dataset}",
                "dataset_key": out.get("dataset_key"),
                "created": out.get("created"),
                "rows": out.get("row_count"),
                "countries": cov.get("countries"),
                "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []],
                "artifact_sha256": out.get("artifact_sha256"),
            }
        )
    return {"fetched": fetched, "errors": errors, "tables": tables, "ok": not errors}


__all__ = [
    "CONNECTOR_NAME",
    "SETS",
    "SETS_BY_DATASET",
    "UnctadSet",
    "atlas_ingest",
    "atlas_source",
    "derive_balances",
    "parse_report",
]
