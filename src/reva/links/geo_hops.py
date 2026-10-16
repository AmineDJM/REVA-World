"""Explicit, cited geography mapping hops for atlas link derivations (agent LINKS3).

An atlas row whose provider geography is not a crosswalk country never becomes a link, except through a hop of
``registry/links/geo_mapping_hops.yaml`` — and only when the knowledge graph holds the hop statement itself
(e.g. Wikidata ``Q55 — country (P17) → Q29999``: the Netherlands, constituent country, belongs to the Kingdom of
the Netherlands = NLD in the crosswalk). That statement becomes evidence of every link the hop produces, next to
the atlas row; without it the hop is not applied and the rows are counted as unmapped
(``mapping_hop_without_kg_evidence``). Nothing is mapped silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from reva.links.model import Evidence, Stmt
from reva.links.taxonomy import LINKS_DIR, TaxonomyError

HOPS_FILE = LINKS_DIR / "geo_mapping_hops.yaml"


@dataclass(frozen=True)
class GeoHop:
    provider_code: str
    iso3: str
    target_qid: str
    subject: str
    property: str
    object: str
    applies_to: frozenset[tuple[str, str]]
    label: Mapping[str, str]
    target_label: Mapping[str, str]
    why: str

    def applies(self, source: str, datasets: Iterable[str]) -> bool:
        return any((source, d) in self.applies_to for d in datasets)

    def note(self, statement_id: str) -> str:
        return (
            f"mapping hop: {self.provider_code} {self.label.get('en', '')} — {self.property} → {self.object} "
            f"{self.target_label.get('en', '')} = {self.iso3} (registry/links/geo_mapping_hops.yaml; KG statement "
            f"{statement_id})"
        )

    def basis(self, ev: Evidence) -> dict[str, Any]:
        return {
            "provider_code": self.provider_code,
            "provider_label": dict(self.label),
            "iso3": self.iso3,
            "target_qid": self.target_qid,
            "statement": {"subject": self.subject, "property": self.property, "object": self.object},
            "statement_id": ev.statement_id,
            "registry": "registry/links/geo_mapping_hops.yaml",
        }


@lru_cache(maxsize=4)
def load_hops(path: Path = HOPS_FILE) -> tuple[GeoHop, ...]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if doc.get("version") != 1:
        raise TaxonomyError("geo_mapping_hops.yaml: version must be 1")
    out = []
    seen: set[str] = set()
    for h in doc.get("hops") or []:
        code = str(h["provider_code"])
        if code in seen:
            raise TaxonomyError(f"geo_mapping_hops.yaml: duplicate provider code {code}")
        seen.add(code)
        st = h["statement"]
        tgt = h["target"]
        if st["object"] != tgt["qid"]:
            raise TaxonomyError(f"geo_mapping_hops.yaml: {code}: the hop statement must end at the target QID")
        if not h.get("why") or not (h.get("citation") or {}).get("hop_statement"):
            raise TaxonomyError(f"geo_mapping_hops.yaml: {code}: a hop must say why and cite its statement")
        out.append(
            GeoHop(
                provider_code=code,
                iso3=str(tgt["iso3"]),
                target_qid=str(tgt["qid"]),
                subject=str(st["subject"]),
                property=str(st["property"]),
                object=str(st["object"]),
                applies_to=frozenset((str(a["source"]), str(a["dataset"])) for a in h["applies_to"]),
                label=dict(h.get("label") or {}),
                target_label=dict(tgt.get("label") or {}),
                why=" ".join(str(h["why"]).split()),
            )
        )
    return tuple(out)


def hop_evidence_from_statements(
    statements: Iterable[Stmt], hops: Iterable[GeoHop] | None = None
) -> dict[str, Evidence]:
    """provider code -> evidence of its hop statement, for the hops whose statement the KG holds (current,
    rank not deprecated)."""
    hs = {(h.subject, h.property, h.object): h for h in (load_hops() if hops is None else hops)}
    out: dict[str, Evidence] = {}
    for s in statements:
        h = hs.get((s.subject, s.pid, s.object or ""))
        if h is None or str(getattr(s, "rank", "normal")) == "deprecated" or h.provider_code in out:
            continue
        ev = Evidence.of_stmt(s)
        out[h.provider_code] = Evidence(**{**ev.to_json(), "note": h.note(ev.statement_id or "")})
    return out


def hop_evidence_from_db(conn: Any, origin: str | None = None) -> dict[str, Evidence]:
    """The same, read from ``kg.statement_current`` (one bounded query)."""
    from sqlalchemy import text

    from reva.links.paths import _ST_SELECT, row_to_stmt

    hops = load_hops()
    if not hops:
        return {}
    try:
        rows = conn.execute(
            text(
                _ST_SELECT
                + """ WHERE subject_qid = ANY(:s) AND property_pid = ANY(:p) AND object_qid = ANY(:o)
                       AND rank <> 'deprecated' AND (CAST(:origin AS text) IS NULL OR origin = CAST(:origin AS text))
                     LIMIT 1000"""
            ),
            {
                "s": sorted({h.subject for h in hops}),
                "p": sorted({h.property for h in hops}),
                "o": sorted({h.object for h in hops}),
                "origin": origin,
            },
        ).mappings()
        stmts = [row_to_stmt(dict(r)) for r in rows]
    except Exception as e:  # no KG tables (bare test databases): no hop is applied
        if "statement_current" in str(e):
            conn.rollback()
            return {}
        raise
    return hop_evidence_from_statements(stmts, hops)
