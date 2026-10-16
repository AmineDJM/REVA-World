"""OECD-WTO Balanced Trade in Services (BaTIS) -> bilateral services atlas table (V3, agent A-TRADE).

Source
------
BaTIS (OECD.SDD.TPS dataflow ``DSD_BATIS@DF_BATIS`` 1.0, BPM6 edition): "a complete, consistent,
and balanced matrix of international trade in services statistics ... annual bilateral data
covering 202 reporters and partners, broken down by total services and 26 EBOPS 2010 (BPM6)
categories from 2005 to 2024" (dataflow description, OECD SDMX, 2026-10-15). Three measures per
flow (``ADJUSTMENT``): ``N`` reported, ``F`` adjusted and/or imputed, ``B`` **balanced** ("the
reconciled bilateral trade flow, where exports equal mirror imports"). This connector keeps only
**total services (``SERVICE=S``), exports (``TRADE_FLOW=X``), balanced (``B``)**: one value per
ordered pair, so exports of A to B are the imports of B from A by construction. Balanced values
are **OECD-WTO estimates** (``OBS_STATUS`` ``I`` "value imputed by a receiving agency" on nearly
every cell); every cell says so (``flags.estimate = "oecd_wto_balanced"``).

Licence: OECD content published from 2024-07-01 defaults to **CC BY 4.0** (OECD "open by default"
policy); no dataset-level statement was found in the dataflow metadata and oecd.org pages answer
403 inside the container, and BaTIS is a joint OECD-WTO product: recorded as
``licence_status = oecd_default_cc_by_4.0_dataset_statement_not_verified`` (pending maintainer
review, like ``oecd.sdmx`` for A-ILO).

Access: OECD SDMX REST (``sdmx.oecd.org``, no key), SDMX-CSV (``format=csvfile``), one request per
reference year (~3.6 MB) + the dataflow definition (edition label, archived as evidence). Unit:
``OBS_VALUE`` in **millions of US dollars** (``UNIT_MULT = 6``), kept verbatim.

Atlas table ``oecd.batis_services``
-----------------------------------
* ``X|partner=<code>``: balanced exports of services of REF_AREA to COUNTERPART_AREA (partner
  verbatim; ``flags.partner_iso3`` / ``flags.partner_unmapped``). Partners include the aggregates
  ``W`` World, ``WXD`` rest of the world, ``WXOECD``, ``WXEU27_2020``, ``OECD``, ``EU27_2020``.
* ``BAL|partner=<code>``: **derived** X(A->B) - X(B->A), both balanced cells of the same year file,
  exact decimal arithmetic, only between two mapped-or-unmapped *economies* (aggregates excluded),
  both inputs cited.

Geography: ``batis`` scheme = ISO3 + ``XKV`` -> XKX (BaTIS CL_AREA "Kosovo"); TWN and aggregates stay
unmapped.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_trade_common as common
from reva.adapters.connectors.atlas_trade_common import canon, strict_decimal
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.oecd.batis"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_batis/1"
SOURCE_KEY = "oecd.sdmx"
SOURCE = "oecd"
DATASET = "batis_services"
HOST = "sdmx.oecd.org"
BASE = f"https://{HOST}/public/rest"
FLOW = "OECD.SDD.TPS,DSD_BATIS@DF_BATIS,1.0"
KEY = "X.S.S.A.USD_EXC.B"  # TRADE_FLOW.PRODUCT_TYPE.SERVICE.FREQ.UNIT_MEASURE.ADJUSTMENT
GEO_SCHEME = "batis"
UNIT = "millions of US dollars (current)"
CSV_MEDIA = ("application/vnd.sdmx.data+csv", "text/csv", "application/octet-stream")
XML_MEDIA = ("application/xml", "text/xml", "application/vnd.sdmx.structure+xml")
YEARS: tuple[int, ...] = tuple(range(2005, 2025))
FIXTURE_YEARS: tuple[int, ...] = (2023, 2024)
FIXTURE_REF_AREAS: tuple[str, ...] = ("FRA", "USA", "DZA", "CHN", "XKV", "W")
AGGREGATES = frozenset({"W", "WXD", "WXOECD", "WXEU27_2020", "OECD", "EU27_2020"})
OBS_STATUS = {
    "A": "Normal value",
    "E": "Estimated value",
    "I": "Value imputed by a receiving agency",
    "P": "Provisional value",
}
METHODOLOGY = {
    "AD": "Adjustment",
    "DE": "Derivation",
    "AG": "Aggregation",
    "IE": "Interpolation / extrapolation",
    "ME": "Model-based estimation",
}

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0 (OECD default for content from 2024-07-01; dataset statement not verified)",
    "licence_status": "oecd_default_cc_by_4.0_dataset_statement_not_verified",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "terms_url": "https://www.oecd.org/en/about/oecd-open-by-default-policy.html",
    "attribution": "OECD-WTO Balanced Trade in Services (BaTIS), BPM6 edition, OECD Data Explorer",
}

COLUMNS = (
    "REF_AREA",
    "COUNTERPART_AREA",
    "TRADE_FLOW",
    "PRODUCT_TYPE",
    "SERVICE",
    "FREQ",
    "UNIT_MEASURE",
    "ADJUSTMENT",
    "TIME_PERIOD",
    "OBS_VALUE",
    "OBS_STATUS",
    "METHODOLOGY_TYPE",
    "UNIT_MULT",
)


def data_url(year: int, ref_areas: tuple[str, ...] | None = None) -> str:
    areas = "+".join(ref_areas) if ref_areas else ""
    return f"{BASE}/data/{FLOW}/{areas}..{KEY}?startPeriod={year}&endPeriod={year}&format=csvfile"


DATAFLOW_URL = f"{BASE}/dataflow/OECD.SDD.TPS/DSD_BATIS@DF_BATIS/1.0"

_EDITION = re.compile(r"OECD-WTO_BATIS_BPM6_([A-Za-z]+[0-9]{4})_bulk")


def parse_edition(body: bytes) -> str:
    """Edition label from the dataflow's bulk-download annotation (e.g. ``December2025``)."""
    m = _EDITION.search(body.decode("utf-8", "replace"))
    if not m:
        raise SourceResponseError("BaTIS dataflow: no edition label in the bulk-download annotation", retryable=False)
    return m.group(1)


def parse_year(body: bytes, raw_sha: str, year: int, edition: str) -> pl.DataFrame:
    """One BaTIS SDMX-CSV year file -> atlas frame (X cells + derived pair balances)."""
    df = common.read_csv_all_text(body, what=f"BaTIS {year}")
    miss = [c for c in COLUMNS if c not in df.columns]
    if miss:
        raise SourceResponseError("BaTIS CSV: missing columns", missing=miss, retryable=False)
    df = df.with_row_index("_row", offset=2)
    fixed = {
        "TRADE_FLOW": "X",
        "PRODUCT_TYPE": "S",
        "SERVICE": "S",
        "FREQ": "A",
        "UNIT_MEASURE": "USD_EXC",
        "ADJUSTMENT": "B",
        "UNIT_MULT": "6",
        "TIME_PERIOD": str(year),
    }
    for col, want in fixed.items():
        got = df[col].unique().to_list()
        if got != [want]:
            raise SourceResponseError(
                "BaTIS CSV: unexpected dimension values", column=col, values=got[:5], retryable=False
            )
    df = df.filter(pl.col("OBS_VALUE").is_not_null())
    vals = {v: strict_decimal(v) for v in df["OBS_VALUE"].unique().to_list()}
    from reva.atlas.tables import country_iso3

    pmap = {c: country_iso3(c, GEO_SCHEME) for c in df["COUNTERPART_AREA"].unique().to_list()}
    flags = []
    for cp, st, mt in df.select("COUNTERPART_AREA", "OBS_STATUS", "METHODOLOGY_TYPE").iter_rows():
        f: dict[str, Any] = {"adjustment": "B (balanced)", "batis_edition": edition, "estimate": "oecd_wto_balanced"}
        if st:
            f["obs_status"] = st
            f["obs_status_label"] = OBS_STATUS.get(st, "(code not in connector table; see OECD CL_OBS_STATUS)")
        if mt:
            f["methodology_type"] = mt
            f["methodology_label"] = METHODOLOGY.get(mt, "(see OECD CL_METHODOLOGY_TYPE)")
        if pmap.get(cp):
            f["partner_iso3"] = pmap[cp]
        else:
            f["partner_unmapped"] = True
        flags.append(canon(f))
    x = df.select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(DATASET).alias("dataset"),
        (pl.lit("X|partner=") + pl.col("COUNTERPART_AREA")).alias("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        pl.col("REF_AREA").alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(pl.lit(year), 1, 1).alias("period_start"),
        pl.date(pl.lit(year + 1), 1, 1).alias("period_end"),
        pl.col("OBS_VALUE").replace_strict(vals, return_dtype=pl.String).alias("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        pl.lit(UNIT).alias("unit"),
        pl.Series("flags", flags, dtype=pl.String),
        pl.lit(raw_sha).alias("raw_sha256"),
        pl.concat_str(
            [
                pl.lit(f'{{"column":"OBS_VALUE","file":"BATIS_{year}.csv","row":'),
                pl.col("_row").cast(pl.String),
                pl.lit("}"),
            ]
        ).alias("selector"),
    )
    econ = df.filter(~pl.col("REF_AREA").is_in(list(AGGREGATES)) & ~pl.col("COUNTERPART_AREA").is_in(list(AGGREGATES)))
    a = econ.select("REF_AREA", "COUNTERPART_AREA", "OBS_VALUE", "_row")
    b = a.select(
        pl.col("COUNTERPART_AREA").alias("REF_AREA"),
        pl.col("REF_AREA").alias("COUNTERPART_AREA"),
        pl.col("OBS_VALUE").alias("v_back"),
        pl.col("_row").alias("row_back"),
    )
    j = a.join(b, on=["REF_AREA", "COUNTERPART_AREA"], how="inner")
    rows = []
    for ra, cp, v, r, vb, rb in j.iter_rows():
        d = Decimal(vals[v] or "0") - Decimal(vals[vb] or "0")
        f2: dict[str, Any] = {
            "batis_edition": edition,
            "derived": "exports_minus_imports",
            "estimate": "oecd_wto_balanced",
            "inputs": [f"X|partner={cp} of {ra}", f"X|partner={ra} of {cp}"],
        }
        if pmap.get(cp):
            f2["partner_iso3"] = pmap[cp]
        else:
            f2["partner_unmapped"] = True
        rows.append(
            (
                f"BAL|partner={cp}",
                ra,
                strict_decimal(format(d, "f")) or "0",
                canon(f2),
                json.dumps(
                    {"derived": "OBS_VALUE[A->B] - OBS_VALUE[B->A]", "file": f"BATIS_{year}.csv", "rows": [r, rb]},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    bal = pl.DataFrame(
        rows,
        schema={
            "indicator": pl.String,
            "provider_geo": pl.String,
            "value": pl.String,
            "flags": pl.String,
            "selector": pl.String,
        },
        orient="row",
    ).select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(DATASET).alias("dataset"),
        "indicator",
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        "provider_geo",
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(pl.lit(year), 1, 1).alias("period_start"),
        pl.date(pl.lit(year + 1), 1, 1).alias("period_end"),
        "value",
        pl.lit(None, dtype=pl.String).alias("value_text"),
        pl.lit(UNIT).alias("unit"),
        "flags",
        pl.lit(raw_sha).alias("raw_sha256"),
        "selector",
    )
    return pl.concat([x, bal])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="OECD and WTO - Balanced Trade in Services (BaTIS), OECD SDMX API",
        licence=f"{RIGHTS['licence']}; attribution: {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("OECD.SDD.TPS:DSD_BATIS@DF_BATIS(1.0)",),
        cadence_seconds=90 * 86400,
        planned_requests=1 + len(YEARS),
        max_response_bytes=20_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(_module()),
        field_mappings={"key": KEY, "years": [YEARS[0], YEARS[-1]], "unit": UNIT, "geo_scheme": GEO_SCHEME},
        notes="Total services, exports, balanced (ADJUSTMENT=B): one SDMX-CSV request per year + dataflow.",
    )


def _module() -> Any:
    import sys

    return sys.modules[__name__]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    fixture = run.origin == "test_fixture"
    years = FIXTURE_YEARS if fixture else YEARS
    areas = FIXTURE_REF_AREAS if fixture else None
    fetched: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not run.wants(DATASET):
        return {"fetched": fetched, "errors": errors, "tables": [], "ok": True}
    flow = await run.get(DATAFLOW_URL, key="dataflow", media_types=XML_MEDIA, max_bytes=2_000_000)
    edition = parse_edition(flow.body)
    shas = [run.archive(flow)]
    fetched.append({"item": "dataflow", "bytes": len(flow.body), "sha256": shas[0], "edition": edition})
    frames = []
    for y in years:
        try:
            raw = await run.get(data_url(y, areas), key=f"batis:{y}", media_types=CSV_MEDIA, max_bytes=20_000_000)
            if not raw.body:
                raise SourceResponseError(f"BaTIS {y}: empty response body", retryable=True)
            sha = run.archive(raw)
            frames.append(parse_year(raw.body, sha, y, edition))
        except BudgetExhausted:
            raise
        except Exception as e:
            errors.append({"item": f"batis:{y}", "error": type(e).__name__, "message": str(e)[:300]})
            continue
        shas.append(sha)
        fetched.append({"item": f"batis:{y}", "bytes": len(raw.body), "sha256": sha, "rows": frames[-1].height})
    tables = []
    if frames:
        note = (
            f"OECD-WTO BaTIS BPM6 edition {edition}: total services, exports, balanced (ADJUSTMENT=B), years "
            f"{years[0]}-{years[-1]}, all reporters x partners (aggregates W, WXD, WXOECD, WXEU27_2020, OECD, "
            "EU27_2020 kept unmapped). BAL rows derived: X(A->B) - X(B->A), same year file. Values are OECD-WTO "
            "estimates (balanced matrix), not reported statistics."
        )
        if areas:
            note += f" Fixture: REF_AREA restricted to {'+'.join(areas)}."
        try:
            out = run.register(
                dataset=DATASET,
                frame=pl.concat(frames),
                raw_sha256s=shas,
                coverage_note=note,
                geo_scheme=GEO_SCHEME,
                rights=dict(RIGHTS),
            )
            cov = out.get("coverage") or {}
            tables.append(
                {
                    "table": f"{SOURCE}.{DATASET}",
                    "dataset_key": out.get("dataset_key"),
                    "created": out.get("created"),
                    "rows": out.get("row_count"),
                    "countries": cov.get("countries"),
                    "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []],
                    "artifact_sha256": out.get("artifact_sha256"),
                }
            )
        except Exception as e:
            errors.append({"stage": "register", "error": type(e).__name__, "message": str(e)[:500]})
    return {"fetched": fetched, "errors": errors, "tables": tables, "edition": edition, "ok": not errors}


__all__ = ["CONNECTOR_NAME", "DATASET", "atlas_ingest", "atlas_source", "data_url", "parse_edition", "parse_year"]
