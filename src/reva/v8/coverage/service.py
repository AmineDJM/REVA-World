"""Read side of the coverage page: the cached aggregates (``coverage.json``) shaped per view, plus cheap live checks.

The heavy counts come from ``reva v8 coverage refresh`` (:mod:`reva.v8.coverage.compute`); requests never recompute
them. Two things are read live (milliseconds): the atlas tables registered after the aggregates were computed (so the
page says how stale it is) and, for queued loads, the connector acquisitions that would complete them.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from reva.v8.coverage.compute import DEFAULT_DIR
from reva.v8.coverage.pending import KINDS, PendingRegistry, load_pending, observe

LEVELS = ("region", "subregion", "country")
NOTE = (
    "Couverture = ce que REVA a chargé, pas la réalité : l’absence d’une source n’est pas l’absence du phénomène, "
    "et un grand nombre de lignes ne garantit ni l’exactitude ni l’exhaustivité."
)
KIND_FR = {
    "licence": "Licences",
    "account_key": "Comptes et clés",
    "permission": "Autorisations",
    "disk": "Disque (≥ 5 Go libres)",
    "bigger_machine": "Machine plus grande",
    "attribution_contact": "Contacts d’attribution",
    "policy_choice": "Choix de méthode ou de publication",
}

STALE_SQL = """
SELECT count(DISTINCT dataset_key) AS n, max(recorded_at) AS last
FROM evidence.dataset WHERE concept_id = 'atlas.table' AND origin = :o AND recorded_at > :t
"""
ACQ_SQL = """
SELECT cv.name, array_agg(DISTINCT cv.version) AS versions, max(a.retrieved_at) AS last_retrieved_at
FROM evidence.connector_version cv JOIN evidence.acquisition a ON a.connector_version_id = cv.id AND a.origin = :o
WHERE cv.name = ANY(CAST(:names AS text[])) GROUP BY 1
"""
TABLES_SQL = (
    "SELECT DISTINCT dataset_key FROM evidence.dataset WHERE concept_id = 'atlas.table' AND origin = :o "
    "AND dataset_key = ANY(CAST(:keys AS text[]))"
)


class CoverageNotComputed(Exception):
    """No coverage.json yet (run ``reva v8 coverage refresh``)."""


@dataclass
class Coverage:
    doc: dict[str, Any]
    mtime: float
    places: dict[str, dict[str, Any]] = field(default_factory=dict)
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    domain_tables: dict[str, list[str]] = field(default_factory=dict)
    members: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # level -> column -> iso3s

    @property
    def domain_ids(self) -> list[str]:
        return [d["id"] for d in self.doc["domains"]]


_LOCK = threading.Lock()
_CACHE: dict[str, Coverage] = {}
_OBS: dict[str, tuple[float, Any]] = {}
OBS_TTL_S = 60.0


def coverage_path(directory: Path | None = None) -> Path:
    return (directory or DEFAULT_DIR) / "coverage.json"


def load(directory: Path | None = None) -> Coverage:
    """The cached aggregates, re-read when the file changes."""
    path = coverage_path(directory)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError as e:
        raise CoverageNotComputed(str(path)) from e
    key = str(path)
    with _LOCK:
        c = _CACHE.get(key)
        if c is not None and c.mtime == mtime:
            return c
    doc = json.loads(path.read_text(encoding="utf-8"))
    c = _index(doc, mtime)
    with _LOCK:
        _CACHE[key] = c
    return c


def _index(doc: dict[str, Any], mtime: float) -> Coverage:
    c = Coverage(doc=doc, mtime=mtime)
    c.places = {p["iso3"]: p for p in doc["places"]}
    c.tables = {t["key"]: t for t in doc["tables"]}
    dt: dict[str, list[str]] = defaultdict(list)
    for t in doc["tables"]:
        dt[t["domain"]].append(t["key"])
    c.domain_tables = dict(dt)
    members: dict[str, dict[str, list[str]]] = {"region": defaultdict(list), "subregion": defaultdict(list)}
    for p in doc["places"]:
        members["region"][p["region"]].append(p["iso3"])
        members["subregion"][p["subregion"]].append(p["iso3"])
    c.members = {k: dict(v) for k, v in members.items()}
    return c


def _label(p: dict[str, Any]) -> str:
    return str(p.get("label_fr") or p.get("label") or p["iso3"])


def columns(c: Coverage, level: str) -> list[dict[str, Any]]:
    if level == "country":
        order = {r: i for i, r in enumerate(c.doc["regions"])}
        ps = sorted(c.doc["places"], key=lambda p: (order.get(p["region"], 99), p["subregion"], _label(p)))
        return [
            {
                "id": p["iso3"],
                "label": _label(p),
                "region": p["region"],
                "subregion": p["subregion"],
                "countries": 1,
                **({"placement": "reva", "placement_basis": p.get("placement_basis")} if p["placement"] == "reva" else {}),
            }
            for p in ps
        ]
    labels = c.doc["regions"] if level == "region" else c.doc["subregions"]
    out = []
    for key, fr in labels.items():
        isos = c.members[level].get(key, [])
        if not isos:
            continue
        region = key if level == "region" else c.places[isos[0]]["region"]
        out.append({"id": key, "label": fr, "region": region, "countries": len(isos)})
    if level == "subregion":
        order = {r: i for i, r in enumerate(c.doc["regions"])}
        out.sort(key=lambda x: (order.get(x["region"], 99), x["label"]))
    return out


def _cell_country(c: Coverage, domain: str, iso3: str) -> dict[str, Any] | None:
    v = (c.doc["cells"].get(domain) or {}).get(iso3)
    kg = ((c.doc.get("kg") or {}).get("cells", {}).get(domain) or {}).get(iso3)
    if v is None and not kg:
        return None
    out: dict[str, Any] = {}
    if v is not None:
        out.update(rows=v[0], tables=v[1], y0=v[2], y1=v[3], future_rows=v[4])
    if kg:
        out["kg"] = kg
    return out


def _cell_group(c: Coverage, domain: str, isos: list[str]) -> dict[str, Any] | None:
    cells = c.doc["cells"].get(domain) or {}
    kgc = ((c.doc.get("kg") or {}).get("cells", {}).get(domain)) or {}
    have = [i for i in isos if i in cells]
    kg = sum(kgc.get(i, 0) for i in isos)
    if not have and not kg:
        return None
    rows = sum(cells[i][0] for i in have)
    y1s = [cells[i][3] for i in have if cells[i][3] is not None]
    tabs = {k for k in c.domain_tables.get(domain, []) if any(i in c.doc["table_countries"].get(k, {}) for i in isos)}
    out: dict[str, Any] = {"countries_with_rows": len(have), "rows": rows, "tables": len(tabs)}
    if y1s:
        out["y1"] = max(y1s)
        out["y1_median"] = sorted(y1s)[len(y1s) // 2]
    if kg:
        out["kg"] = kg
        out["countries_with_kg"] = sum(1 for i in isos if kgc.get(i))
    return out


def domain_summary(c: Coverage, domain: str, pending: PendingRegistry | None) -> dict[str, Any]:
    keys = c.domain_tables.get(domain, [])
    ts = [c.tables[k] for k in keys]
    lic: dict[str, int] = defaultdict(int)
    for t in ts:
        lic[t["licence_class"]] += 1
    kg_total = sum(k.get("by_domain", {}).get(domain, 0) for k in (c.doc.get("kg") or {}).get("kinds", []))
    cells = c.doc["cells"].get(domain) or {}
    y1s = [t["latest_past_year"] for t in ts if t["latest_past_year"] is not None]
    rec = [t["recorded_at"] for t in ts if t["recorded_at"]]
    cat = (c.doc.get("catalog") or {}).get(domain)
    items = [i for i in (pending.items if pending else ()) if domain in i.unlocks.domains and i.status != "done"]
    return {
        "tables": len(ts),
        "rows": sum(t["rows"] for t in ts),
        "countries": len(cells),
        "latest_year": max(y1s) if y1s else None,
        "last_loaded_at": max(rec) if rec else None,
        "licences": dict(lic),
        "kg_entities": kg_total,
        "catalog": {k: cat[k] for k in ("concepts", "with_loaded_table", "without_loaded_table")} if cat else None,
        "pending_loads": sum(1 for i in items if i.type == "load"),
        "pending_decisions": sum(1 for i in items if i.type == "decision"),
    }


def meta(c: Coverage, stale: dict[str, Any] | None) -> dict[str, Any]:
    d = c.doc
    return {
        "computed_at": d["computed_at"],
        "today": d["today"],
        "origin": d["origin"],
        "elapsed_s": d.get("elapsed_s"),
        "atlas_tables": len(d["tables"]),
        "atlas_rows": sum(t["rows"] for t in d["tables"]),
        "kg_entities": sum(k["entities"] for k in (d.get("kg") or {}).get("kinds", [])),
        "kg_statements": sum(s["statements"] for s in (d.get("kg") or {}).get("sources", [])),
        "kg_computed": d.get("kg") is not None,
        "places": len(d["places"]),
        "stale": stale,
        "note": NOTE,
        "refresh_command": "uv run reva v8 coverage refresh",
    }


def staleness(conn: Connection, c: Coverage) -> dict[str, Any]:
    r = conn.execute(text(STALE_SQL), {"o": c.doc["origin"], "t": c.doc.get("db_time") or c.doc["computed_at"]}).one()
    return {"tables_registered_since": int(r.n), "last_registered_at": r.last.isoformat() if r.last else None}


def matrix(c: Coverage, level: str, pending: PendingRegistry | None, stale: dict[str, Any] | None) -> dict[str, Any]:
    cols = columns(c, level)
    rows = []
    for d in c.doc["domains"]:
        did = d["id"]
        if level == "country":
            cells = {col["id"]: x for col in cols if (x := _cell_country(c, did, col["id"]))}
        else:
            cells = {col["id"]: x for col in cols if (x := _cell_group(c, did, c.members[level][col["id"]]))}
        s = domain_summary(c, did, pending)
        if did == "other" and not s["tables"] and not s["kg_entities"]:
            continue
        rows.append({"id": did, "label": d["fr"], "summary": s, "cells": cells})
    return {"level": level, "meta": meta(c, stale), "columns": cols, "rows": rows}


def domain_detail(c: Coverage, domain: str, pending: PendingRegistry | None, obs: dict[str, Any]) -> dict[str, Any]:
    d = next(x for x in c.doc["domains"] if x["id"] == domain)
    tables = sorted((c.tables[k] for k in c.domain_tables.get(domain, [])), key=lambda t: -t["rows"])
    countries = []
    without = []
    for iso3, p in sorted(c.places.items(), key=lambda kv: _label(kv[1])):
        cell = _cell_country(c, domain, iso3)
        if cell:
            countries.append({"iso3": iso3, "label": _label(p), "region": p["region"], **cell})
        else:
            without.append({"iso3": iso3, "label": _label(p), "region": p["region"]})
    kinds = []
    for k in (c.doc.get("kg") or {}).get("kinds", []):
        n = k.get("by_domain", {}).get(domain, 0)
        if n:
            kinds.append({"kind": k["kind"], "entities": n, "sources": k["sources"], "last_recorded_at": k.get("last")})
    connectors = [
        {k: x[k] for k in ("name", "atlas_source", "provider", "licence", "tables_expected", "tables_loaded",
                           "acquired", "last_retrieved_at", "cadence_seconds", "version")}
        for x in c.doc["connectors"]
        if x.get("domain") == domain
    ]
    items = [_item(i, obs) for i in (pending.items if pending else ()) if domain in i.unlocks.domains]
    return {
        "id": domain,
        "label": d["fr"],
        "label_en": d["en"],
        "themes": d.get("themes", []),
        "summary": domain_summary(c, domain, pending),
        "tables": [_table_out(t) for t in tables],
        "countries": countries,
        "countries_without_data": without,
        "kg_kinds": kinds,
        "kg_located_in_note": (c.doc.get("kg") or {}).get("located_in_note"),
        "hist": c.doc["hist"].get(domain, {}),
        "hist_before": c.doc["hist_before"].get(domain, 0),
        "hist_from": c.doc.get("hist_from"),
        "catalog": (c.doc.get("catalog") or {}).get(domain),
        "connectors": connectors,
        "pending": items,
        "meta": meta(c, None),
    }


def _table_out(t: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "key", "source", "dataset", "domain", "domain_basis", "rows", "indicators", "countries", "rows_without_country",
        "period_min", "period_max", "latest_past_year", "future_rows", "recorded_at", "first_recorded_at", "versions",
        "last_fetch_at", "cadence_seconds", "licence", "licence_class", "coverage_note", "missing_cells", "read_error",
    )
    out = {k: t.get(k) for k in keep}
    out["overdue"] = _overdue(t)
    return out


def _overdue(t: dict[str, Any]) -> bool | None:
    """True when the last fetch is older than the connector's declared refresh cadence (None when unknown)."""
    if not t.get("last_fetch_at") or not t.get("cadence_seconds"):
        return None
    last = datetime.fromisoformat(t["last_fetch_at"])
    return (datetime.now(UTC) - last).total_seconds() > float(t["cadence_seconds"])


def country_detail(c: Coverage, iso3: str, pending: PendingRegistry | None) -> dict[str, Any]:
    p = c.places[iso3]
    doms = []
    for d in c.doc["domains"]:
        cell = _cell_country(c, d["id"], iso3)
        if d["id"] == "other" and not cell:
            continue
        doms.append({"id": d["id"], "label": d["fr"], "cell": cell})
    tables = []
    for key, per in c.doc["table_countries"].items():
        v = per.get(iso3)
        if v is None:
            continue
        t = c.tables[key]
        tables.append(
            {
                "key": key,
                "domain": t["domain"],
                "rows": v[0],
                "y0": v[1],
                "y1": v[2],
                "future_rows": v[3],
                "licence_class": t["licence_class"],
                "licence": (t.get("licence") or {}).get("licence"),
                "required_notice": (t.get("licence") or {}).get("required_notice"),
                "recorded_at": t["recorded_at"],
            }
        )
    tables.sort(key=lambda x: (x["domain"], -x["rows"]))
    kinds = sorted(
        ({"kind": k, "entities": v[iso3]} for k, v in ((c.doc.get("kg") or {}).get("kind_countries") or {}).items()
         if v.get(iso3)),
        key=lambda x: -x["entities"],
    )
    return {
        "iso3": iso3,
        "label": _label(p),
        "label_en": p.get("label"),
        "region": p["region"],
        "region_label": c.doc["regions"].get(p["region"], p["region"]),
        "subregion": p["subregion"],
        "subregion_label": c.doc["subregions"].get(p["subregion"], p["subregion"]),
        "placement": p["placement"],
        "placement_basis": p.get("placement_basis"),
        "domains": doms,
        "domains_without_data": [d["label"] for d in doms if not d["cell"]],
        "tables": tables,
        "kg_kinds": kinds,
        "kg_located_in_note": (c.doc.get("kg") or {}).get("located_in_note"),
        "meta": meta(c, None),
    }


def freshness(c: Coverage) -> dict[str, Any]:
    labels = {d["id"]: d["fr"] for d in c.doc["domains"]}
    out = []
    for did, keys in c.domain_tables.items():
        ts = [c.tables[k] for k in keys]
        out.append(
            {
                "id": did,
                "label": labels.get(did, did),
                "hist": c.doc["hist"].get(did, {}),
                "hist_before": c.doc["hist_before"].get(did, 0),
                "tables": [
                    {
                        "key": t["key"],
                        "rows": t["rows"],
                        "latest_past_year": t["latest_past_year"],
                        "future_rows": t["future_rows"],
                        "recorded_at": t["recorded_at"],
                        "last_fetch_at": t["last_fetch_at"],
                        "cadence_seconds": t["cadence_seconds"],
                        "overdue": _overdue(t),
                    }
                    for t in sorted(ts, key=lambda t: (t["latest_past_year"] or -99999), reverse=True)
                ],
            }
        )
    order = {d: i for i, d in enumerate(c.domain_ids)}
    out.sort(key=lambda x: order.get(x["id"], 99))
    never = [
        {k: x[k] for k in ("name", "atlas_source", "provider", "licence", "domain", "tables_expected")}
        for x in c.doc["connectors"]
        if not x["acquired"] and not x["tables_loaded"]
    ]
    return {"meta": meta(c, None), "hist_from": c.doc.get("hist_from"), "domains": out, "never_loaded": never}


# ----------------------------------------------------------------------------------------------- pending / decisions
def observations(conn: Connection, reg: PendingRegistry, origin: str = "live") -> dict[str, Any]:
    """Live observed state of every queued load (cached OBS_TTL_S seconds per process)."""
    key = f"{origin}|{id(reg)}"
    now = time.monotonic()
    with _LOCK:
        hit = _OBS.get(key)
        if hit and now - hit[0] < OBS_TTL_S:
            return hit[1]  # type: ignore[no-any-return]
    names = sorted({c.connector for i in reg.items for c in i.check})
    keys = sorted({t for i in reg.items for c in i.check for t in c.tables})
    acq = {
        r["name"]: {
            "versions": list(r["versions"]),
            "last_retrieved_at": r["last_retrieved_at"].isoformat() if r["last_retrieved_at"] else None,
        }
        for r in conn.execute(text(ACQ_SQL), {"o": origin, "names": names}).mappings()
    }
    tabs = {r[0] for r in conn.execute(text(TABLES_SQL), {"o": origin, "keys": keys})}
    out = {i.id: observe(i, acq, tabs) for i in reg.items}
    out["_checked_at"] = datetime.now(UTC).isoformat()
    with _LOCK:
        _OBS[key] = (now, out)
    return out


def _item(i: Any, obs: dict[str, Any]) -> dict[str, Any]:
    d = i.model_dump(mode="json")
    d["kind_label"] = KIND_FR[i.kind]
    d["observed"] = obs.get(i.id)
    return d


def pending_view(reg: PendingRegistry, obs: dict[str, Any], type_: str) -> dict[str, Any]:
    items = [_item(i, obs) for i in reg.items if i.type == type_]
    groups = []
    for k in KINDS:
        g = [x for x in items if x["kind"] == k]
        if g:
            groups.append({"kind": k, "label": KIND_FR[k], "items": g})
    open_loads = [x for x in items if x["type"] == "load" and (x["observed"] or {}).get("state") != "done"
                  and x["status"] not in ("done", "dropped")]
    return {
        "type": type_,
        "updated": reg.updated,
        "note": reg.note,
        "checked_at": obs.get("_checked_at"),
        "counts": {
            "items": len(items),
            "open": sum(1 for x in items if x["status"] in ("open", "queued", "blocked")),
            "observed_done": sum(1 for x in items if (x["observed"] or {}).get("state") == "done"),
        },
        "disk_mb_open_loads": sum(x["disk_mb"] or 0 for x in open_loads if x["kind"] == "disk"),
        "disk_mb_unknown": sum(1 for x in open_loads if x["disk_mb"] is None),
        "groups": groups,
    }


def safe_pending() -> PendingRegistry | None:
    try:
        return load_pending()
    except ValueError:
        return None
