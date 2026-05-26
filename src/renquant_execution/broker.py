"""Broker contracts owned by renquant-execution."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


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

    def supports_broker_side_stops(self) -> bool:
        return False

    def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__} does not support broker-side stop orders")

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError(f"{type(self).__name__} does not implement cancel_order")


def normalize_order_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Normalize a pipeline order intent into broker-facing fields."""
    symbol = intent.get("symbol") or intent.get("ticker")
    action = str(intent.get("action", "")).upper()
    quantity = intent.get("quantity", intent.get("qty"))
    if not symbol:
        raise ValueError("order intent missing symbol/ticker")
    if action not in {"BUY", "SELL"}:
        raise ValueError(f"order intent has unsupported action: {intent.get('action')!r}")
    if quantity is None:
        raise ValueError("order intent missing quantity/qty")
    quantity_f = float(quantity)
    if quantity_f <= 0:
        raise ValueError(f"order intent quantity must be positive: {quantity!r}")
    return {"symbol": str(symbol), "action": action, "quantity": quantity_f}

