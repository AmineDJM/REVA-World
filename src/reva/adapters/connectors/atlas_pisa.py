"""PISA student skills and PIAAC adult skills (country means) -> atlas table (V3, agent A-FISCAL).

Source: OECD *How's Life?* well-being database, current well-being, "Knowledge and skills" domain (OECD.WISE.WDP
``DSD_HSL@DF_HSL_CWB``) through the OECD Data Explorer SDMX API (``sdmx.oecd.org``, source key ``oecd.sdmx``),
``format=csvfilewithlabels``. It republishes the OECD's own assessment results as country means:

* PISA mean scores of 15-year-old students in reading (``6_1``), mathematics (``6_2``) and science (``6_3``),
  PISA 2006, 2009, 2012, 2015, 2018, 2022 (by sex for 2018/2022), the share of students with low skills in all
  three domains (``6_1_DEP``) and the top-decile ratios (``*_VER``);
* PIAAC (Survey of Adult Skills) mean numeracy (``6_4``) and literacy (``6_5``) scores, 2012–2023 rounds, by sex,
  age group and education level, and the share of adults with low numeracy (``6_4_DEP``).

Coverage: the ~46 OECD members and partners in How's Life (not the 81 PISA 2022 participants). The full PISA
tables (OECD PISA 2022 Results, Annex B1 Excel workbooks on www.oecd.org) answer 403 to non-browser clients and
the PISA Data Explorer is an interactive service without a bulk endpoint; IEA TIMSS/PIRLS results are not
openly licensed (IEA permission required for reproduction). See ``docs/dev/v3_a_fiscal.md``.

Periods are the assessment year (calendar year cell). Licence: OECD Terms and Conditions (see
:mod:`atlas_oecd_fiscal`).
"""

from __future__ import annotations

import sys
from typing import Any

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import atlas_oecd_fiscal as of
from reva.adapters.connectors.atlas_fiscal_common import Flow
from reva.atlas.sources import AtlasSourceSpec

CONNECTOR_NAME = "atlas.oecd.skills"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_oecd_skills/1"
SKILL_MEASURES = "6_1+6_2+6_3+6_1_DEP+6_1_VER+6_2_VER+6_3_VER+6_4+6_4_DEP+6_4_VER+6_5+6_5_VER"

FLOW = Flow(
    dataset="hsl_skills",
    flow="DSD_HSL@DF_HSL_CWB",
    agency="OECD.WISE.WDP",
    title="How's Life? Current well-being - knowledge and skills (PISA students, PIAAC adults)",
    dims=("REF_AREA", "MEASURE", "UNIT_MEASURE", "AGE", "SEX", "EDUCATION_LEV", "DOMAIN"),
    area_dim="REF_AREA",
    fold=("MEASURE", "UNIT_MEASURE", "AGE", "SEX", "EDUCATION_LEV"),
    keys=("{areas}." + SKILL_MEASURES + ".....",),
    fixture_keys=("FRA+CHL+ZAF.6_1+6_2+6_3+6_1_DEP+6_4.....",),
    max_bytes=20_000_000,
    notes="Measures 6_1/6_2/6_3 are PISA mean scores (points), 6_4/6_5 PIAAC mean scores (points); DOMAIN is the"
    " How's Life domain (HSL_6 knowledge and skills).",
)
FLOWS = (FLOW,)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=of.SOURCE_KEY,
        budget_key=of.SOURCE_KEY,
        atlas_source=of.SOURCE,
        provider="OECD Centre on Well-being (How's Life? database: PISA and PIAAC country means)",
        licence=of.LICENCE,
        allowed_hosts=(of.HOST,),
        datasets=(FLOW.dataset,),
        dataset_ids=(f"{FLOW.agency}:{FLOW.flow}",),
        cadence_seconds=90 * 86_400,
        planned_requests=1,
        max_response_bytes=20_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(of, sys.modules[__name__]),
        field_mappings={"geo_scheme": of.GEO_SCHEME, "measures": SKILL_MEASURES.split("+")},
        notes="One SDMX-CSV (with labels) request (~0.7 MB).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await fc.ingest_flows(
        run, FLOWS, of.oecd_requests, of.oecd_build, geo_scheme=of.GEO_SCHEME, rights=of.RIGHTS
    )


__all__ = ["CONNECTOR_NAME", "FLOW", "atlas_ingest", "atlas_source"]
