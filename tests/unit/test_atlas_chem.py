"""V4CHEM chemistry connectors: offline parser tests on captured fixtures (no database, no network)."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from reva.adapters.connectors import atlas_chem_chebi as chebi
from reva.adapters.connectors import atlas_chem_codata as codata
from reva.adapters.connectors import atlas_chem_elements as elm
from reva.adapters.connectors import atlas_chem_pubchem as pc
from reva.adapters.connectors.atlas_chem_common import (
    PERIODIC_TABLE_URL,
    atlas_decimal,
    atlas_frame,
    kg_decimal,
    load_predicates,
    mass_shares,
    parse_formula,
    parse_periodic_table,
    quantity,
)
from reva.atlas.tables import validate_atlas_frame
from reva.kernel.errors import SourceResponseError

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "source"
NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _fixture(connector: str, url: str) -> bytes:
    man = json.loads((FIX / connector / "manifest.json").read_text())
    e = next(x for x in man if x["url"] == url)
    return (FIX / connector / e["file"]).read_bytes()


@pytest.fixture(scope="module")
def elements():
    return parse_periodic_table(_fixture("atlas.chem_elements", PERIODIC_TABLE_URL))


@pytest.fixture(scope="module")
def pages(elements):
    by = {e.z: e for e in elements}
    return [
        elm.parse_element_page(_fixture("atlas.chem_elements", elm.element_url(z)), by[z], raw_sha256="a" * 64)
        for z in elm.FIXTURE_ELEMENTS
    ]


SYMBOLS = {"H", "C", "O", "N", "Ca", "Cu", "S", "Na", "Cl", "Fe", "Rn", "Nb", "Xe", "P", "K"}


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("CH4", {"C": 1, "H": 4}),
        ("C9H8O4", {"C": 9, "H": 8, "O": 4}),
        ("CO3.Ca", {"C": 1, "O": 3, "Ca": 1}),
        ("CuO4S.5H2O", {"Cu": 1, "O": 9, "S": 1, "H": 10}),
        ("Ca(OH)2", {"Ca": 1, "O": 2, "H": 2}),
        ("C2H3O2-", {"C": 2, "H": 3, "O": 2}),
        ("O4S-2", {"O": 4, "S": 1}),
        ("Rn", {"Rn": 1}),
        ("2C7H8N4O2", {"C": 14, "H": 16, "N": 8, "O": 4}),
    ],
)
def test_parse_formula(formula, expected):
    assert parse_formula(formula, SYMBOLS) == (expected, None)


@pytest.mark.parametrize("formula", ["(C2H4)n", "[14C]", "C6H5R", "*C", "CH3X", "C2H5Zz", "Ca(OH"])
def test_parse_formula_refuses_non_plain(formula):
    counts, why = parse_formula(formula, SYMBOLS)
    assert counts is None and why


def test_mass_shares_and_decimals():
    sh = mass_shares({"C": 1, "H": 4}, {"C": Decimal("12.011"), "H": Decimal("1.0080")})
    assert sh == {"C": Decimal("0.748675"), "H": Decimal("0.251325")}
    assert mass_shares({"C": 1, "U": 1}, {"C": Decimal(12)}) is None  # a missing mass is never guessed
    assert kg_decimal(Decimal("6.02214076E+23")) == "602214076000000000000000"
    assert kg_decimal(Decimal("1.50E-3")) == "0.0015"
    assert atlas_decimal(Decimal("6.6446573450E-27")) == "6.644657345e-27"
    assert atlas_decimal(Decimal("56300")) == "56300"
    # beyond the KG's 40+40 plain-decimal digits: kept exactly in scientific notation, never rounded into range
    assert quantity(Decimal("6.62607015e-34"), "J Hz^-1") == {
        "type": "quantity_scientific",
        "amount_scientific": "6.62607015e-34",
        "unit": "J Hz^-1",
    }


def test_periodic_table(elements):
    assert len(elements) == 118
    fe = next(e for e in elements if e.z == 26)
    assert (fe.symbol, fe.name, fe.cells["YearDiscovered"]) == ("Fe", "Iron", "Ancient")
    assert next(e for e in elements if e.z == 8).cells["YearDiscovered"] == "1774"
    with pytest.raises(SourceResponseError):
        parse_periodic_table(b'{"Table": {"Columns": {"Column": ["AtomicNumber"]}, "Row": []}}')


def test_element_pages_values(pages):
    fe = next(p for p in pages if p.z == 26)
    crust = elm.parse_abundance(fe.crustal[0])
    assert crust == (Decimal("5.63e4"), "mg/kg") and fe.crustal[0].ref.key == "jlab"
    assert elm.parse_abundance(fe.oceanic[0]) == (Decimal("2e-3"), "mg/L")
    assert fe.nuclides["56"].a == 56 and fe.nuclides["56"].symbol_text == "56Fe"
    assert {r.key for r, _, _ in fe.nuclides["56"].compositions} == {"ciaaw", "nist_pml"}
    h = next(p for p in pages if p.z == 1)
    assert "2" in h.nuclides and "3" in h.nuclides  # NIST lists 2D / 3T: IUPAC symbols of 2H / 3H
    assert elm.nuclide_id("47Fem", 26, "Fe") == (47, "m")
    assert elm.nuclide_id("54Co", 26, "Fe") is None  # another element: never guessed


def test_value_parsers():
    assert elm.parse_uncertain("55.845(2)") == (Decimal("55.845"), "(2)", False)
    assert elm.parse_uncertain("53.939 608(3)") == (Decimal("53.939608"), "(3)", False)
    assert elm.parse_uncertain("45.015467 ± 0.000304 [Estimated]") == (Decimal("45.015467"), "0.000304", True)
    assert elm.parse_uncertain("[1.007 84, 1.008 11]") is None
    assert elm.parse_interval("[1.00784,1.00811]") == (Decimal("1.00784"), Decimal("1.00811"))
    assert elm.parse_half_life("12.32 y ± 0.02") == ("y", Decimal("12.32"), "0.02")
    assert elm.parse_half_life("2.5 ms ± 0.2") == ("s", Decimal("0.0025"), "0.2")
    assert elm.parse_half_life("4.463 Gy ± 0.003")[1] == Decimal("4.463E9")
    assert elm.parse_half_life("Stable") is None and elm.parse_half_life("Not-specified") is None
    assert elm.decay_modes("2p=70±0.4%; β+=30±0.4%") == ["2p=70±0.4%", "β+=30±0.4%"]


def test_element_atlas_and_kg(elements, pages):
    er = elm.element_rows(elements, pages, pt_sha="b" * 64, retrieved_at=NOW)
    nr = elm.nuclide_rows(pages, retrieved_at=NOW)
    for ds, rows in (("elements", er), ("nuclides", nr)):
        frame, unmapped = validate_atlas_frame(
            atlas_frame(rows), source="pubchem", dataset=ds, raw_sha256s=["a" * 64, "b" * 64]
        )
        assert frame.height == len(rows) and all(
            u["provider_geo"] in ("not_geographic", "earth_crust", "earth_oceans") for u in unmapped
        )
    by = {r["indicator"]: r for r in er + nr}
    assert by["crustal_abundance|Z=26|provider=jlab"]["value"] == "56300"
    assert by["year_discovered|Z=26"]["value"] is None and by["year_discovered|Z=26"]["value_text"] == "Ancient"
    assert by["half_life|nuclide=92-238|unit=y"]["value"] == "4463000000"
    assert json.loads(by["half_life|nuclide=92-238|unit=y"]["flags"])["computed_by"] == "reva"
    assert by["isotopic_abundance|nuclide=1-1|provider=ciaaw"]["value"] is None  # an interval: no invented point
    ents, stmts = elm.kg_build(elements, pages, pt_sha="b" * 64, retrieved_at=NOW)
    assert {e.kind for e in ents} == {"chemical_element", "isotope"}
    assert len({s.statement_id for s in stmts}) == len(stmts)
    pids = {p.pid for p in load_predicates(("pubchem:",))}
    assert {s.property_pid for s in stmts} <= pids
    iso = [s for s in stmts if s.subject_qid == "nuclide:92-235" and s.property_pid == "pubchem:isotope_of"]
    assert iso and iso[0].object_qid == "element:92"
    hist = [s for s in stmts if s.subject_qid == "element:8" and s.property_pid == "pubchem:history_text"]
    assert any("Priestley" in s.value["text"] for s in hist)  # verbatim text, no extraction
    assert all(s.qualifiers.get("provided_by") for s in hist)


def test_codata():
    adj, consts = codata.parse_listing(_fixture("atlas.chem_codata", codata.URL))
    assert adj == "2022 CODATA adjustment" and len(consts) == 355
    na = next(c for c in consts if c.name == "Avogadro constant")
    assert na.value == Decimal("6.02214076e23") and na.exact and na.unit == "mol^-1"
    kb = next(c for c in consts if c.name == "Boltzmann constant in eV/K")
    assert kb.truncated and kb.exact
    g = next(c for c in consts if c.name == "Newtonian constant of gravitation")
    assert g.uncertainty == Decimal("0.00015e-11") and not g.exact
    rows = codata.rows(consts, adj, raw_sha256="c" * 64, retrieved_at=NOW)
    frame, _ = validate_atlas_frame(atlas_frame(rows), source="codata", dataset="constants", raw_sha256s=["c" * 64])
    assert frame.height == 355
    _ents, stmts = codata.kg_build(consts, adj, 2022, raw_sha256="c" * 64, retrieved_at=NOW)
    s = next(x for x in stmts if x.subject_qid == "codata:avogadro_constant")
    assert s.value["amount"] == "602214076000000000000000" and s.qualifiers["exact"] is True


def test_chebi_subset_and_composition():
    onto = chebi.parse_obo(_fixture("atlas.chem_chebi", chebi.OBO_URL), min_terms=1)
    assert onto.release == "256" and onto.licence == "https://creativecommons.org/licenses/by/4.0/"
    seeds = chebi.Seeds([{"chebi": 16183, "name": "methane"}, {"chebi": 50818, "name": "not hematite"}], [], [])
    sub, rep = chebi.select_subset(onto, seeds)
    assert rep["name_mismatch"] == [{"chebi": "CHEBI:50818", "seeds_file": "not hematite", "chebi_name": "hematite"}]
    assert {"CHEBI:16183", "CHEBI:18310", "CHEBI:24632", "CHEBI:76413"} <= sub  # alkane, hydrocarbon, role target
    chem = chebi.parse_chemical_data(_fixture("atlas.chem_chebi", chebi.CHEMDATA_URL), {chebi._num(c) for c in sub})
    els = parse_periodic_table(_fixture("atlas.chem_chebi", PERIODIC_TABLE_URL))
    comp = chebi.compositions(sub, chem, els)
    assert comp.counts["CHEBI:16183"] == {"C": 1, "H": 4}
    assert comp.counts["CHEBI:50818"] == {"Fe": 2, "O": 3}
    assert chebi.chebi_kind(onto, "CHEBI:76413", chem) == "chemical_role"
    assert chebi.chebi_kind(onto, "CHEBI:50818", chem) == "mineral"
    assert chebi.chebi_kind(onto, "CHEBI:16183", chem) == "compound"
    assert chebi.chebi_kind(onto, "CHEBI:18310", chem) == "chemical_class"
    shas = {"obo": "a" * 64, "chemdata": "b" * 64, "periodic_table": "c" * 64}
    _ents, stmts = chebi.kg_build(onto, sub, chem, comp, els, shas=shas, retrieved_at=NOW)
    made = [s for s in stmts if s.subject_qid == "chebi:16183" and s.property_pid == "chebi:formula_element"]
    assert {(s.object_qid, s.qualifiers["atom_count"]) for s in made} == {("element:6", 1), ("element:1", 4)}
    assert all(s.qualifiers["computed_by"] == "reva" for s in made)
    hyd = next(s for s in stmts if s.subject_qid == "chebi:24632" and s.property_pid == "chebi:definition")
    assert hyd.value["text"] == "A compound consisting of carbon and hydrogen only."
    rows = chebi.atlas_rows(onto, sub, chem, comp, shas=shas, retrieved_at=NOW)
    frame, _ = validate_atlas_frame(
        atlas_frame(rows), source="chebi", dataset="composition", raw_sha256s=list(shas.values())
    )
    assert frame.height == len(rows) > 0


def test_chebi_obo_guards():
    with pytest.raises(SourceResponseError):
        chebi.parse_obo(gzip.compress(b"format-version: 1.2\n\n[Term]\nid: CHEBI:1\nname: x\n"), min_terms=1)
    with pytest.raises(SourceResponseError):
        chebi.parse_obo(_fixture("atlas.chem_chebi", chebi.OBO_URL))  # live threshold: an excerpt is refused


def test_pubchem_links_and_properties():
    xr = json.loads(_fixture("atlas.chem_pubchem", pc.xrefs_url([15377, 16183])))
    cids = json.loads(_fixture("atlas.chem_pubchem", pc.cids_url([15377, 16183, 16526, 50819])))
    sid_chebi = {
        int(i["SID"]): (int(i["RegistryID"][0][6:]), "x" * 64, k)
        for k, i in enumerate(xr["InformationList"]["Information"])
    }
    sid_cids = {
        int(i["SID"]): (i.get("CID", []), "y" * 64, k) for k, i in enumerate(cids["InformationList"]["Information"])
    }
    links = pc.chebi_cid_links(sid_chebi, sid_cids)
    assert {(lk.chebi, lk.cid) for lk in links} == {(16183, 297), (15377, 962)}
    props = pc.parse_properties(_fixture("atlas.chem_pubchem", pc.properties_url([280, 297, 962, 518696])))
    assert {c.cid for c in props} == {280, 297, 962, 518696}
    els = parse_periodic_table(_fixture("atlas.chem_pubchem", PERIODIC_TABLE_URL))
    _ents, stmts = pc.kg_build(
        props, links, els, prop_shas={c.cid: "z" * 64 for c in props}, pt_sha="p" * 64, retrieved_at=NOW
    )
    co2 = {
        s.object_qid: s.qualifiers
        for s in stmts
        if s.subject_qid == "pubchem:cid:280" and s.property_pid == "pubchem:formula_element"
    }
    assert co2["element:6"]["atom_count"] == 1 and co2["element:8"]["atom_count"] == 2
    assert Decimal(co2["element:6"]["mass_share"]) + Decimal(co2["element:8"]["mass_share"]) == Decimal(1)
    dep = [s for s in stmts if s.property_pid == "pubchem:chebi_deposit"]
    assert {(s.subject_qid, s.object_qid) for s in dep} == {
        ("pubchem:cid:297", "chebi:16183"),
        ("pubchem:cid:962", "chebi:15377"),
    }
