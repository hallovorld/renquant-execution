from __future__ import annotations

import pytest

from renquant_execution import (
    BrokerExecutionPipeline,
    ExecutionContext,
    ExecutionPipeline,
    PaperBroker,
    normalize_order_intent,
)


def test_execution_pipeline_submits_via_injected_broker() -> None:
    calls = []

    def submitter(broker_name, intents, dry_run):
        calls.append((broker_name, dry_run, len(intents)))
        return [{"id": "dry-1", **intents[0]}]

    ctx = ExecutionContext(
        broker_name="paper",
        order_intents=[{"ticker": "AAPL", "action": "buy"}],
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
