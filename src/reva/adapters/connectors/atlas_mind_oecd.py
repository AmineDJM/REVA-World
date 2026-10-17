"""OECD *How's Life?* well-being: subjective well-being, social connections, trust, time use, safety perceptions
and mental health -> atlas tables (V4, agent V4MIND).

Source: the OECD *How's Life?* well-being database (OECD Centre on Well-being, Inclusion, Sustainability and Equal
Opportunity, agency ``OECD.WISE.WDP``) through the OECD Data Explorer SDMX API (``sdmx.oecd.org``, no key; source
key ``oecd.sdmx`` shared with A-FISCAL / A-ILO / A-SOCIETY / A-TRADE), ``format=csvfilewithlabels``. The same
parser as A-FISCAL's ``atlas.oecd.skills`` (which reads the "Knowledge and skills" measures of the same flow):
:func:`reva.adapters.connectors.atlas_fiscal_common.parse_flow` (dimension order checked, values verbatim, rows
without a value are not cells, observation status kept with its label, OECD aggregates kept unmapped).

Atlas tables (``source = "oecd"``):

* ``hsl_mind_current`` -- ``DSD_HSL@DF_HSL_CWB`` "Current well-being", the measures about how people feel, relate,
  spend their time and perceive their safety (codes and names verbatim from the OECD codelist ``CL_MEASURE``):
  2_6 Job strain, 2_9 Job satisfaction (+ 2_9_DEP), 4_1 Time off, 4_2 Long unpaid working hours, 4_3 Gender gap in
  working hours, 4_4 Satisfaction with time use (+ 4_4_DEP), 5_3 Deaths from suicide, alcohol, drugs, 5_4
  Self-reported depression, 7_1 Social support (+ 7_1_DEP Lack of social support), 7_2 Time spent in social
  interactions, 7_3 Satisfaction with personal relationships (+ 7_3_DEP), 7_4 Feeling lonely, 8_1 Having a say in
  government (+ 8_1_DEP), 10_2 Feeling safe at night (+ 10_2_DEP), 11_1 Life satisfaction (+ 11_1_DEP), 11_2
  Negative affect balance, 11_3 Feelings of physical pain. Breakdowns by age group, sex and education level are
  folded into the indicator (``11_1|unit_measure=0_TO_10|age=_T|sex=F|education_lev=_T``).
* ``hsl_mind_social_capital`` -- ``DSD_HSL@DF_HSL_FWB`` "Future well-being", social capital: 14_1 Trust in
  others, 14_2 Trust in the police, 14_3 Trust in government, 14_7 Volunteering through organisations.

The A-FISCAL ``oecd.hsl_skills`` table keeps the "Knowledge and skills" measures; the other How's Life measures
(income, housing, environment, natural/economic capital) belong to other V4 domains and are not requested here.

Third-party upstream: How's Life? compiles several of these measures from household surveys run by others
(national statistical offices, EU-SILC, and for some countries and measures the Gallup World Poll), as the OECD
documents per measure. The OECD open-by-default licence (CC BY 4.0 since 2024-07-01) excludes third-party
content, so cells whose upstream is a third-party survey carry that reservation: ``licence_review`` in the table
rights (owner decision listed in ``docs/dev/v4_v4mind.md``).

Survey-based measures are self-reports of the people interviewed (life satisfaction 0-10, share feeling lonely,
...); comparisons across countries carry the surveys' own differences in wording and mode. Nothing here is a
cause of anything: co-movements between these series are associations only.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import atlas_oecd_fiscal as of
from reva.adapters.connectors.atlas_fiscal_common import Flow, FlowReport
from reva.atlas.sources import AtlasSourceSpec

CONNECTOR_NAME = "atlas.oecd.wellbeing"
CONNECTOR_VERSION = "1.0.1"  # 1.0.0 was registered in the dev DB from an in-progress copy of this module
PARSER_VERSION = "atlas_mind_oecd/1"
CATALOG_FILE = "mind.yaml"

#: OECD How's Life? CL_MEASURE codes -> English names (verbatim from the codelist, read 2026-10-17).
CWB_MEASURES: dict[str, str] = {
    "2_6": "Job strain",
    "2_9": "Job satisfaction",
    "2_9_DEP": "Satisfaction with job score less than 5",
    "4_1": "Time off",
    "4_2": "Long unpaid working hours",
    "4_3": "Gender gap in working hours",
    "4_4": "Satisfaction with time use",
    "4_4_DEP": "Satisfaction with time use score less than 5",
    "5_3": "Deaths from suicide, alcohol, drugs",
    "5_4": "Self-reported depression",
    "7_1": "Social support",
    "7_1_DEP": "Lack of social support",
    "7_2": "Time spent in social interactions",
    "7_3": "Satisfaction with personal relationships",
    "7_3_DEP": "Satisfaction with personal relationships score less than 5",
    "7_4": "Feeling lonely",
    "8_1": "Having a say in government",
    "8_1_DEP": "Not having a say in government",
    "10_2": "Feeling safe at night",
    "10_2_DEP": "Not feeling safe at night",
    "11_1": "Life satisfaction",
    "11_1_DEP": "Life satisfaction score less than 5",
    "11_2": "Negative affect balance",
    "11_3": "Feelings of physical pain",
}
FWB_MEASURES: dict[str, str] = {
    "14_1": "Trust in others",
    "14_2": "Trust in the police",
    "14_3": "Trust in government",
    "14_7": "Volunteering through organisations",
}
MEASURE_NAMES = {**CWB_MEASURES, **FWB_MEASURES}
_DIMS = ("REF_AREA", "MEASURE", "UNIT_MEASURE", "AGE", "SEX", "EDUCATION_LEV", "DOMAIN")
_FOLD = ("MEASURE", "UNIT_MEASURE", "AGE", "SEX", "EDUCATION_LEV")
FIXTURE_AREAS = "FRA+JPN+MEX"

FLOW_CURRENT = Flow(
    dataset="hsl_mind_current",
    flow="DSD_HSL@DF_HSL_CWB",
    agency="OECD.WISE.WDP",
    title="How's Life? Current well-being - subjective well-being, social connections, time use, safety, mental health",
    dims=_DIMS,
    area_dim="REF_AREA",
    fold=_FOLD,
    keys=("{areas}." + "+".join(CWB_MEASURES) + ".....",),
    fixture_keys=(f"{FIXTURE_AREAS}.11_1+7_4+5_4+10_2+4_1.....",),
    max_bytes=60_000_000,
    notes="Measure names (OECD CL_MEASURE): "
    + "; ".join(f"{k} {v}" for k, v in CWB_MEASURES.items())
    + ". Self-reported survey measures; breakdowns by age, sex and education level folded into the indicator."
    " Third-party survey upstream for some measures (OECD documents the survey per measure): licence review.",
)
FLOW_SOCIAL_CAPITAL = Flow(
    dataset="hsl_mind_social_capital",
    flow="DSD_HSL@DF_HSL_FWB",
    agency="OECD.WISE.WDP",
    title="How's Life? Future well-being - social capital (trust, volunteering)",
    dims=_DIMS,
    area_dim="REF_AREA",
    fold=_FOLD,
    keys=("{areas}." + "+".join(FWB_MEASURES) + ".....",),
    fixture_keys=(f"{FIXTURE_AREAS}.14_1+14_3.....",),
    max_bytes=20_000_000,
    notes="Measure names (OECD CL_MEASURE): "
    + "; ".join(f"{k} {v}" for k, v in FWB_MEASURES.items())
    + ". Survey measures; unit codes ending in _SUB are the subgroup (breakdown) variants the OECD publishes.",
)
FLOWS = (FLOW_CURRENT, FLOW_SOCIAL_CAPITAL)

RIGHTS: dict[str, Any] = {
    **of.RIGHTS,
    "licence_review": (
        "How's Life? compiles some measures from third-party surveys (e.g. Gallup World Poll for some countries "
        "and measures); the OECD open-by-default licence excludes third-party content -> owner review before "
        "redistribution (docs/dev/v4_v4mind.md)."
    ),
    "agent": "V4MIND",
}


def _hints(dataset: str) -> dict[str, str]:
    from reva.adapters.connectors.atlas_society_common import concept_hints

    return concept_hints(CATALOG_FILE, of.SOURCE, dataset)


def build(flow: Flow, files: list[tuple[str, bytes, str]]) -> tuple[pl.DataFrame, FlowReport, str]:
    """A-FISCAL's SDMX-CSV parser, then the catalog's concept hints on the headline (total) cells and the
    measure's OECD name in ``flags.measure_name``."""
    frame, rep, note = of.oecd_build(flow, files)
    hints = _hints(flow.dataset)
    measure = frame["indicator"].str.split("|").list.first()
    names = measure.replace_strict(MEASURE_NAMES, default=None, return_dtype=pl.String)
    unknown = sorted(set(measure.filter(names.is_null()).to_list()))
    if unknown:
        raise fc.FiscalParseError(f"{flow.flow}: measures not requested: {unknown[:10]}")
    frame = frame.with_columns(
        pl.col("indicator").replace_strict(hints, default=None, return_dtype=pl.String).alias("concept_id"),
        pl.struct(flags=pl.col("flags"), name=names)
        .map_elements(lambda s: fc.canon({**json.loads(s["flags"]), "measure_name": s["name"]}), return_dtype=pl.String)
        .alias("flags"),
    )
    return frame, rep, note


def requests(flow: Flow, fixture: bool) -> list[fc.Request]:
    return of.oecd_requests(flow, fixture)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=of.SOURCE_KEY,
        budget_key=of.SOURCE_KEY,
        atlas_source=of.SOURCE,
        provider="OECD Centre on Well-being, Inclusion, Sustainability and Equal Opportunity (How's Life? database)",
        licence=of.LICENCE + "; third-party survey upstream for some measures: licence review",
        allowed_hosts=(of.HOST,),
        datasets=tuple(f.dataset for f in FLOWS),
        dataset_ids=tuple(f"{f.agency}:{f.flow}" for f in FLOWS),
        cadence_seconds=90 * 86_400,
        planned_requests=sum(len(f.keys) for f in FLOWS),
        max_response_bytes=60_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(of, sys.modules[__name__]),
        field_mappings={
            "geo_scheme": of.GEO_SCHEME,
            "measures": {f.dataset: list(CWB_MEASURES if f is FLOW_CURRENT else FWB_MEASURES) for f in FLOWS},
        },
        notes="Two SDMX-CSV (with labels) requests (~20 MB in all).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await fc.ingest_flows(run, FLOWS, requests, build, geo_scheme=of.GEO_SCHEME, rights=RIGHTS)


__all__ = [
    "CONNECTOR_NAME",
    "CWB_MEASURES",
    "FLOWS",
    "FWB_MEASURES",
    "MEASURE_NAMES",
    "atlas_ingest",
    "atlas_source",
    "build",
]
