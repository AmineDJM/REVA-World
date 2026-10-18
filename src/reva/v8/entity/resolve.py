"""``/fiche/<kind>/<id>`` → what the page is about: a KG record, an anchor fiche, or a special object
(forecast question, effect estimate, source). Lenient on the URL (aliases, ISO3, ``CHEBI:27732``), strict on the
answer: one record or a 404, never a guess between several."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

from reva.kg import query as kq
from reva.v8.entity import profiles as P

QID_RE = re.compile(r"^(Q[1-9][0-9]{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$")
ISO3_RE = re.compile(r"^[A-Za-z]{3}$")
ISO2_RE = re.compile(r"^[A-Za-z]{2}$")
SPECIAL_KINDS = ("question", "effect", "source")


@dataclass
class Resolved:
    kind: str  # canonical URL kind (profile key, KG kind, anchor kind or special kind)
    id: str  # canonical URL id (ISO3 for countries, KG id otherwise, anchor id, question id…)
    via: str  # qid | template | iso3 | anchor | special
    qid: str | None = None
    kg_kind: str | None = None
    iso3: str | None = None
    anchor: dict[str, Any] | None = None
    special: str | None = None
    notes: list[str] = field(default_factory=list)


class NotResolved(Exception):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail


def _known(conn: Connection, cands: list[str]) -> list[str]:
    ok = [c for c in cands if QID_RE.match(c)]
    if not ok:
        return []
    found = set(
        conn.execute(text("SELECT DISTINCT qid FROM kg.entity_current WHERE qid = ANY(:c)"), {"c": ok}).scalars()
    )
    missing = [c for c in ok if c not in found]
    if missing:  # endpoints of statements only (no snapshot loaded): still a page, labelled as such
        for c in missing:
            if kq.exists(conn, c):
                found.add(c)
    return [c for c in ok if c in found]


def _iso3_of(conn: Connection, qid: str) -> str | None:
    reg = {v: k for k, v in kq.country_qid_registry().items()}
    if qid in reg:
        return reg[qid]
    row = conn.execute(
        text("""SELECT value->>'value' FROM kg.statement_current WHERE subject_qid = :q AND property_pid = 'P298'
                 ORDER BY (rank = 'preferred') DESC, id LIMIT 1"""),
        {"q": qid},
    ).scalar_one_or_none()
    return str(row) if row and ISO3_RE.match(str(row)) else None


GENERIC_KINDS = ("unclassified", "wikidata_item")


def best_kind(conn: Connection, qid: str) -> str | None:
    """The record's kind: a specific kind when any loaded snapshot gives one (a generic 'unclassified' Wikidata
    snapshot does not hide the 'country' kind another source gives the same id)."""
    kinds = [
        str(k)
        for k in conn.execute(
            text("""SELECT kind FROM kg.entity_current WHERE qid = :q AND kind IS NOT NULL
                     ORDER BY (origin = 'live') DESC, id DESC"""),
            {"q": qid},
        ).scalars()
    ]
    specific = [k for k in kinds if k not in GENERIC_KINDS]
    return (specific or kinds)[0] if kinds else None


def resolve(conn: Connection, kind: str, ident: str) -> Resolved:
    kind_l = kind.strip().lower()
    ident = ident.strip()
    if not ident or len(ident) > 200:
        raise NotResolved("empty or too long id")
    if kind_l in SPECIAL_KINDS:
        return Resolved(kind=kind_l, id=ident, via="special", special=kind_l)
    anc = P.find_anchor(kind_l, ident)
    if anc is not None:
        akind, aid, spec = anc
        return Resolved(kind=akind, id=aid, via="anchor", anchor={"id": aid, **spec})
    pkey = P.profile_key(kind_l)
    # countries: ISO3 first (the atlas key; any case), ISO alpha-2 mapped to alpha-3, then any KG id
    is_country = pkey == "country" or kind_l in ("country", "pays")
    if is_country and ISO2_RE.match(ident):
        from reva.kg import spacetime as stime

        iso3 = stime.iso2_to_iso3().get(ident.upper())
        if iso3:
            ident = iso3
    if is_country and ISO3_RE.match(ident):
        iso = ident.upper()
        res = kq.iso3_lookup(conn, iso)
        qids = [r["qid"] for r in res["results"]]
        if res.get("ambiguous"):
            raise NotResolved("several KG records hold this ISO code; none is chosen", iso3=iso, candidates=qids[:10])
        qid = qids[0] if qids else None
        kg_kind = best_kind(conn, qid) if qid else None
        r = Resolved(kind="country", id=iso, via="iso3", qid=qid, kg_kind=kg_kind or "country", iso3=iso)
        if qid is None:
            r.notes.append("Aucun enregistrement du graphe de connaissances ne porte ce code ISO : fiche atlas seule.")
        return r
    cands = P.id_candidates(kind_l, ident)
    found = _known(conn, cands)
    if not found:
        raise NotResolved("no KG record for this id", kind=kind_l, id=ident, tried=cands[:8])
    qid = found[0]
    kg_kind = best_kind(conn, qid)
    canon_kind: str = P.profile_key(kg_kind or "") or kg_kind or pkey or kind_l
    if canon_kind == "country" or kg_kind == "country" or qid in {v: k for k, v in kq.country_qid_registry().items()}:
        iso_of = _iso3_of(conn, qid)
        if iso_of:
            return Resolved(
                kind="country",
                id=iso_of,
                via="qid" if qid == ident else "template",
                qid=qid,
                kg_kind=kg_kind,
                iso3=iso_of,
            )
    via = "qid" if qid == ident else "template"
    return Resolved(kind=kg_kind or canon_kind, id=qid, via=via, qid=qid, kg_kind=kg_kind)
