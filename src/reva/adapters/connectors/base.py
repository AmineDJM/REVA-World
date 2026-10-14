"""Shared connector helpers."""

from __future__ import annotations

import hashlib
import inspect
import json
from decimal import Decimal
from typing import Any

from reva.adapters.http.safe_client import HttpResult, request_fingerprint
from reva.ports.connectors import FetchItem, RawAcquisition


def module_digest(obj: Any) -> str:
    src = inspect.getsource(inspect.getmodule(obj))  # type: ignore[arg-type]
    return "sha256:" + hashlib.sha256(src.encode()).hexdigest()


def to_raw(item: FetchItem, result: HttpResult, origin: str) -> RawAcquisition:
    return RawAcquisition(
        item=item,
        body=result.body,
        media_type=result.media_type or "application/octet-stream",
        retrieved_at=result.retrieved_at,
        response_metadata=result.response_metadata(),
        request_fingerprint=request_fingerprint("GET", item.url, secret_params=("subscription-key",)),
        origin=origin,  # type: ignore[arg-type]
    )


def json_decimal(body: bytes) -> Any:
    """Parse JSON keeping numbers as Decimal (exact source text, no float rounding)."""
    return json.loads(body, parse_float=Decimal, parse_int=Decimal, parse_constant=_reject)


def _reject(name: str) -> Any:
    raise ValueError(f"non-finite constant {name}")


def dec_str(v: Any) -> str:
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    if not d.is_finite():
        raise ValueError("non-finite number")
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def schema_fingerprint(obj: Any) -> str:
    """Types/dimensions fingerprint that ignores key ordering and values."""

    def shape(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: shape(v) for k, v in sorted(o.items())}
        if isinstance(o, list):
            kinds = sorted({json.dumps(shape(x), sort_keys=True) for x in o[:50]})
            return ["list", kinds]
        if o is None:
            return "null"
        if isinstance(o, bool):
            return "bool"
        if isinstance(o, (int, float, Decimal)):
            return "number"
        return "string"

    return hashlib.sha256(json.dumps(shape(obj), sort_keys=True).encode()).hexdigest()


def plain(v: Any) -> Any:
    """Decimal-free JSON-safe copy (decimals become exact strings)."""
    if isinstance(v, Decimal):
        return dec_str(v)
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v
