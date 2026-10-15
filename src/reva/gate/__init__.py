"""Independent promotion gate (process separation for ``PolicyPromotionGate``).

* ``reva.gate.client`` -- worker side: files an ``ops.gate_request`` for a persisted
  proposal and waits for the decision the gate process records. The worker login
  (``reva_worker``) cannot record gate results (no INSERT on ``ops.gate_result``, no
  EXECUTE on ``ops.record_gate_result``).
* ``reva.gate.service`` -- the gate process (``reva gate`` / ``scripts/start_gate.py``):
  logs in as ``reva_gate``, polls for requests, re-derives everything from stored
  rows and artifacts with the reviewed gate logic
  (``reva.autonomy.gate.PolicyPromotionGate``) and records the decision.

The approval functions (``evidence.approve_eligibility``, ``ontology.approve_mapping``,
``model.approve_version``, ``evidence.restore_eligibility``) accept only gate results
recorded by ``reva_gate`` (or an API-recorded human review). See
``docs/dev/gate_separation.md``.
"""

GATE_LOGIN = "reva_gate"

__all__ = ["GATE_LOGIN"]
