"""ChEMBL approved drugs, mechanisms of action (drug -> target -> UniProt) and approved indications (V6, agent V6DRUGS).

Source
------
ChEMBL web services (``https://www.ebi.ac.uk/chembl/api/data``), release read from ``status.json`` and pinned for
the whole run (``chembl_db_version``; a release change between the first and the last request blocks the load),
plus the release's ``chembl_uniprot_mapping.txt`` (ChEMBL FTP ``ChEMBLdb/latest``; its header names the release,
which must equal the API's). Licence: **CC BY-SA 3.0** (``LICENSE`` file of the release; ``REQUIRED.ATTRIBUTION``:
"we ask that the ChEMBL IDs are preserved, and that the release number of ChEMBL is clearly displayed" -- both are:
entity keys are ChEMBL ids, the statement revision is the release). ShareAlike: derived data redistributed must
keep the licence (flagged ``licence_sharealike`` in the rights).

Bounded queries (no bulk dump):

* ``molecule.json?max_phase=4`` -- approved drugs only (~4,200 molecules incl. salts), selected fields;
* ``mechanism.json?max_phase=4`` -- mechanisms of action of approved drugs (~3,800 rows);
* ``target.json?target_chembl_id__in=`` -- only the targets those mechanisms name (scalar fields);
* ``drug_indication.json?max_phase_for_ind=4`` -- approved indications (~8,700 rows, with references);
* ``chembl_uniprot_mapping.txt`` -- UniProt accession -> target id of the release (1.2 MB).

About 20 + targets/100 requests and ~27 MB per run.

KG (source key ``chembl.api``)
-----------------------------
* ``chembl:<molecule id>`` kind ``drug``: label = ChEMBL ``pref_name``; molecule type, first approval year, max
  phase, ATC codes (WHOCC, flagged non-commercial), route flags, boxed-warning and withdrawn flags, parent molecule
  (salt / form -> parent, family context).
* ``chembl:<molecule> --chembl:mechanism_target--> chembl:<target>`` (family interacts_with, subtype targets),
  statement id ``mec:<mec_id>``, qualifiers action type and mechanism text verbatim, references (DailyMed,
  PubMed, FDA, ...). A mechanism is what ChEMBL curates as the molecule's mode of action, not a causal claim
  about a disease.
* ``chembl:<target>`` kind ``drug_target`` -> ``chembl:target_component`` -> ``uniprot:<accession>`` (has_part)
  and ``chembl:target_organism`` -> ``ncbitaxon:<tax_id>`` (context). UniProt / NCBI entities are V4BIO's.
* ``chembl:<molecule> --chembl:indication--> <EFO/MONDO/DOID/HP/Orphanet term>`` and ``--chembl:indication_mesh-->
  mesh:<D...>`` (family associated_with): ChEMBL's approved indications (max_phase_for_ind 4) with the references
  ChEMBL lists. An indication is an association; efficacy is never inferred from it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from reva.adapters.connectors import drugs_common as dc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.chembl_drugs"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_drugs_chembl/1"
SOURCE_KEY = "chembl.api"
DATASET = "drugs"
API_HOST = "www.ebi.ac.uk"
FTP_HOST = "ftp.ebi.ac.uk"
API = f"https://{API_HOST}/chembl/api/data"
STATUS_URL = f"{API}/status.json"
MAPPING_URL = f"https://{FTP_HOST}/pub/databases/chembl/ChEMBLdb/latest/chembl_uniprot_mapping.txt"
PAGE = 1000
TARGET_CHUNK = 100
MAX_BYTES = 12_000_000
MIN_MOLECULES_LIVE = 3_000
RIGHTS = {
    "licence": "CC BY-SA 3.0",
    "licence_url": "https://creativecommons.org/licenses/by-sa/3.0/",
    "licence_sharealike": True,
    "terms_url": "https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/REQUIRED.ATTRIBUTION",
    "attribution": "ChEMBL (EMBL-EBI), release as stated per statement; Mendez D et al. ChEMBL: towards direct "
    "deposition of bioassay data. Nucleic Acids Res. 2019 47(D1):D930-D940. DOI: 10.1093/nar/gky1075",
}
MOL_FIELDS = (
    "molecule_chembl_id",
    "pref_name",
    "molecule_type",
    "first_approval",
    "max_phase",
    "atc_classifications",
    "molecule_hierarchy",
    "oral",
    "parenteral",
    "topical",
    "black_box_warning",
    "withdrawn_flag",
)
TARGET_FIELDS = ("target_chembl_id", "pref_name", "target_type", "organism", "tax_id")
#: Fixture mode replays a captured subset: aspirin, imatinib + its mesylate salt, metformin, abatacept, nicotine.
FIXTURE_MOLECULES = ("CHEMBL25", "CHEMBL941", "CHEMBL1642", "CHEMBL1431", "CHEMBL1201823", "CHEMBL3")

CHEMBL_ID_RE = re.compile(r"^CHEMBL[0-9]{1,12}$")
UNIPROT_RE = re.compile(r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$")
MESH_RE = re.compile(r"^[CD][0-9]{6,9}$")
ATC_RE = re.compile(r"^[A-Z][0-9]{2}[A-Z]{2}[0-9]{2}$")
#: Indication term prefixes (as ChEMBL's efo_id column publishes them) -> KG prefix. MONDO / DOID entities are
#: V4HEALTH's (not created here); the others are created with ChEMBL's efo_term as label.
TERM_PREFIX = {"EFO": "efo", "MONDO": "mondo", "DOID": "doid", "HP": "hp", "Orphanet": "orphanet", "GO": "go"}
EXTERNAL_DISEASE_PREFIXES = frozenset({"mondo", "doid"})
TERM_RE = re.compile(r"^(EFO|MONDO|DOID|HP|Orphanet|GO)[:_]([0-9]{1,10})$")
MAPPING_HEADER_RE = re.compile(r"^#\s*(chembl_[0-9]+)\s+target list", re.IGNORECASE)


# ============================================================================================== URLs
def _only(fields: Sequence[str]) -> str:
    return ",".join(fields)


def molecules_url(offset: int) -> str:
    return (
        f"{API}/molecule.json?max_phase=4&limit={PAGE}&offset={offset}&order_by=molecule_chembl_id"
        f"&only={_only(MOL_FIELDS)}"
    )


def molecules_ids_url(ids: Sequence[str]) -> str:
    return f"{API}/molecule.json?molecule_chembl_id__in={','.join(ids)}&limit={PAGE}&only={_only(MOL_FIELDS)}"


def mechanisms_url(offset: int) -> str:
    return f"{API}/mechanism.json?max_phase=4&limit={PAGE}&offset={offset}&order_by=mec_id"


def mechanisms_ids_url(ids: Sequence[str]) -> str:
    return f"{API}/mechanism.json?molecule_chembl_id__in={','.join(ids)}&limit={PAGE}&order_by=mec_id"


def indications_url(offset: int) -> str:
    return f"{API}/drug_indication.json?max_phase_for_ind=4&limit={PAGE}&offset={offset}&order_by=drugind_id"


def indications_ids_url(ids: Sequence[str]) -> str:
    return (
        f"{API}/drug_indication.json?molecule_chembl_id__in={','.join(ids)}&max_phase_for_ind=4"
        f"&limit={PAGE}&order_by=drugind_id"
    )


def targets_url(ids: Sequence[str]) -> str:
    return f"{API}/target.json?target_chembl_id__in={','.join(ids)}&limit={PAGE}&only={_only(TARGET_FIELDS)}"


# ============================================================================================== parsing
def parse_status(body: bytes) -> dict[str, Any]:
    d = dc.json_body(body, "ChEMBL status")
    if not isinstance(d, dict) or not isinstance(d.get("chembl_db_version"), str):
        raise dc.err("ChEMBL status: no chembl_db_version")
    if d.get("status") != "UP":
        raise dc.err(f"ChEMBL status: service status {d.get('status')!r}", kind="unavailable")
    return d


def parse_page(body: bytes, key: str, what: str) -> tuple[list[dict[str, Any]], int, str | None]:
    """(rows, total_count, next) of one ChEMBL list page."""
    d = dc.json_body(body, what)
    if not isinstance(d, dict) or not isinstance(d.get(key), list) or not isinstance(d.get("page_meta"), dict):
        raise dc.err(f"{what}: no '{key}' list / page_meta")
    pm = d["page_meta"]
    total = dc.as_count(pm.get("total_count"), f"{what} total_count")
    rows = d[key]
    if not all(isinstance(r, dict) for r in rows):
        raise dc.err(f"{what}: rows are not objects")
    return rows, total, pm.get("next")


def parse_mapping(body: bytes) -> tuple[str, list[tuple[str, str, str, str]]]:
    """(release, [(uniprot accession, target id, target name, target type)]) of chembl_uniprot_mapping.txt."""
    try:
        lines = body.decode("utf-8").splitlines()
    except UnicodeDecodeError as e:
        raise dc.err("chembl_uniprot_mapping.txt is not UTF-8") from e
    if not lines:
        raise dc.err("chembl_uniprot_mapping.txt is empty")
    m = MAPPING_HEADER_RE.match(lines[0])
    if not m:
        raise dc.err(f"chembl_uniprot_mapping.txt: unexpected header {lines[0][:80]!r}")
    out = []
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise dc.err(f"chembl_uniprot_mapping.txt line {n}: {len(parts)} columns")
        acc, tid, name, ttype = (p.strip() for p in parts)
        if not UNIPROT_RE.match(acc) or not CHEMBL_ID_RE.match(tid):
            raise dc.err(f"chembl_uniprot_mapping.txt line {n}: not an accession / target id")
        out.append((acc, tid, name, ttype))
    return m.group(1).lower(), out


def term_qid(efo_id: str | None) -> str | None:
    if not efo_id:
        return None
    m = TERM_RE.match(efo_id.strip())
    if not m:
        return None
    return f"{TERM_PREFIX[m.group(1)]}:{m.group(2)}"


def _refs(refs: Any, cap: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in (refs or [])[:cap]:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("ref_id") or "")
        item: dict[str, Any] = {"ref_type": r.get("ref_type"), "ref_id": rid[:300]}
        if rid.count(",") and str(r.get("ref_type")) == "ClinicalTrials":
            item["nct_ids_listed"] = rid.count(",") + 1
        if len(rid) > 300:
            item["ref_id_truncated"] = True
        url = str(r.get("ref_url") or "")
        if url and len(url) <= 300:
            item["ref_url"] = url
        out.append(item)
    if refs and len(refs) > cap:
        out.append({"more_references": len(refs) - cap})
    return out


# ============================================================================================== build
def build(
    *,
    release: str,
    molecules: Sequence[Mapping[str, Any]],
    mechanisms: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    indications: Sequence[Mapping[str, Any]],
    mapping: Sequence[tuple[str, str, str, str]] | None,
    raws: Mapping[str, str],
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """(entities, statements, report). ``raws`` maps a part (molecules, mechanisms, targets, indications, mapping)
    to the raw sha256 of the page each row came from (row ``_raw``) or of the whole part."""
    from reva.kg.model import KgEntity, KgStatement

    ents: dict[str, Any] = {}
    sts: list[Any] = []
    skipped: Counter[str] = Counter()
    ref = {"stated_in": f"ChEMBL web services, release {release}"}

    def st(subject: str, pid: str, *, raw: str, obj: str | None = None, value: Any = None,
           quals: Mapping[str, Any] | None = None, sid: str | None = None, sel: Mapping[str, Any] | None = None,
           point: Any = None) -> None:
        sts.append(
            KgStatement(
                subject_qid=subject,
                property_pid=pid,
                object_qid=obj,
                value=value,
                qualifiers={k: v for k, v in (quals or {}).items() if v is not None and v != ""},
                references=[{**ref, "selector": dict(sel or {})}],
                statement_id=sid,
                source_revision=release,
                raw_sha256=raw,
                point_in_time=point,
            )
        )

    # ---------------------------------------------------------------- molecules
    approved: set[str] = set()
    for m in molecules:
        mid = str(m.get("molecule_chembl_id") or "")
        if not CHEMBL_ID_RE.match(mid):
            skipped["molecule_bad_id"] += 1
            continue
        raw = str(m.get("_raw") or raws["molecules"])
        qid = f"chembl:{mid}"
        approved.add(mid)
        name = m.get("pref_name")
        ents[qid] = KgEntity(
            qid=qid,
            kind=dc.KIND_DRUG,
            labels={"en": str(name)} if name else {},
            descriptions={"en": f"ChEMBL molecule {mid} (max_phase {m.get('max_phase')}, {release})"},
            source_revision=release,
        )
        sel = {"endpoint": "molecule", "molecule_chembl_id": mid}
        if m.get("molecule_type"):
            st(qid, "chembl:molecule_type", raw=raw, value=dc.string_value(str(m["molecule_type"])), sel=sel)
        fa = m.get("first_approval")
        if isinstance(fa, int) and not isinstance(fa, bool) and 1800 <= fa <= 2100:
            st(qid, "chembl:first_approval", raw=raw, value=dc.year_value(fa), sel=sel)
        mp = m.get("max_phase")
        if mp is not None and re.match(r"^[0-9](\.[0-9])?$", str(mp)):
            st(qid, "chembl:max_phase", raw=raw, value={"type": "quantity", "amount": str(mp), "unit": "phase"},
               sel=sel)
        for code in m.get("atc_classifications") or []:
            if isinstance(code, str) and ATC_RE.match(code):
                st(qid, "chembl:atc_code", raw=raw, value=dc.external_id(code), quals=dc.ATC_FLAGS, sel=sel)
            else:
                skipped["atc_not_level5_code"] += 1
        for route in ("oral", "parenteral", "topical"):
            if m.get(route) is True:
                st(qid, "chembl:route", raw=raw, value=dc.string_value(route), sel={**sel, "field": route})
        if m.get("black_box_warning") == 1:
            st(qid, "chembl:black_box_warning", raw=raw, value=dc.string_value("yes"), sel=sel,
               quals={"definition": "ChEMBL black_box_warning = 1: the drug has a black box warning on its label"})
        if m.get("withdrawn_flag") is True:
            st(qid, "chembl:withdrawn", raw=raw, value=dc.string_value("yes"), sel=sel,
               quals={"definition": "ChEMBL withdrawn_flag: withdrawn in at least one country (not necessarily US)"})
        h = m.get("molecule_hierarchy") or {}
        parent = h.get("parent_chembl_id") if isinstance(h, dict) else None
        if isinstance(parent, str) and CHEMBL_ID_RE.match(parent) and parent != mid:
            st(qid, "chembl:parent_molecule", raw=raw, obj=f"chembl:{parent}", sel=sel)

    # ---------------------------------------------------------------- mechanisms
    target_ids: set[str] = set()
    for r in mechanisms:
        mid, tid = r.get("molecule_chembl_id"), r.get("target_chembl_id")
        if not isinstance(mid, str) or not CHEMBL_ID_RE.match(mid):
            skipped["mechanism_bad_molecule"] += 1
            continue
        if not isinstance(tid, str) or not CHEMBL_ID_RE.match(tid):
            skipped["mechanism_without_target"] += 1
            continue
        target_ids.add(tid)
        quals = {
            "action_type": r.get("action_type"),
            "mechanism_of_action": r.get("mechanism_of_action"),
            "direct_interaction": r.get("direct_interaction"),
            "molecular_mechanism": r.get("molecular_mechanism"),
            "disease_efficacy": r.get("disease_efficacy"),
            "max_phase": r.get("max_phase"),
            "mechanism_comment": r.get("mechanism_comment"),
            "selectivity_comment": r.get("selectivity_comment"),
            "binding_site_comment": r.get("binding_site_comment"),
            "references": _refs(r.get("mechanism_refs")),
            "note": "ChEMBL-curated mode of action of the drug; not a causal claim about any disease",
        }
        st(f"chembl:{mid}", "chembl:mechanism_target", raw=str(r.get("_raw") or raws["mechanisms"]),
           obj=f"chembl:{tid}", quals=quals, sid=f"mec:{r.get('mec_id')}",
           sel={"endpoint": "mechanism", "mec_id": r.get("mec_id")})

    # ---------------------------------------------------------------- targets (+ UniProt components)
    seen_t: set[str] = set()
    for t in targets:
        tid = str(t.get("target_chembl_id") or "")
        if not CHEMBL_ID_RE.match(tid):
            skipped["target_bad_id"] += 1
            continue
        seen_t.add(tid)
        raw = str(t.get("_raw") or raws["targets"])
        qid = f"chembl:{tid}"
        ents[qid] = KgEntity(
            qid=qid,
            kind=dc.KIND_TARGET,
            labels={"en": str(t["pref_name"])} if t.get("pref_name") else {},
            descriptions={"en": f"ChEMBL target {tid} ({t.get('target_type')}, {t.get('organism')}; {release})"},
            source_revision=release,
        )
        sel = {"endpoint": "target", "target_chembl_id": tid}
        if t.get("target_type"):
            st(qid, "chembl:target_type", raw=raw, value=dc.string_value(str(t["target_type"])), sel=sel)
        tax = t.get("tax_id")
        if isinstance(tax, int) and not isinstance(tax, bool) and tax > 0:
            st(qid, "chembl:target_organism", raw=raw, obj=f"ncbitaxon:{tax}", sel=sel,
               quals={"organism_as_written": t.get("organism")})
    missing_targets = sorted(target_ids - seen_t)
    components = 0
    if mapping is not None:
        for line, (acc, tid, name, ttype) in enumerate(mapping, start=2):
            if tid not in target_ids:
                continue
            components += 1
            st(f"chembl:{tid}", "chembl:target_component", raw=raws["mapping"], obj=f"uniprot:{acc}",
               quals={"target_name_as_written": name, "target_type_as_written": ttype},
               sel={"file": "chembl_uniprot_mapping.txt", "line": line})

    # ---------------------------------------------------------------- indications
    mesh_labels: dict[str, str] = {}
    mesh_conflicts = 0
    by_prefix: Counter[str] = Counter()
    for r in indications:
        mid = r.get("molecule_chembl_id")
        if not isinstance(mid, str) or not CHEMBL_ID_RE.match(mid):
            skipped["indication_bad_molecule"] += 1
            continue
        raw = str(r.get("_raw") or raws["indications"])
        did = r.get("drugind_id")
        base = {
            "max_phase_for_ind": r.get("max_phase_for_ind"),
            "efo_id": r.get("efo_id"),
            "efo_term_as_written": r.get("efo_term"),
            "mesh_id": r.get("mesh_id"),
            "mesh_heading_as_written": r.get("mesh_heading"),
            "parent_molecule_chembl_id": r.get("parent_molecule_chembl_id"),
            "references": _refs(r.get("indication_refs")),
            "note": "approved indication as curated by ChEMBL (association; not an efficacy estimate)",
        }
        sel = {"endpoint": "drug_indication", "drugind_id": did}
        tq = term_qid(r.get("efo_id"))
        if tq is None:
            skipped["indication_term_not_identified" if r.get("efo_id") else "indication_without_efo_id"] += 1
        else:
            prefix = tq.split(":", 1)[0]
            by_prefix[prefix] += 1
            if prefix not in EXTERNAL_DISEASE_PREFIXES and tq not in ents and r.get("efo_term"):
                ents[tq] = KgEntity(
                    qid=tq,
                    kind=dc.KIND_DISEASE_TERM,
                    labels={"en": str(r["efo_term"])},
                    descriptions={"en": f"{r.get('efo_id')} as labelled in ChEMBL drug_indication ({release})"},
                    source_revision=release,
                )
            st(f"chembl:{mid}", "chembl:indication", raw=raw, obj=tq, quals=base, sid=f"drugind:{did}", sel=sel)
        mesh = r.get("mesh_id")
        if isinstance(mesh, str) and MESH_RE.match(mesh):
            mq = f"mesh:{mesh}"
            heading = str(r.get("mesh_heading") or "")
            if mq not in mesh_labels and heading:
                mesh_labels[mq] = heading
                ents[mq] = KgEntity(
                    qid=mq,
                    kind=dc.KIND_MESH,
                    labels={"en": heading},
                    descriptions={"en": f"MeSH descriptor {mesh} (heading as published by ChEMBL, {release})"},
                    source_revision=release,
                )
            elif heading and mesh_labels.get(mq) != heading:
                mesh_conflicts += 1
            st(f"chembl:{mid}", "chembl:indication_mesh", raw=raw, obj=mq, quals=base, sid=f"drugind:{did}:mesh",
               sel=sel)
        elif mesh:
            skipped["indication_mesh_not_descriptor"] += 1

    rep = {
        "release": release,
        "molecules": len(approved),
        "mechanisms": sum(1 for s in sts if s.property_pid == "chembl:mechanism_target"),
        "targets": len(seen_t),
        "targets_missing_from_target_endpoint": missing_targets[:20],
        "target_components": components,
        "indications": sum(1 for s in sts if s.property_pid == "chembl:indication"),
        "indications_by_term_prefix": dict(by_prefix),
        "indication_mesh": sum(1 for s in sts if s.property_pid == "chembl:indication_mesh"),
        "mesh_heading_conflicts": mesh_conflicts,
        "statements": len(sts),
        "entities": len(ents),
        "skipped": dict(skipped),
    }
    return list(ents.values()), sts, rep


# ============================================================================================== run
async def _fetch(run: Any, url: str, key: str, *, media: tuple[str, ...] = ("application/json",)) -> tuple[bytes, str]:
    raw = await run.get(url, key=key, media_types=media, max_bytes=MAX_BYTES)
    sha = run.archive(raw)
    return run.store.get("raw", sha), sha


async def _paged(run: Any, first_url: Any, key: str, list_key: str, what: str) -> tuple[list[dict[str, Any]], list[str]]:
    """All pages of an offset-paginated list (``first_url(offset)``); the total must stay the same."""
    rows: list[dict[str, Any]] = []
    shas: list[str] = []
    body, sha = await _fetch(run, first_url(0), f"{key}:0")
    page, total, _ = parse_page(body, list_key, what)
    rows += [{**r, "_raw": sha} for r in page]
    shas.append(sha)
    for off in range(PAGE, total, PAGE):
        body, sha = await _fetch(run, first_url(off), f"{key}:{off}")
        page, t2, _ = parse_page(body, list_key, what)
        if t2 != total:
            raise dc.err(f"{what}: total_count changed during the run ({total} -> {t2})", kind="source_changed")
        rows += [{**r, "_raw": sha} for r in page]
        shas.append(sha)
    if len(rows) != total:
        raise dc.err(f"{what}: {len(rows)} rows for total_count {total}", kind="truncated")
    return rows, shas


async def _by_ids(run: Any, url_of: Any, ids: Sequence[str], key: str, list_key: str, what: str) -> tuple[
    list[dict[str, Any]], list[str]
]:
    rows: list[dict[str, Any]] = []
    shas: list[str] = []
    for i, chunk in enumerate(dc.chunks(sorted(ids), TARGET_CHUNK)):
        body, sha = await _fetch(run, url_of(chunk), f"{key}:{i}")
        page, total, nxt = parse_page(body, list_key, what)
        if nxt or total > PAGE:
            raise dc.err(f"{what}: id chunk returned more than one page ({total})", kind="truncated")
        rows += [{**r, "_raw": sha} for r in page]
        shas.append(sha)
    return rows, shas


def code_digest() -> str:
    import sys

    return dc.digest(sys.modules[__name__], dc)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source="chembl",
        provider="ChEMBL (EMBL-EBI) web services and release FTP",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); ShareAlike; {RIGHTS['attribution']}",
        allowed_hosts=(API_HOST, FTP_HOST),
        datasets=(DATASET,),
        dataset_ids=(f"{API}/molecule.json?max_phase=4", f"{API}/mechanism.json?max_phase=4",
                     f"{API}/drug_indication.json?max_phase_for_ind=4", MAPPING_URL),
        cadence_seconds=90 * 86400,
        planned_requests=60,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"molecule": list(MOL_FIELDS), "target": list(TARGET_FIELDS),
                        "mechanism": "all fields", "drug_indication": "all fields"},
        notes="KG only: approved drugs, mechanisms -> targets -> UniProt, approved indications (associations).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    ids = dc.opt_list(run.options.get("molecules"))
    if ids is None and run.origin == "test_fixture":
        ids = list(FIXTURE_MOLECULES)
    partial = ids is not None and run.origin == "live"
    if ids is not None and not all(CHEMBL_ID_RE.match(i) for i in ids):
        raise dc.err("option molecules: ChEMBL ids expected", kind="invalid_option")
    sraw = await run.get(STATUS_URL, key="chembl:status", max_bytes=MAX_BYTES)
    status_sha = run.archive(sraw)
    retrieved: datetime = sraw.retrieved_at
    status = parse_status(run.store.get("raw", status_sha))
    release = str(status["chembl_db_version"])
    raws: dict[str, list[str]] = {}
    if ids is None:
        molecules, raws["molecules"] = await _paged(run, molecules_url, "chembl:molecules", "molecules", "molecules")
        mechs, raws["mechanisms"] = await _paged(run, mechanisms_url, "chembl:mechanisms", "mechanisms",
                                                 "mechanisms")
        inds, raws["indications"] = await _paged(run, indications_url, "chembl:indications", "drug_indications",
                                                 "drug indications")
        if run.origin == "live" and len(molecules) < MIN_MOLECULES_LIVE:
            raise dc.err(f"only {len(molecules)} approved molecules: truncated?", kind="truncated")
    else:
        molecules, raws["molecules"] = await _by_ids(run, molecules_ids_url, ids, "chembl:molecules", "molecules",
                                                     "molecules")
        mechs, raws["mechanisms"] = await _by_ids(run, mechanisms_ids_url, ids, "chembl:mechanisms", "mechanisms",
                                                  "mechanisms")
        inds, raws["indications"] = await _by_ids(run, indications_ids_url, ids, "chembl:indications",
                                                  "drug_indications", "drug indications")
    tids = sorted({str(r["target_chembl_id"]) for r in mechs if isinstance(r.get("target_chembl_id"), str)})
    targets, raws["targets"] = await _by_ids(run, targets_url, tids, "chembl:targets", "targets", "targets")
    mbody, msha = await _fetch(run, MAPPING_URL, "chembl:uniprot_mapping", media=("text/plain",))
    map_release, mapping = parse_mapping(mbody)
    mapping_used: Sequence[tuple[str, str, str, str]] | None = mapping
    note = None
    if map_release != release.lower():
        mapping_used, note = None, f"mapping file is {map_release}, API is {release}: components not loaded"
    # the release must not change during the run
    body2, status_sha2 = await _fetch(run, STATUS_URL, "chembl:status:end")
    if str(parse_status(body2)["chembl_db_version"]) != release:
        raise dc.err("ChEMBL release changed during the run", kind="source_changed")
    all_raws = sorted({status_sha, status_sha2, msha, *[s for v in raws.values() for s in v]})
    part_raw = {k: v[0] if v else status_sha for k, v in raws.items()}
    part_raw["mapping"] = msha
    ents, sts, rep = build(release=release, molecules=molecules, mechanisms=mechs, targets=targets,
                           indications=inds, mapping=mapping_used, raws=part_raw)
    out: dict[str, Any] = {"release": release, "status": {k: status.get(k) for k in ("chembl_release_date",
                           "targets", "disinct_compounds")}, "partial": partial, "build": rep,
                           "mapping_note": note, "raw_count": len(all_raws)}
    if not dc.kg_available(run.engine):
        out["kg"] = {"status": "kg_unavailable"}
        return out
    out["kg"] = dc.load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=all_raws,
        retrieved_at=retrieved,
        origin=run.origin,
        parameters={"connector": NAME, "parser": PARSER_VERSION, "release": release, "partial": partial},
        prefixes=("chembl:",),
        retract=not partial,
    )
    return out


__all__ = ["NAME", "SOURCE_KEY", "atlas_ingest", "atlas_source", "build", "parse_mapping", "parse_page", "term_qid"]
