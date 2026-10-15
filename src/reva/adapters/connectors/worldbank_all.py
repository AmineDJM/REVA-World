"""World Bank WDI connector for every crosswalk economy (V2 step 1).

Same indicators (SP.POP.TOTL, NY.GDP.MKTP.CD), logical keys, supports, values
and flags as the reviewed ``worldbank.wdi.indicators`` connector, so the four
reviewed countries share one source lineage with it. Scope: every World Bank
economy of ``registry/entities/country_crosswalk.json`` (aggregates -- regions,
income and lending groups -- are excluded there and never requested), years
2019-2024.

Requests: the economies are listed explicitly in the path (semicolon list;
the API accepted all 217 in one request on 2026-10-15) with ``per_page`` large
enough for a single page per indicator, so the page's expected-key set covers
every requested (economy, year) and an absent row blocks as ``incomplete``.
Country metadata is requested with ``per_page=400`` (one page). Six requests per
run; well inside the ``worldbank.wdi`` daily budget.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterator, Sequence

from reva.adapters.connectors import worldbank as base
from reva.adapters.connectors.worldbank import BASE, INDICATORS, WorldBankConnector
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchPlan,
    ObservationBatch,
    RawAcquisition,
)

CONNECTOR_NAME = "worldbank.wdi.all_economies"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "wb-all-parse-1"
YEARS = (2019, 2024)


def _code_digest() -> str:
    h = hashlib.sha256()
    for mod in (base, inspect.getmodule(_code_digest)):
        h.update(inspect.getsource(mod).encode())  # type: ignore[arg-type]
    return "sha256:" + h.hexdigest()


class WorldBankAllConnector(WorldBankConnector):
    def __init__(self, countries: Sequence[str] | None = None, years: tuple[int, int] = YEARS) -> None:
        if countries is None:
            from reva.adapters.connectors.country_crosswalk import worldbank_economies

            countries = sorted(set(worldbank_economies().values()))
        countries = tuple(sorted(countries))
        per_page = max(1000, len(countries) * (years[1] - years[0] + 1) + 100)
        super().__init__(countries=countries, years=years, per_page=per_page)

    def describe(self) -> ConnectorSpec:
        spec = super().describe()
        return spec.model_copy(update={
            "name": CONNECTOR_NAME,
            "version": CONNECTOR_VERSION,
            "geography": self.countries,
            "parser_version": PARSER_VERSION,
            "code_digest": _code_digest(),
        })

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        items = [FetchItem(key="countries",
                           url=f"{BASE}/country/{';'.join(self.countries)}?format=json&per_page=400",
                           max_bytes=2_000_000, params={"kind": "country_metadata"})]
        for ind in INDICATORS:
            items.append(FetchItem(key=f"indicator:{ind}", url=f"{BASE}/indicator/{ind}?format=json",
                                   max_bytes=500_000, params={"kind": "indicator_metadata", "indicator": ind}))
            items.append(FetchItem(key=f"series:{ind}:p1", url=self._series_url(ind, 1), max_bytes=5_000_000,
                                   params={"kind": "series", "indicator": ind, "page": 1,
                                           "depends_on": f"indicator:{ind}"}))
        return FetchPlan(
            connector=CONNECTOR_NAME,
            items=tuple(items[: budget.max_requests]),
            pagination="page/pages header; per_page sized for one page; follow-up pages planned if the provider pages",
            availability_expectation="annual updates; no per-value publication timestamp",
            budget_exhausted=len(items) > budget.max_requests,
        )

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        for b in super().parse(raw):
            meta = dict(b.metadata)
            if meta.get("kind") == "country_metadata":
                header = meta.get("page_header") or {}
                meta["requested_countries"] = len(self.countries)
                meta["paged"] = str(header.get("pages", "1")) != "1"
            yield b.model_copy(update={"connector": CONNECTOR_NAME, "connector_version": CONNECTOR_VERSION,
                                       "metadata": meta})


__all__ = ["CONNECTOR_NAME", "CONNECTOR_VERSION", "WorldBankAllConnector"]
