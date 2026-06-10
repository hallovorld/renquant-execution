from __future__ import annotations

import json
from pathlib import Path

import pytest

from renquant_execution import (
    LiveCommitPlan,
    build_live_commit_plan,
    classify_broker_result,
    sell_first_order_intents,
    write_live_commit_plan,
)


def _execution_payload() -> dict:
    return {
        "schema_version": 1,
        "source": "renquant_execution.execution",
        "broker_name": "readonly-alpaca",
        "dry_run": True,
        "order_intents": [
            {"ticker": "AAPL", "action": "buy", "quantity": 2},
            {"ticker": "MSFT", "action": "sell", "quantity": 1},
        ],
        "submitted_orders": [
            {
                "order_id": "readonly-dry-1",
                "status": "dry_run",
                "symbol": "MSFT",
                "action": "SELL",
                "quantity": 1.0,
            },
            {
                "order_id": "readonly-dry-2",
                "status": "dry_run",
                "symbol": "AAPL",
                "action": "BUY",
                "quantity": 2.0,
            },
        ],
        "execution_audit": [
            {"broker": "readonly-alpaca", "dry_run": True, "n_intents": 2, "n_submitted": 2}
        ],
    }


def test_sell_first_order_intents_normalizes_and_orders_sells_before_buys() -> None:
    intents = sell_first_order_intents([
        {"ticker": "AAPL", "action": "buy", "quantity": 2},
        {"symbol": "MSFT", "action": "SELL", "qty": 1},
        {"ticker": "TSLA", "action": "buy", "quantity": 3},
        {"ticker": "IBM", "action": "sell", "quantity": 4},
    ])

    assert intents == [
        {"symbol": "MSFT", "action": "SELL", "quantity": 1.0},
        {"symbol": "IBM", "action": "SELL", "quantity": 4.0},
        {"symbol": "AAPL", "action": "BUY", "quantity": 2.0},
        {"symbol": "TSLA", "action": "BUY", "quantity": 3.0},
    ]


def test_build_live_commit_plan_is_readonly_and_auditable() -> None:
    plan = build_live_commit_plan(_execution_payload())

    assert isinstance(plan, LiveCommitPlan)
    assert plan.broker_name == "readonly-alpaca"
    assert plan.readonly is True
    assert [intent["action"] for intent in plan.order_intents] == ["SELL", "BUY"]
    assert plan.execution_audit == _execution_payload()["execution_audit"]
    assert plan.state_mutations == [
        {
            "mutation_id": "planned-order-1",
            "mutation_type": "order_submission",
            "readonly": True,
            "symbol": "MSFT",
            "action": "SELL",
            "status": "dry_run",
            "order_id": "readonly-dry-1",
            "filled": False,
            "partial": False,
            "pending": True,
            "rejected": False,
            "filled_qty": 0.0,
            "filled_avg_price": 0.0,
        },
        {
            "mutation_id": "planned-order-2",
            "mutation_type": "order_submission",
            "readonly": True,
            "symbol": "AAPL",
            "action": "BUY",
            "status": "dry_run",
            "order_id": "readonly-dry-2",
            "filled": False,
            "partial": False,
            "pending": True,
            "rejected": False,
            "filled_qty": 0.0,
            "filled_avg_price": 0.0,
        },
    ]
    assert plan.to_payload()["source"] == "renquant_execution.live_commit_plan"


def test_classify_broker_result_covers_live_order_statuses() -> None:
    assert classify_broker_result({
        "status": "filled",
        "quantity": 3,
        "filled_qty": 3,
        "filled_avg_price": 101.5,
    }) == {
        "status": "filled",
        "filled": True,
        "partial": False,
        "pending": False,
        "rejected": False,
        "filled_qty": 3.0,
        "filled_avg_price": 101.5,
    }
    assert classify_broker_result({
        "status": "partially_filled",
        "quantity": 3,
        "filled_qty": 1,
        "avg_price": 100,
    })["partial"] is True
    assert classify_broker_result({"status": "new", "quantity": 3})["pending"] is True
    assert classify_broker_result({"status": "rejected", "quantity": 3})["rejected"] is True


def test_build_live_commit_plan_preserves_existing_state_mutations() -> None:
    payload = _execution_payload()
    payload["state_mutations"] = [
        {"mutation_type": "live_state_write", "path": "live_state.alpaca_shadow.json"}
    ]

    plan = build_live_commit_plan(payload)

    assert plan.state_mutations == [
        {"mutation_type": "live_state_write", "path": "live_state.alpaca_shadow.json"}
    ]


def test_build_live_commit_plan_preserves_explicit_empty_audit_and_mutations() -> None:
    payload = _execution_payload()
    payload["execution_audit"] = []
    payload["audit_rows"] = [{"broker": "fallback", "dry_run": True}]
    payload["state_mutations"] = []

    plan = build_live_commit_plan(payload)

    assert plan.execution_audit == []
    assert plan.state_mutations == []


def test_build_live_commit_plan_rejects_live_mode() -> None:
    with pytest.raises(ValueError, match="readonly-only"):
        build_live_commit_plan(_execution_payload(), readonly=False)


def test_live_commit_plan_requires_broker_name() -> None:
    payload = _execution_payload()
    payload.pop("broker_name")

    with pytest.raises(ValueError, match="broker_name"):
        build_live_commit_plan(payload)


def test_write_live_commit_plan_writes_payload(tmp_path: Path) -> None:
    execution = tmp_path / "execution.json"
    output = tmp_path / "commit-plan.json"
    execution.write_text(json.dumps(_execution_payload()), encoding="utf-8")

    plan = write_live_commit_plan(execution_json=execution, output_json=output)

    assert json.loads(output.read_text(encoding="utf-8")) == plan.to_payload()
