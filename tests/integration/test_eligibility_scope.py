"""Eligibility scope semantics shared by belief selection and the belief.input trigger (R5-03, R5-11).

* One reference-year definition (world.support_ref_year) and one eligibility authority
  (evidence.observation_is_eligible) are used by selection and the trigger, so an observation
  whose support year is unknown (no range, unbounded or infinite lower bound) is excluded from
  selection instead of aborting the whole release, and never satisfies a years-scoped decision.
* The applicability schema is explicit and validated at insert: years are {"from", "to"} or
  {"in": [...]}, integers only; classifications are enforced; unknown keys are rejected.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from reva.belief.selection import eligible_latest
from reva.kernel.errors import ValidationProblem
from reva.ontology.mappings import _applicability_from_registry

# Shared DB-hardening helpers (tests are not a package): load the module by path.
_spec = importlib.util.spec_from_file_location(
    "_reva_db_hardening_helpers", Path(__file__).parents[1] / "security" / "test_db_hardening.py"
)
assert _spec is not None and _spec.loader is not None
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
activity, add_estimate, add_input = _h.activity, _h.add_estimate, _h.add_input
build_world, copy_obs, draft_release, tx = _h.build_world, _h.copy_obs, _h.draft_release, _h.tx

POP = "demography.population.wdi_midyear_total"
REGISTRY_MAPPINGS = ("registry/mappings/comtrade.yaml", "registry/mappings/worldbank.yaml")


@pytest.fixture(scope="module")
def w(db: Any) -> dict[str, Any]:
    return build_world(db)


def _support(c: Connection, valid_range: str | None) -> UUID:
    act = activity(c, "normalisation")
    return UUID(
        str(
            c.execute(
                text("""INSERT INTO world.support(temporal_extent, valid_range, spatial_spec, population_spec, aggregation,
                  resolution, provenance_activity_id, support_digest)
                VALUES ('{"kind":"interval","start":"2020-01-01"}', CAST(:vr AS tstzrange), '{}', '{}', 'total',
                        '{}', :a, :d) RETURNING id"""),
                {"vr": valid_range, "a": act, "d": f"r5-{uuid.uuid4().hex}"},
            ).scalar_one()
        )
    )


def _decision(c: Connection, w: dict[str, Any], applicability: Any, decision: str = "eligible") -> None:
    """Append a decision. 'eligible' decisions need the owner (or the registry approval functions, R5-01),
    so tests that approve run their transaction on ``db.owner``; scope semantics do not depend on the role."""
    o = w["obs"]
    c.execute(
        text("""INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability, decision,
                  policy_version, review_activity_id) VALUES (:cv, :c, CAST(:ap AS jsonb), :d, 'r5', :a)"""),
        {
            "cv": o["connector_version_id"],
            "c": o["concept_version_id"],
            "ap": json.dumps(applicability),
            "d": decision,
            "a": activity(c, "review"),
        },
    )


def _is_eligible(c: Connection, w: dict[str, Any], year: int | None, classification: str | None = None) -> bool:
    o = w["obs"]
    return bool(
        c.execute(
            text("SELECT evidence.is_eligible(:cv, :c, :s, clock_timestamp(), CAST(:y AS integer), CAST(:k AS text))"),
            {
                "cv": o["connector_version_id"],
                "c": o["concept_version_id"],
                "s": o["subject_id"],
                "y": year,
                "k": classification,
            },
        ).scalar_one()
    )


def _selected(c: Connection, oid: UUID) -> bool:
    cut = c.execute(text("SELECT clock_timestamp()")).scalar_one()
    return any(r["id"] == oid for r in eligible_latest(c, cut, ["test_fixture"], [POP]))


# =========================================================================== R5-03 reference year
@pytest.mark.db
def test_support_ref_year_is_null_when_unknown(w: dict[str, Any]) -> None:
    with tx(w["db"].worker) as c:
        cases = {
            None: None,
            "[-infinity,2000-01-01)": None,
            "(,2000-01-01)": None,
            "[2020-01-01 00:00:00+00,2021-01-01 00:00:00+00)": 2020,
            # UTC year of the lower bound, whatever the session time zone
            "[2019-12-31 23:30:00-02,2020-12-31 23:30:00-02)": 2020,
        }
        for vr, expected in cases.items():
            sp = _support(c, vr)
            assert c.execute(text("SELECT world.support_ref_year(:s)"), {"s": sp}).scalar_one() == expected, vr


@pytest.mark.db
@pytest.mark.parametrize("valid_range", [None, "[-infinity,2000-01-01)", "(,2000-01-01)"])
def test_unknown_year_is_excluded_by_selection_and_rejected_by_trigger(
    w: dict[str, Any], valid_range: str | None
) -> None:
    """Selection and belief.check_input agree: an unknown support year never satisfies the seeded
    years-scoped decision, and an infinite lower bound does not break the selection query."""
    with tx(w["db"].owner) as c:
        sp = _support(c, valid_range)
        oid = copy_obs(c, w, f"r5:open-ended:{uuid.uuid4().hex}", support=sp)
        assert not _selected(c, oid)
        assert (
            c.execute(text("SELECT evidence.observation_is_eligible(:o, clock_timestamp())"), {"o": oid}).scalar_one()
            is False
        )
        rid = draft_release(c, w)
        with pytest.raises(DBAPIError, match="is not eligible"), c.begin_nested():
            add_input(c, add_estimate(c, w, rid, support=sp), oid)
        # positive control: a decision without a years scope covers any year, including an unknown one
        _decision(c, w, {"subject_ids": [str(w["obs"]["subject_id"])]})
        assert _selected(c, oid)
        add_input(c, add_estimate(c, w, draft_release(c, w), support=sp), oid)


@pytest.mark.db
def test_unknown_year_never_fails_open(w: dict[str, Any]) -> None:
    with tx(w["db"].worker) as c:
        assert _is_eligible(c, w, 2020)
        assert not _is_eligible(c, w, None)  # was "skip the years scope" (fail open)


@pytest.mark.db
def test_open_ended_observation_does_not_block_publication(w: dict[str, Any]) -> None:
    """(R5-03) One committed open-ended observation used to make every release of its concept fail."""
    from reva.belief.publication import publish_release
    from reva.kernel.context import Origin

    db = w["db"]
    with db.worker.begin() as c:
        sp = _support(c, None)
        oid = copy_obs(c, w, f"r5:open-ended-committed:{uuid.uuid4().hex}", support=sp)
    res = publish_release(db.worker, db.store, origin=Origin.test_fixture, environment="test", concepts=[POP])
    assert res.estimates > 0
    with db.worker.connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM belief.input i JOIN belief.estimate e ON e.id = i.estimate_id "
                    "WHERE e.release_id = :r AND i.observation_id = :o"
                ),
                {"r": res.release_id, "o": oid},
            ).scalar_one()
            == 0
        )


# =========================================================================== R5-11 explicit scope schema
@pytest.mark.db
@pytest.mark.parametrize(
    "applicability",
    [
        {"years": [2019, 2023]},  # ambiguous list form (range or set?)
        {"years": [2020]},
        {"years": ["2019", "2020", "2021"]},
        {"years": {"from": 2023, "to": 2019}},
        {"years": {"from": 2020}},
        {"years": {"from": 2020, "to": 2024, "in": [2021]}},
        {"years": {"from": "2020", "to": "2024"}},
        {"years": {"from": 2020.5, "to": 2024}},
        {"years": {"in": []}},
        {"years": {"in": ["2020"]}},
        {"years": {"in": 2020}},
        {"years": "any"},
        {"classifications": "H6"},
        {"classifications": []},
        {"classifications": [6]},
        {"subject_ids": "FRA"},
        {"subject_ids": ["FRA"]},
        {"subjects_ids": []},  # misspelt scope key would otherwise be ignored
        {"region": ["EU"]},
        [],
    ],
)
def test_ambiguous_or_unknown_scopes_are_rejected_at_insert(w: dict[str, Any], applicability: Any) -> None:
    with tx(w["db"].worker) as c, pytest.raises(DBAPIError, match=r"applicab|scope schema"):
        _decision(c, w, applicability, "quarantine")


@pytest.mark.db
def test_explicit_year_forms(w: dict[str, Any]) -> None:
    with tx(w["db"].owner) as c:
        _decision(c, w, {"years": {"from": 1960, "to": 2024}}, "quarantine")  # everything quarantined ...
        _decision(c, w, {"years": {"in": [2019, 2023]}})  # ... except an explicit two-year set
        assert {y: _is_eligible(c, w, y) for y in (2019, 2021, 2023)} == {2019: True, 2021: False, 2023: True}
        _decision(c, w, {"years": {"from": 2010, "to": 2012}})
        assert [_is_eligible(c, w, y) for y in (2009, 2010, 2011, 2012, 2013)] == [False, True, True, True, False]


@pytest.mark.db
def test_classifications_are_enforced(w: dict[str, Any]) -> None:
    subj = str(w["obs"]["subject_id"])
    with tx(w["db"].owner) as c:
        # the latest decision whose scope covers a record wins: quarantine everything, then approve only
        # the reviewed classifications (as the Comtrade mapping does for HS editions H4..H6)
        _decision(c, w, {}, "quarantine")
        _decision(
            c, w, {"subject_ids": [subj], "years": {"from": 1960, "to": 2024}, "classifications": ["H4", "H5", "H6"]}
        )
        assert _is_eligible(c, w, 2020, "H6")
        assert not _is_eligible(c, w, 2020, "H3")
        assert not _is_eligible(c, w, 2020, None)  # unknown classification fails closed
        # end to end: the classification comes from the observation's source flags
        h3 = _obs_with_flags(c, w, {"classificationCode": "H3"})
        h6 = _obs_with_flags(c, w, {"classificationCode": "H6"})
        assert not _selected(c, h3) and _selected(c, h6)
        rid = draft_release(c, w)
        with pytest.raises(DBAPIError, match="is not eligible"), c.begin_nested():
            add_input(c, add_estimate(c, w, rid), h3)
        add_input(c, add_estimate(c, w, rid), h6)


@pytest.mark.db
def test_unclassified_missing_is_admitted_only_explicitly(w: dict[str, Any]) -> None:
    """A typed-Missing record without a classification (provider returned no rows) is covered by a
    classification-scoped decision only with admit_unclassified_missing; a value without one never is."""
    subj = str(w["obs"]["subject_id"])
    missing = {"kind": "missing", "reason": "unavailable"}
    for admit in (False, True):
        with tx(w["db"].owner) as c:
            _decision(c, w, {}, "quarantine")
            ap: dict[str, Any] = {"subject_ids": [subj], "classifications": ["H6"]}
            if admit:
                ap["admit_unclassified_missing"] = True
            _decision(c, w, ap)
            gone = _obs_with_flags(c, w, {"provider_returned_no_rows": True}, value=missing)
            unclassified_value = _obs_with_flags(c, w, {})
            assert _selected(c, gone) is admit
            assert not _selected(c, unclassified_value)
    with tx(w["db"].worker) as c, pytest.raises(DBAPIError, match="admit_unclassified_missing"):
        _decision(c, w, {"admit_unclassified_missing": True}, "quarantine")  # meaningless without classifications


def _obs_with_flags(
    c: Connection, w: dict[str, Any], flags: dict[str, Any], value: dict[str, Any] | None = None
) -> UUID:
    return UUID(
        str(
            c.execute(
                text("""INSERT INTO evidence.observation(source_id, connector_version_id, acquisition_id, logical_key,
                      revision_no, subject_id, concept_version_id, support_id, value, uncertainty, source_flags,
                      raw_selector, parser_version, provenance_activity_id, semantic_hash, record_hash)
                    SELECT source_id, connector_version_id, acquisition_id, :lk, 1, subject_id, concept_version_id,
                      support_id, COALESCE(CAST(:v AS jsonb), value), uncertainty,
                      (source_flags - 'classificationCode') || CAST(:f AS jsonb), raw_selector, parser_version,
                      provenance_activity_id, 'x', 'y'
                    FROM evidence.observation WHERE id = :o RETURNING id"""),
                {
                    "o": w["obs"]["id"],
                    "lk": f"r5:flags:{uuid.uuid4().hex}",
                    "f": json.dumps(flags),
                    "v": json.dumps(value) if value is not None else None,
                },
            ).scalar_one()
        )
    )


@pytest.mark.db
def test_seeded_decisions_use_the_explicit_schema(w: dict[str, Any]) -> None:
    with w["db"].owner.connect() as c:
        years = (
            c.execute(
                text(
                    "SELECT DISTINCT jsonb_typeof(applicability->'years') FROM evidence.eligibility "
                    "WHERE applicability ? 'years'"
                )
            )
            .scalars()
            .all()
        )
        comtrade = c.execute(
            text(
                """SELECT e.applicability FROM evidence.eligibility e
               JOIN evidence.connector_version cv ON cv.id = e.connector_version_id
               WHERE cv.name = 'un.comtrade.public_exact' AND e.applicability->>'mapping_key' = 'comtrade.2709.M.world'
               ORDER BY e.recorded_at DESC LIMIT 1"""
            )
        ).scalar_one()
    assert years == ["object"]
    assert comtrade["years"] == {"from": 2020, "to": 2024} and comtrade["classifications"] == ["H4", "H5", "H6"]
    assert comtrade["admit_unclassified_missing"] is True


# --------------------------------------------------------------------------- registry loader (no database)
def test_registry_mappings_use_explicit_year_forms() -> None:
    for path in REGISTRY_MAPPINGS:
        for m in yaml.safe_load(open(path, encoding="utf-8"))["mappings"]:
            years = (m.get("applicability") or {}).get("years")
            assert years is None or (isinstance(years, dict) and set(years) in ({"from", "to"}, {"in"})), m


def test_registry_loader_parses_explicit_forms_and_rejects_lists() -> None:
    rng = _applicability_from_registry({"years": {"from": 2019, "to": 2023}})
    assert rng.applies(year=2021) and not rng.applies(year=2024) and not rng.applies(year=None)
    st = _applicability_from_registry({"years": {"in": [2019, 2023]}})
    assert st.applies(year=2019) and st.applies(year=2023) and not st.applies(year=2021)
    for bad in (
        [2019, 2023],
        [2020],
        {"from": 2023, "to": 2019},
        {"in": []},
        {"in": ["2020"]},
        {"from": 2020},
        {"from": True, "to": 2020},
    ):
        with pytest.raises(ValidationProblem):
            _applicability_from_registry({"years": bad})
