from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from renquant_execution.alpaca_broker import _order_to_dict
from renquant_execution import (
    BELOW_MIN_NOTIONAL_STATUS,
    FRACTIONABLE_LOOKUP_FAILED_STATUS,
    INVALID_FRACTIONAL_ORDER_STATUS,
    NO_SUBMIT_STATUSES,
    NON_FRACTIONABLE_STATUS,
    PRECISION_EXCEEDS_9DP_STATUS,
    AlpacaBroker,
    BaseBroker,
    BrokerExecutionPipeline,
    ExecutionContext,
    ExecutionPipeline,
    PaperBroker,
    ReadOnlyBrokerWrapper,
    execution_payload,
    get_broker,
    is_no_submit_status,
    normalize_order_intent,
    validate_fractional_order,
    write_execution_payload,
)


def test_execution_pipeline_submits_via_injected_broker() -> None:
    calls = []

    def submitter(broker_name, intents, dry_run):
        calls.append((broker_name, dry_run, len(intents)))
        return [{"id": "dry-1", **intents[0]}]

    ctx = ExecutionContext(
        broker_name="paper",
        order_intents=[{"ticker": "AAPL", "action": "buy", "quantity": 1}],
        dry_run=True,
    )
    result = ExecutionPipeline(submitter).run(ctx)

    assert result.ok is True
    assert calls == [("paper", True, 1)]
    assert ctx.submitted_orders[0]["ticker"] == "AAPL"
    assert ctx.audit_rows == [
        {"broker": "paper", "dry_run": True, "n_intents": 1, "n_submitted": 1, "n_skipped": 0}
    ]


def test_execution_payload_is_native_bundle_ready(tmp_path) -> None:
    ctx = ExecutionContext(
        broker_name="paper",
        order_intents=[{"ticker": "AAPL", "action": "buy", "quantity": 1}],
        dry_run=True,
    )
    ExecutionPipeline(lambda _broker, intents, _dry: [{"id": "dry-1", **intents[0]}]).run(ctx)
    out = tmp_path / "execution.json"

    payload = execution_payload(ctx)
    written = write_execution_payload(ctx, out)

    assert payload["source"] == "renquant_execution.execution"
    assert payload["broker_name"] == "paper"
    assert payload["dry_run"] is True
    assert payload["order_intents"] == ctx.order_intents
    assert payload["submitted_orders"] == ctx.submitted_orders
    assert payload["execution_audit"] == ctx.audit_rows
    assert written == out
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_execution_pipeline_rejects_malformed_intent() -> None:
    ctx = ExecutionContext(broker_name="paper", order_intents=[{"ticker": "AAPL"}])

    with pytest.raises(ValueError, match="missing action"):
        ExecutionPipeline(lambda *_: []).run(ctx)


def test_normalize_order_intent_accepts_ticker_or_symbol() -> None:
    assert normalize_order_intent({"ticker": "AAPL", "action": "buy", "quantity": 2}) == {
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 2.0,
    }
    assert normalize_order_intent({"symbol": "MSFT", "action": "SELL", "qty": 1}) == {
        "symbol": "MSFT",
        "action": "SELL",
        "quantity": 1.0,
    }
    assert normalize_order_intent({"ticker": "NVDA", "action": "BUY", "shares": 3}) == {
        "symbol": "NVDA",
        "action": "BUY",
        "quantity": 3.0,
    }


def test_alpaca_order_to_dict_exposes_live_execution_aliases() -> None:
    order = SimpleNamespace(
        id="ord-1",
        status="partially_filled",
        symbol="AAPL",
        side="sell",
        qty="5",
        filled_qty="2",
        filled_avg_price="101.25",
        created_at="2026-06-09T16:00:00Z",
        submitted_at="2026-06-09T16:01:00Z",
        filled_at="",
    )

    payload = _order_to_dict(order)

    assert payload["order_id"] == "ord-1"
    assert payload["side"] == "SELL"
    assert payload["action"] == "SELL"
    assert payload["quantity"] == pytest.approx(5.0)
    assert payload["qty"] == pytest.approx(5.0)
    assert payload["filled_qty"] == pytest.approx(2.0)
    assert payload["filled_avg_price"] == pytest.approx(101.25)
    assert payload["avg_price"] == pytest.approx(101.25)
    assert payload["partial"] is True


def test_broker_execution_pipeline_dry_run_does_not_mutate_broker() -> None:
    broker = PaperBroker(initial_cash=1000)
    broker.connect()
    broker.set_price("AAPL", 100)

    ctx = ExecutionContext(
        broker_name="paper",
        order_intents=[{"ticker": "AAPL", "action": "buy", "quantity": 2}],
        dry_run=True,
    )
    BrokerExecutionPipeline(broker).run(ctx)

    assert ctx.submitted_orders == [{
        "order_id": "dry-1",
        "status": "dry_run",
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 2.0,
    }]
    assert broker.get_position("AAPL") == 0
    assert broker.get_cash() == pytest.approx(1000)


def test_broker_execution_pipeline_places_paper_order_when_not_dry_run() -> None:
    broker = PaperBroker(initial_cash=1000)
    broker.connect()
    broker.set_price("AAPL", 100)

    ctx = ExecutionContext(
        broker_name="paper",
        order_intents=[{"ticker": "AAPL", "action": "buy", "quantity": 2}],
        dry_run=False,
    )
    BrokerExecutionPipeline(broker).run(ctx)

    assert ctx.submitted_orders[0]["status"] == "filled"
    assert broker.get_position("AAPL") == pytest.approx(2)
    assert broker.get_cash() == pytest.approx(800)


class FakeBroker(BaseBroker):
    broker_name = "fake"

    def __init__(self) -> None:
        self.connected = False
        self.writes: list[tuple[str, str, float]] = []

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def get_position(self, symbol: str) -> float:
        return 3.0 if symbol == "AAPL" else 0.0

    def get_account_value(self) -> float:
        return 1234.0

    def get_cash(self) -> float:
        return 500.0

    def get_all_positions(self) -> list[dict]:
        return [{"symbol": "AAPL", "qty": 3.0}]

    def get_open_orders(self) -> set[str]:
        return {"MSFT"}

    def place_order(self, symbol: str, action: str, quantity: float) -> dict:
        self.writes.append((symbol, action, quantity))
        return {"order_id": "real"}

    def is_market_open(self) -> bool:
        return True


def test_readonly_broker_forwards_reads_and_swallows_writes() -> None:
    fake = FakeBroker()
    broker = ReadOnlyBrokerWrapper(fake)
    broker.connect()

    assert broker.broker_name == "alpaca_shadow"
    assert fake.connected is True
    assert broker.get_position("AAPL") == pytest.approx(3.0)
    assert broker.get_account_value() == pytest.approx(1234.0)
    assert broker.get_cash() == pytest.approx(500.0)
    assert broker.get_all_positions() == [{"symbol": "AAPL", "qty": 3.0}]
    assert broker.get_open_orders() == {"MSFT"}

    order = broker.place_order("AAPL", "buy", 2)
    assert order["shadow"] is True
    assert order["status"] == "shadow_ack"
    assert order["symbol"] == "AAPL"
    assert fake.writes == []
    assert broker.cancel_order("real-order") is True


def test_readonly_broker_forwards_unknown_read_attrs() -> None:
    broker = ReadOnlyBrokerWrapper(FakeBroker())

    assert broker.is_market_open() is True


def test_get_broker_paper_does_not_import_alpaca_sdk() -> None:
    import sys

    for name in list(sys.modules):
        if name.startswith("alpaca"):
            del sys.modules[name]

    broker = get_broker("paper", initial_cash=123)

    assert isinstance(broker, PaperBroker)
    assert broker.get_cash() == pytest.approx(123)
    assert not any(name.startswith("alpaca") for name in sys.modules)


def test_get_broker_readonly_alpaca_constructs_without_connecting() -> None:
    import sys

    for name in list(sys.modules):
        if name.startswith("alpaca"):
            del sys.modules[name]

    broker = get_broker("readonly-alpaca")

    assert isinstance(broker, ReadOnlyBrokerWrapper)
    assert isinstance(broker.underlying, AlpacaBroker)
    assert broker.broker_name == "alpaca_shadow"
    assert not any(name.startswith("alpaca") for name in sys.modules)


def test_alpaca_broker_names_are_explicit() -> None:
    assert AlpacaBroker(paper=True).broker_name == "alpaca-paper"
    assert AlpacaBroker(paper=False).broker_name == "alpaca"
    assert AlpacaBroker(paper=True, label="alpaca-paper").broker_name == "alpaca-paper"


def test_get_broker_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unsupported broker_type"):
        get_broker("mystery")


# ── Fractional-share fractionable guard (renquant-pipeline #35) ─────────────
#
# These exercise the live broker boundary directly; the end-to-end paths
# (fractional BUY -> broker result -> persistence quantity -> exit/stop, plus the
# lookup-failure and non-fractionable paths) are covered through
# execute_live_commit in tests/test_live_commit.py.


class _FakeAsset:
    def __init__(self, fractionable: bool) -> None:
        self.fractionable = fractionable


class _FakeAccount:
    status = "ACTIVE"
    portfolio_value = 10000.0
    cash = 10000.0
    non_marginable_buying_power = 10000.0


class _FakeAlpacaClient:
    """Minimal stand-in for alpaca-py TradingClient covering place_order.

    Records the real alpaca-py request objects passed to ``submit_order`` so the
    tests can assert the actual qty/side/tif/request-type shape, and echoes the
    request's own side/qty back on the confirmation.
    """

    def __init__(
        self,
        fractionable: dict[str, bool],
        *,
        fill_status: str = "accepted",
        fill: bool = False,
    ) -> None:
        self._fractionable = fractionable
        self._fill_status = fill_status
        self._fill = fill
        self.submitted: list[Any] = []
        self.get_asset_calls: list[str] = []

    def get_account(self):  # noqa: D401
        return _FakeAccount()

    def get_asset(self, symbol: str):
        self.get_asset_calls.append(symbol)
        if symbol not in self._fractionable:
            raise RuntimeError(f"unknown asset {symbol}")
        return _FakeAsset(self._fractionable[symbol])

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        qty = float(getattr(order_data, "qty", 0.0) or 0.0)
        side = str(getattr(getattr(order_data, "side", ""), "value", "") or "").upper()
        return SimpleNamespace(
            id="ord-1",
            status=self._fill_status,
            symbol=getattr(order_data, "symbol", ""),
            side=side or "BUY",
            qty=qty,
            filled_qty=qty if self._fill else 0.0,
            filled_avg_price=101.0 if self._fill else 0.0,
        )


def _broker_with_client(client: _FakeAlpacaClient) -> AlpacaBroker:
    broker = AlpacaBroker(paper=True)
    broker._trading_client = client  # noqa: SLF001 — inject fake, skip connect()
    return broker


def test_fractionable_symbol_passes_fractional_qty_through() -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = _FakeAlpacaClient({"BLK": True})
    broker = _broker_with_client(client)

    result = broker.place_order("BLK", "buy", 0.435578)

    assert len(client.submitted) == 1
    req = client.submitted[0]
    # Real request-shape assertions: MARKET + DAY + correct side, fractional qty.
    assert isinstance(req, MarketOrderRequest)
    assert req.qty == 0.435578
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.DAY
    assert result["quantity"] == 0.435578
    assert result["requested_quantity"] == 0.435578
    assert result["skipped"] is False
    # fractionable lookup is cached — a second order does not re-fetch the asset.
    broker.place_order("BLK", "buy", 0.2)
    assert client.get_asset_calls == ["BLK"]


def test_non_fractionable_fractional_qty_is_rejected_not_floored() -> None:
    client = _FakeAlpacaClient({"GS": False})
    broker = _broker_with_client(client)

    with pytest.warns(RuntimeWarning):
        result = broker.place_order("GS", "buy", 2.7)

    # Never floored to 2.0 and never submitted — explicit rejection instead.
    assert client.submitted == []
    assert result["status"] == "rejected_non_fractionable"
    assert result["skipped"] is True
    assert result["quantity"] == 0.0  # submitted qty
    assert result["requested_quantity"] == 2.7  # intent preserved


def test_non_fractionable_sub_one_share_is_rejected_not_submitted() -> None:
    client = _FakeAlpacaClient({"GS": False})
    broker = _broker_with_client(client)

    with pytest.warns(RuntimeWarning):
        result = broker.place_order("GS", "buy", 0.4)

    assert client.submitted == []  # never reaches the broker
    assert result["status"] == "rejected_non_fractionable"
    assert result["quantity"] == 0.0
    assert result["requested_quantity"] == 0.4


def test_non_fractionable_sell_is_rejected_preserving_qty_no_residual_floor() -> None:
    # The reviewer's SELL 1.9 -> 1.0 residual-exposure case: a fractional SELL on
    # a non-fractionable asset must NOT be floored (which would strand 0.9
    # shares); it is rejected explicitly with the requested qty preserved.
    client = _FakeAlpacaClient({"GS": False})
    broker = _broker_with_client(client)

    with pytest.warns(RuntimeWarning):
        result = broker.place_order("GS", "sell", 1.9)

    assert client.submitted == []
    assert result["status"] == "rejected_non_fractionable"
    assert result["action"] == "SELL"
    assert result["quantity"] == 0.0
    assert result["requested_quantity"] == 1.9


def test_whole_share_qty_skips_fractionable_lookup_entirely() -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = _FakeAlpacaClient({})  # get_asset would raise if called
    broker = _broker_with_client(client)

    result = broker.place_order("AAPL", "sell", 3)

    assert client.get_asset_calls == []  # integral qty → no lookup
    req = client.submitted[0]
    assert isinstance(req, MarketOrderRequest)
    assert req.qty == 3.0
    assert req.side == OrderSide.SELL
    assert req.time_in_force == TimeInForce.DAY
    assert result["quantity"] == 3.0


def test_asset_lookup_failure_fails_closed_and_is_not_cached() -> None:
    # get_asset raises (transient/unknown) → fail closed: nothing submitted, and
    # the failure is NOT cached as non-fractionable, so a retry re-queries.
    client = _FakeAlpacaClient({})
    broker = _broker_with_client(client)

    with pytest.warns(RuntimeWarning):
        result = broker.place_order("ZZZZ", "buy", 1.9)

    assert client.submitted == []
    assert result["status"] == "rejected_fractionable_lookup_failed"
    assert result["skipped"] is True
    assert result["quantity"] == 0.0
    assert result["requested_quantity"] == 1.9
    assert client.get_asset_calls == ["ZZZZ"]

    # Transient failure is not cached: a second fractional attempt re-queries.
    with pytest.warns(RuntimeWarning):
        broker.place_order("ZZZZ", "buy", 1.9)
    assert client.get_asset_calls == ["ZZZZ", "ZZZZ"]


# ── Fractional vs broker-side stop policy (review point 2) ──────────────────


def test_supports_broker_side_stops_false_for_fractional_quantity() -> None:
    broker = _broker_with_client(_FakeAlpacaClient({}))

    # Legacy no-arg call and whole-share quantities keep broker-side stops.
    assert broker.supports_broker_side_stops() is True
    assert broker.supports_broker_side_stops("AAPL", 3) is True
    # A fractional position cannot get a broker-side (GTC) stop → software stop.
    assert broker.supports_broker_side_stops("BLK", 0.4) is False


def test_place_stop_order_rejects_fractional_qty_fail_closed() -> None:
    broker = _broker_with_client(_FakeAlpacaClient({}))

    with pytest.raises(ValueError, match="whole-share"):
        broker.place_stop_order("BLK", 0.4, 900.0)


def test_place_stop_order_submits_gtc_for_whole_share() -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import StopOrderRequest

    client = _FakeAlpacaClient({})
    broker = _broker_with_client(client)

    result = broker.place_stop_order("AAPL", 3, 150.0)

    req = client.submitted[0]
    assert isinstance(req, StopOrderRequest)
    assert req.qty == 3
    assert req.side == OrderSide.SELL
    assert req.time_in_force == TimeInForce.GTC
    assert req.stop_price == 150.0
    assert result["stop_price"] == 150.0


# ── S-FRAC stage 1: notional orders, 9dp grid, rule vocabulary, gate probe ──


def _client_and_broker(fractionable: dict[str, bool]) -> tuple[_FakeAlpacaClient, AlpacaBroker]:
    client = _FakeAlpacaClient(fractionable)
    return client, _broker_with_client(client)


def test_place_notional_order_submits_market_day_notional_shape() -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client, broker = _client_and_broker({"BLK": True})

    result = broker.place_notional_order("BLK", "buy", 324.17)

    assert len(client.submitted) == 1
    req = client.submitted[0]
    # Design SS4: EITHER qty OR notional — the notional shape carries no qty.
    assert isinstance(req, MarketOrderRequest)
    assert req.notional == 324.17
    assert req.qty is None
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.DAY
    assert result["requested_notional"] == 324.17
    assert result["notional"] == 324.17
    assert result["skipped"] is False
    # Notional orders are fractional by construction: fractionable confirmed.
    assert client.get_asset_calls == ["BLK"]


def test_place_notional_order_below_min_dollar_is_no_submit() -> None:
    client, broker = _client_and_broker({"BLK": True})

    with pytest.warns(RuntimeWarning):
        result = broker.place_notional_order("BLK", "buy", 0.42)

    assert client.submitted == []
    assert client.get_asset_calls == []  # rule preflight runs before lookup
    assert result["status"] == BELOW_MIN_NOTIONAL_STATUS
    assert result["skipped"] is True
    assert result["requested_notional"] == 0.42
    assert result["notional"] == 0.0  # nothing was sent


def test_place_notional_order_9dp_grid_is_no_submit() -> None:
    client, broker = _client_and_broker({"BLK": True})

    with pytest.warns(RuntimeWarning):
        result = broker.place_notional_order("BLK", "buy", 5.00000000049)

    assert client.submitted == []
    assert result["status"] == PRECISION_EXCEEDS_9DP_STATUS
    assert result["skipped"] is True


def test_place_notional_order_non_fractionable_and_lookup_failure_fail_closed() -> None:
    client, broker = _client_and_broker({"GS": False})

    with pytest.warns(RuntimeWarning):
        rejected = broker.place_notional_order("GS", "buy", 25.0)
    assert rejected["status"] == NON_FRACTIONABLE_STATUS
    assert rejected["skipped"] is True

    with pytest.warns(RuntimeWarning):
        failed = broker.place_notional_order("ZZZZ", "buy", 25.0)  # lookup raises
    assert failed["status"] == FRACTIONABLE_LOOKUP_FAILED_STATUS
    assert failed["skipped"] is True
    assert client.submitted == []


def test_place_order_fractional_qty_beyond_9dp_grid_is_no_submit() -> None:
    client, broker = _client_and_broker({"BLK": True})

    with pytest.warns(RuntimeWarning):
        result = broker.place_order("BLK", "buy", 0.1234567891)  # 10dp

    assert client.submitted == []
    assert client.get_asset_calls == []  # rejected before any lookup
    assert result["status"] == PRECISION_EXCEEDS_9DP_STATUS
    assert result["quantity"] == 0.0
    assert result["requested_quantity"] == 0.1234567891


def test_place_order_snaps_eps_integral_noise_to_exact_whole_share() -> None:
    # Stage-0 epsilon discipline on the SUBMIT side: 3.0000000001 is broker
    # float noise on a whole share; submitting it raw would read as a >9dp
    # fractional qty. It snaps to exactly 3.0 (and skips the asset lookup).
    client, broker = _client_and_broker({})

    result = broker.place_order("AAPL", "sell", 3.0000000001)

    assert client.get_asset_calls == []
    assert client.submitted[0].qty == 3.0
    assert result["quantity"] == 3.0
    assert result["requested_quantity"] == 3.0000000001


def test_validate_fractional_order_rule_matrix() -> None:
    ok = dict(order_type="market", time_in_force="day")
    # The pinned Alpaca rules (design SS4): market/limit/stop/stop_limit, DAY.
    for order_type in ("market", "limit", "stop", "stop_limit"):
        assert validate_fractional_order(
            order_type=order_type, time_in_force="day", qty=0.341052
        ) is None
    assert validate_fractional_order(**ok, notional=1.0) is None
    assert validate_fractional_order(**ok, qty=0.123456789) is None  # 9dp ok

    def status_of(**kw):
        violation = validate_fractional_order(**kw)
        assert violation is not None
        status, reason = violation
        assert reason  # a reject reason is always surfaced, never silent
        assert is_no_submit_status(status)
        return status

    # Exactly one of qty | notional (both/neither = broker HTTP 400).
    assert status_of(**ok) == INVALID_FRACTIONAL_ORDER_STATUS
    assert status_of(**ok, qty=1.5, notional=10.0) == INVALID_FRACTIONAL_ORDER_STATUS
    # TIF=DAY only — GTC is the Z9 dead-box TIF and is never fractional.
    assert status_of(
        order_type="market", time_in_force="gtc", qty=0.5
    ) == INVALID_FRACTIONAL_ORDER_STATUS
    # Unsupported order type.
    assert status_of(
        order_type="trailing_stop", time_in_force="day", qty=0.5
    ) == INVALID_FRACTIONAL_ORDER_STATUS
    # Finite/positive.
    assert status_of(**ok, qty=-0.5) == INVALID_FRACTIONAL_ORDER_STATUS
    assert status_of(**ok, notional=float("nan")) == INVALID_FRACTIONAL_ORDER_STATUS
    # 9dp grid.
    assert status_of(**ok, qty=0.1234567891) == PRECISION_EXCEEDS_9DP_STATUS
    # $1 broker minimum (notional only).
    assert status_of(**ok, notional=0.99) == BELOW_MIN_NOTIONAL_STATUS


def test_stage0_capability_gate_probe_surface_on_alpaca_broker() -> None:
    """The umbrella stage-0 capability gate (RenQuant#439,
    adapters/commit_contract.py::fractional_capability_gate) probes the
    broker for (a) callable is_fractionable and (b) a callable no-submit
    classifier, and its stop router calls
    supports_broker_side_stops(symbol, qty). This pins the exact surface."""
    _, broker = _client_and_broker({"BLK": True})

    # (a) fractionable probe.
    assert callable(getattr(broker, "is_fractionable", None))
    # (b) no-submit classifier, instance-callable, agrees with the module.
    classifier = getattr(broker, "is_no_submit_status", None)
    assert callable(classifier)
    for status in NO_SUBMIT_STATUSES:
        assert classifier(status) is True
    assert classifier("filled") is False
    # Stop routing: qty-aware two-arg call (the round-2 consumer signature).
    assert broker.supports_broker_side_stops("BLK", 0.435578) is False
    assert broker.supports_broker_side_stops("AAPL", 3) is True
    assert broker.supports_broker_side_stops() is True
    # Eps-integral broker noise counts as whole-share for stop capability.
    assert broker.supports_broker_side_stops("AAPL", 3.0000000001) is True


def test_readonly_wrapper_shadow_acks_notional_orders() -> None:
    _, broker = _client_and_broker({"BLK": True})
    shadow = ReadOnlyBrokerWrapper(broker)

    result = shadow.place_notional_order("BLK", "buy", 324.17)

    assert result["status"] == "shadow_ack"
    assert result["shadow"] is True
    assert result["requested_notional"] == 324.17


def test_base_broker_place_notional_order_fails_loud_by_default() -> None:
    broker = PaperBroker()
    broker.connect()
    with pytest.raises(NotImplementedError, match="notional"):
        broker.place_notional_order("SPY", "buy", 25.0)
