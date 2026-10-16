"""Idempotent registry seeding: a deploy-time step run with the migration owner.

The ``registry/`` YAML is reviewed in git, so seeding it writes *approved* rows
(exact mappings, ``eligible`` decisions, approved model versions) that runtime
roles may not write (R5-01: the database refuses them from ``reva_worker``).
``bootstrap()`` seeds as the migration owner. Called with a runtime-role engine,
``seed`` only confirms that the owner already seeded exactly this registry
(fingerprint recorded on the seed activity) and otherwise refuses.

Published concept versions are immutable: a registry entry whose content differs
from the stored version for the same (concept_id, version) fails loudly; the same
holds for a connector (name, version) whose spec or code changed.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.adapters.connectors.registry import builtin_connectors
from reva.adapters.postgres import repositories as repo
from reva.evidence.locks import take_evidence_write_lock
from reva.kernel.errors import ConfigurationError, ImmutableRecordViolation
from reva.kernel.ids import canonical_bytes, logical_hash, sha256_bytes
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
        {
            "e": eid,
            "p": provider,
            "k": key,
            "c": confidence,
            "a": act,
            "mv": mapping_version,
            "vf": valid_from,
            "vt": valid_to,
        },
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
            _ensure_alias(
                conn,
                eid,
                a["provider"],
                str(a["key"]),
                "reviewed_exact",
                act,
                mapping_version=str(a.get("mapping_version", "1.0.0")),
            )
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
        _ensure_alias(
            conn,
            eid,
            "comtrade_partner",
            code,
            "provider_declared",
            act,
            mapping_version=PARTNER_MAPPING_VERSION,
            valid_from=_catalogue_date(p.get("entryEffectiveDate")),
            valid_to=_catalogue_date(p.get("entryExpiredDate"), exclusive_end=True),
        )
    n += _seed_crosswalk_entities(conn, {e["key"] for e in reg.entities}, act)
    return n


# --- V2 step 1 (agent ALLC): data-driven country entities from the provider crosswalk ---
# registry/entities/country_crosswalk.json is built mechanically from the providers' own
# reference lists (UNSD M49, Comtrade Reporters/partnerAreas, World Bank countries; raw
# bytes archived in registry/reference/, digests verified on load). Its aliases are seeded
# as 'provider_declared' (not human-reviewed); entities in countries.yaml keep their
# reviewed aliases and get nothing from the crosswalk. Composite/"nes"/historical provider
# codes are listed unmapped in the crosswalk and never seeded as country aliases.
def _seed_crosswalk_entities(conn: Connection, reviewed_keys: set[str], act: UUID) -> int:
    from reva.adapters.connectors.country_crosswalk import ALIAS_MAPPING_VERSION, seed_entities

    n = 0
    for e in seed_entities():
        if e["key"] in reviewed_keys:
            continue
        eid = entity_id(e["key"])
        _ensure_entity(conn, eid, e["kind"], e["label"], "real", act)
        for a in e["aliases"]:
            _ensure_alias(
                conn, eid, a["provider"], str(a["key"]), "provider_declared", act, mapping_version=ALIAS_MAPPING_VERSION
            )
        n += 1
    return n


# --- end V2 step 1 (ALLC) ---


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
    # V3 atlas sources (AtlasSourceSpec, discovered from reva.adapters.connectors.atlas_*)
    from reva.atlas.sources import register_atlas_connector_versions

    register_atlas_connector_versions(conn, store, act)


def register_connector_version(conn: Connection, store: ArtifactStore, spec: Any, act: UUID) -> UUID:
    payload = spec.model_dump(mode="json")
    existing = repo.connector_version_row(conn, spec.name, spec.version)
    if existing:
        # A (name, version) is immutable: changed spec or code needs a new version.
        if existing["spec_sha256"] != sha256_bytes(canonical_bytes(payload)) or (
            existing["code_digest"] != spec.code_digest
        ):
            raise ImmutableRecordViolation(
                f"connector {spec.name}@{spec.version} is registered with different content; bump the version",
                connector=spec.name,
                version=spec.version,
            )
        return UUID(str(existing["id"]))
    src = repo.source_by_key(conn, spec.source_key)
    if src is None:
        raise KeyError(f"source {spec.source_key} not registered")
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
                    "caps": json.dumps(
                        {
                            "template": spec.template,
                            "datasets": list(spec.dataset_ids),
                            "allowed_hosts": list(spec.allowed_hosts),
                            # GATE/R6-02: evidence.approve_eligibility binds a gate pass to the
                            # manifest digest the gate evaluated (declarative connectors only)
                            "manifest_sha256": (spec.field_mappings or {}).get("manifest_sha256"),
                        }
                    ),
                },
            ).scalar_one()
        )
    )


def record_mapping_and_eligibility(
    conn: Connection,
    m: dict[str, Any],
    act: UUID,
    *,
    policy_version: str = "seed-reviewed-1",
    approval: tuple[UUID, UUID] | None = None,
) -> None:
    """Record a mapping (and, for seeded exact mappings, its eligibility decision).

    ``approval`` = ``(proposal_id, approval_id)`` (a passing gate result or an approving
    human review of that proposal): an approved non-``incompatible`` mapping is then
    written through ``ontology.approve_mapping``, the only path by which a runtime role
    may record one (R5-01). Without it, an approved mapping can only be seeded by the
    migration owner.
    """
    exists = conn.execute(
        text("SELECT 1 FROM ontology.mapping WHERE mapping_key=:k AND version=:v"),
        {"k": m["mapping_key"], "v": m["version"]},
    ).scalar_one_or_none()
    target = repo.concept_version_id(conn, m["target"]["concept_id"], m["target"]["version"])
    approval_state = "approved" if m["mapping_kind"] in ("exact", "incompatible") else "candidate"
    transform = {"kind": "identity"} if m["mapping_kind"] == "exact" else {"kind": "none"}
    if not exists and approval is not None and approval_state == "approved" and m["mapping_kind"] != "incompatible":
        conn.execute(
            text(
                """SELECT ontology.approve_mapping(:p, :g, :k, :v, CAST(:sr AS jsonb), :t, :mk, CAST(:ts AS jsonb),
                     CAST(:ap AS jsonb), :pv, :a)"""
            ),
            {
                "p": approval[0],
                "g": approval[1],
                "k": m["mapping_key"],
                "v": m["version"],
                "sr": json.dumps(m["source_ref"]),
                "t": target,
                "mk": m["mapping_kind"],
                "ts": json.dumps(transform),
                "ap": json.dumps(m.get("applicability", {})),
                "pv": policy_version,
                "a": act,
            },
        )
    elif not exists:
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
                "ts": json.dumps(transform),
                "ap": json.dumps(m.get("applicability", {})),
                "appr": approval_state,
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
    latest = (
        conn.execute(
            text(
                "SELECT decision, applicability FROM evidence.eligibility WHERE connector_version_id=:cv "
                "AND concept_version_id=:c ORDER BY recorded_at DESC, id DESC LIMIT 1"
            ),
            {"cv": cv["id"], "c": target},
        )
        .mappings()
        .one_or_none()
    )
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


#: ATLASOPS: finished atlas sources (agent reports in docs/dev/v3_a_*.md) and their first-slot offset
#: in hours from seeding; every source runs monthly after that.
ATLAS_SCHEDULE_SLOTS: tuple[tuple[str, int], ...] = (
    ("atlas.imf", 2),  # day 0: ~170 MB
    ("atlas.undp_hdr", 26),  # day 1: small sources
    ("atlas.whr", 27),
    ("atlas.ilostat", 28),
    ("atlas.eurostat.earn_mw_cur", 29),
    ("atlas.oecd.earnings", 30),
    ("atlas.owid", 50),  # day 2
    ("atlas.un_wpp", 51),
    ("atlas.un_ims", 52),
    ("atlas.unsd_syb", 53),
    ("atlas.wid", 74),  # day 3: ~660 MB
    ("atlas.who_gho", 98),  # day 4
    ("atlas.unesco_uis", 99),
    ("atlas.un_sdg", 122),  # day 5
    ("atlas.unhcr", 123),
    ("atlas.worldbank", 146),  # day 6: ~790 MB
    ("atlas.maddison.mpd2023", 170),  # day 7: history
    ("atlas.clio_infra", 171),
    ("atlas.gapminder.fasttrack", 172),
    ("atlas.hyde.hyde321", 173),
    ("atlas.cow", 174),
    ("atlas.vdem", 194),  # day 8: politics
    ("atlas.lied", 195),
    ("atlas.bmr", 196),
    ("atlas.ipu", 197),
    ("atlas.wbl", 218),  # day 9: law
    ("atlas.constitute", 219),
    ("atlas.ccp", 220),
    ("atlas.ssa_ssptw", 221),
    ("atlas.naturalearth_10m", 242),  # day 10: geography
    ("atlas.geonames_countryinfo", 243),
    ("atlas.cckp_climatology", 244),
    ("atlas.inform_risk_hazards", 245),
    ("atlas.fao", 266),  # day 11
    ("atlas.gap_spc", 290),  # day 12: GAPHUNT gap fills (territories)
    ("atlas.gap_eurostat_nuts", 291),
    ("atlas.gap_insee", 292),
    ("atlas.gap_cbs_bes", 293),
    ("atlas.gap_jersey", 294),
)

DEFAULT_SCHEDULES: tuple[tuple[str, str, dict[str, Any], int], ...] = (
    ("ingest:worldbank.wdi.indicators", "ingest", {"connector": "worldbank.wdi.indicators"}, 30 * 86400),
    ("ingest:un.comtrade.public_exact", "ingest", {"connector": "un.comtrade.public_exact"}, 30 * 86400),
    ("ingest:naturalearth.admin0_110m", "ingest", {"connector": "naturalearth.admin0_110m"}, 7 * 86400),
    ("ingest:crossref.works.doi", "ingest", {"connector": "crossref.works.doi"}, 7 * 86400),
    # Promoted by the autonomy gate (agent G); the ingest handler is a no-op until promotion.
    ("ingest:eurostat.demo_pjan.fr", "ingest", {"connector": "eurostat.demo_pjan.fr"}, 7 * 86400),
    # V2 step 1 (ALLC): every crosswalk country. Comtrade rotates over reporter batches,
    # one bounded slice per daily run (cursor offset), within the per-source daily budget.
    ("ingest:worldbank.wdi.all_economies", "ingest", {"connector": "worldbank.wdi.all_economies"}, 30 * 86400),
    ("ingest:un.comtrade.public_batch", "ingest", {"connector": "un.comtrade.public_batch"}, 86400),
    # V2 steps 3-5: Eurostat demography, labour/pensions and FIGARO IO tables (monthly)
    # demography: a 282-request cycle completes in ~3 daily runs within the per-source budget
    ("ingest:eurostat.demography", "ingest", {"connector": "eurostat.demography"}, 86400),
    ("ingest:eurostat.labour_pensions", "ingest", {"connector": "eurostat.labour_pensions"}, 30 * 86400),
    ("ingest:eurostat.figaro.iciot_ii", "ingest", {"connector": "eurostat.figaro.iciot_ii"}, 30 * 86400),
    ("health:connectors", "health", {}, 86400),
    ("gaps:mvp", "gaps", {}, 86400),
    ("gc:orphans", "gc", {}, 86400),
    # --- GATE2: late gate decisions (discovery reported gate_pending) are promoted from the stored proposal
    ("gate_followup:promotion", "gate_followup", {}, 300),
    # --- end GATE2
    # --- ATLASOPS (V3): monthly runs of every finished atlas source through the single metered entry
    # point (reva.atlas.ingest: reserve the spec's planned requests on its budget key, meter, settle),
    # first slots staggered over the month (ATLAS_FIRST_SLOT_HOURS) so large downloads never share a
    # day's byte budget; then a daily snapshot, reused (nothing recorded) when no atlas table changed.
    *((f"atlas_ingest:{name}", "atlas_ingest", {"source": name}, 30 * 86400) for name, _hours in ATLAS_SCHEDULE_SLOTS),
    ("atlas_snapshot:live", "atlas_snapshot", {"origin": "live", "warm": True, "skip_unchanged": True}, 86400),
    # --- end ATLASOPS
    # --- LINKWIRE (V3): daily link store materialisation two hours after the atlas snapshot slot (live atlas
    # snapshot + live KG, the only materialisations production serves); reused when the inputs are unchanged.
    (
        "links_materialise:live",
        "links_materialise",
        {"atlas_origin": "live", "kg_origin": "live", "top_k": 10, "skip_unchanged": True},
        86400,
    ),
    # --- end LINKWIRE
)

#: ATLASOPS: hours from seeding to a schedule's first slot (default 1 h). Heavy downloads get a day of
#: their own (World Bank ~790 MB, WID ~660 MB, IMF ~170 MB); small sources share days.
ATLAS_FIRST_SLOT_HOURS: dict[str, int] = {
    **{f"atlas_ingest:{name}": hours for name, hours in ATLAS_SCHEDULE_SLOTS},
    "atlas_snapshot:live": 8,
    "links_materialise:live": 10,  # LINKWIRE: after the daily atlas snapshot slot
}


def _seed_schedules(conn: Connection) -> None:
    for key, jt, params, secs in DEFAULT_SCHEDULES:
        conn.execute(
            text(
                """INSERT INTO ops.schedule(schedule_key, job_type, parameters, cadence_spec, next_due_at, enabled)
                   VALUES (:k, :t, CAST(:p AS jsonb), CAST(:c AS jsonb),
                           clock_timestamp() + make_interval(hours => :h), true)
                   ON CONFLICT (schedule_key) DO NOTHING"""
            ),
            {
                "k": key,
                "t": jt,
                "p": json.dumps(params),
                "c": json.dumps({"interval_seconds": secs}),
                "h": ATLAS_FIRST_SLOT_HOURS.get(key, 1),
            },
        )


RUNTIME_PRINCIPALS = ("reva_api", "reva_worker", "reva_sim")


def _crosswalk_sha256() -> str:  # V2 step 1 (ALLC)
    from reva.adapters.connectors.country_crosswalk import crosswalk_sha256

    return crosswalk_sha256()


#: Per-process registry snapshot (FIX2 seed flake): ``seed`` loads the registry files once per process and
#: memoises their fingerprint, so the owner's seed (``bootstrap``) and a later runtime-role confirmation in
#: the same process compare the same snapshot even when other edits land in ``registry/`` (or the crosswalk)
#: in between. Seeding is a one-shot deploy step (``reva db bootstrap`` / ``reva seed``): a new process sees
#: the new files. :func:`reset_registry_snapshot` drops the snapshot (tests, long-lived callers).
_SNAPSHOT_LOCK = threading.Lock()
_SNAPSHOT: dict[str, Any] = {}


def registry_snapshot() -> Registry:
    """The reviewed registry as loaded by the first ``seed`` of this process."""
    with _SNAPSHOT_LOCK:
        if "registry" not in _SNAPSHOT:
            reg = load_registry()
            _SNAPSHOT["files"] = _file_digests(reg)  # read with the registry, not at each fingerprint
            _SNAPSHOT["registry"] = reg
            _SNAPSHOT["fingerprints"] = {}
        return _SNAPSHOT["registry"]  # type: ignore[no-any-return]


def reset_registry_snapshot() -> None:
    with _SNAPSHOT_LOCK:
        _SNAPSHOT.clear()


def seed_fingerprint(reg: Registry, contact_email: str | None = None) -> str:
    """Digest of everything ``seed`` writes (registry content, built-in connectors, schedules); memoised for
    the process snapshot (:func:`registry_snapshot`), so it is computed once from one state of the files."""
    with _SNAPSHOT_LOCK:
        memo = _SNAPSHOT.get("fingerprints") if _SNAPSHOT.get("registry") is reg else None
        if memo is not None and contact_email in memo:
            return memo[contact_email]  # type: ignore[no-any-return]
    fp = _compute_fingerprint(reg, contact_email)
    if memo is not None:
        with _SNAPSHOT_LOCK:
            memo.setdefault(contact_email, fp)
            return memo[contact_email]  # type: ignore[no-any-return]
    return fp


def _file_digests(reg: Registry) -> dict[str, str]:
    partners = reg.root / "reference" / "comtrade_partnerAreas.json"
    return {
        "partners_sha256": sha256_bytes(partners.read_bytes()),
        "crosswalk_sha256": _crosswalk_sha256(),  # V2 step 1 (ALLC)
    }


def _compute_fingerprint(reg: Registry, contact_email: str | None) -> str:
    with _SNAPSHOT_LOCK:
        files = _SNAPSHOT.get("files") if _SNAPSHOT.get("registry") is reg else None
    files = files or _file_digests(reg)
    return logical_hash(
        {
            "schema": "reva-seed-fingerprint/1",
            "ontology": reg.manifest(),
            "entities": list(reg.entities),
            "sources": list(reg.sources),
            "mappings": list(reg.mappings),
            "models": list(reg.models),
            "partners_sha256": files["partners_sha256"],
            "crosswalk_sha256": files["crosswalk_sha256"],
            # identity only: a changed spec/code under the same version is refused by
            # register_connector_version when the owner seeds, not hidden in the fingerprint
            "connectors": sorted(
                [s.name, s.version] for s in (c.describe() for c in builtin_connectors(contact_email).values())
            ),
            "schedules": [list(x) for x in DEFAULT_SCHEDULES],
            # ATLASOPS: atlas connector versions are registered by the seed too; a new atlas source (or
            # version) changes the fingerprint so the owner's next seed registers it
            "atlas_sources": _atlas_source_ids(),
        }
    )


def _atlas_source_ids() -> list[list[str]]:
    from reva.atlas.sources import atlas_sources

    return sorted([e.spec.name, e.spec.version] for e in atlas_sources().values())


def _latest_seed(conn: Connection) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT parameters->>'fingerprint' AS fingerprint, recorded_at FROM meta.activity "
                "WHERE kind = 'registry.seed' AND actor = :a ORDER BY recorded_at DESC LIMIT 1"
            ),
            {"a": SEED_ACTOR},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _already_seeded(conn: Connection, fingerprint: str) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM meta.activity WHERE kind = 'registry.seed' AND parameters->>'fingerprint' = :f "
                "AND actor = :a LIMIT 1"
            ),
            {"f": fingerprint, "a": SEED_ACTOR},
        ).scalar_one_or_none()
        is not None
    )


def seed(engine: Engine, store: ArtifactStore, *, contact_email: str | None = None) -> dict[str, Any]:
    """Seed the reviewed registry. Must run as the migration owner (``bootstrap`` does).

    With a runtime-role engine this is a read-only check: it returns ``status:
    already_seeded`` when the owner seeded exactly this registry, and otherwise raises
    ``ConfigurationError`` (runtime roles cannot write approved registry state).
    """
    reg = registry_snapshot()
    fingerprint = seed_fingerprint(reg, contact_email)
    with engine.connect() as conn:
        login = conn.execute(text("SELECT session_user")).scalar_one()
        if login in RUNTIME_PRINCIPALS:
            if _already_seeded(conn, fingerprint):
                return {
                    "new_concepts": 0,
                    "entities": len(reg.entities),
                    "new_models": 0,
                    "status": "already_seeded",
                    "fingerprint": fingerprint,
                }
            latest = _latest_seed(conn)
            raise ConfigurationError(
                f"registry seeding writes approved registry state and must run as the migration owner "
                f"(`reva db bootstrap` / `reva seed`), not as {login}: the owner has not seeded this registry "
                f"(fingerprint {fingerprint[:16]}…; latest owner seed: "
                + (
                    f"{str(latest['fingerprint'])[:16]}… at {latest['recorded_at']}"
                    if latest
                    else "none in this database"
                )
                + "). Registry files, the country crosswalk or the connector/atlas source versions changed since "
                "that seed: run `reva db bootstrap` (or `reva seed`) as the owner.",
                fingerprint=fingerprint,
                latest_owner_fingerprint=latest["fingerprint"] if latest else None,
            )
    with engine.begin() as conn:
        take_evidence_write_lock(conn)  # writes eligibility decisions: evidence lock protocol (R-03)
        if _already_seeded(conn, fingerprint):
            return {
                "new_concepts": 0,
                "entities": len(reg.entities),
                "new_models": 0,
                "status": "already_seeded",
                "fingerprint": fingerprint,
            }
        act = repo.simple_activity(
            "registry.seed", "review", SEED_ACTOR, {"registry": "mvp", "fingerprint": fingerprint}
        )
        repo.record_activity(conn, act)
        concepts = _seed_concepts(conn, reg, act.id)
        entities = _seed_entities(conn, reg, act.id)
        _seed_sources(conn, store, reg, act.id, contact_email)
        for m in reg.mappings:
            record_mapping_and_eligibility(conn, m, act.id)
        models = _seed_models(conn, store, reg, act.id)
        _seed_schedules(conn)
    return {
        "new_concepts": concepts,
        "entities": entities,
        "new_models": models,
        "status": "seeded",
        "fingerprint": fingerprint,
    }
