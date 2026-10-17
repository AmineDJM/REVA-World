"""Genes, proteins and genomes -> KG (V4, agent V4BIO).

Four atlas sources (KG only, no atlas table: nothing here is per country):

``atlas.hgnc`` (source key ``hgnc.complete_set``, CC0)
    The HGNC complete set (``hgnc_complete_set.txt``, ~17 MB TSV, one row per approved human gene symbol) from
    HGNC's public download bucket; its md5 (the Cloud Storage ETag) is checked. Entities ``hgnc:<n>`` (kind ``gene``,
    label = approved symbol, aliases = HGNC alias and previous symbols); statements: symbol, name, locus type and
    group, cytogenetic location (verbatim), ``hgnc:located_on_chromosome`` -> ``chromosome:9606:<name>`` (the
    chromosome part of HGNC's location: ``19q13.43`` -> 19, ``Xp22.33 and Yp11.2`` -> X and Y, ``mitochondria`` ->
    MT by the declared vocabulary map below; ``reserved``/``unplaced``/``not on reference assembly``... -> no link,
    counted), ``hgnc:encodes`` -> ``uniprot:<acc>`` (HGNC's ``uniprot_ids`` column), ``hgnc:in_taxon`` ->
    ``ncbitaxon:9606``, Ensembl and NCBI Gene ids.

``atlas.uniprot`` (source key ``uniprot.rest``, CC BY 4.0)
    UniProtKB REST (``rest.uniprot.org``, streamed TSV with field selection; release from ``X-UniProt-Release``):
    (1) the organism id of every reviewed (Swiss-Prot) entry -> exact counts per NCBI taxon id
    (``uniprot:reviewed_protein_count`` on ``ncbitaxon:<id>``); (2) the UniProt taxonomy records of those organisms
    (scientific name, rank, common name; 100 ids per request, the API maximum) -> ``ncbitaxon:<id>`` entities; (3) the reviewed human entries: accession,
    entry name, primary gene, protein names, length, function comment (verbatim, cut at 1,200 characters and flagged),
    HGNC cross-reference -> ``uniprot:<acc>`` (kind ``protein``) with ``uniprot:encoded_by`` -> ``hgnc:<n>``,
    ``uniprot:in_taxon``, ``uniprot:sequence_length``, ``uniprot:function``, ``uniprot:gene_name``.

``atlas.ensembl_genomes`` (source key ``ensembl.rest``; Ensembl data are free of restriction)
    Ensembl REST ``/info/species`` (vertebrates division): the assembly Ensembl uses per species ->
    ``assembly:<GCA_...>`` (kind ``genome_assembly``) with ``ensembl:assembly_of`` -> ``ncbitaxon:<id>`` and the
    assembly name; ``/info/assembly/<species>`` for the karyotype species of :data:`KARYOTYPE_SPECIES`: golden path
    length and ``ensembl:has_chromosome`` -> ``chromosome:<taxid>:<name>`` (length in bp as qualifier). Ensembl's
    REST API publishes no gene counts and BioMart is being retired (redirects to an archive), so the gene counts
    come from NCBI below.

``atlas.ncbi_genomes`` (source key ``ncbi.datasets``; NCBI data, U.S. Government, no use restriction)
    NCBI Datasets v2 genome dataset reports of reference genomes with annotation for the taxa of
    :data:`NCBI_TAXA` (vertebrates, green plants): ``assembly:<GCF_...>`` with ``ncbi:assembly_of``,
    ``ncbi:paired_assembly`` -> ``assembly:<GCA_...>`` (NCBI's own pairing, which is how an Ensembl GCA assembly and
    a RefSeq GCF annotation meet -- never by name), ``ncbi:annotated_gene_count`` per category (total,
    protein_coding, non_coding, pseudogene, other; annotation name/provider/date as qualifiers), assembly name,
    level and total sequence length.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

from reva.adapters.connectors import bio_common as bc
from reva.atlas.sources import AtlasSourceSpec

VERSION = "1.0.1"
PARSER_VERSION = "atlas_bio_molecular/1"
ATLAS_SOURCE = "bio"

HGNC_NAME = "atlas.hgnc"
HGNC_SOURCE_KEY = "hgnc.complete_set"
HGNC_DATASET = "hgnc_genes"
HGNC_HOST = "storage.googleapis.com"
HGNC_URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
HGNC_MAX_BYTES = 40_000_000
MIN_HGNC_ROWS_LIVE = 40_000

UNIPROT_NAME = "atlas.uniprot"
UNIPROT_SOURCE_KEY = "uniprot.rest"
UNIPROT_DATASET = "uniprot_reviewed"
UNIPROT_HOST = "rest.uniprot.org"
UNIPROT_API = "https://rest.uniprot.org"
UNIPROT_MAX_BYTES = 60_000_000
TAXON_CHUNK = 100  # UniProt refuses more than 100 OR conditions per query
FUNCTION_MAX = 1200
MIN_REVIEWED_LIVE = 500_000
MIN_HUMAN_LIVE = 20_000
HUMAN = "9606"
HUMAN_FIELDS = ("accession", "id", "gene_primary", "protein_name", "length", "cc_function", "xref_hgnc", "organism_id")

ENSEMBL_NAME = "atlas.ensembl_genomes"
ENSEMBL_SOURCE_KEY = "ensembl.rest"
ENSEMBL_DATASET = "ensembl_genomes"
ENSEMBL_HOST = "rest.ensembl.org"
ENSEMBL_API = "https://rest.ensembl.org"
KARYOTYPE_SPECIES = ("homo_sapiens", "mus_musculus")

NCBI_NAME = "atlas.ncbi_genomes"
NCBI_SOURCE_KEY = "ncbi.datasets"
NCBI_DATASET = "ncbi_genomes"
NCBI_HOST = "api.ncbi.nlm.nih.gov"
NCBI_API = "https://api.ncbi.nlm.nih.gov/datasets/v2"
NCBI_TAXA = (("7742", "Vertebrata"), ("33090", "Viridiplantae"))
NCBI_PAGE = 500
GENE_CATEGORIES = ("total", "protein_coding", "non_coding", "pseudogene", "other")

HGNC_RIGHTS = {
    "licence": "CC0 1.0",
    "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "attribution": "HGNC Database, HUGO Gene Nomenclature Committee (HGNC), European Molecular Biology Laboratory, "
    "European Bioinformatics Institute (EMBL-EBI), www.genenames.org (complete set, date in each revision).",
}
UNIPROT_RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "The UniProt Consortium. UniProt: the Universal Protein Knowledgebase (release in each revision). "
    "https://www.uniprot.org",
}
ENSEMBL_RIGHTS = {
    "licence": "Ensembl data: no restrictions on use (https://www.ensembl.org/info/about/legal/disclaimer.html)",
    "licence_url": "https://www.ensembl.org/info/about/legal/disclaimer.html",
    "attribution": "Ensembl (EMBL-EBI), REST API rest.ensembl.org (release in each revision).",
}
NCBI_RIGHTS = {
    "licence": "NCBI data: U.S. Government work, no restriction on use (https://www.ncbi.nlm.nih.gov/home/about/policies/)",
    "licence_url": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
    "attribution": "NCBI Datasets (National Center for Biotechnology Information), genome dataset reports.",
}

#: HGNC location vocabulary -> Ensembl seq-region names of the human karyotype (declared, not inferred).
HGNC_LOCATION_VOCAB = {"mitochondria": "MT"}
CHROM_PART_RE = re.compile(r"^(?P<c>[1-9]|1[0-9]|2[0-2]|X|Y)(?:[pq]|cen|\b|$)")


class MolecularError(bc.BioSourceError):
    pass


def code_digest() -> str:
    import sys

    return bc.digest(sys.modules[__name__], bc)


def header(raw: Any, name: str) -> str | None:
    hs = (getattr(raw, "response_metadata", None) or {}).get("headers") or {}
    for k, v in hs.items():
        if k.lower() == name.lower():
            return str(v)
    return None


def chromosome_node(taxid: str, name: str) -> str:
    return f"chromosome:{taxid}:{name}"


# ============================================================================================== HGNC
@dataclass(frozen=True)
class HgncGene:
    n: str  # numeric part of HGNC:<n>
    symbol: str
    name: str
    locus_group: str
    locus_type: str
    status: str
    location: str
    aliases: tuple[str, ...]
    uniprot: tuple[str, ...]
    ensembl: str
    entrez: str
    line: int


def _multi(v: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in (v or "").split("|") if x.strip())


def parse_hgnc(body: bytes) -> list[HgncGene]:
    text = body.decode("utf-8")
    rd = csv.DictReader(io.StringIO(text, newline=""), delimiter="\t")
    need = (
        "hgnc_id",
        "symbol",
        "name",
        "locus_group",
        "locus_type",
        "status",
        "location",
        "alias_symbol",
        "prev_symbol",
        "uniprot_ids",
        "ensembl_gene_id",
        "entrez_id",
    )
    missing = [c for c in need if c not in (rd.fieldnames or [])]
    if missing:
        raise MolecularError(f"HGNC complete set lacks columns {missing}", retryable=False, kind="schema_drift")
    out: list[HgncGene] = []
    line = 2
    for r in rd:
        hid = r.get("hgnc_id") or ""
        m = re.match(r"^HGNC:([1-9][0-9]{0,6})$", hid)
        if not m:
            raise MolecularError(f"line {line}: malformed hgnc_id {hid!r}", retryable=False, kind="schema_drift")
        out.append(
            HgncGene(
                n=m.group(1),
                symbol=r["symbol"] or "",
                name=r["name"] or "",
                locus_group=r["locus_group"] or "",
                locus_type=r["locus_type"] or "",
                status=r["status"] or "",
                location=r["location"] or "",
                aliases=tuple(dict.fromkeys(_multi(r["alias_symbol"]) + _multi(r["prev_symbol"]))),
                uniprot=_multi(r["uniprot_ids"]),
                ensembl=(r["ensembl_gene_id"] or "").strip(),
                entrez=(r["entrez_id"] or "").strip(),
                line=line,
            )
        )
        line = rd.reader.line_num + 1
    return out


def hgnc_chromosomes(location: str) -> tuple[list[str], str | None]:
    """Chromosome names in an HGNC location (``[]`` + reason when it names none)."""
    loc = location.strip()
    if not loc:
        return [], "empty"
    if loc in HGNC_LOCATION_VOCAB:
        return [HGNC_LOCATION_VOCAB[loc]], None
    out: list[str] = []
    for part in re.split(r"\s+and\s+", loc):
        m = CHROM_PART_RE.match(part.strip())
        if not m:
            return [], "not_a_chromosome_location"
        if m.group("c") not in out:
            out.append(m.group("c"))
    return out, None


UNIPROT_ACC_RE = re.compile(r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})$")


@dataclass
class KgParts:
    entities: list[Any]
    statements: list[Any]
    report: dict[str, Any]


def hgnc_kg(genes: Sequence[HgncGene], *, raw_sha256: str, revision: str, day: str) -> KgParts:
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    unlinked: Counter[str] = Counter()
    bad_acc: list[str] = []

    def st(subject: str, pid: str, sid: str, line: int, column: str, **kw: Any) -> None:
        sts.append(
            KgStatement(
                subject_qid=subject,
                property_pid=pid,
                statement_id=sid,
                source_revision=revision,
                raw_sha256=raw_sha256,
                references=[
                    {
                        "stated_in": "HGNC complete set",
                        "selector": {"file": "hgnc_complete_set.txt", "line": line, "column": column},
                    }
                ],
                **kw,
            )
        )
        rep[pid] += 1

    for g in genes:
        node = f"hgnc:{g.n}"
        ents.append(
            KgEntity(
                qid=node,
                kind=bc.KIND_GENE,
                labels={"en": g.symbol, "fr": g.symbol},
                descriptions={
                    "en": f"human gene (HGNC:{g.n}): {g.name}; {g.locus_type}; {g.location}"[:500],
                    "fr": f"gène humain (HGNC:{g.n}) : {g.name} ; {g.locus_type} ; {g.location}"[:500],
                },
                aliases={"en": list(g.aliases)} if g.aliases else {},
                source_revision=revision,
            )
        )
        st(node, "hgnc:symbol", f"symbol:{g.n}", g.line, "symbol", value=bc.string_value(g.symbol))
        st(node, "hgnc:name", f"name:{g.n}", g.line, "name", value=bc.string_value(g.name))
        if g.locus_type:
            st(node, "hgnc:locus_type", f"locus_type:{g.n}", g.line, "locus_type", value=bc.string_value(g.locus_type))
        if g.locus_group:
            st(
                node,
                "hgnc:locus_group",
                f"locus_group:{g.n}",
                g.line,
                "locus_group",
                value=bc.string_value(g.locus_group),
            )
        st(
            node,
            "hgnc:in_taxon",
            f"in_taxon:{g.n}",
            g.line,
            "hgnc_id",
            object_qid=f"ncbitaxon:{HUMAN}",
            qualifiers={"basis": "HGNC names human genes only"},
        )
        if g.location:
            st(
                node,
                "hgnc:cytogenetic_location",
                f"location:{g.n}",
                g.line,
                "location",
                value=bc.string_value(g.location),
            )
        chroms, why = hgnc_chromosomes(g.location)
        if why:
            unlinked[g.location if why == "not_a_chromosome_location" else why] += 1
        for c in chroms:
            st(
                node,
                "hgnc:located_on_chromosome",
                f"chromosome:{g.n}:{c}",
                g.line,
                "location",
                object_qid=chromosome_node(HUMAN, c),
                qualifiers={"location_as_stated": g.location, "chromosome_name": c},
            )
        for acc in g.uniprot:
            if not UNIPROT_ACC_RE.match(acc):
                bad_acc.append(acc)
                continue
            st(node, "hgnc:encodes", f"encodes:{g.n}:{acc}", g.line, "uniprot_ids", object_qid=f"uniprot:{acc}")
        if g.ensembl:
            st(
                node,
                "hgnc:ensembl_gene_id",
                f"ensembl:{g.n}",
                g.line,
                "ensembl_gene_id",
                value=bc.external_id(g.ensembl),
            )
        if g.entrez:
            st(node, "hgnc:ncbi_gene_id", f"entrez:{g.n}", g.line, "entrez_id", value=bc.external_id(g.entrez))
    report = {
        "genes": len(genes),
        "statements": len(sts),
        "by_property": dict(rep),
        "by_locus_group": dict(Counter(g.locus_group for g in genes)),
        "status": dict(Counter(g.status for g in genes)),
        "location_not_linked": dict(unlinked.most_common(30)),
        "malformed_uniprot_ids": bad_acc[:20],
        "as_of": day,
    }
    return KgParts(ents, sts, report)


async def ingest_hgnc(run: Any) -> dict[str, Any]:
    if not run.wants(HGNC_DATASET):
        return {"skipped": True}
    raw = await run.get(HGNC_URL, key="hgnc:complete_set", media_types=("text/plain",), max_bytes=HGNC_MAX_BYTES)
    etag = (header(raw, "etag") or "").strip('"').lower()
    md5_checked = bool(re.match(r"^[0-9a-f]{32}$", etag))
    if md5_checked and hashlib.md5(raw.body, usedforsecurity=False).hexdigest() != etag:
        # Cloud Storage's ETag of a non-composite object is its md5 (hex): a mismatch is a truncated body
        raise MolecularError("HGNC file does not match its ETag md5 (truncated?)", retryable=True, kind="checksum")
    sha = run.archive(raw)
    genes = parse_hgnc(raw.body)
    if run.origin == "live" and len(genes) < MIN_HGNC_ROWS_LIVE:
        raise MolecularError(f"only {len(genes)} HGNC rows: truncated?", retryable=False, kind="truncated")
    lm = header(raw, "last-modified")
    day = parsedate_to_datetime(lm).date().isoformat() if lm else raw.retrieved_at.date().isoformat()
    revision = f"hgnc_complete_set {day} sha256:{sha[:12]}"
    parts = hgnc_kg(genes, raw_sha256=sha, revision=revision, day=day)
    kg = bc.load_kg(
        run.engine,
        run.store,
        entities=parts.entities,
        statements=parts.statements,
        source_key=HGNC_SOURCE_KEY,
        raw_sha256s=[sha],
        retrieved_at=raw.retrieved_at,
        origin=run.origin,
        parameters={"connector": HGNC_NAME, "parser": PARSER_VERSION, "revision": revision},
        prefixes=("hgnc:",),
        retract_prefix="",
    )
    return {
        "revision": revision,
        "raw": {"sha256": sha, "bytes": len(raw.body), "md5_checked": md5_checked},
        "build": parts.report,
        "kg": kg,
    }


# ============================================================================================== UniProt
def uniprot_stream_url(query: str, fields: Sequence[str]) -> str:
    return f"{UNIPROT_API}/uniprotkb/stream?{urlencode({'query': query, 'fields': ','.join(fields), 'format': 'tsv'})}"


def taxonomy_url(ids: Sequence[str]) -> str:
    q = " OR ".join(f"tax_id:{i}" for i in ids)
    fields = "id,scientific_name,common_name,rank,parent"
    return f"{UNIPROT_API}/taxonomy/stream?query={quote(q)}&fields={fields}&format=tsv"


def tsv_rows(body: bytes, required: Sequence[str], what: str) -> list[dict[str, str]]:
    rd = csv.DictReader(io.StringIO(body.decode("utf-8"), newline=""), delimiter="\t", quoting=csv.QUOTE_NONE)
    missing = [c for c in required if c not in (rd.fieldnames or [])]
    if missing:
        raise MolecularError(f"{what}: TSV lacks columns {missing}", retryable=False, kind="schema_drift")
    return [{k: (v or "") for k, v in r.items() if k is not None} for r in rd]


def organism_counts(body: bytes) -> Counter[str]:
    rows = tsv_rows(body, ["Organism (ID)"], "UniProt organism ids")
    c: Counter[str] = Counter()
    for r in rows:
        oid = r["Organism (ID)"].strip()
        if not re.match(r"^[1-9][0-9]{0,9}$", oid):
            raise MolecularError(f"malformed organism id {oid!r}", retryable=False, kind="schema_drift")
        c[oid] += 1
    return c


@dataclass(frozen=True)
class Release:
    name: str
    date: str  # YYYY-MM-DD

    @property
    def revision(self) -> str:
        return f"UniProt {self.name} ({self.date})"


RELDATE_URL = "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/complete/reldate.txt"
RELDATE_RE = re.compile(
    r"UniProtKB/Swiss-Prot Release (?P<name>[0-9]{4}_[0-9]{2}) of (?P<date>[0-9]{2}-[A-Za-z]{3}-[0-9]{4})"
)


def uniprot_release(body: bytes) -> Release:
    """The Swiss-Prot release named in ``reldate.txt`` (fetched before and after the streams: must not change)."""
    m = RELDATE_RE.search(body.decode("utf-8", "replace"))
    if not m:
        raise MolecularError("reldate.txt does not name a Swiss-Prot release", retryable=False, kind="schema_drift")
    return Release(m.group("name"), datetime.strptime(m.group("date"), "%d-%b-%Y").date().isoformat())


def uniprot_kg(
    counts: Mapping[str, int],
    counts_sha: str,
    taxa: Mapping[str, tuple[dict[str, str], str]],
    human: Sequence[tuple[dict[str, str], int]],
    human_sha: str,
    rel: Release,
) -> KgParts:
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    rev = rel.revision
    when = bc.day_time(rel.date)
    issues: Counter[str] = Counter()

    def st(subject: str, pid: str, sid: str, raw: str, sel: Mapping[str, Any], **kw: Any) -> None:
        sts.append(
            KgStatement(
                subject_qid=subject,
                property_pid=pid,
                statement_id=sid,
                source_revision=rev,
                raw_sha256=raw,
                references=[{"stated_in": f"UniProtKB {rel.name}", "selector": dict(sel)}],
                **kw,
            )
        )
        rep[pid] += 1

    for oid in sorted(counts, key=lambda x: (len(x), x)):
        node = f"ncbitaxon:{oid}"
        t = taxa.get(oid)
        if t is not None:
            row, _sha = t
            sci = row.get("Scientific name") or ""
            common = row.get("Common name") or ""
            rank = row.get("Rank") or ""
            labels = {"en": sci, "fr": sci} if sci else {"en": f"NCBI taxon {oid}"}
            desc = f"{rank or 'taxon'} (NCBI Taxonomy {oid}, as named by UniProt)" + (f"; {common}" if common else "")
            desc_fr = f"{rank or 'taxon'} (NCBI Taxonomy {oid}, nom selon UniProt)" + (f" ; {common}" if common else "")
            ents.append(
                KgEntity(
                    qid=node,
                    kind=bc.KIND_TAXON,
                    labels=labels,
                    descriptions={"en": desc[:500], "fr": desc_fr[:500]},
                    aliases={"en": [common]} if common else {},
                    source_revision=rev,
                )
            )
        else:
            issues["organism_without_taxonomy_record"] += 1
        st(
            node,
            "uniprot:reviewed_protein_count",
            f"reviewed_count:{oid}",
            counts_sha,
            {"query": "reviewed:true", "field": "organism_id", "value": oid},
            value=bc.count_value(int(counts[oid]), "entries"),
            point_in_time=when,
            qualifiers={
                "definition": "number of UniProtKB/Swiss-Prot (reviewed:true) entries whose organism_id is "
                "this NCBI taxon id (exact count of the streamed entries)",
                "release": rel.name,
            },
        )
    for row, line in human:
        acc = row["Entry"].strip()
        if not UNIPROT_ACC_RE.match(acc):
            issues["malformed_accession"] += 1
            continue
        node = f"uniprot:{acc}"
        pname = row.get("Protein names") or ""
        label = re.split(r" \(", pname, maxsplit=1)[0].strip() or row.get("Entry Name") or acc
        gene = (row.get("Gene Names (primary)") or "").strip()
        ents.append(
            KgEntity(
                qid=node,
                kind=bc.KIND_PROTEIN,
                labels={"en": label[:500]},
                descriptions={
                    "en": f"protein (UniProtKB/Swiss-Prot {acc}, {row.get('Entry Name')})"
                    + (f", gene {gene}" if gene else "")
                    + ", Homo sapiens",
                    "fr": f"protéine (UniProtKB/Swiss-Prot {acc}, {row.get('Entry Name')})"
                    + (f", gène {gene}" if gene else "")
                    + ", Homo sapiens",
                },
                aliases={"en": [row["Entry Name"]]} if row.get("Entry Name") else {},
                source_revision=rev,
            )
        )
        sel = {"query": f"reviewed:true AND organism_id:{HUMAN}", "line": line}
        oid = (row.get("Organism (ID)") or "").strip()
        if oid:
            st(
                node,
                "uniprot:in_taxon",
                f"in_taxon:{acc}",
                human_sha,
                {**sel, "field": "organism_id"},
                object_qid=f"ncbitaxon:{oid}",
            )
        if gene:
            st(
                node,
                "uniprot:gene_name",
                f"gene_name:{acc}",
                human_sha,
                {**sel, "field": "gene_primary"},
                value=bc.string_value(gene),
            )
        ln = (row.get("Length") or "").strip()
        if re.match(r"^[0-9]+$", ln):
            st(
                node,
                "uniprot:sequence_length",
                f"length:{acc}",
                human_sha,
                {**sel, "field": "length"},
                value=bc.count_value(int(ln), "amino acids"),
            )
        fn = (row.get("Function [CC]") or "").strip()
        if fn:
            text = fn.removeprefix("FUNCTION: ")
            cut = len(text) > FUNCTION_MAX
            st(
                node,
                "uniprot:function",
                f"function:{acc}",
                human_sha,
                {**sel, "field": "cc_function"},
                value=bc.text_value(text[:FUNCTION_MAX], "en"),
                qualifiers={"truncated": cut, "characters_in_source": len(text)},
            )
        for h in (x.strip() for x in (row.get("HGNC") or "").split(";")):
            m = re.match(r"^HGNC:([1-9][0-9]{0,6})$", h)
            if h and not m:
                issues["malformed_hgnc_xref"] += 1
            if m:
                st(
                    node,
                    "uniprot:encoded_by",
                    f"encoded_by:{acc}:{m.group(1)}",
                    human_sha,
                    {**sel, "field": "xref_hgnc"},
                    object_qid=f"hgnc:{m.group(1)}",
                )
    return KgParts(
        ents,
        sts,
        {
            "organisms": len(counts),
            "reviewed_entries": sum(counts.values()),
            "human_entries": len(human),
            "statements": len(sts),
            "by_property": dict(rep),
            "issues": dict(issues),
        },
    )


async def ingest_uniprot(run: Any) -> dict[str, Any]:
    if not run.wants(UNIPROT_DATASET):
        return {"skipped": True}
    raws: list[Any] = []
    r_rel = await run.get(RELDATE_URL, key="uniprot:reldate:before", media_types=("text/plain",), max_bytes=100_000)
    rel = uniprot_release(r_rel.body)
    rel_sha = run.archive(r_rel)
    raws.append(r_rel)
    r_org = await run.get(
        uniprot_stream_url("reviewed:true", ["organism_id"]),
        key="uniprot:reviewed:organism_id",
        media_types=("text/plain",),
        max_bytes=UNIPROT_MAX_BYTES,
    )
    org_sha = run.archive(r_org)
    raws.append(r_org)
    counts = organism_counts(r_org.body)
    if run.origin == "live" and sum(counts.values()) < MIN_REVIEWED_LIVE:
        raise MolecularError("reviewed entry stream looks truncated", retryable=True, kind="truncated")
    taxa: dict[str, tuple[dict[str, str], str]] = {}
    ids = sorted(counts, key=lambda x: (len(x), x))
    for i in range(0, len(ids), TAXON_CHUNK):
        r = await run.get(
            taxonomy_url(ids[i : i + TAXON_CHUNK]),
            key=f"uniprot:taxonomy:{i}",
            media_types=("text/plain",),
            max_bytes=5_000_000,
        )
        sha = run.archive(r)
        raws.append(r)
        for row in tsv_rows(r.body, ["Taxon Id", "Scientific name"], "UniProt taxonomy"):
            taxa[row["Taxon Id"].strip()] = (row, sha)
    r_h = await run.get(
        uniprot_stream_url(f"reviewed:true AND organism_id:{HUMAN}", HUMAN_FIELDS),
        key="uniprot:reviewed:human",
        media_types=("text/plain",),
        max_bytes=UNIPROT_MAX_BYTES,
    )
    h_sha = run.archive(r_h)
    raws.append(r_h)
    r_rel2 = await run.get(
        RELDATE_URL + "?after=1", key="uniprot:reldate:after", media_types=("text/plain",), max_bytes=100_000
    )
    run.archive(r_rel2)
    raws.append(r_rel2)
    if uniprot_release(r_rel2.body) != rel:
        raise MolecularError("UniProt release changed during the run", retryable=True, kind="version_mismatch")
    rows = tsv_rows(r_h.body, ["Entry", "Entry Name", "Length", "HGNC", "Organism (ID)"], "UniProt human")
    human = [(r, i + 2) for i, r in enumerate(rows)]
    if run.origin == "live" and len(human) < MIN_HUMAN_LIVE:
        raise MolecularError("human reviewed stream looks truncated", retryable=True, kind="truncated")
    parts = uniprot_kg(counts, org_sha, taxa, human, h_sha, rel)
    kg = bc.load_kg(
        run.engine,
        run.store,
        entities=parts.entities,
        statements=parts.statements,
        source_key=UNIPROT_SOURCE_KEY,
        raw_sha256s=sorted({rel_sha, org_sha, h_sha, *(s for _r, s in taxa.values())}),
        retrieved_at=max(r.retrieved_at for r in raws),
        origin=run.origin,
        parameters={"connector": UNIPROT_NAME, "parser": PARSER_VERSION, "release": rel.__dict__},
        prefixes=("uniprot:",),
        retract_prefix="",
    )
    return {
        "release": rel.__dict__,
        "raw": {"requests": len(raws), "bytes": bc.raw_bytes_total(raws)},
        "taxonomy_records": len(taxa),
        "build": parts.report,
        "kg": kg,
    }


# ============================================================================================== Ensembl
def ensembl_kg(
    species: Sequence[Mapping[str, Any]],
    sp_sha: str,
    karyotypes: Mapping[str, tuple[Mapping[str, Any], str]],
    *,
    release: str,
    known_taxa: set[str],
) -> KgParts:
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    issues: Counter[str] = Counter()
    rev = f"Ensembl release {release}"
    seen_asm: set[str] = set()

    def st(subject: str, pid: str, sid: str, raw: str, sel: Mapping[str, Any], **kw: Any) -> None:
        sts.append(
            KgStatement(
                subject_qid=subject,
                property_pid=pid,
                statement_id=sid,
                source_revision=rev,
                raw_sha256=raw,
                references=[{"stated_in": f"Ensembl release {release}", "selector": dict(sel)}],
                **kw,
            )
        )
        rep[pid] += 1

    by_name: dict[str, tuple[str, str]] = {}
    for i, s in enumerate(species):
        acc = str(s.get("accession") or "")
        tax = str(s.get("taxon_id") or "")
        name = str(s.get("name") or "")
        if not re.match(r"^GCA_[0-9]{9}\.[0-9]+$", acc):
            issues["no_insdc_accession"] += 1
            continue
        if not re.match(r"^[1-9][0-9]{0,9}$", tax):
            issues["no_taxon_id"] += 1
            continue
        by_name[name] = (acc, tax)
        node = f"assembly:{acc}"
        sel = {"endpoint": "info/species", "index": i, "name": name}
        if acc not in seen_asm:
            seen_asm.add(acc)
            disp = str(s.get("display_name") or name)
            asm = str(s.get("assembly") or "")
            ents.append(
                KgEntity(
                    qid=node,
                    kind=bc.KIND_ASSEMBLY,
                    labels={"en": f"{asm} ({acc})", "fr": f"{asm} ({acc})"},
                    descriptions={
                        "en": f"genome assembly used by Ensembl for {disp}",
                        "fr": f"assemblage génomique utilisé par Ensembl pour {disp}",
                    },
                    source_revision=rev,
                )
            )
            if asm:
                st(
                    node,
                    "ensembl:assembly_name",
                    f"assembly_name:{acc}",
                    sp_sha,
                    {**sel, "field": "assembly"},
                    value=bc.string_value(asm),
                )
        st(
            node,
            "ensembl:assembly_of",
            f"assembly_of:{acc}:{tax}:{name}",
            sp_sha,
            {**sel, "field": "taxon_id"},
            object_qid=f"ncbitaxon:{tax}",
            qualifiers={"ensembl_species": name, "display_name": s.get("display_name")},
        )
        if tax not in known_taxa:
            known_taxa.add(tax)
            sci = str(s.get("scientific_name") or "") or name.replace("_", " ").capitalize()
            ents.append(
                KgEntity(
                    qid=f"ncbitaxon:{tax}",
                    kind=bc.KIND_TAXON,
                    labels={"en": sci, "fr": sci},
                    descriptions={
                        "en": f"taxon (NCBI Taxonomy {tax}, as named by Ensembl)",
                        "fr": f"taxon (NCBI Taxonomy {tax}, nom selon Ensembl)",
                    },
                    source_revision=rev,
                )
            )
            rep["ncbitaxon_entity"] += 1
    for name, (doc, sha) in sorted(karyotypes.items()):
        if name not in by_name:
            issues["karyotype_species_not_listed"] += 1
            continue
        acc, tax = by_name[name]
        if str(doc.get("assembly_accession")) != acc:
            issues["karyotype_assembly_differs"] += 1
            continue
        node = f"assembly:{acc}"
        sel = {"endpoint": f"info/assembly/{name}"}
        gpl = doc.get("golden_path")
        if isinstance(gpl, int):
            st(
                node,
                "ensembl:golden_path_length",
                f"golden_path:{acc}",
                sha,
                {**sel, "field": "golden_path"},
                value=bc.count_value(gpl, "bp"),
            )
        if doc.get("genebuild_last_geneset_update") or doc.get("genebuild_method"):
            gb = f"{doc.get('genebuild_method') or ''} (last geneset update {doc.get('genebuild_last_geneset_update')})"
            st(
                node,
                "ensembl:genebuild",
                f"genebuild:{acc}",
                sha,
                {**sel, "field": "genebuild_*"},
                value=bc.string_value(gb.strip()),
            )
        lengths = {
            str(r.get("name")): r.get("length")
            for r in doc.get("top_level_region") or []
            if r.get("coord_system") == "chromosome"
        }
        sci = next((str(s.get("display_name") or "") for s in species if s.get("name") == name), name)
        for c in doc.get("karyotype") or []:
            c = str(c)
            cn = chromosome_node(tax, c)
            ln = lengths.get(c)
            ents.append(
                KgEntity(
                    qid=cn,
                    kind=bc.KIND_CHROMOSOME,
                    labels={"en": f"chromosome {c} ({sci})", "fr": f"chromosome {c} ({sci})"},
                    descriptions={
                        "en": f"chromosome {c} of NCBI taxon {tax} (Ensembl karyotype; "
                        "REVA key: taxon id + seq-region name)",
                        "fr": f"chromosome {c} du taxon NCBI {tax} (caryotype Ensembl ; "
                        "clé REVA : id de taxon + nom de région)",
                    },
                    source_revision=rev,
                )
            )
            q: dict[str, Any] = {"seq_region_name": c}
            if isinstance(ln, int):
                q["length_bp"] = str(ln)
            st(
                node,
                "ensembl:has_chromosome",
                f"chromosome:{acc}:{c}",
                sha,
                {**sel, "field": "karyotype", "value": c},
                object_qid=cn,
                qualifiers=q,
            )
    return KgParts(
        ents,
        sts,
        {
            "species": len(species),
            "assemblies": len(seen_asm),
            "statements": len(sts),
            "by_property": dict(rep),
            "issues": dict(issues),
        },
    )


async def ingest_ensembl(run: Any) -> dict[str, Any]:
    if not run.wants(ENSEMBL_DATASET):
        return {"skipped": True}
    raws: list[Any] = []
    r = await run.get(
        f"{ENSEMBL_API}/info/species?content-type=application/json;division=EnsemblVertebrates",
        key="ensembl:info:species",
        max_bytes=10_000_000,
    )
    sp_sha = run.archive(r)
    raws.append(r)
    doc = bc.json_body(r, "Ensembl species")
    species = list(doc.get("species") or [])
    if not species:
        raise MolecularError("Ensembl species list is empty", retryable=True, kind="truncated")
    releases = sorted({str(s.get("release")) for s in species if s.get("release")})
    release = releases[-1] if releases else "unknown"
    kar: dict[str, tuple[Mapping[str, Any], str]] = {}
    for name in bc.opt_list((run.options or {}).get("karyotypes")) or KARYOTYPE_SPECIES:
        ra = await run.get(
            f"{ENSEMBL_API}/info/assembly/{name}?content-type=application/json",
            key=f"ensembl:info:assembly:{name}",
            max_bytes=5_000_000,
        )
        kar[str(name)] = (bc.json_body(ra, f"Ensembl assembly {name}"), run.archive(ra))
        raws.append(ra)
    known = {q.split(":", 1)[1] for q in bc.current_entities(run.engine, run.origin, "ncbitaxon:%")}
    parts = ensembl_kg(species, sp_sha, kar, release=release, known_taxa=known)
    kg = bc.load_kg(
        run.engine,
        run.store,
        entities=parts.entities,
        statements=parts.statements,
        source_key=ENSEMBL_SOURCE_KEY,
        raw_sha256s=sorted({sp_sha, *(v[1] for v in kar.values())}),
        retrieved_at=max(x.retrieved_at for x in raws),
        origin=run.origin,
        parameters={"connector": ENSEMBL_NAME, "parser": PARSER_VERSION, "release": release},
        prefixes=("ensembl:",),
        retract_prefix="",
    )
    return {
        "release": release,
        "raw": {"requests": len(raws), "bytes": bc.raw_bytes_total(raws)},
        "build": parts.report,
        "kg": kg,
    }


# ============================================================================================== NCBI Datasets
def ncbi_url(taxon: str, token: str | None) -> str:
    q = [("filters.reference_only", "true"), ("filters.has_annotation", "true"), ("page_size", str(NCBI_PAGE))]
    if token:
        q.append(("page_token", token))
    return f"{NCBI_API}/genome/taxon/{taxon}/dataset_report?{urlencode(q)}"


def ncbi_kg(
    reports: Sequence[tuple[Mapping[str, Any], str, dict[str, Any]]], *, known_taxa: set[str], retrieved_day: str
) -> KgParts:
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    issues: Counter[str] = Counter()
    seen: set[str] = set()
    for r, sha, sel in reports:
        acc = str(r.get("accession") or "")
        if not re.match(r"^GC[AF]_[0-9]{9}\.[0-9]+$", acc):
            issues["malformed_accession"] += 1
            continue
        if acc in seen:
            continue
        seen.add(acc)
        org = r.get("organism") or {}
        ai = r.get("assembly_info") or {}
        ann = r.get("annotation_info") or {}
        stats = r.get("assembly_stats") or {}
        tax = str(org.get("tax_id") or "")
        rev = f"NCBI Datasets v2 dataset report {acc} (retrieved {retrieved_day})"
        node = f"assembly:{acc}"
        ref = [{"stated_in": "NCBI Datasets genome dataset report", "selector": dict(sel)}]

        def st(
            pid: str,
            sid: str,
            _rev: str = rev,
            _ref: list[dict[str, Any]] = ref,
            _sha: str = sha,
            _node: str = node,
            **kw: Any,
        ) -> None:
            sts.append(
                KgStatement(
                    subject_qid=_node,
                    property_pid=pid,
                    statement_id=sid,
                    source_revision=_rev,
                    raw_sha256=_sha,
                    references=_ref,
                    **kw,
                )
            )
            rep[pid] += 1

        name = str(ai.get("assembly_name") or acc)
        oname = str(org.get("organism_name") or "")
        ents.append(
            KgEntity(
                qid=node,
                kind=bc.KIND_ASSEMBLY,
                labels={"en": f"{name} ({acc})", "fr": f"{name} ({acc})"},
                descriptions={
                    "en": f"reference genome assembly of {oname} (NCBI)",
                    "fr": f"assemblage génomique de référence de {oname} (NCBI)",
                },
                source_revision=rev,
            )
        )
        if re.match(r"^[1-9][0-9]{0,9}$", tax):
            st(
                "ncbi:assembly_of",
                f"assembly_of:{acc}",
                object_qid=f"ncbitaxon:{tax}",
                qualifiers={"refseq_category": ai.get("refseq_category"), "organism_name": oname},
            )
            if tax not in known_taxa:
                known_taxa.add(tax)
                ents.append(
                    KgEntity(
                        qid=f"ncbitaxon:{tax}",
                        kind=bc.KIND_TAXON,
                        labels={"en": oname, "fr": oname} if oname else {"en": f"NCBI taxon {tax}"},
                        descriptions={
                            "en": f"taxon (NCBI Taxonomy {tax}, as named by NCBI Datasets)",
                            "fr": f"taxon (NCBI Taxonomy {tax}, nom selon NCBI Datasets)",
                        },
                        aliases={"en": [str(org["common_name"])]} if org.get("common_name") else {},
                        source_revision=rev,
                    )
                )
        paired = str(r.get("paired_accession") or "")
        if re.match(r"^GC[AF]_[0-9]{9}\.[0-9]+$", paired) and paired != acc:
            st("ncbi:paired_assembly", f"paired:{acc}", object_qid=f"assembly:{paired}")
        if ai.get("assembly_name"):
            st("ncbi:assembly_name", f"assembly_name:{acc}", value=bc.string_value(str(ai["assembly_name"])))
        if ai.get("assembly_level"):
            st("ncbi:assembly_level", f"assembly_level:{acc}", value=bc.string_value(str(ai["assembly_level"])))
        tsl = stats.get("total_sequence_length")
        if tsl is not None and re.match(r"^[0-9]+$", str(tsl)):
            st("ncbi:total_sequence_length", f"total_length:{acc}", value=bc.count_value(int(str(tsl)), "bp"))
        gc = (ann.get("stats") or {}).get("gene_counts") or {}
        when = None
        if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}", str(ann.get("release_date") or "")):
            when = bc.day_time(str(ann["release_date"]))
        for cat in GENE_CATEGORIES:
            v = gc.get(cat)
            if v is None:
                continue
            st(
                "ncbi:annotated_gene_count",
                f"gene_count:{acc}:{cat}",
                value=bc.count_value(bc.as_int(v, cat), "genes"),
                point_in_time=when,
                qualifiers={
                    "category": cat,
                    "annotation": ann.get("name"),
                    "provider": ann.get("provider"),
                    "release_date": ann.get("release_date"),
                },
            )
        if not gc:
            issues["no_gene_counts"] += 1
    return KgParts(
        ents, sts, {"assemblies": len(seen), "statements": len(sts), "by_property": dict(rep), "issues": dict(issues)}
    )


async def ingest_ncbi(run: Any) -> dict[str, Any]:
    if not run.wants(NCBI_DATASET):
        return {"skipped": True}
    raws: list[Any] = []
    shas: list[str] = []
    reports: list[tuple[Mapping[str, Any], str, dict[str, Any]]] = []
    totals: dict[str, int] = {}
    for taxon, label in NCBI_TAXA:
        token: str | None = None
        page = 0
        while True:
            r = await run.get(ncbi_url(taxon, token), key=f"ncbi:genome:{taxon}:page{page}", max_bytes=20_000_000)
            sha = run.archive(r)
            raws.append(r)
            shas.append(sha)
            doc = bc.json_body(r, f"NCBI genome reports {taxon}")
            totals[label] = int(doc.get("total_count") or 0)
            for i, rep in enumerate(doc.get("reports") or []):
                reports.append((rep, sha, {"taxon": taxon, "page": page, "index": i}))
            token = doc.get("next_page_token")
            page += 1
            if not token or page > 50:
                break
    known = {q.split(":", 1)[1] for q in bc.current_entities(run.engine, run.origin, "ncbitaxon:%")}
    day = max(x.retrieved_at for x in raws).date().isoformat()
    parts = ncbi_kg(reports, known_taxa=known, retrieved_day=day)
    kg = bc.load_kg(
        run.engine,
        run.store,
        entities=parts.entities,
        statements=parts.statements,
        source_key=NCBI_SOURCE_KEY,
        raw_sha256s=sorted(set(shas)),
        retrieved_at=max(x.retrieved_at for x in raws),
        origin=run.origin,
        parameters={"connector": NCBI_NAME, "parser": PARSER_VERSION, "taxa": [t for t, _ in NCBI_TAXA]},
        prefixes=("ncbi:",),
        retract_prefix="",
    )
    return {
        "totals": totals,
        "reports": len(reports),
        "raw": {"requests": len(raws), "bytes": bc.raw_bytes_total(raws)},
        "build": parts.report,
        "kg": kg,
    }


# ============================================================================================== atlas protocol
def atlas_source() -> list[AtlasSourceSpec]:
    digest = code_digest()

    def spec(
        name: str,
        sk: str,
        provider: str,
        rights: Mapping[str, str],
        hosts: tuple[str, ...],
        dataset: str,
        ids: tuple[str, ...],
        requests: int,
        max_bytes: int,
        interval: float,
        notes: str,
        fields: dict[str, Any],
    ) -> AtlasSourceSpec:
        return AtlasSourceSpec(
            name=name,
            version=VERSION,
            source_key=sk,
            budget_key=sk,
            atlas_source=ATLAS_SOURCE,
            provider=provider,
            licence=f"{rights['licence']} ({rights['licence_url']}); attribution: {rights['attribution']}",
            allowed_hosts=hosts,
            datasets=(dataset,),
            dataset_ids=ids,
            cadence_seconds=30 * bc.DAY,
            planned_requests=requests,
            max_response_bytes=max_bytes,
            min_interval_seconds=interval,
            parser_version=PARSER_VERSION,
            code_digest=digest,
            field_mappings=fields,
            notes=notes,
        )

    return [
        spec(
            HGNC_NAME,
            HGNC_SOURCE_KEY,
            "HUGO Gene Nomenclature Committee (HGNC), complete set",
            HGNC_RIGHTS,
            (HGNC_HOST,),
            HGNC_DATASET,
            ("hgnc:hgnc_complete_set",),
            3,
            HGNC_MAX_BYTES,
            1.0,
            "KG only: human genes, chromosome links, UniProt cross-references.",
            {"gene": "hgnc_id -> hgnc:<n>", "chromosome": "location (chromosome part)", "encodes": "uniprot_ids"},
        ),
        spec(
            UNIPROT_NAME,
            UNIPROT_SOURCE_KEY,
            "UniProt Consortium, UniProtKB REST API",
            UNIPROT_RIGHTS,
            (UNIPROT_HOST, "ftp.uniprot.org"),
            UNIPROT_DATASET,
            ("uniprotkb:reviewed",),
            170,
            UNIPROT_MAX_BYTES,
            1.0,
            "KG only: reviewed entry counts per organism, organism names, human reviewed proteins.",
            {"counts": "stream reviewed:true fields=organism_id", "human": "stream fields=" + ",".join(HUMAN_FIELDS)},
        ),
        spec(
            ENSEMBL_NAME,
            ENSEMBL_SOURCE_KEY,
            "Ensembl (EMBL-EBI), REST API",
            ENSEMBL_RIGHTS,
            (ENSEMBL_HOST,),
            ENSEMBL_DATASET,
            ("ensembl:info/species", "ensembl:info/assembly"),
            10,
            10_000_000,
            1.0,
            "KG only: Ensembl vertebrate assemblies per species and karyotypes of selected species.",
            {"assembly": "info/species accession", "karyotype": "info/assembly karyotype + top_level_region"},
        ),
        spec(
            NCBI_NAME,
            NCBI_SOURCE_KEY,
            "NCBI Datasets v2 (National Center for Biotechnology Information)",
            NCBI_RIGHTS,
            (NCBI_HOST,),
            NCBI_DATASET,
            ("ncbi:datasets/v2/genome/dataset_report",),
            20,
            20_000_000,
            1.0,
            "KG only: reference annotated genomes (vertebrates, green plants) with gene counts.",
            {"assembly": "accession / paired_accession", "gene_counts": "annotation_info.stats.gene_counts"},
        ),
    ]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == HGNC_NAME:
        return await ingest_hgnc(run)
    if run.spec.name == UNIPROT_NAME:
        return await ingest_uniprot(run)
    if run.spec.name == ENSEMBL_NAME:
        return await ingest_ensembl(run)
    return await ingest_ncbi(run)
