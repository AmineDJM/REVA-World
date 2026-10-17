"""Occupations (ISCO-08): ILOSTAT employment and hours by occupation -> atlas tables, and ISCO-08 groups -> KG
``occupation`` entities (V4, agent V4MIND).

Source: ILOSTAT (International Labour Organization) through its bulk/API service ``rplumber.ilo.org`` (budget key
``ilostat.rplumber`` shared with A-ILO; licence **CC BY 4.0**, attribution "ILOSTAT, International Labour
Organization"). The same connector class, parser and cell rules as A-ILO's ``atlas.ilostat``
(:class:`reva.adapters.connectors.atlas_ilo.IlostatConnector`: one gzip CSV per indicator, every classification
folded into the indicator, best source per area-year as ILOSTAT selects it, modelled and national estimates never
mixed) with a different indicator list:

* ``EMP_2EMP_SEX_OCU_NB_A`` "Employment by sex and occupation -- ILO modelled estimates" (thousands);
* ``EMP_TEMP_SEX_OCU_NB_A`` "Employment by sex and occupation (thousands)" (national estimates);
* ``HOW_TEMP_SEX_OCU_NB_A`` "Average weekly hours actually worked per employed person by sex and occupation".

KG: the ILOSTAT ``classif1`` code dictionary (``/metadata/dic/?var=classif1``, archived) lists the ISCO-08 major
groups (``OCU_ISCO08_1`` ... ``OCU_ISCO08_9``, ``OCU_ISCO08_0``) and sub-major groups (``OC2_ISCO08_11`` ...) with
their ILOSTAT labels. Each becomes an ``occupation`` entity ``isco08:<code>`` (label = the group title from the
ILOSTAT label, the full label kept as description). ``isco:part_of`` links a sub-major group to its major group:
in ISCO-08 the first digit of a group code is the code of the major group that contains it (the classification's
own coding rule), so the link follows the identifiers, never names. ILOSTAT's own categories (``TOTAL``, ``X`` not
elsewhere classified, ``96`` combined groups) are not ISCO groups and are not loaded. ``ilostat:measured_by`` links
an occupation to each loaded table whose cells use its ``classif1`` code (qualifier ``classif1``).

The ILO's ISCO-08 structure files and definitions (``webapps.ilo.org/public/english/bureau/stat/isco/``) answer
HTTP 403 to non-browser clients (2026-10-17): minor and unit groups (3-4 digits) and the official definitions are
not loaded from the ILO (Wikidata P952 "ISCO-08 occupation class" spec in ``registry/kg/queries/society``).

Employment counts and hours by occupation are descriptive; differences between occupations or countries are
associations, not causes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reva.adapters.connectors import atlas_ilo as ilo
from reva.adapters.connectors import atlas_labour_common as lc
from reva.adapters.connectors import atlas_mind_common as mc
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.ilostat_occupation"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_mind_occupations/1+" + ilo.PARSER_VERSION
KG_SOURCE_KEY = "ilostat.classif1"
INDICATORS: tuple[ilo.IloIndicator, ...] = (
    ilo.IloIndicator(
        "EMP_2EMP_SEX_OCU_NB_A",
        "ilo_modelled",
        "thousands",
        30_000_000,
        "Employment by sex and occupation -- ILO modelled estimates (thousands)",
    ),
    ilo.IloIndicator(
        "EMP_TEMP_SEX_OCU_NB_A", "national", "thousands", 30_000_000, "Employment by sex and occupation (thousands)"
    ),
    ilo.IloIndicator(
        "HOW_TEMP_SEX_OCU_NB_A",
        "national",
        "hours per week",
        30_000_000,
        "Average weekly hours actually worked per employed person by sex and occupation",
    ),
)
CLASSIF1_KEY = "dic:classif1"
_LABEL = re.compile(r"^Occupation \(ISCO-08\)(?:, 2 digit level)?: ([0-9]{1,2})(?:\.| -) (.+)$")
_MAJOR = re.compile(r"^OCU_ISCO08_([0-9])$")
_SUBMAJOR = re.compile(r"^OC2_ISCO08_([0-9]{2})$")


class OccupationConnector(ilo.IlostatConnector):
    """A-ILO's connector with the occupation indicators, plus the ``classif1`` dictionary (for the KG)."""

    def plan(self) -> list[FetchItem]:
        items = super().plan()
        items.append(
            FetchItem(
                key=CLASSIF1_KEY,
                url=ilo.dictionary_url("classif1"),
                expected_media_types=ilo.GZIP_MEDIA,
                max_bytes=5_000_000,
                params={"kind": "dictionary", "var": "classif1"},
                required=False,
            )
        )
        return items


def isco_groups(classif1: Mapping[str, str]) -> list[dict[str, str]]:
    """ISCO-08 major and sub-major groups from the ILOSTAT ``classif1`` dictionary (code -> label)."""
    out: list[dict[str, str]] = []
    for code, label in sorted(classif1.items()):
        m = _MAJOR.match(code) or _SUBMAJOR.match(code)
        if not m:
            continue
        isco = m.group(1)
        lm = _LABEL.match(label.strip())
        if not lm or lm.group(1) != isco:
            raise SourceResponseError(
                f"ILOSTAT classif1 {code}: label {label!r} does not carry its ISCO-08 code", retryable=False
            )
        out.append(
            {"classif1": code, "isco": isco, "title": lm.group(2).strip(), "label": label.strip(),
             "level": "major" if len(isco) == 1 else "sub_major", "parent": isco[0] if len(isco) == 2 else ""}
        )  # fmt: skip
    majors = {g["isco"] for g in out if g["level"] == "major"}
    orphans = [g["isco"] for g in out if g["parent"] and g["parent"] not in majors]
    if not majors or orphans:
        raise SourceResponseError(f"ILOSTAT classif1: majors {sorted(majors)}, orphans {orphans}", retryable=False)
    return out


def kg_records(
    groups: Sequence[dict[str, str]], tables: Mapping[str, set[str]], *, dic_sha: str
) -> tuple[list[Any], list[Any]]:
    """``tables`` = atlas dataset -> classif1 codes its cells use."""
    from reva.kg.model import KgEntity, KgStatement

    ref = {"raw_sha256": dic_sha, "selector": {"item": CLASSIF1_KEY, "dictionary": "classif1"}}
    common: dict[str, Any] = {"source_key": KG_SOURCE_KEY, "references": (ref,), "raw_sha256": dic_sha}
    ents: list[Any] = []
    sts: list[Any] = []
    for ds in sorted(tables):
        ind = next(i for i in INDICATORS if i.dataset == ds)
        ents.append(mc.atlas_table_entity(ilo.SOURCE, ds, ind.label))
    for g in groups:
        qid = f"isco08:{g['isco']}"
        ents.append(
            KgEntity(
                qid=qid,
                kind="occupation",
                labels={"en": g["title"]},
                descriptions={
                    "en": f"ISCO-08 {g['level'].replace('_', '-')} group {g['isco']} (ILOSTAT: {g['label']})"
                },
            )
        )
        sts.append(KgStatement(subject_qid=qid, property_pid="isco:code", value={"type": "external_id", "value": g["isco"]},
                               qualifiers={"classif1": g["classif1"]}, **common))  # fmt: skip
        if g["parent"]:
            sts.append(KgStatement(subject_qid=qid, property_pid="isco:part_of", object_qid=f"isco08:{g['parent']}",
                                   qualifiers={"basis": "ISCO-08 coding rule: first digit = major group"},
                                   **common))  # fmt: skip
        for ds, codes in sorted(tables.items()):
            if g["classif1"] in codes:
                sts.append(KgStatement(subject_qid=qid, property_pid="ilostat:measured_by",
                                       object_qid=f"atlas:{ilo.SOURCE}.{ds}",
                                       qualifiers={"classif1": g["classif1"], "atlas_table": f"{ilo.SOURCE}.{ds}"},
                                       **common))  # fmt: skip
    return ents, sts


def atlas_source() -> AtlasSourceSpec:
    import sys

    n_items = len(OccupationConnector(INDICATORS).plan())
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=ilo.SOURCE_KEY,
        budget_key=ilo.SOURCE_KEY,
        atlas_source=ilo.SOURCE,
        provider="International Labour Organization (ILOSTAT), employment and hours by occupation (ISCO-08)",
        licence=f"{ilo.RIGHTS['licence']} ({ilo.RIGHTS['terms_url']}); attribution: {ilo.RIGHTS['attribution']}",
        allowed_hosts=(ilo.HOST,),
        datasets=tuple(i.dataset for i in INDICATORS),
        dataset_ids=tuple(i.id for i in INDICATORS),
        cadence_seconds=90 * 86400,
        planned_requests=n_items,
        max_response_bytes=max(i.max_bytes for i in INDICATORS),
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=lc.code_digest(lc, ilo, sys.modules[__name__]),
        field_mappings={
            "tables": {i.id: f"{ilo.SOURCE}.{i.dataset}" for i in INDICATORS},
            "estimate_type": {i.id: i.estimate for i in INDICATORS},
            "geo_scheme": ilo.GEO_SCHEME,
            "kg_source_key": KG_SOURCE_KEY,
        },
        notes="A-ILO's ILOSTAT connector with occupation indicators; ISCO-08 groups to the KG from classif1.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    import polars as pl

    from reva.atlas.tables import read_atlas_table

    areas = ilo.FIXTURE_REF_AREAS if run.origin == "test_fixture" else None
    conn = OccupationConnector(INDICATORS, ref_areas=areas)
    out = await lc.ingest_with_run(run, conn)
    # the classif1 dictionary was archived by ingest_with_run (an optional item): read it back
    dic = next((f for f in out.get("fetched", []) if f["item"] == CLASSIF1_KEY), None)
    if dic is None or not mc.kg_available(run.engine):
        out["kg"] = {"skipped": "classif1 dictionary not fetched" if dic is None else "no KG schema"}
        return out
    body = run.store.get("raw", dic["sha256"])
    groups = isco_groups(ilo.parse_dictionary(body, "classif1"))
    used: dict[str, set[str]] = {}
    for t in out.get("tables", []):
        ds = t["table"].split(".", 1)[1]
        frame = read_atlas_table(run.store, t["artifact_sha256"])
        codes = frame.select(pl.col("indicator").str.extract(r"classif1=([A-Z0-9_]+)", 1)).to_series().drop_nulls()
        used[ds] = set(codes.unique().to_list())
    ents, sts = kg_records(groups, used, dic_sha=dic["sha256"])
    out["kg"] = {
        "groups": len(groups),
        **mc.load(run.engine, run.store, entities=ents, statements=sts, source_key=KG_SOURCE_KEY,
                  raw_sha256s=[dic["sha256"]], retrieved_at=datetime.now(UTC), origin=run.origin),
    }  # fmt: skip
    return out


__all__ = ["CONNECTOR_NAME", "INDICATORS", "OccupationConnector", "atlas_ingest", "atlas_source", "isco_groups",
           "kg_records"]  # fmt: skip
