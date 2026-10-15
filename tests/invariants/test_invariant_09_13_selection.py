"""Invariants 9 and 13 at the belief-selection boundary.

9: no loss of uncertainty/dependence metadata without an explicit approximation
   record. Selection copies the chosen observation's uncertainty verbatim
   (including ``dependence_group`` and conditioning) and the published estimate
   stores exactly the observation's uncertainty.
13: no source-family duplicates counted as independent evidence. V1 selection is
   a preference choice over lineages, never a pooled or vote-counted estimate:
   agreeing duplicate lineages (same lineage family) leave the chosen value and
   its uncertainty unchanged — agreement does not narrow or upgrade anything —
   the duplicates stay visible as alternatives, and same-source duplication is
   flagged.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from reva.belief.selection import select_report
from reva.kernel.uncertainty import UncertaintySpec

SUBJECT, CONCEPT_VERSION, SUPPORT = uuid4(), uuid4(), uuid4()
CONCEPT = "trade.hs2709.import_value.world_total"


def _row(source_key: str, family: str, logical_key: str, value: str, uncertainty: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "subject_id": SUBJECT,
        "concept_version_id": CONCEPT_VERSION,
        "support_id": SUPPORT,
        "concept_id": CONCEPT,
        "source_key": source_key,
        "lineage_family": family,
        "origin": "test_fixture",
        "logical_key": logical_key,
        "value": {"kind": "decimal", "value": value, "unit_ref": "unit.usd"},
        "uncertainty": uncertainty,
        "source_flags": {"flowCode": "M", "partnerCode": "0"},
        "selection_status": "current",
        "revision_no": 1,
    }


RICH_UNCERTAINTY = UncertaintySpec(
    status="quantified",
    kind="bounds",
    description="provider-stated bounds",
    method_version="provider-1",
    lower="90",
    upper="110",
    dependence_group="un_comtrade:2023:HS2709",
    conditioning=("reporter-declared",),
).model_dump(mode="json")


def test_selection_copies_uncertainty_and_dependence_metadata_verbatim() -> None:
    row = _row("un.comtrade", "un_comtrade", "k1", "100", copy.deepcopy(RICH_UNCERTAINTY))
    res = select_report([row], {CONCEPT: ["un.comtrade"]})
    [sel] = res.selections
    assert sel.uncertainty == RICH_UNCERTAINTY
    assert sel.uncertainty["dependence_group"] == "un_comtrade:2023:HS2709"
    assert sel.uncertainty is not row["uncertainty"]  # a copy: later mutation cannot leak back


def test_same_family_duplicates_do_not_narrow_or_upgrade_the_selected_value() -> None:
    unknown = UncertaintySpec.unknown("UN Comtrade publishes no uncertainty for this value").model_dump(mode="json")
    one = _row("un.comtrade", "un_comtrade", "k1", "100", copy.deepcopy(unknown))
    dup = _row("un.comtrade", "un_comtrade", "k1-replayed", "100.0", copy.deepcopy(unknown))
    single = select_report([one], {CONCEPT: ["un.comtrade"]}).selections[0]
    both = select_report([one, dup], {CONCEPT: ["un.comtrade"]}).selections[0]
    # agreement of two lineages from one family is not extra evidence
    assert both.value == single.value
    assert both.uncertainty == single.uncertainty == unknown
    assert both.uncertainty["status"] == "unquantified"
    # the duplicate is retained, visible and flagged, never merged into the value
    assert [a["observation_id"] for a in both.alternatives] == [str(dup["id"])]
    assert "multiple_lineages_same_source" in both.discrepancy_flags
    assert not any(f.startswith("source_disagreement") for f in both.discrepancy_flags)  # 100 == 100.0


def test_selection_output_has_no_independence_count_or_pooled_value() -> None:
    unknown = UncertaintySpec.unknown().model_dump(mode="json")
    rows = [_row("un.comtrade", "un_comtrade", f"k{i}", "100", copy.deepcopy(unknown)) for i in range(5)]
    [sel] = select_report(rows, {CONCEPT: ["un.comtrade"]}).selections
    assert sel.observation_id == min(rows, key=lambda r: r["logical_key"])["id"]  # one chosen lineage
    assert sel.value == rows[0]["value"]
    assert sel.uncertainty == unknown
    fields = set(vars(sel))
    assert not fields & {"n_sources", "independent_sources", "pooled_value", "weight"}, fields


# --------------------------------------------------------------------------- DB


@pytest.mark.db
def test_published_estimate_uncertainty_equals_its_observation(db, fixture_release) -> None:
    with db.owner.connect() as c:
        rows = (
            c.execute(
                text(
                    """SELECT e.id, e.uncertainty AS est_u, o.uncertainty AS obs_u, e.value AS est_v, o.value AS obs_v
               FROM belief.estimate e JOIN belief.input i ON i.estimate_id = e.id
               JOIN evidence.observation o ON o.id = i.observation_id
               WHERE e.release_id = :r"""
                ),
                {"r": fixture_release["release_id"]},
            )
            .mappings()
            .all()
        )
    assert rows
    lost = [r["id"] for r in rows if r["est_u"] != r["obs_u"]]
    assert not lost, f"{len(lost)} estimates changed their observation's uncertainty, e.g. {lost[:3]}"
    changed = [r["id"] for r in rows if r["est_v"] != r["obs_v"]]
    assert not changed, f"{len(changed)} estimates changed their observation's value"


@pytest.mark.db
def test_every_source_estimate_names_exactly_one_selected_observation(db, fixture_release) -> None:
    """Selection never combines several observations into one value (no pooled duplicates)."""
    with db.owner.connect() as c:
        multi = (
            c.execute(
                text(
                    """SELECT e.id, count(i.observation_id) AS n FROM belief.estimate e
               JOIN belief.input i ON i.estimate_id = e.id
               WHERE e.release_id = :r AND i.observation_id IS NOT NULL
               GROUP BY e.id HAVING count(i.observation_id) > 1"""
                ),
                {"r": fixture_release["release_id"]},
            )
            .mappings()
            .all()
        )
    assert not multi, [UUID(str(m["id"])) for m in multi[:3]]
