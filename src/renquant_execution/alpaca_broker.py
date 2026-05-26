"""Alpaca broker adapter.

The alpaca-py import is intentionally lazy so paper tests and shadow
orchestration can import renquant-execution without broker SDK credentials.
"""
from __future__ import annotations

import os
import warnings
from datetime import datetime, timezone
from typing import Any

from .broker import BaseBroker


class AlpacaBroker(BaseBroker):
    """Broker adapter for Alpaca paper and live accounts."""

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool = True,
        env_prefix: str = "ALPACA",
        label: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = bool(paper)
        self.env_prefix = env_prefix
        self.label = label
        self._trading_client: Any | None = None
        self._account: Any | None = None

    @property
    def broker_name(self) -> str:
        if self.label:
            return self.label
        return "alpaca-paper" if self.paper else "alpaca"

    def connect(self) -> None:
        from alpaca.trading.client import TradingClient

        api_key = self.api_key or os.environ.get(f"{self.env_prefix}_API_KEY")
        secret_key = self.secret_key or os.environ.get(f"{self.env_prefix}_SECRET_KEY")
        if not api_key or not secret_key:
            raise ValueError(
                f"Missing {self.env_prefix}_API_KEY/{self.env_prefix}_SECRET_KEY credentials"
            )

        self._trading_client = TradingClient(api_key, secret_key, paper=self.paper)
        self._account = self._trading_client.get_account()

        if not self.paper:
            expected_account = os.environ.get("RENQUANT_EXPECTED_LIVE_ACCOUNT")
            if not expected_account:
                raise RuntimeError(
                    "RENQUANT_EXPECTED_LIVE_ACCOUNT must be set before live Alpaca execution"
                )
            actual_account = str(getattr(self._account, "account_number", ""))
            if actual_account != expected_account:
                raise RuntimeError(
                    f"Live Alpaca account mismatch: expected {expected_account}, got {actual_account}"
                )

        status = str(getattr(self._account, "status", "")).upper()
        if status and status != "ACTIVE":
            warnings.warn(f"Alpaca account status is {status}", RuntimeWarning, stacklevel=2)

    def disconnect(self) -> None:
        self._trading_client = None
        self._account = None

    def get_position(self, symbol: str) -> float:
        client = self._require_client()
        try:
            position = client.get_open_position(symbol)
        except Exception as exc:
            if _is_not_found_error(exc):
                return 0.0
            raise
        return float(getattr(position, "qty", 0.0))

    def get_account_value(self) -> float:
        account = self._refresh_account()
        return float(getattr(account, "portfolio_value", 0.0))

    def get_cash(self) -> float:
        account = self._refresh_account()
        buying_power = getattr(account, "non_marginable_buying_power", None)
        if buying_power is not None:
            return float(buying_power)
        return float(getattr(account, "cash", 0.0))

    def get_avg_cost(self, symbol: str) -> float:
        client = self._require_client()
        try:
            position = client.get_open_position(symbol)
        except Exception as exc:
            if _is_not_found_error(exc):
                return 0.0
            raise
        return float(getattr(position, "avg_entry_price", 0.0))

    def get_all_positions(self) -> list[dict[str, Any]]:
        positions = self._require_client().get_all_positions()
        rows: list[dict[str, Any]] = []
        for position in positions:
            rows.append({
                "symbol": str(getattr(position, "symbol", "")),
                "qty": float(getattr(position, "qty", 0.0)),
                "qty_available": float(
                    getattr(position, "qty_available", getattr(position, "qty", 0.0))
                ),
                "market_value": float(getattr(position, "market_value", 0.0)),
                "avg_entry_price": float(getattr(position, "avg_entry_price", 0.0)),
                "unrealized_pl": float(getattr(position, "unrealized_pl", 0.0)),
            })
        return rows

    def get_filled_orders(self, after: str | None = None) -> list[dict[str, Any]]:
        from alpaca.trading.enums import AssetClass, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            asset_class=AssetClass.US_EQUITY,
            limit=500,
            after=_parse_datetime(after) if after else None,
        )
        rows: list[dict[str, Any]] = []
        for order in self._require_client().get_orders(filter=request):
            if str(getattr(order, "status", "")).lower() in {"filled", "partially_filled"}:
                rows.append(_order_to_dict(order))
        return rows

    def get_open_orders(self) -> set[str]:
        from alpaca.trading.enums import AssetClass, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            asset_class=AssetClass.US_EQUITY,
            limit=500,
        )
        return {
            str(getattr(order, "symbol", "")).upper()
            for order in self._require_client().get_orders(filter=request)
            if getattr(order, "symbol", None)
        }

    def place_order(self, symbol: str, action: str, quantity: float) -> dict[str, Any]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        self._assert_account_active()
        action_u = action.upper()
        if action_u not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported Alpaca action: {action!r}")
        request = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.BUY if action_u == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self._require_client().submit_order(order_data=request)
        result = _order_to_dict(order)
        result.update({"action": action_u, "quantity": float(quantity)})
        return result

    def supports_broker_side_stops(self) -> bool:
        return True

    def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> dict[str, Any]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        self._assert_account_active()
        request = StopOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price,
        )
        order = self._require_client().submit_order(order_data=request)
        result = _order_to_dict(order)
        result.update({
            "action": "SELL",
            "quantity": float(quantity),
            "stop_price": float(stop_price),
        })
        return result

    def cancel_order(self, order_id: str) -> bool:
        self._require_client().cancel_order_by_id(order_id)
        return True

    def is_market_open(self) -> bool:
        return bool(getattr(self._require_client().get_clock(), "is_open", False))

    def _refresh_account(self) -> Any:
        self._account = self._require_client().get_account()
        return self._account

    def _assert_account_active(self) -> None:
        account = self._refresh_account()
        status = str(getattr(account, "status", "")).upper()
        if status and status != "ACTIVE":
            raise RuntimeError(f"Alpaca account is not active: {status}")

    def _require_client(self) -> Any:
        if self._trading_client is None:
            raise RuntimeError("AlpacaBroker is not connected")
        return self._trading_client


def _is_not_found_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "position does not exist" in text or "not found" in text or "404" in text


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _order_to_dict(order: Any) -> dict[str, Any]:
    return {
        "order_id": str(getattr(order, "id", "")),
        "status": str(getattr(order, "status", "")),
        "symbol": str(getattr(order, "symbol", "")),
        "filled_qty": float(getattr(order, "filled_qty", 0.0) or 0.0),
        "filled_avg_price": float(getattr(order, "filled_avg_price", 0.0) or 0.0),
        "created_at": str(getattr(order, "created_at", "")),
        "submitted_at": str(getattr(order, "submitted_at", "")),
        "filled_at": str(getattr(order, "filled_at", "")),
    }
