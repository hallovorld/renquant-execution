"""Live commit-plan contract for native trading cutover."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .broker import BaseBroker, normalize_order_intent
from .execution import BrokerExecutionPipeline, ExecutionContext


@dataclass(frozen=True)
class LiveCommitPlan:
    """Auditable plan for live execution commit semantics.

    ``readonly=True`` plans are rehearsal artifacts. ``readonly=False`` plans are
    produced only after a caller executes against an already-connected broker.
    """

    broker_name: str
    order_intents: list[dict[str, Any]]
    submitted_orders: list[dict[str, Any]]
    state_mutations: list[dict[str, Any]] = field(default_factory=list)
    execution_audit: list[dict[str, Any]] = field(default_factory=list)
    readonly: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source": "renquant_execution.live_commit_plan",
            "broker_name": self.broker_name,
            "readonly": self.readonly,
            "order_intents": list(self.order_intents),
            "submitted_orders": list(self.submitted_orders),
            "state_mutations": list(self.state_mutations),
            "execution_audit": list(self.execution_audit),
        }


def _load_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a JSON object: {path}")
    return payload


def _list_of_dicts(value: Any, *, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"execution payload field must be a list: {field_name}")
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(value):
        if not isinstance(row, dict):
            raise ValueError(f"execution payload field {field_name}[{idx}] must be an object")
        rows.append(dict(row))
    return rows


def _intent_priority(intent: dict[str, Any]) -> int:
    normalized = normalize_order_intent(intent)
    action = normalized["action"]
    return 0 if action in {"SELL", "SELL_SHORT", "BUY_TO_COVER"} else 1


def sell_first_order_intents(order_intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return normalized intents with sells first and original class order intact."""
    return [
        normalize_order_intent(intent)
        for intent in sorted(order_intents, key=_intent_priority)
    ]


def classify_broker_result(order: dict[str, Any]) -> dict[str, Any]:
    """Classify a broker order result for live state-mutation planning."""
    status = str(order.get("status", "") or "unknown").lower()
    requested_qty = float(order.get("quantity", order.get("qty", 0.0)) or 0.0)
    filled_qty = float(order.get("filled_qty", order.get("filled_quantity", 0.0)) or 0.0)
    filled_avg_price = float(
        order.get("filled_avg_price", order.get("avg_price", order.get("price", 0.0))) or 0.0
    )
    rejected = status in {"rejected", "canceled", "cancelled", "expired", "failed"}
    filled = status == "filled" or (filled_qty > 0.0 and requested_qty > 0.0 and filled_qty >= requested_qty)
    partial = (status in {"partially_filled", "partial"} or (
        filled_qty > 0.0 and requested_qty > 0.0 and filled_qty < requested_qty
    ))
    if filled and filled_qty <= 0.0:
        filled_qty = requested_qty
    pending = not (filled or partial or rejected)
    return {
        "status": status,
        "filled": filled,
        "partial": partial,
        "pending": pending,
        "rejected": rejected,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
    }


def _persistence_effect(action: str) -> str:
    if action == "BUY":
        return "increase_position"
    if action == "SELL":
        return "decrease_position"
    return "unknown"


def _planned_persistence_mutations(
    *,
    idx: int,
    order: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    if not (result["filled"] or result["partial"]):
        return []
    symbol = order.get("symbol") or order.get("ticker")
    action = str(order.get("action", "")).upper()
    order_id = order.get("order_id") or order.get("id")
    common = {
        "readonly": True,
        "committed": False,
        "symbol": symbol,
        "action": action,
        "source_order_id": order_id,
        "status": result["status"],
        "filled_qty": result["filled_qty"],
        "filled_avg_price": result["filled_avg_price"],
    }
    return [
        {
            "mutation_id": f"planned-order-{idx}-live-state",
            "mutation_type": "planned_live_state_update",
            "effect": _persistence_effect(action),
            **common,
        },
        {
            "mutation_id": f"planned-order-{idx}-trade-log",
            "mutation_type": "planned_trade_log_append",
            **common,
        },
    ]


def _planned_state_mutations(
    submitted_orders: list[dict[str, Any]],
    *,
    readonly: bool,
) -> list[dict[str, Any]]:
    mutations: list[dict[str, Any]] = []
    for idx, order in enumerate(submitted_orders, start=1):
        symbol = order.get("symbol") or order.get("ticker")
        action = str(order.get("action", "")).upper()
        result = classify_broker_result(order)
        mutations.append({
            "mutation_id": f"planned-order-{idx}",
            "mutation_type": "order_submission",
            "readonly": readonly,
            "symbol": symbol,
            "action": action,
            "status": result["status"],
            "order_id": order.get("order_id") or order.get("id"),
            "filled": result["filled"],
            "partial": result["partial"],
            "pending": result["pending"],
            "rejected": result["rejected"],
            "filled_qty": result["filled_qty"],
            "filled_avg_price": result["filled_avg_price"],
        })
        mutations.extend(
            _planned_persistence_mutations(
                idx=idx,
                order=order,
                result=result,
            )
        )
    return mutations


def build_live_commit_plan(
    execution_payload: dict[str, Any],
    *,
    readonly: bool = True,
) -> LiveCommitPlan:
    """Build a readonly live commit plan from a native execution payload."""
    if not readonly:
        raise ValueError("live commit plan generation is readonly-only")
    broker_name = execution_payload.get("broker_name")
    if not broker_name:
        raise ValueError("execution payload missing broker_name")
    order_intents = sell_first_order_intents(
        _list_of_dicts(execution_payload.get("order_intents"), field_name="order_intents")
    )
    submitted_orders = _list_of_dicts(
        execution_payload.get("submitted_orders"),
        field_name="submitted_orders",
    )
    raw_audit_rows = (
        execution_payload["execution_audit"]
        if "execution_audit" in execution_payload
        else execution_payload.get("audit_rows")
    )
    audit_rows = _list_of_dicts(raw_audit_rows, field_name="execution_audit/audit_rows")
    if "state_mutations" in execution_payload:
        state_mutations = _list_of_dicts(
            execution_payload["state_mutations"],
            field_name="state_mutations",
        )
    else:
        state_mutations = _planned_state_mutations(submitted_orders, readonly=True)
    return LiveCommitPlan(
        broker_name=str(broker_name),
        order_intents=order_intents,
        submitted_orders=submitted_orders,
        state_mutations=state_mutations,
        execution_audit=audit_rows,
        readonly=True,
    )


def execute_live_commit(
    *,
    broker: BaseBroker,
    order_intents: list[dict[str, Any]],
    dry_run: bool = False,
) -> LiveCommitPlan:
    """Execute normalized order intents on an already-connected broker.

    The function owns the native live commit boundary: normalize and sell-first
    order the intents, submit them through ``BrokerExecutionPipeline``, and return
    an auditable commit plan. Broker connection/account safety remains the
    caller's responsibility so live account preflight cannot be hidden here.
    """
    normalized = sell_first_order_intents(order_intents)
    ctx = ExecutionContext(
        broker_name=broker.broker_name,
        order_intents=normalized,
        dry_run=dry_run,
    )
    result = BrokerExecutionPipeline(broker).run(ctx)
    if not result.ok:
        raise RuntimeError(f"live commit execution failed: {result}")
    return LiveCommitPlan(
        broker_name=broker.broker_name,
        order_intents=normalized,
        submitted_orders=list(ctx.submitted_orders),
        state_mutations=_planned_state_mutations(
            ctx.submitted_orders,
            readonly=dry_run,
        ),
        execution_audit=list(ctx.audit_rows),
        readonly=dry_run,
    )


def write_live_commit_plan(
    *,
    execution_json: str | Path,
    output_json: str | Path,
) -> LiveCommitPlan:
    plan = build_live_commit_plan(_load_json(execution_json))
    out = Path(output_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(plan.to_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return plan


__all__ = [
    "LiveCommitPlan",
    "build_live_commit_plan",
    "classify_broker_result",
    "execute_live_commit",
    "sell_first_order_intents",
    "write_live_commit_plan",
]
