"""Dataset inputs: registration, release pinning and read-only resolution (V2).

Large structured model inputs (input-output matrices, age × sex tables) are
*dataset inputs*: content-addressed Parquet tables derived from archived raw
source bytes. Their life cycle:

1. **register** (worker role, evidence side) — :func:`register_dataset` stores the
   Parquet table and a JSON dataset manifest (namespace ``normalised``), records a
   ``normalisation`` activity whose inputs are the raw artifacts of the source
   acquisitions, and inserts an ``evidence.dataset`` row. The database checks that
   every acquisition exists with the dataset's origin and that both artifacts are
   verified. Idempotent: the same table for the same key returns the existing row.
2. **pin** (worker role, inside ``publish_release``) — :func:`eligible_datasets`
   picks, per (concept, subject), the latest dataset recorded at or before the
   release cutoff with the release's origin; :func:`manifest_entries` lists them in
   the release manifest (``dataset_inputs``, by digest) and
   :func:`pin_release_datasets` writes ``belief.release_dataset`` rows (sealed with
   the release, like estimates) and ``belief.release_artifact`` rows (role
   ``dataset``, verified by ``ops.check_publication``).
3. **resolve** (simulation role) — :func:`resolve_dataset` reads only what the
   scenario's baseline release pinned, re-hashes the bytes against the pinned
   sha256 and checks the row count and schema before handing the model an
   immutable Arrow table. Nothing pinned → a typed ``Missing`` input.

This module never writes evidence observations or beliefs and does not import the
belief publication module (import-linter contract).
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Connection, text

from reva.kernel.errors import IntegrityFailure, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.values import Missing, MissingReason
from reva.ports.storage import ArtifactStore
from reva.simulation.plugins import DatasetInput, InputRequirement

MANIFEST_SCHEMA = "reva-dataset-manifest/1"
TABLE_SCHEMA_VERSION = "dataset-table/1"
MANIFEST_SCHEMA_VERSION = "dataset-manifest/1"
NAMESPACE = "normalised"
PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"


@dataclass(frozen=True)
class DatasetRecord:
    id: UUID
    dataset_key: str
    concept_id: str
    subject_key: str
    origin: str
    artifact_sha256: str
    manifest_sha256: str
    row_count: int
    recorded_at: datetime
    created: bool


def table_schema(table: pa.Table) -> dict[str, str]:
    """Column name -> Arrow type string (the registered, checked schema)."""
    return {f.name: str(f.type) for f in table.schema}


def parquet_bytes(table: pa.Table) -> bytes:
    """Parquet serialisation used for dataset artifacts (zstd, no statistics).

    The bytes are pinned by sha256; like the estimate tables they are not promised
    identical across pyarrow versions (a re-derivation then registers a new artifact).
    """
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", write_statistics=False)
    return buf.getvalue()


def _origin(origin: Any) -> str:
    value = getattr(origin, "value", origin)
    if value not in ("live", "test_fixture"):
        raise ValidationProblem(f"dataset origin must be live or test_fixture (got {value!r})")
    return str(value)


def _row(conn: Connection, where: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(f"""SELECT id, dataset_key, concept_id, subject_key, origin, artifact_sha256, manifest_sha256,
                        row_count, recorded_at FROM evidence.dataset WHERE {where}"""),
            dict(params),
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


def register_dataset(
    conn: Connection,
    store: ArtifactStore,
    *,
    dataset_key: str,
    concept_id: str,
    subject_key: str,
    table: pa.Table,
    source_acquisition_ids: Sequence[UUID | str],
    parser_version: str,
    origin: Any,
    coverage: Mapping[str, Any] | None = None,
    actor: str = "worker:dataset",
    rights: Mapping[str, Any] | None = None,
) -> DatasetRecord:
    """Register a dataset derived from archived raw bytes (worker role, caller's transaction).

    ``table`` must already be the parsed, validated table (the connector/parser owns
    the accounting checks, e.g. IO row/column totals); ``source_acquisition_ids`` are
    the ``evidence.acquisition`` rows whose raw bytes it was parsed from;
    ``parser_version`` names the parser code. Typed failures: ``ValidationProblem``
    for an empty acquisition list, a non-table or unknown acquisitions.
    """
    from reva.adapters.postgres import repositories as repo
    from reva.evidence.locks import take_evidence_write_lock

    if not isinstance(table, pa.Table):
        raise ValidationProblem("dataset table must be a pyarrow.Table")
    if table.num_columns == 0:
        raise ValidationProblem("dataset table has no columns")
    acq_ids = sorted({str(UUID(str(a))) for a in source_acquisition_ids})
    if not acq_ids:
        raise ValidationProblem("a dataset needs at least one source acquisition (archived raw bytes)")
    org = _origin(origin)
    take_evidence_write_lock(conn)  # publication snapshots see all or none of this dataset

    data = parquet_bytes(table)
    sha = hashlib.sha256(data).hexdigest()
    existing = _row(
        conn,
        "dataset_key = :k AND subject_key = :s AND origin = :o AND artifact_sha256 = :a",
        {"k": dataset_key, "s": subject_key, "o": org, "a": sha},
    )
    if existing is not None:
        return DatasetRecord(**existing, created=False)

    acqs = (
        conn.execute(
            text("""SELECT id::text AS id, raw_sha256, origin FROM evidence.acquisition
                WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id"""),
            {"ids": acq_ids},
        )
        .mappings()
        .all()
    )
    found = {a["id"]: a for a in acqs}
    unknown = [a for a in acq_ids if a not in found or found[a]["origin"] != org]
    if unknown:
        raise ValidationProblem(
            "dataset source acquisitions are unknown or have another origin", acquisitions=unknown, origin=org
        )

    schema = table_schema(table)
    obj = store.put_bytes(data, NAMESPACE, PARQUET_MEDIA_TYPE)
    if obj.sha256 != sha:
        raise IntegrityFailure("stored dataset bytes do not match their hash", sha256=sha, stored=obj.sha256)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "dataset_key": dataset_key,
        "concept_id": concept_id,
        "subject_key": subject_key,
        "origin": org,
        "artifact_sha256": sha,
        "media_type": PARQUET_MEDIA_TYPE,
        "parser_version": parser_version,
        "source_acquisition_ids": acq_ids,
        "source_raw_sha256": sorted({found[a]["raw_sha256"] for a in acq_ids}),
        "table_schema": schema,
        "arrow_schema_fingerprint": logical_hash(str(table.schema)),
        "row_count": table.num_rows,
        "coverage": dict(coverage or {}),
        "byte_hash_note": "Parquet bytes are pinned by sha256 and are not promised identical across library versions",
    }
    act = repo.simple_activity(
        "dataset.derive",
        "normalisation",
        actor,
        {
            "dataset_key": dataset_key,
            "concept_id": concept_id,
            "subject_key": subject_key,
            "parser_version": parser_version,
            "source_acquisition_ids": acq_ids,
        },
    )
    repo.register_artifact(
        conn,
        obj,
        schema_version=TABLE_SCHEMA_VERSION,
        rights=dict(rights or {"licence": "derived from source licences; see inputs"}),
    )
    manifest_sha = repo.store_json_artifact(conn, store, manifest, NAMESPACE, schema_version=MANIFEST_SCHEMA_VERSION)
    repo.record_activity(
        conn,
        act,
        inputs=[(raw, {"role": "source_raw"}) for raw in manifest["source_raw_sha256"]],
        outputs=[sha, manifest_sha],
    )
    row = (
        conn.execute(
            text("""INSERT INTO evidence.dataset(dataset_key, concept_id, subject_key, origin, artifact_sha256,
                     manifest_sha256, source_acquisition_ids, parser_version, table_schema, row_count, coverage,
                     provenance_activity_id)
                VALUES (:k, :c, :s, :o, :a, :m, CAST(:acq AS uuid[]), :pv, CAST(:ts AS jsonb), :n,
                        CAST(:cov AS jsonb), :act)
                RETURNING id, dataset_key, concept_id, subject_key, origin, artifact_sha256, manifest_sha256,
                          row_count, recorded_at"""),
            {
                "k": dataset_key,
                "c": concept_id,
                "s": subject_key,
                "o": org,
                "a": sha,
                "m": manifest_sha,
                "acq": acq_ids,
                "pv": parser_version,
                "ts": json.dumps(schema),
                "n": table.num_rows,
                "cov": json.dumps(dict(coverage or {}), sort_keys=True, default=str),
                "act": act.id,
            },
        )
        .mappings()
        .one()
    )
    return DatasetRecord(**dict(row), created=True)


# --------------------------------------------------------------------------- publication hook
ELIGIBLE_SQL = """
SELECT DISTINCT ON (concept_id, subject_key)
       id, dataset_key, concept_id, subject_key, origin, artifact_sha256, manifest_sha256, parser_version,
       row_count, table_schema, source_acquisition_ids, recorded_at
FROM evidence.dataset
WHERE origin = :o AND recorded_at <= :cut
ORDER BY concept_id, subject_key, recorded_at DESC, id DESC
"""


def eligible_datasets(conn: Connection, cutoff: datetime, origin: Any) -> list[dict[str, Any]]:
    """Per (concept, subject): the latest dataset recorded at or before ``cutoff`` with
    the release's origin. Call inside the publication transaction (after the evidence
    snapshot lock), so the set is consistent with the release's evidence snapshot."""
    if not dataset_schema_present(conn):
        return []  # database not yet at migration 0008: no dataset inputs exist
    rows = conn.execute(text(ELIGIBLE_SQL), {"o": _origin(origin), "cut": cutoff}).mappings().all()
    return [dict(r) for r in rows]


def dataset_schema_present(conn: Connection) -> bool:
    return bool(conn.execute(text("SELECT to_regclass('evidence.dataset') IS NOT NULL")).scalar_one())


def manifest_entries(datasets: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The release manifest's ``dataset_inputs`` (pinned by digest)."""
    return [
        {
            "dataset_id": str(d["id"]),
            "dataset_key": d["dataset_key"],
            "concept_id": d["concept_id"],
            "subject_key": d["subject_key"],
            "artifact_sha256": d["artifact_sha256"],
            "manifest_sha256": d["manifest_sha256"],
            "parser_version": d["parser_version"],
            "row_count": int(d["row_count"]),
            "source_acquisition_ids": sorted(str(a) for a in d["source_acquisition_ids"]),
        }
        for d in sorted(datasets, key=lambda d: (d["concept_id"], d["subject_key"]))
    ]


def pin_release_datasets(conn: Connection, release_id: Any, datasets: Iterable[Mapping[str, Any]]) -> int:
    """Pin the datasets in the release (before ``ops.publication`` seals it)."""
    n = 0
    for d in sorted(datasets, key=lambda d: (d["concept_id"], d["subject_key"])):
        conn.execute(
            text("""INSERT INTO belief.release_dataset(release_id, dataset_id, concept_id, subject_key,
                                                       artifact_sha256)
                    VALUES (:r, :d, :c, :s, :a)"""),
            {"r": release_id, "d": d["id"], "c": d["concept_id"], "s": d["subject_key"], "a": d["artifact_sha256"]},
        )
        for sha in (d["artifact_sha256"], d["manifest_sha256"]):
            conn.execute(
                text("""INSERT INTO belief.release_artifact(release_id, artifact_sha256, role)
                        VALUES (:r, :s, 'dataset') ON CONFLICT DO NOTHING"""),
                {"r": release_id, "s": sha},
            )
        n += 1
    return n


def publication_hook(conn: Connection, *, cutoff: datetime, origin: Any) -> tuple[list[dict[str, Any]], Any]:
    """Convenience for ``publish_release``: ``(manifest_entries, pin(release_id))``.

    Returns the release-manifest entries (empty list: add nothing to the manifest) and
    a callable that pins them once the release row exists.
    """
    datasets = eligible_datasets(conn, cutoff, origin)

    def pin(release_id: Any) -> int:
        return pin_release_datasets(conn, release_id, datasets) if datasets else 0

    return manifest_entries(datasets), pin


# --------------------------------------------------------------------------- simulation side
def pinned_datasets(conn: Connection, release_id: Any) -> list[dict[str, Any]]:
    rows = (
        conn.execute(
            text("""SELECT d.id, d.dataset_key, d.concept_id, d.subject_key, d.origin, d.artifact_sha256,
                       d.manifest_sha256, d.parser_version, d.table_schema, d.row_count, d.coverage,
                       d.source_acquisition_ids, d.recorded_at
                FROM belief.release_dataset p JOIN evidence.dataset d ON d.id = p.dataset_id
                WHERE p.release_id = :r ORDER BY d.concept_id, d.subject_key"""),
            {"r": release_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _verified_bytes(store: ArtifactStore, sha: str) -> bytes:
    data = store.get(NAMESPACE, sha)
    if hashlib.sha256(data).hexdigest() != sha:
        raise IntegrityFailure("dataset artifact bytes do not match the pinned sha256", sha256=sha)
    return data


def resolve_dataset(conn: Connection, store: ArtifactStore, release_id: Any, req: InputRequirement) -> DatasetInput:
    """The dataset the release pinned for (concept, subject), loaded read-only and verified."""
    row = (
        conn.execute(
            text("""SELECT d.id, d.artifact_sha256, d.manifest_sha256, d.table_schema, d.row_count
                FROM belief.release_dataset p JOIN evidence.dataset d ON d.id = p.dataset_id
                WHERE p.release_id = :r AND p.concept_id = :c AND p.subject_key = :s"""),
            {"r": release_id, "c": req.concept, "s": req.subject},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return DatasetInput(req, None, None, {}, None, Missing(reason=MissingReason.unavailable))
    manifest_bytes = _verified_bytes(store, row["manifest_sha256"])
    manifest = json.loads(manifest_bytes)
    table = pq.read_table(io.BytesIO(_verified_bytes(store, row["artifact_sha256"])))
    if table.num_rows != row["row_count"] or table_schema(table) != dict(row["table_schema"]):
        raise IntegrityFailure(
            "pinned dataset does not match its registered row count or schema", dataset_id=str(row["id"])
        )
    if manifest.get("artifact_sha256") != row["artifact_sha256"]:
        raise IntegrityFailure("dataset manifest does not name the pinned table", dataset_id=str(row["id"]))
    return DatasetInput(
        req,
        str(row["id"]),
        row["artifact_sha256"],
        {**manifest, "dataset_id": str(row["id"]), "manifest_sha256": row["manifest_sha256"]},
        table,
    )
