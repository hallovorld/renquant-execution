from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from renquant_execution import (
    FRACTIONABLE_LOOKUP_FAILED_STATUS,
    NO_SUBMIT_STATUSES,
    NON_FRACTIONABLE_STATUS,
    AlpacaBroker,
    BaseBroker,
    LiveCommitPlan,
    build_live_persistence_alert_event,
    build_live_commit_plan,
    classify_broker_result,
    commit_live_persistence,
    execute_live_commit,
    post_live_persistence_alert,
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
            "canceled": False,
            "expired": False,
            "terminal": False,
            "skipped": False,
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
            "canceled": False,
            "expired": False,
            "terminal": False,
            "skipped": False,
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
        "canceled": False,
        "expired": False,
        "terminal": True,
        "skipped": False,
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
        {"broker": "recording", "dry_run": False, "n_intents": 2, "n_submitted": 2, "n_skipped": 0}
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
        "run_id": "run-1",
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
    assert committed["persistence_audit"]["run_id"] == "run-db-1"


def test_live_persistence_alert_event_summarizes_commit_payload(tmp_path: Path) -> None:
    state_path = tmp_path / "live_state.alpaca.json"
    journal_path = tmp_path / "trades.jsonl"
    broker = RecordingBroker()
    plan = execute_live_commit(
        broker=broker,
        order_intents=[{"ticker": "AAPL", "action": "buy", "quantity": 2}],
    )
    committed = commit_live_persistence(
        plan,
        live_state_path=state_path,
        trade_journal_path=journal_path,
        run_id="run-alert-1",
        timestamp="2026-06-12T05:00:00+00:00",
    )

    event = build_live_persistence_alert_event(committed)

    assert event.taxonomy == "LIVE_PERSISTENCE"
    assert event.title == "RenQuant native live persistence committed (recording)"
    assert "committed_mutations=2" in event.body
    assert "trade_rows=1" in event.body
    assert str(state_path) in event.body
    assert event.key
    assert event.force is True


def test_post_live_persistence_alert_respects_env_suppression(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RENQUANT_NO_NOTIFY", "1")
    payload = {
        "broker_name": "recording",
        "persistence_audit": {
            "committed_mutation_count": 2,
            "trade_journal_row_count": 1,
            "lifecycle_journal_row_count": 0,
            "live_state_snapshot_row_count": 0,
            "live_state_path": "live_state.alpaca.json",
            "trade_journal_path": "trades.jsonl",
            "run_id": "run-alert-2",
            "timestamp": "2026-06-12T05:00:00+00:00",
        },
    }

    ok = post_live_persistence_alert(
        "https://ntfy.sh/unused",
        payload,
        state_path=tmp_path / "alert-state.json",
    )

    assert ok is False
    alert_log = (tmp_path / "alert-state.jsonl").read_text(encoding="utf-8")
    assert "suppressed_env" in alert_log


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


# ── End-to-end fractional-share behavior through execute_live_commit ─────────
#
# These exercise the full live boundary the reviewer asked for: a fractional
# BUY -> AlpacaBroker broker result -> commit_live_persistence quantity ->
# exit/stop policy, plus the non-fractionable and asset-lookup-failure paths,
# driven through execute_live_commit / commit planning (not just direct broker
# methods). They use the real AlpacaBroker with an injected fake TradingClient
# so the live guard logic runs end to end.


class _FakeAccount:
    status = "ACTIVE"
    portfolio_value = 10000.0
    cash = 10000.0
    non_marginable_buying_power = 10000.0


class _FakeAlpacaClient:
    """Fake alpaca-py TradingClient: records submits, echoes request shape."""

    def __init__(
        self,
        fractionable: dict[str, bool],
        *,
        fill_status: str = "filled",
        fill: bool = True,
        fill_price: float = 101.0,
    ) -> None:
        self._fractionable = fractionable
        self._fill_status = fill_status
        self._fill = fill
        self._fill_price = fill_price
        self.submitted: list[object] = []
        self.get_asset_calls: list[str] = []

    def get_account(self):
        return _FakeAccount()

    def get_asset(self, symbol: str):
        self.get_asset_calls.append(symbol)
        if symbol not in self._fractionable:
            raise RuntimeError(f"unknown asset {symbol}")
        return SimpleNamespace(fractionable=self._fractionable[symbol])

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        qty = float(getattr(order_data, "qty", 0.0) or 0.0)
        side = str(getattr(getattr(order_data, "side", ""), "value", "") or "").upper()
        return SimpleNamespace(
            id=f"ord-{len(self.submitted)}",
            status=self._fill_status,
            symbol=getattr(order_data, "symbol", ""),
            side=side or "BUY",
            qty=qty,
            filled_qty=qty if self._fill else 0.0,
            filled_avg_price=self._fill_price if self._fill else 0.0,
        )


def _alpaca_broker(client: _FakeAlpacaClient) -> AlpacaBroker:
    broker = AlpacaBroker(paper=True, label="alpaca-frac-e2e")
    broker._trading_client = client  # noqa: SLF001 — inject fake, skip connect()
    return broker


def test_e2e_fractional_buy_fills_persists_quantity_and_routes_software_stop(
    tmp_path: Path,
) -> None:
    client = _FakeAlpacaClient({"BLK": True})
    broker = _alpaca_broker(client)
    state_path = tmp_path / "live_state.alpaca.json"
    journal_path = tmp_path / "trades.jsonl"
    state_path.write_text(
        json.dumps({"account_snapshot": {"positions": {}}}), encoding="utf-8"
    )

    plan = execute_live_commit(
        broker=broker,
        order_intents=[{"symbol": "BLK", "action": "buy", "quantity": 0.435578}],
    )

    # Broker result: fractional qty submitted and filled (not floored/skipped).
    submitted = plan.submitted_orders[0]
    assert submitted["status"] == "filled"
    assert submitted["quantity"] == pytest.approx(0.435578)
    assert submitted["requested_quantity"] == pytest.approx(0.435578)
    assert submitted["skipped"] is False
    # Audit counts it as a real submission.
    assert plan.execution_audit == [
        {
            "broker": "alpaca-frac-e2e",
            "dry_run": False,
            "n_intents": 1,
            "n_submitted": 1,
            "n_skipped": 0,
        }
    ]

    # Persistence: the FRACTIONAL quantity flows all the way to live state.
    commit_live_persistence(
        plan,
        live_state_path=state_path,
        trade_journal_path=journal_path,
        run_id="run-frac-1",
        timestamp="2026-06-30T05:00:00+00:00",
    )
    positions = json.loads(state_path.read_text(encoding="utf-8"))[
        "account_snapshot"
    ]["positions"]
    assert positions["BLK"]["quantity"] == pytest.approx(0.435578)
    journal = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert journal[0]["symbol"] == "BLK"
    assert journal[0]["filled_qty"] == pytest.approx(0.435578)

    # Exit/stop policy: the fractional holding cannot get a broker-side stop, so
    # the caller is routed to a software stop and a broker-side stop fails closed.
    assert broker.supports_broker_side_stops("BLK", positions["BLK"]["quantity"]) is False
    with pytest.raises(ValueError, match="whole-share"):
        broker.place_stop_order("BLK", positions["BLK"]["quantity"], 900.0)


def test_e2e_non_fractionable_fractional_buy_is_skipped_not_submitted() -> None:
    client = _FakeAlpacaClient({"GS": False})
    broker = _alpaca_broker(client)

    with pytest.warns(RuntimeWarning):
        plan = execute_live_commit(
            broker=broker,
            order_intents=[{"symbol": "GS", "action": "buy", "quantity": 0.4}],
        )

    submitted = plan.submitted_orders[0]
    assert submitted["status"] == NON_FRACTIONABLE_STATUS
    assert submitted["skipped"] is True
    assert submitted["quantity"] == 0.0
    assert submitted["requested_quantity"] == pytest.approx(0.4)
    # Audit must NOT count a no-submit order as submitted.
    assert plan.execution_audit == [
        {
            "broker": "alpaca-frac-e2e",
            "dry_run": False,
            "n_intents": 1,
            "n_submitted": 0,
            "n_skipped": 1,
        }
    ]
    # The order_submission mutation is marked skipped; no fill is planned.
    assert [row["mutation_type"] for row in plan.state_mutations] == ["order_submission"]
    assert plan.state_mutations[0]["skipped"] is True
    assert plan.state_mutations[0]["pending"] is False


def test_e2e_asset_lookup_failure_fails_closed_through_commit() -> None:
    client = _FakeAlpacaClient({})  # every get_asset raises
    broker = _alpaca_broker(client)

    with pytest.warns(RuntimeWarning):
        plan = execute_live_commit(
            broker=broker,
            order_intents=[{"symbol": "ZZZZ", "action": "buy", "quantity": 1.9}],
        )

    submitted = plan.submitted_orders[0]
    assert submitted["status"] == FRACTIONABLE_LOOKUP_FAILED_STATUS
    assert submitted["skipped"] is True
    assert submitted["quantity"] == 0.0
    assert submitted["requested_quantity"] == pytest.approx(1.9)
    assert plan.execution_audit[0]["n_submitted"] == 0
    assert plan.execution_audit[0]["n_skipped"] == 1
    # Classified as skipped (a first-class no-submit class), never pending.
    classified = classify_broker_result(submitted)
    assert classified["skipped"] is True
    assert classified["pending"] is False
    assert classified["rejected"] is False


# ── S-FRAC stage 1: DAY-expiry / cancel-with-fill terminal classification ────
# and float requested-vs-filled epsilon discipline (design SS4).


def test_classify_day_expiry_unfilled_is_terminal_no_fill() -> None:
    # Fractional orders are TIF=DAY only: an unfilled DAY order expires at
    # the close — TERMINAL, never a resting order carried overnight.
    result = classify_broker_result({
        "status": "expired",
        "quantity": 0.341052,
        "filled_qty": 0.0,
    })
    assert result["expired"] is True
    assert result["terminal"] is True
    assert result["rejected"] is True  # legacy consumers: order is dead
    assert result["canceled"] is False
    assert result["pending"] is False
    assert result["filled"] is False
    assert result["partial"] is False


def test_classify_partial_fill_then_expire_keeps_the_real_fill() -> None:
    # Partial-fill-then-expire: the filled 0.20 is REAL (position + cash);
    # only the unfilled remainder died with the DAY order.
    result = classify_broker_result({
        "status": "expired",
        "quantity": 0.341052,
        "filled_qty": 0.20,
        "filled_avg_price": 948.0,
    })
    assert result["expired"] is True
    assert result["terminal"] is True
    assert result["partial"] is True  # persistence must book the fill
    assert result["filled"] is False
    assert result["pending"] is False
    assert result["filled_qty"] == pytest.approx(0.20)


def test_classify_cancel_with_fill_is_terminal_and_books_the_fill() -> None:
    result = classify_broker_result({
        "status": "canceled",
        "quantity": 1.0,
        "filled_qty": 0.3,
        "filled_avg_price": 100.0,
    })
    assert result["canceled"] is True
    assert result["terminal"] is True
    assert result["partial"] is True
    assert result["pending"] is False
    assert result["filled_qty"] == pytest.approx(0.3)


def test_expired_partial_fill_plans_persistence_for_the_filled_portion() -> None:
    plan = build_live_commit_plan({
        "broker_name": "alpaca",
        "order_intents": [
            {"symbol": "BLK", "action": "BUY", "quantity": 0.341052}
        ],
        "submitted_orders": [{
            "order_id": "ord-exp-1",
            "status": "expired",
            "symbol": "BLK",
            "action": "BUY",
            "quantity": 0.341052,
            "filled_qty": 0.20,
            "filled_avg_price": 948.0,
        }],
        "execution_audit": [],
    })

    submission = plan.state_mutations[0]
    assert submission["expired"] is True
    assert submission["terminal"] is True
    # The REAL filled portion reaches persistence planning; the dead
    # remainder does not create any position/journal effect.
    effects = [m for m in plan.state_mutations if m["mutation_type"] != "order_submission"]
    assert [m["mutation_type"] for m in effects] == [
        "planned_live_state_update",
        "planned_trade_log_append",
    ]
    assert all(m["filled_qty"] == pytest.approx(0.20) for m in effects)


def test_classify_float_fill_comparison_uses_dust_epsilon() -> None:
    # A broker cumulative within QTY_INTEGRAL_EPS of the request is COMPLETE
    # (float partial fills accumulate; never compare with raw >=).
    near_full = classify_broker_result({
        "status": "partially_filled",  # stale status; quantities decide
        "quantity": 0.435578,
        "filled_qty": 0.435578 - 2e-10,
    })
    assert near_full["filled"] is True

    # Float accumulation noise ABOVE the request is also complete, not a
    # phantom overfill/partial.
    accumulated = classify_broker_result({
        "status": "accepted",
        "quantity": 0.3,
        "filled_qty": 0.1 + 0.1 + 0.1,  # 0.30000000000000004
    })
    assert accumulated["filled"] is True
    assert accumulated["partial"] is False

    # A genuine fractional partial stays partial.
    partial = classify_broker_result({
        "status": "accepted",
        "quantity": 0.341052,
        "filled_qty": 0.20,
    })
    assert partial["partial"] is True
    assert partial["filled"] is False


def test_no_submit_status_matrix_is_terminal_never_pending() -> None:
    # The full no-submit vocabulary: never submitted, never pending, never a
    # broker rejection, always terminal, zero filled qty.
    for status in sorted(NO_SUBMIT_STATUSES):
        result = classify_broker_result({
            "status": status,
            "quantity": 0.4,
            "requested_quantity": 0.4,
        })
        assert result["skipped"] is True, status
        assert result["terminal"] is True, status
        assert result["pending"] is False, status
        assert result["rejected"] is False, status
        assert result["filled"] is False, status
        assert result["partial"] is False, status
        assert result["filled_qty"] == 0.0, status
