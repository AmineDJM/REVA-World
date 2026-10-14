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
CONNECTOR_VERSION = "1.1.0"
PARSER_VERSION = "comtrade-parse-2"
VALUATION_UNDETERMINED = "primaryValue (basis not determinable from response)"
EXCHANGE_RATE_TRANSFORM = "provider_reported_usd (UN Comtrade methodology)"
FIXED_DIMS = {"typeCode": "C", "freqCode": "A", "cmdCode": "2709", "customsCode": "C00", "motCode": 0,
              "partner2Code": 0}


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
            field_mappings={**{f"{k[0]}:{k[1]}": v for k, v in FLOW_CONCEPT.items()},
                            **{f"{k}:netWgt:total": v for k, v in WEIGHT_CONCEPT.items()}},
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
        url = (
            f"{BASE}?period={','.join(map(str, years))}&reporterCode={reporter}&cmdCode=2709"
            f"&flowCode={flow}"
        )
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
                            params={"scope": "bilateral", "iso": iso, "reporter": code, "flow": flow,
                                    "years": [y]},
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
            metadata={**meta, "rows": [self._dims(r) for r in rows][:MAX_RECORDS]},
            schema_fingerprint=schema_fingerprint(rows[:5]),
        )

    @staticmethod
    def _dims(r: dict[str, Any]) -> dict[str, Any]:
        keys = ("period", "reporterCode", "flowCode", "partnerCode", "partner2Code", "cmdCode", "customsCode",
                "motCode", "typeCode", "freqCode", "classificationCode")
        return {k: plain(r.get(k)) for k in keys}

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
            "provider_derived_aggregate": bool(r.get("isAggregate")) and not bool(r.get("isReported")),
        }

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
        eq_cif = pv is not None and cif is not None and Decimal(pv) == Decimal(cif)
        eq_fob = pv is not None and fob is not None and Decimal(pv) == Decimal(fob)
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
        return {"currency": "USD", "price_basis": "nominal", "scale_factor": "1", "valuation": valuation,
                "exchange_rate_transform": EXCHANGE_RATE_TRANSFORM}

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
                flags = self._flags(r)
            else:
                flags = self._flags(r)
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
                flags = self._flags(r)
                w = r.get("netWgt")
                if w is None or Decimal(w) <= 0:
                    # null or zero weight alongside a positive value is unusable, never a measurement
                    value = {"kind": "missing", "reason": "unavailable"}
                    flags["weight_unusable"] = True
                else:
                    value = {"kind": "decimal", "value": dec_str(w), "unit_ref": "unit.kg",
                             "basis": {"scale_factor": "1"}}
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
            raw_selector={"match": {"period": str(year), "partnerCode": 0, "flowCode": flow},
                          "field": "primaryValue" if what == "value" else "netWgt"},
        )

    def _bilateral(self, p: dict[str, Any], r: dict[str, Any]) -> ParsedObservation:
        iso, flow = p["iso"], p["flow"]
        year = int(str(r.get("period")))
        partner = str(plain(r.get("partnerCode")))
        v = r.get("primaryValue")
        flags = {**self._flags(r), "flowCode": flow, "partnerCode": partner}
        value: dict[str, Any] = (
            {"kind": "missing", "reason": "unavailable"}
            if v is None
            else {"kind": "decimal", "value": dec_str(v), "unit_ref": "unit.usd",
                  "basis": self._value_basis(flow, r, flags)}
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
            raw_selector={"match": {"period": str(year), "partnerCode": int(partner), "flowCode": flow},
                          "field": "primaryValue"},
            relation=RelationSpec(
                predicate="predicate.trade_flow",
                layer="trade.crude_petroleum",
                participants=(
                    {"role": "role.reporter", "provider": "comtrade_reporter", "key": str(REPORTERS[iso]),
                     "direction": reporter_dir},
                    {"role": "role.partner", "provider": "comtrade_partner", "key": partner,
                     "direction": partner_dir},
                    {"role": "role.product", "provider": "hs_heading", "key": "2709", "direction": "undirected"},
                ),
                component={"flow": flow, "valuation": value.get("basis", {}).get("valuation"),
                           "classification": r.get("classificationCode")},
            ),
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        findings: list[Finding] = []
        meta = batch.metadata
        req = meta["request"]
        count = meta.get("count")
        n = meta["returned_rows"]
        if count is None or int(count) != n:
            findings.append(Finding(code="count_mismatch", severity="blocker",
                                    message=f"count={count} but {n} rows returned"))
        if n >= MAX_RECORDS:
            findings.append(Finding(code="possible_truncation", severity="blocker",
                                    message="row count reached maxRecords; preview truncation cannot be excluded"))
        seen = set()
        for d in meta["rows"]:
            want = {**FIXED_DIMS, "reporterCode": req["reporter"], "flowCode": req["flow"]}
            if req["scope"] == "total":
                want["partnerCode"] = 0
            for k, v in want.items():
                if str(d.get(k)) != str(v):
                    findings.append(Finding(code="dimension_mismatch", severity="blocker",
                                            message=f"{k}={d.get(k)} differs from requested {v}"))
            if int(str(d.get("period"))) not in [int(y) for y in req["years"]]:
                findings.append(Finding(code="dimension_mismatch", severity="blocker",
                                        message=f"unexpected period {d.get('period')}"))
            key = (d.get("period"), d.get("partnerCode"), d.get("classificationCode"))
            dims_key = (d.get("period"), d.get("partnerCode"))
            if dims_key in seen:
                findings.append(Finding(code="ambiguous_duplicate", severity="blocker",
                                        message=f"duplicate rows for {key}"))
            seen.add(dims_key)
        missing = [o.logical_key for o in batch.observations if o.value.get("kind") == "missing"
                   and o.logical_key.endswith("primaryValue")]
        if missing:
            findings.append(Finding(code="missing_flow", severity="warning",
                                    message=f"{len(missing)} requested flows returned no value (kept Missing)"))
        undetermined = [o for o in batch.observations if o.source_flags.get("valuation_warning")]
        if undetermined:
            codes = sorted({str(o.source_flags["valuation_warning"]) for o in undetermined})
            findings.append(Finding(code="valuation_basis_undetermined", severity="warning",
                                    message=f"{len(undetermined)} values carry an undeterminable valuation basis "
                                            f"({', '.join(codes)}); labelled '{VALUATION_UNDETERMINED}'"))
        derived = [o for o in batch.observations if o.source_flags.get("provider_derived_aggregate")]
        if derived:
            findings.append(Finding(code="provider_derived", severity="info",
                                    message=f"{len(derived)} values are provider-derived aggregates (isReported=false)"))
        return _report(findings, len(batch.expected_keys), len(batch.observations), ())

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        required = {i.key for i in plan.items if i.required}
        ok = all(o.status in ("published", "unchanged") for o in outcomes if o.item_key in required)
        return Cursor(state={"complete": ok and not plan.budget_exhausted, "recheck_years": list(self.total_years)})
