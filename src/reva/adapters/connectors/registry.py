"""Built-in, reviewed connector instances (code-reviewed, T3-controlled)."""

from __future__ import annotations

from typing import Any

from reva.adapters.connectors.comtrade import ComtradeConnector
from reva.adapters.connectors.comtrade_batch import ComtradeBatchConnector
from reva.adapters.connectors.crossref import CrossrefConnector
from reva.adapters.connectors.eurostat_demography import WINDOW_YEARS as EUROSTAT_DEMOGRAPHY_WINDOW_YEARS
from reva.adapters.connectors.eurostat_demography import EurostatDemographyConnector
from reva.adapters.connectors.eurostat_labour import EurostatLabourPensionsConnector
from reva.adapters.connectors.io_tables import FigaroConnector
from reva.adapters.connectors.naturalearth import NaturalEarthConnector
from reva.adapters.connectors.worldbank import WorldBankConnector
from reva.adapters.connectors.worldbank_all import WorldBankAllConnector


def builtin_connectors(contact_email: str | None = None) -> dict[str, Any]:
    conns: list[Any] = [
        WorldBankConnector(),
        ComtradeConnector(),
        NaturalEarthConnector(),
        CrossrefConnector(mailto=contact_email),
        # V2 step 1: every crosswalk country (registry/entities/country_crosswalk.json)
        WorldBankAllConnector(),
        ComtradeBatchConnector(),
        # V2 steps 4-5 (agent PEN): LFS rates, ESSPROS pensions, household margins
        EurostatLabourPensionsConnector(),
        # V2 step 3a (agent POP): demo_pjan/demo_magec/demo_fasec + EUROPOP2023 assumption tables
        EurostatDemographyConnector(window_years=EUROSTAT_DEMOGRAPHY_WINDOW_YEARS),
        # V2 step 3b (agent IO): FIGARO inter-country input-output table (latest year)
        FigaroConnector(),
    ]
    return {c.describe().name: c for c in conns}
