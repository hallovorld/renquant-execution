"""Broker contracts owned by renquant-execution."""
from __future__ import annotations

from abc import ABC, abstractmethod
import math
from typing import Any

# Broker-result ``status`` values that mean "nothing was sent to the broker".
# A no-submit result is NOT an order rejection by the broker and NOT a pending
# order — it is an order the adapter deliberately did not submit (e.g. a
# fractional intent on a non-fractionable asset, or an asset lookup that failed
# closed). The execution audit must not count these as submitted, and live
# state-mutation planning must not treat them as pending fills.
NON_FRACTIONABLE_STATUS = "rejected_non_fractionable"
FRACTIONABLE_LOOKUP_FAILED_STATUS = "rejected_fractionable_lookup_failed"
NO_SUBMIT_STATUSES = frozenset({
    NON_FRACTIONABLE_STATUS,
    FRACTIONABLE_LOOKUP_FAILED_STATUS,
    # Legacy floor-to-zero status, kept recognized for back-compat audit replay.
    "skipped_non_fractionable_dust",
})


def is_no_submit_status(status: Any) -> bool:
    """Whether ``status`` denotes a result that never reached the broker."""
    return str(status or "").strip().lower() in NO_SUBMIT_STATUSES


def is_whole_share(quantity: float) -> bool:
    """Whether ``quantity`` is a finite, whole-share (integral) amount."""
    value = float(quantity)
    return math.isfinite(value) and value.is_integer()


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

    def get_all_positions(self) -> list[dict[str, Any]]:
        return []

    def get_filled_orders(self, after: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_open_orders(self) -> set[str]:
        return set()

    @abstractmethod
    def place_order(self, symbol: str, action: str, quantity: float) -> dict[str, Any]:
        """Place an order and return broker confirmation."""

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
