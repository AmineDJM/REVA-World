"""Energy families of the forecast registry (V6, PRED-ENERGY): STEO reference attributed, winners only, as-of."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors.vintage_common import Rows, indicator, month_bounds, vintage_flags
from reva.atlas.tables import ATLAS_SCHEMA
from reva.evaluation.registry import energy as EN
from reva.evaluation.registry import periods as P
from reva.evaluation.registry.service import build_batch
from reva.evaluation.registry.specs import parse_specs

VT = "atlas.vintage_steo.steo_archive"
OBS = "atlas.eia_steo.steo_observed"
ISSUE = 2026 * 12 + 9  # 2026-10
SPECS = parse_specs(
    {
        "schema": "reva-forecast-question-specs/1",
        "version": "t",
        "interval_level": 0.8,
        "families": [
            {
                "key": "eia.brent_monthly",
                "labels": {"fr": "Brent", "en": "Brent"},
                "dataset_key": OBS,
                "indicator": "STEO.BREPUUS.M",
                "geos": [{"provider_geo": "USA", "key": "BRENT-STEO", "label_fr": "Brent", "label_en": "Brent"}],
                "frequency": "M",
                "unit": "dollars per barrel",
                "questions": [
                    {"type": "quantity", "horizons": [1, 3]},
                    {"type": "interval", "horizons": [3]},
                ],
                "publication_lag_days": 45,
                "void_after_days": 365,
                "energy": {"code": "BREPUUS", "vintage_table": VT, "winners": "forecasts/does_not_exist.yaml"},
            }
        ],
    }
)


def _price(i: int) -> float:
    return 70.0 + 10.0 * math.sin(i / 5.0)


def _observed() -> pl.DataFrame:
    rows = []
    for idx in range(2010 * 12, ISSUE):
        rows.append(
            {
                "source": "eia_steo",
                "dataset": "steo_observed",
                "indicator": "STEO.BREPUUS.M",
                "concept_id": None,
                "iso3": "USA",
                "provider_geo": "USA",
                "entity_qid": None,
                "period_start": P.start_of("M", idx),
                "period_end": P.end_of("M", idx),
                "value": format(_price(idx), ".4f"),
                "value_text": None,
                "unit": "dollars per barrel",
                "flags": "{}",
                "raw_sha256": "a" * 64,
                "selector": "{}",
            }
        )
    return pl.DataFrame(rows, schema=ATLAS_SCHEMA)


def _vintage() -> pl.DataFrame:
    rows = Rows()
    avail = datetime(2026, 10, 7, tzinfo=UTC)
    for idx in range(2016 * 12, ISSUE + 15):
        proj = idx >= ISSUE
        p0, p1 = month_bounds(idx // 12, idx % 12 + 1)
        rows.add(
            source="vintage_steo",
            dataset="steo_archive",
            indicator=indicator("BREPUUS", "2026-10"),
            provider_geo="not_stated",
            start=p0,
            end=p1,
            value=format(_price(idx) + (5.0 if proj else 0.0), ".4f"),
            unit="dollars per barrel",
            flags=vintage_flags(
                family="eia_steo",
                vintage="2026-10",
                available_at=avail,
                basis="steo_archive_release_date",
                variable="eia_steo:BREPUUS",
                provider_variable="BREPUUS",
                estimate_kind="projection" if proj else "published_history_boundary_unknown",
                vintage_date_text="10/06/2026",
            ),
            raw_sha256="d" * 64,
            selector={"sheet": "2tab", "row": 6},
        )
    return rows.frame()


def _pin(key: str, sha: str) -> dict[str, Any]:
    return {"dataset_key": key, "dataset_id": "x", "artifact_sha256": sha, "recorded_at": "2026-10-17T10:00:00+00:00"}


def _batch(issued_at: datetime) -> dict[str, Any]:
    pins = {OBS: _pin(OBS, "1" * 64), VT: _pin(VT, "2" * 64)}
    frames = {"1" * 64: _observed(), "2" * 64: _vintage()}
    return build_batch(SPECS, pins, frames, cycle="2026-Q4", issued_at=issued_at, origin="test_fixture", already=set())


def _fc(m: dict[str, Any], model: str, period: str, qtype: str) -> dict[str, Any]:
    qid = next(q["question_id"] for q in m["questions"] if q["reference_period"] == period and q["type"] == qtype)
    return next(f for f in m["forecasts"] if f["question_id"] == qid and f["model"] == model)


def test_steo_reference_is_attributed_point_only_and_pinned() -> None:
    EN._winners_file.cache_clear()
    m = _batch(datetime(2026, 10, 17, 12, tzinfo=UTC))
    assert VT in m["data"]["tables"]
    st = _fc(m, EN.STEO_KEY, "2027-01", "quantity")
    assert st["status"] == "issued" and float(st["point"]) == pytest.approx(_price(ISSUE + 3) + 5.0)
    assert "Energy Information Administration" in st["attribution"] and "2026-10" in st["attribution"]
    assert st["source_cell"]["raw_sha256"] == "d" * 64 and st["source_cell"]["table"]["dataset_key"] == VT
    assert _fc(m, EN.STEO_KEY, "2027-01", "interval")["status"] == "abstained"
    rw = _fc(m, EN.RW_KEY, "2027-01", "quantity")
    assert rw["status"] == "issued" and float(rw["point"]) == pytest.approx(_price(ISSUE - 1), abs=1e-3)
    assert rw["origin_period"] == "2026-09" and rw["horizon_periods"] == 4
    assert dict(rw["detail"])["steo_issue"] == "2026-10"
    models = {f["model"] for f in m["forecasts"]}
    assert not any(k in models for k in ("forecast.energy_inventory", "forecast.energy_combo_steo_rw"))  # no winners


def test_only_winners_are_issued(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(EN, "winners", lambda rel: ("e" * 64, frozenset({("BREPUUS", 3, "combo_models")}), "f" * 64))
    m = _batch(datetime(2026, 10, 17, 12, tzinfo=UTC))
    c3 = _fc(m, "forecast.energy_combo_models", "2027-01", "quantity")
    # the winner is attempted at its horizon (here it abstains for its own reason: too few component models in the
    # fixture); elsewhere it abstains as "not a significant winner"
    assert "not a significant winner" not in str(c3.get("reason") or "")
    c1 = _fc(m, "forecast.energy_combo_models", "2026-11", "quantity")
    assert c1["status"] == "abstained" and "not a significant winner" in c1["reason"]


def test_eia_derived_combination_is_never_issued(monkeypatch: pytest.MonkeyPatch) -> None:
    """FIX7 (QA-PRED §3.D): the STEO + random-walk combination is excluded even if a winners file lists it."""
    monkeypatch.setattr(EN, "winners", lambda rel: ("e" * 64, frozenset({("BREPUUS", 3, "combo_steo_rw")}), "f" * 64))
    m = _batch(datetime(2026, 10, 17, 12, tzinfo=UTC))
    assert not any(f["model"] == "forecast.energy_combo_steo_rw" for f in m["forecasts"])


def test_no_issue_in_force_before_its_availability() -> None:
    EN._winners_file.cache_clear()
    m = _batch(datetime(2026, 10, 6, 20, tzinfo=UTC))  # release day: the issue is not yet admissible
    st = _fc(m, EN.STEO_KEY, "2027-01", "quantity")
    assert st["status"] == "abstained" and "no STEO issue" in st["reason"]
    assert json.dumps(m, default=str)  # manifest stays serialisable


def test_to_predictive_lognormal_quantiles() -> None:
    from reva.models.forecasting import energy_models as em

    p = EN.to_predictive(em.Pred("lognormal", math.exp(4.0), 4.0, 0.2), ())
    assert p.family == "empirical" and len(p.sample) == EN.EMPIRICAL_N
    assert p.quantile(0.5) == pytest.approx(math.exp(4.0), rel=0.01)
