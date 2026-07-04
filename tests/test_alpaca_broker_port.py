"""Tests for AlpacaBrokerPort (moved from renquant-orchestrator PR #291 per
codex review — broker adapters belong in renquant-execution, not orchestrator).

No network: an injected fake TradingClient captures the request shape.
"""
from __future__ import annotations

import pytest

from renquant_execution.alpaca_broker_port import AlpacaBrokerPort, BrokerPortContractError


def test_alpaca_broker_port_request_shaping():
    alpaca = pytest.importorskip("alpaca")  # noqa: F841
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

    class FakeTradingClient:
        def __init__(self):
            self.requests = []

        def submit_order(self, *, order_data):
            self.requests.append(order_data)

            class _Order:
                status = "accepted"
                id = "broker-uuid"
                filled_qty = 0

            return _Order()

    client = FakeTradingClient()
    port = AlpacaBrokerPort(
        entry_order_type="limit",
        exit_order_type="market",
        limit_price_offset_bps=10.0,
        _client=client,
    )
    response = port.submit_order(
        client_order_id="pi-abc:1", symbol="AAA", side="BUY", qty=2, limit_price=100.0
    )
    assert response["status"] == "accepted"
    request = client.requests[0]
    assert isinstance(request, LimitOrderRequest)
    assert request.client_order_id == "pi-abc:1"  # client id == the child id
    assert request.time_in_force == TimeInForce.DAY
    assert request.side == OrderSide.BUY
    assert request.qty == 2
    assert request.limit_price == pytest.approx(100.10)  # +10 bps marketable

    port.submit_order(
        client_order_id="pi-def:1", symbol="BBB", side="SELL", qty=3, limit_price=None
    )
    exit_request = client.requests[1]
    assert isinstance(exit_request, MarketOrderRequest)
    assert exit_request.side == OrderSide.SELL
    assert exit_request.time_in_force == TimeInForce.DAY

    # A limit ENTRY without a positive reference price fails closed.
    with pytest.raises(BrokerPortContractError, match="no positive reference"):
        port.submit_order(
            client_order_id="pi-ghi:1", symbol="CCC", side="BUY", qty=1, limit_price=None
        )
