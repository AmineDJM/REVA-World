"""Future-information leakage guard for replay and backtesting.

Every input a replay, training fold or evaluation consumes is described by a
typed :class:`InputRef` carrying the timestamp that decides whether the input
could have been known at the cutoff:

=========================== =====================================================
kind                        deciding timestamp(s)
=========================== =====================================================
observation_revision        strict: ``recorded_at`` (REVA's own commit clock);
                            available_then: ``source_available_at`` + basis
alias_or_boundary_mapping   ``recorded_at`` (when REVA recorded the mapping)
fitted_scaler               ``fitted_on_data_until`` (+ ``recorded_at`` in strict)
trained_model               ``training_cutoff`` (+ ``recorded_at`` in strict)
paper_coefficient           strict: ``recorded_at``; available_then: proven
                            ``source_available_at`` else ``recorded_at``
population_calibration      ``recorded_at`` and, if declared, ``fitted_on_data_until``
=========================== =====================================================

Unknown deciding timestamps are never guessed: in a blocking mode an input
whose relevant time is unknown is blocked ("no proof means exclusion").

Modes:

* ``strict`` (operational as-known) and ``available_then`` block by raising
  :class:`reva.kernel.errors.LeakageBlocked`.
* ``revised_data_retrospective`` is a *downgrade*. It never blocks, but every
  verdict is labelled. It is only reachable by passing the mode explicitly at
  construction; a guard cannot change mode afterwards and no code path falls
  back from strict to retrospective.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from reva.kernel.errors import CapabilityGap, LeakageBlocked, ValidationProblem

RETROSPECTIVE_LABEL = "revised_data_retrospective"
RETROSPECTIVE_DISCLAIMER = "not a real-time backtest; forecasts were not issued at these origins"


class GuardMode(StrEnum):
    strict = "strict"
    available_then = "available_then"
    revised_data_retrospective = "revised_data_retrospective"


class RefKind(StrEnum):
    observation_revision = "observation_revision"
    alias_or_boundary_mapping = "alias_or_boundary_mapping"
    fitted_scaler = "fitted_scaler"
    trained_model = "trained_model"
    paper_coefficient = "paper_coefficient"
    population_calibration = "population_calibration"


@dataclass(frozen=True)
class InputRef:
    """A typed reference to one input with the timestamps relevant to its kind."""

    kind: RefKind
    ref_id: str
    recorded_at: datetime | None = None
    training_cutoff: datetime | None = None
    fitted_on_data_until: datetime | None = None
    source_available_at: datetime | None = None
    availability_basis: Mapping[str, Any] | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RefKind(self.kind))
        for name in ("recorded_at", "training_cutoff", "fitted_on_data_until", "source_available_at"):
            v = getattr(self, name)
            if v is not None and v.tzinfo is None:
                raise ValidationProblem(f"{name} must be timezone-aware", ref_id=self.ref_id)

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "ref_id": self.ref_id,
            "recorded_at": _iso(self.recorded_at),
            "training_cutoff": _iso(self.training_cutoff),
            "fitted_on_data_until": _iso(self.fitted_on_data_until),
            "source_available_at": _iso(self.source_available_at),
            "availability_basis": dict(self.availability_basis) if self.availability_basis else None,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class Verdict:
    ref: InputRef
    admitted: bool
    mode: GuardMode
    deciding_field: str
    deciding_time: datetime | None
    reason: str
    label: str | None = None
    would_block_in_strict: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            **self.ref.as_json(),
            "admitted": self.admitted,
            "guard_mode": self.mode.value,
            "deciding_field": self.deciding_field,
            "deciding_time": _iso(self.deciding_time),
            "reason": self.reason,
            "would_block_in_strict": self.would_block_in_strict,
            **({"label": self.label} if self.label else {}),
        }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


class LeakageGuard:
    """Checks typed input references against a knowledge cutoff."""

    __slots__ = ("_cutoff", "_mode", "_verdicts")

    def __init__(self, cutoff: datetime, mode: str | GuardMode = GuardMode.strict) -> None:
        if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
            raise ValidationProblem("cutoff must be a timezone-aware datetime")
        try:
            m = GuardMode(mode)
        except ValueError as e:
            raise CapabilityGap(f"unknown leakage-guard mode {mode!r}", supported=[x.value for x in GuardMode]) from e
        self._cutoff = cutoff
        self._mode = m
        self._verdicts: list[Verdict] = []

    # Read-only: a guard can never be switched to a weaker mode after creation.
    @property
    def cutoff(self) -> datetime:
        return self._cutoff

    @property
    def mode(self) -> GuardMode:
        return self._mode

    @property
    def blocking(self) -> bool:
        return self._mode is not GuardMode.revised_data_retrospective

    @property
    def label(self) -> str:
        return RETROSPECTIVE_LABEL if not self.blocking else self._mode.value

    @property
    def verdicts(self) -> tuple[Verdict, ...]:
        return tuple(self._verdicts)

    def flagged(self) -> list[Verdict]:
        """Retrospective-mode inputs that a strict guard would have blocked."""
        return [v for v in self._verdicts if v.would_block_in_strict]

    # ------------------------------------------------------------------ core
    def _decide(self, ref: InputRef) -> tuple[bool, str, datetime | None, str]:
        """(ok, deciding_field, deciding_time, reason) under blocking semantics."""
        c = self._cutoff
        avail_mode = self._mode is GuardMode.available_then

        def after(name: str, t: datetime | None) -> tuple[bool, str, datetime | None, str] | None:
            if t is None:
                return (False, name, None, f"{name} unknown; unknown dates are never assumed early")
            if t > c:
                return (False, name, t, f"{name} {t.isoformat()} is after cutoff {c.isoformat()}")
            return None

        k = ref.kind
        if k is RefKind.observation_revision:
            if avail_mode:
                if ref.source_available_at is None or not ref.availability_basis:
                    return (False, "source_available_at", ref.source_available_at,
                            "no independently evidenced availability (source_available_at + basis)")
                return after("source_available_at", ref.source_available_at) or (
                    True, "source_available_at", ref.source_available_at, "available at/before cutoff")
            return after("recorded_at", ref.recorded_at) or (
                True, "recorded_at", ref.recorded_at, "recorded at/before cutoff")
        if k is RefKind.alias_or_boundary_mapping:
            return after("recorded_at", ref.recorded_at) or (
                True, "recorded_at", ref.recorded_at, "mapping recorded at/before cutoff")
        if k is RefKind.fitted_scaler:
            bad = after("fitted_on_data_until", ref.fitted_on_data_until)
            if bad:
                return bad
            if not avail_mode:
                bad = after("recorded_at", ref.recorded_at)
                if bad:
                    return bad
            return (True, "fitted_on_data_until", ref.fitted_on_data_until, "fitted only on pre-cutoff data")
        if k is RefKind.trained_model:
            bad = after("training_cutoff", ref.training_cutoff)
            if bad:
                return bad
            if not avail_mode:
                bad = after("recorded_at", ref.recorded_at)
                if bad:
                    return bad
            return (True, "training_cutoff", ref.training_cutoff, "trained only on pre-cutoff data")
        if k is RefKind.paper_coefficient:
            if avail_mode and ref.source_available_at is not None and ref.availability_basis:
                return after("source_available_at", ref.source_available_at) or (
                    True, "source_available_at", ref.source_available_at, "publication proven before cutoff")
            return after("recorded_at", ref.recorded_at) or (
                True, "recorded_at", ref.recorded_at, "claim recorded at/before cutoff")
        if k is RefKind.population_calibration:
            bad = after("recorded_at", ref.recorded_at)
            if bad:
                return bad
            if ref.fitted_on_data_until is not None:
                bad = after("fitted_on_data_until", ref.fitted_on_data_until)
                if bad:
                    return bad
            return (True, "recorded_at", ref.recorded_at, "calibration recorded at/before cutoff")
        raise CapabilityGap(f"no leakage rule for input kind {k!r}")  # pragma: no cover

    def check(self, ref: InputRef) -> Verdict:
        """Admit ``ref`` or block it.

        In ``strict``/``available_then`` mode a violating input raises
        :class:`LeakageBlocked`; in ``revised_data_retrospective`` mode it is
        admitted with the retrospective label attached.
        """
        ok, fld, t, reason = self._decide(ref)
        if ok:
            v = Verdict(ref, True, self._mode, fld, t, reason, None if self.blocking else RETROSPECTIVE_LABEL)
            self._verdicts.append(v)
            return v
        if self.blocking:
            self._verdicts.append(Verdict(ref, False, self._mode, fld, t, reason))
            raise LeakageBlocked(
                f"{ref.kind.value} {ref.ref_id} blocked in {self._mode.value} replay: {reason}",
                kind=ref.kind.value,
                ref_id=ref.ref_id,
                deciding_field=fld,
                deciding_time=_iso(t),
                cutoff=self._cutoff.isoformat(),
                mode=self._mode.value,
            )
        v = Verdict(ref, True, self._mode, fld, t,
                    f"admitted only because mode is {RETROSPECTIVE_LABEL}: {reason}", RETROSPECTIVE_LABEL,
                    would_block_in_strict=True)
        self._verdicts.append(v)
        return v

    def check_all(self, refs: list[InputRef]) -> list[Verdict]:
        return [self.check(r) for r in refs]

    def admits(self, ref: InputRef) -> bool:
        """Non-raising probe used for filtering; records nothing."""
        return self._decide(ref)[0]


# --------------------------------------------------------------------------- DB ref loaders
# Loaders read one specific record in order to check it; they are not general
# current-data reads and are not exposed by the cutoff-scoped repository.

def observation_ref(conn: Connection, observation_id: UUID | str) -> InputRef:
    r = conn.execute(
        text(
            """SELECT o.id, o.logical_key, o.revision_no, o.recorded_at, o.source_available_at,
                      o.availability_basis, o.source_published_at, s.source_key, a.retrieved_at
               FROM evidence.observation o JOIN evidence.source s ON s.id = o.source_id
               JOIN evidence.acquisition a ON a.id = o.acquisition_id WHERE o.id = :i"""
        ),
        {"i": observation_id},
    ).mappings().one()
    return InputRef(
        RefKind.observation_revision,
        str(r["id"]),
        recorded_at=r["recorded_at"],
        source_available_at=r["source_available_at"],
        availability_basis=r["availability_basis"],
        detail={"logical_key": r["logical_key"], "revision_no": r["revision_no"], "source_key": r["source_key"],
                "retrieved_at": _iso(r["retrieved_at"]), "source_published_at": _iso(r["source_published_at"])},
    )


def alias_ref(conn: Connection, alias_id: UUID | str) -> InputRef:
    r = conn.execute(
        text("SELECT id, provider, external_key, entity_id, mapping_version, valid_range, recorded_at "
             "FROM world.alias WHERE id = :i"),
        {"i": alias_id},
    ).mappings().one()
    return InputRef(
        RefKind.alias_or_boundary_mapping, str(r["id"]), recorded_at=r["recorded_at"],
        detail={"table": "world.alias", "provider": r["provider"], "external_key": r["external_key"],
                "entity_id": str(r["entity_id"]), "mapping_version": r["mapping_version"],
                "valid_range": str(r["valid_range"]) if r["valid_range"] is not None else None},
    )


def mapping_ref(conn: Connection, mapping_id: UUID | str) -> InputRef:
    r = conn.execute(
        text("SELECT id, mapping_key, version, mapping_kind, recorded_at FROM ontology.mapping WHERE id = :i"),
        {"i": mapping_id},
    ).mappings().one()
    return InputRef(
        RefKind.alias_or_boundary_mapping, str(r["id"]), recorded_at=r["recorded_at"],
        detail={"table": "ontology.mapping", "mapping_key": r["mapping_key"], "version": r["version"],
                "mapping_kind": r["mapping_kind"]},
    )


def model_version_ref(conn: Connection, model_version_id: UUID | str) -> InputRef:
    r = conn.execute(
        text("SELECT id, model_key, version, kind, training_cutoff, training_manifest_sha256, recorded_at "
             "FROM model.version WHERE id = :i"),
        {"i": model_version_id},
    ).mappings().one()
    return InputRef(
        RefKind.trained_model, str(r["id"]), recorded_at=r["recorded_at"], training_cutoff=r["training_cutoff"],
        detail={"model_key": r["model_key"], "version": r["version"], "kind": r["kind"],
                "training_manifest_sha256": r["training_manifest_sha256"]},
    )


def claim_ref(conn: Connection, claim_id: UUID | str) -> InputRef:
    r = conn.execute(
        text("SELECT id, claim_key, version, status, evidence_kind, recorded_at FROM evidence.claim WHERE id = :i"),
        {"i": claim_id},
    ).mappings().one()
    return InputRef(
        RefKind.paper_coefficient, str(r["id"]), recorded_at=r["recorded_at"],
        detail={"claim_key": r["claim_key"], "version": r["version"], "status": r["status"],
                "evidence_kind": r["evidence_kind"]},
    )


def population_ref(conn: Connection, dataset_id: UUID | str) -> InputRef:
    r = conn.execute(
        text("SELECT d.id, d.recipe_sha256, d.recorded_at, c.knowledge_cutoff "
             "FROM model.population_dataset d JOIN meta.context c ON c.id = d.context_id WHERE d.id = :i"),
        {"i": dataset_id},
    ).mappings().one()
    return InputRef(
        RefKind.population_calibration, str(r["id"]), recorded_at=r["recorded_at"],
        fitted_on_data_until=r["knowledge_cutoff"],
        detail={"recipe_sha256": r["recipe_sha256"]},
    )


def scaler_ref(manifest: Mapping[str, Any]) -> InputRef:
    """A fitted transform (scaler) described by its fit manifest.

    Required keys: ``id`` and ``fitted_on_data_until``; ``recorded_at`` when the
    fitted scaler was committed. Missing timestamps are not guessed.
    """

    def ts(key: str) -> datetime | None:
        v = manifest.get(key)
        if v is None or isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v))

    return InputRef(
        RefKind.fitted_scaler, str(manifest["id"]), recorded_at=ts("recorded_at"),
        fitted_on_data_until=ts("fitted_on_data_until"),
        detail={k: v for k, v in manifest.items() if k not in ("id", "recorded_at", "fitted_on_data_until")},
    )
