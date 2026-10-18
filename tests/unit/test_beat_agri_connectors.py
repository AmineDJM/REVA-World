"""V7 BEAT-AGRI connectors: WASDE reports from the ESMIS archive (TXT 1998-2010, XML 2010-) and NCEI ag-belt series.

Fixtures are captures of the live files (tests/fixtures/source/atlas.usda_wasde_esmis, atlas.ncei_cag; licence and
live sha256 in each manifest). The XML fixtures are sub-report excerpts (region groups other than World removed).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from reva.adapters.connectors import atlas_ncei_cag as cag
from reva.adapters.connectors import atlas_usda_wasde_esmis as W

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"
E = FIX / "atlas.usda_wasde_esmis"


def _cells(rep: W.ReportParse) -> dict[tuple[str, int], W.Cell]:
    return {(c.series, c.my): c for c in rep.cells}


def test_listing_rows_give_release_dates_and_report_files() -> None:
    rels = W.parse_listing((E / "listing_page_26.html").read_bytes())
    by = {r.release: r for r in rels}
    r = by[date(2005, 7, 12)]
    assert r.report_file() is not None and r.report_file().endswith("wasde-07-12-2005.txt")  # type: ignore[union-attr]
    rels19 = {r.release: r for r in W.parse_listing((E / "listing_page_19.html").read_bytes())}
    # from 2010-07 the XML is preferred even when a TXT is listed for the same release
    assert rels19[date(2010, 8, 12)].report_file().endswith(".xml")  # type: ignore[union-attr]
    # a release with only a TXT correction is still readable
    assert rels19[date(2010, 12, 15)].report_file().endswith(".txt")  # type: ignore[union-attr]


def test_txt_2005_headline_cells_and_positional_status() -> None:
    rep = W.parse_txt((E / "wasde-07-12-2005.txt").read_bytes())
    assert rep.report_month == 7
    c = _cells(rep)
    assert {s for s, _ in c} == set(W.SERIES)
    # U.S. corn 2005/06 July projection (last column), 2004/05 estimate, 2003/04 history
    assert c[("us.corn.yield", 2005)].value == "145" and c[("us.corn.yield", 2005)].status == "projection"
    assert c[("us.corn.production", 2005)].value == "10785"
    assert (
        c[("us.corn.ending_stocks", 2004)].value == "2115" and c[("us.corn.ending_stocks", 2004)].status == "estimate"
    )
    assert c[("us.corn.production", 2003)].status == "published_history"
    assert c[("us.soybeans.production", 2005)].value == "2890"
    assert c[("us.wheat.ending_stocks", 2005)].value == "700"
    assert c[("us.rice.yield", 2005)].value == "6721"  # TOTAL section, not long grain
    assert c[("world.corn.production", 2005)].value == "667.51"  # July sub-row, not June
    assert c[("world.corn.ending_stocks", 2005)].value == "114.33"
    assert c[("world.rice.production", 2004)].value == "401.21"
    assert c[("world.soybeans.ending_stocks", 2003)].value == "35"
    assert c[("world.corn.production", 2005)].labels["month"] == "July"


def test_xml_2015_matches_printed_report() -> None:
    rep = W.parse_xml((E / "wasde-07-10-2015.xml").read_bytes())
    assert rep.report_month == 7
    c = _cells(rep)
    assert {s for s, _ in c} == set(W.SERIES)
    assert c[("us.corn.production", 2015)].value == "13530"  # July column, not June (13630)
    assert c[("us.corn.yield", 2015)].value == "166.8"
    assert c[("us.soybeans.ending_stocks", 2015)].value == "425"
    assert c[("us.rice.production", 2015)].value == "207"  # total rice (matrix 1), not long grain
    assert c[("us.wheat.production", 2015)].value == "2148"
    assert c[("world.soybeans.production", 2015)].value == "318.92"
    assert c[("world.corn.ending_stocks", 2014)].value == "193.95"
    assert c[("world.corn.ending_stocks", 2014)].status == "estimate"


def test_txt_and_xml_of_the_same_release_agree() -> None:
    """2010-08-12 was published as TXT and XML: same 60 cells, values equal up to one unit of the last printed
    digit (2 of 60 cells differ by 0.01: rounding of the two renderings)."""
    a = _cells(W.parse_txt((E / "wasde-08-12-2010.txt").read_bytes()))
    b = _cells(W.parse_xml((E / "wasde-08-12-2010.xml").read_bytes()))
    assert set(a) == set(b) and len(a) == 60
    diffs = [k for k in a if a[k].value != b[k].value]
    assert all(abs(float(a[k].value) - float(b[k].value)) <= 0.0100001 for k in a)
    assert len(diffs) <= 2
    assert all(a[k].status == b[k].status for k in a)


def test_numbers_and_labels() -> None:
    assert W.number("10,785") == "10785" and W.number("166.8 *") == "166.8" and W.number("0.40") == "0.4"
    assert W.number("NA") is None
    assert W.norm_label("World  3/") == "world" and W.norm_label("Ending\r\nStocks") == "ending stocks"
    assert W.my_of("2015/16 (Projected)") == (2015, "projection") and W.my_of("2014/15 Est.") == (2014, "estimate")
    with pytest.raises(W.EsmisError):
        W.my_of("2015/17")
    assert W.availability(date(2015, 7, 10)) == datetime(2015, 7, 11, tzinfo=UTC)


def test_layout_surprise_is_typed() -> None:
    body = (
        (E / "wasde-07-12-2005.txt")
        .read_bytes()
        .replace(b"Production                  : 10,089", b"Production                  : 10,0x9")
    )
    with pytest.raises(W.EsmisError):
        W.parse_txt(body)


def test_cag_parse() -> None:
    head, rows = cag.parse_csv((FIX / "atlas.ncei_cag" / "cag_261_tavg.csv").read_bytes(), region=261, param="tavg")
    assert head == {"title": "Corn Belt (Area Weighted) Average Temperature", "units": "Degrees Fahrenheit"}
    assert rows[0][:2] == (1990, 1) and len(rows) > 400
    jul = {y: v for y, m, v in rows if m == 7}
    assert jul[1990] == "72"
    with pytest.raises(cag.CagError):
        cag.parse_csv((FIX / "atlas.ncei_cag" / "cag_261_tavg.csv").read_bytes(), region=262, param="tavg")


def test_archive_naming_variants_and_default_namespace() -> None:
    r = W.Release(date(2019, 3, 8), ("/x/latest.pdf", "/x/latest.xml", "/x/latest.txt"))
    assert r.report_file() == "/x/latest.xml"
    r2 = W.Release(date(2018, 10, 11), ("/y/WASDE-582-October-11-2018.xml", "/y/WASDE-582-October-11-2018.pdf"))
    assert r2.report_file() == "/y/WASDE-582-October-11-2018.xml"
    assert W.Release(date(2014, 1, 23), ("/z/wasde-01-23-2014.asc",)).report_file() is None
    # some 2010 / 2015 reports declare xmlns="wasde" on the root
    body = (
        (E / "wasde-07-10-2015.xml")
        .read_bytes()
        .replace(b'<Report Name="wasde">', b'<Report Name="wasde" xmlns="wasde">', 1)
    )
    assert body != (E / "wasde-07-10-2015.xml").read_bytes()
    assert len(W.parse_xml(body).cells) == 60
