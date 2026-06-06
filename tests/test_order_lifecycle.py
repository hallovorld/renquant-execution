from __future__ import annotations

import datetime as dt

import pytest

from renquant_execution import (
    LIFECYCLE_SCHEMA_VERSION,
    build_order_lifecycle_event,
    lifecycle_event_from_confirmation,
)


def test_lifecycle_event_requires_runner_attribution() -> None:
    with pytest.raises(ValueError, match="source_job"):
        build_order_lifecycle_event(
            event="filled",
            source_job="",
            source_task="EmitAttributedOrderIntentsTask",
            broker="alpaca",
            symbol="aapl",
            action="buy",
            quantity=1,
        )


def test_lifecycle_event_normalizes_audit_surface() -> None:
    ts = dt.datetime(2026, 6, 2, 13, 30, tzinfo=dt.timezone.utc)

    row = build_order_lifecycle_event(
        event="submitted",
        source_job="PanelScoringJob",
        source_task="EmitAttributedOrderIntentsTask",
        broker="alpaca-paper",
        symbol="aapl",
        action="buy",
        quantity=2,
        order_id="OID-1",
        run_id="daily-full-shadow-20260602",
        timestamp=ts,
        status="accepted",
    )

    assert row["schema_version"] == LIFECYCLE_SCHEMA_VERSION
    assert row["symbol"] == "AAPL"
    assert row["action"] == "BUY"
    assert row["timestamp"] == "2026-06-02T13:30:00+00:00"
    assert row["attribution"] == {
        "source_job": "PanelScoringJob",
        "source_task": "EmitAttributedOrderIntentsTask",
    }


def test_confirmation_fill_keeps_runner_origin() -> None:
    row = lifecycle_event_from_confirmation(
        {
            "order_id": "OID-2",
            "status": "filled",
            "symbol": "MSFT",
            "side": "sell",
            "filled_qty": 3,
            "filled_avg_price": 410.25,
        },
        source_job="RiskExitJob",
        source_task="SubmitRiskExitOrdersTask",
        broker="alpaca",
        run_id="daily-full-20260602",
    )

    assert row["event"] == "filled"
    assert row["order_id"] == "OID-2"
    assert row["quantity"] == 3.0
    assert row["fill"]["filled_avg_price"] == 410.25
    assert row["attribution"]["source_job"] == "RiskExitJob"
