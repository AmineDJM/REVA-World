"""Access Now #KeepItOn STOP (Shutdown Tracker Optimization Project) -> dated internet-shutdown KG events and
country-year atlas counts (V6, agent V6DIGITAL; report ``docs/dev/v6_digital.md``).

Source (researched 2026-10-17)
------------------------------
* Methodology page ``https://www.accessnow.org/guide/shutdown-tracker-optimization-project/`` ("PUBLISHED: 15 May
  2024 LAST UPDATED: 30 March 2026"): definitions, the "Metadata" table of column descriptions (quoted per column
  from the archived page on every run) and the "DOWNLOAD OUR DATASET" link to the public Google Sheet
  ``1DvPAuHNLp5BXGb0nnZDGNoiIwEeu2ogdXEIDvT4Hyfk`` ("#KeepItOn STOP Data 2016-2025 [Public]"); tab
  "Combined (2016-2025)" (gid 1484157943) is read.
* Transport: the Sheet's ``/export?format=csv`` answers ``307`` to a per-request ``doc-*.googleusercontent.com``
  host that cannot be allow-listed, so the connector reads the Google Visualization endpoint of the same tab
  (``/gviz/tq?tqx=out:csv``, served by docs.google.com) plus its JSON column header (``tq=limit 0``). That endpoint
  types each column; a cell whose text does not fit a column typed ``date``/``number`` (e.g. duration "Curfew
  Style", end_date "April/2017 following ...", every non-numeric econ_impact) is served **empty**. The typed
  columns are recorded on every run; an empty cell in one of them is read as "blank, or text not served by the
  endpoint" (typed Missing), never zero; econ_impact (mixed text) is not loaded.
* Licence: **no dataset-specific licence**. Access Now's "Trademarks, copyright, and media usage" page: "Any and
  all original Media Material on the Access Now website may be freely licensed and distributed at will under the
  Creative Commons Attribution (CC BY) 4.0 International License, unless otherwise noted" -- it defines Media
  Material as "images, symbols, icons, graphics, and logos"; the site Terms of Use otherwise permit "personal,
  non-commercial use only". Recorded as *not an open licence* (restricted; redistribution and commercial use not
  granted) pending an owner request to Access Now.

What a record is
----------------
"A shutdown instance refers to any disruption event lasting longer than one hour, or a series of disruption events
that we attribute to the same or similar circumstances, justifications, methods, and perpetrators." STOP counts
an ongoing shutdown again in every calendar year it is active (column ``count_year``; the 2016 rows include
shutdowns that started earlier). The sheet publishes **no identifier**: REVA keys an instance by the verbatim
values of :data:`KEY_COLUMNS` (rows of different ``count_year`` with identical key values are the same instance,
listed with all their count years; two identical rows in one count year stay two instances, numbered in sheet
order). Keys can change when Access Now edits a row (documented limitation).

Outputs
-------
* atlas ``keepiton.shutdowns_cy``: exact counts of STOP rows per country x ``count_year`` (STOP's own counting),
  ``SHUTDOWN_INSTANCES`` and the breakdowns ``SHUTDOWN_INSTANCES|<column>=<slug>`` for shutdown_type,
  affected_network, shutdown_extent, ordered_by, actual_cause (label verbatim in flags). A row naming several
  countries ("Laos; Myanmar") counts for each (flagged). Non-zero cells only: an absent cell means "no STOP
  instance recorded", not a measured zero.
* atlas ``keepiton.shutdowns``: per instance and country, ``DURATION_DAYS|instance=<id>`` where duration is numeric
  (period = start_date .. end_date inclusive, else the start day), coded fields in flags.
* KG (source ``accessnow.stop``): ``keepiton:shutdown:<id>`` (kind ``internet_shutdown``): P580 start_date, P582
  end_date, P17 country (reviewed country QID via the exact-name scheme ``keepiton_country``, else ``iso3:<code>``;
  a name that is not a crosswalk country, e.g. Somaliland, stays a coded text field), the coded columns,
  ``keepiton:service_affected`` -> ``keepiton:service:<column>`` and source links.

Nothing is causal: ``actual_cause`` is Access Now's category of the circumstances; ``ordered_by`` is its attribution.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from reva.adapters.connectors import digital_common as dc
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement, load_qid_for_iso3
from reva.adapters.connectors.vintage_common import Rows
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.keepiton"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_keepiton/1"
SOURCE_KEY = "accessnow.stop"
SOURCE = "keepiton"
DATASETS = ("shutdowns_cy", "shutdowns")
GEO_SCHEME = "keepiton_country"
SHEET_ID = "1DvPAuHNLp5BXGb0nnZDGNoiIwEeu2ogdXEIDvT4Hyfk"
GID = "1484157943"
TAB = "Combined (2016-2025)"
GVIZ = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq"
CSV_URL = f"{GVIZ}?tqx=out:csv&gid={GID}"
HEADER_URL = f"{GVIZ}?tqx=out:json&gid={GID}&tq=limit%200"
METHOD_URL = "https://www.accessnow.org/guide/shutdown-tracker-optimization-project/"
MEDIA_URL = "https://www.accessnow.org/trademarks-copyright-and-media-usage/"
HOSTS = ("docs.google.com", "www.accessnow.org")
HEADER = (
    "start_date_type", "start_date", "country", "geo_scope", "area_name", "shutdown_type", "affected_network",
    "shutdown_extent", "ordered_by", "decision_maker", "actual_cause", "actual_cause_details", "info_source",
    "info_source_link", "shutdown_status", "end_date", "duration", "gov_justification", "gov_just_details", "gov_ack",
    "legal_just", "legal_method", "gov_ack_source", "gov_ack_quote", "facebook_affected", "twitter_affected",
    "whatsapp_affected", "instagram_affected", "telegram_affected", "other_affected", "sms_affected",
    "phonecall_affected", "telcos_involved", "telco_ack", "telco_ack_source", "telco_ack_quote", "election",
    "violence", "hr_abuse_reported", "users_targeted", "users_target_detail", "users_notified", "econ_impact",
    "event", "an_link", "region", "count_year",
)  # fmt: skip
KEY_COLUMNS = (
    "start_date", "country", "area_name", "shutdown_type", "affected_network", "shutdown_extent", "ordered_by",
    "actual_cause", "end_date",
)  # fmt: skip
BREAKDOWNS = ("shutdown_type", "affected_network", "shutdown_extent", "ordered_by", "actual_cause")
SPECIFIC = {
    "shutdown_type": "keepiton:shutdown_type",
    "affected_network": "keepiton:affected_network",
    "shutdown_extent": "keepiton:shutdown_extent",
    "ordered_by": "keepiton:ordered_by",
    "actual_cause": "keepiton:actual_cause",
    "gov_justification": "keepiton:gov_justification",
    "shutdown_status": "keepiton:shutdown_status",
}
#: STOP columns <service>_affected -> service label (the provider's column; "Yes" links the instance)
SERVICES = {
    "facebook_affected": "Facebook",
    "twitter_affected": "X (formerly Twitter)",
    "whatsapp_affected": "WhatsApp",
    "instagram_affected": "Instagram",
    "telegram_affected": "Telegram",
    "sms_affected": "SMS / text messaging",
    "phonecall_affected": "Phone calls",
}
GENERIC = (
    "start_date_type", "geo_scope", "area_name", "decision_maker", "actual_cause_details", "info_source",
    "gov_just_details", "gov_ack", "legal_just", "legal_method", "gov_ack_quote", "other_affected", "telcos_involved",
    "telco_ack", "telco_ack_quote", "election", "violence", "hr_abuse_reported", "users_targeted",
    "users_target_detail", "users_notified", "event", "region",
)  # fmt: skip
URL_COLUMNS = ("info_source_link", "an_link", "gov_ack_source", "telco_ack_source")
NOT_LOADED = {"econ_impact": "mixed text/number column: the visualization endpoint drops its text cells"}
KG_PIDS = (*SPECIFIC.values(), "keepiton:duration_days", "keepiton:coded_field", "keepiton:service_affected",
           "keepiton:source_url")  # fmt: skip
WIKIDATA_PIDS = ("P17", "P580", "P582")
LICENCE = (
    "No dataset licence stated (Access Now STOP public Google Sheet); Access Now media-usage CC BY 4.0 covers "
    "'Media Material' only and the site Terms of Use allow personal, non-commercial use: not an open licence; "
    "redistribution and commercial use not granted"
)
_MDY = re.compile(r"^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})$")
_NUM = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


class StopError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


@dataclass
class Instance:
    key: str
    rows: list[int]
    count_years: list[str]
    values: dict[str, str]
    countries: list[str] = field(default_factory=list)

    @property
    def node(self) -> str:
        return f"keepiton:shutdown:{self.key}"


# --------------------------------------------------------------------------- pages
def page_facts(method_html: bytes, media_html: bytes | None = None) -> dict[str, Any]:
    """Definitions and statements quoted verbatim from the archived methodology (and media-usage) pages."""
    t = dc.page_text(method_html)
    facts: dict[str, Any] = {
        "updated": (re.search(r"PUBLISHED: [^|]{0,40}? LAST UPDATED: [0-9]{1,2} [A-Z][a-z]+ [0-9]{4}", t) or [""])[0],
        "coverage": dc.quote_between(t, "To date, the tracker contains data from", (" While we try",)),
        "shutdown_definition": dc.quote_between(t, "An internet shutdown is an intentional", (" STOP was designed",)),
        "instance_definition": dc.quote_between(t, "A shutdown instance refers to", (" Depending on the",)),
        "columns": column_definitions(t),
    }
    if media_html is not None:
        m = dc.page_text(media_html)
        facts["media_licence"] = dc.quote_between(
            m, "Any and all original Media Material", (" All Media Material that is not",)
        )
    return facts


def column_definitions(text: str) -> dict[str, str]:
    """``{column: description}`` from the page's "Metadata" table (columns in sheet order; '' when not described)."""
    anchor = "Column name Column description"
    i = text.find(anchor)
    if i < 0:
        return {}
    m = text[i + len(anchor) :]
    locs: list[tuple[str, int, int]] = []
    pos = 0
    for c in HEADER:
        hit = re.compile(rf"(?:^| ){re.escape(c)} (?=[A-Z])").search(m, pos)
        if hit is None:
            continue
        locs.append((c, hit.start(), hit.end()))
        pos = hit.end()
    out = {c: "" for c in HEADER}
    for (c, _, e), nxt in zip(locs, [*locs[1:], None], strict=True):
        end = nxt[1] if nxt else min(len(m), e + 600)
        out[c] = m[e:end].strip()[:1200]
    return out


def gviz_types(body: bytes) -> dict[str, str]:
    """``{label: type}`` of the visualization endpoint's JSON header (``google.visualization.Query.setResponse(..)``)."""
    t = body.decode("utf-8", errors="replace")
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise StopError("visualization header is not a JSON response")
    doc = json.loads(t[a : b + 1])
    if doc.get("status") != "ok":
        raise StopError(f"visualization header status {doc.get('status')!r}")
    return {str(c.get("label")): str(c.get("type")) for c in doc["table"]["cols"]}


# --------------------------------------------------------------------------- sheet
def mdy(text: str) -> date | None:
    s = text.strip()
    if not s:
        return None
    m = _MDY.match(s)
    if not m:
        raise StopError(f"date cell {s[:30]!r} is not M/D/YYYY")
    return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))


def parse_sheet(body: bytes, known: set[str]) -> tuple[list[Instance], dict[str, Any]]:
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    if not rows or tuple(h.strip() for h in rows[0]) != HEADER:
        raise StopError(f"header changed: {rows[0][:8] if rows else None}")
    groups: dict[tuple[str, ...], list[tuple[int, dict[str, str]]]] = defaultdict(list)
    n = 0
    for rnum, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        if len(r) != len(HEADER):
            raise StopError(f"row {rnum}: {len(r)} cells")
        v = {h: c.strip() for h, c in zip(HEADER, r, strict=True)}
        if not re.match(r"^20[0-9]{2}$", v["count_year"]):
            raise StopError(f"row {rnum}: count_year {v['count_year']!r}")
        mdy(v["start_date"])
        mdy(v["end_date"])
        groups[tuple(v[k] for k in KEY_COLUMNS)].append((rnum, v))
        n += 1
    out: list[Instance] = []
    for k, members in groups.items():
        digest = hashlib.sha256("\x1f".join(k).encode()).hexdigest()[:16]
        by_year: dict[str, list[tuple[int, dict[str, str]]]] = defaultdict(list)
        for rnum, v in members:
            by_year[v["count_year"]].append((rnum, v))
        copies = max(len(x) for x in by_year.values())
        for i in range(copies):
            part = [(yr, lst[i]) for yr, lst in sorted(by_year.items()) if i < len(lst)]
            latest = part[-1][1][1]
            inst = Instance(
                key=digest if i == 0 else f"{digest}-{i + 1}",
                rows=[p[1][0] for p in part],
                count_years=[p[0] for p in part],
                values=latest,
                countries=dc.split_countries(latest["country"], known, seps=(";",)),
            )
            out.append(inst)
    stats = {
        "rows": n,
        "instances": len(out),
        "multi_year_instances": sum(len(i.count_years) > 1 for i in out),
        "multi_country_instances": sum(len(i.countries) > 1 for i in out),
        "count_years": dict(sorted(Counter(y for i in out for y in i.count_years).items())),
    }
    if not out:
        raise StopError("no shutdown instance")
    return sorted(out, key=lambda i: (i.values["start_date"][-4:], i.key)), stats


# --------------------------------------------------------------------------- atlas
def atlas_frames(insts: list[Instance], sha: str, typed: dict[str, str], defs: dict[str, str]) -> dict[str, Any]:
    cy = Rows()
    counts: Counter[tuple[str, str, str]] = Counter()
    labels: dict[str, str] = {}
    multi: set[tuple[str, str]] = set()
    for inst in insts:
        for yr in inst.count_years:
            for ctry in inst.countries:
                counts[(ctry, yr, "SHUTDOWN_INSTANCES")] += 1
                if len(inst.countries) > 1:
                    multi.add((ctry, yr))
                for col in BREAKDOWNS:
                    lab = inst.values[col] or "(blank)"
                    ind = f"SHUTDOWN_INSTANCES|{col}={dc.slug(lab.replace('(blank)', 'blank'))}"
                    labels[ind] = f"{col} = {lab}"
                    counts[(ctry, yr, ind)] += 1
    for (ctry, yr, ind), n in sorted(counts.items()):
        y = int(yr)
        cy.add(
            source=SOURCE,
            dataset="shutdowns_cy",
            indicator=ind,
            provider_geo=ctry,
            start=date(y, 1, 1),
            end=date(y + 1, 1, 1),
            value=str(n),
            unit="count of STOP shutdown instances",
            flags=dc.canon(
                {
                    "derived": "count",
                    "counting": "STOP count_year: an ongoing shutdown is counted in every calendar year it is active",
                    "breakdown": labels.get(ind, "all instances"),
                    "multi_country_rows_included": (ctry, yr) in multi,
                    "absent_cell": "no STOP instance recorded (not a measured zero)",
                }
            ),
            raw_sha256=sha,
            selector={"tab": TAB, "group_by": ["country", "count_year"], "indicator": ind},
            iso3=country_iso3(ctry, GEO_SCHEME),
        )
    inst_rows = Rows()
    for inst in insts:
        v = inst.values
        dur = v["duration"]
        if not _NUM.match(dur):
            continue
        start, end = mdy(v["start_date"]), mdy(v["end_date"])
        if start is None:
            continue
        p1, basis = (end + timedelta(days=1), "start_date .. end_date") if end and end >= start else (
            start + timedelta(days=1), "start day (no usable end_date)")  # fmt: skip
        for ctry in inst.countries:
            inst_rows.add(
                source=SOURCE,
                dataset="shutdowns",
                indicator=f"DURATION_DAYS|instance={inst.key}",
                provider_geo=ctry,
                start=start,
                end=p1,
                value=dur,
                unit="days",
                flags=dc.canon(
                    {
                        "definition": defs.get("duration", ""),
                        "instance": inst.node,
                        "count_years": inst.count_years,
                        "period_basis": basis,
                        **{c: v[c] for c in (*BREAKDOWNS, "shutdown_status", "area_name")},
                        "typed_by_endpoint": typed.get("duration"),
                    }
                ),
                raw_sha256=sha,
                selector={"tab": TAB, "rows": inst.rows, "column": "duration"},
                iso3=country_iso3(ctry, GEO_SCHEME),
            )
    return {"shutdowns_cy": cy.frame(), "shutdowns": inst_rows.frame()}


# --------------------------------------------------------------------------- KG
def _t(d: date) -> dict[str, Any]:
    return {"type": "time", "time": d.isoformat(), "precision": 11}


def _s(v: str) -> dict[str, Any]:
    return {"type": "string", "value": v[:4000]}


def kg_records(
    insts: list[Instance], sha: str, qids: dict[str, str], defs: dict[str, str], typed: dict[str, str]
) -> tuple[list[KgEntity], list[KgStatement], dict[str, Any]]:
    ents: list[KgEntity] = [
        KgEntity(
            qid=f"keepiton:service:{col.removesuffix('_affected')}",
            kind="communication_service",
            labels={"en": f"{lab} (service column of Access Now STOP)"},
            descriptions={"en": f"STOP column {col}: {defs.get(col, '')[:300]}"},
        )
        for col, lab in SERVICES.items()
    ]
    sts: list[KgStatement] = []
    stats: Counter[str] = Counter()
    blank_typed = sorted(c for c, t in typed.items() if t in ("date", "number", "datetime"))
    for inst in insts:
        v = inst.values
        start, end = mdy(v["start_date"]), mdy(v["end_date"])
        when = start.isoformat() if start else "undated"
        ents.append(
            KgEntity(
                qid=inst.node,
                kind="internet_shutdown",
                labels={"en": f"Internet shutdown in {v['country']} ({v['area_name'][:80] or 'area not stated'}), "
                        f"from {when} (Access Now STOP)"},  # fmt: skip
                descriptions={"en": (v["event"] or v["actual_cause_details"] or "STOP shutdown instance")[:250]},
            )
        )
        sel = {"tab": TAB, "rows": inst.rows}

        def add(key: str, prop: str, *, _i: Instance = inst, _sel: dict[str, Any] = sel, **kw: Any) -> None:
            sts.append(KgStatement(key=f"{_i.key}:{key}", subject=_i.node, property=prop, raw_sha256=sha,
                                   selector=_sel, **kw))  # fmt: skip

        for ctry in inst.countries:
            iso = country_iso3(ctry, GEO_SCHEME)
            if iso is None:
                stats["country_unmapped"] += 1
                add(f"country_text:{dc.slug(ctry)}", "keepiton:coded_field", value=_s(ctry),
                    qualifiers={"column": "country", "definition": defs.get("country", "")[:500],
                                "note": "not a crosswalk country: no P17 edge"})  # fmt: skip
                continue
            stats["country_qid" if iso in qids else "country_iso3_node"] += 1
            add(f"P17:{iso}", "P17", object_qid=qids.get(iso) or f"iso3:{iso}",
                qualifiers={"country_as_stated": v["country"], "iso3": iso})
        if start:
            add("start", "P580", value=_t(start), qualifiers={"column": "start_date",
                                                                "start_date_type": v["start_date_type"] or None})
        if end:
            add("end", "P582", value=_t(end), qualifiers={"column": "end_date"})
        elif "end_date" in blank_typed:
            stats["end_blank_or_text_not_served"] += 1
        for col, prop in SPECIFIC.items():
            if v[col]:
                add(col, prop, value=_s(v[col]), qualifiers={"definition": defs.get(col, "")[:500]})
        if _NUM.match(v["duration"]):
            add("duration", "keepiton:duration_days", value={"type": "quantity", "amount": v["duration"], "unit": "day"},
                qualifiers={"definition": defs.get("duration", "")[:500]})  # fmt: skip
        for col in GENERIC:
            if v[col]:
                q2: dict[str, Any] = {"column": col, "definition": defs.get(col, "")[:500] or "not described"}
                if len(v[col]) > 4000:
                    q2["truncated"] = True
                add(f"f:{col}", "keepiton:coded_field", value=_s(v[col]), qualifiers=q2)
        add("count_years", "keepiton:coded_field", value=_s(", ".join(inst.count_years)),
            qualifiers={"column": "count_year", "definition": "calendar years in which STOP counts the instance"})
        for col in SERVICES:
            if v[col] == "Yes":
                stats["service_links"] += 1
                add(f"svc:{col}", "keepiton:service_affected",
                    object_qid=f"keepiton:service:{col.removesuffix('_affected')}",
                    qualifiers={"column": col, "definition": defs.get(col, "")[:500]})  # fmt: skip
        for col in URL_COLUMNS:
            for j, url in enumerate(u.strip() for u in v[col].split(";") if u.strip()):
                add(f"url:{col}:{j}", "keepiton:source_url", value=_s(url), qualifiers={"column": col})
    stats["entities"] = len(ents)
    stats["statements"] = len(sts)
    return ents, sts, dict(stats)


# --------------------------------------------------------------------------- spec / ingest
def rights(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "licence": LICENCE,
        "licence_url": MEDIA_URL,
        "terms_url": "https://www.accessnow.org/access-nows-website-terms-of-use/",
        "attribution": "Access Now, #KeepItOn STOP (Shutdown Tracker Optimization Project) data 2016-2025",
        "provider": "Access Now (#KeepItOn coalition)",
        "commercial_use": "not_granted",
        "redistribution": "not_granted",
        "open_licence": False,
        "restriction": "no dataset licence; personal, non-commercial use under the site Terms of Use",
        "media_usage_statement": facts.get("media_licence") or None,
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
        provider="Access Now, #KeepItOn Shutdown Tracker Optimization Project (STOP)",
        licence=LICENCE,
        allowed_hosts=HOSTS,
        datasets=DATASETS,
        dataset_ids=(f"{SHEET_ID}#gid={GID}",),
        cadence_seconds=90 * 86_400,
        planned_requests=4,
        max_response_bytes=20_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "atlas_tables": [f"{SOURCE}.{d}" for d in DATASETS],
            "kg": list(KG_PIDS + WIKIDATA_PIDS),
            "entities": ["keepiton:shutdown:<id>", "keepiton:service:<column>"],
            "geo_scheme": GEO_SCHEME,
        },
        notes="Methodology + media-usage pages, gviz header + CSV of the combined tab (~2.2 MB); KG internet_shutdown events.",
    )


async def atlas_ingest(run: Any, *, load_kg: bool = True) -> dict[str, Any]:
    if not any(run.wants(d) for d in DATASETS):
        return {"tables": {}}
    meth = await run.get(METHOD_URL, key="page:methodology", media_types=("text/html",), max_bytes=5_000_000)
    media = await run.get(MEDIA_URL, key="page:media_usage", media_types=("text/html",), max_bytes=5_000_000)
    head = await run.get(HEADER_URL, key="gviz:header", media_types=("application/javascript", "application/json",
                         "text/javascript"), max_bytes=1_000_000)  # fmt: skip
    data = await run.get(CSV_URL, key="gviz:csv", media_types=("text/csv",), max_bytes=20_000_000)
    shas = {k: run.archive(r) for k, r in (("method", meth), ("media", media), ("header", head), ("csv", data))}
    facts = page_facts(meth.body, media.body)
    typed = gviz_types(head.body)
    if tuple(typed) != HEADER:
        raise StopError(f"visualization header columns changed: {list(typed)[:8]}")
    defs = facts["columns"]
    known = dc.scheme_names(GEO_SCHEME)
    insts, stats = parse_sheet(data.body, known)
    frames = atlas_frames(insts, shas["csv"], typed, defs)
    typed_cols = {c: t for c, t in typed.items() if t != "string"}
    note_common = (
        f"Access Now #KeepItOn STOP, public sheet tab '{TAB}' read through the Google Visualization endpoint "
        f"({stats['rows']} rows, {stats['instances']} instances; {facts.get('coverage', '')[:120]}). Columns typed "
        f"by the endpoint {typed_cols}: their text cells are served empty (typed Missing, never zero). "
    )
    out: dict[str, Any] = {"tables": {}, "sheet": stats, "typed_columns": typed_cols, "pages": {
        "methodology": {"sha256": shas["method"], "updated": facts["updated"], "columns_described":
                        sum(bool(x) for x in defs.values())},
        "media_usage": {"sha256": shas["media"], "statement": facts.get("media_licence")}}}  # fmt: skip
    notes = {
        "shutdowns_cy": "Exact counts of STOP rows per country x count_year (STOP counts an ongoing shutdown in "
        "every year it is active); breakdowns by shutdown_type, affected_network, shutdown_extent, ordered_by, "
        "actual_cause. Non-zero cells only.",
        "shutdowns": "Duration in days per instance and country where STOP's duration is numeric; period = "
        "start_date..end_date.",
    }
    for d in DATASETS:
        if not run.wants(d):
            continue
        reg = run.register(
            dataset=d,
            frame=frames[d],
            raw_sha256s=sorted({shas["csv"], shas["header"], shas["method"]}),
            coverage_note=(note_common + notes[d])[:4000],
            geo_scheme=GEO_SCHEME,
            rights=rights(facts),
        )
        out["tables"][d] = {"dataset_key": reg["dataset_key"], "rows": reg["row_count"], "created": reg["created"]}
    if load_kg:
        ents, sts, kstats = kg_records(insts, shas["csv"], load_qid_for_iso3(), defs, typed)
        out["kg_stats"] = kstats
        out["kg"] = dc.guarded_kg_load(
            run.engine,
            run.store,
            pids=KG_PIDS,
            wikidata_pids=WIKIDATA_PIDS,
            entities=ents,
            statements=sts,
            source_key=SOURCE_KEY,
            source_revision=f"stop@{shas['csv'][:12]}",
            raw_sha256s=sorted({shas["csv"], shas["method"]}),
            retrieved_at=data.retrieved_at,
            origin=run.origin,
        )
    return out


__all__ = [
    "CONNECTOR_NAME",
    "Instance",
    "StopError",
    "atlas_frames",
    "atlas_ingest",
    "atlas_source",
    "column_definitions",
    "gviz_types",
    "kg_records",
    "mdy",
    "page_facts",
    "parse_sheet",
]
