"""FIGARO connector (io_tables): parsing, completeness, accounting identities, refusal.

Offline. Uses a tiny SYNTHETIC table (tests/fixtures/io_tables/synthetic.py) and two real
captured Eurostat responses (tests/fixtures/source/eurostat.figaro.iciot_ii/): the 2026
content constraint of NAIO_10_FCP_II4 and the 2024 ``DOM`` primary-input rows.
"""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from reva.adapters.connectors import io_tables as it
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import SourceResponseError
from reva.models.economic import io_price
from reva.ports.connectors import FetchBudget

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
_spec = importlib.util.spec_from_file_location("io_synthetic", ROOT / "io_tables" / "synthetic.py")
assert _spec and _spec.loader
syn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(syn)

DIMS = it.TableDims(countries=syn.COUNTRIES, industries=syn.INDUSTRIES)


def parsed_items(**kw: object) -> list[it.ParsedItem]:
    return [
        it.parse_sdmx_csv(syn.body(g, **kw), dataset=syn.DATASET, year=syn.YEAR, origins=g, dims=DIMS)
        for g in syn.groups()
    ]


def test_synthetic_table_passes_identities_and_serialises_exact_decimals() -> None:
    items = parsed_items()
    assert all(not p.findings for p in items)
    built = it.build_dataset(items, dataset=syn.DATASET, year=syn.YEAR, origin="test_fixture", dims=DIMS)
    rep = built.identity_report
    assert rep["passed"] is True
    checks = {c["check"]: c for c in rep["checks"]}
    assert checks["dense_completeness"]["rows"] == DIMS.dense_cells() == 3 * 2 * 3 * 7 + 6 * 3 * 7
    assert checks["row_total_equals_column_total"]["max_abs_diff_mio_eur"] == "0.000"
    assert checks["gross_output_non_negative"]["zero_output_industries"] == 1
    table = built.table()
    assert table.num_rows == built.row_count == DIMS.dense_cells() + 6
    assert str(table.schema.field("value").type) == "decimal128(18, 3)"
    rows = table.to_pylist()
    z = [r for r in rows if r["block"] == "Z" and r["value"] != 0]
    assert {(r["row_country"], r["row_code"], r["col_country"], r["col_code"], r["value"]) for r in z} == {
        ("DE", "B", "FR", "C10-12", Decimal("20.000")),
        ("FR", "C10-12", "DE", "C10-12", Decimal("30.000")),
        ("DE", "C10-12", "FR", "C10-12", Decimal("10.000")),
    }
    x = {(r["row_country"], r["row_code"]): r["value"] for r in rows if r["block"] == "X"}
    assert x[("WRL_REST", "C10-12")] == 0 and x[("FR", "B")] == Decimal(100)
    assert built.manifest["economy_iso3"] == {"FR": "FRA", "DE": "DEU", "WRL_REST": "FIGARO_ROW"}
    # deterministic bytes for identical input
    again = it.build_dataset(parsed_items(), dataset=syn.DATASET, year=syn.YEAR, origin="test_fixture", dims=DIMS)
    assert again.build_sha256 == built.build_sha256


def test_identity_violation_refuses_publication() -> None:
    # one intermediate cell off by 1 million EUR: FR:B row total no longer equals its column total
    items = parsed_items(overrides={("FR", "B", "DE", "B"): "1.000"})
    with pytest.raises(it.IdentityCheckFailed) as e:
        it.build_dataset(items, dataset=syn.DATASET, year=syn.YEAR, origin="test_fixture", dims=DIMS)
    report = e.value.details["report"]
    assert report["passed"] is False
    bad = {c["check"]: c for c in report["checks"]}["row_total_equals_column_total"]
    assert bad["violations"] == 2  # FR:B row (+1) and DE:B column (+1)
    assert {(x["country"], x["industry"]) for x in bad["examples"]} == {("FR", "B"), ("DE", "B")}


def test_tolerance_is_one_unit_in_the_last_published_digit() -> None:
    ok = parsed_items(overrides={("FR", "B", "FR", "P51G"): "100.001"})  # +0.001: within tolerance
    assert it.build_dataset(ok, dataset=syn.DATASET, year=syn.YEAR, dims=DIMS).identity_report["passed"]
    bad = parsed_items(overrides={("FR", "B", "FR", "P51G"): "100.002"})
    with pytest.raises(it.IdentityCheckFailed):
        it.build_dataset(bad, dataset=syn.DATASET, year=syn.YEAR, dims=DIMS)


def test_structural_zero_violation_refuses() -> None:
    items = parsed_items(overrides={("DOM", "OP_RES", "FR", "B"): "5.000", ("DOM", "D1", "FR", "B"): "95.000"})
    with pytest.raises(it.IdentityCheckFailed) as e:
        it.build_dataset(items, dataset=syn.DATASET, year=syn.YEAR, dims=DIMS)
    zeros = {c["check"]: c for c in e.value.details["report"]["checks"]}["structural_zeros"]
    assert zeros["op_rows_nonzero_in_industry_columns"] == 1 and not zeros["passed"]


def test_missing_cell_or_empty_value_is_never_zero() -> None:
    dropped = it.parse_sdmx_csv(
        syn.body(("FR", "DE"), drop={("FR", "B", "DE", "B")}),
        dataset=syn.DATASET,
        year=syn.YEAR,
        origins=("FR", "DE"),
        dims=DIMS,
    )
    assert dropped.blocking and {f.code for f in dropped.findings} == {"incomplete_table"}
    empty = it.parse_sdmx_csv(
        syn.body(("FR", "DE"), overrides={("FR", "B", "DE", "B"): ""}),
        dataset=syn.DATASET,
        year=syn.YEAR,
        origins=("FR", "DE"),
        dims=DIMS,
    )
    assert "missing_or_non_numeric_value" in {f.code for f in empty.findings}
    items = parsed_items()
    items[0] = dropped
    with pytest.raises(it.IdentityCheckFailed, match="incomplete"):
        it.build_dataset(items, dataset=syn.DATASET, year=syn.YEAR, dims=DIMS)
    with pytest.raises(it.IdentityCheckFailed) as e:  # a whole origin group missing
        it.build_dataset(parsed_items()[:2], dataset=syn.DATASET, year=syn.YEAR, dims=DIMS)
    assert e.value.details["missing_origins"] == ["DOM"]


def test_schema_drift_is_blocking() -> None:
    wrong_unit = gzip.decompress(syn.body(("WRL_REST",))).replace(b"MIO_EUR", b"MIO_NAC")
    p = it.parse_sdmx_csv(
        gzip.compress(wrong_unit), dataset=syn.DATASET, year=syn.YEAR, origins=("WRL_REST",), dims=DIMS
    )
    assert "unit_mismatch" in {f.code for f in p.findings}
    p = it.parse_sdmx_csv(syn.body(("WRL_REST",)), dataset=syn.DATASET, year=2023, origins=("WRL_REST",), dims=DIMS)
    assert "period_mismatch" in {f.code for f in p.findings}
    p = it.parse_sdmx_csv(syn.body(("FR", "DE")), dataset=syn.DATASET, year=syn.YEAR, origins=("FR",), dims=DIMS)
    assert "origin_mismatch" in {f.code for f in p.findings}
    flagged = gzip.decompress(syn.body(("WRL_REST",))).replace(b",,\n", b",,c\n", 1)
    p = it.parse_sdmx_csv(gzip.compress(flagged), dataset=syn.DATASET, year=syn.YEAR, origins=("WRL_REST",), dims=DIMS)
    assert "confidential_cells" in {f.code for f in p.findings}
    with pytest.raises(SourceResponseError, match="header"):
        it.parse_sdmx_csv(gzip.compress(b"a,b\n1,2\n"), dataset=syn.DATASET, year=syn.YEAR, origins=("FR",), dims=DIMS)


def test_async_queued_envelope_is_a_typed_retryable_error_not_data() -> None:
    body = (
        b'<?xml version="1.0" encoding="UTF-8" ?><env:Envelope><env:Body><ns0:syncResponse>'
        b"<queued><id>x</id><status>SUBMITTED</status></queued></ns0:syncResponse></env:Body></env:Envelope>"
    )
    with pytest.raises(SourceResponseError) as e:
        it.csv_bytes(body)
    assert e.value.details["kind"] == "provider_async_queued" and e.value.retryable


def test_gzip_bomb_and_truncation_are_refused() -> None:
    with pytest.raises(SourceResponseError, match="exceeds"):
        it._gunzip_bounded(gzip.compress(b"0" * 5000), limit=1000)
    with pytest.raises(SourceResponseError, match="truncated"):
        it._gunzip_bounded(gzip.compress(b"abc" * 1000)[:-12])


def test_constraint_parsing_and_drift_detection() -> None:
    dims = it.parse_constraint(syn.constraint_xml())
    findings, target = it.constraint_findings(dims, None, DIMS)
    assert findings == [] and target == 2024
    _, target = it.constraint_findings(dims, 2023, DIMS)
    assert target == 2023
    findings, target = it.constraint_findings(dims, 2025, DIMS)
    assert target is None and findings[0].code == "year_unavailable"
    findings, _ = it.constraint_findings(dims, None)  # against FIGARO's pinned lists
    assert {f.code for f in findings} == {"code_list_drift"}
    with pytest.raises(SourceResponseError, match="DTD"):
        it.parse_constraint(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>')


FIXTURE_DIR = ROOT / "source" / it.NAME


def test_real_captured_constraint_matches_pinned_figaro_code_lists() -> None:
    entries = json.loads((FIXTURE_DIR / "manifest.json").read_text())
    [entry] = [e for e in entries if "contentconstraint" in e["url"]]
    dims = it.parse_constraint((FIXTURE_DIR / entry["file"]).read_bytes())
    findings, target = it.constraint_findings(dims, None)
    assert findings == [] and target == 2024
    assert len(dims["c_dest"]) == 50 and len(dims["ind_use"]) == 69 and len(dims["ind_ava"]) == 70


def test_real_captured_dom_rows_parse_without_findings() -> None:
    c = it.FigaroConnector(2024)
    http = FixtureHttp(it.NAME)
    [item] = [i for i in c._data_items(2024, "NAIO_10_FCP_II4") if i.params["origins"] == ["DOM"]]
    raw = asyncio.run(c.fetch(item, http))
    [batch] = list(c.parse(raw))
    report = c.validate(batch)
    assert report.outcome == "pass" and batch.metadata["rows"] == 6 * 50 * 69
    assert batch.observations == ()  # a dataset input, never scalar observations
    p = it.parse_sdmx_csv(raw.body, dataset="NAIO_10_FCP_II4", year=2024, origins=("DOM",))
    # published FIGARO 2024: taxes less subsidies on products in final-demand columns are non-zero
    import polars as pl

    d21 = p.frame.filter(pl.col("ind_ava") == "D21X31", pl.col("ind_use") == "P3_S14")
    assert d21.height == 50 and d21["value"].sum() > 0


def test_plan_and_follow_up_items() -> None:
    c = it.FigaroConnector(dims=DIMS)
    plan = c.plan(None, FetchBudget())
    [item] = plan.items
    assert item.url.endswith("/contentconstraint/ESTAT/NAIO_10_FCP_II4")
    from reva.ports.connectors import RawAcquisition

    raw = RawAcquisition(
        item=item,
        body=syn.constraint_xml(),
        media_type="application/xml",
        retrieved_at="2026-10-15T00:00:00+00:00",
        response_metadata={},
        request_fingerprint="x",
        origin="test_fixture",
    )
    [batch] = list(c.parse(raw))
    keys = [f.key for f in batch.follow_up]
    assert keys == [
        "data:NAIO_10_FCP_II4:2024:FR+DE",
        "data:NAIO_10_FCP_II4:2024:WRL_REST",
        "data:NAIO_10_FCP_II4:2024:DOM",
    ]
    assert all(
        "format=SDMX-CSV&compressed=true" in f.url and "startPeriod=2024&endPeriod=2024" in f.url
        for f in batch.follow_up
    )
    figaro = it.origin_groups()
    assert len(figaro) == 26 and figaro[-1] == ("DOM",) and all(len(g) == 2 for g in figaro[:-1])


def test_describe_and_crosswalk_agree_with_the_model() -> None:
    spec = it.FigaroConnector().describe()
    assert spec.template == "eurostat_sdmx_csv" and spec.allowed_hosts == ("ec.europa.eu",)
    assert spec.source_key == "eurostat.dissemination"
    assert tuple(it.ECONOMIES.values()) == io_price.FIGARO_KEYS
    assert it.ECONOMIES["EL"] == "GRC" and it.ECONOMIES["UK"] == "GBR"
    assert it.DATASET_CONCEPT == io_price.DATASET_CONCEPT and it.DATASET_SUBJECT == io_price.DATASET_SUBJECT
    assert it.dataset_for_year(2024) == "NAIO_10_FCP_II4" and it.dataset_for_year(2015) == "NAIO_10_FCP_II2"
