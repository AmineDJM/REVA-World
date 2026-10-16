"""LINKS2 atlas derivations (reva.links.atlas_links) over a synthetic snapshot DuckDB file shaped like
``reva.atlas.query.build_snapshot_db`` (tables ``series`` / ``cells`` / ``cells_s``)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import duckdb
import pytest

from reva.atlas.query import DDL
from reva.links.atlas_links import MIN_COMMON_ROLLCALLS, AtlasLinkReader
from reva.links.derive import assert_all_evidenced
from reva.links.store import link_from_row, link_row

SHA = "a" * 64
TSHA = "b" * 64


class Snap:
    def __init__(self) -> None:
        self.con = duckdb.connect()
        self.con.execute(DDL)
        self.sids: dict[tuple[str, str, str], int] = {}

    def cell(
        self,
        source: str,
        dataset: str,
        indicator: str,
        iso3: str | None,
        *,
        year: int | None = None,
        day: date | None = None,
        value: str | None = None,
        value_text: str | None = None,
        unit: str = "US dollars",
        flags: dict[str, Any] | None = None,
        qid: str | None = None,
        geo: str | None = None,
    ) -> None:
        key = (source, dataset, indicator)
        if key not in self.sids:
            self.sids[key] = len(self.sids) + 1
            self.con.execute("INSERT INTO series VALUES (?, ?, ?, ?)", [self.sids[key], *key])
        start = day or date(year or 2000, 1, 1)
        end = date(start.year + 1, 1, 1) if day is None else start
        self.con.execute(
            "INSERT INTO cells VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'calendar_year')",
            [
                self.sids[key],
                iso3,
                year if day is None else None,
                value is not None or value_text is not None,
                geo or iso3 or "?",
                qid,
                start,
                end,
                value,
                value_text,
                unit,
                json.dumps(flags or {}),
                SHA,
                json.dumps({"file": "x.csv", "row": len(self.sids)}),
                TSHA,
            ],
        )

    def reader(self) -> AtlasLinkReader:
        self.con.execute("CREATE OR REPLACE TABLE cells_s AS SELECT * FROM cells")

        def rows(sql: str, params: Any) -> list[dict[str, Any]]:
            res = self.con.execute(sql, list(params))
            names = [d[0] for d in res.description]
            return [dict(zip(names, r, strict=True)) for r in res.fetchall()]

        return AtlasLinkReader(rows, snapshot_sha256="c" * 64, origin="test_fixture")


def _trade(s: Snap, rep: str, partner: str | None, year: int, value: str | None, flow: str = "XG_FOB_USD") -> None:
    s.cell("imf", "imts", f"{flow}|partner={partner or 'G001'}", rep, year=year, value=value,
           flags={"partner_iso3": partner})  # fmt: skip


def test_exports_are_directed_weighted_by_the_reported_string_and_skip_zero_and_missing() -> None:
    s = Snap()
    _trade(s, "BRA", "PRT", 2023, "1234.50")
    _trade(s, "BRA", "USA", 2023, "0")
    _trade(s, "BRA", "ARG", 2023, None)
    _trade(s, "BRA", None, 2023, "99")  # aggregate partner (unmapped)
    _trade(s, "PRT", "BRA", 2023, "800", flow="MG_CIF_USD")  # PRT imports from BRA: BRA -> PRT
    r = s.reader()
    tax = r.taxonomy
    d = r.dyadic(tax.link("bilateral_trade"), top_k=10)
    assert [(lk.subject, lk.object, lk.weight, lk.unit) for lk in d.links] == [
        ("iso3:BRA", "iso3:PRT", "1234.50", "US dollars")
    ]
    assert d.skipped["reported_zero"] == 1 and d.skipped["missing_value"] == 1 and d.skipped["partner_unmapped"] == 1
    ev = d.links[0].evidence[0]
    assert ev.kind == "atlas_row" and ev.raw_sha256 == SHA and json.loads(ev.selector or "")["file"] == "x.csv"
    assert d.links[0].temporal.kind == "snapshot" and d.links[0].temporal.year == 2023
    m = r.dyadic(tax.link("bilateral_trade_imports"), top_k=10)
    assert [(lk.subject, lk.object, lk.weight) for lk in m.links] == [("iso3:BRA", "iso3:PRT", "800")]
    assert m.links[0].basis["reported_by"] == "PRT"
    assert_all_evidenced(d.links + m.links)


def test_top_k_keeps_the_union_of_both_endpoints_top_partners() -> None:
    s = Snap()
    for i, p in enumerate(("AAA", "BBB", "CCC", "DDD")):
        _trade(s, "USA", p, 2020, str(1000 - i))
    _trade(s, "EEE", "DDD", 2020, "1")  # DDD's only other incoming flow
    d = s.reader().dyadic(s.reader().taxonomy.link("bilateral_trade"), top_k=2)
    pairs = {(lk.subject, lk.object) for lk in d.links}
    # USA's top 2 (AAA, BBB) + each importer's top 2 exporters (USA is in all of them) + EEE->DDD
    assert ("iso3:USA", "iso3:AAA") in pairs and ("iso3:USA", "iso3:DDD") in pairs and ("iso3:EEE", "iso3:DDD") in pairs


def test_migrant_stock_runs_origin_to_destination_both_sexes_only() -> None:
    s = Snap()
    for sex, v in (("T", "150"), ("F", "70"), ("M", "80")):
        s.cell("un_desa", "ims2024", f"migrant_stock|origin=076|sex={sex}", "PRT", year=2020, value=v, unit="persons",
               flags={"counterpart_iso3": "BRA"})  # fmt: skip
    d = s.reader().dyadic(s.reader().taxonomy.link("migrant_stock"), top_k=5)
    assert [(lk.subject, lk.object, lk.weight, lk.unit) for lk in d.links] == [
        ("iso3:BRA", "iso3:PRT", "150", "persons")
    ]


def test_land_border_merges_both_directions_and_refuses_disagreement() -> None:
    s = Snap()
    s.cell("naturalearth", "admin0_10m_borders", "land_border_km|partner=PRT", "ESP", year=2026, value="1224.3",
           unit="km", flags={"partner_iso3": "PRT"})  # fmt: skip
    s.cell("naturalearth", "admin0_10m_borders", "land_border_km|partner=ESP", "PRT", year=2026, value="1224.3",
           unit="km", flags={"partner_iso3": "ESP"})  # fmt: skip
    s.cell("naturalearth", "admin0_10m_borders", "land_border_km|partner=AND", "FRA", year=2026, value="56",
           unit="km", flags={"partner_iso3": "AND"})  # fmt: skip
    s.cell("naturalearth", "admin0_10m_borders", "land_border_km|partner=FRA", "AND", year=2026, value="57",
           unit="km", flags={"partner_iso3": "FRA"})  # fmt: skip
    d = s.reader().dyadic(s.reader().taxonomy.link("land_border"))
    assert len(d.links) == 1
    lk = d.links[0]
    assert (lk.subject, lk.object, lk.weight, lk.directed, len(lk.evidence)) == (
        "iso3:ESP",
        "iso3:PRT",
        "1224.3",
        False,
        2,
    )
    assert d.skipped["directions_disagree"] == 1


def _votes(s: Snap, rcid: int, day: date, issues: list[str], positions: dict[str, str]) -> None:
    s.cell("unga", "rollcalls", f"rollcall|rcid={rcid}", None, day=day, value_text=f"Resolution {rcid}",
           unit="description", flags={"issue_codes": issues, "unres": f"A/RES/{rcid}"}, geo="UNGA")  # fmt: skip
    for node, pos in positions.items():
        iso3, qid = (node, None) if not node.startswith("Q") else (None, node)
        s.cell("unga", "votes", f"vote|rcid={rcid}", iso3, day=day, value_text=None if pos == "-" else pos,
               unit="vote position", flags={} if pos != "-" else {"missing": "unreliable"}, qid=qid,
               geo=f"cow:{node}")  # fmt: skip


def test_vote_agreement_is_a_share_of_identical_votes_with_counts() -> None:
    s = Snap()
    # 12 colonialism roll calls in 1960-1961: USA/GBR identical on 9 (incl. 3 joint abstentions); absences excluded
    seq = [("yes", "yes")] * 4 + [("abstain", "abstain")] * 3 + [("no", "no")] * 2 + [("yes", "no")] * 3
    for i, (u, g) in enumerate(seq):
        _votes(s, 100 + i, date(1960 + i % 2, 10, 1 + i), ["co"], {"USA": u, "GBR": g, "Q15180": "yes"})
    _votes(s, 200, date(1960, 12, 14), ["co"], {"USA": "absent", "GBR": "yes", "Q15180": "yes"})
    _votes(s, 201, date(1960, 12, 15), [], {"USA": "yes", "GBR": "-", "Q15180": "no"})
    r = s.reader()
    d = r.votes(grain="decade", pair=("GBR", "USA"))
    by_issue = {lk.basis["issue"]: lk for lk in d.links}
    co = by_issue["co"]
    assert (co.basis["n_common"], co.basis["n_identical"], co.weight) == (12, 9, "0.7500")
    assert co.basis["both_abstain"] == 3 and co.unit == "share of identical votes (0-1)"
    assert co.temporal.kind == "interval" and (co.temporal.first_year, co.temporal.last_year) == (1960, 1969)
    assert co.basis["method"] == "derived" and co.basis["method_version"] == "links-vote-agreement/1"
    sel = json.loads(co.evidence[0].selector or "")
    assert sel["pair"] == ["GBR", "USA"] and sel["issue"] == "co" and co.evidence[0].raw_sha256 == SHA
    assert by_issue["all"].basis["n_common"] == 12  # rcid 200 (absent) and 201 (missing) excluded
    # a historical state keeps its own node (QID), never a present-day country
    ussr = r.votes(grain="decade", pair=("Q15180", "USA"), issues=["all"])
    assert {lk.subject for lk in ussr.links} | {lk.object for lk in ussr.links} == {"Q15180", "iso3:USA"}
    # per year: 6 roll calls each, fewer than the minimum -> no link, counted
    y = r.votes(grain="year", pair=("GBR", "USA"))
    assert y.links == [] and y.skipped["fewer_than_min_common_rollcalls"] >= 2
    assert MIN_COMMON_ROLLCALLS == 10
    det = r.vote_detail("GBR", "USA", year_lo=1960, year_hi=1960, issue="co")
    assert det["rollcalls"][0]["resolution"] == "A/RES/100" and {"GBR", "USA"} <= set(det["rollcalls"][0])


def test_vote_agreement_country_mode_and_store_roundtrip() -> None:
    s = Snap()
    for i in range(10):
        _votes(s, i, date(1975, 1, 1 + i), ["hr"], {"FRA": "yes", "USA": "no" if i < 4 else "yes", "IND": "yes"})
    r = s.reader()
    d = r.votes(grain="decade", node="FRA", issues=["hr"])
    shares = {(lk.subject, lk.object): Decimal(lk.weight or "0") for lk in d.links}
    assert shares == {("iso3:FRA", "iso3:IND"): Decimal("1.0000"), ("iso3:FRA", "iso3:USA"): Decimal("0.6000")}
    row = link_row(d.links[0], 1, {})
    assert row["subject_iso3"] == "FRA" and row["facet"] == "issue=hr" and row["origin"] == "test_fixture"
    back = link_from_row(json.loads(json.dumps(row)))
    assert back.key == d.links[0].key and back.evidence == d.links[0].evidence


def test_sources_absent_from_the_snapshot_give_no_link() -> None:
    s = Snap()
    _trade(s, "BRA", "PRT", 2023, "1")
    r = s.reader()
    assert r.votes().links == [] and r.votes().skipped["source_not_in_snapshot"] == 1
    assert r.dyadic(r.taxonomy.link("migrant_stock")).skipped["source_not_in_snapshot"] == 1


@pytest.mark.parametrize("kind", ["bilateral_trade", "migrant_stock", "land_border", "bilateral_trade_baci"])
def test_dyadic_kinds_declare_their_atlas_spec(kind: str) -> None:
    from reva.links.taxonomy import load_taxonomy

    lt = load_taxonomy().link(kind)
    assert lt.atlas and lt.status == "derivable" and lt.weight_kind == "source_quantity"


def test_issues_with_equal_shares_stay_distinct_links() -> None:
    from reva.links.derive import merge_links

    s = Snap()
    for i in range(10):
        _votes(s, i, date(1981, 2, 1 + i), ["co", "hr"], {"USA": "no", "GBR": "no"})
    links = s.reader().votes(grain="decade", pair=("GBR", "USA")).links
    assert len(merge_links(links)) == len(links) == 3  # all, co, hr: same share 1.0000, three facets
