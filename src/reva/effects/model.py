"""Effect estimates: inputs, validation and the append-only loader (``kg.effect_*``, migration 0019).

Contract: ``docs/dev/v5_effect_interface.md``. REVA asserts no effect: every row is attributed to a publication
(citation, locator, verbatim quote, archived source file), a REVA estimation run or an identity.

Identity: ``estimate_key`` + origin. An unchanged reload writes nothing (no load row); a changed estimate is a
new row; an estimate absent from a ``complete`` delivery is retracted (reason given by the caller); one that
comes back is stored again with ``reinstates_row_id``. Every rejection is per estimate with a reason.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from reva.effects import vocab as v
from reva.effects.consistency import side_problem
from reva.effects.variables import Resolver, default_resolver
from reva.kernel.errors import ValidationProblem

LOADER_VERSION = "reva.effects.model/1"
MAX_ESTIMATES_PER_CALL = 20_000
MAX_TEXT = 4_000
MAX_JSON_BYTES = 65_536
MAX_REPORTED = 200
SCOPE_KEYS = (
    "countries",
    "country_group",
    "regions",
    "period",
    "population",
    "frequency",
    "n_obs",
    "n_countries",
    "sample_note",
)
FREQUENCIES = ("annual", "quarterly", "monthly", "daily", "cross_section", "mixed")
PRINTED_TRANSFORMS = (None, "identity", "negated")

_ENC = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _json(o: Any) -> str:
    return _ENC.encode(o)


def _sha(o: Any) -> str:
    return hashlib.sha256(("reva-effects-1\x00" + _json(o)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class Uncertainty:
    se: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    ci_level: float | None = None
    interval_kind: str | None = None
    draws: Mapping[str, Any] | None = None
    status: str = "not_reported"
    se_as_printed: str | None = None


@dataclass(frozen=True)
class Horizon:
    kind: str = "unspecified"
    value: float | None = None
    unit: str | None = None


@dataclass(frozen=True)
class IrfPoint:
    h: float
    unit: str
    point: float
    se: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    as_printed: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("h", "unit", "point", "se", "ci_low", "ci_high", "as_printed")}


@dataclass(frozen=True)
class RevaRun:
    run_id: str
    code_version: str
    data_snapshot: str
    as_of: str
    spec_sha256: str | None = None
    replicates: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "code_version": self.code_version,
            "data_snapshot": self.data_snapshot,
            "as_of": self.as_of,
            "spec_sha256": self.spec_sha256,
            "replicates": list(self.replicates),
        }


@dataclass(frozen=True)
class EffectEstimateIn:
    estimate_id: str
    cause_kind: str
    cause_id: str
    cause_definition: str
    cause_transform: str
    effect_kind: str
    effect_id: str
    effect_definition: str
    effect_transform: str
    measure: str
    unit: str
    sign: str
    method: str
    as_of: str | date
    point: float | None = None
    point_kind: str | None = None
    as_printed: str | None = None
    shock_size: float | None = None
    shock_unit: str | None = None
    uncertainty: Uncertainty = field(default_factory=Uncertainty)
    horizon: Horizon = field(default_factory=Horizon)
    irf: Sequence[IrfPoint] = ()
    scope: Mapping[str, Any] = field(default_factory=dict)
    identification: str | None = None
    meta: Mapping[str, Any] | None = None
    shock_profile: str | None = None  # vocab.SHOCK_PROFILES; stored as meta.shock_profile
    entry_id: str | None = None
    citation: Mapping[str, Any] | None = None
    locator: str | None = None
    quote: str | None = None
    selector: Mapping[str, Any] | None = None
    transcription: str | None = None
    raw_sha256: str | None = None
    run: RevaRun | None = None
    identity: Mapping[str, Any] | None = None
    applicability: str | None = None
    source_caveats: str | None = None
    causal_claim_keys: Sequence[str] = ()
    rights: Mapping[str, Any] | None = None
    curation: Mapping[str, Any] | None = None


@dataclass
class EffectLoadReport:
    origin: str
    load_id: int | None = None
    scanned: int = 0
    inserted: int = 0
    new: int = 0
    new_revision: int = 0
    unchanged: int = 0
    reinstated: int = 0
    retracted: int = 0
    unknown_events: int = 0
    rejected_counts: dict[str, int] = field(default_factory=dict)
    rejected_sample: list[dict[str, Any]] = field(default_factory=list)
    complete_effective: bool = False
    elapsed_s: float = 0.0

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected_counts.values())

    def reject(self, index: int, reason: str, key: str | None = None) -> None:
        self.rejected_counts[reason] = self.rejected_counts.get(reason, 0) + 1
        if len(self.rejected_sample) < MAX_REPORTED:
            self.rejected_sample.append({"index": index, "reason": reason, "estimate_key": key})

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "load_id": self.load_id,
            "scanned": self.scanned,
            "inserted": self.inserted,
            "new": self.new,
            "new_revision": self.new_revision,
            "unchanged": self.unchanged,
            "reinstated": self.reinstated,
            "retracted": self.retracted,
            "unknown_events": self.unknown_events,
            "rejected_total": self.rejected_total,
            "rejected_counts": dict(self.rejected_counts),
            "rejected_sample": list(self.rejected_sample),
            "complete_effective": self.complete_effective,
            "elapsed_s": self.elapsed_s,
        }


class Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------------------------- validation
_DASHES = dict.fromkeys(map(ord, "−–—‐‑‒﹣－"), "-")


def normalise_text(s: str) -> str:
    """NFKC, every dash/minus sign -> '-', whitespace collapsed (the quote-matching normalisation)."""
    t = unicodedata.normalize("NFKC", s).translate(_DASHES)
    return re.sub(r"\s+", " ", t).strip()


def printed_number(s: str) -> float:
    """The value of a number as printed ('−0.41***', '.52', '0.36%', '1,2' is refused)."""
    t = normalise_text(s).replace(" ", "")
    t = t.rstrip("*%")
    if not re.fullmatch(r"[-+]?(\d+(\.\d*)?|\.\d+)", t):
        raise Reject("as_printed_not_a_number")
    return float(t)


def _num(x: Any, reason: str) -> float | None:
    if x is None:
        return None
    if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(float(x)):
        raise Reject(reason)
    return float(x)


def _txt(x: Any, reason: str, hi: int = MAX_TEXT, required: bool = False) -> str | None:
    if x is None:
        if required:
            raise Reject(reason)
        return None
    if not isinstance(x, str) or not x.strip() or len(x) > hi:
        raise Reject(reason)
    return x


def _obj(x: Any, reason: str) -> dict[str, Any] | None:
    if x is None:
        return None
    if not isinstance(x, Mapping):
        raise Reject(reason)
    d = json.loads(_json(dict(x)))
    if len(_json(d)) > MAX_JSON_BYTES:
        raise Reject(reason)
    return d  # type: ignore[no-any-return]


def _meta(e: EffectEstimateIn) -> dict[str, Any] | None:
    meta = _obj(e.meta, "invalid_meta")
    if e.shock_profile is None:
        if meta is not None and meta.get("shock_profile") not in (None, *v.SHOCK_PROFILES):
            raise Reject("invalid_shock_profile")
        return meta
    if e.shock_profile not in v.SHOCK_PROFILES:
        raise Reject("invalid_shock_profile")
    return {**(meta or {}), "shock_profile": e.shock_profile}


def _sign_of(x: float) -> str:
    return "positive" if x > 0 else "negative" if x < 0 else "zero"


def _check_interval(point: float | None, lo: float | None, hi: float | None, reason: str) -> None:
    if (lo is None) != (hi is None):
        raise Reject(f"{reason}_one_bound")
    if lo is not None and hi is not None:
        if lo > hi:
            raise Reject(f"{reason}_bounds_reversed")
        if point is not None and not lo <= point <= hi:
            raise Reject("point_outside_interval")


def _scope(s: Any) -> dict[str, Any]:
    if s is None:
        return {}
    if not isinstance(s, Mapping):
        raise Reject("invalid_scope")
    bad = [k for k in s if k not in SCOPE_KEYS]
    if bad:
        raise Reject("invalid_scope_key")
    out = dict(s)
    cs = out.get("countries")
    if cs is not None:
        if not isinstance(cs, list | tuple) or not all(isinstance(c, str) and v.ISO3_RE.match(c) for c in cs):
            raise Reject("invalid_scope_countries")
        out["countries"] = sorted(set(cs))
    per = out.get("period")
    if per is not None and (
        not isinstance(per, Mapping)
        or set(per) - {"from", "to"}
        or not all(
            isinstance(per.get(k), str) and re.fullmatch(r"-?\d{4}(-\d{2}(-\d{2})?|Q[1-4]|M\d{2})?", per[k])
            for k in per
        )
    ):
        raise Reject("invalid_scope_period")
    if out.get("frequency") is not None and out["frequency"] not in FREQUENCIES:
        raise Reject("invalid_scope_frequency")
    for k in ("n_obs", "n_countries"):
        if out.get(k) is not None and (not isinstance(out[k], int) or isinstance(out[k], bool) or out[k] < 1):
            raise Reject("invalid_scope_count")
    return _obj(out, "invalid_scope") or {}


def _as_of(x: Any) -> str:
    if isinstance(x, datetime):
        return x.date().isoformat()
    if isinstance(x, date):
        return x.isoformat()
    if isinstance(x, str):
        try:
            return date.fromisoformat(x).isoformat()
        except ValueError:
            pass
    raise Reject("invalid_as_of")


def estimate_key(source_kind: str, source_key: str, e: EffectEstimateIn) -> str:
    if not isinstance(e.estimate_id, str) or not v.PART_RE.match(e.estimate_id):
        raise Reject("invalid_estimate_id")
    if source_kind == "publication":
        if not isinstance(e.entry_id, str) or not v.PART_RE.match(e.entry_id):
            raise Reject("invalid_entry_id")
        k = f"pub:{source_key}:{e.entry_id}:{e.estimate_id}"
    elif source_kind == "reva_run":
        if e.run is None or not isinstance(e.run.run_id, str) or not v.PART_RE.match(e.run.run_id):
            raise Reject("run_required")
        k = f"run:{e.run.run_id}:{e.estimate_id}"
    else:
        if not isinstance(e.entry_id, str) or not v.PART_RE.match(e.entry_id):
            raise Reject("invalid_entry_id")
        k = f"idn:{e.entry_id}:{e.estimate_id}"
    if not v.KEY_RE.match(k):
        raise Reject("invalid_estimate_key")
    return k


def validate_estimate(
    e: EffectEstimateIn,
    *,
    source_kind: str,
    source_key: str,
    raws: set[str] | None = None,
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    """Validate one estimate and return its row (column -> value, plus ``estimate_key`` and
    ``content_sha256``). Raises :class:`Reject` with a stable reason. Pure: no database."""
    if not isinstance(e, EffectEstimateIn):
        raise Reject("not_an_estimate")
    res = resolver or default_resolver()
    key = estimate_key(source_kind, source_key, e)
    # cause / effect
    if e.cause_kind not in v.CAUSE_KINDS:
        raise Reject("invalid_cause_kind")
    if e.effect_kind not in v.EFFECT_KINDS:
        raise Reject("invalid_effect_kind")
    cid_re = v.ENTITY_ID_RE if e.cause_kind == "event" else v.VAR_ID_RE
    if not isinstance(e.cause_id, str) or not cid_re.match(e.cause_id) or len(e.cause_id) > 200:
        raise Reject("invalid_cause_id")
    if not isinstance(e.effect_id, str) or not v.VAR_ID_RE.match(e.effect_id):
        raise Reject("invalid_effect_id")
    if not res.known(e.cause_kind, e.cause_id):
        raise Reject("unknown_cause")
    if not res.known(e.effect_kind, e.effect_id):
        raise Reject("unknown_effect")
    if e.cause_transform not in v.TRANSFORMS:
        raise Reject("invalid_cause_transform")
    if e.effect_transform not in v.EFFECT_TRANSFORMS:
        raise Reject("invalid_effect_transform")
    if e.cause_kind in ("event", "event_type") and e.cause_transform != "occurrence":
        raise Reject("event_cause_needs_occurrence")
    _txt(e.cause_definition, "missing_cause_definition", required=True)
    _txt(e.effect_definition, "missing_effect_definition", required=True)
    # measure / unit / shock
    if e.measure not in v.MEASURES:
        raise Reject("invalid_measure")
    if e.unit not in v.UNITS:
        raise Reject("invalid_unit")
    allowed = v.MEASURE_UNITS[e.measure]
    if allowed is not None and e.unit not in allowed:
        raise Reject("unit_not_allowed_for_measure")
    shock = _num(e.shock_size, "invalid_shock_size")
    if shock == 0:
        raise Reject("invalid_shock_size")
    if (shock is None) != (e.shock_unit is None):
        raise Reject("shock_size_and_unit_go_together")
    _txt(e.shock_unit, "invalid_shock_unit", 100)
    if e.measure == "response" and shock is None:
        raise Reject("response_needs_shock")
    # unit / transform consistency with the named variable's own unit (FIX4B, audit M3)
    for side_kind, side_id, side_tr, side_def, is_effect in (
        (e.cause_kind, e.cause_id, e.cause_transform, e.cause_definition, False),
        (e.effect_kind, e.effect_id, e.effect_transform, e.effect_definition, True),
    ):
        is_growth, gvar = res.growth_info(side_kind, side_id)
        why = side_problem(
            transform=side_tr,
            is_growth=is_growth,
            growth_variable=gvar,
            definition=side_def,
            measure=e.measure,
            unit=e.unit,
            is_effect=is_effect,
        )
        if why:
            raise Reject(f"{'effect' if is_effect else 'cause'}_{why}")
    # point / sign
    if e.sign not in v.SIGNS:
        raise Reject("invalid_sign")
    point = _num(e.point, "invalid_point")
    if (point is None) != (e.point_kind is None):
        raise Reject("point_kind_goes_with_point")
    if e.point_kind is not None and e.point_kind not in v.POINT_KINDS:
        raise Reject("invalid_point_kind")
    if point is not None and e.sign != _sign_of(point):
        raise Reject("sign_mismatch")
    curation = _obj(e.curation, "invalid_curation") or {}
    printed_tf = curation.get("printed_transform")
    if printed_tf not in PRINTED_TRANSFORMS:
        raise Reject("invalid_printed_transform")
    if e.as_printed is not None:
        _txt(e.as_printed, "invalid_as_printed", 100)
        if point is not None:
            pv = printed_number(e.as_printed)
            if printed_tf == "negated":
                pv = -pv
            if abs(pv - point) > 1e-9 * max(1.0, abs(point)):
                raise Reject("point_differs_from_as_printed")
    # uncertainty
    u = e.uncertainty
    se = _num(u.se, "invalid_se")
    if se is not None and se <= 0:
        raise Reject("se_not_positive")
    lo, hi = _num(u.ci_low, "invalid_ci"), _num(u.ci_high, "invalid_ci")
    lvl = _num(u.ci_level, "invalid_ci_level")
    if lvl is not None and not 0 < lvl < 1:
        raise Reject("ci_level_out_of_range")
    _check_interval(point, lo, hi, "ci")
    if (lo is None) != (u.interval_kind is None):
        raise Reject("interval_kind_goes_with_bounds")
    if u.interval_kind is not None and u.interval_kind not in v.INTERVAL_KINDS:
        raise Reject("invalid_interval_kind")
    if u.interval_kind in ("confidence", "credible") and lvl is None:
        raise Reject("ci_level_required")
    if lvl is not None and lo is None:
        raise Reject("ci_level_without_interval")
    draws = _obj(u.draws, "invalid_draws")
    if draws is not None:
        if not isinstance(draws.get("raw_sha256"), str) or (raws is not None and draws["raw_sha256"] not in raws):
            raise Reject("draws_not_in_load")
    if u.status not in v.UNCERTAINTY_STATUSES:
        raise Reject("invalid_uncertainty_status")
    if u.status == "reported" and se is None and draws is None and u.interval_kind not in ("confidence", "credible"):
        raise Reject("reported_uncertainty_missing")
    if u.status == "not_reported" and (se is not None or lo is not None or draws is not None):
        raise Reject("uncertainty_given_but_not_reported")
    if u.se_as_printed is not None:
        if se is None or abs(printed_number(u.se_as_printed) - se) > 1e-9 * max(1.0, se):
            raise Reject("se_differs_from_as_printed")
    # horizon / irf
    h = e.horizon
    if h.kind not in v.HORIZON_KINDS:
        raise Reject("invalid_horizon_kind")
    hv = _num(h.value, "invalid_horizon_value")
    if (hv is None) != (h.unit is None):
        raise Reject("horizon_value_and_unit_go_together")
    if hv is not None and hv < 0:
        raise Reject("invalid_horizon_value")
    if h.unit is not None and h.unit not in v.HORIZON_UNITS:
        raise Reject("invalid_horizon_unit")
    if h.kind in ("long_run", "unspecified") and hv is not None:
        raise Reject("horizon_value_not_allowed")
    irf: list[dict[str, Any]] = []
    prev = -1.0
    for p in e.irf:
        if not isinstance(p, IrfPoint) or p.unit not in v.HORIZON_UNITS:
            raise Reject("invalid_irf_point")
        ph, pp = _num(p.h, "invalid_irf_point"), _num(p.point, "invalid_irf_point")
        if ph is None or pp is None or ph < 0 or ph <= prev:
            raise Reject("irf_not_ascending")
        prev = ph
        pse = _num(p.se, "invalid_irf_point")
        if pse is not None and pse <= 0:
            raise Reject("se_not_positive")
        _check_interval(pp, _num(p.ci_low, "invalid_irf_point"), _num(p.ci_high, "invalid_irf_point"), "irf_ci")
        if p.as_printed is not None and abs(
            printed_number(p.as_printed) * (-1 if printed_tf == "negated" else 1) - pp
        ) > 1e-9 * max(1.0, abs(pp)):
            raise Reject("irf_point_differs_from_as_printed")
        irf.append(p.as_dict())
    if len(irf) > 400:
        raise Reject("irf_too_long")
    # method / scope
    if e.method not in v.METHODS:
        raise Reject("invalid_method")
    scope = _scope(e.scope)
    # source
    if source_kind not in v.SOURCE_KINDS:
        raise Reject("invalid_source_kind")
    citation = _obj(e.citation, "invalid_citation")
    selector = _obj(e.selector, "invalid_selector")
    run = e.run.as_dict() if e.run is not None else None
    identity = _obj(e.identity, "invalid_identity")
    transcription = e.transcription or ("computed" if source_kind != "publication" else None)
    if transcription not in v.TRANSCRIPTIONS:
        raise Reject("invalid_transcription")
    quote = _txt(e.quote, "invalid_quote")
    locator = _txt(e.locator, "invalid_locator", 1000)
    if e.raw_sha256 is not None and (
        not isinstance(e.raw_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", e.raw_sha256)
    ):
        raise Reject("invalid_raw_sha256")
    if e.raw_sha256 is not None and raws is not None and e.raw_sha256 not in raws:
        raise Reject("raw_not_in_load")
    if source_kind == "publication":
        if not citation or not str(citation.get("text") or "").strip():
            raise Reject("citation_required")
        if locator is None:
            raise Reject("locator_required")
        if e.raw_sha256 is None:
            raise Reject("raw_sha256_required")
        if transcription == "computed" or run is not None:
            raise Reject("publication_cannot_be_computed")
        if transcription != "machine_readable":
            if quote is None or e.as_printed is None:
                raise Reject("quote_required")
            if normalise_text(e.as_printed) not in normalise_text(quote):
                raise Reject("quote_missing_number")
            if u.se_as_printed is not None and normalise_text(u.se_as_printed) not in normalise_text(quote):
                raise Reject("quote_missing_se")
    if transcription == "machine_readable" and selector is None:
        raise Reject("selector_required")
    if source_kind == "reva_run":
        if run is None or transcription != "computed":
            raise Reject("run_required")
        for k in ("code_version", "data_snapshot", "as_of"):
            if not isinstance(run.get(k), str) or not run[k].strip():
                raise Reject("run_incomplete")
    if source_kind == "identity" and (identity is None or transcription != "computed"):
        raise Reject("identity_required")
    keys = list(e.causal_claim_keys)
    if len(keys) > 100 or not all(isinstance(k, str) and 4 <= len(k) <= 400 for k in keys):
        raise Reject("invalid_causal_claim_keys")
    row: dict[str, Any] = {
        "estimate_key": key,
        "cause_kind": e.cause_kind,
        "cause_id": e.cause_id,
        "cause_definition": e.cause_definition,
        "cause_transform": e.cause_transform,
        "effect_kind": e.effect_kind,
        "effect_id": e.effect_id,
        "effect_definition": e.effect_definition,
        "effect_transform": e.effect_transform,
        "measure": e.measure,
        "unit": e.unit,
        "shock_size": shock,
        "shock_unit": e.shock_unit,
        "sign": e.sign,
        "point": point,
        "point_kind": e.point_kind,
        "as_printed": e.as_printed,
        "se": se,
        "ci_low": lo,
        "ci_high": hi,
        "ci_level": lvl,
        "interval_kind": u.interval_kind,
        "draws": draws,
        "uncertainty_status": u.status,
        "horizon_kind": h.kind,
        "horizon_value": hv,
        "horizon_unit": h.unit,
        "irf": irf,
        "scope": scope,
        "method": e.method,
        "identification": _txt(e.identification, "invalid_identification"),
        "meta": _meta(e),
        "source_kind": source_kind,
        "citation": citation,
        "locator": locator,
        "quote": quote,
        "selector": selector,
        "transcription": transcription,
        "raw_sha256": e.raw_sha256,
        "run": run,
        "identity": identity,
        "as_of": _as_of(e.as_of),
        "applicability": _txt(e.applicability, "invalid_applicability"),
        "source_caveats": _txt(e.source_caveats, "invalid_source_caveats"),
        "causal_claim_keys": sorted(set(keys)),
        "rights": _obj(e.rights, "invalid_rights"),
        "curation": curation,
    }
    row["content_sha256"] = _sha(row)
    return row


# ---------------------------------------------------------------------------------------------- writing
#: column -> SQL cast used when unpacking the staged jsonb rows.
_COLS: tuple[tuple[str, str], ...] = (
    ("estimate_key", "text"),
    ("content_sha256", "text"),
    ("cause_kind", "text"),
    ("cause_id", "text"),
    ("cause_definition", "text"),
    ("cause_transform", "text"),
    ("effect_kind", "text"),
    ("effect_id", "text"),
    ("effect_definition", "text"),
    ("effect_transform", "text"),
    ("measure", "text"),
    ("unit", "text"),
    ("shock_size", "numeric"),
    ("shock_unit", "text"),
    ("sign", "text"),
    ("point", "numeric"),
    ("point_kind", "text"),
    ("as_printed", "text"),
    ("se", "numeric"),
    ("ci_low", "numeric"),
    ("ci_high", "numeric"),
    ("ci_level", "numeric"),
    ("interval_kind", "text"),
    ("draws", "jsonb"),
    ("uncertainty_status", "text"),
    ("horizon_kind", "text"),
    ("horizon_value", "numeric"),
    ("horizon_unit", "text"),
    ("irf", "jsonb"),
    ("scope", "jsonb"),
    ("method", "text"),
    ("identification", "text"),
    ("meta", "jsonb"),
    ("source_kind", "text"),
    ("citation", "jsonb"),
    ("locator", "text"),
    ("quote", "text"),
    ("selector", "jsonb"),
    ("transcription", "text"),
    ("raw_sha256", "text"),
    ("run", "jsonb"),
    ("identity", "jsonb"),
    ("as_of", "date"),
    ("applicability", "text"),
    ("source_caveats", "text"),
    ("causal_claim_keys", "text[]"),
    ("rights", "jsonb"),
    ("curation", "jsonb"),
)


def _select_expr(i: int, col: str, typ: str) -> str:
    if typ == "jsonb":
        return f"NULLIF(x->{i}, 'null'::jsonb) AS {col}"
    if typ == "text[]":
        return f"ARRAY(SELECT jsonb_array_elements_text(x->{i})) AS {col}"
    return f"(x->>{i})::{typ} AS {col}"


_STAGE = ", ".join(_select_expr(i, c, t) for i, (c, t) in enumerate(_COLS))
_NAMES = [c for c, _t in _COLS]


def _latest(conn: Connection, origin: str, source_key: str, dataset: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        text("""SELECT DISTINCT ON (e.estimate_key) e.id, e.estimate_key, e.content_sha256,
                       (r.id IS NOT NULL) AS retracted
                  FROM kg.effect_estimate e LEFT JOIN kg.effect_estimate_retraction r ON r.estimate_row_id = e.id
                 WHERE e.origin = :o AND e.source_key = :sk AND e.dataset = :ds
                 ORDER BY e.estimate_key, e.id DESC"""),
        {"o": origin, "sk": source_key, "ds": dataset},
    ).mappings():
        out[r["estimate_key"]] = dict(r)
    return out


def _db_reason(e: DBAPIError) -> str:
    diag = getattr(e.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return f"db_refused:{name}"[:100]
    msg = getattr(diag, "message_primary", None) or (str(e.orig).splitlines() or ["error"])[0]
    return ("db_refused:" + re.sub(r"\d+", "#", msg))[:100]


def _insert(
    conn: Connection,
    load_id: int,
    *,
    origin: str,
    source_key: str,
    dataset: str,
    retrieved_at: datetime,
    items: Sequence[tuple[dict[str, Any], int | None, str]],
    rep: EffectLoadReport,
) -> None:
    """Insert under a savepoint; a refused batch is split until the refused rows are isolated and rejected."""
    if not items:
        return
    payload = _json([[r[c] for c in _NAMES] + [reinst] for r, reinst, _k in items])
    try:
        with conn.begin_nested():
            n = conn.execute(
                text(f"""INSERT INTO kg.effect_estimate(load_id, origin, source_key, dataset, retrieved_at,
                                                         {", ".join(_NAMES)}, reinstates_row_id)
                     SELECT :load, :o, :sk, :ds, :ret, {", ".join("st." + c for c in _NAMES)}, st.reinstates_row_id
                       FROM (SELECT {_STAGE}, (x->>{len(_NAMES)})::bigint AS reinstates_row_id, ord
                               FROM jsonb_array_elements(CAST(:rows AS jsonb)) WITH ORDINALITY AS t(x, ord)) st
                      ORDER BY st.ord"""),
                {"load": load_id, "o": origin, "sk": source_key, "ds": dataset, "ret": retrieved_at, "rows": payload},
            ).rowcount
    except DBAPIError as exc:
        if len(items) == 1:
            rep.reject(-1, _db_reason(exc), items[0][0]["estimate_key"])
            return
        mid = len(items) // 2
        kw = {"origin": origin, "source_key": source_key, "dataset": dataset, "retrieved_at": retrieved_at, "rep": rep}
        _insert(conn, load_id, items=items[:mid], **kw)  # type: ignore[arg-type]
        _insert(conn, load_id, items=items[mid:], **kw)  # type: ignore[arg-type]
        return
    rep.inserted += int(n)
    for _r, _reinst, kind in items:
        setattr(rep, kind, getattr(rep, kind) + 1)


def _unknown_events(conn: Connection, ids: Iterable[str]) -> int:
    qs = sorted(set(ids))
    if not qs:
        return 0
    known = {
        r[0]
        for r in conn.execute(
            text("SELECT e.qid FROM kg.entity e WHERE e.qid = ANY(:q)"),
            {"q": qs},
        )
    }
    return len([q for q in qs if q not in known])


def load_effect_estimates(
    engine: Engine,
    store: Any,
    *,
    source_kind: str,
    source_key: str,
    dataset: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    estimates: Iterable[EffectEstimateIn],
    complete: bool = True,
    retraction_reason: str = "absent_from_delivery",
    parameters: Mapping[str, Any] | None = None,
    resolver: Resolver | None = None,
) -> EffectLoadReport:
    """Load one delivery of estimates of ``(source_key, dataset)`` in one transaction (interface doc)."""
    t0 = time.perf_counter()
    if source_kind not in v.SOURCE_KINDS:
        raise ValidationProblem("invalid source_kind", source_kind=str(source_kind))
    if not isinstance(source_key, str) or not v.SOURCE_KEY_RE.match(source_key):
        raise ValidationProblem("invalid source_key", source_key=str(source_key)[:100])
    if not isinstance(dataset, str) or not v.SOURCE_KEY_RE.match(dataset):
        raise ValidationProblem("invalid dataset", dataset=str(dataset)[:100])
    if origin not in v.ORIGINS:
        raise ValidationProblem("invalid origin", origin=str(origin))
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None:
        raise ValidationProblem("retrieved_at must be a tz-aware datetime")
    if retraction_reason not in v.RETRACTION_REASONS:
        raise ValidationProblem("invalid retraction_reason", reason=str(retraction_reason))
    raws = list(dict.fromkeys(raw_sha256s))
    if not all(isinstance(s, str) and re.fullmatch(r"[0-9a-f]{64}", s) for s in raws):
        raise ValidationProblem("raw_sha256s must be lowercase hex sha256")
    if source_kind == "publication" and not raws:
        raise ValidationProblem("a publication delivery needs its archived source file(s)")
    items = list(estimates)
    if len(items) > MAX_ESTIMATES_PER_CALL:
        raise ValidationProblem("too many estimates in one call", max_estimates=MAX_ESTIMATES_PER_CALL)
    res = resolver or default_resolver()
    rep = EffectLoadReport(origin=origin)
    rep.scanned = len(items)
    rows: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    raw_set = set(raws)
    for i, e in enumerate(items):
        try:
            row = validate_estimate(e, source_kind=source_kind, source_key=source_key, raws=raw_set, resolver=res)
        except Reject as r:
            rep.reject(i, r.reason, getattr(e, "estimate_id", None))
            continue
        prev = seen.get(row["estimate_key"])
        if prev is not None:
            if prev != row["content_sha256"]:
                rep.reject(i, "conflict_in_batch", row["estimate_key"])
            continue
        seen[row["estimate_key"]] = row["content_sha256"]
        rows.append(row)
    complete_effective = complete and rep.rejected_total == 0
    rep.complete_effective = complete_effective
    from reva.kg.model import _ensure_raw_artifacts

    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"reva:effects:{source_key}:{dataset}:{origin}"},
        )
        _ensure_raw_artifacts(conn, store, raws, source_key)
        rep.unknown_events = _unknown_events(conn, (r["cause_id"] for r in rows if r["cause_kind"] == "event"))
        latest = _latest(conn, origin, source_key, dataset)
        to_insert: list[tuple[dict[str, Any], int | None, str]] = []
        for row in rows:
            lt = latest.get(row["estimate_key"])
            if lt is None:
                to_insert.append((row, None, "new"))
            elif lt["retracted"]:
                to_insert.append((row, lt["id"], "reinstated"))
            elif lt["content_sha256"] == row["content_sha256"]:
                rep.unchanged += 1
            else:
                to_insert.append((row, None, "new_revision"))
        gone = (
            [(lt["id"], k) for k, lt in latest.items() if k not in seen and not lt["retracted"]]
            if complete_effective
            else []
        )
        if not to_insert and not gone:
            rep.elapsed_s = round(time.perf_counter() - t0, 4)
            return rep
        sp = conn.begin_nested()
        load_id = int(
            conn.execute(
                text("""INSERT INTO kg.effect_load(kind, source_key, dataset, origin, raw_sha256s, retrieved_at,
                            loader_version, parameters)
                        VALUES (:k, :sk, :ds, :o, :raws, :ret, :lv, CAST(:par AS jsonb)) RETURNING id"""),
                {
                    "k": source_kind,
                    "sk": source_key,
                    "ds": dataset,
                    "o": origin,
                    "raws": raws,
                    "ret": retrieved_at,
                    "lv": LOADER_VERSION,
                    "par": _json(
                        {
                            **dict(parameters or {}),
                            "complete_requested": complete,
                            "complete_effective": complete_effective,
                            "retraction_reason": retraction_reason if gone else None,
                            "rejected_counts": dict(rep.rejected_counts),
                            "variables_sha256": res.variables.sha256,
                        }
                    ),
                },
            ).scalar_one()
        )
        _insert(
            conn,
            load_id,
            origin=origin,
            source_key=source_key,
            dataset=dataset,
            retrieved_at=retrieved_at,
            items=to_insert,
            rep=rep,
        )
        if gone:
            n = conn.execute(
                text("""INSERT INTO kg.effect_estimate_retraction(load_id, estimate_row_id, estimate_key, origin,
                                                                   reason, detail)
                        SELECT :load, (x->>0)::bigint, x->>1, :o, :reason, CAST(:detail AS jsonb)
                          FROM jsonb_array_elements(CAST(:rows AS jsonb)) x"""),
                {
                    "load": load_id,
                    "o": origin,
                    "reason": retraction_reason,
                    "detail": _json({"basis": "absent_from_complete_delivery", "raw_sha256s": raws}),
                    "rows": _json([list(g) for g in gone]),
                },
            ).rowcount
            rep.retracted += int(n)
        if rep.inserted == 0 and rep.retracted == 0:
            sp.rollback()  # every row refused: no empty load row
        else:
            sp.commit()
            rep.load_id = load_id
    rep.elapsed_s = round(time.perf_counter() - t0, 4)
    return rep
