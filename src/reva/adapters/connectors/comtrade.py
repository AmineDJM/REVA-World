"""UN Comtrade public-preview connector (template comtrade_public_exact).

Exact, bounded annual goods/HS/2709 requests. Accepted only when every returned
dimension matches the requested key, the record count equals the payload length
and is below maxRecords (no truncation), and keys are unique. Anything ambiguous
blocks publication. A missing flow is Missing, never zero. CIF, FOB and
primaryValue are preserved separately; quantities/weights are never derived from
values, and zero/null unusable weights stay Missing.

1.1.0 (parser comtrade-parse-2): import and export net weights map to the
flow-specific concepts ``trade.hs2709.{import,export}_net_weight.world_total``
(1.0.0 emitted both flows under one concept, collapsing two quantities); the
valuation label is inferred per flow (exports are FOB; a CIF label on an export
is impossible); value bases record the exchange-rate transform.

1.2.0 (parser comtrade-parse-3): estimation flags follow the UNSD coding of
``legacyEstimationFlag`` (0 none, 2 quantity, 4 net weight, 6 both estimated;
UNSD trade data processing method, 2019), which concerns quantity/weight, never
the USD value. Value observations carry ``provider_value_estimated``, which is
``null`` (undeterminable): no field of the public-preview response marks the
value itself as estimated (``isReported=false`` holds for every world-total
row and is kept raw). Net-weight observations carry
``provider_quantity_estimated`` (``isQtyEstimated``/``isNetWgtEstimated`` or
codes 2/4/6). The 1.1.0 flag ``provider_estimation_flagged`` (code != 0, read
by consumers as an estimated value) is no longer emitted; readers treat it on
old rows as quantity estimation. Raw provider flags are kept unchanged. Values,
concepts and logical keys are unchanged (minor bump). Validation now also blocks
structural drift (below).

Structural drift blocks (the acquisition is quarantined; nothing is coerced):
a row lacking a consumed field (e.g. ``primaryValue`` renamed) is a schema
change, not a missing flow -- only a present ``null`` is a provider Missing;
a value field that is not a JSON number or null (e.g. a string-typed number)
is a type drift; a ``classificationCode`` outside the reviewed mapping's
applicability (``REVIEWED_CLASSIFICATIONS``, mirroring
registry/mappings/comtrade.yaml) needs a reviewed mapping first. A silent
rescale (e.g. every value divided by 1000 with no marker change) keeps types,
units and dimensions and cannot be told apart from a genuine revision in one
payload; it is detected by the repair advisor's comparison with the accepted
vintage of the same request (``reva.autonomy.repair.value_comparison``) and,
within a run, only surfaces as the adjacent-year ``implausible_jump`` review
signal. Validation adds no magnitude heuristic that could reject real revisions.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from decimal import Decimal
from typing import Any

from reva.adapters.connectors.base import (
    dec_str,
    json_decimal,
    module_digest,
    plain,
    schema_fingerprint,
    to_raw,
)
from reva.adapters.connectors.worldbank import _report, require_json_like
from reva.kernel.errors import SourceResponseError
from reva.kernel.support import PopulationRef, SpatialRef, Support
from reva.kernel.time import TimeExtent
from reva.kernel.uncertainty import UncertaintySpec
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchOutcome,
    FetchPlan,
    Finding,
    ObservationBatch,
    ParsedObservation,
    QualityReport,
    RawAcquisition,
    RelationSpec,
)

BASE = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
MAX_RECORDS = 500
# Provider reporter codes, verified against the provider reporter catalogue
# (Reporters.json) on 2026-10-14. Not UN M49 (France M49=250, Comtrade=251).
REPORTERS: dict[str, int] = {"FRA": 251, "DZA": 12, "CHN": 156, "USA": 842}
FLOW_CONCEPT = {
    ("M", "total"): "trade.hs2709.import_value.world_total",
    ("X", "total"): "trade.hs2709.export_value.world_total",
    ("M", "bilateral"): "trade.hs2709.import_value.bilateral",
    ("X", "bilateral"): "trade.hs2709.export_value.bilateral",
}
WEIGHT_CONCEPT = {
    "M": "trade.hs2709.import_net_weight.world_total",
    "X": "trade.hs2709.export_net_weight.world_total",
}
CONNECTOR_VERSION = "1.2.0"
PARSER_VERSION = "comtrade-parse-3"
JUMP_RATIO = Decimal(100)
VALUATION_UNDETERMINED = "primaryValue (basis not determinable from response)"
EXCHANGE_RATE_TRANSFORM = "provider_reported_usd (UN Comtrade methodology)"
FIXED_DIMS = {
    "typeCode": "C",
    "freqCode": "A",
    "cmdCode": "2709",
    "customsCode": "C00",
    "motCode": 0,
    "partner2Code": 0,
}
DIM_FIELDS = (
    "period",
    "reporterCode",
    "flowCode",
    "partnerCode",
    "partner2Code",
    "cmdCode",
    "customsCode",
    "motCode",
    "typeCode",
    "freqCode",
    "classificationCode",
)
# Value fields the parser publishes or derives the valuation label from: each must be
# present in every row (null allowed = provider Missing) and a JSON number when not null.
VALUE_FIELDS = ("primaryValue", "cifvalue", "fobvalue", "netWgt")
# HS editions for which the reviewed HS2709 mapping applies (registry/mappings/comtrade.yaml
# applicability.classifications); an edition outside it (e.g. H7) is blocked until reviewed.
REVIEWED_CLASSIFICATIONS = ("H4", "H5", "H6")
# UNSD legacy estimation codes: (quantity estimated, net weight estimated). None of them
# marks the trade value as estimated.
LEGACY_ESTIMATION_CODES: dict[int, tuple[bool, bool]] = {
    0: (False, False),
    2: (True, False),
    4: (False, True),
    6: (True, True),
}


def _legacy_code(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        return None
    return int(d) if d == d.to_integral_value() else None


def _quantity_estimated(r: dict[str, Any]) -> tuple[bool, bool]:
    """(quantity and/or net weight estimated by the provider, legacy code recognised)."""
    raw = r.get("legacyEstimationFlag")
    code = _legacy_code(raw)
    known = LEGACY_ESTIMATION_CODES.get(code) if code is not None else None
    est = r.get("isQtyEstimated") is True or r.get("isNetWgtEstimated") is True or bool(known and any(known))
    return est, raw is None or known is not None


def _nonzero(v: Any) -> bool:
    if v is None or v == "":
        return False
    try:
        return Decimal(str(v)) != 0
    except ArithmeticError:
        return False


class ComtradeConnector:
    def __init__(
        self,
        reporters: Sequence[str] = ("FRA", "CHN", "USA", "DZA"),
        total_years: Sequence[int] = (2020, 2021, 2022, 2023, 2024),
        bilateral_years: Sequence[int] = (2023,),
    ) -> None:
        self.reporters = tuple(reporters)
        self.total_years = tuple(total_years)
        self.bilateral_years = tuple(bilateral_years)
        # Values validated in this run, per (reporter, flow, field) series, for the
        # adjacent-year plausibility review signal (spec §20.2: outliers are review
        # signals, never deletions).
        self._series: dict[tuple[str, str, str], dict[int, Decimal]] = {}

    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name="un.comtrade.public_exact",
            version=CONNECTOR_VERSION,
            template="comtrade_public_exact",
            source_key="un.comtrade.public_preview",
            provider="United Nations Statistics Division",
            licence="UN Comtrade terms of use",
            allowed_hosts=("comtradeapi.un.org",),
            dataset_ids=("C/A/HS/2709",),
            field_mappings={
                **{f"{k[0]}:{k[1]}": v for k, v in FLOW_CONCEPT.items()},
                **{f"{k}:netWgt:total": v for k, v in WEIGHT_CONCEPT.items()},
            },
            units={"primaryValue": "unit.usd", "netWgt": "unit.kg"},
            geography=self.reporters,
            time_semantics="annual calendar-year flow totals",
            lineage_family="un_comtrade",
            cadence_seconds=30 * 86400,
            revision_strategy="append revision on changed latest state; rolling recent-year rechecks",
            max_response_bytes=5_000_000,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(ComtradeConnector),
        )

    def _url(self, reporter: int, flow: str, years: Sequence[int], partner: int | None) -> str:
        url = f"{BASE}?period={','.join(map(str, years))}&reporterCode={reporter}&cmdCode=2709&flowCode={flow}"
        if partner is not None:
            url += f"&partnerCode={partner}"
        return url + f"&partner2Code=0&customsCode=C00&motCode=0&maxRecords={MAX_RECORDS}"

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        items: list[FetchItem] = []
        for iso in self.reporters:
            code = REPORTERS[iso]
            for flow in ("M", "X"):
                # The public preview accepts exactly one period per request.
                for y in self.total_years:
                    items.append(
                        FetchItem(
                            key=f"total:{iso}:{flow}:{y}",
                            url=self._url(code, flow, (y,), 0),
                            max_bytes=2_000_000,
                            params={"scope": "total", "iso": iso, "reporter": code, "flow": flow, "years": [y]},
                        )
                    )
                for y in self.bilateral_years:
                    items.append(
                        FetchItem(
                            key=f"bilateral:{iso}:{flow}:{y}",
                            url=self._url(code, flow, (y,), None),
                            max_bytes=5_000_000,
                            params={"scope": "bilateral", "iso": iso, "reporter": code, "flow": flow, "years": [y]},
                            required=False,
                        )
                    )
        return FetchPlan(
            connector="un.comtrade.public_exact",
            items=tuple(items[: budget.max_requests]),
            pagination="none: exact keys with maxRecords bound; count must be < maxRecords",
            availability_expectation="annual data released with lags; revisions possible for recent years",
            budget_exhausted=len(items) > budget.max_requests,
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        require_json_like(raw)
        payload = json_decimal(raw.body)
        if not isinstance(payload, dict) or "data" not in payload:
            raise SourceResponseError("unexpected Comtrade payload shape", retryable=False, kind="schema_drift")
        if payload.get("error"):
            raise SourceResponseError(f"Comtrade API error: {payload['error']}", retryable=False)
        rows: list[dict[str, Any]] = payload.get("data") or []
        p = raw.item.params
        meta = {
            "count": plain(payload.get("count")),
            "elapsedTime": payload.get("elapsedTime"),
            "returned_rows": len(rows),
            "request": {k: p[k] for k in ("scope", "iso", "reporter", "flow", "years")},
            "maxRecords": MAX_RECORDS,
        }
        obs: list[ParsedObservation] = []
        if p["scope"] == "total":
            by_year = {int(str(r.get("period"))): r for r in rows}
            for year in p["years"]:
                r = by_year.get(int(year))
                obs.append(self._total(p, int(year), r, "value"))
                obs.append(self._total(p, int(year), r, "weight"))
            expected = tuple(o.logical_key for o in obs)
        else:
            for r in rows:
                if int(r.get("partnerCode", -1)) == 0:
                    continue  # World total is a distinct support captured by the total query
                obs.append(self._bilateral(p, r))
            expected = ()
        yield ObservationBatch(
            connector="un.comtrade.public_exact",
            connector_version=CONNECTOR_VERSION,
            item_key=raw.item.key,
            observations=tuple(obs),
            expected_keys=expected,
            metadata={
                **meta,
                "rows": [self._dims(r) for r in rows][:MAX_RECORDS],
                "row_structure": _row_structure(rows),
            },
            schema_fingerprint=schema_fingerprint(rows[:5]),
        )

    @staticmethod
    def _dims(r: dict[str, Any]) -> dict[str, Any]:
        return {k: plain(r.get(k)) for k in DIM_FIELDS}

    def _flags(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "classificationCode": r.get("classificationCode"),
            "isOriginalClassification": r.get("isOriginalClassification"),
            "isReported": r.get("isReported"),
            "isAggregate": r.get("isAggregate"),
            "legacyEstimationFlag": plain(r.get("legacyEstimationFlag")),
            "primaryValue": plain(r.get("primaryValue")),
            "cifvalue": plain(r.get("cifvalue")),
            "fobvalue": plain(r.get("fobvalue")),
            "netWgt": plain(r.get("netWgt")),
            "isNetWgtEstimated": r.get("isNetWgtEstimated"),
            "qtyUnitCode": plain(r.get("qtyUnitCode")),
            "qty": plain(r.get("qty")),
            "isQtyEstimated": r.get("isQtyEstimated"),
        }

    def _value_flags(self, r: dict[str, Any]) -> dict[str, Any]:
        flags = self._flags(r)
        # No response field marks the USD value as estimated (legacyEstimationFlag codes
        # quantity/weight only): undeterminable, never claimed either way.
        flags["provider_value_estimated"] = None
        if not _quantity_estimated(r)[1]:
            flags["legacy_estimation_code_unrecognised"] = True
        return flags

    def _weight_flags(self, r: dict[str, Any]) -> dict[str, Any]:
        flags = self._flags(r)
        est, known = _quantity_estimated(r)
        flags["provider_quantity_estimated"] = est
        if not known:
            flags["legacy_estimation_code_unrecognised"] = True
        return flags

    @staticmethod
    def _valuation(flow: str, r: dict[str, Any]) -> tuple[str, str | None]:
        """Flow-aware valuation label and an optional quality warning code.

        Imports may be reported CIF or FOB: the label is CIF/FOB only when
        primaryValue matches exactly one of them. Exports are valued FOB, so an
        export whose primaryValue matches fobvalue is FOB; an export matching only
        cifvalue would carry an impossible CIF label and is reported as
        undetermined with a warning.
        """
        pv, cif, fob = r.get("primaryValue"), r.get("cifvalue"), r.get("fobvalue")
        # A 0 cifvalue/fobvalue (seen for CHN/USA 2020-21) is kept as a flag only: it never
        # "matches" primaryValue, not even when primaryValue is itself 0.
        eq_cif = _nonzero(pv) and _nonzero(cif) and Decimal(str(pv)) == Decimal(str(cif))
        eq_fob = _nonzero(pv) and _nonzero(fob) and Decimal(str(pv)) == Decimal(str(fob))
        if flow == "M":
            if eq_cif and not eq_fob:
                return "CIF", None
            if eq_fob and not eq_cif:
                return "FOB", None
            return VALUATION_UNDETERMINED, ("valuation_ambiguous" if eq_cif and eq_fob else None)
        if eq_fob:
            return "FOB", None
        if eq_cif:
            return VALUATION_UNDETERMINED, "export_cif_label_impossible"
        return VALUATION_UNDETERMINED, None

    def _value_basis(self, flow: str, r: dict[str, Any], flags: dict[str, Any]) -> dict[str, Any]:
        valuation, warning = self._valuation(flow, r)
        if warning:
            flags["valuation_warning"] = warning
        return {
            "currency": "USD",
            "price_basis": "nominal",
            "scale_factor": "1",
            "valuation": valuation,
            "exchange_rate_transform": EXCHANGE_RATE_TRANSFORM,
        }

    def _support(self, iso: str, year: int, partner: str) -> Support:
        return Support(
            temporal=TimeExtent.calendar_year(year),
            spatial=SpatialRef(
                scheme="provider_reporter_territory" if partner == "0" else "provider_reporter_partner_pair",
                key=f"COMTRADE:{REPORTERS[iso]}" + ("" if partner == "0" else f"->{partner}"),
                authority="UN Comtrade",
                note="reporter territory as defined by provider reporterNote",
            ),
            population=PopulationRef(universe="not_applicable"),
            aggregation="period_flow_total",
            resolution={"classification": "HS", "heading": "2709", "partner": partner},
        )

    def _total(self, p: dict[str, Any], year: int, r: dict[str, Any] | None, what: str) -> ParsedObservation:
        iso, flow = p["iso"], p["flow"]
        support = self._support(iso, year, "0")
        if what == "value":
            concept = FLOW_CONCEPT[(flow, "total")]
            lk = f"comtrade:HS2709:{flow}:{REPORTERS[iso]}:0:{year}:primaryValue"
            if r is None:
                value: dict[str, Any] = {"kind": "missing", "reason": "unavailable"}
                flags: dict[str, Any] = {"provider_returned_no_rows": True}
            elif r.get("primaryValue") is None:
                value = {"kind": "missing", "reason": "unavailable"}
                flags = self._value_flags(r)
            else:
                flags = self._value_flags(r)
                value = {
                    "kind": "decimal",
                    "value": dec_str(r["primaryValue"]),
                    "unit_ref": "unit.usd",
                    "basis": self._value_basis(flow, r, flags),
                }
        else:
            concept = WEIGHT_CONCEPT[flow]
            lk = f"comtrade:HS2709:{flow}:{REPORTERS[iso]}:0:{year}:netWgt"
            if r is None:
                value, flags = {"kind": "missing", "reason": "unavailable"}, {"provider_returned_no_rows": True}
            else:
                flags = self._weight_flags(r)
                w = r.get("netWgt")
                if w is None or Decimal(w) <= 0:
                    # null or zero weight alongside a positive value is unusable, never a measurement
                    value = {"kind": "missing", "reason": "unavailable"}
                    flags["weight_unusable"] = True
                else:
                    value = {
                        "kind": "decimal",
                        "value": dec_str(w),
                        "unit_ref": "unit.kg",
                        "basis": {"scale_factor": "1"},
                    }
        flags["flowCode"] = flow
        return ParsedObservation(
            subject_provider="comtrade_reporter",
            subject_key=str(REPORTERS[iso]),
            concept_id=concept,
            concept_version="1.0.0",
            support=support,
            logical_key=lk,
            value=value,
            uncertainty=UncertaintySpec.unknown("UN Comtrade publishes no uncertainty for this value"),
            source_flags=flags,
            raw_selector={
                "match": {"period": str(year), "partnerCode": 0, "flowCode": flow},
                "field": "primaryValue" if what == "value" else "netWgt",
            },
        )

    def _bilateral(self, p: dict[str, Any], r: dict[str, Any]) -> ParsedObservation:
        iso, flow = p["iso"], p["flow"]
        year = int(str(r.get("period")))
        partner = str(plain(r.get("partnerCode")))
        v = r.get("primaryValue")
        flags = {**self._value_flags(r), "flowCode": flow, "partnerCode": partner}
        value: dict[str, Any] = (
            {"kind": "missing", "reason": "unavailable"}
            if v is None
            else {
                "kind": "decimal",
                "value": dec_str(v),
                "unit_ref": "unit.usd",
                "basis": self._value_basis(flow, r, flags),
            }
        )
        reporter_dir, partner_dir = ("in", "out") if flow == "M" else ("out", "in")
        return ParsedObservation(
            subject_provider="comtrade_reporter",
            subject_key=str(REPORTERS[iso]),
            concept_id=FLOW_CONCEPT[(flow, "bilateral")],
            concept_version="1.0.0",
            support=self._support(iso, year, partner),
            logical_key=f"comtrade:HS2709:{flow}:{REPORTERS[iso]}:{partner}:{year}:primaryValue",
            value=value,
            uncertainty=UncertaintySpec.unknown("UN Comtrade publishes no uncertainty for this value"),
            source_flags=flags,
            raw_selector={
                "match": {"period": str(year), "partnerCode": int(partner), "flowCode": flow},
                "field": "primaryValue",
            },
            relation=RelationSpec(
                predicate="predicate.trade_flow",
                layer="trade.crude_petroleum",
                participants=(
                    {
                        "role": "role.reporter",
                        "provider": "comtrade_reporter",
                        "key": str(REPORTERS[iso]),
                        "direction": reporter_dir,
                    },
                    {"role": "role.partner", "provider": "comtrade_partner", "key": partner, "direction": partner_dir},
                    {"role": "role.product", "provider": "hs_heading", "key": "2709", "direction": "undirected"},
                ),
                component={
                    "flow": flow,
                    "valuation": value.get("basis", {}).get("valuation"),
                    "classification": r.get("classificationCode"),
                },
            ),
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        findings: list[Finding] = []
        meta = batch.metadata
        req = meta["request"]
        count = meta.get("count")
        n = meta["returned_rows"]
        if count is None or int(count) != n:
            findings.append(
                Finding(code="count_mismatch", severity="blocker", message=f"count={count} but {n} rows returned")
            )
        if n >= MAX_RECORDS:
            findings.append(
                Finding(
                    code="possible_truncation",
                    severity="blocker",
                    message="row count reached maxRecords; preview truncation cannot be excluded",
                )
            )
        seen = set()
        for d in meta["rows"]:
            want = {**FIXED_DIMS, "reporterCode": req["reporter"], "flowCode": req["flow"]}
            if req["scope"] == "total":
                want["partnerCode"] = 0
            for k, v in want.items():
                if str(d.get(k)) != str(v):
                    findings.append(
                        Finding(
                            code="dimension_mismatch",
                            severity="blocker",
                            message=f"{k}={d.get(k)} differs from requested {v}",
                        )
                    )
            if int(str(d.get("period"))) not in [int(y) for y in req["years"]]:
                findings.append(
                    Finding(
                        code="dimension_mismatch", severity="blocker", message=f"unexpected period {d.get('period')}"
                    )
                )
            key = (d.get("period"), d.get("partnerCode"), d.get("classificationCode"))
            dims_key = (d.get("period"), d.get("partnerCode"))
            if dims_key in seen:
                findings.append(
                    Finding(code="ambiguous_duplicate", severity="blocker", message=f"duplicate rows for {key}")
                )
            seen.add(dims_key)
        findings.extend(_structure_findings(meta))
        missing = [
            o.logical_key
            for o in batch.observations
            if o.value.get("kind") == "missing" and o.logical_key.endswith("primaryValue")
        ]
        if missing:
            findings.append(
                Finding(
                    code="missing_flow",
                    severity="warning",
                    message=f"{len(missing)} requested flows returned no value (kept Missing)",
                )
            )
        undetermined = [o for o in batch.observations if o.source_flags.get("valuation_warning")]
        if undetermined:
            codes = sorted({str(o.source_flags["valuation_warning"]) for o in undetermined})
            findings.append(
                Finding(
                    code="valuation_basis_undetermined",
                    severity="warning",
                    message=f"{len(undetermined)} values carry an undeterminable valuation basis "
                    f"({', '.join(codes)}); labelled '{VALUATION_UNDETERMINED}'",
                )
            )
        estimated = [o for o in batch.observations if o.source_flags.get("provider_quantity_estimated")]
        if estimated:
            findings.append(
                Finding(
                    code="provider_quantity_estimated",
                    severity="info",
                    message=f"{len(estimated)} net weights are estimated by the provider "
                    "(isQtyEstimated/isNetWgtEstimated, legacyEstimationFlag 2/4/6); "
                    "trade values are as reported",
                )
            )
        unknown = sorted(
            {
                str(o.source_flags.get("legacyEstimationFlag"))
                for o in batch.observations
                if o.source_flags.get("legacy_estimation_code_unrecognised")
            }
        )
        if unknown:
            findings.append(
                Finding(
                    code="estimation_code_unrecognised",
                    severity="warning",
                    message=f"legacyEstimationFlag {', '.join(unknown)} is outside the UNSD codes "
                    "0/2/4/6; estimation status of these rows needs review",
                )
            )
        findings.extend(self._jump_signals(batch))
        return _report(findings, len(batch.expected_keys), len(batch.observations), ())

    def _jump_signals(self, batch: ObservationBatch) -> list[Finding]:
        """Warn (never block, never delete) when a world-total value differs by more than
        JUMP_RATIO from the adjacent year of the same series seen in this run (e.g. a
        reporter's crude imports falling from billions to a few thousand dollars)."""
        out: list[Finding] = []
        for o in batch.observations:
            parts = o.logical_key.split(":")  # comtrade:HS2709:{flow}:{reporter}:{partner}:{year}:{field}
            if len(parts) != 7 or parts[4] != "0" or o.value.get("kind") != "decimal":
                continue
            v = Decimal(str(o.value["value"]))
            if v <= 0:
                continue
            series = self._series.setdefault((parts[3], parts[2], parts[6]), {})
            year = int(parts[5])
            series[year] = v
            for adj in (year - 1, year + 1):
                w = series.get(adj)
                if w is None or w <= 0:
                    continue
                ratio = max(v, w) / min(v, w)
                if ratio > JUMP_RATIO:
                    out.append(
                        Finding(
                            code="implausible_jump",
                            severity="warning",
                            message=f"{o.logical_key}: {v} vs {w} in {adj} (x{ratio:.0f} > x{JUMP_RATIO}); "
                            "review signal only, value kept as reported",
                        )
                    )
        return out

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        required = {i.key for i in plan.items if i.required}
        ok = all(o.status in ("published", "unchanged") for o in outcomes if o.item_key in required)
        return Cursor(state={"complete": ok and not plan.budget_exhausted, "recheck_years": list(self.total_years)})


def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, Decimal | int | float):
        return "number"
    return "string" if isinstance(v, str) else type(v).__name__


def _row_structure(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """Per-row structural facts, recorded only for rows that deviate (absent keys, non-numeric values)."""
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            out.append({"row": i, "not_an_object": True})
            continue
        absent = [k for k in (*DIM_FIELDS, *VALUE_FIELDS) if k not in r]
        types = {k: _json_type(r[k]) for k in VALUE_FIELDS if k in r and _json_type(r[k]) not in ("number", "null")}
        if absent or types:
            out.append({"row": i, "absent": absent, "value_types": types})
    return out


def _structure_findings(meta: dict[str, Any]) -> list[Finding]:
    """Blockers for structural drift: absent consumed fields, non-numeric values, unreviewed classification."""
    out: list[Finding] = []
    structure = meta.get("row_structure")
    if structure is None:
        return [
            Finding(
                code="structure_unverifiable",
                severity="blocker",
                message="batch carries no row structure record; drift cannot be excluded",
            )
        ]
    absent: dict[str, int] = {}
    types: dict[str, set[str]] = {}
    for s in structure:
        if s.get("not_an_object"):
            out.append(Finding(code="row_schema_drift", severity="blocker", message=f"row {s['row']} is not an object"))
        for k in s.get("absent", ()):
            absent[k] = absent.get(k, 0) + 1
        for k, t in (s.get("value_types") or {}).items():
            types.setdefault(k, set()).add(t)
    for k, n in sorted(absent.items()):
        out.append(
            Finding(
                code="field_absent",
                severity="blocker",
                message=f"{n} rows lack the consumed field '{k}' (renamed or removed?); a structurally "
                "absent field is schema drift, not a provider Missing",
            )
        )
    for k, ts in sorted(types.items()):
        out.append(
            Finding(
                code="value_type_drift",
                severity="blocker",
                message=f"'{k}' is {'/'.join(sorted(ts))}, expected a JSON number or null (never coerced)",
            )
        )
    unreviewed = sorted(
        {
            str(d.get("classificationCode"))
            for d in meta.get("rows", ())
            if d.get("classificationCode") not in REVIEWED_CLASSIFICATIONS
        }
    )
    if unreviewed:
        out.append(
            Finding(
                code="classification_unreviewed",
                severity="blocker",
                message=f"classificationCode {', '.join(unreviewed)} is outside the reviewed mapping's "
                f"applicability ({', '.join(REVIEWED_CLASSIFICATIONS)})",
            )
        )
    return out
