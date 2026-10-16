"""KGLANG offline tests: Glottolog / WALS / Grambank CLDF parsing, KG records, per-country counts, Zenodo record
checks and the Wikidata P1394 cross-reference (no network, no database)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.adapters.connectors import atlas_glottolog as gl
from reva.adapters.connectors import atlas_grambank as ag
from reva.adapters.connectors import atlas_wals as aw
from reva.kernel.errors import SourceResponseError

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "source"


def _fixture(conn: str, name: str) -> bytes:
    return (FIX / conn / name).read_bytes()


@pytest.fixture(scope="module")
def glotto() -> gl.GlottologData:
    return gl.parse_glottolog(_fixture("atlas.glottolog", "release.zip"))


@pytest.fixture(scope="module")
def release() -> gl.Release:
    return gl.parse_release(_fixture("atlas.glottolog", "record.json"), title_marker="Glottolog")


# ------------------------------------------------------------------------------------------------ Zenodo records
def test_release_record_is_parsed_and_checked(release: gl.Release) -> None:
    assert release.version == "v5.3" and release.doi == "10.5281/zenodo.18840967"
    assert release.concept_doi == "10.5281/zenodo.3260727" and release.licence == "CC BY 4.0"
    assert release.revision == "v5.3 doi:10.5281/zenodo.18840967" and release.day.isoformat() == "2026-03-02"
    body = _fixture("atlas.glottolog", "release.zip")
    assert release.md5 == hashlib.md5(body, usedforsecurity=False).hexdigest() and release.size == len(body)


@pytest.mark.parametrize(
    "mutate,kind",
    [
        (lambda d: d["metadata"].update(license={"id": "cc-by-nc-4.0"}), "licence_changed"),
        (lambda d: d["metadata"].pop("license"), "licence_changed"),
        (lambda d: d["metadata"].update(title="Something else"), "schema_drift"),
        (lambda d: d.update(files=[]), "schema_drift"),
        (lambda d: d["files"][0]["links"].update(self="https://evil.example/x.zip"), "schema_drift"),
    ],
)
def test_release_record_refusals(mutate: Any, kind: str) -> None:
    doc = json.loads(_fixture("atlas.glottolog", "record.json"))
    mutate(doc)
    with pytest.raises(SourceResponseError) as e:
        gl.parse_release(json.dumps(doc).encode(), title_marker="Glottolog")
    assert e.value.details["kind"] == kind


class _Raw:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.retrieved_at = datetime(2026, 10, 16, tzinfo=UTC)


class _FakeRun:
    """Serves the fixture record and a (possibly corrupted) zip; archive = sha256."""

    def __init__(self, zip_body: bytes) -> None:
        self.zip_body = zip_body
        self.archived: list[str] = []

    async def get(self, url: str, **_: Any) -> _Raw:
        return _Raw(_fixture("atlas.glottolog", "record.json") if "versions/latest" in url else self.zip_body)

    def archive(self, raw: _Raw) -> str:
        sha = hashlib.sha256(raw.body).hexdigest()
        self.archived.append(sha)
        return sha


async def test_fetch_release_checks_md5_and_archives_both() -> None:
    body = _fixture("atlas.glottolog", "release.zip")
    run = _FakeRun(body)
    f = await gl.fetch_release(run, concept_recid="3260727", title_marker="Glottolog", max_zip_bytes=10**8)
    assert f.zip_sha == hashlib.sha256(body).hexdigest() and len(run.archived) == 2
    bad = _FakeRun(body[:-10] + b"0123456789")
    with pytest.raises(SourceResponseError) as e:
        await gl.fetch_release(bad, concept_recid="3260727", title_marker="Glottolog", max_zip_bytes=10**8)
    assert e.value.details["kind"] == "checksum" and len(bad.archived) == 1  # the zip is not archived
    with pytest.raises(SourceResponseError):
        await gl.fetch_release(_FakeRun(body), concept_recid="3260727", title_marker="Glottolog", max_zip_bytes=10)


# ------------------------------------------------------------------------------------------------ Glottolog
def test_portuguese_chain_and_creole_classification(glotto: gl.GlottologData) -> None:
    names = [n for _, n in gl.chain(glotto, "port1283")]
    assert names[0] == "Portuguese" and names[-1] == "Indo-European"
    assert names.index("Romance") < names.index("Italic") < names.index("Indo-European")
    # Haitian sits where Glottolog classifies it (Gallo-Romance / Oil), REVA adds no lexifier link
    haitian = [n for _, n in gl.chain(glotto, "hait1244")]
    assert "Oil" in haitian and "Romance" in haitian
    lg = glotto.languoids["port1283"]
    assert lg.iso == "por" and lg.aes == "aes-not_endangered" and "BR" in lg.countries
    assert glotto.languoids["braz1246"].level == "dialect" and glotto.languoids["braz1246"].parent == "port1283"
    assert glotto.languoids["basq1248"].is_isolate and not glotto.languoids["basq1248"].classification
    assert glotto.report["parent_not_in_release"] == 0


def test_glottolog_kg_records(glotto: gl.GlottologData, release: gl.Release) -> None:
    sha = "a" * 64
    parts = gl.glottolog_kg(glotto, raw_sha256=sha, source_revision=release.revision, country_qids={"BRA": "Q155"})
    by = {(s.subject_qid, s.property_pid, s.statement_id): s for s in parts.statements}
    fam = by[("glottolog:port1283", gl.P_FAMILY, "family:port1283")]
    assert fam.object_qid == "glottolog:indo1319"
    par = by[("glottolog:port1283", gl.P_PARENT, "parent:port1283")]
    assert par.object_qid == f"glottolog:{glotto.languoids['port1283'].parent}"
    br = by[("glottolog:port1283", gl.P_SPOKEN_IN, "spoken_in:port1283:BR")]
    assert br.object_qid == "Q155" and br.qualifiers["iso3"] == "BRA" and br.qualifiers["country_node"] == "qid"
    pt = by[("glottolog:port1283", gl.P_SPOKEN_IN, "spoken_in:port1283:PT")]
    assert pt.object_qid == "iso3:PRT"  # no QID given -> REVA node, never guessed
    aes = by[("glottolog:kain1272", gl.P_AES, "aes:kain1272")]
    assert aes.value == {"type": "string", "value": "threatened"} and aes.qualifiers["aes_rank"] == 2
    assert "EGIDS" in aes.qualifiers["equivalences"]
    iso = by[("glottolog:port1283", gl.P_ISO, "iso639_3:port1283")]
    assert iso.value == {"type": "external_id", "value": "por"}
    coords = by[("glottolog:port1283", gl.P_COORDS, "coords:port1283")]
    assert coords.value["type"] == "globe_coordinate" and isinstance(coords.value["lat"], str)
    # every statement: stable id, selector reference, release revision, the zip as raw artifact
    for s in parts.statements:
        assert s.raw_sha256 == sha and s.source_revision == release.revision
        assert s.references[0]["selector"]["table"] in ("languages.csv", "values.csv")
        assert "v5.3" not in json.dumps(s.qualifiers) and "line" not in s.references[0]["selector"]
    ents = {e.qid: e for e in parts.entities}
    assert ents["glottolog:port1283"].kind == "language" and ents["glottolog:indo1319"].kind == "family"
    assert ents["glottolog:braz1246"].kind == "dialect"
    # Lexvo fr names given by Glottolog: several -> fr aliases, no arbitrary fr label
    sp = ents["glottolog:stan1288"]
    assert "fr" not in sp.labels and "Espagnol" in sp.aliases["fr"]
    # dialects: no macroarea/category statements; top-level families: no parent/family statement
    assert not any(s.subject_qid == "glottolog:braz1246" and s.property_pid == gl.P_MACROAREA for s in parts.statements)
    assert not any(s.subject_qid == "glottolog:indo1319" and s.property_pid == gl.P_PARENT for s in parts.statements)
    assert len({(s.statement_id) for s in parts.statements}) == len(parts.statements)


def test_kg_records_load_cleanly_through_the_validator(glotto: gl.GlottologData) -> None:
    """Every statement passes reva.kg.model's per-statement validation with the declared value kinds."""
    from reva.kg.model import _normalise_statement

    kinds = {p.pid: p.value_kind for p in gl.load_predicates()}
    parts = gl.glottolog_kg(glotto, raw_sha256="b" * 64, source_revision="v", country_qids={})
    for i, s in enumerate(parts.statements):
        _normalise_statement(i, s, gl.SOURCE_KEY, {"b" * 64}, kinds)


def test_unknown_country_code_is_reported_not_linked(glotto: gl.GlottologData) -> None:
    parts = gl.glottolog_kg(
        glotto, raw_sha256="c" * 64, source_revision="v", country_qids={}, iso3_of=lambda c: None if c == "BR" else "X"
    )
    assert parts.report["country_codes_unmapped"]["BR"] >= 5
    assert not any(s.qualifiers.get("iso2") == "BR" for s in parts.statements)


def test_atlas_counts_are_exact_counts_of_records(glotto: gl.GlottologData, release: gl.Release) -> None:
    rows = gl.atlas_rows(glotto, raw_sha256="d" * 64, release=release)
    br = {r["indicator"]: r for r in rows if r["provider_geo"] == "BR"}
    expect = sorted(g for g, lg in glotto.languoids.items() if lg.level == "language" and "BR" in lg.countries)
    assert br["languages_listed"]["value"] == str(len(expect))
    sel = json.loads(br["languages_listed"]["selector"])
    assert sel["glottocodes"] == expect and sel["column"] == "Countries"
    living = int(br["spoken_l1_living"]["value"])
    endangered = int(br["spoken_l1_endangered"]["value"])
    per = {k.split("=")[1]: int(v["value"]) for k, v in br.items() if k.startswith("spoken_l1|aes=")}
    assert living == sum(per[a] for a in ("not_endangered", "threatened", "shifting", "moribund", "nearly_extinct"))
    assert endangered == living - per["not_endangered"] and per["extinct"] >= 1  # Tuxá
    assert br["languages_listed|category=Sign_Language"]["value"] == "1"  # Urubú-Kaapor Sign Language
    r0 = br["languages_listed"]
    flags = json.loads(r0["flags"])
    assert flags["aggregation"] == "count" and flags["glottolog_version"] == "v5.3" and "doi" in flags
    assert r0["period_start"].isoformat() == "2026-03-02" and r0["unit"] == "count"
    assert r0["concept_id"] == "languages.languages_listed"
    frame = gl.atlas_frame(rows)
    assert frame.height == len(rows)
    # concepts named by the module exist in the catalog
    cat = yaml.safe_load((REPO / "registry/atlas/catalog/languages.yaml").read_text())
    ids = {c["id"] for c in cat["concepts"]}
    assert set(gl.CONCEPT_OF.values()) <= ids
    series = {s for c in cat["concepts"] for s in c["source_order"]}
    assert {f"glottolog.languages.{k}" for k in gl.CONCEPT_OF} == series


# ------------------------------------------------------------------------------------------------ WALS / Grambank
def test_wals_feature_statements() -> None:
    z = gl.CldfZip(_fixture("atlas.wals", "release.zip"), "WALS")
    subject, names, ents, rep = aw.wals_subjects(z)
    assert subject["por"] == "glottolog:port1283" and subject["alb"] == "wals:language:alb"
    assert [e.qid for e in ents] == ["wals:language:alb"] and rep["without_glottocode"] == ["alb"]
    sts, props, frep = gl.feature_statements(
        aw.SRC, z, subject_of=subject, language_names=names, raw_sha256="e" * 64, source_revision="v2020.6"
    )
    assert props["81A"].pid == "wals:f81a" and props["81A"].label_en == "Order of Subject, Object and Verb (WALS 81A)"
    jpn = next(s for s in sts if s.statement_id == "value:81A-jpn")
    assert jpn.subject_qid == "glottolog:nucl1643" and jpn.value == {"type": "string", "value": "SOV"}
    assert jpn.qualifiers["code_id"] == "81A-1" and jpn.qualifiers["language_id"] == "jpn"
    assert jpn.references == [
        {"stated_in": "WALS Online (CLDF)", "selector": {"table": "values.csv", "row_id": "81A-jpn"}}
    ]
    assert frep["counters"].get("unknown_language", 0) == 0 and all(s.object_qid is None for s in sts)


def test_grambank_skips_unknown_values_and_uses_descriptions() -> None:
    z = gl.CldfZip(_fixture("atlas.grambank", "release.zip"), "Grambank")
    subject, names, ents, _ = ag.grambank_subjects(z)
    assert subject["port1283"] == "glottolog:port1283" and not ents
    sts, props, frep = gl.feature_statements(
        ag.SRC, z, subject_of=subject, language_names=names, raw_sha256="f" * 64, source_revision="v1.0.3"
    )
    assert props["GB020"].pid == "grambank:gb020"
    vals = {s.value["value"] for s in sts}
    assert vals <= {"absent", "present", "Num-N", "N-Num", "both.", "Dem-N", "N-Dem"}
    assert all(s.qualifiers["value_as_coded"] != "?" for s in sts)
    assert frep["values_rows"] == len(sts) + frep["counters"].get("skipped_value:?", 0)
    from reva.kg.model import PID_RE

    assert all(PID_RE.match(p.pid) for p in props.values())


def test_feature_pid_and_labels_fit_the_allow_list() -> None:
    assert gl.feature_pid("wals", "144Y") == "wals:f144y" and gl.feature_pid("grambank", "GB020") == "grambank:gb020"
    long = gl.feature_properties(ag.SRC, [{"ID": "GB999", "Name": "x" * 400}])["GB999"]
    assert len(long.label_en) == 200 and long.label_en.endswith("(Grambank GB999)")
    with pytest.raises(SourceResponseError):
        gl.feature_properties(aw.SRC, [{"ID": "bad id!", "Name": "x"}])


# ------------------------------------------------------------------------------------------------ Wikidata xref
def _b(item: str, sid: str, code: str, rank: str = "NormalRank") -> dict[str, Any]:
    return {
        "item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{item}"},
        "statement": {"type": "uri", "value": f"http://www.wikidata.org/entity/statement/{sid}"},
        "code": {"type": "literal", "value": code},
        "rank": {"type": "uri", "value": f"http://wikiba.se/ontology#{rank}"},
    }


def test_wikidata_xref_statements() -> None:
    rows = [
        _b("Q5146", "Q5146-AAA", "port1283", "PreferredRank"),
        _b("Q750553", "Q750553-BBB", "port1283"),
        _b("Q1", "Q1-CCC", "not a code"),
        _b("Q2", "Q2-DDD", "abcd1234", "DeprecatedRank"),
        _b("Q5146", "Q5146-AAA", "port1283", "PreferredRank"),
    ]
    sts, rep = gl.wikidata_statements(
        rows, raw_sha256="0" * 64, retrieved_at=datetime(2026, 10, 16, tzinfo=UTC), query_sha256="1" * 64
    )
    assert [(s.subject_qid, s.object_qid, s.rank) for s in sts] == [
        ("glottolog:port1283", "Q5146", "preferred"),
        ("glottolog:port1283", "Q750553", "normal"),
    ]
    assert sts[0].statement_id == "glottolog_xref:Q5146$AAA" and sts[0].property_pid == gl.P_WIKIDATA
    assert sts[0].qualifiers["wikidata_property"] == "P1394" and sts[0].source_revision.startswith("wdqs@")
    assert rep["codes_on_several_items"] == 1
    assert rep["skipped"] == {"code_not_a_glottocode": 1, "row_incomplete_or_deprecated": 1, "duplicate_row": 1}


# ------------------------------------------------------------------------------------------------ declarations
def test_specs_predicates_and_registry_files() -> None:
    from reva.atlas.sources import discover

    found, errors = discover(
        [
            "reva.adapters.connectors.atlas_glottolog",
            "reva.adapters.connectors.atlas_wals",
            "reva.adapters.connectors.atlas_grambank",
        ]
    )
    assert not errors and set(found) == {"atlas.glottolog", "atlas.glottolog_wikidata", "atlas.wals", "atlas.grambank"}
    sources = {yaml.safe_load(p.read_text())["source_key"] for p in (REPO / "registry/sources").glob("*.yaml")}
    req = yaml.safe_load((REPO / "registry/atlas/budget_requests/kglang.yaml").read_text())
    for e in found.values():
        assert e.spec.source_key in sources and e.spec.allowed_hosts in (("zenodo.org",), ("query.wikidata.org",))
        if e.spec.allowed_hosts == ("zenodo.org",):
            assert e.spec.budget_key in {r["source_key"] for r in req["requests"]}
    assert found["atlas.glottolog_wikidata"].spec.budget_key == "wikidata.wdqs"
    preds = {p.pid: p for p in gl.load_predicates()}
    assert {gl.P_PARENT, gl.P_FAMILY, gl.P_SPOKEN_IN, gl.P_AES, gl.P_WIKIDATA} <= set(preds)
    assert preds[gl.P_SPOKEN_IN].value_kind == "item" and preds[gl.P_AES].value_kind == "string"
    for name in ("atlas.glottolog", "atlas.wals", "atlas.grambank"):
        man = json.loads((FIX / name / "manifest.json").read_text())
        assert all("trimmed" in m["fixture_note"] for m in man)
