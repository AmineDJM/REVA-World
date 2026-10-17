"""Inputs of an estimation run: outcome panels from a pinned atlas snapshot and treatment onsets from the
events layer (KG event entities) or from coded atlas country-year tables.

Outcome panels
--------------

:class:`SnapshotPanels` opens the per-snapshot DuckDB query file built by :mod:`reva.atlas.query`
(``atlas-<snapshot sha>-v6.duckdb`` in the atlas cache directory) **read-only**. It never builds, rewrites,
evicts or deletes a query file: it checks the sidecar digest record (format + snapshot sha) and the file's own
``meta`` table, and refuses (typed ``SnapshotNotMaterialised`` / ``IntegrityFailure``) when they do not name the
requested snapshot. Optionally (``verify_bytes=True``) it re-hashes the whole file against the sidecar digest
(several GB: slow) and refuses on mismatch without touching the file.

Two kinds of outcome references:

* ``concept:<catalog concept id>`` — the atlas **best-available** cell per (country, calendar year): among the
  concept's ``source_order`` series, the first that reports a value (same rule as ``AtlasQuery.best_cells``;
  no blending, no imputation; non-numeric cells are dropped and counted);
* ``series:<source>.<dataset>.<indicator>`` — one provider series verbatim.

Every panel carries the snapshot sha256, the series used (with cell counts and licences), and a digest over
its (iso3, year, value, raw sha256) rows, so a run can prove which bytes it read.

Treatments
----------

:func:`load_treatments` reads ``registry/estimation/treatments.yaml``. A treatment is a coded event type with an
onset date:

* ``source: kg`` — KG event entities (``kg.statement_current``) selected by id prefix and declarative filters
  on their statements; the country comes from an object QID (mapped to ISO3 only through the identifier
  registry ``registry/entities/country_qids.json``) or a qualifier holding the crosswalk key; the onset from a
  time-valued statement. As-of: statements recorded after ``as_of`` are ignored.
* ``source: atlas`` — a coded atlas series whose cell value meets a rule (e.g. ``crisisJST == 1``).

Nothing here maps or merges anything by name similarity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.errors import IntegrityFailure, MissingInput, ValidationProblem
from reva.ontology.registry import REGISTRY_ROOT

TREATMENTS_PATH = REGISTRY_ROOT / "estimation" / "treatments.yaml"
DB_SUFFIX = "-v6.duckdb"
DB_FORMAT = "atlas-duckdb/6"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        d = Decimal(v.strip())
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite():
        return None
    f = float(d)
    return f if math.isfinite(f) else None


# --------------------------------------------------------------------------- outcome panels
@dataclass
class PanelData:
    ref: str
    snapshot_sha256: str
    values: dict[tuple[str, int], float]
    unit: str | None
    series: list[dict[str, Any]]
    dropped: dict[str, int]
    digest: str
    label: str | None = None
    definition: str | None = None

    def years(self) -> tuple[int, int] | None:
        if not self.values:
            return None
        ys = [y for _, y in self.values]
        return min(ys), max(ys)

    def summary(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "label": self.label,
            "definition": self.definition,
            "unit": self.unit,
            "snapshot_sha256": self.snapshot_sha256,
            "cells": len(self.values),
            "countries": len({c for c, _ in self.values}),
            "years": self.years(),
            "series": self.series,
            "dropped": self.dropped,
            "panel_digest": self.digest,
        }


class SnapshotNotMaterialised(MissingInput):
    code = "snapshot_not_materialised"


class SnapshotPanels:
    """Read-only access to one atlas snapshot's DuckDB query file (see module docstring)."""

    def __init__(self, snapshot_sha256: str, directory: Path | None = None, *, verify_bytes: bool = False):
        import duckdb

        if not SHA_RE.match(snapshot_sha256):
            raise ValidationProblem("snapshot must be a sha256 hex digest")
        if directory is None:
            from reva.atlas.cachedir import cache_dir

            directory = cache_dir()
        path = directory / f"atlas-{snapshot_sha256}{DB_SUFFIX}"
        side = path.with_name(path.name + ".sha256")
        if not path.exists() or not side.exists():
            raise SnapshotNotMaterialised(
                "the atlas query file of this snapshot is not materialised (the lead builds it with "
                "`reva atlas snapshot --warm`); estimation never builds it",
                snapshot=snapshot_sha256,
            )
        rec = json.loads(side.read_text())
        if rec.get("format") != DB_FORMAT or rec.get("snapshot_sha256") != snapshot_sha256:
            raise IntegrityFailure("atlas query file digest record does not name this snapshot/format")
        if verify_bytes:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 22), b""):
                    h.update(chunk)
            if h.hexdigest() != rec.get("sha256"):
                raise IntegrityFailure("atlas query file bytes differ from their recorded digest (left untouched)")
        self.con = duckdb.connect(str(path), read_only=True)
        meta = self.con.execute("SELECT format, snapshot_sha256, origin, cutoff FROM meta").fetchone()
        if meta is None or meta[0] != DB_FORMAT or meta[1] != snapshot_sha256:
            self.con.close()
            raise IntegrityFailure("atlas query file meta does not name this snapshot")
        self.snapshot_sha256 = snapshot_sha256
        self.origin = str(meta[2])
        self.cutoff = str(meta[3])
        self.file_digest = str(rec.get("sha256"))
        self.bytes_verified = verify_bytes
        rows = self.con.execute("SELECT sid, source, dataset, indicator FROM series").fetchall()
        self.sid_of = {(r[1], r[2], r[3]): int(r[0]) for r in rows}
        self._rights: dict[str, Any] = {}

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> SnapshotPanels:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        return {
            "snapshot_sha256": self.snapshot_sha256,
            "origin": self.origin,
            "knowledge_cutoff": self.cutoff,
            "query_file_sha256": self.file_digest,
            "query_file_bytes_verified": self.bytes_verified,
        }

    def rights(self, source: str) -> Any:
        if source not in self._rights:
            try:
                from reva.atlas.query import source_spec_rights

                self._rights[source] = source_spec_rights(source)
            except Exception:  # pragma: no cover - rights lookup never breaks a read
                self._rights[source] = None
        return self._rights[source]

    def _cells(self, sids: Sequence[tuple[int, int]]) -> list[tuple[Any, ...]]:
        if not sids:
            return []
        values = ",".join(f"({int(s)},{int(r)})" for s, r in sids)
        return self.con.execute(
            f"""WITH cs(sid, rnk) AS (VALUES {values})
                SELECT x.iso3, x.year, x.value, x.value_text, x.unit, x.raw_sha256, x.sid, cs.rnk, x.table_sha
                FROM cells_s x JOIN cs ON x.sid = cs.sid
                WHERE x.iso3 IS NOT NULL AND x.year IS NOT NULL AND x.has_value
                QUALIFY row_number() OVER (PARTITION BY x.iso3, x.year ORDER BY cs.rnk) = 1
                ORDER BY x.iso3, x.year"""
        ).fetchall()

    def _panel(
        self, ref: str, keys: Sequence[tuple[str, str, str]], label: str | None, definition: str | None
    ) -> PanelData:
        sids = [(self.sid_of[k], rank) for rank, k in enumerate(keys) if k in self.sid_of]
        missing = [".".join(k) for k in keys if k not in self.sid_of]
        rows = self._cells(sids)
        key_of = {self.sid_of[k]: k for k in keys if k in self.sid_of}
        values: dict[tuple[str, int], float] = {}
        per: dict[str, dict[str, Any]] = {}
        dropped = {"non_numeric": 0}
        units: set[str] = set()
        digest_rows = []
        for iso3, year, value, _vt, unit, raw, sid, _rank, table_sha in rows:
            v = _num(value)
            if v is None:
                dropped["non_numeric"] += 1
                continue
            values[(str(iso3), int(year))] = v
            src, ds, ind = key_of[int(sid)]
            skey = f"{src}.{ds}.{ind}"
            e = per.setdefault(skey, {"series": skey, "cells": 0, "tables": set(), "raw_artifacts": set()})
            e["cells"] += 1
            e["tables"].add(table_sha)
            e["raw_artifacts"].add(raw)
            units.add(str(unit))
            digest_rows.append([iso3, int(year), value, raw, skey])
        series = []
        for skey, e in sorted(per.items()):
            series.append(
                {
                    "series": skey,
                    "cells": e["cells"],
                    "table_artifacts": sorted(e["tables"]),
                    "raw_artifacts": len(e["raw_artifacts"]),
                    "rights": self.rights(skey.split(".", 1)[0]),
                }
            )
        for m in missing:
            series.append({"series": m, "cells": 0, "absent_from_snapshot": True})
        return PanelData(
            ref=ref,
            snapshot_sha256=self.snapshot_sha256,
            values=values,
            unit=" | ".join(sorted(units)) if units else None,
            series=series,
            dropped=dropped,
            digest=canonical_sha256(digest_rows),
            label=label,
            definition=definition,
        )

    def concept(self, concept_id: str) -> PanelData:
        from reva.atlas.catalog import cached_catalog

        cat = cached_catalog()
        c = cat.concepts.get(concept_id)
        if c is None:
            raise MissingInput("unknown atlas concept", concept=concept_id)
        keys = [(s.source, s.dataset, s.indicator) for s in c.source_order]
        return self._panel(f"concept:{concept_id}", keys, c.labels.get("en"), c.definition)

    def series(self, key: str) -> PanelData:
        parts = key.split(".", 2)
        if len(parts) != 3 or not all(parts):
            raise ValidationProblem("series must be '<source>.<dataset>.<indicator>'", series=key)
        return self._panel(f"series:{key}", [(parts[0], parts[1], parts[2])], None, None)

    def quarterly_series(self, key: str) -> PanelData:
        """One provider series at calendar-quarter frequency: cells whose period is exactly a calendar quarter
        (``[YYYY-01/04/07/10-01, +3 months)``), keyed by the quarter index ``4 * year + quarter - 1``."""
        parts = key.split(".", 2)
        if len(parts) != 3 or (parts[0], parts[1], parts[2]) not in self.sid_of:
            raise MissingInput("series absent from the snapshot", series=key)
        sid = self.sid_of[(parts[0], parts[1], parts[2])]
        rows = self.con.execute(
            """SELECT x.iso3, year(x.period_start) AS y, month(x.period_start) AS m, x.value, x.raw_sha256, x.unit
               FROM cells_s x
               WHERE x.sid = ? AND x.iso3 IS NOT NULL AND x.has_value AND day(x.period_start) = 1
                 AND month(x.period_start) IN (1, 4, 7, 10) AND x.period_end = x.period_start + INTERVAL 3 MONTH
               ORDER BY 1, 2, 3""",
            [sid],
        ).fetchall()
        values: dict[tuple[str, int], float] = {}
        dropped = {"non_numeric": 0}
        units: set[str] = set()
        digest_rows = []
        for iso3, y, m, value, raw, unit in rows:
            v = _num(value)
            if v is None:
                dropped["non_numeric"] += 1
                continue
            values[(str(iso3), 4 * int(y) + (int(m) - 1) // 3)] = v
            units.add(str(unit))
            digest_rows.append([iso3, int(y), int(m), value, raw])
        return PanelData(
            ref=f"series:{key}",
            snapshot_sha256=self.snapshot_sha256,
            values=values,
            unit=" | ".join(sorted(units)) if units else None,
            series=[{"series": key, "cells": len(values), "rights": self.rights(parts[0]), "frequency": "quarter"}],
            dropped=dropped,
            digest=canonical_sha256(digest_rows),
        )

    def outcome(self, ref: str, frequency: str = "annual") -> PanelData:
        kind, _, ident = ref.partition(":")
        if frequency == "quarter":
            if kind != "series":
                raise ValidationProblem("quarterly outcomes are provider series ('series:<key>')", ref=ref)
            return self.quarterly_series(ident)
        if kind == "concept":
            return self.concept(ident)
        if kind == "series":
            return self.series(ident)
        raise ValidationProblem("outcome must be 'concept:<id>' or 'series:<source>.<dataset>.<indicator>'", ref=ref)


# --------------------------------------------------------------------------- treatments
@dataclass(frozen=True)
class Treatment:
    id: str
    event_type: str
    source: str  # kg | atlas
    definition: str
    citation: str
    coverage_years: tuple[int, int]
    spec: Mapping[str, Any]
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "source": self.source,
            "definition": self.definition,
            "citation": self.citation,
            "coverage_years": list(self.coverage_years),
            "selection": dict(self.spec),
            "notes": self.notes,
        }


def load_treatments(path: Path = TREATMENTS_PATH) -> dict[str, Treatment]:
    doc = yaml.safe_load(path.read_text())
    out: dict[str, Treatment] = {}
    for t in doc["treatments"]:
        if t["id"] in out:
            raise ValidationProblem("duplicate treatment id", id=t["id"])
        if t["source"] not in ("kg", "atlas"):
            raise ValidationProblem("treatment source must be kg or atlas", id=t["id"])
        cov = t["coverage_years"]
        out[t["id"]] = Treatment(
            id=t["id"],
            event_type=t["event_type"],
            source=t["source"],
            definition=t["definition"],
            citation=t["citation"],
            coverage_years=(int(cov[0]), int(cov[1])),
            spec=t["selection"],
            notes=t.get("notes", ""),
        )
    return out


@dataclass(frozen=True)
class Onset:
    event_id: str
    iso3: str
    year: int
    date: str
    precision: int | None
    attrs: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class EventSet:
    treatment: Treatment
    onsets: list[Onset]
    skipped: dict[str, int]
    provenance: dict[str, Any]

    @property
    def digest(self) -> str:
        return canonical_sha256([[o.event_id, o.iso3, o.date] for o in sorted(self.onsets, key=lambda o: o.event_id)])

    def counts(self) -> dict[tuple[str, int], int]:
        out: dict[tuple[str, int], int] = {}
        for o in self.onsets:
            out[(o.iso3, o.year)] = out.get((o.iso3, o.year), 0) + 1
        return out

    def summary(self) -> dict[str, Any]:
        years = [o.year for o in self.onsets]
        return {
            "treatment": self.treatment.as_dict(),
            "onsets": len(self.onsets),
            "countries": len({o.iso3 for o in self.onsets}),
            "years": [min(years), max(years)] if years else None,
            "skipped": self.skipped,
            "event_set_digest": self.digest,
            "provenance": self.provenance,
        }


def _iso_year(t: str) -> int:
    m = re.match(r"^([+-]?\d+)-", t)
    if not m:
        raise ValidationProblem("unparseable time value", value=t)
    return int(m.group(1))


def _cond_sql(i: int, cond: Mapping[str, Any], params: dict[str, Any]) -> str:
    """One filter condition: an EXISTS over the subject's current statements of ``property``."""
    params[f"p{i}"] = cond["property"]
    if "qualifier" in cond:
        val = f"(s.qualifiers->>:q{i})"
        params[f"q{i}"] = cond["qualifier"]
    elif cond.get("object"):
        val = "s.object_qid"
    else:
        val = "coalesce(s.value->>'value', s.value->>'amount')"
    extra = ""
    if "where_qualifier" in cond:
        extra = f" AND s.qualifiers->>:wq{i} = :wv{i}"
        params[f"wq{i}"], params[f"wv{i}"] = cond["where_qualifier"]["key"], cond["where_qualifier"]["equals"]
    if "in" in cond:
        params[f"v{i}"] = list(cond["in"])
        test = f"{val} = ANY(:v{i})"
    elif "equals" in cond:
        params[f"v{i}"] = str(cond["equals"])
        test = f"{val} = :v{i}"
    elif "min" in cond or "max" in cond:
        parts = []
        if "min" in cond:
            params[f"lo{i}"] = Decimal(str(cond["min"]))
            parts.append(f"CAST({val} AS numeric) >= :lo{i}")
        if "max" in cond:
            params[f"hi{i}"] = Decimal(str(cond["max"]))
            parts.append(f"CAST({val} AS numeric) <= :hi{i}")
        test = f"{val} ~ '^-?[0-9]+(\\.[0-9]+)?$' AND " + " AND ".join(parts)
    else:
        raise ValidationProblem("filter needs in/equals/min/max", condition=dict(cond))
    neg = "NOT " if cond.get("negate") else ""
    return (
        f"{neg}EXISTS (SELECT 1 FROM kg.statement_current s WHERE s.subject_qid = e.qid "
        f"AND s.property_pid = :p{i} AND s.origin = :origin AND s.recorded_at <= :asof{extra} AND {test})"
    )


def kg_onsets(conn: Any, t: Treatment, *, origin: str = "live", as_of: datetime | None = None) -> EventSet:
    """Onsets of a ``source: kg`` treatment (see module docstring)."""
    from sqlalchemy import text

    from reva.kg.query import country_qid_registry

    sel = t.spec
    as_of = as_of or datetime.max.replace(tzinfo=None)
    params: dict[str, Any] = {"pfx": sel["id_prefix"].replace("_", r"\_") + "%", "origin": origin, "asof": as_of}
    conds = [_cond_sql(i, c, params) for i, c in enumerate(sel.get("filters", []))]
    onset_p = sel["onset"]["property"]
    country = sel["country"]
    params["onset_p"] = onset_p
    params["country_p"] = country["property"]
    where = " AND ".join(["e.qid LIKE :pfx", *conds])
    sql = f"""
        WITH e AS (SELECT DISTINCT subject_qid AS qid FROM kg.statement_current
                   WHERE subject_qid LIKE :pfx AND origin = :origin AND recorded_at <= :asof)
        SELECT e.qid,
               (SELECT json_agg(json_build_object('t', s.value->>'time', 'p', s.value->>'precision', 'rank', s.rank,
                                                  'raw', s.raw_sha256, 'key', s.statement_key, 'src', s.source_key,
                                                  'rec', s.recorded_at)
                                ORDER BY s.rank = 'preferred' DESC, s.value->>'time')
                  FROM kg.statement_current s WHERE s.subject_qid = e.qid AND s.property_pid = :onset_p
                   AND s.origin = :origin AND s.recorded_at <= :asof) AS onset,
               (SELECT json_agg(json_build_object('o', s.object_qid, 'q', s.qualifiers, 'rank', s.rank)
                                ORDER BY s.statement_key)
                  FROM kg.statement_current s WHERE s.subject_qid = e.qid AND s.property_pid = :country_p
                   AND s.origin = :origin AND s.recorded_at <= :asof) AS country
        FROM e WHERE {where} ORDER BY e.qid"""
    rows = conn.execute(text(sql), params).mappings().all()
    qid_to_iso: dict[str, set[str]] = {}
    for iso, q in country_qid_registry().items():
        qid_to_iso.setdefault(q, set()).add(iso)
    skipped = {"no_onset": 0, "no_country": 0, "unmapped_country_qid": 0, "ambiguous_country": 0, "multi_country": 0}
    onsets: list[Onset] = []
    sources: set[str] = set()
    raws: set[str] = set()
    rec_max: datetime | None = None
    for r in rows:
        on = r["onset"] or []
        if not on or not on[0].get("t"):
            skipped["no_onset"] += 1
            continue
        first = on[0]
        cands = r["country"] or []
        isos: set[str] = set()
        bad = False
        for c in cands:
            if country.get("via") == "qualifier":
                k = (c.get("q") or {}).get(country["key"])
                if k:
                    isos.add(str(k))
            else:
                q = c.get("o")
                m = qid_to_iso.get(q or "")
                if not m:
                    bad = True
                elif len(m) > 1:
                    skipped["ambiguous_country"] += 1
                    bad = True
                else:
                    isos |= m
        if not isos:
            skipped["unmapped_country_qid" if bad else "no_country"] += 1
            continue
        if len(isos) > 1:
            if not country.get("allow_multi", False):
                skipped["multi_country"] += 1
                continue
        for iso in sorted(isos):
            onsets.append(
                Onset(
                    event_id=r["qid"],
                    iso3=iso,
                    year=_iso_year(first["t"]),
                    date=first["t"],
                    precision=int(first["p"]) if first.get("p") is not None else None,
                    attrs={"statement_key": first.get("key")},
                )
            )
        sources.add(str(first.get("src")))
        if first.get("raw"):
            raws.add(str(first["raw"]))
        rec = first.get("rec")
        if rec:
            d = datetime.fromisoformat(str(rec))
            rec_max = d if rec_max is None or d > rec_max else rec_max
    lo, hi = t.coverage_years
    outside = [o for o in onsets if not lo <= o.year <= hi]
    onsets = [o for o in onsets if lo <= o.year <= hi]
    skipped["outside_coverage"] = len(outside)
    prov = {
        "kind": "kg",
        "origin": origin,
        "as_of_filter": as_of.isoformat() if as_of.year < 9999 else None,
        "source_keys": sorted(sources),
        "raw_artifacts": len(raws),
        "raw_sha256_digest": canonical_sha256(sorted(raws)),
        "latest_statement_recorded_at": rec_max.isoformat() if rec_max else None,
        "events_read": len(rows),
    }
    return EventSet(t, onsets, skipped, prov)


def atlas_onsets(panels: SnapshotPanels, t: Treatment) -> EventSet:
    """Onsets of a ``source: atlas`` treatment: cells of ``selection.series`` whose numeric value meets
    ``selection.rule`` (``{op: ">=", value: 1}``); one onset per (country, year)."""
    sel = t.spec
    p = panels.series(sel["series"])
    op, thr = sel["rule"]["op"], float(sel["rule"]["value"])
    if op not in (">=", "==", ">"):
        raise ValidationProblem("rule op must be >=, == or >", op=op)

    def ok(v: float) -> bool:
        return v >= thr if op == ">=" else (v == thr if op == "==" else v > thr)

    lo, hi = t.coverage_years
    onsets = [
        Onset(f"{sel['series']}|{c}|{y}", c, y, f"{y:04d}-01-01", 9)
        for (c, y), v in sorted(p.values.items())
        if ok(v) and lo <= y <= hi
    ]
    return EventSet(
        t,
        onsets,
        {"non_numeric": p.dropped.get("non_numeric", 0)},
        {"kind": "atlas", "snapshot_sha256": p.snapshot_sha256, "series": p.series, "panel_digest": p.digest},
    )


def treatment_countries(events: EventSet, panel: PanelData, universe: str) -> list[str] | None:
    """Countries at risk: ``all`` (every country of the outcome panel) or ``treated_dataset`` (countries the
    event dataset codes at least once)."""
    if universe == "all":
        return None
    if universe == "treated_dataset":
        return sorted({o.iso3 for o in events.onsets})
    raise ValidationProblem("unknown universe", universe=universe)
