"""ConnectorValidator V1: bounded sample (captured replay or live), schema/semantic/unit/support checks.

The validator executes the *reviewed template* parameterised by the proposed
manifest. It checks, against the pinned manifest and the mapped concept:

* JSON-stat structure: dimension ids/order, pinned categories, unit, frequency,
  ``size`` product vs value count, status flags (known vocabulary, declared
  labels, status map present when labels are declared);
* semantics: concept, unit_ref, aggregation, population universe, temporal point
  support (e.g. 1 January) and resolution dimensions, via the data-quality rules
  against a data contract derived from the *registry* concept profile (never from
  the manifest's own semantics block, review R4-01);
* malformed provider data yields a typed ``fail`` report (never an untyped crash);
* drift against the structure fingerprint recorded at inspection.

A live sample is captured into a new immutable fixture bank (``fixture_bank`` in
the evidence manifest). Without a fixture and without live sampling the result is
``blocked`` — never a pass.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from reva.adapters.connectors.eurostat_template import (
    TEMPLATE_HOSTS,
    TEMPLATE_ID,
    EurostatJsonStatConnector,
    template_digest,
)
from reva.autonomy.common import ArtifactIO, report
from reva.autonomy.fixtures import BankHttp, CapturingHttp
from reva.autonomy.policy import AutonomyGuard
from reva.autonomy.quality import contract_for, evaluate_batch
from reva.kernel.errors import RevaError
from reva.ontology.registry import Registry, load_registry
from reva.ports.autonomy import ArtifactRef, ConnectorProposal, Finding, ResourceBudget, ValidationReport
from reva.ports.connectors import FetchBudget

EVALUATOR = "connector-validator:eurostat_jsonstat@1"
MALFORMED_DATA = (ValueError, TypeError, AttributeError, KeyError, IndexError, RecursionError, ArithmeticError)


def _f(code: str, severity: str, message: str, evidence: tuple[ArtifactRef, ...] = ()) -> Finding:
    return Finding(code=code, severity=severity, message=message, evidence=evidence)  # type: ignore[arg-type]


class JsonStatConnectorValidator:
    def __init__(
        self,
        io_: ArtifactIO,
        *,
        live_client_factory: Callable[[tuple[str, ...]], Any] | None = None,
        guard: AutonomyGuard | None = None,
        registry: Registry | None = None,
    ) -> None:
        self.io = io_
        self.live_client_factory = live_client_factory
        self.guard = guard
        self.registry = registry or load_registry()

    async def validate(
        self, proposal: ConnectorProposal, fixtures: ArtifactRef, budget: ResourceBudget
    ) -> ValidationReport:
        if self.guard is not None:
            self.guard.check("validate_connector")
        started = time.monotonic()
        findings: list[Finding] = []
        manifest = self.io.get_json(proposal.manifest)
        evidence: dict[str, Any] = {
            "schema": "reva-connector-validation/1",
            "evaluator": EVALUATOR,
            "proposal_id": str(proposal.proposal_id),
            "manifest": proposal.manifest.sha256,
            "fixtures_in": fixtures.sha256,
            "template_digest": template_digest(),
        }

        def finish(outcome: str, usage: dict[str, Any]) -> ValidationReport:
            evidence["findings"] = [{"code": x.code, "severity": x.severity, "message": x.message} for x in findings]
            evidence["outcome"] = outcome
            usage = {**usage, "wall_seconds": round(time.monotonic() - started, 3)}
            evidence["resource_usage"] = usage
            ev = self.io.put_json(evidence, "evaluation")
            return report(outcome, findings, ev, EVALUATOR, usage)

        if proposal.template_id != TEMPLATE_ID or manifest.get("template") != TEMPLATE_ID:
            findings.append(_f("template_not_supported", "blocker", "validator only runs eurostat_jsonstat"))
            return finish("fail", {})
        if proposal.template_digest != template_digest():
            findings.append(
                _f(
                    "template_digest_mismatch",
                    "blocker",
                    "proposal was generated against a different template revision",
                )
            )
        try:
            connector = EurostatJsonStatConnector(manifest)
        except RevaError as e:
            findings.append(_f("manifest_invalid", "blocker", e.message))
            return finish("fail", {})
        hosts = tuple(sorted(set(manifest["allowed_hosts"]) & set(TEMPLATE_HOSTS)))
        plan = connector.plan(None, FetchBudget(max_requests=max(0, budget.max_requests), max_bytes=budget.max_bytes))
        bank = BankHttp(self.io, fixtures, allowed_hosts=hosts)
        http: Any = bank
        mode = "captured_replay"
        if not all(bank.has(i.url) for i in plan.items):
            if self.live_client_factory is None:
                findings.append(
                    _f(
                        "no_sample",
                        "blocker",
                        "no captured fixture for the planned request and live sampling is disabled",
                    )
                )
                return finish("blocked", {"requests": 0, "bytes": 0})
            http = CapturingHttp(self.live_client_factory(hosts), str(manifest["request_parameters"].get("licence")))
            mode = "live_sample"
        evidence["mode"] = mode
        if not plan.items:
            findings.append(_f("budget_exhausted", "blocker", "budget allows no sample request"))
            return finish("blocked", {"requests": 0, "bytes": 0})
        used_bytes = 0
        batches: list[Any] = []
        for item in plan.items:
            remaining = budget.max_bytes - used_bytes
            if remaining <= 0:
                findings.append(_f("budget_exhausted", "blocker", "byte budget exhausted"))
                return finish("blocked", {"requests": len(batches), "bytes": used_bytes})
            try:
                raw = await connector.fetch(item.model_copy(update={"max_bytes": min(item.max_bytes, remaining)}), http)
                used_bytes += len(raw.body)
                for b in connector.parse(raw):
                    batches.append(b)
            except RevaError as e:
                findings.append(_f(e.details.get("kind", e.code), "blocker", e.message))
                return finish("fail", {"requests": 1, "bytes": used_bytes})
            except MALFORMED_DATA as e:  # defence in depth: never an untyped crash of the discovery job
                findings.append(_f("schema_drift", "blocker", f"malformed provider data: {type(e).__name__}"))
                return finish("fail", {"requests": 1, "bytes": used_bytes})
        if isinstance(http, CapturingHttp):
            new_bank = http.bank(self.io, manifest["name"])
            evidence["fixture_bank"] = new_bank.sha256
            await http.aclose()
        try:
            contract = contract_for(manifest, self.registry)
        except RevaError as e:
            findings.append(_f("no_registry_contract", "blocker", e.message))
            return finish("fail", {"requests": len(plan.items), "bytes": used_bytes})
        contract_ref = self.io.put_json(contract, "models")
        evidence["data_contract"] = contract_ref.sha256
        evidence["batches"] = []
        for b in batches:
            q = connector.validate(b)
            for x in q.findings:
                findings.append(_f(x.code, x.severity, x.message))
            batch_json = b.model_dump(mode="json")
            batch_ref = self.io.put_json(batch_json, "normalised")
            dq, detail = evaluate_batch(batch_json, contract)
            findings.extend(dq)
            md = b.metadata
            if md.get("structure_fingerprint") != manifest["request_parameters"].get("inspection_fingerprint"):
                findings.append(
                    _f(
                        "structure_drift_since_inspection",
                        "blocker",
                        "sample structure differs from the structure inspected at discovery",
                    )
                )
            evidence["batches"].append(
                {
                    "item": b.item_key,
                    "batch_sha256": batch_ref.sha256,
                    "quality": q.model_dump(mode="json"),
                    "dimension_order": md.get("dimension_order"),
                    "size": md.get("size"),
                    "cube_positions": md.get("cube_positions"),
                    "value_count": md.get("value_count"),
                    "status_codes_seen": md.get("status_codes_seen"),
                    "status_labels": md.get("status_labels"),
                    "unit_codes": md.get("unit_codes"),
                    "dataset_updated": md.get("dataset_updated"),
                    "structure_fingerprint": md.get("structure_fingerprint"),
                    "data_quality": detail,
                }
            )
        outcome = "fail" if any(x.severity == "blocker" for x in findings) else "pass"
        return finish(outcome, {"requests": len(plan.items), "bytes": used_bytes, "mode": mode})
