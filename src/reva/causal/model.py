"""Causal claims and candidates: the write side (loaders). See ``docs/dev/v3_causal_interface.md``.

* :func:`load_kg_claims` — causal Wikidata statements of ``kg.statement_current`` -> claims (T3 with
  references, T4 without — or with only "imported from Wikimedia" pointers; T2 when a reference carries a
  DOI/ISBN; WDQS rows, whose references were never read, are deferred: no tier is invented), direction
  normalised, statement key/id/row, revision, raw sha256 and references kept verbatim. Idempotent; a statement
  that left the KG current view retracts its claim. A row the database refuses is rejected with its reason;
  it never aborts the load.
* :func:`load_dataset_claims` — T1/T2 claims of expert-coded datasets (dataset row selector + raw sha256 +
  codebook). Idempotent; with ``complete=True`` claims absent from the delivery are retracted.
* :func:`insert_candidates` — quarantined T5/T6 candidates (separate table, never graded, never in answers).

Identity: ``claim_key`` (``kg:<statement key>`` / ``ds:<source>:<dataset>:<id>``) + origin. A claim whose
content changed is a new row (the current view shows the latest); an unchanged reload inserts nothing; a claim
that comes back after a retraction is stored again with ``reinstates_row_id``. Nothing is updated or deleted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from reva.causal import grading as gr
from reva.causal.vocab import CAUSAL_PIDS, POLARITIES, RELATIONS, STANCES, Vocabulary, normalise_wikidata, vocabulary
from reva.kernel.errors import ValidationProblem
from reva.kg.model import QID_RE, SHA_RE, SOURCE_KEY_RE, _ensure_raw_artifacts
from reva.kg.query import is_community_edited

LOADER_VERSION = "reva.causal.model/2"
KG_LOADER_SOURCE = "reva.causal.kg"
MAX_CLAIMS_PER_CALL = 100_000
MAX_KG_STATEMENTS = 500_000
MAX_CANDIDATES_PER_CALL = 50_000
MAX_TEXT = 4_000
MAX_JSON_BYTES = 32_768
INSERT_CHUNK = 5_000
MAX_REPORTED = 200
ORIGINS = ("live", "test_fixture")
DATASET_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
EXTRACTOR_RE = DATASET_RE
CLAIM_ID_RE = re.compile(r"^\S{1,200}$")
#: Qualifiers mapped to the claim scope (place / part) — the full qualifiers are kept too.
SCOPE_PLACE_PIDS = ("P276", "P17", "P1001")
SCOPE_PART_PIDS = ("P518",)
DISPUTED_BY_PID = "P1310"

_ENC = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _json(o: Any) -> str:
    return _ENC.encode(o)


def _sha(o: Any) -> str:
    return hashlib.sha256(("reva-causal-1\x00" + _json(o)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class Codebook:
    """The codebook a T1 dataset's coded relations rest on (definitions quoted verbatim)."""

    name: str
    version: str
    url: str | None = None
    definitions: Mapping[str, str] = field(default_factory=dict)
    #: per term, where the quote sits in the ARCHIVED codebook bytes (AUDFIX, audit M4): ``{"document", "sha256",
    #: "page" or "paragraph", "extraction"}``. A T1 claim carries the entry of its term as ``codebook.quote_source``.
    sources: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "url": self.url,
            "definitions": dict(self.definitions),
        }
        if self.sources:
            out["sources"] = {k: dict(v) for k, v in self.sources.items()}
        return out


@dataclass(frozen=True)
class DatasetClaim:
    cause_id: str
    effect_id: str
    relation: str
    selector: Mapping[str, Any]
    raw_sha256: str | None = None
    codebook_term: str | None = None
    source_claim_id: str | None = None
    polarity: str | None = None
    mechanism: str | None = None
    scope: Mapping[str, Any] = field(default_factory=dict)
    stance: str = "asserts"
    contests: Sequence[str] = ()
    tier: str = "T1"
    citation: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CandidateIn:
    cause_id: str
    effect_id: str
    relation: str
    tier: str
    source_revision: str
    source_sentence: str
    selector: Mapping[str, Any]
    raw_sha256: str | None = None
    polarity: str | None = None
    mechanism: str | None = None


@dataclass
class ClaimLoadReport:
    load_ids: list[int] = field(default_factory=list)
    origin: str | None = None
    scanned: int = 0
    inserted: int = 0
    new: int = 0
    new_revision: int = 0
    unchanged: int = 0
    reinstated: int = 0
    stale: int = 0
    retracted: int = 0
    unknown_entities: int = 0
    #: claims whose cause or effect has no ``kg.entity`` row and no statement (audit M2), with a sample of keys
    unknown_endpoint_claims: int = 0
    unknown_endpoint_sample: list[dict[str, Any]] = field(default_factory=list)
    #: retracted claims by reason detail (e.g. ``value_not_an_item``: the statement still exists, its value
    #: became somevalue/novalue), kept beside the stored retraction reason (audit L8)
    retraction_details: dict[str, int] = field(default_factory=dict)
    #: retracted claims by stored reason (audit2 N1: ``reclassified_by_reva`` when the source row is still there,
    #: ``removed_at_source`` only when it is verified absent, ``absent_from_delivery`` when not checked)
    retraction_reasons: dict[str, int] = field(default_factory=dict)
    #: KG claims whose references / qualifiers were read verbatim from the raw JSON vs the KG copy (audit L4)
    refs_source_counts: dict[str, int] = field(default_factory=dict)
    rejected_counts: dict[str, int] = field(default_factory=dict)
    rejected_sample: list[dict[str, Any]] = field(default_factory=list)
    #: statements not turned into claims yet, by reason (e.g. ``references_unknown``: WDQS row, tier pending)
    deferred_counts: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    elapsed_s: float = 0.0

    @property
    def load_id(self) -> int | None:
        return self.load_ids[-1] if self.load_ids else None

    def reject(self, index: int, reason: str, key: str | None = None) -> None:
        self.rejected_counts[reason] = self.rejected_counts.get(reason, 0) + 1
        if len(self.rejected_sample) < MAX_REPORTED:
            self.rejected_sample.append({"index": index, "reason": reason, "key": key})

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected_counts.values())

    def merge(self, other: ClaimLoadReport) -> None:
        for k in ("scanned", "inserted", "new", "new_revision", "unchanged", "reinstated", "stale", "retracted"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.unknown_entities += other.unknown_entities
        self.unknown_endpoint_claims += other.unknown_endpoint_claims
        self.unknown_endpoint_sample += other.unknown_endpoint_sample[
            : MAX_REPORTED - len(self.unknown_endpoint_sample)
        ]
        for r, n in other.retraction_details.items():
            self.retraction_details[r] = self.retraction_details.get(r, 0) + n
        for r, n in other.retraction_reasons.items():
            self.retraction_reasons[r] = self.retraction_reasons.get(r, 0) + n
        for r, n in other.refs_source_counts.items():
            self.refs_source_counts[r] = self.refs_source_counts.get(r, 0) + n
        self.load_ids += other.load_ids
        for r, n in other.rejected_counts.items():
            self.rejected_counts[r] = self.rejected_counts.get(r, 0) + n
        for r, n in other.deferred_counts.items():
            self.deferred_counts[r] = self.deferred_counts.get(r, 0) + n
        self.rejected_sample += other.rejected_sample[: MAX_REPORTED - len(self.rejected_sample)]
        self.truncated |= other.truncated

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["load_id"] = self.load_id
        return d


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Deferred(ValueError):
    """A KG statement that cannot be tiered yet (``reason``, e.g. ``references_unknown``): not a claim, not an
    error — counted in :attr:`ClaimLoadReport.deferred_counts`; any stored claim of it is kept as is."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------------------------- claim rows
#: Columns of kg.causal_claim written by the loaders (order of the staging JSON arrays).
_CLAIM_COLS = (
    "claim_key",
    "content_sha256",
    "cause_id",
    "effect_id",
    "relation",
    "family",
    "polarity",
    "stance",
    "asserted_on",
    "source_key",
    "source_tier",
    "dataset",
    "source_revision",
    "statement_key",
    "statement_id",
    "statement_row_id",
    "selector",
    "raw_sha256",
    "refs",
    "upstream_keys",
    "citation",
    "codebook",
    "mechanism",
    "scope",
    "qualifiers",
    "contests",
    "disputed_by",
    "retrieved_at",
)
_JSONB = {"selector", "refs", "citation", "codebook", "scope", "qualifiers", "disputed_by"}
_ARRAYS = {"upstream_keys", "contests"}
_CONTENT_EXCLUDED = {"claim_key", "content_sha256", "retrieved_at"}


def _content(row: Mapping[str, Any]) -> str:
    return _sha({k: row[k] for k in _CLAIM_COLS if k not in _CONTENT_EXCLUDED})


def _select_expr(i: int, col: str) -> str:
    if col in _JSONB:
        return f"NULLIF(x->{i}, 'null'::jsonb) AS {col}"
    if col in _ARRAYS:
        return f"ARRAY(SELECT jsonb_array_elements_text(x->{i})) AS {col}"
    if col == "statement_row_id":
        return f"(x->>{i})::bigint AS {col}"
    if col == "retrieved_at":
        return f"(x->>{i})::timestamptz AS {col}"
    return f"x->>{i} AS {col}"


_STAGE_SELECT = ", ".join(_select_expr(i, c) for i, c in enumerate(_CLAIM_COLS))


def _check_id(v: Any, reason: str) -> str:
    if not isinstance(v, str) or not QID_RE.match(v):
        raise _Reject(reason)
    return v


def _check_text(v: Any, reason: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > MAX_TEXT:
        raise _Reject(reason)
    return v


def _check_json(v: Any, reason: str) -> Any:
    try:
        if len(_json(v)) > MAX_JSON_BYTES:
            raise _Reject(reason)
    except (TypeError, ValueError) as e:
        raise _Reject(reason) from e
    return v


# ---------------------------------------------------------------------------------------------- writing
def _latest(conn: Connection, origin: str, prefix: str) -> dict[str, dict[str, Any]]:
    """Latest stored row (retracted or not) per claim key under ``prefix`` for ``origin``."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        text("""SELECT DISTINCT ON (c.claim_key) c.id, c.claim_key, c.content_sha256, c.retrieved_at,
                       c.statement_key, c.raw_sha256, c.source_revision, c.selector, c.load_id,
                       (r.id IS NOT NULL) AS retracted
                  FROM kg.causal_claim c LEFT JOIN kg.causal_claim_retraction r ON r.claim_row_id = c.id
                 WHERE c.origin = :o AND starts_with(c.claim_key, :p)
                 ORDER BY c.claim_key, c.id DESC"""),
        {"o": origin, "p": prefix},
    ).mappings():
        out[r["claim_key"]] = dict(r)
    return out


def _new_load(
    conn: Connection,
    *,
    kind: str,
    source_key: str,
    origin: str,
    dataset: str | None = None,
    raws: Sequence[str] = (),
    retrieved_at: datetime | None = None,
    watermark: int | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> int:
    return int(
        conn.execute(
            text("""INSERT INTO kg.causal_load(kind, source_key, dataset, origin, raw_sha256s, retrieved_at,
                        kg_load_watermark, loader_version, parameters)
                    VALUES (:k, :sk, :ds, :o, :raws, :ret, :wm, :lv, CAST(:par AS jsonb)) RETURNING id"""),
            {
                "k": kind,
                "sk": source_key,
                "ds": dataset,
                "o": origin,
                "raws": list(raws),
                "ret": retrieved_at,
                "wm": watermark,
                "lv": LOADER_VERSION,
                "par": _json(dict(parameters or {})),
            },
        ).scalar_one()
    )


def _write(
    conn: Connection,
    *,
    load: Any,
    origin: str,
    prefix: str,
    rows: list[dict[str, Any]],
    retract_missing: bool,
    retract_reason: Any,
    batch_retrieved_at: datetime | None,
    rep: ClaimLoadReport,
    keep: Iterable[str] = (),
) -> None:
    """Classify ``rows`` against the stored latest rows, insert what changed, retract what disappeared (except
    the claim keys in ``keep``: deferred statements still at the source).

    ``load`` is a zero-argument callable returning the load id: the load row is created only when something is
    written (an unchanged reload records nothing)."""
    latest = _latest(conn, origin, prefix)
    to_insert: list[tuple[dict[str, Any], int | None, str]] = []
    for row in rows:
        lt = latest.get(row["claim_key"])
        if lt is None:
            to_insert.append((row, None, "new"))
        elif batch_retrieved_at is not None and lt["retrieved_at"] > batch_retrieved_at:
            rep.stale += 1
        elif lt["retracted"]:
            to_insert.append((row, lt["id"], "reinstated"))
        elif lt["content_sha256"] == row["content_sha256"]:
            rep.unchanged += 1
        else:
            to_insert.append((row, None, "new_revision"))
    seen = {r["claim_key"] for r in rows} | set(keep)
    gone = []
    if retract_missing:
        for k, lt in latest.items():
            if k in seen or lt["retracted"]:
                continue
            if batch_retrieved_at is not None and lt["retrieved_at"] > batch_retrieved_at:
                continue
            reason, detail = retract_reason(lt)
            rep.retraction_reasons[reason] = rep.retraction_reasons.get(reason, 0) + 1
            gone.append((lt["id"], k, reason, detail))
    if not to_insert and not gone:
        return
    load_id = load()
    for i in range(0, len(to_insert), INSERT_CHUNK):
        _insert_claims(conn, load_id, origin, to_insert[i : i + INSERT_CHUNK], rep)
    for i in range(0, len(gone), INSERT_CHUNK):
        part = gone[i : i + INSERT_CHUNK]
        n = conn.execute(
            text("""INSERT INTO kg.causal_claim_retraction(load_id, claim_row_id, claim_key, origin, reason, detail)
                    SELECT :load, (x->>0)::bigint, x->>1, :o, x->>2, NULLIF(x->3, 'null'::jsonb)
                      FROM jsonb_array_elements(CAST(:rows AS jsonb)) x"""),
            {"load": load_id, "o": origin, "rows": _json([list(g) for g in part])},
        ).rowcount
        rep.retracted += int(n)


def _close(sp: Any, rep: ClaimLoadReport, loaded: list[int]) -> None:
    """Release the write savepoint, or roll it back (with its load row) when the database refused every row."""
    if loaded and rep.inserted == 0 and rep.retracted == 0:
        sp.rollback()
        loaded.clear()
    else:
        sp.commit()


def _db_reason(e: DBAPIError) -> str:
    """Short, stable rejection reason for a row the database refused (constraint name, else its message)."""
    diag = getattr(e.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return f"db_refused:{name}"[:100]
    msg = getattr(diag, "message_primary", None) or (str(e.orig).splitlines() or ["error"])[0]
    return ("db_refused:" + re.sub(r"\d+", "#", msg))[:100]


def _insert_claims(
    conn: Connection,
    load_id: int,
    origin: str,
    items: Sequence[tuple[dict[str, Any], int | None, str]],
    rep: ClaimLoadReport,
) -> None:
    """Insert claim rows under a savepoint; when the database refuses the batch (a CHECK, a trigger), split it
    until the refused rows are isolated, insert the others and reject each refused row with its reason. One bad
    statement never aborts the load."""
    if not items:
        return
    payload = _json([[r[c] for c in _CLAIM_COLS] + [reinst] for r, reinst, _k in items])
    try:
        with conn.begin_nested():
            n = conn.execute(
                text(f"""INSERT INTO kg.causal_claim(load_id, origin, {", ".join(_CLAIM_COLS)}, reinstates_row_id)
                     SELECT :load, :o, {", ".join("st." + c for c in _CLAIM_COLS)}, st.reinstates_row_id
                       FROM (SELECT {_STAGE_SELECT}, (x->>{len(_CLAIM_COLS)})::bigint AS reinstates_row_id,
                                    ordinality AS ord
                               FROM jsonb_array_elements(CAST(:rows AS jsonb)) WITH ORDINALITY AS t(x, ordinality)) st
                      ORDER BY st.ord"""),
                {"load": load_id, "o": origin, "rows": payload},
            ).rowcount
    except DBAPIError as e:
        if len(items) == 1:
            row = items[0][0]
            rep.reject(-1, _db_reason(e), row["claim_key"])
            return
        mid = len(items) // 2
        _insert_claims(conn, load_id, origin, items[:mid], rep)
        _insert_claims(conn, load_id, origin, items[mid:], rep)
        return
    rep.inserted += int(n)
    for _r, _reinst, kind in items:
        setattr(rep, kind, getattr(rep, kind) + 1)


def _unknown_ids(conn: Connection, ids: Iterable[str]) -> set[str]:
    """Ids with no ``kg.entity`` row and no statement about them."""
    qs = sorted(set(ids))
    if not qs:
        return set()
    return set(
        conn.execute(
            text("""SELECT q.id FROM unnest(CAST(:q AS text[])) q(id)
                     WHERE NOT EXISTS (SELECT 1 FROM kg.entity e WHERE e.qid = q.id)
                       AND NOT EXISTS (SELECT 1 FROM kg.statement s WHERE s.subject_qid = q.id)"""),
            {"q": qs},
        ).scalars()
    )


def _unknown_entities(conn: Connection, ids: Iterable[str]) -> int:
    return len(_unknown_ids(conn, ids))


def _count_unknown_endpoints(conn: Connection, rows: Sequence[Mapping[str, Any]], rep: ClaimLoadReport) -> None:
    """Count the claims whose cause or effect is not in the KG (audit M2): such a claim has no label, kind or
    dates (its temporal check can only be ``undetermined``). Counted and sampled, never dropped or invented."""
    unknown = _unknown_ids(conn, [r["cause_id"] for r in rows] + [r["effect_id"] for r in rows])
    rep.unknown_entities = len(unknown)
    for r in rows:
        sides = [s for s, x in (("cause", r["cause_id"]), ("effect", r["effect_id"])) if x in unknown]
        if sides:
            rep.unknown_endpoint_claims += 1
            if len(rep.unknown_endpoint_sample) < MAX_REPORTED:
                rep.unknown_endpoint_sample.append({"claim_key": r["claim_key"], "missing": sides})


# ---------------------------------------------------------------------------------------------- KG loader
_KG_SQL = """SELECT s.id, s.statement_key, s.statement_id, s.origin, s.subject_qid, s.property_pid, s.object_qid,
       s.qualifiers, s.rank, s.refs, s.source_key, s.source_revision, s.raw_sha256, s.retrieved_at,
       s.start_time, s.start_precision, s.end_time, s.end_precision, s.pit_time, s.pit_precision
  FROM kg.statement_current s
 WHERE s.property_pid = ANY(:pids) AND s.object_qid IS NOT NULL AND s.origin = :o
   AND (s.source_key = 'wikidata' OR starts_with(s.source_key, 'wikidata.'))
   {source_filter}
 ORDER BY s.id
 LIMIT :cap"""


def _items(vals: Any) -> list[str]:
    out = []
    for v in vals if isinstance(vals, list) else [vals]:
        q = gr._item(v)
        if q and QID_RE.match(q):
            out.append(q)
    return out


def claim_from_statement(st: Mapping[str, Any], vocab: Vocabulary | None = None) -> dict[str, Any]:
    """The claim row a causal KG statement (a ``kg.statement_current`` row) asserts. Raises ``ValueError`` when
    the statement is not a causal Wikidata statement."""
    v = vocab or vocabulary()
    if st["property_pid"] not in CAUSAL_PIDS:
        raise ValueError("not a causal property")
    if not is_community_edited(st["source_key"]):
        raise ValueError("only community-edited (Wikidata) statements are loaded from the KG")
    n = normalise_wikidata(st["subject_qid"], st["property_pid"], st["object_qid"], v)
    refs = list(st.get("refs") or [])
    quals = dict(st.get("qualifiers") or {})
    tier, _basis = gr.kg_tier(st["source_key"], refs)
    if tier is None:  # references never read (WDQS row): tier pending, not "unreferenced"
        raise Deferred(_basis)
    citation = gr.citation_of(refs) if tier == "T2" else None
    scope: dict[str, Any] = {}
    places = sorted({q for p in SCOPE_PLACE_PIDS for q in _items(quals.get(p, []))})
    if places:
        scope["place"] = places
    parts = sorted({q for p in SCOPE_PART_PIDS for q in _items(quals.get(p, []))})
    if parts:
        scope["part"] = parts
    period = {
        k: {"time": st[f"{k}_time"], "precision": st[f"{k}_precision"]}
        for k in ("start", "end", "pit")
        if st.get(f"{k}_time")
    }
    if period:
        scope["period"] = period
    disputed = [{"property": DISPUTED_BY_PID, "by": q} for q in _items(quals.get(DISPUTED_BY_PID, []))]
    row: dict[str, Any] = {
        "claim_key": "kg:" + st["statement_key"],
        "cause_id": n.cause_id,
        "effect_id": n.effect_id,
        "relation": n.relation,
        "family": v.family(n.relation),
        "polarity": None,
        "stance": "asserts",
        "asserted_on": n.asserted_on,
        "source_key": st["source_key"],
        "source_tier": tier,
        "dataset": None,
        "source_revision": st["source_revision"],
        "statement_key": st["statement_key"],
        "statement_id": st["statement_id"],
        "statement_row_id": int(st["id"]),
        "selector": {
            "entity": st["subject_qid"],
            "property": st["property_pid"],
            "statement_id": st["statement_id"],
            "rank": st["rank"],
            **({"refs_qualifiers": "verbatim from the raw entity JSON"} if st.get("_verbatim") else {}),
        },
        "raw_sha256": st["raw_sha256"],
        "refs": refs,
        "upstream_keys": gr.upstream_keys(source_key=st["source_key"], refs=refs),
        "citation": citation,
        "codebook": None,
        "mechanism": None,
        "scope": scope,
        "qualifiers": quals,
        "contests": [],
        "disputed_by": disputed,
        "retrieved_at": st["retrieved_at"].isoformat()
        if isinstance(st["retrieved_at"], datetime)
        else st["retrieved_at"],
    }
    row["content_sha256"] = _content(row)
    return row


class _RawStatements:
    """Wikidata statements of the archived raw entity JSON, by statement id (audit L4).

    The KG import keeps only allow-listed reference / qualifier properties; a causal claim keeps the statement's
    references and qualifiers VERBATIM (compacted like the harvester, every property), read from the raw bytes the
    KG statement names (``raw_sha256``). A raw document that cannot be read leaves the KG copy (counted)."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.docs: dict[str, dict[str, Mapping[str, Any]] | None] = {}
        self.missing = 0

    def _load(self, sha: str) -> dict[str, Mapping[str, Any]] | None:
        import gzip

        try:
            body = self.store.get("raw", sha)
        except Exception:  # absent / unreadable raw: fall back to the KG copy, counted
            return None
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        try:
            doc = json.loads(body)
        except ValueError:
            return None
        out: dict[str, Mapping[str, Any]] = {}
        for ent in (doc.get("entities") or {}).values() if isinstance(doc, dict) else []:
            for sts in (ent.get("claims") or {}).values() if isinstance(ent, dict) else []:
                for st in sts if isinstance(sts, list) else []:
                    if isinstance(st, dict) and isinstance(st.get("id"), str):
                        out[st["id"]] = st
        return out

    def verbatim(self, st: Mapping[str, Any]) -> tuple[list[dict[str, list[Any]]], dict[str, list[Any]]] | None:
        from reva.adapters.connectors.wikidata import TEMPORAL_QUALIFIERS, _compact_snak_value

        sha, sid = st.get("raw_sha256"), st.get("statement_id")
        if not sha or not sid:
            return None
        if sha not in self.docs:
            self.docs[sha] = self._load(str(sha))
        raw = (self.docs[sha] or {}).get(str(sid))
        if raw is None:
            self.missing += 1
            return None
        refs = [
            {p: [_compact_snak_value(x) for x in snaks] for p, snaks in (r.get("snaks") or {}).items()}
            for r in raw.get("references") or []
        ]
        quals = {
            p: [_compact_snak_value(x) for x in snaks]
            for p, snaks in (raw.get("qualifiers") or {}).items()
            if p not in TEMPORAL_QUALIFIERS  # the claim's period comes from the KG start/end/point in time
        }
        return [r for r in refs if r], quals


def load_kg_claims(
    engine: Engine, *, origin: str | None = None, source_keys: Sequence[str] | None = None, store: Any = None
) -> ClaimLoadReport:
    """Causal statements of ``kg.statement_current`` -> ``kg.causal_claim`` (one transaction per origin).

    With ``store`` (the artifact store holding the raw Wikidata responses), each claim's references and qualifiers
    are the statement's own, verbatim from the archived raw JSON -- not the KG import's allow-listed subset (audit
    L4); the tier is computed on them."""
    t0 = time.perf_counter()
    if origin is not None and origin not in ORIGINS:
        raise ValidationProblem("origin must be live or test_fixture", origin=str(origin)[:32])
    sks = None
    if source_keys is not None:
        sks = sorted(set(source_keys))
        if not sks or not all(isinstance(s, str) and SOURCE_KEY_RE.match(s) and is_community_edited(s) for s in sks):
            raise ValidationProblem("source_keys must be community-edited (wikidata*) source keys")
    total = ClaimLoadReport(origin=origin)
    vocab = vocabulary()
    for o in [origin] if origin else list(ORIGINS):
        rep = ClaimLoadReport(origin=o)
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": "reva:causal:kg:" + o})
            wm = conn.execute(text("SELECT max(id) FROM kg.load")).scalar_one()
            rows_db = (
                conn.execute(
                    text(_KG_SQL.format(source_filter="AND s.source_key = ANY(:sks)" if sks else "")),
                    {"pids": list(CAUSAL_PIDS), "o": o, "sks": sks, "cap": MAX_KG_STATEMENTS + 1},
                )
                .mappings()
                .all()
            )
            if len(rows_db) > MAX_KG_STATEMENTS:
                rep.truncated = True  # nothing is retracted from a truncated scan
                rows_db = rows_db[:MAX_KG_STATEMENTS]
            rep.scanned = len(rows_db)
            rows: list[dict[str, Any]] = []
            deferred_keys: list[str] = []
            raw_sts = _RawStatements(store) if store is not None else None
            for i, st in enumerate(rows_db):
                try:
                    d = dict(st)
                    vb = raw_sts.verbatim(d) if raw_sts is not None else None
                    if vb is not None:
                        d["refs"], d["qualifiers"] = vb
                        d["_verbatim"] = True
                    rows.append(claim_from_statement(d, vocab))
                except Deferred as d:
                    rep.deferred_counts[d.reason] = rep.deferred_counts.get(d.reason, 0) + 1
                    deferred_keys.append("kg:" + st["statement_key"])
                except ValueError as e:
                    rep.reject(i, str(e)[:100], st["statement_key"])
            current_stmt = {r["statement_key"] for r in rows_db}
            if raw_sts is not None:
                n_raw = sum(1 for r in rows if "refs_qualifiers" in r["selector"])
                rep.refs_source_counts = {"raw_verbatim": n_raw, "kg_allowlisted": len(rows) - n_raw}
            _count_unknown_endpoints(conn, rows, rep)
            # Current causal statements whose value is no longer an item (somevalue / novalue): the statement still
            # exists, so their claim is retracted as "no_longer_causal", never "source_statement_retracted" (L8).
            not_item = set(
                conn.execute(
                    text(
                        "SELECT s.statement_key FROM kg.statement_current s WHERE s.property_pid = ANY(:pids) "
                        "AND s.object_qid IS NULL AND s.origin = :o "
                        "AND (s.source_key = 'wikidata' OR starts_with(s.source_key, 'wikidata.'))"
                        + (" AND s.source_key = ANY(:sks)" if sks else "")
                    ),
                    {"pids": list(CAUSAL_PIDS), "o": o, "sks": sks},
                ).scalars()
            )

            def reason(
                lt: Mapping[str, Any],
                _cur: set[str] = current_stmt,
                _ni: set[str] = not_item,
                _rep: ClaimLoadReport = rep,
            ) -> tuple[str, dict[str, Any]]:
                if lt["statement_key"] in _ni:
                    _rep.retraction_details["value_not_an_item"] = (
                        _rep.retraction_details.get("value_not_an_item", 0) + 1
                    )
                    return "no_longer_causal", {"basis": "value_not_an_item", "statement_key": lt["statement_key"]}
                if lt["statement_key"] in _cur:
                    return "no_longer_causal", {"basis": "statement_current_no_claim", "statement_key": lt["statement_key"]}
                return "source_statement_retracted", {"basis": "statement_not_current", "statement_key": lt["statement_key"]}

            if sks is None:
                prefixes = ["kg:"]
            else:
                prefixes = [f"kg:{s}:" for s in sks]
            loaded: list[int] = []

            def mk(
                _o: str = o,
                _wm: int | None = wm,
                _loaded: list[int] = loaded,
                _ni: int = len(not_item),
                _unk: int = rep.unknown_endpoint_claims,
            ) -> int:
                if not _loaded:
                    _loaded.append(
                        _new_load(
                            conn,
                            kind="kg_statements",
                            source_key=KG_LOADER_SOURCE,
                            origin=_o,
                            watermark=_wm,
                            parameters={
                                "source_keys": sks,
                                "properties": list(CAUSAL_PIDS),
                                "relations_sha256": vocab.sha256,
                                "unknown_endpoint_claims": _unk,
                                "statements_value_not_an_item": _ni,
                            },
                        )
                    )
                return _loaded[0]

            sp = conn.begin_nested()  # dropped when every row was refused: no empty load row
            for p in prefixes:
                part = [r for r in rows if r["claim_key"].startswith(p)]
                _write(
                    conn,
                    load=mk,
                    origin=o,
                    prefix=p,
                    rows=part,
                    retract_missing=not rep.truncated,
                    retract_reason=reason,
                    batch_retrieved_at=None,
                    rep=rep,
                    keep=[k for k in deferred_keys if k.startswith(p)],
                )
            _close(sp, rep, loaded)
            rep.load_ids += loaded
        total.merge(rep)
    total.elapsed_s = round(time.perf_counter() - t0, 4)
    return total


# ---------------------------------------------------------------------------------------------- datasets
def _check_common(source_key: str, raw_sha256s: Sequence[str], retrieved_at: datetime, origin: str) -> list[str]:
    if not isinstance(source_key, str) or not SOURCE_KEY_RE.match(source_key):
        raise ValidationProblem("invalid source_key", source_key=str(source_key)[:100])
    if is_community_edited(source_key):
        raise ValidationProblem("Wikidata claims are loaded from the KG (load_kg_claims), not as a dataset")
    if origin not in ORIGINS:
        raise ValidationProblem("origin must be live or test_fixture", origin=str(origin)[:32])
    raws = list(raw_sha256s)
    if (
        not raws
        or len(raws) > 10_000
        or len(set(raws)) != len(raws)
        or not all(isinstance(s, str) and SHA_RE.match(s) for s in raws)
    ):
        raise ValidationProblem("raw_sha256s must be 1..10000 distinct lowercase sha256 hex digests")
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None:
        raise ValidationProblem("retrieved_at must be a timezone-aware datetime")
    return raws


def _citation(c: Mapping[str, Any] | None) -> dict[str, list[str]] | None:
    if c is None:
        return None
    if not isinstance(c, Mapping):
        raise _Reject("invalid_citation")
    out: dict[str, list[str]] = {}
    for k in ("doi", "isbn"):
        v = c.get(k)
        vals = [v] if isinstance(v, str) else list(v or [])
        if not all(isinstance(x, str) and 1 <= len(x) <= 200 for x in vals):
            raise _Reject("invalid_citation")
        if vals:
            out[k] = vals
    extra = set(c) - {"doi", "isbn", "text"}
    if extra or not out:
        raise _Reject("citation_needs_doi_or_isbn")
    if isinstance(c.get("text"), str):
        out["text"] = [c["text"][:MAX_TEXT]]
    return out


def _dataset_row(
    i: int,
    c: DatasetClaim,
    *,
    source_key: str,
    dataset: str,
    dataset_revision: str,
    codebook: Codebook | None,
    raws: set[str],
    retrieved_at: datetime,
    vocab: Vocabulary,
) -> dict[str, Any]:
    if not isinstance(c, DatasetClaim):
        raise _Reject("not_a_dataset_claim")
    cause = _check_id(c.cause_id, "invalid_cause_id")
    effect = _check_id(c.effect_id, "invalid_effect_id")
    if c.relation not in RELATIONS:
        raise _Reject("unknown_relation")
    if c.polarity is not None and c.polarity not in POLARITIES:
        raise _Reject("invalid_polarity")
    if c.stance not in STANCES:
        raise _Reject("invalid_stance")
    if c.tier not in ("T1", "T2"):
        raise _Reject("dataset_tier_must_be_t1_or_t2")
    if not isinstance(c.selector, Mapping) or not c.selector:
        raise _Reject("selector_required")
    selector = _check_json(dict(c.selector), "selector_too_large")
    raw = c.raw_sha256 or (next(iter(raws)) if len(raws) == 1 else None)
    if raw is None or raw not in raws:
        raise _Reject("raw_sha256_not_in_batch")
    cb = None
    if c.tier == "T1":
        if codebook is None:
            raise _Reject("t1_requires_codebook")
        if not c.codebook_term or c.codebook_term not in codebook.definitions:
            raise _Reject("codebook_term_not_defined")
        cb = {**codebook.as_dict(), "term": c.codebook_term, "definition": codebook.definitions[c.codebook_term]}
        cb.pop("definitions")
        cb.pop("sources", None)
        src = (codebook.sources or {}).get(c.codebook_term)
        if src:
            cb["quote_source"] = dict(src)
    citation = _citation(c.citation)
    if c.tier == "T2" and citation is None:
        raise _Reject("t2_requires_citation")
    contests = list(c.contests or ())
    if len(contests) > 100 or not all(isinstance(k, str) and 4 <= len(k) <= 400 for k in contests):
        raise _Reject("invalid_contests")
    if c.source_claim_id is not None and not (
        isinstance(c.source_claim_id, str) and CLAIM_ID_RE.match(c.source_claim_id)
    ):
        raise _Reject("invalid_source_claim_id")
    cid = c.source_claim_id or _sha(selector)[:32]
    scope = _check_json(dict(c.scope or {}), "scope_too_large")
    row: dict[str, Any] = {
        "claim_key": f"ds:{source_key}:{dataset}:{cid}",
        "cause_id": cause,
        "effect_id": effect,
        "relation": c.relation,
        "family": vocab.family(c.relation, c.polarity),
        "polarity": c.polarity,
        "stance": c.stance,
        "asserted_on": None,
        "source_key": source_key,
        "source_tier": c.tier,
        "dataset": dataset,
        "source_revision": dataset_revision,
        "statement_key": None,
        "statement_id": None,
        "statement_row_id": None,
        "selector": selector,
        "raw_sha256": raw,
        "refs": [],
        "upstream_keys": gr.upstream_keys(source_key=source_key, dataset=dataset, citation=citation),
        "citation": citation,
        "codebook": cb,
        "mechanism": _check_text(c.mechanism, "invalid_mechanism"),
        "scope": scope,
        "qualifiers": {},
        "contests": sorted(set(contests)),
        "disputed_by": [],
        "retrieved_at": retrieved_at.isoformat(),
    }
    if len(row["claim_key"]) > 400:
        raise _Reject("claim_key_too_long")
    row["content_sha256"] = _content(row)
    return row


def load_dataset_claims(
    engine: Engine,
    store: Any,
    *,
    source_key: str,
    dataset: str,
    dataset_revision: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    claims: Iterable[DatasetClaim],
    codebook: Codebook | None = None,
    complete: bool = True,
    parameters: Mapping[str, Any] | None = None,
) -> ClaimLoadReport:
    """Load T1/T2 claims of one dataset delivery in one transaction (see the interface doc)."""
    t0 = time.perf_counter()
    raws = _check_common(source_key, raw_sha256s, retrieved_at, origin)
    if not isinstance(dataset, str) or not DATASET_RE.match(dataset):
        raise ValidationProblem("invalid dataset name", dataset=str(dataset)[:100])
    if not isinstance(dataset_revision, str) or not 1 <= len(dataset_revision) <= 100:
        raise ValidationProblem("dataset_revision is required (1..100 chars)")
    if codebook is not None and (
        not isinstance(codebook, Codebook)
        or not codebook.name
        or not codebook.version
        or not all(isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in codebook.definitions.items())
    ):
        raise ValidationProblem("invalid codebook (name, version and verbatim definitions are required)")
    items = list(claims)
    if len(items) > MAX_CLAIMS_PER_CALL:
        raise ValidationProblem("too many claims in one call: split the delivery", max_claims=MAX_CLAIMS_PER_CALL)
    rep = ClaimLoadReport(origin=origin)
    vocab = vocabulary()
    rows: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    raw_set = set(raws)
    for i, c in enumerate(items):
        try:
            row = _dataset_row(
                i,
                c,
                source_key=source_key,
                dataset=dataset,
                dataset_revision=dataset_revision,
                codebook=codebook,
                raws=raw_set,
                retrieved_at=retrieved_at,
                vocab=vocab,
            )
        except _Reject as r:
            rep.reject(i, r.reason)
            continue
        prev = seen.get(row["claim_key"])
        if prev is not None:
            if prev != row["content_sha256"]:
                rep.reject(i, "conflict_in_batch", row["claim_key"])
            continue
        seen[row["claim_key"]] = row["content_sha256"]
        rows.append(row)
    rep.scanned = len(items)
    complete_requested = complete
    if complete and rep.rejected_total:
        # A partly rejected "complete" delivery must not retract the claims of the rejected rows.
        complete = False
        rep.rejected_counts["complete_downgraded_to_partial"] = 1
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"reva:causal:ds:{source_key}:{dataset}:{origin}"},
        )
        _ensure_raw_artifacts(conn, store, raws, source_key)
        _count_unknown_endpoints(conn, rows, rep)
        loaded: list[int] = []
        sp = conn.begin_nested()  # dropped when every row was refused: no empty load row

        def mk() -> int:
            if not loaded:
                loaded.append(
                    _new_load(
                        conn,
                        kind="dataset",
                        source_key=source_key,
                        dataset=dataset,
                        origin=origin,
                        raws=raws,
                        retrieved_at=retrieved_at,
                        parameters={
                            **dict(parameters or {}),
                            "dataset_revision": dataset_revision,
                            "codebook": codebook.as_dict() if codebook else None,
                            "complete": complete,
                            # the caller's flag and the one applied (a partly rejected delivery is downgraded) (L8)
                            "complete_requested": complete_requested,
                            "complete_effective": complete,
                            "complete_downgrade_reason": (
                                f"{rep.rejected_total - 1} row(s) rejected"
                                if complete_requested and not complete
                                else None
                            ),
                            "unknown_endpoint_claims": rep.unknown_endpoint_claims,
                            "relations_sha256": vocab.sha256,
                        },
                    )
                )
            return loaded[0]

        _write(
            conn,
            load=mk,
            origin=origin,
            prefix=f"ds:{source_key}:{dataset}:",
            rows=rows,
            retract_missing=complete,
            retract_reason=lambda _lt: "removed_at_source",
            batch_retrieved_at=retrieved_at,
            rep=rep,
        )
        _close(sp, rep, loaded)
        rep.load_ids += loaded
    rep.elapsed_s = round(time.perf_counter() - t0, 4)
    return rep


# ---------------------------------------------------------------------------------------------- candidates
@dataclass
class CandidateReport:
    load_id: int | None = None
    inserted: int = 0
    unchanged: int = 0
    rejected_counts: dict[str, int] = field(default_factory=dict)
    rejected_sample: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def insert_candidates(
    engine: Engine,
    store: Any,
    *,
    extractor_id: str,
    extractor_version: str,
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    candidates: Iterable[CandidateIn],
) -> CandidateReport:
    """Quarantined T5/T6 candidates (``kg.causal_candidate``). Idempotent (same content = nothing new)."""
    if not isinstance(extractor_id, str) or not EXTRACTOR_RE.match(extractor_id):
        raise ValidationProblem("invalid extractor_id", extractor_id=str(extractor_id)[:100])
    if not isinstance(extractor_version, str) or not 1 <= len(extractor_version) <= 100:
        raise ValidationProblem("extractor_version is required")
    if not isinstance(source_key, str) or not SOURCE_KEY_RE.match(source_key):
        raise ValidationProblem("invalid source_key", source_key=str(source_key)[:100])
    if origin not in ORIGINS:
        raise ValidationProblem("origin must be live or test_fixture")
    raws = list(raw_sha256s)
    if not raws or len(set(raws)) != len(raws) or not all(isinstance(s, str) and SHA_RE.match(s) for s in raws):
        raise ValidationProblem("raw_sha256s must be distinct lowercase sha256 hex digests")
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None:
        raise ValidationProblem("retrieved_at must be a timezone-aware datetime")
    items = list(candidates)
    if len(items) > MAX_CANDIDATES_PER_CALL:
        raise ValidationProblem("too many candidates in one call", max_candidates=MAX_CANDIDATES_PER_CALL)
    vocab = vocabulary()
    rep = CandidateReport()
    counts: Counter[str] = Counter()
    rows: list[list[Any]] = []
    seen: set[tuple[str, str]] = set()
    for i, c in enumerate(items):
        try:
            if not isinstance(c, CandidateIn):
                raise _Reject("not_a_candidate")
            cause = _check_id(c.cause_id, "invalid_cause_id")
            effect = _check_id(c.effect_id, "invalid_effect_id")
            if c.relation not in RELATIONS:
                raise _Reject("unknown_relation")
            if c.tier not in ("T5", "T6"):
                raise _Reject("candidate_tier_must_be_t5_or_t6")
            if c.polarity is not None and c.polarity not in POLARITIES:
                raise _Reject("invalid_polarity")
            if not isinstance(c.source_revision, str) or not 1 <= len(c.source_revision) <= 100:
                raise _Reject("source_revision_required")
            sentence = _check_text(c.source_sentence, "source_sentence_required")
            if sentence is None:
                raise _Reject("source_sentence_required")
            if not isinstance(c.selector, Mapping) or not c.selector:
                raise _Reject("selector_required")
            selector = _check_json(dict(c.selector), "selector_too_large")
            raw = c.raw_sha256 or (raws[0] if len(raws) == 1 else None)
            if raw not in raws:
                raise _Reject("raw_sha256_not_in_batch")
            mech = _check_text(c.mechanism, "invalid_mechanism")
        except _Reject as r:
            counts[r.reason] += 1
            if len(rep.rejected_sample) < MAX_REPORTED:
                rep.rejected_sample.append({"index": i, "reason": r.reason})
            continue
        ident = _sha([source_key, c.source_revision, selector, cause, effect, c.relation])[:40]
        key = f"cand:{extractor_id}:{ident}"
        body = [
            cause,
            effect,
            c.relation,
            vocab.family(c.relation, c.polarity),
            c.polarity,
            c.tier,
            extractor_id,
            extractor_version,
            source_key,
            c.source_revision,
            sentence,
            selector,
            raw,
            mech,
        ]
        content = _sha(body)
        if (key, content) in seen:
            continue
        seen.add((key, content))
        rows.append([key, content, *body])
    rep.rejected_counts = dict(counts)
    with engine.begin() as conn:
        _ensure_raw_artifacts(conn, store, raws, source_key)
        existing = {
            (k, c)
            for k, c in conn.execute(
                text("""SELECT candidate_key, content_sha256 FROM kg.causal_candidate
                         WHERE origin = :o AND candidate_key = ANY(:k)"""),
                {"o": origin, "k": sorted({r[0] for r in rows})},
            ).all()
        }
        new = [r for r in rows if (r[0], r[1]) not in existing]
        rep.unchanged = len(rows) - len(new)
        if not new:
            return rep
        rep.load_id = _new_load(
            conn,
            kind="candidates",
            source_key=source_key,
            origin=origin,
            raws=raws,
            retrieved_at=retrieved_at,
            parameters={"extractor_id": extractor_id, "extractor_version": extractor_version},
        )
        for i in range(0, len(new), INSERT_CHUNK):
            rep.inserted += int(
                conn.execute(
                    text("""INSERT INTO kg.causal_candidate(load_id, origin, retrieved_at, candidate_key, content_sha256,
                                cause_id, effect_id, relation, family, polarity, tier, extractor_id, extractor_version,
                                source_key, source_revision, source_sentence, selector, raw_sha256, mechanism)
                            SELECT :load, :o, :ret, x->>0, x->>1, x->>2, x->>3, x->>4, x->>5, x->>6, x->>7, x->>8,
                                   x->>9, x->>10, x->>11, x->>12, x->13, x->>14, x->>15
                              FROM jsonb_array_elements(CAST(:rows AS jsonb)) x"""),
                    {"load": rep.load_id, "o": origin, "ret": retrieved_at, "rows": _json(new[i : i + INSERT_CHUNK])},
                ).rowcount
            )
    return rep
