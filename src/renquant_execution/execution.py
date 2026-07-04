"""Execution-pipeline contract.

The broker implementation is injected behind this contract so tests can verify
order handling without live account mutation.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from renquant_common import Job, Pipeline, Task

from .broker import BaseBroker, is_no_submit_status, normalize_order_intent


@dataclass
class ExecutionContext:
    broker_name: str
    order_intents: list[dict[str, Any]]
    submitted_orders: list[dict[str, Any]] = field(default_factory=list)
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True


BrokerSubmitter = Callable[[str, list[dict[str, Any]], bool], list[dict[str, Any]]]


def broker_submitter(broker: BaseBroker) -> BrokerSubmitter:
    """Build a submitter that executes normalized order intents on a broker."""

    def submit(broker_name: str, order_intents: list[dict[str, Any]], dry_run: bool) -> list[dict[str, Any]]:
        if broker_name != broker.broker_name:
            raise ValueError(f"broker_name mismatch: ctx={broker_name} broker={broker.broker_name}")
        orders: list[dict[str, Any]] = []
        for intent in order_intents:
            normalized = normalize_order_intent(intent)
            if dry_run:
                orders.append({
                    "order_id": f"dry-{len(orders) + 1}",
                    "status": "dry_run",
                    **normalized,
                })
            else:
                orders.append(broker.place_order(**normalized))
        return orders

    return submit


class ValidateOrderIntentsTask(Task):
    def run(self, ctx: ExecutionContext) -> bool | None:
        if not ctx.broker_name:
            raise ValueError("broker_name is required")
        for idx, intent in enumerate(ctx.order_intents):
            try:
                normalize_order_intent(intent)
            except ValueError as exc:
                raise ValueError(f"order_intents[{idx}] {exc}") from exc
        return True


class SubmitOrdersTask(Task):
    def __init__(self, submitter: BrokerSubmitter) -> None:
        self.submitter = submitter

    def run(self, ctx: ExecutionContext) -> bool | None:
        ctx.submitted_orders = self.submitter(ctx.broker_name, ctx.order_intents, ctx.dry_run)
        return True


class AuditExecutionTask(Task):
    def run(self, ctx: ExecutionContext) -> bool | None:
        # A no-submit result (e.g. a fractional intent skipped on a
        # non-fractionable asset) never reached the broker, so it must not be
        # counted as submitted — that would be a false operational state.
        n_skipped = sum(
            1 for order in ctx.submitted_orders if is_no_submit_status(order.get("status"))
        )
        ctx.audit_rows.append({
            "broker": ctx.broker_name,
            "dry_run": ctx.dry_run,
            "n_intents": len(ctx.order_intents),
            "n_submitted": len(ctx.submitted_orders) - n_skipped,
            "n_skipped": n_skipped,
        })
        return True


class ExecutionJob(Job):
    def __init__(self, submitter: BrokerSubmitter) -> None:
        self._tasks = [ValidateOrderIntentsTask(), SubmitOrdersTask(submitter), AuditExecutionTask()]

    @property
    def tasks(self) -> list[Task]:
        return self._tasks


class ExecutionPipeline(Pipeline):
    def __init__(self, submitter: BrokerSubmitter) -> None:
        super().__init__([ExecutionJob(submitter)], name="execution")


class BrokerExecutionPipeline(ExecutionPipeline):
    """Execution pipeline wired directly to a BaseBroker implementation."""

    def __init__(self, broker: BaseBroker) -> None:
        super().__init__(broker_submitter(broker))


def execution_payload(ctx: ExecutionContext) -> dict[str, Any]:
    """Return the JSON payload consumed by native live-bundle tooling."""
    return {
        "schema_version": 1,
        "source": "renquant_execution.execution",
        "broker_name": ctx.broker_name,
        "dry_run": bool(ctx.dry_run),
        "order_intents": list(ctx.order_intents),
        "submitted_orders": list(ctx.submitted_orders),
        "execution_audit": list(ctx.audit_rows),
    }


def write_execution_payload(ctx: ExecutionContext, path: str | Path) -> Path:
    """Write the execution payload as deterministic JSON."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(execution_payload(ctx), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out
