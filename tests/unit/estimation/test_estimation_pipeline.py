"""Estimation pipeline without a database: read-only snapshot panels (best-available rule, refusals), atlas-coded
treatments, a full run on a synthetic panel with a known effect (recovered, deterministic run id, as-of), the
REVA-run effect estimate validated by PRED-EFFECT's pure validator, and the replication helpers."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from reva.estimation import replication as rp
from reva.estimation.data import SnapshotNotMaterialised, SnapshotPanels, atlas_onsets, load_treatments
from reva.estimation.run import EventStudyRequest, effect_estimates, run_event_study
from reva.kernel.errors import IntegrityFailure, ValidationProblem

FAKE_SNAPSHOT = "ab" * 32  # = conftest.FAKE_SNAPSHOT (the fixture file's snapshot)

GDP = ("wb", "wdi_econ", "NY.GDP.MKTP.KD.ZG")
GDP2 = ("wb", "gender", "NY.GDP.MKTP.KD.ZG")
JST = ("jst", "macrohistory", "crisisJST")


def _isos(n: int) -> list[str]:
    from reva.kg.query import country_qid_registry

    return sorted(country_qid_registry())[:n]


def synthetic(n_c: int = 60, years: range = range(1960, 2011), effect: float = -3.0, seed: int = 0):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(seed)
    isos = _isos(n_c)
    growth: list[tuple[str, int, str | None]] = []
    crises: list[tuple[str, int, str | None]] = []
    for c in isos:
        base = rng.normal(3, 1)
        onset = {int(y) for y in rng.choice(list(years)[3:-6], size=2, replace=False)}
        for y in years:
            g = base + rng.normal(0, 1) + (effect if y in onset else 0.0) + (effect / 2 if (y - 1) in onset else 0.0)
            growth.append((c, y, f"{g:.6f}"))
            crises.append((c, y, "1" if y in onset else "0"))
    return {GDP: growth, JST: crises}


def test_concept_panel_best_available_and_refusals(atlas_file: Callable[..., Path], tmp_path: Path) -> None:
    d = atlas_file({GDP: [("FRA", 2000, "1.0"), ("FRA", 2002, "n.a.")], GDP2: [("FRA", 2000, "9"), ("FRA", 2001, "2.0")]})
    with SnapshotPanels(FAKE_SNAPSHOT, d) as p:
        panel = p.concept("wb.ny.gdp.mktp.kd.zg")
        assert panel.values == {("FRA", 2000): 1.0, ("FRA", 2001): 2.0}
        assert panel.dropped["non_numeric"] == 1
        assert {s["series"] for s in panel.series if s.get("cells")} == {".".join(GDP), ".".join(GDP2)}
        assert len(panel.digest) == 64
        assert p.describe()["origin"] == "test_fixture"
        with pytest.raises(ValidationProblem):
            p.outcome("nonsense")
    with pytest.raises(SnapshotNotMaterialised):
        SnapshotPanels("cd" * 32, d)
    side = d / f"atlas-{FAKE_SNAPSHOT}-v6.duckdb.sha256"
    rec = json.loads(side.read_text())
    side.write_text(json.dumps({**rec, "sha256": "0" * 64}))
    with pytest.raises(IntegrityFailure):
        SnapshotPanels(FAKE_SNAPSHOT, d, verify_bytes=True)
    assert (d / f"atlas-{FAKE_SNAPSHOT}-v6.duckdb").exists()  # refused, never deleted


def test_quarterly_series(atlas_file: Callable[..., Path]) -> None:
    key = ("bis", "spp", "Q:FR:R:628")
    d = atlas_file({}, quarterly={key: [("FRA", 2020, 1, "100"), ("FRA", 2020, 2, "101.5")]})
    with SnapshotPanels(FAKE_SNAPSHOT, d) as p:
        q = p.outcome("series:bis.spp.Q:FR:R:628", "quarter")
    assert q.values == {("FRA", 4 * 2020): 100.0, ("FRA", 4 * 2020 + 1): 101.5}


def test_atlas_treatment_onsets(atlas_file: Callable[..., Path]) -> None:
    d = atlas_file({JST: [("USA", 2007, "1"), ("USA", 2008, "0"), ("GBR", 1860, "1")]})
    t = load_treatments()["banking_crisis_jst"]
    with SnapshotPanels(FAKE_SNAPSHOT, d) as p:
        ev = atlas_onsets(p, t)
    assert [(o.iso3, o.year) for o in ev.onsets] == [("USA", 2007)]  # 1860 is outside the coverage 1870-2020
    assert len(ev.digest) == 64


def test_run_recovers_effect_and_is_reproducible(atlas_file: Callable[..., Path]) -> None:
    d = atlas_file(synthetic())
    req = EventStudyRequest(treatment="banking_crisis_jst", outcome="concept:wb.ny.gdp.mktp.kd.zg", horizons=3, n_perm=20)
    with SnapshotPanels(FAKE_SNAPSHOT, d) as p:
        doc = run_event_study(req, p)
        again = run_event_study(req, p)
        other = run_event_study(EventStudyRequest(**{**req.__dict__, "seed": 1}), p)
        late = run_event_study(EventStudyRequest(**{**req.__dict__, "as_of": date(1990, 6, 30)}), p)
    irf = doc["results"]["local_projection"]["irf"]
    truth = [-3.0, -4.5, -4.5, -4.5]
    for e in irf:
        assert abs(e["point"] - truth[e["h"]]) < max(0.8, 3 * e["se"])
    assert doc["run_id"] == again["run_id"] and doc["result_sha256"] == again["result_sha256"]
    assert other["run_id"] != doc["run_id"]
    assert doc["inputs"]["vintage_mode"] == "final_vintage_pseudo_real_time"
    assert any("not established causation" in c for c in doc["caveats"])
    assert doc["rights"]["non_commercial"]  # JST treatment: CC BY-NC-SA
    assert late["results"]["local_projection"]["sample"]["years"][1] <= 1989
    # the REVA-run effect estimate is valid for PRED-EFFECT's loader (pure validation, no database)
    fx = pytest.importorskip("reva.effects")
    from reva.estimation.run import SOURCE_KEY, build_effect_inputs

    est = effect_estimates(doc)[0]
    assert est["method"] == "local_projection" and est["cause_id"] == "banking_crisis"
    for e in build_effect_inputs(doc):
        row = fx.validate_estimate(e, source_kind="reva_run", source_key=SOURCE_KEY)
        assert row["estimate_key"] == f"run:{doc['run_id']}:h3"


def test_effect_estimates_need_concept_outcome(atlas_file: Callable[..., Path]) -> None:
    d = atlas_file(synthetic(n_c=30))
    req = EventStudyRequest(treatment="banking_crisis_jst", outcome="series:wb.wdi_econ.NY.GDP.MKTP.KD.ZG", horizons=1, n_perm=0)
    with SnapshotPanels(FAKE_SNAPSHOT, d) as p:
        doc = run_event_study(req, p)
    with pytest.raises(ValidationProblem):
        effect_estimates(doc)


def test_request_validation() -> None:
    with pytest.raises(ValidationProblem):
        EventStudyRequest(treatment="x", outcome="y", horizons=50).validate()
    with pytest.raises(ValidationProblem):
        EventStudyRequest(treatment="x", outcome="y", lhs="nope").validate()  # type: ignore[arg-type]


# --------------------------------------------------------------------------- replication helpers
def test_jst_exclusion_rule_reproduces_published_counts() -> None:
    t = rp.load_targets()["jst2013_table4"]
    n = {"N": 0, "F": 0}
    total = 0
    for d in t["peaks"].values():
        for k in ("N", "F"):
            total += len(d[k])
            n[k] += sum(1 for p in d[k] if not rp.jst_excluded(p))
    assert total == 298  # "There are 298 peaks identified in this table"
    assert n == {"N": 173, "F": 50}


def test_lv_loss_known_series() -> None:
    # 2 % trend growth for 20 years, then a 10 % level drop that persists for 4 years
    s = {1980 + i: 100 * 1.02**i for i in range(20)}
    for k in range(4):
        s[2000 + k] = 100 * 1.02 ** (20 + k) * 0.9
    loss = rp.lv_loss(s, 2000)
    assert loss == pytest.approx(40.0, abs=0.5)  # 4 years x 10 % of trend
    s_up = dict(s)
    for k in range(4):
        s_up[2000 + k] = 100 * 1.02 ** (20 + k) * 1.05
    assert rp.lv_loss(s_up, 2000) == 0.0  # truncated at zero
    assert rp.lv_loss({2000: 1.0}, 2000) is None


def test_recession_episodes() -> None:
    g = {("AAA", 1960): 1.0, ("AAA", 1961): -2.0, ("AAA", 1962): -1.0, ("AAA", 1963): 3.0, ("AAA", 2001): -4.0}
    eps = rp.recession_episodes(g, (1960, 2001))
    assert [(e["start"], e["years"], e["loss"]) for e in eps] == [(1961, 2, -3.0), (2001, 1, -4.0)]


def test_jst_table4_on_synthetic_constant_growth() -> None:
    t = rp.load_targets()["jst2013_table4"]
    from reva.estimation.data import PanelData

    values = {(c, y): 100 * math.exp(0.01 * (y - 1870)) for c in t["peaks"] for y in range(1868, 2015)}
    panel = PanelData("series:x", FAKE_SNAPSHOT, values, "index", [], {}, "0" * 64)
    out = rp.jst_table4(t, panel)
    assert out["counts_match"]
    assert all(c["reva"] == pytest.approx(c["h"] * 1.0, abs=1e-6) for c in out["comparisons"])
    assert out["verdict"] == "FAIL"  # a flat 1 %/yr path does not reproduce the published table


def test_gate_refuses_without_three_passes() -> None:
    with pytest.raises(ValidationProblem):
        rp.gate({"gate_open": False, "passed": ["a"]})
    rp.gate({"gate_open": True, "passed": ["a", "b", "c"]})
