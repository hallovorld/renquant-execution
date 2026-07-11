"""Stage-1 intraday order-lifecycle state machine (RFC #208 SS7/SS10/SS11b + #223 A2).

Pure, broker-agnostic implementation of the order-lifecycle / idempotency
contract pre-registered in the renquant105 RFC
(``doc/design/2026-06-30-renquant105-intraday-decisioning-architecture.md``
SS7, safety defaults SS10, session policy SS11b) as amended by the merged
design-review amendment A2 (``doc/design/2026-07-01-104-105-design-review-amendments.md``):

- Lifecycle states per (account, symbol, session)::

    NONE -> INTENDED -> SUBMITTED -> ACCEPTED -> PARTIALLY_FILLED -> FILLED
                 |            |              |
                 +- REJECTED  +- CANCELED    +- (remainder) CANCELED
                 +- STALE_PENDING (age > max_pending_age) -> reconcile -> CANCELED/FILLED

- Two-level id: ``parent_intent_id = hash(account, symbol, trading_day, side,
  signal_version)`` is the dedup key (identifies the *decision*, never a broker
  id); ``child_order_id = parent_intent_id + ":" + attempt_n`` is the broker
  client-order-id, fresh and unique per submission.
- Economic invariant (hard assertion before EVERY submit)::

    target_qty = cum_filled + open_qty + remaining_unsubmitted,
    remaining_unsubmitted >= 0,  cum_filled + open_qty <= target_qty

  so retries can never make total filled exceed ``target_qty``.
- Audit invariant (monotone, MAY exceed target)::

    gross_submitted_qty = cum_filled + open_qty + cum_canceled
                          + cum_rejected + cum_expired

- Re-emit rule: at most one OPEN child per parent; a remainder child is
  eligible only when there is no OPEN child, ``cum_filled < target_qty``, and
  the name is still gate-admitted. At most one filled position per name per
  session (a parent reaches its target then stops).
- Canceled-remainder eligibility: a canceled partial remainder stays eligible
  within the session under the re-emit rule; the canceled quantity does NOT
  reduce ``target_qty`` (it is recovered through ``remaining_unsubmitted``).
- Reserved-cash accounting: open BUY children reserve ``unfilled x price``
  until filled or canceled; sizing must use ``broker_cash - reserved_cash``.
- Reconcile-before-emit: a book restored from a snapshot refuses ALL submits
  until reconciled against broker open-orders; a reconciliation mismatch
  halts new ENTRIES for the session (exits stay allowed) and surfaces the
  mismatch data for alerting.
- Timer-driven stale-pending cancel (SS10 default: 10 min, enforced between
  decision ticks by a watchdog, never inherited by the next tick).
- Amendment A2 (verified intraday-margin regime, effective 2026-06-04): NO
  legacy PDT / day-trade counting anywhere. Entries bind on recorded
  buying-power headroom fields (``non_marginable_buying_power``, consistent
  with the pinned ``execution.buying_power_mode``); a broker-reported intraday
  margin deficit or a broker-rule-regime mismatch is a Tier-1 condition (halt
  new entries). Exits-always-allowed precedence: no envelope, regulatory, or
  budget constraint may ever block a protective exit -- constraints bind
  entries only.

Broker-agnostic by construction: the only seam to a real broker is the
``BrokerPort`` protocol; the driver helpers (``submit_remainder``,
``run_stale_watchdog``, ``reconcile_on_restart``) are the intended call
pattern. Nothing in this module wires into any live execution path -- the
orchestrator/pipeline slices (RFC SS8 rows 2-3) integrate it behind the
default-OFF intraday flag.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from .crypto import is_crypto_pair

ORDER_STATE_SCHEMA_VERSION = "order-state-machine-v1"

#: SS10 conservative default: max pending-order age before the watchdog
#: cancels + reconciles it *between* ticks (< the 12-min decision cadence).
MAX_PENDING_AGE_SECONDS = 600.0

_QTY_EPS = 1e-9
_FIELD_SEP = "\x1f"  # unit separator: cannot appear in symbols/accounts

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
_VALID_SIDES = (SIDE_BUY, SIDE_SELL)


class LifecycleError(RuntimeError):
    """Contract violation inside the order-lifecycle state machine."""


class EconomicInvariantError(LifecycleError):
    """The SS7 economic invariant would be (or has been) violated."""


class DuplicateChildOrderError(LifecycleError):
    """A child_order_id collision -- every submission must be unique."""


class EntryBlockedError(LifecycleError):
    """An ENTRY submit was refused by a policy constraint (never an exit)."""

    def __init__(self, reason: str):
        super().__init__(f"entry blocked: {reason}")
        self.reason = reason


class LifecycleState(str, Enum):
    """Parent-level lifecycle state per the SS7 diagram."""

    NONE = "NONE"
    INTENDED = "INTENDED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    STALE_PENDING = "STALE_PENDING"


class ChildOrderState(str, Enum):
    """State of one broker submission (one client-order-id)."""

    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    STALE_PENDING = "STALE_PENDING"


#: Child states that are live at the broker (consume open_qty + reservation).
#: STALE_PENDING is still OPEN: the order is live until the cancel is
#: confirmed by reconciliation (it may still race to a fill).
OPEN_CHILD_STATES = frozenset(
    {
        ChildOrderState.SUBMITTED,
        ChildOrderState.ACCEPTED,
        ChildOrderState.PARTIALLY_FILLED,
        ChildOrderState.STALE_PENDING,
    }
)

#: S-FRAC stage-1 terminal vocabulary: canonical SS7 mapping from broker
#: status strings to terminal child states. DAY expiry ("expired") is a
#: first-class TERMINAL outcome — fractional orders are TIF=DAY only (S-FRAC
#: design SS4), so an unfilled remainder expires at close and must never be
#: carried as a resting order (same-day reconciliation only, no GTC carryover
#: bookkeeping). "done_for_day" is the broker's close-out of a DAY order and
#: maps to the SS7 diagram's CANCELED branch.
TERMINAL_STATUS_MAP: dict[str, ChildOrderState] = {
    "filled": ChildOrderState.FILLED,
    "canceled": ChildOrderState.CANCELED,
    "cancelled": ChildOrderState.CANCELED,
    "done_for_day": ChildOrderState.CANCELED,
    "expired": ChildOrderState.EXPIRED,
    "rejected": ChildOrderState.REJECTED,
    "failed": ChildOrderState.REJECTED,
}

#: Terminal state -> the parent audit counter its unfilled remainder feeds.
_TERMINAL_COUNTERS: dict[ChildOrderState, str] = {
    ChildOrderState.CANCELED: "cum_canceled",
    ChildOrderState.REJECTED: "cum_rejected",
    ChildOrderState.EXPIRED: "cum_expired",
}


def classify_terminal_status(status: Any) -> ChildOrderState | None:
    """Map a broker status string to its terminal child state (or None).

    ``None`` means the status is not terminal (the order may still fill);
    callers must leave the child OPEN and let reconciliation surface it.
    """
    return TERMINAL_STATUS_MAP.get(str(status or "").strip().lower())


def compute_parent_intent_id(
    *,
    account: str,
    symbol: str,
    trading_day: str,
    side: str,
    signal_version: str,
) -> str:
    """Deterministic dedup key for one *decision* (SS7 two-level id).

    Stable across restarts and processes (sha256, not ``hash()``); identifies
    the INTENDED row, never sent to the broker directly.
    """
    payload = _FIELD_SEP.join(
        [
            str(account),
            str(symbol).upper(),
            str(trading_day),
            str(side).upper(),
            str(signal_version),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"pi-{digest[:20]}"


def child_order_id(parent_intent_id: str, attempt_n: int) -> str:
    """Broker client-order-id: unique per submission (SS7)."""
    return f"{parent_intent_id}:{attempt_n}"


def parent_intent_id_from_client_order_id(client_order_id: str) -> str:
    """Invert :func:`child_order_id` for §5.3 ledger reconciliation.

    Child ids are ``<parent_intent_id>:<attempt_n>``. Anything that does not
    match that shape — e.g. an external/manual order's broker-generated id —
    is returned verbatim, which by construction can never match a ledger
    reservation and therefore fails closed through the sweep's
    unknown-open-buy path.
    """
    cid = str(client_order_id)
    head, sep, tail = cid.rpartition(":")
    if sep and head and tail.isdigit():
        return head
    return cid


#: §5.3 fail-closed vocabulary shared with the account cash ledger: a book
#: reconcile mismatch escalates to an ACCOUNT-WIDE entries halt (every
#: sleeve) when a ledger is attached — single source for the reason string.
ACCOUNT_CASH_RECONCILE_MISMATCH_REASON = "account_cash_reconcile_mismatch"

#: Entry-refusal reason when the canonical cost contract
#: (``renquant_common.cost_model``, D-C8a) cannot be loaded/verified: the
#: ledger path REQUIRES the fee-inclusive worst-case debit from the ONE
#: shared cost model — with the contract absent, NEW ENTRIES fail closed
#: (exits, as always, are never routed through the ledger).
ACCOUNT_CASH_COST_CONTRACT_UNAVAILABLE_REASON = (
    "account_cash_cost_contract_unavailable"
)


class CostContractUnavailableError(LifecycleError):
    """The canonical cost contract (``renquant_common.cost_model``) is
    absent, or fails its surface/schema-version verification.

    Defined here (not in the ledger module) because this is the error the
    :class:`CashLedgerPort` seam is allowed to raise into the submit path;
    ``submit_remainder`` converts it to a fail-closed
    :class:`EntryBlockedError` — a BUY can never be sized without the
    fee-inclusive worst-case debit, and there is deliberately NO
    notional-only fallback."""


def _utc(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts


@dataclass
class ChildOrder:
    """One broker submission attempt for a parent intent.

    ``fee_bps`` (crypto RFC §3.2 E4): the per-side fee schedule value the
    reservation must cover for a fee-bearing asset class (crypto taker bps).
    Defaults to ``0.0`` — every equity child (and every pre-crypto snapshot,
    which has no ``fee_bps`` key) reserves exactly as before.
    """

    child_order_id: str
    attempt_n: int
    requested_qty: float
    price: float  # limit / marketable reference price used for reservation
    submitted_at: dt.datetime
    state: ChildOrderState = ChildOrderState.SUBMITTED
    filled_qty: float = 0.0
    fee_bps: float = 0.0

    @property
    def unfilled_qty(self) -> float:
        return self.requested_qty - self.filled_qty

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_CHILD_STATES

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "child_order_id": self.child_order_id,
            "attempt_n": self.attempt_n,
            "requested_qty": self.requested_qty,
            "price": self.price,
            "submitted_at": self.submitted_at.isoformat(),
            "state": self.state.value,
            "filled_qty": self.filled_qty,
            "fee_bps": self.fee_bps,
        }

    @classmethod
    def from_snapshot(cls, row: Mapping[str, Any]) -> "ChildOrder":
        return cls(
            child_order_id=str(row["child_order_id"]),
            attempt_n=int(row["attempt_n"]),
            requested_qty=float(row["requested_qty"]),
            price=float(row["price"]),
            submitted_at=_utc(dt.datetime.fromisoformat(str(row["submitted_at"]))),
            state=ChildOrderState(row["state"]),
            filled_qty=float(row["filled_qty"]),
            # Pre-crypto snapshots have no fee_bps key: default 0.0 (equity).
            fee_bps=float(row.get("fee_bps", 0.0)),
        )


@dataclass
class ParentIntent:
    """One decision row: the SS7 dedup unit with its cumulative accounting."""

    parent_intent_id: str
    account: str
    symbol: str
    trading_day: str
    side: str
    signal_version: str
    target_qty: float
    children: list[ChildOrder] = field(default_factory=list)
    cum_canceled: float = 0.0
    cum_rejected: float = 0.0
    cum_expired: float = 0.0

    # -- economic accounting -------------------------------------------------
    @property
    def cum_filled(self) -> float:
        return sum(c.filled_qty for c in self.children)

    @property
    def open_qty(self) -> float:
        return sum(c.unfilled_qty for c in self.children if c.is_open)

    @property
    def remaining_unsubmitted(self) -> float:
        """target_qty - cum_filled - open_qty (economic invariant, >= 0)."""
        return self.target_qty - self.cum_filled - self.open_qty

    # -- audit accounting ----------------------------------------------------
    @property
    def gross_submitted_qty(self) -> float:
        """Audit invariant: monotone gross of all attempts; MAY exceed target."""
        return (
            self.cum_filled
            + self.open_qty
            + self.cum_canceled
            + self.cum_rejected
            + self.cum_expired
        )

    @property
    def open_child(self) -> ChildOrder | None:
        for c in self.children:
            if c.is_open:
                return c
        return None

    @property
    def state(self) -> LifecycleState:
        if not self.children:
            return LifecycleState.INTENDED
        if self.cum_filled >= self.target_qty - _QTY_EPS:
            return LifecycleState.FILLED
        open_child = self.open_child
        if open_child is not None:
            if open_child.state is ChildOrderState.STALE_PENDING:
                return LifecycleState.STALE_PENDING
            if self.cum_filled > _QTY_EPS:
                return LifecycleState.PARTIALLY_FILLED
            if open_child.state is ChildOrderState.ACCEPTED:
                return LifecycleState.ACCEPTED
            return LifecycleState.SUBMITTED
        last = self.children[-1]
        if last.state is ChildOrderState.REJECTED:
            return LifecycleState.REJECTED
        # CANCELED and (DAY) EXPIRED both map to the diagram's CANCELED branch;
        # the parent stays re-emit eligible per the canceled-remainder policy.
        return LifecycleState.CANCELED

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "parent_intent_id": self.parent_intent_id,
            "account": self.account,
            "symbol": self.symbol,
            "trading_day": self.trading_day,
            "side": self.side,
            "signal_version": self.signal_version,
            "target_qty": self.target_qty,
            "children": [c.to_snapshot() for c in self.children],
            "cum_canceled": self.cum_canceled,
            "cum_rejected": self.cum_rejected,
            "cum_expired": self.cum_expired,
        }

    @classmethod
    def from_snapshot(cls, row: Mapping[str, Any]) -> "ParentIntent":
        parent = cls(
            parent_intent_id=str(row["parent_intent_id"]),
            account=str(row["account"]),
            symbol=str(row["symbol"]),
            trading_day=str(row["trading_day"]),
            side=str(row["side"]),
            signal_version=str(row["signal_version"]),
            target_qty=float(row["target_qty"]),
            cum_canceled=float(row["cum_canceled"]),
            cum_rejected=float(row["cum_rejected"]),
            cum_expired=float(row["cum_expired"]),
        )
        seen: set[str] = set()
        for child_row in row.get("children", []):
            child = ChildOrder.from_snapshot(child_row)
            if child.child_order_id in seen:
                raise DuplicateChildOrderError(
                    f"snapshot integrity: duplicate child_order_id {child.child_order_id!r}"
                )
            seen.add(child.child_order_id)
            parent.children.append(child)
        return parent


class CashLedgerPort(Protocol):
    """Seam to the account-scoped cash reservation ledger (crypto RFC §5.3).

    Implemented by :class:`renquant_execution.account_cash_ledger.
    AccountCashLedger`; declared here as a Protocol so this module never
    imports the ledger (the ledger imports :data:`MAX_PENDING_AGE_SECONDS`
    from here for its TTL convention). ``None`` everywhere = flag OFF =
    byte-identical legacy behavior.
    """

    def reserve_entry(
        self,
        *,
        sleeve_tag: str,
        parent_intent_id: str,
        notional: float,
        cost_spec: Any,
    ) -> bool:
        """Atomic account-wide headroom check + reservation (idempotent) of
        the WORST-CASE EXECUTABLE DEBIT: notional plus per-side costs from
        the canonical cost contract (``renquant_common.cost_model``,
        required — raises :class:`CostContractUnavailableError` when the
        contract is absent; there is NO notional-only path through this
        seam). The reservation row is stamped with the cost-spec content
        sha."""
        ...

    def release(self, parent_intent_id: str, *, reason: str) -> bool:
        """Idempotent release on a fill/cancel/reject lifecycle transition."""
        ...

    def recheck_before_submit(self) -> bool:
        """Broker-cash recheck immediately before the submit API call."""
        ...

    def halt(self, reason: str) -> None:
        """Sticky account-wide fail-closed for new entries (every sleeve)."""
        ...

    def halt_state(self) -> "tuple[bool, str | None]":
        """(halted, reason) for the shared account."""
        ...

    def sweep(
        self,
        *,
        broker_open_buy_intents: Any,
        local_inflight_intents: Any,
    ) -> Any:
        """§5.3 orphan sweep; result carries halted/halt_reason attributes."""
        ...


@dataclass(frozen=True)
class ReconcileMismatch:
    kind: str  # unknown_broker_order | missing_at_broker | qty_mismatch
    child_order_id: str
    book_qty: float | None = None
    broker_qty: float | None = None


@dataclass(frozen=True)
class ReconcileResult:
    clean: bool
    mismatches: tuple[ReconcileMismatch, ...] = ()


# ---------------------------------------------------------------------------
# Amendment A2: verified broker-rule regime + intraday entry envelope.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerRegimeSnapshot:
    """Recorded broker-effective rule regime for one session (A2 SS11 blocker).

    Queried read-only from the broker account endpoint before the first
    canary tick and recorded in the run bundle. The envelope binds against
    THESE recorded fields (verify-then-bind, never hardcode).

    ``pattern_day_trader`` / ``daytrade_count`` are recorded for the audit
    trail only -- they are DEPRECATED under the FINRA intraday-margin regime
    (effective 2026-06-04) and MUST NEVER gate any decision.
    """

    account_type: str  # "margin" | "cash"
    non_marginable_buying_power: float
    intraday_margin_deficit: float = 0.0  # broker-reported deficit/adjustment
    pattern_day_trader: bool = False  # recorded only; never gates
    daytrade_count: int = 0  # recorded only; never gates

    def to_record(self) -> dict[str, Any]:
        """Run-bundle record of the regime the session was designed against."""
        return {
            "schema_version": ORDER_STATE_SCHEMA_VERSION,
            "account_type": self.account_type,
            "non_marginable_buying_power": self.non_marginable_buying_power,
            "intraday_margin_deficit": self.intraday_margin_deficit,
            "pattern_day_trader_deprecated": self.pattern_day_trader,
            "daytrade_count_deprecated": self.daytrade_count,
        }


@dataclass(frozen=True)
class IntradayEntryEnvelope:
    """Pre-declared entry constraints (A2 SS10): bind ENTRIES only, never exits.

    ``max_entry_fraction`` is the pre-declared fraction of
    ``non_marginable_buying_power`` (consistent with the pinned
    ``execution.buying_power_mode``) that intraday entries may consume,
    including open/pending buy children (consistent with SS7 reserved_cash).
    """

    designed_account_type: str = "margin"
    max_entry_fraction: float = 0.15


#: Envelope block reasons that are Tier-1 conditions: they halt new entries
#: for the session (sticky), not just the one order.
TIER1_ENTRY_BLOCK_REASONS = frozenset(
    {"broker_rule_regime_mismatch", "intraday_margin_deficit"}
)


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: str  # "ok" or block reason
    headroom: float


def evaluate_entry_headroom(
    envelope: IntradayEntryEnvelope,
    regime: BrokerRegimeSnapshot,
    *,
    entry_notional: float,
    reserved_cash: float,
) -> EntryDecision:
    """A2 entry check: live buying-power headroom, NO day-trade counting.

    Order of severity: a regime mismatch or broker-reported intraday margin
    deficit is Tier-1 (session halt); insufficient headroom only refuses this
    entry. Exits must never be routed through this check.
    """
    headroom = (
        envelope.max_entry_fraction * regime.non_marginable_buying_power
        - reserved_cash
    )
    if regime.account_type != envelope.designed_account_type:
        return EntryDecision(False, "broker_rule_regime_mismatch", headroom)
    if regime.intraday_margin_deficit > 0:
        return EntryDecision(False, "intraday_margin_deficit", headroom)
    if entry_notional > headroom + _QTY_EPS:
        return EntryDecision(False, "insufficient_buying_power_headroom", headroom)
    return EntryDecision(True, "ok", headroom)


# ---------------------------------------------------------------------------
# The state book: all parents for one (account, trading_day) session.
# ---------------------------------------------------------------------------


class OrderStateBook:
    """SS7 state machine + invariants for one (account, trading_day) session.

    Pure in-memory state; the caller owns persistence (snapshot) and all
    broker I/O (via :class:`BrokerPort` and the driver helpers below).
    """

    def __init__(
        self,
        *,
        account: str,
        trading_day: str,
        cash_ledger: CashLedgerPort | None = None,
        cost_model_spec: Any | None = None,
    ):
        self.account = str(account)
        self.trading_day = str(trading_day)
        self._parents: dict[str, ParentIntent] = {}
        self.entries_halted = False
        self.halt_reason: str | None = None
        # reconcile-before-emit: set on restore; cleared only by reconcile().
        self._needs_reconcile = False
        # defense-in-depth: audit invariant must be monotone non-decreasing.
        self._gross_high_water: dict[str, float] = {}
        # Crypto RFC §5.3 account-scoped cash reservation ledger. Default
        # None = flag OFF = byte-identical legacy behavior (the per-tag
        # reserved_cash() view stays the headroom source). When attached,
        # BUY reservations are released on the SAME fill/cancel/reject
        # transitions this book already owns, and every reservation is the
        # WORST-CASE EXECUTABLE DEBIT (notional + per-side costs) computed
        # through the canonical cost contract — so a ledger-attached book
        # REQUIRES a cost_model_spec at construction (fail closed HERE,
        # before any order could be submitted, never mid-session).
        self._require_cost_spec_with_ledger(cash_ledger, cost_model_spec)
        self.cash_ledger = cash_ledger
        self.cost_model_spec = cost_model_spec
        # Cost-contract run evidence: the content sha of the cost spec this
        # session reserves with (stamped by the open_session_order_book
        # wiring factory; also stamped per reservation row by the ledger).
        self.cost_model_sha256: str | None = None
        # Last §5.3 orphan-sweep result (set by reconcile_on_restart when a
        # ledger is attached) — the caller's alerting hook reads it here:
        # orphans are released + COUNTED + ALERTED, never silently cleaned.
        self.last_ledger_sweep: Any | None = None

    @staticmethod
    def _require_cost_spec_with_ledger(
        cash_ledger: CashLedgerPort | None, cost_model_spec: Any | None
    ) -> None:
        if cash_ledger is not None and cost_model_spec is None:
            raise LifecycleError(
                "a cash-ledger-attached book requires a cost_model_spec: "
                "reservations are the worst-case executable debit (notional "
                "+ per-side costs per renquant_common.cost_model) — refusing "
                "at construction rather than failing every BUY at submit "
                f"({ACCOUNT_CASH_COST_CONTRACT_UNAVAILABLE_REASON})"
            )

    # -- introspection -------------------------------------------------------
    @property
    def needs_reconcile(self) -> bool:
        return self._needs_reconcile

    def parents(self) -> list[ParentIntent]:
        return list(self._parents.values())

    def parent(self, parent_intent_id: str) -> ParentIntent:
        try:
            return self._parents[parent_intent_id]
        except KeyError:
            raise LifecycleError(f"unknown parent_intent_id: {parent_intent_id!r}") from None

    def lifecycle_state(self, parent_intent_id: str) -> LifecycleState:
        """Parent state, or NONE when the intent was never registered."""
        parent = self._parents.get(parent_intent_id)
        return parent.state if parent is not None else LifecycleState.NONE

    def open_children(self) -> list[ChildOrder]:
        return [c for p in self._parents.values() for c in p.children if c.is_open]

    def _child(self, child_order_id_: str) -> tuple[ParentIntent, ChildOrder]:
        for parent in self._parents.values():
            for child in parent.children:
                if child.child_order_id == child_order_id_:
                    return parent, child
        raise LifecycleError(f"unknown child_order_id: {child_order_id_!r}")

    # -- invariants ----------------------------------------------------------
    def _assert_invariants(self, parent: ParentIntent) -> None:
        if parent.cum_filled + parent.open_qty > parent.target_qty + _QTY_EPS:
            raise EconomicInvariantError(
                f"{parent.parent_intent_id}: cum_filled({parent.cum_filled}) + "
                f"open_qty({parent.open_qty}) > target_qty({parent.target_qty})"
            )
        if parent.remaining_unsubmitted < -_QTY_EPS:
            raise EconomicInvariantError(
                f"{parent.parent_intent_id}: remaining_unsubmitted "
                f"{parent.remaining_unsubmitted} < 0"
            )
        gross = parent.gross_submitted_qty
        high = self._gross_high_water.get(parent.parent_intent_id, 0.0)
        if gross < high - _QTY_EPS:
            raise EconomicInvariantError(
                f"{parent.parent_intent_id}: gross_submitted_qty regressed "
                f"{high} -> {gross} (audit invariant must be monotone)"
            )
        self._gross_high_water[parent.parent_intent_id] = max(gross, high)

    # -- session-level controls ----------------------------------------------
    def halt_entries(self, reason: str) -> None:
        """Halt new ENTRIES for the session; exits stay allowed (SS7/A2)."""
        self.entries_halted = True
        self.halt_reason = reason

    def attach_cash_ledger(
        self,
        cash_ledger: CashLedgerPort | None,
        *,
        cost_model_spec: Any | None = None,
    ) -> None:
        """Attach (or detach) the §5.3 account cash ledger.

        Snapshots never carry the ledger (it is runtime wiring, not state);
        a restored book must be re-attached by the caller before its
        reconcile-before-emit pass so the ledger sweep runs. Attaching a
        ledger requires the cost_model_spec, same as construction (fail
        closed before any order could be submitted).
        """
        self._require_cost_spec_with_ledger(cash_ledger, cost_model_spec)
        self.cash_ledger = cash_ledger
        self.cost_model_spec = cost_model_spec

    def _release_cash_reservation(self, parent: ParentIntent, reason: str) -> None:
        """§5.3 release() on the SAME lifecycle transition that observed the
        terminal event (fill/cancel/reject/expire) — BUY parents only, no-op
        when no ledger is attached (flag OFF) or already released (hooks may
        race)."""
        if self.cash_ledger is None or parent.side != SIDE_BUY:
            return
        self.cash_ledger.release(parent.parent_intent_id, reason=reason)

    # -- transitions ----------------------------------------------------------
    def register_intent(
        self,
        *,
        symbol: str,
        side: str,
        signal_version: str,
        target_qty: float,
    ) -> ParentIntent:
        """NONE -> INTENDED. Idempotent on the parent_intent_id dedup key."""
        side_u = str(side).upper()
        if side_u not in _VALID_SIDES:
            raise LifecycleError(f"unsupported side: {side!r}")
        target = float(target_qty)
        if target <= 0:
            raise LifecycleError(f"target_qty must be positive: {target_qty!r}")
        symbol_u = str(symbol).upper()
        pid = compute_parent_intent_id(
            account=self.account,
            symbol=symbol_u,
            trading_day=self.trading_day,
            side=side_u,
            signal_version=str(signal_version),
        )
        existing = self._parents.get(pid)
        if existing is not None:
            if abs(existing.target_qty - target) > _QTY_EPS:
                raise LifecycleError(
                    f"{pid}: re-registered with different target_qty "
                    f"({existing.target_qty} != {target}); Stage-1 targets are "
                    "immutable within a session"
                )
            return existing
        if side_u == SIDE_BUY:
            # SS7 re-emit rule: at most one filled position per name per
            # session. A second BUY decision for the same name is refused once
            # the first has economic effect (fills or an open child).
            for other in self._parents.values():
                if (
                    other.side == SIDE_BUY
                    and other.symbol == symbol_u
                    and (other.cum_filled > _QTY_EPS or other.open_qty > _QTY_EPS)
                ):
                    raise LifecycleError(
                        f"one filled position per name per session: {symbol_u} "
                        f"already has BUY parent {other.parent_intent_id} with "
                        "fills or an open child"
                    )
        parent = ParentIntent(
            parent_intent_id=pid,
            account=self.account,
            symbol=symbol_u,
            trading_day=self.trading_day,
            side=side_u,
            signal_version=str(signal_version),
            target_qty=target,
        )
        self._parents[pid] = parent
        return parent

    def can_emit_remainder(self, parent_intent_id: str, *, gate_admitted: bool = True) -> bool:
        """SS7 re-emit rule (canceled-remainder eligibility included)."""
        parent = self.parent(parent_intent_id)
        if self._needs_reconcile:
            return False
        if parent.open_child is not None:
            return False
        if parent.cum_filled >= parent.target_qty - _QTY_EPS:
            return False
        if parent.side == SIDE_BUY and (self.entries_halted or not gate_admitted):
            return False
        return True

    def submit_child(
        self,
        parent_intent_id: str,
        *,
        qty: float,
        price: float,
        now: dt.datetime,
        gate_admitted: bool = True,
        fee_bps: float = 0.0,
    ) -> ChildOrder:
        """Open a new child submission (INTENDED/CANCELED -> SUBMITTED).

        Enforces, in order: reconcile-before-emit; entry policy (BUY only:
        session halt + gate-stack admission -- exits are never policy-blocked);
        the one-OPEN-child rule; and the SS7 hard economic assertion
        ``cum_filled + open_qty <= target_qty`` with ``qty`` capped at
        ``remaining_unsubmitted`` so retries can never overfill.
        """
        parent = self.parent(parent_intent_id)
        if self._needs_reconcile:
            raise LifecycleError(
                "reconcile-before-emit: book restored from snapshot; reconcile "
                "against broker open-orders before any submit"
            )
        qty_f = float(qty)
        if qty_f <= 0:
            raise LifecycleError(f"child qty must be positive: {qty!r}")
        price_f = float(price)
        if price_f <= 0:
            raise LifecycleError(f"child price must be positive: {price!r}")
        fee_bps_f = float(fee_bps)
        if fee_bps_f < 0:
            raise LifecycleError(f"child fee_bps must be >= 0: {fee_bps!r}")
        if parent.side == SIDE_BUY:
            if self.entries_halted:
                raise EntryBlockedError(self.halt_reason or "entries_halted")
            if not gate_admitted:
                raise EntryBlockedError("gate_stack_rejected")
        if parent.open_child is not None:
            raise LifecycleError(
                f"{parent.parent_intent_id}: at most one OPEN child per parent "
                f"(open: {parent.open_child.child_order_id})"
            )
        if parent.cum_filled >= parent.target_qty - _QTY_EPS:
            raise LifecycleError(
                f"{parent.parent_intent_id}: target already reached; a parent "
                "reaches its target then stops (Stage-1 re-emit rule)"
            )
        # SS7 hard assertion before EVERY submit.
        if parent.cum_filled + parent.open_qty > parent.target_qty + _QTY_EPS:
            raise EconomicInvariantError(
                f"{parent.parent_intent_id}: cum_filled + open_qty exceeds "
                "target_qty before submit"
            )
        if qty_f > parent.remaining_unsubmitted + _QTY_EPS:
            raise EconomicInvariantError(
                f"{parent.parent_intent_id}: child qty {qty_f} > "
                f"remaining_unsubmitted {parent.remaining_unsubmitted}; "
                "a remainder child requests at most remaining_unsubmitted"
            )
        attempt_n = max((c.attempt_n for c in parent.children), default=0) + 1
        cid = child_order_id(parent.parent_intent_id, attempt_n)
        if any(c.child_order_id == cid for c in parent.children):
            raise DuplicateChildOrderError(f"duplicate child_order_id: {cid!r}")
        child = ChildOrder(
            child_order_id=cid,
            attempt_n=attempt_n,
            requested_qty=qty_f,
            price=price_f,
            submitted_at=_utc(now),
            fee_bps=fee_bps_f,
        )
        parent.children.append(child)
        self._assert_invariants(parent)
        return child

    def on_broker_ack(self, child_order_id_: str) -> ChildOrder:
        """SUBMITTED -> ACCEPTED."""
        parent, child = self._child(child_order_id_)
        if child.state is not ChildOrderState.SUBMITTED:
            raise LifecycleError(
                f"{child_order_id_}: broker ack from state {child.state.value}"
            )
        child.state = ChildOrderState.ACCEPTED
        self._assert_invariants(parent)
        return child

    def on_fill(self, child_order_id_: str, qty: float) -> ChildOrder:
        """Apply an (incremental) fill; full fill -> FILLED."""
        parent, child = self._child(child_order_id_)
        if not child.is_open:
            raise LifecycleError(
                f"{child_order_id_}: fill on non-open state {child.state.value}"
            )
        qty_f = float(qty)
        if qty_f <= 0:
            raise LifecycleError(f"fill qty must be positive: {qty!r}")
        if child.filled_qty + qty_f > child.requested_qty + _QTY_EPS:
            raise EconomicInvariantError(
                f"{child_order_id_}: fill {qty_f} would exceed requested "
                f"{child.requested_qty} (filled {child.filled_qty})"
            )
        child.filled_qty += qty_f
        if child.filled_qty >= child.requested_qty - _QTY_EPS:
            child.state = ChildOrderState.FILLED
            # §5.3: a full fill is a terminal path for this child's cash —
            # the spent amount is real broker cash now, visible to the next
            # reserve() through its fresh broker_cash fetch.
            self._release_cash_reservation(parent, "filled")
        elif child.state is not ChildOrderState.STALE_PENDING:
            child.state = ChildOrderState.PARTIALLY_FILLED
        self._assert_invariants(parent)
        return child

    def _close_open_child(
        self, child_order_id_: str, terminal: ChildOrderState, counter: str
    ) -> ChildOrder:
        parent, child = self._child(child_order_id_)
        if not child.is_open:
            raise LifecycleError(
                f"{child_order_id_}: {terminal.value.lower()} on non-open "
                f"state {child.state.value}"
            )
        remainder = child.unfilled_qty
        child.state = terminal
        setattr(parent, counter, getattr(parent, counter) + remainder)
        # §5.3 release() on the same cancel/reject/expire transition. A
        # re-emitted remainder must go back through reserve() (which
        # re-activates the released row against fresh headroom).
        self._release_cash_reservation(parent, terminal.value.lower())
        self._assert_invariants(parent)
        return child

    def on_cancel(self, child_order_id_: str) -> ChildOrder:
        """OPEN -> CANCELED; unfilled remainder moves to cum_canceled."""
        return self._close_open_child(
            child_order_id_, ChildOrderState.CANCELED, "cum_canceled"
        )

    def on_reject(self, child_order_id_: str) -> ChildOrder:
        """OPEN -> REJECTED; unfilled remainder moves to cum_rejected."""
        return self._close_open_child(
            child_order_id_, ChildOrderState.REJECTED, "cum_rejected"
        )

    def on_expire(self, child_order_id_: str) -> ChildOrder:
        """OPEN -> EXPIRED (DAY expiry); remainder moves to cum_expired.

        Stage 1 pre-empts expiry via the SS11b close-cancel, but the counter
        exists for reconciliation (SS7).
        """
        return self._close_open_child(
            child_order_id_, ChildOrderState.EXPIRED, "cum_expired"
        )

    def apply_terminal_status(
        self, child_order_id_: str, *, status: Any, filled_qty: float = 0.0
    ) -> ChildOrder:
        """Resolve one OPEN child through a broker terminal status row.

        The S-FRAC stage-1 terminal classification, fill-first by contract:

        1. **The filled portion is REAL.** ``filled_qty`` is the broker's
           cumulative fill for this child at terminal sight; any delta above
           the book's view is applied BEFORE the terminal transition. This is
           the cancel-with-fill / partial-fill-then-expire rule: a cancel or
           DAY expiry whose confirmation carries first-sight fills books those
           fills — they are position and cash, not bookkeeping noise.
        2. If the fill completes the child it lands on FILLED regardless of
           the reported status (a cancel/expiry that raced a full fill).
        3. Otherwise the status maps through :data:`TERMINAL_STATUS_MAP` and
           the unfilled remainder moves to the matching audit counter
           (``cum_canceled`` / ``cum_rejected`` / ``cum_expired``) — thereby
           returning to ``remaining_unsubmitted`` for the SS7 re-emit rule,
           with the audit trail preserved (``gross_submitted_qty`` monotone).

        Fail-loud guards: a non-terminal/unknown status raises (leave the
        child open and let reconcile surface it instead); a ``filled`` status
        whose reported quantity is SHORT of the request raises rather than
        inventing shares; a broker cumulative BELOW the book's view raises
        through :meth:`on_fill`'s economic guards on the negative delta —
        quantities are never silently mutated in either direction.
        """
        parent, child = self._child(child_order_id_)
        if not child.is_open:
            raise LifecycleError(
                f"{child_order_id_}: terminal status {status!r} on non-open "
                f"state {child.state.value}"
            )
        terminal = classify_terminal_status(status)
        if terminal is None:
            raise LifecycleError(
                f"{child_order_id_}: {status!r} is not a terminal status; "
                "leave the child open and reconcile instead"
            )
        broker_filled = float(filled_qty)
        if broker_filled < child.filled_qty - _QTY_EPS:
            raise EconomicInvariantError(
                f"{child_order_id_}: broker cumulative filled_qty "
                f"{broker_filled} regressed below the book's {child.filled_qty}"
            )
        delta = broker_filled - child.filled_qty
        if delta > _QTY_EPS:
            self.on_fill(child_order_id_, delta)
        if child.state is ChildOrderState.FILLED:
            return child
        if terminal is ChildOrderState.FILLED:
            raise EconomicInvariantError(
                f"{child_order_id_}: broker says filled but cumulative "
                f"filled_qty {broker_filled} is short of requested "
                f"{child.requested_qty}; refusing to invent shares"
            )
        return self._close_open_child(
            child_order_id_, terminal, _TERMINAL_COUNTERS[terminal]
        )

    # -- reserved-cash accounting (SS7) ---------------------------------------
    def reserved_cash(self, *, unsettled_buys: float = 0.0) -> float:
        """Sum of open BUY children notionals (at reference price) + unsettled.

        The unfilled remainder of a partial stays reserved until its child is
        filled or canceled. Sizing must never use raw broker cash.

        Fee awareness (crypto RFC §3.2 E4): a child carrying a non-zero
        ``fee_bps`` (crypto taker schedule) reserves ``notional * (1 +
        fee_bps/1e4)`` so the fee can never be paid out of headroom another
        order was sized against. Equity children (``fee_bps == 0.0``,
        including every pre-crypto snapshot) reserve the exact historical
        notional — byte-identical.
        """
        reserved = float(unsettled_buys)
        for parent in self._parents.values():
            if parent.side != SIDE_BUY:
                continue
            for child in parent.children:
                if child.is_open:
                    notional = child.unfilled_qty * child.price
                    if child.fee_bps:
                        notional *= 1.0 + child.fee_bps / 10_000.0
                    reserved += notional
        return reserved

    def available_cash(self, broker_cash: float, *, unsettled_buys: float = 0.0) -> float:
        """``broker_cash - reserved_cash`` -- the only sizing input (SS7)."""
        return float(broker_cash) - self.reserved_cash(unsettled_buys=unsettled_buys)

    # -- stale-pending watchdog (SS10) ----------------------------------------
    def mark_stale(
        self,
        *,
        now: dt.datetime,
        max_age_seconds: float = MAX_PENDING_AGE_SECONDS,
    ) -> list[ChildOrder]:
        """Transition over-age open children to STALE_PENDING and return them.

        Timer-driven (between ticks): the caller cancels each at the broker
        and reconciles the outcome to CANCELED or FILLED; a decision tick must
        never inherit an already-overdue order.
        """
        now_utc = _utc(now)
        stale: list[ChildOrder] = []
        for parent in self._parents.values():
            for child in parent.children:
                if child.state is ChildOrderState.STALE_PENDING or not child.is_open:
                    continue
                age = (now_utc - child.submitted_at).total_seconds()
                if age > max_age_seconds:
                    child.state = ChildOrderState.STALE_PENDING
                    stale.append(child)
        return stale

    # -- persistence + restart reconciliation (SS7) ---------------------------
    def to_snapshot(self) -> dict[str, Any]:
        """JSON-serializable ledger snapshot of the whole session book.

        ``cost_model_sha256`` (cost-contract run evidence) is included ONLY
        when stamped — a flag-OFF book's snapshot stays byte-identical to
        the pre-ledger schema.
        """
        snapshot = {
            "schema_version": ORDER_STATE_SCHEMA_VERSION,
            "account": self.account,
            "trading_day": self.trading_day,
            "entries_halted": self.entries_halted,
            "halt_reason": self.halt_reason,
            "parents": [p.to_snapshot() for p in self._parents.values()],
        }
        if self.cost_model_sha256 is not None:
            snapshot["cost_model_sha256"] = self.cost_model_sha256
        return snapshot

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "OrderStateBook":
        """Rebuild from the ledger; the book REFUSES all emits until reconciled."""
        version = snapshot.get("schema_version")
        if version != ORDER_STATE_SCHEMA_VERSION:
            raise LifecycleError(f"unsupported snapshot schema_version: {version!r}")
        book = cls(
            account=str(snapshot["account"]),
            trading_day=str(snapshot["trading_day"]),
        )
        book.entries_halted = bool(snapshot.get("entries_halted", False))
        raw_reason = snapshot.get("halt_reason")
        book.halt_reason = str(raw_reason) if raw_reason is not None else None
        raw_cost_sha = snapshot.get("cost_model_sha256")
        book.cost_model_sha256 = (
            str(raw_cost_sha) if raw_cost_sha is not None else None
        )
        for row in snapshot.get("parents", []):
            parent = ParentIntent.from_snapshot(row)
            if parent.parent_intent_id in book._parents:
                raise LifecycleError(
                    f"snapshot integrity: duplicate parent {parent.parent_intent_id!r}"
                )
            book._parents[parent.parent_intent_id] = parent
            book._assert_invariants(parent)
        book._needs_reconcile = True
        return book

    def reconcile(self, broker_open_orders: Mapping[str, float]) -> ReconcileResult:
        """Compare the book's in-flight set against broker open-orders.

        Clean -> emits re-enabled. Any mismatch (broker open-orders != ledger)
        -> halt new entries for the session, return the mismatch data for
        alerting; exits stay allowed (SS7).
        """
        book_open = {c.child_order_id: c.unfilled_qty for c in self.open_children()}
        mismatches: list[ReconcileMismatch] = []
        for cid, broker_qty in broker_open_orders.items():
            if cid not in book_open:
                mismatches.append(
                    ReconcileMismatch(
                        kind="unknown_broker_order",
                        child_order_id=cid,
                        broker_qty=float(broker_qty),
                    )
                )
        for cid, book_qty in book_open.items():
            if cid not in broker_open_orders:
                mismatches.append(
                    ReconcileMismatch(
                        kind="missing_at_broker",
                        child_order_id=cid,
                        book_qty=book_qty,
                    )
                )
            elif abs(float(broker_open_orders[cid]) - book_qty) > _QTY_EPS:
                mismatches.append(
                    ReconcileMismatch(
                        kind="qty_mismatch",
                        child_order_id=cid,
                        book_qty=book_qty,
                        broker_qty=float(broker_open_orders[cid]),
                    )
                )
        self._needs_reconcile = False
        if mismatches:
            self.halt_entries("reconcile_mismatch")
            return ReconcileResult(clean=False, mismatches=tuple(mismatches))
        return ReconcileResult(clean=True)


# ---------------------------------------------------------------------------
# Driven-adapter seam: the broker Protocol + driver helpers.
# ---------------------------------------------------------------------------


class BrokerPort(Protocol):
    """The ONLY seam to a real broker (Alpaca adapter implements this later).

    All methods are keyed on the child_order_id == broker client-order-id.
    """

    def submit_order(
        self, *, client_order_id: str, symbol: str, side: str, qty: float
    ) -> Mapping[str, Any]:
        """Submit; MUST reject a duplicate client_order_id."""
        ...

    def cancel_order(self, client_order_id: str) -> Mapping[str, Any]:
        """Request cancel; returns final ``{"status": ..., "filled_qty": ...}``."""
        ...

    def open_orders(self) -> Mapping[str, float]:
        """Live open orders as ``{client_order_id: unfilled_qty}``."""
        ...

    def order_status(self, client_order_id: str) -> Mapping[str, Any]:
        """Terminal/live status: ``{"status": ..., "filled_qty": ...}``."""
        ...


def _apply_fill_delta(book: OrderStateBook, child: ChildOrder, broker_filled: float) -> None:
    delta = float(broker_filled) - child.filled_qty
    if delta > _QTY_EPS:
        book.on_fill(child.child_order_id, delta)


def submit_remainder(
    book: OrderStateBook,
    port: BrokerPort,
    parent_intent_id: str,
    *,
    price: float,
    now: dt.datetime,
    gate_admitted: bool = True,
    envelope: IntradayEntryEnvelope | None = None,
    regime: BrokerRegimeSnapshot | None = None,
) -> ChildOrder | None:
    """Emit one child sized to ``remaining_unsubmitted`` through the port.

    Applies the A2 entry envelope to BUY parents when ``envelope`` +
    ``regime`` are supplied (Tier-1 block reasons halt the session's entries);
    SELL (exit) parents are NEVER routed through the envelope. Returns None
    when there is nothing left to submit. A broker submit failure is recorded
    as a REJECTED child before the error propagates.

    §5.3 account cash ledger (flag-gated; ``book.cash_ledger is None`` =
    byte-identical legacy path). For BUY parents with a ledger attached:

    1. ``reserve_entry()`` must grant the WORST-CASE EXECUTABLE DEBIT —
       entry notional PLUS per-side costs computed through the canonical
       cost contract (``renquant_common.cost_model``; the reservation row
       is stamped with the cost-spec content sha) — against the SHARED
       account headroom before any child exists. A refusal is
       ``insufficient_buying_power_headroom`` (the existing A2 reason,
       reused not duplicated) — unless the account is fail-closed, in which
       case the halt reason propagates and the session's entries halt too.
       An absent/unverifiable cost contract FAILS CLOSED
       (``account_cash_cost_contract_unavailable``) — there is no
       notional-only fallback.
    2. Immediately before the actual order-submit API call, the broker-cash
       recheck runs; a mismatch refuses THIS entry (child -> REJECTED,
       reservation released on that same transition) and fail-closes new
       entries across EVERY sleeve (§5.3 Codex round 2).

    SELL (exit) parents never touch the ledger: exits and protective-stop
    maintenance can never be blocked by it (§5.4 precedence).
    """
    parent = book.parent(parent_intent_id)
    qty = parent.remaining_unsubmitted
    if qty <= _QTY_EPS:
        return None
    if parent.side == SIDE_BUY and envelope is not None and regime is not None:
        decision = evaluate_entry_headroom(
            envelope,
            regime,
            entry_notional=qty * float(price),
            reserved_cash=book.reserved_cash(),
        )
        if not decision.allowed:
            if decision.reason in TIER1_ENTRY_BLOCK_REASONS:
                book.halt_entries(decision.reason)
            raise EntryBlockedError(decision.reason)
    ledger = book.cash_ledger if parent.side == SIDE_BUY else None
    if ledger is not None:
        if book.cost_model_spec is None:
            # Unreachable through the constructor/attach guard, but a book
            # mutated around it must still fail closed, never reserve
            # notional-only.
            raise EntryBlockedError(ACCOUNT_CASH_COST_CONTRACT_UNAVAILABLE_REASON)
        try:
            granted = ledger.reserve_entry(
                sleeve_tag=book.account,
                parent_intent_id=parent.parent_intent_id,
                notional=qty * float(price),
                cost_spec=book.cost_model_spec,
            )
        except CostContractUnavailableError:
            raise EntryBlockedError(
                ACCOUNT_CASH_COST_CONTRACT_UNAVAILABLE_REASON
            ) from None
        if not granted:
            halted, halt_reason = ledger.halt_state()
            if halted:
                reason = halt_reason or ACCOUNT_CASH_RECONCILE_MISMATCH_REASON
                book.halt_entries(reason)
                raise EntryBlockedError(reason)
            raise EntryBlockedError("insufficient_buying_power_headroom")
    try:
        child = book.submit_child(
            parent_intent_id,
            qty=qty,
            price=price,
            now=now,
            gate_admitted=gate_admitted,
        )
    except BaseException:
        if ledger is not None:
            # The reservation must not outlive a submit that never happened.
            ledger.release(parent.parent_intent_id, reason="submit_failed")
        raise
    if ledger is not None and not ledger.recheck_before_submit():
        # §5.3: broker cash moved below what the ledger believes is reserved
        # — a REAL reconciliation mismatch, not a soft warning. Refuse this
        # entry (REJECTED child; the transition releases the reservation)
        # and fail closed for every sleeve; the ledger halt is already
        # sticky, mirror it on this session book.
        book.on_reject(child.child_order_id)
        _, halt_reason = ledger.halt_state()
        reason = halt_reason or ACCOUNT_CASH_RECONCILE_MISMATCH_REASON
        book.halt_entries(reason)
        raise EntryBlockedError(reason)
    try:
        port.submit_order(
            client_order_id=child.child_order_id,
            symbol=parent.symbol,
            side=parent.side,
            qty=child.requested_qty,
        )
    except Exception:
        book.on_reject(child.child_order_id)
        raise
    return child


def run_stale_watchdog(
    book: OrderStateBook,
    port: BrokerPort,
    *,
    now: dt.datetime,
    max_age_seconds: float = MAX_PENDING_AGE_SECONDS,
) -> list[ChildOrder]:
    """Timer-driven stale-pending cancel (SS10): cancel + reconcile over-age
    children *between* decision ticks so a tick never inherits an overdue
    order. Each stale child resolves to CANCELED or (if the cancel raced a
    fill) FILLED, per the SS7 STALE_PENDING branch.
    """
    resolved: list[ChildOrder] = []
    for child in book.mark_stale(now=now, max_age_seconds=max_age_seconds):
        outcome = port.cancel_order(child.child_order_id)
        _apply_fill_delta(book, child, float(outcome.get("filled_qty", 0.0)))
        if child.state is not ChildOrderState.FILLED:
            book.on_cancel(child.child_order_id)
        resolved.append(child)
    return resolved


def resolve_day_expiry(book: OrderStateBook, port: BrokerPort) -> list[ChildOrder]:
    """End-of-session DAY-expiry sweep (S-FRAC stage 1, design SS4).

    Fractional orders are TIF=DAY only, so any child the book still holds
    OPEN at/after the close must be resolved to a terminal outcome — never
    carried overnight as a resting order (same-day reconciliation only, no
    GTC carryover bookkeeping). For each open child the broker's status is
    fetched and, when terminal, resolved through
    :meth:`OrderStateBook.apply_terminal_status`:

    * unfilled DAY expiry -> EXPIRED, full quantity to ``cum_expired``;
    * **partial-fill-then-expire** -> the filled portion is REAL (already or
      now booked via the fill-first rule) and only the unfilled remainder
      moves to ``cum_expired``, returning to ``remaining_unsubmitted`` with
      the audit trail intact;
    * a cancel/expiry that raced a full fill -> FILLED.

    Children the broker still reports non-terminal are left OPEN and
    returned to the caller's reconcile path. Returns the resolved children.

    Crypto exemption (crypto RFC §3.2 E9): crypto orders are GTC/IOC on a
    24/7 market — there IS no equity close for them, and a resting GTC
    crypto order (protective stop, resting limit) is a legitimate overnight
    state, not a leftover. The sweep therefore SKIPS every crypto-pair
    child entirely (no status fetch, no terminal resolution), so an
    end-of-equity-session sweep can never wrongly terminate — or even
    touch — a resting crypto order. Stale non-protective crypto GTC orders
    are the crypto sleeve's ``max_resting_age`` watchdog's job (RFC §3.2),
    not this sweep's.
    """
    resolved: list[ChildOrder] = []
    for parent in book.parents():
        if is_crypto_pair(parent.symbol):
            continue  # E9: crypto orders never expire at an equity close
        for child in parent.children:
            if not child.is_open:
                continue
            status_row = port.order_status(child.child_order_id)
            status = status_row.get("status", "")
            if classify_terminal_status(status) is None:
                continue
            book.apply_terminal_status(
                child.child_order_id,
                status=status,
                filled_qty=float(status_row.get("filled_qty", 0.0)),
            )
            resolved.append(child)
    return resolved


def reconcile_on_restart(book: OrderStateBook, port: BrokerPort) -> ReconcileResult:
    """SS7 reconcile-before-emit: rebuild the in-flight picture from broker
    open-orders + per-order terminal statuses, then reconcile the book.

    Children the broker no longer lists as open are resolved through their
    terminal status (fills applied first, so a cancel that raced a fill lands
    on FILLED). Only after this does :meth:`OrderStateBook.reconcile` compare
    the surviving in-flight set; any mismatch halts entries (exits allowed).

    §5.3 ledger reconciliation (only when ``book.cash_ledger`` is attached;
    ``None`` = byte-identical): the same pass additionally runs the account
    cash ledger's orphan sweep — active reservations with no broker open
    order and no local in-flight state are released/counted/alerted; a
    broker open BUY with no active reservation (external/manual order,
    headroom leak) fail-closes new entries for EVERY sleeve sharing the
    account; and a book reconcile mismatch escalates from a session halt to
    that same account-wide halt (an unrecognized order means some path is
    moving cash the ledger doesn't know about).
    """
    broker_open = dict(port.open_orders())
    for child in list(book.open_children()):
        if child.child_order_id in broker_open:
            continue
        status_row = port.order_status(child.child_order_id)
        status = status_row.get("status", "")
        broker_filled = float(status_row.get("filled_qty", 0.0))
        terminal = classify_terminal_status(status)
        if terminal is not None and terminal is not ChildOrderState.FILLED:
            # Shared terminal vocabulary (fills applied first, so a cancel or
            # DAY expiry that raced a fill keeps the REAL filled portion and
            # a full-fill race lands on FILLED).
            book.apply_terminal_status(
                child.child_order_id, status=status, filled_qty=broker_filled
            )
            continue
        # "filled" (fills carry the transition) or a non-terminal/unknown
        # status: apply fills; a short "filled" or unknown status is left
        # open and will surface as a reconcile mismatch.
        _apply_fill_delta(book, child, broker_filled)
    result = book.reconcile(broker_open)
    ledger = book.cash_ledger
    if ledger is not None:
        book_children = {
            c.child_order_id: p for p in book.parents() for c in p.children
        }
        broker_buy_intents: set[str] = set()
        for cid in broker_open:
            known = book_children.get(cid)
            if known is not None and known.side != SIDE_BUY:
                continue  # a known SELL never reserves cash
            # Known BUY, or UNKNOWN (side unknowable -> conservative: treat
            # as a buy so it fails closed through the sweep).
            broker_buy_intents.add(parent_intent_id_from_client_order_id(cid))
        local_inflight = {
            p.parent_intent_id
            for p in book.parents()
            if p.side == SIDE_BUY
            and (p.open_qty > _QTY_EPS or p.remaining_unsubmitted > _QTY_EPS)
        }
        book.last_ledger_sweep = ledger.sweep(
            broker_open_buy_intents=broker_buy_intents,
            local_inflight_intents=local_inflight,
        )
        if not result.clean:
            ledger.halt(ACCOUNT_CASH_RECONCILE_MISMATCH_REASON)
        halted, halt_reason = ledger.halt_state()
        if halted:
            book.halt_entries(halt_reason or ACCOUNT_CASH_RECONCILE_MISMATCH_REASON)
    return result


__all__ = [
    "ACCOUNT_CASH_COST_CONTRACT_UNAVAILABLE_REASON",
    "ACCOUNT_CASH_RECONCILE_MISMATCH_REASON",
    "BrokerPort",
    "BrokerRegimeSnapshot",
    "CashLedgerPort",
    "ChildOrder",
    "CostContractUnavailableError",
    "ChildOrderState",
    "DuplicateChildOrderError",
    "EconomicInvariantError",
    "EntryBlockedError",
    "EntryDecision",
    "IntradayEntryEnvelope",
    "LifecycleError",
    "LifecycleState",
    "MAX_PENDING_AGE_SECONDS",
    "OPEN_CHILD_STATES",
    "ORDER_STATE_SCHEMA_VERSION",
    "OrderStateBook",
    "ParentIntent",
    "ReconcileMismatch",
    "ReconcileResult",
    "SIDE_BUY",
    "SIDE_SELL",
    "TERMINAL_STATUS_MAP",
    "TIER1_ENTRY_BLOCK_REASONS",
    "child_order_id",
    "classify_terminal_status",
    "compute_parent_intent_id",
    "evaluate_entry_headroom",
    "parent_intent_id_from_client_order_id",
    "reconcile_on_restart",
    "resolve_day_expiry",
    "run_stale_watchdog",
    "submit_remainder",
]
