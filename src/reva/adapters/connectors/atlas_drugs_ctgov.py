"""ClinicalTrials.gov registry counts by phase, status, country, condition and intervention (V6, agent V6DRUGS).

Source
------
ClinicalTrials.gov API v2 (U.S. National Library of Medicine), ``https://clinicaltrials.gov/api/v2``. Only
aggregates are read: the field-value statistics endpoint (``/stats/field/values``: top 250 values of a field with
the number of studies) and study *counts* (``/studies?...&countTotal=true&pageSize=1&fields=NCTId``: the total of
a search, one NCT id returned). No study record is stored beyond that one id per count request.

Terms (https://clinicaltrials.gov/about-site/terms-conditions): the data are provided by NLM as a public service;
NLM asks users to cite ClinicalTrials.gov and the data timestamp, and warns that listing a study does not mean it
has been evaluated by the U.S. Federal Government. Recorded as ``public domain (US Government work) with NLM terms``
(licence_review: submitters' free-text may carry their own rights; only counts and MeSH ids are used here).

Requests (about 2 + 4 x countries + 4 x conditions)
--------------------------------------------------
* ``/version`` -> ``dataTimestamp`` (the revision of every value);
* ``/stats/field/values?fields=Phase,OverallStatus,StudyType,InterventionType,LocationCountry,ConditionMeshId,
  InterventionMeshId`` -> atlas ``ctgov.registry_counts`` (global distributions) and ``ctgov.studies_by_country``
  (studies with at least one site in the country, all phases);
* per country (ClinicalTrials.gov's English country name, ``COVERAGE[FullMatch]`` so "Guinea" does not match
  "Equatorial Guinea") and phase 1-4 -> ``ctgov.studies_by_country``;
* per condition MeSH id of the top-250 list and phase 1-4 -> KG aggregate entities.

Country names -> ISO3: only exact equality with a name field of REVA's country crosswalk (registry/atlas/
geo_aliases/v6drugs.yaml, each alias with its evidence); other names stay unmapped (``iso3`` null) and are listed.

KG (source key ``ctgov.api``): ``ctgov:condition_mesh:<D>`` / ``ctgov:intervention_mesh:<D>`` (kind
``clinical_trial``: aggregate "studies registered whose NLM-derived condition / intervention MeSH terms include
<D>"), ``ctgov:studies_count`` per phase (qualifier ``phase``), ``ctgov:condition_mesh`` / ``ctgov:intervention_mesh``
-> ``mesh:<D>`` (family context). A count of registered studies says nothing about results or efficacy.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from reva.adapters.connectors import drugs_common as dc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.ctgov"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_drugs_ctgov/1"
SOURCE_KEY = "ctgov.api"
HOST = "clinicaltrials.gov"
API = f"https://{HOST}/api/v2"
VERSION_URL = f"{API}/version"
STATS_FIELDS = (
    "Phase",
    "OverallStatus",
    "StudyType",
    "InterventionType",
    "LocationCountry",
    "ConditionMeshId",
    "InterventionMeshId",
)
STATS_URL = f"{API}/stats/field/values?fields={','.join(STATS_FIELDS)}"
PHASES = ("PHASE1", "PHASE2", "PHASE3", "PHASE4")
DS_GLOBAL = "registry_counts"
DS_COUNTRY = "studies_by_country"
DS_KG = "mesh_aggregates"
GEO_SCHEME = "ctgov_country"
MAX_BYTES = 2_000_000
MESH_RE = re.compile(r"^[CD][0-9]{6,9}$")
RIGHTS = {
    "licence": "public domain (US Government work, NLM) with ClinicalTrials.gov terms and conditions",
    "licence_review": True,
    "terms_url": "https://clinicaltrials.gov/about-site/terms-conditions",
    "attribution": "ClinicalTrials.gov, U.S. National Library of Medicine (data timestamp per value)",
}
FIXTURE_COUNTRIES = ("France", "Nigeria")
FIXTURE_CONDITIONS = ("D003924", "D008288")  # type 2 diabetes mellitus, malaria (MeSH ids)


def count_url(term: str) -> str:
    return f"{API}/studies?query.term={quote(term, safe='')}&countTotal=true&pageSize=1&fields=NCTId"


def country_term(country: str, phase: str) -> str:
    return f'AREA[LocationCountry]COVERAGE[FullMatch]"{country}" AND AREA[Phase]{phase}'


def condition_term(mesh: str, phase: str) -> str:
    return f"AREA[ConditionMeshId]{mesh} AND AREA[Phase]{phase}"


# ============================================================================================== parsing
def parse_version(body: bytes) -> str:
    d = dc.json_body(body, "ctgov version")
    ts = d.get("dataTimestamp") if isinstance(d, dict) else None
    if not isinstance(ts, str) or not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T", ts):
        raise dc.err("ctgov version: no dataTimestamp")
    return ts


def parse_stats(body: bytes) -> dict[str, dict[str, Any]]:
    """``piece -> {"field", "missing", "unique", "values": [(value, studies)]}``."""
    d = dc.json_body(body, "ctgov stats")
    if not isinstance(d, list):
        raise dc.err("ctgov stats: not a list")
    out: dict[str, dict[str, Any]] = {}
    for item in d:
        if not isinstance(item, dict) or not isinstance(item.get("topValues"), list):
            raise dc.err("ctgov stats: item without topValues")
        vals = []
        for v in item["topValues"]:
            if not isinstance(v, dict) or not isinstance(v.get("value"), str):
                raise dc.err("ctgov stats: value without text")
            vals.append((v["value"], dc.as_count(v.get("studiesCount"), "ctgov studiesCount")))
        out[str(item.get("piece"))] = {
            "field": item.get("field"),
            "missing": dc.as_count(item.get("missingStudiesCount", 0), "missingStudiesCount"),
            "unique": item.get("uniqueValuesCount"),
            "values": vals,
        }
    missing = [f for f in STATS_FIELDS if f not in out]
    if missing:
        raise dc.err(f"ctgov stats: fields missing from the answer: {missing}")
    return out


def parse_total(body: bytes, what: str) -> int:
    d = dc.json_body(body, what)
    if not isinstance(d, dict) or "totalCount" not in d:
        raise dc.err(f"{what}: no totalCount")
    return dc.as_count(d["totalCount"], f"{what} totalCount")


# ============================================================================================== atlas rows
def _stock_period(ts: str) -> tuple[date, date]:
    d = date.fromisoformat(ts[:10])
    return d, d + timedelta(days=1)


def global_rows(stats: Mapping[str, Mapping[str, Any]], *, ts: str, raw: str) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj

    start, end = _stock_period(ts)
    out = []
    for piece in ("Phase", "OverallStatus", "StudyType", "InterventionType"):
        s = stats[piece]
        flags = {"measure": f"registered studies by {piece} (ClinicalTrials.gov field {s['field']})",
                 "stock_as_of": ts, "unique_values": s["unique"]}
        items = [*s["values"], ("__field_missing__", s["missing"])]
        for i, (val, n) in enumerate(items):
            out.append({
                "source": "ctgov", "dataset": DS_GLOBAL,
                "indicator": f"studies.registered|field={piece}|value={val}"[:200],
                "concept_id": None, "iso3": None, "provider_geo": "WORLD", "entity_qid": None,
                "period_start": start, "period_end": end, "value": str(n), "value_text": None, "unit": "studies",
                "flags": json_obj(flags, **({"meaning": "studies without a value for this field"}
                                            if val == "__field_missing__" else {})),
                "raw_sha256": raw, "selector": json_obj({"url": STATS_URL, "piece": piece, "index": i}),
            })
    return out


def country_rows(
    stats: Mapping[str, Mapping[str, Any]],
    phase_counts: Mapping[tuple[str, str], tuple[int, str]],
    *,
    ts: str,
    stats_raw: str,
) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj

    start, end = _stock_period(ts)
    base = {"measure": "registered studies with at least one location in the country (all statuses)",
            "stock_as_of": ts, "country_name_as_published": None}
    out = []
    for i, (name, n) in enumerate(stats["LocationCountry"]["values"]):
        out.append({
            "source": "ctgov", "dataset": DS_COUNTRY, "indicator": "studies.registered|phase=ALL",
            "concept_id": None, "iso3": None, "provider_geo": name[:64], "entity_qid": None,
            "period_start": start, "period_end": end, "value": str(n), "value_text": None, "unit": "studies",
            "flags": json_obj({**base, "country_name_as_published": name}), "raw_sha256": stats_raw,
            "selector": json_obj({"url": STATS_URL, "piece": "LocationCountry", "index": i}),
        })
    for (name, phase), (n, raw) in sorted(phase_counts.items()):
        url = count_url(country_term(name, phase))
        out.append({
            "source": "ctgov", "dataset": DS_COUNTRY, "indicator": f"studies.registered|phase={phase}",
            "concept_id": None, "iso3": None, "provider_geo": name[:64], "entity_qid": None,
            "period_start": start, "period_end": end, "value": str(n), "value_text": None, "unit": "studies",
            "flags": json_obj({**base, "country_name_as_published": name,
                               "phase_note": "a study listing several phases (e.g. PHASE2|PHASE3) counts in each"}),
            "raw_sha256": raw, "selector": json_obj({"url": url}),
        })
    return out


# ============================================================================================== KG
def build_kg(
    stats: Mapping[str, Mapping[str, Any]],
    phase_counts: Mapping[tuple[str, str], tuple[int, str]],
    *,
    ts: str,
    stats_raw: str,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    from reva.kg.model import KgEntity, KgStatement

    revision = f"ClinicalTrials.gov dataTimestamp {ts}"
    when = dc.day_time(ts[:10])
    ents: list[Any] = []
    sts: list[Any] = []
    skipped: Counter[str] = Counter()
    for piece, kind, pid in (("ConditionMeshId", "condition", "ctgov:condition_mesh"),
                             ("InterventionMeshId", "intervention", "ctgov:intervention_mesh")):
        for rank, (mesh, n) in enumerate(stats[piece]["values"], start=1):
            if not MESH_RE.match(mesh):
                skipped[f"{kind}_not_mesh_id"] += 1
                continue
            q = f"ctgov:{kind}_mesh:{mesh}"
            ents.append(KgEntity(
                qid=q, kind=dc.KIND_TRIAL_AGG,
                labels={"en": f"ClinicalTrials.gov studies with {kind} MeSH {mesh}"},
                descriptions={"en": f"REVA-composed aggregate: studies registered on ClinicalTrials.gov whose "
                              f"NLM-derived {kind} MeSH terms include {mesh} (counts only, no trial record)"},
                source_revision=revision))
            ref = [{"stated_in": "ClinicalTrials.gov API v2 /stats/field/values",
                    "selector": {"url": STATS_URL, "piece": piece, "rank": rank}}]
            sts.append(KgStatement(subject_qid=q, property_pid=pid, object_qid=f"mesh:{mesh}",
                                   references=ref, source_revision=revision, raw_sha256=stats_raw))
            sts.append(KgStatement(
                subject_qid=q, property_pid="ctgov:studies_count", value=dc.count_value(n, "studies"),
                point_in_time=when,
                qualifiers={"phase": "ALL", "rank_in_top_250": rank, "stock_as_of": ts,
                            "note": "registered studies (any status); not results, not efficacy"},
                references=ref, source_revision=revision, raw_sha256=stats_raw))
    for (mesh, phase), (n, raw) in sorted(phase_counts.items()):
        sts.append(KgStatement(
            subject_qid=f"ctgov:condition_mesh:{mesh}", property_pid="ctgov:studies_count",
            value=dc.count_value(n, "studies"), point_in_time=when,
            qualifiers={"phase": phase, "stock_as_of": ts,
                        "phase_note": "a study listing several phases (e.g. PHASE2|PHASE3) counts in each"},
            references=[{"stated_in": "ClinicalTrials.gov API v2 /studies countTotal",
                         "selector": {"url": count_url(condition_term(mesh, phase))}}],
            source_revision=revision, raw_sha256=raw))
    rep = {"aggregates": len(ents), "statements": len(sts), "skipped": dict(skipped)}
    return ents, sts, rep


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
        atlas_source="ctgov",
        provider="ClinicalTrials.gov API v2 (U.S. National Library of Medicine), aggregate counts",
        licence=f"{RIGHTS['licence']} ({RIGHTS['terms_url']}); {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=(DS_GLOBAL, DS_COUNTRY, DS_KG),
        dataset_ids=(STATS_URL, f"{API}/studies?countTotal=true"),
        cadence_seconds=30 * 86400,
        planned_requests=2100,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.3,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"stats": list(STATS_FIELDS), "phases": list(PHASES)},
        notes="Counts of registered studies only (no record, no results).",
    )


async def _get(run: Any, url: str, key: str) -> tuple[bytes, str, Any]:
    raw = await run.get(url, key=key, max_bytes=MAX_BYTES)
    sha = run.archive(raw)
    return run.store.get("raw", sha), sha, raw


async def atlas_ingest(run: Any) -> dict[str, Any]:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    body, vsha, vraw = await _get(run, VERSION_URL, "ctgov:version")
    ts = parse_version(body)
    body, ssha, _ = await _get(run, STATS_URL, "ctgov:stats")
    stats = parse_stats(body)
    fixture = run.origin == "test_fixture"
    out: dict[str, Any] = {"data_timestamp": ts, "stats": {k: len(v["values"]) for k, v in stats.items()}}
    if run.wants(DS_GLOBAL):
        out[DS_GLOBAL] = run.register(dataset=DS_GLOBAL, frame=pl.DataFrame(global_rows(stats, ts=ts, raw=ssha),
                                      schema=ATLAS_SCHEMA), raw_sha256s=[ssha],
                                      coverage_note=f"ClinicalTrials.gov registered studies by field value as of {ts}",
                                      rights=dict(RIGHTS))
    if run.wants(DS_COUNTRY):
        names = [n for n, _ in stats["LocationCountry"]["values"]]
        if fixture:
            names = [n for n in names if n in FIXTURE_COUNTRIES]
        cap = run.options.get("countries_max")
        if cap is not None:
            names = names[: int(cap)]
        pc: dict[tuple[str, str], tuple[int, str]] = {}
        for name in names:
            for ph in PHASES:
                b, sha, _ = await _get(run, count_url(country_term(name, ph)), f"ctgov:country:{name}:{ph}")
                pc[(name, ph)] = (parse_total(b, f"ctgov count {name} {ph}"), sha)
        rows = country_rows(stats, pc, ts=ts, stats_raw=ssha)
        out[DS_COUNTRY] = run.register(
            dataset=DS_COUNTRY, frame=pl.DataFrame(rows, schema=ATLAS_SCHEMA),
            raw_sha256s=sorted({ssha, *[v[1] for v in pc.values()]}),
            coverage_note=f"ClinicalTrials.gov studies with a site in the country, all phases ({len(stats['LocationCountry']['values'])} "
            f"countries) and phases 1-4 ({len(names)} countries), as of {ts}",
            geo_scheme=GEO_SCHEME, rights=dict(RIGHTS))
    if run.wants(DS_KG):
        conds = [m for m, _ in stats["ConditionMeshId"]["values"] if MESH_RE.match(m)]
        if fixture:
            conds = [m for m in conds if m in FIXTURE_CONDITIONS]
        cap = run.options.get("conditions_max")
        if cap is not None:
            conds = conds[: int(cap)]
        cc: dict[tuple[str, str], tuple[int, str]] = {}
        for m in conds:
            for ph in PHASES:
                b, sha, _ = await _get(run, count_url(condition_term(m, ph)), f"ctgov:condition:{m}:{ph}")
                cc[(m, ph)] = (parse_total(b, f"ctgov count {m} {ph}"), sha)
        ents, sts, rep = build_kg(stats, cc, ts=ts, stats_raw=ssha)
        out["kg_build"] = rep
        if dc.kg_available(run.engine):
            retrieved: datetime = vraw.retrieved_at
            out["kg"] = dc.load_kg(
                run.engine, run.store, entities=ents, statements=sts, source_key=SOURCE_KEY,
                raw_sha256s=sorted({ssha, *[v[1] for v in cc.values()]}), retrieved_at=retrieved, origin=run.origin,
                parameters={"connector": NAME, "parser": PARSER_VERSION, "conditions_with_phases": len(conds)},
                prefixes=("ctgov:",), retract=cap is None)
        else:
            out["kg"] = {"status": "kg_unavailable"}
    _ = vsha
    return out


__all__ = ["NAME", "SOURCE_KEY", "atlas_ingest", "atlas_source", "build_kg", "country_rows", "global_rows",
           "parse_stats", "parse_total", "parse_version"]
