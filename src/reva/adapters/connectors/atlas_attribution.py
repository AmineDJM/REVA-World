"""Extreme-event attribution studies (Carbon Brief map) -> KG extremes, attributed T2 causal claims, atlas counts.

Agent V4CLIMATE. Atlas source ``atlas.attribution_cb`` (source key ``carbonbrief.attribution``).

Source and licence
------------------
Carbon Brief, "Mapped: How climate change affects extreme weather around the world" (interactive.carbonbrief.org/
attribution-studies/), CSV ``data/papers-download.csv`` ("Get the data (.csv)"): one row per (extreme event or trend,
study) with Carbon Brief's coded ``Classification`` of the study's finding, the study ``Citation`` and ``Link``, the
``Summary statement`` (a quote from the study) and ISO alpha-3 country codes. The page (archived on every run) says:
"Published under a CC license. You are welcome to reproduce unadapted material in full for non-commercial use,
credited 'Carbon Brief' with a link to the article" and links CC BY-NC-ND 4.0. Every value is therefore flagged
**non-commercial / no-derivatives** (owner decision recorded in docs/dev/v4_v4climate.md); the licence marker is checked
at each run (a change is a rights event: the run is refused). The page's "Last updated <date>" is the revision.

What REVA records (and does not)
--------------------------------
* KG (source key ``carbonbrief.attribution``): one entity per distinct ``Name`` (the source's own label, exact text,
  this source only) -- ``cbattr:extreme:<h>`` of kind ``extreme_event`` (typed as a disaster event by
  ``registry/events/taxonomy.yaml``) or, when every row of the Name says ``Trend``, ``cbattr:trend:<h>`` of kind
  ``climate_trend`` (never an event); its event type (``cbattr:event_type``), period (``P585`` year or ``P580``/``P582`` years when the field
  is ``YYYY`` / ``YYYY-YY(YY)``; any other text kept verbatim in ``cbattr:event_period_text``), countries
  (``cbattr:country`` -> country QID, from the ISO3 codes the source publishes; family ``located_in``) and one
  ``cbattr:finding`` per study row: the classification verbatim with the citation, link, rapid-study flag and summary
  statement as qualifiers. Findings are descriptive records of what each study concluded.
* Causal claims (``reva.causal.load_dataset_claims``, tier **T2**: a scholarly reference with its DOI stored
  verbatim; Carbon Brief is the compiler, the *study* is the asserting reference). Cause: the entity
  ``cbattr:human_caused_climate_change``; effect: the extreme/trend entity. Classification -> claim:

  ===================================================  =======================================================
  "More severe or more likely to occur"                ``contributing_factor``, polarity ``promotes``
  "Decrease, less severe or less likely to occur"      ``contributing_factor``, polarity ``inhibits``
  "No discernible human influence"                     ``contributing_factor``, stance ``denies``
  "Insufficient data/inconclusive"                     no claim (the finding stays in the KG only)
  ===================================================  =======================================================

  The DOI is read from the ``Citation`` text, else from a ``Link`` that is a DOI URL or a publisher ``/doi/`` path
  (``scope.doi_basis`` says which). Rows without any DOI (most World Weather Attribution rapid studies, whose links
  are web pages) give **no claim** -- T2 requires a DOI or ISBN; they are counted in the report. The study's summary
  statement is the claim ``mechanism`` (verbatim). Nothing is extracted from free text; IPCC statements are not used.
* Atlas ``attribution_cb.findings``: per country (ISO3 as published) and single event year, the number of study rows by
  classification (``FINDINGS|class=<slug>``) -- derived counts, rows only for counts >= 1. Trends and multi-year
  events are not in the atlas (no single period); they are in the KG.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from reva.adapters.connectors import atlas_climate_common as cc
from reva.adapters.connectors import atlas_infra_common as ic
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.attribution_cb"
VERSION = "1.0.0"
SOURCE_KEY = "carbonbrief.attribution"
ATLAS_SOURCE = "attribution_cb"
DATASET = "findings"
CAUSAL_DATASET = "carbonbrief.attribution_studies"
PARSER_VERSION = "atlas-attribution-cb/1"
HOST = "interactive.carbonbrief.org"
PAGE_URL = f"https://{HOST}/attribution-studies/index.html"
CSV_URL = f"https://{HOST}/attribution-studies/data/papers-download.csv"
MAX_BYTES = 8_000_000
MIN_ROWS = 500
LICENCE_MARKERS = ("Published under a CC license", "creativecommons.org/licenses/by-nc-nd/4.0")
RIGHTS = {
    "licence": "CC BY-NC-ND 4.0 (Carbon Brief): non-commercial, no derivatives",
    "licence_url": "https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode",
    "non_commercial": "true",
    "no_derivatives": "true",
    "terms_quote": "Published under a CC license. You are welcome to reproduce unadapted material in full for "
    "non-commercial use, credited 'Carbon Brief' with a link to the article. Please contact us for commercial use.",
    "attribution": "Carbon Brief, 'Mapped: How climate change affects extreme weather around the world' "
    "(McSweeney, Tandon et al.), interactive.carbonbrief.org/attribution-studies",
}
COLUMNS = (
    "Name",
    "Event year/Trend",
    "Event type",
    "Classification",
    "Publication year",
    "Citation",
    "Rapid study?",
    "Source",
    "Link",
    "Summary statement",
    "cb-region",
    "iso country code",
    "iso sub region",
)
CLASSIFICATIONS: dict[str, tuple[str | None, str | None, str]] = {
    # classification verbatim -> (polarity, stance or None for no claim, slug)
    "More severe or more likely to occur": ("promotes", "asserts", "more_severe_or_likely"),
    "Decrease, less severe or less likely to occur": ("inhibits", "asserts", "less_severe_or_likely"),
    "No discernible human influence": (None, "denies", "no_discernible_influence"),
    "Insufficient data/inconclusive": (None, None, "inconclusive"),
}
CAUSE_ID = "cbattr:human_caused_climate_change"
CAUSE_QUOTE = "They calculated the specific contribution that human-caused climate change made to an individual extreme weather event."
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;]+")
KG_PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "cbattr:event_type": (
        "string",
        "cbattr_event_type",
        "extreme event type (Carbon Brief)",
        "type d'événement extrême (Carbon Brief)",
    ),
    "cbattr:event_period_text": (
        "string",
        "cbattr_event_period_text",
        "event year or trend (verbatim)",
        "année de l'événement ou tendance (verbatim)",
    ),
    "cbattr:country": (
        "item",
        "cbattr_country",
        "country of the extreme (Carbon Brief ISO3 code)",
        "pays de l'extrême (code ISO3 Carbon Brief)",
    ),
    "cbattr:region": ("string", "cbattr_region", "Carbon Brief region", "région Carbon Brief"),
    "cbattr:finding": (
        "string",
        "cbattr_finding",
        "attribution study finding (Carbon Brief classification)",
        "conclusion d'une étude d'attribution (classification Carbon Brief)",
    ),
    "cbattr:definition": ("string", "cbattr_definition", "definition (verbatim)", "définition (verbatim)"),
}
WIKIDATA_PIDS = ("P585", "P580", "P582")


class AttributionError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> AttributionError:
    return AttributionError(msg, retryable=False, kind=kind)


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__], cc)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Carbon Brief (attribution studies map)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(CSV_URL,),
        cadence_seconds=30 * ic.DAY,
        planned_requests=2,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="extreme-event attribution findings: KG extremes + T2 claims (DOI) + per-country-year counts",
    )


# --------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class Row:
    line: int
    cells: dict[str, str]
    key: str  # sha of the whole row (source claim id)

    def __getitem__(self, k: str) -> str:
        return self.cells[k]


def page_meta(body: bytes) -> dict[str, str]:
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace")))
    raw = body.decode("utf-8", "replace")
    missing = [m for m in LICENCE_MARKERS if m not in t and m not in raw]
    if missing:
        raise _err(f"Carbon Brief page no longer states {missing}: licence changed?", kind="licence")
    m = re.search(r"Last updated:? (\d{1,2} \w+ \d{4})", t)
    if not m:
        raise _err("Carbon Brief page: 'Last updated' date not found")
    if CAUSE_QUOTE not in t:
        raise _err("Carbon Brief page: the human-caused climate change sentence changed")
    return {"last_updated": m.group(1)}


def parse_csv(body: bytes, *, min_rows: int = MIN_ROWS) -> list[Row]:
    text = body.decode("utf-8-sig")
    rdr = csv.DictReader(io.StringIO(text))
    if tuple(rdr.fieldnames or ()) != COLUMNS:
        raise _err(f"CSV columns changed: {rdr.fieldnames}")
    out = []
    for i, r in enumerate(rdr, start=2):
        cells = {k: (r.get(k) or "").strip() for k in COLUMNS}
        if cells["Classification"] not in CLASSIFICATIONS:
            raise _err(f"line {i}: unknown classification {cells['Classification']!r}")
        if not cells["Name"]:
            raise _err(f"line {i}: empty Name")
        key = hashlib.sha256("\x1f".join(cells[k] for k in COLUMNS).encode()).hexdigest()[:24]
        out.append(Row(i, cells, key))
    if len(out) < min_rows:
        raise _err(f"only {len(out)} rows (truncated?)", kind="incomplete")
    return out


def event_id(name: str, trend: bool = False) -> str:
    """``cbattr:extreme:<h>`` (an event: typed by registry/events/taxonomy.yaml) or ``cbattr:trend:<h>`` (a trend:
    never an event); ``h`` = sha256 of the source's exact ``Name``."""
    return ("cbattr:trend:" if trend else "cbattr:extreme:") + hashlib.sha256(name.encode()).hexdigest()[:16]


def ids_by_name(rows: list[Row]) -> dict[str, str]:
    """A Name whose every row says ``Trend`` is a trend; otherwise an extreme event."""
    by: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by[r["Name"]].append(r)
    return {n: event_id(n, all(r["Event year/Trend"] == "Trend" for r in rs)) for n, rs in by.items()}


def period(text: str) -> tuple[int, int] | None:
    """``2010`` -> (2010, 2010); ``2013-14`` / ``2008–16`` / ``1999-2000`` -> (start, end); anything else None."""
    t = text.strip()
    if re.fullmatch(r"\d{4}", t):
        return int(t), int(t)
    m = re.fullmatch(r"(\d{4})\s*[-–]\s*(\d{2}|\d{4})", t)
    if m:
        a = int(m.group(1))
        b = int(m.group(2)) if len(m.group(2)) == 4 else (a // 100) * 100 + int(m.group(2))
        if b < a:
            b += 100
        return a, b
    return None


def doi_of(row: Row) -> tuple[str | None, str | None]:
    m = DOI_RE.search(row["Citation"])
    if m:
        return m.group(0).rstrip(".)"), "citation"
    link = row["Link"]
    if "doi.org/" in link or "/doi/" in link:
        m = DOI_RE.search(link)
        if m:
            return m.group(0).rstrip(".)"), "link"
    return None, None


def countries(row: Row) -> list[str]:
    return [c.strip() for c in row["iso country code"].split(",") if c.strip()]


# --------------------------------------------------------------------------- builders
def atlas_counts(rows: list[Row], sha: str) -> tuple[ic.RowBuilder, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    rb = ic.RowBuilder(ATLAS_SOURCE, DATASET)
    cnt: Counter[tuple[str, int, str]] = Counter()
    for r in rows:
        p = period(r["Event year/Trend"])
        if p is None or p[0] != p[1]:
            continue
        for c in countries(r):
            cnt[(c, p[0], CLASSIFICATIONS[r["Classification"]][2])] += 1
    unknown = sorted({c for (c, _, _) in cnt if country_iso3(c, "iso3") is None})
    for (c, y, slug), n in sorted(cnt.items()):
        rb.count(
            indicator=f"FINDINGS|class={slug}",
            provider_geo=c,
            iso3=country_iso3(c, "iso3"),
            period=ic.year_span(y),
            value=n,
            unit="study findings",
            raw_sha256=sha,
            selector={"file": "papers-download.csv", "aggregate": "count",
                      "where": {"iso country code contains": c, "Event year/Trend": str(y), "class": slug}},
            flags=ic.derived("count of CSV rows (one per extreme x study) whose single-year 'Event year/Trend' is the "
                             "year, whose 'iso country code' list contains the country and whose Classification is "
                             "the class", licence=RIGHTS["licence"], non_commercial=True, no_derivatives=True),
        )  # fmt: skip
    return rb, {"atlas_cells": len(cnt), "unknown_iso3": unknown}


def kg_items(rows: list[Row], csv_f: Any, page_f: Any, meta: Mapping[str, str], country_qids: Mapping[str, str]) -> tuple[list[Any], list[Any], dict[str, Any]]:  # fmt: skip
    from reva.atlas.tables import country_iso3
    from reva.kg.model import KgEntity, KgStatement

    rev = meta["last_updated"]
    ref = [cc.kg_ref(file="papers-download.csv", url=CSV_URL, raw_sha256=csv_f.sha256,
                     retrieved_at=csv_f.retrieved_at, licence=RIGHTS["licence"])]  # fmt: skip
    ents: list[Any] = [
        KgEntity(
            qid=CAUSE_ID,
            kind="climate_driver",
            labels={"en": "human-caused climate change", "fr": "changement climatique d'origine humaine"},
            descriptions={"en": "as named by Carbon Brief's attribution map (cause side of attribution findings)"},
            source_revision=rev,
        )
    ]
    sts: list[Any] = [
        KgStatement(
            subject_qid=CAUSE_ID,
            property_pid="cbattr:definition",
            value={"type": "string", "value": CAUSE_QUOTE},
            references=[
                cc.kg_ref(
                    file="index.html",
                    url=PAGE_URL,
                    raw_sha256=page_f.sha256,
                    retrieved_at=page_f.retrieved_at,
                    licence=RIGHTS["licence"],
                )
            ],
            source_key=SOURCE_KEY,
            source_revision=rev,
            statement_id="cbattr:cause:definition",
            raw_sha256=page_f.sha256,
        )
    ]
    by_name: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_name[r["Name"]].append(r)
    rep: Counter[str] = Counter()

    def st(eid: str, pid: str, sid: str, *, obj: str | None = None, value: dict[str, Any] | None = None,
           quals: dict[str, Any] | None = None, **kw: Any) -> None:  # fmt: skip
        sts.append(KgStatement(subject_qid=eid, property_pid=pid, object_qid=obj, value=value, qualifiers=quals or {},
                               references=ref, source_key=SOURCE_KEY, source_revision=rev, statement_id=sid,
                               raw_sha256=csv_f.sha256, **kw))  # fmt: skip
        rep[pid] += 1

    ids = ids_by_name(rows)
    for name, rs in sorted(by_name.items()):
        eid = ids[name]
        trend = eid.startswith("cbattr:trend:")
        ents.append(KgEntity(qid=eid, kind="climate_trend" if trend else "extreme_event", labels={"en": name},
                             descriptions={"en": "extreme weather event or trend in Carbon Brief's attribution map"},
                             source_revision=rev))  # fmt: skip
        h = eid.rsplit(":", 1)[1]
        for et in sorted({r["Event type"] for r in rs if r["Event type"]}):
            st(eid, "cbattr:event_type", f"cbattr:{h}:type:{et}", value={"type": "string", "value": et})
        for pt in sorted({r["Event year/Trend"] for r in rs if r["Event year/Trend"]}):
            st(eid, "cbattr:event_period_text", f"cbattr:{h}:period_text:{pt}", value={"type": "string", "value": pt})
            p = period(pt)
            if p and p[0] == p[1]:
                st(
                    eid,
                    "P585",
                    f"cbattr:{h}:P585:{pt}",
                    value={"type": "time", "time": f"{p[0]:04d}-01-01", "precision": 9},
                )
            elif p:
                st(
                    eid,
                    "P580",
                    f"cbattr:{h}:P580:{pt}",
                    value={"type": "time", "time": f"{p[0]:04d}-01-01", "precision": 9},
                )
                st(
                    eid,
                    "P582",
                    f"cbattr:{h}:P582:{pt}",
                    value={"type": "time", "time": f"{p[1]:04d}-01-01", "precision": 9},
                )
        for c in sorted({c for r in rs for c in countries(r)}):
            q = country_qids.get(country_iso3(c, "iso3") or "")
            if q:
                st(eid, "cbattr:country", f"cbattr:{h}:country:{c}", obj=q, quals={"iso_country_code": c})
            else:
                rep["country_without_qid"] += 1
        for reg in sorted({r["cb-region"] for r in rs if r["cb-region"]}):
            st(eid, "cbattr:region", f"cbattr:{h}:region:{reg}", value={"type": "string", "value": reg})
        for r in rs:
            st(eid, "cbattr:finding", f"cbattr:{h}:finding:{r.key}", value={"type": "string", "value": r["Classification"]},
               quals={"citation": r["Citation"], "link": r["Link"], "rapid_study": r["Rapid study?"],
                      "published_in": r["Source"], "publication_year": r["Publication year"],
                      "summary_statement": r["Summary statement"][:4000], "csv_line": r.line,
                      "licence": RIGHTS["licence"]})  # fmt: skip
    return ents, sts, dict(rep)


def claims(rows: list[Row], sha: str) -> tuple[list[Any], dict[str, Any]]:
    from reva.causal.model import DatasetClaim

    out: list[Any] = []
    rep: Counter[str] = Counter()
    ids = ids_by_name(rows)
    for r in rows:
        pol, stance, slug = CLASSIFICATIONS[r["Classification"]]
        if stance is None:
            rep["no_claim_inconclusive"] += 1
            continue
        doi, basis = doi_of(r)
        if doi is None:
            rep["no_claim_without_doi"] += 1
            if r["Rapid study?"] == "Yes":
                rep["no_claim_without_doi_rapid"] += 1
            continue
        out.append(
            DatasetClaim(
                cause_id=CAUSE_ID,
                effect_id=ids[r["Name"]],
                relation="contributing_factor",
                polarity=pol,
                stance=stance,
                tier="T2",
                selector={"file": "papers-download.csv", "line": r.line, "row_sha": r.key},
                raw_sha256=sha,
                source_claim_id=f"cbattr:{r.key}",
                mechanism=r["Summary statement"][:4000] or None,
                citation={"doi": doi, "text": r["Citation"][:1000]},
                scope={
                    "classification": r["Classification"],
                    "event_type": r["Event type"],
                    "event_period": r["Event year/Trend"],
                    "countries_iso3": countries(r),
                    "rapid_study": r["Rapid study?"],
                    "published_in": r["Source"],
                    "publication_year": r["Publication year"],
                    "doi_basis": basis,
                    "compiled_by": "Carbon Brief attribution map (coded classification of the study's finding)",
                    "licence": RIGHTS["licence"],
                },
            )
        )
        rep[f"claims_{slug}"] += 1
    return out, dict(rep)


# --------------------------------------------------------------------------- ingest
async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    page = await ic.fetch_archived(run, PAGE_URL, name="index.html", key="cb:page", media_types=("text/html",),
                                   max_bytes=MAX_BYTES)  # fmt: skip
    meta = page_meta(page.body)
    csv_f = await ic.fetch_archived(run, CSV_URL, name="papers-download.csv", key="cb:csv",
                                    media_types=("text/csv", "text/plain"), max_bytes=MAX_BYTES)  # fmt: skip
    min_rows = int(run.options.get("min_rows", MIN_ROWS)) if run.origin == "test_fixture" else MIN_ROWS
    rows = parse_csv(csv_f.body, min_rows=min_rows)
    rb, atlas_rep = atlas_counts(rows, csv_f.sha256)
    out = run.register(
        dataset=DATASET,
        frame=rb.frame(),
        raw_sha256s=[csv_f.sha256],
        geo_scheme="iso3",
        rights={**RIGHTS, "source_key": SOURCE_KEY},
        coverage_note=f"Carbon Brief attribution map, last updated {meta['last_updated']}: counts of study findings per "
        "country and single event year (trends and multi-year events are in the KG only); NON-COMMERCIAL, NO DERIVATIVES",
    )
    result: dict[str, Any] = {
        "ok": True,
        "revision": meta["last_updated"],
        "rows": len(rows),
        "atlas": atlas_rep,
        "tables": [
            {
                "table": out["dataset_key"].removeprefix("atlas."),
                "rows": out["row_count"],
                "created": out["created"],
                "artifact_sha256": out["artifact_sha256"],
            }
        ],
    }
    if run.engine is None:
        return result
    ents, sts, kg_rep = kg_items(rows, csv_f, page, meta, load_country_qids())
    cc.register_props(run.engine, KG_PREDICATES, WIKIDATA_PIDS)
    result["kg"] = {
        "entities": len(ents),
        "statements": len(sts),
        "report": kg_rep,
        "load": cc.load_kg(
            run.engine,
            run.store,
            entities=ents,
            statements=sts,
            source_key=SOURCE_KEY,
            raw_sha256s=[csv_f.sha256, page.sha256],
            retrieved_at=csv_f.retrieved_at,
            origin=run.origin,
            parameters={"connector": NAME, "parser": PARSER_VERSION},
        ),
    }
    from reva.causal.model import load_dataset_claims

    cl, cl_rep = claims(rows, csv_f.sha256)
    rep = load_dataset_claims(
        run.engine,
        run.store,
        source_key=SOURCE_KEY,
        dataset=CAUSAL_DATASET,
        dataset_revision=f"last updated {meta['last_updated']}; csv sha256 {csv_f.sha256[:16]}",
        raw_sha256s=[csv_f.sha256],
        retrieved_at=csv_f.retrieved_at,
        origin=run.origin,
        claims=cl,
        parameters={"connector": NAME, "parser": PARSER_VERSION, "tier": "T2", "compiled_by": "Carbon Brief"},
    )
    result["causal"] = {"claims": len(cl), "report": cl_rep, "load": _short(rep)}
    return result


def _short(rep: Any) -> dict[str, Any]:
    d = dict(rep.__dict__) if hasattr(rep, "__dict__") else dict(rep)
    return {k: v for k, v in d.items() if isinstance(v, (int, str)) or k in ("unknown_endpoint_sample",)}
