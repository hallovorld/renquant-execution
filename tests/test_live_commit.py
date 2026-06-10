from __future__ import annotations

import json
from pathlib import Path

import pytest

from renquant_execution import (
    LiveCommitPlan,
    build_live_commit_plan,
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
    ])

    assert intents == [
        {"symbol": "MSFT", "action": "SELL", "quantity": 1.0},
        {"symbol": "AAPL", "action": "BUY", "quantity": 2.0},
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
        },
        {
            "mutation_id": "planned-order-2",
            "mutation_type": "order_submission",
            "readonly": True,
            "symbol": "AAPL",
            "action": "BUY",
            "status": "dry_run",
            "order_id": "readonly-dry-2",
        },
    ]
    assert plan.to_payload()["source"] == "renquant_execution.live_commit_plan"


def test_build_live_commit_plan_preserves_existing_state_mutations() -> None:
    payload = _execution_payload()
    payload["state_mutations"] = [
        {"mutation_type": "live_state_write", "path": "live_state.alpaca_shadow.json"}
    ]

    plan = build_live_commit_plan(payload)

    assert plan.state_mutations == [
        {"mutation_type": "live_state_write", "path": "live_state.alpaca_shadow.json"}
    ]


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
