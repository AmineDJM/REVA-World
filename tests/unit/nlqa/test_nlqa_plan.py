"""Typed query plan: schema, allow-list validation (reject, never substitute), server restatement."""

from __future__ import annotations

from typing import Any

import nlqa_fakes as F
import pytest

from reva.nl.budget import load_llm_budget
from reva.nl.providers import OpenAIDrafter
from reva.nlqa.plan import (
    MAX_OPERATIONS,
    MAX_PATH_OPERATIONS,
    OPS,
    in_question,
    lookup_from_question,
    restate,
    validate_plan,
)
from reva.nlqa.prompt import output_schema, prompt_digest, system_prompt, user_envelope
from reva.nlqa.service import plan_request

CAT = F.catalog()
Q = "Quel est le salaire minimum au Sénégal ?"


def op(id_: str, name: str, **kw: Any) -> dict[str, Any]:
    return {"id": id_, "op": name, **kw}


def plan(*ops: dict[str, Any], unsupported: list | None = None) -> dict[str, Any]:
    return {"input_language": "fr", "restatement": "r", "operations": list(ops), "unsupported": unsupported or []}


def _strict(schema: Any) -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
        for v in schema.values():
            _strict(v)
    elif isinstance(schema, list):
        for v in schema:
            _strict(v)


def test_output_schema_is_strict_mode_compatible_and_closed() -> None:
    s = output_schema()
    _strict(s)
    item = s["properties"]["operations"]["items"]
    assert item["properties"]["op"]["enum"] == list(OPS)
    # identifiers stay free strings (checked server-side, never forced onto an allowed id)
    assert item["properties"]["concept"] == {"type": ["string", "null"]}
    assert "qid" not in item["properties"]  # the model never supplies entity ids


def test_request_payload_is_temperature_zero_strict_and_tool_free() -> None:
    req = plan_request(Q, CAT, load_llm_budget())
    p = OpenAIDrafter("gpt-4.1").payload(req)
    assert p["temperature"] == 0 and p["n"] == 1 and p["store"] is False
    assert p["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in p and "functions" not in p
    # the question is data in the user envelope, never in the system prompt
    assert Q not in req.system_prompt and req.user_content == user_envelope(Q)
    assert "labour.minimum_wage_monthly" in system_prompt(CAT, Q)
    assert prompt_digest(CAT, Q) == prompt_digest(CAT, Q)


def test_valid_plan_is_kept() -> None:
    p = validate_plan(
        plan(
            op("s1", "entity_lookup", text="Christophe Colomb", kind="person"),
            op("s2", "entity_lookup", text="portugais", kind="language"),
            op("s3", "kg_path", from_entity="s1", to_entity="s2", max_hops=4),
            op("s4", "atlas_value", concept="labour.minimum_wage_monthly", country="SEN", year=None),
            op("s5", "atlas_rank", concept="population.density", year=2022, order="desc", limit=5),
        ),
        CAT,
        "Comment Christophe Colomb est-il lié au portugais ? Et le salaire minimum au Sénégal ?",
    )
    assert [o.op for o in p.operations] == ["entity_lookup", "entity_lookup", "kg_path", "atlas_value", "atlas_rank"]
    assert p.rejected == [] and p.planner == "llm"


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (op("s1", "atlas_value", concept="economy.secret_gdp", country="FRA"), "concept_not_in_catalog"),
        (op("s1", "atlas_value", concept="population.density", country="XXX"), "country_not_in_list"),
        (op("s1", "atlas_value", concept="population.density", country="France"), "country_not_in_list"),
        (op("s1", "kg_neighbours", entity="Q155", properties=[]), "unknown_entity_ref"),
        (op("s1", "sql", text="DROP TABLE kg.statement"), "unknown_operation"),
        (op("s1", "atlas_rank", concept="population.density", year=99999), "invalid_argument"),
        (op("s1", "links", country="BRA", link_kinds=["telepathy"]), "link_kind_not_in_list"),
    ],
)
def test_out_of_list_ids_reject_the_operation(bad: dict[str, Any], reason: str) -> None:
    p = validate_plan(plan(bad), CAT, Q)
    assert p.operations == []
    assert p.rejected[0]["reason"] == reason
    assert any(u["source"] == "server" for u in p.unsupported)


def test_unknown_property_is_rejected_not_dropped() -> None:
    p = validate_plan(
        plan(
            op("s1", "entity_lookup", text="Brésil"),
            op("s2", "kg_neighbours", entity="s1", properties=["P37", "P9999999"]),
        ),
        CAT,
        "Quelles langues parlent les Brésiliens ?",
    )
    assert [o.op for o in p.operations] == ["entity_lookup"]
    assert p.rejected[0]["reason"] == "property_not_in_list"


def test_dependent_operation_of_a_rejected_lookup_is_rejected() -> None:
    p = validate_plan(
        plan(op("s1", "entity_lookup", text=""), op("s2", "kg_timeline", entity="s1", from_year=1950, to_year=1969)),
        CAT,
        Q,
    )
    assert p.operations == [] and {r["id"] for r in p.rejected} == {"s1", "s2"}


def test_server_limits_are_capped_with_a_warning_and_stated_years_never_changed() -> None:
    p = validate_plan(
        plan(
            op("a", "entity_lookup", text="A"),
            op("b", "entity_lookup", text="B"),
            op("c", "kg_path", from_entity="a", to_entity="b", max_hops=9),
            op("d", "atlas_rank", concept="population.density", year=1961, limit=500),
        ),
        CAT,
        "Lien entre A et B ? Densité en 1961",
    )
    assert p.op("c").args["max_hops"] == 4 and p.op("d").args["limit"] == 50
    assert p.op("d").args["year"] == 1961
    assert any(w.startswith("max_hops_capped") for w in p.warnings)


def test_too_many_operations_are_truncated_and_reported() -> None:
    ops = [op(f"s{i}", "entity_lookup", text=f"x{i}") for i in range(MAX_OPERATIONS + 3)]
    p = validate_plan(plan(*ops), CAT, " ".join(f"x{i}" for i in range(MAX_OPERATIONS + 3)))
    assert len(p.operations) == MAX_OPERATIONS and len(p.rejected) == 3


def test_lookup_text_must_come_from_the_question() -> None:
    """ASK-6 (review V3): a lookup the planner adds on its own (an injected question can make it look up
    entities the user never named) is rejected with the operations using it; dictionary forms pass."""
    q = "Comment les Brésiliens parlent-ils portugais ?"
    assert lookup_from_question("Brésil", q) and lookup_from_question("portugais", q)
    assert lookup_from_question("Portuguese language", "the Portuguese language of Brazilians")
    assert not lookup_from_question("Adolf Hitler", q) and not lookup_from_question("a", "la"), "word, not substring"
    p = validate_plan(
        plan(
            op("s1", "entity_lookup", text="Brésil"),
            op("s2", "entity_lookup", text="Union soviétique"),
            op("s3", "kg_path", from_entity="s1", to_entity="s2"),
        ),
        CAT,
        q,
    )
    assert [o.id for o in p.operations] == ["s1"]
    assert {r["id"]: r["reason"] for r in p.rejected} == {
        "s2": "lookup_not_in_question",
        "s3": "unknown_entity_ref",
    }


def test_path_searches_per_question_are_capped() -> None:
    """SEC-1 (review V3): path searches are the expensive operation; at most MAX_PATH_OPERATIONS per question."""
    q = "aaa bbb ccc ddd eee"
    looks = [op(f"l{i}", "entity_lookup", text=w) for i, w in enumerate(q.split())]
    paths = [op(f"p{i}", "kg_path", from_entity="l0", to_entity=f"l{i}") for i in range(1, 5)]
    p = validate_plan(plan(*looks, *paths), CAT, q)
    assert sum(o.op == "kg_path" for o in p.operations) == MAX_PATH_OPERATIONS
    assert [r["reason"] for r in p.rejected] == ["too_many_path_operations"] * (4 - MAX_PATH_OPERATIONS)


def test_not_an_object_is_refused() -> None:
    with pytest.raises(ValueError):
        validate_plan(["not", "a", "plan"], CAT, Q)


def test_server_restatement_uses_catalog_labels() -> None:
    p = validate_plan(plan(op("s1", "atlas_value", concept="labour.minimum_wage_monthly", country="SEN")), CAT, Q)
    r = restate(p, CAT)
    assert r.startswith("Question interprétée comme") and "Salaire minimum mensuel" in r and "Senegal" in r


def test_in_question_is_accent_and_case_insensitive() -> None:
    assert in_question("senegal", Q) and in_question("SALAIRE MINIMUM", Q)
    assert not in_question("999 milliards", Q) and not in_question("", Q)


def test_registry_catalog_prompt_stays_bounded() -> None:
    """The real allow-lists (ATLASCORE catalog, crosswalk, KG properties) build, and the prompt shows at most
    MAX_PROMPT_CONCEPTS concepts so a request stays under the LLM client's body cap."""
    from reva.nl.http import LLM_MAX_REQUEST_BYTES
    from reva.nlqa.catalog import MAX_PROMPT_CONCEPTS, build_catalog

    cat = build_catalog()
    assert len(cat.countries) >= 249 and "P37" in cat.properties
    req = plan_request("Quel est le salaire minimum au Sénégal ?", cat, load_llm_budget())
    view = cat.prompt_view("Quel est le salaire minimum au Sénégal ?")
    assert view["concepts_shown"] <= MAX_PROMPT_CONCEPTS
    assert len(req.system_prompt.encode()) + len(req.user_content.encode()) < LLM_MAX_REQUEST_BYTES // 2
    wage = [c for c in cat.concepts if "minimum_wage" in c]
    if wage:  # the labour catalog is loaded: its concepts rank first for this question
        assert {c["id"] for c in view["concepts"][: len(wage)]} == set(wage)
