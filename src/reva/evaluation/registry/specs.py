"""Question families of the forecast registry (``registry/forecasts/question_specs.yaml``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash
from reva.ontology.registry import REGISTRY_ROOT

SPECS_PATH = REGISTRY_ROOT / "forecasts" / "question_specs.yaml"
QuestionType = Literal["quantity", "interval", "binary"]
TYPES = ("quantity", "interval", "binary")
VALUE_RULES = (
    "cell",
    "yoy_pct_from_index",
    "monthly_mean_of_daily",
    "month_end_of_daily",
    "month_end_spread_of_daily",
    "meeting_change",
)
#: ``above_fixed``: strictly above the family's declared ``threshold`` (finance meeting families: a move > 0)
THRESHOLD_RULES = ("above_last_observed", "above_fixed")
FIN_KINDS = ("fx", "yield", "spread", "oil", "meeting")
REFERENCE_FORECASTERS = ("forecast.reference_imf_weo",)
MAX_HORIZON = {"M": 12, "Q": 8, "A": 1}
MIN_HORIZON = {"M": 1, "Q": 1, "A": 0}


@dataclass(frozen=True)
class QuestionRule:
    type: QuestionType
    horizons: tuple[int, ...]
    threshold_rule: str | None = None


@dataclass(frozen=True)
class Geo:
    """A non-country geography (e.g. a world benchmark price): matched by the provider's code verbatim."""

    provider_geo: str
    key: str
    label_fr: str
    label_en: str


@dataclass(frozen=True)
class Family:
    key: str
    concept_id: str | None
    label_fr: str
    label_en: str
    definition: str | None
    definition_source: str | None
    dataset_key: str | None
    indicator: str | None
    indicator_parts: tuple[str, ...]
    frequency: str
    value_rule: str
    unit: str
    countries: tuple[str, ...]
    geos: tuple[Geo, ...]
    questions: tuple[QuestionRule, ...]
    publication_lag_days: int
    void_after_days: int
    backtest_lag_periods: int
    reference_forecasters: tuple[str, ...] = ()
    resolution_note: str | None = None
    owner_agent: str | None = None
    awaiting: str | None = None
    indicator_minus: str | None = None
    threshold: str | None = None
    finance: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def matches_indicator(self, indicator: str) -> bool:
        if self.indicator is not None:
            return indicator == self.indicator
        parts = set(indicator.split("|"))
        return bool(self.indicator_parts) and all(p in parts for p in self.indicator_parts)

    @property
    def runs_generic_baselines(self) -> bool:
        return self.finance is None or bool(self.finance.get("generic_baselines", True))

    @property
    def source(self) -> tuple[str, str] | None:
        """(source, dataset) of the atlas table."""
        if not self.dataset_key:
            return None
        _, src, ds = self.dataset_key.split(".", 2)
        return src, ds


@dataclass(frozen=True)
class SpecSet:
    version: str
    interval_level: float
    families: tuple[Family, ...]
    sha256: str

    def family(self, key: str) -> Family:
        for f in self.families:
            if f.key == key:
                return f
        raise KeyError(key)


def _fail(msg: str, **ctx: Any) -> None:
    raise ValidationProblem(f"question_specs: {msg}", **ctx)


def _family(doc: dict[str, Any], panels: dict[str, list[str]]) -> Family:
    key = str(doc.get("key") or "")
    if not key:
        _fail("family without key")
    freq = str(doc.get("frequency"))
    if freq not in MAX_HORIZON:
        _fail("frequency must be M, Q or A", family=key)
    rule = doc.get("value_rule", "cell")
    if rule not in VALUE_RULES:
        _fail("unknown value_rule", family=key, value_rule=rule)
    dk = doc.get("dataset_key")
    if dk is not None and (not isinstance(dk, str) or not dk.startswith("atlas.") or dk.count(".") < 2):
        _fail("dataset_key must be atlas.<source>.<dataset>", family=key)
    if dk is None and not doc.get("awaiting"):
        _fail("a family without dataset_key must say what it is awaiting", family=key)
    if dk is not None and not (doc.get("indicator") or doc.get("indicator_parts")):
        _fail("indicator or indicator_parts required", family=key)
    qs: list[QuestionRule] = []
    for q in doc.get("questions") or []:
        t = q.get("type")
        if t not in TYPES:
            _fail("unknown question type", family=key, type=t)
        hs = tuple(int(h) for h in q.get("horizons") or [])
        if not hs or any(h < MIN_HORIZON[freq] or h > MAX_HORIZON[freq] for h in hs):
            _fail("horizons outside the registry's range", family=key, horizons=list(hs))
        tr = q.get("threshold_rule")
        if (t == "binary") != (tr is not None) or (tr is not None and tr not in THRESHOLD_RULES):
            _fail("binary questions (only) need a known threshold_rule", family=key)
        qs.append(QuestionRule(t, hs, tr))
    if not qs:
        _fail("family without questions", family=key)
    if rule == "month_end_spread_of_daily" and not doc.get("indicator_minus"):
        _fail("month_end_spread_of_daily needs indicator_minus", family=key)
    if any(q.threshold_rule == "above_fixed" for q in qs) and doc.get("threshold") is None:
        _fail("threshold_rule above_fixed needs a family threshold", family=key)
    fin = doc.get("finance")
    if fin is not None:
        _check_finance(key, rule, fin)
    elif rule == "meeting_change":
        _fail("meeting_change families need a finance block", family=key)
    refs = tuple(doc.get("reference_forecasters") or ())
    if any(r not in REFERENCE_FORECASTERS for r in refs):
        _fail("unknown reference forecaster", family=key)
    panel = doc.get("panel")
    countries = tuple(panels[panel]) if panel else tuple(doc.get("countries") or ())
    if panel and panel not in panels:
        _fail("unknown panel", family=key, panel=panel)
    geos = tuple(
        Geo(str(g["provider_geo"]), str(g["key"]), str(g["label_fr"]), str(g["label_en"]))
        for g in doc.get("geos") or ()
    )
    labels = doc.get("labels") or {}
    return Family(
        key=key,
        concept_id=doc.get("concept_id"),
        label_fr=str(labels.get("fr") or key),
        label_en=str(labels.get("en") or key),
        definition=doc.get("definition"),
        definition_source=doc.get("definition_source"),
        dataset_key=dk,
        indicator=doc.get("indicator"),
        indicator_parts=tuple(doc.get("indicator_parts") or ()),
        frequency=str(freq),
        value_rule=str(rule),
        unit=str(doc.get("unit") or ""),
        countries=countries,
        geos=geos,
        questions=tuple(qs),
        publication_lag_days=int(doc["publication_lag_days"]),
        void_after_days=int(doc["void_after_days"]),
        backtest_lag_periods=int(doc.get("backtest_lag_periods", 1)),
        reference_forecasters=refs,
        resolution_note=doc.get("resolution_note"),
        owner_agent=doc.get("owner_agent"),
        awaiting=doc.get("awaiting"),
        indicator_minus=doc.get("indicator_minus"),
        threshold=None if doc.get("threshold") is None else str(doc.get("threshold")),
        finance=dict(fin) if fin is not None else None,
        raw=dict(doc),
    )


def _check_finance(key: str, rule: str, fin: Any) -> None:
    from reva.models.forecasting.finance_models import FIN_MODELS

    if not isinstance(fin, dict):
        _fail("finance must be a mapping", family=key)
    kind = fin.get("kind")
    if kind not in FIN_KINDS:
        _fail("unknown finance kind", family=key, kind=kind)
    if (kind == "meeting") != (rule == "meeting_change"):
        _fail("finance kind meeting <=> value_rule meeting_change", family=key)
    if kind != "meeting" and fin.get("scale") not in ("log", "level"):
        _fail("finance scale must be log or level", family=key)
    for group in ("forecasters", "candidates"):
        ms = fin.get(group) or []
        if any(m not in FIN_MODELS for m in ms):
            _fail(f"unknown finance model in {group}", family=key)
    if not fin.get("forecasters"):
        _fail("a finance family needs forecasters", family=key)
    for name, inp in (fin.get("inputs") or {}).items():
        dk = inp.get("dataset_key") if isinstance(inp, dict) else None
        if not isinstance(dk, str) or not dk.startswith("atlas.") or dk.count(".") < 2:
            _fail("finance input dataset_key must be atlas.<source>.<dataset>", family=key, input=name)
        if not (inp.get("indicator") or inp.get("indicator_prefix")) or not inp.get("provider_geo"):
            _fail("finance input needs indicator (or indicator_prefix) and provider_geo", family=key, input=name)
    if kind == "meeting":
        cal = fin.get("calendar") or {}
        if not all(cal.get(k) for k in ("dataset_key", "indicator", "provider_geo")):
            _fail("meeting family needs calendar dataset_key / indicator / provider_geo", family=key)
        if fin.get("direction") not in ("up", "down"):
            _fail("meeting direction must be up or down", family=key)
        if "history" not in (fin.get("inputs") or {}):
            _fail("meeting family needs a history input", family=key)
    if kind in ("yield", "spread") and fin.get("maturity") is None:
        _fail("yield/spread family needs maturity", family=key)
    if kind == "spread" and fin.get("minus_maturity") is None:
        _fail("spread family needs minus_maturity", family=key)


def parse_specs(doc: dict[str, Any]) -> SpecSet:
    if doc.get("schema") != "reva-forecast-question-specs/1":
        _fail("unknown schema")
    panels = {str(k): [str(c) for c in v] for k, v in (doc.get("panels") or {}).items()}
    fams = tuple(_family(f, panels) for f in doc.get("families") or [])
    keys = [f.key for f in fams]
    if len(set(keys)) != len(keys):
        _fail("duplicate family keys")
    level = float(doc.get("interval_level", 0.8))
    if not 0.0 < level < 1.0:
        _fail("interval_level must be in (0, 1)")
    return SpecSet(str(doc.get("version")), level, fams, logical_hash(doc))


def load_specs(path: Path = SPECS_PATH) -> SpecSet:
    return parse_specs(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def cached_specs() -> SpecSet:
    return load_specs()
