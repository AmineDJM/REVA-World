"""Link store retention (agent LINKRET): ``reva links prune``.

Every full materialisation of the link store (``kg.link_row``, migration 0012) is ~1.2–1.4 GB on the dev database
and the store was append-only with no deletion path, so each daily materialisation with new inputs grew the disk
without bound. Link rows are a **derived cache**: each materialisation records its inputs (atlas snapshot sha256,
KG load watermark, taxonomy sha256, method version, top-k) in ``kg.link_materialisation``, which is never deleted.
Pruning deletes only the ``kg.link_row`` rows of materialisations nothing serves, pins or references, through the
owner-only database function ``kg.prune_link_materialisation`` (migration 0017_links_retention), which records a
``kg.link_prune`` row (rows deleted, their bytes, the policy) in the same transaction; this module also writes one
``ops.audit`` row (action ``links.prune``) per pruned materialisation.

Kept (never pruned), with the reason reported for each:

* ``latest`` — the newest materialisation (the path engine reads ``latest_materialisation(conn)``);
* ``latest_live`` — the newest live-atlas (or atlas-less) + live-KG one (what production's API serves);
* ``latest_lineage:<atlas origin>/<kg origin>`` — the newest per lineage (fixture and live stores coexist);
* ``recent`` — the newest ``keep`` materialisations (default 2, the current one included);
* ``current_atlas_snapshot:<origin>`` — the newest materialisation built from the latest recorded atlas snapshot
  of each origin (``reva.atlas.snapshot.latest_snapshot_ref``);
* ``atlas_pin`` — the newest one built from the snapshot pinned in the atlas query cache (``live-snapshot.pin``);
* ``fk:<schema.table>.<column>`` — any materialisation referenced by a foreign key from any table other than the
  store's own (found from the catalog, so a future release/pin table is honoured without changing this code).

References checked and found not to pin a materialisation (2026-10-17): ``belief.release`` /
``release_artifact`` / ``release_dataset`` and ``ops.publication`` pin artefacts and datasets, never link rows;
no ``meta.artifact`` schema embeds a materialisation id; ``/v1/links*`` responses are ``Cache-Control: no-store``
and NLQA answers are not persisted; ``ops.job`` results of ``links_materialise`` name their materialisation id —
an operational log that stays resolvable because the materialisation row (and its inputs) is kept.
The database independently refuses pruning the latest materialisation of a lineage or the latest live one.

Rebuilding a pruned materialisation: ``reva links materialise --atlas-snapshot <sha> --kg-origin <origin>``
re-derives from the same immutable atlas snapshot, but from the KG as it is then (the current-statement view and
the context reads are not watermark-bounded), so a rebuild can differ — anything that must stay bit-reproducible
must stay referenced (kept) rather than rebuilt.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

DEFAULT_KEEP = 2
AUDIT_ACTOR = "owner:links-prune"
AUDIT_ACTION = "links.prune"
STORE_TABLES = ("kg.link_row", "kg.link_prune")
VACUUM_MODES = ("none", "plain", "full")
#: a prune of one materialisation deletes ~1 M rows: allow it more than the default statement timeout
PRUNE_STATEMENT_TIMEOUT = "30min"


class RetentionError(RuntimeError):
    pass


@dataclass
class Materialisation:
    id: int
    atlas_snapshot_sha256: str | None
    atlas_origin: str | None
    kg_origin: str | None
    kg_load_watermark: int | None
    link_count: int
    recorded_at: Any
    pruned: bool

    @property
    def live_served(self) -> bool:
        return (self.atlas_origin or "live") == "live" and self.kg_origin == "live"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "atlas_snapshot_sha256": self.atlas_snapshot_sha256,
            "atlas_origin": self.atlas_origin,
            "kg_origin": self.kg_origin,
            "kg_load_watermark": self.kg_load_watermark,
            "link_count": self.link_count,
            "recorded_at": self.recorded_at.isoformat() if hasattr(self.recorded_at, "isoformat") else None,
        }


@dataclass
class PrunePlan:
    keep: int
    materialisations: list[Materialisation]
    kept: dict[int, list[str]]
    prune: list[Materialisation]
    already_pruned: list[int]
    relation_bytes: int
    relation_rows: int
    notes: list[str] = field(default_factory=list)

    def estimated_bytes(self, m: Materialisation) -> int:
        """Share of ``kg.link_row``'s total size (heap + TOAST + indexes) in proportion to the rows."""
        if self.relation_rows <= 0:
            return 0
        return int(self.relation_bytes * min(m.link_count, self.relation_rows) / self.relation_rows)

    def policy(self) -> dict[str, Any]:
        return {"keep": self.keep, "kept": {str(k): v for k, v in sorted(self.kept.items())}}

    def to_json(self) -> dict[str, Any]:
        return {
            "keep": self.keep,
            "kept": [{**m.to_json(), "reasons": self.kept[m.id]} for m in self.materialisations if m.id in self.kept],
            "prune": [{**m.to_json(), "estimated_bytes": self.estimated_bytes(m)} for m in self.prune],
            "already_pruned": self.already_pruned,
            "prune_rows": sum(m.link_count for m in self.prune),
            "prune_estimated_bytes": sum(self.estimated_bytes(m) for m in self.prune),
            "link_row_total_bytes": self.relation_bytes,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------------------------- references
def _fk_references(conn: Connection) -> dict[int, list[str]]:
    """Materialisation ids referenced by a foreign key from any table other than the store's own."""
    fks = conn.execute(
        text(
            """SELECT n.nspname AS sch, r.relname AS tbl, a.attname AS col
                 FROM pg_constraint c
                 JOIN pg_class r ON r.oid = c.conrelid
                 JOIN pg_namespace n ON n.oid = r.relnamespace
                 JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
                WHERE c.contype = 'f' AND c.confrelid = 'kg.link_materialisation'::regclass
                  AND cardinality(c.conkey) = 1
                  AND n.nspname || '.' || r.relname <> ALL(CAST(:own AS text[]))
                ORDER BY 1, 2, 3"""
        ),
        {"own": list(STORE_TABLES)},
    ).all()
    out: dict[int, list[str]] = {}
    for sch, tbl, col in fks:
        q = ".".join('"' + str(x).replace('"', '""') + '"' for x in (sch, tbl))
        col_q = '"' + str(col).replace('"', '""') + '"'
        for i in conn.execute(text(f"SELECT DISTINCT {col_q} FROM {q} WHERE {col_q} IS NOT NULL")).scalars():
            out.setdefault(int(i), []).append(f"fk:{sch}.{tbl}.{col}")
    return out


def _pinned_atlas_sha(pin_dir: Path | None) -> str | None:
    from reva.atlas.query import PIN_FILE

    try:
        if pin_dir is None:
            from reva.atlas.query import cache_dir

            pin_dir = cache_dir()
        v = (pin_dir / PIN_FILE).read_text().strip()
    except Exception:  # no cache directory / no pin: nothing pinned here
        return None
    return v if len(v) == 64 and all(c in "0123456789abcdef" for c in v) else None


def _current_snapshots(conn: Connection) -> dict[str, str]:
    from reva.atlas.snapshot import latest_snapshot_ref

    out: dict[str, str] = {}
    for origin in ("live", "test_fixture"):
        ref = latest_snapshot_ref(conn, origin)
        if ref and ref.get("sha"):
            out[origin] = str(ref["sha"])
    return out


# ---------------------------------------------------------------------------------------------- plan
def plan_prune(conn: Connection, *, keep: int = DEFAULT_KEEP, pin_dir: Path | None = None) -> PrunePlan:
    """Which materialisations to keep (and why) and which to prune. Reads only."""
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        raise RetentionError("--keep must be an integer >= 1 (the current materialisation is always kept)")
    rows = conn.execute(
        text(
            """SELECT m.id, m.atlas_snapshot_sha256, m.atlas_origin, m.kg_origin, m.kg_load_watermark, m.link_count,
                      m.recorded_at, (p.materialisation_id IS NOT NULL) AS pruned
                 FROM kg.link_materialisation m LEFT JOIN kg.link_prune p ON p.materialisation_id = m.id
                ORDER BY m.id"""
        )
    ).mappings()
    allm = [Materialisation(**dict(r)) for r in rows]
    live = [m for m in allm if not m.pruned]
    kept: dict[int, list[str]] = {}

    def keep_(m: Materialisation | None, why: str) -> None:
        if m is not None:
            kept.setdefault(m.id, []).append(why)

    def newest(ms: list[Materialisation]) -> Materialisation | None:
        return max(ms, key=lambda m: m.id) if ms else None

    keep_(newest(live), "latest")
    keep_(newest([m for m in live if m.live_served]), "latest_live")
    lineages = sorted({(m.atlas_origin, m.kg_origin) for m in live}, key=lambda t: (str(t[0]), str(t[1])))
    for ao, ko in lineages:
        keep_(
            newest([m for m in live if (m.atlas_origin, m.kg_origin) == (ao, ko)]),
            f"latest_lineage:{ao or 'none'}/{ko or 'any'}",
        )
    for m in sorted(live, key=lambda m: m.id, reverse=True)[:keep]:
        keep_(m, "recent")
    notes: list[str] = []
    for origin, sha in _current_snapshots(conn).items():
        hit = newest([m for m in live if m.atlas_snapshot_sha256 == sha])
        keep_(hit, f"current_atlas_snapshot:{origin}")
    pinned = _pinned_atlas_sha(pin_dir)
    if pinned:
        hit = newest([m for m in live if m.atlas_snapshot_sha256 == pinned])
        keep_(hit, "atlas_pin")
        if hit is None:
            notes.append(f"atlas pin {pinned[:12]}: no materialisation of that snapshot")
    for mid, why in _fk_references(conn).items():
        for w in why:
            keep_(next((m for m in live if m.id == mid), None), w)
    size = conn.execute(
        text(
            "SELECT pg_total_relation_size('kg.link_row'), "
            "(SELECT coalesce(sum(link_count), 0) FROM kg.link_materialisation m "
            " WHERE NOT EXISTS (SELECT 1 FROM kg.link_prune p WHERE p.materialisation_id = m.id))"
        )
    ).one()
    return PrunePlan(
        keep=keep,
        materialisations=allm,
        kept=kept,
        prune=[m for m in live if m.id not in kept],
        already_pruned=[m.id for m in allm if m.pruned],
        relation_bytes=int(size[0]),
        relation_rows=int(size[1]),
        notes=notes,
    )


# ---------------------------------------------------------------------------------------------- prune
def _relation_sizes(conn: Connection) -> dict[str, int]:
    r = conn.execute(
        text(
            "SELECT pg_total_relation_size('kg.link_row'), pg_relation_size('kg.link_row'), "
            "pg_indexes_size('kg.link_row'), pg_database_size(current_database())"
        )
    ).one()
    return {
        "link_row_total": int(r[0]),
        "link_row_heap": int(r[1]),
        "link_row_indexes": int(r[2]),
        "database": int(r[3]),
    }


def vacuum(engine: Engine, mode: str) -> dict[str, Any]:
    """``VACUUM (ANALYZE)`` (space reusable by the next materialisation, no lock against readers) or
    ``VACUUM (FULL, ANALYZE)`` (rewrites the table and returns the space to the OS; ACCESS EXCLUSIVE lock,
    needs free disk equal to the remaining table) of ``kg.link_row``."""
    if mode not in VACUUM_MODES:
        raise RetentionError(f"vacuum mode must be one of {VACUUM_MODES}")
    if mode == "none":
        return {"mode": "none"}
    with engine.connect() as c:
        before = _relation_sizes(c)
        c.rollback()
        ac = c.execution_options(isolation_level="AUTOCOMMIT")
        ac.execute(text("SET statement_timeout = 0"))
        try:
            ac.execute(text("VACUUM (FULL, ANALYZE) kg.link_row" if mode == "full" else "VACUUM (ANALYZE) kg.link_row"))
            ac.execute(text("VACUUM (ANALYZE) kg.link_prune"))
        finally:
            ac.execute(text("RESET statement_timeout"))
        after = _relation_sizes(c)
        c.rollback()
    out: dict[str, Any] = {
        "mode": mode,
        "before": before,
        "after": after,
        "freed_bytes": before["link_row_total"] - after["link_row_total"],
    }
    if mode == "plain":
        out["note"] = "plain VACUUM makes the space reusable by later materialisations; it is not returned to the OS"
    return out


def prune(
    engine: Engine,
    *,
    keep: int = DEFAULT_KEEP,
    dry_run: bool = False,
    reason: str = "link store retention policy",
    vacuum_mode: str = "plain",
    pin_dir: Path | None = None,
    log_fn: Callable[[str], None] = lambda s: None,
) -> dict[str, Any]:
    """Apply the retention policy with the **owner** engine (``make_engine("migration")``).

    One transaction per pruned materialisation: ``kg.prune_link_materialisation`` (the ``kg.link_prune`` record,
    the deletion, the count check) plus one ``ops.audit`` row. ``dry_run`` reads only."""
    from reva.operations import audit

    if vacuum_mode not in VACUUM_MODES:
        raise RetentionError(f"vacuum mode must be one of {VACUUM_MODES}")
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise RetentionError("reason must be 1..500 characters")
    with engine.connect() as c:
        plan = plan_prune(c, keep=keep, pin_dir=pin_dir)
        sizes_before = _relation_sizes(c)
        c.rollback()
    out: dict[str, Any] = {"dry_run": dry_run, **plan.to_json(), "sizes_before": sizes_before}
    if dry_run or not plan.prune:
        out["pruned"] = []
        out["vacuum"] = {"mode": "none", "skipped": "dry run" if dry_run else "nothing pruned"}
        return out
    run_id = uuid.uuid4()
    policy = {**plan.policy(), "tool": "reva links prune", "run_id": str(run_id)}
    done: list[dict[str, Any]] = []
    for m in plan.prune:
        est = plan.estimated_bytes(m)
        log_fn(f"pruning materialisation {m.id} ({m.link_count} links, ~{est / 1e6:.0f} MB)")
        with engine.begin() as c:
            c.execute(text(f"SET LOCAL statement_timeout = '{PRUNE_STATEMENT_TIMEOUT}'"))
            res = c.execute(
                text(
                    "SELECT rows_deleted, row_bytes FROM kg.prune_link_materialisation(:i, :r, :why, CAST(:p AS jsonb))"
                ),
                {"i": m.id, "r": str(run_id), "why": reason, "p": json.dumps(policy)},
            ).one()
            rec = {
                "materialisation_id": m.id,
                "rows_deleted": int(res.rows_deleted),
                "row_bytes": int(res.row_bytes),
                "estimated_disk_bytes": est,
                "atlas_snapshot_sha256": m.atlas_snapshot_sha256,
                "atlas_origin": m.atlas_origin,
                "kg_origin": m.kg_origin,
                "kg_load_watermark": m.kg_load_watermark,
            }
            aid = audit.record(
                c,
                AUDIT_ACTOR,
                AUDIT_ACTION,
                {"materialisation_id": m.id, "run_id": str(run_id)},
                {**rec, "reason": reason, "keep": plan.keep, "kept": policy["kept"]},
            )
        done.append({**rec, "audit_id": str(aid)})
    out["run_id"] = str(run_id)
    out["pruned"] = done
    out["pruned_rows"] = sum(d["rows_deleted"] for d in done)
    out["pruned_row_bytes"] = sum(d["row_bytes"] for d in done)
    out["vacuum"] = vacuum(engine, vacuum_mode)
    return out
