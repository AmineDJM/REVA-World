"""V6 INTEG: ``reva ingest --connector eurostat.figaro.iciot_ii`` registers the FIGARO dataset input of a complete
year (as the worker ingest job does), so the next ``reva beliefs publish`` pins it for the IO price plugin."""

from __future__ import annotations

from typing import Any

import pytest

from reva.cli import main as cli


def test_figaro_dataset_registered_only_for_a_complete_year(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake(engine: Any, store: Any, *, year: int, origin: str) -> dict[str, Any]:
        calls.append({"year": year, "origin": origin})
        return {"dataset_key": f"figaro:{year}"}

    import reva.adapters.connectors.io_tables as io
    import reva.adapters.objects.factory as fac
    import reva.adapters.postgres.engine as eng

    monkeypatch.setattr(io, "publish_io_dataset", fake)
    monkeypatch.setattr(fac, "get_store", lambda: object())
    monkeypatch.setattr(eng, "get_engine", lambda role: object())
    assert cli._figaro_dataset("worldbank.wdi.indicators", {"complete": True, "year": 2024}, True) is None
    assert cli._figaro_dataset("eurostat.figaro.iciot_ii", {"complete": False, "year": 2024}, True) is None
    assert cli._figaro_dataset("eurostat.figaro.iciot_ii", {"complete": True, "year": 2024}, True) == {
        "dataset_key": "figaro:2024"
    }
    assert calls == [{"year": 2024, "origin": "live"}]
    cli._figaro_dataset("eurostat.figaro.iciot_ii", {"complete": True, "year": "2024"}, False)
    assert calls[-1] == {"year": 2024, "origin": "test_fixture"}
