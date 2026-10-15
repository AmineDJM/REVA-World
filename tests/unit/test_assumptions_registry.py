"""Literature-assumption registry (registry/assumptions): schema and verification discipline.

Every numeric value must be quoted from a hashed text with a locator; entries without such a text
carry no numbers (verification_status: metadata_only). Derived ratios are recomputed here.
Offline: the fetched texts are not in the repository, only their SHA-256 digests.
"""

from __future__ import annotations

import json
import re
import unicodedata
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
ASSUMPTIONS = ROOT / "registry" / "assumptions"
SCHEMA = json.loads((ASSUMPTIONS / "schema.json").read_text(encoding="utf-8"))
FILES = sorted(ASSUMPTIONS.glob("*.yaml"))
EXPECTED = {"retirement_employment_response.yaml", "io_energy_passthrough_benchmarks.yaml"}


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("|", " ")
    return re.sub(r"\s+", " ", text).strip()


def _entries() -> list[tuple[str, dict[str, Any]]]:
    return [(p.name, e) for p in FILES for e in _load(p)["entries"]]


def test_expected_files_present() -> None:
    assert EXPECTED <= {p.name for p in FILES}


def test_schema_itself_is_valid() -> None:
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_file_validates_against_schema(path: Path) -> None:
    validator = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(_load(path)), key=lambda e: list(e.absolute_path))
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors[:20])


def test_entry_ids_are_unique_across_files() -> None:
    ids = [e["id"] for _, e in _entries()]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_review_status_is_pending_never_reviewed() -> None:
    for path in FILES:
        assert _load(path)["curation"]["review_status"] == "maintainer_review_pending"


@pytest.mark.parametrize(("fname", "entry"), _entries(), ids=lambda x: x if isinstance(x, str) else x["id"])
def test_every_number_is_verified_from_a_hashed_text_with_locator(fname: str, entry: dict[str, Any]) -> None:
    sources = {s["id"]: s for s in entry["text_sources"]}
    assert len(sources) == len(entry["text_sources"]), "duplicate text_source id"
    if entry["verification_status"] == "metadata_only":
        assert entry["values"] == [] and not entry.get("derived")
        assert "not verified" in entry["not_verified"].lower()
        return
    assert entry["values"], "verified_from_text entry without values"
    names = [v["name"] for v in entry["values"]]
    assert len(names) == len(set(names)), "duplicate value name"
    for v in entry["values"]:
        assert v["verification"] == "verified_from_text"
        assert v["locator"].strip() and v["quote"].strip()
        src = sources.get(v["text_source"])
        assert src is not None, f"{v['name']}: unknown text_source {v['text_source']}"
        assert re.fullmatch(r"[0-9a-f]{64}", src["sha256"])
        # the number as printed must appear in the verbatim quote
        assert _norm(v["as_printed"]) in _norm(v["quote"]), f"{v['name']}: as_printed not in quote"
        # a secondary compilation is labelled as such on both sides
        is_secondary_source = src["kind"] == "secondary_compilation"
        assert (v["evidence"] == "secondary_compilation") == is_secondary_source, v["name"]
        if v.get("standard_error") is not None:
            assert v.get("standard_error_as_printed"), f"{v['name']}: SE without printed form"


@pytest.mark.parametrize(("fname", "entry"), _entries(), ids=lambda x: x if isinstance(x, str) else x["id"])
def test_derived_values_recompute_from_verified_values(fname: str, entry: dict[str, Any]) -> None:
    values = {v["name"]: Decimal(str(v["value"])) for v in entry["values"]}
    for d in entry.get("derived", []):
        assert d["numerator"] in values and d["denominator"] in values, d["name"]
        assert values[d["denominator"]] != 0
        ratio = Decimal(d["sign"]) * values[d["numerator"]] / values[d["denominator"]]
        quantum = Decimal(1).scaleb(-d["rounding"])
        assert ratio.quantize(quantum, rounding=ROUND_HALF_EVEN) == Decimal(str(d["value"])).quantize(quantum), (
            d["name"],
            ratio,
        )


@pytest.mark.parametrize(("fname", "entry"), _entries(), ids=lambda x: x if isinstance(x, str) else x["id"])
def test_doi_metadata_was_checked_against_crossref(fname: str, entry: dict[str, Any]) -> None:
    if entry["doi"] is None:
        return
    check = entry["metadata_check"]
    assert check is not None and check["source"] == "crossref"
    assert check["url"].lower().endswith(entry["doi"].lower())


def test_retirement_grid_values_exist_in_registry() -> None:
    """The sensitivity grid documented in docs/dev/v2_literature.md uses only registered derived values."""
    derived = {
        (e["id"], d["name"]): d["value"]
        for f, e in _entries()
        if f == "retirement_employment_response.yaml"
        for d in e.get("derived", [])
    }
    grid = {
        "low": ("ardito_2021_ita", "employment_share_of_pension_drop_men", 0.198),
        "france_2010": ("rabate_rochut_2020_fra", "employment_share_of_retirement_drop", 0.437),
        "high": ("geyer_welteke_2017_deu_wp", "employment_share_of_pension_drop_women", 0.578),
    }
    for key, (eid, name, expected) in grid.items():
        assert derived[(eid, name)] == expected, key
