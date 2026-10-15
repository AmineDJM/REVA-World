"""IMF atlas connector (V3, agent A-IMF): WEO, BOP, IMTS (ex-DOTS), GFS, ER / MFS_IR / IL (ex-IFS).

Source: the IMF data portal SDMX 2.1 REST API, ``https://api.imf.org/external/sdmx/2.1`` (open, no
key; researched 2026-10-15). The legacy ``data.imf.org`` bulk services and the IFS / DOTS datasets
were retired by the IMF in 2025; their successors on the new portal are used here:

=========  ===================  =====================================================================
dataset    IMF dataflow         content (annual, ``FREQUENCY=A``)
=========  ===================  =====================================================================
weo        IMF.RES:WEO          World Economic Outlook, latest vintage (``WEO`` = the current vintage;
                                Oct 2026 = 9.0.0, PUBLICATION_DATE 2026-10-13): GDP, growth, inflation,
                                unemployment, population, current account, general government
bop        IMF.STA:BOP          Balance of Payments (BPM6): current account, goods, services, goods &
                                services, primary/secondary income; credit, debit and net; US dollars
gfs_soo    IMF.STA:GFS_SOO      GFS Statement of Operations headlines (revenue, expense, expenditure,
                                net operating balance, net lending/borrowing) for general (S13) and
                                budgetary central (S1311) government, percent of GDP
er         IMF.STA:ER           exchange rates, national currency per US dollar (period average, end of
                                period) -- the IFS exchange-rate series (``IFS_FLAG``)
mfs_ir     IMF.STA:MFS_IR       interest rates: monetary-policy related, discount, money market,
                                deposit, lending, Treasury bill, government bond -- ex-IFS
il         IMF.STA:IL           international liquidity: total reserves (gold at market value),
                                reserves excluding gold, gold at market value; US dollars -- ex-IFS
imts       IMF.STA:IMTS         International Trade in Goods by partner country (successor of the
                                Direction of Trade Statistics): exports FOB / imports CIF, US dollars,
                                reporter x partner x year
=========  ===================  =====================================================================

Honesty rules implemented here:

* Values are what the IMF reports: ``OBS_VALUE`` is in **full units** (US dollars, persons, national
  currency); the series ``SCALE`` attribute is only the IMF's display scale and is kept in
  ``flags.display_scale`` -- values are never rescaled. Absent ``OBS_VALUE`` -> no row (counted).
* WEO projections: per (country, indicator) the WEO declares ``LATEST_ACTUAL_ANNUAL_DATA`` (the
  "estimates start after" column of the old bulk files). Every year after it carries
  ``flags.estimate_kind = "projection"``. Fiscal-year labels (``FY2024/25``) are mapped to the WEO
  calendar label with the mapping the series' own ``METHODOLOGY_NOTES`` state (``FY(t/t+1) = CY(t)``
  or ``FY(t-1/t) = CY(t)``); when no mapping is stated the *earlier* year is used (conservative: a
  value may be flagged projection that is not, never the reverse). Series without a declared
  boundary (the PPP series PPPGDP/PPPPC/PPPEX carry none) are flagged ``projection`` after the same
  country's NGDP boundary or from the publication year on, and ``unknown_boundary`` before.
* Non-WEO datasets: ``DERIVATION_TYPE`` (SP = staff projection, SE* = staff estimate…) and ``STATUS``
  (F forecast, E/e estimate, P provisional, B break…) are carried in flags; SP/F become
  ``estimate_kind = "projection"``, staff estimates/E ``"estimate"``.
* Provider geographies map through ``registry/atlas/geo_aliases/a_imf.yaml`` (scheme ``imf`` = ISO3
  plus IMF ``KOS`` -> XKX and ``WBG`` -> PSE). Aggregates (G001 World, regions…), Taiwan Province of
  China (TWN), Curaçao and Sint Maarten (CWX) and historical areas (SUN, YUG, CSK…) stay unmapped
  (kept, listed). IMTS partners are folded into the indicator (``XG_FOB_USD|partner=DEU``) and mapped
  into ``flags.partner_iso3`` (null when unmapped; the IMF code is always kept).

Atlas source platform (``docs/dev/v3_atlas_interface.md`` section 7): :func:`atlas_source` declares
the source; :func:`atlas_ingest` runs inside :func:`reva.atlas.ingest.run_atlas_ingest`, which meters
every request against the ``imf.sdmx`` budget. This module never builds an HTTP client: it fetches
with ``run.get``, archives raw bytes with ``run.archive`` and registers tables with ``run.register``.

    uv run reva atlas ingest atlas.imf [--dataset weo] [--fixture]
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import polars as pl

from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem, RawAcquisition

CONNECTOR_NAME = "atlas.imf"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_imf/1"
SOURCE = "imf"
SOURCE_KEY = "imf.sdmx"
HOST = "api.imf.org"
BASE = f"https://{HOST}/external/sdmx/2.1"
ACCEPT = "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
GEO_SCHEME = "imf"
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
LICENCE = (
    "IMF Copyright and Usage terms (https://www.imf.org/external/terms.htm); provider LICENSE attribute: "
    "'(c) International Monetary Fund Copyright. All Rights Reserved.' Cite: International Monetary Fund, "
    "<dataset>, https://data.imf.org/en/datasets/<agency>:<flow>"
)

# --------------------------------------------------------------------------- dataset definitions
#: WEO indicators: code -> (unit as reported, catalog concept hint). Units are the IMF's own
#: (SERIES_NAME / UNIT attribute); OBS_VALUE is in full units (see module docstring).
WEO_INDICATORS: dict[str, tuple[str, str | None]] = {
    "NGDP": ("national currency (current prices)", "imf.gdp_current_lcu"),
    "NGDPD": ("US dollars (current prices)", "imf.gdp_current_usd"),
    "NGDP_RPCH": ("% change (constant prices)", "imf.gdp_real_growth"),
    "NGDPDPC": ("US dollars per person (current prices)", "imf.gdp_per_capita_usd"),
    "PPPGDP": ("international dollars (PPP, current prices)", "imf.gdp_ppp"),
    "PPPPC": ("international dollars per person (PPP, current prices)", "imf.gdp_per_capita_ppp"),
    "PPPEX": ("national currency per international dollar", "imf.ppp_conversion_rate"),
    "NGDP_D": ("index (GDP deflator)", "imf.gdp_deflator_index"),
    "NGSD_NGDP": ("% of GDP", "imf.gross_national_savings_gdp"),
    "NID_NGDP": ("% of GDP", "imf.gross_capital_formation_gdp"),
    "PCPI": ("index (CPI, period average)", "imf.cpi_index"),
    "PCPIPCH": ("% change (CPI, period average)", "imf.inflation_cpi_avg"),
    "PCPIEPCH": ("% change (CPI, end of period)", "imf.inflation_cpi_eop"),
    "LUR": ("% of labour force", "imf.unemployment_rate"),
    "LE": ("persons (employed)", "imf.employment"),
    "LP": ("persons", "imf.population"),
    "BCA": ("US dollars", "imf.current_account_balance_usd"),
    "BCA_NGDPD": ("% of GDP", "imf.current_account_balance_gdp"),
    # BX/BM: published by the WEO for country groups only (no economy rows in the Oct 2026 vintage)
    "BX": ("US dollars", None),
    "BM": ("US dollars", None),
    "TX_RPCH": ("% change (volume)", "imf.exports_gs_volume_growth"),
    "TM_RPCH": ("% change (volume)", "imf.imports_gs_volume_growth"),
    "TXG_RPCH": ("% change (volume)", "imf.exports_goods_volume_growth"),
    "TMG_RPCH": ("% change (volume)", "imf.imports_goods_volume_growth"),
    "GGR_NGDP": ("% of GDP", "imf.gg_revenue_gdp"),
    "GGX_NGDP": ("% of GDP", "imf.gg_expenditure_gdp"),
    "GGXCNL_NGDP": ("% of GDP", "imf.gg_net_lending_gdp"),
    "GGXONLB_NGDP": ("% of GDP", "imf.gg_primary_balance_gdp"),
    "GGXWDG_NGDP": ("% of GDP", "imf.gg_gross_debt_gdp"),
    "GGXWDN_NGDP": ("% of GDP", "imf.gg_net_debt_gdp"),
}
#: Indicator-level UNIT attribute the IMF declares for WEO series (checked: a change blocks).
WEO_UNIT_CODES = {"PT": "%", "USD": "US dollars", "XDC": "national currency", "IX": "index"}
#: WEO series that are an index for country groups (SERIES_NAME "Persons for countries / Index for
#: country groups").
WEO_GROUP_INDEX = {"LE": "index (country group)", "LP": "index (country group)"}

BOP_INDICATORS = ("CAB", "G", "S", "GS", "IN1", "IN2")
BOP_ENTRIES = ("CD_T", "DB_T", "NETCD_T")
GFS_INDICATORS = ("G1_T", "G2_T", "G2M_T", "GNOB_T", "GNLB_T")
GFS_SECTORS = ("S13", "S1311")
ER_INDICATORS = ("XDC_USD",)
ER_TRANSFORMS = ("PA_RT", "EOP_RT")
IR_INDICATORS = (
    "MFS166_RT_PT_A_PT",  # Monetary policy-related rate
    "DISR_RT_PT_A_PT",  # Discount rate
    "MMRT_RT_PT_A_PT",  # Money market rate
    "MFS135_RT_PT_A_PT",  # Deposit rate
    "MFS162_RT_PT_A_PT",  # Lending rate
    "IRTBILL_RT_PT_A_PT",  # Treasury bills rate
    "S13BONDS_RT_PT_A_PT",  # Government bonds yields
)
IL_INDICATORS = ("TRGMV_REVS", "RXF11_REVS", "RGOLDMV_REVS")
IMTS_INDICATORS = ("XG_FOB_USD", "MG_CIF_USD")
IMTS_START_YEAR = 2000
IMTS_REPORTERS_PER_REQUEST = 12

DATASETS = ("weo", "bop", "gfs_soo", "er", "mfs_ir", "il", "imts")

_DERIVATION_KIND = {
    "SP": "projection",
    "SE": "estimate",
    "SEME": "estimate",
    "SEHI": "estimate",
}
_STATUS_KIND = {"F": "projection", "E": "estimate", "e": "estimate", "P": "provisional"}


@dataclass(frozen=True)
class FlowQuery:
    """One SDMX data request."""

    dataset: str
    agency: str
    flow: str
    key: str
    start: int | None = None
    end: int | None = None
    part: str = ""

    @property
    def item_key(self) -> str:
        return f"{self.dataset}{':' + self.part if self.part else ''}"

    def url(self) -> str:
        q: dict[str, str] = {}
        if self.start is not None:
            q["startPeriod"] = str(self.start)
        if self.end is not None:
            q["endPeriod"] = str(self.end)
        u = f"{BASE}/data/{self.agency},{self.flow}/{self.key}"
        return u + ("?" + urlencode(q) if q else "")

    def fetch_item(self) -> FetchItem:
        return FetchItem(
            key=self.item_key,
            url=self.url(),
            expected_media_types=("application/vnd.sdmx.structurespecificdata+xml", "application/xml"),
            max_bytes=MAX_RESPONSE_BYTES,
            params={"kind": "sdmx_data", "dataset": self.dataset, "flow": f"{self.agency}:{self.flow}"},
        )


def _plus(codes: Sequence[str]) -> str:
    return "+".join(codes)


def flow_queries(countries: Sequence[str] | None = None, start_year: int | None = None) -> list[FlowQuery]:
    """The non-IMTS requests (one per dataset). ``countries`` (IMF codes) restricts the requests
    (fixtures); ``None`` = every economy and aggregate the IMF publishes."""
    geo = _plus(sorted(countries)) if countries else ""
    s = start_year
    return [
        FlowQuery("weo", "IMF.RES", "WEO", f"{geo}.{_plus(sorted(WEO_INDICATORS))}.A", s),
        FlowQuery("bop", "IMF.STA", "BOP", f"{geo}.{_plus(BOP_ENTRIES)}.{_plus(BOP_INDICATORS)}.USD.A", s),
        FlowQuery(
            "gfs_soo", "IMF.STA", "GFS_SOO", f"{geo}.{_plus(GFS_SECTORS)}..{_plus(GFS_INDICATORS)}.POGDP_PT.A", s
        ),
        FlowQuery("er", "IMF.STA", "ER", f"{geo}.{_plus(ER_INDICATORS)}.{_plus(ER_TRANSFORMS)}.A", s),
        FlowQuery("mfs_ir", "IMF.STA", "MFS_IR", f"{geo}.{_plus(IR_INDICATORS)}.A", s),
        FlowQuery("il", "IMF.STA", "IL", f"{geo}.{_plus(IL_INDICATORS)}.USD.A", s),
    ]


def imts_codelist_item() -> FetchItem:
    return FetchItem(
        key="imts:codelist",
        url=f"{BASE}/codelist/IMF.STA/CL_IMTS_COUNTRY/latest",
        expected_media_types=("application/vnd.sdmx.structure+xml", "application/xml"),
        max_bytes=5_000_000,
        params={"kind": "sdmx_codelist", "dataset": "imts"},
    )


def imts_queries(
    reporters: Sequence[str],
    start_year: int = IMTS_START_YEAR,
    end_year: int | None = None,
    chunk: int = IMTS_REPORTERS_PER_REQUEST,
) -> list[FlowQuery]:
    """Bilateral requests, ``chunk`` reporters each, every partner, years ``start_year``..``end_year``.

    (A one-year all-reporter request took 5 min 45 s on 2026-10-15; per-reporter chunks over many
    years answer in seconds.)"""
    rs = sorted(set(reporters))
    out = []
    for i in range(0, len(rs), chunk):
        part = rs[i : i + chunk]
        out.append(
            FlowQuery(
                "imts",
                "IMF.STA",
                "IMTS",
                f"{_plus(part)}.{_plus(IMTS_INDICATORS)}..A",
                start_year,
                end_year,
                part=f"{part[0]}-{part[-1]}",
            )
        )
    return out


# --------------------------------------------------------------------------- atlas source
#: Fixture subset (tests/fixtures/source/atlas.imf: responses captured live on 2026-10-15).
FIXTURE_COUNTRIES = ("FRA", "DZA", "IND", "EGY", "KOS", "WBG", "USA", "BRA", "G001", "TWN")
FIXTURE_START_YEAR = 2018
FIXTURE_IMTS_YEARS = (2022, 2023)
FIXTURE_IMTS_REPORTERS = ("FRA", "DZA")
#: One full run: 6 flows + the IMTS codelist + ceil(~209 reporters / 12) IMTS chunks (29 measured live).
PLANNED_REQUESTS = 30


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(inspect.getmodule(code_digest)).encode()).hexdigest()  # type: ignore[arg-type]


def atlas_source() -> AtlasSourceSpec:
    """The ``atlas.imf`` source (budget key ``imf.sdmx``)."""
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="International Monetary Fund",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=(
            "IMF.RES:WEO",
            "IMF.STA:BOP",
            "IMF.STA:GFS_SOO",
            "IMF.STA:ER",
            "IMF.STA:MFS_IR",
            "IMF.STA:IL",
            "IMF.STA:IMTS",
        ),
        cadence_seconds=30 * 86400,
        planned_requests=PLANNED_REQUESTS,
        max_response_bytes=MAX_RESPONSE_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "geo_scheme": GEO_SCHEME,
            "units": {
                "weo": "per indicator (see WEO_INDICATORS)",
                "bop": "US dollars",
                "gfs_soo": "% of GDP",
                "er": "national currency per US dollar",
                "mfs_ir": "% per annum",
                "il": "US dollars",
                "imts": "US dollars",
            },
            "time_semantics": "annual periods as labelled by the IMF (WEO maps fiscal years to calendar labels)",
        },
        notes="A new WEO vintage/revision is a new version of the atlas table (latest at cutoff).",
    )


async def fetch(item: FetchItem, run: Any) -> RawAcquisition:
    """GET one item through the atlas run (metered live client or fixture client)."""
    accept = ACCEPT if item.params.get("kind") == "sdmx_data" else "application/vnd.sdmx.structure+xml;version=2.1"
    raw: RawAcquisition = await run.get(
        item.url,
        key=item.key,
        media_types=item.expected_media_types or ("application/xml",),
        headers={"Accept": accept},
        max_bytes=item.max_bytes,
        params=item.params,
    )
    return raw


# --------------------------------------------------------------------------- SDMX parsing
class SdmxError(ValueError):
    """The bytes are not an SDMX-ML 2.1 structure-specific message this parser accepts."""


_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")


def _guard(body: bytes) -> None:
    head = body[:4096]
    if any(f in head for f in _FORBIDDEN) or b"<!ENTITY" in body:
        raise SdmxError("DTD/entity declarations are refused")
    if not body.lstrip().startswith(b"<"):
        raise SdmxError("not an XML document")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class SdmxMessage:
    structure: dict[str, str] = field(default_factory=dict)  # agencyID/id/version of the dataflow
    prepared: str | None = None
    dataset_attrs: dict[str, str] = field(default_factory=dict)
    groups: list[dict[str, str]] = field(default_factory=list)
    series: list[tuple[dict[str, str], list[dict[str, str]]]] = field(default_factory=list)

    @property
    def flow_ref(self) -> str:
        s = self.structure
        return f"{s.get('agencyID')}:{s.get('id')}({s.get('version')})"


def parse_sdmx(body: bytes) -> SdmxMessage:
    """Parse an SDMX-ML 2.1 structure-specific data message (streaming)."""
    _guard(body)
    msg = SdmxMessage()
    cur: tuple[dict[str, str], list[dict[str, str]]] | None = None
    seen_dataset = False
    try:
        # DTDs/entities are refused by _guard; expat never fetches external entities.
        for ev, el in ET.iterparse(io.BytesIO(body), events=("start", "end")):  # noqa: S314
            name = _local(el.tag)
            if ev == "start":
                if name == "DataSet":
                    seen_dataset = True
                    msg.dataset_attrs = {_local(k): v for k, v in el.attrib.items()}
                elif name == "Series":
                    cur = ({_local(k): v for k, v in el.attrib.items()}, [])
                elif name == "Ref" and not msg.structure and el.get("id"):
                    msg.structure = {k: el.get(k, "") for k in ("agencyID", "id", "version")}
                continue
            if name == "Obs" and cur is not None:
                cur[1].append(dict(el.attrib))
                el.clear()
            elif name == "Series" and cur is not None:
                msg.series.append(cur)
                cur = None
                el.clear()
            elif name == "Group":
                msg.groups.append({_local(k): v for k, v in el.attrib.items() if _local(k) != "type"})
                el.clear()
            elif name == "Prepared":
                msg.prepared = (el.text or "").strip()
    except ET.ParseError as e:
        raise SdmxError(f"malformed SDMX-ML: {e}") from e
    if not seen_dataset:
        raise SdmxError("no DataSet element (error message or wrong format)")
    return msg


def parse_codelist(body: bytes) -> dict[str, str]:
    """``{code: English name}`` of an SDMX-ML 2.1 structure message holding one codelist."""
    _guard(body)
    out: dict[str, str] = {}
    lang = "{http://www.w3.org/XML/1998/namespace}lang"
    try:
        root = ET.fromstring(body)  # noqa: S314 - DTDs/entities refused by _guard
    except ET.ParseError as e:
        raise SdmxError(f"malformed SDMX structure: {e}") from e
    for code in root.iter():
        cid = code.get("id")
        if _local(code.tag) != "Code" or not cid:
            continue
        names = [n for n in code if _local(n.tag) == "Name"]
        en = next((n.text for n in names if n.get(lang) == "en"), None) or (names[0].text if names else "")
        out[cid] = (en or "").strip()
    if not out:
        raise SdmxError("codelist has no codes")
    return out


def imts_reporters(codelist: Mapping[str, str]) -> list[str]:
    """Economies (3-letter alphabetic IMF codes), excluding the IMF's area/department codes."""
    departments = {"AFR", "APD", "EUR", "MCD", "WHD", "SDS"}
    return sorted(c for c in codelist if re.fullmatch(r"[A-Z]{3}", c) and c not in departments)


# --------------------------------------------------------------------------- values / flags
_DEC_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")


def decimal_text(v: str | None) -> str | None:
    """The reported number as a strict decimal string; ``None`` for absent / non-finite values."""
    if v is None:
        return None
    s = v.strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    if out in ("-0", "", "-"):
        out = "0"
    if not _DEC_RE.match(out):  # pragma: no cover - format(Decimal, "f") always matches
        return None
    return out


def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps({k: v for k, v in obj.items() if v is not None}, sort_keys=True, separators=(",", ":"))


_FY_RE = re.compile(r"^FY(\d{4})/(\d{2})$")
_FY_MAP_T_T1 = re.compile(r"FY\(t/t\+1\)\s*=\s*CY\(t\)")
_FY_MAP_T1_T = re.compile(r"FY\(t-1/t\)\s*=\s*CY\(t\)")


def weo_boundary(latest_actual: str | None, methodology_notes: str | None) -> tuple[int | None, str]:
    """(last actual WEO year label, rule) from ``LATEST_ACTUAL_ANNUAL_DATA``.

    ``"2025"`` -> (2025, "declared"); ``"FY2024/25"`` -> 2024 or 2025 per the series' own
    ``FY(...) = CY(t)`` note, else the earlier year (``fiscal_year_conservative``)."""
    if not latest_actual:
        return None, "not_declared"
    s = latest_actual.strip()
    if re.fullmatch(r"\d{4}", s):
        return int(s), "declared"
    m = _FY_RE.match(s)
    if m:
        y1 = int(m.group(1))
        notes = methodology_notes or ""
        if _FY_MAP_T1_T.search(notes):
            return y1 + 1, "fiscal_year_mapped"
        if _FY_MAP_T_T1.search(notes):
            return y1, "fiscal_year_mapped"
        return y1, "fiscal_year_conservative"
    return None, "unparsed"


def _year(tp: str | None) -> int | None:
    if tp and re.fullmatch(r"\d{4}", tp):
        return int(tp)
    return None


def _obs_kind(obs: Mapping[str, str]) -> str | None:
    kinds = {_DERIVATION_KIND.get(obs.get("DERIVATION_TYPE", "")), _STATUS_KIND.get(obs.get("STATUS", ""))}
    kinds.discard(None)
    for k in ("projection", "estimate", "provisional"):
        if k in kinds:
            return k
    return None


# --------------------------------------------------------------------------- frame building
class _Columns:
    """Column-wise accumulator (memory-light for millions of rows)."""

    COLS = ("indicator", "concept_id", "provider_geo", "year", "value", "unit", "flags", "selector")

    def __init__(self) -> None:
        self.data: dict[str, list[Any]] = {c: [] for c in self.COLS}
        self._intern: dict[str, str] = {}

    def _i(self, s: str | None) -> str | None:
        if s is None:
            return None
        return self._intern.setdefault(s, s)

    def add(self, **kw: Any) -> None:
        for c in self.COLS:
            v = kw[c]
            self.data[c].append(self._i(v) if isinstance(v, str) and c not in ("value",) else v)

    def __len__(self) -> int:
        return len(self.data["indicator"])

    def frame(self, dataset: str, raw_sha256: str) -> pl.DataFrame:
        from reva.atlas.tables import ATLAS_SCHEMA

        f = pl.DataFrame(
            {
                "indicator": pl.Series(self.data["indicator"], dtype=pl.String),
                "concept_id": pl.Series(self.data["concept_id"], dtype=pl.String),
                "provider_geo": pl.Series(self.data["provider_geo"], dtype=pl.String),
                "year": pl.Series(self.data["year"], dtype=pl.Int32),
                "value": pl.Series(self.data["value"], dtype=pl.String),
                "unit": pl.Series(self.data["unit"], dtype=pl.String),
                "flags": pl.Series(self.data["flags"], dtype=pl.String),
                "selector": pl.Series(self.data["selector"], dtype=pl.String),
            }
        )
        f = f.with_columns(
            pl.lit(SOURCE).alias("source"),
            pl.lit(dataset).alias("dataset"),
            pl.lit(None, dtype=pl.String).alias("iso3"),
            pl.lit(None, dtype=pl.String).alias("entity_qid"),
            pl.date(pl.col("year"), 1, 1).alias("period_start"),
            pl.date(pl.col("year") + 1, 1, 1).alias("period_end"),
            pl.lit(None, dtype=pl.String).alias("value_text"),
            pl.lit(raw_sha256).alias("raw_sha256"),
        )
        return f.select([pl.col(c).cast(ATLAS_SCHEMA[c]) for c in ATLAS_SCHEMA.names()])


@dataclass
class ParseStats:
    series: int = 0
    obs: int = 0
    rows: int = 0
    skipped_no_value: int = 0
    skipped_non_annual: int = 0
    projection_rows: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _selector(msg: SdmxMessage, series_key: str, tp: str, item_key: str) -> str:
    return _canon({"item": item_key, "flow": msg.flow_ref, "series": series_key, "time_period": tp})


def weo_vintage(msg: SdmxMessage) -> dict[str, str]:
    """Vintage label of a WEO message (from the dataset PUBLICATION_DATE and the dataflow version)."""
    pub = msg.dataset_attrs.get("PUBLICATION_DATE") or ""
    m = re.match(r"^(\d{4})-(\d{2})", pub)
    if not m:
        raise SdmxError("WEO message has no PUBLICATION_DATE: the vintage cannot be labelled")
    month = int(m.group(2))
    label = f"WEO {'April' if month <= 6 else 'October'} {m.group(1)}"
    return {
        "vintage": f"{m.group(1)}-{m.group(2)}",
        "vintage_label": label,
        "publication_date": pub,
        "flow": msg.flow_ref,
    }


def parse_weo(msg: SdmxMessage, raw_sha256: str, item_key: str = "weo") -> tuple[pl.DataFrame, ParseStats]:
    from reva.atlas.tables import country_iso3

    vint = weo_vintage(msg)
    pub_year = int(vint["vintage"][:4])
    st = ParseStats()
    ind_groups = {g["INDICATOR"]: g for g in msg.groups if "INDICATOR" in g and "COUNTRY" not in g}
    for code, g in ind_groups.items():
        unit = g.get("UNIT")
        if code in WEO_INDICATORS and unit is not None and unit not in WEO_UNIT_CODES:
            raise SdmxError(f"WEO {code}: unexpected UNIT {unit!r} (definitions changed; review units)")
        want = WEO_INDICATORS.get(code, ("",))[0]
        if (unit == "PT" and not want.startswith("%")) or (unit == "USD" and "US dollars" not in want):
            raise SdmxError(f"WEO {code}: declared UNIT {unit} does not match the catalogued unit {want!r}")
    cgroups = {(g.get("COUNTRY"), g.get("INDICATOR")): g for g in msg.groups if "COUNTRY" in g}
    cols = _Columns()
    boundary_rules: dict[str, int] = {}
    for sattrs, obs in msg.series:
        st.series += 1
        code, geo = sattrs.get("INDICATOR", ""), sattrs.get("COUNTRY", "")
        if code not in WEO_INDICATORS:
            raise SdmxError(f"WEO returned an unrequested indicator {code!r}")
        if sattrs.get("FREQUENCY") != "A":
            st.skipped_non_annual += len(obs)
            continue
        unit, concept = WEO_INDICATORS[code]
        if code in WEO_GROUP_INDEX and country_iso3(geo, GEO_SCHEME) is None:
            unit = WEO_GROUP_INDEX[code]
        g = cgroups.get((geo, code), {})
        latest = g.get("LATEST_ACTUAL_ANNUAL_DATA")
        boundary, rule = weo_boundary(latest, g.get("METHODOLOGY_NOTES"))
        fallback = None
        if boundary is None:
            ng = cgroups.get((geo, "NGDP"), {})
            fallback, _r = weo_boundary(ng.get("LATEST_ACTUAL_ANNUAL_DATA"), ng.get("METHODOLOGY_NOTES"))
            rule = f"{rule}_ngdp_inherited" if fallback is not None else f"{rule}_publication_year"
        boundary_rules[rule] = boundary_rules.get(rule, 0) + 1
        months = g.get("START_END_MONTHS_OF_REPORTING_YEAR")
        skey = f"{geo}.{code}.A"
        for o in obs:
            st.obs += 1
            tp = o.get("TIME_PERIOD")
            y = _year(tp)
            val = decimal_text(o.get("OBS_VALUE"))
            if y is None:
                st.skipped_non_annual += 1
                continue
            if val is None:
                st.skipped_no_value += 1
                continue
            kind: str | None
            if boundary is not None:
                kind = "projection" if y > boundary else None
            elif fallback is not None:
                # No declared boundary (PPP series): a year after the country's NGDP boundary
                # needs projected GDP, so it is a projection; earlier years stay unknown.
                kind = "projection" if y > fallback or y >= pub_year else "unknown_boundary"
            else:
                kind = "projection" if y >= pub_year else "unknown_boundary"
            if kind == "projection":
                st.projection_rows += 1
            flags = _canon(
                {
                    "vintage": vint["vintage"],
                    "latest_actual": latest,
                    "boundary_rule": None if rule == "declared" else rule,
                    "estimate_kind": kind,
                    "display_scale": sattrs.get("SCALE"),
                    "reporting_year": months if months and months != "January/December" else None,
                }
            )
            cols.add(
                indicator=code,
                concept_id=concept,
                provider_geo=geo,
                year=y,
                value=val,
                unit=unit,
                flags=flags,
                selector=_selector(msg, skey, tp or "", item_key),
            )
    st.rows = len(cols)
    st.notes = {"vintage": vint, "boundary_rules": boundary_rules}
    return cols.frame("weo", raw_sha256), st


_BOP_UNIT = {"USD": "US dollars"}


def _generic(
    msg: SdmxMessage,
    raw_sha256: str,
    dataset: str,
    item_key: str,
    *,
    indicator: Callable[[dict[str, str]], str],
    unit: Callable[[dict[str, str]], str],
    concept: Callable[[str], str | None],
    series_key: Callable[[dict[str, str]], str],
    extra_flags: Callable[[dict[str, str], dict[str, str]], dict[str, Any]] | None = None,
    cols: _Columns | None = None,
    st: ParseStats | None = None,
) -> tuple[_Columns, ParseStats]:
    cols = cols if cols is not None else _Columns()
    st = st if st is not None else ParseStats()
    seen: set[tuple[str, str, int]] = set()
    for sattrs, obs in msg.series:
        st.series += 1
        if sattrs.get("FREQUENCY") != "A":
            st.skipped_non_annual += len(obs)
            continue
        ind = indicator(sattrs)
        geo = sattrs.get("COUNTRY", "")
        u = unit(sattrs)
        skey = series_key(sattrs)
        for o in obs:
            st.obs += 1
            tp = o.get("TIME_PERIOD")
            y = _year(tp)
            if y is None:
                st.skipped_non_annual += 1
                continue
            val = decimal_text(o.get("OBS_VALUE"))
            if val is None:
                st.skipped_no_value += 1
                continue
            k = (ind, geo, y)
            if k in seen:
                raise SdmxError(f"{dataset}: duplicate cell {k} (an unfolded dimension)")
            seen.add(k)
            kind = _obs_kind(o)
            if kind == "projection":
                st.projection_rows += 1
            flags: dict[str, Any] = {
                "derivation_type": o.get("DERIVATION_TYPE"),
                "provider_status": o.get("STATUS"),
                "estimate_kind": kind,
                "display_scale": sattrs.get("SCALE"),
                "ifs_series": "true" if sattrs.get("IFS_FLAG") == "true" else None,
                "methodology": sattrs.get("METHODOLOGY"),
            }
            if extra_flags is not None:
                flags.update(extra_flags(sattrs, o))
            cols.add(
                indicator=ind,
                concept_id=concept(ind),
                provider_geo=geo,
                year=y,
                value=val,
                unit=u,
                flags=_canon(flags),
                selector=_selector(msg, skey, tp or "", item_key),
            )
    return cols, st


BOP_CONCEPTS = {
    "CAB|entry=NETCD_T": "imf.current_account_balance_usd",
    "G|entry=NETCD_T": "imf.goods_balance_usd",
    "S|entry=NETCD_T": "imf.services_balance_usd",
    "GS|entry=NETCD_T": "imf.goods_services_balance_usd",
    "G|entry=CD_T": "imf.goods_exports_bop_usd",
    "G|entry=DB_T": "imf.goods_imports_bop_usd",
    "S|entry=CD_T": "imf.services_exports_bop_usd",
    "S|entry=DB_T": "imf.services_imports_bop_usd",
    "GS|entry=CD_T": "imf.exports_goods_services_usd",
    "GS|entry=DB_T": "imf.imports_goods_services_usd",
    "IN1|entry=NETCD_T": "imf.primary_income_balance_usd",
    "IN2|entry=NETCD_T": "imf.secondary_income_balance_usd",
}


def parse_bop(msg: SdmxMessage, raw_sha256: str, item_key: str = "bop") -> tuple[pl.DataFrame, ParseStats]:
    def unit(s: dict[str, str]) -> str:
        if s.get("UNIT") not in _BOP_UNIT:
            raise SdmxError(f"BOP: unrequested UNIT {s.get('UNIT')!r}")
        return _BOP_UNIT[s["UNIT"]]

    cols, st = _generic(
        msg,
        raw_sha256,
        "bop",
        item_key,
        indicator=lambda s: f"{s['INDICATOR']}|entry={s['BOP_ACCOUNTING_ENTRY']}",
        unit=unit,
        concept=BOP_CONCEPTS.get,
        series_key=lambda s: f"{s['COUNTRY']}.{s['BOP_ACCOUNTING_ENTRY']}.{s['INDICATOR']}.{s['UNIT']}.A",
    )
    st.rows = len(cols)
    return cols.frame("bop", raw_sha256), st


GFS_CONCEPTS = {
    "GNLB_T|sector=S13": "imf.gfs_gg_net_lending_gdp",
    "G1_T|sector=S13": "imf.gfs_gg_revenue_gdp",
    "G2M_T|sector=S13": "imf.gfs_gg_expenditure_gdp",
    "G2_T|sector=S13": "imf.gfs_gg_expense_gdp",
    "GNOB_T|sector=S13": "imf.gfs_gg_net_operating_balance_gdp",
    "GNLB_T|sector=S1311": "imf.gfs_bcg_net_lending_gdp",
    "G1_T|sector=S1311": "imf.gfs_bcg_revenue_gdp",
    "G2M_T|sector=S1311": "imf.gfs_bcg_expenditure_gdp",
    "G2_T|sector=S1311": "imf.gfs_bcg_expense_gdp",
    "GNOB_T|sector=S1311": "imf.gfs_bcg_net_operating_balance_gdp",
}


def parse_gfs(msg: SdmxMessage, raw_sha256: str, item_key: str = "gfs_soo") -> tuple[pl.DataFrame, ParseStats]:
    def unit(s: dict[str, str]) -> str:
        if s.get("TYPE_OF_TRANSFORMATION") != "POGDP_PT":
            raise SdmxError(f"GFS: unrequested transformation {s.get('TYPE_OF_TRANSFORMATION')!r}")
        return "% of GDP"

    cols, st = _generic(
        msg,
        raw_sha256,
        "gfs_soo",
        item_key,
        indicator=lambda s: f"{s['INDICATOR']}|sector={s['SECTOR']}",
        unit=unit,
        concept=GFS_CONCEPTS.get,
        series_key=lambda s: (
            f"{s['COUNTRY']}.{s['SECTOR']}.{s['GFS_GRP']}.{s['INDICATOR']}.{s['TYPE_OF_TRANSFORMATION']}.A"
        ),
        extra_flags=lambda s, o: {
            "recording_basis": o.get("BASES_OF_RECORDING_CASH_NON_CASH"),
            "valuation": None if o.get("VALUATION") in (None, "_Z") else o.get("VALUATION"),
        },
    )
    st.rows = len(cols)
    return cols.frame("gfs_soo", raw_sha256), st


def parse_er(msg: SdmxMessage, raw_sha256: str, item_key: str = "er") -> tuple[pl.DataFrame, ParseStats]:
    concepts = {
        "XDC_USD|PA_RT": "imf.exchange_rate_lcu_per_usd_avg",
        "XDC_USD|EOP_RT": "imf.exchange_rate_lcu_per_usd_eop",
    }

    def unit(s: dict[str, str]) -> str:
        if s.get("INDICATOR") != "XDC_USD":
            raise SdmxError(f"ER: unrequested indicator {s.get('INDICATOR')!r}")
        return "national currency per US dollar"

    cols, st = _generic(
        msg,
        raw_sha256,
        "er",
        item_key,
        indicator=lambda s: f"{s['INDICATOR']}|{s['TYPE_OF_TRANSFORMATION']}",
        unit=unit,
        concept=concepts.get,
        series_key=lambda s: f"{s['COUNTRY']}.{s['INDICATOR']}.{s['TYPE_OF_TRANSFORMATION']}.A",
    )
    st.rows = len(cols)
    return cols.frame("er", raw_sha256), st


IR_CONCEPTS = {
    "MFS166_RT_PT_A_PT": "imf.policy_rate",
    "DISR_RT_PT_A_PT": "imf.discount_rate",
    "MMRT_RT_PT_A_PT": "imf.money_market_rate",
    "MFS135_RT_PT_A_PT": "imf.deposit_rate",
    "MFS162_RT_PT_A_PT": "imf.lending_rate",
    "IRTBILL_RT_PT_A_PT": "imf.treasury_bill_rate",
    "S13BONDS_RT_PT_A_PT": "imf.government_bond_yield",
}


def parse_ir(msg: SdmxMessage, raw_sha256: str, item_key: str = "mfs_ir") -> tuple[pl.DataFrame, ParseStats]:
    def unit(s: dict[str, str]) -> str:
        if s.get("INDICATOR") not in IR_CONCEPTS:
            raise SdmxError(f"MFS_IR: unrequested indicator {s.get('INDICATOR')!r}")
        return "% per annum"

    cols, st = _generic(
        msg,
        raw_sha256,
        "mfs_ir",
        item_key,
        indicator=lambda s: s["INDICATOR"],
        unit=unit,
        concept=IR_CONCEPTS.get,
        series_key=lambda s: f"{s['COUNTRY']}.{s['INDICATOR']}.A",
    )
    st.rows = len(cols)
    return cols.frame("mfs_ir", raw_sha256), st


IL_CONCEPTS = {
    "TRGMV_REVS|unit=USD": "imf.total_reserves_usd",
    "RXF11_REVS|unit=USD": "imf.reserves_excl_gold_usd",
    "RGOLDMV_REVS|unit=USD": "imf.gold_reserves_usd",
}


def parse_il(msg: SdmxMessage, raw_sha256: str, item_key: str = "il") -> tuple[pl.DataFrame, ParseStats]:
    def unit(s: dict[str, str]) -> str:
        if s.get("UNIT") != "USD":
            raise SdmxError(f"IL: unrequested UNIT {s.get('UNIT')!r}")
        return "US dollars"

    cols, st = _generic(
        msg,
        raw_sha256,
        "il",
        item_key,
        indicator=lambda s: f"{s['INDICATOR']}|unit={s['UNIT']}",
        unit=unit,
        concept=IL_CONCEPTS.get,
        series_key=lambda s: f"{s['COUNTRY']}.{s['INDICATOR']}.{s['UNIT']}.A",
    )
    st.rows = len(cols)
    return cols.frame("il", raw_sha256), st


IMTS_FLOWS = {"XG_FOB_USD": "exports of goods, FOB", "MG_CIF_USD": "imports of goods, CIF"}
#: Concept hints of the partner "G001 World" rows (reporter totals); bilateral rows carry no hint
#: (one catalog concept cannot list ~250 partner series; LINKS reads the table directly).
IMTS_WORLD_CONCEPTS = {
    "XG_FOB_USD|partner=G001": "imf.goods_exports_world_usd",
    "MG_CIF_USD|partner=G001": "imf.goods_imports_world_usd",
}


def parse_imts(
    msg: SdmxMessage, raw_sha256: str, item_key: str, partner_names: Mapping[str, str] | None = None
) -> tuple[pl.DataFrame, ParseStats]:
    """Bilateral goods trade. ``indicator = "<flow>|partner=<IMF code>"``; partner mapped in flags."""
    from reva.atlas.tables import country_iso3

    pmap: dict[str, str | None] = {}

    def partner_flags(s: dict[str, str], o: dict[str, str]) -> dict[str, Any]:
        p = s["COUNTERPART_COUNTRY"]
        if p not in pmap:
            pmap[p] = country_iso3(p, GEO_SCHEME)
        return {
            "partner": p,
            "partner_iso3": pmap[p],
            "partner_unmapped": True if pmap[p] is None else None,
            "flow_valuation": "FOB" if s["INDICATOR"].startswith("XG") else "CIF",
        }

    def unit(s: dict[str, str]) -> str:
        if s.get("INDICATOR") not in IMTS_FLOWS:
            raise SdmxError(f"IMTS: unrequested indicator {s.get('INDICATOR')!r}")
        return "US dollars"

    cols, st = _generic(
        msg,
        raw_sha256,
        "imts",
        item_key,
        indicator=lambda s: f"{s['INDICATOR']}|partner={s['COUNTERPART_COUNTRY']}",
        unit=unit,
        concept=IMTS_WORLD_CONCEPTS.get,
        series_key=lambda s: f"{s['COUNTRY']}.{s['INDICATOR']}.{s['COUNTERPART_COUNTRY']}.A",
        extra_flags=partner_flags,
    )
    st.rows = len(cols)
    st.notes = {"partners": len(pmap), "unmapped_partners": sorted(p for p, v in pmap.items() if v is None)}
    if partner_names:
        st.notes["unmapped_partner_names"] = {p: partner_names.get(p, "") for p in st.notes["unmapped_partners"]}
    return cols.frame("imts", raw_sha256), st


PARSERS: dict[str, Callable[[SdmxMessage, str, str], tuple[pl.DataFrame, ParseStats]]] = {
    "weo": parse_weo,
    "bop": parse_bop,
    "gfs_soo": parse_gfs,
    "er": parse_er,
    "mfs_ir": parse_ir,
    "il": parse_il,
}

COVERAGE_NOTES = {
    "weo": "IMF World Economic Outlook via SDMX (IMF.RES:WEO), vintage {vintage}; years after each series' "
    "LATEST_ACTUAL_ANNUAL_DATA flagged estimate_kind=projection; values in full units (flags.display_scale "
    "is the IMF display scale); aggregates kept unmapped",
    "bop": "IMF Balance of Payments (IMF.STA:BOP, BPM6), US dollars, credit/debit/net; annual",
    "gfs_soo": "IMF GFS Statement of Operations (IMF.STA:GFS_SOO), % of GDP, general (S13) and budgetary "
    "central (S1311) government; annual",
    "er": "IMF Exchange Rates (IMF.STA:ER; ex-IFS), national currency per USD, period average / end of period",
    "mfs_ir": "IMF MFS interest rates (IMF.STA:MFS_IR; ex-IFS), % per annum, annual",
    "il": "IMF International Liquidity (IMF.STA:IL; ex-IFS), reserves in US dollars, annual",
    "imts": "IMF International Trade in Goods by partner (IMF.STA:IMTS; successor of DOTS), US dollars, "
    "exports FOB / imports CIF; partner folded into indicator, mapped in flags.partner_iso3; "
    "includes IMF estimates for non-reporting economies (status 'e' flagged where the IMF marks it)",
}


# --------------------------------------------------------------------------- orchestration
@dataclass
class IngestResult:
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    raws: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return len(self.raws) + len(self.failures)

    @property
    def bytes(self) -> int:
        return sum(int(r["bytes"]) for r in self.raws)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tables": self.tables,
            "stats": self.stats,
            "failures": self.failures,
            "requests": self.requests,
            "raw_bytes": self.bytes,
        }


def _rights() -> dict[str, Any]:
    return {
        "licence": LICENCE,
        "attribution": "International Monetary Fund",
        "review": "pending_maintainer_review",
        "source_key": SOURCE_KEY,
    }


async def ingest(
    run: Any,
    *,
    countries: Sequence[str] | None = None,
    start_year: int | None = None,
    imts_start_year: int = IMTS_START_YEAR,
    imts_end_year: int | None = None,
    imts_reporters_override: Sequence[str] | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> IngestResult:
    """Fetch, archive (raw first), parse and register every IMF atlas table the run wants.

    ``run`` is a :class:`reva.atlas.ingest.AtlasRun` (``run.wants`` honours ``--dataset``). A
    failed provider request is recorded in ``failures`` and its table is not registered (no
    partial IMTS table: every reporter chunk must succeed). A budget refusal or a cancellation is
    never swallowed: it ends the run (``run_atlas_ingest`` reports ``budget_exhausted``)."""
    from reva.kernel.errors import PolicyDenied
    from reva.operations.budgets import BudgetExhausted

    res = IngestResult()

    async def get(item: FetchItem) -> tuple[RawAcquisition, str] | None:
        try:
            raw = await fetch(item, run)
        except (BudgetExhausted, PolicyDenied):
            raise
        except Exception as e:  # typed SourceResponseError / UnsafeRequest; recorded, table skipped
            res.failures.append({"item": item.key, "url": item.url, "error": f"{type(e).__name__}: {e}"[:500]})
            log(f"FAILED {item.key}: {e}")
            return None
        sha = run.archive(raw)
        res.raws.append(
            {
                "item": item.key,
                "url": item.url,
                "sha256": sha,
                "bytes": len(raw.body),
                "retrieved_at": raw.retrieved_at.isoformat(),
            }
        )
        log(f"archived {item.key} {len(raw.body)} B sha256={sha[:12]}")
        return raw, sha

    def register(dataset: str, frame: pl.DataFrame, shas: list[str], st: ParseStats, note: str) -> None:
        summary = run.register(
            dataset=dataset,
            frame=frame,
            raw_sha256s=shas,
            coverage_note=note[:4000],
            geo_scheme=GEO_SCHEME,
            rights=_rights(),
        )
        res.tables[dataset] = {k: v for k, v in summary.items() if k != "coverage"} | {
            "coverage": {k: v for k, v in summary["coverage"].items() if k != "unmapped_provider_geo"}
        }
        res.stats[dataset] = st.as_dict()
        log(f"registered imf.{dataset}: {summary['row_count']} rows, created={summary['created']}")

    for q in flow_queries(countries, start_year):
        if not run.wants(q.dataset):
            continue
        got = await get(q.fetch_item())
        if got is None:
            continue
        raw, sha = got
        msg = parse_sdmx(raw.body)
        frame, st = PARSERS[q.dataset](msg, sha, q.item_key)
        if frame.height == 0:
            res.failures.append({"item": q.item_key, "error": "no observations"})
            continue
        note = COVERAGE_NOTES[q.dataset]
        if q.dataset == "weo":
            note = note.format(vintage=st.notes["vintage"]["vintage_label"])
        register(q.dataset, frame, [sha], st, note)

    if run.wants("imts"):
        got = await get(imts_codelist_item())
        if got is not None:
            names = parse_codelist(got[0].body)
            reporters = list(imts_reporters_override) if imts_reporters_override else imts_reporters(names)
            if countries and not imts_reporters_override:
                reporters = [r for r in reporters if r in set(countries)]
            frames: list[pl.DataFrame] = []
            shas = [got[1]]
            total = ParseStats()
            failed = False
            unmapped_partners: set[str] = set()
            for q in imts_queries(reporters, imts_start_year, imts_end_year):
                g = await get(q.fetch_item())
                if g is None:
                    failed = True
                    continue
                raw, sha = g
                f, st = parse_imts(parse_sdmx(raw.body), sha, q.item_key, names)
                shas.append(sha)
                if f.height:
                    frames.append(f)
                for k in ("series", "obs", "rows", "skipped_no_value", "skipped_non_annual", "projection_rows"):
                    setattr(total, k, getattr(total, k) + getattr(st, k))
                unmapped_partners.update(st.notes.get("unmapped_partners", []))
            total.notes = {
                "reporters_requested": len(reporters),
                "unmapped_partners": sorted(unmapped_partners),
                "years": [imts_start_year, imts_end_year],
            }
            if failed:
                res.failures.append(
                    {"item": "imts", "error": "one or more reporter chunks failed; table not registered"}
                )
            elif frames:
                register("imts", pl.concat(frames), shas, total, COVERAGE_NOTES["imts"])
    return res


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Atlas platform entry point (``reva atlas ingest atlas.imf``).

    Live: every IMF economy and aggregate. Fixture mode replays the captured subset
    (:data:`FIXTURE_COUNTRIES`, years from :data:`FIXTURE_START_YEAR`, two IMTS reporters)."""
    if run.origin == "test_fixture":
        res = await ingest(
            run,
            countries=FIXTURE_COUNTRIES,
            start_year=FIXTURE_START_YEAR,
            imts_start_year=FIXTURE_IMTS_YEARS[0],
            imts_end_year=FIXTURE_IMTS_YEARS[1],
            imts_reporters_override=FIXTURE_IMTS_REPORTERS,
        )
    else:
        res = await ingest(run)
    return res.as_dict()


__all__ = [
    "CONNECTOR_NAME",
    "DATASETS",
    "GEO_SCHEME",
    "PARSER_VERSION",
    "SOURCE",
    "SdmxError",
    "atlas_ingest",
    "atlas_source",
    "decimal_text",
    "fetch",
    "flow_queries",
    "imts_queries",
    "imts_reporters",
    "ingest",
    "parse_codelist",
    "parse_sdmx",
    "parse_weo",
    "weo_boundary",
]
