"""UniChem identifier crosswalks of the approved ChEMBL drugs: ChEBI, DrugBank, FDA UNII (V6, agent V6DRUGS).

Source
------
UniChem (EMBL-EBI) "wholeSourceMapping" files, one per source pair, served from
``https://ftp.ebi.ac.uk/pub/databases/chembl/UniChem/data/wholeSourceMapping/src_id1/``:

* ``src1src7.txt.gz``  ChEMBL (src 1) -> ChEBI (src 7)       ~0.35 MB
* ``src1src2.txt.gz``  ChEMBL -> DrugBank (src 2)             ~0.07 MB
* ``src1src14.txt.gz`` ChEMBL -> FDA SRS / UNII (src 14)      ~0.48 MB

UniChem pairs two identifiers when both sources deposited the same *standard InChI* (identical standardised
structure): an identifier crosswalk published by the provider, not a name match. The files carry no licence file
(the UniChem FTP README only describes the contents); EMBL-EBI terms of use apply -> ``licence_review`` (the
identifiers themselves are facts; DrugBank's own data is CC BY-NC 4.0 and is NOT fetched -- only its accession
numbers as UniChem lists them).

Scope: only ChEMBL ids that are current ``drug`` entities of ``chembl.api`` in the KG (run ``atlas.chembl_drugs``
first); every other row of the files is counted and skipped.

KG (source key ``unichem.mapping``)
----------------------------------
``chembl:<id> --unichem:chebi--> chebi:<n>`` (family context: V4CHEM's ChEBI entities), ``--unichem:fda_unii-->
unii:<code>`` (context; openFDA's substance key), ``unichem:drugbank_id`` literal (DrugBank accession; the eEML
links its medicines to DrugBank, so this literal is the identifier bridge eEML -> ChEMBL). One-to-many rows are
kept and flagged (``targets_for_this_chembl_id`` > 1).
"""

from __future__ import annotations

import gzip
import io
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from reva.adapters.connectors import drugs_common as dc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.unichem"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_drugs_unichem/1"
SOURCE_KEY = "unichem.mapping"
DATASET = "chembl_crosswalk"
HOST = "ftp.ebi.ac.uk"
BASE = f"https://{HOST}/pub/databases/chembl/UniChem/data/wholeSourceMapping/src_id1"
MAX_BYTES = 5_000_000
MAX_UNCOMPRESSED = 50_000_000
#: UniChem source id -> (file, KG predicate, object prefix, object regex)
PAIRS: dict[int, tuple[str, str, str | None, re.Pattern[str]]] = {
    7: ("src1src7.txt.gz", "unichem:chebi", "chebi", re.compile(r"^CHEBI:([0-9]{1,9})$")),
    2: ("src1src2.txt.gz", "unichem:drugbank_id", None, re.compile(r"^(DB[0-9]{5})$")),
    14: ("src1src14.txt.gz", "unichem:fda_unii", "unii", re.compile(r"^([A-Z0-9]{10})$")),
}
SOURCE_NAMES = {7: "ChEBI", 2: "DrugBank", 14: "FDA SRS (UNII)"}
CHEMBL_RE = re.compile(r"^CHEMBL[0-9]{1,12}$")
RIGHTS = {
    "licence": "EMBL-EBI terms of use (no licence file published with the UniChem mapping files)",
    "licence_review": True,
    "terms_url": "https://www.ebi.ac.uk/about/terms-of-use/",
    "attribution": "UniChem, EMBL-EBI (https://www.ebi.ac.uk/unichem/); Chambers J et al. UniChem: a unified "
    "chemical structure cross-referencing and identifier tracking system. J Cheminform 2013 5:3.",
}


def url(src: int) -> str:
    return f"{BASE}/{PAIRS[src][0]}"


def parse(body: bytes, src: int) -> list[tuple[int, str, str]]:
    """``[(line, chembl id, other id)]`` of one gzip mapping file; the header names the two sources."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as g:
            data = g.read(MAX_UNCOMPRESSED + 1)
    except (OSError, EOFError) as e:
        raise dc.err(f"UniChem src1src{src}: not a gzip file") from e
    if len(data) > MAX_UNCOMPRESSED:
        raise dc.err(f"UniChem src1src{src}: larger than {MAX_UNCOMPRESSED} bytes uncompressed", kind="too_large")
    lines = data.decode("utf-8").splitlines()
    if not lines or lines[0].strip().replace("\t", " ") != f"From src:'1' To src:'{src}'":
        raise dc.err(f"UniChem src1src{src}: unexpected header {lines[0][:60] if lines else ''!r}")
    out = []
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise dc.err(f"UniChem src1src{src} line {n}: {len(parts)} columns")
        out.append((n, parts[0].strip(), parts[1].strip()))
    return out


def build(
    rows: Mapping[int, Sequence[tuple[int, str, str]]],
    raws: Mapping[int, str],
    drugs: set[str],
    revision: str,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """(entities, statements, report) for the ChEMBL ids in ``drugs`` (``CHEMBLn`` without prefix)."""
    from reva.kg.model import KgEntity, KgStatement

    sts: list[Any] = []
    ents: dict[str, Any] = {}
    rep: dict[str, Any] = {}
    for src, items in rows.items():
        fname, pid, prefix, rx = PAIRS[src]
        skipped: Counter[str] = Counter()
        per_chembl: defaultdict[str, int] = defaultdict(int)
        kept = []
        for line, cid, other in items:
            if not CHEMBL_RE.match(cid):
                skipped["not_a_chembl_id"] += 1
                continue
            if cid not in drugs:
                skipped["not_an_approved_drug_in_kg"] += 1
                continue
            m = rx.match(other)
            if not m:
                skipped["other_id_malformed"] += 1
                continue
            per_chembl[cid] += 1
            kept.append((line, cid, m.group(1)))
        for line, cid, oid in kept:
            quals: dict[str, Any] = {
                "basis": f"UniChem pairs ChEMBL and {SOURCE_NAMES[src]} records with the same standard InChI",
            }
            if per_chembl[cid] > 1:
                quals["targets_for_this_chembl_id"] = per_chembl[cid]
            obj = val = None
            if prefix is None:
                val = dc.external_id(oid)
            else:
                obj = f"{prefix}:{oid}"
                if prefix == "unii" and obj not in ents:
                    ents[obj] = KgEntity(
                        qid=obj,
                        kind=dc.KIND_SUBSTANCE,
                        labels={"en": f"UNII {oid}"},
                        descriptions={"en": "FDA Unique Ingredient Identifier (label composed by REVA from the code)"},
                        source_revision=revision,
                    )
            sts.append(
                KgStatement(
                    subject_qid=f"chembl:{cid}",
                    property_pid=pid,
                    object_qid=obj,
                    value=val,
                    qualifiers=quals,
                    references=[{"stated_in": f"UniChem {fname}", "selector": {"file": fname, "line": line}}],
                    source_revision=revision,
                    raw_sha256=raws[src],
                )
            )
        rep[SOURCE_NAMES[src]] = {
            "rows": len(items),
            "statements": len(kept),
            "chembl_ids": len(per_chembl),
            "one_to_many_chembl_ids": sum(1 for v in per_chembl.values() if v > 1),
            "skipped": dict(skipped),
        }
    return list(ents.values()), sts, rep


def code_digest() -> str:
    import sys

    return dc.digest(sys.modules[__name__], dc)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source="unichem",
        provider="UniChem (EMBL-EBI) whole-source mapping files",
        licence=f"{RIGHTS['licence']}; {RIGHTS['terms_url']} (licence review)",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(url(s) for s in PAIRS),
        cadence_seconds=90 * 86400,
        planned_requests=3,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"src7": "unichem:chebi", "src2": "unichem:drugbank_id", "src14": "unichem:fda_unii"},
        notes="KG only: identifier crosswalks of the approved ChEMBL drugs already in the KG (same standard InChI).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    rows: dict[int, list[tuple[int, str, str]]] = {}
    raws: dict[int, str] = {}
    retrieved: datetime | None = None
    for src in PAIRS:
        raw = await run.get(url(src), key=f"unichem:src1src{src}",
                            media_types=("application/x-gzip", "application/gzip", "application/octet-stream"),
                            max_bytes=MAX_BYTES)
        sha = run.archive(raw)
        retrieved = retrieved or raw.retrieved_at
        raws[src] = sha
        rows[src] = parse(run.store.get("raw", sha), src)
    # no release number is published with the files: the revision names the archived bytes (sha256 prefixes)
    revision = "unichem src7/src2/src14 " + "/".join(raws[s][:12] for s in PAIRS)
    out: dict[str, Any] = {"revision": revision[:100], "rows": {SOURCE_NAMES[s]: len(r) for s, r in rows.items()}}
    if not dc.kg_available(run.engine):
        out["kg"] = {"status": "kg_unavailable"}
        return out
    drugs = {
        q.split(":", 1)[1]
        for q in dc.current_entities(run.engine, run.origin, "chembl:CHEMBL%", kind=dc.KIND_DRUG)
    }
    if not drugs:
        raise dc.err("no ChEMBL drug entity in the KG: run atlas.chembl_drugs first", kind="dependency_missing")
    ents, sts, rep = build(rows, raws, drugs, revision[:100])
    out["build"] = rep
    assert retrieved is not None
    out["kg"] = dc.load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=sorted(raws.values()),
        retrieved_at=retrieved,
        origin=run.origin,
        parameters={"connector": NAME, "parser": PARSER_VERSION, "drugs_in_scope": len(drugs)},
        prefixes=("unichem:",),
        retract=True,
    )
    return out


__all__ = ["NAME", "SOURCE_KEY", "atlas_ingest", "atlas_source", "build", "parse"]
