"""Alpaca broker adapter.

The alpaca-py import is intentionally lazy so paper tests and shadow
orchestration can import renquant-execution without broker SDK credentials.
"""
from __future__ import annotations

import os
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
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
from .coverage_report import (
    CoverageObservation,
    CoverageReport,
    _build_coverage_report,
    compute_snapshot_hash,
    default_execution_source_commit,
    default_execution_version,
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


# Bounded (connect, read) timeout for the account-read calls the
# P-BROKER-CONNECT preflight makes (``connect()`` / ``get_account_value()``).
# The alpaca-py ``RESTClient`` builds every HTTP call with NO ``timeout`` key
# (``alpaca/common/rest.py::_one_request`` -> ``self._session.request(...)``),
# so ``requests`` defaults to ``timeout=None`` and a stalled socket hangs until
# the OS-level TCP timeout (~82s observed on the 2026-08-11 07:00 intraday abort:
# ``read timeout=None``) before the preflight can even fail. A bounded default
# makes a stalled read fail FAST so the pipeline's bounded connect-retry can act
# well within the intraday cadence. Values chosen deliberately: a healthy Alpaca
# ``GET /v2/account`` returns in well under a second, so 5s connect / 10s read is
# ample slack for a transient blip without tolerating an open-ended hang.
_DEFAULT_BROKER_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_BROKER_READ_TIMEOUT_SECONDS = 10.0

# Cache for the lazily-built ``requests.Session`` subclass. Built on first use
# (inside a connected broker) rather than at module import, so importing
# ``renquant_execution`` still needs neither ``requests`` nor the ``alpaca``
# extra -- consistent with this module's lazy ``alpaca`` import (paper/shadow
# orchestration import it without the broker SDK installed).
_BOUNDED_TIMEOUT_SESSION_CLS: type | None = None


def _bounded_timeout_session_class() -> type:
    """Lazily build (once) the ``requests.Session`` subclass that injects a
    bounded ``(connect, read)`` timeout into any request lacking one.

    Why a substituted session at all: the alpaca-py SDK exposes NO timeout knob
    on ``TradingClient`` / the base ``RESTClient`` ([VERIFIED] alpaca-py 0.43.5)
    -- it issues every call as ``self._session.request(method, url, **opts)``
    with ``opts`` carrying only ``headers`` / ``allow_redirects`` /
    ``params`` | ``json``, never a ``timeout``. Rather than fork or monkeypatch
    the SDK request loop, we replace the session object and supply the default
    from here. ``requests`` is imported here (not at module top) because it only
    ships with the ``alpaca`` extra, and this class is only ever needed AFTER
    ``connect()`` has already imported the SDK.

    The default is applied ONLY while ``default_timeout`` is set (armed by
    :meth:`AlpacaBroker._bounded_account_timeout` around the two account-read
    calls the preflight exercises). It stays ``None`` for every other call --
    notably order submission -- so those keep their existing, unbounded socket
    semantics byte-for-byte. A caller that passes its own ``timeout`` wins.
    """
    global _BOUNDED_TIMEOUT_SESSION_CLS
    if _BOUNDED_TIMEOUT_SESSION_CLS is None:
        import requests

        class _BoundedTimeoutSession(requests.Session):
            def __init__(self, *args: Any, default_timeout: Any = None, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.default_timeout = default_timeout

            def request(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
                if self.default_timeout is not None and kwargs.get("timeout") is None:
                    kwargs["timeout"] = self.default_timeout
                return super().request(*args, **kwargs)

        _BOUNDED_TIMEOUT_SESSION_CLS = _BoundedTimeoutSession
    return _BOUNDED_TIMEOUT_SESSION_CLS


@dataclass(frozen=True)
class CryptoQuoteSnapshot:
    """A latest-quote lookup result with provenance and freshness evidence.

    Added (2026-07-12, Codex round-2 review finding 4 on execution#34):
    replaces a bare ``float`` reference price -- a probe deriving canary
    order prices from market data must be able to reason about whether that
    data is fresh and which symbol it actually came from, not just trust an
    unadorned number.
    """

    symbol: str
    bid_price: float
    ask_price: float
    mid_price: float
    timestamp: datetime
    age_seconds: float


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


@dataclass
class _CryptoStopCoverageObservation:
    """One bounded broker observation: crypto positions, the qualifying
    protective-stop orders found per symbol, stop-shaped-but-not-resting
    orders per symbol, and the coverage violations computed from them.

    This backs BOTH :meth:`AlpacaBroker.check_crypto_stop_coverage`
    (violations only, public signature unchanged) and
    :meth:`AlpacaBroker.publish_stop_coverage_report` (the full
    :class:`~renquant_execution.coverage_report.CoverageReport`) from the
    exact SAME broker query — never two independent round-trips that could
    observe different states (Codex review 2026-07-13T00:16:11Z finding 2).
    """

    positions: dict[str, float]
    qualifying_orders: dict[str, list[dict[str, Any]]]
    non_resting_shaped: dict[str, list[dict[str, Any]]]
    violations: list[dict[str, Any]]


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
        connect_timeout_seconds: float = _DEFAULT_BROKER_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = _DEFAULT_BROKER_READ_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = bool(paper)
        self.env_prefix = env_prefix
        self.label = label
        # Bounded (connect, read) timeout armed only around the preflight
        # account-read calls (connect / get_account_value); see
        # _bounded_timeout_session_class and _bounded_account_timeout.
        # Order-submission calls are never armed, so their socket semantics are
        # unchanged.
        self._connect_timeout = float(connect_timeout_seconds)
        self._read_timeout = float(read_timeout_seconds)
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
        # Give the account-read calls a bounded read/connect timeout so a
        # stalled Alpaca socket fails FAST instead of hanging on the OS TCP
        # timeout (the 2026-08-11 07:00 P-BROKER-CONNECT abort). Scoped to the
        # preflight account reads only -- order submission stays unbounded.
        self._install_bounded_timeout_session()
        with self._bounded_account_timeout():
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

        status = _enum_value(getattr(self._account, "status", ""))
        if status and status != "active":
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
        # Bounded-timeout scope: this is one of the two P-BROKER-CONNECT
        # preflight reads. Arming here (not inside _refresh_account) keeps every
        # other _refresh_account caller -- e.g. _assert_account_active on the
        # order path -- at its existing, unbounded behaviour.
        with self._bounded_account_timeout():
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
            if _enum_value(getattr(order, "status", "")) in {"filled", "partially_filled"}:
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

    def _wait_for_order_terminal_cancel(
        self,
        order_id: str,
        *,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.25,
    ) -> bool:
        """Poll Alpaca until ``order_id`` reaches a genuinely terminal
        ``canceled`` state, or ``timeout_seconds`` elapses.

        Alpaca's cancellation is ASYNCHRONOUS (Codex review 2026-07-12
        finding 3): ``cancel_order`` returns as soon as the cancel *request*
        is accepted, not once the order has actually reached a terminal
        state — the order can sit in ``pending_cancel`` for a short window,
        or (rare race) fill/reject before the cancel takes effect.

        Deliberate, explicit defaults for a synchronous broker-adapter call
        inside the daily-run loop (a judgment call, not tuned against a
        production SLA): ``timeout_seconds=5.0`` — Alpaca paper/live cancel
        acks are typically sub-second, so 5s gives ample margin without
        stalling the caller noticeably — and ``poll_interval_seconds=0.25``
        — frequent enough to resolve quickly, coarse enough not to hammer the
        API. Callers with a tighter or looser SLA may override both.

        Returns ``True`` only once the order's status is a CONFIRMED terminal
        ``canceled``. Returns ``False`` on timeout OR if the order reaches a
        *different* terminal state first (e.g. it filled before the cancel
        could take effect) — either way the caller must NOT treat the old
        stop as gone and must NOT proceed to place a replacement (that could
        create two overlapping resting stops).
        """
        client = self._require_client()
        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        while True:
            try:
                order = client.get_order_by_id(order_id)
            except Exception:  # noqa: BLE001 — treat as "not yet confirmed", keep polling
                order = None
            if order is not None:
                status = _enum_value(getattr(order, "status", ""))
                if status == _TERMINAL_CANCELED_STATUS:
                    return True
                if status in _TERMINAL_NON_CANCEL_STATUSES:
                    return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_interval_seconds)

    def wait_for_order_terminal_cancel(
        self,
        order_id: str,
        *,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.25,
    ) -> bool:
        """Public wrapper for :meth:`_wait_for_order_terminal_cancel`.

        Added (2026-07-12) so same-package callers outside this class do not
        need to reach into the underscore-prefixed method directly. Shares
        the exact same "confirm, don't assume" polling discipline Codex
        required on PR #31 between two call sites: :meth:`replace_crypto_stop_limit`
        (the protective-stop cancel-then-replace path, unchanged -- it still
        calls the private method directly, this wrapper does not alter that
        logic) and the crypto Stage-0 battery's transactional probes
        (``crypto_stage0_checks.check_gtc_order_acceptance`` /
        ``check_stop_limit_acceptance``) -- both of which request cancellation
        of a canary order and must confirm a genuinely CONFIRMED terminal
        ``canceled`` state before reporting PASS, not merely that
        ``cancel_order`` didn't raise. See :meth:`_wait_for_order_terminal_cancel`
        for the full poll-loop docstring/rationale and default timeout choice.
        """
        return self._wait_for_order_terminal_cancel(
            order_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def replace_crypto_stop_limit(
        self,
        old_order_id: str,
        symbol: str,
        quantity: float,
        stop_price: float,
        limit_price: float,
        *,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.25,
    ) -> dict[str, Any]:
        """Cancel-then-replace a resting crypto protective stop-limit.

        Alpaca has no atomic replace for stop-limit orders — this cancels the
        old order first, CONFIRMS the cancellation actually reached a
        terminal ``canceled`` state (:meth:`_wait_for_order_terminal_cancel`
        — Codex review 2026-07-12 finding 3), and only then places the
        replacement via :meth:`place_crypto_stop_limit`. If the cancel is
        never confirmed, the replacement is deliberately NOT submitted (that
        could create two overlapping resting stops — the exact
        "duplicate/competing protective stops" condition
        :meth:`check_crypto_stop_coverage` fails closed on). If the
        cancellation IS confirmed but the replacement placement itself then
        fails, the position is genuinely UNPROTECTED — the single most
        severe case. "Fails" here is deliberately broader than "raises"
        (Codex review 2026-07-12T21:52:07Z, round 2): a returned result with
        no ``order_id``, or whose own ``status`` is not a genuinely resting
        one, is treated identically to a raised exception — ``protected``
        is never set ``True`` on an unvalidated return value, even though
        :meth:`place_crypto_stop_limit`'s own current contract always
        raises rather than returning a no-submit-shaped dict.

        Returns a discriminated result dict — the return value must be
        checked explicitly, not inferred from the absence of an exception
        (Codex review 2026-07-12 finding 4: "a plain no-submit/exception is
        insufficient because callers can forget to interpret it")::

            {
                "protected": bool,   # True only on a confirmed clean replace
                "status": "replaced" | "cancel_unconfirmed" | "unprotected_after_cancel",
                "old_order_id": str,
                "new_order_id": str | None,  # set only when status == "replaced"
                "unprotected_reason": str | None,  # "cancel_unconfirmed" or
                                                    # "replacement_failed_after_confirmed_cancel"
                "reason": str,        # human-readable detail, always present
                ...                   # place_crypto_stop_limit's own fields, on success
            }

        On the two failure statuses this ALSO emits a ``RuntimeWarning``
        (this file's existing fail-closed signaling convention — see
        ``_no_submit_result``), so a caller that only watches
        warnings/logs — not the return value — still notices. And beyond
        this function's own return value: the true "upstream orchestration
        consumes this to block new entries and page the owner" wiring is
        :meth:`check_crypto_stop_coverage` — a failed replace leaves
        ``symbol`` uncovered, so the very next scheduled
        ``check_crypto_stop_coverage()`` call (the orchestrator scheduler is
        meant to call it before every crypto entry, per orchestrator PR
        #497's own Codex review, fixed in parallel) independently
        re-discovers the same symbol as a violation — a second, independent
        layer of protection beyond this function's own return value.
        """
        self.cancel_order(old_order_id)
        confirmed = self._wait_for_order_terminal_cancel(
            old_order_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if not confirmed:
            reason = (
                f"cancellation of {old_order_id} for {symbol} did not reach "
                f"a confirmed terminal CANCELED state within "
                f"{timeout_seconds}s; replacement stop-limit NOT submitted "
                "(would risk two overlapping resting stops) — position may "
                "be UNPROTECTED, Tier-1 condition"
            )
            warnings.warn(reason, RuntimeWarning, stacklevel=2)
            return {
                "protected": False,
                "status": "cancel_unconfirmed",
                "old_order_id": old_order_id,
                "new_order_id": None,
                "unprotected_reason": "cancel_unconfirmed",
                "reason": reason,
            }
        def _unprotected_after_cancel(detail: str) -> dict[str, Any]:
            reason = (
                f"cancellation of {old_order_id} for {symbol} CONFIRMED, but "
                f"the replacement stop-limit placement failed ({detail}); "
                f"{symbol} is now genuinely UNPROTECTED (no resting stop at "
                "all) — Tier-1 condition, most severe case"
            )
            warnings.warn(reason, RuntimeWarning, stacklevel=3)
            return {
                "protected": False,
                "status": "unprotected_after_cancel",
                "old_order_id": old_order_id,
                "new_order_id": None,
                "unprotected_reason": "replacement_failed_after_confirmed_cancel",
                "reason": reason,
            }

        try:
            result = self.place_crypto_stop_limit(symbol, quantity, stop_price, limit_price)
        except Exception as exc:  # noqa: BLE001 — must surface as a Tier-1 result, not crash
            return _unprotected_after_cancel(repr(exc))

        # Codex review 2026-07-12T21:52:07Z: place_crypto_stop_limit's own
        # contract today always raises on any rejection (it never returns a
        # no-submit-style dict) — but replace_crypto_stop_limit must not
        # depend on that invariant holding forever, and a broker submit
        # call can in principle return normally with an order that never
        # actually became a resting stop (e.g. an immediate reject encoded
        # in the response body rather than an exception). Treat a missing
        # order_id, a no-submit-shaped result, or a returned order whose own
        # status is not genuinely resting exactly like an exception here —
        # never declare "protected" on an unvalidated return value.
        new_order_id = result.get("order_id")
        returned_status = _enum_value(result.get("status", ""))
        if not new_order_id:
            return _unprotected_after_cancel(
                "place_crypto_stop_limit returned no order_id "
                f"(status={returned_status!r})"
            )
        if not _is_resting_order_status(returned_status):
            return _unprotected_after_cancel(
                f"place_crypto_stop_limit returned a non-resting status "
                f"{returned_status!r} for order {new_order_id}"
            )

        result = dict(result)
        result.update({
            "protected": True,
            "status": "replaced",
            "old_order_id": old_order_id,
            "new_order_id": new_order_id,
            "unprotected_reason": None,
            "reason": (
                f"cancelled {old_order_id} (confirmed) and replaced with "
                f"{new_order_id} for {symbol}"
            ),
        })
        return result

    def get_open_orders_detailed(
        self, asset_class: str | None = None
    ) -> list[dict[str, Any]]:
        """All open orders with full detail (type, stop/limit prices, TIF).

        Unlike :meth:`get_open_orders` (which returns symbol names only), this
        returns full order dicts needed for stop-coverage auditing.

        Every enum-bearing field is re-derived via :func:`_enum_value` rather
        than trusting ``_order_to_dict``'s naive ``str(...)`` cast: a real
        alpaca-py SDK ``Order``'s enum fields (``status``, ``side``,
        ``order_type``, ``time_in_force``, ...) stringify via plain ``str()``
        to ``"ClassName.MEMBER"`` (`Enum.__str__`), NOT the lowercase wire
        value — only ``.value`` gives that. The stop-coverage / replace logic
        that consumes these rows depends on exact matches against
        ``"stop_limit"`` / ``"sell"`` / ``"gtc"`` / a resting status string,
        so this normalizes every field it reads the same way
        ``_order_matches_asset_class`` already does for ``asset_class``.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        wanted = self._normalize_asset_class_filter(asset_class)
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        rows: list[dict[str, Any]] = []
        for order in self._require_client().get_orders(filter=request):
            if not self._order_matches_asset_class(order, wanted):
                continue
            d = _order_to_dict(order)
            d["status"] = _enum_value(getattr(order, "status", ""))
            d["side"] = _enum_value(getattr(order, "side", "")).upper()
            d["order_type"] = _enum_value(getattr(order, "order_type", ""))
            d["time_in_force"] = _enum_value(getattr(order, "time_in_force", ""))
            d["stop_price"] = float(getattr(order, "stop_price", 0.0) or 0.0)
            d["limit_price"] = float(getattr(order, "limit_price", 0.0) or 0.0)
            rows.append(d)
        return rows

    def _observe_crypto_stop_coverage(self) -> _CryptoStopCoverageObservation:
        """Single bounded broker observation: query positions once, query
        open orders once, and compute violations from that one snapshot.

        This is the exact same logic :meth:`check_crypto_stop_coverage` has
        always run (extracted unchanged, not rewritten — see that method's
        docstring for the full violation-semantics writeup); it is factored
        out here so :meth:`publish_stop_coverage_report` can derive a
        :class:`~renquant_execution.coverage_report.CoverageReport` from the
        SAME observation the violation list came from, instead of issuing a
        second, independent (and possibly racy) broker query.
        """
        positions = self.get_all_positions()
        crypto_positions = {
            p["symbol"]: float(p["qty"])
            for p in positions
            if is_crypto_pair(p["symbol"]) and float(p["qty"]) > 0
        }
        if not crypto_positions:
            return _CryptoStopCoverageObservation(
                positions={},
                qualifying_orders={},
                non_resting_shaped={},
                violations=[],
            )

        open_orders = self.get_open_orders_detailed(asset_class=ASSET_CLASS_CRYPTO)
        qualifying: dict[str, list[dict[str, Any]]] = {}
        non_resting_shaped: dict[str, list[dict[str, Any]]] = {}
        for order in open_orders:
            sym = order.get("symbol", "")
            if _is_qualifying_protective_stop(order):
                qualifying.setdefault(sym, []).append(order)
            elif _is_stop_shaped_protective_candidate(order):
                non_resting_shaped.setdefault(sym, []).append(order)

        violations: list[dict[str, Any]] = []
        for symbol, held_qty in crypto_positions.items():
            qual = qualifying.get(symbol, [])
            if len(qual) >= 2:
                violations.append({
                    "symbol": symbol,
                    "held_qty": held_qty,
                    "covered_qty": sum(float(o["quantity"]) for o in qual),
                    "violation_kind": "duplicate",
                    "reason": (
                        f"duplicate/competing protective stops for {symbol} "
                        f"— {len(qual)} independently-executable GTC "
                        "stop-limit SELL orders found; ambiguous coverage, "
                        "more than one could fire (fail-closed, quantities "
                        "not summed)"
                    ),
                })
                continue
            if len(qual) == 1:
                try:
                    spec = self._resolve_crypto_spec(symbol)
                except _CryptoSpecLookupError as exc:
                    violations.append({
                        "symbol": symbol,
                        "held_qty": held_qty,
                        "covered_qty": float(qual[0]["quantity"]),
                        "violation_kind": "spec_lookup_failed",
                        "reason": (
                            f"{symbol}: crypto order-grid spec lookup failed "
                            f"({exc}); cannot verify stop coverage within the "
                            "pair's own tolerance — failing closed rather "
                            "than falling back to the equity epsilon"
                        ),
                    })
                    continue
                tol = spec.min_trade_increment
                covered = float(qual[0]["quantity"])
                if covered < held_qty - tol:
                    violations.append({
                        "symbol": symbol,
                        "held_qty": held_qty,
                        "covered_qty": covered,
                        "violation_kind": "partial",
                        "reason": (
                            f"{symbol}: held {held_qty}, single qualifying "
                            f"stop covers only {covered} (shortfall "
                            f"{held_qty - covered:.9f}, tolerance {tol})"
                        ),
                    })
                continue
            # len(qual) == 0
            if non_resting_shaped.get(symbol):
                violations.append({
                    "symbol": symbol,
                    "held_qty": held_qty,
                    "covered_qty": 0.0,
                    "violation_kind": "non_resting_ignored",
                    "reason": (
                        f"{symbol}: a GTC stop-limit SELL order exists but "
                        "is not in a genuinely resting status right now "
                        "(e.g. mid cancel/replace) — not counted as coverage"
                    ),
                })
            else:
                violations.append({
                    "symbol": symbol,
                    "held_qty": held_qty,
                    "covered_qty": 0.0,
                    "violation_kind": "uncovered",
                    "reason": (
                        f"{symbol}: no resting GTC stop-limit found "
                        f"(held {held_qty})"
                    ),
                })
        return _CryptoStopCoverageObservation(
            positions=crypto_positions,
            qualifying_orders=qualifying,
            non_resting_shaped=non_resting_shaped,
            violations=violations,
        )

    def check_crypto_stop_coverage(self) -> list[dict[str, Any]]:
        """Tier-1 audit: every crypto position MUST have exactly ONE resting
        GTC stop-limit SELL order covering at least its held quantity.

        A "qualifying" protective stop (Codex review 2026-07-12 findings 1/2/5
        — see :func:`_is_qualifying_protective_stop`) satisfies ALL of:
        ``order_type == "stop_limit"``, ``side == "SELL"``,
        ``time_in_force == "gtc"`` (case-insensitive), ``stop_price > 0``,
        ``limit_price > 0``, and a genuinely RESTING broker status — NOT a
        transitional ``pending_*`` sub-state that Alpaca's
        ``QueryOrderStatus.OPEN`` filter still reports as "open" (see
        :func:`_is_resting_order_status`).

        Coverage is evaluated PER SYMBOL by *counting* qualifying stops,
        never by summing quantities across multiple orders (finding 2: two
        independently-executable stops can both fire — over-sell / race
        risk with another exit):

        - 0 qualifying stops -> violation, ``violation_kind="uncovered"``
          (or ``"non_resting_ignored"`` if a stop-shaped order exists for the
          symbol but only in a non-resting status right now, e.g. mid
          cancel/replace).
        - exactly 1 qualifying stop, qty >= held qty within the pair's own
          ``min_trade_increment`` tolerance (finding 5 — never the equity
          ``QTY_INTEGRAL_EPS``) -> covered, no violation.
        - exactly 1 qualifying stop, qty short of that -> violation,
          ``violation_kind="partial"``.
        - 2+ qualifying stops for the same symbol -> violation,
          ``violation_kind="duplicate"`` — FAILS CLOSED on ambiguity; the
          summed quantity is NEVER treated as safe coverage.

        Returns a list of violations (empty = all covered); public signature
        is unchanged so the orchestrator scheduler (PR #497) needs no
        coordinated change. Each violation dict adds a ``"violation_kind"``
        field alongside the existing human-readable ``"reason"`` string, so
        callers can distinguish severity/cause. A non-empty result is a
        Tier-1 condition: the caller must not admit new crypto entries until
        it is empty again.

        Note: each crypto symbol contributes AT MOST one violation record to
        the returned list (never one per order — e.g. duplicate/competing
        stops for one symbol is still a single "duplicate" violation for
        that symbol), which is why ``len(violations) == positions_total -
        positions_covered`` holds exactly in
        :meth:`publish_stop_coverage_report`'s
        :class:`~renquant_execution.coverage_report.CoverageReport`.
        """
        return self._observe_crypto_stop_coverage().violations

    def publish_stop_coverage_report(
        self, account_id: str | None = None
    ) -> CoverageReport:
        """The ONLY execution-owned path to a diagnostic
        :class:`~renquant_execution.coverage_report.CoverageReport` (Codex
        review 2026-07-13T00:16:11Z finding 1).

        The returned report has ``trust_level = "unattested_diagnostic"``
        — it is suitable for monitoring and alerting but is NOT
        authorization evidence for any entry gate.

        Every observation field — ``violations``, ``positions_covered``,
        ``positions_total``, ``order_ids`` — is derived exclusively from
        one real, bounded broker observation
        (:meth:`_observe_crypto_stop_coverage`, the SAME query
        :meth:`check_crypto_stop_coverage` uses); none of it is accepted as a
        caller-supplied argument. ``account_id`` and ``environment`` are
        likewise resolved from the connected broker itself
        (:meth:`get_account_id`, ``self.paper``), never from the caller.

        ``account_id``, if passed, is treated as a consistency ASSERTION —
        not an override: it must equal the connected broker's own
        :meth:`get_account_id`, or this raises. This lets a caller fail loud
        if it thinks it's talking to a different account than it actually
        is, without ever letting a caller stamp an arbitrary identity onto
        a report.

        The two ``*_snapshot_hash`` fields on the returned report bind it to
        the exact position / qualifying-order observation used to compute
        it (Codex finding 2); ``violations == positions_total -
        positions_covered`` is enforced by
        :class:`~renquant_execution.coverage_report.CoverageReport`'s own
        validation (finding 3), and ``source_version`` is derived from this
        package's own installed metadata rather than supplied by the
        caller.
        """
        real_account_id = self.get_account_id()
        if account_id is not None and account_id != real_account_id:
            raise ValueError(
                f"account_id mismatch: caller passed {account_id!r} but the "
                f"connected broker's account is {real_account_id!r} — "
                "publish_stop_coverage_report() always uses the broker's "
                "own verified account identity, never a caller override "
                "(pass None, or the broker's own get_account_id() value, "
                "as a no-op consistency assertion)"
            )

        raw_obs = self._observe_crypto_stop_coverage()
        environment = "paper" if self.paper else "live"

        positions_total = len(raw_obs.positions)
        violations_count = len(raw_obs.violations)
        positions_covered = positions_total - violations_count

        order_ids = tuple(sorted({
            str(order.get("order_id", ""))
            for orders in raw_obs.qualifying_orders.values()
            for order in orders
            if str(order.get("order_id", ""))
        }))

        position_snapshot_hash = compute_snapshot_hash(
            {symbol: qty for symbol, qty in sorted(raw_obs.positions.items())}
        )
        order_snapshot_hash = compute_snapshot_hash({
            symbol: sorted(orders, key=lambda o: str(o.get("order_id", "")))
            for symbol, orders in sorted(raw_obs.qualifying_orders.items())
        })

        observation = CoverageObservation(
            account_id=real_account_id,
            environment=environment,
            observed_at_utc=datetime.now(timezone.utc),
            positions_covered=positions_covered,
            positions_total=positions_total,
            qualifying_order_ids=order_ids,
            position_snapshot_hash=position_snapshot_hash,
            order_snapshot_hash=order_snapshot_hash,
        )

        return _build_coverage_report(
            observation,
            source_version=default_execution_version(),
        )

    # ── thin wrappers for crypto Stage-0 battery (2026-07-12) ──────────────

    def get_account_info(self) -> dict[str, Any]:
        """Account metadata: status, crypto_status, buying power, paper flag.

        Thin wrapper over ``get_account()`` that surfaces the fields the
        Stage-0 crypto battery needs to verify the account is crypto-enabled
        on the correct environment -- without the battery importing alpaca-py
        or reaching into private broker state.
        """
        account = self._refresh_account()
        return {
            "account_id": str(getattr(account, "account_number", "") or ""),
            "status": _enum_value(getattr(account, "status", "")).upper(),
            "crypto_status": _enum_value(getattr(account, "crypto_status", "")).upper(),
            "buying_power": float(getattr(account, "buying_power", 0.0) or 0.0),
            "non_marginable_buying_power": float(
                getattr(account, "non_marginable_buying_power", 0.0) or 0.0
            ),
            "cash": float(getattr(account, "cash", 0.0) or 0.0),
            "portfolio_value": float(getattr(account, "portfolio_value", 0.0) or 0.0),
            "paper": self.paper,
        }

    def get_crypto_asset_spec(self, symbol: str) -> CryptoAssetSpec:
        """Public wrapper for the per-pair crypto order-grid spec lookup.

        Returns the ``CryptoAssetSpec`` for ``symbol`` -- either from the
        pinned snapshot or a live ``get_asset`` lookup (fail-closed, cached
        only on confirmed success). Raises ``RuntimeError`` on lookup
        failure so the caller can distinguish "pair not found" from "pair
        found with spec X".
        """
        try:
            return self._resolve_crypto_spec(symbol)
        except _CryptoSpecLookupError as exc:
            raise RuntimeError(
                f"crypto order-grid spec lookup for {symbol!r} failed: {exc}"
            ) from exc

    def place_crypto_limit_order(
        self,
        symbol: str,
        action: str,
        qty: float,
        limit_price: float,
        *,
        time_in_force: str = "gtc",
    ) -> dict[str, Any]:
        """Place a crypto GTC/IOC limit order (BUY or SELL).

        Thin wrapper for the SDK ``LimitOrderRequest`` -- the crypto limit
        order type that the production market-order and stop-limit paths do
        not cover. Primary consumer: the Stage-0 battery's GTC order
        acceptance test (place a limit BUY far below market, verify accepted,
        cancel immediately). Same crypto-only / paper-mode / TIF / spec
        preflight as the other crypto order methods.
        """
        from alpaca.trading.enums import OrderSide
        from alpaca.trading.requests import LimitOrderRequest

        self._assert_account_active()
        action_u = action.upper()
        if action_u not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported action: {action!r}")
        if not is_crypto_pair(symbol):
            raise ValueError(
                f"place_crypto_limit_order is crypto-only; got {symbol!r}"
            )
        tif = str(time_in_force or "gtc").strip().lower()
        violation = validate_crypto_order(
            order_type="limit", time_in_force=tif, qty=float(qty),
        )
        if violation is not None:
            _, why = violation
            raise ValueError(
                f"crypto limit order for {symbol} rejected at preflight: {why}"
            )
        spec = self._resolve_crypto_spec(symbol)
        submit_qty = snap_qty_to_increment(float(qty), spec.min_trade_increment)
        submit_price = round_price_to_increment(float(limit_price), spec.price_increment)
        request = LimitOrderRequest(
            symbol=symbol,
            qty=submit_qty,
            side=OrderSide.BUY if action_u == "BUY" else OrderSide.SELL,
            time_in_force=self._crypto_tif_enum(tif),
            limit_price=submit_price,
        )
        order = self._require_client().submit_order(order_data=request)
        result = _order_to_dict(order)
        result.update({
            "action": action_u,
            "order_type": "limit",
            "quantity": float(submit_qty),
            "requested_quantity": float(qty),
            "limit_price": float(submit_price),
            "asset_class": ASSET_CLASS_CRYPTO,
            "time_in_force": tif,
            "skipped": False,
        })
        return result

    def get_crypto_reference_quote(
        self, symbol: str, *, max_staleness_seconds: float = 60.0
    ) -> "CryptoQuoteSnapshot":
        """Latest bid/ask quote for a crypto pair, with provenance and a
        freshness check, via the market-data ``CryptoHistoricalDataClient``
        -- NOT the trading client.

        Added (2026-07-12, Codex review finding 3 on execution#34) so the
        Stage-0 battery's transactional probes can derive canary prices from
        the pair's REAL current price instead of universal magic constants
        ($0.01 buy-limit / $999,999,999 stop) that say nothing about
        whether a given pair's actual price band/tick grid would even
        accept an order at all -- a rejection at an implausible fixed price
        proves nothing about genuine GTC/stop-limit support.

        Strengthened (2026-07-12, Codex round-2 review finding 4): the
        original version returned a bare ``float`` with no quote timestamp,
        source, or symbol identity, so a probe could silently derive prices
        from a stale or mismatched quote. Returns a typed
        :class:`CryptoQuoteSnapshot` and raises if the quote's own
        ``timestamp`` is missing or older than ``max_staleness_seconds`` --
        deliberately scoped: this is a single latest-quote lookup with a
        staleness gate, not a versioned price-band/quote-schema system.
        """
        from alpaca.data.historical.crypto import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoLatestQuoteRequest

        client = CryptoHistoricalDataClient(self.api_key, self.secret_key)
        request = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
        try:
            quotes = client.get_crypto_latest_quote(request)
            quote = quotes[symbol] if hasattr(quotes, "__getitem__") else quotes
        except Exception as exc:
            raise RuntimeError(
                f"crypto latest-quote lookup for {symbol!r} failed: {exc}"
            ) from exc
        quote_symbol = str(getattr(quote, "symbol", "") or symbol)
        raw_timestamp = getattr(quote, "timestamp", None)
        if raw_timestamp is None:
            raise RuntimeError(
                f"crypto latest-quote for {symbol!r} has no timestamp -- "
                "cannot verify freshness before deriving a canary price"
            )
        timestamp = raw_timestamp if raw_timestamp.tzinfo else raw_timestamp.replace(
            tzinfo=timezone.utc
        )
        age_seconds = (
            datetime.now(timezone.utc) - timestamp
        ).total_seconds()
        if age_seconds > max_staleness_seconds:
            raise RuntimeError(
                f"crypto latest-quote for {symbol!r} is stale: "
                f"{age_seconds:.1f}s old (max {max_staleness_seconds}s) -- "
                f"quote timestamp={timestamp.isoformat()}"
            )
        if age_seconds < -5.0:
            raise RuntimeError(
                f"crypto latest-quote for {symbol!r} has an implausible "
                f"future timestamp {timestamp.isoformat()} "
                f"({-age_seconds:.1f}s ahead of now)"
            )
        bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
        ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
        if bid > 0.0 and ask > 0.0:
            mid = (bid + ask) / 2.0
        elif ask > 0.0:
            mid = ask
        elif bid > 0.0:
            mid = bid
        else:
            raise RuntimeError(f"no usable bid/ask quote for {symbol!r}")
        return CryptoQuoteSnapshot(
            symbol=quote_symbol,
            bid_price=bid,
            ask_price=ask,
            mid_price=mid,
            timestamp=timestamp,
            age_seconds=age_seconds,
        )

    def get_crypto_reference_price(
        self, symbol: str, *, max_staleness_seconds: float = 60.0
    ) -> float:
        """Convenience wrapper: mid/bid/ask price only, no quote provenance.

        Prefer :meth:`get_crypto_reference_quote` for anything that needs to
        reason about quote freshness or identity -- this exists only for
        callers that genuinely just want a number.
        """
        return self.get_crypto_reference_quote(
            symbol, max_staleness_seconds=max_staleness_seconds
        ).mid_price

    def place_crypto_stop_limit_order(
        self,
        symbol: str,
        action: str,
        qty: float,
        stop_price: float,
        limit_price: float,
        *,
        time_in_force: str = "gtc",
    ) -> dict[str, Any]:
        """Place a crypto GTC/IOC stop-limit order (BUY or SELL).

        General-purpose stop-limit wrapper that handles both sides -- unlike
        the protective-SELL-only :meth:`place_crypto_stop_limit` which
        carries E11 no-short / held-qty / Tier-1 safety gates. Primary
        consumer: the Stage-0 battery's stop-limit acceptance test (place a
        BUY stop-limit at unreachable prices, verify accepted, cancel
        immediately).

        Price validation: BUY stop-limit requires ``limit_price >= stop_price``
        (the limit caps how HIGH you pay after the stop triggers). SELL
        stop-limit requires ``limit_price <= stop_price`` (same as the
        protective path).
        """
        from alpaca.trading.enums import OrderSide
        from alpaca.trading.requests import StopLimitOrderRequest

        self._assert_account_active()
        action_u = action.upper()
        if action_u not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported action: {action!r}")
        if not is_crypto_pair(symbol):
            raise ValueError(
                f"place_crypto_stop_limit_order is crypto-only; got {symbol!r}"
            )
        tif = str(time_in_force or "gtc").strip().lower()
        violation = validate_crypto_order(
            order_type="stop_limit", time_in_force=tif, qty=float(qty),
        )
        if violation is not None:
            _, why = violation
            raise ValueError(
                f"crypto stop-limit order for {symbol} rejected at preflight: {why}"
            )
        stop_f = float(stop_price)
        limit_f = float(limit_price)
        if not (stop_f > 0.0 and limit_f > 0.0):
            raise ValueError(
                f"stop/limit prices must be positive: stop={stop_price!r}, "
                f"limit={limit_price!r}"
            )
        if action_u == "BUY" and limit_f < stop_f:
            raise ValueError(
                f"BUY stop-limit requires limit >= stop (the limit caps how "
                f"high you pay); got stop={stop_f}, limit={limit_f}"
            )
        if action_u == "SELL" and limit_f > stop_f:
            raise ValueError(
                f"SELL stop-limit requires limit <= stop; "
                f"got stop={stop_f}, limit={limit_f}"
            )
        spec = self._resolve_crypto_spec(symbol)
        submit_qty = snap_qty_to_increment(float(qty), spec.min_trade_increment)
        submit_stop = round_price_to_increment(stop_f, spec.price_increment)
        submit_limit = round_price_to_increment(limit_f, spec.price_increment)
        request = StopLimitOrderRequest(
            symbol=symbol,
            qty=submit_qty,
            side=OrderSide.BUY if action_u == "BUY" else OrderSide.SELL,
            time_in_force=self._crypto_tif_enum(tif),
            stop_price=submit_stop,
            limit_price=submit_limit,
        )
        order = self._require_client().submit_order(order_data=request)
        result = _order_to_dict(order)
        result.update({
            "action": action_u,
            "order_type": "stop_limit",
            "quantity": float(submit_qty),
            "requested_quantity": float(qty),
            "stop_price": float(submit_stop),
            "limit_price": float(submit_limit),
            "asset_class": ASSET_CLASS_CRYPTO,
            "time_in_force": tif,
            "skipped": False,
        })
        return result

    def cancel_order(self, order_id: str) -> bool:
        self._require_client().cancel_order_by_id(order_id)
        return True

    def get_order_state(self, order_id: str) -> dict[str, Any]:
        """Query the current state of an order by ID.

        Thin wrapper for the SDK's ``get_order_by_id`` that surfaces order
        status, filled_qty, and related fields without the battery module
        importing alpaca-py directly.  Primary consumer: the Stage-0
        battery's residual-exposure audit after probe-order cancellation
        (a confirmed cancel does not undo a fill that happened before the
        cancel took effect).
        """
        client = self._require_client()
        order = client.get_order_by_id(order_id)
        return _order_to_dict(order)

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
        status = _enum_value(getattr(account, "status", ""))
        if status and status != "active":
            raise RuntimeError(f"Alpaca account is not active: {status}")

    def _require_client(self) -> Any:
        if self._trading_client is None:
            raise RuntimeError("AlpacaBroker is not connected")
        return self._trading_client

    def _install_bounded_timeout_session(self) -> None:
        """Swap the trading client's HTTP session for the bounded-timeout
        ``requests.Session`` subclass (:func:`_bounded_timeout_session_class`).

        The replacement starts UNARMED (``default_timeout=None``), so it is
        behaviourally identical to the SDK's own ``requests.Session`` for every
        call -- including order submission -- until
        :meth:`_bounded_account_timeout` arms it for a preflight account read.
        Any headers the SDK configured are carried over. This never fails the
        broker: if the SDK's session cannot be read/replaced we leave it as-is
        (the calls just run unbounded, i.e. today's behaviour).
        """
        cls = _bounded_timeout_session_class()
        client = self._trading_client
        old = getattr(client, "_session", None)
        if isinstance(old, cls):
            return
        new = cls(default_timeout=None)
        if old is not None:
            try:
                new.headers.update(getattr(old, "headers", {}) or {})
            except Exception:  # noqa: BLE001 -- header copy is best-effort
                pass
        try:
            client._session = new
        except Exception:  # noqa: BLE001 -- never fail the broker over this
            pass

    @contextmanager
    def _bounded_account_timeout(self) -> Any:
        """Temporarily arm the bounded ``(connect, read)`` timeout on the
        trading client's HTTP session for the duration of a preflight account
        read (``connect()`` / ``get_account_value()``), then restore the prior
        state.

        Scoped deliberately: order-submission calls never execute inside this
        context, so their socket semantics are unchanged. If the SDK session is
        not the bounded subclass (SDK internals changed, or install failed),
        this is a no-op that runs the call unbounded -- a resilience
        optimisation must never itself become a new failure mode.
        """
        cls = _bounded_timeout_session_class()
        session = getattr(self._trading_client, "_session", None)
        if not isinstance(session, cls):
            yield
            return
        previous = session.default_timeout
        session.default_timeout = (self._connect_timeout, self._read_timeout)
        try:
            yield
        finally:
            session.default_timeout = previous


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
    side = _enum_value(getattr(order, "side", "")).upper()
    quantity = float(getattr(order, "qty", getattr(order, "quantity", 0.0)) or 0.0)
    filled_qty = float(getattr(order, "filled_qty", 0.0) or 0.0)
    filled_avg_price = float(getattr(order, "filled_avg_price", 0.0) or 0.0)
    return {
        "order_id": str(getattr(order, "id", "")),
        "status": _enum_value(getattr(order, "status", "")),
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
        # Broker-confirmed fields: extracted from the SDK Order object's
        # own attributes, NOT from the request or wrapper .update()
        # overrides.  Validators must check these — not the wrapper-set
        # request-echo fields like "time_in_force" or "asset_class" — to
        # detect broker disagreement with the submitted request.
        "confirmed_time_in_force": _enum_value(
            getattr(order, "time_in_force", "")
        ),
        "confirmed_order_type": _enum_value(
            getattr(order, "order_type", "")
        ),
        "confirmed_asset_class": _enum_value(
            getattr(order, "asset_class", "")
        ),
        "confirmed_qty": quantity,
        "confirmed_limit_price": float(
            getattr(order, "limit_price", 0.0) or 0.0
        ),
        "confirmed_stop_price": float(
            getattr(order, "stop_price", 0.0) or 0.0
        ),
    }


def _enum_value(raw: Any) -> str:
    """Extract the lowercase wire value of an alpaca-py SDK enum field.

    [VERIFIED alpaca-py 0.43.4] every ``Order`` enum field (``status``,
    ``side``, ``order_type``, ``time_in_force``, ``asset_class``, ...) is a
    ``(str, Enum)`` subclass whose plain ``str()`` produces
    ``"ClassName.MEMBER"`` (``Enum.__str__``), NOT the lowercase wire value —
    only ``.value`` gives that (e.g. ``str(OrderStatus.ACCEPTED) ==
    "OrderStatus.ACCEPTED"`` but ``OrderStatus.ACCEPTED.value == "accepted"``).
    Same extraction ``_order_matches_asset_class`` already uses for
    ``asset_class``. A plain string (as used by every test double in this
    module) has no ``.value`` attribute, so ``getattr(raw, "value", raw)``
    returns it unchanged — safe for both real SDK enums and fakes.
    """
    return str(getattr(raw, "value", raw) or "").strip().lower()


# Genuinely RESTING (live, triggerable) Alpaca order statuses. [VERIFIED
# alpaca-py 0.43.4 OrderStatus] the full enum is: new, partially_filled,
# filled, done_for_day, canceled, expired, replaced, pending_cancel,
# pending_replace, pending_review, accepted, pending_new,
# accepted_for_bidding, stopped, rejected, suspended, calculated, held.
#
# ``GetOrdersRequest(status=QueryOrderStatus.OPEN...)`` is a broker-side
# "not yet terminal" filter — it does NOT mean "genuinely resting". A
# stop-limit order mid cancel-then-replace reports pending_cancel /
# pending_new / pending_replace while Alpaca's query semantics still call it
# "open"; none of those sub-states is a stop the exchange will actually
# trigger on right now (Codex review 2026-07-12 finding 1), so none may
# count as protective coverage.
_RESTING_ORDER_STATUSES = frozenset({"new", "accepted", "held"})
_TERMINAL_CANCELED_STATUS = "canceled"
_TERMINAL_NON_CANCEL_STATUSES = frozenset({
    "filled", "partially_filled", "rejected", "expired", "replaced",
    "done_for_day", "stopped", "suspended", "calculated",
})


def _is_resting_order_status(status: Any) -> bool:
    """Whether ``status`` denotes a genuinely resting (live, triggerable)
    order — not a transitional ``pending_*`` sub-state."""
    normalized = _enum_value(status)
    if "pending" in normalized:
        return False
    return normalized in _RESTING_ORDER_STATUSES


def _is_stop_shaped_protective_candidate(order: dict[str, Any]) -> bool:
    """Order-shape checks for a protective stop, MINUS the resting-status
    requirement (Codex review 2026-07-12 finding 1: ``order_type``, ``side``,
    ``time_in_force``, positive ``stop_price``/``limit_price``).

    Used to detect the "non_resting_ignored" case in
    :meth:`AlpacaBroker.check_crypto_stop_coverage` — a real
    protective-stop-shaped order exists for the symbol, it just is not
    resting right now (e.g. mid cancel/replace).
    """
    if str(order.get("order_type", "")).strip().lower() != "stop_limit":
        return False
    if str(order.get("side", "")).strip().upper() != "SELL":
        return False
    if str(order.get("time_in_force", "")).strip().lower() != "gtc":
        return False
    if float(order.get("stop_price", 0.0) or 0.0) <= 0.0:
        return False
    if float(order.get("limit_price", 0.0) or 0.0) <= 0.0:
        return False
    return True


def _is_qualifying_protective_stop(order: dict[str, Any]) -> bool:
    """Whether ``order`` (a :meth:`AlpacaBroker.get_open_orders_detailed`
    row) qualifies as protective coverage: every
    :func:`_is_stop_shaped_protective_candidate` shape check, PLUS a
    genuinely resting broker status (Codex review 2026-07-12 finding 1)."""
    if not _is_stop_shaped_protective_candidate(order):
        return False
    return _is_resting_order_status(order.get("status", ""))
