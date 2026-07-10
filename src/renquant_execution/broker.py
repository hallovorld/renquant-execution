"""Broker contracts owned by renquant-execution."""
from __future__ import annotations

from abc import ABC, abstractmethod
import math
from typing import Any

# S-FRAC stage-1 quantity-epsilon discipline.
#
# REPLICATED from the S-FRAC stage-0 commit contract (RenQuant umbrella,
# ``backtesting/renquant_104/adapters/commit_contract.py::QTY_INTEGRAL_EPS``,
# merged in RenQuant#439). The two repos cannot import each other, so the
# constant is replicated verbatim and pinned by a literal constant-equality
# test (tests/test_order_state_machine.py) — if either side ever changes the
# epsilon, that test is the tripwire, not a silent drift.
#
# Semantics (same as stage-0 ``normalize_fill_qty`` / ``is_integral_qty``): a
# quantity within 1e-9 of an integer is a whole-share quantity (broker float
# noise like 5.000000001 must not flip the branch; real fractional quantities
# are >= 1e-6 away from an integer per Alpaca's 6-9dp quantity grid).
QTY_INTEGRAL_EPS = 1e-9

# Canonical asset-class vocabulary (crypto RFC 2026-07-10 §3.0/§3.2, D-C1
# execution slice). ``us_equity`` is the implicit legacy default everywhere;
# ``crypto`` is opted into EXPLICITLY (pair-form symbol or an explicit
# asset_class argument) so every equity code path stays byte-identical.
ASSET_CLASS_EQUITY = "us_equity"
ASSET_CLASS_CRYPTO = "crypto"

# Broker-result ``status`` values that mean "nothing was sent to the broker".
# A no-submit result is NOT an order rejection by the broker and NOT a pending
# order — it is an order the adapter deliberately did not submit (e.g. a
# fractional intent on a non-fractionable asset, or an asset lookup that failed
# closed). The execution audit must not count these as submitted, and live
# state-mutation planning must not treat them as pending fills.
NON_FRACTIONABLE_STATUS = "rejected_non_fractionable"
FRACTIONABLE_LOOKUP_FAILED_STATUS = "rejected_fractionable_lookup_failed"
PRECISION_EXCEEDS_9DP_STATUS = "rejected_precision_exceeds_9dp"
BELOW_MIN_NOTIONAL_STATUS = "rejected_below_min_notional"
INVALID_FRACTIONAL_ORDER_STATUS = "rejected_invalid_fractional_order"
# Crypto order-validation no-submit statuses (crypto RFC §3.2 E1/E2/E5/E6/E11).
# Only ever produced by crypto order paths — equity paths cannot emit them.
INVALID_CRYPTO_ORDER_STATUS = "rejected_invalid_crypto_order"
CRYPTO_NO_SHORT_STATUS = "rejected_crypto_no_short"
BELOW_MIN_ORDER_SIZE_STATUS = "rejected_below_min_order_size"
CRYPTO_SPEC_LOOKUP_FAILED_STATUS = "rejected_crypto_spec_lookup_failed"
NO_SUBMIT_STATUSES = frozenset({
    NON_FRACTIONABLE_STATUS,
    FRACTIONABLE_LOOKUP_FAILED_STATUS,
    PRECISION_EXCEEDS_9DP_STATUS,
    BELOW_MIN_NOTIONAL_STATUS,
    INVALID_FRACTIONAL_ORDER_STATUS,
    INVALID_CRYPTO_ORDER_STATUS,
    CRYPTO_NO_SHORT_STATUS,
    BELOW_MIN_ORDER_SIZE_STATUS,
    CRYPTO_SPEC_LOOKUP_FAILED_STATUS,
    # Legacy floor-to-zero status, kept recognized for back-compat audit replay.
    "skipped_non_fractionable_dust",
})

# Alpaca fractional-order rules, pinned by the S-FRAC v2 design inventory
# (renquant-orchestrator doc/design/2026-07-02-s-frac-fractional-v2.md §4,
# verified against Alpaca docs 2026-07-02):
#   * an order carries EITHER a fractional ``qty`` OR a dollar ``notional`` —
#     both set is an HTTP 400 at the broker;
#   * fractional orders support market / limit / stop / stop-limit types,
#     but ALL with time-in-force DAY only (no GTC on any fractional order);
#   * qty and notional accept up to 9 decimal places;
#   * minimum notional is $1.
FRACTIONAL_ORDER_TYPES = frozenset({"market", "limit", "stop", "stop_limit"})
FRACTIONAL_TIME_IN_FORCE = "day"
MAX_ORDER_DECIMAL_PLACES = 9
MIN_FRACTIONAL_NOTIONAL_USD = 1.0


def is_no_submit_status(status: Any) -> bool:
    """Whether ``status`` denotes a result that never reached the broker."""
    return str(status or "").strip().lower() in NO_SUBMIT_STATUSES


def is_whole_share(quantity: float) -> bool:
    """Whether ``quantity`` is a finite, whole-share (integral) amount.

    Epsilon-integral, consistent with the stage-0 commit contract's
    ``is_integral_qty`` (see ``QTY_INTEGRAL_EPS``): broker float noise within
    1e-9 of an integer is whole-share; a real fractional quantity is not.
    """
    value = float(quantity)
    return math.isfinite(value) and abs(value - round(value)) <= QTY_INTEGRAL_EPS


def is_fill_complete(filled_qty: float, requested_qty: float) -> bool:
    """Float requested-vs-filled comparison with the dust epsilon (design §4).

    Fractional partial fills accumulate as floats; a broker-reported cumulative
    ``filled_qty`` within ``QTY_INTEGRAL_EPS`` of ``requested_qty`` is a
    complete fill. Never compare fill floats with ``==``/``>=`` directly.
    """
    filled = float(filled_qty)
    requested = float(requested_qty)
    return (
        math.isfinite(filled)
        and math.isfinite(requested)
        and requested > QTY_INTEGRAL_EPS
        and filled >= requested - QTY_INTEGRAL_EPS
    )


def exceeds_9dp(value: float) -> bool:
    """Whether ``value`` cannot be represented on the broker's 9dp grid.

    Alpaca accepts at most 9 decimal places on ``qty``/``notional``; anything
    finer is an upstream precision violation and must be rejected (no-submit),
    never silently rounded — the adapter never mutates the intended order.
    """
    v = float(value)
    if not math.isfinite(v):
        return True
    return round(v, MAX_ORDER_DECIMAL_PLACES) != v


def validate_fractional_order(
    *,
    order_type: str,
    time_in_force: str,
    qty: float | None = None,
    notional: float | None = None,
) -> tuple[str, str] | None:
    """Validate a fractional (fractional-qty or notional) order intent.

    Encodes the pinned Alpaca fractional rules (see the constants above).
    Returns ``None`` when the order is submittable, else a
    ``(no_submit_status, reason)`` pair the adapter surfaces as an explicit
    no-submit result — a violation is never silently corrected.
    """
    if (qty is None) == (notional is None):
        return (
            INVALID_FRACTIONAL_ORDER_STATUS,
            "order must carry exactly one of qty | notional "
            "(both/neither is a broker HTTP 400)",
        )
    order_type_n = str(order_type or "").strip().lower()
    if order_type_n not in FRACTIONAL_ORDER_TYPES:
        return (
            INVALID_FRACTIONAL_ORDER_STATUS,
            f"fractional orders support {sorted(FRACTIONAL_ORDER_TYPES)} "
            f"order types only, got {order_type!r}",
        )
    tif_n = str(time_in_force or "").strip().lower()
    if tif_n != FRACTIONAL_TIME_IN_FORCE:
        return (
            INVALID_FRACTIONAL_ORDER_STATUS,
            f"fractional orders are TIF=DAY only (no GTC), got {time_in_force!r}",
        )
    value = qty if qty is not None else notional
    value_f = float(value)  # type: ignore[arg-type]
    label = "qty" if qty is not None else "notional"
    if not math.isfinite(value_f) or value_f <= 0.0:
        return (
            INVALID_FRACTIONAL_ORDER_STATUS,
            f"{label} must be finite and positive, got {value!r}",
        )
    if exceeds_9dp(value_f):
        return (
            PRECISION_EXCEEDS_9DP_STATUS,
            f"{label} {value_f!r} exceeds the broker's "
            f"{MAX_ORDER_DECIMAL_PLACES}dp grid",
        )
    if notional is not None and value_f < MIN_FRACTIONAL_NOTIONAL_USD:
        return (
            BELOW_MIN_NOTIONAL_STATUS,
            f"notional {value_f!r} is below the broker minimum "
            f"${MIN_FRACTIONAL_NOTIONAL_USD}",
        )
    return None


class BaseBroker(ABC):
    """Interface for order execution backends.

    This is migrated from the umbrella repo's live broker contract. Model,
    data, strategy, and backtesting repos must not implement broker mutation.
    """

    broker_name: str = "unknown"

    @abstractmethod
    def connect(self) -> None:
        """Open broker connection."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close broker connection."""

    @abstractmethod
    def get_position(self, symbol: str) -> float:
        """Return current share count for symbol."""

    @abstractmethod
    def get_account_value(self) -> float:
        """Return total account liquidation value."""

    def get_avg_cost(self, symbol: str) -> float:
        return 0.0

    def get_cash(self) -> float:
        return self.get_account_value()

    def get_account_id(self) -> str:
        """The REAL brokerage account identifier (e.g. Alpaca's
        ``account_number``) — never a sleeve/tag string. Required by the
        account-scoped cash ledger's shared-wiring contract (crypto RFC
        §5.3, D-C4): the ledger's identity is DERIVED from this, so a
        caller can never accidentally pass a per-sleeve tag in its place.
        Backends that don't yet participate in the shared-ledger contract
        fail loud rather than silently returning a placeholder a caller
        could mistake for a real account id."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement get_account_id() "
            "(required for the account-scoped cash ledger's shared-wiring "
            "contract)"
        )

    def get_all_positions(self) -> list[dict[str, Any]]:
        return []

    def get_filled_orders(
        self,
        after: str | None = None,
        asset_class: str | None = ASSET_CLASS_EQUITY,
    ) -> list[dict[str, Any]]:
        """Filled orders for reconciliation (crypto RFC §3.2 E3).

        ``asset_class`` defaults to ``us_equity`` so every existing caller is
        unchanged; the crypto sleeve asks EXPLICITLY (``asset_class="crypto"``,
        or ``None`` for every class) — crypto fills must never be silently
        invisible to reconcile-before-emit, and equity reconciliation must
        never silently start seeing crypto rows.
        """
        return []

    def get_open_orders(self, asset_class: str | None = ASSET_CLASS_EQUITY) -> set[str]:
        """Open-order symbols for reconciliation; same E3 contract as
        :meth:`get_filled_orders` (equity default, crypto explicit)."""
        return set()

    @abstractmethod
    def place_order(self, symbol: str, action: str, quantity: float) -> dict[str, Any]:
        """Place an order and return broker confirmation."""

    def place_notional_order(self, symbol: str, action: str, notional: float) -> dict[str, Any]:
        """Place a dollar-``notional`` (fractional, DAY) order.

        Notional orders are fractional by construction (the broker computes
        the quantity), so a backend without fractional support must not
        implement this. Default: fail loud, never a silent whole-share remap.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support notional (fractional) orders"
        )

    @staticmethod
    def is_no_submit_status(status: Any) -> bool:
        """Instance-callable no-submit classifier (S-FRAC stage-0 gate probe).

        The umbrella capability gate (RenQuant#439,
        ``adapters/commit_contract.py::fractional_capability_gate``) probes the
        broker object for a no-submit classifier (``classify_broker_result`` or
        ``is_no_submit_status``) before allowing fractional BUY emission.
        Delegates to the module-level vocabulary so every adapter answers the
        same way.
        """
        return is_no_submit_status(status)

    def supports_broker_side_stops(
        self, symbol: str | None = None, quantity: float | None = None
    ) -> bool:
        """Whether a broker-side protective stop can be installed.

        Optional ``symbol``/``quantity`` let the adapter answer per-position: a
        broker that cannot place a stop for a *fractional* holding must return
        ``False`` when given that fractional quantity so the caller routes the
        position to a software stop instead of opening unprotectable exposure.
        """
        return False

    def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__} does not support broker-side stop orders")

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError(f"{type(self).__name__} does not implement cancel_order")


def normalize_order_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Normalize a pipeline order intent into broker-facing fields."""
    symbol = intent.get("symbol") or intent.get("ticker")
    raw_action = intent.get("action")
    quantity = intent.get("quantity", intent.get("qty", intent.get("shares")))
    if not symbol:
        raise ValueError("order intent missing symbol/ticker")
    if raw_action is None or str(raw_action).strip() == "":
        raise ValueError("order intent missing action")
    action = str(raw_action).upper()
    if action not in {"BUY", "SELL"}:
        raise ValueError(f"order intent has unsupported action: {raw_action!r}")
    if quantity is None:
        raise ValueError("order intent missing quantity/qty/shares")
    quantity_f = float(quantity)
    if quantity_f <= 0:
        raise ValueError(f"order intent quantity must be positive: {quantity!r}")
    return {"symbol": str(symbol), "action": action, "quantity": quantity_f}
