"""V4LINK bridge report: how many cross-domain bridges the KG holds and example paths (read-only).

``uv run python -m reva.adapters.connectors.v4link_report [--origin live] [--paths]`` prints JSON:

* ``concordances``: ``v4link:corresponds_to`` statements per UNSD table (scheme -> scheme);
* ``hierarchy``: ``v4link:class_parent`` per scheme;
* ``identity``: ``v4link:wikidata_item`` statements per scheme, Wikidata items holding identifiers of >= 2 schemes
  (the identity bridges proper) with the scheme pairs, and how many scheme ids are KG entities loaded by their
  domain owner (``resolved``) -- an unresolved bridge is kept, waiting for the owner's load;
* ``classification``: ``v4link:classified_as`` per scheme and how many target codes exist in the loaded structure;
* ``places``: ADM1 / NUTS items with a ``P17`` country;
* ``paths`` (``--paths``): the example chains of docs/dev/v4_v4link.md, searched with :func:`reva.kg.query.paths`.

Counting only; nothing is written.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

from sqlalchemy import Connection, text

from reva.adapters.connectors.v4link_common import P_CLASS_PARENT, P_CLASSIFIED_AS, P_CORRESPONDS, P_WIKIDATA_ITEM

#: (from, to, allowed properties or None, max hops, what it shows)
EXAMPLES: tuple[tuple[str, str, tuple[str, ...] | None, int, str], ...] = (
    (
        "hs1992:2709",
        "isic4:0610",
        (P_CLASS_PARENT, P_CORRESPONDS),
        5,
        "HS 1992 heading 2709 (in BACI chapter 27) -> 270900 -> HS 2022 -> HS 2017 -> CPC 12010 -> ISIC 0610",
    ),
    ("hs1992:2709", "hs1992:27", (P_CLASS_PARENT,), 1, "HS 1992 heading 2709 is part of chapter 27 (BACI hs2=27)"),
    ("isic4:0610", "isic4:B", (P_CLASS_PARENT,), 3, "ISIC class 0610 -> group 061 -> division 06 -> section B"),
    (
        "Q111044",
        "isic4:0729",
        (P_CLASSIFIED_AS, P_CORRESPONDS),
        4,
        "chalcopyrite (Wikidata HS 2022 260300) -> HS 2017 -> CPC -> ISIC class (copper ore mining)",
    ),
    (
        "chebi:30187",
        "hs2022:281820",
        (P_WIKIDATA_ITEM, P_CLASSIFIED_AS),
        2,
        "aluminium oxide: ChEBI = item -> HS 281820",
    ),
    ("hs2022:270900", "sitc4:3330", (P_CORRESPONDS,), 1, "HS 2022 crude petroleum -> SITC Rev.4 3330"),
    ("iso3166_2:DE-BY", "nuts:DE2", (P_WIKIDATA_ITEM,), 2, "Bavaria: ISO 3166-2 DE-BY = item = NUTS DE2"),
    ("iso3166_2:DE-BY", "Q183", (P_WIKIDATA_ITEM, "P17"), 2, "ADM1 Bavaria located in Germany (best-rank P17)"),
    ("iso3166_2:SA-04", "Q851", (P_WIKIDATA_ITEM, "P17"), 2, "ADM1 (Eastern Province) located in Saudi Arabia"),
    ("chebi:15377", "pubchem:cid:962", (P_WIKIDATA_ITEM,), 2, "water: ChEBI = Wikidata item = PubChem CID"),
    ("gbif:2436436", "ncbitaxon:9606", (P_WIKIDATA_ITEM,), 2, "Homo sapiens: GBIF key = item = NCBI taxon"),
    ("icd10:E11", "mondo:0005148", (P_WIKIDATA_ITEM,), 2, "type 2 diabetes: ICD-10 = item = MONDO"),
    ("lei:HWUPKR0MPOU8FGXBT394", "sec_cik:320193", (P_WIKIDATA_ITEM,), 2, "Apple Inc.: LEI = item = SEC CIK"),
)


def _scheme(qid: str) -> str:
    return qid.split(":", 1)[0] if ":" in qid else "wikidata"


def bridge_report(conn: Connection, origin: str = "live", with_paths: bool = False) -> dict[str, Any]:
    o = {"o": origin}
    conc = conn.execute(
        text("""SELECT qualifiers->>'table' AS t, split_part(subject_qid, ':', 1) AS a, split_part(object_qid, ':', 1) AS b,
                       count(*) AS n
                FROM kg.statement_current WHERE property_pid = :p AND origin = :o GROUP BY 1, 2, 3 ORDER BY 1"""),
        {"p": P_CORRESPONDS, **o},
    ).all()
    hier = conn.execute(
        text("""SELECT split_part(subject_qid, ':', 1), count(*) FROM kg.statement_current
                WHERE property_pid = :p AND origin = :o GROUP BY 1 ORDER BY 1"""),
        {"p": P_CLASS_PARENT, **o},
    ).all()
    ident = conn.execute(
        text("""SELECT s.subject_qid, s.object_qid, (e.qid IS NOT NULL) AS resolved
                FROM kg.statement_current s
                LEFT JOIN kg.entity_current e ON e.qid = s.subject_qid AND e.origin = s.origin
                WHERE s.property_pid = :p AND s.origin = :o"""),
        {"p": P_WIKIDATA_ITEM, **o},
    ).all()
    by_scheme: Counter[str] = Counter()
    resolved: Counter[str] = Counter()
    item_schemes: dict[str, set[str]] = {}
    for subj, item, res in ident:
        sc = _scheme(subj)
        by_scheme[sc] += 1
        resolved[sc] += int(bool(res))
        item_schemes.setdefault(item, set()).add(sc)
    pairs: Counter[str] = Counter()
    for schemes in item_schemes.values():
        ss = sorted(schemes)
        for i, a in enumerate(ss):
            for b in ss[i + 1 :]:
                pairs[f"{a}={b}"] += 1
    multi = sum(1 for s in item_schemes.values() if len(s) >= 2)
    cls = conn.execute(
        text("""SELECT split_part(s.object_qid, ':', 1), count(*), count(e.qid)
                FROM kg.statement_current s
                LEFT JOIN kg.entity_current e ON e.qid = s.object_qid AND e.origin = s.origin
                WHERE s.property_pid = :p AND s.origin = :o GROUP BY 1 ORDER BY 1"""),
        {"p": P_CLASSIFIED_AS, **o},
    ).all()
    places = conn.execute(
        text("""SELECT count(DISTINCT s.subject_qid) FROM kg.statement_current s
                WHERE s.property_pid = 'P17' AND s.origin = :o AND s.statement_key LIKE '%:v4xref:places:c:%'"""),
        o,
    ).scalar_one()
    out: dict[str, Any] = {
        "origin": origin,
        "concordances": [{"table": t, "from": a, "to": b, "statements": n} for t, a, b, n in conc],
        "concordance_total": sum(r.n for r in conc),
        "hierarchy": {a: n for a, n in hier},
        "identity": {
            "statements_by_scheme": dict(sorted(by_scheme.items())),
            "resolved_to_loaded_entities_by_scheme": dict(sorted(resolved.items())),
            "items": len(item_schemes),
            "items_bridging_two_or_more_schemes": multi,
            "scheme_pairs": dict(pairs.most_common()),
        },
        "classification": [{"scheme": a, "statements": n, "target_code_loaded": k} for a, n, k in cls],
        "places_items_with_country": places,
    }
    out["cross_domain_bridges"] = {
        "concordance_rows_between_schemes": out["concordance_total"],
        "wikidata_items_bridging_two_schemes": multi,
        "item_to_code_classifications": sum(n for _a, n, _k in cls),
        "identity_edges_resolved_to_owner_entities": sum(resolved.values()),
    }
    if with_paths:
        from reva.kg import query as kq

        res_paths: list[dict[str, Any]] = []
        for a, b, props, hops, what in EXAMPLES:
            try:
                r = kq.paths(conn, a, b, max_hops=hops, allowed_properties=props, origin=origin, max_paths=1)
            except Exception as e:  # an unknown endpoint is reported, not fatal for the report
                res_paths.append({"from": a, "to": b, "what": what, "error": f"{type(e).__name__}: {str(e)[:200]}"})
                continue
            p = (r.get("paths") or [None])[0]
            res_paths.append(
                {
                    "from": a,
                    "to": b,
                    "what": what,
                    "found": p is not None,
                    "hops": [
                        f"{h['subject']['qid']} ({h['subject'].get('label') or ''}) -{h['property']['pid']}-> "
                        f"{h['object']['qid']} ({h['object'].get('label') or ''})"
                        for h in (p or {}).get("hops", [])
                    ],
                    "truncated": r.get("truncated"),
                }
            )
        out["paths"] = res_paths
    return out


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - operator entry point
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--origin", default="live")
    ap.add_argument("--paths", action="store_true")
    args = ap.parse_args(argv)
    eng = make_engine("api", get_settings())
    with eng.connect() as c:
        print(json.dumps(bridge_report(c, args.origin, args.paths), indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
