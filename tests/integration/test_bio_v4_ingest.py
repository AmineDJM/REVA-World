"""V4BIO end to end on a throwaway database: the seven tree-of-life / molecular atlas sources in fixture mode through
the atlas platform (``reva.atlas.ingest.run_atlas_ingest``; no network): KG loads with provenance, idempotence, and
the links: Puma concolor -> is_a -> Puma -> Felidae -> Carnivora -> Mammalia; TP53 (HGNC:11998) -> encodes -> P04637
-> encoded_by -> TP53; TP53 -> located_in -> chromosome 17 <- has_part <- GRCh38 (GCA_000001405.29) -> NCBI pairing
<- GCF_000001405.40 (gene counts); P04637 -> in taxon -> NCBI 9606 (reviewed counts)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.kg import family_query as fq
from reva.kg import query as kq

pytestmark = pytest.mark.db
OPTIONS: dict[str, dict[str, Any]] = {
    "atlas.gbif_backbone": {"groups": ["9703"], "scope_keys": ["2436436", "5290052"]},
    "atlas.bio_wikidata": {"max_queries": 5},
}
ORDER = ("atlas.gbif_backbone", "atlas.col_checklist", "atlas.hgnc", "atlas.uniprot", "atlas.ensembl_genomes",
         "atlas.ncbi_genomes", "atlas.bio_wikidata")


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture", options=OPTIONS.get(name))
    assert out["status"] == "completed", out
    return out


def _one(c: Any, subject: str, pid: str) -> Any:
    return c.execute(
        text("""SELECT s.object_qid, s.value, s.qualifiers, s.source_key, s.source_revision, s.raw_sha256, s.refs,
                       l.raw_sha256s
                FROM kg.statement_current s JOIN kg.load l ON l.id = s.load_id
                WHERE s.subject_qid = :s AND s.property_pid = :p ORDER BY s.statement_key LIMIT 1"""),
        {"s": subject, "p": pid},
    ).one()


async def test_bio_layer_fixture_ingest_links_and_idempotence(db: Any) -> None:
    out = {n: await _run(db, n) for n in ORDER}
    for n in ORDER:
        kg = out[n]["summary"]["kg"]
        assert not kg["rejected_counts"], (n, kg["rejected_sample"])
        assert kg["statements_inserted"] > 0, n
    g = out["atlas.gbif_backbone"]["summary"]
    assert g["scope"]["loaded"] == 2 and not g["scope"]["refused"]
    assert g["counts"]["missing_count"] == 0 and not g["counts"]["conflicts"]
    with db.owner.connect() as c:
        # tree of life: species -> genus -> family -> order -> class (is_a family)
        chain = ["gbif:2435099", "gbif:2435098", "gbif:9703", "gbif:732", "gbif:359"]
        for a, b in zip(chain, chain[1:], strict=False):
            row = _one(c, a, "gbif:parent")
            assert row.object_qid == b and row.source_key == "gbif.backbone"
            assert row.raw_sha256 in row.raw_sha256s and row.source_revision.startswith("backbone 2023-08-28")
        cnt = _one(c, "gbif:359", "gbif:accepted_species_count")
        assert int(cnt.value["amount"]) > 20_000 and cnt.qualifiers["basis"] == "facet_value"
        assert _one(c, "gbif:2436436", "gbif:parent").object_qid == "gbif:2436435"  # Homo sapiens -> Homo
        res = fq.family_paths(c, "gbif:2435099", "gbif:359", families=["is_a"], max_hops=5)
        assert res["paths"] and len(res["paths"][0]["hops"]) == 4, res
        # genes and proteins
        assert _one(c, "hgnc:11998", "hgnc:encodes").object_qid == "uniprot:P04637"
        assert _one(c, "uniprot:P04637", "uniprot:encoded_by").object_qid == "hgnc:11998"
        loc = _one(c, "hgnc:11998", "hgnc:located_on_chromosome")
        assert loc.object_qid == "chromosome:9606:17" and loc.qualifiers["location_as_stated"] == "17p13.1"
        has = c.execute(
            text("""SELECT subject_qid, qualifiers FROM kg.statement_current
                    WHERE property_pid = 'ensembl:has_chromosome' AND object_qid = 'chromosome:9606:17'""")
        ).one()
        assert has.subject_qid == "assembly:GCA_000001405.29" and int(has.qualifiers["length_bp"]) > 80_000_000
        assert _one(c, "assembly:GCF_000001405.40", "ncbi:paired_assembly").object_qid == "assembly:GCA_000001405.29"
        assert _one(c, "assembly:GCA_000001405.29", "ensembl:assembly_of").object_qid == "ncbitaxon:9606"
        assert _one(c, "ncbitaxon:9606", "uniprot:reviewed_protein_count").value["amount"] == "12"
        kinds = dict(
            c.execute(
                text("""SELECT qid, kind FROM kg.entity_current WHERE qid = ANY(:q)"""),
                {"q": ["hgnc:11998", "uniprot:P04637", "chromosome:9606:17", "assembly:GCA_000001405.29",
                       "gbif:359", "ncbitaxon:9606"]},
            ).all()
        )
        assert kinds == {"hgnc:11998": "gene", "uniprot:P04637": "protein", "chromosome:9606:17": "chromosome",
                         "assembly:GCA_000001405.29": "genome_assembly", "gbif:359": "taxon",
                         "ncbitaxon:9606": "taxon"}
        path = kq.paths(c, "hgnc:11998", "assembly:GCA_000001405.29", max_hops=2)
        assert [h["property"]["pid"] for h in path["paths"][0]["hops"]] == [
            "hgnc:located_on_chromosome", "ensembl:has_chromosome"]
        # COL: counts and the release metrics
        assert c.execute(text("SELECT count(*) FROM kg.statement_current WHERE property_pid = 'col:species_count'")
                         ).scalar_one() >= 9
        # Wikidata bridges are community-edited statements under wikidata.wdqs
        wd = c.execute(
            text("""SELECT count(*) FROM kg.statement_current
                    WHERE property_pid = 'bio:wikidata_item' AND source_key = 'wikidata.wdqs'""")
        ).scalar_one()
        assert wd >= 1
    again = await _run(db, "atlas.hgnc")
    assert again["summary"]["kg"]["statements_inserted"] == 0
