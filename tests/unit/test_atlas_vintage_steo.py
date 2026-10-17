"""EIA STEO archive vintages (V6, PRED-ENERGY): archives page, availability rule, workbook parser, as-of reads.

Fixtures (``tests/fixtures/source/atlas.vintage_steo``, licence + live sha256 in the manifest): the STEO Archives
page reduced to four issue rows (October / September 2026, July / June 2013, verbatim ``<tr>`` elements) and the
July 2013 and October 2026 data files reduced to the sheets ``Dates``, ``2tab`` and ``3atab`` (sheet parts and
shared strings verbatim).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_vintage_steo as st
from reva.adapters.connectors.vintage_common import AVAILABILITY_RULES, FAMILIES, Rows, known_variable
from reva.atlas.sources import discover
from reva.atlas.tables import validate_atlas_frame
from reva.evaluation.cutoff import CutoffMode
from reva.evaluation.vintages import VINTAGE_DATASET_KEYS, VintageAccessor, cells_from_atlas_frame

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "source" / "atlas.vintage_steo"
SHA = "b" * 64


def _fixture(url_part: str) -> tuple[bytes, dict[str, Any]]:
    man = json.loads((FIX / "manifest.json").read_text())
    hits = [e for e in man if url_part in e["url"]]
    assert len(hits) == 1, url_part
    return (FIX / hits[0]["file"]).read_bytes(), hits[0]


def _issue(label: str) -> st.Issue:
    issues, _ = st.parse_archive_page(_fixture("outlook.php")[0])
    return next(i for i in issues if i.label == label)


def _parse(file: str, label: str) -> tuple[pl.DataFrame, st.WorkbookReport]:
    body, entry = _fixture(file)
    iss = _issue(label)
    avail, basis, lm = st.availability(iss, entry["headers"]["last-modified"])
    rows = Rows()
    rep = st.parse_workbook(body, iss, SHA, rows, available_at=avail, basis=basis, last_modified=lm)
    return rows.frame(), rep


def _cell(df: pl.DataFrame, code: str, period: date) -> dict[str, Any]:
    f = df.filter(pl.col("indicator").str.starts_with(code + "|v=") & (pl.col("period_start") == period))
    assert f.height == 1, (code, period, f.height)
    return {**f.row(0, named=True), "f": json.loads(f["flags"][0])}


def test_source_is_discovered_and_registered_as_vintage_family() -> None:
    found, problems = discover([st.__name__])
    assert st.NAME in found and not problems
    assert "eia_steo" in FAMILIES
    assert {"steo_archive_release_date", "steo_archive_file_last_modified"} <= set(AVAILABILITY_RULES)
    assert "atlas.vintage_steo.steo_archive" in VINTAGE_DATASET_KEYS
    assert known_variable("eia_steo:BREPUUS") and not known_variable("eia_steo:") and not known_variable("x:Y")


def test_archives_page_rows_release_dates_and_files() -> None:
    issues, stats = st.parse_archive_page(_fixture("outlook.php")[0])
    by = {i.label: i for i in issues}
    assert set(by) == {"October 2026", "September 2026", "July 2013", "June 2013"}
    assert by["October 2026"].release == date(2026, 10, 6) and by["October 2026"].file == "oct26_base.xlsx"
    assert by["October 2026"].notice == "notice_data_10_06_2026.php"
    assert by["July 2013"].release_text == "07/09/2013" and by["June 2013"].file == "jun13_base.xls"
    sel = st.select_issues(issues, {})
    assert [i.label for i in sel] == ["July 2013", "October 2026"]  # quarterly months, .xlsx only
    assert stats["issues_listed"] == 4


def test_irregular_markup_rows_without_tr_are_read_in_cell_order() -> None:
    html = (
        b"<h1>STEO Archives</h1><table><tr><td>May 2019</td><td>5/7/2019</td><td>x</td>"
        b'<td><a href="archives/may19_base.xlsx">may19_base.xlsx</a></td><td></td></tr>'
        b"<td>April 2019</td><td>4/9/2019</td><td>x</td>"
        b'<td><a href="archives/apr19_base.xlsx">apr19_base.xlsx</a></td><td></td></tr></table>'
    )
    issues, _ = st.parse_archive_page(html)
    assert [(i.label, i.release) for i in issues] == [("April 2019", date(2019, 4, 9)), ("May 2019", date(2019, 5, 7))]
    with pytest.raises(st.SteoArchiveError):
        st.parse_archive_page(html.replace(b"apr19_base", b"mar19_base"))
    with pytest.raises(st.SteoArchiveError):
        st.parse_archive_page(b"<html>no heading</html>")


def test_release_outside_issue_month_is_not_selected() -> None:
    i = st.Issue("July 2014", 2014, 7, "8/1/2014", date(2014, 8, 1), "jul14_base.xlsx")
    stats: dict[str, Any] = {}
    assert st.select_issues([i], stats) == [] and stats["release_outside_issue_month"] == ["July 2014"]


def test_availability_rule_is_conservative() -> None:
    iss = _issue("October 2026")
    at, basis, _ = st.availability(iss, "Thu, 01 Oct 2026 21:37:23 GMT")
    assert (at, basis) == (datetime(2026, 10, 7, tzinfo=UTC), "steo_archive_release_date")
    at, basis, _ = st.availability(iss, "Tue, 20 Oct 2026 08:00:00 GMT")  # replaced after release
    assert (at, basis) == (datetime(2026, 10, 21, tzinfo=UTC), "steo_archive_file_last_modified")
    assert st.availability(iss, None) == (None, None, None)
    assert st.availability(iss, "not a date") == (None, None, None)


def test_recent_workbook_splits_at_last_historical_month() -> None:
    df, rep = _parse("oct26_base", "October 2026")
    validate_atlas_frame(df, source=st.ATLAS_SOURCE, dataset=st.DS, raw_sha256s=[SHA])
    assert rep.last_historical_month == "202609" and rep.sheets_missing == ["1tab", "3dtab", "4atab", "5atab"]
    sep = _cell(df, "BREPUUS", date(2026, 9, 1))
    assert sep["f"]["estimate_kind"] == "published_history_boundary_unknown"
    assert sep["f"]["workbook_historical_column"] is True and sep["unit"] == "dollars per barrel"
    oct_ = _cell(df, "BREPUUS", date(2026, 10, 1))
    assert oct_["f"]["estimate_kind"] == "projection" and oct_["f"]["horizon"] == "0"
    assert oct_["f"]["available_at"] == "2026-10-07T00:00:00Z" and oct_["f"]["vintage_date_text"] == "10/06/2026"
    assert oct_["indicator"] == "BREPUUS|v=2026-10" and oct_["provider_geo"] == "not_stated"
    w = _cell(df, "PAPR_WORLD", date(2027, 12, 1))
    assert w["f"]["provider_variable"] == "papr_world" and w["f"]["variable"] == "eia_steo:PAPR_WORLD"
    assert w["unit"] == "million barrels per day" and w["f"]["horizon"] == "14"
    stocks = _cell(df, "PASC_OECD_T3", date(2026, 6, 1))
    assert stocks["unit"] == "million barrels"
    # text cells ('-') are counted, never values
    assert rep.non_numeric and all(v != "-" for v in df["value"].to_list())


def test_old_workbook_without_boundary_uses_the_issue_month() -> None:
    df, rep = _parse("jul13_base", "July 2013")
    validate_atlas_frame(df, source=st.ATLAS_SOURCE, dataset=st.DS, raw_sha256s=[SHA])
    assert rep.last_historical_month is None
    jun = _cell(df, "BREPUUS", date(2013, 6, 1))
    assert jun["f"]["estimate_kind"] == "published_history_boundary_unknown"
    assert "workbook_historical_column" not in jun["f"]
    jul = _cell(df, "WTIPUUS", date(2013, 7, 1))
    assert jul["f"]["estimate_kind"] == "projection" and jul["f"]["horizon"] == "0"
    assert jul["f"]["series_label"] == "West Texas Intermediate Spot Average"


def test_forecast_month_must_match_the_archives_page() -> None:
    body, _ = _fixture("oct26_base")
    wrong = st.Issue("September 2026", 2026, 9, "09/09/2026", date(2026, 9, 9), "sep26_base.xlsx")
    with pytest.raises(st.SteoArchiveError):
        st.parse_workbook(body, wrong, SHA, Rows(), available_at=None, basis=None, last_modified=None)


def test_vintage_accessor_reads_issues_as_of() -> None:
    a, _ = _parse("jul13_base", "July 2013")
    b, _ = _parse("oct26_base", "October 2026")
    cells = cells_from_atlas_frame(
        pl.concat([a, b]), dataset_key="atlas.vintage_steo.steo_archive", table_recorded_at=None
    )
    before = VintageAccessor(
        cells, datetime(2013, 7, 10, tzinfo=UTC) - timedelta(microseconds=1), CutoffMode.available_then
    )
    assert before.vintage_in_force("eia_steo") is None
    acc = VintageAccessor(cells, datetime(2014, 1, 1, tzinfo=UTC), CutoffMode.available_then)
    assert acc.vintage_in_force("eia_steo")["vintage"] == "2013-07"
    fc = acc.forecasts(family="eia_steo", variable="eia_steo:BREPUUS").rows
    assert fc.height > 0 and fc["period_start"].min() == date(2013, 7, 1)
    hist = acc.latest(family="eia_steo", variable="eia_steo:BREPUUS").rows
    assert hist["period_start"].max() == date(2013, 6, 1)  # projections never returned as observations
    now = VintageAccessor(cells, datetime(2026, 10, 8, tzinfo=UTC), CutoffMode.available_then)
    h2 = now.latest(family="eia_steo", variable="eia_steo:BREPUUS").rows
    assert h2.filter(pl.col("period_start") == date(2013, 6, 1))["vintage"].to_list() == ["2013-07"]
    assert h2["period_start"].max() == date(2026, 9, 1)
