"""ClinGen gene-disease validity curations -> KG associations HGNC gene -> Mondo disease (V4, agent V4HEALTH).

Source
------
Clinical Genome Resource (ClinGen), "Gene-Disease Validity" summary CSV
(https://search.clinicalgenome.org/kb/gene-validity/download, ~1.1 MB, one request per run; served as
``Clingen-Gene-Disease-Summary-<date>.csv``). Licence: **CC0 1.0** -- ClinGen terms of use
(https://clinicalgenome.org/docs/terms-of-use/, checked 2026-10-17): "All curated content published by ClinGen is
available free of restriction under the CC0 1.0 Universal (CC0 1.0) Public Domain Dedication"; ClinGen requests
attribution and the access date (recorded in :data:`RIGHTS` and on every statement's reference).

File layout (checked every run; drift blocks the load): 3 preamble rows (title, ``FILE CREATED: <date>``,
``WEBPAGE: ...``), a ``+++`` separator, the header ``GENE SYMBOL, GENE ID (HGNC), DISEASE LABEL, DISEASE ID (MONDO),
MOI, SOP, CLASSIFICATION, ONLINE REPORT, CLASSIFICATION DATE, GCEP``, a separator, then one row per curation.

KG (source key ``clingen.gene_validity``)
-----------------------------------------
One statement per curation: ``hgnc:<id> --clingen:gene_disease_validity--> mondo:<id>``, statement id = the
curation's assertion id (``CGGV:assertion_...`` / legacy ``CGGCIEX:assertion_<n>``, from the online report URL; a re-curation is a new assertion, the
old one is retracted by the next run), qualifiers = classification (Definitive / Strong / Moderate / Limited /
Disputed / Refuted / No Known Disease Relationship ...), mode of inheritance, SOP version, classification date,
expert panel (GCEP), report URL, gene symbol and disease label as written. Family ``associated_with``: a ClinGen
classification grades the *evidence* for a gene-disease relationship; REVA does not turn it into a causal claim
(docs/dev/v4_v4health.md, "Gene-disease"). Gene and disease entities are not created here (HGNC: V4BIO; Mondo:
``atlas.mondo``); ids are used exactly as the file gives them, never matched by symbol or label.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from datetime import datetime
from typing import Any

from reva.adapters.connectors.health_obo_common import digest, drift, kg_available, load_kg
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.clingen"
VERSION = "1.0.1"
PARSER_VERSION = "atlas_clingen/1"
SOURCE_KEY = "clingen.gene_validity"
HOST = "search.clinicalgenome.org"
URL = f"https://{HOST}/kb/gene-validity/download"
DATASET = "gene_validity"
MAX_BYTES = 20_000_000
MIN_ROWS_LIVE = 2_000
PID = "clingen:gene_disease_validity"
HEADER = (
    "GENE SYMBOL",
    "GENE ID (HGNC)",
    "DISEASE LABEL",
    "DISEASE ID (MONDO)",
    "MOI",
    "SOP",
    "CLASSIFICATION",
    "ONLINE REPORT",
    "CLASSIFICATION DATE",
    "GCEP",
)
RIGHTS = {
    "licence": "CC0 1.0",
    "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "terms_url": "https://clinicalgenome.org/docs/terms-of-use/",
    "attribution": "Clinical Genome Resource (ClinGen), Gene-Disease Validity curations, "
    "https://search.clinicalgenome.org/kb/gene-validity (access date = retrieval date of the run)",
}
HGNC_RE = re.compile(r"^HGNC:([0-9]+)$")
MONDO_RE = re.compile(r"^MONDO:([0-9]{7})$")
#: ClinGen curation ids: ``CGGV:assertion_<uuid>-<date>`` (GCI) and ``CGGCIEX:assertion_<n>`` (GCI Express, legacy)
ASSERTION_RE = re.compile(r"/gene-validity/(CGG[A-Z]{1,8}:assertion_[A-Za-z0-9_.:-]+)$")


def parse(body: bytes) -> tuple[str, list[dict[str, Any]]]:
    """(file created date, rows with ``_line``). Layout drift raises ``HealthSourceError``."""
    try:
        text_ = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise drift("ClinGen CSV is not UTF-8") from e
    rows = list(csv.reader(io.StringIO(text_, newline="")))
    if len(rows) < 6 or not rows[0] or "GENE DISEASE VALIDITY" not in rows[0][0].upper():
        raise drift("ClinGen CSV: unexpected preamble")
    m = re.match(r"^FILE CREATED:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})$", rows[1][0].strip())
    if not m:
        raise drift("ClinGen CSV: no 'FILE CREATED: <date>' row")
    if tuple(c.strip() for c in rows[4]) != HEADER:
        raise drift(f"ClinGen CSV: header changed: {rows[4]}")
    if not rows[3][0].startswith("+++") or not rows[5][0].startswith("+++"):
        raise drift("ClinGen CSV: separator rows missing")
    out = []
    for n, r in enumerate(rows[6:], start=7):
        if not any(c.strip() for c in r):
            continue
        if len(r) != len(HEADER):
            raise drift(f"ClinGen CSV: line {n} has {len(r)} columns")
        d: dict[str, Any] = dict(zip(HEADER, (c.strip() for c in r), strict=True))
        d["_line"] = n
        out.append(d)
    return m.group(1), out


def build(rows: list[dict[str, Any]], *, raw_sha256: str, revision: str) -> tuple[list[Any], dict[str, Any]]:
    from reva.kg.model import KgStatement

    sts: dict[str, Any] = {}
    skipped: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    for r in rows:
        g, d = HGNC_RE.match(r["GENE ID (HGNC)"]), MONDO_RE.match(r["DISEASE ID (MONDO)"])
        a = ASSERTION_RE.search(r["ONLINE REPORT"])
        if g is None:
            skipped["gene_id_not_hgnc"] += 1
            continue
        if d is None:
            skipped["disease_id_not_mondo"] += 1
            continue
        if a is None:
            skipped["no_assertion_id_in_report_url"] += 1
            continue
        sid = a.group(1)
        if sid in sts:
            skipped["duplicate_assertion"] += 1
            continue
        classes[r["CLASSIFICATION"]] += 1
        quals = {
            "classification": r["CLASSIFICATION"],
            "mode_of_inheritance": r["MOI"],
            "sop": r["SOP"],
            "classification_date": r["CLASSIFICATION DATE"],
            "gcep": r["GCEP"],
            "report_url": r["ONLINE REPORT"],
            "gene_symbol_as_written": r["GENE SYMBOL"],
            "disease_label_as_written": r["DISEASE LABEL"],
        }
        sts[sid] = KgStatement(
            subject_qid=f"hgnc:{g.group(1)}",
            property_pid=PID,
            object_qid=f"mondo:{d.group(1)}",
            qualifiers={k: v for k, v in quals.items() if v},
            references=[
                {
                    "stated_in": "ClinGen Gene-Disease Validity summary CSV",
                    "selector": {"file": "gene-validity download (CSV)", "line": r["_line"], "assertion": sid},
                }
            ],
            statement_id=sid,
            source_revision=revision,
            raw_sha256=raw_sha256,
        )
    rep = {
        "rows": len(rows),
        "statements": len(sts),
        "genes": len({s.subject_qid for s in sts.values()}),
        "diseases": len({s.object_qid for s in sts.values()}),
        "by_classification": dict(classes.most_common()),
        "skipped": dict(skipped),
    }
    return list(sts.values()), rep


def code_digest() -> str:
    import reva.adapters.connectors.health_obo_common as common

    return digest(code_digest, common)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source="clingen",
        provider="Clinical Genome Resource (ClinGen)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(URL,),
        cadence_seconds=30 * 86400,
        planned_requests=1,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"subject": "GENE ID (HGNC)", "object": "DISEASE ID (MONDO)", "statement_id": "ONLINE REPORT"},
        notes="KG only: one associated_with statement per ClinGen gene-disease validity curation (not causal).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    raw = await run.get(
        URL, key="clingen:gene-validity.csv", media_types=("text/plain", "text/csv"), max_bytes=MAX_BYTES
    )
    sha = run.archive(raw)
    body = run.store.get("raw", sha)
    created, rows = parse(body)
    if run.origin == "live" and len(rows) < MIN_ROWS_LIVE:
        raise drift(f"ClinGen CSV: only {len(rows)} rows: truncated?", kind="truncated")
    revision = f"FILE CREATED {created}"
    sts, rep = build(rows, raw_sha256=sha, revision=revision)
    out: dict[str, Any] = {"revision": revision, "raw_sha256": sha, "build": rep}
    if not kg_available(run.engine):
        out["kg"] = {"status": "kg_unavailable"}
        return out
    retrieved: datetime = raw.retrieved_at
    out["kg"] = load_kg(
        run.engine,
        run.store,
        entities=[],
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=[sha],
        retrieved_at=retrieved,
        origin=run.origin,
        parameters={"connector": NAME, "parser": PARSER_VERSION, "revision": revision},
    )
    return out


__all__ = ["NAME", "PID", "SOURCE_KEY", "atlas_ingest", "atlas_source", "build", "parse"]
