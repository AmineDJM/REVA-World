"""Atlas sections of the Fiche: a country's REVA headline series (cross-source best value, who answered, who was
consulted), an anchor's declared series (snapshot cells, or the published table read directly when the current
snapshot does not pin it yet — labelled), top countries of a catalog concept, and KG ``measured_by`` tables.

Every value keeps its cell provenance (table, raw artifact, selector, licence). Missing is typed: a series with no
cell is reported with its reason (not in the snapshot / loaded after the snapshot / never loaded / no value)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection

from reva.v8.entity import common as C

THEMES_PATH = Path(__file__).resolve().parents[4] / "registry" / "atlas" / "themes.yaml"
RECORDED_TTL_S = 300.0
MAX_POINTS = 120
_REC_LOCK = threading.Lock()
_REC: dict[str, Any] = {"at": 0.0, "tables": {}}
_TABLE_LOCK = threading.Lock()
_TABLES: OrderedDict[tuple[str, str, str], list[dict[str, Any]]] = OrderedDict()


@lru_cache(maxsize=1)
def theme_labels() -> dict[str, str]:
    try:
        doc = yaml.safe_load(THEMES_PATH.read_text(encoding="utf-8")) or {}
        return {k: str(v.get("fr") or k) for k, v in (doc.get("themes") or {}).items()}
    except (OSError, yaml.YAMLError):  # pragma: no cover
        return {}


# ------------------------------------------------------------------------------------------------ table status
def recorded_tables(conn: Connection) -> dict[str, dict[str, Any]]:
    """Latest live recording of every atlas table (``evidence.dataset``), cached for a few minutes."""
    from reva.atlas.snapshot import atlas_tables_as_of

    with _REC_LOCK:
        if time.monotonic() - _REC["at"] < RECORDED_TTL_S and _REC["tables"]:
            return dict(_REC["tables"])
    rows = atlas_tables_as_of(conn, cutoff=datetime.now(UTC), origin="live")
    tables = {str(r["dataset_key"]): r for r in rows}
    with _REC_LOCK:
        _REC.update(at=time.monotonic(), tables=tables)
    return tables


_PINNED: dict[str, frozenset[str]] = {}


def pinned_tables(q: Any) -> frozenset[str]:
    """``source.dataset`` keys the snapshot of ``q`` pins (computed once per snapshot)."""
    sha = str(q.snapshot.sha256)
    got = _PINNED.get(sha)
    if got is None:
        got = frozenset(f"{s}.{d}" for (s, d, _i) in q.sid_of)
        _PINNED.clear()
        _PINNED[sha] = got
    return got


def table_status(q: Any, recorded: Mapping[str, Any], table: str) -> dict[str, Any]:
    """Where a ``source.dataset`` table stands: in the snapshot, recorded after it, or never loaded."""
    pinned = table in pinned_tables(q)
    rec = recorded.get(f"atlas.{table}")
    if pinned:
        return {"status": "in_snapshot", "recorded_at": None}
    if rec is not None:
        return {"status": "after_snapshot", "recorded_at": str(rec["recorded_at"]), "artifact_sha256": rec["artifact_sha256"],
                "rights": rec.get("rights")}  # fmt: skip
    return {"status": "not_loaded", "recorded_at": None}


def status_reason_fr(st: Mapping[str, Any], table: str, snapshot_cutoff: str) -> tuple[str, str]:
    if st["status"] == "after_snapshot":
        return (
            "after_snapshot",
            f"table {table} chargée le {str(st['recorded_at'])[:16].replace('T', ' ')} UTC, après l’instantané atlas "
            f"courant ({snapshot_cutoff[:16].replace('T', ' ')} UTC) : visible au prochain instantané",
        )
    if st["status"] == "not_loaded":
        return "not_loaded", f"table {table} jamais chargée dans la base"
    return "no_value", f"aucune valeur pour ce sujet dans la table {table}"


# ------------------------------------------------------------------------------------------------ country
def country_series(q: Any, iso3: str) -> dict[str, Any]:
    """Headline dossier of one country: every REVA headline concept with its best series (projections excluded)."""
    from reva.atlas.source_order import cached_headlines

    reg = cached_headlines()
    d = q.headline_dossier(reg, iso3, include_series=True, projections="exclude")
    themes = theme_labels()
    cards = []
    for c in d["concepts"]:
        latest = c["latest"]
        ab = latest.get("answered_by") or {}
        pts = []
        for p in (c.get("series") or [])[-MAX_POINTS:]:
            pts.append(
                {"t": str(p["year"]), "x": float(p["year"]), "v": p["value"], "projection": bool(p.get("projection"))}
            )
        table = f"{ab.get('source')}.{ab.get('dataset')}" if ab.get("source") else None
        cards.append(
            {
                "key": c["concept_id"],
                "label_fr": c["labels"].get("fr") or c["labels"].get("en") or c["concept_id"],
                "theme": c["theme"],
                "theme_fr": themes.get(c["theme"], c["theme"]),
                "unit": latest.get("unit") or c.get("unit"),
                "value": latest.get("value"),
                "value_text": None,
                "period": str(latest.get("year")),
                "kind": "projection"
                if latest.get("projection")
                else ("derived" if latest.get("method") == "derived" else "observed"),
                "source": _headline_source(latest),
                "points": pts,
                "n_points": int(c.get("years") or len(pts)),
                "first": str(c.get("year_min")),
                "last": str(c.get("year_max")),
                "source_breaks": len(c.get("source_breaks") or []),
                "sources_used": list(c.get("sources_used") or []),
                "link": {"table": table, "params": {"indicator": str(ab.get("indicator")), "sel": iso3}}
                if table
                else None,
                "out_of_snapshot": False,
                "missing": None,
                "consulted": [{"series": x["series"], "status": x["status"]} for x in (latest.get("consulted") or [])],
            }
        )
    missing = []
    for w in d["without_data"]:
        consulted = w.get("consulted") or []
        statuses = sorted({x["status"] for x in consulted})
        missing.append(
            {
                "key": w["concept_id"],
                "theme": w["theme"],
                "consulted": [{"series": x["series"], "status": x["status"]} for x in consulted],
                "reason_fr": "aucune des sources déclarées ne publie de valeur pour ce pays ("
                + ", ".join(f"{x['series']} : {_consulted_fr(x['status'])}" for x in consulted[:4])
                + ")"
                if consulted
                else "aucune source déclarée",
                "reason_code": statuses[0] if len(statuses) == 1 else "no_value",
            }
        )
    return {"cards": cards, "missing": missing, "licences": d.get("licences") or []}


def _consulted_fr(status: str) -> str:
    return {
        "no_value": "pas de valeur",
        "projection_only": "projection seulement (exclue)",
        "not_equivalent_for_country": "non équivalent pour ce pays",
        "not_in_snapshot": "table absente de l’instantané",
        "answered": "a répondu",
        "also_reported": "rapporte aussi",
    }.get(status, status)


def _headline_source(cell: Mapping[str, Any]) -> dict[str, Any]:
    ab = cell.get("answered_by") or {}
    prov = cell.get("provenance") or {}
    src = str(ab.get("source") or "")
    return {
        "source": f"{src}.{ab.get('dataset')}" if src else "REVA (dérivé)",
        "label": C.source_name(src) if src else "REVA (dérivé)",
        "dataset": prov.get("dataset_key"),
        "statement_key": None,
        "series": ab.get("series"),
        "property": None,
        "raw_sha256": prov.get("raw_sha256"),
        "selector": prov.get("selector") if isinstance(prov.get("selector"), dict) else None,
        "retrieved_at": None,
        "period": f"{str(cell.get('period_start'))[:10]} → {str(cell.get('period_end'))[:10]}"
        if cell.get("period_start")
        else None,
        "licence": C.licence_of(cell.get("rights")),
        "verification": "derived" if cell.get("method") == "derived" else "source_reported_not_verified",
        "origin": None,
        "href": None,
    }


# ------------------------------------------------------------------------------------------------ anchors
def _cells_series(cells: list[dict[str, Any]], *, out_of_snapshot: bool) -> dict[str, Any]:
    cells = [c for c in cells if c.get("value") is not None or c.get("value_text") is not None]
    cells.sort(key=lambda c: str(c.get("period_start")))
    pts = [
        {
            "t": C.period_label(c.get("period_start"), c.get("period_end"), c.get("period_basis")) or "",
            "x": C.period_x(c.get("period_start")) or 0.0,
            "v": c.get("value"),
            "projection": bool(c.get("projection")),
        }
        for c in cells
    ]
    observed = [c for c in cells if not c.get("projection")]
    last = observed[-1] if observed else (cells[-1] if cells else None)
    return {"points": pts[-MAX_POINTS:], "last": last, "n": len(cells)}


def anchor_series(q: Any, recorded: Mapping[str, Any], spec: Mapping[str, Any], store: Any) -> list[dict[str, Any]]:
    from reva.atlas.catalog import cached_catalog

    cat = cached_catalog()
    cutoff = q.snapshot.cutoff.isoformat()
    out = []
    for s in spec.get("series") or []:
        card: dict[str, Any] = {
            "key": s["key"],
            "label_fr": s["label_fr"],
            "theme": None,
            "theme_fr": None,
            "unit": s.get("unit"),
            "value": None,
            "value_text": None,
            "period": None,
            "kind": "observed",
            "source": None,
            "points": [],
            "n_points": 0,
            "first": None,
            "last": None,
            "source_breaks": 0,
            "sources_used": [],
            "link": None,
            "out_of_snapshot": False,
            "missing": None,
            "consulted": [],
        }
        if s.get("catalog"):
            concept = cat.concepts.get(s["catalog"])
            if concept is None:
                card["missing"] = {
                    "code": "unknown_concept",
                    "reason_fr": f"concept {s['catalog']} absent du catalogue",
                }
                out.append(card)
                continue
            ser = concept.source_order[0]
            table, provider_geo, indicators = f"{ser.source}.{ser.dataset}", None, [ser.indicator]
            card["link"] = {"table": table, "params": {"indicator": ser.indicator}}
        else:
            table, provider_geo, indicators = s["table"], s.get("provider_geo"), [s["indicator"]]
            card["link"] = {
                "table": table,
                "params": {"indicator": s["indicator"], **({"geo": provider_geo} if provider_geo else {})},
            }
        st = table_status(q, recorded, table)
        src, _, ds = table.partition(".")
        cells: list[dict[str, Any]] = []
        if st["status"] == "in_snapshot":
            res = q.table_cells(cat, src, ds, provider_geo=provider_geo, indicators=indicators, limit=5000)
            cells = list(res["cells"])
        elif st["status"] == "after_snapshot" and store is not None:
            cells = read_recorded_cells(store, st, table, provider_geo, indicators[0])
            card["out_of_snapshot"] = True
        r = _cells_series(cells, out_of_snapshot=card["out_of_snapshot"])
        last = r["last"]
        if last is None:
            code, reason = status_reason_fr(st, table, cutoff)
            card["missing"] = {"code": code, "reason_fr": reason}
        else:
            card.update(
                value=last.get("value"),
                value_text=last.get("value_text"),
                unit=s.get("unit") or last.get("unit"),
                period=C.period_label(last.get("period_start"), last.get("period_end"), last.get("period_basis")),
                kind="projection" if last.get("projection") else "observed",
                source=C.atlas_source(last, out_of_snapshot=card["out_of_snapshot"]),
                points=r["points"],
                n_points=r["n"],
                first=r["points"][0]["t"] if r["points"] else None,
                last=r["points"][-1]["t"] if r["points"] else None,
                sources_used=[str(last.get("series"))],
            )
        out.append(card)
    return out


def read_recorded_cells(
    store: Any, st: Mapping[str, Any], table: str, provider_geo: str | None, indicator: str
) -> list[dict[str, Any]]:
    """Cells of a recorded (published) atlas table that the current snapshot does not pin yet, read from its
    verified artifact. Cached by artifact sha256 (immutable content)."""
    sha = str(st["artifact_sha256"])
    key = (sha, provider_geo or "", indicator)
    with _TABLE_LOCK:
        if key in _TABLES:
            _TABLES.move_to_end(key)
            return list(_TABLES[key])
    import polars as pl

    from reva.atlas.snapshot import normalise_rights
    from reva.atlas.tables import read_atlas_table

    df = read_atlas_table(store, sha)
    f = df.filter(pl.col("indicator") == indicator)
    if provider_geo:
        f = f.filter(pl.col("provider_geo") == provider_geo)
    src, _, ds = table.partition(".")
    rights = normalise_rights(st.get("rights"), basis="table_artifact") if st.get("rights") else None
    cells = []
    for r in f.sort("period_start").to_dicts():
        flags = r.get("flags")
        cells.append(
            {
                "value": None if r.get("value") is None else str(r["value"]),
                "value_text": r.get("value_text"),
                "unit": r.get("unit"),
                "source": src,
                "dataset": ds,
                "indicator": indicator,
                "series": f"{src}.{ds}.{indicator}",
                "provider_geo": r.get("provider_geo"),
                "period_start": C.iso(r.get("period_start")),
                "period_end": C.iso(r.get("period_end")),
                "period_basis": None,
                "flags": flags,
                "provenance": {
                    "raw_sha256": r.get("raw_sha256"),
                    "selector": r.get("selector") if isinstance(r.get("selector"), dict) else None,
                    "table_artifact_sha256": sha,
                    "dataset_key": f"atlas.{table}",
                },
                "rights": rights,
            }
        )
    with _TABLE_LOCK:
        _TABLES[key] = cells
        while len(_TABLES) > 32:
            _TABLES.popitem(last=False)
    return cells


def anchor_tops(q: Any, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Top countries of a catalog concept for its latest year with broad coverage (best-available cells)."""
    from reva.atlas.catalog import cached_catalog

    cat = cached_catalog()
    out = []
    this_year = datetime.now(UTC).year
    for t in spec.get("top") or []:
        if t["catalog"] not in cat.concepts:
            continue
        best: dict[str, Any] | None = None
        for y in range(this_year, this_year - 6, -1):
            m = q.map_values(cat, t["catalog"], y)
            vals = [v for v in m["values"] if v.get("value") is not None and not v.get("projection")]
            if len(vals) >= 20:
                best = {"year": y, "values": vals, "n": len(vals)}
                break
        if best is None:
            out.append(
                {
                    "key": t["key"],
                    "label_fr": t["label_fr"],
                    "year": None,
                    "items": [],
                    "unit": None,
                    "concept": t["catalog"],
                }
            )
            continue

        def num(v: Mapping[str, Any]) -> float:
            try:
                return float(v["value"])
            except (TypeError, ValueError):
                return float("-inf")

        top = sorted(best["values"], key=num, reverse=True)[: int(t.get("n", 5))]
        out.append(
            {
                "key": t["key"],
                "label_fr": t["label_fr"],
                "concept": t["catalog"],
                "year": best["year"],
                "n_countries": best["n"],
                "unit": top[0].get("unit") if top else None,
                "items": [{"iso3": v["iso3"], "value": v["value"], "source": C.atlas_source(v)} for v in top],
            }
        )
    return out


# ------------------------------------------------------------------------------------------------ measured_by
def measured_by(
    q: Any, groups: list[Mapping[str, Any]], fam: Mapping[str, Any], recorded: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Atlas tables a KG ``measured_by`` statement points at for this entity (regions, occupations)."""
    from reva.atlas.catalog import cached_catalog

    cat = cached_catalog()
    out = []
    for g in fam.get("families") or []:
        if g["family"] != "measured_by":
            continue
        for n in g["neighbours"][:6]:
            h = n.get("atlas") or {}
            if h.get("kind") != "table" or not h.get("source") or not h.get("dataset"):
                continue
            table = f"{h['source']}.{h['dataset']}"
            res = q.table_cells(cat, h["source"], h["dataset"], provider_geo=h.get("provider_geo"), iso3=h.get("iso3"),
                                indicators=h.get("indicators") or [], indicator_contains=h.get("indicator_contains"), limit=3000)  # fmt: skip
            by: dict[str, list[dict[str, Any]]] = {}
            for c in res["cells"]:
                by.setdefault(c["indicator"], []).append(c)
            for ind, cells in list(by.items())[:4]:
                r = _cells_series(cells, out_of_snapshot=False)
                last = r["last"]
                out.append(
                    {
                        "key": f"{table}|{ind}",
                        "label_fr": f"{table} — {ind}",
                        "theme": None,
                        "theme_fr": None,
                        "unit": last.get("unit") if last else None,
                        "value": last.get("value") if last else None,
                        "value_text": last.get("value_text") if last else None,
                        "period": C.period_label(
                            last.get("period_start"), last.get("period_end"), last.get("period_basis")
                        )
                        if last
                        else None,
                        "kind": "projection" if last and last.get("projection") else "observed",
                        "source": C.atlas_source(last) if last else None,
                        "points": r["points"],
                        "n_points": r["n"],
                        "first": r["points"][0]["t"] if r["points"] else None,
                        "last": r["points"][-1]["t"] if r["points"] else None,
                        "source_breaks": 0,
                        "sources_used": [],
                        "link": {
                            "table": table,
                            "params": {
                                "indicator": ind,
                                **({"geo": h["provider_geo"]} if h.get("provider_geo") else {}),
                            },
                        },
                        "out_of_snapshot": False,
                        "missing": None
                        if last
                        else {
                            "code": "no_value",
                            "reason_fr": status_reason_fr(
                                table_status(q, recorded, table), table, q.snapshot.cutoff.isoformat()
                            )[1],
                        },
                        "consulted": [],
                    }
                )
    return out
