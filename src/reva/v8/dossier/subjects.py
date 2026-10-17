"""Subject picker for briefs: featured subjects of each template + lookups (countries, KG entities, decisions)."""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from reva.v8.dossier import model as M
from reva.v8.dossier.templates import all_templates

log = logging.getLogger(__name__)

#: KG entity kinds a brief kind accepts (generic subjects) and the id prefix it requires.
KG_KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "disease": (("disease",), ("mondo:", "doid:")),
    "drug": (("drug",), ("chembl:",)),
    "conflict": (("armed_conflict",), ("ucdp:conflict:",)),
}


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c)).strip()


def kinds() -> list[dict[str, str]]:
    return [{"kind": t.t.kind, "label": t.t.label} for t in all_templates()]


def featured() -> list[M.SubjectHit]:
    out = []
    for tpl in all_templates():
        t = tpl.t
        seen = set()
        for f in t.featured:
            out.append(M.SubjectHit(kind=t.kind, id=f.id, label=f.label, sublabel=f.sublabel, featured=True))
            seen.add(f.id)
        for sid, s in t.subjects.items():
            if s.featured and sid not in seen:
                out.append(M.SubjectHit(kind=t.kind, id=sid, label=s.label, sublabel=s.sublabel, featured=True))
    return out


def search(conn: Any, q: str | None, kind: str | None = None, limit: int = 20) -> M.SubjectList:
    hits: list[M.SubjectHit] = []
    fq = fold(q or "")
    for h in featured():
        if kind and h.kind != kind:
            continue
        if not fq or fq in fold(h.label) or fq in fold(h.id) or fq in fold(h.sublabel or ""):
            hits.append(h)
    if fq:
        if kind in (None, "country"):
            from reva.nlqa.catalog import load_countries

            for c in load_countries():
                names = [c.label_fr or "", c.label, c.iso3, *c.names]
                if any(fq == fold(n) or fold(n).startswith(fq) for n in names if n):
                    if not any(h.kind == "country" and h.id == c.iso3 for h in hits):
                        hits.append(
                            M.SubjectHit(kind="country", id=c.iso3, label=c.label_fr or c.label, sublabel=c.iso3)
                        )
                if len(hits) >= limit:
                    break
        if kind in (None, "decision"):
            from reva.predict.decision import reference_decisions

            for d in reference_decisions().values():
                dd = d.model_dump(mode="json")
                if fq in fold(dd["label_fr"]) or fq in fold(dd["id"]):
                    if not any(h.kind == "decision" and h.id == dd["id"] for h in hits):
                        hits.append(
                            M.SubjectHit(kind="decision", id=dd["id"], label=dd["label_fr"], sublabel=dd["instrument"])
                        )
        if conn is not None and len(fq) >= 3:
            from reva.kg import query as kq

            for bk, (ekinds, prefixes) in KG_KINDS.items():
                if kind not in (None, bk):
                    continue
                for ek in ekinds:
                    try:
                        res = kq.search(conn, q or "", langs=["fr", "en"], kind=ek, limit=8, origin="live")
                    except Exception as e:  # a failing lookup only means fewer suggestions
                        log.info("dossier subject search (%s) failed: %s", ek, e)
                        continue
                    for r in res["results"]:
                        if not r["qid"].startswith(prefixes):
                            continue
                        lab = (r.get("labels") or {}).get("fr") or (r.get("labels") or {}).get("en") or r["qid"]
                        if not any(h.id == r["qid"] for h in hits):
                            hits.append(
                                M.SubjectHit(kind=bk, id=r["qid"], label=lab, sublabel=f"{r['kind']} · {r['qid']}")
                            )
    return M.SubjectList(query=q, hits=hits[:limit], kinds=kinds())
