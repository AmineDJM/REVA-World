"""Review V3b — links layer (reva.links.atlas_links, dataset_kg, derive).

VOTE-1: with a year window (pair view ``from_year``/``to_year``, NLQA ``links_pair`` with years) the decade
        vote-agreement link is computed over part of the decade but labelled with the whole decade, and its
        evidence selector states the whole decade ("years": [1960, 1969]) — the citation no longer reproduces
        the number.
CSH-1:  CShapes coverage starts on 1886-01-01; a dependency period that starts there (Algeria, colony of France
        since 1830) is served as starting 1886-01-01 at day precision, with nothing saying the start is the
        dataset's coverage start.
LIC-2:  ``co_party`` links (UN Treaty Collection: UN Terms of Use, personal non-commercial use, not an open
        licence) carry no licence in their basis, unlike CShapes / OpenFlights / GCAT links, so the relation
        page and the API never show the restriction.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import duckdb
import pytest

from reva.atlas.query import DDL
from reva.links.atlas_links import AtlasLinkReader
from reva.links.dataset_kg import derive_dataset_links
from reva.links.derive import KgIndex, derive_co_party
from reva.links.model import PartialDate, Stmt

SHA = "a" * 64
TSHA = "b" * 64


class VoteSnap:
    def __init__(self) -> None:
        self.con = duckdb.connect()
        self.con.execute(DDL)
        self.sids: dict[tuple[str, str, str], int] = {}

    def _cell(self, key: tuple[str, str, str], iso3: str | None, day: date, text: str, flags: dict[str, Any]) -> None:
        if key not in self.sids:
            self.sids[key] = len(self.sids) + 1
            self.con.execute("INSERT INTO series VALUES (?, ?, ?, ?)", [self.sids[key], *key])
        self.con.execute(
            "INSERT INTO cells VALUES (?, ?, NULL, TRUE, NULL, ?, NULL, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'point_in_time')",
            [self.sids[key], iso3, iso3 or "UNGA", day, day, text, "vote", json.dumps(flags), SHA, "{}", TSHA],
        )

    def rollcall(self, rcid: int, day: date, issues: list[str], positions: dict[str, str]) -> None:
        self._cell(
            ("unga", "rollcalls", f"rollcall|rcid={rcid}"),
            None,
            day,
            f"Resolution {rcid}",
            {"issue_codes": issues, "unres": f"A/RES/{rcid}"},
        )
        for iso3, pos in positions.items():
            self._cell(("unga", "votes", f"vote|rcid={rcid}"), iso3, day, pos, {})

    def reader(self) -> AtlasLinkReader:
        self.con.execute("CREATE OR REPLACE TABLE cells_s AS SELECT * FROM cells")

        def rows(sql: str, params: Any) -> list[dict[str, Any]]:
            res = self.con.execute(sql, list(params))
            names = [d[0] for d in res.description]
            return [dict(zip(names, r, strict=True)) for r in res.fetchall()]

        return AtlasLinkReader(rows, snapshot_sha256="c" * 64, origin="test_fixture")


# Fixed (agent REVFIX2).
def test_decade_vote_agreement_with_a_year_window_states_the_years_it_was_computed_on() -> None:
    s = VoteSnap()
    # 1961: 10 identical votes; 1966: 10 roll calls, 5 identical  -> decade 15/20, 1965-1969 only 5/10
    for i in range(10):
        s.rollcall(100 + i, date(1961, 10, 1 + i), ["co"], {"USA": "no", "GBR": "no"})
    for i in range(10):
        s.rollcall(200 + i, date(1966, 10, 1 + i), ["co"], {"USA": "no", "GBR": "no" if i < 5 else "yes"})
    r = s.reader()
    (full,) = [lk for lk in r.votes(grain="decade", pair=("GBR", "USA")).links if lk.basis["issue"] == "co"]
    assert full.weight == "0.7500"
    (part,) = [
        lk for lk in r.votes(grain="decade", pair=("GBR", "USA"), year_lo=1965).links if lk.basis["issue"] == "co"
    ]
    assert part.weight == "0.5000" and part.basis["roll_call_years"] == [1966, 1966]
    sel = json.loads(part.evidence[0].selector or "{}")
    # the value is not the 1960s agreement: the link or at least its citation must say 1965-1969
    assert part.temporal.first_year >= 1965 or sel["years"][0] >= 1965
    # the label, the temporal and the citation all state the years used (1965-1966: window and data coverage)
    assert (part.temporal.first_year, part.temporal.last_year) == (1965, 1966) and sel["years"] == [1965, 1966]
    assert part.basis["decade"] == [1960, 1969] and part.basis["partial_period"] is True
    # VOTE-2: the data stop in 1966 here, so even the whole-decade link is labelled with the real end year
    assert (full.temporal.first_year, full.temporal.last_year) == (1961, 1966)


def _d(y: int, m: int = 1, day: int = 1) -> PartialDate:
    return PartialDate(y, m, day, 11)


def _corr(node: str, iso3: str, a: int, b: int) -> Stmt:
    return Stmt(
        f"c:{node}:{iso3}:{a}",
        node,
        "geopol:corresponds_to_iso3",
        value=iso3,
        start=PartialDate(a),
        end=PartialDate(b),
        source_key="cshapes.cran",
    )


# Fixed (agent REVFIX2).
def test_cshapes_colonial_period_starting_at_coverage_start_is_marked_left_censored() -> None:
    sts = [
        Stmt(
            "r1",
            "cshapes:gw:615",
            "cshapes:ruled_by",
            "gw:220",
            start=_d(1886),
            end=_d(1962, 7, 4),
            qualifiers={"status": ("colony",)},
            source_key="cshapes.cran",
        ),
        _corr("cshapes:gw:615", "DZA", 1962, 2019),
        _corr("gw:220", "FRA", 1816, 2019),
    ]
    (lk,) = derive_dataset_links(sts, node_of=lambda c: f"iso3:{c}", qid_iso3={})["colonial_rule_cshapes"].links
    assert lk.temporal.first_year == 1886
    text = json.dumps(lk.basis).lower()
    assert "censor" in text or "coverage_start" in text or "not before" in text
    assert lk.temporal.start is not None and lk.temporal.start.iso() == "1886"  # never served as 1886-01-01
    assert lk.basis["left_censored"] is True and lk.temporal.end is not None and lk.temporal.end.iso() == "1962-07-04"


# Fixed (agent REVFIX2).
def test_co_party_links_carry_the_untc_licence_restriction() -> None:
    t = "untc:treaty:XVIII-10"
    sts = [
        Stmt(f"untc:{t}:{n}", t, "untc:party", q, start=_d(2002, 6, 20), source_key="untc.mtdsg", origin="live")
        for n, q in enumerate(("Q155", "Q45"))
    ]
    (lk,) = derive_co_party(KgIndex(sts)).links
    text = json.dumps(lk.basis).lower()
    assert "licence" in text or "license" in text or "terms" in text
    assert lk.basis["open_licence"] is False and lk.basis["non_commercial"] is True


class _FakeSnapshot:
    sha256 = "c" * 64
    origin = "test_fixture"

    def ref(self) -> dict[str, Any]:
        return {"snapshot_sha256": self.sha256, "origin": self.origin}


class _FakeQuery:
    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self.snapshot = _FakeSnapshot()

    def _rows(self, sql: str, params: Any) -> list[dict[str, Any]]:
        res = self.con.execute(sql, list(params))
        names = [d[0] for d in res.description]
        return [dict(zip(names, r, strict=True)) for r in res.fetchall()]


class _NoKg:
    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def iso3_to_qid(self, *a: Any) -> dict[str, str]:
        return {"USA": "Q30"}

    def labels(self, nodes: Any) -> dict[str, Any]:
        return {}


# Fixed (agent REVFIX2).
def test_on_demand_country_view_reports_a_capped_atlas_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from reva.links import geo_hops, service

    con = duckdb.connect()
    con.execute(DDL)
    # USA exports to 250 partners x 201 years (50,250 rows > the 50,000 cap); the largest partner, ZWE, sorts last
    con.execute(
        """CREATE TEMP TABLE p AS
           SELECT chr(CAST(65 + i // 26 % 26 AS INTEGER)) || chr(CAST(65 + i % 26 AS INTEGER)) || 'Q' AS code, i FROM range(249) t(i)
           UNION ALL SELECT 'ZWE', 999"""
    )
    con.execute(
        "INSERT INTO series SELECT row_number() OVER (ORDER BY code)::INTEGER, 'imf', 'imts', "
        "'XG_FOB_USD|partner=' || code FROM p"
    )
    con.execute(
        """INSERT INTO cells
           SELECT s.sid, 'USA', y, TRUE, NULL, 'USA', NULL, make_date(y, 1, 1), make_date(y + 1, 1, 1),
                  CASE WHEN p.code = 'ZWE' THEN '999999999' ELSE CAST(100 + p.i AS VARCHAR) END, NULL, 'US dollars',
                  '{"partner_iso3":"' || p.code || '"}', ?, '{}', ?, 'calendar_year'
             FROM p JOIN series s ON s.indicator = 'XG_FOB_USD|partner=' || p.code, range(1800, 2001) t(y)""",
        [SHA, TSHA],
    )
    con.execute("CREATE OR REPLACE TABLE cells_s AS SELECT * FROM cells ORDER BY sid, year, iso3")
    assert con.execute("SELECT count(*) FROM cells").fetchone()[0] == 250 * 201  # type: ignore[index]

    monkeypatch.setattr(service, "links_for_country", lambda *a, **k: {"qid": None, "links": [], "labels": {}})
    monkeypatch.setattr(service, "KgNeighbourhood", _NoKg)
    monkeypatch.setattr(geo_hops, "hop_evidence_from_db", lambda conn: {})
    data = service._country_on_demand(
        None, "USA", ["bilateral_trade"], None, None, 1, lambda sha: _FakeQuery(con), False
    )
    top = data["by_kind"]["bilateral_trade"]["groups"][0]["links"][0]
    # either the read is reported as truncated, or the ranking is right (ZWE: 999,999,999 USD in 2000)
    assert data.get("truncated") or top["object"] == "iso3:ZWE"
    assert data["truncated"] is True and data["truncated_kinds"] == ["bilateral_trade"]
    assert top["object"] == "iso3:ZWE"  # ranked over the latest year, read first under the cap
