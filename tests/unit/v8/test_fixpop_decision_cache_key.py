"""V8 decision studio cache key covers the engine/plugin code, not only the data (V6 FIXPOP).

The bundle cache key is ``scenario_hash:data_fingerprint``. After the retirement start-date fix the
same scenario on the same data gives a different result, so the scenario hash must change with the
code and registry files that compute it (``code_fingerprint``)."""

from __future__ import annotations

import pytest

from reva.predict.decision import reference_decisions
from reva.v8.decision import runner as rn


def test_scenario_hash_changes_with_code_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    d = reference_decisions()["france_retirement_65_2027"]
    kw = {"draws": 2000, "seed": 0, "as_of_max": None, "origin": "live"}
    h1 = rn.scenario_hash(d, **kw)
    assert h1 == rn.scenario_hash(d, **kw)  # stable for the same code
    monkeypatch.setattr(rn, "code_fingerprint", lambda: "other-code")
    assert rn.scenario_hash(d, **kw) != h1


def test_code_fingerprint_covers_engine_plugin_and_reference_registry() -> None:
    files = {str(f) for d, g in rn.CODE_FILES_GLOBS if d.is_dir() for f in d.glob(g)}
    for name in (
        "predict/engine.py",
        "predict/propagators.py",
        "models/social/retirement_age.py",
        "registry/predict/reference_decisions.yaml",
        "registry/rules/retirement_age/FRA.yaml",
    ):
        assert any(f.endswith(name) for f in files), name
    assert len(rn.code_fingerprint()) == 16
