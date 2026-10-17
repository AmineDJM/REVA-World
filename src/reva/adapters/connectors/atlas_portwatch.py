"""IMF PortWatch: daily chokepoint transits, monthly port activity per country, ports and chokepoints (V6INFRA).

Source ``atlas.imf_portwatch`` (``source_key`` ``imf.portwatch``). PortWatch (IMF + University of Oxford) derives
port calls and chokepoint transits from AIS vessel positions and **estimates** the cargo carried (metric tons) from
vessel characteristics (Arslanalp, Koepke & Verschuur, IMF WP/21/225). Data are hosted as ArcGIS Online layers of
the organisation ``IMF-portwatch_imf_dataviz``; every item names its licence as the IMF terms page
``https://www.imf.org/external/terms.htm`` (that page answers HTTP 403 to the container, as it did for A-IMF), so
rows carry ``licence_review: pending`` (owner decision in ``docs/dev/v6_infra.md``). The item metadata (description
with the provider's definitions, licence field) is archived on every run and the licence field is checked.

Definitions (quoted from the item descriptions, archived):

* transit call: "a transit call is defined when a ship transits through the chokepoint boundary. Transits that span
  across several days are counted only once and a threshold of 48 hours is applied before counting the same ship
  again."
* port call: "a port call is defined when a ship enters the port boundary. Port calls with a turnaround time of less
  than 5 hours and no draft change between the current and next port are excluded to filter out vessels in transit"
* trade volume / import / export: model estimates in metric tons ("we use the vessel information (length, width,
  draft, capacity, block coefficient) to estimate the payload (or utilization rate) of the vessel") -> every such row
  carries ``flags.estimate_kind = "model_estimate"``; counts carry ``"ais_derived_count"``.

Atlas tables (``iso3`` scheme):

* ``portwatch.chokepoints_daily`` -- the published ``Daily_Chokepoints_Data`` layer (one CSV download from the ArcGIS
  Hub): per chokepoint (``provider_geo`` = PortWatch ``portid``, ``iso3`` null: a strait is not a country) and UTC day,
  ``TRANSITS|vessel_type=<t>`` (``n_*``) and ``TRADE_VOLUME_EST|vessel_type=<t>`` (``capacity_*``, metric tons).
  Values verbatim (provider integers; a provider zero is a reported zero, a blank is no row).
* ``portwatch.country_monthly`` -- monthly sums per country of the published ``Daily_Ports_Data`` layer (port x day,
  5.9 M rows, too large to download here), computed **by the provider's ArcGIS query service** at REVA's request
  (``outStatistics`` sum grouped by ``ISO3, year, month``): ``PORT_CALLS``, ``IMPORT_VOLUME_EST``,
  ``EXPORT_VOLUME_EST``; ``flags.derived = "sum"`` with the request, ``flags.port_days`` = rows summed. The sum covers
  the ports PortWatch tracks in the country (not every port). The month holding the layer's last date is flagged
  ``partial_period``.

KG (source key ``imf.portwatch``): ``maritime_chokepoint`` entities ``portwatch:chokepoint<n>`` (``P625``,
``infra:measured_by`` -> ``atlas:portwatch.chokepoints_daily``) and ``port`` entities ``portwatch:port<n>`` from the
published ports database (``P625``, ``P17`` from the provider's ISO3, ``portwatch:locode`` verbatim and
``portwatch:at_unlocode`` -> ``unlocode:<code>``, ``portwatch:in_adm1`` by REVA point in polygon / nearest coastal
polygon, :mod:`v6infra_common`). Port <-> WPI port and chokepoint <-> Wikidata strait are **not** merged: no shared
identifier is published (a port and a WPI port meet only through the UN/LOCODE both publish).

    uv run reva atlas ingest atlas.imf_portwatch [--dataset chokepoints_daily]
"""

from __future__ import annotations

import csv
import functools
import io
import json
import re
import sys
from collections import Counter
from datetime import date
from typing import Any
from urllib.parse import urlencode

from reva.adapters.connectors import atlas_infra_common as ic
from reva.adapters.connectors import v6infra_common as vc
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.imf_portwatch"
VERSION = "1.0.0"
SOURCE_KEY = "imf.portwatch"
ATLAS_SOURCE = "portwatch"
DS_CHOKE = "chokepoints_daily"
DS_COUNTRY = "country_monthly"
PARSER_VERSION = "atlas_imf_portwatch/1"
SERVICES = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
ITEMS = "https://www.arcgis.com/sharing/rest/content/items"
HUB = "https://hub.arcgis.com/api/v3/datasets"
ITEM_CHOKE_DAILY = "3da2b9ca97684916b75c4013f95d18ab"
ITEM_PORTS_DAILY = "83b1bbc7b3354c5fb1f40673bb8f852e"
ITEM_PORTS_DB = "acc668d199d1472abaaf2467133d4ca4"
ITEM_CHOKE_DB = "fa9a5800b0ee4855af8b2944ab1e07af"
LICENCE_MARKER = "imf.org/external/terms.htm"
FIRST_YEAR = 2019
PAGE = 1000  # the layers' maxRecordCount (aggregated queries are capped by it)
MAX_BYTES = 30_000_000
MIN_CHOKE_ROWS = 50_000  # truncation guard (79,548 on 2026-10-17); trimmed fixtures lower it
MIN_PORTS = 1_500
COASTAL_DEG = 0.05
VESSEL_TYPES = ("container", "dry_bulk", "general_cargo", "roro", "tanker", "cargo", "total")
RIGHTS: dict[str, Any] = {
    "licence": "IMF Copyright and Usage terms (https://www.imf.org/external/terms.htm), named in every PortWatch item's "
    "licence field; the terms page answers HTTP 403 to this container and could not be quoted",
    "licence_status": "terms_named_not_quoted",
    "licence_review": "pending owner decision (docs/dev/v6_infra.md)",
    "attribution": "IMF PortWatch (portwatch.imf.org); Arslanalp, Koepke & Verschuur (2021), Tracking Trade from "
    "Space: An Application to Pacific Island Countries, IMF Working Paper 2021/225",
    "upstream": "AIS vessel positions (UN Global Platform) processed by the IMF; cargo volumes are model estimates",
}
FLAG_RIGHTS = {"licence_review": "pending", "licence_url": "https://www.imf.org/external/terms.htm"}
QUOTES = {
    "transit": "a transit call is defined when a ship transits through the chokepoint boundary",
    "port_call": "a port call is defined when a ship enters the port boundary",
    "estimate": "to estimate the payload (or utilization rate) of the vessel",
}
KG_PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "portwatch:locode": (
        "external_id",
        "portwatch_locode",
        "UN/LOCODE published by PortWatch",
        "UN/LOCODE publié par PortWatch",
    ),
    "portwatch:at_unlocode": (
        "item",
        "portwatch_at_unlocode",
        "at UN/LOCODE location (code published by PortWatch)",
        "au lieu UN/LOCODE (code publié par PortWatch)",
    ),
    "portwatch:in_adm1": (
        "item",
        "portwatch_in_adm1",
        "located in first-level subdivision (REVA point in polygon)",
        "situé dans la subdivision de premier niveau (point dans polygone, REVA)",
    ),
}
WIKIDATA_PIDS = ("P17", "P625")


def _err(msg: str, kind: str = "schema_drift") -> SourceResponseError:
    return SourceResponseError(msg, retryable=False, kind=kind)


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__], vc, vc.gh)


def atlas_source() -> AtlasSourceSpec:
    years = date.today().year - FIRST_YEAR + 1
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="IMF PortWatch (ArcGIS Online organisation IMF-portwatch_imf_dataviz): daily chokepoint transits, "
        "daily port calls and trade estimates, ports and chokepoints databases",
        licence=RIGHTS["licence"] + "; flagged for maintainer review",
        allowed_hosts=("services9.arcgis.com", "www.arcgis.com", "hub.arcgis.com", vc.NE_HOST),
        datasets=(DS_CHOKE, DS_COUNTRY),
        dataset_ids=tuple(
            f"arcgis:item:{i}" for i in (ITEM_CHOKE_DAILY, ITEM_PORTS_DAILY, ITEM_PORTS_DB, ITEM_CHOKE_DB)
        ),
        cadence_seconds=7 * ic.DAY,
        # 4 item metadata + 1 CSV + 1 date range + ~3 aggregate pages per year + 3 ports pages + 1 chokepoints + NE
        planned_requests=4 + 1 + 1 + 3 * years + 3 + 1 + 1,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="Daily chokepoint transits (AIS) and model-estimated trade volumes; monthly port calls per country "
        "summed by the provider's query service; ports/chokepoints in the KG. Licence (IMF terms) flagged for review.",
    )


# --------------------------------------------------------------------------- URLs
def item_url(item: str) -> str:
    return f"{ITEMS}/{item}?f=json"


def choke_csv_url() -> str:
    return f"{HUB}/{ITEM_CHOKE_DAILY}_0/downloads/data?format=csv&spatialRefId=4326&where=1%3D1"


def _query(layer: str, **params: Any) -> str:
    return f"{SERVICES}/{layer}/FeatureServer/0/query?" + urlencode({**params, "f": "json"})


STATS = [
    {"statisticType": "sum", "onStatisticField": "portcalls", "outStatisticFieldName": "portcalls"},
    {"statisticType": "sum", "onStatisticField": "import", "outStatisticFieldName": "import_t"},
    {"statisticType": "sum", "onStatisticField": "export", "outStatisticFieldName": "export_t"},
    {"statisticType": "count", "onStatisticField": "ObjectId", "outStatisticFieldName": "port_days"},
]


def date_range_url() -> str:
    st = [{"statisticType": t, "onStatisticField": "date", "outStatisticFieldName": t} for t in ("min", "max")]
    return _query("Daily_Ports_Data", where="1=1", outStatistics=json.dumps(st, separators=(",", ":")))


def monthly_url(year: int, offset: int) -> str:
    return _query("Daily_Ports_Data", where=f"year={year}", groupByFieldsForStatistics="ISO3,year,month",
                  outStatistics=json.dumps(STATS, separators=(",", ":")), orderByFieldsForStatistics="ISO3,year,month",
                  resultOffset=offset, resultRecordCount=PAGE)  # fmt: skip


def ports_url(layer: str, offset: int) -> str:
    return _query(layer, where="1=1", outFields="portid,portname,country,ISO3,fullname,lat,lon,LOCODE",
                  returnGeometry="false", orderByFields="portid", resultOffset=offset, resultRecordCount=PAGE)  # fmt: skip


# --------------------------------------------------------------------------- parsing
def check_item(f: ic.Fetched) -> dict[str, Any]:
    try:
        d = json.loads(f.body)
    except json.JSONDecodeError as e:
        raise _err(f"ArcGIS item {f.url}: not JSON") from e
    if "error" in d:
        raise _err(f"ArcGIS item {f.url}: {str(d['error'])[:200]}")
    if LICENCE_MARKER not in str(d.get("licenseInfo") or ""):
        raise _err(f"PortWatch item {d.get('id')} no longer names the IMF terms as its licence", kind="licence")
    return {"id": d.get("id"), "title": d.get("title"), "modified": d.get("modified"), "owner": d.get("owner")}


def _features(f: ic.Fetched) -> tuple[list[dict[str, Any]], bool]:
    try:
        d = json.loads(f.body)
    except json.JSONDecodeError as e:
        raise _err(f"ArcGIS query {f.url[:120]}: not JSON") from e
    if "error" in d:
        raise _err(f"ArcGIS query error: {str(d['error'])[:300]}", kind="provider_error")
    return [x["attributes"] for x in d.get("features") or []], bool(d.get("exceededTransferLimit"))


def build_chokepoints(
    raw: ic.Fetched, item: ic.Fetched, *, min_rows: int | None = None
) -> tuple[ic.RowBuilder, dict[str, Any]]:
    text = raw.body.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    need = {"date", "portid", "portname"} | {f"n_{t}" for t in VESSEL_TYPES[:-1]} | {"n_total", "capacity"}
    if not rows or not need <= set(rows[0]):
        raise _err(f"chokepoints CSV lacks columns {sorted(need - set(rows[0] if rows else {}))}")
    if len(rows) < (MIN_CHOKE_ROWS if min_rows is None else min_rows):
        raise _err(f"chokepoints CSV has only {len(rows)} rows (truncated?)", kind="incomplete")
    desc = item.body.decode("utf-8", "replace")
    for k in ("transit", "estimate"):
        if QUOTES[k] not in desc:
            raise _err(f"chokepoints item description no longer states: {QUOTES[k]!r}")
    rb = ic.RowBuilder(ATLAS_SOURCE, DS_CHOKE)
    rep: Counter[str] = Counter()
    names: dict[str, str] = {}
    days: list[date] = []
    for i, r in enumerate(rows):
        m = re.match(r"^(\d{4})/(\d{2})/(\d{2})", r["date"].strip())
        if not m:
            raise _err(f"chokepoints CSV row {i}: date {r['date']!r}")
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if (d.year, d.month, d.day) != (int(r["year"]), int(r["month"]), int(r["day"])):
            raise _err(f"chokepoints CSV row {i}: date and year/month/day columns disagree")
        days.append(d)
        cp = r["portid"].strip()
        names.setdefault(cp, r["portname"].strip())
        for t in VESSEL_TYPES:
            for col, ind, unit, kind, q in (
                (f"n_{t}", f"TRANSITS|vessel_type={t}", "vessels", "ais_derived_count", QUOTES["transit"]),
                ("capacity" if t == "total" else f"capacity_{t}", f"TRADE_VOLUME_EST|vessel_type={t}", "metric tons",
                 "model_estimate", QUOTES["estimate"]),
            ):  # fmt: skip
                v = (r.get(col) or "").strip()
                if v == "":
                    rep["blank_cells"] += 1
                    continue
                if not re.match(r"^-?\d+$", v):
                    raise _err(f"chokepoints CSV row {i}: {col} = {v!r} is not an integer")
                rb.rows.append(_row(rb, ind, cp, (d, ic.day_span(d)[1]), v, unit, raw.sha256,
                                    {"file": "Daily_Chokepoints_Data.csv", "row": i, "column": col},
                                    {**FLAG_RIGHTS, "estimate_kind": kind, "definition": q, "chokepoint_name": names[cp],
                                     "provider_column": col, "time_basis": "UTC day"}))  # fmt: skip
                rep["zeros" if int(v) == 0 else "cells"] += 1
    report = {"rows": len(rows), "chokepoints": len(names), "first_day": min(days).isoformat(),
              "last_day": max(days).isoformat(), **dict(rep)}  # fmt: skip
    return rb, report


def _row(rb: ic.RowBuilder, indicator: str, geo: str, period: tuple[date, date], value: str, unit: str, sha: str,
         selector: dict[str, Any], flags: dict[str, Any], iso3: str | None = None) -> dict[str, Any]:  # fmt: skip
    key = (indicator, geo, period[0], period[1])
    if key in rb._seen:
        raise _err(f"duplicate atlas cell {key}")
    rb._seen.add(key)
    return {"source": rb.source, "dataset": rb.dataset, "indicator": indicator, "concept_id": None, "iso3": iso3,
            "provider_geo": geo, "entity_qid": None, "period_start": period[0], "period_end": period[1], "value": value,
            "value_text": None, "unit": unit, "flags": ic.canon(flags), "raw_sha256": sha, "selector": ic.canon(selector)}  # fmt: skip


def build_country_monthly(pages: list[tuple[ic.Fetched, list[dict[str, Any]]]], last_day: date, item: ic.Fetched) -> tuple[ic.RowBuilder, dict[str, Any]]:  # fmt: skip
    from reva.atlas.tables import country_iso3

    desc = item.body.decode("utf-8", "replace")
    for k in ("port_call", "estimate"):
        if QUOTES[k] not in desc:
            raise _err(f"Daily_Ports_Data item description no longer states: {QUOTES[k]!r}")
    rb = ic.RowBuilder(ATLAS_SOURCE, DS_COUNTRY)
    rep: Counter[str] = Counter()
    geos: set[str] = set()
    for f, feats in pages:
        for a in feats:
            geo, y, mo = str(a.get("ISO3") or "").strip(), a.get("year"), a.get("month")
            if not geo or not isinstance(y, int) or not isinstance(mo, int):
                rep["groups_without_country_or_month"] += 1
                continue
            geos.add(geo)
            iso3 = country_iso3(geo, "iso3")
            span = ic.month_span(y, mo)
            partial = span[1] > last_day
            for field, ind, unit, kind, q in (
                ("portcalls", "PORT_CALLS", "port calls", "ais_derived_count", QUOTES["port_call"]),
                ("import_t", "IMPORT_VOLUME_EST", "metric tons", "model_estimate", QUOTES["estimate"]),
                ("export_t", "EXPORT_VOLUME_EST", "metric tons", "model_estimate", QUOTES["estimate"]),
            ):
                v = a.get(field)
                if v is None:
                    rep["null_sums"] += 1
                    continue
                if isinstance(v, float) and v.is_integer():  # the service types sums as double
                    v = int(v)
                if not isinstance(v, int) or isinstance(v, bool):
                    raise _err(f"monthly sum {field} for {geo} {y}-{mo} is not an integer: {v!r}")
                src = {"portcalls": "portcalls", "import_t": "import", "export_t": "export"}[field]
                fl = {**FLAG_RIGHTS, "derived": "sum", "estimate_kind": kind, "definition": q,
                      "formula": f"sum of Daily_Ports_Data.{src} over the country's PortWatch ports and the month's days, "
                      "computed by the provider's ArcGIS query service (outStatistics, groupBy ISO3, year, month)",
                      "port_days": a.get("port_days"), "coverage": "ports tracked by PortWatch (not every port)",
                      "time_basis": "UTC days"}  # fmt: skip
                if partial:
                    fl["partial_period"] = f"layer's last date is {last_day.isoformat()}"
                rb.rows.append(_row(rb, ind, geo, span, str(v), unit, f.sha256,
                                    {"layer": "Daily_Ports_Data", "group": {"ISO3": geo, "year": y, "month": mo},
                                     "statistic": "sum", "field": src}, fl, iso3))  # fmt: skip
                rep["cells"] += 1
    report = {"groups_cells": rep["cells"], "geographies": len(geos), "last_day": last_day.isoformat(),
              "unmapped": sorted(g for g in geos if country_iso3(g, "iso3") is None), **dict(rep)}  # fmt: skip
    return rb, report


def build_kg(ports: list[dict[str, Any]], chokes: list[dict[str, Any]], port_raws: list[ic.Fetched],
             choke_raw: ic.Fetched, idx: vc.Adm1Index | None, qids: dict[str, str], choke_table: bool) -> vc.KgBuilder:  # fmt: skip
    from reva.atlas.tables import country_iso3

    kb = vc.KgBuilder(SOURCE_KEY, f"portwatch-{choke_raw.sha256[:16]}")
    cref = [
        vc.kg_ref(choke_raw, file="PortWatch_chokepoints_database", licence=RIGHTS["licence"], licence_review="pending")
    ]
    series = (
        kb.data_series(f"{ATLAS_SOURCE}.{DS_CHOKE}", "IMF PortWatch daily chokepoint transits") if choke_table else None
    )
    for a in chokes:
        pid = str(a.get("portid") or "")
        if not re.match(r"^chokepoint\d+$", pid):
            kb.counts["chokepoint_bad_id"] += 1
            continue
        qid = f"portwatch:{pid}"
        kb.entity(qid, "maritime_chokepoint", str(a.get("fullname") or a.get("portname") or pid),
                  aliases=[str(a.get("portname") or ""), pid], description=f"IMF PortWatch chokepoint {pid}")  # fmt: skip
        lat, lon = vc.decimal_text(str(a.get("lat"))), vc.decimal_text(str(a.get("lon")))
        if lat and lon:
            kb.st(qid, "P625", raw=choke_raw.sha256, refs=cref, value=vc.coordinate_value(lat, lon),
                  quals={"provider_fields": "lat, lon"})  # fmt: skip
        if series:
            kb.st(qid, vc.MEASURED_BY, raw=choke_raw.sha256, refs=cref, obj=series,
                  quals={"table": f"{ATLAS_SOURCE}.{DS_CHOKE}", "provider_geo": pid})  # fmt: skip
    by_sha = {f.sha256: f for f in port_raws}
    for a in ports:
        pid = str(a.get("portid") or "")
        sha = str(a.get("_raw_sha256"))
        if not re.match(r"^(port|fso)\d+$", pid):  # fso<n>: offshore units listed in the ports database
            kb.counts["port_bad_id"] += 1
            continue
        f = by_sha[sha]
        ref = [vc.kg_ref(f, file="PortWatch_ports_database", licence=RIGHTS["licence"], licence_review="pending")]
        qid = f"portwatch:{pid}"
        kb.entity(qid, "port", str(a.get("fullname") or a.get("portname") or pid),
                  aliases=[str(a.get("portname") or ""), pid],
                  description=f"IMF PortWatch port {pid} ({a.get('country') or 'country not stated'})")  # fmt: skip

        def st(p: str, *, obj: str | None = None, value: dict[str, Any] | None = None, quals: dict[str, Any] | None = None,
               refs: list[dict[str, Any]] = ref, qid: str = qid, sha: str = sha) -> None:  # fmt: skip
            kb.st(qid, p, raw=sha, refs=refs, obj=obj, value=value, quals=quals)

        geo = str(a.get("ISO3") or "").strip()
        iso3 = country_iso3(geo, "iso3") if geo else None
        cq = vc.country_qid(iso3, qids)
        iso2 = _iso2(iso3) if iso3 else None
        lat, lon = vc.decimal_text(str(a.get("lat"))), vc.decimal_text(str(a.get("lon")))
        place = (vc.place_facility(idx, country_iso2=iso2, lat=lat, lon=lon, coastal_deg=COASTAL_DEG)
                 if lat and lon and iso2 else None)  # fmt: skip
        if cq:
            pq: dict[str, Any] = {"basis": "PortWatch ISO3 field", "provider_iso3": geo}
            if place is not None and "coordinates_fall_in" in place.quals:
                pq["coordinates_fall_in"] = place.quals["coordinates_fall_in"]
            st("P17", obj=cq, quals=pq)
        else:
            kb.counts["country_unmapped_or_no_qid"] += 1
        if lat and lon:
            st("P625", value=vc.coordinate_value(lat, lon), quals={"provider_fields": "lat, lon"})
        lo = str(a.get("LOCODE") or "").strip()
        if lo:
            st("portwatch:locode", value={"type": "external_id", "value": lo})
            m = re.match(r"^([A-Z]{2}) ?([A-Z0-9]{3})$", lo)
            if m:
                st("portwatch:at_unlocode", obj=f"unlocode:{m.group(1)}{m.group(2)}",
                   quals={"basis": "UN/LOCODE published in the PortWatch LOCODE field", "locode_text": lo})  # fmt: skip
            else:
                kb.counts["locode_malformed"] += 1
        if place is not None:
            if place.code and idx is not None:
                st("portwatch:in_adm1", obj=f"iso3166_2:{place.code}", quals=place.quals, refs=[*ref, idx.ref()])
            kb.counts[f"adm1_{place.reason}"] += 1
    return kb


def _iso2(iso3: str) -> str | None:
    """ISO alpha-2 of a crosswalk key (same-country check of ADM1 placement); None unless exactly one code maps to it."""
    hits = _iso2_inverse().get(iso3, [])
    return hits[0] if len(hits) == 1 else None


@functools.lru_cache(maxsize=1)
def _iso2_inverse() -> dict[str, list[str]]:
    from reva.atlas.tables import geo_index

    inv: dict[str, list[str]] = {}
    for c, k in geo_index().maps["iso2"].items():
        if len(c) == 2:
            inv.setdefault(k, []).append(c)
    return inv


# --------------------------------------------------------------------------- ingest
async def _get(
    run: Any, url: str, name: str, key: str, media: tuple[str, ...] = ("application/json", "text/plain")
) -> ic.Fetched:
    return await ic.fetch_archived(run, url, name=name, key=key, media_types=media, max_bytes=MAX_BYTES)


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    out: dict[str, Any] = {"ok": True, "tables": [], "reports": {}}
    items = {i: await _get(run, item_url(i), f"item_{i}.json", f"portwatch:item:{i}")
             for i in (ITEM_CHOKE_DAILY, ITEM_PORTS_DAILY, ITEM_PORTS_DB, ITEM_CHOKE_DB)}  # fmt: skip
    out["items"] = {i: check_item(f) for i, f in items.items()}
    choke_table = False
    if run.wants(DS_CHOKE):
        raw = await _get(run, choke_csv_url(), "Daily_Chokepoints_Data.csv", "portwatch:chokepoints_daily.csv",
                         ("text/csv", "application/octet-stream"))  # fmt: skip
        rb, rep = build_chokepoints(raw, items[ITEM_CHOKE_DAILY])
        reg = run.register(dataset=DS_CHOKE, frame=rb.frame(), raw_sha256s=[raw.sha256, items[ITEM_CHOKE_DAILY].sha256],
                           coverage_note=f"IMF PortWatch daily chokepoint transits and estimated trade volumes: "
                           f"{rep['chokepoints']} chokepoints, {rep['first_day']}..{rep['last_day']} (UTC days); volumes "
                           "are model estimates; licence (IMF terms) pending review",
                           geo_scheme="iso3", rights={**RIGHTS, "source_key": SOURCE_KEY})  # fmt: skip
        out["tables"].append(_table(reg))
        out["reports"][DS_CHOKE] = rep
        choke_table = True
    if run.wants(DS_COUNTRY):
        rng = await _get(run, date_range_url(), "Daily_Ports_Data_date_range.json", "portwatch:ports_daily:range")
        feats, _ = _features(rng)
        if len(feats) != 1 or not feats[0].get("max"):
            raise _err("Daily_Ports_Data date range query returned no maximum date")
        last_day = date.fromisoformat(str(feats[0]["max"])[:10])
        pages: list[tuple[ic.Fetched, list[dict[str, Any]]]] = []
        for year in range(FIRST_YEAR, last_day.year + 1):
            offset = 0
            while True:
                f = await _get(run, monthly_url(year, offset), f"Daily_Ports_Data_sum_{year}_{offset}.json",
                               f"portwatch:ports_daily:sum:{year}:{offset}")  # fmt: skip
                feats, more = _features(f)
                pages.append((f, feats))
                if not more or not feats:
                    break
                offset += len(feats)
        rb, rep = build_country_monthly(pages, last_day, items[ITEM_PORTS_DAILY])
        reg = run.register(dataset=DS_COUNTRY, frame=rb.frame(),
                           raw_sha256s=sorted({rng.sha256, items[ITEM_PORTS_DAILY].sha256, *(f.sha256 for f, _ in pages)}),
                           coverage_note=f"IMF PortWatch Daily_Ports_Data summed per country and month by the provider's "
                           f"query service ({FIRST_YEAR}-01..{rep['last_day']}; last month partial): port calls (AIS) and "
                           "estimated import/export volumes (model estimates) over the ports PortWatch tracks; "
                           "licence (IMF terms) pending review", geo_scheme="iso3",
                           rights={**RIGHTS, "source_key": SOURCE_KEY})  # fmt: skip
        out["tables"].append(_table(reg))
        out["reports"][DS_COUNTRY] = {k: v for k, v in rep.items() if k != "unmapped"} | {"unmapped": rep["unmapped"]}
    # KG: ports and chokepoints databases
    port_raws: list[ic.Fetched] = []
    ports: list[dict[str, Any]] = []
    offset = 0
    while True:
        f = await _get(run, ports_url("PortWatch_ports_database", offset), f"ports_db_{offset}.json",
                       f"portwatch:ports_db:{offset}")  # fmt: skip
        feats, more = _features(f)
        port_raws.append(f)
        ports += [{**a, "_raw_sha256": f.sha256} for a in feats]
        if not more or not feats:
            break
        offset += len(feats)
    if len(ports) < MIN_PORTS:
        raise _err(f"PortWatch ports database lists only {len(ports)} ports (truncated?)", kind="incomplete")
    choke_db = await _get(
        run, ports_url("PortWatch_chokepoints_database", 0), "chokepoints_db.json", "portwatch:chokepoints_db"
    )
    chokes, _ = _features(choke_db)
    idx = await vc.fetch_ne_admin1(run)
    kb = build_kg(ports, chokes, port_raws, choke_db, idx, load_country_qids(), choke_table)
    raws = sorted({choke_db.sha256, idx.fetched.sha256, *(f.sha256 for f in port_raws)})
    out["kg"] = vc.load(run, kb, KG_PREDICATES, WIKIDATA_PIDS, raws, choke_db.retrieved_at,
                        {"connector": NAME, "parser": PARSER_VERSION})  # fmt: skip
    out["kg"]["counts"] = dict(kb.counts)
    return out


def _table(out: dict[str, Any]) -> dict[str, Any]:
    return {"table": out["dataset_key"].removeprefix("atlas."), "rows": out["row_count"], "created": out["created"],
            "artifact_sha256": out["artifact_sha256"],
            "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []][:50]}  # fmt: skip
