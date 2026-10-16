"""WALS Online (CLDF release on Zenodo) -> KG typological feature values on Glottolog languoids (V3, agent KGLANG).

Source
------
The World Atlas of Language Structures Online, ``cldf-datasets/wals`` on Zenodo (concept record 3606197; **CC BY
4.0**, checked on every run's record). Cite: Dryer, Matthew S. & Haspelmath, Martin (eds.) WALS Online
(<version>). Zenodo, doi:<version DOI>. https://wals.info. Latest version at the time of writing: v2020.6
(2026-09-28, doi:10.5281/zenodo.23010827). Fetch, archiving and licence check: see
:mod:`reva.adapters.connectors.atlas_glottolog` (Zenodo record -> release zip, md5/size checked, both archived).

What is loaded (``source_key`` ``wals.cldf``; KG only, no atlas table)
-----------------------------------------------------------------------
* One string-valued predicate per WALS feature, generated from the release's ``parameters.csv``
  (``wals:f81a`` = "Order of Subject, Object and Verb (WALS 81A)"), registered in the KG allow-list by this
  connector (``registry/kg/predicates_lang.yaml`` documents the namespace).
* One statement per ``values.csv`` row (~76k): subject ``glottolog:<Glottocode>`` -- the Glottocode **WALS
  itself gives** for the WALS language (``languages.csv`` column Glottocode) -- or ``wals:language:<WALS id>``
  (an entity created here) when WALS gives none. Value = the CodeTable name of the value (e.g. ``SOV``);
  qualifiers: WALS language id/name, code id, the value as coded, comment, WALS source references; the
  statement's own reference = ``values.csv`` row id. Several WALS languages can share one Glottocode: their
  statements stay distinct (statement id = WALS value id).

Feature values are literals on purpose: two languages sharing a word order are not "connected" for the path
engine.
"""

from __future__ import annotations

import inspect
from typing import Any

from reva.adapters.connectors import atlas_glottolog as gl
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.wals"
VERSION = "1.0.1"
PARSER_VERSION = "atlas_wals/1"
SOURCE_KEY = "wals.cldf"
ATLAS_SOURCE = "wals"
DATASET = "features"
CONCEPT_RECID = "3606197"
TITLE_MARKER = "World Atlas of Language Structures"
MAX_ZIP_BYTES = 60_000_000
SRC = gl.FeatureSource(
    ns="wals",
    tag="WALS",
    stated_in="WALS Online (CLDF)",
    keep=(("Comment", "comment"), ("Source", "wals_sources"), ("Example_ID", "example_ids")),
)
RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": (
        "Dryer, Matthew S. & Haspelmath, Martin (eds.). WALS Online. Zenodo (concept DOI 10.5281/zenodo.3606197). "
        "https://wals.info"
    ),
}


def code_digest() -> str:
    return gl._digest(inspect.getmodule(code_digest), gl)


def wals_subjects(z: gl.CldfZip) -> tuple[dict[str, str], dict[str, str], list[Any], dict[str, Any]]:
    """WALS language id -> KG node (WALS-declared Glottocode, else ``wals:language:<id>`` + entity)."""
    from reva.kg.model import KgEntity

    subject: dict[str, str] = {}
    names: dict[str, str] = {}
    ents: list[Any] = []
    no_gc: list[str] = []
    for r in z.rows("cldf/languages.csv", ("ID", "Name", "Glottocode")):
        lid = r["ID"].strip()
        if not lid or lid.startswith(("genus-", "family-", "subfamily-")):
            continue  # classification rows of the LanguageTable carry no values
        names[lid] = r["Name"].strip()
        gc = r["Glottocode"].strip()
        if gl.GLOTTOCODE_RE.match(gc):
            subject[lid] = f"glottolog:{gc}"
            continue
        node = f"wals:language:{lid}"
        subject[lid] = node
        no_gc.append(lid)
        ents.append(
            KgEntity(
                qid=node,
                kind="language",
                labels={"en": names[lid] or lid},
                descriptions={
                    "en": "WALS language for which WALS gives no Glottocode",
                    "fr": "langue de WALS sans code Glottolog dans WALS",
                },
            )
        )
    shared = len(subject) - len(set(subject.values()))
    return (
        subject,
        names,
        ents,
        {"languages": len(subject), "without_glottocode": sorted(no_gc), "languages_sharing_a_glottocode": shared},
    )


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="WALS Online (Max Planck Institute for Evolutionary Anthropology), CLDF release on Zenodo",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); attribution: {RIGHTS['attribution']}",
        allowed_hosts=(gl.ZENODO_HOST,),
        datasets=(DATASET,),
        dataset_ids=(f"zenodo:{CONCEPT_RECID}", "doi:10.5281/zenodo.3606197"),
        cadence_seconds=90 * gl.DAY,
        planned_requests=2,
        max_response_bytes=MAX_ZIP_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "subject": "languages.csv Glottocode (WALS-declared) else wals:language:<ID>",
            "predicate": "parameters.csv ID -> wals:f<id>",
            "value": "codes.csv Name of values.csv Code_ID",
        },
        notes="KG only (no atlas table): ~76k typological values as literal statements on Glottolog languoids.",
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
        subjects=wals_subjects,
    )
