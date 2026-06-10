"""Readonly live commit-plan contract for native trading cutover."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .broker import normalize_order_intent


@dataclass(frozen=True)
class LiveCommitPlan:
    """Auditable plan for live execution commit semantics.

    The plan is intentionally readonly. It is the contract boundary used while
    umbrella RunnerAdapter.commit semantics are lifted into renquant-execution.
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


def _sell_first_key(intent: dict[str, Any]) -> tuple[int, str]:
    normalized = normalize_order_intent(intent)
    action = normalized["action"]
    priority = 0 if action in {"SELL", "SELL_SHORT", "BUY_TO_COVER"} else 1
    return priority, normalized["symbol"]


def sell_first_order_intents(order_intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return normalized order intents in commit-safe sell-before-buy order."""
    return [
        normalize_order_intent(intent)
        for intent in sorted(order_intents, key=_sell_first_key)
    ]


def _planned_state_mutations(submitted_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mutations: list[dict[str, Any]] = []
    for idx, order in enumerate(submitted_orders, start=1):
        symbol = order.get("symbol") or order.get("ticker")
        action = str(order.get("action", "")).upper()
        status = str(order.get("status", "unknown"))
        mutations.append({
            "mutation_id": f"planned-order-{idx}",
            "mutation_type": "order_submission",
            "readonly": True,
            "symbol": symbol,
            "action": action,
            "status": status,
            "order_id": order.get("order_id") or order.get("id"),
        })
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
    audit_rows = _list_of_dicts(
        execution_payload.get("execution_audit") or execution_payload.get("audit_rows"),
        field_name="execution_audit/audit_rows",
    )
    state_mutations = _list_of_dicts(
        execution_payload.get("state_mutations"),
        field_name="state_mutations",
    ) or _planned_state_mutations(submitted_orders)
    return LiveCommitPlan(
        broker_name=str(broker_name),
        order_intents=order_intents,
        submitted_orders=submitted_orders,
        state_mutations=state_mutations,
        execution_audit=audit_rows,
        readonly=True,
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
    "sell_first_order_intents",
    "write_live_commit_plan",
]
