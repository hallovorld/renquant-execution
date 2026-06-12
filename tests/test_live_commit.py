from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from renquant_execution import (
    BaseBroker,
    LiveCommitPlan,
    build_live_commit_plan,
    classify_broker_result,
    commit_live_persistence,
    execute_live_commit,
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
    assert classify_broker_result({
        "status": "filled",
        "quantity": 4,
        "filled_avg_price": 50.0,
    })["filled_qty"] == pytest.approx(4.0)


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


class RecordingBroker(BaseBroker):
    broker_name = "recording"

    def __init__(self) -> None:
        self.orders: list[tuple[str, str, float]] = []

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def get_position(self, symbol: str) -> float:
        return 0.0

    def get_account_value(self) -> float:
        return 1000.0

    def get_cash(self) -> float:
        return 1000.0

    def place_order(self, symbol: str, action: str, quantity: float) -> dict:
        self.orders.append((symbol, action, quantity))
        return {
            "order_id": f"ord-{len(self.orders)}",
            "status": "filled",
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "filled_qty": quantity,
            "filled_avg_price": 10.0,
        }


def test_execute_live_commit_submits_sell_first_and_returns_non_readonly_plan() -> None:
    broker = RecordingBroker()

    plan = execute_live_commit(
        broker=broker,
        order_intents=[
            {"ticker": "AAPL", "action": "buy", "quantity": 2},
            {"ticker": "MSFT", "action": "sell", "quantity": 1},
        ],
    )

    assert broker.orders == [("MSFT", "SELL", 1.0), ("AAPL", "BUY", 2.0)]
    assert plan.readonly is False
    assert plan.broker_name == "recording"
    assert [row["action"] for row in plan.submitted_orders] == ["SELL", "BUY"]
    assert [row["mutation_type"] for row in plan.state_mutations] == [
        "order_submission",
        "planned_live_state_update",
        "planned_trade_log_append",
        "order_submission",
        "planned_live_state_update",
        "planned_trade_log_append",
    ]
    assert plan.state_mutations[0]["readonly"] is False
    assert plan.state_mutations[1] == {
        "mutation_id": "planned-order-1-live-state",
        "mutation_type": "planned_live_state_update",
        "readonly": True,
        "committed": False,
        "effect": "decrease_position",
        "symbol": "MSFT",
        "action": "SELL",
        "source_order_id": "ord-1",
        "status": "filled",
        "filled_qty": 1.0,
        "filled_avg_price": 10.0,
    }
    assert plan.state_mutations[2] == {
        "mutation_id": "planned-order-1-trade-log",
        "mutation_type": "planned_trade_log_append",
        "readonly": True,
        "committed": False,
        "symbol": "MSFT",
        "action": "SELL",
        "source_order_id": "ord-1",
        "status": "filled",
        "filled_qty": 1.0,
        "filled_avg_price": 10.0,
    }
    assert plan.execution_audit == [
        {"broker": "recording", "dry_run": False, "n_intents": 2, "n_submitted": 2}
    ]


def test_execute_live_commit_dry_run_does_not_mutate_broker() -> None:
    broker = RecordingBroker()

    plan = execute_live_commit(
        broker=broker,
        order_intents=[{"ticker": "AAPL", "action": "buy", "quantity": 2}],
        dry_run=True,
    )

    assert broker.orders == []
    assert plan.readonly is True
    assert plan.submitted_orders == [
        {
            "order_id": "dry-1",
            "status": "dry_run",
            "symbol": "AAPL",
            "action": "BUY",
            "quantity": 2.0,
        }
    ]
    assert [row["mutation_type"] for row in plan.state_mutations] == ["order_submission"]
    assert plan.state_mutations[0]["readonly"] is True


class RejectingBroker(RecordingBroker):
    broker_name = "rejecting"

    def place_order(self, symbol: str, action: str, quantity: float) -> dict:
        self.orders.append((symbol, action, quantity))
        return {
            "order_id": f"rej-{len(self.orders)}",
            "status": "rejected",
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "reject_reason": "test rejection",
        }


def test_execute_live_commit_does_not_plan_persistence_for_rejected_orders() -> None:
    broker = RejectingBroker()

    plan = execute_live_commit(
        broker=broker,
        order_intents=[{"ticker": "AAPL", "action": "buy", "quantity": 2}],
    )

    assert [row["mutation_type"] for row in plan.state_mutations] == ["order_submission"]
    assert plan.state_mutations[0]["rejected"] is True
    assert plan.state_mutations[0]["readonly"] is False


def test_commit_live_persistence_updates_state_and_trade_journal(tmp_path: Path) -> None:
    state_path = tmp_path / "live_state.alpaca.json"
    journal_path = tmp_path / "trades.jsonl"
    state_path.write_text(
        json.dumps({
            "account_snapshot": {
                "positions": {
                    "MSFT": {
                        "ticker": "MSFT",
                        "quantity": 3,
                        "avg_entry_price": 8.0,
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    broker = RecordingBroker()
    plan = execute_live_commit(
        broker=broker,
        order_intents=[
            {"ticker": "AAPL", "action": "buy", "quantity": 2},
            {"ticker": "MSFT", "action": "sell", "quantity": 1},
        ],
    )

    committed = commit_live_persistence(
        plan,
        live_state_path=state_path,
        trade_journal_path=journal_path,
        run_id="run-1",
        timestamp="2026-06-12T05:00:00+00:00",
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    positions = state["account_snapshot"]["positions"]
    assert positions["MSFT"]["quantity"] == pytest.approx(2.0)
    assert positions["MSFT"]["avg_entry_price"] == pytest.approx(8.0)
    assert positions["AAPL"] == {
        "ticker": "AAPL",
        "quantity": 2.0,
        "avg_entry_price": 10.0,
    }
    assert state["native_persistence"] == {
        "schema_version": 1,
        "last_commit_timestamp": "2026-06-12T05:00:00+00:00",
        "last_commit_run_id": "run-1",
        "last_commit_broker": "recording",
    }

    journal_rows = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [
        (row["symbol"], row["action"], row["filled_qty"], row["order_id"])
        for row in journal_rows
    ] == [
        ("MSFT", "SELL", 1.0, "ord-1"),
        ("AAPL", "BUY", 2.0, "ord-2"),
    ]
    committed_persistence = [
        row for row in committed["state_mutations"]
        if row["mutation_type"].startswith("planned_")
    ]
    assert all(row["committed"] is True for row in committed_persistence)
    assert all(row["readonly"] is False for row in committed_persistence)
    assert committed["persistence_audit"] == {
        "schema_version": 1,
        "committed_mutation_count": 4,
        "trade_journal_row_count": 2,
        "lifecycle_journal_row_count": 0,
        "live_state_snapshot_row_count": 0,
        "live_state_path": str(state_path),
        "trade_journal_path": str(journal_path),
        "lifecycle_journal_path": None,
        "runs_db_path": None,
        "timestamp": "2026-06-12T05:00:00+00:00",
    }


def test_commit_live_persistence_records_db_snapshot_and_lifecycle_journal(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "live_state.alpaca.json"
    journal_path = tmp_path / "trades.jsonl"
    lifecycle_path = tmp_path / "lifecycle.jsonl"
    db_path = tmp_path / "runs.alpaca.db"
    state_path.write_text(
        json.dumps({
            "regime": "BULL_CALM",
            "regime_confidence": 0.61,
            "high_water_mark": 12000.0,
            "cash": 900.0,
            "portfolio_value": 12050.0,
            "account_snapshot": {"positions": {}},
        }),
        encoding="utf-8",
    )
    broker = RecordingBroker()
    plan = execute_live_commit(
        broker=broker,
        order_intents=[{"ticker": "AAPL", "action": "buy", "quantity": 2}],
    )

    committed = commit_live_persistence(
        plan,
        live_state_path=state_path,
        trade_journal_path=journal_path,
        lifecycle_journal_path=lifecycle_path,
        runs_db_path=db_path,
        run_id="run-db-1",
        strategy="renquant_104_live",
        timestamp="2026-06-12T05:00:00+00:00",
    )

    lifecycle_rows = [
        json.loads(line)
        for line in lifecycle_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lifecycle_rows) == 1
    assert lifecycle_rows[0]["schema_version"] == "order-lifecycle-v1"
    assert lifecycle_rows[0]["event"] == "filled"
    assert lifecycle_rows[0]["run_id"] == "run-db-1"
    assert lifecycle_rows[0]["broker"] == "recording"
    assert lifecycle_rows[0]["order_id"] == "ord-1"
    assert lifecycle_rows[0]["symbol"] == "AAPL"
    assert lifecycle_rows[0]["action"] == "BUY"
    assert lifecycle_rows[0]["quantity"] == pytest.approx(2.0)
    assert lifecycle_rows[0]["attribution"] == {
        "source_job": "native_live_run_candidate",
        "source_task": "commit_live_persistence",
    }

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT run_id, run_date, strategy, regime, confidence,
                      high_water_mark, cash, portfolio_value, n_holdings, state_json
                 FROM live_state_snapshots"""
        ).fetchone()
    assert row[:9] == (
        "run-db-1",
        "2026-06-12",
        "renquant_104_live",
        "BULL_CALM",
        0.61,
        12000.0,
        900.0,
        12050.0,
        1,
    )
    snapshot_state = json.loads(row[9])
    assert snapshot_state["account_snapshot"]["positions"]["AAPL"]["quantity"] == 2.0
    assert committed["persistence_audit"]["lifecycle_journal_row_count"] == 1
    assert committed["persistence_audit"]["live_state_snapshot_row_count"] == 1
    assert committed["persistence_audit"]["lifecycle_journal_path"] == str(lifecycle_path)
    assert committed["persistence_audit"]["runs_db_path"] == str(db_path)


def test_commit_live_persistence_full_sell_removes_position_and_stamps_wash_sale(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "live_state.alpaca.json"
    journal_path = tmp_path / "trades.jsonl"
    state_path.write_text(
        json.dumps({
            "account_snapshot": {
                "positions": {
                    "MSFT": {
                        "ticker": "MSFT",
                        "quantity": 1,
                        "avg_entry_price": 8.0,
                    }
                }
            },
            "last_sell_dates": {},
        }),
        encoding="utf-8",
    )
    plan = LiveCommitPlan(
        broker_name="recording",
        readonly=False,
        order_intents=[{"symbol": "MSFT", "action": "SELL", "quantity": 1.0}],
        submitted_orders=[],
        state_mutations=[
            {
                "mutation_id": "planned-order-1-live-state",
                "mutation_type": "planned_live_state_update",
                "readonly": True,
                "committed": False,
                "symbol": "MSFT",
                "action": "SELL",
                "source_order_id": "ord-1",
                "status": "filled",
                "filled_qty": 1.0,
                "filled_avg_price": 10.0,
            },
            {
                "mutation_id": "planned-order-1-trade-log",
                "mutation_type": "planned_trade_log_append",
                "readonly": True,
                "committed": False,
                "symbol": "MSFT",
                "action": "SELL",
                "source_order_id": "ord-1",
                "status": "filled",
                "filled_qty": 1.0,
                "filled_avg_price": 10.0,
            },
        ],
    )

    commit_live_persistence(
        plan,
        live_state_path=state_path,
        trade_journal_path=journal_path,
        timestamp="2026-06-12T05:00:00+00:00",
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["account_snapshot"]["positions"] == {}
    assert state["last_sell_dates"] == {"MSFT": "2026-06-12"}


def test_commit_live_persistence_rejects_readonly_plan(tmp_path: Path) -> None:
    plan = build_live_commit_plan(_execution_payload())

    with pytest.raises(ValueError, match="readonly"):
        commit_live_persistence(
            plan,
            live_state_path=tmp_path / "live_state.alpaca.json",
            trade_journal_path=tmp_path / "trades.jsonl",
        )


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
