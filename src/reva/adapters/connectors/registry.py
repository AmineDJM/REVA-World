"""Built-in, reviewed connector instances (code-reviewed, T3-controlled)."""

from __future__ import annotations

from typing import Any

from reva.adapters.connectors.comtrade import ComtradeConnector
from reva.adapters.connectors.comtrade_batch import ComtradeBatchConnector
from reva.adapters.connectors.crossref import CrossrefConnector
from reva.adapters.connectors.eurostat_demography import WINDOW_YEARS as EUROSTAT_DEMOGRAPHY_WINDOW_YEARS
from reva.adapters.connectors.eurostat_demography import EurostatDemographyConnector
from reva.adapters.connectors.eurostat_labour import EurostatLabourPensionsConnector
from reva.adapters.connectors.ilostat_labour import IlostatLabourConnector
from reva.adapters.connectors.ilostat_social_protection import IlostatSocialProtectionConnector
from reva.adapters.connectors.io_tables import FigaroConnector
from reva.adapters.connectors.naturalearth import NaturalEarthConnector
from reva.adapters.connectors.un_wpp_demography import UnWppDemographyConnector
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
        # V3 SIMWORLD: every-country inputs of demography.cohort_component@1.1.0 and
        # social.retirement_age@1.1.0 (WPP 2024 single-age demography; ILOSTAT rates by age band)
        UnWppDemographyConnector(),
        IlostatLabourConnector(),
        # V3 SIMFIX: ILOSTAT SDG 1.3.1 old-age pension coverage by sex (social.retirement_age@1.2.0)
        IlostatSocialProtectionConnector(),
    ]
    return {c.describe().name: c for c in conns}
