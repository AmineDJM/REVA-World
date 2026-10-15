"""Append-only operational audit records (ops.audit is immutable).

Callers pass the connection of the transaction that performs the audited
action, so the audit row commits atomically with it. Details are bounded and
secret-redacted; actors are namespaced (``worker:...``, ``human:...``,
``scheduler:...``, ``system``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.kernel.errors import ValidationProblem
from reva.operations.telemetry import redact_obj

MAX_DETAILS_BYTES = 16_384
_ACTION = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_ACTOR = re.compile(r"[A-Za-z0-9_.@:\-/]{1,200}")


def _bounded_json(obj: Mapping[str, Any]) -> str:
    # Normalise to plain JSON values first (str() of UUIDs, datetimes...), then redact leaf by leaf:
    # secret-named keys are masked whole and text values lose token/DSN/SQL-parameter shapes, while
    # the document stays valid JSON (text-level redaction of serialized JSON could cut it).
    plain = json.loads(json.dumps(dict(obj), sort_keys=True, default=str, allow_nan=False))
    raw = json.dumps(redact_obj(plain), sort_keys=True, allow_nan=False)
    if len(raw.encode()) > MAX_DETAILS_BYTES:
        raw = json.dumps({"truncated": True, "preview": raw[: MAX_DETAILS_BYTES // 2]})
    return raw


def record(
    conn: Connection,
    actor: str,
    action: str,
    target: Mapping[str, Any],
    details: Mapping[str, Any] | None = None,
) -> UUID:
    """Insert one audit row in the caller's transaction and return its id."""
    if not _ACTOR.fullmatch(actor):
        raise ValidationProblem("invalid audit actor", actor=actor[:64])
    if not _ACTION.fullmatch(action):
        raise ValidationProblem("audit action must be dotted lower-case (e.g. job.cancel)", action=action[:64])
    rid = conn.execute(
        text(
            "INSERT INTO ops.audit(actor, action, target, details) "
            "VALUES (:a, :ac, CAST(:t AS jsonb), CAST(:d AS jsonb)) RETURNING id"
        ),
        {"a": actor, "ac": action, "t": _bounded_json(target), "d": _bounded_json(details or {})},
    ).scalar_one()
    return UUID(str(rid))
