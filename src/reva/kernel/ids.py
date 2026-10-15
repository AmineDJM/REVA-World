"""Identifiers and versioned canonical hashing.

IDs are UUIDs. Deterministic external identities use UUIDv5 over provider
namespace, external key and identity epoch; display names are never hashed into
identities. Logical-record hashes use a versioned canonical JSON serialisation
with sorted keys, decimals as strings and NaN/Infinity forbidden.

``reva-cjson-2`` (review R-22) changed two things relative to ``reva-cjson-1``:

* aware datetimes are normalised to UTC before encoding, so the same instant in
  ``+00:00`` and ``+01:00`` has one serialisation (naive datetimes stay forbidden);
* the type tags (``$float``, ``$decimal``, ``$datetime``, ``$date``) are
  unspoofable: every user mapping key that starts with ``$`` is escaped by
  prefixing another ``$`` (``"$decimal"`` -> ``"$$decimal"``), so a user mapping
  can never serialise like a tagged scalar.

Because the version string is part of every logical hash, all logical hashes
changed with the bump; hashes stored under ``reva-cjson-1`` are not comparable.
External identities (``external_id``) are unchanged for components without
backslash or U+001F.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

CANONICAL_HASH_VERSION = "reva-cjson-2"
REVA_NAMESPACE = uuid.UUID("5d0f8f1c-3f61-5c5b-9a8e-6f2b7d2a9c11")


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def external_id(provider: str, external_key: str, identity_epoch: str = "1") -> uuid.UUID:
    """Deterministic UUIDv5 for a provider-scoped key within an identity epoch."""
    if not provider or not external_key:
        raise ValueError("provider and external_key are required")
    parts = (_escape_component(provider), _escape_component(external_key), _escape_component(identity_epoch))
    return uuid.uuid5(REVA_NAMESPACE, "\x1f".join(parts))


def _escape_component(value: str) -> str:
    """Injective escaping of the U+001F separator (identity for ordinary keys)."""
    return value.replace("\\", "\\\\").replace("\x1f", "\\u001f")


def _normalise(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("NaN/Infinity are forbidden in canonical JSON")
        # Floats are encoded via repr to keep round-trip precision explicit.
        return {"$float": repr(obj)}
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError("non-finite Decimal is forbidden")
        return {"$decimal": format(obj, "f")}
    if isinstance(obj, datetime):
        if obj.tzinfo is None or obj.utcoffset() is None:
            raise ValueError("naive datetimes are forbidden in canonical JSON")
        return {"$datetime": obj.astimezone(UTC).isoformat()}
    if isinstance(obj, date):
        return {"$date": obj.isoformat()}
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, Enum):
        return _normalise(obj.value)
    if hasattr(obj, "model_dump"):
        return _normalise(obj.model_dump(mode="python"))
    if isinstance(obj, Mapping):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError("canonical JSON keys must be strings")
            # Escape keys that could collide with type tags ("$decimal" -> "$$decimal").
            out["$" + k if k.startswith("$") else k] = _normalise(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_normalise(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_normalise(v) for v in obj), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(obj, Sequence) and not isinstance(obj, (bytes, bytearray)):
        return [_normalise(v) for v in obj]
    raise TypeError(f"unsupported type for canonical JSON: {type(obj).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic UTF-8 encoding used for logical hashes."""
    return json.dumps(
        _normalise(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def logical_hash(obj: Any) -> str:
    """SHA-256 of the versioned canonical serialisation (prefix-free hex)."""
    h = hashlib.sha256()
    h.update(CANONICAL_HASH_VERSION.encode("ascii"))
    h.update(b"\x00")
    h.update(canonical_bytes(obj))
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def entity_id(key: str) -> uuid.UUID:
    """Deterministic REVA entity identity for a registry key (e.g. 'FRA', 'WORLD', 'HS2709')."""
    return external_id("reva.entity", key)
