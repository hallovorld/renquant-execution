"""Paper broker for deterministic execution tests and dry local runs."""
from __future__ import annotations

import math
from typing import Any

from .broker import BaseBroker


class PaperBroker(BaseBroker):
    """Small no-network broker that tracks cash and long positions."""

    broker_name = "paper"

    def __init__(self, initial_cash: float = 100_000.0) -> None:
        self._initial_cash = float(initial_cash)
        self._cash = float(initial_cash)
        self._positions: dict[str, float] = {}
        self._avg_cost: dict[str, float] = {}
        self._last_price: dict[str, float] = {}
        self._order_counter = 0
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def set_price(self, symbol: str, price: float) -> None:
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"price must be finite and positive: {price!r}")
        self._last_price[symbol] = float(price)

    def get_position(self, symbol: str) -> float:
        return self._positions.get(symbol, 0.0)

    def get_account_value(self) -> float:
        total = self._cash
        for symbol, qty in self._positions.items():
            if qty <= 0:
                continue
            total += qty * self._last_price.get(symbol, self._avg_cost.get(symbol, 0.0))
        return total

    def get_cash(self) -> float:
        return self._cash

    def get_avg_cost(self, symbol: str) -> float:
        return self._avg_cost.get(symbol, 0.0)

    def get_all_positions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for symbol, qty in self._positions.items():
            if qty <= 0:
                continue
            cost = self._avg_cost.get(symbol, 0.0)
            price = self._last_price.get(symbol, cost)
            rows.append({
                "symbol": symbol,
                "qty": qty,
                "avg_entry_price": cost,
                "market_value": qty * price,
                "unrealized_pl": qty * (price - cost),
            })
        return rows

    def place_order(self, symbol: str, action: str, quantity: float) -> dict[str, Any]:
        if not self.connected:
            raise RuntimeError("PaperBroker is not connected")
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError(f"quantity must be finite and positive: {quantity!r}")
        action_u = action.upper()
        if action_u not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported action: {action!r}")
        price = self._last_price.get(symbol)
        if price is None:
            raise ValueError(f"no last price set for {symbol}")

        self._order_counter += 1
        order_id = f"PAPER-{self._order_counter:04d}"
        notional = float(quantity) * price
        if action_u == "BUY":
            if notional > self._cash + 1e-6:
                return {
                    "order_id": order_id,
                    "status": "rejected",
                    "symbol": symbol,
                    "action": action_u,
                    "quantity": 0.0,
                    "price": price,
                    "reject_reason": "insufficient cash",
                }
            old_qty = self._positions.get(symbol, 0.0)
            old_cost = self._avg_cost.get(symbol, 0.0)
            new_qty = old_qty + quantity
            self._avg_cost[symbol] = (old_cost * old_qty + price * quantity) / new_qty
            self._positions[symbol] = new_qty
            self._cash -= notional
        else:
            held = self._positions.get(symbol, 0.0)
            sell_qty = min(quantity, held)
            self._positions[symbol] = max(0.0, held - sell_qty)
            if self._positions[symbol] == 0:
                self._avg_cost.pop(symbol, None)
            self._cash += sell_qty * price
            quantity = sell_qty

        return {
            "order_id": order_id,
            "status": "filled",
            "symbol": symbol,
            "action": action_u,
            "quantity": float(quantity),
            "price": price,
        }

