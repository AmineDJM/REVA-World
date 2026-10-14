"""P9 input-output and stock-flow-consistent (SFC) ledger interfaces.

* :class:`InputOutputModel` / :class:`IOTableMeta` — protocol for a future
  multi-region sector IO model with capacity constraints (reserved:
  ``economic.input_output``).
* :class:`StockFlowLedger` — protocol for a future SFC ledger model (reserved:
  ``economic.sfc_ledger``).
* :func:`verify_double_entry`, :func:`verify_stock_flow` — **implemented pure
  verifiers** (``verification.double_entry_balance``). They check ledgers
  supplied by a caller; they do not produce economic estimates.

Accounting conventions (architecture §16.2): postings are signed ``Decimal``
amounts (debit +, credit −) in an explicit unit. Each transaction must balance
separately in every unit — money never balances against energy or mass.
Revaluations change valuation inside one sector only (no counterparty, no
physical flow) and are monetary. Financial instruments are zero-sum across
sectors: every financial asset is another sector's liability.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

import numpy as np

from reva.capabilities import require
from reva.kernel.errors import ValidationProblem

FlowKind = Literal["transaction", "transfer", "production", "destruction", "revaluation", "boundary_exchange"]
BOUNDARY_SECTOR = "rest_of_world"


def _amount(value: object, where: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValidationProblem(f"{where}: ledger amounts must be Decimal/int/str, not {type(value).__name__}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, str)):
        try:
            d = Decimal(value)
        except Exception as exc:
            raise ValidationProblem(f"{where}: invalid amount {value!r}") from exc
    else:
        raise ValidationProblem(f"{where}: unsupported amount type {type(value).__name__}")
    if not d.is_finite():
        raise ValidationProblem(f"{where}: amount must be finite")
    return d


@dataclass(frozen=True)
class Posting:
    sector: str
    account: str
    unit: str
    amount: Decimal
    """Signed change in the sector's position: + increases an asset / reduces a liability."""

    def __post_init__(self) -> None:
        if not (self.sector and self.account and self.unit):
            raise ValidationProblem("posting needs sector, account and unit")
        object.__setattr__(self, "amount", _amount(self.amount, f"posting {self.sector}/{self.account}"))


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    kind: FlowKind
    postings: tuple[Posting, ...]
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.transaction_id:
            raise ValidationProblem("transaction_id is required")
        if len(self.postings) < 2:
            raise ValidationProblem(f"transaction {self.transaction_id!r}: double entry needs at least two postings")


@dataclass(frozen=True)
class LedgerFinding:
    code: Literal[
        "unbalanced_transaction",
        "revaluation_with_counterparty",
        "revaluation_physical_unit",
        "boundary_without_rest_of_world",
        "stock_flow_mismatch",
        "instrument_not_zero_sum",
        "duplicate_transaction_id",
    ]
    message: str
    transaction_id: str | None = None
    key: tuple[str, ...] | None = None
    residual: Decimal | None = None


@dataclass(frozen=True)
class LedgerCheckReport:
    balanced: bool
    findings: tuple[LedgerFinding, ...]
    transactions_checked: int
    verifier: str = "reva.extensions.io_sfc.verify/1"
    verification_only: Literal[True] = True


def verify_double_entry(
    transactions: Iterable[LedgerTransaction],
    *,
    monetary_units: frozenset[str] = frozenset({"USD", "EUR", "CNY", "DZD", "XDR"}),
    tolerance: Decimal = Decimal(0),
) -> LedgerCheckReport:
    """Check every transaction balances per unit and respects flow-kind rules."""
    findings: list[LedgerFinding] = []
    seen: set[str] = set()
    n = 0
    for tx in transactions:
        n += 1
        if tx.transaction_id in seen:
            findings.append(
                LedgerFinding("duplicate_transaction_id", "transaction id appears twice", tx.transaction_id)
            )
        seen.add(tx.transaction_id)
        per_unit: dict[str, Decimal] = defaultdict(Decimal)
        for p in tx.postings:
            per_unit[p.unit] += p.amount
        for unit, residual in sorted(per_unit.items()):
            if abs(residual) > tolerance:
                findings.append(
                    LedgerFinding(
                        "unbalanced_transaction",
                        f"postings in {unit} sum to {residual}, not zero",
                        tx.transaction_id,
                        (unit,),
                        residual,
                    )
                )
        sectors = {p.sector for p in tx.postings}
        if tx.kind == "revaluation":
            if len(sectors) != 1:
                findings.append(
                    LedgerFinding(
                        "revaluation_with_counterparty",
                        f"revaluation touches sectors {sorted(sectors)}; it must stay within one holder",
                        tx.transaction_id,
                    )
                )
            physical = sorted({p.unit for p in tx.postings} - monetary_units)
            if physical:
                findings.append(
                    LedgerFinding(
                        "revaluation_physical_unit",
                        f"revaluation posts physical units {physical}; valuation changes carry no physical flow",
                        tx.transaction_id,
                    )
                )
        if tx.kind == "boundary_exchange" and BOUNDARY_SECTOR not in sectors:
            findings.append(
                LedgerFinding(
                    "boundary_without_rest_of_world",
                    f"boundary exchange must post the counterparty to {BOUNDARY_SECTOR!r}",
                    tx.transaction_id,
                )
            )
    return LedgerCheckReport(balanced=not findings, findings=tuple(findings), transactions_checked=n)


PositionKey = tuple[str, str, str]  # (sector, account, unit)


def verify_stock_flow(
    opening: Mapping[PositionKey, object],
    transactions: Sequence[LedgerTransaction],
    closing: Mapping[PositionKey, object],
    *,
    financial_accounts: frozenset[str] = frozenset(),
    monetary_units: frozenset[str] = frozenset({"USD", "EUR", "CNY", "DZD", "XDR"}),
    tolerance: Decimal = Decimal(0),
) -> LedgerCheckReport:
    """Double entry + opening + Σ postings = closing per position + zero-sum financial claims."""
    base = verify_double_entry(transactions, monetary_units=monetary_units, tolerance=tolerance)
    findings = list(base.findings)
    expected: dict[PositionKey, Decimal] = defaultdict(Decimal)
    for k, v in opening.items():
        expected[k] += _amount(v, f"opening {k}")
    for tx in transactions:
        for p in tx.postings:
            expected[(p.sector, p.account, p.unit)] += p.amount
    actual = {k: _amount(v, f"closing {k}") for k, v in closing.items()}
    for key in sorted(set(expected) | set(actual)):
        diff = actual.get(key, Decimal(0)) - expected.get(key, Decimal(0))
        if abs(diff) > tolerance:
            findings.append(
                LedgerFinding(
                    "stock_flow_mismatch", f"closing differs from opening + flows by {diff}", key=key, residual=diff
                )
            )
    claims: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for (_sector, account, unit), v in actual.items():
        if account in financial_accounts:
            claims[(account, unit)] += v
    for (account, unit), total in sorted(claims.items()):
        if abs(total) > tolerance:
            findings.append(
                LedgerFinding(
                    "instrument_not_zero_sum",
                    f"financial instrument {account} ({unit}) nets to {total} across sectors; assets must equal "
                    "liabilities",
                    key=(account, unit),
                    residual=total,
                )
            )
    return LedgerCheckReport(
        balanced=not findings, findings=tuple(findings), transactions_checked=base.transactions_checked
    )


# --------------------------------------------------------------------------- protocols (reserved)


@dataclass(frozen=True)
class IOTableMeta:
    table_id: str
    regions: tuple[str, ...]
    sectors: tuple[str, ...]
    base_year: int
    price_basis: Literal["basic_prices", "purchasers_prices"]
    currency: str
    unit_multiplier: int
    source_ref: str
    licence: str

    def __post_init__(self) -> None:
        if not self.regions or not self.sectors:
            raise ValidationProblem("IO table needs regions and sectors")
        if len(set(self.regions)) != len(self.regions) or len(set(self.sectors)) != len(self.sectors):
            raise ValidationProblem("IO regions/sectors must be unique")


@runtime_checkable
class InputOutputModel(Protocol):
    """Multi-region sector IO with inventory/capacity constraints (P9).

    Implementations must keep production nonnegative and feasible, must not double
    count totals against bilateral data, and must declare the shock envelope in which
    linear multipliers are valid (they are not valid for arbitrarily large supply shocks).
    """

    meta: IOTableMeta

    def technical_coefficients(self) -> np.ndarray: ...
    def capacity(self) -> np.ndarray: ...
    def output_for_final_demand(self, final_demand: np.ndarray) -> np.ndarray: ...
    def multiplier_validity_envelope(self) -> Mapping[str, float]: ...


@runtime_checkable
class StockFlowLedger(Protocol):
    """Explicit SFC ledger model (P9). ``verify`` must use the pure verifiers above."""

    def post(self, transaction: LedgerTransaction) -> None: ...
    def positions(self) -> Mapping[PositionKey, Decimal]: ...
    def verify(self) -> LedgerCheckReport: ...


def run_input_output(meta: IOTableMeta, final_demand_shock: Mapping[str, object]) -> None:
    """Reserved: raises ``CapabilityGap`` (economic.input_output)."""
    require("economic.input_output", table_id=meta.table_id)
    raise AssertionError("unreachable")  # pragma: no cover


def run_sfc_model(ledger_id: str) -> None:
    """Reserved: raises ``CapabilityGap`` (economic.sfc_ledger)."""
    require("economic.sfc_ledger", ledger_id=ledger_id)
    raise AssertionError("unreachable")  # pragma: no cover
