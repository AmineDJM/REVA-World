"""Cutoff-scoped repository: the only data access a replay or training fold gets.

``CutoffRepository(conn, cutoff, mode)`` exposes *only* methods that filter by
time **before** any revision selection. There is deliberately no method for an
unrestricted "current" read; the connection is private.

Modes
-----
``as_known`` (operational)
    Observations recorded in REVA at/before the cutoff (``recorded_at``), with
    eligibility decisions and retractions as recorded at the cutoff. When source
    availability is unknown this is the honest operational clock (first REVA
    retrieval/commit).
``available_then`` (reconstruction)
    Observations with independently evidenced availability at/before the
    cutoff (``source_available_at`` **and** ``availability_basis``), whenever
    REVA recorded them. Records without proof are excluded and counted.
    Data-quality knowledge is applied *as of the cutoff* (R3-10): a retraction
    excludes a record only if it was recorded at/before the cutoff, and the
    eligibility decision in force at the cutoff governs. When REVA had made no
    eligibility decision for a (connector version, concept) by the cutoff, the
    *first* decision REVA ever recorded for it (the algorithm-design decision)
    is used and its date is reported (``eligibility_evaluated_at``). Later
    retractions and later eligibility changes (quarantine/retirement) are
    never used to exclude records; they are reported as row flags
    (``retracted_after_cutoff``, ``ineligible_after_cutoff``) and counted in
    :meth:`ObservationScope.flag_report`. Selection method and ontology are the
    current algorithm design, reported separately from availability.
``revised_data_retrospective`` (explicit downgrade)
    The data snapshot is identical to ``as_known`` at the cutoff (latest
    revised values recorded by then); the mode exists so that consumers that
    then split it temporally carry the retrospective label everywhere.

Revision selection always happens inside the already time-filtered set, so a
later correction can never change an earlier as-of answer.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.evaluation.leakage import (
    GuardMode,
    InputRef,
    LeakageGuard,
    RefKind,
)
from reva.kernel.errors import CapabilityGap, LeakageBlocked, ValidationProblem


class CutoffMode(StrEnum):
    as_known = "as_known"
    available_then = "available_then"
    revised_data_retrospective = "revised_data_retrospective"


_GUARD_FOR = {
    CutoffMode.as_known: GuardMode.strict,
    CutoffMode.available_then: GuardMode.available_then,
    # The snapshot itself is as-known at the cutoff; the downgrade happens when
    # a consumer splits it temporally at earlier origins.
    CutoffMode.revised_data_retrospective: GuardMode.strict,
}


@dataclass
class ObservationScope:
    """Selected (one revision per lineage) observations plus exclusion accounting."""

    mode: CutoffMode
    cutoff: datetime
    rows: list[dict[str, Any]]
    exclusions: Counter[str] = field(default_factory=Counter)
    candidate_revisions: int = 0
    flags: Counter[str] = field(default_factory=Counter)

    def exclusion_report(self) -> list[dict[str, Any]]:
        reasons = {
            "no_availability_proof": "source_available_at or availability_basis missing: no proof means exclusion",
            "available_after_cutoff": "proven availability is after the cutoff",
            "ineligible": "connector/concept/subject not eligible under the eligibility decisions in force",
            "retracted": "observation retracted",
        }
        return [
            {"reason": k, "count": v, "explanation": reasons.get(k, k)}
            for k, v in sorted(self.exclusions.items())
        ]

    def flag_report(self) -> list[dict[str, Any]]:
        """Post-cutoff quality knowledge about *included* rows: flagged, never used to exclude."""
        reasons = {
            "retracted_after_cutoff": "retracted after the cutoff; it was available then, so it stays in",
            "ineligible_after_cutoff": "eligible under the decision in force at the cutoff, ineligible today",
            "eligibility_from_first_decision_after_cutoff": (
                "REVA had no eligibility decision at the cutoff; the first recorded (design) decision was used"),
        }
        return [
            {"flag": k, "count": v, "explanation": reasons.get(k, k)}
            for k, v in sorted(self.flags.items())
        ]


_OBS_CTE = """
WITH base AS (
  SELECT o.*,
         -- eligibility clock: the cutoff; in available_then, if REVA had no decision for this
         -- (connector version, concept) by the cutoff, the first decision it ever recorded.
         CASE WHEN :mode = 'available_then'
              THEN GREATEST(CAST(:cutoff AS timestamptz),
                            (SELECT min(e.recorded_at) FROM evidence.eligibility e
                             WHERE e.connector_version_id = o.connector_version_id
                               AND e.concept_version_id = o.concept_version_id))
              ELSE CAST(:cutoff AS timestamptz) END AS eligibility_evaluated_at
  FROM evidence.observation o
), cand AS (
  SELECT o.id, o.source_id, o.logical_key, o.revision_no, o.subject_id, o.concept_version_id, o.support_id,
         o.value, o.uncertainty, o.source_flags, o.recorded_at, o.observed_at, o.source_published_at,
         o.source_available_at, o.availability_basis, o.source_revision_id, o.connector_version_id,
         o.acquisition_id, o.parser_version, s.source_key, c.concept_id, a.origin, a.retrieved_at,
         sp.temporal_extent, sp.spatial_spec, sp.aggregation, o.eligibility_evaluated_at,
         EXISTS (SELECT 1 FROM evidence.observation_retraction r
                 WHERE r.observation_id = o.id AND r.recorded_at > CAST(:cutoff AS timestamptz))
           AS retracted_after_cutoff,
         NOT evidence.is_eligible(o.connector_version_id, o.concept_version_id, o.subject_id,
                                  CAST(:now AS timestamptz)) AS ineligible_now,
         CASE
           WHEN NOT evidence.is_eligible(o.connector_version_id, o.concept_version_id, o.subject_id,
                                         o.eligibility_evaluated_at) THEN 'ineligible'
           WHEN EXISTS (SELECT 1 FROM evidence.observation_retraction r
                        WHERE r.observation_id = o.id AND r.recorded_at <= CAST(:cutoff AS timestamptz))
             THEN 'retracted'
           WHEN :mode = 'available_then' AND (o.source_available_at IS NULL OR o.availability_basis IS NULL)
             THEN 'no_availability_proof'
           WHEN :mode = 'available_then' AND o.source_available_at > :cutoff THEN 'available_after_cutoff'
           ELSE NULL
         END AS exclusion
  FROM base o
  JOIN evidence.source s ON s.id = o.source_id
  JOIN evidence.acquisition a ON a.id = o.acquisition_id
  JOIN ontology.concept_version c ON c.id = o.concept_version_id
  JOIN world.support sp ON sp.id = o.support_id
  WHERE a.origin = ANY(:origins)
    AND (CAST(:concepts AS text[]) IS NULL OR c.concept_id = ANY(CAST(:concepts AS text[])))
    -- time filter BEFORE revision selection (as-known clock)
    AND (:mode = 'available_then' OR o.recorded_at <= :cutoff)
)
"""

_OBS_SELECT = _OBS_CTE + """
, ranked AS (
  SELECT cand.*,
         count(*) OVER (PARTITION BY source_id, logical_key) AS eligible_revisions,
         row_number() OVER (
           PARTITION BY source_id, logical_key
           ORDER BY CASE WHEN :mode = 'available_then' THEN source_available_at END DESC NULLS LAST,
                    revision_no DESC) AS rn
  FROM cand WHERE exclusion IS NULL
)
SELECT r.*, f.value AS first_release_value, f.recorded_at AS first_release_recorded_at
FROM ranked r
LEFT JOIN evidence.observation f
  ON f.source_id = r.source_id AND f.logical_key = r.logical_key AND f.revision_no = 1
     AND (:mode = 'available_then' OR f.recorded_at <= :cutoff)
WHERE r.rn = 1
ORDER BY r.concept_id, r.logical_key
"""

_OBS_COUNTS = _OBS_CTE + """
SELECT exclusion, count(*) AS n FROM cand GROUP BY exclusion
"""


class CutoffRepository:
    """Time-scoped, read-only access for replay and evaluation."""

    __slots__ = ("_conn", "_cutoff", "_guard", "_mode", "_now", "_origins")

    def __init__(
        self,
        conn: Connection,
        cutoff: datetime,
        mode: str | CutoffMode = CutoffMode.as_known,
        *,
        origins: Sequence[str] = ("live",),
    ) -> None:
        if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
            raise ValidationProblem("cutoff must be a timezone-aware datetime")
        try:
            m = CutoffMode(mode)
        except ValueError as e:
            raise CapabilityGap(f"unknown cutoff mode {mode!r}", supported=[x.value for x in CutoffMode]) from e
        bad = set(origins) - {"live", "test_fixture"}
        if bad or not origins:
            raise ValidationProblem(f"invalid origins {sorted(bad)}")
        self._conn = conn
        self._cutoff = cutoff
        self._mode = m
        self._origins = list(origins)
        self._guard = LeakageGuard(cutoff, _GUARD_FOR[m])
        self._now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()

    @property
    def cutoff(self) -> datetime:
        return self._cutoff

    @property
    def mode(self) -> CutoffMode:
        return self._mode

    @property
    def guard(self) -> LeakageGuard:
        return self._guard

    @property
    def queried_at(self) -> datetime:
        """Database clock when this repository was opened (reconstruction time)."""
        return self._now

    # ----------------------------------------------------------------- evidence
    def _obs_params(self, concepts: Sequence[str] | None) -> dict[str, Any]:
        # Retractions and eligibility decisions are always evaluated as of the cutoff
        # (available_then: see the module docstring for the design-decision floor);
        # ``now`` is used only to flag post-cutoff changes, never to exclude.
        return {
            "cutoff": self._cutoff,
            "now": self._now,
            "mode": self._mode.value,
            "origins": self._origins,
            "concepts": list(concepts) if concepts else None,
        }

    def observations(self, concepts: Sequence[str] | None = None) -> ObservationScope:
        """One revision per lineage among observations admissible at the cutoff."""
        params = self._obs_params(concepts)
        rows = [dict(r) for r in self._conn.execute(text(_OBS_SELECT), params).mappings()]
        scope = ObservationScope(self._mode, self._cutoff, [])
        for r in self._conn.execute(text(_OBS_COUNTS), params).mappings():
            if r["exclusion"] is None:
                scope.candidate_revisions = int(r["n"])
            else:
                scope.exclusions[r["exclusion"]] = int(r["n"])
        for r in rows:
            self._guard.check(self.observation_input(r))  # defence in depth
            r["ineligible_after_cutoff"] = bool(r.pop("ineligible_now"))
            if r["retracted_after_cutoff"]:
                scope.flags["retracted_after_cutoff"] += 1
            if r["ineligible_after_cutoff"]:
                scope.flags["ineligible_after_cutoff"] += 1
            if r["eligibility_evaluated_at"] is not None and r["eligibility_evaluated_at"] > self._cutoff:
                scope.flags["eligibility_from_first_decision_after_cutoff"] += 1
            scope.rows.append(r)
        return scope

    @staticmethod
    def observation_input(row: dict[str, Any]) -> InputRef:
        return InputRef(
            RefKind.observation_revision,
            str(row["id"]),
            recorded_at=row["recorded_at"],
            source_available_at=row.get("source_available_at"),
            availability_basis=row.get("availability_basis"),
            detail={"logical_key": row["logical_key"], "revision_no": row["revision_no"],
                    "source_key": row["source_key"]},
        )

    def claims(self, status: Sequence[str] = ("reviewed",)) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            text("""SELECT id, claim_key, version, card, evidence_kind, status, extraction_method, recorded_at
                    FROM evidence.claim WHERE recorded_at <= :c AND status = ANY(:st)
                    ORDER BY claim_key, version"""),
            {"c": self._cutoff, "st": list(status)},
        ).mappings()
        out = [dict(r) for r in rows]
        for r in out:
            self._guard.check(InputRef(RefKind.paper_coefficient, str(r["id"]), recorded_at=r["recorded_at"]))
        return out

    # ----------------------------------------------------------------- identity / ontology
    def aliases(self, provider: str | None = None) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            text("""SELECT id, entity_id, provider, external_key, valid_range, mapping_version, confidence_kind,
                           recorded_at
                    FROM world.alias WHERE recorded_at <= :c AND (CAST(:p AS text) IS NULL OR provider = :p)
                    ORDER BY provider, external_key, recorded_at"""),
            {"c": self._cutoff, "p": provider},
        ).mappings()
        out = [dict(r) for r in rows]
        for r in out:
            self._guard.check(InputRef(RefKind.alias_or_boundary_mapping, str(r["id"]), recorded_at=r["recorded_at"]))
        return out

    def resolve_alias(self, provider: str, external_key: str) -> UUID | None:
        """Entity for a provider key using only aliases recorded at/before the cutoff."""
        row = self._conn.execute(
            text("""SELECT id, entity_id, recorded_at FROM world.alias
                    WHERE provider = :p AND external_key = :k AND recorded_at <= :c
                      AND confidence_kind IN ('reviewed_exact','provider_declared')
                    ORDER BY recorded_at DESC LIMIT 1"""),
            {"p": provider, "k": external_key, "c": self._cutoff},
        ).mappings().one_or_none()
        if row is None:
            return None
        self._guard.check(InputRef(RefKind.alias_or_boundary_mapping, str(row["id"]), recorded_at=row["recorded_at"]))
        return UUID(str(row["entity_id"]))

    def mappings(self, mapping_key: str | None = None) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            text("""SELECT id, mapping_key, source_ref, target_concept_id, mapping_kind, version, approval,
                           recorded_at
                    FROM ontology.mapping WHERE recorded_at <= :c
                      AND (CAST(:k AS text) IS NULL OR mapping_key = :k)
                    ORDER BY mapping_key, recorded_at"""),
            {"c": self._cutoff, "k": mapping_key},
        ).mappings()
        out = [dict(r) for r in rows]
        for r in out:
            self._guard.check(InputRef(RefKind.alias_or_boundary_mapping, str(r["id"]), recorded_at=r["recorded_at"]))
        return out

    # ----------------------------------------------------------------- models / population
    def model_versions(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Model versions usable at the cutoff.

        Trained models need ``training_cutoff <= cutoff``; a trained model with an
        unknown training cutoff is excluded. In the operational modes the
        version must also have been recorded at/before the cutoff.
        """
        rows = self._conn.execute(
            text("""SELECT id, model_key, version, kind, training_cutoff, training_manifest_sha256, approval_status,
                           spec_sha256, recorded_at
                    FROM model.version
                    WHERE (CAST(:k AS text) IS NULL OR kind = :k)
                      AND ((training_cutoff IS NOT NULL AND training_cutoff <= :c)
                           OR (training_cutoff IS NULL AND training_manifest_sha256 IS NULL))
                      AND (:mode = 'available_then' OR recorded_at <= :c)
                    ORDER BY model_key, version"""),
            {"c": self._cutoff, "k": kind, "mode": self._mode.value},
        ).mappings()
        out = [dict(r) for r in rows]
        for r in out:
            if r["training_cutoff"] is not None:
                self._guard.check(InputRef(RefKind.trained_model, str(r["id"]), recorded_at=r["recorded_at"],
                                           training_cutoff=r["training_cutoff"]))
        return out

    def population_datasets(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            text("""SELECT d.id, d.context_id, d.recipe_sha256, d.materialisation_sha256, d.record_count,
                           d.recorded_at, c.knowledge_cutoff
                    FROM model.population_dataset d JOIN meta.context c ON c.id = d.context_id
                    WHERE d.recorded_at <= :c AND (c.knowledge_cutoff IS NULL OR c.knowledge_cutoff <= :c)
                    ORDER BY d.recorded_at"""),
            {"c": self._cutoff},
        ).mappings()
        out = [dict(r) for r in rows]
        for r in out:
            self._guard.check(InputRef(RefKind.population_calibration, str(r["id"]), recorded_at=r["recorded_at"],
                                       fitted_on_data_until=r["knowledge_cutoff"]))
        return out

    # ----------------------------------------------------------------- belief releases
    def releases(self, origin: str | None = None) -> list[dict[str, Any]]:
        """Releases published (ops.publication) at/before the cutoff, in publication order."""
        rows = self._conn.execute(
            text("""SELECT r.id, r.context_id, r.origin, r.manifest_sha256, r.ontology_manifest_sha256,
                           r.model_manifest_sha256, r.knowledge_cutoff, r.method, r.recorded_at,
                           p.sequence, p.published_at
                    FROM belief.release r JOIN ops.publication p ON p.release_id = r.id
                    WHERE p.published_at <= :c AND (CAST(:o AS text) IS NULL OR r.origin = :o)
                    ORDER BY p.sequence"""),
            {"c": self._cutoff, "o": origin},
        ).mappings()
        return [dict(r) for r in rows]

    def release_at_cutoff(self, origin: str | None = None) -> dict[str, Any] | None:
        rels = self.releases(origin)
        return rels[-1] if rels else None

    def release_estimates(self, release_id: UUID | str) -> list[dict[str, Any]]:
        """Estimates and pinned inputs of a release that was published at/before the cutoff."""
        pub = self._conn.execute(
            text("SELECT published_at FROM ops.publication WHERE release_id = :r"), {"r": release_id}
        ).scalar_one_or_none()
        if pub is None or pub > self._cutoff:
            raise LeakageBlocked(
                f"release {release_id} was not published at/before the cutoff",
                release_id=str(release_id), cutoff=self._cutoff.isoformat(),
                published_at=pub.isoformat() if pub else None,
            )
        rows = self._conn.execute(
            text("""SELECT e.id AS estimate_id, e.subject_id, c.concept_id, e.concept_version_id, e.support_id,
                           sp.temporal_extent, e.modality, e.value, e.uncertainty, e.alternatives,
                           e.discrepancy_flags, i.observation_id, i.claim_id, i.prior_estimate_id,
                           o.logical_key, o.revision_no, o.recorded_at AS observation_recorded_at,
                           o.source_published_at, o.source_available_at, o.availability_basis,
                           a.retrieved_at, src.source_key
                    FROM belief.estimate e
                    JOIN ontology.concept_version c ON c.id = e.concept_version_id
                    JOIN world.support sp ON sp.id = e.support_id
                    JOIN belief.input i ON i.estimate_id = e.id
                    LEFT JOIN evidence.observation o ON o.id = i.observation_id
                    LEFT JOIN evidence.acquisition a ON a.id = o.acquisition_id
                    LEFT JOIN evidence.source src ON src.id = o.source_id
                    WHERE e.release_id = :r
                    ORDER BY c.concept_id, o.logical_key, e.id"""),
            {"r": release_id},
        ).mappings()
        out = [dict(r) for r in rows]
        # A release is an operational record: its inputs are always checked on the
        # REVA commit clock, whatever mode this repository was opened in.
        operational = LeakageGuard(self._cutoff, GuardMode.strict)
        for r in out:
            if r["observation_id"] is not None:
                operational.check(InputRef(RefKind.observation_revision, str(r["observation_id"]),
                                           recorded_at=r["observation_recorded_at"]))
        return out
