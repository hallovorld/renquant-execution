"""Native live commit persistence.

This module owns the first native write path after broker submission: apply
filled commit-plan rows to a broker-scoped live-state cache and append an
auditable trade journal. It intentionally stays small; richer decision-trace DB
and notification parity can layer on top of this deterministic contract.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from .live_commit import LiveCommitPlan
from .order_lifecycle import build_order_lifecycle_event

_PERSISTENCE_MUTATIONS = {
    "planned_live_state_update",
    "planned_trade_log_append",
}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_datetime(value: dt.datetime | str | None) -> dt.datetime:
    if value is None:
        return _utc_now()
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"live_state must be a JSON object: {path}")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _ensure_live_state_snapshot_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS live_state_snapshots (
            run_id          TEXT PRIMARY KEY,
            run_date        DATE NOT NULL,
            strategy        TEXT,
            regime          TEXT,
            confidence      REAL,
            high_water_mark REAL,
            cash            REAL,
            portfolio_value REAL,
            n_holdings      INTEGER,
            state_json      TEXT NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lss_date ON live_state_snapshots(run_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lss_strategy ON live_state_snapshots(strategy)")


def _record_live_state_snapshot(
    *,
    db_path: Path,
    run_id: str,
    run_date: dt.date,
    strategy: str,
    state: dict[str, Any],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    positions = ((state.get("account_snapshot") or {}).get("positions") or {})
    n_holdings = len(positions) if isinstance(positions, dict) else None
    with sqlite3.connect(db_path) as conn:
        _ensure_live_state_snapshot_schema(conn)
        conn.execute(
            """INSERT OR REPLACE INTO live_state_snapshots
                  (run_id, run_date, strategy, regime, confidence,
                   high_water_mark, cash, portfolio_value, n_holdings, state_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                run_date.isoformat(),
                strategy,
                state.get("regime"),
                state.get("regime_confidence"),
                state.get("high_water_mark"),
                state.get("cash"),
                state.get("portfolio_value"),
                n_holdings,
                json.dumps(state, sort_keys=True),
            ),
        )
        conn.commit()


def _positions(state: dict[str, Any]) -> dict[str, Any]:
    account_snapshot = state.setdefault("account_snapshot", {})
    if not isinstance(account_snapshot, dict):
        raise ValueError("live_state.account_snapshot must be an object")
    positions = account_snapshot.setdefault("positions", {})
    if not isinstance(positions, dict):
        raise ValueError("live_state.account_snapshot.positions must be an object")
    return positions


def _finite_positive(value: Any) -> float:
    number = float(value or 0.0)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"filled quantity/price must be finite and positive: {value!r}")
    return number


def _apply_buy(positions: dict[str, Any], symbol: str, qty: float, price: float) -> None:
    existing = positions.get(symbol)
    if not isinstance(existing, dict):
        existing = {"ticker": symbol, "quantity": 0.0}
    old_qty = float(existing.get("quantity", existing.get("qty", 0.0)) or 0.0)
    old_avg = float(existing.get("avg_entry_price", existing.get("avg_price", 0.0)) or 0.0)
    new_qty = old_qty + qty
    if old_qty > 0.0 and old_avg > 0.0:
        avg_entry_price = ((old_qty * old_avg) + (qty * price)) / new_qty
    else:
        avg_entry_price = price
    existing.update({
        "ticker": symbol,
        "quantity": new_qty,
        "avg_entry_price": avg_entry_price,
    })
    positions[symbol] = existing


def _apply_sell(
    state: dict[str, Any],
    positions: dict[str, Any],
    symbol: str,
    qty: float,
    asof: dt.datetime,
) -> None:
    existing = positions.get(symbol)
    if not isinstance(existing, dict):
        state.setdefault("native_persistence_warnings", []).append({
            "warning": "sell_without_cached_position",
            "symbol": symbol,
            "quantity": qty,
            "timestamp": asof.isoformat(),
        })
        return
    old_qty = float(existing.get("quantity", existing.get("qty", 0.0)) or 0.0)
    new_qty = max(0.0, old_qty - qty)
    if new_qty <= 1e-9:
        positions.pop(symbol, None)
        last_sell_dates = state.setdefault("last_sell_dates", {})
        if isinstance(last_sell_dates, dict):
            last_sell_dates[symbol] = asof.date().isoformat()
        return
    existing["ticker"] = symbol
    existing["quantity"] = new_qty
    positions[symbol] = existing


def _apply_live_state_mutation(
    state: dict[str, Any],
    mutation: dict[str, Any],
    *,
    asof: dt.datetime,
) -> None:
    symbol = str(mutation.get("symbol") or "").upper()
    action = str(mutation.get("action") or "").upper()
    if not symbol:
        raise ValueError("persistence mutation missing symbol")
    qty = _finite_positive(mutation.get("filled_qty"))
    price = _finite_positive(mutation.get("filled_avg_price"))
    positions = _positions(state)
    if action == "BUY":
        _apply_buy(positions, symbol, qty, price)
    elif action == "SELL":
        _apply_sell(state, positions, symbol, qty, asof)
    else:
        raise ValueError(f"unsupported persistence action: {action!r}")


def _trade_journal_row(
    mutation: dict[str, Any],
    *,
    broker_name: str,
    run_id: str | None,
    asof: dt.datetime,
) -> dict[str, Any]:
    return {
        "schema_version": "native-live-trade-journal-v1",
        "timestamp": asof.isoformat(),
        "run_id": run_id,
        "broker_name": broker_name,
        "order_id": mutation.get("source_order_id"),
        "symbol": mutation.get("symbol"),
        "action": mutation.get("action"),
        "status": mutation.get("status"),
        "filled_qty": mutation.get("filled_qty"),
        "filled_avg_price": mutation.get("filled_avg_price"),
    }


def _lifecycle_event_from_mutation(
    mutation: dict[str, Any],
    *,
    broker_name: str,
    run_id: str | None,
    asof: dt.datetime,
    source_job: str,
    source_task: str,
) -> dict[str, Any]:
    status = str(mutation.get("status") or "").lower()
    event = "partially_filled" if status in {"partial", "partially_filled"} else "filled"
    return build_order_lifecycle_event(
        event=event,
        source_job=source_job,
        source_task=source_task,
        broker=broker_name,
        symbol=str(mutation.get("symbol") or ""),
        action=str(mutation.get("action") or ""),
        quantity=float(mutation.get("filled_qty") or 0.0),
        order_id=(
            str(mutation["source_order_id"])
            if mutation.get("source_order_id") is not None
            else None
        ),
        run_id=run_id,
        timestamp=asof,
        status=status or None,
        fill={
            "filled_qty": mutation.get("filled_qty"),
            "filled_avg_price": mutation.get("filled_avg_price"),
        },
    )


def _payload_from_plan(plan: LiveCommitPlan | dict[str, Any]) -> dict[str, Any]:
    return plan.to_payload() if isinstance(plan, LiveCommitPlan) else dict(plan)


def commit_live_persistence(
    plan: LiveCommitPlan | dict[str, Any],
    *,
    live_state_path: str | Path,
    trade_journal_path: str | Path,
    run_id: str | None = None,
    timestamp: dt.datetime | str | None = None,
    runs_db_path: str | Path | None = None,
    strategy: str = "renquant_104",
    lifecycle_journal_path: str | Path | None = None,
    lifecycle_source_job: str = "native_live_run_candidate",
    lifecycle_source_task: str = "commit_live_persistence",
) -> dict[str, Any]:
    """Commit filled live orders to native state and trade-journal artifacts.

    Readonly plans are rejected so rehearsal artifacts cannot accidentally
    mutate production state. Pending/rejected order submissions are ignored;
    only explicit planned persistence rows are committed.
    """
    payload = _payload_from_plan(plan)
    if payload.get("readonly", True):
        raise ValueError("cannot commit persistence for a readonly live commit plan")
    broker_name = str(payload.get("broker_name") or "")
    if not broker_name:
        raise ValueError("live commit plan missing broker_name")
    mutations = payload.get("state_mutations") or []
    if not isinstance(mutations, list):
        raise ValueError("live commit plan state_mutations must be a list")

    asof = _as_datetime(timestamp)
    state_path = Path(live_state_path)
    journal_path = Path(trade_journal_path)
    state = _load_state(state_path)
    committed_ids: set[str] = set()
    journal_rows: list[dict[str, Any]] = []
    lifecycle_rows: list[dict[str, Any]] = []

    for mutation in mutations:
        if not isinstance(mutation, dict):
            raise ValueError("live commit plan state_mutations must contain objects")
        mutation_type = mutation.get("mutation_type")
        if mutation_type not in _PERSISTENCE_MUTATIONS or mutation.get("committed") is True:
            continue
        mutation_id = mutation.get("mutation_id")
        if not mutation_id:
            raise ValueError("persistence mutation missing mutation_id")
        if mutation_type == "planned_live_state_update":
            _apply_live_state_mutation(state, mutation, asof=asof)
        elif mutation_type == "planned_trade_log_append":
            journal_rows.append(
                _trade_journal_row(
                    mutation,
                    broker_name=broker_name,
                    run_id=run_id,
                    asof=asof,
                )
            )
            lifecycle_rows.append(
                _lifecycle_event_from_mutation(
                    mutation,
                    broker_name=broker_name,
                    run_id=run_id,
                    asof=asof,
                    source_job=lifecycle_source_job,
                    source_task=lifecycle_source_task,
                )
            )
        committed_ids.add(str(mutation_id))

    state["native_persistence"] = {
        "schema_version": 1,
        "last_commit_timestamp": asof.isoformat(),
        "last_commit_run_id": run_id,
        "last_commit_broker": broker_name,
    }
    _write_json_atomic(state_path, state)
    _append_jsonl(journal_path, journal_rows)
    if lifecycle_journal_path is not None:
        _append_jsonl(Path(lifecycle_journal_path), lifecycle_rows)
    if runs_db_path is not None and run_id is not None:
        _record_live_state_snapshot(
            db_path=Path(runs_db_path),
            run_id=run_id,
            run_date=asof.date(),
            strategy=strategy,
            state=state,
        )

    committed_mutations: list[dict[str, Any]] = []
    for mutation in mutations:
        row = dict(mutation)
        if row.get("mutation_id") in committed_ids:
            row["readonly"] = False
            row["committed"] = True
            if row["mutation_type"] == "planned_live_state_update":
                row["path"] = str(state_path)
            elif row["mutation_type"] == "planned_trade_log_append":
                row["path"] = str(journal_path)
        committed_mutations.append(row)

    out = dict(payload)
    out["state_mutations"] = committed_mutations
    out["persistence_audit"] = {
        "schema_version": 1,
        "committed_mutation_count": len(committed_ids),
        "trade_journal_row_count": len(journal_rows),
        "lifecycle_journal_row_count": len(lifecycle_rows) if lifecycle_journal_path else 0,
        "live_state_snapshot_row_count": 1 if runs_db_path is not None and run_id else 0,
        "live_state_path": str(state_path),
        "trade_journal_path": str(journal_path),
        "lifecycle_journal_path": str(lifecycle_journal_path) if lifecycle_journal_path else None,
        "runs_db_path": str(runs_db_path) if runs_db_path else None,
        "timestamp": asof.isoformat(),
    }
    return out


__all__ = ["commit_live_persistence"]
