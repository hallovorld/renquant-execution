"""Read-only broker wrapper for shadow/live decision rehearsal."""
from __future__ import annotations

import time
import uuid
from typing import Any

from .broker import BaseBroker


class ReadOnlyBrokerWrapper(BaseBroker):
    """Forward account reads while converting all mutations into shadow events."""

    broker_name = "alpaca_shadow"

    def __init__(self, underlying: BaseBroker) -> None:
        self.underlying = underlying

    def connect(self) -> None:
        self.underlying.connect()

    def disconnect(self) -> None:
        self.underlying.disconnect()

    def get_position(self, symbol: str) -> float:
        return self.underlying.get_position(symbol)

    def get_account_value(self) -> float:
        return self.underlying.get_account_value()

    def get_avg_cost(self, symbol: str) -> float:
        return self.underlying.get_avg_cost(symbol)

    def get_cash(self) -> float:
        return self.underlying.get_cash()

    def get_all_positions(self) -> list[dict[str, Any]]:
        return self.underlying.get_all_positions()

    def get_filled_orders(self, after: str | None = None) -> list[dict[str, Any]]:
        return self.underlying.get_filled_orders(after=after)

    def get_open_orders(self) -> set[str]:
        return self.underlying.get_open_orders()

    def supports_broker_side_stops(
        self, symbol: str | None = None, quantity: float | None = None
    ) -> bool:
        return self.underlying.supports_broker_side_stops(symbol, quantity)

    def place_order(self, symbol: str, action: str, quantity: float) -> dict[str, Any]:
        return {
            "order_id": f"SHADOW-{uuid.uuid4().hex[:12].upper()}",
            "status": "shadow_ack",
            "symbol": symbol,
            "action": action.upper(),
            "quantity": float(quantity),
            "shadow": True,
            "timestamp": time.time(),
        }

    def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> dict[str, Any]:
        return {
            "order_id": f"SHADOW-STOP-{uuid.uuid4().hex[:12].upper()}",
            "status": "shadow_ack",
            "symbol": symbol,
            "action": "SELL",
            "quantity": float(quantity),
            "stop_price": float(stop_price),
            "shadow": True,
            "timestamp": time.time(),
        }

    def cancel_order(self, order_id: str) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.underlying, name)
