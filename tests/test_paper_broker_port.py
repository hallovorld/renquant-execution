"""PaperBrokerPort — BrokerPort protocol conformance over PaperBroker."""
from __future__ import annotations

import pytest

from renquant_execution.alpaca_broker_port import BrokerPortContractError
from renquant_execution.order_state_machine import (
    OrderStateBook,
    SIDE_BUY,
    SIDE_SELL,
    reconcile_on_restart,
)
from renquant_execution.paper_broker import PaperBroker
from renquant_execution.paper_broker_port import PaperBrokerPort


def _port() -> PaperBrokerPort:
    port = PaperBrokerPort(broker=PaperBroker(initial_cash=100_000.0))
    port.broker.set_price("AAPL", 100.0)
    return port


def test_submit_order_fills_and_returns_terminal_status():
    port = _port()
    result = port.submit_order(client_order_id="c1", symbol="AAPL", side=SIDE_BUY, qty=10)
    assert result["status"] == "filled"
    assert result["filled_qty"] == 10.0
    assert result["client_order_id"] == "c1"


def test_submit_order_rejects_duplicate_client_order_id():
    port = _port()
    port.submit_order(client_order_id="c1", symbol="AAPL", side=SIDE_BUY, qty=10)
    with pytest.raises(BrokerPortContractError):
        port.submit_order(client_order_id="c1", symbol="AAPL", side=SIDE_BUY, qty=5)


def test_open_orders_always_empty_because_paper_broker_fills_synchronously():
    port = _port()
    port.submit_order(client_order_id="c1", symbol="AAPL", side=SIDE_BUY, qty=10)
    assert dict(port.open_orders()) == {}


def test_order_status_returns_stored_terminal_result():
    port = _port()
    port.submit_order(client_order_id="c1", symbol="AAPL", side=SIDE_BUY, qty=10)
    status = port.order_status("c1")
    assert status["status"] == "filled"
    assert status["filled_qty"] == 10.0


def test_order_status_unknown_client_order_id_raises_key_error():
    port = _port()
    with pytest.raises(KeyError):
        port.order_status("nope")


def test_cancel_order_returns_already_terminal_status_no_live_cancel():
    port = _port()
    port.submit_order(client_order_id="c1", symbol="AAPL", side=SIDE_BUY, qty=10)
    result = port.cancel_order("c1")
    assert result["status"] == "filled"
    assert result["filled_qty"] == 10.0


def test_insufficient_cash_reports_rejected_not_an_exception():
    port = _port()
    result = port.submit_order(
        client_order_id="c1", symbol="AAPL", side=SIDE_BUY, qty=100_000
    )
    assert result["status"] == "rejected"
    assert result["filled_qty"] == 0.0


def test_sell_side_maps_correctly():
    port = _port()
    port.submit_order(client_order_id="buy1", symbol="AAPL", side=SIDE_BUY, qty=10)
    result = port.submit_order(client_order_id="sell1", symbol="AAPL", side=SIDE_SELL, qty=5)
    assert result["status"] == "filled"
    assert result["filled_qty"] == 5.0


def test_reconcile_on_restart_succeeds_against_fresh_book_and_paper_port():
    """The real end-to-end protocol conformance check the orchestrator
    PR's coupling test needs: reconcile_on_restart is the actual call
    begin_session() makes, and it must work against a genuine
    PaperBrokerPort, not a hand-patched fake."""
    port = _port()
    book = OrderStateBook(account="paper-test", trading_day="2026-07-05")
    result = reconcile_on_restart(book, port)
    assert result.clean
    assert result.mismatches == ()
