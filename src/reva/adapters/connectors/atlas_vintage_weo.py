"""IMF World Economic Outlook historical vintages -> vintage atlas tables (V5, agent PRED-VINTAGE).

Two sources, one atlas family (``vintage_weo``); conventions in :mod:`.vintage_common`:

``atlas.vintage_weo_dbn`` -> table ``vintage_weo.weo_dbnomics``
    DBnomics' copies of the IMF WEO database **as released**, one DBnomics dataset per vintage
    (``IMF/WEO:2008-04`` ... ``IMF/WEO:2024-10``; 33 vintages, the September 2011 release is not
    in DBnomics). API ``https://api.db.nomics.world/v22/series/IMF/WEO:<vintage>`` (open, no key),
    restricted to the five WEO subjects below for every country. The IMF's own per-vintage
    download pages (``www.imf.org/.../weo-database/<year>/<month>``) answer 403 "Access Denied"
    (Akamai) to non-browser clients: not worked around (owner decision, see the report).
    DBnomics keeps neither the release day nor the "Estimates Start After" column, so:
    ``available_at`` = first instant of the month after the release month (rule
    ``weo_release_month_end``) and years before the vintage year are
    ``published_history_boundary_unknown`` (IMF values as then published: actuals or staff
    estimates), the vintage year and later ``projection``.

``atlas.vintage_weo_imf`` -> table ``vintage_weo.weo_imf``
    IMF data portal SDMX 2.1 (``api.imf.org``): ``IMF.RES:WEO(6.0.0)`` (April 2025),
    ``IMF.RES:WEO_2025_OCT_VINTAGE``, ``IMF.RES:WEO_2026_APR_VINTAGE``, ``IMF.RES:WEO(9.0.0)``
    (October 2026) — the vintages the portal serves. Parsed with :func:`atlas_imf.parse_weo`
    (``LATEST_ACTUAL_ANNUAL_DATA`` boundary -> ``actual``/``projection``); ``available_at`` =
    the dataset ``PUBLICATION_DATE`` instant verbatim (rule ``imf_publication_date_attribute``).

Cells: ``indicator`` = ``<WEO subject>|v=<YYYY-MM>``; ``period`` = the **WEO year label** as a
calendar year in both tables (so vintages compare cell by cell; the IMF portal's true fiscal-year
window is kept in ``flags.fiscal_window``); ``provider_geo`` = the WEO country code (ISO 3166
alpha-3, plus ``UVK`` Kosovo — left unmapped: no published identifier links it — and ``WBG``
West Bank and Gaza -> PSE through the ``imf`` scheme); ``value`` = the number verbatim.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import polars as pl

from reva.adapters.connectors import atlas_imf
from reva.adapters.connectors.vintage_common import (
    Rows,
    coverage_line,
    decimal_or_none,
    first_of_next_month,
    indicator,
    vintage_flags,
    year_bounds,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

ATLAS_SOURCE = "vintage_weo"
DS_DBN = "weo_dbnomics"
DS_IMF = "weo_imf"
NAME_DBN = "atlas.vintage_weo_dbn"
NAME_IMF = "atlas.vintage_weo_imf"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_vintage_weo/1 (+atlas_imf/2 parse_weo)"
DBN_HOST = "api.db.nomics.world"
DBN_BASE = f"https://{DBN_HOST}/v22/series/IMF"
IMF_BASE = atlas_imf.BASE
GEO_SCHEME = "imf"

#: WEO subject -> (REVA vintage variable, WEO definition as published in the WEO database).
SUBJECTS: dict[str, tuple[str, str]] = {
    "NGDP_RPCH": ("real_gdp_growth", "Gross domestic product, constant prices; Percent change"),
    "PCPIPCH": ("cpi_inflation", "Inflation, average consumer prices; Percent change"),
    "LUR": ("unemployment_rate", "Unemployment rate; Percent of total labor force"),
    "BCA_NGDPD": ("current_account_gdp", "Current account balance; Percent of GDP"),
    "GGXWDG_NGDP": ("gg_gross_debt_gdp", "General government gross debt; Percent of GDP"),
}
#: DBnomics IMF/WEO datasets (one per release), listed by https://api.db.nomics.world/v22/datasets/IMF on
#: 2026-10-17. 2011-09 is absent from DBnomics; 2025-04 is taken from the IMF portal (WEO 6.0.0).
DBN_VINTAGES: tuple[str, ...] = tuple(
    f"{y}-{m}" for y in range(2008, 2025) for m in ("04", "10") if (y, m) != (2011, "10")
)
#: IMF portal WEO vintage dataflows: (flow id, version).
IMF_FLOWS: tuple[tuple[str, str], ...] = (
    ("WEO", "6.0.0"),
    ("WEO_2025_OCT_VINTAGE", "1.0.0"),
    ("WEO_2026_APR_VINTAGE", "1.0.0"),
    ("WEO", "9.0.0"),
)
FIXTURE_DBN_VINTAGES = ("2008-04", "2009-10")
FIXTURE_IMF_FLOWS = (("WEO_2025_OCT_VINTAGE", "1.0.0"),)
DBN_PAGE = 1000
MAX_DBN_BYTES = 8_000_000
MAX_IMF_BYTES = 20_000_000
DBN_LICENCE = (
    "IMF Copyright and Usage terms (https://www.imf.org/external/terms.htm), redistributed by DBnomics "
    "(https://db.nomics.world, data under the provider's terms). Cite: International Monetary Fund, World "
    "Economic Outlook database, <release>; via DBnomics."
)
VINTAGE_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class WeoVintageError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> list[AtlasSourceSpec]:
    common: dict[str, Any] = {
        "version": VERSION,
        "atlas_source": ATLAS_SOURCE,
        "cadence_seconds": 90 * 86_400,
        "parser_version": PARSER_VERSION,
        "code_digest": code_digest(),
    }
    return [
        AtlasSourceSpec(
            name=NAME_DBN,
            source_key="dbnomics.api",
            provider="International Monetary Fund, World Economic Outlook database releases (via DBnomics)",
            licence=DBN_LICENCE,
            allowed_hosts=(DBN_HOST,),
            datasets=(DS_DBN,),
            dataset_ids=tuple(f"IMF/WEO:{v}" for v in DBN_VINTAGES),
            planned_requests=len(DBN_VINTAGES) + 4,
            max_response_bytes=MAX_DBN_BYTES,
            min_interval_seconds=1.0,
            field_mappings={
                "atlas_tables": [f"{ATLAS_SOURCE}.{DS_DBN}"],
                "geo_scheme": GEO_SCHEME,
                "subjects": sorted(SUBJECTS),
            },
            notes="one DBnomics series request per WEO vintage (5 subjects x all countries); vintage folded in indicator",
            **common,
        ),
        AtlasSourceSpec(
            name=NAME_IMF,
            source_key=atlas_imf.SOURCE_KEY,
            provider="International Monetary Fund, World Economic Outlook vintage dataflows (IMF data portal)",
            licence=atlas_imf.LICENCE,
            allowed_hosts=(atlas_imf.HOST,),
            datasets=(DS_IMF,),
            dataset_ids=tuple(f"IMF.RES:{f}({v})" for f, v in IMF_FLOWS),
            planned_requests=len(IMF_FLOWS) + 1,
            max_response_bytes=MAX_IMF_BYTES,
            min_interval_seconds=1.0,
            field_mappings={
                "atlas_tables": [f"{ATLAS_SOURCE}.{DS_IMF}"],
                "geo_scheme": GEO_SCHEME,
                "subjects": sorted(SUBJECTS),
            },
            notes="IMF portal WEO vintage dataflows; PUBLICATION_DATE = available_at",
            **common,
        ),
    ]


def rights(dbnomics: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "licence": "IMF Copyright and Usage terms",
        "licence_url": "https://www.imf.org/external/terms.htm",
        "provider_licence_attribute": "(c) International Monetary Fund Copyright. All Rights Reserved.",
        "attribution": "International Monetary Fund, World Economic Outlook database (historical releases)",
        "open_licence": False,
        "restriction": "IMF terms: free use with attribution; not an open licence (redistribution per IMF terms)",
        "licence_checked": "2026-10-17",
    }
    if dbnomics:
        out["redistributor"] = "DBnomics (https://db.nomics.world), CEPREMAP; data under the provider's terms"
    return out


# --------------------------------------------------------------------------- DBnomics
def dbn_url(vintage: str, offset: int = 0) -> str:
    if not VINTAGE_RE.match(vintage):
        raise ValueError(f"bad WEO vintage label {vintage!r}")
    dims = json.dumps({"weo-subject": sorted(SUBJECTS)}, separators=(",", ":"))
    q = {"dimensions": dims, "observations": "1", "limit": str(DBN_PAGE)}
    if offset:
        q["offset"] = str(offset)
    return f"{DBN_BASE}/WEO:{vintage}?{urlencode(q)}"


def dbn_available_at(vintage: str) -> datetime:
    m = VINTAGE_RE.match(vintage)
    if not m:
        raise WeoVintageError(f"bad vintage label {vintage!r}")
    return first_of_next_month(int(m.group(1)), int(m.group(2)))


def parse_dbn_page(body: bytes, vintage: str) -> tuple[list[dict[str, Any]], int]:
    """(series docs, num_found) of one DBnomics series page, values kept as text."""
    try:
        doc = json.loads(body, parse_float=str, parse_int=str)
    except ValueError as e:
        raise WeoVintageError(f"DBnomics WEO:{vintage}: not JSON ({e})") from e
    if doc.get("errors"):
        raise WeoVintageError(f"DBnomics WEO:{vintage}: errors {str(doc['errors'])[:300]}")
    s = doc.get("series") or {}
    docs = s.get("docs")
    if not isinstance(docs, list):
        raise WeoVintageError(f"DBnomics WEO:{vintage}: no series.docs")
    for d in docs:
        if d.get("dataset_code") != f"WEO:{vintage}":
            raise WeoVintageError(f"DBnomics answered dataset {d.get('dataset_code')!r} for WEO:{vintage}")
    return docs, int(s.get("num_found", len(docs)))


def parse_dbn(docs: list[dict[str, Any]], vintage: str, raw_sha: str, rows: Rows, stats: dict[str, int]) -> None:
    vy = int(vintage[:4])
    avail = dbn_available_at(vintage)
    for d in docs:
        dims = d.get("dimensions") or {}
        subj, geo, unit = dims.get("weo-subject"), dims.get("weo-country"), dims.get("unit")
        if subj not in SUBJECTS:
            raise WeoVintageError(f"WEO:{vintage}: unrequested subject {subj!r}")
        if not geo or not unit:
            raise WeoVintageError(f"WEO:{vintage}: series {d.get('series_code')} lacks country/unit")
        if d.get("@frequency") not in (None, "annual"):
            stats["non_annual_series"] = stats.get("non_annual_series", 0) + 1
            continue
        variable, _definition = SUBJECTS[subj]
        periods, values = d.get("period") or [], d.get("value") or []
        if len(periods) != len(values):
            raise WeoVintageError(f"WEO:{vintage}: series {d.get('series_code')} period/value lengths differ")
        for i, (p, v) in enumerate(zip(periods, values, strict=True)):
            if not re.fullmatch(r"\d{4}", str(p)):
                raise WeoVintageError(f"WEO:{vintage}: non-annual period {p!r}")
            val = decimal_or_none(v)
            if val is None:
                stats["no_value"] = stats.get("no_value", 0) + 1
                continue
            y = int(p)
            kind = "projection" if y >= vy else "published_history_boundary_unknown"
            s, e = year_bounds(y)
            rows.add(
                source=ATLAS_SOURCE,
                dataset=DS_DBN,
                indicator=indicator(subj, vintage),
                provider_geo=str(geo),
                start=s,
                end=e,
                value=val,
                unit=str(unit),
                flags=vintage_flags(
                    family="imf_weo",
                    vintage=vintage,
                    available_at=avail,
                    basis="weo_release_month_end",
                    variable=variable,
                    provider_variable=subj,
                    estimate_kind=kind,
                    release="IMF World Economic Outlook database " + vintage,
                    copy="DBnomics IMF/WEO:" + vintage,
                ),
                raw_sha256=raw_sha,
                selector={
                    "dataset": f"IMF/WEO:{vintage}",
                    "series_code": d.get("series_code"),
                    "index": i,
                    "period": p,
                },
            )
            stats["cells"] = stats.get("cells", 0) + 1


# --------------------------------------------------------------------------- IMF portal
def imf_url(flow: str, version: str) -> str:
    return f"{IMF_BASE}/data/IMF.RES,{flow},{version}/.{'+'.join(sorted(SUBJECTS))}.A"


def parse_imf(body: bytes, raw_sha: str, flow: str, version: str, rows: Rows, stats: dict[str, int]) -> dict[str, Any]:
    msg = atlas_imf.parse_sdmx(body)
    if msg.structure.get("id") != flow or msg.structure.get("version") != version:
        raise WeoVintageError(f"IMF answered {msg.flow_ref} for {flow}({version})")
    vint = atlas_imf.weo_vintage(msg)
    pub = vint["publication_date"]
    try:
        avail = datetime.fromisoformat(pub.replace("Z", "+00:00"))
    except ValueError as e:
        raise WeoVintageError(f"{flow}: PUBLICATION_DATE {pub!r} is not an instant") from e
    if avail.tzinfo is None:
        raise WeoVintageError(f"{flow}: PUBLICATION_DATE {pub!r} has no time zone")
    frame, st = atlas_imf.parse_weo(msg, raw_sha, item_key=f"{flow}({version})")
    vintage = vint["vintage"]
    for r in frame.iter_rows(named=True):
        f = json.loads(r["flags"])
        y = int(f["label_year"]) if f.get("label_year") is not None else r["period_start"].year
        if r["period_start"] != date(y, 1, 1) and not f.get("period_basis"):
            raise WeoVintageError(f"{flow}: unexpected non-calendar period without basis")
        ek = f.get("estimate_kind")
        kind = {"projection": "projection", "unknown_boundary": "published_history_boundary_unknown"}.get(
            ek or "", "actual"
        )
        if ek not in (None, "projection", "unknown_boundary"):
            raise WeoVintageError(f"{flow}: unexpected estimate_kind {ek!r}")
        p0, p1 = year_bounds(y)
        rows.add(
            source=ATLAS_SOURCE,
            dataset=DS_IMF,
            indicator=indicator(r["indicator"], vintage),
            provider_geo=r["provider_geo"],
            start=s,
            end=e,
            value=r["value"],
            unit=r["unit"],
            flags=vintage_flags(
                family="imf_weo",
                vintage=vintage,
                vintage_date_text=pub,
                available_at=avail,
                basis="imf_publication_date_attribute",
                variable=SUBJECTS[r["indicator"]][0],
                provider_variable=r["indicator"],
                estimate_kind=kind,
                release=vint["vintage_label"],
                flow=vint["flow"],
                latest_actual=f.get("latest_actual"),
                boundary_rule=f.get("boundary_rule"),
                period_basis=f.get("period_basis"),
                fiscal_window=(
                    [str(r["period_start"]), str(r["period_end"])] if f.get("period_basis") == "fiscal_year" else None
                ),
            ),
            raw_sha256=raw_sha,
            selector=json.loads(r["selector"]),
        )
        stats["cells"] = stats.get("cells", 0) + 1
    stats["no_value"] = stats.get("no_value", 0) + st.skipped_no_value
    return vint


# --------------------------------------------------------------------------- ingest
def _note(ds: str, frame: pl.DataFrame, stats: dict[str, Any]) -> str:
    cov = coverage_line(frame)
    if ds == DS_DBN:
        head = (
            "IMF WEO database releases as published (DBnomics copies IMF/WEO:<YYYY-MM>), subjects "
            + ", ".join(f"{k} ({v[1]})" for k, v in SUBJECTS.items())
            + ". available_at = first instant of the month after the release month (release day not in the copy). "
            "Years >= vintage year: projection; earlier years: published_history_boundary_unknown."
        )
    else:
        head = (
            "IMF WEO vintage dataflows of the IMF data portal (api.imf.org); available_at = PUBLICATION_DATE; "
            "actual/projection from LATEST_ACTUAL_ANNUAL_DATA. Periods are WEO year labels (fiscal windows in flags)."
        )
    return (f"{head} Coverage: {json.dumps(cov)}. Parse: {json.dumps(stats, default=str)}.")[:4000]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    fixture = run.origin == "test_fixture"
    out: dict[str, Any] = {"tables": {}, "failures": [], "requests": 0}
    rows, stats, raws = Rows(), dict[str, int](), []
    if run.spec.name == NAME_DBN:
        vintages = tuple(run.options.get("vintages") or (FIXTURE_DBN_VINTAGES if fixture else DBN_VINTAGES))
        for v in vintages:
            try:
                offset, got, total = 0, 0, None
                while total is None or got < total:
                    raw = await run.get(
                        dbn_url(v, offset),
                        key=f"weo:{v}:{offset}",
                        media_types=("application/json",),
                        max_bytes=MAX_DBN_BYTES,
                        params={"vintage": v, "offset": offset},
                    )
                    out["requests"] += 1
                    sha = run.archive(raw)
                    raws.append(sha)
                    docs, total = parse_dbn_page(raw.body, v)
                    parse_dbn(docs, v, sha, rows, stats)
                    got += len(docs)
                    offset += DBN_PAGE
                    if not docs:
                        break
            except (SourceResponseError, ValueError) as e:
                out["failures"].append({"vintage": v, "error": str(e)[:300]})
        ds, dbn = DS_DBN, True
    elif run.spec.name == NAME_IMF:
        flows = FIXTURE_IMF_FLOWS if fixture else IMF_FLOWS
        out["vintages"] = []
        for flow, version in flows:
            try:
                raw = await run.get(
                    imf_url(flow, version),
                    key=f"weo:{flow}:{version}",
                    media_types=("application/vnd.sdmx.structurespecificdata+xml", "application/xml"),
                    headers={"Accept": atlas_imf.ACCEPT},
                    max_bytes=MAX_IMF_BYTES,
                    params={"flow": flow, "version": version},
                )
                out["requests"] += 1
                sha = run.archive(raw)
                raws.append(sha)
                out["vintages"].append(parse_imf(raw.body, sha, flow, version, rows, stats))
            except (SourceResponseError, ValueError) as e:
                out["failures"].append({"flow": f"{flow}({version})", "error": str(e)[:300]})
        ds, dbn = DS_IMF, False
    else:  # pragma: no cover - discover() only hands this module its own specs
        raise ValueError(run.spec.name)
    if not len(rows):
        out["status"] = "no_cells"
        return out
    frame = rows.frame()
    reg = run.register(
        dataset=ds,
        frame=frame,
        raw_sha256s=sorted(set(raws)),
        coverage_note=_note(ds, frame, stats),
        geo_scheme=GEO_SCHEME,
        rights=rights(dbn),
    )
    out["tables"][ds] = {
        "dataset_key": reg["dataset_key"],
        "rows": reg["row_count"],
        "created": reg["created"],
        "artifact_sha256": reg["artifact_sha256"],
        "coverage": coverage_line(frame),
    }
    out["stats"] = stats
    return out


def available_at_of(vintage: str, *, imf_publication_date: str | None = None) -> datetime:
    """The availability instant this module assigns to a WEO vintage (tests, report)."""
    if imf_publication_date:
        return datetime.fromisoformat(imf_publication_date.replace("Z", "+00:00")).astimezone(UTC)
    return dbn_available_at(vintage)


__all__ = ["DBN_VINTAGES", "IMF_FLOWS", "SUBJECTS", "atlas_ingest", "atlas_source", "parse_dbn", "parse_imf"]
