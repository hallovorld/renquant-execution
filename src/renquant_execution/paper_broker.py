"""Paper broker for deterministic execution tests and dry local runs."""
from __future__ import annotations

import math
from typing import Any

from .broker import ASSET_CLASS_CRYPTO, BaseBroker
from .crypto import CryptoFeeSchedule, is_crypto_pair


class PaperBroker(BaseBroker):
    """Small no-network broker that tracks cash and long positions.

    Crypto fee awareness (crypto RFC §3.2 E4): when constructed with a
    ``crypto_fee_schedule``, fills on crypto pairs (pair-form symbols) net
    the taker fee — a BUY must afford ``notional + fee`` and debits both; a
    SELL credits ``notional - fee`` — so paper P&L stops overstating a
    fee-bearing asset class. Equity fills are byte-identical with or without
    a schedule (zero-fee, result shape unchanged); with no schedule (the
    default) crypto fills are zero-fee too, so every existing caller is
    unchanged.
    """

    broker_name = "paper"

    def __init__(
        self,
        initial_cash: float = 100_000.0,
        *,
        crypto_fee_schedule: CryptoFeeSchedule | None = None,
    ) -> None:
        self._initial_cash = float(initial_cash)
        self._cash = float(initial_cash)
        self._positions: dict[str, float] = {}
        self._avg_cost: dict[str, float] = {}
        self._last_price: dict[str, float] = {}
        self._order_counter = 0
        self._crypto_fee_schedule = crypto_fee_schedule
        self.connected = False

    def _paper_fill_fee(self, symbol: str, notional: float) -> float:
        """Taker fee for a paper fill: crypto pairs only, else exactly 0.0."""
        if self._crypto_fee_schedule is None or not is_crypto_pair(symbol):
            return 0.0
        return self._crypto_fee_schedule.fee_usd(notional, liquidity="taker")

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
        fee = self._paper_fill_fee(symbol, notional)
        if action_u == "BUY":
            # Fee-aware affordability (E4): the budget must cover fill + fee.
            # For equities (and crypto with no schedule) fee == 0.0 and this
            # is the historical check byte-for-byte.
            if notional + fee > self._cash + 1e-6:
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
            self._cash -= notional + fee
        else:
            held = self._positions.get(symbol, 0.0)
            sell_qty = min(quantity, held)
            self._positions[symbol] = max(0.0, held - sell_qty)
            if self._positions[symbol] == 0:
                self._avg_cost.pop(symbol, None)
            fee = self._paper_fill_fee(symbol, sell_qty * price)
            self._cash += sell_qty * price - fee
            quantity = sell_qty

        result = {
            "order_id": order_id,
            "status": "filled",
            "symbol": symbol,
            "action": action_u,
            "quantity": float(quantity),
            "price": price,
        }
        # Crypto fills surface the netted fee; equity results keep the exact
        # historical shape (no new keys — byte-identity pin).
        if is_crypto_pair(symbol):
            result["fee"] = float(fee)
            result["asset_class"] = ASSET_CLASS_CRYPTO
        return result

