"""Council on Foreign Relations Cyber Operations Tracker -> dated cyber-operation KG events with the suspected state
sponsor *as attributed by CFR*, and sponsor-year atlas counts (V6, agent V6DIGITAL; report ``docs/dev/v6_digital.md``).

Source (researched 2026-10-17)
------------------------------
* Tracker page ``https://www.cfr.org/cyber-operations/`` (archived each run: glossary and methodology quoted from
  it): "The Digital and Cyberspace Policy program's cyber operations tracker is a database of the publicly known
  state-sponsored incidents that have occurred since 2005." Its "Download the Data" link is
  ``https://static.cfr.org/cyber-operations/data/cyber-operations-incidents.csv`` (12 columns: Title, Date,
  Affiliations, Description, Response, Victims, Sponsor, Type, Category, Sources_1..3; read 2026-10-17: 897
  incidents dated 2005-08-25 .. 2023-12-19, 19 undated; Last-Modified 2025-11-13). The page lists 2024 incidents
  that the CSV does not contain yet (documented, not scraped).
* Attribution: "This data set identifies suspected threat actors and their state sponsors based on what the
  reporting suggests and whether the tools, techniques, and procedures used by the threat actor conform to what is
  known about a state sponsor's preferred methods of intrusion." Every sponsor edge is therefore CFR's *suspected*
  sponsor, an attribution claim (qualifier ``attribution``), never a REVA finding; victim countries are not coded in
  the CSV (free text ``Victims`` only) and are not inferred.
* Licence: CFR Terms of Use (``https://www.cfr.org/terms-use``, archived): "You agree not to sell or modify such
  content or materials or reproduce, display, publicly perform, distribute, or otherwise use such content or
  materials in any way for any public or commercial purpose" (footer: "All rights reserved"). Recorded as
  restricted: non-commercial, no redistribution; the owner must ask CFR before any public use (docs/dev/v6_digital.md).

Outputs
-------
* atlas ``cfr_cyber.sponsor_year``: exact counts of incidents per suspected sponsor (provider name, exact-name
  scheme ``cfr_cyber_country``) x calendar year of ``Date``: ``SUSPECTED_SPONSORED_OPERATIONS``, ``...|type=<slug>``
  and ``...|victim_category=<slug>`` (each listed category). An incident with several sponsors counts for each
  (flagged). Undated incidents are not counted (reported). Non-zero cells only: an absent cell is "none recorded
  by CFR", not a measured zero.
* KG (source ``cfr.cyber_operations``): ``cfr_cyber:operation:<id>`` (kind ``cyber_operation``; id = sha256 of the
  verbatim Title + Date, first 16 hex, CFR publishing no identifier), P585 (Date), ``cfr_cyber:suspected_state_sponsor``
  -> country QID (else ``iso3:<code>``), type, victim category, victims, threat actor and sponsor as stated,
  description, response and source links (all verbatim).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from reva.adapters.connectors import digital_common as dc
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement, load_qid_for_iso3
from reva.adapters.connectors.vintage_common import Rows
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.cfr_cyber"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_cfr_cyber/1"
SOURCE_KEY = "cfr.cyber_operations"
SOURCE = "cfr_cyber"
DATASET = "sponsor_year"
GEO_SCHEME = "cfr_cyber_country"
PAGE_URL = "https://www.cfr.org/cyber-operations/"
CSV_URL = "https://static.cfr.org/cyber-operations/data/cyber-operations-incidents.csv"
TERMS_URL = "https://www.cfr.org/terms-use"
HOSTS = ("www.cfr.org", "static.cfr.org")
HEADER = (
    "Title",
    "Date",
    "Affiliations",
    "Description",
    "Response",
    "Victims",
    "Sponsor",
    "Type",
    "Category",
    "Sources_1",
    "Sources_2",
    "Sources_3",
)
#: CSV ``Type`` value -> the tracker glossary term that defines it
GLOSSARY_TERMS = {
    "Denial of service": "Distributed Denial of Service",
    "Espionage": "Espionage",
    "Defacement": "Defacement",
    "Data destruction": "Data Destruction",
    "Sabotage": "Sabotage",
    "Doxing": "Doxing",
    "Financial Theft": "Financial Theft",
}
_GLOSSARY_ORDER = (
    "Distributed Denial of Service",
    "Espionage",
    "Defacement",
    "Data Destruction",
    "Sabotage",
    "Doxing",
    "Financial Theft",
    "These terms were adapted",
)
ATTRIBUTION_QUOTE = (
    "This data set identifies suspected threat actors and their state sponsors based on what the reporting suggests"
)
KG_PIDS = (
    "cfr_cyber:suspected_state_sponsor",
    "cfr_cyber:operation_type",
    "cfr_cyber:victim_category",
    "cfr_cyber:victims",
    "cfr_cyber:threat_actor_as_stated",
    "cfr_cyber:sponsor_as_stated",
    "cfr_cyber:description",
    "cfr_cyber:government_response",
    "cfr_cyber:source_url",
)
WIKIDATA_PIDS = ("P585",)
LICENCE = (
    "CFR Terms of Use: all rights reserved; no reproduction or distribution for any public or commercial purpose "
    "(non-commercial; redistribution not granted)"
)
_ISO = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")


class CfrError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


@dataclass
class Operation:
    key: str
    row: int
    values: dict[str, str]
    when: date | None
    sponsors: list[str] = field(default_factory=list)

    @property
    def node(self) -> str:
        return f"cfr_cyber:operation:{self.key}"


def page_facts(page_html: bytes, terms_html: bytes | None = None) -> dict[str, Any]:
    t = dc.page_text(page_html)
    gl = t[t.find("Anchor Name Link Glossary") :] if "Anchor Name Link Glossary" in t else ""
    glossary = {}
    for term in GLOSSARY_TERMS.values():
        ends = tuple(f" {x}" for x in _GLOSSARY_ORDER if x != term)
        glossary[term] = dc.quote_between(gl, f"{term} ", ends, limit=500)
    facts: dict[str, Any] = {
        "scope": dc.quote_between(t, "The Digital and Cyberspace Policy program", (" Know of an incident",)),
        "attribution": dc.quote_between(t, ATTRIBUTION_QUOTE, (" Completeness of Data",)),
        "completeness": dc.quote_between(t, "No claims are made that the data", (" There are three reasons",)),
        "inclusion": dc.quote_between(
            t, "The tracker only contains data in which the perpetrator", (" The tracker focuses",)
        ),
        "rights_footer": "All rights reserved." if "All rights reserved" in t else "",
        "glossary": glossary,
    }
    if terms_html is not None:
        tt = dc.page_text(terms_html)
        facts["terms"] = dc.quote_between(tt, "You agree not to sell or modify such content", (" REGISTRATION",), 700)
    return facts


def parse_csv(body: bytes, known: set[str]) -> tuple[list[Operation], dict[str, Any]]:
    text = body.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or tuple(h.strip() for h in rows[0]) != HEADER:
        raise CfrError(f"header changed: {rows[0] if rows else None}")
    out: list[Operation] = []
    seen: Counter[str] = Counter()
    for rnum, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        if len(r) != len(HEADER):
            raise CfrError(f"row {rnum}: {len(r)} cells")
        v = {h: c.strip() for h, c in zip(HEADER, r, strict=True)}
        if not v["Title"]:
            raise CfrError(f"row {rnum}: empty Title")
        when = None
        if v["Date"]:
            m = _ISO.match(v["Date"])
            if not m:
                raise CfrError(f"row {rnum}: Date {v['Date']!r} is not YYYY-MM-DD")
            when = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        base = hashlib.sha256(f"{v['Title']}\x1f{v['Date']}".encode()).hexdigest()[:16]
        seen[base] += 1
        key = base if seen[base] == 1 else f"{base}-{seen[base]}"
        sponsors = dc.split_countries(v["Sponsor"], known, seps=(",",)) if v["Sponsor"] else []
        out.append(Operation(key, rnum, v, when, sponsors))
    if not out:
        raise CfrError("no incident")
    dated = [o.when for o in out if o.when]
    stats = {
        "incidents": len(out),
        "undated": sum(o.when is None for o in out),
        "first": min(dated).isoformat() if dated else None,
        "last": max(dated).isoformat() if dated else None,
        "no_sponsor": sum(not o.sponsors for o in out),
        "multi_sponsor": sum(len(o.sponsors) > 1 for o in out),
        "types": dict(Counter(o.values["Type"] or "(blank)" for o in out).most_common()),
        "sponsors": dict(Counter(s for o in out for s in o.sponsors).most_common(15)),
    }
    return out, stats


def atlas_frame(ops: list[Operation], sha: str, glossary: dict[str, str], attribution: str) -> Any:
    counts: Counter[tuple[str, int, str]] = Counter()
    labels: dict[str, str] = {}
    multi: set[tuple[str, int]] = set()
    for o in ops:
        if o.when is None:
            continue
        cats = [c.strip() for c in o.values["Category"].split(",") if c.strip()]
        for s in o.sponsors:
            y = o.when.year
            if len(o.sponsors) > 1:
                multi.add((s, y))
            counts[(s, y, "SUSPECTED_SPONSORED_OPERATIONS")] += 1
            typ = o.values["Type"] or "blank"
            ind = f"SUSPECTED_SPONSORED_OPERATIONS|type={dc.slug(typ)}"
            labels[ind] = f"Type = {o.values['Type'] or '(blank)'}"
            counts[(s, y, ind)] += 1
            for c in dict.fromkeys(cats):
                ind = f"SUSPECTED_SPONSORED_OPERATIONS|victim_category={dc.slug(c)}"
                labels[ind] = f"Category lists {c}"
                counts[(s, y, ind)] += 1
    rows = Rows()
    for (s, y, ind), n in sorted(counts.items()):
        typ = labels.get(ind, "")
        definition = ""
        if typ.startswith("Type = "):
            term = GLOSSARY_TERMS.get(typ.removeprefix("Type = "))
            definition = glossary.get(term or "", "")
        rows.add(
            source=SOURCE,
            dataset=DATASET,
            indicator=ind,
            provider_geo=s,
            start=date(y, 1, 1),
            end=date(y + 1, 1, 1),
            value=str(n),
            unit="count of incidents (CFR Cyber Operations Tracker)",
            flags=dc.canon(
                {
                    "derived": "count",
                    "attribution": attribution or ATTRIBUTION_QUOTE,
                    "sponsor": "suspected state sponsor as attributed by CFR (not established by REVA)",
                    "breakdown": typ or "all incidents",
                    "glossary_definition": definition or None,
                    "multi_sponsor_incidents_included": (s, y) in multi,
                    "absent_cell": "none recorded by CFR (not a measured zero)",
                }
            ),
            raw_sha256=sha,
            selector={"file": CSV_URL.rsplit("/", 1)[-1], "group_by": ["Sponsor", "year(Date)"], "indicator": ind},
            iso3=country_iso3(s, GEO_SCHEME),
        )
    return rows.frame()


def _s(v: str) -> dict[str, Any]:
    return {"type": "string", "value": v[:4000]}


def kg_records(
    ops: list[Operation], sha: str, qids: dict[str, str], facts: dict[str, Any]
) -> tuple[list[KgEntity], list[KgStatement], dict[str, Any]]:
    ents: list[KgEntity] = []
    sts: list[KgStatement] = []
    stats: Counter[str] = Counter()
    attribution = facts.get("attribution") or ATTRIBUTION_QUOTE
    for o in ops:
        v = o.values
        ents.append(
            KgEntity(
                qid=o.node,
                kind="cyber_operation",
                labels={"en": f"{v['Title'][:200]} ({o.when.isoformat() if o.when else 'undated'}, CFR tracker)"},
                descriptions={
                    "en": "State-sponsored cyber incident as recorded by the CFR Cyber Operations Tracker; "
                    f"suspected sponsor per CFR: {v['Sponsor'] or 'not stated'}"
                },
            )
        )
        sel = {"file": CSV_URL.rsplit("/", 1)[-1], "row": o.row}

        def add(key: str, prop: str, *, _o: Operation = o, _sel: dict[str, Any] = sel, **kw: Any) -> None:
            sts.append(
                KgStatement(key=f"{_o.key}:{key}", subject=_o.node, property=prop, raw_sha256=sha, selector=_sel, **kw)
            )

        if o.when:
            add(
                "date",
                "P585",
                value={"type": "time", "time": o.when.isoformat(), "precision": 11},
                qualifiers={"column": "Date"},
            )
        tkw: dict[str, Any] = {"start": o.when.isoformat(), "start_precision": 11} if o.when else {}
        for s in o.sponsors:
            iso = country_iso3(s, GEO_SCHEME)
            if iso is None:
                stats["sponsor_unmapped"] += 1
                continue
            stats["sponsor_qid" if iso in qids else "sponsor_iso3_node"] += 1
            add(
                f"sponsor:{iso}",
                "cfr_cyber:suspected_state_sponsor",
                object_qid=qids.get(iso) or f"iso3:{iso}",
                qualifiers={
                    "sponsor_as_stated": s,
                    "iso3": iso,
                    "attribution": attribution[:500],
                    "claim": "suspected state sponsor as attributed by CFR",
                },
                **tkw,
            )
        for col, prop in (
            ("Type", "cfr_cyber:operation_type"),
            ("Category", "cfr_cyber:victim_category"),
            ("Victims", "cfr_cyber:victims"),
            ("Affiliations", "cfr_cyber:threat_actor_as_stated"),
            ("Sponsor", "cfr_cyber:sponsor_as_stated"),
            ("Response", "cfr_cyber:government_response"),
            ("Description", "cfr_cyber:description"),
        ):
            if v[col]:
                q: dict[str, Any] = {"column": col}
                if col == "Type":
                    q["glossary_definition"] = facts.get("glossary", {}).get(GLOSSARY_TERMS.get(v[col], ""), "") or None
                if len(v[col]) > 4000:
                    q["truncated"] = True
                add(col.lower(), prop, value=_s(v[col]), qualifiers=q)
        for col in ("Sources_1", "Sources_2", "Sources_3"):
            if v[col]:
                add(col.lower(), "cfr_cyber:source_url", value=_s(v[col]), qualifiers={"column": col})
    stats["entities"] = len(ents)
    stats["statements"] = len(sts)
    return ents, sts, dict(stats)


def rights(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "licence": LICENCE,
        "terms_url": TERMS_URL,
        "attribution": "Council on Foreign Relations, Cyber Operations Tracker (Digital and Cyberspace Policy program)",
        "provider": "Council on Foreign Relations (CFR)",
        "commercial_use": "not_permitted",
        "non_commercial": True,
        "redistribution": "not_granted",
        "open_licence": False,
        "restriction": "CFR Terms of Use: no reproduction, display or distribution for any public or commercial purpose",
        "terms": facts.get("terms") or None,
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


def code_digest() -> str:
    return dc.module_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="Council on Foreign Relations, Cyber Operations Tracker",
        licence=LICENCE,
        allowed_hosts=HOSTS,
        datasets=(DATASET,),
        dataset_ids=("cyber-operations-incidents.csv",),
        cadence_seconds=90 * 86_400,
        planned_requests=3,
        max_response_bytes=10_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "atlas_tables": [f"{SOURCE}.{DATASET}"],
            "kg": list(KG_PIDS + WIKIDATA_PIDS),
            "entities": ["cfr_cyber:operation:<id>"],
            "geo_scheme": GEO_SCHEME,
        },
        notes="Tracker page (~1.1 MB) + incidents CSV (~0.6 MB) + terms page; KG cyber_operation events.",
    )


async def atlas_ingest(run: Any, *, load_kg: bool = True) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"tables": {}}
    page = await run.get(PAGE_URL, key="page:tracker", media_types=("text/html",), max_bytes=10_000_000)
    terms = await run.get(TERMS_URL, key="page:terms", media_types=("text/html",), max_bytes=5_000_000)
    data = await run.get(
        CSV_URL, key="csv:incidents", media_types=("text/csv", "application/octet-stream"), max_bytes=10_000_000
    )
    shas = {k: run.archive(r) for k, r in (("page", page), ("terms", terms), ("csv", data))}
    facts = page_facts(page.body, terms.body)
    ops, stats = parse_csv(data.body, dc.scheme_names(GEO_SCHEME))
    frame = atlas_frame(ops, shas["csv"], facts["glossary"], facts["attribution"])
    note = (
        f"CFR Cyber Operations Tracker CSV ({stats['incidents']} incidents, {stats['first']}..{stats['last']}, "
        f"{stats['undated']} undated and not counted). Counts per suspected state sponsor x year, by Type and by "
        f"victim Category. '{facts['attribution'][:300] or ATTRIBUTION_QUOTE}'. Sponsor = CFR's attribution claim. "
        "Restricted: CFR Terms of Use (no public or commercial use)."
    )[:4000]
    reg = run.register(
        dataset=DATASET,
        frame=frame,
        raw_sha256s=sorted({shas["csv"], shas["page"]}),
        coverage_note=note,
        geo_scheme=GEO_SCHEME,
        rights=rights(facts),
    )
    out: dict[str, Any] = {
        "tables": {DATASET: {"dataset_key": reg["dataset_key"], "rows": reg["row_count"], "created": reg["created"]}},
        "csv": stats,
        "pages": {
            "tracker": shas["page"],
            "terms": shas["terms"],
            "facts": {k: v for k, v in facts.items() if k != "glossary"},
        },
    }
    if load_kg:
        ents, sts, kstats = kg_records(ops, shas["csv"], load_qid_for_iso3(), facts)
        out["kg_stats"] = kstats
        out["kg"] = dc.guarded_kg_load(
            run.engine,
            run.store,
            pids=KG_PIDS,
            wikidata_pids=WIKIDATA_PIDS,
            entities=ents,
            statements=sts,
            source_key=SOURCE_KEY,
            source_revision=f"cfr_cyber@{shas['csv'][:12]}",
            raw_sha256s=sorted({shas["csv"], shas["page"]}),
            retrieved_at=data.retrieved_at,
            origin=run.origin,
        )
    return out


__all__ = [
    "CONNECTOR_NAME",
    "CfrError",
    "Operation",
    "atlas_frame",
    "atlas_ingest",
    "atlas_source",
    "kg_records",
    "page_facts",
    "parse_csv",
]
