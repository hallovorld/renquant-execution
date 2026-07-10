"""``AlpacaBrokerPort`` — the Alpaca REST adapter for the slice-1 ``BrokerPort``
Protocol (:mod:`renquant_execution.order_state_machine`).

Moved here from renquant-orchestrator (RFC #208 sprint D2, Stage-2 live
executor PR #291) per codex review: orchestrator's own operating instructions
say "do not implement broker adapters here" — this repo owns broker
execution, and ``BrokerPort``'s own docstring already says "Alpaca adapter
implements this later." This is that adapter.

NEVER constructed in tests that exercise orchestration logic — those inject a
fake port. This module's own tests exercise ``AlpacaBrokerPort`` directly
against an injected fake ``TradingClient``, with no network calls.

Fractional surface: BUY-side fractional validation
(``broker.validate_fractional_order``) and the explicit no-submit status
classification belong to the s-frac stage-1 surface (execution#22), not yet
on main. This adapter deliberately does NOT duplicate them; when #22 lands,
``submit_order`` is the integration seam (validate before the client call
and surface the s-frac no-submit result instead of raising, where that
contract requires it). Until then this adapter passes ``qty`` through
unvalidated — callers own share sizing (the Stage-2 canary binds risk with
its own daily entry-notional cap).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from .crypto import is_crypto_pair
from .order_state_machine import SIDE_BUY

#: Broker order statuses treated as an acknowledgment of a live order.
ACK_STATUSES = frozenset({"accepted", "new", "pending_new", "accepted_for_bidding"})
CANCELED_STATUSES = frozenset({"canceled", "cancelled", "done_for_day"})


class BrokerPortContractError(RuntimeError):
    """The adapter would violate its own submission contract (fail closed)."""


@dataclass
class AlpacaBrokerPort:
    """Slice-1 ``BrokerPort`` over the Alpaca trading REST API.

    - client-order-id == the slice-1 ``child_order_id`` (idempotency at the
      broker: Alpaca rejects a duplicate client_order_id);
    - DAY time-in-force always (RFC #208 §11b: no order carries overnight);
    - limit vs market per the caller-supplied order type (pre-declared by the
      caller's own authorization artifact, never per-order): entries default
      marketable-limit at a reference price ± ``limit_price_offset_bps``;
      exits default market (exits favor action);
    - reads (``open_orders`` / ``order_status``) are GET-only.

    NEVER constructed in orchestration tests — those use a fake port; this
    adapter is the only place a real submit can originate.
    """

    paper: bool = True
    entry_order_type: str = "limit"
    exit_order_type: str = "market"
    limit_price_offset_bps: float = 0.0
    _client: Any = None

    def _trading_client(self) -> Any:
        if self._client is None:
            from alpaca.trading.client import TradingClient  # noqa: PLC0415

            self._client = TradingClient(
                os.environ["ALPACA_API_KEY"],
                os.environ["ALPACA_SECRET_KEY"],
                paper=self.paper,
            )
        return self._client

    @staticmethod
    def _status_of(order: Any) -> str:
        status = getattr(order, "status", "")
        return str(getattr(status, "value", status)).lower()

    @staticmethod
    def _filled_of(order: Any) -> float:
        return float(getattr(order, "filled_qty", 0.0) or 0.0)

    def _limit_price(self, side: str, reference_price: float) -> float:
        offset = self.limit_price_offset_bps / 10_000.0
        price = (
            reference_price * (1.0 + offset)
            if side == SIDE_BUY
            else reference_price * (1.0 - offset)
        )
        # Alpaca sub-penny rule: >= $1 quotes to 2dp, sub-$1 to 4dp.
        return round(price, 2 if price >= 1.0 else 4)

    def submit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float | None = None,
    ) -> Mapping[str, Any]:
        from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
        from alpaca.trading.requests import (  # noqa: PLC0415
            LimitOrderRequest,
            MarketOrderRequest,
        )

        # Crypto guard (crypto RFC §3.2 E1): this port is the 105 equity
        # intraday path and pins TIF=DAY on every submit — a shape the broker
        # rejects for crypto (GTC/IOC only). Fail closed instead of silently
        # submitting a doomed DAY order; the crypto sleeve's port wiring is a
        # separate slice (D-C11).
        if is_crypto_pair(symbol):
            raise BrokerPortContractError(
                f"AlpacaBrokerPort is a TIF=DAY equity port; crypto pair "
                f"{symbol!r} is not supported here (crypto TIF is GTC/IOC "
                "only — use the crypto order paths on AlpacaBroker)"
            )

        side_u = str(side).upper()
        order_type = self.entry_order_type if side_u == SIDE_BUY else self.exit_order_type
        order_side = OrderSide.BUY if side_u == SIDE_BUY else OrderSide.SELL
        common: dict[str, Any] = {
            "symbol": str(symbol).upper(),
            "qty": qty,
            "side": order_side,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": client_order_id,
        }
        if order_type == "limit" and (limit_price is None or limit_price <= 0):
            if side_u == SIDE_BUY:
                # Entries fail closed without a limit reference (a missing
                # quote never silently becomes a market order).
                raise BrokerPortContractError(
                    f"limit entry for {symbol} has no positive reference "
                    f"price ({limit_price!r})"
                )
            order_type = "market"  # exits favor action over quote freshness
        if order_type == "limit":
            request: Any = LimitOrderRequest(
                limit_price=self._limit_price(side_u, float(limit_price)),
                **common,
            )
        else:
            request = MarketOrderRequest(**common)
        order = self._trading_client().submit_order(order_data=request)
        return {
            "status": self._status_of(order),
            "broker_order_id": str(getattr(order, "id", "")),
            "client_order_id": client_order_id,
            "filled_qty": self._filled_of(order),
        }

    def cancel_order(self, client_order_id: str) -> Mapping[str, Any]:
        client = self._trading_client()
        order = client.get_order_by_client_id(client_order_id)
        if self._status_of(order) not in (
            {"filled", "rejected", "expired"} | CANCELED_STATUSES
        ):
            client.cancel_order_by_id(getattr(order, "id"))
            order = client.get_order_by_client_id(client_order_id)
        return {"status": self._status_of(order), "filled_qty": self._filled_of(order)}

    def open_orders(self) -> Mapping[str, float]:
        from alpaca.trading.enums import QueryOrderStatus  # noqa: PLC0415
        from alpaca.trading.requests import GetOrdersRequest  # noqa: PLC0415

        orders = self._trading_client().get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        )
        result: dict[str, float] = {}
        for order in orders:
            cid = str(getattr(order, "client_order_id", "") or "")
            if not cid:
                continue
            requested = float(getattr(order, "qty", 0.0) or 0.0)
            result[cid] = requested - self._filled_of(order)
        return result

    def order_status(self, client_order_id: str) -> Mapping[str, Any]:
        order = self._trading_client().get_order_by_client_id(client_order_id)
        return {"status": self._status_of(order), "filled_qty": self._filled_of(order)}


__all__ = ["AlpacaBrokerPort", "BrokerPortContractError"]
