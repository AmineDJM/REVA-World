"""INSEE BDM (Banque de données macroéconomiques) — French overseas departments gap fill (V3, GAPHUNT).

Source: the INSEE BDM SDMX 2.1 web service ``https://bdm.insee.fr/series/sdmx`` (open, no key, GET).
Licence: Licence Ouverte / Open Licence 2.0 (Etalab), the licence of INSEE's statistical data.
Two requests, two atlas tables (``source = "insee"``):

``ipc_dom``  dataflow ``IPC-2025``, annual all-items consumer price index (base 2025, all
             households) and its **annual variation** ("Indice annuel des prix à la consommation …
             Variation annuelle", the change of the annual average index) for Guadeloupe (D971),
             Martinique (D972), Guyane (D973), La Réunion (D974), Mayotte (D976). France totals
             (FE, FM) are left out (the IMF/World Bank report FRA).
``smic_dom`` series 000822501 "Montant du Smic … brut horaire dans les départements d'outre-mer (hors
             Mayotte)" and 011800491 "… brut horaire à Mayotte" (euros per hour, monthly cells).

Geography: REF_AREA ``D971``… map through scheme ``insee_gaphunt`` (``geo_aliases/gaphunt.yaml``);
each series title must name the expected department (checked). The Smic series for the group "DOM"
is published once for "départements d'outre-mer (hors Mayotte)": its rows are written once per
department that the title covers (GLP, MTQ, GUF, REU — the overseas departments other than
Mayotte), keeping ``provider_geo = "DOM"``, the indicator suffix ``|applies_to=<dept>`` and
``flags.applies_to_rule`` quoting the title. Nothing is computed: the hourly Smic is not turned into
a monthly wage (151.67 h × rate would be a REVA computation).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree as ET

from reva.adapters.connectors import atlas_gap_common as common
from reva.adapters.connectors.atlas_gap_common import Fetched, GapItem, GapTable, canon, strict_decimal
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

NAME = "atlas.gap_insee"
SOURCE_KEY = "insee.bdm"
HOST = "bdm.insee.fr"
BASE = f"https://{HOST}/series/sdmx/data"
ATLAS_SOURCE = "insee"
GEO_SCHEME = "insee_gaphunt"
PARSER_VERSION = "atlas_gap_insee/1"
RIGHTS = {
    "licence": "Licence Ouverte / Open Licence 2.0 (Etalab): Insee legal notice, unless stated otherwise; attribution 'Source : Insee' + last update date",
    "licence_url": "https://www.insee.fr/fr/information/2008466",
    "attribution": "Source: Insee, Banque de données macroéconomiques (BDM)",
}
IPC_URL = f"{BASE}/IPC-2025/A...00........"
SMIC_IDBANKS = ("000822501", "011800491")
SMIC_URL = f"{BASE}/SERIES_BDM/{'+'.join(SMIC_IDBANKS)}"
#: REF_AREA -> word that the series title must contain
DEPARTMENTS = {"D971": "Guadeloupe", "D972": "Martinique", "D973": "Guyane", "D974": "réunion", "D976": "Mayotte"}
#: the Smic group "DOM" ("départements d'outre-mer (hors Mayotte)") and the departments it names
DOM_EXCEPT_MAYOTTE = ("D971", "D972", "D973", "D974")
IPC_NATURES = {"INDICE": ("CPI", "index (2025 = 100)"), "VARIATIONS_A": ("CPI_ANNUAL_CHANGE", "% change")}


def items(*, fixture: bool = False) -> list[GapItem]:
    mt = ("application/xml", "application/vnd.sdmx.structurespecificdata+xml", "text/xml")
    return [
        GapItem("data:IPC-2025", IPC_URL, ("ipc_dom",), mt, max_bytes=10_000_000),
        GapItem("data:SMIC", SMIC_URL, ("smic_dom",), mt, max_bytes=10_000_000),
    ]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_series(body: bytes, what: str) -> list[tuple[dict[str, str], list[dict[str, str]]]]:
    """SDMX 2.1 structure-specific data -> [(series attributes, [obs attributes])]."""
    if b"<!DOCTYPE" in body[:4096].upper() or b"<!ENTITY" in body.upper():
        raise SourceResponseError(f"{what}: DTD/entity declaration refused", retryable=False)
    try:
        root = ET.fromstring(body)  # noqa: S314 - DTD/entities refused above; expat fetches nothing
    except ET.ParseError as e:
        raise SourceResponseError(f"{what}: not well-formed XML", retryable=False) from e
    if _local(root.tag) == "Error":
        raise SourceResponseError(f"{what}: provider error message", retryable=False)
    out = []
    for el in root.iter():
        if _local(el.tag) == "Series":
            obs = [dict(o.attrib) for o in el if _local(o.tag) == "Obs"]
            out.append((dict(el.attrib), obs))
    if not out:
        raise SourceResponseError(f"{what}: no series", retryable=False)
    return out


def _obs_value(o: Mapping[str, str]) -> str | None:
    """OBS_STATUS ``O`` (missing) cells carry ``OBS_VALUE="NaN"``: no value, omitted (never a number)."""
    if o.get("OBS_STATUS") == "O" and o.get("OBS_VALUE", "NaN") in ("NaN", ""):
        return None
    return strict_decimal(o.get("OBS_VALUE"))


def _obs_flags(o: Mapping[str, str]) -> dict[str, Any]:
    return {f"insee_{k.lower()}": v for k, v in o.items() if k in ("OBS_STATUS", "OBS_QUAL", "OBS_TYPE") and v}


def parse_ipc(f: Fetched) -> list[dict[str, Any]]:
    rows = []
    for attrs, obs in read_series(f.body, "INSEE IPC-2025"):
        area, nature = attrs.get("REF_AREA", ""), attrs.get("NATURE", "")
        if area not in DEPARTMENTS or nature not in IPC_NATURES or attrs.get("MENAGES_IPC") != "ENSEMBLE":
            continue
        title = attrs.get("TITLE_FR", "")
        if DEPARTMENTS[area].lower() not in title.lower():
            raise SourceResponseError(
                "INSEE IPC: series title does not name its department", area=area, retryable=False
            )
        code, unit = IPC_NATURES[nature]
        for o in obs:
            value = _obs_value(o)
            if value is None:
                continue
            start, end, kind = common.period_of(o.get("TIME_PERIOD", ""))
            if kind != "year":
                raise SourceResponseError("INSEE IPC: non-annual observation", retryable=False)
            flags = {
                "idbank": attrs.get("IDBANK", ""),
                "title_fr": title,
                "title_en": attrs.get("TITLE_EN", ""),
                "base": "2025 = 100",
                "households": "ENSEMBLE (all households)",
                "last_update": attrs.get("LAST_UPDATE", ""),
                **_obs_flags(o),
            }
            rows.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": "ipc_dom",
                    "indicator": f"{code}|coicop=00|households=ENSEMBLE|base=2025",
                    "iso3": country_iso3(area, GEO_SCHEME),
                    "provider_geo": area,
                    "period_start": start,
                    "period_end": end,
                    "value": value,
                    "unit": unit,
                    "flags": canon(flags),
                    "raw_sha256": f.sha256,
                    "selector": canon(
                        {"dataflow": "IPC-2025", "idbank": attrs.get("IDBANK", ""), "time": o.get("TIME_PERIOD")}
                    ),
                }
            )
    common.dedupe_check(rows, "INSEE IPC")
    return rows


def parse_smic(f: Fetched) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for attrs, obs in read_series(f.body, "INSEE SMIC"):
        idbank, area, title = attrs.get("IDBANK", ""), attrs.get("REF_AREA", ""), attrs.get("TITLE_FR", "")
        seen.add(idbank)
        if idbank == "000822501":
            if area != "DOM" or "hors Mayotte" not in title:
                raise SourceResponseError("INSEE SMIC: DOM series changed scope", area=area, retryable=False)
            targets = [(d, "DOM", f"|applies_to={d}") for d in DOM_EXCEPT_MAYOTTE]
        elif idbank == "011800491":
            if area != "976" or "Mayotte" not in title:
                raise SourceResponseError("INSEE SMIC: Mayotte series changed scope", area=area, retryable=False)
            targets = [("976", "976", "")]
        else:
            raise SourceResponseError("INSEE SMIC: unexpected series", idbank=idbank[:12], retryable=False)
        if attrs.get("UNIT_MEASURE") not in ("EUROS", None):
            raise SourceResponseError("INSEE SMIC: unit is not euros", retryable=False)
        for o in obs:
            value = _obs_value(o)
            if value is None:
                continue
            start, end, kind = common.period_of(o.get("TIME_PERIOD", ""))
            for geo_code, provider_geo, suffix in targets:
                flags: dict[str, Any] = {
                    "idbank": idbank,
                    "title_fr": title,
                    "title_en": attrs.get("TITLE_EN", ""),
                    "pay": "gross statutory minimum wage (Smic), per hour",
                    "period_basis": f"monthly ({kind}) value in force",
                    "last_update": attrs.get("LAST_UPDATE", ""),
                    **_obs_flags(o),
                }
                if provider_geo == "DOM":
                    flags["applies_to_rule"] = (
                        "INSEE publishes one Smic for 'départements d'outre-mer (hors Mayotte)'; written once per "
                        "overseas department other than Mayotte (Guadeloupe, Martinique, Guyane, La Réunion) by agent "
                        "GAPHUNT, maintainer review pending"
                    )
                rows.append(
                    {
                        "source": ATLAS_SOURCE,
                        "dataset": "smic_dom",
                        "indicator": f"SMIC_HOURLY_GROSS{suffix}",
                        "iso3": country_iso3(geo_code, GEO_SCHEME),
                        "provider_geo": provider_geo,
                        "period_start": start,
                        "period_end": end,
                        "value": value,
                        "unit": "EUR per hour",
                        "flags": canon(flags),
                        "raw_sha256": f.sha256,
                        "selector": canon({"idbank": idbank, "time": o.get("TIME_PERIOD")}),
                    }
                )
    if seen != set(SMIC_IDBANKS):
        raise SourceResponseError("INSEE SMIC: a requested series is missing", got=sorted(seen), retryable=False)
    common.dedupe_check(rows, "INSEE SMIC")
    return rows


def build(got: Mapping[str, Fetched]) -> list[GapTable]:
    out = []
    f = got.get("data:IPC-2025")
    if f is not None:
        rows = parse_ipc(f)
        note = f"INSEE BDM IPC-2025 annual all-items CPI and annual variation, departments 971-974, 976 ({len(rows)} cells)."
        out.append(GapTable("ipc_dom", common.frame(rows), (f.sha256,), GEO_SCHEME, note, RIGHTS))
    f = got.get("data:SMIC")
    if f is not None:
        rows = parse_smic(f)
        note = (
            f"INSEE BDM Smic gross hourly amounts: series 000822501 (DOM except Mayotte, written per department) and "
            f"011800491 (Mayotte); {len(rows)} monthly cells. Hourly euros, not converted to a monthly wage."
        )
        out.append(GapTable("smic_dom", common.frame(rows), (f.sha256,), None, note, RIGHTS))
    return out


def _module() -> Any:
    import sys

    return sys.modules[__name__]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version="1.0.0",
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Insee (Institut national de la statistique et des études économiques)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']})",
        allowed_hosts=(HOST,),
        datasets=("ipc_dom", "smic_dom"),
        dataset_ids=("FR1:IPC-2025", "SERIES_BDM:000822501", "SERIES_BDM:011800491"),
        cadence_seconds=30 * 86400,
        planned_requests=2,
        max_response_bytes=10_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(_module()),
        field_mappings={"geo_scheme": GEO_SCHEME},
        notes="CPI and Smic of the French overseas departments.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await common.ingest_items(run, items(fixture=run.origin == "test_fixture"), build)


__all__ = ["NAME", "atlas_ingest", "atlas_source", "build", "items", "parse_ipc", "parse_smic", "read_series"]
