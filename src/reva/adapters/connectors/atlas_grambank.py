"""Grambank (CLDF release on Zenodo) -> KG grammatical feature values on Glottolog languoids (V3, agent KGLANG).

Source
------
Grambank, ``grambank/grambank`` on Zenodo (concept record 7740139; **CC BY 4.0**, checked on every run's record).
Cite: Skirgård, Hedvig et al. (2023). Grambank reveals the importance of genealogical constraints on linguistic
diversity and highlights the impact of language loss. Science Advances 9(16), doi:10.1126/sciadv.adg6175; and the
dataset: Grambank <version>, Zenodo, doi:<version DOI>. Latest version at the time of writing: v1.0.3 (2023-04-19,
doi:10.5281/zenodo.7844558). Fetch, archiving and licence check as for Glottolog
(:mod:`reva.adapters.connectors.atlas_glottolog`).

What is loaded (``source_key`` ``grambank.cldf``; KG only, no atlas table)
---------------------------------------------------------------------------
* One string-valued predicate per Grambank feature (195), generated from ``parameters.csv``
  (``grambank:gb020`` = "Are there definite or specific articles? (Grambank GB020)").
* One statement per ``values.csv`` row whose value is coded (0/1/2/3): subject ``glottolog:<Language_ID>``
  (Grambank language ids are Glottocodes), value = the CodeTable description (``absent``, ``present``, ``N-Num``, ...); qualifiers:
  code id, the value as coded, comment, sources, source comment, coders. Rows coded ``?`` (the sources consulted
  do not allow a decision) are not stored; they are counted in the run report (~80k). ~362k statements, loaded
  in two batches of at most 200k.

Grambank language ids follow the Glottolog version Grambank was built on: a code Glottolog later retired stays a
bare ``glottolog:<code>`` node (counted in the run report against the loaded Glottolog release).
"""

from __future__ import annotations

import inspect
from typing import Any

from reva.adapters.connectors import atlas_glottolog as gl
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.grambank"
VERSION = "1.0.1"
PARSER_VERSION = "atlas_grambank/1"
SOURCE_KEY = "grambank.cldf"
ATLAS_SOURCE = "grambank"
DATASET = "features"
CONCEPT_RECID = "7740139"
TITLE_MARKER = "Grambank"
MAX_ZIP_BYTES = 60_000_000
SRC = gl.FeatureSource(
    ns="grambank",
    tag="Grambank",
    stated_in="Grambank (CLDF)",
    skip_values=frozenset({"?"}),
    value_column="Description",
    keep=(("Comment", "comment"), ("Source", "sources"), ("Source_comment", "source_comment"), ("Coders", "coders")),
)
RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": (
        "Skirgård, Hedvig et al. 2023. Grambank (CLDF dataset, concept DOI 10.5281/zenodo.7740139); Science "
        "Advances 9(16), doi:10.1126/sciadv.adg6175. https://grambank.clld.org"
    ),
}


def code_digest() -> str:
    return gl._digest(inspect.getmodule(code_digest), gl)


def grambank_subjects(z: gl.CldfZip) -> tuple[dict[str, str], dict[str, str], list[Any], dict[str, Any]]:
    """Grambank language id (a Glottocode) -> ``glottolog:<id>``; ids that are not Glottocodes are refused."""
    subject: dict[str, str] = {}
    names: dict[str, str] = {}
    bad: list[str] = []
    for r in z.rows("cldf/languages.csv", ("ID", "Name", "Glottocode")):
        lid = r["ID"].strip()
        gc = r["Glottocode"].strip() or lid
        if not gl.GLOTTOCODE_RE.match(gc) or gc != lid:
            bad.append(lid)
            continue
        subject[lid] = f"glottolog:{gc}"
        names[lid] = r["Name"].strip()
    return subject, names, [], {"languages": len(subject), "ids_not_glottocodes": bad}


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Grambank consortium (Max Planck Institute for Evolutionary Anthropology), CLDF release on Zenodo",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); attribution: {RIGHTS['attribution']}",
        allowed_hosts=(gl.ZENODO_HOST,),
        datasets=(DATASET,),
        dataset_ids=(f"zenodo:{CONCEPT_RECID}", "doi:10.5281/zenodo.7740139"),
        cadence_seconds=90 * gl.DAY,
        planned_requests=2,
        max_response_bytes=MAX_ZIP_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "subject": "languages.csv ID (= Glottocode) -> glottolog:<id>",
            "predicate": "parameters.csv ID -> grambank:<id>",
            "value": "codes.csv Description of values.csv Code_ID ('?' not stored)",
        },
        notes="KG only (no atlas table): ~362k coded grammatical values as literal statements on Glottolog languoids.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await gl.ingest_feature_dataset(
        run,
        src=SRC,
        concept_recid=CONCEPT_RECID,
        title_marker=TITLE_MARKER,
        dataset=DATASET,
        source_key=SOURCE_KEY,
        connector=NAME,
        parser_version=PARSER_VERSION,
        max_zip_bytes=MAX_ZIP_BYTES,
        subjects=grambank_subjects,
    )
