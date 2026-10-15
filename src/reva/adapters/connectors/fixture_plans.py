"""Fixture-plan mode: run a connector offline on exactly what its recorded fixtures captured.

Some V2 fixtures (``tests/fixtures/source/<name>``) were captured for a *restricted* plan (a
country subset, fewer years, one FIGARO data group), so replaying the connector's default
(all-country) plan through :class:`reva.evidence.runner.FixtureHttp` fails on the first
request that was never captured. Fixture-plan mode, used only with ``FixtureHttp``
(``scripts/e2e_stack.py --fixture-plan``, offline tests):

1. builds the connector with the configuration its fixtures were captured with
   (:data:`CAPTURED_CONFIGURATIONS`; the same configurations the integration tests use);
2. keeps only plan items *and* parser follow-up items whose request fingerprint has a
   captured response; every dropped item is recorded in ``not_captured`` (reported, never
   stubbed or silently treated as fetched).

Live behaviour is unchanged: nothing here is used by live ingestion, and the connector
classes, their default plans and the built-in registry are untouched. The connector's
name and version are those of the registered connector, so acquisitions are recorded
against the same connector version (with ``origin = test_fixture``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Reporter subset (ISO3 -> Comtrade reporter code) of tests/fixtures/source/un.comtrade.public_batch.
COMTRADE_FIXTURE_REPORTERS = {
    "FRA": 251,
    "DZA": 12,
    "CHN": 156,
    "USA": 842,
    "CHE": 757,
    "IRQ": 368,
    "NLD": 528,
    "ATG": 28,
}
#: Economy subset of tests/fixtures/source/worldbank.wdi.all_economies.
WORLDBANK_FIXTURE_ECONOMIES = ("FRA", "DZA", "CHN", "USA", "CHE", "IRQ", "NLD", "ATG", "XKX", "PRI")
#: Eurostat geo codes of tests/fixtures/source/eurostat.demography (since 2008, windowed URLs, + EUROPOP2023).
DEMOGRAPHY_FIXTURE_GEOS = ("LI", "LU")
DEMOGRAPHY_FIXTURE_SINCE = 2008
#: FIGARO year of tests/fixtures/source/eurostat.figaro.iciot_ii (content constraint + the DOM data group only).
FIGARO_FIXTURE_YEAR = 2024


def _worldbank() -> Any:
    from reva.adapters.connectors.worldbank_all import WorldBankAllConnector

    return WorldBankAllConnector(list(WORLDBANK_FIXTURE_ECONOMIES))


def _comtrade() -> Any:
    from reva.adapters.connectors.comtrade_batch import ComtradeBatchConnector

    return ComtradeBatchConnector(
        dict(COMTRADE_FIXTURE_REPORTERS), total_years=(2023,), bilateral_years=(2023,), bilateral_batch=4
    )


def _demography() -> Any:
    from reva.adapters.connectors import eurostat_demography as ed

    return ed.EurostatDemographyConnector(
        countries=DEMOGRAPHY_FIXTURE_GEOS, since_year=DEMOGRAPHY_FIXTURE_SINCE, window_years=ed.WINDOW_YEARS
    )


def _figaro() -> Any:
    from reva.adapters.connectors.io_tables import FigaroConnector

    return FigaroConnector(FIGARO_FIXTURE_YEAR)


#: connector name -> factory of the connector configured as its fixtures were captured.
CAPTURED_CONFIGURATIONS: dict[str, Callable[[], Any]] = {
    "worldbank.wdi.all_economies": _worldbank,
    "un.comtrade.public_batch": _comtrade,
    "eurostat.demography": _demography,
    "eurostat.figaro.iciot_ii": _figaro,
}


class FixturePlanConnector:
    """Delegates to ``inner``; restricts plan items and follow-ups to captured requests."""

    def __init__(self, inner: Any, captured: frozenset[str]) -> None:
        self._inner = inner
        self._captured = captured
        self.not_captured: list[dict[str, str]] = []

    def __getattr__(self, name: str) -> Any:  # describe, fetch, validate, post_publish, ...
        return getattr(self._inner, name)

    def _keep(self, item: Any, stage: str) -> bool:
        from reva.evidence.runner import _fixture_fingerprint

        if _fixture_fingerprint(item.url) in self._captured:
            return True
        self.not_captured.append({"item": item.key, "stage": stage})
        return False

    def plan(self, cursor: Any, budget: Any) -> Any:
        plan = self._inner.plan(cursor, budget)
        return plan.model_copy(update={"items": tuple(i for i in plan.items if self._keep(i, "plan"))})

    def parse(self, raw: Any) -> list[Any]:
        out = []
        for b in self._inner.parse(raw):
            follow = tuple(f for f in b.follow_up if self._keep(f, "follow_up"))
            out.append(b.model_copy(update={"follow_up": follow}) if follow != tuple(b.follow_up) else b)
        return out


def captured_fingerprints(name: str, root: Path | None = None) -> frozenset[str]:
    from reva.evidence.runner import FixtureHttp

    http = FixtureHttp(name) if root is None else FixtureHttp(name, root=root)
    return frozenset(http.entries)


def fixture_plan_connector(name: str, connector: Any = None, *, root: Path | None = None) -> FixturePlanConnector:
    """The connector ``name`` in fixture-plan mode (see module docstring).

    ``connector`` overrides the captured configuration (e.g. another fixture root's subset);
    otherwise the :data:`CAPTURED_CONFIGURATIONS` factory is used, or the built-in instance for
    connectors whose default plan was captured as is (the filter then drops nothing)."""
    if connector is None:
        factory = CAPTURED_CONFIGURATIONS.get(name)
        if factory is None:
            from reva.adapters.connectors.registry import builtin_connectors

            connector = builtin_connectors()[name]
        else:
            connector = factory()
    if connector.describe().name != name:
        raise ValueError(f"connector {connector.describe().name!r} is not {name!r}")
    return FixturePlanConnector(connector, captured_fingerprints(name, root))
