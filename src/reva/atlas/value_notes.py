"""Read-time QA flags for values outside a quantity's definitional range (FIX5, QA-DATA D6).

``registry/atlas/value_range_notes.yaml`` declares, per atlas series (``<source>.<dataset>.<indicator>``), either a
definitional ``range`` (cells outside it are served unchanged with ``flags.reva_qa`` added to a copy of their flags)
or ``signed: true`` (a net quantity whose negative values are within the provider definition). Values are never
altered.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

VALUE_NOTES_FILE = Path(__file__).resolve().parents[3] / "registry" / "atlas" / "value_range_notes.yaml"
REGISTRY_REF = "registry/atlas/value_range_notes.yaml"
FLAG_KEY = "reva_qa"


@lru_cache(maxsize=1)
def value_notes(path: Path = VALUE_NOTES_FILE) -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, Any]] = {}
    for key, e in (doc.get("series") or {}).items():
        if str(key).count(".") < 2:
            raise ValueError(f"{path.name}: series {key!r} is not <source>.<dataset>.<indicator>")
        if "range" in e:
            lo, hi = e["range"]
            if float(lo) > float(hi):
                raise ValueError(f"{path.name}: {key}: empty range")
        elif not e.get("signed"):
            raise ValueError(f"{path.name}: {key}: needs `range` or `signed: true`")
        if not e.get("definition") or not e.get("note"):
            raise ValueError(f"{path.name}: {key}: definition and note are required")
        out[str(key)] = dict(e)
    return out


def signed_series() -> frozenset[str]:
    return frozenset(k for k, e in value_notes().items() if e.get("signed"))


def qa_flag(series: str | None, value: Any) -> dict[str, Any] | None:
    """The ``reva_qa`` flag of a cell whose value is outside its series' definitional range, else ``None``."""
    e = value_notes().get(str(series or ""))
    if not e or "range" not in e or value is None:
        return None
    try:
        v = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    lo, hi = (Decimal(str(x)) for x in e["range"])
    if lo <= v <= hi:
        return None
    return {
        "check": "outside_definitional_range",
        "range": [str(e["range"][0]), str(e["range"][1])],
        "value_unchanged": True,
        "note": e["note"],
        "registry": REGISTRY_REF,
    }


def with_qa_flag(series: str | None, value: Any, flags: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """``flags`` unchanged, or a copy carrying ``reva_qa`` (the shared parsed flags are never mutated)."""
    q = qa_flag(series, value)
    if q is None:
        return flags
    return {**(flags if isinstance(flags, Mapping) else {}), FLAG_KEY: q}
