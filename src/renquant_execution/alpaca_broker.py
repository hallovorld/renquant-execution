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
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_EQUITY,
    BELOW_MIN_ORDER_SIZE_STATUS,
    CRYPTO_NO_SHORT_STATUS,
    CRYPTO_SPEC_LOOKUP_FAILED_STATUS,
    FRACTIONABLE_LOOKUP_FAILED_STATUS,
    FRACTIONAL_TIME_IN_FORCE,
    NON_FRACTIONABLE_STATUS,
    QTY_INTEGRAL_EPS,
    BaseBroker,
    is_whole_share,
    validate_fractional_order,
)
from .crypto import (
    CRYPTO_MARKET_DEFAULT_TIF,
    CRYPTO_STOP_LIMIT_TIF,
    CryptoAssetSpec,
    classify_asset_class,
    crypto_no_short_violation,
    is_crypto_pair,
    round_price_to_increment,
    snap_qty_to_increment,
    validate_crypto_order,
)


class _FractionableLookupError(RuntimeError):
    """Raised when an Alpaca ``get_asset`` fractionability lookup fails.

    Distinct from a *confirmed* non-fractionable verdict so the caller can fail
    closed (and retry later) instead of caching a transient failure forever.
    """


class _CryptoSpecLookupError(RuntimeError):
    """Raised when an Alpaca ``get_asset`` crypto order-grid lookup fails.

    Same fail-closed discipline as ``_FractionableLookupError``: a transient
    lookup failure is never cached as an authoritative spec, and the order is
    refused (no-submit) rather than submitted on a guessed grid.
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
        crypto_asset_specs: dict[str, CryptoAssetSpec] | None = None,
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
        # Pinned per-pair crypto order grids (crypto RFC §3.1: the strategy
        # repo snapshots min_order_size/min_trade_increment/price_increment
        # per pair — auditable, never dynamic at runtime). When a pair has no
        # pinned spec the adapter falls back to a fail-closed get_asset
        # lookup (SDK Asset fields), cached only on confirmed success.
        self._crypto_asset_specs: dict[str, CryptoAssetSpec] = {
            str(key).upper(): spec
            for key, spec in (crypto_asset_specs or {}).items()
        }
        self._crypto_spec_cache: dict[str, CryptoAssetSpec] = {}

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

    def get_account_id(self) -> str:
        """Alpaca's real ``account_number`` (the same field ``connect()``
        already verifies against ``RENQUANT_EXPECTED_LIVE_ACCOUNT`` in live
        mode) — the shared cash ledger's identity is derived from THIS,
        never a sleeve tag."""
        self._require_client()
        account_id = str(getattr(self._account, "account_number", "") or "")
        if not account_id:
            raise RuntimeError(
                "AlpacaBroker has no account_number on its connected account "
                "(cannot derive the shared cash ledger's identity)"
            )
        return account_id

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

    @staticmethod
    def _normalize_asset_class_filter(asset_class: str | None) -> str | None:
        """Validate the E3 ``asset_class`` parameter (``us_equity`` /
        ``crypto`` / ``None`` for every class); anything else is a wiring
        error and fails loud."""
        if asset_class is None:
            return None
        normalized = str(asset_class).strip().lower()
        if normalized in {ASSET_CLASS_EQUITY, ASSET_CLASS_CRYPTO}:
            return normalized
        raise ValueError(f"unsupported asset_class filter: {asset_class!r}")

    @staticmethod
    def _order_matches_asset_class(order: Any, asset_class: str | None) -> bool:
        """Client-side E3 filter on the returned ``Order.asset_class``.

        [VERIFIED alpaca-py 0.43.4] ``GetOrdersRequest`` has NO
        ``asset_class`` field (``trading/requests.py:198-219``) — the
        pydantic model silently DROPS the kwarg the legacy code passed, so
        the historical "US_EQUITY filter" never reached the API at all. The
        real filter must therefore run client-side against the returned
        ``Order.asset_class`` ([VERIFIED] ``trading/models.py:188``;
        optional — omitted for mleg orders, so an absent value falls back to
        the pair-form symbol classifier).
        """
        if asset_class is None:
            return True
        raw = getattr(order, "asset_class", None)
        value = str(getattr(raw, "value", raw) or "").strip().lower()
        if not value:
            value = classify_asset_class(getattr(order, "symbol", ""))
        return value == asset_class

    def get_filled_orders(
        self,
        after: str | None = None,
        asset_class: str | None = ASSET_CLASS_EQUITY,
    ) -> list[dict[str, Any]]:
        """Filled orders for reconciliation (crypto RFC §3.2 E3).

        Crypto fills must never be silently invisible to
        reconcile-before-emit, and equity reconciliation must never silently
        start seeing crypto rows. The default stays equity-only (existing
        callers unchanged); the crypto sleeve passes ``asset_class="crypto"``
        explicitly, or ``None`` to see every class. Filtering is client-side
        on ``Order.asset_class`` — see :meth:`_order_matches_asset_class`
        for why the request-level filter does not exist.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        wanted = self._normalize_asset_class_filter(asset_class)
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=500,
            after=_parse_datetime(after) if after else None,
        )
        rows: list[dict[str, Any]] = []
        for order in self._require_client().get_orders(filter=request):
            if not self._order_matches_asset_class(order, wanted):
                continue
            if str(getattr(order, "status", "")).lower() in {"filled", "partially_filled"}:
                rows.append(_order_to_dict(order))
        return rows

    def get_open_orders(self, asset_class: str | None = ASSET_CLASS_EQUITY) -> set[str]:
        """Open-order symbols; same E3 contract as :meth:`get_filled_orders`."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        wanted = self._normalize_asset_class_filter(asset_class)
        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            limit=500,
        )
        return {
            str(getattr(order, "symbol", "")).upper()
            for order in self._require_client().get_orders(filter=request)
            if getattr(order, "symbol", None)
            and self._order_matches_asset_class(order, wanted)
        }

    def place_order(
        self,
        symbol: str,
        action: str,
        quantity: float,
        *,
        time_in_force: str | None = None,
        asset_class: str | None = None,
    ) -> dict[str, Any]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        self._assert_account_active()
        action_u = action.upper()
        if action_u not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported Alpaca action: {action!r}")

        requested_qty = float(quantity)

        # Asset-class seam (crypto RFC §3.2 E1/E2): pair-form symbols (or an
        # explicit asset_class="crypto") take the crypto order rules; plain
        # tickers take the existing equity path UNTOUCHED. The equity path is
        # TIF=DAY by construction — a caller-supplied non-DAY TIF on an
        # equity order is a wiring error and fails loud, never silently
        # remapped.
        if classify_asset_class(symbol, asset_class) == ASSET_CLASS_CRYPTO:
            return self._place_crypto_market_order(
                symbol=symbol,
                action=action_u,
                requested_qty=requested_qty,
                time_in_force=time_in_force,
            )
        if time_in_force is not None and str(time_in_force).strip().lower() != "day":
            raise ValueError(
                f"equity place_order is TIF=DAY only, got {time_in_force!r} "
                f"for {symbol}"
            )

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

    @staticmethod
    def _crypto_tif_enum(tif: str) -> Any:
        from alpaca.trading.enums import TimeInForce

        return TimeInForce.GTC if tif == "gtc" else TimeInForce.IOC

    def _place_crypto_market_order(
        self,
        *,
        symbol: str,
        action: str,
        requested_qty: float,
        time_in_force: str | None,
    ) -> dict[str, Any]:
        """Crypto market order (crypto RFC §3.2: E1/E2 TIF, E5/E6 grid, E11).

        - TIF is GTC or IOC only; DAY is rejected (E1/E2). Default is IOC —
          the RFC's TIF policy maps IOC to "immediate entry", which is what a
          market order is; GTC is for resting limits / protective stops.
        - No fractionable lookup and NO whole-share snap (E5/E6): crypto is
          natively fractional; the quantity is floored onto the per-pair
          ``min_trade_increment`` grid and rejected below ``min_order_size``.
        - SELL is covered-only (E11): sell qty <= held qty, asserted before
          submit — crypto stays long-only by construction.
        """
        from alpaca.trading.enums import OrderSide
        from alpaca.trading.requests import MarketOrderRequest

        tif = str(time_in_force or CRYPTO_MARKET_DEFAULT_TIF).strip().lower()
        violation = validate_crypto_order(
            order_type="market",
            time_in_force=tif,
            qty=requested_qty,
        )
        if violation is not None:
            status, why = violation
            return self._no_submit_result(
                symbol,
                action,
                requested_qty,
                status=status,
                reason=(
                    f"crypto {action} qty {requested_qty!r} on {symbol} "
                    f"rejected at preflight: {why}"
                ),
            )
        if action == "SELL":
            held_qty = self.get_position(symbol)
            no_short = crypto_no_short_violation(requested_qty, held_qty)
            if no_short is not None:
                return self._no_submit_result(
                    symbol,
                    action,
                    requested_qty,
                    status=CRYPTO_NO_SHORT_STATUS,
                    reason=f"crypto SELL on {symbol} rejected: {no_short}",
                )
        try:
            spec = self._resolve_crypto_spec(symbol)
        except _CryptoSpecLookupError as exc:
            return self._no_submit_result(
                symbol,
                action,
                requested_qty,
                status=CRYPTO_SPEC_LOOKUP_FAILED_STATUS,
                reason=(
                    f"Alpaca get_asset({symbol!r}) crypto order-grid lookup "
                    f"failed ({exc}); failing closed on crypto {action} qty "
                    f"{requested_qty} (no submit, not cached — will retry)"
                ),
            )
        submit_qty = snap_qty_to_increment(requested_qty, spec.min_trade_increment)
        if submit_qty <= 0.0 or submit_qty < spec.min_order_size - QTY_INTEGRAL_EPS:
            return self._no_submit_result(
                symbol,
                action,
                requested_qty,
                status=BELOW_MIN_ORDER_SIZE_STATUS,
                reason=(
                    f"crypto {action} qty {requested_qty} on {symbol} snaps to "
                    f"{submit_qty} on the min_trade_increment grid "
                    f"({spec.min_trade_increment}), below min_order_size "
                    f"({spec.min_order_size}); rejected (never rounded up)"
                ),
            )
        request = MarketOrderRequest(
            symbol=symbol,
            qty=submit_qty,
            side=OrderSide.BUY if action == "BUY" else OrderSide.SELL,
            time_in_force=self._crypto_tif_enum(tif),
        )
        order = self._require_client().submit_order(order_data=request)
        result = _order_to_dict(order)
        result.update({
            "action": action,
            "quantity": float(submit_qty),
            "requested_quantity": requested_qty,
            "asset_class": ASSET_CLASS_CRYPTO,
            "time_in_force": tif,
            "skipped": False,
        })
        return result

    def _resolve_crypto_spec(self, symbol: str) -> CryptoAssetSpec:
        """Per-pair order grid: pinned snapshot first (RFC §3.1), else a
        fail-closed ``get_asset`` lookup cached only on confirmed success."""
        key = str(symbol).upper()
        pinned = self._crypto_asset_specs.get(key)
        if pinned is not None:
            return pinned
        cached = self._crypto_spec_cache.get(key)
        if cached is not None:
            return cached
        try:
            asset = self._require_client().get_asset(symbol)
            spec = CryptoAssetSpec.from_asset(key, asset)
        except Exception as exc:  # noqa: BLE001 — surface as a fail-closed signal
            raise _CryptoSpecLookupError(repr(exc)) from exc
        self._crypto_spec_cache[key] = spec
        return spec

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
        # Crypto guard (E1/E2 fail-closed): this path is a MARKET + TIF=DAY
        # shape by construction, which the broker rejects for crypto. Notional
        # crypto orders are out of scope for the D-C1 slice — refuse
        # explicitly rather than submit a doomed DAY order.
        if is_crypto_pair(symbol):
            raise ValueError(
                f"place_notional_order is an equity (TIF=DAY) path; crypto "
                f"pair {symbol!r} must use qty-based place_order "
                f"(GTC/IOC) — notional crypto orders are not supported in "
                "the D-C1 slice"
            )
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
        """Alpaca supports broker-side stops only for WHOLE-share quantities —
        for EQUITIES.

        A protective ``StopOrderRequest`` (GTC) is rejected for a fractional
        equity position, so when asked about a fractional ``quantity`` we
        return ``False`` — the caller must protect that position with a
        software stop rather than open a fractional holding whose broker-side
        stop will fail. With no quantity (legacy callers) we report the
        whole-share capability.

        Crypto pairs invert this (crypto RFC §5.1 / E8): the GTC stop-limit
        path accepts NATIVE fractional quantities, so a crypto symbol answers
        ``True`` regardless of fractionality — route through
        :meth:`place_crypto_stop_limit`.
        """
        if symbol is not None and is_crypto_pair(symbol):
            return True
        if quantity is not None and not is_whole_share(float(quantity)):
            return False
        return True

    def place_stop_order(self, symbol: str, quantity: float, stop_price: float) -> dict[str, Any]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        self._assert_account_active()
        # Crypto guard (E8): the SDK crypto order-type matrix has NO plain
        # stop order (market/limit/stop_limit only). Refuse at preflight and
        # direct to the crypto stop-limit path instead of submitting an order
        # the broker will bounce after the position is already open.
        if is_crypto_pair(symbol):
            raise ValueError(
                f"Alpaca has no plain stop order for crypto ({symbol}); use "
                "place_crypto_stop_limit (GTC stop_limit, native fractional qty)"
            )
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

    def place_crypto_stop_limit(
        self,
        symbol: str,
        quantity: float,
        stop_price: float,
        limit_price: float,
    ) -> dict[str, Any]:
        """Broker-resident protective GTC stop-limit SELL for a crypto pair
        (crypto RFC §5.1 / E8): ``StopLimitOrderRequest`` + ``TimeInForce.GTC``
        in NATIVE fractional quantity — no whole-share gate.

        Broker residency means the order survives machine death: if this Mac
        dies, sleeps, or loses network, a RESTING ORDER persists at the
        broker. **This is NOT an execution guarantee**: a stop-LIMIT can gap
        through in a fast move without filling at all — the order rests,
        triggers, and then may not execute if the market gaps past the limit
        price before it can. The honest claim is narrower: broker residency
        means the STOP ORDER survives machine death; it does not mean the
        position is guaranteed to exit at or near the stop price. The limit
        band below the stop and the residual gap-through probability are set
        ex-ante from the Stage-0 per-pair gap statistics (RFC §4.4/§5.1) —
        never assumed benign.

        Fail-loud (not no-submit) on violations: a protective stop that
        cannot be placed is a Tier-1 condition for the caller (alert +
        re-place before any new entry, RFC §5.1) — it must never be a quiet
        skipped-order row.
        """
        from alpaca.trading.enums import OrderSide
        from alpaca.trading.requests import StopLimitOrderRequest

        self._assert_account_active()
        if not is_crypto_pair(symbol):
            raise ValueError(
                f"place_crypto_stop_limit is crypto-only; equity symbol "
                f"{symbol!r} uses place_stop_order (whole-share GTC stop)"
            )
        requested_qty = float(quantity)
        violation = validate_crypto_order(
            order_type="stop_limit",
            time_in_force=CRYPTO_STOP_LIMIT_TIF,
            qty=requested_qty,
        )
        if violation is not None:
            _, why = violation
            raise ValueError(
                f"crypto stop-limit for {symbol} rejected at preflight: {why}"
            )
        stop_f = float(stop_price)
        limit_f = float(limit_price)
        if not (stop_f > 0.0 and limit_f > 0.0):
            raise ValueError(
                f"stop/limit prices must be positive: stop={stop_price!r}, "
                f"limit={limit_price!r}"
            )
        if limit_f > stop_f:
            raise ValueError(
                f"protective SELL stop-limit for {symbol} requires "
                f"limit <= stop (the limit band sits below the stop, RFC "
                f"§5.1); got stop={stop_f}, limit={limit_f}"
            )
        # E11: a protective stop may only cover held quantity — a stop for
        # more than the position would be a short on trigger.
        held_qty = self.get_position(symbol)
        no_short = crypto_no_short_violation(requested_qty, held_qty)
        if no_short is not None:
            raise ValueError(f"crypto stop-limit for {symbol} rejected: {no_short}")
        spec = self._resolve_crypto_spec(symbol)  # _CryptoSpecLookupError is loud here
        submit_qty = snap_qty_to_increment(requested_qty, spec.min_trade_increment)
        if submit_qty <= 0.0 or submit_qty < spec.min_order_size - QTY_INTEGRAL_EPS:
            raise ValueError(
                f"crypto stop-limit qty {requested_qty} on {symbol} snaps to "
                f"{submit_qty} (< min_order_size {spec.min_order_size}); the "
                "position is below the broker's minimum protectable size"
            )
        submit_stop = round_price_to_increment(stop_f, spec.price_increment)
        submit_limit = round_price_to_increment(limit_f, spec.price_increment)
        request = StopLimitOrderRequest(
            symbol=symbol,
            qty=submit_qty,
            side=OrderSide.SELL,
            time_in_force=self._crypto_tif_enum(CRYPTO_STOP_LIMIT_TIF),
            stop_price=submit_stop,
            limit_price=submit_limit,
        )
        order = self._require_client().submit_order(order_data=request)
        result = _order_to_dict(order)
        result.update({
            "action": "SELL",
            "quantity": float(submit_qty),
            "requested_quantity": requested_qty,
            "stop_price": float(submit_stop),
            "limit_price": float(submit_limit),
            "asset_class": ASSET_CLASS_CRYPTO,
            "time_in_force": CRYPTO_STOP_LIMIT_TIF,
        })
        return result

    def cancel_order(self, order_id: str) -> bool:
        self._require_client().cancel_order_by_id(order_id)
        return True

    def is_market_open(self, symbol: str | None = None) -> bool:
        """Whether the market for ``symbol`` is open.

        Crypto trades 24/7 (crypto RFC §3.2 E10): crypto paths never consult
        ``get_clock().is_open`` — a pair-form symbol answers ``True``
        unconditionally, with NO broker round-trip. The default (no symbol /
        equity ticker) keeps the equity clock behavior unchanged.
        """
        if symbol is not None and is_crypto_pair(symbol):
            return True
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
