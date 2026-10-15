"""Idempotent registry seeding through the worker's controlled write path.

Published concept versions are immutable: a registry entry whose content differs
from the stored version for the same (concept_id, version) fails loudly.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.postgres import repositories as repo
from reva.evidence.locks import take_evidence_write_lock
from reva.kernel.errors import ImmutableRecordViolation
from reva.kernel.ids import entity_id as kernel_entity_id
from reva.ontology.registry import Registry, concept_hash, load_registry
from reva.ports.storage import ArtifactStore

SEED_ACTOR = "maintainer:registry-seed"


def entity_id(key: str) -> UUID:
    """Re-exported from the kernel (kept here for existing callers)."""
    return kernel_entity_id(key)


def _seed_concepts(conn: Connection, reg: Registry, act: UUID) -> int:
    n = 0
    for c in reg.concepts:
        h = concept_hash(c)
        row = conn.execute(
            text("SELECT registry_sha256 FROM ontology.concept_version WHERE concept_id=:c AND version=:v"),
            {"c": c.concept_id, "v": c.version},
        ).scalar_one_or_none()
        if row is not None:
            if row != h:
                raise ImmutableRecordViolation(
                    f"concept {c.concept_id}@{c.version} differs from its published definition; bump the version"
                )
            continue
        conn.execute(
            text(
                """INSERT INTO ontology.concept_version(concept_id, version, kind, labels, definition, owner,
                     semantic_profile, registry_sha256, provenance_activity_id)
                   VALUES (:c, :v, :k, CAST(:l AS jsonb), :d, :o, CAST(:sp AS jsonb), :h, :a)"""
            ),
            {
                "c": c.concept_id,
                "v": c.version,
                "k": c.kind.value,
                "l": json.dumps(c.labels),
                "d": c.definition,
                "o": c.owner,
                "sp": c.semantic_profile.model_dump_json() if c.semantic_profile else "{}",
                "h": h,
                "a": act,
            },
        )
        n += 1
    return n


def _ensure_entity(conn: Connection, eid: UUID, kind: str, label: str, mode: str, act: UUID) -> None:
    conn.execute(
        text(
            """INSERT INTO world.entity(id, kind_concept_id, namespace, referent_mode, label, created_activity_id)
               VALUES (:i, :k, 'reva', :m, :l, :a) ON CONFLICT (id) DO NOTHING"""
        ),
        {"i": eid, "k": repo.concept_version_id(conn, kind), "m": mode, "l": label, "a": act},
    )


# Partner-alias mapping 1.0.0 aliased every partner code with a seeded ISO3 to
# that country (250 and 251 -> FRA; 840/841/842 -> USA). 1.1.0 (R-10) keeps only
# reviewed partner aliases on country entities; resolve_alias prefers the newest
# alias row, so re-seeding an existing database supersedes the 1.0.0 rows.
PARTNER_MAPPING_VERSION = "1.1.0"


def _ensure_alias(
    conn: Connection,
    eid: UUID,
    provider: str,
    key: str,
    confidence: str,
    act: UUID,
    *,
    mapping_version: str = "1.0.0",
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> None:
    conn.execute(
        text(
            """INSERT INTO world.alias(entity_id, provider, external_key, valid_range, mapping_version,
                 confidence_kind, provenance_activity_id)
               VALUES (:e, :p, :k,
                       CASE WHEN CAST(:vf AS date) IS NULL AND CAST(:vt AS date) IS NULL THEN NULL
                            ELSE daterange(CAST(:vf AS date), CAST(:vt AS date), '[)') END,
                       :mv, :c, :a)
               ON CONFLICT (provider, external_key, mapping_version) DO NOTHING"""
        ),
        {"e": eid, "p": provider, "k": key, "c": confidence, "a": act, "mv": mapping_version,
         "vf": valid_from, "vt": valid_to},
    )


def _catalogue_date(value: Any, *, exclusive_end: bool = False) -> str | None:
    if not value:
        return None
    from datetime import date, timedelta

    d = date.fromisoformat(str(value)[:10])
    return (d + timedelta(days=1)).isoformat() if exclusive_end else d.isoformat()


def _seed_entities(conn: Connection, reg: Registry, act: UUID) -> int:
    n = 0
    for e in reg.entities:
        eid = entity_id(e["key"])
        _ensure_entity(conn, eid, e["kind"], e["label"], e.get("referent_mode", "real"), act)
        for a in e["aliases"]:
            _ensure_alias(conn, eid, a["provider"], str(a["key"]), "reviewed_exact", act,
                          mapping_version=str(a.get("mapping_version", "1.0.0")))
        n += 1
    # Provider-declared partner areas for bilateral trade relations (R-10): only a
    # reviewed comtrade_partner alias (countries.yaml) maps a partner code to a
    # reviewed entity. Every other code gets its own provider-declared entity, even
    # when it shares an ISO alpha-3 with a reviewed country (250 "Metropolitan
    # France" vs 251 "France, Monaco"; 840/841/842 USA variants).
    partners = json.loads((reg.root / "reference" / "comtrade_partnerAreas.json").read_text())["results"]
    reviewed = {str(a["key"]) for e in reg.entities for a in e["aliases"] if a["provider"] == "comtrade_partner"}
    for p in partners:
        code = str(p["PartnerCode"])
        if code in reviewed:
            continue  # reviewed alias (incl. 0 = World) seeded from countries.yaml
        eid = entity_id(f"comtrade_partner:{code}")
        text_l = (p.get("text") or "").lower()
        is_area = bool(p.get("isGroup")) or "nes" in text_l.split() or text_l.endswith(", nes")
        _ensure_entity(
            conn,
            eid,
            "entity.world_aggregate" if is_area else "entity.country",
            p.get("text") or code,
            "abstract" if is_area else "real",
            act,
        )
        _ensure_alias(conn, eid, "comtrade_partner", code, "provider_declared", act,
                      mapping_version=PARTNER_MAPPING_VERSION,
                      valid_from=_catalogue_date(p.get("entryEffectiveDate")),
                      valid_to=_catalogue_date(p.get("entryExpiredDate"), exclusive_end=True))
    return n


def _seed_sources(conn: Connection, store: ArtifactStore, reg: Registry, act: UUID, contact: str | None) -> None:
    for s in reg.sources:
        row = repo.source_by_key(conn, s["source_key"])
        if row is None:
            sid = conn.execute(
                text(
                    """INSERT INTO evidence.source(source_key, title, provider_url, lineage_family, rights, metadata,
                         provenance_activity_id)
                       VALUES (:k, :t, :u, :lf, CAST(:r AS jsonb), CAST(:m AS jsonb), :a) RETURNING id"""
                ),
                {
                    "k": s["source_key"],
                    "t": s["title"],
                    "u": s["provider_url"],
                    "lf": s["lineage_family"],
                    "r": json.dumps(s["rights"]),
                    "m": json.dumps(s["metadata"]),
                    "a": act,
                },
            ).scalar_one()
            conn.execute(
                text(
                    """INSERT INTO evidence.source_metadata_version(source_id, version, metadata, rights,
                         provenance_activity_id) VALUES (:s, 1, CAST(:m AS jsonb), CAST(:r AS jsonb), :a)"""
                ),
                {"s": sid, "m": json.dumps(s["metadata"]), "r": json.dumps(s["rights"]), "a": act},
            )
        else:
            repo.append_source_metadata_if_changed(conn, row["id"], s["metadata"], s["rights"], act)
    for spec_conn in builtin_connectors(contact).values():
        register_connector_version(conn, store, spec_conn.describe(), act)


def register_connector_version(conn: Connection, store: ArtifactStore, spec: Any, act: UUID) -> UUID:
    existing = repo.connector_version_row(conn, spec.name, spec.version)
    if existing:
        return UUID(str(existing["id"]))
    src = repo.source_by_key(conn, spec.source_key)
    if src is None:
        raise KeyError(f"source {spec.source_key} not registered")
    payload = spec.model_dump(mode="json")
    sha = repo.store_json_artifact(conn, store, payload, "models", schema_version="connector-spec/1")
    return UUID(
        str(
            conn.execute(
                text(
                    """INSERT INTO evidence.connector_version(source_id, name, version, spec_sha256, code_digest,
                         declared_capabilities)
                       VALUES (:s, :n, :v, :sha, :cd, CAST(:caps AS jsonb)) RETURNING id"""
                ),
                {
                    "s": src["id"],
                    "n": spec.name,
                    "v": spec.version,
                    "sha": sha,
                    "cd": spec.code_digest,
                    "caps": json.dumps({"template": spec.template, "datasets": list(spec.dataset_ids),
                                        "allowed_hosts": list(spec.allowed_hosts)}),
                },
            ).scalar_one()
        )
    )


def record_mapping_and_eligibility(
    conn: Connection, m: dict[str, Any], act: UUID, *, policy_version: str = "seed-reviewed-1"
) -> None:
    exists = conn.execute(
        text("SELECT 1 FROM ontology.mapping WHERE mapping_key=:k AND version=:v"),
        {"k": m["mapping_key"], "v": m["version"]},
    ).scalar_one_or_none()
    target = repo.concept_version_id(conn, m["target"]["concept_id"], m["target"]["version"])
    if not exists:
        conn.execute(
            text(
                """INSERT INTO ontology.mapping(mapping_key, source_ref, target_concept_id, mapping_kind, version,
                     transform_spec, applicability, approval, review_activity_id)
                   VALUES (:k, CAST(:sr AS jsonb), :t, :mk, :v, CAST(:ts AS jsonb), CAST(:ap AS jsonb), :appr, :a)"""
            ),
            {
                "k": m["mapping_key"],
                "sr": json.dumps(m["source_ref"]),
                "t": target,
                "mk": m["mapping_kind"],
                "v": m["version"],
                "ts": json.dumps({"kind": "identity"} if m["mapping_kind"] == "exact" else {"kind": "none"}),
                "ap": json.dumps(m.get("applicability", {})),
                "appr": "approved" if m["mapping_kind"] in ("exact", "incompatible") else "candidate",
                "a": act,
            },
        )
    if m.get("eligibility") != "eligible" or m["mapping_kind"] != "exact":
        return
    cv = repo.connector_version_row(conn, m["connector"]["name"], m["connector"]["version"])
    if cv is None:
        return
    subjects = m.get("applicability", {}).get("subjects", [])
    subject_ids = [str(entity_id(s)) for s in subjects]
    applicability = {**m.get("applicability", {}), "subject_ids": subject_ids, "mapping_key": m["mapping_key"]}
    # Append a decision only when the latest seeded one differs (e.g. a mapping version
    # widened its applicability); decisions are append-only and the latest in scope wins.
    latest = conn.execute(
        text(
            "SELECT decision, applicability FROM evidence.eligibility WHERE connector_version_id=:cv "
            "AND concept_version_id=:c ORDER BY recorded_at DESC, id DESC LIMIT 1"
        ),
        {"cv": cv["id"], "c": target},
    ).mappings().one_or_none()
    if latest is not None and latest["decision"] == "eligible" and dict(latest["applicability"]) == applicability:
        return
    if latest is not None and latest["decision"] != "eligible":
        return  # a later review retired/quarantined it: seeding never overrides a review decision
    conn.execute(
        text(
            """INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
                 policy_version, review_activity_id)
               VALUES (:cv, :c, CAST(:ap AS jsonb), 'eligible', :p, :a)"""
        ),
        {
            "cv": cv["id"],
            "c": target,
            "ap": json.dumps(applicability),
            "p": policy_version,
            "a": act,
        },
    )


def _seed_models(conn: Connection, store: ArtifactStore, reg: Registry, act: UUID) -> int:
    n = 0
    for m in reg.models:
        if conn.execute(
            text("SELECT 1 FROM model.version WHERE model_key=:k AND version=:v"),
            {"k": m["model_key"], "v": m["version"]},
        ).scalar_one_or_none():
            continue
        from reva.operations.environment import code_digest

        sha = repo.store_json_artifact(conn, store, m, "models", schema_version="model-spec/1")
        conn.execute(
            text(
                """INSERT INTO model.version(model_key, version, kind, spec_sha256, code_digest, input_ports,
                     output_ports, applicability, assumptions, approval_status, provenance_activity_id)
                   VALUES (:k, :v, :kind, :sha, :cd, CAST(:ip AS jsonb), CAST(:op AS jsonb), CAST(:ap AS jsonb),
                           CAST(:as AS jsonb), :st, :a)"""
            ),
            {
                "k": m["model_key"],
                "v": m["version"],
                "kind": m["kind"],
                "sha": sha,
                "cd": code_digest(),
                "ip": json.dumps(m["input_ports"]),
                "op": json.dumps(m["output_ports"]),
                "ap": json.dumps(m["applicability"]),
                "as": json.dumps(m["assumptions"]),
                "st": m["approval_status"],
                "a": act,
            },
        )
        n += 1
    return n


DEFAULT_SCHEDULES: tuple[tuple[str, str, dict[str, Any], int], ...] = (
    ("ingest:worldbank.wdi.indicators", "ingest", {"connector": "worldbank.wdi.indicators"}, 30 * 86400),
    ("ingest:un.comtrade.public_exact", "ingest", {"connector": "un.comtrade.public_exact"}, 30 * 86400),
    ("ingest:naturalearth.admin0_110m", "ingest", {"connector": "naturalearth.admin0_110m"}, 7 * 86400),
    ("ingest:crossref.works.doi", "ingest", {"connector": "crossref.works.doi"}, 7 * 86400),
    # Promoted by the autonomy gate (agent G); the ingest handler is a no-op until promotion.
    ("ingest:eurostat.demo_pjan.fr", "ingest", {"connector": "eurostat.demo_pjan.fr"}, 7 * 86400),
    ("health:connectors", "health", {}, 86400),
    ("gaps:mvp", "gaps", {}, 86400),
    ("gc:orphans", "gc", {}, 86400),
)


def _seed_schedules(conn: Connection) -> None:
    for key, jt, params, secs in DEFAULT_SCHEDULES:
        conn.execute(
            text(
                """INSERT INTO ops.schedule(schedule_key, job_type, parameters, cadence_spec, next_due_at, enabled)
                   VALUES (:k, :t, CAST(:p AS jsonb), CAST(:c AS jsonb), clock_timestamp() + interval '1 hour', true)
                   ON CONFLICT (schedule_key) DO NOTHING"""
            ),
            {"k": key, "t": jt, "p": json.dumps(params), "c": json.dumps({"interval_seconds": secs})},
        )


def seed(engine: Engine, store: ArtifactStore, *, contact_email: str | None = None) -> dict[str, Any]:
    reg = load_registry()
    with engine.begin() as conn:
        take_evidence_write_lock(conn)  # writes eligibility decisions: evidence lock protocol (R-03)
        act = repo.simple_activity("registry.seed", "review", SEED_ACTOR, {"registry": "mvp"})
        repo.record_activity(conn, act)
        concepts = _seed_concepts(conn, reg, act.id)
        entities = _seed_entities(conn, reg, act.id)
        _seed_sources(conn, store, reg, act.id, contact_email)
        for m in reg.mappings:
            record_mapping_and_eligibility(conn, m, act.id)
        models = _seed_models(conn, store, reg, act.id)
        _seed_schedules(conn)
    return {"new_concepts": concepts, "entities": entities, "new_models": models}
