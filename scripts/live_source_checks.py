#!/usr/bin/env python
"""Live source checks (implementation prompt §14 item 4, §17.4; architecture §10.4).

Runs every built-in connector against the real providers into a FRESH, uniquely
named PostgreSQL database (never the dev database ``reva``), publishes a live
belief release and writes a coverage matrix for the finite requirement
catalogue ``registry/requirements/mvp.yaml``.

* Origin is ``live``. Nothing is recorded into ``tests/fixtures``: connectors use
  the plain live client (``make_live_client``) wrapped in a pass-through tracer
  that only notes URL, status, byte size, SHA-256, headers and retrieval time.
* Provider rate limits are respected by the shared per-host limiter
  (Comtrade >= 1.5 s between requests, one in flight).
* Provider reference metadata (Comtrade Reporters.json / partnerAreas.json) is
  checked against the codes and pins in the repository.
* Eurostat ``demo_pjan`` FR 2023 is checked through agent G's approved
  JSON-stat template when it is importable (live discovery -> declarative
  manifest -> live sample -> parse/validate; never published), otherwise as a
  bounded direct probe.

Outputs: ``var/live/live_checks.json`` and a generated Markdown rendering
``var/live/live_checks.md`` (the curated narrative lives in
``docs/dev/live_checks.md``). Usage::

    uv run python scripts/live_source_checks.py [--keep-db] [--connector NAME ...] [--skip-eurostat]

Failures of a provider are recorded as blocked/degraded entries, never replaced
by fabricated success.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import subprocess
import sys
import time
import traceback
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml
from sqlalchemy import Engine, create_engine, text

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "var" / "live"
REPORTERS_URL = "https://comtradeapi.un.org/files/v1/app/reference/Reporters.json"
PARTNERS_URL = "https://comtradeapi.un.org/files/v1/app/reference/partnerAreas.json"
EXPECTED_REPORTERS = {"FRA": 251, "DZA": 12, "CHN": 156, "USA": 842}
ISO3 = ("FRA", "DZA", "CHN", "USA")
EUROSTAT_SAMPLE_URL = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/demo_pjan"
                       "?format=JSON&lang=EN&geo=FR&time=2023")
TRACE_HEADERS = ("content-type", "content-length", "etag", "last-modified", "date", "cache-control",
                 "x-rate-limit-limit", "x-rate-limit-interval", "x-concurrency-limit", "retry-after",
                 "x-api-pool")

log = logging.getLogger("reva.live_checks")


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- tracing client
class TracingHttp:
    """Pass-through wrapper around a live SafeHttpClient (origin=live).

    It records request metadata and keeps bodies in memory for the report; it
    never writes fixtures."""

    origin = "live"

    def __init__(self, client: Any, label: str) -> None:
        self.client = client
        self.label = label
        self.trace: list[dict[str, Any]] = []
        self.bodies: dict[str, bytes] = {}

    async def get(self, url: str, **kw: Any) -> Any:
        started = now()
        try:
            r = await self.client.get(url, **kw)
        except Exception as e:
            details = getattr(e, "details", None) or {}
            self.trace.append({"url": url, "requested_at": started, "error": type(e).__name__,
                               "message": str(getattr(e, "message", e))[:400],
                               "status": details.get("status")})
            raise
        self.bodies[url] = r.body
        self.trace.append({
            "url": url,
            "final_url": r.final_url,
            "requested_at": started,
            "retrieved_at": r.retrieved_at.isoformat(),
            "status": r.status,
            "bytes": len(r.body),
            "sha256": sha256(r.body),
            "elapsed_seconds": round(r.elapsed_seconds, 3),
            "attempts": r.attempts,
            "redirects": list(r.redirects),
            "headers": {k: v for k, v in r.headers.items() if k in TRACE_HEADERS},
        })
        return r

    async def aclose(self) -> None:
        await self.client.aclose()


# --------------------------------------------------------------------------- fresh database
@dataclass
class LiveDB:
    settings: Any
    name: str
    owner: Engine
    worker: Engine
    store: Any
    admin: Engine


def _base_settings(**over: Any) -> Any:
    from reva.settings import Settings

    s = Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]
    data = s.model_dump()
    data.update(over)
    return Settings.model_validate(data)


@contextlib.contextmanager
def fresh_database(object_root: Path, keep: bool = False, prefix: str = "reva_live") -> Iterator[LiveDB]:
    """Same pattern as the ``db`` test fixture: new database, bootstrap, seed."""
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.cli.bootstrap import bootstrap
    from reva.cli.seed import seed

    base = _base_settings()
    if base.reva_migration_database_url is None:
        raise RuntimeError("REVA_MIGRATION_DATABASE_URL not configured")
    admin = create_engine(base.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    if name == "reva" or not name.startswith(prefix):
        raise RuntimeError("refusing to touch the dev database")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.migration_url().set(database=name).render_as_string(hide_password=False)
    object_root.mkdir(parents=True, exist_ok=True)
    settings = _base_settings(reva_migration_database_url=url, reva_db_name=name, reva_env="test",
                              reva_object_root=object_root)
    d: LiveDB | None = None
    try:
        for attempt in range(3):  # concurrent sessions may race on cluster-global role DDL
            try:
                bootstrap(settings)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        store = FilesystemArtifactStore(object_root, settings.reva_s3_prefix)
        d = LiveDB(settings, name, make_engine("migration", settings), make_engine("worker", settings), store,
                   admin)
        for attempt in range(12):  # the runtime roles have a cluster-wide connection limit shared by agents
            try:
                seed(d.worker, store)
                break
            except Exception as e:
                if "too many connections" not in str(e) or attempt == 11:
                    raise
                log.warning("connection limit reached; retrying seed in 10 s")
                time.sleep(10)
        yield d
    finally:
        if d is not None:
            d.owner.dispose()
            d.worker.dispose()
        if not keep:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# --------------------------------------------------------------------------- provider reference metadata
async def check_comtrade_reference(contact_email: str | None = None) -> dict[str, Any]:
    from reva.adapters.connectors.comtrade import REPORTERS
    from reva.adapters.http.safe_client import SafeHttpClient

    out: dict[str, Any] = {"checked_at": now(), "requests": []}
    tr = TracingHttp(SafeHttpClient(allowed_hosts=("comtradeapi.un.org",), max_bytes=5_000_000,
                                    contact_email=contact_email), "comtrade.reference")
    try:
        r = await tr.get(REPORTERS_URL)
        rows = json.loads(r.body)["results"]
        found: dict[str, Any] = {}
        for iso, code in EXPECTED_REPORTERS.items():
            hits = [x for x in rows if x.get("reporterCodeIsoAlpha3") == iso and not x.get("isGroup")]
            by_code = [x for x in rows if x.get("reporterCode") == code]
            entry = by_code[0] if by_code else None
            found[iso] = {
                "expected_code": code,
                "repository_code": REPORTERS.get(iso),
                "provider_codes_for_iso3": [x.get("reporterCode") for x in hits],
                "provider_entries_for_iso3": [
                    {k: x.get(k) for k in ("reporterCode", "reporterDesc", "entryEffectiveDate", "entryExpiredDate")}
                    for x in hits],
                "provider_entry": {k: entry.get(k) for k in ("reporterCode", "reporterDesc", "reporterNote",
                                                              "reporterCodeIsoAlpha3", "entryEffectiveDate",
                                                              "entryExpiredDate", "isGroup")} if entry else None,
                "verified": bool(entry) and entry.get("reporterCodeIsoAlpha3") == iso
                and REPORTERS.get(iso) == code and not entry.get("isGroup")
                and not entry.get("entryExpiredDate"),
            }
        out["reporters"] = {"url": REPORTERS_URL, "count": len(rows), "codes": found,
                            "all_verified": all(v["verified"] for v in found.values())}
        await asyncio.sleep(1.6)  # Comtrade spacing
        p = await tr.get(PARTNERS_URL)
        pinned = yaml.safe_load((REPO / "registry" / "reference" / "manifest.yaml").read_text())
        pin = next((e for e in pinned if e["url"] == PARTNERS_URL), {})
        prow = json.loads(p.body)["results"]
        out["partner_areas"] = {"url": PARTNERS_URL, "count": len(prow), "sha256": sha256(p.body),
                                "pinned_sha256": pin.get("sha256"),
                                "matches_pin": sha256(p.body) == pin.get("sha256"),
                                "world_code_0": next((x.get("text") for x in prow if x.get("id") == 0), None)}
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {getattr(e, 'message', e)}"
    finally:
        out["requests"] = tr.trace
        await tr.aclose()
    return out


# --------------------------------------------------------------------------- connectors
async def run_live_connectors(db: LiveDB, names: list[str] | None = None) -> dict[str, Any]:
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.evidence.runner import make_live_client, post_publish_for, run_connector

    conns = builtin_connectors(db.settings.reva_http_contact_email)
    results: dict[str, Any] = {}
    for name in names or list(conns):
        c = conns[name]
        spec = c.describe()
        http = TracingHttp(make_live_client(spec, db.settings), name)
        started = now()
        try:
            rep = await run_connector(c, http, db.store, db.worker, post_publish=post_publish_for(spec.template),
                                      persist_cursor=True)
            report = rep.as_dict()
        except Exception as e:
            report = {"connector": name, "crash": f"{type(e).__name__}: {e}",
                      "traceback": traceback.format_exc()[-2000:]}
        finally:
            await http.aclose()
        statuses = Counter(o["status"] for o in report.get("outcomes", []))
        results[name] = {
            "spec": {"name": spec.name, "version": spec.version, "template": spec.template,
                     "source_key": spec.source_key, "licence": spec.licence, "hosts": list(spec.allowed_hosts)},
            "started_at": started, "finished_at": now(),
            "requests": http.trace,
            "request_count": len(http.trace),
            "bytes_total": sum(t.get("bytes", 0) for t in http.trace),
            "outcome_counts": dict(statuses),
            "report": report,
            "_bodies": http.bodies,
        }
    return results


def publish_live_release(db: LiveDB) -> dict[str, Any]:
    from reva.belief.publication import publish_release
    from reva.kernel.context import Origin

    try:
        r = publish_release(db.worker, db.store, cutoff=None, origin=Origin.live,
                            environment=db.settings.reva_env)
    except Exception as e:
        return {"published": False, "error": f"{type(e).__name__}: {getattr(e, 'message', e)}"}
    return {"published": True, "release_id": str(r.release_id), "context_id": str(r.context_id),
            "sequence": r.sequence, "estimates": r.estimates, "manifest_sha256": r.manifest_sha256,
            "knowledge_cutoff": r.knowledge_cutoff.isoformat(), "origin": "live"}


# --------------------------------------------------------------------------- coverage matrix
def _year(temporal: Any) -> int | None:
    if isinstance(temporal, str):
        temporal = json.loads(temporal)
    start = (temporal or {}).get("start") if isinstance(temporal, dict) else None
    return int(str(start)[:4]) if start else None


def _flag_summary(flags: dict[str, Any] | None) -> dict[str, Any]:
    flags = flags or {}
    keep = ("provider_returned_no_rows", "weight_unusable", "obs_status", "isReported", "isAggregate",
            "classificationCode", "legacyEstimationFlag", "provider_derived_aggregate", "flowCode",
            "cifvalue", "fobvalue")
    return {k: flags[k] for k in keep if k in flags and flags[k] not in ("", None)}


def coverage_matrix(db: LiveDB, release_id: str | None) -> dict[str, Any]:
    from reva.belief.snapshots import estimates
    from reva.cli.seed import entity_id

    cat = yaml.safe_load((REPO / "registry" / "requirements" / "mvp.yaml").read_text())
    reqs = []
    totals: Counter[str] = Counter()
    with db.owner.connect() as conn:
        for req in cat["requirements"]:
            rows = estimates(conn, uuid.UUID(release_id), concept_id=req["concept"]) if release_id else []
            idx: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
            by_entity = {entity_id(iso): iso for iso in req["geography"]}
            for r in rows:
                iso = by_entity.get(r["subject_id"])
                y = _year(r["temporal_extent"])
                if iso and y is not None:
                    idx[(iso, y)].append(r)
            cells = []
            for iso in req["geography"]:
                for y in range(req["period"][0], req["period"][1] + 1):
                    hits = idx.get((iso, y), [])
                    if not hits:
                        n_obs = conn.execute(text(
                            """SELECT count(*) FROM evidence.observation o
                               JOIN ontology.concept_version cv ON cv.id = o.concept_version_id
                               JOIN world.support s ON s.id = o.support_id
                               JOIN evidence.acquisition a ON a.id = o.acquisition_id
                               WHERE cv.concept_id = :c AND o.subject_id = :s AND a.origin = 'live'
                                 AND substr(s.temporal_extent->>'start', 1, 4) = :y"""),
                            {"c": req["concept"], "s": entity_id(iso), "y": str(y)}).scalar_one()
                        cells.append({"geo": iso, "year": y, "status": "absent",
                                      "live_observations_in_evidence": int(n_obs)})
                        totals["absent"] += 1
                        continue
                    h = hits[0]
                    v = h["value"] if isinstance(h["value"], dict) else json.loads(h["value"])
                    flags = h["source_flags"] if isinstance(h["source_flags"], dict) else json.loads(
                        h["source_flags"] or "{}")
                    cell: dict[str, Any] = {"geo": iso, "year": y, "source": h["source_key"],
                                            "observation_id": str(h["observation_id"]),
                                            "retrieved_at": str(h["retrieved_at"]),
                                            "estimates_for_cell": len(hits), "flags": _flag_summary(flags)}
                    if v.get("kind") == "missing":
                        cell.update(status="missing", reason=v.get("reason"))
                    else:
                        cell.update(status="value", value=v.get("value"), unit=v.get("unit_ref"),
                                    valuation=(v.get("basis") or {}).get("valuation"))
                    totals[cell["status"]] += 1
                    cells.append(cell)
            counts = Counter(c["status"] for c in cells)
            reqs.append({"id": req["id"], "concept": req["concept"], "task": req["task"],
                         "geography": req["geography"], "period": req["period"],
                         "counts": {k: counts.get(k, 0) for k in ("value", "missing", "absent")},
                         "cells": cells})
    return {"catalogue_version": cat["catalogue_version"], "release_id": release_id,
            "totals": {k: totals.get(k, 0) for k in ("value", "missing", "absent")}, "requirements": reqs}


# --------------------------------------------------------------------------- per-source details
def comtrade_details(run: dict[str, Any]) -> dict[str, Any]:
    from reva.adapters.connectors.comtrade import REPORTERS

    code_iso = {v: k for k, v in REPORTERS.items()}
    totals: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    bilateral: dict[str, dict[str, Any]] = defaultdict(dict)
    meta = {t["url"]: t for t in run["requests"]}
    for url, body in run["_bodies"].items():
        q = parse_qs(urlsplit(url).query)
        iso = code_iso.get(int(q["reporterCode"][0]), q["reporterCode"][0])
        flow = q["flowCode"][0]
        period = q["period"][0]
        t = meta.get(url, {})
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"data": None, "error": "non-JSON body"}
        rows = payload.get("data") or []
        base = {"url": url, "status": t.get("status"), "bytes": t.get("bytes"), "sha256": t.get("sha256"),
                "retrieved_at": t.get("retrieved_at"), "count": payload.get("count"), "rows": len(rows),
                "error": payload.get("error") or None}
        if "partnerCode" in q:
            row = next((r for r in rows if str(r.get("period")) == period and r.get("partnerCode") == 0), None)
            if row:
                base.update({k: row.get(k) for k in ("primaryValue", "cifvalue", "fobvalue", "netWgt", "qty",
                                                     "qtyUnitCode", "classificationCode", "isReported",
                                                     "isAggregate", "isOriginalClassification",
                                                     "legacyEstimationFlag", "isNetWgtEstimated")})
            base["present"] = row is not None and row.get("primaryValue") is not None
            totals[iso][flow][period] = base
        else:
            partners = [r for r in rows if r.get("partnerCode") != 0]
            base.update({
                "partner_rows_excluding_world": len(partners),
                "world_row_in_response": any(r.get("partnerCode") == 0 for r in rows),
                "distinct_partners": len({r.get("partnerCode") for r in partners}),
                "null_primary_values": sum(1 for r in partners if r.get("primaryValue") is None),
                "classification_codes": sorted({str(r.get("classificationCode")) for r in rows}),
                "top_partners_by_primaryValue": [
                    {"partnerCode": r.get("partnerCode"), "partnerDesc": r.get("partnerDesc"),
                     "primaryValue": r.get("primaryValue")}
                    for r in sorted(partners, key=lambda r: -(r.get("primaryValue") or 0))[:5]],
            })
            bilateral[iso][f"{flow}:{period}"] = base
    dza = {f"{flow}:{y}": {"present": v.get("present"), "rows": v.get("rows"), "primaryValue": v.get("primaryValue"),
                          "netWgt": v.get("netWgt"), "isReported": v.get("isReported"),
                          "isAggregate": v.get("isAggregate")}
           for flow, years in totals.get("DZA", {}).items() for y, v in sorted(years.items())}
    return {"totals": {k: {f: dict(sorted(v.items())) for f, v in d.items()} for k, d in totals.items()},
            "bilateral": dict(bilateral), "dza_negative_demonstration": dza}


def worldbank_details(run: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"series_pages": [], "indicators": {}, "countries": []}
    meta = {t["url"]: t for t in run["requests"]}
    for url, body in run["_bodies"].items():
        data = json.loads(body)
        header, rows = (data[0], data[1] or []) if isinstance(data, list) and len(data) == 2 else ({}, [])
        t = meta.get(url, {})
        path = urlsplit(url).path
        if "/indicator/" in path and "/country/" in path:
            nulls = Counter(r.get("countryiso3code") for r in rows if r.get("value") is None)
            out["series_pages"].append({
                "url": url, "status": t.get("status"), "bytes": t.get("bytes"), "sha256": t.get("sha256"),
                "retrieved_at": t.get("retrieved_at"),
                "header": {k: header.get(k) for k in ("page", "pages", "per_page", "total", "sourceid",
                                                       "lastupdated")},
                "rows": len(rows), "null_values_by_country": dict(nulls),
                "latest_year_with_value": {iso: max((int(r["date"]) for r in rows if r.get("countryiso3code") == iso
                                                     and r.get("value") is not None), default=None)
                                           for iso in ISO3},
            })
        elif "/indicator/" in path:
            r = rows[0] if rows else {}
            out["indicators"][r.get("id")] = {"url": url, "status": t.get("status"), "sha256": t.get("sha256"),
                                              "name": r.get("name"),
                                              "source": (r.get("source") or {}).get("value"),
                                              "sourceOrganization": r.get("sourceOrganization"),
                                              "sourceNote_head": (r.get("sourceNote") or "")[:300]}
        else:
            out["countries"] = [{"id": r.get("id"), "name": r.get("name"),
                                 "region": (r.get("region") or {}).get("value"),
                                 "incomeLevel": (r.get("incomeLevel") or {}).get("value")} for r in rows]
            out["countries_request"] = {"url": url, "status": t.get("status"), "sha256": t.get("sha256"),
                                        "header": header}
    pag: dict[str, Any] = {}
    for ind in ("SP.POP.TOTL", "NY.GDP.MKTP.CD"):
        pages = [p for p in out["series_pages"] if f"/indicator/{ind}?" in p["url"]]
        declared_pages = max((int(p["header"].get("pages") or 0) for p in pages), default=0)
        declared_total = max((int(p["header"].get("total") or 0) for p in pages), default=-1)
        rows = sum(p["rows"] for p in pages)
        pag[ind] = {"pages_fetched": len(pages), "pages_declared": declared_pages, "rows": rows,
                    "total_declared": declared_total,
                    "fully_consumed": bool(pages) and len(pages) == declared_pages and rows == declared_total}
    out["pagination"] = pag
    out["pagination_fully_consumed"] = all(v["fully_consumed"] for v in pag.values())
    return out


def naturalearth_details(run: dict[str, Any], db: LiveDB) -> dict[str, Any]:
    from reva.adapters.connectors.naturalearth import NaturalEarthConnector

    out: dict[str, Any] = {}
    for url, body in run["_bodies"].items():
        t = next(x for x in run["requests"] if x["url"] == url)
        gj = NaturalEarthConnector.to_geojson(body)
        out.update({"url": url, "status": t.get("status"), "bytes": len(body), "sha256": sha256(body),
                    "retrieved_at": t.get("retrieved_at"), "etag": t["headers"].get("etag"),
                    "last_modified": t["headers"].get("last-modified"), "ne_version": gj.get("ne_version"),
                    "feature_count": len(gj["features"]),
                    "types": dict(Counter(str(f["properties"].get("TYPE")) for f in gj["features"]))})
        with db.owner.connect() as conn:
            rows = conn.execute(text(
                """SELECT match_status, count(*) FROM world.display_geometry WHERE dataset_sha256 = :d
                   GROUP BY match_status ORDER BY 1"""), {"d": sha256(body)}).all()
            out["match_status_counts"] = {r[0]: int(r[1]) for r in rows}
            ours = conn.execute(text(
                """SELECT g.feature_key, g.match_status, e.label FROM world.display_geometry g
                   LEFT JOIN world.entity e ON e.id = g.entity_id
                   WHERE g.dataset_sha256 = :d AND g.match_status <> 'unmatched' ORDER BY 1"""),
                {"d": sha256(body)}).all()
            out["non_unmatched_features"] = [{"feature_key": r[0], "match_status": r[1], "entity": r[2]}
                                             for r in ours]
    return out


def crossref_details(run: dict[str, Any], db: LiveDB) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for url, body in run["_bodies"].items():
        t = next(x for x in run["requests"] if x["url"] == url)
        m = json.loads(body).get("message", {})
        out.update({"url": url, "status": t.get("status"), "bytes": len(body), "sha256": sha256(body),
                    "retrieved_at": t.get("retrieved_at"),
                    "rate_limit_headers": {k: v for k, v in t["headers"].items() if k.startswith("x-")},
                    "doi": m.get("DOI"), "title": (m.get("title") or [None])[0],
                    "container_title": (m.get("container-title") or [None])[0],
                    "published": (m.get("published") or {}).get("date-parts"),
                    "updated_by": m.get("updated-by"), "update_to": m.get("update-to"),
                    "relation_keys": sorted((m.get("relation") or {}).keys()),
                    "indexed": (m.get("indexed") or {}).get("date-time"),
                    "is_referenced_by_count": m.get("is-referenced-by-count")})
    with db.owner.connect() as conn:
        out["studies_in_db"] = [dict(r) for r in conn.execute(text(
            "SELECT doi, source_artifact_sha256 FROM evidence.study ORDER BY doi")).mappings()]
    return out


# --------------------------------------------------------------------------- fixture drift
VOLATILE_JSON_KEYS = ("elapsedTime",)  # Comtrade reports server time per response


def _semantic(body: bytes) -> Any:
    try:
        doc = json.loads(body)
    except ValueError:
        return sha256(body)
    if isinstance(doc, dict):
        doc = {k: v for k, v in doc.items() if k not in VOLATILE_JSON_KEYS}
    return doc


def fixture_drift(bodies_by_connector: dict[str, dict[str, bytes]]) -> dict[str, Any]:
    """Compare live responses with the captured CI fixtures (read-only).

    Byte identity and semantic identity (JSON minus volatile keys) are reported
    separately: a byte change with equal semantics is not a provider revision."""
    root = REPO / "tests" / "fixtures" / "source"
    out: dict[str, Any] = {}
    for name, bodies in bodies_by_connector.items():
        mpath = root / name / "manifest.json"
        if not mpath.exists():
            out[name] = {"fixtures": False}
            continue
        entries = {e["url"]: e for e in json.loads(mpath.read_text())}
        res: dict[str, Any] = {"compared": 0, "byte_identical": 0, "semantically_identical": 0,
                               "changed": [], "not_in_fixtures": []}
        for url, body in bodies.items():
            e = entries.get(url)
            if e is None:
                res["not_in_fixtures"].append(url)
                continue
            fbody = (root / name / e["file"]).read_bytes()
            res["compared"] += 1
            res["byte_identical"] += int(fbody == body)
            if _semantic(fbody) == _semantic(body):
                res["semantically_identical"] += 1
            else:
                res["changed"].append({"url": url, "fixture_sha256": e["sha256"], "live_sha256": sha256(body),
                                       "fixture_retrieved_at": e.get("retrieved_at")})
        out[name] = res
    return out


# --------------------------------------------------------------------------- Eurostat
async def eurostat_check(settings: Any, work: Path) -> dict[str, Any]:
    """Bounded live check of demo_pjan FR 2023 through the approved template."""
    from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
    from reva.adapters.http.safe_client import SafeHttpClient

    def client() -> Any:
        return SafeHttpClient(allowed_hosts=("ec.europa.eu",), timeout_seconds=settings.reva_http_timeout_seconds,
                              max_bytes=4_000_000, contact_email=settings.reva_http_contact_email,
                              rate_limiter=RateLimiter({"ec.europa.eu": HostPolicy(min_interval_seconds=1.0,
                                                                                    max_concurrency=1)}))

    try:
        from reva.adapters.connectors.eurostat_template import EurostatJsonStatConnector  # noqa: F401
        from reva.autonomy.discovery import EurostatCatalogueDiscoverer  # noqa: F401
    except Exception as e:
        return await _eurostat_probe(client(), f"template unavailable: {type(e).__name__}: {e}")
    try:
        return await _eurostat_template(client(), work)
    except Exception as e:
        res = await _eurostat_probe(client(), f"template path failed: {type(e).__name__}: "
                                              f"{getattr(e, 'message', e)}")
        res["template_traceback"] = traceback.format_exc()[-1500:]
        return res


async def _eurostat_template(c: Any, work: Path) -> dict[str, Any]:
    from reva.adapters.connectors.eurostat_template import EurostatJsonStatConnector, template_digest
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.autonomy.common import ArtifactIO, default_budget, make_context
    from reva.autonomy.discovery import EUROSTAT_TOC_URL, EurostatCatalogueDiscoverer
    from reva.autonomy.generator import DeclarativeConnectorGenerator
    from reva.autonomy.mapping import RegistryOntologyMapper
    from reva.ports.autonomy import KnowledgeGap
    from reva.ports.connectors import FetchBudget

    io_ = ArtifactIO(FilesystemArtifactStore(work / "eurostat_objects"))
    http = TracingHttp(c, "eurostat")
    try:
        ctx = make_context(io_, "evidence", datetime.now(UTC), {"schema": "live-check"})
        gap = KnowledgeGap(gap_id=uuid.uuid4(), requirement_id="R-POP-JAN-AGESEX",
                           concept_version="demography.population.jan1_age_sex@1.0.0", geography=("FRA",),
                           temporal_support={"years": [2023]}, reason="live check", priority=1.0,
                           priority_method="live-check", context=ctx)
        disc = EurostatCatalogueDiscoverer(io_, http)
        cands = await disc.discover(gap, default_budget())
        search = disc.last_search or {}
        if not cands:
            return {"mode": "template", "status": "no_candidate", "search": _small(search),
                    "requests": http.trace}
        mappings = RegistryOntologyMapper(io_).assess(cands[0], gap.context)
        cp = DeclarativeConnectorGenerator(io_).generate(cands[0], mappings[0], ("eurostat_jsonstat",))
        manifest = io_.get_json(cp.manifest)
        conn = EurostatJsonStatConnector(manifest)
        plan = conn.plan(None, FetchBudget(max_requests=1))
        item = plan.items[0]
        raw = await conn.fetch(item, http)
        batch = next(iter(conn.parse(raw)))
        q = conn.validate(batch)
        md = batch.metadata
        flags = Counter(o.source_flags.get("status") or o.source_flags.get("obs_status") or ""
                        for o in batch.observations)
        total = next((o for o in batch.observations if o.logical_key.endswith(":T:TOTAL:2023")), None)
        return {
            "mode": "template",
            "status": "checked",
            "published": False,
            "note": "validated through the approved template; not published (promotion requires the autonomy gate)",
            "template_digest": template_digest(),
            "catalogue": {"url": EUROSTAT_TOC_URL,
                          "datasets_in_catalogue": search.get("datasets_in_catalogue"),
                          "ranked_top": [r.get("code") for r in (search.get("ranked") or [])][:5],
                          "search": _small(search)},
            "candidate": cands[0].dataset_id,
            "mapping": [{"target": m.target_concept_version, "relation": m.relation,
                         "review_required": m.review_required} for m in mappings],
            "manifest_sha256": cp.manifest.sha256,
            "data_url": item.url,
            "matches_captured_sample_url": item.url == EUROSTAT_SAMPLE_URL,
            "dimension_order": md.get("dimension_order"),
            "pinned_dimension_order": md.get("pinned_dimension_order"),
            "size": md.get("size"), "cube_positions": md.get("cube_positions"),
            "value_count": md.get("value_count"), "status_count": md.get("status_count"),
            "status_codes_seen": md.get("status_codes_seen"), "status_labels": md.get("status_labels"),
            "dataset_updated": md.get("dataset_updated"), "annotations": md.get("annotations"),
            "observations": len(batch.observations),
            "missing_cells": sum(1 for o in batch.observations if o.value.get("kind") == "missing"),
            "per_cell_status_counts": dict(flags),
            "fr_total_2023": total.value if total else None,
            "fr_total_2023_flags": {k: total.source_flags.get(k) for k in ("status", "cell_type")} if total else None,
            "quality": q.model_dump(mode="json"),
            "requests": http.trace,
            "_bodies": dict(http.bodies),
        }
    finally:
        await http.aclose()


async def _eurostat_probe(c: Any, why: str) -> dict[str, Any]:
    http = TracingHttp(c, "eurostat")
    try:
        r = await http.get(EUROSTAT_SAMPLE_URL)
        doc = json.loads(r.body)
        status = doc.get("status") or {}
        return {"mode": "probe only", "why": why, "url": EUROSTAT_SAMPLE_URL, "dimension_order": doc.get("id"),
                "size": doc.get("size"), "value_count": len(doc.get("value") or {}),
                "status_codes_seen": sorted(set(status.values())) if isinstance(status, dict) else None,
                "status_labels": ((doc.get("extension") or {}).get("status") or {}).get("label"),
                "dataset_updated": doc.get("updated"), "requests": http.trace}
    except Exception as e:
        return {"mode": "probe only", "why": why, "status": "blocked",
                "error": f"{type(e).__name__}: {getattr(e, 'message', e)}", "requests": http.trace}
    finally:
        await http.aclose()


def _small(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k in ("datasets_in_catalogue", "query_tokens", "usage", "inspections")}


# --------------------------------------------------------------------------- orchestration
def git_head() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,  # noqa: S607
                              check=True).stdout.strip()
    except Exception:
        return None


async def run_all(db: LiveDB, names: list[str] | None, eurostat: bool, work: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "reva-live-checks/1",
        "date": datetime.now(UTC).date().isoformat(),
        "started_at": now(),
        "git_head": git_head(),
        "database": {"name": db.name, "fresh": True, "dev_database_untouched": db.name != "reva"},
        "origin": "live",
        "fixtures_recorded": False,
    }
    result["comtrade_reference"] = await check_comtrade_reference(db.settings.reva_http_contact_email)
    await asyncio.sleep(2.0)  # spacing before the Comtrade connector run
    runs = await run_live_connectors(db, names)
    result["release"] = publish_live_release(db)
    result["coverage"] = coverage_matrix(db, result["release"].get("release_id"))
    details: dict[str, Any] = {}
    for name, run in runs.items():
        try:
            if name.startswith("un.comtrade"):
                details[name] = comtrade_details(run)
            elif name.startswith("worldbank"):
                details[name] = worldbank_details(run)
            elif name.startswith("naturalearth"):
                details[name] = naturalearth_details(run, db)
            elif name.startswith("crossref"):
                details[name] = crossref_details(run, db)
        except Exception as e:
            details[name] = {"error": f"{type(e).__name__}: {e}"}
    result["source_details"] = details
    result["fixture_drift"] = fixture_drift({n: r["_bodies"] for n, r in runs.items()})
    for run in runs.values():
        run.pop("_bodies", None)
        rep = run["report"]
        if "quality" in rep:  # keep findings, drop bulky repetition
            rep["quality_findings"] = {k: [f for q in v for f in q.get("findings", [])]
                                       for k, v in rep.pop("quality").items()}
    result["connectors"] = runs
    with db.owner.connect() as conn:
        result["database"]["counts"] = {
            "acquisitions_live": conn.execute(text(
                "SELECT count(*) FROM evidence.acquisition WHERE origin='live'")).scalar_one(),
            "acquisitions_non_live": conn.execute(text(
                "SELECT count(*) FROM evidence.acquisition WHERE origin<>'live'")).scalar_one(),
            "observations": conn.execute(text("SELECT count(*) FROM evidence.observation")).scalar_one(),
            "relations": conn.execute(text("SELECT count(*) FROM world.relation")).scalar_one()
            if conn.execute(text("SELECT to_regclass('world.relation')")).scalar_one() else None,
            "connector_health": [dict(r) for r in conn.execute(text(
                """SELECT cv.name, h.status, h.metrics->>'request_success_ratio' AS success_ratio
                   FROM ops.connector_health h JOIN evidence.connector_version cv ON cv.id = h.connector_version_id
                   ORDER BY cv.name""")).mappings()],
        }
    result["eurostat"] = await eurostat_check(db.settings, work) if eurostat else {"mode": "skipped"}
    if "_bodies" in result["eurostat"]:
        result["fixture_drift"]["eurostat.demo_pjan.fr"] = fixture_drift(
            {"eurostat.demo_pjan.fr": result["eurostat"].pop("_bodies")})["eurostat.demo_pjan.fr"]
    result["finished_at"] = now()
    result["gates"] = gates(result)
    return result


def gates(r: dict[str, Any]) -> dict[str, Any]:
    g: dict[str, Any] = {}
    ref = r.get("comtrade_reference", {})
    g["comtrade_reporter_codes_verified"] = bool(ref.get("reporters", {}).get("all_verified"))
    for name, run in r.get("connectors", {}).items():
        rep = run["report"]
        oc = run["outcome_counts"]
        if "crash" in rep:
            g[name] = "blocked"
        elif oc.get("failed") or oc.get("quarantined"):
            g[name] = "degraded"
        else:
            g[name] = "pass"
    g["live_release_published"] = bool(r.get("release", {}).get("published"))
    g["no_fixture_origin_in_db"] = r["database"]["counts"]["acquisitions_non_live"] == 0
    eu = r.get("eurostat", {})
    g["eurostat_demo_pjan"] = ("pass" if eu.get("mode") == "template" and eu.get("quality", {}).get("outcome") == "pass"
                               else eu.get("mode", "unknown") if eu.get("mode") != "template"
                               else f"template:{eu.get('quality', {}).get('outcome', eu.get('status'))}")
    return g


# --------------------------------------------------------------------------- markdown rendering
def render_markdown(r: dict[str, Any]) -> str:
    L: list[str] = []
    L.append(f"# Live source checks (generated {r['finished_at']})\n")
    L.append(f"Database `{r['database']['name']}` (fresh), origin `live`, git `{r.get('git_head')}`.\n")
    L.append("## Gates\n")
    for k, v in r["gates"].items():
        L.append(f"- `{k}`: **{v}**")
    rel = r["release"]
    L.append(f"\nRelease: `{rel.get('release_id')}` seq {rel.get('sequence')}, {rel.get('estimates')} estimates, "
             f"manifest `{rel.get('manifest_sha256')}`, cutoff {rel.get('knowledge_cutoff')}.\n")
    L.append("## Coverage matrix\n")
    for req in r["coverage"]["requirements"]:
        years = list(range(req["period"][0], req["period"][1] + 1))
        L.append(f"### {req['id']} `{req['concept']}` — {req['counts']}\n")
        if len(years) > 8:
            L.append("| geo | value | missing | absent | first/last value year |\n|---|---|---|---|---|")
            for iso in req["geography"]:
                cs = [c for c in req["cells"] if c["geo"] == iso]
                vy = [c["year"] for c in cs if c["status"] == "value"]
                L.append(f"| {iso} | {sum(c['status'] == 'value' for c in cs)} | "
                         f"{sum(c['status'] == 'missing' for c in cs)} | {sum(c['status'] == 'absent' for c in cs)} | "
                         f"{min(vy) if vy else '-'}/{max(vy) if vy else '-'} |")
        else:
            L.append("| geo | " + " | ".join(map(str, years)) + " |")
            L.append("|---" * (len(years) + 1) + "|")
            for iso in req["geography"]:
                row = []
                for y in years:
                    c = next(c for c in req["cells"] if c["geo"] == iso and c["year"] == y)
                    if c["status"] == "value":
                        row.append(f"{c['value']}")
                    elif c["status"] == "missing":
                        fl = ",".join(k for k, v in c["flags"].items() if v is True)
                        row.append(f"Missing({c['reason']}{'; ' + fl if fl else ''})")
                    else:
                        row.append("absent")
                L.append(f"| {iso} | " + " | ".join(row) + " |")
        L.append("")
    ct = r.get("source_details", {}).get("un.comtrade.public_exact", {})
    if ct.get("totals"):
        L.append("## Comtrade HS2709 world totals (partner 0)\n")
        L.append("| reporter | flow | year | rows | primaryValue | cif | fob | netWgt | cls | isReported | isAggregate "
                 "| legacyEst |\n|---|---|---|---|---|---|---|---|---|---|---|---|")
        for iso, flows in ct["totals"].items():
            for flow, years in flows.items():
                for y, v in years.items():
                    L.append(f"| {iso} | {flow} | {y} | {v['rows']} | {v.get('primaryValue')} | {v.get('cifvalue')} | "
                             f"{v.get('fobvalue')} | {v.get('netWgt')} | {v.get('classificationCode')} | "
                             f"{v.get('isReported')} | {v.get('isAggregate')} | {v.get('legacyEstimationFlag')} |")
        L.append("\n## Comtrade bilateral 2023 (partner rows excl. World)\n")
        L.append("| reporter | flow:year | rows | partner rows | world row in response | null values | cls |\n"
                 "|---|---|---|---|---|---|---|")
        for iso, d in ct["bilateral"].items():
            for k, v in d.items():
                L.append(f"| {iso} | {k} | {v['rows']} | {v['partner_rows_excluding_world']} | "
                         f"{v['world_row_in_response']} | {v['null_primary_values']} | "
                         f"{','.join(v['classification_codes'])} |")
        L.append("")
    if r.get("fixture_drift"):
        L.append("## Drift against captured CI fixtures\n")
        L.append("| connector | compared | byte identical | semantically identical | changed |\n|---|---|---|---|---|")
        for name, d in r["fixture_drift"].items():
            L.append(f"| {name} | {d.get('compared')} | {d.get('byte_identical')} | "
                     f"{d.get('semantically_identical')} | {len(d.get('changed') or [])} |")
        L.append("")
    L.append("## Requests\n")
    L.append("| connector | requests | bytes | outcomes |\n|---|---|---|---|")
    for name, run in r["connectors"].items():
        L.append(f"| {name} | {run['request_count']} | {run['bytes_total']} | {run['outcome_counts']} |")
    return "\n".join(L) + "\n"


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, bytes):
        return f"<{len(o)} bytes>"
    return str(o)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--connector", action="append", help="restrict to connector names")
    ap.add_argument("--keep-db", action="store_true", help="keep the fresh database after the run")
    ap.add_argument("--skip-eurostat", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT_DIR / "live_checks.json")
    ap.add_argument("--md", type=Path, default=OUT_DIR / "live_checks.md")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work = OUT_DIR / "runs" / stamp
    work.mkdir(parents=True, exist_ok=True)
    with fresh_database(work / "objects", keep=args.keep_db) as db:
        log.info("fresh database %s", db.name)
        result = asyncio.run(run_all(db, args.connector, not args.skip_eurostat, work))
        result["database"]["kept"] = args.keep_db
    result["object_store"] = str((work / "objects").relative_to(REPO))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, default=_default, sort_keys=False))
    (work / "live_checks.json").write_text(args.out.read_text())
    args.md.write_text(render_markdown(result))
    print(json.dumps({"out": str(args.out), "gates": result["gates"], "coverage": result["coverage"]["totals"]},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
