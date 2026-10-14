"""Built-in, reviewed connector instances (code-reviewed, T3-controlled)."""

from __future__ import annotations

from typing import Any

from reva.adapters.connectors.comtrade import ComtradeConnector
from reva.adapters.connectors.crossref import CrossrefConnector
from reva.adapters.connectors.naturalearth import NaturalEarthConnector
from reva.adapters.connectors.worldbank import WorldBankConnector


def builtin_connectors(contact_email: str | None = None) -> dict[str, Any]:
    conns: list[Any] = [
        WorldBankConnector(),
        ComtradeConnector(),
        NaturalEarthConnector(),
        CrossrefConnector(mailto=contact_email),
    ]
    return {c.describe().name: c for c in conns}
