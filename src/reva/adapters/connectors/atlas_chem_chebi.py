"""ChEBI ontology subset (EMBL-EBI) -> KG + atlas (V4, agent V4CHEM).

Source
------
ChEBI release files on ``ftp.ebi.ac.uk/pub/databases/chebi`` (HTTPS), no account:

* ``ontology/chebi_lite.obo.gz`` (~7.9 MB): every ChEBI term with name, definition, ``is_a`` and the
  relationships (has part, has role, has functional parent, has parent hydride, conjugate acid/base, enantiomer,
  tautomer, substituent group); the header states the release (``data-version: chebi/256``) and the licence
  (``terms:license https://creativecommons.org/licenses/by/4.0/``);
* ``flat_files/chemical_data.tsv.gz`` (~3.8 MB): formula, net charge, average and monoisotopic mass per entity;
* PubChem's periodic table (``pubchem.ncbi.nlm.nih.gov/rest/pug/periodictable/JSON``) to resolve formula element
  symbols to ``element:<Z>`` and to compute mass shares.

Licence: CC BY 4.0 per the ontology header (the flat-files README says "CC Attribution-ShareAlike 4.0" in one
sentence and "Creative Commons Attribution 4.0" in its file list: recorded, owner to confirm; attribution names
the release).

Subset (not the 219k terms)
---------------------------
``registry/chem/seeds.yaml``: seeds (ChEBI ids) + entities ChEBI states ``has_role`` a listed role + ``is_a``
children of listed classes; then the ``is_a`` ancestors of all of them, and the one-hop targets of the seeds'
relationships with their ancestors. Statements: every ChEBI relation whose subject is in the subset and whose
object is in the subset; formula / mass / charge / definition of subset entities; ``chebi:formula_element``
(made_of) from plain formulas (symbols via the periodic table; mass share computed by REVA).

Kinds: ``chemical_role`` (is_a* role CHEBI:50906), ``mineral`` (is_a* mineral CHEBI:46662), ``isotope`` (atom
with an isotope-labelled formula such as ``[14C]``), ``chemical_element`` (is_a* atom CHEBI:33250), ``compound``
(has a formula), else ``chemical_class``.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import yaml

from reva.adapters.connectors.atlas_chem_common import (
    NOT_GEOGRAPHIC,
    PERIODIC_TABLE_URL,
    PUBCHEM_HOST,
    SEEDS_FILE,
    Element,
    atlas_decimal,
    atlas_frame,
    canon,
    code_digest,
    element_masses,
    err,
    kg_decimal,
    load_kg,
    mass_shares,
    parse_formula,
    parse_periodic_table,
    quantity,
    string_value,
    text_value,
    to_decimal,
    trim_registration,
)

CONNECTOR_NAME = "atlas.chem_chebi"
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas-chem-chebi-1"
SOURCE_KEY = "chebi.release"
ATLAS_SOURCE = "chebi"
DATASET = "composition"
HOST = "ftp.ebi.ac.uk"
OBO_URL = f"https://{HOST}/pub/databases/chebi/ontology/chebi_lite.obo.gz"
CHEMDATA_URL = f"https://{HOST}/pub/databases/chebi/flat_files/chemical_data.tsv.gz"
MAX_BYTES = 30_000_000
MIN_TERMS_LIVE = 150_000  # 219,012 in release 256
KG_PREFIXES = ("chebi:",)

ROLE = "CHEBI:50906"
MINERAL = "CHEBI:46662"
ATOM = "CHEBI:33250"

#: OBO relationship id -> REVA pid (ChEBI lite typedefs, release 256).
RELATIONS: dict[str, str] = {
    "is_a": "chebi:is_a",
    "BFO:0000051": "chebi:has_part",
    "RO:0000087": "chebi:has_role",
    "RO:0018038": "chebi:has_functional_parent",
    "RO:0018040": "chebi:has_parent_hydride",
    "RO:0018034": "chebi:is_conjugate_acid_of",
    "RO:0018033": "chebi:is_conjugate_base_of",
    "RO:0018039": "chebi:is_enantiomer_of",
    "RO:0018036": "chebi:is_tautomer_of",
    "RO:0018037": "chebi:is_substituent_group_from",
}
#: Relations whose one-hop targets of a seed are added to the subset.
EXPAND = ("BFO:0000051", "RO:0000087", "RO:0018038", "RO:0018040")

RIGHTS: dict[str, Any] = {
    "licence": "CC-BY-4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "ChEBI (Chemical Entities of Biological Interest), EMBL-EBI, https://www.ebi.ac.uk/chebi — release {release}",
    "licence_note": "ontology header: CC BY 4.0; flat_files/README mentions both CC BY-SA 4.0 and CC BY 4.0 (owner to confirm)",
}


@dataclass
class Term:
    id: str
    line: int
    name: str = ""
    definition: str | None = None
    is_a: list[str] = field(default_factory=list)
    rels: list[tuple[str, str]] = field(default_factory=list)  # (relation id, target)
    obsolete: bool = False


@dataclass
class Ontology:
    release: str
    licence: str | None
    terms: dict[str, Term]


@dataclass(frozen=True)
class ChemData:
    row: int
    formula: str
    charge: str
    mass: str
    monoisotopic_mass: str


@dataclass
class Seeds:
    seeds: list[dict[str, Any]]
    role_members: list[dict[str, Any]]
    class_members: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Any = SEEDS_FILE) -> Seeds:
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        return cls(doc.get("seeds") or [], doc.get("role_members") or [], doc.get("class_members") or [])


# ============================================================================================== parsing
def parse_obo(body: bytes, *, min_terms: int = MIN_TERMS_LIVE) -> Ontology:
    """Terms of a (gzipped) ChEBI OBO file. Only the fields REVA uses are read; unknown lines are ignored."""
    try:
        raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise err(f"ChEBI OBO: cannot decode ({e})") from None
    release = None
    licence = None
    terms: dict[str, Term] = {}
    cur: Term | None = None
    for n, line in enumerate(text.splitlines(), 1):
        if line == "[Term]":
            cur = Term("", n)
            continue
        if line.startswith("["):
            cur = None
            continue
        if cur is None:
            if line.startswith("data-version: "):
                release = line.split(":", 1)[1].strip()
            elif line.startswith("property_value: terms:license "):
                licence = line.split()[2]
            continue
        if line.startswith("id: "):
            cur.id = line[4:].strip()
            terms[cur.id] = cur
        elif line.startswith("name: "):
            cur.name = line[6:]
        elif line.startswith("def: "):
            m = re.match(r'^def: "((?:[^"\\]|\\.)*)"', line)
            if m:
                cur.definition = m.group(1).replace('\\"', '"')
        elif line.startswith("is_a: "):
            cur.is_a.append(line[6:].split(" ", 1)[0])
        elif line.startswith("relationship: "):
            parts = line.split(" ")
            if len(parts) >= 3:
                cur.rels.append((parts[1], parts[2]))
        elif line.startswith("is_obsolete: true"):
            cur.obsolete = True
    if release is None or not release.startswith("chebi/"):
        raise err(f"ChEBI OBO: no data-version header ({release!r})")
    if len(terms) < min_terms:
        raise err(f"ChEBI OBO: only {len(terms)} terms", kind="incomplete")
    return Ontology(release.split("/", 1)[1], licence, terms)


CHEMDATA_COLUMNS = (
    "id",
    "compound_id",
    "formula",
    "charge",
    "mass",
    "monoisotopic_mass",
    "status_id",
    "structure_id",
    "is_autogenerated",
)


def parse_chemical_data(body: bytes, wanted: set[int] | None = None) -> dict[int, ChemData]:
    raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
    rd = csv.reader(io.StringIO(raw.decode("utf-8")), delimiter="\t")
    header = next(rd, None)
    if header is None or tuple(header) != CHEMDATA_COLUMNS:
        raise err(f"ChEBI chemical_data: columns changed: {header}")
    out: dict[int, ChemData] = {}
    for i, r in enumerate(rd, 2):
        if len(r) != len(CHEMDATA_COLUMNS) or not r[1].isdigit():
            raise err(f"ChEBI chemical_data: line {i} malformed")
        cid = int(r[1])
        if wanted is not None and cid not in wanted:
            continue
        if cid in out:
            raise err(f"ChEBI chemical_data: compound {cid} twice (line {i})")
        out[cid] = ChemData(i, r[2], r[3], r[4], r[5])
    return out


# ============================================================================================== subset
def _num(cid: str) -> int:
    return int(cid.split(":", 1)[1])


def ancestors(onto: Ontology, ids: Iterable[str]) -> set[str]:
    out: set[str] = set()
    stack = list(ids)
    while stack:
        t = onto.terms.get(stack.pop())
        if t is None:
            continue
        for p in t.is_a:
            if p not in out:
                out.add(p)
                stack.append(p)
    return out


def select_subset(onto: Ontology, seeds: Seeds) -> tuple[set[str], dict[str, Any]]:
    """The subset of ChEBI ids (see module doc) and a report (unknown seeds, name mismatches, counts)."""
    report: dict[str, Any] = {"unknown_seeds": [], "name_mismatch": [], "obsolete_seeds": []}

    def check(entry: Mapping[str, Any]) -> str | None:
        cid = f"CHEBI:{int(entry['chebi'])}"
        t = onto.terms.get(cid)
        if t is None:
            report["unknown_seeds"].append(cid)
            return None
        if t.obsolete:
            report["obsolete_seeds"].append(cid)
        if entry.get("name") and str(entry["name"]).lower() != t.name.lower():
            report["name_mismatch"].append({"chebi": cid, "seeds_file": entry["name"], "chebi_name": t.name})
        return cid

    core: set[str] = set()
    for e in seeds.seeds:
        if (c := check(e)) is not None:
            core.add(c)
    role_of: dict[str, set[str]] = defaultdict(set)
    children: dict[str, set[str]] = defaultdict(set)
    for t in onto.terms.values():
        if t.obsolete:
            continue
        for p in t.is_a:
            children[p].add(t.id)
        for r, o in t.rels:
            if r == "RO:0000087":
                role_of[o].add(t.id)
    added_roles: set[str] = set()
    for e in seeds.role_members:
        if (c := check(e)) is not None:
            added_roles |= role_of.get(c, set())
            core.add(c)
    added_classes: set[str] = set()
    for e in seeds.class_members:
        c = check(e)
        if c is None:
            continue
        core.add(c)
        frontier = {c}
        depth = e.get("depth", 1)
        while frontier:
            nxt = set().union(*(children.get(x, set()) for x in frontier)) - added_classes
            added_classes |= nxt
            frontier = nxt if depth == "all" else set()
    members = core | added_roles | added_classes
    targets = {o for s in core if (tt := onto.terms.get(s)) is not None for r, o in tt.rels if r in EXPAND}
    subset = members | targets
    subset |= ancestors(onto, subset)
    subset = {s for s in subset if s in onto.terms}
    report.update(
        seeds=len(core),
        role_members=len(added_roles),
        class_members=len(added_classes),
        relation_targets=len(targets),
        subset=len(subset),
    )
    return subset, report


def chebi_kind(onto: Ontology, cid: str, chem: Mapping[int, ChemData]) -> str:
    anc = ancestors(onto, [cid]) | {cid}
    if ROLE in anc:
        return "chemical_role"
    if MINERAL in anc:
        return "mineral"
    cd = chem.get(_num(cid))
    if ATOM in anc:
        if cd is not None and re.match(r"^\[[0-9]+[A-Z][a-z]?\]$", cd.formula):
            return "isotope"
        return "chemical_element"
    if cd is not None and cd.formula and not GENERIC_FORMULA_RE.search(cd.formula):
        return "compound"
    return "chemical_class"  # no formula, or a generic one (alkane "CH3R", polymer "(C2H4)n")


#: Markers of a class formula: R/X groups (not Rn, Rb, Xe, ...), wildcards, polymer "n", isotope brackets.
GENERIC_FORMULA_RE = re.compile(r"R(?![a-z])|X(?![a-z])|\*|\)n|[0-9]n\b|\bn\b|\[|\]")


def key(cid: str) -> str:
    return f"chebi:{_num(cid)}"


# ============================================================================================== connector
def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="ChEBI, EMBL-EBI (ontology + chemical data release files); PubChem periodic table for element symbols",
        licence="CC-BY-4.0",
        allowed_hosts=(HOST, PUBCHEM_HOST),
        datasets=(DATASET,),
        dataset_ids=("ontology/chebi_lite.obo.gz", "flat_files/chemical_data.tsv.gz"),
        cadence_seconds=30 * 86400,
        planned_requests=4,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__], sys.modules["reva.adapters.connectors.atlas_chem_common"]),
        field_mappings={
            "atlas": f"{ATLAS_SOURCE}.{DATASET}",
            "kg": list(KG_PREFIXES),
            "seeds": "registry/chem/seeds.yaml",
        },
        notes="3 requests (~12 MB): chebi_lite.obo.gz, chemical_data.tsv.gz, PubChem periodic table",
    )


async def atlas_ingest(run: Any, *, load: bool = True) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": DATASET}
    min_terms = 1 if run.origin == "test_fixture" else MIN_TERMS_LIVE
    obo_raw = await run.get(
        OBO_URL,
        key="chebi_lite.obo.gz",
        media_types=("application/x-gzip", "application/gzip", "application/octet-stream"),
        max_bytes=MAX_BYTES,
    )
    obo_sha = run.archive(obo_raw)
    cd_raw = await run.get(
        CHEMDATA_URL,
        key="chemical_data.tsv.gz",
        media_types=("application/x-gzip", "application/gzip", "application/octet-stream"),
        max_bytes=MAX_BYTES,
    )
    cd_sha = run.archive(cd_raw)
    pt_raw = await run.get(PERIODIC_TABLE_URL, key="periodictable", max_bytes=1_000_000)
    pt_sha = run.archive(pt_raw)
    onto = parse_obo(obo_raw.body, min_terms=min_terms)
    elements = parse_periodic_table(pt_raw.body)
    opt = run.options.get("chebi_ids") if isinstance(run.options, Mapping) else None
    # option chebi_ids (tests, partial runs): only these seeds, no role/class member expansion, no retraction
    seeds = Seeds([{"chebi": int(x)} for x in opt], [], []) if opt else Seeds.load()
    subset, rep = select_subset(onto, seeds)
    chem = parse_chemical_data(cd_raw.body, {_num(c) for c in subset})
    shas = {"obo": obo_sha, "chemdata": cd_sha, "periodic_table": pt_sha}
    retrieved_at = obo_raw.retrieved_at
    comp = compositions(subset, chem, elements)
    out: dict[str, Any] = {
        "release": onto.release,
        "licence_in_header": onto.licence,
        "subset": rep,
        "formulas_unparsed": comp.unparsed_count(),
    }
    rows = atlas_rows(onto, subset, chem, comp, shas=shas, retrieved_at=retrieved_at)
    reg = run.register(
        dataset=DATASET,
        frame=atlas_frame(rows),
        raw_sha256s=list(shas.values()),
        coverage_note=f"Masses and element mass shares of the ChEBI subset (release {onto.release}, registry/chem/seeds.yaml).",
        rights={**RIGHTS, "attribution": RIGHTS["attribution"].format(release=onto.release)},
    )
    out["atlas"] = trim_registration(reg)
    if load:
        ents, stmts = kg_build(onto, subset, chem, comp, elements, shas=shas, retrieved_at=retrieved_at)
        out["kg"] = load_kg(
            run.engine,
            run.store,
            entities=ents,
            statements=stmts,
            source_key=SOURCE_KEY,
            raw_sha256s=list(shas.values()),
            retrieved_at=retrieved_at,
            origin=run.origin,
            parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "release": onto.release},
            prefixes=KG_PREFIXES,
            retract_prefix=None if opt else "",
        )
    return out


# ============================================================================================== composition
@dataclass
class Compositions:
    counts: dict[str, dict[str, int]] = field(default_factory=dict)  # chebi id -> symbol -> count
    shares: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    unparsed: dict[str, str] = field(default_factory=dict)  # chebi id -> reason

    def unparsed_count(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for r in self.unparsed.values():
            out[r.split(":", 1)[0]] += 1
        return dict(out)


def compositions(subset: Iterable[str], chem: Mapping[int, ChemData], elements: Sequence[Element]) -> Compositions:
    symbols = {e.symbol for e in elements}
    masses = element_masses(elements)
    comp = Compositions()
    for cid in sorted(subset):
        cd = chem.get(_num(cid))
        if cd is None or not cd.formula:
            continue
        counts, why = parse_formula(cd.formula, symbols)
        if counts is None:
            comp.unparsed[cid] = why or "unparsed"
            continue
        comp.counts[cid] = counts
        sh = mass_shares(counts, masses)
        if sh is not None:
            comp.shares[cid] = sh
    return comp


# ============================================================================================== atlas + KG
def atlas_rows(
    onto: Ontology,
    subset: Iterable[str],
    chem: Mapping[int, ChemData],
    comp: Compositions,
    *,
    shas: Mapping[str, str],
    retrieved_at: datetime,
) -> list[dict[str, Any]]:
    y = retrieved_at.year
    base_flags = {
        "release": onto.release,
        "retrieved_at": retrieved_at.isoformat(),
        "period_semantics": "value as published in the ChEBI release (not a measurement year)",
    }
    out: list[dict[str, Any]] = []

    def row(ind: str, value: str, unit: str, flags: Mapping[str, Any], sha: str, sel: Mapping[str, Any]) -> None:
        out.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": DATASET,
                "indicator": ind,
                "concept_id": None,
                "iso3": None,
                "provider_geo": NOT_GEOGRAPHIC,
                "entity_qid": None,
                "period_start": date(y, 1, 1),
                "period_end": date(y + 1, 1, 1),
                "value": value,
                "value_text": None,
                "unit": unit,
                "flags": canon({**base_flags, **flags}),
                "raw_sha256": sha,
                "selector": canon(sel),
            }
        )

    for cid in sorted(subset, key=_num):
        cd = chem.get(_num(cid))
        if cd is None:
            continue
        n = _num(cid)
        m = to_decimal(cd.mass)
        if m is not None:
            row(
                f"mass|chebi={n}",
                atlas_decimal(m),
                "Da",
                {"chebi": cid, "formula": cd.formula},
                shas["chemdata"],
                {"file": "chemical_data.tsv", "line": cd.row, "column": "mass"},
            )
        for sym, share in sorted(comp.shares.get(cid, {}).items()):
            row(
                f"element_mass_share|chebi={n}|element={sym}",
                atlas_decimal(share),
                "mass fraction",
                {
                    "chebi": cid,
                    "formula": cd.formula,
                    "atom_count": comp.counts[cid][sym],
                    "computed_by": "reva",
                    "method": "atom_count x PubChem periodic-table atomic mass / formula total",
                    "periodic_table_sha256": shas["periodic_table"],
                },
                shas["chemdata"],
                {"file": "chemical_data.tsv", "line": cd.row, "column": "formula"},
            )
    return out


def kg_build(
    onto: Ontology,
    subset: set[str],
    chem: Mapping[int, ChemData],
    comp: Compositions,
    elements: Sequence[Element],
    *,
    shas: Mapping[str, str],
    retrieved_at: datetime,
) -> tuple[list[Any], list[Any]]:
    from reva.kg.model import KgEntity, KgStatement

    rev = f"chebi:{onto.release}"
    z_of = {e.symbol: e.z for e in elements}
    ents: list[Any] = []
    stmts: list[Any] = []

    def ref(cid: str, sel: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        return (
            {
                "P854": f"https://www.ebi.ac.uk/chebi/searchId.do?chebiId={cid}",
                "P813": retrieved_at.date().isoformat(),
                "publisher": f"ChEBI release {onto.release} (EMBL-EBI, CC BY 4.0)",
                "selector": dict(sel),
            },
        )

    def st(
        sid: str,
        subj: str,
        pid: str,
        sha: str,
        refs: Sequence[Mapping[str, Any]],
        *,
        obj: str | None = None,
        value: Mapping[str, Any] | None = None,
        quals: Mapping[str, Any] | None = None,
    ) -> None:
        stmts.append(
            KgStatement(
                subject_qid=subj,
                property_pid=pid,
                object_qid=obj,
                value=value,
                qualifiers=dict(quals or {}),
                references=tuple(refs),
                source_key=SOURCE_KEY,
                source_revision=rev,
                statement_id=sid,
                raw_sha256=sha,
            )
        )

    for cid in sorted(subset, key=_num):
        t = onto.terms[cid]
        k = key(cid)
        n = _num(cid)
        ents.append(
            KgEntity(
                qid=k,
                kind=chebi_kind(onto, cid, chem),
                labels={"en": t.name or cid},
                descriptions={"en": t.definition[:500]} if t.definition else {},
                aliases={"en": [cid]},
                source_revision=rev,
            )
        )
        osel = {"file": "chebi_lite.obo", "line": t.line, "id": cid}
        if t.definition:
            st(
                f"{n}:def",
                k,
                "chebi:definition",
                shas["obo"],
                ref(cid, osel),
                value=text_value(t.definition),
                quals={"verbatim": True},
            )
        for p in t.is_a:
            if p in subset:
                st(f"{n}:is_a:{_num(p)}", k, "chebi:is_a", shas["obo"], ref(cid, osel), obj=key(p))
        for r, o in t.rels:
            pid = RELATIONS.get(r)
            if pid is None or o not in subset or not o.startswith("CHEBI:"):
                continue
            st(f"{n}:{r}:{_num(o)}", k, pid, shas["obo"], ref(cid, osel), obj=key(o), quals={"obo_relation": r})
        cd = chem.get(n)
        if cd is None:
            continue
        csel = {"file": "chemical_data.tsv", "line": cd.row}
        if cd.formula:
            st(f"{n}:formula", k, "chebi:formula", shas["chemdata"], ref(cid, csel), value=string_value(cd.formula))
        if (m := to_decimal(cd.mass)) is not None:
            st(f"{n}:mass", k, "chebi:mass", shas["chemdata"], ref(cid, csel), value=quantity(m, "Da"))
        if (c := to_decimal(cd.charge)) is not None:
            st(f"{n}:charge", k, "chebi:charge", shas["chemdata"], ref(cid, csel), value=quantity(c))
        for sym, cnt in sorted(comp.counts.get(cid, {}).items()):
            q: dict[str, Any] = {"atom_count": cnt, "formula": cd.formula, "symbol": sym}
            share = comp.shares.get(cid, {}).get(sym)
            if share is not None:
                q.update(mass_share=kg_decimal(share), computed_by="reva", periodic_table_sha256=shas["periodic_table"])
            st(
                f"{n}:element:{z_of[sym]}",
                k,
                "chebi:formula_element",
                shas["chemdata"],
                ref(cid, csel),
                obj=f"element:{z_of[sym]}",
                quals=q,
            )
    return ents, stmts


__all__ = [
    "CONNECTOR_NAME",
    "Ontology",
    "Seeds",
    "atlas_ingest",
    "atlas_rows",
    "atlas_source",
    "chebi_kind",
    "compositions",
    "kg_build",
    "parse_chemical_data",
    "parse_obo",
    "select_subset",
]
