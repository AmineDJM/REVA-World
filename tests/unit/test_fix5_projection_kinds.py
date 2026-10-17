"""FIX5 (QA-DATA D1/D2/D6): one projection vocabulary for every reader; date rule; value-range notes.

The DB inventory below is every ``flags.estimate_kind`` value present in the latest version of the 331 atlas tables
of the dev DB ``reva`` (origin live) on 2026-10-17 (cell counts), scanned read-only through the store API
(``docs/dev/v6_fix5.md``). ``test_live_db_kinds_classified`` re-scans the DB (``pytest -m live``).
"""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

from reva.atlas import projection as P
from reva.atlas.query import PROJECTION_SQL
from reva.atlas.value_notes import qa_flag, signed_series, value_notes, with_qa_flag

DB_KINDS_2026_10_17 = {
    "actual": 101_626,
    "announced_schedule": 90,
    "estimate": 96_242,
    "estimated": 8_529_972,
    "forecast": 10_804,
    "imputed": 1_532_066,
    "model_simulation": 101_844,
    "observation": 2_752_249,
    "observation_gridded": 786_656,
    "projection": 1_675_567,
    "provisional": 16_918,
    "published_history": 405_943,
    "published_history_boundary_unknown": 1_348_365,
    "reanalysis": 479_700,
    "unknown_boundary": 36_047,
}
ROOT = Path(__file__).resolve().parents[2]


def test_every_db_kind_is_classified() -> None:
    for k in DB_KINDS_2026_10_17:
        assert P.kind_class(k) in ("observation", "estimate", "projection"), k
    assert P.kind_class("forecast") == "projection"  # D1: FAOSTAT "F" = Forecast value
    assert P.kind_class("model_simulation") == "projection"
    assert P.kind_class("estimated") == "estimate" and P.kind_class("actual") == "observation"
    assert P.kind_class("something_new") == "unknown" and P.kind_class(None) is None


def _connector_kind_literals() -> set[str]:
    """String literals assigned to ``estimate_kind`` (keyword, dict key or assignment) in every connector."""
    out: set[str] = set()
    for f in (ROOT / "src/reva/adapters/connectors").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.keyword) and n.arg == "estimate_kind":
                vals = [n.value]
            elif isinstance(n, ast.Dict):
                vals = [
                    v
                    for k, v in zip(n.keys, n.values, strict=True)
                    if isinstance(k, ast.Constant) and k.value == "estimate_kind"
                ]
            elif isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "estimate_kind" for t in n.targets
            ):
                vals = [n.value]
            else:
                continue
            for v in vals:
                for c in ast.walk(v):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.isidentifier():
                        out.add(c.value)
    return out


def test_every_connector_kind_is_classified() -> None:
    lits = _connector_kind_literals()
    assert {"projection", "observation", "unknown_boundary"} <= lits
    unclassified = sorted(k for k in lits if P.kind_class(k) == "unknown")
    assert not unclassified, f"add these estimate_kind values to registry/atlas/projection_kinds.yaml: {unclassified}"


def _sql(flags: dict[str, Any], start: date = date(2000, 1, 1), recorded: date | None = None) -> bool:
    con = duckdb.connect()
    con.execute("CREATE TABLE x (flags JSON, period_start DATE, rec DATE)")
    con.execute("INSERT INTO x VALUES (?, ?, ?)", [json.dumps(flags), start, recorded])
    q = P.projection_sql("x", period=True, recorded_sql="x.rec")
    return bool(con.execute(f"SELECT {q} FROM x").fetchone()[0])  # type: ignore[index]


def _flags_only(flags: dict[str, Any]) -> bool:
    con = duckdb.connect()
    con.execute("CREATE TABLE x (flags JSON)")
    con.execute("INSERT INTO x VALUES (?)", [json.dumps(flags)])
    return bool(con.execute(f"SELECT {PROJECTION_SQL} FROM x").fetchone()[0])  # type: ignore[index]


FAO = {"estimate_kind": "forecast", "flag": "F", "flag_meaning": "Forecast value"}
STEO = {"frequency": "M", "last_historical_period": "202712", "series_last_updated": "2026-10-06T16:02:16-04:00"}
ILO = {"estimate_type": "ilo_estimate_in_national_series", "ilo_source": "XA:8405"}
REC = date(2026, 10, 10)
CASES: list[tuple[dict[str, Any], date, bool]] = [
    (FAO, date(2030, 1, 1), True),  # D1
    (FAO, date(1990, 1, 1), True),
    ({"estimate_kind": "brand_new_kind"}, date(1990, 1, 1), True),  # unknown -> not observed
    ({"estimate_kind": "estimated"}, date(1990, 1, 1), False),
    ({"estimate_kind": "model_simulation", "projection": "CMIP6 historical"}, date(1995, 1, 1), True),
    (STEO, date(2026, 11, 1), True),  # D2: declared lastHistoricalPeriod 202712 is after the release
    (STEO, date(2026, 10, 1), False),  # release month itself: a period that has begun (period_not_ended, info)
    (STEO, date(2026, 9, 1), False),
    (ILO, date(2027, 1, 1), True),  # D2: after the table's record date
    (ILO, date(2025, 1, 1), False),
    ({}, date(2084, 1, 1), True),  # oecd.pag projections to 2084, flags {}
    ({"estimate_kind": "announced_schedule"}, date(2027, 3, 1), False),  # announced meeting date: exempt
    ({"projection": "false"}, date(2001, 1, 1), False),
    ({"period_after_vintage": "true"}, date(2001, 1, 1), True),
]


@pytest.mark.parametrize(("flags", "start", "want"), CASES)
def test_sql_and_python_agree(flags: dict[str, Any], start: date, want: bool) -> None:
    assert _sql(flags, start, REC) is want
    assert P.is_projection(flags, period_start=start, recorded=REC) is want
    assert P.is_projection(flags, period_start=start.isoformat(), recorded=REC.isoformat()) is want
    # flags-only rule (no period columns): kinds and projection flags
    assert _flags_only(flags) is P.flags_say_projection(flags)


def test_date_rule_without_recorded_uses_as_of_and_table_exemption() -> None:
    assert P.is_projection({}, period_start="2027-06-24", as_of=date(2026, 10, 17))
    assert not P.is_projection({}, period_start="2027-06-24", as_of=date(2026, 10, 17), table="untc.treaty_status")
    assert not P.is_projection({}, period_start="-0043-01-01", as_of=date(2026, 10, 17))  # BCE text: no date rule
    assert not _sql({}, date(2084, 1, 1), None)  # recorded NULL (exempt table in the query layer): rule off


def test_readers_share_the_vocabulary() -> None:
    from reva.evaluation.health_registry import core as health
    from reva.evaluation.registry import questions
    from reva.nlqa.ports import AtlasCell, cell_is_projection, estimate_status, is_projection
    from reva.predict.db import _is_projection

    assert questions.PROJECTION_KINDS == health.PROJECTION_KINDS == P.PROJECTION_KINDS
    assert {"forecast", "unknown_boundary", "published_history_boundary_unknown"} <= set(P.PROJECTION_KINDS)
    assert is_projection(FAO) and _is_projection(FAO) and questions._is_projection({"flags": FAO})
    assert estimate_status(FAO) == "projection" and estimate_status({"estimate_kind": "reanalysis"}) == "estimate"
    steo = AtlasCell("eia_steo", "steo_observed", "STEO.COPRAP.M", "", "2026-11-01", "2026-12-01", "0.2", flags=STEO)
    assert cell_is_projection(steo)
    assert _is_projection(STEO, "2026-11-01") and not _is_projection(STEO, "2026-09-01")
    assert questions._is_projection({"flags": ILO, "period_start": date(2099, 1, 1), "recorded": "2026-10-10T08:00:00"})
    assert not questions._is_projection({"flags": ILO, "period_start": date(2099, 1, 1)})  # registry: pin date only


def test_energy_outcomes_skip_steo_cells_after_the_release() -> None:
    from reva.evaluation.energy_backtests import outcomes_from_frame

    code = "COPRAP"
    frame = pl.DataFrame(
        {
            "indicator": [f"STEO.{code}.M"] * 2,
            "period_start": [date(2026, 9, 1), date(2026, 11, 1)],
            "value": ["0.2", "0.3"],
            "flags": [json.dumps(STEO)] * 2,
        }
    )
    out = outcomes_from_frame(frame, codes=[code])
    assert len(out[code]) == 1


def test_qa_projection_frame_uses_the_shared_rule() -> None:
    from reva.qa.atlas_checks import projection_frame

    df = pl.DataFrame(
        {
            "flags": [json.dumps(FAO), json.dumps(STEO), "{}", "{}", json.dumps({"estimate_kind": "zzz"})],
            "period_start": [date(2030, 1, 1), date(2026, 11, 1), date(2084, 1, 1), date(2020, 1, 1), date(2000, 1, 1)],
        }
    )
    out = projection_frame(df, REC, "oecd.pag")
    assert out["is_proj"].to_list() == [True, True, True, False, True]
    assert out["by_date"].to_list() == [False, True, True, False, False]
    assert out["unclassified"].to_list() == [False, False, False, False, True]
    assert projection_frame(df, REC, "untc.treaty_status")["is_proj"].to_list() == [True, False, False, False, True]


def test_value_range_notes_flag_without_altering() -> None:
    notes = value_notes()
    assert "wb.edstats.UIS.MENF.56" in signed_series()
    for key, e in notes.items():
        assert e["definition"] and e["note"], key
    s = "wb.wdi_social.EG.ELC.RNWX.ZS"
    assert qa_flag(s, "99.5") is None and qa_flag(s, None) is None
    q = qa_flag(s, "112.3")
    assert q is not None and q["check"] == "outside_definitional_range" and q["value_unchanged"]
    base = {"footnote": "x"}
    got = with_qa_flag(s, "112.3", base)
    assert got is not None and got["reva_qa"] == q and got["footnote"] == "x" and "reva_qa" not in base
    assert with_qa_flag(s, "50", base) is base
    assert qa_flag("wb.edstats.UIS.MENF.56", "-604993") is None  # signed: within the definition


@pytest.mark.live
def test_live_db_kinds_classified() -> None:  # pragma: no cover - needs the dev DB (pytest -m live)
    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import get_engine
    from reva.qa.atlas_checks import atlas_tables
    from reva.qa.backend import postgres_backend

    be = postgres_backend(get_engine("worker"), get_store())
    try:
        kinds: set[str] = set()
        for t in atlas_tables(be, "live"):
            df = be.read_parquet(t.sha256, ["flags"]).filter(pl.col("flags").str.contains("estimate_kind"))
            kinds |= set(df.select(pl.col("flags").str.json_path_match("$.estimate_kind")).to_series().drop_nulls())
    finally:
        be.close()
    assert not sorted(k for k in kinds if P.kind_class(k) == "unknown")
