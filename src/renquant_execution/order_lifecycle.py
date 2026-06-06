"""Order lifecycle audit helpers.

The execution repo owns broker mutation, so it also owns the deterministic
shape used to audit intent -> submit -> fill/reconcile transitions.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

LIFECYCLE_SCHEMA_VERSION = "order-lifecycle-v1"
VALID_LIFECYCLE_EVENTS = (
    "intent_emitted",
    "submitted",
    "filled",
    "partially_filled",
    "cancelled",
    "reconciled",
    "rejected",
)


def build_order_lifecycle_event(
    *,
    event: str,
    source_job: str,
    source_task: str,
    broker: str,
    symbol: str,
    action: str,
    quantity: float,
    order_id: str | None = None,
    run_id: str | None = None,
    timestamp: dt.datetime | None = None,
    status: str | None = None,
    fill: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one normalized order lifecycle audit row.

    ``source_job`` and ``source_task`` are required for every event, including
    fills discovered during reconciliation, so runner-originated fills do not
    collapse into "manual/external" attribution.
    """
    if event not in VALID_LIFECYCLE_EVENTS:
        raise ValueError(f"unsupported order lifecycle event: {event!r}")
    if not source_job or not source_task:
        raise ValueError("source_job and source_task are required")
    qty = float(quantity)
    if qty <= 0:
        raise ValueError(f"quantity must be positive: {quantity!r}")
    ts = timestamp or dt.datetime.now(dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    payload = {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "event": event,
        "timestamp": ts.isoformat(),
        "run_id": run_id,
        "broker": str(broker),
        "order_id": order_id,
        "symbol": str(symbol).upper(),
        "action": str(action).upper(),
        "quantity": qty,
        "status": status,
        "attribution": {
            "source_job": source_job,
            "source_task": source_task,
        },
    }
    if fill:
        payload["fill"] = dict(fill)
    return payload


def lifecycle_event_from_confirmation(
    confirmation: Mapping[str, Any],
    *,
    source_job: str,
    source_task: str,
    broker: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Normalize a broker confirmation into a lifecycle audit event."""
    status = str(confirmation.get("status") or "").lower()
    event = "filled" if status == "filled" else "submitted"
    symbol = confirmation.get("symbol") or confirmation.get("ticker")
    quantity = confirmation.get("filled_qty") or confirmation.get("quantity") or confirmation.get("qty")
    return build_order_lifecycle_event(
        event=event,
        source_job=source_job,
        source_task=source_task,
        broker=broker,
        symbol=str(symbol or ""),
        action=str(confirmation.get("action") or confirmation.get("side") or ""),
        quantity=float(quantity or 0),
        order_id=str(confirmation.get("order_id") or ""),
        run_id=run_id,
        status=status or None,
        fill={
            "filled_qty": confirmation.get("filled_qty"),
            "filled_avg_price": confirmation.get("filled_avg_price"),
            "filled_at": confirmation.get("filled_at"),
        },
    )


__all__ = [
    "LIFECYCLE_SCHEMA_VERSION",
    "VALID_LIFECYCLE_EVENTS",
    "build_order_lifecycle_event",
    "lifecycle_event_from_confirmation",
]
