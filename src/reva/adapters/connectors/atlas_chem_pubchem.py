"""PubChem compounds for the ChEBI seeds (PUG-REST) -> KG + atlas (V4, agent V4CHEM).

Source
------
PubChem PUG-REST (NCBI/NLM, no account; <= 5 requests/s):

1. ``/rest/pug/periodictable/JSON`` — element symbols and atomic masses (formula -> ``element:<Z>``, mass shares);
2. ``/rest/pug/substance/sourceid/ChEBI/CHEBI:a,CHEBI:b,.../xrefs/RegistryID/JSON`` — the SID PubChem gave each
   ChEBI deposit (source "ChEBI", registry id = the ChEBI accession);
3. ``/rest/pug/substance/sourceid/ChEBI/<same list>/cids/JSON`` — the CID(s) PubChem standardised each SID to;
4. ``/rest/pug/compound/cid/<cids>/property/Title,MolecularFormula,MolecularWeight,ExactMass,IUPACName,InChIKey,Charge/JSON``.

ChEBI -> CID is therefore PubChem's own identifier link (joined on SID), never a name match. A ChEBI id that
PubChem does not hold makes PUG-REST refuse the whole list (HTTP 400 "Invalid identifier"): the list is split in
halves until the offending ids are isolated; they are reported as ``not_in_pubchem`` (typed missing).

Which ChEBI ids: ``registry/chem/seeds.yaml`` entries without ``pubchem: false`` (option ``chebi_ids`` overrides).

Published: KG entities ``pubchem:cid:<n>`` (kind ``compound``, label = PubChem Title) with ``pubchem:*`` statements
(formula, molecular weight, exact mass, IUPAC name, InChIKey, formal charge), ``pubchem:formula_element``
(made_of, atom counts from the formula; mass_share computed by REVA) and ``pubchem:chebi_deposit`` (CID -> ChEBI).
Atlas ``pubchem.compounds``: molecular weight, exact mass and element mass shares per CID.

Licence: PubChem compound properties are computed by PubChem (NLM, US Government work); PubChem's data-use
policy asks for attribution and notes that depositors may have their own terms (RIGHTS below).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    to_decimal,
    trim_registration,
)
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.chem_pubchem"
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas-chem-pubchem-1"
SOURCE_KEY = "pubchem.pug"
ATLAS_SOURCE = "pubchem"
DATASET = "compounds"
BASE = f"https://{PUBCHEM_HOST}/rest/pug"
PROPERTIES = ("Title", "MolecularFormula", "MolecularWeight", "ExactMass", "IUPACName", "InChIKey", "Charge")
MAX_IDS_PER_REQUEST = 150
KG_PREFIXES = ("pubchem:",)

RIGHTS: dict[str, Any] = {
    "licence": "PubChem (NLM): computed properties are US Government work; attribution requested; depositor terms may apply",
    "licence_url": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
    "attribution": "PubChem, National Center for Biotechnology Information, https://pubchem.ncbi.nlm.nih.gov",
}


@dataclass(frozen=True)
class Compound:
    cid: int
    index: int  # position in PropertyTable.Properties
    props: Mapping[str, Any]

    @property
    def key(self) -> str:
        return f"pubchem:cid:{self.cid}"


def seed_chebi_ids(path: Any = SEEDS_FILE) -> list[int]:
    doc = yaml.safe_load(open(path, encoding="utf-8"))
    return [int(s["chebi"]) for s in doc.get("seeds") or [] if s.get("pubchem", True)]


def xrefs_url(ids: Sequence[int]) -> str:
    return f"{BASE}/substance/sourceid/ChEBI/{','.join(f'CHEBI:{i}' for i in ids)}/xrefs/RegistryID/JSON"


def cids_url(ids: Sequence[int]) -> str:
    return f"{BASE}/substance/sourceid/ChEBI/{','.join(f'CHEBI:{i}' for i in ids)}/cids/JSON"


def properties_url(cids: Sequence[int]) -> str:
    return f"{BASE}/compound/cid/{','.join(str(c) for c in cids)}/property/{','.join(PROPERTIES)}/JSON"


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="PubChem (NCBI/NLM) PUG-REST",
        licence=RIGHTS["licence"],
        allowed_hosts=(PUBCHEM_HOST,),
        datasets=(DATASET,),
        dataset_ids=("rest/pug/substance/sourceid/ChEBI", "rest/pug/compound/cid/property"),
        cadence_seconds=30 * 86400,
        planned_requests=40,
        max_response_bytes=5_000_000,
        min_interval_seconds=0.4,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__], sys.modules["reva.adapters.connectors.atlas_chem_common"]),
        field_mappings={
            "atlas": f"{ATLAS_SOURCE}.{DATASET}",
            "kg": list(KG_PREFIXES),
            "seeds": "registry/chem/seeds.yaml",
        },
        notes="~4 requests for the seed list (more when ids absent from PubChem force list splitting)",
    )


def _status(e: Exception) -> int | None:
    d = getattr(e, "details", None) or {}
    s = d.get("status") if isinstance(d, Mapping) else None
    return int(s) if isinstance(s, int) else None


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise err(f"{what}: not JSON ({e})") from None


async def _fetch_split(
    run: Any, ids: list[int], url_of: Any, key: str, acc: list[tuple[list[int], Any]], missing: list[int]
) -> None:
    """GET ``url_of(ids)``; on HTTP 400/404 split the list until single refused ids remain (reported missing)."""
    try:
        raw = await run.get(url_of(ids), key=f"{key}:{ids[0]}:{len(ids)}", max_bytes=5_000_000)
    except SourceResponseError as e:
        if _status(e) not in (400, 404):
            raise
        if len(ids) == 1:
            missing.append(ids[0])
            return
        mid = len(ids) // 2
        await _fetch_split(run, ids[:mid], url_of, key, acc, missing)
        await _fetch_split(run, ids[mid:], url_of, key, acc, missing)
        return
    if raw.response_metadata.get("status") in (400, 404):  # fixture replay of a refused list
        if len(ids) == 1:
            missing.append(ids[0])
            return
        mid = len(ids) // 2
        await _fetch_split(run, ids[:mid], url_of, key, acc, missing)
        await _fetch_split(run, ids[mid:], url_of, key, acc, missing)
        return
    acc.append((ids, raw))


async def atlas_ingest(run: Any, *, load: bool = True) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": DATASET}
    opt = run.options.get("chebi_ids") if isinstance(run.options, Mapping) else None
    chebi_ids = sorted({int(x) for x in opt}) if opt else sorted(set(seed_chebi_ids()))
    pt_raw = await run.get(PERIODIC_TABLE_URL, key="periodictable", max_bytes=1_000_000)
    pt_sha = run.archive(pt_raw)
    elements = parse_periodic_table(pt_raw.body)
    shas: list[str] = [pt_sha]
    sid_chebi: dict[int, tuple[int, str, int]] = {}  # sid -> (chebi, raw sha, index)
    sid_cids: dict[int, tuple[list[int], str, int]] = {}
    not_in_pubchem: list[int] = []
    for i in range(0, len(chebi_ids), MAX_IDS_PER_REQUEST):
        chunk = chebi_ids[i : i + MAX_IDS_PER_REQUEST]
        got: list[tuple[list[int], Any]] = []
        await _fetch_split(run, chunk, xrefs_url, "xrefs", got, not_in_pubchem)
        for _ids, raw in got:
            sha = run.archive(raw)
            shas.append(sha)
            for k, info in enumerate(_json(raw.body, "xrefs")["InformationList"]["Information"]):
                regs = [r for r in info.get("RegistryID") or [] if isinstance(r, str) and r.startswith("CHEBI:")]
                if len(regs) == 1 and regs[0][6:].isdigit():
                    sid_chebi[int(info["SID"])] = (int(regs[0][6:]), sha, k)
        ok = [c for c in chunk if c not in not_in_pubchem]
        if not ok:
            continue
        got = []
        await _fetch_split(run, ok, cids_url, "cids", got, [])
        for _ids, raw in got:
            sha = run.archive(raw)
            shas.append(sha)
            for k, info in enumerate(_json(raw.body, "cids")["InformationList"]["Information"]):
                sid_cids[int(info["SID"])] = ([int(c) for c in info.get("CID") or []], sha, k)
    links = chebi_cid_links(sid_chebi, sid_cids)
    cids = sorted({cid for lk in links for cid in [lk.cid]})
    compounds: list[Compound] = []
    prop_shas: dict[int, str] = {}
    for i in range(0, len(cids), MAX_IDS_PER_REQUEST):
        chunk = cids[i : i + MAX_IDS_PER_REQUEST]
        raw = await run.get(properties_url(chunk), key=f"properties:{chunk[0]}", max_bytes=5_000_000)
        sha = run.archive(raw)
        shas.append(sha)
        for c in parse_properties(raw.body):
            compounds.append(c)
            prop_shas[c.cid] = sha
    retrieved_at = pt_raw.retrieved_at
    seen_chebi = {lk.chebi for lk in links}
    out: dict[str, Any] = {
        "chebi_ids": len(chebi_ids),
        "not_in_pubchem": [f"CHEBI:{c}" for c in sorted(set(not_in_pubchem))],
        "without_standardised_cid": [
            f"CHEBI:{c}" for c in chebi_ids if c not in seen_chebi and c not in not_in_pubchem
        ],
        "links": len(links),
        "compounds": len(compounds),
    }
    rows = atlas_rows(compounds, elements, prop_shas=prop_shas, pt_sha=pt_sha, retrieved_at=retrieved_at)
    if rows:
        reg = run.register(
            dataset=DATASET,
            frame=atlas_frame(rows),
            raw_sha256s=sorted(set(prop_shas.values())),
            coverage_note="PubChem computed properties and element mass shares of the compounds PubChem links to the ChEBI seeds.",
            rights=RIGHTS,
        )
        out["atlas"] = trim_registration(reg)
    if load:
        ents, stmts = kg_build(
            compounds, links, elements, prop_shas=prop_shas, pt_sha=pt_sha, retrieved_at=retrieved_at
        )
        out["kg"] = load_kg(
            run.engine,
            run.store,
            entities=ents,
            statements=stmts,
            source_key=SOURCE_KEY,
            raw_sha256s=shas,
            retrieved_at=retrieved_at,
            origin=run.origin,
            parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "chebi_ids": len(chebi_ids)},
            prefixes=KG_PREFIXES,
            retract_prefix=None if opt else "cid:",
        )
    return out


@dataclass(frozen=True)
class Link:
    chebi: int
    sid: int
    cid: int
    xref_sha: str
    xref_index: int
    cid_sha: str
    cid_index: int
    n_cids: int


def chebi_cid_links(
    sid_chebi: Mapping[int, tuple[int, str, int]], sid_cids: Mapping[int, tuple[list[int], str, int]]
) -> list[Link]:
    """ChEBI -> CID through the SID PubChem assigned the ChEBI deposit. A SID standardised to several CIDs (mixtures)
    yields one link per CID (flagged ``n_cids``); a SID without a CID yields none."""
    out = []
    for sid, (chebi, xsha, xi) in sorted(sid_chebi.items()):
        if sid not in sid_cids:
            continue
        cids, csha, ci = sid_cids[sid]
        for cid in cids:
            out.append(Link(chebi, sid, cid, xsha, xi, csha, ci, len(cids)))
    return out


def parse_properties(body: bytes) -> list[Compound]:
    doc = _json(body, "properties")
    try:
        props = doc["PropertyTable"]["Properties"]
    except (KeyError, TypeError):
        raise err("properties: no PropertyTable") from None
    out = []
    for i, p in enumerate(props):
        if not isinstance(p, Mapping) or not isinstance(p.get("CID"), int):
            raise err(f"properties: row {i} without CID")
        out.append(Compound(int(p["CID"]), i, dict(p)))
    return out


def _composition(
    c: Compound, elements: Sequence[Element]
) -> tuple[dict[str, int] | None, dict[str, Decimal] | None, str | None]:
    f = c.props.get("MolecularFormula")
    if not isinstance(f, str):
        return None, None, "no formula"
    counts, why = parse_formula(f, {e.symbol for e in elements})
    if counts is None:
        return None, None, why
    return counts, mass_shares(counts, element_masses(elements)), None


def atlas_rows(
    compounds: Sequence[Compound],
    elements: Sequence[Element],
    *,
    prop_shas: Mapping[int, str],
    pt_sha: str,
    retrieved_at: datetime,
) -> list[dict[str, Any]]:
    y = retrieved_at.year
    out: list[dict[str, Any]] = []
    for c in compounds:
        sha = prop_shas[c.cid]
        base = {
            "cid": c.cid,
            "title": c.props.get("Title"),
            "formula": c.props.get("MolecularFormula"),
            "retrieved_at": retrieved_at.isoformat(),
            "period_semantics": "value as computed by PubChem at retrieval",
        }

        def row(
            ind: str,
            value: str,
            unit: str,
            flags: Mapping[str, Any],
            column: str,
            c: Compound = c,
            sha: str = sha,
            base: Mapping[str, Any] = base,
        ) -> None:
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
                    "flags": canon({**base, **flags}),
                    "raw_sha256": sha,
                    "selector": canon(
                        {"file": "properties.json", "path": f"PropertyTable.Properties[{c.index}].{column}"}
                    ),
                }
            )

        for col, ind, unit in (("MolecularWeight", "molecular_weight", "g/mol"), ("ExactMass", "exact_mass", "Da")):
            v = to_decimal(str(c.props.get(col))) if c.props.get(col) is not None else None
            if v is not None:
                row(f"{ind}|cid={c.cid}", atlas_decimal(v), unit, {"computed_by": "pubchem"}, col)
        counts, shares, _why = _composition(c, elements)
        for sym, share in sorted((shares or {}).items()):
            row(
                f"element_mass_share|cid={c.cid}|element={sym}",
                atlas_decimal(share),
                "mass fraction",
                {
                    "atom_count": (counts or {})[sym],
                    "computed_by": "reva",
                    "method": "atom_count x PubChem periodic-table atomic mass / formula total",
                    "periodic_table_sha256": pt_sha,
                },
                "MolecularFormula",
            )
    return out


def kg_build(
    compounds: Sequence[Compound],
    links: Sequence[Link],
    elements: Sequence[Element],
    *,
    prop_shas: Mapping[int, str],
    pt_sha: str,
    retrieved_at: datetime,
) -> tuple[list[Any], list[Any]]:
    from reva.kg.model import KgEntity, KgStatement

    z_of = {e.symbol: e.z for e in elements}
    ents: list[Any] = []
    stmts: list[Any] = []

    def ref(url: str, sel: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        return (
            {
                "P854": url,
                "P813": retrieved_at.date().isoformat(),
                "publisher": "PubChem (NCBI/NLM)",
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
                source_revision="sha256:" + sha[:16],
                statement_id=sid,
                raw_sha256=sha,
            )
        )

    for c in compounds:
        sha = prop_shas[c.cid]
        url = f"https://{PUBCHEM_HOST}/compound/{c.cid}"
        sel = {"file": "properties.json", "path": f"PropertyTable.Properties[{c.index}]"}
        title = c.props.get("Title") or f"CID {c.cid}"
        ents.append(
            KgEntity(
                qid=c.key,
                kind="compound",
                labels={"en": str(title)},
                descriptions={"en": f"PubChem compound CID {c.cid}"},
                aliases={"en": [f"CID {c.cid}"]},
                source_revision="sha256:" + sha[:16],
            )
        )
        p = c.props
        if isinstance(p.get("MolecularFormula"), str):
            st(
                f"cid:{c.cid}:formula",
                c.key,
                "pubchem:molecular_formula",
                sha,
                ref(url, sel),
                value=string_value(p["MolecularFormula"]),
            )
        for col, pid, unit in (
            ("MolecularWeight", "pubchem:molecular_weight", "g/mol"),
            ("ExactMass", "pubchem:exact_mass", "Da"),
            ("Charge", "pubchem:formal_charge", None),
        ):
            v = to_decimal(str(p[col])) if p.get(col) is not None else None
            if v is not None:
                st(
                    f"cid:{c.cid}:{col}",
                    c.key,
                    pid,
                    sha,
                    ref(url, sel),
                    value=quantity(v, unit),
                    quals={"computed_by": "pubchem"},
                )
        if isinstance(p.get("IUPACName"), str):
            st(
                f"cid:{c.cid}:iupac",
                c.key,
                "pubchem:iupac_name",
                sha,
                ref(url, sel),
                value=string_value(p["IUPACName"]),
                quals={"computed_by": "pubchem (Lexichem TK)"},
            )
        if isinstance(p.get("InChIKey"), str):
            st(
                f"cid:{c.cid}:inchikey",
                c.key,
                "pubchem:inchikey",
                sha,
                ref(url, sel),
                value={"type": "external_id", "value": p["InChIKey"]},
            )
        counts, shares, _why = _composition(c, elements)
        for sym, cnt in sorted((counts or {}).items()):
            q: dict[str, Any] = {"atom_count": cnt, "formula": p.get("MolecularFormula"), "symbol": sym}
            if shares and sym in shares:
                q.update(mass_share=kg_decimal(shares[sym]), computed_by="reva", periodic_table_sha256=pt_sha)
            st(
                f"cid:{c.cid}:element:{z_of[sym]}",
                c.key,
                "pubchem:formula_element",
                sha,
                ref(url, sel),
                obj=f"element:{z_of[sym]}",
                quals=q,
            )
    known = {c.cid for c in compounds}
    for lk in links:
        if lk.cid not in known:
            continue
        st(
            f"cid:{lk.cid}:chebi:{lk.chebi}",
            f"pubchem:cid:{lk.cid}",
            "pubchem:chebi_deposit",
            lk.cid_sha,
            (
                {
                    "P854": cids_url([lk.chebi]),
                    "P813": retrieved_at.date().isoformat(),
                    "publisher": "PubChem (NCBI/NLM)",
                    "selector": {"xrefs_index": lk.xref_index, "cids_index": lk.cid_index, "sid": lk.sid},
                    "xrefs_raw_sha256": lk.xref_sha,
                },
            ),
            obj=f"chebi:{lk.chebi}",
            quals={"sid": lk.sid, "cids_for_sid": lk.n_cids},
        )
    return ents, stmts


__all__ = [
    "CONNECTOR_NAME",
    "Compound",
    "Link",
    "atlas_ingest",
    "atlas_rows",
    "atlas_source",
    "chebi_cid_links",
    "kg_build",
    "parse_properties",
    "seed_chebi_ids",
]
