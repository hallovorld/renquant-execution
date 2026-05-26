from __future__ import annotations

import pytest

from renquant_execution import (
    AlpacaBroker,
    BaseBroker,
    BrokerExecutionPipeline,
    ExecutionContext,
    ExecutionPipeline,
    PaperBroker,
    ReadOnlyBrokerWrapper,
    get_broker,
    normalize_order_intent,
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
    assert ctx.audit_rows == [{"broker": "paper", "dry_run": True, "n_intents": 1, "n_submitted": 1}]


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
    assert AlpacaBroker(paper=True, label="alpaca-shorts").broker_name == "alpaca-shorts"


def test_get_broker_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unsupported broker_type"):
        get_broker("mystery")
