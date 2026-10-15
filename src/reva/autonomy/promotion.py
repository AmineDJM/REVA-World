"""Promoted declarative connectors: content-derived versions, eligibility state, rollback.

* **Versioning (R4-03).** A connector's identity is ``(name, version)``. A manifest
  whose content (everything but ``version``) or template code digest differs from
  every registered version of that name gets a *new* semver patch version; an
  identical one reuses its version. Registration refuses to silently map a
  different spec onto an existing ``(name, version)`` row.
* **State (R4-03/R4-04).** For each registered template connector version the
  state is ``retired`` (latest eligibility decision is not ``eligible``),
  ``template_changed`` (its code digest is not the current reviewed template), or
  ``active``. Only ``active`` versions are loaded for ingestion; per name the most
  recently registered active version wins.
* **Rollback (R4-04).** Retiring a version that superseded an earlier one
  re-appends ``eligible`` for that earlier version (if it is still runnable), so
  the previous connector is restored; history stays append-only.
* **Rollback hold on content (R6-05).** The hold is keyed on the manifest *content*
  (everything but ``version``), across every version and template digest: if any
  registered version with the same content was rolled back (latest decision
  ``retired`` that is not a supersession), the content stays held until a named
  human re-approves it. Neither a proposer-chosen version nor a later template
  re-pin can re-enable rolled-back content.
* **Canonical versions (R6-05).** Only ``MAJOR.MINOR.PATCH`` without leading zeros
  or suffixes; anything else is not a version (``"1.0.0\n"`` is refused).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.adapters.connectors import eurostat_template as et
from reva.autonomy.common import ArtifactIO
from reva.ports.autonomy import ArtifactRef

log = logging.getLogger("reva.autonomy")
TEMPLATE_CAPABILITY = et.TEMPLATE_ID

VERSIONS_SQL = """
SELECT cv.id, cv.name, cv.version, cv.spec_sha256, cv.code_digest, cv.recorded_at, s.source_key,
       cv.declared_capabilities->>'template' AS template,
       (SELECT e.decision FROM evidence.eligibility e WHERE e.connector_version_id = cv.id
         ORDER BY e.recorded_at DESC, e.id DESC LIMIT 1) AS latest_decision
FROM evidence.connector_version cv JOIN evidence.source s ON s.id = cv.source_id
"""


def content_key(manifest: Mapping[str, Any]) -> str:
    return et.manifest_digest({k: v for k, v in manifest.items() if k != "version"})


def _semver(v: str) -> tuple[int, int, int]:
    m = et.SEMVER_RE.fullmatch(str(v))
    if m is None:
        return (0, 0, 0)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def spec_manifest(io_: ArtifactIO, spec_sha256: str) -> dict[str, Any] | None:
    try:
        spec = io_.get_json(ArtifactRef(spec_sha256, "models", "application/json"))
        return dict(spec["field_mappings"]["manifest"])
    except Exception:
        return None


def versions_of(conn: Connection, name: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            text(VERSIONS_SQL + " WHERE cv.name = :n ORDER BY cv.recorded_at, cv.id"), {"n": name}
        ).mappings()
    ]


def resolve_connector_version(
    conn: Connection, io_: ArtifactIO, manifest: Mapping[str, Any], code_digest: str
) -> tuple[str, UUID | None]:
    """(version, existing connector_version id or None) for this manifest content + template digest."""
    rows = versions_of(conn, str(manifest["name"]))
    key = content_key(manifest)
    for r in rows:
        m = spec_manifest(io_, r["spec_sha256"])
        if m is not None and content_key(m) == key and r["code_digest"] == code_digest:
            return str(r["version"]), UUID(str(r["id"]))
    if not rows:
        return "1.0.0", None
    major, minor, patch = max(_semver(r["version"]) for r in rows)
    return f"{major}.{minor}.{patch + 1}", None


def rolled_back_content(
    conn: Connection, io_: ArtifactIO, manifest: Mapping[str, Any], concept_version_id: Any
) -> list[dict[str, Any]]:
    """Registered versions with this manifest's *content* that were rolled back (not superseded).

    Any hit means ``rolled_back_hold``: the content needs a named human re-approval
    (``jobs.reapprove_promotion``), whatever version or template digest a new proposal carries.
    """
    key = content_key(manifest)
    out = []
    for r in versions_of(conn, str(manifest["name"])):
        m = spec_manifest(io_, r["spec_sha256"])
        if m is None or content_key(m) != key:
            continue
        last = (
            conn.execute(
                text(
                    """SELECT decision, applicability FROM evidence.eligibility
               WHERE connector_version_id = :cv AND concept_version_id = :c
               ORDER BY recorded_at DESC, id DESC LIMIT 1"""
                ),
                {"cv": r["id"], "c": concept_version_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            last is not None
            and last["decision"] != "eligible"
            and "superseded_by" not in dict(last["applicability"] or {})
        ):
            out.append({"connector_version_id": str(r["id"]), "version": r["version"], "decision": last["decision"]})
    return out


def same_registered_content(
    io_: ArtifactIO, row: Mapping[str, Any], manifest: Mapping[str, Any], code_digest: str
) -> bool:
    m = spec_manifest(io_, row["spec_sha256"])
    return m is not None and m == dict(manifest) and row["code_digest"] == code_digest


def latest_decision(conn: Connection, connector_version_id: Any, concept_version_id: Any) -> str | None:
    return conn.execute(
        text(
            """SELECT decision FROM evidence.eligibility WHERE connector_version_id = :cv AND concept_version_id = :c
           ORDER BY recorded_at DESC, id DESC LIMIT 1"""
        ),
        {"cv": connector_version_id, "c": concept_version_id},
    ).scalar_one_or_none()


def connector_states(conn: Connection, current_digest: str | None = None) -> list[dict[str, Any]]:
    """State of every registered template connector version (``active``/``template_changed``/``retired``)."""
    digest = current_digest or et.template_digest()
    rows = (
        conn.execute(
            text(VERSIONS_SQL + " WHERE cv.declared_capabilities->>'template' = :t ORDER BY cv.recorded_at, cv.id"),
            {"t": TEMPLATE_CAPABILITY},
        )
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        if r["latest_decision"] != "eligible":
            state = "retired" if r["latest_decision"] else "not_eligible"
        elif r["code_digest"] != digest:
            state = "template_changed"
        else:
            state = "active"
        out.append(
            {
                "connector_version_id": str(r["id"]),
                "name": r["name"],
                "version": r["version"],
                "source_key": r["source_key"],
                "spec_sha256": r["spec_sha256"],
                "code_digest": r["code_digest"],
                "latest_decision": r["latest_decision"],
                "state": state,
            }
        )
    return out


def active_specs(engine: Engine, current_digest: str | None = None) -> dict[str, dict[str, Any]]:
    """name -> newest ``active`` version row (later registrations win)."""
    from reva.autonomy.binding import builtin_connector_names

    builtins = builtin_connector_names()
    with engine.connect() as conn:
        states = connector_states(conn, current_digest)
    out: dict[str, dict[str, Any]] = {}
    for s in states:
        if s["state"] == "template_changed":
            log.warning(
                "promoted connector %s@%s disabled: template_changed (re-validation required)", s["name"], s["version"]
            )
        if s["state"] != "active":
            continue
        if s["name"] in builtins:
            log.error("promoted connector %s@%s collides with a built-in name; ignored", s["name"], s["version"])
            continue
        out[s["name"]] = s
    return out
