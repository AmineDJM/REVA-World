"""2010-reform back-test: reproducibility, no leakage, committed evaluation artifact matches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reva.kernel.errors import ValidationProblem
from reva.models.social import assumptions as lit
from reva.models.social import backtest as bt
from reva.models.social import rules as rl

from .fixture_inputs import labour_tables

ARTIFACT = Path(__file__).resolve().parents[3] / "docs" / "dev" / "v2_pen_backtest.json"


@pytest.fixture(scope="module")
def rows() -> tuple[list[dict], list[dict]]:
    lt = labour_tables()
    return lt["lfsa_ergan"]["FRA"].to_pylist(), lt["lfsa_argan"]["FRA"].to_pylist()


def test_backtest_is_reproducible_and_matches_the_committed_artifact(rows: tuple[list[dict], list[dict]]) -> None:
    a = bt.run(*rows)
    b = bt.run(*rows)
    assert a["digest"] == b["digest"]
    committed = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert committed["digest"] == a["digest"], "docs/dev/v2_pen_backtest.json is stale: regenerate it"
    assert committed["results"] == json.loads(json.dumps(a["results"]))


def test_no_grid_estimate_overlaps_the_backtested_reform_or_period() -> None:
    points, excluded = bt.backtest_grid(lit.load_grid())
    ids = {p.point_id for p in points}
    assert "rabate_rochut_2020_fra" not in ids  # estimated on the 2010 French reform itself
    assert {e["point_id"] for e in excluded} >= {"rabate_rochut_2020_fra"}
    for p in points:
        assert p.country != bt.COUNTRY
        assert not p.overlaps(bt.COUNTRY, bt.BASE_YEAR, bt.TEST_YEARS[-1])
    assert bt.CENTRAL in ids


def test_prediction_refuses_post_2010_training_rows(rows: tuple[list[dict], list[dict]]) -> None:
    emp, act = rows
    points, _ = bt.backtest_grid(lit.load_grid())
    with pytest.raises(ValidationProblem, match="leakage"):
        bt.predict(emp, act, rl.load_rule_table("FRA"), points)


def test_predictions_do_not_depend_on_test_year_observations(rows: tuple[list[dict], list[dict]]) -> None:
    emp, act = rows
    tampered = [dict(r, value="99.9") if r["year"] > bt.BASE_YEAR else r for r in emp]
    a, b = bt.run(emp, act), bt.run(tampered, act)
    for pid in a["results"]:
        for s in ("F", "M"):
            pa = [x["predicted"] for x in a["results"][pid][s]["per_year"]]
            pb = [x["predicted"] for x in b["results"][pid][s]["per_year"]]
            assert pa == pb


def test_errors_are_reported_per_year_and_sex_with_flags(rows: tuple[list[dict], list[dict]]) -> None:
    rep = bt.run(*rows)
    central = rep["results"][bt.CENTRAL]
    for s in ("F", "M"):
        assert [x["year"] for x in central[s]["per_year"]] == list(range(2012, 2020))
        assert any(x["observed_flag"] == "b" for x in central[s]["per_year"])  # 2014 break in series
    assert "benchmark_persistence" in rep["results"] and "placebo_55_59" in rep
