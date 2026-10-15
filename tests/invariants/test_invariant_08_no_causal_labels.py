"""Invariant 8: no observed correlation labelled causal without an admissible causal claim.

V1 has no identified causal model. The ``identified_causal_estimate`` modality and
the ``identified_causal`` relation type exist only as closed-vocabulary
definitions; no code path emits them. Belief estimates cannot carry a causal
modality (DB CHECK), a causal claim from autonomy is tier T3 (human review),
and a hypothesis card cannot carry a causal coefficient.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text

from reva.autonomy.policy import derive_tier
from reva.kernel.context import Modality

SRC = Path(__file__).resolve().parents[2] / "src" / "reva"
CAUSAL_LITERALS = {"identified_causal_estimate", "identified_causal"}
# Files allowed to *define* the closed vocabulary (enum/Literal), never to emit it.
DEFINITIONS = {"kernel/context.py", "kernel/records.py", "api/schemas.py"}


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_code_path_emits_an_identified_causal_modality() -> None:
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == Modality.identified_causal_estimate.name
                and not (rel == "kernel/context.py")
            ):
                offenders.append(f"{rel}:{node.lineno} uses Modality.{node.attr}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in CAUSAL_LITERALS
                and rel not in DEFINITIONS
            ):
                offenders.append(f"{rel}:{node.lineno} literal {node.value!r}")
    assert not offenders, "causal modality emitted outside the vocabulary definitions:\n" + "\n".join(offenders)


def test_causal_claim_capability_requires_human_review_tier() -> None:
    tier, reasons = derive_tier(("registry/evidence/new_card.yaml",), ("causal_claim",), True, False)
    assert tier == 3, (tier, reasons)


def test_hypothesis_card_cannot_carry_a_causal_coefficient() -> None:
    import yaml

    from reva.evidence.cards import CARD_DIR, validate_card
    from reva.kernel.errors import ValidationProblem

    card = yaml.safe_load(sorted(Path(CARD_DIR).glob("*.yaml"))[0].read_text())["card"]
    validate_card(card)  # positive control: the curated card itself is valid
    card = {
        **card,
        "evidence_kind": "hypothesis",
        "coefficient": "0.5",
        "source_spans": [{**card["source_spans"][0], "artifact": "table"}],
    }
    with pytest.raises(ValidationProblem, match="hypothesis cannot carry a causal coefficient"):
        validate_card(card)


@pytest.mark.db
def test_belief_estimates_cannot_be_causal(db) -> None:
    with db.owner.connect() as c:
        defs = (
            c.execute(
                text(
                    """SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conrelid = 'belief.estimate'::regclass AND contype = 'c'"""
                )
            )
            .scalars()
            .all()
        )
    modality = [d for d in defs if "modality" in d]
    assert modality, defs
    assert all("causal" not in d for d in modality), modality
