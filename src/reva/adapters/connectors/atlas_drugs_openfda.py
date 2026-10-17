"""openFDA aggregated counts: FAERS adverse-event reports, drug labels, drug recalls (V6, agent V6DRUGS).

Source
------
openFDA (U.S. Food and Drug Administration), ``https://api.fda.gov/drug/{event,label,enforcement}.json`` -- only the
``count`` aggregation endpoints (no individual report is fetched). Licence: **CC0 1.0** (open.fda.gov/license:
"unless otherwise noted, the content, data, documentation, code, and related materials on openFDA is public
domain and made available with a Creative Commons CC0 1.0 Universal dedication"). Every response carries
openFDA's disclaimer and ``last_updated`` date, kept on every value.

No API key is used: without one openFDA allows 240 requests per minute and 1,000 per day per IP, and refuses
``count`` queries with ``limit`` > 100 on the event endpoint (``API_KEY_MISSING``). A key requires a registration
(an e-mail): owner decision, documented in docs/dev/v6_drugs.md.

Adverse events are REPORTS, not causation. FDA (https://open.fda.gov/apis/drug/event/, retrieved 2026-10-17):
"FAERS data does have limitations. There is no certainty that the reported event (adverse event or medication
error) was actually due to the product. FDA does not require that a causal relationship between a product and
event be proven, and reports do not always contain enough detail to properly evaluate an event."

Requests (about 25 + 15 + 3 + substances)
-----------------------------------------
* ``event.json?search=receivedate:[Y0101 TO Y1231]&count=occurcountry.exact&limit=300`` for every year 2004..now
  -> atlas table ``openfda.faers_country`` (reports received per year by country where the event occurred; ISO
  3166-1 alpha-2 codes as published -> ISO3 through the crosswalk; the current year is partial and its period ends
  at ``last_updated``);
* ``enforcement.json?search=report_date:[Y0101 TO Y1231]&count=classification.exact`` 2012..now -> atlas table
  ``openfda.recalls`` (drug recall enforcement reports per year and recall class; FDA = US market);
* ``event.json?count=patient.drug.openfda.unii.exact&limit=100`` (top 100 substances by reports),
  ``label.json?count=openfda.unii.exact&limit=1000``, ``enforcement.json?count=openfda.unii.exact&limit=1000`` ->
  KG counts on ``unii:<code>`` (only what the top lists return: a substance outside a list is Missing, not zero);
* ``event.json?search=patient.drug.openfda.unii:"<UNII>"&count=serious`` per substance in scope (default: the UNIIs
  UniChem gives for the ChEMBL drugs linked to WHO eEML medicines; at most ``max_substances``) -> serious /
  non-serious report counts. "No match" (HTTP 404 ``NOT_FOUND``) is recorded in the run report, never as zero:
  openFDA harmonises only the reports whose drug names it could match to a product.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from reva.adapters.connectors import drugs_common as dc
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.openfda"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_drugs_openfda/1"
SOURCE_KEY = "openfda.api"
HOST = "api.fda.gov"
EVENT = f"https://{HOST}/drug/event.json"
LABEL = f"https://{HOST}/drug/label.json"
ENFORCEMENT = f"https://{HOST}/drug/enforcement.json"
DS_COUNTRY = "faers_country"
DS_RECALLS = "recalls"
DS_SUBSTANCES = "substances"
FIRST_EVENT_YEAR = 2004
FIRST_RECALL_YEAR = 2012
COUNTRY_LIMIT = 300
MAX_BYTES = 2_000_000
DEFAULT_MAX_SUBSTANCES = 600
FDA_CAVEAT = (
    "FAERS data does have limitations. There is no certainty that the reported event (adverse event or medication "
    "error) was actually due to the product. FDA does not require that a causal relationship between a product and "
    "event be proven, and reports do not always contain enough detail to properly evaluate an event."
)
FDA_CAVEAT_URL = "https://open.fda.gov/apis/drug/event/"
RIGHTS = {
    "licence": "CC0 1.0 (openFDA)",
    "licence_url": "https://open.fda.gov/license/",
    "terms_url": "https://open.fda.gov/terms/",
    "attribution": "U.S. Food and Drug Administration, openFDA (https://open.fda.gov)",
    "caveat": FDA_CAVEAT,
}
UNII_RE = re.compile(r"^[A-Z0-9]{10}$")
ISO2_RE = re.compile(r"^[A-Z]{2}$")
#: Fixture mode: aspirin, imatinib, imatinib mesylate, metformin UNIIs (UniChem src14 rows of the fixture ChEMBL ids).
FIXTURE_SUBSTANCES = ("R16CO5Y76E", "BKJ8M8G5HI", "8A1O1M485B", "9100L32L2N")
FIXTURE_YEARS = (2023, 2024)
SERIOUS = {1: "serious", 2: "non_serious"}


def _range(field: str, y: int) -> str:
    return f"{field}:%5B{y}0101+TO+{y}1231%5D"


def country_url(y: int) -> str:
    return f"{EVENT}?search={_range('receivedate', y)}&count=occurcountry.exact&limit={COUNTRY_LIMIT}"


def recalls_url(y: int) -> str:
    return f"{ENFORCEMENT}?search={_range('report_date', y)}&count=classification.exact"


TOP_EVENT_URL = f"{EVENT}?count=patient.drug.openfda.unii.exact&limit=100"
LABEL_COUNT_URL = f"{LABEL}?count=openfda.unii.exact&limit=1000"
RECALL_COUNT_URL = f"{ENFORCEMENT}?count=openfda.unii.exact&limit=1000"


def serious_url(unii: str) -> str:
    return f"{EVENT}?search=patient.drug.openfda.unii:%22{unii}%22&count=serious"


# ============================================================================================== parsing
def parse_count(body: bytes, what: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """(results or None when openFDA answers NOT_FOUND, meta)."""
    d = dc.json_body(body, what)
    if not isinstance(d, dict):
        raise dc.err(f"{what}: not an object")
    if "error" in d:
        e = d["error"] if isinstance(d["error"], dict) else {}
        if e.get("code") == "NOT_FOUND":
            return None, {}
        raise dc.err(f"{what}: openFDA error {e.get('code')!r}: {str(e.get('message'))[:200]}", kind="api_error")
    meta, res = d.get("meta"), d.get("results")
    if not isinstance(meta, dict) or not isinstance(res, list):
        raise dc.err(f"{what}: no meta/results")
    if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", str(meta.get("last_updated") or "")):
        raise dc.err(f"{what}: meta.last_updated missing")
    out = []
    for r in res:
        if not isinstance(r, dict) or "term" not in r:
            raise dc.err(f"{what}: count row without term")
        out.append({"term": r["term"], "count": dc.as_count(r.get("count"), f"{what} count")})
    return out, meta


# ============================================================================================== atlas rows
def country_rows(
    results: Sequence[Mapping[str, Any]], *, year: int, last_updated: str, raw: str, url: str
) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj

    lu = date.fromisoformat(last_updated)
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    flags: dict[str, Any] = {
        "measure": "FAERS reports received by FDA in the year, by country where the event occurred (occurcountry)",
        "as_of": last_updated,
        "caveat": FDA_CAVEAT,
        "caveat_url": FDA_CAVEAT_URL,
        "reports_not_causation": True,
    }
    if lu < end:
        end = lu + timedelta(days=1)
        flags["partial_period"] = f"data through {last_updated}"
    if len(results) >= COUNTRY_LIMIT:
        flags["list_may_be_truncated_at_limit"] = COUNTRY_LIMIT
    out = []
    for i, r in enumerate(results):
        term = str(r["term"]).strip().upper()
        if not term or len(term) > 64:
            continue
        out.append(
            {
                "source": "openfda",
                "dataset": DS_COUNTRY,
                "indicator": "faers.reports_received|by=occurcountry",
                "concept_id": None,
                "iso3": None,
                "provider_geo": term,
                "entity_qid": None,
                "period_start": start,
                "period_end": end,
                "value": str(r["count"]),
                "value_text": None,
                "unit": "reports",
                "flags": json_obj(flags, iso2_published=bool(ISO2_RE.match(term))),
                "raw_sha256": raw,
                "selector": json_obj({"url": url, "index": i, "term": r["term"]}),
            }
        )
    return out


def recall_rows(
    results: Sequence[Mapping[str, Any]], *, year: int, last_updated: str, raw: str, url: str
) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj

    lu = date.fromisoformat(last_updated)
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    flags: dict[str, Any] = {
        "measure": "drug recall enforcement reports by report_date year and FDA recall classification",
        "scope": "recalls of drug products classified by FDA (US market); recalling firm may be abroad",
        "as_of": last_updated,
    }
    if lu < end:
        end = lu + timedelta(days=1)
        flags["partial_period"] = f"data through {last_updated}"
    return [
        {
            "source": "openfda",
            "dataset": DS_RECALLS,
            "indicator": f"recalls.enforcement_reports|class={str(r['term']).strip()}",
            "concept_id": None,
            "iso3": None,
            "provider_geo": "US",
            "entity_qid": None,
            "period_start": start,
            "period_end": end,
            "value": str(r["count"]),
            "value_text": None,
            "unit": "enforcement reports",
            "flags": json_obj(flags),
            "raw_sha256": raw,
            "selector": json_obj({"url": url, "index": i, "term": r["term"]}),
        }
        for i, r in enumerate(results)
    ]


# ============================================================================================== KG
def build_kg(
    *,
    top_events: tuple[list[dict[str, Any]], dict[str, Any], str],
    labels: tuple[list[dict[str, Any]], dict[str, Any], str],
    recalls: tuple[list[dict[str, Any]], dict[str, Any], str],
    serious: Mapping[str, tuple[list[dict[str, Any]], dict[str, Any], str]],
    revision: str,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    from reva.kg.model import KgEntity, KgStatement

    ents: dict[str, Any] = {}
    sts: list[Any] = []
    skipped: Counter[str] = Counter()

    def unii_ent(u: str) -> str:
        q = f"unii:{u}"
        if q not in ents:
            ents[q] = KgEntity(qid=q, kind=dc.KIND_SUBSTANCE, labels={"en": f"UNII {u}"},
                               descriptions={"en": "FDA Unique Ingredient Identifier (label composed by REVA from "
                                             "the code)"}, source_revision=revision)
        return q

    def add(u: str, pid: str, n: int, unit: str, raw: str, meta: Mapping[str, Any], quals: Mapping[str, Any],
            sel: Mapping[str, Any]) -> None:
        q = unii_ent(u)
        sts.append(
            KgStatement(
                subject_qid=q,
                property_pid=pid,
                value=dc.count_value(n, unit),
                point_in_time=dc.day_time(str(meta["last_updated"])),
                qualifiers={"as_of": meta.get("last_updated"), **quals},
                references=[{"stated_in": "openFDA API", "selector": dict(sel),
                             "disclaimer": str(meta.get("disclaimer") or "")[:400]}],
                source_revision=revision,
                raw_sha256=raw,
            )
        )

    for name, (rows, meta, raw), pid, unit, scope, url in (
        ("events", top_events, "openfda:faers_reports", "reports",
         "top 100 substances by FAERS report count (openFDA count limit without API key)", TOP_EVENT_URL),
        ("labels", labels, "openfda:label_count", "labels",
         "top 1,000 substances by number of SPL labels listing them", LABEL_COUNT_URL),
        ("recalls", recalls, "openfda:recall_count", "enforcement reports",
         "substances listed in drug recall enforcement reports (top 1,000)", RECALL_COUNT_URL),
    ):
        for rank, r in enumerate(rows, start=1):
            u = str(r["term"]).strip()
            if not UNII_RE.match(u):
                skipped[f"{name}_term_not_unii"] += 1
                continue
            quals: dict[str, Any] = {"scope": scope, "rank": rank, "query": url.split("?", 1)[1]}
            if pid == "openfda:faers_reports":
                quals.update(seriousness="all", caveat=FDA_CAVEAT, reports_not_causation=True)
            add(u, pid, r["count"], unit, raw, meta, quals, {"url": url, "rank": rank})
    for u, (rows, meta, raw) in serious.items():
        for r in rows:
            kind = SERIOUS.get(int(r["term"])) if str(r["term"]).isdigit() else None
            if kind is None:
                skipped["serious_term_unknown"] += 1
                continue
            add(u, "openfda:faers_reports", r["count"], "reports", raw, meta,
                {"seriousness": kind, "query": serious_url(u).split("?", 1)[1],
                 "matching": "reports listing the substance in any drug of the report (suspect or concomitant), "
                 "through openFDA harmonised fields", "caveat": FDA_CAVEAT, "reports_not_causation": True},
                {"url": serious_url(u), "term": r["term"]})
    rep = {
        "substances": len(ents),
        "statements": len(sts),
        "by_predicate": dict(Counter(s.property_pid for s in sts)),
        "skipped": dict(skipped),
    }
    return list(ents.values()), sts, rep


# ============================================================================================== run
def code_digest() -> str:
    import sys

    return dc.digest(sys.modules[__name__], dc)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source="openfda",
        provider="U.S. Food and Drug Administration, openFDA API (count aggregations)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=(DS_COUNTRY, DS_RECALLS, DS_SUBSTANCES),
        dataset_ids=(EVENT, LABEL, ENFORCEMENT),
        cadence_seconds=30 * 86400,
        planned_requests=700,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=0.5,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"faers_country": "count=occurcountry.exact per receivedate year",
                        "recalls": "count=classification.exact per report_date year",
                        "substances": "count=patient.drug.openfda.unii.exact / openfda.unii.exact / serious"},
        notes="Aggregated counts only (no individual report). Adverse-event REPORTS, never causation.",
    )


async def _get(run: Any, url: str, key: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any], str | None, Any]:
    """(results | None on NOT_FOUND, meta, raw sha | None, raw acquisition | None)."""
    try:
        raw = await run.get(url, key=key, max_bytes=MAX_BYTES)
    except SourceResponseError as e:
        if (e.details or {}).get("status") == 404:
            return None, {}, None, None
        raise
    sha = run.archive(raw)
    res, meta = parse_count(run.store.get("raw", sha), key)
    return res, meta, sha, raw


def _scope_substances(run: Any) -> tuple[list[str], str]:
    opt = dc.opt_list(run.options.get("substances"))
    if opt is not None:
        bad = [u for u in opt if not UNII_RE.match(u)]
        if bad:
            raise dc.err(f"option substances: not UNII codes: {bad[:5]}", kind="invalid_option")
        return sorted(set(opt)), "option"
    if run.origin == "test_fixture":
        return list(FIXTURE_SUBSTANCES), "fixture"
    if not dc.kg_available(run.engine):
        return [], "kg_unavailable"
    eml = {o for _, o in dc.current_edges(run.engine, run.origin, "eml:chembl_molecule")}
    uniis = sorted({o.split(":", 1)[1] for s, o in dc.current_edges(run.engine, run.origin, "unichem:fda_unii")
                    if s in eml})
    return uniis, "eml_linked_chembl_drugs"


async def atlas_ingest(run: Any) -> dict[str, Any]:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    out: dict[str, Any] = {}
    this_year = datetime.now().year
    years_e = list(FIXTURE_YEARS) if run.origin == "test_fixture" else list(range(FIRST_EVENT_YEAR, this_year + 1))
    years_r = list(FIXTURE_YEARS) if run.origin == "test_fixture" else list(range(FIRST_RECALL_YEAR, this_year + 1))
    if run.wants(DS_COUNTRY):
        rows: list[dict[str, Any]] = []
        shas: list[str] = []
        empty: list[int] = []
        for y in years_e:
            res, meta, sha, _ = await _get(run, country_url(y), f"openfda:event:country:{y}")
            if res is None or sha is None:
                empty.append(y)
                continue
            shas.append(sha)
            rows += country_rows(res, year=y, last_updated=meta["last_updated"], raw=sha, url=country_url(y))
        if rows:
            out[DS_COUNTRY] = run.register(
                dataset=DS_COUNTRY,
                frame=pl.DataFrame(rows, schema=ATLAS_SCHEMA),
                raw_sha256s=sorted(set(shas)),
                coverage_note=f"FAERS reports received per year {years_e[0]}-{years_e[-1]} by occurcountry "
                "(openFDA count aggregation); reports, not causation; current year partial",
                geo_scheme="iso2",
                rights=dict(RIGHTS),
            )
        out["faers_country_years_not_found"] = empty
    if run.wants(DS_RECALLS):
        rows, shas, empty = [], [], []
        for y in years_r:
            res, meta, sha, _ = await _get(run, recalls_url(y), f"openfda:enforcement:class:{y}")
            if res is None or sha is None:
                empty.append(y)
                continue
            shas.append(sha)
            rows += recall_rows(res, year=y, last_updated=meta["last_updated"], raw=sha, url=recalls_url(y))
        if rows:
            out[DS_RECALLS] = run.register(
                dataset=DS_RECALLS,
                frame=pl.DataFrame(rows, schema=ATLAS_SCHEMA),
                raw_sha256s=sorted(set(shas)),
                coverage_note=f"drug recall enforcement reports per year {years_r[0]}-{years_r[-1]} and class",
                geo_scheme="iso2",
                rights=dict(RIGHTS),
            )
        out["recall_years_not_found"] = empty
    if run.wants(DS_SUBSTANCES):
        lists = {}
        retrieved: datetime | None = None
        for key, url in (("events", TOP_EVENT_URL), ("labels", LABEL_COUNT_URL), ("recalls", RECALL_COUNT_URL)):
            res, meta, sha, raw = await _get(run, url, f"openfda:{key}:unii")
            if res is None or sha is None:
                raise dc.err(f"openFDA {key} count by UNII returned NOT_FOUND", kind="api_error")
            retrieved = retrieved or raw.retrieved_at
            lists[key] = (res, meta, sha)
        scope, scope_kind = _scope_substances(run)
        cap = int(run.options.get("max_substances") or DEFAULT_MAX_SUBSTANCES)
        serious: dict[str, tuple[list[dict[str, Any]], dict[str, Any], str]] = {}
        not_found: list[str] = []
        for u in scope[:cap]:
            res, meta, sha, _ = await _get(run, serious_url(u), f"openfda:event:serious:{u}")
            if res is None or sha is None:
                not_found.append(u)
                continue
            serious[u] = (res, meta, sha)
        lu = "/".join(str(lists[k][1].get("last_updated")) for k in ("events", "labels", "recalls"))
        revision = f"openFDA last_updated event/label/enforcement {lu}"
        ents, sts, rep = build_kg(top_events=lists["events"], labels=lists["labels"], recalls=lists["recalls"],
                                  serious=serious, revision=revision)
        rep.update(scope=scope_kind, scope_size=len(scope), scope_capped_at=cap if len(scope) > cap else None,
                   serious_not_found=not_found[:50], serious_not_found_count=len(not_found))
        out["kg_build"] = rep
        if dc.kg_available(run.engine):
            assert retrieved is not None
            raws = sorted({v[2] for v in lists.values()} | {v[2] for v in serious.values()})
            out["kg"] = dc.load_kg(
                run.engine,
                run.store,
                entities=ents,
                statements=sts,
                source_key=SOURCE_KEY,
                raw_sha256s=raws,
                retrieved_at=retrieved,
                origin=run.origin,
                parameters={"connector": NAME, "parser": PARSER_VERSION, "scope": scope_kind},
                prefixes=("openfda:",),
                retract=scope_kind != "option" and not rep.get("scope_capped_at"),
            )
        else:
            out["kg"] = {"status": "kg_unavailable"}
    return out


__all__ = [
    "FDA_CAVEAT",
    "NAME",
    "SOURCE_KEY",
    "atlas_ingest",
    "atlas_source",
    "build_kg",
    "country_rows",
    "parse_count",
    "recall_rows",
]
