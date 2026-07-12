"""Crypto order semantics (crypto RFC D-C1 execution slice) — per-gap
regression tests + equity byte-identity pins.

Layout mirrors the RFC §2.1 gap table:
  E1/E2  crypto TIF policy (GTC/IOC only, DAY rejected) + order-shape matrix
  E3     reconciliation asset-class parameter (crypto never silently invisible)
  E4     fee schedule + fee-aware sizing / paper fills / reserved_cash
  E5/E6  no fractionable gate, min_trade_increment grid, min_order_size
  E7     per-asset price_increment rounding
  E8     crypto GTC stop-limit path (native fractional qty)
  E9     DAY-expiry sweep exempts crypto orders
  E10    market-hours gates bypass for crypto (always-open)
  E11    explicit crypto no-short assertion

Every crypto behavior is opt-in (pair-form symbol / explicit parameter);
the equity pins assert the legacy paths are byte-identical.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from renquant_execution import preopen_cancel_gate as gate
from renquant_execution.alpaca_broker import AlpacaBroker
from renquant_execution.alpaca_broker_port import (
    AlpacaBrokerPort,
    BrokerPortContractError,
)
from renquant_execution.broker import (
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_EQUITY,
    BELOW_MIN_ORDER_SIZE_STATUS,
    CRYPTO_NO_SHORT_STATUS,
    CRYPTO_SPEC_LOOKUP_FAILED_STATUS,
    INVALID_CRYPTO_ORDER_STATUS,
    NO_SUBMIT_STATUSES,
    PRECISION_EXCEEDS_9DP_STATUS,
    is_no_submit_status,
    validate_fractional_order,
)
from renquant_execution.crypto import (
    CRYPTO_ORDER_TYPES,
    CRYPTO_TIME_IN_FORCES,
    CryptoAssetSpec,
    CryptoFeeSchedule,
    assert_crypto_no_short,
    classify_asset_class,
    crypto_no_short_violation,
    is_crypto_pair,
    round_price_to_increment,
    snap_qty_to_increment,
    validate_crypto_order,
)
from renquant_execution.order_math import (
    cap_affordable_qty,
    cap_affordable_qty_crypto,
)
from renquant_execution.order_state_machine import (
    ChildOrder,
    OrderStateBook,
    resolve_day_expiry,
)
from renquant_execution.paper_broker import PaperBroker
from renquant_execution.readonly_broker import ReadOnlyBrokerWrapper

BTC = "BTC/USD"
BTC_SPEC = CryptoAssetSpec(
    symbol=BTC,
    min_order_size=0.0001,
    min_trade_increment=0.0001,
    price_increment=0.01,
)


# ── asset-class classification ───────────────────────────────────────────────


def test_is_crypto_pair_slash_form_only() -> None:
    assert is_crypto_pair("BTC/USD") is True
    assert is_crypto_pair("eth/usd") is True
    assert is_crypto_pair("AAPL") is False
    assert is_crypto_pair("BRK.B") is False
    assert is_crypto_pair("") is False
    assert is_crypto_pair(None) is False


def test_classify_asset_class_inference_and_explicit() -> None:
    assert classify_asset_class("BTC/USD") == ASSET_CLASS_CRYPTO
    assert classify_asset_class("AAPL") == ASSET_CLASS_EQUITY
    assert classify_asset_class("BTC/USD", "crypto") == ASSET_CLASS_CRYPTO
    assert classify_asset_class("AAPL", "us_equity") == ASSET_CLASS_EQUITY


def test_classify_asset_class_contradiction_fails_loud() -> None:
    with pytest.raises(ValueError, match="contradicts"):
        classify_asset_class("BTC/USD", "us_equity")
    with pytest.raises(ValueError, match="contradicts"):
        classify_asset_class("AAPL", "crypto")
    with pytest.raises(ValueError, match="unsupported asset_class"):
        classify_asset_class("AAPL", "options")


# ── E1/E2: crypto TIF policy + order-shape matrix ────────────────────────────


@pytest.mark.parametrize("order_type", sorted(CRYPTO_ORDER_TYPES))
@pytest.mark.parametrize("tif", sorted(CRYPTO_TIME_IN_FORCES))
def test_crypto_order_shape_matrix_accepts_gtc_ioc(order_type: str, tif: str) -> None:
    """market/limit/stop_limit × GTC/IOC are all submittable crypto shapes."""
    assert validate_crypto_order(order_type=order_type, time_in_force=tif, qty=0.5) is None


@pytest.mark.parametrize("order_type", sorted(CRYPTO_ORDER_TYPES))
def test_crypto_order_shape_matrix_rejects_day(order_type: str) -> None:
    """DAY — the equity fractional pin — is rejected for every crypto shape."""
    violation = validate_crypto_order(order_type=order_type, time_in_force="day", qty=0.5)
    assert violation is not None
    status, reason = violation
    assert status == INVALID_CRYPTO_ORDER_STATUS
    assert "GTC|IOC" in reason


@pytest.mark.parametrize("order_type", ["stop", "trailing_stop", "market_on_open", ""])
def test_crypto_rejects_order_types_outside_sdk_matrix(order_type: str) -> None:
    violation = validate_crypto_order(order_type=order_type, time_in_force="gtc", qty=0.5)
    assert violation is not None
    assert violation[0] == INVALID_CRYPTO_ORDER_STATUS


def test_crypto_validator_qty_notional_and_grid_rules() -> None:
    both = validate_crypto_order(
        order_type="market", time_in_force="gtc", qty=1.0, notional=10.0
    )
    assert both is not None and both[0] == INVALID_CRYPTO_ORDER_STATUS
    neither = validate_crypto_order(order_type="market", time_in_force="gtc")
    assert neither is not None and neither[0] == INVALID_CRYPTO_ORDER_STATUS
    negative = validate_crypto_order(order_type="market", time_in_force="ioc", qty=-1.0)
    assert negative is not None and negative[0] == INVALID_CRYPTO_ORDER_STATUS
    too_fine = validate_crypto_order(
        order_type="market", time_in_force="ioc", qty=0.1234567891
    )
    assert too_fine is not None and too_fine[0] == PRECISION_EXCEEDS_9DP_STATUS


def test_crypto_no_submit_statuses_are_recognized() -> None:
    for status in (
        INVALID_CRYPTO_ORDER_STATUS,
        CRYPTO_NO_SHORT_STATUS,
        BELOW_MIN_ORDER_SIZE_STATUS,
        CRYPTO_SPEC_LOOKUP_FAILED_STATUS,
    ):
        assert status in NO_SUBMIT_STATUSES
        assert is_no_submit_status(status) is True


def test_equity_fractional_validator_byte_identity_pin() -> None:
    """E1/E2 inverse pin: the EQUITY fractional validator still accepts DAY
    only and still rejects GTC/IOC — the crypto seam changed nothing."""
    assert validate_fractional_order(order_type="market", time_in_force="day", qty=1.5) is None
    for tif in ("gtc", "ioc"):
        violation = validate_fractional_order(order_type="market", time_in_force=tif, qty=1.5)
        assert violation is not None
        assert violation[0] == "rejected_invalid_fractional_order"
        assert "TIF=DAY only" in violation[1]


# ── fake Alpaca client (pattern from test_live_commit.py) ────────────────────


class _FakeAccount:
    status = "ACTIVE"
    portfolio_value = 10000.0
    cash = 10000.0
    non_marginable_buying_power = 10000.0


class _FakeCryptoClient:
    """Fake alpaca-py TradingClient with crypto assets + positions."""

    def __init__(
        self,
        assets: dict[str, object] | None = None,
        positions: dict[str, float] | None = None,
        orders: list[object] | None = None,
        order_status_sequence: dict[str, list[str]] | None = None,
    ) -> None:
        self._assets = assets or {}
        self._positions = positions or {}
        self._orders = orders or []
        # D-C5 replace/coverage tests: a registry of every order ever passed
        # in (keyed by id), independent of the "open orders" list above, so
        # get_order_by_id can still answer for an order that cancel_order_by_id
        # has already removed from the open-orders view (mirrors the real
        # Alpaca API: a canceled order still exists, it just stops being
        # "open"). `order_status_sequence` lets a test script exactly what
        # get_order_by_id reports on each successive poll for a given id — a
        # single-element list repeats forever (e.g. to simulate a
        # cancellation that never confirms within the timeout); omit an id
        # entirely to get the default "cancel_order_by_id marks it canceled
        # immediately" behavior.
        self._all_orders_by_id = {
            str(getattr(o, "id", "")): o for o in self._orders
        }
        self._order_status_sequence = {
            k: list(v) for k, v in (order_status_sequence or {}).items()
        }
        self.submitted: list[object] = []
        self.get_asset_calls: list[str] = []
        self.get_orders_requests: list[object] = []
        self.get_clock_calls = 0
        self.get_order_by_id_calls: list[str] = []
        self.cancel_order_calls: list[str] = []

    def get_account(self):
        return _FakeAccount()

    def get_asset(self, symbol: str):
        self.get_asset_calls.append(symbol)
        if symbol not in self._assets:
            raise RuntimeError(f"unknown asset {symbol}")
        return self._assets[symbol]

    def get_open_position(self, symbol: str):
        if symbol not in self._positions:
            raise RuntimeError("position does not exist")
        return SimpleNamespace(qty=self._positions[symbol], avg_entry_price=100.0)

    def get_orders(self, filter=None):  # noqa: A002 — SDK argument name
        self.get_orders_requests.append(filter)
        return list(self._orders)

    def get_all_positions(self):
        return [
            SimpleNamespace(
                symbol=sym,
                qty=qty,
                qty_available=qty,
                market_value=qty * 60000.0,
                avg_entry_price=60000.0,
                current_price=60000.0,
                unrealized_pl=0.0,
                unrealized_plpc=0.0,
                asset_class="crypto",
            )
            for sym, qty in self._positions.items()
            if qty > 0
        ]

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancel_order_calls.append(order_id)
        self._orders = [
            o for o in self._orders
            if str(getattr(o, "id", "")) != order_id
        ]
        # Default (no explicit order_status_sequence for this id): the
        # cancellation reaches a confirmed terminal CANCELED state right
        # away — the common happy-path test shape.
        if order_id not in self._order_status_sequence:
            order = self._all_orders_by_id.get(order_id)
            if order is not None:
                order.status = "canceled"

    def get_order_by_id(self, order_id: str):
        self.get_order_by_id_calls.append(order_id)
        sequence = self._order_status_sequence.get(order_id)
        if sequence:
            status = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return SimpleNamespace(id=order_id, status=status)
        order = self._all_orders_by_id.get(order_id)
        if order is not None:
            return order
        return SimpleNamespace(id=order_id, status="canceled")

    def get_clock(self):
        self.get_clock_calls += 1
        return SimpleNamespace(is_open=False)

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        qty = float(getattr(order_data, "qty", 0.0) or 0.0)
        side = str(getattr(getattr(order_data, "side", ""), "value", "") or "").upper()
        return SimpleNamespace(
            id=f"ord-{len(self.submitted)}",
            status="accepted",
            symbol=getattr(order_data, "symbol", ""),
            side=side or "BUY",
            qty=qty,
            filled_qty=0.0,
            filled_avg_price=0.0,
        )


def _crypto_asset(**overrides):
    fields = {
        "fractionable": False,  # E5 proof: the equity gate must NOT be consulted
        "min_order_size": 0.0001,
        "min_trade_increment": 0.0001,
        "price_increment": 0.01,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _broker(client, **kwargs) -> AlpacaBroker:
    broker = AlpacaBroker(paper=True, label="alpaca-crypto-test", **kwargs)
    broker._trading_client = client  # noqa: SLF001 — inject fake, skip connect()
    return broker


# ── E3: reconciliation asset-class parameter ─────────────────────────────────
#
# [VERIFIED alpaca-py 0.43.4] GetOrdersRequest has NO asset_class field — the
# legacy request-level "US_EQUITY filter" was silently dropped by pydantic
# and never reached the API. The E3 fix filters CLIENT-SIDE on the returned
# Order.asset_class, so these tests drive mixed-class order lists.


def _closed_order(symbol: str, asset_class: str, status: str = "filled"):
    from alpaca.trading.enums import AssetClass

    return SimpleNamespace(
        id=f"ord-{symbol}",
        status=status,
        symbol=symbol,
        side="buy",
        qty=1.0,
        filled_qty=1.0,
        filled_avg_price=100.0,
        asset_class=(
            AssetClass.CRYPTO if asset_class == "crypto" else AssetClass.US_EQUITY
        ),
    )


def _mixed_orders():
    return [
        _closed_order("AAPL", "us_equity"),
        _closed_order("BTC/USD", "crypto"),
    ]


def test_get_filled_orders_default_stays_equity_only() -> None:
    client = _FakeCryptoClient(orders=_mixed_orders())
    broker = _broker(client)
    rows = broker.get_filled_orders()
    assert [r["symbol"] for r in rows] == ["AAPL"]


def test_get_filled_orders_crypto_mode_is_explicit() -> None:
    client = _FakeCryptoClient(orders=_mixed_orders())
    broker = _broker(client)
    rows = broker.get_filled_orders(asset_class="crypto")
    assert [r["symbol"] for r in rows] == ["BTC/USD"]


def test_get_filled_orders_none_means_all_classes() -> None:
    client = _FakeCryptoClient(orders=_mixed_orders())
    broker = _broker(client)
    rows = broker.get_filled_orders(asset_class=None)
    assert [r["symbol"] for r in rows] == ["AAPL", "BTC/USD"]


def test_get_filled_orders_falls_back_to_symbol_form_when_class_missing() -> None:
    # Order.asset_class is Optional in the SDK (omitted for mleg orders):
    # an absent value classifies by pair-form symbol, never crashes.
    orders = [
        SimpleNamespace(
            id="o-1", status="filled", symbol="ETH/USD", side="buy",
            qty=1.0, filled_qty=1.0, filled_avg_price=100.0, asset_class=None,
        )
    ]
    broker = _broker(_FakeCryptoClient(orders=orders))
    assert broker.get_filled_orders() == []
    assert [r["symbol"] for r in broker.get_filled_orders(asset_class="crypto")] == [
        "ETH/USD"
    ]


def test_get_open_orders_asset_class_parameter() -> None:
    client = _FakeCryptoClient(
        orders=[
            _closed_order("AAPL", "us_equity", status="accepted"),
            _closed_order("BTC/USD", "crypto", status="accepted"),
        ]
    )
    broker = _broker(client)
    assert broker.get_open_orders() == {"AAPL"}  # default: equity only
    assert broker.get_open_orders(asset_class="crypto") == {"BTC/USD"}
    assert broker.get_open_orders(asset_class=None) == {"AAPL", "BTC/USD"}


def test_orders_asset_class_filter_rejects_garbage() -> None:
    client = _FakeCryptoClient()
    broker = _broker(client)
    with pytest.raises(ValueError, match="unsupported asset_class"):
        broker.get_filled_orders(asset_class="options")


def test_readonly_wrapper_forwards_asset_class_only_when_explicit() -> None:
    class _LegacyUnderlying:
        broker_name = "legacy"
        filled_calls: list[dict] = []

        # Legacy signature WITHOUT asset_class: the wrapper must not break it.
        def get_filled_orders(self, after=None):
            return [{"symbol": "MSFT"}]

        def get_open_orders(self):
            return {"MSFT"}

    wrapper = ReadOnlyBrokerWrapper.__new__(ReadOnlyBrokerWrapper)
    wrapper.underlying = _LegacyUnderlying()
    wrapper.broker_name = "alpaca_shadow"
    assert wrapper.get_filled_orders() == [{"symbol": "MSFT"}]
    assert wrapper.get_open_orders() == {"MSFT"}

    class _CryptoUnderlying:
        def get_filled_orders(self, after=None, asset_class="us_equity"):
            return [{"symbol": "BTC/USD", "asset_class": asset_class}]

        def get_open_orders(self, asset_class="us_equity"):
            return {f"{asset_class}"}

    wrapper.underlying = _CryptoUnderlying()
    assert wrapper.get_filled_orders(asset_class="crypto") == [
        {"symbol": "BTC/USD", "asset_class": "crypto"}
    ]
    assert wrapper.get_open_orders(asset_class="crypto") == {"crypto"}


# ── crypto market orders through place_order (E1/E2/E5/E6/E11) ───────────────


def test_crypto_market_buy_defaults_to_ioc_gtc_explicit() -> None:
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = _FakeCryptoClient(assets={BTC: _crypto_asset()})
    broker = _broker(client)
    result = broker.place_order(BTC, "BUY", 0.5)
    request = client.submitted[0]
    assert isinstance(request, MarketOrderRequest)
    assert request.time_in_force is TimeInForce.IOC
    assert request.qty == 0.5
    assert result["asset_class"] == ASSET_CLASS_CRYPTO
    assert result["time_in_force"] == "ioc"
    assert result["quantity"] == 0.5
    assert result["requested_quantity"] == 0.5

    broker.place_order(BTC, "BUY", 0.5, time_in_force="gtc")
    assert client.submitted[1].time_in_force is TimeInForce.GTC


def test_crypto_market_order_rejects_day_tif() -> None:
    client = _FakeCryptoClient(assets={BTC: _crypto_asset()})
    broker = _broker(client)
    with pytest.warns(RuntimeWarning, match="GTC|IOC"):
        result = broker.place_order(BTC, "BUY", 0.5, time_in_force="day")
    assert result["status"] == INVALID_CRYPTO_ORDER_STATUS
    assert result["skipped"] is True
    assert client.submitted == []


def test_crypto_qty_snaps_down_to_min_trade_increment_grid() -> None:
    """E6: no whole-share concept — sizing floors onto the per-pair grid."""
    client = _FakeCryptoClient(assets={BTC: _crypto_asset()})
    broker = _broker(client)
    result = broker.place_order(BTC, "BUY", 0.12345678)
    assert client.submitted[0].qty == 0.1234  # floored, never rounded up
    assert result["quantity"] == 0.1234
    assert result["requested_quantity"] == 0.12345678


def test_crypto_has_no_whole_share_snap() -> None:
    """E6: a fractional crypto qty like 1.5 submits as 1.5 (increment 0.5),
    never floored to a whole share."""
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset(min_order_size=0.5, min_trade_increment=0.5)}
    )
    broker = _broker(client)
    result = broker.place_order(BTC, "BUY", 1.5)
    assert client.submitted[0].qty == 1.5
    assert result["quantity"] == 1.5


def test_crypto_never_consults_the_fractionable_gate() -> None:
    """E5: the fake asset is flagged fractionable=False, yet the crypto order
    submits — the equity fractionable gate is semantically wrong for pairs
    and must not be consulted."""
    client = _FakeCryptoClient(assets={BTC: _crypto_asset(fractionable=False)})
    broker = _broker(client)
    result = broker.place_order(BTC, "BUY", 0.5)
    assert result["skipped"] is False
    assert broker._fractionable_cache == {}  # noqa: SLF001 — gate untouched


def test_crypto_below_min_order_size_is_rejected_not_rounded_up() -> None:
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset(min_order_size=0.001, min_trade_increment=0.001)}
    )
    broker = _broker(client)
    with pytest.warns(RuntimeWarning, match="min_order_size"):
        result = broker.place_order(BTC, "BUY", 0.0004)
    assert result["status"] == BELOW_MIN_ORDER_SIZE_STATUS
    assert result["skipped"] is True
    assert client.submitted == []


def test_crypto_spec_lookup_failure_fails_closed_and_is_not_cached() -> None:
    client = _FakeCryptoClient(assets={})  # every get_asset raises
    broker = _broker(client)
    with pytest.warns(RuntimeWarning, match="failing closed"):
        result = broker.place_order(BTC, "BUY", 0.5)
    assert result["status"] == CRYPTO_SPEC_LOOKUP_FAILED_STATUS
    assert client.submitted == []
    # Not cached: a later call retries the lookup.
    client._assets[BTC] = _crypto_asset()
    retry = broker.place_order(BTC, "BUY", 0.5)
    assert retry["skipped"] is False
    assert client.get_asset_calls == [BTC, BTC]


def test_pinned_crypto_spec_snapshot_takes_precedence_over_lookup() -> None:
    client = _FakeCryptoClient(assets={})  # lookup would fail
    broker = _broker(client, crypto_asset_specs={BTC: BTC_SPEC})
    result = broker.place_order(BTC, "BUY", 0.5)
    assert result["skipped"] is False
    assert client.get_asset_calls == []  # pinned snapshot, no round-trip


def test_crypto_sell_beyond_held_is_no_short_rejected() -> None:
    """E11: crypto sell qty <= held qty, asserted before submit."""
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()}, positions={BTC: 0.3}
    )
    broker = _broker(client)
    with pytest.warns(RuntimeWarning, match="long-only"):
        result = broker.place_order(BTC, "SELL", 0.5)
    assert result["status"] == CRYPTO_NO_SHORT_STATUS
    assert client.submitted == []

    covered = broker.place_order(BTC, "SELL", 0.3)
    assert covered["skipped"] is False
    assert client.submitted[0].qty == 0.3


def test_crypto_sell_with_no_position_is_no_short_rejected() -> None:
    client = _FakeCryptoClient(assets={BTC: _crypto_asset()})
    broker = _broker(client)
    with pytest.warns(RuntimeWarning, match="long-only"):
        result = broker.place_order(BTC, "SELL", 0.1)
    assert result["status"] == CRYPTO_NO_SHORT_STATUS


def test_no_short_helpers() -> None:
    assert crypto_no_short_violation(0.5, 1.0) is None
    assert crypto_no_short_violation(0.5, 0.5) is None
    assert crypto_no_short_violation(0.5, 0.4) is not None
    assert crypto_no_short_violation(-1.0, 5.0) is not None
    assert_crypto_no_short(0.5, 1.0, symbol=BTC)
    with pytest.raises(ValueError, match="long-only"):
        assert_crypto_no_short(2.0, 1.0, symbol=BTC)


def test_place_notional_order_refuses_crypto_pairs() -> None:
    client = _FakeCryptoClient(assets={BTC: _crypto_asset()})
    broker = _broker(client)
    with pytest.raises(ValueError, match="notional crypto orders are not supported"):
        broker.place_notional_order(BTC, "BUY", 100.0)
    assert client.submitted == []


# ── equity byte-identity pins for place_order ────────────────────────────────


def test_equity_place_order_request_shape_unchanged() -> None:
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = _FakeCryptoClient(assets={"AAPL": SimpleNamespace(fractionable=True)})
    broker = _broker(client)
    result = broker.place_order("AAPL", "BUY", 3.0000000001)
    request = client.submitted[0]
    assert isinstance(request, MarketOrderRequest)
    assert request.time_in_force is TimeInForce.DAY  # equity DAY pin
    assert request.qty == 3.0  # whole-share eps snap still applies to equity
    assert "asset_class" not in result  # equity result shape unchanged
    assert "time_in_force" not in result


def test_equity_place_order_rejects_non_day_tif() -> None:
    client = _FakeCryptoClient()
    broker = _broker(client)
    with pytest.raises(ValueError, match="TIF=DAY only"):
        broker.place_order("AAPL", "BUY", 1, time_in_force="gtc")
    assert client.submitted == []


# ── E7: price increment rounding ─────────────────────────────────────────────


def test_snap_qty_to_increment_floor_semantics() -> None:
    assert snap_qty_to_increment(0.12345678, 0.0001) == 0.1234
    assert snap_qty_to_increment(1.5, 0.5) == 1.5
    assert snap_qty_to_increment(0.30000000000000004, 0.0001) == 0.3
    assert snap_qty_to_increment(0.00009, 0.0001) == 0.0
    with pytest.raises(ValueError):
        snap_qty_to_increment(1.0, 0.0)
    with pytest.raises(ValueError):
        snap_qty_to_increment(-1.0, 0.1)


def test_round_price_to_increment_nearest_grid() -> None:
    assert round_price_to_increment(60000.123, 0.01) == 60000.12
    assert round_price_to_increment(60000.126, 0.01) == 60000.13
    assert round_price_to_increment(0.123456, 0.000001) == 0.123456
    with pytest.raises(ValueError):
        round_price_to_increment(100.0, 0.0)


# ── E8: crypto GTC stop-limit protective path ────────────────────────────────


def test_place_crypto_stop_limit_builds_gtc_fractional_stop_limit() -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import StopLimitOrderRequest

    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()}, positions={BTC: 0.5}
    )
    broker = _broker(client)
    result = broker.place_crypto_stop_limit(BTC, 0.5, 60000.123, 59800.456)
    request = client.submitted[0]
    assert isinstance(request, StopLimitOrderRequest)
    assert request.time_in_force is TimeInForce.GTC
    assert request.side is OrderSide.SELL
    assert request.qty == 0.5  # native fractional qty — no whole-share gate
    assert request.stop_price == 60000.12  # E7 price_increment rounding
    assert request.limit_price == 59800.46
    assert result["asset_class"] == ASSET_CLASS_CRYPTO
    assert result["time_in_force"] == "gtc"
    assert result["stop_price"] == 60000.12
    assert result["limit_price"] == 59800.46


def test_place_crypto_stop_limit_docstring_carries_gap_through_honesty() -> None:
    """The RFC §5.1 gap-through / non-fill honesty language, verbatim."""
    normalized = " ".join((AlpacaBroker.place_crypto_stop_limit.__doc__ or "").split())
    assert "NOT an execution guarantee" in normalized
    assert (
        "a stop-LIMIT can gap through in a fast move without filling at all "
        "— the order rests, triggers, and then may not execute if the market "
        "gaps past the limit price before it can" in normalized
    )
    assert (
        "broker residency means the STOP ORDER survives machine death; it "
        "does not mean the position is guaranteed to exit at or near the "
        "stop price" in normalized
    )


def test_place_crypto_stop_limit_rejects_equity_symbols() -> None:
    client = _FakeCryptoClient()
    broker = _broker(client)
    with pytest.raises(ValueError, match="crypto-only"):
        broker.place_crypto_stop_limit("AAPL", 1.0, 100.0, 99.0)


def test_place_crypto_stop_limit_requires_limit_at_or_below_stop() -> None:
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()}, positions={BTC: 1.0}
    )
    broker = _broker(client)
    with pytest.raises(ValueError, match="limit <= stop"):
        broker.place_crypto_stop_limit(BTC, 0.5, 59000.0, 59500.0)


def test_place_crypto_stop_limit_enforces_no_short() -> None:
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()}, positions={BTC: 0.2}
    )
    broker = _broker(client)
    with pytest.raises(ValueError, match="long-only"):
        broker.place_crypto_stop_limit(BTC, 0.5, 60000.0, 59800.0)


def test_place_stop_order_refuses_crypto_and_directs_to_stop_limit() -> None:
    client = _FakeCryptoClient(positions={BTC: 1.0})
    broker = _broker(client)
    with pytest.raises(ValueError, match="place_crypto_stop_limit"):
        broker.place_stop_order(BTC, 1.0, 60000.0)


def test_supports_broker_side_stops_crypto_fractional_true_equity_pins() -> None:
    client = _FakeCryptoClient()
    broker = _broker(client)
    # Crypto: fractional broker-side protection exists (GTC stop_limit).
    assert broker.supports_broker_side_stops(BTC, 0.4) is True
    assert broker.supports_broker_side_stops(BTC, 2.0) is True
    # Equity pins unchanged (whole-share only).
    assert broker.supports_broker_side_stops("BLK", 0.4) is False
    assert broker.supports_broker_side_stops("AAPL", 3) is True
    assert broker.supports_broker_side_stops() is True


# ── E9: DAY-expiry sweep exempts crypto orders ───────────────────────────────


class _ExpiryPort:
    """Port whose order_status reports everything expired (equity close)."""

    def __init__(self) -> None:
        self.status_calls: list[str] = []

    def order_status(self, client_order_id: str):
        self.status_calls.append(client_order_id)
        return {"status": "expired", "filled_qty": 0.0}


def test_resolve_day_expiry_exempts_gtc_crypto_orders() -> None:
    book = OrderStateBook(account="alpaca_crypto", trading_day="2026-07-10")
    now = dt.datetime(2026, 7, 10, 12, tzinfo=dt.timezone.utc)

    equity = book.register_intent(
        symbol="AAPL", side="BUY", signal_version="s1", target_qty=3
    )
    book.submit_child(equity.parent_intent_id, qty=3, price=100.0, now=now)
    crypto = book.register_intent(
        symbol=BTC, side="BUY", signal_version="s1", target_qty=0.5
    )
    crypto_child = book.submit_child(
        crypto.parent_intent_id, qty=0.5, price=60000.0, now=now
    )

    port = _ExpiryPort()
    resolved = resolve_day_expiry(book, port)

    # The equity child expired at the close; the crypto child was never even
    # queried — a resting GTC crypto order is a legitimate overnight state.
    assert [c.child_order_id for c in resolved] == [
        equity.children[0].child_order_id
    ]
    assert crypto_child.is_open is True
    assert crypto_child.child_order_id not in port.status_calls
    assert equity.cum_expired == 3.0
    assert crypto.cum_expired == 0.0


def test_resolve_day_expiry_equity_behavior_unchanged_pin() -> None:
    book = OrderStateBook(account="alpaca", trading_day="2026-07-10")
    now = dt.datetime(2026, 7, 10, 12, tzinfo=dt.timezone.utc)
    parent = book.register_intent(
        symbol="MSFT", side="BUY", signal_version="s1", target_qty=2
    )
    book.submit_child(parent.parent_intent_id, qty=2, price=400.0, now=now)
    resolved = resolve_day_expiry(book, _ExpiryPort())
    assert len(resolved) == 1
    assert parent.cum_expired == 2.0


# ── E10: market-hours gates bypass for crypto ────────────────────────────────


def test_is_market_open_crypto_never_consults_the_clock() -> None:
    client = _FakeCryptoClient()
    broker = _broker(client)
    assert broker.is_market_open(BTC) is True
    assert client.get_clock_calls == 0  # 24/7: no broker round-trip at all
    # Equity default still consults the clock.
    assert broker.is_market_open() is False
    assert broker.is_market_open("AAPL") is False
    assert client.get_clock_calls == 2


def test_is_market_open_crypto_true_even_disconnected() -> None:
    broker = AlpacaBroker(paper=True)
    assert broker.is_market_open(BTC) is True  # would raise if it needed a client
    with pytest.raises(RuntimeError, match="not connected"):
        broker.is_market_open()


def _gate_order(symbol: str, side: str, order_id: str):
    return SimpleNamespace(
        symbol=symbol,
        order_type="OrderType.MARKET",
        side=side,
        qty="1",
        id=order_id,
        position_intent="buy_to_open",
    )


def test_preopen_cancel_gate_never_touches_crypto_orders(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("RENQUANT_PREOPEN_CANCEL_LEDGER", str(tmp_path / "ledger.jsonl"))
    client = MagicMock()
    client.get_orders.return_value = [
        _gate_order("BTC/USD", "buy", "c-1"),
        _gate_order("AAPL", "buy", "e-1"),
    ]
    metrics = {
        "source": "ES=F", "prior_close": 5000.0, "latest": 5150.0,
        "current_pct": 2.52 * 0.005, "sigma_60d": 0.005, "severity": 2.52,
        "n_obs": 100, "stale_minutes": 1.0,
    }
    with patch.object(gate, "compute_overnight_severity", return_value=metrics), \
            patch.object(gate, "post_ntfy_alert"):
        result = gate.cancel_stale_market_orders(
            threshold_sigma=2.0,
            dry_run=False,
            cancel_both_sides=True,  # even the cancel-everything mode
            trading_client_factory=lambda **_kw: client,
            orders_request_factory=lambda **kw: kw,
            open_status="open",
        )
    assert result["cancelled"] == ["AAPL"]
    cancelled_ids = [call.args[0] for call in client.cancel_order_by_id.call_args_list]
    assert cancelled_ids == ["e-1"]  # the crypto order was never considered


# ── E4: fee schedule + fee-aware order math / paper fills / reservations ─────


def test_crypto_fee_schedule_defaults_and_math() -> None:
    schedule = CryptoFeeSchedule()
    # Tier-0 defaults [GUESS: Stage-0 verifies] — config defaults, not truth.
    assert schedule.taker_bps == 25.0
    assert schedule.maker_bps == 15.0
    assert schedule.fee_usd(10_000.0) == 25.0
    assert schedule.fee_usd(10_000.0, liquidity="maker") == 15.0
    with pytest.raises(ValueError, match="taker.*maker|maker.*taker|liquidity"):
        schedule.fee_usd(100.0, liquidity="mystery")
    with pytest.raises(ValueError):
        CryptoFeeSchedule(taker_bps=-1.0)


def test_cap_affordable_qty_fee_awareness_and_equity_byte_identity() -> None:
    # Byte-identity pins: default fee_bps leaves values AND types unchanged.
    legacy_whole = cap_affordable_qty(100.0, 350.0)
    assert legacy_whole == 3 and isinstance(legacy_whole, int)
    assert cap_affordable_qty(100.0, 350.0, fee_bps=0.0) == 3
    legacy_frac = cap_affordable_qty(3.0, 100.0, fractional=True)
    assert legacy_frac == 33.333333
    assert cap_affordable_qty(3.0, 100.0, fractional=True, fee_bps=0.0) == 33.333333
    # Fee-aware: 25 bps taker shrinks the affordable qty so that
    # qty * price * 1.0025 <= cash.
    qty = cap_affordable_qty(100.0, 1000.0, fractional=True, fee_bps=25.0)
    assert qty == 9.975062
    assert qty * 100.0 * 1.0025 <= 1000.0
    with pytest.raises(ValueError, match="fee_bps"):
        cap_affordable_qty(100.0, 1000.0, fee_bps=-1.0)


def test_cap_affordable_qty_crypto_increment_grid_sizing() -> None:
    # 1000 / (60000 * 1.0025) = 0.0166251... -> floored to the 0.0001 grid.
    qty = cap_affordable_qty_crypto(
        60000.0, 1000.0,
        min_order_size=0.0001, min_trade_increment=0.0001, fee_bps=25.0,
    )
    assert qty == 0.0166
    assert qty * 60000.0 * 1.0025 <= 1000.0
    # Below min_order_size -> explicit 0.0 reject, never rounded up.
    assert cap_affordable_qty_crypto(
        60000.0, 5.0, min_order_size=0.0001, min_trade_increment=0.0001
    ) == 0.0
    assert cap_affordable_qty_crypto(
        60000.0, -1.0, min_order_size=0.0001, min_trade_increment=0.0001
    ) == 0.0
    with pytest.raises(ValueError):
        cap_affordable_qty_crypto(
            0.0, 100.0, min_order_size=0.0001, min_trade_increment=0.0001
        )


def test_paper_broker_crypto_fills_net_taker_fees() -> None:
    broker = PaperBroker(10_000.0, crypto_fee_schedule=CryptoFeeSchedule())
    broker.connect()
    broker.set_price(BTC, 100.0)

    buy = broker.place_order(BTC, "BUY", 10.0)  # notional 1000, fee 2.50
    assert buy["status"] == "filled"
    assert buy["fee"] == 2.50
    assert buy["asset_class"] == ASSET_CLASS_CRYPTO
    assert broker.get_cash() == pytest.approx(10_000.0 - 1000.0 - 2.50)

    sell = broker.place_order(BTC, "SELL", 10.0)  # notional 1000, fee 2.50
    assert sell["fee"] == 2.50
    assert broker.get_cash() == pytest.approx(10_000.0 - 5.0)  # 2 x 2.50 fees


def test_paper_broker_crypto_buy_must_afford_fill_plus_fee() -> None:
    broker = PaperBroker(1001.0, crypto_fee_schedule=CryptoFeeSchedule())
    broker.connect()
    broker.set_price(BTC, 100.0)
    # notional 1000 + fee 2.50 > 1001 -> rejected, not partially charged.
    result = broker.place_order(BTC, "BUY", 10.0)
    assert result["status"] == "rejected"
    assert broker.get_cash() == 1001.0
    assert broker.get_position(BTC) == 0.0


def test_paper_broker_equity_fills_byte_identical_with_schedule() -> None:
    """Equity byte-identity pin: the fee schedule changes NOTHING for a
    plain-ticker fill — same result dict shape, same cash arithmetic."""
    with_schedule = PaperBroker(10_000.0, crypto_fee_schedule=CryptoFeeSchedule())
    without = PaperBroker(10_000.0)
    for broker in (with_schedule, without):
        broker.connect()
        broker.set_price("AAPL", 200.0)
    result_a = with_schedule.place_order("AAPL", "BUY", 5)
    result_b = without.place_order("AAPL", "BUY", 5)
    assert result_a == result_b
    assert "fee" not in result_a
    assert with_schedule.get_cash() == without.get_cash() == 9_000.0


def test_reserved_cash_fee_awareness_and_equity_identity() -> None:
    book = OrderStateBook(account="alpaca_crypto", trading_day="2026-07-10")
    now = dt.datetime(2026, 7, 10, 12, tzinfo=dt.timezone.utc)

    equity = book.register_intent(
        symbol="AAPL", side="BUY", signal_version="s1", target_qty=10
    )
    book.submit_child(equity.parent_intent_id, qty=10, price=50.0, now=now)
    # Equity identity: no fee term, exact historical notional.
    assert book.reserved_cash() == 10 * 50.0

    crypto = book.register_intent(
        symbol=BTC, side="BUY", signal_version="s1", target_qty=0.5
    )
    book.submit_child(
        crypto.parent_intent_id, qty=0.5, price=60_000.0, now=now, fee_bps=25.0
    )
    expected_crypto = 0.5 * 60_000.0 * 1.0025
    assert book.reserved_cash() == pytest.approx(10 * 50.0 + expected_crypto)


def test_child_order_snapshot_round_trips_fee_bps_with_back_compat() -> None:
    now = dt.datetime(2026, 7, 10, 12, tzinfo=dt.timezone.utc)
    child = ChildOrder(
        child_order_id="pi-x:1",
        attempt_n=1,
        requested_qty=0.5,
        price=60_000.0,
        submitted_at=now,
        fee_bps=25.0,
    )
    row = child.to_snapshot()
    assert row["fee_bps"] == 25.0
    assert ChildOrder.from_snapshot(row).fee_bps == 25.0
    # Pre-crypto snapshots carry no fee_bps key: defaults to 0.0 (equity).
    legacy_row = {k: v for k, v in row.items() if k != "fee_bps"}
    assert ChildOrder.from_snapshot(legacy_row).fee_bps == 0.0


def test_submit_child_rejects_negative_fee_bps() -> None:
    book = OrderStateBook(account="alpaca_crypto", trading_day="2026-07-10")
    now = dt.datetime(2026, 7, 10, 12, tzinfo=dt.timezone.utc)
    parent = book.register_intent(
        symbol=BTC, side="BUY", signal_version="s1", target_qty=1.0
    )
    with pytest.raises(Exception, match="fee_bps"):
        book.submit_child(parent.parent_intent_id, qty=1.0, price=100.0, now=now, fee_bps=-5.0)


# ── crypto spec dataclass validation ─────────────────────────────────────────


def test_crypto_asset_spec_validation_and_from_asset() -> None:
    with pytest.raises(ValueError, match="min_trade_increment"):
        CryptoAssetSpec(
            symbol=BTC, min_order_size=0.1, min_trade_increment=0.0, price_increment=0.01
        )
    spec = CryptoAssetSpec.from_asset(BTC, _crypto_asset())
    assert spec.min_order_size == 0.0001
    assert spec.min_trade_increment == 0.0001
    assert spec.price_increment == 0.01
    with pytest.raises(ValueError, match="refusing to guess"):
        CryptoAssetSpec.from_asset(BTC, SimpleNamespace(fractionable=True))


# ── equity DAY port guard ────────────────────────────────────────────────────


def test_alpaca_broker_port_refuses_crypto_pairs() -> None:
    port = AlpacaBrokerPort(paper=True)
    with pytest.raises(BrokerPortContractError, match="GTC/IOC"):
        port.submit_order(
            client_order_id="pi-x:1", symbol=BTC, side="BUY", qty=0.5,
            limit_price=60_000.0,
        )


# ── D-C5: replace_crypto_stop_limit + check_crypto_stop_coverage ──────────


def test_replace_crypto_stop_limit_cancels_then_places() -> None:
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[SimpleNamespace(
            id="old-stop-1", status="accepted", symbol=BTC, side="SELL",
            qty=0.5, filled_qty=0.0, filled_avg_price=0.0,
            order_type="stop_limit", time_in_force="gtc",
            stop_price=59000.0, limit_price=58500.0,
        )],
    )
    broker = _broker(client)
    result = broker.replace_crypto_stop_limit(
        "old-stop-1", BTC, 0.5, 58000.0, 57500.0,
    )
    assert result["stop_price"] == 58000.0
    assert result["limit_price"] == 57500.0
    assert result["asset_class"] == ASSET_CLASS_CRYPTO
    assert len(client.submitted) == 1
    # Fix 3/4: cancellation is confirmed (client.get_order_by_id reports the
    # order as canceled) before the replacement is placed, and the result is
    # the new discriminated shape.
    assert client.get_order_by_id_calls == ["old-stop-1"]
    assert result["protected"] is True
    assert result["status"] == "replaced"
    assert result["old_order_id"] == "old-stop-1"
    assert result["new_order_id"] == result["order_id"]


def test_get_open_orders_detailed_returns_stop_prices() -> None:
    orders = [SimpleNamespace(
        id="ord-1", status="accepted", symbol=BTC, side="SELL",
        qty=0.3, filled_qty=0.0, filled_avg_price=0.0,
        order_type="stop_limit", time_in_force="gtc",
        stop_price=59000.0, limit_price=58500.0,
        created_at="2026-07-12", submitted_at="2026-07-12",
        filled_at=None, asset_class="crypto",
    )]
    client = _FakeCryptoClient(orders=orders)
    broker = _broker(client)
    detailed = broker.get_open_orders_detailed(asset_class=ASSET_CLASS_CRYPTO)
    assert len(detailed) == 1
    d = detailed[0]
    assert d["order_type"] == "stop_limit"
    assert d["stop_price"] == 59000.0
    assert d["limit_price"] == 58500.0
    assert d["time_in_force"] == "gtc"


def test_check_crypto_stop_coverage_all_covered() -> None:
    orders = [SimpleNamespace(
        id="stop-1", status="accepted", symbol=BTC, side="SELL",
        qty=0.5, filled_qty=0.0, filled_avg_price=0.0,
        order_type="stop_limit", time_in_force="gtc",
        stop_price=59000.0, limit_price=58500.0,
        created_at="", submitted_at="", filled_at=None,
        asset_class="crypto",
    )]
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=orders,
    )
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert violations == []


def test_check_crypto_stop_coverage_detects_unprotected_position() -> None:
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[],
    )
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert len(violations) == 1
    assert violations[0]["symbol"] == BTC
    assert violations[0]["held_qty"] == 0.5
    assert violations[0]["covered_qty"] == 0.0
    assert violations[0]["violation_kind"] == "uncovered"


def test_check_crypto_stop_coverage_partial_shortfall() -> None:
    orders = [SimpleNamespace(
        id="stop-1", status="accepted", symbol=BTC, side="SELL",
        qty=0.3, filled_qty=0.0, filled_avg_price=0.0,
        order_type="stop_limit", time_in_force="gtc",
        stop_price=59000.0, limit_price=58500.0,
        created_at="", submitted_at="", filled_at=None,
        asset_class="crypto",
    )]
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=orders,
    )
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert len(violations) == 1
    assert violations[0]["covered_qty"] == 0.3
    assert violations[0]["violation_kind"] == "partial"
    assert "shortfall" in violations[0]["reason"]


# ── Codex review 2026-07-12 fixes: strict coverage semantics ─────────────────


def _stop_order(
    order_id: str,
    *,
    qty: float,
    stop_price: float = 59000.0,
    limit_price: float = 58500.0,
    time_in_force: str = "gtc",
    status: str = "accepted",
    order_type: str = "stop_limit",
    side: str = "SELL",
):
    return SimpleNamespace(
        id=order_id, status=status, symbol=BTC, side=side,
        qty=qty, filled_qty=0.0, filled_avg_price=0.0,
        order_type=order_type, time_in_force=time_in_force,
        stop_price=stop_price, limit_price=limit_price,
        created_at="", submitted_at="", filled_at=None,
        asset_class="crypto",
    )


def test_check_crypto_stop_coverage_rejects_non_gtc_stop_as_coverage() -> None:
    """Finding 1: an IOC/DAY stop-limit is NOT counted as coverage."""
    for tif in ("day", "ioc"):
        orders = [_stop_order("stop-1", qty=1.0, time_in_force=tif)]
        client = _FakeCryptoClient(
            assets={BTC: _crypto_asset()}, positions={BTC: 0.5}, orders=orders,
        )
        broker = _broker(client)
        violations = broker.check_crypto_stop_coverage()
        assert len(violations) == 1, tif
        assert violations[0]["violation_kind"] == "uncovered"
        assert violations[0]["covered_qty"] == 0.0


def test_check_crypto_stop_coverage_duplicate_stops_fail_closed() -> None:
    """Finding 2: two independently-executable GTC stop-limit SELL orders
    for the same symbol are a "duplicate" violation, even though their
    summed quantity would exceed the held quantity — never treated as safe
    coverage by summing."""
    orders = [
        _stop_order("stop-1", qty=0.4),
        _stop_order("stop-2", qty=0.4),
    ]
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()}, positions={BTC: 0.5}, orders=orders,
    )
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert len(violations) == 1
    assert violations[0]["violation_kind"] == "duplicate"
    assert violations[0]["covered_qty"] == pytest.approx(0.8)  # informational only
    assert "duplicate" in violations[0]["reason"] or "ambiguous" in violations[0]["reason"]


def test_check_crypto_stop_coverage_excludes_pending_status_order() -> None:
    """A resting-looking order whose broker status is a transitional
    pending_* sub-state (Alpaca still reports it under
    QueryOrderStatus.OPEN) must be excluded from coverage, not counted."""
    orders = [_stop_order("stop-1", qty=1.0, status="pending_cancel")]
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()}, positions={BTC: 0.5}, orders=orders,
    )
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert len(violations) == 1
    assert violations[0]["violation_kind"] == "non_resting_ignored"
    assert violations[0]["covered_qty"] == 0.0


def test_check_crypto_stop_coverage_increment_boundary_just_inside_is_covered() -> None:
    """Finding 5: the tolerance is the pair's own min_trade_increment, not
    the equity QTY_INTEGRAL_EPS. A shortfall strictly SMALLER than the
    increment is still covered."""
    increment = 0.0001
    held = 0.5
    covered_qty = held - (increment / 2)  # shortfall 0.00005 < 0.0001 tol
    orders = [_stop_order("stop-1", qty=covered_qty)]
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset(min_order_size=increment, min_trade_increment=increment)},
        positions={BTC: held},
        orders=orders,
    )
    broker = _broker(client)
    assert broker.check_crypto_stop_coverage() == []


def test_check_crypto_stop_coverage_increment_boundary_just_outside_is_partial() -> None:
    """Symmetric case: a shortfall strictly LARGER than the pair's
    min_trade_increment is a partial-coverage violation."""
    increment = 0.0001
    held = 0.5
    covered_qty = held - (increment * 1.5)  # shortfall 0.00015 > 0.0001 tol
    orders = [_stop_order("stop-1", qty=covered_qty)]
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset(min_order_size=increment, min_trade_increment=increment)},
        positions={BTC: held},
        orders=orders,
    )
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert len(violations) == 1
    assert violations[0]["violation_kind"] == "partial"


def test_check_crypto_stop_coverage_fails_closed_on_spec_lookup_failure() -> None:
    """Finding 5 fail-closed clause: if the pair's spec lookup fails, the
    symbol is a violation — never silently falls back to the equity epsilon
    or gets skipped."""
    orders = [_stop_order("stop-1", qty=0.5)]
    client = _FakeCryptoClient(assets={}, positions={BTC: 0.5}, orders=orders)
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert len(violations) == 1
    assert violations[0]["violation_kind"] == "spec_lookup_failed"


def test_get_open_orders_detailed_normalizes_sdk_enum_like_fields() -> None:
    """A real alpaca-py SDK Order's enum fields stringify to
    "ClassName.MEMBER" via plain str() — only `.value` gives the wire value.
    get_open_orders_detailed must extract via `.value` (see `_enum_value`),
    not a naive str() cast, or every downstream qualifying-stop check would
    silently never match against real (non-test-double) broker responses."""

    class _FakeSdkEnum:
        def __init__(self, value: str) -> None:
            self.value = value

        def __str__(self) -> str:  # pragma: no cover - mirrors alpaca-py Enum.__str__
            return f"FakeEnum.{self.value.upper()}"

    order = SimpleNamespace(
        id="ord-1", status=_FakeSdkEnum("accepted"), symbol=BTC,
        side=_FakeSdkEnum("sell"), qty=0.5, filled_qty=0.0, filled_avg_price=0.0,
        order_type=_FakeSdkEnum("stop_limit"), time_in_force=_FakeSdkEnum("gtc"),
        stop_price=59000.0, limit_price=58500.0,
        created_at="", submitted_at="", filled_at=None, asset_class=_FakeSdkEnum("crypto"),
    )
    client = _FakeCryptoClient(orders=[order])
    broker = _broker(client)
    detailed = broker.get_open_orders_detailed(asset_class=ASSET_CLASS_CRYPTO)
    assert len(detailed) == 1
    d = detailed[0]
    assert d["status"] == "accepted"
    assert d["side"] == "SELL"
    assert d["order_type"] == "stop_limit"
    assert d["time_in_force"] == "gtc"

    # And check_crypto_stop_coverage correctly counts it as qualifying
    # coverage (it would NOT, pre-fix, since "fakeenum.stop_limit" !=
    # "stop_limit").
    client2 = _FakeCryptoClient(
        assets={BTC: _crypto_asset()}, positions={BTC: 0.5}, orders=[order],
    )
    broker2 = _broker(client2)
    assert broker2.check_crypto_stop_coverage() == []


def test_replace_crypto_stop_limit_cancel_unconfirmed_does_not_place_replacement() -> None:
    """Finding 3: if cancellation never reaches a confirmed terminal
    CANCELED state within the timeout, the replacement must NOT be placed
    (that could create two overlapping resting stops)."""
    old_order = _stop_order("old-stop-1", qty=0.5)
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[old_order],
        # A single-element sequence repeats forever: the cancellation is
        # perpetually stuck pending, never reaching a terminal state.
        order_status_sequence={"old-stop-1": ["pending_cancel"]},
    )
    broker = _broker(client)
    with pytest.warns(RuntimeWarning, match="cancel_unconfirmed|did not reach"):
        result = broker.replace_crypto_stop_limit(
            "old-stop-1", BTC, 0.5, 58000.0, 57500.0,
            timeout_seconds=0.05, poll_interval_seconds=0.01,
        )
    assert result["protected"] is False
    assert result["status"] == "cancel_unconfirmed"
    assert result["unprotected_reason"] == "cancel_unconfirmed"
    assert result["new_order_id"] is None
    assert result["old_order_id"] == "old-stop-1"
    assert client.submitted == []  # replacement was never attempted


def test_replace_crypto_stop_limit_unprotected_after_confirmed_cancel_when_placement_fails() -> None:
    """Finding 3 (most severe case): cancellation IS confirmed, but the
    replacement placement itself then fails — the position is genuinely
    unprotected (no resting stop at all)."""
    old_order = _stop_order("old-stop-2", qty=0.5)
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[old_order],
    )
    broker = _broker(client)
    client.submit_order = MagicMock(side_effect=RuntimeError("submit boom"))
    with pytest.warns(RuntimeWarning, match="unprotected_after_cancel|UNPROTECTED"):
        result = broker.replace_crypto_stop_limit(
            "old-stop-2", BTC, 0.5, 58000.0, 57500.0,
        )
    assert result["protected"] is False
    assert result["status"] == "unprotected_after_cancel"
    assert result["unprotected_reason"] == "replacement_failed_after_confirmed_cancel"
    assert result["new_order_id"] is None
    assert result["old_order_id"] == "old-stop-2"
    # The cancellation itself DID happen and WAS confirmed.
    assert client.cancel_order_calls == ["old-stop-2"]
    assert client.get_order_by_id_calls == ["old-stop-2"]


def test_replace_crypto_stop_limit_treats_missing_order_id_as_unprotected() -> None:
    """Codex round-2 review (2026-07-12T21:52:07Z): a non-throwing
    no-submit-shaped return from place_crypto_stop_limit (empty order_id)
    must be treated exactly like a raised exception -- never declare
    protected=True on an unvalidated return value, even though
    place_crypto_stop_limit's own current contract always raises rather
    than returning such a dict."""
    old_order = _stop_order("old-stop-3", qty=0.5)
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[old_order],
    )
    broker = _broker(client)
    broker.place_crypto_stop_limit = MagicMock(  # noqa: SLF001 -- test injection
        return_value={"order_id": "", "status": "accepted", "symbol": BTC}
    )
    with pytest.warns(RuntimeWarning, match="unprotected_after_cancel|UNPROTECTED"):
        result = broker.replace_crypto_stop_limit(
            "old-stop-3", BTC, 0.5, 58000.0, 57500.0,
        )
    assert result["protected"] is False
    assert result["status"] == "unprotected_after_cancel"
    assert result["unprotected_reason"] == "replacement_failed_after_confirmed_cancel"
    assert result["new_order_id"] is None


def test_replace_crypto_stop_limit_treats_non_resting_returned_status_as_unprotected() -> None:
    """Same finding: a returned order with a real order_id but a
    non-resting status (e.g. immediately rejected in the response body,
    never surfaced as an exception) must also be treated as unprotected."""
    old_order = _stop_order("old-stop-4", qty=0.5)
    client = _FakeCryptoClient(
        assets={BTC: _crypto_asset()},
        positions={BTC: 0.5},
        orders=[old_order],
    )
    broker = _broker(client)
    broker.place_crypto_stop_limit = MagicMock(  # noqa: SLF001 -- test injection
        return_value={"order_id": "new-rejected-1", "status": "rejected", "symbol": BTC}
    )
    with pytest.warns(RuntimeWarning, match="unprotected_after_cancel|UNPROTECTED"):
        result = broker.replace_crypto_stop_limit(
            "old-stop-4", BTC, 0.5, 58000.0, 57500.0,
        )
    assert result["protected"] is False
    assert result["status"] == "unprotected_after_cancel"
    assert result["unprotected_reason"] == "replacement_failed_after_confirmed_cancel"
    assert result["new_order_id"] is None


def test_check_crypto_stop_coverage_ignores_equity_positions() -> None:
    client = _FakeCryptoClient(
        positions={"AAPL": 10.0},
        orders=[],
    )
    client.get_all_positions = lambda: [
        SimpleNamespace(symbol="AAPL", qty=10.0, qty_available=10.0,
                        market_value=1000.0, avg_entry_price=100.0,
                        current_price=100.0, unrealized_pl=0.0,
                        unrealized_plpc=0.0, asset_class="us_equity"),
    ]
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert violations == []


def test_check_crypto_stop_coverage_no_positions() -> None:
    client = _FakeCryptoClient(positions={}, orders=[])
    broker = _broker(client)
    violations = broker.check_crypto_stop_coverage()
    assert violations == []
