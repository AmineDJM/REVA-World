"""Universal relation families (registry/kg/relations_v4.yaml + relations_v4.d/*.yaml, reva.kg.families)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reva.kg import families as F

ROADMAP_FAMILIES = {
    "is_a", "part_of", "has_part", "made_of", "located_in", "occurs_during", "produces", "produced_by", "owns",
    "owned_by", "supplies", "buys_from", "trades_with", "eats", "interacts_with", "associated_with",
    "measured_by", "discovered_by", "invented_by", "regulates", "context",
}  # fmt: skip


def test_repo_registry_is_valid() -> None:
    """Every mapped property is allow-listed, requested, or namespaced; no family causal; no duplicates."""
    assert F.validate() == []


def test_all_roadmap_families_present_and_never_causal() -> None:
    reg = F.load_registry()
    assert ROADMAP_FAMILIES <= set(reg.families)
    for fam in reg.families.values():
        assert fam.causal is False
        assert fam.label["fr"] and fam.label["en"] and fam.inverse_label["fr"] and fam.inverse_label["en"]
    doc = yaml.safe_load(F.RELATIONS_FILE.read_text(encoding="utf-8"))
    assert all(f["causal"] is False for f in doc["families"])


def test_no_causal_property_is_mapped() -> None:
    reg = F.load_registry()
    assert not (set(reg.mappings) & F.CAUSAL_PIDS)


def test_core_mappings_from_the_brief() -> None:
    reg = F.load_registry()
    expected = {
        "P279": "is_a", "P361": "part_of", "P527": "has_part", "P186": "made_of", "P131": "located_in",
        "P276": "located_in", "P176": "produced_by", "P127": "owned_by", "P749": "owned_by", "P355": "owns",
        "P61": "discovered_by", "P2283": "associated_with", "P1056": "produces", "P2293": "associated_with",
        "P780": "associated_with", "P927": "located_in", "P1582": "produced_by", "P1034": "eats",
        "P183": "located_in", "P9714": "located_in",
    }  # fmt: skip
    for pid, fam in expected.items():
        assert reg.mapping(pid) is not None, pid
        assert reg.mapping(pid).family == fam, pid  # type: ignore[union-attr]


def test_mapped_properties_exist_or_are_namespaced() -> None:
    reg = F.load_registry()
    allow = F.kg_allowlist()
    requested = F.requested_properties()
    for pid in reg.mappings:
        if ":" in pid:
            assert F.PID_RE.match(pid)
        else:
            assert pid in allow or pid in requested, pid


def test_edge_family_reading_directions() -> None:
    reg = F.load_registry()
    out = reg.edge_family("P361", "out")
    inn = reg.edge_family("P361", "in")
    assert out is not None and out.family == "part_of" and not out.inverse
    assert inn is not None and inn.family == "has_part"
    made = reg.edge_family("P186", "in")
    assert made is not None and made.family == "made_of" and made.inverse
    assert made.label["fr"] == "est un constituant de"
    assert reg.edge_family("P999999", "out") is None
    # pairs: P127 owned by read from the owner = owns
    own = reg.edge_family("P127", "in")
    assert own is not None and own.family == "owns"


def test_properties_for_families() -> None:
    reg = F.load_registry()
    pairs = reg.properties_for({"has_part"})
    assert ("P527", "out") in pairs and ("P361", "in") in pairs
    assert ("P361", "out") not in pairs
    with pytest.raises(F.FamilyError):
        reg.properties_for({"causes"})


def _write(tmp: Path, core: dict, dropins: dict[str, dict]) -> tuple[Path, Path]:
    c = tmp / "relations_v4.yaml"
    c.write_text(yaml.safe_dump(core, allow_unicode=True), encoding="utf-8")
    d = tmp / "relations_v4.d"
    d.mkdir()
    for name, doc in dropins.items():
        (d / name).write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return c, d


def _core() -> dict:
    return yaml.safe_load(F.RELATIONS_FILE.read_text(encoding="utf-8"))


def test_dropin_appends_mappings(tmp_path: Path) -> None:
    c, d = _write(tmp_path, _core(), {"v4chem.yaml": {
        "version": 1, "agent": "V4CHEM",
        "mappings": [{"property": "chebi:has_part", "family": "has_part", "source": "chebi"},
                     {"property": "chebi:is_a", "family": "is_a"}],
    }})  # fmt: skip
    assert F.validate(c, d, allowlist=F.kg_allowlist(), requested=F.requested_properties()) == []
    reg = F.load_registry(c, d)
    assert reg.mapping("chebi:has_part").agent == "V4CHEM"  # type: ignore[union-attr]
    assert reg.edge_family("chebi:has_part", "in").family == "part_of"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("mapping", "needle"),
    [
        ({"property": "mondo:has_material_basis_in", "family": "associated_with"}, "causal marker"),
        ({"property": "globi:causes", "family": "interacts_with"}, "causal marker"),
        ({"property": "P828", "family": "associated_with"}, "never a family"),
        ({"property": "P361", "family": "part_of"}, "already mapped"),
        ({"property": "xx:foo", "family": "causes"}, "unknown family"),
        ({"property": "P99999", "family": "context"}, "neither in"),
        ({"property": "xx:foo", "family": "context", "share": {"qualifier": "s", "scale": "fraction"}}, "share"),
    ],
)
def test_dropin_rejections(tmp_path: Path, mapping: dict, needle: str) -> None:
    c, d = _write(tmp_path, _core(), {"v4x.yaml": {"version": 1, "agent": "V4X", "mappings": [mapping]}})
    problems = F.validate(c, d, allowlist=F.kg_allowlist(), requested=F.requested_properties())
    assert any(needle in p for p in problems), problems


def test_dropin_file_name_must_match_agent(tmp_path: Path) -> None:
    c, d = _write(tmp_path, _core(), {"other.yaml": {"version": 1, "agent": "V4X", "mappings": []}})
    assert any("file name" in p for p in F.validate(c, d, allowlist=set(), requested=set()))


def test_causal_family_refused_by_schema(tmp_path: Path) -> None:
    core = _core()
    core["families"][0]["causal"] = True
    c, d = _write(tmp_path, core, {})
    assert any("causal" in p for p in F.validate(c, d, allowlist=set(), requested=set()))
    with pytest.raises(F.FamilyError):
        F.load_registry(c, d)


def test_share_is_missing_not_zero() -> None:
    m = F.load_registry().mapping("P186")
    assert m is not None
    assert F.share_of(m, {}) is None
    assert F.share_of(m, {"P1107": [{"amount": "+0.88", "unit": "1"}]}) == {
        "value": "0.88", "fraction": "0.88", "unit": "1", "scale": "fraction", "qualifier": "P1107",
    }  # fmt: skip
    assert F.share_of(m, {"P1107": [{"amount": "0.1"}, {"amount": "0.2"}]}) is None  # ambiguous
    assert F.share_of(m, {"P1107": [{"amount": "abc"}]}) is None
    no_share = F.load_registry().mapping("P361")
    assert no_share is not None and F.share_of(no_share, {"P1107": [{"amount": "1"}]}) is None
    pct = F.Mapping_(property="xx:y", family="made_of", share_qualifier="pct", share_scale="percent")
    assert F.share_of(pct, {"pct": "12.5"})["fraction"] == "0.125"  # type: ignore[index]


def test_cli_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert F.main(["check"]) == 0
    assert "families" in capsys.readouterr().out
    assert F.main([]) == 2
