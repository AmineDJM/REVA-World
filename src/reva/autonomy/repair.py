"""Connector self-healing (architecture §10.3): classify drift, propose repairs, pause publication.

``ConnectorRepairAdvisor`` takes a connector whose latest acquisition was
quarantined (ingest validation blocked it) or flagged by the health monitor
(schema fingerprint drift, persistent request failures) and compares the new
raw response with the **immutable fixture bank** (``tests/fixtures/source/<connector>``
loaded through :func:`reva.autonomy.fixtures.bank_from_dir`, which verifies every
captured hash). It never edits a connector, a mapping, a policy or a gate result.

Classification (most severe wins):

=============================  =====================================  ====
class                          action                                 tier
=============================  =====================================  ====
additive_unused_fields         re-validate; propose re-enabling the   T1
                               connector with the *unchanged* mapping
field_rename, unit_change,     proposal for a NEW mapping version     T2
scale_change, meaning_change,  (needs review) + semantic diff
classification_change,         artifact; any proposed mapping is
value_type_change,             replayed over every stored fixture
field_removed,
unreviewed_structure_change,
content_anomaly
endpoint_disappeared           documentation-discovery task over the  T0
(404/410/DNS, persistent)      registry's allowlisted documentation
                               (never URL guessing)
authentication_changed         credential provisioning required       T3
(401/403, persistent)          (never bypass)
transient_failure              retry with backoff; no proposal        —
=============================  =====================================  ====

Schema fingerprints (:func:`semantic_schema_fingerprint`) ignore JSON key order
and values but preserve types, list/dimension structure and the values of the
connector's declared unit / classification / meaning fields.

History cannot be silently reinterpreted: :class:`FixtureReplayChecker` replays
every stored fixture (and every accepted raw acquisition in the database) through
both the reviewed parser and the proposed parser/mapping and requires identical
semantic outputs (concept, subject, support, value, uncertainty, relation) for
every historical vintage; otherwise its report is ``blocked`` and the gate blocks.

While a case is open, belief publication for the affected concepts is paused
with an ``ops.dirty_node`` marker ``publication_pause:belief:<concept>`` (outside
the ``belief:%`` namespace, so it neither triggers nor is cleared by a belief
release) plus a ``connector.publication_paused`` outbox event that names the
last accepted release, which stays readable and is reported as stale. Only a
gate evaluator or a named human can resume (:func:`resume_belief_publication`).

Proposals go through :class:`reva.autonomy.proposals.ProposalStore`; decisions
come only from :class:`reva.autonomy.gate.PolicyPromotionGate`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from reva.adapters.connectors.base import json_decimal
from reva.adapters.http.safe_client import redact_url, request_fingerprint
from reva.adapters.postgres import repositories as repo
from reva.autonomy.common import ArtifactIO, AutonomyBlocked, report, stable_id
from reva.autonomy.fixtures import bank_entries, bank_from_dir
from reva.autonomy.policy import AutonomyGuard, promotion_policy
from reva.autonomy.proposals import ProposalStore, base_commit
from reva.evidence.runner import FIXTURE_ROOT
from reva.kernel.errors import RevaError, ValidationProblem
from reva.ports.autonomy import ArtifactRef, Finding, PlatformChangeProposal, ValidationReport, ValueEstimate
from reva.ports.connectors import FetchBudget, FetchItem, ObservationBatch, RawAcquisition
from reva.ports.storage import ArtifactStore

log = logging.getLogger("reva.autonomy.repair")
AUTHOR = "agent:connector-repair"
REPLAY_EVALUATOR = "fixture-replay:semantic@1"
REVALIDATION_EVALUATOR = "connector-revalidation:unchanged-mapping@1"
REPAIR_SCHEMA = "reva-connector-repair/1"
DIFF_SCHEMA = "reva-connector-semantic-diff/1"
FINGERPRINT_SCHEMA = "reva-schema-fingerprint/1"
PAUSE_PREFIX = "publication_pause:belief:"
PERSISTENT_RUNS = 3
SEMANTIC_FIELDS = ("subject_provider", "subject_key", "concept_id", "concept_version", "support", "value",
                   "uncertainty", "relation")

# --------------------------------------------------------------------------- classes
ADDITIVE = "additive_unused_fields"
RENAME = "field_rename"
UNIT = "unit_change"
SCALE = "scale_change"
MEANING = "meaning_change"
CLASSIFICATION = "classification_change"
TYPE_CHANGE = "value_type_change"
REMOVED = "field_removed"
STRUCTURE = "unreviewed_structure_change"
CONTENT = "content_anomaly"
ENDPOINT = "endpoint_disappeared"
AUTH = "authentication_changed"
TRANSIENT = "transient_failure"
NO_CHANGE = "no_schema_change"

# Severity order of payload drift classes (first present wins).
PAYLOAD_PRIORITY = (UNIT, SCALE, MEANING, CLASSIFICATION, RENAME, TYPE_CHANGE, REMOVED, STRUCTURE)
T2_CLASSES = frozenset(PAYLOAD_PRIORITY) | {CONTENT}
ACTIONS: dict[str, tuple[str, int | None]] = {
    ADDITIVE: ("reenable_unchanged_mapping", 1),
    **{c: ("new_mapping_version", 2) for c in PAYLOAD_PRIORITY},
    CONTENT: ("review_content_anomaly", 2),
    ENDPOINT: ("documentation_discovery", 0),
    AUTH: ("credential_provisioning_required", 3),
    TRANSIENT: ("retry_with_backoff", None),
    NO_CHANGE: ("none", None),
}


# --------------------------------------------------------------------------- connector semantics
@dataclass(frozen=True)
class SchemaSemantics:
    """What a reviewed parser reads and which raw fields carry units/classifications/meaning.

    ``consumed`` lists every raw path the parser (or its validator) reads; an added
    path outside it is *unused*. Paths use the profile notation of
    :func:`schema_profile` (``$``, ``.key``, ``[]`` homogeneous list, ``[i]`` tuple position).
    """

    consumed: frozenset[str]
    units: tuple[str, ...]
    classifications: tuple[str, ...]
    meanings: tuple[str, ...]
    value_paths: tuple[str, ...]
    group_params: tuple[str, ...]


_WB_ROW = "$[1][]"
_CT_ROW = "$.data[]"
SEMANTICS: dict[str, SchemaSemantics] = {
    "worldbank.wdi.indicators": SchemaSemantics(
        consumed=frozenset(
            ["$[0]", "$[1]", "$[0].page", "$[0].pages", "$[0].per_page", "$[0].total", "$[0].lastupdated",
             "$[0].message", "$[]", "$[].message"]
            + [f"{_WB_ROW}.{k}" for k in (
                # series rows
                "countryiso3code", "date", "value", "obs_status", "decimal", "unit", "country", "country.id",
                # country metadata rows
                "id", "iso2Code", "name", "region", "region.value", "incomeLevel", "incomeLevel.value",
                "capitalCity", "longitude", "latitude",
                # indicator metadata rows
                "sourceNote", "sourceOrganization", "source", "source.value")]
            + [_WB_ROW]),
        units=(f"{_WB_ROW}.unit", f"{_WB_ROW}.decimal"),
        classifications=(f"{_WB_ROW}.indicator.id",),
        meanings=(f"{_WB_ROW}.indicator.value", f"{_WB_ROW}.sourceNote"),
        value_paths=(f"{_WB_ROW}.value",),
        group_params=("kind",),
    ),
    "un.comtrade.public_exact": SchemaSemantics(
        consumed=frozenset(
            ["$.data", "$.error", "$.count", "$.elapsedTime", _CT_ROW]
            + [f"{_CT_ROW}.{k}" for k in (
                "period", "reporterCode", "flowCode", "partnerCode", "partner2Code", "cmdCode", "customsCode",
                "motCode", "typeCode", "freqCode", "classificationCode", "isOriginalClassification", "isReported",
                "isAggregate", "legacyEstimationFlag", "primaryValue", "cifvalue", "fobvalue", "netWgt",
                "isNetWgtEstimated", "qtyUnitCode", "qty", "isQtyEstimated")]),
        units=(f"{_CT_ROW}.qtyUnitCode", f"{_CT_ROW}.qtyUnitAbbr"),
        classifications=tuple(f"{_CT_ROW}.{k}" for k in (
            "classificationCode", "classificationSearchCode", "cmdCode", "typeCode", "freqCode", "customsCode",
            "motCode")),
        meanings=(f"{_CT_ROW}.cmdDesc", f"{_CT_ROW}.flowDesc"),
        value_paths=(f"{_CT_ROW}.primaryValue", f"{_CT_ROW}.netWgt"),
        group_params=("scope",),
    ),
}


# --------------------------------------------------------------------------- paths
_TOKEN = re.compile(r'\.([^.\[\]"]+)|\[(\d+)\]|\[\]|\[("(?:[^"\\]|\\.)*")\]')


def _key_token(k: str) -> str:
    return f".{k}" if k and re.fullmatch(r'[^.\[\]"]+', k) else f"[{json.dumps(k)}]"


def parse_path(path: str) -> list[tuple[str, Any]]:
    if not path.startswith("$"):
        raise ValidationProblem("profile path must start with '$'", path=path[:200])
    out: list[tuple[str, Any]] = []
    pos = 1
    while pos < len(path):
        m = _TOKEN.match(path, pos)
        if m is None:
            raise ValidationProblem("malformed profile path", path=path[:200])
        if m.group(1) is not None:
            out.append(("key", m.group(1)))
        elif m.group(2) is not None:
            out.append(("idx", int(m.group(2))))
        elif m.group(3) is not None:
            out.append(("key", json.loads(m.group(3))))
        else:
            out.append(("all", None))
        pos = m.end()
    return out


def _parent(path: str) -> str:
    toks = parse_path(path)
    if not toks:
        return ""
    return _render(toks[:-1])


def _render(toks: Sequence[tuple[str, Any]]) -> str:
    s = "$"
    for kind, v in toks:
        s += _key_token(v) if kind == "key" else (f"[{v}]" if kind == "idx" else "[]")
    return s


def _nodes(obj: Any, toks: Sequence[tuple[str, Any]]) -> Iterator[Any]:
    if not toks:
        yield obj
        return
    (kind, v), rest = toks[0], toks[1:]
    if kind == "key":
        if isinstance(obj, dict) and v in obj:
            yield from _nodes(obj[v], rest)
    elif kind == "idx":
        if isinstance(obj, list) and v < len(obj):
            yield from _nodes(obj[v], rest)
    elif isinstance(obj, list):
        for x in obj:
            yield from _nodes(x, rest)


def values_at(obj: Any, path: str) -> list[Any]:
    return list(_nodes(obj, parse_path(path)))


def _records(obj: Any, field_path: str) -> tuple[list[dict[str, Any]], str]:
    toks = parse_path(field_path)
    if not toks or toks[-1][0] != "key":
        raise ValidationProblem("mapping rules address object fields", path=field_path[:200])
    return [r for r in _nodes(obj, toks[:-1]) if isinstance(r, dict)], toks[-1][1]


# --------------------------------------------------------------------------- profiles
def _type_label(o: Any) -> str:
    if o is None:
        return "null"
    if isinstance(o, bool):
        return "boolean"
    if isinstance(o, (int, float, Decimal)):
        return "number"
    if isinstance(o, str):
        return "string"
    if isinstance(o, dict):
        return "object"
    if isinstance(o, list):
        return "array"
    return type(o).__name__


def _kind(o: Any) -> str:
    return "object" if isinstance(o, dict) else "array" if isinstance(o, list) else "scalar"


def schema_profile(obj: Any) -> dict[str, list[str]]:
    """Path -> sorted JSON type labels. Key order and values are ignored; list structure is kept
    (homogeneous lists collapse to ``[]``, heterogeneous tuples keep positions ``[i]``)."""
    prof: dict[str, set[str]] = defaultdict(set)

    def walk(o: Any, path: str) -> None:
        prof[path].add(_type_label(o))
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + _key_token(str(k)))
        elif isinstance(o, list):
            if len({_kind(x) for x in o}) <= 1:
                for x in o:
                    walk(x, path + "[]")
            else:
                for i, x in enumerate(o):
                    walk(x, f"{path}[{i}]")

    walk(obj, "$")
    return {p: sorted(t) for p, t in sorted(prof.items())}


def _marker_values(obj: Any, paths: Sequence[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in paths:
        vals = sorted({_scalar_text(v) for v in values_at(obj, p) if v is not None})
        if vals:
            out[p] = vals
    return out


def _scalar_text(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return str(v.normalize()) if v == v.to_integral_value() else str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, default=str)
    return str(v)


def semantic_schema_fingerprint(obj: Any, semantics: SchemaSemantics | None = None) -> str:
    """SHA-256 over types/dimensions (key-order independent) and the declared unit,
    classification and meaning field values; ordinary values do not enter it."""
    markers: dict[str, list[str]] = {}
    if semantics is not None:
        raw = _marker_values(obj, (*semantics.units, *semantics.classifications, *semantics.meanings))
        markers = {p: [hashlib.sha256(v.encode()).hexdigest()[:16] if len(v) > 64 else v for v in vs]
                   for p, vs in raw.items()}
    doc = {"schema": FINGERPRINT_SCHEMA, "paths": schema_profile(obj), "markers": markers}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------- exact JSON I/O
def dumps_exact(obj: Any) -> bytes:
    """JSON encoding that writes Decimals as their exact literal (no float rounding)."""

    def enc(o: Any) -> str:
        if isinstance(o, dict):
            return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + enc(v) for k, v in o.items()) + "}"
        if isinstance(o, list):
            return "[" + ",".join(enc(x) for x in o) + "]"
        if o is None:
            return "null"
        if isinstance(o, bool):
            return "true" if o else "false"
        if isinstance(o, Decimal):
            if not o.is_finite():
                raise ValueError("non-finite decimal")
            return str(o)
        if isinstance(o, (int, float, str)):
            return json.dumps(o, ensure_ascii=False, allow_nan=False)
        raise TypeError(f"cannot encode {type(o).__name__}")

    return enc(obj).encode("utf-8")


def load_exact(body: bytes) -> Any:
    return json_decimal(body)


# --------------------------------------------------------------------------- mapping revisions
_DECIMAL_TEXT = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?")


@dataclass(frozen=True)
class ScaleRule:
    """Multiply ``path`` by ``factor`` where ``when_path == when_equals``.

    ``normalize_to`` (conditional rules only) rewrites the matched unit marker to the
    reviewed unit value, so the mapped payload carries the unit the reviewed parser
    and validator expect (e.g. ``"thousands"`` -> ``""`` after multiplying by 1000)
    instead of a rescaled value under a stale marker.
    """

    path: str
    factor: str
    when_path: str | None = None
    when_equals: str | None = None
    normalize_to: str | None = None


@dataclass(frozen=True)
class MappingRevision:
    """A declarative, versioned normalisation applied to the raw payload before the reviewed parser.

    Renames apply only where the new field is present (old vintages are untouched);
    scale rules apply where their condition holds (an unconditional rule touches
    every vintage and is therefore caught by the replay check); coercions turn
    strictly numeric strings into exact decimals. No code is generated.
    """

    base_parser_version: str
    renames: tuple[tuple[str, str], ...] = ()  # (new_path, accepted_path)
    scales: tuple[ScaleRule, ...] = ()
    coercions: tuple[str, ...] = ()  # paths coerced from numeric text to decimal
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"base_parser_version": self.base_parser_version,
                "renames": [{"from": a, "to": b} for a, b in self.renames],
                "scales": [{"path": s.path, "factor": s.factor, "when": (
                    {"path": s.when_path, "equals": s.when_equals} if s.when_path else None),
                    **({"normalize_to": s.normalize_to} if s.normalize_to is not None else {})}
                    for s in self.scales],
                "coercions": list(self.coercions), "note": self.note}

    @property
    def version(self) -> str:
        rules = {k: v for k, v in self.as_json().items() if k != "note"}
        h = hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:12]
        return f"{self.base_parser_version}+map.{h}"

    def apply(self, obj: Any) -> Any:
        for new, old in self.renames:
            recs, new_key = _records(obj, new)
            _, old_key = _records(obj, old)
            for r in recs:
                if new_key in r and old_key not in r:
                    r[old_key] = r.pop(new_key)
        for s in self.scales:
            recs, key = _records(obj, s.path)
            cond_key = _records(obj, s.when_path)[1] if s.when_path else None
            factor = Decimal(s.factor)
            for r in recs:
                if cond_key is not None and _scalar_text(r.get(cond_key)) != s.when_equals:
                    continue
                v = r.get(key)
                if isinstance(v, Decimal):
                    r[key] = v * factor
                if cond_key is not None and s.normalize_to is not None:
                    r[cond_key] = s.normalize_to
        for p in self.coercions:
            recs, key = _records(obj, p)
            for r in recs:
                v = r.get(key)
                if isinstance(v, str) and _DECIMAL_TEXT.fullmatch(v.strip()):
                    r[key] = Decimal(v.strip())
        return obj


class Parser(Protocol):
    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]: ...


class MappedParser:
    """Reviewed connector parser behind a declarative mapping revision."""

    def __init__(self, connector: Any, revision: MappingRevision) -> None:
        self.connector = connector
        self.revision = revision

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        try:
            obj = load_exact(raw.body)
        except ValueError:
            yield from self.connector.parse(raw)
            return
        body = dumps_exact(self.revision.apply(obj))
        yield from self.connector.parse(raw.model_copy(update={"body": body}))


def parser_identity(parser: Any) -> dict[str, Any]:
    if isinstance(parser, MappedParser):
        return {"kind": "declarative_mapping", "mapping_version": parser.revision.version,
                "mapping": parser.revision.as_json()}
    cls = type(parser)
    import inspect

    try:
        digest = "sha256:" + hashlib.sha256(inspect.getsource(cls).encode()).hexdigest()
    except (OSError, TypeError):
        digest = "unavailable"
    return {"kind": "code", "class": f"{cls.__module__}.{cls.__qualname__}", "source_digest": digest}


# --------------------------------------------------------------------------- semantic outputs
def semantic_outputs(batches: Sequence[ObservationBatch]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(logical_key -> canonical semantic JSON, logical_key -> flags JSON, expected keys)."""
    sem: dict[str, str] = {}
    flags: dict[str, str] = {}
    expected: list[str] = []
    for b in batches:
        expected.extend(b.expected_keys)
        for o in b.observations:
            d = o.model_dump(mode="json")
            sem[o.logical_key] = json.dumps({k: d.get(k) for k in SEMANTIC_FIELDS}, sort_keys=True, default=str)
            flags[o.logical_key] = json.dumps(d.get("source_flags") or {}, sort_keys=True, default=str)
    return sem, flags, sorted(expected)


def _decimal_values(sem: Mapping[str, str]) -> dict[str, tuple[str, Decimal]]:
    """logical_key -> (concept, value) for decimal values."""
    out = {}
    for k, s in sem.items():
        d = json.loads(s)
        v = d.get("value") or {}
        if v.get("kind") == "decimal":
            try:
                out[k] = (f"{d.get('concept_id')}@{d.get('concept_version')}", Decimal(str(v["value"])))
            except (InvalidOperation, KeyError):
                continue
    return out


def value_comparison(baseline: Mapping[str, str], new: Mapping[str, str]) -> dict[str, Any]:
    """Compare values on shared logical keys, per concept; detect uniform power-of-ten rescaling.

    A concept is *rescaled* when every shared nonzero value changed by the same factor
    10^k (k != 0); ordinary revisions are counted but never treated as unit changes.
    """
    b, n = _decimal_values(baseline), _decimal_values(new)
    shared = sorted(b.keys() & n.keys())
    per: dict[str, dict[str, Any]] = {}
    identical, revised = 0, []
    for k in shared:
        concept, bv = b[k]
        nv = n[k][1]
        st = per.setdefault(concept, {"shared": 0, "identical": 0, "exponents": set()})
        st["shared"] += 1
        if bv == nv:
            identical += 1
            st["identical"] += 1
            st["exponents"].add(0)
            continue
        revised.append(k)
        if bv == 0 or nv == 0:
            st["exponents"].add(None)
            continue
        t = (nv / bv).normalize().as_tuple()
        st["exponents"].add(int(t.exponent) if t.digits == (1,) and t.sign == 0 else None)
    concepts: dict[str, Any] = {}
    rescaled: dict[str, str] = {}
    for c, st in sorted(per.items()):
        exps = st.pop("exponents")
        uniform = len(exps) == 1 and None not in exps and 0 not in exps
        st["uniform_power_of_ten"] = uniform
        if uniform:
            rescaled[c] = f"1E{next(iter(exps))}"
            st["factor_new_over_old"] = rescaled[c]
        concepts[c] = st
    factors = set(rescaled.values())
    return {"shared_keys": len(shared), "identical": identical, "revised": len(revised),
            "revised_examples": revised[:10], "per_concept": concepts, "rescaled_concepts": rescaled,
            "uniform_power_of_ten": bool(rescaled),
            "factor_new_over_old": next(iter(factors)) if len(factors) == 1 else None,
            "baseline_only": len(b.keys() - n.keys()), "new_only": len(n.keys() - b.keys())}


# --------------------------------------------------------------------------- cases
@dataclass(frozen=True)
class AttemptSignal:
    item_key: str
    status: int | None
    kind: str  # http_status | dns_failure | transport | other
    message: str


@dataclass(frozen=True)
class RunSignals:
    recorded_at: datetime
    success_ratio: float | None
    signals: tuple[AttemptSignal, ...]


@dataclass(frozen=True)
class DriftCase:
    """A quarantined/drifted response (``body``) or a failure history (``failures``, newest last)."""

    connector: str
    observed_at: datetime
    origin: str
    item_key: str | None = None
    body: bytes | None = None
    media_type: str = "application/json"
    status: int | None = 200
    acquisition_id: str | None = None
    raw_ref: ArtifactRef | None = None
    quarantined: bool = False
    quarantine_findings: tuple[Mapping[str, Any], ...] = ()
    failures: tuple[RunSignals, ...] = ()


@dataclass(frozen=True)
class RepairAssessment:
    connector: str
    connector_version: str
    parser_version: str
    item_key: str | None
    classification: str
    action: str
    tier: int | None
    changes: tuple[Mapping[str, Any], ...]
    affected_concepts: tuple[str, ...]
    bank: ArtifactRef
    semantic_diff: ArtifactRef | None
    raw_ref: ArtifactRef | None
    accepted_fingerprint: str | None
    new_fingerprint: str | None
    suggested_mapping: MappingRevision | None
    evidence: Mapping[str, Any]
    case: DriftCase

    @property
    def schema_failure(self) -> bool:
        return self.classification not in (TRANSIENT, NO_CHANGE)


@dataclass(frozen=True)
class PublicationPause:
    node_keys: tuple[str, ...]
    concepts: tuple[str, ...]
    reason: str
    last_accepted_release: Mapping[str, Any] | None


@dataclass(frozen=True)
class RepairProposal:
    assessment: RepairAssessment
    proposal: PlatformChangeProposal
    proposal_type: str
    manifest: ArtifactRef
    reports: tuple[ValidationReport, ...]
    pause: PublicationPause | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    proposal_record: ArtifactRef | None = None  # ProposalStore manifest (ops.proposal.manifest_sha256)


# --------------------------------------------------------------------------- failure signals
_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")


def classify_error(item: str, code: str, message: str, status: int | None = None) -> AttemptSignal:
    if status is None:
        m = _HTTP_STATUS.search(message or "")
        status = int(m.group(1)) if m else None
    if status is not None:
        return AttemptSignal(item, status, "http_status", message[:300])
    low = (message or "").lower()
    if code == "unsafe_request" and ("dns resolution failed" in low or "did not resolve" in low):
        return AttemptSignal(item, None, "dns_failure", message[:300])
    if "transport failure" in low:
        return AttemptSignal(item, None, "transport", message[:300])
    return AttemptSignal(item, None, "other", message[:300])


def _structured_status(v: Any) -> int | None:
    """The run record's structured HTTP status (``errors[].status``); ``None`` for old rows,
    which fall back to parsing ``HTTP nnn`` out of the message."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and 100 <= v <= 599:
        return v
    if isinstance(v, str) and v.isdigit() and 100 <= int(v) <= 599:
        return int(v)
    return None


def failure_history(conn: Connection, connector_name: str, *, version: str | None = None,
                    origin: str | None = None, since: datetime | None = None,
                    window: int = 20) -> tuple[RunSignals, ...]:
    """Run records (``reva.evidence.runner.record_health``) as per-item failure signals, oldest first."""
    rows = conn.execute(text(
        """SELECT h.recorded_at, h.metrics FROM ops.connector_health h
             JOIN evidence.connector_version cv ON cv.id = h.connector_version_id
            WHERE cv.name = :n AND (CAST(:v AS text) IS NULL OR cv.version = :v)
              AND (CAST(:o AS text) IS NULL OR h.metrics->>'origin' = :o)
              AND (CAST(:t AS timestamptz) IS NULL OR h.recorded_at >= :t)
              AND COALESCE(h.metrics->>'kind', '') <> 'health_vector'
            ORDER BY h.recorded_at DESC, h.id DESC LIMIT :w"""),
        {"n": connector_name, "v": version, "o": origin, "t": since, "w": window}).mappings().all()
    out = []
    for r in reversed(rows):
        m = dict(r["metrics"])
        sigs = tuple(classify_error(str(e.get("item", "")), str(e.get("code", "")), str(e.get("error", "")),
                                    _structured_status(e.get("status")))
                     for e in m.get("errors") or [] if e.get("code"))
        ratio = m.get("request_success_ratio")
        out.append(RunSignals(r["recorded_at"], float(ratio) if ratio is not None else None, sigs))
    return tuple(out)


def failure_streak(conn: Connection, connector_name: str, *, origin: str | None = None,
                   window: int = 20) -> dict[str, Any] | None:
    """The current streak of consecutive runs failing with authentication (401/403) or
    gone (404/410/DNS) signals, newest run last; ``None`` when the newest run has neither.

    ``connector_wide`` is true when no item of any run in the streak succeeded.
    ``since`` (the streak's first run) is a stable identity for the streak.
    """
    runs = failure_history(conn, connector_name, origin=origin, window=window)
    if not runs:
        return None
    for kind, pred in ((AUTH, _is_auth), (ENDPOINT, _is_gone)):
        n = _consecutive(runs, pred, None)
        if n:
            streak = runs[-n:]
            return {"kind": kind, "runs": n, "since": streak[0].recorded_at, "latest": streak[-1].recorded_at,
                    "statuses": sorted({sig.status for r in streak for sig in r.signals
                                        if pred(sig) and sig.status is not None}),
                    "items": sorted({sig.item_key for sig in streak[-1].signals if pred(sig)}),
                    "connector_wide": all(r.success_ratio is not None and r.success_ratio <= 0 for r in streak)}
    return None


def _is_gone(s: AttemptSignal) -> bool:
    return s.kind == "dns_failure" or s.status in (404, 410)


def _is_auth(s: AttemptSignal) -> bool:
    return s.status in (401, 403)


def _consecutive(runs: Sequence[RunSignals], pred: Any, item: str | None) -> int:
    n = 0
    for run in reversed(runs):
        hit = [s for s in run.signals if pred(s) and (item is None or s.item_key == item)]
        if not hit:
            break
        n += 1
    return n


# --------------------------------------------------------------------------- replay checker
class FixtureReplayChecker:
    """Independent evaluator: replays stored raw vintages through reviewed vs proposed parsers."""

    evaluator = REPLAY_EVALUATOR

    def __init__(self, io_: ArtifactIO) -> None:
        self.io = io_

    def check(self, connector: Any, proposed: Any, bank: ArtifactRef, *, proposal_key: str,
              history: Sequence[RawAcquisition] = (), new_raw: RawAcquisition | None = None,
              baseline_new: Mapping[str, str] | None = None) -> ValidationReport:
        plan = _plan_by_fingerprint(connector)
        findings: list[Finding] = []
        vintages: list[dict[str, Any]] = []
        replayed = 0
        for e in bank_entries(self.io, bank):
            if int(e.get("status", 200)) != 200:
                continue
            item = plan.get(e["request_fingerprint"])
            if item is None:
                findings.append(_finding("fixture_not_replayable", "blocker",
                                         f"stored fixture {e['url'][:160]} matches no planned request"))
                continue
            raw = _raw_from_entry(self.io, e, item)
            res = self._compare(connector, proposed, raw, strict=True)
            replayed += 1
            vintages.append({"source": "fixture_bank", "url": redact_url(e["url"]), "body_sha256": e["body_sha256"],
                             **res})
        for raw in history:
            res = self._compare(connector, proposed, raw, strict=False)
            if res.get("skipped"):
                vintages.append({"source": "accepted_acquisition", "item": raw.item.key, **res})
                continue
            replayed += 1
            vintages.append({"source": "accepted_acquisition", "item": raw.item.key, **res})
        changed = [v for v in vintages if v.get("semantic_changes")]
        for v in changed:
            findings.append(_finding(
                "historical_reinterpretation", "blocker",
                f"{v.get('url') or v.get('item')}: {v['semantic_changes']} historical semantic outputs differ "
                f"(e.g. {', '.join(v['examples'][:3])})"))
        for v in vintages:
            if v.get("error"):
                findings.append(_finding("historical_replay_failed", "blocker",
                                         f"{v.get('url') or v.get('item')}: {v['error']}"))
            elif v.get("flag_changes"):
                findings.append(_finding("historical_flags_changed", "warning",
                                         f"{v.get('url') or v.get('item')}: {v['flag_changes']} source-flag sets "
                                         "differ (non-semantic)"))
        if replayed == 0:
            findings.append(_finding("no_history", "blocker", "no stored vintage could be replayed"))
        new_summary: dict[str, Any] | None = None
        if new_raw is not None:
            new_summary = {}
            try:
                batches = list(proposed.parse(new_raw))
                validator = type(connector)() if _default_constructible(connector) else connector
                blockers = [f for b in batches for f in validator.validate(b).findings if f.severity == "blocker"]
                sem, _, _ = semantic_outputs(batches)
                new_summary = {"observations": len(sem), "validation_blockers": [f.code for f in blockers]}
                if baseline_new is not None:
                    new_summary["vs_same_request_vintage"] = value_comparison(baseline_new, sem)
                for f in blockers:
                    findings.append(_finding(f"new_payload_{f.code}", "blocker",
                                             f"drifted payload still fails validation: {f.message}"))
            except RevaError as e:
                new_summary = {"error": e.message}
                findings.append(_finding("new_payload_unparseable", "blocker",
                                         f"proposed parser cannot read the drifted payload: {e.message}"))
            except (KeyError, IndexError, TypeError, ValueError, AttributeError, ArithmeticError) as e:
                new_summary = {"error": type(e).__name__}
                findings.append(_finding("new_payload_unparseable", "blocker",
                                         f"proposed parser cannot read the drifted payload ({type(e).__name__})"))
        history_blocked = any(f.code in ("historical_reinterpretation", "historical_replay_failed",
                                         "fixture_not_replayable", "no_history") for f in findings)
        outcome = "blocked" if history_blocked else (
            "fail" if any(f.severity == "blocker" for f in findings) else "pass")
        evidence = {"schema": "reva-fixture-replay/1", "evaluator": self.evaluator, "proposal": proposal_key,
                    "fixture_bank": bank.sha256, "parser": parser_identity(proposed),
                    "baseline_parser_version": connector.describe().parser_version,
                    "vintages_replayed": replayed, "vintages": vintages, "new_payload": new_summary,
                    "outcome": outcome, "rule": "every historical vintage must yield identical semantic outputs "
                                                 "(concept, subject, support, value, uncertainty, relation)"}
        evidence["findings"] = [{"code": f.code, "severity": f.severity, "message": f.message} for f in findings]
        ref = self.io.put_json(evidence, "evaluation")
        findings = [Finding(f.code, f.severity, f.message, (ref,)) for f in findings]
        return report(outcome, findings, ref, self.evaluator, {"vintages": replayed})

    @staticmethod
    def _compare(connector: Any, proposed: Any, raw: RawAcquisition, *, strict: bool) -> dict[str, Any]:
        try:
            base = semantic_outputs(list(connector.parse(raw)))
        except Exception as e:  # a history vintage the reviewed parser itself rejects
            if strict:
                return {"error": f"reviewed parser failed on stored fixture ({type(e).__name__})"}
            return {"skipped": f"reviewed parser rejects this vintage ({type(e).__name__})"}
        try:
            prop = semantic_outputs(list(proposed.parse(raw)))
        except Exception as e:  # any failure of the proposal on history blocks it
            return {"error": f"proposed parser failed on a historical vintage ({type(e).__name__}: {str(e)[:120]})"}
        keys = sorted(set(base[0]) | set(prop[0]))
        changed = [k for k in keys if base[0].get(k) != prop[0].get(k)]
        if base[2] != prop[2]:
            changed.append("<expected_keys>")
        flag_changes = sum(1 for k in keys if base[1].get(k) != prop[1].get(k))
        digest = hashlib.sha256(json.dumps(base[0], sort_keys=True).encode()).hexdigest()
        return {"observations": len(base[0]), "semantic_digest": digest, "semantic_changes": len(changed),
                "examples": changed[:10], "flag_changes": flag_changes}


def _default_constructible(connector: Any) -> bool:
    try:
        type(connector)()
        return True
    except TypeError:
        return False


def _finding(code: str, severity: str, message: str) -> Finding:
    return Finding(code=code, severity=severity, message=message, evidence=())  # type: ignore[arg-type]


def _plan_by_fingerprint(connector: Any) -> dict[str, FetchItem]:
    plan = connector.plan(None, FetchBudget(max_requests=100_000, max_bytes=52_428_800))
    return {request_fingerprint("GET", i.url): i for i in plan.items}


def _plan_by_key(connector: Any) -> dict[str, FetchItem]:
    plan = connector.plan(None, FetchBudget(max_requests=100_000, max_bytes=52_428_800))
    return {i.key: i for i in plan.items}


def _raw_from_entry(io_: ArtifactIO, e: Mapping[str, Any], item: FetchItem) -> RawAcquisition:
    body = io_.get_bytes(ArtifactRef(e["body_sha256"], "raw", e["media_type"]))
    return RawAcquisition(item=item, body=body, media_type=e["media_type"],
                          retrieved_at=datetime.fromisoformat(e["retrieved_at"]),
                          response_metadata={"status": e["status"], "replay": "fixture_bank"},
                          request_fingerprint=e["request_fingerprint"], origin="test_fixture")


# --------------------------------------------------------------------------- advisor
class ConnectorRepairAdvisor:
    """Classifies connector drift against the immutable fixture bank and files repair proposals."""

    def __init__(self, engine: Engine, store: ArtifactStore, *, guard: AutonomyGuard | None = None,
                 fixture_root: Path = FIXTURE_ROOT, persistent_runs: int = PERSISTENT_RUNS,
                 author: str = AUTHOR, include_db_history: bool = True) -> None:
        if persistent_runs < 1:
            raise ValueError("persistent_runs must be >= 1")
        self.engine = engine
        self.store = store
        self.io = ArtifactIO(store)
        self.guard = guard
        self.fixture_root = fixture_root
        self.persistent_runs = persistent_runs
        self.author = author
        self.include_db_history = include_db_history
        self._banks: dict[str, ArtifactRef] = {}

    # ------------------------------------------------------------------ inputs
    def bank(self, connector_name: str) -> ArtifactRef:
        if connector_name not in self._banks:
            self._banks[connector_name] = bank_from_dir(self.io, connector_name, self.fixture_root)
        return self._banks[connector_name]

    def case_from_response(self, connector: Any, url: str, body: bytes, *, status: int = 200,
                           media_type: str = "application/json", observed_at: datetime | None = None,
                           origin: str = "live") -> DriftCase:
        item = _plan_by_fingerprint(connector).get(request_fingerprint("GET", url))
        if item is None:
            raise ValidationProblem("response URL matches no planned request of this connector")
        return DriftCase(connector=connector.describe().name, observed_at=observed_at or datetime.now(UTC),
                         origin=origin, item_key=item.key, body=body, media_type=media_type, status=status)

    def find_case(self, connector: Any, *, version: str | None = None, origin: str | None = None,
                  since: datetime | None = None, limit: int = 200) -> DriftCase | None:
        """The newest open case: a persistent request-failure history, else the newest acquisition
        that was quarantined or whose semantic fingerprint differs from the fixture bank."""
        name = connector.describe().name
        with self.engine.connect() as conn:
            runs = failure_history(conn, name, version=version, origin=origin, since=since)
        if runs and any(_is_gone(s) or _is_auth(s) for s in runs[-1].signals):
            return DriftCase(connector=name, observed_at=runs[-1].recorded_at, origin=origin or "live",
                             status=None, failures=runs)
        return self.find_drifted_acquisition(connector, version=version, origin=origin, since=since,
                                             limit=limit)

    def find_drifted_acquisition(self, connector: Any, *, version: str | None = None, origin: str | None = None,
                                 since: datetime | None = None, limit: int = 200) -> DriftCase | None:
        name = connector.describe().name
        sem = SEMANTICS.get(name)
        bank_by_fp = {e["request_fingerprint"]: e for e in bank_entries(self.io, self.bank(name))}
        plan = _plan_by_key(connector)
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                """SELECT a.id, a.raw_sha256, a.retrieved_at, a.origin, a.response_metadata,
                          EXISTS (SELECT 1 FROM meta.artifact_location l WHERE l.artifact_sha256 = a.raw_sha256
                                  AND l.namespace = 'quarantine') AS quarantined,
                          (SELECT media_type FROM meta.artifact m WHERE m.sha256 = a.raw_sha256) AS media_type
                     FROM evidence.acquisition a JOIN evidence.connector_version cv ON cv.id = a.connector_version_id
                    WHERE cv.name = :n AND (CAST(:v AS text) IS NULL OR cv.version = :v)
                      AND (CAST(:o AS text) IS NULL OR a.origin = :o)
                      AND (CAST(:t AS timestamptz) IS NULL OR a.recorded_at >= :t)
                    ORDER BY a.recorded_at DESC, a.id DESC LIMIT :l"""),
                {"n": name, "v": version, "o": origin, "t": since, "l": limit}).mappings().all()
        seen: set[str] = set()
        for r in rows:
            meta = dict(r["response_metadata"] or {})
            key = str(meta.get("item_key") or "")
            if key in seen or key not in plan:
                continue
            seen.add(key)
            ns = "quarantine" if r["quarantined"] else "raw"
            ref = ArtifactRef(r["raw_sha256"], ns, r["media_type"] or "application/json")
            body = self.io.get_bytes(ref)
            entry = bank_by_fp.get(request_fingerprint("GET", plan[key].url))
            drift = bool(r["quarantined"])
            if not drift and entry is not None:
                old_body = self.io.get_bytes(ArtifactRef(entry["body_sha256"], "raw", entry["media_type"]))
                try:
                    drift = (semantic_schema_fingerprint(load_exact(body), sem)
                             != semantic_schema_fingerprint(load_exact(old_body), sem))
                except ValueError:
                    drift = True
                if not drift and body != old_body:
                    drift = self._silently_rescaled(connector, plan[key], body, entry)
            if drift:
                findings = tuple(f for q in meta.get("quality") or [] for f in q.get("findings") or []
                                 if f.get("severity") == "blocker")
                return DriftCase(connector=name, observed_at=r["retrieved_at"], origin=r["origin"], item_key=key,
                                 body=body, media_type=ref.media_type, status=int(meta.get("status") or 200),
                                 acquisition_id=str(r["id"]), raw_ref=ref, quarantined=bool(r["quarantined"]),
                                 quarantine_findings=findings)
        return None

    def _silently_rescaled(self, connector: Any, item: FetchItem, body: bytes, entry: Mapping[str, Any]) -> bool:
        """Same schema, but a concept's shared values all moved by one power of ten (§10.3 unit drift)."""
        try:
            old = semantic_outputs(list(connector.parse(_raw_from_entry(self.io, entry, item))))[0]
            new_raw = RawAcquisition(item=item, body=body, media_type="application/json",
                                     retrieved_at=datetime.now(UTC), response_metadata={},
                                     request_fingerprint=entry["request_fingerprint"], origin="test_fixture")
            new = semantic_outputs(list(connector.parse(new_raw)))[0]
        except Exception as e:  # unparseable with the reviewed parser: that is drift too
            log.info("repair: acquisition of %s does not parse with the reviewed parser (%s)", item.key,
                     type(e).__name__)
            return True
        return bool(value_comparison(old, new)["rescaled_concepts"])

    def accepted_history(self, connector: Any, *, limit: int = 500) -> list[RawAcquisition]:
        """Accepted (non-quarantined) raw acquisitions of this connector, for replay."""
        name = connector.describe().name
        plan = _plan_by_key(connector)
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                """SELECT DISTINCT ON (a.raw_sha256) a.raw_sha256, a.retrieved_at, a.origin, a.request_fingerprint,
                          a.response_metadata->>'item_key' AS item_key, m.media_type
                     FROM evidence.acquisition a
                     JOIN evidence.connector_version cv ON cv.id = a.connector_version_id
                     JOIN meta.artifact m ON m.sha256 = a.raw_sha256
                    WHERE cv.name = :n AND EXISTS (SELECT 1 FROM meta.artifact_location l
                          WHERE l.artifact_sha256 = a.raw_sha256 AND l.namespace = 'raw')
                    ORDER BY a.raw_sha256, a.recorded_at DESC LIMIT :l"""),
                {"n": name, "l": limit}).mappings().all()
        out = []
        for r in rows:
            item = plan.get(r["item_key"] or "")
            if item is None:
                continue
            try:
                body = self.io.get_bytes(ArtifactRef(r["raw_sha256"], "raw", r["media_type"]))
            except Exception as e:  # unreadable history is reported by its absence
                log.warning("repair: accepted raw %s unreadable (%s)", r["raw_sha256"][:12], type(e).__name__)
                continue
            out.append(RawAcquisition(item=item, body=body, media_type=r["media_type"],
                                      retrieved_at=r["retrieved_at"], response_metadata={"replay": "accepted"},
                                      request_fingerprint=r["request_fingerprint"], origin=r["origin"]))
        return out

    # ------------------------------------------------------------------ assessment
    def assess(self, connector: Any, case: DriftCase) -> RepairAssessment:
        if self.guard is not None:
            self.guard.check("assess_connector_repair")
        spec = connector.describe()
        if case.connector != spec.name:
            raise ValidationProblem("case belongs to a different connector")
        bank = self.bank(spec.name)
        base = {"connector": spec.name, "connector_version": spec.version, "parser_version": spec.parser_version,
                "bank": bank, "case": case}
        if case.failures or case.body is None:
            return self._assess_failures(connector, case, base)
        return self._assess_payload(connector, case, base)

    def _assess_failures(self, connector: Any, case: DriftCase, base: dict[str, Any]) -> RepairAssessment:
        runs = case.failures
        if not runs:
            status = case.status
            sig = AttemptSignal(case.item_key or "*", status, "http_status" if status else "other", "")
            runs = (RunSignals(case.observed_at, 0.0, (sig,)),)
        latest = runs[-1]
        auth_items = sorted({s.item_key for s in latest.signals if _is_auth(s)})
        gone_items = sorted({s.item_key for s in latest.signals if _is_gone(s)})
        n_auth = _consecutive(runs, _is_auth, None)
        evidence: dict[str, Any] = {
            "runs_considered": len(runs), "persistence_threshold": self.persistent_runs,
            "latest_run": {"recorded_at": latest.recorded_at.isoformat(), "success_ratio": latest.success_ratio,
                           "signals": [{"item": s.item_key, "status": s.status, "kind": s.kind,
                                        "message": s.message[:200]} for s in latest.signals]}}
        changes: list[dict[str, Any]] = []
        if auth_items:
            evidence["consecutive_auth_failure_runs"] = n_auth
            changes.append({"kind": AUTH, "items": auth_items,
                            "statuses": sorted({s.status for s in latest.signals if _is_auth(s)})})
            cls = AUTH if n_auth >= self.persistent_runs else TRANSIENT
            item = None if (latest.success_ratio in (0, 0.0) or len(auth_items) > 1) else auth_items[0]
        elif gone_items:
            per_item = {i: _consecutive(runs, _is_gone, i) for i in gone_items}
            evidence["consecutive_gone_runs"] = per_item
            item = max(gone_items, key=lambda i: (per_item[i], i))
            changes.append({"kind": ENDPOINT, "items": gone_items,
                            "signals": sorted({s.kind if s.status is None else str(s.status)
                                               for s in latest.signals if _is_gone(s)})})
            cls = ENDPOINT if per_item[item] >= self.persistent_runs else TRANSIENT
        else:
            cls, item = TRANSIENT, case.item_key
            changes.append({"kind": TRANSIENT, "signals": [s.kind for s in latest.signals]})
        evidence["persistent"] = cls != TRANSIENT
        concepts = self._affected_concepts(connector, item)
        action, tier = ACTIONS[cls]
        return RepairAssessment(item_key=item, classification=cls, action=action, tier=tier,
                                changes=tuple(changes), affected_concepts=concepts, semantic_diff=None,
                                raw_ref=None, accepted_fingerprint=None, new_fingerprint=None,
                                suggested_mapping=None, evidence=evidence, **base)

    def _assess_payload(self, connector: Any, case: DriftCase, base: dict[str, Any]) -> RepairAssessment:
        spec = connector.describe()
        sem = SEMANTICS.get(spec.name)
        plan = _plan_by_key(connector)
        item = plan.get(case.item_key or "")
        if item is None:
            raise ValidationProblem("drift case names no planned request of this connector",
                                    item=str(case.item_key)[:120])
        raw_ref = case.raw_ref or self.io.put_bytes(case.body or b"", "quarantine", case.media_type)
        new_raw = RawAcquisition(item=item, body=case.body or b"", media_type=case.media_type,
                                 retrieved_at=case.observed_at, response_metadata={"status": case.status},
                                 request_fingerprint=request_fingerprint("GET", item.url), origin="test_fixture")
        entries = bank_entries(self.io, base["bank"])
        by_fp = {e["request_fingerprint"]: e for e in entries}
        plan_fp = {request_fingerprint("GET", i.url): i for i in plan.values()}
        group = _group_key(item, sem)
        group_objs: list[Any] = []
        same: Any = None
        same_entry = by_fp.get(request_fingerprint("GET", item.url))
        for e in entries:
            it = plan_fp.get(e["request_fingerprint"])
            if it is None or _group_key(it, sem) != group or int(e.get("status", 200)) != 200:
                continue
            obj = load_exact(self.io.get_bytes(ArtifactRef(e["body_sha256"], "raw", e["media_type"])))
            group_objs.append(obj)
            if e is same_entry:
                same = obj
        evidence: dict[str, Any] = {"group": list(group), "group_vintages": len(group_objs),
                                    "same_request_vintage": same_entry["body_sha256"] if same_entry else None,
                                    "quarantined": case.quarantined,
                                    "quarantine_findings": [dict(f) for f in case.quarantine_findings]}
        try:
            new_obj = load_exact(case.body or b"")
        except ValueError as e:
            evidence["decode_error"] = str(e)[:200]
            return self._finish(connector, case, base, item.key, CONTENT,
                                [{"kind": CONTENT, "detail": "response is not JSON"}], evidence, raw_ref,
                                None, None, None)
        changes = diff_payload(new_obj, group_objs, same, sem)
        # Parse with the reviewed parser (unchanged mapping) to measure semantic effects.
        baseline_sem: dict[str, str] | None = None
        if same_entry is not None:
            baseline_sem = semantic_outputs(list(connector.parse(_raw_from_entry(self.io, same_entry, item))))[0]
        new_sem: dict[str, str] | None = None
        new_blockers: list[str] = []
        try:
            batches = list(connector.parse(new_raw))
            new_sem = semantic_outputs(batches)[0]
            validator = type(connector)() if _default_constructible(connector) else connector
            new_blockers = [f.code for b in batches for f in validator.validate(b).findings
                            if f.severity == "blocker"]
        except RevaError as e:
            new_blockers = [str(e.details.get("kind", e.code))]
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, ArithmeticError) as e:
            new_blockers = [f"row_schema_drift:{type(e).__name__}"]
        evidence["unchanged_mapping"] = {"parses": new_sem is not None, "validation_blockers": new_blockers}
        if baseline_sem is not None and new_sem is not None:
            cmp = value_comparison(baseline_sem, new_sem)
            evidence["values_vs_same_request_vintage"] = cmp
            if cmp["uniform_power_of_ten"]:
                changes.append({"kind": SCALE, "paths": list(sem.value_paths) if sem else [],
                                "factor_new_over_old": cmp["factor_new_over_old"],
                                "rescaled_concepts": cmp["rescaled_concepts"], "consumed": True,
                                "evidence": f"{cmp['revised']} of {cmp['shared_keys']} shared values rescaled"})
        kinds = {c["kind"] for c in changes}
        additive_ok = False
        if kinds and kinds <= {"added_field", "populated_unused_field"}:
            strip = self._strip_check(connector, new_raw, new_obj, changes)
            evidence["unused_proof"] = strip
            additive_ok = strip["identical"] and not new_blockers and sem is not None
        cls = _primary_class(changes, sem, additive_ok, new_blockers)
        suggested = suggest_mapping(spec.parser_version, cls, changes, sem)
        return self._finish(connector, case, base, item.key, cls, changes, evidence, raw_ref, same, new_obj,
                            suggested)

    def _strip_check(self, connector: Any, new_raw: RawAcquisition, new_obj: Any,
                     changes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Remove added fields (null previously-null ones) and require identical outputs."""
        stripped = load_exact(dumps_exact(new_obj))  # exact deep copy
        for c in changes:
            recs, key = _records(stripped, c["path"])
            for r in recs:
                if c["kind"] == "added_field":
                    r.pop(key, None)
                elif key in r:
                    r[key] = None
        try:
            a = semantic_outputs(list(connector.parse(new_raw)))
            b = semantic_outputs(list(connector.parse(new_raw.model_copy(update={"body": dumps_exact(stripped)}))))
        except Exception as e:  # the stripped payload must parse like the original
            return {"identical": False, "error": type(e).__name__}
        return {"identical": a[0] == b[0] and a[2] == b[2], "flags_identical": a[1] == b[1],
                "observations": len(a[0]), "paths": [c["path"] for c in changes]}

    def _finish(self, connector: Any, case: DriftCase, base: dict[str, Any], item_key: str, cls: str,
                changes: list[dict[str, Any]], evidence: dict[str, Any], raw_ref: ArtifactRef,
                same: Any, new_obj: Any, suggested: MappingRevision | None) -> RepairAssessment:
        sem = SEMANTICS.get(base["connector"])
        accepted_fp = semantic_schema_fingerprint(same, sem) if same is not None else None
        new_fp = semantic_schema_fingerprint(new_obj, sem) if new_obj is not None else None
        concepts = self._affected_concepts(connector, item_key)
        action, tier = ACTIONS[cls]
        diff = {"schema": DIFF_SCHEMA, "connector": base["connector"], "connector_version": base["connector_version"],
                "parser_version": base["parser_version"], "item_key": item_key, "classification": cls,
                "action": action, "tier": tier, "fixture_bank": base["bank"].sha256, "drifted_raw": raw_ref.sha256,
                "acquisition_id": case.acquisition_id, "observed_at": case.observed_at.isoformat(),
                "fingerprints": {"accepted": accepted_fp, "new": new_fp, "schema": FINGERPRINT_SCHEMA},
                "changes": changes, "affected_concepts": list(concepts), "evidence": evidence,
                "suggested_mapping": suggested.as_json() if suggested else None,
                "semantics_declared": sem is not None,
                "rule": "§10.3: additive unused fields may auto-pass; rename/unit/meaning/classification changes "
                        "require a new mapping version; history is replayed and must not change"}
        diff_ref = self.io.put_json(diff, "evaluation")
        return RepairAssessment(item_key=item_key, classification=cls, action=action, tier=tier,
                                changes=tuple(changes), affected_concepts=concepts, semantic_diff=diff_ref,
                                raw_ref=raw_ref, accepted_fingerprint=accepted_fp, new_fingerprint=new_fp,
                                suggested_mapping=suggested, evidence=evidence, **base)

    def _affected_concepts(self, connector: Any, item_key: str | None) -> tuple[str, ...]:
        """Concepts a request feeds, from replaying its stored vintage (dependents for metadata items)."""
        plan = _plan_by_key(connector)
        keys = [item_key] if item_key else list(plan)
        if item_key:
            keys += [k for k, it in plan.items() if it.params.get("depends_on") == item_key]
        by_fp = {e["request_fingerprint"]: e for e in bank_entries(self.io, self.bank(connector.describe().name))}
        concepts: set[str] = set()
        for k in keys:
            it = plan.get(k)
            e = by_fp.get(request_fingerprint("GET", it.url)) if it else None
            if it is None or e is None:
                continue
            try:
                for b in connector.parse(_raw_from_entry(self.io, e, it)):
                    concepts.update(o.concept_id for o in b.observations)
            except Exception as e:  # fall back to declared mappings below
                log.warning("repair: stored vintage of %s does not parse (%s)", k, type(e).__name__)
                continue
        if not concepts:
            concepts = {str(v) for v in connector.describe().field_mappings.values()}
        return tuple(sorted(concepts))

    # ------------------------------------------------------------------ proposals
    def propose(self, connector: Any, assessment: RepairAssessment, *,
                mapping: MappingRevision | None = None, parser: Any = None,
                fence: Any = None) -> RepairProposal | None:
        """Persist the repair proposal (ops.proposal + manifest), run independent checks, pause publication.

        Returns ``None`` for transient failures / no schema change. Gate results are
        *not* written here: pass ``result.proposal`` and ``result.reports`` to a
        :class:`~reva.autonomy.gate.PolicyPromotionGate`.
        """
        if assessment.tier is None:
            return None
        if self.guard is not None:
            self.guard.check("propose")
        if mapping is not None and parser is not None:
            raise ValidationProblem("give either a declarative mapping revision or a parser, not both")
        if (mapping is not None or parser is not None) and assessment.tier != 2:
            raise ValidationProblem("a new mapping/parser only belongs to a T2 repair proposal",
                                    classification=assessment.classification)
        spec = connector.describe()
        mapping = mapping if parser is None else None
        if mapping is None and parser is None and assessment.tier == 2:
            mapping = assessment.suggested_mapping
        proposed = parser if parser is not None else (MappedParser(connector, mapping) if mapping else None)
        reports: list[ValidationReport] = []
        extra: dict[str, Any] = {}
        if assessment.classification == ADDITIVE:
            reports.append(self._revalidation_report(connector, assessment))
        if proposed is not None:
            history = self.accepted_history(connector) if self.include_db_history else []
            new_raw = self._new_raw(connector, assessment)
            baseline_new = self._baseline_same_request(connector, assessment)
            reports.append(FixtureReplayChecker(self.io).check(
                connector, proposed, assessment.bank, proposal_key=f"{spec.name}:{assessment.item_key}",
                history=history, new_raw=new_raw, baseline_new=baseline_new))
        manifest = self._manifest(connector, assessment, proposed, reports)
        ref = self.io.put_json(manifest, "models")
        tier = int(assessment.tier)
        kind = manifest["action"]
        changed_paths: tuple[str, ...]
        caps: tuple[str, ...]
        if kind == "reenable_unchanged_mapping":
            changed_paths = (f"connectors/{spec.name}/{spec.version}/accepted_schema/"
                             f"{(assessment.new_fingerprint or 'none')[:16]}.json",)
            caps, scientific = ("concept_eligibility",), False
        elif kind in ("new_mapping_version", "review_content_anomaly"):
            ver = manifest["proposed_mapping_version"] or f"{spec.parser_version}+map.pending"
            changed_paths = (f"registry/mappings/{spec.name}/{ver}.json",)
            caps, scientific = ("ontology_mapping", "new_parser"), True
            if parser is not None:
                caps = (*caps, "execute_code")
        elif kind == "documentation_discovery":
            changed_paths, caps, scientific = (), ("discover", "report"), False
        else:  # credential provisioning
            changed_paths, caps, scientific = (), ("secret_access",), False
        proposal = PlatformChangeProposal(
            proposal_id=stable_id("connector-repair", ref.sha256), author=self.author,
            permission_tier=tier,  # type: ignore[arg-type]
            changed_paths=changed_paths, base_commit=base_commit(), patch_or_manifest=ref,
            requested_capabilities=caps, scientific_change=scientific, migration_required=False,
            expected_benefit=self._value(assessment), policy_version=promotion_policy().version)
        with self.engine.begin() as conn:
            for r in (assessment.semantic_diff, assessment.raw_ref):
                if r is not None:
                    self.io.register(conn, r, "connector-semantic-diff/1" if r is assessment.semantic_diff
                                     else f"{spec.name}/{spec.parser_version}/raw")
            for rep in reports:
                self.io.register(conn, rep.evidence_manifest, "connector-repair-check/1")
        proposal_type = f"connector_repair:{kind}"
        record = ProposalStore(self.engine, self.io, self.guard).persist(
            proposal, proposal_type=proposal_type,
            scope={"connector": spec.name, "connector_version": spec.version, "item": assessment.item_key,
                   "concepts": list(assessment.affected_concepts)},
            risk={"tier": f"T{tier}", "classification": assessment.classification,
                  "semantic_change": scientific, "history": "replay-checked" if proposed is not None else "n/a"},
            dependencies=[f"fixture_bank:{assessment.bank.sha256}"]
            + ([f"semantic_diff:{assessment.semantic_diff.sha256}"] if assessment.semantic_diff else [])
            + ([f"acquisition:{assessment.case.acquisition_id}"] if assessment.case.acquisition_id else []),
            summary={"classification": assessment.classification, "action": kind,
                     "reports": [{"evaluator": r.evaluator_version, "outcome": r.outcome} for r in reports]})
        pause = None
        if assessment.schema_failure:
            pause = self.pause_publication(assessment, proposal.proposal_id, fence=fence)
        if kind == "documentation_discovery":
            extra["documentation_sources"] = manifest["task"]["allowed_documentation"]
        return RepairProposal(assessment, proposal, proposal_type, ref, tuple(reports), pause, extra, record)

    def _new_raw(self, connector: Any, a: RepairAssessment) -> RawAcquisition | None:
        if a.case.body is None or a.item_key is None:
            return None
        item = _plan_by_key(connector)[a.item_key]
        return RawAcquisition(item=item, body=a.case.body, media_type=a.case.media_type,
                              retrieved_at=a.case.observed_at, response_metadata={"status": a.case.status},
                              request_fingerprint=request_fingerprint("GET", item.url), origin="test_fixture")

    def _baseline_same_request(self, connector: Any, a: RepairAssessment) -> dict[str, str] | None:
        if a.item_key is None:
            return None
        item = _plan_by_key(connector)[a.item_key]
        e = {x["request_fingerprint"]: x for x in bank_entries(self.io, a.bank)}.get(
            request_fingerprint("GET", item.url))
        return semantic_outputs(list(connector.parse(_raw_from_entry(self.io, e, item))))[0] if e else None

    def _revalidation_report(self, connector: Any, a: RepairAssessment) -> ValidationReport:
        """Re-run parse + validate with the unchanged mapping and replay the bank (identity)."""
        findings: list[Finding] = []
        raw = self._new_raw(connector, a)
        detail: dict[str, Any] = {}
        if raw is None:
            findings.append(_finding("no_payload", "blocker", "no drifted payload to re-validate"))
        else:
            validator = type(connector)() if _default_constructible(connector) else connector
            try:
                batches = list(connector.parse(raw))
                q = [validator.validate(b) for b in batches]
                for rep in q:
                    findings.extend(_finding(f.code, f.severity, f.message) for f in rep.findings)
                detail = {"observations": sum(len(b.observations) for b in batches),
                          "quality": [r.model_dump(mode="json") for r in q]}
            except RevaError as e:
                findings.append(_finding("parse_failed", "blocker", e.message))
            if not (a.evidence.get("unused_proof") or {}).get("identical"):
                findings.append(_finding("added_fields_not_proven_unused", "blocker",
                                         "removing the added fields changes semantic outputs"))
        replay = FixtureReplayChecker(self.io)._compare  # identity replay: bank must still parse
        bank_ok = all(not replay(connector, connector, _raw_from_entry(self.io, e, it), strict=True).get("error")
                      for e in bank_entries(self.io, a.bank)
                      if (it := _plan_by_fingerprint(connector).get(e["request_fingerprint"])) is not None)
        if not bank_ok:
            findings.append(_finding("bank_replay_failed", "blocker", "stored fixtures no longer parse"))
        outcome = "fail" if any(f.severity == "blocker" for f in findings) else "pass"
        ev = self.io.put_json({"schema": "reva-connector-revalidation/1", "evaluator": REVALIDATION_EVALUATOR,
                               "connector": a.connector, "parser_version": a.parser_version, "item": a.item_key,
                               "drifted_raw": a.raw_ref.sha256 if a.raw_ref else None,
                               "fixture_bank": a.bank.sha256, "mapping": "unchanged", "outcome": outcome,
                               "findings": [{"code": f.code, "severity": f.severity, "message": f.message}
                                            for f in findings], "detail": detail}, "evaluation")
        return report(outcome, findings, ev, REVALIDATION_EVALUATOR)

    def _manifest(self, connector: Any, a: RepairAssessment, proposed: Any,
                  reports: Sequence[ValidationReport]) -> dict[str, Any]:
        spec = connector.describe()
        m: dict[str, Any] = {
            "schema": REPAIR_SCHEMA, "action": a.action, "classification": a.classification, "tier": a.tier,
            "connector": {"name": spec.name, "version": spec.version, "template": spec.template,
                          "code_digest": spec.code_digest, "parser_version": spec.parser_version},
            "item_key": a.item_key, "affected_concepts": list(a.affected_concepts),
            "fixture_bank": a.bank.sha256, "semantic_diff": a.semantic_diff.sha256 if a.semantic_diff else None,
            "drifted_raw": a.raw_ref.sha256 if a.raw_ref else None, "acquisition_id": a.case.acquisition_id,
            "fingerprints": {"accepted": a.accepted_fingerprint, "new": a.new_fingerprint},
            "changes": [dict(c) for c in a.changes],
            "independent_checks": [{"evaluator": r.evaluator_version, "outcome": r.outcome,
                                    "evidence": r.evidence_manifest.sha256} for r in reports],
            "publication": {"paused_concepts": list(a.affected_concepts) if a.schema_failure else [],
                            "resume": "only after a promote decision by a gate evaluator or a named human review"},
            "proposed_mapping_version": None, "proposed_parser": None,
        }
        if a.action == "reenable_unchanged_mapping":
            m["mapping"] = {"version": spec.parser_version, "change": "none",
                            "accept_schema_fingerprint": a.new_fingerprint}
        elif a.action in ("new_mapping_version", "review_content_anomaly"):
            if isinstance(proposed, MappedParser):
                m["proposed_mapping_version"] = proposed.revision.version
            m["proposed_parser"] = parser_identity(proposed) if proposed is not None else None
            m["mapping"] = {"base_version": spec.parser_version, "requires_new_version": True,
                            "status": "proposed" if proposed is not None else
                            "to be authored in review (no transform is inferable from the evidence)"}
        elif a.action == "documentation_discovery":
            m["task"] = self._documentation_task(spec, a)
        elif a.action == "credential_provisioning_required":
            m["task"] = {"kind": "credential_provisioning", "secret_ref": spec.authentication_secret_ref,
                         "statuses": sorted({s for c in a.changes for s in c.get("statuses", [])}),
                         "bypass": "forbidden: no anonymous fallback, no alternative host, no credential reuse",
                         "connector": "remains quarantined until a maintainer provisions a credential "
                                      "(registry secret reference) and a new connector version is reviewed",
                         "evidence": a.evidence}
        return m

    def _documentation_task(self, spec: Any, a: RepairAssessment) -> dict[str, Any]:
        from reva.ontology.registry import load_registry

        src = next((s for s in load_registry().sources if s.get("source_key") == spec.source_key), {})
        md = src.get("metadata") or {}
        docs = sorted({str(v) for k, v in md.items() if k in ("api_docs", "documentation") and v}
                      | ({str(src["provider_url"])} if src.get("provider_url") else set()))
        return {"kind": "documentation_discovery", "source_key": spec.source_key,
                "allowed_documentation": docs,
                "failing_items": [c.get("items") for c in a.changes if c.get("kind") == ENDPOINT],
                "method": "read the provider's registered documentation/catalogue only; a replacement endpoint "
                          "becomes a new T2 connector-version proposal validated against the fixture bank",
                "forbidden": ["random_url_guessing", "endpoint_enumeration", "hosts_outside_allowlist"],
                "evidence": a.evidence}

    def _value(self, a: RepairAssessment) -> ValueEstimate:
        ref = self.io.put_json({"reason": "a repair restores an existing feed; benefit is not quantified",
                                "connector": a.connector, "classification": a.classification}, "evaluation")
        return ValueEstimate("unsupported", None, None, {"status": "unquantified", "kind": "unknown",
                                                         "description": "repair benefit not quantified"},
                             ref, "autonomy.repair@1")

    # ------------------------------------------------------------------ publication pause
    def pause_publication(self, a: RepairAssessment, proposal_id: UUID, *, fence: Any = None) -> PublicationPause:
        with self.engine.begin() as conn:
            if fence is not None:
                fence(conn)
            return pause_belief_publication(
                conn, a.affected_concepts, connector=a.connector, classification=a.classification,
                proposal_id=proposal_id, origin=a.case.origin, item_key=a.item_key,
                acquisition_id=a.case.acquisition_id, actor=self.author)


def _group_key(item: FetchItem, sem: SchemaSemantics | None) -> tuple[str, ...]:
    params = sem.group_params if sem else ()
    return tuple(str(item.params.get(p, "")) for p in params) or ("*",)


# --------------------------------------------------------------------------- diff
def diff_payload(new_obj: Any, group_objs: Sequence[Any], same: Any, sem: SchemaSemantics | None) -> list[
        dict[str, Any]]:
    """Structural/semantic changes of ``new_obj`` versus the stored vintages of the same request kind."""
    consumed = sem.consumed if sem else frozenset()
    new_prof = schema_profile(new_obj)
    profs = [schema_profile(o) for o in group_objs]
    union: dict[str, set[str]] = defaultdict(set)
    for prof in profs:
        for k, t in prof.items():
            union[k].update(t)
    required = set()
    for k in union:
        toks = parse_path(k)
        if not toks or toks[-1][0] != "key":
            continue
        parent = _render(toks[:-1])
        with_parent = [prof for prof in profs if parent in prof]
        if with_parent and all(k in prof for prof in with_parent):
            required.add(k)
    changes: list[dict[str, Any]] = []
    added = [p for p in new_prof if p not in union and not _under_any(p, [q for q in new_prof
                                                                          if q not in union and q != p])]
    removed = [p for p in sorted(required) if p not in new_prof and _parent(p) in new_prof
               and not _under_any(p, [q for q in required if q not in new_prof and q != p])]
    same_prof = schema_profile(same) if same is not None else {}
    # renames: a removed path paired with an added sibling of compatible type and matching values
    renamed: set[str] = set()
    used_new: set[str] = set()
    for old in removed:
        old_types = set(union[old]) - {"null"}
        cands = [a for a in added if a not in used_new and _parent(a) == _parent(old)
                 and (not old_types or (set(new_prof[a]) - {"null"}) <= old_types)]
        if not cands:
            continue
        old_vals = {_scalar_text(v) for v in values_at(same if same is not None else group_objs[0], old)
                    if v is not None} if (same is not None or group_objs) else set()
        scored = []
        for c in cands:
            new_vals = {_scalar_text(v) for v in values_at(new_obj, c) if v is not None}
            union_vals = old_vals | new_vals
            scored.append(((len(old_vals & new_vals) / len(union_vals)) if union_vals else 0.0, c))
        scored.sort(reverse=True)
        best_score, best = scored[0]
        if best_score >= 0.5 or (len(cands) == 1 and len(removed) == 1):
            renamed.add(old)
            used_new.add(best)
            changes.append({"kind": RENAME, "path": old, "new_path": best, "consumed": old in consumed,
                            "types": sorted(new_prof[best]),
                            "confidence": "values_match" if best_score >= 0.5 else "structure_only",
                            "value_overlap": round(best_score, 4)})
    for p in added:
        if p in used_new:
            continue
        changes.append({"kind": "added_field" if p not in consumed else STRUCTURE, "path": p,
                        "types": new_prof[p], "consumed": p in consumed,
                        **({"detail": "a path the reviewed parser reads appeared"} if p in consumed else {})})
    for p in removed:
        if p in renamed:
            continue
        changes.append({"kind": REMOVED if p in consumed else STRUCTURE, "path": p, "consumed": p in consumed,
                        "old_types": sorted(union[p]),
                        **({} if p in consumed else {"detail": "an unused field disappeared"})})
    for p, types in new_prof.items():
        if p not in union:
            continue
        old_nn = set(union[p]) - {"null"}
        new_nn = set(types) - {"null"}
        if not new_nn or new_nn <= old_nn:
            continue
        if not old_nn:
            changes.append({"kind": "populated_unused_field" if p not in consumed else TYPE_CHANGE, "path": p,
                            "old_types": sorted(union[p]), "new_types": list(types), "consumed": p in consumed})
        elif {"object", "array"} & (new_nn | old_nn) and p not in consumed:
            changes.append({"kind": STRUCTURE, "path": p, "old_types": sorted(union[p]), "new_types": list(types),
                            "consumed": False})
        else:
            changes.append({"kind": TYPE_CHANGE if p in consumed else STRUCTURE, "path": p,
                            "old_types": sorted(union[p]), "new_types": list(types), "consumed": p in consumed})
    if sem is not None:
        for kind, paths in ((UNIT, sem.units), (CLASSIFICATION, sem.classifications), (MEANING, sem.meanings)):
            for p in paths:
                new_vals = {_scalar_text(v) for v in values_at(new_obj, p) if v is not None}
                if not new_vals:
                    continue
                if same is not None and same_prof.get(p) and set(same_prof[p]) - {"null"}:
                    base_vals = {_scalar_text(v) for v in values_at(same, p) if v is not None}
                    basis = "same_request_vintage"
                else:
                    base_vals = {_scalar_text(v) for o in group_objs for v in values_at(o, p) if v is not None}
                    basis = "request_kind_vocabulary"
                if not base_vals or new_vals <= base_vals:
                    continue
                changes.append({"kind": kind, "path": p, "old_values": _clip(sorted(base_vals)),
                                "new_values": _clip(sorted(new_vals - base_vals)), "basis": basis,
                                "consumed": p in consumed})
    return changes


def _clip(vals: list[str], n: int = 10, width: int = 300) -> list[str]:
    return [v if len(v) <= width else v[:width] + "…" for v in vals[:n]]


def _under_any(p: str, others: Sequence[str]) -> bool:
    """True when ``p`` lies below another path of the same set (report only the top-most change)."""
    return any(p != o and p.startswith(o) and p[len(o)] in ".[" for o in others if len(p) > len(o))


def _primary_class(changes: Sequence[Mapping[str, Any]], sem: SchemaSemantics | None, additive_ok: bool,
                   new_blockers: Sequence[str]) -> str:
    kinds = {c["kind"] for c in changes}
    for k in PAYLOAD_PRIORITY:
        if k in kinds:
            return k
    if kinds and kinds <= {"added_field", "populated_unused_field"}:
        return ADDITIVE if additive_ok else (STRUCTURE if sem is None or not new_blockers else CONTENT)
    if new_blockers:
        return CONTENT
    return NO_CHANGE


def suggest_mapping(parser_version: str, cls: str, changes: Sequence[Mapping[str, Any]],
                    sem: SchemaSemantics | None) -> MappingRevision | None:
    """Infer a declarative mapping revision only where the evidence determines it."""
    if cls not in (RENAME, TYPE_CHANGE, UNIT, SCALE):
        return None
    renames = tuple((c["new_path"], c["path"]) for c in changes if c["kind"] == RENAME)
    coercions = tuple(c["path"] for c in changes if c["kind"] == TYPE_CHANGE and set(c.get("old_types", [])) - {
        "null"} == {"number"} and "string" in c.get("new_types", []))
    scales: tuple[ScaleRule, ...] = ()
    scale = next((c for c in changes if c["kind"] == SCALE), None)
    units = [c for c in changes if c["kind"] == UNIT]
    if scale is not None:
        # A rescaling is only invertible when one unit marker changed to one new value
        # (the condition) and the item has a single value field; otherwise review decides.
        # The matched marker is restored to the single accepted unit value (normalize_to);
        # with several accepted values the target unit is ambiguous and review decides.
        if not (sem and len(sem.value_paths) == 1 and len(units) == 1 and len(units[0]["new_values"]) == 1
                and len(units[0].get("old_values") or []) == 1 and scale["factor_new_over_old"]):
            return None
        factor = Decimal(1) / Decimal(scale["factor_new_over_old"])
        scales = (ScaleRule(sem.value_paths[0], format(factor.normalize(), "f"), units[0]["path"],
                            units[0]["new_values"][0], units[0]["old_values"][0]),)
    elif units:
        return None  # unit text changed without measurable rescaling: meaning must be reviewed
    if any(c["kind"] in (MEANING, CLASSIFICATION, REMOVED, STRUCTURE) for c in changes):
        return None
    if not (renames or coercions or scales):
        return None
    return MappingRevision(parser_version, renames, scales, coercions,
                           note=f"inferred from {cls} evidence; replay-checked before review")


# --------------------------------------------------------------------------- publication markers
def last_accepted_release(conn: Connection, origin: str, concept_id: str | None = None) -> dict[str, Any] | None:
    row = conn.execute(text(
        """SELECT r.id, p.sequence, r.knowledge_cutoff, p.published_at FROM belief.release r
             JOIN ops.publication p ON p.release_id = r.id
            WHERE r.origin = :o AND (CAST(:c AS text) IS NULL OR EXISTS (
                  SELECT 1 FROM belief.estimate e JOIN ontology.concept_version cv ON cv.id = e.concept_version_id
                   WHERE e.release_id = r.id AND cv.concept_id = :c))
            ORDER BY p.sequence DESC LIMIT 1"""), {"o": origin, "c": concept_id}).mappings().one_or_none()
    if row is None:
        return None
    return {"release_id": str(row["id"]), "sequence": int(row["sequence"]),
            "knowledge_cutoff": row["knowledge_cutoff"].isoformat(), "published_at": row["published_at"].isoformat(),
            "readable": True, "stale": True}


def pause_belief_publication(conn: Connection, concepts: Sequence[str], *, connector: str, classification: str,
                             proposal_id: UUID, origin: str, item_key: str | None, acquisition_id: str | None,
                             actor: str) -> PublicationPause:
    """Mark concepts as paused (``publication_pause:belief:<concept>``) and announce the stale release.

    Takes the publication advisory lock first, so a pause either commits before a
    concurrent release reads the markers (the release withholds the concepts) or
    waits until that release has committed (and then names it as the last
    accepted release) -- never in between.
    """
    repo.take_publication_lock(conn)
    seq = int(conn.execute(text("SELECT COALESCE(max(id), 0) FROM ops.outbox")).scalar_one())
    reason = f"schema_quarantine:{connector}:{classification}"[:200]
    keys = []
    releases = {}
    for c in sorted(set(concepts)):
        key = f"{PAUSE_PREFIX}{c}"
        keys.append(key)
        conn.execute(text(
            """INSERT INTO ops.dirty_node(node_key, since_sequence, accumulated_error_bound, reason)
               VALUES (:k, :s, CAST(:b AS jsonb), :r)
               ON CONFLICT (node_key) DO UPDATE SET reason = EXCLUDED.reason, updated_at = clock_timestamp(),
                 accumulated_error_bound = EXCLUDED.accumulated_error_bound"""),
            {"k": key, "s": seq, "r": reason,
             "b": json.dumps({"kind": "unbounded", "basis": "source_schema_quarantine",
                              "proposal_id": str(proposal_id)})})
        releases[c] = last_accepted_release(conn, origin, c)
    last = last_accepted_release(conn, origin)
    payload = {"connector": connector, "classification": classification, "item": item_key,
               "proposal_id": str(proposal_id), "acquisition_id": acquisition_id, "origin": origin,
               "concepts": sorted(set(concepts)), "node_keys": keys, "last_accepted_release": last,
               "last_release_per_concept": releases,
               "visibility": "last accepted release remains readable and must be shown as stale for these concepts"}
    repo.emit_outbox(conn, "connector.publication_paused", payload)
    repo.audit(conn, actor, "connector.publication_paused", {"connector": connector, "concepts": payload["concepts"]},
               {"classification": classification, "proposal_id": str(proposal_id)})
    return PublicationPause(tuple(keys), tuple(sorted(set(concepts))), reason, last)


PAUSE_WITHHOLD_REASON = "source_schema_quarantine"


def active_publication_pauses(conn: Connection) -> list[dict[str, Any]]:
    """Active pause markers (readable by every runtime role: ops.dirty_node is granted to reva_api)."""
    rows = conn.execute(text(
        "SELECT node_key, reason, accumulated_error_bound, updated_at FROM ops.dirty_node "
        "WHERE node_key LIKE :p ORDER BY node_key"), {"p": PAUSE_PREFIX + "%"}).mappings().all()
    return [{"concept": r["node_key"][len(PAUSE_PREFIX):], "reason": r["reason"],
             "basis": (r["accumulated_error_bound"] or {}).get("basis", PAUSE_WITHHOLD_REASON),
             "proposal_id": (r["accumulated_error_bound"] or {}).get("proposal_id"),
             "since": r["updated_at"].isoformat()} for r in rows]


def is_publication_paused(conn: Connection, concept_id: str) -> bool:
    return conn.execute(text("SELECT 1 FROM ops.dirty_node WHERE node_key = :k"),
                        {"k": PAUSE_PREFIX + concept_id}).scalar_one_or_none() is not None


RESUME_EXECUTOR = "worker:publication-resume"


def resume_belief_publication(engine: Engine, concept_id: str, *, actor: str, reason: str,
                              gate_result_id: UUID | None = None, review_id: UUID | None = None) -> bool:
    """Lift a pause. Only a gate evaluator (with its promote decision) or a named human may do so.

    A human resume is never asserted by the worker: it must cite the human's recorded
    review (``review_id``: an ``approve`` row in ``ops.proposal_review``, written only
    through ``ops.review_proposal`` by the API/admin principal) of the proposal that
    paused this concept, by that same reviewer. The audit row is attributed to the
    executing worker (``worker:publication-resume``) and names the reviewer and review.
    """
    if not actor.startswith(("gate-evaluator:", "human:")):
        raise AutonomyBlocked("only a gate evaluator or a named human can resume belief publication")
    with engine.begin() as conn:
        marker = conn.execute(text("SELECT accumulated_error_bound FROM ops.dirty_node WHERE node_key = :k FOR UPDATE"),
                              {"k": PAUSE_PREFIX + concept_id}).scalar_one_or_none()
        pause_proposal = (marker or {}).get("proposal_id")
        evidence: dict[str, Any] = {"reason": reason[:500]}
        if actor.startswith("gate-evaluator:"):
            if gate_result_id is None:
                raise AutonomyBlocked("a gate evaluator resumes publication only with its promote decision")
            ok = conn.execute(text("SELECT decision = 'pass' AND evaluator = :a FROM ops.gate_result WHERE id = :g"),
                              {"g": gate_result_id, "a": actor}).scalar_one_or_none()
            if not ok:
                raise AutonomyBlocked("gate result is not a promote decision by this evaluator")
            audit_actor = actor
            evidence["gate_result_id"] = str(gate_result_id)
        else:
            if review_id is None:
                raise AutonomyBlocked("a human resume must cite the reviewer's recorded approval (review_id)")
            rv = conn.execute(text("SELECT proposal_id, decision, reviewer FROM ops.proposal_review WHERE id = :r"),
                              {"r": review_id}).mappings().one_or_none()
            if rv is None or rv["decision"] != "approve" or rv["reviewer"] != actor:
                raise AutonomyBlocked("review is not an approval recorded by this reviewer")
            if marker is not None and str(rv["proposal_id"]) != str(pause_proposal):
                raise AutonomyBlocked("the review approves a different proposal than the one that paused publication")
            audit_actor = RESUME_EXECUTOR
            evidence.update({"reviewer": actor, "review_id": str(review_id)})
        n = conn.execute(text("DELETE FROM ops.dirty_node WHERE node_key = :k"),
                         {"k": PAUSE_PREFIX + concept_id}).rowcount
        if n:
            repo.emit_outbox(conn, "connector.publication_resumed",
                             {"concept": concept_id, "actor": actor, "reason": reason[:500],
                              "proposal_id": pause_proposal,
                              "gate_result_id": str(gate_result_id) if gate_result_id else None,
                              "review_id": str(review_id) if review_id else None})
            repo.audit(conn, audit_actor, "connector.publication_resumed", {"concept": concept_id}, evidence)
    return bool(n)


# --------------------------------------------------------------------------- job handler
def handle_connector_repair(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Job ``connector_repair``: ``{"connector": name, "origin": "live"|"test_fixture"}``.

    Finds the newest open case, classifies it, files the proposal and lets the
    promotion gate decide. Nothing in the payload can choose hosts, fixtures, tiers or policies.
    """
    from reva.adapters.connectors.registry import builtin_connectors
    from reva.autonomy.gate import PolicyPromotionGate

    guard = AutonomyGuard.from_settings(ctx.settings)
    reason = guard.blocked_reason()
    if reason:
        return {"status": "blocked", "reason": reason}
    connectors = builtin_connectors(getattr(ctx.settings, "reva_http_contact_email", None))
    name = str(payload.get("connector", ""))
    origin = str(payload.get("origin", "live"))
    if name not in connectors or origin not in ("live", "test_fixture"):
        return {"status": "rejected", "reason": "unknown connector or origin", "known": sorted(connectors)}
    connector = connectors[name]
    if name not in SEMANTICS:
        return {"status": "unsupported", "reason": f"no declared schema semantics for {name}; "
                                                   "repairs for it need named review"}
    advisor = ConnectorRepairAdvisor(ctx.worker_engine, ctx.store, guard=guard)
    try:
        case = advisor.find_case(connector, origin=origin)
        if case is None:
            return {"status": "no_case", "connector": name}
        a = advisor.assess(connector, case)
        fence = ctx.assert_lease if getattr(ctx, "job_id", None) is not None and hasattr(ctx, "assert_lease") \
            else None
        rp = advisor.propose(connector, a, fence=fence)
    except AutonomyBlocked as e:
        return {"status": "blocked", "reason": e.message}
    out: dict[str, Any] = {"connector": name, "classification": a.classification, "action": a.action,
                           "tier": a.tier, "item": a.item_key, "concepts": list(a.affected_concepts)}
    if rp is None:
        return {**out, "status": "no_proposal"}
    gate = PolicyPromotionGate(ctx.worker_engine, ArtifactIO(ctx.store), guard=guard)
    d = gate.decide(rp.proposal, list(rp.reports), promotion_policy().ref())
    out.update({"status": f"gate_{d.outcome}", "proposal_id": str(rp.proposal.proposal_id),
                "gate_result_id": str(d.decision_id), "reasons": list(d.reasons),
                "paused_concepts": list(rp.pause.concepts) if rp.pause else []})
    if d.outcome == "promote" and a.action == "documentation_discovery":
        with ctx.worker_engine.begin() as conn:
            repo.emit_outbox(conn, "autonomy.documentation_discovery_requested",
                             {"connector": name, "proposal_id": str(rp.proposal.proposal_id),
                              "gate_result_id": str(d.decision_id),
                              "allowed_documentation": rp.extra.get("documentation_sources", [])})
    return out


__all__ = [
    "ADDITIVE", "AUTH", "CLASSIFICATION", "CONTENT", "ENDPOINT", "MEANING", "PAUSE_WITHHOLD_REASON", "REMOVED",
    "RENAME", "RESUME_EXECUTOR", "SCALE", "STRUCTURE", "TRANSIENT", "TYPE_CHANGE", "UNIT", "AttemptSignal",
    "ConnectorRepairAdvisor", "DriftCase", "FixtureReplayChecker", "MappedParser", "MappingRevision",
    "PublicationPause", "RepairAssessment", "RepairProposal", "RunSignals", "ScaleRule", "SchemaSemantics",
    "active_publication_pauses", "diff_payload", "dumps_exact", "failure_history", "failure_streak",
    "handle_connector_repair", "is_publication_paused", "last_accepted_release", "load_exact",
    "pause_belief_publication", "resume_belief_publication", "schema_profile", "semantic_schema_fingerprint",
    "suggest_mapping", "value_comparison",
]
