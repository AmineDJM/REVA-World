"""Per-kind Fiche profiles (``profiles.yaml``): URL kind aliases, id templates, headline specs, expected facts,
anchor fiches and authority links. Read-only, cached per process; nothing here is a value."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROFILES_PATH = Path(__file__).with_name("profiles.yaml")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    doc = yaml.safe_load(PROFILES_PATH.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise ValueError("profiles.yaml: version 1 expected")
    return doc


def profiles() -> dict[str, dict[str, Any]]:
    return dict(load().get("kinds") or {})


def anchors() -> dict[str, dict[str, dict[str, Any]]]:
    return dict(load().get("anchors") or {})


def profile_key(kind: str) -> str | None:
    """The profile a URL kind (or a KG kind) belongs to: exact key, alias or member KG kind."""
    k = kind.strip().lower()
    ps = profiles()
    if k in ps:
        return k
    for key, p in ps.items():
        if k in (p.get("aliases") or []) or k in (p.get("kg_kinds") or []):
            return key
    return None


def profile_for(kind: str | None) -> dict[str, Any]:
    key = profile_key(kind or "")
    return profiles().get(key, {}) if key else {}


def profile_for_kg_kind(kg_kind: str | None) -> tuple[str | None, dict[str, Any]]:
    if not kg_kind:
        return None, {}
    for key, p in profiles().items():
        if kg_kind == key or kg_kind in (p.get("kg_kinds") or []):
            return key, p
    return None, {}


def kind_label_fr(kind: str | None) -> str:
    """French label of a KG kind: the explicit table, else the profile whose main kind it is, else the id."""
    if not kind:
        return "Entité (type non chargé)"
    labels = load().get("kind_labels_fr") or {}
    if kind in labels:
        return str(labels[kind])
    for key, p in profiles().items():
        if kind == key or kind == (p.get("kg_kinds") or [None])[0]:
            return str(p.get("label_fr") or kind)
    return kind.replace("_", " ").capitalize()


def find_anchor(kind: str, ident: str) -> tuple[str, str, dict[str, Any]] | None:
    """``(anchor kind, anchor id, spec)`` for a URL naming an anchor fiche (id or one of its aliases)."""
    k = profile_key(kind) or kind.strip().lower()
    i = ident.strip().lower()
    for akind, items in anchors().items():
        if akind != k:
            continue
        for aid, spec in items.items():
            if i == aid or i in [str(a).lower() for a in spec.get("aliases") or []]:
                return akind, aid, spec
    return None


def id_candidates(kind: str, ident: str) -> list[str]:
    """KG ids to try for ``/fiche/<kind>/<ident>``, most specific first (the raw id is always tried)."""
    p = profile_for(kind)
    out: list[str] = []
    raw = ident.strip()
    local = raw.split(":", 1)[1] if ":" in raw and raw.split(":", 1)[0].upper() == raw.split(":", 1)[0] else raw
    for t in p.get("id_templates") or []:
        for v in (raw, local):
            cand = str(t).replace("{ID}", v.upper()).replace("{id}", v)
            if cand not in out:
                out.append(cand)
    if raw not in out:
        out.insert(0, raw)
    low = raw.lower()
    if ":" in raw and low != raw and low not in out:  # "CHEBI:27732" -> "chebi:27732"
        pre, rest = raw.split(":", 1)
        out.append(f"{pre.lower()}:{rest}")
    return out


def authority_url(qid: str) -> str | None:
    links = load().get("authority_links") or {}
    if re.match(r"^Q[1-9][0-9]*$", qid):
        return str(links["Q"]).replace("{qid}", qid) if "Q" in links else None
    if ":" not in qid:
        return None
    prefix, local = qid.split(":", 1)
    tpl = links.get(prefix)
    if not tpl:
        return None
    return str(tpl).replace("{id}", local).replace("{last}", local.rsplit(":", 1)[-1]).replace("{qid}", qid)


def value_url(pid: str, value: str) -> str | None:
    """Link of an external-id literal (by property, or by the ``PREFIX:`` of a cross-reference value)."""
    links = load().get("value_links") or {}
    if pid in links:
        return str(links[pid]).replace("{v}", value)
    if ":" in value:
        pre, code = value.split(":", 1)
        if pre in links:
            return str(links[pre]).replace("{v}", code)
    return None
