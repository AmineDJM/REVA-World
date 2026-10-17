"""Human body and disease ontologies -> KG (V4, agent V4HEALTH): UBERON anatomy (human view) + Cell Ontology
terms it carries, the Human Disease Ontology (DOID) and the Mondo Disease Ontology (MONDO); ontology causal axioms
(pathogen -> disease, gene -> disease, ...) -> attributed T1 claims through the causal loader.

Sources (one atlas source each, KG only -- no atlas table; fetched through ``run.get``, raw bytes archived)
-----------------------------------------------------------------------------------------------------------
* ``atlas.uberon`` (source key ``uberon.obo``): ``uberon/subsets/human-view.obo`` of the latest UBERON GitHub release
  (https://github.com/obophenotype/uberon/releases/latest/download/human-view.obo, ~12.6 MB; the OBO PURL
  ``http://purl.obolibrary.org/obo/uberon/subsets/human-view.obo`` redirects there). UBERON's human view: the
  classes valid for *Homo sapiens* (taxon constraints applied by the UBERON release pipeline), incl. the Cell
  Ontology (CL) classes it imports. Licence **CC BY 3.0** (header ``dcterms-license``; checked every run).
* ``atlas.doid`` (``doid.obo``): ``doid.json`` (OBO Graphs JSON, ~24 MB) of the Human Disease Ontology
  (https://raw.githubusercontent.com/DiseaseOntology/HumanDiseaseOntology/main/src/ontology/doid.json, the file
  the ``doid.json`` PURL serves). The OBO flat release (``doid.obo``) carries no logical axioms (anatomical
  location, pathogen, symptom are only in the OWL/JSON forms), hence the JSON. It includes DOID's import modules
  (NCBITaxon, SYMP, HP, GENO, SO, TRANS, FOODON, DISDRIV, UBERON, CL, CHEBI subsets) with their labels.
  Licence **CC0 1.0** (graph meta ``dcterms:license``; checked every run).
* ``atlas.mondo`` (``mondo.obo``): ``mondo.obo`` of the latest Mondo GitHub release
  (https://github.com/monarch-initiative/mondo/releases/latest/download/mondo.obo, ~53 MB). Licence **CC BY 4.0**
  (header ``terms:license``; checked every run).
* every run also fetches the Relation Ontology ``ro.obo`` (~0.46 MB, CC0 1.0,
  https://raw.githubusercontent.com/oborel/obo-relations/master/ro.obo): the relation hierarchy and verbatim
  relation definitions the causal decision rule and the claim codebooks rest on.

Source revision = the file's own ``data-version`` (e.g. ``uberon/releases/2026-10-01/subsets/human-view.owl``,
``http://purl.obolibrary.org/obo/doid/releases/2026-09-30/doid.owl``, ``releases/2026-10-06``).

KG (predicates ``registry/kg/predicates_health.yaml``, families ``registry/kg/relations_v4.d/v4health.yaml``)
-------------------------------------------------------------------------------------------------------------
Entities ``uberon:<n>`` (anatomical_structure), ``cl:<n>`` (cell_type), ``doid:<n>`` / ``mondo:<n>`` (disease) and,
from DOID's import modules only (labels as DOID imported them), ``ncbitaxon:<n>`` (taxon: pathogens, vectors and
their lineage), ``symp:`` (symptom), ``hp:`` (phenotype; HPO labels, HPO licence -- see report), ``geno:``,
``so:``, ``trans:``, ``foodon:``, ``disdriv:``. CHEBI classes are referenced, never created (V4CHEM owns
compounds); HGNC genes are referenced (``hgnc:<n>``, V4BIO owns genes). Obsolete classes are skipped.
Statements per class: is_a, the non-causal relation axioms (``SubClassOf R some X`` and the differentia of
``EquivalentClasses``/``intersection_of``, qualifier ``axioms``), definition (verbatim string), cross-references
(ICD-10/ICD-11/OMIM/Orphanet/MeSH/UMLS ...; external ids exactly as written, qualifier ``db`` and, for Mondo, the
xref's own ``source`` annotations). Mondo ``xref: DOID:n {source="MONDO:equivalentTo"}`` becomes the item edge
``mondo:equivalent_doid`` (Mondo's curated equivalence; never inferred from names).

Causal axioms (docs/dev/v4_v4health.md)
---------------------------------------
A relation is causal when RO places it under ``RO:0002410`` "causally related to" (e.g. ``RO:0014001`` disease has
infectious agent, ``RO:0004003`` has material basis in germline mutation in, ``RO:0004019`` disease has basis in,
``RO:0001022`` has allergic trigger), when the ontology defines it as causation (DOID's ``IDO:0000664`` "has
material basis in": "Relating the disease to it's causation (disease and disorder)."), or when its label carries
a causal marker (``health_obo_common.CAUSAL_MARKERS``). Such an axiom is **never** a KG statement: it becomes one
T1 claim (expert-curated ontology axiom with a verbatim definition, the closest V3 tier -- a coded relation of a
curated research resource) via ``reva.causal.load_dataset_claims`` -- cause = the filler (pathogen, gene,
inheritance mode, ...), effect = the disease, except the two RO relations whose subject is the cause ("disease
causes disruption/dysfunction of"). Codebook = the relation definition quoted verbatim from the archived ro.obo /
doid.json (or, explicitly marked, the nearest ancestor's when RO gives none). A causal relation with no definition
anywhere is not loaded at all (counted in the run report). Claims are class-level ("a disease of this class has
its material basis in an infection by this taxon"), never about a person or an outbreak.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from reva.adapters.connectors.health_obo_common import (
    KIND_BY_PREFIX,
    RO_HOST,
    RO_URL,
    OboStanza,
    Relation,
    RelationIndex,
    claim_relation,
    curie,
    data_version,
    digest,
    drift,
    exact_synonyms,
    kg_available,
    kg_id,
    load_kg,
    parse_obo,
    prefix,
    relationship,
)
from reva.atlas.sources import AtlasSourceSpec

VERSION = "1.0.0"
PARSER_VERSION = "atlas_health_onto/1"
DAY = 86400
GH_HOSTS = ("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com")

UBERON_NAME, UBERON_KEY = "atlas.uberon", "uberon.obo"
UBERON_URL = "https://github.com/obophenotype/uberon/releases/latest/download/human-view.obo"
DOID_NAME, DOID_KEY = "atlas.doid", "doid.obo"
DOID_URL = "https://raw.githubusercontent.com/DiseaseOntology/HumanDiseaseOntology/main/src/ontology/doid.json"
MONDO_NAME, MONDO_KEY = "atlas.mondo", "mondo.obo"
MONDO_URL = "https://github.com/monarch-initiative/mondo/releases/latest/download/mondo.obo"

MAX_UBERON_BYTES = 40_000_000
MAX_DOID_BYTES = 60_000_000
MAX_MONDO_BYTES = 120_000_000
MAX_RO_BYTES = 5_000_000
#: truncation guards (live): a release far below these is refused, never loaded as a partial ontology
MIN_LIVE = {"uberon": 10_000, "doid": 10_000, "mondo": 20_000}

LICENCES = {
    "uberon": ("http://creativecommons.org/licenses/by/3.0/", "CC BY 3.0"),
    "doid": ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0 1.0"),
    "mondo": ("http://creativecommons.org/licenses/by/4.0/", "CC BY 4.0"),
    "ro": ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0 1.0"),
}
RIGHTS: dict[str, dict[str, str]] = {
    "uberon": {
        "licence": "CC BY 3.0",
        "licence_url": "https://creativecommons.org/licenses/by/3.0/",
        "attribution": "Uberon multi-species anatomy ontology (human view), https://obophenotype.github.io/uberon/ "
        "(Mungall et al., Genome Biology 2012;13:R5)",
    },
    "doid": {
        "licence": "CC0 1.0",
        "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "attribution": "Human Disease Ontology, https://disease-ontology.org (Schriml et al.)",
    },
    "mondo": {
        "licence": "CC BY 4.0",
        "licence_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Mondo Disease Ontology, https://mondo.monarchinitiative.org (Monarch Initiative)",
    },
}

# ---------------------------------------------------------------------------------------------- relation maps
#: UBERON relationship / intersection_of relation -> predicate (others: counted, not loaded)
UBERON_RELS = {
    "part_of": "uberon:part_of",
    "has_part": "uberon:has_part",
    "located_in": "uberon:located_in",
    "develops_from": "uberon:develops_from",
}
UBERON_XREF_DBS = ("FMA", "MESH", "NCIT", "UMLS", "SCTID")
UBERON_ENTITY_PREFIXES = ("UBERON", "CL")
#: DOID edge predicate (RO/BFO CURIE) -> predicate, non-causal only (a causal one here is a code error)
DOID_RELS = {
    "RO:0004026": "doid:has_location",
    "RO:0002452": "doid:has_symptom",
    "RO:0002200": "doid:has_phenotype",
    "RO:0004029": "doid:has_feature",
    "RO:0002451": "doid:transmitted_by",
    "RO:0001000": "doid:derives_from",
    "RO:0002488": "doid:onset_during",
    "RO:0002220": "doid:adjacent_to",
    "so#has_origin": "doid:has_origin",
}
#: prefixes of DOID import-module classes the DOID loader creates as entities (others are owned elsewhere)
DOID_ENTITY_PREFIXES = ("DOID", "NCBITaxon", "SYMP", "HP", "GENO", "SO", "TRANS", "FOODON", "DISDRIV")
#: Mondo relation (shorthand id or RO CURIE) -> predicate, non-causal only
MONDO_RELS = {
    "disease_has_location": "mondo:has_location",
    "disease_has_inflammation_site": "mondo:inflammation_site",
    "disease_has_feature": "mondo:has_feature",
    "disease_has_major_feature": "mondo:has_feature",
    "has_characteristic": "mondo:has_characteristic",
    "BFO:0000050": "mondo:part_of",
    "part_of": "mondo:part_of",
    "disease_shares_features_of": "mondo:shares_features_of",
    "disease_responds_to": "mondo:responds_to",
    "in_taxon": "mondo:in_taxon",
    "transmitted_by": "mondo:transmitted_by",
}
#: Mondo bookkeeping relations: not facts about the disease (never loaded, counted)
MONDO_META_RELS = ("excluded_subClassOf", "should_conform_to", "curated_content_resource", "seeAlso")
MONDO_XREF_DBS = ("ICD10WHO", "ICD10CM", "icd11.foundation", "ICD11", "OMIM", "OMIMPS", "Orphanet", "MESH", "UMLS",
                  "NCIT", "DOID")  # fmt: skip


@dataclass
class Parts:
    entities: list[Any]
    statements: list[Any]
    claims: list[Any] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    codebook: dict[str, Any] = field(default_factory=dict)


class _Builder:
    """Accumulates entities / statements / claims of one ontology (dedup by statement id, axioms merged)."""

    def __init__(self, *, source_label: str, file: str, raw_sha256: str, revision: str) -> None:
        self.source_label, self.file, self.raw, self.revision = source_label, file, raw_sha256, revision
        self.ents: dict[str, Any] = {}
        self.sts: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, Any] = {}
        self.rep: Counter[str] = Counter()
        self.skipped: Counter[str] = Counter()

    def entity(self, c: str, label: str | None, *, kind: str | None = None, aliases: Iterable[str] = (),
               note: str = "") -> None:  # fmt: skip
        from reva.kg.model import KgEntity

        if not label:
            self.skipped["entity_without_label"] += 1
            return
        kind = kind or KIND_BY_PREFIX[prefix(c)]
        al = sorted({a for a in aliases if a and a != label and len(a) <= 500})
        desc_en = f"{kind.replace('_', ' ')} ({c}, {self.source_label}{note})"
        desc_fr = f"{_KIND_FR.get(kind, kind)} ({c}, {self.source_label}{note})"
        self.ents[kg_id(c)] = KgEntity(
            qid=kg_id(c),
            kind=kind,
            labels={"en": label[:500]},
            descriptions={"en": desc_en, "fr": desc_fr},
            aliases={"en": al} if al else {},
            source_revision=self.revision,
        )

    def statement(self, subj: str, pid: str, sid: str, selector: Mapping[str, Any], *, obj: str | None = None,
                  value: Mapping[str, Any] | None = None, quals: Mapping[str, Any] | None = None,
                  axiom: str | None = None) -> None:  # fmt: skip
        if len(sid) > 240:
            import hashlib

            sid = sid[:180] + ":" + hashlib.sha256(sid.encode()).hexdigest()[:32]
        cur = self.sts.get(sid)
        if cur is None:
            q = dict(quals or {})
            if axiom:
                q["axioms"] = [axiom]
            self.sts[sid] = {
                "subject": kg_id(subj),
                "pid": pid,
                "obj": kg_id(obj) if obj else None,
                "value": dict(value) if value is not None else None,
                "quals": q,
                "refs": [{"stated_in": f"{self.source_label} ({self.file})", "selector": dict(selector)}],
            }
            self.rep[pid] += 1
        elif axiom and axiom not in cur["quals"].setdefault("axioms", []):
            cur["quals"]["axioms"].append(axiom)

    def kg_statements(self) -> list[Any]:
        from reva.kg.model import KgStatement

        return [
            KgStatement(
                subject_qid=s["subject"],
                property_pid=s["pid"],
                object_qid=s["obj"],
                value=s["value"],
                qualifiers=s["quals"],
                references=s["refs"],
                statement_id=sid,
                source_revision=self.revision,
                raw_sha256=self.raw,
            )
            for sid, s in sorted(self.sts.items())
        ]


_KIND_FR = {
    "anatomical_structure": "structure anatomique",
    "cell_type": "type cellulaire",
    "disease": "maladie",
    "taxon": "taxon",
    "symptom": "symptôme",
    "phenotype": "phénotype",
    "genetic_inheritance_pattern": "mode de transmission génétique",
    "sequence_feature": "élément de séquence",
    "transmission_process": "processus de transmission",
    "food_material": "aliment",
    "disease_driver": "facteur déterminant de maladie",
}


def _xref_db(x: str) -> str:
    return x.split(":", 1)[0] if ":" in x else ""


# ============================================================================================== causal claims
class _Claims:
    """Causal ontology axioms -> ``causdata_common.ClaimRecord`` (one per subject/relation/filler)."""

    def __init__(self, ri: RelationIndex, *, raw_sha256: str, file: str) -> None:
        self.ri, self.raw, self.file = ri, raw_sha256, file
        self.records: dict[str, Any] = {}
        self.definitions: dict[str, str] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.undefined: Counter[str] = Counter()
        self.by_relation: Counter[str] = Counter()

    def add(self, subj: str, rel: str, filler: str, *, axiom: str, selector: Mapping[str, Any]) -> None:
        from reva.adapters.connectors.causdata_common import ClaimRecord

        ro = self.ri.resolve(rel)
        term = ro if ro.startswith(("RO:", "IDO:", "BFO:")) else rel
        if term not in self.definitions:
            d = self.ri.definition(rel)
            if d is None:
                self.undefined[f"{rel} ({self.ri.name(rel)})"] += 1
                return
            self.definitions[term] = d[0]
            self.sources[term] = d[1]
        relation, side = claim_relation(rel, ro)
        cause, effect = (subj, filler) if side == "subject" else (filler, subj)
        cid = f"{subj}|{term}|{filler}"
        prev = self.records.get(cid)
        if prev is not None:
            axioms = list(prev.selector.get("axioms", []))
            if axiom not in axioms:
                axioms.append(axiom)
                self.records[cid] = replace(prev, selector={**prev.selector, "axioms": axioms})
            return
        self.records[cid] = ClaimRecord(
            cause_id=kg_id(cause),
            effect_id=kg_id(effect),
            relation=relation,
            selector={
                "file": self.file,
                "subject": subj,
                "relation": rel,
                "relation_label": self.ri.name(rel),
                "filler": filler,
                "axioms": [axiom],
                **dict(selector),
            },
            raw_sha256=self.raw,
            codebook_term=term,
            source_claim_id=cid,
        )
        self.by_relation[f"{term} {self.ri.name(rel)}"] += 1


def codebook(name: str, revision: str, ro_revision: str, claims: _Claims, url: str) -> dict[str, Any]:
    return {
        "name": f"{name} relation axioms read with the Relation Ontology (RO)",
        "version": f"{revision}; RO {ro_revision}"[:100],
        "url": url,
        "definitions": dict(sorted(claims.definitions.items())),
        "sources": {k: dict(v) for k, v in sorted(claims.sources.items())},
    }


# ============================================================================================== RO
def ro_index(body: bytes, *, raw_sha256: str) -> tuple[RelationIndex, str]:
    header, stanzas = parse_obo(body, what="ro.obo")
    _check_obo_licence(header, "ro")
    ri = RelationIndex()
    ri.add_obo(stanzas, document="ro.obo")
    ri.documents["ro.obo"] = {"title": "Relation Ontology (ro.obo)", "url": RO_URL, "sha256": raw_sha256}
    if "RO:0002410" not in ri.relations:
        raise drift("ro.obo: 'causally related to' (RO:0002410) missing")
    return ri, data_version(header, "ro.obo")


def _check_obo_licence(header: Mapping[str, list[str]], key: str) -> None:
    url = LICENCES[key][0]
    vals = [v for v in header.get("property_value", []) if "license" in v.lower()]
    if not any(url.rstrip("/").split("://", 1)[1] in v for v in vals):
        raise drift(f"{key}: licence {vals[:3]} is not the expected {LICENCES[key][1]} ({url})", kind="licence")


# ============================================================================================== UBERON
def uberon_kg(body: bytes, *, raw_sha256: str) -> Parts:
    header, stanzas = parse_obo(body, what="human-view.obo")
    _check_obo_licence(header, "uberon")
    rev = data_version(header, "human-view.obo")
    b = _Builder(source_label="UBERON human view", file="human-view.obo", raw_sha256=raw_sha256, revision=rev)
    terms = {s.id: s for s in stanzas if s.kind == "Term" and prefix(s.id) in UBERON_ENTITY_PREFIXES}
    live = {i for i, s in terms.items() if not s.obsolete}
    for i in sorted(live):
        s = terms[i]
        b.entity(i, s.first("name"), aliases=exact_synonyms(s))
        _common_term_statements(b, s, "uberon", UBERON_XREF_DBS)
        for v, _q in s.tagged("is_a"):
            if v in live:
                b.statement(i, "uberon:subclass_of", f"is_a:{i}:{v}", {"term": i, "tag": "is_a", "value": v}, obj=v)
            else:
                b.skipped["is_a_target_outside_uberon_cl"] += 1
        for tag in ("relationship", "intersection_of"):
            for v, _q in s.tagged(tag):
                parts = v.split()
                if tag == "intersection_of" and len(parts) == 1:
                    if parts[0] in live:
                        b.statement(i, "uberon:subclass_of", f"is_a:{i}:{parts[0]}",
                                    {"term": i, "tag": tag, "value": v}, obj=parts[0], axiom="intersection_of_genus")  # fmt: skip
                    continue
                rel, tgt = relationship(v)
                pid = UBERON_RELS.get(rel)
                if pid is None:
                    b.skipped[f"relation_not_loaded:{rel}"] += 1
                    continue
                if tgt not in live:
                    b.skipped[f"target_outside_uberon_cl:{rel}"] += 1
                    continue
                b.statement(i, pid, f"{rel}:{i}:{tgt}", {"term": i, "tag": tag, "value": v}, obj=tgt,
                            axiom="intersection_of" if tag == "intersection_of" else "subclass_of_some")  # fmt: skip
    out = Parts(list(b.ents.values()), b.kg_statements())
    out.report = {
        "revision": rev,
        "terms_in_file": len(terms),
        "obsolete_skipped": len(terms) - len(live),
        "entities": len(out.entities),
        "entities_by_kind": dict(Counter(e.kind for e in out.entities)),
        "statements": len(out.statements),
        "by_property": dict(sorted(b.rep.items())),
        "skipped": dict(sorted(b.skipped.items())),
    }
    return out


def _common_term_statements(b: _Builder, s: OboStanza, ns: str, xref_dbs: Iterable[str]) -> None:
    i = s.id
    d = s.first("def")
    if d:
        b.statement(i, f"{ns}:definition", f"definition:{i}", {"term": i, "tag": "def"},
                    value={"type": "string", "value": d[:20000]})  # fmt: skip
    dbs = set(xref_dbs)
    for v, rest in s.tagged("xref"):
        db = _xref_db(v)
        if db not in dbs:
            b.skipped[f"xref_db_not_kept:{db}"] += 1
            continue
        q: dict[str, Any] = {"db": db}
        if rest:
            q["annotations"] = rest[:1000]
        b.statement(i, f"{ns}:xref", f"xref:{i}:{v}", {"term": i, "tag": "xref", "value": v},
                    value={"type": "external_id", "value": v}, quals=q)  # fmt: skip


# ============================================================================================== DOID (OBO Graphs JSON)
def _node_meta_def(n: Mapping[str, Any]) -> str | None:
    d = ((n.get("meta") or {}).get("definition") or {}).get("val")
    return str(d) if d else None


def doid_kg(body: bytes, *, raw_sha256: str, ri: RelationIndex) -> tuple[Parts, str]:
    try:
        doc = json.loads(body)
        graphs = doc["graphs"]
    except (ValueError, KeyError, TypeError) as e:
        raise drift("doid.json is not OBO Graphs JSON") from e
    main = [g for g in graphs if str(g.get("id", "")).endswith("/doid.owl")]
    if len(main) != 1:
        raise drift(f"doid.json: expected one doid.owl graph, found {len(main)}")
    g = main[0]
    meta = g.get("meta") or {}
    lic = [str(p.get("val")) for p in meta.get("basicPropertyValues") or [] if "license" in str(p.get("pred"))]
    if LICENCES["doid"][0] not in lic:
        raise drift(f"doid.json: licence {lic} is not CC0 1.0", kind="licence")
    rev = str(meta.get("version") or "")
    if not rev:
        raise drift("doid.json: no version IRI")
    # relation declarations of the file (IDO:0000664 "has material basis in" is defined here, not in RO)
    for gr in graphs:
        for n in gr.get("nodes") or []:
            if n.get("type") != "PROPERTY":
                continue
            c = curie(str(n.get("id")))
            if c is None or c in ri.relations:
                continue
            ri.add(Relation(c, n.get("lbl"), _node_meta_def(n), (), (), "doid.json"))
    ri.documents["doid.json"] = {"title": "Human Disease Ontology (doid.json)", "url": DOID_URL, "sha256": raw_sha256}

    b = _Builder(source_label="Human Disease Ontology", file="doid.json", raw_sha256=raw_sha256, revision=rev)
    claims = _Claims(ri, raw_sha256=raw_sha256, file="doid.json")
    nodes: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for gr in graphs:
        gid = str(gr.get("id", "")).rsplit("/", 1)[-1]
        for n in gr.get("nodes") or []:
            if n.get("type") != "CLASS":
                continue
            c = curie(str(n.get("id")))
            if c is None or (n.get("meta") or {}).get("deprecated"):
                continue
            if prefix(c) == "DOID" and gr is not g:
                continue  # DOID classes re-declared by an import module (omim_susc): the main graph is authoritative
            nodes.setdefault(c, (gid, n))
    created = {c for c, (gid, _n) in nodes.items() if prefix(c) in DOID_ENTITY_PREFIXES}
    for c in sorted(created):
        gid, n = nodes[c]
        syn = [s["val"] for s in (n.get("meta") or {}).get("synonyms") or [] if s.get("pred") == "hasExactSynonym"]
        note = "" if prefix(c) == "DOID" else f"; label as imported by DOID ({gid})"
        b.entity(c, n.get("lbl"), aliases=syn, note=note)
        if prefix(c) != "DOID":
            continue
        m = n.get("meta") or {}
        d = _node_meta_def(n)
        if d:
            dx = [str(x) for x in ((m.get("definition") or {}).get("xrefs") or [])]
            b.statement(c, "doid:definition", f"definition:{c}", {"node": c, "field": "meta.definition"},
                        value={"type": "string", "value": d[:20000]}, quals={"def_xrefs": dx[:20]} if dx else None)  # fmt: skip
        for x in m.get("xrefs") or []:
            v = str(x.get("val") or "")
            if not v:
                continue
            b.statement(c, "doid:xref", f"xref:{c}:{v}", {"node": c, "field": "meta.xrefs", "value": v},
                        value={"type": "external_id", "value": v}, quals={"db": _xref_db(v)})  # fmt: skip
        for p in m.get("basicPropertyValues") or []:
            pred = str(p.get("pred") or "")
            if pred.endswith(("skos/core#exactMatch", "skos/core#closeMatch", "skos/core#narrowMatch",
                              "skos/core#broadMatch")):  # fmt: skip
                v = str(p.get("val") or "")
                kind = pred.rsplit("#", 1)[-1]
                b.statement(c, "doid:xref", f"xref:{c}:{v}", {"node": c, "field": "basicPropertyValues", "pred": pred},
                            value={"type": "external_id", "value": v}, quals={"db": _xref_db(v), "mapping": f"skos:{kind}"})  # fmt: skip

    def axiom(subj: str, pred_iri: str, obj_iri: str, kind: str, graph: str) -> None:
        o = curie(obj_iri)
        if o is None:
            b.skipped[f"filler_not_an_identifier:{kind}"] += 1
            return
        sel = {"graph": graph, "axiom": kind, "sub": subj, "pred": pred_iri, "obj": o}
        if pred_iri == "is_a":
            if subj in created and o in nodes:
                b.statement(subj, "doid:subclass_of", f"is_a:{subj}:{o}", sel, obj=o,
                            axiom=kind, quals={"graph": graph} if graph != "doid.owl" else None)  # fmt: skip
            else:
                b.skipped["is_a_outside_created_classes"] += 1
            return
        rel = curie(pred_iri) or pred_iri.rsplit("/", 1)[-1]
        if rel.startswith("so#"):
            rel = "so#" + rel.split("#", 1)[1]
        if subj not in created or prefix(subj) != "DOID":
            b.skipped["relation_on_imported_class"] += 1
            return
        if ri.is_causal(rel):
            if rel in DOID_RELS:
                raise drift(f"DOID relation {rel} is mapped as a plain link but is causal by the rule")
            claims.add(subj, rel, o, axiom=kind, selector={"graph": graph})
            return
        pid = DOID_RELS.get(rel)
        if pid is None:
            b.skipped[f"relation_not_loaded:{rel}"] += 1
            return
        b.statement(subj, pid, f"{rel}:{subj}:{o}", sel, obj=o, axiom=kind)

    for gr in graphs:
        gid = str(gr.get("id", "")).rsplit("/", 1)[-1]
        for e in gr.get("edges") or []:
            s = curie(str(e.get("sub")))
            if s is None:
                continue
            axiom(s, str(e.get("pred")), str(e.get("obj")), "subclass_of" if e.get("pred") == "is_a" else
                  "subclass_of_some", gid)  # fmt: skip
    for ld in g.get("logicalDefinitionAxioms") or []:
        s = curie(str(ld.get("definedClassId")))
        if s is None:
            continue
        for gen in ld.get("genusIds") or []:
            axiom(s, "is_a", str(gen), "equivalent_class_genus", "doid.owl")
        for r in ld.get("restrictions") or []:
            axiom(s, str(r.get("propertyId")), str(r.get("fillerId")), "equivalent_class", "doid.owl")
    out = Parts(list(b.ents.values()), b.kg_statements(), list(claims.records.values()))
    out.report = {
        "revision": rev,
        "doid_classes": sum(1 for c in created if prefix(c) == "DOID"),
        "entities": len(out.entities),
        "entities_by_kind": dict(Counter(e.kind for e in out.entities)),
        "statements": len(out.statements),
        "by_property": dict(sorted(b.rep.items())),
        "causal_claims": len(out.claims),
        "causal_by_relation": dict(sorted(claims.by_relation.items())),
        "causal_cause_prefixes": dict(Counter(c.cause_id.split(":")[0] for c in out.claims)),
        "causal_without_definition_not_loaded": dict(claims.undefined),
        "skipped": dict(sorted(b.skipped.items())),
    }
    out.codebook = {"claims": claims}
    return out, rev


# ============================================================================================== MONDO
def mondo_kg(body: bytes, *, raw_sha256: str, ri: RelationIndex) -> tuple[Parts, str]:
    header, stanzas = parse_obo(body, what="mondo.obo")
    _check_obo_licence(header, "mondo")
    rev = data_version(header, "mondo.obo")
    ri.add_obo(stanzas, document="mondo.obo")
    ri.documents["mondo.obo"] = {"title": "Mondo Disease Ontology (mondo.obo)", "url": MONDO_URL, "sha256": raw_sha256}
    b = _Builder(source_label="Mondo Disease Ontology", file="mondo.obo", raw_sha256=raw_sha256, revision=rev)
    claims = _Claims(ri, raw_sha256=raw_sha256, file="mondo.obo")
    terms = {s.id: s for s in stanzas if s.kind == "Term" and prefix(s.id) == "MONDO"}
    live = {i for i, s in terms.items() if not s.obsolete}
    dbs = set(MONDO_XREF_DBS)
    for i in sorted(live):
        s = terms[i]
        b.entity(i, s.first("name"), aliases=exact_synonyms(s))
        d = s.first("def")
        if d:
            b.statement(i, "mondo:definition", f"definition:{i}", {"term": i, "tag": "def"},
                        value={"type": "string", "value": d[:20000]})  # fmt: skip
        for v, rest in s.tagged("xref"):
            db = _xref_db(v)
            if db not in dbs:
                b.skipped[f"xref_db_not_kept:{db}"] += 1
                continue
            sources = _annotation_values(rest, "source")
            sel = {"term": i, "tag": "xref", "value": v}
            if db == "DOID" and "MONDO:equivalentTo" in sources:
                b.statement(i, "mondo:equivalent_doid", f"equivalent_doid:{i}:{v}", sel, obj=v,
                            quals={"xref_sources": sources[:10]})  # fmt: skip
                continue
            q: dict[str, Any] = {"db": db}
            if sources:
                q["xref_sources"] = sources[:10]
            b.statement(i, "mondo:xref", f"xref:{i}:{v}", sel, value={"type": "external_id", "value": v}, quals=q)
        for v, _q in s.tagged("is_a"):
            if v in live:
                b.statement(i, "mondo:subclass_of", f"is_a:{i}:{v}", {"term": i, "tag": "is_a", "value": v}, obj=v)
            else:
                b.skipped["is_a_target_not_live_mondo"] += 1
        for tag in ("relationship", "intersection_of"):
            for v, qual in s.tagged(tag):
                parts = v.split()
                if tag == "intersection_of" and len(parts) == 1:
                    if parts[0] in live:
                        b.statement(i, "mondo:subclass_of", f"is_a:{i}:{parts[0]}",
                                    {"term": i, "tag": tag, "value": v}, obj=parts[0], axiom="intersection_of_genus")  # fmt: skip
                    continue
                rel, tgt_raw = relationship(v)
                if rel in MONDO_META_RELS:
                    b.skipped[f"bookkeeping_relation:{rel}"] += 1
                    continue
                tgt = curie(tgt_raw)
                if tgt is None:
                    b.skipped[f"target_not_an_identifier:{rel}"] += 1
                    continue
                kind = "intersection_of" if tag == "intersection_of" else "subclass_of_some"
                sel = {"term": i, "tag": tag, "value": v}
                if ri.is_causal(rel):
                    if rel in MONDO_RELS or ri.resolve(rel) in MONDO_RELS:
                        raise drift(f"Mondo relation {rel} is mapped as a plain link but is causal by the rule")
                    claims.add(i, rel, tgt, axiom=kind, selector={"tag": tag, **({"annotations": qual[:500]} if qual else {})})
                    continue
                pid = MONDO_RELS.get(rel) or MONDO_RELS.get(ri.resolve(rel))
                if pid is None:
                    b.skipped[f"relation_not_loaded:{rel}"] += 1
                    continue
                if pid == "mondo:part_of" and prefix(tgt) != "MONDO":
                    b.skipped["part_of_target_not_mondo"] += 1
                    continue
                b.statement(i, pid, f"{rel}:{i}:{tgt}", sel, obj=tgt, axiom=kind,
                            quals={"relation": rel} if pid == "mondo:has_feature" else None)  # fmt: skip
    out = Parts(list(b.ents.values()), b.kg_statements(), list(claims.records.values()))
    out.report = {
        "revision": rev,
        "mondo_terms_in_file": len(terms),
        "obsolete_skipped": len(terms) - len(live),
        "entities": len(out.entities),
        "statements": len(out.statements),
        "by_property": dict(sorted(b.rep.items())),
        "causal_claims": len(out.claims),
        "causal_by_relation": dict(sorted(claims.by_relation.items())),
        "causal_cause_prefixes": dict(Counter(c.cause_id.split(":")[0] for c in out.claims)),
        "causal_without_definition_not_loaded": dict(claims.undefined),
        "skipped": dict(sorted(b.skipped.most_common(60))),
    }
    out.codebook = {"claims": claims}
    return out, rev


def _annotation_values(rest: str, key: str) -> list[str]:
    import re

    return re.findall(rf'{key}="([^"]*)"', rest or "")


# ============================================================================================== atlas protocol
def code_digest() -> str:
    import reva.adapters.connectors.health_obo_common as common

    return digest(code_digest, common)


def _spec(name: str, key: str, atlas_source: str, url: str, hosts: tuple[str, ...], max_bytes: int,
          provider: str, licence: str, dataset: str, notes: str) -> AtlasSourceSpec:  # fmt: skip
    return AtlasSourceSpec(
        name=name,
        version=VERSION,
        source_key=key,
        budget_key=key,
        atlas_source=atlas_source,
        provider=provider,
        licence=licence,
        allowed_hosts=tuple(dict.fromkeys((*hosts, RO_HOST))),
        datasets=(dataset,),
        dataset_ids=(url, RO_URL),
        cadence_seconds=30 * DAY,
        planned_requests=2,
        max_response_bytes=max_bytes,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"file": url, "relations": RO_URL, "predicates": "registry/kg/predicates_health.yaml"},
        notes=notes,
    )


def atlas_source() -> list[AtlasSourceSpec]:
    return [
        _spec(UBERON_NAME, UBERON_KEY, "uberon", UBERON_URL, GH_HOSTS, MAX_UBERON_BYTES,
              "Uberon anatomy ontology (OBO Foundry; GitHub release, human view)",
              f"{RIGHTS['uberon']['licence']} ({RIGHTS['uberon']['licence_url']}); {RIGHTS['uberon']['attribution']}",
              "anatomy", "KG only: UBERON + CL classes of the human view with is_a/part_of/has_part/located_in/"
              "develops_from, definitions and FMA/MeSH/NCIt/UMLS/SNOMED xrefs."),
        _spec(DOID_NAME, DOID_KEY, "doid", DOID_URL, (RO_HOST,), MAX_DOID_BYTES,
              "Human Disease Ontology (doid.json, GitHub main)",
              f"{RIGHTS['doid']['licence']} ({RIGHTS['doid']['licence_url']}); {RIGHTS['doid']['attribution']}",
              "diseases", "KG: DOID diseases, is_a, location (UBERON), symptoms (SYMP), phenotypes, onset, "
              "transmission, ICD/OMIM/ORDO/MeSH xrefs; causal axioms (has material basis in: pathogens, inheritance, "
              "...) -> T1 claims via the causal loader."),
        _spec(MONDO_NAME, MONDO_KEY, "mondo", MONDO_URL, GH_HOSTS, MAX_MONDO_BYTES,
              "Mondo Disease Ontology (GitHub release)",
              f"{RIGHTS['mondo']['licence']} ({RIGHTS['mondo']['licence_url']}); {RIGHTS['mondo']['attribution']}",
              "diseases", "KG: Mondo diseases, is_a, location, features, DOID equivalences, ICD-10/ICD-11/OMIM/"
              "Orphanet xrefs; causal axioms (infectious agent, germline mutation in HGNC gene, ...) -> T1 claims."),
    ]  # fmt: skip


async def _fetch(run: Any, url: str, key: str, max_bytes: int, media: tuple[str, ...]) -> tuple[str, bytes, datetime]:
    raw = await run.get(url, key=key, media_types=media, max_bytes=max_bytes)
    sha = run.archive(raw)
    body = run.store.get("raw", sha)
    return sha, body, raw.retrieved_at


async def atlas_ingest(run: Any) -> dict[str, Any]:
    name = run.spec.name
    dataset = run.spec.datasets[0]
    if not run.wants(dataset):
        return {"skipped": True}
    ro_sha, ro_body, ro_at = await _fetch(run, RO_URL, "ro.obo", MAX_RO_BYTES, ("text/plain",))
    ri, ro_rev = ro_index(ro_body, raw_sha256=ro_sha)
    if name == UBERON_NAME:
        sha, body, at = await _fetch(run, UBERON_URL, "uberon:human-view.obo", MAX_UBERON_BYTES,
                                     ("application/octet-stream", "text/plain"))  # fmt: skip
        parts, rev, key, short = uberon_kg(body, raw_sha256=sha), "", UBERON_KEY, "uberon"
        rev = parts.report["revision"]
    elif name == DOID_NAME:
        sha, body, at = await _fetch(run, DOID_URL, "doid:doid.json", MAX_DOID_BYTES,
                                     ("application/json", "text/plain"))  # fmt: skip
        parts, rev = doid_kg(body, raw_sha256=sha, ri=ri)
        key, short = DOID_KEY, "doid"
    elif name == MONDO_NAME:
        sha, body, at = await _fetch(run, MONDO_URL, "mondo:mondo.obo", MAX_MONDO_BYTES,
                                     ("application/octet-stream", "text/plain"))  # fmt: skip
        parts, rev = mondo_kg(body, raw_sha256=sha, ri=ri)
        key, short = MONDO_KEY, "mondo"
    else:  # pragma: no cover - registry guarantees the names
        raise drift(f"unknown source {name}")
    del body
    n_main = parts.report.get("terms_in_file") or parts.report.get("doid_classes") or parts.report.get(
        "mondo_terms_in_file", 0)  # fmt: skip
    if run.origin == "live" and int(n_main) < MIN_LIVE[short]:
        raise drift(f"{short}: only {n_main} classes: truncated release?", kind="truncated")
    retrieved = max(at, ro_at)
    out: dict[str, Any] = {"source": key, "revision": rev, "ro_revision": ro_rev, "raw": {"file": sha, "ro": ro_sha},
                           "build": parts.report}  # fmt: skip
    if not kg_available(run.engine):
        out["kg"] = {"status": "kg_unavailable"}
        return out
    out["kg"] = load_kg(
        run.engine,
        run.store,
        entities=parts.entities,
        statements=parts.statements,
        source_key=key,
        raw_sha256s=[sha],
        retrieved_at=retrieved,
        origin=run.origin,
        parameters={"connector": name, "parser": PARSER_VERSION, "revision": rev, "ro_revision": ro_rev},
    )
    if parts.claims:
        out["causal"] = deliver(run, parts, key=key, short=short, rev=rev, ro_rev=ro_rev, raws=[sha, ro_sha],
                                retrieved=retrieved)  # fmt: skip
    return out


def deliver(run: Any, parts: Parts, *, key: str, short: str, rev: str, ro_rev: str, raws: list[str],
            retrieved: datetime) -> dict[str, Any]:  # fmt: skip
    from reva.adapters.connectors.causdata_common import ClaimDelivery, deliver_claims

    claims: _Claims = parts.codebook["claims"]
    url = DOID_URL if short == "doid" else MONDO_URL
    d = ClaimDelivery(
        source_key=key,
        dataset=short,
        dataset_revision=rev[-100:],
        codebook=codebook({"doid": "Human Disease Ontology", "mondo": "Mondo Disease Ontology"}[short], rev,
                          ro_rev, claims, url),  # fmt: skip
        raw_sha256s=sorted(set(raws)),
        retrieved_at=retrieved,
        origin=run.origin,
        claims=sorted(parts.claims, key=lambda c: c.source_claim_id),
    )
    return deliver_claims(run, [d])


def chain_up(parts: Parts, start: str, pid: str, max_hops: int = 12) -> list[str]:
    """Follow one item predicate upward from ``start`` (first object each hop; report/doc helper)."""
    edges: dict[str, list[str]] = defaultdict(list)
    for s in parts.statements:
        if s.property_pid == pid and s.object_qid:
            edges[s.subject_qid].append(s.object_qid)
    out, cur = [start], start
    for _ in range(max_hops):
        nxt = sorted(edges.get(cur, []))
        if not nxt:
            break
        cur = nxt[0]
        out.append(cur)
    return out


__all__ = [
    "DOID_NAME",
    "MONDO_NAME",
    "UBERON_NAME",
    "Parts",
    "atlas_ingest",
    "atlas_source",
    "doid_kg",
    "mondo_kg",
    "ro_index",
    "uberon_kg",
]
