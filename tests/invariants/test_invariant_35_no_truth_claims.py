"""Invariant 35: no claim that tests prove causal truth or completeness of reality.

Software tests cannot establish anything about the world, so this invariant is
enforced on *wording*: the limitations document states the disclaimer, the
coverage contract is catalogue-relative, the evaluation services label their
evidence, and no user-facing text (README, docs, UI source, runtime messages)
makes an affirmative claim that tests/validation prove truth, causality or
completeness. The scan is lexical; it cannot judge meaning, which is why the
invariant is ``partially_proven`` in coverage.yaml.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

OVERCLAIM = re.compile(
    r"\b(tests?|validation|backtests?|the model|reva)\b[^.\n]{0,40}\b(prove[sn]?|guarantee[sd]?|establish(es)?)\b"
    r"[^.\n]{0,40}\b(truth|true|causal(ity)?|reality|real world|complete(ness)?|correct(ness)? of the world)\b"
    r"|\b(complete|full|100 ?%)\b[^.\n]{0,20}\b(knowledge|picture|model|coverage) of (the )?(world|reality|earth)\b",
    re.IGNORECASE,
)
NEGATION = re.compile(r"\b(not|no|never|cannot|can't|nothing|neither|nor|without|do not|does not)\b", re.IGNORECASE)


def _user_facing_files() -> list[Path]:
    files = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]
    files += sorted(p for p in (REPO / "ui" / "src").rglob("*") if p.suffix in (".ts", ".tsx")
                    and "test" not in p.parts)
    files += sorted(p for p in (REPO / "src" / "reva").rglob("*.py") if "__pycache__" not in p.parts)
    return [f for f in files if f.exists()]


def test_limitations_state_that_tests_do_not_prove_truth() -> None:
    text = (REPO / "docs" / "limitations.md").read_text()
    assert "Tests do not prove truth" in text
    assert "invariant 35" in text
    assert re.search(r"say nothing about whether the world behaves as\s+modelled", text)
    assert "Not causal" in text


def test_overclaim_detector_positive_control() -> None:
    assert OVERCLAIM.search("Our tests prove the causal effect is real")
    assert OVERCLAIM.search("REVA gives a complete picture of the world")
    assert not OVERCLAIM.search("Passing tests verify code against stated mathematics")


def test_no_user_facing_text_claims_tests_prove_truth_or_completeness() -> None:
    offenders: list[str] = []
    for path in _user_facing_files():
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if OVERCLAIM.search(line) and not NEGATION.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()[:160]}")
    assert not offenders, "\n".join(offenders)


def test_evaluation_and_coverage_statements_disclaim_reality() -> None:
    from reva.evaluation.tournament_service import HONESTY_STATEMENT

    assert "not a genuine real-time backtest" in HONESTY_STATEMENT
    mvp = (REPO / "registry" / "requirements" / "mvp.yaml").read_text()
    assert "never as a percentage of reality" in mvp
