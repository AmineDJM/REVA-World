"""LLM drafting budget: daily request and token caps through the persistent ledger.

Policy: ``registry/policies/budgets.yaml`` → ``llm.scenario_drafts``
(``daily_requests``, ``daily_tokens``, ``max_output_tokens``), read through the
digest-pinned loader (:func:`reva.autonomy.policy.budgets_policy`): a tampered
policy file fails closed. A missing section or a zero limit refuses every draft.

Ledger: ``ops.budget_ledger`` accounts ``llm:scenario_drafts:requests`` and
``llm:scenario_drafts:tokens``. Before each provider request the service
reserves one request and an *upper bound* of the tokens it can consume (the
UTF-8 byte length of everything sent — a byte-level BPE token covers at least
one byte — plus per-message overhead, plus ``max_output_tokens``). After the
response it settles the provider-reported total; when the provider reports
nothing (failure) the reservation stays charged (fail closed). Over budget →
:class:`~reva.operations.budgets.BudgetExhausted` (HTTP 429 ``budget_exhausted``)
*before* any request; there is never a fallback to a guess.

Database access: the API role ``reva_api`` may only SELECT the ledger, so the
reservation goes through two narrow SECURITY DEFINER functions
(:data:`LEDGER_FUNCTIONS_SQL`, requested for migration ``0008_v2``). Where they
are absent, a role that may write the ledger directly (worker, owner) uses the
generic :func:`reva.operations.budgets.reserve`/``consume``; any other role is
refused (:class:`~reva.nl.ports.LlmUnavailable`, nothing is sent).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from reva.nl.ports import LlmRequest, LlmUnavailable
from reva.operations.budgets import BudgetExhausted

REQUESTS_ACCOUNT = "llm:scenario_drafts:requests"
TOKENS_ACCOUNT = "llm:scenario_drafts:tokens"
MESSAGE_OVERHEAD_TOKENS = 64
LLM_LOCK_KEY = "reva:budget:llm"


@dataclass(frozen=True)
class LlmBudget:
    policy_version: str
    policy_sha256: str
    daily_requests: int
    daily_tokens: int
    max_output_tokens: int


def load_llm_budget(policy: Any = None) -> LlmBudget:
    """The pinned ``llm.scenario_drafts`` limits; refuses (fail closed) when absent or zero."""
    if policy is None:
        from reva.autonomy.policy import budgets_policy

        policy = budgets_policy()
    content = policy.content
    section = (content.get("llm") or {}).get("scenario_drafts") or {}
    version = str(content.get("policy_version", "unversioned"))
    try:
        b = LlmBudget(version, str(getattr(policy, "sha256", "")), int(section["daily_requests"]),
                      int(section["daily_tokens"]), int(section["max_output_tokens"]))
    except (KeyError, TypeError, ValueError):
        raise BudgetExhausted("no LLM drafting budget in budgets.yaml (llm.scenario_drafts): refused (fail closed)",
                              reason="unknown_source", source_key="llm.scenario_drafts", policy=version) from None
    if b.daily_requests <= 0 or b.daily_tokens <= 0 or b.max_output_tokens <= 0:
        raise BudgetExhausted("the LLM drafting budget is 0: no requests allowed", reason="zero_limit",
                              source_key="llm.scenario_drafts", policy=version)
    return b


def token_upper_bound(request: LlmRequest) -> int:
    """Upper bound of the tokens one request can consume (input bytes + overhead + max output)."""
    sent = (len(request.system_prompt.encode()) + len(request.user_content.encode())
            + len(json.dumps(request.output_schema, separators=(",", ":")).encode()))
    return sent + MESSAGE_OVERHEAD_TOKENS + request.max_output_tokens


class LlmLedger(Protocol):
    def reserve(self, operation_id: str, *, requests: int, tokens: int) -> None: ...  # raises BudgetExhausted

    def settle(self, operation_id: str, *, requests: int, tokens: int) -> None: ...


LEDGER_FUNCTIONS_SQL = r"""
CREATE FUNCTION ops.reserve_llm_budget(p_operation_id text, p_requests integer, p_tokens bigint,
                                       p_daily_requests integer, p_daily_tokens bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE used_r numeric; used_t numeric; day_start timestamptz;
BEGIN
  IF meta.session_principal() NOT IN ('reva_api','reva_worker','owner') THEN
    RAISE EXCEPTION 'LLM budget reservation is an API/worker action' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_operation_id IS NULL OR p_operation_id !~ '^nl_draft:[0-9a-f]{32,64}:[0-9]{1,2}$' THEN
    RAISE EXCEPTION 'invalid LLM budget operation id';
  END IF;
  IF p_requests IS NULL OR p_requests < 1 OR p_requests > 10 OR p_tokens IS NULL OR p_tokens < 1
     OR p_tokens > 2000000 OR p_daily_requests IS NULL OR p_daily_requests < 0
     OR p_daily_tokens IS NULL OR p_daily_tokens < 0 THEN
    RAISE EXCEPTION 'LLM budget amounts out of range';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('reva:budget:llm', 0));
  IF EXISTS (SELECT 1 FROM ops.budget_ledger
              WHERE operation_id IN (p_operation_id || ':requests', p_operation_id || ':tokens')) THEN
    RETURN jsonb_build_object('granted', false, 'reason', 'operation_exists');
  END IF;
  day_start := date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
  SELECT COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE account_key = 'llm:scenario_drafts:requests'), 0),
         COALESCE(sum(COALESCE(consumed, reserved)) FILTER (WHERE account_key = 'llm:scenario_drafts:tokens'), 0)
    INTO used_r, used_t
    FROM ops.budget_ledger
   WHERE account_key IN ('llm:scenario_drafts:requests', 'llm:scenario_drafts:tokens')
     AND recorded_at >= day_start;
  IF used_r + p_requests > p_daily_requests THEN
    RETURN jsonb_build_object('granted', false, 'reason', 'request_limit', 'used_requests', used_r,
                              'used_tokens', used_t);
  END IF;
  IF used_t + p_tokens > p_daily_tokens THEN
    RETURN jsonb_build_object('granted', false, 'reason', 'token_limit', 'used_requests', used_r,
                              'used_tokens', used_t);
  END IF;
  INSERT INTO ops.budget_ledger(account_key, operation_id, unit, reserved) VALUES
    ('llm:scenario_drafts:requests', p_operation_id || ':requests', 'requests', p_requests),
    ('llm:scenario_drafts:tokens', p_operation_id || ':tokens', 'tokens', p_tokens);
  RETURN jsonb_build_object('granted', true, 'used_requests', used_r, 'used_tokens', used_t);
END $$;

CREATE FUNCTION ops.settle_llm_budget(p_operation_id text, p_requests integer, p_tokens bigint)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE n integer;
BEGIN
  IF meta.session_principal() NOT IN ('reva_api','reva_worker','owner') THEN
    RAISE EXCEPTION 'LLM budget settlement is an API/worker action' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_requests IS NULL OR p_requests < 0 OR p_requests > 10 OR p_tokens IS NULL OR p_tokens < 0
     OR p_tokens > 2000000 THEN
    RAISE EXCEPTION 'LLM budget amounts out of range';
  END IF;
  UPDATE ops.budget_ledger
     SET consumed = CASE unit WHEN 'requests' THEN p_requests ELSE p_tokens END
   WHERE operation_id IN (p_operation_id || ':requests', p_operation_id || ':tokens')
     AND account_key IN ('llm:scenario_drafts:requests', 'llm:scenario_drafts:tokens')
     AND consumed IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

REVOKE ALL ON FUNCTION ops.reserve_llm_budget(text, integer, bigint, integer, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops.settle_llm_budget(text, integer, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.reserve_llm_budget(text, integer, bigint, integer, bigint),
                          ops.settle_llm_budget(text, integer, bigint) TO reva_api, reva_worker;
"""

_HAS_FUNCTIONS = text(
    "SELECT to_regprocedure('ops.reserve_llm_budget(text,integer,bigint,integer,bigint)') IS NOT NULL "
    "AND to_regprocedure('ops.settle_llm_budget(text,integer,bigint)') IS NOT NULL")


class DbLlmLedger:
    """:class:`LlmLedger` on ``ops.budget_ledger`` (SECURITY DEFINER functions, else direct writes)."""

    def __init__(self, engine: Engine, budget: LlmBudget) -> None:
        self.engine = engine
        self.budget = budget

    def _functions(self) -> bool:
        with self.engine.connect() as conn:
            return bool(conn.execute(_HAS_FUNCTIONS).scalar_one())

    def _exhausted(self, reason: str, used_r: Any, used_t: Any, requests: int, tokens: int) -> BudgetExhausted:
        b = self.budget
        if reason == "request_limit":
            msg = f"daily LLM request budget exhausted ({used_r} used + {requests} > {b.daily_requests})"
        else:
            msg = f"daily LLM token budget exhausted ({used_t} used + {tokens} reserved > {b.daily_tokens})"
        return BudgetExhausted(msg + "; nothing was sent to a provider", reason=reason,
                               source_key="llm.scenario_drafts", policy=b.policy_version,
                               daily_requests=b.daily_requests, daily_tokens=b.daily_tokens,
                               used_requests=int(used_r or 0), used_tokens=int(used_t or 0))

    def reserve(self, operation_id: str, *, requests: int, tokens: int) -> None:
        b = self.budget
        if self._functions():
            with self.engine.begin() as conn:
                res = conn.execute(text("SELECT ops.reserve_llm_budget(:o, :r, :t, :dr, :dt)"),
                                   {"o": operation_id, "r": requests, "t": tokens, "dr": b.daily_requests,
                                    "dt": b.daily_tokens}).scalar_one()
            res = res if isinstance(res, dict) else json.loads(res)
            if res.get("granted"):
                return
            if res.get("reason") == "operation_exists":
                raise IdempotencyReplay()
            raise self._exhausted(str(res.get("reason")), res.get("used_requests"), res.get("used_tokens"),
                                  requests, tokens)
        self._reserve_direct(operation_id, requests=requests, tokens=tokens)

    def _reserve_direct(self, operation_id: str, *, requests: int, tokens: int) -> None:
        from reva.operations.budgets import consume, reserve

        b = self.budget
        try:
            with self.engine.connect() as conn:
                exists = conn.execute(text("SELECT count(*) FROM ops.budget_ledger WHERE operation_id = ANY(:o)"),
                                      {"o": [f"{operation_id}:requests", f"{operation_id}:tokens"]}).scalar_one()
            if exists:
                raise IdempotencyReplay()
            reserve(self.engine, account_key=REQUESTS_ACCOUNT, operation_id=f"{operation_id}:requests",
                    unit="requests", amount=requests, daily_limit=b.daily_requests)
            try:
                reserve(self.engine, account_key=TOKENS_ACCOUNT, operation_id=f"{operation_id}:tokens",
                        unit="tokens", amount=tokens, daily_limit=b.daily_tokens)
            except BudgetExhausted as e:
                consume(self.engine, f"{operation_id}:requests", 0)  # nothing was sent
                raise self._exhausted("token_limit", None, e.details.get("used"), requests, tokens) from None
        except BudgetExhausted as e:
            if e.details.get("reason"):
                raise
            raise self._exhausted("request_limit", e.details.get("used"), None, requests, tokens) from None
        except (ProgrammingError, DBAPIError) as e:
            if "permission denied" in str(getattr(e, "orig", e)).lower():
                raise LlmUnavailable(
                    "the LLM budget ledger is not writable by this database role and the ledger functions "
                    "(ops.reserve_llm_budget / ops.settle_llm_budget, migration 0008_v2) are absent: drafting "
                    "is refused (fail closed)", reason="budget_ledger_unavailable") from None
            raise

    def settle(self, operation_id: str, *, requests: int, tokens: int) -> None:
        if self._functions():
            with self.engine.begin() as conn:
                conn.execute(text("SELECT ops.settle_llm_budget(:o, :r, :t)"),
                             {"o": operation_id, "r": requests, "t": tokens})
            return
        from reva.operations.budgets import consume

        consume(self.engine, f"{operation_id}:requests", requests)
        consume(self.engine, f"{operation_id}:tokens", tokens)


class IdempotencyReplay(BudgetExhausted):
    """The operation (actor + Idempotency-Key) was already charged; the draft is not stored to replay."""

    code = "idempotency_conflict"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("this Idempotency-Key was already used for a scenario draft; drafts are not stored, so "
                         "the earlier result cannot be replayed: send the request with a new key",
                         reason="operation_exists")
