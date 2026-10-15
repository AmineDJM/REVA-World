"""GATE2: rolling back a promoted connector restores along the supersession chain.

``rollback_promotion`` used to restore the superseded version only when it was the immediately
preceding registered version; a rolled-back version sitting in between meant nothing was restored.
Restoration now walks back past rolled-back versions to the newest earlier version retired by
supersession of that chain, still through ``evidence.restore_eligibility`` (independent approval).

Each test registers its own connector name (no template capability, so the promoted-connector
loader never sees it) with owner-written (reviewed) eligibility rows.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

ACTOR = "human:maintainer"


class Chain:
    """v1..vn of one synthetic connector name, with an owner-written promotion history."""

    def __init__(self, db: Any) -> None:
        from reva.adapters.connectors.eurostat_template import template_digest

        self.db = db
        self.name = f"test.rollback_chain.{uuid.uuid4().hex[:10]}"
        self.ids: dict[str, uuid.UUID] = {}
        with db.owner.connect() as c:
            row = c.execute(text("SELECT id, concept_id, version FROM ontology.concept_version "
                                 "ORDER BY concept_id, version LIMIT 1")).one()
            self.concept_id, self.concept = row.id, f"{row.concept_id}@{row.version}"
            self.source = c.execute(text("SELECT id FROM evidence.source ORDER BY source_key LIMIT 1")).scalar_one()
            self.spec = c.execute(text("SELECT sha256 FROM meta.artifact ORDER BY sha256 LIMIT 1")).scalar_one()
            self.subject = c.execute(text("SELECT id FROM world.entity ORDER BY id LIMIT 1")).scalar_one()
        self.digest = template_digest()
        self.app = {"subject_ids": [str(self.subject)], "years": {"in": [2023]}}

    def _activity(self, c: Any) -> uuid.UUID:
        from reva.adapters.postgres import repositories as repo

        act = repo.simple_activity("test.rollback_chain", "review", "test:rollback-chain")
        repo.record_activity(c, act)
        return act.id

    def _elig(self, c: Any, v: str, decision: str, app: dict[str, Any]) -> None:
        c.execute(text("""INSERT INTO evidence.eligibility(connector_version_id, concept_version_id, applicability,
                            decision, policy_version, review_activity_id)
                          VALUES (:cv, :c, CAST(:ap AS jsonb), :d, 'promotion-test', :a)"""),
                  {"cv": self.ids[v], "c": self.concept_id, "ap": json.dumps(app), "d": decision,
                   "a": self._activity(c)})

    def promote(self, v: str, *, digest: str | None = None) -> None:
        """Register v as eligible and retire every currently eligible version as superseded by it."""
        with self.db.owner.begin() as c:
            self.ids[v] = c.execute(text(
                """INSERT INTO evidence.connector_version(source_id, name, version, spec_sha256, code_digest,
                     declared_capabilities) VALUES (:s, :n, :v, :sp, :cd, '{"datasets": ["test"]}') RETURNING id"""),
                {"s": self.source, "n": self.name, "v": v, "sp": self.spec, "cd": digest or self.digest}).scalar_one()
            active = [o for o in self.ids if o != v and self.latest(o, c) == "eligible"]
            self._elig(c, v, "eligible", self.app)
            for o in active:
                self._elig(c, o, "retired", {**self.app, "superseded_by": {"connector_version_id": str(self.ids[v]),
                                                                          "version": v}})

    def latest(self, v: str, c: Any = None) -> str | None:
        sql = text("""SELECT decision FROM evidence.eligibility WHERE connector_version_id = :cv
                      AND concept_version_id = :c ORDER BY recorded_at DESC, id DESC LIMIT 1""")
        params = {"cv": self.ids[v], "c": self.concept_id}
        if c is not None:
            return c.execute(sql, params).scalar_one_or_none()
        with self.db.owner.connect() as conn:
            return conn.execute(sql, params).scalar_one_or_none()

    def last_applicability(self, v: str) -> dict[str, Any]:
        with self.db.owner.connect() as c:
            return dict(c.execute(text(
                """SELECT applicability FROM evidence.eligibility WHERE connector_version_id = :cv
                   AND concept_version_id = :c ORDER BY recorded_at DESC, id DESC LIMIT 1"""),
                {"cv": self.ids[v], "c": self.concept_id}).scalar_one())

    def rollback(self, v: str) -> dict[str, Any]:
        from reva.autonomy.jobs import rollback_promotion

        rollback_promotion(self.db.owner, self.name, v, self.concept, ACTOR, "rollback chain test")
        with self.db.owner.connect() as c:
            return dict(c.execute(text(
                """SELECT payload FROM ops.outbox WHERE event_type = 'autonomy.connector_rolled_back'
                   AND payload->>'connector' = :n AND payload->>'version' = :v ORDER BY id DESC LIMIT 1"""),
                {"n": self.name, "v": v}).scalar_one())

    def states(self) -> dict[str, str | None]:
        return {v: self.latest(v) for v in self.ids}


def test_rollback_restores_the_immediately_preceding_version(db: Any) -> None:
    ch = Chain(db)
    ch.promote("1.0.0")
    ch.promote("1.0.1")
    assert ch.states() == {"1.0.0": "retired", "1.0.1": "eligible"}
    assert ch.rollback("1.0.1")["restored"] == "1.0.0"
    assert ch.states() == {"1.0.0": "eligible", "1.0.1": "retired"}
    assert "superseded_by" not in ch.last_applicability("1.0.1")  # a rollback is not a supersession


def test_rollback_walks_past_an_intermediate_rolled_back_version(db: Any) -> None:
    """v1 → v2 → v3; v2 (already superseded by v3) is rolled back; rolling back v3 restores v1."""
    ch = Chain(db)
    for v in ("1.0.0", "1.0.1", "1.0.2"):
        ch.promote(v)
    assert ch.states() == {"1.0.0": "retired", "1.0.1": "retired", "1.0.2": "eligible"}
    # rolling back a version that is not the eligible one restores nothing (v3 stays the only eligible one)
    assert ch.rollback("1.0.1")["restored"] is None
    assert ch.states() == {"1.0.0": "retired", "1.0.1": "retired", "1.0.2": "eligible"}
    assert "superseded_by" not in ch.last_applicability("1.0.1")
    # v3 rolled back: v2 sits in between, rolled back; v1 (retired by supersession of v2) is restored
    assert ch.rollback("1.0.2")["restored"] == "1.0.0"
    assert ch.states() == {"1.0.0": "eligible", "1.0.1": "retired", "1.0.2": "retired"}
    with db.owner.connect() as c:  # restored through the DB-checked function (audited)
        assert c.execute(text("""SELECT count(*) FROM ops.audit WHERE action = 'eligibility.restore'
                                 AND details->>'rolled_back_connector_version_id' = :rb"""),
                         {"rb": str(ch.ids["1.0.1"])}).scalar_one() == 1


def test_rollback_restores_a_version_superseded_after_an_earlier_rollback(db: Any) -> None:
    """v1 → v2, v2 rolled back (v1 restored), v3 supersedes v1; rolling back v3 restores v1, not v2."""
    ch = Chain(db)
    ch.promote("1.0.0")
    ch.promote("1.0.1")
    assert ch.rollback("1.0.1")["restored"] == "1.0.0"
    ch.promote("1.0.2")
    assert ch.states() == {"1.0.0": "retired", "1.0.1": "retired", "1.0.2": "eligible"}
    assert ch.rollback("1.0.2")["restored"] == "1.0.0"
    assert ch.states() == {"1.0.0": "eligible", "1.0.1": "retired", "1.0.2": "retired"}


def test_rollback_never_resurrects_outside_the_chain(db: Any) -> None:
    ch = Chain(db)
    ch.promote("1.0.0")
    ch.promote("1.0.1")
    ch.promote("1.0.2", digest="sha256:" + "e" * 64)  # cannot run under the current template
    ch.promote("1.0.3")
    # v3 was retired by supersession of v4 but is template_changed: the walk stops, nothing restored
    assert ch.rollback("1.0.3")["restored"] is None
    assert ch.states() == {"1.0.0": "retired", "1.0.1": "retired", "1.0.2": "retired", "1.0.3": "retired"}
    # rolling back an already retired version is a no-op for restoration
    assert ch.rollback("1.0.3")["restored"] is None
    assert ch.states()["1.0.1"] == "retired"


def test_a_refused_restoration_keeps_the_rollback(db: Any) -> None:
    """The restored version needs an independent approval; without one the rollback still stands."""
    with db.owner.connect() as c:
        if not c.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar_one():
            pytest.skip("simulating a worker-written eligible row needs a superuser (trigger bypass)")
    ch = Chain(db)
    ch.promote("1.0.0")
    with db.owner.begin() as c:  # v1's eligibility as the worker would have written it without any approval
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("UPDATE evidence.eligibility SET recorded_by = 'reva_worker' WHERE connector_version_id = :cv"),
                  {"cv": ch.ids["1.0.0"]})
        c.execute(text("SET LOCAL session_replication_role = origin"))
    ch.promote("1.0.1")
    assert ch.rollback("1.0.1")["restored"] is None
    assert ch.states() == {"1.0.0": "retired", "1.0.1": "retired"}
