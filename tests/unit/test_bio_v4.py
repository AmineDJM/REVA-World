"""V4BIO offline tests: GBIF backbone / COL / Wikidata bridges / HGNC / UniProt / Ensembl / NCBI parsing and KG
records from the captured (trimmed) fixtures; facet partitioning with a fake client (no network, no database)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_bio_molecular as mo
from reva.adapters.connectors import atlas_bio_taxonomy as tx
from reva.adapters.connectors import bio_common as bc
from reva.kg import families

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "source"


def _fx(conn: str, name: str) -> bytes:
    return (FIX / conn / name).read_bytes()


def _by(sts: list[Any], pid: str, subject: str | None = None) -> list[Any]:
    return [s for s in sts if s.property_pid == pid and (subject is None or s.subject_qid == subject)]


# ------------------------------------------------------------------------------------------------ registry files
def test_predicates_and_family_mappings_are_consistent() -> None:
    preds = {p["pid"]: p for p in bc.predicate_docs()}
    assert all(p.pid in preds for p in bc.load_predicates())
    drop = yaml.safe_load((REPO / "registry/kg/relations_v4.d/v4bio.yaml").read_text())
    mapped = {m["property"]: m["family"] for m in drop["mappings"]}
    for pid, fam in mapped.items():
        assert preds[pid]["value_kind"] == "item"
        assert preds[pid].get("family") == fam, pid
    reg = families.load_registry()
    assert reg.mapping("hgnc:encodes") is not None and reg.mapping("hgnc:encodes").family == "produces"
    assert reg.mapping("gbif:parent").family == "is_a"
    # identity bridges stay unmapped; nothing causal
    assert reg.mapping("bio:same_taxon_via_wikidata") is None
    assert not any(w in p for p in preds for w in ("cause", "causal", "effect"))


def test_scope_file_is_well_formed() -> None:
    scope = tx.load_scope()
    assert scope.group_keys() == [359, 212]
    keys = [s.key for s in scope.species]
    assert len(keys) == len(set(keys)) and len(keys) >= 150
    groups = {s.group for s in scope.species}
    assert {"crops", "livestock", "pathogens", "model_organisms", "iconic_threatened"} <= groups
    assert next(s for s in scope.species if s.name == "Homo sapiens").key == 2436436


# ------------------------------------------------------------------------------------------------ GBIF backbone
def test_backbone_meta_checks_identity_licence_and_version() -> None:
    g = _fx("atlas.gbif_backbone", "gbif_dataset.json")
    c = _fx("atlas.gbif_backbone", "clb_dataset.json")
    meta = tx.parse_backbone_meta(g, c)
    assert meta.pub_date == "2023-08-28" and meta.revision.startswith("backbone 2023-08-28 (ChecklistBank 53147")
    bad = json.loads(c)
    bad["gbifKey"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(tx.TaxonomyError):
        tx.parse_backbone_meta(g, json.dumps(bad).encode())
    bad = json.loads(c)
    bad["version"] = "2026-01-01"
    with pytest.raises(tx.TaxonomyError, match="differs"):
        tx.parse_backbone_meta(g, json.dumps(bad).encode())
    gl = json.loads(g)
    gl["license"] = "http://creativecommons.org/licenses/by-nc/4.0/legalcode"
    with pytest.raises(tx.TaxonomyError, match="licence"):
        tx.parse_backbone_meta(json.dumps(gl).encode(), c)


def _usages() -> dict[str, tx.Usage]:
    out: dict[str, tx.Usage] = {}
    for rank in tx.HIGHER_RANKS:
        res, total, last = tx.parse_clb_page(_fx("atlas.gbif_backbone", f"clb_{rank}.json"), what=rank)
        assert last and len(res) == total
        for i, rec in enumerate(res):
            u = tx.usage_from_clb(rec, raw="a" * 64, selector={"rank": rank, "index": i})
            assert u is not None
            out[u.id] = u
    return out


def test_clb_usages_and_species_classification_give_genera() -> None:
    us = _usages()
    assert us["9703"].name == "Felidae" and us["9703"].rank == "family" and us["9703"].parent_id == "732"
    assert us["359"].parent_id == "44" and us["1"].rank == "kingdom"
    res, _t, _l = tx.parse_clb_page(_fx("atlas.gbif_backbone", "clb_species_of_9703.json"), what="sp")
    sp = [u for r in res if (u := tx.usage_from_clb(r, raw="b" * 64, selector={})) is not None]
    puma = next(u for u in sp if u.name == "Puma concolor")
    assert puma.id == "2435099" and puma.parent_id == "2435098"
    gen = tx.genus_usages_from_species(sp)
    assert gen["2435098"].name == "Puma" and gen["2435098"].parent_id == "9703"
    assert gen["2435098"].parent_basis == "classification"


def test_scope_record_check_refuses_anything_but_the_stated_accepted_species() -> None:
    rec = json.loads(_fx("atlas.gbif_backbone", "gbif_species_2436436.json"))
    sp = tx.ScopeSpecies(2436436, "Homo sapiens", "model_organisms")
    assert tx.check_scope_record(rec, sp) is None
    u = tx.usage_from_gbif_species(rec, raw="c" * 64)
    assert u.parent_id == "2436435" and u.classification[-2] == ("2436435", "Homo", "genus")
    assert tx.check_scope_record(rec, tx.ScopeSpecies(2436436, "Homo neanderthalensis", "x")) == "canonical_name_differs"
    assert tx.check_scope_record({**rec, "taxonomicStatus": "SYNONYM"}, sp) == "status_SYNONYM"


class _FacetHttp:
    """Fake run: partitions answer facets from a dict; truncation simulated with a small FACET_LIMIT."""

    def __init__(self, answers: Mapping[str | None, list[tuple[str, int]]], totals: Mapping[str | None, int]):
        self.answers, self.totals, self.calls = answers, totals, []

    async def get(self, url: str, *, key: str, max_bytes: int) -> Any:
        part = key.split(":", 2)[2]
        p = None if part == "root" else part
        self.calls.append(p)
        body = json.dumps({"count": self.totals[p], "facets": [{"counts": [
            {"name": k, "count": c} for k, c in self.answers[p]]}]}).encode()
        return type("R", (), {"body": body, "retrieved_at": datetime(2026, 10, 17, tzinfo=UTC)})()

    def archive(self, raw: Any) -> str:
        return "d" * 64


async def test_facet_partitions_recurse_only_where_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tx, "FACET_LIMIT", 3)
    # root lists 3 values (truncated): kingdom 1 and 2 + one genus; family 30 and order 20 are missing
    answers = {
        None: [("1", 100), ("2", 50), ("99", 40)],
        "1": [("1", 100), ("20", 60)],  # complete (2 < 3): 30 absent -> exhaustive zero
        "2": [("2", 50), ("40", 7), ("41", 5)],  # truncated, its child 42 is unknown -> missing
    }
    totals = {None: 150, "1": 100, "2": 50}
    run = _FacetHttp(answers, totals)
    children = {"1": ["20"], "20": ["30"], "2": ["40", "42"]}
    fc = await tx.facet_counts(run, needed={"1", "2", "20", "30", "40", "42"}, children=children, roots=["1", "2"],
                               raws=[])
    assert run.calls == [None, "1", "2"]
    assert fc.counts["20"][0] == 60 and fc.counts["40"][0] == 7
    assert fc.counts["30"][:2] == (0, "absent_from_exhaustive_facet")
    assert fc.missing == ["42"]
    assert [p["truncated"] for p in fc.partitions] == [True, False, True]


def test_gbif_kg_records() -> None:
    us = _usages()
    meta = tx.parse_backbone_meta(_fx("atlas.gbif_backbone", "gbif_dataset.json"),
                                  _fx("atlas.gbif_backbone", "clb_dataset.json"))
    vals, _t = tx.parse_facet(_fx("atlas.gbif_backbone", "gbif_facet_root.json"))
    counts = tx.FacetCounts({k: (c, "facet_value", "root", "e" * 64) for k, c in vals}, [], [], [])
    parts = tx.gbif_kg(us, counts, meta=meta, scope_groups={"9703": "test"})
    ent = {e.qid: e for e in parts.entities}
    assert ent["gbif:359"].kind == "taxon" and ent["gbif:359"].labels["en"] == "Mammalia"
    assert "REVA scope list: test" in ent["gbif:9703"].descriptions["en"]
    par = _by(parts.statements, "gbif:parent", "gbif:9703")
    assert par[0].object_qid == "gbif:732" and par[0].statement_id == "parent:9703"
    cnt = _by(parts.statements, "gbif:accepted_species_count", "gbif:359")[0]
    assert cnt.value["unit"] == "species" and int(cnt.value["amount"]) > 20000
    assert "status=ACCEPTED" in cnt.qualifiers["definition"] and cnt.point_in_time.iso.startswith("2023-08-28")
    from reva.kg.model import _normalise_statement  # validator of the loader, offline

    props = {p.pid: p.value_kind for p in bc.load_predicates()}
    for i, s in enumerate(parts.statements):
        _normalise_statement(i, s, tx.GBIF_SOURCE_KEY, {"a" * 64, "e" * 64}, props)


# ------------------------------------------------------------------------------------------------ COL
def test_col_release_metrics_and_counts() -> None:
    rel = tx.parse_col_release(_fx("atlas.col_checklist", "col_release.json"))
    assert rel.title.startswith("Catalogue of Life") and rel.doi.startswith("10.48580/")
    imp = json.loads(_fx("atlas.col_checklist", "col_import.json"))[0]
    us: dict[str, tx.Usage] = {}
    for rank in tx.COL_RANKS:
        res, _t, _l = tx.parse_clb_page(_fx("atlas.col_checklist", f"col_{rank}.json"), what=rank)
        for r in res:
            u = tx.usage_from_clb(r, raw="a" * 64, selector={})
            if u is not None:
                us[u.id] = u
    facets = {}
    for v in ("all", "extant"):
        vals, total = tx.parse_clb_facet(_fx("atlas.col_checklist", f"col_facet_{v}.json"))
        facets[v] = (vals, total, "f" * 64)
    parts = tx.col_kg(rel, imp, "m" * 64, us, facets, {"all": False, "extant": False})
    mam = next(k for k, u in us.items() if u.name == "Mammalia")
    par = _by(parts.statements, "col:parent", f"col:{mam}")[0]
    assert us[par.object_qid.split(":", 1)[1]].name == "Chordata"
    assert "Tetrapoda (megaclass)" in par.qualifiers["intermediate_ranks_skipped"]
    n = {s.qualifiers["variant"]: int(s.value["amount"]) for s in _by(parts.statements, "col:species_count", f"col:{mam}")}
    assert n["extant"] < n["all"]
    metrics = _by(parts.statements, "col:release_metric", f"col:release.{rel.key}")
    assert any(s.qualifiers.get("metric") == "taxonCount" for s in metrics)
    with pytest.raises(tx.TaxonomyError):
        tx.parse_col_release(json.dumps({**json.loads(_fx("atlas.col_checklist", "col_release.json")),
                                         "license": "cc by-nc"}).encode())


# ------------------------------------------------------------------------------------------------ Wikidata
def _b(**kw: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in kw.items():
        if k == "cn_fr":
            out["cn"] = {"type": "literal", "value": v, "xml:lang": "fr"}
        elif k in ("item",):
            out[k] = {"type": "uri", "value": f"http://www.wikidata.org/entity/{v}"}
        elif k == "st":
            out[k] = {"type": "uri", "value": f"http://www.wikidata.org/entity/statement/{v}"}
        else:
            out[k] = {"type": "literal", "value": v}
    return out


def test_wikidata_bridges_need_unique_values_both_ways() -> None:
    rows = [
        (_b(item="Q35255", st="Q35255-ABC", gbif="2435099", ncbi="9696", cn_fr="Puma"), "s1"),
        (_b(item="Q1", st="Q1-X", gbif="111", ncbi="5"), "s1"),
        (_b(item="Q2", st="Q2-X", gbif="222", ncbi="5"), "s1"),  # NCBI 5 on two items: ambiguous
        (_b(item="Q3", st="Q3-X", gbif="333", col="4QHKG"), "s1"),
    ]
    parts = tx.wikidata_kg(rows, retrieved_at=datetime(2026, 10, 17, tzinfo=UTC),
                           loaded_gbif={"2435099", "111", "222", "333"}, loaded_ncbi=set(), loaded_col=set())
    same = {(s.subject_qid, s.object_qid) for s in _by(parts.statements, "bio:same_taxon_via_wikidata")}
    assert ("ncbitaxon:9696", "gbif:2435099") in same and ("col:4QHKG", "gbif:333") in same
    assert not any(o in ("gbif:111", "gbif:222") for _s, o in same)
    assert parts.report["ambiguous"]["P685_value_on_several_items"] == 2
    item = _by(parts.statements, "bio:wikidata_item", "gbif:2435099")[0]
    assert item.object_qid == "Q35255" and item.qualifiers["wikidata_statement_id"] == "Q35255-ABC"
    name = _by(parts.statements, "bio:common_name", "gbif:2435099")[0]
    assert name.value == {"type": "monolingualtext", "text": "Puma", "language": "fr"}


# ------------------------------------------------------------------------------------------------ HGNC
def test_hgnc_genes_chromosomes_and_encodes() -> None:
    genes = mo.parse_hgnc(_fx("atlas.hgnc", "hgnc_complete_set.txt"))
    by = {g.symbol: g for g in genes}
    assert by["TP53"].n == "11998" and by["TP53"].uniprot == ("P04637",)
    assert mo.hgnc_chromosomes("17p13.1") == (["17"], None)
    assert mo.hgnc_chromosomes("Xp22.33 and Yp11.32") == (["X", "Y"], None)
    assert mo.hgnc_chromosomes("mitochondria") == (["MT"], None)
    assert mo.hgnc_chromosomes("reserved") == ([], "not_a_chromosome_location")
    assert mo.hgnc_chromosomes("23q1")[0] == []
    parts = mo.hgnc_kg(genes, raw_sha256="a" * 64, revision="r", day="2026-10-16")
    loc = {(s.subject_qid, s.object_qid) for s in _by(parts.statements, "hgnc:located_on_chromosome")}
    assert ("hgnc:11998", "chromosome:9606:17") in loc and ("hgnc:7419", "chromosome:9606:MT") in loc
    shox = by["SHOX"].n
    assert {o for s, o in loc if s == f"hgnc:{shox}"} == {"chromosome:9606:X", "chromosome:9606:Y"}
    enc = _by(parts.statements, "hgnc:encodes", "hgnc:11998")[0]
    assert enc.object_qid == "uniprot:P04637" and enc.references[0]["selector"]["column"] == "uniprot_ids"
    assert parts.report["location_not_linked"].get("reserved", 0) >= 1


# ------------------------------------------------------------------------------------------------ UniProt
def test_uniprot_release_counts_and_human_entries() -> None:
    rel = mo.uniprot_release(_fx("atlas.uniprot", "reldate.txt"))
    assert rel.name >= "2026_03" and len(rel.date) == 10
    counts = mo.organism_counts(_fx("atlas.uniprot", "organisms.tsv"))
    assert counts == {"9606": 12, "10090": 5, "83333": 3}
    taxa = {r["Taxon Id"]: (r, "t" * 64) for r in mo.tsv_rows(_fx("atlas.uniprot", "taxonomy_0.tsv"),
                                                                ["Taxon Id"], "tax")}
    rows = mo.tsv_rows(_fx("atlas.uniprot", "human.tsv"), ["Entry", "HGNC"], "human")
    parts = mo.uniprot_kg(counts, "o" * 64, taxa, [(r, i + 2) for i, r in enumerate(rows)], "h" * 64, rel)
    ent = {e.qid: e for e in parts.entities}
    assert ent["ncbitaxon:9606"].labels["en"] == "Homo sapiens"
    assert ent["uniprot:P04637"].kind == "protein" and ent["uniprot:P04637"].labels["en"] == "Cellular tumor antigen p53"
    enc = _by(parts.statements, "uniprot:encoded_by", "uniprot:P04637")[0]
    assert enc.object_qid == "hgnc:11998"
    ln = _by(parts.statements, "uniprot:sequence_length", "uniprot:P04637")[0]
    assert ln.value == {"type": "quantity", "amount": "393", "unit": "amino acids"}
    fn = _by(parts.statements, "uniprot:function", "uniprot:P04637")[0]
    assert fn.value["language"] == "en" and len(fn.value["text"]) <= mo.FUNCTION_MAX
    assert fn.qualifiers["truncated"] == (fn.qualifiers["characters_in_source"] > mo.FUNCTION_MAX)
    cnt = _by(parts.statements, "uniprot:reviewed_protein_count", "ncbitaxon:9606")[0]
    assert cnt.value["amount"] == "12" and cnt.qualifiers["release"] == rel.name


# ------------------------------------------------------------------------------------------------ Ensembl / NCBI
def test_ensembl_assemblies_and_karyotype() -> None:
    sp = json.loads(_fx("atlas.ensembl_genomes", "info_species.json"))["species"]
    kar = {n: (json.loads(_fx("atlas.ensembl_genomes", f"info_assembly_{n}.json")), "k" * 64)
           for n in mo.KARYOTYPE_SPECIES}
    parts = mo.ensembl_kg(sp, "s" * 64, kar, release="115", known_taxa={"9606"})
    asm = _by(parts.statements, "ensembl:assembly_of")
    human = next(s for s in asm if s.object_qid == "ncbitaxon:9606")
    assert human.subject_qid == "assembly:GCA_000001405.29"
    ch = {s.object_qid: s for s in _by(parts.statements, "ensembl:has_chromosome", "assembly:GCA_000001405.29")}
    assert "chromosome:9606:17" in ch and int(ch["chromosome:9606:17"].qualifiers["length_bp"]) > 80_000_000
    assert "chromosome:9606:MT" in ch
    ents = {e.qid for e in parts.entities}
    assert "ncbitaxon:9606" not in ents and "ncbitaxon:10090" in ents  # only unknown taxa get an entity


def test_ncbi_gene_counts_and_pairing() -> None:
    reps = []
    for t, _l in mo.NCBI_TAXA:
        doc = json.loads(_fx("atlas.ncbi_genomes", f"genomes_{t}.json"))
        reps += [(r, "n" * 64, {"taxon": t, "index": i}) for i, r in enumerate(doc["reports"])]
    parts = mo.ncbi_kg(reps, known_taxa=set(), retrieved_day="2026-10-17")
    pair = _by(parts.statements, "ncbi:paired_assembly", "assembly:GCF_000001405.40")[0]
    assert pair.object_qid == "assembly:GCA_000001405.29"
    gc = {s.qualifiers["category"]: int(s.value["amount"])
          for s in _by(parts.statements, "ncbi:annotated_gene_count", "assembly:GCF_000001405.40")}
    assert gc["protein_coding"] > 19_000 and gc["total"] >= gc["protein_coding"]
    assert all(s.value["unit"] == "genes" for s in _by(parts.statements, "ncbi:annotated_gene_count"))


def test_atlas_specs() -> None:
    specs = {s.name: s for s in [*tx.atlas_source(), *mo.atlas_source()]}
    assert set(specs) == {"atlas.gbif_backbone", "atlas.col_checklist", "atlas.bio_wikidata", "atlas.hgnc",
                          "atlas.uniprot", "atlas.ensembl_genomes", "atlas.ncbi_genomes"}
    for s in specs.values():
        assert s.code_digest.startswith("sha256:") and s.planned_requests >= 1
        man = json.loads((FIX / s.name / "manifest.json").read_text())
        assert man and all(m["licence"] and "trimmed" in m["fixture_note"] for m in man)
    assert specs["atlas.bio_wikidata"].source_key == "wikidata.wdqs"
