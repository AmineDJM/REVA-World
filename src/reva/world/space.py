"""Display geography (Natural Earth). Display geometry is not authoritative:
it does not resolve disputed sovereignty, and a provider's statistical reporter
territory may differ from the display polygon (e.g. overseas territories)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from reva.adapters.connectors.naturalearth import DISPUTED_TYPES, NaturalEarthConnector
from reva.adapters.postgres import repositories as repo
from reva.evidence.ingest import PublishSummary
from reva.ports.connectors import ObservationBatch, RawAcquisition
from reva.ports.storage import ArtifactStore

DISPLAY_NOTES = {
    "FRA": "Display polygon includes overseas parts present in Natural Earth; Comtrade reporter 251 covers "
    "France and Monaco; WDI FRA follows World Bank coverage notes. Supports differ.",
    "USA": "Comtrade reporter 842 includes Puerto Rico and US Virgin Islands; display polygon differs.",
}


def display_match_status(props: dict[str, Any], *, matched: bool) -> str:
    """Display classification only (never a sovereignty statement): 'disputed' iff the
    Natural Earth TYPE is Disputed or Indeterminate; otherwise reviewed_match/unmatched.
    NOTE_BRK alone does not mark a feature disputed (recorded as a display note)."""
    if str(props.get("TYPE")) in DISPUTED_TYPES:
        return "disputed"
    return "reviewed_match" if matched else "unmatched"


def publish_display_geometries(
    conn: Connection,
    store: ArtifactStore,
    raw: RawAcquisition,
    batches: list[ObservationBatch],
    summary: PublishSummary,
) -> dict[str, int]:
    """Post-publish step for Natural Earth: runs inside the acquisition publication
    transaction (``conn``), so geometries commit atomically with the acquisition."""
    gj = NaturalEarthConnector.to_geojson(raw.body)
    counts = {"reviewed_match": 0, "unmatched": 0, "disputed": 0}
    act = repo.simple_activity(
        "geometry.normalisation",
        "normalisation",
        "worker:geometry",
        {"dataset_sha256": summary.raw_sha256, "ne_version": gj.get("ne_version")},
    )
    norm_sha = repo.store_json_artifact(
        conn,
        store,
        gj,
        "normalised",
        schema_version="geojson/naturalearth-1",
        rights={"licence": "Public domain", "attribution": "Made with Natural Earth"},
    )
    repo.record_activity(
        conn, act, inputs=[(summary.raw_sha256, {"archive": "ne_110m_admin_0_countries"})], outputs=[norm_sha]
    )
    for i, f in enumerate(gj["features"]):
        props = dict(f["properties"])
        key = str(props.get("ADM0_A3") or f"feature-{i}")
        entity = repo.resolve_alias(conn, "naturalearth_adm0_a3", key)
        status = display_match_status(props, matched=entity is not None)
        counts[status] = counts.get(status, 0) + 1
        props["display_only"] = True
        if key in DISPLAY_NOTES:
            props["display_note"] = DISPLAY_NOTES[key]
        if props.get("NOTE_BRK"):
            # Kept as a display note only: a breakaway/dispute note does not make the
            # feature itself 'disputed' (it is present e.g. on ISR and ATA).
            props["display_note_brk"] = str(props["NOTE_BRK"])
        conn.execute(
            text(
                """INSERT INTO world.display_geometry(dataset_sha256, feature_key, entity_id, match_status,
                     properties, geometry_4326, provenance_activity_id)
                   VALUES (:d, :k, :e, :s, CAST(:p AS jsonb),
                           ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326)), 3)),
                           :a)
                   ON CONFLICT (dataset_sha256, feature_key) DO NOTHING"""
            ),
            {
                "d": summary.raw_sha256,
                "k": f"{key}:{i}",
                "e": entity,
                "s": status,
                "p": json.dumps(props, default=str),
                "g": json.dumps(f["geometry"]),
                "a": act.id,
            },
        )
    repo.emit_outbox(
        conn,
        "world.display_geometry_published",
        {"dataset_sha256": summary.raw_sha256, "normalised_sha256": norm_sha, **counts},
    )
    return counts


def latest_geometry_dataset(conn: Any) -> str | None:
    sha = conn.execute(
        text("SELECT dataset_sha256 FROM world.display_geometry ORDER BY recorded_at DESC LIMIT 1")
    ).scalar_one_or_none()
    return None if sha is None else str(sha)
