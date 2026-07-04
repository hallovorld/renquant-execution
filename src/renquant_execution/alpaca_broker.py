"""Alpaca broker adapter.

The alpaca-py import is intentionally lazy so paper tests and shadow
orchestration can import renquant-execution without broker SDK credentials.
"""
from __future__ import annotations

import os
import warnings
from datetime import datetime, timezone
from typing import Any

from .broker import (
    FRACTIONABLE_LOOKUP_FAILED_STATUS,
    FRACTIONAL_TIME_IN_FORCE,
    NON_FRACTIONABLE_STATUS,
    BaseBroker,
    is_whole_share,
    validate_fractional_order,
)


class _FractionableLookupError(RuntimeError):
    """Raised when an Alpaca ``get_asset`` fractionability lookup fails.

    Distinct from a *confirmed* non-fractionable verdict so the caller can fail
    closed (and retry later) instead of caching a transient failure forever.
    """


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
        # Cache of symbol -> fractionable (Alpaca asset attribute). Avoids a
        # get_asset round-trip per order; assets' fractionability is stable.
        self._fractionable_cache: dict[str, bool] = {}

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

        requested_qty = float(quantity)

        # Fractional-share safety guard (renquant-pipeline #35 cash-drag
        # follow-up). Alpaca accepts a FRACTIONAL `qty` ONLY for assets flagged
        # `fractionable=True`, and only on MARKET orders with DAY time-in-force
        # in the regular session — which this method already uses
        # (MarketOrderRequest + TimeInForce.DAY).
        #
        # Whole-share quantities are always broker-valid and pass through with no
        # asset lookup. A fractional intent requires a confirmed fractionable
        # asset; otherwise we FAIL CLOSED with an explicit no-submit result that
        # preserves the requested-vs-submitted quantity. We never silently floor
        # a fractional intent (that would drop residual exposure on a SELL and
        # mutate a BUY), and we never cache a transient lookup failure as an
        # authoritative non-fractionable verdict.
        if is_whole_share(requested_qty):
            # Snap eps-integral broker float noise (e.g. 3.0000000001) to the
            # exact integer before submission: Alpaca would read the raw float
            # as a >9dp fractional qty and reject it. Same ONE sanctioned
            # whole-share branch as stage-0's ``normalize_fill_qty``.
            submit_qty = float(round(requested_qty))
        else:
            # Rule preflight (design §4 pins): this path builds a MARKET + DAY
            # order, so type/TIF always satisfy the fractional rules — the
            # live check here is the 9dp grid. A violation is an explicit
            # no-submit, never a silent round.
            violation = validate_fractional_order(
                order_type="market",
                time_in_force=FRACTIONAL_TIME_IN_FORCE,
                qty=requested_qty,
            )
            if violation is not None:
                status, why = violation
                return self._no_submit_result(
                    symbol,
                    action_u,
                    requested_qty,
                    status=status,
                    reason=(
                        f"fractional {action_u} qty {requested_qty!r} on "
                        f"{symbol} rejected at preflight: {why}"
                    ),
                )
            try:
                fractionable = self._lookup_fractionable(symbol)
            except _FractionableLookupError as exc:
                return self._no_submit_result(
                    symbol,
                    action_u,
                    requested_qty,
                    status=FRACTIONABLE_LOOKUP_FAILED_STATUS,
                    reason=(
                        f"Alpaca get_asset({symbol!r}) failed ({exc}); failing "
                        f"closed on fractional {action_u} qty {requested_qty} "
                        "(no submit, not cached — will retry)"
                    ),
                )
            if not fractionable:
                return self._no_submit_result(
                    symbol,
                    action_u,
                    requested_qty,
                    status=NON_FRACTIONABLE_STATUS,
                    reason=(
                        f"{symbol} is not fractionable; fractional {action_u} "
                        f"qty {requested_qty} rejected (not floored)"
                    ),
                )
            submit_qty = requested_qty

        request = MarketOrderRequest(
            symbol=symbol,
            qty=submit_qty,
            side=OrderSide.BUY if action_u == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self._require_client().submit_order(order_data=request)
        result = _order_to_dict(order)
        result.update({
            "action": action_u,
            "quantity": float(submit_qty),
            "requested_quantity": requested_qty,
            "skipped": False,
        })
        return result

    def place_notional_order(self, symbol: str, action: str, notional: float) -> dict[str, Any]:
        """Place a dollar-``notional`` market DAY order (fractional by construction).

        S-FRAC stage 1 (design §4): an Alpaca order carries EITHER ``qty`` OR
        ``notional`` — this is the notional shape, kept for the sliver-sweep
        use case (design §9.4). The broker computes the executed quantity, so
        the confirmation's ``qty``/``filled_qty`` are broker-authoritative and
        ``requested_notional`` records the intent. Same fail-closed discipline
        as ``place_order``: rule preflight (9dp grid, $1 minimum, DAY-only) and
        a confirmed-fractionable asset, else an explicit no-submit result.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        self._assert_account_active()
        action_u = action.upper()
        if action_u not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported Alpaca action: {action!r}")

        requested_notional = float(notional)
        violation = validate_fractional_order(
            order_type="market",
            time_in_force=FRACTIONAL_TIME_IN_FORCE,
            notional=requested_notional,
        )
        if violation is not None:
            status, why = violation
            return self._no_submit_result(
                symbol,
                action_u,
                0.0,
                requested_notional=requested_notional,
                status=status,
                reason=(
                    f"notional {action_u} ${requested_notional!r} on {symbol} "
                    f"rejected at preflight: {why}"
                ),
            )
        try:
            fractionable = self._lookup_fractionable(symbol)
        except _FractionableLookupError as exc:
            return self._no_submit_result(
                symbol,
                action_u,
                0.0,
                requested_notional=requested_notional,
                status=FRACTIONABLE_LOOKUP_FAILED_STATUS,
                reason=(
                    f"Alpaca get_asset({symbol!r}) failed ({exc}); failing "
                    f"closed on notional {action_u} ${requested_notional} "
                    "(no submit, not cached — will retry)"
                ),
            )
        if not fractionable:
            return self._no_submit_result(
                symbol,
                action_u,
                0.0,
                requested_notional=requested_notional,
                status=NON_FRACTIONABLE_STATUS,
                reason=(
                    f"{symbol} is not fractionable; notional {action_u} "
                    f"${requested_notional} rejected (notional orders are "
                    "fractional by construction)"
                ),
            )

        request = MarketOrderRequest(
            symbol=symbol,
            notional=requested_notional,
            side=OrderSide.BUY if action_u == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self._require_client().submit_order(order_data=request)
        result = _order_to_dict(order)
        result.update({
            "action": action_u,
            "requested_notional": requested_notional,
            "notional": requested_notional,
            "skipped": False,
        })
        return result

    def _no_submit_result(
        self,
        symbol: str,
        action: str,
        requested_qty: float,
        *,
        status: str,
        reason: str,
        requested_notional: float | None = None,
    ) -> dict[str, Any]:
        """Build an explicit no-submit result that preserves order intent.

        ``quantity`` is the *submitted* qty (0.0 — nothing was sent) while
        ``requested_quantity`` (and, for notional orders,
        ``requested_notional``) records what the pipeline asked for, so the
        audit can show the dropped intent instead of a silently mutated order.
        """
        warnings.warn(reason, RuntimeWarning, stacklevel=3)
        result = {
            "order_id": "",
            "status": status,
            "symbol": symbol,
            "side": action,
            "action": action,
            "quantity": 0.0,
            "qty": 0.0,
            "requested_quantity": float(requested_qty),
            "filled_qty": 0.0,
            "filled_avg_price": 0.0,
            "avg_price": 0.0,
            "partial": False,
            "skipped": True,
            "reason": reason,
            "created_at": "",
            "submitted_at": "",
            "filled_at": "",
        }
        if requested_notional is not None:
            result["requested_notional"] = float(requested_notional)
            result["notional"] = 0.0
        return result

    def _lookup_fractionable(self, symbol: str) -> bool:
        """Return whether ``symbol`` is fractionable, caching only confirmed
        lookups. Raises ``_FractionableLookupError`` on lookup failure so a
        transient error is never cached as an authoritative verdict."""
        key = str(symbol).upper()
        cached = self._fractionable_cache.get(key)
        if cached is not None:
            return cached
        try:
            asset = self._require_client().get_asset(symbol)
        except Exception as exc:  # noqa: BLE001 — surface as a fail-closed signal
            raise _FractionableLookupError(repr(exc)) from exc
        fractionable = bool(getattr(asset, "fractionable", False))
        self._fractionable_cache[key] = fractionable
        return fractionable

    def is_fractionable(self, symbol: str) -> bool:
        """Whether ``symbol`` supports fractional Alpaca orders (cached).

        Returns ``False`` on lookup failure (safe default) but, unlike a
        confirmed lookup, does NOT cache that failure — so a later call retries
        rather than treating a transient error as a permanent verdict. Callers
        that must distinguish "confirmed non-fractionable" from "lookup failed"
        (e.g. ``place_order``) use ``_lookup_fractionable`` directly.
        """
        try:
            return self._lookup_fractionable(symbol)
        except _FractionableLookupError:
            return False

    def supports_broker_side_stops(
        self, symbol: str | None = None, quantity: float | None = None
    ) -> bool:
        """Alpaca supports broker-side stops only for WHOLE-share quantities.

        A protective ``StopOrderRequest`` (GTC) is rejected for a fractional
        position, so when asked about a fractional ``quantity`` we return
        ``False`` — the caller must protect that position with a software stop
        rather than open a fractional holding whose broker-side stop will fail.
        With no quantity (legacy callers) we report the whole-share capability.
        """
        if quantity is not None and not is_whole_share(float(quantity)):
            return False
        return True

    def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> dict[str, Any]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        self._assert_account_active()
        # Fail closed: Alpaca rejects a fractional broker-side stop. Refuse it
        # here (preflight) instead of submitting an order the broker will bounce
        # after the position is already open. Fractional positions must use a
        # software stop (see supports_broker_side_stops).
        if not is_whole_share(float(quantity)):
            raise ValueError(
                f"Alpaca broker-side stop orders require a whole-share quantity; "
                f"{symbol} qty={quantity} is fractional — route to a software stop"
            )
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
    side = str(getattr(order, "side", "") or "").upper()
    quantity = float(getattr(order, "qty", getattr(order, "quantity", 0.0)) or 0.0)
    filled_qty = float(getattr(order, "filled_qty", 0.0) or 0.0)
    filled_avg_price = float(getattr(order, "filled_avg_price", 0.0) or 0.0)
    return {
        "order_id": str(getattr(order, "id", "")),
        "status": str(getattr(order, "status", "")),
        "symbol": str(getattr(order, "symbol", "")),
        "side": side,
        "action": side,
        "quantity": quantity,
        "qty": quantity,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "avg_price": filled_avg_price,
        "partial": 0.0 < filled_qty < quantity,
        "created_at": str(getattr(order, "created_at", "")),
        "submitted_at": str(getattr(order, "submitted_at", "")),
        "filled_at": str(getattr(order, "filled_at", "")),
    }
