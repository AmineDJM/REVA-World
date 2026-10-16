"""Shared helpers of the CAUSDATA connectors (expert-coded crisis / conflict datasets -> events + T1 claims).

Connectors: :mod:`atlas_icb` (International Crisis Behavior v16), :mod:`atlas_mid` (COW Militarized Interstate
Disputes 5.0/5.01), :mod:`atlas_ucdp_term` (UCDP Conflict Termination v4-2024 + Peace Agreements 22.2).

What lives here
---------------
* :func:`relations` -- ``registry/causal/relations_datasets.yaml`` (owner CAUSDATA): per dataset, the pinned codebook
  documents (sha256), the verbatim codebook definitions passed as ``Codebook.definitions`` and the mapping of every
  dataset variable to a REVA relation (T1 claim) or to a non-causal context statement, with the reason.
* :class:`ClaimRecord` / :class:`ClaimDelivery` -- neutral records with exactly the fields of CAUSCORE's
  ``reva.causal.load_dataset_claims`` / ``DatasetClaim`` (``docs/dev/v3_causal_interface.md``).
* :func:`deliver_claims` -- loads deliveries through ``reva.causal.load_dataset_claims`` when the causal schema and
  module exist; otherwise writes each delivery as one JSON file into the run option ``claims_staging`` (a directory)
  for a later :func:`load_staged`; otherwise reports ``causal_unavailable``. Never fabricates a claim.
* Coded dates -> (ISO date, Wikibase precision); event time statements (``P580``/``P582``/``P585``, the dates the
  causal grader reads); the dataset predicates of these connectors and a bounded KG loader.

Nothing here makes a request: connectors fetch through ``run.get`` only (atlas platform, invariant 30).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement, to_kg_model
from reva.kernel.errors import SourceResponseError

RELATIONS_FILE = "causal/relations_datasets.yaml"


class CausdataError(SourceResponseError):
    pass


def drift(msg: str) -> CausdataError:
    return CausdataError(msg, retryable=False, kind="schema_drift")


# ----------------------------------------------------------------------------- registry


@lru_cache(maxsize=1)
def relations() -> dict[str, Any]:
    import yaml

    from reva.ontology.registry import REGISTRY_ROOT

    doc = yaml.safe_load((REGISTRY_ROOT / RELATIONS_FILE).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("maintainer") != "CAUSDATA":
        raise ValueError("registry/causal/relations_datasets.yaml: unexpected content")
    return doc


def dataset_entry(key: str) -> dict[str, Any]:
    out: dict[str, Any] = relations()["datasets"][key]
    return out


def document(key: str) -> dict[str, Any]:
    out: dict[str, Any] = relations()["documents"][key]
    return out


def value_label(dataset: str, variable: str, code: Any) -> str | None:
    """Codebook label of a coded value (``None`` for an empty / uncoded value)."""
    if code is None or str(code).strip() == "":
        return None
    vals = dataset_entry(dataset).get("values", {}).get(variable, {})
    try:
        label = vals.get(int(str(code).strip()))
        return None if label is None else str(label)
    except ValueError:
        return None


def codebook(dataset: str, documents_sha256: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``Codebook`` fields for ``load_dataset_claims``: name, version (dataset revision + document sha256s), url and
    the verbatim definitions (term -> text)."""
    e = dataset_entry(dataset)
    doc = document(e["codebook_document"])
    shas = dict(documents_sha256 or {})
    version = e["dataset_revision"]
    if shas:
        version += " (" + ", ".join(f"{k} sha256 {v[:12]}" for k, v in sorted(shas.items())) + ")"
    return {
        "name": e["codebook_name"],
        "version": version,
        "url": doc.get("url") or doc.get("download_url") or "",
        "definitions": {k: v["text"] for k, v in e["definitions"].items()},
    }


def check_document(key: str, body: bytes) -> dict[str, Any]:
    """sha256 of a fetched codebook document vs the pinned one. A changed codebook is reported (``matches`` false):
    the connectors then deliver no claim for that dataset (the quoted definitions may no longer be the provider's)."""
    sha = hashlib.sha256(body).hexdigest()
    pinned = document(key)["sha256"]
    return {"document": key, "sha256": sha, "pinned_sha256": pinned, "matches": sha == pinned}


# ----------------------------------------------------------------------------- coded dates


def coded_date(
    y: Any, m: Any = None, d: Any = None, *, missing: Iterable[str] = ("", "-9", "88", "99", "NA")
) -> tuple[str, int] | None:
    """Dataset (year, month, day) -> (ISO date of the first day of the known window, Wikibase precision).

    A missing/unknown day lowers the precision to the month (10), a missing month to the year (9); an impossible
    date (e.g. 31 June) is kept at month precision rather than corrected."""
    miss = set(missing)
    ys = "" if y is None else str(y).strip()
    if ys in miss:
        return None
    try:
        yi = int(float(ys))
    except ValueError:
        return None
    ms = "" if m is None else str(m).strip()
    if ms in miss or not ms.lstrip("-").isdigit() or not 1 <= int(ms) <= 12:
        return f"{yi:04d}-01-01", 9
    mi = int(ms)
    ds = "" if d is None else str(d).strip()
    if ds in miss or not ds.lstrip("-").isdigit():
        return f"{yi:04d}-{mi:02d}-01", 10
    try:
        return date(yi, mi, int(ds)).isoformat(), 11
    except ValueError:
        return f"{yi:04d}-{mi:02d}-01", 10


def iso_date(text: Any) -> str | None:
    s = "" if text is None else str(text).strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def time_value(iso: str, precision: int) -> dict[str, Any]:
    return {"type": "time", "time": iso, "precision": int(precision)}


def event_times(
    subject: str,
    key: str,
    *,
    start: tuple[str, int] | None = None,
    end: tuple[str, int] | None = None,
    point: tuple[str, int] | None = None,
    raw_sha256: str,
    selector: Mapping[str, Any],
    qualifiers: Mapping[str, Any] | None = None,
) -> list[KgStatement]:
    """``P580`` start time / ``P582`` end time / ``P585`` point in time of an event (the dates the causal grader
    reads), each with its own precision and the row selector."""
    out = []
    for pid, val, name in (("P580", start, "start"), ("P582", end, "end"), ("P585", point, "point")):
        if val is None:
            continue
        out.append(
            KgStatement(
                key=f"{key}:{name}",
                subject=subject,
                property=pid,
                value=time_value(*val),
                qualifiers=dict(qualifiers or {}),
                raw_sha256=raw_sha256,
                selector=dict(selector),
            )
        )
    return out


def string_stmt(
    key: str, subject: str, pid: str, text: str, *, raw_sha256: str, selector: Mapping[str, Any], **qual: Any
) -> KgStatement:
    return KgStatement(
        key=key,
        subject=subject,
        property=pid,
        value={"type": "string", "value": str(text)[:4000]},
        qualifiers={k: v for k, v in qual.items() if v is not None},
        raw_sha256=raw_sha256,
        selector=dict(selector),
    )


def coded_stmt(
    key: str,
    subject: str,
    pid: str,
    dataset: str,
    variable: str,
    code: Any,
    *,
    raw_sha256: str,
    selector: Mapping[str, Any],
    **qual: Any,
) -> KgStatement | None:
    """A coded categorical value as a string statement: the codebook label, with the raw code in the qualifiers.
    ``None`` when the value is empty or not in the codebook (unknown codes are kept by the caller's flags)."""
    label = value_label(dataset, variable, code)
    if label is None:
        return None
    return string_stmt(
        key,
        subject,
        pid,
        label,
        raw_sha256=raw_sha256,
        selector=selector,
        code=str(code).strip(),
        variable=variable,
        **qual,
    )


# ----------------------------------------------------------------------------- claims (CAUSCORE interface)


@dataclass(frozen=True)
class ClaimRecord:
    """Exactly the fields of ``reva.causal.DatasetClaim`` (docs/dev/v3_causal_interface.md)."""

    cause_id: str
    effect_id: str
    relation: str
    selector: Mapping[str, Any]
    raw_sha256: str
    codebook_term: str
    source_claim_id: str
    polarity: str | None = None
    mechanism: str | None = None
    scope: Mapping[str, Any] = field(default_factory=dict)
    stance: str = "asserts"
    contests: tuple[str, ...] = ()
    tier: str = "T1"
    citation: Mapping[str, str] | None = None


@dataclass
class ClaimDelivery:
    """Exactly the arguments of ``reva.causal.load_dataset_claims`` (without engine/store)."""

    source_key: str
    dataset: str
    dataset_revision: str
    codebook: dict[str, Any]
    raw_sha256s: list[str]
    retrieved_at: datetime
    origin: str
    claims: list[ClaimRecord]
    complete: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "interface": "reva.causal.load_dataset_claims",
            "source_key": self.source_key,
            "dataset": self.dataset,
            "dataset_revision": self.dataset_revision,
            "codebook": self.codebook,
            "raw_sha256s": sorted(set(self.raw_sha256s)),
            "retrieved_at": self.retrieved_at.isoformat(),
            "origin": self.origin,
            "complete": self.complete,
            "claims": [{**asdict(c), "contests": list(c.contests)} for c in self.claims],
        }

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> ClaimDelivery:
        return cls(
            source_key=doc["source_key"],
            dataset=doc["dataset"],
            dataset_revision=doc["dataset_revision"],
            codebook=dict(doc["codebook"]),
            raw_sha256s=list(doc["raw_sha256s"]),
            retrieved_at=datetime.fromisoformat(doc["retrieved_at"]),
            origin=doc["origin"],
            complete=bool(doc.get("complete", True)),
            claims=[ClaimRecord(**{**c, "contests": tuple(c.get("contests") or ())}) for c in doc["claims"]],
        )


def _causal_api(name: str) -> Any:
    """``name`` from ``reva.causal`` (the documented import), else from the CAUSCORE module defining it
    (``reva.causal.model`` / ``reva.causal.grader``); ``None`` when CAUSCORE is not installed."""
    import importlib

    for mod in ("reva.causal", "reva.causal.model", "reva.causal.grader"):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        if hasattr(m, name):
            return getattr(m, name)
    return None


def causal_available(engine: Any) -> bool:
    """CAUSCORE's loader and schema are both present (``load_dataset_claims`` + ``kg.causal_claim``)."""
    if _causal_api("load_dataset_claims") is None or engine is None:
        return False
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('kg.causal_claim') IS NOT NULL")).scalar_one())


def _load_one(engine: Any, store: Any, d: ClaimDelivery) -> dict[str, Any]:
    codebook_cls, claim_cls = _causal_api("Codebook"), _causal_api("DatasetClaim")
    load_dataset_claims = _causal_api("load_dataset_claims")
    cb = codebook_cls(**d.codebook)
    claims = [
        claim_cls(
            cause_id=c.cause_id,
            effect_id=c.effect_id,
            relation=c.relation,
            selector=dict(c.selector),
            raw_sha256=c.raw_sha256,
            codebook_term=c.codebook_term,
            source_claim_id=c.source_claim_id,
            polarity=c.polarity,
            mechanism=c.mechanism,
            scope=dict(c.scope),
            stance=c.stance,
            contests=tuple(c.contests),
            tier=c.tier,
            citation=dict(c.citation) if c.citation else None,
        )
        for c in d.claims
    ]
    rep = load_dataset_claims(
        engine,
        store,
        source_key=d.source_key,
        dataset=d.dataset,
        dataset_revision=d.dataset_revision,
        codebook=cb,
        raw_sha256s=sorted(set(d.raw_sha256s)),
        retrieved_at=d.retrieved_at,
        origin=d.origin,
        claims=claims,
        complete=d.complete,
    )
    return rep.as_dict() if hasattr(rep, "as_dict") else dict(rep)


def staging_name(d: ClaimDelivery) -> str:
    return f"{d.source_key}__{d.dataset}__{d.origin}.json"


def deliver_claims(run: Any, deliveries: Sequence[ClaimDelivery]) -> dict[str, Any]:
    """Load (or stage) claim deliveries; see the module doc. Returns a per-dataset report."""
    out: dict[str, Any] = {}
    staging = (getattr(run, "options", None) or {}).get("claims_staging")
    if causal_available(getattr(run, "engine", None)):
        for d in deliveries:
            out[d.dataset] = {
                "status": "loaded",
                "claims": len(d.claims),
                "report": _load_one(run.engine, run.store, d),
            }
        return out
    for d in deliveries:
        if staging:
            p = Path(str(staging)).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            f = p / staging_name(d)
            f.write_text(json.dumps(d.as_json(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
            out[d.dataset] = {"status": "staged", "claims": len(d.claims), "file": str(f)}
        else:
            out[d.dataset] = {"status": "causal_unavailable", "claims": len(d.claims)}
    return out


def load_staged(engine: Any, store: Any, directory: str | Path, *, grade: bool = True) -> dict[str, Any]:
    """Load every staged delivery of ``directory`` through ``load_dataset_claims`` (worker engine), then grade."""
    if not causal_available(engine):
        raise RuntimeError("reva.causal.load_dataset_claims / kg.causal_claim not available")
    out: dict[str, Any] = {}
    for f in sorted(Path(directory).glob("*.json")):
        d = ClaimDelivery.from_json(json.loads(f.read_text(encoding="utf-8")))
        out[f.name] = _load_one(engine, store, d)
    if grade:
        rep = _causal_api("grade_claims")(engine)
        out["grading"] = rep.as_dict() if hasattr(rep, "as_dict") else str(rep)
    return out


# ----------------------------------------------------------------------------- KG predicates + loader

#: Dataset predicates of the CAUSDATA connectors: pid -> (value kind, reva predicate, en label, fr label, inverse
#: name, inverse en label). None is causal: causal links are claims (kg.causal_claim), never KG predicates.
CAUSDATA_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "causdata:event_type": (
        "string",
        "causdata_event_type",
        "event type (dataset kind)",
        "type d'événement (jeu de données)",
        None,
        None,
    ),
    "causdata:coded_issue": (
        "string",
        "coded_issue",
        "coded issue at stake (dataset coding, not a cause)",
        "enjeu codé (codage du jeu de données, pas une cause)",
        None,
        None,
    ),
    "causdata:agreement_in_termination_window": (
        "item",
        "agreement_in_termination_window",
        "peace agreement dated in the termination window (REVA pairing, not causal)",
        "accord de paix daté dans la fenêtre de fin (appariement REVA, non causal)",
        "termination_window_agreement_of",
        "agreement dated in the termination window of",
    ),
    "icb:trigger_type": (
        "string",
        "icb_trigger_type",
        "trigger type (ICB TRIGGR/BREAK)",
        "type de déclencheur (ICB)",
        None,
        None,
    ),
    "icb:triggering_entity": (
        "item",
        "icb_triggering_entity",
        "triggering entity (ICB TRIGENT)",
        "entité déclenchante (ICB)",
        "icb_triggered",
        "triggered (ICB TRIGENT)",
    ),
    "icb:triggering_entity_kind": (
        "string",
        "icb_triggering_entity_kind",
        "triggering entity kind (ICB TRIGENT 995/996/997)",
        "nature de l'entité déclenchante (ICB)",
        None,
        None,
    ),
    "icb:trigger_location": (
        "string",
        "icb_trigger_location",
        "external or internal trigger (ICB TRIGLOC)",
        "déclencheur externe ou interne (ICB)",
        None,
        None,
    ),
    "icb:response_type": (
        "string",
        "icb_response_type",
        "major response (ICB MAJRES)",
        "réponse principale (ICB)",
        None,
        None,
    ),
    "icb:gravity": (
        "string",
        "icb_gravity",
        "gravity of value threat (ICB GRAVCR/GRAVTY)",
        "gravité de la menace (ICB)",
        None,
        None,
    ),
    "icb:violence": (
        "string",
        "icb_violence",
        "violence in the crisis (ICB VIOL)",
        "violence de la crise (ICB)",
        None,
        None,
    ),
    "icb:crisis_management": (
        "string",
        "icb_crisis_management",
        "crisis management technique (ICB CRISMG)",
        "technique de gestion de crise (ICB)",
        None,
        None,
    ),
    "icb:outcome_form": (
        "string",
        "icb_outcome_form",
        "form of outcome (ICB FOROUT/OUTFOR)",
        "forme de l'issue (ICB)",
        None,
        None,
    ),
    "icb:outcome_content": (
        "string",
        "icb_outcome_content",
        "content of outcome (ICB SUBOUT/OUTCOM)",
        "contenu de l'issue (ICB)",
        None,
        None,
    ),
    "icb:outcome_satisfaction": (
        "string",
        "icb_outcome_satisfaction",
        "satisfaction about outcome (ICB EXSAT/OUTEVL)",
        "satisfaction quant à l'issue (ICB)",
        None,
        None,
    ),
    "icb:tension_after_outcome": (
        "string",
        "icb_tension_after_outcome",
        "escalation or reduction of tension (ICB OUTESR)",
        "escalade ou réduction des tensions (ICB)",
        None,
        None,
    ),
    "mid:hostility_level": (
        "string",
        "mid_hostility_level",
        "hostility level (MID HostLev)",
        "niveau d'hostilité (MID)",
        None,
        None,
    ),
    "mid:highest_action": (
        "string",
        "mid_highest_action",
        "highest action / action (MID HiAct/Action)",
        "action la plus élevée (MID)",
        None,
        None,
    ),
    "mid:fatality_level": (
        "string",
        "mid_fatality_level",
        "fatality level (MID Fatality)",
        "niveau de pertes (MID)",
        None,
        None,
    ),
    "mid:reciprocated": (
        "string",
        "mid_reciprocated",
        "reciprocated dispute (MID Recip)",
        "différend réciproque (MID)",
        None,
        None,
    ),
    "mid:outcome": ("string", "mid_outcome", "dispute outcome (MID Outcome)", "issue du différend (MID)", None, None),
    "mid:settlement": (
        "string",
        "mid_settlement",
        "settlement of dispute (MID Settle)",
        "règlement du différend (MID)",
        None,
        None,
    ),
    "ucdpterm:outcome": (
        "string",
        "ucdp_termination_outcome",
        "termination outcome (UCDP c_outcome)",
        "issue de la fin d'épisode (UCDP)",
        None,
        None,
    ),
    "ucdppa:agreement_type": (
        "string",
        "ucdp_pa_type",
        "peace agreement type (UCDP pa_type)",
        "type d'accord de paix (UCDP)",
        None,
        None,
    ),
    "ucdppa:inclusive": (
        "string",
        "ucdp_pa_inclusive",
        "comprehensive or dyadic agreement (UCDP inclusive)",
        "accord global ou dyadique (UCDP)",
        None,
        None,
    ),
    "ucdppa:ceasefire_provision": (
        "string",
        "ucdp_pa_ceasefire",
        "ceasefire provision (UCDP cease)",
        "clause de cessez-le-feu (UCDP)",
        None,
        None,
    ),
    "ucdppa:conflict": (
        "item",
        "ucdp_pa_conflict",
        "agreement of conflict (UCDP conflict_id)",
        "accord relatif au conflit (UCDP)",
        "ucdp_pa_of_conflict",
        "has peace agreement (UCDP)",
    ),
    "ucdppa:ended_on": (
        "time",
        "ucdp_pa_ended_on",
        "agreement ended on (UCDP duration)",
        "fin de l'accord (UCDP)",
        None,
        None,
    ),
}
#: Wikidata properties used as context / dates, with WDHARVEST's own definitions (registry/kg/properties.yaml).
WIKIDATA_PIDS = ("P361", "P155", "P580", "P582", "P585", "P710")


def ensure_kg_properties(engine: Any) -> None:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import KgProperty, register_properties

    props = [p for p in kg_properties(load_allowlist()) if p.pid in WIKIDATA_PIDS]
    if len(props) != len(WIKIDATA_PIDS):
        raise ValueError(f"{WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    props += [
        KgProperty(
            pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv, inverse_label_en=ile
        )
        for p, (k, rp, le, lf, inv, ile) in CAUSDATA_PROPERTIES.items()
    ]
    register_properties(engine, props)


def kg_available(engine: Any) -> bool:
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one())


def load_kg(
    engine: Any,
    store: Any,
    entities: Sequence[KgEntity],
    statements: Sequence[KgStatement],
    *,
    source_key: str,
    source_revision: str,
    raw_sha256s: Sequence[str],
    retrieved_at: Any,
    origin: str,
    batch_size: int = 100_000,
) -> dict[str, Any]:
    """Register the predicates, then ``load_batch`` entities and statements in bounded batches."""
    from collections import Counter

    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_STATEMENTS_PER_BATCH, load_batch

    ensure_kg_properties(engine)
    totals: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    sample: list[tuple[str, str | None]] = []
    ents, _ = to_kg_model(entities, (), source_key=source_key, source_revision=source_revision)
    chunks: list[tuple[list[Any], list[Any]]] = [
        (ents[i : i + MAX_ENTITIES_PER_BATCH], []) for i in range(0, len(ents), MAX_ENTITIES_PER_BATCH)
    ]
    step = min(MAX_STATEMENTS_PER_BATCH, batch_size)
    for i in range(0, len(statements), step):
        _, sts = to_kg_model((), statements[i : i + step], source_key=source_key, source_revision=source_revision)
        chunks.append(([], sts))
    for be, bs in chunks:
        used = sorted({s.raw_sha256 for s in bs if s.raw_sha256} or set(raw_sha256s))
        rep = load_batch(
            engine,
            store,
            entities=be,
            statements=bs,
            source_key=source_key,
            raw_sha256s=used,
            retrieved_at=retrieved_at,
            origin=origin,
        )
        for k in (
            "entities_inserted",
            "entities_unchanged",
            "statements_inserted",
            "statements_unchanged",
            "statements_new_revision",
            "statements_conflicting",
            "statements_stale",
        ):
            totals[k] += getattr(rep, k)
        rejected.update(rep.rejected_counts)
        sample.extend((r.reason, r.key) for r in rep.rejected[:5])
    return {**totals, "rejected": dict(rejected), "rejected_sample": sample[:20], "batches": len(chunks)}


def check_unique_keys(statements: Iterable[KgStatement]) -> None:
    seen: set[str] = set()
    for s in statements:
        if s.key in seen:
            raise ValueError(f"duplicate statement key {s.key}")
        seen.add(s.key)


def unmapped_geo(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["iso3"] is None:
            e = seen.setdefault(r["provider_geo"], {"provider_geo": r["provider_geo"], "rows": 0})
            e["rows"] += 1
    return sorted(seen.values(), key=lambda e: e["provider_geo"])


# ----------------------------------------------------------------------------- computed country-year counts


def count_atlas_rows(
    members: Mapping[tuple[str, str, int, int], set[str]],
    lines: Mapping[tuple[str, str, int, int], list[int]],
    *,
    source: str,
    dataset: str,
    crosswalk: Any,
    file: str,
    raw_sha256: str,
    methods: Mapping[str, str],
    computed_from: str,
    geo_prefix: str,
    id_label: str,
) -> list[dict[str, Any]]:
    """Atlas rows of COMPUTED counts: ``members[(indicator, code_system, code, year)]`` = distinct case ids.

    Only (code, year) pairs with at least one case are emitted (the datasets list cases, not countries: absence is
    not a reported zero). The case ids (capped) and the source rows are in flags/selector."""
    from reva.atlas.tables import json_obj, year_period

    out = []
    for (ind, system, code, year), ids in sorted(members.items()):
        res = crosswalk.resolve(system, code, date(year, 7, 1))
        start, end = year_period(year)
        flags: dict[str, Any] = {
            "computed_from": computed_from,
            "method": methods[ind],
            id_label: sorted(ids, key=lambda x: (len(x), x))[:200],
            "code_resolution": res.reason,
        }
        if res.iso3 is None and res.label:
            flags["historical_state"] = res.label
        ln = lines[(ind, system, code, year)]
        out.append(
            {
                "source": source,
                "dataset": dataset,
                "indicator": ind,
                "concept_id": None,
                "iso3": res.iso3,
                "provider_geo": f"{geo_prefix}:{code}",
                "entity_qid": res.qid,
                "period_start": start,
                "period_end": end,
                "value": str(len(ids)),
                "value_text": None,
                "unit": "count",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=file, rows=sorted(set(ln))[:50], rows_total=len(set(ln)), year=year),
            }
        )
    return out


def read_csv_bytes(
    body: bytes, *, name: str, required: Sequence[str], encoding: str = "utf-8-sig"
) -> list[dict[str, str]]:
    """Rows of a CSV body (declared encoding, strict) after checking the header has every required column."""
    import csv
    import io

    try:
        text = body.decode(encoding)
    except UnicodeDecodeError as e:
        raise drift(f"{name} is not {encoding}") from e
    reader = csv.DictReader(io.StringIO(text))
    cols = [c.strip() for c in (reader.fieldnames or [])]
    missing = [c for c in required if c not in cols]
    if missing:
        raise drift(f"{name}: missing columns {missing}")
    rows = []
    for r in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in r.items() if k is not None})
    return rows
