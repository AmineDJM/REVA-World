"""Event-card demonstration in a THROWAWAY database (agent EVENTS).

``uv run python -m reva.events.demo [--db reva_events_demo] [--keep] [--fetch-labels N] [--out DIR]``

1. creates and migrates a throwaway database (name ``reva_events_*``; the dev database ``reva`` is refused);
2. loads the finished agents' dataset fixtures through the atlas platform (UCDP, COW, CShapes, UN peace
   operations, UNTC treaties: ``mode=fixture``, no network);
3. loads Wikidata snapshots of the demonstration events (Suez Crisis, Treaty of Tordesillas, Wall Street crash of
   1929, Algerian War) and label snapshots of the entities they cite, from the archive when an archived
   ``Special:EntityData`` body exists, else fetched through the locked, budget-metered client
   (:func:`reva.events.verify.wbapi_client`); statements are extracted by WDHARVEST's own code (allow-list,
   ranks, qualifiers, references, revision ids);
4. derives evidence-based ``same_as`` links, then writes one event card per event (JSON) and a text rendering;
5. drops the database unless ``--keep``.

Nothing is written to ``reva``. Raw artifacts are copied into the demo's own object store.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_EVENTS = {
    "Q49101": "Suez Crisis",
    "Q180897": "Treaty of Tordesillas",
    "Q201684": "Wall Street crash of 1929",
    "Q200790": "Algerian War",
}
DATASET_EVENTS = ("ucdp:conflict:246", "unpk:mission:UNEF_I", "cow:war:1")
ARCHIVE_INDEX = REPO_ROOT / "var" / "kg" / "archive_index.jsonl"
ARCHIVE_ROOT = REPO_ROOT / "var" / "kg" / "objects"


def _settings(db_name: str, object_root: Path) -> Any:
    from reva.settings import Settings

    if db_name == "reva" or not re.match(r"^reva_events_[a-z0-9_]{1,40}$", db_name):
        raise SystemExit(f"refusing database {db_name!r}: the demo runs only in a throwaway reva_events_* database")
    base = Settings(_env_file=REPO_ROOT / ".env")
    data = base.model_dump()
    data.update(
        reva_migration_database_url=base.migration_url().set(database=db_name).render_as_string(hide_password=False),
        reva_db_name=db_name,
        reva_env="test",
        reva_object_root=object_root,
    )
    return Settings.model_validate(data)


def _admin(settings: Any) -> Any:
    from sqlalchemy import create_engine

    return create_engine(settings.migration_url().set(database="postgres"), isolation_level="AUTOCOMMIT")


def archived_entitydata(eid: str) -> tuple[bytes, str, datetime] | None:
    """Latest archived ``Special:EntityData`` body of ``eid`` (raw JSON bytes, archive sha256, retrieval time)."""
    best: dict[str, Any] | None = None
    if not ARCHIVE_INDEX.exists():
        return None
    with ARCHIVE_INDEX.open() as f:
        for line in f:
            if f'"{eid}"' not in line:
                continue
            d = json.loads(line)
            if d.get("kind") == "entitydata" and d.get("ids") == [eid]:
                best = d if best is None or d["retrieved_at"] > best["retrieved_at"] else best
    if best is None:
        return None
    sha = best["sha256"]
    p = ARCHIVE_ROOT / "reva" / "raw" / "sha256" / sha[:2] / sha[2:4] / sha
    if not p.exists():
        return None
    return gzip.decompress(p.read_bytes()), sha, datetime.fromisoformat(best["retrieved_at"])


def _copy_raw(sha: str, store_root: Path) -> None:
    src = ARCHIVE_ROOT / "reva" / "raw" / "sha256" / sha[:2] / sha[2:4] / sha
    dst = store_root / "reva" / "raw" / "sha256" / sha[:2] / sha[2:4] / sha
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


async def snapshots(ids: Sequence[str], *, fetch: bool) -> dict[str, tuple[dict[str, Any], str, datetime]]:
    """Entity JSON per id from the archive, fetching the missing ones (budget-metered) when ``fetch``."""
    out: dict[str, tuple[dict[str, Any], str, datetime]] = {}
    missing: list[str] = []
    for eid in ids:
        got = archived_entitydata(eid)
        if got is None:
            missing.append(eid)
            continue
        body, sha, ret = got
        ent = (json.loads(body).get("entities") or {}).get(eid)
        if ent is not None:
            out[eid] = (ent, sha, ret)
    if missing and fetch:
        from reva.events.verify import wbapi_client

        async with wbapi_client(
            purpose="eventsdemo", planned_wbapi=len(missing), mode="linked_data", reuse_hours=72.0
        ) as sess:
            res = await sess.client.get_entities(missing, meta={"purpose": "events_demo"})
        for eid in missing:
            got = archived_entitydata(eid)
            if got is not None:
                body, sha, ret = got
                ent = (json.loads(body).get("entities") or {}).get(eid)
                if ent is not None:
                    out[eid] = (ent, sha, ret)
        _ = res
    return out


def load_wikidata(
    engine: Any,
    store: Any,
    store_root: Path,
    snaps: Mapping[str, tuple[dict[str, Any], str, datetime]],
    *,
    full: set[str],
) -> dict[str, Any]:
    """Load snapshots with WDHARVEST's extraction (allow-list, ranks, qualifiers, references, revisions). ``full``
    ids keep their claims; the others are label snapshots (``<lastrevid>:labels``)."""
    from reva.adapters.connectors.wikidata import extract_entity, load_allowlist
    from reva.kg.harvest import kg_properties, to_kg_entity, to_kg_statement
    from reva.kg.model import load_batch, register_properties

    allow = load_allowlist()
    register_properties(engine, kg_properties(allow))
    ents, sts, raws = [], [], set()
    oldest = None
    for eid, (raw, sha, ret) in snaps.items():
        _copy_raw(sha, store_root)
        raws.add(sha)
        oldest = ret if oldest is None or ret < oldest else oldest
        keep = raw if eid in full else {k: v for k, v in raw.items() if k != "claims"}
        we = extract_entity(keep, allow, raw_sha256=sha, retrieved_at=ret, kind_hint="event" if eid in full else None)
        ents.append(to_kg_entity(we))
        sts += [to_kg_statement(s) for s in we.statements]
    rep = load_batch(
        engine,
        store,
        entities=ents,
        statements=sts,
        source_key="wikidata.wbapi",
        raw_sha256s=sorted(raws),
        retrieved_at=oldest or datetime.now(UTC),
        origin="live",
        parameters={"purpose": "events demo"},
    )
    return rep.as_dict()


def referenced_ids(
    snaps: Mapping[str, tuple[dict[str, Any], str, datetime]], ids: Sequence[str], limit: int
) -> list[str]:
    """Entity ids the demo events cite through event-view properties (P31 classes excluded), first ``limit``."""
    from reva.events.taxonomy import load_taxonomy

    tax = load_taxonomy()
    pids = [p.pid for p in tax.properties if p.pid.startswith("P") and p.direction == "out"]
    pids += ["P828", "P1542", "P1478", "P1479", "P1536", "P1537"]
    out: list[str] = []
    for eid in ids:
        claims = (snaps.get(eid, ({}, "", None))[0] or {}).get("claims") or {}
        for pid in pids:
            for c in claims.get(pid) or []:
                v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                q = v.get("id") if isinstance(v, dict) else None
                if q and q not in out and q not in ids:
                    out.append(q)
    return out[:limit]


def render_text(card: Mapping[str, Any]) -> str:
    """A compact plain-text rendering of an event card (for documentation and the console)."""
    lines = [f"== {card['label']} ({card['id']})"]
    t = card["types"]
    lines.append(
        "types: "
        + ", ".join(f"{x['id']} ({x['label_fr']})" for x in t["primary"])
        + (f" | secondary: {', '.join(x['id'] for x in t['secondary'])}" if t["secondary"] else "")
        + f" | basis: {t['basis']}"
    )
    for m in t["matches"][:3]:
        chain = " > ".join([m.get("p31_class") or m.get("dataset_kind") or ""] + [h["subclass_of"] for h in m["chain"]])
        lines.append(f"  via {chain} [{m['rank']}, {m['chain'][0]['via'] if m['chain'] else 'direct'}]")
    iv = card["interval"]

    def tv(x: Any) -> str:
        return "-" if not x else f"{x['time']} (precision {x['precision']})"

    lines.append(
        f"time: start {tv(iv['start'])}, end {tv(iv['end'])}, point {tv(iv['point_in_time'])} -> "
        f"{iv['earliest']} .. {iv['latest']} [{iv['basis']}, {iv['consistency']}]"
    )
    for n in iv["notes"]:
        lines.append(f"  note: {n}")
    if card["places"]:
        lines.append(
            "places: "
            + "; ".join(
                f"{p['relation']}: {(p['place'] or {}).get('label') or (p['value'] or {}).get('value')}"
                for p in card["places"]
            )
        )
    for p in card["participants"][:12]:
        who = (p["participant"] or {}).get("label") or (p["value"] or {}).get("value")
        side = f", side {p['side']}" if p["side"] else ""
        span = f", {p['valid_from']}..{p['valid_to']}" if p["valid_from"] or p["valid_to"] else ""
        lines.append(
            f"participant: {who} — {p['role']['label_en']}{side}{span} [{p['property']} {p['direction']}, "
            f"{p['statements'][0]['source_key']}]"
        )
    if len(card["participants"]) > 12:
        lines.append(f"  ... {len(card['participants']) - 12} more participants")
    for s in card["structure"][:10]:
        lines.append(
            f"structure: {s['relation']} {s['event']['label']} [{s['provenance']['property']} stated on "
            f"{s['stated_on']}]"
        )
    for c in card["causal_statements"]:
        lines.append(
            f"causal (asserted by {c['asserted_by']}): {c['subject']['label']} —{c['property_label_en']}→ "
            f"{c['object']['label']}"
        )
    for s in card["same_as"]:
        lines.append(f"same_as: {s['event']['id']} ({s['evidence']['type']})")
    if card.get("derived"):
        lines.append(f"derived: {card['derived']['method']} from {', '.join(card['derived']['from_statements'])}")
    lines.append("sources: " + ", ".join(card["sources"]))
    return "\n".join(lines)


async def run(db_name: str, *, keep: bool, fetch_labels: int, out_dir: Path, store_root: Path) -> dict[str, Any]:
    from sqlalchemy import text

    from reva.adapters.connectors import atlas_cow, atlas_cshapes, atlas_peacekeeping, atlas_treaties, atlas_ucdp
    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.adapters.postgres.engine import make_engine
    from reva.atlas.ingest import run_atlas_ingest
    from reva.atlas.sources import ensure_registered
    from reva.cli.bootstrap import bootstrap
    from reva.events import same_as as sa
    from reva.events import view as ev

    settings = _settings(db_name, store_root)
    admin = _admin(settings)
    with admin.connect() as c:
        if c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}).scalar():
            c.execute(text(f'DROP DATABASE "{db_name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{db_name}"'))
    report: dict[str, Any] = {"database": db_name, "started_at": datetime.now(UTC).isoformat()}
    try:
        bootstrap(settings)
        owner, worker, api = (make_engine(r, settings) for r in ("migration", "worker", "api"))
        store = FilesystemArtifactStore(store_root, settings.reva_s3_prefix)
        ds: dict[str, Any] = {}
        for module in (atlas_ucdp, atlas_cow, atlas_cshapes, atlas_peacekeeping, atlas_treaties):
            spec = module.atlas_source()
            ensure_registered(owner, store, spec)
            out = await run_atlas_ingest(settings, worker, store, spec.name, mode="fixture")
            ds[spec.name] = {
                "status": out["status"],
                "kg_statements": (out.get("summary") or {}).get("kg", {}).get("statements_inserted"),
            }
        report["datasets"] = ds
        snaps = await snapshots(list(DEMO_EVENTS), fetch=True)
        refs = referenced_ids(snaps, list(DEMO_EVENTS), fetch_labels)
        label_snaps = await snapshots(refs, fetch=fetch_labels > 0)
        report["wikidata"] = load_wikidata(worker, store, store_root, {**label_snaps, **snaps}, full=set(snaps))
        report["wikidata"]["events"] = sorted(snaps)
        report["wikidata"]["label_snapshots"] = len(label_snaps)
        with worker.connect() as c:
            links = sa.derive_links(c)
        report["same_as"] = links.as_dict()
        if links.links:
            sa.load_links(worker, store, links, origin="live", rules_sha256=sa.rules_sha256())
        cards: dict[str, Any] = {}
        with api.connect() as c:
            for eid in [*DEMO_EVENTS, *DATASET_EVENTS]:
                cards[eid] = ev.event_card(c, eid)
            indep = ev.cshapes_independence_events(c)
            dz = [e["id"] for e in indep if e["territory"] == "cshapes:gw:615"]
            if dz:
                cards[dz[0]] = ev.event_card(c, dz[0])
            report["listing_1954_1962_DZA"] = ev.list_events(
                c, start="1954", end="1962", country="DZA", involving=True, limit=50
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        texts = []
        for eid, card in cards.items():
            if card is None:
                texts.append(f"== {eid}: not an event in this database")
                continue
            texts.append(render_text(card))
        (out_dir / "event_cards.json").write_text(json.dumps(cards, indent=1, ensure_ascii=False, default=str))
        (out_dir / "event_cards.txt").write_text("\n\n".join(texts) + "\n")
        report["cards"] = {k: (v is not None) for k, v in cards.items()}
        for e in (owner, worker, api):
            e.dispose()
    finally:
        if not keep:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            report["dropped"] = True
        admin.dispose()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reva.events.demo")
    ap.add_argument("--db", default="reva_events_demo")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--fetch-labels", type=int, default=40, help="label snapshots of cited entities (0 = none)")
    ap.add_argument("--out", type=Path, required=True, help="output directory (cards JSON + text)")
    ap.add_argument("--store", type=Path, required=True, help="object store directory of the throwaway database")
    a = ap.parse_args(argv)
    rep = asyncio.run(run(a.db, keep=a.keep, fetch_labels=a.fetch_labels, out_dir=a.out, store_root=a.store))
    print(json.dumps({k: v for k, v in rep.items() if k != "listing_1954_1962_DZA"}, indent=1, default=str))
    print(
        "\n".join(
            f"listing: {i['id']} {i['label']} {[t['id'] for t in i['types']]} {i['earliest']}..{i['latest']}"
            for i in rep.get("listing_1954_1962_DZA", {}).get("items", [])
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
